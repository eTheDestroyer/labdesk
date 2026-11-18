import json

import pytest

from labdesk_app import config, launchers
from labdesk_app.validate import ValidationError

UUID = "0f1e2d3c-4b5a-4968-8776-655443322110"


@pytest.mark.linux_only
def test_cml_console_argv_is_direct_and_raw():
    cmd = launchers.cml_console("R1", UUID, "admin")
    a = cmd.argv
    assert a[0] == "ssh" and "-t" in a
    assert a[a.index("-e") + 1] == "none"               # no ~ escape processing
    assert a[-3:] == [f"admin@{config.CML_HOST}", "connect", UUID]
    assert cmd.title == "R1"


def test_cml_console_uses_key_when_present(tmp_path, monkeypatch):
    key = tmp_path / "k"
    key.write_text("x")
    a = launchers.cml_ssh_base("admin", key=key)
    assert a[a.index("-i") + 1] == str(key)
    assert "IdentitiesOnly=yes" in a


@pytest.mark.linux_only
def test_no_password_anywhere_in_console_command(tmp_path, monkeypatch):
    cmd = launchers.cml_console("R1", UUID, "admin")
    blob = json.dumps({"argv": cmd.argv, "env": cmd.env})
    creds = config.load_env_file() if config.CRED_FILE.exists() else {}
    for k, v in creds.items():
        if k.endswith("PASS") and v:
            assert v not in blob
    assert cmd.env["SSH_ASKPASS"].endswith("askpass.py")
    assert cmd.env["LABDESK_ASKPASS_KEY"] == "CML_ADMIN_PASS"


@pytest.mark.parametrize("bad", ["not-a-uuid", UUID + "; reboot", "", "$(id)"])
def test_console_rejects_bad_uuid(bad):
    with pytest.raises(ValidationError):
        launchers.cml_console("R1", bad, "admin")


def test_console_rejects_bad_label_and_user():
    with pytest.raises(ValidationError):
        launchers.cml_console("R1 && x", UUID, "admin")
    with pytest.raises(ValidationError):
        launchers.cml_console("R1", UUID, "-oProxyCommand=x")


def test_kvm_console():
    c = launchers.kvm_console("vjunos1")
    assert c.argv == ["virsh", "-c", "qemu:///system", "console", "vjunos1", "--force"]


def test_ssh_and_telnet_to_device():
    s = launchers.ssh_to_device("R1", "192.168.122.50", "admin")
    assert s.argv[-3:] == ["-l", "admin", "192.168.122.50"]
    assert "HostKeyAlgorithms=+ssh-rsa" in s.argv
    assert launchers.telnet_to_device("R1", "10.0.0.1").argv == ["telnet", "10.0.0.1"]
    with pytest.raises(ValidationError):
        launchers.ssh_to_device("R1", "-oProxyCommand=evil", "admin")


def test_http_url():
    assert launchers.http_url("10.1.1.1", True) == "https://10.1.1.1/"
    assert launchers.http_url("fe80::1", False) == "http://[fe80::1]/"


def test_dbus_call_builders():
    q = launchers.build_dbus_call("org.kde.konsole-1", "/Sessions/2", "org.kde.konsole.Session",
                                  "runCommand", ['{"a":"it\'s"}'], tool="/usr/bin/qdbus6")
    assert q == ["/usr/bin/qdbus6", "org.kde.konsole-1", "/Sessions/2", "org.kde.konsole.Session.runCommand",
                 '{"a":"it\'s"}']
    g = launchers.build_dbus_call("svc", "/Sessions/2", "org.kde.konsole.Session", "setTabTitleFormat",
                                  [0, "R'1"], tool="gdbus")
    assert g[:3] == ["gdbus", "call", "--session"]
    assert g[-2:] == ["0", "'R\\'1'"]


def test_console_command_json_roundtrip():
    c = launchers.cml_console("R1", UUID, "admin")
    d = json.loads(c.as_json())
    assert d["argv"] == c.argv and d["title"] == "R1"
    assert "\n" not in c.as_json()        # must be a single line for the tab bootstrap


def test_profile_runs_tabexec():
    t = launchers.profile_text()
    assert "Name=LabDesk" in t
    assert f"Command=/usr/bin/python3 -I -S {launchers.TABEXEC}" in t
