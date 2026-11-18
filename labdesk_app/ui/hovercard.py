"""Hover card: a small themed live-info card shown ~400 ms after the pointer rests on a device tile.

It is a child of the canvas viewport (an overlay like the legend), so it never takes focus, never becomes a
separate window (Wayland-safe) and lets every click / double-click / wheel through
(``WA_TransparentForMouseEvents``).  The canvas hides it on press, drag, wheel / zoom, pan, key press and leave,
and refreshes it while visible when new facts arrive (``LabView.refresh_hover_card``).

Content comes from :func:`hovercard_model.build_card` (pure); this module only paints it with the theme tokens
(light / dark / KDE colours).
"""
from __future__ import annotations

from PySide6.QtCore import QPoint, QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFontMetricsF, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import QWidget

from . import theme as T
from .hovercard_model import CardModel, Row, Section

HOVER_DELAY_MS = 400


class HoverCard(QWidget):
    W = 360                 # card width (logical px)
    PAD = 14
    SH = 10                 # shadow margin around the card
    GAP = 10                # section gap
    MAX_H = 640

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self.setObjectName("hovercard")
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WA_NoSystemBackground, True)
        self.setAutoFillBackground(False)
        self.setFocusPolicy(Qt.NoFocus)
        self.model: CardModel | None = None
        self.icon: QPixmap | None = None
        self.node_key: str | None = None
        self._ops: list = []
        self._h = 0.0
        self._anchor: QPoint | None = None
        self.hide()

    # ------------------------------------------------------------------ fonts
    @staticmethod
    def _fonts() -> dict:
        return {"title": T.type("label", scale=1.08), "sub": T.type("small"), "cap": T.type("caption", upper=True),
                "small": T.type("small"), "mono": T.mono(1.0, "small"), "tag": T.type("caption"),
                "strong": T.type("small", weight=600)}

    # ------------------------------------------------------------------ public
    def show_card(self, model: CardModel, icon: QPixmap | None, anchor: QPoint) -> None:
        self.node_key = model.key
        self._anchor = QPoint(anchor)
        self.set_model(model, icon)
        self.show()
        self.raise_()

    def set_model(self, model: CardModel, icon: QPixmap | None = None) -> None:
        self.model = model
        if icon is not None:
            self.icon = icon
        self._layout()
        self.resize(int(self.W + 2 * self.SH), int(min(self._h, self.MAX_H) + 2 * self.SH))
        if self._anchor is not None:
            self._place(self._anchor)
        self.update()

    def dismiss(self) -> None:
        if self.isVisible():
            self.hide()
        self.node_key = None
        self._anchor = None

    def reposition(self) -> None:
        """Called by the view on pan / resize: the card belongs to a pointer position, so it just goes away."""
        self.dismiss()

    def _place(self, anchor: QPoint) -> None:
        par = self.parentWidget()
        if par is None:
            return
        pw, ph = par.width(), par.height()
        w, h = self.width(), self.height()
        x = anchor.x() + 18 - self.SH
        y = anchor.y() + 20 - self.SH
        if x + w > pw - 4:
            x = anchor.x() - 14 - w + self.SH          # flip to the left of the pointer
        if y + h > ph - 4:
            y = max(4 - self.SH, ph - 4 - h)           # slide up, stay inside the canvas
        x = max(4 - self.SH, min(x, pw - w))
        self.move(int(x), int(y))

    # ------------------------------------------------------------------ layout
    def _layout(self) -> None:
        """Turn the model into draw ops [(kind, y, payload)] and the card height."""
        m = self.model
        ops: list = []
        if m is None:
            self._ops, self._h = ops, 0
            return
        f = self._fonts()
        fm = {k: QFontMetricsF(v) for k, v in f.items()}
        pad = self.PAD
        y = pad
        head_h = max(32.0, fm["title"].height() + fm["sub"].height() + 2)
        ops.append(("header", y, head_h))
        y += head_h + 8
        if m.meta:
            ops.append(("meta", y, None))
            rh = fm["small"].height() + 4
            y += rh * len(m.meta) + 2
        for sec in m.sections:
            y += 4
            ops.append(("rule", y, None))
            y += self.GAP - 2
            ops.append(("sectitle", y, sec))
            y += fm["cap"].height() + 5
            if sec.rows:
                rh = max(fm["small"].height(), fm["mono"].height()) + 5
                ops.append(("rows", y, sec))
                y += rh * len(sec.rows)
            if sec.more:
                ops.append(("more", y, sec))
                y += fm["small"].height() + 3
            if sec.note:
                r = fm["small"].boundingRect(QRectF(0, 0, self.W - 2 * pad, 400), Qt.TextWordWrap, sec.note)
                ops.append(("note", y, (sec, r.height())))
                y += r.height() + 3
        y += pad - 4
        self._ops, self._h = ops, y

    # ------------------------------------------------------------------ paint helpers
    @staticmethod
    def _tone(tone: str | None, tk) -> QColor:
        if tone == "ok":
            return T.status_text("up", tk)
        if tone == "warn":
            return T.status_text("warn", tk)
        if tone == "down":
            return T.status_text("down", tk)
        if tone == "muted":
            return tk.N(11)
        return tk.N(12)

    @staticmethod
    def _hue(tag: str) -> str:
        if tag == "EC":
            return T.EC_HUE[1] if T.tokens().dark else T.EC_HUE[0]
        return T.PROTO_HUES.get(tag, T.NEUTRAL)

    def paintEvent(self, ev):  # noqa: N802
        m = self.model
        if m is None:
            return
        tk = T.tokens()
        f = self._fonts()
        fm = {k: QFontMetricsF(v) for k, v in f.items()}
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.TextAntialiasing)
        sh = self.SH
        card = QRectF(sh, sh, self.W, min(self._h, self.MAX_H))
        # soft shadow: a few expanding rounded rects, alpha fading out
        base = QColor(tk.shadow)
        for i in range(sh, 0, -2):
            c = QColor(base)
            c.setAlphaF(min(1.0, base.alphaF() * (0.20 + 0.80 * (1 - i / sh)) * 0.55))
            p.setPen(Qt.NoPen)
            p.setBrush(c)
            p.drawRoundedRect(card.adjusted(-i / 2, -i / 2 + 2, i / 2, i / 2 + 2), T.RADIUS_LG + i / 2, T.RADIUS_LG + i / 2)
        bg = tk.N(2) if tk.dark else tk.N(1)
        p.setBrush(bg)
        p.setPen(QPen(tk.card_border, 1.0))
        p.drawRoundedRect(card.adjusted(0.5, 0.5, -0.5, -0.5), T.RADIUS_LG, T.RADIUS_LG)
        p.save()
        clip = QPainterPath()
        clip.addRoundedRect(card, T.RADIUS_LG, T.RADIUS_LG)
        p.setClipPath(clip)
        p.translate(sh, sh)
        x0, x1 = float(self.PAD), float(self.W - self.PAD)
        for kind, y, payload in self._ops:
            if y > self.MAX_H - 8:
                break
            if kind == "header":
                self._paint_header(p, m, y, payload, x0, x1, f, fm, tk)
            elif kind == "meta":
                self._paint_rows(p, m.meta, y, x0, x1, f, fm, tk, key_w=76.0, meta=True)
            elif kind == "rule":
                p.setPen(QPen(tk.N(6) if not tk.dark else tk.N(5), 1.0))
                p.drawLine(QPointF(0, y + 0.5), QPointF(self.W, y + 0.5))
            elif kind == "sectitle":
                sec: Section = payload
                p.setFont(f["cap"])
                p.setPen(tk.N(11))
                p.drawText(QRectF(x0, y, x1 - x0, fm["cap"].height()), Qt.AlignLeft | Qt.AlignVCenter, sec.title)
                if sec.aside:
                    p.setFont(f["small"])
                    p.drawText(QRectF(x0, y, x1 - x0, fm["cap"].height()), Qt.AlignRight | Qt.AlignVCenter, sec.aside)
            elif kind == "rows":
                sec = payload
                self._paint_rows(p, sec.rows, y, x0, x1, f, fm, tk)
            elif kind == "more":
                p.setFont(f["small"])
                p.setPen(tk.N(11))
                p.drawText(QRectF(x0, y, x1 - x0, fm["small"].height()), Qt.AlignLeft | Qt.AlignVCenter,
                           f"+{payload.more} more")
            elif kind == "note":
                sec, h = payload
                p.setFont(f["small"])
                p.setPen(self._tone(sec.note_tone or "muted", tk))
                p.drawText(QRectF(x0, y, x1 - x0, h + 2), Qt.AlignLeft | Qt.TextWordWrap, sec.note)
        p.restore()
        p.end()

    def _paint_header(self, p, m: CardModel, y, h, x0, x1, f, fm, tk):
        ic = 30.0
        if self.icon is not None and not self.icon.isNull():
            p.drawPixmap(QRectF(x0, y + (h - ic) / 2, ic, ic), self.icon, QRectF(self.icon.rect()))
            tx = x0 + ic + 10
        else:
            hue = T.VENDOR_COLORS.get(m.vendor, T.VENDOR_COLORS["generic"])[0]
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(hue))
            p.drawRoundedRect(QRectF(x0, y + (h - 22) / 2, 22, 22), 5, 5)
            tx = x0 + 32
        # state pill on the right
        p.setFont(f["small"])
        pw = fm["small"].horizontalAdvance(m.state_word) + 26
        ph = fm["small"].height() + 6
        pr = QRectF(x1 - pw, y + 2, pw, ph)
        tone_col = self._tone(m.state_tone, tk)
        p.setPen(QPen(T.alpha(tone_col, 0.45), 1.0))
        p.setBrush(T.alpha(tone_col, 0.10))
        p.drawRoundedRect(pr, ph / 2, ph / 2)
        gcol = T.glyph_state_color(m.state_glyph, tk) if m.state_tone != "muted" else tk.N(10)
        T.draw_status_glyph(p, QPointF(pr.left() + 10, pr.center().y()), m.state_glyph, 8.0, gcol, tk)
        p.setPen(tone_col)
        p.drawText(pr.adjusted(17, 0, -6, 0), Qt.AlignLeft | Qt.AlignVCenter, m.state_word)
        # title + subtitle
        tw = pr.left() - 8 - tx
        p.setFont(f["title"])
        p.setPen(tk.N(12))
        th = fm["title"].height()
        p.drawText(QRectF(tx, y, tw, th), Qt.AlignLeft | Qt.AlignVCenter,
                   fm["title"].elidedText(m.title, Qt.ElideRight, tw))
        p.setFont(f["sub"])
        p.setPen(tk.N(11))
        p.drawText(QRectF(tx, y + th, x1 - tx, fm["sub"].height()), Qt.AlignLeft | Qt.AlignVCenter,
                   fm["sub"].elidedText(m.subtitle, Qt.ElideRight, x1 - tx))

    def _paint_rows(self, p, rows: list[Row], y, x0, x1, f, fm, tk, key_w: float | None = None, meta: bool = False):
        has_glyph = any(r.glyph for r in rows)
        gx = x0
        kx = x0 + (14 if has_glyph else 0)
        if key_w is None:
            widths = []
            for r in rows:
                if r.tag:
                    widths.append(fm["tag"].horizontalAdvance(r.text) + 12)
                else:
                    widths.append((fm["mono"] if r.glyph else fm["small"]).horizontalAdvance(r.text))
            key_w = min(118.0, max(widths) if widths else 0.0)
        vx = kx + key_w + 10
        rh = (fm["small"].height() + 4) if meta else (max(fm["small"].height(), fm["mono"].height()) + 5)
        for r in rows:
            cy = y + rh / 2
            if r.glyph:
                T.draw_status_glyph(p, QPointF(gx + 5, cy), r.glyph, 8.0, None, tk)
            # key
            if r.tag:
                hue = QColor(self._hue(r.tag))
                tw = fm["tag"].horizontalAdvance(r.text) + 12
                tr = QRectF(kx, cy - (fm["tag"].height() + 2) / 2, tw, fm["tag"].height() + 2)
                p.setPen(Qt.NoPen)
                p.setBrush(T.alpha(hue, 0.22 if tk.dark else 0.13))
                p.drawRoundedRect(tr, 4, 4)
                p.setFont(f["tag"])
                p.setPen(T.text_for(hue, tk))
                p.drawText(tr, Qt.AlignCenter, r.text)
            else:
                p.setFont(f["mono"] if r.glyph else f["small"])
                p.setPen(tk.N(11) if (meta or not r.glyph) else tk.N(12))
                p.drawText(QRectF(kx, y, key_w, rh), Qt.AlignLeft | Qt.AlignVCenter,
                           (fm["mono"] if r.glyph else fm["small"]).elidedText(r.text, Qt.ElideRight, key_w))
            # note (right) and value
            nw = 0.0
            if r.badge:
                p.setFont(f["tag"])
                bw = fm["tag"].horizontalAdvance(r.badge) + 12
                br = QRectF(x1 - bw, cy - (fm["tag"].height() + 2) / 2, bw, fm["tag"].height() + 2)
                p.setPen(Qt.NoPen)
                p.setBrush(T.alpha(tk.N(11), 0.16 if tk.dark else 0.10))
                p.drawRoundedRect(br, 4, 4)
                p.setPen(tk.N(11))
                p.drawText(br, Qt.AlignCenter, r.badge)
                nw = bw
            elif r.note:
                p.setFont(f["small"])
                nw = min(fm["small"].horizontalAdvance(r.note), (x1 - vx) * 0.55)
                p.setPen(self._tone(r.note_tone or "muted", tk))
                p.drawText(QRectF(x1 - nw, y, nw, rh), Qt.AlignRight | Qt.AlignVCenter,
                           fm["small"].elidedText(r.note, Qt.ElideRight, nw))
            if r.value:
                vf = f["mono"] if r.mono else (f["strong"] if r.tone in ("ok", "warn", "down") else f["small"])
                vfm = QFontMetricsF(vf)
                avail = max(10.0, x1 - vx - (nw + 8 if nw else 0))
                p.setFont(vf)
                p.setPen(self._tone(r.tone, tk))
                p.drawText(QRectF(vx, y, avail, rh), Qt.AlignLeft | Qt.AlignVCenter,
                           vfm.elidedText(r.value, Qt.ElideRight, avail))
            y += rh
