#!/usr/bin/env python3
"""Live, read-only check of the on-device account parser against the running "Switching Lab".

    .venv/bin/python tools/accounts_live_check.py [--nodes R1,SW1] [--enable-prompt | --enable-env VAR]

For each node: the CONFIG tier's burst (FAST commands + 'show running-config', same timeout) through the real
probe path (probe.transport.TermserverPty + probe.session.run_burst: 'enable' only because the running-config
needs it, 'disable' + terminal restore afterwards).  Nothing is configured.  Safe next to a running LabDesk: the console
start is gated (waits until no other ssh session is on that console, then 4 s of quiet so the other instance's
/proc scan pauses that node), and a node whose console a user has open is skipped.

The enable secret is read from device-creds (``creds.load``) or, with ``--enable-prompt``, typed at a hidden
prompt / from an environment variable; it is never printed or stored.  Output: account names and how each password is stored (no values, no
hashes), plus a check that the redacted transcript holds no plain-text password.
"""
from __future__ import annotations

import argparse
import getpass
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def foreign_sessions(uuid: str, mine: set[int]) -> list[int]:
    """ssh 'connect <uuid>' processes that are not ours (another LabDesk's probe, or a user's console)."""
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
    """A user's console (ssh under tabexec.py or a shell); probe sessions (ssh under python) do not count."""
    for pid in foreign_sessions(uuid, set()):
        try:
            ppid = int(Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[1])
            parent = (Path(f"/proc/{ppid}/cmdline").read_bytes().split(b"\0") or [b""])
        except (OSError, IndexError, ValueError):
            continue
        if any(b"tabexec" in a for a in parent) or b"python" not in parent[0]:
            return True
    return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--lab", default="Switching Lab")
    ap.add_argument("--nodes", default="R1,SW1")
    ap.add_argument("--enable-prompt", action="store_true")
    ap.add_argument("--enable-env", help="read the enable secret from this environment variable")
    a = ap.parse_args()
    from labdesk_app import accounts as A
    from labdesk_app.cml_api import CMLClient
    from labdesk_app.probe import creds as C
    from labdesk_app.probe.redact import redact, redact_values
    from labdesk_app.probe.session import IosDialect, run_burst
    from labdesk_app.probe.transport import TermserverPty
    from labdesk_app.telemetry import (CONFIG_TIMEOUT_S, EC_EXTRA_TIMEOUT_S, EC_FAST, IOS_CONFIG, IOS_FAST,
                                       SW_FAST)

    cli = CMLClient()
    lab = cli.find_lab(a.lab)
    if not lab or lab.get("state") != "STARTED":
        print(f"lab {a.lab!r} is not running - nothing checked (it is not started by this tool)")
        return 2
    prompt_secret = getpass.getpass("enable secret (hidden): ") if a.enable_prompt else \
        (os.environ.get(a.enable_env) or None if a.enable_env else None)
    rc = 0
    for n in cli.nodes(lab["id"]):
        if n["label"] not in a.nodes.split(","):
            continue
        if n.get("state") != "BOOTED":
            print(f"{n['label']}: not booted - skipped")
            continue
        uuid = cli.console_key(lab["id"], n["id"])
        if user_console_open(uuid):
            print(f"{n['label']}: a user console is open - skipped (nothing typed)")
            continue
        cr = C.load(n["label"])
        if prompt_secret:
            cr = C.DeviceCreds(cr.username, cr.password, prompt_secret)
        t0 = time.time()
        while foreign_sessions(uuid, set()) and time.time() - t0 < 60:
            time.sleep(0.2)                      # the other LabDesk's probe burst is still on that line
        t = TermserverPty(uuid, "admin")
        t.start()
        mine = {t.pid} if getattr(t, "pid", None) else set()
        quiet = time.time()
        while time.time() - quiet < 4.0 and time.time() - t0 < 90:
            if foreign_sessions(uuid, mine):
                quiet = time.time()
            time.sleep(0.2)
        # exactly the CONFIG tier's burst: FAST rides along, then 'show running-config'
        sw = n["node_definition"] in ("iosvl2", "ioll2-xe")
        cmds = (SW_FAST + EC_FAST if sw else IOS_FAST) + IOS_CONFIG
        res = run_burst(t, IosDialect(), cmds, cr, timeout=CONFIG_TIMEOUT_S + (EC_EXTRA_TIMEOUT_S if sw else 0.0),
                        expect_hostname=n["label"], start=False)
        out = res.outputs.get("show running-config")
        rco = res.outputs.get("show running-config")
        print(f"\n== {n['label']} ({n['node_definition']}): burst {res.status} in {res.seconds:.1f} s "
              f"(running-config {rco.seconds if rco else 0:.1f} s), "
              f"found at {res.mode_found}, enabled={res.enabled}")
        if out is None or out.text is None:
            print(f"   running-config not read: {(out.error if out else None) or res.reason}")
            if os.environ.get("LDK_DEBUG"):
                tail = redact_values(redact(res.transcript, "cisco"), C.all_secret_values() +
                                     ([prompt_secret] if prompt_secret else []))
                print("   --- redacted transcript tail ---\n" + tail[-1500:])
            rc = 1
            continue
        acc = A.parse_ios_accounts(out.text)
        for x in acc.accounts:
            prv = f" (privilege {x.privilege})" if x.privilege is not None else ""
            print(f"   {x.name:<22} {x.detail}{prv}")
        print(f"   service password-encryption: {acc.password_encryption}")
        secrets = C.all_secret_values() + acc.plaintexts() + ([prompt_secret] if prompt_secret else [])
        clean = redact_values(redact(res.transcript, "cisco"), secrets)
        leaked = [s for s in acc.plaintexts() if s in clean]
        hashes = "$9$" in clean or "$8$" in clean or "$1$" in clean
        print(f"   redacted transcript: {'LEAKS a plain-text password' if leaked else 'no plain-text password'}"
              f", {'hash present' if hashes else 'no hash'}")
        rc |= int(bool(leaked or hashes))
    return rc


if __name__ == "__main__":
    sys.exit(main())
