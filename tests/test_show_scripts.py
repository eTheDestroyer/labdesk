"""Per-device show scripts: storage + validation, the sentinel-free ('plain') console run, the engine action
and the editor dialog."""
import json
import time

import pytest
from fake_console import FakeConsole
from showcase_replay import fixture_text

from labdesk_app import show_scripts as SS
from labdesk_app.facts import Fact, FhrpGroup, NodeFacts, Proto
from labdesk_app.probe.session import BurstStatus, IosDialect, run_burst

CMDS = ["show ip interface brief", "show standby brief"]


def outputs(r="R1"):
    return {c: fixture_text(r, c) for c in CMDS}


# ----------------------------------------------------------------------------- validation
def test_only_read_only_commands_are_accepted():
    assert SS.parse("show ip route\nping 8.8.8.8\ntraceroute 8.8.8.8\n") == \
        ["show ip route", "ping 8.8.8.8", "traceroute 8.8.8.8"]
    for bad in ("configure terminal", "reload", "write memory", "no shutdown", "clear ip bgp *",
                "shutdown", "copy run start", "debug ip packet", "showme"):
        with pytest.raises(SS.ScriptError):
            SS.parse(bad)


def test_abbreviated_verbs_comments_blank_lines_and_duplicates():
    text = "! my HSRP checks\n\n  sh   standby brief \n# another comment\nsho ip int br\nshow standby brief\n"
    assert SS.parse(text) == ["show standby brief", "show ip int br"]


def test_file_redirects_and_control_characters_are_refused():
    assert SS.check_line("show running-config | redirect flash:x")
    assert SS.check_line("show version | tee flash:x")
    assert SS.check_line("show ip route\x03")
    assert not SS.check_line("show ip route | include 0.0.0.0")
    assert SS.check_line("show " + "x" * SS.MAX_LEN)


def test_error_names_the_line():
    with pytest.raises(SS.ScriptError) as e:
        SS.parse("show ip route\nconf t\n")
    assert "line 2" in str(e.value) and "only show" in str(e.value)


def test_too_many_commands():
    with pytest.raises(SS.ScriptError):
        SS.parse("\n".join(f"show ip route 10.0.{i}.0" for i in range(SS.MAX_COMMANDS + 1)))


# ----------------------------------------------------------------------------- storage
def test_each_device_keeps_its_own_script(tmp_path):
    p = tmp_path / "show-scripts.json"
    SS.save("R1", SS.ShowScript(["show standby brief"], True), p)
    SS.save("HUB", SS.ShowScript(["show ip bgp summary", "show ip route"], False), p)
    assert SS.load("R1", p).commands == ["show standby brief"] and SS.load("R1", p).active
    hub = SS.load("HUB", p)
    assert hub.commands == ["show ip bgp summary", "show ip route"] and not hub.on_open and not hub.active
    assert SS.load("R2", p) is None
    SS.save("R1", None, p)                                    # an empty script removes the entry
    assert SS.load("R1", p) is None and SS.load("HUB", p) is not None
    assert json.loads(p.read_text())["version"] == SS.VERSION


def test_save_refuses_a_config_command_and_a_hand_edited_file_is_revalidated(tmp_path):
    p = tmp_path / "show-scripts.json"
    with pytest.raises(SS.ScriptError):
        SS.save("R1", SS.ShowScript(["configure terminal"]), p)
    assert not p.exists()
    p.write_text(json.dumps({"version": 1, "devices": {"R1": {"commands": ["show clock", "reload", 7]}}}))
    assert SS.load("R1", p).commands == ["show clock"]
    p.write_text("not json")
    assert SS.load_all(p) == {}


# ----------------------------------------------------------------------------- suggestions (live facts)
def test_suggestion_follows_what_the_device_is_running():
    assert SS.suggest(None) == ["show ip interface brief", "show ip route"]
    nf = NodeFacts("cml:n1", "R1")
    nf.fhrp = Fact([FhrpGroup(Proto.HSRP, "GigabitEthernet0/1", 10, "Active", 110, True, "172.16.0.1")], time.time())
    got = SS.suggest(nf)
    assert "show standby brief" in got and "show track brief" in got
    assert "show ip bgp summary" not in got and "show vrrp brief" not in got
    nf.bgp = Fact(object(), time.time())
    assert "show ip bgp summary" in SS.suggest(nf)
    sw = SS.suggest(None, is_switch=True)
    assert "show vlan brief" in sw and "show spanning-tree summary" in sw and "show ip route" not in sw
    assert not any(SS.check_line(c) for c in got + sw)


# ----------------------------------------------------------------------------- plain console run
def test_plain_run_types_no_sentinel_lines():
    con = FakeConsole("R1", "priv", outputs())
    res = run_burst(con, IosDialect(), CMDS, timeout=10, plain=True)
    assert res.status == BurstStatus.OK and res.hostname == "R1"
    sent = con.sent_text
    assert "LDK" not in sent and "show clock" not in sent
    assert "terminal length 0" in sent and "terminal length 24" in sent
    assert res.outputs["show standby brief"].text.strip().endswith("192.168.10.1")
    assert res.outputs["show ip interface brief"].text.startswith("Interface")
    assert "R1#" not in res.outputs["show ip interface brief"].text


def test_plain_run_keeps_the_device_error_text():
    con = FakeConsole("R1", "user", {})
    res = run_burst(con, IosDialect(), ["show bogus"], timeout=10, plain=True)
    assert "% Invalid input" in res.outputs["show bogus"].text


def test_stay_logged_in_leaves_a_logged_out_line_at_its_prompt():
    con = FakeConsole("R1", "logged_out", outputs())
    res = run_burst(con, IosDialect(), CMDS, timeout=10, plain=True, stay_logged_in=True)
    assert res.status == BurstStatus.OK and res.mode_found == "logged_out"
    assert "exit" not in con.sent_text and con.mode == "user"
    con = FakeConsole("R1", "logged_out", outputs())           # the background probes still log out again
    run_burst(con, IosDialect(), CMDS, timeout=10)
    assert "exit" in con.sent_text


def test_plain_run_stops_at_a_command_that_never_answers():
    con = FakeConsole("R1", "priv", outputs(), hang={"show standby brief"})
    res = run_burst(con, IosDialect(), CMDS + ["show ip route"], timeout=6, cmd_timeout=1.0, plain=True)
    assert res.outputs["show ip interface brief"].text is not None
    assert res.outputs["show standby brief"].text is None and "show ip route" not in res.outputs


def test_plain_ping_waits_for_the_prompt():
    con = FakeConsole("R1", "user", {"ping 8.8.8.8": "!!!!!\nSuccess rate is 100 percent (5/5)\n"})
    res = run_burst(con, IosDialect(), ["ping 8.8.8.8"], timeout=10, plain=True)
    assert "Success rate is 100 percent" in res.outputs["ping 8.8.8.8"].text and "LDK" not in con.sent_text


# ----------------------------------------------------------------------------- engine action
@pytest.fixture()
def ctl(tmp_path, monkeypatch):
    from test_telemetry_engine import LAB, FakeClient
    from labdesk_app import config
    from labdesk_app.controller import LabController
    from labdesk_app.probe.creds import DeviceCreds
    from labdesk_app.telemetry import TelemetryEngine
    consoles = []

    def factory(uuid, user):
        from test_telemetry_engine import UUID_TO_LABEL
        from labdesk_app.telemetry import IOS_FAST, IOS_SLOW
        r = UUID_TO_LABEL[uuid]
        con = FakeConsole(r, "priv", {c: fixture_text(r, c) or "" for c in IOS_FAST + IOS_SLOW})
        consoles.append(con)
        return con

    monkeypatch.setattr(config, "STATE_FILE", tmp_path / "state.json")
    c = LabController(FakeClient(), config.State(tmp_path / "state.json"), telemetry=False)
    c.telemetry = TelemetryEngine(c.client, transport_factory=factory, probe_log_dir=tmp_path / "probe",
                                  console_scan=lambda ex: set(), creds_loader=lambda label: DeviceCreds())
    c.lab_id = LAB
    c.consoles = consoles
    yield c
    c.shutdown()


def _r1(ctl):
    end = time.time() + 30
    while time.time() < end:
        v = ctl.poll()
        r1 = next((n for n in v.nodes if n.label == "R1"), None)
        if r1 is not None and ctl.telemetry.console_uuid.get(r1.key) and ctl.telemetry.facts.get(r1.key) \
                and ctl.telemetry.facts[r1.key].ifaces is not None:
            return r1.key
        time.sleep(0.3)
    raise AssertionError("R1 never probed")


def test_engine_runs_the_script_plain_and_reports_it(ctl):
    key = _r1(ctl)
    before = len(ctl.consoles)
    res = ctl.telemetry.run_show_script(key, ["sh standby brief", "show ip interface brief"])
    assert res.ok, res.text
    assert res.text.startswith("2 of 2 command(s)")
    mine = [c for c in ctl.consoles[before:] if "show standby brief\r" in c.sent_text and "LDK" not in c.sent_text]
    assert mine, "the script was not typed plain"
    assert "Interface" in res.data["outputs"]["show ip interface brief"]


def test_engine_reports_a_command_the_device_rejects(ctl):
    key = _r1(ctl)
    res = ctl.telemetry.run_show_script(key, ["show ip interface brief", "show bogus thing"])
    assert not res.ok and "the device rejected: 'show bogus thing'" in res.text
    assert res.data["rejected"] == ["show bogus thing"]


def test_engine_never_types_a_config_command(ctl):
    key = _r1(ctl)
    before = len(ctl.consoles)
    res = ctl.telemetry.run_show_script(key, ["show clock", "configure terminal"])
    assert not res.ok and "only show, ping and traceroute" in res.text
    assert not any("configure" in c.sent_text for c in ctl.consoles[before:])
    assert not ctl.telemetry.run_show_script(key, []).ok


# ----------------------------------------------------------------------------- dialog
def test_dialog_validates_suggests_and_copies():
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    from labdesk_app.ui.dialogs import ShowScriptDialog
    others = {"HUB": SS.ShowScript(["show ip bgp summary"], True)}
    dlg = ShowScriptDialog("R1", None, ["show ip interface brief", "show standby brief"], others, can_run=True)
    assert dlg.on_open.isChecked() and dlg.result_script() is None and not dlg.run_btn.isEnabled()
    dlg._suggest()
    assert dlg.commands() == ["show ip interface brief", "show standby brief"]
    assert dlg.ok.isEnabled() and dlg.run_btn.isEnabled()
    dlg.edit.setPlainText("show clock\nreload\n")
    assert not dlg.ok.isEnabled() and "line 2" in dlg.error.text()
    dlg._copy(1)
    assert dlg.commands() == ["show ip bgp summary"] and dlg.ok.isEnabled()
    dlg.on_open.setChecked(False)
    got = dlg.result_script()
    assert got.commands == ["show ip bgp summary"] and not got.on_open
    off = ShowScriptDialog("R1", SS.ShowScript(["show clock"], False), [], {}, can_run=False)
    assert not off.on_open.isChecked() and not off.run_btn.isEnabled() and not off.copy_from.isEnabled()
    for d in (dlg, off):
        d.close()
        d.deleteLater()
    app.processEvents()


# ----------------------------------------------------------------------------- the window
def test_opening_a_console_runs_that_devices_script_only(tmp_path, monkeypatch):
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QEventLoop, QSettings
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    QSettings.setDefaultFormat(QSettings.IniFormat)
    QSettings.setPath(QSettings.IniFormat, QSettings.UserScope, str(tmp_path))
    monkeypatch.setenv("LABDESK_SHOW_SCRIPTS", str(tmp_path / "show-scripts.json"))
    import ui_replay
    from labdesk_app.ui.main_window import MainWindow

    def pump(secs):
        end = time.time() + secs
        while time.time() < end:
            app.processEvents(QEventLoop.AllEvents, 30)

    class _L:
        opened = []

        def open(self, cmd):
            self.opened.append(cmd.title)
            return "tab opened"

    ctl = ui_replay.replay_controller(tmp_path)
    ui_replay.poll_until_settled(ctl)
    win = MainWindow(ctl, _L())
    win.timer.stop()
    win.show()
    win._lab_opened()
    end = time.time() + 25
    while time.time() < end and not (win.view_data is not None and win.view_data.nodes and win.scene.nodes):
        pump(0.3)
    try:
        keys = {it.label: k for k, it in win.scene.nodes.items()}
        r1, r2 = keys["R1"], keys["R2"]
        assert win.has_show_script(r1) and win.show_script_of(r1) is None
        ran = []
        from labdesk_app import launchers
        from labdesk_app.ui import main_window as MW
        popups = []
        monkeypatch.setattr(MW, "info", lambda *a, **k: popups.append(a[1:]))        # never a modal box in a test
        monkeypatch.setattr(ctl, "console_command",
                            lambda key: launchers.ConsoleCommand(win._label(key), ["ssh", "x"], {}))
        monkeypatch.setattr(ctl.telemetry, "run_show_script",
                            lambda key, cmds, **kw: ran.append((key, list(cmds))) or
                            __import__("labdesk_app.telemetry", fromlist=["x"]).ActionResult(True, "ok"))
        SS.save("R1", SS.ShowScript(["show standby brief", "show ip bgp summary"], True))
        SS.save("R2", SS.ShowScript(["show vrrp brief"], False))          # saved, but not on open
        for key in (r1, r2):
            win.open_console(key)
        end = time.time() + 10
        while time.time() < end and not ran:
            pump(0.2)
        pump(0.5)
        assert ran == [(r1, ["show standby brief", "show ip bgp summary"])] and not popups
        assert sorted(_L.opened) == ["R1", "R2"]
        win.run_show_script(r2)                                            # the menu's "Run show script now"
        end = time.time() + 10
        while time.time() < end and len(ran) < 2:
            pump(0.2)
        assert ran[-1] == (r2, ["show vrrp brief"])
        assert win._show_script_suggestion(r1)[0] == "show ip interface brief"
        assert "show standby brief" in win._show_script_suggestion(r1)      # R1 runs HSRP in the replay (live facts)
    finally:
        win._closing = True
        win.runner.wait(3000)
        win.hide()
        win.deleteLater()
        ctl.shutdown()
        app.processEvents()
