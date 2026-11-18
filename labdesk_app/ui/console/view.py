"""The terminal well: device text on an exact pixel grid.

* ``cell_metrics(pt, dpr)`` is the only place the cell is computed (Hack 11 pt at 96 dpi = 9 x 21 px)
* every glyph is placed explicitly at ``pad + column * cell_w`` (QGlyphRun from QRawFont), box-drawing and
  block characters are painted to the full cell, backgrounds / selection / cursor are whole-pixel rects
* a line is rendered once into an image and cached by its content (text, attribute runs, lens spans,
  selection, palette and font), never by its row
* the well shows device bytes only; LabDesk's own words live in the tab strip and the status line
"""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass

from PySide6.QtCore import QPoint, QPointF, QRect, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QGlyphRun, QPainter, QPen, QPixmap, QRawFont
from PySide6.QtWidgets import QApplication, QWidget

from .. import theme as T
from . import PAD_X, PAD_Y, boxdraw, keymap, lens            # padding = T.SPACE[4], T.SPACE[3]
from .palette import PHOSPHOR, ConsolePalette
from .screen import BOLD, FAINT, MIN_COLUMNS, REVERSE, STRIKE, UNDERLINE, WIDE_STUB

LINE_HEIGHT = 1.2
CACHE_LINES = 400
WHEEL_LINES = 3
PILL_BLEED = 6               # px a '--More--' pill reaches past its cells; the line image grows by it
# the right padding (16 px or more) holds two things on separate pixels: the dim mark of a clipped row
# right after the text, and the scroll handle at the window edge
CLIP_MARK_X, CLIP_MARK_W = 2, 5          # mark: 2..6 px after the last visible column (ink 1..7)
BAR_W, BAR_RIGHT = 4, 2                  # handle: the 4 px that end 2 px before the edge


# ============================================================================ the grid
@dataclass(frozen=True)
class Metrics:
    pt: float
    dpr: float
    cw: int                  # cell width, px
    ch: int                  # cell height, px
    baseline: int            # px below the row top
    regular: QRawFont
    bold: QRawFont


_METRICS: dict = {}


def _raw(pt: float, bold: bool, hinting=None) -> QRawFont:
    f = QFont(T.console_font(pt, bold))
    if hinting is not None:
        f.setHintingPreference(hinting)
    return QRawFont.fromFont(f)


def _measure(pt: float, dpr: float):
    """(cell_w, cell_h, baseline, regular raw font, bold raw font).

    cell_w = round(hinted advance of 'M'); cell_h = round(1.2 x (ascent + descent)) measured on an
    UNHINTED raw font of the same family and size (the hinted height would round the cell one pixel too
    tall); the glyphs themselves are rasterised with full hinting."""
    regular, bold = _raw(pt, False), _raw(pt, True)
    soft = _raw(pt, False, QFont.PreferNoHinting)
    idx = regular.glyphIndexesForString("M")
    adv = regular.advancesForGlyphIndexes(idx)[0].x() if idx else regular.averageCharWidth()
    cw = max(1, round(adv))
    ch = max(1, round(LINE_HEIGHT * (soft.ascent() + soft.descent())))
    asc, desc = round(regular.ascent()), round(regular.descent())
    baseline = max(0, (ch - (asc + desc)) // 2) + asc
    return cw, ch, baseline, regular, bold


def metrics(pt: float, dpr: float = 1.0) -> Metrics:
    key = (round(float(pt), 2), round(float(dpr), 2), T.generation())
    m = _METRICS.get(key)
    if m is None:
        cw, ch, baseline, regular, bold = _measure(pt, dpr)
        m = _METRICS[key] = Metrics(float(pt), float(dpr), cw, ch, baseline, regular, bold)
    return m


def cell_metrics(pt: float, dpr: float = 1.0) -> tuple[int, int]:
    """(cell_w, cell_h) in pixels of the console font at ``pt`` points."""
    m = metrics(pt, dpr)
    return m.cw, m.ch


# ============================================================================ line images
_LENS_STYLE = {lens.PROMPT: ("bright", True), lens.CONFIG: ("amber", True), lens.COMMAND: ("bright", False),
               lens.REFUSAL: ("red", False), lens.SEV_HIGH: ("red", False), lens.SEV_MID: ("amber", False),
               lens.TIMESTAMP: ("dim", False), lens.MORE: ("pill_fg", False)}


class LinePainter:
    """Renders compact lines into images; one instance per (palette, metrics)."""

    def __init__(self, pal: ConsolePalette, m: Metrics):
        self.pal = pal
        self.m = m
        self._glyphs = ({}, {})              # char -> glyph index, for (regular, bold)
        self._colors: dict = {}

    def color(self, c: str) -> QColor:
        q = self._colors.get(c)
        if q is None:
            q = self._colors[c] = QColor(c)
        return q

    def _glyph(self, ch: str, bold: bool) -> int:
        cache = self._glyphs[1 if bold else 0]
        g = cache.get(ch)
        if g is None:
            idx = (self.m.bold if bold else self.m.regular).glyphIndexesForString(ch)
            g = cache[ch] = idx[0] if len(idx) == 1 else 0
        return g

    def cells(self, line: tuple, spans=(), sel: tuple | None = None):
        """Per-cell (foreground, background, bold, decorations) lists of a line and its pill ranges."""
        text, runs, _wrapped = line
        pal = self.pal
        n = len(text)
        if sel is not None:
            n = max(n, min(sel[1], 100000))
        fg = [pal.body] * n
        bg: list = [None] * n
        bold = [False] * n
        deco = [0] * n
        for start, length, rfg, rbg, fl in runs:
            f, b = pal.fg(rfg), pal.bg(rbg)
            if rfg == "black" and b is not None:
                f = pal.ansi[0]                            # real black on a coloured background
            if fl & REVERSE:
                f, b = (b or pal.well), f
            if fl & FAINT:
                f = pal.faint(f)
            for x in range(start, min(start + length, n)):
                fg[x], bg[x], bold[x], deco[x] = f, b, bool(fl & BOLD), fl & (UNDERLINE | STRIKE)
        pills = []
        for start, length, cls in spans:
            name, is_bold = _LENS_STYLE[cls]
            colour = getattr(pal, name)
            for x in range(start, min(start + length, n)):
                fg[x] = colour
                bold[x] = bold[x] or is_bold
            if cls == lens.MORE:
                pills.append((start, length))
        if sel is not None:
            for x in range(max(0, sel[0]), min(sel[1], n)):
                fg[x], bg[x] = pal.sel_fg, pal.sel_bg
        return fg, bg, bold, deco, pills

    @staticmethod
    def bleed(spans) -> int:
        """Px the image of a line reaches past its cells on each side: room for the round ends of a pill."""
        return PILL_BLEED if any(cls == lens.MORE for _s, _n, cls in spans) else 0

    def render(self, line: tuple, spans=(), sel: tuple | None = None, dpr: float = 1.0) -> QPixmap | None:
        """The image of a line: ``cells x cell_w`` px wide, plus ``bleed(spans)`` px on each side (the
        caller draws it that many px further left)."""
        text = line[0]
        fg, bg, bold, deco, pills = self.cells(line, spans, sel)
        n = len(fg)
        if n == 0:
            return None
        m = self.m
        cw, ch = m.cw, m.ch
        bleed = self.bleed(spans)
        pm = QPixmap(round((n * cw + 2 * bleed) * dpr), round(ch * dpr))
        pm.setDevicePixelRatio(dpr)
        pm.fill(self.color(self.pal.well))
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing, False)
        p.translate(bleed, 0)
        # backgrounds: whole-pixel rects
        x = 0
        while x < n:
            b = bg[x]
            if b is None:
                x += 1
                continue
            x0 = x
            while x < n and bg[x] == b:
                x += 1
            p.fillRect(x0 * cw, 0, (x - x0) * cw, ch, self.color(b))
        for start, length in pills:
            p.setRenderHint(QPainter.Antialiasing, True)
            p.setPen(Qt.NoPen)
            p.setBrush(self.color(self.pal.muted))
            p.drawRoundedRect(QRectF(start * cw - (PILL_BLEED - 1), 1, length * cw + 2 * (PILL_BLEED - 1),
                                     ch - 2), 3, 3)
            p.setRenderHint(QPainter.Antialiasing, False)
        # text: one glyph run per stretch of equal (colour, weight), every glyph placed on its cell
        x = 0
        nt = len(text)
        while x < nt:
            style = (fg[x], bold[x])
            idxs, pos, special = [], [], []
            raw = m.bold if style[1] else m.regular
            while x < nt and (fg[x], bold[x]) == style:
                c = text[x]
                if c != " " and c != WIDE_STUB:
                    if boxdraw.is_box(c) and boxdraw.rects(c, cw, ch) is not None:
                        special.append((x, c, True))
                    else:
                        g = self._glyph(c, style[1])
                        if g:
                            idxs.append(g)
                            pos.append(QPointF(x * cw, m.baseline))
                        else:
                            special.append((x, c, False))
                x += 1
            colour = self.color(style[0])
            if idxs:
                run = QGlyphRun()
                run.setRawFont(raw)
                run.setGlyphIndexes(idxs)
                run.setPositions(pos)
                p.setPen(colour)
                p.drawGlyphRun(QPointF(0, 0), run)
            for cx, c, is_box in special:
                if is_box:
                    for rx, ry, rw, rh, a in boxdraw.rects(c, cw, ch):
                        if a >= 1.0:
                            p.fillRect(cx * cw + rx, ry, rw, rh, colour)
                        else:
                            shade = QColor(colour)
                            shade.setAlphaF(a)
                            p.fillRect(cx * cw + rx, ry, rw, rh, shade)
                else:                                   # a character the font lacks: Qt's fallback, clipped
                    wide = 2 if cx + 1 < nt and text[cx + 1] == WIDE_STUB else 1
                    p.save()
                    p.setClipRect(cx * cw, 0, wide * cw, ch)
                    p.setFont(T.console_font(m.pt, style[1]))
                    p.setPen(colour)
                    p.drawText(QPointF(cx * cw, m.baseline), c)
                    p.restore()
        # underline / strike-through
        for x in range(n):
            if deco[x]:
                c = self.color(fg[x])
                if deco[x] & UNDERLINE:
                    p.fillRect(x * cw, min(ch - 1, m.baseline + 2), cw, 1, c)
                if deco[x] & STRIKE:
                    p.fillRect(x * cw, m.baseline - round(ch * 0.25), cw, 1, c)
        p.end()
        return pm


def ink_offsets(pm: QPixmap, cw: int, cells: int, background: str) -> list:
    """For each of the first ``cells`` cells of a line image: the x offset (px inside the cell) of its
    leftmost pixel that is not the background, or None for an empty cell."""
    img = pm.toImage()
    bg = QColor(background).rgb()
    h = img.height()
    out = []
    for k in range(cells):
        found = None
        for dx in range(cw):
            x = k * cw + dx
            if x < img.width() and any(img.pixel(x, y) != bg for y in range(h)):
                found = dx
                break
        out.append(found)
    return out


def grid_check(pt: float, cells: int = 110) -> bool:
    """Start-up assertion: in a 110-character run, glyph k sits at exactly k x cell_w (the same ink
    offset in every cell)."""
    m = metrics(pt, 1.0)
    pm = LinePainter(PHOSPHOR, m).render(("H" * cells, (), False))
    offs = ink_offsets(pm, m.cw, cells, PHOSPHOR.well)
    return pm.width() == cells * m.cw and offs[0] is not None and len(set(offs)) == 1


# ============================================================================ the widget
class TerminalView(QWidget):
    """One tab's terminal.  ``session`` is a PtySession (or any object with the same small surface:
    ``screen``, ``write``, ``resize``, ``alive``, ``slices_pending``, signal ``updated``)."""

    message = Signal(str, int)           # text for the status line, milliseconds
    grid_changed = Signal(int, int)      # model columns, rows
    switch_tab = Signal(int)             # +1 / -1
    zoom = Signal(int)                   # +1 / -1 / 0 (reset)
    menu_requested = Signal(QPoint)      # global position

    def __init__(self, session, pal: ConsolePalette = PHOSPHOR, pt: float = T.CONSOLE_PT,
                 node_definition: str | None = None, highlight: bool = True, parent=None):
        super().__init__(parent)
        self.session = session
        self.screen = session.screen
        self.pal = pal
        self.pt = float(pt)
        self.lens_on = bool(highlight)
        self.lens_applies = lens.applies(node_definition)
        self.known_hosts: set = set()
        self._m = metrics(self.pt, self._dpr())
        self._painter = LinePainter(pal, self._m)
        self._cache: OrderedDict = OrderedDict()
        self._scroll_top: int | None = None           # None = follow the output
        self._sel: tuple | None = None                # ((line id, col), (line id, col)) anchor, head
        self._selecting = False
        self._widest = 0
        self._drag_bar = False
        self.setFocusPolicy(Qt.StrongFocus)
        self.setAttribute(Qt.WA_OpaquePaintEvent, True)
        self.setAttribute(Qt.WA_InputMethodEnabled, False)
        self.setCursor(Qt.IBeamCursor)
        self.setMinimumSize(2 * PAD_X + 20 * self._m.cw, 2 * PAD_Y + 4 * self._m.ch)
        self._repaint = QTimer(self)
        self._repaint.setSingleShot(True)
        self._repaint.timeout.connect(self.update)
        self._resize = QTimer(self)
        self._resize.setSingleShot(True)
        self._resize.setInterval(150)
        self._resize.timeout.connect(self.apply_size)
        session.updated.connect(self._on_output)

    # ------------------------------------------------------------------ geometry
    def _dpr(self) -> float:
        return float(self.devicePixelRatioF() or 1.0)

    @property
    def cell(self) -> tuple[int, int]:
        return self._m.cw, self._m.ch

    def view_columns(self) -> int:
        return max(1, (self.width() - 2 * PAD_X) // self._m.cw)

    def view_rows(self) -> int:
        return max(2, (self.height() - 2 * PAD_Y) // self._m.ch)

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        self._resize.start()

    def apply_size(self) -> None:
        """Rows follow the view; model columns = max(80, widest view so far) and never shrink."""
        cols, rows = self.view_columns(), self.view_rows()
        self._widest = max(self._widest, cols)
        before = (self.screen.columns, self.screen.lines)
        self.session.resize(max(MIN_COLUMNS, self._widest), rows)
        now = (self.screen.columns, self.screen.lines)
        if now != before:
            self.grid_changed.emit(*now)
        if cols < self.screen.columns:
            self.message.emit(f"showing {cols} of {self.screen.columns} columns", 2000)
        self.update()

    # ------------------------------------------------------------------ look
    def set_palette(self, pal: ConsolePalette) -> None:
        self.pal = pal
        self._rebuild()

    def set_point_size(self, pt: float) -> None:
        self.pt = float(pt)
        self._rebuild()
        self.setMinimumSize(2 * PAD_X + 20 * self._m.cw, 2 * PAD_Y + 4 * self._m.ch)
        self.apply_size()

    def set_highlight(self, on: bool) -> None:
        self.lens_on = bool(on)
        self.update()

    def _rebuild(self) -> None:
        self._m = metrics(self.pt, self._dpr())
        self._painter = LinePainter(self.pal, self._m)
        self._cache.clear()
        self.update()

    # ------------------------------------------------------------------ output
    def _on_output(self) -> None:
        if self.lens_applies:
            scr = self.screen
            lens.learn(scr.cursor_row_text(), scr.cursor.x, self.known_hosts)
        if not self._repaint.isActive():
            self._repaint.start(33 if self.session.slices_pending else 16)      # 30 Hz while busy, else 60 Hz

    def _screen_top(self) -> int:
        """Absolute id of the line shown in the first row while following the output."""
        scr = self.screen
        top = scr.first_id + len(scr.history)
        rows = self.view_rows()
        if rows < scr.lines:                               # until the debounced resize: keep the cursor in
            top += max(0, scr.cursor.y - (rows - 1))
        return top

    def top_id(self) -> int:
        follow = self._screen_top()
        if self._scroll_top is None:
            return follow
        return max(self.screen.first_id, min(self._scroll_top, follow))

    def scroll_lines(self, delta: int) -> None:
        """Positive = toward older lines."""
        follow = self._screen_top()
        top = max(self.screen.first_id, min(self.top_id() - delta, follow))
        self._scroll_top = None if top >= follow else top
        self.update()

    def scroll_to_bottom(self) -> None:
        if self._scroll_top is not None:
            self._scroll_top = None
            self.update()

    @property
    def scrolled_back(self) -> int:
        """How many lines above the live screen the view is."""
        return self._screen_top() - self.top_id()

    # ------------------------------------------------------------------ painting
    def _spans(self, lid: int, line: tuple):
        if not (self.lens_on and self.lens_applies) or not line[0]:
            return ()
        for _s, _n, fg, bg, _fl in line[1]:
            if fg != "default" or bg != "default":
                return ()                                  # the device coloured this row: hands off
        nxt = self.screen.line(lid + 1)[0] if line[0].lstrip(" ") == "^" else ""
        return tuple(lens.classify(line[0], self.known_hosts, nxt))

    def _sel_range(self, lid: int) -> tuple | None:
        """Selected columns (start, end) of a line, or None."""
        if self._sel is None:
            return None
        a, b = sorted(self._sel)
        if a == b or lid < a[0] or lid > b[0]:
            return None
        cols = self.screen.columns
        start = a[1] if lid == a[0] else 0
        end = b[1] if lid == b[0] else cols
        return (start, end) if end > start else None

    def _image(self, line: tuple, spans, sel) -> QPixmap | None:
        key = (line[0], line[1], spans, sel)
        pm = self._cache.get(key)
        if pm is None and key not in self._cache:
            pm = self._painter.render(line, spans, sel, self._dpr())
            self._cache[key] = pm
            if len(self._cache) > CACHE_LINES:
                self._cache.popitem(last=False)
        else:
            self._cache.move_to_end(key)
        return pm

    def paintEvent(self, _ev):
        p = QPainter(self)
        pal, m, scr = self.pal, self._m, self.screen
        cw, ch = m.cw, m.ch
        p.fillRect(self.rect(), self._painter.color(pal.well))
        scr.sync()
        rows, cols = self.view_rows(), self.view_columns()
        top = self.top_id()
        clip_w = cols * cw
        for r in range(rows):
            lid = top + r
            line = scr.line(lid)
            sel = self._sel_range(lid)
            if not line[0] and sel is None:
                continue
            if sel is not None:
                # a selected line break shows as one selected cell behind the text
                sel = (sel[0], min(sel[1], max(len(line[0]) + 1, sel[0] + 1)))
            spans = self._spans(lid, line)
            pm = self._image(line, spans, sel)
            if pm is None:
                continue
            y = PAD_Y + r * ch
            dpr = pm.devicePixelRatio()
            bleed = LinePainter.bleed(spans)               # the image starts that many px left of column 0
            w = round(pm.width() / dpr) - 2 * bleed
            if w > clip_w:
                p.drawPixmap(PAD_X - bleed, y, pm, 0, 0, round((bleed + clip_w) * dpr), pm.height())
                if len(line[0]) > cols:                    # clipped content: a dim mark in the padding
                    self._paint_clip_mark(p, PAD_X + clip_w, y)
            else:
                p.drawPixmap(PAD_X - bleed, y, pm)
        self._paint_cursor(p, top, rows, cols)
        self._paint_scrollbar(p)
        p.end()

    def _paint_clip_mark(self, p: QPainter, x: int, y: int) -> None:
        """A dim '›' right after the last visible column of a row whose text continues (painted, 5 px
        wide: it never reaches the scroll handle at the window edge)."""
        m = self._m
        half = max(3.0, min(5.0, m.ch * 0.2))
        x0, mid = x + CLIP_MARK_X + 0.5, y + m.ch / 2
        p.save()
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setPen(QPen(self._painter.color(self.pal.dim), 1.5, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        p.drawPolyline([QPointF(x0, mid - half), QPointF(x0 + CLIP_MARK_W - 1, mid), QPointF(x0, mid + half)])
        p.restore()

    def _paint_cursor(self, p: QPainter, top: int, rows: int, cols: int) -> None:
        scr = self.screen
        if not scr.cursor_visible or not self.session.alive:
            return
        r = scr.cursor_line_id() - top
        cx = min(scr.cursor.x, scr.columns - 1)
        if not (0 <= r < rows) or cx >= cols:
            return
        m, pal = self._m, self.pal
        rect = QRect(PAD_X + cx * m.cw, PAD_Y + r * m.ch, m.cw, m.ch)
        colour = self._painter.color(pal.cursor)
        if not self.hasFocus():
            p.setPen(QPen(colour, 1))
            p.setBrush(Qt.NoBrush)
            p.drawRect(rect.adjusted(0, 0, -1, -1))
            return
        p.fillRect(rect, colour)
        text = scr.line(scr.cursor_line_id())[0]
        c = text[cx] if cx < len(text) else " "
        if c not in (" ", WIDE_STUB):
            g = self._painter._glyph(c, False)
            if g:
                run = QGlyphRun()
                run.setRawFont(m.regular)
                run.setGlyphIndexes([g])
                run.setPositions([QPointF(rect.x(), rect.y() + m.baseline)])
                p.setPen(self._painter.color(pal.well))
                p.drawGlyphRun(QPointF(0, 0), run)

    def _bar_geometry(self):
        """(track top, track height, handle top, handle height) of the scroll handle, or None."""
        scr = self.screen
        hist = self._screen_top() - scr.first_id
        if hist <= 0:
            return None
        rows = self.view_rows()
        total = hist + rows
        track_top, track_h = PAD_Y, self.height() - 2 * PAD_Y
        handle_h = max(24, round(track_h * rows / total))
        pos = (self.top_id() - scr.first_id) / max(1, hist)
        return track_top, track_h, track_top + round((track_h - handle_h) * pos), handle_h

    def _paint_scrollbar(self, p: QPainter) -> None:
        g = self._bar_geometry()
        if g is None:
            return
        _tt, _th, top, h = g
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setPen(Qt.NoPen)
        p.setBrush(self._painter.color(self.pal.muted))
        p.drawRoundedRect(QRectF(self.width() - BAR_RIGHT - BAR_W, top, BAR_W, h), 2, 2)
        p.setRenderHint(QPainter.Antialiasing, False)

    # ------------------------------------------------------------------ keyboard
    def focusNextPrevChild(self, _next: bool) -> bool:
        return False                                       # Tab belongs to the device (completion)

    def keyPressEvent(self, ev):
        key, mods = ev.key(), ev.modifiers()
        ctrl, shift, alt = bool(mods & Qt.ControlModifier), bool(mods & Qt.ShiftModifier), bool(mods & Qt.AltModifier)
        if ctrl and alt:                                   # text size (with or without Shift: '=' / '+')
            if key in (Qt.Key_Equal, Qt.Key_Plus):
                self.zoom.emit(1)
                return
            if key in (Qt.Key_Minus, Qt.Key_Underscore):
                self.zoom.emit(-1)
                return
            if key in (Qt.Key_0, Qt.Key_ParenRight):
                self.zoom.emit(0)
                return
        if ctrl and shift and not alt:
            if key == Qt.Key_C:
                self.copy()
                return
            if key == Qt.Key_V:
                self.paste()
                return
            if key in (Qt.Key_Tab, Qt.Key_Backtab):
                self.switch_tab.emit(-1)
                return
        if ctrl and not alt:
            if key == Qt.Key_PageUp:
                self.switch_tab.emit(-1)
                return
            if key == Qt.Key_PageDown:
                self.switch_tab.emit(1)
                return
            if key == Qt.Key_Tab:
                self.switch_tab.emit(1)
                return
            if key == Qt.Key_Backtab:
                self.switch_tab.emit(-1)
                return
        if shift and not ctrl and not alt:
            if key == Qt.Key_PageUp:
                self.scroll_lines(max(1, self.view_rows() - 1))
                return
            if key == Qt.Key_PageDown:
                self.scroll_lines(-max(1, self.view_rows() - 1))
                return
            if key == Qt.Key_Insert:
                self.paste()
                return
        data = keymap.encode(int(key), int(mods.value), ev.text(), self.screen.app_cursor_keys)
        if data:
            self.send(data)

    def send(self, data: bytes) -> None:
        """Bytes typed by the user: to the device, and back to the live end of the output."""
        if not self.session.alive:
            return
        self.session.write(data)
        self.scroll_to_bottom()

    # ------------------------------------------------------------------ copy / paste
    def selected_text(self) -> str:
        if self._sel is None:
            return ""
        a, b = sorted(self._sel)
        return self.screen.text_between(a, b) if a != b else ""

    def copy(self) -> None:
        text = self.selected_text()
        if text:
            QApplication.clipboard().setText(text)

    def paste(self, selection: bool = False) -> None:
        cb = QApplication.clipboard()
        text = cb.text(cb.Mode.Selection) if selection and cb.supportsSelection() else cb.text()
        if text:
            self.send(text.replace("\r\n", "\r").replace("\n", "\r").encode("utf-8", "replace"))

    def clear_selection(self) -> None:
        if self._sel is not None:
            self._sel = None
            self.update()

    # ------------------------------------------------------------------ mouse
    def _cell_at(self, pos, snap: bool = True) -> tuple[int, int]:
        """(line id, column) under a point, both whole numbers (a mouse position is a QPointF: its
        coordinates are floats); ``snap`` rounds to the nearest cell boundary (selection)."""
        m = self._m
        x = int(pos.x()) - PAD_X + (m.cw // 2 if snap else 0)
        col = max(0, min(self.screen.columns, x // m.cw))
        row = max(0, min(self.view_rows() - 1, (int(pos.y()) - PAD_Y) // m.ch))
        return int(self.top_id() + row), int(col)

    def mousePressEvent(self, ev):
        self.setFocus(Qt.MouseFocusReason)
        if ev.button() == Qt.LeftButton:
            if ev.position().x() >= self.width() - PAD_X + 2 and self._bar_geometry() is not None:
                self._drag_bar = True
                self._drag_to(ev.position().y())
                return
            c = self._cell_at(ev.position())
            self._sel = (c, c)
            self._selecting = True
            self.update()
        elif ev.button() == Qt.MiddleButton:
            self.paste(selection=True)

    def mouseMoveEvent(self, ev):
        if self._drag_bar:
            self._drag_to(ev.position().y())
        elif self._selecting and self._sel is not None:
            self._sel = (self._sel[0], self._cell_at(ev.position()))
            self.update()

    def mouseReleaseEvent(self, ev):
        if ev.button() != Qt.LeftButton:
            return
        if self._drag_bar:
            self._drag_bar = False
            return
        self._selecting = False
        if self._sel is not None and self._sel[0] == self._sel[1]:
            self._sel = None
            self.update()
        else:
            self._to_primary()

    def mouseDoubleClickEvent(self, ev):
        if ev.button() != Qt.LeftButton:
            return
        lid, col = self._cell_at(ev.position(), snap=False)
        text = self.screen.line(lid)[0]
        if col >= len(text) or text[col] == " ":
            return
        a = b = col
        while a > 0 and text[a - 1] != " ":
            a -= 1
        while b < len(text) and text[b] != " ":
            b += 1
        self._sel = ((lid, a), (lid, b))
        self._selecting = False
        self._to_primary()
        self.update()

    def _to_primary(self) -> None:
        cb = QApplication.clipboard()
        text = self.selected_text()
        if text and cb.supportsSelection():
            cb.setText(text, cb.Mode.Selection)

    def _drag_to(self, y: float) -> None:
        g = self._bar_geometry()
        if g is None:
            return
        track_top, track_h, _top, handle_h = g
        frac = (y - track_top - handle_h / 2) / max(1, track_h - handle_h)
        scr = self.screen
        follow = self._screen_top()
        top = scr.first_id + round(max(0.0, min(1.0, frac)) * (follow - scr.first_id))
        self._scroll_top = None if top >= follow else top
        self.update()

    def wheelEvent(self, ev):
        dy = ev.angleDelta().y()
        if dy:
            steps = dy / 120.0
            self.scroll_lines(int(round(steps * WHEEL_LINES)) or (1 if dy > 0 else -1))
        ev.accept()

    def contextMenuEvent(self, ev):
        self.menu_requested.emit(ev.globalPos())

    def focusInEvent(self, ev):
        super().focusInEvent(ev)
        self.update()

    def focusOutEvent(self, ev):
        super().focusOutEvent(ev)
        self.update()

