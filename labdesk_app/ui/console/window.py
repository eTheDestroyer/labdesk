"""The LabDesk console window: tab strip (header), terminal well (body), status line.

One window for all device consoles, owned by LabDesk; no toolbar, no menu bar.  It has no parent window,
so it stays typeable while a LabDesk dialog is open (LabDesk dialogs are window-modal to the main window).
Linux only (the sessions run on ptys).
"""
from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import QPoint, QPointF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QGuiApplication, QIcon, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QApplication, QMenu, QStackedWidget, QVBoxLayout, QWidget

from ... import config
from .. import dialogs
from .. import theme as T
from . import (CHROME_H, DEFAULT_COLUMNS, MIN_COLUMNS, MIN_ROWS, PAD_X, default_rows,  # noqa: F401
               window_size)
from .session import ENDED, LIVE, STARTED, PtySession, reap_all, status_text
from .statusline import ConsoleStatusLine
from .tabstrip import ConsoleTabStrip
from .view import TerminalView, cell_metrics, grid_check

def _check_icon(colour: str, on: bool, dpr: float = 1.0) -> QIcon:
    """A painted check mark in the console's own colour (no stock check box in the console menu)."""
    pm = QPixmap(round(14 * dpr), round(14 * dpr))
    pm.setDevicePixelRatio(dpr)
    pm.fill(Qt.transparent)
    if on:
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setPen(QPen(QColor(colour), 1.8, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        p.drawPolyline([QPointF(2.5, 7.5), QPointF(5.8, 10.5), QPointF(11.5, 3.5)])
        p.end()
    return QIcon(pm)


@dataclass
class ConsoleTab:
    label: str
    cmd: object                  # launchers.ConsoleCommand
    session: PtySession
    view: TerminalView
    busy: int = 0                # > 0: LabDesk is typing that many show commands on this console line

    @property
    def meta(self) -> dict:
        return getattr(self.cmd, "meta", None) or {}


class ConsoleWindow(QWidget):
    notice = Signal(str, str)            # text, kind - for the LabDesk event log
    closed = Signal()

    def __init__(self, prefs, parent=None):
        super().__init__(parent)
        self.prefs = prefs
        self.tabs: list[ConsoleTab] = []
        self._force_close = False
        self.setWindowIcon(QIcon(str(config.ICON_PATH)))
        self.setWindowTitle("LabDesk console")
        self.setAttribute(Qt.WA_StyledBackground, False)
        pal = prefs.palette()
        self.strip = ConsoleTabStrip(pal, self)
        self.stack = QStackedWidget(self)
        self.status = ConsoleStatusLine(pal, self)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        lay.addWidget(self.strip)
        lay.addWidget(self.stack, 1)
        lay.addWidget(self.status)
        self.strip.selected.connect(self.select)
        self.strip.close_requested.connect(self.close_tab)
        self.strip.menu_requested.connect(self.show_menu)
        prefs.changed.connect(self._apply_prefs)
        self._apply_window_colours()
        self._set_minimum()
        if not self._restore_geometry():
            self.resize(*self.default_size())
        self.grid_ok = grid_check(prefs.font_pt)         # start-up assertion: glyph k sits at k x cell_w

    # ------------------------------------------------------------------ geometry
    def cell(self) -> tuple[int, int]:
        return cell_metrics(self.prefs.font_pt, float(self.devicePixelRatioF() or 1.0))

    def default_size(self) -> tuple[int, int]:
        scr = self.screen() or QGuiApplication.primaryScreen()
        avail = scr.availableGeometry().height() if scr is not None else 1080
        cell = self.cell()
        return window_size(DEFAULT_COLUMNS, default_rows(avail, cell[1]), cell)

    def _set_minimum(self) -> None:
        self.setMinimumSize(*window_size(MIN_COLUMNS, MIN_ROWS, self.cell()))

    def grid(self) -> tuple[int, int]:
        """(columns, rows) the terminal area holds at the current window size."""
        cw, ch = self.cell()
        return max(1, (self.width() - 2 * PAD_X) // cw), max(2, (self.height() - CHROME_H) // ch)

    def _restore_geometry(self) -> bool:
        try:
            g = self.prefs.settings.value("console/geometry")
            return bool(g) and self.restoreGeometry(g)
        except Exception:  # noqa: BLE001
            return False

    def _apply_window_colours(self) -> None:
        # the stack shows through for a frame when a tab is added: paint it in the well colour
        self.stack.setStyleSheet(f"QStackedWidget {{ background: {self.prefs.palette().well}; border: none; }}")

    # ------------------------------------------------------------------ tabs
    @property
    def current(self) -> int:
        return self.strip.current

    def tab_of(self, view_or_session) -> int:
        for i, t in enumerate(self.tabs):
            if t.view is view_or_session or t.session is view_or_session:
                return i
        return -1

    def live_count(self) -> int:
        return sum(1 for t in self.tabs if t.session.alive)

    def open(self, cmd) -> str:
        """Open ``cmd`` (a ConsoleCommand): 'switched' to its live tab, 'restarted' in its ended tab, or
        a 'new' tab at the right."""
        argv = list(cmd.argv)
        key = (getattr(cmd, "meta", None) or {}).get("key")
        for i, t in enumerate(self.tabs):
            if t.session.alive and list(t.cmd.argv) == argv:
                self.select(i)
                self.bring_to_front()
                return "switched"
        for i, t in enumerate(self.tabs):
            if not t.session.alive and ((key and t.meta.get("key") == key) or list(t.cmd.argv) == argv):
                t.cmd, t.label = cmd, cmd.title
                self.strip.set_label(i, cmd.title)
                t.view.scroll_to_bottom()
                # same tab, scrollback kept: start on a fresh line with default attributes
                t.session.term.feed(b"\x1b[0m" + (b"\r\n" if t.session.screen.cursor.x else b""))
                t.session.start(argv, dict(cmd.env))
                self.select(i)
                self.bring_to_front()
                return "restarted"
        self._add_tab(cmd)
        self.bring_to_front()
        return "new"

    def _add_tab(self, cmd) -> int:
        cols, rows = self.grid()
        session = PtySession(cols, rows, self.prefs.scrollback, self)
        meta = getattr(cmd, "meta", None) or {}
        view = TerminalView(session, self.prefs.palette(), self.prefs.font_pt, meta.get("node_definition"),
                            self.prefs.highlight, self.stack)
        tab = ConsoleTab(cmd.title, cmd, session, view)
        self.tabs.append(tab)
        self.stack.addWidget(view)
        i = self.strip.add_tab(cmd.title)
        session.state_changed.connect(lambda _s, s=session: self._session_state(s))
        # 'output', not 'updated': a resize (text size, window size) is not news from the device
        session.output.connect(lambda s=session: self._session_output(s))
        session.bell.connect(lambda s=session: self.strip.ring_bell(self.tab_of(s)))
        view.message.connect(self.status.show_message)
        view.grid_changed.connect(lambda *_a: self._refresh_status())
        view.switch_tab.connect(self.step_tab)
        view.zoom.connect(self.prefs.zoom)
        view.menu_requested.connect(self.show_menu)
        view.scrolled.connect(self._refresh_right)       # "N lines up" follows wheel / keys / handle at once
        session.start(list(cmd.argv), dict(cmd.env))
        self.select(i)
        return i

    def select(self, i: int) -> None:
        if not (0 <= i < len(self.tabs)):
            return
        self.strip.set_current(i)
        self.stack.setCurrentWidget(self.tabs[i].view)
        self.tabs[i].view.setFocus(Qt.OtherFocusReason)        # focus always returns to the terminal
        self._refresh_status()
        self._refresh_title()

    def step_tab(self, step: int) -> None:
        if self.tabs:
            self.select((self.current + step) % len(self.tabs))

    def close_tab(self, i: int, ask: bool = True) -> bool:
        if not (0 <= i < len(self.tabs)):
            return False
        t = self.tabs[i]
        if ask and t.session.alive and not dialogs.confirm(
                self, "Close console", f"Close the console of {t.label}?", "Close console",
                detail="The session to the device ends. LabDesk keeps probing the device again afterwards."):
            return False
        self._drop_tab(i)
        if not self.tabs:
            self._force_close = True
            self.close()
        return True

    def _drop_tab(self, i: int) -> None:
        t = self.tabs.pop(i)
        was_current = i == self.current
        self.strip.remove_tab(i)
        self.stack.removeWidget(t.view)
        t.session.close()
        t.view.deleteLater()
        if self.tabs:
            self.select(min(i, len(self.tabs) - 1) if was_current else self.strip.current)

    def set_busy(self, node_key: str, busy: bool, commands: int = 0) -> None:
        """LabDesk types (or stopped typing) a show script on the console line of ``node_key``."""
        for t in self.tabs:
            if t.meta.get("key") == node_key:
                t.busy = max(1, int(commands)) if busy else 0
        self._refresh_status()

    # ------------------------------------------------------------------ session events
    def _session_state(self, session) -> None:
        i = self.tab_of(session)
        if i < 0:
            return
        self.strip.set_state(i, session.state, session.status)
        if session.state == ENDED:
            self.tabs[i].busy = 0
        if i == self.current:
            self._refresh_status()
        self.tabs[i].view.update()

    def _session_output(self, session) -> None:
        i = self.tab_of(session)
        if i >= 0 and i != self.current:
            self.strip.set_unseen(i, True)
        elif i == self.current:
            self._refresh_right()

    def _state_text(self, t: ConsoleTab) -> tuple[str, bool, bool]:
        s = t.session
        if s.state == STARTED:
            return "session started - waiting for output", False, False
        if s.state == LIVE:
            if t.busy:
                n = t.busy
                return (f"LabDesk is running {n} show command{'s' if n != 1 else ''} on this console - "
                        "wait before typing"), False, False
            kind = t.meta.get("kind", "")
            hint = "  ·  Ctrl-] leaves the console" if kind in ("cml", "kvm", "telnet") else ""
            return "session live" + hint, False, False
        if s.error:
            return s.error, True, True
        failed = s.status not in (0, None)
        return status_text(s.status) + " - open the device again to reconnect", True, failed

    def _refresh_status(self) -> None:
        if not (0 <= self.current < len(self.tabs)):
            self.status.set_state("")
            self.status.set_right("")
            return
        self.status.set_state(*self._state_text(self.tabs[self.current]))
        self._refresh_right()

    def _refresh_right(self) -> None:
        if not (0 <= self.current < len(self.tabs)):
            return
        t = self.tabs[self.current]
        back = t.view.scrolled_back
        size = f"{t.session.screen.columns} × {t.session.screen.lines}"
        self.status.set_right(f"{back} line{'s' if back != 1 else ''} up  ·  {size}" if back else size)

    def _refresh_title(self) -> None:
        if not (0 <= self.current < len(self.tabs)):
            self.setWindowTitle("LabDesk console")
            return
        t = self.tabs[self.current]
        lab = t.meta.get("lab_title") or ""
        self.setWindowTitle(" - ".join(x for x in (t.label, lab, "LabDesk console") if x))

    # ------------------------------------------------------------------ look
    def _apply_prefs(self) -> None:
        pal = self.prefs.palette()
        self.strip.set_palette(pal)
        self.status.set_palette(pal)
        self._apply_window_colours()
        self._set_minimum()
        for t in self.tabs:
            t.view.set_palette(pal)
            t.view.set_highlight(self.prefs.highlight)
            if t.view.pt != self.prefs.font_pt:
                t.view.set_point_size(self.prefs.font_pt)
        self._refresh_status()

    def show_menu(self, gpos: QPoint) -> None:
        view = self.tabs[self.current].view if 0 <= self.current < len(self.tabs) else None
        self.build_menu(view).exec(gpos)
        if view:
            view.setFocus(Qt.OtherFocusReason)

    def build_menu(self, view=None) -> QMenu:
        """The console menu (the three dots, or a right click in the terminal)."""
        pal = self.prefs.palette()
        m = QMenu(self)
        m.setFont(T.type("body"))
        m.setStyleSheet(
            f"QMenu {{ background: {pal.chrome}; color: {pal.bright}; border: 1px solid {pal.hairline}; "
            f"padding: 4px; }}"
            f"QMenu::item {{ padding: 5px 22px 5px 22px; border-radius: 4px; background: transparent; }}"
            f"QMenu::item:selected {{ background: {pal.raised}; color: {pal.bright}; }}"
            f"QMenu::item:disabled {{ color: {pal.dim}; }}"
            f"QMenu::separator {{ height: 1px; background: {pal.hairline}; margin: 4px 6px; }}"
            f"QMenu::icon {{ left: 6px; }}")
        a = m.addAction("Copy\tCtrl+Shift+C")
        a.setEnabled(bool(view and view.selected_text()))
        if view:
            a.triggered.connect(lambda _c=False: view.copy())
        a = m.addAction("Paste\tCtrl+Shift+V")
        a.setEnabled(bool(view and view.session.alive))
        if view:
            a.triggered.connect(lambda _c=False: view.paste())
        m.addSeparator()
        m.addAction("Larger text\tCtrl+Alt+=").triggered.connect(lambda _c=False: self.prefs.zoom(1))
        m.addAction("Smaller text\tCtrl+Alt+-").triggered.connect(lambda _c=False: self.prefs.zoom(-1))
        m.addAction("Reset text size\tCtrl+Alt+0").triggered.connect(lambda _c=False: self.prefs.zoom(0))
        m.addSeparator()
        for title, name in (("Neon green console", "neon"), ("Softer green", "soft"),
                            ("Highlight prompts and errors", "highlight")):
            on = bool(getattr(self.prefs, name))
            a = m.addAction(_check_icon(pal.body if name != "soft" or self.prefs.neon else pal.dim, on,
                                        float(self.devicePixelRatioF() or 1.0)), title)
            a.setEnabled(name != "soft" or self.prefs.neon)
            a.triggered.connect(lambda _c=False, n=name, v=not on: self.prefs.set(n, v))
        return m

    # ------------------------------------------------------------------ window
    def bring_to_front(self) -> None:
        if self.isMinimized():
            self.showNormal()
        self.show()
        self.raise_()
        self.activateWindow()
        h = self.windowHandle()
        if h is not None:
            h.requestActivate()

    def alert_if_hidden(self, done=None, delay_ms: int = 300) -> None:
        """300 ms after a raise: when the compositor did not activate the window, flag it in the task bar.
        ``done(active: bool)`` reports the result."""
        def check():
            active = self.isActiveWindow()
            if not active:
                QApplication.alert(self)
            if done is not None:
                done(active)
        QTimer.singleShot(delay_ms, check)

    def changeEvent(self, ev):
        super().changeEvent(ev)
        if ev.type() == ev.Type.ActivationChange:
            self.strip.set_window_active(self.isActiveWindow())

    def close_all(self) -> None:
        """Close the window and end every session without asking (LabDesk itself is closing)."""
        self._force_close = True
        self.close()

    def closeEvent(self, ev):
        live = self.live_count()
        if live and not self._force_close:
            verb = "Close console" if live == 1 else f"Close {live} consoles"
            text = ("The console is still open." if live == 1 else f"{live} consoles are still open.")
            if not dialogs.confirm(self, "Close consoles" if live != 1 else "Close console", text, verb,
                                   detail="Closing the window ends " + ("its session." if live == 1
                                                                         else "their sessions.")):
                ev.ignore()
                return
        self._force_close = False
        try:
            self.prefs.settings.setValue("console/geometry", self.saveGeometry())
        except Exception:  # noqa: BLE001
            pass
        while self.tabs:
            self._drop_tab(len(self.tabs) - 1)
        self._refresh_status()
        self._refresh_title()
        super().closeEvent(ev)
        self.closed.emit()

    def shutdown(self) -> None:
        """Application exit: close without asking and wait for the children to be gone."""
        if self.isVisible() or self.tabs:
            self.close_all()
        reap_all()
