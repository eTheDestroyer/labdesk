"""First-login defaults: the login to use the first time you open a device's console.

One table, one entry per platform, each value checked against its source of truth:

* Cisco IOSv / IOSvL2 / IOL-XE / IOL-L2-XE created by LabDesk: ``catalog.IOS_BASE_CONFIG`` (the day-0 config
  LabDesk writes): ``username admin privilege 15 secret cisco`` + ``enable secret cisco``.
* Alpine / Desktop / Tiny Core / Ubuntu: the CML 2.10 node definitions (``GET /api/v0/node_definitions/<id>``,
  provisioning file + description "The user account is configurable (cisco/cisco by default)").
* Any other CML node without a login in its day-0 config (a blank IOSv, CSR, ASAv, ...): IOS has no login on the
  console and no enable password until one is configured.
* Juniper vJunos-router created by LabDesk (``kvm.create_vm`` imports the image and sets nothing): factory
  default ``root`` with no password on the console (amnesiac).  When ``JUNOS_ADMIN_USER`` / ``JUNOS_ADMIN_PASS``
  exist in ``cml-credentials.env`` (the lab scripts configure Juniper VMs with them) those are used.
* FortiOS / FortiGate VM: ``admin`` with an empty password, a new one is forced at first login
  (``FGT_ADMIN_USER`` / ``FGT_ADMIN_PASS`` from ``cml-credentials.env`` when set).
* Unmanaged switch / external connector (NAT, bridge, Internet cloud): no console, no login.

A node's actual day-0 config wins over the table (:func:`parse_day0`): plain-text values only, a hashed secret
is reported as "set in config (encrypted)" and never kept.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, replace

ENCRYPTED = "set in config (encrypted)"
NO_LOGIN_DEFS = {"unmanaged_switch", "external_connector"}
IOS_DEFS = {"iosv", "iosvl2", "iol-xe", "ioll2-xe", "iol-xe-serial-4eth", "csr1000v", "cat8000v",
            "cat9000v-q200", "cat9000v-uadp", "asav"}
LINUX_DEFS = {"alpine", "desktop", "server", "ubuntu", "net-tools", "frr", "dnsmasq", "nginx", "radius",
              "syslog", "tacplus", "chrome", "firefox", "wireless-client"}


@dataclass(frozen=True)
class FirstLogin:
    username: str | None = None          # None = no login prompt
    password: str | None = None          # None = no password ('' never used)
    enable: str | None = None            # None = no enable password / not applicable
    note: str = ""                       # short hint shown under the login
    source: str = ""                     # where the values come from (docs / tooltips)
    password_encrypted: bool = False     # the day-0 config has it hashed: value unknown
    enable_encrypted: bool = False
    has_enable: bool = False             # the platform has an enable step (IOS)
    console_note: str = ""               # how the console differs ('no username on the console: press Enter')

    def __repr__(self) -> str:           # never print values
        return (f"FirstLogin(username={self.username!r}, password={'set' if self.password else None}, "
                f"enable={'set' if self.enable else None}, source={self.source!r})")

    @property
    def empty(self) -> bool:
        return not (self.username or self.password or self.enable)

    def creds_values(self) -> dict:
        """What can be written to device-creds (plain-text values only)."""
        return {k: v for k, v in (("USERNAME", self.username), ("PASSWORD", self.password),
                                  ("ENABLE", self.enable)) if v}

    def summary(self, show: bool = True, mask: str = "••••••") -> str:
        """'admin / cisco · enable cisco' (the hover card / toast / inspector line)."""
        def sec(v, enc):
            if enc:
                return "(encrypted)"
            if not v:
                return "(no password)"
            return v if show else mask
        if not self.username and not self.password:
            s = "no login (press Enter)"
        else:
            s = f"{self.username or '—'} / {sec(self.password, self.password_encrypted)}"
        if self.enable or self.enable_encrypted:
            s += f" · enable {sec(self.enable, self.enable_encrypted)}"
        elif self.has_enable:
            s += " · enable: no password"
        return s


CON_NO_LOGIN = "console: no username, press Enter, then 'enable'"
_IOS_LABDESK = FirstLogin("admin", "cisco", "cisco",
                          "LabDesk day-0 config; the console has no login: press Enter, then 'enable' - "
                          "admin / cisco is for SSH / telnet", "catalog.IOS_BASE_CONFIG", has_enable=True,
                          console_note=CON_NO_LOGIN)
_IOS_BLANK = FirstLogin(None, None, None, "press Enter; no password until you configure one",
                        "IOS without a login in its day-0 config", has_enable=True)
_LINUX = FirstLogin("cisco", "cisco", None, "CML Linux default (USERNAME/PASSWORD in the node config)",
                    "CML node definition (cisco/cisco by default)")
_TINYCORE = FirstLogin("cisco", "cisco", None, "CML Tiny Core default; user 'tc' has no password",
                       "CML node definition 'server'")
_VJUNOS = FirstLogin("root", None, None,
                     "no password at the console (amnesiac): type 'cli', then set one with "
                     "'set system root-authentication plain-text-password'",
                     "vJunos factory default (LabDesk sets no password)")
_FORTIOS = FirstLogin("admin", None, None, "empty password: press Enter, then you must set a new one",
                      "FortiOS factory default")

# platform key (CML node_definition or host-KVM kind) -> first login
PLATFORM_DEFAULTS: dict[str, FirstLogin] = {
    "iosv": _IOS_LABDESK, "iosvl2": _IOS_LABDESK, "iol-xe": _IOS_LABDESK, "ioll2-xe": _IOS_LABDESK,
    "alpine": _LINUX, "desktop": _LINUX, "ubuntu": _LINUX, "server": _TINYCORE,
    "vjunos": _VJUNOS, "fortios": _FORTIOS,
}

# Rows for the README / docs table: (platform, login, enable, note)
DOC_ROWS = [
    ("Cisco IOSv / IOSvL2 / IOL-XE (created by LabDesk)", "console: none (press Enter); SSH / telnet: admin / cisco",
     "cisco", "LabDesk day-0 config"),
    ("Cisco node with no login in its day-0 config", "none (press Enter)", "no password",
     "until you configure one"),
    ("Alpine / Desktop / Ubuntu (CML)", "cisco / cisco", "-", "CML node definition default"),
    ("Tiny Core (CML `server`)", "cisco / cisco", "-", "user `tc` has no password"),
    ("Juniper vJunos (host KVM)", "root / (no password)", "-",
     "amnesiac; `JUNOS_ADMIN_USER`/`_PASS` from cml-credentials.env when set"),
    ("FortiOS (host KVM)", "admin / (empty)", "-",
     "new password forced at first login; `FGT_ADMIN_USER`/`_PASS` when set"),
    ("Unmanaged switch, NAT / bridge / Internet cloud", "no login", "-", "no console"),
]


def has_login(platform: str) -> bool:
    return platform not in NO_LOGIN_DEFS


def config_text(cfg) -> str:
    """CML node ``configuration``: a string or the multi-file form [{name, content}]."""
    if isinstance(cfg, list):
        parts = [str(c.get("content") or "") for c in cfg if isinstance(c, dict)]
        return "\n".join(parts)
    return str(cfg or "")


_IOS_USER = re.compile(r"^username\s+(\S+)(?:\s+privilege\s+\d+)?(?:\s+\S+)*?\s+(secret|password)\s+(?:(\d+)\s+)?(\S+)\s*$",
                       re.M)
_IOS_ENABLE = re.compile(r"^enable\s+(secret|password)\s+(?:level\s+\d+\s+)?(?:(\d+)\s+)?(\S+)\s*$", re.M)


def _ios_plain(kind: str, typ: str | None, value: str) -> bool:
    if typ is None or typ == "0":
        return not value.startswith("$")
    return False


def _console_has_login(text: str) -> bool:
    """``line con 0`` has ``login`` / ``login local`` (or AAA is on): the console asks for a username."""
    if re.search(r"^aaa new-model\s*$", text, re.M):
        return True
    m = re.search(r"^line con 0\s*\n((?:[ \t]+.*\n?)*)", text, re.M)
    return bool(m and re.search(r"^\s+login(\s+local)?\s*$", m.group(1), re.M))


def parse_day0(platform: str, cfg) -> FirstLogin | None:
    """The login a node's day-0 config sets (None = the config says nothing about logins)."""
    text = config_text(cfg)
    if not text.strip():
        return None
    if platform in LINUX_DEFS:
        if platform == "ubuntu":
            u = re.search(r"default_user:\s*\n\s+name:\s*(\S+)", text)
            p = re.search(r"^password:\s*(\S+)\s*$", text, re.M)
            if not (u or p):
                return None
            return FirstLogin(u.group(1) if u else "ubuntu", p.group(1).strip("\"'") if p else None, None,
                              "cloud-init user-data", "day-0 config")
        u = re.search(r"^\s*USERNAME=(\S*)\s*$", text, re.M)
        p = re.search(r"^\s*PASSWORD=(\S*)\s*$", text, re.M)
        if not (u or p):
            return None
        user = (u.group(1).strip("\"'") if u else "") or "cisco"
        pw = (p.group(1).strip("\"'") if p else "") or None
        return FirstLogin(user, pw, None, "USERNAME / PASSWORD in the node config", "day-0 config")
    if platform in IOS_DEFS or platform not in PLATFORM_DEFAULTS:
        users = _IOS_USER.findall(text)
        en = _IOS_ENABLE.findall(text)
        if not users and not en:
            if platform in IOS_DEFS:
                return replace(_IOS_BLANK, source="day-0 config (no username / enable lines)")
            return None
        user = pw = None
        pw_enc = False
        if users:
            # prefer a privilege-15 user, then the first one
            line15 = re.search(r"^username\s+(\S+)\s+privilege\s+15\b", text, re.M)
            pick = next((u for u in users if line15 and u[0] == line15.group(1)), users[0])
            user, _, typ, val = pick
            if _ios_plain(pick[1], typ or None, val):
                pw = val
            else:
                pw_enc = True
        enable = None
        en_enc = False
        if en:
            sec = next((e for e in en if e[0] == "secret"), en[0])   # secret wins over password on IOS
            if _ios_plain(sec[0], sec[1] or None, sec[2]):
                enable = sec[2]
            else:
                en_enc = True
        con_login = _console_has_login(text)
        note = "from the node's day-0 config" + (f" — {ENCRYPTED}" if pw_enc or en_enc else "")
        if users and not con_login:
            note += "; the console has no login: press Enter, then 'enable' - the user is for SSH / telnet"
        if not users:
            note = "no username in the day-0 config: press Enter"
        return FirstLogin(user, pw, enable, note, "day-0 config", pw_enc, en_enc, has_enable=True,
                          console_note="" if (con_login or not users) else CON_NO_LOGIN)
    return None


def kvm_default(kind: str, env: dict | None = None) -> FirstLogin | None:
    """Host-KVM VM: the table default, or the admin login from cml-credentials.env when it is set."""
    base = PLATFORM_DEFAULTS.get(kind)
    env = env or {}
    if kind == "vjunos" and env.get("JUNOS_ADMIN_USER") and env.get("JUNOS_ADMIN_PASS"):
        return FirstLogin(env["JUNOS_ADMIN_USER"], env["JUNOS_ADMIN_PASS"], None,
                          "JUNOS_ADMIN_USER / _PASS from cml-credentials.env (a new vJunos: root, no password)",
                          "cml-credentials.env")
    if kind == "fortios" and env.get("FGT_ADMIN_USER") and env.get("FGT_ADMIN_PASS"):
        return FirstLogin(env["FGT_ADMIN_USER"], env["FGT_ADMIN_PASS"], None,
                          "FGT_ADMIN_USER / _PASS from cml-credentials.env (a new FortiOS: admin, empty password)",
                          "cml-credentials.env")
    return base


def first_login(platform: str, cfg=None, *, backend: str = "cml", env: dict | None = None) -> FirstLogin | None:
    """The first login of a device: its day-0 config, else the platform table (None = no login at all)."""
    if not has_login(platform):
        return None
    if backend == "kvm":
        return kvm_default(platform, env)
    parsed = parse_day0(platform, cfg) if cfg is not None else None
    if parsed is not None:
        return parsed
    if platform in PLATFORM_DEFAULTS:
        base = PLATFORM_DEFAULTS[platform]
        if platform in IOS_DEFS and cfg is not None:
            return _IOS_BLANK        # an IOS node whose config has no login lines: no LabDesk day-0 here
        return base
    if platform in IOS_DEFS:
        return _IOS_BLANK
    return None


def for_node(nv, env: dict | None = None) -> FirstLogin | None:
    """``nv``: controller.NodeView (duck-typed: dtype, raw)."""
    dtype = nv.dtype
    if getattr(dtype, "backend", "cml") == "kvm":
        return first_login(dtype.key, backend="kvm", env=env)
    raw = getattr(nv, "raw", None) or {}
    nd = raw.get("node_definition") or getattr(dtype, "node_definition", "") or dtype.key
    return first_login(nd, raw.get("configuration") if "configuration" in raw else None)


def load_env() -> dict:
    """cml-credentials.env (only the JUNOS_* / FGT_* admin keys are used here)."""
    from . import config
    try:
        vals = config.load_env_file(config.CRED_FILE)
    except OSError:
        return {}
    return {k: v for k, v in vals.items() if k.startswith(("JUNOS_ADMIN_", "FGT_ADMIN_"))}


def effective(detail, first: FirstLogin | None) -> tuple[FirstLogin | None, str | None]:
    """The login to show for a node: (login, tag).  ``detail``: probe.creds.CredsDetail or None.
    tag: 'default' (first-login defaults, saved by the backfill or not saved yet) | 'saved' (the user's own
    values or the shared DEFAULT_* login) | None (no login)."""
    c = getattr(detail, "creds", None)
    if detail is not None and not getattr(detail, "error", None) and c is not None \
            and (c.username or c.password or c.enable):
        seeded = bool(getattr(detail, "seeded", False))
        has_enable = first.has_enable if first is not None else bool(c.enable)
        note = first.note if (seeded and first is not None) else ""
        fl = FirstLogin(c.username, c.password, c.enable, note,
                        first.source if (seeded and first is not None) else "device-creds", has_enable=has_enable,
                        console_note=first.console_note if first is not None else "")
        return fl, ("default" if seeded else "saved")
    if first is not None:
        return first, "default"
    return None, None


def console_hint(label: str, fl: FirstLogin | None, show: bool = True, mask: str = "••••••") -> str:
    """Event-log / toast line when a console opens: 'R1 first login: admin / cisco (enable: cisco)'."""
    if fl is None:
        return ""

    def sec(v, enc):
        if enc:
            return "encrypted in config"
        if not v:
            return "no password"
        return v if show else mask
    if not fl.username and not fl.password:
        s = f"{label} first login: none — press Enter"
    else:
        s = f"{label} first login: {fl.username or '—'} / {sec(fl.password, fl.password_encrypted)}"
    if fl.enable or fl.enable_encrypted or fl.has_enable:
        s += f" (enable: {sec(fl.enable, fl.enable_encrypted)})"
    if fl.console_note:
        s += f" · {fl.console_note}"
    return s
