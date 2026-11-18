"""Offline replay of the live EtherChannel captures (tests/fixtures/etherchannel): the "Switching Lab" with
Po1 (LACP, SW1 active / SW2 passive, Gi0/1+Gi0/2) and Po2 (PAgP, SW1 desirable / SW2 auto, Gi1/0+Gi1/1).

States (file prefix): ``healthy``, ``partial`` (SW1 Gi0/2 shut), ``fail_on`` (SW2 Po1 members 'mode on'),
``fail_proto`` (SW2 Po1 members PAgP desirable), ``fail_passive`` (both sides LACP passive).

Two levels:
* ``join_state(state)``: pure - parse every capture, merge into NodeFacts and run ``facts.join_facts``;
* ``replay_controller(state)``: the real TelemetryEngine behind scripted consoles (same as switch_replay).
"""
from __future__ import annotations

import json
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
for p in (HERE, HERE.parent):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from fake_console import FakeConsole  # noqa: E402
from showcase_replay import slug  # noqa: E402
from test_telemetry_engine import FakeClient  # noqa: E402

from labdesk_app import config  # noqa: E402
from labdesk_app.controller import LabController  # noqa: E402
from labdesk_app.facts import (EC_CMDS, L2_CMDS, NodeFacts, ec_facts_update, ios_facts_update,  # noqa: E402
                               join_facts, l2_facts_update)
from labdesk_app.parsers import ios_l2 as P_L2  # noqa: E402
from labdesk_app.probe.creds import DeviceCreds  # noqa: E402
from labdesk_app.telemetry import (FRESH_S, IOS_FAST, IOS_PARSERS, IOS_SLOW, TelemetryEngine,  # noqa: E402
                                   topology_from_snapshot)

EC_DIR = HERE / "fixtures" / "etherchannel"
SWITCHING = HERE / "fixtures" / "switching"
SNAP = json.loads((EC_DIR / "cml_snapshot.json").read_text())
LAB = SNAP["lab"]["id"]
STATES = ("healthy", "partial", "fail_on", "fail_proto", "fail_passive")
LABEL_KEY = {n["label"]: f"cml:{n['id']}" for n in SNAP["nodes"]}


def fixture(state: str, label: str, cmd: str) -> str | None:
    p = EC_DIR / f"{state}__{label}__{slug(cmd)}.txt"
    if not p.exists() and not label.startswith("SW"):
        p = EC_DIR / f"healthy__{label}__{slug(cmd)}.txt"          # R1 was captured once (healthy)
        if not p.exists():
            p = SWITCHING / f"{label}__{slug(cmd)}.txt"
    return p.read_text() if p.exists() else None


def topology():
    return topology_from_snapshot(SNAP["lab"], SNAP["nodes"], SNAP["links"], SNAP["interfaces"])[0]


def node_facts(state: str, label: str, now: float) -> NodeFacts:
    nk = LABEL_KEY[label]
    nf = NodeFacts(nk, label)
    parsed = {}
    for cmd, fn in IOS_PARSERS.items():
        t = fixture(state, label, cmd)
        if t is not None:
            parsed[cmd] = fn(t)
    if label.startswith("SW"):
        rc = fixture(state, label, "show ip route connected")
        if rc is not None:
            parsed[L2_CMDS["routing"]] = P_L2.parse_routing_enabled(rc)
    ios_facts_update(nf, parsed, now)
    if label.startswith("SW"):
        l2_facts_update(nf, parsed, now)
    ec_facts_update(nf, parsed, now)
    return nf


def join_state(state: str, now: float | None = None):
    now = now or time.time()
    topo = topology()
    facts = {LABEL_KEY[lbl]: node_facts(state, lbl, now) for lbl in ("SW1", "SW2", "R1")}
    return topo, facts, join_facts(topo, facts, {}, now, FRESH_S)


def link_by_ports(topo, a_label: str, a_port: str) -> str:
    from labdesk_app.parsers.common import iface_key
    nk = LABEL_KEY[a_label]
    for lk, l in topo.links.items():
        if (l.a == nk and iface_key(l.iface_a) == iface_key(a_port)) or \
                (l.b == nk and iface_key(l.iface_b) == iface_key(a_port)):
            return lk
    raise KeyError(a_port)


# ------------------------------------------------------------------ full engine replay
UUID_TO_LABEL = {sc["console_key"]: n["label"] for n in SNAP["nodes"]
                 for sc in ((n.get("operational") or {}).get("serial_consoles") or []) if sc.get("device_number") == 0}


class EcClient(FakeClient):
    def __init__(self):
        super().__init__()
        self.d = json.loads(json.dumps(SNAP))

    def running_node_count(self):
        return 5

    def system_information(self):
        return {"version": "2.10.1 (replay)"}

    def is_ready(self):
        return True

    def labs(self):
        return [self.d["lab"]]

    # day-0 configs (the real CML node configurations of SW1 / SW2 after the EtherChannel build)
    day0_override: dict = {}

    def node(self, lab_id, node_id):
        return next(n for n in self.d["nodes"] if n["id"] == node_id)

    def node_configuration(self, lab_id, node_id):
        lbl = self.node(lab_id, node_id)["label"]
        if lbl in self.day0_override:
            return self.day0_override[lbl]
        p = EC_DIR / f"{lbl}__day0.txt"
        return p.read_text() if p.exists() else ""

    def node_state(self, lab_id, node_id):
        return self.node(lab_id, node_id).get("state", "BOOTED")


class StateBox:
    """Which capture set the scripted consoles answer with (switch it mid-run for transitions)."""

    def __init__(self, state: str = "healthy"):
        self.state = state


def console_factory(box: StateBox):
    def make(uuid, user):
        label = UUID_TO_LABEL[uuid]
        cmds = list(IOS_PARSERS) + list(EC_CMDS.values()) + ["show ip route connected"]
        if not label.startswith("SW"):
            cmds = IOS_FAST + IOS_SLOW
        outs = {}
        for c in cmds:
            t = fixture(box.state, label, c)
            if t is not None:
                outs[c] = t
        return FakeConsole(label, "user", outs)
    return make


def replay_controller(state: str = "healthy", tmp: Path | None = None) -> tuple[LabController, StateBox]:
    tmp = Path(tmp or tempfile.mkdtemp(prefix="labdesk-ecreplay-"))
    config.STATE_FILE = tmp / "state.json"
    box = StateBox(state)
    ctl = LabController(EcClient(), config.State(tmp / "state.json"), telemetry=False)
    ctl.telemetry = TelemetryEngine(ctl.client, transport_factory=console_factory(box),
                                    probe_log_dir=tmp / "probe", console_scan=lambda ex: set(),
                                    creds_loader=lambda label: DeviceCreds())
    ctl.lab_id = LAB
    ctl.lab_title = "Switching Lab"
    return ctl, box


def settled(view) -> bool:
    tv = view.telemetry
    if tv is None:
        return False
    sws = [n for n in tv.nodes.values() if n.switch is not None]
    if len(sws) != 2 or not all(n.switch.stp for n in sws):
        return False
    return bool(tv.bundles) and all(
        (s.side_a is None or s.side_a.members and all(m.mode for m in s.side_a.members if m.mode != "on"))
        for s in tv.bundles.values()) and all(len(n.switch.ec_sides) >= 1 for n in sws)


def poll_until_settled(ctl, timeout: float = 60.0):
    end = time.time() + timeout
    v = ctl.poll()
    while time.time() < end and not settled(v):
        time.sleep(0.3)
        v = ctl.poll()
    return v
