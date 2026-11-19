"""StreamSession: a console tab whose bytes come from an in-process stream instead of a child on a pty.

Windows has no pty, so the LabDesk console window there runs CML consoles over paramiko and VMware VM
consoles over the VM's serial named pipe (:mod:`labdesk_app.streams`).  Same interface and signals as
:class:`.session.PtySession` (screen, scrollback, the 8 ms feed slices are inherited), so the window,
the view and the tab strip do not know the difference.

A reader thread pulls bytes from the stream and hands them to the GUI thread with a queued signal; it
waits while more than PENDING_MAX bytes are not parsed yet (the stream's own buffer back-pressures the
device, like the kernel pty buffer does on Linux).  Ctrl-] typed on a VM serial console ends the session
(as ``virsh console`` does on Linux).
"""
from __future__ import annotations

import threading
import time

from PySide6.QtCore import Qt, Signal

from ...streams import StreamClosed, make_stream
from .session import ENDED, LIVE, PENDING_MAX, STARTED, PtySession

CTRL_RBRACKET = b"\x1d"


class StreamSession(PtySession):
    _data = Signal(int, bytes)                # generation, bytes
    _ended = Signal(int, str)                 # generation, error ('' = clean end)

    def __init__(self, transport: dict, columns: int = 110, lines: int = 42, scrollback: int = 50_000, parent=None):
        super().__init__(columns, lines, scrollback, parent)
        self.transport = dict(transport)
        self.stream = None
        self._gen = 0
        self._stop = threading.Event()
        self._room = threading.Event()
        self._room.set()
        self._data.connect(self._on_data, Qt.QueuedConnection)
        self._ended.connect(self._on_ended, Qt.QueuedConnection)

    # ------------------------------------------------------------------ start
    def start(self, argv: list[str], env: dict | None = None) -> bool:
        self._shutdown_stream()
        self.argv = list(argv)
        self.status, self.error = None, ""
        self._pending.clear()
        self._eof = self._closing = False
        self._gen += 1
        self._stop = threading.Event()
        self._room.set()
        stream = make_stream(self.transport, self.screen.columns, self.screen.lines)
        self.stream = stream
        self._set_state(STARTED)
        threading.Thread(target=self._reader, args=(self._gen, stream, self._stop), daemon=True,
                         name="labdesk-console").start()
        return True

    @property
    def pid(self):
        return None

    def _reader(self, gen: int, stream, stop: threading.Event) -> None:
        try:
            stream.open()
        except StreamClosed as e:
            self._ended.emit(gen, str(e))
            return
        except Exception as e:  # noqa: BLE001
            self._ended.emit(gen, f"could not connect: {e}")
            return
        err = ""
        while not stop.is_set():
            self._room.wait(0.5)
            if not self._room.is_set():
                continue
            try:
                data = stream.read(0.25)
            except StreamClosed as e:
                err = "" if stop.is_set() else str(e)
                break
            except Exception as e:  # noqa: BLE001
                err = str(e)
                break
            if data:
                self._data.emit(gen, data)
        try:
            stream.close()
        except Exception:  # noqa: BLE001
            pass
        if not stop.is_set():
            self._ended.emit(gen, err)

    # ------------------------------------------------------------------ GUI thread
    def _on_data(self, gen: int, data: bytes) -> None:
        if gen != self._gen or self._closing:
            return
        self._pending += data
        if len(self._pending) >= PENDING_MAX:
            self._room.clear()
        if not self._feed.isActive():
            self._feed_slice()

    def _feed_slice(self) -> None:
        super()._feed_slice()
        if len(self._pending) < PENDING_MAX:
            self._room.set()

    def _on_ended(self, gen: int, error: str) -> None:
        if gen != self._gen or self._closing:
            return
        self._eof = True
        if error and self.state == STARTED:
            self.error = error                 # never connected: the status line shows why
        if not self._pending:
            self._finish()

    def _finish(self) -> None:
        self.stream = None
        self.status = 0 if not self.error else 255
        self._set_state(ENDED)

    def write(self, data: bytes) -> None:
        if not data or self.stream is None or self.state == ENDED:
            return
        if self.transport.get("type") == "pipe" and CTRL_RBRACKET in data:
            self._shutdown_stream()
            self._eof = True
            if not self._pending:
                self._finish()
            return
        try:
            self.stream.write(data)
        except Exception:  # noqa: BLE001
            pass

    def resize(self, columns: int, lines: int) -> None:
        self.screen.resize(lines, columns)
        if self.stream is not None:
            try:
                self.stream.resize(self.screen.columns, self.screen.lines)
            except Exception:  # noqa: BLE001
                pass
        self.updated.emit()

    # ------------------------------------------------------------------ end
    def _shutdown_stream(self) -> None:
        self._stop.set()
        self._room.set()
        s, self.stream = self.stream, None
        if s is not None:
            threading.Thread(target=_close_quietly, args=(s,), daemon=True).start()

    def close(self) -> None:
        self._closing = True
        self._feed.stop()
        self._pending.clear()
        self._shutdown_stream()
        self.state = ENDED
        self.deleteLater()

    def reap_now(self, *_a, **_kw) -> None:
        self._feed.stop()
        s, self.stream = self.stream, None
        self._stop.set()
        if s is not None:
            _close_quietly(s)
        self.state = ENDED


def _close_quietly(stream) -> None:
    try:
        stream.close()
    except Exception:  # noqa: BLE001
        pass
    time.sleep(0)


__all__ = ["StreamSession", "ENDED", "LIVE", "STARTED"]
