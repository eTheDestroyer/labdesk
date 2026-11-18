"""A scripted IOS console behind the CML termserver, for probe-session tests (no network)."""
from __future__ import annotations

import re

from labdesk_app.probe.transport import TransportError

BANNER = (b">>> CML2 termserv\r\n\r\nConnecting to console for {host}\r\n"
          b"Connected to CML terminalserver.\r\nEscape character is '^]'.\r\n")


class FakeConsole:
    """mode: 'priv' | 'user' | 'config' | 'more' | 'logged_out' | 'password' | 'username'
    outputs: {command: text}; unknown commands -> '% Invalid input' marker.
    syslog_in: {command: syslog line inserted in the middle of its output}
    hang: set of commands that never answer (sentinel never echoed)."""

    def __init__(self, host="R1", mode="priv", outputs=None, syslog_in=None, hang=None, enable_secret=None):
        self.host = host
        self.mode = mode
        self.outputs = outputs or {}
        self.syslog_in = syslog_in or {}
        self.hang = set(hang or [])
        self.enable_secret = enable_secret
        self.pending = BANNER.replace(b"{host}", host.encode())
        self.written: list[bytes] = []
        self.inbuf = b""
        self.started = False
        self.closed = False
        self.hung = False
        self.awaiting = None          # 'enable_pw' | 'login_pw'
        self.pid = None

    # transport API
    def start(self):
        self.started = True

    def read(self, timeout):
        if self.closed:
            raise TransportError("closed")
        out, self.pending = self.pending, b""
        if not out:
            import time
            time.sleep(min(timeout, 0.005))
        return out

    def write(self, data: bytes):
        self.written.append(data)
        self.inbuf += data
        while b"\r" in self.inbuf:
            line, self.inbuf = self.inbuf.split(b"\r", 1)
            self._line(line.decode())

    def close(self):
        self.closed = True

    # simulation
    def prompt(self):
        if self.mode == "config":
            return f"{self.host}(config)#"
        return f"{self.host}{'#' if self.mode == 'priv' else '>'}"

    def _emit(self, s: str):
        self.pending += s.replace("\n", "\r\n").encode()

    def _line(self, line: str):
        if self.hung:
            return
        if line == "\x03":
            return
        if self.awaiting == "enable_pw":
            self.awaiting = None
            if line == self.enable_secret:
                self.mode = "priv"
                self._emit("\n" + self.prompt())
            else:
                self._emit("\n% Bad secrets\n\n" + self.prompt())
            return
        if self.mode == "logged_out":
            self.mode = "user"
            self._emit("\n**********************\n* IOSv is strictly limited *\n**********************\n"
                       + self.prompt())
            return
        if self.mode == "more":
            self._emit(" --More-- ")
            return
        if self.mode == "password":
            self._emit("\nPassword: ")
            return
        if line == "":
            self._emit("\n" + self.prompt())
            return
        self._emit(line + "\n")
        if line in self.hang:
            self.hung = True
            return
        if line == "enable":
            if self.enable_secret is None:
                self.mode = "priv"
                self._emit(self.prompt())
            else:
                self.awaiting = "enable_pw"
                self._emit("Password: ")
            return
        if line == "disable":
            self.mode = "user"
            self._emit(self.prompt())
            return
        if line == "exit":
            self.mode = "logged_out"
            self._emit("\n\nR1 con0 is now available\n\nPress RETURN to get started.\n")
            return
        if line.startswith("terminal ") or re.match(r"show clock \| include LDK", line):
            self._emit(self.prompt())
            return
        if line.startswith(("ping", "traceroute")):
            if self.mode == "user" and len(line.split()) > 2:      # IOSv '>' rejects every option
                self._emit("                ^\n% Invalid input detected at '^' marker.\n\n" + self.prompt())
                return
            self.inbuf = b""                     # IOS flushes typed-ahead input during ping/trace
        if line in self.outputs:
            text = self.outputs[line]
            if line in self.syslog_in:
                parts = text.split("\n")
                mid = len(parts) // 2
                parts.insert(mid, self.syslog_in[line])
                text = "\n".join(parts)
            if text and not text.endswith("\n"):
                text += "\n"
            self._emit(text + self.prompt())
            return
        self._emit("                ^\n% Invalid input detected at '^' marker.\n\n" + self.prompt())

    @property
    def sent_text(self) -> str:
        return b"".join(self.written).decode()
