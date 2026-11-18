"""IconProvider (S6): bundled Breeze SVGs, recoloured to the LabDesk theme and cached.

* Auto / Light / Dark (and offscreen QA): the bundled SVG with its ``ColorScheme-Text`` colour rewritten
  to N12 (Normal), N8 (Disabled) or accent_ring (Active / checked), rendered with QSvgRenderer at the
  device pixel ratio.
* "KDE colours": ``QIcon.fromTheme(name, bundled)`` so the user's icon theme wins and the bundled set only
  fills gaps.
* Role ``danger`` (red destructive menu items) and the custom glyphs (``activity``) always use the bundled,
  recoloured SVG.

Results are cached per (name, size, dpr, mode, role, theme mode) and dropped on a palette / theme change.
"""
from __future__ import annotations

import re
from pathlib import Path

from PySide6.QtCore import QByteArray, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QIcon, QIconEngine, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer

from . import theme as T

ICON_DIR = Path(__file__).resolve().parent / "icons"
CUSTOM = {"activity", "terminal"}
SIZES = {"toolbar": 18, "menu": 16, "inspector": 16}
# action -> Breeze name (S6 mapping)
ACTION_ICONS = {
    "open": "document-open", "new": "document-new", "start": "media-playback-start", "stop": "media-playback-stop",
    "wipe": "edit-clear-all", "link": "network-connect", "fit": "zoom-fit-best", "refresh": "view-refresh",
    "console": "utilities-terminal", "config": "document-edit", "layers": "view-visible",
    "legend": "view-list-details", "simulate": "network-disconnect", "help": "help-about", "probing": "activity",
    "export": "document-save-as", "delete": "edit-delete", "lab": "application-menu", "view": "view-visible",
    "properties": "document-properties", "rename": "edit-rename", "copy": "edit-copy", "find": "edit-find",
    "info": "dialog-information", "warning": "dialog-warning", "error": "dialog-error",
    "more": "overflow-menu", "close": "window-close", "folder": "folder-open",
}
_SVG_CACHE: dict[str, str] = {}
_PM_CACHE: dict[tuple, QPixmap] = {}
_THEME_MODE = ["auto"]               # set by the ThemeController


def set_theme_mode(mode: str) -> None:
    _THEME_MODE[0] = mode
    clear_cache()


def clear_cache() -> None:
    _PM_CACHE.clear()


def svg_path(name: str, px: int) -> Path | None:
    for size in ((16, 22) if px <= 16 else (22, 16)):
        p = ICON_DIR / f"{name}-{size}.svg"
        if p.exists():
            return p
    return None


def available(name: str) -> bool:
    return svg_path(name, 16) is not None


def _svg_text(path: Path) -> str:
    s = _SVG_CACHE.get(str(path))
    if s is None:
        s = _SVG_CACHE[str(path)] = path.read_text(encoding="utf-8")
    return s


_CLASS_RE = re.compile(r"(\.ColorScheme-(\w+)\s*\{\s*color:\s*)(#[0-9a-fA-F]{3,8})")


def recolour(svg: str, text: QColor, tk: T.Tokens | None = None) -> str:
    tk = tk or T.tokens()
    repl = {"Text": text, "ButtonText": text, "Highlight": tk.accent_ring,
            "NegativeText": T.status_fill("down", tk), "PositiveText": T.status_fill("up", tk),
            "NeutralText": T.status_fill("warn", tk), "Background": tk.N(1)}

    def sub(m):
        c = repl.get(m.group(2), text)
        return m.group(1) + c.name()
    out = _CLASS_RE.sub(sub, svg)
    if out == svg:                                  # icon without a colour scheme block: tint fill/stroke
        out = svg.replace("currentColor", text.name())
    return out


def _colour_for(mode: QIcon.Mode, state: QIcon.State, role: str, tk: T.Tokens) -> QColor:
    if role == "danger":
        c = T.status_fill("down", tk)
        if mode == QIcon.Disabled:
            c = T.alpha(c, 0.45)
        return c
    if role == "onaccent":                       # icons on an accent / destructive filled button
        return QColor(255, 255, 255, 140) if mode == QIcon.Disabled else QColor("white")
    if mode == QIcon.Disabled:
        return tk.N(8)
    if mode in (QIcon.Active, QIcon.Selected) and role == "accent":
        return tk.accent_ring
    if state == QIcon.On and role == "checkable":
        return tk.accent_ring
    if mode == QIcon.Selected:
        return QColor("white")
    return tk.N(12) if role != "muted" else tk.N(11)


def render(name: str, px: int, dpr: float = 1.0, mode=QIcon.Normal, state=QIcon.Off, role: str = "normal",
           color: QColor | None = None) -> QPixmap:
    tk = T.tokens()
    col = QColor(color) if color is not None else _colour_for(mode, state, role, tk)
    key = (name, px, round(dpr, 2), int(mode.value) if hasattr(mode, "value") else int(mode),
           int(state.value) if hasattr(state, "value") else int(state), role, col.name(QColor.HexArgb),
           _THEME_MODE[0], T.generation())
    pm = _PM_CACHE.get(key)
    if pm is not None:
        return pm
    path = svg_path(name, px)
    pm = QPixmap(max(1, int(round(px * dpr))), max(1, int(round(px * dpr))))
    pm.setDevicePixelRatio(dpr)
    pm.fill(Qt.transparent)
    if path is not None:
        r = QSvgRenderer(QByteArray(recolour(_svg_text(path), col, tk).encode()))
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing)
        if col.alpha() < 255:
            p.setOpacity(col.alphaF())
        r.render(p, QRectF(0, 0, px, px))
        p.end()
    _PM_CACHE[key] = pm
    return pm


class _Engine(QIconEngine):
    """Renders on demand, so the icon follows theme switches and any DPR."""

    def __init__(self, name: str, role: str):
        super().__init__()
        self.name, self.role = name, role

    def pixmap(self, size: QSize, mode, state):
        px = max(size.width(), size.height())
        return render(self.name, px, 1.0, mode, state, self.role)

    def scaledPixmap(self, size: QSize, mode, state, scale: float):
        px = max(1, int(round(max(size.width(), size.height()) / max(scale, 1.0))))
        return render(self.name, px, scale, mode, state, self.role)

    def paint(self, painter: QPainter, rect, mode, state):
        dpr = painter.device().devicePixelRatioF() if painter.device() is not None else 1.0
        px = min(rect.width(), rect.height())
        painter.drawPixmap(rect, render(self.name, px, dpr, mode, state, self.role))

    def clone(self):
        return _Engine(self.name, self.role)


def icon(name: str, role: str = "normal") -> QIcon:
    """QIcon for an action or Breeze name.  role: normal | muted | accent | checkable | danger | onaccent."""
    name = ACTION_ICONS.get(name, name)
    bundled = QIcon(_Engine(name, role)) if available(name) else QIcon()
    if _THEME_MODE[0] == "kde" and role not in ("danger", "onaccent") and name not in CUSTOM:
        th = QIcon.fromTheme(name)
        if not th.isNull():
            return th
    return bundled


def pixmap(name: str, px: int, color: QColor | None = None, dpr: float = 2.0, role: str = "normal") -> QPixmap:
    return render(ACTION_ICONS.get(name, name), px, dpr, role=role, color=color)
