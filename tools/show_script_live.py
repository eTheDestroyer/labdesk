#!/usr/bin/env python3
"""Live, read-only check of per-device show scripts against a running lab.

    .venv/bin/python tools/show_script_live.py --lab "PE Hub-Spoke HSRP" --nodes R1,HUB [--cmd "show ..."]...

For each node: its saved script (show-scripts.json), or the ``--cmd`` lines, or - with neither - the default
suggestion, typed through the real probe path exactly as LabDesk does when a console opens
(``run_burst(plain=True)``: no sentinel lines).  Prints the console transcript (redacted) so you see what
the console window shows.  Nothing is configured; the line is left as it was found (``--stay`` keeps a
logged-out line logged in, like the app does).  Safe next to a running LabDesk: same gating as
accounts_live_check.py, and a node whose console a user has open is skipped.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--lab", default="PE Hub-Spoke HSRP")
    ap.add_argument("--nodes", default="R1")
    ap.add_argument("--cmd", action="append", default=[])
    ap.add_argument("--stay", action="store_true")
    a = ap.parse_args()
    from accounts_live_check import foreign_sessions, user_console_open
    from labdesk_app import show_scripts as SS
    from labdesk_app.cml_api import CMLClient
    from labdesk_app.probe import creds as C
    from labdesk_app.probe.redact import redact, redact_values
    from labdesk_app.probe.session import IosDialect, run_burst
    from labdesk_app.probe.transport import TermserverPty

    cli = CMLClient()
    lab = cli.find_lab(a.lab)
    if not lab or lab.get("state") != "STARTED":
        print(f"lab {a.lab!r} is not running - nothing checked")
        return 2
    rc = 0
    for n in cli.nodes(lab["id"]):
        if n["label"] not in a.nodes.split(","):
            continue
        if n.get("state") != "BOOTED" or n["node_definition"] not in ("iosv", "iosvl2", "iol-xe", "ioll2-xe"):
            print(f"{n['label']}: not a booted IOS device - skipped")
            continue
        saved = SS.load(n["label"])
        cmds = SS.parse("\n".join(a.cmd)) if a.cmd else (saved.commands if saved else
                                                        SS.suggest(None, n["node_definition"] in ("iosvl2", "ioll2-xe")))
        uuid = cli.console_key(lab["id"], n["id"])
        if user_console_open(uuid):
            print(f"{n['label']}: a user console is open - skipped (nothing typed)")
            continue
        t0 = time.time()
        while foreign_sessions(uuid, set()) and time.time() - t0 < 60:
            time.sleep(0.2)
        t = TermserverPty(uuid, "admin")
        t.start()
        mine = {t.pid} if getattr(t, "pid", None) else set()
        quiet = time.time()
        while time.time() - quiet < 4.0 and time.time() - t0 < 90:
            if foreign_sessions(uuid, mine):
                quiet = time.time()
            time.sleep(0.2)
        res = run_burst(t, IosDialect(), cmds, C.load(n["label"]), timeout=SS.burst_timeout(cmds), cmd_timeout=30.0,
                        expect_hostname=n["label"], start=False, plain=True, stay_logged_in=a.stay)
        print(f"\n== {n['label']}: {res.status} in {res.seconds:.1f} s, found at {res.mode_found} "
              f"({len(cmds)} command(s){'; ' + res.reason if res.reason else ''})")
        for c in cmds:
            o = res.outputs.get(c)
            state = "not sent" if o is None else (o.error or f"{len(o.text.splitlines())} lines, {o.seconds:.1f} s")
            print(f"   {c:<34} {state}")
            if o is None or o.text is None or "% Invalid input" in o.text:
                rc = 1
        body = redact_values(redact(res.transcript.replace("\r", ""), "cisco"), C.all_secret_values())
        if "LDK" in body.split("Escape character is", 1)[-1].split(cmds[0], 1)[-1]:
            print("   !! a sentinel line was typed")
            rc = 1
        print("   --- what the console shows (redacted) ---")
        tail = body.split("Escape character is", 1)[-1]
        start = tail.find("terminal length 0")
        print("\n".join("   | " + l for l in tail[max(0, tail.rfind("\n", 0, start)):].strip("\n").split("\n")))
    return rc


if __name__ == "__main__":
    sys.exit(main())
