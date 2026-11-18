#!/usr/bin/env python3
"""Live end-to-end check through LabDesk's own code paths (offscreen GUI + controller).

1. Offscreen MainWindow: create lab "LabDesk Demo", drop two IOSv routers (auto-named R1, R2)
   via the palette-drop handler, link them via the link handler.
2. Startup configs via LabController.set_config, start the lab via the "Start all" handler,
   wait for BOOTED.
3. Console: build R1's console command with the app's launcher code and drive it with pexpect
   (no password prompt expected), run "show ip interface brief", prove raw tty with "sh<TAB>",
   ping R2.
4. Open ONE real Konsole tab for R1 via KonsoleLauncher, verify over D-Bus that the tab exists
   and shows the router prompt, then close that Konsole.
Output is mirrored to qa/e2e_log.txt.
"""
from __future__ import annotations

import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import tempfile as _tempfile  # noqa: E402
os.environ["XDG_CONFIG_HOME"] = _tempfile.mkdtemp(prefix="labdesk-qa-")   # L1: never touch ~/.config/LabDesk

import pexpect  # noqa: E402
from PySide6.QtCore import QEventLoop  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from labdesk_app import config, launchers  # noqa: E402
from labdesk_app.ui.main_window import MainWindow  # noqa: E402

LAB = "LabDesk Demo"
LOG = config.APP_DIR / "qa" / "e2e_log.txt"
_lines: list[str] = LOG.read_text().splitlines() if LOG.exists() else []


def say(msg: str):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    _lines.append(line)
    LOG.write_text("\n".join(_lines) + "\n")


def pump(app, secs=0.0, until=None, timeout=120):
    end = time.time() + (secs or timeout)
    while time.time() < end:
        app.processEvents(QEventLoop.AllEvents, 50)
        if until and until():
            return True
        time.sleep(0.02)
    return until() if until else True


def wait_idle(app, win, timeout=180):
    ok = pump(app, until=lambda: win.runner.active == 0, timeout=timeout)
    if not ok:
        raise SystemExit("timeout waiting for background tasks")


def cfg(host, ip):
    return f"""hostname {host}
no ip domain lookup
username admin privilege 15 secret cisco
enable secret cisco
interface GigabitEthernet0/0
 description to-{'R2' if host == 'R1' else 'R1'}
 ip address {ip} 255.255.255.252
 no shutdown
line con 0
 exec-timeout 0 0
 logging synchronous
line vty 0 4
 login local
 transport input ssh telnet
end
"""


def main():
    LOG.parent.mkdir(exist_ok=True)
    app = QApplication(sys.argv[:1])
    win = MainWindow()
    win.show()
    ctl = win.ctl

    # ------------------------------------------------------------ 1. build
    existing = ctl.client.find_lab(LAB)
    if existing:
        say(f"lab '{LAB}' exists ({existing['id']}) - reusing it")
        ctl.open_lab(existing["id"])
        win._lab_opened()
        wait_idle(app, win)
        win.poll()
        wait_idle(app, win)
        nodes = {n.label: n.key for n in win.view_data.nodes}
        links = [(l.label_a, l.label_b) for l in win.view_data.links]
        say(f"existing nodes {sorted(nodes)} links {links}")
    else:
        ctl.new_lab(LAB)
        win._lab_opened()
        wait_idle(app, win)
        say(f"created lab '{LAB}' id={ctl.lab_id}")
        win.on_device_dropped("iosv", -180, 0)
        wait_idle(app, win)
        win.on_device_dropped("iosv", 180, 0)
        wait_idle(app, win)
        win.poll()
        wait_idle(app, win)
        nodes = {n.label: n.key for n in win.view_data.nodes}
        say(f"nodes after palette drops: {sorted(nodes)}")
        win.on_link_requested(nodes["R1"], nodes["R2"])
        wait_idle(app, win)
        win.poll()
        wait_idle(app, win)
        links = [(l.label_a, l.label_b) for l in win.view_data.links]
        say(f"links: {links}")
        ctl.set_config(nodes["R1"], cfg("R1", "10.0.12.1"))
        ctl.set_config(nodes["R2"], cfg("R2", "10.0.12.2"))
        say("startup configs stored (R1 10.0.12.1/30, R2 10.0.12.2/30 on Gi0/0)")
    assert set(nodes) == {"R1", "R2"}, nodes
    assert links == [("Gi0/0", "Gi0/0")], links

    # ------------------------------------------------------------ 2. boot
    win.start_all()
    wait_idle(app, win)
    t0 = time.time()
    booted = False
    while time.time() - t0 < 900:
        win.poll()
        wait_idle(app, win)
        states = {n.label: n.state for n in win.view_data.nodes}
        if all(s == "BOOTED" for s in states.values()):
            booted = True
            break
        pump(app, 5)
    say(f"states after {time.time() - t0:.0f}s: {states}  running counter: "
        f"{win.view_data.running_total}/{win.view_data.limit}")
    assert booted, "routers did not boot"

    # ------------------------------------------------------- 3. console (pexpect)
    cmd = ctl.console_command(nodes["R1"])
    say("console argv: " + " ".join(cmd.argv))
    env = dict(os.environ)
    env.update(cmd.env)
    child = pexpect.spawn(cmd.argv[0], cmd.argv[1:], env=env, encoding="utf-8", timeout=30,
                          dimensions=(50, 160))
    transcript = []
    child.logfile_read = type("T", (), {"write": lambda s, d: transcript.append(d), "flush": lambda s: None})()
    i = child.expect([r"[Pp]assword:", r"Escape character|Connected|\r\n", pexpect.TIMEOUT], timeout=20)
    assert i != 0, "unexpected password prompt"
    say("no password prompt: connected via public key")
    prompt_re = r"R1[>#]"
    for attempt in range(10):
        child.send("\r")
        j = child.expect([prompt_re, r"Press RETURN", r"yes/no\]", pexpect.TIMEOUT], timeout=8)
        if j == 0:
            break
        if j == 2:
            child.send("no\r")
    else:
        raise SystemExit("no router prompt")
    say(f"landed on router prompt: {child.after!r}")
    child.send("enable\r")
    k = child.expect([r"[Pp]assword:", r"R1#"], timeout=10)
    if k == 0:
        child.send("cisco\r")
        child.expect(r"R1#", timeout=10)
    child.send("terminal length 0\r")
    child.expect(r"R1#")
    # wait until Gi0/0 is up/up (link start may lag the boot by a few seconds)
    for _ in range(24):
        child.send("show ip interface brief\r")
        child.expect(r"R1#", timeout=15)
        out = child.before
        if re.search(r"GigabitEthernet0/0\s+10\.0\.12\.1\s+\S+\s+\S+\s+up\s+up", out):
            break
        time.sleep(5)
    say("show ip interface brief ->\n" + "\n".join(l.rstrip() for l in out.splitlines() if l.strip()))
    assert "10.0.12.1" in out

    child.send("sh\t")
    child.expect(r"R1#show ", timeout=10)
    say(f"TAB completion: typed 'sh<TAB>' -> IOS echoed {child.after!r}")
    child.send("\x15")      # Ctrl-U clears the line
    child.send("\r")
    child.expect(r"R1#")
    child.send("show ?")
    child.expect(r"access-lists", timeout=30)
    child.expect(r"R1#show ", timeout=180)      # long list at serial-console speed
    say("'?' help works (context help listed, line restored as 'R1#show ')")
    child.send("\x15\r")
    child.expect(r"R1#")

    ping_ok = False
    for _ in range(6):
        child.send("ping 10.0.12.2\r")
        child.expect(r"Success rate is (\d+) percent.*?\r\n", timeout=30)
        rate = int(child.match.group(1))
        child.expect(r"R1#")
        if rate >= 60:
            ping_ok = True
            break
        time.sleep(5)
    say(f"ping 10.0.12.2 from R1: success rate {rate}%")
    assert ping_ok
    child.sendcontrol("]")           # leave the console via the terminal-server escape
    child.expect(pexpect.EOF, timeout=10)
    child.close()
    say(f"Ctrl-] closed the console session (ssh exit status {child.exitstatus})")

    # ------------------------------------------------------- 4. real Konsole tab
    launcher = launchers.KonsoleLauncher()
    res = launcher.open(ctl.console_command(nodes["R1"]))
    say(f"launcher: {res}")
    svc = launcher.service
    time.sleep(4)
    sessions = launcher.sessions()
    say(f"Konsole sessions over D-Bus: {sessions}")
    assert any(s["title"] == "R1" for s in sessions), sessions
    sid = next(s["id"] for s in sessions if s["title"] == "R1")
    launchers.dbus_call(svc, f"/Sessions/{sid}", "org.kde.konsole.Session", "sendText", "\r")
    time.sleep(2)
    screen = launchers.dbus_call(svc, f"/Sessions/{sid}", "org.kde.konsole.Session", "getAllDisplayedText", "true")
    tail = [l for l in screen.splitlines() if l.strip()][-3:]
    say(f"Konsole tab 'R1' screen tail: {tail}")
    assert any(re.search(prompt_re, l) for l in tail), "router prompt not visible in the Konsole tab"
    fg = launchers.dbus_call(svc, f"/Sessions/{sid}", "org.kde.konsole.Session", "foregroundProcessId")
    try:
        fg_cmd = Path(f"/proc/{fg}/cmdline").read_bytes().split(b"\0")[0].decode()
    except OSError:
        fg_cmd = "?"
    say(f"tab foreground process: pid {fg} ({fg_cmd}) on the tab's pty")
    pid = int(svc.rsplit("-", 1)[1])
    os.kill(pid, 15)
    time.sleep(1.5)
    say(f"closed the test Konsole (pid {pid}); still on bus: {svc in launchers.list_konsole_services()}")

    win.poll()
    wait_idle(app, win)
    win.close()
    say("E2E OK")


if __name__ == "__main__":
    main()
