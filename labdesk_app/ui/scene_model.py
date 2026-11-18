"""Pure LabView -> render data for the canvas (no widgets; unit-tested against the showcase fixtures).

Everything here only *reads* the telemetry model (docs/telemetry_model.md).  It never parses CLI
text and never derives a path: the blue hops come from ``LinkFacts.default_hop`` and the break
markers from ``DefaultPath.broken_at``.
"""
from __future__ import annotations

import time
from collections import defaultdict
from dataclasses import dataclass, field

from . import theme as T


def ev(v):
    """Enum -> its value (the model uses str enums; tolerate plain strings in tests)."""
    return getattr(v, "value", v)


@dataclass
class SceneData:
    path_marks: dict = field(default_factory=dict)       # link key -> (icon, color, tooltip)
    egress_caption: dict = field(default_factory=dict)   # node key -> (text, verified, tooltip)
    switch_chips: dict = field(default_factory=dict)     # node key -> [(ChipSpec, tooltip, layer)]
    fhrp_tint: dict = field(default_factory=dict)        # link key -> hue
    chords: list = field(default_factory=list)           # (a, b, proto, state, via switch)
    arcs: list = field(default_factory=list)             # (a, b, count, state)
    fans: dict = field(default_factory=dict)             # link key -> signed offset (px)
    no_exit: dict = field(default_factory=dict)          # node key -> PathState value (no blue, chip only)
    vlans: dict = field(default_factory=dict)            # VLAN id -> name (all managed switches of the lab)
    vlan_hues: dict = field(default_factory=dict)        # VLAN id -> hue


PATH_BREAK = {"BROKEN_AT", "STALE"}
NO_BLUE_STATES = {"NO_EGRESS", "NONE", "BLACKHOLE", "UNRESOLVED", "LOOP"}


def build_scene_data(data, verified_at: dict | None = None) -> SceneData:
    """controller.LabView -> SceneData.  ``verified_at``: egress key -> time of the last
    successful Verify Internet (UI-side, for the 'INTERNET ✓ hh:mm' caption)."""
    sd = SceneData()
    labels = {n.key: n.label for n in data.nodes}
    tv = getattr(data, "telemetry", None)
    sd.fans = link_fans(data.links)
    if tv is None:
        return sd
    verified_at = verified_at or {}
    sd.vlans = lab_vlans(tv)
    sd.vlan_hues = T.vlan_hues([v for v in sd.vlans if v != 1])
    if 1 in sd.vlans:
        sd.vlan_hues[1] = T.NEUTRAL

    # ---------------------------------------------------------------- default paths (V6)
    marks: dict[str, list] = defaultdict(list)
    egress_users: dict[str, list] = defaultdict(list)
    for origin, p in tv.paths.items():
        state = ev(p.state)
        name = labels.get(origin, origin)
        if state in PATH_BREAK and p.broken_at:
            if state == "STALE":
                marks[p.broken_at].append(("clock", T.NEUTRAL, f"{name}'s default path: data too old here (⏱)"))
            elif ev(p.broken_kind) == "UNKNOWN":
                marks[p.broken_at].append(("question", T.NEUTRAL,
                                           f"{name}'s default path: state of this link is unknown"))
            else:
                marks[p.broken_at].append(("cross", T.DOWN_RED,
                                           f"{name}'s default path breaks here" + (f": {p.detail}" if p.detail else "")))
        if state in NO_BLUE_STATES:
            sd.no_exit[origin] = state
        if p.egress_node and state in ("EGRESS", "VERIFIED_INTERNET"):
            egress_users[p.egress_node].append((origin, p))
    for lk, items in marks.items():
        # one marker per link: a real break (✕) wins over '?' / '⏱'
        items.sort(key=lambda m: {"cross": 0, "question": 1, "clock": 2}[m[0]])
        icon, col, _ = items[0]
        sd.path_marks[lk] = (icon, col, "<br>".join(dict.fromkeys(t for _, _, t in items)))

    # ---------------------------------------------------------------- egress tiles
    # a connector LabDesk wired to a host-KVM VM is a bridge to that VM (a cable), not an exit
    kvm_bridged = {e for l in data.links if str(l.key).startswith("kvm:") for e in (l.a, l.b)}
    for n in data.nodes:
        nt = n.tele
        if nt is None or ev(nt.role) != "egress":
            continue
        users = egress_users.get(n.key, [])
        if n.key in kvm_bridged and not users:
            continue
        mode = next((p.egress_mode for _, p in users if p.egress_mode), None) or _egress_mode_guess(n)
        addr = None
        for _, p in users:
            if p.hops and p.hops[-1].to_node == n.key and p.hops[-1].via_ip:
                addr = p.hops[-1].via_ip
                break
        verified = any(ev(p.state) == "VERIFIED_INTERNET" for _, p in users)
        who = ", ".join(sorted(labels.get(o, o) for o, _ in users))
        cloud = mode == "Cloud" or getattr(n.dtype, "key", "") == "ext_cloud"
        if verified:
            t = verified_at.get(n.key) or time.time()
            text = f"INTERNET ✓ {time.strftime('%H:%M', time.localtime(t))}"
            tip = (f"Verify Internet succeeded at {time.strftime('%H:%M:%S', time.localtime(t))} "
                   f"(ping 8.8.8.8 and 1.1.1.1 got replies).<br>Default routes of {who} leave the lab here.")
        elif cloud:
            if not users:
                continue                         # the cloud tile shows its own '8.8.8.8 · 1.1.1.1' pill
            text = "egress" + (f" · {addr}" if addr else "")
            tip = (f"The simulated Internet (host namespace, answers 8.8.8.8 / 1.1.1.1).<br>"
                   f"Default routes of {who} leave the lab here."
                   "<br><i>Click to flash the blue paths · Simulate ▸ Verify Internet to test it</i>")
        else:
            text = f"{mode or 'NAT'} egress" + (f" · {addr}" if addr else "")
            tip = (f"Where the lab meets the outside world ({'NAT' if mode != 'Bridge' else 'host bridge'}).<br>"
                   + (f"Default routes of {who} leave the lab here." if who else "No router's default route uses it yet.")
                   + "<br><i>Click to flash the blue paths · Simulate ▸ Verify Internet to test it</i>")
        sd.egress_caption[n.key] = (text, verified, tip)

    # ---------------------------------------------------------------- segments: FHRP / chords (V4/V5)
    for seg in tv.segments.values():
        kind = ev(seg.kind)
        protos = {ev(k): ev(v) for k, v in seg.protocols.items()}
        if kind == "multi" and seg.switch_node:
            chips = []
            for proto in ("HSRP", "VRRP", "OSPF", "BGP"):
                if proto not in protos:
                    continue
                h = protos[proto]
                glyph = {"OK": "✓", "PARTIAL": "…", "DOWN": "!"}.get(h, "?")
                hue = T.PROTO_HUES[proto]
                detail = seg.detail.get(proto) or next((v for k, v in seg.detail.items() if ev(k) == proto), "")
                text, tip = proto, f"<b>{proto}</b> on this switch segment: {detail or h.lower()}"
                if proto == "BGP":
                    text, tip = switch_bgp_chip(getattr(seg, "bgp_sessions", None) or [], labels, detail, h)
                spec = T.ChipSpec(text, hue, T.health_fill(h, hue), T.GLYPH_ICON.get(glyph, ""),
                                  T.glyph_color(glyph, hue))
                chips.append((spec, tip, "fhrp" if proto in ("HSRP", "VRRP") else "chips"))
            if chips:
                sd.switch_chips[seg.switch_node] = chips
            fhrp = [p for p in ("HSRP", "VRRP") if p in protos]
            if fhrp:
                for lk in seg.link_keys:
                    sd.fhrp_tint[lk] = T.PROTO_HUES[fhrp[0]]
            for a, b, proto, state in seg.adjacencies:
                sd.chords.append((a, b, ev(proto), state, seg.switch_node))

    # ---------------------------------------------------------------- BGP sessions without a shared cable
    direct_bgp = set()
    for lf in tv.links.values():
        for a, b, proto, _ in lf.adjacencies:
            if ev(proto) == "BGP":
                direct_bgp.add(frozenset((a, b)))
    pair_states: dict[frozenset, list[str]] = defaultdict(list)
    for nk, nt in tv.nodes.items():
        for peer in ((nt.bgp or {}).get("peers") or []):
            other = _node_for_name(peer.get("name"), labels)
            if not other or other == nk:
                continue
            pair = frozenset((nk, other))
            if pair in direct_bgp:
                continue
            pair_states[pair].append(peer.get("state") or "")
    for pair, states in pair_states.items():
        a, b = sorted(pair)
        n_sessions = max(1, (len(states) + 1) // 2)            # both ends report the same session
        state = "Established" if all(T.adjacency_state(s) == "up" for s in states) else (states[0] or "Idle")
        sd.arcs.append((a, b, n_sessions, state))
    return sd


def switch_bgp_chip(sessions, labels: dict, detail: str = "", health: str = "") -> tuple[str, str]:
    """(chip text, tooltip) for BGP on a switch: names the routers whose session crosses it
    ('iBGP R1↔R2', 'eBGP A↔B', 'iBGP ×2') - the switch itself never runs BGP."""
    from ..facts import _pair_labels, bgp_segment_chip_text, bgp_session_words
    if not sessions:
        return "BGP", f"<b>BGP</b> across this switch: {detail or str(health).lower()}"
    text = bgp_segment_chip_text(sessions, labels)
    if len(sessions) == 1:
        a, b = _pair_labels(sessions[0], labels)
        s = sessions[0]
        body = (f"{bgp_session_words(s, labels)}.<br>This switch doesn't run BGP: "
                f"the session between {a} and {b} crosses it.")
    else:
        body = (f"{len(sessions)} BGP sessions cross this switch:<br>"
                + "<br>".join("• " + bgp_session_words(s, labels) for s in sessions)
                + "<br>This switch doesn't run BGP itself.")
    return text, f"<b>{text}</b><br>{body}"


def lab_vlans(tv) -> dict[int, str]:
    """VLAN id -> name over every managed switch of the lab (for hues and the VLAN picker)."""
    out: dict[int, str] = {}
    for nt in (tv.nodes.values() if tv is not None else []):
        sw = getattr(nt, "switch", None)
        if sw is None:
            continue
        for v in sw.vlans:
            out.setdefault(v.id, v.name)
    return dict(sorted(out.items()))


def _node_for_name(name, labels: dict) -> str | None:
    if not name:
        return None
    for k, lbl in labels.items():
        if lbl == name:
            return k
    return None


def _egress_mode_guess(nv) -> str | None:
    cfg = (nv.raw or {}).get("configuration")
    # same rule as catalog.type_for_cml_node: NAT only when it says so; anything else ('System Bridge',
    # or a bridge label such as 'PE-HUB') is a bridge
    if isinstance(cfg, str):
        return "NAT" if "NAT" in cfg else "Bridge"
    if isinstance(cfg, list):
        txt = " ".join(str(c.get("content", "")) for c in cfg if isinstance(c, dict))
        return "NAT" if "NAT" in txt else "Bridge"
    return None


EC_PITCH = 16.0              # member cables of one EtherChannel: close together (the lasso groups them)
EC_GAP = 38.0                # between two bundles / a bundle and a plain cable


def link_fans(links) -> dict:
    """Parallel links between the same two nodes get symmetric curve offsets (V2).  EtherChannel members
    (``LinkView.bundle``) are kept adjacent at a tight pitch, bundles separated by a wider gap; the order is
    stable (by the lowest interface name of each bundle) so cables do not jump when a bundle changes state."""
    groups: dict[frozenset, list] = defaultdict(list)
    for lv in links:
        groups[frozenset((lv.a, lv.b))].append(lv)
    out = {}
    for group in groups.values():
        n = len(group)
        if n > 1 and any(getattr(l, "bundle", None) for l in group):
            first: dict = {}
            for l in group:
                bk = getattr(l, "bundle", None)
                if bk:
                    first[bk] = min(first.get(bk, "~"), l.label_a or "")
            order = sorted(group, key=lambda l: ((first.get(l.bundle) if getattr(l, "bundle", None) else
                                                  (l.label_a or "")), l.label_a or "", l.key))
            pos, x, prev = {}, 0.0, None
            for l in order:
                bk = getattr(l, "bundle", None)
                if prev is not None:
                    x += EC_PITCH if (bk and bk == prev) else EC_GAP
                pos[l.key] = x
                prev = bk or ("solo", l.key)
            mid = (max(pos.values()) + min(pos.values())) / 2
            for l in order:
                off = pos[l.key] - mid
                if off and l.a > l.b:
                    off = -off
                out[l.key] = off
            continue
        for i, lv in enumerate(sorted(group, key=lambda l: (l.label_a or "", l.key))):
            off = (i - (n - 1) / 2) * (26.0 if n != 2 else 44.0)   # a pair needs room for two end pills
            if off and lv.a > lv.b:                  # same side regardless of a/b orientation
                off = -off
            out[lv.key] = off
    return out


# ---------------------------------------------------------------------------- inspector text
SIDE_WORDS = {"UP": "UP", "ADMIN_DOWN": "ADMIN DOWN", "DOWN": "DOWN", "PROTO_DOWN": "PROTO DOWN",
              "UNKNOWN": "UNKNOWN", "NOT_APPLICABLE": "—"}


def link_header(lv, labels: dict) -> str:
    """'R1 Gi0/2 ⟷ R3 Gi0/0 · 10.0.13.0/30 · P2P (OSPF)'"""
    tele = lv.tele
    parts = [f"{labels.get(lv.a, '?')} {lv.label_a or ''}".strip() + " ⟷ "
             + f"{labels.get(lv.b, '?')} {lv.label_b or ''}".strip()]
    if tele is not None and tele.subnet:
        parts.append(tele.subnet)
    src = ev(lv.p2p_source) if lv.p2p_source is not None else None
    if lv.p2p and src:
        parts.append("P2P (" + {"OSPF_OPER": "OSPF", "JUNOS_PTTOPT": "Junos OSPF", "FORTIOS_P2P": "FortiOS OSPF",
                                "MEDIUM": "serial"}.get(src, src) + ")")
    elif lv.p2p_conflict:
        parts.append("P2P? (configured, not active)")
    elif tele is not None and tele.bcast:
        parts.append("OSPF broadcast" + (f" ({tele.bcast_detail})" if tele.bcast_detail else ""))
    return " · ".join(parts)


# ---------------------------------------------------------------------------- midpoint stack placement
def _overlap(a: tuple, b: tuple) -> float:
    """Intersection area of two (x, y, w, h) rects."""
    dx = min(a[0] + a[2], b[0] + b[2]) - max(a[0], b[0])
    dy = min(a[1] + a[3], b[1] + b[3]) - max(a[1], b[1])
    return dx * dy if dx > 0 and dy > 0 else 0.0


def place_stacks(entries: list, obstacles: list | None = None, pad: float = 3.0, strokes: dict | None = None,
                 stroke_weight: float = 0.4, tolerate: float = 0.12) -> dict:
    """Choose where along each link its midpoint stack (down pill, beads, TRUNK, subnet label…) goes so
    that no two stacks overlap (crossing links, parallel links, a stack sitting on a node tile).

    entries: [(key, w, h, [(along, cx, cy), ...])] - candidate centres in scene coordinates, the
    preferred one first (the middle of the free stretch).  obstacles: [(x, y, w, h)] that should be
    avoided too (node tiles, chips, interface pills).  Greedy and deterministic: bigger stacks are placed
    first; each takes its first candidate with no overlap, else the one with the least overlap.
    ``strokes``: {link key: [(x, y, w, h)]} small samples of each cable's stroke; a stack avoids the
    other cables' strokes (weighted ``stroke_weight``) so a label beside its cable never lands on a
    neighbour.  An entry may carry a 5th element ``(w, h, candidates)``: a compact form (shorter subnet
    label) tried only when the best full-size candidate still overlaps more than ``tolerate`` of its area.  Returns {key: (along, cx, cy)} - the chosen
    candidate (it may sit beside the cable) - with a 4th element True when the compact form was chosen."""
    out = {}
    placed: list[tuple] = []
    obst = list(obstacles or [])
    order = sorted(entries, key=lambda e: (-(e[1] * e[2]), str(e[0])))

    def cost_of(key, r):
        c = sum(_overlap(r, p) for p in placed) + sum(_overlap(r, o) for o in obst)
        if strokes:
            c += stroke_weight * sum(_overlap(r, o) for k2, ss in strokes.items() if k2 != key for o in ss)
        return c

    for entry in order:
        key, w, h, cands = entry[:4]
        alt = entry[4] if len(entry) > 4 else None       # (w, h, candidates) of the compact form
        if not cands:
            continue
        best, best_cost = None, None
        variants = [(w, h, cands, False)] + ([(alt[0], alt[1], alt[2], True)] if alt else [])
        for vw, vh, vc, compact in variants:
            for along, cx, cy in vc:
                r = (cx - vw / 2 - pad, cy - vh / 2 - pad, vw + 2 * pad, vh + 2 * pad)
                cost = cost_of(key, r)
                if best_cost is None or cost < best_cost - 1e-6:
                    best, best_cost = ((along, cx, cy) + ((True,) if compact else ()), r), cost
                if cost == 0:
                    break
            if best_cost is not None and best_cost <= tolerate * vw * vh:
                break                                  # good enough: keep the full label
        out[key] = best[0]
        placed.append(best[1])
    return out
