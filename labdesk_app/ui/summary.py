"""One set of lab counters (L5 / L6): the status bar and the idle inspector summary read the same object, so
they can never disagree.

``summarize(view_data, cloud_health)`` -> :class:`LabSummary`.  Pure: no Qt widgets, cheap enough per poll.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .scene_model import ev

RUNNING = {"BOOTED", "STARTED", "running", "booting"}
LINK_DOWN_KINDS = {"ADMIN_DOWN", "LINK_DOWN", "PROTOCOL_DOWN", "CABLE_PULLED"}


@dataclass
class Row:
    """One line of the idle summary card: ``tone`` up | down | warn | neutral; ``key`` = node (or link) to select."""
    label: str
    value: str
    tone: str = "neutral"
    key: str | None = None
    link: bool = False
    tip: str = ""


@dataclass
class LabSummary:
    title: str = ""
    lab_state: str = ""
    nodes_total: int = 0
    nodes_running: int = 0
    links_total: int = 0
    links_up: int = 0
    links_down: int = 0
    rows: list[Row] = field(default_factory=list)

    @property
    def running_text(self) -> str:
        return f"{self.nodes_running}/{self.nodes_total} running"


def lab_tone(state: str | None) -> str:
    s = (state or "").upper()
    return {"STARTED": "up", "BOOTED": "up", "RUNNING": "up"}.get(s, "neutral")


def summarize(data, cloud_health=None) -> LabSummary:
    if data is None:
        return LabSummary()
    labels = {n.key: n.label for n in data.nodes}
    s = LabSummary(title=data.title, lab_state=getattr(data, "lab_state", "") or "")
    s.nodes_total = len(data.nodes)
    s.nodes_running = sum(1 for n in data.nodes if n.state in RUNNING)
    stopped = [n for n in data.nodes if n.state not in RUNNING]
    bad_nodes = [n for n in data.nodes if n.state in ("DISCONNECTED", "missing", "unknown")]
    first_off = (bad_nodes or stopped or [None])[0]
    s.rows.append(Row("Nodes up", f"{s.nodes_running} / {s.nodes_total}",
                      "up" if s.nodes_total and not stopped else ("down" if bad_nodes else "neutral"),
                      first_off.key if first_off is not None and (stopped or bad_nodes) else None,
                      tip=(f"{first_off.label} is not running" if first_off is not None and stopped else "")))
    # links
    down_first = None
    for lv in data.links:
        s.links_total += 1
        k = ev(lv.status.kind) if lv.status is not None else ("UP" if lv.state in ("STARTED", "up") else "")
        if k == "UP":
            s.links_up += 1
        elif k in LINK_DOWN_KINDS:
            s.links_down += 1
            if down_first is None:
                down_first = lv
    tone = "down" if s.links_down else ("up" if s.links_total and s.links_up == s.links_total else "neutral")
    s.rows.append(Row("Links up", f"{s.links_up} / {s.links_total}", tone,
                      down_first.key if down_first is not None else None, link=True,
                      tip=(f"{labels.get(down_first.a, '?')} {down_first.label_a} ↔ {labels.get(down_first.b, '?')} "
                           f"{down_first.label_b}: {down_first.status.text or ev(down_first.status.kind)}"
                           if down_first is not None and down_first.status is not None else "")))
    # protocols (chips: OSPF / BGP; health OK / PARTIAL / DOWN)
    if data.telemetry is not None:
        per: dict[str, list] = {}
        for n in sorted(data.nodes, key=lambda n: n.label):
            for c in n.chips or []:
                if c.kind in ("OSPF", "BGP"):
                    per.setdefault(c.kind, []).append((n, ev(c.health)))
        for proto, items in per.items():
            ok = [n for n, h in items if h == "OK"]
            bad = [n for n, h in items if h in ("DOWN", "PARTIAL")]
            tone = "down" if any(h == "DOWN" for _, h in items) else ("warn" if bad else
                                                                      ("up" if ok else "neutral"))
            s.rows.append(Row(f"{proto} healthy", f"{len(ok)} / {len(items)}", tone, bad[0].key if bad else None,
                              tip=(f"{bad[0].label}: {proto} not fully up" if bad else "")))
        # default route
        routers = [n for n in data.nodes if n.tele is not None and ev(n.tele.role) == "router"
                   and n.tele.default_path is not None]
        if routers:
            good = [n for n in routers if ev(n.tele.default_path.state) in ("EGRESS", "VERIFIED_INTERNET")]
            bad = [n for n in routers if n not in good and ev(n.tele.default_path.state) not in ("UNKNOWN", "STALE")]
            s.rows.append(Row("Default route", f"{len(good)} / {len(routers)}",
                              "down" if bad else ("up" if good else "neutral"), bad[0].key if bad else None,
                              tip=(f"{bad[0].label}: {bad[0].tele.default_path.detail or ''}" if bad else "")))
    # cloud
    clouds = [n for n in data.nodes if n.dtype.key == "ext_cloud"]
    if clouds:
        h = cloud_health
        if h is None:
            s.rows.append(Row("Internet cloud", "checking…", "neutral", clouds[0].key))
        else:
            tone = "up" if h.ok else ("warn" if getattr(h, "state", "") == "degraded" else "down")
            s.rows.append(Row("Internet cloud", "up" if h.ok else (getattr(h, "state", "") or "down"), tone,
                              None if h.ok else clouds[0].key, tip=h.headline or ""))
    return s
