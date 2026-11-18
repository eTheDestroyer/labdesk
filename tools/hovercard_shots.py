#!/usr/bin/env python3
"""Offscreen QA screenshots of the device hover card and the Credentials dialog -> qa/hover-card/*.png

    QT_QPA_PLATFORM=offscreen .venv/bin/python tools/hovercard_shots.py --theme light|dark

Replay only (no network, no ssh, no lab changes): the Protocol Showcase captures (consoles at '#', so the
CONFIG tier reads R1's running-config -> on-device accounts) and the Switching Lab captures (consoles at '>',
no enable secret -> "needs enable secret").  R1's running-config gets one extra ``username guest password 0 …``
line so the plain-text case is visible.  Settings (XDG_CONFIG_HOME) and device-creds (LABDESK_DEVICE_CREDS)
live in a fresh temp dir: the user's ~/.config is never read or written.
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
_TMP = tempfile.mkdtemp(prefix="labdesk-qa-hover-")
os.environ["XDG_CONFIG_HOME"] = _TMP                                  # isolated settings (L1)
os.environ["LABDESK_DEVICE_CREDS"] = str(Path(_TMP) / "labdesk" / "device-creds")   # isolated creds

from PySide6.QtCore import QRect, QSettings, Qt  # noqa: E402
from PySide6.QtGui import QPixmap  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from ui_shots import NullLauncher, pump, wait_for  # noqa: E402

OUT = ROOT / "qa" / "hover-card"


def save(pm: QPixmap, name: str) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    p = OUT / f"{name}.png"
    pm.save(str(p))
    print("saved", p)


def hover(app, win, label: str) -> bool:
    """Rest the pointer on ``label``'s tile through the real mouse-move path and wait for the card."""
    from PySide6.QtCore import QPointF
    from PySide6.QtGui import QMouseEvent
    item = next(it for it in win.scene.nodes.values() if it.label == label)
    vp = win.view.viewport()
    pos = win.view.mapFromScene(item.mapToScene(item.tile_rect().center()))
    ev = QMouseEvent(QMouseEvent.MouseMove, QPointF(pos), QPointF(vp.mapToGlobal(pos)), Qt.NoButton, Qt.NoButton,
                     Qt.NoModifier)
    QApplication.sendEvent(vp, ev)
    pump(app, 0.7)
    return win.view.hover_card.isVisible()


def card_crop(win, pad: int = 80) -> QPixmap:
    """The canvas around the card (card included: it is a child of the viewport)."""
    vp = win.view.viewport()
    full = vp.grab()
    g = win.view.hover_card.geometry()
    item = win.scene.nodes.get(win.view.hover_card.node_key)
    r = g
    if item is not None:
        tile = win.view.mapFromScene(item.mapToScene(item.tile_rect().center()))
        r = r.united(QRect(tile.x() - 60, tile.y() - 60, 120, 120))
    r = r.adjusted(-pad // 2, -pad // 2, pad // 2, pad // 2).intersected(vp.rect())
    return full.copy(r)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--theme", default="light")
    a = ap.parse_args()
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
    from labdesk_app.probe import creds as C
    assert str(C.creds_path()).startswith(_TMP)
    C.save("R1", {"USERNAME": "admin", "PASSWORD": "Lab-R1-pass", "ENABLE": "cisco"})
    C.save(None, {"USERNAME": "admin", "ENABLE": "cisco"})          # defaults (SW1 / SW2 use them)
    from labdesk_app.ui.main_window import MainWindow
    th = a.theme

    # ---------------------------------------------------------------- Protocol Showcase (accounts read)
    import ui_replay
    from fake_console import FakeConsole
    from showcase_replay import fixture_text
    from test_telemetry_engine import UUID_TO_LABEL
    from labdesk_app.telemetry import IOS_FAST, IOS_SLOW

    def factory(uuid, user):
        r = UUID_TO_LABEL[uuid]
        outs = {c: fixture_text(r, c) or "" for c in IOS_FAST + IOS_SLOW + ["show running-config"]}
        if r == "R1":
            outs["show running-config"] = outs["show running-config"].replace(
                "no aaa new-model", "username guest privilege 1 password 0 Gu3st-pw\nno aaa new-model")
        return FakeConsole(r, "priv", outs)

    ctl = ui_replay.replay_controller()
    ctl.telemetry.transport_factory = factory
    ui_replay.poll_until_settled(ctl)
    import time
    end = time.time() + 40
    while time.time() < end:
        v = ctl.poll()
        r1 = next(n for n in v.nodes if n.label == "R1")
        if r1.tele is not None and r1.tele.accounts is not None:
            break
        time.sleep(0.3)
    ctl.state.lab(ctl.lab_id)["mgmt"][r1.key] = {"ip": "192.168.122.51", "user": "admin"}
    win = MainWindow(ctl, NullLauncher())
    win.resize(1500, 940)
    win.show()
    win._lab_opened()
    wait_for(app, win, lambda d: d.telemetry is not None and bool(d.nodes), 25)
    pump(app, 1.2)
    win.infobar.hide()
    win._notice_shown = True
    win.view.fit_all()
    pump(app, 0.6)
    ok = hover(app, win, "R1")
    print("card visible:", ok)
    save(card_crop(win), f"r1-{th}")
    save(win.grab(), f"r1-window-{th}")
    win.a_show_pw.setChecked(False)
    win._set_show_passwords(False)
    pump(app, 0.3)
    save(card_crop(win), f"r1-masked-{th}")
    win.a_show_pw.setChecked(True)
    win._set_show_passwords(True)
    win.view.hide_hover_card()
    pump(app, 0.2)
    ok = hover(app, win, "R3")
    save(card_crop(win), f"r3-{th}")
    win.view.hide_hover_card()
    # Credentials dialog (R1: own values; open + grab, never exec'd)
    from labdesk_app.ui.dialogs import CredentialsDialog
    dlg = CredentialsDialog("R1", C.load_detail("R1"), win)
    dlg.show()
    pump(app, 0.3)
    save(dlg.grab(), f"dialog-{th}")
    dlg.password.eye.trigger()
    pump(app, 0.2)
    save(dlg.grab(), f"dialog-shown-{th}")
    dlg.defaults_box.setChecked(True)
    pump(app, 0.2)
    save(dlg.grab(), f"dialog-defaults-{th}")
    dlg.reject()
    win.timer.stop()
    win.runner.wait(3000)
    win.close()
    ctl.shutdown()
    pump(app, 0.3)

    # ---------------------------------------------------------------- Switching Lab (no enable, '>')
    import switch_replay
    ctl2 = switch_replay.replay_controller()
    switch_replay.poll_until_settled(ctl2)
    win2 = MainWindow(ctl2, NullLauncher())
    win2.resize(1500, 940)
    win2.show()
    win2._lab_opened()
    wait_for(app, win2, lambda d: d.telemetry is not None and bool(d.nodes), 25)
    pump(app, 1.2)
    win2.infobar.hide()
    win2._notice_shown = True
    win2.view.fit_all()
    pump(app, 0.6)
    for lbl in ("SW2", "PC1"):
        win2.view.hide_hover_card()
        pump(app, 0.1)
        ok = hover(app, win2, lbl)
        print(lbl, "card visible:", ok)
        save(card_crop(win2), f"{lbl.lower()}-{th}")
    win2.timer.stop()
    win2.runner.wait(3000)
    win2.close()
    ctl2.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
