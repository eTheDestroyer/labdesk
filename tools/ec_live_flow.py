#!/usr/bin/env python3
"""Live 'Unbundle…' + 'Bundle into EtherChannel…' through the app's own dialog flow, safe while ANOTHER LabDesk is
running and probing the same lab (the user's instance).

    QT_QPA_PLATFORM=offscreen .venv/bin/python tools/ec_live_flow.py [--po2-only]

* This offscreen MainWindow does not probe (probing off).  Its telemetry facts are MIRRORED from the running
  instance's probe logs (~/.cache/labdesk/probe/<node>.log: a passive read), so the Inspector / bundles / watch see
  what the user's LabDesk sees.
* Console etiquette between the two instances: a console session of this window (telemetry.configure) starts
  through a gated transport - it waits until the other instance has no probe session on that console, then opens
  its ssh and waits until it has been visible for 4 s (the other instance's /proc scan then marks the node
  'console in use' and pauses it) before typing.  Nothing is typed into a console a user has open (none of SW1/SW2
  may be open; checked first).
Steps: Unbundle Po2 (confirm patched to 'yes', the commands are logged), wait until it is gone, then the
BundleDialog (PAgP, SW1 desirable / SW2 auto, Po2, load-balance src-dst-ip) -> Apply -> watch until formed.
Screenshots qa/ec-live-flow-*.png, log qa/ec_live_flow_log.txt.  The day-0 pending ops go to a temp state file
(the CML day-0 configs of SW1/SW2 already contain Po2 PAgP desirable/auto).
"""
from __future__ import annotations

import os
import re
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
_TMP = tempfile.mkdtemp(prefix="labdesk-qa-")
os.environ["XDG_CONFIG_HOME"] = _TMP

from PySide6.QtCore import QSettings  # noqa: E402
from PySide6.QtWidgets import QApplication, QDialog  # noqa: E402

from ui_shots import QA, NullLauncher, pump  # noqa: E402

LOGDIR = Path.home() / ".cache" / "labdesk" / "probe"
HDR = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d) status=(\S+)")
OUT: list[str] = []


def say(m):
    line = time.strftime("%H:%M:%S ") + m
    print(line, flush=True)
    OUT.append(line)


def foreign_sessions(uuid: str, mine: set[int]) -> list[int]:
    """ssh 'connect <uuid>' processes that are not ours (the other LabDesk's probes, or a user's console)."""
    out = []
    for d in Path("/proc").iterdir():
        if not d.name.isdigit() or int(d.name) in mine or int(d.name) == os.getpid():
            continue
        try:
            argv = (d / "cmdline").read_bytes().split(b"\0")
        except OSError:
            continue
        if argv and argv[0].endswith(b"ssh") and uuid.encode() in argv:
            out.append(int(d.name))
    return out


def user_console_open(uuid: str) -> bool:
    """A user's console on that node (a LabDesk Konsole tab = ssh under tabexec.py, or ssh under a shell /
    terminal).  Probe sessions (ssh under a python process: any LabDesk instance or this tool) do not count."""
    for pid in foreign_sessions(uuid, set()):
        try:
            ppid = int(Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[1])
            parent = (Path(f"/proc/{ppid}/cmdline").read_bytes().split(b"\0") or [b""])
        except (OSError, IndexError, ValueError):
            continue
        if any(b"tabexec" in a for a in parent) or b"python" not in parent[0]:
            return True
    return False


def gated_factory(base):
    from labdesk_app.probe.transport import TermserverPty

    class Gated(TermserverPty):
        def start(self):
            uuid = self.argv[-1]
            t0 = time.time()
            while foreign_sessions(uuid, set()) and time.time() - t0 < 60:
                time.sleep(0.2)
            super().start()
            mine = {self.pid} if self.pid else set()
            quiet_since = time.time()
            while time.time() - quiet_since < 4.0 and time.time() - t0 < 90:
                if foreign_sessions(uuid, mine):
                    quiet_since = time.time()           # their burst still runs: wait until it is over
                time.sleep(0.2)
            say(f"  gated console start after {time.time() - t0:.1f} s (other instance paused)")

    def make(uuid, user):
        return Gated(uuid, user)
    return make


def latest_outputs(label: str) -> tuple[dict, float]:
    text = (LOGDIR / f"{label}.log").read_text(errors="replace").splitlines()
    heads = [(i, HDR.match(ln)) for i, ln in enumerate(text) if HDR.match(ln)]
    out, newest = {}, 0.0
    for k, (start, m) in enumerate(heads):
        end = heads[k + 1][0] if k + 1 < len(heads) else len(text)
        ts = time.mktime(time.strptime(m[1], "%Y-%m-%d %H:%M:%S"))
        cur, buf = None, []
        for ln in text[start + 1:end] + [f"{label}>"]:
            mm = re.match(rf"^{label}[>#](.*)$", ln)
            if mm:
                if cur and not cur.startswith(("show clock", "terminal")):
                    out[cur] = "\n".join(buf)
                cur, buf = mm[1].strip(), []
            elif cur is not None:
                buf.append(ln)
        newest = max(newest, ts)
    return out, newest


def mirror(eng) -> None:
    """Facts of SW1 / SW2 / R1 from the other instance's probe logs (as if this window had probed)."""
    from labdesk_app.facts import (CollectorState, L2_CMDS, NodeFacts, ec_facts_update, ios_facts_update,
                                   l2_facts_update)
    from labdesk_app.parsers import ios_l2 as P_L2
    from labdesk_app.telemetry import IOS_PARSERS
    if eng.topo is None:
        return
    for nk, n in eng.topo.nodes.items():
        if n.label not in ("SW1", "SW2", "R1"):
            continue
        outs, ts = latest_outputs(n.label)
        now = time.time()
        parsed = {c: fn(outs[c]) for c, fn in IOS_PARSERS.items() if c in outs}
        if n.label.startswith("SW") and "show ip route connected" in outs:
            parsed[L2_CMDS["routing"]] = P_L2.parse_routing_enabled(outs["show ip route connected"])
        nf = NodeFacts(nk, n.label)
        ios_facts_update(nf, parsed, now)
        if n.label.startswith("SW"):
            l2_facts_update(nf, parsed, now)
        ec_facts_update(nf, parsed, now)
        nf.collector_state = CollectorState.OK
        nf.last_burst_ts = ts
        with eng._lock:
            eng.facts[nk] = nf


def other_instance() -> list[int]:
    """PIDs of another running LabDesk (python -m labdesk_app)."""
    out = []
    for d in Path("/proc").iterdir():
        if d.name.isdigit() and int(d.name) != os.getpid():
            try:
                if b"labdesk_app" in (d / "cmdline").read_bytes().split(b"\0"):
                    out.append(int(d.name))
            except OSError:
                pass
    return out


def main() -> int:
    QApplication.setStyle("Fusion")
    app = QApplication(sys.argv[:1])
    QSettings.setDefaultFormat(QSettings.IniFormat)
    QSettings.setPath(QSettings.IniFormat, QSettings.UserScope, _TMP)
    from labdesk_app.controller import LabController
    from labdesk_app.ui import dialogs as D
    from labdesk_app.ui import main_window as MW
    from labdesk_app.ui import themectl
    from labdesk_app.ui.main_window import MainWindow
    themectl.install(app, mode="light")
    ctl = LabController()
    eng = ctl.telemetry
    other = other_instance()
    if other:
        eng.set_probing(False)                 # the other LabDesk probes; mirror its facts (passive read)
    eng.transport_factory = gated_factory(eng.transport_factory)
    win = MainWindow(ctl, NullLauncher())
    win.resize(1920, 1080)
    win.show()
    real_log = win.log

    def log(msg, level="info", tag=None, hue=None, ts=None):
        if tag == "ETHERCHANNEL" or "EtherChannel" in str(msg) or "Unbundle" in str(msg):
            say(f"  [app log] {msg}")
        return real_log(msg, level, tag, hue, ts)
    win.log = log
    win.open_lab_title("Switching Lab")
    win.infobar.hide()
    win._notice_shown = True
    QA.mkdir(exist_ok=True)

    def tick(secs):
        end = time.time() + secs
        while time.time() < end:
            if other:
                mirror(eng)
            pump(app, 1.0)

    def bundles():
        tv = win.view_data.telemetry if win.view_data else None
        return list(tv.bundles.values()) if tv is not None else []

    def po2():
        """The bundle on the Gi1/0 + Gi1/1 cables (whichever side it is keyed on)."""
        links = {lk for lk, l in (eng.topo.links.items() if eng.topo else []) if l.iface_a.startswith("GigabitEthernet1/")}
        return next((b for b in bundles() if set(b.link_keys) & links), None)

    def wait(pred, timeout, what):
        t0 = time.time()
        while time.time() - t0 < timeout:
            tick(2)
            try:
                if pred():
                    say(f"  OK after {time.time() - t0:.0f} s: {what}")
                    return True
            except Exception:  # noqa: BLE001
                pass
        say(f"  TIMEOUT {timeout} s: {what}")
        return False

    def state():
        return " | ".join(f"{b.label} [{b.state.value}]" + (f" STP {b.stp_block.port} BLK" if b.stp_block else "")
                          for b in sorted(bundles(), key=lambda x: x.po_text)) or "no bundles"

    def unbundle_step():
        say("== Unbundle Po2 (app flow)")
        asked = []
        MW.confirm = lambda parent, title, text, verb, **kw: asked.append((title, text, kw.get("detail", ""))) or True
        win.unbundle(po2().key)
        wait(lambda: asked, 30, "unbundle confirmation")
        for t in asked:
            say(f"  dialog: {t[1]}")
            for ln in t[2].splitlines():
                say(f"      {ln}")
        wait(lambda: any("Unbundle Po2: done" in x for x in OUT), 300, "unbundle done")
        say("app: " + state())
        win.view.fit_all()
        tick(2)
        win.grab().save(str(QA / "ec-live-flow-unbundled.png"))

    def bundle_step():
        MW.confirm = lambda parent, title, text, verb, **kw: True
        say("== Bundle into EtherChannel… (dialog: PAgP, SW1 desirable, SW2 auto, Po2, load-balance src-dst-ip)")
        seen = {}

        def fake_exec(dlg):
            next(b for b in dlg.proto_group.buttons() if b.property("proto") == "pagp").setChecked(True)
            a_sw1 = dlg.plan.a.label == "SW1"
            (dlg.mode_a if a_sw1 else dlg.mode_b).setCurrentIndex(0)
            (dlg.mode_b if a_sw1 else dlg.mode_a).setCurrentIndex(1)
            dlg.po.setValue(2)
            dlg.cb_lb.setChecked(True)
            dlg.lb.setCurrentText("src-dst-ip")
            dlg.resize(760, 860)
            dlg.show()
            pump(app, 0.6)
            dlg.grab().save(str(QA / "ec-live-flow-dialog.png"))
            seen["plan"], seen["check"] = dlg.plan, dlg.check.text()
            seen["cmds"] = (dlg.prev_a.toPlainText(), dlg.prev_b.toPlainText())
            dlg.hide()
            return QDialog.Accepted
        D.BundleDialog.exec = fake_exec
        keys = sorted(lk for lk, l in eng.topo.links.items() if l.iface_a.startswith("GigabitEthernet1/"))
        win.bundle_dialog(keys)
        if not wait(lambda: "plan" in seen, 90, "dialog shown"):
            return
        p = seen["plan"]
        say(f"  dialog: Po{p.po} {p.protocol} {p.a.label} {p.a.mode} / {p.b.label} {p.b.mode}, load-balance "
            f"{p.load_balance}, L2 from {p.l2_source[0]}; check: {re.sub('<[^>]+>', '', seen['check'])}")
        for lbl, txt in zip((p.a.label, p.b.label), seen["cmds"]):
            say(f"  commands {lbl}: " + " | ".join(x.strip() for x in txt.splitlines() if x.strip()))
        wait(lambda: po2() is not None and po2().state.value == "up" and po2().protocol == "PAgP", 300,
             "Po2 PAgP 2/2 formed")
        wait(lambda: po2() is not None and po2().stp_block is not None, 120, "Po2 blocked as a whole again")
        tick(4)
        say("app: " + state())
        win.view.fit_all()
        tick(2)
        win.grab().save(str(QA / "ec-live-flow-rebundled.png"))
        pend = ctl.state.lab(ctl.lab_id).get("day0_pending_ec") or {}
        say(f"day-0 pending ops (temp state of this check): { {k[:8]: [o['op'] for o in v] for k, v in pend.items()} }")

    try:
        tick(6)
        say(f"other LabDesk running: {other or 'no'} -> " + ("mirroring its probe logs" if other else "probing myself"))
        for lbl in ("SW1", "SW2"):
            nk = next(n.key for n in win.view_data.nodes if n.label == lbl)
            if user_console_open(eng.console_uuid.get(nk, "-")):
                say(f"{lbl}: a user's console is open - live console work skipped")
                return 1
        wait(lambda: win.view_data.telemetry is not None and all(
            n.switch is not None and n.switch.stp for n in win.view_data.telemetry.nodes.values()
            if n.label in ("SW1", "SW2")), 180, "switches probed")
        tick(25)
        say("app: " + state())
        if "--bundle-only" not in sys.argv:
            unbundle_step()
        bundle_step()
    finally:
        (QA / "ec_live_flow_log.txt").write_text("\n".join(OUT) + "\n")
        win.close()
        pump(app, 0.5)
        ctl.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
