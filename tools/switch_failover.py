#!/usr/bin/env python3
"""Live STP fail-over check on the Switching Lab, watched through the real MainWindow (offscreen).

    QT_QPA_PLATFORM=offscreen .venv/bin/python tools/switch_failover.py

1. waits until SW2 shows Gi0/2 Alternate/BLK (the blocked trunk) in the app
2. 'shutdown' on SW1 Gi0/1 (the trunk facing SW2's root port) over the console
3. waits until the app shows that link ADMIN_DOWN and the SW1 Gi0/2 - SW2 Gi0/2 trunk forwarding
   (no STP block), screenshots qa/switch-failover.png, cross-checks 'show spanning-tree' on SW2,
   saves the CLI as tests/fixtures/switching/after_shut_*.txt
4. 'no shutdown', waits for recovery (Gi0/2 blocking again), screenshots qa/switch-recovered.png
Prints the app's event log lines.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for p in (ROOT, ROOT / "tests", ROOT / "tools", ROOT / "tests" / "fixtures" / "switching" / "_tools"):
    sys.path.insert(0, str(p))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import tempfile as _tempfile  # noqa: E402
os.environ["XDG_CONFIG_HOME"] = _tempfile.mkdtemp(prefix="labdesk-qa-")   # L1: never touch ~/.config/LabDesk

from PySide6.QtCore import QSettings  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from switch_shots import shot, switches_ready  # noqa: E402
from ui_shots import NullLauncher, pump, wait_for  # noqa: E402


def link(d, a, ia, b, ib):
    lab = {n.key: n.label for n in d.nodes}
    for l in d.links:
        if (lab[l.a], l.label_a, lab[l.b], l.label_b) in ((a, ia, b, ib), (b, ib, a, ia)):
            return l
    raise KeyError((a, ia, b, ib))


KINDS = []


def watch(win):
    """record the status kind of the SW1 Gi0/1 - SW2 Gi0/1 link at every poll (event debugging)"""
    d = win.view_data
    if d is None:
        return
    try:
        k = link(d, "SW1", "Gi0/1", "SW2", "Gi0/1").status.kind.value
    except Exception:
        return
    if not KINDS or KINDS[-1][1] != k:
        KINDS.append((time.strftime("%H:%M:%S"), k))


ENGINE = None


def cli(label, cmds=None, cfg=None):
    """Console session that never interleaves with a LabDesk probe burst: it holds the engine's
    per-node console lock (the same lock the bursts take) for the whole session."""
    from console import Con
    lock = None
    if ENGINE is not None and ENGINE.topo is not None:
        nk = next(k for k, n in ENGINE.topo.nodes.items() if n.label == label)
        lock = ENGINE._node_lock(nk)
        lock.acquire()
    try:
        return _cli(label, cmds, cfg)
    finally:
        if lock is not None:
            lock.release()


def _cli(label, cmds=None, cfg=None):
    from console import Con
    c = Con(label, enable=cfg is not None)
    try:
        if cfg:
            c.cfg(cfg)
            return ""
        return {cmd: c.run(cmd) for cmd in cmds}
    finally:
        c.close()


def main() -> int:
    app = QApplication(sys.argv[:1])
    QSettings.setDefaultFormat(QSettings.IniFormat)
    QSettings.setPath(QSettings.IniFormat, QSettings.UserScope, str(ROOT / "qa" / ".settings-switch"))
    from labdesk_app.controller import LabController
    from labdesk_app.ui.main_window import MainWindow
    global ENGINE
    ctl = LabController()
    ENGINE = ctl.telemetry
    win = MainWindow(ctl, NullLauncher())
    win.resize(1500, 940)
    win.show()
    win.open_lab_title("Switching Lab")
    from PySide6.QtCore import QTimer
    tm = QTimer()
    tm.timeout.connect(lambda: watch(win))
    tm.start(500)
    t0 = time.time()

    def blocked_ok(d):
        if not switches_ready(d):
            return False
        lv = link(d, "SW1", "Gi0/2", "SW2", "Gi0/2")
        return lv.tele.stp_block is not None and lv.tele.stp_block.port == "Gi0/2"
    print("baseline:", wait_for(app, win, blocked_ok, 150), f"{time.time() - t0:.0f}s")
    win.infobar.hide()
    win.view.fit_all()
    pump(app, 1)
    print("shutdown SW1 Gi0/1 ...")
    cli("SW1", cfg=["interface GigabitEthernet0/1", "shutdown"])
    t1 = time.time()

    def failed_over(d):
        a = link(d, "SW1", "Gi0/1", "SW2", "Gi0/1")
        b = link(d, "SW1", "Gi0/2", "SW2", "Gi0/2")
        return a.status is not None and a.status.kind.value in ("ADMIN_DOWN", "LINK_DOWN") and \
            b.tele.stp_block is None and b.status.kind.value == "UP"
    ok = wait_for(app, win, failed_over, 120)
    d = win.view_data
    a = link(d, "SW1", "Gi0/1", "SW2", "Gi0/1")
    print(f"app after shut ({time.time() - t1:.0f}s): ok={ok} SW1-SW2 Gi0/1 link = {a.status.kind.value} "
          f"'{a.status.text}'; Gi0/2 link block = {link(d, 'SW1', 'Gi0/2', 'SW2', 'Gi0/2').tele.stp_block}")
    sw2 = next(n for n in d.nodes if n.label == "SW2")
    print("app SW2:", sw2.tele.summary)
    has = lambda kind: any(e.kind == kind for e in ctl.telemetry.events_since(0))  # noqa: E731
    print("LINK DOWN logged:", wait_for(app, win, lambda d: has("LINK DOWN"), 60))
    pump(app, 1)
    win.scene.clearSelection()
    shot(win, "failover")
    for it in win.scene.nodes.values():
        it.setSelected(it.label == "SW2")
    pump(app, 0.6)
    win.inspector.tabs.setCurrentWidget(win.inspector.tab_stp)
    pump(app, 0.5)
    shot(win, "failover-sw2-stp")
    win.inspector.tabs.setCurrentIndex(0)
    win.scene.clearSelection()
    from capture import CMDS
    from showcase_replay import slug
    out = cli("SW2", ["show spanning-tree", "show interfaces trunk", "show interfaces status",
                      "show ip interface brief", "show spanning-tree detail | include ^ VLAN|occurr|from",
                      "show vlan brief"])
    print("CLI SW2 show spanning-tree (after shut):\n" + out["show spanning-tree"])
    fx = ROOT / "tests" / "fixtures" / "switching"
    for cmd, text in out.items():
        (fx / f"after_shut_SW2__{slug(cmd)}.txt").write_text(text)
    out1 = cli("SW1", ["show ip interface brief", "show interfaces status", "show interfaces trunk",
                       "show spanning-tree", "show spanning-tree detail | include ^ VLAN|occurr|from",
                       "show vlan brief"])
    for cmd, text in out1.items():
        (fx / f"after_shut_SW1__{slug(cmd)}.txt").write_text(text)
    _ = CMDS
    print("no shutdown SW1 Gi0/1 ...")
    cli("SW1", cfg=["interface GigabitEthernet0/1", "no shutdown"])
    t2 = time.time()
    ok = wait_for(app, win, lambda d: blocked_ok(d) and link(d, "SW1", "Gi0/1", "SW2", "Gi0/1").status.kind.value
                  == "UP", 150)
    print(f"recovered ({time.time() - t2:.0f}s): {ok}")
    print("LINK UP logged:", wait_for(app, win, lambda d: has("LINK UP"), 60))
    pump(app, 1)
    shot(win, "recovered")
    print("CLI SW2 after no shut:\n" + cli("SW2", ["show spanning-tree | include Gi0"])["show spanning-tree | include Gi0"])
    print("link kinds:", KINDS)
    print("\n--- app event log")
    for e in ctl.telemetry.events_since(0):
        print("  " + e.line)
    win.close()
    pump(app, 0.5)
    ctl.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
