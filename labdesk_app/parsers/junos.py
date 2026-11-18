"""Juniper Junos parsers ("| display json" first, text fallbacks).  Vendored from
lab-viz/parsers.py and extended with BGP / VRRP / OSPF-interface parsers (D6).

Junos renders "| display json" as nested lists of {"data": ...} leaves; ``_jl`` / ``_jv``
walk that shape.  All functions: ``text -> ParseResult``; pure, never raise.
"""
from __future__ import annotations

import json
import re

from .common import ANSI_RE, IP, ParseResult, error_marker, lines, mask_to_len

__all__ = ["parse_interfaces_terse_json", "parse_interfaces_terse", "parse_ospf_neighbors_json",
           "parse_ospf_neighbors", "parse_ospf_interface_json", "parse_bgp_summary_json",
           "parse_vrrp_summary_json", "parse_routes_json", "parse_routes_terse", "parse_default_route",
           "parse_ospf_overview_json", "parse_version_json"]


def _json_load(text):
    if not text:
        return None
    t = ANSI_RE.sub("", text)
    i, j = t.find("{"), t.rfind("}")
    if i < 0 or j < i:
        return None
    try:
        return json.loads(t[i:j + 1])
    except ValueError:
        return None


def _jl(node, key):
    if not isinstance(node, dict):
        return []
    v = node.get(key)
    if isinstance(v, list):
        return [x for x in v if isinstance(x, dict)]
    return [v] if isinstance(v, dict) else []


def _jv(node, key, default=None):
    for x in _jl(node, key):
        d = x.get("data")
        if isinstance(d, str):
            return d.strip()
        if d is not None and not (isinstance(d, list) and d == [None]):
            return d
        return default if d is None else True          # [null] = presence flag
    return default


def _json_guard(text, root):
    if text is None:
        return None, ParseResult(False, None, "no output (sentinel not seen)")
    err = error_marker(text)
    if err is None:
        err = next((l.strip() for l in lines(text) if l.strip().startswith("error:")), None)
    if err:
        return None, ParseResult(False, None, err)
    data = _json_load(text)
    if data is None:
        if not text.strip():
            return {}, None                       # empty output = feature not configured
        return None, ParseResult(False, None, "not JSON")
    return data, None


def _adj(state):
    s = (state or "").strip()
    return {"full": "FULL", "init": "INIT", "down": "DOWN", "2way": "2WAY", "exstart": "EXSTART",
            "exchange": "EXCHANGE", "loading": "LOADING", "attempt": "ATTEMPT"}.get(s.lower(), s.upper() or "UNKNOWN")


def _phys(name: str) -> str:
    """ge-0/0/0.0 -> ge-0/0/0 (unit 0 is the port itself in LabDesk's model)."""
    return name[:-2] if name and name.endswith(".0") else name


# ------------------------------------------------------------------ OSPF
def parse_ospf_neighbors_json(text) -> ParseResult:
    """show ospf neighbor | display json -> same row shape as ios.parse_ospf_neighbors."""
    data, g = _json_guard(text, "ospf-neighbor-information")
    if g is not None:
        return g
    out = []
    for info in _jl(data, "ospf-neighbor-information"):
        for nb in _jl(info, "ospf-neighbor"):
            st = _jv(nb, "ospf-neighbor-state", "")
            iface = _jv(nb, "interface-name") or ""
            out.append({"neighbor_id": _jv(nb, "neighbor-id"), "priority": int(_jv(nb, "neighbor-priority", 0) or 0),
                        "state": _adj(st), "adj_state": _adj(st), "role": "-",
                        "dead_time": _jv(nb, "activity-timer"), "address": _jv(nb, "neighbor-address"),
                        "iface": _phys(iface), "short": _phys(iface)})
    return ParseResult(True, out)


_OSPF_TXT = re.compile(r"^\s*(?P<addr>" + IP + r")\s+(?P<iface>\S+)\s+(?P<state>[A-Za-z0-9\-]+)\s+"
                       r"(?P<nid>" + IP + r")\s+(?P<pri>\d+)\s+(?P<dead>\d+)\s*$")


def parse_ospf_neighbors(text) -> ParseResult:
    if text is None:
        return ParseResult(False, None, "no output")
    out = []
    for line in lines(text):
        m = _OSPF_TXT.match(line)
        if m:
            out.append({"neighbor_id": m["nid"], "priority": int(m["pri"]), "state": _adj(m["state"]),
                        "adj_state": _adj(m["state"]), "role": "-", "dead_time": m["dead"],
                        "address": m["addr"], "iface": _phys(m["iface"]), "short": _phys(m["iface"])})
    return ParseResult(True, out)


_JUNOS_OSPF_STATE = {"pttopt": "P2P", "dr": "DR", "bdr": "BDR", "drother": "DROTHER", "waiting": "WAIT",
                     "down": "DOWN", "loopback": "LOOP"}


def parse_ospf_interface_json(text) -> ParseResult:
    """show ospf interface detail | display json ->
    [{iface, state (P2P|DR|BDR|DROTHER|WAIT|DOWN), net_type (POINT_TO_POINT|BROADCAST|...),
      ip, prefixlen, area, nbrs, passive, dr_id, bdr_id}]
    interface-type P2P -> POINT_TO_POINT (P2PSource.JUNOS_PTTOPT), LAN -> BROADCAST."""
    data, g = _json_guard(text, "ospf-interface-information")
    if g is not None:
        return g
    out = []
    for info in _jl(data, "ospf-interface-information"):
        for oi in _jl(info, "ospf-interface"):
            name = _jv(oi, "interface-name") or ""
            st_raw = (_jv(oi, "ospf-interface-state") or "").lower()
            itype = (_jv(oi, "interface-type") or "").upper()
            net = {"P2P": "POINT_TO_POINT", "LAN": "BROADCAST", "NBMA": "NON_BROADCAST",
                   "P2MP": "POINT_TO_MULTIPOINT"}.get(itype)
            if net is None and st_raw == "pttopt":
                net = "POINT_TO_POINT"
            mask = _jv(oi, "address-mask")
            out.append({"iface": _phys(name), "short": _phys(name),
                        "state": _JUNOS_OSPF_STATE.get(st_raw, st_raw.upper() or None), "net_type": net,
                        "ip": _jv(oi, "interface-address"), "prefixlen": mask_to_len(mask) if mask else None,
                        "area": _jv(oi, "ospf-area"), "nbrs": int(_jv(oi, "neighbor-count", 0) or 0),
                        "passive": bool(_jv(oi, "passive", False)),
                        "dr_id": _jv(oi, "dr-id"), "bdr_id": _jv(oi, "bdr-id")})
    return ParseResult(True, out)


def parse_ospf_overview_json(text) -> ParseResult:
    data, g = _json_guard(text, "ospf-overview-information")
    if g is not None:
        return g
    for info in _jl(data, "ospf-overview-information"):
        for ov in _jl(info, "ospf-overview"):
            return ParseResult(True, {"router_id": _jv(ov, "ospf-router-id"), "instance": _jv(ov, "instance-name")})
    return ParseResult(True, {})


# ------------------------------------------------------------------ BGP / VRRP
def parse_bgp_summary_json(text) -> ParseResult:
    """show bgp summary | display json -> same shape as ios.parse_bgp_summary
    ({active, rid, local_as, peers:[{ip, remote_as, state, established, pfx_rcvd, uptime}]}).
    Empty output / 'BGP is not running' -> ok=True, active=False."""
    if text is not None and "not running" in text:
        return ParseResult(True, {"active": False, "rid": None, "local_as": None, "peers": []})
    data, g = _json_guard(text, "bgp-information")
    if g is not None:
        return g
    res = {"active": False, "rid": None, "local_as": None, "peers": []}
    for info in _jl(data, "bgp-information"):
        res["active"] = True
        for p in _jl(info, "bgp-peer"):
            addr = (_jv(p, "peer-address") or "").split("+")[0]
            st = _jv(p, "peer-state") or "unknown"
            pfx = None
            for rib in _jl(p, "bgp-rib"):
                if _jv(rib, "name") in (None, "inet.0"):
                    v = _jv(rib, "received-prefix-count")
                    pfx = int(v) if str(v or "").isdigit() else pfx
            asn = _jv(p, "peer-as")
            res["peers"].append({"ip": addr, "remote_as": int(asn) if str(asn or "").isdigit() else asn,
                                 "state": "Established" if st.lower().startswith("establ") else st,
                                 "established": st.lower().startswith("establ"), "pfx_rcvd": pfx,
                                 "uptime": _jv(p, "elapsed-time")})
    return ParseResult(True, res)


def parse_vrrp_summary_json(text) -> ParseResult:
    """show vrrp summary | display json -> [{proto:'VRRP', iface, short, group, state
    (Master|Backup|Init|...), vip, local_ip}]"""
    data, g = _json_guard(text, "vrrp-information")
    if g is not None:
        return g
    out = []
    for info in _jl(data, "vrrp-information"):
        for vi in _jl(info, "vrrp-interface"):
            name = _phys(_jv(vi, "interface") or "")
            st = (_jv(vi, "vrrp-state") or "unknown").lower()
            out.append({"proto": "VRRP", "iface": name, "short": name,
                        "group": int(_jv(vi, "group", 0) or 0),
                        "state": {"master": "Master", "backup": "Backup", "init": "Init", "idle": "Init"}.get(st, st),
                        "vip": _jv(vi, "virtual-ip-address"), "local_ip": _jv(vi, "address"),
                        "priority": None, "preempt": None})
    return ParseResult(True, out)


# ------------------------------------------------------------------ interfaces
_IF_KEEP = re.compile(r"^(?:(?:ge|xe|et|fe|ae|mge)-\d+/\d+/\d+|ae\d+|lo0|fxp0|em0|me0|irb|reth\d+|st0)$")


def _status(admin, oper):
    a, o = (admin or "").lower(), (oper or "").lower()
    if a == "down":
        return "admin-down"
    if a == "up" and o == "up":
        return "up"
    if o == "down":
        return "down"
    return "unknown"


def parse_interfaces_terse_json(text) -> ParseResult:
    """show interfaces terse | display json ->
    [{iface (physical), unit, ip, prefixlen, status: up|down|admin-down, admin, oper}]
    physical ports and units with inet addresses; internal units are dropped."""
    data, g = _json_guard(text, "interface-information")
    if g is not None:
        return g
    out = []
    for info in _jl(data, "interface-information"):
        for pi in _jl(info, "physical-interface"):
            pname = _jv(pi, "name", "")
            if not _IF_KEEP.match(pname):
                continue
            padmin, poper = _jv(pi, "admin-status"), _jv(pi, "oper-status")
            row = {"iface": pname, "short": pname, "unit": None, "ip": None, "prefixlen": None,
                   "status": _status(padmin, poper), "admin": padmin, "oper": poper}
            for li in _jl(pi, "logical-interface"):
                lname = _jv(li, "name", "")
                if re.search(r"\.(1638[4-6]|3276[7-9])$", lname):
                    continue
                for af in _jl(li, "address-family"):
                    if _jv(af, "address-family-name") != "inet":
                        continue
                    for ia in _jl(af, "interface-address"):
                        loc = _jv(ia, "ifa-local")
                        if loc and re.match(IP, loc) and row["ip"] is None:
                            ip, _, plen = loc.partition("/")
                            row.update(unit=lname, ip=ip, prefixlen=int(plen) if plen else 32)
                            lst = _status(_jv(li, "admin-status"), _jv(li, "oper-status"))
                            if row["status"] == "up" and lst != "up":
                                row["status"] = lst
            out.append(row)
    return ParseResult(True, out)


_TERSE_RE = re.compile(r"^(?P<name>[a-z][\w\-/.:]*)\s+(?P<admin>up|down)\s+(?P<link>up|down)"
                       r"(?:\s+(?P<proto>inet6?|iso|mpls|multiservice|tnp|vpls|eth-switch|aenet|bridge)"
                       r"(?:\s+(?P<local>\S+))?)?")


def parse_interfaces_terse(text) -> ParseResult:
    """Text 'show interfaces terse' fallback (same row shape as the JSON parser)."""
    if text is None:
        return ParseResult(False, None, "no output")
    phys, order = {}, []
    for line in lines(text):
        m = _TERSE_RE.match(line.rstrip())
        if not m:
            continue
        name = m["name"]
        base = name.split(".")[0]
        if not _IF_KEEP.match(base) or re.search(r"\.(1638[4-6]|3276[7-9])$", name):
            continue
        st = _status(m["admin"], m["link"])
        p = phys.get(base)
        if p is None:
            p = phys[base] = {"iface": base, "short": base, "unit": None, "ip": None, "prefixlen": None,
                              "status": st, "admin": m["admin"], "oper": m["link"]}
            order.append(base)
        if "." in name and m["proto"] == "inet" and m["local"] and re.match(IP, m["local"]) and not p["ip"]:
            ip, _, plen = m["local"].partition("/")
            p.update(unit=name, ip=ip, prefixlen=int(plen) if plen else 32)
            if p["status"] == "up" and st != "up":
                p["status"] = st
    return ParseResult(True, [phys[k] for k in order])


# ------------------------------------------------------------------ routes
_PROTO = {"ospf": "ospf", "direct": "connected", "local": "local", "static": "static", "bgp": "bgp",
          "isis": "isis", "rip": "rip", "aggregate": "aggregate", "access-internal": "access"}


def parse_routes_json(text) -> ParseResult:
    """show route [...] | display json -> [{prefix, proto, distance, metric, active, table,
    next_hops:[{via, iface}]}] (the active entry per destination)."""
    data, g = _json_guard(text, "route-information")
    if g is not None:
        return g
    out = []
    for info in _jl(data, "route-information"):
        for tbl in _jl(info, "route-table"):
            tname = _jv(tbl, "table-name", "")
            for rt in _jl(tbl, "rt"):
                dest = _jv(rt, "rt-destination")
                if not dest:
                    continue
                entries = _jl(rt, "rt-entry")
                ent = next((e for e in entries if _jv(e, "active-tag") in ("*", "+")), entries[0] if entries else None)
                if ent is None:
                    continue
                proto = (_jv(ent, "protocol-name", "") or "").lower()
                hops = []
                for nh in _jl(ent, "nh"):
                    to, via = _jv(nh, "to"), _jv(nh, "via")
                    if to or via:
                        hops.append({"via": to, "iface": _phys(via) if via else None})
                if not hops and _jv(ent, "nh-type"):
                    hops.append({"via": None, "iface": _jv(ent, "nh-type")})
                pref, met = _jv(ent, "preference"), _jv(ent, "metric")
                out.append({"prefix": dest if "/" in dest else dest + "/32",
                            "proto": _PROTO.get(proto, proto),
                            "distance": int(pref) if str(pref or "").isdigit() else None,
                            "metric": int(met) if str(met or "").isdigit() else None,
                            "active": _jv(ent, "active-tag") in ("*", "+"), "table": tname,
                            "next_hops": hops})
    return ParseResult(True, out)


_RT_TERSE = re.compile(r"^[*+\- ]\s?[?VIN ]?\s+(?P<dest>" + IP + r"/\d+)\s+(?P<p>[A-Z])\s+(?P<pref>\d+)\s+"
                       r"(?:(?P<m1>\d+)\s+)?(?:(?P<m2>\d+)\s+)?(?P<nh>\S+)?")


def parse_routes_terse(text) -> ParseResult:
    if text is None:
        return ParseResult(False, None, "no output")
    codes = {"O": "ospf", "D": "connected", "L": "local", "S": "static", "B": "bgp", "I": "isis"}
    out, table = [], None
    for line in lines(text):
        m = re.match(r"^([\w.\-]+\.inet6?\.\d+|inet6?\.\d+):", line)
        if m:
            table = m.group(1)
            continue
        if table != "inet.0":
            continue
        m = _RT_TERSE.match(line)
        if not m:
            continue
        nh = (m["nh"] or "").lstrip(">")
        out.append({"prefix": m["dest"], "proto": codes.get(m["p"], m["p"]), "distance": int(m["pref"]),
                    "metric": int(m["m1"]) if m["m1"] else None, "active": line.startswith("*"), "table": table,
                    "next_hops": [{"via": nh, "iface": None}] if re.fullmatch(IP, nh) else
                                 ([{"via": None, "iface": nh}] if nh else [])})
    return ParseResult(True, out)


def parse_default_route(text) -> ParseResult:
    """show route 0.0.0.0/0 exact [| display json] -> same shape as ios.parse_ip_route_default
    ({present, proto, code ('S*'|'B*'|'O*'...), distance, next_hops, ...}).  Only inet.0 counts
    (the mgmt_junos table's fxp0 default is the management VRF, not the data plane)."""
    if text is None:
        return ParseResult(False, None, "no output")
    if _json_load(text) is not None:
        r = parse_routes_json(text)
    else:
        r = _parse_route_text_default(text)
    if not r.ok:
        return r
    rows = [x for x in r.data if x["prefix"] == "0.0.0.0/0" and x.get("table", "inet.0") == "inet.0"]
    if not rows:
        return ParseResult(True, {"present": False, "next_hops": []})
    x = rows[0]
    letter = {"static": "S", "bgp": "B", "ospf": "O", "isis": "i", "rip": "R"}.get(x["proto"], "?")
    hops = [h for h in x["next_hops"] if h.get("via") or h.get("iface")]
    blackhole = any((h.get("iface") or "").lower() in ("discard", "reject") for h in hops)
    return ParseResult(True, {"present": True, "known_via": x["proto"], "proto": x["proto"], "code": letter + "*",
                              "distance": x["distance"], "metric": x["metric"], "candidate_default": True,
                              "ebgp": None, "from_ip": None,
                              "next_hops": [] if blackhole else hops, "blackhole": blackhole})


def _parse_route_text_default(text) -> ParseResult:
    """Text 'show route 0.0.0.0/0 exact' ('0.0.0.0/0 *[Static/5] ...  >  to X via ge-0/0/0.0')."""
    out, table, cur = [], None, None
    for line in lines(text):
        m = re.match(r"^([\w.\-]*inet6?\.\d+):", line)
        if m:
            table = m.group(1)
            continue
        m = re.match(r"^(" + IP + r"/\d+)\s+[*+\-]?\[(\w[\w\-]*)/(\d+)\]", line)
        if m:
            cur = {"prefix": m[1], "proto": _PROTO.get(m[2].lower(), m[2].lower()), "distance": int(m[3]),
                   "metric": None, "active": True, "table": table or "inet.0", "next_hops": []}
            out.append(cur)
            continue
        if cur is not None:
            m = re.search(r"to (" + IP + r") via (\S+)", line)
            if m:
                cur["next_hops"].append({"via": m[1], "iface": _phys(m[2])})
            elif (m := re.search(r"\b(Discard|Reject)\b", line)):
                cur["next_hops"].append({"via": None, "iface": m[1].lower()})
    return ParseResult(True, out)


def parse_version_json(text) -> ParseResult:
    data, g = _json_guard(text, "software-information")
    if g is not None:
        return g
    for si in _jl(data, "software-information"):
        return ParseResult(True, {"hostname": _jv(si, "host-name"), "model": _jv(si, "product-model"),
                                  "version": _jv(si, "junos-version")})
    return ParseResult(True, {})
