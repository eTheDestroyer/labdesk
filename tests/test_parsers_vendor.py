"""Junos (real lab-viz captures + SYNTHETIC samples) and FortiOS (SYNTHETIC samples) parsers (D6)."""
from pathlib import Path

import pytest

from labdesk_app.catalog import guest_ifname, nic_for_ifname
from labdesk_app.facts import NodeFacts, P2PSource, fortios_facts_update, junos_facts_update
from labdesk_app.parsers import fortios, junos

J = Path(__file__).parent / "fixtures" / "junos"
F = Path(__file__).parent / "fixtures" / "fortios"


def t(p):
    return p.read_text()


def test_junos_interfaces_json_and_text_agree():
    a = junos.parse_interfaces_terse_json(t(J / "junos_interfaces_terse_json.txt"))
    b = junos.parse_interfaces_terse(t(J / "junos_interfaces_terse.txt"))
    assert a.ok and b.ok
    ja = {r["iface"]: (r["ip"], r["prefixlen"], r["status"]) for r in a.data if r["ip"]}
    jb = {r["iface"]: (r["ip"], r["prefixlen"], r["status"]) for r in b.data if r["ip"]}
    assert ja == jb and ja["ge-0/0/0"] == ("10.0.34.2", 30, "up")


def test_junos_ospf_and_default():
    n = junos.parse_ospf_neighbors_json(t(J / "junos_ospf_neighbor_json.txt")).data[0]
    assert (n["neighbor_id"], n["adj_state"], n["iface"]) == ("3.3.3.3", "FULL", "ge-0/0/0")
    # the only 0/0 is in mgmt_junos (fxp0): the data plane has no default
    d = junos.parse_default_route(t(J / "junos_route_default.txt"))
    assert d.ok and d.data["present"] is False
    oi = {r["iface"]: r for r in junos.parse_ospf_interface_json(t(J / "junos_ospf_interface_detail_json.SYNTHETIC.txt")).data}
    assert oi["ge-0/0/0"]["net_type"] == "POINT_TO_POINT" and oi["ge-0/0/1"]["passive"]


def test_junos_bgp_vrrp():
    b = junos.parse_bgp_summary_json(t(J / "junos_bgp_summary_json.SYNTHETIC.txt")).data
    assert [(p["ip"], p["established"], p["pfx_rcvd"]) for p in b["peers"]] == [("10.0.45.1", True, 9),
                                                                               ("6.6.6.6", False, None)]
    v = junos.parse_vrrp_summary_json(t(J / "junos_vrrp_summary_json.SYNTHETIC.txt")).data[0]
    assert (v["group"], v["state"], v["vip"]) == (20, "Master", "192.168.50.1")
    assert junos.parse_bgp_summary_json("").data["active"] is False        # not configured
    assert not junos.parse_bgp_summary_json("error: syntax error, expecting <command>").ok


def test_junos_facts_p2p_source():
    nf = NodeFacts("kvm:JR1", "JR1", "juniper")
    junos_facts_update(nf, {
        "show interfaces terse | display json": junos.parse_interfaces_terse_json(t(J / "junos_interfaces_terse_json.txt")),
        "show ospf interface detail | display json": junos.parse_ospf_interface_json(
            t(J / "junos_ospf_interface_detail_json.SYNTHETIC.txt")),
    }, 1.0)
    assert nf.iface("ge-0/0/0").ospf_net == "POINT_TO_POINT"
    assert P2PSource.JUNOS_PTTOPT.value == "JUNOS_PTTOPT"


def test_fortios_parsers():
    rt = fortios.parse_default_route(t(F / "fortios_routing_table_details_default.SYNTHETIC.txt")).data
    assert rt["code"] == "S*" and rt["next_hops"] == [{"via": "192.168.122.1", "iface": "port1"}]
    rt2 = fortios.parse_default_route(t(F / "fortios_routing_table_all.SYNTHETIC.txt")).data
    assert rt2["present"] and rt2["next_hops"][0]["iface"] == "port1"
    phys = {r["iface"]: r for r in fortios.parse_interface_physical(t(F / "fortios_interface_physical.SYNTHETIC.txt")).data}
    assert phys["port2"]["prefixlen"] == 30 and phys["port3"]["status"] == "down"
    oi = {r["iface"]: r for r in fortios.parse_ospf_interface(t(F / "fortios_ospf_interface.SYNTHETIC.txt")).data}
    assert "port1" not in oi and oi["port2"]["net_type"] == "POINT_TO_POINT" and oi["port3"]["passive"]
    b = fortios.parse_bgp_summary(t(F / "fortios_bgp_summary.SYNTHETIC.txt")).data
    assert b["local_as"] == 65006 and [p["established"] for p in b["peers"]] == [True, False]
    v = fortios.parse_vrrp(t(F / "fortios_vrrp.SYNTHETIC.txt")).data[0]
    assert (v["group"], v["state"], v["priority"], v["preempt"]) == (30, "Backup", 120, True)
    n = fortios.parse_ospf_neighbors(t(F / "fortios_ospf_neighbor.SYNTHETIC.txt")).data[0]
    assert n["adj_state"] == "FULL"


def test_fortios_limited_only_when_status_says_so():
    assert fortios.parse_status(t(F / "fortios_system_status_unlicensed.SYNTHETIC.txt")).data["limited"] is True
    assert fortios.parse_status(t(F / "fortios_system_status.SYNTHETIC.txt")).data["limited"] is False
    assert not fortios.parse_bgp_summary("Command fail. Return code -61").ok


def test_fortios_facts():
    nf = NodeFacts("kvm:FGT", "FGT", "fortinet")
    fortios_facts_update(nf, {
        "get system interface physical": fortios.parse_interface_physical(t(F / "fortios_interface_physical.SYNTHETIC.txt")),
        "get router info ospf interface": fortios.parse_ospf_interface(t(F / "fortios_ospf_interface.SYNTHETIC.txt")),
        "get router info routing-table details 0.0.0.0/0": fortios.parse_default_route(
            t(F / "fortios_routing_table_details_default.SYNTHETIC.txt")),
    }, 1.0)
    assert nf.iface("port2").ospf_net == "POINT_TO_POINT" and nf.default_route.value.code == "S*"


@pytest.mark.parametrize("kind,nic,name", [("vjunos", 0, "fxp0"), ("vjunos", 1, "ge-0/0/0"), ("vjunos", 4, "ge-0/0/3"),
                                           ("fortios", 0, "port1"), ("fortios", 2, "port3")])
def test_catalog_nic_naming(kind, nic, name):
    assert guest_ifname(kind, nic) == name
    assert nic_for_ifname(kind, name) == nic
