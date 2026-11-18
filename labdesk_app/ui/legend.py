"""S9 legend: a contextual, bounded, collapsible overlay card (QWidget child of the view's viewport).

* 240 px card, radius 10, 12 px padding, N1 @ 96 % (light) / N4 (dark), 1 px N6/N7 border, e2 in light.
  Anchored bottom-left at a 12 px inset; its height never exceeds the viewport - 24 (internal scrollbar),
  so rows never overlap and labels wrap instead of clipping.
* Header (28 px): icon, LEGEND (caption, N11) and a chevron.  Collapsed it is a 28 px "Legend ⌄" pill.
  One persisted collapse state; expanded on first run.
* Sections Links / Routing / Switching / Nodes / Cloud; 20 px rows with a 36 px sample painted by the
  same theme helpers as the canvas (shape and colour together), plus the S2 end-shape key.
* Contextual: a debounced (1 s) presence summary of the scene decides which rows show, with hysteresis
  (a row stays for 3 summaries after its feature disappears) so the card does not jitter every poll.
  Hidden while no lab is open.  Clicking a layer row toggles the shared Layers QAction.
"""
from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (QFrame, QHBoxLayout, QLabel, QScrollArea, QSizePolicy, QVBoxLayout, QWidget)

from . import icons
from . import theme as T
from .graphics import draw_glyph, tile_path  # noqa: F401  (re-exported for older callers)

SAMPLE_W = 36
ROW_H = 20
PAD = 12
HEADER_H = 28
HYSTERESIS = 3          # summaries a row survives after its feature disappeared


def _as_bool(v) -> bool:
    return str(v).lower() in ("true", "1", "yes", "on")


class _Sample(QWidget):
    def __init__(self, painter_fn, parent=None):
        super().__init__(parent)
        self.fn = painter_fn
        self.setFixedSize(SAMPLE_W, ROW_H)

    def paintEvent(self, ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.TextAntialiasing)
        tk = T.tokens(self.palette())
        self.fn(p, QRectF(0, 0, SAMPLE_W, ROW_H), tk)
        p.end()


class LegendRow(QWidget):
    clicked = Signal()

    def __init__(self, text: str, painter_fn, layer: str | None = None, tip: str = "", parent=None):
        super().__init__(parent)
        self.layer = layer
        self.setCursor(Qt.PointingHandCursor if layer else Qt.ArrowCursor)
        h = QHBoxLayout(self)
        h.setContentsMargins(2, 0, 2, 0)
        h.setSpacing(8)
        self.sample = _Sample(painter_fn, self)
        self.label = QLabel(text, self)
        self.label.setFont(T.type("caption", weight=500))
        self.label.setWordWrap(True)
        self.label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        tk = T.tokens()
        self.label.setStyleSheet(f"color: {tk.N(12).name()}; background: transparent;")
        h.addWidget(self.sample, 0, Qt.AlignTop)
        h.addWidget(self.label, 1)
        self.setMinimumHeight(ROW_H)
        self.setToolTip(tip or (f"click to toggle the '{layer}' layer" if layer else ""))
        self._hover = False

    def enterEvent(self, ev):
        self._hover = True
        self.update()

    def leaveEvent(self, ev):
        self._hover = False
        self.update()

    def paintEvent(self, ev):
        if self._hover and self.layer:
            p = QPainter(self)
            p.setRenderHint(QPainter.Antialiasing)
            p.setPen(Qt.NoPen)
            p.setBrush(T.alpha(T.tokens().N(12), 0.06))
            p.drawRoundedRect(QRectF(self.rect()), 4, 4)
            p.end()

    def mouseReleaseEvent(self, ev):
        if ev.button() == Qt.LeftButton and self.layer:
            self.clicked.emit()


# ------------------------------------------------------------------ sample painters (36 x 20)
def _line(p, r, tk, color=None, width=2.2):
    p.setPen(QPen(color or T.status_fill("up", tk), width, Qt.SolidLine, Qt.RoundCap))
    p.drawLine(QPointF(r.left() + 3, r.center().y()), QPointF(r.right() - 3, r.center().y()))


def _link(kind, ends=None, **kw):
    def fn(p, r, tk):
        T.draw_link_sample(p, r, kind, tk, ends=ends, **kw)
    return fn


def _ends_key(p, r, tk):
    """S2 shape key: circle up, bar down, ring protocol-down, square admin-down."""
    y = r.center().y()
    for i, (g, ang) in enumerate((("up", 0), ("down", 0), ("proto_down", 0), ("admin_down", 0))):
        T.draw_status_glyph(p, QPointF(r.left() + 4.5 + i * 9, y), g, 7.0, tk=tk, angle=ang)


def _bead(text, outlined=False):
    def fn(p, r, tk):
        _line(p, r, tk)
        T.draw_bead(p, r.center(), text, tk, outlined=outlined, lod=1.0)
    return fn


def _chip(text, hue_kind, glyph="✓", outline=False, blue_bar=False, health="OK"):
    def fn(p, r, tk):
        hue = T.PROTO_HUES[hue_kind]
        spec = T.ChipSpec(text, hue, None if outline else T.health_fill(health, hue),
                          T.GLYPH_ICON.get(glyph, ""), T.glyph_color(glyph, hue), outline=outline,
                          blue_bar=blue_bar)
        p.save()
        f = T.type("micro")
        w = T.chip_width(spec, T.fm(f))
        if w > SAMPLE_W - 1 and spec.icon:            # keep the word legible: drop the health glyph first
            spec.icon = ""
            w = T.chip_width(spec, T.fm(f))
        sc = min(1.0, (SAMPLE_W - 1) / w)
        p.translate(r.left() + 0.5, r.center().y() - T.CHIP_H * sc / 2)
        p.scale(sc, sc)
        T.draw_chip(p, 0, 0, spec, tk, f)
        p.restore()
    return fn


def _rail(proto, state):
    def fn(p, r, tk):
        p.setPen(T.rail_pen(proto, state, tk))
        y = r.center().y()
        p.drawLine(QPointF(r.left() + 2, y), QPointF(r.right() - 12, y))
        T.draw_icon(p, T.ADJ_ICON[state], QRectF(r.right() - 11, y - 4.5, 9, 9), T.PROTO_HUES[proto], 1.5)
    return fn


def _fhrp(proto, role, primary):
    def fn(p, r, tk):
        hue = QColor(T.PROTO_HUES[proto])
        rr = QRectF(r.left() + 1, r.center().y() - 7, r.width() - 2, 14)
        p.setPen(QPen(hue, 1.0))
        p.setBrush(hue if primary else tk.card)
        p.drawRoundedRect(rr.adjusted(0.5, 0.5, -0.5, -0.5), 7, 7)
        p.setFont(T.type("micro"))
        p.setPen(QColor("white") if primary else T._hue_text(hue, tk))
        p.drawText(rr, Qt.AlignCenter, f"{proto[0]} {role[:3]}")
    return fn


def _mark(icon, color):
    def fn(p, r, tk):
        c = r.center()
        _line(p, r, tk, QColor(T.ROUTE_BLUE), 2.4)
        p.setPen(QPen(tk.card, 2))
        p.setBrush(T.resolve(color, tk))
        p.drawEllipse(c, 7, 7)
        T.draw_icon(p, icon, QRectF(c.x() - 4.5, c.y() - 4.5, 9, 9), QColor("white"), 1.7)
    return fn


def _pill(text, icon, color, dashed=False):
    def fn(p, r, tk):
        T.draw_pill(p, r.center(), text, tk, border=T.resolve(color, tk), text_color=T.text_for(color, tk),
                    icon=icon, icon_color=color, height=14, pad=3, dashed=dashed,
                    f=T.type("caption", scale=0.8, weight=500))
    return fn


def _tile(vendor_key, size=17, **kw):
    from ..catalog import BY_KEY
    from .graphics import paint_tile

    def fn(p, r, tk):
        dt = BY_KEY.get(vendor_key)
        if dt is None:
            return
        p.save()
        p.translate(r.center())
        paint_tile(p, dt, size, **kw)
        p.restore()
    return fn


def _badge(glyph, state):
    def fn(p, r, tk):
        from .graphics import BADGE, NodeItem
        c = r.center()
        fill = {"booted": T.status_fill("up", tk), "x": T.status_fill("down", tk),
                "disconnected": T.status_fill("down", tk)}.get(glyph, tk.N(9))
        NodeItem._draw_badge(p, c, BADGE, glyph, fill, tk)
        if glyph == "booting":
            p.setPen(QPen(QColor("white"), 1.6, Qt.SolidLine, Qt.RoundCap))
            rr = BADGE / 2 - 2.6
            p.drawArc(QRectF(c.x() - rr, c.y() - rr, 2 * rr, 2 * rr), 90 * 16, -260 * 16)
    return fn


def _trunk(p, r, tk):
    _line(p, r, tk)
    p.save()
    p.translate(r.center())
    p.scale(0.85, 0.85)
    T.draw_trunk_bead(p, QPointF(0, 0), "TRUNK", tk)
    p.restore()


def _vchip(p, r, tk):
    _line(p, r, tk)
    hue = QColor(T.VLAN_PALETTE[0])
    T.draw_pill(p, r.center(), "V10", tk, border=hue, text_color=T._hue_text(hue, tk), fill=T.alpha(hue, 0.14),
                bold=True, height=13, pad=3, f=T.type("caption", scale=0.8))


def _stp_block(p, r, tk):
    y = r.center().y()
    p.setPen(QPen(T.status_fill("up", tk), 2.2))
    p.drawLine(QPointF(r.right() - 1, y), QPointF(r.center().x(), y))
    pen = QPen(tk.N(9), 2.2, Qt.SolidLine, Qt.FlatCap)
    pen.setDashPattern([2.5, 2.2])
    p.setPen(pen)
    p.drawLine(QPointF(r.left() + 6, y), QPointF(r.center().x(), y))
    c = QPointF(r.left() + 7, y)
    p.setPen(QPen(tk.card, 1.3))
    p.setBrush(QColor(T.STP_BLOCK))
    p.drawEllipse(c, 6, 6)
    if not tk.dark:
        p.setPen(QPen(T.status_text("warn", tk), 1.0))
        p.setBrush(Qt.NoBrush)
        p.drawEllipse(c, 5.5, 5.5)
    T.draw_icon(p, "slash", QRectF(c.x() - 4, c.y() - 4, 8, 8), QColor("white"), 1.8)
    T.draw_pill(p, QPointF(r.center().x() + 7, y), "BLK", tk, border=QColor(T.STP_BLOCK),
                text_color=T.status_text("warn", tk) if not tk.dark else QColor("#FDBA74"), bold=True, height=12,
                pad=2, fill=T.alpha(T.STP_BLOCK, 0.14), hatch=T.STP_BLOCK, f=T.type("micro"))


def _ec(proto, state="up"):
    """Two member strokes through the bundle's lasso, outline pattern per protocol (as on the canvas)."""
    def fn(p, r, tk):
        cx, cy = r.center().x(), r.center().y()
        p.setPen(T.stroke_pen(T.LINK_STROKES["UP" if state != "member" else "UP"], width=1.6, tk=tk))
        for dy in (-3.0, 3.0):
            if state == "member" and dy > 0:
                pen = QPen(T.alpha(tk.N(10), 0.85), 1.6)
                pen.setDashPattern([2.5, 2.0])
                p.setPen(pen)
            p.drawLine(QPointF(r.left() + 2, cy + dy * 0.5), QPointF(r.right() - 2, cy + dy * 0.5) + QPointF(0, dy * 0.5))
        st = state if state in ("partial", "mismatch", "down") else "up"
        p.setPen(T.ec_lasso_pen(proto, st, tk, 1.4))
        p.setBrush(T.alpha(T.ec_hue(tk) if st == "up" else T.ec_state_colors(st, tk)[0], 0.12))
        p.drawEllipse(QPointF(cx, cy), 8.5, 7.0)
        if state == "mismatch":
            T.draw_status_glyph(p, QPointF(r.right() - 5, cy - 4), "warn", 8.0, T.status_fill("warn", tk), tk)
        if state == "member":
            col = T.status_fill("warn", tk)
            p.setPen(QPen(col, 1.4))
            p.setBrush(tk.card)
            p.drawEllipse(QPointF(r.right() - 6, cy + 3), 3.4, 3.4)
    return fn


def _ec_blk(p, r, tk):
    cy = r.center().y()
    p.setPen(T.stroke_pen(T.LINK_STROKES["UP"], width=1.6, tk=tk))
    for dy in (-2.0, 2.0):
        p.drawLine(QPointF(r.left() + 10, cy + dy), QPointF(r.right() - 2, cy + dy))
    c = QPointF(r.left() + 7, cy)
    p.setPen(QPen(tk.base, 1.2))
    p.setBrush(QColor(T.STP_BLOCK))
    p.drawEllipse(c, 6.0, 6.0)
    T.draw_icon(p, "slash", QRectF(c.x() - 4, c.y() - 4, 8, 8), QColor("white"), 1.8)


def _root(p, r, tk):
    T.draw_pill(p, r.center(), "ROOT", tk, border=QColor("#8A6400"), text_color=QColor("#3D2E00"),
                fill=QColor(T.ROOT_GOLD), bold=True, height=13, pad=3, f=T.type("micro"))


def _vlan_halo(hue):
    def fn(p, r, tk):
        y = r.center().y()
        p.setPen(QPen(T.alpha(hue, 0.30), 9, Qt.SolidLine, Qt.RoundCap))
        p.drawLine(QPointF(r.left() + 5, y), QPointF(r.right() - 5, y))
        p.setPen(QPen(T.status_fill("up", tk), 2.2))
        p.drawLine(QPointF(r.left() + 5, y), QPointF(r.right() - 5, y))
    return fn


def _subnet(text, style):
    def fn(p, r, tk):
        T.draw_subnet_label(p, r.center(), text, tk, style)
    return fn


def _collector(text, icon, color):
    def fn(p, r, tk):
        T.draw_pill(p, r.center(), text, tk, border=T.alpha(T.resolve(color, tk), 0.8), text_color=tk.muted,
                    icon=icon, icon_color=color, height=14, pad=3, f=T.type("caption", scale=0.75, weight=500))
    return fn


def _cloud_down(p, r, tk):
    y = r.center().y()
    pen = QPen(T.alpha(T.ROUTE_BLUE, 0.55), 3.0, Qt.SolidLine, Qt.FlatCap)
    pen.setDashPattern([2.0, 1.33])
    p.setPen(pen)
    p.drawLine(QPointF(r.left() + 2, y), QPointF(r.right() - 10, y))
    from .graphics import CLOUD_BADGE, NodeItem
    NodeItem._draw_badge(p, QPointF(r.right() - 8, y), CLOUD_BADGE * 0.85, "x", T.status_fill("down", tk), tk)


def _inet(p, r, tk):
    _tile("ext_cloud", 20)(p, r, tk)


# ------------------------------------------------------------------ rows: (key, text, painter, layer, tooltip)
# ``key``: presence key (None = always shown while its section is shown)
SECTIONS = {
    "Links": [
        ("link:UP", "up (both ends up/up)", _link("UP", ends="up"), None, ""),
        (None, "end shapes: up · down · protocol down · admin down", _ends_key, None,
         "the shape at each end of a cable tells its state without colour: filled circle = up, bar = down, "
         "hollow ring = line protocol down, filled square = admin down / not probed"),
        ("link:ADMIN_DOWN", "admin down (shutdown)", _link("ADMIN_DOWN", ends="admin_down"), None, ""),
        ("link:LINK_DOWN", "link down (no carrier)", _link("LINK_DOWN", ends="down"), None, ""),
        ("link:PROTOCOL_DOWN", "line protocol down", _link("PROTOCOL_DOWN", ends="proto_down"), None, ""),
        ("link:CABLE_PULLED", "cable pulled (simulated)", _link("CABLE_PULLED"), None, ""),
        ("link:OFF", "not started / node off", _link("LINK_NOT_STARTED"), None, ""),
        ("link:UNKNOWN", "unknown / not read (no live data)", _link("UNKNOWN", ends="neutral"), None,
         "LabDesk shows only what it reads live from the devices: no fresh read = unknown, never a guess"),
        ("pill:unread", "port name not read from device", _pill("Gi0/1", "", T.NEUTRAL, dashed=True), None,
         "dashed interface pill: the name is CML's wiring (the slot the cable plugs into); the device has not "
         "reported this interface yet (stopped, not probed yet, or not read by LabDesk)"),
        ("kvm", "host-KVM link (double line)", _link("UP", kvm=True), None, ""),
        ("cond", "conditioned (delay / loss)", _pill("50 ms", "bolt", "#6D28D9"), None, ""),
    ],
    "Routing": [
        ("default", "default route to internet", _link("UP", blue=True), "default",
         "blue = the path packets to 0.0.0.0/0 take; › shows the direction"),
        ("mark", "default path breaks here", _mark("cross", T.DOWN_RED), "default", ""),
        ("route_src", "route source (S* B* O*E2)", _chip("O*E2", "DEFAULT", "", True, True), "default",
         "outline chip; the blue bar means a fresh, unbroken path to the egress"),
        ("no_default", "no default route (0/0 ✕)", _chip("0/0 ✕", "DEFAULT", "", True), "default",
         "this router has no route to 0.0.0.0/0, so it cannot reach the internet (real state, not an error "
         "of LabDesk) - hover the chip for details"),
        ("bead:P2P", "OSPF point-to-point", _bead("P2P"), "p2p", ""),
        ("bead:P2P?", "p2p configured, not active", _bead("P2P?", True), "p2p", ""),
        ("bead:BCAST", "OSPF broadcast (DR/BDR)", _bead("BCAST"), "p2p", ""),
        ("chip:OSPF", "OSPF up, neighbours FULL", _chip("OSPF", "OSPF"), "chips",
         "✓ FULL · … converging · ! no neighbour"),
        ("chip:BGP", "BGP sessions established", _chip("BGP", "BGP"), "chips",
         "on a router: BGP <its AS> ✓ = all of its BGP sessions are up"),
        ("chip:BGPSW", "BGP session crossing a switch", _chip("iBGP R1↔R2", "BGP"), "chips",
         "on a switch: the switch runs no BGP - the session between those routers (iBGP: same AS, "
         "eBGP: different AS) crosses its LAN; 'iBGP ×2' = several, hover for the list"),
        ("fhrp", "HSRP/VRRP active · master", _fhrp("HSRP", "Active", True), "fhrp", ""),
        ("fhrp", "HSRP/VRRP standby · backup", _fhrp("VRRP", "Backup", False), "fhrp", ""),
        ("rail:OSPF", "OSPF adjacency rail (✓ full)", _rail("OSPF", "up"), "ospf", ""),
        ("rail:BGP", "BGP session rail (… converging)", _rail("BGP", "converging"), "bgp", ""),
        ("traffic", "traffic (bit rate)", _pill("1.2 kb/s", "", T.NEUTRAL), "traffic", ""),
        ("subnet:match", "subnet on the cable", _subnet("/30", "match"), "subnets",
         "from the live interface addresses of both ends; the host part (.1 / .2) sits next to each "
         "interface name; it updates within ~1 s when typed on an open console"),
        ("subnet:half", "only one side configured", _subnet("½/30", "half"), "subnets", ""),
        ("subnet:mismatch", "ends in different subnets", _subnet("⚠ ≠", "mismatch"), "subnets",
         "⚠ mismatch a ≠ b: the two ends cannot reach each other"),
    ],
    "Switching": [
        ("trunk", "trunk + VLANs it carries", _trunk, "l2", "TRUNK 10,20 bead; the native VLAN is in its tooltip"),
        ("access", "access port's VLAN", _vchip, "l2", "V10 = the switch port is an access port in VLAN 10"),
        ("stp_block", "STP blocked (not down!)", _stp_block, "l2",
         "hatched amber cap + dashed half: spanning tree keeps this redundant cable as a backup "
         "(Altn/Back BLK); the cable itself is up"),
        ("root", "STP root bridge", _root, "l2", "ROOT v10,20 crown above the root switch"),
        ("chip:STP", "switch: L2 · VLANs · STP", _chip("RPVST+", "STP"), "l2", ""),
    ],
    "EtherChannel": [
        ("ec:lacp", "LACP bundle (oval, solid)", _ec("lacp"), "l2",
         "member cables inside an oval + one bead 'Po1 · LACP 2/2'; the rounded bead = LACP"),
        ("ec:pagp", "PAgP bundle (oval, dashed)", _ec("pagp"), "l2",
         "dashed oval; the bead has pointed ends: 'Po2 · PAgP 2/2'"),
        ("ec:on", "static bundle, mode on (dotted)", _ec("on"), "l2", "dotted oval, square outlined bead"),
        ("ec:partial", "partly bundled (1/2, amber)", _ec("lacp", "partial"), "l2",
         "only some members are bundled - it still works with less bandwidth"),
        ("ec:down", "Port-channel down (0/2, red)", _ec("lacp", "down"), "l2", ""),
        ("ec:mismatch", "never bundles (⚠ reason)", _ec("lacp", "mismatch"), "l2",
         "amber ⚠ bead with the reason, e.g. 'LACP passive ↔ passive — never bundles'"),
        ("ec:member", "member not bundled (s / I / D)", _ec("lacp", "member"), "l2",
         "muted dashed cable + a marker 's · suspended' / 'I · stand-alone' / 'D · down'"),
        ("ec:blk", "STP blocks the whole Po", _ec_blk, "l2",
         "one hatched 'Po2 · Altn BLK' marker for the bundle (spanning tree sees the Port-channel as one port)"),
    ],
    "Nodes": [
        ("state:booted", "running (booted)", _badge("booted", "BOOTED"), None,
         "node state badges are CML's (or libvirt's) runtime state, not read from the device"),
        ("state:booting", "booting", _badge("booting", "STARTED"), None, ""),
        ("state:stopped", "stopped / wiped (body dimmed)", _badge("stopped", "STOPPED"), None, ""),
        ("state:disconnected", "disconnected / missing", _badge("disconnected", "DISCONNECTED"), None, ""),
        ("node:cisco", "Cisco IOSv (rounded square)", _tile("iosv"), None, ""),
        ("node:l2switch", "IOSvL2 managed switch", _tile("iosvl2", 15, ports=["up", "up", "blk", "up"]), None,
         "Cisco-blue front panel; the port LEDs are live: green forwarding, amber STP-blocked, red err-disabled"),
        ("node:switch", "unmanaged switch (grey)", _tile("unmanaged_switch", 17), None, ""),
        ("node:juniper", "Juniper vJunos (hexagon)", _tile("vjunos"), None, ""),
        ("node:fortinet", "Fortinet FortiOS (shield)", _tile("fortios"), None, ""),
        ("node:host", "Linux host", _tile("alpine"), None, ""),
        ("learning", "learning… first probe running", _collector("learn", "dots", T.NEUTRAL), None, ""),
        ("paused", "paused: your console is open", _collector("paused", "clock", T.NEUTRAL), None, ""),
        ("notread", "not read: no live collector", _collector("not read", "question", T.NEUTRAL), None,
         "nothing about this device is shown until LabDesk reads it live"),
        ("stale", "stale data (faded, ⏱)", _chip("OSPF", "OSPF", "⏱"), None,
         "the last live read is older than 45 s; hover the device for its age"),
    ],
    "Cloud": [
        ("cloud", "Internet cloud (answers 8.8.8.8 / 1.1.1.1)", _inet, None, ""),
        ("cloud_down", "cloud DOWN: route exists, egress dead", _cloud_down, None,
         "grey cloud with a red ✕ badge; the default route to it is drawn dashed blue"),
        ("egress", "NAT egress of the default route", _pill("NAT", "", T.ROUTE_BLUE), None, ""),
        ("connector", "NAT / bridge connector", _tile("ext_nat", 18), None, ""),
    ],
}
LAYER_TITLES = {"default": "default route", "p2p": "P2P beads", "chips": "protocol chips", "fhrp": "HSRP/VRRP",
                "ospf": "OSPF rails", "bgp": "BGP rails", "traffic": "traffic", "l2": "switching",
                "vlan": "VLAN colouring", "subnets": "subnets"}


def legend_rows():
    """{section: [(text, painter, layer, tooltip)]} (the pre-S9 shape, for older callers)."""
    return {s: [(t, fn, layer, tip) for _k, t, fn, layer, tip in rows] for s, rows in SECTIONS.items()}


def scene_presence(scene) -> set[str]:
    """What the scene shows right now (drives the contextual rows)."""
    keys: set[str] = set()
    if scene is None:
        return keys
    ctx = getattr(scene, "ctx", None)
    for li in getattr(scene, "links", {}).values():
        try:
            vis = li.visual()
        except Exception:  # noqa: BLE001
            continue
        st = vis.stroke
        keys.add("link:" + ("OFF" if st in ("LINK_NOT_STARTED", "NODE_OFF", "LAB_OFF", "API_DOWN") else
                            "UP" if st == "API_UP" else st))
        if li.dashed:
            keys.add("kvm")
        if vis.blue:
            keys.add("default")
        if vis.bead:
            keys.add("bead:" + vis.bead)
        if vis.condition:
            keys.add("cond")
        if vis.trunk:
            keys.add("trunk")
        if vis.access_text:
            keys.add("access")
        if vis.stp_side and not vis.stp_on_bundle:
            keys.add("stp_block")
        if vis.subnet:
            keys.add("subnet:" + vis.subnet_style)
        tele = getattr(li.lv, "tele", None)
        if tele is not None and tele.protocols:
            for k in tele.protocols:
                keys.add("rail:" + str(getattr(k, "value", k)))
        if getattr(li.lv, "traffic", None) is not None:
            keys.add("traffic")
        try:
            if any((txt and not li.end_name_live(sd)) for sd, txt in (("a", li.label_a), ("b", li.label_b))):
                keys.add("pill:unread")
        except Exception:  # noqa: BLE001
            pass
        for side in vis.side_kinds or ():
            if side == "PROTO_DOWN":
                keys.add("link:PROTOCOL_DOWN")
            elif side == "ADMIN_DOWN":
                keys.add("link:ADMIN_DOWN")
            elif side == "DOWN":
                keys.add("link:LINK_DOWN")
    if ctx is not None and ctx.path_marks:
        keys.add("mark")
    for b in getattr(scene, "bundles", {}).values():
        bd = getattr(b, "bd", None)
        if bd is None:
            continue
        keys.add("ec:" + b.proto())
        st = b.state()
        if st in ("partial", "down", "mismatch"):
            keys.add("ec:" + st)
        if bd.stp_block is not None:
            keys.add("ec:blk")
        if bd.trunk_text:
            keys.add("trunk")
    for li in getattr(scene, "links", {}).values():
        try:
            if li.visual().ec_tags:
                keys.add("ec:member")
        except Exception:  # noqa: BLE001
            pass
    for n in getattr(scene, "nodes", {}).values():
        kind = n.kind
        if n.is_internet:
            keys.add("cloud")
            if n.cloud_down:
                keys.add("cloud_down")
        elif kind == "cloud":
            keys.add("connector")
        else:
            keys.add("node:" + kind)
            g = T.STATE_GLYPH.get(n.state, "disconnected")
            keys.add("state:" + g)
        if ctx is not None and n.key in ctx.egress_caption:
            keys.add("egress")
        nv = n.nv
        if nv is None:
            continue
        for c in nv.chips or ():
            keys.add("chip:" + c.kind)
            if c.kind == "ROOT":
                keys.add("root")
            if c.outline:
                keys.add("route_src")
            if (c.text or "").startswith("0/0"):
                keys.add("no_default")
            if c.stale or c.faded:
                keys.add("stale")
        if nv.fhrp:
            keys.add("fhrp")
        ck = n.collector_key
        if ck == "learning":
            keys.add("learning")
        elif ck == "paused_console":
            keys.add("paused")
        elif ck == "unverified":
            keys.add("notread")
    if ctx is not None and ctx.switch_chips:
        keys.add("chip:STP")
        for chips in ctx.switch_chips.values():
            for item in chips:
                if str(getattr(item[0], "text", "")).split(" ")[0] in ("iBGP", "eBGP", "BGP"):
                    keys.add("chip:BGPSW")
    return keys


class _Header(QWidget):
    toggled = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(HEADER_H)
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip("collapse / expand the legend")
        self.collapsed = False

    def paintEvent(self, ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        tk = T.tokens()
        r = QRectF(self.rect())
        if self.collapsed:
            ic = icons.pixmap("legend", 14, tk.N(11), self.devicePixelRatioF())
            p.drawPixmap(QPointF(10, (r.height() - 14) / 2), ic)
            p.setFont(T.type("caption", weight=600))
            p.setPen(tk.N(12))
            p.drawText(r.adjusted(30, 0, -22, 0), Qt.AlignVCenter | Qt.AlignLeft, "Legend")
            T.draw_icon(p, "chevron_up", QRectF(r.right() - 20, r.center().y() - 5, 10, 10), tk.N(11), 1.5)
        else:
            ic = icons.pixmap("legend", 14, tk.N(11), self.devicePixelRatioF())
            p.drawPixmap(QPointF(PAD, (r.height() - 14) / 2 + 1), ic)
            p.setFont(T.type("caption", upper=True))
            p.setPen(tk.N(11))
            p.drawText(r.adjusted(PAD + 20, 1, -30, 0), Qt.AlignVCenter | Qt.AlignLeft, "Legend")
            T.draw_icon(p, "chevron_down", QRectF(r.right() - PAD - 10, r.center().y() - 4, 10, 10), tk.N(11), 1.5)
        p.end()

    def mouseReleaseEvent(self, ev):
        if ev.button() == Qt.LeftButton:
            self.toggled.emit()


class Legend(QWidget):
    """Clickable contextual legend overlay.  ``layers`` is the shared ui.layers.Layers object."""

    WIDTH = 240

    def __init__(self, viewport: QWidget, layers):
        super().__init__(viewport)
        self.layers = layers
        self.setObjectName("legend")
        self.setAttribute(Qt.WA_StyledBackground, False)
        st = getattr(layers, "settings", None)
        self.settings = st
        self.minimized = bool(st is not None and (_as_bool(st.value("legend/collapsed", False))
                                                  or _as_bool(st.value("legend/minimized", False))))
        self.collapsed: dict = {}                       # (pre-S9 per-section state; unused)
        self.full = False                               # (pre-S9 'show all'; contextual rows replace it)
        self.freshness: list[tuple[str, float | None]] = []
        self.vlans: dict[int, str] = {}                 # lab VLANs (managed switches); {} hides the picker
        self.vlan_hues: dict[int, str] = {}
        self.user_visible = True
        self.has_lab = False
        self._present: dict[str, int] = {}              # presence key -> summaries left (hysteresis)
        self._sig = None
        self._fresh_label: QLabel | None = None
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self.refresh_presence)
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 2)
        v.setSpacing(0)
        self.header = _Header(self)
        self.header.toggled.connect(lambda: self.set_minimized(not self.minimized))
        v.addWidget(self.header)
        self.scroll = QScrollArea(self)
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll.setStyleSheet("QScrollArea { background: transparent; } "
                                  "QScrollArea > QWidget > QWidget { background: transparent; }")
        self.body = QWidget()
        self._v = QVBoxLayout(self.body)
        self._v.setContentsMargins(PAD - 2, 0, PAD - 4, PAD - 2)
        self._v.setSpacing(1)
        self.scroll.setWidget(self.body)
        v.addWidget(self.scroll, 1)
        layers.changed.connect(lambda *_: self.rebuild())
        self.rebuild()

    # ------------------------------------------------------------------ visibility
    def setVisible(self, on: bool) -> None:          # noqa: N802  (Qt API)
        self.user_visible = bool(on)
        super().setVisible(bool(on) and self.has_lab)

    def _apply_visibility(self) -> None:
        super().setVisible(self.user_visible and self.has_lab)
        if self.isVisible():
            self.reposition()

    # ------------------------------------------------------------------ painting
    def paintEvent(self, ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        tk = T.tokens()
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -2.5)
        rad = HEADER_H / 2 if self.minimized else T.RADIUS_MD
        if not tk.dark:                                  # e2
            for i, a in ((3.0, 0.035), (1.5, 0.05)):
                sh = QPainterPath()
                sh.addRoundedRect(r.translated(0, i), rad, rad)
                p.fillPath(sh, QColor(16, 24, 40, int(255 * a)))
        if self.minimized:
            bg = tk.N(3) if not tk.dark else tk.N(4)
        else:
            bg = QColor(tk.N(1)) if not tk.dark else QColor(tk.N(4))
            if not tk.dark:
                bg.setAlphaF(0.96)
        p.setPen(QPen(tk.N(6) if not tk.dark else tk.N(7), 1))
        p.setBrush(bg)
        p.drawRoundedRect(r, rad, rad)
        p.end()

    # ------------------------------------------------------------------ content
    def _clear(self):
        self._fresh_label = None
        while self._v.count():
            it = self._v.takeAt(0)
            w = it.widget()
            if w is not None:
                w.hide()
                w.setParent(None)
                w.deleteLater()

    def _section_header(self, text: str):
        lab = QLabel(text)
        lab.setFont(T.type("caption", upper=True))
        lab.setStyleSheet(f"color: {T.tokens().N(11).name()}; padding-top: 6px; padding-bottom: 1px;"
                          " background: transparent;")
        self._v.addWidget(lab)
        lab.show()

    def _shown(self, key: str | None) -> bool:
        return key is None or key in self._present

    def rebuild(self):
        self._clear()
        self.header.collapsed = self.minimized
        self.header.update()
        self.scroll.setVisible(not self.minimized)
        if self.minimized:
            self.reposition()
            self.update()
            return
        off_layers: list[str] = []
        tk = T.tokens()
        for section, rows in SECTIONS.items():
            if section == "Links" and not any(k.startswith("link:") for k in self._present):
                continue
            visible = []
            for key, text, fn, layer, tip in rows:
                if not self._shown(key):
                    continue
                if key is None and section != "Links":
                    continue
                if layer and not self.layers.on(layer):
                    if layer not in off_layers:
                        off_layers.append(layer)
                    continue
                visible.append((text, fn, layer, tip))
            picker = section == "Switching" and any(v != 1 for v in self.vlans)
            if not visible and not picker:
                continue
            self._section_header(section)
            seen = set()
            for text, fn, layer, tip in visible:
                if text in seen:
                    continue
                seen.add(text)
                row = LegendRow(text, fn, layer, tip)
                if layer:
                    row.clicked.connect(lambda l=layer: self.layers.set(l, not self.layers.on(l)))
                self._v.addWidget(row)
                row.show()
            if picker:
                self._vlan_row()
        if off_layers:
            ink = tk.accent_text.name()
            lab = QLabel("Off: " + " · ".join(f"<a href='{l}' style='color:{ink}; text-decoration:none'>"
                                               f"{LAYER_TITLES.get(l, l)}</a>" for l in off_layers))
            lab.setWordWrap(True)
            lab.setToolTip("layers that are hidden - click one to show it (View ▸ Layers)")
            lab.linkActivated.connect(lambda l: self.layers.set(l, True))
            lab.setFont(T.type("caption", weight=500))
            lab.setStyleSheet(f"color: {tk.N(11).name()}; padding-top: 8px; background: transparent;")
            self._v.addWidget(lab)
            lab.show()
        if self.freshness:
            self._section_header("Freshness")
            lab = QLabel(self._fresh_text(self.freshness))
            lab.setWordWrap(True)
            lab.setFont(T.type("caption", weight=500))
            lab.setStyleSheet(f"color: {tk.N(11).name()}; background: transparent;")
            lab.setToolTip("time since the last successful console probe of each router")
            self._fresh_label = lab
            self._v.addWidget(lab)
            lab.show()
        self.body.adjustSize()
        self.reposition()
        self.update()

    def _vlan_row(self):
        """VLAN colouring picker: the row shows the highlighted VLAN; click = next VLAN (then off)."""
        user = [v for v in self.vlans if v != 1]
        if not user:
            return
        on = self.layers.on("vlan")
        cur = getattr(self.layers, "vlan", None)
        if on and cur in self.vlans:
            hue = self.vlan_hues.get(cur, T.VLAN_PALETTE[0])
            text = f"VLAN {cur} {self.vlans[cur]} highlighted ▸"
            tip = "click: next VLAN (after the last one the layer turns off) · View ▸ Layers ▸ Highlight VLAN"
        else:
            hue = T.NEUTRAL
            text = "+ highlight a VLAN…"
            tip = "colour the links and ports that carry one VLAN (click to start with VLAN %d)" % user[0]
        row = LegendRow(text, _vlan_halo(hue), "vlan", tip)
        row.clicked.connect(self._next_vlan)
        self._v.addWidget(row)
        row.show()

    def _next_vlan(self):
        user = [v for v in self.vlans if v != 1]
        if not user:
            return
        cur = getattr(self.layers, "vlan", None)
        if not self.layers.on("vlan") or cur not in user:
            self.layers.set_vlan(user[0])
        elif user.index(cur) + 1 < len(user):
            self.layers.set_vlan(user[user.index(cur) + 1])
        else:
            self.layers.set("vlan", False)
        self.rebuild()

    # ------------------------------------------------------------------ presence (contextual rows)
    def schedule_presence(self) -> None:
        if not self._present:
            self.refresh_presence()
        elif not self._timer.isActive():
            self._timer.start()

    def refresh_presence(self) -> None:
        view = self.parentWidget().parentWidget() if self.parentWidget() is not None else None
        scene = view.scene() if view is not None and hasattr(view, "scene") else None
        now = scene_presence(scene)
        has_lab = bool(scene is not None and getattr(scene, "nodes", None))
        nxt = {}
        for k in now:
            nxt[k] = HYSTERESIS
        for k, left in self._present.items():
            if k not in now and left > 1:
                nxt[k] = left - 1
        changed = set(nxt) != set(self._present) or has_lab != self.has_lab
        self._present = nxt
        if has_lab != self.has_lab:
            self.has_lab = has_lab
            self._apply_visibility()
        if changed:
            self.rebuild()

    def set_vlans(self, vlans: dict, hues: dict) -> None:
        if dict(vlans) != self.vlans or dict(hues) != self.vlan_hues:
            self.vlans, self.vlan_hues = dict(vlans), dict(hues)
            self.rebuild()
        self.schedule_presence()

    def set_minimized(self, on: bool):
        self.minimized = bool(on)
        if self.settings is not None:
            self.settings.setValue("legend/collapsed", self.minimized)
            if not self.minimized:
                self.settings.setValue("legend/seen_expanded", True)
            else:
                self.settings.setValue("legend/user_collapsed", True)
        self.rebuild()

    def set_compact(self, compact: bool) -> None:
        """L10: compact mode starts collapsed only once the user has collapsed it or already seen it."""
        if not compact or self.settings is None:
            return
        if _as_bool(self.settings.value("legend/user_collapsed", False)) or \
                _as_bool(self.settings.value("legend/seen_expanded", False)):
            self.minimized = True
            self.rebuild()

    def _toggle_full(self):
        self.full = not self.full
        self.rebuild()

    @staticmethod
    def _fresh_text(rows) -> str:
        return " · ".join(f"{name} {T.fmt_age(age).replace(' ago', '')}" for name, age in rows)

    def set_freshness(self, rows: list[tuple[str, float | None]]) -> None:
        changed = [n for n, _ in rows] != [n for n, _ in self.freshness]
        self.freshness = rows
        if changed:
            self.rebuild()
        elif self._fresh_label is not None:
            self._fresh_label.setText(self._fresh_text(rows))
        self.schedule_presence()

    # ------------------------------------------------------------------ geometry
    def content_height(self) -> int:
        if self.minimized:
            return HEADER_H
        self.body.adjustSize()
        return HEADER_H + self._v.sizeHint().height() + 4

    def reposition(self):
        par = self.parentWidget()
        if par is None:
            return
        if self.minimized:
            w, h = 112, HEADER_H + 2
        else:
            w = self.WIDTH
            self._v.activate()
            h = min(self.content_height(), max(HEADER_H + 2, par.height() - 24))
        self.resize(w, h)
        self.move(12, max(12, par.height() - h - 12))

    def sizeHint(self):
        return QSize(self.WIDTH, self.content_height())
