"""Shared parser plumbing: ParseResult, line splitting, interface names, syslog stripping.

Every parser in this package is a pure function ``text -> ParseResult`` that never raises.
``ok`` means "the command ran and produced a valid answer" (sentinel seen, no IOS error
marker); it does NOT require non-empty output: on IOSv an empty / header-only answer is a
valid "not configured".  The burst layer sets ok=False for a missing sentinel / timeout by
calling :func:`failed`.
"""
from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass, field
from typing import Any

IP = r"\d{1,3}(?:\.\d{1,3}){3}"
IP_RE = re.compile(r"\b(" + IP + r")\b")
ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b[()][A-Za-z0-9]|\x1b[=>78DEHM]|\x07")

ERROR_MARKERS = ("% Invalid input", "% Incomplete command", "% Ambiguous command",
                 "% Unknown command", "% Unrecognized command", "Command fail", "syntax error",
                 "unknown command")

# "*Nov 17 04:12:33.123: %OSPF-5-ADJCHG: ..." / "%LINK-3-UPDOWN: ..." / "Nov 17 04:12:33: %..."
SYSLOG_RE = re.compile(r"^\s*(?:\*|\.)?(?:(?P<ts>[A-Z][a-z]{2} +\d+ [\d:.]+)(?: [A-Z]{3,4})?: )?"
                       r"(?P<msg>%(?P<fac>[A-Z0-9_]+)-(?P<sev>\d)-(?P<mn>[A-Z0-9_]+): ?(?P<text>.*))$")


@dataclass
class ParseResult:
    ok: bool
    data: Any = None
    error: str | None = None

    def __bool__(self) -> bool:          # convenience: ``if res:`` == parsed fine
        return self.ok


def failed(error: str, data: Any = None) -> ParseResult:
    return ParseResult(False, data, error)


def lines(text: str | None) -> list[str]:
    if not text:
        return []
    text = ANSI_RE.sub("", text).replace("\r\n", "\n").replace("\r", "\n")
    return text.split("\n")


def error_marker(text: str | None) -> str | None:
    """The first IOS/Junos/FortiOS CLI error marker found in the output, or None."""
    for ln in lines(text):
        s = ln.strip()
        for m in ERROR_MARKERS:
            if s.startswith(m) or (m.startswith("%") and s.startswith(m)):
                return s[:120]
    return None


@dataclass
class SyslogLine:
    ts: str | None          # device timestamp as printed ("Nov 17 04:12:33.123") or None
    facility: str           # OSPF, LINEPROTO, LINK, BGP, HSRP, VRRP, SYS ...
    severity: int
    mnemonic: str           # ADJCHG, UPDOWN, ADJCHANGE, STATECHANGE, CONFIG_I ...
    text: str
    raw: str = field(repr=False, default="")


def split_syslog(text: str | None) -> tuple[str, list[SyslogLine]]:
    """Remove unsolicited syslog lines from CLI output; return (clean_text, syslog lines)."""
    keep, logs = [], []
    for ln in lines(text):
        m = SYSLOG_RE.match(ln)
        if m:
            logs.append(SyslogLine(m["ts"], m["fac"], int(m["sev"]), m["mn"], m["text"].strip(), ln))
            continue
        keep.append(ln)
    return "\n".join(keep), logs


# ------------------------------------------------------------------ interface names
_IFACE_ABBR = [
    ("TenGigabitEthernet", ("Te", "Ten")),
    ("GigabitEthernet", ("Gi", "Gig", "Ge")),
    ("FastEthernet", ("Fa",)),
    ("Ethernet", ("Et", "Eth", "E")),
    ("Loopback", ("Lo",)),
    ("Tunnel", ("Tu",)),
    ("Vlan", ("Vl",)),
    ("Port-channel", ("Po",)),
    ("Serial", ("Se",)),
    ("NVI", ("NV",)),
]


def expand_iface(name: str) -> str:
    """'Gi0/0' -> 'GigabitEthernet0/0'. Unknown names (ge-0/0/0, port2) are returned unchanged."""
    if not name:
        return name
    m = re.match(r"([A-Za-z][A-Za-z\-]*?)(\d.*)$", name)
    if not m:
        return name
    prefix, rest = m.groups()
    for full, abbrs in _IFACE_ABBR:
        if prefix.lower() == full.lower() or prefix in abbrs:
            return full + rest
    return name


def short_iface(name: str) -> str:
    """'GigabitEthernet0/1' -> 'Gi0/1'."""
    full = expand_iface(name)
    for f, abbrs in _IFACE_ABBR:
        if full.startswith(f) and full[len(f):len(f) + 1].isdigit():
            return abbrs[0] + full[len(f):]
    return name


def iface_key(name: str) -> str:
    """Canonical comparison key for an interface name ('Gi0/1' == 'GigabitEthernet0/1')."""
    n = (name or "").strip()
    if re.match(r"^(ge|xe|et|fe)-\d", n) and n.endswith(".0"):
        n = n[:-2]                               # Junos unit .0 == the physical port
    return expand_iface(n).lower()


def is_nat_virtual(name: str) -> bool:
    """IOS NAT virtual interfaces (NVI0 / NV0) duplicate a real interface's IP - skip them."""
    return bool(re.match(r"^NVI?\d", name or ""))


def mask_to_len(mask: str) -> int | None:
    try:
        return ipaddress.IPv4Network(f"0.0.0.0/{mask}").prefixlen
    except ValueError:
        return None


def column_spans(header: str, names: list[str]) -> dict[str, int]:
    """Start column of each header word (for fixed-width tables with empty cells)."""
    out = {}
    for n in names:
        i = header.find(n)
        if i >= 0:
            out[n] = i
    return out
