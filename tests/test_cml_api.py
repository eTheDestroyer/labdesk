import json
import re

import pytest

from labdesk_app.cml_api import CMLClient, CMLError, NodeLockedError, NoFreeInterface, short_ifname

SECRET = "s3cr3t-Pa55"


class FakeCML:
    """Minimal fake of the CML REST API, used as the client's transport."""

    def __init__(self):
        self.calls = []
        self.token_n = 0
        self.expire_next = False
        self.ifaces = {}       # node -> list of iface dicts
        self.locked = set()
        self.links = []
        self.nodes = {}

    def __call__(self, method, url, body, headers, timeout):
        path = url.split("/api/v0", 1)[1]
        data = json.loads(body) if body else None
        self.calls.append((method, path, data, headers.get("Authorization")))
        if path == "/authenticate":
            if data != {"username": "admin", "password": SECRET}:
                return 403, b'{"description": "bad creds"}'
            self.token_n += 1
            return 200, json.dumps(f"tok{self.token_n}").encode()
        if headers.get("Authorization") != f"Bearer tok{self.token_n}" or self.expire_next:
            self.expire_next = False
            return 401, b'{"description": "expired"}'
        m = re.fullmatch(r"/labs/L1/nodes/(\w+)/interfaces\?data=true", path)
        if m:
            return 200, json.dumps(self.ifaces.get(m.group(1), [])).encode()
        if path == "/labs/L1/interfaces" and method == "POST":
            node = data["node"]
            if node in self.locked:
                return 400, b'{"description": "Node is locked: wipe node disks first"}'
            existing = self.ifaces.setdefault(node, [])
            created = []
            for s in range(len(existing), data["slot"] + 1):
                it = {"id": f"{node}-i{s}", "slot": s, "label": f"GigabitEthernet0/{s}", "type": "physical",
                      "is_connected": False, "node": node}
                existing.append(it)
                created.append(it)
            return 200, json.dumps(created).encode()
        if path == "/labs/L1/links" and method == "POST":
            self.links.append(data)
            for lst in self.ifaces.values():
                for it in lst:
                    if it["id"] in (data["src_int"], data["dst_int"]):
                        it["is_connected"] = True
            return 200, json.dumps({"id": f"link{len(self.links)}"}).encode()
        if path == "/node_definitions":
            return 200, json.dumps([{"id": "iosv", "device": {"interfaces": {"physical": [
                f"GigabitEthernet0/{i}" for i in range(4)]}}}]).encode()
        if path.startswith("/labs/L1/nodes?populate_interfaces=true") and method == "POST":
            self.nodes["n9"] = dict(data, id="n9")
            return 200, b'{"id": "n9"}'
        if path == "/labs/L1/nodes/n9":
            return 200, json.dumps(self.nodes["n9"]).encode()
        if re.fullmatch(r"/labs/L1/nodes/\w+/keys/console\?line=0", path):
            return 200, b'"0f1e2d3c-4b5a-4968-8776-655443322110"'
        return 404, b'{"description": "not found"}'


@pytest.fixture
def env(tmp_path):
    p = tmp_path / "creds.env"
    p.write_text(f"# comment\nCML_ADMIN_USER=admin\nCML_ADMIN_PASS='{SECRET}'\n")
    return p


@pytest.fixture
def fake():
    return FakeCML()


@pytest.fixture
def client(env, fake):
    return CMLClient(host="cml.test", cred_file=env, transport=fake)


def node(nid, nd="iosv", label=None):
    return {"id": nid, "node_definition": nd, "label": label or nid.upper()}


def test_auth_and_reauth_on_401(client, fake):
    fake.ifaces["r1"] = []
    assert client.node_interfaces("L1", "r1") == []
    assert fake.token_n == 1
    fake.expire_next = True
    client.node_interfaces("L1", "r1")
    assert fake.token_n == 2                      # re-authenticated exactly once


def test_bad_credentials_message_has_no_secret(tmp_path, fake):
    p = tmp_path / "c.env"
    p.write_text("CML_ADMIN_USER=admin\nCML_ADMIN_PASS=wrong-one\n")
    c = CMLClient(host="x", cred_file=p, transport=fake)
    with pytest.raises(CMLError) as ei:
        c.node_interfaces("L1", "r1")
    assert "wrong-one" not in str(ei.value)
    assert "authentication failed" in str(ei.value)


def test_secret_never_in_errors(client, fake):
    with pytest.raises(CMLError) as ei:
        client.get("/does-not-exist")
    assert SECRET not in str(ei.value)
    assert ei.value.status == 404


def test_next_free_interface_prefers_lowest_unconnected(client, fake):
    fake.ifaces["r1"] = [
        {"id": "a", "slot": 0, "type": "physical", "is_connected": True, "label": "Gi0/0"},
        {"id": "lo", "slot": None, "type": "loopback", "is_connected": False, "label": "Loopback0"},
        {"id": "c", "slot": 2, "type": "physical", "is_connected": False, "label": "Gi0/2"},
        {"id": "b", "slot": 1, "type": "physical", "is_connected": False, "label": "Gi0/1"},
    ]
    assert client.next_free_interface("L1", node("r1"))["id"] == "b"


def test_next_free_interface_creates_and_picks_requested_slot(client, fake):
    fake.ifaces["r1"] = [{"id": "x0", "slot": 0, "type": "physical", "is_connected": True, "label": "Gi0/0"}]
    it = client.next_free_interface("L1", node("r1"))
    assert it["slot"] == 1
    post = [c for c in fake.calls if c[0] == "POST" and c[1] == "/labs/L1/interfaces"]
    assert post[-1][2] == {"node": "r1", "slot": 1}


def test_interface_create_returns_list_pick_matching_slot(client, fake):
    fake.ifaces["r1"] = []
    it = client.create_interface("L1", "r1", 3)      # fake returns slots 0..3
    assert it["slot"] == 3


def test_locked_node_raises_node_locked(client, fake):
    fake.ifaces["r1"] = [{"id": "x0", "slot": 0, "type": "physical", "is_connected": True, "label": "Gi0/0"}]
    fake.locked.add("r1")
    with pytest.raises(NodeLockedError) as ei:
        client.next_free_interface("L1", node("r1", label="R1"))
    assert ei.value.node_label == "R1"


def test_no_free_interface_when_all_slots_used(client, fake):
    fake.ifaces["r1"] = [{"id": f"x{i}", "slot": i, "type": "physical", "is_connected": True,
                          "label": f"Gi0/{i}"} for i in range(4)]
    with pytest.raises(NoFreeInterface):
        client.next_free_interface("L1", node("r1"))


def test_link_nodes_uses_free_interfaces(client, fake):
    fake.ifaces["r1"] = [{"id": "r1-i0", "slot": 0, "type": "physical", "is_connected": False, "label": "Gi0/0"}]
    fake.ifaces["r2"] = [{"id": "r2-i0", "slot": 0, "type": "physical", "is_connected": True, "label": "Gi0/0"}]
    link, ia, ib = client.link_nodes("L1", node("r1"), node("r2"))
    assert fake.links[-1] == {"src_int": "r1-i0", "dst_int": "r2-i1"}
    assert ib["slot"] == 1 and link["id"] == "link1"


def test_create_node_validates_label_and_sends_payload(client, fake):
    with pytest.raises(ValueError):
        client.create_node("L1", "bad label;rm", "iosv", 0, 0)
    n = client.create_node("L1", "R1", "iosv", 10.7, -5, "hostname R1")
    assert n["label"] == "R1"
    post = [c for c in fake.calls if c[0] == "POST" and "populate_interfaces" in c[1]][-1]
    assert post[2] == {"label": "R1", "node_definition": "iosv", "x": 10, "y": -5, "configuration": "hostname R1"}


def test_console_key_is_validated_uuid(client, fake):
    assert client.console_key("L1", "n1") == "0f1e2d3c-4b5a-4968-8776-655443322110"


def test_path_components_are_quoted(client, fake):
    with pytest.raises(CMLError):
        client.node_state("L1", "../../system")
    assert any("..%2F..%2Fsystem" in c[1] for c in fake.calls)


@pytest.mark.parametrize("long,short", [("GigabitEthernet0/1", "Gi0/1"), ("Ethernet0/0", "E0/0"),
                                        ("port3", "p3"), ("eth0", "eth0"), ("", "")])
def test_short_ifname(long, short):
    assert short_ifname(long) == short
