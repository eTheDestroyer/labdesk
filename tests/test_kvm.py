import pytest

from labdesk_app import kvm
from labdesk_app.validate import ValidationError


def test_vjunos_install_cmd():
    c = kvm.build_install_cmd("JR1", "vjunos")
    s = " ".join(c)
    assert c[:3] == ["virt-install", "--connect", "qemu:///system"]
    assert c[c.index("--memory") + 1] == "5120" and c[c.index("--vcpus") + 1] == "4"
    assert c[c.index("--cpu") + 1] == "host-passthrough"
    assert c[c.index("--sysinfo") + 1] == ("type=smbios,system.manufacturer=Juniper,system.product=VM-VMX,"
                                           "system.family=lab")
    assert "--graphics none" in s and "pty,target_type=serial" in s
    assert "vol=images/JR1.qcow2,bus=virtio" in s
    assert c[c.index("--network") + 1] == "network=default,model=virtio"


def test_fortios_install_has_log_disk():
    c = kvm.build_install_cmd("FGT1", "fortios")
    disks = [c[i + 1] for i, x in enumerate(c) if x == "--disk"]
    assert disks == ["vol=images/FGT1.qcow2,bus=virtio,cache=writeback", "vol=images/FGT1-log.qcow2,bus=virtio"]
    assert c[c.index("--memory") + 1] == "2048" and c[c.index("--vcpus") + 1] == "1"
    assert "--sysinfo" not in c


@pytest.mark.parametrize("bad", ["", "a b", "x;rm -rf", "-flag", "a/b", "x" * 60])
def test_bad_vm_names_rejected(bad):
    with pytest.raises(ValidationError):
        kvm.build_install_cmd(bad, "vjunos")


def test_overlay_and_upload_cmds():
    o = kvm.build_overlay_cmd("JR1", kvm.PROFILES["vjunos"], 34359738368)
    assert o[:5] == ["virsh", "-c", "qemu:///system", "vol-create-as", "images"]
    assert "--backing-vol" in o and o[o.index("--backing-vol") + 1] == kvm.PROFILES["vjunos"].base_volume
    up = kvm.build_base_upload_cmds("base.qcow2", "/x/img.qcow2", 100)
    assert up[1] == ["virsh", "-c", "qemu:///system", "vol-upload", "--pool", "images", "base.qcow2", "/x/img.qcow2"]


def test_attach_nic_cmd():
    c = kvm.build_attach_nic_cmd("JR1", "labdesk-FGT1-JR1", live=True)
    assert c[-3:] == ["virtio", "--config", "--live"]
    assert "--source" in c and c[c.index("--source") + 1] == "labdesk-FGT1-JR1"


def test_isolated_network_xml():
    x = kvm.isolated_network_xml("labdesk-a-b")
    assert "<forward" not in x and "<ip" not in x and "<name>labdesk-a-b</name>" in x
    with pytest.raises(ValidationError):
        kvm.isolated_network_xml("default")


@pytest.mark.parametrize("text,state", [("running\n", "running"), ("shut off\n", "stopped"),
                                        ("paused", "paused"), ("in shutdown", "stopping")])
def test_parse_domstate(text, state):
    assert kvm.parse_domstate(text) == state


def test_parse_domifaddr():
    out = (" Name       MAC address          Protocol     Address\n"
           "-------------------------------------------------------\n"
           " vnet3      52:54:00:aa:bb:cc    ipv4         192.168.122.77/24\n")
    assert kvm.parse_domifaddr(out) == "192.168.122.77"
    assert kvm.parse_domifaddr("") is None


def test_memory_warning():
    low = "MemTotal: 24000000 kB\nMemAvailable:    4194304 kB\n"
    high = "MemAvailable:   12582912 kB\n"
    assert "4.0 GB" in kvm.memory_warning("vjunos", low)
    assert kvm.memory_warning("vjunos", high) is None
    assert kvm.memory_warning("fortios", low) is None


def test_guest_nic_names():
    assert kvm.guest_nic_name("vjunos", 0) == "fxp0"
    assert kvm.guest_nic_name("vjunos", 1) == "ge-0/0/0"
    assert kvm.guest_nic_name("fortios", 0) == "port1"
    assert kvm.guest_nic_name("fortios", 1) == "port2"


def test_console_and_lifecycle_cmds():
    assert kvm.build_console_cmd("JR1")[-3:] == ["console", "JR1", "--force"]
    assert kvm.build_undefine_cmd("JR1")[-3:] == ["JR1", "--remove-all-storage", "--nvram"]
    assert kvm.build_stop_cmd("JR1", force=True)[-2:] == ["destroy", "JR1"]
