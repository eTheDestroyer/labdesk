"""Helpers: replay tests/fixtures/showcase through parsers -> facts -> join (no I/O beyond files)."""
from __future__ import annotations

import json
from pathlib import Path

from labdesk_app.facts import NodeFacts, ios_facts_update, join_facts
from labdesk_app.telemetry import IOS_FAST, IOS_PARSERS, IOS_SLOW, topology_from_snapshot

SHOWCASE = Path(__file__).resolve().parent / "fixtures" / "showcase"
NOW = 1_000_000.0


def slug(cmd: str) -> str:
    return cmd.replace(" ", "_").replace(".", "_").replace("-", "_").replace("/", "_").replace("|", "").replace("(", "") \
        .replace(")", "").replace("__", "_")


def fixture_text(router: str, cmd: str) -> str | None:
    p = SHOWCASE / f"{router}__{slug(cmd)}.txt"
    return p.read_text() if p.exists() else None


def showcase_topology():
    d = json.loads((SHOWCASE / "cml_snapshot.json").read_text())
    return topology_from_snapshot(d["lab"], d["nodes"], d["links"], d["interfaces"])


def router_facts(topo, ts: float = NOW, overrides: dict | None = None, commands=None) -> dict[str, NodeFacts]:
    """{node_key: NodeFacts} from the fixture files.  overrides: {(router, cmd): text}."""
    out = {}
    for nk, n in topo.nodes.items():
        if n.role.value != "router":
            continue
        nf = NodeFacts(nk, n.label, n.vendor)
        parsed = {}
        for cmd in (commands or (IOS_FAST + IOS_SLOW + ["show running-config"])):
            text = (overrides or {}).get((n.label, cmd), fixture_text(n.label, cmd))
            if text is None:
                continue
            parsed[cmd] = IOS_PARSERS[cmd](text)
        ios_facts_update(nf, parsed, ts)
        out[nk] = nf
    return out


def by_label(topo):
    return {n.label: k for k, n in topo.nodes.items()}


def link_by_labels(topo, a: str, ia: str, b: str, ib: str) -> str:
    from labdesk_app.parsers.common import iface_key
    lab = by_label(topo)
    for k, l in topo.links.items():
        for x, xi, y, yi in ((l.a, l.iface_a, l.b, l.iface_b), (l.b, l.iface_b, l.a, l.iface_a)):
            if x == lab[a] and y == lab[b] and iface_key(xi) == iface_key(ia) and iface_key(yi) == iface_key(ib):
                return k
    raise KeyError((a, ia, b, ib))


def join(topo, facts, history=None, now=NOW, fresh_s=30.0):
    return join_facts(topo, facts, history or {}, now, fresh_s)
