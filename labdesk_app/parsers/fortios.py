"""Fortinet FortiOS parsers (vendored from lab-viz/parsers.py, extended for D6).

FortiOS routing output is zebra/Cisco-like.  'Command fail' / '-61' answers are reported as
ok=False so the collector can cache "unsupported" per command.  All functions:
``text -> ParseResult``; pure, never raise.
"""
from __future__ import annotations

import re

from .common import IP, ParseResult, error_marker, lines, mask_to_len

__all__ = ["parse_ospf_neighbors", "parse_ospf_interface", "parse_routing_table", "parse_default_route",
           "parse_interface_physical", "parse_bgp_summary", "parse_vrrp", "parse_status",
           "parse_session_stat"]


def _guard(text):
    if text is None:
        return ParseResult(False, None, "no output (sentinel not seen)")
    if re.search(r"Command fail|return code -61|Unknown action", text):
        return ParseResult(False, None, "command not supported")
    err = error_marker(text)
    if err:
        return ParseResult(False, None, err)
    return None


def _clean(text):
    return [l for l in lines(text) if not l.startswith("#")]


def _adj(s):
    return {"full": "FULL", "init": "INIT", "down": "DOWN", "2way": "2WAY", "exstart": "EXSTART",
            "exchange": "EXCHANGE", "loading": "LOADING", "attempt": "ATTEMPT"}.get(s.lower(), s.upper())


_OSPF_RE = re.compile(r"^\s*(?P<nid>" + IP + r")\s+(?P<pri>\d+)\s+(?P<state>[A-Za-z0-9\-]+/\s*[A-Za-z\-]+|[A-Za-z0-9\-]+)\s+"
                      r"(?P<dead>\d+:\d\d:\d\d|\d+(?:\.\d+)?s?)\s+(?P<addr>" + IP + r")\s+(?P<iface>\S+)")


def parse_ospf_neighbors(text) -> ParseResult:
    """get router info ospf neighbor -> same row shape as ios.parse_ospf_neighbors."""
    if (g := _guard(text)) is not None:
        return g
    out = []
    for line in _clean(text):
        m = _OSPF_RE.match(line)
        if not m:
            continue
        state = re.sub(r"\s+", "", m["state"])
        adj, _, role = state.partition("/")
        out.append({"neighbor_id": m["nid"], "priority": int(m["pri"]),
                    "state": _adj(adj) + ("/" + role.upper() if role else ""), "adj_state": _adj(adj),
                    "role": role.upper() or "-", "dead_time": m["dead"], "address": m["addr"],
                    "iface": m["iface"], "short": m["iface"]})
    return ParseResult(True, out)


def parse_ospf_interface(text) -> ParseResult:
    """get router info ospf interface -> [{iface, status, protocol, enabled, ip, prefixlen, area,
    rid, net_type (POINT_TO_POINT|BROADCAST|...), state, passive, nbrs, adj}]
    'Network Type POINTOPOINT' -> POINT_TO_POINT (P2PSource.FORTIOS_P2P)."""
    if (g := _guard(text)) is not None:
        return g
    out, cur = [], None
    for line in _clean(text):
        m = re.match(r"^(\S+) is (up|down|administratively down), line protocol is (up|down)", line)
        if m:
            cur = {"iface": m[1], "short": m[1], "status": "admin-down" if m[2].startswith("admin") else m[2],
                   "protocol": m[3], "enabled": True, "ip": None, "prefixlen": None, "area": None, "rid": None,
                   "net_type": None, "state": None, "passive": False, "nbrs": None, "adj": None}
            out.append(cur)
            continue
        if cur is None:
            continue
        s = line.strip()
        if s.startswith("OSPF not enabled"):
            cur["enabled"] = False
        elif (mm := re.match(r"Internet Address (" + IP + r")/(\d+), Area (\S+?),", s)):
            cur.update(ip=mm[1], prefixlen=int(mm[2]), area=mm[3])
        elif (mm := re.search(r"Router ID (" + IP + r"), Network Type (\w+)", s)):
            nt = mm[2].upper()
            cur.update(rid=mm[1], net_type={"POINTOPOINT": "POINT_TO_POINT", "POINT_TO_POINT": "POINT_TO_POINT",
                                            "BROADCAST": "BROADCAST", "NBMA": "NON_BROADCAST",
                                            "POINTOMULTIPOINT": "POINT_TO_MULTIPOINT"}.get(nt, nt))
        elif (mm := re.search(r"State ([\w\-]+)", s)) and s.startswith("Transmit Delay"):
            st = mm[1].upper()
            cur["state"] = {"POINT-TO-POINT": "P2P", "DROTHER": "DROTHER"}.get(st, st)
        elif "Passive interface" in s:
            cur["passive"] = True
        elif (mm := re.match(r"Neighbor Count is (\d+), Adjacent neighbor count is (\d+)", s)):
            cur.update(nbrs=int(mm[1]), adj=int(mm[2]))
    return ParseResult(True, [o for o in out if o["enabled"]])


_RT_RE = re.compile(r"^(?P<code>[A-Za-z]{1,2}\*?(?:\s+(?:IA|E1|E2|N1|N2|L1|L2|ia))?\*?)\s+(?P<prefix>" + IP + r"/\d{1,2})\s+(?P<rest>.*)$")
_VIA_RE = re.compile(r"\[(?P<ad>\d+)/(?P<metric>\d+)\]\s+via\s+(?P<via>" + IP + r"),\s*(?P<iface>[\w.\-]+)")
_CONT_RE = re.compile(r"^\s+\[(?P<ad>\d+)/(?P<metric>\d+)\]\s+via\s+(?P<via>" + IP + r"),\s*(?P<iface>[\w.\-]+)")
_CODES = {"K": "kernel", "C": "connected", "S": "static", "R": "rip", "B": "bgp", "O": "ospf", "i": "isis",
          "V": "bgp-vpnv4"}


def parse_routing_table(text) -> ParseResult:
    """get router info routing-table all -> {gateway: None, routes: [{prefix, code, proto,
    proto_name, candidate_default, distance, metric, next_hops:[{via, iface}]}]}"""
    if (g := _guard(text)) is not None:
        return g
    out, cur = [], None
    for line in _clean(text):
        st = line.strip()
        if not st or st.startswith(("Codes:", "Routing table for")) or re.match(r"^\s{4,}\S+ - ", line):
            cur = None
            continue
        m = _RT_RE.match(line)
        if m and not line[0].isspace():
            code = re.sub(r"\s+", "", m["code"])
            base = re.match(r"[A-Za-z]+", code).group(0)
            cur = {"prefix": m["prefix"], "code": code, "proto": code.replace("*", ""),
                   "proto_name": _CODES.get(base[0], base), "candidate_default": "*" in code,
                   "distance": None, "metric": None, "next_hops": []}
            out.append(cur)
            v = _VIA_RE.search(m["rest"])
            if v:
                cur.update(distance=int(v["ad"]), metric=int(v["metric"]))
                cur["next_hops"].append({"via": v["via"], "iface": v["iface"]})
            else:
                c = re.search(r"is directly connected,\s*([\w.\-]+)", m["rest"])
                if c:
                    cur.update(distance=0, metric=0)
                    cur["next_hops"].append({"via": None, "iface": c.group(1)})
                elif "blackhole" in m["rest"] or "Null" in m["rest"]:
                    cur["next_hops"].append({"via": None, "iface": "Null0"})
            continue
        if cur is not None:
            v = _CONT_RE.match(line)
            if v:
                cur["next_hops"].append({"via": v["via"], "iface": v["iface"]})
    return ParseResult(True, {"gateway": None, "routes": out})


def parse_default_route(text) -> ParseResult:
    """get router info routing-table details 0.0.0.0/0 (or the 'all' table) ->
    same shape as ios.parse_ip_route_default."""
    if (g := _guard(text)) is not None:
        return g
    t = text or ""
    if "Routing entry for 0.0.0.0/0" not in t:
        tbl = parse_routing_table(t)
        rows = [r for r in (tbl.data or {}).get("routes", []) if r["prefix"] == "0.0.0.0/0"] if tbl.ok else []
        if not rows:
            return ParseResult(True, {"present": False, "next_hops": []})
        r = rows[0]
        return ParseResult(True, {"present": True, "known_via": r["proto_name"], "proto": r["proto_name"],
                                  "code": r["code"], "distance": r["distance"], "metric": r["metric"],
                                  "candidate_default": r["candidate_default"], "ebgp": None, "from_ip": None,
                                  "next_hops": r["next_hops"],
                                  "blackhole": any(h["iface"] == "Null0" for h in r["next_hops"])})
    res = {"present": True, "known_via": None, "proto": None, "code": None, "distance": None, "metric": None,
           "candidate_default": True, "ebgp": None, "from_ip": None, "next_hops": [], "blackhole": False}
    for line in _clean(t):
        s = line.strip()
        if (m := re.match(r'Known via "([^"]+)", distance (\d+), metric (\d+)', s)):
            res.update(known_via=m[1], proto=m[1].split()[0], distance=int(m[2]), metric=int(m[3]))
        elif (m := re.match(r"^\*?\s*(?:vrf \d+ )?(" + IP + r")(?:, via (\S+))?", s)):
            res["next_hops"].append({"via": m[1], "iface": m[2]})
        elif (m := re.match(r"^\*?\s*directly connected, (\S+)", s)):
            res["next_hops"].append({"via": None, "iface": m[1]})
        elif "blackhole" in s.lower():
            res["blackhole"] = True
    p = res["proto"] or ""
    res["code"] = {"static": "S*", "bgp": "B*", "ospf": "O*", "connected": "C"}.get(p, (p[:1].upper() or "?") + "*")
    return ParseResult(True, res)


def parse_interface_physical(text) -> ParseResult:
    """get system interface physical -> [{iface, ip, prefixlen, status: up|down, mode, speed}]"""
    if (g := _guard(text)) is not None:
        return g
    out, cur = [], None
    for line in _clean(text):
        m = re.match(r"^\s*==\s*\[([\w.\-]+)\]\s*$", line)
        if m and m.group(1) != "onboard":
            cur = {"iface": m.group(1), "short": m.group(1), "ip": None, "prefixlen": None, "status": "unknown",
                   "mode": None, "speed": None}
            out.append(cur)
            continue
        if cur is None:
            continue
        s = line.strip()
        if (mm := re.match(r"ip:\s*(" + IP + r")\s+(" + IP + r")", s)):
            if mm.group(1) != "0.0.0.0":
                cur.update(ip=mm.group(1), prefixlen=mask_to_len(mm.group(2)))
        elif (mm := re.match(r"status:\s*(\w+)", s)):
            cur["status"] = {"up": "up", "down": "down"}.get(mm.group(1).lower(), mm.group(1).lower())
        elif (mm := re.match(r"mode:\s*(\S+)", s)):
            cur["mode"] = mm.group(1)
        elif (mm := re.match(r"speed:\s*(.+)$", s)):
            cur["speed"] = mm.group(1).strip()
    return ParseResult(True, out)


_BGP_PEER_RE = re.compile(r"^(?P<ip>" + IP + r")\s+(?P<v>\d)\s+(?P<asn>[\d.]+)\s+(?P<rcv>\d+)\s+(?P<snt>\d+)\s+"
                          r"(?P<tbl>\d+)\s+(?P<inq>\d+)\s+(?P<outq>\d+)\s+(?P<updown>\S+)\s+(?P<st>.+?)\s*$")


def parse_bgp_summary(text) -> ParseResult:
    """get router info bgp summary -> same shape as ios.parse_bgp_summary."""
    if (g := _guard(text)) is not None:
        return g
    res = {"active": False, "rid": None, "local_as": None, "peers": []}
    for line in _clean(text):
        s = line.strip()
        if (m := re.search(r"BGP router identifier (" + IP + r"), local AS number ([\d.]+)", s)):
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
                             "pfx_rcvd": int(st) if est else None, "uptime": m["updown"]})
    return ParseResult(True, res)


def parse_vrrp(text) -> ParseResult:
    """get router info vrrp -> [{proto:'VRRP', iface, short, group, state (Master|Backup|Init), vip,
    priority, preempt, local_ip}]"""
    if (g := _guard(text)) is not None:
        return g
    out, iface, local, cur = [], None, None, None
    for line in _clean(text):
        s = line.strip()
        if (m := re.match(r"Interface: (\S+), primary IP address: (" + IP + ")", s)):
            iface, local = m[1].rstrip(","), m[2]
        elif (m := re.match(r"VRID: (\d+)", s)) and iface:
            cur = {"proto": "VRRP", "iface": iface, "short": iface, "group": int(m[1]), "state": "unknown",
                   "vip": None, "priority": None, "preempt": None, "local_ip": local}
            out.append(cur)
        elif cur is not None and (m := re.match(r"vrip: (" + IP + r"), priority: (\d+), state: (\w+)", s)):
            cur.update(vip=m[1], priority=int(m[2]),
                       state={"MASTER": "Master", "BACKUP": "Backup", "INIT": "Init"}.get(m[3].upper(), m[3]))
        elif cur is not None and (m := re.search(r"preempt: (\d)", s)):
            cur["preempt"] = m[1] == "1"
    return ParseResult(True, out)


def parse_status(text) -> ParseResult:
    """get system status -> {version, model, fortios, hostname, operation_mode, license, limited}
    ``limited`` is True only when the output itself says the licence is invalid/expired/eval."""
    if (g := _guard(text)) is not None:
        return g
    res = {"limited": False}
    for line in _clean(text):
        m = re.match(r"^\s*([A-Za-z][\w \-]*?):\s*(.*?)\s*$", line)
        if not m:
            continue
        k, v = m.group(1).strip().lower(), m.group(2)
        if k == "version":
            res["version"] = v
            mm = re.match(r"(\S+)\s+(v\S+?),", v)
            if mm:
                res["model"], res["fortios"] = mm.group(1), mm.group(2)
        elif k == "hostname":
            res["hostname"] = v
        elif k == "operation mode":
            res["operation_mode"] = v
        elif k == "license status":
            res["license"] = v
            res["limited"] = v.lower() in ("invalid", "expired", "evaluation", "eval", "unlicensed")
    return ParseResult(True, res)


def parse_session_stat(text) -> ParseResult:
    if (g := _guard(text)) is not None:
        return g
    res = {}
    for k in ("session_count", "setup_rate", "exp_count", "clash"):
        m = re.search(r"\b" + k + r"=(\d+)", text or "")
        if m:
            res[k] = int(m.group(1))
    return ParseResult(True, res)
