"""LabController: all lab operations, UI independent (called from worker threads).

Node keys:  "cml:<node-id>"  for CML nodes,  "kvm:<vm-name>"  for host-KVM VMs.
Link keys:  "cml:<link-id>"  for CML links,  "kvm:<index>"    for host-VM links.
"""
from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from . import cloud, config, kvm, launchers, validate
from .catalog import BY_KEY, NON_COUNTING_DEFS, DeviceType, default_config, next_label, type_for_cml_node
from .cml_api import (ACTIVE_STATES, CMLClient, CMLError, NodeLockedError, Snapshot, node_needs_wipe_for_new_link,
                      short_ifname)


@dataclass
class NodeView:
    key: str
    label: str
    dtype: DeviceType
    state: str
    x: float
    y: float
    raw: dict = field(default_factory=dict)
    # ---- telemetry (D3/V4/V5/V6/V10); None/empty when telemetry is off - see docs/telemetry_model.md
    tele: object = None                     # telemetry.NodeTelemetry | None
    chips: list = field(default_factory=list)          # [telemetry.Chip]  (OSPF, BGP, DEFAULT)
    fhrp: list = field(default_factory=list)           # [telemetry.FhrpBadge]
    default_chip: object = None             # telemetry.Chip | None (kind == 'DEFAULT')
    collector_state: object = None          # facts.CollectorState | None


@dataclass
class LinkView:
    key: str
    a: str
    b: str
    label_a: str
    label_b: str
    state: str
    dashed: bool = False
    # ---- telemetry (D3/D4/D5/V2/V3/V6); defaults = "no data" - see docs/telemetry_model.md
    tele: object = None                     # facts.LinkFacts | None
    side_a: object = None                   # facts.LinkSide | None
    side_b: object = None
    status: object = None                   # facts.LinkStatus | None
    p2p: bool = False
    p2p_source: object = None               # facts.P2PSource | None
    p2p_conflict: str | None = None
    subnet_hint: str | None = None
    addressing_mismatch: bool = False
    addressing: object = None               # facts.LinkAddressing | None (subnet label on the cable)
    default_hop: object = None              # facts.RouteHop | None (blue)
    traffic: object = None                  # telemetry.LinkTraffic | None
    condition: object = None                # telemetry.LinkCondition | None
    bundle: str | None = None               # facts.Bundle.key when the cable is an EtherChannel member


@dataclass
class LabView:
    lab_id: str
    title: str
    lab_state: str
    nodes: list[NodeView]
    links: list[LinkView]
    running_total: int
    limit: int = config.CML_FREE_NODE_LIMIT
    telemetry: object = None                # telemetry.TelemetryView | None
    this_lab_running: int = 0               # licence-consuming running nodes of THIS lab


def _invalidating(fn):
    """Mutating controller calls force the next poll to take a full snapshot (D7)."""
    def wrapper(self, *a, **kw):
        try:
            return fn(self, *a, **kw)
        finally:
            self._force_full = True
    wrapper.__name__, wrapper.__doc__ = fn.__name__, fn.__doc__
    return wrapper


@dataclass
class LinkResult:
    link_key: str
    text: str
    needs_restart: list[tuple[str, str]] = field(default_factory=list)   # (key, label) of booted nodes


@dataclass
class CloudPlan:
    """'Auto-configure for Internet cloud' for one router (see LabController.cloud_plan)."""
    router_key: str
    router_label: str
    cloud_key: str
    cloud_label: str
    iface: str                    # full IOS name, e.g. GigabitEthernet0/1
    ip: str                       # 100.64.0.N
    reused: bool                  # the address was already configured (live or day-0)
    conf: object                  # cloud.CloudConf
    ospf: list                    # OSPF process ids on the router (offer default-information originate)
    commands: list                # config-mode lines (without the OSPF part)
    state: str
    booted: bool
    console_open: bool            # a console for the router is open on this host: ask before typing
    day0_now: bool                # node is wiped: the day-0 config can be written right away
    day0_has: bool                # the day-0 config already contains the uplink
    routes: object                # cloud.RoutePlan
    lans: list
    routes_writable: bool
    needs_wipe: bool = False      # the cable is not wired into the running VM yet (CML: wipe + restart)


class LabController:
    def __init__(self, client: CMLClient | None = None, state: config.State | None = None,
                 telemetry: bool = True):
        self.client = client or CMLClient()
        self.state = state or config.State()
        self.lab_id: str | None = None
        self.lab_title: str = ""
        self._last_nodes: dict[str, dict] = {}
        # D7 light polling cache
        self._snap: Snapshot | None = None
        self._snap_lab: str | None = None
        self._snap_ts = 0.0
        self._last_poll = 0.0
        self._other_running = 0
        self._force_full = True
        self.element_state_ok = True
        self.telemetry = None
        if telemetry:
            from .telemetry import TelemetryEngine
            self.telemetry = TelemetryEngine(self.client)
            try:
                self.telemetry.cml_user = config.load_env_file(self.client._cred_file).get("CML_ADMIN_USER", "admin")
            except (OSError, AttributeError):
                pass

    def shutdown(self) -> None:
        """Stop background probe threads (call on application exit)."""
        if self.telemetry:
            self.telemetry.shutdown()

    # ------------------------------------------------------------- lab level
    def list_labs(self) -> list[dict]:
        labs = self.client.labs()
        return sorted(labs, key=lambda l: l.get("lab_title", "").lower())

    def open_lab(self, lab_id: str) -> str:
        lab = self.client.lab(lab_id)
        self.lab_id = lab_id
        self.lab_title = lab.get("lab_title", lab_id)
        self.state.last_lab = lab_id
        return self.lab_title

    def open_lab_by_title(self, title: str) -> str | None:
        lab = self.client.find_lab(title)
        if not lab:
            return None
        return self.open_lab(lab["id"])

    def new_lab(self, title: str) -> str:
        lab = self.client.create_lab(title)
        self.open_lab(lab["id"])
        return lab["id"]

    def import_yaml(self, path: str | Path, title: str | None = None) -> str:
        with open(path, encoding="utf-8") as fh:
            topo = yaml.safe_load(fh)
        if not isinstance(topo, dict) or "nodes" not in topo:
            raise CMLError(f"{Path(path).name} is not a CML topology")
        lab_id = self.client.import_topology(topo, title)
        self.open_lab(lab_id)
        return lab_id

    def export_yaml(self, path: str | Path) -> None:
        Path(path).write_text(self.client.download(self._lab()))

    def delete_lab(self) -> None:
        lab_id = self._lab()
        lab = self.state.lab(lab_id)
        if any(kvm.domstate(vm) != "missing" for vm in lab["kvm"]):
            raise CMLError("delete the host-KVM devices of this lab first")
        try:
            self.client.stop_lab(lab_id)
            self.client.wipe_lab(lab_id)
        except CMLError:
            pass
        self.client.delete_lab(lab_id)
        self.state.data["labs"].pop(lab_id, None)
        self.lab_id = None
        self.state.last_lab = None

    def start_all(self) -> str:
        lab_id = self._lab()
        nodes = self.client.nodes(lab_id)
        wanted = sum(1 for n in nodes if n.get("node_definition") not in NON_COUNTING_DEFS
                     and n.get("state") not in ACTIVE_STATES)
        running = self.client.running_node_count()
        msg = ""
        if running + wanted > config.CML_FREE_NODE_LIMIT:
            msg = (f"warning: {running} running + {wanted} to start exceeds the CML-Free limit of "
                   f"{config.CML_FREE_NODE_LIMIT}; CML will refuse the extra nodes")
        self.client.start_lab(lab_id)
        for vm in self.state.lab(lab_id)["kvm"]:
            if kvm.domstate(vm) == "stopped":
                kvm.start_vm(vm)
        return msg

    def stop_all(self) -> None:
        lab_id = self._lab()
        self.client.stop_lab(lab_id)
        for vm in self.state.lab(lab_id)["kvm"]:
            if kvm.domstate(vm) == "running":
                kvm.stop_vm(vm)

    def wipe_all(self) -> None:
        self.client.wipe_lab(self._lab())
        self._apply_pending_day0(list(self.state.lab(self._lab()).get("day0_pending", {})))

    # ----------------------------------------------------------------- poll
    def _fetch(self, lab_id: str) -> tuple[Snapshot, bool]:
        """D7: full snapshot on start / lab change / mutation / every 30 s / element-set change /
        clock gap; otherwise GET lab + lab_element_state and patch the cached snapshot."""
        now = time.monotonic()
        gap = self._last_poll and now - self._last_poll > 4 * config.POLL_INTERVAL_MS / 1000.0
        self._last_poll = now
        full = (self._snap is None or self._snap_lab != lab_id or self._force_full or gap
                or now - self._snap_ts >= 30.0 or not self.element_state_ok)
        if not full:
            try:
                es = self.client.lab_element_state(lab_id)
                lab = self.client.lab(lab_id)
            except CMLError:
                es, lab = {}, None
            snap = self._snap
            node_ids = {n["id"] for n in snap.nodes}
            link_ids = {l["id"] for l in snap.links}
            if lab is None or set(es.get("nodes", {})) != node_ids or set(es.get("links", {})) != link_ids:
                full = True
            else:
                changed_ifaces = []
                for n in snap.nodes:
                    n["state"] = es["nodes"].get(n["id"], n.get("state"))
                for l in snap.links:
                    l["state"] = es["links"].get(l["id"], l.get("state"))
                ifs = es.get("interfaces") or {}
                for i in snap.interfaces:
                    st = ifs.get(i["id"])
                    if st is not None and st != i.get("state"):
                        changed_ifaces.append(i.get("node"))
                        i["state"] = st
                snap.lab = lab
                mine = sum(1 for n in snap.nodes if n.get("node_definition") not in NON_COUNTING_DEFS
                           and n.get("state") in ACTIVE_STATES)
                snap.running_total = self._other_running + mine
                if changed_ifaces and self.telemetry:
                    self.telemetry.interfaces_changed([f"cml:{x}" for x in changed_ifaces if x])
                return snap, False
        snap = self.client.snapshot(lab_id)
        mine = sum(1 for n in snap.nodes if n.get("node_definition") not in NON_COUNTING_DEFS
                   and n.get("state") in ACTIVE_STATES)
        self._other_running = max(0, snap.running_total - mine)
        self._snap, self._snap_lab, self._snap_ts, self._force_full = snap, lab_id, now, False
        return snap, True

    def poll(self) -> LabView:
        lab_id = self._lab()
        snap, full = self._fetch(lab_id)
        nodes_by_id = {n["id"]: n for n in snap.nodes}
        self._last_nodes = nodes_by_id
        if_by_id = {i["id"]: i for i in snap.interfaces}
        views: list[NodeView] = []
        for n in snap.nodes:
            views.append(NodeView(f"cml:{n['id']}", n.get("label", "?"), type_for_cml_node(n),
                                  n.get("state", "unknown"), float(n.get("x", 0)), float(n.get("y", 0)), n))
        links: list[LinkView] = []
        for l in snap.links:
            ia, ib = if_by_id.get(l.get("interface_a"), {}), if_by_id.get(l.get("interface_b"), {})
            na = l.get("node_a") or ia.get("node")
            nb = l.get("node_b") or ib.get("node")
            links.append(LinkView(f"cml:{l['id']}", f"cml:{na}", f"cml:{nb}",
                                  short_ifname(ia.get("label", "")), short_ifname(ib.get("label", "")),
                                  l.get("state", "")))
        lab_state = self.state.lab(lab_id)
        kvm_states = {}
        for name, meta in lab_state["kvm"].items():
            dtype = BY_KEY.get(meta.get("kind"), BY_KEY["vjunos"])
            kvm_states[name] = kvm.domstate(name)
            views.append(NodeView(f"kvm:{name}", name, dtype, kvm_states[name],
                                  float(meta.get("x", 0)), float(meta.get("y", 0)), {"kind": meta.get("kind")}))
        for i, kl in enumerate(lab_state["kvm_links"]):
            links.append(LinkView(f"kvm:{i}", kl["a"], kl["b"], kl.get("label_a", ""), kl.get("label_b", ""),
                                  "up", dashed=True))
        title = snap.lab.get("lab_title", self.lab_title)
        self.lab_title = title
        mine = sum(1 for n in snap.nodes if n.get("node_definition") not in NON_COUNTING_DEFS
                   and n.get("state") in ACTIVE_STATES)
        view = LabView(lab_id, title, snap.lab.get("state", ""), views, links, snap.running_total,
                       this_lab_running=mine)
        if self.telemetry is not None:
            self._attach_telemetry(view, snap, lab_state, kvm_states, full)
        return view

    def _attach_telemetry(self, view: LabView, snap: Snapshot, lab_state: dict, kvm_states: dict,
                          full: bool) -> None:
        from .telemetry import topology_from_snapshot
        topo, consoles = topology_from_snapshot(snap.lab, snap.nodes, snap.links, snap.interfaces,
                                                lab_state["kvm"], lab_state["kvm_links"], kvm_states)
        for nk, n in topo.nodes.items():           # console keys not in the node payload: fetch once
            if nk.startswith("cml:") and nk not in consoles and n.booted and n.collected \
                    and not self.telemetry.console_uuid.get(nk):
                try:
                    consoles[nk] = self.client.console_key(view.lab_id, nk[4:])
                except CMLError:
                    pass
        tv = self.telemetry.update(topo, view.lab_id, consoles, full)
        view.telemetry = tv
        for nv in view.nodes:
            nt = tv.nodes.get(nv.key)
            if nt is None:
                continue
            nv.tele, nv.chips, nv.fhrp, nv.collector_state = nt, nt.chips, nt.fhrp_badges, nt.collector_state
            nv.default_chip = next((c for c in nt.chips if c.kind == "DEFAULT"), None)
        for lv in view.links:
            lf = tv.links.get(lv.key)
            if lf is None:
                continue
            lv.tele, lv.side_a, lv.side_b, lv.status = lf, lf.side_a, lf.side_b, lf.status
            lv.p2p, lv.p2p_source, lv.p2p_conflict = lf.p2p, lf.p2p_source, lf.p2p_conflict
            lv.subnet_hint, lv.addressing_mismatch, lv.default_hop = lf.subnet_hint, lf.addressing_mismatch, lf.default_hop
            lv.addressing = lf.addressing
            lv.traffic, lv.condition = tv.traffic.get(lv.key), tv.conditions.get(lv.key)
            lv.bundle = lf.bundle

    # ---------------------------------------------------------------- nodes
    def existing_labels(self) -> set[str]:
        labels = {n.get("label", "") for n in self.client.nodes(self._lab())}
        labels |= set(self.state.lab(self._lab())["kvm"].keys())
        return labels

    def add_device(self, type_key: str, x: float, y: float, label: str | None = None, log=print) -> str:
        dtype = BY_KEY[type_key]
        lab_id = self._lab()
        if not label and dtype.key == "ext_cloud":
            existing = self.existing_labels()
            label = cloud.CLOUD_LABEL if cloud.CLOUD_LABEL not in existing else next_label(dtype.prefix, existing)
        label = label or next_label(dtype.prefix, self.existing_labels())
        if dtype.backend == "cml":
            node = self.client.create_node(lab_id, label, dtype.node_definition, int(x), int(y),
                                           default_config(dtype, label), tags=dtype.tags)
            return f"cml:{node['id']}"
        name = validate.virsh_name(label)
        if name in self.existing_labels():
            raise CMLError(f"name {name} already used in this lab")
        kvm.create_vm(name, dtype.key, log=log)
        lab = self.state.lab(lab_id)
        lab["kvm"][name] = {"kind": dtype.key, "x": x, "y": y, "nics": 1}
        self.state.save()
        return f"kvm:{name}"

    def label_of(self, key: str) -> str:
        kind, ident = key.split(":", 1)
        if kind == "kvm":
            return ident
        n = self._last_nodes.get(ident) or self.client.node(self._lab(), ident)
        return n.get("label", ident)

    def move(self, key: str, x: float, y: float) -> None:
        kind, ident = key.split(":", 1)
        if kind == "cml":
            self.client.update_node(self._lab(), ident, x=int(x), y=int(y))
        else:
            meta = self.state.lab(self._lab())["kvm"].get(ident)
            if meta is not None:
                meta.update(x=x, y=y)
                self.state.save()

    def rename(self, key: str, new_label: str) -> str:
        kind, ident = key.split(":", 1)
        if kind != "cml":
            raise CMLError("host-KVM VMs cannot be renamed (libvirt domain name); delete and re-create")
        new_label = validate.cml_label(new_label)
        self.client.update_node(self._lab(), ident, label=new_label)
        return new_label

    def delete(self, key: str) -> None:
        lab_id = self._lab()
        kind, ident = key.split(":", 1)
        lab = self.state.lab(lab_id)
        if kind == "cml":
            st = self.client.node_state(lab_id, ident)
            if st in ACTIVE_STATES:
                self.client.stop_node(lab_id, ident)
                self._wait_state(ident, {"STOPPED", "DEFINED_ON_CORE"}, 120)
            if self.client.node_state(lab_id, ident) == "STOPPED":
                self.client.wipe_node(lab_id, ident)
            self.client.delete_node(lab_id, ident)
            # host-VM links that went through this node
            lab["kvm_links"] = [l for l in lab["kvm_links"] if l.get("connector") != ident and l["b"] != key]
        else:
            for i in reversed(range(len(lab["kvm_links"]))):
                l = lab["kvm_links"][i]
                if key in (l["a"], l["b"]):
                    self._remove_kvm_link(lab, i)
            kvm.delete_vm(ident)
            lab["kvm"].pop(ident, None)
        lab["mgmt"].pop(key, None)
        self.state.save()

    def start(self, key: str) -> str:
        kind, ident = key.split(":", 1)
        if kind == "cml":
            n = self._last_nodes.get(ident) or self.client.node(self._lab(), ident)
            msg = ""
            if n.get("node_definition") not in NON_COUNTING_DEFS:
                running = self.client.running_node_count()
                if running >= config.CML_FREE_NODE_LIMIT:
                    raise CMLError(f"CML-Free limit reached ({running}/{config.CML_FREE_NODE_LIMIT} running): "
                                   "stop another node first")
            self.client.start_node(self._lab(), ident)
            return msg
        kind_name = self.state.lab(self._lab())["kvm"].get(ident, {}).get("kind", "")
        kvm.start_vm(ident)
        return kvm.memory_warning(kind_name) or ""

    def stop(self, key: str) -> None:
        kind, ident = key.split(":", 1)
        if kind == "cml":
            self.client.stop_node(self._lab(), ident)
        else:
            kvm.stop_vm(ident)

    def wipe(self, key: str, wait: bool = True) -> None:
        kind, ident = key.split(":", 1)
        if kind != "cml":
            raise CMLError("wipe applies to CML nodes only")
        lab_id = self._lab()
        if self.client.node_state(lab_id, ident) in ACTIVE_STATES:
            self.client.stop_node(lab_id, ident)
            if wait:
                self._wait_state(ident, {"STOPPED", "DEFINED_ON_CORE"}, 120)
        if self.client.node_state(lab_id, ident) != "DEFINED_ON_CORE":
            self.client.wipe_node(lab_id, ident)
        self._apply_pending_day0([ident])

    def wipe_and_restart(self, key: str) -> None:
        self.wipe(key)
        self.start(key)

    def get_config(self, key: str) -> str:
        kind, ident = key.split(":", 1)
        if kind != "cml":
            raise CMLError("startup configs are managed by CML nodes only (use the console for host VMs)")
        return self.client.node_configuration(self._lab(), ident)

    def set_config(self, key: str, text: str) -> None:
        kind, ident = key.split(":", 1)
        if kind != "cml":
            raise CMLError("not a CML node")
        try:
            self.client.update_node(self._lab(), ident, configuration=text)
        except CMLError as e:
            if e.status in (400, 403, 409, 422):
                raise CMLError(f"CML refused the config change ({e}). The node must be wiped first "
                               "(its config is locked while it has a disk).", e.status) from None
            raise

    def wait_booted(self, keys: list[str], timeout: float = 900) -> bool:
        ids = [k.split(":", 1)[1] for k in keys if k.startswith("cml:")]
        end = time.time() + timeout
        while time.time() < end:
            states = {i: self.client.node_state(self._lab(), i) for i in ids}
            if all(s == "BOOTED" for s in states.values()):
                return True
            time.sleep(5)
        return False

    def _wait_state(self, node_id: str, states: set[str], timeout: float) -> str:
        end = time.time() + timeout
        st = ""
        while time.time() < end:
            st = self.client.node_state(self._lab(), node_id)
            if st in states:
                return st
            time.sleep(1.5)
        raise CMLError(f"node did not reach {sorted(states)} within {timeout:.0f}s (state {st})")

    # ---------------------------------------------------------------- links
    def add_link(self, key_a: str, key_b: str, log=print) -> LinkResult:
        if key_a == key_b:
            raise CMLError("cannot link a node to itself")
        lab_id = self._lab()
        ka, kb = key_a.split(":", 1)[0], key_b.split(":", 1)[0]
        if ka == "cml" and kb == "cml":
            na = self.client.node(lab_id, key_a[4:])
            nb = self.client.node(lab_id, key_b[4:])
            link, ia, ib = self.client.link_nodes(lab_id, na, nb)
            res = LinkResult(f"cml:{link.get('id')}",
                             f"link {na['label']} {short_ifname(ia.get('label', ''))} <-> "
                             f"{nb['label']} {short_ifname(ib.get('label', ''))}")
            for n in (na, nb):
                if n.get("node_definition") not in NON_COUNTING_DEFS and node_needs_wipe_for_new_link(n):
                    res.needs_restart.append((f"cml:{n['id']}", n["label"]))
            return res
        if ka == "kvm" and kb == "kvm":
            return self._link_vm_vm(key_a[4:], key_b[4:])
        vm_key, cml_key = (key_a, key_b) if ka == "kvm" else (key_b, key_a)
        return self._link_vm_cml(vm_key[4:], cml_key[4:], log)

    def _attach(self, vm: str, network: str) -> tuple[str, str]:
        lab = self.state.lab(self._lab())
        meta = lab["kvm"][vm]
        before = set(_domiflist_macs(vm))
        kvm.attach_nic(vm, network)
        after = set(_domiflist_macs(vm))
        mac = next(iter(after - before), "")
        idx = int(meta.get("nics", 1))
        meta["nics"] = idx + 1
        return mac, kvm.guest_nic_name(meta["kind"], idx)

    def _link_vm_vm(self, a: str, b: str) -> LinkResult:
        net = validate.net_name(a, b)
        kvm.ensure_isolated_network(net)
        mac_a, if_a = self._attach(a, net)
        mac_b, if_b = self._attach(b, net)
        lab = self.state.lab(self._lab())
        lab["kvm_links"].append({"a": f"kvm:{a}", "b": f"kvm:{b}", "net": net, "mac_a": mac_a, "mac_b": mac_b,
                                 "label_a": if_a, "label_b": if_b})
        self.state.save()
        return LinkResult(f"kvm:{len(lab['kvm_links']) - 1}",
                          f"link {a} {if_a} <-> {b} {if_b} (isolated libvirt network {net}; "
                          "new NICs appear in the guest after a VM reboot)")

    def _link_vm_cml(self, vm: str, cml_node_id: str, log=print) -> LinkResult:
        """VM <-> CML node: through a CML 'System Bridge' external connector (host virbr0)."""
        lab_id = self._lab()
        lab = self.state.lab(lab_id)
        meta = lab["kvm"][vm]
        node = self.client.node(lab_id, cml_node_id)
        if node.get("node_definition") == "external_connector":
            conn = node
        else:
            label = next_label(f"BR-{vm}-", self.existing_labels()) if f"BR-{vm}" in self.existing_labels() \
                else f"BR-{vm}"
            label = re.sub(r"[^A-Za-z0-9_.\-]", "-", label)[:63]
            ckey = self.add_device("ext_bridge", (meta["x"] + node.get("x", 0)) / 2,
                                   (meta["y"] + node.get("y", 0)) / 2, label)
            conn = self.client.node(lab_id, ckey[4:])
            link, ia, ib = self.client.link_nodes(lab_id, node, conn)
            log(f"created {label} (System Bridge) and linked {node['label']} "
                f"{short_ifname(ia.get('label', ''))} to it")
            if self.client.node_state(lab_id, node["id"]) in ACTIVE_STATES:
                self.client.start_node(lab_id, conn["id"])
        mac, ifname = self._attach(vm, "default")
        lab["kvm_links"].append({"a": f"kvm:{vm}", "b": f"cml:{conn['id']}", "net": "default", "mac_a": mac,
                                 "label_a": ifname, "label_b": "", "connector": conn["id"]})
        self.state.save()
        res = LinkResult(f"kvm:{len(lab['kvm_links']) - 1}",
                         f"link {vm} {ifname} <-> {conn['label']} (System Bridge = host virbr0, "
                         "shared L2 with every bridged device)")
        if node.get("node_definition") not in NON_COUNTING_DEFS and node_needs_wipe_for_new_link(node):
            res.needs_restart.append((f"cml:{node['id']}", node["label"]))
        return res

    def delete_link(self, link_key: str) -> None:
        kind, ident = link_key.split(":", 1)
        if kind == "cml":
            self.client.delete_link(self._lab(), ident)
            return
        lab = self.state.lab(self._lab())
        self._remove_kvm_link(lab, int(ident))
        self.state.save()

    def _remove_kvm_link(self, lab: dict, index: int) -> None:
        l = lab["kvm_links"].pop(index)
        for end, mac in (("a", l.get("mac_a")), ("b", l.get("mac_b"))):
            if l[end].startswith("kvm:") and mac:
                vm = l[end][4:]
                live = kvm.domstate(vm) == "running"
                cmd = kvm.virsh("detach-interface", "--domain", vm, "--type", "network", "--mac", mac, "--config")
                if live:
                    cmd.append("--live")
                try:
                    kvm.run(cmd, timeout=30)
                except kvm.KvmError:
                    pass
        if l.get("net", "").startswith("labdesk-"):
            kvm.delete_network(l["net"])

    # ------------------------------------------------------------- Internet cloud
    def cloud_nodes(self) -> list[dict]:
        """CML nodes of this lab that are the LabDesk Internet cloud (tagged System-Bridge connectors)."""
        nodes = self._snap.nodes if self._snap is not None and self._snap_lab == self.lab_id else \
            self.client.nodes(self._lab())
        return [n for n in nodes if cloud.is_cloud_node(n)]

    def cloud_link_of(self, router_key: str) -> tuple[dict, str] | None:
        """(cloud node, router interface label) when ``router_key`` is cabled to an Internet cloud."""
        lab_id = self._lab()
        rid = router_key.split(":", 1)[1]
        clouds = {n["id"]: n for n in self.client.nodes(lab_id) if cloud.is_cloud_node(n)}
        if not clouds:
            return None
        ifaces = {i["id"]: i for i in self.client.get(f"/labs/{self.client._q(lab_id)}/interfaces?data=true") or []}
        for l in self.client.get(f"/labs/{self.client._q(lab_id)}/links?data=true") or []:
            ia, ib = ifaces.get(l.get("interface_a"), {}), ifaces.get(l.get("interface_b"), {})
            na, nb = l.get("node_a") or ia.get("node"), l.get("node_b") or ib.get("node")
            if na == rid and nb in clouds:
                self._cloud_iface_id = ia.get("id")
                return clouds[nb], ia.get("label", "")
            if nb == rid and na in clouds:
                self._cloud_iface_id = ib.get("id")
                return clouds[na], ib.get("label", "")
        return None

    def _cloud_used_ips(self, conf: cloud.CloudConf) -> set[str]:
        """Cloud-subnet addresses already used by routers: day-0 configs of every lab, LabDesk's pending
        day-0 patches and the live interface facts of this lab."""
        used: set[str] = set()
        for lab_id in self.client.get("/labs") or []:
            try:
                nodes = self.client.nodes(lab_id)
            except CMLError:
                continue
            for n in nodes:
                cfg = n.get("configuration")
                if isinstance(cfg, list):
                    cfg = " ".join(str(c.get("content", "")) for c in cfg if isinstance(c, dict))
                used |= cloud.ips_in_config(str(cfg or ""), conf.network)
        for lab in self.state.data.get("labs", {}).values():
            for p in (lab.get("day0_pending") or {}).values():
                if p.get("ip"):
                    used.add(p["ip"])
        if self.telemetry is not None:
            for nf in list(self.telemetry.facts.values()):
                if nf.ifaces is None:
                    continue
                for f in nf.ifaces.value.values():
                    if f.ip and cloud._is_ip(f.ip) and cloud.ipaddress.ip_address(f.ip) in conf.network:
                        used.add(f.ip)
        return used

    def cloud_plan(self, router_key: str, conf: cloud.CloudConf | None = None) -> "CloudPlan":
        """Everything the 'Auto-configure for Internet cloud' dialog shows (read-only, no side effects)."""
        conf = conf or cloud.load_conf()
        if not router_key.startswith("cml:"):
            raise CMLError("auto-configure works for CML IOS routers")
        lab_id = self._lab()
        node = self.client.node(lab_id, router_key[4:])
        if node.get("node_definition") not in ("iosv", "iol-xe", "csr1000v", "cat8000v", "iosvl2", "ioll2-xe"):
            raise CMLError(f"{node.get('label')}: auto-configure supports Cisco IOS / IOS-XE nodes")
        found = self.cloud_link_of(router_key)
        if found is None:
            raise CMLError(f"{node.get('label')} is not cabled to an Internet cloud: drop the cloud from "
                           "Connectors and Shift-drag a link from the router to it first")
        cnode, iface = found
        day0 = self.client.node_configuration(lab_id, node["id"])
        nf = self.telemetry.facts.get(router_key) if self.telemetry is not None else None
        ip = None
        if nf is not None and nf.ifaces is not None:
            from .facts import iface_key
            f = nf.ifaces.value.get(iface_key(iface))
            if f is not None and f.ip and cloud.ipaddress.ip_address(f.ip) in conf.network:
                ip = f.ip
        if ip is None:
            blk = next((b for b in cloud._blocks(day0) if b[0].strip().lower() == f"interface {iface}".lower()), [])
            ips = cloud.ips_in_config("\n".join(blk), conf.network)
            ip = sorted(ips)[0] if ips else None
        pend = (self.state.lab(lab_id).get("day0_pending") or {}).get(node["id"])
        if ip is None and pend and pend.get("iface") == iface:
            ip = pend.get("ip")
        used = self._cloud_used_ips(conf)
        reused = ip is not None
        if ip is None:
            ip = cloud.next_free_ip(used, conf)
        live_cfg = nf.config.value.text if nf is not None and nf.config is not None else ""
        ospf = sorted(set(cloud.ospf_processes(day0)) | set(cloud.ospf_processes(live_cfg or "")))
        lans = []
        if nf is not None and nf.ifaces is not None:
            lans = cloud.cloud_iface_lans(nf.ifaces.value.values())
        if not lans:
            import ipaddress as _ip
            for a, m in cloud._ADDR_RE.findall(day0):
                try:
                    lans.append(str(_ip.ip_interface(f"{a}/{m}").network))
                except ValueError:
                    pass
        lans = [x for x in dict.fromkeys(lans) if not cloud.ipaddress.ip_network(x).overlaps(conf.network)]
        routes = cloud.plan_routes(cloud.read_routes_file(), ip, lans, conf, live_edges=used | {ip})
        state = node.get("state", "")
        nt_open = bool(self.telemetry is not None and router_key in self.telemetry.console_open)
        needs_wipe = False
        if state in ACTIVE_STATES:
            # CML wires a cable into a running VM only if the node was (re)created with it: a link added
            # after the node's first boot leaves the interface STOPPED until the node is wiped
            try:
                ist = (self.client.lab_element_state(lab_id).get("interfaces") or {}).get(
                    getattr(self, "_cloud_iface_id", None))
                needs_wipe = ist is not None and ist != "STARTED"
            except CMLError:
                pass
        return CloudPlan(router_key, node.get("label", "?"), f"cml:{cnode['id']}", cnode.get("label", "?"), iface,
                         ip, reused, conf, ospf, cloud.live_commands(iface, ip, conf), state, state == "BOOTED",
                         nt_open, state == "DEFINED_ON_CORE", cloud.day0_has_cloud(day0, iface, ip, conf), routes,
                         lans, cloud.ROUTES_PATH.parent.is_dir() and os.access(cloud.ROUTES_PATH.parent, os.W_OK),
                         needs_wipe)

    def cloud_apply(self, plan: "CloudPlan", live: bool = True, day0: bool = True, ospf: bool = False,
                    routes: bool = True, restart: bool = False) -> list[tuple[bool, str]]:
        """Apply a CloudPlan.  Returns [(ok, text)] per step (never raises for a single failed step).
        ``restart``: wipe + start the router afterwards (the stored day-0 patch is written into CML by
        wipe(), so the router boots with the uplink and CML wires the new cable in)."""
        if restart:
            day0 = True
        out: list[tuple[bool, str]] = []
        lab_id = self._lab()
        nid = plan.router_key[4:]
        pids = plan.ospf if ospf else []
        if live:
            if self.telemetry is None or not plan.booted:
                out.append((False, f"console: {plan.router_label} is not running - only the day-0 config is updated"))
            else:
                r = self.telemetry.configure(plan.router_key, cloud.live_commands(plan.iface, plan.ip, plan.conf, pids),
                                             save=True, enable_secret=cloud.LAB_ENABLE_SECRET)
                out.append((r.ok, f"console: {r.text}"))
        if day0:
            try:
                cfg = self.client.node_configuration(lab_id, nid)
                new = cloud.patch_day0(cfg, plan.iface, plan.ip, plan.conf, ospf=bool(pids))
                if new == cfg:
                    out.append((True, "day-0 config already has it"))
                elif self.client.node_state(lab_id, nid) == "DEFINED_ON_CORE":
                    self.set_config(plan.router_key, new)
                    out.append((True, "day-0 config updated in CML"))
                else:
                    lab = self.state.lab(lab_id)
                    lab.setdefault("day0_pending", {})[nid] = {"kind": "cloud", "iface": plan.iface, "ip": plan.ip,
                                                               "ospf": bool(pids), "label": plan.router_label}
                    self.state.save()
                    out.append((True, f"day-0 config: CML locks it until {plan.router_label} is wiped - LabDesk "
                                      "stores it and writes it into CML on the next wipe (running config was saved "
                                      "with write memory, so a stop/start keeps it too)"))
            except CMLError as e:
                out.append((False, f"day-0 config: {e}"))
        if routes:
            text = cloud.render_routes(plan.routes.edge, plan.routes.routes)
            try:
                if plan.routes.changed or cloud.read_routes_file() != text:
                    cloud.write_routes_file(text)
                out.append((True, f"return path: {plan.routes.text}"))
            except OSError:
                out.append((False, "return path: cannot write " + str(cloud.ROUTES_PATH) + " - run: "
                                   + cloud.manual_routes_command(text)))
        if restart:
            try:
                self.wipe(plan.router_key)
                cfg = self.client.node_configuration(lab_id, nid)
                has = cloud.day0_has_cloud(cfg, plan.iface, plan.ip, plan.conf)
                self.start(plan.router_key)
                out.append((has, f"wiped and restarted {plan.router_label} (day-0 config "
                                 + ("now has the uplink" if has else "was NOT updated") + "); it boots in 2-3 min"))
            except CMLError as e:
                out.append((False, f"wipe & restart: {e}"))
        return out

    # ------------------------------------------------------------ EtherChannel
    def _ec_link_ends(self, link_keys: list[str]) -> tuple[dict, dict, list[tuple[str, str]]]:
        """(node a, node b, [(iface a, iface b)]) of parallel CML links between two nodes (full IOS names)."""
        lab_id = self._lab()
        snap = self.client.snapshot(lab_id, with_total=False)
        if_by_id = {i["id"]: i for i in snap.interfaces}
        nodes = {n["id"]: n for n in snap.nodes}
        pairs, na, nb = [], None, None
        for lk in link_keys:
            if not lk.startswith("cml:"):
                raise CMLError("EtherChannel needs CML links between two switches")
            l = next((x for x in snap.links if x["id"] == lk[4:]), None)
            if l is None:
                raise CMLError(f"link {lk} not found")
            ia, ib = if_by_id.get(l.get("interface_a"), {}), if_by_id.get(l.get("interface_b"), {})
            a, b = nodes.get(ia.get("node")), nodes.get(ib.get("node"))
            if a is None or b is None:
                raise CMLError("link endpoints not found")
            if na is None:
                na, nb = a, b
            if {a["id"], b["id"]} != {na["id"], nb["id"]}:
                raise CMLError("the selected links do not connect the same two devices")
            pairs.append((ia.get("label", ""), ib.get("label", "")) if a["id"] == na["id"] else
                         (ib.get("label", ""), ia.get("label", "")))
        return na, nb, pairs

    def _ec_l2_lines(self, node: dict, ifaces: list[str]) -> tuple[list[str], str]:
        """Switchport lines to copy onto the Port-channel + where they came from (plain words).  Order:
        the running-config (when the probe has it), the CML day-0 config if it agrees with the live mode,
        then the live trunk / access state from telemetry."""
        from . import etherchannel as EC
        from .parsers.common import iface_key
        nk = f"cml:{node['id']}"
        nf = self.telemetry.facts.get(nk) if self.telemetry is not None else None
        first = ifaces[0]
        live_mode, rebuilt = None, []
        if nf is not None and nf.l2 is not None:
            ecg = nf.ec.group_of(first) if nf.ec is not None else None
            key = iface_key(ecg[0]["port_channel"]) if ecg else iface_key(first)
            tr = nf.l2.trunks.value.get(key) if nf.l2.trunks else None
            sp = nf.l2.ports.value.get(iface_key(first)) if nf.l2.ports else None
            if tr is not None:
                live_mode = "trunk"
                rebuilt = EC.l2_lines_from_facts("trunk", allowed=tr.allowed_text, native=tr.native)
            elif sp is not None and isinstance(sp.vlan, int):
                live_mode = "access"
                rebuilt = EC.l2_lines_from_facts("access", access_vlan=sp.vlan)
            elif sp is not None and sp.vlan == "trunk":
                live_mode = "trunk"
        if nf is not None and nf.config is not None:
            lines = EC.l2_lines(EC.iface_block(nf.config.value.text, first))
            if lines:
                return lines, f"running-config of {node['label']} {EC.short_ifname(first)}"
        try:
            day0 = self.client.node_configuration(self._lab(), node["id"])
        except CMLError:
            day0 = ""
        lines = EC.l2_lines(EC.iface_block(day0, first))
        mode = "trunk" if any("switchport mode trunk" in x for x in lines) else ("access" if lines else None)
        if lines and (live_mode is None or mode == live_mode):
            return lines, f"day-0 config of {node['label']} {EC.short_ifname(first)}"
        if rebuilt:
            return rebuilt, f"live state of {node['label']} {EC.short_ifname(first)} (show interfaces trunk/status)"
        return lines, f"day-0 config of {node['label']} {EC.short_ifname(first)}" if lines else "nothing (access VLAN 1)"

    def bundle_plan(self, link_keys: list[str], protocol: str = "lacp") -> "object":
        """'Bundle into EtherChannel…': a BundlePlan (etherchannel.py) for 2+ parallel cables between two IOS
        switches (read-only, no side effects).  Defaults: LACP active/active, the next free Port-channel number
        on both switches, the members' current switchport config copied onto the Port-channel."""
        from . import etherchannel as EC
        from .parsers.common import iface_key
        if len(link_keys) < 2:
            raise CMLError("select two or more parallel links between the same two switches (Ctrl-click)")
        na, nb, pairs = self._ec_link_ends(link_keys)
        ok_defs = ("iosvl2", "ioll2-xe")
        for n in (na, nb):
            if n.get("node_definition") not in ok_defs:
                raise CMLError(f"{n.get('label')}: EtherChannel from LabDesk supports IOSvL2 switches "
                               f"(it is {n.get('node_definition')})")
        used: set[int] = set()
        for n, ifs in ((na, [p[0] for p in pairs]), (nb, [p[1] for p in pairs])):
            nf = self.telemetry.facts.get(f"cml:{n['id']}") if self.telemetry is not None else None
            if nf is not None and nf.ec is not None:
                for g in nf.ec.groups():
                    used.add(g["group"])
                    for pt in g["ports"]:
                        if any(iface_key(pt["port"]) == iface_key(x) for x in ifs):
                            raise CMLError(f"{n['label']} {pt['port']} is already in {g['port_channel']}: "
                                           "unbundle it first")
            if nf is not None and nf.ifaces is not None:
                used |= {int(k[len('port-channel'):]) for k in nf.ifaces.value if k.startswith("port-channel")
                         and k[len('port-channel'):].isdigit()}
            try:
                used |= EC.used_po_numbers(self.client.node_configuration(self._lab(), n["id"]))
            except CMLError:
                pass
        po = EC.next_free_po(used)
        l2a, src_a = self._ec_l2_lines(na, [p[0] for p in pairs])
        l2b, src_b = self._ec_l2_lines(nb, [p[1] for p in pairs])
        ma, mb = EC.DEFAULT_MODES[protocol]
        opened = self.telemetry.console_open if self.telemetry is not None else set()

        def is_open(n):
            u = self.telemetry.console_uuid.get(f"cml:{n['id']}") if self.telemetry is not None else None
            return bool(u and u in opened)

        a = EC.BundleSide(f"cml:{na['id']}", na["label"], [p[0] for p in pairs], ma, l2a, nb["label"],
                          [p[1] for p in pairs], is_open(na), na.get("state") == "BOOTED")
        b = EC.BundleSide(f"cml:{nb['id']}", nb["label"], [p[1] for p in pairs], mb, l2b, na["label"],
                          [p[0] for p in pairs], is_open(nb), nb.get("state") == "BOOTED")
        plan = EC.BundlePlan(po, protocol, a, b, None, list(link_keys), EC.check_modes(ma, mb))
        plan.used = sorted(used)
        plan.l2_source = (src_a, src_b)
        if EC.l2_summary(l2a) != EC.l2_summary(l2b):
            plan.l2_conflict = (f"{na['label']}: {EC.l2_summary(l2a)} but {nb['label']}: {EC.l2_summary(l2b)} - the "
                                "two Port-channels will differ (fix the members first for a working trunk)")
        return plan

    def bundle_apply(self, plan, live: bool = True, day0: bool = True) -> list[tuple[bool, str]]:
        """Apply a BundlePlan: type the commands on both switches through the console path (telemetry.configure:
        per-node lock, console etiquette, write memory), and store the bundle in the CML day-0 configs (written now
        when the node is wiped, else kept and written at the next wipe).  Returns [(ok, text)] per step."""
        from . import etherchannel as EC
        out: list[tuple[bool, str]] = []
        errs = EC.validate_plan(plan)
        if errs:
            return [(False, "; ".join(errs))]
        for side_name, side in (("a", plan.a), ("b", plan.b)):
            if not live:
                break
            if self.telemetry is None or not side.booted:
                out.append((False, f"{side.label}: not running - only the day-0 config is updated"))
                continue
            r = self.telemetry.configure(side.node_key, plan.commands(side_name), save=True,
                                         enable_secret=cloud.LAB_ENABLE_SECRET)
            out.append((r.ok, f"{side.label}: {r.text}"))
            self._ec_kick(side.node_key)
        if day0:
            for side_name, side in (("a", plan.a), ("b", plan.b)):
                op = {"op": "bundle", "po": plan.po, "members": list(side.members), "mode": side.mode,
                      "l2": list(side.l2), "desc": plan.description(side_name), "lb": plan.load_balance,
                      "label": side.label}
                out.append(self._ec_day0(side.node_key, side.label, op))
        return out

    def unbundle_plan(self, bundle_key: str) -> dict:
        """Everything the Unbundle confirmation shows: per side {node_key, label, po (int), members, l2, commands,
        console_open, booted}."""
        from . import etherchannel as EC
        tv = self.telemetry.view() if self.telemetry is not None else None
        bd = tv.bundles.get(bundle_key) if tv is not None else None
        if bd is None:
            raise CMLError("that EtherChannel is gone (probe again)")
        sides = []
        for side in (bd.side_a, bd.side_b):
            if side is None or side.group is None:
                continue
            node = self.client.node(self._lab(), side.node[4:])
            members = [EC.full_ifname(m.port) for m in side.members]
            l2, _src = self._ec_l2_lines(node, [side.po] + members)
            u = self.telemetry.console_uuid.get(side.node)
            sides.append({"node_key": side.node, "label": side.label, "po": side.group, "members": members, "l2": l2,
                          "commands": EC.unbundle_commands(side.group, members, l2),
                          "console_open": bool(u and u in self.telemetry.console_open),
                          "booted": node.get("state") == "BOOTED"})
        return {"key": bundle_key, "label": bd.po_text, "sides": sides, "link_keys": list(bd.link_keys)}

    def unbundle_apply(self, plan: dict, live: bool = True, day0: bool = True) -> list[tuple[bool, str]]:
        out: list[tuple[bool, str]] = []
        for side in plan["sides"]:
            if live:
                if self.telemetry is None or not side["booted"]:
                    out.append((False, f"{side['label']}: not running - only the day-0 config is updated"))
                else:
                    r = self.telemetry.configure(side["node_key"], side["commands"], save=True,
                                                 enable_secret=cloud.LAB_ENABLE_SECRET)
                    out.append((r.ok, f"{side['label']}: {r.text}"))
                    self._ec_kick(side["node_key"])
            if day0:
                out.append(self._ec_day0(side["node_key"], side["label"],
                                         {"op": "unbundle", "po": side["po"], "l2": side["l2"],
                                          "label": side["label"]}))
        return out

    def _ec_kick(self, node_key: str) -> None:
        """Re-read the EtherChannel tables of a switch right after a change (FAST + SLOW)."""
        eng = self.telemetry
        if eng is None:
            return
        with eng._lock:
            s = eng.sched.get(node_key)
            if s is not None:
                s.next_fast = s.next_slow = 0.0
                s.failures = 0

    @staticmethod
    def _ec_patch(cfg: str, op: dict) -> str:
        from . import etherchannel as EC
        if op["op"] == "bundle":
            return EC.patch_day0(cfg, op["po"], op["members"], op["mode"], op["l2"], op.get("desc", ""),
                                 op.get("lb"))
        return EC.unpatch_day0(cfg, op["po"], op.get("l2"))

    def _ec_day0(self, node_key: str, label: str, op: dict) -> tuple[bool, str]:
        lab_id = self._lab()
        nid = node_key[4:]
        try:
            cfg = self.client.node_configuration(lab_id, nid)
            new = self._ec_patch(cfg, op)
            if new == cfg:
                return True, f"{label}: day-0 config already has it"
            if self.client.node_state(lab_id, nid) == "DEFINED_ON_CORE":
                self.set_config(node_key, new)
                return True, f"{label}: day-0 config updated in CML"
            lab = self.state.lab(lab_id)
            lab.setdefault("day0_pending_ec", {}).setdefault(nid, []).append(op)
            self.state.save()
            return True, (f"{label}: day-0 config - CML locks it while the node runs; LabDesk stores the change and "
                          "writes it into CML at the next wipe (running config was saved with write memory)")
        except CMLError as e:
            return False, f"{label}: day-0 config: {e}"

    def _apply_pending_day0(self, node_ids: list[str]) -> None:
        """Write stored day-0 patches (Internet cloud uplink, EtherChannel bundle / unbundle) into CML once a
        node is wiped."""
        lab_id = self._lab()
        lab = self.state.lab(lab_id)
        pend_ec = lab.get("day0_pending_ec") or {}
        for nid in [i for i in node_ids if i in pend_ec]:
            try:
                end = time.time() + 30
                while self.client.node_state(lab_id, nid) != "DEFINED_ON_CORE" and time.time() < end:
                    time.sleep(1.0)
                cfg = self.client.node_configuration(lab_id, nid)
                new = cfg
                for op in pend_ec[nid]:
                    new = self._ec_patch(new, op)
                if new != cfg:
                    self.client.update_node(lab_id, nid, configuration=new)
                pend_ec.pop(nid, None)
            except CMLError:
                continue
        if "day0_pending_ec" in lab:
            lab["day0_pending_ec"] = pend_ec
            self.state.save()
        pend = lab.get("day0_pending") or {}
        todo = [i for i in node_ids if i in pend]
        if not todo:
            return
        conf = cloud.load_conf()
        for nid in todo:
            p = pend[nid]
            try:
                end = time.time() + 30
                while self.client.node_state(lab_id, nid) != "DEFINED_ON_CORE" and time.time() < end:
                    time.sleep(1.0)
                cfg = self.client.node_configuration(lab_id, nid)
                new = cloud.patch_day0(cfg, p["iface"], p["ip"], conf, ospf=bool(p.get("ospf")))
                if new != cfg:
                    self.client.update_node(lab_id, nid, configuration=new)
                pend.pop(nid, None)
            except CMLError:
                continue
        lab["day0_pending"] = pend
        self.state.save()

    # ------------------------------------------------------------- consoles
    def console_command(self, key: str) -> launchers.ConsoleCommand:
        kind, ident = key.split(":", 1)
        if kind == "kvm":
            return launchers.kvm_console(ident)
        lab_id = self._lab()
        n = self.client.node(lab_id, ident)
        if n.get("node_definition") in NON_COUNTING_DEFS:
            raise CMLError(f"{n.get('label')} has no console")
        if n.get("state") not in ACTIVE_STATES:
            raise CMLError(f"{n.get('label')} is not running - start it first")
        uuid = self.client.console_key(lab_id, ident)
        user = config.load_env_file(self.client._cred_file).get("CML_ADMIN_USER", "admin")
        return launchers.cml_console(n.get("label", ident), uuid, user)

    def mgmt(self, key: str) -> dict:
        m = dict(self.state.lab(self._lab())["mgmt"].get(key, {}))
        if not m.get("ip") and key.startswith("kvm:"):
            ip = kvm.mgmt_ip(key[4:])
            if ip:
                m["ip"] = ip
                m.setdefault("auto", True)
        return m

    def set_mgmt(self, key: str, ip: str, user: str) -> None:
        lab = self.state.lab(self._lab())
        if not ip:
            lab["mgmt"].pop(key, None)
        else:
            lab["mgmt"][key] = {"ip": validate.host(ip), "user": validate.username(user or "admin")}
        self.state.save()

    def _lab(self) -> str:
        if not self.lab_id:
            raise CMLError("no lab open")
        return self.lab_id


for _name in ("open_lab", "new_lab", "import_yaml", "delete_lab", "start_all", "stop_all", "wipe_all", "add_device",
              "move", "rename", "delete", "start", "stop", "wipe", "wipe_and_restart", "set_config", "add_link",
              "delete_link", "cloud_apply", "bundle_apply", "unbundle_apply"):
    setattr(LabController, _name, _invalidating(getattr(LabController, _name)))
del _name


def _domiflist_macs(vm: str) -> list[str]:
    try:
        out = kvm.run(kvm.virsh("domiflist", vm), timeout=10)
    except kvm.KvmError:
        return []
    return re.findall(r"([0-9a-f]{2}(?::[0-9a-f]{2}){5})", out)
