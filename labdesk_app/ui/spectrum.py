"""A Wi-Fi analyser: every radio of the site as heard at one spot, drawn over the channels of each band.

Each radio is its channel's spectrum shape (the 802.11 transmit mask) with its top at the level heard here.
Your access points are in colour, the neighbours' networks grey and dashed; the radio that serves this spot is
drawn bold.  Dashed lines at −82 and −62 dBm are where a Wi-Fi radio starts waiting for another one (on its
own channel, and for any energy).  Click a channel to see how loud it is everywhere (the channel map).
"""
from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QSizePolicy, QWidget

from .. import wifi_channels as wc
from .. import wifi_interference as wi
from .. import wifi_site as ws
from . import theme as T

TOP_DBM, BOTTOM_DBM = -20.0, -100.0
ROW_H = 104.0
PAD_L, PAD_R = 34.0, 8.0
# 5 GHz has a hole (5350-5470 MHz): drawn as a narrow break instead of empty space
_SEGMENTS = {"2.4": [(2402.0, 2482.0)], "5": [(5170.0, 5330.0), (5490.0, 5710.0)], "6": [(5945.0, 6425.0)]}
_GAP = 10.0                       # px between the two parts of 5 GHz


def radio_color(idx: int) -> QColor:
    return QColor(T.VLAN_PALETTE[idx % len(T.VLAN_PALETTE)])


class SpectrumView(QWidget):
    channel_clicked = Signal(str, int)        # band, 20 MHz channel

    def __init__(self, parent=None):
        super().__init__(parent)
        self.bands: list[str] = ["5"]
        self.lines: dict[str, list] = {}          # band -> [(Emitter, dBm, serving)]
        self.note = ""
        self.selected: tuple | None = None        # (band, channel) shown on the floor
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setMouseTracking(True)
        self._hover: tuple | None = None
        self.setToolTip("Every radio as heard at this spot, over the channels it uses.\n"
                        "Click a channel to see how loud it is all over the floor.")
        self._fit()

    def _fit(self) -> None:
        self.setFixedHeight(int(ROW_H * max(1, len(self.bands)) + 6))

    def sizeHint(self) -> QSize:
        return QSize(360, int(ROW_H * max(1, len(self.bands)) + 6))

    def set_data(self, bands: list[str], lines: dict, note: str = "", selected: tuple | None = None) -> None:
        self.bands = list(bands) or ["5"]
        self.lines = lines
        self.note = note
        self.selected = selected
        self._fit()
        self.update()

    # ---------------------------------------------------------------- geometry
    def _row_rect(self, k: int) -> QRectF:
        return QRectF(PAD_L, 4 + k * ROW_H + 14, max(10.0, self.width() - PAD_L - PAD_R), ROW_H - 14 - 22)

    def _x(self, band: str, mhz: float, r: QRectF) -> float | None:
        segs = _SEGMENTS[band]
        total = sum(b - a for a, b in segs)
        usable = r.width() - _GAP * (len(segs) - 1)
        x = r.left()
        for a, b in segs:
            if a <= mhz <= b:
                return x + (mhz - a) / total * usable
            x += (b - a) / total * usable + _GAP
        return None

    def _mhz(self, band: str, x: float, r: QRectF) -> float | None:
        segs = _SEGMENTS[band]
        total = sum(b - a for a, b in segs)
        usable = r.width() - _GAP * (len(segs) - 1)
        left = r.left()
        for a, b in segs:
            w = (b - a) / total * usable
            if left <= x <= left + w:
                return a + (x - left) / w * (b - a)
            left += w + _GAP
        return None

    def _y(self, dbm: float, r: QRectF) -> float:
        k = (ws.clamp(dbm, BOTTOM_DBM, TOP_DBM) - TOP_DBM) / (BOTTOM_DBM - TOP_DBM)
        return r.top() + k * r.height()

    def channel_at(self, pos: QPointF) -> tuple | None:
        for k, band in enumerate(self.bands):
            r = self._row_rect(k)
            if r.top() - 14 <= pos.y() <= r.bottom() + 22:
                f = self._mhz(band, pos.x(), r)
                if f is None:
                    return None
                ch = min(wc.CHANNELS[band], key=lambda c: abs(wc.channel_mhz(band, c) - f))
                return band, ch
        return None

    # ---------------------------------------------------------------- events
    def mouseMoveEvent(self, ev):
        hit = self.channel_at(ev.position())
        if hit != self._hover:
            self._hover = hit
            self.update()

    def leaveEvent(self, _ev):
        self._hover = None
        self.update()

    def mousePressEvent(self, ev):
        hit = self.channel_at(ev.position())
        if hit is not None and ev.button() == Qt.LeftButton:
            self.channel_clicked.emit(*hit)

    # ---------------------------------------------------------------- painting
    def paintEvent(self, _ev):
        tk = T.tokens()
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        for k, band in enumerate(self.bands):
            self._paint_band(p, tk, band, self._row_rect(k))
        p.end()

    def _paint_band(self, p: QPainter, tk, band: str, r: QRectF) -> None:
        small = T.type("caption", weight=500)
        p.setFont(T.type("caption", weight=600))
        p.setPen(tk.ink)
        p.drawText(QRectF(0, r.top() - 15, self.width(), 14), Qt.AlignLeft | Qt.AlignVCenter, ws.BAND_TITLES[band])
        # plot area and dBm grid
        for (a, b) in _SEGMENTS[band]:
            x0, x1 = self._x(band, a, r), self._x(band, b, r)
            p.setPen(Qt.NoPen)
            p.setBrush(tk.N(2))
            p.drawRect(QRectF(x0, r.top(), x1 - x0, r.height()))
        p.setFont(small)
        for dbm in (-40.0, -60.0, -80.0):
            y = self._y(dbm, r)
            p.setPen(QPen(T.alpha(tk.N(8), 0.35), 1.0))
            p.drawLine(QPointF(r.left(), y), QPointF(r.right(), y))
            p.setPen(tk.muted)
            p.drawText(QRectF(0, y - 7, PAD_L - 4, 14), Qt.AlignRight | Qt.AlignVCenter, f"{dbm:.0f}")
        for dbm, what in ((wi.CCA_PD_DBM, "waits: same channel"), (wi.CCA_ED_DBM, "waits: any energy")):
            y = self._y(dbm, r)
            p.setPen(QPen(T.alpha(tk.ink, 0.45), 1.0, Qt.DashLine))
            p.drawLine(QPointF(r.left(), y), QPointF(r.right(), y))
        # the channel shown on the floor, and the one under the pointer
        for sel, a in ((self.selected, 0.20), (self._hover, 0.12)):
            if sel and sel[0] == band:
                c = wc.channel_mhz(band, sel[1])
                xa, xb = self._x(band, c - 10, r), self._x(band, c + 10, r)
                if xa is not None and xb is not None:
                    p.setPen(Qt.NoPen)
                    p.setBrush(T.alpha(tk.accent_ring, a))
                    p.drawRect(QRectF(xa, r.top(), xb - xa, r.height()))
        # channel numbers
        p.setPen(tk.muted)
        p.setFont(small)
        chans = wc.CHANNELS[band]
        step = {"2.4": 1, "5": 2, "6": 4}[band]
        for i, ch in enumerate(chans):
            if i % step and ch not in (36, 100):
                continue
            x = self._x(band, wc.channel_mhz(band, ch), r)
            if x is None:
                continue
            p.drawText(QRectF(x - 14, r.bottom() + 1, 28, 12), Qt.AlignCenter, str(ch))
        # the radios: weakest first so the strongest sit on top
        lines = sorted(self.lines.get(band, []), key=lambda t: t[1])
        labels = []
        for e, dbm, serving in lines:
            col = QColor(tk.N(9)) if e.neighbour else radio_color(e.index)
            path = QPainterPath()
            c, w = e.centre_mhz, e.width
            pts = []
            for off in (-1.5 * w, -w, -w / 2 - 1, -w / 2 + 1, w / 2 - 1, w / 2 + 1, w, 1.5 * w):
                f = c + off
                x = self._x(band, ws.clamp(f, _SEGMENTS[band][0][0], _SEGMENTS[band][-1][1]), r)
                if x is None:
                    continue
                pts.append(QPointF(x, self._y(dbm + wc.mask_db(off, w), r)))
            if len(pts) < 2:
                continue
            path.moveTo(pts[0].x(), r.bottom())
            for q in pts:
                path.lineTo(q)
            path.lineTo(pts[-1].x(), r.bottom())
            fill = QColor(col)
            fill.setAlphaF(0.10 if e.neighbour else 0.22)
            p.setBrush(fill)
            p.setPen(QPen(col, 2.4 if serving else 1.3, Qt.DashLine if e.neighbour else Qt.SolidLine))
            p.drawPath(path)
            top = self._y(dbm, r)
            xc = self._x(band, ws.clamp(c, _SEGMENTS[band][0][0], _SEGMENTS[band][-1][1]), r)
            if xc is not None and dbm > BOTTOM_DBM:
                labels.append((xc, top, e, serving, col))
        p.setFont(small)
        used: list[QRectF] = []
        for xc, top, e, serving, col in reversed(labels):           # strongest first gets its place
            text = e.name + (" ★" if serving else "")
            wpx = T.fm(small).horizontalAdvance(text) + 4
            box = QRectF(ws.clamp(xc - wpx / 2, r.left(), r.right() - wpx), top - 15, wpx, 13)
            while any(box.intersects(u) for u in used) and box.top() > r.top() - 14:
                box.translate(0, -12)
            if any(box.intersects(u) for u in used):
                continue
            used.append(box)
            p.setPen(tk.ink if not e.neighbour else tk.muted)
            p.drawText(box, Qt.AlignCenter, text)
        if not lines:
            p.setPen(tk.muted)
            p.drawText(r, Qt.AlignCenter, "No radio on this band")
