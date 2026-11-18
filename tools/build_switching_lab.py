"""Build the "Switching Lab" (SW1/SW2 IOSvL2, R1 router-on-a-stick, PC1/PC2 alpine) through the
LabDesk controller, so the app's own code paths (add_device / add_link / set_config) are used.

    .venv/bin/python tools/build_switching_lab.py [--start]

Refuses to touch an existing lab with the same title.  Day-0 configs are stored as CML node
configurations.  Interface plan (next-free-interface order matters, so links are added in order):

    SW2 Gi0/0  -- PC2 eth0      access VLAN 20
    SW1 Gi0/0  -- R1  Gi0/0     dot1q trunk 10,20 (R1 subifs .10/.20)
    SW1 Gi0/1  -- SW2 Gi0/1     Po1 LACP (SW1 active / SW2 passive), trunk 10,20
    SW1 Gi0/2  -- SW2 Gi0/2     Po1
    SW1 Gi0/3  -- PC1 eth0      access VLAN 10
    SW1 Gi1/0  -- SW2 Gi1/0     Po2 PAgP (SW1 desirable / SW2 auto), trunk 10,20 (STP blocks SW2 Po2)
    SW1 Gi1/1  -- SW2 Gi1/1     Po2   (Gi1/0, Gi1/1 are created explicitly: slots 4 and 5)

The INTERNET cloud on R1 Gi0/1 is added with LabDesk's "Auto-configure for Internet cloud".
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from labdesk_app.controller import LabController  # noqa: E402

TITLE = "Switching Lab"

# IOSvL2 (iosvl2-2020) quirk, seen live: an SVI configured from the day-0 config stays down/down
# after boot even though its VLAN has forwarding ports; a shutdown/no shutdown fixes it.  This
# one-shot EEM applet bounces it 60 s after the config is loaded.
SVI_KICK = """event manager applet LABDESK-SVI-KICK
 event timer countdown time 60
 action 1.0 cli command "enable"
 action 2.0 cli command "configure terminal"
 action 3.0 cli command "interface Vlan10"
 action 4.0 cli command "shutdown"
 action 5.0 cli command "no shutdown"
 action 6.0 cli command "end"
"""

TAIL = """cdp run
line con 0
 exec-timeout 0 0
 logging synchronous
line vty 0 4
 login local
 transport input ssh telnet
end
"""
HEAD = """hostname {h}
no ip domain lookup
username admin privilege 15 secret cisco
enable secret cisco
"""
TRUNK = """ switchport trunk encapsulation dot1q
 switchport trunk allowed vlan 10,20
 switchport mode trunk
 switchport nonegotiate
 no shutdown
"""
SW_COMMON = """vtp mode transparent
spanning-tree mode rapid-pvst
vlan 10
 name USERS
vlan 20
 name SERVERS
"""

SW1 = HEAD.format(h="SW1") + SW_COMMON + """spanning-tree vlan 10,20 priority 4096
interface GigabitEthernet0/0
 description TRUNK to R1 Gi0/0 (router-on-a-stick)
""" + TRUNK + """interface GigabitEthernet0/1
 description TRUNK to SW2 Gi0/1
""" + TRUNK + """interface GigabitEthernet0/2
 description TRUNK to SW2 Gi0/2
""" + TRUNK + """interface GigabitEthernet0/3
 description PC1 (VLAN 10 USERS)
 switchport mode access
 switchport access vlan 10
 spanning-tree portfast edge
 no shutdown
interface Vlan10
 description management SVI
 ip address 192.168.10.2 255.255.255.0
 no shutdown
ip default-gateway 192.168.10.1
""" + SVI_KICK + TAIL

SW2 = HEAD.format(h="SW2") + SW_COMMON + """interface GigabitEthernet0/0
 description PC2 (VLAN 20 SERVERS)
 switchport mode access
 switchport access vlan 20
 spanning-tree portfast edge
 no shutdown
interface GigabitEthernet0/1
 description TRUNK to SW1 Gi0/1
""" + TRUNK + """interface GigabitEthernet0/2
 description TRUNK to SW1 Gi0/2
""" + TRUNK + """interface GigabitEthernet0/3
 description unused
 shutdown
interface Vlan10
 description management SVI
 ip address 192.168.10.3 255.255.255.0
 no shutdown
ip default-gateway 192.168.10.1
""" + SVI_KICK + TAIL

R1 = HEAD.format(h="R1") + """interface GigabitEthernet0/0
 description TRUNK to SW1 Gi0/0 (router-on-a-stick)
 no ip address
 cdp enable
 no shutdown
interface GigabitEthernet0/0.10
 description VLAN 10 USERS gateway
 encapsulation dot1Q 10
 ip address 192.168.10.1 255.255.255.0
interface GigabitEthernet0/0.20
 description VLAN 20 SERVERS gateway
 encapsulation dot1Q 20
 ip address 192.168.20.1 255.255.255.0
""" + TAIL


def alpine(h: str, cidr: str, gw: str) -> str:
    return f"""# this is a shell script which will be sourced at boot
hostname {h}
# configurable user account
USERNAME=cisco
PASSWORD=cisco
ip link set dev eth0 up
ip address add {cidr} dev eth0
ip route add default via {gw}
"""


PC1 = alpine("PC1", "192.168.10.10/24", "192.168.10.1")
PC2 = alpine("PC2", "192.168.20.10/24", "192.168.20.1")


def _with_etherchannels(cfg: str, me: str, peer: str, lacp: str, pagp: str) -> str:
    """Po1 (LACP, Gi0/1+Gi0/2) and Po2 (PAgP, Gi1/0+Gi1/1), both trunk 10,20, load-balance src-dst-ip."""
    from labdesk_app import etherchannel as EC
    l2 = EC.l2_lines(EC.iface_block(cfg, "Gi0/1"))
    cfg = EC.patch_day0(cfg, 1, ["Gi0/1", "Gi0/2"], lacp, l2, f"LACP bundle to {peer} Po1 (Gi0/1+Gi0/2)", "src-dst-ip",
                        {"Gi0/1": f"Po1 LACP member to {peer} Gi0/1", "Gi0/2": f"Po1 LACP member to {peer} Gi0/2"})
    return EC.patch_day0(cfg, 2, ["Gi1/0", "Gi1/1"], pagp, l2, f"PAgP bundle to {peer} Po2 (Gi1/0+Gi1/1)", "src-dst-ip",
                         {"Gi1/0": f"Po2 PAgP member to {peer} Gi1/0", "Gi1/1": f"Po2 PAgP member to {peer} Gi1/1"})


SW1 = _with_etherchannels(SW1, "SW1", "SW2", "active", "desirable")
SW2 = _with_etherchannels(SW2, "SW2", "SW1", "passive", "auto")
CONFIGS = {"R1": R1, "SW1": SW1, "SW2": SW2, "PC1": PC1, "PC2": PC2}


def reconfigure(labels: list[str]) -> None:
    """Stop + wipe the given nodes, store the current day-0 config, start them again."""
    ctl = LabController(telemetry=False)
    if not ctl.open_lab_by_title(TITLE):
        raise SystemExit(f"no lab {TITLE!r}")
    nodes = {n["label"]: n for n in ctl.client.nodes(ctl.lab_id)}
    for lbl in labels:
        key = f"cml:{nodes[lbl]['id']}"
        ctl.wipe(key)
        ctl.set_config(key, CONFIGS[lbl])
        print(f"{lbl}: wiped, day-0 config stored")
    for lbl in labels:
        print(ctl.start(f"cml:{nodes[lbl]['id']}") or f"{lbl}: started")


def main() -> int:
    if "--reconfigure" in sys.argv:
        reconfigure([a for a in sys.argv[1:] if not a.startswith("--")] or list(CONFIGS))
        return 0
    ctl = LabController(telemetry=False)
    if ctl.client.find_lab(TITLE):
        print(f"lab {TITLE!r} already exists; not touching it")
        return 1
    lab_id = ctl.new_lab(TITLE)
    print("lab", lab_id)
    keys = {}
    for label, kind, x, y, cfg in (("R1", "iosv", 0, -230, R1), ("SW1", "iosvl2", -190, 0, SW1),
                                   ("SW2", "iosvl2", 190, 0, SW2), ("PC1", "alpine", -330, 220, PC1),
                                   ("PC2", "alpine", 330, 220, PC2)):
        keys[label] = ctl.add_device(kind, x, y, label=label)
        ctl.set_config(keys[label], cfg)
    for a, b in (("SW2", "PC2"), ("SW1", "R1"), ("SW1", "SW2"), ("SW1", "SW2"), ("SW1", "PC1")):
        print(ctl.add_link(keys[a], keys[b]).text)
    # Po2 members Gi1/0, Gi1/1 (slots 4, 5) - explicit, the next free interface on SW2 would be Gi0/3
    ifs = {}
    for sw in ("SW1", "SW2"):
        for slot in (4, 5):
            ifs[(sw, slot)] = ctl.client.create_interface(lab_id, keys[sw][4:], slot, sw)
    for slot in (4, 5):
        ctl.client.create_link(lab_id, ifs[("SW1", slot)]["id"], ifs[("SW2", slot)]["id"])
        print(f"link SW1 {ifs[('SW1', slot)].get('label')} <-> SW2 {ifs[('SW2', slot)].get('label')}")
    if "--start" in sys.argv:
        print(ctl.start_all() or "started")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
