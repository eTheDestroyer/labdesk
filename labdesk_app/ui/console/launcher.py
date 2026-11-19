"""ConsoleWindowLauncher: the launcher MainWindow installs so device consoles open as tabs of LabDesk's own
console window (``launchers.py`` stays free of Qt; Konsole remains the Linux fallback).  On Windows the
device consoles are in-process streams (CML: paramiko, VMware VMs: serial pipe); management ssh / telnet
to a device still opens in Windows Terminal.

``open(cmd)`` is called on a worker thread: it validates the command, hands it to the GUI thread through
a queued signal and returns at once - it never waits for the tab (no timeout can report a failure for a
tab that opens a moment later).
"""
from __future__ import annotations

import os

from PySide6.QtCore import QObject, Qt, Signal

from ...settings import app_settings, as_bool
from .. import theme as T
from . import palette as P

ALLOWED = ("ssh", "virsh", "telnet")


class ConsolePrefs(QObject):
    """The console's look, persisted in QSettings (console/neon, console/soft, console/highlight,
    console/font_pt, console/scrollback) and applied live to every tab."""
    changed = Signal()

    def __init__(self, settings=None, parent=None):
        super().__init__(parent)
        self.settings = settings if settings is not None else app_settings()
        st = self.settings
        self.neon = as_bool(st.value("console/neon", True), True)
        self.soft = as_bool(st.value("console/soft", False), False)
        self.highlight = as_bool(st.value("console/highlight", True), True)
        self.font_pt = self._pt(st.value("console/font_pt", T.CONSOLE_PT))
        try:
            self.scrollback = max(100, int(st.value("console/scrollback", 50_000)))
        except (TypeError, ValueError):
            self.scrollback = 50_000

    @staticmethod
    def _pt(v) -> int:
        try:
            return max(T.CONSOLE_PT_MIN, min(T.CONSOLE_PT_MAX, int(float(v))))
        except (TypeError, ValueError):
            return T.CONSOLE_PT

    def palette(self) -> P.ConsolePalette:
        return P.pick(self.neon, self.soft)

    def set(self, name: str, value) -> None:
        """Change 'neon' | 'soft' | 'highlight' | 'font_pt', save it and apply it live."""
        value = self._pt(value) if name == "font_pt" else bool(value)
        if getattr(self, name) == value:
            return
        setattr(self, name, value)
        self.settings.setValue(f"console/{name}", value)
        self.changed.emit()

    def zoom(self, step: int) -> None:
        """+1 larger, -1 smaller, 0 reset (8 to 20 pt in 1 pt steps, default 11)."""
        self.set("font_pt", T.CONSOLE_PT if step == 0 else self.font_pt + step)


class ConsoleWindowLauncher(QObject):
    """Launcher contract: ``open(cmd) -> str`` (off the GUI thread).  Create it on the GUI thread."""
    supports_look = True
    notice = Signal(str, str)                # text, kind ('info' | 'ok' | 'warn') for the event log
    _open_requested = Signal(object)
    _busy_requested = Signal(str, bool, int)

    def __init__(self, prefs: ConsolePrefs | None = None, parent=None):
        super().__init__(parent)
        self.prefs = prefs or ConsolePrefs(parent=self)
        self.window = None                   # created with the first console, never before
        self._open_requested.connect(self._open_gui, Qt.QueuedConnection)
        self._busy_requested.connect(self._busy_gui, Qt.QueuedConnection)

    # the View menu's existing 'Neon green console' switch drives this attribute
    @property
    def neon(self) -> bool:
        return self.prefs.neon

    @neon.setter
    def neon(self, on: bool) -> None:
        self.prefs.set("neon", on)

    # ------------------------------------------------------------------ any thread
    def open(self, cmd) -> str:
        meta = getattr(cmd, "meta", None) or {}
        if not meta.get("transport") and os.name == "nt":
            # Windows: management ssh / telnet to a device need a real console - Windows Terminal
            from ...launchers import WindowsConsoleLauncher
            return WindowsConsoleLauncher().open(cmd)
        prog = os.path.basename(cmd.argv[0]) if cmd.argv else ""
        if meta.get("transport"):
            self._open_requested.emit(cmd)
            return f"LabDesk console tab '{cmd.title}'"
        if prog not in ALLOWED:
            raise ValueError(f"the LabDesk console runs only ssh, virsh or telnet (not {prog or 'nothing'})")
        self._open_requested.emit(cmd)
        return f"LabDesk console tab '{cmd.title}'"

    def set_busy(self, node_key: str, busy: bool, commands: int = 0) -> None:
        self._busy_requested.emit(str(node_key), bool(busy), int(commands))

    # ------------------------------------------------------------------ GUI thread
    def _ensure_window(self):
        if self.window is None:
            from .window import ConsoleWindow
            self.window = ConsoleWindow(self.prefs)
        return self.window

    def _open_gui(self, cmd) -> None:
        win = self._ensure_window()
        how = win.open(cmd)
        if how == "switched":
            def report(active: bool):
                self.notice.emit(f"{cmd.title} console already open - switched to its tab"
                                 + ("" if active else " (look for the LabDesk console window)"), "info")
            win.alert_if_hidden(report)
        elif how == "restarted":
            self.notice.emit(f"{cmd.title} console restarted in its tab", "info")

    def _busy_gui(self, node_key: str, busy: bool, commands: int) -> None:
        if self.window is not None:
            self.window.set_busy(node_key, busy, commands)

    def sessions(self) -> list[dict]:
        if self.window is None:
            return []
        return [{"id": i, "title": t.label, "state": t.session.state} for i, t in enumerate(self.window.tabs)]

    def live_count(self) -> int:
        return self.window.live_count() if self.window is not None else 0

    def shutdown(self) -> None:
        """LabDesk is closing: end every console session."""
        if self.window is not None:
            self.window.shutdown()
