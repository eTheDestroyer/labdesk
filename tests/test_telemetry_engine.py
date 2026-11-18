"""End-to-end: LabController.poll -> TelemetryEngine (fake CML client, scripted consoles)."""
import copy
import json
import time

import pytest
from fake_console import FakeConsole
from showcase_replay import SHOWCASE, fixture_text

from labdesk_app import config, kvm
from labdesk_app.cml_api import Snapshot
from labdesk_app.controller import LabController
from labdesk_app.facts import CollectorState, LinkKind, P2PSource, PathState
from labdesk_app.telemetry import IOS_FAST, IOS_SLOW, TelemetryEngine

LAB = "fb0393aa-b6de-45fa-b056-f8607420f84f"


class FakeClient:
    def __init__(self):
        d = json.loads((SHOWCASE / "cml_snapshot.json").read_text())
        self.d = d
        self.calls = []
        self.conditions = {}
        self._cred_file = "/nonexistent"

    def snapshot(self, lab_id, with_total=True):
        self.calls.append("snapshot")
        d = copy.deepcopy(self.d)
        return Snapshot(d["lab"], d["nodes"], d["links"], d["interfaces"], 4)

    def lab(self, lab_id):
        self.calls.append("lab")
        return copy.deepcopy(self.d["lab"])

    def lab_element_state(self, lab_id):
        self.calls.append("element_state")
        return {"nodes": {n["id"]: n["state"] for n in self.d["nodes"]},
                "links": {l["id"]: l["state"] for l in self.d["links"]},
                "interfaces": {i["id"]: i.get("state") for i in self.d["interfaces"]}}

    def link_condition(self, lab_id, link_id):
        return self.conditions.get(link_id, {"enabled": False, "latency": None,
                                             "operational": {"latency": None}})

    def set_link_condition(self, lab_id, link_id, cfg):
        self.conditions[link_id] = dict(cfg, operational=dict(cfg))

    def clear_link_condition(self, lab_id, link_id):
        self.conditions.pop(link_id, None)

    def stop_link(self, lab_id, link_id):
        for l in self.d["links"]:
            if l["id"] == link_id:
                l["state"] = "STOPPED"

    def start_link(self, lab_id, link_id):
        for l in self.d["links"]:
            if l["id"] == link_id:
                l["state"] = "STARTED"

    def simulation_stats(self, lab_id):
        return {"links": {l["id"]: {"readbytes": 1000, "writebytes": 1000, "readpackets": 10, "writepackets": 10,
                                    "drops": 0} for l in self.d["links"]}}

    def console_key(self, lab_id, node_id):
        raise AssertionError("console keys come from the node payload")


UUID_TO_LABEL = {"ad62403d-e0e6-4c80-bf90-5dd999ac9a9f": "R1", "225663e3-de05-4d57-9f80-b6c5228c9b68": "R2",
                 "539f48a0-8da7-4be1-bc02-6883ed1d22d6": "R3", "d5711300-1654-4b4c-baeb-e0f7ac73d6c9": "R4"}


def console_factory(uuid, user):
    r = UUID_TO_LABEL[uuid]
    outs = {c: fixture_text(r, c) or "" for c in IOS_FAST + IOS_SLOW + ["show running-config"]}
    return FakeConsole(r, "priv", outs)


@pytest.fixture()
def ctl(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "STATE_FILE", tmp_path / "state.json")
    c = LabController(FakeClient(), config.State(tmp_path / "state.json"), telemetry=False)
    c.telemetry = TelemetryEngine(c.client, transport_factory=console_factory, probe_log_dir=tmp_path / "probe",
                                  console_scan=lambda ex: set(),
                                  creds_loader=lambda label: __import__("labdesk_app.probe.creds",
                                                                        fromlist=["x"]).DeviceCreds())
    c.lab_id = LAB
    yield c
    c.shutdown()


def poll_until(ctl, pred, timeout=30):
    end = time.time() + timeout
    while time.time() < end:
        v = ctl.poll()
        if pred(v):
            return v
        time.sleep(0.3)
    raise AssertionError("condition not reached")


def test_end_to_end_view(ctl):
    v = poll_until(ctl, lambda v: all(n.collector_state == CollectorState.OK
                                      for n in v.telemetry.nodes.values() if n.role.value == "router")
                   and all(n.tele.default_path and n.tele.default_path.state == PathState.EGRESS
                           for n in v.nodes if n.tele and n.tele.role.value == "router"))
    tv = v.telemetry
    lab = {n.key: n.label for n in v.nodes}
    r1 = next(n for n in v.nodes if n.label == "R1")
    kinds = [c.kind for c in r1.chips]
    assert kinds == ["OSPF", "DEFAULT"] and r1.default_chip.text == "O*E2" and r1.default_chip.blue_bar
    assert r1.chips[0].glyph == "✓"
    assert [(b.proto.value, b.role) for b in r1.fhrp] == [("HSRP", "Active"), ("VRRP", "Master")]
    r3 = next(n for n in v.nodes if n.label == "R3")
    assert [c.text for c in r3.chips] == ["OSPF", "BGP 65000", "B* eBGP"]
    assert "R4, AS 65004" in r3.chips[1].tooltip
    r4 = next(n for n in v.nodes if n.label == "R4")
    assert r4.default_chip.text == "S* dhcp"
    shut = next(l for l in v.links if lab[l.a] == "R2" and l.label_a == "Gi0/4")
    assert shut.status.kind == LinkKind.ADMIN_DOWN and shut.status.text == "R2 Gi0/4 shut"
    p2p = next(l for l in v.links if lab[l.a] == "R1" and l.label_a == "Gi0/2")
    assert p2p.p2p and p2p.p2p_source == P2PSource.OSPF_OPER and p2p.default_hop is not None
    assert "R1: OSPF ok (2 neighbours)" in r1.tele.summary and "HSRP Active grp 10" in r1.tele.summary
    # config tier ran (line was at '#') and is redacted
    v = poll_until(ctl, lambda v: next(n for n in v.nodes if n.label == "R1").tele.config_text is not None)
    assert "$9$" not in next(n for n in v.nodes if n.label == "R1").tele.config_text
    # link-down event: hysteresis needs 2 fresh CLI polls before it is logged, then exactly once
    def logged():
        return [e for e in ctl.telemetry.events_since(0) if e.kind == "LINK DOWN" and "R2 Gi0/4" in e.text]
    assert not logged() or len(logged()) == 1
    ctl.telemetry._kick(list(ctl.telemetry.sched))
    poll_until(ctl, lambda v: bool(logged()))
    ctl.telemetry._kick(list(ctl.telemetry.sched))
    time.sleep(2)
    ctl.poll()
    assert len(logged()) == 1 and "R2 side shutdown" in logged()[0].line


def test_light_polling_uses_element_state(ctl):
    ctl.poll()
    ctl.poll()
    ctl.poll()
    assert ctl.client.calls.count("snapshot") == 1 and ctl.client.calls.count("element_state") == 2
    ctl._force_full = True
    ctl.poll()
    assert ctl.client.calls.count("snapshot") == 2


def test_console_open_pauses_probing(ctl):
    ctl.telemetry.console_scan = lambda ex: {"ad62403d-e0e6-4c80-bf90-5dd999ac9a9f"}
    v = ctl.poll()
    r1 = next(n for n in v.nodes if n.label == "R1")
    assert r1.collector_state == CollectorState.PAUSED_CONSOLE and r1.tele.console_open
    time.sleep(3)
    assert "cml:3354b6a5-2be8-46c1-ab8d-a99ec8bb482b" not in ctl.telemetry.facts


def test_cable_pull_and_condition(ctl):
    poll_until(ctl, lambda v: any(n.collector_state == CollectorState.OK for n in v.telemetry.nodes.values()))
    lk = "cml:" + next(l["id"] for l in ctl.client.d["links"] if l["label"].startswith("R1-GigabitEthernet0/2"))
    assert ctl.telemetry.pull_cable(lk).ok
    ctl._force_full = True
    v = ctl.poll()
    st = next(l for l in v.links if l.key == lk).status
    assert st.kind == LinkKind.CABLE_PULLED
    assert any(e.kind == "CABLE" for e in ctl.telemetry.events_since(0))
    assert ctl.telemetry.restore_cable(lk).ok
    r = ctl.telemetry.set_condition(lk, preset="WAN 50 ms")
    assert r.ok and r.text == "50 ms ±5"
    assert ctl.telemetry.clear_condition(lk).ok


def test_no_kvm_means_no_virsh(tmp_path, monkeypatch):
    """D6 zero-cost gate: no KVM devices -> no virsh / kvm call anywhere in poll()."""
    def boom(*a, **kw):
        raise AssertionError("virsh invoked with no KVM devices")
    monkeypatch.setattr(kvm, "run", boom)
    monkeypatch.setattr(kvm, "domstate", boom)
    monkeypatch.setattr(kvm, "mgmt_ip", boom)
    c = LabController(FakeClient(), config.State(tmp_path / "state.json"), telemetry=False)
    c.telemetry = TelemetryEngine(c.client, transport_factory=console_factory, probe_log_dir=tmp_path / "p",
                                  console_scan=lambda ex: set())
    c.lab_id = LAB
    try:
        v = c.poll()
        assert v.telemetry is not None and not any(k.startswith("kvm:") for k in v.telemetry.nodes)
    finally:
        c.shutdown()
