"""The tab strip: the header of the LabDesk console window.  One tab per device.

A plain QWidget with its own painting, hit-testing and overflow (not a QTabBar: no stock tab painted
mid-drag, no stock overflow buttons, no arrow keys stolen from the terminal - it never takes focus).

The dot of a tab shows the SESSION state only, coded by shape:

* hollow ring           session started - waiting for output
* solid dot             session live - output received
* ring with a slash     session ended (red only when the exit status is not 0)
"""
from __future__ import annotations

import time
from dataclasses import dataclass

from PySide6.QtCore import QEvent, QPoint, QPointF, QRect, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFontMetricsF, QPainter, QPen
from PySide6.QtWidgets import QToolTip, QWidget

from .. import theme as T
from . import TAB_STRIP_H
from .palette import PHOSPHOR, ConsolePalette

HEIGHT = TAB_STRIP_H
LOGO_W = 32
MENU_W = 28
TAB_MIN, TAB_MAX = 96, 220
TAB_EXTRA = 60                 # 12 pad + 8 dot + 8 gap + label + 8 gap + 16 cross + 8 pad
DOT, CROSS = 8, 16
CHEVRON_W = 18
BELL_S = 0.3
FONT_PT = 11

STARTED, LIVE, ENDED = "started", "live", "ended"


@dataclass
class Tab:
    label: str
    state: str = STARTED
    status: int | None = None
    unseen: bool = False
    bell_until: float = 0.0

    def tooltip(self) -> str:
        if self.state == STARTED:
            return "session started - waiting for output"
        if self.state == LIVE:
            return "session live - output received"
        if self.status is None:
            return "session ended"
        if self.status < 0:
            return f"session ended (signal {-self.status})"
        return f"session ended (status {self.status})"


class ConsoleTabStrip(QWidget):
    selected = Signal(int)
    close_requested = Signal(int)
    menu_requested = Signal(QPoint)          # global position under the menu button

    def __init__(self, pal: ConsolePalette = PHOSPHOR, parent=None):
        super().__init__(parent)
        self.pal = pal
        self.tabs: list[Tab] = []
        self.current = -1
        self.window_active = True
        self._hover = -1                     # tab index, -2 = menu button
        self._hover_close = False
        self._scroll = 0                     # px scrolled to the left
        self.setFixedHeight(HEIGHT)
        self.setFocusPolicy(Qt.NoFocus)
        self.setMouseTracking(True)
        self.setAttribute(Qt.WA_OpaquePaintEvent, True)
        self.setAttribute(Qt.WA_StyledBackground, False)
        self._bell = QTimer(self)
        self._bell.setSingleShot(True)
        self._bell.timeout.connect(self.update)

    # ------------------------------------------------------------------ model
    def set_palette(self, pal: ConsolePalette) -> None:
        self.pal = pal
        self.update()

    def add_tab(self, label: str) -> int:
        self.tabs.append(Tab(label))
        self.update()
        return len(self.tabs) - 1

    def remove_tab(self, i: int) -> None:
        if 0 <= i < len(self.tabs):
            del self.tabs[i]
            if self.current >= len(self.tabs):
                self.current = len(self.tabs) - 1
            elif i < self.current:
                self.current -= 1
            self._hover = -1
            self._clamp_scroll()
            self.update()

    def set_current(self, i: int) -> None:
        if 0 <= i < len(self.tabs):
            self.current = i
            self.tabs[i].unseen = False
            self._reveal(i)
            self.update()

    def set_state(self, i: int, state: str, status: int | None = None) -> None:
        if 0 <= i < len(self.tabs):
            self.tabs[i].state, self.tabs[i].status = state, status
            self.update()

    def set_label(self, i: int, label: str) -> None:
        if 0 <= i < len(self.tabs):
            self.tabs[i].label = label
            self.update()

    def set_unseen(self, i: int, unseen: bool = True) -> None:
        if 0 <= i < len(self.tabs) and self.tabs[i].unseen != unseen and (i != self.current or not unseen):
            self.tabs[i].unseen = unseen
            self.update()

    def ring_bell(self, i: int) -> None:
        """BEL: no sound, no screen flash - the tab's dot is bright for 300 ms."""
        if 0 <= i < len(self.tabs):
            self.tabs[i].bell_until = time.monotonic() + BELL_S
            self._bell.start(int(BELL_S * 1000) + 20)
            self.update()

    def set_window_active(self, active: bool) -> None:
        if active != self.window_active:
            self.window_active = active
            self.update()

    # ------------------------------------------------------------------ layout
    def _fonts(self):
        return T.console_font(FONT_PT), T.console_font(FONT_PT, True)

    def _widths(self) -> list[int]:
        """Tab widths: label width + 60 px clamped to 96-220; all shrink toward 96 before anything scrolls."""
        fm = QFontMetricsF(self._fonts()[1])
        want = [int(max(TAB_MIN, min(TAB_MAX, round(fm.horizontalAdvance(t.label)) + TAB_EXTRA))) for t in self.tabs]
        room = self._tabs_area().width()
        over = sum(want) - room
        while over > 0:
            widest = max(want, default=TAB_MIN)
            if widest <= TAB_MIN:
                break
            idx = [i for i, w in enumerate(want) if w == widest]
            nxt = max([w for w in want if w < widest], default=TAB_MIN)
            cut = min(widest - max(nxt, TAB_MIN), -(-over // len(idx)))
            if cut <= 0:
                break
            for i in idx:
                want[i] -= cut
            over = sum(want) - room
        return want

    def _tabs_area(self) -> QRect:
        return QRect(LOGO_W, 0, max(0, self.width() - LOGO_W - MENU_W), HEIGHT)

    def _menu_rect(self) -> QRect:
        return QRect(self.width() - MENU_W, 0, MENU_W, HEIGHT)

    def _overflow(self) -> int:
        return max(0, sum(self._widths()) - self._tabs_area().width())

    def _clamp_scroll(self) -> None:
        self._scroll = max(0, min(self._scroll, self._overflow()))

    def tab_rects(self) -> list[QRect]:
        self._clamp_scroll()
        x = LOGO_W - self._scroll
        out = []
        for w in self._widths():
            out.append(QRect(x, 0, w, HEIGHT))
            x += w
        return out

    def _close_rect(self, r: QRect) -> QRect:
        return QRect(r.right() - 8 - CROSS + 1, (HEIGHT - CROSS) // 2, CROSS, CROSS)

    def _dot_rect(self, r: QRect) -> QRectF:
        return QRectF(r.x() + 12, (HEIGHT - DOT) / 2, DOT, DOT)

    def _reveal(self, i: int) -> None:
        rects = self.tab_rects()
        if not (0 <= i < len(rects)):
            return
        area = self._tabs_area()
        r = rects[i]
        if r.left() < area.left():
            self._scroll -= area.left() - r.left()
        elif r.right() > area.right():
            self._scroll += r.right() - area.right()
        self._clamp_scroll()

    def _hit(self, pos) -> tuple[int, bool]:
        """(tab index | -2 menu | -1 nothing, on the close cross)."""
        if self._menu_rect().contains(pos):
            return -2, False
        if not self._tabs_area().contains(pos):
            return -1, False
        for i, r in enumerate(self.tab_rects()):
            if r.contains(pos):
                shown = i == self.current or i == self._hover
                return i, shown and self._close_rect(r).adjusted(-3, -3, 3, 3).contains(pos)
        return -1, False

    # ------------------------------------------------------------------ painting
    def paintEvent(self, _ev):
        pal = self.pal
        p = QPainter(self)
        chrome, hair = QColor(pal.chrome), QColor(pal.hairline)
        p.fillRect(self.rect(), chrome)
        p.fillRect(0, HEIGHT - 1, self.width(), 1, hair)
        regular, bold = self._fonts()
        # '>_'
        p.setFont(bold)
        p.setPen(QColor(pal.body))
        p.drawText(QRect(0, 0, LOGO_W, HEIGHT - 1), Qt.AlignCenter, ">_")
        area = self._tabs_area()
        p.save()
        p.setClipRect(area)
        rects = self.tab_rects()
        now = time.monotonic()
        for i, (tab, r) in enumerate(zip(self.tabs, rects)):
            if r.right() < area.left() or r.left() > area.right():
                continue
            active, hover = i == self.current, i == self._hover
            if active:
                p.fillRect(r, QColor(pal.well))                              # merges with the terminal
                p.fillRect(r.x(), 0, r.width(), 2,
                           QColor(pal.accent if self.window_active else pal.muted))
                p.fillRect(r.x(), 0, 1, HEIGHT, hair)
                p.fillRect(r.right(), 0, 1, HEIGHT, hair)
                label = QColor(pal.bright)
            elif hover:
                p.fillRect(r.adjusted(0, 0, 0, -1), QColor(pal.raised))
                label = QColor(pal.body)
            else:
                label = QColor(pal.body if tab.unseen else pal.dim)
            self._paint_dot(p, self._dot_rect(r), tab, now)
            font = bold if active else regular
            p.setFont(font)
            p.setPen(label)
            text_rect = QRect(r.x() + 12 + DOT + 8, 0, r.width() - TAB_EXTRA + 8, HEIGHT - 1)
            fm = QFontMetricsF(font)
            p.drawText(text_rect, Qt.AlignVCenter | Qt.AlignLeft,
                       fm.elidedText(tab.label, Qt.ElideMiddle, text_rect.width()))
            if active or hover:
                self._paint_cross(p, self._close_rect(r), hover and self._hover_close)
        p.restore()
        # overflow chevrons at the clipped ends
        if self._scroll > 0:
            self._paint_chevron(p, area.left(), True)
        if self._overflow() - self._scroll > 0:
            self._paint_chevron(p, area.right() - CHEVRON_W + 1, False)
        # menu button: three painted dots
        mr = self._menu_rect()
        if self._hover == -2:
            p.fillRect(mr.adjusted(0, 0, 0, -1), QColor(pal.raised))
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(pal.body if self._hover == -2 else pal.dim))
        cx, cy = mr.center().x() + 0.5, (HEIGHT - 1) / 2
        for dy in (-5, 0, 5):
            p.drawEllipse(QPointF(cx, cy + dy), 1.5, 1.5)
        p.end()

    def _paint_dot(self, p: QPainter, r: QRectF, tab: Tab, now: float) -> None:
        pal = self.pal
        p.save()
        p.setRenderHint(QPainter.Antialiasing, True)
        ringing = tab.bell_until > now
        if tab.state == LIVE:
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(pal.bright if ringing else pal.body))
            p.drawEllipse(r)
        else:
            colour = pal.dim
            if tab.state == ENDED and tab.status not in (0, None):
                colour = pal.red
            if ringing:
                colour = pal.bright
            p.setPen(QPen(QColor(colour), 1.5))
            p.setBrush(Qt.NoBrush)
            ring = r.adjusted(0.75, 0.75, -0.75, -0.75)
            p.drawEllipse(ring)
            if tab.state == ENDED:
                p.drawLine(QPointF(r.left() - 0.5, r.bottom() + 0.5), QPointF(r.right() + 0.5, r.top() - 0.5))
        p.restore()

    def _paint_cross(self, p: QPainter, r: QRect, hot: bool) -> None:
        p.save()
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setPen(QPen(QColor(self.pal.red if hot else self.pal.dim), 1.5, Qt.SolidLine, Qt.RoundCap))
        c = QRectF(r).adjusted(4.5, 4.5, -4.5, -4.5)
        p.drawLine(c.topLeft(), c.bottomRight())
        p.drawLine(c.topRight(), c.bottomLeft())
        p.restore()

    def _paint_chevron(self, p: QPainter, x: int, left: bool) -> None:
        p.save()
        p.fillRect(x, 0, CHEVRON_W, HEIGHT - 1, QColor(self.pal.chrome))
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setPen(QPen(QColor(self.pal.body), 1.5, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        cx, cy = x + CHEVRON_W / 2, (HEIGHT - 1) / 2
        d = 3 if left else -3
        p.drawLine(QPointF(cx + d / 2, cy - 4), QPointF(cx - d / 2 - (1 if left else -1), cy))
        p.drawLine(QPointF(cx - d / 2 - (1 if left else -1), cy), QPointF(cx + d / 2, cy + 4))
        p.restore()

    # ------------------------------------------------------------------ events
    def mouseMoveEvent(self, ev):
        i, on_close = self._hit(ev.position().toPoint())
        if (i, on_close) != (self._hover, self._hover_close):
            self._hover, self._hover_close = i, on_close
            self.update()

    def leaveEvent(self, _ev):
        if self._hover != -1 or self._hover_close:
            self._hover, self._hover_close = -1, False
            self.update()

    def mousePressEvent(self, ev):
        if ev.button() != Qt.LeftButton:
            return
        i, on_close = self._hit(ev.position().toPoint())
        if i == -2:
            mr = self._menu_rect()
            self.menu_requested.emit(self.mapToGlobal(QPoint(mr.right() + 1, mr.bottom())))
        elif i >= 0 and on_close:
            self.close_requested.emit(i)
        elif i >= 0:
            self.selected.emit(i)

    def wheelEvent(self, ev):
        if self._overflow() > 0:
            d = ev.angleDelta()
            self._scroll -= int((d.y() or d.x()) / 120 * 60)
            self._clamp_scroll()
            self.update()
        ev.accept()

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        self._clamp_scroll()
        if self.current >= 0:
            self._reveal(self.current)

    def event(self, ev):
        if ev.type() == QEvent.ToolTip:
            pos = ev.pos()
            i, on_close = self._hit(pos)
            if i == -2:
                QToolTip.showText(ev.globalPos(), "Console menu", self)
            elif i >= 0 and on_close:
                QToolTip.showText(ev.globalPos(), "Close this console", self)
            elif i >= 0:
                QToolTip.showText(ev.globalPos(), f"{self.tabs[i].label}: {self.tabs[i].tooltip()}", self)
            else:
                QToolTip.hideText()
            return True
        return super().event(ev)
