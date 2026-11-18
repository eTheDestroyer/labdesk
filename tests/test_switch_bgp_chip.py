"""BGP chip on a switch: names the routers whose session crosses the LAN (the switch runs no BGP)."""
from __future__ import annotations

import os
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from labdesk_app.facts import (BgpInfo, BgpPeer, Fact, IfFact, NodeFacts, NodeRole, iface_key, Proto, Segment, SegBgpSession,  # noqa: E402
                               SegmentKind, _segment_protocols, bgp_segment_chip_text, bgp_segment_sentence)
from labdesk_app.ui.scene_model import build_scene_data, switch_bgp_chip  # noqa: E402

LABELS = {"cml:r1": "R1", "cml:r2": "R2", "cml:r3": "R3", "cml:sw": "SW-DATA"}


def _peer(ip, asn, up=True):
    return BgpPeer(ip, asn, "Established" if up else "Active", up, 1, "1h")


def _router(ip, local_as, peers):
    f = IfFact("GigabitEthernet0/1", "Gi0/1", ip, 24, "up", "up", "up")
    return NodeFacts("x", "x", ifaces=Fact({iface_key("Gi0/1"): f}, 0.0),
                     bgp=Fact(BgpInfo(True, local_as, None, peers), 0.0))


def _segment(facts):
    seg = Segment("s1", SegmentKind.MULTI, "cml:sw", [(k, "Gi0/1") for k in facts], [])
    topo = SimpleNamespace(nodes={k: SimpleNamespace(role=NodeRole.ROUTER) for k in facts})
    _segment_protocols(seg, topo, facts, {}, {}, LABELS, 0.0, 60.0)
    return seg


def test_ibgp_session_on_a_switch_is_named():
    seg = _segment({"cml:r1": _router("172.16.0.2", 65200, [_peer("172.16.0.3", 65200)]),
                    "cml:r2": _router("172.16.0.3", 65200, [_peer("172.16.0.2", 65200)])})
    assert seg.protocols[Proto.BGP].value == "OK"
    assert [(s.a, s.b, s.kind, s.established) for s in seg.bgp_sessions] == [("cml:r1", "cml:r2", "iBGP", True)]
    assert bgp_segment_chip_text(seg.bgp_sessions, LABELS) == "iBGP R1↔R2"
    assert "doesn't run BGP" in seg.detail[Proto.BGP] and "R1 and R2" in seg.detail[Proto.BGP]


def test_ebgp_and_several_sessions():
    one = [SegBgpSession("cml:r1", "cml:r3", "eBGP", 65200, 65300, True)]
    assert bgp_segment_chip_text(one, LABELS) == "eBGP R1↔R3"
    two = [SegBgpSession("cml:r1", "cml:r2", "iBGP", 65200, 65200, True),
           SegBgpSession("cml:r2", "cml:r3", "iBGP", 65200, 65200, False)]
    assert bgp_segment_chip_text(two, LABELS) == "iBGP ×2"
    mixed = two[:1] + one
    assert bgp_segment_chip_text(mixed, LABELS) == "BGP ×2"
    text, tip = switch_bgp_chip(mixed, LABELS)
    assert text == "BGP ×2"
    assert "iBGP R1 ↔ R2 (AS 65200)" in tip and "eBGP R1 ↔ R3 (AS 65200 ↔ AS 65300)" in tip
    assert "doesn't run BGP" in tip
    assert "not established" in bgp_segment_sentence(two, LABELS)


def test_unknown_as_and_long_labels():
    s = [SegBgpSession("x", "y", "BGP", None, None, False)]
    assert bgp_segment_chip_text(s, {"x": "BRANCH-ROUTER-1", "y": "R2"}) == "BGP BRANCH-…↔R2"
    assert bgp_segment_chip_text([], LABELS) == "BGP"
    assert switch_bgp_chip([], LABELS, "1/2 session ends established", "PARTIAL")[0] == "BGP"


def test_ebgp_detected_from_remote_as():
    seg = _segment({"cml:r1": _router("10.0.0.1", 65200, [_peer("10.0.0.3", 65300, up=False)]),
                    "cml:r3": _router("10.0.0.3", 65300, [_peer("10.0.0.1", 65200)])})
    (s,) = seg.bgp_sessions
    assert (s.kind, s.as_a, s.as_b, s.established) == ("eBGP", 65200, 65300, False)
    assert seg.protocols[Proto.BGP].value == "PARTIAL"


def test_scene_chip_text_on_switch_router_chips_untouched():
    seg = _segment({"cml:r1": _router("172.16.0.2", 65200, [_peer("172.16.0.3", 65200)]),
                    "cml:r2": _router("172.16.0.3", 65200, [_peer("172.16.0.2", 65200)])})
    tv = SimpleNamespace(paths={}, nodes={}, segments={"s1": seg}, links={})
    data = SimpleNamespace(nodes=[SimpleNamespace(key=k, label=v, tele=None) for k, v in LABELS.items()],
                           links=[], telemetry=tv)
    sd = build_scene_data(data)
    (spec, tip, layer), = sd.switch_chips["cml:sw"]
    assert spec.text == "iBGP R1↔R2" and layer == "chips"
    assert "This switch doesn't run BGP" in tip and "R1 and R2" in tip


def test_names_in_label_order_not_key_order():
    labels = {"cml:zz": "R1", "cml:aa": "R2", "cml:bb": "R3"}
    s = SegBgpSession("cml:aa", "cml:zz", "iBGP", 65200, 65200, True)        # key order = R2, R1
    assert bgp_segment_chip_text([s], labels) == "iBGP R1↔R2"
    assert "the session between R1 and R2" in bgp_segment_sentence([s], labels)
    e = SegBgpSession("cml:bb", "cml:zz", "eBGP", 65300, 65200, True)         # R3 (65300) - R1 (65200)
    assert "eBGP R1 ↔ R3 (AS 65200 ↔ AS 65300)" in switch_bgp_chip([e], labels)[1]


def test_bridge_connector_with_custom_label_is_bridge_egress():
    from labdesk_app.ui.scene_model import _egress_mode_guess
    nv = SimpleNamespace(raw={"configuration": [{"name": "default", "content": "PE-HUB"}]})
    assert _egress_mode_guess(nv) == "Bridge"
    assert _egress_mode_guess(SimpleNamespace(raw={"configuration": "NAT"})) == "NAT"
    assert _egress_mode_guess(SimpleNamespace(raw={"configuration": "System Bridge"})) == "Bridge"
