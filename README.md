# LabDesk

**A native desktop app for Linux and Windows for designing, running and watching Cisco CML network labs, with real-tty consoles and a live protocol map.**

LabDesk is a Python 3.13 + PySide6 front end for **Cisco Modeling Labs (CML-Free 2.10)** running on KVM. It also handles
host-KVM **Juniper vJunos** and **Fortinet FortiOS** VMs. Drag routers and switches onto a canvas, cable them up and boot them.
Double-click any device to get a **real console as a tab of the LabDesk console window** (neon green on green-black, one tab
per device), where Tab completion, `?`, Ctrl-C and the arrow keys work as on physical hardware, with no browser or websocket lag.

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

> Requires Linux (KDE recommended; Konsole is the fallback console), libvirt/KVM and a CML-Free VM. Device images (IOSv, IOSvL2, vJunos, FortiOS) are
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
| Console | **double-click**, **Enter**, or right-click → *Console*. A tab named after the node opens in the **LabDesk console** window (opening the same device again switches to its tab). Leave the console with **Ctrl-]** |
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

### Where the data comes from (live only)

Everything LabDesk shows **about a device** comes from **live output of that device**:
* console probe bursts (`show …` at `>`);
* the device's own console transcript, while you type;
* the running-config read from the device;
* for host-KVM vJunos, read-only SSH `show` commands to its fxp0.

That covers interface names and IPs, subnets on cables, link up/down, OSPF / BGP / HSRP / VRRP / STP / VLAN /
trunk / EtherChannel chips, routes, the blue default path, router-id, AS numbers and the accounts *On the
device*.

Nothing is taken from the lab YAML, the CML day-0 / startup config, templates, CML's intended interface data,
CML's L3 address snooping, or a guess. When a device has not been read (stopped, not probed yet, probe failed,
no collector), it says **not read yet** / **state unknown**. When the last read is old, it shows its age
(*Live data: stale · last read 3 min ago*). An interface pill drawn **dashed** is CML's wiring label that the
device has not confirmed yet.

These are not device data, and they are labelled as such:
* CML's / libvirt's runtime state (**CML: Booted**, **VM: Running**, link started);
* your layout;
* the management IP and logins LabDesk saved (**saved in LabDesk**);
* the defaults LabDesk will *try* (**LabDesk will try: admin / cisco**, *default, not read from device*).

The full field-by-field audit is in [`docs/live-data-audit.md`](docs/live-data-audit.md).

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
  and `HSRP ✓` / `VRRP ✓` chips on the switch of that segment. A BGP chip on a **switch** names the
  routers whose session crosses that LAN - `iBGP R1↔R2` (same AS), `eBGP A↔B` (different AS), several
  sessions `iBGP ×2` (hover lists them): the switch itself never runs BGP. *View ▸ Layers* adds OSPF/BGP
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

* **Login** - what LabDesk has **saved** (`Saved login  admin / Lab-R1-pass`, `enable cisco`) or the default it **will
  try** (`LabDesk will try  admin / cisco`, *default, not read from device*), or *no credentials saved*. It is never
  something the device reported.
* **On the device** - the accounts configured on the device itself, read from its running-config: `username`
  lines, `enable secret` / `enable password`, `line con` / `line vty` passwords (Junos `root` / login users and
  FortiOS admins are parsed too; vJunos accounts are read live over SSH). An encrypted one says **encrypted**
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

**First logins.** You never have to look a login up: every device shows the login to use the first time.
LabDesk writes each node's first login into `device-creds` itself (per node, `R1_USERNAME=` … plus
`R1_SOURCE=first-login`; mode 0600, the same atomic writer) when it creates the node (palette drop, template import,
build) and, on lab open, for every existing node that has no entry yet - it never overwrites a login you saved, and
a field you set as the shared default (`DEFAULT_*`) stays yours. The probes use these values, so login, `enable`
and the *On the device* accounts work out of the box. Where it shows:

* **Hover card ▸ Login**: `LabDesk will try  admin / cisco` + `enable  cisco`, tagged **default** (first-login
  value, *not read from device*) or `Saved login …` tagged **saved** (you edited it), with a one-line hint (e.g. *the console has no login: press Enter, then 'enable'*).
  *View ▸ Show passwords on hover* masks it like every other password.
* **Inspector ▸ Overview**: a *Login* row (never the password) and *User* / *Password* copy buttons.
* **Right-click**: *Copy login* (user name), *Copy password*.
* **First console of a node in a session**: a toast and an event-log line, e.g.
  `R1 first login: admin / cisco (enable: cisco) · console: no username, press Enter, then 'enable'`. A password
  that comes from `cml-credentials.env` or that you saved is masked in the event log. The console tab itself is not
  touched (the terminal shows device output only).
* **Credentials… ▸ Reset to default** puts the first-login value back.

The node's own day-0 config wins over the table: `username … secret|password …` / `enable secret|password …` lines
(plain text only - a hashed one shows *set in config (encrypted)*), `line con 0` with or without `login`, alpine
`USERNAME=` / `PASSWORD=`, Ubuntu cloud-init `default_user` / `password:`. The table (`labdesk_app/default_logins.py`):

| Device | First login | enable | Source |
|---|---|---|---|
| Cisco IOSv / IOSvL2 / IOL-XE created by LabDesk | console: none (press Enter); SSH / telnet: `admin` / `cisco` | `cisco` | LabDesk day-0 config (`catalog.IOS_BASE_CONFIG`) |
| Cisco node with no login in its day-0 config (blank IOSv) | none - press Enter | no password until you configure one | IOS default |
| Alpine / Desktop / Ubuntu (CML) | `cisco` / `cisco` | - | CML node definition ("cisco/cisco by default") |
| Tiny Core (CML `server`) | `cisco` / `cisco` (user `tc`: no password) | - | CML node definition |
| Juniper vJunos (host KVM) | `root`, no password at the console (amnesiac): type `cli`, then `set system root-authentication plain-text-password` | - | vJunos factory default (LabDesk sets nothing); `JUNOS_ADMIN_USER` / `JUNOS_ADMIN_PASS` from `cml-credentials.env` when set |
| FortiOS (host KVM) | `admin`, empty password - you must set a new one at first login | - | FortiOS factory default; `FGT_ADMIN_USER` / `FGT_ADMIN_PASS` when set |
| Unmanaged switch, NAT / bridge connector, Internet cloud | no console, no login | - | - |

Live QA (read-only; real `device-creds`, temp settings): `QT_QPA_PLATFORM=offscreen .venv/bin/python
tools/first_login_live.py --lab "PE Hub-Spoke HSRP" --nodes R1,PC-DATA,PE` backfills, waits for the probes and
writes `qa/first-login/*.png` (a password from `cml-credentials.env` is masked in the PNG).

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
  For a link that must be its own cable (no DHCP, nothing else on it), give it an isolated libvirt
  network + an extra CML VM NIC + a CML controller bridge used by that connector; the PE Hub-Spoke HSRP
  lab does this for its three PE links (`docs/pe-hub-spoke-hsrp.md`, "How it's wired"). Deleting such a
  link in LabDesk keeps the `labdesk-*` network while another VM (e.g. `cml`) still has a NIC on it.
* New NICs usually appear in the guest only after a VM reboot.
* These devices are stored per lab in `~/.config/labdesk/state.json`, along with the management IPs.
* **Live collector (vJunos)**: when a vJunos has a management IP (`state.json` mgmt or found by virsh), LabDesk
  reads it every 15 s over SSH. Each burst is about 1.5 s of read-only `show` commands; anything else is refused
  before connecting: `show interfaces terse`, `show bgp summary` (all instances), `show route 0.0.0.0/0 exact`
  (inet.0 and VRF tables, not mgmt_junos), `show ospf neighbor`, the ge-* MACs (they name the cable ends), and
  every 60 s the local AS and login accounts. The login comes from device-creds `<LABEL>_USERNAME/_PASSWORD`,
  else `JUNOS_ADMIN_USER/_PASS` from `cml-credentials.env`. FortiOS has no live collector yet, so it shows **not
  read**, and its cables show **state unknown**, never "up".

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
  `~/cml-labs/cml-credentials.env`. The password never appears on a command line, in the console, or in
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
* **Linux: the LabDesk console window** runs that same argv as a real child process (`/usr/bin/setsid -c ssh …`)
  on a real pty inside LabDesk's own window - see the next section. Only `ssh` / `virsh` / `telnet` are accepted,
  there is no shell and no shell parsing. Because the child is an ordinary `ssh … connect <uuid>` process, LabDesk
  finds it like any other open console and pauses its background probing of that device.
* **Konsole (the fallback, or View → Console → Use Konsole instead):** LabDesk starts one
  `konsole --separate --profile LabDesk` window and adds a tab per
  device over D-Bus (`newSession`, `setTabTitleFormat`, `runCommand`). The `LabDesk` profile
  (`~/.local/share/konsole/LabDesk.profile`) doesn't start a shell. Its command is
  `labdesk_app/tabexec.py`, which reads one JSON line (argv) with echo off and runs that argv on the
  tab's pty. It accepts only `ssh`/`virsh`/`telnet`, does no shell parsing, and adds nothing to your
  shell history. If the command fails, the tab stays open and shows the error. Without D-Bus, LabDesk
  falls back to a new `konsole -e …` window, or xterm.

### The console window: neon green, one tab per device

On Linux every console opens as a **tab in one window, the LabDesk console**. It is LabDesk's own window, not a
recoloured terminal program: no toolbar and no menu bar, just three parts.

* **Tab strip (the header).** The first console creates the window, each later one adds a tab at the right, named
  after the device (R1, R2, HUB…) and focused. Opening a device that already has a live tab switches to that tab.
  The dot in a tab shows the *session* (not the device's health), by shape: a hollow ring = started, waiting for
  output; a solid dot = live; a ring with a slash = ended (red only when it failed). A tab whose device printed
  something while you were elsewhere gets a brighter label. Close a tab with its ✕ (a live one asks first), switch
  with **Ctrl+PgUp / Ctrl+PgDn** or **Ctrl+Tab**. The window title is `R1 - <lab> - LabDesk console`.
  With more tabs than fit, the tabs first shrink (long names are shortened in the middle; the tooltip and the
  window title have the full name), then the strip scrolls one whole tab at a time: mouse wheel over the strip,
  or click the `‹` / `›` at its ends.
* **The terminal.** **Neon green text (#39FF14) on green-black**, in the bundled *Hack* font at 11 pt on an exact
  9 × 21 px cell with 16 / 12 px padding; 110 × 42 characters by default. It is a real terminal on a real pty:
  Tab completion, `?`, Ctrl-C, Ctrl-Shift-6, the arrow keys, `--More--` paging and **Ctrl-]** (leave the console)
  reach the device exactly as typed. Only what the device sends is shown here - LabDesk never writes into it.
  Scrollback is 50 000 lines (mouse wheel, Shift+PgUp / PgDn, or drag the thin bar at the right).
  Select with the mouse (drag, or double-click a word); **Ctrl+Shift+C** copies, **Ctrl+Shift+V** or the middle
  button pastes.
* **Status line.** LabDesk speaks here: the session state, "LabDesk is running N show commands on this console"
  while a show script types, the size (`110 × 42`), and a red row when a session failed
  (`session ended (status 255) - open the device again to reconnect`; reopening restarts it in the same tab and
  keeps the scrollback).

On Cisco IOS / IOS-XE consoles LabDesk makes four things stand out (display only - what you copy is the raw text):
the **prompt** and the command you typed (bright; the `(config…)` part amber), **refused commands**
(`% Invalid input…`, `% Incomplete command`, `% Ambiguous command`, `% Unknown command` and the `^` marker, red),
the **severity of syslog messages** (`%LINK-3-UPDOWN` amber, severity 0-2 red, 5-7 untouched) and `--More--`.
Nothing else is coloured; colours the device itself sends always win.

Switches (menu **View → Console**, or the ⋮ button / right-click in the console; all apply at once and are remembered):

| Switch | What it does |
|---|---|
| **Neon green console** | off = a plain light-on-dark console |
| **Softer green** | a less saturated green (#7DF56A) for long sessions |
| **Highlight prompts and errors** | the IOS emphasis described above |
| **Larger / Smaller / Reset text size** | 8 to 20 pt, default 11 (in the console: **Ctrl+Alt+=**, **Ctrl+Alt+-**, **Ctrl+Alt+0**) |
| **Use Konsole instead** | open consoles as tabs of a Konsole window, as before (applies to the next console) |

Good to know:

* The consoles run inside LabDesk: **closing LabDesk ends them** (it asks first when any is open). LabDesk's own
  dialogs do not block the console window; the file pickers (import, export, save log, CML .vmx) do while they
  are open.
* A narrower window never re-wraps or loses text: it clips on the right (a dim `›` marks clipped rows) and the
  status line says `showing 90 of 110 columns`; widen the window to see everything again.
* A bell from the device is silent: the tab's dot brightens for a moment.
* If `pyte` or `/usr/bin/setsid` is missing, LabDesk uses Konsole automatically.
* **Konsole fallback:** there the look is the older Konsole colour scheme *LabDesk Neon*
  (`~/.local/share/konsole/LabDeskNeon.colorscheme`, yours to edit; LabDesk creates it once and never overwrites
  it) with profiles `LabDeskNeon.profile` / `LabDesk.profile`; *Neon green console* then applies to the next tab.
  Your own Konsole profiles and windows are not touched.
* Windows (LabDesk.exe): consoles are tabs in a Windows Terminal window named *LabDesk*; the LabDesk console
  window and its look are Linux only for now.
* The Hack font is bundled (`labdesk_app/ui/fonts/`, MIT + Bitstream Vera licence, see `LICENSE-Hack.txt`).

### Show script per device (what LabDesk types when you open a console)

Each Cisco router and managed switch can have its own **show script**: a short list of commands LabDesk types
for you in that device's console, so the window opens on the output you care about for *that* device
(HSRP and tracking on the spokes, BGP on the hub, VLANs on a switch…).

* **Edit it:** right-click a device → **Show script…**. One command per line.
  * **Suggest from live data** fills the editor from what the device is running right now (HSRP / VRRP groups,
    BGP, OSPF, VLANs, EtherChannel - read from the device, never from the lab file).
  * **Copy from…** takes another device's script as a starting point.
  * **Run this script each time I open the console** (on by default). Untick it to keep the script for the menu only.
  * **Save and run now** types it straight away.
* **Run it by hand:** right-click → **Run show script now** (the output appears in the open console).
* **Remove it:** empty the editor (or **Clear**) and **Save**.

What the console shows is just the commands and their output (plus `terminal length` / `terminal width`, which stop
the output from pausing at `--More--` and are put back at the end):

```
R1#terminal length 0
R1#show standby brief
Interface   Grp  Pri P State   Active          Standby         Virtual IP
Gi0/1       10   110 P Active  local           172.16.0.3      172.16.0.1
R1#show track brief
…
R1#terminal length 24
R1#
```

Rules: only `show`, `ping` and `traceroute` lines are accepted (`sh` / `sho` are fine), so a script that runs by
itself can never change a device; writing output to a file (`| redirect`, `| tee`, `| append`) is refused; a line
starting with `!` or `#` is a comment; at most 20 commands. `show running-config` needs the enable secret
(right-click → **Credentials…**). Wait for the script to finish before typing - your keys and the script would mix.
A device without a script behaves as before. Scripts are saved per device name in
`~/.config/labdesk/show-scripts.json` (Windows: `%APPDATA%\LabDesk\show-scripts.json`). Not available for Juniper /
Fortinet VMs and Linux hosts yet. Live check: `tools/show_script_live.py --nodes R1,HUB`.

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
  ui/console/           the LabDesk console window (Linux): palette, screen (pyte + scrollback), keymap, lens (IOS
                        emphasis), boxdraw - pure Python; session (ssh child on a pty), view, tabstrip, statusline,
                        window, launcher
  catalog.py, validate.py, config.py
  parsers/ios_l2.py     IOSvL2 switching parsers (VLANs, trunks, STP, MAC, CDP, EtherChannel, LACP, PAgP)
  etherchannel.py       EtherChannel mode rules, IOS command generation, day-0 patching (pure)
  show_scripts.py       per-device show scripts: read-only validation, show-scripts.json, suggestions from live facts
  accounts.py           on-device accounts from IOS / Junos / FortiOS configs: encrypted (scheme) or plain text (pure)
  probe/creds.py        device-creds file: per-node / default login, atomic 0600 writer
  ui/                   main_window, canvas, graphics (vector vendor icons), dialogs, hovercard (+ hovercard_model), worker (thread pool)
templates/              3-router-hsrp.yaml, hub-spoke-hsrp-bgp.yaml, switching-lab-etherchannel.yaml,
                        pe-hub-spoke-hsrp.yaml (Juniper PE + Cisco hub/spokes, docs/pe-hub-spoke-hsrp.md)
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

## PE Hub-Spoke HSRP lab (Juniper PE + Cisco hub / HSRP spokes)

`templates/pe-hub-spoke-hsrp.yaml` is the CML export of the "PE Hub-Spoke HSRP" lab: HUB, R1, R2 (IOSv),
PC-DATA, PC-PHONE (alpine) and three external connectors `BR-PE-HUB/R1/R2`, each on its own CML bridge
(`PE-HUB` / `PE-R1` / `PE-R2` = controller `bridge1..3`) and its own isolated host network
(`labdesk-pe-hub/-r1/-r2`): every PE link is a separate L2 segment. Importing the YAML on another CML needs
those bridges first (`build_pe_hub_spoke.py --restart-cml` creates them). The PE is the host-KVM
vJunos VM `PE` (routing-instance CUSTOMER), placed on the lab canvas in `state.json` with one link per
connector. Build script, device configs, verification and the full write-up live in `~/cml-labs/pe-hub-spoke`
(copy of the guide: `docs/pe-hub-spoke-hsrp.md`).
