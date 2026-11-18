"""Vendor-aware secret redaction (D10).  Runs before anything is logged or displayed."""
from __future__ import annotations

import re

MASK = "<redacted>"

_IOS = [
    # enable secret 9 $9$... / enable password 7 0822455D0A16 / enable secret cisco
    (re.compile(r"^(\s*enable (?:secret|password)(?: level \d+)?(?: \d)?) \S+.*$", re.M), r"\1 " + MASK),
    # username admin privilege 15 secret 9 $9$...  / password 7 xxx / password cleartext
    (re.compile(r"^(\s*username \S+.*?\b(?:secret|password)(?: \d)?) \S+.*$", re.M), r"\1 " + MASK),
    (re.compile(r"^(\s*(?:\S+ )*?password(?: \d)?) \S+\s*$", re.M), r"\1 " + MASK),
    (re.compile(r"^(\s*key-string(?: \d)?) \S+.*$", re.M), r"\1 " + MASK),
    (re.compile(r"^(\s*crypto isakmp key(?: [06])?) \S+( address .*)?$", re.M), r"\1 " + MASK + r"\2"),
    (re.compile(r"(\bkey 7) \S+"), r"\1 " + MASK),
    (re.compile(r"(\bpre-shared-key(?: \w+)*?(?: \d)?) \S+"), r"\1 " + MASK),
    (re.compile(r"(\bauthentication-key(?: \d)?) \S+"), r"\1 " + MASK),
    (re.compile(r"(\bmessage-digest-key \d+ md5(?: \d)?) \S+"), r"\1 " + MASK),
    (re.compile(r"^(\s*snmp-server community) \S+", re.M), r"\1 " + MASK),
    (re.compile(r"(\bneighbor \S+ password(?: \d)?) \S+"), r"\1 " + MASK),
    (re.compile(r"(\b(?:tacacs|radius)-server (?:\S+ )*?key(?: \d)?) \S+"), r"\1 " + MASK),
]
_HASH = re.compile(r"\$(?:9|8|5|1|6)\$[^\s\"';]+")          # IOS type 5/8/9, Junos $1$/$6$
_JUNOS = re.compile(r"\$9\$[^\s\"';]+")                       # Junos $9$ reversible
_FORTI = re.compile(r"\bENC [A-Za-z0-9+/=]{8,}")
_FORTI_SET = re.compile(r"^(\s*set (?:password|passwd|psksecret|ppk-secret|auth-pwd|key)) .*$", re.M)
_JUNOS_SECRET = re.compile(r'(\b(?:encrypted-password|secret|authentication-key|pre-shared-key ascii-text)) "[^"]*"')


def redact(text: str | None, vendor: str = "cisco") -> str:
    if not text:
        return text or ""
    out = text
    for rx, rep in _IOS:
        out = rx.sub(rep, out)
    out = _JUNOS_SECRET.sub(r'\1 "' + MASK + '"', out)
    out = _FORTI_SET.sub(r"\1 " + MASK, out)
    out = _FORTI.sub("ENC " + MASK, out)
    out = _HASH.sub(MASK, out)
    out = _JUNOS.sub(MASK, out)
    return out


def redact_values(text: str, secrets: list[str]) -> str:
    """Remove literal secret values (from device-creds / cml-credentials) wherever they appear."""
    out = text or ""
    for s in secrets:
        if s and len(s) >= 3:
            out = out.replace(s, MASK)
    return out
