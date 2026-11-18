import json, sys
sys.path.insert(0, '/home/lf/cml-labs/labdesk')
COMMON_TAIL = """cdp run
line con 0
 exec-timeout 0 0
 logging synchronous
line vty 0 4
 login local
 transport input ssh telnet
end"""
HEAD = """hostname {h}
no ip domain lookup
username admin privilege 15 secret cisco
enable secret cisco
"""
R1 = HEAD.format(h="R1") + """interface Loopback0
 ip address 1.1.1.1 255.255.255.255
interface GigabitEthernet0/0
 description LAN-A (HSRP v2 grp 10 VIP 192.168.10.1)
 ip address 192.168.10.2 255.255.255.0
 standby version 2
 standby 10 ip 192.168.10.1
 standby 10 priority 110
 standby 10 preempt
 cdp enable
 no shutdown
interface GigabitEthernet0/1
 description LAN-B (VRRP grp 20 VIP 192.168.20.1)
 ip address 192.168.20.2 255.255.255.0
 vrrp 20 ip 192.168.20.1
 vrrp 20 priority 110
 vrrp 20 preempt
 cdp enable
 no shutdown
interface GigabitEthernet0/2
 description P2P to R3 Gi0/0
 ip address 10.0.13.1 255.255.255.252
 ip ospf network point-to-point
 cdp enable
 no shutdown
interface GigabitEthernet0/3
 description XLINK to R2 Gi0/3 (OSPF broadcast)
 ip address 10.0.12.1 255.255.255.252
 cdp enable
 no shutdown
router ospf 1
 router-id 1.1.1.1
 passive-interface GigabitEthernet0/0
 passive-interface GigabitEthernet0/1
 network 1.1.1.1 0.0.0.0 area 0
 network 192.168.10.0 0.0.0.255 area 0
 network 192.168.20.0 0.0.0.255 area 0
 network 10.0.13.0 0.0.0.3 area 0
 network 10.0.12.0 0.0.0.3 area 0
""" + COMMON_TAIL
R2 = HEAD.format(h="R2") + """interface Loopback0
 ip address 2.2.2.2 255.255.255.255
interface GigabitEthernet0/0
 description LAN-A (HSRP v2 grp 10 VIP 192.168.10.1)
 ip address 192.168.10.3 255.255.255.0
 standby version 2
 standby 10 ip 192.168.10.1
 standby 10 priority 100
 standby 10 preempt
 cdp enable
 no shutdown
interface GigabitEthernet0/1
 description LAN-B (VRRP grp 20 VIP 192.168.20.1)
 ip address 192.168.20.3 255.255.255.0
 vrrp 20 ip 192.168.20.1
 vrrp 20 priority 100
 vrrp 20 preempt
 cdp enable
 no shutdown
interface GigabitEthernet0/2
 description P2P to R3 Gi0/1
 ip address 10.0.23.1 255.255.255.252
 ip ospf network point-to-point
 cdp enable
 no shutdown
interface GigabitEthernet0/3
 description XLINK to R1 Gi0/3 (OSPF broadcast)
 ip address 10.0.12.2 255.255.255.252
 cdp enable
 no shutdown
interface GigabitEthernet0/4
 description BACKUP to R4 Gi0/2 (admin down)
 ip address 10.0.24.1 255.255.255.252
 shutdown
router ospf 1
 router-id 2.2.2.2
 passive-interface GigabitEthernet0/0
 passive-interface GigabitEthernet0/1
 network 2.2.2.2 0.0.0.0 area 0
 network 192.168.10.0 0.0.0.255 area 0
 network 192.168.20.0 0.0.0.255 area 0
 network 10.0.23.0 0.0.0.3 area 0
 network 10.0.12.0 0.0.0.3 area 0
""" + COMMON_TAIL
R3 = HEAD.format(h="R3") + """interface Loopback0
 ip address 3.3.3.3 255.255.255.255
interface GigabitEthernet0/0
 description P2P to R1 Gi0/2
 ip address 10.0.13.2 255.255.255.252
 ip ospf network point-to-point
 cdp enable
 no shutdown
interface GigabitEthernet0/1
 description P2P to R2 Gi0/2
 ip address 10.0.23.2 255.255.255.252
 ip ospf network point-to-point
 cdp enable
 no shutdown
interface GigabitEthernet0/2
 description eBGP to R4 Gi0/0 (AS 65004)
 ip address 10.0.34.1 255.255.255.252
 cdp enable
 no shutdown
router ospf 1
 router-id 3.3.3.3
 network 3.3.3.3 0.0.0.0 area 0
 network 10.0.13.0 0.0.0.3 area 0
 network 10.0.23.0 0.0.0.3 area 0
 default-information originate
router bgp 65000
 bgp router-id 3.3.3.3
 bgp log-neighbor-changes
 neighbor 10.0.34.2 remote-as 65004
 neighbor 10.0.34.2 description R4-ISP-EDGE
 address-family ipv4
  redistribute ospf 1
  neighbor 10.0.34.2 activate
 exit-address-family
""" + COMMON_TAIL
R4 = HEAD.format(h="R4") + """interface Loopback0
 ip address 4.4.4.4 255.255.255.255
interface GigabitEthernet0/0
 description eBGP to R3 Gi0/2 (AS 65000)
 ip address 10.0.34.2 255.255.255.252
 ip nat inside
 cdp enable
 no shutdown
interface GigabitEthernet0/1
 description INTERNET via CML NAT (DHCP)
 ip address dhcp
 ip nat outside
 no shutdown
interface GigabitEthernet0/2
 description BACKUP to R2 Gi0/4 (R2 side admin down)
 ip address 10.0.24.2 255.255.255.252
 ip nat inside
 cdp enable
 no shutdown
ip access-list standard NAT-INSIDE
 permit 10.0.0.0 0.255.255.255
 permit 192.168.10.0 0.0.0.255
 permit 192.168.20.0 0.0.0.255
 permit 1.1.1.1
 permit 2.2.2.2
 permit 3.3.3.3
 permit 4.4.4.4
ip nat inside source list NAT-INSIDE interface GigabitEthernet0/1 overload
router bgp 65004
 bgp router-id 4.4.4.4
 bgp log-neighbor-changes
 neighbor 10.0.34.1 remote-as 65000
 neighbor 10.0.34.1 description R3-CORE
 address-family ipv4
  network 4.4.4.4 mask 255.255.255.255
  neighbor 10.0.34.1 activate
  neighbor 10.0.34.1 default-originate
 exit-address-family
""" + COMMON_TAIL

def ifs(n): return [{"id": f"i{k}", "label": f"GigabitEthernet0/{k}", "slot": k, "type": "physical"} for k in range(n)]
def ports(n): return [{"id": f"i{k}", "label": f"port{k}", "slot": k, "type": "physical"} for k in range(n)]
nodes = [
 {"id":"n1","label":"R1","node_definition":"iosv","x":-250,"y":0,"configuration":R1,"interfaces":ifs(4)},
 {"id":"n2","label":"R2","node_definition":"iosv","x":250,"y":0,"configuration":R2,"interfaces":ifs(5)},
 {"id":"n3","label":"R3","node_definition":"iosv","x":0,"y":-200,"configuration":R3,"interfaces":ifs(3)},
 {"id":"n4","label":"R4","node_definition":"iosv","x":0,"y":-400,"configuration":R4,"interfaces":ifs(3)},
 {"id":"n5","label":"SW-A","node_definition":"unmanaged_switch","x":-150,"y":200,"configuration":"","interfaces":ports(4)},
 {"id":"n6","label":"SW-B","node_definition":"unmanaged_switch","x":150,"y":200,"configuration":"","interfaces":ports(4)},
 {"id":"n7","label":"NAT","node_definition":"external_connector","x":0,"y":-580,"configuration":"NAT","interfaces":[{"id":"i0","label":"port","slot":0,"type":"physical"}]},
]
L = [("n1","i0","n5","i0"),("n2","i0","n5","i1"),("n1","i1","n6","i0"),("n2","i1","n6","i1"),
     ("n1","i2","n3","i0"),("n2","i2","n3","i1"),("n1","i3","n2","i3"),("n3","i2","n4","i0"),
     ("n4","i1","n7","i0"),("n2","i4","n4","i2")]
links = [{"id":f"l{k}","n1":a,"i1":ai,"n2":b,"i2":bi} for k,(a,ai,b,bi) in enumerate(L)]
topo = {"lab":{"title":"Protocol Showcase","version":"0.3.0","description":"HSRP/VRRP/OSPF p2p+broadcast/eBGP/default via NAT/admin-down link","notes":"LabDesk fixture lab"},"nodes":nodes,"links":links,"annotations":[]}
from labdesk_app.cml_api import CMLClient
c = CMLClient()
if c.find_lab("Protocol Showcase"): sys.exit("exists")
lid = c.import_topology(topo, "Protocol Showcase")
print(lid)
c.start_lab(lid)
