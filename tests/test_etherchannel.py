"""EtherChannel (LACP / PAgP / static): parsers on real IOSvL2 captures, the bundle join (facts.build_bundles),
mismatch detection, STP on the Port-channel, events, command generation and mode validation.

Fixtures: tests/fixtures/etherchannel/<state>__<SW>__<command>.txt, captured live on the "Switching Lab"
(tests/fixtures/etherchannel/_tools/capture.py):
  healthy      Po1 LACP SW1 active / SW2 passive (Gi0/1+Gi0/2), Po2 PAgP desirable / auto (Gi1/0+Gi1/1)
  partial      SW1 Gi0/2 shutdown (Po1 1/2; SW2 Gi0/2 suspended)
  fail_on      SW2 Po1 members 'channel-group 1 mode on' (SW1 LACP members suspended)
  fail_proto   SW2 Po1 members PAgP desirable (SW1 suspended, SW2 stand-alone)
  fail_passive SW1 Po1 members LACP passive too (passive <-> passive)
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import ec_replay as R  # noqa: E402

from labdesk_app import etherchannel as EC  # noqa: E402
from labdesk_app.facts import EcProto, EcState, LinkKind, PortRole, PortState  # noqa: E402
from labdesk_app.parsers import ios_l2 as L  # noqa: E402
from labdesk_app.telemetry import command_sets  # noqa: E402


def fx(state, sw, cmd):
    t = R.fixture(state, sw, cmd)
    assert t is not None, (state, sw, cmd)
    return t


def bundle_by_po(j, po):
    return next(b for b in j.bundles.values() if b.po_text.split("/")[0] == po)


# ======================================================================= parsers (real captures)
def test_summary_healthy_two_bundles():
    d = L.parse_etherchannel_summary(fx("healthy", "SW1", "show etherchannel summary")).data
    assert d["in_use"] == 2 and d["aggregators"] == 2
    g1, g2 = d["groups"]
    assert (g1["port_channel"], g1["flags"], g1["protocol"], g1["proto"], g1["up"], g1["layer"]) == \
        ("Po1", "SU", "LACP", "lacp", True, "L2")
    assert [(p["port"], p["flags"]) for p in g1["ports"]] == [("Gi0/1", "P"), ("Gi0/2", "P")]
    assert (g2["port_channel"], g2["protocol"], g2["proto"]) == ("Po2", "PAgP", "pagp")
    assert [p["port"] for p in g2["ports"]] == ["Gi1/0", "Gi1/1"]


def test_summary_failure_flags():
    on = L.parse_etherchannel_summary(fx("fail_on", "SW2", "show etherchannel summary")).data["groups"][0]
    assert on["protocol"] == "-" and on["proto"] == "on" and on["up"]          # static: column shows '-'
    sw1 = L.parse_etherchannel_summary(fx("fail_on", "SW1", "show etherchannel summary")).data["groups"][0]
    assert sw1["flags"] == "SD" and not sw1["up"] and {p["flags"] for p in sw1["ports"]} == {"s"}
    pr = L.parse_etherchannel_summary(fx("fail_proto", "SW2", "show etherchannel summary")).data["groups"][0]
    assert pr["proto"] == "pagp" and {p["flags"] for p in pr["ports"]} == {"I"}
    pa = L.parse_etherchannel_summary(fx("partial", "SW1", "show etherchannel summary")).data["groups"][0]
    assert [p["flags"] for p in pa["ports"]] == ["P", "D"]


def test_summary_synthetic_without_protocol_column():
    txt = ("Group  Port-channel  Protocol    Ports\n------+-------------+-----------+------\n"
           "3      Po3(SD)         -         \n")
    g = L.parse_etherchannel_summary(txt).data["groups"][0]
    assert g["proto"] == "on" and g["ports"] == [] and not g["up"]


def test_lacp_internal_neighbor_sysid():
    own = L.parse_lacp_internal(fx("healthy", "SW1", "show lacp internal")).data[1]
    assert [(r["port"], r["mode"], r["state"], r["port_number"]) for r in own] == \
        [("Gi0/1", "active", "bndl", 2), ("Gi0/2", "active", "bndl", 3)]
    own2 = L.parse_lacp_internal(fx("healthy", "SW2", "show lacp internal")).data[1]
    assert {r["mode"] for r in own2} == {"passive"}
    nbr = L.parse_lacp_neighbor(fx("healthy", "SW1", "show lacp neighbor")).data[1]
    sys2 = L.parse_lacp_sys_id(fx("healthy", "SW2", "show lacp sys-id")).data
    assert sys2["priority"] == 32768 and nbr[0]["dev_id"] == sys2["mac"] and nbr[0]["mode"] == "passive"
    susp = L.parse_lacp_internal(fx("fail_on", "SW1", "show lacp internal")).data[1]
    assert {r["state"] for r in susp} == {"susp"}
    lost = L.parse_lacp_neighbor(fx("partial", "SW2", "show lacp neighbor")).data[1]
    assert next(r for r in lost if r["port"] == "Gi0/2")["dev_id"] is None        # 0000.0000.0000 = no partner


def test_pagp_tables():
    nbr = L.parse_pagp_neighbor(fx("healthy", "SW2", "show pagp neighbor")).data[2]
    assert [(r["port"], r["name"], r["partner_port"]) for r in nbr] == [("Gi1/0", "SW1", "Gi1/0"), ("Gi1/1", "SW1", "Gi1/1")]
    assert {r["mode"] for r in L.parse_pagp_internal(fx("healthy", "SW2", "show pagp internal")).data[2]} == {"auto"}
    assert {r["mode"] for r in L.parse_pagp_internal(fx("healthy", "SW1", "show pagp internal")).data[2]} == {"desirable"}
    # a desirable port without a partner prints an empty Flags column (tabs in the real output)
    lone = L.parse_pagp_internal(fx("fail_proto", "SW2", "show pagp internal")).data[1]
    assert [(r["port"], r["flags"], r["mode"], r["count"]) for r in lone] == [("Gi0/1", "", "desirable", 0),
                                                                           ("Gi0/2", "", "desirable", 0)]


def test_load_balance_detail_and_syslog_reasons():
    assert L.parse_etherchannel_load_balance(fx("healthy", "SW1", "show etherchannel load-balance")).data["method"] \
        == "src-dst-ip"
    det = L.parse_etherchannel_detail(fx("healthy", "SW2", "show etherchannel detail")).data
    assert det[1]["ports"]["Gi0/1"]["mode"] == "passive" and det[2]["ports"]["Gi1/0"]["mode"] == "auto"
    assert det[1]["port_channels"]["Po1"]["protocol"] == "LACP"
    assert L.ec_syslog_reason("L3DONTBNDL2", "Gi0/1 suspended: LACP currently not enabled on the remote port.") == \
        ("Gi0/1", "LACP currently not enabled on the remote port")
    assert L.ec_syslog_reason("CANNOT_BUNDLE2", "Gi0/2 is not compatible with Gi0/1 and will be suspended "
                              "(vlan mask is different)") == ("Gi0/2", "not compatible with Gi0/1 (vlan mask is different)")
    assert L.ec_syslog_reason("BUNDLE", "Interface Gi0/1 joined port-channel Po1") is None


def test_stp_runs_on_the_port_channel():
    st = L.parse_spanning_tree(fx("healthy", "SW2", "show spanning-tree")).data
    ports = {p["short"]: (p["role"], p["state"]) for p in st[10]["ports"]}
    assert ports == {"Po1": ("Root", "FWD"), "Po2": ("Altn", "BLK")}
    tr = L.parse_interfaces_trunk(fx("healthy", "SW2", "show interfaces trunk")).data
    assert set(tr) == {"Po1", "Po2"} and tr["Po2"]["forwarding_text"] == "none"


# ======================================================================= bundle join (facts)
def test_healthy_bundles_and_partners():
    topo, facts, j = R.join_state("healthy")
    assert len(j.bundles) == 2
    b1, b2 = bundle_by_po(j, "Po1"), bundle_by_po(j, "Po2")
    assert (b1.label, b1.state, b1.mismatch) == ("Po1 · LACP 2/2", EcState.UP, [])
    assert (b2.label, b2.state) == ("Po2 · PAgP 2/2", EcState.UP)
    sw1, sw2 = R.LABEL_KEY["SW1"], R.LABEL_KEY["SW2"]
    s1 = b1.side(sw1)
    assert s1.proto == EcProto.LACP and s1.modes == ["active"] and b1.side(sw2).modes == ["passive"]
    assert b2.side(sw1).modes == ["desirable"] and b2.side(sw2).modes == ["auto"]
    for m in s1.members:      # LACP partner joined through 'show lacp sys-id' + port number
        assert (m.partner_node, m.partner_port, m.partner_source) == (sw2, m.port, "lacp")
    for m in b2.side(sw2).members:   # PAgP partner by name
        assert (m.partner_node, m.partner_port, m.partner_source) == (sw1, m.port, "pagp")
    assert b1.load_balance == "src-dst-ip" and b1.trunk_text == "10,20" and b1.vlans == [10, 20]
    assert s1.sys_priority == 32768 and s1.sys_mac


def test_whole_po_blocked_by_stp_and_members_inherit():
    topo, facts, j = R.join_state("healthy")
    b2 = bundle_by_po(j, "Po2")
    blk = b2.stp_block
    assert blk is not None and (blk.node, blk.port, blk.role, blk.vlans) == (R.LABEL_KEY["SW2"], "Po2", PortRole.ALTN,
                                                                            [10, 20])
    assert bundle_by_po(j, "Po1").stp_block is None
    for lk in b2.link_keys:              # members inherit the Po's STP role/state + trunk for display
        lf = j.links[lk]
        sw2_end = lf.l2_a if lf.l2_a.node == R.LABEL_KEY["SW2"] else lf.l2_b
        assert sw2_end.po == "Po2" and sw2_end.stp[10] == (PortRole.ALTN, PortState.BLK)
        assert lf.trunk and lf.trunk_text == "10,20" and lf.bundle == b2.key
        assert lf.stp_block is not None and lf.status.kind == LinkKind.UP
    assert "Po2 is Alternate BLOCKING" in j.links[b2.link_keys[0]].tooltip


def test_partial_bundle_one_of_two():
    topo, facts, j = R.join_state("partial")
    b1 = bundle_by_po(j, "Po1")
    assert b1.state == EcState.PARTIAL and (b1.bundled, b1.total) == (1, 2) and b1.label == "Po1 · LACP 1/2"
    assert not b1.mismatch and any("Gi0/2" in p for p in b1.problems)
    lk = R.link_by_ports(topo, "SW1", "Gi0/2")
    assert j.links[lk].status.kind == LinkKind.ADMIN_DOWN
    # the 1-member Po1 costs more than Po2 now: spanning tree moved the block to Po1
    assert b1.stp_block is not None and b1.stp_block.port == "Po1"
    assert bundle_by_po(j, "Po2").stp_block is None


@pytest.mark.parametrize("state,short", [
    ("fail_on", "on ↔ LACP active — never bundles"),
    ("fail_proto", "LACP ↔ PAgP — never bundles"),
    ("fail_passive", "LACP passive ↔ passive — never bundles"),
])
def test_mismatch_detection_on_real_failures(state, short):
    if R.fixture(state, "SW2", "show etherchannel summary") is None:
        pytest.skip(f"no {state} capture")
    topo, facts, j = R.join_state(state)
    b1 = next(b for b in j.bundles.values() if R.LABEL_KEY["SW1"] in (b.a, b.b) and
              any(m.port == "Gi0/1" for s in (b.side_a, b.side_b) if s for m in s.members))
    assert b1.state == EcState.MISMATCH and b1.mismatch_short == short
    assert b1.mismatch and "never" in b1.mismatch[0] or "nobody" in b1.mismatch[0] or "not understand" in b1.mismatch[0]
    assert b1.bundled == 0 and bundle_by_po(j, "Po2").state == EcState.UP
    # an unbundled member reads 'suspended' / 'stand-alone', never just 'line protocol down'
    lf = j.links[R.link_by_ports(topo, "SW1", "Gi0/1")]
    if lf.status.kind == LinkKind.PROTOCOL_DOWN:
        assert lf.status.text.endswith(("suspended", "stand-alone", "waiting")) and "EtherChannel" in lf.status.cause


def test_member_config_mismatch_is_detected():
    topo, facts, j = R.join_state("healthy")
    sw1 = facts[R.LABEL_KEY["SW1"]]
    # make SW1 Gi0/2 an access port in VLAN 10 (synthetic: config differs between members)
    from labdesk_app.parsers.common import iface_key
    sp = sw1.l2.ports.value[iface_key("Gi0/2")]
    sp.vlan = 10
    import time
    from labdesk_app.facts import join_facts
    from labdesk_app.telemetry import FRESH_S
    j2 = join_facts(topo, facts, {}, time.time(), FRESH_S)
    b1 = bundle_by_po(j2, "Po1")
    assert b1.state == EcState.MISMATCH and "members differ" in b1.mismatch_short


def test_only_one_side_configured():
    topo, facts, j = R.join_state("healthy")
    sw2 = facts[R.LABEL_KEY["SW2"]]
    sw2.ec.summary.value["groups"] = [g for g in sw2.ec.summary.value["groups"] if g["group"] != 1]
    import time
    from labdesk_app.facts import join_facts
    from labdesk_app.telemetry import FRESH_S
    j2 = join_facts(topo, facts, {}, time.time(), FRESH_S)
    b1 = bundle_by_po(j2, "Po1")
    assert b1.side_b is None and b1.state == EcState.MISMATCH and "no channel-group" in b1.mismatch_short


def test_switch_view_and_events_through_the_engine():
    ctl, box = R.replay_controller("healthy")
    try:
        v = R.poll_until_settled(ctl)
        tv = v.telemetry
        assert {b.label for b in tv.bundles.values()} == {"Po1 · LACP 2/2", "Po2 · PAgP 2/2"}
        sw2 = next(n for n in tv.nodes.values() if n.label == "SW2")
        assert [s.po for s in sw2.switch.ec_sides] == ["Po1", "Po2"] and set(sw2.switch.ec_bundles) == {"Po1", "Po2"}
        # the EtherChannel tables were probed (dynamic command set)
        nf = ctl.telemetry.facts[R.LABEL_KEY["SW1"]]
        fast, slow = command_sets(ctl.telemetry.topo.nodes[R.LABEL_KEY["SW1"]], nf)
        assert "show etherchannel summary" in fast and "show lacp neighbor" in slow and "show pagp neighbor" in slow
        # break it: SW2 Po1 -> mode on; the next bursts bring the mismatch + member events
        box.state = "fail_on"
        import time
        for nk in list(ctl.telemetry.sched):
            ctl.telemetry.sched[nk].next_fast = ctl.telemetry.sched[nk].next_slow = 0.0
        end = time.time() + 40
        while time.time() < end:
            v = ctl.poll()
            b = next((b for b in v.telemetry.bundles.values() if b.po_text.startswith("Po1")), None)
            if b is not None and b.state == EcState.MISMATCH:
                break
            time.sleep(0.3)
        assert b.state == EcState.MISMATCH
        texts = [e.text for e in ctl.telemetry.events_since(0) if e.kind == "ETHERCHANNEL"]
        assert any("⚠ on ↔ LACP active — never bundles" in t for t in texts), texts
        assert any("left Po1" in t for t in texts) and any("Po1 down" in t for t in texts)
    finally:
        ctl.telemetry.shutdown()


def test_no_etherchannel_means_no_extra_commands():
    from labdesk_app.facts import NodeFacts, NodeRole, TopoNode
    n = TopoNode("cml:x", "SW9", NodeRole.SWITCH, "cisco", "BOOTED", "iosvl2", managed=True)
    fast, slow = command_sets(n, NodeFacts("cml:x", "SW9"))
    assert "show etherchannel summary" in slow and "show lacp internal" not in slow + fast
    r = TopoNode("cml:r", "R9", NodeRole.ROUTER, "cisco", "BOOTED", "iosv")
    assert "show etherchannel summary" not in sum(command_sets(r, NodeFacts("cml:r", "R9")), [])


# ======================================================================= mode rules + command generation
@pytest.mark.parametrize("a,b,forms,word", [
    ("active", "active", True, ""), ("active", "passive", True, ""), ("passive", "passive", False, "passive ↔ passive"),
    ("desirable", "desirable", True, ""), ("desirable", "auto", True, ""), ("auto", "auto", False, "auto ↔ auto"),
    ("on", "on", True, ""), ("on", "active", False, "on ↔ LACP"), ("desirable", "on", False, "on ↔ PAgP"),
    ("active", "desirable", False, "LACP ↔ PAgP"), (None, "active", False, "no channel-group"),
])
def test_check_modes(a, b, forms, word):
    c = EC.check_modes(a, b)
    assert c.forms is forms
    if not forms:
        assert word in c.reason and c.explain


def test_bundle_commands_and_unbundle():
    l2 = [" switchport trunk encapsulation dot1q", " switchport trunk allowed vlan 10,20", " switchport mode trunk",
          " switchport nonegotiate"]
    cmds = EC.bundle_commands(2, ["Gi1/0", "GigabitEthernet1/1"], "desirable", l2, "PAgP bundle to SW2", "src-dst-ip")
    assert cmds[0] == "port-channel load-balance src-dst-ip" and cmds[1] == "interface Port-channel2"
    assert cmds.index("interface Port-channel2") < cmds.index("interface GigabitEthernet1/0")
    i = cmds.index("interface GigabitEthernet1/1")
    assert cmds[i + 1:i + 7] == l2 + [" channel-group 2 mode desirable", " no shutdown"]
    un = EC.unbundle_commands(2, ["Gi1/0", "Gi1/1"], l2)
    assert " no channel-group" in un and un[-1] == "no interface Port-channel2" and " no channel-group 2" not in un


def test_plan_validation_and_po_numbers():
    a = EC.BundleSide("cml:a", "SW1", ["GigabitEthernet1/0", "GigabitEthernet1/1"], "auto")
    b = EC.BundleSide("cml:b", "SW2", ["GigabitEthernet1/0", "GigabitEthernet1/1"], "auto")
    plan = EC.BundlePlan(2, "pagp", a, b)
    assert EC.validate_plan(plan) == []                         # never forms, but allowed (teaching lab)
    assert not EC.check_modes(a.mode, b.mode).forms
    b.members = b.members[:1]
    assert EC.validate_plan(plan)
    assert EC.validate_plan(EC.BundlePlan(99, "lacp", EC.BundleSide("x", "SW1", ["Gi0/1", "Gi0/2"], "active"),
                                          EC.BundleSide("y", "SW2", ["Gi0/1", "Gi0/2"], "on")))
    assert EC.next_free_po({1, 2, 4}) == 3
    cfg = fx("healthy", "SW1", "show running-config | section Port-channel|channel-group")
    assert EC.used_po_numbers(cfg) == {1, 2}


def test_day0_patch_roundtrip():
    day0 = (Path(R.EC_DIR).parent / "switching").exists()
    assert day0
    cfg = ("hostname SW1\nvlan 10\n name USERS\ninterface GigabitEthernet0/1\n description TRUNK\n"
           " switchport trunk encapsulation dot1q\n switchport mode trunk\n no shutdown\n"
           "interface GigabitEthernet0/2\n switchport trunk encapsulation dot1q\n switchport mode trunk\n"
           "interface Vlan10\n ip address 192.168.10.2 255.255.255.0\nevent manager applet X\n action 1.0 cli command \"enable\"\n"
           "line con 0\nend\n")
    l2 = EC.l2_lines(EC.iface_block(cfg, "Gi0/1"))
    assert l2 == [" switchport trunk encapsulation dot1q", " switchport mode trunk"]
    new = EC.patch_day0(cfg, 1, ["Gi0/1", "Gi0/2"], "active", l2, "LACP bundle to SW2", "src-dst-ip")
    new = EC.patch_day0(new, 2, ["Gi1/0", "Gi1/1"], "desirable", l2, "PAgP bundle to SW2")
    assert EC.day0_has_bundle(new, 1, ["Gi0/1", "Gi0/2"], "active")
    assert EC.day0_has_bundle(new, 2, ["Gi1/0", "Gi1/1"], "desirable")
    assert new.index("interface Port-channel1") < new.index("interface GigabitEthernet0/1")
    assert new.index("interface GigabitEthernet1/1") < new.index("interface Vlan10")     # new members: physical order
    assert "event manager applet X" in new and " description TRUNK" in new and new.count("port-channel load-balance") == 1
    assert EC.patch_day0(new, 1, ["Gi0/1", "Gi0/2"], "active", l2, "LACP bundle to SW2", "src-dst-ip") == new  # idempotent
    back = EC.unpatch_day0(new, 1)
    assert "interface Port-channel1" not in back and "channel-group 1" not in back
    assert EC.day0_has_bundle(back, 2, ["Gi1/0", "Gi1/1"], "desirable")


def test_l2_lines_from_facts():
    assert EC.l2_lines_from_facts("trunk", allowed="10,20", native=1) == [
        " switchport trunk encapsulation dot1q", " switchport trunk allowed vlan 10,20", " switchport mode trunk",
        " switchport nonegotiate"]
    assert EC.l2_lines_from_facts("access", access_vlan=10) == [" switchport mode access", " switchport access vlan 10"]
    assert EC.l2_summary(EC.l2_lines_from_facts("trunk", allowed="10,20")) == "trunk, VLAN 10,20"
    assert EC.full_ifname("Gi1/0") == "GigabitEthernet1/0" and EC.short_ifname("Port-channel12") == "Po12"


# ======================================================================= UI model + controller (replay)
@pytest.fixture(scope="module")
def app():
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def _engine_view(state="healthy"):
    ctl, box = R.replay_controller(state)
    return ctl, box, R.poll_until_settled(ctl)


def test_link_visual_members_and_fans():
    from labdesk_app.ui.graphics import link_visual
    from labdesk_app.ui.scene_model import link_fans
    ctl, box, v = _engine_view("healthy")
    try:
        members = [lv for lv in v.links if lv.bundle]
        assert len(members) == 4
        po2 = [lv for lv in members if lv.label_a.startswith("Gi1/")]
        for lv in po2:
            vis = link_visual(lv, {})
            assert vis.bundle == lv.bundle and vis.trunk is None and vis.pill is None
            assert vis.stp_on_bundle and vis.stp_side and not vis.ec_tags     # one BLK marker on the bundle
        fans = link_fans(v.links)
        offs = sorted((fans[lv.key], lv.bundle) for lv in members)
        # members of one bundle adjacent, tight pitch; a wider gap between the two bundles
        assert offs[0][1] == offs[1][1] and offs[2][1] == offs[3][1] and offs[1][1] != offs[2][1]
        assert abs(offs[1][0] - offs[0][0]) < abs(offs[2][0] - offs[1][0])
    finally:
        ctl.telemetry.shutdown()


def test_link_visual_unbundled_member_markers():
    from labdesk_app.ui.graphics import link_visual
    ctl, box, v = _engine_view("partial")
    try:
        lv = next(lv for lv in v.links if lv.bundle and lv.label_a == "Gi0/2")
        vis = link_visual(lv, {})
        assert vis.ec_side == "ab" and {t[2] for t in vis.ec_tags} == {"D", "s"}
    finally:
        ctl.telemetry.shutdown()


def test_bundle_plan_through_the_controller():
    ctl, box, v = _engine_view("healthy")
    try:
        topo = ctl.telemetry.topo
        po2 = sorted(lk for lk, l in topo.links.items() if l.iface_a.startswith("GigabitEthernet1/"))
        from labdesk_app.cml_api import CMLError
        with pytest.raises(CMLError, match="already in Po2"):
            ctl.bundle_plan(po2, "pagp")
        with pytest.raises(CMLError, match="two or more"):
            ctl.bundle_plan(po2[:1])
        # pretend Po2 was unbundled (facts + day-0): the plan copies the trunk config, picks the next free Po
        eng = ctl.telemetry
        for nf in eng.facts.values():
            if nf.ec is not None and nf.ec.summary is not None:
                nf.ec.summary.value["groups"] = [g for g in nf.ec.summary.value["groups"] if g["group"] != 2]
            if nf.ifaces is not None:
                nf.ifaces.value.pop("port-channel2", None)
        for lbl in ("SW1", "SW2"):
            ctl.client.day0_override[lbl] = EC.unpatch_day0(
                ctl.client.node_configuration(ctl.lab_id, R.LABEL_KEY[lbl][4:]), 2)
        plan = ctl.bundle_plan(po2, "pagp")
        assert plan.po == 2 and plan.protocol == "pagp" and (plan.a.mode, plan.b.mode) == ("desirable", "desirable")
        assert {plan.a.label, plan.b.label} == {"SW1", "SW2"} and plan.check.forms and not plan.l2_conflict
        assert " switchport trunk allowed vlan 10,20" in plan.a.l2 and " switchport mode trunk" in plan.a.l2
        cmds = plan.commands("a")
        assert "interface Port-channel2" in cmds and " channel-group 2 mode desirable" in cmds
        assert [c for c in cmds if c.startswith("interface Gigabit")] == ["interface GigabitEthernet1/0",
                                                                           "interface GigabitEthernet1/1"]
    finally:
        ctl.client.day0_override.clear()
        ctl.telemetry.shutdown()


def test_bundle_item_and_dialog_render(app):
    """Headless: the canvas item, the inspector page and the dialog paint without errors."""
    from PySide6.QtGui import QImage, QPainter
    from labdesk_app.ui.dialogs import BundleDialog
    ctl, box, v = _engine_view("fail_on")
    try:
        from labdesk_app.ui.canvas import LabScene
        from labdesk_app.ui.graphics import BundleItem, LinkItem, NodeItem
        sc = LabScene()
        for nv in v.nodes:
            it = NodeItem(nv.key, nv.label, nv.dtype)
            it.setPos(nv.x, nv.y)
            it.set_view(nv)
            sc.addItem(it)
            sc.nodes[nv.key] = it
        for lv in v.links:
            li = LinkItem(lv.key, sc.nodes[lv.a], sc.nodes[lv.b], lv.label_a, lv.label_b)
            li.set_view(lv)
            sc.addItem(li)
            sc.links[lv.key] = li
        for k, bd in v.telemetry.bundles.items():
            bi = BundleItem(k, [sc.links[x] for x in bd.link_keys], sc.nodes[bd.a], sc.nodes[bd.b])
            bi.set_bundle(bd, [sc.links[x] for x in bd.link_keys], sc.nodes[bd.a], sc.nodes[bd.b])
            sc.addItem(bi)
            sc.bundles[k] = bi
        img = QImage(1200, 800, QImage.Format_ARGB32)
        p = QPainter(img)
        sc.render(p)
        p.end()
        mm = next(b for b in sc.bundles.values() if b.state() == "mismatch")
        assert mm._hits and any("never bundles" in t or "mismatch" in t.lower() for _, t in mm._hits)
        plan = EC.BundlePlan(3, "lacp", EC.BundleSide("cml:a", "SW1", ["Gi1/0", "Gi1/1"], "active", [" switchport mode trunk"]),
                             EC.BundleSide("cml:b", "SW2", ["Gi1/0", "Gi1/1"], "active", [" switchport mode trunk"]))
        dlg = BundleDialog(plan)
        dlg.mode_a.setCurrentIndex(1)
        dlg.mode_b.setCurrentIndex(1)
        assert "passive ↔ passive" in dlg.check.text() and dlg.apply_btn.isEnabled()
        assert "channel-group 3 mode passive" in dlg.prev_a.toPlainText()
        dlg.po.setValue(48)
        assert "Port-channel48" in dlg.prev_b.toPlainText()
    finally:
        ctl.telemetry.shutdown()


def test_stopped_cables_do_not_count_as_bundled():
    """CML link stop: the switches still say (P) until LACP times out, but the API is certain - Po1 0/2 down."""
    import time
    from labdesk_app.facts import join_facts
    from labdesk_app.telemetry import FRESH_S
    topo, facts, j = R.join_state("healthy")
    keys = bundle_by_po(j, "Po1").link_keys
    for lk in keys:
        topo.links[lk].api_state = "STOPPED"
    hist = {lk: {"observed_stop": time.time()} for lk in keys}
    j2 = join_facts(topo, facts, hist, time.time(), FRESH_S)
    b1 = bundle_by_po(j2, "Po1")
    assert (b1.bundled, b1.state) == (0, EcState.DOWN) and b1.label == "Po1 · LACP 0/2"
    assert all("cable pulled" in p for p in b1.problems) and len(b1.problems) == 2
    assert bundle_by_po(j2, "Po2").state == EcState.UP
