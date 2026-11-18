#!/usr/bin/env python3
"""Offscreen QA screenshots of the 'Subnets on cables' layer -> qa/subnet-*.png

    QT_QPA_PLATFORM=offscreen .venv/bin/python tools/subnet_shots.py [--dark] [--only normal,one-sided,...]

Replay only (no network, no ssh): the Protocol Showcase and Switching Lab captures drive the real
MainWindow.  The one-sided / mismatch / secondary pictures go through the real console-tap path:
the fake CML client serves a scripted console log (the same '|ms|line' format CML 2.10.1 returns),
the node's console is reported "open" (probing pauses), and the engine's tap thread parses it.
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


def shot(win, name, widget=None):
    QA.mkdir(exist_ok=True)
    path = QA / f"subnet-{name}.png"
    img = (widget or win).grab()
    img.save(str(path))
    print("saved", path)


class ScriptedLog:
    """console_log(lab, node_id, ...) for a fake client: a growing '|ms|line' transcript per node."""

    def __init__(self):
        self.logs: dict[str, list[str]] = {}
        self.ms = 4_000_000

    def type(self, node_id: str, *lines: str):
        buf = self.logs.setdefault(node_id, [])
        for ln in lines:
            self.ms += 900
            buf.append(f"|{self.ms:010d}|{ln}")

    def __call__(self, lab_id, node_id, console=0, lines=200):
        return "\n".join(self.logs.get(node_id, [])[-lines:]) + "\n"


def open_console(ctl, label: str, log: ScriptedLog, opened: set):
    """Mark ``label``'s console as open (as the /proc scan would) and prime its tap with a prompt."""
    nk = next(k for k, n in ctl.telemetry.topo.nodes.items() if n.label == label)
    opened.add(ctl.telemetry.console_uuid[nk])
    log.type(nk[4:], f"{label}>")
    return nk[4:]


def palette_dark(app):
    from PySide6.QtGui import QColor, QPalette
    pal = QPalette()
    for role, c in ((QPalette.Window, "#202329"), (QPalette.Base, "#1A1D22"), (QPalette.Text, "#E6EAF0"),
                    (QPalette.WindowText, "#E6EAF0"), (QPalette.Button, "#2A2E35"),
                    (QPalette.ButtonText, "#E6EAF0"), (QPalette.AlternateBase, "#23272D")):
        pal.setColor(role, QColor(c))
    app.setPalette(pal)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dark", action="store_true")
    ap.add_argument("--only")
    a = ap.parse_args()
    app = QApplication(sys.argv[:1])
    if a.dark:
        palette_dark(app)
    QSettings.setDefaultFormat(QSettings.IniFormat)
    QSettings.setPath(QSettings.IniFormat, QSettings.UserScope, str(ROOT / "qa" / ".settings-subnet"))
    from labdesk_app.ui.main_window import MainWindow
    px = "dark-" if a.dark else ""
    only = set(a.only.split(",")) if a.only else None

    def want(n):
        return only is None or n in only

    # ------------------------------------------------------------------ Protocol Showcase
    if want("normal") or want("one-sided") or want("mismatch") or want("zoomed-out"):
        from ui_replay import poll_until_settled, replay_controller
        ctl = replay_controller()
        log = ScriptedLog()
        opened: set[str] = set()
        ctl.client.console_log = log
        ctl.telemetry.console_scan = lambda ex: set(opened)
        poll_until_settled(ctl)
        win = MainWindow(ctl, NullLauncher())
        win.resize(1500, 940)
        win.show()
        win._lab_opened()
        wait_for(app, win, lambda d: d.telemetry is not None and any(
            lv.addressing is not None for lv in d.links), 20)
        win.layers.set("subnets", True)
        win.infobar.hide()
        pump(app, 1.0)
        win.view.fit_all()
        pump(app, 0.6)
        if want("normal"):
            shot(win, px + "normal")
            select(win, link=("R1", "Gi0/2", "R3", "Gi0/0"))
            pump(app, 0.6)
            shot(win, px + "normal-selected")
            win.scene.clearSelection()
        if want("zoomed-out"):
            win.legend.hide()
            win.view.resetTransform()
            win.view.scale(0.5, 0.5)
            win.view.centerOn(0, 0)
            pump(app, 0.6)
            shot(win, px + "zoomed-out", win.view)
            win.view.resetTransform()
            win.view.scale(0.68, 0.68)
            pump(app, 0.6)
            shot(win, px + "zoomed-mid", win.view)
            win.legend.show()
            win.view.fit_all()
            pump(app, 0.4)
        if want("one-sided") or want("mismatch"):
            # R3: somebody has its console open and types (probing of R3 pauses; the tap reads the log)
            nid = open_console(ctl, "R3", log, opened)
            pump(app, 4.0)
            log.type(nid, "R3>enable", "Password: ", "R3#configure terminal",
                     "Enter configuration commands, one per line.  End with CNTL/Z.",
                     "R3(config)#interface GigabitEthernet0/1", "R3(config-if)#no ip address", "R3(config-if)#")
            pump(app, 4.0)
            if want("one-sided"):
                select(win, link=("R2", "Gi0/2", "R3", "Gi0/1"))
                pump(app, 0.8)
                shot(win, px + "one-sided")
            log.type(nid, "R3(config-if)#int gi0/0", "R3(config-if)#ip add 10.0.31.2 255.255.255.252",
                     "R3(config-if)#")
            pump(app, 0.7)
            if want("mismatch"):
                select(win, link=("R1", "Gi0/2", "R3", "Gi0/0"))
                pump(app, 0.5)
                shot(win, px + "mismatch-pulse")
                pump(app, 3.0)
                shot(win, px + "mismatch")
                shot(win, px + "log", win.log_dock if hasattr(win, "log_dock") else None)
        win.close()
        ctl.shutdown()
        pump(app, 0.5)

    # ------------------------------------------------------------------ Switching Lab (router-on-a-stick)
    if want("trunk"):
        from switch_replay import poll_until_settled as sw_settle
        from switch_replay import replay_controller as sw_ctl
        ctl = sw_ctl()
        log = ScriptedLog()
        opened = set()
        ctl.client.console_log = log
        ctl.telemetry.console_scan = lambda ex: set(opened)
        sw_settle(ctl)
        win = MainWindow(ctl, NullLauncher())
        win.resize(1500, 940)
        win.show()
        win._lab_opened()
        wait_for(app, win, lambda d: d.telemetry is not None and any(
            lv.addressing is not None for lv in d.links), 20)
        win.layers.set("vlan", False)
        win.infobar.hide()
        pump(app, 1.0)
        win.view.fit_all()
        pump(app, 0.5)
        select(win, link=("R1", "Gi0/0", "SW1", "Gi0/0"))
        pump(app, 0.6)
        shot(win, px + "trunk")
        nid = open_console(ctl, "R1", log, opened)
        pump(app, 4.0)
        log.type(nid, "R1#conf t", "R1(config)#int gi0/0.10", "R1(config-subif)#ip add 172.16.99.1 255.255.255.0 sec",
                 "R1(config-subif)#")
        pump(app, 3.5)
        shot(win, px + "trunk-secondary")
        sw = next(i for i in win.scene.nodes.values() if i.label == "SW1")
        win.scene.clearSelection()
        sw.setSelected(True)
        pump(app, 0.6)
        shot(win, px + "switch-svi")
        win.close()
        ctl.shutdown()
        pump(app, 0.5)
    return 0


if __name__ == "__main__":
    sys.exit(main())
