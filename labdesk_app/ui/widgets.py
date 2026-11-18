"""Small reusable widgets: sparkline, canvas info / notification bar, StatusPill, card frame, FlowLayout,
busy spinner and key chips."""
from __future__ import annotations

import math

from PySide6.QtCore import QPoint, QPointF, QRect, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (QAbstractButton, QFrame, QHBoxLayout, QLabel, QLayout, QMenu, QPushButton,
                               QSizePolicy, QVBoxLayout, QWidget)

from . import theme as T


class Sparkline(QWidget):
    """60-point bit-rate sparkline for the selected link (V9)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.values: list[float] = []
        self.setMinimumHeight(46)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

    def set_values(self, values):
        self.values = list(values or [])[-60:]
        self.update()

    def paintEvent(self, ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        tk = T.tokens(self.palette())
        r = QRectF(self.rect()).adjusted(1, 4, -1, -14)
        p.setPen(QPen(tk.faint, 1))
        p.drawLine(r.bottomLeft(), r.bottomRight())
        vals = self.values
        p.setFont(T.type("caption", weight=400))
        if len(vals) < 2:
            p.setPen(tk.muted)
            p.drawText(QRectF(self.rect()), Qt.AlignCenter, "collecting traffic samples…")
            p.end()
            return
        top = max(vals) or 1.0
        n = len(vals)
        pts = [QPointF(r.left() + r.width() * i / 59.0 + r.width() * (60 - n) / 59.0,
                       r.bottom() - r.height() * v / top) for i, v in enumerate(vals)]
        area = QPainterPath(QPointF(pts[0].x(), r.bottom()))
        for q in pts:
            area.lineTo(q)
        area.lineTo(QPointF(pts[-1].x(), r.bottom()))
        area.closeSubpath()
        hue = QColor(T.PROTO_HUES["OSPF"])
        p.setPen(Qt.NoPen)
        p.setBrush(T.alpha(hue, 0.16))
        p.drawPath(area)
        line = QPainterPath(pts[0])
        for q in pts[1:]:
            line.lineTo(q)
        p.setPen(QPen(hue, 1.6))
        p.setBrush(Qt.NoBrush)
        p.drawPath(line)
        p.setPen(tk.muted)
        p.drawText(QRectF(0, self.height() - 13, self.width(), 12), Qt.AlignLeft | Qt.AlignVCenter,
                   f"now {T.fmt_bps(vals[-1])}")
        p.drawText(QRectF(0, self.height() - 13, self.width(), 12), Qt.AlignRight | Qt.AlignVCenter,
                   f"peak {T.fmt_bps(top)} · last {n} samples")
        p.end()


class InfoBar(QFrame):
    """Dismissible banner pinned to the top of the canvas viewport (e.g. the one-time probe notice)."""

    dismissed = Signal()

    def __init__(self, viewport: QWidget):
        super().__init__(viewport)
        self.setObjectName("infobar")
        h = QHBoxLayout(self)
        h.setContentsMargins(12, 6, 6, 6)
        self.icon = QLabel()
        from . import icons
        self.icon.setPixmap(icons.pixmap("dialog-information", 22, None, self.devicePixelRatioF() or 1.0))
        self.icon.setFixedSize(22, 22)
        self.text = QLabel("")
        self.text.setWordWrap(True)
        self.text.setTextFormat(Qt.RichText)
        self.btn = QPushButton("Got it")
        self.btn.setFlat(True)
        self.btn.clicked.connect(self._dismiss)
        h.addWidget(self.icon)
        h.addWidget(self.text, 1)
        h.addWidget(self.btn)
        self.hide()

    level = "info"            # info (amber stripe, the probe notice) | warn | error
    offset_y = 12

    def show_text(self, html: str, level: str = "info", timeout_ms: int = 0):
        self.level = level
        icon = {"error": "dialog-error", "warn": "dialog-warning"}.get(level, "dialog-information")
        self.icon.setPixmap(icons_pixmap(icon, 22, self.devicePixelRatioF() or 1.0))
        self.text.setText(html)
        self.reposition()
        self.show()
        self.raise_()
        if timeout_ms:
            if not hasattr(self, "_hide_t"):
                self._hide_t = QTimer(self)
                self._hide_t.setSingleShot(True)
                self._hide_t.timeout.connect(self.hide)
            self._hide_t.start(timeout_ms)

    def _dismiss(self):
        self.hide()
        self.dismissed.emit()

    def reposition(self):
        par = self.parentWidget()
        if par is None:
            return
        w = min(760, par.width() - 40)
        self.setFixedWidth(max(300, w))
        self.adjustSize()
        self.move((par.width() - self.width()) // 2, self.offset_y)

    def paintEvent(self, ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        tk = T.tokens(self.palette())
        bg = QColor(tk.card)
        bg.setAlpha(245)
        p.setBrush(bg)
        p.setPen(QPen(T.alpha(T.ROUTE_BLUE, 0.0), 1))
        p.setPen(QPen(tk.card_border, 1))
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        p.drawRoundedRect(r, 8, 8)
        p.setPen(Qt.NoPen)
        stripe = {"error": T.status_fill("down", tk), "warn": T.status_fill("warn", tk)}.get(
            self.level, QColor(T.AMBER))
        p.setBrush(stripe)
        p.drawRoundedRect(QRectF(r.left(), r.top(), 4, r.height()), 2, 2)
        p.end()


def icons_pixmap(name: str, px: int, dpr: float):
    from . import icons
    return icons.pixmap(name, px, None, dpr)


class Card(QFrame):
    """A titled card: header (title + muted 'updated 4 s ago ⏱') and a body layout."""

    def __init__(self, title: str, parent=None):
        super().__init__(parent)
        self.setObjectName("card")
        self.v = QVBoxLayout(self)
        self.v.setContentsMargins(10, 8, 10, 10)
        self.v.setSpacing(4)
        head = QHBoxLayout()
        self.title = QLabel(f"<b>{title}</b>")
        self.age = QLabel("")
        self.age.setObjectName("muted")
        head.addWidget(self.title)
        head.addStretch(1)
        head.addWidget(self.age)
        self.v.addLayout(head)
        self.age_s: float | None = None
        self._age_t0 = 0.0

    def set_age(self, age_s: float | None, t0: float):
        self.age_s, self._age_t0 = age_s, t0
        self.tick(t0)

    def tick(self, now: float):
        if self.age_s is None:
            self.age.setText("")
            return
        a = self.age_s + (now - self._age_t0)
        self.age.setText(f"updated {T.fmt_age(a)} ⏱")


def status_badge_html(word: str, color: str) -> str:
    """Inline status word for rich-text labels: the S2 *text* token of the hue (≥4.5:1, amber never as text on
    light) behind a small dot.  ``color`` may be a fixed status hue (LINK_UP, DOWN_RED, ...) or any colour."""
    col = T.text_hex(color) if color else T.tokens().N(11).name()
    return (f"<span style='color:{col}; font-weight:600'>"
            f"<span style='font-size:small'>●</span>&nbsp;{word}</span>")


# ============================================================================ StatusPill (L2)
TONES = ("neutral", "up", "down", "proto", "warn", "accent")


def _tone_colors(tone: str, tk: T.Tokens) -> tuple[QColor, QColor, QColor]:
    """(background, border, text) of a pill tone."""
    if tone in ("up", "down", "proto", "warn"):
        fill = T.status_fill(tone, tk)
        txt = T.status_text(tone, tk)
        bg = T.alpha(fill, 0.16 if tone == "warn" else 0.12)
        border = T.alpha(txt if (tone == "warn" and not tk.dark) else fill, 0.55)
        return bg, border, txt
    if tone == "accent":
        return QColor(tk.accent_subtle), T.alpha(tk.accent_ring, 0.55), QColor(tk.accent_text)
    return (tk.N(3) if not tk.dark else tk.N(4)), (tk.N(6) if not tk.dark else tk.N(7)), tk.N(12)


class StatusPill(QAbstractButton):
    """One pill class for the toolbar status cluster, the inspector header and the Open-lab table.

    ``tone``: neutral | up | down | proto | warn | accent; ``glyph``: a theme.status_glyph state drawn before
    the text (shape + colour, never colour alone).  ``interactive=False`` = display only (no focus, no hover,
    clicks pass through).  ``menu`` adds a chevron segment that opens it (the main segment still clicks).
    """

    def __init__(self, text: str = "", tone: str = "neutral", glyph: str | None = None, *, height: int = 24,
                 interactive: bool = False, checkable: bool = False, tabular: bool = False, parent=None):
        super().__init__(parent)
        self._tone, self._glyph, self._h = tone, glyph, height
        self._interactive = interactive
        self._tabular = tabular
        self._menu: QMenu | None = None
        self._hover = False
        self._hover_menu = False
        self._spin = 0.0
        self._spin_timer: QTimer | None = None
        self.setText(text)
        self.setCheckable(checkable)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        if interactive:
            self.setCursor(Qt.PointingHandCursor)
            self.setFocusPolicy(Qt.TabFocus)
            self.setMouseTracking(True)
        else:
            self.setFocusPolicy(Qt.NoFocus)
            self.setAttribute(Qt.WA_TransparentForMouseEvents, True)

    # ------------------------------------------------------------------ API
    def set_state(self, text: str | None = None, tone: str | None = None, glyph: str | None = "keep") -> None:
        changed = False
        if text is not None and text != self.text():
            self.setText(text)
            changed = True
        if tone is not None and tone != self._tone:
            self._tone = tone
            changed = True
        if glyph != "keep" and glyph != self._glyph:
            self._glyph = glyph
            changed = True
        if self._glyph == "booting":
            self._start_spin()
        elif self._spin_timer is not None:
            self._spin_timer.stop()
        if changed:
            self.updateGeometry()
            self.update()

    def tone(self) -> str:
        return self._tone

    def glyph(self) -> str | None:
        return self._glyph

    def setMenu(self, menu: QMenu) -> None:  # noqa: N802
        self._menu = menu
        self.updateGeometry()

    def menu(self) -> QMenu | None:
        return self._menu

    def _start_spin(self):
        if self._spin_timer is None:
            self._spin_timer = QTimer(self)
            self._spin_timer.setInterval(60)
            self._spin_timer.timeout.connect(self._step)
        if self.isVisible() and not self._spin_timer.isActive():
            self._spin_timer.start()

    def _step(self):
        if not self.isVisible() or self._glyph != "booting":
            self._spin_timer.stop()
            return
        self._spin = (self._spin + 24) % 360
        self.update()

    def showEvent(self, ev):
        super().showEvent(ev)
        if self._glyph == "booting":
            self._start_spin()

    # ------------------------------------------------------------------ geometry
    def _font(self):
        return T.tabular("small") if self._tabular else T.type("small", weight=600)

    def _menu_w(self) -> int:
        return 22 if self._menu is not None else 0

    def sizeHint(self) -> QSize:
        fm = T.fm(self._font())
        gw = (self._glyph_size() + 6) if self._glyph else 0
        w = 10 + gw + int(math.ceil(fm.horizontalAdvance(self.text()))) + 10 + self._menu_w()
        return QSize(max(w, self._h), self._h)

    def minimumSizeHint(self) -> QSize:
        return self.sizeHint()

    def _glyph_size(self) -> float:
        return 9.0 if self._h <= 20 else 10.0

    # ------------------------------------------------------------------ events
    def enterEvent(self, ev):
        self._hover = True
        self.update()
        super().enterEvent(ev)

    def leaveEvent(self, ev):
        self._hover = self._hover_menu = False
        self.update()
        super().leaveEvent(ev)

    def mouseMoveEvent(self, ev):
        hm = self._menu is not None and ev.position().x() >= self.width() - self._menu_w()
        if hm != self._hover_menu:
            self._hover_menu = hm
            self.update()
        super().mouseMoveEvent(ev)

    def mousePressEvent(self, ev):
        if self._menu is not None and ev.position().x() >= self.width() - self._menu_w():
            self._menu.exec(self.mapToGlobal(QPoint(0, self.height() + 2)))
            return
        super().mousePressEvent(ev)

    def hitButton(self, pos) -> bool:
        return self.rect().adjusted(0, 0, -self._menu_w(), 0).contains(pos)

    # ------------------------------------------------------------------ paint
    def paintEvent(self, ev):
        tk = T.tokens()
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        h = self._h
        r = QRectF(0.5, (self.height() - h) / 2 + 0.5, self.width() - 1, h - 1)
        tone = self._tone
        bg, border, txt = _tone_colors(tone, tk)
        if not self.isEnabled():
            txt = tk.N(8)
        p.setPen(QPen(border, 1))
        p.setBrush(bg)
        p.drawRoundedRect(r, r.height() / 2, r.height() / 2)
        if self._interactive and (self._hover or self.isDown()):
            ov = T.alpha(tk.N(12), 0.14 if self.isDown() else 0.07)
            p.setPen(Qt.NoPen)
            p.setBrush(ov)
            if self._menu is not None:
                mw = self._menu_w()
                clip = QRectF(r.right() - mw, r.top(), mw, r.height()) if self._hover_menu else \
                    QRectF(r.left(), r.top(), r.width() - mw, r.height())
                p.save()
                p.setClipRect(clip)
                p.drawRoundedRect(r, r.height() / 2, r.height() / 2)
                p.restore()
            else:
                p.drawRoundedRect(r, r.height() / 2, r.height() / 2)
        if self.hasFocus():
            p.setPen(QPen(tk.accent_ring, 1.5))
            p.setBrush(Qt.NoBrush)
            p.drawRoundedRect(r.adjusted(-0.5, -0.5, 0.5, 0.5), r.height() / 2, r.height() / 2)
        x = r.left() + 10
        cy = r.center().y()
        if self._glyph:
            gs = self._glyph_size()
            col = T.glyph_state_color(self._glyph, tk) if tone == "neutral" or self._glyph in ("stopped", "neutral") \
                else T.status_fill(tone, tk) if tone in T.STATUS else tk.accent_ring
            if self._glyph in ("stopped", "neutral", "admin_down") and tone == "neutral":
                col = tk.N(9)
            p.save()
            if self._glyph == "booting":
                p.translate(QPointF(x + gs / 2, cy))
                p.rotate(self._spin)
                p.translate(QPointF(-(x + gs / 2), -cy))
            T.draw_status_glyph(p, QPointF(x + gs / 2, cy), self._glyph, gs, col, tk)
            p.restore()
            x += gs + 6
        p.setFont(self._font())
        p.setPen(txt)
        p.drawText(QRectF(x, r.top(), r.right() - x - 8 - self._menu_w(), r.height()),
                   Qt.AlignVCenter | Qt.AlignLeft, self.text())
        if self._menu is not None:
            mx = r.right() - self._menu_w()
            p.setPen(QPen(border, 1))
            p.drawLine(QPointF(mx, r.top() + 5), QPointF(mx, r.bottom() - 5))
            c = QPointF(mx + self._menu_w() / 2 - 1, cy)
            ch = QPainterPath(QPointF(c.x() - 3.5, c.y() - 1.5))
            ch.lineTo(QPointF(c.x(), c.y() + 2))
            ch.lineTo(QPointF(c.x() + 3.5, c.y() - 1.5))
            p.setPen(QPen(tk.N(11), 1.5, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
            p.setBrush(Qt.NoBrush)
            p.drawPath(ch)
        p.end()


# ============================================================================ busy spinner
class Spinner(QWidget):
    """12 px indeterminate arc (status bar 'working…'); animates only while visible and running."""

    def __init__(self, size: int = 12, parent=None):
        super().__init__(parent)
        self.setFixedSize(size, size)
        self._a = 0
        self._t = QTimer(self)
        self._t.setInterval(50)
        self._t.timeout.connect(self._step)

    def set_running(self, on: bool) -> None:
        self.setVisible(on)
        if on and not self._t.isActive():
            self._t.start()
        elif not on:
            self._t.stop()

    def _step(self):
        if not self.isVisible():
            self._t.stop()
            return
        self._a = (self._a + 30) % 360
        self.update()

    def paintEvent(self, ev):
        tk = T.tokens()
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(self.rect()).adjusted(1.5, 1.5, -1.5, -1.5)
        p.setPen(QPen(T.alpha(tk.N(9), 0.35), 1.6))
        p.drawEllipse(r)
        p.setPen(QPen(tk.accent_ring, 1.6, Qt.SolidLine, Qt.RoundCap))
        p.drawArc(r, int(-self._a * 16), 100 * 16)
        p.end()


# ============================================================================ key chip
class KeyChip(QLabel):
    """A keyboard key ('Ctrl+O') as a small bordered chip (Tips card, About)."""

    def __init__(self, text: str, parent=None):
        super().__init__(text, parent)
        self.setFont(T.mono(0.85, "small"))
        self.setObjectName("keychip")
        self.setAlignment(Qt.AlignCenter)
        self.setContentsMargins(5, 0, 5, 1)

    def paintEvent(self, ev):
        tk = T.tokens()
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -1.5)
        p.setPen(QPen(tk.N(7) if not tk.dark else tk.N(8), 1))
        p.setBrush(tk.N(2) if not tk.dark else tk.N(4))
        p.drawRoundedRect(r, 4, 4)
        p.setPen(QPen(tk.N(7) if not tk.dark else tk.N(6), 1))
        p.drawLine(QPointF(r.left() + 3, r.bottom() + 1), QPointF(r.right() - 3, r.bottom() + 1))
        p.setPen(tk.N(12))
        p.setFont(self.font())
        p.drawText(r, Qt.AlignCenter, self.text())
        p.end()

    def sizeHint(self) -> QSize:
        fm = T.fm(self.font())
        return QSize(int(fm.horizontalAdvance(self.text())) + 12, int(fm.height()) + 6)

    def minimumSizeHint(self) -> QSize:
        return self.sizeHint()


# ============================================================================ FlowLayout (Qt example port)
class FlowLayout(QLayout):
    """Wraps its widgets onto the next line when the row is full (inspector link buttons)."""

    def __init__(self, parent=None, margin: int = 0, hspacing: int = 6, vspacing: int = 6):
        super().__init__(parent)
        self._items = []
        self._h, self._v = hspacing, vspacing
        self.setContentsMargins(margin, margin, margin, margin)

    def addItem(self, item):  # noqa: N802
        self._items.append(item)

    def count(self):
        return len(self._items)

    def itemAt(self, i):  # noqa: N802
        return self._items[i] if 0 <= i < len(self._items) else None

    def takeAt(self, i):  # noqa: N802
        return self._items.pop(i) if 0 <= i < len(self._items) else None

    def expandingDirections(self):  # noqa: N802
        return Qt.Orientations(0)

    def hasHeightForWidth(self):  # noqa: N802
        return True

    def heightForWidth(self, w):  # noqa: N802
        return self._do(QRect(0, 0, w, 0), True)

    def setGeometry(self, rect):  # noqa: N802
        super().setGeometry(rect)
        self._do(rect, False)

    def sizeHint(self):  # noqa: N802
        return self.minimumSize()

    def minimumSize(self):  # noqa: N802
        s = QSize()
        for it in self._items:
            s = s.expandedTo(it.minimumSize())
        m = self.contentsMargins()
        return s + QSize(m.left() + m.right(), m.top() + m.bottom())

    def _do(self, rect, test: bool) -> int:
        m = self.contentsMargins()
        r = rect.adjusted(m.left(), m.top(), -m.right(), -m.bottom())
        x, y, line = r.x(), r.y(), 0
        for it in self._items:
            w = it.widget()
            if w is not None and w.isHidden():
                continue
            sh = it.sizeHint()
            nx = x + sh.width() + self._h
            if nx - self._h > r.right() + 1 and line > 0:
                x = r.x()
                y += line + self._v
                nx = x + sh.width() + self._h
                line = 0
            if not test:
                it.setGeometry(QRect(QPoint(x, y), sh))
            x = nx
            line = max(line, sh.height())
        return y + line - rect.y() + m.bottom()


# ============================================================================ dock title bar
class DockTitle(QWidget):
    """28 px dock title: caption (uppercase N11) + optional badge + trailing widgets.  Mouse events fall
    through to the QDockWidget, so the dock stays movable."""

    def __init__(self, text: str, parent=None):
        super().__init__(parent)
        self.setObjectName("docktitle")
        self.setFixedHeight(28)
        h = self.h = QHBoxLayout(self)
        h.setContentsMargins(12, 0, 6, 0)
        h.setSpacing(6)
        self.label = QLabel(text)
        self.label.setFont(T.type("caption", upper=True))
        self.label.setObjectName("muted")
        h.addWidget(self.label)
        self.badge = StatusPill("", "down", "down", height=18)
        self.badge.setVisible(False)
        h.addWidget(self.badge)
        h.addStretch(1)

    def sizeHint(self) -> QSize:                    # QDockWidget lays the content out below sizeHint().height()
        return QSize(super().sizeHint().width(), 28)

    def minimumSizeHint(self) -> QSize:
        return QSize(super().minimumSizeHint().width(), 28)

    def set_text(self, text: str) -> None:
        self.label.setText(text)

    def set_badge(self, n: int, tip: str = "") -> None:
        self.badge.setVisible(n > 0)
        if n > 0:
            self.badge.set_state(str(n))
            self.badge.setToolTip(tip)

    def add_widget(self, w: QWidget) -> None:
        self.h.addWidget(w)

    def theme_changed(self) -> None:
        self.label.setFont(T.type("caption", upper=True))

    def paintEvent(self, ev):
        tk = T.tokens()
        p = QPainter(self)
        p.fillRect(self.rect(), tk.N(2))
        p.setPen(QPen(tk.N(7) if tk.dark else tk.N(6), 1))
        p.drawLine(0, self.height() - 1, self.width(), self.height() - 1)
        p.end()
