"""Managed switches (IOSvL2) against the live Switching Lab captures (tests/fixtures/switching).

SW1 = STP root for VLAN 10/20, SW2 has two trunks to SW1 and blocks Gi0/2 (Alternate).
after_shut_* = the same switches after 'shutdown' on SW1 Gi0/1 (SW2 Gi0/2 takes over).
"""
from __future__ import annotations

import os
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from switch_replay import (SWITCHING, console_factory, fixture, poll_until_settled, replay_controller,  # noqa: E402
                           switching_topology)

from labdesk_app.facts import (L2_CMDS, LinkKind, NodeFacts, PortMode, PortRole, PortState, ios_facts_update,  # noqa: E402
                               join_facts, l2_facts_update, node_summary, switch_is_l3)
from labdesk_app.parsers import ios_l2 as L  # noqa: E402
from labdesk_app.parsers.common import ParseResult  # noqa: E402
from labdesk_app.telemetry import IOS_FAST, IOS_PARSERS, IOS_SLOW, SW_FAST, SW_SLOW  # noqa: E402

NOW = 1_000_000.0


# ------------------------------------------------------------------ parsers
def test_vlan_brief():
    r = L.parse_vlan_brief(fixture("SW2", "show vlan brief"))
    assert r.ok
    by = {v["id"]: v for v in r.data}
    assert by[10]["name"] == "USERS" and by[20]["name"] == "SERVERS"
    assert by[20]["ports"] == ["Gi0/0"] and by[1]["ports"] == ["Gi0/3"]
    assert by[1002]["reserved"] and not by[10]["reserved"]
    assert L.parse_vlan_brief(fixture("SW1", "show vlan brief")).data[1]["ports"] == ["Gi0/3"]   # VLAN 10: PC1


def test_interfaces_trunk():
    r = L.parse_interfaces_trunk(fixture("SW2", "show interfaces trunk"))
    assert r.ok and set(r.data) == {"Gi0/1", "Gi0/2"}
    g1, g2 = r.data["Gi0/1"], r.data["Gi0/2"]
    assert g1["native"] == 1 and g1["encap"] == "802.1q" and g1["mode"] == "on" and g1["status"] == "trunking"
    assert g1["allowed"] == [(10, 10), (20, 20)] and g1["active_text"] == "10,20"
    assert g1["forwarding_text"] == "10,20" and g2["forwarding_text"] == "none" and g2["forwarding"] == []
    assert set(L.parse_interfaces_trunk(fixture("SW1", "show interfaces trunk")).data) == {"Gi0/0", "Gi0/1", "Gi0/2"}


def test_switchport_status():
    r = L.parse_switchport_status(fixture("SW2", "show interfaces status"))
    by = {x["port"]: x for x in r.data}
    assert by["Gi0/0"]["vlan"] == 20 and by["Gi0/0"]["status"] == "connected"
    assert by["Gi0/1"]["vlan"] == "trunk"
    assert by["Gi0/3"]["status"] == "disabled" and by["Gi0/3"]["vlan"] == 1
    assert by["Gi0/0"]["name"].startswith("PC2 (VLAN 20")
    assert by["Gi0/0"]["iface"] == "GigabitEthernet0/0"


def test_spanning_tree_sw2_blocks_gi0_2():
    r = L.parse_spanning_tree(fixture("SW2", "show spanning-tree"))
    assert r.ok and set(r.data) == {10, 20}
    v10 = r.data[10]
    assert v10["protocol"] == "rstp" and not v10["is_root"]
    assert v10["root_priority"] == 4106 and v10["bridge_priority"] == 32778
    assert v10["root_port"] == "Gi0/1" and v10["root_cost"] == 4
    ports = {p["short"]: p for p in v10["ports"]}
    assert (ports["Gi0/1"]["role"], ports["Gi0/1"]["state"]) == ("Root", "FWD")
    assert (ports["Gi0/2"]["role"], ports["Gi0/2"]["state"]) == ("Altn", "BLK")
    edge = {p["short"]: p for p in r.data[20]["ports"]}["Gi0/0"]
    assert edge["edge"] and edge["p2p"]


def test_spanning_tree_sw1_is_root():
    r = L.parse_spanning_tree(fixture("SW1", "show spanning-tree"))
    assert all(v["is_root"] and v["root_port"] is None for v in r.data.values())
    assert r.data[10]["root_address"] == r.data[10]["bridge_address"]
    assert {p["role"] for p in r.data[10]["ports"]} == {"Desg"}


def test_spanning_tree_summary():
    r1 = L.parse_spanning_tree_summary(fixture("SW1", "show spanning-tree summary"))
    r2 = L.parse_spanning_tree_summary(fixture("SW2", "show spanning-tree summary"))
    assert r1.data["mode"] == "rapid-pvst" and r1.data["root_for"] == [10, 20]
    assert r2.data["root_for"] == [] and r2.data["vlans"][10]["blocking"] == 1
    assert r2.data["flags"]["portfast_default"] is False


def test_mac_tables():
    c = L.parse_mac_count(fixture("SW2", "show mac address-table count"))
    assert c.ok and c.data["vlans"][20]["dynamic"] == 2 and c.data["total"] == 4
    t = L.parse_mac_table(fixture("SW2", "show mac address-table dynamic"))
    assert len(t.data) == 4 and all(e["type"] == "DYNAMIC" for e in t.data)
    assert {"vlan": 20, "mac": "5254.001d.c416", "type": "DYNAMIC", "port": "Gi0/0"} in t.data


def test_cdp_etherchannel_tc_routing():
    cdp = L.parse_cdp_neighbors(fixture("SW1", "show cdp neighbors")).data
    assert {(x["device"], x["local"], x["port"]) for x in cdp} == {("SW2", "Gi0/2", "Gi0/2"), ("SW2", "Gi0/1", "Gi0/1"),
                                                                   ("R1", "Gi0/0", "Gi0/0")}
    ec = L.parse_etherchannel_summary(fixture("SW2", "show etherchannel summary"))
    assert ec.ok and ec.data == {"groups": [], "in_use": 0, "aggregators": 0}
    tc = L.parse_stp_topology_changes(fixture("SW2", L2_CMDS["tc"])).data
    assert tc[10]["changes"] >= 1 and tc[10]["from_iface"] == "Gi0/1" and tc[10]["last_ago_s"] == 406
    assert L.parse_routing_enabled(fixture("SW2", "show ip route connected")).data["routing"] is True
    assert L.parse_routing_enabled("Default gateway is 192.168.10.1\n\nHost  Gateway  Last Use\n").data["routing"] \
        is False


def test_router_answers_are_errors_not_crashes():
    for cmd in ("show vlan brief", "show interfaces trunk", "show spanning-tree summary"):
        r = IOS_PARSERS[cmd](fixture("R1", cmd))
        assert not r.ok and "Invalid input" in r.error
    assert L.parse_spanning_tree(fixture("R1", "show spanning-tree")).data == {}


def test_etherchannel_synthetic():
    txt = ("Group  Port-channel  Protocol    Ports\n------+-------------+-----------+------\n"
           "1      Po1(SU)         LACP      Gi0/1(P)    Gi0/2(P)\n")
    g = L.parse_etherchannel_summary(txt).data["groups"][0]
    assert g["port_channel"] == "Po1" and g["flags"] == "SU" and [p["port"] for p in g["ports"]] == ["Gi0/1", "Gi0/2"]


def test_vlan_text_helpers():
    assert L.vlan_text([10, 20, 21, 22, 30]) == "10,20-22,30"
    assert L.vlan_text([10, 11]) == "10,11"
    assert L.vlan_text([]) == "none"
    assert L.vlan_ranges("1-4094") == [(1, 4094)] and L.in_ranges(20, L.vlan_ranges("10,20"))
    assert L.vlan_ranges("none") == []


# ------------------------------------------------------------------ facts / join
def _facts(topo, prefix=""):
    out = {}
    for nk, n in topo.nodes.items():
        if not n.collected:
            continue
        nf = NodeFacts(nk, n.label, n.vendor)
        cmds = SW_FAST + SW_SLOW if n.managed else IOS_FAST + IOS_SLOW
        parsed = {}
        for c in cmds:
            t = fixture(n.label, c, prefix) or fixture(n.label, c)
            if t is not None and c in IOS_PARSERS:
                parsed[c] = IOS_PARSERS[c](t)
        if n.managed:
            parsed[L2_CMDS["routing"]] = L.parse_routing_enabled(fixture(n.label, "show ip route connected"))
        ios_facts_update(nf, parsed, NOW)
        if n.managed:
            l2_facts_update(nf, parsed, NOW)
        out[nk] = nf
    return out


def _link(topo, join, a, ia, b, ib):
    lab = {k: n.label for k, n in topo.nodes.items()}
    from labdesk_app.parsers.common import short_iface
    for lk, l in topo.links.items():
        ends = (lab[l.a], short_iface(l.iface_a), lab[l.b], short_iface(l.iface_b))
        if ends in ((a, ia, b, ib), (b, ib, a, ia)):
            return join.links[lk]
    raise KeyError((a, ia, b, ib))


@pytest.fixture(scope="module")
def lab():
    topo, _ = switching_topology()
    facts = _facts(topo)
    return topo, facts, join_facts(topo, facts, {}, NOW, 45.0)


def test_topology_marks_managed_switches(lab):
    topo, _, _ = lab
    by = {n.label: n for n in topo.nodes.values()}
    assert by["SW1"].managed and by["SW1"].collected and by["R1"].collected
    assert not by["PC1"].collected


def test_l2_facts(lab):
    topo, facts, _ = lab
    sw2 = next(f for f in facts.values() if f.label == "SW2")
    l2 = sw2.l2
    assert set(l2.configured_vlans()) == {10, 20}
    assert l2.port("Gi0/0").mode == PortMode.ACCESS and l2.port("GigabitEthernet0/1").mode == PortMode.TRUNK
    pt = l2.stp.value[10].ports["gigabitethernet0/2"]
    assert pt.role == PortRole.ALTN and pt.state == PortState.BLK and pt.blocked
    assert l2.stp_mode.value == "rapid-pvst" and l2.mac_counts.value[20] == 2 and l2.routing.value
    assert not switch_is_l3(sw2)


def test_links_trunks_access_and_stp_block(lab):
    topo, _, j = lab
    blocked = _link(topo, j, "SW1", "Gi0/2", "SW2", "Gi0/2")
    assert blocked.status.kind == LinkKind.UP                     # blocked is NOT down
    b = blocked.stp_block
    assert b is not None and b.port == "Gi0/2" and b.role == PortRole.ALTN and b.vlans == [10, 20]
    assert topo.nodes[b.node].label == "SW2"
    assert blocked.trunk and blocked.trunk_text == "10,20" and blocked.native_vlan == 1
    assert "BLOCKING" in blocked.tooltip and "802.1Q trunk carrying VLAN 10,20" in blocked.tooltip
    fwd = _link(topo, j, "SW1", "Gi0/1", "SW2", "Gi0/1")
    assert fwd.stp_block is None and fwd.vlans == [10, 20]
    r1 = _link(topo, j, "SW1", "Gi0/0", "R1", "Gi0/0")
    assert r1.trunk and r1.trunk_text == "10,20" and r1.side_a.value == r1.side_b.value == "UP"
    pc1 = _link(topo, j, "SW1", "Gi0/3", "PC1", "eth0")
    assert pc1.access_vlan == 10 and not pc1.trunk and pc1.vlans == [10]
    pc2 = _link(topo, j, "SW2", "Gi0/0", "PC2", "eth0")
    assert pc2.access_vlan == 20 and pc2.access_side in ("a", "b")


def test_switch_summaries(lab):
    topo, facts, j = lab
    keys = {n.label: k for k, n in topo.nodes.items()}
    assert node_summary(keys["SW2"], topo, facts, j) == (
        "SW2: 2 VLANs, root for none (root is SW1), Gi0/2 blocking (Alternate) for VLAN 10,20 to prevent a loop")
    assert node_summary(keys["SW1"], topo, facts, j) == "SW1: 2 VLANs, root bridge for VLAN 10,20"


def test_after_shut_failover():
    topo, _ = switching_topology()
    facts = _facts(topo, prefix="after_shut_")
    j = join_facts(topo, facts, {}, NOW, 45.0)
    down = _link(topo, j, "SW1", "Gi0/1", "SW2", "Gi0/1")
    assert down.status.kind == LinkKind.ADMIN_DOWN and down.status.text == "SW1 Gi0/1 shut"
    other = _link(topo, j, "SW1", "Gi0/2", "SW2", "Gi0/2")
    assert other.stp_block is None and other.status.kind == LinkKind.UP
    sw2 = next(k for k, n in topo.nodes.items() if n.label == "SW2")
    s = node_summary(sw2, topo, facts, j)
    assert "Alternate" not in s and "root is SW1" in s


def test_l3_switch_and_err_disabled_synthetic(lab):
    topo, facts, _ = lab
    import copy
    f2 = copy.deepcopy(facts)
    k1 = next(k for k, n in topo.nodes.items() if n.label == "SW1")
    ib = fixture("SW1", "show ip interface brief") + \
        "\nVlan20                 192.168.20.2    YES manual up                    up      \n"
    ios_facts_update(f2[k1], {"show ip interface brief": IOS_PARSERS["show ip interface brief"](ib)}, NOW)
    assert switch_is_l3(f2[k1])
    # err-disabled PC1 port: shown as down with the cause in words
    st = fixture("SW1", "show interfaces status").replace("Gi0/3     PC1 (VLAN 10 USERS connected   ",
                                                          "Gi0/3     PC1 (VLAN 10 USERS err-disabled")
    ib2 = fixture("SW1", "show ip interface brief").replace(
        "GigabitEthernet0/3     unassigned      YES unset  up                    up",
        "GigabitEthernet0/3     unassigned      YES unset  down                  down")
    ios_facts_update(f2[k1], {"show ip interface brief": IOS_PARSERS["show ip interface brief"](ib2)}, NOW)
    l2_facts_update(f2[k1], {"show interfaces status": IOS_PARSERS["show interfaces status"](st)}, NOW)
    j = join_facts(topo, f2, {}, NOW, 45.0)
    lf = _link(topo, j, "SW1", "Gi0/3", "PC1", "eth0")
    assert lf.status.kind == LinkKind.LINK_DOWN and lf.status.text == "SW1 Gi0/3 err-disabled"
    assert "err-disabled" in lf.status.plain_text


# ------------------------------------------------------------------ engine (replay consoles)
@pytest.fixture(scope="module")
def replay(tmp_path_factory):
    overrides: dict = {}
    ctl = replay_controller(tmp_path_factory.mktemp("swreplay"), overrides=overrides)
    view = poll_until_settled(ctl)
    yield ctl, view, overrides
    ctl.shutdown()


def test_switches_are_probed_at_user_exec(replay):
    ctl, view, _ = replay
    by = {n.label: n for n in view.nodes}
    for sw in ("SW1", "SW2"):
        t = by[sw].tele
        assert t.collector_state.value == "ok" and t.switch is not None
        assert [c.kind for c in by[sw].chips][-3:] == ["L2", "VLAN", "STP"]
    assert by["SW1"].chips[0].kind == "ROOT" and by["SW1"].chips[0].text == "ROOT v10,20"
    assert all(c.kind != "ROOT" for c in by["SW2"].chips)
    sw2 = by["SW2"].tele.switch
    assert sw2.stp_label == "RPVST+" and sw2.root_of == {10: "SW1", 20: "SW1"}
    assert sw2.blocked == [("Gi0/2", "Alternate", [10, 20])]
    assert {p["port"]: p["mode"] for p in sw2.ports}["Gi0/0"] == "access"
    assert by["PC1"].tele.collector_state.value == "not_probed"


def test_switch_burst_uses_l2_commands_without_enable():
    from labdesk_app.probe.session import IosDialect, run_burst
    from switch_replay import UUID_TO_LABEL
    uuid = next(u for u, lbl in UUID_TO_LABEL.items() if lbl == "SW2")
    con = console_factory()(uuid, "admin")
    res = run_burst(con, IosDialect(), SW_FAST + SW_SLOW, timeout=10)
    assert res.status == "ok" and res.mode_found == "user"
    assert "enable" not in con.sent_text.split("\r")
    assert all(res.outputs[c].text is not None for c in SW_FAST + SW_SLOW)


def test_failover_events(replay):
    ctl, _, overrides = replay
    tele = ctl.telemetry
    seq = tele.events_since(0)[-1].seq if tele.events_since(0) else 0
    for lbl in ("SW1", "SW2"):
        for c in SW_FAST + SW_SLOW:
            t = fixture(lbl, c, "after_shut_")
            if t is not None:
                overrides[(lbl, c)] = t

    def kick_until(pred, timeout=25):
        end = time.time() + timeout
        while time.time() < end:
            for s in tele.sched.values():
                s.next_fast = 0.0
            ctl.poll()
            if pred():
                return True
            time.sleep(0.4)
        return False

    def kinds():
        return [(e.kind, e.text) for e in tele.events_since(seq)]
    assert kick_until(lambda: any(k == "LINK DOWN" for k, _ in kinds()))
    texts = [t for _, t in kinds()]
    assert any("SW2 Gi0/2 VLAN 10,20: Altn BLK → Root FWD" in t for t in texts)
    assert any("SW1 Gi0/1: trunk down" in t for t in texts)
    assert any(k == "STP" and "topology change" in t for k, t in kinds())
    overrides.clear()
    assert kick_until(lambda: any(k == "LINK UP" for k, _ in kinds()))
    assert any("Root FWD → Altn BLK - now a backup path" in t for _, t in kinds())
    assert any("trunk up, carrying VLAN 10,20" in t for _, t in kinds())


# ------------------------------------------------------------------ UI
@pytest.fixture(scope="module")
def app():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def _lv(view, a, ia, b, ib):
    lab = {n.key: n.label for n in view.nodes}
    return next(l for l in view.links if (lab[l.a], l.label_a, lab[l.b], l.label_b) in ((a, ia, b, ib), (b, ib, a, ia)))


def test_link_visuals(replay, app):
    from labdesk_app.ui.graphics import link_visual
    _, view, _ = replay
    on = {"l2": True}
    v = link_visual(_lv(view, "SW1", "Gi0/2", "SW2", "Gi0/2"), on)
    assert v.stroke == "UP" and v.pill is None and v.down_side is None      # blocked != down
    assert v.stp_label == "Altn BLK" and v.stp_side in ("a", "b") and "backup path" in v.stp_tip
    assert v.trunk == "TRUNK 10,20" and "native (untagged) VLAN 1" in v.trunk_tip
    acc = link_visual(_lv(view, "SW1", "Gi0/3", "PC1", "eth0"), on)
    assert acc.access_text == "V10" and acc.trunk is None
    off = link_visual(_lv(view, "SW1", "Gi0/2", "SW2", "Gi0/2"), {"l2": False})
    assert off.stp_side is None and off.trunk is None and off.vlans == (10, 20)


def test_tiles_and_palette(app):
    from labdesk_app.catalog import BY_KEY
    from labdesk_app.ui.graphics import device_pixmap, tile_kind, tile_path
    assert tile_kind(BY_KEY["iosvl2"]) == "l2switch" and tile_kind(BY_KEY["unmanaged_switch"]) == "switch"
    br = tile_path("l2switch").boundingRect()
    assert br.width() > br.height() and br.width() > tile_path("switch").boundingRect().width()
    assert not device_pixmap(BY_KEY["iosvl2"], 40).isNull()


def test_scene_vlans_and_window(replay, app, tmp_path):
    from PySide6.QtCore import QSettings
    from labdesk_app.ui.layers import Layers
    from labdesk_app.ui.scene_model import build_scene_data
    _, view, _ = replay
    sd = build_scene_data(view)
    assert sd.vlans == {1: "default", 10: "USERS", 20: "SERVERS"}
    assert sd.vlan_hues[10] != sd.vlan_hues[20]
    layers = Layers(settings=QSettings(str(tmp_path / "l.ini"), QSettings.IniFormat))
    assert layers.on("l2") and not layers.on("vlan")
    layers.set_vlan(20)
    assert layers.on("vlan") and layers.vlan == 20


def test_main_window_switch_inspector(replay, app):
    from PySide6.QtCore import QEventLoop
    from labdesk_app.ui.main_window import MainWindow

    class Null:
        def open(self, cmd):
            return "x"
    ctl, _, _ = replay
    win = MainWindow(ctl, Null())
    win._lab_opened()
    end = time.time() + 15
    while time.time() < end and (win.view_data is None or not win.scene.nodes):
        app.processEvents(QEventLoop.AllEvents, 50)
        time.sleep(0.05)
    item = next(i for i in win.scene.nodes.values() if i.label == "SW2")
    assert item.kind == "l2switch" and item.port_leds() == ["up", "up", "blk", None]
    item.setSelected(True)
    app.processEvents()
    ins = win.inspector
    assert ins.tabs.isTabVisible(ins.tabs.indexOf(ins.tab_stp))
    assert not ins.tabs.isTabVisible(ins.tabs.indexOf(ins._router_tabs[1]))
    assert len(ins.stp_cards) == 2 and ins.port_table.rowCount() == 4
    assert "Gi0/2 blocking (Alternate)" in ins.ov_summary.text()
    assert win.legend.vlans and 10 in win.legend.vlans
    win.close()
