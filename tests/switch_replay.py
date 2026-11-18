"""Offline replay of the live Switching Lab captures (tests/fixtures/switching): SW1/SW2 IOSvL2,
R1 router-on-a-stick, PC1/PC2 alpine.  Fake CML client + scripted consoles at the user-exec '>'
prompt (exactly how the probes found the real lab).  No network, no ssh.

``overrides``: {(label, command): text} replaces one capture, e.g. the 'after shut' captures.
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
from labdesk_app.facts import CollectorState  # noqa: E402
from labdesk_app.probe.creds import DeviceCreds  # noqa: E402
from labdesk_app.telemetry import (IOS_FAST, IOS_SLOW, SW_FAST, SW_SLOW, TelemetryEngine,  # noqa: E402
                                   topology_from_snapshot)

SWITCHING = HERE / "fixtures" / "switching"
SNAP = json.loads((SWITCHING / "cml_snapshot.json").read_text())
LAB = SNAP["lab"]["id"]


def fixture(label: str, cmd: str, prefix: str = "") -> str | None:
    p = SWITCHING / f"{prefix}{label}__{slug(cmd)}.txt"
    return p.read_text() if p.exists() else None


def switching_topology():
    return topology_from_snapshot(SNAP["lab"], SNAP["nodes"], SNAP["links"], SNAP["interfaces"])


UUID_TO_LABEL = {sc["console_key"]: n["label"] for n in SNAP["nodes"]
                 for sc in ((n.get("operational") or {}).get("serial_consoles") or []) if sc.get("device_number") == 0}


class SwitchingClient(FakeClient):
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


def console_factory(overrides: dict | None = None, prefix: str = ""):
    def make(uuid, user):
        label = UUID_TO_LABEL[uuid]
        cmds = SW_FAST + SW_SLOW if label.startswith("SW") else IOS_FAST + IOS_SLOW
        outs = {}
        for c in cmds:
            t = (overrides or {}).get((label, c)) or fixture(label, c, prefix) or fixture(label, c)
            if t is not None:
                outs[c] = t
        return FakeConsole(label, "user", outs)
    return make


def replay_controller(tmp: Path | None = None, overrides: dict | None = None, prefix: str = "") -> LabController:
    tmp = Path(tmp or tempfile.mkdtemp(prefix="labdesk-swreplay-"))
    config.STATE_FILE = tmp / "state.json"
    ctl = LabController(SwitchingClient(), config.State(tmp / "state.json"), telemetry=False)
    ctl.telemetry = TelemetryEngine(ctl.client, transport_factory=console_factory(overrides, prefix),
                                    probe_log_dir=tmp / "probe", console_scan=lambda ex: set(),
                                    creds_loader=lambda label: DeviceCreds())
    ctl.lab_id = LAB
    ctl.lab_title = "Switching Lab"
    return ctl


def settled(view) -> bool:
    tv = view.telemetry
    if tv is None:
        return False
    sws = [n for n in tv.nodes.values() if n.switch is not None]
    probed = [n for n in tv.nodes.values() if n.role.value == "router"] + sws
    return len(sws) == 2 and all(n.collector_state == CollectorState.OK for n in probed) and \
        all(n.switch.stp and n.switch.mac_total is not None for n in sws)


def poll_until_settled(ctl, timeout: float = 40.0):
    end = time.time() + timeout
    v = ctl.poll()
    while time.time() < end and not settled(v):
        time.sleep(0.3)
        v = ctl.poll()
    return v
