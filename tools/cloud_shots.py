#!/usr/bin/env python3
"""Offscreen QA screenshots of the Internet cloud on the live "Switching Lab" -> qa/cloud-*.png

    QT_QPA_PLATFORM=offscreen .venv/bin/python tools/cloud_shots.py [--dark] [--verify] [--only a,b]

Read-only except --verify (runs Verify Internet from R1: two pings on its console).  The "down" pictures
inject a stopped CloudHealth into the window (the real service is not touched).
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import tempfile as _tempfile  # noqa: E402
os.environ["XDG_CONFIG_HOME"] = _tempfile.mkdtemp(prefix="labdesk-qa-")   # L1: never touch ~/.config/LabDesk

from PySide6.QtCore import QSettings  # noqa: E402
from PySide6.QtWidgets import QAbstractItemView, QApplication  # noqa: E402

from ui_shots import QA, NullLauncher, pump, select, wait_for  # noqa: E402


def shot(win, name, widget=None):
    QA.mkdir(exist_ok=True)
    path = QA / f"cloud-{name}.png"
    if widget is None:
        img = win.grab()
    elif widget is win.view or widget.window() is not win:
        img = widget.grab()
    else:
        img = win.grab(widget.geometry() if widget.parent() is win else
                       widget.rect().translated(widget.mapTo(win, widget.rect().topLeft())))
    img.save(str(path))
    print("saved", path)


def ready(d) -> bool:
    tv = d.telemetry
    if tv is None:
        return False
    r1 = next((n for n in d.nodes if n.label == "R1"), None)
    return r1 is not None and r1.tele is not None and r1.tele.default_path is not None and \
        r1.tele.default_path.state.value in ("EGRESS", "VERIFIED_INTERNET") and \
        any(l.default_hop is not None for l in d.links)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dark", action="store_true")
    ap.add_argument("--verify", action="store_true")
    ap.add_argument("--only")
    ap.add_argument("--wait", type=float, default=150)
    a = ap.parse_args()
    app = QApplication(sys.argv[:1])
    if a.dark:
        from PySide6.QtGui import QColor, QPalette
        pal = QPalette()
        for role, c in ((QPalette.Window, "#202329"), (QPalette.Base, "#1A1D22"), (QPalette.Text, "#E6EAF0"),
                        (QPalette.WindowText, "#E6EAF0"), (QPalette.Button, "#2A2E35"),
                        (QPalette.ButtonText, "#E6EAF0"), (QPalette.AlternateBase, "#23272D")):
            pal.setColor(role, QColor(c))
        app.setPalette(pal)
    QSettings.setDefaultFormat(QSettings.IniFormat)
    QSettings.setPath(QSettings.IniFormat, QSettings.UserScope, str(ROOT / "qa" / ".settings-cloud"))
    from labdesk_app import cloud
    from labdesk_app.controller import LabController
    from labdesk_app.ui.dialogs import CloudConfigDialog
    from labdesk_app.ui.main_window import MainWindow
    ctl = LabController()
    win = MainWindow(ctl, NullLauncher())
    win.resize(1500, 940)
    win.show()
    win.open_lab_title("Switching Lab")
    ok = wait_for(app, win, ready, a.wait)
    end = time.time() + 20
    while win.cloud_health is None and time.time() < end:
        pump(app, 0.5)
    print("ready" if ok else "NOT ready (taking pictures anyway)", "cloud:",
          win.cloud_health.headline if win.cloud_health else None)
    only = set(a.only.split(",")) if a.only else None
    px = "dark-" if a.dark else ""

    def want(n):
        return only is None or n in only

    win.infobar.hide()
    win.view.fit_all()
    pump(app, 0.8)
    if a.verify:
        r1 = next(it for it in win.scene.nodes.values() if it.label == "R1")
        win.verify_internet(r1.key)
        end = time.time() + 60
        while time.time() < end:
            pump(app, 0.5)
            d = win.view_data
            r = next((n for n in d.nodes if n.label == "R1"), None) if d else None
            if r is not None and r.tele and r.tele.default_path and \
                    r.tele.default_path.state.value == "VERIFIED_INTERNET":
                break
        pump(app, 3.0)          # let the packet animation finish
    if want("overview"):
        shot(win, px + "overview")
    if want("node"):
        select(win, "INTERNET")
        pump(app, 0.8)
        shot(win, px + "node")
    if want("router"):
        select(win, "R1")
        pump(app, 0.8)
        shot(win, px + "router", win.inspector_dock)
    if want("link"):
        select(win, link=("R1", "Gi0/1", "INTERNET", "p"))
        pump(app, 0.8)
        shot(win, px + "link")
    if want("zoom"):
        win.scene.clearSelection()
        win.legend.hide()
        r1 = next(it for it in win.scene.nodes.values() if it.label == "R1")
        cl = next(it for it in win.scene.nodes.values() if it.is_internet)
        win.view.resetTransform()
        win.view.scale(1.6, 1.6)
        c = (r1.pos() + cl.pos()) / 2
        win.view.centerOn(c.x(), c.y() + 30)
        pump(app, 0.6)
        shot(win, px + "zoom", win.view)
        win.legend.show()
        win.view.fit_all()
    if want("palette"):
        pal = win.palette_list
        for i in range(pal.count()):
            if pal.item(i).text().startswith("CONNECTORS"):
                pal.scrollToItem(pal.item(i), QAbstractItemView.PositionAtTop)
        pump(app, 0.3)
        shot(win, px + "palette", win.palette_list.parentWidget())
    if want("dialog"):
        r1 = next(it for it in win.scene.nodes.values() if it.label == "R1")
        plan = ctl.cloud_plan(r1.key)
        dlg = CloudConfigDialog(plan, win.cloud_health, win)
        dlg.show()
        pump(app, 0.6)
        shot(win, px + "dialog", dlg)
        dlg.close()
    if want("down"):
        win.cloud_timer.stop()
        win._cloud_done(cloud.health_from(f"ts={time.time():.0f}\nstate=stopped\n", ns_exists=False, host_ping=None,
                                          installed=True))
        select(win, "INTERNET")
        pump(app, 0.8)
        shot(win, px + "down")
        win.scene.clearSelection()
        win.legend.hide()
        cl = next(it for it in win.scene.nodes.values() if it.is_internet)
        win.view.resetTransform()
        win.view.scale(1.6, 1.6)
        win.view.centerOn(cl.pos().x(), cl.pos().y() + 30)
        pump(app, 0.6)
        shot(win, px + "down-zoom", win.view)
    win.close()
    pump(app, 0.5)
    ctl.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
