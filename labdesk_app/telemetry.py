"""TelemetryEngine: CLI probing + API state diffs + traffic + link simulation -> TelemetryView.

Threading model (UI never blocks):
* ``update(topo, ...)`` is called from ``LabController.poll()`` (a TaskRunner worker thread,
  every 3 s).  It is cheap: it diffs API states, picks due probe bursts and hands them to the
  engine's own pool of ``MAX_PARALLEL_BURSTS`` daemon threads, then returns the latest
  ``TelemetryView`` (built from whatever facts exist right now).  It never waits on a console.
* Bursts run in the pool; results are merged under ``self._lock``.  The next poll (<= 3 s)
  carries them to the UI.  ``on_change`` (optional callback, called from a pool thread) lets
  the UI request an early poll.
* User actions (``probe_now``, ``refresh_config``, ``ping``, ``traceroute``, ``verify_internet``,
  ``pull_cable``, ``restore_cable``, ``set_condition``...) are blocking calls meant to be
  submitted through the UI's TaskRunner.  They take the per-node lock with priority over
  background bursts.

D2 cadence (per router):
  FAST   every 15 s : interfaces, OSPF ifaces+neighbours, HSRP, VRRP, BGP summary, default route+CEF
  SLOW   every 60 s : interface descriptions, 'show ip protocols' (passive ifaces, RID), connected routes
  CONFIG on demand, once after learning when privilege 15 is available without typing a secret,
         after a SYS-5-CONFIG_I syslog, and every 10 min : show running-config (redacted)
Facts older than FRESH_S (2 x FAST) are not trusted (link side -> UNKNOWN, glyphs fade).

See docs/telemetry_model.md for the exact public model.
"""
from __future__ import annotations

import copy
import dataclasses
import os
import queue
import re
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import accounts as accounts_mod
from . import config, plat
from .catalog import BY_KEY, type_for_cml_node
from .cml_api import CMLError
from .facts import (DOWN_KINDS, EC_CMDS, L2_CMDS, MANAGED_SWITCH_DEFS, ROLE_WORDS, STP_MODE_LABELS, AddrInfo, CollectorState,
                    ConfigFacts, DefaultPath, DefaultRoute, Fact, FhrpGroup, Health, IfFact, JoinResult, LinkFacts,
                    LinkKind, NodeFacts, NodeRole, PathState, PortMode, PortState, Proto, Segment, StpVlanInfo,
                    SubnetState, TopoLink, TopoNode, Topology, VlanInfo, apply_addr_cmd, effective_addrs,
                    ios_facts_update, join_facts, l2_facts_update, node_summary, root_names, subnet_changes,
                    switch_is_l3, topo_role, ec_facts_update, junos_facts_update, Bundle, EcState)  # noqa: F401
from .parsers import console_tap as P_TAP
from .parsers import ios as P_IOS
from .parsers import ios_l2 as P_L2
from .parsers import junos as P_JUNOS
from .parsers.common import SyslogLine, iface_key, short_iface
from .probe import creds as creds_mod
from .probe.redact import redact, redact_values
from .probe.session import CONFIG_KEY, SAVE_KEY, BurstResult, BurstStatus, IosDialect, run_burst
from .probe.transport import TermserverPty, default_transport  # noqa: F401  (TermserverPty: tests / API)

# ----------------------------------------------------------------------- cadence (D2)
FAST_S = 15.0
SLOW_S = 60.0
CONFIG_S = 600.0
FRESH_S = 2 * FAST_S + 15.0          # 45 s: 2 x FAST + burst/queue grace (serial console ~400 B/s)
FADE_FACTOR = 3                      # other CLI overlays fade at 3x their tier interval
MAX_PARALLEL_BURSTS = 3
BURST_TIMEOUT_S = 20.0
CONFIG_TIMEOUT_S = 45.0              # FAST + enable + show running-config: ~27 s measured live on IOSv
TRACE_TIMEOUT_S = 30.0
VERIFY_TARGETS = ("8.8.8.8", "1.1.1.1")   # Verify Internet pings both (the Internet cloud answers them)
BACKOFF_MAX_S = 120.0
FULL_SNAPSHOT_S = 30.0
VERIFY_TTL_S = 300.0
TRAFFIC_VISIBLE_S = 3.0
TRAFFIC_VISIBLE_SLOW_S = 5.0          # if simulation_stats takes > 300 ms
TRAFFIC_SELECTED_S = 10.0
SPARK_POINTS = 60
FLAP_WINDOW_S = 30.0
EVENT_KEEP = 500
TAP_S = 1.0                           # console-tap cadence while a node's console is open (CML console log API)
TAP_LINES = 200                       # console log lines fetched per tap read (~17 ms on CML 2.10.1)

IOS_FAST = ["show ip interface brief", "show ip ospf interface brief", "show ip ospf neighbor",
            "show standby brief", "show vrrp brief", "show ip bgp summary", "show ip route 0.0.0.0",
            "show ip cef 0.0.0.0/0", P_IOS.IP_ADDR_CMD]
IOS_SLOW = ["show interfaces description", "show ip protocols", "show ip route connected"]
IOS_CONFIG = ["show running-config"]

# Managed switches (IOSvL2): every command verified live at the user-exec '>' prompt.  The FAST set
# carries what changes the picture (port up/down, STP role/state, trunks, VLANs, TC counters);
# measured ~7 s on the serial console, the SLOW set ~9 s.
SW_FAST = ["show ip interface brief", "show interfaces status", "show interfaces trunk", "show spanning-tree",
           "show vlan brief", L2_CMDS["tc"], P_IOS.IP_ADDR_CMD]
SW_SLOW = ["show spanning-tree summary", "show mac address-table count", "show mac address-table dynamic",
           "show cdp neighbors", "show etherchannel summary", "show interfaces description",
           "show ip route connected"]
SWITCH_BURST_TIMEOUT_S = 30.0
# EtherChannel (verified at '>' on IOSvL2, ~1 s each): the summary rides in FAST once a channel-group exists
# (member bundled / unbundled, Po up / down are what changes the picture); the protocol tables only for the
# protocols in use, in SLOW.  A router is asked only when it has a Port-channel interface.
EC_FAST = [EC_CMDS["summary"]]
EC_SLOW_LACP = [EC_CMDS["lacp_internal"], EC_CMDS["lacp_neighbor"], EC_CMDS["lacp_sys"]]
EC_SLOW_PAGP = [EC_CMDS["pagp_internal"], EC_CMDS["pagp_neighbor"]]
EC_SLOW_ANY = [EC_CMDS["load_balance"]]
EC_EXTRA_TIMEOUT_S = 10.0

IOS_PARSERS = {
    "show ip interface brief": P_IOS.parse_ip_int_brief,
    "show ip ospf interface brief": P_IOS.parse_ospf_int_brief,
    "show ip ospf neighbor": P_IOS.parse_ospf_neighbors,
    "show standby brief": P_IOS.parse_standby_brief,
    "show vrrp brief": P_IOS.parse_vrrp_brief,
    "show ip bgp summary": P_IOS.parse_bgp_summary,
    "show ip route 0.0.0.0": P_IOS.parse_ip_route_default,
    "show ip cef 0.0.0.0/0": P_IOS.parse_ip_cef_default,
    "show interfaces description": P_IOS.parse_interfaces_description,
    "show ip protocols": P_IOS.parse_ip_protocols,
    "show ip route connected": P_IOS.parse_ip_route,
    "show running-config": P_IOS.parse_running_config,
    P_IOS.IP_ADDR_CMD: P_IOS.parse_ip_interface_addrs,
    # L2 (managed switches)
    "show vlan brief": P_L2.parse_vlan_brief,
    "show interfaces trunk": P_L2.parse_interfaces_trunk,
    "show interfaces status": P_L2.parse_switchport_status,
    "show spanning-tree": P_L2.parse_spanning_tree,
    "show spanning-tree summary": P_L2.parse_spanning_tree_summary,
    "show mac address-table count": P_L2.parse_mac_count,
    "show mac address-table dynamic": P_L2.parse_mac_table,
    "show cdp neighbors": P_L2.parse_cdp_neighbors,
    "show etherchannel summary": P_L2.parse_etherchannel_summary,
    L2_CMDS["tc"]: P_L2.parse_stp_topology_changes,
    # EtherChannel (switches; routers with a Port-channel)
    EC_CMDS["lacp_internal"]: P_L2.parse_lacp_internal,
    EC_CMDS["lacp_neighbor"]: P_L2.parse_lacp_neighbor,
    EC_CMDS["lacp_sys"]: P_L2.parse_lacp_sys_id,
    EC_CMDS["pagp_internal"]: P_L2.parse_pagp_internal,
    EC_CMDS["pagp_neighbor"]: P_L2.parse_pagp_neighbor,
    EC_CMDS["load_balance"]: P_L2.parse_etherchannel_load_balance,
}

# D6 (specified; collectors not wired until a live VM validates them)
JUNOS_COMMANDS = {
    "fast": ["show interfaces terse | display json", "show ospf neighbor | display json",
             "show bgp summary | display json", "show vrrp summary | display json",
             "show route 0.0.0.0/0 exact | display json"],
    "slow": ["show ospf interface detail | display json"],
    "config": ["show configuration protocols | display json"],
    "accounts": accounts_mod.JUNOS_ACCOUNT_CMDS,         # parsed by accounts.parse_junos_accounts
}
JUNOS_MAC_CMD = 'show interfaces ge-* | match "Physical interface|Current address"'
JUNOS_AS_CMD = "show configuration routing-options autonomous-system | display json"
JUNOS_LIVE = {          # what the wired vJunos collector actually runs (read-only, ~1.5 s over SSH)
    "fast": ["show interfaces terse | display json", "show bgp summary | display json",
             "show route 0.0.0.0/0 exact | display json", "show ospf neighbor | display json", JUNOS_MAC_CMD],
    "slow": [JUNOS_AS_CMD] + accounts_mod.JUNOS_ACCOUNT_CMDS,
}
JUNOS_PARSERS = {
    "show interfaces terse | display json": P_JUNOS.parse_interfaces_terse_json,
    "show bgp summary | display json": P_JUNOS.parse_bgp_summary_json,
    "show route 0.0.0.0/0 exact | display json": P_JUNOS.parse_default_route,
    "show ospf neighbor | display json": P_JUNOS.parse_ospf_neighbors_json,
}
FORTIOS_COMMANDS = {
    "fast": ["get system interface physical", "get router info ospf neighbor", "get router info bgp summary",
             "get router info vrrp"],
    "slow": ["get router info ospf interface", "get router info routing-table details 0.0.0.0/0",
             "get system status"],
    "accounts": accounts_mod.FORTIOS_ACCOUNT_CMDS,       # parsed by accounts.parse_fortios_accounts
}

LINK_CONDITION_PRESETS: dict[str, dict] = {
    "LAN (clean)": {},
    "WAN 50 ms": {"latency": 50, "jitter": 5},
    "Intercontinental 150 ms": {"latency": 150, "jitter": 10},
    "Satellite 600 ms": {"latency": 600, "jitter": 20, "bandwidth": 2000},
    "DSL 8 Mbit/s": {"bandwidth": 8000, "latency": 20},
    "Lossy 5 %": {"loss": 5},
    "Bad Wi-Fi": {"latency": 30, "jitter": 40, "loss": 3, "reorder_prob": 1},
    "Congested 1 Mbit/s": {"bandwidth": 1000, "latency": 80, "loss": 1, "limit": 100},
}


# ======================================================================= view model
@dataclass
class Chip:
    kind: str                     # 'OSPF' | 'BGP' | 'DEFAULT'
    text: str                     # 'OSPF' | 'BGP 65000' | 'O*E2' | '0/0 no exit'
    health: Health
    glyph: str                    # '✓' | '…' | '!' | '✕' | '?' | '⏱' | ''
    tooltip: str
    stale: bool = False           # older than FRESH_S: glyph not trusted
    faded: bool = False           # older than FADE_FACTOR x interval: render 40 % + ⏱
    outline: bool = False         # route-source chips are outline (non-filled, neutral text)
    blue_bar: bool = False        # default chip: fresh unbroken path to an egress
    age_s: float | None = None


@dataclass
class FhrpBadge:
    proto: Proto
    group: int
    role: str                     # Active | Standby | Master | Backup | Speak | Listen | Init
    primary: bool
    iface: str                    # short name
    vip: str | None
    priority: int | None
    tooltip: str
    stale: bool = False


@dataclass
class SwitchView:
    """Managed-switch model for the canvas and the Inspector (VLANs / Ports / Spanning Tree / MAC)."""
    stp_mode: str | None = None               # 'rapid-pvst'
    stp_label: str | None = None              # 'RPVST+'
    l3: bool = False                          # ip routing + >= 2 SVIs up (L3 badge)
    routing: bool | None = None
    vlans: list[VlanInfo] = field(default_factory=list)            # user VLANs (1002-1005 hidden)
    root_for: list[int] = field(default_factory=list)              # VLANs this switch is root for
    root_of: dict[int, str] = field(default_factory=dict)          # VLAN -> root bridge name
    stp: list[StpVlanInfo] = field(default_factory=list)
    tc: dict[int, object] = field(default_factory=dict)            # VLAN -> facts.TcInfo
    ports: list[dict] = field(default_factory=list)   # {port, desc, status, mode, vlan_text, stp_text, blocked,
    #                                                    err_disabled, side, neighbor}
    blocked: list[tuple[str, str, list[int]]] = field(default_factory=list)   # (port, role word, VLANs)
    err_disabled: list[str] = field(default_factory=list)
    svis: list[IfFact] = field(default_factory=list)
    mac_counts: dict[int, int] = field(default_factory=dict)
    mac_total: int | None = None
    mac_table: list[dict] = field(default_factory=list)
    cdp: list[dict] = field(default_factory=list)
    etherchannel: dict | None = None
    ec_sides: list = field(default_factory=list)      # [facts.EcSide] this switch's Port-channels (EtherChannel tab)
    ec_bundles: dict = field(default_factory=dict)    # Po ('Po1') -> Bundle.key (select the bundle on the canvas)
    ages: dict[str, float | None] = field(default_factory=dict)


@dataclass
class NodeTelemetry:
    key: str
    label: str
    role: NodeRole
    vendor: str
    collector_state: CollectorState
    collector_detail: str = ""
    probing: bool = False                 # a burst is running right now (tile spinner)
    last_probe_age_s: float | None = None
    hostname: str | None = None
    rid: str | None = None
    loopback: str | None = None
    chips: list[Chip] = field(default_factory=list)
    fhrp_badges: list[FhrpBadge] = field(default_factory=list)
    default_route: DefaultRoute | None = None
    default_path: DefaultPath | None = None
    ifaces: list[IfFact] = field(default_factory=list)
    ospf_nbrs: list[dict] = field(default_factory=list)   # {neighbor_id, name, state, iface, address}
    bgp: dict | None = None                                # {local_as, rid, peers:[{ip, name, remote_as, state, ...}]}
    fhrp: list[FhrpGroup] = field(default_factory=list)
    config_text: str | None = None                         # REDACTED running-config
    config_age_s: float | None = None
    summary: str = ""
    ages: dict[str, float | None] = field(default_factory=dict)   # per fact age in seconds
    console_open: bool = False
    probe_while_open: bool = False
    switch: SwitchView | None = None                       # managed switches only
    addrs: dict[str, AddrInfo] = field(default_factory=dict)   # iface_key -> effective addressing (all sources)
    # on-device login accounts (accounts.DeviceAccounts) parsed from the running-config of the CONFIG tier; the
    # plain-text passwords in it are for the hover card only (never logged); None = not read (see accounts_note)
    accounts: object = None
    accounts_age_s: float | None = None
    accounts_note: str = ""


@dataclass
class LinkTraffic:
    bps: float
    pps: float
    drops: int                    # drops in the last interval
    ts: float
    history: list[float] = field(default_factory=list)   # bps, only for the selected link
    directional: bool = False     # read/write direction not yet verified -> non-directional


@dataclass
class LinkCondition:
    enabled: bool
    params: dict                  # applied (operational) values, None entries dropped
    label: str                    # '50 ms · 5 % loss' (empty when disabled)
    ts: float


@dataclass
class TelemetryEvent:
    seq: int
    ts: float                     # host time.time()
    device_ts: str | None         # router clock text from syslog when available
    kind: str                     # 'LINK DOWN' | 'LINK UP' | 'FLAPPING' | 'OSPF' | 'BGP' | 'HSRP' | 'VRRP' |
    #                               'DEFAULT' | 'SIM' | 'PROBE' | 'SYSLOG' | 'CABLE'
    proto: str                    # hue key: link | ospf | bgp | hsrp | vrrp | default | sim | probe
    severity: str                 # ok | info | warn | error
    text: str
    items: list[str] = field(default_factory=list)   # node/link keys to flash (800 ms)

    @property
    def line(self) -> str:
        t = self.device_ts or time.strftime("%H:%M:%S", time.localtime(self.ts))
        return f"{t} {self.kind} {self.text}"


@dataclass
class TelemetryView:
    ts: float
    lab_id: str
    probing_enabled: bool
    nodes: dict[str, NodeTelemetry]
    links: dict[str, LinkFacts]
    segments: dict[str, Segment]
    paths: dict[str, DefaultPath]
    traffic: dict[str, LinkTraffic]
    conditions: dict[str, LinkCondition]
    last_event_seq: int
    capabilities: dict[str, bool]
    rid_map: dict[str, str] = field(default_factory=dict)
    fresh_s: float = FRESH_S
    show_probe_notice: bool = False      # one-time 'probe output is visible on open consoles'
    bundles: dict[str, Bundle] = field(default_factory=dict)   # EtherChannels (facts.Bundle), key = Bundle.key


@dataclass
class ActionResult:
    ok: bool
    text: str
    data: dict = field(default_factory=dict)


# ======================================================================= helpers
_UUID_RX = re.compile(rb"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def console_uuids_in(argv: list[bytes]) -> set[str]:
    """The console uuid of an ssh '... connect <uuid>' argv (empty for anything else)."""
    out: set[str] = set()
    if not argv:
        return out
    prog = argv[0].lower()
    if not (prog.endswith(b"ssh") or prog.endswith(b"ssh.exe")):
        return out
    for i, a in enumerate(argv[:-1]):
        if a == b"connect" and _UUID_RX.match(argv[i + 1]):
            out.add(argv[i + 1].decode())
    return out


def _windows_console_uuids(ex: set[int]) -> set[str]:
    """Windows: the same scan with psutil (ssh.exe processes in Windows Terminal tabs)."""
    out: set[str] = set()
    try:
        import psutil
    except ImportError:
        return out
    for proc in psutil.process_iter(["pid", "name"]):
        try:
            if proc.info["pid"] in ex or (proc.info["name"] or "").lower() != "ssh.exe":
                continue
            out |= console_uuids_in([a.encode("utf-8", "replace") for a in proc.cmdline()])
        except (psutil.Error, OSError):
            continue
    return out


def open_console_uuids(exclude_pids: set[int] | None = None) -> set[str]:
    """Console uuids with an ssh 'connect <uuid>' process on this host (LabDesk Konsole tabs
    and the user's own ssh).  Cheap /proc scan; our own probe pids are excluded."""
    out = set()
    ex = exclude_pids or set()
    if plat.IS_WINDOWS:
        return _windows_console_uuids(ex)
    try:
        pids = [p for p in os.listdir("/proc") if p.isdigit()]
    except OSError:
        return out
    for p in pids:
        if int(p) in ex:
            continue
        try:
            with open(f"/proc/{p}/cmdline", "rb") as fh:
                argv = fh.read().split(b"\0")
        except OSError:
            continue
        if not argv or not argv[0].endswith(b"ssh"):
            continue
        out |= console_uuids_in(argv)
    return out


class _Pool:
    """Tiny daemon-thread worker pool (does not block interpreter exit)."""

    def __init__(self, n: int):
        self.q: queue.Queue = queue.Queue()
        self.threads = [threading.Thread(target=self._run, daemon=True, name=f"labdesk-probe-{i}") for i in range(n)]
        for t in self.threads:
            t.start()

    def _run(self):
        while True:
            fn = self.q.get()
            if fn is None:
                return
            try:
                fn()
            except Exception:       # noqa: BLE001 - a burst must never kill the pool
                pass

    def submit(self, fn):
        self.q.put(fn)

    def stop(self):
        for _ in self.threads:
            self.q.put(None)


@dataclass
class _Sched:
    next_fast: float = 0.0
    next_slow: float = 0.0
    next_config: float = 0.0
    config_wanted: bool = True
    failures: int = 0
    needs_recovery: bool = False
    running: bool = False
    user_waiting: int = 0
    last_status: str = ""
    learned: bool = False
    priv_seen: bool = False          # the line was at '#' (config readable without a secret)


class _LinkLog:
    """D4 hysteresis for link events (logging only)."""

    def __init__(self):
        self.logged: LinkKind | None = None
        self.cand: LinkKind | None = None
        self.cand_count = 0
        self.cand_sig = None
        self.transitions: deque = deque()
        self.flapping_until = 0.0


# ======================================================================= engine
class TelemetryEngine:
    def __init__(self, client=None, *, transport_factory: Callable | None = None,
                 creds_loader: Callable | None = None, now: Callable[[], float] = time.time,
                 workers: int = MAX_PARALLEL_BURSTS, probe_log_dir: Path | None = None,
                 console_scan: Callable[[set], set] | None = None, autostart: bool = True):
        self.client = client
        self.now = now
        self.transport_factory = transport_factory or (lambda uuid, user: default_transport(uuid, user))
        self.creds_loader = creds_loader or creds_mod.load
        self.console_scan = console_scan or open_console_uuids
        self.probe_log_dir = Path(probe_log_dir) if probe_log_dir else config.CACHE_DIR / "probe"
        self._lock = threading.RLock()
        self._node_locks: dict[str, threading.Lock] = {}
        self._workers = workers
        self._pool: _Pool | None = None
        self._autostart = autostart
        self.lab_id: str | None = None
        self.facts: dict[str, NodeFacts] = {}
        self.sched: dict[str, _Sched] = {}
        self.link_history: dict[str, dict] = {}
        self.topo: Topology | None = None
        self.console_uuid: dict[str, str] = {}
        self.cml_user = "admin"
        self.probing_enabled = True
        self.probe_while_open: set[str] = set()
        self.one_shot: set[str] = set()
        self.console_open: set[str] = set()
        self._own_pids: set[int] = set()
        self._probe_notice_shown = False
        self.verified_internet: dict[str, float] = {}
        self.events: deque[TelemetryEvent] = deque(maxlen=EVENT_KEEP)
        self._seq = 0
        self._linklog: dict[str, _LinkLog] = {}
        self._path_state: dict[str, tuple] = {}
        self._syslog_recent: dict[tuple, float] = {}
        self._view: TelemetryView | None = None
        self.on_change: Callable[[], None] | None = None
        # traffic / conditions
        self.traffic_layer = False
        self.selected_link: str | None = None
        self._traffic_prev: dict[str, tuple[float, int, int, int]] = {}
        self.traffic: dict[str, LinkTraffic] = {}
        self._spark: dict[str, deque] = {}
        self._next_traffic = 0.0
        self._traffic_slow = False
        self.conditions: dict[str, LinkCondition] = {}
        self._conditions_loaded_for: frozenset = frozenset()
        self.capabilities = {"element_state_interfaces": True, "link_conditioning": True, "packet_capture": True,
                             "kvm_collectors": False}
        self._secrets: list[str] = []
        self._secrets_mtime: float | None = None
        self._device_secrets: set[str] = set()          # plain-text passwords seen in device configs
        self._live_transports: set = set()
        # subnets on cables: console tap (CML console log), address event baselines, CML L3 snooping
        self._tap_state: dict[str, P_TAP.TapState] = {}
        self._tap_final: set[str] = set()          # one more tap read after a console closed / Refresh
        self._tap_thread: threading.Thread | None = None
        self._tap_wake = threading.Event()
        self._tap_stop = False
        self.tap_ok: dict[str, float] = {}         # node key -> last successful console-log read
        self._addr_state: dict[str, dict] = {}     # node key -> effective addressing (event baseline)
        self._mismatch: dict[str, str | None] = {}  # link key -> mismatch label last logged
        self._ec_state: dict[frozenset, dict] = {}  # bundle member set -> last logged snapshot (ETHERCHANNEL events)
        self._ec_rescan: set[str] = set()           # nodes whose EtherChannel protocol set just changed
        # host-KVM vJunos: live read-only SSH collector (probe/junos_ssh.py)
        self.kvm_mgmt: dict[str, str] = {}          # node key -> management IP (state.json mgmt / kvm.mgmt_ip)
        self.junos_runner: Callable | None = None   # tests: (host, user, password, cmds, timeout) -> JunosBurst
        self.junos_creds: Callable | None = None    # tests: label -> (user, password, source)
        import atexit
        import weakref
        ref = weakref.ref(self)
        atexit.register(lambda: ref() and ref().shutdown())

    # ------------------------------------------------------------- settings / lifecycle
    def _ensure_pool(self):
        if self._pool is None:
            self._pool = _Pool(self._workers)

    def shutdown(self) -> None:
        """Stop the pool and kill every in-flight console ssh (never leave a session open)."""
        self._tap_stop = True
        self._tap_wake.set()
        if self._pool:
            self._pool.stop()
            self._pool = None
        for t in list(self._live_transports):
            try:
                t.close()
            except Exception:       # noqa: BLE001
                pass
        self._live_transports.clear()

    def reset(self, lab_id: str | None) -> None:
        with self._lock:
            self.lab_id = lab_id
            self.facts.clear()
            self.sched.clear()
            self.link_history.clear()
            self.topo = None
            self.console_uuid.clear()
            self.verified_internet.clear()
            self._linklog.clear()
            self._path_state.clear()
            self.traffic.clear()
            self._traffic_prev.clear()
            self._spark.clear()
            self.conditions.clear()
            self._conditions_loaded_for = frozenset()
            self._view = None
            self._tap_state.clear()
            self._tap_final.clear()
            self.tap_ok.clear()
            self._addr_state.clear()
            self._mismatch.clear()
            self._ec_state.clear()

    def set_probing(self, on: bool) -> None:
        self.probing_enabled = bool(on)
        self._event("PROBE", "probe", "info", "probing " + ("on" if on else "off"))

    def set_probe_while_open(self, node_key: str, on: bool) -> None:
        (self.probe_while_open.add if on else self.probe_while_open.discard)(node_key)

    def set_traffic_layer(self, on: bool) -> None:
        self.traffic_layer = bool(on)
        self._next_traffic = 0.0

    def set_selected_link(self, link_key: str | None) -> None:
        self.selected_link = link_key
        self._next_traffic = 0.0

    def acknowledge_probe_notice(self) -> None:
        self._probe_notice_shown = True

    def view(self) -> TelemetryView | None:
        return self._view

    def events_since(self, seq: int) -> list[TelemetryEvent]:
        with self._lock:
            return [e for e in self.events if e.seq > seq]

    # ------------------------------------------------------------- events
    def _event(self, kind, proto, severity, text, items=None, device_ts=None) -> TelemetryEvent:
        with self._lock:
            self._seq += 1
            ev = TelemetryEvent(self._seq, self.now(), device_ts, kind, proto, severity, text, list(items or []))
            self.events.append(ev)
            return ev

    # ------------------------------------------------------------- main entry (poll thread)
    def update(self, topo: Topology, lab_id: str, console_keys: dict[str, str] | None = None,
               full_snapshot: bool = False) -> TelemetryView:
        now = self.now()
        with self._lock:
            if lab_id != self.lab_id:
                self.reset(lab_id)
            prev = self.topo
            self.topo = topo
            for k, v in (console_keys or {}).items():
                if v:
                    self.console_uuid[k] = v
            self._api_transitions(prev, topo, now)
        # conditions (D7: once per link set so D8's indicator is truthful)
        cml_links = frozenset(k for k, l in topo.links.items() if not l.kvm)
        if self.client is not None and cml_links and cml_links != self._conditions_loaded_for:
            self._load_conditions(cml_links)
        if self.client is not None:
            self._poll_traffic(topo, now)
        # console etiquette + scheduling
        if any(n.collected for n in topo.nodes.values()):
            before = self.console_open
            self.console_open = self.console_scan(set(self._own_pids))
            closed = before - self.console_open
            if closed:
                with self._lock:
                    gone = [nk for nk, u in self.console_uuid.items() if u in closed and nk in topo.nodes]
                    self._kick(gone)                  # the line is free again: re-read it now
                    self._tap_final.update(gone)      # and catch what was typed just before closing
            if self.client is not None and (self._tap_targets(topo) or self._tap_final):
                self._ensure_tap()
            self._schedule(topo, now)
        view = self._build_view(topo, now)
        self._view = view
        return view

    # ------------------------------------------------------------- API transitions (D7)
    def _api_transitions(self, prev: Topology | None, topo: Topology, now: float) -> None:
        if prev is None:
            return
        for lk, l in topo.links.items():
            pl = prev.links.get(lk)
            if pl is None or l.kvm:
                continue
            if pl.api_state == "STARTED" and l.api_state != "STARTED":
                na, nb = topo.nodes.get(l.a), topo.nodes.get(l.b)
                if na and nb and na.booted and nb.booted:
                    self.link_history.setdefault(lk, {})["observed_stop"] = now
                self._kick([l.a, l.b])
            elif pl.api_state != "STARTED" and l.api_state == "STARTED":
                h = self.link_history.get(lk, {})
                for k in ("observed_stop", "pulled_by_labdesk", "api_stop"):
                    h.pop(k, None)
                self._kick([l.a, l.b])
        for nk, n in topo.nodes.items():
            pn = prev.nodes.get(nk)
            if pn and pn.state != n.state:
                if n.booted:
                    s = self.sched.setdefault(nk, _Sched())
                    s.next_fast = s.next_slow = 0.0
                    s.failures = 0
                    s.learned = False
                else:
                    self.facts.pop(nk, None)
                    self.sched.pop(nk, None)

    def interfaces_changed(self, node_keys: list[str]) -> None:
        """Controller hook: CML interface state transitions -> immediate FAST on those nodes."""
        with self._lock:
            self._kick(node_keys)

    def _kick(self, node_keys) -> None:
        for nk in node_keys:
            if nk in self.sched:
                self.sched[nk].next_fast = 0.0
                self.sched[nk].failures = 0

    # ------------------------------------------------------------- scheduling (D2/D10)
    def _paused(self, nk: str) -> bool:
        uuid = self.console_uuid.get(nk)
        return bool(uuid and uuid in self.console_open and nk not in self.probe_while_open
                    and nk not in self.one_shot)

    def _schedule(self, topo: Topology, now: float) -> None:
        if not self.probing_enabled:
            return
        for nk, n in topo.nodes.items():
            if not n.collected or not n.booted:
                continue
            if nk.startswith("kvm:"):
                if n.vendor == "juniper" and self.kvm_mgmt.get(nk):
                    with self._lock:
                        s = self.sched.setdefault(nk, _Sched())
                        if s.running or now < s.next_fast:
                            continue
                        s.running = True
                        slow = now >= s.next_slow
                    self._ensure_pool()
                    self._pool.submit(lambda nk=nk, slow=slow: self._run_junos(nk, slow))
                continue                                   # FortiOS: no live collector yet
            fast_cmds, slow_cmds = command_sets(n, self.facts.get(nk))
            with self._lock:
                s = self.sched.setdefault(nk, _Sched())
                if s.running or s.user_waiting or self._paused(nk):
                    continue
                if not self.console_uuid.get(nk):
                    continue
                cmds, tier = [], []
                if s.learned and s.config_wanted and now >= s.next_config:
                    tier = ["config"]
                elif now >= s.next_fast:
                    cmds += fast_cmds
                    tier.append("fast")
                if "config" not in tier and now >= s.next_slow and (tier or not s.learned):
                    cmds += slow_cmds
                    tier.append("slow")
                if not tier:
                    continue
                s.running = True
            self._ensure_pool()
            self._pool.submit(lambda nk=nk, cmds=cmds, tier=tier: self._run_background(nk, cmds, tier))

    def _node_lock(self, nk: str) -> threading.Lock:
        with self._lock:
            return self._node_locks.setdefault(nk, threading.Lock())

    def _run_background(self, nk: str, cmds: list[str], tiers: list[str]) -> None:
        lock = self._node_lock(nk)
        try:
            if not lock.acquire(timeout=1.0):
                return
            try:
                n = self.topo.nodes.get(nk) if self.topo else None
                fast_cmds = command_sets(n, self.facts.get(nk))[0] if n is not None else IOS_FAST
                if "config" in tiers:
                    # FAST rides along so facts never lapse while the slow config read runs
                    self._burst(nk, fast_cmds + IOS_CONFIG, timeout=CONFIG_TIMEOUT_S + _extra_timeout(n, self.facts.get(nk)),
                                tiers=["fast", "config"])
                else:
                    self._burst(nk, cmds, timeout=BURST_TIMEOUT_S + _extra_timeout(n, self.facts.get(nk)), tiers=tiers)
            finally:
                lock.release()
        finally:
            with self._lock:
                s = self.sched.get(nk)
                if s:
                    s.running = False
                self.one_shot.discard(nk)
            cb = self.on_change
            if cb:
                try:
                    cb()
                except Exception:
                    pass

    def _burst(self, nk: str, cmds: list[str], *, timeout: float, tiers: list[str],
               user: bool = False, config_lines: list[str] | None = None, save_config: bool = False,
               enable_fallback: str | None = None, plain: bool = False,
               stay_logged_in: bool = False, cmd_timeout: float | None = None) -> BurstResult | None:
        topo = self.topo
        n = topo.nodes.get(nk) if topo else None
        uuid = self.console_uuid.get(nk)
        if n is None or not uuid:
            return None
        s = self.sched.setdefault(nk, _Sched())
        try:
            creds = self.creds_loader(n.label)
        except creds_mod.CredsError as e:
            with self._lock:
                nf = self.facts.setdefault(nk, NodeFacts(nk, n.label, n.vendor))
                nf.collector_state, nf.collector_detail = CollectorState.NEEDS_CREDENTIALS, str(e)
            return None
        if enable_fallback and not creds.enable:
            creds = creds_mod.DeviceCreds(creds.username, creds.password, enable_fallback)
        started = self.now()                 # cadence is start-to-start (D2)
        t = self.transport_factory(uuid, self.cml_user)
        try:
            t.start()
        except Exception as e:   # noqa: BLE001
            self._burst_failed(nk, s, f"ssh start failed: {e}", now=self.now())
            return None
        pid = getattr(t, "pid", None)
        if pid:
            self._own_pids.add(pid)
        self._live_transports.add(t)
        try:
            res = run_burst(t, IosDialect(), cmds, creds, timeout=timeout, needs_recovery=s.needs_recovery,
                            expect_hostname=n.label, start=False, config_lines=config_lines,
                            save_config=save_config, plain=plain, stay_logged_in=stay_logged_in,
                            **({"cmd_timeout": cmd_timeout} if cmd_timeout else {}))
        finally:
            self._live_transports.discard(t)
            if pid:
                self._own_pids.discard(pid)
        now = self.now()
        self._learn_device_secrets(res)
        self._write_probe_log(n.label, res, cmds)
        s.needs_recovery = res.needs_recovery
        if _only_needs_secret(res):
            # the line sits at '>' and no enable secret is stored: the running-config simply is not
            # readable - not a probe error (don't back off, don't retry the config tier every burst)
            res.status = BurstStatus.OK
            res.reason = "running-config needs an enable secret (device-creds)"
        s.last_status = res.status
        if res.status in (BurstStatus.OK, BurstStatus.PARTIAL):
            self._merge(nk, n, res, now, tiers)
            s.failures = 0 if res.status == BurstStatus.OK else s.failures + 1
            if "fast" in tiers:
                s.next_fast = started + FAST_S
            if "slow" in tiers:
                s.next_slow = started + SLOW_S
            if nk in self._ec_rescan:
                self._ec_rescan.discard(nk)
                s.next_slow = 0.0
                s.next_fast = min(s.next_fast, now + 1.0)
            if "config" in tiers:
                s.next_config = started + CONFIG_S
                s.config_wanted = False
            if res.mode_found == "priv":
                s.priv_seen = True
            if not s.learned and "fast" in tiers:
                s.learned = True
                # CONFIG once after learning, only when no secret has to be typed
                if s.priv_seen or creds.enable:
                    # staggered so that not every router reads its config at the same moment
                    s.config_wanted = True
                    s.next_config = now + 5.0 + 12.0 * (sorted(self.sched).index(nk) if nk in self.sched else 0)
            if res.status == BurstStatus.PARTIAL:
                s.next_fast = now + min(BACKOFF_MAX_S, FAST_S * (2 ** min(s.failures, 3)))
        else:
            self._burst_failed(nk, s, res.reason, now, res.status)
        return res

    def _burst_failed(self, nk, s: _Sched, reason: str, now: float, status: str = BurstStatus.ERROR):
        s.failures += 1
        delay = min(BACKOFF_MAX_S, FAST_S * (2 ** min(s.failures, 4)))
        s.next_fast = now + delay
        with self._lock:
            n = self.topo.nodes.get(nk) if self.topo else None
            nf = self.facts.setdefault(nk, NodeFacts(nk, n.label if n else nk, n.vendor if n else "cisco"))
            nf.collector_state = {BurstStatus.BUSY: CollectorState.CONSOLE_BUSY,
                                  BurstStatus.NEEDS_CREDS: CollectorState.NEEDS_CREDENTIALS}.get(
                status, CollectorState.ERROR)
            nf.collector_detail = f"{reason} (retry in {int(delay)} s)"
        if s.failures == 1 or status == BurstStatus.TIMEOUT:
            self._event("PROBE", "probe", "warn", f"{nf.label}: {reason}", [nk])

    # ------------------------------------------------------------- host-KVM vJunos (live, read-only SSH)
    def _run_junos(self, nk: str, slow: bool) -> None:
        try:
            self._junos_burst(nk, slow)
        finally:
            with self._lock:
                s = self.sched.get(nk)
                if s:
                    s.running = False
            cb = self.on_change
            if cb:
                try:
                    cb()
                except Exception:
                    pass

    def _junos_burst(self, nk: str, slow: bool) -> None:
        from .probe import junos_ssh as JS
        topo = self.topo
        n = topo.nodes.get(nk) if topo else None
        host = self.kvm_mgmt.get(nk)
        if n is None or not host:
            return
        s = self.sched.setdefault(nk, _Sched())
        if self.junos_creds is not None:
            user, pw, _src = self.junos_creds(n.label)
        else:
            try:
                det = creds_mod.load_detail(n.label)
            except Exception:  # noqa: BLE001
                det = None
            user, pw, _src = JS.junos_creds(n.label, det)
        if not user or not pw:
            with self._lock:
                nf = self.facts.setdefault(nk, NodeFacts(nk, n.label, n.vendor))
                nf.collector_state = CollectorState.NEEDS_CREDENTIALS
                nf.collector_detail = "no Junos login saved (device-creds or JUNOS_ADMIN_* in cml-credentials.env)"
            s.next_fast = self.now() + FAST_S
            return
        cmds = list(JUNOS_LIVE["fast"]) + (list(JUNOS_LIVE["slow"]) if slow else [])
        started = self.now()
        runner = self.junos_runner or (lambda h, u, p, c, t: JS.run_junos_burst(h, u, p, c, timeout=t))
        res = runner(host, user, pw, cmds, BURST_TIMEOUT_S)
        now = self.now()
        if not res.ok and not res.outputs:
            self._burst_failed(nk, s, res.reason or "ssh failed", now)
            return
        parsed = {}
        for c, out in res.outputs.items():
            fn = JUNOS_PARSERS.get(c)
            if fn is not None:
                parsed[c] = fn(out.text) if out.text is not None else P_IOS.ParseResult(False, None, out.error)
        with self._lock:
            nf = self.facts.get(nk)
            if nf is None:
                nf = self.facts[nk] = NodeFacts(nk, n.label, n.vendor)
            before = _proto_snapshot(nf)
            junos_facts_update(nf, parsed, now)
            mac_out = res.outputs.get(JUNOS_MAC_CMD)
            if mac_out is not None and mac_out.text is not None:
                nf.iface_macs = Fact(JS.parse_iface_macs(mac_out.text), now)
            as_out = res.outputs.get(JUNOS_AS_CMD)
            if as_out is not None and as_out.text is not None:
                nf.junos_local_as = Fact(JS.parse_local_as(as_out.text), now)
            if nf.bgp is not None and nf.junos_local_as is not None and nf.bgp.value.local_as is None:
                nf.bgp.value.local_as = nf.junos_local_as.value
            acc = [res.outputs.get(c) for c in accounts_mod.JUNOS_ACCOUNT_CMDS]
            if slow and all(a is not None and a.text is not None for a in acc):
                nf.accounts = Fact(accounts_mod.parse_junos_accounts("\n".join(a.text for a in acc)), now)
                nf.accounts_note = ""
            nf.last_burst_ts = now
            nf.collector_state = CollectorState.OK if res.ok else CollectorState.ERROR
            nf.collector_detail = "" if res.ok else (res.reason or "partial read")
            after = _proto_snapshot(nf)
        s.failures = 0
        s.learned = True
        s.next_fast = started + FAST_S
        if slow:
            s.next_slow = started + SLOW_S
        self._proto_diff_events(nk, n.label, before, after, now)
        self._addr_events(nk, n.label)

    def _live_kvm_names(self, topo: Topology, facts: dict) -> Topology:
        """A host-KVM cable end is named after the device's own interface whose MAC matches the VM NIC (live
        ``show interfaces``); until that is read the name LabDesk gave the NIC at creation stays, unconfirmed."""
        changed = {}
        for lk, l in topo.links.items():
            if not l.kvm:
                continue
            new = {}
            for side, nk, mac in (("a", l.a, l.mac_a), ("b", l.b, l.mac_b)):
                nf = facts.get(nk)
                if mac and nf is not None and nf.iface_macs is not None:
                    name = nf.iface_macs.value.get(mac.lower())
                    if name:
                        new["iface_" + side] = name
            if new:
                changed[lk] = dataclasses.replace(l, **new)
        if not changed:
            return topo
        return dataclasses.replace(topo, links={**topo.links, **changed})

    # ------------------------------------------------------------- merge + diffs
    def _merge(self, nk: str, n: TopoNode, res: BurstResult, now: float, tiers: list[str]) -> None:
        parsed = {}
        for c, out in res.outputs.items():
            fn = IOS_PARSERS.get(c)
            if fn is None:
                continue
            parsed[c] = fn(out.text) if out.text is not None else P_IOS.ParseResult(False, None, out.error)
        switch = n.role == NodeRole.SWITCH and n.managed
        rc_out = res.outputs.get("show ip route connected")
        if switch and rc_out is not None and rc_out.text is not None:
            parsed[L2_CMDS["routing"]] = P_L2.parse_routing_enabled(rc_out.text)
        with self._lock:
            nf = self.facts.get(nk)
            if nf is None:
                nf = self.facts[nk] = NodeFacts(nk, n.label, n.vendor)
            before = _proto_snapshot(nf)
            l2_before = _l2_snapshot(nf) if switch else None
            walls = {c: o.wall for c, o in res.outputs.items() if o.wall}
            ios_facts_update(nf, parsed, now, walls)
            if switch:
                l2_facts_update(nf, parsed, now, walls)
            protos_before = nf.ec.protos() if nf.ec is not None else set()
            ec_facts_update(nf, parsed, now, walls)
            if nf.ec is not None and nf.ec.protos() != protos_before:
                self._ec_rescan.add(nk)                 # a protocol appeared: read its tables right after this burst
            if res.hostname:
                nf.hostname = res.hostname
            rc = parsed.get("show running-config")
            if rc is not None and rc.ok:
                d = rc.data
                text = redact_values(redact(res.outputs["show running-config"].text, "cisco"), self._secret_values())
                cf = ConfigFacts(text=text, parsed={k: v for k, v in d.items() if k != "interfaces"})
                for name, itf in d["interfaces"].items():
                    k = iface_key(name)
                    if itf["ospf_network"] == "POINT_TO_POINT":
                        cf.p2p_ifaces.add(k)
                    if itf["ospf_passive"]:
                        cf.passive_ifaces.add(k)
                    if itf["shutdown"]:
                        cf.shutdown_ifaces.add(k)
                cf.ospf_enabled = bool(d["ospf"])
                for name, itf in d["interfaces"].items():
                    prim = (itf["ip"], itf["prefixlen"]) if itf.get("ip") and itf.get("prefixlen") is not None else None
                    method = "dhcp" if itf.get("dhcp") else ("unnumbered" if itf.get("unnumbered") else
                                                            ("static" if prim else "none"))
                    cf.addrs[iface_key(name)] = AddrInfo(prim, tuple(itf.get("secondaries") or ()), method,
                                                         itf.get("unnumbered"), None, "config", now)
                cf.bgp_asn = d["bgp"]["asn"] if d["bgp"] else None
                nf.config = Fact(cf, now)
                nf.accounts = Fact(accounts_mod.parse_ios_accounts(res.outputs["show running-config"].text), now)
                nf.accounts_note = ""
            elif rc is not None and res.outputs["show running-config"].error == "needs enable secret":
                nf.collector_detail = "config needs an enable secret (device-creds)"
                nf.accounts_note = "needs enable secret"
            nf.last_burst_ts = now
            nf.collector_state = CollectorState.OK if res.status == BurstStatus.OK else CollectorState.ERROR
            if res.status == BurstStatus.OK:
                nf.collector_detail = res.reason or ""
            else:
                nf.collector_detail = res.reason
            after = _proto_snapshot(nf)
            l2_after = _l2_snapshot(nf) if switch else None
        self._syslog_events(nk, n.label, res.syslog, now)
        self._proto_diff_events(nk, n.label, before, after, now)
        self._addr_events(nk, n.label)
        if switch:
            self._l2_diff_events(nk, n.label, l2_before, l2_after)

    def _secret_values(self) -> list[str]:
        """Literal secrets to remove from logs: device-creds + CML credentials (re-read when the device-creds
        file changes, e.g. after Credentials… saved a new password) + plain-text passwords seen in device
        configs (``username x password 0 y``)."""
        mt = creds_mod.mtime()
        if not self._secrets or mt != self._secrets_mtime:
            self._secrets = creds_mod.all_secret_values()
            self._secrets_mtime = mt
        return self._secrets + sorted(self._device_secrets)

    def _learn_device_secrets(self, res: BurstResult) -> None:
        out = res.outputs.get("show running-config")
        if out is not None and out.text:
            pw = accounts_mod.parse_ios_accounts(out.text).plaintexts()
            if pw:
                with self._lock:
                    self._device_secrets.update(p for p in pw if len(p) >= 3)

    def credentials_changed(self, label: str | None = None) -> None:
        """Device credentials were saved (Credentials… dialog): drop the secrets cache and, now that an enable
        secret may exist, read the running-config (on-device accounts) of ``label`` (all nodes when None) at
        the next CONFIG slot - staggered, riding the normal FAST burst; nothing extra is typed."""
        now = self.now()
        with self._lock:
            self._secrets = []
            topo = self.topo
            i = 0
            for nk, s in self.sched.items():
                n = topo.nodes.get(nk) if topo else None
                if n is None or (label is not None and n.label != label):
                    continue
                nf = self.facts.get(nk)
                if nf is not None and nf.collector_state == CollectorState.NEEDS_CREDENTIALS:
                    s.failures, s.next_fast = 0, 0.0
                if s.learned:
                    s.config_wanted = True
                    s.next_config = now + 2.0 + 12.0 * i
                    i += 1

    def _syslog_events(self, nk: str, label: str, logs: list[SyslogLine], now: float) -> None:
        for lg in logs:
            proto = {"OSPF": "ospf", "BGP": "bgp", "HSRP": "hsrp", "VRRP": "vrrp", "LINK": "link",
                     "LINEPROTO": "link", "SPANTREE": "stp", "SW_VLAN": "vlan", "DTP": "trunk",
                     "PM": "link", "EC": "ec"}.get(lg.facility, "probe")
            sev = "warn" if lg.severity <= 4 or "down" in lg.text.lower() else "info"
            self._event("SYSLOG", proto, sev, f"{label}: %{lg.facility}-{lg.severity}-{lg.mnemonic}: "
                        f"{redact_values(redact(lg.text), self._secret_values())}", [nk], device_ts=lg.ts)
            with self._lock:
                self._syslog_recent[(nk, proto)] = now
                if lg.facility in ("LINK", "LINEPROTO") and lg.mnemonic == "UPDOWN":
                    m = re.search(r"Interface (\S+?),", lg.text)
                    if m:
                        self.link_history.setdefault(("if", nk, iface_key(m[1])), {})["syslog_ts"] = now
                if lg.mnemonic == "CONFIG_I" and nk in self.sched:
                    self.sched[nk].config_wanted, self.sched[nk].next_config = True, now + 5.0
                if lg.mnemonic in ("UPDOWN", "ADJCHG", "ADJCHANGE", "STATECHANGE", "STATE"):
                    self._kick([nk])
                if lg.facility == "EC":
                    self._ec_syslog(nk, lg, now)

    def _proto_diff_events(self, nk: str, label: str, before: dict, after: dict, now: float) -> None:
        if before.get("empty"):
            return

        def recent(proto):
            ts = self._syslog_recent.get((nk, proto))
            return ts is not None and now - ts < 60

        names = self._rid_names()
        for key in set(before["ospf"]) | set(after["ospf"]):
            b, a = before["ospf"].get(key), after["ospf"].get(key)
            if b == a or recent("ospf"):
                continue
            nb = names.get(key[0], key[0])
            if a is None:
                self._event("OSPF", "ospf", "warn", f"{label} ↔ {nb} ({short_iface(key[1])}): {b} → DOWN", [nk])
            elif b is None:
                self._event("OSPF", "ospf", "ok" if a.startswith("FULL") else "info",
                            f"{label} ↔ {nb} ({short_iface(key[1])}): {a}", [nk])
            else:
                self._event("OSPF", "ospf", "info", f"{label} ↔ {nb} ({short_iface(key[1])}): {b} → {a}", [nk])
        for ip in set(before["bgp"]) | set(after["bgp"]):
            b, a = before["bgp"].get(ip), after["bgp"].get(ip)
            if b == a or recent("bgp"):
                continue
            who = names.get(ip, ip)
            sev = "ok" if a == "Established" else "warn"
            self._event("BGP", "bgp", sev, f"{label} → {who}: {b or 'none'} → {a or 'removed'}", [nk])
        for key in set(before["fhrp"]) | set(after["fhrp"]):
            b, a = before["fhrp"].get(key), after["fhrp"].get(key)
            if b == a:
                continue
            proto = key[0]
            if recent(proto.lower()):
                continue
            self._event(proto, proto.lower(), "info",
                        f"{label} {short_iface(key[1])} grp {key[2]}: {b or 'none'} → {a or 'removed'}", [nk])

    def _l2_diff_events(self, nk: str, label: str, before: dict | None, after: dict | None) -> None:
        """STP port role/state changes, root bridge changes, topology changes, VLAN and trunk changes."""
        from .parsers.ios_l2 import vlan_text
        if not before or not after:
            return
        names = self._bridge_names()
        if before["stp"] is not None and after["stp"] is not None:
            moves: dict[tuple, list[int]] = {}
            for key in set(before["stp"]) | set(after["stp"]):
                b, a = before["stp"].get(key), after["stp"].get(key)
                if b == a or b is None or a is None:
                    continue
                port, vid = key
                moves.setdefault((port, b, a), []).append(vid)
            for (port, b, a), vids in sorted(moves.items()):
                to_blk, from_blk = a[1] == "BLK", b[1] in ("BLK", "LRN", "LIS")
                sev = "warn" if to_blk or (from_blk and a[1] == "FWD") else "info"
                backup = a[0] in ("Altn", "Back")
                why = (" - now a backup path, blocked to prevent a loop" if to_blk and backup else
                       " - discarding while RSTP converges" if a[1] in ("BLK", "LRN", "LIS") else
                       " - takes over forwarding" if from_blk and a[1] == "FWD" and b[0] in ("Altn", "Back") else
                       " - forwarding again" if from_blk and a[1] == "FWD" else "")
                self._event("STP", "stp", sev, f"{label} {port} VLAN {vlan_text(vids)}: {b[0]} {b[1]} → {a[0]} {a[1]}"
                            + why, [nk])
            for vid in sorted(set(before["root"]) & set(after["root"])):
                b, a = before["root"][vid], after["root"][vid]
                if b != a and a:
                    self._event("STP", "stp", "warn", f"VLAN {vid}: root bridge {names.get(b, b)} → {names.get(a, a)}"
                                f" (seen by {label})", [nk])
        if before["tc"] is not None and after["tc"] is not None:
            grew = sorted(v for v, c in after["tc"].items() if c[0] > before["tc"].get(v, (0, None))[0])
            if grew:
                frm = sorted({after["tc"][v][1] for v in grew if after["tc"][v][1]})
                self._event("STP", "stp", "info", f"{label}: spanning-tree topology change in VLAN {vlan_text(grew)}"
                            + (f" (from {', '.join(frm)})" if frm else ""), [nk])
        if before["vlans"] is not None and after["vlans"] is not None:
            for vid in sorted(set(before["vlans"]) | set(after["vlans"])):
                b, a = before["vlans"].get(vid), after["vlans"].get(vid)
                if b == a:
                    continue
                txt = (f"VLAN {vid} {a} created" if b is None else f"VLAN {vid} {b} removed" if a is None
                       else f"VLAN {vid} renamed {b} → {a}")
                self._event("VLAN", "vlan", "info" if a else "warn", f"{label}: {txt}", [nk])
        if before["trunks"] is not None and after["trunks"] is not None:
            for port in sorted(set(before["trunks"]) | set(after["trunks"])):
                b, a = before["trunks"].get(port), after["trunks"].get(port)
                if b == a:
                    continue
                if a is None:
                    self._event("TRUNK", "trunk", "warn", f"{label} {port}: trunk down (was carrying VLAN {b})", [nk])
                elif b is None:
                    self._event("TRUNK", "trunk", "ok", f"{label} {port}: trunk up, carrying VLAN {a}", [nk])
                else:
                    self._event("TRUNK", "trunk", "info", f"{label} {port}: trunk VLANs {b} → {a}", [nk])

    # ------------------------------------------------------------- subnets on cables
    def _iface_links(self, nk: str, ikey: str) -> list[str]:
        """Canvas links of interface ``ikey`` (a subinterface maps to its physical port's cable)."""
        topo = self.topo
        if topo is None:
            return []
        phys = ikey.split(".", 1)[0]
        return [lk for lk, l in topo.links.items()
                if (l.a == nk and iface_key(l.iface_a) == phys) or (l.b == nk and iface_key(l.iface_b) == phys)]

    def _addr_events(self, nk: str, label: str) -> None:
        """SUBNET events from the node's effective addressing (CLI read and console tap merged), so the
        same change seen first on the console and then by the CLI probe is logged once.  The first
        read of a node is the baseline (no events)."""
        with self._lock:
            nf = self.facts.get(nk)
            if nf is None or (nf.addrs is None and nf.ifaces is None and not nf.addr_overrides):
                return
            before = self._addr_state.get(nk)
            after = effective_addrs(nf)
            self._addr_state[nk] = after
            changes = subnet_changes(before, after)
        for ch in changes:
            src = after.get(ch.iface_key).source if after.get(ch.iface_key) is not None else \
                (before or {}).get(ch.iface_key, AddrInfo()).source
            sev = {"configured": "ok", "changed": "info", "removed": "warn"}[ch.kind]
            self._event("SUBNET", "subnet", sev, f"{label} {ch.text}" + (" (console)" if src == "console" else ""),
                        [nk] + self._iface_links(nk, ch.iface_key))

    def _mismatch_events(self, topo: Topology, join: JoinResult) -> None:
        for lk, lf in join.links.items():
            ad = lf.addressing
            cur = ad.label if (ad is not None and ad.state == SubnetState.MISMATCH) else None
            prev = self._mismatch.get(lk)
            if cur == prev:
                continue
            self._mismatch[lk] = cur
            name = self._link_name(lk)
            if cur is not None:
                self._event("SUBNET", "subnet", "warn", f"{name}: addressing {cur.replace('⚠ ', '')}"
                            " - the two ends cannot reach each other", [lk])
            elif prev is not None:
                fixed = ad is not None and ad.state in (SubnetState.MATCH, SubnetState.DHCP) and bool(ad.shared)
                self._event("SUBNET", "subnet", "ok" if fixed else "info",
                            f"{name}: " + (f"addressing fixed ({ad.label})" if fixed else "addressing mismatch cleared"
                                           + (f" (now {ad.label})" if ad is not None else "")), [lk])

    def _ec_syslog(self, nk: str, lg: SyslogLine, now: float) -> None:
        """%EC-5-* (caller holds the lock): remember why IOS refused to bundle a port (shown as the member's
        problem while it stays unbundled) and re-read the EtherChannel tables at once."""
        nf = self.facts.get(nk)
        if nf is not None:
            reason = P_L2.ec_syslog_reason(lg.mnemonic, lg.text)
            if reason is not None:
                from .facts import EcFacts
                nf.ec = nf.ec or EcFacts()
                nf.ec.problems[reason[0]] = (reason[1], now)
        if nk in self.sched:
            self.sched[nk].next_fast = 0.0
            self.sched[nk].next_slow = 0.0

    def _ec_events(self, join: JoinResult) -> None:
        """ETHERCHANNEL events from bundle diffs: member bundled / unbundled, Port-channel up / down, protocol
        change, mismatch detected / cleared.  The first sight of a bundle is the baseline (no event)."""
        seen = set()
        for bd in join.bundles.values():
            ident = frozenset(bd.link_keys)
            seen.add(ident)
            if bd.state == EcState.UNKNOWN:
                continue
            snap = {"protocol": bd.protocol, "mismatch": bd.mismatch_short, "state": bd.state,
                    "po": {s_.node: (s_.po, s_.up) for s_ in (bd.side_a, bd.side_b) if s_ is not None},
                    "members": {(s_.node, m.port): (m.bundled, m.words) for s_ in (bd.side_a, bd.side_b)
                                if s_ is not None for m in s_.members}}
            prev = self._ec_state.get(ident)
            self._ec_state[ident] = snap
            if prev is None:
                continue
            items = list(bd.link_keys) + [bd.key]
            name = f"{bd.po_text} {self._label(bd.a)} ↔ {self._label(bd.b)}"
            if prev["protocol"] != snap["protocol"]:
                self._event("ETHERCHANNEL", "ec", "info", f"{name}: protocol {prev['protocol']} → {snap['protocol']}",
                            items)
            for node, (po, up) in snap["po"].items():
                pu = prev["po"].get(node)
                if pu is not None and pu[1] != up:
                    self._event("ETHERCHANNEL", "ec", "ok" if up else "error",
                                f"{self._label(node)} {po} {'up' if up else 'down'}", items + [node])
            for (node, port), (b, words) in sorted(snap["members"].items()):
                pb = prev["members"].get((node, port))
                if pb is None or pb[0] == b:
                    continue
                self._event("ETHERCHANNEL", "ec", "ok" if b else "warn",
                            f"{self._label(node)} {port} " + (f"bundled into {snap['po'].get(node, ('Po?',))[0]}" if b
                                                             else f"left {snap['po'].get(node, ('Po?',))[0]} ({words})"),
                            items + [node])
            if snap["mismatch"] != prev["mismatch"]:
                if snap["mismatch"]:
                    self._event("ETHERCHANNEL", "ec", "warn", f"{name}: ⚠ {snap['mismatch']}", items)
                else:
                    self._event("ETHERCHANNEL", "ec", "ok", f"{name}: mismatch cleared ({prev['mismatch']})"
                                + (f", {bd.bundled}/{bd.total} bundled" if bd.total else ""), items)
        for ident in [k for k in self._ec_state if k not in seen]:
            prev = self._ec_state.pop(ident)
            if prev.get("po"):
                names = ", ".join(f"{self._label(n)} {po}" for n, (po, _) in prev["po"].items())
                self._event("ETHERCHANNEL", "ec", "info", f"EtherChannel removed ({names})", list(ident))

    def _tap_targets(self, topo: Topology | None = None) -> list[str]:
        """Booted CML nodes with a CLI collector whose console is open on this host right now."""
        topo = topo or self.topo
        if topo is None:
            return []
        out = []
        for nk, n in topo.nodes.items():
            if not nk.startswith("cml:") or not n.collected or not n.booted:
                continue
            u = self.console_uuid.get(nk)
            if u and u in self.console_open:
                out.append(nk)
        return out

    def _ensure_tap(self) -> None:
        if self._tap_thread is not None and self._tap_thread.is_alive():
            self._tap_wake.set()
            return
        self._tap_stop = False
        self._tap_thread = threading.Thread(target=self._tap_loop, daemon=True, name="labdesk-console-tap")
        self._tap_thread.start()

    def _tap_loop(self) -> None:
        idle = 0
        while not self._tap_stop:
            with self._lock:
                targets = set(self._tap_targets()) | set(self._tap_final)
                self._tap_final.clear()
            changed = False
            for nk in sorted(targets):
                try:
                    changed |= self.tap_once(nk)
                except Exception:          # noqa: BLE001 - the tap must never die on one bad read
                    pass
            if changed and self.on_change:
                try:
                    self.on_change()
                except Exception:          # noqa: BLE001
                    pass
            idle = 0 if targets else idle + 1
            if idle > 30:                  # no console open for ~30 s: park the thread
                self._tap_thread = None
                return
            self._tap_wake.wait(TAP_S)
            self._tap_wake.clear()

    def tap_once(self, nk: str) -> bool:
        """One passive read of the node's CML console log; applies typed address commands as
        'console' overrides.  Never types anything.  Returns True when the addressing changed."""
        lab = self.lab_id
        n = self.topo.nodes.get(nk) if self.topo else None
        if self.client is None or lab is None or n is None or not nk.startswith("cml:"):
            return False
        nid = nk.split(":", 1)[1]
        try:
            text = self.client.console_log(lab, nid, lines=TAP_LINES)
        except CMLError:
            return False
        st = self._tap_state.setdefault(nk, P_TAP.TapState())
        res = P_TAP.feed(st, P_TAP.parse_log(text))
        cmds = list(res.cmds)
        now = self.now()
        self.tap_ok[nk] = now
        with self._lock:
            nf = self.facts.get(nk)
            if nf is None:
                nf = self.facts[nk] = NodeFacts(nk, n.label, n.vendor)
            nf.addr_live_ts = now
            if self._addr_state.get(nk) is None and (nf.addrs is not None or nf.ifaces is not None):
                self._addr_state[nk] = effective_addrs(nf)       # baseline before the first typed change
            for c in cmds:
                k = iface_key(c.iface)
                base = effective_addrs(nf).get(k)
                nf.addr_overrides[k] = apply_addr_cmd(base, c.op, c.ip, c.prefixlen, c.src, ts=now, source="console")
            if res.config_done and nk in self.sched and not self._paused(nk):
                self._kick([nk])
        if cmds:
            self._addr_events(nk, n.label)
        return bool(cmds)

    def refresh(self, node_keys: list[str] | None = None) -> ActionResult:
        """Toolbar Refresh: read the given (default: all) routers / managed switches now.  Nodes whose
        console is open are not probed (etiquette) - their console log is re-read instead."""
        topo = self.topo
        if topo is None:
            return ActionResult(False, "no lab")
        keys = node_keys or [k for k, n in topo.nodes.items() if n.collected and n.booted]
        probed, tapped = [], []
        with self._lock:
            for nk in keys:
                n = topo.nodes.get(nk)
                if n is None or not n.collected or not n.booted:
                    continue
                if self._paused(nk):
                    self._tap_final.add(nk)
                    tapped.append(n.label)
                else:
                    self._kick([nk])
                    probed.append(n.label)
        if tapped and self.client is not None:
            self._ensure_tap()
        parts = []
        if probed:
            parts.append("probing " + ", ".join(sorted(probed)))
        if tapped:
            parts.append("console open, reading the console log of " + ", ".join(sorted(tapped)))
        return ActionResult(True, "; ".join(parts) or "nothing to refresh")

    def _bridge_names(self) -> dict[str, str]:
        out = {}
        for nf in self.facts.values():
            if nf.l2 and nf.l2.stp:
                for sv in nf.l2.stp.value.values():
                    if sv.bridge_address:
                        out[sv.bridge_address] = nf.label
        return out

    def _rid_names(self) -> dict[str, str]:
        out = {}
        for nk, nf in self.facts.items():
            lbl = nf.label
            if nf.rid:
                out[nf.rid.value] = lbl
            if nf.ifaces:
                for f in nf.ifaces.value.values():
                    if f.ip:
                        out.setdefault(f.ip, lbl)
        return out

    def _write_probe_log(self, label: str, res: BurstResult, cmds: list[str]) -> None:
        try:
            d = self.probe_log_dir
            d.mkdir(parents=True, exist_ok=True)
            os.chmod(d, 0o700)
            p = d / f"{re.sub(r'[^A-Za-z0-9._-]', '_', label)}.log"
            sep = "\n=====LDK-BURST=====\n"
            old = p.read_text(errors="replace").split(sep) if p.exists() else []
            head = (f"{time.strftime('%Y-%m-%d %H:%M:%S')} status={res.status} reason={res.reason!r} "
                    f"mode={res.mode_found} secs={res.seconds:.1f} cmds={cmds}\n")
            body = redact_values(redact(res.transcript.replace("\r", ""), "cisco"), self._secret_values())
            blocks = [b for b in old if b.strip()][-49:] + [head + body]
            fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as fh:
                fh.write(sep.join(blocks))
            os.chmod(p, 0o600)
        except OSError:
            pass

    # ------------------------------------------------------------- traffic (D7)
    def _poll_traffic(self, topo: Topology, now: float) -> None:
        if not (self.traffic_layer or self.selected_link):
            return
        if now < self._next_traffic:
            return
        interval = (TRAFFIC_VISIBLE_SLOW_S if self._traffic_slow else TRAFFIC_VISIBLE_S) \
            if self.traffic_layer else TRAFFIC_SELECTED_S
        self._next_traffic = now + interval
        t0 = time.monotonic()
        try:
            stats = self.client.simulation_stats(self.lab_id)
        except CMLError:
            return
        self._traffic_slow = (time.monotonic() - t0) > 0.3
        for lid, st in (stats.get("links") or {}).items():
            lk = f"cml:{lid}"
            total_b = int(st.get("readbytes", 0)) + int(st.get("writebytes", 0))
            total_p = int(st.get("readpackets", 0)) + int(st.get("writepackets", 0))
            drops = int(st.get("drops", 0))
            prev = self._traffic_prev.get(lk)
            self._traffic_prev[lk] = (now, total_b, total_p, drops)
            if prev is None:
                continue
            dt = max(0.001, now - prev[0])
            db, dp, dd = total_b - prev[1], total_p - prev[2], drops - prev[3]
            if db < 0 or dp < 0:
                db = dp = 0                      # counters reset: resync
            bps = db * 8 / dt
            hist = self._spark.setdefault(lk, deque(maxlen=SPARK_POINTS))
            if lk == self.selected_link:
                hist.append(bps)
            self.traffic[lk] = LinkTraffic(bps, dp / dt, max(0, dd), now,
                                           list(hist) if lk == self.selected_link else [])

    # ------------------------------------------------------------- conditions (D7/D8)
    def _load_conditions(self, cml_links: frozenset) -> None:
        for lk in cml_links:
            try:
                c = self.client.link_condition(self.lab_id, lk.split(":", 1)[1])
            except CMLError:
                self.capabilities["link_conditioning"] = False
                break
            self.conditions[lk] = _condition_from_api(c, self.now())
        self._conditions_loaded_for = cml_links

    def condition_presets(self) -> dict[str, dict]:
        return dict(LINK_CONDITION_PRESETS)

    def set_condition(self, link_key: str, params: dict | None = None, preset: str | None = None) -> ActionResult:
        """D8: apply netem-style conditioning to a CML link (blocking; run in a worker)."""
        if not link_key.startswith("cml:"):
            return ActionResult(False, "conditioning is only available on CML links (not host-KVM links)")
        cfg = dict(LINK_CONDITION_PRESETS[preset]) if preset else dict(params or {})
        lid = link_key.split(":", 1)[1]
        if not cfg:
            return self.clear_condition(link_key)
        cfg["enabled"] = True
        try:
            self.client.set_link_condition(self.lab_id, lid, cfg)
            c = _condition_from_api(self.client.link_condition(self.lab_id, lid), self.now())
        except CMLError as e:
            return ActionResult(False, str(e))
        self.conditions[link_key] = c
        name = self._link_name(link_key)
        self._event("SIM", "sim", "info", f"{name}: conditioned {c.label or '(none)'}", [link_key])
        return ActionResult(True, c.label, {"condition": c})

    def clear_condition(self, link_key: str) -> ActionResult:
        if not link_key.startswith("cml:"):
            return ActionResult(False, "conditioning is only available on CML links")
        lid = link_key.split(":", 1)[1]
        try:
            self.client.clear_link_condition(self.lab_id, lid)
            c = _condition_from_api(self.client.link_condition(self.lab_id, lid), self.now())
        except CMLError as e:
            return ActionResult(False, str(e))
        self.conditions[link_key] = c
        self._event("SIM", "sim", "info", f"{self._link_name(link_key)}: conditioning removed", [link_key])
        return ActionResult(True, "cleared", {"condition": c})

    def pull_cable(self, link_key: str) -> ActionResult:
        """D8: simulate unplugging the cable (CML link state stop)."""
        if not link_key.startswith("cml:"):
            return ActionResult(False, "cable pull is only available on CML links in this release")
        lid = link_key.split(":", 1)[1]
        try:
            self.client.stop_link(self.lab_id, lid)
        except CMLError as e:
            return ActionResult(False, str(e))
        with self._lock:
            self.link_history.setdefault(link_key, {})["pulled_by_labdesk"] = self.now()
            l = self.topo.links.get(link_key) if self.topo else None
            if l:
                self._kick([l.a, l.b])
        self._event("CABLE", "sim", "warn", f"{self._link_name(link_key)}: cable pulled (LabDesk)", [link_key])
        return ActionResult(True, "cable pulled")

    def restore_cable(self, link_key: str) -> ActionResult:
        if not link_key.startswith("cml:"):
            return ActionResult(False, "only CML links")
        lid = link_key.split(":", 1)[1]
        try:
            self.client.start_link(self.lab_id, lid)
        except CMLError as e:
            return ActionResult(False, str(e))
        with self._lock:
            self.link_history.pop(link_key, None)
            l = self.topo.links.get(link_key) if self.topo else None
            if l:
                self._kick([l.a, l.b])
        self._event("CABLE", "sim", "ok", f"{self._link_name(link_key)}: cable plugged back in", [link_key])
        return ActionResult(True, "cable restored")

    def _link_name(self, link_key: str) -> str:
        t = self.topo
        l = t.links.get(link_key) if t else None
        if not l:
            return link_key
        la, lb = t.nodes.get(l.a), t.nodes.get(l.b)
        return f"{la.label if la else '?'} {short_iface(l.iface_a)} ↔ {lb.label if lb else '?'} {short_iface(l.iface_b)}"

    # ------------------------------------------------------------- user actions (blocking)
    def _user_burst(self, nk: str, cmds: list[str], timeout: float, **burst_kw) -> BurstResult | ActionResult:
        n = self.topo.nodes.get(nk) if self.topo else None
        if n is None or not n.collected or not n.booted:
            return ActionResult(False, "not a running router or managed switch")
        if nk.startswith("kvm:"):
            return ActionResult(False, "host-KVM collector not available in this release (beta)")
        if not self.console_uuid.get(nk):
            return ActionResult(False, "no console key yet")
        s = self.sched.setdefault(nk, _Sched())
        lock = self._node_lock(nk)
        with self._lock:
            s.user_waiting += 1
        try:
            if not lock.acquire(timeout=BURST_TIMEOUT_S + _extra_timeout(n, self.facts.get(nk)) + 5):
                return ActionResult(False, "console busy (another probe is running)")
            try:
                tiers = burst_kw.pop("tiers", None)
                res = self._burst(nk, cmds, timeout=timeout + _extra_timeout(n, self.facts.get(nk)),
                                  tiers=_tiers_of(cmds) if tiers is None else tiers, user=True, **burst_kw)
            finally:
                lock.release()
        finally:
            with self._lock:
                s.user_waiting -= 1
        if res is None:
            return ActionResult(False, "probe could not start")
        if res.status not in (BurstStatus.OK, BurstStatus.PARTIAL):
            return ActionResult(False, {"console_busy": "console in use: " + res.reason}.get(res.status, res.reason))
        return res

    def probe_now(self, node_key: str) -> ActionResult:
        """'Probe now (output appears in your console)': one FAST+SLOW burst even if paused."""
        self.one_shot.add(node_key)
        n = self.topo.nodes.get(node_key) if self.topo else None
        fast_cmds, slow_cmds = command_sets(n, self.facts.get(node_key)) if n is not None else (IOS_FAST, IOS_SLOW)
        try:
            r = self._user_burst(node_key, fast_cmds + slow_cmds, BURST_TIMEOUT_S)
        finally:
            self.one_shot.discard(node_key)
        if isinstance(r, ActionResult):
            return r
        return ActionResult(r.status == BurstStatus.OK, f"probed in {r.seconds:.1f} s", {"status": r.status})

    def run_show_script(self, node_key: str, commands: list[str], stay_logged_in: bool = True) -> ActionResult:
        """The user's own show script for this device (right-click > Show script...): typed on the console
        without sentinel lines, so the open console window shows just the commands and their output.  Runs
        even while the console is open (that is the point); a line found logged out is left logged in.
        Read-only: the script is re-validated here (show / ping / traceroute only)."""
        from . import show_scripts as SS
        cmds = [SS.normalise(c) for c in commands]
        bad = next((f"{c[:40]}: {SS.check_line(c)}" for c in cmds if SS.check_line(c)), "")
        if bad:
            return ActionResult(False, f"script not run - {bad}")
        if not cmds:
            return ActionResult(False, "the script is empty")
        r = self._user_burst(node_key, cmds, SS.burst_timeout(cmds), tiers=[], plain=True,
                             stay_logged_in=stay_logged_in, cmd_timeout=30.0)
        if isinstance(r, ActionResult):
            return r
        failed = [c for c in cmds if c not in r.outputs or r.outputs[c].text is None]
        errors = [c for c in cmds if c in r.outputs and r.outputs[c].text is not None
                  and re.search(r"^\s*% ?(Invalid input|Incomplete command|Ambiguous command)", r.outputs[c].text, re.M)]
        done = len(cmds) - len(failed)
        text = f"{done} of {len(cmds)} command(s) in {r.seconds:.1f} s"
        if failed:
            why = r.outputs[failed[0]].error if failed[0] in r.outputs else "not sent"
            text += f"; stopped at '{failed[0]}' ({why})"
        if errors:
            text += "; the device rejected: " + ", ".join(f"'{c}'" for c in errors)
        return ActionResult(not failed and not errors, text,
                            {"status": r.status, "failed": failed, "rejected": errors,
                             "outputs": {c: o.text for c, o in r.outputs.items()}})

    def refresh_config(self, node_key: str) -> ActionResult:
        r = self._user_burst(node_key, IOS_CONFIG, CONFIG_TIMEOUT_S)
        if isinstance(r, ActionResult):
            return r
        out = r.outputs.get("show running-config")
        if out is None or out.text is None:
            return ActionResult(False, out.error if out else "no output")
        nf = self.facts.get(node_key)
        return ActionResult(True, "config refreshed",
                            {"text": nf.config.value.text if nf and nf.config else None})

    def route_table(self, node_key: str) -> ActionResult:
        """Inspector Routing tab: the full 'show ip route', fetched on demand under the node lock."""
        cmd = "show ip route"
        r = self._user_burst(node_key, [cmd], BURST_TIMEOUT_S)
        if isinstance(r, ActionResult):
            return r
        out = r.outputs.get(cmd)
        if out is None or out.text is None:
            return ActionResult(False, out.error if out and out.error else "no output")
        return ActionResult(True, "routing table read",
                            {"text": redact_values(redact(out.text, "cisco"), self._secret_values())})

    def ping(self, node_key: str, target: str, count: int = 5, source: str | None = None) -> ActionResult:
        from . import validate
        tgt = validate.host(target)
        cmd = f"ping {tgt} repeat {max(1, min(int(count), 20))} timeout 1"
        if source:
            cmd += f" source {validate.host(source)}"
        r = self._user_burst(node_key, [cmd], TRACE_TIMEOUT_S)
        if isinstance(r, ActionResult):
            return r
        out = r.outputs.get(cmd)
        pr = P_IOS.parse_ping(out.text if out else None)
        if not pr.ok:
            return ActionResult(False, pr.error or "ping failed")
        d = pr.data
        self._event("SIM", "sim", "ok" if d["success"] else "warn",
                    f"{self._label(node_key)} ping {tgt}: {d['received']}/{d['sent']}"
                    + (f", avg {d['rtt_avg_ms']} ms" if d['rtt_avg_ms'] is not None else ""), [node_key])
        return ActionResult(bool(d["success"]), f"{d['received']}/{d['sent']} replies", d)

    def traceroute(self, node_key: str, target: str, max_ttl: int = 8) -> ActionResult:
        """Trace + resolve each hop to a lab node and the links between them (for animation)."""
        from . import validate
        tgt = validate.host(target)
        cmd = f"traceroute {tgt} numeric timeout 1 probe 1 ttl 1 {max(1, min(int(max_ttl), 15))}"
        r = self._user_burst(node_key, [cmd], TRACE_TIMEOUT_S)
        if isinstance(r, ActionResult):
            return r
        out = r.outputs.get(cmd)
        pr = P_IOS.parse_traceroute(out.text if out else None)
        if not pr.ok:
            return ActionResult(False, pr.error or "traceroute failed")
        hops = self.resolve_trace(node_key, pr.data)
        names = " → ".join(h["name"] or (h["ip"] or "*") for h in hops)
        self._event("SIM", "sim", "info", f"{self._label(node_key)} trace {tgt}: {names}", [node_key])
        return ActionResult(True, names, {"hops": hops})

    def resolve_trace(self, origin: str, rows: list[dict]) -> list[dict]:
        """[{hop, ip, name, node_key, link_keys (from previous hop), rtt_ms}]"""
        view = self._view
        owners = {}
        for nk, nf in self.facts.items():
            if nf.ifaces:
                for f in nf.ifaces.value.values():
                    if f.ip:
                        owners[f.ip] = nk
        prev = origin
        out = []
        topo = self.topo
        from .facts import _egress_from_iface, _links_between, _out_iface_for, build_segments
        segs, link_seg = build_segments(topo) if topo else ({}, {})
        for r in rows:
            nk = owners.get(r["ip"]) if r["ip"] else None
            links = []
            if nk and topo and prev:
                links = _links_between(prev, None, nk, None, topo, segs, link_seg) or []
            elif r["ip"] and topo and prev and prev in self.facts:
                # first hop outside the lab: the gateway behind an external connector
                nfp = self.facts[prev]
                oif = _out_iface_for(nfp, r["ip"])
                if oif is None and nfp.default_route is not None and nfp.default_route.value.next_hops:
                    via, o = nfp.default_route.value.next_hops[0]     # off-link target: the default route's exit
                    oif = o or (_out_iface_for(nfp, via) if via else None)
                eg = _egress_from_iface(prev, oif, topo, segs, link_seg)
                if eg:
                    links, nk = eg
            out.append({"hop": r["hop"], "ip": r["ip"], "name": self._label(nk) if nk else None, "node_key": nk,
                        "link_keys": links, "rtt_ms": (sum(r["rtts_ms"]) / len(r["rtts_ms"])) if r["rtts_ms"] else None})
            prev = nk if nk and nk in self.facts else None
        _ = view
        return out

    def verify_internet(self, node_key: str, target: str | None = None,
                        targets: tuple[str, ...] = VERIFY_TARGETS) -> ActionResult:
        """Explicit 'Verify Internet' (V6): ping 8.8.8.8 AND 1.1.1.1 from the router (one console burst);
        success (every target answers) marks its path VERIFIED.  ``data`` = {'targets': {ip: ping data},
        'success', 'sent', 'received'} (+ the first target's ping fields, as before)."""
        from . import validate
        tgts = [validate.host(t) for t in ((target,) if target else targets)]
        cmds = [f"ping {t} repeat 5 timeout 1" for t in tgts]
        r = self._user_burst(node_key, cmds, TRACE_TIMEOUT_S + 10.0 * (len(cmds) - 1))
        if isinstance(r, ActionResult):
            return r
        per: dict[str, dict] = {}
        errors = []
        for t, cmd in zip(tgts, cmds):
            out = r.outputs.get(cmd)
            pr = P_IOS.parse_ping(out.text if out else None)
            if pr.ok:
                per[t] = pr.data
            else:
                errors.append(f"{t}: {pr.error or 'ping failed'}")
        ok = bool(per) and not errors and all(d["success"] for d in per.values())
        parts = [f"{t} {d['received']}/{d['sent']}" + (f" ({d['rtt_avg_ms']} ms)" if d.get("rtt_avg_ms") is not None
                                                      else "") for t, d in per.items()] + errors
        text = ", ".join(parts)
        self._event("SIM", "sim", "ok" if ok else "warn", f"{self._label(node_key)} Verify Internet: {text}",
                    [node_key])
        first = next(iter(per.values()), {})
        data = dict(first)
        data.update({"targets": per, "success": ok, "sent": sum(d["sent"] for d in per.values()),
                     "received": sum(d["received"] for d in per.values())})
        if ok:
            with self._lock:
                self.verified_internet[node_key] = self.now()
                p = self._view.paths.get(node_key) if self._view else None
                if p and p.egress_node:
                    self.verified_internet[p.egress_node] = self.now()
        return ActionResult(ok, text, data)

    def configure(self, node_key: str, lines: list[str], save: bool = True,
                  enable_secret: str | None = None) -> ActionResult:
        """Type ``lines`` in 'configure terminal' on the node's console (a user action: callers ask before
        touching an open console), optionally 'write memory', then run the FAST tier so the canvas
        shows the result at once.  The enable secret comes from device-creds, else ``enable_secret``."""
        n = self.topo.nodes.get(node_key) if self.topo else None
        fast_cmds = command_sets(n, self.facts.get(node_key))[0] if n is not None else IOS_FAST
        r = self._user_burst(node_key, list(fast_cmds), CONFIG_TIMEOUT_S + 2.0 * len(lines),
                             config_lines=list(lines), save_config=save, enable_fallback=enable_secret)
        if isinstance(r, ActionResult):
            return r
        cfg = r.outputs.get(CONFIG_KEY)
        if cfg is None or cfg.text is None:
            return ActionResult(False, (cfg.error if cfg else None) or r.reason or "configuration not applied")
        if cfg.error:
            return ActionResult(False, f"IOS rejected: {cfg.error}", {"errors": cfg.error})
        saved = r.outputs.get(SAVE_KEY)
        text = f"{len(lines)} line(s) applied" + (", saved (write memory)" if saved is not None and saved.text
                                                  else (", NOT saved" if save else ""))
        self._event("SIM", "sim", "ok", f"{self._label(node_key)}: configuration applied ({len(lines)} lines)",
                    [node_key])
        return ActionResult(True, text, {"saved": bool(saved is not None and saved.text)})

    def _label(self, nk: str | None) -> str:
        if not nk or not self.topo or nk not in self.topo.nodes:
            return nk or "?"
        return self.topo.nodes[nk].label

    # ------------------------------------------------------------- view
    def _build_view(self, topo: Topology, now: float) -> TelemetryView:
        with self._lock:
            facts = dict(self.facts)
            topo = self._live_kvm_names(topo, facts)
            join = join_facts(topo, facts, self.link_history, now, FRESH_S, self.verified_internet, VERIFY_TTL_S)
            nodes = {nk: self._node_view(nk, n, topo, facts, join, now) for nk, n in topo.nodes.items()}
            self._link_events(topo, join, now)
            self._path_events(topo, join, now)
            self._mismatch_events(topo, join)
            self._ec_events(join)
            show_notice = (not self._probe_notice_shown and self.probing_enabled and
                           any(n.collector_state == CollectorState.OK for n in nodes.values()))
            return TelemetryView(now, self.lab_id or "", self.probing_enabled, nodes, join.links, join.segments,
                                 join.paths, dict(self.traffic), dict(self.conditions), self._seq,
                                 dict(self.capabilities), join.rid_map, FRESH_S, show_notice, dict(join.bundles))

    def _node_view(self, nk: str, n: TopoNode, topo: Topology, facts, join: JoinResult, now: float) -> NodeTelemetry:
        nf = facts.get(nk)
        s = self.sched.get(nk)
        if not (n.role == NodeRole.ROUTER or n.collected):
            st = CollectorState.NOT_PROBED
        elif not n.booted:
            st = CollectorState.OFF
        elif not n.probed:
            st = CollectorState.UNVERIFIED
        elif not self.probing_enabled:
            st = CollectorState.DISABLED
        elif self._paused(nk):
            st = CollectorState.PAUSED_CONSOLE
        elif nf is None or (nf.ifaces is None and nf.collector_state in (CollectorState.LEARNING, CollectorState.OK)):
            st = CollectorState.LEARNING
        else:
            st = nf.collector_state
        nt = NodeTelemetry(nk, n.label, n.role, n.vendor, st,
                           (nf.collector_detail if nf else "") if st not in (CollectorState.PAUSED_CONSOLE,) else
                           "console in use - probing paused",
                           probing=bool(s and s.running),
                           console_open=bool(self.console_uuid.get(nk) and self.console_uuid[nk] in self.console_open),
                           probe_while_open=nk in self.probe_while_open)
        if st == CollectorState.UNVERIFIED:
            nt.collector_detail = ("not read: no management IP saved for the Juniper collector"
                                   if n.vendor == "juniper" else
                                   f"not read ({'FortiOS' if n.vendor == 'fortinet' else n.vendor} collector "
                                   "not wired yet)")
        if nf is None:
            return nt
        # legend 'Freshness': age of the interface facts that drive link state (a ping/trace burst refreshes
        # nothing, so it must not make a stale router look fresh)
        nt.last_probe_age_s = (now - nf.ifaces.ts) if nf.ifaces else \
            ((now - nf.last_burst_ts) if nf.last_burst_ts else None)
        nt.hostname, nt.loopback = nf.hostname, nf.loopback
        nt.rid = nf.rid.value if nf.rid else None
        nt.ifaces = sorted(nf.ifaces.value.values(), key=lambda f: _ifsort(f.name)) if nf.ifaces else []
        nt.addrs = effective_addrs(nf)
        ages = {}
        for fld in ("ifaces", "ospf_nbrs", "bgp", "fhrp", "default_route", "connected", "config", "rid"):
            fct = getattr(nf, fld)
            ages[fld] = fct.age(now) if fct is not None else None
        nt.ages = ages
        names = join.rid_map
        label = {k: x.label for k, x in topo.nodes.items()}
        if nf.ospf_nbrs:
            nt.ospf_nbrs = [{"neighbor_id": x.neighbor_id, "name": label.get(names.get(x.neighbor_id, ""), None),
                             "state": x.state, "adj_state": x.adj_state, "role": x.role, "iface": short_iface(x.iface),
                             "address": x.address} for x in nf.ospf_nbrs.value]
        if nf.bgp and nf.bgp.value.active:
            b = nf.bgp.value
            nt.bgp = {"local_as": b.local_as, "rid": b.rid,
                      "peers": [{"ip": p.ip, "name": label.get(names.get(p.ip, ""), None), "remote_as": p.remote_as,
                                 "ebgp": (p.remote_as != b.local_as) if b.local_as is not None else None,
                                 "state": p.state, "established": p.established, "pfx_rcvd": p.pfx_rcvd,
                                 "uptime": p.uptime} for p in b.peers]}
        nt.fhrp = list(nf.fhrp.value) if nf.fhrp else []
        if nf.accounts is not None:
            nt.accounts, nt.accounts_age_s = nf.accounts.value, nf.accounts.age(now)
        nt.accounts_note = nf.accounts_note
        if nf.accounts is None and not nt.accounts_note and s is not None and s.learned:
            nt.accounts_note = "reading…" if s.config_wanted else "needs enable secret"
        if n.role == NodeRole.SWITCH and n.managed:
            nt.switch = self._switch_view(nk, n, nf, topo, facts, join, now)
            nt.summary = node_summary(nk, topo, facts, join)
            nt.chips = self._switch_chips(nk, nf, nt, now)
            if nf.config:
                nt.config_text, nt.config_age_s = nf.config.value.text, nf.config.age(now)
            return nt
        if nf.config:
            nt.config_text, nt.config_age_s = nf.config.value.text, nf.config.age(now)
        nt.default_route = nf.default_route.value if nf.default_route else None
        nt.default_path = join.paths.get(nk)
        nt.chips = self._chips(nk, nf, nt, join, topo, now)
        stale_fhrp = nf.fhrp is not None and nf.fhrp.age(now) > FRESH_S
        nt.fhrp_badges = [FhrpBadge(g.proto, g.group, g.role, g.is_primary, short_iface(g.iface), g.vip, g.priority,
                                    f"{g.proto.value} group {g.group} on {short_iface(g.iface)}: {g.role}"
                                    f" (priority {g.priority}{', preempt' if g.preempt else ''}), virtual IP {g.vip}",
                                    stale_fhrp) for g in nt.fhrp]
        nt.summary = node_summary(nk, topo, facts, join)
        return nt

    def _switch_view(self, nk, n: TopoNode, nf: NodeFacts, topo: Topology, facts, join: JoinResult,
                     now: float) -> SwitchView:
        from .parsers.ios_l2 import vlan_text
        sv = SwitchView()
        l2 = nf.l2
        if l2 is None:
            return sv
        label = {k: x.label for k, x in topo.nodes.items()}
        if l2.stp_mode:
            sv.stp_mode = l2.stp_mode.value
            sv.stp_label = STP_MODE_LABELS.get(sv.stp_mode, sv.stp_mode)
        sv.routing = l2.routing.value if l2.routing else None
        sv.l3 = switch_is_l3(nf)
        sv.vlans = sorted(l2.user_vlans().values(), key=lambda v: v.id)
        stp = l2.stp.value if l2.stp else {}
        sv.stp = [stp[v] for v in sorted(stp)]
        sv.root_for = sorted(v for v, x in stp.items() if x.is_root) if stp else \
            (list(l2.root_for.value) if l2.root_for else [])
        sv.root_of = root_names(nk, topo, facts)
        sv.tc = dict(l2.tc.value) if l2.tc else {}
        # neighbours on the canvas, per port
        nbr = {}
        for lk, l in topo.links.items():
            for x, xi, y, yi in ((l.a, l.iface_a, l.b, l.iface_b), (l.b, l.iface_b, l.a, l.iface_a)):
                if x == nk:
                    nbr[iface_key(xi)] = f"{label.get(y, '?')} {short_iface(yi)}"
        ports = l2.ports.value if l2.ports else {}
        trunks = l2.trunks.value if l2.trunks else {}
        keys = sorted(set(ports) | set(trunks), key=_ifsort)
        blocked: dict[tuple[str, str], list[int]] = {}
        for k in keys:
            sp, tr = ports.get(k), trunks.get(k)
            short = sp.port if sp else tr.port
            # a bundled EtherChannel member shows its Port-channel's trunk + STP (STP runs on the Po)
            ecg = nf.ec.group_of(short) if nf.ec is not None and nf.ec.summary is not None else None
            po = ecg[0]["port_channel"] if ecg and "P" in ecg[1]["flags"] else None
            stp_key = iface_key(po) if po else k
            if po and tr is None:
                tr = trunks.get(stp_key)
            if tr is not None:
                mode, vlan_txt = "trunk", f"{tr.active_text or tr.allowed_text} (native {tr.native})"
            elif sp is not None and sp.mode == PortMode.ACCESS:
                mode, vlan_txt = "access", str(sp.vlan)
            else:
                mode, vlan_txt = (sp.mode.value if sp else "?"), (str(sp.vlan) if sp and sp.vlan else "")
            if ecg is not None and not k.startswith("port-channel"):
                mode += f" · {ecg[0]['port_channel']}" + ("" if po else f" ({ecg[1]['flags']})")
            groups: dict[tuple[str, str], list[int]] = {}
            for vid in sorted(stp):
                pt = stp[vid].ports.get(stp_key)
                if pt is not None:
                    groups.setdefault((pt.role.value, pt.state.value), []).append(vid)
                    if pt.state == PortState.BLK and not po:      # the Po itself is listed as blocked
                        blocked.setdefault((short, ROLE_WORDS.get(pt.role, pt.role.value)), []).append(vid)
            stp_txt = " · ".join(f"{r} {s_}" + (f" (v{vlan_text(v)})" if len(groups) > 1 else "")
                                 for (r, s_), v in groups.items())
            if po and stp_txt:
                stp_txt = f"via {po}: {stp_txt}"
            f = nf.iface(short)
            sv.ports.append({"port": short, "desc": (f.desc if f and f.desc else (sp.name if sp else None)),
                             "status": sp.status if sp else (tr.status if tr else "?"), "mode": mode,
                             "vlan_text": vlan_txt, "stp_text": stp_txt,
                             "blocked": any(s_ == "BLK" for (_, s_) in groups),
                             "err_disabled": bool(sp and sp.status == "err-disabled"),
                             "side": f.side if f else None, "neighbor": nbr.get(k)})
        sv.blocked = [(p, r, v) for (p, r), v in blocked.items()]
        sv.err_disabled = [p["port"] for p in sv.ports if p["err_disabled"]]
        sv.svis = [f for f in (nf.ifaces.value.values() if nf.ifaces else []) if f.name.lower().startswith("vlan")]
        if l2.mac_counts:
            sv.mac_counts = {v: c for v, c in l2.mac_counts.value.items()}
            sv.mac_total = sum(sv.mac_counts.values())
        sv.mac_table = list(l2.mac_table.value) if l2.mac_table else []
        sv.cdp = list(l2.cdp.value) if l2.cdp else []
        sv.etherchannel = l2.etherchannel.value if l2.etherchannel else None
        for bk, bd in join.bundles.items():
            for side in (bd.side_a, bd.side_b):
                if side is not None and side.node == nk:
                    sv.ec_sides.append(side)
                    sv.ec_bundles[side.po] = bk
        sv.ec_sides.sort(key=lambda x: int(x.po[2:]) if x.po and x.po[2:].isdigit() else 0)
        sv.ages = {k: (getattr(l2, k).age(now) if getattr(l2, k) is not None else None)
                   for k in ("vlans", "trunks", "ports", "stp", "stp_mode", "tc", "mac_counts", "mac_table", "cdp")}
        return sv

    def _switch_chips(self, nk, nf: NodeFacts, nt: NodeTelemetry, now) -> list[Chip]:
        """Switch tile chip row: ROOT (crown), L2/L3, VLAN count, STP mode."""
        from .parsers.ios_l2 import vlan_text
        sv = nt.switch
        chips = []
        l2 = nf.l2
        if sv is None or l2 is None:
            return chips
        stp_age = l2.stp.age(now) if l2.stp else None
        if sv.root_for:
            vl = vlan_text(sv.root_for)
            chips.append(_age_chip(Chip("ROOT", f"ROOT v{vl}", Health.OK, "",
                                        f"{nt.label} is the spanning-tree root bridge for VLAN {vl}: every other "
                                        "switch builds its loop-free tree towards it"), stp_age, FAST_S))
        kind = "L3" if sv.l3 else "L2"
        svis = [f"{f.short} {f.ip}" for f in sv.svis if f.ip]
        tip = ("Layer-3 switch: ip routing is on and it routes between its VLAN interfaces (" + ", ".join(svis) + ")"
               if sv.l3 else "Layer-2 switch (IOSvL2)" + (f"; management SVI {', '.join(svis)}" if svis else ""))
        chips.append(_age_chip(Chip(kind, kind, Health.OK, "", tip), nf.ifaces.age(now) if nf.ifaces else None,
                               FAST_S))
        if l2.vlans is not None:
            conf = [v for v in sv.vlans if v.id != 1]
            names = ", ".join(f"{v.id} {v.name}" for v in conf) or "only the default VLAN 1"
            chips.append(_age_chip(Chip("VLAN", f"{len(conf)} VLAN{'s' if len(conf) != 1 else ''}",
                                        Health.OK, "", f"VLANs: {names}"), l2.vlans.age(now), FAST_S))
        if sv.stp_label:
            blk = "; " + "; ".join(f"{p} {r.lower()} (blocking) for VLAN {vlan_text(v)}" for p, r, v in sv.blocked) \
                if sv.blocked else ""
            roots = sorted(set(sv.root_of.values()))
            h = Health.DOWN if any(pt.inconsistent for x in sv.stp for pt in x.ports.values()) else Health.OK
            chips.append(_age_chip(Chip("STP", sv.stp_label, h, "",
                                        f"spanning tree {sv.stp_mode}; root bridge: {', '.join(roots) or '?'}{blk}"),
                                   stp_age, FAST_S))
        return chips

    def _chips(self, nk, nf: NodeFacts, nt: NodeTelemetry, join: JoinResult, topo: Topology, now) -> list[Chip]:
        chips = []
        label = {k: x.label for k, x in topo.nodes.items()}
        # ---- OSPF
        if nf.ospf_running and nf.ospf_running.value:
            age = nf.ospf_nbrs.age(now) if nf.ospf_nbrs else None
            hs = [seg.protocols[Proto.OSPF] for seg in join.segments.values()
                  if Proto.OSPF in seg.protocols and any(m[0] == nk for m in seg.members)]
            nbrs = nt.ospf_nbrs
            full = [x for x in nbrs if x["adj_state"] == "FULL"]
            if any(h == Health.DOWN for h in hs):
                h, g = Health.DOWN, "!"
            elif any(h == Health.PARTIAL for h in hs) or (nbrs and len(full) < len(nbrs)):
                h, g = Health.PARTIAL, "…"
            else:
                h, g = Health.OK, "✓"
            desc = ", ".join(f"{x['name'] or x['neighbor_id']} on {x['iface']}"
                             + (f" ({x['role']})" if x["role"] not in ("-", "") else "") for x in full)
            tip = (f"OSPF: {len(full)} neighbour{'s' if len(full) != 1 else ''} FULL"
                   + (f" ({desc})" if desc else "") + (f"; router-id {nt.rid}" if nt.rid else ""))
            if h == Health.DOWN:
                tip += "; OSPF runs on a link but no neighbour answers"
            chips.append(_age_chip(Chip("OSPF", "OSPF", h, g, tip), age, FAST_S))
        # ---- BGP
        if nf.bgp and nf.bgp.value.active and nt.bgp:
            peers = nt.bgp["peers"]
            up = [p for p in peers if p["established"]]
            if peers and len(up) == len(peers):
                h, g = Health.OK, "✓"
            elif up:
                h, g = Health.PARTIAL, "…"
            elif any(p["state"].startswith(("Active", "Connect", "Open")) for p in peers):
                h, g = Health.DOWN, "…"
            else:
                h, g = Health.DOWN, "!"
            parts = [f"{p['name'] or p['ip']}, AS {p['remote_as']}" for p in up]
            pfx = sum(p["pfx_rcvd"] or 0 for p in up)
            tip = (f"BGP AS {nt.bgp['local_as']}: {len(up)} peer{'s' if len(up) != 1 else ''} up"
                   + (f" ({'; '.join(parts)})" if parts else "") + f", {pfx} prefixes received")
            down = [p for p in peers if not p["established"]]
            if down:
                tip += "; not established: " + ", ".join(f"{p['name'] or p['ip']} ({p['state']})" for p in down)
            chips.append(_age_chip(Chip("BGP", f"BGP {nt.bgp['local_as']}", h, g, tip), nf.bgp.age(now), FAST_S))
        # ---- default route (V6)
        p = nt.default_path
        dr = nt.default_route
        if p is not None and (dr is not None or p.state not in (PathState.UNKNOWN,)):
            age = nf.default_route.age(now) if nf.default_route else None
            if dr and dr.present and p.state in (PathState.EGRESS, PathState.VERIFIED_INTERNET, PathState.BROKEN_AT,
                                                 PathState.STALE, PathState.UNKNOWN, PathState.NO_EGRESS):
                text = dr.source_label or dr.code or "0/0"
                if p.state == PathState.NO_EGRESS:
                    text, h, g = "0/0 no exit", Health.DOWN, "!"
                elif p.state == PathState.BROKEN_AT:
                    h, g = Health.DOWN, "✕"
                elif p.state in (PathState.STALE, PathState.UNKNOWN):
                    h, g = Health.UNKNOWN, "⏱"
                else:
                    h, g = Health.OK, ""
                first = p.hops[0] if p.hops else None
                nh = dr.next_hops[0] if dr.next_hops else (None, None)
                src_word = {"static": "a static route" + (" from DHCP" if dr.via_dhcp else ""),
                            "bgp": "BGP", "ospf": "OSPF"}.get(dr.proto or "", dr.proto or "?")
                via_name = label.get(first.to_node) if first and first.to_node else None
                tip = (f"default route learned from {src_word}"
                       + (f" (neighbour {via_name})" if via_name and dr.proto in ("bgp", "ospf") else "")
                       + f"; next hop {nh[0] or '-'}" + (f" via {short_iface(nh[1])}" if nh[1] else
                                                          (f" via {first.out_iface}" if first and first.out_iface else ""))
                       + f". {p.detail}")
            else:
                text, h, g = {PathState.NONE: ("0/0 ✕", Health.DOWN, ""),
                              PathState.BLACKHOLE: ("0/0 ✕ blackhole", Health.DOWN, ""),
                              PathState.UNRESOLVED: ("0/0 ?", Health.UNKNOWN, ""),
                              PathState.LOOP: (f"0/0 loop {label.get(p.loop_pair[0], '?')}↔{label.get(p.loop_pair[1], '?')}"
                                               if p.loop_pair else "0/0 loop", Health.DOWN, "")}.get(
                    p.state, ("0/0 ?", Health.UNKNOWN, ""))
                tip = p.detail
            c = _age_chip(Chip("DEFAULT", text, h, g, tip, outline=True), age, FAST_S)
            c.blue_bar = p.state in (PathState.EGRESS, PathState.VERIFIED_INTERNET) and not c.stale
            chips.append(c)
        return chips

    # ------------------------------------------------------------- link + path events (D4/V10)
    def _link_events(self, topo: Topology, join: JoinResult, now: float) -> None:
        loggable = DOWN_KINDS | {LinkKind.UP}
        for lk, lf in join.links.items():
            kind = lf.status.kind
            lg = self._linklog.setdefault(lk, _LinkLog())
            if kind not in loggable:
                lg.cand, lg.cand_count = None, 0
                continue
            if kind == lg.logged:
                lg.cand, lg.cand_count = None, 0
                continue
            sig = (self._fact_ts(topo.links[lk].a), self._fact_ts(topo.links[lk].b))
            if lg.cand != kind:
                lg.cand, lg.cand_count, lg.cand_sig = kind, 1, sig
            elif sig != lg.cand_sig:
                lg.cand_count += 1
                lg.cand_sig = sig
            immediate = lf.status.api_certain or self._recent_updown(topo.links[lk], now)
            need = 1 if immediate else 2
            if kind == LinkKind.UP and lg.logged is None:
                lg.logged = kind               # initial UP is not news
                continue
            if lg.cand_count < need:
                continue
            prev = lg.logged
            lg.logged = kind
            lg.cand, lg.cand_count = None, 0
            lg.transitions.append(now)
            while lg.transitions and now - lg.transitions[0] > FLAP_WINDOW_S:
                lg.transitions.popleft()
            name = self._link_name(lk)
            if len(lg.transitions) >= 3:
                if now >= lg.flapping_until:
                    self._event("FLAPPING", "link", "warn", f"{name}: {len(lg.transitions)} changes in 30 s", [lk])
                lg.flapping_until = now + FLAP_WINDOW_S
                continue
            if kind == LinkKind.UP:
                if prev in DOWN_KINDS:
                    self._event("LINK UP", "link", "ok", f"{name}", [lk])
            else:
                side = ""
                if lf.status.side_node:
                    side = f": {self._label(lf.status.side_node)} side {lf.status.cause}"
                elif kind == LinkKind.CABLE_PULLED:
                    side = ": cable pulled"
                self._event("LINK DOWN", "link", "error", f"{name}{side}", [lk])

    def _fact_ts(self, nk: str):
        nf = self.facts.get(nk)
        return nf.ifaces.ts if nf and nf.ifaces else None

    def _recent_updown(self, l: TopoLink, now: float) -> bool:
        for nk, ifl in ((l.a, l.iface_a), (l.b, l.iface_b)):
            h = self.link_history.get(("if", nk, iface_key(ifl)))
            if h and now - h.get("syslog_ts", 0) < 60:
                return True
        return False

    def _path_events(self, topo: Topology, join: JoinResult, now: float) -> None:
        for nk, p in join.paths.items():
            sig = (p.state, tuple(tuple(h.link_keys) for h in p.hops))
            prev = self._path_state.get(nk)
            learning = p.state in (PathState.STALE, PathState.UNKNOWN) or \
                (p.state == PathState.BROKEN_AT and p.broken_kind == LinkKind.UNKNOWN)
            if prev is None:
                if not learning:
                    self._path_state[nk] = sig        # first real state = baseline, not an event
                continue
            if prev == sig:
                continue
            self._path_state[nk] = sig
            good = {PathState.EGRESS, PathState.VERIFIED_INTERNET}
            lbl = self._label(nk)
            if p.state == PathState.BROKEN_AT and p.broken_kind == LinkKind.UNKNOWN:
                self._path_state[nk] = prev          # 'unknown' hop: not news, keep the last real state
                continue
            if prev[0] in good and p.state not in good and p.state not in (PathState.STALE, PathState.UNKNOWN):
                self._event("DEFAULT", "default", "error", f"{lbl}: default path lost ({p.detail})",
                            [nk] + ([p.broken_at] if p.broken_at else []))
            elif prev[0] not in good and p.state in good and prev[0] not in (PathState.STALE, PathState.UNKNOWN):
                self._event("DEFAULT", "default", "ok", f"{lbl}: default path restored ({p.detail})", [nk])
            elif prev[0] in good and p.state in good and prev[1] != sig[1]:
                self._event("DEFAULT", "default", "info", f"{lbl}: default path rerouted ({p.detail})", [nk])


# ======================================================================= module helpers
def _only_needs_secret(res) -> bool:
    """PARTIAL only because privilege-15 commands were skipped for lack of an enable secret."""
    if res.status != BurstStatus.PARTIAL:
        return False
    failed = [o for o in res.outputs.values() if o.text is None or o.error]
    return bool(failed) and all(o.error == "needs enable secret" for o in failed)


def command_sets(n: TopoNode | None, nf: NodeFacts | None = None) -> tuple[list[str], list[str]]:
    """(FAST, SLOW) command lists for a collected node.  ``nf`` (the node's facts) adds the EtherChannel set
    once the device is known to have a channel-group (switch) or a Port-channel interface (router)."""
    switch = n is not None and n.role == NodeRole.SWITCH and n.managed
    fast, slow = (list(SW_FAST), list(SW_SLOW)) if switch else (list(IOS_FAST), list(IOS_SLOW))
    ec = nf.ec if nf is not None else None
    groups = ec.groups() if ec is not None else []
    has_po = nf is not None and nf.ifaces is not None and any(k.startswith("port-channel") for k in nf.ifaces.value)
    if groups or (switch and has_po):
        fast += [c for c in EC_FAST if c not in fast]
        slow = [c for c in slow if c not in EC_FAST]
        protos = {g.get("proto") for g in groups}
        if "lacp" in protos:
            slow += EC_SLOW_LACP
        if "pagp" in protos:
            slow += EC_SLOW_PAGP
        slow += EC_SLOW_ANY
    elif has_po:                                     # an IOS router with a Port-channel: discover it (SLOW)
        slow += EC_FAST
    return fast, slow


def _extra_timeout(n: TopoNode | None, nf: NodeFacts | None = None) -> float:
    extra = SWITCH_BURST_TIMEOUT_S - BURST_TIMEOUT_S if (n is not None and n.role == NodeRole.SWITCH and n.managed) \
        else 0.0
    if nf is not None and nf.ec is not None and nf.ec.groups():
        extra += EC_EXTRA_TIMEOUT_S
    return extra


def _l2_snapshot(nf: NodeFacts) -> dict:
    """Comparable L2 state for the event log (None = not read yet)."""
    from .parsers.ios_l2 import vlan_text
    l2 = nf.l2
    out = {"stp": None, "root": {}, "tc": None, "vlans": None, "trunks": None}
    if l2 is None:
        return out
    if l2.stp is not None:
        out["stp"] = {(pt.port, vid): (pt.role.value, pt.state.value)
                      for vid, sv in l2.stp.value.items() for pt in sv.ports.values()}
        out["root"] = {vid: sv.root_address for vid, sv in l2.stp.value.items()}
    if l2.tc is not None:
        out["tc"] = {vid: (t.changes, t.from_iface) for vid, t in l2.tc.value.items()}
    if l2.vlans is not None:
        out["vlans"] = {v.id: v.name for v in l2.user_vlans().values()}
    if l2.trunks is not None:
        out["trunks"] = {t.port: (t.active_text or vlan_text([])) for t in l2.trunks.value.values()
                         if t.status == "trunking"}
    return out


def _tiers_of(cmds: list[str]) -> list[str]:
    t = []
    if any(c in IOS_FAST or c in SW_FAST for c in cmds):
        t.append("fast")
    if any(c in IOS_SLOW or c in SW_SLOW for c in cmds):
        t.append("slow")
    if any(c in IOS_CONFIG for c in cmds):
        t.append("config")
    return t


def _age_chip(c: Chip, age: float | None, interval: float) -> Chip:
    c.age_s = age
    if age is None:
        return c
    if age > FRESH_S:
        c.stale = True
        c.glyph = "⏱"
    if age > FADE_FACTOR * interval:
        c.faded = True
    return c


def _ifsort(name: str):
    return [int(x) if x.isdigit() else x for x in re.split(r"(\d+)", name)]


def _proto_snapshot(nf: NodeFacts) -> dict:
    snap = {"ospf": {}, "bgp": {}, "fhrp": {}, "empty": nf.ifaces is None}
    if nf.ospf_nbrs:
        snap["ospf"] = {(x.neighbor_id, iface_key(x.iface)): x.adj_state for x in nf.ospf_nbrs.value}
    if nf.bgp:
        snap["bgp"] = {p.ip: p.state for p in nf.bgp.value.peers}
    if nf.fhrp:
        snap["fhrp"] = {(g.proto.value, iface_key(g.iface), g.group): g.state for g in nf.fhrp.value}
    return snap


def _condition_from_api(c: dict, ts: float) -> LinkCondition:
    op = c.get("operational") if isinstance(c.get("operational"), dict) else None
    src = op if (op and any(v is not None for v in op.values())) else c
    params = {k: v for k, v in src.items() if k not in ("enabled", "operational") and v not in (None, 0, 0.0)}
    enabled = bool(c.get("enabled")) and bool(params)
    parts = []
    if "latency" in params:
        parts.append(f"{params['latency']} ms" + (f" ±{params['jitter']}" if params.get("jitter") else ""))
    if "bandwidth" in params:
        bw = params["bandwidth"]
        parts.append(f"{bw / 1000:g} Mbit/s" if bw >= 1000 else f"{bw} kbit/s")
    if "loss" in params:
        parts.append(f"{params['loss']:g} % loss")
    for k, w in (("duplicate", "dup"), ("reorder_prob", "reorder"), ("corrupt_prob", "corrupt")):
        if k in params:
            parts.append(f"{params[k]:g} % {w}")
    return LinkCondition(enabled, params, " · ".join(parts) if enabled else "", ts)


def host_mem_available_gb() -> float | None:
    """MemAvailable from /proc/meminfo (V8 status bar; cheap, GUI-thread safe).  Windows: GlobalMemoryStatusEx."""
    if plat.IS_WINDOWS:
        return _windows_mem_available_gb()
    try:
        with open("/proc/meminfo") as fh:
            for line in fh:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) / 1024 / 1024
    except (OSError, ValueError):
        pass
    return None


def _windows_mem_available_gb() -> float | None:
    import ctypes

    class MEMORYSTATUSEX(ctypes.Structure):
        _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
    try:
        st = MEMORYSTATUSEX()
        st.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):
            return st.ullAvailPhys / 1024 ** 3
    except (AttributeError, OSError):
        pass
    return None


# ======================================================================= topology adapter
def topology_from_snapshot(lab: dict, nodes: list[dict], links: list[dict], interfaces: list[dict],
                           kvm_nodes: dict[str, dict] | None = None, kvm_links: list[dict] | None = None,
                           kvm_states: dict[str, str] | None = None,
                           kvm_mgmt: dict[str, str] | None = None) -> tuple[Topology, dict[str, str]]:
    """CML snapshot (+ LabDesk KVM state) -> (Topology, console uuid per node key).  ``kvm_mgmt``: node key ->
    management IP; a vJunos with one is read live over SSH (``probed``), any other KVM router is not read."""
    if_by_id = {i["id"]: i for i in interfaces}
    tn: dict[str, TopoNode] = {}
    consoles: dict[str, str] = {}
    for n in nodes:
        nd = n.get("node_definition", "")
        dt = type_for_cml_node(n)
        key = f"cml:{n['id']}"
        mode = None
        if nd == "external_connector":
            if dt.key == "ext_cloud":                           # the LabDesk Internet cloud (node tag)
                mode = "Cloud"
            elif n.get("configuration") in (None, "", []):      # payload without configs: use the label
                mode = "NAT" if "NAT" in str(n.get("label", "")).upper() else "Bridge"
            else:
                mode = "NAT" if dt.key == "ext_nat" else "Bridge"
        tn[key] = TopoNode(key, n.get("label", "?"), topo_role(nd), dt.vendor, n.get("state", ""), nd, mode,
                           managed=nd in MANAGED_SWITCH_DEFS)
        op = n.get("operational") or {}
        for sc in (op.get("serial_consoles") or []):
            if sc.get("device_number") == 0 and sc.get("console_key"):
                consoles[key] = sc["console_key"]
    tl: dict[str, TopoLink] = {}
    for l in links:
        ia, ib = if_by_id.get(l.get("interface_a"), {}), if_by_id.get(l.get("interface_b"), {})
        na = l.get("node_a") or ia.get("node")
        nb = l.get("node_b") or ib.get("node")
        key = f"cml:{l['id']}"
        tl[key] = TopoLink(key, f"cml:{na}", f"cml:{nb}", ia.get("label", ""), ib.get("label", ""),
                           l.get("state", ""))
    for name, meta in (kvm_nodes or {}).items():
        key = f"kvm:{name}"
        dt = BY_KEY.get(meta.get("kind"), BY_KEY["vjunos"])
        tn[key] = TopoNode(key, name, topo_role("", "kvm", meta.get("kind", "")), dt.vendor,
                           (kvm_states or {}).get(name, "unknown"), meta.get("kind", ""),
                           probed=dt.vendor == "juniper" and bool((kvm_mgmt or {}).get(key)))
    for i, kl in enumerate(kvm_links or []):
        key = f"kvm:{i}"
        tl[key] = TopoLink(key, kl["a"], kl["b"], kl.get("label_a", ""), kl.get("label_b", ""), "up", kvm=True,
                           mac_a=kl.get("mac_a", "") or "", mac_b=kl.get("mac_b", "") or "")
    return Topology(lab.get("state", ""), tn, tl), consoles


__all__ = ["TelemetryEngine", "TelemetryView", "NodeTelemetry", "Chip", "FhrpBadge", "LinkTraffic", "LinkCondition",
           "TelemetryEvent", "ActionResult", "topology_from_snapshot", "LINK_CONDITION_PRESETS", "FRESH_S", "FAST_S",
           "SLOW_S", "IOS_FAST", "IOS_SLOW", "IOS_CONFIG", "IOS_PARSERS", "SW_FAST", "SW_SLOW", "SwitchView",
           "command_sets", "open_console_uuids",
           "host_mem_available_gb"]
