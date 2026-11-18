"""The 26 px status line that closes the console window.  LabDesk speaks here (and in the tab strip), never
inside the terminal: session state on the left, a short-lived message, the grid size on the right.  When
the session of the active tab has ended the whole row turns into the 'ended' banner.
"""
from __future__ import annotations

from PySide6.QtCore import QRect, Qt, QTimer
from PySide6.QtGui import QColor, QFontMetricsF, QPainter
from PySide6.QtWidgets import QWidget

from .. import theme as T
from . import STATUS_H
from .palette import PHOSPHOR, ConsolePalette

HEIGHT = STATUS_H
FONT_PT = 10
PAD = 16                     # = the well's side padding


class ConsoleStatusLine(QWidget):
    def __init__(self, pal: ConsolePalette = PHOSPHOR, parent=None):
        super().__init__(parent)
        self.pal = pal
        self.text = ""               # session state of the active tab
        self.ended = False           # the active tab's session has ended
        self.failed = False          # ... with a non-zero status / it could not start
        self.right = ""              # e.g. '110 x 42'
        self.flash = ""              # a message shown for a moment
        self.setFixedHeight(HEIGHT)
        self.setFocusPolicy(Qt.NoFocus)
        self.setAttribute(Qt.WA_OpaquePaintEvent, True)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._clear_flash)

    def set_palette(self, pal: ConsolePalette) -> None:
        self.pal = pal
        self.update()

    def set_state(self, text: str, ended: bool = False, failed: bool = False) -> None:
        self.text, self.ended, self.failed = text, ended, failed
        self.update()

    def set_right(self, text: str) -> None:
        self.right = text
        self.update()

    def show_message(self, text: str, ms: int = 2000) -> None:
        self.flash = text
        self._timer.start(ms)
        self.update()

    def _clear_flash(self) -> None:
        self.flash = ""
        self.update()

    def paintEvent(self, _ev):
        pal = self.pal
        p = QPainter(self)
        banner = self.ended and self.failed
        p.fillRect(self.rect(), QColor(pal.ended_bg if banner else pal.chrome))
        p.fillRect(0, 0, self.width(), 1, QColor(pal.ended_border if banner else pal.hairline))
        font = T.console_font(FONT_PT)
        fm = QFontMetricsF(font)
        p.setFont(font)
        box = QRect(PAD, 1, self.width() - 2 * PAD, HEIGHT - 1)
        right = self.flash or self.right
        rw = int(fm.horizontalAdvance(right)) + 1 if right else 0
        if right:
            p.setPen(QColor(pal.ended_fg if banner else (pal.body if self.flash else pal.dim)))
            p.drawText(box, Qt.AlignVCenter | Qt.AlignRight, right)
        p.setPen(QColor(pal.ended_fg if banner else pal.dim))
        left = QRect(box.x(), box.y(), max(0, box.width() - rw - 24), box.height())
        p.drawText(left, Qt.AlignVCenter | Qt.AlignLeft, fm.elidedText(self.text, Qt.ElideRight, left.width()))
        p.end()
