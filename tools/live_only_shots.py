#!/usr/bin/env python3
"""Offscreen QA screenshots of the live "PE Hub-Spoke HSRP" lab: canvas + hover cards of HUB, R1 and PE
-> qa/live-only/<tag>-*.png.  Shows that everything on screen comes from live device reads.

    QT_QPA_PLATFORM=offscreen .venv/bin/python tools/live_only_shots.py [--wait 75] [--tag now]

Read-only: show commands only (IOS console probes, Junos SSH 'show ...'); a temporary settings / config dir
holds a COPY of ~/.config/labdesk/state.json, so the real LabDesk files are never written.  device-creds is
only read (first-login backfill is off).
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "tests"), str(ROOT / "tools")]
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
REAL = Path(os.path.expanduser("~/.config/labdesk"))
os.environ.setdefault("LABDESK_DEVICE_CREDS", str(REAL / "device-creds"))      # read only
from PySide6.QtCore import QSettings  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from hovercard_shots import card_crop, hover  # noqa: E402
from ui_shots import NullLauncher, pump, wait_for  # noqa: E402

tmp = Path(tempfile.mkdtemp(prefix="labdesk-live-only-"))
os.environ["XDG_CONFIG_HOME"] = str(tmp)
(tmp / "labdesk").mkdir()
shutil.copy(REAL / "state.json", tmp / "labdesk" / "state.json")
OUT = ROOT / "qa" / "live-only"


def main() -> int:
    wait = int(sys.argv[sys.argv.index("--wait") + 1]) if "--wait" in sys.argv else 75
    tag = sys.argv[sys.argv.index("--tag") + 1] if "--tag" in sys.argv else "live"
    OUT.mkdir(parents=True, exist_ok=True)
    app = QApplication(sys.argv[:1])
    QSettings.setDefaultFormat(QSettings.IniFormat)
    QSettings.setPath(QSettings.IniFormat, QSettings.UserScope, str(tmp / "settings"))
    from labdesk_app.controller import LabController
    from labdesk_app.ui import hovercard_model as H
    from labdesk_app.ui.main_window import MainWindow
    ctl = LabController()
    ctl.first_login_backfill = False
    win = MainWindow(ctl, NullLauncher())
    win._set_show_passwords(False)              # never put a saved password into a screenshot
    win.a_show_pw.setChecked(False)
    win.resize(1600, 1050)
    win.show()
    win.open_lab_title("PE Hub-Spoke HSRP")
    wait_for(app, win, lambda d: d.telemetry is not None and bool(d.nodes), 30)
    pump(app, wait)
    win.infobar.hide()
    win._notice_shown = True
    win.view.fit_all()
    pump(app, 1.5)
    d = win.view_data
    for nv in d.nodes:
        nt = nv.tele
        print(f"{nv.label:9} {nv.state:9} collector={getattr(nt, 'collector_state', None)} "
              f"age={getattr(nt, 'last_probe_age_s', None)} chips={[c.text for c in nv.chips]}")
    for lv in d.links:
        print(f"  link {lv.key[:12]} {lv.label_a}({'live' if lv.live_a else 'cml'})-{lv.label_b}"
              f"({'live' if lv.live_b else 'cml'}) {getattr(lv.status, 'kind', None)} "
              f"subnet={getattr(lv.addressing, 'label', None)} blue={lv.default_hop is not None}")
    win.view.grab().save(str(OUT / f"{tag}-canvas.png"))
    win.grab().save(str(OUT / f"{tag}-window.png"))
    for lbl, f in (("PE", 2.2), ("HUB", 2.2)):            # zoomed: interface pills + subnet labels
        win.view.fit_all()
        pump(app, 0.3)
        win.view.zoom(f)
        win.view.center_on_item(win.scene.nodes[next(n.key for n in d.nodes if n.label == lbl)])
        pump(app, 1.0)
        win.view.grab().save(str(OUT / f"{tag}-zoom-{lbl.lower()}.png"))
    win.view.fit_all()
    pump(app, 0.5)
    for lbl in ("HUB", "R1", "PE"):
        win.view.hide_hover_card()
        pump(app, 0.3)
        nv = next((n for n in d.nodes if n.label == lbl), None)
        if nv is not None:
            print(f"--- {lbl} card ---\n" + H.build_card(nv, creds=None, show_passwords=False).text())
        win.view.fit_all()
        pump(app, 0.3)
        if hover(app, win, lbl):
            card_crop(win).save(str(OUT / f"{tag}-card-{lbl.lower()}.png"))
        else:
            print(lbl, "card not visible")
    print("saved to", OUT)
    ctl.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
