"""CML 2.x REST API wrapper (stdlib only, thread safe).

* Bearer-token auth with transparent re-authentication on HTTP 401.
* Self-signed controller certificate -> unverified TLS context.
* The HTTP layer is a pluggable ``transport`` so the logic can be unit-tested with
  a fake server (see tests/test_cml_api.py).
* Credentials come from the env file and never appear in exceptions or logs.
"""
from __future__ import annotations

import json
import ssl
import threading
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Callable

from . import config, validate
from .catalog import NON_COUNTING_DEFS

Transport = Callable[[str, str, bytes | None, dict, float], tuple[int, bytes]]

ACTIVE_STATES = {"STARTED", "QUEUED", "BOOTED"}


class CMLError(Exception):
    def __init__(self, msg: str, status: int | None = None):
        super().__init__(msg)
        self.status = status


class CMLUnreachable(CMLError):
    pass


class NodeLockedError(CMLError):
    """The node's physical configuration is locked (it has a disk) - wipe it first."""

    def __init__(self, node_label: str, detail: str = ""):
        super().__init__(f"{node_label}: interfaces are locked until the node's disk is wiped"
                         + (f" ({detail})" if detail else ""), status=None)
        self.node_label = node_label


class NoFreeInterface(CMLError):
    pass


def _urllib_transport() -> Transport:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    def send(method: str, url: str, body: bytes | None, headers: dict, timeout: float):
        req = urllib.request.Request(url, data=body, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, context=ctx, timeout=timeout) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as e:
            try:
                data = e.read()
            except Exception:
                data = b""
            return e.code, data
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            raise CMLUnreachable(f"{method} {urllib.parse.urlsplit(url).path}: "
                                 f"controller unreachable ({getattr(e, 'reason', e)})") from None

    return send


@dataclass
class Snapshot:
    """One poll of a lab: everything the canvas needs."""
    lab: dict
    nodes: list[dict]
    links: list[dict]
    interfaces: list[dict]
    running_total: int   # running CML nodes counting against the licence (all labs)


def short_ifname(name: str) -> str:
    """GigabitEthernet0/1 -> Gi0/1, Ethernet0/0 -> E0/0, port3 -> p3."""
    if not name:
        return ""
    table = [("GigabitEthernet", "Gi"), ("TenGigabitEthernet", "Te"), ("FastEthernet", "Fa"),
             ("Ethernet", "E"), ("Loopback", "Lo"), ("port", "p")]
    for long, short in table:
        if name.startswith(long):
            return short + name[len(long):]
    return name


class CMLClient:
    def __init__(self, host: str = config.CML_HOST, cred_file=config.CRED_FILE,
                 transport: Transport | None = None, timeout: float = 10.0):
        self.base = f"https://{host}/api/v0"
        self.host = host
        self.timeout = timeout
        self._cred_file = cred_file
        self._send = transport or _urllib_transport()
        self._token: str | None = None
        self._auth_lock = threading.Lock()
        self._nodedefs: dict[str, dict] | None = None

    # ------------------------------------------------------------------ core
    def _raw(self, method: str, path: str, body=None, auth=True, timeout=None, raw_body: bytes | None = None,
             content_type="application/json"):
        headers = {"Accept": "application/json"}
        data = raw_body
        if body is not None:
            data = json.dumps(body).encode()
        if data is not None:
            headers["Content-Type"] = content_type
        if auth and self._token:
            headers["Authorization"] = "Bearer " + self._token
        status, payload = self._send(method, self.base + path, data, headers, timeout or self.timeout)
        if status >= 400:
            detail = ""
            try:
                j = json.loads(payload or b"null")
                detail = j.get("description") if isinstance(j, dict) else str(j)
            except ValueError:
                detail = (payload or b"").decode("utf-8", "replace")[:200]
            raise CMLError(f"{method} {path.split('?')[0]} -> HTTP {status}: {detail}".rstrip(": "), status)
        if not payload:
            return None
        try:
            return json.loads(payload)
        except ValueError:
            return payload.decode("utf-8", "replace")

    def authenticate(self) -> None:
        creds = config.load_env_file(self._cred_file)
        user, pw = creds.get("CML_ADMIN_USER"), creds.get("CML_ADMIN_PASS")
        if not user or not pw:
            raise CMLError("CML_ADMIN_USER / CML_ADMIN_PASS missing in the credentials file")
        try:
            tok = self._raw("POST", "/authenticate", {"username": user, "password": pw}, auth=False)
        except CMLError as e:
            if e.status in (401, 403):
                raise CMLError("CML authentication failed (check the credentials file)", e.status) from None
            raise
        if not isinstance(tok, str) or not tok:
            raise CMLError("CML authentication returned no token")
        self._token = tok

    def request(self, method: str, path: str, body=None, **kw):
        with self._auth_lock:
            if not self._token:
                self.authenticate()
        try:
            return self._raw(method, path, body, **kw)
        except CMLError as e:
            if e.status != 401:
                raise
        with self._auth_lock:
            self.authenticate()
        return self._raw(method, path, body, **kw)

    def get(self, path, **kw):
        return self.request("GET", path, **kw)

    def post(self, path, body=None, **kw):
        return self.request("POST", path, body, **kw)

    def put(self, path, body=None, **kw):
        return self.request("PUT", path, body, **kw)

    def patch(self, path, body=None, **kw):
        return self.request("PATCH", path, body, **kw)

    def delete(self, path, **kw):
        return self.request("DELETE", path, **kw)

    @staticmethod
    def _q(value: str) -> str:
        return urllib.parse.quote(str(value), safe="")

    # ---------------------------------------------------------------- system
    def system_information(self) -> dict:
        info = self._raw("GET", "/system_information", auth=False, timeout=4)
        return info if isinstance(info, dict) else {}

    def is_ready(self) -> bool:
        try:
            return bool(self.system_information().get("ready"))
        except CMLError:
            return False

    def running_node_count(self) -> int:
        """Running nodes that count against the CML-Free limit (all labs)."""
        total = 0
        for lab_id in self.get("/labs") or []:
            for n in self.get(f"/labs/{self._q(lab_id)}/nodes?data=true&exclude_configurations=true") or []:
                if n.get("node_definition") in NON_COUNTING_DEFS:
                    continue
                if n.get("state") in ACTIVE_STATES:
                    total += 1
        return total

    def node_definitions(self) -> dict[str, dict]:
        if self._nodedefs is None:
            self._nodedefs = {d["id"]: d for d in (self.get("/node_definitions") or []) if "id" in d}
        return self._nodedefs

    # ------------------------------------------------------------------ labs
    def labs(self) -> list[dict]:
        out = []
        for lab_id in self.get("/labs") or []:
            try:
                lab = self.get(f"/labs/{self._q(lab_id)}")
            except CMLError:
                continue
            out.append(lab)
        return out

    def find_lab(self, title: str) -> dict | None:
        for lab in self.labs():
            if lab.get("lab_title") == title or lab.get("title") == title:
                return lab
        return None

    def lab(self, lab_id: str) -> dict:
        return self.get(f"/labs/{self._q(lab_id)}")

    def create_lab(self, title: str) -> dict:
        return self.post("/labs", {"title": validate.lab_title(title)})

    def import_topology(self, topology: dict, title: str | None = None) -> str:
        path = "/import"
        if title:
            path += "?title=" + self._q(validate.lab_title(title))
        res = self.post(path, topology, timeout=60)
        lab_id = res.get("id") if isinstance(res, dict) else None
        if not lab_id:
            raise CMLError("import returned no lab id")
        return lab_id

    def download(self, lab_id: str) -> str:
        res = self.get(f"/labs/{self._q(lab_id)}/download")
        return res if isinstance(res, str) else json.dumps(res)

    def start_lab(self, lab_id: str):
        return self.put(f"/labs/{self._q(lab_id)}/start", timeout=60)

    def stop_lab(self, lab_id: str):
        return self.put(f"/labs/{self._q(lab_id)}/stop", timeout=120)

    def wipe_lab(self, lab_id: str):
        return self.put(f"/labs/{self._q(lab_id)}/wipe", timeout=120)

    def delete_lab(self, lab_id: str):
        return self.delete(f"/labs/{self._q(lab_id)}", timeout=60)

    def snapshot(self, lab_id: str, with_total: bool = True) -> Snapshot:
        q = self._q(lab_id)
        lab = self.get(f"/labs/{q}")
        nodes = self.get(f"/labs/{q}/nodes?data=true") or []
        links = self.get(f"/labs/{q}/links?data=true") or []
        ifaces = self.get(f"/labs/{q}/interfaces?data=true") or []
        total = self.running_node_count() if with_total else sum(
            1 for n in nodes if n.get("node_definition") not in NON_COUNTING_DEFS
            and n.get("state") in ACTIVE_STATES)
        return Snapshot(lab, nodes, links, ifaces, total)

    # ------------------------------------------------------------ telemetry (D7/D8)
    def lab_element_state(self, lab_id: str) -> dict:
        """{nodes: {id: state}, links: {id: state}, interfaces: {id: state}} in one call."""
        res = self.get(f"/labs/{self._q(lab_id)}/lab_element_state")
        return res if isinstance(res, dict) else {}

    def simulation_stats(self, lab_id: str) -> dict:
        """{links: {id: {readbytes, readpackets, writebytes, writepackets, drops}}, nodes: {...}}"""
        res = self.get(f"/labs/{self._q(lab_id)}/simulation_stats")
        return res if isinstance(res, dict) else {}

    def console_log(self, lab_id: str, node_id: str, console: int = 0, lines: int | None = 200) -> str:
        """CML's server-side transcript of a node's serial console ('|<ms>|<line>' per line).  A pure
        read: it never touches an open console session (used by the console tap, ~17 ms)."""
        q = f"?lines={int(lines)}" if lines else ""
        res = self.get(f"/labs/{self._q(lab_id)}/nodes/{self._q(node_id)}/consoles/{int(console)}/log{q}")
        return res if isinstance(res, str) else ""

    def layer3_addresses(self, lab_id: str) -> dict:
        """{node_id: {name, interfaces: {mac: {id, label, ip4: [...], ip6: [...]}}}}.  On 2.10.1 CML only
        fills this for nodes on an L2 (bridge) external connector that got their address by DHCP."""
        res = self.get(f"/labs/{self._q(lab_id)}/layer3_addresses")
        return res if isinstance(res, dict) else {}

    def link_condition(self, lab_id: str, link_id: str) -> dict:
        res = self.get(f"/labs/{self._q(lab_id)}/links/{self._q(link_id)}/condition")
        return res if isinstance(res, dict) else {}

    def set_link_condition(self, lab_id: str, link_id: str, cfg: dict) -> dict:
        """PATCH LinkConditionConfiguration (validated by link_condition_body())."""
        res = self.patch(f"/labs/{self._q(lab_id)}/links/{self._q(link_id)}/condition", link_condition_body(cfg))
        return res if isinstance(res, dict) else {}

    def clear_link_condition(self, lab_id: str, link_id: str) -> None:
        self.delete(f"/labs/{self._q(lab_id)}/links/{self._q(link_id)}/condition")

    def start_link(self, lab_id: str, link_id: str):
        return self.put(f"/labs/{self._q(lab_id)}/links/{self._q(link_id)}/state/start", timeout=30)

    def stop_link(self, lab_id: str, link_id: str):
        return self.put(f"/labs/{self._q(lab_id)}/links/{self._q(link_id)}/state/stop", timeout=30)

    # ----------------------------------------------------------------- nodes
    def nodes(self, lab_id: str) -> list[dict]:
        return self.get(f"/labs/{self._q(lab_id)}/nodes?data=true") or []

    def node(self, lab_id: str, node_id: str) -> dict:
        return self.get(f"/labs/{self._q(lab_id)}/nodes/{self._q(node_id)}")

    def create_node(self, lab_id: str, label: str, node_definition: str, x: int, y: int,
                    configuration: str = "", tags: list[str] | tuple = ()) -> dict:
        body = {"label": validate.cml_label(label), "node_definition": node_definition,
                "x": int(x), "y": int(y)}
        if configuration:
            body["configuration"] = configuration
        if tags:
            body["tags"] = [validate.cml_label(t) for t in tags]
        res = self.post(f"/labs/{self._q(lab_id)}/nodes?populate_interfaces=true", body)
        node_id = res.get("id") if isinstance(res, dict) else None
        if not node_id:
            raise CMLError("node create returned no id")
        return self.node(lab_id, node_id)

    def update_node(self, lab_id: str, node_id: str, **fields):
        if "label" in fields:
            fields["label"] = validate.cml_label(fields["label"])
        for k in ("x", "y"):
            if k in fields:
                fields[k] = int(fields[k])
        return self.patch(f"/labs/{self._q(lab_id)}/nodes/{self._q(node_id)}", fields)

    def delete_node(self, lab_id: str, node_id: str):
        return self.delete(f"/labs/{self._q(lab_id)}/nodes/{self._q(node_id)}")

    def start_node(self, lab_id: str, node_id: str):
        return self.put(f"/labs/{self._q(lab_id)}/nodes/{self._q(node_id)}/state/start")

    def stop_node(self, lab_id: str, node_id: str):
        return self.put(f"/labs/{self._q(lab_id)}/nodes/{self._q(node_id)}/state/stop", timeout=60)

    def wipe_node(self, lab_id: str, node_id: str):
        return self.put(f"/labs/{self._q(lab_id)}/nodes/{self._q(node_id)}/wipe_disks", timeout=60)

    def node_state(self, lab_id: str, node_id: str) -> str:
        res = self.get(f"/labs/{self._q(lab_id)}/nodes/{self._q(node_id)}/state")
        if isinstance(res, dict):
            return res.get("state", "")
        return str(res or "")

    def console_key(self, lab_id: str, node_id: str, line: int = 0) -> str:
        res = self.get(f"/labs/{self._q(lab_id)}/nodes/{self._q(node_id)}/keys/console?line={int(line)}")
        if not isinstance(res, str):
            raise CMLError("unexpected console key response")
        return validate.uuid(res)

    def node_configuration(self, lab_id: str, node_id: str) -> str:
        n = self.get(f"/labs/{self._q(lab_id)}/nodes/{self._q(node_id)}")
        cfg = n.get("configuration") if isinstance(n, dict) else ""
        if isinstance(cfg, list):        # multi-file form: [{name, content}]
            cfg = next((c.get("content", "") for c in cfg if isinstance(c, dict)), "")
        return cfg or ""

    # ------------------------------------------------------- interfaces/links
    def node_interfaces(self, lab_id: str, node_id: str) -> list[dict]:
        res = self.get(f"/labs/{self._q(lab_id)}/nodes/{self._q(node_id)}/interfaces?data=true") or []
        return [i for i in res if isinstance(i, dict)]

    def create_interface(self, lab_id: str, node_id: str, slot: int, node_label: str = "") -> dict:
        try:
            res = self.post(f"/labs/{self._q(lab_id)}/interfaces", {"node": node_id, "slot": int(slot)})
        except CMLError as e:
            if e.status in (400, 403, 409, 422) and _looks_locked(str(e)):
                raise NodeLockedError(node_label or node_id, str(e)) from None
            raise
        items = res if isinstance(res, list) else [res]
        for it in items:
            if isinstance(it, dict) and it.get("slot") == int(slot):
                return it
        if items and isinstance(items[-1], dict):
            return items[-1]
        raise CMLError("interface create returned nothing")

    def next_free_interface(self, lab_id: str, node: dict) -> dict:
        """First unconnected physical interface of ``node``; creates one if needed."""
        ifaces = [i for i in self.node_interfaces(lab_id, node["id"]) if i.get("type", "physical") == "physical"]
        free = sorted((i for i in ifaces if not i.get("is_connected")), key=lambda i: i.get("slot", 0))
        if free:
            return free[0]
        nd = self.node_definitions().get(node.get("node_definition", ""), {})
        physical = ((nd.get("device") or {}).get("interfaces") or {}).get("physical") or []
        used_slots = {i.get("slot") for i in ifaces}
        slot = 0
        while slot in used_slots:
            slot += 1
        if physical and slot >= len(physical):
            raise NoFreeInterface(f"{node.get('label')}: all {len(physical)} interfaces are in use")
        return self.create_interface(lab_id, node["id"], slot, node.get("label", ""))

    def create_link(self, lab_id: str, src_iface: str, dst_iface: str) -> dict:
        res = self.post(f"/labs/{self._q(lab_id)}/links", {"src_int": src_iface, "dst_int": dst_iface})
        return res if isinstance(res, dict) else {"id": res}

    def link_nodes(self, lab_id: str, node_a: dict, node_b: dict) -> tuple[dict, dict, dict]:
        """Connect two nodes using the next free interface on each side.

        Returns (link, iface_a, iface_b). Raises NodeLockedError when a new interface is
        needed on a node whose disk exists (caller offers a wipe)."""
        ia = self.next_free_interface(lab_id, node_a)
        ib = self.next_free_interface(lab_id, node_b)
        link = self.create_link(lab_id, ia["id"], ib["id"])
        return link, ia, ib

    def delete_link(self, lab_id: str, link_id: str):
        return self.delete(f"/labs/{self._q(lab_id)}/links/{self._q(link_id)}")


def _looks_locked(msg: str) -> bool:
    m = msg.lower()
    return any(w in m for w in ("lock", "wipe", "not in defined", "running", "booted", "state"))


def node_needs_wipe_for_new_link(node: dict) -> bool:
    """A node that has booted keeps a disk; links/interfaces added now may stay down
    until it is wiped and restarted."""
    return node.get("state") not in (None, "", "DEFINED_ON_CORE")


# LinkConditionConfiguration (CML 2.10 OpenAPI, additionalProperties: false)
LINK_CONDITION_LIMITS: dict[str, tuple[type, float, float]] = {
    "bandwidth": (int, 0, 10_000_000),      # kbps
    "latency": (int, 0, 10_000),            # ms
    "jitter": (int, 0, 10_000),             # ms
    "loss": (float, 0, 100),                # %
    "loss_corr": (float, 0, 100),
    "delay_corr": (float, 0, 100),
    "limit": (int, 0, 10_000),
    "gap": (int, 0, 10_000),
    "duplicate": (float, 0, 100),
    "duplicate_corr": (float, 0, 100),
    "reorder_prob": (float, 0, 100),
    "reorder_corr": (float, 0, 100),
    "corrupt_prob": (float, 0, 100),
    "corrupt_corr": (float, 0, 100),
}


def link_condition_body(cfg: dict) -> dict:
    """Validate/clamp a link-condition dict; unknown keys are rejected (the API would 400)."""
    body: dict = {}
    for k, v in (cfg or {}).items():
        if k == "enabled":
            body[k] = bool(v)
            continue
        if k not in LINK_CONDITION_LIMITS:
            raise CMLError(f"unknown link condition field '{k}'")
        if v is None:
            continue
        typ, lo, hi = LINK_CONDITION_LIMITS[k]
        try:
            num = typ(v)
        except (TypeError, ValueError):
            raise CMLError(f"link condition {k}: not a number") from None
        if not (lo <= num <= hi):
            raise CMLError(f"link condition {k}={v} outside {lo}..{hi}")
        body[k] = num
    body.setdefault("enabled", True)
    return body
