#!/usr/bin/env python3
"""Offscreen QA screenshots of the Protocol Showcase lab -> qa/proto-*.png

    QT_QPA_PLATFORM=offscreen .venv/bin/python tools/ui_shots.py            # replay of the live captures
    QT_QPA_PLATFORM=offscreen .venv/bin/python tools/ui_shots.py --live     # the real lab (read-only)

Replay mode drives the real MainWindow with the fixture-fed controller from tests/ui_replay.py
(no network, no ssh) and also exercises the simulations (cable pull, conditioning, packets).
Live mode only reads: it opens "Protocol Showcase", waits for the probes and takes pictures.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import tempfile as _tempfile  # noqa: E402
os.environ["XDG_CONFIG_HOME"] = _tempfile.mkdtemp(prefix="labdesk-qa-")   # L1: never touch ~/.config/LabDesk

from PySide6.QtCore import QEventLoop, QSettings  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

QA = ROOT / "qa"


class NullLauncher:
    def open(self, cmd):
        return "not opened (QA)"


def pump(app, secs):
    end = time.time() + secs
    while time.time() < end:
        app.processEvents(QEventLoop.AllEvents, 30)
        time.sleep(0.01)


def wait_for(app, win, pred, timeout):
    end = time.time() + timeout
    while time.time() < end:
        pump(app, 0.5)
        if win.view_data is not None and pred(win.view_data):
            return True
    return False


def select(win, label=None, link=None):
    win.scene.clearSelection()
    if label:
        for it in win.scene.nodes.values():
            if it.label == label:
                it.setSelected(True)
    if link:
        a, ia, b, ib = link
        for li in win.scene.links.values():
            if ((li.a.label, li.label_a, li.b.label, li.label_b) == (a, ia, b, ib)
                    or (li.b.label, li.label_b, li.a.label, li.label_a) == (a, ia, b, ib)):
                li.setSelected(True)


def shot(win, name, widget=None):
    QA.mkdir(exist_ok=True)
    path = QA / f"proto-{name}.png"
    if widget is None or widget is win:
        img = win.grab()
    elif widget.window() is win and widget is not win.view:
        img = win.grab(widget.geometry() if widget.parent() is win else
                       widget.rect().translated(widget.mapTo(win, widget.rect().topLeft())))
    else:
        img = widget.grab()
    img.save(str(path))
    print("saved", path)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--only", help="comma list of shot names")
    ap.add_argument("--dark", action="store_true")
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
    tmp_settings = ROOT / "qa" / ".settings"
    QSettings.setPath(QSettings.IniFormat, QSettings.UserScope, str(tmp_settings))
    from labdesk_app.ui.main_window import MainWindow
    if a.live:
        from labdesk_app.controller import LabController
        ctl = LabController()
        win = MainWindow(ctl, NullLauncher())
        win.resize(1500, 940)
        win.show()
        win.open_lab_title("Protocol Showcase")
        from ui_replay import settled
        ok = wait_for(app, win, settled, 150)
        print("settled" if ok else "NOT settled (taking pictures anyway)")
        prefix = "live-"
    else:
        from ui_replay import poll_until_settled, replay_controller
        ctl = replay_controller()
        poll_until_settled(ctl)               # warm the probes before the window polls
        win = MainWindow(ctl, NullLauncher())
        win.resize(1500, 940)
        win.show()
        win._lab_opened()
        wait_for(app, win, lambda d: d.telemetry is not None and bool(d.nodes), 20)
        prefix = ""
    only = set(a.only.split(",")) if a.only else None
    if a.dark:
        prefix += "dark-"

    def want(n):
        return only is None or n in only

    pump(app, 1.0)
    win.infobar.hide()
    win._notice_shown = True
    win.view.fit_all()
    pump(app, 0.4)
    if want("overview"):
        shot(win, prefix + "overview")
    if want("canvas"):
        shot(win, prefix + "canvas", win.view)
    if want("node-r1"):
        select(win, "R1")
        pump(app, 0.4)
        shot(win, prefix + "node-r1")
    if want("protocols-r3"):
        select(win, "R3")
        pump(app, 0.2)
        win.inspector.tabs.setCurrentIndex(3)
        pump(app, 0.3)
        shot(win, prefix + "protocols-r3", win.inspector_dock)
        win.inspector.tabs.setCurrentIndex(1)
        pump(app, 0.3)
        shot(win, prefix + "interfaces-r3", win.inspector_dock)
        win.inspector.tabs.setCurrentIndex(2)
        pump(app, 0.3)
        shot(win, prefix + "routing-r3", win.inspector_dock)
        win.inspector.tabs.setCurrentIndex(0)
    if want("link-p2p"):
        select(win, link=("R1", "Gi0/2", "R3", "Gi0/0"))
        pump(app, 0.4)
        shot(win, prefix + "link-p2p")
    if want("link-down"):
        select(win, link=("R2", "Gi0/4", "R4", "Gi0/2"))
        pump(app, 0.4)
        shot(win, prefix + "link-down")
    if want("zoom"):
        win.scene.clearSelection()
        win.view.resetTransform()
        win.view.scale(1.35, 1.35)
        win.view.centerOn(0, -60)
        pump(app, 0.4)
        shot(win, prefix + "zoom", win.view)
        win.view.fit_all()
    if want("layers"):
        for k in ("ospf", "bgp", "traffic"):
            win.layers.set(k, True)
        pump(app, 4.0 if not a.live else 8.0)
        win.poll()
        pump(app, 1.0)
        shot(win, prefix + "layers")
        for k in ("ospf", "bgp", "traffic"):
            win.layers.set(k, False)
    if want("packet"):
        r2 = next(n.key for n in win.view_data.nodes if n.label == "R2")
        win.animate_default_path(r2)
        pump(app, 1.35)
        shot(win, prefix + "packet", win.view)
        pump(app, 3.0)
    if not a.live and want("cable"):
        # simulate: pull the R3-R4 cable -> every default path breaks there (red ✕), LINK shows cable pulled
        tele = ctl.telemetry
        lk = next(l.key for l in win.view_data.links if {win._label(l.a), win._label(l.b)} == {"R3", "R4"})
        tele.pull_cable(lk)
        r1 = next(n.key for n in win.view_data.nodes if n.label == "R1")
        tele.set_condition(next(l.key for l in win.view_data.links
                                if {win._label(l.a), win._label(l.b)} == {"R1", "R3"}), preset="Satellite 600 ms")
        win.poll()
        pump(app, 1.5)
        select(win, link=("R3", "Gi0/2", "R4", "Gi0/0"))
        pump(app, 0.5)
        shot(win, prefix + "cable-pulled")
        win.scene.clearSelection()
        win.animate_default_path(r1)
        pump(app, 1.6)
        shot(win, prefix + "packet-dropped", win.view)
        tele.restore_cable(lk)
        win.poll()
        pump(app, 2.0)
    if not a.live and want("vendors"):
        # host-KVM routers next to the CML ones (replay only: virsh is faked, nothing is created)
        from labdesk_app import kvm
        kvm.domstate = lambda name: "running"
        lab = ctl.state.lab(ctl.lab_id)
        keys = {n.label: n.key for n in win.view_data.nodes}
        lab["kvm"]["JR1"] = {"kind": "vjunos", "x": -560, "y": -160}
        lab["kvm"]["FGT1"] = {"kind": "fortios", "x": 560, "y": -160}
        lab["kvm_links"] += [{"a": "kvm:JR1", "b": keys["R1"], "label_a": "ge-0/0/0", "label_b": "Gi0/5"},
                             {"a": "kvm:FGT1", "b": keys["R2"], "label_a": "port2", "label_b": "Gi0/5"}]
        ctl._force_full = True
        win.poll()
        pump(app, 2.0)
        win.view.fit_all()
        pump(app, 0.5)
        shot(win, prefix + "vendors")
        select(win, "JR1")
        pump(app, 0.5)
        shot(win, prefix + "vendors-junos")
    if want("legend"):
        shot(win, prefix + "legend", win.legend)
    win.close()
    pump(app, 0.5)
    ctl.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
