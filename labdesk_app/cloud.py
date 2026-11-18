"""The simulated "Internet cloud": a host network namespace that always answers 8.8.8.8 / 1.1.1.1.

Host side (installed once by ``host/labdesk-cloud/install.sh``, see README "Internet cloud"):

* netns ``labcloud``: loopback 8.8.8.8/32, 1.1.1.1/32, 9.9.9.9/32, 208.67.222.222/32; ``cloud0``
  100.64.0.1/24 on a veth pair whose host end is a port of the libvirt bridge virbr0.  Forwarding off.
* return routes inside the namespace: 10/8, 172.16/12, 192.168/16 via EDGE_GW (the first router linked),
  plus per-prefix ``ROUTE <prefix> <gw>`` lines.  LabDesk writes them to the USER-OWNED file
  ``/var/lib/labdesk-cloud/routes.conf``; the systemd path unit ``labdesk-cloud-routes.path`` re-applies
  it as root within a second (strictly validated, it can only change routes inside the namespace), so
  no password is ever needed at runtime.
* ``labdesk-cloud-check.timer`` self-heals the namespace every 10 s and writes the world-readable
  ``/run/labdesk-cloud/status`` (key=value) that :func:`check_health` reads.

Lab side: a CML ``external_connector`` in "System Bridge" mode (bridged onto virbr0, does not count
against the CML-Free node limit), tagged ``labdesk-cloud``.  A router linked to it gets
``ip address 100.64.0.N 255.255.255.0`` and ``ip route 0.0.0.0 0.0.0.0 100.64.0.1``.

Everything here except :func:`check_health`, :func:`write_routes_file` and the file readers is pure
(unit-tested in tests/test_cloud.py).
"""
from __future__ import annotations

import ipaddress
import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import plat

CLOUD_TAG = "labdesk-cloud"            # CML node tag that marks the System-Bridge connector as the cloud
CLOUD_LABEL = "INTERNET"
LAB_ENABLE_SECRET = "cisco"             # every lab router/switch has 'enable secret cisco' (catalog.IOS_BASE_CONFIG)

CONF_PATH = Path(os.environ.get("LABDESK_CLOUD_CONF", "/etc/labdesk-cloud.conf"))
ROUTES_PATH = Path(os.environ.get("LABDESK_CLOUD_ROUTES", "/var/lib/labdesk-cloud/routes.conf"))
STATUS_PATH = Path(os.environ.get("LABDESK_CLOUD_STATUS", "/run/labdesk-cloud/status"))
UP_SCRIPT = Path("/usr/local/libexec/labdesk-cloud/up")
FIX_HINT = "sudo systemctl start labdesk-cloud"
INSTALL_HINT = f"sudo {Path(__file__).resolve().parent.parent}/host/labdesk-cloud/install.sh"
STATUS_MAX_AGE_S = 45.0                 # the check timer writes every 10 s
HEALTH_INTERVAL_MS = 10_000

DEFAULT_RETURN = ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
PRIVATE = tuple(ipaddress.ip_network(p) for p in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10"))


# ============================================================================ host configuration
@dataclass(frozen=True)
class CloudConf:
    ns: str = "labcloud"
    bridge: str = "virbr0"
    cloud_ip: str = "100.64.0.1"
    prefixlen: int = 24
    answers: tuple = ("8.8.8.8", "1.1.1.1", "9.9.9.9", "208.67.222.222")
    return_prefixes: tuple = DEFAULT_RETURN
    edge_gw: str | None = "100.64.0.2"

    @property
    def network(self) -> ipaddress.IPv4Network:
        return ipaddress.ip_interface(f"{self.cloud_ip}/{self.prefixlen}").network

    @property
    def net_text(self) -> str:
        return str(self.network)

    @property
    def netmask(self) -> str:
        return str(self.network.netmask)

    @property
    def headline_answers(self) -> tuple:
        """The two the user cares about first (8.8.8.8, 1.1.1.1), then the rest."""
        first = [a for a in ("8.8.8.8", "1.1.1.1") if a in self.answers]
        return tuple(first + [a for a in self.answers if a not in first])


def _kv(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        v = re.sub(r"\s+#.*$", "", v.strip())
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def parse_conf(text: str) -> CloudConf:
    """/etc/labdesk-cloud.conf -> CloudConf (invalid values fall back to the defaults)."""
    d = _kv(text)
    base = CloudConf()
    ip, plen = base.cloud_ip, base.prefixlen
    m = re.fullmatch(r"(\d+\.\d+\.\d+\.\d+)/(\d+)", d.get("CLOUD_ADDR", ""))
    if m:
        try:
            ipaddress.ip_interface(f"{m[1]}/{m[2]}")
            ip, plen = m[1], int(m[2])
        except ValueError:
            pass
    answers = tuple(a for a in d.get("ANSWER_IPS", "").split() if _is_ip(a)) or base.answers
    rets = tuple(p for p in d.get("RETURN_PREFIXES", "").split() if _is_net(p)) or base.return_prefixes
    edge = d.get("EDGE_GW", base.edge_gw or "")
    return CloudConf(d.get("NS") or base.ns, d.get("BRIDGE") or base.bridge, ip, plen, answers, rets,
                     edge if _is_ip(edge) else None)


def load_conf(path: Path | None = None) -> CloudConf:
    try:
        return parse_conf(Path(path or CONF_PATH).read_text(encoding="utf-8"))
    except OSError:
        return CloudConf()


def _is_ip(s: str) -> bool:
    try:
        ipaddress.IPv4Address(s)
        return True
    except ValueError:
        return False


def _is_net(s: str) -> bool:
    try:
        ipaddress.IPv4Network(s, strict=True)
        return "/" in s
    except ValueError:
        return False


# ============================================================================ health
@dataclass
class CloudHealth:
    state: str                       # 'up' | 'degraded' | 'down' | 'stopped' | 'not_installed' | 'unknown'
    #                                  | 'unsupported' (LabDesk for Windows: Linux only)
    answers: tuple = ()
    selfping: dict = field(default_factory=dict)        # ip -> bool (from the namespace itself)
    host_ping: bool | None = None                       # host -> cloud_ip over virbr0
    ns_exists: bool | None = None
    edge_gw: str | None = None
    routes: list = field(default_factory=list)          # [(prefix, gw)]
    problem: str = ""
    status_age_s: float | None = None
    cloud_ip: str = "100.64.0.1"
    net: str = "100.64.0.0/24"
    rx_packets: int | None = None
    healed: str = ""
    ts: float = 0.0

    @property
    def ok(self) -> bool:
        return self.state == "up"

    @property
    def answers_text(self) -> str:
        return " · ".join(self.answers[:2]) if self.answers else "8.8.8.8 · 1.1.1.1"

    @property
    def headline(self) -> str:
        two = ", ".join(self.answers[:2]) or "8.8.8.8, 1.1.1.1"
        if self.state == "up":
            return f"cloud up — answers {two}"
        if self.state == "degraded":
            return f"cloud degraded — {self.problem or 'partly working'}"
        if self.state == "not_installed":
            return "cloud not installed on this host"
        if self.state == "unsupported":
            return "Internet cloud: Linux only (not available in LabDesk for Windows)"
        if self.state == "stopped":
            return "cloud DOWN — labdesk-cloud.service is stopped"
        if self.state == "down":
            return f"cloud DOWN — {self.problem or 'no answer'}"
        return "cloud state unknown (checking…)"

    @property
    def hint(self) -> str:
        if self.state == "not_installed":
            return INSTALL_HINT
        if self.state in ("down", "stopped", "degraded"):
            return FIX_HINT
        return ""

    def tooltip_lines(self) -> list[str]:
        lines = [self.headline]
        if self.answers:
            lines.append("answers ping: " + ", ".join(self.answers))
        lines.append(f"gateway {self.cloud_ip} on {self.net} (routers use .2, .3, …)")
        if self.edge_gw:
            lines.append(f"return path for lab LANs via {self.edge_gw}"
                         + (f" (+{len(self.routes) - 3} per-prefix)" if len(self.routes) > 3 else ""))
        if self.host_ping is not None:
            lines.append(f"host → {self.cloud_ip}: {'ok' if self.host_ping else 'no reply'}")
        if self.selfping:
            bad = [ip for ip, ok in self.selfping.items() if not ok]
            lines.append("namespace self-test: " + ("all answer" if not bad else "no answer from " + ", ".join(bad)))
        if self.hint:
            lines.append(f"fix: {self.hint}")
        return lines


def parse_status(text: str) -> dict[str, str]:
    return _kv(text)


def health_from(status_text: str | None, *, ns_exists: bool, host_ping: bool | None, installed: bool,
                now: float | None = None, conf: CloudConf | None = None) -> CloudHealth:
    """Combine the root-written status file with the app's own checks (pure; unit-tested)."""
    now = time.time() if now is None else now
    conf = conf or CloudConf()
    h = CloudHealth("unknown", conf.headline_answers, cloud_ip=conf.cloud_ip, net=conf.net_text,
                    host_ping=host_ping, ns_exists=ns_exists, ts=now)
    if not installed:
        h.state, h.problem = "not_installed", "labdesk-cloud is not installed"
        return h
    st = parse_status(status_text or "")
    try:
        h.status_age_s = max(0.0, now - float(st["ts"])) if "ts" in st else None
    except ValueError:
        h.status_age_s = None
    if st.get("answers"):
        h.answers = tuple(a for a in st["answers"].split() if _is_ip(a)) or h.answers
        first = [a for a in ("8.8.8.8", "1.1.1.1") if a in h.answers]
        h.answers = tuple(first + [a for a in h.answers if a not in first])
    for item in st.get("selfping", "").split():
        ip, _, res = item.partition(":")
        if _is_ip(ip):
            h.selfping[ip] = res == "ok"
    h.edge_gw = st.get("edge_gw") or None
    for item in st.get("routes", "").split():
        pfx, _, gw = item.partition(">")
        if _is_net(pfx) and _is_ip(gw):
            h.routes.append((pfx, gw))
    try:
        h.rx_packets = int(st["rx_packets"]) if st.get("rx_packets") else None
    except ValueError:
        h.rx_packets = None
    h.healed = st.get("healed", "")
    fresh = h.status_age_s is not None and h.status_age_s <= STATUS_MAX_AGE_S
    if st.get("state") == "stopped" or (not ns_exists and st.get("state") in (None, "stopped")):
        h.state, h.problem = "stopped", "labdesk-cloud.service is stopped"
        return h
    if not ns_exists:
        h.state, h.problem = "down", f"namespace {conf.ns} is missing"
        return h
    if host_ping is False:
        h.state = "down"
        h.problem = f"{conf.cloud_ip} does not answer from the host (veth not on {conf.bridge}?)"
        return h
    if not fresh:
        h.state = "degraded"
        h.problem = "health check not running (status file " + \
                    ("missing" if h.status_age_s is None else f"{int(h.status_age_s)} s old") + ")"
        return h
    bad = [ip for ip, ok in h.selfping.items() if not ok]
    if st.get("state") == "up" and not bad:
        h.state = "up"
    else:
        h.state = "degraded"
        h.problem = st.get("problem") or ("no answer from " + ", ".join(bad) if bad else "partly working")
    return h


def _ping(ip: str, timeout_s: int = 1) -> bool:
    try:
        cp = subprocess.run(["ping", "-n", "-q", "-c", "1", "-W", str(int(timeout_s)), ip],
                            capture_output=True, text=True, timeout=timeout_s + 3)
        return cp.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def check_health(conf: CloudConf | None = None) -> CloudHealth:
    """IO: status file + /run/netns/<ns> + one host ping (≈1 ms when up, ≤1 s when down).
    Never needs root; run it on the TaskRunner.  Windows: always 'unsupported' (Linux only)."""
    conf = conf or load_conf()
    if not plat.HAS_CLOUD:
        return CloudHealth("unsupported", problem="Linux only", cloud_ip=conf.cloud_ip, net=str(conf.network),
                           ts=time.time())
    installed = UP_SCRIPT.exists() or CONF_PATH.exists()
    ns_exists = Path(f"/run/netns/{conf.ns}").exists()
    try:
        status = STATUS_PATH.read_text(encoding="utf-8")
    except OSError:
        status = None
    host_ping = _ping(conf.cloud_ip) if installed and ns_exists else None
    return health_from(status, ns_exists=ns_exists, host_ping=host_ping, installed=installed, conf=conf)


# ============================================================================ addressing
_ADDR_RE = re.compile(r"^\s*ip address (\d+\.\d+\.\d+\.\d+) (\d+\.\d+\.\d+\.\d+)", re.M)


def ips_in_config(text: str, net: ipaddress.IPv4Network) -> set[str]:
    """IPv4 addresses inside ``net`` configured in an IOS config text."""
    out = set()
    for ip, _mask in _ADDR_RE.findall(text or ""):
        try:
            if ipaddress.IPv4Address(ip) in net:
                out.add(ip)
        except ValueError:
            pass
    return out


def next_free_ip(used, conf: CloudConf | None = None) -> str:
    """Lowest free router address in the cloud subnet (.2 first; the cloud is .1)."""
    conf = conf or CloudConf()
    taken = {str(u) for u in used} | {conf.cloud_ip}
    for host in conf.network.hosts():
        if str(host) not in taken:
            return str(host)
    raise ValueError(f"no free address left in {conf.net_text}")


# ============================================================================ router config
def ospf_processes(config_text: str) -> list[str]:
    return re.findall(r"^router ospf (\d+)", config_text or "", re.M)


def live_commands(iface: str, ip: str, conf: CloudConf | None = None, ospf: list[str] | None = None) -> list[str]:
    """Config-mode lines typed on the router console (IOS)."""
    conf = conf or CloudConf()
    lines = [f"interface {iface}",
             " description INTERNET cloud (LabDesk: answers " + ", ".join(conf.headline_answers[:2]) + ")",
             f" ip address {ip} {conf.netmask}",
             " no shutdown",
             " exit",
             f"ip route 0.0.0.0 0.0.0.0 {conf.cloud_ip}"]
    lines += eem_applet(iface, ip, conf) + [" exit"]       # survives IOSv AutoInstall after a wipe (see eem_applet)
    for pid in ospf or []:
        lines += [f"router ospf {pid}", " default-information originate", " exit"]
    return lines


EEM_NAME = "LABDESK-CLOUD-UPLINK"


def eem_applet(iface: str, ip: str, conf: CloudConf | None = None) -> list[str]:
    """Day-0 only.  IOSv quirk (seen live on CML 2.10.1): after a wipe NVRAM is empty, so IOS AutoInstall
    DHCPs on every interface with carrier while CML's day-0 config is loaded.  On the System Bridge the libvirt
    dnsmasq answers, and the router ends up with 'ip address 192.168.122.x' on the cloud uplink instead of
    100.64.0.N.  This applet re-applies the static address once the day-0 config is in place (CVAC done /
    PnP stopped)."""
    conf = conf or CloudConf()
    return [f"event manager applet {EEM_NAME}",
            ' event syslog pattern "CVAC-4-CONFIG_DONE|PNP-6-PNP_DISCOVERY_STOPPED"',
            ' action 1.0 cli command "enable"',
            ' action 2.0 cli command "configure terminal"',
            f' action 3.0 cli command "interface {iface}"',
            f' action 4.0 cli command "ip address {ip} {conf.netmask}"',
            ' action 5.0 cli command "no shutdown"',
            ' action 6.0 cli command "end"']


def _blocks(text: str) -> list[list[str]]:
    """Split an IOS config into top-level blocks (a line without leading space + its indented lines)."""
    blocks: list[list[str]] = []
    for line in (text or "").splitlines():
        if line.startswith(" ") and blocks:
            blocks[-1].append(line)
        else:
            blocks.append([line])
    return blocks


def patch_day0(config_text: str, iface: str, ip: str, conf: CloudConf | None = None,
               ospf: bool = False) -> str:
    """Idempotently add the cloud uplink to an IOS day-0 config: the interface block (replaced if it
    exists), the static default to the cloud, and ``default-information originate`` under every
    ``router ospf`` when ``ospf``.  Everything else is kept verbatim."""
    conf = conf or CloudConf()
    want_if = [f"interface {iface}",
               " description INTERNET cloud (LabDesk: answers " + ", ".join(conf.headline_answers[:2]) + ")",
               f" ip address {ip} {conf.netmask}",
               " no shutdown"]
    want_route = f"ip route 0.0.0.0 0.0.0.0 {conf.cloud_ip}"
    blocks = _blocks(config_text)
    out: list[list[str]] = []
    placed_if = False
    for b in blocks:
        head = b[0].strip()
        if head.lower() == f"interface {iface}".lower():
            if not placed_if:
                out.append(want_if)
                placed_if = True
            continue
        if re.fullmatch(r"ip route 0\.0\.0\.0 0\.0\.0\.0 \S+.*", head) and conf.cloud_ip in head.split():
            continue                         # re-added below (exactly once)
        if head == f"event manager applet {EEM_NAME}":
            continue
        if ospf and re.match(r"router ospf \d+", head):
            b = [l for l in b if l.strip() != "default-information originate"] + [" default-information originate"]
        out.append(b)
    # insert new blocks before the first 'line ' / 'end' block (IOS order), else at the end
    idx = next((i for i, b in enumerate(out) if b[0].startswith(("line ", "end"))), len(out))
    ins = []
    if not placed_if:
        # keep interfaces together: right after the last interface block if there is one
        last_if = max((i for i, b in enumerate(out[:idx]) if b[0].startswith("interface ")), default=None)
        if last_if is not None:
            out.insert(last_if + 1, want_if)
            idx += 1
        else:
            ins.append(want_if)
    ins.append([want_route])
    ins.append(eem_applet(iface, ip, conf))
    out[idx:idx] = ins
    text = "\n".join(l for b in out for l in b)
    if not text.endswith("\n"):
        text += "\n"
    return text


def day0_has_cloud(config_text: str, iface: str, ip: str, conf: CloudConf | None = None) -> bool:
    conf = conf or CloudConf()
    blocks = _blocks(config_text)
    has_if = any(b[0].strip().lower() == f"interface {iface}".lower() and
                 any(l.strip() == f"ip address {ip} {conf.netmask}" for l in b) for b in blocks)
    has_rt = any(b[0].strip() == f"ip route 0.0.0.0 0.0.0.0 {conf.cloud_ip}" for b in blocks)
    has_eem = any(b == eem_applet(iface, ip, conf) for b in blocks)
    return has_if and has_rt and has_eem


# ============================================================================ return routes file
def parse_routes(text: str) -> tuple[str | None, dict[str, str], bool]:
    """routes.conf -> (EDGE_GW or None, {prefix: gw}, edge_set).  ``edge_set`` tells an explicit
    'EDGE_GW=' (no edge) from a missing line (the /etc default applies)."""
    edge, routes, edge_set = None, {}, False
    for line in (text or "").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        m = re.fullmatch(r"EDGE_GW=\s*(\S*)", line)
        if m:
            edge_set = True
            edge = m[1] if _is_ip(m[1]) else None
            continue
        m = re.fullmatch(r"ROUTE\s+(\S+)\s+(\S+)", line)
        if m and _is_net(m[1]) and _is_ip(m[2]):
            routes[m[1]] = m[2]
    return edge, routes, edge_set


def render_routes(edge: str | None, routes: dict[str, str]) -> str:
    lines = ["# LabDesk Internet cloud: return routes inside the cloud namespace.",
             "# Written by LabDesk; re-applied by labdesk-cloud-routes.path (root) within a second.",
             "# EDGE_GW=<router>   -> 10/8, 172.16/12, 192.168/16 go back via that router",
             "# ROUTE <prefix> <router>   -> a more specific prefix via another edge router",
             f"EDGE_GW={edge or ''}"]
    for pfx in sorted(routes, key=lambda p: (ipaddress.ip_network(p).network_address, p)):
        lines.append(f"ROUTE {pfx} {routes[pfx]}")
    return "\n".join(lines) + "\n"


@dataclass
class RoutePlan:
    edge: str | None
    routes: dict[str, str]
    make_edge: bool                   # this router becomes EDGE_GW
    added: list = field(default_factory=list)       # prefixes routed to this router (per-prefix)
    text: str = ""                    # plain words
    changed: bool = True


def plan_routes(current_text: str | None, router_ip: str, lans, conf: CloudConf | None = None,
                live_edges=None, force_edge: bool | None = None) -> RoutePlan:
    """Decide how the cloud sends replies back to hosts behind ``router_ip``.

    * no edge yet (or the edge is not a router that still uses the cloud: ``live_edges`` = addresses
      known to be configured on lab routers) -> this router becomes EDGE_GW;
    * otherwise this router gets per-prefix ROUTE lines for its private LANs (``lans``)."""
    conf = conf or CloudConf()
    edge, routes, edge_set = parse_routes(current_text or "")
    if not edge_set:
        edge = conf.edge_gw
    live = set(live_edges or ())
    become = force_edge if force_edge is not None else (
        not edge or edge == router_ip or (live_edges is not None and edge not in live))
    new_routes = dict(routes)
    added = []
    if become:
        # this router owns the summary; drop per-prefix lines that pointed at it (redundant)
        new_routes = {p: g for p, g in new_routes.items() if g != router_ip}
        new_edge = router_ip
        text = (f"{router_ip} becomes the cloud's EDGE_GW: replies to "
                + ", ".join(conf.return_prefixes) + f" go back via {router_ip}")
    else:
        new_edge = edge
        for net in lans or ():
            try:
                n = ipaddress.ip_network(str(net), strict=False)
            except ValueError:
                continue
            if n.prefixlen >= 32 or n.overlaps(conf.network) or not any(n.subnet_of(p) for p in PRIVATE):
                continue
            new_routes[str(n)] = router_ip
            added.append(str(n))
        text = (f"EDGE_GW stays {edge}; " + (f"per-prefix return routes via {router_ip}: " + ", ".join(added)
                                              if added else f"no LAN prefixes known for {router_ip} yet"))
    changed = (new_edge, new_routes) != (edge, routes) or not edge_set
    return RoutePlan(new_edge, new_routes, become, added, text, changed)


def read_routes_file(path: Path | None = None) -> str | None:
    try:
        return Path(path or ROUTES_PATH).read_text(encoding="utf-8")
    except OSError:
        return None


def write_routes_file(text: str, path: Path | None = None) -> None:
    """Atomic write of the user-owned routes file (the path unit picks it up).  Raises PermissionError
    when the directory is not writable (cloud not installed for this user)."""
    p = Path(path or ROUTES_PATH)
    tmp = p.with_name(f".{p.name}.{os.getpid()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.chmod(tmp, 0o644)
    os.replace(tmp, p)


def manual_routes_command(text: str, path: Path | None = None) -> str:
    """The one-liner to run when the routes file cannot be written by this user."""
    body = text.strip().splitlines()
    body = [l for l in body if not l.startswith("#")]
    esc = "\\n".join(body)
    return f"printf '{esc}\\n' | sudo tee {Path(path or ROUTES_PATH).as_posix()} >/dev/null"


# ============================================================================ CML node helpers
def is_cloud_node(node: dict) -> bool:
    """A CML node dict that is the LabDesk Internet cloud (tag, or the INTERNET label convention)."""
    if node.get("node_definition") != "external_connector":
        return False
    tags = node.get("tags") or []
    if CLOUD_TAG in tags:
        return True
    return False


def cloud_iface_lans(ifaces) -> list[str]:
    """Connected private networks of a router (IfFact-like objects with ip / prefixlen)."""
    out = []
    for f in ifaces or ():
        ip, plen = getattr(f, "ip", None), getattr(f, "prefixlen", None)
        if not ip or plen is None:
            continue
        try:
            out.append(str(ipaddress.ip_interface(f"{ip}/{plen}").network))
        except ValueError:
            pass
    return sorted(set(out))
