"""On-device login accounts, parsed from a device's configuration (pure; no Qt, no I/O).

What it answers, per account (``username X`` / ``enable`` / ``line vty 0 4`` / Junos ``root`` / FortiOS admin):
*how is the password stored on the device* - encrypted (and with which scheme) or plain text.

* The hash / encrypted string itself is never kept: only the scheme word ("type 9 scrypt").
* A plain-text password is kept in ``DeviceAccount.plaintext`` (``repr=False``) because the hover card shows it
  (the user asked for that).  It never goes to the event log, probe logs or the Config tab: those are redacted
  (``probe.redact`` + ``redact_values(..., accounts.plaintexts())``).

Sources:
* IOS / IOS-XE / IOSvL2: ``show running-config`` (the CONFIG probe tier; needs privilege 15).
* Junos: ``show configuration system | display set`` (set form) or the curly-brace form.
* FortiOS: ``show system admin`` (``config system admin`` / ``edit "admin"`` / ``set password ENC ...``).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# ----------------------------------------------------------------------------- IOS type numbers
IOS_TYPES = {
    "0": ("plain text", False, False),
    "4": ("type 4 SHA-256 (deprecated, weak)", True, True),
    "5": ("type 5 MD5", True, True),
    "7": ("type 7 — weak, reversible", True, True),
    "8": ("type 8 PBKDF2-SHA-256", True, False),
    "9": ("type 9 scrypt", True, False),
}
# crypt(3)-style prefixes (Junos encrypted-password, also seen in Linux shadow lines)
CRYPT_PREFIX = {
    "$1$": ("$1 MD5", True),
    "$5$": ("$5 SHA-256", False),
    "$6$": ("$6 SHA-512", False),
    "$8$": ("$8 PBKDF2-SHA-256", False),
    "$9$": ("$9 Junos reversible", True),
    "$sha1$": ("$sha1 SHA-1", True),
}


@dataclass(frozen=True)
class DeviceAccount:
    kind: str                       # 'user' | 'enable' | 'line' | 'root' | 'admin'
    name: str                       # 'admin' | 'enable' | 'line vty 0 4' | 'root'
    encrypted: bool
    scheme: str                     # 'type 9 scrypt' | '$6 SHA-512' | 'ENC' | 'plain text' | 'no password'
    weak: bool = False              # reversible / broken hash (type 7, type 4, MD5, $9$)
    privilege: int | None = None
    plaintext: str | None = field(default=None, repr=False, compare=False)   # only when NOT encrypted

    @property
    def detail(self) -> str:
        """'encrypted (type 9 scrypt)' | 'plain text (not encrypted)' | 'no password'."""
        if self.scheme == "no password":
            return "no password"
        if self.encrypted:
            return f"encrypted ({self.scheme})"
        return "plain text (not encrypted)"


@dataclass
class DeviceAccounts:
    vendor: str
    accounts: list[DeviceAccount] = field(default_factory=list)
    password_encryption: bool | None = None     # IOS 'service password-encryption' (None: not stated)

    def plaintexts(self) -> list[str]:
        """Plain-text device passwords (for literal redaction of logs)."""
        return [a.plaintext for a in self.accounts if a.plaintext]

    def __repr__(self) -> str:                  # never print values
        return f"DeviceAccounts({self.vendor}, {len(self.accounts)} accounts, pwenc={self.password_encryption})"


def _unquote(v: str) -> str:
    v = v.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        return v[1:-1]
    return v


# ----------------------------------------------------------------------------- IOS
_IOS_USER = re.compile(
    r"^username (?P<name>\S+)(?P<opts>.*?)\s(?P<kw>secret|password)(?: (?P<type>\d))? (?P<val>\S.*?)\s*$")
_IOS_USER_NOPW = re.compile(r"^username (?P<name>\S+)(?:.*\s)?nopassword\b")
_IOS_ENABLE = re.compile(
    r"^enable (?P<kw>secret|password)(?: level (?P<lvl>\d+))?(?: (?P<type>\d))? (?P<val>\S.*?)\s*$")
_IOS_LINE_PW = re.compile(r"^password(?: (?P<type>\d))? (?P<val>\S.*?)\s*$")


def _ios_account(kind: str, name: str, kw: str, typ: str | None, val: str, privilege: int | None) -> DeviceAccount:
    if typ is None:
        # no type number: a 'secret' typed in a config file / day-0 text is still the literal secret
        # (the running-config always shows the type after IOS hashed it)
        return DeviceAccount(kind, name, False, "plain text", True, privilege, val)
    scheme, enc, weak = IOS_TYPES.get(typ, (f"type {typ}", True, False))
    if not enc:
        return DeviceAccount(kind, name, False, "plain text", True, privilege, val)
    return DeviceAccount(kind, name, True, scheme, weak, privilege)


def parse_ios_accounts(text: str | None) -> DeviceAccounts:
    """``show running-config`` (IOS / IOSvL2) -> accounts: ``username``, ``enable secret|password`` and line
    passwords (``line con 0`` / ``line vty 0 4`` ``password``), plus ``service password-encryption``."""
    res = DeviceAccounts("cisco")
    line_block: str | None = None
    for raw in (text or "").replace("\r", "").split("\n"):
        if not raw.strip():
            continue
        top = not raw.startswith(" ")
        s = raw.strip()
        if top:
            line_block = None
            if s == "service password-encryption":
                res.password_encryption = True
            elif s == "no service password-encryption":
                res.password_encryption = False
            elif (m := _IOS_USER.match(s)):
                pm = re.search(r"\bprivilege (\d+)", m["opts"] or "")
                res.accounts.append(_ios_account("user", m["name"], m["kw"], m["type"], m["val"],
                                                 int(pm[1]) if pm else None))
            elif (m := _IOS_USER_NOPW.match(s)):
                pm = re.search(r"\bprivilege (\d+)", s)
                res.accounts.append(DeviceAccount("user", m["name"], False, "no password", True,
                                                  int(pm[1]) if pm else None))
            elif (m := _IOS_ENABLE.match(s)):
                lvl = int(m["lvl"]) if m["lvl"] else None
                name = "enable" + (" secret" if m["kw"] == "secret" else " password") + \
                    (f" level {lvl}" if lvl is not None else "")
                res.accounts.append(_ios_account("enable", name, m["kw"], m["type"], m["val"], lvl))
            elif (m := re.match(r"^line ((?:con|vty|aux|tty)\s.*)$", s)):
                line_block = "line " + re.sub(r"\s+", " ", m[1])
            continue
        if line_block and (m := _IOS_LINE_PW.match(s)):
            res.accounts.append(_ios_account("line", line_block, "password", m["type"], m["val"], None))
    return res


# ----------------------------------------------------------------------------- Junos
def _crypt_scheme(h: str) -> tuple[str, bool]:
    for p, (word, weak) in CRYPT_PREFIX.items():
        if h.startswith(p):
            return word, weak
    return "hashed", False


_J_SET_ROOT = re.compile(r'^set system root-authentication (?P<kw>encrypted-password|plain-text-password-value)'
                         r' (?P<val>.+?)\s*$')
_J_SET_USER = re.compile(r'^set system login user (?P<name>\S+) authentication '
                         r'(?P<kw>encrypted-password|plain-text-password-value) (?P<val>.+?)\s*$')
_J_SET_CLASS = re.compile(r'^set system login user (?P<name>\S+) class (?P<cls>\S+)')


def _junos_account(kind: str, name: str, kw: str, val: str) -> DeviceAccount:
    v = _unquote(val.split("##")[0].rstrip(" ;"))
    if kw.startswith("plain-text"):
        return DeviceAccount(kind, name, False, "plain text", True, None, v)
    word, weak = _crypt_scheme(v)
    return DeviceAccount(kind, name, True, word, weak)


def parse_junos_accounts(text: str | None) -> DeviceAccounts:
    """Junos ``show configuration system [login|root-authentication] | display set`` or the curly-brace form
    -> root + login users.  Junos always stores login passwords hashed (``encrypted-password``)."""
    res = DeviceAccounts("juniper")
    classes: dict[str, str] = {}
    seen: dict[str, DeviceAccount] = {}
    stack: list[str] = []
    for raw in (text or "").replace("\r", "").split("\n"):
        s = raw.strip()
        if not s or s.startswith(("#", "/*")):
            continue
        if s.startswith("set "):
            if (m := _J_SET_ROOT.match(s)):
                seen["root"] = _junos_account("root", "root", m["kw"], m["val"])
            elif (m := _J_SET_USER.match(s)):
                seen[m["name"]] = _junos_account("user", m["name"], m["kw"], m["val"])
            elif (m := _J_SET_CLASS.match(s)):
                classes[m["name"]] = m["cls"]
            continue
        # curly-brace form
        if s.endswith("{"):
            stack.append(s[:-1].strip())
            continue
        if s == "}":
            if stack:
                stack.pop()
            continue
        m = re.match(r'^(encrypted-password|plain-text-password-value) (.+?);?\s*(?:##.*)?$', s)
        if m:
            if "root-authentication" in stack:
                seen["root"] = _junos_account("root", "root", m[1], m[2])
            else:
                user = next((x[5:] for x in reversed(stack) if x.startswith("user ")), None)
                if user:
                    seen[user] = _junos_account("user", user, m[1], m[2])
            continue
        m = re.match(r"^class (\S+?);", s)
        if m:
            user = next((x[5:] for x in reversed(stack) if x.startswith("user ")), None)
            if user:
                classes[user] = m[1]
    for name, acc in seen.items():
        res.accounts.append(acc)
    for name in classes:
        if name not in seen:
            res.accounts.append(DeviceAccount("user", name, False, "no password", True))   # key-only / none
    return res


# ----------------------------------------------------------------------------- FortiOS
def parse_fortios_accounts(text: str | None) -> DeviceAccounts:
    """FortiOS ``show system admin`` -> admin accounts.  ``set password ENC <hash>`` = encrypted (FortiOS shows
    'SH2…' salted SHA-256 / 'AK1…' legacy SHA-1); a quoted ``set password "x"`` (a typed script) = plain text."""
    res = DeviceAccounts("fortinet")
    in_admin = False
    cur: str | None = None
    found: dict[str, DeviceAccount] = {}
    for raw in (text or "").replace("\r", "").split("\n"):
        s = raw.strip()
        if s.startswith("config "):
            in_admin = s == "config system admin"
            continue
        if not in_admin:
            continue
        if s == "end":
            in_admin, cur = False, None
            continue
        if (m := re.match(r'^edit (.+)$', s)):
            cur = _unquote(m[1])
            found.setdefault(cur, DeviceAccount("admin", cur, False, "no password", True))
            continue
        if s == "next":
            cur = None
            continue
        if cur and (m := re.match(r"^set password (.+)$", s)):
            v = m[1].strip()
            if v.startswith("ENC "):
                h = v[4:].strip()
                scheme = "ENC SHA-256" if h.startswith("SH2") else ("ENC SHA-1, legacy" if h.startswith("AK1")
                                                                     else "ENC")
                found[cur] = DeviceAccount("admin", cur, True, scheme, h.startswith("AK1"))
            else:
                found[cur] = DeviceAccount("admin", cur, False, "plain text", True, None, _unquote(v))
    res.accounts = list(found.values())
    return res


def parse_accounts(text: str | None, vendor: str = "cisco") -> DeviceAccounts:
    return {"juniper": parse_junos_accounts, "fortinet": parse_fortios_accounts}.get(vendor, parse_ios_accounts)(text)


# commands that read the account lines (the IOS collector reuses its CONFIG-tier 'show running-config')
JUNOS_ACCOUNT_CMDS = ["show configuration system login | display set",
                      "show configuration system root-authentication | display set"]
FORTIOS_ACCOUNT_CMDS = ["show system admin"]
