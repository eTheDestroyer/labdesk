"""VMware Workstation backend: LabDesk for Windows runs CML and the host VMs (vJunos, FortiOS) itself.

KVM exists only on Linux.  On Windows, LabDesk drives VMware Workstation (free for personal use) with
``vmrun`` and the VMs of the **LabDesk-VMs bundle** - a folder built once on the Linux PC by
``tools/make_windows_bundle.py`` and copied next to LabDesk on Windows:

    LabDesk-VMs/
      labdesk-bundle.json        manifest (what is inside, CML address, NIC MACs)
      cml-credentials.env        the CML login (copied to %APPDATA%\\LabDesk on first start: no dialog)
      CML/CML.vmx, cml.vmdk, refplat.iso          the CML controller with its router images
      templates/vjunos/…, templates/fortios/…     base VMs; each device is a linked clone of one
      devices/<name>/<name>.vmx                   created by LabDesk (Juniper / Fortinet devices)
      installer/VMware-workstation-*.exe          optional: installed silently on first start

Networks (same addresses as the libvirt networks on Linux, so labs and configs work unchanged):

* ``default`` (libvirt virbr0, 192.168.122.0/24) -> a host-only VMware network ("VMnetN") with the host
  at 192.168.122.1 and DHCP: the CML VM (192.168.122.60), the host VMs' management NIC and every CML
  "System Bridge" connector share it.  Created once with vnetlib (one Windows admin prompt).
* isolated ``labdesk-*`` networks -> VMware LAN segments (pure L2, no host adapter, no admin rights).

Linux-only pieces that live inside the CML VM on Windows: the Internet cloud (service ``labdesk-cloud``
inside CML, started only under VMware; see :mod:`.cloud`).

Pure helpers (vmx parse/format, ids, MACs, lease parsing) are unit-tested on every platform; everything
that runs ``vmrun`` / touches the registry is Windows-only and guarded.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import config, plat, validate

BUNDLE_DIR_NAME = "LabDesk-VMs"
MANIFEST = "labdesk-bundle.json"
BUNDLE_FORMAT = 1
LAB_SUBNET, LAB_MASK, LAB_HOST_IP = "192.168.122.0", "255.255.255.0", "192.168.122.1"
VNET_SETTING = "vmware/vnet"
VHW = "19"                              # virtual hardware version: VMware Workstation 16.2 and newer
SNAPSHOT = "labdesk-base"


class VmwareError(RuntimeError):
    pass


# =========================================================================== pure helpers
def pvn_id(network: str) -> str:
    """Stable VMware LAN-segment id for an isolated network name (both VMs of a link use the same id)."""
    h = hashlib.md5(("labdesk:" + network).encode()).hexdigest()
    b = [h[i:i + 2] for i in range(0, 32, 2)]
    return " ".join(b[:8]) + "-" + " ".join(b[8:])


def static_mac(seed: str) -> str:
    """A VMware static MAC (00:50:56:00:00:00 - 00:50:56:3F:FF:FF) derived from ``seed``."""
    h = hashlib.sha1(seed.encode()).digest()
    return "00:50:56:%02x:%02x:%02x" % (h[0] & 0x3F, h[1], h[2])


class Vmx:
    """A .vmx file: ordered ``key = "value"`` lines, keys case-insensitive (as VMware treats them)."""

    def __init__(self, text: str = ""):
        self.items: list[list[str]] = []
        for line in text.splitlines():
            m = re.match(r'^\s*([^=#\s][^=]*?)\s*=\s*"(.*)"\s*$', line)
            if m:
                self.items.append([m.group(1), m.group(2)])

    @classmethod
    def load(cls, path: Path) -> Vmx:
        return cls(Path(path).read_text(encoding="utf-8", errors="replace"))

    def _find(self, key: str) -> int:
        k = key.lower()
        return next((i for i, (kk, _v) in enumerate(self.items) if kk.lower() == k), -1)

    def get(self, key: str, default: str | None = None) -> str | None:
        i = self._find(key)
        return self.items[i][1] if i >= 0 else default

    def set(self, key: str, value) -> None:
        value = str(value)
        if '"' in value or "\n" in value:
            raise ValueError(f"bad vmx value for {key}")
        i = self._find(key)
        if i >= 0:
            self.items[i][1] = value
        else:
            self.items.append([key, value])

    def update(self, values: dict) -> None:
        for k, v in values.items():
            self.set(k, v)

    def remove_prefix(self, prefix: str) -> None:
        p = prefix.lower()
        self.items = [kv for kv in self.items if not kv[0].lower().startswith(p)]

    def keys(self) -> list[str]:
        return [k for k, _v in self.items]

    def text(self) -> str:
        items = [kv for kv in self.items if kv[0].lower() != ".encoding"]
        return ".encoding = \"UTF-8\"\n" + "".join(f'{k} = "{v}"\n' for k, v in items)

    def save(self, path: Path) -> None:
        path = Path(path)
        tmp = path.with_name(path.name + ".labdesk-tmp")
        tmp.write_text(self.text(), encoding="utf-8", newline="\n")
        os.replace(tmp, path)

    # ------------------------------------------------------------------ NICs
    def nic_indexes(self) -> list[int]:
        out = set()
        for k in self.keys():
            m = re.fullmatch(r"ethernet(\d+)\.present", k, re.I)
            if m and (self.get(k) or "").upper() == "TRUE":
                out.add(int(m.group(1)))
        return sorted(out)

    def nics(self) -> list[dict]:
        """[{index, mac, network}] for present NICs (network: 'default' / LAN segment name / vnet)."""
        out = []
        for i in self.nic_indexes():
            p = f"ethernet{i}."
            ctype = (self.get(p + "connectionType") or "").lower()
            net = self.get(p + "labdesk.network") or (self.get(p + "vnet") if ctype == "custom" else ctype)
            mac = (self.get(p + "address") or self.get(p + "generatedAddress") or "").lower()
            out.append({"index": i, "mac": mac, "network": net or ""})
        return out

    def add_nic(self, network: str, vnet: str, mac: str, dev: str = "vmxnet3", index: int | None = None) -> int:
        if index is None:
            used = set(self.nic_indexes())
            index = next(i for i in range(0, 10) if i not in used)
        self.update(nic_values(index, network, vnet, mac, dev))
        return index

    def remove_nic(self, mac: str) -> bool:
        for n in self.nics():
            if n["mac"] == mac.lower():
                self.remove_prefix(f"ethernet{n['index']}.")
                return True
        return False


def nic_values(index: int, network: str, vnet: str, mac: str, dev: str = "vmxnet3") -> dict:
    """vmx lines of one NIC: ``default`` -> the LabDesk host-only VMnet, anything else -> a LAN segment."""
    p = f"ethernet{index}."
    v = {p + "present": "TRUE", p + "virtualDev": dev, p + "addressType": "static", p + "address": mac,
         p + "checkMACAddress": "FALSE", p + "startConnected": "TRUE", p + "labdesk.network": network}
    if network == "default":
        v.update({p + "connectionType": "custom", p + "vnet": vnet})
    else:
        v.update({p + "connectionType": "pvn", p + "pvnID": pvn_id(network)})
    return v


def base_vmx(name: str, guest: str, mem_mb: int, cpus: int, efi: bool, nested: bool) -> Vmx:
    v = Vmx()
    v.update({
        "config.version": "8", "virtualHW.version": VHW, "displayName": name, "guestOS": guest,
        "memsize": mem_mb, "numvcpus": cpus, "cpuid.coresPerSocket": cpus,
        "pciBridge0.present": "TRUE",
        **{f"pciBridge{n}.{k}": val for n in (4, 5, 6, 7)
           for k, val in (("present", "TRUE"), ("virtualDev", "pcieRootPort"), ("functions", "8"))},
        "vmci0.present": "TRUE", "hpet0.present": "TRUE", "floppy0.present": "FALSE",
        "sata0.present": "TRUE", "svga.vramSize": "8388608",
        "tools.syncTime": "FALSE", "uuid.action": "create", "msg.autoAnswer": "TRUE",
        "mks.enable3d": "FALSE", "usb.present": "FALSE", "sound.present": "FALSE",
    })
    if efi:
        v.set("firmware", "efi")
    if nested:
        v.set("vhv.enable", "TRUE")
    return v


def cml_vmx(nics: list[tuple[str, str]], vnet: str, mem_mb: int = 8192, cpus: int = 4) -> Vmx:
    """The CML controller: EFI, nested VT-x, SATA disk + the refplat ISO, its NICs with the MACs it had
    under KVM (the bundle builder pinned the guest's NIC names to those MACs)."""
    v = base_vmx("LabDesk CML", "ubuntu-64", mem_mb, cpus, efi=True, nested=True)
    v.update({"sata0:0.present": "TRUE", "sata0:0.fileName": "cml.vmdk",
              "sata0:1.present": "TRUE", "sata0:1.deviceType": "cdrom-image", "sata0:1.fileName": "refplat.iso",
              "labdesk.role": "cml"})
    for i, (mac, network) in enumerate(nics):
        v.add_nic(network, vnet, mac, index=i)
    return v


TEMPLATES = {
    # kind: (guest OS, memory MB, vCPUs, nested VT-x, disks, NIC model)
    "vjunos": ("other5xlinux-64", 5120, 4, True, ["vjunos.vmdk"], "vmxnet3"),
    "fortios": ("other5xlinux-64", 2048, 1, False, ["fortios.vmdk", "fortios-log.vmdk"], "e1000"),
}


def template_vmx(kind: str, vnet: str) -> Vmx:
    guest, mem, cpus, nested, disks, dev = TEMPLATES[kind]
    v = base_vmx(f"LabDesk template {kind}", guest, mem, cpus, efi=False, nested=nested)
    for i, d in enumerate(disks):
        v.update({f"sata0:{i}.present": "TRUE", f"sata0:{i}.fileName": d})
    v.add_nic("default", vnet, static_mac(f"template-{kind}"), dev=dev, index=0)
    v.set("labdesk.role", f"template-{kind}")
    v.set("labdesk.nicdev", dev)
    if kind == "vjunos":
        # vJunos-router looks for the lab SMBIOS identity (libvirt: system.product=VM-VMX, family=lab)
        v.update({"smbios.reflectHost": "FALSE", "hw.model": "VM-VMX", "smbios.family": "lab"})
    return v


def serial_pipe_values(vm: str) -> dict:
    return {"serial0.present": "TRUE", "serial0.fileType": "pipe", "serial0.fileName": pipe_name(vm),
            "serial0.pipe.endPoint": "server", "serial0.tryNoRxLoss": "TRUE", "serial0.yieldOnMsrRead": "TRUE"}


def pipe_name(vm: str) -> str:
    return r"\\.\pipe\labdesk-" + validate.virsh_name(vm)


def parse_leases(text: str) -> dict[str, str]:
    """VMware vmnetdhcp.leases (ISC dhcpd format) -> {mac: newest ip}."""
    out: dict[str, tuple[str, str]] = {}
    for m in re.finditer(r"lease\s+(\d+\.\d+\.\d+\.\d+)\s*\{(.*?)\}", text or "", re.S):
        ip, body = m.group(1), m.group(2)
        hw = re.search(r"hardware ethernet\s+([0-9a-fA-F:]{17})", body)
        if not hw:
            continue
        start = re.search(r"starts\s+\d\s+([\d/: ]+);", body)
        stamp = start.group(1) if start else ""
        mac = hw.group(1).lower()
        if mac not in out or stamp >= out[mac][1]:
            out[mac] = (ip, stamp)
    return {k: v[0] for k, v in out.items()}


def parse_vmrun_list(text: str) -> set[str]:
    """``vmrun list`` -> normalised paths of the running VMs."""
    return {norm(line.strip()) for line in (text or "").splitlines()[1:] if line.strip().lower().endswith(".vmx")}


def norm(p) -> str:
    return os.path.normcase(os.path.abspath(str(p)))


def sizing(total_ram_mb: int, logical_cpus: int) -> tuple[int, int]:
    """CML VM memory / vCPUs for this PC: 8 GB when the PC has 14 GB or more (else leave 6 GB to Windows,
    never below 4 GB); vCPUs = logical CPUs - 2 (2 to 8)."""
    mem = 8192 if total_ram_mb >= 14 * 1024 else max(4096, total_ram_mb - 6144)
    cpus = max(2, min(8, logical_cpus - 2))
    return mem, cpus


VNETLIB_STEPS = ("-- stop dhcp", "-- stop nat", "-- add adapter {v}", "-- set vnet {v} mask {mask}",
                 "-- set vnet {v} addr {subnet}", "-- set adapter {v} addr {host}", "-- add dhcp {v}",
                 "-- update dhcp {v}", "-- update adapter {v}", "-- start dhcp", "-- start nat")


RESERVATION = "labdesk-cml"


def reservation_block(mac: str, ip: str) -> list[str]:
    """ISC dhcpd host declaration: CML always gets ``ip`` (on Linux it kept its libvirt lease .60)."""
    return [f"host {RESERVATION} {{", f"    hardware ethernet {mac};", f"    fixed-address {ip};", "}"]


def has_reservation(conf_text: str) -> bool:
    return re.search(rf"^\s*host\s+{RESERVATION}\s*\{{", conf_text or "", re.M) is not None


def vnet_setup_script(vnetlib: str | None, vnet: str | None, cml_mac: str | None = None,
                      cml_ip: str | None = None, dhcp_conf: str | None = None) -> str:
    """cmd.exe script (run elevated once): creates the LabDesk host-only network (when ``vnet``) and the
    DHCP reservation of the CML VM (when ``cml_mac``), then restarts VMware's DHCP service."""
    lines = ["@echo off", "rem LabDesk: host-only VMware network for CML (192.168.122.0/24, host .1, DHCP)"]
    if vnet:
        for step in VNETLIB_STEPS:
            lines.append(f'"{vnetlib}" ' + step.format(v=vnet.lower(), mask=LAB_MASK, subnet=LAB_SUBNET,
                                                       host=LAB_HOST_IP))
    if cml_mac and cml_ip and dhcp_conf:
        lines.append(f'findstr /r /c:"host {RESERVATION} " "{dhcp_conf}" >nul 2>nul || (')
        lines += [f'  >> "{dhcp_conf}" echo {line}' for line in [""] + reservation_block(cml_mac, cml_ip)]
        lines.append(")")
        lines += ["net stop VMnetDHCP >nul 2>nul", "net start VMnetDHCP >nul 2>nul"]
    lines.append("exit /b 0")
    return "\r\n".join(lines) + "\r\n"


def dhcp_conf_path() -> Path:
    return Path(os.environ.get("ProgramData", r"C:\ProgramData")) / "VMware" / "vmnetdhcp.conf"


# =========================================================================== the bundle
@dataclass
class Bundle:
    root: Path
    manifest: dict = field(default_factory=dict)

    @property
    def cml_vmx(self) -> Path:
        return self.root / self.manifest.get("cml", {}).get("vmx", "CML/CML.vmx")

    @property
    def cred_file(self) -> Path:
        return self.root / "cml-credentials.env"

    @property
    def devices_dir(self) -> Path:
        return self.root / "devices"

    def template(self, kind: str) -> Path | None:
        rel = self.manifest.get("templates", {}).get(kind)
        return self.root / rel if rel else None

    @property
    def kinds(self) -> list[str]:
        return [k for k in self.manifest.get("templates", {}) if (self.template(k) or Path()).exists()]

    def installer(self) -> Path | None:
        found = sorted((self.root / "installer").glob("VMware-workstation*.exe"))
        return found[-1] if found else None


def bundle_candidates() -> list[Path]:
    out = []
    env = os.environ.get("LABDESK_VMS")
    if env:
        out.append(Path(env))
    out += [config.APP_DIR / BUNDLE_DIR_NAME, config.APP_DIR.parent / BUNDLE_DIR_NAME]
    if plat.FROZEN:
        exe_dir = Path(sys.executable).resolve().parent
        out += [exe_dir / BUNDLE_DIR_NAME, exe_dir.parent / BUNDLE_DIR_NAME]
    out += [Path.home() / BUNDLE_DIR_NAME, Path.home() / "Documents" / BUNDLE_DIR_NAME,
            Path.home() / "Desktop" / BUNDLE_DIR_NAME, Path.home() / "Downloads" / BUNDLE_DIR_NAME]
    return out


_BUNDLE: Bundle | None = None


def find_bundle(refresh: bool = False) -> Bundle | None:
    """The LabDesk-VMs bundle on this PC (first candidate folder holding a valid manifest)."""
    global _BUNDLE
    if _BUNDLE is not None and not refresh and (_BUNDLE.root / MANIFEST).exists():
        return _BUNDLE
    _BUNDLE = None
    seen = set()
    for c in bundle_candidates():
        key = norm(c)
        if key in seen:
            continue
        seen.add(key)
        mf = c / MANIFEST
        try:
            data = json.loads(mf.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and data.get("format") == BUNDLE_FORMAT:
            _BUNDLE = Bundle(c.resolve(), data)
            break
    return _BUNDLE


def active() -> bool:
    """LabDesk runs its VMs in VMware here: Windows with a LabDesk-VMs bundle."""
    return plat.IS_WINDOWS and find_bundle() is not None


def activate() -> bool:
    """LabDesk start-up on Windows: with a bundle, the 'Linux only' features (host VMs, Internet cloud) are
    available - VMware runs them.  Also imports the bundle's CML login (no connection dialog)."""
    if not plat.IS_WINDOWS or find_bundle(refresh=True) is None:
        return False
    plat.HAS_KVM = True
    plat.HAS_CLOUD = bool(_BUNDLE and _BUNDLE.manifest.get("cml", {}).get("cloud_inside"))
    try:
        import_credentials()
    except OSError:
        pass
    return True


def import_credentials(bundle: Bundle | None = None) -> bool:
    """First start on Windows: take the CML login from the bundle (no connection dialog).  The saved file
    is never overwritten; missing keys (e.g. CML_SYSADMIN_PASS for the cloud) are added."""
    b = bundle or find_bundle()
    if b is None or not b.cred_file.exists():
        return False
    try:
        src = config.load_env_file(b.cred_file)
    except OSError:
        return False
    try:
        cur = config.load_env_file(config.CRED_FILE)
    except OSError:
        cur = {}
    add = {k: v for k, v in src.items() if v and not cur.get(k)}
    if not cur.get("CML_HOST"):
        add["CML_HOST"] = b.manifest.get("cml", {}).get("ip") or src.get("CML_HOST") or "192.168.122.60"
    if not add:
        return False
    config.update_env_file(add)
    config.reload_cml_host()
    return True


# =========================================================================== Windows runtime
def _vmware_dir() -> Path | None:
    exe = plat.vmrun_exe()
    return Path(exe).parent if exe else None


def vmrun(*args: str, timeout: float = 120) -> str:
    exe = plat.vmrun_exe()
    if not exe:
        raise VmwareError("VMware Workstation is not installed (vmrun.exe not found)")
    try:
        cp = subprocess.run([exe, "-T", "ws", *args], capture_output=True, text=True, timeout=timeout,
                            **plat.no_window_kwargs())
    except subprocess.TimeoutExpired:
        raise VmwareError(f"vmrun {args[0]} timed out") from None
    except OSError as e:
        raise VmwareError(f"vmrun: {e}") from None
    out = (cp.stdout or "") + (cp.stderr or "")
    if cp.returncode != 0 or out.strip().startswith("Error:"):
        raise VmwareError(explain(out.strip() or f"vmrun {args[0]} failed ({cp.returncode})"))
    return cp.stdout


def explain(msg: str) -> str:
    """vmrun's error + what to do about the usual ones."""
    low = msg.lower()
    if "virtualized intel vt-x" in low or "nested" in low or "hyper-v" in low or "not supported on this platform" in low:
        return (msg + "  -  CML needs nested virtualization, which VMware cannot offer while Windows' Hyper-V "
                "is on. Turn off Windows Security > Device security > Core isolation > Memory integrity, run "
                "'bcdedit /set hypervisorlaunchtype off' as administrator, and restart Windows.")
    if "is in use" in low or "locked" in low:
        return msg + "  -  the VM is open in the VMware Workstation window or another program; close it there."
    return msg


def running() -> set[str]:
    try:
        return parse_vmrun_list(vmrun("list", timeout=30))
    except VmwareError:
        return set()


def vm_state(vmx: Path) -> str:
    if not Path(vmx).exists():
        return "missing"
    return "running" if norm(vmx) in running() else "stopped"


def _settings():
    from .settings import app_settings
    return app_settings()


def _registry_vnets() -> dict[str, str]:
    """{vmnetN: subnet} from VMware's network configuration in the registry (Windows)."""
    out = {}
    try:
        import winreg
    except ImportError:
        return out
    for base in (r"SOFTWARE\WOW6432Node\VMware, Inc.\VMnetLib\VMnetConfig", r"SOFTWARE\VMware, Inc.\VMnetLib\VMnetConfig"):
        try:
            root = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, base)
        except OSError:
            continue
        i = 0
        while True:
            try:
                name = winreg.EnumKey(root, i)
            except OSError:
                break
            i += 1
            try:
                with winreg.OpenKey(root, name) as k:
                    subnet, _t = winreg.QueryValueEx(k, "IPSubnetAddress")
                    out[name.lower()] = str(subnet)
            except OSError:
                out.setdefault(name.lower(), "")
        break
    return out


def find_lab_vnet() -> str | None:
    saved = str(_settings().value(VNET_SETTING, "") or "")
    vnets = _registry_vnets()
    if saved and vnets.get(saved.lower()) == LAB_SUBNET:
        return saved
    for name, subnet in sorted(vnets.items()):
        if subnet == LAB_SUBNET:
            return name
    return saved or None


def run_elevated(script_text: str, name: str, timeout: float = 600) -> int:
    """Run a cmd script as administrator (one UAC prompt) and wait for it."""
    import ctypes
    from ctypes import wintypes
    d = plat.localappdata_dir()
    d.mkdir(parents=True, exist_ok=True)
    script = d / f"{name}.cmd"
    script.write_text(script_text, encoding="utf-8")

    class SHELLEXECUTEINFO(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("fMask", ctypes.c_ulong), ("hwnd", wintypes.HWND),
                    ("lpVerb", wintypes.LPCWSTR), ("lpFile", wintypes.LPCWSTR), ("lpParameters", wintypes.LPCWSTR),
                    ("lpDirectory", wintypes.LPCWSTR), ("nShow", ctypes.c_int), ("hInstApp", wintypes.HINSTANCE),
                    ("lpIDList", ctypes.c_void_p), ("lpClass", wintypes.LPCWSTR), ("hkeyClass", wintypes.HKEY),
                    ("dwHotKey", wintypes.DWORD), ("hIconOrMonitor", wintypes.HANDLE), ("hProcess", wintypes.HANDLE)]
    sei = SHELLEXECUTEINFO()
    sei.cbSize = ctypes.sizeof(sei)
    sei.fMask = 0x00000040                      # SEE_MASK_NOCLOSEPROCESS
    sei.lpVerb = "runas"
    sei.lpFile = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "cmd.exe")
    sei.lpParameters = f'/d /c "{script}"'
    sei.nShow = 0                               # SW_HIDE
    if not ctypes.windll.shell32.ShellExecuteExW(ctypes.byref(sei)):
        raise VmwareError("the administrator prompt was cancelled - LabDesk needs it once to set up VMware")
    try:
        ctypes.windll.kernel32.WaitForSingleObject(sei.hProcess, int(timeout * 1000))
        code = wintypes.DWORD()
        ctypes.windll.kernel32.GetExitCodeProcess(sei.hProcess, ctypes.byref(code))
        return int(code.value)
    finally:
        ctypes.windll.kernel32.CloseHandle(sei.hProcess)


def ensure_vmware(bundle: Bundle, log=print) -> None:
    if plat.vmrun_exe():
        return
    inst = bundle.installer()
    if inst is None:
        raise VmwareError("VMware Workstation is not installed. Install VMware Workstation Pro (free for personal "
                          "use, from Broadcom's download portal) - or put its installer "
                          f"(VMware-workstation-full-*.exe) into {bundle.root / 'installer'} - then start LabDesk again.")
    log(f"installing VMware Workstation from {inst.name} (one administrator prompt, a few minutes)…")
    rc = run_elevated(f'@echo off\r\n"{inst}" /s /v"/qn EULAS_AGREED=1 AUTOSOFTWAREUPDATE=0 DATACOLLECTION=0 '
                      f'REBOOT=ReallySuppress"\r\nexit /b %errorlevel%\r\n', "install-vmware", timeout=1800)
    if not plat.vmrun_exe():
        raise VmwareError(f"the VMware Workstation installer ended (code {rc}) but vmrun.exe is still missing")


def cml_mac(bundle: Bundle) -> str | None:
    nics = bundle.manifest.get("cml", {}).get("nics") or []
    return next((n["mac"] for n in nics if n.get("network") == "default"), None)


def ensure_network(bundle: Bundle | None = None, log=print) -> str:
    """The LabDesk host-only network (192.168.122.0/24) and CML's DHCP reservation: found, or created
    once (one administrator prompt for both)."""
    b = bundle or find_bundle()
    vnet = find_lab_vnet()
    mac = cml_mac(b) if b else None
    ip = (b.manifest.get("cml", {}).get("ip") if b else None) or "192.168.122.60"
    conf = dhcp_conf_path()
    try:
        reserved = has_reservation(conf.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        reserved = False
    need_res = bool(mac) and not reserved
    if vnet and not need_res:
        return vnet
    d = _vmware_dir()
    vnetlib = next((d / n for n in ("vnetlib64.exe", "vnetlib.exe") if d and (d / n).exists()), None)
    new_vnet = None
    if not vnet:
        if vnetlib is None:
            raise VmwareError("vnetlib.exe not found in the VMware Workstation folder")
        used = set(_registry_vnets())
        new_vnet = next(f"vmnet{n}" for n in (10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 2, 3, 4, 5, 6, 7, 9)
                        if f"vmnet{n}" not in used)
        log(f"creating the LabDesk network {new_vnet} (192.168.122.0/24, CML at {ip}; one administrator prompt)…")
    else:
        log(f"reserving {ip} for CML in VMware's DHCP (one administrator prompt)…")
    run_elevated(vnet_setup_script(str(vnetlib) if vnetlib else None, new_vnet, mac if need_res else None, ip,
                                   str(conf)), "labdesk-network", timeout=300)
    if new_vnet:
        if _registry_vnets().get(new_vnet) not in (LAB_SUBNET, None) and find_lab_vnet() is None:
            raise VmwareError(f"VMware did not create {new_vnet} with 192.168.122.0/24")
        vnet = new_vnet
        st = _settings()
        st.setValue(VNET_SETTING, vnet)
        st.sync()
    return vnet


def refresh_cml_address(bundle: Bundle | None = None) -> str | None:
    """CML got another address than the saved one (no reservation yet, or VMware's DHCP was reset): take
    the address of its lease, save it as CML_HOST and return it (else None)."""
    b = bundle or find_bundle()
    mac = cml_mac(b) if b else None
    if not mac:
        return None
    try:
        ip = parse_leases(leases_path().read_text(encoding="utf-8", errors="replace")).get(mac.lower())
    except OSError:
        return None
    if not ip or ip == config.CML_HOST:
        return None
    config.update_env_file({"CML_HOST": ip})
    config.reload_cml_host()
    return ip


def _host_resources() -> tuple[int, int]:
    total = 16 * 1024
    import ctypes
    if hasattr(ctypes, "windll"):

        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
        st = MEMORYSTATUSEX()
        st.dwLength = ctypes.sizeof(st)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):
            total = int(st.ullTotalPhys // (1024 * 1024))
    return total, os.cpu_count() or 4


def _point_at_vnet(vmx_path: Path, vnet: str, sized: bool = False) -> None:
    v = Vmx.load(vmx_path)
    changed = False
    for n in v.nics():
        p = f"ethernet{n['index']}."
        if (v.get(p + "labdesk.network") == "default") and (v.get(p + "vnet") or "").lower() != vnet.lower():
            v.set(p + "vnet", vnet)
            changed = True
    if sized:
        mem, cpus = sizing(*_host_resources())
        if v.get("memsize") != str(mem) or v.get("numvcpus") != str(cpus):
            v.update({"memsize": mem, "numvcpus": cpus, "cpuid.coresPerSocket": cpus})
            changed = True
    if changed:
        v.save(vmx_path)


def prepare(log=print) -> Bundle:
    """Everything the bundle needs before a VM starts: VMware, the network, the credentials."""
    b = find_bundle(refresh=True)
    if b is None:
        raise VmwareError(f"no {BUNDLE_DIR_NAME} folder found (put it next to LabDesk; see INSTALL.md)")
    import_credentials(b)
    ensure_vmware(b, log)
    vnet = ensure_network(b, log)
    if vm_state(b.cml_vmx) != "running":
        _point_at_vnet(b.cml_vmx, vnet, sized=True)
    return b


def start_cml(log=print) -> None:
    b = prepare(log)
    if vm_state(b.cml_vmx) == "running":
        return
    log("starting the CML VM in VMware Workstation…")
    vmrun("start", str(b.cml_vmx), "nogui", timeout=180)


def stop_cml() -> None:
    b = find_bundle()
    if b and vm_state(b.cml_vmx) == "running":
        vmrun("stop", str(b.cml_vmx), "soft", timeout=300)


# ------------------------------------------------------------------ host VMs (the kvm.py API on Windows)
def _bundle() -> Bundle:
    b = find_bundle()
    if b is None:
        raise VmwareError(f"no {BUNDLE_DIR_NAME} folder found")
    return b


def dev_vmx(vm: str) -> Path:
    vm = validate.virsh_name(vm)
    return _bundle().devices_dir / vm / f"{vm}.vmx"


def _pending_path(vmx: Path) -> Path:
    return vmx.with_suffix(".labdesk-pending.json")


def _load_pending(vmx: Path) -> list[dict]:
    try:
        return json.loads(_pending_path(vmx).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []


def _save_pending(vmx: Path, ops: list[dict]) -> None:
    p = _pending_path(vmx)
    if ops:
        p.write_text(json.dumps(ops), encoding="utf-8")
    elif p.exists():
        p.unlink()


def _apply_pending(vmx: Path) -> None:
    ops = _load_pending(vmx)
    if not ops:
        return
    v = Vmx.load(vmx)
    for op in ops:
        if op["op"] == "add":
            v.add_nic(op["network"], op["vnet"], op["mac"], dev=v.get("labdesk.nicdev") or "vmxnet3")
        elif op["op"] == "remove":
            v.remove_nic(op["mac"])
    v.save(vmx)
    _save_pending(vmx, [])


def domstate(vm: str) -> str:
    return vm_state(dev_vmx(vm))


def create_vm(vm: str, kind: str, log=print) -> None:
    b = prepare(log)
    tpl = b.template(kind)
    if tpl is None or not tpl.exists():
        raise VmwareError(f"the {BUNDLE_DIR_NAME} bundle has no {kind} image (rebuild it with that image present)")
    dest = dev_vmx(vm)
    if dest.exists():
        raise VmwareError(f"a VM named {vm} already exists")
    vnet = find_lab_vnet() or "vmnet10"
    _point_at_vnet(tpl, vnet)
    try:
        snaps = vmrun("listSnapshots", str(tpl), timeout=60)
    except VmwareError:
        snaps = ""
    if SNAPSHOT not in snaps.split():
        log(f"preparing the {kind} template (once)…")
        vmrun("snapshot", str(tpl), SNAPSHOT, timeout=300)
    dest.parent.mkdir(parents=True, exist_ok=True)
    log(f"creating {vm} (linked clone of the {kind} template)…")
    vmrun("clone", str(tpl), str(dest), "linked", f"-snapshot={SNAPSHOT}", f"-cloneName={vm}", timeout=600)
    v = Vmx.load(dest)
    v.set("displayName", vm)
    v.set("labdesk.role", f"device-{kind}")
    v.set("labdesk.kind", kind)
    v.remove_prefix("ethernet")
    v.add_nic("default", vnet, static_mac(f"{vm}-0"), dev=v.get("labdesk.nicdev") or "vmxnet3", index=0)
    v.update(serial_pipe_values(vm))
    v.save(dest)


def start_vm(vm: str) -> None:
    vmx = dev_vmx(vm)
    if vm_state(vmx) == "running":
        return
    _point_at_vnet(vmx, find_lab_vnet() or "vmnet10")
    _apply_pending(vmx)
    vmrun("start", str(vmx), "nogui", timeout=180)


def stop_vm(vm: str, force: bool = True) -> None:
    vmrun("stop", str(dev_vmx(vm)), "hard" if force else "soft", timeout=180)


def delete_vm(vm: str) -> None:
    vmx = dev_vmx(vm)
    if vm_state(vmx) == "running":
        stop_vm(vm, force=True)
    if vmx.exists():
        try:
            vmrun("deleteVM", str(vmx), timeout=180)
        except VmwareError:
            pass
    shutil.rmtree(vmx.parent, ignore_errors=True)


def nic_macs(vm: str) -> list[str]:
    vmx = dev_vmx(vm)
    if not vmx.exists():
        return []
    macs = [n["mac"] for n in Vmx.load(vmx).nics() if n["mac"]]
    for op in _load_pending(vmx):
        if op["op"] == "add":
            macs.append(op["mac"])
        elif op["op"] == "remove" and op["mac"] in macs:
            macs.remove(op["mac"])
    return macs


def attach_nic(vm: str, network: str) -> str:
    """Add a NIC on ``network`` ('default' or an isolated labdesk-* network).  A running VM gets it at its
    next start (like a libvirt NIC that appears in the guest after a reboot).  Returns the MAC."""
    vmx = dev_vmx(vm)
    vnet = find_lab_vnet() or "vmnet10"
    existing = set(nic_macs(vm))
    n = len(existing)
    mac = static_mac(f"{vm}-{network}-{n}")
    while mac in existing:
        n += 1
        mac = static_mac(f"{vm}-{network}-{n}")
    if vm_state(vmx) == "running":
        _save_pending(vmx, _load_pending(vmx) + [{"op": "add", "network": network, "vnet": vnet, "mac": mac}])
    else:
        v = Vmx.load(vmx)
        v.add_nic(network, vnet, mac, dev=v.get("labdesk.nicdev") or "vmxnet3")
        v.save(vmx)
    return mac


def detach_nic(vm: str, mac: str) -> None:
    vmx = dev_vmx(vm)
    if not vmx.exists():
        return
    pend = _load_pending(vmx)
    if any(op["op"] == "add" and op["mac"] == mac.lower() for op in pend):
        _save_pending(vmx, [op for op in pend if not (op["op"] == "add" and op["mac"] == mac.lower())])
    elif vm_state(vmx) == "running":
        _save_pending(vmx, pend + [{"op": "remove", "mac": mac.lower()}])
    else:
        v = Vmx.load(vmx)
        if v.remove_nic(mac):
            v.save(vmx)


def network_users(network: str) -> set[str]:
    users = set()
    b = find_bundle()
    if b is None:
        return users
    vmxs = list(b.devices_dir.glob("*/*.vmx")) + [b.cml_vmx]
    for vmx in vmxs:
        try:
            v = Vmx.load(vmx)
        except OSError:
            continue
        if any(n["network"] == network for n in v.nics()) or \
                any(op.get("network") == network for op in _load_pending(vmx) if op["op"] == "add"):
            users.add(vmx.stem if vmx != b.cml_vmx else config.CML_VM_NAME)
    return users


def leases_path() -> Path:
    return Path(os.environ.get("ProgramData", r"C:\ProgramData")) / "VMware" / "vmnetdhcp.leases"


def mgmt_ip(vm: str) -> str | None:
    vmx = dev_vmx(vm)
    if not vmx.exists():
        return None
    nics = Vmx.load(vmx).nics()
    mac = next((n["mac"] for n in nics if n["network"] == "default"), None)
    if not mac:
        return None
    try:
        return parse_leases(leases_path().read_text(encoding="utf-8", errors="replace")).get(mac)
    except OSError:
        return None


def wait_cml_api(timeout: float = 900, log=print) -> bool:
    from .cml_api import CMLClient
    end = time.time() + timeout
    client = CMLClient()
    while time.time() < end:
        if client.is_ready():
            return True
        time.sleep(5)
    return False
