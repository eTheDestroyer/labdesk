"""Subnets on cables: subnet math, the address parser, link addressing (match / one-sided / mismatch /
secondaries / subinterfaces / dhcp / unnumbered), the console tap, change events and label placement.

Real captures: tests/fixtures/showcase (Protocol Showcase), tests/fixtures/switching (Switching Lab,
incl. the filtered 'show ip interface' and a CML console log of R1 being configured)."""
from __future__ import annotations

import time

import pytest
from showcase_replay import NOW, by_label, join, link_by_labels, router_facts, showcase_topology
from switch_replay import SWITCHING, fixture, switching_topology

from labdesk_app.facts import (AddrInfo, Fact, NodeFacts, SubnetState, addr_from_parse, apply_addr_cmd, compact_net,
                               dhcp_net, effective_addrs, host_part, ios_facts_update, l2_facts_update, short_net,
                               subnet_changes)
from labdesk_app.parsers import console_tap as tap
from labdesk_app.parsers.ios import IP_ADDR_CMD, parse_ip_interface_addrs
from labdesk_app.telemetry import IOS_PARSERS, SW_FAST, SW_SLOW, IOS_FAST, IOS_SLOW
from labdesk_app.ui.scene_model import place_stacks

BRIEF_HDR = "Interface                  IP-Address      OK? Method Status                Protocol\n"


# ============================================================================ subnet math
@pytest.mark.parametrize("ip,plen,host", [("10.0.12.1", 30, ".1"), ("10.0.12.2", 30, ".2"), ("10.0.12.0", 31, ".0"),
                                          ("10.0.12.1", 31, ".1"), ("10.0.12.1", 32, ""), ("192.168.10.1", 24, ".1"),
                                          ("172.16.5.9", 16, ".5.9"), ("172.16.5.9", 20, ".5.9"),
                                          ("10.1.2.3", 8, ".1.2.3")])
def test_host_part(ip, plen, host):
    assert host_part(ip, plen) == host


def test_net_forms():
    assert short_net("192.168.10.0/24") == "192.168.10/24"
    assert short_net("10.0.12.0/30") == "10.0.12.0/30"          # a /30 keeps all four octets
    assert compact_net("10.0.12.0/30") == "…12.0/30"
    assert compact_net("192.168.10.0/24") == "192.168.10/24"
    assert dhcp_net("192.168.255.0/24") == "192.168.255.x/24"
    assert dhcp_net("10.1.0.0/16") == "10.1.x.x/16"


def test_addrinfo_networks_and_31_32():
    a = AddrInfo(("10.0.12.0", 31), (("10.9.9.1", 32),), "static")
    assert a.networks == ["10.0.12.0/31", "10.9.9.1/32"]
    assert a.cidrs == ["10.0.12.0/31", "10.9.9.1/32"]
    assert a.configured


# ============================================================================ parser
def test_parse_addrs_real_router_on_a_stick():
    rows = {r["short"]: r for r in parse_ip_interface_addrs(fixture("R1", IP_ADDR_CMD)).data}
    assert (rows["Gi0/0.10"]["ip"], rows["Gi0/0.10"]["prefixlen"], rows["Gi0/0.10"]["method"]) == \
        ("192.168.10.1", 24, "static")
    assert rows["Gi0/0.20"]["ip"] == "192.168.20.1"
    assert rows["Gi0/0"]["ip"] is None and rows["Gi0/0"]["enabled"] is False
    sw = {r["short"]: r for r in parse_ip_interface_addrs(fixture("SW1", IP_ADDR_CMD)).data}
    assert sw["Vl10"]["ip"] == "192.168.10.2" and sw["Gi0/0"]["method"] == "none"   # L2 port: status line only


SYNTH = """GigabitEthernet0/1 is up, line protocol is up
  Internet address is 10.0.12.1/30
  Secondary address 172.16.0.1/24
  Secondary address 172.16.1.1/24
  Address determined by non-volatile memory
GigabitEthernet0/2 is up, line protocol is up
  Interface is unnumbered. Using address of Loopback0 (1.1.1.1)
GigabitEthernet0/3 is up, line protocol is up
  Internet address is 192.168.255.14/24
  Address determined by DHCP
GigabitEthernet0/4 is up, line protocol is down
  Internet address will be negotiated using DHCP
"""


def test_parse_addrs_secondary_unnumbered_dhcp():   # SYNTHETIC lines, in the IOS 15 wording
    rows = {r["short"]: r for r in parse_ip_interface_addrs(SYNTH).data}
    assert rows["Gi0/1"]["secondaries"] == [("172.16.0.1", 24), ("172.16.1.1", 24)]
    assert rows["Gi0/1"]["method"] == "static"
    assert (rows["Gi0/2"]["method"], rows["Gi0/2"]["unnumbered"]) == ("unnumbered", "Loopback0")
    assert rows["Gi0/3"]["method"] == "dhcp" and rows["Gi0/3"]["prefixlen"] == 24
    assert rows["Gi0/4"]["method"] == "dhcp" and rows["Gi0/4"]["ip"] is None
    assert not parse_ip_interface_addrs("% Invalid input detected at '^' marker.").ok


# ============================================================================ link addressing
@pytest.fixture(scope="module")
def topo():
    return showcase_topology()[0]


def _lf(topo, facts, a, ia, b, ib):
    return join(topo, facts).links[link_by_labels(topo, a, ia, b, ib)]


def _with_addrs(facts, topo, label, rows: dict[str, AddrInfo]):
    nf = facts[by_label(topo)[label]]
    cur = dict(nf.addrs.value) if nf.addrs else {}
    cur.update(rows)
    nf.addrs = Fact(cur, NOW)
    return nf


def test_match_30_with_host_parts(topo):
    ad = _lf(topo, router_facts(topo), "R1", "GigabitEthernet0/2", "R3", "GigabitEthernet0/0").addressing
    assert ad.state == SubnetState.MATCH and ad.label == "10.0.13.0/30"
    assert {ad.host_a, ad.host_b} == {".1", ".2"}
    assert "R1 Gi0/2 10.0.13.1/30" in ad.tooltip and "R3 Gi0/0 10.0.13.2/30" in ad.tooltip


def test_match_31(topo):
    f = router_facts(topo)
    _with_addrs(f, topo, "R1", {"gigabitethernet0/2": AddrInfo(("10.0.13.0", 31), (), "static", ts=NOW)})
    _with_addrs(f, topo, "R3", {"gigabitethernet0/0": AddrInfo(("10.0.13.1", 31), (), "static", ts=NOW)})
    lf = _lf(topo, f, "R1", "GigabitEthernet0/2", "R3", "GigabitEthernet0/0")
    assert lf.addressing.state == SubnetState.MATCH and lf.addressing.label == "10.0.13.0/31"
    assert {lf.addressing.host_a, lf.addressing.host_b} == {".0", ".1"}
    assert lf.subnet_hint == "/31" and not lf.addressing_mismatch


def test_32_is_one_network_per_end(topo):
    f = router_facts(topo)
    _with_addrs(f, topo, "R1", {"gigabitethernet0/2": AddrInfo(("10.0.13.1", 32), (), "static", ts=NOW)})
    _with_addrs(f, topo, "R3", {"gigabitethernet0/0": AddrInfo(("10.0.13.2", 32), (), "static", ts=NOW)})
    ad = _lf(topo, f, "R1", "GigabitEthernet0/2", "R3", "GigabitEthernet0/0").addressing
    assert ad.state == SubnetState.MISMATCH and "10.0.13.1/32" in ad.label and ad.host_a == ""


def test_mismatch_is_amber_text_not_colour_only(topo):
    f = router_facts(topo, overrides={
        ("R3", "show ip interface brief"): BRIEF_HDR + "GigabitEthernet0/0         10.0.31.2       YES TFTP   up   up\n",
        ("R3", "show ip ospf interface brief"): "Interface    PID   Area   IP Address/Mask    Cost  State Nbrs F/C\n"
                                                "Gi0/0        1     0      10.0.31.2/30       1     P2P   0/0\n"})
    lf = _lf(topo, f, "R1", "GigabitEthernet0/2", "R3", "GigabitEthernet0/0")
    ad = lf.addressing
    assert ad.state == SubnetState.MISMATCH and lf.addressing_mismatch
    assert ad.label == "⚠ mismatch 10.0.13.0/30 ≠ 10.0.31.0/30"
    assert "cannot reach each other" in ad.explain
    assert "the two ends are not in the same subnet" in lf.tooltip


def test_one_sided(topo):
    f = router_facts(topo)
    _with_addrs(f, topo, "R3", {"gigabitethernet0/1": AddrInfo(None, (), "none", ts=NOW)})
    ad = _lf(topo, f, "R2", "GigabitEthernet0/2", "R3", "GigabitEthernet0/1").addressing
    assert ad.state == SubnetState.ONE_SIDED and ad.label == "10.0.23.0/30"
    assert "only R2 side configured" in ad.tooltip
    end = getattr(ad, ad.configured_side)
    assert end.label == "R2"


def test_secondaries_compact_list(topo):
    f = router_facts(topo)
    _with_addrs(f, topo, "R1", {"gigabitethernet0/2": AddrInfo(("10.0.13.1", 30), (("172.16.0.1", 24),), "static",
                                                               ts=NOW)})
    ad = _lf(topo, f, "R1", "GigabitEthernet0/2", "R3", "GigabitEthernet0/0").addressing
    assert ad.state == SubnetState.MATCH and ad.label == "10.0.13.0/30 +1"
    assert "172.16.0.1/24 secondary" in ad.tooltip and "only on one end: 172.16.0.0/24" in ad.tooltip


def test_dhcp_end_to_nat(topo):
    ad = _lf(topo, router_facts(topo), "R4", "GigabitEthernet0/1", "NAT", "port").addressing
    assert ad.state == SubnetState.DHCP and ad.label == "dhcp 192.168.255.x/24"
    assert "DHCP" in ad.explain


def test_unnumbered(topo):
    f = router_facts(topo)
    _with_addrs(f, topo, "R1", {"gigabitethernet0/2": AddrInfo(None, (), "unnumbered", "Loopback0", ts=NOW)})
    _with_addrs(f, topo, "R3", {"gigabitethernet0/0": AddrInfo(None, (), "unnumbered", "Loopback0", ts=NOW)})
    ad = _lf(topo, f, "R1", "GigabitEthernet0/2", "R3", "GigabitEthernet0/0").addressing
    assert ad.state == SubnetState.UNNUMBERED and ad.label == "unnumbered"


def test_no_ip_on_either_end_draws_nothing(topo):
    f = router_facts(topo)
    _with_addrs(f, topo, "R1", {"gigabitethernet0/2": AddrInfo(None, (), "none", ts=NOW)})
    _with_addrs(f, topo, "R3", {"gigabitethernet0/0": AddrInfo(None, (), "none", ts=NOW)})
    lf = _lf(topo, f, "R1", "GigabitEthernet0/2", "R3", "GigabitEthernet0/0")
    assert lf.addressing is None


def _switching_facts(topo, ts=NOW):
    out = {}
    for nk, n in topo.nodes.items():
        if not n.collected:
            continue
        sw = n.label.startswith("SW")
        nf = NodeFacts(nk, n.label, n.vendor)
        parsed = {c: IOS_PARSERS[c](t) for c in (SW_FAST + SW_SLOW if sw else IOS_FAST + IOS_SLOW)
                  if (t := fixture(n.label, c)) is not None}
        ios_facts_update(nf, parsed, ts)
        if sw:
            l2_facts_update(nf, parsed, ts)
        out[nk] = nf
    return out


def test_router_on_a_stick_trunk_lists_subinterfaces_svi_not_on_cable():
    topo = switching_topology()[0]
    lab = {k: n.label for k, n in topo.nodes.items()}
    j = join(topo, _switching_facts(topo))
    seen = {}
    for lk, lf in j.links.items():
        l = topo.links[lk]
        seen[frozenset((lab[l.a], lab[l.b]))] = lf
    trunk = seen[frozenset(("R1", "SW1"))].addressing
    assert trunk.state == SubnetState.MATCH and trunk.label == "192.168.10.0/24 +1"
    assert trunk.nets == ["192.168.10.0/24", "192.168.20.0/24"]
    r1_side = trunk.a if trunk.a.label == "R1" else trunk.b
    assert [a[0] for a in r1_side.addrs] == ["Gi0/0.10", "Gi0/0.20"]
    assert (trunk.host_a if trunk.a.label == "R1" else trunk.host_b) == ".1"
    assert "Gi0/0.20 192.168.20.1/24" in trunk.tooltip
    # SVIs (SW1/SW2 Vlan10) are never drawn on a cable; access links to the (unprobed) PCs carry no label
    for pair in (frozenset(("SW1", "SW2")), frozenset(("SW1", "PC1")), frozenset(("SW2", "PC2"))):
        assert seen[pair].addressing is None


# ============================================================================ apply / effective / changes
def test_apply_addr_cmd_semantics():
    a = apply_addr_cmd(None, "set", "10.0.13.1", 30, ts=1)
    a = apply_addr_cmd(a, "add_sec", "172.16.99.1", 24, ts=2)
    assert a.cidrs == ["10.0.13.1/30", "172.16.99.1/24"] and a.source == "console"
    a = apply_addr_cmd(a, "set", "10.0.14.1", 30, ts=3)                   # replaces the primary only
    assert a.cidrs == ["10.0.14.1/30", "172.16.99.1/24"]
    a = apply_addr_cmd(a, "del_sec", "172.16.99.1", 24, ts=4)
    assert a.cidrs == ["10.0.14.1/30"]
    assert apply_addr_cmd(a, "clear", ts=5).configured is False
    d = apply_addr_cmd(a, "dhcp", ts=6)
    assert d.method == "dhcp" and d.primary is None and d.configured
    u = apply_addr_cmd(a, "unnumbered", src="Loopback0", ts=7)
    assert u.method == "unnumbered" and u.unnumbered == "Loopback0"


def test_console_override_wins_until_a_newer_cli_read():
    nf = NodeFacts("cml:r1", "R1")
    nf.addrs = Fact({"gigabitethernet0/2": AddrInfo(None, (), "none", ts=100.0)}, 100.0)
    nf.addr_overrides["gigabitethernet0/2"] = AddrInfo(("10.0.13.1", 30), (), "static", source="console", ts=105.0)
    assert effective_addrs(nf)["gigabitethernet0/2"].source == "console"
    # a CLI burst that started before the console change must not undo it ...
    parsed = {IP_ADDR_CMD: parse_ip_interface_addrs("GigabitEthernet0/2 is up, line protocol is up\n"
                                                    "  Internet protocol processing disabled\n")}
    ios_facts_update(nf, parsed, 103.0)
    assert effective_addrs(nf)["gigabitethernet0/2"].cidrs == ["10.0.13.1/30"]
    # ... a later read is authoritative (and drops the override)
    parsed = {IP_ADDR_CMD: parse_ip_interface_addrs("GigabitEthernet0/2 is up, line protocol is up\n"
                                                    "  Internet address is 10.0.13.1/30\n")}
    ios_facts_update(nf, parsed, 110.0)
    eff = effective_addrs(nf)["gigabitethernet0/2"]
    assert eff.source == "cli" and eff.cidrs == ["10.0.13.1/30"] and not nf.addr_overrides


def _m(**kw):
    return {k: v for k, v in kw.items()}


def test_subnet_changes_configured_changed_removed():
    k = "gigabitethernet0/2"
    none = _m()
    one = _m(**{k: AddrInfo(("10.0.13.1", 30), (), "static")})
    other = _m(**{k: AddrInfo(("10.0.14.1", 30), (), "static")})
    host = _m(**{k: AddrInfo(("10.0.13.2", 30), (), "static")})
    sec = _m(**{k: AddrInfo(("10.0.13.1", 30), (("172.16.99.1", 24),), "static")})
    assert subnet_changes(None, one) == []                                  # first read = baseline
    assert [c.text for c in subnet_changes(none, one)] == ["Gi0/2: 10.0.13.0/30 configured"]
    assert [c.text for c in subnet_changes(one, other)] == ["Gi0/2: 10.0.14.0/30 changed (was 10.0.13.0/30)"]
    assert [c.text for c in subnet_changes(one, none)] == ["Gi0/2: 10.0.13.0/30 removed"]
    assert [c.kind for c in subnet_changes(one, host)] == ["changed"]
    assert "10.0.13.2/30" in subnet_changes(one, host)[0].text
    assert [c.text for c in subnet_changes(one, sec)] == ["Gi0/2: 172.16.99.0/24 secondary configured"]
    assert [c.text for c in subnet_changes(sec, one)] == ["Gi0/2: 172.16.99.0/24 secondary removed"]
    # the same addressing from another source is not news
    assert subnet_changes(one, {k: AddrInfo(("10.0.13.1", 30), (), "static", source="console", ts=9)}) == []


# ============================================================================ console tap
def _log():
    return tap.parse_log((SWITCHING / "R1__console_log_config.txt").read_text())


def test_tap_real_log_accepts_and_rejects():
    log = _log()
    st = tap.TapState()
    assert tap.feed(st, log[:3]).cmds == []               # priming: context only, nothing replayed
    res = tap.feed(st, log)
    assert [(c.iface, c.op, c.ip, c.prefixlen) for c in res.cmds] == [
        ("GigabitEthernet0/0.10", "add_sec", "172.16.99.1", 24),
        ("GigabitEthernet0/0.10", "del_sec", "172.16.99.1", 24)]
    # 'ip address 192.168.20.5 ... secondary' got '% ... is assigned to Gi0/0.20': rejected
    assert res.config_done
    assert tap.feed(st, log).cmds == []                   # cursor: nothing twice


def test_tap_open_line_is_not_consumed_until_enter():
    """CML updates the open prompt line in place (same counter) while the user types: the priming read
    sees 'R1(config-subif)#' empty, the next read the typed text on the SAME line, and only the next
    prompt (after Enter) commits it.  Reproduces the live 2.10.1 behaviour."""
    st = tap.TapState()
    base = ["|0006954491|R1(config)#interface GigabitEthernet0/0.10"]
    tap.feed(st, tap.parse_log("\n".join(base + ["|0006954696|R1(config-subif)#"])))
    typing = base + ["|0006954696|R1(config-subif)#ip address 172.16.99.1 255.255.255.0 secondary"]
    r = tap.feed(st, tap.parse_log("\n".join(typing)))
    assert r.pending and r.cmds == []
    r = tap.feed(st, tap.parse_log("\n".join(typing + ["|0006960846|R1(config-subif)#"])))
    assert [(c.iface, c.op, c.ip) for c in r.cmds] == [("GigabitEthernet0/0.10", "add_sec", "172.16.99.1")]
    # an IOS error before the next prompt rejects the command
    bad = typing + ["|0006960846|R1(config-subif)#ip address 192.168.20.5 255.255.255.0 secondary",
                    "|0006961000|% 192.168.20.0 is assigned to GigabitEthernet0/0.20", "|0006961100|R1(config-subif)#"]
    assert tap.feed(st, tap.parse_log("\n".join(bad))).cmds == []


def test_tap_edits_tab_redraw_and_help():
    text = ("|0000000100|R1(config)#int gi0/2\n"
            "|0000000200|R1(config-if)#ip addr 10.0.13.9x\x08 \x08 255.255.255.252\n"
            "|0000000300|R1(config-if)#ip ad\n"
            "|0000000300|R1(config-if)#ip address 10.0.13.5 255.255.255.252 ?\n"
            "|0000000400|R1(config-if)#no ip address\n"
            "|0000000500|R1(config-if)#ip unnumbered lo0\n"
            "|0000000600|R1(config-if)#end\n"
            "|0000000700|R1#\n")
    st = tap.TapState(primed=True)
    res = tap.feed(st, tap.parse_log(text))
    assert [(c.iface, c.op, c.ip, c.src) for c in res.cmds] == [
        ("GigabitEthernet0/2", "set", "10.0.13.9", None),
        ("GigabitEthernet0/2", "clear", None, None),
        ("GigabitEthernet0/2", "unnumbered", None, "Loopback0")]


def test_tap_counter_reset_and_prefix_form():
    st = tap.TapState(primed=True)
    tap.feed(st, tap.parse_log("|0000008000|R1>\n|0000009000|R1>\n"))
    res = tap.feed(st, tap.parse_log("|0000000100|R1(config)#interface GigabitEthernet0/3\n"
                                     "|0000000200|R1(config-if)#ip address 10.0.99.1/24\n|0000000300|R1(config-if)#\n"))
    assert res.reset               # the node restarted: its log starts again, every new command counts
    assert [(c.iface, c.op, c.ip, c.prefixlen) for c in res.cmds] == [("GigabitEthernet0/3", "set", "10.0.99.1", 24)]


def test_tap_ignores_exec_and_other_modes():
    st = tap.TapState(primed=True)
    text = ("|0000000100|R1>ip address 1.1.1.1 255.0.0.0\n"
            "|0000000200|R1(config)#router ospf 1\n"
            "|0000000300|R1(config-router)#network 10.0.0.0 0.255.255.255 area 0\n"
            "|0000000400|R1(config-router)#interface range gi0/1 - 2\n"
            "|0000000500|R1(config-if-range)#ip address 10.0.0.1 255.0.0.0\n"
            "|0000000600|R1(config-if-range)#end\n|0000000700|R1#\n")
    assert tap.feed(st, tap.parse_log(text)).cmds == []


# ============================================================================ engine: console path end to end
class _TapClient:
    def __init__(self):
        self.lines = ["|0000000100|R1>"]

    def console_log(self, lab, node_id, console=0, lines=200):
        return "\n".join(self.lines[-lines:]) + "\n"


def test_engine_tap_override_event_and_dedupe():
    from labdesk_app.facts import NodeRole, TopoLink, TopoNode, Topology
    from labdesk_app.telemetry import TelemetryEngine
    topo = Topology("STARTED", {
        "cml:r1": TopoNode("cml:r1", "R1", NodeRole.ROUTER, "cisco", "BOOTED", "iosv"),
        "cml:r3": TopoNode("cml:r3", "R3", NodeRole.ROUTER, "cisco", "BOOTED", "iosv")},
        {"cml:l1": TopoLink("cml:l1", "cml:r1", "cml:r3", "GigabitEthernet0/2", "GigabitEthernet0/0", "STARTED")})
    cl = _TapClient()
    t = [1000.0]
    opened: set[str] = {"u-r1"}
    eng = TelemetryEngine(cl, now=lambda: t[0], console_scan=lambda ex: set(opened), autostart=False)
    eng.lab_id = "lab"
    eng.topo = topo
    eng.console_uuid = {"cml:r1": "u-r1", "cml:r3": "u-r3"}
    for nk, lbl, ip in (("cml:r1", "R1", None), ("cml:r3", "R3", "10.0.13.2/30")):
        nf = eng.facts[nk] = NodeFacts(nk, lbl)
        txt = "GigabitEthernet0/{} is up, line protocol is up\n".format(2 if lbl == "R1" else 0) + \
            (f"  Internet address is {ip}\n" if ip else "  Internet protocol processing disabled\n")
        ios_facts_update(nf, {IP_ADDR_CMD: parse_ip_interface_addrs(txt)}, 990.0)
        eng._addr_events(nk, lbl)                                   # baseline
    assert eng.tap_once("cml:r1") is False                          # priming
    cl.lines += ["|0000000200|R1#conf t", "|0000000300|R1(config)#int gi0/2",
                 "|0000000400|R1(config-if)#ip address 10.0.13.1 255.255.255.252", "|0000000500|R1(config-if)#end",
                 "|0000000600|R1#"]
    t[0] = 1001.0
    assert eng.tap_once("cml:r1") is True
    evs = [e for e in eng.events if e.kind == "SUBNET"]
    assert [e.text for e in evs] == ["R1 Gi0/2: 10.0.13.0/30 configured (console)"]
    assert "cml:l1" in evs[0].items
    view = eng._build_view(topo, 1001.5)
    ad = view.links["cml:l1"].addressing
    assert ad.state == SubnetState.MATCH and ad.label == "10.0.13.0/30" and "console" in ad.source
    # the CLI probe later reads the same thing: no second event, the override is gone
    t[0] = 1010.0
    ios_facts_update(eng.facts["cml:r1"], {IP_ADDR_CMD: parse_ip_interface_addrs(
        "GigabitEthernet0/2 is up, line protocol is up\n  Internet address is 10.0.13.1/30\n")}, 1010.0)
    eng._addr_events("cml:r1", "R1")
    assert len([e for e in eng.events if e.kind == "SUBNET"]) == 1
    assert not eng.facts["cml:r1"].addr_overrides
    # mismatch -> a warning event, once
    cl.lines += ["|0000000700|R1#conf t", "|0000000800|R1(config)#int gi0/2",
                 "|0000000900|R1(config-if)#ip address 10.0.31.1 255.255.255.252", "|0000001000|R1(config-if)#end",
                 "|0000001100|R1#"]
    t[0] = 1020.0
    eng.tap_once("cml:r1")
    eng._build_view(topo, 1020.5)
    eng._build_view(topo, 1021.0)
    texts = [e.text for e in eng.events if e.kind == "SUBNET"]
    assert texts[-2] == "R1 Gi0/2: 10.0.31.0/30 changed (was 10.0.13.0/30) (console)"
    assert texts[-1].endswith("addressing mismatch 10.0.13.0/30 ≠ 10.0.31.0/30 - the two ends cannot reach each other") \
        or "mismatch 10.0.31.0/30 ≠ 10.0.13.0/30" in texts[-1]
    eng.shutdown()


def test_console_close_kicks_a_probe_and_a_last_tap():
    from labdesk_app.facts import NodeRole, TopoNode, Topology
    from labdesk_app.telemetry import TelemetryEngine, _Sched
    topo = Topology("STARTED", {"cml:r1": TopoNode("cml:r1", "R1", NodeRole.ROUTER, "cisco", "BOOTED", "iosv")}, {})
    opened = {"u-r1"}
    eng = TelemetryEngine(None, console_scan=lambda ex: set(opened), autostart=False)
    eng.probing_enabled = False                 # scheduling off: only the bookkeeping is under test
    eng.update(topo, "lab", {"cml:r1": "u-r1"})
    eng.sched["cml:r1"] = _Sched(next_fast=time.time() + 999)
    opened.clear()
    eng.update(topo, "lab", {"cml:r1": "u-r1"})
    assert eng.sched["cml:r1"].next_fast == 0.0 and "cml:r1" in eng._tap_final
    eng.shutdown()


# ============================================================================ label placement
def test_place_stacks_separates_crossing_labels():
    # two links crossing at (100, 100): both prefer the crossing point
    a = ("A", 80, 14, [(50, 100, 100), (40, 90, 90), (60, 110, 110)])
    b = ("B", 80, 14, [(50, 100, 100), (40, 110, 90), (60, 90, 110)])
    out = place_stacks([a, b])
    assert out["A"][1:] == (100, 100)            # deterministic: the first (by key on a tie) keeps its spot
    assert out["B"][1:] != (100, 100)


def test_place_stacks_prefers_first_free_candidate_and_avoids_obstacles():
    e = ("L", 60, 14, [(50, 0, 0), (60, 0, 40), (40, 0, -40)])
    assert place_stacks([e])["L"][0] == 50
    assert place_stacks([e], obstacles=[(-10, -10, 20, 20)])["L"][0] == 60
    # a neighbour cable's stroke counts too (weighted), but never the stack's own cable
    strokes = {"L": [(-3, -3, 6, 6)], "M": [(-3, 37, 6, 6)]}
    assert place_stacks([e], obstacles=[(-10, -10, 20, 20)], strokes=strokes)["L"][0] == 40


def test_place_stacks_least_overlap_when_crowded():
    e = ("L", 40, 14, [(1, 0, 0), (2, 5, 0)])
    out = place_stacks([e], obstacles=[(-30, -10, 40, 20)])
    assert out["L"][0] == 2                      # both overlap: the smaller overlap wins


# ============================================================================ canvas visual (pure)
def _lv(lf, a, b):
    from types import SimpleNamespace
    return SimpleNamespace(status=lf.status, side_a=lf.side_a, side_b=lf.side_b, tele=lf, default_hop=None,
                           p2p=lf.p2p, p2p_source=lf.p2p_source, p2p_conflict=lf.p2p_conflict, condition=None,
                           addressing=lf.addressing, a=a, b=b, state="STARTED")


def test_link_visual_subnet_label_styles(topo):
    from labdesk_app.ui.graphics import link_visual
    lk = link_by_labels(topo, "R2", "GigabitEthernet0/2", "R3", "GigabitEthernet0/1")
    l = topo.links[lk]
    lf = join(topo, router_facts(topo)).links[lk]
    vis = link_visual(_lv(lf, l.a, l.b), {"subnets": True})
    assert (vis.subnet, vis.subnet_style, vis.subnet_compact) == ("10.0.23.0/30", "match", "…23.0/30")
    assert {vis.host_a, vis.host_b} == {".1", ".2"} and "<b>" in vis.subnet_tip
    assert link_visual(_lv(lf, l.a, l.b), {"subnets": False}).subnet is None      # layer off: nothing
    f = router_facts(topo)
    _with_addrs(f, topo, "R3", {"gigabitethernet0/1": AddrInfo(None, (), "none", ts=NOW)})
    vis = link_visual(_lv(join(topo, f).links[lk], l.a, l.b), {"subnets": True})
    assert vis.subnet == "½ 10.0.23.0/30" and vis.subnet_style == "half"
    assert "only R2 side configured" in vis.subnet_tip


def test_place_stacks_falls_back_to_the_compact_label():
    full = ("L", 120, 14, [(50, 0, 0)], (40, 14, [(50, 0, 0)]))
    obst = [(45, -10, 40, 20)]                    # blocks the right part of the long label only
    assert place_stacks([full], obstacles=obst)["L"] == (50, 0, 0, True)
    assert place_stacks([full])["L"] == (50, 0, 0)      # room: the full label
