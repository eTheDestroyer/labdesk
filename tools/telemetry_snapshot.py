#!/usr/bin/env python3
"""Print the telemetry model of a live lab (read-only probes; consoles left as found).

    .venv/bin/python tools/telemetry_snapshot.py [--lab "Protocol Showcase"] [--wait 60] [--json]

No secrets are printed: configs are redacted and credentials are never read into output.
"""
from __future__ import annotations

import argparse
import dataclasses
import enum
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from labdesk_app.controller import LabController  # noqa: E402
from labdesk_app.facts import CollectorState, NodeRole  # noqa: E402


def _plain(o):
    if isinstance(o, enum.Enum):
        return o.value
    if dataclasses.is_dataclass(o):
        return {f.name: _plain(getattr(o, f.name)) for f in dataclasses.fields(o)
                if f.name not in ("config_text", "text", "plaintext")}
    if isinstance(o, dict):
        return {str(_plain(k)) if not isinstance(k, str) else k: _plain(v) for k, v in o.items()}
    if isinstance(o, (list, tuple, set)):
        return [_plain(x) for x in o]
    return o


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--lab", default="Protocol Showcase")
    ap.add_argument("--wait", type=float, default=90.0, help="max seconds to wait for every router")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    ctl = LabController()
    if not ctl.open_lab_by_title(args.lab):
        print(f"lab {args.lab!r} not found", file=sys.stderr)
        return 2
    t0 = time.time()
    v = ctl.poll()
    while time.time() - t0 < args.wait:
        tv = v.telemetry
        routers = [n for n in tv.nodes.values() if n.role == NodeRole.ROUTER or n.switch is not None
                   or n.collector_state not in (CollectorState.NOT_PROBED, CollectorState.OFF)]
        if routers and all(n.collector_state not in (CollectorState.LEARNING,) for n in routers) and \
                all(n.config_text is not None or n.collector_state != CollectorState.OK for n in routers):
            break
        time.sleep(3)
        v = ctl.poll()
    tv = v.telemetry
    lab = {n.key: n.label for n in v.nodes}
    if args.json:
        print(json.dumps(_plain(tv), indent=1, default=str))
        ctl.shutdown()
        return 0
    print(f"== {v.title}  lab={v.lab_state}  CML {v.running_total}/{v.limit} (this lab {v.this_lab_running})"
          f"  probing={tv.probing_enabled}  t={time.time() - t0:.0f}s")
    print("\n-- nodes")
    for n in v.nodes:
        t = n.tele
        chips = "  ".join(f"[{c.text}{(' ' + c.glyph) if c.glyph else ''}{' |blue' if c.blue_bar else ''}]"
                          for c in n.chips)
        badges = " ".join(f"<{b.proto.value} {b.role} g{b.group}>" for b in n.fhrp)
        print(f"{n.label:5} {t.role.value:7} {t.collector_state.value:15} {chips} {badges}")
        if t.role == NodeRole.ROUTER:
            age = f"{t.last_probe_age_s:.0f}s" if t.last_probe_age_s is not None else "-"
            print(f"      rid={t.rid} lo={t.loopback} probe_age={age} cfg={'yes' if t.config_text else 'no'}"
                  f" {t.collector_detail}")
            print(f"      {t.summary}")
            if t.default_path:
                print(f"      default: {t.default_path.state.value} - {t.default_path.detail}")
        if t.switch is not None:
            sw = t.switch
            print(f"      {t.summary}")
            print(f"      stp={sw.stp_mode} root_for={sw.root_for} root_of={sw.root_of} l3={sw.l3} "
                  f"vlans={[(x.id, x.name) for x in sw.vlans]} mac={sw.mac_total}")
            for pr in sw.ports:
                print(f"        {pr['port']:6} {pr['status']:12} {pr['mode']:7} {pr['vlan_text']:18} {pr['stp_text']}")
    print("\n-- links")
    for l in v.links:
        lf = l.tele
        st = l.status
        extra = []
        if l.p2p:
            extra.append(f"P2P({l.p2p_source.value})")
        if l.p2p_conflict:
            extra.append("P2P?")
        if lf.bcast:
            extra.append(f"BCAST({lf.bcast_detail})")
        if l.default_hop:
            extra.append(f"BLUE {lf.default_direction}")
        if lf.protocols:
            extra.append(",".join(f"{p.value}:{h.value}" for p, h in lf.protocols.items()))
        if lf.trunk:
            extra.append(f"TRUNK {lf.trunk_text} native {lf.native_vlan}")
        if lf.access_vlan is not None:
            extra.append(f"V{lf.access_vlan}@{lf.access_side}")
        if lf.stp_block:
            b = lf.stp_block
            extra.append(f"STP-BLK {lab[b.node]} {b.port} {b.role.value} v{b.vlans}")
        if l.condition and l.condition.enabled:
            extra.append(f"cond {l.condition.label}")
        print(f"{lab[l.a]:5} {l.label_a:6} <-> {lab[l.b]:5} {l.label_b:6} {st.kind.value:14} "
              f"{st.text:22} {lf.subnet or '':18} {' '.join(extra)}")
    print("\n-- segments")
    for s in tv.segments.values():
        if s.protocols:
            print(f"{s.kind.value:6} {[lab.get(m[0]) for m in s.members]} "
                  f"{ {p.value: h.value for p, h in s.protocols.items()} } {list(s.detail.values())}")
    print("\n-- events")
    for e in ctl.telemetry.events_since(0)[-20:]:
        print("  " + e.line)
    ctl.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
