"""Per-device show scripts: the commands LabDesk types on a device's console when you open it.

File: ``~/.config/labdesk/show-scripts.json`` (override: $LABDESK_SHOW_SCRIPTS), keyed by the device label
(like device-creds), so a script survives a lab being rebuilt::

    {"version": 1,
     "devices": {"R1": {"commands": ["show standby brief", "show track brief"], "on_open": true}}}

Rules
* read-only: every line must start with ``show``, ``ping`` or ``traceroute`` - a script that runs by itself
  each time a console opens must never be able to change a device;
* a line starting with ``!`` or ``#`` is a comment, blank lines are ignored;
* at most ``MAX_COMMANDS`` commands of at most ``MAX_LEN`` characters.

``suggest()`` proposes a script from what the device is running *right now* (live facts only: HSRP / VRRP
groups, BGP, OSPF, VLANs, EtherChannel read from the device) - never from the lab YAML or a day-0 config.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from . import config

VERSION = 1
MAX_COMMANDS = 20
MAX_LEN = 200
ALLOWED = ("show", "ping", "traceroute")
_VERB_RE = re.compile(r"^(sh(?:ow?)?|ping|trace(?:r(?:o(?:u(?:te?)?)?)?)?)(?:\s|$)", re.I)
_CANON = {"s": "show", "p": "ping", "t": "traceroute"}
# a script line is typed on a console: no control characters, no pager / redirect tricks to files
_BAD_RE = re.compile(r"[\x00-\x1f\x7f]")
_REDIRECT_RE = re.compile(r"\|\s*(redirect|tee|append)\b", re.I)


class ScriptError(ValueError):
    pass


@dataclass
class ShowScript:
    commands: list[str] = field(default_factory=list)
    on_open: bool = True

    @property
    def active(self) -> bool:
        """Runs when the console opens."""
        return bool(self.on_open and self.commands)

    def text(self) -> str:
        return "\n".join(self.commands)


def scripts_path() -> Path:
    return Path(os.environ.get("LABDESK_SHOW_SCRIPTS", str(config.CONFIG_DIR / "show-scripts.json")))


def check_line(line: str) -> str:
    """'' when ``line`` is an allowed command, else what is wrong with it (plain words)."""
    s = line.strip()
    if not s:
        return "empty line"
    if len(s) > MAX_LEN:
        return f"longer than {MAX_LEN} characters"
    if _BAD_RE.search(s):
        return "contains a control character"
    if not _VERB_RE.match(s):
        return "only show, ping and traceroute commands are allowed"
    if _REDIRECT_RE.search(s):
        return "writing output to a file (redirect / tee / append) is not allowed"
    return ""


def normalise(line: str) -> str:
    """``sh ip int br`` stays as typed apart from the verb, which is spelled out (``show ip int br``)."""
    s = " ".join(line.strip().split())
    m = _VERB_RE.match(s)
    if not m:
        return s
    return _CANON[m.group(1)[0].lower()] + s[len(m.group(1)):]


def parse(text: str) -> list[str]:
    """Script text (one command per line) -> commands.  Raises ScriptError naming the first bad line."""
    out: list[str] = []
    for no, raw in enumerate(text.splitlines(), 1):
        s = raw.strip()
        if not s or s.startswith(("!", "#")):
            continue
        err = check_line(s)
        if err:
            raise ScriptError(f"line {no} ({s[:40]}): {err}")
        cmd = normalise(s)
        if cmd not in out:
            out.append(cmd)
    if len(out) > MAX_COMMANDS:
        raise ScriptError(f"at most {MAX_COMMANDS} commands (this script has {len(out)})")
    return out


# ----------------------------------------------------------------------------- storage
def _read(path: Path | None = None) -> dict:
    p = Path(path) if path else scripts_path()
    try:
        data = json.loads(p.read_text())
    except (OSError, ValueError):
        return {}
    dev = data.get("devices") if isinstance(data, dict) else None
    return dev if isinstance(dev, dict) else {}


def load_all(path: Path | None = None) -> dict[str, ShowScript]:
    out: dict[str, ShowScript] = {}
    for label, entry in _read(path).items():
        if not isinstance(entry, dict):
            continue
        cmds = [normalise(c) for c in entry.get("commands") or [] if isinstance(c, str) and not check_line(c)]
        out[str(label)] = ShowScript(cmds[:MAX_COMMANDS], bool(entry.get("on_open", True)))
    return out


def load(label: str, path: Path | None = None) -> ShowScript | None:
    """The saved script of one device, or None when it has none (a hand-edited file is re-validated)."""
    return load_all(path).get(label)


def save(label: str, script: ShowScript | None, path: Path | None = None) -> None:
    """Save (or, with ``None`` / no commands, remove) one device's script.  Atomic replace; every other
    device's entry is kept."""
    p = Path(path) if path else scripts_path()
    devices = _read(p)
    if script is None or not script.commands:
        devices.pop(label, None)
    else:
        bad = next((f"{c[:40]}: {check_line(c)}" for c in script.commands if check_line(c)), "")
        if bad:
            raise ScriptError(bad)
        if len(script.commands) > MAX_COMMANDS:
            raise ScriptError(f"at most {MAX_COMMANDS} commands")
        devices[label] = {"commands": [normalise(c) for c in script.commands], "on_open": bool(script.on_open)}
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=".show-scripts.")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump({"version": VERSION, "devices": devices}, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, p)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ----------------------------------------------------------------------------- suggestions (live facts)
def suggest(nf, is_switch: bool = False) -> list[str]:
    """A starting script for a device, from its LIVE facts (``facts.NodeFacts``; ``None`` = nothing read
    yet -> only the commands every IOS device answers)."""
    cmds = ["show ip interface brief"]

    def val(name):
        f = getattr(nf, name, None) if nf is not None else None
        return getattr(f, "value", None) if f is not None else None

    groups = val("fhrp") or []
    protos = {str(getattr(getattr(g, "proto", None), "value", getattr(g, "proto", ""))).upper() for g in groups}
    if "HSRP" in protos:
        cmds += ["show standby brief", "show track brief"]
    if "VRRP" in protos:
        cmds.append("show vrrp brief")
    if val("bgp") is not None:
        cmds += ["show ip bgp summary", "show ip bgp"]
    if val("ospf_running") or val("ospf_nbrs"):
        cmds.append("show ip ospf neighbor")
    l2 = getattr(nf, "l2", None) if nf is not None else None
    if is_switch or l2 is not None:
        cmds += ["show vlan brief", "show interfaces trunk", "show spanning-tree summary"]
    ec = getattr(nf, "ec", None) if nf is not None else None
    try:
        has_ec = bool(ec is not None and ec.groups())
    except Exception:  # noqa: BLE001
        has_ec = False
    if has_ec:
        cmds.append("show etherchannel summary")
    if not (is_switch or l2 is not None) or val("default_route") is not None:
        cmds.append("show ip route")
    out: list[str] = []
    for c in cmds:
        if c not in out:
            out.append(c)
    return out


def burst_timeout(commands: list[str]) -> float:
    """Whole-script deadline: login + terminal settings, then room for every command on a 9600-baud line."""
    slow = sum(1 for c in commands if c.startswith(("ping", "traceroute", "show running", "show run ",
                                                     "show tech", "show startup")))
    return min(240.0, 15.0 + 8.0 * len(commands) + 25.0 * slow)
