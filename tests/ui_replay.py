"""Offline replay controller for UI tests and QA screenshots.

A LabController whose CML client and router consoles are fakes fed from
tests/fixtures/showcase (the live Protocol Showcase captures).  No network, no ssh.
"""
from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
if str(HERE.parent) not in sys.path:
    sys.path.insert(0, str(HERE.parent))

from test_telemetry_engine import LAB, FakeClient, console_factory  # noqa: E402

from labdesk_app import config  # noqa: E402
from labdesk_app.controller import LabController  # noqa: E402
from labdesk_app.facts import CollectorState, PathState  # noqa: E402
from labdesk_app.probe.creds import DeviceCreds  # noqa: E402
from labdesk_app.telemetry import TelemetryEngine  # noqa: E402


class ReplayClient(FakeClient):
    """FakeClient + the few read calls the main window makes."""

    def running_node_count(self):
        return 4

    def system_information(self):
        return {"version": "2.10.1 (replay)"}

    def is_ready(self):
        return True

    def labs(self):
        return [self.d["lab"]]


def replay_controller(tmp: Path | None = None) -> LabController:
    tmp = Path(tmp or tempfile.mkdtemp(prefix="labdesk-replay-"))
    config.STATE_FILE = tmp / "state.json"
    ctl = LabController(ReplayClient(), config.State(tmp / "state.json"), telemetry=False)
    ctl.telemetry = TelemetryEngine(ctl.client, transport_factory=console_factory, probe_log_dir=tmp / "probe",
                                    console_scan=lambda ex: set(), creds_loader=lambda label: DeviceCreds())
    ctl.lab_id = LAB
    ctl.lab_title = "Protocol Showcase"
    return ctl


def settled(view) -> bool:
    tv = view.telemetry
    if tv is None:
        return False
    routers = [n for n in tv.nodes.values() if n.role.value == "router"]
    return bool(routers) and all(n.collector_state == CollectorState.OK for n in routers) and all(
        n.default_path is not None and n.default_path.state in (PathState.EGRESS, PathState.VERIFIED_INTERNET)
        for n in routers)


def poll_until_settled(ctl, timeout: float = 40.0):
    end = time.time() + timeout
    v = ctl.poll()
    while time.time() < end and not settled(v):
        time.sleep(0.3)
        v = ctl.poll()
    return v
