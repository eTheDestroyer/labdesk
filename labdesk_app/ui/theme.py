"""LabDesk design system: colour tokens, type scale, spacing, status glyphs and the canvas paint helpers.

Everything visual is defined here once, so the canvas, the legend, the inspector, the log and the
dialogs share one vocabulary (S1-S9):

* **Neutrals** ``N1..N12`` (light / dark), an indigo **accent** (fill / ring / text / subtle) and the
  canvas surface.  ``tokens()`` returns the current theme's :class:`Tokens`.
* **Status pairs** (S2): every state has a *fill* (marks, strokes) and a *text* value (words), per theme;
  the text value reaches 4.5:1 on N1-N3 (``tests/test_design_system.py`` checks it).
* **One glyph helper** ``status_glyph(state, size)``: filled circle = up, single bar = down, hollow ring =
  protocol down, filled square = admin down / neutral; check / X / spinner arc for node badges.  No font
  glyphs, no arrows.
* **Type** (S4): ``type(role)`` / ``mono(scale)`` - roles derived from the system font size B; fonts are
  built once per theme / font change and cached (never created in ``paint()``).
* **LOD tiers** (S8): ``LOD_TIER1`` / ``LOD_TIER2`` / ``LOD_GRID``.

Icons are drawn as vector paths, never as font glyphs, so they look identical on every host.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import (QBrush, QColor, QFont, QFontDatabase, QFontMetricsF, QPainter, QPainterPath,
                           QPainterPathStroker, QPalette, QPen)

# ============================================================================ neutral + accent scales (S1)
# Radix "slate" (cool neutral) - N1 app background ... N12 high-contrast text
N_LIGHT = ("#FCFCFD", "#F9F9FB", "#F0F0F3", "#E8E8EC", "#E0E1E6", "#D9D9E0",
           "#CDCED6", "#B9BBC6", "#8B8D98", "#80838D", "#60646C", "#1C2024")
N_DARK = ("#111113", "#18191B", "#212225", "#272A2D", "#2E3135", "#363A3F",
          "#43484E", "#5A6169", "#696E77", "#777B84", "#B0B4BA", "#EDEEF0")
CANVAS_LIGHT, CANVAS_DARK = "#F4F5F7", "#141517"
# indigo/iris accent: never blue-on-canvas (blue = default route), never green/red (status)
ACCENT_LIGHT = {"fill": "#5B5BD6", "ring": "#5B5BD6", "text": "#4F4BC2", "subtle": "#ECEDFE", "hover": "#5151CD"}
ACCENT_DARK = {"fill": "#5B5BD6", "ring": "#8481EA", "text": "#B1A9FF", "subtle": "#25264F", "hover": "#6E6ADE"}

RADIUS_SM, RADIUS_MD, RADIUS_LG = 6, 10, 14
SPACE = (0, 4, 8, 12, 16, 24, 32)          # S5 spacing scale: SPACE[1] = 4 px ... SPACE[5] = 24 px

# ============================================================================ status pairs (S2)
# state: (fill light, fill dark, text light, text dark)
STATUS = {
    "up": ("#1F9D63", "#3DD68C", "#18794E", "#4CC38A"),
    "down": ("#E5484D", "#FF6369", "#CD2B31", "#FF6369"),
    "proto": ("#E5641A", "#FF8B3E", "#B84900", "#FF8B3E"),   # S2 #BD4B00 was 4.43:1 on N3 -> #B84900
    "warn": ("#E3A008", "#FFCA16", "#8A6100", "#F0C000"),
}

# ============================================================================ LOD tiers (S8)
LOD_TIER1 = 0.6          # one bead per link + protocol chips + 12 px node badge
LOD_TIER2 = 0.9          # subnet tags, interface names, secondary chips (or when hovered / selected)
LOD_GRID = 0.5           # dot grid hidden below
MIN_TEXT_PX = 7.0        # text smaller than this on screen is not drawn (tier-0 shapes take over)

# ----------------------------------------------------------------------------- fixed hues
ROUTE_BLUE = "#2F7BFF"           # V6: the default route to the internet - never used for anything else
LINK_UP = STATUS["up"][0]
DOWN_RED = STATUS["down"][0]
PROTO_ORANGE = STATUS["proto"][0]  # line protocol down
AMBER = STATUS["warn"][0]
NEUTRAL = "#8B8D98"              # = N9 light
NEUTRAL_LIGHT = "#B9BBC6"        # = N8 light

# protocol hues (V4): fixed per protocol, never red/green/route-blue so they cannot read as
# "down", "up" or "default route"
PROTO_HUES = {
    "OSPF": "#0E9F9A",           # teal
    "BGP": "#8B5CF6",            # purple (BGP loopback arcs are purple too)
    "HSRP": "#C77D0A",           # ochre
    "VRRP": "#D9468F",           # rose
    "DEFAULT": "#64748B",        # route-source chip: neutral outline
    # switching (managed switches)
    "L2": "#0B5F86",             # Cisco-blue family: the switch's own chips
    "L3": "#0B5F86",
    "VLAN": "#7E22CE",
    "STP": "#B45309",            # spanning tree = amber/brown family (the BLK marker is amber too)
    "ROOT": "#A16207",
}
STP_BLOCK = "#D97706"            # STP-blocked end cap (hatched, labelled - never colour-only)
EC_HUE = ("#0A4D6E", "#7CC8EC")  # EtherChannel bead / lasso (light, dark): the trunk bead's Cisco-blue family
ROOT_GOLD = "#F5B800"            # root-bridge crown badge fill
# VLAN hues (V10 chips, VLAN colouring layer); assigned in VLAN-id order per lab, never red/green/route-blue
VLAN_PALETTE = ["#C026D3", "#0891B2", "#CA8A04", "#7C3AED", "#DB2777", "#0F766E", "#9F1239", "#4338CA"]


def vlan_hues(vlans) -> dict[int, str]:
    """{vlan id: hue} - stable for a given set of lab VLANs (sorted ids walk the palette)."""
    return {v: VLAN_PALETTE[i % len(VLAN_PALETTE)] for i, v in enumerate(sorted(set(vlans)))}


SUBNET_HUE = "#56657A"           # subnet labels on cables + SUBNET events: slate (never blue/red/green)
PULSE_S = 2.4                    # a changed subnet label pulses this long

EVENT_HUES = {"link": DOWN_RED, "ospf": PROTO_HUES["OSPF"], "bgp": PROTO_HUES["BGP"],
              "hsrp": PROTO_HUES["HSRP"], "vrrp": PROTO_HUES["VRRP"], "default": ROUTE_BLUE,
              "sim": "#6D28D9", "probe": NEUTRAL, "cable": DOWN_RED,
              "stp": PROTO_HUES["STP"], "vlan": PROTO_HUES["VLAN"], "trunk": PROTO_HUES["L2"],
              "subnet": SUBNET_HUE, "ec": EC_HUE[0]}
# text colours per log severity; re-tinted for the active theme by set_current() (warn = WARN.text, S2)
SEVERITY_HUES = {"ok": STATUS["up"][2], "info": None, "warn": STATUS["warn"][2], "error": STATUS["down"][2]}

# vendor tiles (V8): tinted gradients + plain-text strip; no logo lookalikes
VENDOR_COLORS = {
    "cisco": ("#22A6DC", "#0B5F86", "IOSv"),
    "juniper": ("#7FB03A", "#3F6B14", "Junos"),
    "fortinet": ("#EE4B3B", "#9A1B10", "FortiOS"),
    "linux": ("#64748B", "#334155", "Linux"),
    "generic": ("#8A94A0", "#525C68", ""),
}

STATE_COLORS = {
    "BOOTED": LINK_UP, "running": LINK_UP,
    "STARTED": NEUTRAL, "QUEUED": NEUTRAL, "booting": NEUTRAL, "stopping": NEUTRAL,   # S2: no third orange
    "STOPPED": NEUTRAL, "stopped": NEUTRAL,
    "DEFINED_ON_CORE": NEUTRAL_LIGHT, "defined": NEUTRAL_LIGHT,
    "DISCONNECTED": DOWN_RED, "missing": DOWN_RED, "unknown": DOWN_RED,
}
STATE_TEXT = {
    "BOOTED": "booted", "STARTED": "booting", "QUEUED": "queued", "STOPPED": "stopped",
    "DEFINED_ON_CORE": "wiped", "DISCONNECTED": "disconnected", "running": "running",
    "stopped": "stopped", "missing": "not created", "booting": "booting", "unknown": "unknown",
}
PULSING_STATES = {"STARTED", "QUEUED", "booting"}
# node state -> status_glyph state of its badge
STATE_GLYPH = {"BOOTED": "booted", "running": "booted", "STARTED": "booting", "QUEUED": "booting",
               "booting": "booting", "stopping": "booting", "STOPPED": "stopped", "stopped": "stopped",
               "DEFINED_ON_CORE": "stopped", "defined": "stopped", "DISCONNECTED": "disconnected",
               "missing": "disconnected", "unknown": "disconnected"}

_STATE_LABELS = {"defined_on_core": "Defined", "started": "Running", "booted": "Running", "stopped": "Stopped",
                 "queued": "Queued", "disconnected": "Disconnected", "running": "Running", "defined": "Defined",
                 "missing": "Not created", "unknown": "Unknown", "booting": "Booting"}


def state_label(raw: str | None) -> str:
    """Raw CML state -> a word for people ('DEFINED_ON_CORE' -> 'Defined')."""
    if not raw:
        return "Unknown"
    return _STATE_LABELS.get(str(raw).lower(), str(raw).replace("_", " ").capitalize())


# ============================================================================ tokens
@dataclass(frozen=True)
class Tokens:
    """Theme colours.  The first seven fields are the original (pre-S1) API and stay valid."""
    dark: bool
    base: QColor          # canvas background
    ink: QColor           # primary text (N12)
    muted: QColor         # secondary text (N11)
    faint: QColor         # grid dots, hairlines (N6)
    card: QColor          # pill / chip background (N1 light, N3 dark)
    card_border: QColor   # N6 light / N7 dark
    shadow: QColor
    n: tuple = ()                       # N1..N12 as QColor (index 1..12; n[0] = N1)
    accent_fill: QColor = field(default_factory=lambda: QColor(ACCENT_LIGHT["fill"]))
    accent_ring: QColor = field(default_factory=lambda: QColor(ACCENT_LIGHT["ring"]))
    accent_text: QColor = field(default_factory=lambda: QColor(ACCENT_LIGHT["text"]))
    accent_subtle: QColor = field(default_factory=lambda: QColor(ACCENT_LIGHT["subtle"]))
    window: QColor = field(default_factory=lambda: QColor(N_LIGHT[1]))
    mode: str = "light"                 # auto resolves to light/dark; 'kde' keeps the system palette

    def N(self, i: int) -> QColor:
        return QColor(self.n[i - 1]) if self.n else QColor((N_DARK if self.dark else N_LIGHT)[i - 1])

    def fill(self, state: str) -> QColor:
        return status_fill(state, self)

    def text(self, state: str) -> QColor:
        return status_text(state, self)


def build_tokens(dark: bool, palette: QPalette | None = None, mode: str | None = None) -> Tokens:
    """Tokens for the light or dark table; ``palette`` (KDE colours mode) supplies window/canvas/accent."""
    scale = N_DARK if dark else N_LIGHT
    n = tuple(QColor(c) for c in scale)
    acc = ACCENT_DARK if dark else ACCENT_LIGHT
    canvas = QColor(CANVAS_DARK if dark else CANVAS_LIGHT)
    window = QColor(scale[1])
    accent = {k: QColor(v) for k, v in acc.items()}
    if palette is not None:
        canvas = QColor(palette.base().color())
        if dark:
            canvas = canvas.lighter(104)
        window = QColor(palette.window().color())
        hl = QColor(palette.highlight().color())
        if hl.isValid() and contrast(hl, n[2]) >= 3.0:
            accent["fill"] = hl
            accent["ring"] = hl
            accent["subtle"] = _mix(hl, n[0], 0.86)
            accent["text"] = hl if contrast(hl, n[0]) >= 4.5 else (hl.lighter(140) if dark else hl.darker(130))
    card = n[2] if dark else n[0]
    border = n[6] if dark else n[5]
    shadow = QColor(0, 0, 0, 120) if dark else QColor(16, 24, 40, 34)
    return Tokens(dark, canvas, n[11], n[10], n[5], card, border, shadow, n,
                  accent["fill"], accent["ring"], accent["text"], accent["subtle"], window,
                  mode or ("dark" if dark else "light"))


_CURRENT: Tokens | None = None
_GEN = 0                 # bumped on every theme / font change: invalidates font + pixmap caches


def set_current(tk: Tokens | None) -> None:
    """Install the active theme (ThemeController) and re-tint the module-level hue dicts."""
    global _CURRENT, _GEN
    _CURRENT = tk
    _GEN += 1
    _FONT_CACHE.clear()
    dark = bool(tk and tk.dark)
    i = 3 if dark else 2
    SEVERITY_HUES.update(ok=STATUS["up"][i], warn=STATUS["warn"][i], error=STATUS["down"][i])


def generation() -> int:
    return _GEN


def tokens(palette: QPalette | None = None) -> Tokens:
    """The active theme's tokens (set by the ThemeController); before that, light/dark from ``palette``."""
    if _CURRENT is not None:
        return _CURRENT
    if palette is None:
        return build_tokens(False)
    return build_tokens(palette.base().color().lightness() < 128)


def current() -> Tokens:
    return tokens()


# ----------------------------------------------------------------------------- status helpers
_FILL_MAP = {LINK_UP.upper(): "up", "#2EB872": "up", DOWN_RED.upper(): "down", PROTO_ORANGE.upper(): "proto",
             "#F07A2B": "proto", AMBER.upper(): "warn"}


def status_fill(state: str, tk: Tokens | None = None) -> QColor:
    tk = tk or tokens()
    if state in STATUS:
        return QColor(STATUS[state][1 if tk.dark else 0])
    return tk.N(9)


def status_text(state: str, tk: Tokens | None = None) -> QColor:
    tk = tk or tokens()
    if state in STATUS:
        return QColor(STATUS[state][3 if tk.dark else 2])
    return tk.N(11)


def resolve(color, tk: Tokens | None = None) -> QColor:
    """A fixed status hue (LINK_UP / DOWN_RED / PROTO_ORANGE / AMBER / NEUTRAL) -> this theme's fill."""
    c = QColor(color)
    key = c.name().upper()
    if key in _FILL_MAP:
        out = status_fill(_FILL_MAP[key], tk)
    elif key in (NEUTRAL.upper(), "#8A94A0"):
        out = (tk or tokens()).N(9)
    elif key in (NEUTRAL_LIGHT.upper(), "#B8C0CA"):
        out = (tk or tokens()).N(8)
    else:
        return c
    out.setAlpha(c.alpha())
    return out


def text_for(color, tk: Tokens | None = None) -> QColor:
    """A status hue used for words -> the matching ≥4.5:1 text token (amber is never text on light)."""
    key = QColor(color).name().upper()
    if key in _FILL_MAP:
        return status_text(_FILL_MAP[key], tk)
    if key in (NEUTRAL.upper(), "#8A94A0", NEUTRAL_LIGHT.upper(), "#B8C0CA"):
        return (tk or tokens()).N(11)
    tk = tk or tokens()
    c = QColor(color)
    return c.lighter(140) if tk.dark else c.darker(135)


def text_hex(color, tk: Tokens | None = None) -> str:
    return text_for(color, tk).name()


# ----------------------------------------------------------------------------- contrast (S1 validator)
def _lin(v: float) -> float:
    v /= 255.0
    return v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4


def luminance(c) -> float:
    c = QColor(c)
    return 0.2126 * _lin(c.red()) + 0.7152 * _lin(c.green()) + 0.0722 * _lin(c.blue())


def contrast(a, b) -> float:
    la, lb = luminance(a), luminance(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def _mix(a: QColor, b: QColor, t: float) -> QColor:
    """a * (1 - t) + b * t"""
    return QColor(round(a.red() * (1 - t) + b.red() * t), round(a.green() * (1 - t) + b.green() * t),
                  round(a.blue() * (1 - t) + b.blue() * t))


def alpha(color: str | QColor, a: float) -> QColor:
    c = QColor(color)
    c.setAlphaF(max(0.0, min(1.0, a)))
    return c


# ============================================================================ typography (S4)
TYPE_ROLES = {               # role: (scale of B, weight)
    "micro": (0.70, 600),    # trunk / P2P beads only (floor 7 pt)
    "caption": (0.75, 600),  # chips, pills, legend rows, section headers
    "small": (0.85, 400),    # tables, log, state lines, widget captions
    "body": (1.00, 400),
    "label": (1.00, 600),    # tile names, toolbar lab name
    "title": (1.30, 600),    # inspector header, dialog titles
    "display": (1.50, 600),  # About box, empty state
}
_FONT_CACHE: dict = {}
_PINNED: dict = {}           # QA: {'family':, 'mono':, 'pt':} for deterministic shots


def pin_fonts(family: str | None = None, mono_family: str | None = None, pt: float | None = None) -> None:
    """QA only: pin the families / base size (the app itself follows the system font)."""
    _PINNED.update({k: v for k, v in (("family", family), ("mono", mono_family), ("pt", pt)) if v})
    _FONT_CACHE.clear()


def base_font() -> QFont:
    if "family" in _PINNED:
        f = QFont(_PINNED["family"])
        f.setPointSizeF(_PINNED.get("pt", 10.0))
        return f
    return QFontDatabase.systemFont(QFontDatabase.GeneralFont)


def base_pt() -> float:
    """B: the system GeneralFont size as the user set it."""
    if "pt" in _PINNED:
        return float(_PINNED["pt"])
    f = base_font()
    return f.pointSizeF() if f.pointSizeF() > 0 else 10.0


def _weight(w: int):
    return {400: QFont.Normal, 500: QFont.Medium, 600: QFont.DemiBold, 700: QFont.Bold}.get(w, QFont.Normal)


def type(role: str = "body", *, scale: float = 1.0, italic: bool = False, upper: bool = False,  # noqa: A001
         weight: int | None = None) -> QFont:
    """The cached QFont of a type role (copy before mutating)."""
    key = ("t", role, round(scale, 3), italic, upper, weight)
    f = _FONT_CACHE.get(key)
    if f is None:
        k, w = TYPE_ROLES.get(role, TYPE_ROLES["body"])
        f = QFont(base_font())
        pt = base_pt() * k * scale
        if role == "micro":
            pt = max(7.0, pt)
        f.setPointSizeF(pt)
        f.setWeight(_weight(weight or w))
        f.setItalic(italic)
        if upper:
            f.setCapitalization(QFont.AllUppercase)
            f.setLetterSpacing(QFont.PercentageSpacing, 106)
        _FONT_CACHE[key] = f
    return f


def mono(scale: float = 1.0, role: str = "body", weight: int | None = None) -> QFont:
    """Monospace for IPs, prefixes, interfaces, MACs, configs and timestamps (system FixedFont family)."""
    key = ("m", round(scale, 3), role, weight)
    f = _FONT_CACHE.get(key)
    if f is None:
        k, w = TYPE_ROLES.get(role, TYPE_ROLES["body"])
        if "mono" in _PINNED:
            f = QFont(_PINNED["mono"])
        else:
            f = QFont(QFontDatabase.systemFont(QFontDatabase.FixedFont))
        f.setStyleHint(QFont.Monospace)
        f.setPointSizeF(base_pt() * k * scale)
        f.setWeight(_weight(weight or (w if role in ("caption", "micro") else 400)))
        _FONT_CACHE[key] = f
    return f


CONSOLE_FONT_DIR = Path(__file__).resolve().parent / "fonts"
CONSOLE_FONT_FILES = ("Hack-Regular.ttf", "Hack-Bold.ttf")
CONSOLE_PT, CONSOLE_PT_MIN, CONSOLE_PT_MAX = 11, 8, 20
_CONSOLE_FONTS: list = []    # registration result (once per process)


def console_font_registered() -> bool:
    """Register the bundled Hack Regular / Bold once.  True when both files were loaded."""
    if not _CONSOLE_FONTS:
        ok = True
        for name in CONSOLE_FONT_FILES:
            f = CONSOLE_FONT_DIR / name
            ok = ok and f.exists() and QFontDatabase.addApplicationFont(str(f)) >= 0
        _CONSOLE_FONTS.append(ok)
    return _CONSOLE_FONTS[0]


def console_font(pt: float, bold: bool = False) -> QFont:
    """The LabDesk console font: bundled Hack (then DejaVu Sans Mono, then the system fixed font), full
    hinting, no kerning.  The only place that sets the console text size."""
    key = ("c", round(float(pt), 2), bool(bold))
    f = _FONT_CACHE.get(key)
    if f is None:
        console_font_registered()
        f = QFont()
        f.setFamilies(["Hack", "DejaVu Sans Mono", QFontDatabase.systemFont(QFontDatabase.FixedFont).family()])
        f.setStyleHint(QFont.Monospace)
        f.setFixedPitch(True)
        f.setPointSizeF(float(pt))
        f.setWeight(QFont.Bold if bold else QFont.Normal)
        f.setHintingPreference(QFont.PreferFullHinting)
        f.setKerning(False)
        _FONT_CACHE[key] = f
    return f


def tabular(role: str = "body", scale: float = 1.0) -> QFont:
    """Tabular figures for counters (CML 4/5, ages); falls back to mono when the family lacks 'tnum'."""
    key = ("tn", role, round(scale, 3))
    f = _FONT_CACHE.get(key)
    if f is None:
        f = QFont(type(role, scale=scale))
        try:
            from PySide6.QtGui import QFont as _QF
            f.setFeature(_QF.Tag("tnum"), 1)
            fm = QFontMetricsF(f)
            if abs(fm.horizontalAdvance("1111") - fm.horizontalAdvance("0000")) > 0.5:
                f = QFont(mono(scale, role))
        except Exception:  # noqa: BLE001  (Qt < 6.7 has no OpenType feature API)
            f = QFont(mono(scale, role))
        _FONT_CACHE[key] = f
    return f


def font(pt: float, bold: bool = False, family: str | None = None) -> QFont:
    """Legacy canvas helper: a cached font of ``pt`` points scaled to the user's B (pt is relative to 10)."""
    key = ("f", round(pt, 2), bold, family)
    f = _FONT_CACHE.get(key)
    if f is None:
        f = QFont(family) if family else QFont(base_font())
        f.setPointSizeF(max(1.0, pt * base_pt() / 10.0))
        f.setWeight(QFont.DemiBold if bold else QFont.Normal)
        _FONT_CACHE[key] = f
    return f


def fm(f: QFont) -> QFontMetricsF:
    key = ("fm", id(f), f.key())
    m = _FONT_CACHE.get(key)
    if m is None:
        m = _FONT_CACHE[key] = QFontMetricsF(f)
    return m


# ============================================================================ status glyphs (S2)
LINK_END_GLYPH = {"UP": "up", "DOWN": "down", "PROTO_DOWN": "proto_down", "ADMIN_DOWN": "admin_down",
                  "UNKNOWN": "neutral", "NOT_APPLICABLE": None}
_GLYPH_CACHE: dict = {}


def _stroked(path: QPainterPath, width: float, cap=Qt.RoundCap) -> QPainterPath:
    s = QPainterPathStroker()
    s.setWidth(width)
    s.setCapStyle(cap)
    s.setJoinStyle(Qt.RoundJoin)
    return s.createStroke(path).simplified()


def status_glyph(state: str, size: float) -> QPainterPath:
    """One filled QPainterPath centred on (0, 0) per state - canvas, legend, pills, log, notifications.

    Link ends use exactly four silhouettes: ``up`` filled circle, ``down`` a single bar (perpendicular to
    the link once rotated), ``proto_down`` hollow ring (stroke 0.2·size), ``admin_down`` / ``neutral`` filled
    square.  Node badges: ``booted`` check, ``disconnected`` / ``x`` an X (never on link ends), ``booting``
    a spinner arc, ``stopped`` a square.  ``warn`` triangle, ``info`` small dot, ``blocking`` slashed disc.
    """
    key = (state, round(size, 3))
    hit = _GLYPH_CACHE.get(key)
    if hit is not None:
        return QPainterPath(hit)
    r = size / 2.0
    path = QPainterPath()
    if state in ("up", "ok"):
        path.addEllipse(QPointF(0, 0), r, r)
    elif state in ("down", "error"):
        w = size * 0.30
        path.addRoundedRect(QRectF(-w / 2, -r, w, size), w * 0.3, w * 0.3)
    elif state == "proto_down":
        path.setFillRule(Qt.OddEvenFill)
        path.addEllipse(QPointF(0, 0), r, r)
        ri = r - 0.2 * size
        path.addEllipse(QPointF(0, 0), ri, ri)
    elif state in ("admin_down", "neutral", "stopped"):
        s = size * 0.84
        path.addRoundedRect(QRectF(-s / 2, -s / 2, s, s), s * 0.12, s * 0.12)
    elif state == "booted":
        p = QPainterPath(QPointF(-r * 0.62, r * 0.02))
        p.lineTo(QPointF(-r * 0.16, r * 0.48))
        p.lineTo(QPointF(r * 0.66, -r * 0.46))
        path = _stroked(p, size * 0.2)
    elif state in ("disconnected", "x", "cloud_down"):
        p = QPainterPath(QPointF(-r * 0.55, -r * 0.55))
        p.lineTo(QPointF(r * 0.55, r * 0.55))
        p.moveTo(QPointF(r * 0.55, -r * 0.55))
        p.lineTo(QPointF(-r * 0.55, r * 0.55))
        path = _stroked(p, size * 0.2)
    elif state == "booting":
        p = QPainterPath()
        rect = QRectF(-r * 0.8, -r * 0.8, r * 1.6, r * 1.6)
        p.arcMoveTo(rect, 90)
        p.arcTo(rect, 90, -270)
        path = _stroked(p, size * 0.18)
    elif state == "warn":
        h = size * 0.92
        path.moveTo(0, -h / 2)
        path.lineTo(r, h / 2)
        path.lineTo(-r, h / 2)
        path.closeSubpath()
    elif state == "info":
        path.addEllipse(QPointF(0, 0), r * 0.62, r * 0.62)
    elif state == "blocking":
        path.setFillRule(Qt.OddEvenFill)
        path.addEllipse(QPointF(0, 0), r, r)
        sl = QPainterPath(QPointF(-r * 0.55, r * 0.55))
        sl.lineTo(QPointF(r * 0.55, -r * 0.55))
        path = path.subtracted(_stroked(sl, size * 0.16))
    else:
        path.addEllipse(QPointF(0, 0), r * 0.5, r * 0.5)
    _GLYPH_CACHE[key] = QPainterPath(path)
    return path


def glyph_state_color(state: str, tk: Tokens | None = None) -> QColor:
    """The fill a glyph state is painted with."""
    tk = tk or tokens()
    return {"up": status_fill("up", tk), "ok": status_fill("up", tk), "booted": status_fill("up", tk),
            "down": status_fill("down", tk), "error": status_fill("down", tk),
            "disconnected": status_fill("down", tk), "x": status_fill("down", tk),
            "cloud_down": status_fill("down", tk), "admin_down": status_fill("down", tk),
            "proto_down": status_fill("proto", tk), "warn": status_fill("warn", tk),
            "blocking": QColor(STP_BLOCK)}.get(state, tk.N(9))


def draw_status_glyph(p: QPainter, center: QPointF, state: str, size: float, color: QColor | None = None,
                      tk: Tokens | None = None, angle: float = 0.0, halo: QColor | None = None) -> QPainterPath:
    """Paint status_glyph(state) at ``center`` (rotated by ``angle`` degrees).  In the light theme every amber
    mark gets a 1 px WARN.text outline (WCAG 1.4.11 non-text contrast); ``halo`` = a 1.5 px separator ring."""
    tk = tk or tokens()
    col = QColor(color) if color is not None else glyph_state_color(state, tk)
    path = status_glyph(state, size)
    p.save()
    p.translate(center)
    if angle:
        p.rotate(angle)
    if halo is not None:
        p.setPen(QPen(halo, 3.0, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        p.setBrush(halo)
        p.drawPath(path)
    amber = col.name().upper() in (STATUS["warn"][0], STATUS["warn"][1], STP_BLOCK)
    p.setPen(QPen(status_text("warn", tk), 1.0) if amber and not tk.dark else Qt.NoPen)
    p.setBrush(col)
    p.drawPath(path)
    p.restore()
    return path


# ============================================================================ vector icons
def draw_icon(p: QPainter, kind: str, rect: QRectF, color: QColor | str, width: float | None = None) -> None:
    """Small line icons: check, cross, dots, bang, clock, question, bolt, diamond, chevron."""
    col = QColor(color)
    w = width or max(1.2, rect.width() * 0.16)
    p.save()
    p.setRenderHint(QPainter.Antialiasing)
    pen = QPen(col, w, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
    p.setPen(pen)
    p.setBrush(Qt.NoBrush)
    r = rect.adjusted(rect.width() * 0.12, rect.height() * 0.12, -rect.width() * 0.12, -rect.height() * 0.12)
    c = r.center()
    if kind == "check":
        path = QPainterPath(QPointF(r.left(), c.y()))
        path.lineTo(QPointF(r.left() + r.width() * 0.38, r.bottom() - r.height() * 0.08))
        path.lineTo(QPointF(r.right(), r.top() + r.height() * 0.1))
        p.drawPath(path)
    elif kind == "cross":
        p.drawLine(r.topLeft(), r.bottomRight())
        p.drawLine(r.topRight(), r.bottomLeft())
    elif kind == "dots":
        p.setPen(Qt.NoPen)
        p.setBrush(col)
        d = max(1.4, r.width() * 0.2)
        for i in (-1, 0, 1):
            p.drawEllipse(QPointF(c.x() + i * r.width() * 0.36, c.y() + r.height() * 0.15), d / 2, d / 2)
    elif kind == "bang":
        p.drawLine(QPointF(c.x(), r.top()), QPointF(c.x(), r.bottom() - r.height() * 0.32))
        p.setPen(Qt.NoPen)
        p.setBrush(col)
        p.drawEllipse(QPointF(c.x(), r.bottom() - r.height() * 0.05), w * 0.6, w * 0.6)
    elif kind == "clock":
        rad = min(r.width(), r.height()) / 2
        p.drawEllipse(c, rad, rad)
        p.drawLine(c, QPointF(c.x(), c.y() - rad * 0.62))
        p.drawLine(c, QPointF(c.x() + rad * 0.5, c.y()))
    elif kind == "question":
        rad = r.width() * 0.32
        path = QPainterPath()
        path.arcMoveTo(QRectF(c.x() - rad, r.top(), rad * 2, rad * 2), 160)
        path.arcTo(QRectF(c.x() - rad, r.top(), rad * 2, rad * 2), 160, -250)
        path.lineTo(QPointF(c.x(), r.bottom() - r.height() * 0.3))
        p.drawPath(path)
        p.setPen(Qt.NoPen)
        p.setBrush(col)
        p.drawEllipse(QPointF(c.x(), r.bottom() - r.height() * 0.04), w * 0.6, w * 0.6)
    elif kind == "bolt":          # link conditioning
        path = QPainterPath(QPointF(r.left(), c.y() + r.height() * 0.1))
        path.lineTo(QPointF(c.x() - r.width() * 0.12, c.y() - r.height() * 0.3))
        path.lineTo(QPointF(c.x() + r.width() * 0.12, c.y() + r.height() * 0.3))
        path.lineTo(QPointF(r.right(), c.y() - r.height() * 0.1))
        p.drawPath(path)
    elif kind == "diamond":
        path = QPainterPath(QPointF(c.x(), r.top()))
        path.lineTo(QPointF(r.right(), c.y()))
        path.lineTo(QPointF(c.x(), r.bottom()))
        path.lineTo(QPointF(r.left(), c.y()))
        path.closeSubpath()
        p.setBrush(col)
        p.drawPath(path)
    elif kind == "chevron":
        path = QPainterPath(QPointF(r.left() + r.width() * 0.2, r.top()))
        path.lineTo(QPointF(r.right() - r.width() * 0.2, c.y()))
        path.lineTo(QPointF(r.left() + r.width() * 0.2, r.bottom()))
        p.drawPath(path)
    elif kind == "chevron_down":
        path = QPainterPath(QPointF(r.left(), r.top() + r.height() * 0.3))
        path.lineTo(QPointF(c.x(), r.bottom() - r.height() * 0.3))
        path.lineTo(QPointF(r.right(), r.top() + r.height() * 0.3))
        p.drawPath(path)
    elif kind == "chevron_up":
        path = QPainterPath(QPointF(r.left(), r.bottom() - r.height() * 0.3))
        path.lineTo(QPointF(c.x(), r.top() + r.height() * 0.3))
        path.lineTo(QPointF(r.right(), r.bottom() - r.height() * 0.3))
        p.drawPath(path)
    elif kind == "crown":        # STP root bridge
        p.setPen(QPen(col, w * 0.8, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        p.setBrush(col)
        path = QPainterPath(QPointF(r.left(), r.bottom() - r.height() * 0.18))
        path.lineTo(QPointF(r.left(), r.top() + r.height() * 0.22))
        path.lineTo(QPointF(r.left() + r.width() * 0.28, r.top() + r.height() * 0.52))
        path.lineTo(QPointF(c.x(), r.top()))
        path.lineTo(QPointF(r.right() - r.width() * 0.28, r.top() + r.height() * 0.52))
        path.lineTo(QPointF(r.right(), r.top() + r.height() * 0.22))
        path.lineTo(QPointF(r.right(), r.bottom() - r.height() * 0.18))
        path.closeSubpath()
        p.drawPath(path)
        p.drawRect(QRectF(r.left(), r.bottom() - r.height() * 0.08, r.width(), r.height() * 0.08))
    elif kind == "slash":        # STP blocked port
        p.drawLine(QPointF(r.left(), r.bottom()), QPointF(r.right(), r.top()))
    elif kind == "scissors":     # cable pulled
        p.drawLine(QPointF(r.left(), r.top()), QPointF(r.right(), r.bottom() - r.height() * 0.3))
        p.drawLine(QPointF(r.left(), r.bottom() - r.height() * 0.3), QPointF(r.right(), r.top()))
        rad = r.width() * 0.16
        p.drawEllipse(QPointF(r.left() + rad, r.bottom() - rad * 0.4), rad, rad)
        p.drawEllipse(QPointF(r.right() - rad, r.bottom() - rad * 0.4), rad, rad)
    elif kind == "globe":
        rad = min(r.width(), r.height()) / 2
        p.drawEllipse(c, rad, rad)
        p.drawEllipse(c, rad * 0.45, rad)
        p.drawLine(QPointF(c.x() - rad, c.y()), QPointF(c.x() + rad, c.y()))
    p.restore()


GLYPH_ICON = {"✓": "check", "✕": "cross", "…": "dots", "!": "bang", "⏱": "clock", "?": "question"}


# ============================================================================ chips
CHIP_H = 16.0
CHIP_PITCH = 18.0            # S8: chip column pitch


@dataclass
class ChipSpec:
    text: str
    hue: str                     # protocol hue (outline)
    fill: QColor | None          # health fill
    icon: str = ""               # GLYPH_ICON value
    icon_color: str | QColor = ""
    outline: bool = False        # neutral outline chip (route source)
    blue_bar: bool = False
    faded: bool = False
    dashed: bool = False


def health_fill(health_value: str, hue: str) -> QColor | None:
    if health_value == "OK":
        return alpha(hue, 0.14)
    if health_value == "PARTIAL":
        return alpha(AMBER, 0.18)
    if health_value == "DOWN":
        return alpha(DOWN_RED, 0.14)
    return None


def glyph_color(glyph: str, hue: str) -> str:
    return {"✓": hue, "…": AMBER, "!": DOWN_RED, "✕": DOWN_RED, "⏱": NEUTRAL, "?": NEUTRAL}.get(glyph, hue)


def chip_spec_from(chip) -> ChipSpec:
    """telemetry.Chip -> paint spec (the hue is fixed per kind, the fill and glyph give health)."""
    hue = PROTO_HUES.get(chip.kind, PROTO_HUES["DEFAULT"])
    glyph = "⏱" if (chip.stale or chip.faded) and chip.glyph in ("✓", "…", "!", "") else chip.glyph
    if chip.outline:
        return ChipSpec(chip.text, PROTO_HUES["DEFAULT"], None, GLYPH_ICON.get(glyph, ""),
                        glyph_color(glyph, NEUTRAL), outline=True, blue_bar=chip.blue_bar, faded=chip.faded)
    fill = None if chip.stale else health_fill(chip.health.value, hue)
    return ChipSpec(chip.text, hue, fill, GLYPH_ICON.get(glyph, ""), glyph_color(glyph, hue), faded=chip.faded)


def chip_font() -> QFont:
    return type("caption")


def chip_width(spec: ChipSpec, fm_: QFontMetricsF) -> float:
    w = fm_.horizontalAdvance(spec.text) + 14
    if spec.icon:
        w += 11
    if spec.blue_bar:
        w += 3
    return w


def _hue_text(hue, tk: Tokens) -> QColor:
    """Readable text colour for a protocol hue on the chip card."""
    c = QColor(hue)
    out = c.lighter(150) if tk.dark else c.darker(125)
    bg = tk.card
    for _ in range(4):
        if contrast(out, bg) >= 4.5:
            break
        out = out.lighter(118) if tk.dark else out.darker(118)
    return out


def draw_chip(p: QPainter, x: float, y: float, spec: ChipSpec, tk: Tokens, f: QFont | None = None) -> QRectF:
    """Draw a chip with its top-left at (x, y); returns its rect."""
    f = f or chip_font()
    p.save()
    p.setFont(f)
    fm_ = fm(f)
    w = chip_width(spec, fm_)
    r = QRectF(x, y, w, CHIP_H)
    rad = CHIP_H / 2
    if spec.faded:
        p.setOpacity(p.opacity() * 0.45)
    p.setBrush(tk.card)
    p.setPen(Qt.NoPen)
    p.drawRoundedRect(r, rad, rad)
    if spec.fill is not None:
        fill = resolve(spec.fill, tk)
        if tk.dark:
            fill.setAlphaF(min(1.0, fill.alphaF() * 1.35))
        p.setBrush(fill)
        p.drawRoundedRect(r, rad, rad)
    hue = QColor(spec.hue)
    pen = QPen(tk.N(8) if spec.outline else alpha(hue, 0.85), 1.0,
               Qt.DashLine if spec.dashed else Qt.SolidLine)
    p.setPen(pen)
    p.setBrush(Qt.NoBrush)
    p.drawRoundedRect(r.adjusted(0.5, 0.5, -0.5, -0.5), rad - 0.5, rad - 0.5)
    tx = r.left() + 7
    if spec.blue_bar:
        clip = QPainterPath()
        clip.addRoundedRect(r, rad, rad)
        p.save()
        p.setClipPath(clip)
        p.fillRect(QRectF(r.left(), r.top(), 5.0, r.height()), QColor(ROUTE_BLUE))
        p.restore()
        tx += 3
    text_col = tk.ink if spec.outline else _hue_text(spec.hue, tk)
    p.setPen(text_col)
    tw = fm_.horizontalAdvance(spec.text)
    p.drawText(QRectF(tx, r.top(), tw + 1, r.height()), Qt.AlignVCenter | Qt.AlignLeft, spec.text)
    if spec.icon:
        ic = resolve(spec.icon_color or spec.hue, tk)
        if ic.name().upper() == STATUS["warn"][0] and not tk.dark:
            ic = status_text("warn", tk)
        draw_icon(p, spec.icon, QRectF(tx + tw + 2, r.top() + 3.5, 9, 9), ic, 1.5)
    p.restore()
    return r


# ----------------------------------------------------------------------------- pills / beads
def draw_pill(p: QPainter, center: QPointF, text: str, tk: Tokens, *, border: QColor | str | None = None,
              text_color: QColor | str | None = None, fill: QColor | None = None, pt: float = 7.5,
              bold: bool = False, icon: str = "", icon_color: QColor | str | None = None,
              dashed: bool = False, height: float = 16.0, pad: float = 5.0,
              hatch: QColor | str | None = None, f: QFont | None = None, glyph: str | None = None) -> QRectF:
    """A rounded pill centred on ``center``.  ``f`` overrides the font (default: caption role, regular weight
    unless ``bold``); ``glyph`` = a status_glyph state drawn instead of a line icon."""
    if f is None:
        f = type("caption", scale=pt / 7.5, weight=600 if bold else 500)
    fm_ = fm(f)
    tw = fm_.horizontalAdvance(text)
    iw = (height - 6) + 3 if (icon or glyph) else 0
    w = tw + iw + pad * 2 + 2
    r = QRectF(center.x() - w / 2, center.y() - height / 2, w, height)
    rad = height / 2
    p.save()
    if fill is not None and QColor(fill).alpha() < 255:     # translucent tint: opaque card underneath
        p.setPen(Qt.NoPen)
        p.setBrush(tk.card)
        p.drawRoundedRect(r, rad, rad)
    bcol = resolve(border, tk) if border is not None else tk.card_border
    p.setPen(QPen(bcol, 1.0, Qt.DashLine if dashed else Qt.SolidLine))
    p.setBrush(resolve(fill, tk) if fill is not None else tk.card)
    p.drawRoundedRect(r.adjusted(0.5, 0.5, -0.5, -0.5), rad - 0.5, rad - 0.5)
    if hatch is not None:                       # diagonal hatching (STP blocked), under the text
        hb = QBrush(alpha(hatch, 0.45), Qt.BDiagPattern)
        p.setPen(Qt.NoPen)
        p.setBrush(hb)
        p.drawRoundedRect(r, rad, rad)
    x = r.left() + pad + 1
    if glyph:
        s = height - 7
        draw_status_glyph(p, QPointF(x + s / 2, r.center().y()), glyph, s,
                          resolve(icon_color, tk) if icon_color is not None else None, tk)
        x += s + 4
    elif icon:
        s = height - 6
        ic = resolve(icon_color or border or tk.ink, tk)
        if ic.name().upper() == STATUS["warn"][0] and not tk.dark:
            ic = status_text("warn", tk)
        draw_icon(p, icon, QRectF(x, r.top() + 3, s, s), ic, 1.5)
        x += s + 3
    p.setFont(f)
    tc = QColor(text_color) if text_color is not None else tk.ink
    if tc.name().upper() in _FILL_MAP:               # a status fill used for words -> its text token
        tc = text_for(tc, tk)
    p.setPen(tc)
    p.drawText(QRectF(x, r.top(), tw + 2, r.height()), Qt.AlignVCenter | Qt.AlignLeft, text)
    p.restore()
    return r


def pill_width(text: str, *, pt: float = 7.5, bold: bool = False, icon: bool = False, height: float = 16.0,
               pad: float = 5.0) -> float:
    f = type("caption", scale=pt / 7.5, weight=600 if bold else 500)
    return fm(f).horizontalAdvance(text) + ((height - 6) + 3 if icon else 0) + pad * 2 + 2


def _bead_colors(tk: Tokens) -> tuple[QColor, QColor]:
    return (tk.N(12), tk.N(1)) if not tk.dark else (tk.N(11), tk.N(1))


def draw_bead(p: QPainter, center: QPointF, text: str, tk: Tokens, *, outlined: bool = False,
              lod: float = 1.0) -> QRectF:
    """V3 P2P / P2P? / BCAST bead: neutral ink, micro role.  Below LOD_TIER1: a 6 px diamond."""
    if lod < LOD_TIER1:
        r = QRectF(center.x() - 3 / lod, center.y() - 3 / lod, 6 / lod, 6 / lod)
        draw_icon(p, "diamond", r.adjusted(-1, -1, 1, 1), tk.ink if not outlined else tk.muted)
        return r
    f = type("micro")
    fm_ = fm(f)
    w = max(20.0, fm_.horizontalAdvance(text) + 9)
    h = 13.0
    r = QRectF(center.x() - w / 2, center.y() - h / 2, w, h)
    p.save()
    bg, fg = _bead_colors(tk)
    if outlined:
        p.setBrush(tk.card)
        p.setPen(QPen(tk.N(9), 1.0, Qt.DashLine))
        text_col = tk.muted
    else:
        p.setBrush(bg)
        p.setPen(Qt.NoPen)
        text_col = fg
    p.drawRoundedRect(r.adjusted(0.5, 0.5, -0.5, -0.5), 4, 4)
    p.setFont(f)
    p.setPen(text_col)
    p.drawText(r, Qt.AlignCenter, text)
    p.restore()
    return r


def subnet_colors(style: str, tk: Tokens) -> tuple[QColor, QColor, QColor | None, bool]:
    """(border, text, fill, dashed) of a subnet label.  Styles: match | half | mismatch | dhcp."""
    if style == "mismatch":
        return (status_fill("warn", tk), status_text("warn", tk), alpha(status_fill("warn", tk), 0.16), False)
    if style == "half":
        return (tk.N(9), tk.muted, None, True)
    if style == "dhcp":
        return (tk.N(8), tk.muted, None, False)
    return (tk.N(8) if not tk.dark else tk.N(8), tk.N(11) if not tk.dark else tk.N(11), None, False)


def subnet_font(style: str = "match") -> QFont:
    if style == "dhcp":
        key = ("subnet-dhcp",)
        f = _FONT_CACHE.get(key)
        if f is None:
            f = QFont(mono(1.0, "caption", weight=500))
            f.setItalic(True)
            _FONT_CACHE[key] = f
        return f
    return mono(1.0, "caption", weight=600 if style == "mismatch" else 500)


def draw_subnet_label(p: QPainter, center: QPointF, text: str, tk: Tokens, style: str = "match",
                      pulse: float = 0.0, pulse_hue: str | None = None) -> QRectF:
    """The cable's subnet tag: a small square-ish mono label (distinct from the round state pills and the
    dark beads).  ``pulse`` 0..1 draws a fading ring (the label just changed)."""
    border, text_col, fill, dashed = subnet_colors(style, tk)
    f = subnet_font(style)
    fm_ = fm(f)
    w = fm_.horizontalAdvance(text) + 10
    h = 14.0
    r = QRectF(center.x() - w / 2, center.y() - h / 2, w, h)
    p.save()
    if pulse > 0:
        ring = r.adjusted(-2 - 4 * (1 - pulse), -2 - 4 * (1 - pulse), 2 + 4 * (1 - pulse), 2 + 4 * (1 - pulse))
        p.setPen(QPen(alpha(pulse_hue or SUBNET_HUE, 0.85 * pulse), 2.4))
        p.setBrush(alpha(pulse_hue or SUBNET_HUE, 0.18 * pulse))
        p.drawRoundedRect(ring, 5, 5)
    p.setPen(Qt.NoPen)
    p.setBrush(tk.card)
    p.drawRoundedRect(r, 3, 3)
    pen = QPen(border, 1.2 if style == "mismatch" else 1.0, Qt.DashLine if dashed else Qt.SolidLine)
    if dashed:
        pen.setDashPattern([2.5, 2.0])
    p.setPen(pen)
    p.setBrush(fill if fill is not None else Qt.NoBrush)
    p.drawRoundedRect(r.adjusted(0.5, 0.5, -0.5, -0.5), 3, 3)
    p.setFont(f)
    p.setPen(text_col)
    p.drawText(r, Qt.AlignCenter, text)
    p.restore()
    return r


def draw_trunk_bead(p: QPainter, center: QPointF, text: str, tk: Tokens, hue: str | None = None) -> QRectF:
    """'TRUNK 10,20' bead: Cisco-blue-dark, like the P2P bead family but wider; ``hue`` = VLAN colouring."""
    f = type("micro")
    fm_ = fm(f)
    w = fm_.horizontalAdvance(text) + 10
    h = 13.0
    r = QRectF(center.x() - w / 2, center.y() - h / 2, w, h)
    p.save()
    base = QColor("#0A4D6E") if not tk.dark else QColor("#7CC8EC")
    p.setBrush(base)
    p.setPen(QPen(QColor(hue), 2.0) if hue else Qt.NoPen)
    p.drawRoundedRect(r.adjusted(0.5, 0.5, -0.5, -0.5), 4, 4)
    p.setFont(f)
    p.setPen(QColor("white") if not tk.dark else QColor("#0B1E2A"))
    p.drawText(r, Qt.AlignCenter, text)
    p.restore()
    return r


# ----------------------------------------------------------------------------- EtherChannel (bundles)
# One hue for the whole feature (the Cisco-blue family of the trunk bead: a bundle is a switching thing).
# The protocol is told by TEXT and SHAPE, never by colour: LACP = rounded bead + solid lasso, PAgP = bead with
# pointed (hexagonal) ends + dashed lasso, static 'on' = square outlined bead + dotted lasso.
EC_PROTOS = ("lacp", "pagp", "on")


def ec_hue(tk: Tokens) -> QColor:
    return QColor(EC_HUE[1] if tk.dark else EC_HUE[0])


def ec_state_colors(state: str, tk: Tokens) -> tuple[QColor, QColor, QColor | None]:
    """(stroke/border, text, fill) of a bundle bead per EcState value; healthy = filled EC hue."""
    if state == "partial" or state == "mismatch":
        return status_fill("warn", tk), status_text("warn", tk), alpha(status_fill("warn", tk), 0.16)
    if state == "down":
        return status_fill("down", tk), status_text("down", tk), alpha(status_fill("down", tk), 0.12)
    if state == "unknown":
        return tk.N(9), tk.muted, None
    hue = ec_hue(tk)
    return hue, QColor("white") if not tk.dark else QColor("#0B1E2A"), hue


def ec_bead_path(r: QRectF, proto: str) -> QPainterPath:
    """Bead silhouette per protocol: LACP rounded, PAgP pointed ends, static 'on' square."""
    path = QPainterPath()
    if proto == "pagp":
        k = r.height() / 2
        path.moveTo(r.left() + k, r.top())
        path.lineTo(r.right() - k, r.top())
        path.lineTo(r.right(), r.center().y())
        path.lineTo(r.right() - k, r.bottom())
        path.lineTo(r.left() + k, r.bottom())
        path.lineTo(r.left(), r.center().y())
        path.closeSubpath()
    elif proto == "on":
        path.addRect(r)
    else:
        path.addRoundedRect(r, r.height() / 2, r.height() / 2)
    return path


def ec_bead_width(text: str, proto: str = "lacp") -> float:
    return fm(type("micro")).horizontalAdvance(text) + (18 if proto == "pagp" else 12)


def draw_ec_bead(p: QPainter, center: QPointF, text: str, tk: Tokens, proto: str = "lacp", state: str = "up",
                 h: float = 14.0) -> QRectF:
    """'Po1 · LACP 2/2' bundle bead.  Shape = protocol, colour = state (healthy EC hue / warn '1/2' / down)."""
    f = type("micro")
    w = ec_bead_width(text, proto)
    r = QRectF(center.x() - w / 2, center.y() - h / 2, w, h)
    border, text_col, fill = ec_state_colors(state, tk)
    p.save()
    path = ec_bead_path(r.adjusted(0.6, 0.6, -0.6, -0.6), proto)
    if fill is not None and fill.alpha() < 255:
        p.setPen(Qt.NoPen)
        p.setBrush(tk.card)
        p.drawPath(path)
    outline_only = proto == "on" and state == "up"
    pen = QPen(border, 1.4 if state != "up" or outline_only else 1.0)
    if state == "unknown":
        pen.setStyle(Qt.DashLine)
    p.setPen(pen)
    p.setBrush(tk.card if outline_only else (fill if fill is not None else tk.card))
    p.drawPath(path)
    p.setFont(f)
    p.setPen(ec_hue(tk) if outline_only else text_col)
    p.drawText(r, Qt.AlignCenter, text)
    p.restore()
    return r


def ec_lasso_pen(proto: str, state: str, tk: Tokens, width: float = 1.6) -> QPen:
    """The oval drawn around a bundle's member cables: solid (LACP) / dashed (PAgP) / dotted (static on)."""
    col = ec_state_colors(state, tk)[0] if state in ("partial", "mismatch", "down", "unknown") else ec_hue(tk)
    pen = QPen(col, width, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
    if proto == "pagp":
        pen.setDashPattern([4.0, 2.2])
        pen.setCapStyle(Qt.FlatCap)
    elif proto == "on":
        pen.setDashPattern([0.4, 2.0])
    return pen


def draw_ec_warn(p: QPainter, center: QPointF, text: str, tk: Tokens, height: float = 16.0) -> QRectF:
    """Amber ⚠ bead with the short reason ('LACP passive ↔ passive — never bundles')."""
    return draw_pill(p, center, text, tk, border=status_fill("warn", tk), text_color=status_text("warn", tk),
                     fill=alpha(status_fill("warn", tk), 0.16), bold=True, glyph="warn",
                     icon_color=status_fill("warn", tk), height=height)


def ec_warn_width(text: str, height: float = 16.0) -> float:
    return pill_width(text, bold=True, icon=True, height=height)


# ----------------------------------------------------------------------------- link styles
@dataclass(frozen=True)
class StrokeStyle:
    color: str
    width: float
    dash: tuple = ()           # () solid, else a Qt dash pattern (in pen widths)
    gap: bool = False          # cable pulled: break in the middle
    neutral: bool = False


LINK_STROKES = {
    "UP": StrokeStyle(LINK_UP, 2.2),
    "ADMIN_DOWN": StrokeStyle(DOWN_RED, 2.4, (4.0, 2.5)),
    "LINK_DOWN": StrokeStyle(DOWN_RED, 2.6),
    "PROTOCOL_DOWN": StrokeStyle(PROTO_ORANGE, 2.4, (5.0, 2.0, 1.0, 2.0)),
    "CABLE_PULLED": StrokeStyle(DOWN_RED, 2.6, (), True),
    "LINK_NOT_STARTED": StrokeStyle(NEUTRAL, 2.0, (7.0, 4.0), neutral=True),
    "UNKNOWN": StrokeStyle(NEUTRAL, 2.2, (0.6, 2.4), neutral=True),
    "LAB_OFF": StrokeStyle(NEUTRAL_LIGHT, 2.0, neutral=True),
    "NODE_OFF": StrokeStyle(NEUTRAL_LIGHT, 2.0, neutral=True),
    "API_UP": StrokeStyle(LINK_UP, 2.0),
    "API_DOWN": StrokeStyle(NEUTRAL, 2.0, neutral=True),
}


def stroke_pen(style: StrokeStyle, color: QColor | None = None, width: float | None = None,
               tk: Tokens | None = None) -> QPen:
    pen = QPen(color or resolve(style.color, tk), width or style.width, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
    if style.dash:
        pen.setDashPattern(list(style.dash))
        pen.setCapStyle(Qt.RoundCap if style.dash[0] < 1 else Qt.FlatCap)
    return pen


def draw_link_sample(p: QPainter, rect: QRectF, kind: str, tk: Tokens, *, blue: bool = False,
                     kvm: bool = False, ends: str | None = None) -> None:
    """Legend sample for a link status (same pens and end glyphs as LinkItem)."""
    st = LINK_STROKES.get(kind, LINK_STROKES["UP"])
    y = rect.center().y()
    x0, x1 = rect.left() + 4, rect.right() - 4
    p.save()
    p.setRenderHint(QPainter.Antialiasing)
    if blue:
        p.setPen(QPen(alpha(ROUTE_BLUE, 0.22), 6, Qt.SolidLine, Qt.RoundCap))
        p.drawLine(QPointF(x0, y), QPointF(x1, y))
        p.setPen(QPen(QColor(ROUTE_BLUE), 3.0, Qt.SolidLine, Qt.RoundCap))
        p.drawLine(QPointF(x0, y), QPointF(x1, y))
        draw_icon(p, "chevron", QRectF(rect.center().x() - 3, y - 3.5, 6, 7), QColor("white"), 1.4)
    elif kvm:
        pen = stroke_pen(st, width=1.2, tk=tk)
        p.setPen(pen)
        p.drawLine(QPointF(x0, y - 2), QPointF(x1, y - 2))
        p.drawLine(QPointF(x0, y + 2), QPointF(x1, y + 2))
    elif st.gap:
        p.setPen(stroke_pen(st, tk=tk))
        mid = rect.center().x()
        p.drawLine(QPointF(x0, y), QPointF(mid - 4, y))
        p.drawLine(QPointF(mid + 4, y), QPointF(x1, y))
        p.setPen(QPen(resolve(st.color, tk), 1.6))
        p.drawLine(QPointF(mid - 4, y - 4), QPointF(mid - 4, y + 4))
        p.drawLine(QPointF(mid + 4, y - 4), QPointF(mid + 4, y + 4))
    else:
        p.setPen(stroke_pen(st, tk=tk))
        p.drawLine(QPointF(x0, y), QPointF(x1, y))
    if ends:
        sz = 6.0
        draw_status_glyph(p, QPointF(x0, y), ends, sz, tk=tk, angle=0, halo=tk.card)
        draw_status_glyph(p, QPointF(x1, y), "up" if ends != "neutral" else "neutral", sz, tk=tk, halo=tk.card)
    p.restore()


def rail_pen(proto: str, state: str, tk: Tokens) -> QPen:
    """Adjacency rail / chord pen: hue fixed per protocol, state shown by dash (never red)."""
    pen = QPen(QColor(PROTO_HUES.get(proto, NEUTRAL)), 2.0, Qt.SolidLine, Qt.FlatCap)
    if state == "converging":
        pen.setDashPattern([3.0, 2.0])
    elif state == "none":
        pen.setDashPattern([0.6, 2.2])
        pen.setCapStyle(Qt.RoundCap)
    return pen


def adjacency_state(raw: str) -> str:
    """Raw OSPF/BGP state text -> 'up' | 'converging' | 'none'."""
    s = (raw or "").upper()
    if s.startswith("FULL") or s.startswith("ESTAB"):
        return "up"
    if any(s.startswith(x) for x in ("INIT", "2WAY", "EXSTART", "EXCHANGE", "LOADING", "ACTIVE", "CONNECT",
                                     "OPENSENT", "OPENCONFIRM", "ATTEMPT")):
        return "converging"
    return "none"


ADJ_ICON = {"up": "check", "converging": "dots", "none": "bang"}


def fmt_bps(bps: float | None) -> str:
    if bps is None:
        return "–"
    for unit_, div in (("Gb/s", 1e9), ("Mb/s", 1e6), ("kb/s", 1e3)):
        if bps >= div:
            return f"{bps / div:.1f} {unit_}"
    return f"{bps:.0f} b/s"


def fmt_age(age: float | None) -> str:
    if age is None:
        return "never"
    if age < 1.5:
        return "just now"
    if age < 90:
        return f"{age:.0f} s ago"
    if age < 5400:
        return f"{age / 60:.0f} min ago"
    return f"{age / 3600:.1f} h ago"


def unit(v: QPointF) -> QPointF:
    ln = math.hypot(v.x(), v.y()) or 1.0
    return QPointF(v.x() / ln, v.y() / ln)


def brush(c) -> QBrush:
    return QBrush(QColor(c))
