#!/usr/bin/env python3
"""Live first-login check on a running lab (read-only on the devices) -> qa/first-login/*.png

    QT_QPA_PLATFORM=offscreen .venv/bin/python tools/first_login_live.py --lab "PE Hub-Spoke HSRP" \
        --nodes R1,PC-DATA,PE [--wait 240]

* Settings + state.json: a temp dir holding a COPY of ~/.config/labdesk/state.json (the real one is never written).
* device-creds: the REAL ~/.config/labdesk/device-creds (or $LABDESK_DEVICE_CREDS) - this is the backfill: every
  node without an entry gets its first login (mode 0600, existing entries untouched).
* Waits until the probes have logged in and read the running-config ("On the device" accounts) of the first
  probed node, hovers each node and saves the card, plus the console-hint toast.
Values are never printed.
"""
from __future__ import annotations

import argparse
import os
import shutil
import stat
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "tests"), str(ROOT / "tools")]
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
REAL_CFG = Path(os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config"))) / "labdesk"
REAL_CREDS = os.environ.get("LABDESK_DEVICE_CREDS", str(REAL_CFG / "device-creds"))

from PySide6.QtCore import QSettings  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from ui_shots import NullLauncher, pump  # noqa: E402  (sets its own XDG_CONFIG_HOME: override after)
from hovercard_shots import card_crop, hover  # noqa: E402  (sets a temp LABDESK_DEVICE_CREDS: override after)

os.environ["LABDESK_DEVICE_CREDS"] = REAL_CREDS

_TMP = Path(tempfile.mkdtemp(prefix="labdesk-first-login-"))
os.environ["XDG_CONFIG_HOME"] = str(_TMP)
(_TMP / "labdesk").mkdir()
if (REAL_CFG / "state.json").exists():
    shutil.copy(REAL_CFG / "state.json", _TMP / "labdesk" / "state.json")
OUT = ROOT / "qa" / "first-login"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--lab", default="PE Hub-Spoke HSRP")
    ap.add_argument("--nodes", default="R1,PC-DATA,PE")
    ap.add_argument("--wait", type=int, default=240)
    ap.add_argument("--theme", default="light")
    a = ap.parse_args()
    QApplication.setStyle("Fusion")
    app = QApplication(sys.argv[:1])
    QSettings.setDefaultFormat(QSettings.IniFormat)
    QSettings.setPath(QSettings.IniFormat, QSettings.UserScope, str(_TMP / "settings"))
    from PySide6.QtGui import QFont
    from labdesk_app.ui import theme as T
    from labdesk_app.ui import themectl
    T.pin_fonts("Noto Sans", "Noto Sans Mono", 10.0)
    app.setFont(QFont("Noto Sans", 10))
    themectl.install(app, mode=a.theme)
    from labdesk_app.controller import LabController
    from labdesk_app.probe import creds as C
    from labdesk_app.ui.main_window import MainWindow
    cpath = C.creds_path()
    print("device-creds:", cpath, "(exists)" if cpath.exists() else "(new)")
    ctl = LabController()
    win = MainWindow(ctl, NullLauncher())
    win.resize(1500, 1000)
    win.show()
    win.open_lab_title(a.lab)
    end = time.time() + 40
    while time.time() < end and not (win.view_data and win.view_data.nodes):
        pump(app, 0.5)
    print("mode:", oct(stat.S_IMODE(cpath.stat().st_mode)) if cpath.exists() else "missing")
    labels = [n.label for n in win.view_data.nodes]
    for lbl in labels:
        d = C.load_detail(lbl)
        fields = sorted(d.own)
        print(f"  {lbl:10s} entry={'yes' if C.has_entry(lbl) else 'no '} seeded={d.seeded} fields={fields}")
    nodes = [n.strip() for n in a.nodes.split(",") if n.strip()]
    # wait for a probe login + the CONFIG tier (on-device accounts) on the probed nodes
    end = time.time() + a.wait
    probed = [n for n in nodes if any(v.label == n and v.key.startswith("cml:") and v.dtype.vendor == "cisco"
                                      for v in win.view_data.nodes)]
    while time.time() < end:
        pump(app, 2.0)
        vs = {v.label: v for v in win.view_data.nodes}
        ready = [n for n in probed if vs.get(n) is not None and vs[n].tele is not None
                 and getattr(vs[n].tele, "accounts", None) is not None]
        if len(ready) == len(probed):
            break
    vs = {v.label: v for v in win.view_data.nodes}
    for lbl in labels:
        nt = vs[lbl].tele
        if nt is None:
            continue
        acc = getattr(nt, "accounts", None)
        cs = getattr(nt.collector_state, "value", nt.collector_state)
        print(f"  probe {lbl:10s} state={cs} accounts="
              + (", ".join(f"{x.name}:{'encrypted ' + x.scheme if x.encrypted else 'plain'}" for x in acc.accounts)
                 if acc is not None else f"- ({getattr(nt, 'accounts_note', '')})"))
    win.infobar.hide()
    win._notice_shown = True
    win.view.fit_all()
    pump(app, 1.0)
    OUT.mkdir(parents=True, exist_ok=True)
    for lbl in nodes:
        win.view.hide_hover_card()
        key = next(v.key for v in win.view_data.nodes if v.label == lbl)
        fl, tag = win.login_of(key)
        # a password from cml-credentials.env / saved by the user is masked in the PNG (lab defaults are shown)
        show = fl is None or (tag == "default" and fl.source != "cml-credentials.env")
        win.a_show_pw.setChecked(show)
        win._set_show_passwords(show)
        pump(app, 0.2)
        ok = hover(app, win, lbl)
        print(lbl, "card visible:", ok)
        if ok:
            m = getattr(win.view.hover_card, "model", None)
            if m is not None:                     # row names + tags only: never the values
                for sec in ("Login", "On the device"):
                    sc = m.section(sec)
                    if sc is not None:
                        print(f"    [{sec}]", [f"{r.text}{' [' + r.badge + ']' if r.badge else ''}" for r in sc.rows],
                              sc.note[:90])
            p = OUT / f"{lbl.lower()}-{a.theme}.png"
            card_crop(win).save(str(p))
            print("saved", p)
    win.view.hide_hover_card()
    win.a_show_pw.setChecked(True)
    win._set_show_passwords(True)
    # console hint (toast + event log) as when a console opens for the first time
    key = next(v.key for v in win.view_data.nodes if v.label == nodes[0])
    win._first_console_hint(key)
    pump(app, 0.6)
    p = OUT / f"console-hint-{a.theme}.png"
    win.grab().save(str(p))
    print("saved", p)
    win.timer.stop()
    win.runner.wait(3000)
    ctl.shutdown()
    win.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
