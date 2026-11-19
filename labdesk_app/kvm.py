"""Host-KVM network VMs (Juniper vJunos-router, Fortinet FortiOS) via virsh/virt-install.

Windows: the same functions run the VMs in VMware Workstation instead (:mod:`.vmware`, the LabDesk-VMs
bundle) - callers never see the difference; VMware errors come back as :class:`KvmError`.

All commands are built as argv lists (never shell=True); names are validated.
Disk layout: each image is uploaded ONCE into the libvirt pool as a base volume, every VM
gets a thin qcow2 overlay on top of it (fast create, small footprint).
"""
from __future__ import annotations

import json
import re
import subprocess
import zipfile
from dataclasses import dataclass
from pathlib import Path

from . import config, plat, validate

URI = config.LIBVIRT_URI
POOL = config.KVM_POOL


@dataclass(frozen=True)
class KvmProfile:
    kind: str
    vcpus: int
    memory_mb: int
    base_volume: str
    first_nic: str             # name of NIC 0 inside the guest (management)
    data_nic_fmt: str          # name of data NIC n (0-based) inside the guest
    min_avail_mb: int = 0      # warn when host MemAvailable is below this


PROFILES = {
    "vjunos": KvmProfile("vjunos", 4, 5120, "labdesk-base-vjunos-26.2R1.7.qcow2", "fxp0", "ge-0/0/{n}",
                         min_avail_mb=6144),
    "fortios": KvmProfile("fortios", 1, 2048, "labdesk-base-fortios-7.6.8.qcow2", "port1", "port{n2}"),
}


class KvmError(RuntimeError):
    pass


def _vmware():
    """The VMware backend module when LabDesk runs its VMs in VMware (Windows + bundle), else None."""
    if not plat.IS_WINDOWS:
        return None
    from . import vmware
    return vmware if vmware.active() else None


def _vm_call(fn, *args, **kw):
    from .vmware import VmwareError
    try:
        return fn(*args, **kw)
    except VmwareError as e:
        raise KvmError(str(e)) from None


# ----------------------------------------------------------------- builders
def virsh(*args: str) -> list[str]:
    return ["virsh", "-c", URI, *args]


def vol_name(vm: str, suffix: str = "") -> str:
    return f"{validate.virsh_name(vm)}{suffix}.qcow2"


def build_overlay_cmd(vm: str, profile: KvmProfile, capacity_bytes: int) -> list[str]:
    return virsh("vol-create-as", POOL, vol_name(vm), str(int(capacity_bytes)),
                 "--format", "qcow2", "--backing-vol", profile.base_volume,
                 "--backing-vol-format", "qcow2")


def build_blank_vol_cmd(name: str, size: str = "2G") -> list[str]:
    if not re.fullmatch(r"\d+[KMG]?", size):
        raise validate.ValidationError("bad size")
    return virsh("vol-create-as", POOL, name, size, "--format", "qcow2")


def build_base_upload_cmds(base_volume: str, src: Path, size_bytes: int) -> list[list[str]]:
    return [virsh("vol-create-as", POOL, base_volume, str(int(size_bytes)), "--format", "qcow2"),
            virsh("vol-upload", "--pool", POOL, base_volume, str(src))]


def build_install_cmd(vm: str, kind: str) -> list[str]:
    vm = validate.virsh_name(vm)
    p = PROFILES[kind]
    cmd = ["virt-install", "--connect", URI, "--name", vm,
           "--memory", str(p.memory_mb), "--vcpus", str(p.vcpus),
           "--osinfo", "detect=on,require=off",
           "--import", "--noautoconsole", "--noreboot",
           "--graphics", "none", "--console", "pty,target_type=serial",
           "--disk", f"vol={POOL}/{vol_name(vm)},bus=virtio,cache=writeback",
           "--network", "network=default,model=virtio"]          # NIC 0 = management
    if kind == "vjunos":
        cmd += ["--cpu", "host-passthrough",
                "--sysinfo", "type=smbios,system.manufacturer=Juniper,system.product=VM-VMX,"
                             "system.family=lab"]
    elif kind == "fortios":
        cmd += ["--disk", f"vol={POOL}/{vol_name(vm, '-log')},bus=virtio"]
    return cmd


def build_console_cmd(vm: str) -> list[str]:
    return virsh("console", validate.virsh_name(vm), "--force")


def build_start_cmd(vm: str) -> list[str]:
    return virsh("start", validate.virsh_name(vm))


def build_stop_cmd(vm: str, force: bool = False) -> list[str]:
    return virsh("destroy" if force else "shutdown", validate.virsh_name(vm))


def build_undefine_cmd(vm: str) -> list[str]:
    return virsh("undefine", validate.virsh_name(vm), "--remove-all-storage", "--nvram")


def build_attach_nic_cmd(vm: str, network: str, live: bool) -> list[str]:
    cmd = virsh("attach-interface", "--domain", validate.virsh_name(vm), "--type", "network",
                "--source", network, "--model", "virtio", "--config")
    if live:
        cmd.append("--live")
    return cmd


def isolated_network_xml(name: str) -> str:
    if not re.fullmatch(r"labdesk-[A-Za-z0-9_.\-]+", name):
        raise validate.ValidationError("bad network name")
    # no <forward>, no <ip>: a pure isolated L2 segment between two VMs
    return (f"<network>\n  <name>{name}</name>\n  <bridge stp='off' delay='0'/>\n"
            f"</network>\n")


def parse_domstate(text: str) -> str:
    t = (text or "").strip().lower()
    if t.startswith("running"):
        return "running"
    if t.startswith("shut off") or t.startswith("shutoff"):
        return "stopped"
    if t.startswith("paused"):
        return "paused"
    if "shutdown" in t or "in shutdown" in t:
        return "stopping"
    return t or "unknown"


def parse_domifaddr(text: str) -> str | None:
    for line in (text or "").splitlines():
        m = re.search(r"ipv4\s+(\d+\.\d+\.\d+\.\d+)/\d+", line)
        if m:
            return m.group(1)
    return None


def mem_available_mb(meminfo: str | None = None) -> int:
    if meminfo is None and plat.IS_WINDOWS:
        from .telemetry import host_mem_available_gb
        gb = host_mem_available_gb()
        return int(gb * 1024) if gb else 0
    if meminfo is None:
        try:
            meminfo = Path("/proc/meminfo").read_text(encoding="utf-8")
        except OSError:
            return 0
    m = re.search(r"^MemAvailable:\s+(\d+)\s+kB", meminfo, re.M)
    return int(m.group(1)) // 1024 if m else 0


def memory_warning(kind: str, meminfo: str | None = None) -> str | None:
    p = PROFILES.get(kind)
    if not p or not p.min_avail_mb:
        return None
    avail = mem_available_mb(meminfo)
    if avail < p.min_avail_mb:
        return (f"Only {avail / 1024:.1f} GB RAM available on the host; {kind} needs "
                f"{p.memory_mb / 1024:.0f} GB (recommended free: {p.min_avail_mb / 1024:.0f} GB). "
                "Starting it may make the host swap or trigger the OOM killer.")
    return None


# ------------------------------------------------------------------ runners
def run(cmd: list[str], timeout: float = 120, input_text: str | None = None) -> str:
    try:
        cp = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, input=input_text,
                            **plat.no_window_kwargs())
    except FileNotFoundError:
        raise KvmError(f"{cmd[0]} not installed") from None
    except subprocess.TimeoutExpired:
        raise KvmError(f"{' '.join(cmd[:4])} timed out") from None
    if cp.returncode != 0:
        raise KvmError((cp.stderr or cp.stdout).strip() or f"{cmd[0]} failed ({cp.returncode})")
    return cp.stdout


def domstate(vm: str) -> str:
    vw = _vmware()
    if vw:
        try:
            return vw.domstate(vm)
        except (vw.VmwareError, OSError):
            return "unknown"
    try:
        return parse_domstate(run(virsh("domstate", validate.virsh_name(vm)), timeout=10))
    except KvmError as e:
        if "failed to get domain" in str(e).lower() or "not found" in str(e).lower():
            return "missing"
        return "unknown"


def mgmt_ip(vm: str) -> str | None:
    vw = _vmware()
    if vw:
        try:
            return vw.mgmt_ip(vm)
        except (vw.VmwareError, OSError):
            return None
    for source in ("lease", "arp"):
        try:
            ip = parse_domifaddr(run(virsh("domifaddr", validate.virsh_name(vm), "--source", source), timeout=10))
        except KvmError:
            ip = None
        if ip:
            return ip
    return None


def _vol_exists(name: str) -> bool:
    try:
        run(virsh("vol-info", "--pool", POOL, name), timeout=15)
        return True
    except KvmError:
        return False


def _qcow2_virtual_size(path: Path) -> int:
    out = run(["qemu-img", "info", "--output", "json", str(path)], timeout=30)
    return int(json.loads(out)["virtual-size"])


def _ensure_base(kind: str, log=print) -> int:
    """Upload the base image into the pool once; return its virtual size."""
    p = PROFILES[kind]
    if kind == "vjunos":
        src = config.VJUNOS_IMAGE
    else:
        src = config.CACHE_DIR / "fortios.qcow2"
        if not src.exists():
            if not config.FORTIOS_ZIP.exists():
                raise KvmError(f"FortiOS image not found: {config.FORTIOS_ZIP}")
            config.CACHE_DIR.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(config.FORTIOS_ZIP) as z:
                member = next((m for m in z.namelist() if m.endswith("fortios.qcow2")), None)
                if not member:
                    raise KvmError("fortios.qcow2 not found inside the zip")
                log("Extracting fortios.qcow2 ...")
                with z.open(member) as fin, open(src, "wb") as fout:
                    while chunk := fin.read(1 << 20):
                        fout.write(chunk)
    if not src.exists():
        raise KvmError(f"image not found: {src}")
    vsize = _qcow2_virtual_size(src)
    if not _vol_exists(p.base_volume):
        log(f"Uploading {src.name} into pool '{POOL}' (once) ...")
        for c in build_base_upload_cmds(p.base_volume, src, src.stat().st_size):
            run(c, timeout=1800)
    return vsize


def create_vm(vm: str, kind: str, log=print) -> None:
    vm = validate.virsh_name(vm)
    vw = _vmware()
    if vw:
        return _vm_call(vw.create_vm, vm, kind, log=log)
    if domstate(vm) != "missing":
        raise KvmError(f"a libvirt VM named {vm} already exists")
    vsize = _ensure_base(kind, log)
    run(build_overlay_cmd(vm, PROFILES[kind], vsize), timeout=60)
    if kind == "fortios":
        run(build_blank_vol_cmd(vol_name(vm, "-log"), "2G"), timeout=60)
    log(f"Defining VM {vm} ...")
    run(build_install_cmd(vm, kind), timeout=180)


def start_vm(vm: str):
    vw = _vmware()
    if vw:
        return _vm_call(vw.start_vm, vm)
    run(build_start_cmd(vm), timeout=60)


def stop_vm(vm: str, force: bool = True):
    vw = _vmware()
    if vw:
        return _vm_call(vw.stop_vm, vm, force)
    run(build_stop_cmd(vm, force), timeout=60)


def delete_vm(vm: str):
    vw = _vmware()
    if vw:
        return _vm_call(vw.delete_vm, vm)
    if domstate(vm) == "running":
        stop_vm(vm, force=True)
    run(build_undefine_cmd(vm), timeout=60)


def ensure_isolated_network(name: str) -> None:
    if _vmware():
        isolated_network_xml(name)          # same name check; a VMware LAN segment needs no creation
        return
    try:
        run(virsh("net-info", name), timeout=10)
        exists = True
    except KvmError:
        exists = False
    if not exists:
        run(virsh("net-define", "/dev/stdin"), timeout=20, input_text=isolated_network_xml(name))
    info = run(virsh("net-info", name), timeout=10)
    if re.search(r"Active:\s+no", info):
        run(virsh("net-start", name), timeout=20)


def delete_network(name: str) -> None:
    if _vmware():
        return                              # LAN segments vanish with their last NIC
    for c in (virsh("net-destroy", name), virsh("net-undefine", name)):
        try:
            run(c, timeout=20)
        except KvmError:
            pass


def parse_domiflist_networks(text: str) -> set[str]:
    """Libvirt network names in ``virsh domiflist`` output."""
    return set(re.findall(r"^\s*\S+\s+network\s+(\S+)\s", text or "", re.M))


def network_users(name: str) -> set[str]:
    """Domains (running or not) with a NIC on libvirt network ``name``."""
    vw = _vmware()
    if vw:
        return _vm_call(vw.network_users, name)
    users = set()
    for vm in run(virsh("list", "--all", "--name"), timeout=15).split():
        try:
            if name in parse_domiflist_networks(run(virsh("domiflist", vm, "--inactive"), timeout=15)):
                users.add(vm)
        except KvmError:
            pass
    return users


def attach_nic(vm: str, network: str) -> None:
    vw = _vmware()
    if vw:
        _vm_call(vw.attach_nic, vm, network)
        return
    run(build_attach_nic_cmd(vm, network, live=domstate(vm) == "running"), timeout=30)


def nic_macs(vm: str) -> list[str]:
    """MAC addresses of the VM's NICs, in order."""
    vw = _vmware()
    if vw:
        try:
            return vw.nic_macs(vm)
        except (vw.VmwareError, OSError):
            return []
    try:
        out = run(virsh("domiflist", validate.virsh_name(vm)), timeout=10)
    except KvmError:
        return []
    return re.findall(r"([0-9a-f]{2}(?::[0-9a-f]{2}){5})", out)


def detach_nic(vm: str, mac: str) -> None:
    """Remove the NIC with this MAC (live too when the VM runs).  Errors are ignored like a missing NIC."""
    vw = _vmware()
    if vw:
        try:
            vw.detach_nic(vm, mac)
        except (vw.VmwareError, OSError):
            pass
        return
    cmd = virsh("detach-interface", "--domain", validate.virsh_name(vm), "--type", "network", "--mac", mac, "--config")
    if domstate(vm) == "running":
        cmd.append("--live")
    try:
        run(cmd, timeout=30)
    except KvmError:
        pass


def start_cml_vm(log=print) -> None:
    """Start the CML controller VM: ``virsh start cml`` (Linux) or VMware from the bundle (Windows)."""
    from . import config as _c
    vw = _vmware()
    if vw:
        _vm_call(vw.start_cml, log=log)
        return
    try:
        run(virsh("start", _c.CML_VM_NAME), timeout=60)
    except KvmError as e:
        if "already active" not in str(e) and "already running" not in str(e).lower():
            raise


def guest_nic_name(kind: str, index: int) -> str:
    """Name inside the guest of NIC #index (0 = management)."""
    p = PROFILES[kind]
    if index == 0:
        return p.first_nic
    return p.data_nic_fmt.format(n=index - 1, n2=index + 1)
