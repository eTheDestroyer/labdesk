import json
import sys

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
    if sys.platform != "win32":                  # Windows: needs the optional Telnet Client (test_windows_port)
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


# ----------------------------------------------------------------------------- neon green console look
def test_neon_profile_is_the_plain_profile_plus_colours():
    plain, neon = launchers.profile_text(), launchers.neon_profile_text()
    assert "Name=LabDeskNeon\n" in neon and "Name=LabDesk\n" in plain and "ColorScheme" not in plain
    assert f"Command=/usr/bin/python3 -I -S {launchers.TABEXEC}" in neon          # same tab bootstrap
    assert "ColorScheme=LabDeskNeon" in neon and "CustomCursorColor=57,255,20" in neon
    assert neon.startswith(plain.replace("Name=LabDesk\n", "Name=LabDeskNeon\n"))


def test_neon_scheme_is_neon_green_on_black():
    import configparser
    cp = configparser.ConfigParser()
    cp.optionxform = str
    cp.read_string(launchers.neon_colorscheme_text())
    assert cp["Foreground"]["Color"] == "57,255,20" and cp["Color2"]["Color"] == "57,255,20"
    assert max(int(v) for v in cp["Background"]["Color"].split(",")) < 16           # near black
    assert cp["General"]["Description"] == "LabDesk Neon"
    for i in range(8):                                                            # every ANSI colour, 3 shades
        for suffix in ("", "Faint", "Intense"):
            assert len(cp[f"Color{i}{suffix}"]["Color"].split(",")) == 3
    r, g, b = (int(v) for v in cp["Color1"]["Color"].split(","))
    assert r > 200 and g < 100                                                    # errors stay red


def test_ensure_profile_writes_scheme_only_for_neon(tmp_path):
    p = launchers.ensure_profile(False, tmp_path)
    assert p.stem == "LabDesk" and sorted(f.name for f in tmp_path.iterdir()) == ["LabDesk.profile"]
    p = launchers.ensure_profile(True, tmp_path)
    assert p.stem == "LabDeskNeon"
    assert sorted(f.name for f in tmp_path.iterdir()) == ["LabDesk.profile", "LabDeskNeon.colorscheme",
                                                          "LabDeskNeon.profile"]
    before = (tmp_path / "LabDeskNeon.profile").stat().st_mtime_ns
    launchers.ensure_profile(True, tmp_path)                                      # unchanged -> not rewritten
    assert (tmp_path / "LabDeskNeon.profile").stat().st_mtime_ns == before
    scheme = tmp_path / "LabDeskNeon.colorscheme"                                 # the user's own colours are kept
    scheme.write_text("[Foreground]\nColor=255,0,255\n", encoding="utf-8")
    launchers.ensure_profile(True, tmp_path)
    assert "255,0,255" in scheme.read_text(encoding="utf-8")
    scheme.unlink()                                                               # deleted -> LabDesk's look is back
    launchers.ensure_profile(True, tmp_path)
    assert "57,255,20" in scheme.read_text(encoding="utf-8")


@pytest.mark.linux_only
def test_konsole_tabs_use_the_chosen_look(tmp_path, monkeypatch):
    """First console: a new window with the profile; every later console: a new TAB in that window."""
    calls, popen = [], []

    class _P:
        pid = 4242

    monkeypatch.setattr(launchers.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(launchers, "ensure_profile",
                        lambda neon=False, directory=None: tmp_path / ("LabDeskNeon.profile" if neon else "LabDesk.profile"))
    monkeypatch.setattr(launchers.subprocess, "Popen", lambda argv, **kw: popen.append(argv) or _P())
    state = {"running": False}
    monkeypatch.setattr(launchers.KonsoleLauncher, "_find_existing",
                        lambda self: "org.kde.konsole-4242" if state["running"] else None)
    monkeypatch.setattr(launchers.KonsoleLauncher, "_wait_service", lambda self, svc, timeout=8.0: True)

    def dbus(service, path, iface, method, *args, timeout=5.0):
        calls.append((method, args))
        return "7"
    monkeypatch.setattr(launchers, "dbus_call", dbus)
    k = launchers.KonsoleLauncher()
    assert k.neon                                                                  # neon is the default look
    k.open(launchers.ConsoleCommand("R1", ["ssh", "x"]))
    assert popen[0][:4] == ["konsole", "--separate", "--profile", "LabDeskNeon"] and len(popen) == 1
    state["running"] = True
    k.open(launchers.ConsoleCommand("R2", ["ssh", "x"]))
    assert len(popen) == 1                                                         # no second window
    assert ("newSession", ("LabDeskNeon",)) in calls
    k.neon = False                                                                 # View > Neon green console off
    k.open(launchers.ConsoleCommand("HUB", ["ssh", "x"]))
    assert ("newSession", ("LabDesk",)) in calls and len(popen) == 1


def test_a_neon_console_window_is_recognised_as_labdesks(monkeypatch):
    class _F:
        def __init__(self, data):
            self.data = data

        def read_bytes(self):
            return self.data
    cmd = b"/usr/bin/konsole\0--separate\0--profile\0LabDeskNeon\0--hide-menubar\0"
    monkeypatch.setattr(launchers, "Path", lambda p: _F(cmd))
    assert launchers._pid_is_labdesk_konsole(1)
    monkeypatch.setattr(launchers, "Path", lambda p: _F(b"/usr/bin/konsole\0"))
    assert not launchers._pid_is_labdesk_konsole(1)                                # the user's own Konsole
