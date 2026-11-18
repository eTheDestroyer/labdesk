# PE Hub-Spoke HSRP lab

A small "service provider + customer" lab:

* **PE** - a Juniper vJunos router (provider edge). The customer lives in its own routing table, the
  routing-instance **CUSTOMER** (type `vrf`).
* **HUB** - the customer's central site (מרכז), LAN `192.168.10.0/24`.
* **R1 + R2** - the branch (סניף). Two Cisco routers on the same two LANs, sharing the default gateway
  with **HSRP**:
  * DATA (computers) `172.16.0.0/24` - **R1 is primary**
  * PHONE (telephony) `192.168.20.0/24` - **R2 is primary**
* **BGP** carries the routes, and **only prefix-lists** decide what is sent and accepted.

## Diagram

```
                         HUB  (Cisco IOSv, AS 65100)
                         Lo10 192.168.10.1/24   = "HUB LAN"
                         Gi0/0 10.1.1.2/30
                          |
                     [BR-PE-HUB]  CML System Bridge  (host virbr0)
                          |
                     ge-0/0/0 10.1.1.1/30
      +--------------------------------------------------+
      |  PE  (Juniper vJunos VM on host KVM, AS 65000)    |
      |  routing-instance CUSTOMER (vrf, RD 65000:100)    |
      |  fxp0 = management (DHCP on libvirt default)      |
      +--------------------------------------------------+
     ge-0/0/1 10.1.2.1/30                 ge-0/0/2 10.1.3.1/30
          |                                     |
     [BR-PE-R1]                            [BR-PE-R2]
          |                                     |
     Gi0/0 10.1.2.2/30                     Gi0/0 10.1.3.2/30
          R1  (AS 65200)  ---- iBGP ----  R2  (AS 65200)
     Gi0/1 .2   Gi0/2 .2                Gi0/1 .3   Gi0/2 .3
        |          |                       |          |
        |          +------ SW-PHONE -------+----------+     PHONE 192.168.20.0/24
        |                     |                             HSRP grp 20, VIP .1, R2 active
        +--------- SW-DATA ---+----------------------+      DATA  172.16.0.0/24
                      |       |                             HSRP grp 10, VIP .1, R1 active
                 PC-DATA    PC-PHONE
             172.16.0.10   192.168.20.10
```

CML-Free runs at most 5 counted nodes: HUB, R1, R2, PC-DATA, PC-PHONE. Switches and the three
`BR-PE-*` connectors don't count. The PE is not a CML node at all: it is a KVM VM on this PC
(`virsh -c qemu:///system list`), plugged into the same host bridge (`virbr0`) the CML "System Bridge"
connectors use.

## Addressing

| Device | Interface | Address | Notes |
|---|---|---|---|
| HUB | Lo10 | 192.168.10.1/24 | the HUB LAN (a loopback, no 6th node is allowed) |
| HUB | Gi0/0 | 10.1.1.2/30 | to PE ge-0/0/0 |
| PE | ge-0/0/0.0 | 10.1.1.1/30 | in CUSTOMER |
| PE | ge-0/0/1.0 | 10.1.2.1/30 | in CUSTOMER |
| PE | ge-0/0/2.0 | 10.1.3.1/30 | in CUSTOMER |
| PE | fxp0.0 | DHCP (192.168.122.238) | management, routing-instance `mgmt_junos` |
| R1 | Gi0/0 | 10.1.2.2/30 | to PE ge-0/0/1 |
| R1 | Gi0/1 | 172.16.0.2/24 | DATA, HSRP 10 priority 110 (active) |
| R1 | Gi0/2 | 192.168.20.2/24 | PHONE, HSRP 20 priority 100 |
| R2 | Gi0/0 | 10.1.3.2/30 | to PE ge-0/0/2 |
| R2 | Gi0/1 | 172.16.0.3/24 | DATA, HSRP 10 priority 100 |
| R2 | Gi0/2 | 192.168.20.3/24 | PHONE, HSRP 20 priority 110 (active) |
| VIP | DATA | 172.16.0.1 | gateway of PC-DATA |
| VIP | PHONE | 192.168.20.1 | gateway of PC-PHONE |
| PC-DATA | eth0 | 172.16.0.10/24 | gw 172.16.0.1 |
| PC-PHONE | eth0 | 192.168.20.10/24 | gw 192.168.20.1 |

BGP: PE AS 65000, HUB AS 65100, R1 + R2 AS 65200 (one site = one AS).

Logins: Cisco `admin` / `cisco`, enable `cisco` (SSH + telnet). PCs `cisco` / `cisco`.
PE: `JUNOS_ADMIN_USER` / `JUNOS_ADMIN_PASS` from `~/cml-labs/cml-credentials.env`
(`ssh <user>@192.168.122.238`, or the serial console `virsh -c qemu:///system console PE --force`).

## What each config block does

### Cisco routers (`configs/HUB.cfg`, `configs/R1.cfg`, `configs/R2.cfg`)

* **Basics** - hostname, `username admin privilege 15 secret cisco`, `enable secret cisco`,
  `ip domain name lab.local`, `no ip domain lookup`, `logging synchronous`, VTY `login local`,
  `transport input ssh telnet`.
* **`no service config`** + the **`LAB-DAY0-FIX` EEM applet** - the WAN port sits on `virbr0`, where
  libvirt hands out DHCP. On the first boot after a wipe IOS "AutoInstall" grabs a DHCP lease on Gi0/0
  and replaces the WAN address. When CML's config loader logs `CVAC-4-CONFIG_DONE` the applet puts the
  WAN address back, creates the 2048-bit RSA key for SSH, sets `ip ssh version 2` and saves.
* **HSRP** (R1/R2, `standby version 2`): group 10 on Gi0/1 (DATA), group 20 on Gi0/2 (PHONE).
  The primary has priority 110, the other 100; both have `preempt` (with a 15 s delay so a router
  that just came back first gets its BGP up).
* **Track ("proper track")** on R1/R2:
  * `ip sla 1` pings the PE end of its own WAN link every 5 s.
  * `track 1 ip sla 1 reachability` (delay down 10 s / up 15 s, so one lost ping doesn't flap HSRP).
  * `track 2 interface Gi0/0 line-protocol`.
  * `track 3 list boolean and` = "the WAN is usable" only when both are true.
  * The primary router has `standby <grp> track 3 decrement 20`: when its WAN dies its priority drops
    110 -> 90, below the other router's 100, and the other router (preempt) becomes active.
* **BGP** - eBGP to the PE (`timers 5 15`, so a dead WAN is noticed within 15 s), plus on R1/R2 an
  **iBGP** session between them over the DATA LAN with `next-hop-self`: if R1's WAN is down R1 can
  still reach the hub through R2 (and the other way round).
* **Prefix-list filtering (requirement 2)**
  * Spokes: `prefix-list BRANCH-OUT` (only 172.16.0.0/24 and 192.168.20.0/24) out to the PE,
    `prefix-list HUB-IN` (only 192.168.10.0/24) in from the PE and on the iBGP session.
  * HUB: `prefix-list HUB-LAN` out, `prefix-list BRANCH-LANS` in.
* **Path symmetry** - `route-map TO-PE`: R1 advertises DATA normally and PHONE with
  `as-path prepend 65200 65200`; R2 does the opposite. So the PE (and the HUB) send traffic for each
  LAN back to the router that is HSRP-active for it: no asymmetric routing.

### PE (`configs/PE.set.template`, saved as `PE.conf`)

* `system` - host name, root + admin user (SHA-512 hashes only), SSH, `management-instance`
  (fxp0 in its own table `mgmt_junos`), `delete chassis auto-image-upgrade`.
* `interfaces ge-0/0/0..2` - one NIC per customer site.
* `policy-options` - prefix-lists `HUB-LAN`, `BRANCH-LANS` and four policies:
  `FROM-HUB` (import from HUB: only HUB-LAN), `TO-HUB` (export to HUB: only BRANCH-LANS),
  `FROM-BRANCH` (import from R1/R2: only BRANCH-LANS), `TO-BRANCH` (export to R1/R2: only HUB-LAN).
  Each ends with `term rest then reject`.
* `routing-instances CUSTOMER` - `instance-type vrf`, `route-distinguisher 65000:100`,
  `vrf-target target:65000:100`, the three interfaces, and BGP groups `HUB` (peer-as 65100) and
  `BRANCH` (peer-as 65200) with the policies above and `hold-time 15`.
  The PE has no MPLS core here (one PE), so the RD / route-target are ready for a second PE but not
  used yet; the VRF itself works like a separate routing table.

## Build / rebuild

```
cd ~/cml-labs/labdesk
.venv/bin/python ../pe-hub-spoke/build_pe_hub_spoke.py --start          # lab + PE VM + LabDesk canvas, start all
.venv/bin/python ../pe-hub-spoke/build_pe_hub_spoke.py --pe-config      # first PE config (serial console)
.venv/bin/python ../pe-hub-spoke/build_pe_hub_spoke.py --pe-config --ssh   # re-push PE config over SSH
.venv/bin/python ../pe-hub-spoke/build_pe_hub_spoke.py --reconfigure R1 # wipe R1, store configs/R1.cfg, start
```

Or import `pe-hub-spoke.yaml` (the CML export, also in LabDesk `templates/pe-hub-spoke-hsrp.yaml`)
and run the script once for the PE VM. Stop any other running lab first (5-node limit).

`labcli.py` runs commands on any device from the host, e.g.
`.venv/bin/python ../pe-hub-spoke/labcli.py R1 "show standby brief"`,
`... labcli.py PE "show bgp summary instance CUSTOMER"`, `... labcli.py PC-DATA "ping -c 3 192.168.10.1"`.

## Verification commands

| Where | Command | Expect |
|---|---|---|
| PE | `show bgp summary instance CUSTOMER` | 3 peers `Establ` |
| PE | `show route table CUSTOMER.inet.0 protocol bgp` | 192.168.10.0/24 via HUB, 172.16.0.0/24 best via R1 (10.1.2.2), 192.168.20.0/24 best via R2 (10.1.3.2) |
| PE | `show route receive-protocol bgp 10.1.2.2 table CUSTOMER.inet.0` | what R1 sent (PHONE prepended) |
| HUB | `show ip bgp` | the two branch LANs only |
| R1/R2 | `show ip bgp` / `show ip route bgp` | 192.168.10.0/24 only |
| R1/R2 | `show standby brief` | R1 Active grp 10, R2 Active grp 20 |
| R1/R2 | `show track brief` | objects 1, 2, 3 Up |
| PC-DATA | `ping -c 3 192.168.10.1`, `traceroute -n 192.168.10.1` | first hop 172.16.0.2 (R1) |
| PC-PHONE | `ping -c 3 192.168.10.1`, `traceroute -n 192.168.10.1` | first hop 192.168.20.3 (R2) |

## Failover test (step by step)

1. R1: `configure terminal` / `interface Gi0/0` / `shutdown`.
2. R1 `show track brief`: track 2 goes Down at once, track 3 Down -> HSRP 10 priority 90.
3. R2 `show standby brief`: R2 is now **Active** for group 10 (DATA).
4. PC-DATA `ping 192.168.10.1` still works; `traceroute -n` now shows 172.16.0.3 (R2) first.
5. R1: `no shutdown`. After ~15 s (track up delay) + 15 s (preempt delay) R1 is Active again.
6. Same for the phone side with R2 Gi0/0, PC-PHONE and group 20.

Variation: shut the PE side instead (`set interfaces ge-0/0/1 disable`, `commit`) - R1's line protocol
stays up, so this time the **IP SLA** (track 1) is what notices it (~10-15 s).

## Files

| File | What |
|---|---|
| `pe-hub-spoke.yaml` | CML topology export (with the day-0 configs) |
| `configs/*.cfg`, `configs/PC-*.sh` | Cisco / alpine day-0 configs (also stored in the CML nodes) |
| `configs/PE.set.template` | PE config source (password hashes filled in at push time) |
| `PE.conf` | PE running config, `display set`, secrets replaced by `<redacted>` |
| `running/*.txt` | `show running-config` of HUB / R1 / R2 after verification |
| `verify.py` | the live checks below (`--steps base,filter,fail-r1,fail-r2`), log -> `verification.txt` |
| `verification-full-run.txt` | full run 2026-11-18: base, filtering, both failovers |
| `labdesk-overview.png` | LabDesk with this lab (PE drawn as a host-KVM device) |
| `build_pe_hub_spoke.py` | builds / repairs the lab (idempotent) |
| `labcli.py` | run commands on devices from the host |
| `labdesk_shot.py` | offscreen LabDesk screenshot of this lab |

## Verified results (2026-11-18)

* PE: 3 peers `Establ`; 172.16.0.0/24 best via 10.1.2.2 (R1, AS path `65200`), backup via R2
  (`65200 65200 65200`); 192.168.20.0/24 best via 10.1.3.2 (R2). HUB traceroute to 172.16.0.10 goes
  10.1.1.1 -> 10.1.2.2 (R1), to 192.168.20.10 goes 10.1.1.1 -> 10.1.3.2 (R2).
* HSRP: R1 Active grp 10 (pri 110), R2 Active grp 20 (pri 110); all tracks Up.
* PC-DATA and PC-PHONE ping 192.168.10.1 (0% loss); first hop 172.16.0.2 (R1) / 192.168.20.3 (R2).
* Filtering: a test 10.99.99.99/32 on R1 was "Not advertised to any peer" (Cisco prefix-list);
  with R1's filter opened on purpose the PE showed it `Hidden reason: Rejected by import policy`, and
  HUB never had it. Test config removed afterwards.
* Failover R1 (Cisco `shutdown` Gi0/0): track 2 down at once, R2 Active for grp 10, R1 reached the hub
  through iBGP via R2 (`Known via "bgp 65200", distance 200 ... 172.16.0.3`), PE moved 172.16.0.0/24 to
  R2, PC-DATA kept pinging (first hop 172.16.0.3). After `no shutdown` R1 preempted back (~55 s).
* Failover R2 (PE side, `set interfaces ge-0/0/2 disable`): R2 line protocol stayed Up, IP SLA went
  Down -> R1 Active for grp 20, PC-PHONE kept pinging via R1. Re-enabled -> R2 Active again (~50 s).
* A host reboot during the work: after restarting the lab (no wipe) everything came back by itself.

## Deviations from the first plan

* **802.1Q sub-interfaces over one PE NIC did not work**: virbr0 itself passes tagged frames, but the
  CML 2.10.1 System-Bridge external connector drops 802.1Q-tagged frames (untagged frames from HUB Gi0/0
  reached virbr0; tagged ones never did). So the PE got **one NIC per site** (ge-0/0/0, ge-0/0/1,
  ge-0/0/2), each matching one `BR-PE-*` connector, and the Cisco WAN ports are untagged.
  All three links still share the one virbr0 L2 segment (with 192.168.122.0/24): they are separated
  by their /30 subnets, not by VLANs. `no ip proxy-arp` on the Cisco WAN ports keeps ARP clean.
* **Track list uses `boolean and`**, not `or`: with one WAN link, "WAN usable" means the PE answers
  AND the port is up; `or` would only fail when both are down.
* **First boot after a wipe: IOS AutoInstall.** With Gi0/0 on virbr0 (libvirt DHCP) and an empty
  NVRAM, IOSv took a DHCP lease (192.168.122.x + a default route) on Gi0/0 and replaced the WAN
  address; `no service config` alone did not stop it. The `LAB-DAY0-FIX` EEM applet (see above) repairs
  it at `CVAC-4-CONFIG_DONE` and saves, so later reboots never run AutoInstall. A plain
  `event timer countdown` applet was not reliable (the CLI was busy during AutoInstall).
* PE management address: `192.168.122.238` (DHCP lease from libvirt, may change after a long
  power-off; `virsh -c qemu:///system domifaddr PE --source lease`).
