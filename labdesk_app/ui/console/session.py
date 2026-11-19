"""One console session: the device's ssh / virsh / telnet argv as a real child process on a real pty.

The pty parts are POSIX only; on Windows the window uses :class:`.stream_session.StreamSession` (same
interface, in-process ssh / VMware serial pipe), which subclasses this class for its screen handling.

* the child is ``/usr/bin/setsid -c <argv>``: setsid makes it a session leader with the pty as its
  controlling terminal and then execs the program in place, so the pid IS the ssh pid (telemetry finds an
  open console by scanning for ``ssh ... connect <uuid>`` processes; this pid is never registered as a
  probe pid, so probing pauses exactly as it does for any other console of the user)
* output is read only as fast as it is parsed (the kernel pty buffer back-pressures ssh), parsed in
  sub-chunks of 256 bytes and never for more than 8 ms per event-loop pass, so typing stays responsive
  during a long ``show running-config``
* closing: close the pty master (the kernel hangs the session up), wait 2 s, then SIGTERM that pid, then
  SIGKILL it 1 s later.  Only pids started here are ever signalled.
"""
from __future__ import annotations

import os
import struct
import subprocess
import time

if os.name == "posix":                 # Windows uses StreamSession (stream_session.py): no pty there
    import fcntl
    import pty
    import termios

from PySide6.QtCore import QObject, QSocketNotifier, QTimer, Signal

from .screen import DEFAULT_SCROLLBACK, MIN_COLUMNS, Terminal

SETSID = "/usr/bin/setsid"
READ_SIZE = 4096
PENDING_MAX = 16 * 1024
FEED_CHUNK = 256
SLICE_S = 0.008
HANGUP_MS, TERM_MS = 2000, 1000

STARTED, LIVE, ENDED = "started", "live", "ended"

_CLOSING: list = []          # sessions whose child is still being reaped (keeps them alive)


def set_winsize(fd: int, lines: int, columns: int) -> None:
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", lines, columns, 0, 0))


class PtySession(QObject):
    updated = Signal()               # the screen changed (device output, or a resize): repaint
    output = Signal()                # bytes of the device were parsed (never emitted for a resize)
    state_changed = Signal(str)      # STARTED / LIVE / ENDED
    bell = Signal()

    def __init__(self, columns: int = 110, lines: int = 42, scrollback: int = DEFAULT_SCROLLBACK, parent=None):
        super().__init__(parent)
        self.term = Terminal(max(MIN_COLUMNS, columns), lines, scrollback)
        self.screen = self.term.screen
        self.screen.on_reply = lambda s: self.write(s.encode("utf-8", "replace"))
        self.state = ENDED
        self.status: int | None = None
        self.error = ""                 # why the child could not start
        self.argv: list[str] = []
        self.proc: subprocess.Popen | None = None
        self.fd = -1
        self._rn: QSocketNotifier | None = None
        self._wn: QSocketNotifier | None = None
        self._pending = bytearray()
        self._out = bytearray()
        self._eof = False
        self._bells = 0
        self._feed = QTimer(self)
        self._feed.setSingleShot(True)
        self._feed.setInterval(0)
        self._feed.timeout.connect(self._feed_slice)
        self._reap = QTimer(self)
        self._reap.setInterval(40)
        self._reap.timeout.connect(self._poll_child)
        self._close_t0 = 0.0
        self._closing = False
        self._termed = False

    # ------------------------------------------------------------------ start
    def start(self, argv: list[str], env: dict | None = None) -> bool:
        """Run ``argv`` on a new pty.  False (state ENDED, ``error`` set) when the child cannot start."""
        self._drop_fd()
        self.argv = list(argv)
        self.status, self.error = None, ""
        self._pending.clear()
        self._out.clear()
        self._eof = self._closing = self._termed = False
        full_env = dict(os.environ)
        full_env.update(env or {})
        full_env["TERM"] = "linux"
        slave = -1
        try:
            self.fd, slave = pty.openpty()
            set_winsize(self.fd, self.screen.lines, self.screen.columns)
            self.proc = subprocess.Popen([SETSID, "-c", *argv], stdin=slave, stdout=slave, stderr=slave,
                                         close_fds=True, env=full_env)
        except (OSError, ValueError) as e:
            self.proc = None
            self._drop_fd()
            self.error = f"could not start {os.path.basename(argv[0]) if argv else 'the command'}: {e}"
            self._set_state(ENDED)
            return False
        finally:
            if slave >= 0:
                os.close(slave)
        os.set_blocking(self.fd, False)
        self._rn = QSocketNotifier(self.fd, QSocketNotifier.Read, self)
        self._rn.activated.connect(self._readable)
        self._wn = QSocketNotifier(self.fd, QSocketNotifier.Write, self)
        self._wn.setEnabled(False)
        self._wn.activated.connect(self._flush)
        self._set_state(STARTED)
        return True

    @property
    def pid(self) -> int | None:
        return self.proc.pid if self.proc is not None else None

    @property
    def alive(self) -> bool:
        return self.state != ENDED

    @property
    def slices_pending(self) -> bool:
        return bool(self._pending)

    def _set_state(self, state: str) -> None:
        if state != self.state or state == ENDED:
            self.state = state
            self.state_changed.emit(state)

    # ------------------------------------------------------------------ output of the child
    def _readable(self, *_a) -> None:
        if self.fd < 0:
            return
        try:
            data = os.read(self.fd, READ_SIZE)
        except BlockingIOError:
            return
        except OSError:                       # EIO: the slave side is closed - the child is gone
            data = b""
        if not data:
            self._eof = True
            if self._rn is not None:
                self._rn.setEnabled(False)
            if not self._pending:
                self._finish()
            return
        self._pending += data
        if len(self._pending) >= PENDING_MAX and self._rn is not None:
            self._rn.setEnabled(False)        # stop reading: the kernel buffer back-pressures the child
        if not self._feed.isActive():
            self._feed_slice()

    def _feed_slice(self) -> None:
        t0 = time.perf_counter()
        pend = self._pending
        feed = self.term.feed
        fed = bool(pend)
        while pend:
            chunk = bytes(pend[:FEED_CHUNK])
            del pend[:FEED_CHUNK]
            feed(chunk)
            if time.perf_counter() - t0 >= SLICE_S:
                break
        if pend:
            self._feed.start()
        elif self._eof:
            self._finish()
        if not self._eof and self._rn is not None and len(pend) < PENDING_MAX:
            self._rn.setEnabled(True)
        if self.state == STARTED:
            self._set_state(LIVE)
        if self.screen.bells != self._bells:
            self._bells = self.screen.bells
            self.bell.emit()
        self.updated.emit()
        if fed:
            self.output.emit()

    # ------------------------------------------------------------------ input for the child
    def write(self, data: bytes) -> None:
        if self.fd < 0 or not data or self._eof:
            return
        self._out += data
        self._flush()

    def _flush(self, *_a) -> None:
        if self.fd < 0:
            return
        try:
            while self._out:
                n = os.write(self.fd, bytes(self._out[:4096]))
                del self._out[:n]
        except BlockingIOError:
            pass
        except OSError:
            self._out.clear()
        if self._wn is not None:
            self._wn.setEnabled(bool(self._out))

    def resize(self, columns: int, lines: int) -> None:
        """Resize the model (columns only grow) and tell the pty the MODEL size, so both always agree."""
        self.screen.resize(lines, columns)
        if self.fd >= 0:
            try:
                set_winsize(self.fd, self.screen.lines, self.screen.columns)
            except OSError:
                pass
        self.updated.emit()

    # ------------------------------------------------------------------ end
    def _drop_fd(self) -> None:
        for n in (self._rn, self._wn):
            if n is not None:
                n.setEnabled(False)
                n.deleteLater()
        self._rn = self._wn = None
        if self.fd >= 0:
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = -1

    def _finish(self) -> None:
        """EOF on the pty: the child closed its side.  Collect its exit status."""
        self._drop_fd()
        if not self._reap.isActive():
            self._close_t0 = time.monotonic()
            self._reap.start()
        self._poll_child()

    def _poll_child(self) -> None:
        p = self.proc
        rc = p.poll() if p is not None else 0
        if rc is not None:
            self._reap.stop()
            if self in _CLOSING:
                _CLOSING.remove(self)
            if self._closing:                 # the tab is gone: nothing to tell, free the scrollback
                self.status, self.state = rc, ENDED
                self.deleteLater()
            elif self.state != ENDED or self.status is None:
                self.status = rc
                self._set_state(ENDED)
            return
        waited = (time.monotonic() - self._close_t0) * 1000
        if not self._termed and waited >= HANGUP_MS:
            self._termed = True
            try:
                p.terminate()
            except OSError:
                pass
        elif self._termed and waited >= HANGUP_MS + TERM_MS:
            try:
                p.kill()
            except OSError:
                pass

    def close(self) -> None:
        """Close the tab's session: hang up, then SIGTERM after 2 s, SIGKILL 1 s later (non-blocking)."""
        self._closing = True
        self._feed.stop()
        self._pending.clear()
        self._eof = True
        self._drop_fd()
        if self.proc is None or self.proc.poll() is not None:
            if self.proc is not None:
                self.status = self.proc.returncode
            self.state = ENDED
            self.deleteLater()
            return
        if self not in _CLOSING:
            _CLOSING.append(self)
        self._close_t0 = time.monotonic()
        self._reap.start()

    def reap_now(self, hangup_s: float = HANGUP_MS / 1000, term_s: float = TERM_MS / 1000) -> None:
        """Blocking variant of ``close`` for application exit."""
        self._reap.stop()
        self._feed.stop()
        self._drop_fd()
        p = self.proc
        if p is not None and p.poll() is None:
            try:
                p.wait(hangup_s)
            except subprocess.TimeoutExpired:
                p.terminate()
                try:
                    p.wait(term_s)
                except subprocess.TimeoutExpired:
                    p.kill()
                    try:
                        p.wait(1.0)
                    except subprocess.TimeoutExpired:
                        pass
        if self in _CLOSING:
            _CLOSING.remove(self)
        self.status = p.returncode if p is not None else self.status
        self.state = ENDED


def reap_all() -> None:
    """Application exit: finish every session that is still closing."""
    for s in list(_CLOSING):
        s.reap_now()


def status_text(status: int | None) -> str:
    if status is None:
        return "session ended"
    if status < 0:
        return f"session ended (signal {-status})"
    return f"session ended (status {status})"
