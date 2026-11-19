"""Byte streams for device consoles that need no pty (LabDesk console window on Windows).

* :class:`CmlConsoleStream` - ``connect <console-uuid>`` on CML's terminal server, in process with paramiko
  (the same login as the Windows probes: ~/.ssh/labdesk_ed25519, else the saved CML password - nothing to
  type, the password never reaches a command line).
* :class:`PipeStream`       - the serial console of a VMware VM: the named pipe ``\\\\.\\pipe\\labdesk-<vm>``
  VMware serves (vmware.serial_pipe_values).  Polled with PeekNamedPipe so a pending read never blocks a
  write on the same synchronous handle.

Contract (used by ui/console/stream_session.py, from a reader thread + the GUI thread):
``open()`` (may block a few seconds), ``read(timeout) -> bytes`` (b"" = nothing yet; raises
:class:`StreamClosed` at the end), ``write(bytes)``, ``resize(cols, rows)``, ``close()``.

Open consoles are registered in :data:`OPEN_UUIDS` so the probes pause for that device exactly as they do
for an ``ssh ... connect <uuid>`` process (telemetry.open_console_uuids).
"""
from __future__ import annotations

import threading
import time

from . import validate

OPEN_UUIDS: set[str] = set()
_LOCK = threading.Lock()


class StreamClosed(RuntimeError):
    pass


def open_uuids() -> set[str]:
    with _LOCK:
        return set(OPEN_UUIDS)


class CmlConsoleStream:
    TERM = "xterm"

    def __init__(self, console_uuid: str, user: str = "admin", cols: int = 110, rows: int = 42, tr_factory=None):
        self.uuid = validate.uuid(console_uuid)
        self.user = validate.username(user)
        self.cols, self.rows = cols, rows
        self._tr_factory = tr_factory
        self.tr = None

    def open(self) -> None:
        from .probe.transport import TermserverParamiko, TransportError
        tr = (self._tr_factory or TermserverParamiko)(self.uuid, self.user, connect_timeout=10.0)
        tr.TERM, tr.COLS, tr.ROWS = self.TERM, self.cols, self.rows
        try:
            tr.start()
        except TransportError as e:
            raise StreamClosed(str(e)) from None
        self.tr = tr
        with _LOCK:
            OPEN_UUIDS.add(self.uuid)

    def read(self, timeout: float) -> bytes:
        from .probe.transport import TransportError
        if self.tr is None:
            raise StreamClosed("not connected")
        try:
            return self.tr.read(timeout)
        except TransportError as e:
            raise StreamClosed(str(e)) from None

    def write(self, data: bytes) -> None:
        from .probe.transport import TransportError
        if self.tr is None:
            return
        try:
            self.tr.write(data)
        except TransportError:
            pass

    def resize(self, cols: int, rows: int) -> None:
        chan = getattr(self.tr, "chan", None)
        if chan is not None:
            try:
                chan.resize_pty(width=cols, height=rows)
            except Exception:  # noqa: BLE001
                pass

    def close(self) -> None:
        with _LOCK:
            OPEN_UUIDS.discard(self.uuid)
        if self.tr is not None:
            self.tr.close()
            self.tr = None


class PipeStream:
    """Windows named-pipe client (VMware serial port, VMware is the pipe server)."""

    POLL_S = 0.015

    def __init__(self, path: str, connect_timeout: float = 10.0):
        if not path.startswith("\\\\.\\pipe\\"):
            raise ValueError("not a named pipe path")
        self.path = path
        self.connect_timeout = connect_timeout
        self.h = None
        self._k32 = None

    def open(self) -> None:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateFileW.restype = wintypes.HANDLE
        k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                                    wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        k32.WaitNamedPipeW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD]
        k32.PeekNamedPipe.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p,
                                      ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
        k32.ReadFile.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                                 ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
        k32.WriteFile.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                                  ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        self._k32 = k32
        GENERIC_RW, OPEN_EXISTING, INVALID = 0xC0000000, 3, wintypes.HANDLE(-1).value
        end = time.time() + self.connect_timeout
        while True:
            h = k32.CreateFileW(self.path, GENERIC_RW, 0, None, OPEN_EXISTING, 0, None)
            if h and h != INVALID:
                self.h = h
                return
            err = ctypes.get_last_error()
            if time.time() >= end:
                if err == 2:
                    raise StreamClosed("the VM is not running (no serial console pipe) - start it first")
                if err == 231:
                    raise StreamClosed("the VM's serial console is already open in another window")
                raise StreamClosed(f"cannot open the VM's serial console (Windows error {err})")
            if err == 231:
                k32.WaitNamedPipeW(self.path, 1000)
            else:
                time.sleep(0.3)

    def read(self, timeout: float) -> bytes:
        import ctypes
        from ctypes import wintypes
        if self.h is None:
            raise StreamClosed("not connected")
        end = time.time() + max(0.0, timeout)
        avail = wintypes.DWORD(0)
        while True:
            if not self._k32.PeekNamedPipe(self.h, None, 0, None, ctypes.byref(avail), None):
                raise StreamClosed("the VM's serial console closed (VM stopped?)")
            if avail.value:
                break
            if time.time() >= end:
                return b""
            time.sleep(self.POLL_S)
        buf = ctypes.create_string_buffer(min(avail.value, 65536))
        got = wintypes.DWORD(0)
        if not self._k32.ReadFile(self.h, buf, len(buf), ctypes.byref(got), None):
            raise StreamClosed("the VM's serial console closed")
        return buf.raw[:got.value]

    def write(self, data: bytes) -> None:
        import ctypes
        from ctypes import wintypes
        if self.h is None or not data:
            return
        done = wintypes.DWORD(0)
        self._k32.WriteFile(self.h, data, len(data), ctypes.byref(done), None)

    def resize(self, cols: int, rows: int) -> None:
        pass                                  # a serial line has no window size

    def close(self) -> None:
        if self.h is not None and self._k32 is not None:
            self._k32.CloseHandle(self.h)
        self.h = None


def make_stream(transport: dict, cols: int, rows: int):
    kind = transport.get("type")
    if kind == "cml":
        return CmlConsoleStream(transport["uuid"], transport.get("user", "admin"), cols, rows)
    if kind == "pipe":
        return PipeStream(transport["path"])
    raise ValueError(f"unknown console transport {kind!r}")
