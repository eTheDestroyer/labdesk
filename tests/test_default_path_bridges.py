"""Default-route tracer across CML external connectors bridged to a host-KVM VM (PE Hub-Spoke HSRP).

R1 Gi0/0 -- BR-PE-R1 (CML external connector) -- LabDesk host-KVM link -- PE ge-0/0/1: the connector is a
piece of cable, not an exit.  Every fact here is parsed from live-format device output (IOS show commands,
the PE's 'display json' captured live), never from YAML / day-0 configs.
"""
from pathlib import Path

import pytest

from labdesk_app.facts import (NodeFacts, PathState, ios_facts_update, join_facts, junos_facts_update)
from labdesk_app.telemetry import IOS_PARSERS, JUNOS_PARSERS, topology_from_snapshot

NOW = 1_000_000.0
JL = Path(__file__).parent / "fixtures" / "junos_live"

CODES = """Codes: L - local, C - connected, S - static, R - RIP, M - mobile, B - BGP
       ia - IS-IS inter area, * - candidate default, U - per-user static route

"""


def _brief(rows):
    out = "Interface                  IP-Address      OK? Method Status                Protocol\n"
    for name, ip, st in rows:
        up = "up" if st else "administratively down"
        out += f"{name:<27}{ip or 'unassigned':<16}YES NVRAM  {up:<22}{'up' if st else 'down'}\n"
    return out


def _connected(gw, rows):
    out = CODES + (f"Gateway of last resort is {gw} to network 0.0.0.0\n\n" if gw else
                   "Gateway of last resort is not set\n\n")
    for name, net, plen, ip in rows:
        out += f"C        {net}/{plen} is directly connected, {name}\n"
        out += f"L        {ip}/32 is directly connected, {name}\n"
    return out


def _route_default(via_word, via, frm=None):
    if via is None:
        return "% Network not in table\n"
    out = (f"Routing entry for 0.0.0.0/0, supernet\n  Known via \"{via_word}\", distance "
           f"{1 if via_word == 'static' else (200 if 'internal' in via_word else 20)}, metric 0, candidate default path\n")
    if via_word.startswith("bgp"):
        out += f"  Tag 65000, type external\n  Last update from {via} 00:12:00 ago\n"
    out += "  Routing Descriptor Blocks:\n"
    out += f"  * {via}" + (f", from {frm or via}, 00:12:00 ago\n" if via_word.startswith("bgp") else "\n")
    out += "      Route metric is 0, traffic share count is 1\n"
    return out


def _cef(via, iface):
    return f"0.0.0.0/0\n  nexthop {via} {iface}\n" if via else "0.0.0.0/0\n  no route\n"


IOS = {
    "R1": dict(ifs=[("GigabitEthernet0/0", "10.1.2.2", 30, True), ("GigabitEthernet0/1", "172.16.0.2", 24, True),
                    ("GigabitEthernet0/2", "192.168.20.2", 24, True)],
               dflt=("bgp 65001", "10.1.2.1", "GigabitEthernet0/0")),
    "R2": dict(ifs=[("GigabitEthernet0/0", "10.1.3.2", 30, True), ("GigabitEthernet0/1", "172.16.0.3", 24, True),
                    ("GigabitEthernet0/2", "192.168.20.3", 24, True)],
               dflt=("bgp 65001", "10.1.3.1", "GigabitEthernet0/0")),
    "HUB": dict(ifs=[("GigabitEthernet0/0", "10.1.1.2", 30, True), ("GigabitEthernet0/1", "100.64.0.2", 24, True)],
                dflt=("static", "100.64.0.1", "GigabitEthernet0/1")),
}


def _ios_facts(nk, label, spec, ts=NOW):
    nf = NodeFacts(nk, label, "cisco")
    word, via, iface = spec["dflt"]
    up = [r for r in spec["ifs"] if r[3]]
    texts = {
        "show ip interface brief": _brief([(n, ip, st) for n, ip, _p, st in spec["ifs"]]),
        "show ip route connected": _connected(via, [(n, _net(ip, p), p, ip) for n, ip, p, _ in up]),
        "show ip route 0.0.0.0": _route_default(word, via),
        "show ip cef 0.0.0.0/0": _cef(via, iface),
    }
    ios_facts_update(nf, {c: IOS_PARSERS[c](t) for c, t in texts.items()}, ts)
    return nf


def _net(ip, plen):
    import ipaddress
    return str(ipaddress.ip_interface(f"{ip}/{plen}").network.network_address)


def _pe_facts(nk, ts=NOW, route_file="route_default_vrf.json"):
    nf = NodeFacts(nk, "PE", "juniper")
    parsed = {c: JUNOS_PARSERS[c]((JL / f).read_text()) for c, f in
              (("show interfaces terse | display json", "interfaces_terse.json"),
               ("show route 0.0.0.0/0 exact | display json", route_file))}
    junos_facts_update(nf, parsed, ts)
    return nf


def _iface(i, node, label):
    return {"id": f"i{i}", "node": node, "label": label}


def lab_topology(extra_connector=False):
    """The live lab's cabling (CML snapshot shape + LabDesk's host-KVM links from state.json)."""
    nodes = [
        {"id": "hub", "label": "HUB", "node_definition": "iosv", "state": "BOOTED"},
        {"id": "r1", "label": "R1", "node_definition": "iosv", "state": "BOOTED"},
        {"id": "r2", "label": "R2", "node_definition": "iosv", "state": "BOOTED"},
        {"id": "swd", "label": "SW-DATA", "node_definition": "unmanaged_switch", "state": "BOOTED"},
        {"id": "brh", "label": "BR-PE-HUB", "node_definition": "external_connector", "state": "BOOTED",
         "configuration": "PE-HUB"},
        {"id": "br1", "label": "BR-PE-R1", "node_definition": "external_connector", "state": "BOOTED",
         "configuration": "PE-R1"},
        {"id": "br2", "label": "BR-PE-R2", "node_definition": "external_connector", "state": "BOOTED",
         "configuration": "PE-R2"},
        {"id": "inet", "label": "INTERNET", "node_definition": "external_connector", "state": "BOOTED",
         "configuration": "System Bridge", "tags": ["labdesk-cloud"]},
    ]
    ifs = [_iface(1, "hub", "GigabitEthernet0/0"), _iface(2, "brh", "port"),
           _iface(3, "hub", "GigabitEthernet0/1"), _iface(4, "inet", "port"),
           _iface(5, "r1", "GigabitEthernet0/0"), _iface(6, "br1", "port"),
           _iface(7, "r2", "GigabitEthernet0/0"), _iface(8, "br2", "port"),
           _iface(9, "r1", "GigabitEthernet0/1"), _iface(10, "swd", "port0"),
           _iface(11, "r2", "GigabitEthernet0/1"), _iface(12, "swd", "port1")]
    links = [{"id": f"l{k}", "interface_a": f"i{2 * k - 1}", "interface_b": f"i{2 * k}", "state": "STARTED"}
             for k in range(1, 7)]
    kvm_links = [{"a": "kvm:PE", "b": "cml:brh", "label_a": "ge-0/0/0", "label_b": "", "connector": "brh"},
                 {"a": "kvm:PE", "b": "cml:br1", "label_a": "ge-0/0/1", "label_b": "", "connector": "br1"},
                 {"a": "kvm:PE", "b": "cml:br2", "label_a": "ge-0/0/2", "label_b": "", "connector": "br2"}]
    if extra_connector:
        # R1 Gi0/0 really goes R1 - BR-A - SW-WAN - BR-PE-R1 - PE (two bridges and a switch in the cable)
        nodes += [{"id": "bra", "label": "BR-A", "node_definition": "external_connector", "state": "BOOTED",
                   "configuration": "WAN-A"},
                  {"id": "swan", "label": "SW-WAN", "node_definition": "unmanaged_switch", "state": "BOOTED"}]
        ifs += [_iface(20, "bra", "port"), _iface(21, "swan", "port0"), _iface(22, "swan", "port1"),
                _iface(23, "br1", "port")]
        links = [l for l in links if l["id"] != "l3"]
        links += [{"id": "l3", "interface_a": "i5", "interface_b": "i20", "state": "STARTED"},
                  {"id": "la", "interface_a": "i21", "interface_b": "i23", "state": "STARTED"}]
        kvm_links.append({"a": "kvm:PE", "b": "cml:bra", "label_a": "ge-0/0/9", "label_b": "", "connector": "bra"})
        # BR-A's host bridge reaches SW-WAN in this made-up variant; cable it so the walk can cross it
        links.append({"id": "lb", "interface_a": "i20", "interface_b": "i22", "state": "STARTED"})
    topo, _ = topology_from_snapshot({"state": "STARTED"}, nodes, links, ifs, {"PE": {"kind": "vjunos"}},
                                     kvm_links, {"PE": "running"}, {"kvm:PE": "192.0.2.10"})
    return topo


def live_facts(topo, pe=True, pe_ts=NOW):
    key = {n.label: k for k, n in topo.nodes.items()}
    f = {key[lb]: _ios_facts(key[lb], lb, spec) for lb, spec in IOS.items()}
    if pe:
        f["kvm:PE"] = _pe_facts("kvm:PE", pe_ts)
    return f


def _join(topo, facts, now=NOW):
    return join_facts(topo, facts, {}, now, 30.0)


def _hops(topo, p):
    lab = {k: n.label for k, n in topo.nodes.items()}
    return [(lab[h.from_node], lab.get(h.to_node)) for h in p.hops]


@pytest.fixture()
def topo():
    return lab_topology()


def test_r1_r2_pe_follow_the_bridges_to_the_internet_cloud(topo):
    j = _join(topo, live_facts(topo))
    key = {n.label: k for k, n in topo.nodes.items()}
    for r, first in (("R1", ("R1", "PE")), ("R2", ("R2", "PE"))):
        p = j.paths[key[r]]
        assert p.state == PathState.EGRESS, p.detail
        assert _hops(topo, p) == [first, ("PE", "HUB"), ("HUB", "INTERNET")]
        assert p.egress_mode == "Cloud" and "no cable" not in p.detail
        # the R1 hop crosses R1-BR-PE-R1 (CML link) + BR-PE-R1-PE (host-KVM link)
        assert len(p.hops[0].link_keys) == 2 and any(k.startswith("kvm:") for k in p.hops[0].link_keys)
    pe = j.paths["kvm:PE"]                                           # the PE's own CUSTOMER VRF default
    assert pe.state == PathState.EGRESS and _hops(topo, pe) == [("PE", "HUB"), ("HUB", "INTERNET")]
    assert j.paths[key["HUB"]].state == PathState.EGRESS
    # the blue overlay runs along every cable of the chain, in the direction of travel
    for lk in ("cml:l3", "kvm:1", "kvm:0", "cml:l1", "cml:l2"):
        assert j.links[lk].default_hop is not None, lk
    assert j.links["cml:l3"].default_direction == "a2b"              # R1 -> BR-PE-R1
    assert j.links["kvm:1"].default_direction == "b2a"               # BR-PE-R1 -> PE (kvm link a = PE)
    assert j.links["kvm:0"].default_direction == "a2b"               # PE -> BR-PE-HUB
    assert j.links["cml:l1"].default_direction == "b2a"              # BR-PE-HUB -> HUB


def test_the_right_connector_is_picked_and_bridges_are_not_exits(topo):
    """Three bridge connectors: R1 must use BR-PE-R1, and a bridge to a VM never counts as an Internet exit."""
    j = _join(topo, live_facts(topo))
    key = {n.label: k for k, n in topo.nodes.items()}
    p1 = j.paths[key["R1"]]
    assert p1.hops[0].link_keys == ["cml:l3", "kvm:1"]
    p2 = j.paths[key["R2"]]
    assert p2.hops[0].link_keys == ["cml:l4", "kvm:2"]
    assert all(p.egress_node == key["INTERNET"] for p in j.paths.values())


def test_chain_of_several_connectors_and_a_switch():
    topo = lab_topology(extra_connector=True)
    j = _join(topo, live_facts(topo))
    key = {n.label: k for k, n in topo.nodes.items()}
    p = j.paths[key["R1"]]
    assert p.state == PathState.EGRESS, p.detail
    assert _hops(topo, p)[0] == ("R1", "PE")


def test_missing_cable_is_still_flagged(topo):
    """No cable at all between R2 and the PE (link + host link removed): still 'no cable'."""
    topo.links.pop("cml:l4")
    topo.links.pop("kvm:2")
    j = _join(topo, live_facts(topo))
    key = {n.label: k for k, n in topo.nodes.items()}
    p = j.paths[key["R2"]]
    assert p.state == PathState.UNRESOLVED and "no cable from R2 to PE" in p.detail
    assert j.paths[key["R1"]].state == PathState.EGRESS


def test_connector_to_another_vm_is_not_a_cable_to_the_pe(topo):
    """R1's connector bridges to some other VM, not the PE: R1 -> PE has no cable."""
    topo.links["kvm:1"].a = "kvm:OTHER"
    j = _join(topo, live_facts(topo))
    p = j.paths[{n.label: k for k, n in topo.nodes.items()}["R1"]]
    assert p.state == PathState.UNRESOLVED and "no cable from R1 to PE" in p.detail


def test_unread_pe_is_not_read_never_lost_or_an_exit(topo):
    j = _join(topo, live_facts(topo, pe=False))
    key = {n.label: k for k, n in topo.nodes.items()}
    for r in ("R1", "R2"):
        p = j.paths[key[r]]
        assert p.state == PathState.UNKNOWN and "PE not read" in p.detail, p.detail
        assert p.egress_node is None                            # BR-PE-R1 is not an Internet exit


def test_stale_pe_is_stale_not_lost(topo):
    j = _join(topo, live_facts(topo, pe_ts=NOW - 600))
    key = {n.label: k for k, n in topo.nodes.items()}
    for r in ("R1", "R2"):
        assert j.paths[key[r]].state == PathState.STALE


def test_pe_without_data_plane_default_is_no_egress(topo):
    """The PE read before the CUSTOMER default existed (only mgmt_junos has one): traffic stops at the PE."""
    f = live_facts(topo)
    f["kvm:PE"] = _pe_facts("kvm:PE", route_file="route_default.json")
    p = _join(topo, f).paths[{n.label: k for k, n in topo.nodes.items()}["R1"]]
    assert p.state == PathState.NO_EGRESS and "PE has no default route" in p.detail


def test_r1_wan_failover_follows_ibgp_over_the_data_switch(topo):
    """R1 Gi0/0 shut: R1's default is via 172.16.0.3 (R2, iBGP over SW-DATA) -> R2 -> PE -> HUB -> INTERNET."""
    key = {n.label: k for k, n in topo.nodes.items()}
    f = live_facts(topo)
    spec = dict(IOS["R1"])
    spec["ifs"] = [("GigabitEthernet0/0", "10.1.2.2", 30, False)] + IOS["R1"]["ifs"][1:]
    spec["dflt"] = ("bgp 65001", "172.16.0.3", "GigabitEthernet0/1")
    f[key["R1"]] = _ios_facts(key["R1"], "R1", spec)
    p = _join(topo, f).paths[key["R1"]]
    assert p.state == PathState.EGRESS, p.detail
    assert _hops(topo, p) == [("R1", "R2"), ("R2", "PE"), ("PE", "HUB"), ("HUB", "INTERNET")]
    assert p.hops[0].link_keys == ["cml:l5", "cml:l6"]               # R1 - SW-DATA - R2


def test_event_log_is_truthful_while_the_pe_gets_read(topo):
    """PE unread -> read -> R1 WAN failover -> back: never 'default path lost'."""
    from labdesk_app.telemetry import TelemetryEngine
    eng = TelemetryEngine(None, console_scan=lambda ex: set())
    try:
        eng.topo = topo
        key = {n.label: k for k, n in topo.nodes.items()}
        f = live_facts(topo)
        fail = dict(f)
        spec = dict(IOS["R1"], dflt=("bgp 65001", "172.16.0.3", "GigabitEthernet0/1"))
        spec["ifs"] = [("GigabitEthernet0/0", "10.1.2.2", 30, False)] + IOS["R1"]["ifs"][1:]
        fail[key["R1"]] = _ios_facts(key["R1"], "R1", spec)
        for facts in (live_facts(topo, pe=False), f, fail, f):
            eng._path_events(topo, _join(topo, facts), NOW)
        texts = [e.text for e in eng.events if e.kind == "DEFAULT"]
        assert not any("lost" in t for t in texts), texts
        assert sum("rerouted" in t for t in texts) == 2, texts      # R1 -> R2 -> PE, and back
    finally:
        eng.shutdown()


def test_stale_origin_interfaces_are_stale_not_broken(topo):
    """R1's console busy (labcli/user): its interface read ages while its default route is still fresh."""
    key = {n.label: k for k, n in topo.nodes.items()}
    f = live_facts(topo)
    import dataclasses
    r1 = f[key["R1"]]
    r1.ifaces = dataclasses.replace(r1.ifaces, ts=NOW - 600)
    p = _join(topo, f).paths[key["R1"]]
    assert p.state in (PathState.STALE, PathState.EGRESS), p.detail
    assert p.state != PathState.BROKEN_AT
