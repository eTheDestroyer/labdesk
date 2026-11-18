"""Internet cloud: address allocation, router config generation, return routes, health parsing,
catalog / topology marking and the console config step (no network, no root)."""
import ipaddress

import pytest

from fake_console import FakeConsole

from labdesk_app import catalog, cloud
from labdesk_app.probe.creds import DeviceCreds
from labdesk_app.probe.session import CONFIG_KEY, SAVE_KEY, BurstStatus, IosDialect, run_burst

CONF_TEXT = """# /etc/labdesk-cloud.conf
NS=labcloud
BRIDGE=virbr0
CLOUD_ADDR=100.64.0.1/24
ANSWER_IPS="8.8.8.8 1.1.1.1 9.9.9.9 208.67.222.222"
RETURN_PREFIXES="10.0.0.0/8 172.16.0.0/12 192.168.0.0/16"
EDGE_GW=100.64.0.2   # first router
"""

R1_DAY0 = """hostname R1
enable secret cisco
interface GigabitEthernet0/0
 no ip address
 no shutdown
interface GigabitEthernet0/0.10
 encapsulation dot1Q 10
 ip address 192.168.10.1 255.255.255.0
cdp run
line con 0
 exec-timeout 0 0
end
"""


# ----------------------------------------------------------------------------- conf
def test_parse_conf_and_defaults():
    c = cloud.parse_conf(CONF_TEXT)
    assert (c.ns, c.bridge, c.cloud_ip, c.prefixlen) == ("labcloud", "virbr0", "100.64.0.1", 24)
    assert c.answers == ("8.8.8.8", "1.1.1.1", "9.9.9.9", "208.67.222.222")
    assert c.edge_gw == "100.64.0.2" and c.net_text == "100.64.0.0/24" and c.netmask == "255.255.255.0"
    bad = cloud.parse_conf("CLOUD_ADDR=300.1.1.1/24\nANSWER_IPS=nope\nEDGE_GW=x")
    assert bad.cloud_ip == "100.64.0.1" and bad.answers[:2] == ("8.8.8.8", "1.1.1.1") and bad.edge_gw is None
    other = cloud.parse_conf("CLOUD_ADDR=198.18.7.1/28\nANSWER_IPS=1.1.1.1 8.8.8.8")
    assert other.net_text == "198.18.7.0/28" and other.netmask == "255.255.255.240"
    assert other.headline_answers == ("8.8.8.8", "1.1.1.1")


# ----------------------------------------------------------------------------- allocation
def test_next_free_ip_starts_at_dot2_and_skips_used():
    conf = cloud.CloudConf()
    assert cloud.next_free_ip(set(), conf) == "100.64.0.2"
    assert cloud.next_free_ip({"100.64.0.2", "100.64.0.3"}, conf) == "100.64.0.4"
    assert cloud.next_free_ip({"100.64.0.3"}, conf) == "100.64.0.2"
    small = cloud.parse_conf("CLOUD_ADDR=100.64.0.1/30")
    assert cloud.next_free_ip(set(), small) == "100.64.0.2"
    try:
        cloud.next_free_ip({"100.64.0.2"}, small)
        raise AssertionError("expected ValueError")
    except ValueError as e:
        assert "no free address" in str(e)


def test_ips_in_config_only_cloud_subnet():
    net = ipaddress.ip_network("100.64.0.0/24")
    text = R1_DAY0 + "interface Gi0/1\n ip address 100.64.0.7 255.255.255.0\n ip address 100.64.1.9 255.255.255.0\n"
    assert cloud.ips_in_config(text, net) == {"100.64.0.7"}
    assert cloud.ips_in_config("", net) == set()


# ----------------------------------------------------------------------------- config generation
def test_live_commands_with_and_without_ospf():
    lines = cloud.live_commands("GigabitEthernet0/1", "100.64.0.2")
    assert lines[0] == "interface GigabitEthernet0/1"
    assert " ip address 100.64.0.2 255.255.255.0" in lines and " no shutdown" in lines
    assert "ip route 0.0.0.0 0.0.0.0 100.64.0.1" in lines
    assert f"event manager applet {cloud.EEM_NAME}" in lines
    assert not any("ospf" in l for l in lines)
    o = cloud.live_commands("GigabitEthernet0/1", "100.64.0.2", ospf=["1"])
    assert o[-3:] == ["router ospf 1", " default-information originate", " exit"]


def test_patch_day0_inserts_in_ios_order_and_is_idempotent():
    out = cloud.patch_day0(R1_DAY0, "GigabitEthernet0/1", "100.64.0.2")
    lines = out.splitlines()
    i_if = lines.index("interface GigabitEthernet0/1")
    assert lines[i_if - 1] == " ip address 192.168.10.1 255.255.255.0"       # right after the last interface
    assert lines[i_if + 2] == " ip address 100.64.0.2 255.255.255.0"
    assert lines.index("ip route 0.0.0.0 0.0.0.0 100.64.0.1") < lines.index("line con 0")
    assert lines[-1] == "end" and out.count("ip route 0.0.0.0 0.0.0.0 100.64.0.1") == 1
    assert "hostname R1" in out and "enable secret cisco" in out and " exec-timeout 0 0" in out
    assert cloud.patch_day0(out, "GigabitEthernet0/1", "100.64.0.2") == out
    assert cloud.day0_has_cloud(out, "GigabitEthernet0/1", "100.64.0.2")
    assert not cloud.day0_has_cloud(R1_DAY0, "GigabitEthernet0/1", "100.64.0.2")


def test_patch_day0_replaces_existing_interface_and_adds_ospf_originate():
    cfg = R1_DAY0.replace("cdp run\n", "interface GigabitEthernet0/1\n ip address dhcp\n shutdown\n"
                                       "router ospf 1\n network 192.168.10.0 0.0.0.255 area 0\ncdp run\n")
    out = cloud.patch_day0(cfg, "GigabitEthernet0/1", "100.64.0.3", ospf=True)
    assert "ip address dhcp" not in out and "\n shutdown\n" not in out
    assert out.splitlines().count("interface GigabitEthernet0/1") == 1
    blk = out.split("router ospf 1\n", 1)[1].split("\n")
    assert blk[0] == " network 192.168.10.0 0.0.0.255 area 0" and blk[1] == " default-information originate"
    assert cloud.patch_day0(out, "GigabitEthernet0/1", "100.64.0.3", ospf=True) == out
    assert cloud.ospf_processes(out) == ["1"]


def test_patch_day0_without_interfaces_or_end():
    out = cloud.patch_day0("hostname X\n", "Ethernet0/1", "100.64.0.4")
    assert out.splitlines()[:4] == ["hostname X", "interface Ethernet0/1",
                                    " description INTERNET cloud (LabDesk: answers 8.8.8.8, 1.1.1.1)",
                                    " ip address 100.64.0.4 255.255.255.0"]


# ----------------------------------------------------------------------------- return routes
def test_routes_file_roundtrip():
    text = cloud.render_routes("100.64.0.2", {"192.168.30.0/24": "100.64.0.3", "10.9.0.0/16": "100.64.0.4"})
    edge, routes, edge_set = cloud.parse_routes(text)
    assert edge == "100.64.0.2" and edge_set
    assert routes == {"192.168.30.0/24": "100.64.0.3", "10.9.0.0/16": "100.64.0.4"}
    assert text.index("ROUTE 10.9.0.0/16") < text.index("ROUTE 192.168.30.0/24")      # numeric order
    assert cloud.parse_routes("EDGE_GW=\nROUTE bogus 1.2.3.4\nrm -rf /\n") == (None, {}, True)
    cmd = cloud.manual_routes_command(text)
    assert cmd.startswith("printf 'EDGE_GW=100.64.0.2\\n") and cmd.endswith("| sudo tee /var/lib/labdesk-cloud/routes.conf >/dev/null")


def test_plan_routes_first_router_becomes_edge():
    p = cloud.plan_routes(None, "100.64.0.2", ["192.168.10.0/24"], cloud.CloudConf(), live_edges={"100.64.0.2"})
    assert p.make_edge and p.edge == "100.64.0.2" and p.routes == {} and p.changed
    # the file already says so: nothing to change
    same = cloud.plan_routes(cloud.render_routes("100.64.0.2", {}), "100.64.0.2", [], live_edges={"100.64.0.2"})
    assert same.make_edge and not same.changed


def test_plan_routes_second_router_gets_per_prefix_routes():
    cur = cloud.render_routes("100.64.0.2", {})
    p = cloud.plan_routes(cur, "100.64.0.3", ["192.168.30.0/24", "100.64.0.0/24", "8.8.8.0/24", "3.3.3.3/32"],
                          live_edges={"100.64.0.2", "100.64.0.3"})
    assert not p.make_edge and p.edge == "100.64.0.2"
    assert p.routes == {"192.168.30.0/24": "100.64.0.3"}          # no cloud net, no public, no /32
    assert "per-prefix" in p.text and p.added == ["192.168.30.0/24"]


def test_plan_routes_stale_edge_is_taken_over():
    # /etc default EDGE_GW=.2 but no router uses .2 any more -> the router doing auto-configure takes over
    p = cloud.plan_routes(None, "100.64.0.5", ["10.1.0.0/24"], live_edges={"100.64.0.5"})
    assert p.make_edge and p.edge == "100.64.0.5"
    forced = cloud.plan_routes(cloud.render_routes("100.64.0.2", {"10.1.0.0/24": "100.64.0.5"}), "100.64.0.5", [],
                               live_edges={"100.64.0.2", "100.64.0.5"}, force_edge=True)
    assert forced.edge == "100.64.0.5" and forced.routes == {}


# ----------------------------------------------------------------------------- health
STATUS_UP = """# labdesk-cloud health
ts=1000
state=up
ns=labcloud
answers=8.8.8.8 1.1.1.1 9.9.9.9 208.67.222.222
selfping=100.64.0.1:ok 8.8.8.8:ok 1.1.1.1:ok 9.9.9.9:ok 208.67.222.222:ok
edge_gw=100.64.0.2
routes=10.0.0.0/8>100.64.0.2 172.16.0.0/12>100.64.0.2 192.168.0.0/16>100.64.0.2 192.168.30.0/24>100.64.0.3
rx_packets=42
healed=
problem=
"""


def test_health_up():
    h = cloud.health_from(STATUS_UP, ns_exists=True, host_ping=True, installed=True, now=1005)
    assert h.state == "up" and h.ok and h.headline == "cloud up — answers 8.8.8.8, 1.1.1.1"
    assert h.answers_text == "8.8.8.8 · 1.1.1.1" and h.edge_gw == "100.64.0.2" and h.rx_packets == 42
    assert ("192.168.30.0/24", "100.64.0.3") in h.routes and h.hint == ""
    assert all(h.selfping.values()) and h.status_age_s == 5


def test_health_down_states_and_fix_hint():
    stopped = cloud.health_from("ts=1000\nstate=stopped\n", ns_exists=False, host_ping=None, installed=True, now=1001)
    assert stopped.state == "stopped" and stopped.hint == "sudo systemctl start labdesk-cloud"
    assert "DOWN" in stopped.headline
    nons = cloud.health_from(STATUS_UP, ns_exists=False, host_ping=None, installed=True, now=1001)
    assert nons.state == "down" and "namespace" in nons.problem and nons.hint == cloud.FIX_HINT
    noping = cloud.health_from(STATUS_UP, ns_exists=True, host_ping=False, installed=True, now=1001)
    assert noping.state == "down" and "100.64.0.1" in noping.problem
    old = cloud.health_from(STATUS_UP, ns_exists=True, host_ping=True, installed=True, now=1000 + 600)
    assert old.state == "degraded" and "600 s old" in old.problem
    bad = cloud.health_from(STATUS_UP.replace("1.1.1.1:ok", "1.1.1.1:fail").replace("state=up", "state=degraded"),
                            ns_exists=True, host_ping=True, installed=True, now=1001)
    assert bad.state == "degraded" and bad.selfping["1.1.1.1"] is False
    missing = cloud.health_from(None, ns_exists=False, host_ping=None, installed=False)
    assert missing.state == "not_installed" and "install.sh" in missing.hint
    assert any(l.startswith("fix: ") for l in stopped.tooltip_lines())


# ----------------------------------------------------------------------------- catalog / topology
def test_catalog_cloud_type_and_node_mapping():
    d = catalog.BY_KEY["ext_cloud"]
    assert d.title == "Internet cloud (always answers 8.8.8.8 / 1.1.1.1)"
    assert d.node_definition == "external_connector" and d.configuration == "System Bridge"
    assert not d.counts_against_limit and d.tags == ("labdesk-cloud",) and d.shape == "cloud"
    node = {"node_definition": "external_connector", "configuration": "System Bridge", "tags": ["labdesk-cloud"]}
    assert catalog.type_for_cml_node(node).key == "ext_cloud" and cloud.is_cloud_node(node)
    plain = {"node_definition": "external_connector", "configuration": "System Bridge", "tags": []}
    assert catalog.type_for_cml_node(plain).key == "ext_bridge" and not cloud.is_cloud_node(plain)
    assert catalog.default_config(d, "INTERNET") == "System Bridge"


def test_topology_marks_cloud_egress():
    from labdesk_app.telemetry import topology_from_snapshot
    nodes = [{"id": "c", "label": "INTERNET", "node_definition": "external_connector", "state": "BOOTED",
              "configuration": [{"name": "default", "content": "System Bridge"}], "tags": ["labdesk-cloud"]},
             {"id": "b", "label": "BR1", "node_definition": "external_connector", "state": "BOOTED",
              "configuration": "System Bridge"}]
    topo, _ = topology_from_snapshot({"state": "STARTED"}, nodes, [], [])
    assert topo.nodes["cml:c"].egress_mode == "Cloud" and topo.nodes["cml:b"].egress_mode == "Bridge"


def test_create_node_sends_tags():
    from labdesk_app.cml_api import CMLClient
    sent = []

    def transport(method, url, body, headers, timeout):
        import json
        sent.append((method, url, json.loads(body) if body else None))
        if url.endswith("/authenticate"):
            return 200, b'"tok"'
        if method == "POST":
            return 200, b'{"id": "n1"}'
        return 200, b'{"id": "n1", "label": "INTERNET"}'
    import tempfile
    with tempfile.NamedTemporaryFile("w", suffix=".env", delete=False) as fh:
        fh.write("CML_ADMIN_USER=a\nCML_ADMIN_PASS=b\n")
    c = CMLClient("h", fh.name, transport=transport)
    c.create_node("lab", "INTERNET", "external_connector", 1, 2, "System Bridge", tags=("labdesk-cloud",))
    post = next(b for m, u, b in sent if m == "POST" and "/nodes" in u)
    assert post["tags"] == ["labdesk-cloud"] and post["configuration"] == "System Bridge"


# ----------------------------------------------------------------------------- console config step
class ConfigConsole(FakeConsole):
    """FakeConsole + configure terminal / sub-modes / end / write memory."""

    def __init__(self, *a, reject=(), confirm=False, **kw):
        super().__init__(*a, **kw)
        self.sub = ""
        self.reject = set(reject)
        self.applied: list[str] = []
        self.confirm = confirm

    def prompt(self):
        if self.mode == "config":
            return f"{self.host}(config{self.sub})#"
        return super().prompt()

    def _line(self, line: str):
        if self.mode == "config":
            self._emit(line + "\n")
            s = line.strip()
            if s in self.reject:
                self._emit("                ^\n% Invalid input detected at '^' marker.\n\n" + self.prompt())
                return
            if s == "end":
                self.mode, self.sub = "priv", ""
                self._emit(self.prompt())
                return
            if s == "exit":
                self.sub = ""
            elif s.startswith("interface "):
                self.sub = "-if"
            elif s.startswith("event manager applet"):
                self.sub = "-applet"
            elif s.startswith("router "):
                self.sub = "-router"
            self.applied.append(s)
            self._emit(self.prompt())
            return
        if self.mode == "priv" and line == "configure terminal":
            self._emit(line + "\nEnter configuration commands, one per line.  End with CNTL/Z.\n")
            self.mode = "config"
            self._emit(self.prompt())
            return
        if self.mode == "priv" and line == "write memory":
            if self.confirm:
                self.confirm = False
                self._emit(line + "\nOverwrite the previous NVRAM configuration?[confirm]")
                return
            self._emit(line + "\nBuilding configuration...\n[OK]\n" + self.prompt())
            return
        if self.mode == "priv" and line == "" and self.pending.endswith(b"[confirm]"):
            self._emit("\nBuilding configuration...\n[OK]\n" + self.prompt())
            return
        super()._line(line)


def test_run_burst_config_lines_enable_and_save():
    con = ConfigConsole("R1", "user", {"show ip interface brief": "Interface  IP-Address\n"}, enable_secret="cisco",
                        confirm=True)
    lines = cloud.live_commands("GigabitEthernet0/1", "100.64.0.2")
    res = run_burst(con, IosDialect(), ["show ip interface brief"], DeviceCreds(enable="cisco"), timeout=10,
                    config_lines=lines, save_config=True)
    assert res.status == BurstStatus.OK, res.reason
    assert res.outputs[CONFIG_KEY].error is None and res.outputs[SAVE_KEY].text == "[OK]\n"
    assert con.applied[:3] == ["interface GigabitEthernet0/1",
                               "description INTERNET cloud (LabDesk: answers 8.8.8.8, 1.1.1.1)",
                               "ip address 100.64.0.2 255.255.255.0"]
    assert "ip route 0.0.0.0 0.0.0.0 100.64.0.1" in con.applied
    sent = con.sent_text
    assert sent.index("configure terminal") < sent.index("end\r") < sent.index("write memory")
    assert sent.index("write memory") < sent.index("show ip interface brief")
    assert sent.rstrip().endswith("disable") or "disable\r" in sent          # line left at '>' as found
    assert con.mode == "user"


def test_run_burst_config_reports_rejected_lines_and_still_ends():
    con = ConfigConsole("R1", "priv", reject={"ip route 0.0.0.0 0.0.0.0 100.64.0.1"})
    res = run_burst(con, IosDialect(), [], timeout=10, config_lines=cloud.live_commands("Gi0/1", "100.64.0.2"),
                    save_config=True)
    assert res.status == BurstStatus.PARTIAL
    assert "ip route 0.0.0.0 0.0.0.0 100.64.0.1: % Invalid input" in res.outputs[CONFIG_KEY].error
    assert SAVE_KEY not in res.outputs                     # never save a half-applied change
    assert con.mode == "priv" and "end\r" in con.sent_text


def test_run_burst_config_needs_enable_secret():
    con = ConfigConsole("R1", "user", enable_secret="cisco")
    res = run_burst(con, IosDialect(), [], DeviceCreds(), timeout=5, config_lines=["interface Gi0/1"])
    assert res.status == BurstStatus.NEEDS_CREDS and "configure terminal" not in con.sent_text
    con2 = ConfigConsole("R1", "user", enable_secret="cisco")
    res2 = run_burst(con2, IosDialect(), [], DeviceCreds(enable="wrong"), timeout=8, config_lines=["interface Gi0/1"])
    assert res2.status == BurstStatus.NEEDS_CREDS and "configure terminal" not in con2.sent_text


def test_run_burst_config_refuses_foreign_config_mode():
    con = ConfigConsole("R1", "config")
    res = run_burst(con, IosDialect(), [], timeout=5, config_lines=["interface Gi0/1"])
    assert res.status == BurstStatus.BUSY and "interface Gi0/1" not in con.sent_text


# ----------------------------------------------------------------------------- root helper: routes validation
def _desired(tmp_path, routes_text, conf_text=CONF_TEXT):
    import subprocess
    from pathlib import Path
    common = Path(__file__).resolve().parent.parent / "host" / "labdesk-cloud" / "libexec" / "common.sh"
    conf = tmp_path / "labdesk-cloud.conf"
    conf.write_text(conf_text)
    routes = tmp_path / "routes.conf"
    routes.write_text(routes_text)
    cp = subprocess.run(["bash", "-c", f'. "{common}"; load_config; desired_routes | sort'],
                        env={"LABDESK_CLOUD_CONF": str(conf), "LABDESK_CLOUD_USER_ROUTES": str(routes)},
                        capture_output=True, text=True, timeout=20)
    return cp.stdout.split("\n")[:-1], cp.stderr


@pytest.mark.linux_only
def test_root_helper_validates_user_routes_file(tmp_path):
    out, err = _desired(tmp_path, cloud.render_routes("100.64.0.3", {"192.168.30.0/24": "100.64.0.4"}))
    assert out == ["10.0.0.0/8 100.64.0.3", "172.16.0.0/12 100.64.0.3", "192.168.0.0/16 100.64.0.3",
                   "192.168.30.0/24 100.64.0.4"]
    hostile = ("EDGE_GW=100.64.0.1\nROUTE 0.0.0.0/0 100.64.0.9\nROUTE 8.8.8.0/24 100.64.0.9\n"
               "ROUTE 192.168.1.0/24 192.168.1.1\nROUTE 192.168.5.1/24 100.64.0.9\n$(touch /tmp/x)\n"
               "ROUTE 100.64.0.0/24 100.64.0.9\n")
    out, err = _desired(tmp_path, hostile)
    # EDGE_GW=.1 (the cloud itself) is refused -> the /etc default .2 stays; every bad ROUTE is ignored
    assert out == ["10.0.0.0/8 100.64.0.2", "172.16.0.0/12 100.64.0.2", "192.168.0.0/16 100.64.0.2"]
    assert err.count("ignoring") >= 6
    out, _ = _desired(tmp_path, "EDGE_GW=\n")
    assert out == []                                     # explicit 'no edge': no return routes


# ----------------------------------------------------------------------------- UI pieces
def test_ui_cloud_tile_and_dialog_choices():
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    from labdesk_app.controller import CloudPlan
    from labdesk_app.ui.dialogs import CloudConfigDialog
    from labdesk_app.ui.graphics import NodeItem, RenderCtx, device_pixmap, tile_kind
    d = catalog.BY_KEY["ext_cloud"]
    assert tile_kind(d) == "cloud" and not device_pixmap(d, 40).isNull()
    item = NodeItem("cml:c", "INTERNET", d)
    assert item.is_internet and not NodeItem("cml:n", "NAT", catalog.BY_KEY["ext_nat"]).is_internet
    assert "8.8.8.8" in item.toolTip()
    conf = cloud.CloudConf()
    rp = cloud.plan_routes(None, "100.64.0.2", [], conf, live_edges={"100.64.0.2"})
    plan = CloudPlan("cml:r", "R1", "cml:c", "INTERNET", "GigabitEthernet0/1", "100.64.0.2", False, conf, ["1"],
                     cloud.live_commands("GigabitEthernet0/1", "100.64.0.2", conf), "BOOTED", True, False, False,
                     False, rp, [], True, needs_wipe=True)
    dlg = CloudConfigDialog(plan, None)
    ch = dlg.choices()
    # an unwired cable: wipe & restart is pre-selected, so the console step is skipped and day-0 is written
    assert ch == {"live": False, "day0": True, "ospf": True, "routes": True, "restart": True}
    assert "default-information originate" in dlg.preview.toPlainText()
    dlg.cb_restart.setChecked(False)
    dlg.cb_live.setChecked(True)
    dlg.cb_ospf.setChecked(False)
    assert dlg.choices()["live"] and "default-information" not in dlg.preview.toPlainText()
    _ = (app, RenderCtx)
