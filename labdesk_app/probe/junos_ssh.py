"""Minimal live Junos collector transport: read-only ``show`` commands over SSH to the VM's management address.

Used for host-KVM vJunos routers (their fxp0 address is in ``state.json`` ``labs.<id>.mgmt`` or found by
``kvm.mgmt_ip``).  One SSH connection per burst, one ``exec`` per command (no shell, no pager, no prompt
parsing).  Only commands that start with ``show `` are ever sent - anything else raises before connecting.

Credentials (never logged): device-creds ``<LABEL>_USERNAME`` / ``_PASSWORD``, else ``JUNOS_ADMIN_USER`` /
``JUNOS_ADMIN_PASS`` from cml-credentials.env.  Host key: accept-new into the LabDesk known-hosts file.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from .. import config

_SAFE = re.compile(r"^show [\w .:/|\"*+\-=,]+$")


class JunosError(RuntimeError):
    pass


def check_read_only(cmd: str) -> str:
    """Refuse anything that is not a single plain ``show`` command (no ';', newline, request/set/...)."""
    c = cmd.strip()
    if not _SAFE.match(c) or ";" in c or "\n" in c or re.search(r"\|\s*(save|load|request)\b", c):
        raise JunosError(f"refusing non read-only command: {c[:40]!r}")
    return c


@dataclass
class JunosOutput:
    text: str | None
    error: str | None = None
    wall: float = 0.0


@dataclass
class JunosBurst:
    ok: bool
    outputs: dict[str, JunosOutput] = field(default_factory=dict)
    reason: str = ""
    seconds: float = 0.0


def junos_creds(label: str, creds_detail=None, env: dict | None = None) -> tuple[str | None, str | None, str]:
    """(user, password, source) - device-creds first, then cml-credentials.env JUNOS_ADMIN_*."""
    c = getattr(creds_detail, "creds", None)
    if c is not None and c.username and c.password:
        return c.username, c.password, "device-creds"
    if env is None:
        try:
            env = config.load_env_file(config.CRED_FILE)
        except OSError:
            env = {}
    if env.get("JUNOS_ADMIN_USER") and env.get("JUNOS_ADMIN_PASS"):
        return env["JUNOS_ADMIN_USER"], env["JUNOS_ADMIN_PASS"], "cml-credentials.env"
    return None, None, ""


def run_junos_burst(host: str, user: str, password: str, cmds: list[str], *, timeout: float = 20.0,
                    connect_timeout: float = 6.0, client_factory=None) -> JunosBurst:
    """Run read-only ``cmds`` on ``host``.  ``client_factory`` (tests) returns a paramiko-like client."""
    cmds = [check_read_only(c) for c in cmds]
    t0 = time.monotonic()
    try:
        if client_factory is not None:
            client = client_factory()
        else:
            import paramiko
            client = paramiko.SSHClient()
            kh = Path(config.KNOWN_HOSTS)
            if kh.exists():
                try:
                    client.load_host_keys(str(kh))
                except Exception:  # noqa: BLE001
                    pass
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            client.connect(hostname=host, port=22, username=user, password=password, timeout=connect_timeout,
                           banner_timeout=connect_timeout, auth_timeout=connect_timeout,
                           allow_agent=False, look_for_keys=False)
            try:
                kh.parent.mkdir(parents=True, exist_ok=True)
                client.save_host_keys(str(kh))
            except Exception:  # noqa: BLE001
                pass
    except Exception as e:  # noqa: BLE001  (auth / socket / host key -> one reason, never the password)
        name = type(e).__name__
        why = "login refused" if "Authentication" in name else f"ssh to {host}: {name}"
        return JunosBurst(False, {}, why, time.monotonic() - t0)
    res = JunosBurst(True)
    try:
        for c in cmds:
            if time.monotonic() - t0 > timeout:
                res.outputs[c] = JunosOutput(None, "burst timeout")
                res.ok, res.reason = False, "burst timeout"
                break
            ts = time.time()
            try:
                _i, o, _e = client.exec_command(c, timeout=max(2.0, timeout - (time.monotonic() - t0)))
                text = o.read().decode("utf-8", errors="replace")
                res.outputs[c] = JunosOutput(text, None, ts)
            except Exception as e:  # noqa: BLE001
                res.outputs[c] = JunosOutput(None, type(e).__name__, ts)
    finally:
        try:
            client.close()
        except Exception:  # noqa: BLE001
            pass
    res.seconds = time.monotonic() - t0
    return res


def parse_iface_macs(text: str | None) -> dict[str, str]:
    """``show interfaces ge-* | match "Physical interface|Current address"`` -> {mac (lower): 'ge-0/0/0'}."""
    out: dict[str, str] = {}
    cur = None
    for line in (text or "").splitlines():
        m = re.match(r"^Physical interface:\s*([\w\-/.:]+)", line)
        if m:
            cur = m.group(1)
            continue
        m = re.search(r"Current address:\s*([0-9a-fA-F:]{17})", line)
        if m and cur:
            out[m.group(1).lower()] = cur
    return out


def parse_local_as(text: str | None) -> int | None:
    """``show configuration routing-options autonomous-system | display json`` -> 65000 (None if unset)."""
    m = re.search(r'"as-number"\s*:\s*"?(\d+)', text or "")
    return int(m.group(1)) if m else None
