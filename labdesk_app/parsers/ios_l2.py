"""Cisco IOS / IOSvL2 switching parsers (VLANs, trunks, switchports, spanning tree, MAC table, CDP,
EtherChannel).

Same contract as parsers/ios.py: ``text -> ParseResult``, pure, never raise; header-only output is
ok=True with an empty result.  Every command here was verified live at the user-exec '>' prompt of
IOSvL2 (iosvl2-2020) through the CML terminal server, so the probe never needs 'enable' for them.
Fixtures: tests/fixtures/switching/.
"""
from __future__ import annotations

import re

from .common import ParseResult, column_spans, expand_iface, lines, short_iface
from .ios import _guard

__all__ = [
    "parse_vlan_brief", "parse_interfaces_trunk", "parse_switchport_status", "parse_spanning_tree",
    "parse_spanning_tree_summary", "parse_mac_count", "parse_mac_table", "parse_cdp_neighbors",
    "parse_etherchannel_summary", "parse_stp_topology_changes", "parse_routing_enabled",
    "parse_lacp_internal", "parse_lacp_neighbor", "parse_lacp_sys_id", "parse_pagp_neighbor", "parse_pagp_internal",
    "parse_etherchannel_load_balance", "parse_etherchannel_detail", "EC_PORT_FLAGS", "EC_PO_FLAGS",
    "ec_syslog_reason",
    "vlan_ranges", "vlan_text", "in_ranges", "STP_ROLES", "STP_STATES",
]

RESERVED_VLANS = range(1002, 1006)          # fddi/token-ring defaults: never shown as user VLANs

STP_ROLES = {"Root": "Root", "Desg": "Designated", "Altn": "Alternate", "Back": "Backup", "Mstr": "Master",
             "Disa": "Disabled", "Shr": "Shared", "None": "None"}
STP_STATES = {"FWD": "forwarding", "BLK": "blocking", "LRN": "learning", "LIS": "listening",
              "DIS": "disabled", "BKN": "broken"}


# ------------------------------------------------------------------ VLAN lists
def vlan_ranges(text: str | None) -> list[tuple[int, int]]:
    """'1-4094' / '10,20,30-32' / 'none' -> [(lo, hi)]  (wrapped continuation text is joined first)."""
    out = []
    for part in re.split(r"[,\s]+", (text or "").strip()):
        if not part or part.lower() == "none":
            continue
        m = re.fullmatch(r"(\d+)(?:-(\d+))?", part)
        if m:
            lo = int(m[1])
            hi = int(m[2]) if m[2] else lo
            out.append((lo, hi))
    return out


def in_ranges(v: int, ranges: list[tuple[int, int]]) -> bool:
    return any(lo <= v <= hi for lo, hi in ranges)


def vlan_text(vlans) -> str:
    """[10, 20, 21, 22] -> '10,20-22'; [] -> 'none'."""
    vs = sorted(set(int(v) for v in vlans))
    if not vs:
        return "none"
    out, start, prev = [], vs[0], vs[0]
    for v in vs[1:] + [None]:
        if v is not None and v == prev + 1:
            prev = v
            continue
        out.append(f"{start}" if start == prev else (f"{start},{prev}" if prev == start + 1 else f"{start}-{prev}"))
        if v is not None:
            start = prev = v
    return ",".join(out)


def _ports(text: str) -> list[str]:
    return [short_iface(p.strip()) for p in text.split(",") if p.strip()]


# ------------------------------------------------------------------ show vlan brief
_VLAN_RE = re.compile(r"^(?P<id>\d{1,4})\s+(?P<name>\S+)\s+(?P<status>active|act/unsup|act/lshut|sus/lshut|"
                      r"suspended|act/ishut|sus/ishut)\s*(?P<ports>.*)$")


def parse_vlan_brief(text) -> ParseResult:
    """show vlan brief -> [{id, name, status, ports: [short access ports], reserved: bool}]"""
    if (g := _guard(text)) is not None:
        return g
    out, cur = [], None
    for ln in lines(text):
        if not ln.strip() or ln.startswith(("VLAN Name", "----")):
            continue
        m = _VLAN_RE.match(ln)
        if m:
            cur = {"id": int(m["id"]), "name": m["name"], "status": m["status"], "ports": _ports(m["ports"]),
                   "reserved": int(m["id"]) in RESERVED_VLANS}
            out.append(cur)
        elif cur is not None and ln.startswith(" ") and re.search(r"[A-Za-z]+\d", ln):
            cur["ports"] += _ports(ln)                   # wrapped port list
    return ParseResult(True, out)


# ------------------------------------------------------------------ show interfaces trunk
def parse_interfaces_trunk(text) -> ParseResult:
    """show interfaces trunk ->
    {short port: {mode, encap, status, native, allowed, active, forwarding}}; the VLAN fields are
    [(lo, hi)] ranges, plus '<field>_text' with the raw device text ('10,20', '1-4094', 'none')."""
    if (g := _guard(text)) is not None:
        return g
    out: dict[str, dict] = {}
    section = None
    last = None
    for ln in lines(text):
        s = ln.strip()
        if not s:
            continue
        if s.startswith("Port") and "Mode" in s and "Native" in s:
            section = "head"
            continue
        if s.startswith("Port") and "allowed on trunk" in s:
            section = "allowed"
            continue
        if s.startswith("Port") and "allowed and active" in s:
            section = "active"
            continue
        if s.startswith("Port") and "forwarding state" in s:
            section = "forwarding"
            continue
        if section is None:
            continue
        if ln.startswith(" ") and last and section != "head":         # wrapped VLAN list
            out[last][section + "_text"] += s
            continue
        parts = s.split()
        port = short_iface(parts[0])
        if section == "head" and len(parts) >= 5:
            out[port] = {"port": port, "iface": expand_iface(parts[0]), "mode": parts[1], "encap": parts[2],
                         "status": parts[3], "native": int(parts[4]) if parts[4].isdigit() else None,
                         "allowed_text": "", "active_text": "", "forwarding_text": ""}
        elif section != "head" and port in out and len(parts) >= 2:
            out[port][section + "_text"] = parts[1]
            last = port
    for d in out.values():
        for k in ("allowed", "active", "forwarding"):
            d[k] = vlan_ranges(d[k + "_text"])
    return ParseResult(True, out)


# ------------------------------------------------------------------ show interfaces status
_STATUS_WORDS = ("connected", "notconnect", "disabled", "err-disabled", "inactive", "monitoring", "suspended",
                 "sfpAbsent", "xcvrAbsen", "noOperMem", "faulty")


def parse_switchport_status(text) -> ParseResult:
    """show interfaces status ->
    [{port (short), iface (full), name, status, vlan: int|'trunk'|'routed'|None, duplex, speed, type}]"""
    if (g := _guard(text)) is not None:
        return g
    out = []
    header = None
    cols = {}
    for ln in lines(text):
        if ln.startswith("Port") and "Status" in ln and "Vlan" in ln:
            header = ln
            cols = column_spans(header, ["Port", "Name", "Status", "Vlan", "Duplex", "Speed", "Type"])
            continue
        if header is None or not ln.strip():
            continue
        port = ln.split()[0]
        # 'Status' is the reliable anchor: find the status word at/after its column
        m = None
        for w in _STATUS_WORDS:
            for mm in re.finditer(rf"(?<!\S){re.escape(w)}(?!\S)", ln):
                if mm.start() >= cols.get("Name", 10) - 1:
                    m = mm
                    break
            if m:
                break
        if not m:
            continue
        name = ln[cols.get("Name", len(port)):m.start()].strip()
        rest = ln[m.end():].split()
        vlan_raw = rest[0] if rest else ""
        vlan = int(vlan_raw) if vlan_raw.isdigit() else (vlan_raw or None)
        out.append({"port": short_iface(port), "iface": expand_iface(port), "name": name or None,
                    "status": m.group(0), "vlan": vlan,
                    "duplex": rest[1] if len(rest) > 1 else None, "speed": rest[2] if len(rest) > 2 else None,
                    "type": " ".join(rest[3:]) or None})
    return ParseResult(True, out)


# ------------------------------------------------------------------ show spanning-tree
_STP_PORT_RE = re.compile(r"^(?P<iface>[A-Za-z][\w\-./:]*\d)\s+(?P<role>Root|Desg|Altn|Back|Mstr|Disa|Shr|None)\s+"
                          r"(?P<state>FWD|BLK|LRN|LIS|DIS|BKN)\*?\s*(?P<cost>\d+)\s+(?P<prio>\d+)\.(?P<nbr>\d+)"
                          r"\s*(?P<type>.*)$")


def _bridge_mac(m: str) -> str:
    return m.lower()


def parse_spanning_tree(text) -> ParseResult:
    """show spanning-tree ->
    {vlan: {vlan, protocol, root_priority, root_address, root_cost, root_port, is_root,
            bridge_priority, bridge_address, ports: [{iface, short, role, state, cost, prio, nbr, type,
            edge, p2p, inconsistent}]}}   (MST instances use 'MST0' -> key 0 and 'instance': True)"""
    if (g := _guard(text)) is not None:
        return g
    out: dict[int, dict] = {}
    cur = None
    block = None
    for ln in lines(text):
        s = ln.strip()
        m = re.match(r"^(VLAN|MST)(\d+)\s*$", s)
        if m:
            cur = {"vlan": int(m[2]), "instance": m[1] == "MST", "protocol": None, "root_priority": None,
                   "root_address": None, "root_cost": 0, "root_port": None, "is_root": False,
                   "bridge_priority": None, "bridge_address": None, "ports": []}
            out[cur["vlan"]] = cur
            block = None
            continue
        if cur is None:
            continue
        m = re.match(r"Spanning tree enabled protocol (\S+)", s)
        if m:
            cur["protocol"] = m[1]
            continue
        if s.startswith("Root ID"):
            block = "root"
        elif s.startswith("Bridge ID"):
            block = "bridge"
        m = re.search(r"Priority\s+(\d+)", s)
        if m and block in ("root", "bridge") and (s.startswith(("Root ID", "Bridge ID"))):
            cur[f"{block}_priority"] = int(m[1])
            continue
        m = re.match(r"Address\s+([0-9a-fA-F.]+)", s)
        if m and block in ("root", "bridge"):
            cur[f"{block}_address"] = _bridge_mac(m[1])
            continue
        if block == "root":
            m = re.match(r"Cost\s+(\d+)", s)
            if m:
                cur["root_cost"] = int(m[1])
                continue
            m = re.match(r"Port\s+\d+\s+\((\S+)\)", s)
            if m:
                cur["root_port"] = short_iface(m[1])
                continue
            if "This bridge is the root" in s:
                cur["is_root"] = True
                continue
        m = _STP_PORT_RE.match(s)
        if m:
            typ = m["type"].strip()
            cur["ports"].append({"iface": expand_iface(m["iface"]), "short": short_iface(m["iface"]),
                                 "role": m["role"], "state": m["state"], "cost": int(m["cost"]),
                                 "prio": int(m["prio"]), "nbr": int(m["nbr"]), "type": typ,
                                 "edge": "Edge" in typ, "p2p": "P2p" in typ,
                                 "inconsistent": "*" in s.split()[2] or "_Inc" in typ})
    for v in out.values():
        if v["root_address"] and v["root_address"] == v["bridge_address"]:
            v["is_root"] = True
    return ParseResult(True, out)


def parse_spanning_tree_summary(text) -> ParseResult:
    """show spanning-tree summary -> {mode, root_for: [vlan ids], vlans: {vlan: {blocking, listening,
    learning, forwarding, active}}, flags: {portfast_default, bpduguard_default, ...}}"""
    if (g := _guard(text)) is not None:
        return g
    mode = None
    root_for: list[int] = []
    vlans: dict[int, dict] = {}
    flags = {}
    in_root = False
    for ln in lines(text):
        s = ln.strip()
        m = re.match(r"Switch is in (\S+) mode", s)
        if m:
            mode = m[1]
            continue
        m = re.match(r"Root bridge for:\s*(.*)$", s)
        if m:
            in_root = True
            root_for += [int(x) for x in re.findall(r"(?:VLAN|MST)(\d+)", m[1])]
            continue
        if in_root and ln.startswith(" ") and re.search(r"(?:VLAN|MST)\d+", s):
            root_for += [int(x) for x in re.findall(r"(?:VLAN|MST)(\d+)", s)]
            continue
        in_root = False
        m = re.match(r"(.+?)\s+is\s+(enabled|disabled)", s)
        if m:
            flags[re.sub(r"\W+", "_", m[1].strip().lower())] = m[2] == "enabled"
            continue
        m = re.match(r"^(?:VLAN|MST)(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s*$", s)
        if m:
            vlans[int(m[1])] = {"blocking": int(m[2]), "listening": int(m[3]), "learning": int(m[4]),
                                "forwarding": int(m[5]), "active": int(m[6])}
    return ParseResult(True, {"mode": mode, "root_for": sorted(set(root_for)), "vlans": vlans, "flags": flags})


# ------------------------------------------------------------------ MAC table
def parse_mac_count(text) -> ParseResult:
    """show mac address-table count -> {vlans: {vlan: {dynamic, static, total}}, total, space}"""
    if (g := _guard(text)) is not None:
        return g
    vlans: dict[int, dict] = {}
    cur = None
    space = None
    for ln in lines(text):
        s = ln.strip()
        m = re.match(r"Mac Entries for Vlan (\d+)", s, re.I)
        if m:
            cur = vlans.setdefault(int(m[1]), {"dynamic": 0, "static": 0, "total": 0})
            continue
        m = re.match(r"(Dynamic|Static|Total)\s+(?:Address Count|Mac Addresses)\s*:\s*(\d+)", s, re.I)
        if m and cur is not None:
            cur[m[1].lower()] = int(m[2])
            continue
        m = re.match(r"Total Mac Address Space Available:\s*(\d+)", s, re.I)
        if m:
            space = int(m[1])
    return ParseResult(True, {"vlans": vlans, "total": sum(v["total"] for v in vlans.values()), "space": space})


_MAC_RE = re.compile(r"^\*?\s*(?P<vlan>\d+|All)\s+(?P<mac>[0-9a-fA-F]{4}\.[0-9a-fA-F]{4}\.[0-9a-fA-F]{4})\s+"
                     r"(?P<type>\S+)\s+(?P<ports>.*)$")


def parse_mac_table(text) -> ParseResult:
    """show mac address-table [dynamic] -> [{vlan, mac, type, port}]"""
    if (g := _guard(text)) is not None:
        return g
    out = []
    for ln in lines(text):
        m = _MAC_RE.match(ln.strip())
        if not m:
            continue
        port = m["ports"].strip().split(",")[0].strip()
        out.append({"vlan": int(m["vlan"]) if m["vlan"].isdigit() else None, "mac": m["mac"].lower(),
                    "type": m["type"].upper(), "port": short_iface(port) if port else None})
    return ParseResult(True, out)


# ------------------------------------------------------------------ CDP / EtherChannel
def parse_cdp_neighbors(text) -> ParseResult:
    """show cdp neighbors -> [{device, local (short), holdtime, capability, platform, port (short)}]
    (a long Device ID wraps onto its own line; IOS prints interfaces as 'Gig 0/1')."""
    if (g := _guard(text)) is not None:
        return g
    out = []
    started = False
    pending = None
    rx = re.compile(r"^(?P<dev>\S+)?\s+(?P<lt>[A-Za-z]+)\s?(?P<ln>\d+(?:/\d+)*)\s+(?P<hold>\d+)\s+"
                    r"(?P<cap>(?:[RTBSHIrPDCM]\s)*[RTBSHIrPDCM]?)\s*(?P<plat>\S+(?:\s\S+)?)?\s+"
                    r"(?P<pt>[A-Za-z]+)\s?(?P<pn>\d+(?:/\d+)*)\s*$")
    for ln in lines(text):
        if ln.startswith("Device ID"):
            started = True
            continue
        if not started or not ln.strip() or ln.startswith("Total cdp"):
            continue
        m = rx.match(ln.rstrip())
        if m and (m["dev"] or pending):
            dev = m["dev"] or pending
            out.append({"device": dev.split(".")[0], "local": short_iface(m["lt"] + m["ln"]),
                        "holdtime": int(m["hold"]), "capability": m["cap"].split(),
                        "platform": (m["plat"] or "").strip() or None, "port": short_iface(m["pt"] + m["pn"])})
            pending = None
        elif len(ln.split()) == 1:
            pending = ln.strip()                        # wrapped Device ID
    return ParseResult(True, out)


# EtherChannel flags ('show etherchannel summary' legend) -> words
EC_PO_FLAGS = {"S": "Layer2", "R": "Layer3", "U": "in use", "D": "down", "N": "not in use",
               "M": "minimum links not met", "f": "failed to allocate aggregator", "A": "formed by Auto LAG"}
EC_PORT_FLAGS = {"P": "bundled", "I": "stand-alone", "s": "suspended", "H": "hot-standby", "D": "down",
                 "w": "waiting to be aggregated", "u": "unsuitable for bundling", "m": "not aggregated (min links)",
                 "d": "default port", "U": "in use", "S": "Layer2", "R": "Layer3", "N": "not in use",
                 "f": "failed to allocate aggregator"}
EC_PROTOCOLS = {"LACP": "lacp", "PAgP": "pagp", "-": "on", "": "on"}


def parse_etherchannel_summary(text) -> ParseResult:
    """show etherchannel summary -> {groups: [{group, port_channel, flags, protocol, proto, up, layer,
    ports:[{port, flags}]}], in_use, aggregators}.  ``protocol`` is the device text ('LACP' / 'PAgP' / '-');
    ``proto`` is 'lacp' | 'pagp' | 'on' (a '-' protocol column = static 'mode on')."""
    if (g := _guard(text)) is not None:
        return g
    groups = []
    in_use = aggs = None
    table = False
    port_rx = re.compile(r"([A-Za-z][\w/.:\-]*\d)\((\w+)\)")
    for ln in lines(text):
        s = ln.strip()
        m = re.match(r"Number of channel-groups in use:\s*(\d+)", s)
        if m:
            in_use = int(m[1])
            continue
        m = re.match(r"Number of aggregators:\s*(\d+)", s)
        if m:
            aggs = int(m[1])
            continue
        if s.startswith("Group") and "Port-channel" in s:
            table = True
            continue
        if not table or s.startswith("-") or not s:
            continue
        m = re.match(r"^(\d+)\s+(Po\d+)\((\w+)\)\s*(.*)$", s)
        if m:
            rest = m[4]
            proto_txt = ""
            pm = re.match(r"^(LACP|PAgP|-)(?:\s+|$)(.*)$", rest)
            if pm:
                proto_txt, rest = pm[1], pm[2]
            ports = [{"port": short_iface(x), "flags": f} for x, f in port_rx.findall(rest)]
            fl = m[3]
            groups.append({"group": int(m[1]), "port_channel": m[2], "flags": fl, "protocol": proto_txt or "-",
                           "proto": EC_PROTOCOLS.get(proto_txt, "on"), "up": "U" in fl and "D" not in fl,
                           "layer": "L3" if "R" in fl else "L2", "ports": ports})
        elif groups and ln.startswith(" "):
            groups[-1]["ports"] += [{"port": short_iface(x), "flags": f} for x, f in port_rx.findall(s)]
    return ParseResult(True, {"groups": groups, "in_use": in_use, "aggregators": aggs})


_LACP_INT_RE = re.compile(r"^(?P<port>[A-Za-z][\w/.:\-]*\d)\s+(?P<flags>[SFAP]+)\s+(?P<state>\S+)\s+(?P<prio>\d+)\s+"
                          r"(?P<akey>0x[0-9A-Fa-f]+)\s+(?P<okey>0x[0-9A-Fa-f]+)\s+(?P<pnum>0x[0-9A-Fa-f]+)\s+"
                          r"(?P<pstate>0x[0-9A-Fa-f]+)\s*$")
LACP_STATES = {"bndl": "bundled", "indep": "stand-alone", "susp": "suspended", "hot-sby": "hot-standby",
               "down": "down", "waiting": "waiting"}


def parse_lacp_internal(text) -> ParseResult:
    """show lacp internal -> {group: [{port, flags, mode ('active'|'passive'), fast, state, prio, admin_key,
    oper_key, port_number (int), port_state (int)}]}"""
    if (g := _guard(text)) is not None:
        return g
    out: dict[int, list] = {}
    cur = None
    for ln in lines(text):
        s = ln.strip()
        m = re.match(r"^Channel group (\d+)\s*$", s)
        if m:
            cur = out.setdefault(int(m[1]), [])
            continue
        m = _LACP_INT_RE.match(s)
        if m and cur is not None:
            fl = m["flags"]
            cur.append({"port": short_iface(m["port"]), "flags": fl, "mode": "active" if "A" in fl else "passive",
                        "fast": "F" in fl, "state": m["state"], "prio": int(m["prio"]),
                        "admin_key": int(m["akey"], 16), "oper_key": int(m["okey"], 16),
                        "port_number": int(m["pnum"], 16), "port_state": int(m["pstate"], 16)})
    return ParseResult(True, out)


_LACP_NBR_RE = re.compile(r"^(?P<port>[A-Za-z][\w/.:\-]*\d)\s+(?P<flags>[SFAP]+)\s+(?P<prio>\d+)\s+"
                          r"(?P<dev>[0-9a-fA-F]{4}\.[0-9a-fA-F]{4}\.[0-9a-fA-F]{4})\s+(?P<age>\d+s)\s+"
                          r"(?P<akey>0x[0-9A-Fa-f]+)\s+(?P<okey>0x[0-9A-Fa-f]+)\s+(?P<pnum>0x[0-9A-Fa-f]+)\s+"
                          r"(?P<pstate>0x[0-9A-Fa-f]+)\s*$")


def parse_lacp_neighbor(text) -> ParseResult:
    """show lacp neighbor -> {group: [{port, flags, mode (partner 'active'|'passive'), prio (partner port
    priority), dev_id (partner LACP system MAC), age_s, admin_key, oper_key, port_number, port_state}]}
    (a port without a partner is simply absent)."""
    if (g := _guard(text)) is not None:
        return g
    out: dict[int, list] = {}
    cur = None
    for ln in lines(text):
        s = ln.strip()
        m = re.match(r"^Channel group (\d+) neighbors\s*$", s)
        if m:
            cur = out.setdefault(int(m[1]), [])
            continue
        m = _LACP_NBR_RE.match(s)
        if m and cur is not None:
            fl = m["flags"]
            dev = m["dev"].lower()
            cur.append({"port": short_iface(m["port"]), "flags": fl, "mode": "active" if "A" in fl else "passive",
                        "prio": int(m["prio"]), "dev_id": None if dev == "0000.0000.0000" else dev,
                        "age_s": int(m["age"][:-1]),
                        "admin_key": int(m["akey"], 16), "oper_key": int(m["okey"], 16),
                        "port_number": int(m["pnum"], 16), "port_state": int(m["pstate"], 16)})
    return ParseResult(True, out)


def parse_lacp_sys_id(text) -> ParseResult:
    """show lacp sys-id -> {priority, mac}  ('32768, 5254.0009.8000'); {} when LACP never ran."""
    if (g := _guard(text)) is not None:
        return g
    m = re.search(r"(\d+)\s*,\s*([0-9a-fA-F]{4}\.[0-9a-fA-F]{4}\.[0-9a-fA-F]{4})", text or "")
    return ParseResult(True, {"priority": int(m[1]), "mac": m[2].lower()} if m else {})


def parse_pagp_neighbor(text) -> ParseResult:
    """show pagp neighbor -> {group: [{port, name (partner hostname), dev_id (MAC), partner_port (short),
    age_s, flags, cap}]}"""
    if (g := _guard(text)) is not None:
        return g
    out: dict[int, list] = {}
    cur = None
    rx = re.compile(r"^(?P<port>[A-Za-z][\w/.:\-]*\d)\s+(?P<name>\S+)\s+"
                    r"(?P<dev>[0-9a-fA-F]{4}\.[0-9a-fA-F]{4}\.[0-9a-fA-F]{4})\s+(?P<pport>[A-Za-z][\w/.:\-]*\d)\s+"
                    r"(?P<age>\d+)s\s+(?P<flags>[A-Za-z]+)\s+(?P<cap>[0-9A-Fa-f]+)\s*$")
    for ln in lines(text):
        s = ln.strip()
        m = re.match(r"^Channel group (\d+) neighbors\s*$", s)
        if m:
            cur = out.setdefault(int(m[1]), [])
            continue
        m = rx.match(s)
        if m and cur is not None:
            cur.append({"port": short_iface(m["port"]), "name": m["name"].split(".")[0], "dev_id": m["dev"].lower(),
                        "partner_port": short_iface(m["pport"]), "age_s": int(m["age"]), "flags": m["flags"],
                        "cap": m["cap"]})
    return ParseResult(True, out)


def parse_pagp_internal(text) -> ParseResult:
    """show pagp internal -> {group: [{port, flags, mode ('auto'|'desirable'), state, timers, hello_s, count,
    prio, method, ifindex}]}.  Flag 'A' = auto mode (no 'A' = desirable)."""
    if (g := _guard(text)) is not None:
        return g
    out: dict[int, list] = {}
    cur = None
    # the Flags column is empty for a desirable port that has no partner (seen live: 'Gi0/1  <tab>U4/S4  H ...')
    rx = re.compile(r"^(?P<port>[A-Za-z][\w/.:\-]*\d)\s+(?:(?P<flags>[A-Za-z]+)\s+)?(?P<state>[A-Za-z]*\d\S*)\s+"
                    r"(?:(?P<timers>[HQSI]+)\s+)?(?P<hello>\d+)s\s+(?P<count>\d+)\s+(?P<prio>\d+)\s+"
                    r"(?P<method>\S+)\s+(?P<ifindex>\d+)\s*$")
    for ln in lines(text):
        s = ln.strip()
        m = re.match(r"^Channel group (\d+)\s*$", s)
        if m:
            cur = out.setdefault(int(m[1]), [])
            continue
        m = rx.match(s)
        if m and cur is not None:
            fl = m["flags"] or ""
            cur.append({"port": short_iface(m["port"]), "flags": fl, "mode": "auto" if "A" in fl else "desirable",
                        "state": m["state"], "timers": m["timers"] or "", "hello_s": int(m["hello"]),
                        "count": int(m["count"]), "prio": int(m["prio"]), "method": m["method"],
                        "ifindex": int(m["ifindex"])})
    return ParseResult(True, out)


def parse_etherchannel_load_balance(text) -> ParseResult:
    """show etherchannel load-balance -> {method: 'src-dst-ip', ipv4: 'Source XOR Destination IP address', ...}"""
    if (g := _guard(text)) is not None:
        return g
    out: dict = {}
    t = lines(text)
    for i, ln in enumerate(t):
        s = ln.strip()
        if s.startswith("EtherChannel Load-Balancing Configuration"):
            nxt = next((x.strip() for x in t[i + 1:] if x.strip()), "")
            if nxt and ":" not in nxt:
                out["method"] = nxt.split()[0]
        m = re.match(r"^(Non-IP|IPv4|IPv6):\s*(.+)$", s)
        if m:
            out[m[1].lower().replace("-", "_")] = m[2].strip()
    return ParseResult(True, out)


def ec_syslog_reason(mnemonic: str, text: str) -> tuple[str, str] | None:
    """%EC-5-* syslog -> (member port short, plain reason) when IOS refused / suspended a member, e.g.
    'L3DONTBNDL2: Gi0/1 suspended: LACP currently not enabled on the remote port.' ->
    ('Gi0/1', 'LACP currently not enabled on the remote port');
    'CANNOT_BUNDLE2: Gi0/2 is not compatible with Gi0/1 and will be suspended (vlan mask is different)' ->
    ('Gi0/2', 'not compatible with Gi0/1 (vlan mask is different)').  Bundle / unbundle notices -> None."""
    t = (text or "").strip().rstrip(".")
    m = re.match(r"^(?:Interface\s+)?([A-Za-z][\w/.:\-]*\d)\s+suspended:\s*(.+)$", t)
    if m:
        return short_iface(m[1]), m[2].strip()
    m = re.match(r"^(?:Interface\s+)?([A-Za-z][\w/.:\-]*\d)\s+is not compatible with\s+(\S+)\s+and will be suspended"
                 r"\s*\((.+)\)", t)
    if m:
        return short_iface(m[1]), f"not compatible with {short_iface(m[2])} ({m[3]})"
    if mnemonic.startswith(("CANNOT_BUNDLE", "L3DONTBNDL", "L2MIXED", "PORT_SUSPENDED", "SUSPEND")):
        m = re.match(r"^(?:Interface\s+)?([A-Za-z][\w/.:\-]*\d)[\s:,]+(.+)$", t)
        if m:
            return short_iface(m[1]), m[2].strip()
    return None


EC_MODES_DETAIL = {"Active": "active", "Passive": "passive", "Desirable": "desirable", "Automatic": "auto",
                   "Auto": "auto", "On": "on"}


def parse_etherchannel_detail(text) -> ParseResult:
    """show etherchannel detail -> {group: {protocol, ports: {port: {mode, port_state, port_channel}},
    port_channels: {Po: {state, protocol, age}}}}  (heavy output: fixtures / on demand, not polled)."""
    if (g := _guard(text)) is not None:
        return g
    out: dict[int, dict] = {}
    cur = None
    port = None
    po = None
    for ln in lines(text):
        s = ln.strip()
        m = re.match(r"^Group:\s*(\d+)", s)
        if m:
            cur = out.setdefault(int(m[1]), {"protocol": None, "ports": {}, "port_channels": {}})
            port = po = None
            continue
        if cur is None:
            continue
        m = re.match(r"^Protocol:\s*(\S+)", s)
        if m and port is None and po is None:
            cur["protocol"] = m[1]
            continue
        m = re.match(r"^Port:\s*(\S+)", s)
        if m:
            port = short_iface(m[1])
            po = None
            cur["ports"][port] = {"mode": None, "port_state": None, "port_channel": None}
            continue
        m = re.match(r"^Port-channel:\s*(Po\d+)", s)
        if m:
            po, port = m[1], None
            cur["port_channels"][po] = {"state": None, "protocol": None, "age": None}
            continue
        if port is not None:
            m = re.match(r"^Port state\s*=\s*(.+)$", s)
            if m:
                cur["ports"][port]["port_state"] = m[1].strip()
            m = re.search(r"Mode\s*=\s*(\w+)", s)
            if m:
                cur["ports"][port]["mode"] = EC_MODES_DETAIL.get(m[1], m[1].lower())
            m = re.match(r"^Port-channel\s*=\s*(Po\d+)", s)
            if m:
                cur["ports"][port]["port_channel"] = m[1]
        if po is not None:
            m = re.match(r"^Port state\s*=\s*(.+)$", s)
            if m:
                cur["port_channels"][po]["state"] = m[1].strip()
            m = re.match(r"^Protocol\s*=\s*(\S+)", s)
            if m:
                cur["port_channels"][po]["protocol"] = m[1]
            m = re.match(r"^Age of the Port-channel\s*=\s*(\S+)", s)
            if m:
                cur["port_channels"][po]["age"] = m[1]
    return ParseResult(True, out)


# ------------------------------------------------------------------ topology changes
def _ago_seconds(t: str) -> int | None:
    """'00:04:35' / '1d02h' / '2w3d' -> seconds."""
    m = re.fullmatch(r"(\d+):(\d+):(\d+)", t)
    if m:
        return int(m[1]) * 3600 + int(m[2]) * 60 + int(m[3])
    total = 0
    found = False
    for n, u in re.findall(r"(\d+)([wdhms])", t):
        total += int(n) * {"w": 604800, "d": 86400, "h": 3600, "m": 60, "s": 1}[u]
        found = True
    return total if found else None


def parse_stp_topology_changes(text) -> ParseResult:
    """show spanning-tree detail | include ^ VLAN|occurr|from ->
    {vlan: {changes, last_ago (text), last_ago_s, from_iface (short)|None}}"""
    if (g := _guard(text)) is not None:
        return g
    out: dict[int, dict] = {}
    cur = None
    for ln in lines(text):
        s = ln.strip()
        m = re.match(r"^(?:VLAN|MST)(\d+) is executing", s)
        if m:
            cur = out.setdefault(int(m[1]), {"changes": 0, "last_ago": None, "last_ago_s": None, "from_iface": None})
            continue
        if cur is None:
            continue
        m = re.match(r"Number of topology changes (\d+) last change occurred (\S+) ago", s)
        if m:
            cur["changes"], cur["last_ago"], cur["last_ago_s"] = int(m[1]), m[2], _ago_seconds(m[2])
            continue
        m = re.match(r"from (\S+)$", s)
        if m and cur["from_iface"] is None:
            cur["from_iface"] = short_iface(m[1])
    return ParseResult(True, out)


def parse_routing_enabled(text) -> ParseResult:
    """show ip route connected -> {routing: bool, connected: [prefix]}.  A switch with 'no ip routing'
    answers 'Default gateway is ...' (host mode) instead of a routing table."""
    if (g := _guard(text)) is not None:
        return g
    t = text or ""
    if re.search(r"^\s*Default gateway is", t, re.M) and "Gateway of last resort" not in t:
        return ParseResult(True, {"routing": False, "connected": []})
    conn = re.findall(r"^C\s+(\d+\.\d+\.\d+\.\d+/\d+)", t, re.M)
    return ParseResult(True, {"routing": "Codes:" in t or "Gateway of last resort" in t or bool(conn),
                              "connected": conn})
