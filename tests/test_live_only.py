"""Live-only rule: everything LabDesk shows about a device comes from live device output.

A node whose only information is its day-0 config / CML's intended interface data (lab YAML) shows "not read
yet": no subnet labels, no protocol chips, no default route (blue), no confirmed interface names.  The same
facts appear once a live probe result arrives.  The host-KVM vJunos collector reads live over SSH (fixtures are
real captures from the 'PE Hub-Spoke HSRP' lab's PE, read-only commands)."""
import copy
import json
import time
from pathlib import Path

import pytest
from fake_console import FakeConsole
from showcase_replay import SHOWCASE, fixture_text

from labdesk_app import config
from labdesk_app.cml_api import Snapshot
from labdesk_app.controller import LabController
from labdesk_app.facts import CollectorState, LinkKind, LinkSide
from labdesk_app.parsers import junos as J
from labdesk_app.probe import junos_ssh as JS
from labdesk_app.probe.transport import TransportError
from labdesk_app.telemetry import IOS_FAST, IOS_SLOW, JUNOS_AS_CMD, JUNOS_LIVE, JUNOS_MAC_CMD, TelemetryEngine, \
    topology_from_snapshot
from labdesk_app.ui import hovercard_model as H

LAB = "fb0393aa-b6de-45fa-b056-f8607420f84f"
JL = Path(__file__).parent / "fixtures" / "junos_live"

DAY0 = """hostname {label}
interface GigabitEthernet0/1
 ip address 10.99.{n}.1 255.255.255.252
 ip ospf network point-to-point
router ospf 1
 network 10.0.0.0 0.255.255.255 area 0
router bgp 65099
 neighbor 10.99.{n}.2 remote-as 65100
ip route 0.0.0.0 0.0.0.0 10.99.{n}.2
username admin privilege 15 secret cisco
"""


class Day0Client:
    """A CML client whose nodes carry rich day-0 configs and whose interfaces carry CML-snooped addresses -
    none of which may ever reach the screen."""

    def __init__(self):
        d = json.loads((SHOWCASE / "cml_snapshot.json").read_text())
        for i, n in enumerate(d["nodes"]):
            if n["node_definition"] == "iosv":
                n["configuration"] = DAY0.format(label=n["label"], n=i)
        for i, itf in enumerate(d["interfaces"]):
            itf["ip4"] = [f"172.31.{i}.9"]                 # CML L3 snooping style data
            itf["mac_address"] = f"52:54:00:00:00:{i:02x}"
        self.d = d

    def snapshot(self, lab_id, with_total=True):
        d = copy.deepcopy(self.d)
        return Snapshot(d["lab"], d["nodes"], d["links"], d["interfaces"], 4)

    def lab(self, lab_id):
        return copy.deepcopy(self.d["lab"])

    def lab_element_state(self, lab_id):
        return {"nodes": {n["id"]: n["state"] for n in self.d["nodes"]},
                "links": {l["id"]: l["state"] for l in self.d["links"]},
                "interfaces": {i["id"]: i.get("state") for i in self.d["interfaces"]}}

    def link_condition(self, lab_id, link_id):
        return {"enabled": False, "operational": {}}

    def simulation_stats(self, lab_id):
        return {"links": {}}

    def layer3_addresses(self, lab_id):
        raise AssertionError("CML L3 snooping is not device output and must not be read")

    def console_key(self, lab_id, node_id):
        raise AssertionError("console keys come from the node payload")


UUID_TO_LABEL = {"ad62403d-e0e6-4c80-bf90-5dd999ac9a9f": "R1", "225663e3-de05-4d57-9f80-b6c5228c9b68": "R2",
                 "539f48a0-8da7-4be1-bc02-6883ed1d22d6": "R3", "d5711300-1654-4b4c-baeb-e0f7ac73d6c9": "R4"}


class DeadConsole:
    """The device does not answer (stopped / not reachable yet)."""
    pid = None

    def __init__(self, *a):
        pass

    def start(self):
        raise TransportError("console not reachable")

    def close(self):
        pass


class Switchable:
    def __init__(self):
        self.live = False

    def __call__(self, uuid, user):
        if not self.live:
            return DeadConsole()
        r = UUID_TO_LABEL[uuid]
        outs = {c: fixture_text(r, c) or "" for c in IOS_FAST + IOS_SLOW + ["show running-config"]}
        return FakeConsole(r, "priv", outs)


@pytest.fixture()
def ctl(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "STATE_FILE", tmp_path / "state.json")
    sw = Switchable()
    c = LabController(Day0Client(), config.State(tmp_path / "state.json"), telemetry=False)
    c.first_login_backfill = False
    c.telemetry = TelemetryEngine(c.client, transport_factory=sw, probe_log_dir=tmp_path / "probe",
                                  console_scan=lambda ex: set(),
                                  creds_loader=lambda label: __import__("labdesk_app.probe.creds",
                                                                        fromlist=["x"]).DeviceCreds())
    c.lab_id = LAB
    c._switch = sw
    yield c
    c.shutdown()


def _routers(v):
    return [n for n in v.nodes if n.dtype.node_definition == "iosv"]


def test_day0_only_node_shows_not_read_yet(ctl):
    v = ctl.poll()
    time.sleep(1.0)                 # the (failing) first bursts ran
    v = ctl.poll()
    for nv in _routers(v):
        assert nv.chips == [] and nv.fhrp == [] and nv.default_chip is None, nv.label
        assert nv.tele.ifaces == [] and nv.tele.rid is None and nv.tele.bgp is None
        card = H.build_card(nv, creds=None)
        assert card.section("Interfaces") is None and card.section("Protocols") is None
        live = next(r for r in card.meta if r.text == "Live data")
        assert live.value == "not read yet", card.text()
        assert "10.99." not in card.text() and "65099" not in card.text() and "172.31." not in card.text()
    for lv in v.links:
        assert lv.addressing is None or not lv.addressing.label, lv.key    # no subnet tag from day-0 / CML
        assert lv.default_hop is None and not lv.p2p                          # no blue, no P2P bead
        assert not lv.live_a or not lv.live_b                                 # names not confirmed by a device
    rl = [lv for lv in v.links if any(n.key in (lv.a, lv.b) for n in _routers(v))]
    assert rl and all(lv.status.kind in (LinkKind.UNKNOWN,) for lv in rl)


def test_facts_appear_once_live_probe_arrives(ctl):
    ctl.poll()
    ctl._switch.live = True
    for s in ctl.telemetry.sched.values():
        s.next_fast = s.next_slow = 0.0
        s.failures = 0
    end = time.time() + 30
    while time.time() < end:
        v = ctl.poll()
        if all(n.collector_state == CollectorState.OK for n in _routers(v)) and \
                any(lv.addressing is not None and lv.addressing.label for lv in v.links):
            break
        time.sleep(0.3)
    else:
        raise AssertionError("live facts never arrived")
    r1 = next(n for n in v.nodes if n.label == "R1")
    assert [c.kind for c in r1.chips][:1] == ["OSPF"]
    card = H.build_card(r1, creds=None)
    assert next(r for r in card.meta if r.text == "Live data").value.startswith("read ")
    assert card.section("Interfaces") is not None
    assert "10.99." not in card.text()                     # still nothing from the day-0 config
    assert any(lv.live_a and lv.live_b for lv in v.links)


# ------------------------------------------------------------------ host-KVM vJunos (live SSH collector)
def _fixture_runner(calls):
    files = {"show interfaces terse | display json": "interfaces_terse.json",
             "show bgp summary | display json": "bgp_summary.json",
             "show route 0.0.0.0/0 exact | display json": "route_default.json",
             "show ospf neighbor | display json": "ospf_neighbor.json",
             JUNOS_MAC_CMD: "iface_macs.txt", JUNOS_AS_CMD: "local_as.json"}

    def run(host, user, pw, cmds, timeout):
        calls.append((host, list(cmds)))
        for c in cmds:
            JS.check_read_only(c)
        outs = {c: JS.JunosOutput((JL / files[c]).read_text() if c in files else "") for c in cmds}
        return JS.JunosBurst(True, outs)
    return run


def _pe_topo(mgmt):
    nodes = [{"id": "c1", "label": "PE-HUB", "node_definition": "external_connector", "state": "BOOTED",
              "configuration": "System Bridge"}]
    kvm_links = [{"a": "kvm:PE", "b": "cml:c1", "label_a": "ge-0/0/7", "label_b": "",
                  "mac_a": "52:54:00:0f:8a:90"}]
    return topology_from_snapshot({"state": "STARTED"}, nodes, [], [], {"PE": {"kind": "vjunos"}}, kvm_links,
                                  {"PE": "running"}, mgmt)[0]


def test_pe_without_collector_is_not_read():
    eng = TelemetryEngine(None, console_scan=lambda ex: set())
    try:
        tv = eng.update(_pe_topo({}), "lab")
        pe = tv.nodes["kvm:PE"]
        assert pe.collector_state == CollectorState.UNVERIFIED and pe.chips == [] and pe.ifaces == []
        lf = tv.links["kvm:0"]
        assert lf.status.kind == LinkKind.UNKNOWN and lf.side_a == LinkSide.UNKNOWN and lf.status.text == "not read"
    finally:
        eng.shutdown()


def test_pe_live_junos_read():
    calls = []
    eng = TelemetryEngine(None, console_scan=lambda ex: set())
    eng.junos_runner = _fixture_runner(calls)
    eng.junos_creds = lambda label: ("admin", "x", "test")
    eng.kvm_mgmt = {"kvm:PE": "192.168.122.238"}
    try:
        topo = _pe_topo({"kvm:PE": "192.168.122.238"})
        tv = eng.update(topo, "lab")
        assert tv.nodes["kvm:PE"].chips == []                       # nothing before the first read
        end = time.time() + 10
        while time.time() < end and not (eng.facts.get("kvm:PE") and eng.facts["kvm:PE"].ifaces):
            time.sleep(0.05)
        tv = eng.update(topo, "lab")
        pe = tv.nodes["kvm:PE"]
        assert calls and calls[0][0] == "192.168.122.238"
        assert set(calls[0][1]) == set(JUNOS_LIVE["fast"] + JUNOS_LIVE["slow"])
        assert pe.collector_state == CollectorState.OK
        assert pe.bgp["local_as"] == 65000 and len(pe.bgp["peers"]) == 3
        assert all(p["established"] for p in pe.bgp["peers"]) and pe.bgp["peers"][1]["pfx_rcvd"] == 2
        assert [c.text for c in pe.chips] == ["BGP 65000", "0/0 ✕"]      # live: no data-plane default
        assert {f.name: f.ip for f in pe.ifaces}["ge-0/0/1"] == "10.1.2.1"
        lf = tv.links["kvm:0"]
        # the cable end is named after the device's own interface with the VM NIC's MAC (not 'ge-0/0/7')
        assert lf.iface_a is not None and lf.iface_a.name == "ge-0/0/0" and lf.side_a == LinkSide.UP
        assert lf.status.kind == LinkKind.UP
    finally:
        eng.shutdown()


def test_junos_read_only_guard_and_parsers():
    for bad in ("request system reboot", "show version; request system halt", "configure", "show x\nrollback",
                "show configuration | save /tmp/x"):
        with pytest.raises(JS.JunosError):
            JS.check_read_only(bad)
    for c in JUNOS_LIVE["fast"] + JUNOS_LIVE["slow"]:
        assert JS.check_read_only(c) == c
    assert JS.parse_iface_macs((JL / "iface_macs.txt").read_text())["52:54:00:da:d1:28"] == "ge-0/0/1"
    assert JS.parse_local_as((JL / "local_as.json").read_text()) == 65000
    # only the management table has a default today: not the data plane
    assert J.parse_default_route((JL / "route_default.json").read_text()).data["present"] is False
    vrf = ("CUSTOMER.inet.0: 9 destinations\n0.0.0.0/0          *[BGP/170] 00:01:00, localpref 100\n"
           "                    >  to 10.1.1.2 via ge-0/0/0.0\n"
           "mgmt_junos.inet.0: 3 destinations\n0.0.0.0/0          *[Static/5] 01:00:00\n"
           "                    >  to 192.168.122.1 via fxp0.0\n")
    d = J.parse_default_route(vrf).data
    assert d["present"] and d["code"] == "B*" and d["next_hops"][0]["via"] == "10.1.1.2"
