#!/usr/bin/env python3
"""Offscreen QA screenshots of the Switching Lab -> qa/switch-*.png

    QT_QPA_PLATFORM=offscreen .venv/bin/python tools/switch_shots.py            # the real lab (read-only)
    QT_QPA_PLATFORM=offscreen .venv/bin/python tools/switch_shots.py --replay   # fixture replay (no network)
    ... [--only overview,sw2,vlan10,palette,legend] [--prefix failover-] [--dark]

Live mode only reads: it opens "Switching Lab", waits until both switches report spanning tree and
takes the pictures.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "tools"))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import tempfile as _tempfile  # noqa: E402
os.environ["XDG_CONFIG_HOME"] = _tempfile.mkdtemp(prefix="labdesk-qa-")   # L1: never touch ~/.config/LabDesk

from PySide6.QtCore import QSettings  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from ui_shots import QA, NullLauncher, pump, select, wait_for  # noqa: E402


def switches_ready(d) -> bool:
    tv = d.telemetry
    if tv is None:
        return False
    sws = [n for n in tv.nodes.values() if n.switch is not None]
    rts = [n for n in tv.nodes.values() if n.role.value == "router"]
    return bool(sws) and all(n.switch.stp and n.switch.vlans and n.switch.mac_total is not None for n in sws) \
        and all(n.collector_state.value == "ok" for n in rts + sws)


def shot(win, name, widget=None):
    QA.mkdir(exist_ok=True)
    path = QA / f"switch-{name}.png"
    if widget is None:
        img = win.grab()
    elif widget is win.view:
        img = widget.grab()
    else:
        img = win.grab(widget.geometry() if widget.parent() is win else
                       widget.rect().translated(widget.mapTo(win, widget.rect().topLeft())))
    img.save(str(path))
    print("saved", path)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--replay", action="store_true")
    ap.add_argument("--only")
    ap.add_argument("--prefix", default="")
    ap.add_argument("--dark", action="store_true")
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
    QSettings.setPath(QSettings.IniFormat, QSettings.UserScope, str(ROOT / "qa" / ".settings-switch"))
    from labdesk_app.ui.main_window import MainWindow
    if a.replay:
        from switch_replay import poll_until_settled, replay_controller
        ctl = replay_controller()
        poll_until_settled(ctl)
        win = MainWindow(ctl, NullLauncher())
        win.resize(1500, 940)
        win.show()
        win._lab_opened()
    else:
        from labdesk_app.controller import LabController
        ctl = LabController()
        win = MainWindow(ctl, NullLauncher())
        win.resize(1500, 940)
        win.show()
        win.open_lab_title("Switching Lab")
    ok = wait_for(app, win, switches_ready, a.wait if not a.replay else 30)
    print("ready" if ok else "NOT ready (taking pictures anyway)")
    only = set(a.only.split(",")) if a.only else None
    px = a.prefix + ("dark-" if a.dark else "")

    def want(n):
        return only is None or n in only

    win.layers.set("vlan", False)
    pump(app, 1.0)
    win.infobar.hide()
    win.view.fit_all()
    pump(app, 0.5)
    if want("overview"):
        shot(win, px + "overview")
    if want("sw2"):
        select(win, "SW2")
        pump(app, 0.6)
        shot(win, px + "sw2")
        for name, w in (("stp", win.inspector.tab_stp), ("ports", win.inspector.tab_ports),
                        ("vlans", win.inspector.tab_vlans), ("mac", win.inspector.tab_mac)):
            win.inspector.tabs.setCurrentWidget(w)
            pump(app, 0.4)
            shot(win, px + f"sw2-{name}", win.inspector_dock)
        win.inspector.tabs.setCurrentIndex(0)
    if want("sw1"):
        select(win, "SW1")
        pump(app, 0.6)
        shot(win, px + "sw1")
    if want("link"):
        select(win, link=("SW1", "Gi0/2", "SW2", "Gi0/2"))
        pump(app, 0.6)
        shot(win, px + "link-blocked")
    if want("vlan10"):
        win.scene.clearSelection()
        win.layers.set_vlan(10)
        pump(app, 0.8)
        shot(win, px + "vlan10")
        win.layers.set_vlan(20)
        pump(app, 0.8)
        shot(win, px + "vlan20", win.view)
        win.layers.set("vlan", False)
    if want("zoom"):
        win.scene.clearSelection()
        win.legend.hide()
        win.view.resetTransform()
        win.view.scale(1.5, 1.5)
        win.view.centerOn(20, 10)
        pump(app, 0.5)
        shot(win, px + "zoom", win.view)
        win.legend.show()
        win.view.fit_all()
    if want("palette"):
        shot(win, px + "palette", win.palette_list.parentWidget())
    if want("legend"):
        win.legend.full = True
        win.legend.collapsed["Nodes"] = False
        win.legend.rebuild()
        pump(app, 0.4)
        shot(win, px + "legend", win.legend)
    win.close()
    pump(app, 0.5)
    ctl.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
