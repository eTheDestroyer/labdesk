"""Window shell (builder #2): event log model (L5), compact controller (L10), lab summary (L5/L6), per-kind
inspector tabs and in-place tables (L4), dialogs with explicit verbs (L9), tips from real shortcuts (L6)."""
import os
import re
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEventLoop, QSize  # noqa: E402
from PySide6.QtWidgets import QApplication, QTableWidgetItem  # noqa: E402

UI = Path(__file__).resolve().parent.parent / "labdesk_app" / "ui"


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def _pump(app, secs=0.2):
    end = time.time() + secs
    while time.time() < end:
        app.processEvents(QEventLoop.AllEvents, 20)


# ----------------------------------------------------------------------------- L5 event log
def test_log_ring_buffer_coalesces_and_caps(app):
    from labdesk_app.ui.eventlog import Entry, LogModel
    m = LogModel(cap=50)
    inserts = []
    m.rowsInserted.connect(lambda *a: inserts.append(a))
    for i in range(120):
        m.add(Entry("12:00:00", "info", f"line {i}"))
    assert m.rowCount() == 0                       # coalesced: nothing until the 100 ms flush
    m.flush()
    assert m.rowCount() == 50 and len(inserts) == 1
    assert m.rows[0].msg == "line 70" and m.rows[-1].msg == "line 119"


def test_log_filter_levels_search_and_node_syntax(app):
    from labdesk_app.ui.eventlog import Entry, LogFilter, LogModel
    m = LogModel()
    m.node_labels = {"R1", "R2"}
    for e in (Entry("1", "info", "R1: probe ok"), Entry("2", "warn", "R2: OSPF neighbour lost"),
              Entry("3", "error", "R1: console busy"), Entry("4", "ok", "opened lab")):
        m.add(e)
    m.flush()
    assert [e.source for e in m.rows] == ["R1", "R2", "R1", ""]
    f = LogFilter()
    f.setSourceModel(m)
    assert f.rowCount() == 4
    f.set_level("warn")
    assert f.rowCount() == 2
    f.set_level("error")
    assert f.rowCount() == 1
    f.set_level("all")
    f.set_query("node:R1")
    assert f.rowCount() == 2
    f.set_query("node:R1 busy")
    assert f.rowCount() == 1


def test_event_log_widget_unread_errors_and_text(app):
    from labdesk_app.ui.eventlog import EventLog
    w = EventLog()
    seen = []
    w.unread_changed.connect(seen.append)
    w.append("everything fine")
    w.append("R1: broken", "error")
    assert w.unread == 1 and seen[-1] == 1
    assert "R1: broken" in w.toPlainText()
    w.mark_read()
    assert w.unread == 0 and seen[-1] == 0


# ----------------------------------------------------------------------------- L10 compact controller
def test_compact_controller_hysteresis_and_modes(app, tmp_path):
    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QWidget
    from labdesk_app.ui.compact import CompactController
    w = QWidget()
    st = QSettings(str(tmp_path / "c.ini"), QSettings.IniFormat)
    c = CompactController(w, st)
    assert c.want(QSize(1366, 768)) and not c.want(QSize(1920, 1080))
    c.compact = True                                    # hysteresis: 1500 wide stays compact ...
    assert c.want(QSize(1500, 1000))
    c.compact = False                                   # ... and 1500 wide stays full
    assert not c.want(QSize(1500, 1000))
    c.set_mode("compact")
    assert c.want(QSize(1920, 1080)) and st.value("ui/layout") == "compact"
    c.set_mode("full")
    assert not c.want(QSize(1024, 600))


# ----------------------------------------------------------------------------- L5/L6 one summary
def _nv(key, label, state, kind="iosv", chips=()):
    from labdesk_app.catalog import BY_KEY
    return SimpleNamespace(key=key, label=label, state=state, dtype=BY_KEY[kind], chips=list(chips), tele=None)


def test_summary_counts_and_first_affected_item():
    from labdesk_app.facts import Health, LinkKind
    from labdesk_app.ui.summary import summarize
    chip_ok = SimpleNamespace(kind="OSPF", health=Health.OK)
    chip_bad = SimpleNamespace(kind="OSPF", health=Health.DOWN)
    nodes = [_nv("a", "R1", "BOOTED", chips=[chip_ok]), _nv("b", "R2", "BOOTED", chips=[chip_bad]),
             _nv("c", "R3", "STOPPED")]
    links = [SimpleNamespace(key="l1", a="a", b="b", label_a="Gi0/0", label_b="Gi0/0", state="STARTED",
                             status=SimpleNamespace(kind=LinkKind.UP, text="")),
             SimpleNamespace(key="l2", a="b", b="c", label_a="Gi0/1", label_b="Gi0/1", state="STARTED",
                             status=SimpleNamespace(kind=LinkKind.LINK_DOWN, text="R3 Gi0/1 down"))]
    data = SimpleNamespace(title="Lab", lab_state="STARTED", nodes=nodes, links=links, telemetry=object())
    s = summarize(data)
    assert s.running_text == "2/3 running"
    rows = {r.label: r for r in s.rows}
    assert rows["Nodes up"].value == "2 / 3" and rows["Nodes up"].key == "c"
    assert rows["Links up"].value == "1 / 2" and rows["Links up"].tone == "down" and rows["Links up"].key == "l2"
    assert rows["OSPF healthy"].value == "1 / 2" and rows["OSPF healthy"].key == "b"


# ----------------------------------------------------------------------------- L4 inspector
def test_inspector_tabs_depend_only_on_node_kind():
    from labdesk_app.catalog import BY_KEY
    from labdesk_app.ui.inspector import KIND_TABS, TAB_CONFIG, TAB_EC, TAB_OVERVIEW, node_kind
    assert node_kind(BY_KEY["iosv"]) == "router" and node_kind(BY_KEY["vjunos"]) == "router"
    assert node_kind(BY_KEY["iosvl2"]) == "switch"
    assert node_kind(BY_KEY["alpine"]) == "host"
    assert node_kind(BY_KEY["ext_cloud"]) == "cloud" and node_kind(BY_KEY["unmanaged_switch"]) == "cloud"
    assert len(KIND_TABS["switch"]) == 7 and len(KIND_TABS["router"]) == 5     # + EtherChannel tab
    assert TAB_EC in KIND_TABS["switch"] and TAB_EC not in KIND_TABS["router"]
    assert KIND_TABS["host"] == {TAB_OVERVIEW, TAB_CONFIG} and KIND_TABS["cloud"] == {TAB_OVERVIEW}


def test_live_table_updates_cells_in_place(app):
    from labdesk_app.ui.inspector import _table
    t = _table(["A", "B"])

    def fill(vals):
        t.begin("node")
        for a, b in vals:
            r = t.rowCount()
            t.insertRow(r)
            t.setItem(r, 0, QTableWidgetItem(a))
            t.setItem(r, 1, QTableWidgetItem(b))
        t.commit()
    fill([("Gi0/0", "up"), ("Gi0/1", "down")])
    first = t.item(0, 0)
    t.selectRow(1)
    fill([("Gi0/0", "up"), ("Gi0/1", "up")])
    assert t.item(0, 0) is first                            # no rebuild
    assert t.item(1, 1).text() == "up" and t.currentRow() == 1
    assert t.item(1, 1).toolTip() == "up"                  # full text in the tooltip
    fill([("Gi0/0", "up")])
    assert t.rowCount() == 1


# ----------------------------------------------------------------------------- L9 dialogs
def test_no_yes_no_questions_or_input_dialogs_left():
    for p in UI.glob("*.py"):
        src = p.read_text()
        assert "QMessageBox.question" not in src and "QMessageBox.Yes" not in src, p.name
        assert "QInputDialog." not in src and "QInputDialog," not in src, p.name


def test_text_prompt_validates_inline(app):
    from labdesk_app import validate
    from labdesk_app.ui.dialogs import TextPrompt, validator
    d = TextPrompt("Rename", "New name", "R1", "Rename", validator(validate.cml_label))
    assert d.ok.isEnabled() and d.ok.text() == "Rename"
    d.edit.setText("bad name")
    assert not d.ok.isEnabled() and d.error.text()
    d.edit.setText("R9")
    assert d.ok.isEnabled()


def test_about_diagnostics_redact_the_host(app):
    from labdesk_app import config
    from labdesk_app.ui.dialogs import AboutDialog
    d = AboutDialog("2.10.1")
    assert config.CML_HOST not in d.diagnostics() and "<redacted>" in d.diagnostics()
    d.include_host.setChecked(True)
    assert config.CML_HOST in d.diagnostics()


# ----------------------------------------------------------------------------- the window
@pytest.fixture()
def window(app, tmp_path):
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from PySide6.QtCore import QSettings
    QSettings.setDefaultFormat(QSettings.IniFormat)
    QSettings.setPath(QSettings.IniFormat, QSettings.UserScope, str(tmp_path))
    from ui_replay import replay_controller
    from labdesk_app.ui.main_window import MainWindow

    class _L:
        def open(self, cmd):
            return "no"
    ctl = replay_controller(tmp_path)
    ctl.lab_id = None
    ctl.lab_title = None
    win = MainWindow(ctl, _L())
    win.timer.stop()
    win.resize(1920, 1080)
    win.show()
    _pump(app, 0.3)
    yield win
    win._closing = True
    win.runner.wait(3000)
    win.hide()
    win.deleteLater()


def test_tips_name_only_registered_shortcuts(app, window):
    win = window
    want = {a.shortcut().toString() for a, _ in win.tip_actions() if not a.shortcut().isEmpty()}
    assert {"Ctrl+O", "Ctrl+N", "F5", "F2", "Ctrl+J"} <= want
    grid = win.inspector.tips_grid
    assert grid.count() // 2 == len(want)


def test_empty_state_and_lab_actions_without_a_lab(app, window):
    win = window
    assert win.empty.isVisible() and not win.legend.isVisible()
    assert not win.a_start.isEnabled() and not win.a_wipe.isEnabled() and win.a_open.isEnabled()
    assert win.windowTitle() == "LabDesk"


def test_compact_mode_moves_the_docks(app, window):
    win = window
    win.compactor.set_mode("compact")
    _pump(app, 0.3)
    assert win._compact and not win.log_dock.isVisible()
    assert win.palette_list.rail and win.rail_dock.maximumWidth() == 56
    assert win.inspector_dock.minimumWidth() == 340
    win.toggle_log()                                       # Ctrl+J
    assert win.log_dock.isVisible()
    win.compactor.set_mode("full")
    _pump(app, 0.3)
    assert not win._compact and win.log_dock.isVisible() and not win.palette_list.rail
    assert win.settings.value("layout/compact") is not None   # per-mode saveState


def test_log_lines_no_longer_go_to_the_status_bar(app, window):
    win = window
    win.log("R1: something failed", "error")
    assert win.statusBar().currentMessage() == ""
    assert re.search(r"R1: something failed", win.logview.toPlainText())
