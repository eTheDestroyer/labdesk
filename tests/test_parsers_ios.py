"""IOS parsers against the live-captured Protocol Showcase fixtures."""
from showcase_replay import fixture_text

from labdesk_app.parsers import ios
from labdesk_app.parsers.common import expand_iface, iface_key, short_iface, split_syslog


def fx(r, c):
    t = fixture_text(r, c)
    assert t is not None, (r, c)
    return t


def test_iface_names():
    assert expand_iface("Gi0/3") == "GigabitEthernet0/3"
    assert short_iface("GigabitEthernet0/3") == "Gi0/3"
    assert iface_key("Gi0/1") == iface_key("GigabitEthernet0/1")
    assert iface_key("ge-0/0/1.0") == iface_key("ge-0/0/1")
    assert expand_iface("port2") == "port2"


def test_int_brief_admin_down_and_nvi_skipped():
    r2 = ios.parse_ip_int_brief(fx("R2", "show ip interface brief"))
    assert r2.ok
    gi4 = next(x for x in r2.data if x["short"] == "Gi0/4")
    assert (gi4["status"], gi4["protocol"]) == ("admin-down", "down")
    r4 = ios.parse_ip_int_brief(fx("R4", "show ip interface brief"))
    assert all(not x["iface"].startswith("NVI") for x in r4.data)
    assert next(x for x in r4.data if x["short"] == "Gi0/1")["method"] == "DHCP"
    nv = ios.parse_ip_int_brief("Interface IP-Address OK? Method Status Protocol\n"
                                "NV0                        10.0.34.2       YES unset  up                    up\n")
    assert nv.ok and nv.data == []


def test_ospf_neighbor_p2p_column_with_space():
    r = ios.parse_ospf_neighbors(fx("R1", "show ip ospf neighbor"))
    assert r.ok
    by = {x["neighbor_id"]: x for x in r.data}
    assert by["3.3.3.3"]["state"] == "FULL/-" and by["3.3.3.3"]["role"] == "-"
    assert by["2.2.2.2"]["role"] == "DR" and by["2.2.2.2"]["iface"] == "GigabitEthernet0/3"


def test_ospf_int_brief_types():
    r = {x["short"]: x for x in ios.parse_ospf_int_brief(fx("R1", "show ip ospf interface brief")).data}
    assert r["Gi0/2"]["net_type"] == "POINT_TO_POINT" and r["Gi0/2"]["state"] == "P2P"
    assert r["Gi0/3"]["net_type"] == "BROADCAST" and r["Gi0/3"]["state"] == "BDR"
    assert r["Gi0/0"]["nbrs_full"] == 0            # passive LAN shows DR 0/0
    d = {x["short"]: x for x in ios.parse_ospf_interface(fx("R2", "show ip ospf interface")).data}
    assert d["Gi0/2"]["net_type"] == "POINT_TO_POINT" and d["Gi0/0"]["passive"] and not d["Gi0/3"]["passive"]


def test_empty_output_is_absent_ok():
    for r, c, fn in (("R3", "show standby brief", ios.parse_standby_brief),
                     ("R3", "show vrrp", ios.parse_vrrp_detail),
                     ("R4", "show ip ospf neighbor", ios.parse_ospf_neighbors),
                     ("R4", "show ip ospf interface", ios.parse_ospf_interface),
                     ("R3", "show vrrp brief", ios.parse_vrrp_brief)):       # header only
        res = fn(fx(r, c))
        assert res.ok and res.data == [], (r, c)
    bgp = ios.parse_bgp_summary(fx("R1", "show ip bgp summary"))
    assert bgp.ok and bgp.data["active"] is False


def test_fhrp():
    h = ios.parse_standby_brief(fx("R1", "show standby brief")).data[0]
    assert (h["group"], h["state"], h["priority"], h["preempt"], h["vip"]) == (10, "Active", 110, True, "192.168.10.1")
    v = ios.parse_vrrp_brief(fx("R1", "show vrrp brief")).data[0]
    assert (v["group"], v["state"], v["priority"], v["owner"], v["preempt"]) == (20, "Master", 110, False, True)
    v2 = ios.parse_vrrp_brief(fx("R2", "show vrrp brief")).data[0]
    assert v2["state"] == "Backup"
    d = ios.parse_standby_detail(fx("R1", "show standby")).data[0]
    assert d["version"] == 2 and d["standby"] == "192.168.10.3"


def test_bgp():
    s = ios.parse_bgp_summary(fx("R3", "show ip bgp summary")).data
    assert s["local_as"] == 65000 and s["peers"][0]["established"] and s["peers"][0]["pfx_rcvd"] == 2
    s4 = ios.parse_bgp_summary(fx("R4", "show ip bgp summary")).data
    assert s4["peers"][0]["remote_as"] == 65000 and s4["peers"][0]["pfx_rcvd"] == 8
    t = ios.parse_ip_bgp(fx("R4", "show ip bgp")).data
    assert t[0]["network"] == "0.0.0.0" and not t[0]["valid"]          # default-originate row w/o status
    assert ios.parse_bgp_summary("BGP router identifier 1.1.1.1, local AS number 1\n\n"
                                 "Neighbor V AS MsgRcvd MsgSent TblVer InQ OutQ Up/Down State/PfxRcd\n"
                                 "10.0.0.2        4        65004       0       0        1    0    0 never    Idle\n"
                                 ).data["peers"][0]["state"] == "Idle"


def test_default_route_variants():
    r1 = ios.parse_ip_route_default(fx("R1", "show ip route 0.0.0.0")).data
    assert r1["code"] == "O*E2" and r1["next_hops"][0] == {"via": "10.0.13.2", "iface": "GigabitEthernet0/2"}
    r3 = ios.parse_ip_route_default(fx("R3", "show ip route 0.0.0.0")).data
    assert r3["code"] == "B*" and r3["ebgp"] is True
    r4 = ios.parse_ip_route_default(fx("R4", "show ip route 0.0.0.0")).data
    assert r4["code"] == "S*" and r4["distance"] == 254
    none = ios.parse_ip_route_default("% Network not in table\n")
    assert none.ok and not none.data["present"]
    full = {r["code"]: r for r in ios.parse_ip_route(fx("R1", "show ip route")).data["routes"] if r["candidate_default"]}
    assert "O*E2" in full
    ecmp = next(r for r in ios.parse_ip_route(fx("R1", "show ip route")).data["routes"] if r["prefix"] == "10.0.23.0/30")
    assert len(ecmp["next_hops"]) == 2


def test_cef_default():
    c = ios.parse_ip_cef_default(fx("R4", "show ip cef 0.0.0.0/0")).data
    assert c["kind"] == "nexthop" and c["next_hops"] == [{"via": "192.168.255.1", "iface": "GigabitEthernet0/1"}]
    assert ios.parse_ip_cef_default("0.0.0.0/0\n  attached to Null0\n").data["kind"] == "drop"


def test_running_config():
    c = ios.parse_running_config(fx("R1", "show running-config")).data
    assert c["hostname"] == "R1"
    assert c["interfaces"]["GigabitEthernet0/2"]["ospf_network"] == "POINT_TO_POINT"
    assert c["interfaces"]["GigabitEthernet0/0"]["ospf_passive"] is True
    assert c["interfaces"]["GigabitEthernet0/0"]["hsrp"] == [10]
    c2 = ios.parse_running_config(fx("R2", "show running-config")).data
    assert c2["interfaces"]["GigabitEthernet0/4"]["shutdown"]
    c4 = ios.parse_running_config(fx("R4", "show running-config")).data
    assert c4["interfaces"]["GigabitEthernet0/1"]["dhcp"]
    assert c4["bgp"]["neighbors"]["10.0.34.1"]["remote_as"] == 65000


def test_invalid_input_is_not_ok():
    r = ios.parse_ip_int_brief("show ip int bri x\n                    ^\n% Invalid input detected at '^' marker.\n")
    assert not r.ok and "Invalid input" in r.error
    assert not ios.parse_ospf_neighbors(None).ok          # no sentinel


def test_syslog_split():
    text = ("Interface IP-Address OK? Method Status Protocol\n"
            "*Nov 17 04:12:33.123: %LINEPROTO-5-UPDOWN: Line protocol on Interface GigabitEthernet0/4, "
            "changed state to down\n"
            "GigabitEthernet0/0 10.0.0.1 YES manual up up\n")
    clean, logs = split_syslog(text)
    assert "%LINEPROTO" not in clean
    assert logs[0].facility == "LINEPROTO" and logs[0].mnemonic == "UPDOWN" and logs[0].ts.startswith("Nov 17")
    _, l2 = split_syslog("% BGP not active\n")
    assert l2 == []                                       # error answers are not syslog


def test_tools():
    p = ios.parse_ping(fx("R1", "ping 8.8.8.8")).data
    assert p["success"] and p["received"] == 5
    t = ios.parse_traceroute(fx("R1", "traceroute 8.8.8.8 numeric timeout 1 ttl 1 6")).data
    assert [h["ip"] for h in t[:3]] == ["10.0.13.2", "10.0.34.2", "192.168.255.1"]
