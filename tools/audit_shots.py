#!/usr/bin/env python3
"""GUI audit screenshots (replay only: no network, no ssh, no lab changes) -> qa/audit-*.png

    QT_QPA_PLATFORM=offscreen .venv/bin/python tools/audit_shots.py --scene switch|proto|cloud|empty \
        [--dark] [--size 1920x1080]

--dark uses the user's KDE colour scheme (~/.config/kdeglobals, what Qt's KDE platform theme feeds the
real app), the KDE icon theme and Noto Sans 10 (the KDE default font).  Light = Fusion's default palette.
Context menus are captured by turning QMenu.exec into popup()+grab.
"""
from __future__ import annotations

import argparse
import configparser
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "tools"))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
# QSettings("LabDesk", "LabDesk") is NativeFormat: keep it away from the user's ~/.config/LabDesk
os.environ["XDG_CONFIG_HOME"] = str(ROOT / "qa" / ".settings-audit")

from PySide6.QtCore import QPoint, QSettings  # noqa: E402
from PySide6.QtGui import QColor, QFont, QIcon, QPalette  # noqa: E402
from PySide6.QtWidgets import QApplication, QInputDialog, QMenu, QMessageBox  # noqa: E402

from ui_shots import QA, NullLauncher, pump, select, wait_for  # noqa: E402

PX = ""
APP = None


def shot(w, name):
    path = QA / f"audit-{PX}{name}.png"
    w.grab().save(str(path))
    print("saved", path)


def kde_palette():
    cp = configparser.ConfigParser(interpolation=None, strict=False)
    cp.optionxform = str
    cp.read(Path.home() / ".config" / "kdeglobals")

    def c(sec, key, fb):
        try:
            return QColor(*[int(x) for x in cp[sec][key].split(",")[:3]])
        except Exception:  # noqa: BLE001
            return QColor(fb)
    pal = QPalette()
    W, V, B, S, T = "Colors:Window", "Colors:View", "Colors:Button", "Colors:Selection", "Colors:Tooltip"
    for role, col in ((QPalette.Window, c(W, "BackgroundNormal", "#202329")),
                      (QPalette.WindowText, c(W, "ForegroundNormal", "#E6EAF0")),
                      (QPalette.Base, c(V, "BackgroundNormal", "#1A1D22")),
                      (QPalette.AlternateBase, c(V, "BackgroundAlternate", "#23272D")),
                      (QPalette.Text, c(V, "ForegroundNormal", "#E6EAF0")),
                      (QPalette.Button, c(B, "BackgroundNormal", "#2A2E35")),
                      (QPalette.ButtonText, c(B, "ForegroundNormal", "#E6EAF0")),
                      (QPalette.Highlight, c(S, "BackgroundNormal", "#3daee9")),
                      (QPalette.HighlightedText, c(S, "ForegroundNormal", "#ffffff")),
                      (QPalette.ToolTipBase, c(T, "BackgroundNormal", "#202329")),
                      (QPalette.ToolTipText, c(T, "ForegroundNormal", "#E6EAF0")),
                      (QPalette.Link, c(V, "ForegroundLink", "#3daee9")),
                      (QPalette.PlaceholderText, c(V, "ForegroundInactive", "#888"))):
        pal.setColor(role, col)
    icons = cp.get("Icons", "Theme", fallback="breeze-dark")
    return pal, icons


def capture_menus():
    """QMenu.exec blocks: a timer grabs the open popup, then closes it."""
    from PySide6.QtCore import QTimer
    counter = {"name": "menu"}

    class Arm:
        def __setitem__(self, k, v):
            counter[k] = v

            def grab():
                m = QApplication.activePopupWidget()
                if m is None:
                    QTimer.singleShot(100, grab)
                    return
                shot(m, counter["name"])
                m.close()
            QTimer.singleShot(400, grab)
    return Arm()


def node_by(win, label):
    return next(it for it in win.scene.nodes.values() if it.label == label)


def node_tabs(win, label, prefix):
    select(win, label)
    pump(APP, 0.6)
    shot(win, f"{prefix}-window")
    tabs = win.inspector.tabs
    for i in range(tabs.count()):
        if not tabs.isTabVisible(i):
            continue
        tabs.setCurrentIndex(i)
        pump(APP, 0.4)
        shot(win.inspector_dock, f"{prefix}-tab{i}-{tabs.tabText(i).lower().replace(' ', '')}")
    tabs.setCurrentIndex(0)


def legend_full(win, name):
    win.legend.show()
    win.legend.minimized = False
    win.legend.full = True
    for k in list(win.legend.collapsed):
        win.legend.collapsed[k] = False
    win.legend.rebuild()
    pump(APP, 0.5)
    shot(win.legend, name + "-crop")
    shot(win, name)
    win.legend.full = False
    win.legend.collapsed["Nodes"] = True
    win.legend.rebuild()


def main() -> int:
    global PX, APP
    ap = argparse.ArgumentParser()
    ap.add_argument("--scene", required=True)
    ap.add_argument("--dark", action="store_true")
    ap.add_argument("--size", default="1920x1080")
    a = ap.parse_args()
    w, h = (int(x) for x in a.size.split("x"))
    APP = app = QApplication(sys.argv[:1])
    QIcon.setThemeSearchPaths([str(Path.home() / ".local/share/icons"), "/usr/share/icons"])
    app.setFont(QFont("Noto Sans", 10))
    if a.dark:
        pal, icons = kde_palette()
        app.setPalette(pal)
        QIcon.setThemeName(icons)
    else:
        QIcon.setThemeName("breeze")
    QSettings.setDefaultFormat(QSettings.IniFormat)
    QSettings.setPath(QSettings.IniFormat, QSettings.UserScope, str(ROOT / "qa" / ".settings-audit"))
    PX = f"{a.scene}-{'dark' if a.dark else 'light'}-{w}-"
    import faulthandler; faulthandler.dump_traceback_later(150, exit=True)
    menus = capture_menus()
    from labdesk_app import cloud
    from labdesk_app.ui.main_window import MainWindow

    if a.scene == "switch":
        from switch_replay import poll_until_settled, replay_controller
        ctl = replay_controller()
        poll_until_settled(ctl)
    elif a.scene in ("proto", "cloud", "empty"):
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
    pump(app, 0.5)
    sc = a.scene

    if sc == "empty":
        shot(win, "empty")
        win.infobar.show()
        win.infobar.reposition()
        pump(app, 0.3)
        shot(win, "empty-infobar")
        win.infobar.hide()
        from labdesk_app.ui.dialogs import OpenLabDialog
        labs = [{"id": "x1", "lab_title": "Switching Lab", "node_count": 5, "state": "STARTED"},
                {"id": "x2", "lab_title": "Protocol Showcase", "node_count": 7, "state": "STOPPED"},
                {"id": "x3", "lab_title": "LabDesk Demo", "node_count": 3, "state": "DEFINED_ON_CORE"}]
        d = OpenLabDialog(labs, win)
        d.show(); pump(app, 0.4); shot(d, "dialog-open")
        d.tabs.setCurrentIndex(1); pump(app, 0.3); shot(d, "dialog-templates"); d.close()
        win.close()
        return 0

    shot(win, "overview")
    shot(win.view, "canvas")
    if sc == "switch":
        node_tabs(win, "SW2", "sw2")
        node_tabs(win, "R1", "r1")
        node_tabs(win, "PC1", "pc1")
        select(win, link=("SW1", "Gi0/2", "SW2", "Gi0/2"))
        pump(app, 0.6)
        shot(win, "link-blocked")
        win.scene.clearSelection()
        win.layers.set_vlan(10)
        pump(app, 0.8)
        shot(win, "vlan10")
        win.layers.set("vlan", False)
        pump(app, 0.4)
        legend_full(win, "legend")
        win.legend.hide()
        win.view.resetTransform(); win.view.scale(1.5, 1.5); win.view.centerOn(20, 10)
        pump(app, 0.5)
        shot(win.view, "zoom")
        win.legend.show(); win.view.fit_all()
        shot(win.palette_list.parentWidget(), "palette")
        menus["name"] = "menu-node-sw2"
        win.node_menu(node_by(win, "SW2").key, QPoint(0, 0))
        lk = next(iter(win.scene.links))
        menus["name"] = "menu-link"
        win.link_menu(lk, QPoint(0, 0))
        menus["name"] = "menu-canvas"
        win.canvas_menu(win.view.mapToScene(10, 10), QPoint(0, 0))
        menus["name"] = "menu-simulate"
        win._fill_sim_menu(); win.sim_menu.exec(QPoint(0, 0))
        from PySide6.QtWidgets import QToolButton
        for b in win.findChildren(QToolButton):
            if b.text() in ("View", "Lab") and b.menu() is not None:
                menus["name"] = "menu-" + b.text().lower()
                b.menu().exec(QPoint(0, 0))
    elif sc == "proto":
        node_tabs(win, "R1", "r1")
        node_tabs(win, "R3", "r3")
        select(win, link=("R1", "Gi0/2", "R3", "Gi0/0")); pump(app, 0.6); shot(win, "link-p2p")
        select(win, link=("R2", "Gi0/4", "R4", "Gi0/2")); pump(app, 0.6); shot(win, "link-admindown")
        shot(win.inspector_dock, "link-admindown-dock")
        select(win, "SW-A"); pump(app, 0.6); shot(win, "unmanaged-sw")
        select(win, "NAT"); pump(app, 0.6); shot(win, "nat")
        win.scene.clearSelection()
        for k in ("ospf", "bgp", "traffic"):
            win.layers.set(k, True)
        pump(app, 4.0); win.poll(); pump(app, 1.0)
        shot(win, "layers")
        for k in ("ospf", "bgp", "traffic"):
            win.layers.set(k, False)
        legend_full(win, "legend")
        win.legend.minimized = True; win.legend.rebuild(); pump(app, 0.3); shot(win.view, "legend-min")
        win.legend.minimized = False; win.legend.rebuild()
        tele = ctl.telemetry
        lk = next(l.key for l in win.view_data.links if {win._label(l.a), win._label(l.b)} == {"R3", "R4"})
        tele.pull_cable(lk)
        tele.set_condition(next(l.key for l in win.view_data.links
                                if {win._label(l.a), win._label(l.b)} == {"R1", "R3"}), preset="Satellite 600 ms")
        win.poll(); pump(app, 1.5)
        select(win, link=("R3", "Gi0/2", "R4", "Gi0/0")); pump(app, 0.6)
        shot(win, "cable-pulled")
        win.scene.clearSelection()
        r1 = next(n.key for n in win.view_data.nodes if n.label == "R1")
        win.animate_default_path(r1); pump(app, 1.4); shot(win.view, "packet-dropped")
        tele.restore_cable(lk); win.poll(); pump(app, 2.0)
        menus["name"] = "menu-node-r1"
        win.node_menu(node_by(win, "R1").key, QPoint(0, 0))
        menus["name"] = "menu-link"
        win.link_menu(next(l.key for l in win.view_data.links
                           if {win._label(l.a), win._label(l.b)} == {"R1", "R3"}), QPoint(0, 0))
        menus["name"] = "menu-simulate"
        win._fill_sim_menu(); win.sim_menu.exec(QPoint(0, 0))
        # dialogs
        from labdesk_app.ui.dialogs import ConfigDialog, PropertiesDialog
        from showcase_replay import fixture_text
        cfg = fixture_text("R1", "show running-config") or "hostname R1\n!\nend\n"
        d = ConfigDialog("R1", cfg, win); d.show(); pump(app, 0.4); shot(d, "dialog-config"); d.close()
        d = PropertiesDialog("R1", {"Name": "R1", "Type": "Cisco IOSv router", "Backend": "CML",
                                    "ID": r1[4:]}, {"ip": "", "user": "admin"}, win)
        d.show(); pump(app, 0.4); shot(d, "dialog-properties"); d.close()
        d = QInputDialog(win); d.setWindowTitle("Ping"); d.setLabelText("Ping from R1 to (IP or name):")
        d.setTextValue("8.8.8.8"); d.show(); pump(app, 0.3); shot(d, "dialog-ping"); d.close()
        d = QInputDialog(win); d.setWindowTitle("Rename"); d.setLabelText("New name for R1:")
        d.setTextValue("R1"); d.show(); pump(app, 0.3); shot(d, "dialog-rename"); d.close()
        d = QMessageBox(QMessageBox.Question, "Pull cable", "Simulate unplugging the cable R3 Gi0/2 ⟷ R4 Gi0/0?\n\n"
                        "CML stops the link; both routers see the interface go down.",
                        QMessageBox.Yes | QMessageBox.No, win)
        d.show(); pump(app, 0.3); shot(d, "dialog-pullcable"); d.close()
    elif sc == "cloud":
        win.cloud_timer.stop()
        win._cloud_done(cloud.check_health())
        pump(app, 1.0)
        shot(win, "cloud-up")
        select(win, "INTERNET"); pump(app, 0.6); shot(win, "cloud-up-selected")
        menus["name"] = "menu-cloud"
        win.node_menu(node_by(win, "INTERNET").key, QPoint(0, 0))
        down = cloud.health_from("ts=1000\nstate=stopped\n", ns_exists=False, host_ping=None, installed=True,
                                 now=1001)
        win._cloud_done(down); pump(app, 0.8)
        shot(win, "cloud-down-selected")
        win.scene.clearSelection(); pump(app, 0.4)
        shot(win, "cloud-down")
        win.legend.hide()
        cl = node_by(win, "INTERNET")
        win.view.resetTransform(); win.view.scale(1.6, 1.6); win.view.centerOn(cl.pos().x(), cl.pos().y() + 30)
        pump(app, 0.6); shot(win.view, "cloud-down-zoom")
        # auto-configure dialog
        from labdesk_app.controller import CloudPlan
        from labdesk_app.ui.dialogs import CloudConfigDialog
        conf = cloud.CloudConf()
        rp = cloud.plan_routes(None, "100.64.0.2", [], conf, live_edges={"100.64.0.2"})
        plan = CloudPlan("cml:r", "R1", "cml:c", "INTERNET", "GigabitEthernet0/1", "100.64.0.2", False, conf, ["1"],
                         cloud.live_commands("GigabitEthernet0/1", "100.64.0.2", conf, ["1"]), "BOOTED", True,
                         False, False, False, rp, [], True, needs_wipe=False)
        d = CloudConfigDialog(plan, down, win); d.show(); pump(app, 0.5); shot(d, "dialog-cloud"); d.close()
    win.close()
    pump(app, 0.3)
    ctl.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
