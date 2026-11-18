#!/usr/bin/env python3
"""Offscreen QA screenshots of EtherChannel (LACP Po1 + PAgP Po2 in the "Switching Lab") -> qa/ec-*.png

    QT_QPA_PLATFORM=offscreen .venv/bin/python tools/ec_shots.py --theme light|dark [--size 1920x1080]
                                                                  [--only overview,selected,...] [--live]

Replays the real captures in tests/fixtures/etherchannel (healthy -> partial -> fail_on) through the real
TelemetryEngine and MainWindow (no network).  ``--live`` reads the running lab instead (read-only).
Shots (``ec-<theme>-<w>-<name>.png``): overview, zoom, selected (bundle view), sw2-tab (switch EtherChannel
tab), partial, partial-zoom, mismatch, mismatch-selected, dialog, dialog-warn, legend.
Settings go to a temp XDG_CONFIG_HOME (never the user's).
"""
from __future__ import annotations

import argparse
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "tools"))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
_TMP = tempfile.mkdtemp(prefix="labdesk-qa-")
os.environ["XDG_CONFIG_HOME"] = _TMP                     # L1: never touch ~/.config/LabDesk

from PySide6.QtCore import QSettings, Qt  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from ui_shots import QA, NullLauncher, pump, select, wait_for  # noqa: E402

PX = ""
ONLY: set | None = None


def want(name: str) -> bool:
    return ONLY is None or name in ONLY


def shot(widget, name: str) -> None:
    QA.mkdir(exist_ok=True)
    p = QA / f"ec-{PX}{name}.png"
    widget.grab().save(str(p))
    print("saved", p, flush=True)


def bundles(d):
    tv = d.telemetry
    return list(tv.bundles.values()) if tv is not None else []


def by_po(d, po):
    return next((b for b in bundles(d) if b.po_text.split("/")[0] == po), None)


def kick(ctl):
    eng = ctl.telemetry
    with eng._lock:
        for s in eng.sched.values():
            s.next_fast = s.next_slow = 0.0


def zoom_on_bundles(win, app, scale=1.55):
    win.scene.clearSelection()
    win.view.resetTransform()
    win.view.scale(scale, scale)
    its = list(win.scene.bundles.values())
    if its:
        c = sum((it.geom()[0] for it in its[1:]), its[0].geom()[0]) / len(its)
        win.view.centerOn(c)
    pump(app, 0.6)


def main() -> int:
    global PX, ONLY
    ap = argparse.ArgumentParser()
    ap.add_argument("--theme", default="light")
    ap.add_argument("--size", default="1920x1080")
    ap.add_argument("--only")
    ap.add_argument("--live", action="store_true")
    a = ap.parse_args()
    ONLY = set(a.only.split(",")) if a.only else None
    w, h = (int(x) for x in a.size.split("x"))
    QApplication.setStyle("Fusion")
    app = QApplication(sys.argv[:1])
    QSettings.setDefaultFormat(QSettings.IniFormat)
    QSettings.setPath(QSettings.IniFormat, QSettings.UserScope, _TMP)
    from PySide6.QtGui import QFont
    from labdesk_app.ui import theme as T
    from labdesk_app.ui import themectl
    T.pin_fonts("Noto Sans", "Noto Sans Mono", 10.0)
    app.setFont(QFont("Noto Sans", 10))
    themectl.install(app, mode=a.theme)
    PX = f"{'live-' if a.live else ''}{a.theme}-{w}-"
    import faulthandler
    faulthandler.dump_traceback_later(400, exit=True)
    from labdesk_app.ui.main_window import MainWindow
    box = None
    import ec_replay as R
    if a.live:
        from labdesk_app.controller import LabController
        ctl = LabController()
        win = MainWindow(ctl, NullLauncher())
        win.resize(w, h)
        win.show()
        win.open_lab_title("Switching Lab")
    else:
        import ec_replay as R
        ctl, box = R.replay_controller("healthy")
        R.poll_until_settled(ctl)
        win = MainWindow(ctl, NullLauncher())
        win.resize(w, h)
        win.show()
        win._lab_opened()
    ok = wait_for(app, win, lambda d: len([b for b in bundles(d) if b.state.value == "up"]) == 2 and
                  all(s.modes for b in bundles(d) for s in (b.side_a, b.side_b) if s), 150 if a.live else 40)
    print("ready" if ok else "NOT ready (taking pictures anyway)", flush=True)
    pump(app, 1.0)
    win.infobar.hide()
    win._notice_shown = True
    win.layers.set("vlan", False)
    win.view.fit_all()
    pump(app, 0.8)
    if want("overview"):
        shot(win, "overview")
    if want("zoom"):
        zoom_on_bundles(win, app)
        shot(win.view, "zoom")
        win.view.fit_all()
        pump(app, 0.4)
    if want("selected"):
        b2 = by_po(win.view_data, "Po2")
        if b2 is not None:
            win.select_bundle(b2.key)
            pump(app, 1.0)
            shot(win, "selected")
            win.scene.clearSelection()
    if want("sw2-tab"):
        select(win, "SW2")
        pump(app, 0.6)
        win.inspector.tabs.setCurrentWidget(win.inspector.tab_ec)
        pump(app, 0.6)
        shot(win, "sw2-tab")
        win.inspector.tabs.setCurrentIndex(0)
        win.scene.clearSelection()
    if want("legend"):
        win.legend.full = True
        win.legend.rebuild()
        pump(app, 0.5)
        shot(win.legend, "legend")
    if box is not None and (want("partial") or want("partial-zoom")):
        box.state = "partial"
        kick(ctl)
        wait_for(app, win, lambda d: (by_po(d, "Po1") is not None and by_po(d, "Po1").state.value == "partial"), 40)
        pump(app, 1.0)
        win.view.fit_all()
        pump(app, 0.4)
        shot(win, "partial")
        zoom_on_bundles(win, app)
        shot(win.view, "partial-zoom")
        win.view.fit_all()
    if box is not None and (want("mismatch") or want("mismatch-selected")):
        box.state = "fail_on"
        kick(ctl)
        wait_for(app, win, lambda d: (by_po(d, "Po1") is not None and by_po(d, "Po1").state.value == "mismatch"), 40)
        pump(app, 1.0)
        win.view.fit_all()
        pump(app, 0.4)
        shot(win, "mismatch")
        b1 = by_po(win.view_data, "Po1")
        if b1 is not None:
            win.select_bundle(b1.key)
            pump(app, 1.0)
            shot(win, "mismatch-selected")
            win.scene.clearSelection()
    if box is not None and (want("dialog") or want("dialog-warn")):
        box.state = "healthy"
        kick(ctl)
        wait_for(app, win, lambda d: len([b for b in bundles(d) if b.state.value == "up"]) == 2, 40)
        # pretend Po2 was unbundled (facts + day-0), then plan the PAgP bundle the way the dialog does
        from labdesk_app import etherchannel as EC
        eng = ctl.telemetry
        with eng._lock:
            for nk, nf in eng.facts.items():
                if nf.ec is not None and nf.ec.summary is not None:
                    nf.ec.summary.value["groups"] = [g for g in nf.ec.summary.value["groups"] if g["group"] != 2]
                if nf.ifaces is not None:
                    nf.ifaces.value.pop("port-channel2", None)
        for lbl in ("SW1", "SW2"):
            nid = R.LABEL_KEY[lbl][4:]
            ctl.client.day0_override[lbl] = EC.unpatch_day0(ctl.client.node_configuration(ctl.lab_id, nid), 2)
        topo = eng.topo
        keys = [lk for lk, l in topo.links.items() if "1/0" in l.iface_a or "1/1" in l.iface_a]
        plan = ctl.bundle_plan(sorted(keys), "lacp")
        from labdesk_app.ui.dialogs import BundleDialog
        dlg = BundleDialog(plan, win)
        dlg.resize(760, 860 if h > 800 else 700)
        dlg.show()
        pump(app, 0.5)
        pagp = next(b for b in dlg.proto_group.buttons() if b.property("proto") == "pagp")
        pagp.setChecked(True)
        dlg.cb_lb.setChecked(True)
        pump(app, 0.5)
        if want("dialog"):
            shot(dlg, "dialog")
        dlg.mode_a.setCurrentIndex(1)            # auto / auto: never forms
        dlg.mode_b.setCurrentIndex(1)
        pump(app, 0.4)
        if want("dialog-warn"):
            shot(dlg, "dialog-warn")
        dlg.reject()
    win.close()
    pump(app, 0.5)
    ctl.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
