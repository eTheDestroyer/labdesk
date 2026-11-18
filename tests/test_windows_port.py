"""LabDesk for Windows: platform layer, Windows console launcher, paramiko probe transport (fake channel),
credentials file writer / first-run detection, CML connection dialog, Linux-only features switched off.

Everything runs on Linux too (plat.* is monkeypatched); nothing touches the network or the user's files."""
from __future__ import annotations

import os
import socket
import stat
import sys

import pytest
from fake_console import FakeConsole
from showcase_replay import fixture_text

from labdesk_app import cloud, config, launchers, plat
from labdesk_app.probe import creds as creds_mod
from labdesk_app.probe import transport as tr
from labdesk_app.probe.session import BurstStatus, IosDialect, run_burst
from labdesk_app.telemetry import console_uuids_in

UUID = "0f1e2d3c-4b5a-4968-8776-655443322110"
WIN_SSH = r"C:\Windows\System32\OpenSSH\ssh.exe"


@pytest.fixture(scope="module")
def qapp():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


@pytest.fixture
def windows(monkeypatch):
    """Pretend to be LabDesk for Windows (only the plat switches; the OS stays what it is)."""
    monkeypatch.setattr(plat, "IS_WINDOWS", True)
    monkeypatch.setattr(plat, "HAS_KVM", False)
    monkeypatch.setattr(plat, "HAS_CLOUD", False)
    monkeypatch.setattr(plat, "ssh_exe", lambda: WIN_SSH)
    return monkeypatch


# ------------------------------------------------------------------ platform layer / paths
def test_linux_defaults_unchanged():
    if plat.IS_WINDOWS:
        pytest.skip("Linux paths")
    assert plat.ssh_exe() == "ssh"
    assert plat.default_cred_file() == __import__("pathlib").Path(os.path.expanduser("~/cml-labs/cml-credentials.env"))
    assert plat.HAS_KVM and plat.HAS_CLOUD
    assert plat.no_window_kwargs() == {}


def test_windows_folders(monkeypatch, tmp_path):
    monkeypatch.setattr(plat, "IS_WINDOWS", True)
    monkeypatch.setenv("APPDATA", str(tmp_path / "Roaming"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    assert plat.default_cred_file() == tmp_path / "Roaming" / "LabDesk" / "cml-credentials.env"
    assert plat.default_config_dir() == tmp_path / "Roaming" / "LabDesk"
    assert plat.default_cache_dir() == tmp_path / "Local" / "LabDesk" / "cache"
    assert plat.no_window_kwargs() == {"creationflags": plat.CREATE_NO_WINDOW}


def test_resource_dir_has_runtime_data():
    d = plat.resource_dir()
    assert (d / "labdesk.svg").exists() and (d / "templates").is_dir()
    assert (d / "labdesk_app" / "ui" / "icons").is_dir()
    assert config.TEMPLATES_DIR == d / "templates" and config.PKG_DIR == d / "labdesk_app"


# ------------------------------------------------------------------ credentials file
def test_update_env_file_keeps_other_keys(tmp_path):
    p = tmp_path / "cml.env"
    p.write_text("# mine\nCML_HOST=1.2.3.4\nJUNOS_ADMIN_PASS=j\nCML_ADMIN_PASS=old\n")
    config.update_env_file({"CML_HOST": "10.0.0.5", "CML_ADMIN_USER": "admin", "CML_ADMIN_PASS": " sp ace "}, p)
    vals = config.load_env_file(p)
    assert vals == {"CML_HOST": "10.0.0.5", "JUNOS_ADMIN_PASS": "j", "CML_ADMIN_PASS": " sp ace ",
                    "CML_ADMIN_USER": "admin"}
    assert p.read_text().startswith("# mine\n")
    if not plat.IS_WINDOWS:
        assert stat.S_IMODE(p.stat().st_mode) == 0o600
    assert not [f for f in os.listdir(tmp_path) if f.startswith(".cml-credentials.")]


def test_update_env_file_creates_folder(tmp_path):
    p = tmp_path / "LabDesk" / "cml-credentials.env"
    config.update_env_file({"CML_HOST": "h", "CML_ADMIN_USER": "u", "CML_ADMIN_PASS": "p#w=1"}, p)
    assert config.load_env_file(p)["CML_ADMIN_PASS"] == "p#w=1"


def test_needs_setup(tmp_path):
    p = tmp_path / "c.env"
    assert config.needs_setup(p)                       # missing
    p.write_text("CML_HOST=1.2.3.4\nCML_ADMIN_USER=admin\n")
    assert config.needs_setup(p)                       # no password
    p.write_text("CML_ADMIN_PASS=x\n")
    assert not config.needs_setup(p)


def test_device_creds_mode_check_skipped_on_windows(tmp_path, monkeypatch):
    p = tmp_path / "device-creds"
    p.write_text("R1_USERNAME=admin\nR1_PASSWORD=cisco\n")
    os.chmod(p, 0o644)                                 # what every file looks like on Windows
    if not plat.IS_WINDOWS:
        with pytest.raises(creds_mod.CredsError):
            creds_mod.load("R1", p)
    monkeypatch.setattr(plat, "IS_WINDOWS", True)
    assert creds_mod.load("R1", p).password == "cisco"


def test_reload_cml_host(tmp_path, monkeypatch):
    p = tmp_path / "c.env"
    p.write_text("CML_HOST=10.9.8.7\nCML_ADMIN_PASS=x\n")
    monkeypatch.setattr(config, "CRED_FILE", p)
    monkeypatch.delenv("CML_HOST", raising=False)
    old = config.CML_HOST
    try:
        assert config.reload_cml_host() == "10.9.8.7" == config.CML_HOST
        assert launchers.cml_ssh_base("admin")[-1] == "admin@10.9.8.7"     # read at call time
        from labdesk_app.cml_api import CMLClient
        assert CMLClient(cred_file=p).host == "10.9.8.7"
    finally:
        config.CML_HOST = old


# ------------------------------------------------------------------ consoles on Windows
def test_windows_cml_console_command(windows):
    cmd = launchers.cml_console("R1", UUID, "admin")
    assert cmd.argv[0] == WIN_SSH and cmd.argv[-2:] == ["connect", UUID]
    assert cmd.argv[cmd.argv.index("-e") + 1] == "none" and "-t" in cmd.argv
    assert cmd.env == {}                                # no askpass: the user types the password
    assert not any("PASS" in a for a in cmd.argv)


def test_windows_known_hosts_option(windows, monkeypatch):
    from pathlib import PureWindowsPath
    assert launchers._known_hosts_opt(PureWindowsPath(r"C:\Users\ed\.ssh\labdesk_known_hosts")) == \
        ["-o", "UserKnownHostsFile=C:/Users/ed/.ssh/labdesk_known_hosts"]
    assert launchers._known_hosts_opt(PureWindowsPath(r"C:\Users\Ed Smith\.ssh\kh")) == []


def test_wt_argv_and_conhost_line(windows):
    cmd = launchers.ConsoleCommand("R1 (ssh)", [WIN_SSH, "-t", "admin@10.0.0.1", "connect", UUID])
    a = launchers.wt_argv("wt.exe", cmd)
    assert a[:7] == ["wt.exe", "-w", "LabDesk", "new-tab", "--title", "R1 (ssh)", "--suppressApplicationTitle"]
    assert a[7:] == cmd.argv
    semi = launchers.ConsoleCommand("x;y", ["ssh", "a;b"])
    assert launchers.wt_argv("wt", semi)[-1] == "a\\;b" and launchers.wt_argv("wt", semi)[5] == "x_y"
    line = launchers.conhost_cmdline(cmd)
    assert line.startswith('cmd.exe /d /s /c "title R1 (ssh) & ') and line.endswith(' || pause"')
    assert UUID in line


def test_windows_launcher_prefers_windows_terminal(windows):
    calls = []
    lw = launchers.WindowsConsoleLauncher(popen=lambda *a, **k: calls.append((a, k)), wt_finder=lambda: "wt.exe")
    msg = lw.open(launchers.cml_console("R2", UUID, "admin"))
    assert "Windows Terminal" in msg and calls[0][0][0][:4] == ["wt.exe", "-w", "LabDesk", "new-tab"]
    assert lw.sessions() == []


def test_windows_launcher_falls_back_to_new_console(windows):
    calls = []

    def popen(argv, **kw):
        calls.append((argv, kw))
        if isinstance(argv, list):
            raise OSError("wt broken")
    lw = launchers.WindowsConsoleLauncher(popen=popen, wt_finder=lambda: "wt.exe")
    assert "new console window" in lw.open(launchers.cml_console("R3", UUID, "admin"))
    assert calls[-1][0].startswith("cmd.exe /d /s /c") and calls[-1][1]["creationflags"] == plat.CREATE_NEW_CONSOLE
    calls.clear()
    lw = launchers.WindowsConsoleLauncher(popen=popen, wt_finder=lambda: None)
    lw.open(launchers.cml_console("R3", UUID, "admin"))
    assert len(calls) == 1 and calls[0][0].startswith("cmd.exe")


def test_default_launcher_per_os(monkeypatch):
    monkeypatch.setattr(plat, "IS_WINDOWS", True)
    assert isinstance(launchers.default_launcher(), launchers.WindowsConsoleLauncher)
    monkeypatch.setattr(plat, "IS_WINDOWS", False)
    assert isinstance(launchers.default_launcher(), launchers.KonsoleLauncher)


def test_console_scan_matches_windows_ssh():
    assert console_uuids_in([b"C:\\Windows\\System32\\OpenSSH\\ssh.exe", b"-t", b"admin@h", b"connect",
                             UUID.encode()]) == {UUID}
    assert console_uuids_in([b"/usr/bin/ssh", b"connect", UUID.encode()]) == {UUID}
    assert console_uuids_in([b"python", b"connect", UUID.encode()]) == set()
    assert console_uuids_in([b"ssh", b"connect", b"nope"]) == set()


# ------------------------------------------------------------------ paramiko transport (fake channel)
class FakeChannel:
    """paramiko.Channel stand-in around the scripted IOS console."""

    def __init__(self, console: FakeConsole):
        self.con = console
        self.timeout = None
        self.closed = False
        self.exited = False

    def settimeout(self, t):
        self.timeout = t

    def recv(self, n):
        if self.closed:
            raise OSError("closed")
        try:
            data = self.con.read(self.timeout or 0.0)
        except tr.TransportError:
            return b""                               # EOF
        if not data:
            raise socket.timeout()
        return data[:n]

    def sendall(self, data):
        self.con.write(data)

    def exit_status_ready(self):
        return self.exited

    def close(self):
        self.closed = True
        self.con.close()


def _outputs():
    cmds = ["show ip interface brief", "show ip ospf neighbor", "show standby brief"]
    return cmds, {c: fixture_text("R1", c) for c in cmds}


def test_paramiko_transport_runs_a_full_burst():
    cmds, outs = _outputs()
    con = FakeConsole("R1", "priv", outs)
    t = tr.TermserverParamiko(UUID, "admin", channel_factory=lambda: FakeChannel(con))
    res = run_burst(t, IosDialect(), cmds, timeout=5)
    assert res.status == BurstStatus.OK and res.hostname == "R1" and res.mode_found == "priv"
    assert res.outputs["show ip ospf neighbor"].text.startswith("Neighbor ID")
    assert "terminal length 24" in con.sent_text and con.closed
    assert t.chan is None and t.pid is None


def test_paramiko_transport_same_result_as_pty_path():
    cmds, outs = _outputs()
    direct = run_burst(FakeConsole("R1", "user", outs), IosDialect(), cmds, timeout=5)
    con = FakeConsole("R1", "user", outs)
    via = run_burst(tr.TermserverParamiko(UUID, "admin", channel_factory=lambda: FakeChannel(con)),
                    IosDialect(), cmds, timeout=5)
    assert via.status == direct.status and via.mode_found == direct.mode_found
    assert {k: v.text for k, v in via.outputs.items()} == {k: v.text for k, v in direct.outputs.items()}


def test_paramiko_transport_io_contract():
    con = FakeConsole("R1", "priv", {})
    ch = FakeChannel(con)
    t = tr.TermserverParamiko(UUID, "admin", channel_factory=lambda: ch)
    with pytest.raises(tr.TransportError):
        t.read(0.1)                                    # not started
    t.start()
    assert t.alive()
    assert b"Escape character" in t.read(0.5)
    assert t.read(0.01) == b""                         # nothing pending -> timeout, not EOF
    t.write(b"\r")
    assert b"R1#" in t.read(0.5)
    ch.exited = True
    assert not t.alive()
    con.closed = True
    with pytest.raises(tr.TransportError):
        t.read(0.1)                                    # EOF
    t.close()
    t.close()                                          # idempotent
    assert ch.closed


def test_paramiko_transport_start_errors_become_transport_errors():
    def boom():
        raise OSError("connection refused")
    t = tr.TermserverParamiko(UUID, "admin", channel_factory=boom)
    with pytest.raises(tr.TransportError, match="connection refused"):
        t.start()


def test_paramiko_transport_validates_input():
    from labdesk_app.validate import ValidationError
    with pytest.raises(ValidationError):
        tr.TermserverParamiko("not-a-uuid", "admin")


def test_paramiko_needs_key_or_password(tmp_path, monkeypatch):
    pytest.importorskip("paramiko")
    monkeypatch.setattr(config, "SSH_KEY", tmp_path / "nokey")
    monkeypatch.setattr(config, "CRED_FILE", tmp_path / "none.env")
    t = tr.TermserverParamiko(UUID, "admin", host="192.0.2.1")
    with pytest.raises(tr.TransportError, match="no SSH key and no CML password"):
        t.start()


def test_default_transport_per_os(monkeypatch):
    monkeypatch.setattr(plat, "IS_WINDOWS", True)
    assert isinstance(tr.default_transport(UUID, "admin"), tr.TermserverParamiko)
    if sys.platform != "win32":
        monkeypatch.setattr(plat, "IS_WINDOWS", False)
        assert isinstance(tr.default_transport(UUID, "admin"), tr.TermserverPty)


# ------------------------------------------------------------------ Linux-only features
def test_cloud_is_linux_only_on_windows(windows):
    h = cloud.check_health()
    assert h.state == "unsupported" and not h.ok and "Linux only" in h.headline


def test_palette_disables_kvm_and_cloud_on_windows(qapp, windows):
    from PySide6.QtCore import Qt

    from labdesk_app.ui.main_window import Palette, unavailable_reason
    pal = Palette()
    kvm_items = [pal.item(i) for i in range(pal.count())
                 if pal.item(i).data(Qt.UserRole) in ("vjunos", "fortios", "ext_cloud")]
    assert kvm_items and all(not (it.flags() & Qt.ItemIsEnabled) for it in kvm_items)
    assert all("Linux only" in it.toolTip() for it in kvm_items)
    from labdesk_app.catalog import BY_KEY
    assert unavailable_reason(BY_KEY["iosv"]) == ""


def test_palette_unchanged_on_linux(qapp, monkeypatch):
    monkeypatch.setattr(plat, "HAS_KVM", True)
    monkeypatch.setattr(plat, "HAS_CLOUD", True)
    from PySide6.QtCore import Qt

    from labdesk_app.ui.main_window import Palette
    pal = Palette()
    items = [pal.item(i) for i in range(pal.count()) if pal.item(i).data(Qt.UserRole)]
    assert items and all(it.flags() & Qt.ItemIsEnabled for it in items)


# ------------------------------------------------------------------ connection dialog
class _FakeClient:
    def __init__(self, host, ok=True):
        self.host, self.ok = host, ok

    def check_login(self, user, pw):
        from labdesk_app.cml_api import CMLError
        if not self.ok or pw != "secret":
            raise CMLError("wrong username or password", 401)


def _settings(tmp_path):
    from PySide6.QtCore import QSettings
    return QSettings(str(tmp_path / "t.ini"), QSettings.IniFormat)


def test_connection_dialog_first_run_saves(qapp, tmp_path):
    from labdesk_app.ui.connection_dialog import CmlConnectionDialog
    p = tmp_path / "LabDesk" / "cml-credentials.env"
    d = CmlConnectionDialog(None, cred_file=p, first_run=True, client_factory=_FakeClient,
                            settings=_settings(tmp_path))
    assert "Welcome" in d.header_title.text()
    assert not d.ok.isEnabled()                         # password still missing
    d.host.setText("192.168.80.130")
    d.user.setText("admin")
    d.password.setText("secret")
    assert d.ok.isEnabled() and d.problem() == ""
    d.host.setText("https://x/")
    assert not d.ok.isEnabled() and "IP address" in d.problem()
    d.host.setText("192.168.80.130")
    d._save()
    assert d.saved
    assert config.load_env_file(p) == {"CML_HOST": "192.168.80.130", "CML_ADMIN_USER": "admin",
                                       "CML_ADMIN_PASS": "secret"}


def test_connection_dialog_keeps_saved_password(qapp, tmp_path):
    from labdesk_app.ui.connection_dialog import CmlConnectionDialog
    p = tmp_path / "c.env"
    p.write_text("CML_HOST=10.1.1.1\nCML_ADMIN_USER=admin\nCML_ADMIN_PASS=secret\nJUNOS_ADMIN_PASS=j\n")
    d = CmlConnectionDialog(None, cred_file=p, client_factory=_FakeClient, settings=_settings(tmp_path))
    assert d.host.text() == "10.1.1.1" and d.password.text() == ""        # never shown
    assert d.ok.isEnabled()
    d.host.setText("10.1.1.2")
    d._save()
    assert config.load_env_file(p) == {"CML_HOST": "10.1.1.2", "CML_ADMIN_USER": "admin",
                                       "CML_ADMIN_PASS": "secret", "JUNOS_ADMIN_PASS": "j"}


def test_connection_dialog_test_button(qapp, tmp_path):
    from labdesk_app.ui.connection_dialog import CmlConnectionDialog
    d = CmlConnectionDialog(None, cred_file=tmp_path / "c.env", client_factory=_FakeClient,
                            settings=_settings(tmp_path))
    d.host.setText("10.0.0.9")
    d.password.setText("secret")
    d.test_connection()
    d._runner.wait(5000)
    qapp.processEvents()
    assert "Connected" in d.status.text()
    d.password.setText("wrong")
    d.test_connection()
    d._runner.wait(5000)
    qapp.processEvents()
    assert "wrong username or password" in d.status.text()
    assert not (tmp_path / "c.env").exists()           # testing never saves


def test_connection_dialog_vmx_field_on_windows(qapp, tmp_path, windows):
    from labdesk_app.ui.connection_dialog import VMX_KEY, CmlConnectionDialog
    st = _settings(tmp_path)
    d = CmlConnectionDialog(None, cred_file=tmp_path / "c.env", client_factory=_FakeClient, settings=st)
    assert d.vmx is not None
    d.host.setText("10.0.0.9")
    d.password.setText("secret")
    d.vmx.setText(r"C:\VMs\CML\cml.vmx")
    d._save()
    assert st.value(VMX_KEY) == r"C:\VMs\CML\cml.vmx"


def test_no_tracked_file_has_a_name_windows_cannot_check_out():
    """CON, PRN, AUX, NUL, COM1-9, LPT1-9 (with any extension), names ending in a dot or blank and the
    characters <>:"|?* cannot exist on Windows: one such file makes 'git clone' fail there."""
    import re
    import shutil
    import subprocess
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    if not shutil.which("git") or not (root / ".git").exists():
        pytest.skip("not a git checkout")
    files = subprocess.run(["git", "ls-files"], cwd=root, capture_output=True, text=True, check=True).stdout.split("\n")
    reserved = re.compile(r"^(con|prn|aux|nul|com[1-9]|lpt[1-9])(\..*)?$", re.I)
    bad = [f for f in files if f and any(reserved.match(part) or re.search(r'[<>:"|?*]', part) or part.endswith((".", " "))
                                         for part in f.split("/"))]
    assert not bad, f"rename these (invalid on Windows): {bad}"


def test_download_guide_and_windows_batch_files_agree():
    """DOWNLOAD.md names files that exist; the .bat files are ASCII with CRLF (cmd.exe labels need it) and
    .gitattributes keeps them that way in every checkout."""
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    guide = (root / "DOWNLOAD.md").read_text(encoding="utf-8")
    for name in ("install.sh", "install.bat", "labdesk.bat", "INSTALL.md", "requirements.txt"):
        assert (root / name).exists(), name
    for needle in ("git clone https://github.com/eTheDestroyer/labdesk.git", "./install.sh", ".\\install.bat",
                   ".\\labdesk.bat", "~/labdesk/labdesk", "git pull", "## Linux", "## Windows"):
        assert needle in guide, needle
    assert "*.bat text eol=crlf" in (root / ".gitattributes").read_text(encoding="utf-8")
    for name in ("install.bat", "labdesk.bat"):
        raw = (root / name).read_bytes()
        raw.decode("ascii")
        if b"\r\n" in raw:                                    # a Linux checkout may hold LF: both are fine here
            assert b"\n" not in raw.replace(b"\r\n", b"")
    install = (root / "install.bat").read_text(encoding="ascii")
    assert "requirements.txt" in install and ":nopython" in install and "LABDESK_NO_PAUSE" in install
    assert "-m labdesk_app" in (root / "labdesk.bat").read_text(encoding="ascii")
    assert (root / "INSTALL.md").read_text(encoding="utf-8").count("DOWNLOAD.md") >= 1
