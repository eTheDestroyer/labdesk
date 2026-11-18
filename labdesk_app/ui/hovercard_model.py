"""Hover card content (pure: NodeView + stored credentials -> rows).  No widgets here, so tests can check exactly
what the card says.  ``ui/hovercard.py`` paints it.

Rules: only facts that exist are shown (nothing is invented); a missing fact is a muted hint, never a guess.
On-device passwords that are encrypted are only ever called "encrypted (<scheme>)" - the hash is not kept anywhere.
Plain-text passwords (stored LabDesk login, ``password 0 x`` on the device) are shown in clear unless
``show_passwords`` is off (View ▸ Show passwords on hover), then they are masked.
"""
from __future__ import annotations

from dataclasses import dataclass, field

MASK = "••••••"                       # fixed length: never reveals the length of the real value
MAX_IFACES = 8
MAX_PROTO_ROWS = 8
CREDS_HINT = "right-click ▸ Credentials…"
PLAIN_NOTE = "plain text, not encrypted"

STATE_WORDS = {"BOOTED": ("Booted", "up", "booted"), "running": ("Running", "up", "booted"),
               "STARTED": ("Booting", "muted", "booting"), "QUEUED": ("Queued", "muted", "booting"),
               "booting": ("Booting", "muted", "booting"), "stopping": ("Stopping", "muted", "booting"),
               "STOPPED": ("Stopped", "muted", "stopped"), "stopped": ("Stopped", "muted", "stopped"),
               "DEFINED_ON_CORE": ("Wiped", "muted", "stopped"), "defined": ("Defined", "muted", "stopped"),
               "DISCONNECTED": ("Disconnected", "down", "disconnected"),
               "missing": ("Not created", "down", "disconnected"), "unknown": ("Unknown", "down", "disconnected")}
COLLECTOR_WORDS = {"learning": ("learning…", "muted"), "paused_console": ("paused: console in use", "muted"),
                   "console_busy": ("console busy", "warn"), "needs_credentials": ("needs login / enable secret", "warn"),
                   "error": ("probe error", "down"), "unverified": ("collector beta / unverified", "muted"),
                   "limited": ("limited", "warn"), "no_mgmt": ("no mgmt address", "warn"),
                   "disabled": ("probing off", "muted")}
SIDE_GLYPH = {"UP": ("up", "up"), "DOWN": ("down", "down"), "PROTO_DOWN": ("proto_down", "proto down"),
              "ADMIN_DOWN": ("admin_down", "shut")}


def _ev(x) -> str:
    return getattr(x, "value", x) if x is not None else ""


@dataclass
class Row:
    text: str                          # left column: interface / account / key
    value: str = ""
    glyph: str | None = None           # theme.status_glyph state drawn before ``text``
    tone: str | None = None            # value colour: ok | warn | down | muted | None (ink)
    tag: str | None = None             # protocol hue key: ``text`` is drawn as a tag in that hue
    mono: bool = False                 # value in the mono face (IPs, passwords)
    note: str = ""                     # right-aligned muted remark
    note_tone: str | None = None
    secret: bool = False               # value is a password (shown or masked)


@dataclass
class Section:
    title: str
    rows: list[Row] = field(default_factory=list)
    aside: str = ""                    # right side of the section header ('5 of 9 up')
    more: int = 0                      # '+N more'
    note: str = ""                     # muted line (hint / why empty)
    note_tone: str | None = None


@dataclass
class CardModel:
    key: str
    title: str
    subtitle: str
    vendor: str
    state_word: str
    state_tone: str
    state_glyph: str
    meta: list[Row] = field(default_factory=list)
    sections: list[Section] = field(default_factory=list)

    def section(self, title: str) -> Section | None:
        return next((s for s in self.sections if s.title == title), None)

    def text(self) -> str:
        """Plain text of the card (tests / debugging)."""
        out = [f"{self.title} · {self.subtitle} · {self.state_word}"]
        out += [f"{r.text}: {r.value}" + (f" ({r.note})" if r.note else "") for r in self.meta]
        for s in self.sections:
            out.append(f"[{s.title}]" + (f" {s.aside}" if s.aside else ""))
            out += [f"  {r.text}: {r.value}" + (f" ({r.note})" if r.note else "") for r in s.rows]
            if s.more:
                out.append(f"  +{s.more} more")
            if s.note:
                out.append(f"  {s.note}")
        return "\n".join(out)


def _secret(v: str | None, show: bool) -> str:
    if not v:
        return ""
    return v if show else MASK


def _age(age: float | None) -> str:
    if age is None:
        return ""
    if age < 1.5:
        return "just now"
    if age < 90:
        return f"{age:.0f} s ago"
    if age < 5400:
        return f"{age / 60:.0f} min ago"
    return f"{age / 3600:.1f} h ago"


def _iface_rows(nt) -> Section | None:
    ifaces = list(getattr(nt, "ifaces", None) or [])
    if not ifaces:
        return None
    from ..parsers.common import iface_key
    addrs = getattr(nt, "addrs", None) or {}
    rows = []
    for f in ifaces:
        a = addrs.get(iface_key(f.name))
        ip = ""
        if a is not None and a.primary:
            ip = f"{a.primary[0]}/{a.primary[1]}"
        elif f.ip and f.prefixlen is not None:
            ip = f"{f.ip}/{f.prefixlen}"
        elif f.ip:
            ip = f.ip
        elif a is not None and a.method in ("dhcp", "unnumbered", "negotiated"):
            ip = a.method
        side = _ev(f.side)
        glyph, word = SIDE_GLYPH.get(side, ("neutral", "?"))
        short = f.short or f.name
        if short.startswith("Vl") and short[2:].isdigit():
            short = "Vlan" + short[2:]                 # 'Vl10' reads as 'V110' in many mono faces
        rows.append((0 if ip else (1 if side == "UP" else 2), Row(short, ip, glyph=glyph, mono=True,
                                                                  note=word, tone=None if ip else "muted")))
    up = sum(1 for _, r in rows if r.glyph == "up")
    ordered = [r for _, r in sorted(enumerate(rows), key=lambda x: (x[1][0], x[0]))]
    ordered = [r for _, r in ordered]
    sec = Section("Interfaces", ordered[:MAX_IFACES], aside=f"{up} of {len(rows)} up",
                  more=max(0, len(ordered) - MAX_IFACES))
    return sec


def _names(items, key="name", limit=3) -> str:
    names = [x.get(key) or x.get("neighbor_id") or x.get("ip") or "?" for x in items][:limit]
    extra = len(items) - len(names)
    return ", ".join(names) + (f" +{extra}" if extra > 0 else "")


def _proto_rows(nv, nt) -> list[Row]:
    rows: list[Row] = []
    nbrs = list(getattr(nt, "ospf_nbrs", None) or [])
    if nbrs:
        full = [x for x in nbrs if str(x.get("adj_state") or x.get("state", "")).upper().startswith("FULL")]
        tone = "ok" if len(full) == len(nbrs) else "warn"
        rows.append(Row("OSPF", f"{len(full)}/{len(nbrs)} neighbour{'s' if len(nbrs) != 1 else ''} FULL",
                        tag="OSPF", tone=tone, note=_names(nbrs)))
    else:
        chip = next((c for c in (getattr(nv, "chips", None) or []) if c.kind == "OSPF"), None)
        if chip is not None:
            rows.append(Row("OSPF", "configured, no neighbour", tag="OSPF", tone="warn"))
    bgp = getattr(nt, "bgp", None)
    if bgp:
        peers = bgp.get("peers") or []
        up = [p for p in peers if p.get("established")]
        rows.append(Row("BGP", f"AS {bgp.get('local_as')} · {len(up)}/{len(peers)} peers up",
                        tag="BGP", tone="ok" if peers and len(up) == len(peers) else "warn",
                        note=_names(peers)))
    for g in getattr(nt, "fhrp", None) or []:
        proto = _ev(g.proto)
        rows.append(Row(proto, f"grp {g.group} {g.role}" + (f" · VIP {g.vip}" if g.vip else ""), tag=proto,
                        tone="ok" if g.is_primary else None, note=_short(g.iface)))
    dr = getattr(nt, "default_route", None)
    if dr is not None and getattr(dr, "present", False):
        nh = dr.next_hops[0][0] if dr.next_hops and dr.next_hops[0][0] else (
            dr.next_hops[0][1] if dr.next_hops else "")
        rows.append(Row("0/0", (dr.source_label or dr.code or "default") + (f" via {nh}" if nh else ""),
                        tag="DEFAULT"))
    sw = getattr(nt, "switch", None)
    if sw is not None:
        if sw.stp_label or sw.stp:
            if sw.root_for:
                txt = f"root bridge for VLAN {','.join(str(v) for v in sw.root_for)}"
                tone = "ok"
            else:
                roots = sorted({v for v in (sw.root_of or {}).values() if v})
                txt = f"root is {', '.join(roots)}" if roots else "not root"
                tone = None
            rows.append(Row("STP", (f"{sw.stp_label} · " if sw.stp_label else "") + txt, tag="STP", tone=tone))
        for port, role, vlans in sw.blocked or []:
            rows.append(Row("STP", f"{port} blocking ({role})", tag="STP", tone="warn",
                            note=("VLAN " + ",".join(str(v) for v in vlans)) if vlans else ""))
        if sw.vlans:
            rows.append(Row("VLAN", f"{len(sw.vlans)} VLAN{'s' if len(sw.vlans) != 1 else ''}", tag="VLAN",
                            note=", ".join(str(v.id) for v in sw.vlans[:6])))
        for side in sw.ec_sides or []:
            n = len(side.members)
            b = side.bundled
            proto = _ev(side.proto)
            proto = {"lacp": "LACP", "pagp": "PAgP", "on": "static"}.get(proto, proto)
            rows.append(Row("EC", f"{side.po or 'Po?'} {proto} {b}/{n}" + (" up" if side.up else " down"),
                            tag="EC", tone="ok" if side.up and b == n else ("warn" if side.up else "down")))
    return rows


def _short(name: str | None) -> str:
    if not name:
        return ""
    try:
        from ..parsers.common import short_iface
        return short_iface(name)
    except Exception:  # noqa: BLE001
        return name


def _login_section(creds, show: bool) -> Section:
    sec = Section("Login")
    if creds is None:
        sec.note = f"no credentials saved — {CREDS_HINT}"
        return sec
    if getattr(creds, "error", None):
        sec.note, sec.note_tone = f"{creds.error}", "warn"
        return sec
    c = creds.creds
    if not (c.username or c.password or c.enable):
        sec.note = f"no credentials saved — {CREDS_HINT}"
        return sec
    dflt = "default" if creds.uses_defaults else ""
    user = c.username or "—"
    if c.password:
        rows_val = f"{user} / {_secret(c.password, show)}"
    else:
        rows_val = f"{user} / (no password)"
    sec.rows.append(Row("LabDesk login", rows_val, mono=True, secret=bool(c.password), note=dflt))
    if c.enable:
        src = creds.source("ENABLE")
        sec.rows.append(Row("enable", _secret(c.enable, show), mono=True, secret=True,
                            note="default" if src == "default" and not dflt else ""))
    if not show:
        sec.aside = "hidden"
    return sec


def _device_section(nv, nt, show: bool) -> Section:
    sec = Section("On the device")
    kvm = str(getattr(nv, "key", "")).startswith("kvm:")
    cs = _ev(getattr(nt, "collector_state", None)) if nt is not None else ""
    if nt is None or cs in ("off", ""):
        sec.note = "read from the running-config while the device runs"
        return sec
    if cs == "not_probed":
        sec.note = "not read: LabDesk does not probe this kind of device"
        return sec
    acc = getattr(nt, "accounts", None)
    if acc is None:
        note = getattr(nt, "accounts_note", "") or ""
        if kvm or _ev(nt.collector_state) == "unverified":
            sec.note = "not read: the host-KVM collector is not wired yet"
        elif note == "needs enable secret":
            sec.note, sec.note_tone = f"on-device accounts: needs enable secret ({CREDS_HINT})", "warn"
        elif note:
            sec.note = f"on-device accounts: {note}"
        else:
            sec.note = "on-device accounts: not read yet"
        return sec
    for a in acc.accounts:
        name = a.name if a.kind != "enable" else a.name.replace(" level ", " lvl ")
        if a.scheme == "no password":
            sec.rows.append(Row(name, "no password", tone="warn"))
        elif a.encrypted:
            sec.rows.append(Row(name, "encrypted", tone="warn" if a.weak else "ok", note=a.scheme,
                                note_tone="warn" if a.weak else None))
        else:
            sec.rows.append(Row(name, _secret(a.plaintext, show), mono=True, secret=True,
                                note=PLAIN_NOTE, note_tone="down"))
    notes = []
    if not acc.accounts:
        notes.append("no local accounts or passwords configured")
    if acc.password_encryption is not None:
        notes.append(f"service password-encryption: {'on' if acc.password_encryption else 'off'}")
    sec.note = " · ".join(notes)
    age = getattr(nt, "accounts_age_s", None)
    if age is not None:
        sec.aside = f"read {_age(age)}"
    return sec


def build_card(nv, *, creds=None, show_passwords: bool = True, mgmt_ip: str | None = None) -> CardModel:
    """``nv``: controller.NodeView (duck-typed: key, label, dtype, state, tele, chips).  ``creds``: probe.creds
    CredsDetail or None.  ``mgmt_ip``: the stored / detected management address."""
    dtype = nv.dtype
    nt = getattr(nv, "tele", None)
    platform = (getattr(dtype, "title", "") or "").split(" (")[0]
    backend = "host KVM" if getattr(dtype, "backend", "") == "kvm" else "CML"
    word, tone, glyph = STATE_WORDS.get(nv.state, (str(nv.state or "unknown").capitalize(), "muted", "neutral"))
    m = CardModel(nv.key, nv.label, f"{platform} · {backend}" if platform else backend,
                  getattr(dtype, "vendor", "generic") or "generic", word, tone, glyph)
    if mgmt_ip:
        m.meta.append(Row("Mgmt IP", mgmt_ip, mono=True))
    if nt is not None:
        if nt.hostname and nt.hostname != nv.label:
            m.meta.append(Row("Hostname", nt.hostname, mono=True))
        if nt.rid:
            m.meta.append(Row("Router ID", nt.rid, mono=True))
        cs = _ev(nt.collector_state)
        if cs in COLLECTOR_WORDS:
            w, t = COLLECTOR_WORDS[cs]
            m.meta.append(Row("Probe", w, tone=t,
                              note=f"data {_age(nt.last_probe_age_s)}" if nt.last_probe_age_s is not None else ""))
        elif nt.last_probe_age_s is not None:
            m.meta.append(Row("Probed", _age(nt.last_probe_age_s), tone="muted" if nt.last_probe_age_s > 45 else None,
                              note="probing…" if nt.probing else ""))
        sec = _iface_rows(nt)
        if sec is not None:
            m.sections.append(sec)
        prow = _proto_rows(nv, nt)
        if prow:
            m.sections.append(Section("Protocols", prow[:MAX_PROTO_ROWS], more=max(0, len(prow) - MAX_PROTO_ROWS)))
    m.sections.append(_login_section(creds, show_passwords))
    m.sections.append(_device_section(nv, nt, show_passwords))
    return m
