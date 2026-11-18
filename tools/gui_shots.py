#!/usr/bin/env python3
"""Design-system QA screenshots (real-fixture replay: no network, no ssh, no lab changes) -> qa/gui-*.png

    QT_QPA_PLATFORM=offscreen .venv/bin/python tools/gui_shots.py --scene switch|proto|cloud|empty \\
        --theme light|dark [--size 1920x1080]

Settings are isolated: XDG_CONFIG_HOME points at a fresh temp dir *before* the QApplication exists, so the
user's ~/.config/LabDesk is never touched.  Fonts are pinned (Noto Sans 10 / Noto Sans Mono) so shots are
deterministic.  Every window shot also gets a grayscale copy (*-gray.png) for the S2 shape-only check.
"""
from __future__ import annotations

import argparse
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "tools"))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
_TMP = tempfile.mkdtemp(prefix="labdesk-qa-")
os.environ["XDG_CONFIG_HOME"] = _TMP                     # isolated settings (L1)

from PySide6.QtCore import QPoint, QSettings, Qt  # noqa: E402
from PySide6.QtWidgets import QToolButton  # noqa: E402
from PySide6.QtGui import QImage  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from ui_shots import QA, NullLauncher, pump, select, wait_for  # noqa: E402

PX = ""
APP = None
ONLY: set | None = None


def shot(w, name, gray: bool = False):
    if ONLY and name not in ONLY:
        return
    path = QA / f"gui-{PX}{name}.png"
    img = w.grab()
    img.save(str(path))
    print("saved", path)
    if gray:
        g = img.toImage().convertToFormat(QImage.Format_Grayscale8)
        gp = QA / f"gui-{PX}{name}-gray.png"
        g.save(str(gp))
        print("saved", gp)


def node_by(win, label):
    return next(it for it in win.scene.nodes.values() if it.label == label)


def capture_menu(menu, name):
    from PySide6.QtCore import QTimer

    def grab():
        m = QApplication.activePopupWidget()
        if m is None:
            QTimer.singleShot(100, grab)
            return
        shot(m, name)
        m.close()
    QTimer.singleShot(400, grab)
    menu.exec(QPoint(0, 0))


def grab_dialog(dlg, name, app, setup=None):
    dlg.show()
    if setup:
        setup(dlg)
    pump(app, 0.5)
    shot(dlg, name)
    dlg.close()


def dialogs(win, app, labs):
    """L9 dialog shell: Open lab (table + filter), Templates (card grid), confirm, prompt, About."""
    from labdesk_app.ui import dialogs as D
    grab_dialog(D.OpenLabDialog(labs, win), "dialog-open", app)
    grab_dialog(D.OpenLabDialog(labs, win, "templates"), "dialog-templates", app)
    dlg = D.LabDeskDialog("Pull cable", win)
    # confirm() is modal: rebuild its body the same way for the shot
    import PySide6.QtWidgets as W
    from PySide6.QtCore import QTimer

    def grab_modal(name):
        def g():
            m = QApplication.activeModalWidget()
            if m is None:
                QTimer.singleShot(100, g)
                return
            pump(app, 0.2)
            shot(m, name)
            m.reject()
        QTimer.singleShot(300, g)
    grab_modal("dialog-confirm")
    D.confirm(win, "Pull cable", "Simulate unplugging the cable R3 Gi0/1 ↔ R4 Gi0/1?", "Pull Cable",
              detail="CML stops the link; both routers see the interface go down.")
    grab_modal("dialog-rename")
    from labdesk_app import validate
    D.TextPrompt.ask(win, "Rename", "New name for R1", "R1 new", "Rename", D.validator(validate.cml_label))
    grab_modal("dialog-about")
    win.show_about()
    del dlg, W


def shell_shots(win, app, sc):
    """L2/L4/L5/L6/L10: inspector idle summary + tips, toolbar, log filters, inspector widths."""
    win.scene.clearSelection()
    pump(app, 0.4)
    shot(win.inspector_dock, "inspector-idle")
    shot(win.toolbar, "toolbar")
    shot(win.statusBar(), "statusbar")
    win.log("R1: probe error - console busy (test line)", "error")
    win.log("SW2 Gi0/2 VLAN 10,20: Altn BLK → Root FWD", "warn", tag="STP", hue="#D97706")
    pump(app, 0.4)
    if win.log_dock.isVisible():
        shot(win.log_dock, "log")
        win.logview.level_btns["warn"].click()
        pump(app, 0.3)
        shot(win.log_dock, "log-warnings")
        win.logview.level_btns["all"].click()
    else:
        shot(win.statusBar(), "statusbar-errors")
        win.toggle_log(True)
        pump(app, 0.5)
        shot(win, "log-open")
        win.toggle_log(False)
        pump(app, 0.3)
    pick = {"switch": "SW2", "proto": "R1", "cloud": "INTERNET"}[sc]
    select(win, pick)
    pump(app, 0.5)
    for wpx in (360, 340):
        win.resizeDocks([win.inspector_dock], [wpx], Qt.Horizontal)
        pump(app, 0.4)
        tb = win.inspector.tabs.tabBar()
        vis = [i for i in range(tb.count()) if tb.isTabVisible(i)]
        clipped = [tb.tabText(i) for i in vis if tb.tabRect(i).width() < tb.fontMetrics().horizontalAdvance(tb.tabText(i))]
        scroll = any(b.isVisible() for b in tb.findChildren(QToolButton))
        print(f"inspector {wpx}px: dock={win.inspector_dock.width()} tabs={[tb.tabText(i) for i in vis]} "
              f"scroll_buttons={scroll} clipped={clipped}")
        shot(win.inspector_dock, f"inspector-{pick.lower()}-{wpx}")
    win.resizeDocks([win.inspector_dock], [340 if win._compact else 380], Qt.Horizontal)
    pump(app, 0.3)
    tabs = win.inspector.tabs
    for i in range(1, tabs.count()):
        if tabs.isTabVisible(i):
            tabs.setCurrentIndex(i)
            pump(app, 0.3)
            shot(win.inspector_dock, f"inspector-{pick.lower()}-tab-{tabs.tabText(i).lower()}")
    tabs.setCurrentIndex(0)
    capture_menu(win.inspector.more_menu, "menu-more")
    capture_menu(win.help_pill.menu(), "menu-help")
    win.notify("Starting every node would pass the CML-Free limit (5 of 5 already running).", "warn", 0)
    pump(app, 0.3)
    shot(win.view, "notify")
    win.notifybar.hide()
    win.scene.clearSelection()
    pump(app, 0.3)


def main() -> int:
    global PX, APP, ONLY
    ap = argparse.ArgumentParser()
    ap.add_argument("--scene", required=True)
    ap.add_argument("--theme", default="light")
    ap.add_argument("--size", default="1920x1080")
    ap.add_argument("--only")
    a = ap.parse_args()
    ONLY = set(a.only.split(",")) if a.only else None
    w, h = (int(x) for x in a.size.split("x"))
    QApplication.setStyle("Fusion")
    APP = app = QApplication(sys.argv[:1])
    QSettings.setDefaultFormat(QSettings.IniFormat)
    QSettings.setPath(QSettings.IniFormat, QSettings.UserScope, _TMP)
    from labdesk_app.ui import theme as T
    from labdesk_app.ui import themectl
    from PySide6.QtGui import QFont
    T.pin_fonts("Noto Sans", "Noto Sans Mono", 10.0)
    app.setFont(QFont("Noto Sans", 10))
    themectl.install(app, mode=a.theme)
    PX = f"{a.scene}-{a.theme}-{w}-"
    import faulthandler
    faulthandler.dump_traceback_later(170, exit=True)
    from labdesk_app import cloud
    from labdesk_app.ui.main_window import MainWindow

    if a.scene == "switch":
        from switch_replay import poll_until_settled, replay_controller
        ctl = replay_controller()
        poll_until_settled(ctl)
    else:
        from ui_replay import poll_until_settled, replay_controller
        ctl = replay_controller()
        if a.scene == "cloud":
            n = next(n for n in ctl.client.d["nodes"] if n["label"] == "NAT")
            n["label"] = "INTERNET"
            n["tags"] = ["labdesk-cloud"]
            n["configuration"] = [{"name": "default", "content": "System Bridge"}]
            up = cloud.health_from("ts=1000\nstate=up\nanswers=8.8.8.8:ok 1.1.1.1:ok\n", ns_exists=True,
                                   host_ping=True, installed=True, now=1001)
            cloud.check_health = lambda *x, **k: up
        if a.scene == "empty":
            ctl.lab_id = None
            ctl.lab_title = None
        else:
            poll_until_settled(ctl)
    win = MainWindow(ctl, NullLauncher())
    win.resize(w, h)
    win.show()
    if a.scene != "empty":
        win._lab_opened()
        wait_for(app, win, lambda d: d.telemetry is not None and bool(d.nodes), 25)
    pump(app, 1.5)
    win.infobar.hide()
    win._notice_shown = True
    win.view.fit_all()
    pump(app, 0.6)
    sc = a.scene
    if sc == "empty":
        pump(app, 1.0)
        shot(win, "empty")
        win.timer.stop()
        win.runner.wait(3000)
        pump(app, 0.3)
        win._set_cml_ok(False)
        pump(app, 0.4)
        shot(win.view, "empty-offline")
        win._set_cml_ok(True, "2.10.1")
        pump(app, 0.6)
        dialogs(win, app, labs=ctl.list_labs())
        win.close()
        return 0
    shell_shots(win, app, sc)
    shot(win, "overview", gray=True)
    shot(win.view, "canvas")
    if hasattr(win.legend, "card"):
        shot(win.legend, "legend")
    else:
        shot(win.legend, "legend")
    if sc == "switch":
        select(win, "SW2")
        pump(app, 0.6)
        shot(win, "sw2-selected")
        select(win, link=("SW1", "Gi0/2", "SW2", "Gi0/2"))
        pump(app, 0.6)
        shot(win, "link-blocked")
        win.scene.clearSelection()
        win.layers.set_vlan(10)
        pump(app, 0.8)
        shot(win.view, "vlan10")
        win.layers.set("vlan", False)
        pump(app, 0.4)
        win.view.resetTransform(); win.view.scale(1.5, 1.5); win.view.centerOn(20, 10)
        pump(app, 0.6)
        shot(win.view, "zoom")
        win.view.resetTransform(); win.view.scale(0.45, 0.45)
        pump(app, 0.6)
        shot(win.view, "far", gray=True)
        win.view.fit_all()
        pump(app, 0.4)
        for b in win.findChildren(type(win.findChild(QApplication.instance().__class__) or object)):
            pass
        from PySide6.QtWidgets import QToolButton
        for b in win.findChildren(QToolButton):
            if b.text() == "View" and b.menu() is not None:
                capture_menu(b.menu(), "menu-view")
    elif sc == "proto":
        select(win, "R1")
        pump(app, 0.6)
        shot(win, "r1-selected")
        select(win, link=("R2", "Gi0/4", "R4", "Gi0/2"))
        pump(app, 0.6)
        shot(win.view, "link-admindown")
        win.scene.clearSelection()
        for k in ("ospf", "bgp", "traffic"):
            win.layers.set(k, True)
        pump(app, 4.0); win.poll(); pump(app, 1.0)
        shot(win.view, "layers")
        for k in ("ospf", "bgp", "traffic"):
            win.layers.set(k, False)
        tele = ctl.telemetry
        lk = next(l.key for l in win.view_data.links if {win._label(l.a), win._label(l.b)} == {"R3", "R4"})
        tele.pull_cable(lk)
        tele.set_condition(next(l.key for l in win.view_data.links
                                if {win._label(l.a), win._label(l.b)} == {"R1", "R3"}), preset="Satellite 600 ms")
        win.poll(); pump(app, 1.5)
        shot(win.view, "cable-pulled", gray=True)
        win.view.resetTransform(); win.view.scale(1.4, 1.4); win.view.centerOn(0, 40)
        pump(app, 0.6)
        shot(win.view, "zoom")
        win.view.resetTransform(); win.view.scale(0.42, 0.42)
        pump(app, 0.6)
        shot(win.view, "far", gray=True)
        win.view.fit_all()
        tele.restore_cable(lk); win.poll(); pump(app, 1.0)
    elif sc == "cloud":
        win.cloud_timer.stop()
        win._cloud_done(cloud.check_health())
        pump(app, 1.0)
        shot(win, "cloud-up")
        down = cloud.health_from("ts=1000\nstate=stopped\n", ns_exists=False, host_ping=None, installed=True,
                                 now=1001)
        win._cloud_done(down); pump(app, 0.8)
        win.scene.clearSelection(); pump(app, 0.4)
        shot(win, "cloud-down", gray=True)
        cl = node_by(win, "INTERNET")
        win.view.resetTransform(); win.view.scale(1.6, 1.6); win.view.centerOn(cl.pos().x(), cl.pos().y() + 30)
        pump(app, 0.6)
        shot(win.view, "cloud-down-zoom")
    win.close()
    pump(app, 0.3)
    ctl.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
