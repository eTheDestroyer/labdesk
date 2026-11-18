#!/usr/bin/env python3
"""Live check of the Internet cloud on "Switching Lab" through LabDesk's own controller paths.

    .venv/bin/python tools/cloud_live_check.py [--lab "Switching Lab"] [--router R1]
        [--setup] [--start] [--configure] [--verify] [--pc PC1 [--pc PC2 ...]] [--ping-pc2]

  --setup      drop an Internet cloud (controller.add_device('ext_cloud')) and link the router to it
               (controller.add_link: next free interface, Gi0/1 on R1)
  --start      start the lab and wait until every node is BOOTED
  --configure  controller.cloud_plan + cloud_apply (console config with enable secret 'cisco' + write memory,
               day-0 config / pending patch, return routes file)
  --verify     Verify Internet from the router (telemetry.verify_internet: ping 8.8.8.8 and 1.1.1.1)
  --pc PC1     ping -c 3 8.8.8.8 / 1.1.1.1 on the alpine host's console (and PC2's address with --ping-pc2)

Writes a transcript to qa/cloud_live_log.txt.
"""
from __future__ import annotations

import argparse
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from labdesk_app import cloud  # noqa: E402
from labdesk_app.controller import LabController  # noqa: E402
from labdesk_app.probe.transport import TermserverPty  # noqa: E402

LOG = Path(__file__).resolve().parent.parent / "qa" / "cloud_live_log.txt"


def log(msg: str) -> None:
    line = f"{time.strftime('%H:%M:%S')} {msg}"
    print(line, flush=True)
    with open(LOG, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def node_by_label(ctl: LabController, label: str) -> dict:
    n = next((n for n in ctl.client.nodes(ctl.lab_id) if n.get("label") == label), None)
    if n is None:
        raise SystemExit(f"no node {label}")
    return n


def wait_learned(ctl: LabController, key: str, timeout: float = 240) -> None:
    end = time.time() + timeout
    while time.time() < end:
        v = ctl.poll()
        nv = next((n for n in v.nodes if n.key == key), None)
        if nv is not None and nv.tele is not None and nv.tele.ifaces and nv.tele.default_route is not None:
            return
        time.sleep(3)
    raise SystemExit("router facts not learned in time")


def alpine_run(uuid: str, cmds: list[str], user: str = "admin", timeout: float = 40.0) -> str:
    """Run shell commands on an alpine console (logs in as cisco/cisco if needed, exits if it logged in)."""
    t = TermserverPty(uuid, user)
    t.start()
    buf = ""

    def pump(sec: float) -> None:
        nonlocal buf
        end = time.time() + sec
        while time.time() < end:
            try:
                d = t.read(0.2)
            except Exception:       # noqa: BLE001
                return
            if d:
                buf += d.decode("utf-8", "replace")

    def until(pat: str, sec: float) -> bool:
        end = time.time() + sec
        while time.time() < end:
            if re.search(pat, re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", buf[-400:])):
                return True
            pump(0.3)
        return False

    logged_in = False
    try:
        until(r"Escape character is", 8)
        pump(0.8)
        buf = ""
        t.write(b"\r")
        pump(1.5)
        if re.search(r"login:\s*$", buf):
            t.write(b"cisco\r")
            until(r"[Pp]assword:\s*$", 5)
            t.write(b"cisco\r")
            logged_in = True
            until(r"[$#] $", 8)
        out = []
        for c in cmds:
            buf = ""
            t.write(c.encode() + b"\r")
            time.sleep(0.3)
            until(r"\n[^\n]*[$#] $", timeout)
            out.append(buf)
        if logged_in:
            t.write(b"exit\r")
            pump(0.5)
        return "".join(out).replace("\r", "")
    finally:
        t.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--lab", default="Switching Lab")
    ap.add_argument("--router", default="R1")
    ap.add_argument("--setup", action="store_true")
    ap.add_argument("--start", action="store_true")
    ap.add_argument("--configure", action="store_true")
    ap.add_argument("--verify", action="store_true")
    ap.add_argument("--pc", action="append", default=[])
    ap.add_argument("--ping-pc2", action="store_true")
    args = ap.parse_args()
    ctl = LabController()
    if not ctl.open_lab_by_title(args.lab):
        raise SystemExit(f"lab {args.lab!r} not found")
    r = node_by_label(ctl, args.router)
    rkey = f"cml:{r['id']}"
    log(f"== lab {args.lab} ({ctl.lab_id}) router {args.router} state {r['state']}")
    h = cloud.check_health()
    log(f"cloud health: {h.headline} | " + " | ".join(h.tooltip_lines()[1:]))
    try:
        if args.setup:
            clouds = [n for n in ctl.client.nodes(ctl.lab_id) if cloud.is_cloud_node(n)]
            if clouds:
                ckey = f"cml:{clouds[0]['id']}"
                log(f"cloud node exists: {clouds[0]['label']}")
            else:
                ckey = ctl.add_device("ext_cloud", r.get("x", 0) + 300, r.get("y", 0) - 40)
                log(f"added Internet cloud {ctl.label_of(ckey)} ({ckey})")
            if ctl.cloud_link_of(rkey) is None:
                res = ctl.add_link(rkey, ckey)
                log(f"{res.text}; needs_restart={[l for _, l in res.needs_restart]}")
            else:
                log(f"{args.router} already linked: {ctl.cloud_link_of(rkey)[1]}")
            ctl.poll()
        if args.start:
            log(ctl.start_all() or "start_all: ok")
            keys = [f"cml:{n['id']}" for n in ctl.client.nodes(ctl.lab_id)
                    if n.get("node_definition") not in ("external_connector", "unmanaged_switch")]
            ok = ctl.wait_booted(keys, timeout=900)
            log(f"all booted: {ok}")
        if args.configure or args.verify:
            wait_learned(ctl, rkey)
        if args.configure:
            plan = ctl.cloud_plan(rkey)
            log(f"plan: {plan.router_label} {plan.iface} {plan.ip}/{plan.conf.prefixlen} reused={plan.reused} "
                f"ospf={plan.ospf} booted={plan.booted} console_open={plan.console_open} day0_now={plan.day0_now} "
                f"day0_has={plan.day0_has} needs_wipe={plan.needs_wipe} routes: {plan.routes.text}")
            for line in plan.commands:
                log(f"   | {line}")
            restart = plan.needs_wipe
            for ok, text in ctl.cloud_apply(plan, live=not restart, day0=True, ospf=bool(plan.ospf), routes=True,
                                            restart=restart):
                log(f"apply {'OK ' if ok else 'ERR'} {text}")
            if restart:
                log(f"day-0 config of {args.router} after the wipe:")
                for line in ctl.get_config(rkey).splitlines():
                    log(f"   day0| {line}")
                log(f"booted again: {ctl.wait_booted([rkey], 600)}")
                ctl.telemetry.reset(ctl.lab_id)
                wait_learned(ctl, rkey)
            time.sleep(2)
            for _ in range(40):
                v = ctl.poll()
                nv = next(n for n in v.nodes if n.key == rkey)
                p = nv.tele.default_path if nv.tele else None
                if p is not None and p.state.value in ("EGRESS", "VERIFIED_INTERNET"):
                    break
                time.sleep(3)
            log(f"default path: {p.state.value if p else None} - {p.detail if p else ''}")
            blue = [l for l in v.links if l.default_hop is not None]
            log(f"blue links: {[(ctl.label_of(l.a), l.label_a, ctl.label_of(l.b), l.label_b) for l in blue]}")
            for l in v.links:
                if l.addressing is not None and "100.64" in (l.addressing.label or ""):
                    log(f"subnet label on {ctl.label_of(l.a)}-{ctl.label_of(l.b)}: {l.addressing.label} "
                        f"({l.addressing.host_a or '-'} / {l.addressing.host_b or '-'})")
        if args.verify:
            res = ctl.telemetry.verify_internet(rkey)
            log(f"Verify Internet from {args.router}: ok={res.ok} {res.text}")
            for tgt, d in (res.data.get("targets") or {}).items():
                log(f"   {tgt}: {d['received']}/{d['sent']} success={d['success']} avg={d['rtt_avg_ms']} ms")
            v = ctl.poll()
            nv = next(n for n in v.nodes if n.key == rkey)
            log(f"path after verify: {nv.tele.default_path.state.value}")
        for pc in args.pc:
            n = node_by_label(ctl, pc)
            uuid = ctl.client.console_key(ctl.lab_id, n["id"])
            cmds = ["ping -c 3 -W 2 8.8.8.8", "ping -c 3 -W 2 1.1.1.1"]
            if args.ping_pc2:
                cmds.append("ping -c 3 -W 2 192.168.20.10")
            out = alpine_run(uuid, cmds)
            for line in out.splitlines():
                if line.strip():
                    log(f"   {pc}| {line}")
    finally:
        ctl.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
