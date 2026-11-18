"""Canvas items: vendor-distinct node tiles, status-aware links, overlays and the packet animation.

Only reads the telemetry model (docs/telemetry_model.md); never parses CLI or derives paths.
The visual decisions implemented here:

* V8  one ``tile_path(kind, size)`` per vendor (IOSv rounded square, Junos hexagon, FortiOS shield)
      used for painting, the double selection outline and link anchoring; KVM ribbon; 10 px LED.
* V2/V4/V3/V6  link stroke by ``LinkStatus.kind``, midpoint stack (down text, ⌁ condition, P2P
      bead, traffic, subnet), ✕ at the down end, blue default-route hops with chevrons.
* V4  chip row (max 3 + "+N"), optional adjacency rails / chords / BGP arcs.
* V10 collector chips, freshness fading, 800 ms flash on transitions.
"""
from __future__ import annotations

import html
import math
import time
from dataclasses import dataclass, field

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import (QBrush, QColor, QFontMetricsF, QLinearGradient, QPainter, QPainterPath, QPen,
                           QPixmap, QPolygonF, QTransform)
from PySide6.QtWidgets import QGraphicsItem, QGraphicsObject, QStyleOptionGraphicsItem

from ..catalog import DeviceType
from . import theme as T
from .theme import STATE_COLORS, STATE_TEXT, VENDOR_COLORS  # noqa: F401  (re-exported)

try:                                    # enums only; the UI also works with telemetry disabled
    from ..facts import CollectorState, LinkKind, LinkSide, P2PSource, PathState
except Exception:  # pragma: no cover
    CollectorState = LinkKind = LinkSide = P2PSource = PathState = None


# ============================================================================ tile outlines
TILE = 72.0            # V8: 64 -> 72 px, centre-anchored (saved layouts do not shift)

STRIP_TEXT = {"iosv": "IOSv", "iol-xe": "IOL-XE", "iosvl2": "IOSvL2", "vjunos": "Junos", "fortios": "FortiOS",
              "alpine": "Linux", "asav": "ASAv"}


def tile_kind(dtype: DeviceType) -> str:
    if dtype.shape == "cloud":
        return "cloud"
    if dtype.vendor == "juniper":
        return "juniper"
    if dtype.vendor == "fortinet":
        return "fortinet"
    if dtype.shape == "hub":
        return "switch"                         # unmanaged switch: grey, ports only
    if dtype.shape == "switch":
        return "l2switch"                       # managed Cisco switch: blue front panel, port LEDs, L2 badge
    if dtype.vendor == "linux":
        return "host"
    return "cisco"


def tile_size(kind: str, size: float = TILE) -> tuple[float, float]:
    return {"switch": (size, size * 0.56), "l2switch": (size * 1.22, size * 0.66),
            "cloud": (size * 1.22, size * 0.74)}.get(kind, (size, size))


def tile_path(kind: str, size: float = TILE) -> QPainterPath:
    """Closed outline centred on (0, 0) - painting, selection outline and link anchoring all use it."""
    w, h = tile_size(kind, size)
    path = QPainterPath()
    if kind == "juniper":                       # hexagon
        hw, hh = w / 2, h / 2 * 0.94
        pts = [QPointF(-hw, 0), QPointF(-hw * 0.5, -hh), QPointF(hw * 0.5, -hh), QPointF(hw, 0),
               QPointF(hw * 0.5, hh), QPointF(-hw * 0.5, hh)]
        path.addPolygon(QPolygonF(pts))
        path.closeSubpath()
    elif kind == "fortinet":                    # shield
        hw, hh = w / 2 * 0.94, h / 2
        r = size * 0.10
        path.moveTo(-hw + r, -hh)
        path.lineTo(hw - r, -hh)
        path.quadTo(hw, -hh, hw, -hh + r)
        path.lineTo(hw, hh * 0.12)
        path.cubicTo(hw, hh * 0.62, hw * 0.45, hh * 0.86, 0, hh)
        path.cubicTo(-hw * 0.45, hh * 0.86, -hw, hh * 0.62, -hw, hh * 0.12)
        path.lineTo(-hw, -hh + r)
        path.quadTo(-hw, -hh, -hw + r, -hh)
        path.closeSubpath()
    elif kind == "cloud":
        hw, hh = w / 2, h / 2
        path.moveTo(-hw * 0.70, hh)
        path.cubicTo(-hw * 1.08, hh, -hw * 1.08, hh * 0.05, -hw * 0.66, hh * 0.02)
        path.cubicTo(-hw * 0.72, -hh * 0.72, -hw * 0.08, -hh * 0.92, hw * 0.02, -hh * 0.42)
        path.cubicTo(hw * 0.2, -hh * 1.08, hw * 0.86, -hh * 0.9, hw * 0.74, -hh * 0.06)
        path.cubicTo(hw * 1.1, -hh * 0.02, hw * 1.1, hh, hw * 0.70, hh)
        path.closeSubpath()
    elif kind == "switch":
        path.addRoundedRect(QRectF(-w / 2, -h / 2, w, h), size * 0.10, size * 0.10)
    elif kind == "l2switch":
        path.addRoundedRect(QRectF(-w / 2, -h / 2, w, h), size * 0.13, size * 0.13)
    elif kind == "host":
        path.addRoundedRect(QRectF(-w / 2, -h / 2, w, h), size * 0.30, size * 0.30)
    else:                                       # cisco: rounded square
        path.addRoundedRect(QRectF(-w / 2, -h / 2, w, h), size * 0.18, size * 0.18)     # S7: radius 13 @ 72
    return path


def anchor_point(path: QPainterPath, direction: QPointF, gap: float = 3.0) -> QPointF:
    """Where a ray from (0,0) along `direction` leaves `path` (+ gap), by bisection."""
    d = T.unit(direction)
    br = path.boundingRect()
    hi = math.hypot(br.width(), br.height())
    lo = 0.0
    for _ in range(18):
        mid = (lo + hi) / 2
        if path.contains(QPointF(d.x() * mid, d.y() * mid)):
            lo = mid
        else:
            hi = mid
    r = hi + gap
    return QPointF(d.x() * r, d.y() * r)


# ============================================================================ glyphs
def draw_glyph(p: QPainter, rect: QRectF, shape: str, color: QColor = QColor("white")):
    """White device glyph (router / switch / host / cloud / firewall / hub) in rect."""
    p.save()
    pen = QPen(color, max(1.5, rect.width() * 0.07), Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
    p.setPen(pen)
    p.setBrush(Qt.NoBrush)
    c = rect.center()
    w, h = rect.width(), rect.height()
    if shape == "router":
        r = QRectF(c.x() - w * 0.40, c.y() - h * 0.40, w * 0.80, h * 0.80)
        p.drawEllipse(r)
        a = w * 0.13
        for dx, dy in ((1, 1), (-1, -1)):          # two arrows in, two out (classic router mark)
            y = c.y() + dy * h * 0.12
            x0, x1 = c.x() - dx * w * 0.26, c.x() + dx * w * 0.26
            p.drawLine(QPointF(x0, y), QPointF(x1, y))
            p.drawLine(QPointF(x1, y), QPointF(x1 - dx * a * 0.7, y - a * 0.55))
            p.drawLine(QPointF(x1, y), QPointF(x1 - dx * a * 0.7, y + a * 0.55))
    elif shape == "switch":
        r = QRectF(c.x() - w * 0.42, c.y() - h * 0.26, w * 0.84, h * 0.52)
        p.drawRoundedRect(r, w * 0.06, w * 0.06)
        for i, dy in enumerate((-0.1, 0.1)):
            y = c.y() + dy * h
            d = 1 if i == 0 else -1
            x0, x1 = c.x() - w * 0.26 * d, c.x() + w * 0.26 * d
            p.drawLine(QPointF(x0, y), QPointF(x1, y))
            p.drawLine(QPointF(x1, y), QPointF(x1 - d * w * 0.08, y - h * 0.06))
            p.drawLine(QPointF(x1, y), QPointF(x1 - d * w * 0.08, y + h * 0.06))
    elif shape == "hub":
        r = QRectF(c.x() - w * 0.42, c.y() - h * 0.18, w * 0.84, h * 0.36)
        p.drawRoundedRect(r, w * 0.05, w * 0.05)
        for i in range(5):
            x = r.left() + r.width() * (i + 1) / 6
            p.drawLine(QPointF(x, r.top() + h * 0.08), QPointF(x, r.bottom() - h * 0.08))
    elif shape == "host":
        r = QRectF(c.x() - w * 0.36, c.y() - h * 0.32, w * 0.72, h * 0.46)
        p.drawRoundedRect(r, w * 0.04, w * 0.04)
        p.drawLine(QPointF(c.x(), r.bottom()), QPointF(c.x(), r.bottom() + h * 0.12))
        p.drawLine(QPointF(c.x() - w * 0.2, r.bottom() + h * 0.14), QPointF(c.x() + w * 0.2, r.bottom() + h * 0.14))
    elif shape == "cloud":
        path = QPainterPath()
        bx, by = c.x() - w * 0.40, c.y() + h * 0.18
        path.moveTo(bx, by)
        path.cubicTo(bx - w * 0.08, by - h * 0.25, bx + w * 0.12, by - h * 0.36, bx + w * 0.24, by - h * 0.28)
        path.cubicTo(bx + w * 0.30, by - h * 0.52, bx + w * 0.62, by - h * 0.50, bx + w * 0.62, by - h * 0.30)
        path.cubicTo(bx + w * 0.82, by - h * 0.36, bx + w * 0.92, by - h * 0.10, bx + w * 0.80, by)
        path.closeSubpath()
        p.drawPath(path)
    elif shape == "firewall":                   # generic brick wall (no vendor logo)
        r = QRectF(c.x() - w * 0.36, c.y() - h * 0.30, w * 0.72, h * 0.60)
        p.drawRoundedRect(r, 2, 2)
        rows = 3
        for i in range(1, rows):
            y = r.top() + r.height() * i / rows
            p.drawLine(QPointF(r.left(), y), QPointF(r.right(), y))
        for i in range(rows):
            y0 = r.top() + r.height() * i / rows
            y1 = y0 + r.height() / rows
            xs = [0.5] if i != 1 else [0.25, 0.75]
            for fx in xs:
                x = r.left() + r.width() * fx
                p.drawLine(QPointF(x, y0), QPointF(x, y1))
    p.restore()


_STOPS_CACHE: dict = {}


def vendor_stops(vendor: str, dark: bool) -> tuple[QColor, QColor]:
    """Gradient stops of a vendor tile; the dark theme's are precomputed once per theme change
    (saturation -12 %, lightness -6 %), never converted per paint (S7)."""
    key = (vendor, dark, T.generation())
    hit = _STOPS_CACHE.get(key)
    if hit is None:
        c1, c2, _ = VENDOR_COLORS.get(vendor, VENDOR_COLORS["generic"])
        out = []
        for c in (QColor(c1), QColor(c2)):
            if dark:
                h, s, l, a = c.getHslF()
                c = QColor.fromHslF(max(0.0, h), max(0.0, s * 0.88), max(0.0, l * 0.94), a)
            out.append(c)
        hit = _STOPS_CACHE[key] = tuple(out)
    return hit


def tile_gradient(rect: QRectF, vendor: str, dark: bool | None = None) -> QLinearGradient:
    c1, c2 = vendor_stops(vendor, T.tokens().dark if dark is None else dark)
    g = QLinearGradient(rect.topLeft(), rect.bottomLeft())
    g.setColorAt(0, c1)
    g.setColorAt(1, c2)
    return g




def strip_text(dtype: DeviceType) -> str:
    return STRIP_TEXT.get(dtype.key, VENDOR_COLORS.get(dtype.vendor, VENDOR_COLORS["generic"])[2])


LED_COLORS = {"up": "#3DDC84", "blk": "#FFB020", "err": "#FF5A5F", "off": None}


def _rj45(p: QPainter, r: QRectF, body: QColor, led: str | None, lit_off: QColor) -> None:
    """One RJ45 socket (with its notch) and an LED dot above it."""
    p.setPen(QPen(QColor(255, 255, 255, 55), 0.6))
    p.setBrush(body)
    p.drawRoundedRect(r, r.width() * 0.12, r.width() * 0.12)
    p.setPen(Qt.NoPen)
    notch = QRectF(r.center().x() - r.width() * 0.22, r.bottom() - r.height() * 0.28, r.width() * 0.44,
                   r.height() * 0.28)
    p.setBrush(QColor(0, 0, 0, 90))
    p.drawRect(notch)
    d = max(1.3, r.width() * 0.30)
    c = QPointF(r.center().x(), r.top() - d * 0.95)
    if led:
        g = QColor(led)
        g.setAlpha(110)
        p.setBrush(g)
        p.drawEllipse(c, d * 0.95, d * 0.95)
        p.setBrush(QColor(led))
    else:
        p.setBrush(lit_off)
    p.drawEllipse(c, d * 0.55, d * 0.55)


def paint_switch_tile(p: QPainter, dtype: DeviceType, size: float, path: QPainterPath, *,
                      ports: list[str | None] | None = None, badge: str = "L2") -> None:
    """Managed switch: Cisco-blue chassis, a dark front panel with two rows of RJ45 ports (+ LEDs) and
    an 'L2' (or 'L3') badge.  ``ports``: LED colour per real port (None = dark), drawn left to right."""
    br = path.boundingRect()
    w, h = br.width(), br.height()
    g = QLinearGradient(br.topLeft(), br.bottomLeft())
    g.setColorAt(0, QColor("#2BB0E6"))
    g.setColorAt(0.5, QColor("#1489C0"))
    g.setColorAt(1, QColor("#0A5A80"))
    p.setPen(Qt.NoPen)
    p.setBrush(g)
    p.drawPath(path)
    p.save()
    p.setClipPath(path)
    sheen = QLinearGradient(br.topLeft(), QPointF(br.left(), br.top() + h * 0.45))
    sheen.setColorAt(0, QColor(255, 255, 255, 80))
    sheen.setColorAt(1, QColor(255, 255, 255, 0))
    p.setBrush(sheen)
    p.drawRect(br)
    p.restore()
    p.setBrush(Qt.NoBrush)
    p.setPen(QPen(QColor(255, 255, 255, 70), 1.0))
    p.drawPath(path)
    # top band: badge + model text
    band_h = h * 0.36
    bh = band_h * 0.62
    f = T.font(max(4.5, size * 0.105), True)
    fm = QFontMetricsF(f)
    bw = fm.horizontalAdvance(badge) + size * 0.09
    bx, by = br.left() + size * 0.09, br.top() + (band_h - bh) / 2 + size * 0.02
    badge_r = QRectF(bx, by, bw, bh)
    p.setPen(Qt.NoPen)
    p.setBrush(QColor("white") if badge == "L2" else QColor(T.ROOT_GOLD))
    p.drawRoundedRect(badge_r, bh * 0.28, bh * 0.28)
    p.setFont(f)
    p.setPen(QColor("#0A5A80") if badge == "L2" else QColor("#3D2E00"))
    p.drawText(badge_r, Qt.AlignCenter, badge)
    f2 = T.font(max(4.2, size * 0.088), True)
    f2.setLetterSpacing(f2.SpacingType.PercentageSpacing, 106)
    p.setFont(f2)
    p.setPen(QColor(255, 255, 255, 235))
    p.drawText(QRectF(badge_r.right() + size * 0.06, br.top() + size * 0.02, w * 0.6, band_h), Qt.AlignVCenter | Qt.AlignLeft,
               strip_text(dtype))
    # front panel with two rows of ports
    panel = QRectF(br.left() + size * 0.07, br.top() + band_h + size * 0.01, w - size * 0.14,
                   h - band_h - size * 0.08)
    p.setPen(QPen(QColor(255, 255, 255, 40), 0.8))
    p.setBrush(QColor("#08354D"))
    p.drawRoundedRect(panel, size * 0.04, size * 0.04)
    cols, rows = 6, 2
    pad_x = panel.width() * 0.06
    cell_w = (panel.width() - 2 * pad_x) / cols
    sock_w = cell_w * 0.66
    sock_h = min(panel.height() * 0.22, sock_w * 0.82)
    leds = list(ports or [])
    off = QColor(255, 255, 255, 38)
    for i in range(cols * rows):
        row, col = divmod(i, cols)
        cx = panel.left() + pad_x + cell_w * (col + 0.5)
        cy = panel.top() + panel.height() * (0.40 if row == 0 else 0.82)
        led = LED_COLORS.get(leds[i]) if i < len(leds) and leds[i] else None
        body = QColor("#2A3A48") if i < len(leds) or not leds else QColor("#1A2833")
        _rj45(p, QRectF(cx - sock_w / 2, cy - sock_h / 2, sock_w, sock_h), body, led, off)


def paint_hub_tile(p: QPainter, dtype: DeviceType, size: float, path: QPainterPath) -> None:
    """Unmanaged switch: plain neutral grey box, one row of ports, no LEDs or badges - 'just a hub'."""
    br = path.boundingRect()
    w, h = br.width(), br.height()
    g = QLinearGradient(br.topLeft(), br.bottomLeft())
    g.setColorAt(0, QColor("#C3CAD3"))
    g.setColorAt(1, QColor("#8E99A6"))
    p.setPen(Qt.NoPen)
    p.setBrush(g)
    p.drawPath(path)
    p.setBrush(Qt.NoBrush)
    p.setPen(QPen(QColor("#6B7683"), 1.0))
    p.drawPath(path)
    p.setPen(QPen(QColor(255, 255, 255, 120), 1.0))
    p.drawLine(QPointF(br.left() + size * 0.10, br.top() + 1.2), QPointF(br.right() - size * 0.10, br.top() + 1.2))
    n = 8
    pad = w * 0.10
    cell = (w - 2 * pad) / n
    sw_, sh_ = cell * 0.68, h * 0.26
    y = br.top() + h * 0.52
    for i in range(n):
        cx = br.left() + pad + cell * (i + 0.5)
        r = QRectF(cx - sw_ / 2, y - sh_ / 2, sw_, sh_)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor("#4A5562"))
        p.drawRoundedRect(r, 1.0, 1.0)
        p.setBrush(QColor(0, 0, 0, 80))
        p.drawRect(QRectF(cx - sw_ * 0.2, r.bottom() - sh_ * 0.3, sw_ * 0.4, sh_ * 0.3))
    if size < 50:
        return
    f = T.font(max(4.0, size * 0.070), False)
    f.setLetterSpacing(f.SpacingType.PercentageSpacing, 112)
    p.setFont(f)
    p.setPen(QColor("#4A5562"))
    p.drawText(QRectF(br.left(), y + sh_ / 2, w, br.bottom() - (y + sh_ / 2)), Qt.AlignCenter, "unmanaged")




INNER_HIGHLIGHT = QColor(255, 255, 255, 41)        # S7: 1 px inner highlight at white 16 %


def paint_tile(p: QPainter, dtype: DeviceType, size: float, *, dim: bool = False,
               ports: list[str | None] | None = None, badge: str = "L2", down: bool = False) -> QPainterPath:
    """Vendor tile body (gradient, sheen, glyph, text strip, KVM ribbon) centred on (0, 0).

    ``dim`` (stopped / wiped): the body only is drawn at 45 % and desaturated - names and state text are
    painted by the caller at full contrast.  ``down`` (Internet cloud DOWN): grey N8->N9 body."""
    kind = tile_kind(dtype)
    path = tile_path(kind, size)
    br = path.boundingRect()
    p.save()
    if dim:
        p.setOpacity(p.opacity() * 0.45)
    if kind == "l2switch":
        paint_switch_tile(p, dtype, size, path, ports=ports, badge=badge)
    elif kind == "switch":
        paint_hub_tile(p, dtype, size, path)
    else:
        _paint_vendor_tile(p, dtype, size, kind, path, br, down=down)
    if dim:                                     # desaturate: a neutral N9 wash over the faded body
        p.setOpacity(1.0)                       # (Qt has no portable 'saturation' blend mode)
        p.setPen(Qt.NoPen)
        p.setBrush(T.alpha(T.tokens().N(9), 0.38))
        p.drawPath(path)
    p.restore()
    return path


INTERNET_TOP, INTERNET_BOTTOM = "#5FD3F5", "#1381B8"     # sky cyan: never ROUTE_BLUE (that means "default route")
CLOUD_HEALTH_COLORS = {"up": T.LINK_UP, "degraded": T.AMBER, "down": T.DOWN_RED, "stopped": T.DOWN_RED,
                       "not_installed": T.DOWN_RED, "unknown": T.NEUTRAL}
CLOUD_DOWN_STATES = ("down", "stopped", "not_installed")


def draw_globe(p: QPainter, c: QPointF, r: float, color: QColor = QColor("white"), width: float = 1.6) -> None:
    """Line globe (outline, equator, two parallels, two meridians) - the Internet-cloud glyph."""
    p.save()
    p.setPen(QPen(color, width, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
    p.setBrush(Qt.NoBrush)
    p.drawEllipse(c, r, r)
    p.drawEllipse(c, r * 0.45, r)
    p.drawLine(QPointF(c.x(), c.y() - r), QPointF(c.x(), c.y() + r))
    p.drawLine(QPointF(c.x() - r, c.y()), QPointF(c.x() + r, c.y()))
    for f in (-0.55, 0.55):
        hw = r * math.sqrt(1 - f * f)
        p.drawLine(QPointF(c.x() - hw, c.y() + f * r), QPointF(c.x() + hw, c.y() + f * r))
    p.restore()


def _paint_internet_tile(p: QPainter, size: float, path: QPainterPath, br: QRectF, down: bool = False) -> None:
    """The LabDesk Internet cloud: a sky-cyan cloud with a white globe; grey (N8 -> N9) while the cloud
    service is DOWN (NAT / System-Bridge connectors stay grey and plain)."""
    tk = T.tokens()
    g = QLinearGradient(br.topLeft(), br.bottomLeft())
    if down:
        g.setColorAt(0, tk.N(8))
        g.setColorAt(1, tk.N(9))
    else:
        g.setColorAt(0, QColor(INTERNET_TOP))
        g.setColorAt(1, QColor(INTERNET_BOTTOM))
    p.setPen(Qt.NoPen)
    p.setBrush(QBrush(g))
    p.drawPath(path)
    p.save()
    p.setClipPath(path)
    sheen = QLinearGradient(br.topLeft(), QPointF(br.left(), br.top() + br.height() * 0.55))
    sheen.setColorAt(0, QColor(255, 255, 255, 70))
    sheen.setColorAt(1, QColor(255, 255, 255, 0))
    p.setBrush(sheen)
    p.drawRect(br)
    p.restore()
    p.setBrush(Qt.NoBrush)
    p.setPen(QPen(QColor(255, 255, 255, 90), 1.0))
    p.drawPath(path)
    gr = size * 0.17
    draw_globe(p, QPointF(br.center().x() + size * 0.02, br.center().y() + br.height() * 0.12), gr,
               QColor(255, 255, 255, 235), max(1.0, size * 0.022))


def _paint_vendor_tile(p: QPainter, dtype: DeviceType, size: float, kind: str, path: QPainterPath,
                       br: QRectF, down: bool = False) -> None:
    tk = T.tokens()
    if dtype.key == "ext_cloud":
        _paint_internet_tile(p, size, path, br, down)
        return
    p.setPen(Qt.NoPen)
    if kind == "cloud":
        g = QLinearGradient(br.topLeft(), br.bottomLeft())
        if tk.dark:
            g.setColorAt(0, tk.N(7))
            g.setColorAt(1, tk.N(6))
        else:
            g.setColorAt(0, QColor("#FFFFFF"))
            g.setColorAt(1, tk.N(4))
        p.setBrush(QBrush(g))
    else:
        p.setBrush(QBrush(tile_gradient(br, dtype.vendor, tk.dark)))
    p.drawPath(path)
    # top sheen
    if kind != "cloud":
        p.save()
        p.setClipPath(path)
        sheen = QLinearGradient(br.topLeft(), QPointF(br.left(), br.top() + br.height() * 0.55))
        sheen.setColorAt(0, QColor(255, 255, 255, 56))
        sheen.setColorAt(1, QColor(255, 255, 255, 0))
        p.setBrush(sheen)
        p.drawRect(br)
        p.restore()
    # 1 px inner highlight (S7)
    p.setBrush(Qt.NoBrush)
    if kind == "cloud":
        p.setPen(QPen(tk.N(8) if not tk.dark else tk.N(8), 1.0))
        p.drawPath(path)
    else:
        p.save()
        p.setClipPath(path)
        p.setPen(QPen(INNER_HIGHLIGHT, 2.0))
        p.drawPath(path)
        p.restore()
    if kind != "cloud":
        gs = size * (0.46 if kind != "fortinet" else 0.42)
        gy = -size * 0.09
        draw_glyph(p, QRectF(-gs / 2, gy - gs / 2, gs, gs), dtype.shape)
        text = strip_text(dtype)
        if text and size >= 30:
            f = _tile_strip_font(size)
            p.setFont(f)
            p.setPen(QColor(255, 255, 255, 225))
            p.drawText(QRectF(-size / 2, size * 0.19, size, size * 0.18), Qt.AlignCenter, text)
    if dtype.backend == "kvm":              # V8: 14 px 'KVM' ribbon across the top-left corner
        p.save()
        p.setClipPath(path)
        rib = size * 0.195
        p.translate(br.left() + size * 0.17, br.top() + size * 0.17)
        p.rotate(-45)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(20, 24, 31, 200))
        p.drawRect(QRectF(-size * 0.5, -rib / 2, size, rib))
        f = T.font(max(5.0, size * 0.085), True)
        p.setFont(f)
        p.setPen(QColor("white"))
        p.drawText(QRectF(-size * 0.5, -rib / 2, size, rib), Qt.AlignCenter, "KVM")
        p.restore()


def _tile_strip_font(size: float):
    f = T.font(max(5.0, size * 0.092), True)
    return f


def device_pixmap(dtype: DeviceType, size: int = 44, dpr: float = 2.0) -> QPixmap:
    pm = QPixmap(int(size * dpr), int(size * dpr))
    pm.setDevicePixelRatio(dpr)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.translate(size / 2, size / 2)
    kind = tile_kind(dtype)
    w, h = tile_size(kind, size - 2)
    scale = (size - 2) / max(w, h)
    demo = ["up", "up", "up", "blk", "up", None, "up", "up"] if kind == "l2switch" else None
    paint_tile(p, dtype, (size - 2) * scale, ports=demo)
    p.end()
    return pm


# ============================================================================ render context
@dataclass
class RenderCtx:
    """Per-scene data the items read when painting (set by the main window on every poll)."""
    layers: dict = field(default_factory=lambda: {"chips": True, "p2p": True, "default": True, "fhrp": True,
                                                  "ospf": False, "bgp": False, "traffic": False, "l2": True,
                                                  "vlan": False, "subnets": True})
    tokens: T.Tokens = field(default_factory=T.tokens)
    labels: dict = field(default_factory=dict)              # node key -> label
    path_marks: dict = field(default_factory=dict)          # link key -> (icon, color, tooltip)
    egress_caption: dict = field(default_factory=dict)      # node key -> (text, verified: bool, tooltip)
    switch_chips: dict = field(default_factory=dict)        # node key -> [(ChipSpec, tooltip)]
    fhrp_tint: dict = field(default_factory=dict)           # link key -> hue
    flash: dict = field(default_factory=dict)               # item key -> (t0, hue)
    subnet_pulse: dict = field(default_factory=dict)        # link key -> (t0, hue): subnet label just changed
    now: float = 0.0
    pulse: float = 0.0                                      # 0..1 LED pulse phase (legacy)
    spin: float = 0.0                                       # degrees (legacy)
    telemetry_on: bool = False
    vlan_hl: int | None = None                              # VLAN colouring layer: highlighted VLAN
    vlan_hues: dict = field(default_factory=dict)           # VLAN id -> hue
    vlan_names: dict = field(default_factory=dict)          # VLAN id -> name
    cloud_health: object = None                             # cloud.CloudHealth of the Internet cloud (or None)
    hover_card: bool = False                                # the canvas shows the live hover card for devices


def _lod(p: QPainter, opt: QStyleOptionGraphicsItem | None) -> float:
    try:
        return QStyleOptionGraphicsItem.levelOfDetailFromTransform(p.worldTransform())
    except Exception:  # pragma: no cover
        return 1.0


def text_scale(lod: float) -> float:
    """Counter-scale for captions/chips when zoomed out, so they stay legible at fit zoom (map-label
    style): 1.0 at zoom >= 0.95, up to 1.4x at zoom 0.68 and below."""
    return max(1.0, min(1.4, 0.95 / max(lod, 0.05)))


def glyph_scale(lod: float) -> float:
    """Tier-0 shapes (end glyphs, badges) keep a usable on-screen size when zoomed far out."""
    return max(1.0, min(2.2, 0.9 / max(lod, 0.05)))


def text_visible(pt: float, k: float, lod: float) -> bool:
    """S4/S8: text whose on-screen size would fall below MIN_TEXT_PX is not drawn at all."""
    return pt * 96.0 / 72.0 * k * lod >= T.MIN_TEXT_PX


def dim_factor(*factors: float) -> float:
    """S7 composite dimming: the minimum of the factors, floored at 40 % (never multiplied)."""
    return max(0.40, min((1.0,) + tuple(factors)))


class _HitMixin:
    """Hit rects recorded in item coordinates whatever the painter's current (scaled) transform."""

    def _begin_hits(self, p: QPainter) -> None:
        self._hits = []
        self._base_inv = p.worldTransform().inverted()[0]

    def _hit(self, p: QPainter, r: QRectF, tip: str) -> None:
        m = p.worldTransform() * self._base_inv
        self._hits.append((m.mapRect(r), tip))


def _scaled(p: QPainter, anchor: QPointF, k: float) -> None:
    p.save()
    p.translate(anchor)
    p.scale(k, k)
    p.translate(-anchor)


def _scale_rect(r: QRectF, anchor: QPointF, k: float) -> QRectF:
    """The item-coordinate rect of ``r`` drawn under _scaled(anchor, k)."""
    return QRectF(anchor.x() + (r.x() - anchor.x()) * k, anchor.y() + (r.y() - anchor.y()) * k,
                  r.width() * k, r.height() * k)


COLLECTOR_CHIP = {      # V10 static node collector chips: text, icon, colour
    "learning": ("learning…", "dots", T.NEUTRAL),
    "paused_console": ("paused: console in use", "clock", T.NEUTRAL),
    "console_busy": ("console busy", "bang", T.AMBER),
    "needs_credentials": ("needs login / enable secret", "bang", T.AMBER),
    "error": ("probe error", "bang", T.DOWN_RED),
    "unverified": ("beta", "question", "#8B5CF6"),
    "limited": ("limited", "bang", T.AMBER),
    "no_mgmt": ("no mgmt", "bang", T.AMBER),
}


def _ev(v):
    return getattr(v, "value", v)


# ============================================================================ selection geometry (S7)
RING_OFFSET = 3.0          # selection ring sits 3 px outside the tile ...
RING_STROKE = 2.0          # ... 2 px wide: its outer edge is 5 px from the tile
LABEL_GAP = RING_OFFSET + RING_STROKE + 3.0      # = 8: tile edge -> name (below / beside)
BADGE = 12.0               # node badge diameter at tier 1+
CLOUD_BADGE = 16.0
OFF_STATES = ("STOPPED", "stopped", "DEFINED_ON_CORE", "defined", "missing")


def ring_path(kind: str, size: float = TILE) -> QPainterPath:
    """Centre line of the selection ring: the tile outline grown by offset + half the stroke."""
    w, h = tile_size(kind, size)
    grow = RING_OFFSET + RING_STROKE / 2
    path = tile_path(kind, size)
    br = path.boundingRect()
    sx, sy = (br.width() + 2 * grow) / br.width(), (br.height() + 2 * grow) / br.height()
    from PySide6.QtGui import QTransform
    return QTransform().scale(sx, sy).map(path)


def ring_rect(kind: str, size: float = TILE) -> QRectF:
    """Outer bounding rect of the selection ring (stroke included)."""
    r = ring_path(kind, size).boundingRect()
    hw = RING_STROKE / 2
    return r.adjusted(-hw, -hw, hw, hw)


def reduce_motion() -> bool:
    try:
        from ..settings import app_settings, as_bool
        return as_bool(app_settings().value("ui/reduce_motion", False))
    except Exception:  # noqa: BLE001
        return False


class BadgeSpinner(QGraphicsObject):
    """S2: the booting / probing spinner arc on its own small child item, so only 20x20 px repaint.

    The animation runs only while the item is visible, the zoom is at tier 1+, the window is not
    minimised and Reduce motion is off; otherwise the arc is drawn static."""

    def __init__(self, parent: "NodeItem"):
        super().__init__(parent)
        self.mode = ""                   # '' | 'booting' (arc inside the badge) | 'probing' (ring around it)
        self.angle = 0.0
        self.setAcceptedMouseButtons(Qt.NoButton)
        self.setZValue(2)
        from PySide6.QtCore import QEasingCurve, QVariantAnimation
        self.anim = QVariantAnimation(self)
        self.anim.setStartValue(0.0)
        self.anim.setEndValue(360.0)
        self.anim.setDuration(1100)
        self.anim.setLoopCount(-1)
        self.anim.setEasingCurve(QEasingCurve.Linear)
        self.anim.valueChanged.connect(self._step)
        self.hide()

    def boundingRect(self) -> QRectF:
        return QRectF(-11, -11, 22, 22)

    def _step(self, v):
        if not self._may_run():
            self.anim.stop()
            self.angle = 0.0
        else:
            self.angle = float(v)
        self.update()

    def _may_run(self) -> bool:
        if not self.isVisible() or reduce_motion():
            return False
        sc = self.scene()
        views = sc.views() if sc is not None else []
        if not views:
            return False
        v = views[0]
        if v.window().isMinimized() or v.transform().m11() < T.LOD_TIER1:
            return False
        return True

    def sync(self, mode: str) -> None:
        self.mode = mode
        self.setVisible(bool(mode))
        if mode and self._may_run():
            if self.anim.state() != self.anim.State.Running:
                self.anim.start()
        else:
            self.anim.stop()
            self.angle = 0.0
        self.update()

    def paint(self, p: QPainter, opt, widget=None):
        if not self.mode:
            return
        p.setRenderHint(QPainter.Antialiasing)
        tk = T.tokens()
        if self.mode == "booting":
            r = BADGE / 2 - 2.6
            p.setPen(QPen(QColor("white"), 1.7, Qt.SolidLine, Qt.RoundCap))
        else:
            r = BADGE / 2 + 3.2
            p.setPen(QPen(tk.N(11), 1.6, Qt.SolidLine, Qt.RoundCap))
        p.setBrush(Qt.NoBrush)
        p.drawArc(QRectF(-r, -r, 2 * r, 2 * r), int((90 - self.angle) * 16), -260 * 16 if self.mode == "booting"
                  else -110 * 16)


# ============================================================================ node item
class NodeItem(_HitMixin, QGraphicsObject):
    """A device tile: vendor outline, status badge, label, chip column, FHRP badges, collector chip."""

    SIZE = TILE
    moved = Signal(str, float, float)            # key, x, y  (emitted on mouse release)
    clicked = Signal(str)                        # released without moving

    def __init__(self, key: str, label: str, dtype: DeviceType):
        super().__init__()
        self.key = key
        self.label = label
        self.dtype = dtype
        self.state = "unknown"
        self.subtitle = dtype.title
        self.links: list["LinkItem"] = []
        self.nv = None                         # controller.NodeView
        self._press_pos = None
        self._hits: list[tuple[QRectF, str]] = []
        self._hover = False
        self.link_highlight = False
        self.hidden_labels: set = set()        # S8 LabelLayout: ids suppressed by the scene pass
        self.focus_dim = 1.0                   # S8 layer focus factor (1 = not dimmed)
        self.setFlags(QGraphicsItem.ItemIsMovable | QGraphicsItem.ItemIsSelectable
                      | QGraphicsItem.ItemSendsGeometryChanges | QGraphicsItem.ItemIsFocusable)
        self.setAcceptHoverEvents(True)
        self.setZValue(10)
        self.setCursor(Qt.OpenHandCursor)
        self.spinner = BadgeSpinner(self)
        self._update_tooltip()

    # ------------------------------------------------------------------ model
    @property
    def kind(self) -> str:
        return tile_kind(self.dtype)

    @property
    def is_internet(self) -> bool:
        return self.dtype.key == "ext_cloud"

    def refresh_tooltip(self) -> None:
        self._update_tooltip()

    @property
    def has_card(self) -> bool:
        """Devices get the live hover card; clouds, unmanaged switches and connectors keep their tooltip."""
        return self.kind != "cloud" and self.dtype.node_definition not in ("unmanaged_switch", "external_connector")

    def card_hit(self, pos: QPointF, lod: float = 1.0) -> bool:
        """``pos`` (item coordinates) is on the tile or its name - not on a chip / badge with its own tooltip."""
        if any(r.adjusted(-2, -2, 2, 2).contains(pos) for r, _ in self._hits):
            return False
        if self.tile_rect().adjusted(-3, -3, 3, 3).contains(pos):
            return True
        try:
            return self.name_rect(lod).adjusted(-2, -2, 2, 2).contains(pos)
        except Exception:  # noqa: BLE001
            return False

    @property
    def outline(self) -> QPainterPath:
        return tile_path(self.kind, self.SIZE)

    def ctx(self) -> RenderCtx:
        sc = self.scene()
        return getattr(sc, "ctx", None) or RenderCtx()

    def _update_tooltip(self):
        st = STATE_TEXT.get(self.state, self.state)
        lines = [f"<b>{self.label}</b> · {self.dtype.title}", f"state: {st}"]
        nt = getattr(self.nv, "tele", None)
        if nt is not None:
            if nt.summary:
                lines.append(nt.summary)
            if nt.rid:
                lines.append(f"router-id {nt.rid}" + (f" · loopback {nt.loopback}" if nt.loopback else ""))
            if nt.collector_detail:
                lines.append(f"<i>collector: {nt.collector_detail}</i>")
        if self.dtype.backend == "kvm":
            lines.append("host KVM (virsh) · collector unverified (beta)")
        if self.is_internet:
            h = self.ctx().cloud_health
            if h is not None:
                lines[1:] = [html.escape(t) if i else f"<b>{html.escape(t)}</b>"
                             for i, t in enumerate(h.tooltip_lines())] + lines[1:]
            else:
                lines.insert(1, "simulated Internet: answers ping to 8.8.8.8 and 1.1.1.1 (checking…)")
            lines.append("<i>right-click a linked router → Auto-configure for Internet cloud</i>")
            self.setToolTip("<br>".join(lines))
            return
        if self.has_card and self.ctx().hover_card:
            self.setToolTip("")             # the hover card replaces the tile tooltip (chips keep theirs)
            return
        lines.append("<i>double-click / Enter: console</i>")
        self.setToolTip("<br>".join(lines))

    def set_state(self, state: str):
        if state != self.state:
            self.state = state
            self._update_tooltip()
            self.sync_spinner()
            self.update()

    def set_label(self, label: str):
        if label != self.label:
            self.prepareGeometryChange()
            self.label = label
            self._update_tooltip()
            self.update()

    def set_view(self, nv) -> None:
        self.nv = nv
        self._update_tooltip()
        self.sync_spinner()
        self.update()

    @property
    def collector_key(self) -> str | None:
        cs = getattr(self.nv, "collector_state", None)
        return _ev(cs) if cs is not None else None

    @property
    def animating(self) -> bool:
        """The whole tile never animates any more: the spinner is a child item with its own timer."""
        return False

    @property
    def booting(self) -> bool:
        return self.state in T.PULSING_STATES

    @property
    def probing(self) -> bool:
        nt = getattr(self.nv, "tele", None)
        return bool(nt is not None and nt.probing)

    def sync_spinner(self) -> None:
        if self.kind == "cloud":
            self.spinner.sync("")
            return
        mode = "booting" if self.booting else ("probing" if self.probing else "")
        self.spinner.setPos(self.badge_center())
        self.spinner.sync(mode)

    @property
    def cloud_down(self) -> bool:
        if not self.is_internet:
            return False
        h = self.ctx().cloud_health
        return h is not None and h.state in CLOUD_DOWN_STATES

    # ------------------------------------------------------------------ geometry
    def boundingRect(self) -> QRectF:
        s = self.SIZE
        return QRectF(-230, -s / 2 - 46, 460, s + 130)

    def tile_rect(self) -> QRectF:
        return self.outline.boundingRect()

    def badge_center(self) -> QPointF:
        br = self.tile_rect()
        if self.is_internet:
            return QPointF(br.right() - br.width() * 0.16, br.top() + br.height() * 0.20)
        if self.kind == "juniper":
            return QPointF(br.right() * 0.52, br.top() + 2.0)
        return QPointF(br.right() - 2.0, br.top() + 2.0)

    def obstacle_rects(self, lod: float | None = None) -> list[QRectF]:
        """Scene rects of the tile, the label and every chip / badge (the stack placement avoids them).
        With ``lod``: from the shared geometry (name + chip column) instead of the last paint."""
        if lod is not None:
            out = [self.mapRectToScene(self.tile_rect()), self.mapRectToScene(self.name_rect(lod))]
            if lod >= T.LOD_TIER1:
                for _lid, _k, _p, _t, _prio, r, anchor, k in self.chip_layout(lod):
                    out.append(self.mapRectToScene(_scale_rect(r, anchor, k)))
            out += [self.mapRectToScene(hr) for hr, _ in self._hits]
            return out
        out = [self.mapRectToScene(self.outline.boundingRect())]
        lr = getattr(self, "_label_rect", None)
        if lr is not None:
            out.append(self.mapRectToScene(lr))
        out += [self.mapRectToScene(hr) for hr, _ in self._hits]
        return out

    def content_rect(self) -> QRectF:
        """Scene rect of what is painted (tile, label, chips, badges) - for 'fit view'."""
        s = self.SIZE
        r = QRectF(-s / 2 - 10, -s / 2 - 10, s + 20, s + 20)
        lr = getattr(self, "_label_rect", None)
        if lr is not None:
            r = r.united(lr)
        for hr, _ in self._hits:
            r = r.united(hr)
        return self.mapRectToScene(r)

    def shape(self):
        s = self.SIZE
        path = QPainterPath(self.outline)
        path.addRect(QRectF(-s / 2 - 20, s / 2 + 2, s + 40, 22))
        for r, _ in self._hits:                  # chips / badges are hoverable (tooltips)
            path.addRect(r)
        return path

    # ------------------------------------------------------------------ label geometry (shared with LabelLayout)
    def label_geom(self, lod: float) -> tuple:
        """(anchor, align, x0, name_rect, state_rect) in unscaled coordinates about ``anchor``."""
        br = self.tile_rect()
        lside = self.label_side()
        f = T.type("label")
        h = T.fm(f).height() + 1
        if lside == "below":
            anchor, align, x0 = QPointF(0, br.bottom()), 0, 0.0
            y = br.bottom() + LABEL_GAP
            name = QRectF(-110, y, 220, h)
        else:
            sgn = 1 if lside == "right" else -1
            anchor = QPointF(br.right() + LABEL_GAP if sgn > 0 else br.left() - LABEL_GAP, br.center().y())
            align, x0 = sgn, anchor.x()
            y = br.center().y() - h
            name = QRectF(x0, y, 220, h) if sgn > 0 else QRectF(x0 - 220, y, 220, h)
        sh = T.fm(T.type("small")).height() + 1
        state = QRectF(name.left(), name.bottom() + 1, name.width(), sh)
        return anchor, align, x0, name, state

    def name_rect(self, lod: float) -> QRectF:
        """Item rect of the painted name (tight), for the ring/label test and the LabelLayout pass."""
        anchor, align, x0, name, _ = self.label_geom(lod)
        w = T.fm(T.type("label")).horizontalAdvance(self.label) + 2
        if align == 0:
            r = QRectF(-w / 2, name.top(), w, name.height())
        elif align > 0:
            r = QRectF(x0, name.top(), w, name.height())
        else:
            r = QRectF(x0 - w, name.top(), w, name.height())
        return _scale_rect(r, anchor, text_scale(lod))

    def chip_side(self) -> int:
        """S8 chip column side: right of the tile, unless the label sits there or links crowd that side."""
        lside = self.label_side()
        if lside == "right":
            return -1
        if lside == "left":
            return 1
        return 1 if self._side_load(1) <= self._side_load(-1) + 0.2 else -1

    def _side_load(self, sgn: int) -> float:
        me = self.pos()
        load = 0.0
        for li in self.links:
            other = li.b if li.a is self else li.a
            d = other.pos() - me
            ln = math.hypot(d.x(), d.y()) or 1.0
            if d.x() * sgn / ln > 0.35:
                load += abs(d.x() / ln)
        return load

    def chip_specs(self, fhrp: bool = False) -> list:
        """[(kind, payload, tooltip, prio)] of the chip column, top to bottom (prio 0 = alarm).
        ``fhrp``: append the HSRP/VRRP role badges (secondary chips, tier 2)."""
        ctx = self.ctx()
        out = []
        kind = self.kind
        paused = self.collector_key == "paused_console"
        if self.is_internet:
            h = ctx.cloud_health
            st = h.state if h is not None else "unknown"
            text = h.answers_text if h is not None else "8.8.8.8 · 1.1.1.1"
            tip = "<br>".join(html.escape(t) for t in (h.tooltip_lines() if h is not None else ["checking the cloud…"]))
            out.append(("inet", (text, st), tip, 1))
            if h is not None and st not in ("up", "unknown"):
                out.append(("cloudstate", ("cloud DOWN" if st != "degraded" else "cloud degraded", st), tip, 0))
            return out
        if kind == "cloud" and self.key in ctx.egress_caption:
            text, verified, tip = ctx.egress_caption[self.key]
            out.append(("egress", (text, verified), tip, 1))
            return out
        specs = []
        if self.key in ctx.switch_chips or (kind == "l2switch" and self.nv is not None and self.nv.chips):
            specs = [(sp, tip) for sp, tip, layer in ctx.switch_chips.get(self.key, []) if ctx.layers.get(layer, True)]
            if kind == "l2switch" and self.nv is not None and ctx.layers.get("l2", True):
                own = [(T.chip_spec_from(c), c.tooltip + (f" (updated {T.fmt_age(c.age_s)})"
                                                           if c.age_s is not None else ""))
                       for c in self.nv.chips if c.kind != "ROOT"]
                if paused:
                    for sp, _ in own:
                        sp.faded = True
                specs = own + specs
        elif self.nv is not None and self.nv.chips and ctx.layers.get("chips", True):
            specs = [(T.chip_spec_from(c), c.tooltip + (f" (updated {T.fmt_age(c.age_s)})"
                                                         if c.age_s is not None else "")) for c in self.nv.chips]
            if paused:
                for sp, _ in specs:
                    sp.faded = True
        show = specs[:3]
        for sp, tip in show:
            prio = 0 if (sp.icon == "bang" and QColor(sp.icon_color).name().upper() == T.DOWN_RED.upper()) else 1
            out.append(("chip", sp, tip, prio))
        if len(specs) > 3:
            more = T.ChipSpec(f"+{len(specs) - 3}", T.NEUTRAL, None, outline=True)
            out.append(("chip", more, "<br>".join(t for _, t in specs[3:]), 1))
        if fhrp and self.nv is not None and self.nv.fhrp and ctx.layers.get("fhrp", True):
            for b in self.nv.fhrp[:3]:
                out.append(("fhrp", b, b.tooltip + (" (stale)" if b.stale else ""), 1))
        return out

    def chip_layout(self, lod: float, reveal: bool = False) -> list:
        """[(id, kind, payload, tip, prio, rect_unscaled, anchor, k)] - the S8 chip column: chips in a
        column at the tile edge + 8 px with an 18 px pitch (left-aligned on the right, right-aligned on the
        left), vertically centred on the tile."""
        specs = self.chip_specs(fhrp=lod >= T.LOD_TIER2 or reveal)
        if not specs:
            return []
        br = self.tile_rect()
        side = self.chip_side()
        k = text_scale(lod)
        ax = br.right() + 8 if side > 0 else br.left() - 8
        anchor = QPointF(ax, br.center().y())
        n = len(specs)
        y = br.center().y() - (n * T.CHIP_PITCH - (T.CHIP_PITCH - T.CHIP_H)) / 2
        f = T.chip_font()
        fm_ = T.fm(f)
        out = []
        for i, (kind, payload, tip, prio) in enumerate(specs):
            if kind == "chip":
                w = T.chip_width(payload, fm_)
            elif kind == "inet":
                w = T.pill_width(payload[0], bold=True, icon=True)
            elif kind == "cloudstate":
                w = T.pill_width(payload[0], bold=True, icon=True)
            elif kind == "fhrp":
                w = self._fhrp_width(payload)
            else:
                w = T.pill_width(payload[0], bold=True, icon=payload[1])
            x = ax if side > 0 else ax - w
            out.append((("chip", i), kind, payload, tip, prio, QRectF(x, y, w, T.CHIP_H), anchor, k))
            y += T.CHIP_PITCH
        return out

    def label_entries(self, lod: float) -> list:
        """LabelLayout input: [(prio, id, item rect)] - prio 0 alarm, 1 protocol chip."""
        out = []
        for lid, kind, payload, tip, prio, r, anchor, k in self.chip_layout(lod):
            if prio > 0 and lod < T.LOD_TIER1:
                continue
            out.append((prio, lid, _scale_rect(r, anchor, k)))
        return out

    # ------------------------------------------------------------------ painting
    def paint(self, p: QPainter, opt: QStyleOptionGraphicsItem, widget=None):
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.TextAntialiasing)
        ctx = self.ctx()
        tk = ctx.tokens
        lod = _lod(p, opt)
        s = self.SIZE
        kind = self.kind
        self._begin_hits(p)
        k = text_scale(lod)
        gk = glyph_scale(lod)
        tier1 = lod >= T.LOD_TIER1
        sel = self.isSelected()
        tier2 = lod >= T.LOD_TIER2 or self._hover or sel
        nt = getattr(self.nv, "tele", None)
        off = self.state in OFF_STATES
        cdown = self.cloud_down
        reveal = self._hover or sel
        # flash (V10: 800 ms after a transition)
        fl = ctx.flash.get(self.key)
        if fl and ctx.now - fl[0] < 0.8:
            a = 1.0 - (ctx.now - fl[0]) / 0.8
            p.setPen(Qt.NoPen)
            p.setBrush(T.alpha(fl[1], 0.30 * a))
            p.drawPath(tile_path(kind, s + 26))
        body_dim = dim_factor(0.45 if off else 1.0, self.focus_dim)
        # elevation: e1 in light, 1 px N7 outline in dark (S7)
        if not tk.dark:
            p.setPen(Qt.NoPen)
            p.setBrush(T.alpha(QColor(16, 24, 40), 0.10 * body_dim))
            p.drawPath(tile_path(kind, s + 1).translated(0, 2.0))
            p.setBrush(T.alpha(QColor(16, 24, 40), 0.05 * body_dim))
            p.drawPath(tile_path(kind, s + 4).translated(0, 3.5))
        # selection ring / link-target halo / hover ring
        if sel:
            p.setBrush(Qt.NoBrush)
            p.setPen(QPen(tk.accent_ring, RING_STROKE))
            p.drawPath(ring_path(kind, s))
        elif self.link_highlight:
            p.setBrush(Qt.NoBrush)
            p.setPen(QPen(tk.accent_ring, RING_STROKE, Qt.DashLine))
            p.drawPath(ring_path(kind, s))
        elif self._hover:
            p.setBrush(Qt.NoBrush)
            p.setPen(QPen(tk.N(8), 1.0))
            p.drawPath(ring_path(kind, s))
        sw = self.switch_view
        p.save()
        if body_dim < 1.0 and not off:
            p.setOpacity(p.opacity() * body_dim)
        paint_tile(p, self.dtype, s, dim=off, ports=self.port_leds() if not off else None,
                   badge="L3" if sw is not None and sw.l3 else "L2", down=cdown)
        if tk.dark and kind not in ("cloud",):
            p.setBrush(Qt.NoBrush)
            p.setPen(QPen(tk.N(7), 1.0))
            p.drawPath(tile_path(kind, s + 1))
        p.restore()
        br = self.tile_rect()
        # ---- tier 0: node badge (12 px at tier 1+, counter-scaled below) / cloud-down badge (16 px, any zoom)
        self._paint_badge(p, ctx, tk, lod, gk, br, nt, cdown)
        lside = self.label_side()
        # ---- tier 2: FHRP role badges on the side opposite the chip column (secondary chips)
        # ---- tier 1: collector chip above the tile (V10)
        ck = self.collector_key
        top_used = 0.0
        if ck in COLLECTOR_CHIP and tier1:
            top_used = 18.0
            text, icon, col = COLLECTOR_CHIP[ck]
            _scaled(p, QPointF(0, br.top()), k)
            r = T.draw_pill(p, QPointF(0, br.top() - 12), text, tk, border=T.alpha(T.resolve(col, tk), 0.8),
                            text_color=tk.muted, icon=icon, icon_color=col, height=16)
            tip = (nt.collector_detail if nt is not None and nt.collector_detail else text)
            if ck == "paused_console":
                tip += "<br>Right-click → <b>Probe now</b> (output appears in your console)"
            self._hit(p, r, tip)
            p.restore()
        # STP root bridge: a gold crown badge above the switch ('ROOT v10,20')
        root = self.root_chip()
        if root is not None and ctx.layers.get("l2", True) and tier1:
            _scaled(p, QPointF(0, br.top()), k)
            stale = root.stale or root.faded
            p.save()
            if stale:
                p.setOpacity(0.45)
            rw = T.pill_width(root.text, bold=True, icon=True)
            r = T.draw_pill(p, QPointF(self.root_dx(br, rw), br.top() - 13 - top_used), root.text, tk,
                            border=QColor("#8A6400"), text_color=QColor("#3D2E00"), fill=QColor(T.ROOT_GOLD),
                            bold=True, icon="crown", icon_color=QColor("#3D2E00"), height=16)
            p.restore()
            self._hit(p, r, root.tooltip + (" (data is old)" if stale else ""))
            p.restore()
        # ---- name + state line (full contrast even when the body is dimmed)
        anchor, align, x0, name_r, state_r = self.label_geom(lod)
        self._chip_align, self._chip_x0 = align, x0
        halign = {0: Qt.AlignHCenter, 1: Qt.AlignLeft, -1: Qt.AlignRight}[align]
        lf = T.type("label")
        if text_visible(lf.pointSizeF(), k, lod):
            _scaled(p, anchor, k)
            if sel:                                           # accent_subtle pill behind the name
                w = T.fm(lf).horizontalAdvance(self.label) + 14
                cx = 0.0 if align == 0 else (x0 + w / 2 - 7 if align > 0 else x0 - w / 2 + 7)
                pr = QRectF(cx - w / 2, name_r.top() - 1, w, name_r.height() + 2)
                p.setPen(Qt.NoPen)
                p.setBrush(tk.accent_subtle)
                p.drawRoundedRect(pr, pr.height() / 2, pr.height() / 2)
            p.setFont(lf)
            p.setPen(tk.ink if not sel else (tk.accent_text if not tk.dark else tk.N(12)))
            p.drawText(name_r, halign | Qt.AlignVCenter, self.label)
            if self.state not in ("BOOTED", "running") and kind not in ("cloud",):
                sf = T.type("small")
                p.setFont(sf)
                word = STATE_TEXT.get(self.state, self.state)
                tw = T.fm(sf).horizontalAdvance(word)
                gsz = 7.0
                total = tw + gsz + 4
                gx = (-total / 2 if align == 0 else (x0 if align > 0 else x0 - total))
                gstate = T.STATE_GLYPH.get(self.state, "neutral")
                gstate = {"booted": "up", "disconnected": "down", "stopped": "neutral"}.get(gstate, gstate)
                if gstate == "booting":
                    gstate = "neutral"
                T.draw_status_glyph(p, QPointF(gx + gsz / 2, state_r.center().y()), gstate, gsz, tk=tk)
                p.setPen(tk.muted)
                p.drawText(QRectF(gx + gsz + 4, state_r.top(), tw + 2, state_r.height()),
                           Qt.AlignLeft | Qt.AlignVCenter, word)
            p.restore()
        self._label_rect = self.name_rect(lod)
        # ---- tier 1: chip column (alarms in it - cloud DOWN - at every tier)
        for lid, ckind, payload, tip, prio, r, anc, kk in self.chip_layout(lod, reveal):
            if not tier1 and prio > 0:
                continue
            if lid in self.hidden_labels and not reveal and prio > 0:
                continue
            _scaled(p, anc, kk)
            if ckind == "chip":
                rr = T.draw_chip(p, r.x(), r.y(), payload, tk)
            elif ckind == "fhrp":
                rr = self._paint_fhrp_badge(p, payload, r, tk)
            else:
                rr = self._paint_column_pill(p, ckind, payload, r, tk)
            self._hit(p, rr, tip)
            p.restore()

    def _paint_column_pill(self, p: QPainter, kind: str, payload, r: QRectF, tk) -> QRectF:
        c = r.center()
        if kind == "inet":
            text, st = payload
            col = T.resolve(CLOUD_HEALTH_COLORS.get(st, T.NEUTRAL), tk)
            glyph = {"up": "up", "unknown": "neutral", "degraded": "warn"}.get(st, "down")
            ink = QColor(INTERNET_BOTTOM) if not tk.dark else QColor(INTERNET_TOP)
            return T.draw_pill(p, c, text, tk, border=T.alpha(ink, 0.8), text_color=tk.ink, bold=True,
                               glyph=glyph, icon_color=col, height=T.CHIP_H)
        if kind == "cloudstate":
            text, st = payload
            state = "warn" if st == "degraded" else "down"
            return T.draw_pill(p, c, text, tk, border=T.status_fill(state, tk), text_color=T.status_text(state, tk),
                               fill=T.alpha(T.status_fill(state, tk), 0.12), bold=True, glyph="warn" if
                               state == "warn" else "down", height=T.CHIP_H)
        text, verified = payload
        if verified:
            return T.draw_pill(p, c, text, tk, border=T.status_fill("up", tk), text_color=T.status_text("up", tk),
                               bold=True, icon="check", icon_color=T.status_fill("up", tk), height=T.CHIP_H)
        return T.draw_pill(p, c, text, tk, border=QColor(T.ROUTE_BLUE), text_color=tk.ink, bold=True,
                           height=T.CHIP_H)

    def _paint_badge(self, p: QPainter, ctx, tk, lod: float, gk: float, br: QRectF, nt, cdown: bool) -> None:
        """S2 node badge: check (booted), X (disconnected / cloud down), spinner arc (booting), square
        (stopped / wiped).  12 px at tier 1+, counter-scaled below; the cloud-down X is 16 px at every lod."""
        c = self.badge_center()
        if self.is_internet:
            h = ctx.cloud_health
            st = h.state if h is not None else "unknown"
            tip = "<br>".join(html.escape(t) for t in (h.tooltip_lines() if h is not None else ["checking the cloud…"]))
            if st in CLOUD_DOWN_STATES:
                size, glyph, fill = CLOUD_BADGE, "x", T.status_fill("down", tk)
            elif st == "degraded":
                size, glyph, fill = BADGE, "warn", T.status_fill("warn", tk)
            elif st == "up":
                size, glyph, fill = BADGE, "booted", T.status_fill("up", tk)
            else:
                size, glyph, fill = BADGE, "booting", tk.N(9)
            kk = gk if size == BADGE else max(1.0, gk)
            _scaled(p, c, kk)
            self._draw_badge(p, c, size, glyph, fill, tk)
            self._hit(p, QRectF(c.x() - size / 2 - 2, c.y() - size / 2 - 2, size + 4, size + 4), tip)
            p.restore()
            return
        if self.kind == "cloud":
            return
        g = T.STATE_GLYPH.get(self.state, "disconnected")
        fill = {"booted": T.status_fill("up", tk), "disconnected": T.status_fill("down", tk)}.get(g, tk.N(9))
        if g == "stopped" and self.state in ("DEFINED_ON_CORE", "defined"):
            fill = tk.N(8)
        kk = 1.0 if lod >= T.LOD_TIER1 else gk
        _scaled(p, c, kk)
        self._draw_badge(p, c, BADGE, g, fill, tk)
        if nt is not None and nt.probing:
            self._hit(p, QRectF(c.x() - 10, c.y() - 10, 20, 20), "probing… (reading the console)")
        else:
            self._hit(p, QRectF(c.x() - 8, c.y() - 8, 16, 16), f"state: {STATE_TEXT.get(self.state, self.state)}")
        p.restore()

    @staticmethod
    def _draw_badge(p: QPainter, c: QPointF, size: float, glyph: str, fill: QColor, tk) -> None:
        p.save()
        p.setPen(QPen(tk.base, 2.0))                 # separator ring in the canvas colour
        p.setBrush(fill)
        p.drawEllipse(c, size / 2, size / 2)
        inner = {"booted": 0.62, "x": 0.56, "disconnected": 0.56, "stopped": 0.40, "warn": 0.55}.get(glyph)
        if glyph == "booting":
            pass                                     # the arc is the BadgeSpinner child (static when idle)
        elif inner:
            path = T.status_glyph(glyph if glyph != "stopped" else "neutral", size * inner)
            p.translate(c)
            p.setPen(Qt.NoPen)
            p.setBrush(QColor("white") if glyph != "warn" else QColor("#3D2E00"))
            p.drawPath(path)
        p.restore()

    @property
    def switch_view(self):
        nt = getattr(self.nv, "tele", None)
        return getattr(nt, "switch", None) if nt is not None else None

    def root_dx(self, br: QRectF, w: float) -> float:
        """x of the ROOT badge: over the tile, shifted away from links that leave upwards."""
        me = self.pos()
        up = 0.0
        for li in self.links:
            other = li.b if li.a is self else li.a
            d = other.pos() - me
            ln = math.hypot(d.x(), d.y()) or 1.0
            if d.y() / ln < -0.3:
                up += d.x() / ln
        if abs(up) < 0.1:
            return 0.0
        return -(w / 2 - 12) if up > 0 else (w / 2 - 12)

    def root_chip(self):
        if self.nv is None:
            return None
        return next((c for c in (self.nv.chips or []) if c.kind == "ROOT"), None)

    def port_leds(self) -> list[str | None] | None:
        """Front-panel LEDs from the live switch ports: green forwarding, amber STP-blocked, red
        err-disabled, dark = not connected / shut."""
        sw = self.switch_view
        if self.kind != "l2switch" or sw is None or not sw.ports:
            return [] if self.kind == "l2switch" and self.state in ("BOOTED",) else None
        out = []
        for pr in sw.ports:
            if pr["err_disabled"]:
                out.append("err")
            elif pr["status"] != "connected":
                out.append(None)
            elif pr["blocked"]:
                out.append("blk")
            else:
                out.append("up")
        return out

    def _paint_chip_row(self, p: QPainter, y: float, specs: list, tk: T.Tokens) -> None:
        """Legacy horizontal chip row (kept for callers outside the canvas)."""
        f = T.chip_font()
        fm = T.fm(f)
        show = specs[:3]
        extra = len(specs) - len(show)
        widths = [T.chip_width(s, fm) for s, _ in show]
        more_spec = T.ChipSpec(f"+{extra}", T.NEUTRAL, None, outline=True) if extra else None
        if more_spec:
            widths.append(T.chip_width(more_spec, fm))
        total = sum(widths) + 4 * (len(widths) - 1)
        align, x0 = getattr(self, "_chip_align", 0), getattr(self, "_chip_x0", 0.0)
        x = -total / 2 if align == 0 else (x0 if align > 0 else x0 - total)
        for (spec, tip), w in zip(show, widths):
            r = T.draw_chip(p, x, y, spec, tk, f)
            self._hit(p, r, tip)
            x += w + 4
        if more_spec:
            r = T.draw_chip(p, x, y, more_spec, tk, f)
            self._hit(p, r, "<br>".join(t for _, t in specs[3:]))

    def label_side(self) -> str:
        """'below' unless a link leaves the tile (nearly) straight down - then 'right' / 'left'
        (whichever is free of near-horizontal links), else 'above'."""
        me = self.pos()
        dirs = []
        for li in self.links:
            other = li.b if li.a is self else li.a
            d = other.pos() - me
            ln = math.hypot(d.x(), d.y()) or 1.0
            dirs.append((d.x() / ln, d.y() / ln))
        if not any(dy > 0.9 for dx, dy in dirs):
            return "below"
        for side, sgn in (("right", 1), ("left", -1)):
            if not any(dx * sgn > 0.55 and abs(dy) < 0.75 for dx, dy in dirs):
                return side
        return "below"

    def fhrp_side(self) -> int:
        """-1: badges left of the tile, +1: right - whichever side has fewer links leaving it."""
        score = {-1: 0.0, 1: 0.0}
        me = self.pos()
        for li in self.links:
            other = li.b if li.a is self else li.a
            d = other.pos() - me
            ln = math.hypot(d.x(), d.y()) or 1.0
            cx = d.x() / ln
            if abs(cx) > 0.35:
                score[1 if cx > 0 else -1] += abs(cx)
        return -1 if score[-1] <= score[1] else 1

    @staticmethod
    def _fhrp_width(b) -> float:
        proto = _ev(b.proto)
        return (T.fm(T.type("caption")).horizontalAdvance(f"{proto} {b.role}")
                + T.fm(T.type("caption", weight=400, scale=0.9)).horizontalAdvance(f"g{b.group}") + 20)

    def _paint_fhrp_badge(self, p: QPainter, b, r: QRectF, tk) -> QRectF:
        """V5/V8: one HSRP/VRRP role badge in the chip column (filled = active / master)."""
        proto = _ev(b.proto)
        hue = QColor(T.PROTO_HUES.get(proto, T.NEUTRAL))
        h = r.height()
        p.save()
        if b.stale:
            p.setOpacity(p.opacity() * 0.45)
        p.setPen(QPen(hue, 1.0))
        p.setBrush(hue if b.primary else tk.card)
        p.drawRoundedRect(r.adjusted(0.5, 0.5, -0.5, -0.5), h / 2, h / 2)
        p.setFont(T.type("caption"))
        p.setPen(QColor("white") if b.primary else T._hue_text(hue, tk))
        tr = r.adjusted(7, 0, -7, 0)
        p.drawText(tr, Qt.AlignVCenter | Qt.AlignLeft, f"{proto} {b.role}")
        p.setFont(T.type("caption", weight=400, scale=0.9))
        c = QColor("white") if b.primary else QColor(tk.muted)
        c.setAlphaF(0.85)
        p.setPen(c)
        p.drawText(tr, Qt.AlignVCenter | Qt.AlignRight, f"g{b.group}")
        p.restore()
        return r

    def _paint_fhrp(self, p: QPainter, br: QRectF, tk: T.Tokens, side: int) -> None:
        """V5/V8: HSRP/VRRP role badges hugging the tile's free side (filled = active/master)."""
        badges = self.nv.fhrp[:3]
        f = T.type("caption")
        g = T.type("caption", weight=400, scale=0.9)
        fm = T.fm(f)
        h = T.CHIP_H
        total = h * len(badges) + 2 * (len(badges) - 1)
        y = br.center().y() - total / 2
        for b in badges:
            proto = _ev(b.proto)
            hue = QColor(T.PROTO_HUES.get(proto, T.NEUTRAL))
            text = f"{proto} {b.role}"
            gtxt = f"g{b.group}"
            w = fm.horizontalAdvance(text) + T.fm(g).horizontalAdvance(gtxt) + 18
            x = br.left() - 8 - w if side < 0 else br.right() + 8
            r = QRectF(x, y, w, h)
            p.save()
            if b.stale:
                p.setOpacity(0.45)
            p.setPen(QPen(hue, 1.0))
            p.setBrush(hue if b.primary else tk.card)
            p.drawRoundedRect(r.adjusted(0.5, 0.5, -0.5, -0.5), h / 2, h / 2)
            p.setFont(f)
            p.setPen(QColor("white") if b.primary else T._hue_text(hue, tk))
            tr = r.adjusted(7, 0, -6, 0)
            p.drawText(tr, Qt.AlignVCenter | Qt.AlignLeft, text)
            p.setFont(g)
            c = QColor("white") if b.primary else tk.muted
            c.setAlphaF(0.85)
            p.setPen(c)
            p.drawText(tr, Qt.AlignVCenter | Qt.AlignRight, gtxt)
            p.restore()
            tip = b.tooltip + (" (stale)" if b.stale else "")
            self._hit(p, r, tip)
            y += h + 2

    # ------------------------------------------------------------------ events
    def hoverEnterEvent(self, ev):
        self.prepareGeometryChange()
        self._hover = True
        self.update()
        super().hoverEnterEvent(ev)

    def hoverLeaveEvent(self, ev):
        self.prepareGeometryChange()
        self._hover = False
        self._update_tooltip()
        self.update()
        super().hoverLeaveEvent(ev)

    def hoverMoveEvent(self, ev):
        pos = ev.pos()
        for r, tip in self._hits:
            if r.adjusted(-2, -2, 2, 2).contains(pos):
                self.setToolTip(tip)
                break
        else:
            self._update_tooltip()
        super().hoverMoveEvent(ev)

    def itemChange(self, change, value):
        if change == QGraphicsItem.ItemPositionHasChanged:
            for link in self.links:
                link.adjust()
            sc = self.scene()
            if sc is not None and hasattr(sc, "node_moved_hook"):
                sc.node_moved_hook()
        elif change == QGraphicsItem.ItemSelectedHasChanged:
            self.update()
        elif change == QGraphicsItem.ItemSceneHasChanged:
            self.sync_spinner()
        return super().itemChange(change, value)

    def mousePressEvent(self, ev):
        self._press_pos = QPointF(self.pos())
        self.setCursor(Qt.ClosedHandCursor)
        super().mousePressEvent(ev)

    def mouseReleaseEvent(self, ev):
        self.setCursor(Qt.OpenHandCursor)
        super().mouseReleaseEvent(ev)
        if self._press_pos is not None and (self.pos() - self._press_pos).manhattanLength() > 2:
            self.moved.emit(self.key, self.pos().x(), self.pos().y())
        elif self._press_pos is not None and ev.button() == Qt.LeftButton:
            self.clicked.emit(self.key)
        self._press_pos = None


# ============================================================================ link visual (pure)
@dataclass
class LinkVisual:
    stroke: str                     # key into theme.LINK_STROKES
    blue: bool = False              # V6: paint the default-route hop blue
    direction: str | None = None    # 'a2b' | 'b2a' (chevrons)
    pill: str | None = None         # down / not-started text in the midpoint stack
    pill_color: str | None = None
    pill_icon: str = ""
    pill_tip: str = ""
    down_side: str | None = None    # 'a' | 'b': ✕ marker + red interface pill
    side_colors: tuple = (None, None)
    side_kinds: tuple = (None, None)  # raw LinkSide values ('UP' / 'DOWN' / 'PROTO_DOWN' / 'ADMIN_DOWN' / ...)
    bead: str | None = None         # 'P2P' | 'P2P?' | 'BCAST'
    bead_tip: str = ""
    condition: str | None = None    # '600 ms ±20'
    unknown: bool = False
    # ---- switching (managed-switch ends; layer 'l2')
    trunk: str | None = None        # 'TRUNK 10,20' bead
    trunk_tip: str = ""
    access_side: str | None = None  # 'a' | 'b': 'V10' chip at that (switch) end
    access_text: str | None = None  # 'V10'
    access_vlan: int | None = None
    stp_side: str | None = None     # 'a' | 'b': hatched 'Altn BLK' end cap + muted dashed half
    stp_label: str | None = None    # 'Altn BLK' / 'BLK v10'
    stp_tip: str = ""
    vlans: tuple = ()               # VLANs carried (VLAN colouring layer)
    # ---- subnets on cables (layer 'subnets')
    subnet: str | None = None       # '10.0.12.0/30' | '½ 10.0.12.0/30' | '⚠ mismatch a ≠ b' | 'dhcp …'
    subnet_compact: str | None = None   # zoomed-out text
    subnet_style: str = "match"     # match | half | mismatch | dhcp
    subnet_tip: str = ""
    subnet_stale: bool = False
    host_a: str = ""                # '.1' next to the a-end interface name
    host_b: str = ""
    # ---- EtherChannel member cable (the BundleItem draws bead / lasso / the bundle's STP marker)
    bundle: str | None = None       # facts.Bundle.key
    ec_side: str | None = None      # 'a' | 'b' | 'ab': end(s) whose member port is NOT bundled
    ec_tag: str | None = None       # 'suspended' | 'stand-alone' | 'down' | 'hot-standby' | 'waiting'
    ec_flag: str = ""               # IOS member flag of that end ('s' / 'I' / 'D' / 'H' / 'w')
    ec_tag_tip: str = ""
    ec_tags: tuple = ()             # ((side, words, flag, tip), ...) for every unbundled end
    stp_on_bundle: bool = False     # STP blocks the whole Port-channel: one marker on the bundle, none per member


SIDE_COLORS = {"UP": T.LINK_UP, "ADMIN_DOWN": T.DOWN_RED, "DOWN": T.DOWN_RED, "PROTO_DOWN": T.PROTO_ORANGE,
               "UNKNOWN": T.NEUTRAL, "NOT_APPLICABLE": None}


def link_visual(lv, layers: dict | None = None) -> LinkVisual:
    """LinkView (+ layers) -> what to paint.  Pure: unit-tested per LinkKind."""
    layers = layers or {}
    st = getattr(lv, "status", None)
    if st is None:                                  # telemetry off / not joined: API state only
        up = lv.state in ("STARTED", "running", "up")
        return LinkVisual("API_UP" if up else "API_DOWN")
    kind = _ev(st.kind)
    vis = LinkVisual(kind if kind in T.LINK_STROKES else "UNKNOWN")
    vis.side_colors = (SIDE_COLORS.get(_ev(lv.side_a)) if lv.side_a is not None else None,
                       SIDE_COLORS.get(_ev(lv.side_b)) if lv.side_b is not None else None)
    vis.side_kinds = (_ev(lv.side_a) if lv.side_a is not None else None,
                      _ev(lv.side_b) if lv.side_b is not None else None)
    tele = getattr(lv, "tele", None)
    # V6: blue only on hops whose status is exactly UP
    if kind == "UP" and lv.default_hop is not None and layers.get("default", True):
        vis.blue = True
        vis.direction = getattr(tele, "default_direction", None) or "a2b"
    if kind in ("ADMIN_DOWN", "LINK_DOWN", "PROTOCOL_DOWN", "CABLE_PULLED"):
        vis.pill = st.text or kind.replace("_", " ").lower()
        vis.pill_color = T.PROTO_ORANGE if kind == "PROTOCOL_DOWN" else T.DOWN_RED
        vis.pill_icon = "scissors" if kind == "CABLE_PULLED" else "cross"
        vis.pill_tip = st.plain_text or st.text
        if st.side_node:
            vis.down_side = "a" if st.side_node == lv.a else ("b" if st.side_node == lv.b else None)
    elif kind in ("LINK_NOT_STARTED", "UNKNOWN", "NODE_OFF", "LAB_OFF"):
        vis.pill = st.text if kind in ("LINK_NOT_STARTED", "UNKNOWN") else None
        vis.pill_color = T.NEUTRAL
        vis.pill_icon = "question" if kind == "UNKNOWN" else ""
        vis.pill_tip = st.plain_text or st.text
        vis.unknown = kind == "UNKNOWN"
    # V3: bead only from the real network type
    if layers.get("p2p", True):
        src = _ev(lv.p2p_source) if lv.p2p_source is not None else None
        iface = None
        if tele is not None:
            iface = (tele.iface_a.short if tele.iface_a else None) or (tele.iface_b.short if tele.iface_b else None)
        if lv.p2p and src and src != "CONFIG":
            vis.bead = "P2P"
            how = {"OSPF_OPER": "from show ip ospf interface", "JUNOS_PTTOPT": "Junos OSPF interface type P2P",
                   "FORTIOS_P2P": "FortiOS network type POINTOPOINT", "MEDIUM": "serial / PPP medium"}.get(src, src)
            vis.bead_tip = (f"OSPF network point-to-point{f' ({iface})' if iface else ''}: {how}"
                            if src != "MEDIUM" else f"point-to-point medium: {how}")
        elif lv.p2p_conflict or src == "CONFIG":
            vis.bead = "P2P?"
            vis.bead_tip = lv.p2p_conflict or "configured p2p, not active"
        elif tele is not None and tele.bcast:
            vis.bead = "BCAST"
            vis.bead_tip = ("OSPF network broadcast on a 2-router link (DR/BDR election)"
                            + (f": {tele.bcast_detail}" if tele.bcast_detail else ""))
    cond = getattr(lv, "condition", None)
    if cond is not None and cond.enabled:
        vis.condition = cond.label or "conditioned"
    _subnet_visual(vis, lv, kind, layers)
    if tele is not None and getattr(tele, "bundle", None) and layers.get("l2", True):
        _bundle_member_visual(vis, lv, tele, kind)
        return vis
    if tele is not None and getattr(tele, "l2_a", None) is None and getattr(tele, "l2_b", None) is None:
        return vis
    if tele is not None:
        vis.vlans = tuple(getattr(tele, "vlans", ()) or ())
    if tele is not None and layers.get("l2", True):
        if tele.trunk and kind == "UP" and tele.vlans:
            vis.trunk = f"TRUNK {tele.trunk_text}"
            vis.trunk_tip = (f"802.1Q trunk carrying VLAN {tele.trunk_text}"
                             + (f"<br>native (untagged) VLAN {tele.native_vlan}" if tele.native_vlan is not None
                                else "") + ("<br><b>native VLAN mismatch between the two ends</b>"
                                            if tele.native_mismatch else ""))
        elif tele.access_vlan is not None and tele.access_side:
            vis.access_side, vis.access_vlan = tele.access_side, tele.access_vlan
            vis.access_text = f"V{tele.access_vlan}"
        b = tele.stp_block
        if b is not None and kind == "UP":
            from ..parsers.ios_l2 import vlan_text
            vis.stp_side = b.side
            role = _ev(b.role)
            vis.stp_label = f"{role} BLK" if not b.partial else f"BLK v{vlan_text(b.vlans)}"
            if role in ("Altn", "Back"):
                vis.stp_tip = (f"<b>Spanning tree is blocking this end</b> ({b.role_word} port, VLAN "
                               f"{vlan_text(b.vlans)}).<br>The cable is up: STP keeps it as a backup path so the "
                               "two switches do not form a loop. It takes over if the forwarding path fails.")
            else:
                vis.stp_label = f"{role} BLK"
                vis.stp_tip = (f"<b>Spanning tree is discarding on this end</b> ({b.role_word} port, VLAN "
                               f"{vlan_text(b.vlans)}) while RSTP converges - it normally forwards within seconds.")
    return vis


EC_TAG_SHORT = {"suspended": "susp", "stand-alone": "alone", "down": "down", "hot-standby": "standby",
                "waiting": "wait", "unsuitable": "unsuitable"}


def _bundle_member_visual(vis: LinkVisual, lv, tele, kind: str) -> None:
    """A member cable of an EtherChannel: the bundle shows the bead, trunk, STP marker and alarms; the member
    keeps its stroke + end shapes, and a non-bundled member gets a marker + muted dashed stroke."""
    vis.bundle = tele.bundle
    vis.trunk = None
    vis.bead = None
    vis.subnet = None
    vis.access_side = vis.access_text = None
    vis.vlans = tuple(getattr(tele, "vlans", ()) or ())
    vis.pill = None                                  # the bundle carries '1/2' and the down text
    b = tele.stp_block
    if b is not None and kind == "UP":
        vis.stp_side = b.side
        vis.stp_on_bundle = True
    bad = [(s_, m) for s_, m in (("a", tele.ec_a), ("b", tele.ec_b)) if m is not None and not m.bundled]
    if bad:
        vis.ec_side = "".join(s_ for s_, _ in bad)
        tags = []
        for side, m in bad:
            tip = (f"<b>Not bundled</b>: {html.escape(m.port)} is {html.escape(m.words)} "
                   f"(flag {html.escape(m.flags)})" + (f" - {html.escape(m.problem)}" if m.problem else "")
                   + "<br>This cable is not part of the EtherChannel right now.")
            tags.append((side, m.words, m.flags, tip))
        vis.ec_tags = tuple(tags)
        vis.ec_tag, vis.ec_flag, vis.ec_tag_tip = tags[0][1], tags[0][2], tags[0][3]


SUBNET_FULL_LOD = 0.55        # full '10.0.12.0/30' (counter-scaled text, legible at fit zoom) and host parts
SUBNET_MIN_LOD = 0.33         # compact '…12.0/30' between this and FULL; below: only a mismatch '⚠'
SUBNET_STYLES = {"match": "match", "one_sided": "half", "mismatch": "mismatch", "dhcp": "dhcp",
                 "unnumbered": "dhcp"}


def _subnet_visual(vis: LinkVisual, lv, kind: str, layers: dict) -> None:
    """Layer 'subnets': the cable's subnet label + host parts at the ends (from LinkView.addressing)."""
    ad = getattr(lv, "addressing", None)
    if ad is None or not layers.get("subnets", True) or kind in ("LAB_OFF", "NODE_OFF"):
        return
    state = _ev(ad.state)
    if state == "none" or not ad.label:
        return
    vis.subnet_style = SUBNET_STYLES.get(state, "match")
    vis.subnet = ("½ " + ad.label) if state == "one_sided" else ad.label
    vis.subnet_compact = ("½ " + ad.compact) if state == "one_sided" else (ad.compact or ad.label)
    lines = [ln for ln in (ad.tooltip or ad.label).split("\n") if ln]
    if lines:
        lines[0] = f"<b>{html.escape(lines[0])}</b>"
        lines[1:] = [html.escape(x) for x in lines[1:]]
    vis.subnet_tip = "<br>".join(lines)
    vis.subnet_stale = bool(ad.stale)
    vis.host_a, vis.host_b = ad.host_a or "", ad.host_b or ""


def iface_font(bold: bool = False):
    """Interface names at link ends: caption mono (S4)."""
    return T.mono(1.0, "caption", weight=600 if bold else 500)


# ============================================================================ link item
class LinkItem(_HitMixin, QGraphicsItem):
    def __init__(self, key: str, a: NodeItem, b: NodeItem, label_a: str = "", label_b: str = "",
                 dashed: bool = False):
        super().__init__()
        self.key = key
        self.a, self.b = a, b
        self.label_a, self.label_b = label_a, label_b
        self.dashed = dashed                 # KVM link: drawn as a double line
        self.state = ""
        self.lv = None
        self.fan = 0.0                       # signed curve offset (px) for parallel links
        self._hover = False
        self._hits: list[tuple[QRectF, str]] = []
        self.hidden_labels: set = set()      # S8 LabelLayout: ids suppressed by the scene pass
        self.setZValue(1)
        self.setFlags(QGraphicsItem.ItemIsSelectable)
        self.setAcceptHoverEvents(True)
        a.links.append(self)
        b.links.append(self)
        self._path = QPainterPath()
        self._p1 = self._p2 = QPointF()
        self.adjust()

    def detach(self):
        for n in (self.a, self.b):
            if self in n.links:
                n.links.remove(self)

    def ctx(self) -> RenderCtx:
        sc = self.scene()
        return getattr(sc, "ctx", None) or RenderCtx()

    # ------------------------------------------------------------------ geometry
    def adjust(self):
        self.prepareGeometryChange()
        ca, cb = self.a.pos(), self.b.pos()
        v = cb - ca
        ln = math.hypot(v.x(), v.y()) or 1.0
        n = QPointF(-v.y() / ln, v.x() / ln)
        mid = (ca + cb) / 2
        ctrl = mid + n * (self.fan * 2) if self.fan else mid
        p1 = ca + anchor_point(self.a.outline, ctrl - ca)
        p2 = cb + anchor_point(self.b.outline, ctrl - cb)
        path = QPainterPath(p1)
        if self.fan:
            path.quadTo(ctrl, p2)
        else:
            path.lineTo(p2)
        self._path, self._p1, self._p2 = path, p1, p2

    def set_state(self, state: str):
        if state != self.state:
            self.state = state
            self.update()

    def set_view(self, lv) -> None:
        self.lv = lv
        tip = getattr(getattr(lv, "tele", None), "tooltip", "") or ""
        self.setToolTip(tip.replace("\n", "<br>") if tip else "")
        self.update()

    @property
    def path(self) -> QPainterPath:
        return self._path

    def visual(self) -> LinkVisual:
        if self.lv is None:
            up = self.state in ("STARTED", "running", "up")
            return LinkVisual("API_UP" if up else "API_DOWN")
        return link_visual(self.lv, self.ctx().layers)

    def boundingRect(self):
        return self._path.boundingRect().adjusted(-90, -70, 90, 70)

    def shape(self):
        from PySide6.QtGui import QPainterPathStroker
        s = QPainterPathStroker()
        s.setWidth(14)
        path = s.createStroke(self._path)
        for r, _ in self._hits:                  # pills / beads / markers are hoverable
            path.addRect(r)
        return path

    # ------------------------------------------------------------------ painting
    def paint(self, p: QPainter, opt, widget=None):
        p.setRenderHint(QPainter.Antialiasing)
        ctx = self.ctx()
        tk = ctx.tokens
        lod = _lod(p, opt)
        self._begin_hits(p)
        path = self._path
        L = path.length()
        if L < 2:
            return
        vis = self.visual()
        style = T.LINK_STROKES[vis.stroke]
        sel = self.isSelected()
        lv = self.lv
        # flash halo (V10)
        fl = ctx.flash.get(self.key)
        if fl and ctx.now - fl[0] < 0.8:
            a = 1.0 - (ctx.now - fl[0]) / 0.8
            p.setPen(QPen(T.alpha(fl[1], 0.45 * a), 14, Qt.SolidLine, Qt.RoundCap))
            p.drawPath(path)
        # selection: accent_ring halo (never route blue); hover: N12 at 8 %
        if sel:
            p.setPen(QPen(T.alpha(tk.accent_ring, 0.30), 12, Qt.SolidLine, Qt.RoundCap))
            p.drawPath(path)
            p.setPen(QPen(T.alpha(tk.accent_ring, 0.55), 6, Qt.SolidLine, Qt.RoundCap))
            p.drawPath(path)
        elif self._hover:
            p.setPen(QPen(T.alpha(tk.ink, 0.08), 10, Qt.SolidLine, Qt.RoundCap))
            p.drawPath(path)
        # FHRP segment tint (router <-> switch links of an HSRP/VRRP segment)
        tint = ctx.fhrp_tint.get(self.key) if ctx.layers.get("fhrp", True) else None
        if tint and not vis.blue:
            p.setPen(QPen(T.alpha(tint, 0.22), 8, Qt.SolidLine, Qt.RoundCap))
            p.drawPath(path)
        # VLAN colouring layer: halo on links carrying the chosen VLAN, everything else dimmed
        vh = ctx.vlan_hl if ctx.layers.get("vlan", False) else None
        dimmed = False
        if vh is not None:
            if vh in vis.vlans:                      # S8 layer focus: 6 px halo at 28 % hue alpha
                hue = ctx.vlan_hues.get(vh, T.VLAN_PALETTE[0])
                pen = QPen(T.alpha(hue, 0.28), style.width + 12, Qt.SolidLine, Qt.RoundCap)
                if vis.stp_side and lv is not None and lv.tele is not None and lv.tele.stp_block and \
                        vh in lv.tele.stp_block.vlans:
                    pen.setStyle(Qt.DashLine)            # carried but blocked by STP for this VLAN
                p.setPen(pen)
                p.drawPath(path)
            else:
                dimmed = True
                p.setOpacity(dim_factor(0.0))            # composite dimming floor (40 %)
        # main stroke
        if vis.bundle and vis.ec_side and vis.stroke in ("UP", "PROTOCOL_DOWN", "UNKNOWN") and not vis.blue:
            # EtherChannel member that is not bundled (stand-alone / suspended): muted, dashed - not part of the Po
            pen = QPen(T.alpha(tk.N(10), 0.85), 2.0, Qt.SolidLine, Qt.FlatCap)
            pen.setDashPattern([3.0, 2.4])
            p.setPen(pen)
            p.drawPath(path)
        elif vis.stp_side and not vis.blue and not self.dashed:
            # STP blocked: normal stroke from the forwarding end, muted dashes on the blocked half
            near_b = vis.stp_side == "b"
            cut = 0.5
            good = _sub_path(path, 0.0, cut) if near_b else _sub_path(path, cut, 1.0)
            bad = _sub_path(path, cut, 1.0) if near_b else _sub_path(path, 0.0, cut)
            p.setPen(T.stroke_pen(style, tk=tk))
            p.drawPath(good)
            pen = QPen(tk.N(9), 2.2, Qt.SolidLine, Qt.FlatCap)     # muted: carries nothing
            pen.setDashPattern([2.5, 2.2])
            p.setPen(pen)
            p.drawPath(bad)
        elif vis.blue and (self.a.cloud_down or self.b.cloud_down):
            # S7: 'route exists, egress dead' - blue dashed 6/4 at 55 % (differs from 'no route')
            pen = QPen(T.alpha(T.ROUTE_BLUE, 0.55), 3.2, Qt.SolidLine, Qt.FlatCap)
            pen.setDashPattern([6.0 / 3.2, 4.0 / 3.2])
            p.setPen(pen)
            p.drawPath(path)
        elif vis.blue:
            p.setPen(QPen(T.alpha(T.ROUTE_BLUE, 0.22), 9, Qt.SolidLine, Qt.RoundCap))
            p.drawPath(path)
            p.setPen(QPen(QColor(T.ROUTE_BLUE), 3.4, Qt.SolidLine, Qt.RoundCap))
            p.drawPath(path)
        elif self.dashed:                                   # KVM: double line
            st = QPainterPath(path)
            pen = T.stroke_pen(style, width=1.3, tk=tk)
            p.setPen(pen)
            for off in (-2.2, 2.2):
                p.drawPath(_offset_path(st, off))
        elif style.gap:
            gap = min(18.0, L * 0.2)
            t0 = path.percentAtLength(max(0.0, L / 2 - gap / 2))
            t1 = path.percentAtLength(min(L, L / 2 + gap / 2))
            p.setPen(T.stroke_pen(style, tk=tk))
            p.drawPath(_sub_path(path, 0.0, t0))
            p.drawPath(_sub_path(path, t1, 1.0))
            for t in (t0, t1):
                pt, nrm = path.pointAtPercent(t), _normal(path, t)
                p.setPen(QPen(T.resolve(style.color, tk), 2.0, Qt.SolidLine, Qt.RoundCap))
                p.drawLine(pt - nrm * 5, pt + nrm * 5)
        else:
            p.setPen(T.stroke_pen(style, tk=tk))
            p.drawPath(path)
        # adjacency rails (V4, layer off by default; 2-router segments only)
        tele = getattr(lv, "tele", None)
        if tele is not None and tele.protocols and lod >= T.LOD_TIER1:
            self._paint_rails(p, tele, ctx, tk, 8.0 if vis.blue else 5.5)
        # chevrons on blue hops
        if vis.blue and lod >= T.LOD_TIER1 and L > 120 and not (self.a.cloud_down or self.b.cloud_down):
            self._paint_chevrons(p, vis.direction, L)
        # tier 0: S2 end shapes (and the hatched BLK cap) at every zoom
        self._paint_end_glyphs(p, vis, tk, L, lod)
        # EtherChannel: marker on a member that is not bundled (tier 0 shape, words from tier 1)
        if vis.ec_tag:
            self._paint_ec_marker(p, vis, tk, L, lod)
        # tier 2: interface pills at each end
        self._paint_end_pills(p, vis, tk, L, lod)
        # midpoint stack (V2): down text / ⌁ condition / bead / route mark / traffic / subnet
        self._paint_stack(p, vis, tk, ctx, L, lod, sel)
        if dimmed:
            p.setOpacity(1.0)

    def _paint_ec_marker(self, p, vis: LinkVisual, tk, L: float, lod: float):
        for side, words, flag, tip in vis.ec_tags or ((vis.ec_side[0], vis.ec_tag, vis.ec_flag, vis.ec_tag_tip),):
            self._paint_one_ec_marker(p, side, words, flag, tip, tk, L, lod)

    def _paint_one_ec_marker(self, p, side, words, flag, tip, tk, L: float, lod: float):
        """A member that is not bundled: a ring badge with the IOS flag letter ('s' / 'I' / 'D' / 'w' / 'H') on
        the cable just outside the bundle's lasso, on that end's side; hover / selection spells it out."""
        reveal = self._hover or self.isSelected()
        gk = glyph_scale(lod)
        d = max(L * 0.18, L / 2 - 24.0 - 12.0 * gk)
        t = self._path.percentAtLength(d if side == "a" else L - d)
        pt = self._path.pointAtPercent(t)
        down = flag == "D" or words == "down"
        col = T.status_fill("down" if down else "warn", tk)
        txt_col = T.status_text("down" if down else "warn", tk)
        if reveal:
            k = text_scale(lod)
            _scaled(p, pt, k)
            r = T.draw_pill(p, pt, f"{flag} · {words}" if flag else words, tk, border=col, text_color=txt_col,
                            fill=T.alpha(col, 0.12), height=14, pad=4, dashed=not down, f=T.type("micro"))
        else:
            _scaled(p, pt, gk)
            p.setPen(QPen(tk.base, 2.0))
            p.setBrush(tk.card)
            p.drawEllipse(pt, 7.2, 7.2)
            pen = QPen(col, 1.6)
            if not down:
                pen.setDashPattern([2.0, 1.4])
            p.setPen(pen)
            p.drawEllipse(pt, 6.4, 6.4)
            letter = (flag or "?")[0]
            if lod * gk >= 0.45:
                p.setFont(T.type("micro"))
                p.setPen(txt_col)
                p.drawText(QRectF(pt.x() - 6, pt.y() - 6.5, 12, 12), Qt.AlignCenter, letter)
            else:
                p.drawLine(QPointF(pt.x() - 3, pt.y()), QPointF(pt.x() + 3, pt.y()))
            r = QRectF(pt.x() - 7.5, pt.y() - 7.5, 15, 15)
        self._hit(p, r.adjusted(-2, -2, 2, 2), tip)
        p.restore()

    def _paint_rails(self, p, tele, ctx, tk, off: float = 5.5):
        sides = {"OSPF": off, "BGP": -off}
        for proto, off in sides.items():
            if not ctx.layers.get(proto.lower(), False):
                continue
            states = [adj[3] for adj in tele.adjacencies if _ev(adj[2]) == proto]
            h = None
            for k, v in tele.protocols.items():
                if _ev(k) == proto:
                    h = _ev(v)
            if h is None:
                continue
            state = T.adjacency_state(states[0]) if states else ("none" if h == "DOWN" else "converging")
            rail = _offset_path(self._path, off, trim=16)
            p.setPen(T.rail_pen(proto, state, tk))
            p.drawPath(rail)
            if rail.length() > 30:
                pt = rail.pointAtPercent(0.78)
                p.setPen(Qt.NoPen)
                p.setBrush(tk.card)
                p.drawEllipse(pt, 6, 6)
                T.draw_icon(p, T.ADJ_ICON[state], QRectF(pt.x() - 4.5, pt.y() - 4.5, 9, 9), T.PROTO_HUES[proto], 1.5)
                tip = f"{proto} adjacency: " + (", ".join(states) if states else "configured, no neighbour")
                self._hit(p, QRectF(pt.x() - 7, pt.y() - 7, 14, 14), tip)

    def _paint_chevrons(self, p, direction, L):
        start, end = 46.0, L - 46.0
        mid_clear = (L / 2 - 22, L / 2 + 22)
        d = start
        reverse = direction == "b2a"
        while d <= end:
            if not (mid_clear[0] < d < mid_clear[1]):
                t = self._path.percentAtLength(d)
                pt = self._path.pointAtPercent(t)
                ang = -self._path.angleAtPercent(t) + (180 if reverse else 0)
                p.save()
                p.translate(pt)
                p.rotate(ang)
                p.setPen(QPen(QColor("white"), 1.6, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
                p.drawPolyline(QPolygonF([QPointF(-2, -3), QPointF(1.6, 0), QPointF(-2, 3)]))
                p.restore()
            d += 40.0

    def _end_pill_layout(self, vis: LinkVisual, L: float, lod: float) -> list[tuple]:
        """[(idx, side, at_start, text, ptxt, node, t, pt, k, pw)] for the interface pills at both ends
        ('Gi0/0 .1' with the subnets layer).  Shared by painting and the stack placement solver."""
        out = []
        if L < 2 or not (lod >= T.LOD_TIER2 or self._hover or self.isSelected()):
            return out
        if vis.bundle and not (self._hover or self.isSelected()):
            return out                                # the bundle shows one port list per end
        k = text_scale(lod)
        for idx, (text, at_start) in enumerate(((self.label_a, True), (self.label_b, False))):
            if not text or (self.a if at_start else self.b).kind == "cloud":
                continue
            side = "a" if at_start else "b"
            host = vis.host_a if at_start else vis.host_b
            if host:
                text = f"{text} {host}"                  # 'Gi0/0 .1' (layer 'subnets')
            node = self.a if at_start else self.b
            ept0 = self._p1 if at_start else self._p2
            d = T.unit(ept0 - node.pos())
            blk_side = vis.stp_side == side and not vis.stp_on_bundle
            ptxt = f"{text} · {vis.stp_label}" if blk_side else text
            pw = T.fm(iface_font(blk_side)).horizontalAdvance(ptxt) + 12
            # leaving through the bottom: clear the node's label + chip row
            if d.y() > 0.3 and node.kind == "l2switch" and node.label_side() == "below":
                # a switch's chip row is wide: go below it on a diagonal link
                dist = min(max(26.0 * k, 54.0 * k / d.y()), L * 0.45)
            elif d.y() > 0.75 and node.kind != "switch" and node.label_side() == "below":
                dist = 50.0 * k                       # just below the label + chip row
                if dist + 40.0 * k > L and not (self._hover or self.isSelected()):
                    continue                          # no room on a short link: tooltip / inspector only
                dist = min(dist, L * 0.6)
            else:
                # a wide pill must not slide under the tile: keep its half-width clear of the edge
                dist = min(max(26.0 * k, (pw / 2 * abs(d.x()) + 7) * k), L * 0.3)
            # a sibling link leaving the same node at a close angle: stagger (the later key moves further out
            # along its cable until the two pills no longer overlap)
            sib = []
            for other in node.links:
                if other is self or other.key >= self.key or other._path.length() < 2:
                    continue
                oe = other._p1 if other.a is node else other._p2
                od = T.unit(oe - node.pos())
                if d.x() * od.x() + d.y() * od.y() > 0.5:          # within 60 degrees
                    sib += [r for r in other.end_pill_rects(lod, node)]
            t = self._path.percentAtLength(dist if at_start else L - dist)
            pt = self._path.pointAtPercent(t)
            limit = L * 0.45
            while sib and dist < limit:
                me = (pt.x() - pw * k / 2 - 2, pt.y() - 7.5 * k - 2, pw * k + 4, 15.0 * k + 4)
                if not any(_rects_overlap(me, r) for r in sib):
                    break
                dist = min(dist + 5.0 * k, limit)
                t = self._path.percentAtLength(dist if at_start else L - dist)
                pt = self._path.pointAtPercent(t)
            out.append((idx, side, at_start, text, ptxt, node, t, pt, k, pw))
        return out

    def end_pill_rects(self, lod: float, node=None) -> list[tuple]:
        """Scene rects (x, y, w, h) of the interface pills (only the one at ``node`` if given): obstacles for
        the stack placement and for staggering sibling pills."""
        L = self._path.length()
        out = []
        for _idx, _side, _s, _text, _ptxt, nd, _t, pt, k, pw in self._end_pill_layout(self.visual(), L, lod):
            if node is None or nd is node:
                out.append((pt.x() - pw * k / 2, pt.y() - 7.5 * k, pw * k, 15.0 * k))
        return out

    def end_glyph(self, vis: LinkVisual, idx: int) -> str | None:
        """S2 link-end silhouette for end ``idx`` (0 = a, 1 = b): up / down / proto_down / admin_down / neutral."""
        side = "ab"[idx]
        sk = vis.side_kinds[idx] if vis.side_kinds else None
        if sk not in (None, "NOT_APPLICABLE"):
            g = T.LINK_END_GLYPH.get(sk, "neutral")
            if g == "down" and vis.stroke == "ADMIN_DOWN" and vis.down_side == side:
                g = "admin_down"
            return g
        return {"UP": "up", "API_UP": "up", "LINK_DOWN": "down", "CABLE_PULLED": "down",
                "PROTOCOL_DOWN": "proto_down",
                "ADMIN_DOWN": "admin_down" if vis.down_side in (None, side) else "up"}.get(vis.stroke, "neutral")

    def end_glyph_pos(self, at_start: bool, size: float) -> tuple[QPointF, float]:
        """(centre, angle) of an end glyph: just outside the tile, on the stroke."""
        L = self._path.length()
        d = min(L / 2, size / 2 + 1.0)
        t = self._path.percentAtLength(d if at_start else L - d)
        return self._path.pointAtPercent(t), -self._path.angleAtPercent(t)

    def _paint_end_glyphs(self, p, vis: LinkVisual, tk, L, lod):
        """Tier 0: the four S2 end silhouettes (circle / bar / ring / square) and the hatched STP BLK cap."""
        if L < 24:
            return
        gk = glyph_scale(lod)
        size = 7.0 * gk
        for idx, at_start in ((0, True), (1, False)):
            side = "ab"[idx]
            node = self.a if at_start else self.b
            if vis.stp_side == side and not vis.stp_on_bundle:   # slashed amber cap right at the tile edge
                ept = self._p1 if at_start else self._p2
                _scaled(p, ept, gk)
                p.setPen(QPen(tk.base, 1.5))
                p.setBrush(QColor(T.STP_BLOCK))
                p.drawEllipse(ept, 6.5, 6.5)
                if not tk.dark:
                    p.setPen(QPen(T.status_text("warn", tk), 1.0))
                    p.setBrush(Qt.NoBrush)
                    p.drawEllipse(ept, 6.0, 6.0)
                p.setBrush(QBrush(QColor(255, 255, 255, 120), Qt.BDiagPattern))
                p.setPen(Qt.NoPen)
                p.drawEllipse(ept, 5.0, 5.0)
                T.draw_icon(p, "slash", QRectF(ept.x() - 4.5, ept.y() - 4.5, 9, 9), QColor("white"), 2.0)
                self._hit(p, QRectF(ept.x() - 7, ept.y() - 7, 14, 14), vis.stp_tip)
                p.restore()
                continue
            g = self.end_glyph(vis, idx)
            if g is None or (node.kind == "cloud" and not node.is_internet and g == "up"):
                continue
            col = None
            if vis.stroke in ("LAB_OFF", "NODE_OFF", "API_DOWN", "LINK_NOT_STARTED") and g == "neutral":
                col = tk.N(8)
            if vis.blue and g == "up":
                col = QColor(T.ROUTE_BLUE)
            c, ang = self.end_glyph_pos(at_start, size)
            T.draw_status_glyph(p, c, g, size, col, tk, angle=ang, halo=tk.base)
            if g != "up":
                tip = self._side_tip(side) or vis.pill_tip
                self._hit(p, QRectF(c.x() - size, c.y() - size, size * 2, size * 2), tip)

    def _paint_end_pills(self, p, vis: LinkVisual, tk, L, lod):
        reveal = self._hover or self.isSelected()
        for idx, side, at_start, text, ptxt, node, t, pt, k, pw in self._end_pill_layout(vis, L, lod):
            if ("end", side) in self.hidden_labels and not reveal:
                continue
            down = vis.down_side == side
            blk = vis.stp_side == side and not vis.stp_on_bundle
            sk = vis.side_kinds[idx] if vis.side_kinds else None
            ctx = self.ctx()
            border = tk.card_border
            if vis.blue:
                border = T.alpha(T.ROUTE_BLUE, 0.7)
            if sk in ("DOWN", "ADMIN_DOWN"):
                border = T.status_fill("down", tk)
            elif sk == "PROTO_DOWN":
                border = T.status_fill("proto", tk)
            text_col = T.status_text("down", tk) if down else tk.muted
            vh = ctx.vlan_hl if ctx.layers.get("vlan", False) else None
            pl = getattr(getattr(self.lv, "tele", None), f"l2_{side}", None)
            if vh is not None and pl is not None and vh in pl.vlans:
                border = QColor(ctx.vlan_hues.get(vh, T.VLAN_PALETTE[0]))
            _scaled(p, pt, k)
            if blk:
                r = T.draw_pill(p, pt, ptxt, tk, border=QColor(T.STP_BLOCK), text_color=T.status_text("warn", tk)
                                if not tk.dark else QColor("#FDBA74"), height=15, pad=5,
                                fill=T.alpha(T.STP_BLOCK, 0.12), hatch=T.STP_BLOCK, f=iface_font(True))
            else:
                r = T.draw_pill(p, pt, text, tk, border=border, text_color=text_col, height=15, pad=5,
                                fill=T.alpha(T.status_fill("down", tk), 0.10) if down else None,
                                f=iface_font(down))
            tip = self._side_tip(side)
            if blk:
                tip = vis.stp_tip + ("<br><br>" + tip if tip else "")
            if tip:
                self._hit(p, r, tip)
            if vis.access_side == side and vis.access_text:
                hue = QColor(ctx.vlan_hues.get(vis.access_vlan, T.VLAN_PALETTE[0]))
                d2 = T.unit(self._path.pointAtPercent(min(1.0, t + 0.01)) - self._path.pointAtPercent(max(0.0, t - 0.01)))
                sgn = 1 if at_start else -1
                c2 = pt + QPointF(d2.x() * sgn, d2.y() * sgn) * (r.width() / 2 + 15)
                vr = T.draw_pill(p, c2, vis.access_text, tk, border=hue, text_color=T._hue_text(hue, tk),
                                 fill=T.alpha(hue, 0.14), bold=True, height=14, pad=4)
                vname = ctx.vlan_names.get(vis.access_vlan)
                self._hit(p, vr, f"access port in VLAN {vis.access_vlan}" + (f" ({vname})" if vname else "")
                          + f": {node.label} {text} sends this device's frames untagged into VLAN {vis.access_vlan}")
            p.restore()

    def _side_tip(self, side: str) -> str:
        lv = self.lv
        if lv is None or lv.tele is None:
            return ""
        f = lv.tele.iface_a if side == "a" else lv.tele.iface_b
        node = self.a.label if side == "a" else self.b.label
        sv = lv.side_a if side == "a" else lv.side_b
        st = _ev(sv) if sv is not None else ""
        words = {"UP": "up/up", "ADMIN_DOWN": "administratively down (shutdown)", "DOWN": "down (no carrier)",
                 "PROTO_DOWN": "up, line protocol down", "UNKNOWN": "unknown (no fresh probe)",
                 "NOT_APPLICABLE": "not probed (unmanaged switch / connector / host)"}.get(st, st)
        if f is None:
            return f"{node} {self.label_a if side == 'a' else self.label_b}: {words}"
        pl = getattr(lv.tele, f"l2_{side}", None)
        l2 = ""
        if pl is not None:
            mode = _ev(pl.mode)
            if mode == "trunk":
                l2 = f"<br>trunk, VLAN {pl.trunk_text} (native {pl.native})"
            elif mode == "access":
                l2 = f"<br>access port, VLAN {pl.access_vlan}"
            if pl.stp:
                grp = {}
                for v, (role, st_) in sorted(pl.stp.items()):
                    grp.setdefault(f"{_ev(role)} {_ev(st_)}", []).append(v)
                from ..parsers.ios_l2 import vlan_text
                l2 += "<br>STP " + " · ".join(f"{k_} (VLAN {vlan_text(v)})" for k_, v in grp.items())
            if pl.err_disabled:
                l2 += "<br><b>err-disabled</b>"
        return (f"{node} {f.short}: {words}" + (f"<br>{f.ip}" + (f"/{f.prefixlen}" if f.prefixlen is not None else "") if f.ip else "")
                + (f"<br>{f.desc}" if f.desc else "") + l2)

    STACK_H = {"pill": 17, "cond": 16, "mark": 17, "bead": 13, "bead_small": 8, "traffic": 15, "subnet": 14,
               "subnet_hint": 12, "trunk": 13}

    def _stack_items(self, vis: LinkVisual, ctx: RenderCtx, lod: float, sel: bool, hover: bool):
        """(items, vis) of the midpoint stack: [(kind, payload)] top to bottom."""
        lv = self.lv
        tele = getattr(lv, "tele", None)
        items = []                                   # (kind, payload)
        tier1 = lod >= T.LOD_TIER1 or hover or sel
        tier2 = lod >= T.LOD_TIER2 or hover or sel
        k = text_scale(lod)
        if vis.pill and text_visible(7.5, k, lod):  # alarm: every tier while its text is legible
            items.append(("pill", vis))
        if vis.condition and tier1:
            items.append(("cond", vis.condition))
        mark = ctx.path_marks.get(self.key) if ctx.layers.get("default", True) else None
        if mark and items and items[0][0] == "pill":
            # the down pill doubles as the 'default path breaks here' ✕ (one row less on short links)
            v = vis
            vis = LinkVisual(**{**v.__dict__})
            vis.pill_icon = "cross" if mark[0] == "cross" else v.pill_icon
            vis.pill_tip = (v.pill_tip or "") + "<br><br>" + mark[2]
            items[0] = ("pill", vis)
        elif mark:
            items.append(("mark", mark))
        if tier1:                                    # S8: one bead per link (the trunk bead wins)
            if vis.trunk:
                items.append(("trunk", vis))
            elif vis.bead:
                items.append(("bead", vis))
        tr = getattr(lv, "traffic", None)
        if ctx.layers.get("traffic") and tr is not None and tier1:
            items.append(("traffic", tr))
        if vis.subnet:                               # tier 2: full tag; a mismatch stays visible as '⚠' at tier 1
            text = vis.subnet if tier2 else ("⚠" if vis.subnet_style == "mismatch" and tier1 else None)
            sc = self.scene()
            slot = sc.stack_slot(self.key, lod) if (sc is not None and hasattr(sc, "stack_slot")
                                                     and not hover) else None
            if slot is not None and len(slot) > 3 and slot[3] and vis.subnet_compact:
                text = vis.subnet_compact            # crowded: the placement chose the short form
            if text:
                items.append(("subnet", (text, vis)))
        else:
            subnet = getattr(tele, "subnet", None) if not ctx.layers.get("subnets", True) else None
            if subnet and (hover or sel or lod >= 1.2):
                items.append(("subnet_hint", subnet))
        return items, vis

    def _stack_height(self, items, lod) -> float:
        hs = [self.STACK_H[k] for k, _ in items]
        return sum(hs) + 3 * (len(items) - 1)

    @staticmethod
    def _stack_width(items) -> float:
        """Estimated width (unscaled) of the widest stack item, for the placement solver."""
        w = 0.0
        for kind, payload in items:
            w = max(w, LinkItem._item_width(kind, payload))
        return w

    @staticmethod
    def _item_width(kind, payload) -> float:
        if kind == "pill":
            return T.pill_width(payload.pill or "", bold=True, icon=True, height=17)
        if kind == "cond":
            return T.pill_width(payload, icon=True)
        if kind == "subnet":
            return T.fm(T.subnet_font(payload[1].subnet_style)).horizontalAdvance(payload[0]) + 10
        if kind == "trunk":
            return T.fm(T.type("micro")).horizontalAdvance(payload.trunk or "") + 10
        if kind == "bead":
            return max(20.0, T.fm(T.type("micro")).horizontalAdvance(payload.bead or "") + 9)
        if kind == "traffic":
            return 64.0
        if kind == "mark":
            return 16.0
        return 30.0

    def _stack_free(self, vis: LinkVisual, L: float, k: float) -> tuple[float, float]:
        """(start, end) distances along the path that the stack may use: clear of what each end already
        shows (interface pills, node labels + chip rows, the wide STP pill)."""
        res = []
        for node, ept in ((self.a, self._p1), (self.b, self._p2)):
            d = T.unit(ept - node.pos())
            if node.kind == "cloud":
                res.append(10.0)
            elif d.y() > 0.45 and node.kind != "switch" and node.label_side() == "below":
                res.append(64.0 * k if d.y() < 0.75 else 48.0 * k)     # clear the label + chip row
            else:
                res.append(36.0 * k)
        if vis.stp_side in ("a", "b"):                 # the wide hatched 'Gi0/2 · Altn BLK' pill
            i = 0 if vis.stp_side == "a" else 1
            res[i] = max(res[i], 104.0 * k)
        return res[0], L - res[1]

    def stack_candidates(self, lod: float, compact: bool = False):
        """(w, h, [(along, x, y), ...]) for scene_model.place_stacks; None when there is no stack.
        ``compact``: the same with the shorter subnet label (None when that changes nothing)."""
        L = self._path.length()
        if L < 2:
            return None
        ctx = self.ctx()
        vis = self.visual()
        items, vis = self._stack_items(vis, ctx, lod, self.isSelected(), False)
        if compact:
            sub = next((i for i, (kk, _) in enumerate(items) if kk == "subnet"), None)
            if sub is None or not vis.subnet_compact or items[sub][1][0] == vis.subnet_compact:
                return None
            items = list(items)
            items[sub] = ("subnet", (vis.subnet_compact, vis))
        if not items:
            return None
        k = text_scale(lod)
        h = self._stack_height(items, lod) * k
        w = self._stack_width(items) * k
        lo, hi = self._stack_free(vis, L, k)
        if hi - lo <= h:
            lo, hi = 0.0, L
        pref = (lo + hi) / 2 if hi - lo > h else L / 2
        alts = [pref]
        span = (hi - lo) / 2 - h / 2
        for f in (0.45, -0.45, 0.85, -0.85):
            d = pref + f * span
            if lo <= d <= hi:
                alts.append(d)
        for f in (0.5, 0.4, 0.6, 0.32, 0.68, 0.25, 0.75):          # tight: anywhere along the cable
            d = f * L
            if all(abs(d - x) > 4 for x in alts):
                alts.append(d)
        out = []
        for d in alts:
            pt = self._path.pointAtPercent(self._path.percentAtLength(max(0.0, min(L, d))))
            out.append((d, pt.x(), pt.y()))
        # crowded short cable: beside it (just clear of the stroke), at the first few along-positions
        for d, x, y in list(out[:3]):
            t = self._path.percentAtLength(max(0.0, min(L, d)))
            nrm = _normal(self._path, t)
            off = w / 2 * abs(nrm.x()) + h / 2 * abs(nrm.y()) + 5
            for sg in (1, -1):
                out.append((d, x + nrm.x() * off * sg, y + nrm.y() * off * sg))
        return w, h, out

    def _stack_center(self, vis: LinkVisual, items, L: float, lod: float) -> QPointF:
        total = self._stack_height(items, lod)
        k = text_scale(lod)
        sc = self.scene()
        slot = sc.stack_slot(self.key, lod) if sc is not None and hasattr(sc, "stack_slot") else None
        if slot is None:
            lo, hi = self._stack_free(vis, L, k)
            at = (lo + hi) / 2 if hi - lo > total * k else L / 2
            return self._path.pointAtPercent(self._path.percentAtLength(max(0.0, min(L, at))))
        return QPointF(slot[1], slot[2])

    def label_entries(self, lod: float) -> list:
        """LabelLayout input: [(prio, id, scene rect)] - 0 alarm (down pill, path-break mark), 1 bead / trunk /
        condition / traffic, 2 subnet tag, 3 interface name."""
        out = []
        L = self._path.length()
        if L < 2:
            return out
        vis = self.visual()
        for _idx, side, _s, _t, _ptxt, _n, _tt, pt, kk, pw in self._end_pill_layout(vis, L, lod):
            out.append((3, ("end", side), QRectF(pt.x() - pw * kk / 2, pt.y() - 7.5 * kk, pw * kk, 15.0 * kk)))
        items, vis2 = self._stack_items(vis, self.ctx(), lod, False, False)
        if items:
            k = text_scale(lod)
            c = self._stack_center(vis2, items, L, lod)
            y = c.y() - self._stack_height(items, lod) / 2
            for kind, payload in items:
                h = self.STACK_H[kind]
                w = self._item_width(kind, payload)
                r = _scale_rect(QRectF(c.x() - w / 2, y, w, h), c, k)
                prio = 0 if kind in ("pill", "mark") else (2 if kind in ("subnet", "subnet_hint") else 1)
                out.append((prio, ("stack", kind), r))
                y += h + 3
        return out

    def _paint_stack(self, p, vis: LinkVisual, tk, ctx: RenderCtx, L, lod, sel):
        items, vis = self._stack_items(vis, ctx, lod, sel, self._hover)
        if not items:
            return
        heights = dict(self.STACK_H)
        total = self._stack_height(items, lod)
        k = text_scale(lod)
        mid = self._stack_center(vis, items, L, lod)
        y = mid.y() - total / 2
        _scaled(p, mid, k)
        for k, payload in items:
            h = heights[k]
            c = QPointF(mid.x(), y + h / 2)
            if k == "pill":
                v = payload
                neutral = v.pill_color == T.NEUTRAL
                state = "proto" if v.pill_color == T.PROTO_ORANGE else "down"
                if neutral:
                    r = T.draw_pill(p, c, v.pill, tk, border=tk.N(8), text_color=tk.muted, icon=v.pill_icon,
                                    icon_color=tk.N(9), height=17, dashed=True)
                else:
                    r = T.draw_pill(p, c, v.pill, tk, border=T.status_fill(state, tk),
                                    text_color=T.status_text(state, tk), bold=True,
                                    glyph="proto_down" if state == "proto" and v.pill_icon == "cross" else None,
                                    icon=v.pill_icon if not (state == "proto" and v.pill_icon == "cross") else "",
                                    icon_color=T.status_fill(state, tk),
                                    fill=T.alpha(T.status_fill(state, tk), 0.10), height=17)
                self._hit(p, r, v.pill_tip)
            elif k == "cond":
                hue = QColor(T.EVENT_HUES["sim"]) if not tk.dark else QColor("#A78BFA")
                r = T.draw_pill(p, c, payload, tk, border=hue, text_color=hue, icon="bolt", icon_color=hue,
                                height=16)
                self._hit(p, r, f"link conditioning (netem) active: {payload}<br>right-click → Clear condition")
            elif k == "mark":
                icon, col, tip = payload
                p.setPen(QPen(tk.base, 2.0))
                p.setBrush(T.resolve(col, tk))
                p.drawEllipse(c, 8, 8)
                T.draw_icon(p, icon, QRectF(c.x() - 5, c.y() - 5, 10, 10), QColor("white"), 1.8)
                self._hit(p, QRectF(c.x() - 9, c.y() - 9, 18, 18), tip)
            elif k == "bead":
                v = payload
                r = T.draw_bead(p, c, v.bead, tk, outlined=v.bead == "P2P?", lod=lod)
                self._hit(p, r.adjusted(-3, -3, 3, 3), v.bead_tip)
            elif k == "trunk":
                v = payload
                vh = ctx.vlan_hl if ctx.layers.get("vlan", False) else None
                r = T.draw_trunk_bead(p, c, v.trunk, tk, hue=ctx.vlan_hues.get(vh) if vh in v.vlans else None)
                self._hit(p, r.adjusted(-3, -3, 3, 3), v.trunk_tip)
            elif k == "traffic":
                tr = payload
                idle = (tr.bps or 0) < 1 and not tr.drops
                txt = ("idle" if idle else T.fmt_bps(tr.bps)) + (f" · {tr.drops} drops" if tr.drops else "")
                r = T.draw_pill(p, c, txt, tk, border=tk.card_border if not tr.drops else T.status_fill("warn", tk),
                                text_color=tk.muted, height=13 if idle else 15, bold=not idle,
                                f=T.mono(0.94 if idle else 1.0, "caption"))
                self._hit(p, r, f"{T.fmt_bps(tr.bps)}, {tr.pps:.0f} packets/s"
                                      + ("" if tr.directional else " (both directions combined)"))
            elif k == "subnet":
                text, v = payload
                if ("stack", "subnet") in self.hidden_labels and not (self._hover or sel):
                    y += h + 3
                    continue
                pl = ctx.subnet_pulse.get(self.key)
                pulse = 0.0
                if pl is not None and ctx.now - pl[0] < T.PULSE_S:
                    ph = max(0.0, (ctx.now - pl[0]) / T.PULSE_S)
                    pulse = (1.0 - ph) * (0.55 + 0.45 * math.cos(ph * 3 * 2 * math.pi))
                if v.subnet_stale:
                    p.setOpacity(p.opacity() * 0.55)
                r = T.draw_subnet_label(p, c, text, tk, v.subnet_style, pulse, pl[1] if pl else None)
                if v.subnet_stale:
                    p.setOpacity(p.opacity() / 0.55)
                self._hit(p, r.adjusted(-2, -2, 2, 2), v.subnet_tip)
            elif k == "subnet_hint":
                f7 = T.mono(0.96, "caption")
                tw = T.fm(f7).horizontalAdvance(payload) + 8
                bg = QColor(tk.base)
                bg.setAlpha(225)
                p.setPen(Qt.NoPen)
                p.setBrush(bg)
                p.drawRoundedRect(QRectF(c.x() - tw / 2, c.y() - 6, tw, 12), 4, 4)
                p.setFont(f7)
                p.setPen(tk.muted)
                p.drawText(QRectF(c.x() - 60, c.y() - 6, 120, 12), Qt.AlignCenter, payload)
            y += h + 3
        p.restore()

    # ------------------------------------------------------------------ events
    def hoverEnterEvent(self, ev):
        self.prepareGeometryChange()             # hover reveals suppressed labels (S8)
        self._hover = True
        self.update()
        super().hoverEnterEvent(ev)

    def hoverLeaveEvent(self, ev):
        self.prepareGeometryChange()
        self._hover = False
        self.set_view(self.lv) if self.lv is not None else None
        self.update()
        super().hoverLeaveEvent(ev)

    def hoverMoveEvent(self, ev):
        pos = ev.pos()
        for r, tip in self._hits:
            if r.adjusted(-2, -2, 2, 2).contains(pos):
                self.setToolTip(tip)
                break
        else:
            tip = getattr(getattr(self.lv, "tele", None), "tooltip", "") or ""
            self.setToolTip(tip.replace("\n", "<br>"))
        super().hoverMoveEvent(ev)

    def itemChange(self, change, value):
        if change == QGraphicsItem.ItemSelectedHasChanged:
            self.update()
        return super().itemChange(change, value)


def _rects_overlap(a: tuple, b: tuple) -> bool:
    return a[0] < b[0] + b[2] and b[0] < a[0] + a[2] and a[1] < b[1] + b[3] and b[1] < a[1] + a[3]


def _normal(path: QPainterPath, t: float) -> QPointF:
    a = math.radians(path.angleAtPercent(t))
    return QPointF(math.sin(a), math.cos(a))           # Qt angles are counter-clockwise, y down


def _sub_path(path: QPainterPath, t0: float, t1: float, n: int = 16) -> QPainterPath:
    out = QPainterPath(path.pointAtPercent(t0))
    for i in range(1, n + 1):
        out.lineTo(path.pointAtPercent(t0 + (t1 - t0) * i / n))
    return out


def _offset_path(path: QPainterPath, off: float, trim: float = 0.0, n: int = 24) -> QPainterPath:
    L = path.length()
    if L <= 2 * trim + 2:
        trim = 0.0
    t0, t1 = path.percentAtLength(trim), path.percentAtLength(L - trim)
    pts = []
    for i in range(n + 1):
        t = t0 + (t1 - t0) * i / n
        pts.append(path.pointAtPercent(t) + _normal(path, t) * off)
    out = QPainterPath(pts[0])
    for q in pts[1:]:
        out.lineTo(q)
    return out


# ============================================================================ overlays
class BundleItem(_HitMixin, QGraphicsItem):
    """An EtherChannel on the canvas: a lasso (oval) around the member cables at their midpoint, one bundle
    bead ('Po1 · LACP 2/2', shape = protocol), the ⚠ mismatch bead, the TRUNK bead (tier 2) and - when spanning
    tree blocks the whole Port-channel - ONE hatched 'Po2 · Altn BLK' marker at the blocking end.  Selectable:
    clicking the lasso or the bead shows the EtherChannel view in the Inspector.

    LOD: mismatch / down always (tier 0), the bundle bead from tier 1, the trunk bead and member words at tier 2.
    """

    def __init__(self, key: str, members: list, a: NodeItem, b: NodeItem):
        super().__init__()
        self.key = key
        self.members = list(members)            # LinkItems
        self.a, self.b = a, b
        self.bd = None                          # facts.Bundle
        self._hover = False
        self._hits: list[tuple[QRectF, str]] = []
        self._shape = QPainterPath()
        self.setZValue(3)
        self.setFlags(QGraphicsItem.ItemIsSelectable)
        self.setAcceptHoverEvents(True)

    def ctx(self) -> RenderCtx:
        sc = self.scene()
        return getattr(sc, "ctx", None) or RenderCtx()

    def set_bundle(self, bd, members: list, a: NodeItem, b: NodeItem) -> None:
        self.prepareGeometryChange()
        self.bd, self.members, self.a, self.b = bd, list(members), a, b
        self.setToolTip((bd.tooltip or "").replace("\n", "<br>") if bd is not None else "")
        self.update()

    def adjust(self) -> None:
        self.prepareGeometryChange()
        self.update()

    # ------------------------------------------------------------------ geometry
    def proto(self) -> str:
        bd = self.bd
        side = bd.side_a if bd is not None else None
        return getattr(getattr(side, "proto", None), "value", "lacp") if side is not None else "lacp"

    def state(self) -> str:
        return getattr(getattr(self.bd, "state", None), "value", "unknown") if self.bd is not None else "unknown"

    def geom(self):
        """(centre, along unit, normal unit, half-length along, half-width across, side sign)."""
        mids = [li.path.pointAtPercent(0.5) for li in self.members if li.path.length() > 2]
        if not mids:
            c = (self.a.pos() + self.b.pos()) / 2
            mids = [c]
        cx = sum(m.x() for m in mids) / len(mids)
        cy = sum(m.y() for m in mids) / len(mids)
        c = QPointF(cx, cy)
        d = T.unit(self.b.pos() - self.a.pos())
        n = QPointF(-d.y(), d.x())
        offs = [(m.x() - cx) * n.x() + (m.y() - cy) * n.y() for m in mids]
        half_w = (max(offs) - min(offs)) / 2 + 11.0
        mid_ab = (self.a.pos() + self.b.pos()) / 2
        side = (c.x() - mid_ab.x()) * n.x() + (c.y() - mid_ab.y()) * n.y()
        sign = 1.0 if side >= 0 else -1.0
        return c, d, n, 24.0, half_w, sign

    def lasso_path(self) -> QPainterPath:
        c, d, n, hl, hw, _ = self.geom()
        path = QPainterPath()
        path.addEllipse(QPointF(0, 0), hl, hw)
        tr = QTransform()
        tr.translate(c.x(), c.y())
        tr.rotate(math.degrees(math.atan2(d.y(), d.x())))
        return tr.map(path)

    def end_points(self, node) -> list[QPointF]:
        return [(li._p1 if li.a is node else li._p2) for li in self.members if li.path.length() > 2]

    def boundingRect(self):
        r = self.lasso_path().boundingRect()
        for node in (self.a, self.b):
            for pt in self.end_points(node):
                r = r.united(QRectF(pt.x() - 2, pt.y() - 2, 4, 4))
        return r.adjusted(-110, -70, 110, 70)

    def shape(self):
        path = QPainterPath(self.lasso_path())
        for r, _ in self._hits:
            path.addRect(r)
        return path

    def _stack(self, lod: float, reveal: bool):
        """[(kind, text)] of the bead stack, top (nearest the lasso) to bottom."""
        bd = self.bd
        if bd is None:
            return []
        out = []
        tier1 = lod >= T.LOD_TIER1 or reveal
        tier2 = lod >= T.LOD_TIER2 or reveal
        st = self.state()
        if bd.mismatch_short:                                 # tier 0
            out.append(("warn", f"⚠ {bd.mismatch_short}" if not tier1 else bd.mismatch_short))
        if tier1 or st in ("down", "partial"):                 # down / 1-of-N are alarms: always
            out.append(("bead", bd.label))
        if tier2 and bd.trunk_text and st in ("up", "partial"):
            out.append(("trunk", f"TRUNK {bd.trunk_text}"))
        return out

    # ------------------------------------------------------------------ paint
    def paint(self, p: QPainter, opt, widget=None):
        bd = self.bd
        if bd is None or not self.members:
            return
        p.setRenderHint(QPainter.Antialiasing)
        ctx = self.ctx()
        if not ctx.layers.get("l2", True):
            return
        tk = ctx.tokens
        lod = _lod(p, opt)
        self._begin_hits(p)
        proto, st = self.proto(), self.state()
        sel = self.isSelected()
        reveal = self._hover or sel
        lasso = self.lasso_path()
        c, d, n, hl, hw, sign = self.geom()
        # flash (events) + selection halo
        fl = ctx.flash.get(self.key)
        if fl and ctx.now - fl[0] < 0.8:
            a = 1.0 - (ctx.now - fl[0]) / 0.8
            p.setPen(QPen(T.alpha(fl[1], 0.45 * a), 10))
            p.setBrush(Qt.NoBrush)
            p.drawPath(lasso)
        if sel:
            p.setPen(QPen(T.alpha(tk.accent_ring, 0.45), 7))
            p.setBrush(Qt.NoBrush)
            p.drawPath(lasso)
        elif self._hover:
            p.setPen(QPen(T.alpha(tk.ink, 0.10), 7))
            p.setBrush(Qt.NoBrush)
            p.drawPath(lasso)
        # the lasso: a light veil (groups the members) + the protocol-patterned outline
        gk = glyph_scale(lod)
        p.setPen(Qt.NoPen)
        p.setBrush(T.alpha(T.ec_hue(tk) if st == "up" else T.ec_state_colors(st, tk)[0], 0.10))
        p.drawPath(lasso)
        p.setPen(T.ec_lasso_pen(proto, st, tk, 1.6 * min(gk, 1.6)))
        p.setBrush(Qt.NoBrush)
        p.drawPath(lasso)
        # bracket ticks where the members enter / leave the lasso (reads as 'bound together')
        for sgn in (-1, 1):
            q = c + d * (hl * sgn)
            p.drawLine(q - n * (hw * 0.55), q + n * (hw * 0.55))
        self._hit(p, lasso.boundingRect(), (bd.tooltip or "").replace("\n", "<br>"))
        # STP blocks the whole Port-channel: one marker at the blocking end; member ports listed per end (tier 2)
        b = bd.stp_block
        blk_node = None
        if b is not None and st in ("up", "partial"):
            blk_node = self._paint_blk(p, tk, lod, reveal, b)
        if lod >= T.LOD_TIER2 or reveal:
            for node in (self.a, self.b):
                if node is not blk_node:
                    self._paint_ports(p, tk, lod, node)
        # bead stack outside the lasso (away from the centre line between the two switches)
        items = self._stack(lod, reveal)
        if not items:
            return
        k = text_scale(lod)
        y = 0.0
        heights = {"warn": 16.0, "bead": 14.0, "trunk": 13.0}
        widths = {"warn": lambda t: T.ec_warn_width(t), "bead": lambda t: T.ec_bead_width(t, proto),
                  "trunk": lambda t: T.fm(T.type("micro")).horizontalAdvance(t) + 10}
        total = sum(heights[kd] for kd, _ in items) + 3 * (len(items) - 1)
        maxw = max(widths[kd](t) for kd, t in items)
        # anchor: just outside the lasso, on the outer side; along the normal (vertical-ish link) the stack
        # needs its half height, along a vertical link (normal ~ horizontal) its half width
        reach = abs(n.y()) * total / 2 + abs(n.x()) * maxw / 2
        anchor = c + n * (sign * (hw + 5 + reach * k))
        _scaled(p, anchor, k)
        y = anchor.y() - total / 2
        for kd, text in items:
            h = heights[kd]
            cc = QPointF(anchor.x(), y + h / 2)
            if kd == "warn":
                r = T.draw_ec_warn(p, cc, text, tk)
                self._hit(p, r.adjusted(-2, -2, 2, 2), "<b>EtherChannel mismatch</b><br>" +
                          "<br>".join(html.escape(x) for x in bd.mismatch))
            elif kd == "bead":
                r = T.draw_ec_bead(p, cc, text, tk, proto, st if st != "mismatch" else "down")
                self._hit(p, r.adjusted(-3, -3, 3, 3), (bd.tooltip or "").replace("\n", "<br>")
                          + "<br><i>click to open the EtherChannel view</i>")
            else:
                vh = ctx.vlan_hl if ctx.layers.get("vlan", False) else None
                r = T.draw_trunk_bead(p, cc, text, tk, hue=ctx.vlan_hues.get(vh) if vh in (bd.vlans or []) else None)
                self._hit(p, r.adjusted(-3, -3, 3, 3), f"802.1Q trunk on {html.escape(bd.po_text)}: VLAN "
                          f"{html.escape(bd.trunk_text or '')}" + (f", native {bd.native_vlan}"
                                                                   if bd.native_vlan is not None else ""))
            y += h + 3
        p.restore()

    def _axis_point(self, node, dist: float) -> tuple[QPointF, QPointF]:
        """(point, direction into the bundle) at ``dist`` along the members from ``node``'s end (their mean)."""
        pts, dirs = [], []
        for li in self.members:
            L = li.path.length()
            if L < 2:
                continue
            at_start = li.a is node
            dd = min(dist, L * 0.45)
            t = li.path.percentAtLength(dd if at_start else L - dd)
            pts.append(li.path.pointAtPercent(t))
            t2 = li.path.percentAtLength(min(L, dd + 4) if at_start else max(0.0, L - dd - 4))
            dirs.append(li.path.pointAtPercent(t2) - pts[-1])
        if not pts:
            return node.pos(), QPointF(1, 0)
        c = QPointF(sum(q.x() for q in pts) / len(pts), sum(q.y() for q in pts) / len(pts))
        dv = QPointF(sum(q.x() for q in dirs), sum(q.y() for q in dirs))
        return c, T.unit(dv)

    def _end_pill_center(self, node, width: float, k: float) -> QPointF:
        ends = self.end_points(node)
        d0 = T.unit((sum((e for e in ends[1:]), ends[0]) / len(ends)) - node.pos()) if ends else QPointF(1, 0)
        dist = max(26.0 * k, (width / 2 * abs(d0.x()) + 9) * k) + 4
        return self._axis_point(node, dist)[0]

    def _paint_ports(self, p, tk, lod: float, node) -> None:
        """Tier 2: the member ports of one end as ONE pill ('Gi0/1 · Gi0/2'); hover a cable for its own."""
        ports = [(li.label_a if li.a is node else li.label_b) for li in self.members]
        ports = [x for x in ports if x]
        if not ports or node.kind == "cloud":
            return
        side = self.bd.side(node.key) if self.bd is not None else None
        text = " · ".join(ports)
        f = iface_font()
        k = text_scale(lod)
        w = T.fm(f).horizontalAdvance(text) + 12
        pc = self._end_pill_center(node, w, k)
        _scaled(p, pc, k)
        r = T.draw_pill(p, pc, text, tk, border=tk.card_border, text_color=tk.muted, height=15, pad=5, f=f)
        tip = (f"{html.escape(node.label)}: {html.escape(text)} → {html.escape(side.po)} "
               f"({html.escape(side.flag_words)})" if side is not None and side.po else
               f"{html.escape(node.label)}: {html.escape(text)} (no channel-group)")
        self._hit(p, r, tip)
        p.restore()

    def _paint_blk(self, p, tk, lod: float, reveal: bool, b):
        node = self.a if b.node == self.a.key else (self.b if b.node == self.b.key else None)
        if node is None:
            return None
        ends = self.end_points(node)
        if not ends:
            return None
        ex = sum(e.x() for e in ends) / len(ends)
        ey = sum(e.y() for e in ends) / len(ends)
        ept = QPointF(ex, ey)
        out = T.unit(ept - node.pos())
        gk = glyph_scale(lod)
        from ..parsers.ios_l2 import vlan_text
        tip = (f"<b>Spanning tree blocks the whole {html.escape(b.port)}</b> ({b.role_word} port on "
               f"{html.escape(node.label)}, VLAN {vlan_text(b.vlans)}).<br>Every member cable is up and bundled: STP "
               "sees the Port-channel as ONE port and keeps it as the backup path; it takes over if the other "
               "path fails.")
        # one slashed amber cap for the bundle, centred on the members' ends
        cap = ept + out * (7.0 * gk)
        _scaled(p, cap, gk)
        p.setPen(QPen(tk.base, 1.5))
        p.setBrush(QColor(T.STP_BLOCK))
        p.drawEllipse(cap, 7.5, 7.5)
        if not tk.dark:
            p.setPen(QPen(T.status_text("warn", tk), 1.0))
            p.setBrush(Qt.NoBrush)
            p.drawEllipse(cap, 7.0, 7.0)
        p.setBrush(QBrush(QColor(255, 255, 255, 120), Qt.BDiagPattern))
        p.setPen(Qt.NoPen)
        p.drawEllipse(cap, 6.0, 6.0)
        T.draw_icon(p, "slash", QRectF(cap.x() - 5, cap.y() - 5, 10, 10), QColor("white"), 2.0)
        self._hit(p, QRectF(cap.x() - 8, cap.y() - 8, 16, 16), tip)
        p.restore()
        if not (lod >= T.LOD_TIER1 or reveal):
            return node
        k = text_scale(lod)
        label = f"{b.port} · {_ev(b.role)} BLK" if not b.partial else f"{b.port} · BLK v{vlan_text(b.vlans)}"
        f = iface_font(True)
        w = T.fm(f).horizontalAdvance(label) + 12
        _c, _d, n, _hl, _hw, sign = self.geom()
        pc = self._end_pill_center(node, w, k) + n * (sign * 15.0 * k)     # outer side: clear of the members
        _scaled(p, pc, k)
        r = T.draw_pill(p, pc, label, tk, border=QColor(T.STP_BLOCK), text_color=T.status_text("warn", tk)
                        if not tk.dark else QColor("#FDBA74"), height=15, pad=5, fill=T.alpha(T.STP_BLOCK, 0.12),
                        hatch=T.STP_BLOCK, f=f)
        self._hit(p, r, tip)
        p.restore()
        return node

    # ------------------------------------------------------------------ events
    def hoverEnterEvent(self, ev):
        self.prepareGeometryChange()
        self._hover = True
        self.update()
        super().hoverEnterEvent(ev)

    def hoverLeaveEvent(self, ev):
        self.prepareGeometryChange()
        self._hover = False
        self.update()
        super().hoverLeaveEvent(ev)

    def hoverMoveEvent(self, ev):
        pos = ev.pos()
        for r, tip in self._hits:
            if r.adjusted(-2, -2, 2, 2).contains(pos):
                self.setToolTip(tip)
                break
        super().hoverMoveEvent(ev)

    def itemChange(self, change, value):
        if change == QGraphicsItem.ItemSelectedHasChanged:
            self.update()
        return super().itemChange(change, value)


class AdjacencyOverlay(QGraphicsItem):
    """V4 optional layer: chords between router tiles through an unmanaged switch (multi-access
    segments) and dashed purple arcs for BGP sessions between routers without a shared link."""

    def __init__(self):
        super().__init__()
        self.setZValue(0.5)
        self.chords: list[tuple[str, str, str, str, str | None]] = []   # (a, b, proto, state, via switch)
        self.arcs: list[tuple[str, str, int, str]] = []                 # (a, b, count, state)
        self.setAcceptedMouseButtons(Qt.NoButton)

    def set_data(self, chords, arcs):
        self.prepareGeometryChange()
        self.chords, self.arcs = chords, arcs
        self.update()

    def boundingRect(self):
        sc = self.scene()
        return sc.sceneRect() if sc else QRectF()

    def paint(self, p: QPainter, opt, widget=None):
        sc = self.scene()
        if sc is None:
            return
        ctx: RenderCtx = sc.ctx
        nodes = sc.nodes
        p.setRenderHint(QPainter.Antialiasing)
        for i, (a, b, proto, state, via) in enumerate(self.chords):
            if not ctx.layers.get(proto.lower(), False) or a not in nodes or b not in nodes:
                continue
            pa, pb = nodes[a].pos(), nodes[b].pos()
            ctrl = nodes[via].pos() if via in nodes else (pa + pb) / 2
            off = (i % 3 - 1) * 8
            path = QPainterPath(pa)
            path.quadTo(ctrl + QPointF(off, off), pb)
            pen = T.rail_pen(proto, T.adjacency_state(state), ctx.tokens)
            pen.setWidthF(1.6)
            p.setPen(pen)
            p.setBrush(Qt.NoBrush)
            p.drawPath(path)
        for a, b, count, state in self.arcs:
            if not ctx.layers.get("bgp", False) or a not in nodes or b not in nodes:
                continue
            pa, pb = nodes[a].pos(), nodes[b].pos()
            v = pb - pa
            ln = math.hypot(v.x(), v.y()) or 1
            n = QPointF(-v.y() / ln, v.x() / ln)
            ctrl = (pa + pb) / 2 + n * max(60.0, ln * 0.35)
            path = QPainterPath(pa)
            path.quadTo(ctrl, pb)
            pen = QPen(QColor(T.PROTO_HUES["BGP"]), 1.8, Qt.DashLine, Qt.RoundCap)
            p.setPen(pen)
            p.drawPath(path)
            if count > 1:
                T.draw_pill(p, path.pointAtPercent(0.5), f"BGP ×{count}", ctx.tokens,
                            border=QColor(T.PROTO_HUES["BGP"]), text_color=QColor(T.PROTO_HUES["BGP"]), pt=7)


DROP_AT = 0.3          # a dropped packet stops 30 % into the failed link (clear of its down pill)


class PacketItem(QGraphicsItem):
    """'Simulate the connection': a glowing packet that travels hop by hop along real links
    (traceroute hops / the default path), each hop labelled with what the router answered."""

    HOP_S = 0.55

    def __init__(self, legs: list[tuple], color: str = T.ROUTE_BLUE, on_done=None):
        super().__init__()
        # (link item, reverse, caption at arrival[, fail]) - a failing leg stops half way with a red ✕
        self.legs = [tuple(l) + (False,) * (4 - len(l)) for l in legs]
        self.color = color
        self.t0 = time.monotonic()
        self.on_done = on_done
        self.done = False
        self.trail: list[tuple[QPointF, str]] = []
        self.setZValue(40)
        self.setAcceptedMouseButtons(Qt.NoButton)

    def boundingRect(self):
        sc = self.scene()
        return sc.sceneRect() if sc else QRectF()

    def progress(self, now: float) -> float:
        return (now - self.t0) / self.HOP_S

    def tick(self, now: float) -> None:
        if not self.done and self.progress(now) >= len(self.legs) + 2.5:
            self.done = True
        self.update()

    def paint(self, p: QPainter, opt, widget=None):
        sc = self.scene()
        tk = sc.ctx.tokens if sc is not None else T.tokens()
        now = time.monotonic()
        prog = self.progress(now)
        p.setRenderHint(QPainter.Antialiasing)
        fade = 1.0 if prog < len(self.legs) + 1.5 else max(0.0, 1.0 - (prog - len(self.legs) - 1.5))
        p.setOpacity(fade)
        n = min(int(prog), len(self.legs))
        for i in range(n):                     # legs already travelled: keep a faint trace + captions
            li, rev, cap, fail = self.legs[i]
            path = li.path if not fail else (_sub_path(li.path, 1 - DROP_AT, 1.0) if rev else _sub_path(li.path, 0.0, DROP_AT))
            p.setPen(QPen(T.alpha(self.color, 0.35), 6, Qt.SolidLine, Qt.RoundCap))
            p.drawPath(path)
            end = li.path.pointAtPercent((1 - DROP_AT if rev else DROP_AT) if fail else (0.0 if rev else 1.0))
            dest = li.a if rev else li.b
            cw = QFontMetricsF(T.font(7.4, True)).horizontalAdvance(cap or "") + 12
            if fail:                                   # beside the ✕, away from the end pills
                cap_at = end + QPointF(cw / 2 + 14, 0)
            else:                                      # the hop's router: caption at its top-left corner
                cap_at = dest.pos() + QPointF(-dest.SIZE / 2 - cw / 2 - 4, -dest.SIZE / 2 + 2)
            if fail:
                p.setPen(QPen(QColor("white"), 2))
                p.setBrush(QColor(T.DOWN_RED))
                p.drawEllipse(end, 9, 9)
                T.draw_icon(p, "cross", QRectF(end.x() - 5, end.y() - 5, 10, 10), QColor("white"), 2.0)
            if cap:
                k = text_scale(_lod(p, None))
                cap_at = end + (cap_at - end) * k if fail else dest.pos() + (cap_at - dest.pos()) * k
                _scaled(p, cap_at, k)
                T.draw_pill(p, cap_at, cap, tk, border=QColor(T.DOWN_RED if fail else self.color),
                            text_color=QColor(T.DOWN_RED) if fail else tk.ink, pt=7.4, bold=True, height=17,
                            fill=T.alpha(T.DOWN_RED, 0.10) if fail else None)
                p.restore()
        if n < len(self.legs):
            li, rev, _, fail = self.legs[n]
            t = prog - n
            t = t * t * (3 - 2 * t)            # ease
            if fail:
                t *= DROP_AT
            pct = 1.0 - t if rev else t
            pt = li.path.pointAtPercent(max(0.0, min(1.0, pct)))
            for r, a in ((13, 0.18), (8, 0.35)):
                p.setPen(Qt.NoPen)
                p.setBrush(T.alpha(self.color, a))
                p.drawEllipse(pt, r, r)
            p.setBrush(QColor(self.color))
            p.setPen(QPen(QColor("white"), 2))
            p.drawEllipse(pt, 5.5, 5.5)
