"""D3/D4/D5 + V6: join_facts, link_status and the default-route resolver on the showcase lab."""
import dataclasses

import pytest
from showcase_replay import NOW, by_label, join, link_by_labels, router_facts, showcase_topology

from labdesk_app.facts import (Health, LinkKind, LinkSide, P2PSource, PathState, Proto, SegmentKind, TopoLink,
                               Topology, link_status)

ALL_NON_UP = [LinkKind.PROTOCOL_DOWN, LinkKind.ADMIN_DOWN, LinkKind.LINK_DOWN, LinkKind.CABLE_PULLED,
              LinkKind.LINK_NOT_STARTED, LinkKind.UNKNOWN]


@pytest.fixture(scope="module")
def topo():
    return showcase_topology()[0]


@pytest.fixture()
def facts(topo):
    return router_facts(topo)


def test_admin_down_is_one_sided(topo, facts):
    j = join(topo, facts)
    lk = link_by_labels(topo, "R2", "GigabitEthernet0/4", "R4", "GigabitEthernet0/2")
    lf = j.links[lk]
    assert lf.status.kind == LinkKind.ADMIN_DOWN
    assert {lf.side_a, lf.side_b} == {LinkSide.ADMIN_DOWN, LinkSide.UP}
    assert topo.nodes[lf.status.side_node].label == "R2" and lf.status.side_iface == "Gi0/4"
    assert lf.status.text == "R2 Gi0/4 shut"
    assert "R4 Gi0/2 still up/up" in lf.tooltip or "R4" in lf.status.plain_text


def test_p2p_from_ospf_oper_state(topo, facts):
    j = join(topo, facts)
    for a, ia, b, ib in (("R1", "GigabitEthernet0/2", "R3", "GigabitEthernet0/0"),
                         ("R2", "GigabitEthernet0/2", "R3", "GigabitEthernet0/1")):
        lf = j.links[link_by_labels(topo, a, ia, b, ib)]
        assert lf.p2p and lf.p2p_source == P2PSource.OSPF_OPER and not lf.bcast
        assert lf.subnet_hint == "/30"


def test_cross_link_is_broadcast_not_p2p(topo, facts):
    j = join(topo, facts)
    lf = j.links[link_by_labels(topo, "R1", "GigabitEthernet0/3", "R2", "GigabitEthernet0/3")]
    assert not lf.p2p and lf.p2p_source is None
    assert lf.bcast and lf.bcast_detail == "DR R2 · BDR R1"
    assert lf.subnet_hint == "/30"            # a /30 alone never draws P2P


def test_fhrp_and_bgp_segments(topo, facts):
    j = join(topo, facts)
    lab = {k: n.label for k, n in topo.nodes.items()}
    multi = [s for s in j.segments.values() if s.kind == SegmentKind.MULTI]
    hs = next(s for s in multi if Proto.HSRP in s.protocols)
    vr = next(s for s in multi if Proto.VRRP in s.protocols)
    assert hs.protocols[Proto.HSRP] == Health.OK and "Active R1" in hs.detail[Proto.HSRP]
    assert vr.protocols[Proto.VRRP] == Health.OK and "Master R1" in vr.detail[Proto.VRRP]
    r1 = facts[by_label(topo)["R1"]]
    g10 = next(g for g in r1.fhrp.value if g.group == 10)
    assert (g10.proto, g10.state, g10.priority) == (Proto.HSRP, "Active", 110)
    lf = j.links[link_by_labels(topo, "R3", "GigabitEthernet0/2", "R4", "GigabitEthernet0/0")]
    assert lf.protocols[Proto.BGP] == Health.OK
    assert any(p == Proto.BGP and st == "Established" and {lab[a], lab[b]} == {"R3", "R4"}
               for a, b, p, st in lf.adjacencies)
    # switch links never carry protocol rails
    sw = j.links[link_by_labels(topo, "R1", "GigabitEthernet0/0", "SW-A", "port0")]
    assert sw.protocols == {} and sw.side_b == LinkSide.NOT_APPLICABLE


def test_default_path_r1_r3_r4_egress(topo, facts):
    j = join(topo, facts)
    lab = {k: n.label for k, n in topo.nodes.items()}
    p = j.paths[by_label(topo)["R1"]]
    assert p.state == PathState.EGRESS
    assert [(lab[h.from_node], lab[h.to_node]) for h in p.hops] == [("R1", "R3"), ("R3", "R4"), ("R4", "NAT")]
    assert [h.code for h in p.hops] == ["O*E2", "B*", "S*"]
    assert p.egress_mode == "NAT"
    r4 = facts[by_label(topo)["R4"]].default_route.value
    assert r4.via_dhcp and r4.source_label == "S* dhcp" and r4.forwarding_checked
    # blue overlay on exactly the hops of the chain, oriented in forwarding direction
    blue = {k for k, lf in j.links.items() if lf.default_hop}
    assert link_by_labels(topo, "R1", "GigabitEthernet0/2", "R3", "GigabitEthernet0/0") in blue
    assert link_by_labels(topo, "R2", "GigabitEthernet0/4", "R4", "GigabitEthernet0/2") not in blue
    assert link_by_labels(topo, "R1", "GigabitEthernet0/3", "R2", "GigabitEthernet0/3") not in blue


@pytest.mark.parametrize("kind", ALL_NON_UP)
def test_no_blue_on_or_beyond_a_bad_hop(topo, facts, kind, monkeypatch):
    """V6: any non-UP status on the R3-R4 hop stops blue there for R1's path."""
    import labdesk_app.facts as F
    bad = link_by_labels(topo, "R3", "GigabitEthernet0/2", "R4", "GigabitEthernet0/0")
    real = F.link_status

    def fake(link, *a, **kw):
        st, sa, sb, fa, fb = real(link, *a, **kw)
        if link.key == bad:
            st = dataclasses.replace(st, kind=kind, text=kind.value)
        return st, sa, sb, fa, fb

    monkeypatch.setattr(F, "link_status", fake)
    j = join(topo, facts)
    p = j.paths[by_label(topo)["R1"]]
    assert p.state == PathState.BROKEN_AT and p.broken_at == bad and p.broken_kind == kind
    assert j.links[bad].default_hop is None
    egress_link = link_by_labels(topo, "R4", "GigabitEthernet0/1", "NAT", "port")
    # R4's own path still exits, but nothing from R1 is painted beyond the break
    assert all(h.ok is False for h in p.hops if bad in h.link_keys)
    assert j.links[egress_link].default_hop.from_node == by_label(topo)["R4"]


def test_stale_admin_down_superseded_by_fresh_up(topo):
    stale = router_facts(topo, ts=NOW - 300)
    fresh_up = ("Interface                  IP-Address      OK? Method Status                Protocol\n"
                "GigabitEthernet0/4         10.0.24.1       YES TFTP   up                    up\n")
    lk = link_by_labels(topo, "R2", "GigabitEthernet0/4", "R4", "GigabitEthernet0/2")
    j = join(topo, stale)
    assert j.links[lk].status.kind == LinkKind.UNKNOWN            # stale ADMIN_DOWN never shows 'shut'
    fresh = router_facts(topo, overrides={("R2", "show ip interface brief"): fresh_up})
    j2 = join(topo, fresh)
    assert j2.links[lk].status.kind == LinkKind.UP


def test_stale_side_with_fresh_down_side(topo):
    f = router_facts(topo)
    r4 = by_label(topo)["R4"]
    f[r4] = router_facts(topo, ts=NOW - 300)[r4]
    lk = link_by_labels(topo, "R2", "GigabitEthernet0/4", "R4", "GigabitEthernet0/2")
    st = join(topo, f).links[lk].status
    assert st.kind == LinkKind.ADMIN_DOWN and r4 in st.stale_sides


def _link(topo, state):
    lk = link_by_labels(topo, "R1", "GigabitEthernet0/2", "R3", "GigabitEthernet0/0")
    l = topo.links[lk]
    return TopoLink(l.key, l.a, l.b, l.iface_a, l.iface_b, state)


def test_stopped_without_evidence_is_not_started(topo, facts):
    st = link_status(_link(topo, "STOPPED"), topo, facts, {}, NOW, 30)[0]
    assert st.kind == LinkKind.LINK_NOT_STARTED
    st = link_status(_link(topo, "DEFINED_ON_CORE"), topo, facts, {}, NOW, 30)[0]
    assert st.kind == LinkKind.LINK_NOT_STARTED


def test_observed_stop_is_cable_pulled(topo, facts):
    l = _link(topo, "STOPPED")
    st = link_status(l, topo, facts, {l.key: {"observed_stop": NOW}}, NOW, 30)[0]
    assert st.kind == LinkKind.CABLE_PULLED and st.api_certain
    st = link_status(l, topo, facts, {l.key: {"pulled_by_labdesk": NOW}}, NOW - 999, 30)[0]
    assert st.kind == LinkKind.CABLE_PULLED                       # never degraded by stale CLI


def test_lab_off_and_node_off(topo, facts):
    off = Topology("STOPPED", topo.nodes, topo.links)
    assert link_status(_link(topo, "STARTED"), off, facts, {}, NOW, 30)[0].kind == LinkKind.LAB_OFF
    nodes = dict(topo.nodes)
    r3 = by_label(topo)["R3"]
    nodes[r3] = dataclasses.replace(nodes[r3], state="STOPPED")
    t2 = Topology("STARTED", nodes, topo.links)
    assert link_status(_link(topo, "STARTED"), t2, facts, {}, NOW, 30)[0].kind == LinkKind.NODE_OFF


DEFAULT_HDR = "Routing entry for 0.0.0.0/0, supernet\n"


def test_cef_loop(topo):
    r1 = ("0.0.0.0/0\n  nexthop 10.0.13.2 GigabitEthernet0/2\n")
    r3 = ("0.0.0.0/0\n  nexthop 10.0.13.1 GigabitEthernet0/0\n")
    route = DEFAULT_HDR + '  Known via "static", distance 1, metric 0, candidate default path\n'
    f = router_facts(topo, overrides={("R1", "show ip cef 0.0.0.0/0"): r1, ("R3", "show ip cef 0.0.0.0/0"): r3,
                                      ("R3", "show ip route 0.0.0.0"): route})
    p = join(topo, f).paths[by_label(topo)["R1"]]
    assert p.state == PathState.LOOP and p.loop_pair is not None


def test_null0_blackhole(topo):
    route = DEFAULT_HDR + ('  Known via "static", distance 1, metric 0 (connected), candidate default path\n'
                           "  Routing Descriptor Blocks:\n  * directly connected, via Null0\n")
    f = router_facts(topo, overrides={("R3", "show ip route 0.0.0.0"): route,
                                      ("R3", "show ip cef 0.0.0.0/0"): "0.0.0.0/0\n  attached to Null0\n"})
    assert join(topo, f).paths[by_label(topo)["R3"]].state == PathState.BLACKHOLE
    assert join(topo, f).paths[by_label(topo)["R1"]].state == PathState.BLACKHOLE


def test_unresolved_ibgp_next_hop(topo):
    route = DEFAULT_HDR + ('  Known via "bgp 65000", distance 200, metric 0, candidate default path, type internal\n'
                           "  Routing Descriptor Blocks:\n  * 9.9.9.9, from 9.9.9.9, 00:01:00 ago\n")
    f = router_facts(topo, overrides={("R3", "show ip route 0.0.0.0"): route,
                                      ("R3", "show ip cef 0.0.0.0/0"): "0.0.0.0/0\n  no route\n"})
    p = join(topo, f).paths[by_label(topo)["R3"]]
    assert p.state == PathState.UNRESOLVED and "9.9.9.9" in p.detail
    assert f[by_label(topo)["R3"]].default_route.value.source_label == "B* iBGP"


def test_no_default_and_no_egress(topo):
    f = router_facts(topo, overrides={("R4", "show ip route 0.0.0.0"): "% Network not in table\n",
                                      ("R4", "show ip cef 0.0.0.0/0"): "0.0.0.0/0\n  no route\n"})
    j = join(topo, f)
    assert j.paths[by_label(topo)["R4"]].state == PathState.NONE
    assert j.paths[by_label(topo)["R3"]].state == PathState.NO_EGRESS


def test_config_p2p_conflict(topo):
    """Configured p2p but oper says broadcast -> 'P2P?' (configured p2p, not active); oper wins."""
    from labdesk_app.facts import ConfigFacts, Fact
    f = router_facts(topo)
    r1 = f[by_label(topo)["R1"]]
    r1.config = Fact(ConfigFacts("", p2p_ifaces={"gigabitethernet0/3"}), NOW)
    lf = join(topo, f).links[link_by_labels(topo, "R1", "GigabitEthernet0/3", "R2", "GigabitEthernet0/3")]
    assert not lf.p2p and lf.p2p_conflict == "configured p2p, not active"


def test_addressing_mismatch(topo):
    bad = ("Interface                  IP-Address      OK? Method Status                Protocol\n"
           "GigabitEthernet0/0         10.9.13.2       YES TFTP   up                    up\n")
    obr = ("Interface    PID   Area            IP Address/Mask    Cost  State Nbrs F/C\n"
           "Gi0/0        1     0               10.9.13.2/30       1     P2P   0/0\n")
    f = router_facts(topo, overrides={("R3", "show ip interface brief"): bad,
                                      ("R3", "show ip ospf interface brief"): obr})
    lf = join(topo, f).links[link_by_labels(topo, "R1", "GigabitEthernet0/2", "R3", "GigabitEthernet0/0")]
    assert lf.addressing_mismatch
