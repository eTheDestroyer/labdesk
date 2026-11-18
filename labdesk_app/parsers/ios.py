"""Cisco IOS / IOSv CLI parsers (vendored from lab-viz/parsers.py, extended).

All functions: ``text -> ParseResult``; pure, never raise.  Shapes are documented per
function and in docs/telemetry_model.md.  NVI0/NV0 (IOS NAT virtual interfaces) are
skipped everywhere.  "Empty / header-only output" is ok=True with an empty result.
"""
from __future__ import annotations

import re

from .common import (IP, ParseResult, error_marker, expand_iface, is_nat_virtual, lines, mask_to_len,
                     short_iface)

__all__ = [
    "parse_ip_int_brief", "parse_interfaces_status", "parse_interfaces_description",
    "parse_ospf_neighbors", "parse_ospf_int_brief", "parse_ospf_interface", "parse_ip_protocols",
    "parse_standby_brief", "parse_standby_detail", "parse_vrrp_brief", "parse_vrrp_detail",
    "parse_bgp_summary", "parse_ip_bgp", "parse_ip_route", "parse_ip_route_default",
    "parse_ip_cef_default", "parse_running_config", "parse_ping", "parse_traceroute",
    "parse_show_clock", "parse_ip_interface_addrs", "IP_ADDR_CMD",
]


def _guard(text):
    """Common ok/error handling: returns a failing ParseResult or None."""
    if text is None:
        return ParseResult(False, None, "no output (sentinel not seen)")
    err = error_marker(text)
    if err:
        return ParseResult(False, None, err)
    return None


# ------------------------------------------------------------------ interfaces
_INTBRIEF_RE = re.compile(
    r"^(?P<iface>[A-Za-z][\w\-./:]*\d)\s+(?P<ip>\S+)\s+(?P<ok>YES|NO|\?)\s+(?P<method>\S+)\s+"
    r"(?P<status>administratively down|up|down|deleted|reset)\s+(?P<proto>up|down)\b")


def parse_ip_int_brief(text) -> ParseResult:
    """show ip interface brief ->
    [{iface (full name), short, ip|None, method, status: up|down|admin-down, protocol: up|down}]"""
    if (g := _guard(text)) is not None:
        return g
    out = []
    for line in lines(text):
        m = _INTBRIEF_RE.match(line.strip())
        if not m or is_nat_virtual(m["iface"]):
            continue
        raw = m["status"]
        status = "admin-down" if raw.startswith("administratively") else ("up" if raw == "up" else "down")
        ip = m["ip"] if re.fullmatch(IP, m["ip"]) else None
        out.append({"iface": expand_iface(m["iface"]), "short": short_iface(m["iface"]), "ip": ip,
                    "method": m["method"], "status": status, "protocol": m["proto"]})
    return ParseResult(True, out)


_IFSTAT_RE = re.compile(r"^(?P<iface>[A-Za-z][\w\-./:]*\d) is (?P<status>administratively down|up|down|"
                        r"deleted|reset)(?:\s*\(.*?\))?, line protocol is (?P<proto>up|down)")


# One FAST-tier read for every interface's addresses (primary + secondaries + how they were set).
# Verified live at the user-exec '>' prompt on IOSv and IOSvL2 (~1.5 s for 7 interfaces).
IP_ADDR_CMD = ("show ip interface | include line protocol|Internet address|Secondary address|unnumbered|"
               "determined by|negotiated|processing disabled")
_ADDR_RE = re.compile(r"^\s+Internet address is (?P<ip>" + IP + r")/(?P<plen>\d{1,2})")
_SEC_RE = re.compile(r"^\s+Secondary address (?P<ip>" + IP + r")/(?P<plen>\d{1,2})")
_UNNUM_RE = re.compile(r"^\s+Interface is unnumbered\.\s+Using address of (?P<src>\S+)(?:\s+\((?P<ip>" + IP
                       + r")\))?")
_HOW_RE = re.compile(r"^\s+Address determined by (?P<how>.+?)\s*$")


def parse_ip_interface_addrs(text) -> ParseResult:
    """``IP_ADDR_CMD`` (filtered 'show ip interface') ->
    [{iface, short, ip, prefixlen, secondaries: [(ip, plen)], method, unnumbered, unnumbered_ip, enabled}]

    method: 'static' (configuration file / setup / non-volatile memory / manual) | 'dhcp' |
    'unnumbered' | 'negotiated' (DHCP with no lease yet, IPCP) | 'none' (IP disabled, or an L2
    switchport, which prints only its status line)."""
    if (g := _guard(text)) is not None:
        return g
    out, cur = [], None
    for line in lines(text):
        if not line.strip():
            continue
        m = _IFSTAT_RE.match(line.strip()) if not line.startswith(" ") else None
        if m:
            if is_nat_virtual(m["iface"]):
                cur = None
                continue
            cur = {"iface": expand_iface(m["iface"]), "short": short_iface(m["iface"]), "ip": None,
                   "prefixlen": None, "secondaries": [], "method": "none", "unnumbered": None,
                   "unnumbered_ip": None, "enabled": None}
            out.append(cur)
            continue
        if cur is None:
            continue
        if (m := _ADDR_RE.match(line)):
            cur["ip"], cur["prefixlen"], cur["enabled"] = m["ip"], int(m["plen"]), True
            if cur["method"] == "none":
                cur["method"] = "static"
        elif (m := _SEC_RE.match(line)):
            cur["secondaries"].append((m["ip"], int(m["plen"])))
        elif (m := _UNNUM_RE.match(line)):
            cur["method"], cur["unnumbered"], cur["unnumbered_ip"] = "unnumbered", expand_iface(m["src"]), m["ip"]
            cur["enabled"] = True
        elif (m := _HOW_RE.match(line)):
            how = m["how"].lower()
            if "dhcp" in how:
                cur["method"] = "dhcp"
            elif cur["method"] != "unnumbered":
                cur["method"] = "static" if cur["ip"] else cur["method"]
        elif "negotiated" in line:
            cur["method"] = "dhcp" if "dhcp" in line.lower() else "negotiated"
            cur["enabled"] = True
        elif "processing disabled" in line:
            cur["enabled"] = False
    return ParseResult(True, out)


def parse_interfaces_status(text) -> ParseResult:
    """show interfaces | include (line protocol|Description) ->
    [{iface, short, status, protocol, desc}]"""
    if (g := _guard(text)) is not None:
        return g
    out, cur = [], None
    for line in lines(text):
        m = _IFSTAT_RE.match(line.strip())
        if m:
            if is_nat_virtual(m["iface"]):
                cur = None
                continue
            raw = m["status"]
            cur = {"iface": expand_iface(m["iface"]), "short": short_iface(m["iface"]),
                   "status": "admin-down" if raw.startswith("admin") else ("up" if raw == "up" else "down"),
                   "protocol": m["proto"], "desc": None}
            out.append(cur)
            continue
        d = re.match(r"^\s+Description:\s*(.*)$", line)
        if d and cur is not None:
            cur["desc"] = d.group(1).strip()
    return ParseResult(True, out)


def parse_interfaces_description(text) -> ParseResult:
    """show interfaces description -> [{iface, short, status, protocol, desc}] (column based)."""
    if (g := _guard(text)) is not None:
        return g
    out, cols = [], None
    for line in lines(text):
        if line.startswith("Interface") and "Description" in line:
            cols = (line.index("Status"), line.index("Protocol"), line.index("Description"))
            continue
        if cols is None or not line.strip():
            continue
        name = line[:cols[0]].strip()
        if not name or is_nat_virtual(name):
            continue
        status = line[cols[0]:cols[1]].strip()
        out.append({"iface": expand_iface(name), "short": short_iface(name),
                    "status": "admin-down" if status.startswith("admin") else status,
                    "protocol": line[cols[1]:cols[2]].strip(),
                    "desc": line[cols[2]:].strip() or None})
    return ParseResult(True, out)


# ------------------------------------------------------------------ OSPF
_OSPF_NBR_RE = re.compile(
    r"^\s*(?P<nid>" + IP + r")\s+(?P<pri>\d+)\s+(?P<state>[A-Z0-9\-]+(?:/\s*[A-Za-z\-]+)?)\s+"
    r"(?P<dead>\d+:\d\d:\d\d|\d+[a-z]\d+[a-z]|-)\s+(?P<addr>" + IP + r")\s+(?P<iface>\S+)\s*$")


def parse_ospf_neighbors(text) -> ParseResult:
    """show ip ospf neighbor ->
    [{neighbor_id, priority, state ('FULL/DR', 'FULL/-'), adj_state, role (DR|BDR|DROTHER|-),
      dead_time, address, iface, short}]

    'FULL/  -' (p2p) has a space inside the field; the regex treats '/\\s*-' as one token."""
    if (g := _guard(text)) is not None:
        return g
    out = []
    for line in lines(text):
        m = _OSPF_NBR_RE.match(line)
        if not m:
            continue
        state = re.sub(r"\s+", "", m["state"])
        adj, _, role = state.partition("/")
        out.append({"neighbor_id": m["nid"], "priority": int(m["pri"]), "state": state,
                    "adj_state": adj, "role": role or "-", "dead_time": m["dead"],
                    "address": m["addr"], "iface": expand_iface(m["iface"]), "short": short_iface(m["iface"])})
    return ParseResult(True, out)


_OSPF_BRIEF_RE = re.compile(
    r"^(?P<iface>[A-Za-z][\w\-./:]*\d)\s+(?P<pid>\d+)\s+(?P<area>\S+)\s+(?P<ip>" + IP + r")/(?P<len>\d+)\s+"
    r"(?P<cost>\d+)\s+(?P<state>[A-Za-z0-9\-]+)\s+(?P<full>\d+)/(?P<cfg>\d+)\s*$")
OSPF_STATE_TO_NETTYPE = {"P2P": "POINT_TO_POINT", "DR": "BROADCAST", "BDR": "BROADCAST",
                         "DROTH": "BROADCAST", "DROTHER": "BROADCAST", "WAIT": "BROADCAST",
                         "LOOP": "LOOPBACK", "P2MP": "POINT_TO_MULTIPOINT"}


def parse_ospf_int_brief(text) -> ParseResult:
    """show ip ospf interface brief ->
    [{iface, short, pid, area, ip, prefixlen, cost, state (P2P|DR|BDR|DROTH|WAIT|LOOP|DOWN),
      net_type (POINT_TO_POINT|BROADCAST|LOOPBACK|None when DOWN), nbrs_full, nbrs_cfg}]
    IOS lists interfaces in reverse order; order is not significant."""
    if (g := _guard(text)) is not None:
        return g
    out = []
    for line in lines(text):
        m = _OSPF_BRIEF_RE.match(line.strip())
        if not m:
            continue
        st = m["state"].upper()
        out.append({"iface": expand_iface(m["iface"]), "short": short_iface(m["iface"]), "pid": int(m["pid"]),
                    "area": m["area"], "ip": m["ip"], "prefixlen": int(m["len"]), "cost": int(m["cost"]),
                    "state": st, "net_type": OSPF_STATE_TO_NETTYPE.get(st),
                    "nbrs_full": int(m["full"]), "nbrs_cfg": int(m["cfg"])})
    return ParseResult(True, out)


def parse_ospf_interface(text) -> ParseResult:
    """show ip ospf interface (detail) ->
    [{iface, short, status, protocol, ip, prefixlen, area, rid, net_type, cost, state, passive,
      dr_id, bdr_id, nbr_count, adj_count}]"""
    if (g := _guard(text)) is not None:
        return g
    out, cur = [], None
    for line in lines(text):
        m = _IFSTAT_RE.match(line.strip()) if not line.startswith(" ") else None
        if m:
            raw = m["status"]
            cur = {"iface": expand_iface(m["iface"]), "short": short_iface(m["iface"]),
                   "status": "admin-down" if raw.startswith("admin") else ("up" if raw == "up" else "down"),
                   "protocol": m["proto"], "ip": None, "prefixlen": None, "area": None, "rid": None,
                   "net_type": None, "cost": None, "state": None, "passive": False,
                   "dr_id": None, "bdr_id": None, "nbr_count": None, "adj_count": None}
            out.append(cur)
            continue
        if cur is None:
            continue
        s = line.strip()
        if (mm := re.match(r"Internet Address (" + IP + r")/(\d+), Area (\S+?),", s)):
            cur.update(ip=mm[1], prefixlen=int(mm[2]), area=mm[3])
        elif (mm := re.search(r"Router ID (" + IP + r"), Network Type (\w+), Cost: (\d+)", s)):
            cur.update(rid=mm[1], net_type=mm[2], cost=int(mm[3]))
        elif (mm := re.search(r"State (\w+)", s)) and s.startswith("Transmit Delay"):
            cur["state"] = mm[1]
        elif (mm := re.match(r"Designated Router \(ID\) (" + IP + ")", s)):
            cur["dr_id"] = mm[1]
        elif (mm := re.match(r"Backup Designated router \(ID\) (" + IP + ")", s)):
            cur["bdr_id"] = mm[1]
        elif "Passive interface" in s:
            cur["passive"] = True
        elif (mm := re.match(r"Neighbor Count is (\d+), Adjacent neighbor count is (\d+)", s)):
            cur.update(nbr_count=int(mm[1]), adj_count=int(mm[2]))
    return ParseResult(True, out)


def parse_ip_protocols(text) -> ParseResult:
    """show ip protocols ->
    {ospf: [{pid, rid, passive: [iface...], networks: [...]}], bgp: {asn, neighbors:[ip]} | None}"""
    if (g := _guard(text)) is not None:
        return g
    res = {"ospf": [], "bgp": None}
    cur, section = None, None
    for line in lines(text):
        s = line.strip()
        if (m := re.match(r'Routing Protocol is "(\w+)\s*(\d*)"', s)):
            kind, num = m[1], m[2]
            section = None
            if kind == "ospf":
                cur = {"pid": int(num or 0), "rid": None, "passive": [], "networks": []}
                res["ospf"].append(cur)
            elif kind == "bgp":
                cur = {"asn": int(num or 0), "neighbors": []}
                res["bgp"] = cur
            else:
                cur = None
            continue
        if cur is None:
            continue
        if (m := re.match(r"Router ID (" + IP + ")", s)):
            cur["rid"] = m[1]
        elif s.startswith("Passive Interface"):
            section = "passive"
        elif s.startswith("Routing for Networks"):
            section = "networks"
        elif s.startswith("Neighbor(s):"):
            section = "neighbors"
        elif s.startswith(("Routing Information Sources", "Distance:", "Maximum path", "Address ")):
            section = None
        elif section == "passive" and re.match(r"^[A-Za-z][\w\-./:]*\d$", s):
            cur["passive"].append(expand_iface(s))
        elif section == "networks" and s:
            cur.setdefault("networks", []).append(s)
        elif section == "neighbors" and (m := re.match(r"^(" + IP + r")\b", s)):
            cur.setdefault("neighbors", []).append(m[1])
    return ParseResult(True, res)


# ------------------------------------------------------------------ FHRP
_HSRP_STATES = {s.lower(): s for s in ("Active", "Standby", "Speak", "Listen", "Learn", "Init", "Disabled")}
_STANDBY_RE = re.compile(
    r"^\s*(?P<iface>[A-Za-z][\w\-./:]*\d)\s+(?P<group>\d+)\s+(?P<pri>\d+)\s+(?P<p>P\s+)?"
    r"(?P<state>[A-Za-z]+)\s+(?P<active>\S+)\s+(?P<standby>\S+)\s+(?P<vip>\S+)\s*$")


def parse_standby_brief(text) -> ParseResult:
    """show standby brief ->
    [{proto:'HSRP', iface, short, group, priority, preempt, state, active, standby, vip}]
    active/standby are 'local', an IP or 'unknown'."""
    if (g := _guard(text)) is not None:
        return g
    out = []
    for line in lines(text):
        m = _STANDBY_RE.match(line)
        if not m:
            continue
        state = _HSRP_STATES.get(m["state"].lower())
        if state is None:
            continue
        out.append({"proto": "HSRP", "iface": expand_iface(m["iface"]), "short": short_iface(m["iface"]),
                    "group": int(m["group"]), "priority": int(m["pri"]), "preempt": bool(m["p"]),
                    "state": state, "active": m["active"], "standby": m["standby"], "vip": m["vip"]})
    return ParseResult(True, out)


_SB_HEAD = re.compile(r"^(?P<iface>[A-Za-z][\w\-./:]*\d)\s+-\s+Group\s+(?P<group>\d+)(?:\s+\(version\s+(?P<ver>\d)\))?")


def parse_standby_detail(text) -> ParseResult:
    """show standby -> [{proto:'HSRP', iface, short, group, version, state, priority, preempt,
    active, standby, vip, vmac, state_changes}]"""
    if (g := _guard(text)) is not None:
        return g
    out, cur = [], None
    for line in lines(text):
        m = _SB_HEAD.match(line)
        if m:
            cur = {"proto": "HSRP", "iface": expand_iface(m["iface"]), "short": short_iface(m["iface"]),
                   "group": int(m["group"]), "version": int(m["ver"]) if m["ver"] else 1,
                   "state": "unknown", "priority": None, "preempt": False, "active": "unknown",
                   "standby": "unknown", "vip": None, "vmac": None, "state_changes": None}
            out.append(cur)
            continue
        if cur is None:
            continue
        s = line.strip()
        if (mm := re.match(r"State is (\w+)", s)):
            cur["state"] = _HSRP_STATES.get(mm[1].lower(), mm[1].capitalize())
        elif (mm := re.match(r"(\d+) state changes?", s)):
            cur["state_changes"] = int(mm[1])
        elif (mm := re.match(r"Virtual IP address is (" + IP + ")", s)):
            cur["vip"] = mm[1]
        elif (mm := re.match(r"Active virtual MAC address is ([0-9a-fA-F.]+)", s)):
            cur["vmac"] = mm[1].lower()
        elif s.startswith("Preemption enabled"):
            cur["preempt"] = True
        elif (mm := re.match(r"Active router is (local|unknown|" + IP + r")", s)):
            cur["active"] = mm[1]
        elif (mm := re.match(r"Standby router is (local|unknown|" + IP + r")", s)):
            cur["standby"] = mm[1]
        elif (mm := re.match(r"Priority (\d+)", s)):
            cur["priority"] = int(mm[1])
    return ParseResult(True, out)


_VRRP_STATES = {"master": "Master", "backup": "Backup", "init": "Init"}


def parse_vrrp_brief(text) -> ParseResult:
    """show vrrp brief -> [{proto:'VRRP', iface, short, group, priority, owner, preempt, state,
    master, vip}].  Column-position parse: the Own column is usually empty."""
    if (g := _guard(text)) is not None:
        return g
    out, cols = [], None
    for line in lines(text):
        if line.startswith("Interface") and "Grp" in line and "State" in line:
            cols = {k: line.index(k) for k in ("Grp", "Pri", "Time", "Own", "Pre", "State", "Master addr",
                                                "Group addr")}
            continue
        if cols is None or not line.strip():
            continue
        m = re.match(r"^(\S+)\s+(\d+)\s+(\d+)\s+(\d+)", line)
        if not m:
            continue
        own = line[cols["Own"]:cols["Pre"]].strip()
        pre = line[cols["Pre"]:cols["State"]].strip()
        rest = line[cols["State"]:].split()
        if len(rest) < 3:
            continue
        out.append({"proto": "VRRP", "iface": expand_iface(m[1]), "short": short_iface(m[1]),
                    "group": int(m[2]), "priority": int(m[3]), "owner": own.upper() == "Y",
                    "preempt": pre.upper() == "Y", "state": _VRRP_STATES.get(rest[0].lower(), rest[0]),
                    "master": rest[1], "vip": rest[2]})
    return ParseResult(True, out)


def parse_vrrp_detail(text) -> ParseResult:
    """show vrrp -> [{proto:'VRRP', iface, short, group, state, vip, priority, preempt, master, master_local}]"""
    if (g := _guard(text)) is not None:
        return g
    out, cur = [], None
    for line in lines(text):
        m = re.match(r"^(?P<iface>[A-Za-z][\w\-./:]*\d) - Group (?P<g>\d+)", line)
        if m:
            cur = {"proto": "VRRP", "iface": expand_iface(m["iface"]), "short": short_iface(m["iface"]),
                   "group": int(m["g"]), "state": "unknown", "vip": None, "priority": None,
                   "preempt": False, "master": None, "master_local": False}
            out.append(cur)
            continue
        if cur is None:
            continue
        s = line.strip()
        if (mm := re.match(r"State is (\w+)", s)):
            cur["state"] = _VRRP_STATES.get(mm[1].lower(), mm[1])
        elif (mm := re.match(r"Virtual IP address is (" + IP + ")", s)):
            cur["vip"] = mm[1]
        elif (mm := re.match(r"Priority is (\d+)", s)):
            cur["priority"] = int(mm[1])
        elif s.startswith("Preemption enabled"):
            cur["preempt"] = True
        elif (mm := re.match(r"Master Router is (" + IP + r")(\s*\(local\))?", s)):
            cur["master"], cur["master_local"] = mm[1], bool(mm[2])
    return ParseResult(True, out)


# ------------------------------------------------------------------ BGP
_BGP_PEER_RE = re.compile(r"^(?P<ip>" + IP + r")\s+(?P<v>\d)\s+(?P<asn>[\d.]+)\s+(?P<rcv>\d+)\s+(?P<snt>\d+)\s+"
                          r"(?P<tbl>\d+)\s+(?P<inq>\d+)\s+(?P<outq>\d+)\s+(?P<updown>\S+)\s+(?P<st>.+?)\s*$")


def parse_bgp_summary(text) -> ParseResult:
    """show ip bgp summary ->
    {active: bool, rid, local_as, peers: [{ip, remote_as, state ('Established' or Idle/Active/
     Connect/OpenSent/OpenConfirm/'Idle (Admin)'), established, pfx_rcvd, uptime, msg_rcvd, msg_sent}]}
    '% BGP not active' -> ok=True, active=False."""
    if text is not None and "BGP not active" in text:
        return ParseResult(True, {"active": False, "rid": None, "local_as": None, "peers": []})
    if (g := _guard(text)) is not None:
        return g
    res = {"active": False, "rid": None, "local_as": None, "peers": []}
    for line in lines(text):
        s = line.strip()
        if (m := re.match(r"BGP router identifier (" + IP + r"), local AS number ([\d.]+)", s)):
            res.update(active=True, rid=m[1], local_as=int(m[2]) if m[2].isdigit() else m[2])
            continue
        m = _BGP_PEER_RE.match(s)
        if not m:
            continue
        st = m["st"]
        est = st.isdigit()
        res["active"] = True
        res["peers"].append({"ip": m["ip"], "remote_as": int(m["asn"]) if m["asn"].isdigit() else m["asn"],
                             "state": "Established" if est else st, "established": est,
                             "pfx_rcvd": int(st) if est else None, "uptime": m["updown"],
                             "msg_rcvd": int(m["rcv"]), "msg_sent": int(m["snt"])})
    return ParseResult(True, res)


def parse_ip_bgp(text) -> ParseResult:
    """show ip bgp -> [{network, next_hop, valid, best, internal, origin, path}]
    Handles rows with no status code (e.g. a default-originate row: '     0.0.0.0  0.0.0.0 ... 0 i')."""
    if text is not None and "BGP not active" in text:
        return ParseResult(True, [])
    if (g := _guard(text)) is not None:
        return g
    out, path_col, last_net = [], None, None
    for line in lines(text):
        if line.strip().startswith("Network") and "Next Hop" in line:
            path_col = line.find("Path")
            continue
        if path_col is None or not line.strip():
            continue
        status = line[:5]
        body = line[5:]
        m = re.match(r"^\s*(?:(?P<net>" + IP + r"(?:/\d+)?)\s+)?(?P<nh>" + IP + r")\s+(?P<tail>.*)$", body)
        if not m:
            continue
        net = m["net"] or last_net
        last_net = net
        ptoks = line[path_col:].split() if path_col > 0 else m["tail"].split()[-1:]
        origin = ptoks[-1] if ptoks and ptoks[-1] in ("i", "e", "?") else None
        path = [t for t in (ptoks[:-1] if origin else ptoks) if re.fullmatch(r"[\d.]+", t)]
        out.append({"network": net, "next_hop": m["nh"], "valid": "*" in status, "best": ">" in status,
                    "internal": "i" in status, "origin": origin, "path": path})
    return ParseResult(True, out)


# ------------------------------------------------------------------ routing
_PROTO_NAMES = {
    "L": "local", "C": "connected", "S": "static", "R": "rip", "M": "mobile", "B": "bgp",
    "D": "eigrp", "EX": "eigrp-external", "O": "ospf", "IA": "ospf-inter-area",
    "N1": "ospf-nssa-1", "N2": "ospf-nssa-2", "E1": "ospf-ext-1", "E2": "ospf-ext-2",
    "i": "isis", "o": "odr", "P": "periodic", "H": "nhrp", "l": "lisp", "a": "application",
    "U": "per-user-static",
}
_ROUTE_RE = re.compile(
    r"^(?P<code>[A-Za-z]{1,2}\*?(?:\s{0,2}(?:IA|E1|E2|N1|N2|L1|L2|ia|su|EX))?\*?[%+p]?)\s+"
    r"(?P<prefix>" + IP + r")(?P<mask>/\d{1,2})?(?:\s+(?P<rest>.*))?$")
_SUBNETTED_RE = re.compile(r"^\s+(" + IP + r")/(\d{1,2}) is (?:variably )?subnetted")
_VIA_RE = re.compile(r"\[(?P<ad>\d+)/(?P<metric>\d+)\]\s+via\s+(?P<via>" + IP + r")(?P<tail>.*)$")
_CONNECTED_RE = re.compile(r"is directly connected,\s*(?P<iface>[^\s,]+)")


def _classful_len(ip):
    first = int(ip.split(".")[0])
    return 8 if first < 128 else 16 if first < 192 else 24


def _hop_from_tail(via, tail):
    parts = [p.strip() for p in tail.split(",") if p.strip()]
    iface = None
    for p in reversed(parts):
        if re.match(r"^[A-Za-z][\w\-./:]*\d$", p):
            iface = expand_iface(p)
            break
    return {"via": via, "iface": iface}


def _add_hops(route, rest):
    if not rest:
        return
    c = _CONNECTED_RE.search(rest)
    if c:
        route["next_hops"].append({"via": None, "iface": expand_iface(c["iface"])})
        if route["distance"] is None:
            route["distance"], route["metric"] = 0, 0
        return
    v = _VIA_RE.search(rest)
    if v:
        if route["distance"] is None:
            route["distance"], route["metric"] = int(v["ad"]), int(v["metric"])
        route["next_hops"].append(_hop_from_tail(v["via"], v["tail"]))
        return
    s = re.search(r"via\s+(" + IP + r")(.*)$", rest)
    if s:
        route["next_hops"].append(_hop_from_tail(s.group(1), s.group(2)))
        return
    if re.search(r"\bNull0\b", rest):
        route["next_hops"].append({"via": None, "iface": "Null0"})


def parse_ip_route(text) -> ParseResult:
    """show ip route [connected] -> {gateway: ip|None, routes: [{prefix, code ('O*E2'), proto,
    proto_name, candidate_default, distance, metric, next_hops:[{via, iface}]}]}
    ECMP continuation lines ('   [110/2] via ...') are appended to the previous route."""
    if (g := _guard(text)) is not None:
        return g
    routes, cur, parent_len, gateway = [], None, None, None
    for line in lines(text):
        if not line.strip():
            continue
        stripped = line.strip()
        if (m := re.match(r"Gateway of last resort is (" + IP + ")", stripped)):
            gateway = m[1]
            cur = None
            continue
        if stripped.startswith(("Codes:", "Gateway of last resort", "Routing Table:")) or \
                re.match(r"^\s{4,}[A-Za-z0-9*+%]{1,3} - ", line):
            cur = None
            continue
        m = _SUBNETTED_RE.match(line)
        if m:
            parent_len = int(m.group(2)) if "variably" not in line else None
            cur = None
            continue
        m = _ROUTE_RE.match(line)
        if m and not line[0].isspace():
            code = re.sub(r"\s+", "", m["code"])
            base = re.match(r"[A-Za-z]+", code).group(0)
            sub = re.sub(r"^[A-Za-z]+\*?", "", code).replace("*", "") or None
            if m["mask"]:
                plen = int(m["mask"][1:])
            elif parent_len is not None:
                plen = parent_len
            else:
                plen = _classful_len(m["prefix"])
            cur = {"prefix": f"{m['prefix']}/{plen}", "code": code,
                   "proto": code.replace("*", "").replace("%", "").replace("+", ""),
                   "proto_name": _PROTO_NAMES.get(sub or base, _PROTO_NAMES.get(base, base)),
                   "candidate_default": "*" in code, "distance": None, "metric": None, "next_hops": []}
            routes.append(cur)
            _add_hops(cur, m["rest"] or "")
            continue
        if cur is not None and line[0].isspace():
            _add_hops(cur, stripped)
    return ParseResult(True, {"gateway": gateway, "routes": routes})


def parse_ip_route_default(text) -> ParseResult:
    """show ip route 0.0.0.0 ->
    {present, known_via ('static'|'bgp 65000'|'ospf 1'|...), proto (static|bgp|ospf|...),
     code ('S*'|'B*'|'O*E2'|'O*E1'|'O*IA'|'D*EX'...), distance, metric, candidate_default,
     ebgp: bool|None, from_ip, next_hops: [{via, iface}]}
    '% Network not in table' -> ok=True, present=False."""
    if text is not None and "Network not in table" in text:
        return ParseResult(True, {"present": False, "next_hops": []})
    if (g := _guard(text)) is not None:
        return g
    res = {"present": False, "known_via": None, "proto": None, "code": None, "distance": None,
           "metric": None, "candidate_default": False, "ebgp": None, "from_ip": None, "next_hops": []}
    ext_type = None
    in_blocks = False
    for line in lines(text):
        s = line.strip()
        if s.startswith("Routing entry for 0.0.0.0/0"):
            res["present"] = True
        elif (m := re.match(r'Known via "([^"]+)", distance (\d+), metric (\d+)(.*)$', s)):
            res.update(known_via=m[1], distance=int(m[2]), metric=int(m[3]),
                       candidate_default="candidate default" in m[4])
            res["proto"] = m[1].split()[0]
        elif (m := re.search(r"type (extern [12]|NSSA extern [12]|inter area|intra area|external|internal)", s)):
            ext_type = m[1]
        elif s.startswith("Routing Descriptor Blocks"):
            in_blocks = True
        elif in_blocks and (m := re.match(r"^\*?\s*(" + IP + r")(?:, from (" + IP + r"))?(.*)$", s)):
            via_if = re.search(r"via ([A-Za-z][\w\-./:]*\d)", m[3] or "")
            res["next_hops"].append({"via": m[1], "iface": expand_iface(via_if[1]) if via_if else None})
            if m[2] and not res["from_ip"]:
                res["from_ip"] = m[2]
        elif in_blocks and (m := re.match(r"^\*?\s*directly connected, via ([A-Za-z][\w\-./:]*\d)", s)):
            res["next_hops"].append({"via": None, "iface": expand_iface(m[1])})
    if res["present"]:
        p = res["proto"] or ""
        star = "*" if res["candidate_default"] else ""
        if p == "static":
            res["code"] = "S" + star
        elif p == "bgp":
            res["code"] = "B" + star
            res["ebgp"] = res["distance"] == 20 if res["distance"] is not None else None
            if ext_type == "internal":
                res["ebgp"] = False
            elif ext_type == "external":
                res["ebgp"] = True
        elif p == "ospf":
            sub = {"extern 2": "E2", "extern 1": "E1", "NSSA extern 2": "N2", "NSSA extern 1": "N1",
                   "inter area": "IA"}.get(ext_type or "", "")
            res["code"] = "O" + star + sub
        elif p == "eigrp":
            res["code"] = "D" + star + ("EX" if ext_type == "external" else "")
        elif p == "connected":
            res["code"] = "C"
        else:
            res["code"] = (p[:1].upper() if p else "?") + star
    return ParseResult(True, res)


def parse_ip_cef_default(text) -> ParseResult:
    """show ip cef 0.0.0.0/0 -> {present, next_hops: [{via, iface}], kind: 'nexthop'|'attached'|
    'drop'|'receive'|'no_route'|'unknown'}  (forwarding truth, recursion already resolved)."""
    if (g := _guard(text)) is not None:
        return g
    res = {"present": False, "next_hops": [], "kind": "unknown"}
    for line in lines(text):
        s = line.strip()
        if s.startswith("0.0.0.0/0"):
            res["present"] = True
            if "no route" in s:
                res["kind"] = "no_route"
        elif (m := re.match(r"nexthop (" + IP + r") ([A-Za-z][\w\-./:]*\d)", s)):
            res["next_hops"].append({"via": m[1], "iface": expand_iface(m[2])})
            res["kind"] = "nexthop"
        elif s in ("drop", "discard") or s.startswith(("drop", "discard", "Null0")) or "Null0" in s:
            res["kind"] = "drop"
            res["next_hops"].append({"via": None, "iface": "Null0"})
        elif (m := re.match(r"attached to ([A-Za-z][\w\-./:]*\d)", s)):
            res["next_hops"].append({"via": None, "iface": expand_iface(m[1])})
            res["kind"] = "attached"
        elif s.startswith("no route"):
            res["kind"] = "no_route"
        elif s.startswith("receive"):
            res["kind"] = "receive"
    if res["present"] and res["kind"] == "unknown" and not res["next_hops"]:
        res["kind"] = "no_route"
    return ParseResult(True, res)


# ------------------------------------------------------------------ running-config
def parse_running_config(text) -> ParseResult:
    """show running-config ->
    {hostname, interfaces: {full_name: {desc, ip, prefixlen, dhcp, shutdown, ospf_network,
       ospf_passive, hsrp: [grp], vrrp: [grp], nat}},
     ospf: [{pid, rid, passive_default, passive: [...], no_passive: [...], networks: [...],
             default_originate}],
     bgp: {asn, rid, neighbors: {ip: {remote_as, desc, default_originate}}} | None,
     static_default: [{via, iface}]}
    'Building configuration...' / 'Current configuration : N bytes' are ignored."""
    if (g := _guard(text)) is not None:
        return g
    res = {"hostname": None, "interfaces": {}, "ospf": [], "bgp": None, "static_default": []}
    block, cur = None, None
    for raw in lines(text):
        if not raw.strip() or raw.startswith(("Building configuration", "Current configuration")):
            continue
        if raw.strip() == "!":
            block, cur = None, None
            continue
        top = not raw.startswith(" ")
        s = raw.strip()
        if top:
            block, cur = None, None
            if (m := re.match(r"^hostname (\S+)", s)):
                res["hostname"] = m[1]
            elif (m := re.match(r"^interface (\S+)", s)):
                name = expand_iface(m[1])
                cur = res["interfaces"].setdefault(name, {
                    "desc": None, "ip": None, "prefixlen": None, "dhcp": False, "shutdown": False,
                    "secondaries": [], "unnumbered": None,
                    "ospf_network": None, "ospf_passive": None, "hsrp": [], "vrrp": [], "nat": None})
                block = "if"
            elif (m := re.match(r"^router ospf (\d+)", s)):
                cur = {"pid": int(m[1]), "rid": None, "passive_default": False, "passive": [],
                       "no_passive": [], "networks": [], "default_originate": False}
                res["ospf"].append(cur)
                block = "ospf"
            elif (m := re.match(r"^router bgp ([\d.]+)", s)):
                cur = {"asn": int(m[1]) if m[1].isdigit() else m[1], "rid": None, "neighbors": {}}
                res["bgp"] = cur
                block = "bgp"
            elif (m := re.match(r"^ip route 0\.0\.0\.0 0\.0\.0\.0 (\S+)(?: (" + IP + "))?", s)):
                a = m[1]
                if re.fullmatch(IP, a):
                    res["static_default"].append({"via": a, "iface": None})
                else:
                    res["static_default"].append({"via": m[2], "iface": expand_iface(a)})
            continue
        if block == "if" and cur is not None:
            if s.startswith("description "):
                cur["desc"] = s[len("description "):]
            elif (m := re.match(r"^ip address (" + IP + r") (" + IP + r")(?! secondary)\s*$", s)):
                cur["ip"], cur["prefixlen"] = m[1], mask_to_len(m[2])
            elif (m := re.match(r"^ip address (" + IP + r") (" + IP + r") secondary\s*$", s)):
                cur["secondaries"].append((m[1], mask_to_len(m[2])))
            elif (m := re.match(r"^ip unnumbered (\S+)", s)):
                cur["unnumbered"] = expand_iface(m[1])
            elif s == "ip address dhcp" or s.startswith("ip address dhcp "):
                cur["dhcp"] = True
            elif s == "shutdown":
                cur["shutdown"] = True
            elif (m := re.match(r"^ip ospf network (\S+)", s)):
                cur["ospf_network"] = {"point-to-point": "POINT_TO_POINT", "broadcast": "BROADCAST",
                                       "non-broadcast": "NON_BROADCAST",
                                       "point-to-multipoint": "POINT_TO_MULTIPOINT"}.get(m[1], m[1].upper())
            elif (m := re.match(r"^standby (?:version \d+)?(\d+) ip", s)):
                if int(m[1]) not in cur["hsrp"]:
                    cur["hsrp"].append(int(m[1]))
            elif (m := re.match(r"^vrrp (\d+) ip", s)):
                if int(m[1]) not in cur["vrrp"]:
                    cur["vrrp"].append(int(m[1]))
            elif (m := re.match(r"^ip nat (inside|outside)", s)):
                cur["nat"] = m[1]
        elif block == "ospf" and cur is not None:
            if (m := re.match(r"^router-id (" + IP + ")", s)):
                cur["rid"] = m[1]
            elif s == "passive-interface default":
                cur["passive_default"] = True
            elif (m := re.match(r"^no passive-interface (\S+)", s)):
                cur["no_passive"].append(expand_iface(m[1]))
            elif (m := re.match(r"^passive-interface (\S+)", s)):
                cur["passive"].append(expand_iface(m[1]))
            elif s.startswith("network "):
                cur["networks"].append(s[len("network "):])
            elif s.startswith("default-information originate"):
                cur["default_originate"] = True
        elif block == "bgp" and cur is not None:
            if (m := re.match(r"^bgp router-id (" + IP + ")", s)):
                cur["rid"] = m[1]
            elif (m := re.match(r"^neighbor (" + IP + r") (.*)$", s)):
                nb = cur["neighbors"].setdefault(m[1], {"remote_as": None, "desc": None, "default_originate": False})
                rest = m[2]
                if (mm := re.match(r"remote-as ([\d.]+)", rest)):
                    nb["remote_as"] = int(mm[1]) if mm[1].isdigit() else mm[1]
                elif rest.startswith("description "):
                    nb["desc"] = rest[len("description "):]
                elif rest.startswith("default-originate"):
                    nb["default_originate"] = True
    # derive per-interface ospf_passive from the router ospf blocks
    if res["ospf"]:
        for name, itf in res["interfaces"].items():
            itf["ospf_passive"] = any(
                (name not in o["no_passive"]) if o["passive_default"] else (name in o["passive"])
                for o in res["ospf"])
    return ParseResult(True, res)


# ------------------------------------------------------------------ tools
def parse_ping(text) -> ParseResult:
    """ping -> {sent, received, loss_pct, rtt_min_ms, rtt_avg_ms, rtt_max_ms, success}"""
    if (g := _guard(text)) is not None:
        return g
    t = text or ""
    res = {"sent": None, "received": None, "loss_pct": None, "rtt_min_ms": None, "rtt_avg_ms": None,
           "rtt_max_ms": None, "success": False}
    m = re.search(r"Success rate is (\d+) percent \((\d+)/(\d+)\)", t)
    if m:
        res.update(received=int(m[2]), sent=int(m[3]), loss_pct=100 - int(m[1]), success=int(m[2]) > 0)
    m = re.search(r"min/avg/max = (\d+)/(\d+)/(\d+)", t)
    if m:
        res.update(rtt_min_ms=int(m[1]), rtt_avg_ms=int(m[2]), rtt_max_ms=int(m[3]))
    return ParseResult(True, res)


def parse_traceroute(text) -> ParseResult:
    """traceroute -> [{hop, ip|None, rtts_ms:[...], timeouts}]"""
    if (g := _guard(text)) is not None:
        return g
    out = []
    for line in lines(text):
        m = re.match(r"^\s*(\d{1,3})\s+(.*)$", line)
        if not m or "Tracing" in line:
            continue
        rest = m[2]
        ip = re.search(IP, rest)
        out.append({"hop": int(m[1]), "ip": ip.group(0) if ip else None,
                    "rtts_ms": [float(x) for x in re.findall(r"(\d+(?:\.\d+)?)\s*msec", rest)],
                    "timeouts": rest.count("*")})
    return ParseResult(True, out)


def parse_show_clock(text) -> ParseResult:
    if (g := _guard(text)) is not None:
        return g
    t = " ".join(l.strip() for l in lines(text) if l.strip())
    return ParseResult(True, {"clock": t or None})
