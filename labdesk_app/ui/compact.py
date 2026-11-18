"""Compact layout mode (L10): one ``compactChanged(bool)`` signal for every subscriber.

View ▸ Layout: Auto / Compact / Full (persisted).  Auto turns compact on below 1450 x 830 and off again above
1550 x 890 (hysteresis around the 1500 x 860 threshold), debounced by 150 ms so docks do not flap while the
window is being resized.  On a 1920 x 1080 screen it never triggers.
"""
from __future__ import annotations

from PySide6.QtCore import QEvent, QObject, QSize, QTimer, Signal

from ..settings import app_settings

MODES = (("auto", "Auto"), ("compact", "Compact"), ("full", "Full"))
ON_W, ON_H = 1450, 830          # auto: compact below either
OFF_W, OFF_H = 1550, 890        # auto: full again above both


class CompactController(QObject):
    compactChanged = Signal(bool)     # noqa: N815  (Qt naming)

    def __init__(self, window, settings=None):
        super().__init__(window)
        self.win = window
        self.settings = settings if settings is not None else app_settings()
        self.mode = str(self.settings.value("ui/layout", "auto"))
        if self.mode not in dict(MODES):
            self.mode = "auto"
        self.compact = False
        self._size = QSize()
        self._t = QTimer(self)
        self._t.setSingleShot(True)
        self._t.setInterval(150)
        self._t.timeout.connect(self._evaluate)
        window.installEventFilter(self)

    def set_mode(self, mode: str) -> None:
        if mode not in dict(MODES):
            return
        self.mode = mode
        self.settings.setValue("ui/layout", mode)
        self._evaluate()

    def want(self, size: QSize) -> bool:
        if self.mode == "compact":
            return True
        if self.mode == "full":
            return False
        w, h = size.width(), size.height()
        if self.compact:                       # stay compact until clearly large again
            return not (w > OFF_W and h > OFF_H)
        return w < ON_W or h < ON_H

    def _evaluate(self) -> None:
        size = self.win.size()
        c = self.want(size)
        if c != self.compact:
            self.compact = c
            self.compactChanged.emit(c)

    def evaluate_now(self) -> None:
        self._t.stop()
        self._evaluate()

    def eventFilter(self, obj, ev):
        if obj is self.win and ev.type() == QEvent.Resize:
            self._t.start()
        return False
