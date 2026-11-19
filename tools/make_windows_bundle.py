#!/usr/bin/env python3
"""Build the LabDesk-VMs bundle for LabDesk on Windows (VMware Workstation), from this Linux PC's KVM VMs.

    cd ~/cml-labs/labdesk
    .venv/bin/python tools/make_windows_bundle.py [--out ~/LabDesk-VMs] [--yes]

What it does (each step prints a line; nothing on this PC changes except a short CML downtime):

1. **Prepares the CML VM for VMware** (through Cockpit, while CML runs; harmless under KVM):
   * pins the guest's NIC names to their MAC addresses (``/etc/systemd/network/10-labdesk-*.link``), so
     CML's network (bridge0 = 192.168.122.60 on enp1s0) comes up under VMware's NICs too;
   * installs the LabDesk Internet cloud INSIDE the CML VM (the same scripts as host/labdesk-cloud, bridge0),
     with ``ConditionVirtualization=vmware`` on every unit: it never runs under KVM (this PC keeps its own
     cloud), and starts by itself under VMware.
2. **Shuts CML down cleanly**, copies its disk as a VMware .vmdk and the refplat ISO, **starts CML again**
   (about 5 minutes of downtime; running labs must be started again afterwards).
3. Converts vJunos-router and FortiOS (from ~/Downloads, the same images LabDesk uses on Linux) into
   VMware templates.  Experimental on VMware: Juniper and Fortinet support these KVM images on KVM only.
4. Writes the VMware VM files, the manifest, the CML login (cml-credentials.env, owner-only) and a README.
   A VMware Workstation installer found in ~/Downloads (VMware-workstation-full-*.exe) is copied too:
   LabDesk then installs VMware silently on the first start.

The bundle holds licensed images and your CML password: copy it only to your own PCs (USB disk, your own
cloud drive), never publish it.  It is ignored by git (.gitignore: LabDesk-VMs/).
"""
from __future__ import annotations

import argparse
import base64
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import time
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

from labdesk_app import config, vmware  # noqa: E402
from labdesk_app.cockpit import Cockpit  # noqa: E402

URI = config.LIBVIRT_URI
CLOUD_SRC = ROOT / "host" / "labdesk-cloud"
CLOUD_UNITS = ("labdesk-cloud.service", "labdesk-cloud-check.service", "labdesk-cloud-check.timer",
               "labdesk-cloud-routes.path")


def say(msg: str) -> None:
    print(f"== {msg}", flush=True)


def sh(cmd: list[str], timeout: float = 3600, check: bool = True) -> str:
    cp = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if check and cp.returncode != 0:
        raise SystemExit(f"{' '.join(cmd[:3])} failed: {(cp.stderr or cp.stdout).strip()}")
    return cp.stdout


def virsh(*args: str, check: bool = True, timeout: float = 120) -> str:
    return sh(["virsh", "-c", URI, *args], timeout=timeout, check=check)


# ------------------------------------------------------------------ inspect the KVM CML VM
def cml_layout(vm: str) -> dict:
    root = ET.fromstring(virsh("dumpxml", "--inactive", vm))
    disk = iso = None
    for d in root.iter("disk"):
        src = d.find("source")
        f = src.get("file") if src is not None else None
        if not f:
            continue
        if d.get("device") == "disk" and disk is None:
            disk = f
        elif d.get("device") == "cdrom" and "refplat" in Path(f).name.lower():
            iso = f
    nics = []
    for i in root.iter("interface"):
        mac = i.find("mac").get("address").lower()
        src = i.find("source")
        net = (src.get("network") or src.get("bridge") or "default") if src is not None else "default"
        nics.append((mac, net))
    if not disk or not iso or not nics:
        raise SystemExit(f"could not read the disk / refplat ISO / NICs of the libvirt VM {vm!r}")
    return {"disk": disk, "iso": iso, "nics": nics}


# ------------------------------------------------------------------ step 1: prepare CML (Cockpit)
def link_file(mac: str, name: str) -> str:
    return (f"# LabDesk: keep this NIC's name when CML runs under VMware (Windows) instead of KVM\n"
            f"[Match]\nMACAddress={mac}\n\n[Link]\nName={name}\n")


def with_vmware_condition(unit_text: str) -> str:
    line = "ConditionVirtualization=vmware"
    if line in unit_text:
        return unit_text
    return unit_text.replace("[Unit]\n", "[Unit]\n# LabDesk: inside the CML VM the cloud runs only under VMware (Windows)\n"
                             + line + "\n", 1)


def cloud_conf_text() -> str:
    t = (CLOUD_SRC / "labdesk-cloud.conf").read_text(encoding="utf-8")
    t = re.sub(r"^BRIDGE=.*$", "BRIDGE=bridge0", t, flags=re.M)
    t = t.replace("@OWNER@", "sysadmin")
    return ("# Installed INSIDE the CML VM by LabDesk's make_windows_bundle.py (runs only under VMware).\n" + t)


def prep_tarball(guest_nics: dict[str, str], routes: str | None) -> bytes:
    files: dict[str, tuple[str, int]] = {}
    for mac, name in guest_nics.items():
        files[f"etc/systemd/network/10-labdesk-{name}.link"] = (link_file(mac, name), 0o644)
    for f in ("common.sh", "up", "down", "check"):
        mode = 0o644 if f == "common.sh" else 0o755
        files[f"usr/local/libexec/labdesk-cloud/{f}"] = ((CLOUD_SRC / "libexec" / f).read_text(encoding="utf-8"), mode)
    files["etc/labdesk-cloud.conf"] = (cloud_conf_text(), 0o644)
    for u in CLOUD_UNITS:
        files[f"etc/systemd/system/{u}"] = (with_vmware_condition((CLOUD_SRC / "systemd" / u).read_text(encoding="utf-8")),
                                            0o644)
    if routes:
        files["var/lib/labdesk-cloud/routes.conf"] = (routes, 0o644)
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, (text, mode) in sorted(files.items()):
            data = text.encode()
            ti = tarfile.TarInfo(name)
            ti.size, ti.mode, ti.mtime = len(data), mode, int(time.time())
            tar.addfile(ti, io.BytesIO(data))
    return buf.getvalue()


def prepare_cml(cp: Cockpit, kvm_macs: list[str]) -> dict[str, str]:
    out = cp.sh("ip -o link").output
    guest = {}
    for m in re.finditer(r"^\d+:\s+([^:@\s]+)[^\n]*link/ether\s+([0-9a-f:]{17})", out, re.M):
        name, mac = m.group(1), m.group(2).lower()
        if mac in kvm_macs and not name.startswith("bridge"):
            guest[mac] = name
    missing = [m for m in kvm_macs if m not in guest]
    if missing:
        raise SystemExit(f"CML guest has no NIC with MAC {', '.join(missing)} (is CML running?)")
    routes = None
    try:
        routes = Path("/var/lib/labdesk-cloud/routes.conf").read_text(encoding="utf-8")
    except OSError:
        pass
    payload = base64.b64encode(prep_tarball(guest, routes)).decode()
    script = ("set -e; base64 -d | tar -xzf - -C / --no-same-owner; "
              "install -d -m 0755 -o sysadmin -g sysadmin /var/lib/labdesk-cloud; "
              "[ -e /var/lib/labdesk-cloud/routes.conf ] && chown sysadmin:sysadmin /var/lib/labdesk-cloud/routes.conf; "
              "systemctl daemon-reload; systemctl enable labdesk-cloud.service >/dev/null 2>&1; "
              "update-initramfs -u >/dev/null 2>&1 || echo 'warning: update-initramfs failed'; "
              "echo virt=$(systemd-detect-virt) cloud=$(systemctl is-active labdesk-cloud.service) "
              "enabled=$(systemctl is-enabled labdesk-cloud.service); ls /etc/systemd/network")
    r = cp.sh(script, stdin=payload + "\n", sudo=True, timeout=600)
    print(r.output.strip())
    if not r.ok:
        raise SystemExit(f"preparing the CML VM failed: {r.problem or 'exit ' + str(r.status)}")
    return guest


# ------------------------------------------------------------------ steps 2-4
def wait_state(vm: str, want: str, timeout: float) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if virsh("domstate", vm, check=False).strip() == want:
            return True
        time.sleep(3)
    return False


def to_vmdk(src: Path | str, dst: Path, fmt: str | None = None) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(dst.name + ".part")
    cmd = ["qemu-img", "convert", "-p"]
    if fmt:
        cmd += ["-f", fmt]
    cmd += ["-O", "vmdk", "-o", "subformat=monolithicSparse,adapter_type=lsilogic", str(src), str(tmp)]
    subprocess.run(cmd, check=True)
    os.replace(tmp, dst)


def blank_vmdk(dst: Path, size: str) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    sh(["qemu-img", "create", "-f", "vmdk", "-o", "subformat=monolithicSparse,adapter_type=lsilogic", str(dst), size])


def copy_big(src: str, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(dst.name + ".part")
    subprocess.run(["cp", "--sparse=always", src, str(tmp)], check=True)
    os.replace(tmp, dst)


README = """LabDesk-VMs - the virtual machines of LabDesk for Windows (VMware Workstation)
==================================================================================

Built {date} on the LabDesk Linux PC by tools/make_windows_bundle.py.

1. Copy this whole folder to the Windows PC, next to LabDesk:
     C:\\Users\\<you>\\labdesk\\LabDesk-VMs      (git clone: inside the labdesk folder)
   or next to LabDesk.exe.  Use a local disk (not a USB stick) - the VMs run from here.
2. Start LabDesk (labdesk.bat or LabDesk.exe).  The first time it
   * takes the CML login from cml-credentials.env (no connection dialog),
   * installs VMware Workstation if it is missing and an installer is in installer\\,
   * creates the LabDesk network VMnet (192.168.122.0/24) - Windows asks once for administrator rights,
   * starts CML (about 2-4 minutes until the status bar says CML is ready).

Inside: {contents}

Keep this folder private: it holds licensed Cisco / Juniper / Fortinet images and your CML password.
"""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", default=str(Path.home() / vmware.BUNDLE_DIR_NAME))
    ap.add_argument("--vm", default=config.CML_VM_NAME, help="libvirt name of the CML VM (default: cml)")
    ap.add_argument("--yes", action="store_true", help="shut CML down without asking")
    ap.add_argument("--no-prepare", action="store_true", help="skip step 1 (CML already prepared)")
    ap.add_argument("--skip-cml", action="store_true", help="keep the CML disk already in the bundle")
    ap.add_argument("--skip-vjunos", action="store_true")
    ap.add_argument("--skip-fortios", action="store_true")
    args = ap.parse_args()
    out = Path(args.out).expanduser().resolve()
    out.mkdir(parents=True, exist_ok=True)
    work = out / ".work"
    work.mkdir(exist_ok=True)

    lay = cml_layout(args.vm)
    say(f"CML VM {args.vm}: disk {lay['disk']}, ISO {Path(lay['iso']).name}, NICs "
        + ", ".join(f"{m} ({n})" for m, n in lay["nics"]))
    # libvirt 'default' network = the LabDesk VMnet; every other network = a LAN segment of the same name
    nics = [(mac, "default" if net == "default" else net) for mac, net in lay["nics"]]

    if not args.skip_cml:
        if not args.no_prepare:
            say("step 1: preparing the CML VM for VMware (NIC names, Internet cloud) through Cockpit")
            prepare_cml(Cockpit(), [m for m, _n in nics])
        was_running = virsh("domstate", args.vm).strip() == "running"
        if was_running:
            if not args.yes and input(f"Shut down {args.vm} now to copy its disk (about 5 min)? [y/N] ").lower() != "y":
                return 1
            say(f"step 2: shutting down {args.vm} (clean shutdown, up to 10 min)…")
            virsh("shutdown", args.vm)
            if not wait_state(args.vm, "shut off", 600):
                raise SystemExit(f"{args.vm} did not shut down - stop it yourself and run again")
        try:
            say("copying the CML disk as VMware cml.vmdk…")
            to_vmdk(lay["disk"], out / "CML" / "cml.vmdk", "qcow2")
            iso = out / "CML" / "refplat.iso"
            if not iso.exists() or iso.stat().st_size != Path(lay["iso"]).stat().st_size:
                say("copying the refplat ISO (router images)…")
                copy_big(lay["iso"], iso)
        finally:
            if was_running:
                say(f"starting {args.vm} again")
                virsh("start", args.vm, check=False)
        vmware.cml_vmx(nics, "vmnet10").save(out / "CML" / "CML.vmx")

    templates = {}
    if not args.skip_vjunos and config.VJUNOS_IMAGE.exists():
        say(f"step 3: vJunos-router template from {config.VJUNOS_IMAGE.name}…")
        to_vmdk(config.VJUNOS_IMAGE, out / "templates" / "vjunos" / "vjunos.vmdk", "qcow2")
        vmware.template_vmx("vjunos", "vmnet10").save(out / "templates" / "vjunos" / "vjunos.vmx")
    if (out / "templates" / "vjunos" / "vjunos.vmx").exists():
        templates["vjunos"] = "templates/vjunos/vjunos.vmx"
    if not args.skip_fortios and config.FORTIOS_ZIP.exists():
        say(f"step 3: FortiOS template from {config.FORTIOS_ZIP.name}…")
        q = work / "fortios.qcow2"
        with zipfile.ZipFile(config.FORTIOS_ZIP) as z:
            member = next(m for m in z.namelist() if m.endswith("fortios.qcow2"))
            with z.open(member) as fin, open(q, "wb") as fout:
                shutil.copyfileobj(fin, fout, 1 << 20)
        d = out / "templates" / "fortios"
        to_vmdk(q, d / "fortios.vmdk", "qcow2")
        blank_vmdk(d / "fortios-log.vmdk", "2G")
        vmware.template_vmx("fortios", "vmnet10").save(d / "fortios.vmx")
        q.unlink()
    if (out / "templates" / "fortios" / "fortios.vmx").exists():
        templates["fortios"] = "templates/fortios/fortios.vmx"

    say("step 4: login, manifest, README")
    creds = config.load_env_file(config.CRED_FILE)
    creds.setdefault("CML_HOST", config.CML_HOST)
    cf = out / "cml-credentials.env"
    cf.write_text("# LabDesk: CML login for LabDesk on Windows (copied to %APPDATA%\\LabDesk on first start)\n"
                  + "".join(f"{k}={v}\n" for k, v in creds.items()), encoding="utf-8")
    os.chmod(cf, 0o600)
    inst = sorted(Path.home().joinpath("Downloads").glob("VMware-workstation*.exe"))
    if inst:
        (out / "installer").mkdir(exist_ok=True)
        if not (out / "installer" / inst[-1].name).exists():
            say(f"copying the VMware installer {inst[-1].name}")
            shutil.copy2(inst[-1], out / "installer" / inst[-1].name)
    manifest = {"format": vmware.BUNDLE_FORMAT, "built": time.strftime("%Y-%m-%d %H:%M"),
                "cml": {"vmx": "CML/CML.vmx", "ip": creds.get("CML_HOST") or config.CML_HOST,
                        "nics": [{"mac": m, "network": n} for m, n in nics], "cloud_inside": True},
                "templates": templates}
    (out / vmware.MANIFEST).write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    contents = ", ".join(["CML (+ router images)"] + [{"vjunos": "vJunos-router", "fortios": "FortiOS"}[k]
                                                      for k in templates] + (["VMware installer"] if inst else []))
    (out / "README.txt").write_text(README.format(date=manifest["built"], contents=contents).replace("\n", "\r\n"),
                                    encoding="utf-8")
    shutil.rmtree(work, ignore_errors=True)
    total = sum(f.stat().st_size for f in out.rglob("*") if f.is_file())
    say(f"done: {out}  ({total / 1e9:.1f} GB) - copy this folder to the Windows PC next to LabDesk")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
