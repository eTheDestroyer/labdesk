"""Unified fact model (D3), link state machine (D4) and default-route resolver (D5).

Everything here is PURE: no I/O, no Qt, no clocks except the ``now`` arguments.  The
telemetry engine (telemetry.py) feeds it parsed CLI output and CML API state; the UI only
reads the resulting dataclasses.  String vocabularies live in the enums below - UI code must
compare against these, never against literal spellings.

Field-by-field documentation: docs/telemetry_model.md.
"""
from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Generic, Iterable, TypeVar

from .parsers.common import expand_iface, iface_key, short_iface
from .parsers.ios import IP_ADDR_CMD

T = TypeVar("T")


# ======================================================================= vocabularies
class LinkSide(str, Enum):
    UP = "UP"
    ADMIN_DOWN = "ADMIN_DOWN"
    DOWN = "DOWN"                    # physical down/down
    PROTO_DOWN = "PROTO_DOWN"        # up / line protocol down
    NOT_APPLICABLE = "NOT_APPLICABLE"  # unmanaged switch / connector / unprobed end
    UNKNOWN = "UNKNOWN"              # no fresh CLI fact


class Health(str, Enum):
    OK = "OK"
    PARTIAL = "PARTIAL"
    DOWN = "DOWN"
    UNKNOWN = "UNKNOWN"


class P2PSource(str, Enum):
    OSPF_OPER = "OSPF_OPER"          # IOS 'show ip ospf interface [brief]' -> P2P / POINT_TO_POINT
    CONFIG = "CONFIG"                # 'ip ospf network point-to-point' in the running-config only
    JUNOS_PTTOPT = "JUNOS_PTTOPT"    # Junos ospf interface type P2P / state PtToPt
    FORTIOS_P2P = "FORTIOS_P2P"      # FortiOS 'Network Type POINTOPOINT'
    MEDIUM = "MEDIUM"                # serial / PPP / HDLC medium


OPER_P2P_SOURCES = {P2PSource.OSPF_OPER, P2PSource.JUNOS_PTTOPT, P2PSource.FORTIOS_P2P, P2PSource.MEDIUM}


class LinkKind(str, Enum):
    """D4 link status, in priority order (first match wins)."""
    LAB_OFF = "LAB_OFF"
    NODE_OFF = "NODE_OFF"
    CABLE_PULLED = "CABLE_PULLED"
    LINK_NOT_STARTED = "LINK_NOT_STARTED"
    UNKNOWN = "UNKNOWN"
    ADMIN_DOWN = "ADMIN_DOWN"
    LINK_DOWN = "LINK_DOWN"
    PROTOCOL_DOWN = "PROTOCOL_DOWN"
    UP = "UP"


DOWN_KINDS = {LinkKind.ADMIN_DOWN, LinkKind.LINK_DOWN, LinkKind.PROTOCOL_DOWN, LinkKind.CABLE_PULLED}


class CollectorState(str, Enum):
    OK = "ok"
    LEARNING = "learning"                  # booted, first FAST burst not complete yet
    PAUSED_CONSOLE = "paused_console"      # a console tab for this node is open (D10)
    CONSOLE_BUSY = "console_busy"          # foreign session in config mode / pager / prompt
    NEEDS_CREDENTIALS = "needs_credentials"  # login / enable secret required, none stored
    ERROR = "error"                        # timeout / ssh failure (backing off)
    NO_MGMT = "no_mgmt"                    # KVM: no management IP
    LIMITED = "limited"                    # FortiOS unlicensed (confirmed by get system status)
    UNVERIFIED = "unverified"              # KVM collector (beta) - not wired in this release
    OFF = "off"                            # node not booted
    DISABLED = "disabled"                  # global probing switch off
    NOT_PROBED = "not_probed"              # switches, connectors, hosts


class Proto(str, Enum):
    OSPF = "OSPF"
    BGP = "BGP"
    HSRP = "HSRP"
    VRRP = "VRRP"


class PathState(str, Enum):
    EGRESS = "EGRESS"                      # reaches an external connector
    VERIFIED_INTERNET = "VERIFIED_INTERNET"  # EGRESS + a user-run Verify Internet succeeded
    BROKEN_AT = "BROKEN_AT"
    NO_EGRESS = "NO_EGRESS"                # ends inside the lab (next router has no default)
    NONE = "NONE"                          # this router has no default route
    BLACKHOLE = "BLACKHOLE"                # Null0 / discard
    UNRESOLVED = "UNRESOLVED"              # next hop not reachable / not in the lab
    LOOP = "LOOP"
    STALE = "STALE"                        # a router on the path has stale facts
    UNKNOWN = "UNKNOWN"                    # no fresh facts for the origin router


class SegmentKind(str, Enum):
    P2P = "p2p"            # two routers, direct cable
    MULTI = "multi"        # unmanaged switch (+ all its members)
    EGRESS = "egress"      # contains an external connector
    OTHER = "other"        # hosts etc.


class NodeRole(str, Enum):
    ROUTER = "router"      # probed (IOS/IOS-XE, Junos, FortiOS)
    SWITCH = "switch"      # unmanaged_switch / iosvl2 - a segment hub
    EGRESS = "egress"      # external_connector (NAT / System Bridge)
    HOST = "host"


class PortRole(str, Enum):
    """STP port role as IOS abbreviates it ('show spanning-tree')."""
    ROOT = "Root"
    DESG = "Desg"
    ALTN = "Altn"
    BACK = "Back"
    MSTR = "Mstr"
    DISA = "Disa"
    SHR = "Shr"
    NONE = "None"


class PortState(str, Enum):
    FWD = "FWD"
    BLK = "BLK"
    LRN = "LRN"
    LIS = "LIS"
    DIS = "DIS"
    BKN = "BKN"


class PortMode(str, Enum):
    ACCESS = "access"
    TRUNK = "trunk"
    ROUTED = "routed"        # router interface / 'no switchport'
    UNKNOWN = "unknown"


class EcProto(str, Enum):
    """EtherChannel negotiation protocol of one side (``show etherchannel summary`` Protocol column)."""
    LACP = "lacp"
    PAGP = "pagp"
    ON = "on"                        # static 'channel-group N mode on' (column shows '-')
    NONE = "none"                    # the member ports have no channel-group at all


class EcState(str, Enum):
    """Health of a bundle (``Bundle.state``): drives the bead style."""
    UP = "up"                        # every member bundled, Port-channel up
    PARTIAL = "partial"              # 1..N-1 members bundled (warning '1/2')
    DOWN = "down"                    # Port-channel down / no member bundled
    MISMATCH = "mismatch"            # a configuration that never bundles (amber ⚠ + reason)
    UNKNOWN = "unknown"              # facts too old


class SubnetState(str, Enum):
    """What the cable's subnet label says (LinkFacts.addressing.state)."""
    MATCH = "match"              # both ends share a subnet, or the only L3 end has one (router <-> switch / host)
    ONE_SIDED = "one_sided"      # two L3 ends, only one of them has an address
    MISMATCH = "mismatch"        # two L3 ends with addresses in different subnets
    DHCP = "dhcp"                # DHCP client end (with or without a lease yet)
    UNNUMBERED = "unnumbered"    # ip unnumbered (borrows another interface's address)
    NONE = "none"                # nothing configured / nothing known: draw nothing


ROLE_WORDS = {PortRole.ROOT: "Root", PortRole.DESG: "Designated", PortRole.ALTN: "Alternate",
              PortRole.BACK: "Backup", PortRole.MSTR: "Master", PortRole.DISA: "Disabled",
              PortRole.SHR: "Shared", PortRole.NONE: "None"}
STATE_WORDS = {PortState.FWD: "forwarding", PortState.BLK: "blocking", PortState.LRN: "learning",
               PortState.LIS: "listening", PortState.DIS: "disabled", PortState.BKN: "broken"}
STP_MODE_LABELS = {"rapid-pvst": "RPVST+", "pvst": "PVST+", "mst": "MST"}

PROBED_CML_DEFS = {"iosv", "iol-xe", "csr1000v", "cat8000v", "iosv-xe"}
MANAGED_SWITCH_DEFS = {"iosvl2", "ioll2-xe"}       # probed like routers, with the L2 command set
SWITCH_DEFS = {"unmanaged_switch"} | MANAGED_SWITCH_DEFS
ROUTER_KVM_KINDS = {"vjunos", "fortios"}


# ======================================================================= facts
@dataclass(frozen=True)
class Fact(Generic[T]):
    value: T
    ts: float                 # time.time() when the answer was read
    source: str = "cli"       # 'api' | 'cli' | 'syslog'

    def age(self, now: float) -> float:
        return max(0.0, now - self.ts)

    def fresh(self, now: float, max_age: float) -> bool:
        return self.age(now) <= max_age


@dataclass
class IfFact:
    name: str                       # device name, full form (GigabitEthernet0/2, ge-0/0/0, port2)
    short: str                      # Gi0/2
    ip: str | None = None
    prefixlen: int | None = None
    admin: str | None = None        # 'up' | 'down'
    oper: str | None = None         # 'up' | 'down'  (line protocol)
    phys: str | None = None         # 'up' | 'down' | 'admin-down' (IOS 'Status' column)
    method: str | None = None       # IOS: TFTP / manual / DHCP / unset
    ospf_net: str | None = None     # POINT_TO_POINT | BROADCAST | LOOPBACK | ... (oper)
    ospf_state: str | None = None   # P2P | DR | BDR | DROTH | WAIT | DOWN | LOOP
    ospf_passive: bool | None = None
    ospf_nbrs: tuple[int, int] | None = None   # (full, configured) from the brief table
    desc: str | None = None
    mac: str | None = None

    @property
    def side(self) -> LinkSide:
        if self.phys == "admin-down" or self.admin == "down":
            return LinkSide.ADMIN_DOWN
        if self.phys == "down":
            return LinkSide.DOWN
        if self.phys == "up" and self.oper == "down":
            return LinkSide.PROTO_DOWN
        if self.phys == "up" and self.oper == "up":
            return LinkSide.UP
        return LinkSide.UNKNOWN

    @property
    def cidr(self) -> str | None:
        if self.ip and self.prefixlen is not None:
            return str(ipaddress.IPv4Interface(f"{self.ip}/{self.prefixlen}").network)
        return None


@dataclass
class OspfNbr:
    neighbor_id: str
    state: str           # 'FULL/DR', 'FULL/-'
    adj_state: str       # FULL | 2WAY | INIT | EXSTART | ...
    role: str            # DR | BDR | DROTHER | -
    address: str
    iface: str           # full device name
    priority: int = 0


@dataclass
class BgpPeer:
    ip: str
    remote_as: Any
    state: str            # 'Established' or the FSM state
    established: bool
    pfx_rcvd: int | None
    uptime: str | None


@dataclass
class BgpInfo:
    active: bool
    local_as: Any = None
    rid: str | None = None
    peers: list[BgpPeer] = field(default_factory=list)


@dataclass
class FhrpGroup:
    proto: Proto          # HSRP | VRRP
    iface: str            # full device name
    group: int
    state: str            # HSRP: Active/Standby/Speak/Listen/Init ; VRRP: Master/Backup/Init
    priority: int | None
    preempt: bool | None
    vip: str | None
    active: str | None = None      # HSRP active router ('local' / ip) ; VRRP master ip
    standby: str | None = None

    @property
    def is_primary(self) -> bool:
        return self.state in ("Active", "Master")

    @property
    def role(self) -> str:
        return {"Active": "Active", "Master": "Master", "Standby": "Standby", "Backup": "Backup"}.get(
            self.state, self.state)


@dataclass
class DefaultRoute:
    present: bool
    code: str | None = None               # 'S*' | 'B*' | 'O*E2' | ...
    source_label: str | None = None       # 'S* dhcp' | 'B* eBGP' | 'O*E2'
    proto: str | None = None              # static | bgp | ospf | ...
    distance: int | None = None
    next_hops: list[tuple[str | None, str | None]] = field(default_factory=list)  # (ip, out_iface)
    via_dhcp: bool = False
    blackhole: bool = False
    from_ip: str | None = None
    forwarding_checked: bool = False      # next_hops came from CEF (recursion resolved)


@dataclass
class ConfigFacts:
    text: str                              # REDACTED running-config
    p2p_ifaces: set[str] = field(default_factory=set)       # iface_key with ip ospf network p2p
    passive_ifaces: set[str] = field(default_factory=set)
    shutdown_ifaces: set[str] = field(default_factory=set)
    ospf_enabled: bool = False
    bgp_asn: Any = None
    parsed: dict = field(default_factory=dict)
    addrs: dict = field(default_factory=dict)              # iface_key -> AddrInfo (source 'config')


# ----------------------------------------------------------------------- L2 (managed switches)
@dataclass
class VlanInfo:
    id: int
    name: str
    status: str                      # active | act/lshut | suspended ...
    ports: list[str] = field(default_factory=list)   # short access ports ('show vlan brief')


@dataclass
class TrunkInfo:
    port: str                        # short ('Gi0/1')
    native: int | None
    allowed: list[tuple[int, int]]   # VLAN ranges
    allowed_text: str                # device text: '10,20' | '1-4094'
    active: list[tuple[int, int]]    # allowed and active in the management domain
    active_text: str
    forwarding: list[tuple[int, int]]  # in STP forwarding state and not pruned
    forwarding_text: str
    status: str = "trunking"
    encap: str = "802.1q"
    mode: str = "on"


@dataclass
class SwitchPortInfo:
    port: str                        # short
    iface: str                       # full
    status: str                      # connected | notconnect | disabled | err-disabled | ...
    vlan: Any                        # int (access VLAN) | 'trunk' | 'routed' | None
    name: str | None = None          # description (truncated by IOS)
    duplex: str | None = None        # 'a-full' | 'full' | 'auto'
    speed: str | None = None         # 'auto' | 'a-1000' | '100'

    @property
    def mode(self) -> PortMode:
        if self.vlan == "trunk":
            return PortMode.TRUNK
        if self.vlan == "routed":
            return PortMode.ROUTED
        if isinstance(self.vlan, int):
            return PortMode.ACCESS
        return PortMode.UNKNOWN


@dataclass
class StpPortInfo:
    port: str                        # short
    role: PortRole
    state: PortState
    cost: int
    prio: int = 128
    nbr: int = 0
    edge: bool = False
    p2p: bool = False
    inconsistent: bool = False

    @property
    def blocked(self) -> bool:
        return self.state == PortState.BLK


@dataclass
class StpVlanInfo:
    vlan: int
    protocol: str | None             # rstp | ieee | mstp
    root_priority: int | None
    root_address: str | None
    root_cost: int
    root_port: str | None            # short, None on the root bridge
    is_root: bool
    bridge_priority: int | None
    bridge_address: str | None
    ports: dict[str, StpPortInfo] = field(default_factory=dict)   # iface_key -> port

    @property
    def root_id(self) -> str:
        return f"{self.root_priority} / {self.root_address}" if self.root_address else "?"

    @property
    def bridge_id(self) -> str:
        return f"{self.bridge_priority} / {self.bridge_address}" if self.bridge_address else "?"


@dataclass
class TcInfo:
    changes: int
    last_ago_s: int | None
    from_iface: str | None


@dataclass
class L2Facts:
    """Everything the L2 command set reads from a managed switch (each part ages separately)."""
    vlans: Fact[dict[int, VlanInfo]] | None = None
    trunks: Fact[dict[str, TrunkInfo]] | None = None          # iface_key -> trunk
    ports: Fact[dict[str, SwitchPortInfo]] | None = None      # iface_key -> switchport
    stp: Fact[dict[int, StpVlanInfo]] | None = None
    stp_mode: Fact[str] | None = None                         # 'rapid-pvst' | 'pvst' | 'mst'
    root_for: Fact[list[int]] | None = None
    tc: Fact[dict[int, TcInfo]] | None = None                  # topology change counters per VLAN
    mac_counts: Fact[dict[int, int]] | None = None            # VLAN -> total MAC entries
    mac_table: Fact[list[dict]] | None = None                 # dynamic entries {vlan, mac, type, port}
    cdp: Fact[list[dict]] | None = None
    etherchannel: Fact[dict] | None = None
    routing: Fact[bool] | None = None                         # 'ip routing' active (routing table shown)

    def port(self, name: str) -> SwitchPortInfo | None:
        return self.ports.value.get(iface_key(name)) if self.ports else None

    def trunk(self, name: str) -> TrunkInfo | None:
        return self.trunks.value.get(iface_key(name)) if self.trunks else None

    def user_vlans(self) -> dict[int, VlanInfo]:
        return {k: v for k, v in (self.vlans.value if self.vlans else {}).items() if not 1002 <= k <= 1005}

    def configured_vlans(self) -> dict[int, VlanInfo]:
        """User VLANs without the default VLAN 1 (what 'N VLANs' counts)."""
        return {k: v for k, v in self.user_vlans().items() if k != 1}


@dataclass
class EcFacts:
    """EtherChannel tables of one device (parsers/ios_l2.py shapes); each part ages separately."""
    summary: Fact[dict] | None = None             # show etherchannel summary  {groups, in_use, aggregators}
    lacp_internal: Fact[dict] | None = None       # {group: [{port, mode, state, prio, port_number, ...}]}
    lacp_neighbor: Fact[dict] | None = None       # {group: [{port, mode, dev_id, prio, port_number, ...}]}
    lacp_sys: Fact[dict] | None = None            # {priority, mac}
    pagp_internal: Fact[dict] | None = None       # {group: [{port, mode, state, prio, ...}]}
    pagp_neighbor: Fact[dict] | None = None       # {group: [{port, name, dev_id, partner_port, ...}]}
    load_balance: Fact[dict] | None = None        # {method, ipv4, ...}
    problems: dict = field(default_factory=dict)  # port short -> (reason, ts) from %EC-5-CANNOT_BUNDLE syslog

    def groups(self) -> list[dict]:
        return list(self.summary.value.get("groups") or []) if self.summary else []

    def protos(self) -> set[str]:
        return {g.get("proto") for g in self.groups()}

    def group_of(self, port: str) -> tuple[dict, dict] | None:
        """(group row, port row) of a member port (short or full name)."""
        k = iface_key(port)
        for g in self.groups():
            for pt in g["ports"]:
                if iface_key(pt["port"]) == k:
                    return g, pt
        return None

    def _row(self, fact: "Fact[dict] | None", group: int, port: str) -> dict | None:
        if fact is None:
            return None
        k = iface_key(port)
        return next((r for r in fact.value.get(group, []) if iface_key(r["port"]) == k), None)

    def lacp_port(self, group: int, port: str) -> dict | None:
        return self._row(self.lacp_internal, group, port)

    def lacp_partner(self, group: int, port: str) -> dict | None:
        return self._row(self.lacp_neighbor, group, port)

    def pagp_port(self, group: int, port: str) -> dict | None:
        return self._row(self.pagp_internal, group, port)

    def pagp_partner(self, group: int, port: str) -> dict | None:
        return self._row(self.pagp_neighbor, group, port)


@dataclass
class NodeFacts:
    node_key: str
    label: str
    vendor: str = "cisco"
    hostname: str | None = None
    rid: Fact[str] | None = None
    loopback: str | None = None
    ifaces: Fact[dict[str, IfFact]] | None = None     # key: iface_key(name)
    ospf_nbrs: Fact[list[OspfNbr]] | None = None
    ospf_running: Fact[bool] | None = None
    bgp: Fact[BgpInfo] | None = None
    fhrp: Fact[list[FhrpGroup]] | None = None
    default_route: Fact[DefaultRoute] | None = None
    connected: Fact[dict[str, int]] | None = None     # iface_key -> prefixlen (show ip route connected)
    config: Fact[ConfigFacts] | None = None
    collector_state: CollectorState = CollectorState.LEARNING
    collector_detail: str = ""
    last_burst_ts: float | None = None
    ospf_passive_set: set | None = None               # from 'show ip protocols' (kept until ifaces exist)
    l2: L2Facts | None = None                         # managed switches only
    ec: "EcFacts | None" = None                       # EtherChannel tables (switches; routers with a Port-channel)
    addrs: Fact[dict[str, "AddrInfo"]] | None = None  # iface_key -> addresses ('show ip interface', source cli)
    addr_overrides: dict[str, "AddrInfo"] = field(default_factory=dict)   # console tap (source 'console')
    addr_live_ts: float | None = None                 # last time an address source was read (cli / tap)
    accounts: "Fact | None" = None                    # accounts.DeviceAccounts from the running-config (CONFIG tier)
    accounts_note: str = ""                           # why accounts are unknown ('needs enable secret')
    iface_macs: "Fact | None" = None                  # Junos: {mac: 'ge-0/0/0'} (live 'show interfaces')
    junos_local_as: "Fact | None" = None              # Junos: routing-options autonomous-system (live)

    def iface(self, name: str) -> IfFact | None:
        if self.ifaces is None:
            return None
        return self.ifaces.value.get(iface_key(name))

    def owns_ip(self, ip: str) -> IfFact | None:
        if self.ifaces is None or not ip:
            return None
        for f in self.ifaces.value.values():
            if f.ip == ip:
                return f
        return None


# ----------------------------------------------------------------------- interface addresses
ADDR_SOURCES = ("cli", "console", "config")     # provenance, most to least authoritative per read time
# Live-only rule: every source above is device output (CLI probe, the device's own console transcript, the
# running-config read from the device).  CML's layer3_addresses (L2 DHCP snooping) is NOT device output and
# is not used.


@dataclass(frozen=True)
class AddrInfo:
    """The IPv4 addressing of one interface, from one source at one time."""
    primary: tuple[str, int] | None = None                  # (ip, prefixlen)
    secondaries: tuple[tuple[str, int], ...] = ()
    method: str = "none"          # static | dhcp | unnumbered | negotiated | none
    unnumbered: str | None = None  # 'Loopback0' (method 'unnumbered')
    unnumbered_ip: str | None = None
    source: str = "cli"           # cli | console | config | api
    ts: float = 0.0

    @property
    def configured(self) -> bool:
        return self.primary is not None or bool(self.secondaries) or self.method in ("dhcp", "unnumbered",
                                                                                      "negotiated")

    @property
    def addresses(self) -> list[tuple[str, int, bool]]:
        """[(ip, prefixlen, secondary)] primary first."""
        out = [(self.primary[0], self.primary[1], False)] if self.primary else []
        return out + [(ip, pl, True) for ip, pl in self.secondaries]

    @property
    def cidrs(self) -> list[str]:
        return [f"{ip}/{pl}" if pl is not None else ip for ip, pl, _ in self.addresses]

    @property
    def networks(self) -> list[str]:
        out = []
        for ip, pl, _ in self.addresses:
            n = _subnet(ip, pl) if pl is not None else None
            if n and n not in out:
                out.append(n)
        return out

    def signature(self) -> tuple:
        """What the event log compares (the source and the read time do not matter)."""
        return (self.primary, tuple(sorted(self.secondaries)), self.method, self.unnumbered)


def addr_from_parse(row: dict, ts: float, source: str = "cli") -> AddrInfo:
    """parsers.ios.parse_ip_interface_addrs row -> AddrInfo."""
    prim = (row["ip"], row["prefixlen"]) if row.get("ip") and row.get("prefixlen") is not None else None
    return AddrInfo(prim, tuple((ip, int(pl)) for ip, pl in row.get("secondaries") or []),
                    row.get("method") or ("static" if prim else "none"), row.get("unnumbered"),
                    row.get("unnumbered_ip"), source, ts)


def apply_addr_cmd(info: AddrInfo | None, op: str, ip: str | None = None, prefixlen: int | None = None,
                   src: str | None = None, ts: float = 0.0, source: str = "console") -> AddrInfo:
    """IOS semantics of one interface address command on top of the current addressing."""
    cur = info or AddrInfo(source=source, ts=ts)
    prim, secs = cur.primary, list(cur.secondaries)
    method, unn, unn_ip = cur.method, cur.unnumbered, cur.unnumbered_ip
    if op == "set" and ip and prefixlen is not None:
        prim, method, unn, unn_ip = (ip, prefixlen), "static", None, None
        secs = [x for x in secs if x[0] != ip]
    elif op == "add_sec" and ip and prefixlen is not None:
        if (ip, prefixlen) not in secs and (prim is None or prim[0] != ip):
            secs.append((ip, prefixlen))
        if method in ("none", "unnumbered"):
            method, unn, unn_ip = ("static" if prim else method), None, None
    elif op == "del_sec":
        secs = [x for x in secs if x[0] != ip]
    elif op == "del_primary":
        if prim is not None and (ip is None or prim[0] == ip):
            prim = None
            method = "none" if not secs else method
    elif op == "clear":
        prim, secs, method, unn, unn_ip = None, [], "none", None, None
    elif op == "dhcp":
        prim, secs, method, unn, unn_ip = None, [], "dhcp", None, None
    elif op == "unnumbered":
        prim, secs, method, unn, unn_ip = None, [], "unnumbered", src, None
    elif op == "no_unnumbered":
        if method == "unnumbered":
            method, unn, unn_ip = "none", None, None
    return AddrInfo(prim, tuple(secs), method, unn, unn_ip, source, ts)


def _fallback_addr(f: "IfFact", nf: "NodeFacts", ts: float) -> AddrInfo:
    """Addressing from 'show ip interface brief' + prefix lengths (OSPF brief / connected routes) when
    the address command has not been read (old fixtures, KVM parsers)."""
    plen = f.prefixlen
    k = iface_key(f.name)
    if plen is None and nf.connected:
        plen = nf.connected.value.get(k)
    if plen is None and nf.config is not None:
        ca = nf.config.value.addrs.get(k)
        if ca is not None and ca.primary and ca.primary[0] == f.ip:
            plen = ca.primary[1]
    prim = (f.ip, plen) if f.ip else None          # prefix length may be unknown (None)
    method = "dhcp" if (f.method or "").upper() == "DHCP" else ("static" if f.ip else "none")
    return AddrInfo(prim, (), method, None, None, "cli", ts)


def effective_addrs(nf: "NodeFacts | None") -> dict[str, AddrInfo]:
    """iface_key -> the newest addressing of every interface, merging the sources by read time:
    the CLI address read (or the 'ip interface brief' fallback), then console-tap overrides that are
    newer than that read.  An override is dropped as soon as a later CLI read exists."""
    if nf is None:
        return {}
    out: dict[str, AddrInfo] = {}
    if nf.addrs is not None:
        out.update(nf.addrs.value)
    if nf.ifaces is not None:
        for k, f in nf.ifaces.value.items():
            if k not in out:                  # not covered by the address read (or it never ran)
                out[k] = _fallback_addr(f, nf, nf.ifaces.ts)
    base_ts = max([x for x in (nf.addrs.ts if nf.addrs else None, nf.ifaces.ts if nf.ifaces else None)
                   if x is not None], default=None)
    for k, ov in nf.addr_overrides.items():
        cli = out.get(k)
        cli_ts = cli.ts if (cli is not None and cli.source == "cli") else (base_ts or 0.0)
        if ov.ts > cli_ts:
            out[k] = ov
    return out


def prune_overrides(nf: "NodeFacts") -> None:
    """Forget console-tap overrides that a later CLI read has superseded."""
    if not nf.addr_overrides:
        return
    ref = nf.addrs.ts if nf.addrs else None
    if ref is None:
        return
    for k in [k for k, ov in nf.addr_overrides.items() if ov.ts <= ref]:
        del nf.addr_overrides[k]


def addr_snapshot(nf: "NodeFacts | None") -> dict[str, tuple]:
    """iface_key -> AddrInfo.signature() for interfaces that have any addressing (event diffs)."""
    return {k: (a.signature(), a.source) for k, a in effective_addrs(nf).items() if a.configured}


@dataclass
class SubnetChange:
    iface: str                     # short name ('Gi0/2', 'Gi0/0.10')
    iface_key: str
    kind: str                      # 'configured' | 'changed' | 'removed'
    text: str                      # 'Gi0/2: 10.0.13.0/30 configured'
    nets: list[str] = field(default_factory=list)


def _addr_words(a: AddrInfo) -> list[str]:
    if a.method == "unnumbered":
        return [f"unnumbered ({short_iface(a.unnumbered or '?')})"]
    if a.method in ("dhcp", "negotiated") and not a.primary:
        return ["dhcp"]
    out = []
    for ip, pl, sec in a.addresses:
        n = _subnet(ip, pl) if pl is not None else ip
        out.append(n + (" secondary" if sec else "") + (" dhcp" if a.method == "dhcp" and not sec else ""))
    return out


def subnet_changes(before: dict[str, AddrInfo] | None, after: dict[str, AddrInfo]) -> list[SubnetChange]:
    """Pure diff of two effective address maps (iface_key -> AddrInfo) -> SUBNET event lines.
    ``before is None`` (first read) is the baseline: no events."""
    if before is None:
        return []
    out = []
    for k in sorted(set(before) | set(after)):
        b, a = before.get(k), after.get(k)
        b = b if b is not None and b.configured else None
        a = a if a is not None and a.configured else None
        if (b.signature() if b else None) == (a.signature() if a else None):
            continue
        name = short_iface(expand_iface(k))
        bw, aw = (_addr_words(b) if b else []), (_addr_words(a) if a else [])
        if b is None:
            out.append(SubnetChange(name, k, "configured", f"{name}: {', '.join(aw)} configured", a.networks))
            continue
        if a is None:
            out.append(SubnetChange(name, k, "removed", f"{name}: {', '.join(bw)} removed", b.networks))
            continue
        added = [w for w in aw if w not in bw]
        gone = [w for w in bw if w not in aw]
        if not added and not gone:
            # same subnets, different host address (or method): show the interface addresses
            ba, aa = ", ".join(b.cidrs) or b.method, ", ".join(a.cidrs) or a.method
            out.append(SubnetChange(name, k, "changed", f"{name}: {aa} changed (was {ba})", a.networks))
        elif added and gone and len(added) == 1 and len(gone) == 1:
            out.append(SubnetChange(name, k, "changed", f"{name}: {added[0]} changed (was {gone[0]})", a.networks))
        else:
            if added:
                out.append(SubnetChange(name, k, "configured", f"{name}: {', '.join(added)} configured", a.networks))
            if gone:
                out.append(SubnetChange(name, k, "removed", f"{name}: {', '.join(gone)} removed", b.networks))
    return out


def host_part(ip: str, plen: int) -> str:
    """The host bits as dotted octets: 10.0.12.1/30 -> '.1', 172.16.5.9/16 -> '.5.9', /32 -> ''."""
    if plen >= 32:
        return ""
    octs = ip.split(".")
    n = max(1, min(4, 4 - plen // 8))
    return "." + ".".join(octs[4 - n:])


def short_net(net: str) -> str:
    """'10.0.12.0/30' -> '10.0.12/30' (trailing zero octets dropped: the zoomed-out form)."""
    try:
        addr, pl = net.split("/")
    except ValueError:
        return net
    octs = addr.split(".")
    keep = max(1, (int(pl) + 7) // 8)
    while len(octs) > keep and octs[-1] == "0":
        octs.pop()
    return ".".join(octs) + "/" + pl


def compact_net(net: str) -> str:
    """The far-zoom form: '192.168.10.0/24' -> '192.168.10/24', '10.0.12.0/30' -> '…12.0/30'."""
    try:
        addr, pl = net.split("/")
        if pl == "?" or int(pl) <= 24:
            return short_net(net)
    except ValueError:
        return net
    return "…" + ".".join(addr.split(".")[-2:]) + "/" + pl


def dhcp_net(net: str) -> str:
    """'192.168.255.0/24' -> '192.168.255.x/24' (host octets as x)."""
    try:
        addr, pl = net.split("/")
    except ValueError:
        return net
    octs = addr.split(".")
    full = int(pl) // 8
    return ".".join(o if i < full else "x" for i, o in enumerate(octs)) + "/" + pl


# ======================================================================= topology inputs
@dataclass
class TopoNode:
    key: str
    label: str
    role: NodeRole
    vendor: str
    state: str                     # CML state (BOOTED...) or virsh state (running...)
    node_definition: str = ""
    egress_mode: str | None = None  # 'NAT' | 'Bridge' | 'Cloud' (LabDesk Internet cloud) for external connectors
    probed: bool = True             # False: a router without a CLI collector (KVM in this release)
    managed: bool = False           # a managed switch (IOSvL2): probed with the L2 command set

    @property
    def booted(self) -> bool:
        return self.state in ("BOOTED", "running")

    @property
    def collected(self) -> bool:
        """This node has a CLI collector (IOS router or managed switch)."""
        return self.probed and (self.role == NodeRole.ROUTER or (self.role == NodeRole.SWITCH and self.managed))


@dataclass
class TopoLink:
    key: str
    a: str
    b: str
    iface_a: str           # CML interface label (GigabitEthernet0/2 / port0 / ge-0/0/0)
    iface_b: str
    api_state: str         # CML link state (STARTED / STOPPED / DEFINED_ON_CORE) or 'up' for KVM
    kvm: bool = False
    mac_a: str = ""        # host-KVM: the VM NIC MAC of that end (to find the device's own interface name)
    mac_b: str = ""


@dataclass
class Topology:
    lab_state: str
    nodes: dict[str, TopoNode]
    links: dict[str, TopoLink]


# ======================================================================= derived outputs
@dataclass
class Segment:
    id: str
    kind: SegmentKind
    switch_node: str | None
    members: list[tuple[str, str]]          # (node_key, iface label) - routers/hosts/connectors
    link_keys: list[str]
    subnet: str | None = None
    protocols: dict[Proto, Health] = field(default_factory=dict)
    adjacencies: list[tuple[str, str, Proto, str]] = field(default_factory=list)  # (nodeA, nodeB, proto, state)
    detail: dict[Proto, str] = field(default_factory=dict)   # plain-words per protocol
    bgp_sessions: list["SegBgpSession"] = field(default_factory=list)  # BGP sessions crossing this segment


@dataclass
class SegBgpSession:
    """One BGP session between two routers whose peer addresses sit on the same switch segment.
    The switch itself does not run BGP: the session only crosses it."""
    a: str                       # node key (sorted pair)
    b: str
    kind: str                    # 'iBGP' | 'eBGP' | 'BGP' (AS of one end unknown)
    as_a: Any = None
    as_b: Any = None
    established: bool = False


def _short(name: str, n: int = 8) -> str:
    return name if len(name) <= n else name[:n - 1] + "…"


def bgp_segment_chip_text(sessions: list[SegBgpSession], label: dict[str, str]) -> str:
    """Chip on a switch carrying BGP sessions: 'iBGP R1↔R2', 'eBGP A↔B', several -> 'iBGP ×2' / 'BGP ×3'."""
    if not sessions:
        return "BGP"
    kinds = {s.kind for s in sessions}
    kind = kinds.pop() if len(kinds) == 1 else "BGP"
    if len(sessions) == 1:
        a, b = _pair_labels(sessions[0], label)
        return f"{kind} {_short(a)}↔{_short(b)}"
    return f"{kind} ×{len(sessions)}"


def _pair_labels(s: SegBgpSession, label: dict[str, str]) -> tuple[str, str]:
    """The two router names in label order (R1↔R2, not node-key order)."""
    a, b = label.get(s.a, s.a), label.get(s.b, s.b)
    return (a, b) if a <= b else (b, a)


def bgp_session_words(s: SegBgpSession, label: dict[str, str]) -> str:
    a, b = _pair_labels(s, label)
    flip = (a, b) != (label.get(s.a, s.a), label.get(s.b, s.b))
    as_a, as_b = (s.as_b, s.as_a) if flip else (s.as_a, s.as_b)
    if s.kind == "iBGP":
        asn = f" (AS {as_a})"
    elif s.kind == "eBGP":
        asn = f" (AS {as_a} ↔ AS {as_b})"
    else:
        asn = ""
    return f"{s.kind} {a} ↔ {b}{asn}: {'established' if s.established else 'not established'}"


def bgp_segment_sentence(sessions: list[SegBgpSession], label: dict[str, str]) -> str:
    """Plain words for the switch's BGP chip / inspector: the switch does not run BGP itself."""
    if not sessions:
        return ""
    if len(sessions) == 1:
        s = sessions[0]
        a, b = _pair_labels(s, label)
        return (f"{bgp_session_words(s, label)}. The switch itself doesn't run BGP; "
                f"the session between {a} and {b} crosses it.")
    return (f"{len(sessions)} BGP sessions cross this switch: "
            + "; ".join(bgp_session_words(s, label) for s in sessions)
            + ". The switch itself doesn't run BGP.")


@dataclass
class LinkStatus:
    kind: LinkKind
    side_node: str | None = None       # node key of the side the status is about
    side_iface: str | None = None      # short iface name
    cause: str = ""                    # 'shutdown' | 'no carrier' | 'line protocol down' | ...
    text: str = ""                     # short canvas label: 'R2 Gi0/4 shut'
    plain_text: str = ""               # jargon-free tooltip sentence
    api_certain: bool = False
    stale_sides: tuple[str, ...] = ()  # node keys whose facts were too old


@dataclass
class RouteHop:
    from_node: str
    to_node: str | None                # None: leaves the lab (egress) / unresolved
    link_keys: list[str]               # 1 (direct) or 2 (via a switch: from->SW, SW->to)
    via_ip: str | None
    out_iface: str | None
    code: str | None                   # the from_node's default route code
    ok: bool = True                    # every link on the hop has status UP


@dataclass
class DefaultPath:
    origin: str
    state: PathState
    hops: list[RouteHop] = field(default_factory=list)   # only the good (blue) hops + the failed one
    broken_at: str | None = None        # link key where blue stops (BROKEN_AT / STALE)
    broken_kind: LinkKind | None = None
    egress_node: str | None = None
    egress_mode: str | None = None      # 'NAT' | 'Bridge' | 'Cloud'
    loop_pair: tuple[str, str] | None = None
    detail: str = ""                    # plain words
    ecmp: bool = False


@dataclass
class LinkFacts:
    """Everything D3/D4/D5 derive for one canvas link."""
    key: str
    side_a: LinkSide
    side_b: LinkSide
    iface_a: IfFact | None
    iface_b: IfFact | None
    status: LinkStatus
    segment_id: str | None
    p2p: bool = False
    p2p_source: P2PSource | None = None
    p2p_conflict: str | None = None     # 'configured p2p, not active'
    bcast: bool = False                 # 2-router segment running OSPF broadcast (teaching contrast)
    bcast_detail: str | None = None     # 'DR R2 · BDR R1'
    subnet: str | None = None           # '10.0.13.0/30'
    subnet_hint: str | None = None      # '/30'
    addressing_mismatch: bool = False
    protocols: dict[Proto, Health] = field(default_factory=dict)   # only for 2-router segments
    adjacencies: list[tuple[str, str, Proto, str]] = field(default_factory=list)
    default_hop: RouteHop | None = None  # set when this link carries a fresh blue default hop
    default_direction: str | None = None  # 'a2b' | 'b2a'
    tooltip: str = ""
    # ---- L2 (managed-switch ends; empty when neither end is a managed switch)
    l2_a: "PortL2 | None" = None
    l2_b: "PortL2 | None" = None
    vlans: list[int] = field(default_factory=list)     # user VLANs this link carries
    trunk: bool = False                  # an 802.1Q trunk on the switch end(s)
    trunk_text: str | None = None        # '10,20' (allowed and active on both ends)
    native_vlan: int | None = None
    native_mismatch: bool = False
    access_vlan: int | None = None       # access port VLAN at the switch end
    access_side: str | None = None       # 'a' | 'b': the switch end that is the access port
    stp_block: "StpBlock | None" = None  # an end spanning tree is blocking
    addressing: "LinkAddressing | None" = None   # subnets on the cable (both ends, every source)
    # ---- EtherChannel (member cable of a bundle; TelemetryView.bundles[bundle])
    bundle: str | None = None            # Bundle.key
    ec_a: "EcMember | None" = None       # the a-end member state (flags, mode, partner)
    ec_b: "EcMember | None" = None


# ======================================================================= helpers
def topo_role(node_definition: str, backend: str = "cml", kind: str = "") -> NodeRole:
    if backend == "kvm":
        return NodeRole.ROUTER if kind in ROUTER_KVM_KINDS else NodeRole.HOST
    if node_definition in SWITCH_DEFS:
        return NodeRole.SWITCH
    if node_definition == "external_connector":
        return NodeRole.EGRESS
    if node_definition in PROBED_CML_DEFS:
        return NodeRole.ROUTER
    return NodeRole.HOST


def _subnet(ip: str | None, plen: int | None) -> str | None:
    if not ip or plen is None:
        return None
    try:
        return str(ipaddress.IPv4Interface(f"{ip}/{plen}").network)
    except ValueError:
        return None


def _in_subnet(ip: str, net: str | None) -> bool:
    if not net or not ip:
        return False
    try:
        return ipaddress.IPv4Address(ip) in ipaddress.IPv4Network(net)
    except ValueError:
        return False


# ======================================================================= IOS facts from parses
def ios_facts_update(nf: NodeFacts, parsed: dict[str, Any], ts: float,
                     ts_by_cmd: dict[str, float] | None = None) -> None:
    """Merge one burst's parse results (command -> ParseResult) into ``nf``.  Only commands
    that parsed ok replace their facts (a failed command leaves the old, ageing fact).
    ``ts_by_cmd`` gives the read time of each command (default: ``ts``)."""
    when = ts_by_cmd or {}

    def T(cmd):
        return when.get(cmd, ts)

    def ok(cmd):
        r = parsed.get(cmd)
        return r if (r is not None and r.ok) else None

    ib = ok("show ip interface brief")
    ob = ok("show ip ospf interface brief")
    old = nf.ifaces.value if nf.ifaces else {}
    if ib:
        ifs: dict[str, IfFact] = {}
        for row in ib.data:
            k = iface_key(row["iface"])
            prev = old.get(k)
            f = IfFact(name=row["iface"], short=row["short"], ip=row["ip"],
                       admin="down" if row["status"] == "admin-down" else "up",
                       oper=row["protocol"], phys=row["status"], method=row["method"])
            if prev:                                     # keep slow-tier attributes
                f.desc, f.mac, f.ospf_passive = prev.desc, prev.mac, prev.ospf_passive
                f.prefixlen = prev.prefixlen
            elif nf.ospf_passive_set is not None:        # SLOW ran before the first FAST
                f.ospf_passive = k in nf.ospf_passive_set
            ifs[k] = f
        nf.ifaces = Fact(ifs, T("show ip interface brief"))
        lo = ifs.get("loopback0")
        nf.loopback = lo.ip if lo else nf.loopback
    if ob and nf.ifaces:
        seen = set()
        for row in ob.data:
            k = iface_key(row["iface"])
            f = nf.ifaces.value.get(k)
            if f is None:
                continue
            seen.add(k)
            f.ospf_net, f.ospf_state = row["net_type"], row["state"]
            f.ospf_nbrs = (row["nbrs_full"], row["nbrs_cfg"])
            f.prefixlen = row["prefixlen"]
        for k, f in nf.ifaces.value.items():
            if k not in seen:
                f.ospf_net = f.ospf_state = None
                f.ospf_nbrs = None
        nf.ospf_running = Fact(bool(ob.data), T("show ip ospf interface brief"))
    ad = ok(IP_ADDR_CMD)
    if ad:
        t = T(IP_ADDR_CMD)
        nf.addrs = Fact({iface_key(r["iface"]): addr_from_parse(r, t) for r in ad.data}, t)
        nf.addr_live_ts = max(nf.addr_live_ts or 0.0, t)
        if nf.ifaces:
            for r in ad.data:
                f = nf.ifaces.value.get(iface_key(r["iface"]))
                if f is not None and r["prefixlen"] is not None and f.ip == r["ip"]:
                    f.prefixlen = r["prefixlen"]
        prune_overrides(nf)
    nb = ok("show ip ospf neighbor")
    if nb:
        nf.ospf_nbrs = Fact([OspfNbr(r["neighbor_id"], r["state"], r["adj_state"], r["role"], r["address"],
                                     r["iface"], r["priority"]) for r in nb.data], T("show ip ospf neighbor"))
    hs, vr = ok("show standby brief"), ok("show vrrp brief")
    if hs or vr:
        groups = []
        prev = nf.fhrp.value if nf.fhrp else []
        if hs:
            groups += [FhrpGroup(Proto.HSRP, r["iface"], r["group"], r["state"], r["priority"], r["preempt"],
                                 r["vip"], r["active"], r["standby"]) for r in hs.data]
        else:
            groups += [g for g in prev if g.proto == Proto.HSRP]
        if vr:
            groups += [FhrpGroup(Proto.VRRP, r["iface"], r["group"], r["state"], r["priority"], r["preempt"],
                                 r["vip"], r["master"], None) for r in vr.data]
        else:
            groups += [g for g in prev if g.proto == Proto.VRRP]
        nf.fhrp = Fact(groups, min(T("show standby brief"), T("show vrrp brief")))
    bg = ok("show ip bgp summary")
    if bg:
        d = bg.data
        nf.bgp = Fact(BgpInfo(d["active"], d["local_as"], d["rid"],
                              [BgpPeer(p["ip"], p["remote_as"], p["state"], p["established"], p["pfx_rcvd"],
                                       p["uptime"]) for p in d["peers"]]), T("show ip bgp summary"))
    rd, cef = ok("show ip route 0.0.0.0"), ok("show ip cef 0.0.0.0/0")
    if rd:
        nf.default_route = Fact(default_from_parse(rd.data, cef.data if cef else None, nf), T("show ip route 0.0.0.0"))
    pr = ok("show ip protocols")
    if pr:
        passive = set()
        rid = None
        for o in pr.data["ospf"]:
            passive |= {iface_key(i) for i in o["passive"]}
            rid = rid or o["rid"]
        nf.ospf_passive_set = passive if pr.data["ospf"] else None
        if nf.ifaces:
            for k, f in nf.ifaces.value.items():
                f.ospf_passive = (k in passive) if pr.data["ospf"] else None
        if rid:
            nf.rid = Fact(rid, T("show ip protocols"))
    if nf.rid is None and nf.bgp and nf.bgp.value.rid:
        nf.rid = Fact(nf.bgp.value.rid, nf.bgp.ts)
    de = ok("show interfaces description")
    if de and nf.ifaces:
        for row in de.data:
            f = nf.ifaces.value.get(iface_key(row["iface"]))
            if f:
                f.desc = row["desc"]
    rc = ok("show ip route connected")
    if rc:
        conn = {}
        for r in rc.data["routes"]:
            if r["proto"] == "C" and r["next_hops"] and r["next_hops"][0]["iface"]:
                conn[iface_key(r["next_hops"][0]["iface"])] = int(r["prefix"].split("/")[1])
        nf.connected = Fact(conn, T("show ip route connected"))
    if nf.connected and nf.ifaces:
        for k, plen in nf.connected.value.items():
            f = nf.ifaces.value.get(k)
            if f and f.prefixlen is None:
                f.prefixlen = plen
    if nf.default_route and nf.ifaces and nf.default_route.value.code == "S*" and not nf.default_route.value.via_dhcp:
        dr = nf.default_route.value
        if dr.distance == 254 or any(f.method == "DHCP" for f in nf.ifaces.value.values()):
            dr.via_dhcp = dr.distance == 254
            if dr.via_dhcp:
                dr.source_label = "S* dhcp"


def default_from_parse(rd: dict, cef: dict | None, nf: NodeFacts | None = None) -> DefaultRoute:
    """ios.parse_ip_route_default (+ optional ios.parse_ip_cef_default) -> DefaultRoute."""
    if not rd.get("present"):
        return DefaultRoute(False)
    code = rd.get("code")
    label = code
    if rd.get("proto") == "bgp":
        label = f"{code} {'eBGP' if rd.get('ebgp') else 'iBGP'}" if rd.get("ebgp") is not None else code
    via_dhcp = rd.get("proto") == "static" and rd.get("distance") == 254
    if via_dhcp:
        label = f"{code} dhcp"
    hops = [(h.get("via"), h.get("iface")) for h in rd.get("next_hops", [])]
    blackhole = bool(rd.get("blackhole")) or any(i == "Null0" for _, i in hops)
    checked = False
    if cef and cef.get("present"):
        if cef.get("kind") == "drop":
            blackhole = True
            hops = []
            checked = True
        elif cef.get("next_hops"):
            hops = [(h["via"], h["iface"]) for h in cef["next_hops"]]
            checked = True
    return DefaultRoute(True, code, label, rd.get("proto"), rd.get("distance"), hops, via_dhcp, blackhole,
                        rd.get("from_ip"), checked)


def junos_facts_update(nf: NodeFacts, parsed: dict[str, Any], ts: float) -> None:
    """Merge Junos (display json) parse results; command names per telemetry.JUNOS_COMMANDS."""
    def ok(cmd):
        r = parsed.get(cmd)
        return r if (r is not None and r.ok) else None

    t = ok("show interfaces terse | display json")
    if t:
        nf.ifaces = Fact({iface_key(r["iface"]): IfFact(r["iface"], r["iface"], r["ip"], r["prefixlen"],
                                                        (r["admin"] or "").lower() or None,
                                                        (r["oper"] or "").lower() or None, r["status"])
                          for r in t.data}, ts)
    oi = ok("show ospf interface detail | display json")
    if oi and nf.ifaces:
        for r in oi.data:
            f = nf.ifaces.value.get(iface_key(r["iface"]))
            if f:
                f.ospf_net, f.ospf_state, f.ospf_passive = r["net_type"], r["state"], r["passive"]
        nf.ospf_running = Fact(bool(oi.data), ts)
    n = ok("show ospf neighbor | display json")
    if n:
        nf.ospf_nbrs = Fact([OspfNbr(r["neighbor_id"], r["state"], r["adj_state"], r["role"], r["address"],
                                     r["iface"], r["priority"]) for r in n.data], ts)
    b = ok("show bgp summary | display json")
    if b:
        d = b.data
        nf.bgp = Fact(BgpInfo(d["active"], d["local_as"], d["rid"],
                              [BgpPeer(p["ip"], p["remote_as"], p["state"], p["established"], p["pfx_rcvd"],
                                       p["uptime"]) for p in d["peers"]]), ts)
    v = ok("show vrrp summary | display json")
    if v:
        nf.fhrp = Fact([FhrpGroup(Proto.VRRP, r["iface"], r["group"], r["state"], r["priority"], r["preempt"],
                                  r["vip"]) for r in v.data], ts)
    d = ok("show route 0.0.0.0/0 exact | display json")
    if d:
        rd = d.data
        nf.default_route = Fact(DefaultRoute(rd["present"], rd.get("code"), rd.get("code"), rd.get("proto"),
                                             rd.get("distance"),
                                             [(h.get("via"), h.get("iface")) for h in rd.get("next_hops", [])],
                                             False, bool(rd.get("blackhole"))) if rd["present"]
                                else DefaultRoute(False), ts)


def fortios_facts_update(nf: NodeFacts, parsed: dict[str, Any], ts: float) -> None:
    def ok(cmd):
        r = parsed.get(cmd)
        return r if (r is not None and r.ok) else None

    p = ok("get system interface physical")
    if p:
        nf.ifaces = Fact({iface_key(r["iface"]): IfFact(r["iface"], r["iface"], r["ip"], r["prefixlen"], None,
                                                        r["status"] if r["status"] in ("up", "down") else None,
                                                        r["status"]) for r in p.data}, ts)
    oi = ok("get router info ospf interface")
    if oi and nf.ifaces:
        for r in oi.data:
            f = nf.ifaces.value.get(iface_key(r["iface"]))
            if f:
                f.ospf_net, f.ospf_state, f.ospf_passive = r["net_type"], r["state"], r["passive"]
        nf.ospf_running = Fact(bool(oi.data), ts)
    n = ok("get router info ospf neighbor")
    if n:
        nf.ospf_nbrs = Fact([OspfNbr(r["neighbor_id"], r["state"], r["adj_state"], r["role"], r["address"],
                                     r["iface"], r["priority"]) for r in n.data], ts)
    b = ok("get router info bgp summary")
    if b:
        d = b.data
        nf.bgp = Fact(BgpInfo(d["active"], d["local_as"], d["rid"],
                              [BgpPeer(x["ip"], x["remote_as"], x["state"], x["established"], x["pfx_rcvd"],
                                       x["uptime"]) for x in d["peers"]]), ts)
    v = ok("get router info vrrp")
    if v:
        nf.fhrp = Fact([FhrpGroup(Proto.VRRP, r["iface"], r["group"], r["state"], r["priority"], r["preempt"],
                                  r["vip"]) for r in v.data], ts)
    d = ok("get router info routing-table details 0.0.0.0/0")
    if d:
        rd = d.data
        nf.default_route = Fact(DefaultRoute(rd["present"], rd.get("code"), rd.get("code"), rd.get("proto"),
                                             rd.get("distance"),
                                             [(h.get("via"), h.get("iface")) for h in rd.get("next_hops", [])],
                                             False, bool(rd.get("blackhole"))) if rd["present"]
                                else DefaultRoute(False), ts)


# ======================================================================= L2 facts from parses
L2_CMDS = {
    "vlans": "show vlan brief",
    "trunks": "show interfaces trunk",
    "ports": "show interfaces status",
    "stp": "show spanning-tree",
    "summary": "show spanning-tree summary",
    "mac_count": "show mac address-table count",
    "mac": "show mac address-table dynamic",
    "cdp": "show cdp neighbors",
    "etherchannel": "show etherchannel summary",
    "tc": "show spanning-tree detail | include ^ VLAN|occurr|from",
    "routing": "#routing",            # pseudo: parse_routing_enabled over 'show ip route connected'
}


def l2_facts_update(nf: NodeFacts, parsed: dict[str, Any], ts: float,
                    ts_by_cmd: dict[str, float] | None = None) -> None:
    """Merge the L2 command set (parsers/ios_l2.py shapes, keyed by L2_CMDS values) into ``nf.l2``.
    Only commands that parsed ok replace their fact."""
    when = ts_by_cmd or {}

    def T(cmd):
        return when.get(cmd, when.get("show ip route connected", ts) if cmd == "#routing" else ts)

    def ok(key):
        r = parsed.get(L2_CMDS[key])
        return r if (r is not None and r.ok) else None

    l2 = nf.l2 = nf.l2 or L2Facts()
    if (r := ok("vlans")):
        l2.vlans = Fact({v["id"]: VlanInfo(v["id"], v["name"], v["status"], list(v["ports"])) for v in r.data},
                        T(L2_CMDS["vlans"]))
    if (r := ok("trunks")):
        l2.trunks = Fact({iface_key(t["iface"]): TrunkInfo(t["port"], t["native"], t["allowed"], t["allowed_text"],
                                                           t["active"], t["active_text"], t["forwarding"],
                                                           t["forwarding_text"], t["status"], t["encap"], t["mode"])
                          for t in r.data.values()}, T(L2_CMDS["trunks"]))
    if (r := ok("ports")):
        l2.ports = Fact({iface_key(x["iface"]): SwitchPortInfo(x["port"], x["iface"], x["status"], x["vlan"], x["name"],
                                                               x.get("duplex"), x.get("speed"))
                         for x in r.data}, T(L2_CMDS["ports"]))
    if (r := ok("stp")):
        stp = {}
        for vid, v in r.data.items():
            ports = {}
            for pt in v["ports"]:
                try:
                    role, state = PortRole(pt["role"]), PortState(pt["state"])
                except ValueError:
                    continue
                ports[iface_key(pt["iface"])] = StpPortInfo(pt["short"], role, state, pt["cost"], pt["prio"], pt["nbr"],
                                                            pt["edge"], pt["p2p"], pt["inconsistent"])
            stp[vid] = StpVlanInfo(vid, v["protocol"], v["root_priority"], v["root_address"], v["root_cost"],
                                   v["root_port"], v["is_root"], v["bridge_priority"], v["bridge_address"], ports)
        l2.stp = Fact(stp, T(L2_CMDS["stp"]))
    if (r := ok("summary")):
        if r.data.get("mode"):
            l2.stp_mode = Fact(r.data["mode"], T(L2_CMDS["summary"]))
        l2.root_for = Fact(list(r.data.get("root_for") or []), T(L2_CMDS["summary"]))
    if (r := ok("tc")):
        l2.tc = Fact({v: TcInfo(d["changes"], d["last_ago_s"], d["from_iface"]) for v, d in r.data.items()},
                     T(L2_CMDS["tc"]))
    if (r := ok("mac_count")):
        l2.mac_counts = Fact({v: d["total"] for v, d in r.data["vlans"].items()}, T(L2_CMDS["mac_count"]))
    if (r := ok("mac")):
        l2.mac_table = Fact(list(r.data), T(L2_CMDS["mac"]))
    if (r := ok("cdp")):
        l2.cdp = Fact(list(r.data), T(L2_CMDS["cdp"]))
    if (r := ok("etherchannel")):
        l2.etherchannel = Fact(dict(r.data), T(L2_CMDS["etherchannel"]))
    if (r := ok("routing")):
        l2.routing = Fact(bool(r.data["routing"]), T("#routing"))


EC_CMDS = {
    "summary": "show etherchannel summary",
    "lacp_internal": "show lacp internal",
    "lacp_neighbor": "show lacp neighbor",
    "lacp_sys": "show lacp sys-id",
    "pagp_internal": "show pagp internal",
    "pagp_neighbor": "show pagp neighbor",
    "load_balance": "show etherchannel load-balance",
}


def ec_facts_update(nf: NodeFacts, parsed: dict[str, Any], ts: float,
                    ts_by_cmd: dict[str, float] | None = None) -> None:
    """Merge the EtherChannel command set (EC_CMDS values -> ParseResult) into ``nf.ec``.  The summary is
    also mirrored into ``nf.l2.etherchannel`` (older readers).  Only commands that parsed ok replace facts."""
    when = ts_by_cmd or {}
    got = {k: parsed.get(c) for k, c in EC_CMDS.items()}
    if not any(r is not None and r.ok for r in got.values()):
        return
    ec = nf.ec = nf.ec or EcFacts()
    for k, r in got.items():
        if r is not None and r.ok:
            setattr(ec, k, Fact(r.data if not isinstance(r.data, dict) else dict(r.data), when.get(EC_CMDS[k], ts)))
    if ec.summary is not None and nf.l2 is not None:
        nf.l2.etherchannel = ec.summary


@dataclass
class PortL2:
    """One switch end of a link, as spanning tree and the VLAN config see it."""
    node: str
    port: str                                  # short
    mode: PortMode
    status: str | None = None                  # 'connected' | 'err-disabled' | ...
    access_vlan: int | None = None
    native: int | None = None
    trunk_text: str | None = None              # VLANs carried ('allowed and active'): '10,20'
    allowed_text: str | None = None
    vlans: list[int] = field(default_factory=list)          # user VLANs this port carries
    stp: dict[int, tuple[PortRole, PortState]] = field(default_factory=dict)   # VLAN -> (role, state)
    po: str | None = None                      # 'Po1' when this port is a bundled EtherChannel member: trunk /
    #                                            VLANs / STP above are the Port-channel's (STP runs on the Po)
    ec_flags: str | None = None                # member flags ('P' bundled, 'I', 's', 'D', 'H', 'w')

    @property
    def blocked_vlans(self) -> list[int]:
        return sorted(v for v, (_, st) in self.stp.items() if st == PortState.BLK)

    @property
    def forwarding_vlans(self) -> list[int]:
        return sorted(v for v, (_, st) in self.stp.items() if st == PortState.FWD)

    @property
    def err_disabled(self) -> bool:
        return self.status == "err-disabled"


@dataclass
class StpBlock:
    """V-STP: a link end that spanning tree keeps blocking (the cable itself is fine)."""
    side: str                    # 'a' | 'b'
    node: str
    port: str                    # short
    role: PortRole               # ALTN | BACK (or the role reported while blocking)
    vlans: list[int]             # VLANs blocked on this port
    partial: bool = False        # other VLANs on the same port still forward

    @property
    def role_word(self) -> str:
        return ROLE_WORDS.get(self.role, self.role.value)


def _vlan_list_text(vlans) -> str:
    from .parsers.ios_l2 import vlan_text
    return vlan_text(vlans)


def port_l2(nk: str, nf: NodeFacts | None, iface_label: str, now: float, fresh_s: float) -> PortL2 | None:
    """The L2 view of one switch port (None if the node is not a managed switch with fresh L2 facts)."""
    if nf is None or nf.l2 is None:
        return None
    l2 = nf.l2
    if l2.ports is None or not l2.ports.fresh(now, fresh_s):
        return None
    sp = l2.port(iface_label)
    tr = l2.trunk(iface_label)
    short = short_iface(iface_label)
    if sp is None and tr is None:
        return None
    # EtherChannel member: spanning tree and the trunk run on the Port-channel, not on the member port
    po_key = None
    flags = None
    ecg = nf.ec.group_of(iface_label) if nf.ec is not None and nf.ec.summary is not None else None
    if ecg is not None:
        flags = ecg[1]["flags"]
        if "P" in flags:
            po_key = iface_key(ecg[0]["port_channel"])
            tr = l2.trunks.value.get(po_key) if l2.trunks else None
    mode = PortMode.TRUNK if tr is not None else (sp.mode if sp else PortMode.UNKNOWN)
    pl = PortL2(nk, short, mode, sp.status if sp else None)
    if ecg is not None:
        pl.ec_flags = flags
        pl.po = ecg[0]["port_channel"] if po_key else None
    user = set(l2.user_vlans())
    if mode == PortMode.ACCESS and sp is not None:
        pl.access_vlan = sp.vlan
        pl.vlans = [sp.vlan]
    elif tr is not None:
        from .parsers.ios_l2 import in_ranges
        pl.native, pl.trunk_text, pl.allowed_text = tr.native, tr.active_text, tr.allowed_text
        pl.vlans = sorted(v for v in user if in_ranges(v, tr.active)) if user else \
            sorted({v for lo, hi in tr.active if hi - lo < 64 for v in range(lo, hi + 1)})
    if l2.stp is not None and l2.stp.fresh(now, fresh_s):
        k = po_key or iface_key(iface_label)
        for vid, sv in l2.stp.value.items():
            pt = sv.ports.get(k)
            if pt is not None:
                pl.stp[vid] = (pt.role, pt.state)
    return pl


def _link_l2(lf: LinkFacts, l: TopoLink, topo: Topology, facts: dict[str, NodeFacts], now: float,
             fresh_s: float) -> None:
    for side, nk, ifl in (("a", l.a, l.iface_a), ("b", l.b, l.iface_b)):
        n = topo.nodes.get(nk)
        if n is None or not (n.role == NodeRole.SWITCH and n.managed):
            continue
        pl = port_l2(nk, facts.get(nk), ifl, now, fresh_s)
        setattr(lf, f"l2_{side}", pl)
    ends = [p for p in (lf.l2_a, lf.l2_b) if p is not None]
    if not ends:
        return
    trunks = [p for p in ends if p.mode == PortMode.TRUNK]
    if trunks:
        lf.trunk = True
        sets = [set(p.vlans) for p in trunks]
        carried = set.intersection(*sets) if len(sets) > 1 else sets[0]
        lf.vlans = sorted(carried)
        lf.trunk_text = _vlan_list_text(lf.vlans) if lf.vlans else (trunks[0].trunk_text or "none")
        natives = {p.native for p in trunks if p.native is not None}
        lf.native_vlan = natives.pop() if len(natives) == 1 else None
        lf.native_mismatch = len({p.native for p in trunks if p.native is not None}) > 1
    else:
        acc = next((p for p in ends if p.mode == PortMode.ACCESS), None)
        if acc is not None:
            lf.access_vlan = acc.access_vlan
            lf.access_side = "a" if acc is lf.l2_a else "b"
            lf.vlans = [acc.access_vlan] if acc.access_vlan is not None else []
    for side, p in (("a", lf.l2_a), ("b", lf.l2_b)):
        if p is None:
            continue
        blocked = p.blocked_vlans
        if blocked and (lf.stp_block is None):
            roles = [p.stp[v][0] for v in blocked]
            role = next((r for r in roles if r in (PortRole.ALTN, PortRole.BACK)), roles[0])
            lf.stp_block = StpBlock(side, p.node, p.port, role, blocked, partial=bool(p.forwarding_vlans))


def _stp_tooltip(lf: LinkFacts, label: dict[str, str]) -> str:
    b = lf.stp_block
    if b is None:
        return ""
    vl = _vlan_list_text(b.vlans)
    if b.role not in (PortRole.ALTN, PortRole.BACK):
        return (f"spanning tree: {label.get(b.node, '?')} {b.port} ({b.role_word}) is discarding for VLAN {vl} "
                "while RSTP converges")
    if b.partial:
        return (f"spanning tree: {label.get(b.node, '?')} {b.port} is {b.role_word.lower()} and BLOCKING for "
                f"VLAN {vl} (forwards the others) - a redundant path kept in reserve")
    return (f"spanning tree: {label.get(b.node, '?')} {b.port} is an {b.role_word} port, BLOCKING "
            f"(VLAN {vl}) to prevent a loop - the cable is fine, it is a backup path")


# ======================================================================= segments
def build_segments(topo: Topology) -> tuple[dict[str, Segment], dict[str, str]]:
    """Group links into L2 segments.  Returns (segments by id, link_key -> segment id)."""
    parent: dict[str, str] = {}

    def find(x):
        while parent.setdefault(x, x) != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        parent[find(a)] = find(b)

    for lk, l in topo.links.items():
        find("L:" + lk)
        for end in (l.a, l.b):
            n = topo.nodes.get(end)
            if n and n.role == NodeRole.SWITCH:
                union("L:" + lk, "S:" + end)
    groups: dict[str, list[str]] = {}
    for lk in topo.links:
        groups.setdefault(find("L:" + lk), []).append(lk)
    segs: dict[str, Segment] = {}
    link_seg: dict[str, str] = {}
    for root, lks in groups.items():
        lks.sort()
        members, switches = [], []
        for lk in lks:
            l = topo.links[lk]
            for end, ifl in ((l.a, l.iface_a), (l.b, l.iface_b)):
                n = topo.nodes.get(end)
                if n is None:
                    continue
                if n.role == NodeRole.SWITCH:
                    if end not in switches:
                        switches.append(end)
                elif (end, ifl) not in members:
                    members.append((end, ifl))
        roles = {topo.nodes[m[0]].role for m in members if m[0] in topo.nodes}
        if NodeRole.EGRESS in roles:
            kind = SegmentKind.EGRESS
        elif switches:
            kind = SegmentKind.MULTI
        elif len(members) == 2 and roles == {NodeRole.ROUTER}:
            kind = SegmentKind.P2P
        else:
            kind = SegmentKind.OTHER
        sid = "seg:" + (switches[0] if switches else lks[0])
        segs[sid] = Segment(sid, kind, switches[0] if switches else None, members, lks)
        for lk in lks:
            link_seg[lk] = sid
    return segs, link_seg


# ======================================================================= subnets on cables
@dataclass
class EndAddr:
    """One cable end as an IP endpoint: the interface and its subinterfaces (router-on-a-stick)."""
    node: str
    label: str                     # node label
    iface: str                     # short name of the cabled interface
    l3: bool                       # a router port / routed switch port (an L2 switchport or a host is not)
    addrs: list[tuple[str, str, int, bool]] = field(default_factory=list)   # (iface short, ip, plen, secondary)
    method: str = "none"           # of the cabled interface (or 'static' when only subinterfaces carry IPs)
    unnumbered: str | None = None
    sources: set[str] = field(default_factory=set)
    ts: float | None = None        # oldest read time of the addressing used
    live_ts: float | None = None   # last time any address source of this node was read
    role: str = "router"           # NodeRole value of the node

    @property
    def configured(self) -> bool:
        return bool(self.addrs) or self.method in ("dhcp", "unnumbered", "negotiated")

    @property
    def networks(self) -> list[str]:
        return self.networks_with([])

    def networks_with(self, other_nets: list[str]) -> list[str]:
        """Networks of this end; an address without a known prefix length takes the other end's network
        when it falls inside it, else shows as 'ip/?'."""
        out = []
        for _, ip, pl, _ in self.addrs:
            if pl is not None:
                n = _subnet(ip, pl)
            else:
                n = next((on for on in other_nets if _in_subnet(ip, on)), f"{ip}/?")
            if n and n not in out:
                out.append(n)
        return out

    def host_for(self, net: str | None) -> str:
        """'.1' for the address in ``net`` (else the first address); '.1…' when the subinterfaces differ."""
        if not self.addrs:
            return ""

        def hp(ip, pl):
            if pl is None and net and "/?" not in net and _in_subnet(ip, net):
                pl = int(net.split("/")[1])
            return host_part(ip, pl if pl is not None else 24)
        hosts = []
        for _, ip, pl, _ in self.addrs:
            h = hp(ip, pl)
            if h and h not in hosts:
                hosts.append(h)
        first = next((hp(ip, pl) for _, ip, pl, _ in self.addrs if net and "/?" not in net and _in_subnet(ip, net)),
                     None)
        first = first or (hosts[0] if hosts else "")
        return first + ("…" if len(hosts) > 1 else "")

    def lines(self) -> list[str]:
        if not self.l3 and not self.addrs:
            what = {"switch": "switchport (no IP on the cable)", "egress": "external connector",
                    "host": "host (not probed: address not read)"}.get(self.role, "not read yet")
            return [f"{self.label} {self.iface}: {what}"]
        if not self.configured:
            return [f"{self.label} {self.iface}: no IP address"]
        if self.method == "unnumbered":
            return [f"{self.label} {self.iface}: ip unnumbered {short_iface(self.unnumbered or '?')}"]
        out = []
        for ifn, ip, pl, sec in self.addrs:
            out.append(f"{self.label} {ifn} {ip}/{pl if pl is not None else '?'}" + (" secondary" if sec else "")
                       + (" (DHCP)" if self.method == "dhcp" and not sec else ""))
        if not self.addrs and self.method == "dhcp":
            out.append(f"{self.label} {self.iface}: DHCP, no lease yet")
        return out


@dataclass
class LinkAddressing:
    state: SubnetState
    nets: list[str] = field(default_factory=list)      # every network on the cable, shown one first
    shared: list[str] = field(default_factory=list)    # networks both L3 ends have
    label: str = ""                # canvas text: '10.0.12.0/30', '192.168.10.0/24 +1', '⚠ mismatch a ≠ b'
    compact: str = ""              # zoomed-out text
    host_a: str = ""               # '.1' (next to the a-end interface name)
    host_b: str = ""
    a: EndAddr | None = None
    b: EndAddr | None = None
    configured_side: str | None = None   # ONE_SIDED: 'a' | 'b'
    tooltip: str = ""              # multi-line plain text
    explain: str = ""              # one sentence for the Inspector
    source: str = ""               # 'cli' | 'console' | 'config' | 'cli+console'
    age_s: float | None = None
    stale: bool = False

    @property
    def extra(self) -> int:
        return max(0, len(self.nets) - 1)


ADDR_STALE_FACTOR = 4               # addressing older than 4 x FRESH_S (no read from any source) is faded


def _end_addr(nk: str, ifl: str, topo: Topology, facts: dict[str, NodeFacts]) -> EndAddr | None:
    n = topo.nodes.get(nk)
    if n is None:
        return None
    nf = facts.get(nk)
    l3 = n.collected and n.role == NodeRole.ROUTER
    if n.collected and n.role == NodeRole.SWITCH and nf is not None and nf.l2 is not None:
        sp = nf.l2.port(ifl)
        l3 = sp is not None and sp.mode == PortMode.ROUTED
    e = EndAddr(nk, n.label, short_iface(ifl), l3, role=n.role.value)
    if nf is None:
        return e
    k = iface_key(ifl)
    eff = effective_addrs(nf)
    own = eff.get(k)
    subs = sorted((kk for kk in eff if kk.startswith(k + ".")), key=lambda x: [int(t) if t.isdigit() else t
                                                                             for t in re.split(r"(\d+)", x)])
    used = [x for x in ([own] if own else []) + [eff[kk] for kk in subs] if x is not None]
    if own is not None:
        e.method, e.unnumbered = own.method, own.unnumbered
    for kk in ([k] if own else []) + subs:
        a = eff[kk]
        name = short_iface(nf.ifaces.value[kk].name) if nf.ifaces and kk in nf.ifaces.value else \
            short_iface(expand_iface(kk))
        for ip, pl, sec in a.addresses:
            e.addrs.append((name, ip, pl, sec))
    if e.addrs and e.method in ("none",):
        e.method = "static"
    if used:
        e.sources = {a.source for a in used}
        e.ts = min(a.ts for a in used)
    e.live_ts = nf.addr_live_ts or (nf.ifaces.ts if nf.ifaces else None)
    return e


def link_addressing(link: TopoLink, topo: Topology, facts: dict[str, NodeFacts], now: float,
                    fresh_s: float) -> LinkAddressing:
    """Pure: the subnet(s) a cable carries, from the live interface facts of both ends."""
    a = _end_addr(link.a, link.iface_a, topo, facts)
    b = _end_addr(link.b, link.iface_b, topo, facts)
    la = LinkAddressing(SubnetState.NONE, a=a, b=b)
    ends = [(s, e) for s, e in (("a", a), ("b", b)) if e is not None]
    l3 = [(s, e) for s, e in ends if e.l3]
    conf = [(s, e) for s, e in ends if e.configured and (e.l3 or e.addrs)]
    srcs = sorted({x for _, e in ends if e.l3 or e.addrs for x in e.sources},
                  key=lambda x: ADDR_SOURCES.index(x) if x in ADDR_SOURCES else 9)
    la.source = "+".join(srcs)
    tss = [e.ts for _, e in conf if e.ts is not None]
    lives = [e.live_ts for _, e in conf if e.live_ts is not None]
    if lives:
        la.age_s = max(0.0, now - min(lives))
        la.stale = la.age_s > ADDR_STALE_FACTOR * fresh_s
    elif tss:
        la.age_s = max(0.0, now - min(tss))
    names = {s: f"{e.label} {e.iface}" for s, e in ends}
    detail = [ln for _, e in ends for ln in e.lines()]
    if not conf:
        return la
    known = {s: e.networks for s, e in conf}
    nets_by = {s: e.networks_with([n for s2, ns in known.items() if s2 != s for n in ns]) for s, e in conf}
    all_nets: list[str] = []
    for s, _ in conf:
        for n in nets_by[s]:
            if n not in all_nets:
                all_nets.append(n)
    la.nets = all_nets
    if len(conf) == 2:
        sa, sb = nets_by["a"], nets_by["b"]
        la.shared = [n for n in sa if n in sb]
    show = la.shared[0] if la.shared else (all_nets[0] if all_nets else None)
    if show and la.shared:
        la.nets = [show] + [n for n in all_nets if n != show]
    la.host_a = a.host_for(show) if a is not None and a.addrs else ""
    la.host_b = b.host_for(show) if b is not None and b.addrs else ""
    methods = {s: e.method for s, e in conf}
    plus = f" +{len(la.nets) - 1}" if len(la.nets) > 1 else ""
    head = ""
    if len(conf) == 2 and not la.shared and sa and sb:
        la.state = SubnetState.MISMATCH
        la.label = f"⚠ mismatch {sa[0]} ≠ {sb[0]}"
        la.compact = "⚠ mismatch"
        head = (f"⚠ addressing mismatch: {names['a']} is in {', '.join(sa)} but {names['b']} is in "
                f"{', '.join(sb)}")
        la.explain = (f"The two ends are in different subnets ({sa[0]} on {names['a']}, {sb[0]} on {names['b']}): "
                      "they cannot reach each other over this cable (no ARP, no OSPF/BGP neighbour, no ping). "
                      "Fix one side's ip address.")
    elif all(m == "unnumbered" for m in methods.values()) or (not all_nets and "unnumbered" in methods.values()):
        la.state, la.label, la.compact = SubnetState.UNNUMBERED, "unnumbered", "unnum."
        src = next(e for _, e in conf if e.method == "unnumbered")
        head = f"ip unnumbered: {names[next(s for s, e in conf if e is src)]} borrows " \
               f"{short_iface(src.unnumbered or '?')}'s address"
        la.explain = head + "."
    elif not all_nets and "dhcp" in methods.values():
        la.state, la.label, la.compact = SubnetState.DHCP, "dhcp", "dhcp"
        head = "DHCP client, no lease yet"
        la.explain = head + "."
    elif show is not None:
        dhcp_side = next((s for s, m in methods.items() if m == "dhcp"), None)
        if dhcp_side is not None and len(conf) == 1:
            la.state = SubnetState.DHCP
            la.label = f"dhcp {dhcp_net(show)}" + plus
            la.compact = "dhcp"
            de = dict(conf)[dhcp_side]
            got = next((f"{ip}/{pl}" for _, ip, pl, _ in de.addrs), show)
            head = f"{show} (DHCP)"
            la.explain = f"{names[dhcp_side]} got {got} by DHCP."
        else:
            la.state = SubnetState.MATCH
            la.label = show + plus
            la.compact = compact_net(show) + ("+" if plus else "")
        if la.state == SubnetState.DHCP:
            pass
        elif len(l3) == 2 and len(conf) == 1:
            la.state = SubnetState.ONE_SIDED
            la.configured_side = conf[0][0]
            other = "b" if la.configured_side == "a" else "a"
            head = f"{show}: only {conf[0][1].label} side configured ({names[other]} has no IP address)"
            la.explain = (f"Only {names[la.configured_side]} has an address ({show}); configure {names[other]} "
                          "in the same subnet for the two to talk.")
        else:
            head = show + (f" (+ {', '.join(la.nets[1:])})" if plus else "")
            if len(conf) == 2:
                only = [n for n in all_nets if n not in la.shared]
                la.explain = f"Both ends are in {show}." + (f" Only on one end: {', '.join(only)}." if only else "")
                if only:
                    head += f"; only on one end: {', '.join(only)}"
            else:
                la.explain = f"{names[conf[0][0]]} is in {show}" + \
                    (f" (and {', '.join(la.nets[1:])})" if plus else "") + "."
    else:
        return la
    src_words = {"cli": "CLI probe", "console": "typed on the console (CML console log)",
                 "config": "running-config"}
    when = f", read {int(la.age_s)} s ago" if la.age_s is not None else ""
    tail = f"source: {', '.join(src_words.get(x, x) for x in srcs)}{when}" + (" (stale)" if la.stale else "")
    la.tooltip = "\n".join([head] + detail + [tail])
    return la


# ======================================================================= EtherChannel bundles
EC_STALE_FACTOR = 4                 # EtherChannel tables older than 4 x FRESH_S are not used at all
EC_DETAIL_FACTOR = 5                # LACP / PAgP detail tables (SLOW tier) are trusted up to 5 x FRESH_S
EC_PROBLEM_TTL = 300.0              # a %EC-5-CANNOT_BUNDLE reason is shown this long (while the port is not bundled)


@dataclass
class EcMember:
    """One member cable of a bundle, as seen from one side."""
    port: str                        # short ('Gi0/1')
    flags: str                       # summary flags: 'P' bundled, 'I' stand-alone, 's' suspended, 'H', 'D', 'w'
    words: str                       # 'bundled' | 'stand-alone' | 'suspended' | 'down' | 'hot-standby' | ...
    bundled: bool
    mode: str | None = None          # active | passive | desirable | auto | on (None: no channel-group / not read)
    link_key: str | None = None      # the canvas cable
    peer_node: str | None = None     # far end of the cable (CML topology)
    peer_port: str | None = None
    partner_node: str | None = None  # far end as the protocol reports it (LACP system id / PAgP name / CDP)
    partner_port: str | None = None
    partner_source: str | None = None   # 'lacp' | 'pagp' | 'cdp' | None
    partner_dev: str | None = None   # LACP system MAC / PAgP device id
    partner_mode: str | None = None  # LACP partner active/passive (from the flags)
    port_priority: int | None = None
    partner_priority: int | None = None
    lacp_state: str | None = None    # bndl | indep | susp | hot-sby | down
    speed: str | None = None         # show interfaces status
    duplex: str | None = None
    vlan: Any = None                 # 'trunk' | access VLAN id
    problem: str | None = None       # %EC-5-CANNOT_BUNDLE reason ('vlan mask is different')

    @property
    def flag_words(self) -> str:
        from .parsers.ios_l2 import EC_PORT_FLAGS
        return ", ".join(EC_PORT_FLAGS.get(c, c) for c in self.flags) or "-"


@dataclass
class EcSide:
    """One switch's Port-channel of a bundle."""
    node: str
    label: str
    group: int | None                # channel-group number (None: the ports have no channel-group)
    po: str | None                   # 'Po1'
    proto: EcProto
    flags: str = ""                  # 'SU' | 'SD' | 'RU' ...
    up: bool = False
    members: list[EcMember] = field(default_factory=list)
    sys_priority: int | None = None  # LACP system priority
    sys_mac: str | None = None       # LACP system id MAC
    load_balance: str | None = None  # 'src-dst-ip'
    stp: dict[int, tuple[PortRole, PortState]] = field(default_factory=dict)   # the Port-channel's STP (per VLAN)
    trunk_text: str | None = None    # VLANs carried by the Port-channel trunk
    native: int | None = None
    access_vlan: int | None = None
    mode_l2: PortMode = PortMode.UNKNOWN
    age_s: float | None = None

    @property
    def bundled(self) -> int:
        return sum(1 for m in self.members if m.bundled)

    @property
    def modes(self) -> list[str]:
        return sorted({m.mode for m in self.members if m.mode})

    @property
    def flag_words(self) -> str:
        from .parsers.ios_l2 import EC_PO_FLAGS
        return ", ".join(EC_PO_FLAGS.get(c, c) for c in self.flags) or "-"

    @property
    def stp_text(self) -> str:
        grp: dict[str, list[int]] = {}
        for v, (r, st) in sorted(self.stp.items()):
            grp.setdefault(f"{ROLE_WORDS.get(r, r.value)} {STATE_WORDS.get(st, st.value)}", []).append(v)
        return " · ".join(f"{k} (VLAN {_vlan_list_text(v)})" for k, v in grp.items()) if grp else ""


@dataclass
class Bundle:
    """An EtherChannel between two devices: the parallel member cables + both Port-channels."""
    key: str                         # 'ec:<node a>/<Po>|<node b>/<Po>'
    a: str                           # node keys (a = side_a's node)
    b: str
    side_a: EcSide | None
    side_b: EcSide | None
    link_keys: list[str]             # member cables on the canvas
    protocol: str = ""               # 'LACP' | 'PAgP' | 'on' | 'LACP≠PAgP' ...
    bundled: int = 0                 # member cables bundled on every configured side
    total: int = 0
    state: EcState = EcState.UNKNOWN
    label: str = ""                  # 'Po1 · LACP 2/2'
    po_text: str = ""                # 'Po1' | 'Po1/Po3' (different numbers per side)
    mismatch: list[str] = field(default_factory=list)      # plain-English sentences
    mismatch_short: str | None = None                       # 'LACP passive ↔ passive — never bundles'
    problems: list[str] = field(default_factory=list)       # partial / member problems (plain English)
    stp_block: "StpBlock | None" = None                     # the Port-channel spanning tree blocks (whole bundle)
    trunk_text: str | None = None
    native_vlan: int | None = None
    vlans: list[int] = field(default_factory=list)
    load_balance: str | None = None
    tooltip: str = ""
    explain: str = ""                # one sentence for the Inspector
    stale: bool = False

    def side(self, node: str) -> "EcSide | None":
        return self.side_a if node == self.a else (self.side_b if node == self.b else None)


def _ec_fresh(f: "Fact | None", now: float, limit: float) -> bool:
    return f is not None and f.age(now) <= limit


def _member_mode(ec: EcFacts, g: dict, port: str, now: float, lim: float) -> tuple[str | None, dict | None, dict | None]:
    """(mode, own protocol row, partner row) of one member."""
    proto = g.get("proto")
    if proto == "on":
        return "on", None, None
    if proto == "lacp":
        own = ec.lacp_port(g["group"], port) if _ec_fresh(ec.lacp_internal, now, lim) else None
        par = ec.lacp_partner(g["group"], port) if _ec_fresh(ec.lacp_neighbor, now, lim) else None
        return (own["mode"] if own else None), own, par
    if proto == "pagp":
        own = ec.pagp_port(g["group"], port) if _ec_fresh(ec.pagp_internal, now, lim) else None
        par = ec.pagp_partner(g["group"], port) if _ec_fresh(ec.pagp_neighbor, now, lim) else None
        return (own["mode"] if own else None), own, par
    return None, None, None


def _member_words(flags: str) -> str:
    for c, w in (("P", "bundled"), ("s", "suspended"), ("H", "hot-standby"), ("I", "stand-alone"), ("D", "down"),
                 ("w", "waiting"), ("u", "unsuitable"), ("m", "not aggregated (min links)")):
        if c in flags:
            return w
    return flags or "?"


def _lacp_owners(topo: Topology, facts: dict[str, NodeFacts], now: float, lim: float) -> dict[str, str]:
    """LACP system MAC -> node key ('show lacp sys-id' of every probed device)."""
    out = {}
    for nk, nf in facts.items():
        if nk in topo.nodes and nf.ec is not None and _ec_fresh(nf.ec.lacp_sys, now, lim * 4) and \
                nf.ec.lacp_sys.value.get("mac"):
            out[nf.ec.lacp_sys.value["mac"]] = nk
    return out


def _port_by_lacp_number(nf: NodeFacts | None, number: int) -> str | None:
    if nf is None or nf.ec is None or nf.ec.lacp_internal is None:
        return None
    for rows in nf.ec.lacp_internal.value.values():
        for r in rows:
            if r["port_number"] == number:
                return r["port"]
    return None


def build_bundles(topo: Topology, facts: dict[str, NodeFacts], links: dict[str, LinkFacts], now: float,
                  fresh_s: float, label: dict[str, str] | None = None) -> dict[str, Bundle]:
    """Group EtherChannel member cables into bundles and judge them (pure).  Sets ``LinkFacts.bundle`` /
    ``ec_a`` / ``ec_b`` on every member cable.  A bundle exists when at least one end of two or more
    parallel cables (or of one cable) has a ``channel-group``; the other end may have none (mismatch)."""
    from .etherchannel import check_modes, proto_label
    label = label or {k: n.label for k, n in topo.nodes.items()}
    lim = fresh_s * EC_STALE_FACTOR
    dlim = fresh_s * EC_DETAIL_FACTOR
    by_end: dict[tuple[str, str], str] = {}          # (node, iface_key) -> link key
    for lk, l in topo.links.items():
        by_end[(l.a, iface_key(l.iface_a))] = lk
        by_end[(l.b, iface_key(l.iface_b))] = lk
    lacp_owner = _lacp_owners(topo, facts, now, lim)
    sides: dict[tuple[str, str], EcSide] = {}         # (node, Po) -> side
    side_links: dict[tuple[str, str], list[str]] = {}
    for nk, nf in facts.items():
        n = topo.nodes.get(nk)
        if n is None or not n.booted or nf.ec is None or not _ec_fresh(nf.ec.summary, now, lim):
            continue
        ec = nf.ec
        age = ec.summary.age(now)
        lb = ec.load_balance.value.get("method") if ec.load_balance else None
        sysid = ec.lacp_sys.value if ec.lacp_sys else {}
        status = nf.l2.ports.value if (nf.l2 is not None and nf.l2.ports is not None) else {}
        for g in ec.groups():
            po = g["port_channel"]
            try:
                proto = EcProto(g.get("proto") or "on")
            except ValueError:
                proto = EcProto.ON
            side = EcSide(nk, n.label, g["group"], po, proto, g["flags"], bool(g.get("up")), [],
                          sysid.get("priority") if proto == EcProto.LACP else None,
                          sysid.get("mac") if proto == EcProto.LACP else None, lb, age_s=age)
            lks = []
            for pt in g["ports"]:
                port = pt["port"]
                mode, own, par = _member_mode(ec, g, port, now, dlim)
                m = EcMember(port, pt["flags"], _member_words(pt["flags"]), "P" in pt["flags"], mode)
                lk = by_end.get((nk, iface_key(port)))
                if lk is not None:
                    l = topo.links[lk]
                    m.link_key = lk
                    m.peer_node, m.peer_port = (l.b, short_iface(l.iface_b)) if l.a == nk else (l.a, short_iface(l.iface_a))
                    lks.append(lk)
                if own is not None:
                    m.port_priority = own.get("prio")
                    m.lacp_state = own.get("state") if proto == EcProto.LACP else None
                if par is not None:
                    m.partner_dev = par.get("dev_id")
                    m.partner_priority = par.get("prio") if proto == EcProto.LACP else None
                    if proto == EcProto.LACP:
                        m.partner_mode = par.get("mode")
                        owner = lacp_owner.get(par.get("dev_id") or "")
                        if owner:
                            m.partner_node, m.partner_source = owner, "lacp"
                            m.partner_port = _port_by_lacp_number(facts.get(owner), par.get("port_number", -1))
                    else:
                        name = par.get("name")
                        owner = next((k for k, x in topo.nodes.items() if x.label == name), None)
                        m.partner_node, m.partner_port, m.partner_source = owner, par.get("partner_port"), "pagp"
                if m.partner_node is None and nf.l2 is not None and nf.l2.cdp is not None:
                    c = next((c for c in nf.l2.cdp.value if iface_key(c["local"]) == iface_key(port)), None)
                    if c is not None:
                        owner = next((k for k, x in topo.nodes.items() if x.label == c["device"]), None)
                        m.partner_node, m.partner_port, m.partner_source = owner, c["port"], "cdp"
                sp = status.get(iface_key(port))
                if sp is not None:
                    m.vlan, m.speed, m.duplex = sp.vlan, sp.speed, sp.duplex
                prob = ec.problems.get(short_iface(port))
                if prob and now - prob[1] < EC_PROBLEM_TTL and not m.bundled:
                    m.problem = prob[0]
                side.members.append(m)
            # the Port-channel's own L2 / STP view (STP runs on the Po)
            pl = port_l2(nk, nf, po, now, fresh_s)
            if pl is not None:
                side.stp, side.trunk_text, side.native = dict(pl.stp), pl.trunk_text, pl.native
                side.mode_l2, side.access_vlan = pl.mode, pl.access_vlan
            sides[(nk, po)] = side
            side_links[(nk, po)] = lks
    # ---- pair sides across their shared cables
    used: set[tuple[str, str]] = set()
    bundles: dict[str, Bundle] = {}
    for sk in sorted(sides, key=lambda k: (label.get(k[0], k[0]), k[1])):
        if sk in used:
            continue
        lks = side_links[sk]
        if not lks:
            continue
        used.add(sk)
        sa = sides[sk]
        peers = [m.peer_node for m in sa.members if m.peer_node]
        peer = max(set(peers), key=peers.count) if peers else None
        sb = None
        for ok, ol in side_links.items():
            if ok not in used and ok[0] == peer and set(ol) & set(lks):
                sb = sides[ok]
                used.add(ok)
                lks = sorted(set(lks) | set(ol))
                break
        lks = sorted(lks, key=lambda k: (iface_key(topo.links[k].iface_a if topo.links[k].a == sa.node
                                                   else topo.links[k].iface_b)))
        key = f"ec:{sa.node}/{sa.po}|{peer or '?'}/{sb.po if sb else '-'}"
        bd = Bundle(key, sa.node, peer or "?", sa, sb, lks, total=len(lks))
        _judge_bundle(bd, topo, facts, links, label, now, fresh_s, check_modes, proto_label)
        bundles[key] = bd
        for lk in lks:
            lf = links.get(lk)
            if lf is None:
                continue
            lf.bundle = key
            l = topo.links[lk]
            for side in (sa, sb):
                if side is None:
                    continue
                m = next((x for x in side.members if x.link_key == lk), None)
                if m is None:
                    continue
                if l.a == side.node:
                    lf.ec_a = m
                else:
                    lf.ec_b = m
    return bundles


def _judge_bundle(bd: Bundle, topo: Topology, facts: dict[str, NodeFacts], links: dict[str, LinkFacts],
                  label: dict[str, str], now: float, fresh_s: float, check_modes, proto_label) -> None:
    sa, sb = bd.side_a, bd.side_b
    la, lb = label.get(bd.a, "?"), label.get(bd.b, "?")
    peer = topo.nodes.get(bd.b)
    peer_probed = peer is not None and peer.collected and peer.booted
    nfb = facts.get(bd.b)
    peer_has_ec = nfb is not None and nfb.ec is not None and _ec_fresh(nfb.ec.summary, now, fresh_s * EC_STALE_FACTOR)
    bd.stale = sa.age_s is not None and sa.age_s > fresh_s * 1.5
    # ---- protocol / Po names
    pa = sa.proto
    pb = sb.proto if sb else (EcProto.NONE if peer_probed and peer_has_ec else None)
    if pb is None or pb == pa:
        bd.protocol = proto_label(pa.value)
    else:
        bd.protocol = f"{proto_label(pa.value)}≠{proto_label(pb.value) if pb != EcProto.NONE else 'none'}"
    bd.po_text = sa.po if (sb is None or sb.po == sa.po) else f"{sa.po}/{sb.po}"
    bd.load_balance = sa.load_balance or (sb.load_balance if sb else None)
    # ---- per cable: modes on both ends + bundled on both ends
    reasons: list[tuple[str, str]] = []
    bundled = 0
    for lk in bd.link_keys:
        ma = next((m for m in sa.members if m.link_key == lk), None)
        mb = next((m for m in sb.members if m.link_key == lk), None) if sb else None
        ok_a = ma is not None and ma.bundled
        ok_b = (mb is not None and mb.bundled) if (sb is not None or (peer_probed and peer_has_ec)) else True
        n_before = len(reasons)
        if ma is not None and sb is None and peer_probed and peer_has_ec:
            reasons.append((f"{lb} has no channel-group — never bundles",
                            f"{lb} {ma.peer_port or '?'} is not in any channel-group, so {la}'s {ma.port} "
                            f"({ma.mode or sa.proto.value}) has no partner to bundle with."))
        elif ma is not None and mb is not None and ma.mode and mb.mode:
            chk = check_modes(ma.mode, mb.mode)
            if not chk.forms:
                reasons.append((chk.reason, chk.explain))
        elif ma is not None and mb is not None and sa.proto != sb.proto and EcProto.NONE not in (sa.proto, sb.proto):
            chk = check_modes({"lacp": "active", "pagp": "desirable", "on": "on"}[sa.proto.value],
                              {"lacp": "active", "pagp": "desirable", "on": "on"}[sb.proto.value])
            if not chk.forms:
                reasons.append((chk.reason, chk.explain))
        # modes that can never bundle: the cable does not count even while a side still shows (P) (the LACP
        # side keeps its old state until the partner times out; a static 'on' side always says P)
        # a cable CML has stopped (pulled / not started) carries nothing, whatever the switches still report:
        # in CML the far end keeps carrier and LACP / PAgP keep the port bundled until their timeout
        lf_ = links.get(lk)
        cut = lf_ is not None and lf_.status.kind in (LinkKind.CABLE_PULLED, LinkKind.LINK_NOT_STARTED)
        if cut:
            m_ = ma or mb
            bd.problems.append(f"{la} {m_.port if ma else '?'} ↔ {lb} {mb.port if mb else (ma.peer_port if ma else '?')}: "
                               f"{lf_.status.text} (the switches may still list it as bundled until LACP/PAgP time out)")
        if ok_a and ok_b and len(reasons) == n_before and not cut:
            bundled += 1
    # ---- member config consistency per side (speed / duplex / VLAN-trunk) and syslog reasons
    for side in (sa, sb):
        if side is None or len(side.members) < 2:
            continue
        ref = side.members[0]
        for m in side.members[1:]:
            diffs = []
            if ref.vlan is not None and m.vlan is not None and ref.vlan != m.vlan:
                def vw(v):
                    return "trunk" if v == "trunk" else (f"access VLAN {v}" if isinstance(v, int) else str(v))
                diffs.append(f"{vw(m.vlan)} vs {vw(ref.vlan)}")
            for attr, word in (("speed", "speed"), ("duplex", "duplex")):
                x, y = getattr(m, attr), getattr(ref, attr)
                if x and y and x.replace("a-", "") != y.replace("a-", "") and "auto" not in (x, y):
                    diffs.append(f"{word} {x} vs {y}")
            if diffs:
                reasons.append((f"{side.label} members differ — {diffs[0]}",
                                f"{side.label} {m.port} and {ref.port} are configured differently "
                                f"({'; '.join(diffs)}): all members of a bundle need the same speed, duplex and "
                                "VLAN / trunk settings, so IOS suspends the odd one."))
        for m in side.members:
            if m.problem:
                reasons.append((f"{side.label} {m.port}: {m.problem}",
                                f"IOS refused to bundle {side.label} {m.port}: {m.problem}."))
    # ---- partner check (protocol view vs cabling)
    for side in (sa, sb):
        if side is None:
            continue
        for m in side.members:
            if m.partner_node and m.peer_node and m.partner_node != m.peer_node:
                reasons.append((f"{side.label} {m.port}: partner is {label.get(m.partner_node, '?')}",
                                f"{side.label} {m.port} is cabled to {label.get(m.peer_node, '?')} but "
                                f"{(m.partner_source or '').upper()} reports {label.get(m.partner_node, '?')}."))
    seen = set()
    uniq = []
    for short, long_ in reasons:
        if short not in seen:
            seen.add(short)
            uniq.append((short, long_))
    bd.mismatch = [lg for _, lg in uniq]
    bd.mismatch_short = uniq[0][0] if uniq else None
    bd.bundled = bundled
    # ---- state
    po_down = (not sa.up) or (sb is not None and not sb.up)
    if bd.stale:
        bd.state = EcState.UNKNOWN
    elif uniq:
        bd.state = EcState.MISMATCH
    elif bundled == 0 or po_down:
        bd.state = EcState.DOWN
    elif bundled < bd.total:
        bd.state = EcState.PARTIAL
    else:
        bd.state = EcState.UP
    # ---- partial / down words per member
    for lk in bd.link_keys:
        lf = links.get(lk)
        ma = next((m for m in sa.members if m.link_key == lk), None)
        mb = next((m for m in sb.members if m.link_key == lk), None) if sb else None
        bad = [(side, m) for side, m in ((sa, ma), (sb, mb)) if side is not None and m is not None and not m.bundled]
        for side, m in bad:
            why = m.words
            if lf is not None and lf.status.kind in DOWN_KINDS and lf.status.side_node == side.node and \
                    lf.status.kind != LinkKind.PROTOCOL_DOWN:
                why = lf.status.cause or why
            elif m.problem:
                why += f": {m.problem}"
            bd.problems.append(f"{side.label} {m.port} not bundled ({why})")
        # an unbundled member shows 'up / line protocol down' in IOS: say what it really is
        if lf is not None and lf.status.kind == LinkKind.PROTOCOL_DOWN and lf.status.side_node:
            m = ma if (ma is not None and sa.node == lf.status.side_node) else \
                (mb if (mb is not None and sb is not None and sb.node == lf.status.side_node) else None)
            if m is not None and not m.bundled and m.words in ("suspended", "stand-alone", "hot-standby", "waiting"):
                lbl = label.get(lf.status.side_node, "?")
                st = lf.status
                st.cause = f"EtherChannel {m.words}"
                st.text = f"{lbl} {m.port} {m.words}"
                st.plain_text = (f"{lbl} {m.port} is {m.words}: it is cabled but not in {bd.po_text} because the "
                                 f"bundle did not form on it ({m.problem or (bd.mismatch_short or 'see the bundle')})."
                                 if bd.state == EcState.MISMATCH or m.problem else
                                 f"{lbl} {m.port} is {m.words}: it is cabled but not bundled into {bd.po_text}.")
    # ---- L2 view of the bundle (from the Port-channels)
    trunks = [s_ for s_ in (sa, sb) if s_ is not None and s_.mode_l2 == PortMode.TRUNK]
    if trunks:
        bd.trunk_text = trunks[0].trunk_text
        bd.native_vlan = trunks[0].native
        from .parsers.ios_l2 import vlan_ranges
        vs = [set(v for lo, hi in vlan_ranges(t.trunk_text) if hi - lo < 64 for v in range(lo, hi + 1))
              for t in trunks if t.trunk_text]
        bd.vlans = sorted(set.intersection(*vs)) if vs else []
    for side_name, side in (("a", sa), ("b", sb)):
        if side is None or bd.stp_block is not None:
            continue
        blocked = sorted(v for v, (_, st) in side.stp.items() if st == PortState.BLK)
        if blocked:
            roles = [side.stp[v][0] for v in blocked]
            role = next((r for r in roles if r in (PortRole.ALTN, PortRole.BACK)), roles[0])
            fwd = [v for v, (_, st) in side.stp.items() if st == PortState.FWD]
            bd.stp_block = StpBlock(side_name, side.node, side.po, role, blocked, partial=bool(fwd))
    # ---- texts
    bd.label = f"{bd.po_text} · {bd.protocol} {bd.bundled}/{bd.total}"
    modes = " ↔ ".join(f"{s_.label} {'/'.join(s_.modes) or s_.proto.value}" for s_ in (sa, sb) if s_ is not None)
    lines_ = [f"EtherChannel {bd.po_text} {la} ↔ {lb}: {bd.protocol}, {bd.bundled} of {bd.total} member links bundled"]
    if modes:
        lines_.append(f"modes: {modes}")
    if bd.trunk_text:
        lines_.append(f"802.1Q trunk on the Port-channel, VLAN {bd.trunk_text}"
                      + (f", native {bd.native_vlan}" if bd.native_vlan is not None else ""))
    if bd.load_balance:
        lines_.append(f"load-balance {bd.load_balance}")
    if bd.stp_block is not None:
        b = bd.stp_block
        lines_.append(f"spanning tree: {label.get(b.node, '?')} {b.port} is {b.role_word} and BLOCKING "
                      f"(VLAN {_vlan_list_text(b.vlans)}) - the whole bundle is the backup path")
    for r in bd.mismatch:
        lines_.append("⚠ " + r)
    for p_ in bd.problems:
        lines_.append(p_)
    bd.tooltip = "\n".join(lines_)
    if bd.state == EcState.MISMATCH:
        bd.explain = bd.mismatch[0]
    elif bd.state == EcState.UP:
        bd.explain = (f"{bd.total} cables act as one {bd.protocol} link ({bd.po_text}); spanning tree sees a single "
                      "port, traffic is shared across the members" + (f" ({bd.load_balance})" if bd.load_balance else "")
                      + ("; it is blocked as a whole by spanning tree" if bd.stp_block is not None else "") + ".")
    elif bd.state == EcState.PARTIAL:
        bd.explain = (f"Only {bd.bundled} of {bd.total} members are bundled ({'; '.join(bd.problems) or 'see members'}); "
                      "the bundle still works with less bandwidth.")
    elif bd.state == EcState.DOWN:
        bd.explain = f"{bd.po_text} is down: no member is bundled" + (f" ({'; '.join(bd.problems)})" if bd.problems else "") + "."
    else:
        bd.explain = "No fresh EtherChannel data (probe pending)."


# ======================================================================= link status (D4)
def _side(topo: Topology, node_key: str, iface_label: str, facts: dict[str, NodeFacts], now: float,
          fresh_s: float) -> tuple[LinkSide, IfFact | None, bool]:
    """(side, iface fact, stale)"""
    n = topo.nodes.get(node_key)
    if n is not None and n.role == NodeRole.ROUTER and not n.probed:
        # a router LabDesk cannot read (host-KVM without a wired collector): never assume its port is up
        return LinkSide.UNKNOWN, None, True
    if n is None or not n.collected:
        return LinkSide.NOT_APPLICABLE, None, False
    nf = facts.get(node_key)
    if nf is None or nf.ifaces is None:
        return LinkSide.UNKNOWN, None, True
    f = nf.iface(iface_label)
    if not nf.ifaces.fresh(now, fresh_s):
        return LinkSide.UNKNOWN, f, True
    if f is None:
        return LinkSide.UNKNOWN, None, False
    return f.side, f, False


def link_status(link: TopoLink, topo: Topology, facts: dict[str, NodeFacts], link_history: dict,
                now: float, fresh_s: float) -> tuple[LinkStatus, LinkSide, LinkSide, IfFact | None, IfFact | None]:
    """Pure D4 state machine.  Returns (status, side_a, side_b, iface_fact_a, iface_fact_b).

    ``link_history[link.key]`` may hold {'pulled_by_labdesk': ts, 'observed_stop': ts,
    'api_stop': ts, 'kvm_left_down': bool} - the evidence required for CABLE_PULLED."""
    na, nb = topo.nodes.get(link.a), topo.nodes.get(link.b)
    la, lb = (na.label if na else "?"), (nb.label if nb else "?")
    sa_short, sb_short = short_iface(link.iface_a), short_iface(link.iface_b)
    side_a, fa, stale_a = _side(topo, link.a, link.iface_a, facts, now, fresh_s)
    side_b, fb, stale_b = _side(topo, link.b, link.iface_b, facts, now, fresh_s)

    def done(st):
        return st, side_a, side_b, fa, fb

    # 1. lab stopped
    if topo.lab_state not in ("STARTED", "BOOTED", "QUEUED") and not link.kvm:
        return done(LinkStatus(LinkKind.LAB_OFF, text="lab off", plain_text="The lab is not running.",
                               api_certain=True))
    # 2. node off
    for n, lbl in ((na, la), (nb, lb)):
        if n is None or not n.booted:
            return done(LinkStatus(LinkKind.NODE_OFF, n.key if n else None, None, "node not booted",
                                   f"{lbl} off", f"{lbl} is not running, so this cable carries nothing.",
                                   api_certain=True))
    # 3. cable pulled (API-certain) / link not started
    if not link.kvm and link.api_state != "STARTED":
        h = link_history.get(link.key, {})
        if h.get("observed_stop") or h.get("pulled_by_labdesk") or h.get("api_stop"):
            return done(LinkStatus(LinkKind.CABLE_PULLED, None, None, "link stopped in CML", "cable pulled",
                                   f"The virtual cable between {la} {sa_short} and {lb} {sb_short} is "
                                   "disconnected (link stopped in CML).", api_certain=True))
        return done(LinkStatus(LinkKind.LINK_NOT_STARTED, None, None, f"CML link {link.api_state or 'not started'}",
                               "link not started", "CML has not started this link yet.", api_certain=True))
    if link.kvm and link_history.get(link.key, {}).get("kvm_link_down") and \
            not link_history.get(link.key, {}).get("kvm_left_down"):
        return done(LinkStatus(LinkKind.CABLE_PULLED, None, None, "vnet link down", "cable pulled",
                               f"The host-VM cable between {la} and {lb} is disconnected.", api_certain=True))

    sides = [(link.a, la, sa_short, side_a, stale_a), (link.b, lb, sb_short, side_b, stale_b)]
    stale = tuple(k for k, _, _, s, st in sides if s == LinkSide.UNKNOWN)
    other_txt = {}
    for k, lbl, ifn, s, st in sides:
        other_txt[k] = (f"{lbl} {ifn} still up/up" if s == LinkSide.UP else
                        f"{lbl} {ifn} state unknown (no fresh data)" if s == LinkSide.UNKNOWN else
                        f"{lbl} {ifn} {s.value.lower().replace('_', ' ')}")
    # 5-7: a fresh side is down (priority: admin down > link down > protocol down)
    for wanted, kind, cause, short_word, plain in (
            (LinkSide.ADMIN_DOWN, LinkKind.ADMIN_DOWN, "shutdown", "shut",
             "Someone typed shutdown on {lbl}'s {ifn}"),
            (LinkSide.DOWN, LinkKind.LINK_DOWN, "no carrier", "down",
             "{lbl}'s {ifn} sees no signal on the cable"),
            (LinkSide.PROTO_DOWN, LinkKind.PROTOCOL_DOWN, "line protocol down", "line protocol down",
             "{lbl}'s {ifn} has a signal but the line protocol is down")):
        for i, (k, lbl, ifn, s, st) in enumerate(sides):
            if s != wanted:
                continue
            ok_, olbl, oifn, os_, ost = sides[1 - i]
            tail = ""
            if os_ == LinkSide.UP:
                tail = f"; {olbl}'s side still looks fine"
            elif os_ == LinkSide.UNKNOWN:
                tail = f"; no fresh data from {olbl}"
            txt = f"{lbl} {ifn} {short_word}"
            tip = f"down at {lbl} {ifn} ({cause})"
            if os_ not in (LinkSide.NOT_APPLICABLE,):
                tip += f"; {other_txt[ok_]}"
            return done(LinkStatus(kind, k, ifn, cause, txt, plain.format(lbl=lbl, ifn=ifn) + tail + ".",
                                   False, stale))
    # 4. unknown (stale / missing) side
    if stale:
        names = [topo.nodes[k].label for k in stale if k in topo.nodes]
        unread = [topo.nodes[k].label for k in stale if k in topo.nodes and not topo.nodes[k].probed]
        if unread:
            return done(LinkStatus(LinkKind.UNKNOWN, stale[0], None, "not read",
                                   "not read", " and ".join(unread) + " is not read by LabDesk (no live collector"
                                   " for it yet), so the state of this port is unknown.", False, stale))
        return done(LinkStatus(LinkKind.UNKNOWN, stale[0], None, "no fresh data",
                               "state unknown", "No recent information from " + " and ".join(names) + ".",
                               False, stale))
    # 8. up
    if side_a == side_b == LinkSide.NOT_APPLICABLE:
        # no end is read from a device: only CML's own runtime state says anything about this cable
        return done(LinkStatus(LinkKind.UP, text="up", plain_text="CML state: the link is started and both nodes "
                               "are running (no device on this cable is read by LabDesk).", api_certain=True))
    return done(LinkStatus(LinkKind.UP, text="up", plain_text="Both ends are up (read from the device).",
                           api_certain=False))


# ======================================================================= join (D3)
@dataclass
class JoinResult:
    links: dict[str, LinkFacts]
    segments: dict[str, Segment]
    paths: dict[str, DefaultPath]
    rid_map: dict[str, str]                 # router-id / loopback / any iface ip -> node key
    ip_owner: dict[str, tuple[str, str]]    # ip -> (node key, iface_key)
    bundles: dict[str, "Bundle"] = field(default_factory=dict)   # EtherChannels (Bundle.key)


def _ip_owners(topo: Topology, facts: dict[str, NodeFacts]) -> dict[str, tuple[str, str]]:
    out = {}
    for nk, nf in facts.items():
        if nk not in topo.nodes or nf.ifaces is None:
            continue
        for k, f in nf.ifaces.value.items():
            if f.ip:
                out.setdefault(f.ip, (nk, k))
    return out


def _rid_map(facts: dict[str, NodeFacts], ip_owner) -> dict[str, str]:
    m = {ip: nk for ip, (nk, _) in ip_owner.items()}
    for nk, nf in facts.items():
        if nf.rid:
            m[nf.rid.value] = nk
        if nf.loopback:
            m[nf.loopback] = nk
    return m


def join_facts(topo: Topology, facts: dict[str, NodeFacts], link_history: dict, now: float,
               fresh_s: float, verified_internet: dict[str, float] | None = None,
               verify_ttl: float = 300.0) -> JoinResult:
    segs, link_seg = build_segments(topo)
    ip_owner = _ip_owners(topo, facts)
    rid_map = _rid_map(facts, ip_owner)
    label = {k: n.label for k, n in topo.nodes.items()}

    # ---- link status + P2P
    links: dict[str, LinkFacts] = {}
    for lk, l in topo.links.items():
        st, sa, sb, fa, fb = link_status(l, topo, facts, link_history, now, fresh_s)
        lf = LinkFacts(lk, sa, sb, fa, fb, st, link_seg.get(lk))
        seg = segs.get(lf.segment_id)
        # subnet / addressing
        nets = set()
        for nk, f in ((l.a, fa), (l.b, fb)):
            if f is None:
                continue
            plen = f.prefixlen
            if plen is None and facts.get(nk) and facts[nk].connected:
                plen = facts[nk].connected.value.get(iface_key(f.name))
            if f.ip and plen is not None:
                nets.add(_subnet(f.ip, plen))
        if len(nets) == 1:
            lf.subnet = nets.pop()
            lf.subnet_hint = "/" + lf.subnet.split("/")[1]
        elif len(nets) > 1:
            lf.addressing_mismatch = True
        if seg and seg.kind == SegmentKind.P2P and fa and fb and fa.ip and fb.ip and \
                fa.prefixlen is not None and fb.prefixlen is not None and \
                _subnet(fa.ip, fa.prefixlen) != _subnet(fb.ip, fb.prefixlen):
            lf.addressing_mismatch = True
        # P2P provenance
        srcs = []
        cfg_p2p = False
        for nk, f in ((l.a, fa), (l.b, fb)):
            if f is None:
                continue
            nf = facts.get(nk)
            vendor = nf.vendor if nf else "cisco"
            if f.ospf_net == "POINT_TO_POINT":
                srcs.append({"juniper": P2PSource.JUNOS_PTTOPT, "fortinet": P2PSource.FORTIOS_P2P}.get(
                    vendor, P2PSource.OSPF_OPER))
            if re_serial(f.name):
                srcs.append(P2PSource.MEDIUM)
            if nf and nf.config and iface_key(f.name) in nf.config.value.p2p_ifaces:
                cfg_p2p = True
        oper_known = any(f is not None and f.ospf_net for f in (fa, fb))
        if srcs:
            lf.p2p, lf.p2p_source = True, srcs[0]
        elif cfg_p2p:
            lf.p2p_source = P2PSource.CONFIG
            lf.p2p_conflict = "configured p2p, not active"
        if cfg_p2p and oper_known and not srcs:
            lf.p2p_conflict = "configured p2p, not active"
        # BCAST teaching bead: 2-router segment, OSPF broadcast on both ends
        if seg and seg.kind == SegmentKind.P2P and fa and fb and fa.ospf_net == "BROADCAST" == fb.ospf_net:
            lf.bcast = True
        _link_l2(lf, l, topo, facts, now, fresh_s)
        _err_disabled_cause(lf, label)
        if topo.lab_state in ("STARTED", "BOOTED", "QUEUED") or l.kvm:
            ad = link_addressing(l, topo, facts, now, fresh_s)
            if ad.state != SubnetState.NONE:
                lf.addressing = ad
                if ad.state == SubnetState.MISMATCH:
                    lf.addressing_mismatch, lf.subnet, lf.subnet_hint = True, None, None
                elif ad.nets:
                    lf.subnet = ad.nets[0]
                    lf.subnet_hint = "/" + lf.subnet.split("/")[1]
                    lf.addressing_mismatch = False
        links[lk] = lf

    # ---- segment protocols / adjacencies
    for seg in segs.values():
        _segment_protocols(seg, topo, facts, ip_owner, rid_map, label, now, fresh_s)
        for lk in seg.link_keys:
            lf = links[lk]
            if seg.kind == SegmentKind.P2P:
                lf.protocols = dict(seg.protocols)
                lf.adjacencies = list(seg.adjacencies)
            if lf.bcast:
                dr = bdr = None
                for nk, ifl in seg.members:
                    nf = facts.get(nk)
                    f = nf.iface(ifl) if nf else None
                    if f and f.ospf_state == "DR":
                        dr = label.get(nk)
                    elif f and f.ospf_state == "BDR":
                        bdr = label.get(nk)
                parts = [f"DR {dr}" if dr else None, f"BDR {bdr}" if bdr else None]
                lf.bcast_detail = " · ".join(p for p in parts if p) or None

    # ---- default paths (D5)
    paths = {}
    for nk, n in topo.nodes.items():
        if n.role != NodeRole.ROUTER or not n.booted:
            continue
        paths[nk] = resolve_default_path(nk, topo, facts, links, segs, link_seg, ip_owner, now, fresh_s,
                                         verified_internet or {}, verify_ttl)
    # blue overlay: mark good hops on links
    for p in paths.values():
        for h in p.hops:
            if not h.ok:
                continue
            for i, lk in enumerate(h.link_keys):
                lf = links.get(lk)
                if lf is None or lf.default_hop is not None:
                    continue
                l = topo.links[lk]
                src = h.from_node if i == 0 else (topo.links[h.link_keys[0]].b if topo.links[h.link_keys[0]].a == h.from_node
                                                   else topo.links[h.link_keys[0]].a)
                lf.default_hop = h
                lf.default_direction = "a2b" if l.a == src else "b2a"
    bundles = build_bundles(topo, facts, links, now, fresh_s, label)
    for lf in links.values():
        lf.tooltip = link_tooltip(lf, topo, label, bundles.get(lf.bundle) if lf.bundle else None)
    return JoinResult(links, segs, paths, rid_map, ip_owner, bundles)


def _err_disabled_cause(lf: LinkFacts, label: dict[str, str]) -> None:
    """A switch port in err-disabled shows down/down in 'show ip interface brief'; say why."""
    st = lf.status
    if st.kind != LinkKind.LINK_DOWN or not st.side_node:
        return
    p = lf.l2_a if (lf.l2_a and lf.l2_a.node == st.side_node) else (lf.l2_b if lf.l2_b and lf.l2_b.node == st.side_node
                                                                      else None)
    if p is None or not p.err_disabled:
        return
    lbl = label.get(st.side_node, "?")
    st.cause = "err-disabled"
    st.text = f"{lbl} {st.side_iface} err-disabled"
    st.plain_text = (f"{lbl} shut {st.side_iface} down by itself (err-disabled, e.g. BPDU guard or port "
                     "security); it stays down until someone does shutdown / no shutdown.")


def re_serial(name: str) -> bool:
    """Serial / POS / multilink interfaces are point-to-point by medium."""
    return bool(re.match(r"^(serial|se\d|pos|multilink)", (name or "").lower()))


def _segment_protocols(seg: Segment, topo: Topology, facts, ip_owner, rid_map, label, now, fresh_s) -> None:
    routers = [(nk, ifl) for nk, ifl in seg.members
               if topo.nodes.get(nk) and topo.nodes[nk].role == NodeRole.ROUTER]
    member_keys = {nk for nk, _ in routers}
    # -------- OSPF
    ospf_ends = []
    stale = False
    for nk, ifl in routers:
        nf = facts.get(nk)
        if nf is None or nf.ifaces is None:
            stale = True
            continue
        if not nf.ifaces.fresh(now, fresh_s):
            stale = True
        f = nf.iface(ifl)
        if f and f.ospf_state and f.ospf_state != "LOOP":
            ospf_ends.append((nk, f))
    if ospf_ends:
        active = [(nk, f) for nk, f in ospf_ends if not f.ospf_passive]
        seen_pairs = set()
        full_pairs, partial_pairs = 0, 0
        for nk, f in active:
            nf = facts[nk]
            for nb in (nf.ospf_nbrs.value if nf.ospf_nbrs else []):
                if iface_key(nb.iface) != iface_key(f.name):
                    continue
                other = rid_map.get(nb.neighbor_id) or (ip_owner.get(nb.address) or (None,))[0]
                if other is None or other == nk:
                    continue
                pair = tuple(sorted((nk, other)))
                if pair in seen_pairs:
                    continue
                seen_pairs.add(pair)
                seg.adjacencies.append((pair[0], pair[1], Proto.OSPF, nb.state))
                if nb.adj_state == "FULL" or (nb.adj_state == "2WAY" and nb.role == "DROTHER"):
                    full_pairs += 1
                else:
                    partial_pairs += 1
        expected = len(active) >= 2
        if not active:
            h = None                               # OSPF only passive here: nothing to show
        elif stale:
            h = Health.UNKNOWN
        elif full_pairs and not partial_pairs:
            h = Health.OK
        elif full_pairs or partial_pairs:
            h = Health.PARTIAL
        elif expected and any(f.ospf_passive is None for _, f in active):
            h = None                               # passive state not known yet: never claim '!'
        elif expected:
            h = Health.DOWN                        # '! no neighbour'
        else:
            h = None
        if h is not None:
            seg.protocols[Proto.OSPF] = h
            seg.detail[Proto.OSPF] = (f"{full_pairs} full {'adjacency' if full_pairs == 1 else 'adjacencies'}"
                                      if h != Health.DOWN else "OSPF on both ends but no neighbour")
    # -------- BGP sessions on this segment (peer IP owned by a member interface)
    seg_ips = {}
    for nk, ifl in routers:
        nf = facts.get(nk)
        f = nf.iface(ifl) if nf else None
        if f and f.ip:
            seg_ips[f.ip] = nk
    bgp_states = []
    sessions: dict[tuple[str, str], SegBgpSession] = {}
    for nk, _ in routers:
        nf = facts.get(nk)
        if not nf or not nf.bgp or not nf.bgp.value.active:
            continue
        for p in nf.bgp.value.peers:
            if p.ip in seg_ips and seg_ips[p.ip] != nk:
                pair = tuple(sorted((nk, seg_ips[p.ip])))
                if not any(a == pair[0] and b == pair[1] and pr == Proto.BGP for a, b, pr, _ in seg.adjacencies):
                    seg.adjacencies.append((pair[0], pair[1], Proto.BGP, p.state))
                bgp_states.append(p.established)
                mine, theirs = nf.bgp.value.local_as, p.remote_as
                s = sessions.get(pair)
                if s is None:
                    s = sessions[pair] = SegBgpSession(pair[0], pair[1], "BGP", established=p.established)
                else:
                    s.established = s.established and p.established
                if mine not in (None, ""):
                    setattr(s, "as_a" if nk == pair[0] else "as_b", mine)
                if theirs not in (None, ""):
                    setattr(s, "as_b" if nk == pair[0] else "as_a", theirs)
                if s.as_a not in (None, "") and s.as_b not in (None, ""):
                    s.kind = "iBGP" if str(s.as_a) == str(s.as_b) else "eBGP"
    if bgp_states:
        seg.protocols[Proto.BGP] = Health.OK if all(bgp_states) else (Health.PARTIAL if any(bgp_states) else Health.DOWN)
        seg.bgp_sessions = list(sessions.values())
        seg.detail[Proto.BGP] = (f"{bgp_segment_sentence(seg.bgp_sessions, label)} "
                                 f"({sum(bgp_states)}/{len(bgp_states)} session ends established)")
    # -------- FHRP
    for proto in (Proto.HSRP, Proto.VRRP):
        groups: dict[int, list[tuple[str, FhrpGroup]]] = {}
        for nk, ifl in routers:
            nf = facts.get(nk)
            if not nf or not nf.fhrp:
                continue
            for g in nf.fhrp.value:
                if g.proto == proto and iface_key(g.iface) == iface_key(ifl):
                    groups.setdefault(g.group, []).append((nk, g))
        if not groups:
            continue
        healths, details = [], []
        for gid, mem in groups.items():
            prim = [label.get(nk, nk) for nk, g in mem if g.is_primary]
            sec = [label.get(nk, nk) for nk, g in mem if g.state in ("Standby", "Backup")]
            if len(prim) == 1 and sec:
                healths.append(Health.OK)
            elif len(prim) == 1:
                healths.append(Health.PARTIAL)
            else:
                healths.append(Health.DOWN)
            role_word = "Active" if proto == Proto.HSRP else "Master"
            sec_word = "Standby" if proto == Proto.HSRP else "Backup"
            details.append(f"grp {gid}: {role_word} {', '.join(prim) or 'none'}"
                           + (f", {sec_word} {', '.join(sec)}" if sec else ""))
        seg.protocols[proto] = (Health.OK if all(h == Health.OK for h in healths) else
                                Health.DOWN if all(h == Health.DOWN for h in healths) else Health.PARTIAL)
        seg.detail[proto] = "; ".join(details)


# ======================================================================= default path (D5)
def _links_between(a: str, a_if: str | None, b: str, b_if: str | None, topo: Topology, segs, link_seg) -> list[str] | None:
    """Link key(s) from node a (iface a_if) to node b (iface b_if): direct, or via one switch segment."""
    for lk, l in topo.links.items():
        for x, xi, y, yi in ((l.a, l.iface_a, l.b, l.iface_b), (l.b, l.iface_b, l.a, l.iface_a)):
            if x == a and y == b and (a_if is None or iface_key(xi) == iface_key(a_if)) and \
                    (b_if is None or iface_key(yi) == iface_key(b_if)):
                return [lk]
    # via a switch segment
    for seg in segs.values():
        if seg.switch_node is None:
            continue
        mem = {(nk, iface_key(i)) for nk, i in seg.members}
        if (a_if is None and any(nk == a for nk, _ in mem) or (a, iface_key(a_if or "")) in mem) and \
                (b_if is None and any(nk == b for nk, _ in mem) or (b, iface_key(b_if or "")) in mem):
            la = next((lk for lk in seg.link_keys if a in (topo.links[lk].a, topo.links[lk].b) and
                       (a_if is None or iface_key(topo.links[lk].iface_a if topo.links[lk].a == a
                                                  else topo.links[lk].iface_b) == iface_key(a_if))), None)
            lb = next((lk for lk in seg.link_keys if b in (topo.links[lk].a, topo.links[lk].b) and
                       (b_if is None or iface_key(topo.links[lk].iface_a if topo.links[lk].a == b
                                                  else topo.links[lk].iface_b) == iface_key(b_if))), None)
            if la and lb:
                return [la, lb]
    return None


def _egress_from_iface(node: str, iface: str | None, topo: Topology, segs, link_seg) -> tuple[list[str], str] | None:
    """If ``iface`` of ``node`` leads (directly or via a switch) to an external connector:
    (link keys, egress node key)."""
    if iface is None:
        return None
    for lk, l in topo.links.items():
        for x, xi, y in ((l.a, l.iface_a, l.b), (l.b, l.iface_b, l.a)):
            if x != node or iface_key(xi) != iface_key(iface):
                continue
            yn = topo.nodes.get(y)
            if yn and yn.role == NodeRole.EGRESS:
                return [lk], y
            if yn and yn.role == NodeRole.SWITCH:
                seg = segs.get(link_seg.get(lk))
                for nk, _ in (seg.members if seg else []):
                    if topo.nodes.get(nk) and topo.nodes[nk].role == NodeRole.EGRESS:
                        l2 = next((k for k in seg.link_keys if nk in (topo.links[k].a, topo.links[k].b)), None)
                        if l2:
                            return [lk, l2], nk
    return None


def _out_iface_for(nf: NodeFacts, ip: str) -> str | None:
    """Interface whose connected subnet contains ``ip``."""
    if nf.ifaces is None:
        return None
    for k, f in nf.ifaces.value.items():
        plen = f.prefixlen
        if plen is None and nf.connected:
            plen = nf.connected.value.get(k)
        if f.ip and plen is not None and plen < 32 and _in_subnet(ip, _subnet(f.ip, plen)):
            return f.name
    return None


def resolve_default_path(origin: str, topo: Topology, facts: dict[str, NodeFacts], links: dict[str, LinkFacts],
                         segs, link_seg, ip_owner, now: float, fresh_s: float,
                         verified: dict[str, float], verify_ttl: float) -> DefaultPath:
    label = {k: n.label for k, n in topo.nodes.items()}
    nf0 = facts.get(origin)
    if nf0 is None or nf0.default_route is None:
        return DefaultPath(origin, PathState.UNKNOWN, detail="no routing data yet")
    if not nf0.default_route.fresh(now, fresh_s):
        return DefaultPath(origin, PathState.STALE, detail="routing data is stale")
    dr0 = nf0.default_route.value
    if not dr0.present:
        return DefaultPath(origin, PathState.NONE, detail=f"{label[origin]} has no default route")
    if dr0.blackhole:
        return DefaultPath(origin, PathState.BLACKHOLE, detail=f"{label[origin]}'s default route points to Null0")

    hops: list[RouteHop] = []
    visited = [origin]
    cur = origin
    ecmp = False
    for _ in range(16):
        nf = facts.get(cur)
        if nf is None or nf.default_route is None:
            return DefaultPath(origin, PathState.UNKNOWN, hops, detail=f"no routing data from {label.get(cur)}")
        if not nf.default_route.fresh(now, fresh_s):
            return DefaultPath(origin, PathState.STALE, hops, broken_at=hops[-1].link_keys[-1] if hops else None,
                               detail=f"routing data from {label.get(cur)} is stale")
        dr = nf.default_route.value
        if not dr.present:
            return DefaultPath(origin, PathState.NO_EGRESS, hops,
                               detail=f"{label.get(cur)} has no default route: traffic stops there")
        if dr.blackhole:
            return DefaultPath(origin, PathState.BLACKHOLE, hops, detail=f"{label.get(cur)} drops it (Null0)")
        if not dr.next_hops:
            return DefaultPath(origin, PathState.UNRESOLVED, hops, detail=f"{label.get(cur)}: no next hop")
        ecmp = ecmp or len(dr.next_hops) > 1
        via, out_if = dr.next_hops[0]
        owner = ip_owner.get(via) if via else None
        if owner and owner[0] != cur:
            nxt, nxt_if = owner
            nxt_name = facts[nxt].ifaces.value[nxt_if].name if facts.get(nxt) and facts[nxt].ifaces else None
            if out_if is None:
                out_if = _out_iface_for(nf, via)
            lks = _links_between(cur, out_if, nxt, nxt_name, topo, segs, link_seg) or \
                _links_between(cur, None, nxt, nxt_name, topo, segs, link_seg)
            if not lks:
                return DefaultPath(origin, PathState.UNRESOLVED, hops,
                                   detail=f"via {via}: no cable from {label.get(cur)} to {label.get(nxt)} found")
            hop = RouteHop(cur, nxt, lks, via, short_iface(out_if or ""), dr.code)
            bad = next((lk for lk in lks if links[lk].status.kind != LinkKind.UP), None)
            if bad:
                hop.ok = False
                hops.append(hop)
                return DefaultPath(origin, PathState.BROKEN_AT, hops, broken_at=bad,
                                   broken_kind=links[bad].status.kind,
                                   detail=f"blocked at {links[bad].status.text or bad} "
                                          f"({label.get(cur)} → {label.get(nxt)})", ecmp=ecmp)
            hops.append(hop)
            if nxt in visited:
                return DefaultPath(origin, PathState.LOOP, hops, loop_pair=(cur, nxt),
                                   detail=f"default route loops {label.get(cur)} ↔ {label.get(nxt)}", ecmp=ecmp)
            visited.append(nxt)
            cur = nxt
            continue
        # next hop outside the lab routers: egress?  (while some routers have no data yet, a
        # foreign next hop is 'unknown', not 'unresolved')
        waiting = [k for k, x in topo.nodes.items() if x.role == NodeRole.ROUTER and x.probed and x.booted
                   and (facts.get(k) is None or facts[k].ifaces is None)]
        if out_if is None and via:
            out_if = _out_iface_for(nf, via)
        eg = _egress_from_iface(cur, out_if, topo, segs, link_seg)
        if eg:
            lks, egn = eg
            hop = RouteHop(cur, egn, lks, via, short_iface(out_if or ""), dr.code)
            bad = next((lk for lk in lks if links[lk].status.kind != LinkKind.UP), None)
            if bad:
                hop.ok = False
                hops.append(hop)
                return DefaultPath(origin, PathState.BROKEN_AT, hops, broken_at=bad,
                                   broken_kind=links[bad].status.kind, egress_node=egn,
                                   egress_mode=topo.nodes[egn].egress_mode,
                                   detail=f"blocked at {links[bad].status.text} before the exit", ecmp=ecmp)
            hops.append(hop)
            state = PathState.EGRESS
            vts = verified.get(origin) or verified.get(egn)
            if vts is not None and now - vts <= verify_ttl:
                state = PathState.VERIFIED_INTERNET
            path_names = " → ".join(label.get(h.from_node, "?") for h in hops) + f" → {label.get(egn)}"
            return DefaultPath(origin, state, hops, egress_node=egn, egress_mode=topo.nodes[egn].egress_mode,
                               detail=f"exits via {path_names}", ecmp=ecmp)
        if waiting:
            return DefaultPath(origin, PathState.UNKNOWN, hops, detail="waiting for data from "
                               + ", ".join(label.get(k, k) for k in waiting), ecmp=ecmp)
        return DefaultPath(origin, PathState.UNRESOLVED, hops,
                           detail=f"via {via or '?'} unresolved (not a lab router, no exit on {out_if or '?'})",
                           ecmp=ecmp)
    return DefaultPath(origin, PathState.LOOP, hops, detail="path too long (loop?)")


# ======================================================================= text
def link_tooltip(lf: LinkFacts, topo: Topology, label: dict[str, str], bundle: "Bundle | None" = None) -> str:
    l = topo.links[lf.key]
    a = f"{label.get(l.a, '?')} {short_iface(l.iface_a)}"
    b = f"{label.get(l.b, '?')} {short_iface(l.iface_b)}"
    parts = [f"{a} ⟷ {b}"]
    if lf.subnet:
        parts[0] += f" · {lf.subnet}"
    if lf.p2p:
        parts.append({P2PSource.OSPF_OPER: "OSPF network point-to-point (from show ip ospf interface)",
                      P2PSource.JUNOS_PTTOPT: "OSPF interface type P2P (Junos)",
                      P2PSource.FORTIOS_P2P: "OSPF network type POINTOPOINT (FortiOS)",
                      P2PSource.MEDIUM: "point-to-point medium (serial/PPP)"}.get(lf.p2p_source, "point-to-point"))
    elif lf.p2p_conflict:
        parts.append(lf.p2p_conflict)
    if lf.bcast:
        parts.append("OSPF broadcast network" + (f" ({lf.bcast_detail})" if lf.bcast_detail else ""))
    parts.append(lf.status.plain_text)
    for p, h in lf.protocols.items():
        parts.append(f"{p.value}: {h.value.lower()}")
    if lf.addressing_mismatch:
        parts.append("the two ends are not in the same subnet")
    elif lf.addressing is not None and lf.addressing.state == SubnetState.ONE_SIDED:
        parts.append(f"only the {label.get(getattr(lf.addressing, lf.addressing.configured_side).node, '?')} "
                     "side has an IP address")
    if lf.trunk:
        parts.append(f"802.1Q trunk carrying VLAN {lf.trunk_text}"
                     + (f", native VLAN {lf.native_vlan}" if lf.native_vlan is not None else "")
                     + (" (native VLAN mismatch!)" if lf.native_mismatch else ""))
    elif lf.access_vlan is not None:
        parts.append(f"access port in VLAN {lf.access_vlan}")
    if bundle is not None:
        ends = [f"{label.get(nd, '?')} {m_.port}: {m_.words}" for nd, m_ in ((l.a, lf.ec_a), (l.b, lf.ec_b))
                if m_ is not None]
        parts.append(f"member of EtherChannel {bundle.po_text} ({bundle.protocol}, {bundle.bundled}/{bundle.total} "
                     "bundled)" + (" - " + "; ".join(ends) if ends else ""))
        if bundle.stp_block:
            parts.append(f"spanning tree runs on {bundle.stp_block.port}: {label.get(bundle.stp_block.node, '?')} "
                         f"{bundle.stp_block.port} is {bundle.stp_block.role_word} BLOCKING (the whole bundle)")
    elif lf.stp_block:
        parts.append(_stp_tooltip(lf, label))
    return "\n".join(x for x in parts if x)


def node_summary(nk: str, topo: Topology, facts: dict[str, NodeFacts], join: JoinResult) -> str:
    """Plain-English overview line for the Inspector (V9)."""
    n = topo.nodes.get(nk)
    nf = facts.get(nk)
    if n is None:
        return ""
    if n.role == NodeRole.SWITCH and n.managed:
        return switch_summary(nk, topo, facts, join)
    if n.role != NodeRole.ROUTER:
        return f"{n.label}: {n.role.value}"
    if nf is None:
        return f"{n.label}: no data yet"
    label = {k: x.label for k, x in topo.nodes.items()}
    parts = []
    if nf.ospf_running and nf.ospf_running.value:
        full = [x for x in (nf.ospf_nbrs.value if nf.ospf_nbrs else []) if x.adj_state == "FULL"]
        parts.append(f"OSPF {'ok' if full else 'no neighbours'} ({len(full)} neighbour{'s' if len(full) != 1 else ''})")
    else:
        parts.append("OSPF none")
    if nf.bgp and nf.bgp.value.active:
        up = [p for p in nf.bgp.value.peers if p.established]
        parts.append(f"BGP AS {nf.bgp.value.local_as} ({len(up)}/{len(nf.bgp.value.peers)} peers up)")
    else:
        parts.append("BGP none")
    for g in (nf.fhrp.value if nf.fhrp else []):
        parts.append(f"{g.proto.value} {g.role} grp {g.group}")
    p = join.paths.get(nk)
    if nf.default_route and nf.default_route.value.present:
        dr = nf.default_route.value
        via = None
        if p and p.hops:
            via = label.get(p.hops[0].to_node) if p.hops[0].to_node else None
        parts.append(f"default route via {via or (dr.next_hops[0][0] if dr.next_hops else '?')} ({dr.source_label})")
    else:
        parts.append("no default route")
    for lf in join.links.values():
        if lf.status.kind in DOWN_KINDS and lf.status.side_node == nk:
            parts.append(f"link {lf.status.side_iface} {lf.status.kind.value.lower().replace('_', ' ')}")
    return f"{n.label}: " + ", ".join(parts)


def bridge_owners(topo: Topology, facts: dict[str, NodeFacts]) -> dict[str, str]:
    """STP bridge MAC address -> node key (every managed switch reports its own bridge ID)."""
    out = {}
    for nk, nf in facts.items():
        if nk not in topo.nodes or nf.l2 is None or nf.l2.stp is None:
            continue
        for sv in nf.l2.stp.value.values():
            if sv.bridge_address:
                out[sv.bridge_address] = nk
    return out


def root_names(nk: str, topo: Topology, facts: dict[str, NodeFacts]) -> dict[int, str]:
    """VLAN -> name of the root bridge as seen by switch ``nk`` (lab label, or the MAC if foreign)."""
    nf = facts.get(nk)
    if nf is None or nf.l2 is None or nf.l2.stp is None:
        return {}
    owners = bridge_owners(topo, facts)
    out = {}
    for vid, sv in nf.l2.stp.value.items():
        if sv.is_root:
            out[vid] = topo.nodes[nk].label
        elif sv.root_address:
            ok = owners.get(sv.root_address)
            out[vid] = topo.nodes[ok].label if ok in topo.nodes else sv.root_address
    return out


def switch_summary(nk: str, topo: Topology, facts: dict[str, NodeFacts], join: JoinResult) -> str:
    """'SW2: 2 VLANs, root for none (root is SW1), Gi0/2 blocking (Alternate) for VLAN 10,20 to prevent a loop'"""
    n = topo.nodes[nk]
    nf = facts.get(nk)
    if nf is None or nf.l2 is None or (nf.l2.vlans is None and nf.l2.stp is None):
        return f"{n.label}: managed switch, no data yet"
    l2 = nf.l2
    parts = []
    user = l2.configured_vlans()
    if l2.vlans is not None:
        parts.append(f"{len(user)} VLAN{'s' if len(user) != 1 else ''}")
    roots = root_names(nk, topo, facts)
    stp_vlans = sorted(l2.stp.value) if l2.stp else []
    mine = [v for v in stp_vlans if l2.stp.value[v].is_root]
    if l2.stp is not None:
        if not stp_vlans:
            parts.append("spanning tree has no active VLAN")
        elif mine and len(mine) == len(stp_vlans):
            parts.append(f"root bridge for VLAN {_vlan_list_text(mine)}")
        else:
            others = sorted({roots[v] for v in stp_vlans if v not in mine and v in roots})
            parts.append(("root for none" if not mine else f"root for VLAN {_vlan_list_text(mine)}")
                         + (f" (root is {', '.join(others)})" if others else ""))
        blocked: dict[tuple[str, PortRole], list[int]] = {}
        for vid in stp_vlans:
            for pt in l2.stp.value[vid].ports.values():
                if pt.state == PortState.BLK:
                    blocked.setdefault((pt.port, pt.role), []).append(vid)
        for (port, role), vids in sorted(blocked.items()):
            if role in (PortRole.ALTN, PortRole.BACK):
                parts.append(f"{port} blocking ({ROLE_WORDS.get(role, role.value)}) for VLAN {_vlan_list_text(vids)} "
                             "to prevent a loop")
            else:
                parts.append(f"{port} converging ({ROLE_WORDS.get(role, role.value)}, discarding) for VLAN "
                             f"{_vlan_list_text(vids)}")
    for bd in join.bundles.values():
        side = bd.side(nk)
        if side is None:
            continue
        peer = topo.nodes.get(bd.b if bd.a == nk else bd.a)
        txt = f"{side.po} {bd.protocol} {bd.bundled}/{bd.total}" + (f" to {peer.label}" if peer else "")
        if bd.state == EcState.MISMATCH and bd.mismatch_short:
            txt += f" (⚠ {bd.mismatch_short})"
        elif bd.state in (EcState.PARTIAL, EcState.DOWN):
            txt += f" ({bd.state.value})"
        parts.append(txt)
    if l2.ports is not None:
        errd = [p.port for p in l2.ports.value.values() if p.status == "err-disabled"]
        if errd:
            parts.append("err-disabled: " + ", ".join(errd))
    for lf in join.links.values():
        if lf.status.kind in DOWN_KINDS and lf.status.side_node == nk:
            parts.append(f"link {lf.status.side_iface} {lf.status.kind.value.lower().replace('_', ' ')}")
    return f"{n.label}: " + ", ".join(parts)


def switch_is_l3(nf: NodeFacts | None) -> bool:
    """An L3 switch: 'ip routing' active and at least two SVIs up with an address (it routes between
    VLANs).  A single management SVI does not make it a router."""
    if nf is None or nf.l2 is None or nf.l2.routing is None or not nf.l2.routing.value or nf.ifaces is None:
        return False
    svis = [f for f in nf.ifaces.value.values() if f.name.lower().startswith("vlan") and f.ip and f.side == LinkSide.UP]
    return len(svis) >= 2


def iter_router_keys(topo: Topology) -> Iterable[str]:
    return (k for k, n in topo.nodes.items() if n.role == NodeRole.ROUTER)
