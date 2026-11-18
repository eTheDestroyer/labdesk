#!/usr/bin/env python3
"""Live EtherChannel check on the running "Switching Lab" (needs CML; every change is reverted).

    QT_QPA_PLATFORM=offscreen .venv/bin/python tools/ec_live_check.py [--steps base,shut,mismatch,pull,flow,final]

Drives the real MainWindow (offscreen) against the real controller.  Changes are made the way a user makes them:
on the switch console (ssh ... connect <uuid>, taken under the engine's per-node lock so it never interrupts a
probe burst; the engine sees the console as "in use" meanwhile), or through the app's own paths (cable pull via the
CML API, the "Bundle into EtherChannel…" dialog and "Unbundle…").  Each step is cross-checked with the CLI.

  base      the app shows Po1 LACP 2/2 + Po2 PAgP 2/2 and the blocked Po (CLI: etherchannel summary, STP)
  shut      SW1 Gi0/2 shutdown -> app '1/2' + member down; no shutdown -> 2/2
  mismatch  SW2 Po1 members 'mode on' -> ⚠ 'on ↔ LACP active — never bundles'; back to passive
  pull      both Po1 cables stopped via the CML API -> STP fails over to Po2 (BLK moves); restored
  flow      'Unbundle…' Po2, then 'Bundle into EtherChannel…' PAgP (SW1 desirable / SW2 auto) via the dialog
  final     CLI summary / lacp / pagp / STP / trunk + PC1 pings PC2 and 8.8.8.8
Screenshots: qa/ec-live-*.png.  Log: qa/ec_live_log.txt.
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
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "tests" / "fixtures" / "switching" / "_tools"))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
_TMP = tempfile.mkdtemp(prefix="labdesk-qa-")
os.environ["XDG_CONFIG_HOME"] = _TMP                     # L1: never touch ~/.config/LabDesk / labdesk state

from PySide6.QtCore import QSettings  # noqa: E402
from PySide6.QtWidgets import QApplication, QDialog  # noqa: E402

from ui_shots import QA, NullLauncher, pump  # noqa: E402

OUT: list[str] = []
APP = None
WIN = None


def say(msg: str) -> None:
    line = time.strftime("%H:%M:%S ") + msg
    print(line, flush=True)
    OUT.append(line)


def shot(widget, name: str) -> None:
    p = QA / f"ec-live-{name}.png"
    widget.grab().save(str(p))
    say(f"  screenshot {p.name}")


def bundles():
    d = WIN.view_data
    tv = d.telemetry if d is not None else None
    return list(tv.bundles.values()) if tv is not None else []


def by_ports(ports: tuple[str, ...]):
    """The bundle whose SW1 members are ``ports`` (e.g. ('Gi0/1', 'Gi0/2'))."""
    for b in bundles():
        for s in (b.side_a, b.side_b):
            if s is not None and s.label == "SW1" and tuple(m.port for m in s.members) == ports:
                return b
    return None


def po1():
    return by_ports(("Gi0/1", "Gi0/2"))


def po2():
    return by_ports(("Gi1/0", "Gi1/1"))


def wait(pred, timeout: float, what: str) -> float | None:
    t0 = time.time()
    while time.time() - t0 < timeout:
        pump(APP, 0.5)
        try:
            if pred():
                dt = time.time() - t0
                say(f"  OK after {dt:.0f} s: {what}")
                return dt
        except Exception:  # noqa: BLE001
            pass
    say(f"  TIMEOUT ({timeout:.0f} s): {what}")
    return None


def node_key(label: str) -> str:
    return next(n.key for n in WIN.view_data.nodes if n.label == label)


def cli(label: str, cmds: list[str], cfg: list[str] | None = None) -> dict:
    """Run show commands (and optionally config lines) on a switch console, under the engine's per-node lock."""
    from console import Con
    eng = WIN.ctl.telemetry
    lock = eng._node_lock(node_key(label))
    out = {}
    if not lock.acquire(timeout=60):
        raise RuntimeError(f"{label}: probe lock busy")
    try:
        c = Con(label, enable=True)
        try:
            if cfg:
                c.p.sendline("configure terminal")
                c.p.expect(rb"\(config\)#")
                for ln in cfg:
                    c.p.sendline(ln)
                    c.p.expect(rb"\)#\s*$", timeout=30)
                    err = c.p.before.decode("utf-8", "replace")
                    if "%" in err.split("\n", 1)[-1]:
                        say(f"  {label} IOS: {ln!r} -> {err.strip().splitlines()[-1]}")
                c.p.sendline("end")
                c.p.expect(rb"#\s*$")
                say(f"  {label} (console): " + " / ".join(cfg))
            for cmd in cmds:
                out[cmd] = c.run(cmd, timeout=90)
        finally:
            c.close()
    finally:
        lock.release()
    with eng._lock:                      # the console closed: the engine probes the node at once
        eng._kick([node_key(label)])
    return out


def show(label: str, cmds: list[str], grep: tuple[str, ...] = ()) -> None:
    res = cli(label, cmds)
    for cmd, text in res.items():
        lines = [ln for ln in text.splitlines() if ln.strip() and (not grep or any(g in ln for g in grep))]
        say(f"  CLI {label}# {cmd}")
        for ln in lines[-14:]:
            say(f"      {ln.rstrip()}")


def state_line() -> str:
    parts = []
    for b in sorted(bundles(), key=lambda x: x.po_text):
        blk = f", STP {b.stp_block.port} {b.stp_block.role.value} BLK on {WIN._label(b.stp_block.node)}" \
            if b.stp_block else ""
        parts.append(f"{b.label} [{b.state.value}]" + (f" ⚠ {b.mismatch_short}" if b.mismatch_short else "") + blk)
    return " | ".join(parts) or "no bundles"


def zoom(win, scale=1.45):
    win.scene.clearSelection()
    win.view.resetTransform()
    win.view.scale(scale, scale)
    its = list(win.scene.bundles.values())
    if its:
        c = sum((it.geom()[0] for it in its[1:]), its[0].geom()[0]) / len(its)
        win.view.centerOn(c)
    pump(APP, 0.6)


def step_base():
    say("== base: the app shows both bundles and the blocked Port-channel")
    wait(lambda: po1() and po2() and po1().state.value == "up" and po2().state.value == "up" and
         all(s.modes for b in (po1(), po2()) for s in (b.side_a, b.side_b)), 240, "Po1 LACP 2/2 + Po2 PAgP 2/2 up")
    wait(lambda: any(b.stp_block for b in bundles()), 60, "a whole Port-channel blocked by STP")
    say("  app: " + state_line())
    WIN.view.fit_all()
    pump(APP, 0.6)
    shot(WIN, "overview")
    zoom(WIN)
    shot(WIN.view, "zoom")
    WIN.view.fit_all()
    show("SW1", ["show etherchannel summary"], ("Po1", "Po2"))
    show("SW2", ["show etherchannel summary", "show spanning-tree vlan 10", "show lacp neighbor", "show pagp neighbor"],
         ("Po1", "Po2", "Gi0/", "Gi1/"))


def step_shut():
    say("== shut: SW1 Gi0/2 shutdown (one Po1 member)")
    cli("SW1", [], ["interface GigabitEthernet0/2", "shutdown"])
    wait(lambda: po1() and po1().bundled == 1 and po1().state.value in ("partial", "mismatch"), 120,
         "app shows Po1 1/2 (partial)")
    say("  app: " + state_line())
    lk = [lk for lk in po1().link_keys if WIN.scene.links[lk].label_a == "Gi0/2" or
          WIN.scene.links[lk].label_b == "Gi0/2"]
    lv = next(l for l in WIN.view_data.links if l.key == lk[0])
    say(f"  app: member Gi0/2 cable status {lv.status.kind.value} '{lv.status.text}'")
    zoom(WIN)
    shot(WIN.view, "partial")
    WIN.view.fit_all()
    show("SW1", ["show etherchannel summary"], ("Po1",))
    say("== shut: no shutdown")
    cli("SW1", [], ["interface GigabitEthernet0/2", "no shutdown"])
    wait(lambda: po1() and po1().state.value == "up" and po1().bundled == 2, 180, "app shows Po1 2/2 again")
    say("  app: " + state_line())
    show("SW1", ["show etherchannel summary"], ("Po1",))


def step_mismatch():
    say("== mismatch: SW2 Po1 members -> channel-group 1 mode on (SW1 stays LACP active)")
    cli("SW2", [], ["interface range GigabitEthernet0/1 - 2", "no channel-group", "channel-group 1 mode on"])
    wait(lambda: po1() and po1().state.value == "mismatch" and "on ↔ LACP" in (po1().mismatch_short or ""), 180,
         "app shows ⚠ on ↔ LACP active — never bundles")
    say("  app: " + state_line())
    if po1() is not None:
        say(f"  app reason: {po1().mismatch_short} | {po1().mismatch[0] if po1().mismatch else ''}")
        WIN.select_bundle(po1().key)
        pump(APP, 1.0)
        shot(WIN, "mismatch")
        WIN.scene.clearSelection()
    show("SW1", ["show etherchannel summary"], ("Po1",))
    show("SW2", ["show etherchannel summary"], ("Po1",))
    say("== mismatch: revert SW2 to LACP passive")
    cli("SW2", [], ["interface range GigabitEthernet0/1 - 2", "no channel-group", "channel-group 1 mode passive"])
    wait(lambda: po1() and po1().state.value == "up" and po1().bundled == 2 and not po1().mismatch_short, 240,
         "app shows Po1 LACP 2/2 again, mismatch cleared")
    say("  app: " + state_line())
    show("SW2", ["show etherchannel summary"], ("Po1",))


def step_pull():
    say("== pull: stop both Po1 cables through the CML API")
    keys = list(po1().link_keys)
    eng = WIN.ctl.telemetry
    for lk in keys:
        r = eng.pull_cable(lk)
        say(f"  pull_cable {lk[:12]}…: {r.text}")
    wait(lambda: all(next(l for l in WIN.view_data.links if l.key == lk).status.kind.value == "CABLE_PULLED"
                     for lk in keys), 60, "both Po1 cables shown as pulled")
    def po2_root():
        b = po2()
        side = b.side(node_key("SW2")) if b is not None else None
        return b is not None and b.stp_block is None and side is not None and 10 in side.stp and \
            side.stp[10][0].value == "Root"
    wait(po2_root, 120, "STP failed over: SW2 Po2 is the root port (BLK gone from Po2)")
    say("  app: " + state_line())
    zoom(WIN)
    shot(WIN.view, "po1-pulled")
    WIN.view.fit_all()
    show("SW2", ["show spanning-tree vlan 10", "show etherchannel summary"], ("Po1", "Po2", "Port "))
    say("== pull: restore both cables")
    for lk in keys:
        r = eng.restore_cable(lk)
        say(f"  restore_cable {lk[:12]}…: {r.text}")
    wait(lambda: po1() and po1().state.value == "up" and po1().bundled == 2, 240, "Po1 LACP 2/2 again")
    wait(lambda: po2() and po2().stp_block is not None, 120, "Po2 blocked again (STP back to Po1)")
    say("  app: " + state_line())
    show("SW2", ["show spanning-tree vlan 10"], ("Po1", "Po2"))


def step_flow():
    from labdesk_app.ui import dialogs as D
    from labdesk_app.ui import main_window as MW
    say("== flow: 'Unbundle…' Po2 through the app")
    b2 = po2()
    keys = sorted(b2.link_keys)
    asked = []
    real_confirm = MW.confirm
    MW.confirm = lambda parent, title, text, verb, **kw: asked.append((title, text, kw.get("detail", ""))) or True
    try:
        WIN.unbundle(b2.key)
        wait(lambda: asked, 30, "unbundle confirmation shown")
        for t in asked:
            say(f"  dialog: {t[0]}: {t[1]}")
            for ln in t[2].splitlines():
                say(f"      {ln}")
        wait(lambda: po2() is None, 240, "Po2 gone in the app (cables are separate links)")
    finally:
        MW.confirm = real_confirm
    show("SW1", ["show etherchannel summary"], ("Po1", "Po2"))
    show("SW2", ["show spanning-tree vlan 10"], ("Po1", "Gi1/"))
    WIN.view.fit_all()
    pump(APP, 1.0)
    shot(WIN, "unbundled")
    say("== flow: 'Bundle into EtherChannel…' PAgP via the dialog (SW1 desirable, SW2 auto, Po2)")
    seen = {}

    def fake_exec(dlg):
        pagp = next(b for b in dlg.proto_group.buttons() if b.property("proto") == "pagp")
        pagp.setChecked(True)
        a_is_sw1 = dlg.plan.a.label == "SW1"
        (dlg.mode_a if a_is_sw1 else dlg.mode_b).setCurrentIndex(0)      # desirable
        (dlg.mode_b if a_is_sw1 else dlg.mode_a).setCurrentIndex(1)      # auto
        dlg.po.setValue(2)
        dlg.cb_lb.setChecked(True)
        dlg.lb.setCurrentText("src-dst-ip")
        dlg.resize(760, 860)
        dlg.show()
        pump(APP, 0.6)
        shot(dlg, "dialog")
        seen["plan"] = dlg.plan
        seen["check"] = dlg.check.text()
        seen["cmds"] = (dlg.prev_a.toPlainText(), dlg.prev_b.toPlainText())
        dlg.hide()
        return QDialog.Accepted
    real_exec = D.BundleDialog.exec
    D.BundleDialog.exec = fake_exec
    try:
        WIN.bundle_dialog(keys)
        wait(lambda: "plan" in seen, 60, "dialog shown")
        p = seen["plan"]
        say(f"  dialog: Po{p.po} {p.protocol} {p.a.label} {p.a.mode} / {p.b.label} {p.b.mode}, load-balance "
            f"{p.load_balance}, L2 from {p.l2_source[0]}")
        for lbl, txt in zip((p.a.label, p.b.label), seen["cmds"]):
            say(f"  commands {lbl}: " + " | ".join(x.strip() for x in txt.splitlines() if x.strip()))
        wait(lambda: po2() is not None and po2().state.value == "up" and po2().protocol == "PAgP", 300,
             "app: Po2 PAgP 2/2 formed")
        wait(lambda: po2() is not None and po2().stp_block is not None, 120, "Po2 blocked as a whole again")
    finally:
        D.BundleDialog.exec = real_exec
    say("  app: " + state_line())
    WIN.view.fit_all()
    pump(APP, 1.0)
    shot(WIN, "rebundled")
    show("SW1", ["show etherchannel summary", "show running-config | section Port-channel2|channel-group 2"],
         ("Po2", "channel-group 2", "Port-channel2", "load-balance", "mode"))
    show("SW2", ["show etherchannel summary", "show pagp neighbor"], ("Po2", "Gi1/"))
    pend = WIN.ctl.state.lab(WIN.ctl.lab_id).get("day0_pending_ec") or {}
    say(f"  day-0 pending ops (temp state of this check): { {k[:8]: [o['op'] for o in v] for k, v in pend.items()} }")


def step_final():
    say("== final: CLI + pings")
    show("SW1", ["show etherchannel summary", "show interfaces trunk"], ("Po1", "Po2", "Gi0/0"))
    show("SW2", ["show etherchannel summary", "show lacp neighbor", "show pagp neighbor", "show spanning-tree vlan 20",
                 "show interfaces trunk"], ("Po1", "Po2", "Gi0/", "Gi1/"))
    import pc
    out = pc.run("PC1", ["ping -c 3 192.168.20.10", "ping -c 3 8.8.8.8"])
    for ln in out.splitlines():
        if "packets transmitted" in ln or ln.startswith("###"):
            say("  " + ln)
    say("  app: " + state_line())


def main() -> int:
    global APP, WIN
    ap = argparse.ArgumentParser()
    ap.add_argument("--steps", default="base,shut,mismatch,pull,flow,final")
    a = ap.parse_args()
    QApplication.setStyle("Fusion")
    APP = QApplication(sys.argv[:1])
    QSettings.setDefaultFormat(QSettings.IniFormat)
    QSettings.setPath(QSettings.IniFormat, QSettings.UserScope, _TMP)
    from labdesk_app.controller import LabController
    from labdesk_app.ui import themectl
    from labdesk_app.ui.main_window import MainWindow
    themectl.install(APP, mode="light")
    ctl = LabController()
    WIN = MainWindow(ctl, NullLauncher())
    WIN.resize(1920, 1080)
    WIN.show()
    real_log = WIN.log

    def log(msg, level="info", tag=None, hue=None, ts=None):
        if tag in ("ETHERCHANNEL", "STP") or "EtherChannel" in str(msg) or "Unbundle" in str(msg):
            say(f"  [app log] {tag or ''} {msg}")
        return real_log(msg, level, tag, hue, ts) if ts is not None else real_log(msg, level, tag, hue)
    WIN.log = log
    WIN.open_lab_title("Switching Lab")
    QA.mkdir(exist_ok=True)
    WIN.infobar.hide()
    WIN._notice_shown = True
    try:
        for st in a.steps.split(","):
            {"base": step_base, "shut": step_shut, "mismatch": step_mismatch, "pull": step_pull,
             "flow": step_flow, "final": step_final}[st]()
    finally:
        (QA / "ec_live_log.txt").write_text("\n".join(OUT) + "\n")
        WIN.close()
        pump(APP, 0.5)
        ctl.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
