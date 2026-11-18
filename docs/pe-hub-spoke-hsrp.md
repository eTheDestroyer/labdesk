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
                     [BR-PE-HUB]  CML connector "PE-HUB" = bridge1 -> host labdesk-pe-hub
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
     "PE-R1" = bridge2                     "PE-R2" = bridge3
     host labdesk-pe-r1                    host labdesk-pe-r2
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
(`virsh -c qemu:///system list`).

## How it's wired (one isolated L2 segment per PE link)

Every PE link behaves like its own cable: nothing else is on it, no DHCP, no IP on the host.

```
 CML node port          CML connector       CML controller          host (libvirt, qemu:///system)      PE (vJunos)
 HUB Gi0/0  ---------- BR-PE-HUB "PE-HUB" -- bridge1 on enp7s0 -- net labdesk-pe-hub (virbr1) -- NIC 2 = ge-0/0/0
 R1  Gi0/0  ---------- BR-PE-R1  "PE-R1"  -- bridge2 on enp8s0 -- net labdesk-pe-r1  (virbr2) -- NIC 3 = ge-0/0/1
 R2  Gi0/0  ---------- BR-PE-R2  "PE-R2"  -- bridge3 on enp9s0 -- net labdesk-pe-r2  (virbr3) -- NIC 4 = ge-0/0/2
                                             bridge0 on enp1s0 -- net default        (virbr0) -- NIC 1 = fxp0 (mgmt, DHCP)
```

* **Host**: three libvirt networks `labdesk-pe-hub`, `labdesk-pe-r1`, `labdesk-pe-r2` with no `<ip>` and no
  `<forward>` (persistent, autostart). libvirt names their bridges `virbr1..3` (the numbers can differ on
  another host; the network names are what counts).
* **CML VM `cml`**: one extra virtio NIC per network (`virsh domiflist cml`). New NICs need a CML reboot.
* **CML controller** (Cockpit -> Networking, done by the build script with `nmcli` over Cockpit as the
  sysadmin user): `bridge1/2/3` (STP off, no IPv4/IPv6) each with its NIC as the only port, same style as
  CML's own `bridge0`. CML then lists them as external connectors (`GET /api/v0/system/external_connectors`,
  after a rescan `PUT` of the same URL); they are labelled/tagged `PE-HUB`, `PE-R1`, `PE-R2`.
* **Lab**: each `BR-PE-*` external connector's configuration is its bridge's label (`PE-HUB`, ...) instead
  of `System Bridge`.
* **PE**: fxp0 stays on libvirt `default` (management, 192.168.122.x); ge-0/0/0..2 are on the three
  isolated networks. The MACs (and so the ge-0/0/x order) did not change.
* Management of the lab is unchanged: CML at 192.168.122.60 (bridge0), PE fxp0 by DHCP on `default`.

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

### First logins

LabDesk shows these on each device's hover card and saves them in `~/.config/labdesk/device-creds` (0600); right-click
a device → **Copy password**. Checked live on 2026-11-18 (HUB, PC-DATA, PE).

| Device | Console | SSH / telnet | enable |
|---|---|---|---|
| HUB, R1, R2 (Cisco IOSv) | no login: press Enter (`line con 0` has no `login`) | `admin` / `cisco` (VTY `login local`) | `cisco` |
| PC-DATA, PC-PHONE (alpine) | `cisco` / `cisco` | - | - (shell user) |
| PE (vJunos) | `admin`, or `root` (both set from `cml-credentials.env`) | `ssh admin@192.168.122.238` | - |
| SW-DATA, SW-PHONE, BR-PE-* | no console | - | - |

PE password: saved in LabDesk (hover card / right-click → **Copy password**); or
`grep JUNOS_ADMIN_PASS ~/cml-labs/cml-credentials.env`. User name = `JUNOS_ADMIN_USER` from the same file.
PE console: `virsh -c qemu:///system console PE --force`.

## What each config block does

### Cisco routers (`configs/HUB.cfg`, `configs/R1.cfg`, `configs/R2.cfg`)

* **Basics** - hostname, `username admin privilege 15 secret cisco`, `enable secret cisco`,
  `ip domain name lab.local`, `no ip domain lookup`, `logging synchronous`, VTY `login local`,
  `transport input ssh telnet`.
* **`no service config`** + the **`LAB-DAY0-FIX` EEM applet** - written when the WAN port sat on `virbr0`,
  where libvirt hands out DHCP: on the first boot after a wipe IOS "AutoInstall" grabbed a DHCP lease on
  Gi0/0 and replaced the WAN address. When CML's config loader logs `CVAC-4-CONFIG_DONE` the applet puts
  the WAN address back, creates the 2048-bit RSA key for SSH, sets `ip ssh version 2` and saves.
  Since every WAN link is now an isolated segment with no DHCP server, AutoInstall finds nothing; the
  applet is kept as **belt-and-braces** (it is harmless: it re-applies the same address, and it still
  creates the SSH key and saves). Same for `no ip proxy-arp` on Gi0/0: no longer needed for a clean ARP
  table on a shared segment, kept because a /30 WAN never needs proxy-ARP.
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
.venv/bin/python ../pe-hub-spoke/build_pe_hub_spoke.py --restart-cml    # first time on a host: also add the
                                    # CML NICs and reboot CML for them (stops this lab, no wipe)
.venv/bin/python ../pe-hub-spoke/build_pe_hub_spoke.py --pe-config      # first PE config (serial console)
.venv/bin/python ../pe-hub-spoke/build_pe_hub_spoke.py --pe-config --ssh   # re-push PE config over SSH
.venv/bin/python ../pe-hub-spoke/build_pe_hub_spoke.py --reconfigure R1 # wipe R1, store configs/R1.cfg, start
```

Every run is idempotent: it ensures the 3 isolated libvirt networks, the 3 CML VM NICs (reboot only with
`--restart-cml`), `bridge1..3` on the CML controller + their connector labels, the `BR-PE-*` connector
configs (a stopped connector is wiped to change it: it has no disk state) and the PE NIC networks
(moved live with `virsh update-device --live --config`, same MAC, so no PE reboot).

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
| `verification.txt` | full run after isolating the PE links (base, filtering, both failovers, isolation proof) |
| `verification-full-run.txt` | first full run 2026-11-18 morning (links still on virbr0) |
| `labdesk-overview.png` | LabDesk with this lab (PE drawn as a host-KVM device; SW-DATA shows `iBGP R1↔R2`: the R1-R2 iBGP session crosses that LAN, the switch runs no BGP) |
| `build_pe_hub_spoke.py` | builds / repairs the lab (idempotent) |
| `labcli.py` | run commands on devices from the host |
| `labdesk_shot.py` | offscreen LabDesk screenshot of this lab |

## Verified results (2026-11-18)

**After isolating the PE links (afternoon, `verification.txt`)** - everything below passed again:
PE 3/3 `Establ`; HUB only the two branch LANs; R1 Active grp 10 / R2 Active grp 20, all tracks Up;
PC-DATA / PC-PHONE ping 192.168.10.1 0% loss, first hop 172.16.0.2 / 192.168.20.3; filter test A
("Not advertised to any peer") and B ("Rejected by import policy", HUB never had it); failover R1
(Cisco shutdown): R2 Active for grp 10 after 22 s, R1 back after 42 s; failover R2 (PE side
`ge-0/0/2 disable`): R1 Active for grp 20 after 46 s, R2 back after 47 s.
Isolation proof: each `virbr1..3` learns exactly two MACs (one PE ge port + one Cisco Gi0/0);
`virbr0` only PE fxp0 + the CML controller; `virsh net-dhcp-leases default` lists no lab router; HUB with a
temporary 192.168.122.250/24 on Gi0/0 pings the PE (10.1.1.1) but ARP for 192.168.122.1 / .60 stays
`Incomplete`. (tcpdump on the bridges needs root here, so it was not used.)

Earlier run (links still on virbr0):

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

* **Isolated links (2026-11-18 afternoon)**: the PE NICs were moved with `virsh update-device --live --config`
  (same MAC), so the PE did not need a reboot. Changing a connector's bridge in CML 2.10 is only allowed on a
  wiped connector node (they hold no state). Careful on the CML controller: re-activating a bridge
  (`nmcli con up bridgeN`) while the lab runs drops CML's connector port from it and cuts the link (it
  happened once; restarting the three `BR-PE-*` nodes re-attached them). The build script only activates
  a bridge that isn't active. 802.1Q over the isolated bridges was not re-tested (design unchanged).

* **802.1Q sub-interfaces over one PE NIC did not work**: virbr0 itself passes tagged frames, but the
  CML 2.10.1 System-Bridge external connector drops 802.1Q-tagged frames (untagged frames from HUB Gi0/0
  reached virbr0; tagged ones never did). So the PE got **one NIC per site** (ge-0/0/0, ge-0/0/1,
  ge-0/0/2), each matching one `BR-PE-*` connector, and the Cisco WAN ports are untagged.
  Since 2026-11-18 (afternoon) each link is its own isolated L2 segment (see "How it's wired"); before
  that all three shared virbr0 with 192.168.122.0/24 and were separated only by their /30 subnets.
* **Track list uses `boolean and`**, not `or`: with one WAN link, "WAN usable" means the PE answers
  AND the port is up; `or` would only fail when both are down.
* **First boot after a wipe: IOS AutoInstall** (historical: fixed for good by the isolated links). With Gi0/0 on virbr0 (libvirt DHCP) and an empty
  NVRAM, IOSv took a DHCP lease (192.168.122.x + a default route) on Gi0/0 and replaced the WAN
  address; `no service config` alone did not stop it. The `LAB-DAY0-FIX` EEM applet (see above) repairs
  it at `CVAC-4-CONFIG_DONE` and saves, so later reboots never run AutoInstall. A plain
  `event timer countdown` applet was not reliable (the CLI was busy during AutoInstall).
* PE management address: `192.168.122.238` (DHCP lease from libvirt, may change after a long
  power-off; `virsh -c qemu:///system domifaddr PE --source lease`).
