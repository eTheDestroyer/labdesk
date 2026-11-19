"""LabDesk for Windows with VMware Workstation: the LabDesk-VMs bundle, .vmx files, the kvm.py dispatch,
NIC bookkeeping, DHCP leases, the cloud inside the CML VM (Cockpit) and in-process console streams.

Runs everywhere: vmrun / the registry / Cockpit are replaced by fakes; plat.IS_WINDOWS is monkeypatched."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from labdesk_app import cloud, config, kvm, launchers, plat, streams, vmware

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))


# ------------------------------------------------------------------ pure helpers
def test_pvn_id_is_stable_and_vmware_shaped():
    a = vmware.pvn_id("labdesk-pe-r1")
    assert a == vmware.pvn_id("labdesk-pe-r1") != vmware.pvn_id("labdesk-pe-r2")
    left, right = a.split("-")
    assert len(left.split()) == 8 and len(right.split()) == 8


def test_static_mac_in_vmware_range():
    for seed in ("a", "b", "PE-0", "x" * 50):
        mac = vmware.static_mac(seed)
        parts = [int(p, 16) for p in mac.split(":")]
        assert parts[:3] == [0x00, 0x50, 0x56] and parts[3] <= 0x3F


def test_vmx_roundtrip_and_case_insensitive_keys():
    v = vmware.Vmx('.encoding = "UTF-8"\ndisplayName = "x"\nMemSize = "1024"\n# comment\n')
    assert v.get("memsize") == "1024" and v.get("DISPLAYNAME") == "x"
    v.set("memsize", 2048)
    assert vmware.Vmx(v.text()).get("memSize") == "2048"
    assert v.text().startswith('.encoding = "UTF-8"\n') and v.text().count(".encoding") == 1
    with pytest.raises(ValueError):
        v.set("bad", 'a"b')


def test_cml_vmx_keeps_kvm_macs_and_maps_networks():
    nics = [("52:54:00:0d:b1:8b", "default"), ("52:54:00:07:fb:fc", "labdesk-pe-hub")]
    v = vmware.cml_vmx(nics, "vmnet10", 8192, 6)
    assert v.get("firmware") == "efi" and v.get("vhv.enable") == "TRUE" and v.get("numvcpus") == "6"
    assert v.get("sata0:1.fileName") == "refplat.iso" and v.get("sata0:1.deviceType") == "cdrom-image"
    assert v.get("ethernet0.connectionType") == "custom" and v.get("ethernet0.vnet") == "vmnet10"
    assert v.get("ethernet0.address") == "52:54:00:0d:b1:8b" and v.get("ethernet0.checkMACAddress") == "FALSE"
    assert v.get("ethernet1.connectionType") == "pvn" and v.get("ethernet1.pvnID") == vmware.pvn_id("labdesk-pe-hub")
    assert [n["network"] for n in v.nics()] == ["default", "labdesk-pe-hub"]


def test_add_and_remove_nic():
    v = vmware.template_vmx("fortios", "vmnet10")
    assert v.get("labdesk.nicdev") == "e1000" and v.get("sata0:1.fileName") == "fortios-log.vmdk"
    assert [n["index"] for n in v.nics()] == [0]
    i = v.add_nic("labdesk-a-b", "vmnet10", "00:50:56:01:02:03", dev="e1000")
    assert i == 1 and v.get("ethernet1.virtualDev") == "e1000"
    assert v.remove_nic("00:50:56:01:02:03") and not v.get("ethernet1.present")
    assert not v.remove_nic("00:50:56:99:99:99")


def test_parse_leases_newest_wins():
    text = ("lease 192.168.122.130 {\n starts 3 2026/11/18 10:00:00;\n hardware ethernet 00:50:56:2A:00:01;\n}\n"
            "lease 192.168.122.131 {\n starts 4 2026/11/19 10:00:00;\n hardware ethernet 00:50:56:2a:00:01;\n}\n"
            "lease 192.168.122.140 {\n starts 4 2026/11/19 09:00:00;\n}\n")
    assert vmware.parse_leases(text) == {"00:50:56:2a:00:01": "192.168.122.131"}


def test_parse_vmrun_list():
    out = "Total running VMs: 2\nC:\\VMs\\CML\\CML.vmx\nC:\\VMs\\devices\\PE\\PE.vmx\n"
    got = vmware.parse_vmrun_list(out)
    assert vmware.norm(r"C:\VMs\CML\CML.vmx") in got and len(got) == 2


def test_sizing():
    assert vmware.sizing(16 * 1024, 8) == (8192, 6)
    assert vmware.sizing(12 * 1024, 4) == (6144, 2)
    assert vmware.sizing(8 * 1024, 32) == (4096, 8)


def test_vnet_script_creates_the_lab_subnet():
    s = vmware.vnet_setup_script(r"C:\VMware\vnetlib64.exe", "vmnet10")
    assert "-- add adapter vmnet10" in s and "-- set vnet vmnet10 addr 192.168.122.0" in s
    assert "-- set adapter vmnet10 addr 192.168.122.1" in s and "-- add dhcp vmnet10" in s
    assert s.endswith("\r\n")


def test_vnet_script_reserves_the_cml_address():
    s = vmware.vnet_setup_script(None, None, "52:54:00:0d:b1:8b", "192.168.122.60", r"C:\PD\vmnetdhcp.conf")
    assert "vnetlib" not in s and "host labdesk-cml {" in s
    assert "hardware ethernet 52:54:00:0d:b1:8b;" in s and "fixed-address 192.168.122.60;" in s
    assert "net start VMnetDHCP" in s and 'findstr' in s
    conf = "subnet 192.168.122.0 netmask 255.255.255.0 {\n}\n" + "\n".join(vmware.reservation_block("m", "1.2.3.4"))
    assert vmware.has_reservation(conf) and not vmware.has_reservation("host other {\n}")


def test_explain_hyper_v_conflict():
    m = vmware.explain("Error: Virtualized Intel VT-x/EPT is not supported on this platform")
    assert "Memory integrity" in m and "hypervisorlaunchtype off" in m


# ------------------------------------------------------------------ a bundle on disk
@pytest.fixture
def bundle(tmp_path, monkeypatch):
    root = tmp_path / vmware.BUNDLE_DIR_NAME
    (root / "CML").mkdir(parents=True)
    (root / "templates" / "vjunos").mkdir(parents=True)
    vmware.cml_vmx([("52:54:00:0d:b1:8b", "default")], "vmnet10").save(root / "CML" / "CML.vmx")
    vmware.template_vmx("vjunos", "vmnet10").save(root / "templates" / "vjunos" / "vjunos.vmx")
    (root / "cml-credentials.env").write_text("CML_HOST=192.168.122.60\nCML_ADMIN_USER=admin\n"
                                              "CML_ADMIN_PASS=pw\nCML_SYSADMIN_USER=sysadmin\nCML_SYSADMIN_PASS=sp\n")
    (root / vmware.MANIFEST).write_text(json.dumps({
        "format": 1, "cml": {"vmx": "CML/CML.vmx", "ip": "192.168.122.60", "cloud_inside": True,
                             "nics": [{"mac": "52:54:00:0d:b1:8b", "network": "default"}]},
        "templates": {"vjunos": "templates/vjunos/vjunos.vmx", "fortios": "templates/fortios/fortios.vmx"}}))
    monkeypatch.setattr(vmware, "bundle_candidates", lambda: [tmp_path / "nothing", root])
    monkeypatch.setattr(vmware, "_BUNDLE", None)
    monkeypatch.setattr(plat, "IS_WINDOWS", True)
    monkeypatch.setattr(plat, "HAS_KVM", False)
    monkeypatch.setattr(plat, "HAS_CLOUD", False)
    return vmware.find_bundle()


def test_find_bundle_and_kinds(bundle):
    assert bundle is not None and bundle.cml_vmx.name == "CML.vmx"
    assert bundle.kinds == ["vjunos"]                    # fortios is in the manifest but not on disk
    assert vmware.active()


def test_find_bundle_ignores_folders_without_valid_manifest(tmp_path, monkeypatch):
    (tmp_path / "x").mkdir()
    (tmp_path / "x" / vmware.MANIFEST).write_text("{not json")
    monkeypatch.setattr(vmware, "bundle_candidates", lambda: [tmp_path / "x"])
    monkeypatch.setattr(vmware, "_BUNDLE", None)
    assert vmware.find_bundle(refresh=True) is None


def test_activate_turns_on_features_and_imports_login(bundle, tmp_path, monkeypatch):
    cred = tmp_path / "appdata" / "cml-credentials.env"
    monkeypatch.setattr(config, "CRED_FILE", cred)
    assert config.needs_setup(cred)
    assert vmware.activate()
    assert plat.HAS_KVM and plat.HAS_CLOUD
    vals = config.load_env_file(cred)
    assert vals["CML_ADMIN_PASS"] == "pw" and vals["CML_HOST"] == "192.168.122.60"
    assert vals["CML_SYSADMIN_PASS"] == "sp"
    assert not config.needs_setup(cred)                 # -> no connection dialog on the first start


def test_import_never_overwrites_a_saved_login(bundle, tmp_path, monkeypatch):
    cred = tmp_path / "c.env"
    cred.write_text("CML_HOST=10.1.1.1\nCML_ADMIN_USER=me\nCML_ADMIN_PASS=mine\n")
    monkeypatch.setattr(config, "CRED_FILE", cred)
    vmware.import_credentials()
    vals = config.load_env_file(cred)
    assert vals["CML_ADMIN_PASS"] == "mine" and vals["CML_HOST"] == "10.1.1.1" and vals["CML_SYSADMIN_PASS"] == "sp"


class FakeVmrun:
    def __init__(self, root: Path):
        self.root = root
        self.calls: list[tuple] = []
        self.running: set[str] = set()
        self.snaps: dict[str, list[str]] = {}

    def __call__(self, *args, timeout=120):
        self.calls.append(args)
        op = args[0]
        if op == "list":
            return f"Total running VMs: {len(self.running)}\n" + "".join(p + "\n" for p in self.running)
        if op == "listSnapshots":
            return "Total snapshots: 0\n" + "\n".join(self.snaps.get(args[1], []))
        if op == "snapshot":
            self.snaps.setdefault(args[1], []).append(args[2])
            return ""
        if op == "clone":
            src, dst = Path(args[1]), Path(args[2])
            dst.write_text(src.read_text())
            return ""
        if op == "start":
            self.running.add(args[1])
            return ""
        if op == "stop":
            self.running.discard(args[1])
            return ""
        if op == "deleteVM":
            Path(args[1]).unlink()
            return ""
        raise AssertionError(op)


@pytest.fixture
def fake_vmrun(bundle, monkeypatch):
    f = FakeVmrun(bundle.root)
    monkeypatch.setattr(vmware, "vmrun", f)
    monkeypatch.setattr(vmware, "find_lab_vnet", lambda: "vmnet12")
    monkeypatch.setattr(vmware, "ensure_vmware", lambda b, log=print: None)
    monkeypatch.setattr(vmware, "ensure_network", lambda b=None, log=print: "vmnet12")
    monkeypatch.setattr(vmware, "import_credentials", lambda b=None: False)
    monkeypatch.setattr(vmware, "_host_resources", lambda: (16384, 8))
    return f


def test_kvm_api_runs_vms_in_vmware(fake_vmrun, bundle):
    kvm.create_vm("PE", "vjunos", log=lambda m: None)
    vmx = bundle.devices_dir / "PE" / "PE.vmx"
    v = vmware.Vmx.load(vmx)
    assert v.get("displayName") == "PE" and v.get("serial0.fileName") == r"\\.\pipe\labdesk-PE"
    assert v.get("ethernet0.vnet") == "vmnet12" and len(v.nics()) == 1
    assert ("snapshot", str(bundle.template("vjunos")), vmware.SNAPSHOT) in fake_vmrun.calls
    assert kvm.domstate("PE") == "stopped"
    kvm.start_vm("PE")
    assert kvm.domstate("PE") == "running"
    kvm.stop_vm("PE")
    assert kvm.domstate("PE") == "stopped"
    kvm.delete_vm("PE")
    assert kvm.domstate("PE") == "missing"


def test_create_unknown_kind_is_a_kvm_error(fake_vmrun):
    with pytest.raises(kvm.KvmError, match="no fortios image"):
        kvm.create_vm("FGT", "fortios", log=lambda m: None)


def test_nics_on_a_stopped_and_a_running_vm(fake_vmrun, bundle):
    kvm.create_vm("PE", "vjunos", log=lambda m: None)
    before = kvm.nic_macs("PE")
    kvm.ensure_isolated_network("labdesk-PE-R1")
    kvm.attach_nic("PE", "labdesk-PE-R1")                 # stopped: written into the vmx now
    macs = kvm.nic_macs("PE")
    assert len(macs) == len(before) + 1
    v = vmware.Vmx.load(bundle.devices_dir / "PE" / "PE.vmx")
    assert v.get("ethernet1.pvnID") == vmware.pvn_id("labdesk-PE-R1")
    assert kvm.network_users("labdesk-PE-R1") == {"PE"}
    kvm.start_vm("PE")
    kvm.attach_nic("PE", "default")                       # running: pending until the next start
    assert len(kvm.nic_macs("PE")) == len(macs) + 1
    assert len(vmware.Vmx.load(bundle.devices_dir / "PE" / "PE.vmx").nics()) == 2
    kvm.detach_nic("PE", macs[-1])                        # running: removal is pending too
    kvm.stop_vm("PE")
    kvm.start_vm("PE")                                    # pending edits applied before power-on
    nets = [n["network"] for n in vmware.Vmx.load(bundle.devices_dir / "PE" / "PE.vmx").nics()]
    assert nets == ["default", "default"]


def test_mgmt_ip_from_vmware_dhcp_leases(fake_vmrun, bundle, tmp_path, monkeypatch):
    kvm.create_vm("PE", "vjunos", log=lambda m: None)
    mac = kvm.nic_macs("PE")[0]
    leases = tmp_path / "vmnetdhcp.leases"
    leases.write_text(f"lease 192.168.122.133 {{\n starts 4 2026/11/19 10:00:00;\n hardware ethernet {mac.upper()};\n}}\n")
    monkeypatch.setattr(vmware, "leases_path", lambda: leases)
    assert kvm.mgmt_ip("PE") == "192.168.122.133"


def test_start_cml_vm_uses_vmware(fake_vmrun, bundle, monkeypatch):
    monkeypatch.setattr(vmware, "_host_resources", lambda: (16384, 8))
    kvm.start_cml_vm(log=lambda m: None)
    assert ("start", str(bundle.cml_vmx), "nogui") in fake_vmrun.calls
    v = vmware.Vmx.load(bundle.cml_vmx)
    assert v.get("ethernet0.vnet") == "vmnet12" and v.get("memsize") == "8192" and v.get("numvcpus") == "6"


def test_linux_kvm_path_untouched(monkeypatch):
    monkeypatch.setattr(plat, "IS_WINDOWS", False)
    assert kvm._vmware() is None


# ------------------------------------------------------------------ consoles
def test_windows_console_commands_carry_transports(bundle):
    c = launchers.kvm_console("PE")
    assert c.meta["transport"] == {"type": "pipe", "path": r"\\.\pipe\labdesk-PE"}
    u = "0f1e2d3c-4b5a-4968-8776-655443322110"
    c = launchers.cml_console("R1", u, "admin")
    assert c.meta["transport"] == {"type": "cml", "uuid": u, "user": "admin"} and c.argv[-2:] == ["connect", u]


class FakeTr:
    def __init__(self, uuid, user, connect_timeout=5.0):
        self.chunks = [b"R1>", b""]
        self.sent = []
        self.chan = self
        self.size = None

    def start(self):
        pass

    def read(self, timeout):
        from labdesk_app.probe.transport import TransportError
        if not self.chunks:
            raise TransportError("console connection closed")
        return self.chunks.pop(0)

    def write(self, data):
        self.sent.append(data)

    def resize_pty(self, width, height):
        self.size = (width, height)

    def close(self):
        pass


def test_cml_stream_registers_open_console():
    u = "0f1e2d3c-4b5a-4968-8776-655443322110"
    s = streams.CmlConsoleStream(u, "admin", 120, 40, tr_factory=FakeTr)
    s.open()
    assert u in streams.open_uuids()
    assert s.read(0.1) == b"R1>"
    s.write(b"show ip int br\r")
    s.resize(150, 50)
    assert s.tr.sent == [b"show ip int br\r"] and s.tr.size == (150, 50)
    s.read(0.1)
    with pytest.raises(streams.StreamClosed):
        s.read(0.1)
    s.close()
    assert u not in streams.open_uuids()


def test_probes_pause_for_in_process_consoles(monkeypatch):
    from labdesk_app import telemetry
    u = "0f1e2d3c-4b5a-4968-8776-655443322111"
    streams.OPEN_UUIDS.add(u)
    try:
        assert u in telemetry.open_console_uuids()
    finally:
        streams.OPEN_UUIDS.discard(u)


def test_pipe_stream_rejects_non_pipes():
    with pytest.raises(ValueError):
        streams.PipeStream("C:\\file.txt")


def test_stream_session_feeds_the_screen(qapp_vm):
    from PySide6.QtCore import QCoreApplication

    from labdesk_app.ui.console.stream_session import ENDED, StreamSession

    class FakeStream:
        def __init__(self):
            self.out = [b"Router>", b" ok"]
            self.sent = []

        def open(self):
            pass

        def read(self, timeout):
            if self.out:
                return self.out.pop(0)
            raise streams.StreamClosed("bye")

        def write(self, d):
            self.sent.append(d)

        def resize(self, c, r):
            pass

        def close(self):
            pass

    fake = FakeStream()
    import labdesk_app.ui.console.stream_session as ss
    orig = ss.make_stream
    ss.make_stream = lambda t, c, r: fake
    try:
        s = StreamSession({"type": "cml", "uuid": "x"}, 100, 30)
        s.start(["ssh"], {})
        import time
        end = time.time() + 5
        while s.state != ENDED and time.time() < end:
            QCoreApplication.processEvents()
            time.sleep(0.01)
        assert s.state == ENDED and s.status == 0
        assert "Router> ok" in "".join(s.screen.display)
    finally:
        ss.make_stream = orig


@pytest.fixture(scope="module")
def qapp_vm():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


# ------------------------------------------------------------------ the cloud inside the CML VM
class FakeCockpit:
    files: dict[str, str] = {}
    script_out = ""

    def __init__(self, *a, **kw):
        pass

    def sh(self, script, stdin=None, sudo=False, timeout=None):
        from labdesk_app.cockpit import Result
        return Result(0, FakeCockpit.script_out)

    def read_file(self, path):
        return FakeCockpit.files.get(path)

    def write_file(self, path, text):
        FakeCockpit.files[path] = text


def test_cloud_in_cml_vm_reads_status_through_cockpit(bundle, monkeypatch):
    import time
    from labdesk_app import cockpit
    monkeypatch.setattr(cockpit, "Cockpit", FakeCockpit)
    FakeCockpit.script_out = (f"state=up\nts={time.time():.0f}\nanswers=8.8.8.8 1.1.1.1\n"
                              "selfping=8.8.8.8:ok 1.1.1.1:ok\n\n@@ns=1 inst=1\n")
    assert cloud.in_cml_vm() and cloud.load_conf().bridge == "bridge0"
    h = cloud.check_health()
    assert h.state == "up" and h.ok
    FakeCockpit.script_out = "\n@@ns=0 inst=1\n"
    assert cloud.check_health().state in ("down", "stopped")


def test_cloud_routes_file_in_cml_vm(bundle, monkeypatch):
    from labdesk_app import cockpit
    monkeypatch.setattr(cockpit, "Cockpit", FakeCockpit)
    cloud.write_routes_file("EDGE_GW=100.64.0.2\n")
    assert cloud.read_routes_file() == "EDGE_GW=100.64.0.2\n" == FakeCockpit.files[cloud.GUEST_ROUTES]


# ------------------------------------------------------------------ the bundle builder (Linux side)
def test_builder_pins_nic_names_and_guards_the_cloud():
    import io
    import tarfile

    import make_windows_bundle as mb
    data = mb.prep_tarball({"52:54:00:0d:b1:8b": "enp1s0"}, "EDGE_GW=100.64.0.2\n")
    tar = tarfile.open(fileobj=io.BytesIO(data))
    names = tar.getnames()
    link = tar.extractfile("etc/systemd/network/10-labdesk-enp1s0.link").read().decode()
    assert "MACAddress=52:54:00:0d:b1:8b" in link and "Name=enp1s0" in link
    for u in mb.CLOUD_UNITS:                     # never under KVM: the Linux host keeps its own cloud
        assert "ConditionVirtualization=vmware" in tar.extractfile(f"etc/systemd/system/{u}").read().decode()
    conf = tar.extractfile("etc/labdesk-cloud.conf").read().decode()
    assert "BRIDGE=bridge0" in conf and "STATE_OWNER=sysadmin" in conf
    assert "var/lib/labdesk-cloud/routes.conf" in names
    assert tar.getmember("usr/local/libexec/labdesk-cloud/up").mode == 0o755


def test_cml_address_follows_its_dhcp_lease(bundle, tmp_path, monkeypatch):
    cred = tmp_path / "c.env"
    cred.write_text("CML_HOST=192.168.122.60\nCML_ADMIN_PASS=x\n")
    monkeypatch.setattr(config, "CRED_FILE", cred)
    monkeypatch.setattr(config, "CML_HOST", "192.168.122.60")
    monkeypatch.delenv("CML_HOST", raising=False)
    leases = tmp_path / "l"
    monkeypatch.setattr(vmware, "leases_path", lambda: leases)
    leases.write_text("lease 192.168.122.60 {\n starts 4 2026/11/19 10:00:00;\n hardware ethernet 52:54:00:0d:b1:8b;\n}\n")
    assert vmware.refresh_cml_address() is None
    leases.write_text("lease 192.168.122.200 {\n starts 4 2026/11/19 11:00:00;\n hardware ethernet 52:54:00:0d:b1:8b;\n}\n")
    assert vmware.refresh_cml_address() == "192.168.122.200"
    assert config.load_env_file(cred)["CML_HOST"] == "192.168.122.200" == config.CML_HOST
