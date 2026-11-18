"""EtherChannel (LACP / PAgP / static) configuration: mode rules, IOS command generation, day-0 patching.

Pure (no I/O, no Qt): used by the "Bundle into EtherChannel..." dialog (ui/dialogs.py), by
``LabController.bundle_plan / bundle_apply`` and by the telemetry mismatch detection (``check_modes`` is the
single source of the "never bundles" rules).  Verified live on IOSvL2 (iosvl2-2020, CML 2.10.1).

Mode rules (IEEE 802.3ad LACP, Cisco PAgP, static "on"):

  ============  ===========  ===========================================================
  side A        side B       result
  ============  ===========  ===========================================================
  active        active       bundles (LACP)
  active        passive      bundles (LACP)
  passive       passive      never: neither side starts LACP negotiation
  desirable     desirable    bundles (PAgP)
  desirable     auto         bundles (PAgP)
  auto          auto         never: neither side starts PAgP negotiation
  on            on           bundles without a protocol (static)
  on            LACP / PAgP  never: the negotiating side gets no answer (stand-alone / suspended)
  LACP          PAgP         never: different protocols
  ============  ===========  ===========================================================
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

PROTOCOLS = ("lacp", "pagp", "on")
PROTOCOL_LABEL = {"lacp": "LACP", "pagp": "PAgP", "on": "Static on"}
MODES = {"lacp": ("active", "passive"), "pagp": ("desirable", "auto"), "on": ("on",)}
DEFAULT_MODES = {"lacp": ("active", "active"), "pagp": ("desirable", "desirable"), "on": ("on", "on")}
MODE_PROTOCOL = {"active": "lacp", "passive": "lacp", "desirable": "pagp", "auto": "pagp", "on": "on"}
# words people know, for the inspector / tooltips
MODE_WORDS = {"active": "active (starts LACP)", "passive": "passive (answers LACP only)",
              "desirable": "desirable (starts PAgP)", "auto": "auto (answers PAgP only)",
              "on": "on (static, no negotiation)"}
LOAD_BALANCE = ("src-dst-ip", "src-dst-mac", "src-ip", "dst-ip", "src-mac", "dst-mac")
MAX_PO = 48                     # IOSvL2 accepts Port-channel1..48 (Max Port-channels per group 4, groups 1-48)


def mode_protocol(mode: str | None) -> str | None:
    """'active' -> 'lacp', 'auto' -> 'pagp', 'on' -> 'on' (None for unknown)."""
    return MODE_PROTOCOL.get((mode or "").lower())


def proto_label(proto: str | None) -> str:
    return {"lacp": "LACP", "pagp": "PAgP", "on": "on"}.get((proto or "").lower(), proto or "?")


@dataclass(frozen=True)
class ModeCheck:
    forms: bool
    reason: str = ""            # short canvas text: 'LACP passive ↔ passive — never bundles'
    explain: str = ""           # one plain-English sentence for the inspector / dialog


def check_modes(mode_a: str | None, mode_b: str | None) -> ModeCheck:
    """Will a bundle with these per-side channel-group modes form?  (pure rule table, module docstring)"""
    a, b = (mode_a or "").lower(), (mode_b or "").lower()
    pa, pb = mode_protocol(a), mode_protocol(b)
    if pa is None or pb is None:
        missing = "one side has no channel-group" if (pa is None) != (pb is None) else "no channel-group on either side"
        return ModeCheck(False, f"{missing} — never bundles",
                         "Both ends of every member cable need 'channel-group N mode ...' for an EtherChannel to form.")
    if pa != pb:
        if "on" in (pa, pb):
            other = pb if pa == "on" else pa
            neg = a if pa != "on" else b
            return ModeCheck(False, f"on ↔ {proto_label(other)} {neg} — never bundles",
                             f"One side is static 'on' and sends no {proto_label(other)} messages, so the "
                             f"{proto_label(other)} side never gets an answer: its ports stay stand-alone or "
                             "suspended, while the 'on' side bundles blindly (risk of a loop).")
        return ModeCheck(False, "LACP ↔ PAgP — never bundles",
                         "One side speaks LACP and the other PAgP: the two protocols do not understand each other, "
                         "so the ports never bundle.")
    if pa == "lacp" and a == b == "passive":
        return ModeCheck(False, "LACP passive ↔ passive — never bundles",
                         "Both sides are LACP passive: each only answers LACP, nobody starts it, so the bundle "
                         "never forms. Make at least one side 'active'.")
    if pa == "pagp" and a == b == "auto":
        return ModeCheck(False, "PAgP auto ↔ auto — never bundles",
                         "Both sides are PAgP auto: each only answers PAgP, nobody starts it, so the bundle never "
                         "forms. Make at least one side 'desirable'.")
    return ModeCheck(True)


# ======================================================================= config blocks
def blocks(text: str) -> list[list[str]]:
    """Split an IOS config into top-level blocks (a line without leading space + its indented lines)."""
    out: list[list[str]] = []
    for line in (text or "").splitlines():
        if line.startswith(" ") and out:
            out[-1].append(line)
        else:
            out.append([line])
    return out


_IF_ALIASES = (("port-channel", "Port-channel"), ("po", "Port-channel"), ("gigabitethernet", "GigabitEthernet"),
               ("gi", "GigabitEthernet"), ("fastethernet", "FastEthernet"), ("fa", "FastEthernet"),
               ("tengigabitethernet", "TenGigabitEthernet"), ("te", "TenGigabitEthernet"),
               ("ethernet", "Ethernet"), ("et", "Ethernet"))


def full_ifname(name: str) -> str:
    """'Gi0/1' / 'gigabitethernet0/1' / 'Po1' -> 'GigabitEthernet0/1' / 'Port-channel1' (IOS full form)."""
    s = (name or "").strip().replace(" ", "")
    m = re.match(r"^([A-Za-z\-]+)(\d.*)$", s)
    if not m:
        return s
    word, num = m[1].lower(), m[2]
    for alias, full in _IF_ALIASES:
        if word == alias:
            return full + num
    for alias, full in _IF_ALIASES:
        if full.lower().startswith(word) and len(word) >= 2:
            return full + num
    return m[1] + num


def short_ifname(name: str) -> str:
    full = full_ifname(name)
    for pre, short in (("Port-channel", "Po"), ("GigabitEthernet", "Gi"), ("FastEthernet", "Fa"),
                       ("TenGigabitEthernet", "Te"), ("Ethernet", "Et")):
        if full.startswith(pre):
            return short + full[len(pre):]
    return full


def iface_block(cfg: str, iface: str) -> list[str] | None:
    want = f"interface {full_ifname(iface)}".lower()
    for b in blocks(cfg):
        head = b[0].strip()
        if head.lower().startswith("interface ") and \
                f"interface {full_ifname(head.split(None, 1)[1])}".lower() == want:
            return b
    return None


L2_KEEP = re.compile(r"^\s*switchport\b")


def l2_lines(block: list[str] | None) -> list[str]:
    """The switchport lines of an interface block, in config order (what a Port-channel must copy)."""
    if not block:
        return []
    return [" " + ln.strip() for ln in block[1:] if L2_KEEP.match(ln) and "port-security" not in ln
            and "voice" not in ln]


def l2_lines_from_facts(mode: str, *, allowed: str | None = None, native: int | None = None,
                        access_vlan: int | None = None, nonegotiate: bool = True) -> list[str]:
    """Switchport lines rebuilt from telemetry (``show interfaces trunk`` / ``status``) when no config is known."""
    if mode == "trunk":
        out = [" switchport trunk encapsulation dot1q"]
        if allowed and allowed not in ("1-4094", ""):
            out.append(f" switchport trunk allowed vlan {allowed}")
        if native not in (None, 1):
            out.append(f" switchport trunk native vlan {native}")
        out.append(" switchport mode trunk")
        if nonegotiate:
            out.append(" switchport nonegotiate")
        return out
    if mode == "access" and access_vlan is not None:
        return [" switchport mode access", f" switchport access vlan {access_vlan}"]
    return []


def l2_summary(lines: list[str]) -> str:
    """' switchport mode trunk' ... -> 'trunk, VLAN 10,20' / 'access VLAN 10' / 'default (access VLAN 1)'."""
    txt = [ln.strip() for ln in lines]
    if "switchport mode trunk" in txt:
        allowed = next((t.split()[-1] for t in txt if t.startswith("switchport trunk allowed vlan ")), "all")
        native = next((t.split()[-1] for t in txt if t.startswith("switchport trunk native vlan ")), None)
        return f"trunk, VLAN {allowed}" + (f", native {native}" if native else "")
    acc = next((t.split()[-1] for t in txt if t.startswith("switchport access vlan ")), None)
    if acc or "switchport mode access" in txt:
        return f"access VLAN {acc or 1}"
    return "default (access VLAN 1)"


# ======================================================================= plan
@dataclass
class BundleSide:
    node_key: str
    label: str
    members: list[str]                     # full IOS names, e.g. ['GigabitEthernet0/1', 'GigabitEthernet0/2']
    mode: str                              # active | passive | desirable | auto | on
    l2: list[str] = field(default_factory=list)       # switchport lines copied onto the Port-channel
    peer_label: str = ""
    peer_members: list[str] = field(default_factory=list)
    console_open: bool = False
    booted: bool = True


@dataclass
class BundlePlan:
    po: int
    protocol: str                          # lacp | pagp | on
    a: BundleSide
    b: BundleSide
    load_balance: str | None = None        # None = leave the switch default
    link_keys: list[str] = field(default_factory=list)
    check: ModeCheck = field(default_factory=lambda: ModeCheck(True))
    l2_conflict: str = ""                  # the two sides' switchport configs differ (plain words)
    used: list = field(default_factory=list)          # Port-channel numbers already used on either switch
    l2_source: tuple = ("", "")                       # where each side's switchport lines came from

    @property
    def po_name(self) -> str:
        return f"Port-channel{self.po}"

    def commands(self, side: str) -> list[str]:
        s = self.a if side == "a" else self.b
        return bundle_commands(self.po, s.members, s.mode, s.l2, self.description(side), self.load_balance)

    def description(self, side: str) -> str:
        s, o = (self.a, self.b) if side == "a" else (self.b, self.a)
        return (f"{PROTOCOL_LABEL[self.protocol]} bundle to {o.label} "
                f"({'+'.join(short_ifname(m) for m in s.members)})")


def validate_plan(plan: BundlePlan) -> list[str]:
    """Problems that block Apply (the mode warning is shown but does not block: it is a teaching lab)."""
    errs = []
    if not 1 <= plan.po <= MAX_PO:
        errs.append(f"Port-channel number must be 1-{MAX_PO}")
    if len(plan.a.members) < 2 or len(plan.a.members) != len(plan.b.members):
        errs.append("select at least two parallel links between the same two switches")
    for s in (plan.a, plan.b):
        if s.mode not in MODES.get(plan.protocol, ()) and not (plan.protocol == "mixed"):
            errs.append(f"{s.label}: mode '{s.mode}' is not a {PROTOCOL_LABEL.get(plan.protocol, plan.protocol)} mode")
    if plan.load_balance and plan.load_balance not in LOAD_BALANCE:
        errs.append(f"unknown load-balance method {plan.load_balance}")
    return errs


def next_free_po(used: set[int] | list[int]) -> int:
    used = set(used)
    n = 1
    while n in used:
        n += 1
    return n


def used_po_numbers(cfg: str) -> set[int]:
    """Port-channel numbers present in a config (interfaces and channel-group lines)."""
    out = {int(x) for x in re.findall(r"(?mi)^interface Port-channel(\d+)\s*$", cfg or "")}
    out |= {int(x) for x in re.findall(r"(?mi)^\s*channel-group (\d+) mode", cfg or "")}
    return out


def bundle_commands(po: int, members: list[str], mode: str, l2: list[str], description: str = "",
                    load_balance: str | None = None) -> list[str]:
    """Config-mode lines (IOS) that build Port-channel ``po`` from ``members`` on one switch.  The
    Port-channel is created first with the members' switchport config, then every member gets the same
    switchport lines + ``channel-group po mode <mode>`` + ``no shutdown`` (IOS rejects a member whose
    trunk/access config differs from the Port-channel's)."""
    out = []
    if load_balance:
        out.append(f"port-channel load-balance {load_balance}")
    out.append(f"interface Port-channel{po}")
    if description:
        out.append(f" description {description}")
    out += [ln if ln.startswith(" ") else " " + ln for ln in l2]
    out.append(" exit")
    for m in members:
        out.append(f"interface {full_ifname(m)}")
        out += [ln if ln.startswith(" ") else " " + ln for ln in l2]
        out.append(f" channel-group {po} mode {mode}")
        out.append(" no shutdown")
        out.append(" exit")
    return out


def unbundle_commands(po: int, members: list[str], l2: list[str]) -> list[str]:
    """Take the members out of the bundle (they keep / get back the switchport config), delete the
    Port-channel, and make sure the members are up again.  (Verified live on IOSvL2: ``no channel-group``
    without the number; ``no channel-group 1`` answers '% Incomplete command'.)"""
    out = []
    for m in members:
        out += [f"interface {full_ifname(m)}", " no channel-group"]   # IOSvL2: 'no channel-group <n>' is incomplete
        out += [ln if ln.startswith(" ") else " " + ln for ln in l2]
        out += [" no shutdown", " exit"]
    out.append(f"no interface Port-channel{po}")
    return out


# ======================================================================= day-0 (CML node configuration)
def _insert_index(bl: list[list[str]], *, before_first_gi: bool) -> int:
    if before_first_gi:
        for i, b in enumerate(bl):
            if re.match(r"(?i)^interface (GigabitEthernet|FastEthernet|Ethernet|TenGig)", b[0]):
                return i
    last_if = None
    for i, b in enumerate(bl):                       # after the last physical interface (before Vlan SVIs)
        if re.match(r"(?i)^interface (GigabitEthernet|FastEthernet|Ethernet|TenGig)", b[0]):
            last_if = i
    if last_if is None:
        for i, b in enumerate(bl):
            if b[0].lower().startswith("interface "):
                last_if = i
    if last_if is not None:
        return last_if + 1
    for i, b in enumerate(bl):
        if b[0].startswith(("line ", "end")):
            return i
    return len(bl)


def patch_day0(cfg: str, po: int, members: list[str], mode: str, l2: list[str], description: str = "",
               load_balance: str | None = None, member_descriptions: dict[str, str] | None = None) -> str:
    """Idempotently add Port-channel ``po`` to an IOS day-0 config: the ``interface Port-channelN`` block
    (before the first physical interface), every member block (created when missing, e.g. a new Gi1/0)
    with the switchport lines + ``channel-group`` + ``no shutdown``, and ``port-channel load-balance``.
    Every other line (EEM applets, SVIs, VLANs, ...) is kept verbatim."""
    md = {full_ifname(k): v for k, v in (member_descriptions or {}).items()}
    want_po = [f"interface Port-channel{po}"] + ([f" description {description}"] if description else []) + \
        [ln if ln.startswith(" ") else " " + ln for ln in l2]
    names = [full_ifname(m) for m in members]
    bl = [b for b in blocks(cfg) if b and b != [""]]
    out: list[list[str]] = []
    seen = set()
    po_at = None
    for b in bl:
        head = b[0].strip()
        if head.lower() == f"interface port-channel{po}".lower():
            po_at = len(out)                                 # replaced in place below (idempotent)
            continue
        if load_balance and head.startswith("port-channel load-balance"):
            continue
        if head.lower().startswith("interface "):
            nm = full_ifname(head.split(None, 1)[1])
            if nm in names:
                seen.add(nm)
                body = [ln for ln in b[1:] if not re.match(r"^\s*(channel-group|switchport|shutdown|no shutdown)\b",
                                                           ln)]
                if nm in md:
                    body = [ln for ln in body if not ln.strip().startswith("description ")]
                    body.insert(0, f" description {md[nm]}")
                b = [f"interface {nm}"] + body + [ln if ln.startswith(" ") else " " + ln for ln in l2] + \
                    [f" channel-group {po} mode {mode}", " no shutdown"]
        out.append(b)
    for nm in names:
        if nm in seen:
            continue
        nb = [f"interface {nm}"] + ([f" description {md[nm]}"] if nm in md else []) + \
            [ln if ln.startswith(" ") else " " + ln for ln in l2] + [f" channel-group {po} mode {mode}", " no shutdown"]
        out.insert(_insert_index(out, before_first_gi=False), nb)
    if po_at is not None:
        out.insert(po_at, want_po)
    else:
        out.insert(_insert_index(out, before_first_gi=True), want_po)
    if load_balance:
        i = next((i for i, b in enumerate(out) if b[0].lower().startswith("interface ")), len(out))
        out.insert(i, [f"port-channel load-balance {load_balance}"])
    text = "\n".join("\n".join(b) for b in out)
    return text.rstrip("\n") + "\n"


def unpatch_day0(cfg: str, po: int, l2: list[str] | None = None) -> str:
    """Remove Port-channel ``po`` from a day-0 config: its interface block and every member's
    ``channel-group po`` line (members keep their switchport config; ``l2`` is added when a member had
    none)."""
    out = []
    for b in [b for b in blocks(cfg) if b and b != [""]]:
        head = b[0].strip()
        if head.lower() == f"interface port-channel{po}".lower():
            continue
        if head.lower().startswith("interface ") and any(re.match(rf"^\s*channel-group {po} mode", ln) for ln in b):
            body = [ln for ln in b[1:] if not re.match(rf"^\s*channel-group {po}\b", ln)]
            if l2 and not any(L2_KEEP.match(ln) for ln in body):
                body = [ln for ln in body if ln.strip() not in ("shutdown", "no shutdown")] + \
                    [ln if ln.startswith(" ") else " " + ln for ln in l2] + [" no shutdown"]
            b = [b[0]] + body
        out.append(b)
    return "\n".join("\n".join(b) for b in out).rstrip("\n") + "\n"


def day0_has_bundle(cfg: str, po: int, members: list[str], mode: str) -> bool:
    if iface_block(cfg, f"Port-channel{po}") is None:
        return False
    for m in members:
        b = iface_block(cfg, m)
        if b is None or not any(ln.strip() == f"channel-group {po} mode {mode}" for ln in b):
            return False
    return True
