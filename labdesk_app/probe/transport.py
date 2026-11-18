"""Byte transports for CLI probe bursts (D1 / D6).

* ``TermserverPty``  - ``ssh -tt ... admin@<cml> connect <console-uuid>`` on a local pty,
  driven with ``select()``.  The argv extends :func:`launchers.cml_ssh_base` (same key, same
  known-hosts file) and adds BatchMode so no password / askpass prompt can ever appear.
* ``SshBurstArgv``   - argv builder for the host-KVM (Junos / FortiOS) key-only SSH burst
  transport with ControlMaster/ControlPersist.  Specified and unit tested; the live KVM
  collector is not wired in this release (see docs/telemetry_model.md, D6).

No pexpect: stdlib ``pty`` + ``select`` only.
"""
from __future__ import annotations

import fcntl
import os
import pty
import select
import signal
import struct
import subprocess
import termios
import time
from pathlib import Path

from .. import config, launchers, validate

# options that must win over the ones in cml_ssh_base (ssh: first -o value wins, so these are
# inserted right after "ssh")
PROBE_SSH_OPTS = ["-t",                                  # + the "-t" in cml_ssh_base == -tt
                  "-o", "BatchMode=yes",
                  "-o", "ConnectTimeout=5",
                  "-o", "ServerAliveInterval=5",
                  "-o", "ServerAliveCountMax=2",
                  "-o", "PreferredAuthentications=publickey"]


SETSID = "/usr/bin/setsid"


class TransportError(RuntimeError):
    pass


def termserver_argv(console_uuid: str, user: str) -> list[str]:
    base = launchers.cml_ssh_base(user)
    assert base[0] == "ssh"
    return [base[0], *PROBE_SSH_OPTS, *base[1:], "connect", validate.uuid(console_uuid)]


def _probe_env() -> dict[str, str]:
    # deliberately minimal: no DISPLAY / SSH_ASKPASS -> a GUI password dialog can never appear
    return {"PATH": "/usr/bin:/bin:/usr/local/bin", "HOME": os.path.expanduser("~"),
            "TERM": "vt100", "LANG": "C"}


class TermserverPty:
    """One short-lived ssh process on a pty.  Not thread safe; one burst owns it."""

    def __init__(self, console_uuid: str, user: str = "admin", argv: list[str] | None = None):
        self.argv = argv or termserver_argv(console_uuid, user)
        self.proc: subprocess.Popen | None = None
        self.fd: int | None = None

    @property
    def pid(self) -> int | None:
        return self.proc.pid if self.proc else None

    def start(self) -> None:
        master, slave = pty.openpty()
        # wide window: nothing on the router side wraps
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 50, 250, 0, 0))
        # 'setsid -c' makes the pty ssh's CONTROLLING terminal: if LabDesk dies (even SIGKILL) the
        # master closes, the kernel hangs up the session and ssh exits - no orphaned console
        # session is left holding the router line.
        setsid = SETSID if SETSID and os.path.exists(SETSID) else None
        argv = [setsid, "-c", *self.argv] if setsid else self.argv
        try:
            self.proc = subprocess.Popen(argv, stdin=slave, stdout=slave, stderr=slave,
                                         env=_probe_env(), start_new_session=setsid is None, close_fds=True)
        finally:
            os.close(slave)
        self.fd = master
        fl = fcntl.fcntl(master, fcntl.F_GETFL)
        fcntl.fcntl(master, fcntl.F_SETFL, fl | os.O_NONBLOCK)

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def read(self, timeout: float) -> bytes:
        """Bytes available within ``timeout`` seconds (b"" on timeout). Raises on EOF."""
        if self.fd is None:
            raise TransportError("not started")
        r, _, _ = select.select([self.fd], [], [], max(0.0, timeout))
        if not r:
            return b""
        try:
            data = os.read(self.fd, 65536)
        except BlockingIOError:
            return b""
        except OSError:          # EIO: child exited and the pty closed
            raise TransportError("console connection closed") from None
        if not data:
            raise TransportError("console connection closed")
        return data

    def write(self, data: bytes) -> None:
        if self.fd is None:
            raise TransportError("not started")
        end = time.monotonic() + 5
        view = memoryview(data)
        while view:
            try:
                n = os.write(self.fd, view)
                view = view[n:]
            except BlockingIOError:
                if time.monotonic() > end:
                    raise TransportError("write timeout") from None
                select.select([], [self.fd], [], 0.2)

    def close(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            try:
                os.killpg(self.proc.pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
            try:
                self.proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(self.proc.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
                self.proc.wait(timeout=2)
        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = None


# ------------------------------------------------------------------ host-KVM (D6, spec only)
def control_dir() -> Path:
    return config.CACHE_DIR / "ssh"


def ssh_burst_argv(vm: str, mgmt_ip: str, user: str, remote_cmd: str | None = None) -> list[str]:
    """Key-only, non-interactive ssh exec for one Junos/FortiOS burst (D6).

    ControlMaster=auto + ControlPersist=60s keep the handshake cost to the first burst."""
    vm = validate.virsh_name(vm)
    argv = ["ssh", "-o", "BatchMode=yes",
            "-o", "StrictHostKeyChecking=accept-new",
            "-o", f"UserKnownHostsFile={config.DEVICE_KNOWN_HOSTS}",
            "-o", "ConnectTimeout=5",
            "-o", "ServerAliveInterval=5", "-o", "ServerAliveCountMax=2",
            "-o", "LogLevel=ERROR",
            "-o", "PreferredAuthentications=publickey",
            "-o", "IdentitiesOnly=yes", "-i", str(config.SSH_KEY),
            "-o", "ControlMaster=auto", "-o", "ControlPersist=60s",
            "-o", f"ControlPath={control_dir() / (vm + '.sock')}",
            "-l", validate.username(user), validate.host(mgmt_ip)]
    if remote_cmd is not None:
        argv.append(remote_cmd)
    return argv
