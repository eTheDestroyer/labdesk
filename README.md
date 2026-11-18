# LabDesk

**A native desktop app for Linux and Windows for designing, running and watching Cisco CML network labs, with real-tty consoles and a live protocol map.**

LabDesk is a Python 3.13 + PySide6 front end for **Cisco Modeling Labs (CML-Free 2.10)** running on KVM. It also handles
host-KVM **Juniper vJunos** and **Fortinet FortiOS** VMs. Drag routers and switches onto a canvas, cable them up and boot them.
Double-click any device to get a **real console in a Konsole tab**, where Tab completion, `?`, Ctrl-C and the arrow keys work as on
physical hardware, with no browser or websocket lag.

While the lab runs, LabDesk polls the devices and draws what is actually configured on them:

- **Routing & redundancy:** OSPF, BGP, HSRP and VRRP chips on links and nodes, point-to-point links, and the default route to the
  Internet highlighted in blue.
- **Switching:** VLANs, 802.1Q trunks, Rapid-PVST+ roles (blocked ports marked), and **EtherChannel** bundles (LACP, PAgP, static).
- **Live subnets:** an interface's subnet appears on its cable as soon as you configure it.
- **Failures you can see:** link down, admin down, line-protocol down and pulled cables are shown with shapes as well as colours.
  You can also simulate failures (cut and restore links) and watch the network converge.
- **Device credentials & hover card:** a username / password / enable secret per device (right-click ▸
  *Credentials…*), and a live info card when you rest the pointer on a device: interfaces, protocols, the stored
  login and the device's own accounts, with each password marked *encrypted (type 9 scrypt)* or *plain text*.
- **Always-on Internet cloud:** an optional host network namespace that answers `8.8.8.8` / `1.1.1.1` from inside the lab.

Also included: lab templates (3-router HSRP, hub-and-spoke HSRP/BGP/ACL, switching with LACP/PAgP), SSH/Telnet/HTTP
right-click management, light/dark/KDE themes, a 300+ test suite built on real captured device output, and QA screenshot tools.

> Requires Linux (KDE/Konsole recommended), libvirt/KVM and a CML-Free VM. Device images (IOSv, IOSvL2, vJunos, FortiOS) are
> **not** included. Get them from the vendors under their licences.

**Windows 10/11:** download **LabDesk.exe** from the GitHub *Releases* page and run it; a first-run dialog asks for the
CML address and login ([INSTALL.md → LabDesk for Windows](INSTALL.md#labdesk-for-windows)). It needs a CML VM that
Windows can reach (for example CML in VMware Workstation). Consoles open as tabs in Windows Terminal (or a console
window) with the built-in OpenSSH client; you type the CML password there unless `~/.ssh/labdesk_ed25519` exists.
**Not on Windows:** host-KVM Juniper/Fortinet VMs and the Internet cloud (Linux only, greyed out); *Start CML VM* works
only through VMware Workstation's `vmrun` (set the CML `.vmx` under *Lab ▸ CML connection…*). LabDesk.exe is not
code-signed: SmartScreen → *More info* → *Run anyway*.

### Quick start

**New here? Follow [INSTALL.md](INSTALL.md)**: a step-by-step guide from `git clone` to a running window.

```bash
git clone https://github.com/eTheDestroyer/labdesk.git ~/labdesk
cd ~/labdesk && ./install.sh     # checks packages, installs, asks your CML login once, adds a menu entry
~/labdesk/labdesk                # or open "LabDesk" from the application menu
```

---

## Launch

* Application menu → **LabDesk** (`~/.local/share/applications/labdesk.desktop`), or
* `~/cml-labs/labdesk/labdesk [--lab "LabDesk Demo" | --lab-id <id>]`

The last opened lab is reopened automatically.

## Using it

| Action | How |
|---|---|
| New / open lab, import a template | toolbar **New lab**, **Open…** (Ctrl+O) → *CML labs* tab or *Templates* tab (`templates/*.yaml`, or any YAML file) |
| Add a device | drag it from the **Devices** palette onto the canvas (or right-click the canvas → *Add device here*). Names are generated automatically (R1, SW1, …) |
| Link two devices | **Shift-drag** from one device to another, or turn on **Link mode** (L). The next free interface is used on each side, and interfaces are created when needed |
| Console | **double-click**, **Enter**, or right-click → *Console*. A tab named after the node opens in the dedicated "LabDesk consoles" Konsole window. Leave the console with **Ctrl-]** |
| SSH / Telnet / HTTP(S) | right-click → *Management* (set the IP in *Properties…*; host VMs auto-detect theirs from the libvirt DHCP lease) |
| Start / stop / wipe | right-click menu, the inspector buttons, or toolbar *Start all / Stop all / Wipe all* |
| Startup config | right-click → *Edit startup config…*. It's applied on the first boot after a wipe; *Save, wipe & restart* does all three |
| Rename / delete / move | F2 / Del (asks for confirmation) / drag (positions are saved to CML) |
| Zoom / fit / pan | Ctrl+wheel, Ctrl +/-, F / middle-button or Alt-drag, wheel |
| Event log | Ctrl+J shows / hides it; filter *All / Warnings / Errors*, search with Ctrl+F (`node:R1` shows one node); right-click to copy, save or clear |
| Device login / live info | right-click → *Credentials…* (or Inspector ▸ *Credentials…*); rest the pointer on a device for its hover card (see below) |
| Help / About | the arrow on the toolbar **Legend** pill → *About LabDesk* (versions, *Copy diagnostics* - the CML host is left out unless you tick it) |

**Window layout.** The toolbar starts with the open lab's name (click it to open another lab) and its state
pill; on the right, **Probing**, **CML n/5** (running nodes; it turns amber with *limit* only when a start
would pass the CML-Free limit, red *CML offline* when the controller does not answer) and **Legend**. The
status bar shows the lab (`Switching Lab · 5/5 running`), the last poll, background work and the CML version
and address. With no lab open the canvas shows *Open lab… / From template… / New empty lab* and your recent
labs; with nothing selected the Inspector shows a live lab summary (click a red row to jump to the problem) and
a Tips card. *View ▸ Layout*: **Auto** switches to a compact layout on small screens (below about 1450×830:
icon-only toolbar groups, a 56 px device rail, a 340 px Inspector and the event log hidden behind Ctrl+J /
the status-bar error badge), **Compact** / **Full** force it. Dock layouts are remembered per layout.
Settings live in `~/.config/LabDesk/LabDesk.ini`. *Restore note:* if an earlier QA run changed your layer
choices or highlighted VLAN, re-pick them under *View ▸ Layers* (keys `layers/*` and `layers/vlan_id`);
QA tools now always use a temporary settings directory.

Node badge (top-right corner of each tile): green check = booted, grey spinner arc = booting, grey square =
stopped / wiped (the tile body is dimmed; its name stays readable), red ✕ = disconnected / missing.
*View ▸ Theme* picks Auto (follows the system), Light, Dark or KDE colours (your own palette and icons). The toolbar counter shows **running CML nodes x/5** across all labs
(CML-Free licence limit; unmanaged switches and external connectors don't count). If CML is
down, the status bar shows **Start CML VM**, which runs `virsh start cml` and waits for the API.

### Reading the canvas (live protocol view)

* **Links** - green = up on both ends. **Down is never colour-only**: every cable end carries a shape -
  filled circle = up, bar = down, hollow ring = line protocol down, filled square = admin down / not probed
  (readable in grayscale). *Admin down* is red dashed with a `✕ R2 Gi0/4 shut` pill; *link down* red,
  *line protocol down* orange dash-dot, *cable pulled* red with a gap, *unknown* (no fresh data) grey dotted.
* **Zoom levels** - zoomed far out you still see every alarm (stroke colour, end shapes, node badges, down
  pills, STP BLK caps); from 60 % zoom one bead per link and the protocol chips appear; from 90 % (or when
  you hover / select something) the subnet tags and interface names. Labels that would cover a more
  important one are hidden until you hover.
  Hover a link for a plain-English sentence; host-KVM links are a double line.
* **Point-to-point** - a dark `P2P` bead when OSPF really runs the link as point-to-point
  (`show ip ospf interface`), an outlined `P2P?` when only the config says so, and `BCAST` on a
  two-router link that still elects a DR/BDR. A /30 alone draws nothing (Inspector only).
* **OSPF / BGP / HSRP / VRRP** - a column of chips beside each router (`OSPF ✓`, `BGP 65000 ✓`; `…`
  converging, `!` configured but no neighbour; `0/0 ✕` = no default route), HSRP/VRRP role badges in the
  same column (filled = Active/Master),
  and `HSRP ✓` / `VRRP ✓` chips on the switch of that segment. *View ▸ Layers* adds OSPF/BGP
  adjacency rails, BGP session arcs and live traffic.
* **Default route to the internet = BLUE**: every hop of each router's default path, with `›`
  chevrons in the forwarding direction, ending at the `NAT egress` tile. A red ✕ marks where a
  path breaks. The outline chip (`O*E2`, `B* eBGP`, `S* dhcp`) says where each router learned it.
* **Simulate** (toolbar, or right-click): animate a packet along a default path, run a real
  traceroute and watch the hops, ping, *Verify Internet* (pings 8.8.8.8 and 1.1.1.1; egress turns `INTERNET ✓`), add
  delay/loss presets to a link or pull/plug its cable.
* **Tiles** - Cisco rounded square, Juniper hexagon, Fortinet shield; host-KVM VMs carry a `KVM` ribbon.
  A **managed Cisco switch** (IOSvL2) is a wide Cisco-blue front panel with an `L2` badge (`L3` when
  `ip routing` is on and it has two or more SVIs up) and **live port LEDs** (green forwarding, amber
  STP-blocked, red err-disabled, dark not connected / shut). An **unmanaged switch** is a plain grey
  box with a row of ports and no LEDs.
* **Switching (managed switches, layer *Switching*, on by default)**:
  * access links get a `V10` chip at the switch end (one colour per VLAN), trunks a `TRUNK 10,20` bead
    (allowed-and-active VLANs on both ends; the native VLAN is in its tooltip);
  * a port **blocked by spanning tree** gets a hatched amber end pill `Gi0/2 · Altn BLK`, a slashed amber
    cap at the tile edge and a muted grey dashed half - the link stays "up" (it is never drawn red or
    as down). A Designated/Root port that is only discarding while RSTP converges says so instead;
  * the root bridge carries a gold crown badge `ROOT v10,20`; every switch shows `L2 · 2 VLANs · RPVST+`;
  * *View ▸ Layers ▸ Highlight VLAN* (or the legend's VLAN row, which cycles VLANs) turns on **VLAN
    colouring**: links and ports carrying that VLAN get a halo in the VLAN's colour (dashed where STP
    blocks it), everything else is dimmed;
  * Inspector tabs for a switch: Overview (plain English, e.g. *"SW2: 2 VLANs, root for none (root is
    SW1), Gi0/2 blocking (Alternate) for VLAN 10,20 to prevent a loop"*), **VLANs**, **Ports**
    (mode / VLAN / STP role+state / status / neighbour), **Spanning Tree** (per VLAN: root ID, this
    bridge ID, root port and cost, topology changes, port roles) and **MAC** (counts per VLAN + dynamic
    entries);
  * event log: `STP` (role/state changes such as `SW2 Gi0/2 VLAN 10,20: Altn BLK → Root FWD`, root bridge
    changes, topology changes), `VLAN` (created / removed / renamed) and `TRUNK` (up / down / VLAN changes).
* **EtherChannel (LACP / PAgP / static)** - see "EtherChannel" below: the member cables of a Port-channel are
  drawn as one bundle (an oval around them, one bead `Po1 · LACP 2/2`), spanning tree blocking a whole
  Port-channel shows ONE hatched `Po2 · Altn BLK` marker, and a configuration that never bundles gets an amber
  `⚠ LACP passive ↔ passive — never bundles` bead.
* **Subnets on cables** (*View ▸ Layers ▸ Subnets on cables*, on by default): every routed cable shows its
  IPv4 subnet at the midpoint (`10.0.12.0/30`) and each end's host part next to the interface name
  (`Gi0/0 .1` … `Gi0/0 .2`). `½ 10.0.23.0/30` (dashed) = only one side has an address; an amber
  `⚠ mismatch 10.0.13.0/30 ≠ 10.0.31.0/30` = the two ends are in different subnets (Inspector explains why
  they cannot talk; an event is logged); `192.168.10.0/24 +1` = several subnets (secondaries, or a
  router-on-a-stick's subinterfaces - full list in the tooltip and Inspector); `dhcp 192.168.255.x/24`,
  `unnumbered`. A router ↔ switch link shows the router side's subnet; switch SVIs are only in the switch
  Inspector. Zoomed out the label shortens (`…12.0/30`), far out only a mismatch `⚠` remains. Labels of
  crossing / crowded links are placed so they never overlap (beside the cable if needed).
  **It follows what you type in an open console within about 1 s**: while a console is open LabDesk does not
  probe that node, so it reads CML's own console log over the REST API (a passive read, nothing is typed)
  and applies `ip address … [secondary]`, `no ip address`, `ip address dhcp`, `ip unnumbered`. When the
  console closes the node is probed at once. Each change logs `SUBNET R1 Gi0/2: 10.0.13.0/30 configured`
  / `… changed (was …)` / `… removed` and the label pulses. **Refresh** (F5) probes the selected node (all
  routers / switches if none is selected) - a node whose console is open gets a console-log read instead.
* **Legend** - bottom-left card (`?` key or toolbar `?`); it only lists what the open lab actually shows,
  scrolls instead of growing past the canvas, rows toggle layers, the header collapses it to a pill.
* **Inspector** - Overview in plain English, Interfaces (status as text), Routing, Protocols, Config
  (redacted); for a link: both ends, protocols, traffic sparkline and the simulation controls.
* QA: `QT_QPA_PLATFORM=offscreen .venv/bin/python tools/gui_shots.py --scene switch|proto|cloud|empty --theme
  light|dark [--size 1366x768]` writes the design-system shots `qa/gui-*.png` (+ `*-gray.png` copies), including
  the window shell (toolbar, status bar, event log, Inspector tabs at 360 / 340 px, idle summary, menus,
  notification bar) and, for `--scene empty`, the empty / offline states and the dialogs;
  `tools/ui_shots.py [--live] [--dark]` writes `qa/proto-*.png`;
  `tools/subnet_shots.py [--dark]` writes `qa/subnet-*.png` (replay), `tools/subnet_live_check.py` runs the live
  subnet check on "Switching Lab" (configures and reverts R1 / SW1 / SW2, writes `qa/subnet-live-*.png`).

### Device credentials & hover card

**Credentials.** Right-click a device (CML node or host-KVM VM) → **Credentials…**, or the Inspector's
*Credentials…* button: *Username*, *Password* and *Enable secret* (the eye shows / hides the text). *Use the
default login* edits the shared default instead (used by every device without its own values) and drops this
device's own. An empty field falls back to the default. The values go to `~/.config/labdesk/device-creds`
(`R1_USERNAME=` / `R1_PASSWORD=` / `R1_ENABLE=`, `DEFAULT_*`; written atomically, mode 0600, other lines and
comments kept) and nowhere else: not the event log, `state.json`, `LabDesk.ini`, CML or stdout. The probes use them
for a login prompt and for `enable`; probe logs and the event log remove them literally. Saving an enable secret
makes LabDesk read the running-config at the next CONFIG slot.

**Hover card.** Rest the pointer on a device tile (or its name) for ~0.4 s: a small card shows the name,
platform, state, management IP, how fresh the data is, up to 8 interfaces (address, up / down / shut;
`+N more`), the protocols LabDesk knows (OSPF neighbours, BGP peers, HSRP / VRRP role + VIP, default route,
spanning-tree root / blocked ports, VLANs, EtherChannel `Po1 LACP 2/2 up`) and two login blocks:

* **Login** - the LabDesk login you saved (`admin / Lab-R1-pass`, `enable cisco`), or *no credentials saved*.
* **On the device** - the accounts configured on the device itself, read from its running-config: `username`
  lines, `enable secret` / `enable password`, `line con` / `line vty` passwords (Junos `root` / login users and
  FortiOS admins are parsed too, for when the host-KVM collector is wired). An encrypted one says **encrypted**
  and the scheme (`type 5 MD5`, `type 7` *weak, reversible*, `type 8 PBKDF2-SHA-256`, `type 9 scrypt`; Junos
  `$6 SHA-512`; FortiOS `ENC`) - the hash itself is never shown or kept. A plain-text one (`password 0 x`,
  `enable password x`) is shown in clear, marked **plain text, not encrypted**. `service password-encryption`
  on / off is noted. Reading the running-config needs privilege 15: without an enable secret the card says
  *needs enable secret*. It rides the existing CONFIG tier (`show running-config`, after learning, after a
  `%SYS-5-CONFIG_I` and every 10 min): no extra console command.

*View ▸ Show passwords on hover* (on by default) masks every password on the card as `••••••` when off. The card
never takes focus or blocks clicks / double-click, hides on click, drag, wheel / zoom and pan, and updates while
it is open. The Inspector only says *password saved*, never the value. QA: `QT_QPA_PLATFORM=offscreen
.venv/bin/python tools/hovercard_shots.py --theme light|dark` writes `qa/hover-card/*.png` (temp settings and
creds; replay only). Live, read-only: `.venv/bin/python tools/accounts_live_check.py --nodes R1,SW1
--enable-prompt` runs the CONFIG tier's burst on a running "Switching Lab" (gated next to a running LabDesk) and
prints how each account is stored (seen live: R1 IOSv `type 9 scrypt`, SW1 IOSvL2 `type 5 MD5`).

### CML gotchas handled

* A node's interfaces are **locked while it has a disk**. If a link needs a new interface on such a
  node, LabDesk offers *stop + wipe + retry*.
* A link added to an already-booted node usually stays down inside the VM. LabDesk offers
  *wipe & restart* of that node.
* Starting a node while 5 are already running is refused with a clear message.

### Host-KVM devices (Juniper vJunos-router, Fortinet FortiOS)

* Built with `virt-install` on `qemu:///system`. Each image is uploaded **once** into the pool
  `images` as a base volume, and every VM gets a thin qcow2 overlay.
  vJunos: 4 vCPU / 5 GB, host-passthrough CPU, Juniper SMBIOS, NIC1 = fxp0, then ge-0/0/0…
  FortiOS: 1 vCPU / 2 GB + 2 GB log disk, NIC1 = port1, then port2…
* LabDesk warns before starting vJunos when `MemAvailable` < 6 GB.
* **FortiOS is the unlicensed FortiFirewall VM**: routing and OSPF work, but it does **not forward
  traffic**.
* Console: `virsh -c qemu:///system console <vm> --force` (Ctrl-] to leave).
* VM ↔ VM link: an isolated libvirt network `labdesk-<a>-<b>` (no IP, no forwarding).
* VM ↔ CML node link: LabDesk creates a CML **System Bridge** external connector `BR-<vm>`, links
  the CML node to it, and adds a VM NIC on libvirt `default` (virbr0). All System-Bridge links
  share **one L2 segment** (192.168.122.0/24).
* New NICs usually appear in the guest only after a VM reboot.
* These devices are stored per lab in `~/.config/labdesk/state.json`, along with the management IPs.

## Internet cloud (always answers 8.8.8.8 / 1.1.1.1)

Drag **Connectors ▸ Internet cloud (always answers 8.8.8.8 / 1.1.1.1)** onto the canvas, Shift-drag a link
from a router to it and accept **Auto-configure**. The router and every host behind it then get ping replies from
**8.8.8.8, 1.1.1.1** (also 9.9.9.9 and 208.67.222.222) every time, even when this PC has no Internet at all. It
costs **no CML node**: the cloud is an external connector, and the "Internet" lives in a network namespace on this host.

```
 PC1 192.168.10.10 ─ SW1 ─ R1 Gi0/0.10            R1 Gi0/1 100.64.0.2/24 ── INTERNET (CML external_connector,
                                                    │                       "System Bridge" = bridge0 in the CML VM
   default via 100.64.0.1 (blue on the canvas)      │                       = the host's virbr0)
                                                    ▼
 host: virbr0 ── labcloud-br ══veth══ cloud0 100.64.0.1/24   [netns labcloud]
                                      lo 8.8.8.8/32 1.1.1.1/32 9.9.9.9/32 208.67.222.222/32
                                      return routes 10/8, 172.16/12, 192.168/16 via 100.64.0.2 (EDGE_GW)
                                      ip_forward=0, icmp_echo_ignore_all=0: an endpoint, not a router
```

**The host is not changed otherwise.** 8.8.8.8 and 1.1.1.1 exist only inside `labcloud`; the host's own routes, DNS
and real Internet stay as they were (`ip route get 8.8.8.8` still goes `via 192.168.1.1 dev wlo1`). The only host
route added is `100.64.0.1/32 dev virbr0`, so LabDesk can health-check the cloud.

### What is installed (once, as root: `sudo host/labdesk-cloud/install.sh`)

| file | what |
|---|---|
| `/etc/labdesk-cloud.conf` | root-owned settings: `NS`, `BRIDGE`, `CLOUD_ADDR=100.64.0.1/24`, `CLOUD_MAC`, `ANSWER_IPS`, `RETURN_PREFIXES`, default `EDGE_GW=100.64.0.2` |
| `/usr/local/libexec/labdesk-cloud/{common.sh,up,down,check}` | idempotent setup / teardown / 10 s check (parse the config files, never source them) |
| `labdesk-cloud.service` | `up` / `down` (oneshot, RemainAfterExit; `After=libvirtd virtnetworkd`; waits up to 90 s for virbr0). Enabled at boot |
| `labdesk-cloud-check.timer` → `labdesk-cloud-check.service` | every 10 s: repairs the namespace / re-attaches the veth (e.g. after the libvirt default network restarts), re-applies the return routes, pings every answer address **from inside the namespace** and writes `/run/labdesk-cloud/status` (world-readable). Quiet in the journal (`LogLevelMax=notice`) |
| `labdesk-cloud-routes.path` | re-runs the check as soon as `/var/lib/labdesk-cloud/routes.conf` changes |
| `/var/lib/labdesk-cloud/` | owned by **you** (`lf`): LabDesk writes `routes.conf` there |
| `/etc/libvirt/hooks/network` | a block between `# >>> labdesk-cloud` and `# <<< labdesk-cloud` re-runs `up` when the `default` network is `started` (the existing 10.0.99.1/29 behaviour is unchanged; backup in `network.pre-labdesk-cloud`) |

Timer and path unit are `PartOf=labdesk-cloud.service`: stopping the cloud stops them too.

**Return path without a password.** Replies to hosts behind a router (PC1 192.168.10.10) must go back to that router.
The namespace routes 10/8, 172.16/12 and 192.168/16 to `EDGE_GW` - the first router that was auto-configured. LabDesk
writes `EDGE_GW=100.64.0.2` (and `ROUTE <prefix> <router>` for more edge routers) into the user-owned
`/var/lib/labdesk-cloud/routes.conf`; the root path unit applies it within a second. The root helper validates the
file strictly (gateway must be a router address in 100.64.0.0/24, prefixes must be private lab space, at most 64
lines, unknown lines ignored), and it can only ever change routes **inside the namespace**, so no sudo/polkit rule is
needed at runtime. To set it by hand: `printf 'EDGE_GW=100.64.0.2\n' > /var/lib/labdesk-cloud/routes.conf`.

**Several edge routers.** The first one becomes `EDGE_GW`; every further router gets per-prefix routes for its own
private LANs (`ROUTE 192.168.30.0/24 100.64.0.3`; more specific wins). Overlapping LANs on two edge routers can't
both work, the last router configured wins. Labs share one cloud, so LabDesk picks addresses that are unused across
**all** labs' day-0 configs.

### In LabDesk

* **Palette ▸ Connectors**: the sky-blue cloud with a globe (the grey NAT / System-Bridge clouds are the plain
  connectors). Dropping it creates a CML `external_connector` in **System Bridge** mode, label `INTERNET`, CML node
  tag `labdesk-cloud` (the marker travels with the lab, also through YAML export).
* **Canvas**: the cloud tile, a `8.8.8.8 · 1.1.1.1` pill and a **health dot** (green up, amber degraded, red down).
  Hover for `cloud up — answers 8.8.8.8, 1.1.1.1`. When down: a red `cloud DOWN` pill, and the tooltip, the Inspector
  and the event log say **`sudo systemctl start labdesk-cloud`** (right-click → *Copy fix command*). Health is checked
  every 10 s while the lab has a cloud: `/run/netns/labcloud` exists, `ping 100.64.0.1` from the host answers, and the
  status file (namespace self-ping of every answer address, ≤ 45 s old).
* **Auto-configure for Internet cloud** (offered right after you link a router to the cloud; also right-click the
  router, the cloud or the cable): picks the next free 100.64.0.x and shows the exact commands first. *Apply*:
  1. console (only if you leave it ticked; if the router's console is open LabDesk asks first, and a console found in
     config mode / at `--More--` is left alone): `enable` with secret `cisco`, `interface Gi0/1 / ip address
     100.64.0.2 255.255.255.0 / no shutdown`, `ip route 0.0.0.0 0.0.0.0 100.64.0.1`, the EEM applet below, optionally
     `router ospf N / default-information originate`, `end`, `write memory`; then a FAST probe so the canvas updates;
  2. the CML **day-0 config** gets the same lines. CML locks a booted node's config, so LabDesk stores the patch and
     writes it into CML at the next wipe it does (Wipe / Wipe & restart / Wipe all);
  3. `routes.conf` (return path, see above);
  4. **Verify Internet** runs by itself afterwards.
  If the new cable is not wired into the running router yet (CML connects a cable added after the node's first boot
  only after a wipe; the interface stays down/down), the dialog pre-selects **Wipe & restart**.
* The default route toward the cloud draws **blue**, the cable label shows `100.64.0.0/24` (`Gi0/1 .2`), and
  **Simulate ▸ Verify Internet** pings **8.8.8.8 and 1.1.1.1** (one console burst) and turns the egress into
  `INTERNET ✓ hh:mm`.

**IOSv quirk (handled):** after a wipe NVRAM is empty, so IOS AutoInstall DHCPs on every connected interface while
CML loads the day-0 config. On the System Bridge libvirt's dnsmasq answers, and the uplink ends up as
`ip address 192.168.122.x` instead of 100.64.0.2 (seen live). The EEM applet `LABDESK-CLOUD-UPLINK` re-applies the
static address when `%CVAC-4-CONFIG_DONE` / `%PNP-6-PNP_DISCOVERY_STOPPED` is logged (about 3 min after boot).

### Check it by hand

```
systemctl status labdesk-cloud labdesk-cloud-check.timer labdesk-cloud-routes.path
cat /run/labdesk-cloud/status                 # state=up, selfping=..., edge_gw=..., routes=...
ping -c1 100.64.0.1                           # from the host
ip route get 8.8.8.8                          # unchanged: via your real gateway
sudo ip netns exec labcloud tcpdump -ni cloud0 icmp      # replies really come from the namespace
.venv/bin/python tools/cloud_live_check.py --verify --pc PC1 --ping-pc2   # R1 + PC1 pings, qa/cloud_live_log.txt
QT_QPA_PLATFORM=offscreen .venv/bin/python tools/cloud_shots.py [--verify] [--dark]  # qa/cloud-*.png
```

### Stop or remove it

* Stop now and at boot: `sudo systemctl disable --now labdesk-cloud` (removes the namespace, the veth and the
  `100.64.0.1/32` host route; timer and path unit stop with it).
* Remove everything: `sudo host/labdesk-cloud/uninstall.sh`, or by hand: the command above, then delete
  `/etc/systemd/system/labdesk-cloud.service`, `labdesk-cloud-check.service`, `labdesk-cloud-check.timer`,
  `labdesk-cloud-routes.path` (`sudo systemctl daemon-reload`), `/usr/local/libexec/labdesk-cloud/`,
  `/etc/labdesk-cloud.conf`, `/var/lib/labdesk-cloud/`, `/run/labdesk-cloud/`, and the `# >>> labdesk-cloud` …
  `# <<< labdesk-cloud` block in `/etc/libvirt/hooks/network`.

### Limitations

* ICMP echo only: no DNS, HTTP or TCP service behind 8.8.8.8 / 1.1.1.1 (TCP gets a reset). A traceroute from the
  edge router shows one hop: 8.8.8.8 itself.
* 100.64.0.0/24 (CGNAT space) must not collide with something on this host (e.g. Tailscale uses 100.64.0.0/10);
  change `CLOUD_ADDR` in `/etc/labdesk-cloud.conf` if it does.
* All System-Bridge links share one L2 segment (virbr0): a host-KVM VM on a System Bridge sees the cloud too.
* One return path per prefix: overlapping LANs behind two edge routers can't both be answered.
* Auto-configure covers Cisco IOS / IOS-XE routers; others need the two lines by hand
  (`ip address 100.64.0.N/24`, default route via 100.64.0.1) plus a `ROUTE` line in `routes.conf`.

## How consoles work

* **CML nodes:** `ssh -t -e none -i ~/.ssh/labdesk_ed25519 admin@192.168.122.60 connect <console-uuid>`.
  The CML terminal server accepts the command non-interactively, so the tab lands directly on the
  router console. `-e none` disables ssh's `~` escapes, so every byte goes through untouched.
* **Passwordless:** SSH public-key auth is enabled on the CML terminal server, and the key
  `~/.ssh/labdesk_ed25519.pub` is registered for the CML `admin` user. As a fallback, the tab also
  carries `SSH_ASKPASS=labdesk_app/askpass.py`, which reads the password from
  `~/cml-labs/cml-credentials.env`. The password never appears on a command line, in Konsole, or in
  logs.
* **Windows (LabDesk.exe):** the same `ssh -t -e none … connect <console-uuid>` with the built-in OpenSSH client
  (`C:\Windows\System32\OpenSSH\ssh.exe`), in a tab of one Windows Terminal window named *LabDesk*
  (`wt -w LabDesk new-tab --title <node> …`), or a new console window when Windows Terminal is missing. There is no
  askpass helper: without the SSH key, ssh asks for the CML password in the console. Background probes use
  **paramiko** in-process (same `connect <uuid>` session on a 250×50 pty; SSH key if present, else the saved CML
  password - never on a command line); open consoles are found with **psutil** so probing pauses for them, as on Linux.
  Credentials: `%APPDATA%\LabDesk\cml-credentials.env` (`CML_CRED_FILE` overrides), settings
  `%APPDATA%\LabDesk\LabDesk.ini`, state and device logins in `%APPDATA%\LabDesk`, cache and probe logs in
  `%LOCALAPPDATA%\LabDesk\cache` (crash log: `labdesk.log` there).
* **Konsole:** LabDesk starts one `konsole --separate --profile LabDesk` window and adds a tab per
  device over D-Bus (`newSession`, `setTabTitleFormat`, `runCommand`). The `LabDesk` profile
  (`~/.local/share/konsole/LabDesk.profile`) doesn't start a shell. Its command is
  `labdesk_app/tabexec.py`, which reads one JSON line (argv) with echo off and runs that argv on the
  tab's pty. It accepts only `ssh`/`virsh`/`telnet`, does no shell parsing, and adds nothing to your
  shell history. If the command fails, the tab stays open and shows the error. Without D-Bus, LabDesk
  falls back to a new `konsole -e …` window, or xterm.

### CML controller changes made during setup

1. Cockpit → CML2 → *SSH Terminal Server Public Key Authentication* was enabled
   (`manage-compute.sh --set-defaults sshPubkey on`, see `tools/cockpit_exec.py`).
2. **CML 2.10.1 bug workaround:** `virl2-sshd` runs as `www-data` with `Group=virl2`, so it can't
   read `/var/local/virl2/secrets/compute_secret` (`virl2:www-data 0640`). It then logs "SSH
   public key authentication disabled". A systemd drop-in on the controller fixes this:
   `/etc/systemd/system/virl2-sshd.service.d/labdesk-pubkey.conf` → `SupplementaryGroups=www-data`.
   To revert: delete that file, run `systemctl daemon-reload`, then `systemctl restart virl2-sshd`.
   A CML upgrade may need it re-applied.
3. The public key was set on the CML `admin` user (`PATCH /api/v0/users/<id> {"pubkey": …}`).

## Host network note

`virbr0` still carries a leftover alias `10.0.99.1/29`, with routes via `10.0.99.2`, from an older
lab (libvirt hook `/etc/libvirt/hooks/network`). It's harmless, and LabDesk doesn't depend on it.
The Internet cloud adds only `100.64.0.1/32 dev virbr0` and the `labcloud-br` bridge port (see "Internet cloud").

## Layout

```
labdesk                 launcher (runs .venv/bin/python -m labdesk_app)
labdesk.svg             app icon
labdesk_app/
  cloud.py              Internet cloud: health, address allocation, router/day-0 config, return routes file
  cml_api.py            CML REST wrapper (token auth, re-auth on 401, next free interface, links…)
  controller.py         all lab operations (UI independent, used by the UI and tools/e2e_demo.py)
  kvm.py                virsh / virt-install command builders and runners
  launchers.py          console / ssh / telnet / http command builders, Konsole D-Bus launcher, Windows Terminal launcher
  plat.py               platform layer: Linux vs Windows paths, tools (ssh.exe, wt.exe, vmrun) and Linux-only features
  tabexec.py, askpass.py  Konsole tab bootstrap, SSH_ASKPASS helper
  catalog.py, validate.py, config.py
  parsers/ios_l2.py     IOSvL2 switching parsers (VLANs, trunks, STP, MAC, CDP, EtherChannel, LACP, PAgP)
  etherchannel.py       EtherChannel mode rules, IOS command generation, day-0 patching (pure)
  accounts.py           on-device accounts from IOS / Junos / FortiOS configs: encrypted (scheme) or plain text (pure)
  probe/creds.py        device-creds file: per-node / default login, atomic 0600 writer
  ui/                   main_window, canvas, graphics (vector vendor icons), dialogs, hovercard (+ hovercard_model), worker (thread pool)
templates/              3-router-hsrp.yaml, hub-spoke-hsrp-bgp.yaml, switching-lab-etherchannel.yaml
host/labdesk-cloud/     the Internet cloud's host side: install.sh / uninstall.sh, libexec scripts, systemd units
tests/                  pytest: API wrapper with a fake HTTP transport, command builders, helpers
                        (@pytest.mark.linux_only tests are skipped on Windows)
labdesk.spec, packaging/  PyInstaller build of LabDesk.exe (entry script, labdesk.ico from tools/make_ico.py)
.github/workflows/windows.yml  CI: Linux + Windows tests, LabDesk.exe build + smoke test, Release on v* tags
tools/                  e2e_demo.py (live test), screenshots.py / ui_shots.py / switch_shots.py (offscreen QA),
                        build_switching_lab.py, switch_failover.py, telemetry_snapshot.py, cockpit_exec.py,
                        cloud_live_check.py / cloud_shots.py (Internet cloud live check, qa/cloud-*.png)
qa/                     screenshots and e2e_log.txt
```

All REST, virsh and D-Bus work runs on a thread pool, so the UI never blocks. State is polled every 3 s.
Subprocesses always get argv lists (never `shell=True`), and labels/VM names are validated.

## Development

```
.venv/bin/python -m pytest -q tests
pyinstaller --noconfirm --clean labdesk.spec       # on Windows: dist\LabDesk.exe (CI does this on every push)
QT_QPA_PLATFORM=offscreen .venv/bin/python tools/screenshots.py --lab "LabDesk Demo" --select R1 --dialogs
QT_QPA_PLATFORM=offscreen .venv/bin/python tools/e2e_demo.py      # live: needs CML
```

## Limitations

* CML-Free allows only 5 running nodes.
* LabDesk for Windows: no host-KVM devices and no Internet cloud (Linux only); consoles ask for the CML password
  unless the SSH key is set up; *Start CML VM* needs VMware Workstation. The .exe is unsigned (SmartScreen warning).
* Host-VM links via the System Bridge all share one broadcast domain. Removing a host-VM link
  detaches its NIC, but the CML connector node stays in place, so delete it yourself if you want it gone.
* Host-KVM VMs can't be renamed, because the name is the libvirt domain name.
* Startup-config edits are applied by CML only after a wipe.
* Switching: PVST+/Rapid-PVST+ are tested live; MST instances are parsed (`MST0`) but untested. In CML,
  shutting one end of a link does not take the far end's virtual NIC down, so the far switch port keeps
  "up" and briefly shows Designated/discarding. The `V10` chip, `TRUNK` bead and STP marker come only from
  managed-switch ends. A router subinterface trunk (R1 Gi0/0.10/.20) is shown from the switch side.

## Switching Lab (managed switches)

`tools/build_switching_lab.py` builds the lab "Switching Lab" through the controller (5 counted nodes):

```
            R1 (IOSv)  router-on-a-stick: Gi0/0.10 192.168.10.1/24, Gi0/0.20 192.168.20.1/24
             | Gi0/0  dot1q trunk 10,20
           Gi0/0
   PC1 --Gi0/3 SW1 Gi0/1+0/2 == Po1 LACP == Gi0/1+0/2 SW2 Gi0/0-- PC2    SW1/SW2: IOSvL2, rapid-pvst,
 VLAN 10       Gi1/0+1/1 == Po2 PAgP == Gi1/0+1/1 (Po2 Altn BLK)  VLAN 20   VLAN 10 USERS / 20 SERVERS,
 .10.10/24                                          .20.10/24  SW1 priority 4096 (root for 10,20)
```

R1 Gi0/1 100.64.0.2/24 is cabled to the **INTERNET** cloud (System Bridge), default route via 100.64.0.1: R1, PC1 and
PC2 ping 8.8.8.8 / 1.1.1.1 (see "Internet cloud").

SVIs: SW1 Vlan10 192.168.10.2, SW2 Vlan10 192.168.10.3. Trunks (Gi0/0 to R1, Port-channel1 and Port-channel2)
allow 10,20 (native 1, `nonegotiate`). See "EtherChannel" for Po1 / Po2. Template:
`templates/switching-lab-etherchannel.yaml` (CML export of the lab, with the day-0 configs).
Day-0 configs are stored in CML. `--reconfigure SW1 SW2` wipes those nodes and stores the current configs.

**IOSvL2 quirk:** an SVI from the day-0 config stays down/down after boot even though its VLAN has
forwarding ports, until you do `shutdown` / `no shutdown`. The day-0 configs include a one-shot EEM applet
`LABDESK-SVI-KICK` that bounces `Vlan10` 60 s after boot.

Probing a switch uses the same console etiquette as for routers. Every L2 command works at the user-exec
`>` prompt (verified live on iosvl2-2020): `show vlan brief`, `show interfaces trunk`,
`show interfaces status`, `show spanning-tree [summary|detail]`, `show mac address-table count|dynamic`,
`show cdp neighbors` and `show etherchannel summary`. Fixtures are in `tests/fixtures/switching/` (real
captures, plus `after_shut_*` taken after `shutdown` on SW1 Gi0/1). The capture tools are in `_tools/`.

QA:

```
QT_QPA_PLATFORM=offscreen .venv/bin/python tools/switch_shots.py [--replay]   # qa/switch-*.png
QT_QPA_PLATFORM=offscreen .venv/bin/python tools/switch_failover.py           # live STP fail-over check
.venv/bin/python tools/telemetry_snapshot.py --lab "Switching Lab"
```

Only one lab fits the CML-Free licence at a time. Stop "Protocol Showcase" before you start
"Switching Lab", and the other way round.

## EtherChannel (LACP / PAgP / static on)

The Switching Lab has two parallel port-channels between SW1 and SW2:

```
            Po1  LACP   SW1 Gi0/1+Gi0/2 (active)    ===  SW2 Gi0/1+Gi0/2 (passive)     trunk 10,20
            Po2  PAgP   SW1 Gi1/0+Gi1/1 (desirable) ===  SW2 Gi1/0+Gi1/1 (auto)        trunk 10,20 (SW2 Po2 Altn BLK)
            port-channel load-balance src-dst-ip on both switches
```

Spanning tree sees each Port-channel as ONE port: SW2's root port is Po1 and **Po2 is blocked as a whole**
(`Po2 Altn BLK`, both members stay bundled).  Shut a Po1 member and Po1 runs on 1 of 2 links (cost 4 > 3: STP moves
the block to Po1); stop both Po1 cables and Po2 takes over as root port.

**On the canvas** (layer *Switching*):

| what | how it looks |
|---|---|
| a bundle | the member cables run close together through an oval; one bead `Po1 · LACP 2/2` beside it. The protocol is told by text **and** shape: LACP = rounded bead + solid oval, PAgP = bead with pointed ends + dashed oval, static `mode on` = square outlined bead + dotted oval |
| zoom | the bead from 60 % zoom; `TRUNK 10,20`, and the member ports as one pill per end (`Gi0/1 · Gi0/2`) from 90 % (hover a cable for its own pill); a mismatch and a down / partial bundle at every zoom |
| member state | each member keeps its end shapes (circle up, bar down, ring line-protocol down, square admin down). A member that is not bundled is drawn muted and dashed with a ring badge carrying its IOS flag: `s` suspended, `I` stand-alone, `D` down, `w` waiting, `H` hot-standby (hover: `s · suspended`) |
| partly bundled | amber bead `Po1 · LACP 1/2` |
| down | red bead `Po1 · LACP 0/2` |
| mismatch | an amber `⚠` bead with the short reason (`LACP passive ↔ passive — never bundles`, `on ↔ LACP active — never bundles`, `LACP ↔ PAgP — never bundles`, `SW2 has no channel-group — never bundles`, `SW1 members differ — trunk vs access VLAN 10`), the plain-English explanation in the Inspector and an `ETHERCHANNEL` event |
| STP blocks the Po | ONE hatched `Po2 · Altn BLK` pill + slashed amber cap at the blocking end of the bundle (not one per member); the blocked halves of the members are muted-dashed |

Click the oval or the bead → **Inspector ▸ EtherChannel**: Po id, protocol and the modes of both sides, LACP system
id, load-balance, STP role/state of each Port-channel, trunk VLANs, the reason it does not bundle, and a member
table (cable, state in words, partner from LACP / PAgP). A switch has an **EtherChannel** tab (one card per
Port-channel: flags in words, mode, STP, trunk, members with partner port; *Show on canvas*). The Ports tab shows a
bundled member as `trunk · Po2` with `via Po2: Altn BLK`, and the front-panel LEDs follow the Port-channel.
Legend: an *EtherChannel* section appears when the lab has bundles.

**Create a bundle.** Ctrl-click two or more parallel cables between the same two switches (or just right-click one:
all free parallel cables are taken) → **Bundle into EtherChannel…**: protocol LACP / PAgP / Static on, the mode of
each side (defaults LACP active/active, PAgP desirable/desirable) with an inline warning for combinations that never
form (passive/passive, auto/auto), the Port-channel number (next free on both switches), *copy the members' current
trunk/access config onto the Port-channel*, optional `port-channel load-balance`, and the exact commands for both
switches. *Apply* types them through the console path (per-node lock, the usual etiquette; LabDesk asks before
typing on a console that is open on this host), saves with `write memory`, stores the change in the CML day-0 configs
(written at the next wipe while the node runs: CML locks a booted node's config) and watches until the bundle forms
(event log + notification). A bundle's right-click menu (or the Inspector) offers **Unbundle…** (`no channel-group`
on the members, `no interface Port-channelN`).

**Probing.** All EtherChannel commands work at `>` (verified on IOSvL2): once a channel-group exists, `show
etherchannel summary` runs every 15 s (FAST) and `show lacp internal|neighbor|sys-id`, `show pagp internal|neighbor`
(only the protocols in use) and `show etherchannel load-balance` every 60 s. `%EC-5-*` syslog lines trigger an
immediate re-read and give the reason a member was suspended. LACP partners are matched to lab nodes through the
partner's `show lacp sys-id` (the LACP system MAC differs from the bridge MAC) and port number, PAgP partners by name,
otherwise CDP.

**Fixtures / QA.** Real captures in `tests/fixtures/etherchannel/` (`healthy`, `partial` = SW1 Gi0/2 shut,
`fail_on` = SW2 Po1 `mode on`, `fail_proto` = SW2 Po1 PAgP, `fail_passive` = both LACP passive), capture tool
`tests/fixtures/etherchannel/_tools/capture.py`, replay `tests/ec_replay.py`, tests `tests/test_etherchannel.py`.

```
QT_QPA_PLATFORM=offscreen .venv/bin/python tools/ec_shots.py --theme light|dark [--size 1366x768]   # qa/ec-*.png
QT_QPA_PLATFORM=offscreen .venv/bin/python tools/ec_live_check.py   # live: shut / mismatch / cable stop / dialog
QT_QPA_PLATFORM=offscreen .venv/bin/python tools/ec_live_flow.py [--bundle-only]   # live Unbundle + Bundle dialog,
      # safe next to a running LabDesk (mirrors its probe logs, waits for a gap in its probes, skips user consoles)
```

IOSvL2 notes (seen live): `no channel-group 1` answers `% Incomplete command` (use `no channel-group`); a single
member of an LACP group cannot be set to `mode on` ("already part of a channel with a different type of protocol"),
the whole group has to be re-created; in CML the far end of a shut member keeps carrier, so the far switch suspends
its port only after the LACP timeout (~90 s) - LabDesk shows `1/2` as soon as the near switch reports it. A stopped CML
cable never counts as bundled, whatever the switches still report. With bundles present a switch's FAST burst takes
~14 s instead of ~10.5 s (the summary moves to FAST), and FAST + SLOW ~29 s (burst timeout raised by 10 s).

## Live telemetry (data layer)

LabDesk reads protocol state from the routers and managed switches themselves: link up/down per side,
VLANs, trunks and spanning tree, P2P vs broadcast
OSPF, OSPF/BGP adjacencies, HSRP/VRRP roles and the default-route path. It also simulates links
(cable pull, latency/loss conditioning, ping/traceroute/Verify Internet). The model, cadence and
console etiquette are documented in `docs/telemetry_model.md`. To see a live lab from the
terminal, run `.venv/bin/python tools/telemetry_snapshot.py --lab "Protocol Showcase"`.

Probing uses short read-only bursts on the CML console line. Probing pauses while a console
for that node is open; meanwhile interface addresses typed in that console are read from CML's console log
(REST, passive) so the subnet labels stay live. Optional logins and enable secrets go in
`~/.config/labdesk/device-creds` (mode 0600; right-click ▸ *Credentials…* writes it).
