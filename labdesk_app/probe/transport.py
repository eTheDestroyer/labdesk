"""Byte transports for CLI probe bursts (D1 / D6).

* ``TermserverPty``  - ``ssh -tt ... admin@<cml> connect <console-uuid>`` on a local pty,
  driven with ``select()``.  The argv extends :func:`launchers.cml_ssh_base` (same key, same
  known-hosts file) and adds BatchMode so no password / askpass prompt can ever appear.
* ``SshBurstArgv``   - argv builder for the host-KVM (Junos / FortiOS) key-only SSH burst
  transport with ControlMaster/ControlPersist.  Specified and unit tested; the live KVM
  collector is not wired in this release (see docs/telemetry_model.md, D6).

* ``TermserverParamiko`` - Windows (no pty): the same termserver session in-process with paramiko
  (``connect <console-uuid>`` on a pty channel, 250x50), same read / write / close interface, so
  :mod:`.session` drives both identically.

No pexpect: stdlib ``pty`` + ``select`` on Linux; paramiko on Windows.
"""
from __future__ import annotations

import os
import select
import signal
import socket
import struct
import subprocess
import time
from pathlib import Path

from .. import config, launchers, plat, validate

if not plat.IS_WINDOWS:                       # POSIX-only modules (LabDesk.exe must import cleanly)
    import fcntl
    import pty
    import termios

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


class TermserverParamiko:
    """Windows: ``ssh admin@<cml> connect <console-uuid>`` with paramiko (one short-lived session).

    Auth: ~/.ssh/labdesk_ed25519 when present, else CML_ADMIN_PASS from the credentials file (in
    process: the password is never on a command line).  Host key: accept-new into
    ~/.ssh/labdesk_known_hosts, a CHANGED key is refused.  ``channel_factory`` (tests) returns a
    ready channel-like object (recv / sendall / settimeout / closed / exit_status_ready / close)."""

    TERM, COLS, ROWS = "vt100", 250, 50

    def __init__(self, console_uuid: str, user: str = "admin", *, host: str | None = None,
                 channel_factory=None, connect_timeout: float = 5.0):
        self.console_uuid = validate.uuid(console_uuid)
        self.user = validate.username(user)
        self.host = host
        self.connect_timeout = connect_timeout
        self._channel_factory = channel_factory
        self._client = None
        self.chan = None
        self.pid = None                      # in-process: no ssh pid (console_scan never sees it)

    # ---------------------------------------------------------------- connect
    def _password(self) -> str | None:
        try:
            return config.load_env_file(config.CRED_FILE).get("CML_ADMIN_PASS") or None
        except OSError:
            return None

    def _open_channel(self):
        import paramiko                       # Windows-only dependency (requirements.txt marker)
        host = self.host or config.CML_HOST
        client = paramiko.SSHClient()
        kh = Path(config.KNOWN_HOSTS)
        if kh.exists():
            try:
                client.load_host_keys(str(kh))
            except (OSError, paramiko.SSHException):
                pass
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())    # accept-new; changed key -> refused
        kw = dict(hostname=host, port=22, username=self.user, timeout=self.connect_timeout,
                  banner_timeout=self.connect_timeout, auth_timeout=self.connect_timeout,
                  allow_agent=False, look_for_keys=False)
        if Path(config.SSH_KEY).exists():
            kw["key_filename"] = str(config.SSH_KEY)
        else:
            pw = self._password()
            if not pw:
                raise TransportError("no SSH key and no CML password saved")
            kw["password"] = pw
        try:
            client.connect(**kw)
        except paramiko.BadHostKeyException:
            client.close()
            raise TransportError(f"host key of {host} CHANGED - refusing (check {kh})") from None
        except paramiko.AuthenticationException:
            client.close()
            raise TransportError("CML terminal server refused the login") from None
        except (paramiko.SSHException, OSError) as e:
            client.close()
            raise TransportError(f"ssh to {host}: {e}") from None
        try:
            kh.parent.mkdir(parents=True, exist_ok=True)
            client.save_host_keys(str(kh))
        except OSError:
            pass
        self._client = client
        tr = client.get_transport()
        tr.set_keepalive(5)
        chan = tr.open_session(timeout=self.connect_timeout)
        chan.get_pty(term=self.TERM, width=self.COLS, height=self.ROWS)
        chan.exec_command(f"connect {self.console_uuid}")
        return chan

    def start(self) -> None:
        try:
            self.chan = (self._channel_factory or self._open_channel)()
        except TransportError:
            raise
        except Exception as e:  # noqa: BLE001  (paramiko / socket errors -> one error type)
            self.close()
            raise TransportError(f"ssh: {e}") from None

    # ---------------------------------------------------------------- I/O
    def alive(self) -> bool:
        c = self.chan
        return c is not None and not c.closed and not c.exit_status_ready()

    def read(self, timeout: float) -> bytes:
        """Bytes available within ``timeout`` seconds (b"" on timeout). Raises on EOF."""
        if self.chan is None:
            raise TransportError("not started")
        self.chan.settimeout(max(0.0, timeout))
        try:
            data = self.chan.recv(65536)
        except socket.timeout:
            return b""
        except OSError:
            raise TransportError("console connection closed") from None
        if not data:
            raise TransportError("console connection closed")
        return data

    def write(self, data: bytes) -> None:
        if self.chan is None:
            raise TransportError("not started")
        self.chan.settimeout(5.0)
        try:
            self.chan.sendall(data)
        except socket.timeout:
            raise TransportError("write timeout") from None
        except OSError:
            raise TransportError("console connection closed") from None

    def close(self) -> None:
        for obj in (self.chan, self._client):
            if obj is not None:
                try:
                    obj.close()
                except Exception:  # noqa: BLE001
                    pass
        self.chan = None
        self._client = None


def default_transport(console_uuid: str, user: str = "admin"):
    """The probe transport for this OS: ssh on a pty (Linux) or paramiko (Windows)."""
    if plat.IS_WINDOWS:
        return TermserverParamiko(console_uuid, user)
    return TermserverPty(console_uuid, user)


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
