"""UI decisions (V3/V4/V5/V6/V8/V9/V10) against the real Protocol Showcase captures.

The controller is the offline replay (tests/ui_replay.py): the live CML snapshot plus the
captured 'show' outputs of R1-R4, fed through the real parsers -> facts -> join -> LabView.
"""
from __future__ import annotations

import os
import time
from dataclasses import replace
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEventLoop, QSettings  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from labdesk_app.facts import LinkKind, LinkStatus, P2PSource, PathState  # noqa: E402
from labdesk_app.ui import theme as T  # noqa: E402
from labdesk_app.ui.graphics import link_visual, tile_kind, tile_path  # noqa: E402
from labdesk_app.ui.scene_model import build_scene_data, link_fans, link_header  # noqa: E402

from ui_replay import poll_until_settled, replay_controller  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture(scope="module")
def showcase(tmp_path_factory):
    ctl = replay_controller(tmp_path_factory.mktemp("replay"))
    view = poll_until_settled(ctl)
    yield ctl, view
    ctl.shutdown()


def labels(view):
    return {n.key: n.label for n in view.nodes}


def link(view, a, ia, b, ib):
    lab = labels(view)
    for l in view.links:
        if (lab[l.a], l.label_a, lab[l.b], l.label_b) in ((a, ia, b, ib), (b, ib, a, ia)):
            return l
    raise KeyError((a, ia, b, ib))


def node(view, label):
    return next(n for n in view.nodes if n.label == label)


ALL_ON = {"chips": True, "p2p": True, "default": True, "fhrp": True, "ospf": True, "bgp": True}


# ------------------------------------------------------------------ V3 point-to-point beads
def test_p2p_bead_only_from_ospf_oper_state(showcase):
    _, v = showcase
    for a, ia, b, ib in (("R1", "Gi0/2", "R3", "Gi0/0"), ("R2", "Gi0/2", "R3", "Gi0/1")):
        vis = link_visual(link(v, a, ia, b, ib), ALL_ON)
        assert vis.bead == "P2P"
        assert "OSPF network point-to-point" in vis.bead_tip and "show ip ospf interface" in vis.bead_tip


def test_cross_link_is_bcast_teaching_bead_not_p2p(showcase):
    _, v = showcase
    vis = link_visual(link(v, "R1", "Gi0/3", "R2", "Gi0/3"), ALL_ON)
    assert vis.bead == "BCAST" and "DR R2" in vis.bead_tip


def test_a_slash30_alone_draws_no_bead(showcase):
    _, v = showcase
    l34 = link(v, "R3", "Gi0/2", "R4", "Gi0/0")
    assert l34.subnet_hint == "/30"
    assert link_visual(l34, ALL_ON).bead is None


def test_configured_p2p_conflict_is_outlined_bead(showcase):
    _, v = showcase
    l = replace(link(v, "R3", "Gi0/2", "R4", "Gi0/0"), p2p=False, p2p_source=P2PSource.CONFIG,
                p2p_conflict="configured p2p, not active")
    vis = link_visual(l, ALL_ON)
    assert vis.bead == "P2P?" and vis.bead_tip == "configured p2p, not active"


def test_p2p_layer_off_hides_beads(showcase):
    _, v = showcase
    assert link_visual(link(v, "R1", "Gi0/2", "R3", "Gi0/0"), dict(ALL_ON, p2p=False)).bead is None


# ------------------------------------------------------------------ D4/V2 link down is not colour-only
def test_admin_down_names_the_side_and_marks_it(showcase):
    _, v = showcase
    l = link(v, "R2", "Gi0/4", "R4", "Gi0/2")
    vis = link_visual(l, ALL_ON)
    assert vis.stroke == "ADMIN_DOWN" and vis.pill == "R2 Gi0/4 shut"
    assert vis.down_side == ("a" if labels(v)[l.a] == "R2" else "b")
    assert vis.pill_icon == "cross" and "shutdown" in vis.pill_tip
    assert T.LINK_STROKES["ADMIN_DOWN"].dash, "admin down must be dashed, not colour-only"


@pytest.mark.parametrize("kind", ["ADMIN_DOWN", "LINK_DOWN", "PROTOCOL_DOWN", "CABLE_PULLED"])
def test_every_down_kind_has_text_and_non_colour_cue(showcase, kind):
    _, v = showcase
    l = link(v, "R1", "Gi0/2", "R3", "Gi0/0")
    l = replace(l, status=LinkStatus(LinkKind(kind), l.a, "Gi0/2", text=f"R1 Gi0/2 {kind.lower()}"))
    vis = link_visual(l, ALL_ON)
    st = T.LINK_STROKES[vis.stroke]
    assert vis.pill and vis.pill_icon in ("cross", "scissors")
    assert st.dash or st.gap or kind == "LINK_DOWN"           # LINK_DOWN: solid red + ✕ + text
    assert vis.down_side == "a"


# ------------------------------------------------------------------ V6 blue default route
def test_blue_exactly_on_the_default_path(showcase):
    _, v = showcase
    blue = {(labels(v)[l.a], labels(v)[l.b]) for l in v.links if link_visual(l, ALL_ON).blue}
    assert blue == {("R1", "R3"), ("R2", "R3"), ("R3", "R4"), ("R4", "NAT")}


@pytest.mark.parametrize("kind", [k for k in LinkKind if k != LinkKind.UP])
def test_no_blue_on_a_hop_that_is_not_up(showcase, kind):
    _, v = showcase
    l = link(v, "R3", "Gi0/2", "R4", "Gi0/0")
    assert l.default_hop is not None
    l = replace(l, status=LinkStatus(kind, None))
    assert link_visual(l, ALL_ON).blue is False


def test_default_layer_off_removes_blue(showcase):
    _, v = showcase
    assert not link_visual(link(v, "R3", "Gi0/2", "R4", "Gi0/0"), dict(ALL_ON, default=False)).blue


def test_route_source_chips_tell_the_story(showcase):
    _, v = showcase
    chips = {n.label: n.default_chip for n in v.nodes if n.default_chip is not None}
    assert {k: c.text for k, c in chips.items()} == {"R1": "O*E2", "R2": "O*E2", "R3": "B* eBGP", "R4": "S* dhcp"}
    spec = T.chip_spec_from(chips["R3"])
    assert spec.outline and spec.blue_bar and spec.fill is None


def test_egress_tile_caption(showcase):
    _, v = showcase
    sd = build_scene_data(v)
    nat = node(v, "NAT").key
    text, verified, tip = sd.egress_caption[nat]
    assert text == "NAT egress · 192.168.255.1" and not verified
    assert "R1" in tip and "R4" in tip
    sd2 = build_scene_data(_with_path_state(v, PathState.VERIFIED_INTERNET), {nat: time.time()})
    assert sd2.egress_caption[nat][0].startswith("INTERNET ✓ ") and sd2.egress_caption[nat][1]


def _with_path_state(v, state, **kw):
    tv = v.telemetry
    paths = {k: replace(p, state=state, **kw) for k, p in tv.paths.items()}
    return SimpleNamespace(nodes=v.nodes, links=v.links, telemetry=replace(tv, paths=paths))


def test_break_markers(showcase):
    _, v = showcase
    lk = link(v, "R3", "Gi0/2", "R4", "Gi0/0").key
    sd = build_scene_data(_with_path_state(v, PathState.BROKEN_AT, broken_at=lk, broken_kind=LinkKind.ADMIN_DOWN))
    assert sd.path_marks[lk][0] == "cross" and sd.path_marks[lk][1] == T.DOWN_RED
    sd = build_scene_data(_with_path_state(v, PathState.BROKEN_AT, broken_at=lk, broken_kind=LinkKind.UNKNOWN))
    assert sd.path_marks[lk][0] == "question" and sd.path_marks[lk][1] == T.NEUTRAL
    sd = build_scene_data(_with_path_state(v, PathState.STALE, broken_at=lk))
    assert sd.path_marks[lk][0] == "clock"
    sd = build_scene_data(_with_path_state(v, PathState.LOOP))
    assert not sd.path_marks and set(sd.no_exit.values()) == {"LOOP"}


def test_cable_pull_breaks_every_path_at_that_link(showcase):
    ctl, v = showcase
    lk = link(v, "R3", "Gi0/2", "R4", "Gi0/0").key
    assert ctl.telemetry.pull_cable(lk).ok
    try:
        end = time.time() + 15
        v2 = ctl.poll()
        while time.time() < end and next(l for l in v2.links if l.key == lk).status.kind != LinkKind.CABLE_PULLED:
            time.sleep(0.2)
            v2 = ctl.poll()
        l2 = next(l for l in v2.links if l.key == lk)
        assert l2.status.kind == LinkKind.CABLE_PULLED and l2.status.api_certain
        vis = link_visual(l2, ALL_ON)
        assert vis.stroke == "CABLE_PULLED" and T.LINK_STROKES["CABLE_PULLED"].gap and not vis.blue
        sd = build_scene_data(v2)
        assert sd.path_marks[lk][0] == "cross"
        r1 = node(v2, "R1")
        assert r1.default_chip.glyph == "✕" and not r1.default_chip.blue_bar
        # R1's blue stops at the last good hop (R1-R3 stays blue, R3-R4 does not)
        assert link_visual(link(v2, "R1", "Gi0/2", "R3", "Gi0/0"), ALL_ON).blue
    finally:
        ctl.telemetry.restore_cable(lk)
        ctl.poll()


# ------------------------------------------------------------------ V4/V5 protocols
def test_protocol_chips_and_fhrp_badges(showcase):
    _, v = showcase
    r3 = node(v, "R3")
    specs = [T.chip_spec_from(c) for c in r3.chips]
    assert [s.text for s in specs] == ["OSPF", "BGP 65000", "B* eBGP"]
    assert specs[0].hue == T.PROTO_HUES["OSPF"] and specs[1].hue == T.PROTO_HUES["BGP"]
    assert specs[0].icon == "check" and specs[1].icon == "check"
    r1, r2 = node(v, "R1"), node(v, "R2")
    assert [(b.proto.value, b.role, b.primary) for b in r1.fhrp] == [("HSRP", "Active", True), ("VRRP", "Master", True)]
    assert [(b.proto.value, b.role, b.primary) for b in r2.fhrp] == [("HSRP", "Standby", False),
                                                                     ("VRRP", "Backup", False)]


def test_switch_segments_show_hsrp_and_vrrp(showcase):
    _, v = showcase
    sd = build_scene_data(v)
    chips = {labels(v)[k]: [(s.text, layer) for s, _, layer in c] for k, c in sd.switch_chips.items()}
    assert chips == {"SW-A": [("HSRP", "fhrp")], "SW-B": [("VRRP", "fhrp")]}
    tip = sd.switch_chips[node(v, "SW-A").key][0][1]
    assert "Active R1" in tip and "Standby R2" in tip
    tinted = {(labels(v)[l.a], labels(v)[l.b]) for l in v.links if l.key in sd.fhrp_tint}
    assert tinted == {("R1", "SW-A"), ("R2", "SW-A"), ("R1", "SW-B"), ("R2", "SW-B")}
    assert not sd.arcs                                        # the only BGP session has its own cable


def test_rails_only_on_two_router_segments(showcase):
    _, v = showcase
    for l in v.links:
        names = {labels(v)[l.a], labels(v)[l.b]}
        if names & {"SW-A", "SW-B", "NAT"}:
            assert not l.tele.protocols, names
    assert {p.value for p in link(v, "R3", "Gi0/2", "R4", "Gi0/0").tele.protocols} == {"BGP"}


def test_adjacency_state_words():
    assert T.adjacency_state("FULL/DR") == "up" and T.adjacency_state("Established") == "up"
    assert T.adjacency_state("EXSTART/-") == "converging" and T.adjacency_state("Active") == "converging"
    assert T.adjacency_state("Idle") == "none"
    assert T.rail_pen("OSPF", "none", T.tokens()).color().name().lower() != T.DOWN_RED.lower()


# ------------------------------------------------------------------ V8 tiles / V9 header
def test_vendor_tiles_are_distinct():
    from labdesk_app.catalog import BY_KEY
    kinds = {k: tile_kind(BY_KEY[k]) for k in ("iosv", "vjunos", "fortios", "ext_nat", "unmanaged_switch")}
    assert kinds == {"iosv": "cisco", "vjunos": "juniper", "fortios": "fortinet", "ext_nat": "cloud",
                     "unmanaged_switch": "switch"}
    areas = {k: tile_path(k).boundingRect().width() * tile_path(k).boundingRect().height()
             for k in ("cisco", "juniper", "fortinet")}
    assert len({round(tile_path(k).length()) for k in areas}) == 3          # three different outlines


def test_inspector_link_header(showcase):
    _, v = showcase
    assert link_header(link(v, "R1", "Gi0/2", "R3", "Gi0/0"), labels(v)) == \
        "R1 Gi0/2 ⟷ R3 Gi0/0 · 10.0.13.0/30 · P2P (OSPF)"
    assert "OSPF broadcast (DR R2 · BDR R1)" in link_header(link(v, "R1", "Gi0/3", "R2", "Gi0/3"), labels(v))


def test_parallel_links_fan_out():
    ls = [SimpleNamespace(key=f"k{i}", a="x", b="y", label_a=f"e{i}") for i in range(3)]
    fans = link_fans(ls)
    assert sorted(fans.values()) == [-26.0, 0.0, 26.0]


# ------------------------------------------------------------------ the real window (offscreen)
class _NoLauncher:
    def open(self, cmd):
        return "not opened"


def _pump(app, secs):
    end = time.time() + secs
    while time.time() < end:
        app.processEvents(QEventLoop.AllEvents, 20)
        time.sleep(0.005)


@pytest.fixture()
def window(app, showcase, tmp_path):
    QSettings.setDefaultFormat(QSettings.IniFormat)
    QSettings.setPath(QSettings.IniFormat, QSettings.UserScope, str(tmp_path))
    from labdesk_app.ui.main_window import MainWindow
    ctl, _ = showcase
    win = MainWindow(ctl, _NoLauncher())
    win.timer.stop()
    win.resize(1400, 900)
    win.show()
    win.apply_view(ctl.poll())
    _pump(app, 0.3)
    yield win
    win._closing = True
    win.timer.stop()
    win.runner.wait(3000)
    win.hide()
    win.deleteLater()


def test_window_renders_the_model(app, window):
    win = window
    items = {n.label: n for n in win.scene.nodes.values()}
    assert set(items) == {"R1", "R2", "R3", "R4", "SW-A", "SW-B", "NAT"}
    assert [c.text for c in items["R3"].nv.chips] == ["OSPF", "BGP 65000", "B* eBGP"]
    assert items["R4"].label_side() == "right"               # the R3 link leaves R4 straight down
    assert items["R1"].label_side() == "below"
    img = win.grab()
    assert not img.isNull() and img.width() > 1000
    blue = [li for li in win.scene.links.values() if li.visual().blue]
    assert len(blue) == 4


def test_inspector_overview_is_plain_english(app, window):
    win = window
    r1 = next(n for n in win.scene.nodes.values() if n.label == "R1")
    win.scene.clearSelection()
    r1.setSelected(True)
    _pump(app, 0.1)
    ins = win.inspector
    assert ins.stack.currentIndex() == 1
    assert "HSRP Active grp 10" in ins.ov_summary.text()
    assert "reaches the internet exit" in ins.ov_body.text()
    assert ins.if_table.rowCount() >= 4
    statuses = {ins.if_table.item(r, 0).text(): ins.if_table.item(r, 1).text() for r in range(ins.if_table.rowCount())}
    assert statuses["Gi0/2"] == "UP"
    r2 = next(n for n in win.scene.nodes.values() if n.label == "R2")
    win.scene.clearSelection()
    r2.setSelected(True)
    _pump(app, 0.1)
    statuses = {ins.if_table.item(r, 0).text(): ins.if_table.item(r, 1).text() for r in range(ins.if_table.rowCount())}
    assert statuses["Gi0/4"] == "ADMIN DOWN"                  # text, not only a red border


def test_inspector_link_page(app, window):
    win = window
    li = next(l for l in win.scene.links.values() if {l.a.label, l.b.label} == {"R2", "R4"})
    win.scene.clearSelection()
    li.setSelected(True)
    _pump(app, 0.1)
    ins = win.inspector
    assert ins.stack.currentIndex() == 2
    assert "ADMIN DOWN" in ins.state.text() and "R2 Gi0/4 shut" in ins.state.text()
    assert "shutdown" in ins.lk_status.text()
    assert ins.cond_combo.count() >= 5 and ins.pull_btn.isEnabled()


def test_layers_are_shared_and_persisted(app, window):
    win = window
    a = win.layers.actions["ospf"]
    assert not a.isChecked()
    a.setChecked(True)
    assert win.scene.ctx.layers["ospf"] is True
    assert str(win.layers.settings.value("layers/ospf")).lower() == "true"
    a.setChecked(False)


def test_packet_animation_follows_the_default_path(app, window):
    win = window
    r2 = next(n for n in win.scene.nodes.values() if n.label == "R2")
    assert win.animate_default_path(r2.key)
    pk = win.scene.packets[-1]
    walked = []
    for li, rev, cap, fail in pk.legs:
        walked.append((li.a.label, li.b.label)[::-1] if rev else (li.a.label, li.b.label))
        assert not fail
    assert walked == [("R2", "R3"), ("R3", "R4"), ("R4", "NAT")]
    assert pk.legs[-1][2] == "→ internet"


def test_events_go_to_the_log_with_protocol_tag(app, window):
    win = window
    assert "LINK DOWN" in win.logview.toPlainText()


# ------------------------------------------------------------------ live finding: '>' consoles without a secret
def test_unreadable_config_at_user_prompt_is_not_a_probe_error(tmp_path, monkeypatch):
    """Live run (2026-11-17): the showcase consoles sat at '>' and no enable secret is stored, so the
    one-time CONFIG burst came back PARTIAL and every router showed a red 'probe error' chip."""
    import test_telemetry_engine as te
    from fake_console import FakeConsole

    from labdesk_app import config
    from labdesk_app.controller import LabController
    from labdesk_app.facts import CollectorState
    from labdesk_app.probe.creds import DeviceCreds
    from labdesk_app.telemetry import IOS_FAST, IOS_SLOW, TelemetryEngine
    from showcase_replay import fixture_text

    def user_console(uuid, user):
        r = te.UUID_TO_LABEL[uuid]
        outs = {c: fixture_text(r, c) or "" for c in IOS_FAST + IOS_SLOW + ["show running-config"]}
        return FakeConsole(r, "user", outs)

    monkeypatch.setattr(config, "STATE_FILE", tmp_path / "state.json")
    c = LabController(te.FakeClient(), config.State(tmp_path / "state.json"), telemetry=False)
    c.telemetry = TelemetryEngine(c.client, transport_factory=user_console, probe_log_dir=tmp_path / "probe",
                                  console_scan=lambda ex: set(), creds_loader=lambda label: DeviceCreds())
    c.lab_id = te.LAB
    try:
        end = time.time() + 30
        while time.time() < end:
            v = c.poll()
            sch = c.telemetry.sched
            if sch and all(s.learned and not s.config_wanted for s in sch.values()) and \
                    not any(s.running for s in sch.values()):
                break
            time.sleep(0.2)
        v = c.poll()
        routers = [n for n in v.telemetry.nodes.values() if n.role.value == "router"]
        assert routers and all(n.collector_state == CollectorState.OK for n in routers), \
            [(n.label, n.collector_state, n.collector_detail) for n in routers]
        assert all("enable secret" in n.collector_detail for n in routers)
        assert all(n.config_text is None for n in routers)
    finally:
        c.shutdown()


def test_passive_lan_never_shows_no_neighbour_when_slow_tier_ran_first():
    """Live run: the first FAST+SLOW burst hit a busy console, the retry ran SLOW alone (no
    interfaces yet), so 'passive-interface Gi0/0/Gi0/1' was dropped and R1/R2/SW-A/SW-B showed
    'OSPF !' for a minute."""
    from labdesk_app.facts import Health, NodeFacts, Proto, ios_facts_update
    from labdesk_app.telemetry import IOS_FAST, IOS_PARSERS, IOS_SLOW
    from showcase_replay import NOW, fixture_text, join, showcase_topology

    topo, _ = showcase_topology()
    facts = {}
    for nk, n in topo.nodes.items():
        if n.role.value != "router":
            continue
        nf = NodeFacts(nk, n.label, n.vendor)
        for cmds in (IOS_SLOW, IOS_FAST):                 # SLOW alone first, then FAST
            parsed = {c: IOS_PARSERS[c](fixture_text(n.label, c)) for c in cmds if fixture_text(n.label, c)}
            ios_facts_update(nf, parsed, NOW)
        facts[nk] = nf
    jr = join(topo, facts)
    for seg in jr.segments.values():
        if seg.kind.value == "multi":
            assert seg.protocols.get(Proto.OSPF) != Health.DOWN, seg.detail
    r1 = next(nf for nf in facts.values() if nf.label == "R1")
    assert r1.iface("GigabitEthernet0/0").ospf_passive is True
