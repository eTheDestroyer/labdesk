#!/usr/bin/env python3
"""Live check of 'Subnets on cables' on the running "Switching Lab" (needs CML; changes are reverted).

    QT_QPA_PLATFORM=offscreen .venv/bin/python tools/subnet_live_check.py [--skip-mismatch]

Drives the real MainWindow (offscreen) against the real controller, and configures R1 / SW1 / SW2 through
their serial consoles exactly the way LabDesk reaches a console (ssh to the CML terminal server,
``connect <console-uuid>``), with the app's etiquette: the session is opened under the engine's per-node
lock (never in the middle of a probe burst), the line state is classified before anything is typed, and
nothing is typed into a session found in someone else's dialogue.  While the session is open, the engine
sees it as "console in use" (probing of that node pauses), so the label can only come from the console tap.

Steps (latency = Enter on the address command -> the cable label on the canvas shows it):
  A. console held open on R1: add / change / remove a secondary on Gi0/0.10 (172.16.99.1/24 ...)
  B. console closed: the same with one short session per change (label from the last tap read + the
     immediate probe that a closing console triggers; the CLI confirmation time is reported too)
  C. mismatch: SW1 Gi0/2 and SW2 Gi0/2 (the STP-blocked redundant trunk) become routed ports in different
     /30s, then are restored to the day-0 trunk config
  D. revert check: running-config of the touched interfaces identical to before, PC1 pings PC2.
Screenshots: qa/subnet-live-*.png.  Results: qa/subnet_live_log.txt.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import tempfile as _tempfile  # noqa: E402
os.environ["XDG_CONFIG_HOME"] = _tempfile.mkdtemp(prefix="labdesk-qa-")   # L1: never touch ~/.config/LabDesk

from PySide6.QtCore import QSettings  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from ui_shots import QA, NullLauncher, pump, select  # noqa: E402

from labdesk_app.parsers.common import ANSI_RE  # noqa: E402
from labdesk_app.probe import creds as creds_mod  # noqa: E402
from labdesk_app.probe.creds import DeviceCreds  # noqa: E402
from labdesk_app.probe.session import IosDialect, run_burst  # noqa: E402
from labdesk_app.probe.transport import TermserverPty  # noqa: E402

LAB_ENABLE = "cisco"     # the Switching Lab's day-0 'enable secret' (tools/build_switching_lab.py); never printed
OUT: list[str] = []


def say(msg: str) -> None:
    line = time.strftime("%H:%M:%S ") + msg
    print(line, flush=True)
    OUT.append(line)


def enable_secret(label: str) -> str:
    try:
        c = creds_mod.load(label)
        if c.enable:
            return c.enable
    except Exception:       # noqa: BLE001
        pass
    return LAB_ENABLE


class Console:
    """An interactive console session the way a user's Konsole tab has one (ssh ... connect <uuid>)."""

    PROMPT = re.compile(r"(?m)^[A-Za-z0-9._-]+(\(config[^)]*\))?[>#]\s*$")

    def __init__(self, eng, nk: str, label: str):
        self.eng, self.nk, self.label = eng, nk, label
        self.t: TermserverPty | None = None
        self.buf = ""
        self.enabled = False
        self.sent_at: dict[str, float] = {}

    def _read(self, secs: float, until=None) -> None:
        end = time.time() + secs
        while time.time() < end:
            d = self.t.read(0.1)
            if d:
                self.buf += d.decode("utf-8", "replace").replace("\r", "")
                if until and until(self.buf):
                    return

    def _prompt(self) -> str:
        tail = self.buf.rstrip("\n").rsplit("\n", 1)[-1].strip()
        return tail

    def open(self) -> None:
        lock = self.eng._node_lock(self.nk)          # never start in the middle of a probe burst
        if not lock.acquire(timeout=40):
            raise RuntimeError(f"{self.label}: probe lock busy")
        try:
            self.t = TermserverPty(self.eng.console_uuid[self.nk])
            self.t.start()
            self._read(8, lambda b: "Escape character is" in b)
            self._read(0.6)
            self.buf = ""
            self.t.write(b"\r")
            self._read(2.0, lambda b: self.PROMPT.search(b.split("\n")[-1] or "") is not None)
            kind, _ = IosDialect.classify(self.buf)
            self.found = kind                        # restored on close (etiquette: leave the line as found)
            if kind not in ("user", "priv"):
                self.t.close()
                raise RuntimeError(f"{self.label}: console is not at an exec prompt ({kind}) - not typing")
        finally:
            lock.release()

    def send(self, *lines: str, wait: float = 4.0, mark: str | None = None) -> str:
        """Type lines (each waits for the next prompt); ``mark``: remember when the line containing it hit Enter."""
        lock = self.eng._node_lock(self.nk)
        if not lock.acquire(timeout=40):
            raise RuntimeError(f"{self.label}: probe lock busy")
        out = []
        try:
            for ln in lines:
                self.buf = ""
                self.t.write(ln.encode() + b"\r")
                if mark and mark in ln:
                    self.sent_at[mark] = time.time()
                self._read(wait, lambda b: bool(re.search(r"[>#]\s*$", b)) or b.rstrip().endswith("Password:"))
                out.append(self.buf)
                err = [x for x in self.buf.split("\n") if x.strip().startswith("%") and "SYS-5-CONFIG_I" not in x
                       and not re.match(r"^\*?\w{3}\s+\d", x.strip())]
                if err:
                    say(f"  {self.label}: '{ln}' -> {err[0].strip()}")
        finally:
            lock.release()
        return "".join(out)

    def enable(self) -> None:
        if self._prompt().endswith("#"):
            return
        lock = self.eng._node_lock(self.nk)
        lock.acquire(timeout=40)
        try:
            self.buf = ""
            self.t.write(b"enable\r")
            self._read(3, lambda b: b.rstrip().endswith("Password:") or b.rstrip().endswith("#"))
            if self.buf.rstrip().endswith("Password:"):
                self.buf = ""
                self.t.write(enable_secret(self.label).encode() + b"\r")
                self._read(3, lambda b: bool(re.search(r"[>#]\s*$", b)))
            self.enabled = self._prompt().endswith("#")
        finally:
            lock.release()
        if not self.enabled:
            raise RuntimeError(f"{self.label}: enable failed")

    def close(self) -> None:
        try:
            if self.t is not None and self.t.alive():
                self.buf = ""
                self.t.write(b"\r")
                self._read(2.0, lambda b: bool(re.search(r"[>#]\s*$", b)))
                if "(config" in self._prompt():
                    self.send("end")
                if getattr(self, "found", "user") == "user" and self._prompt().endswith("#"):
                    self.send("disable")
        finally:
            if self.t is not None:
                self.t.close()
                self.t = None


def run_bg(app, fn, *a):
    box = {}

    def go():
        try:
            box["r"] = fn(*a)
        except Exception as e:      # noqa: BLE001
            box["e"] = e
    th = threading.Thread(target=go, daemon=True)
    th.start()
    while th.is_alive():
        pump(app, 0.05)
    if "e" in box:
        raise box["e"]
    return box.get("r")


def link_key(win, a, ia, b, ib):
    for li in win.scene.links.values():
        if ((li.a.label, li.label_a, li.b.label, li.label_b) in ((a, ia, b, ib), (b, ib, a, ia))):
            return li.key
    raise KeyError((a, ia, b, ib))


def addressing(win, lk):
    li = win.scene.links.get(lk)
    return getattr(li.lv, "addressing", None) if li is not None and li.lv is not None else None


def wait_label(app, win, lk, pred, timeout=90.0):
    end = time.time() + timeout
    while time.time() < end:
        pump(app, 0.05)
        ad = addressing(win, lk)
        if pred(ad):
            return time.time(), ad
    return None, addressing(win, lk)


def shot(win, name):
    QA.mkdir(exist_ok=True)
    p = QA / f"subnet-live-{name}.png"
    win.grab().save(str(p))
    say(f"  screenshot {p}")


def subnet_events(ctl, since):
    return [e for e in ctl.telemetry.events_since(since) if e.kind == "SUBNET"]


def show_run(eng, nk, label, iface) -> str:
    """'show running-config interface X' under the probe lock (enable for this read only)."""
    lock = eng._node_lock(nk)
    lock.acquire(timeout=40)
    try:
        cmd = f"show running-config interface {iface}"
        res = run_burst(TermserverPty(eng.console_uuid[nk]), IosDialect(), [cmd],
                        DeviceCreds(enable=enable_secret(label)), timeout=40, expect_hostname=label)
        out = res.outputs.get(cmd)
        text = out.text if out and out.text else ""
        return "\n".join(x for x in text.split("\n") if x.strip() and not x.startswith(("Building", "Current"))
                         and x.strip() != "end")
    finally:
        lock.release()


def has_net(net):
    return lambda ad: ad is not None and net in (ad.nets or [])


def lacks_net(net):
    return lambda ad: ad is not None and net not in (ad.nets or [])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-mismatch", action="store_true")
    ap.add_argument("--skip-closed", action="store_true")
    ap.add_argument("--only-mismatch", action="store_true")
    a = ap.parse_args()
    app = QApplication(sys.argv[:1])
    QSettings.setDefaultFormat(QSettings.IniFormat)
    QSettings.setPath(QSettings.IniFormat, QSettings.UserScope, str(ROOT / "qa" / ".settings-subnet-live"))
    from labdesk_app.controller import LabController
    from labdesk_app.ui.main_window import MainWindow
    ctl = LabController()
    win = MainWindow(ctl, NullLauncher())
    win.resize(1500, 940)
    win.show()
    win.open_lab_title("Switching Lab")
    eng = ctl.telemetry
    # wait until R1, SW1, SW2 have address facts and the trunk label is up
    end = time.time() + 150
    while time.time() < end:
        pump(app, 0.5)
        if win.view_data is not None and win.scene.links:
            try:
                lk_trunk = link_key(win, "R1", "Gi0/0", "SW1", "Gi0/0")
            except KeyError:
                continue
            if has_net("192.168.10.0/24")(addressing(win, lk_trunk)) and all(
                    eng.facts.get(k) is not None and eng.facts[k].addrs is not None
                    for k, n in eng.topo.nodes.items() if n.collected):
                break
    else:
        say("lab not ready")
        return 1
    win.infobar.hide()
    win.view.fit_all()
    pump(app, 1.0)
    nodes = {n.label: k for k, n in eng.topo.nodes.items()}
    r1 = nodes["R1"]
    say(f"ready: trunk label '{addressing(win, lk_trunk).label}'")
    shot(win, "0-before")
    before_cfg = {("R1", "GigabitEthernet0/0.10"): run_bg(app, show_run, eng, r1, "R1", "GigabitEthernet0/0.10")}
    if not a.skip_mismatch:
        for sw in ("SW1", "SW2"):
            before_cfg[(sw, "GigabitEthernet0/2")] = run_bg(app, show_run, eng, nodes[sw], sw, "GigabitEthernet0/2")
    for (lbl, ifn), txt in before_cfg.items():
        say(f"before {lbl} {ifn}: " + " | ".join(x.strip() for x in txt.split("\n")))
    results = []

    def measure(tag, con, cmd_lines, mark, pred, shot_name=None, select_link=None):
        seq = eng._seq
        run_bg(app, lambda: con.send(*cmd_lines, mark=mark))
        t_cmd = con.sent_at[mark]
        t_lab, ad = wait_label(app, win, lk_trunk if select_link is None else select_link, pred)
        dt = (t_lab - t_cmd) if t_lab else None
        evs = subnet_events(ctl, seq)
        pump(app, 0.3)
        say(f"{tag}: label {'after %.2f s' % dt if dt is not None else 'NOT seen'} -> "
            f"'{ad.label if ad else None}' (source {ad.source if ad else '-'})")
        for e in evs:
            say(f"  event: SUBNET {e.text}")
        results.append((tag, dt, ad.label if ad else None, ad.source if ad else None, [e.text for e in evs]))
        if shot_name:
            shot(win, shot_name)
        return dt

    # ---------------------------------------------------------------- A. console held open
    if not a.only_mismatch:
        select(win, link=("R1", "Gi0/0", "SW1", "Gi0/0"))
        con = Console(eng, r1, "R1")
        run_bg(app, con.open)
        run_bg(app, con.enable)
        run_bg(app, lambda: con.send("configure terminal", "interface GigabitEthernet0/0.10"))
        pump(app, 4.5)                                        # the /proc scan sees the session -> R1 paused
        say(f"R1 collector while the console is open: {win.view_data.telemetry.nodes[r1].collector_state.value}")
        measure("A1 open/configured", con, ["ip address 172.16.99.1 255.255.255.0 secondary"], "172.16.99.1",
                has_net("172.16.99.0/24"), "A1-open-configured")
        measure("A2 open/changed", con, ["no ip address 172.16.99.1 255.255.255.0 secondary",
                                         "ip address 172.16.98.1 255.255.255.0 secondary"], "172.16.98.1",
                lambda ad: has_net("172.16.98.0/24")(ad) and lacks_net("172.16.99.0/24")(ad), "A2-open-changed")
        measure("A3 open/removed", con, ["no ip address 172.16.98.1 255.255.255.0 secondary"], "172.16.98.1",
                lacks_net("172.16.98.0/24"), "A3-open-removed")
        run_bg(app, lambda: con.send("end"))
        pump(app, 2.0)
        say(f"R1 collector (console still open): {win.view_data.telemetry.nodes[r1].collector_state.value}")
        run_bg(app, con.close)
        t_close = time.time()
        t_ok, _ = wait_label(app, win, lk_trunk, lambda ad: True, 1)
        end = time.time() + 60
        while time.time() < end and win.view_data.telemetry.nodes[r1].collector_state.value != "ok":
            pump(app, 0.2)
        say(f"console closed -> R1 probing resumed after {time.time() - t_close:.1f} s")

        # ---------------------------------------------------------------- B. console closed (one session per change)
        if not a.skip_closed:
            def session(lines, mark):
                c = Console(eng, r1, "R1")
                c.open()
                c.enable()
                c.send("configure terminal", "interface GigabitEthernet0/0.10")
                c.send(*lines, mark=mark)
                c.send("end")
                c.close()
                return c.sent_at[mark], time.time()

            for tag, lines, mark, pred, name in (
                    ("B1 closed/configured", ["ip address 172.16.97.1 255.255.255.0 secondary"], "172.16.97.1",
                     has_net("172.16.97.0/24"), "B1-closed-configured"),
                    ("B2 closed/changed", ["no ip address 172.16.97.1 255.255.255.0 secondary",
                                           "ip address 172.16.96.1 255.255.255.0 secondary"], "172.16.96.1",
                     lambda ad: has_net("172.16.96.0/24")(ad) and lacks_net("172.16.97.0/24")(ad), "B2-closed-changed"),
                    ("B3 closed/removed", ["no ip address 172.16.96.1 255.255.255.0 secondary"], "172.16.96.1",
                     lacks_net("172.16.96.0/24"), "B3-closed-removed")):
                seq = eng._seq
                t_cmd, t_closed = run_bg(app, session, lines, mark)
                t_lab, ad = wait_label(app, win, lk_trunk, pred)
                # when does the CLI probe confirm it (the source becomes 'cli' with the change in place)?
                t_cli, ad2 = wait_label(app, win, lk_trunk, lambda x: pred(x) and x.source == "cli", 60)
                evs = subnet_events(ctl, seq)
                say(f"{tag}: session {t_closed - t_cmd:.1f} s; label after {t_lab - t_cmd:.2f} s from Enter "
                    f"({t_lab - t_closed:+.2f} s from closing), CLI-confirmed after "
                    f"{(t_cli - t_cmd) if t_cli else float('nan'):.1f} s -> '{ad.label}'")
                for e in evs:
                    say(f"  event: SUBNET {e.text}")
                results.append((tag, t_lab - t_cmd if t_lab else None, ad.label, ad.source, [e.text for e in evs]))
                shot(win, name)

    # ---------------------------------------------------------------- C. mismatch on the redundant SW1-SW2 trunk
    if not a.skip_mismatch:
        lk_sw = link_key(win, "SW1", "Gi0/2", "SW2", "Gi0/2")
        select(win, link=("SW1", "Gi0/2", "SW2", "Gi0/2"))
        seq = eng._seq
        t0 = time.time()
        for sw, ip in (("SW1", "10.99.12.1"), ("SW2", "10.99.21.2")):
            c = Console(eng, nodes[sw], sw)
            run_bg(app, c.open)
            run_bg(app, c.enable)
            run_bg(app, lambda: c.send("configure terminal", "interface GigabitEthernet0/2", "no switchport",
                                       f"ip address {ip} 255.255.255.252", "end"))
            run_bg(app, c.close)
        t_lab, ad = wait_label(app, win, lk_sw, lambda x: x is not None and x.state.value == "mismatch", 120)
        say(f"C mismatch: label after {(t_lab - t0) if t_lab else float('nan'):.1f} s -> "
            f"'{ad.label if ad else None}'")
        pump(app, 1.0)
        for e in subnet_events(ctl, seq):
            say(f"  event: SUBNET {e.text}")
        shot(win, "C-mismatch")
        if ad is not None:
            say("  inspector: " + ad.explain)
        # restore the day-0 trunk (SW1 and SW2 Gi0/2 are identical apart from the description)
        seq = eng._seq
        for sw, desc in (("SW1", "TRUNK to SW2 Gi0/2"), ("SW2", "TRUNK to SW1 Gi0/2")):
            c = Console(eng, nodes[sw], sw)
            run_bg(app, c.open)
            run_bg(app, c.enable)
            run_bg(app, lambda: c.send("configure terminal", "interface GigabitEthernet0/2", "no ip address",
                                       "switchport", "switchport trunk encapsulation dot1q",
                                       "switchport trunk allowed vlan 10,20", "switchport mode trunk",
                                       "switchport nonegotiate", f"description {desc}", "no shutdown", "end"))
            run_bg(app, c.close)
        t_lab, ad = wait_label(app, win, lk_sw, lambda x: x is None, 120)
        say(f"C restored: SW1-SW2 Gi0/2 label gone: {ad is None}")
        for e in subnet_events(ctl, seq):
            say(f"  event: SUBNET {e.text}")

    # ---------------------------------------------------------------- D. revert check
    win.scene.clearSelection()
    pump(app, 1.0)
    ok = True
    for (lbl, ifn), txt in before_cfg.items():
        now_txt = run_bg(app, show_run, eng, nodes[lbl], lbl, ifn)
        same = now_txt == txt
        ok &= same
        say(f"after {lbl} {ifn}: {'identical' if same else 'DIFFERENT'}: "
            + " | ".join(x.strip() for x in now_txt.split("\n")))
    pc = run_bg(app, ping_pc, eng, nodes["PC1"], "192.168.20.10")
    pc_ok = bool(re.match(r"[1-9]\d*/\d+ replies", pc))
    say(f"PC1 -> PC2 ping: {pc}")
    t_lab, ad = wait_label(app, win, lk_trunk, lambda x: x is not None and x.label == "192.168.10.0/24 +1"
                           and x.source == "cli", 60)
    say(f"final trunk label: '{ad.label if ad else None}' ({ad.source if ad else '-'})")
    shot(win, "D-after")
    win.close()
    ctl.shutdown()
    (QA / "subnet_live_log.txt").write_text("\n".join(OUT) + "\n")
    return 0 if ok and pc_ok else 2


def ping_pc(eng, nk, target, logout: bool = False) -> str:
    """Alpine console: log in only if the line asks for it (cisco/cisco, day-0), ping, log out only if we
    logged in."""
    t = TermserverPty(eng.console_uuid.get(nk) or eng.client.console_key(eng.lab_id, nk[4:]))
    t.start()
    buf = ""

    def rd(secs, until=None):
        nonlocal buf
        end = time.time() + secs
        while time.time() < end:
            d = t.read(0.1)
            if d:
                buf += ANSI_RE.sub("", d.decode("utf-8", "replace")).replace("\r", "")
                if until and until(buf):
                    return

    try:
        rd(6, lambda b: "Escape character is" in b)
        rd(0.5)
        buf = ""
        t.write(b"\r")
        rd(3, lambda b: b.rstrip().endswith(("#", "$", "login:")))
        logged_in = False
        if buf.rstrip().endswith("login:"):
            t.write(b"cisco\r")
            rd(3, lambda b: b.rstrip().endswith("Password:"))
            t.write(b"cisco\r")
            rd(4, lambda b: b.rstrip().endswith(("#", "$")))
            logged_in = True
        if not buf.rstrip().endswith(("#", "$")):
            return "PC1 console not at a shell prompt - not typing"
        buf = ""
        t.write(f"ping -c 3 -W 2 {target}\r".encode())
        rd(12, lambda b: "packet loss" in b and b.rstrip().endswith(("#", "$")))
        m = re.search(r"(\d+) packets transmitted, (\d+) (?:packets )?received", buf)
        if logged_in or logout:
            t.write(b"exit\r")
            rd(1)
        return f"{m[2]}/{m[1]} replies" if m else "no ping summary"
    finally:
        t.close()


if __name__ == "__main__":
    sys.exit(main())
