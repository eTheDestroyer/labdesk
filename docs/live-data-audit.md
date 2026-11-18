# Live-data audit (2026-11-18)

**Rule:** everything LabDesk shows *about a device* comes from live device output: IOS console probe bursts,
the device's own console transcript (CML console log), the running-config read from the device, and
read-only SSH `show` commands (vJunos). It never comes from the lab YAML, the CML node configuration (day-0 /
startup config), templates, CML's intended interface data, CML's L3 address snooping, hardcoded tables or
guesses. When nothing live exists (node stopped, not probed yet, probe failed, no collector), the UI says
**not read yet** / **unknown**, and gives the age of the last read when the data is stale.

These are allowed because they are not information from the router, and the UI labels them:

* CML's (or libvirt's) runtime state of nodes and links: the header reads **CML: Booted** / **VM: Running**.
* the user's layout.
* LabDesk's own saved values: the management IP and device-creds, shown as **saved in LabDesk**.
* first-login defaults: LabDesk uses them to *try* a login, and shows them as **LabDesk will try: … (default,
  not read from device)**.

| Field | Where shown | Source before | Source after |
|---|---|---|---|
| Interface IP / subnet on cables (`LinkAddressing`) | canvas subnet label, host part `.1`, Inspector link page | CLI `show ip interface`, console transcript, running-config **plus CML `layer3_addresses` (L2 DHCP snooping, `source='api'`)** | CLI, console transcript and running-config only. The CML snooping read (`TelemetryEngine._poll_l3`, `NodeFacts.api_addrs`, `EndAddr.api_ips`, `SubnetState.DHCP` from the API) was **removed**. |
| Interface name pills at cable ends | canvas | CML interface label, or the NIC name LabDesk gave a KVM VM at creation, always drawn as fact | CML's port label is the cable's wiring and is still drawn, but **dashed** with the tooltip "port name from CML's wiring, not confirmed by the device" until the device reports that interface (`LinkView.live_a/live_b`). For KVM cables, the name now comes from the device: the interface whose MAC matches the VM NIC (`show interfaces ge-* \| match …`). |
| Link up/down | canvas stroke and end shapes, link status text | Live CLI. **A host-KVM router end was `NOT_APPLICABLE`, so a KVM cable showed UP because "both VMs are running"** (a guess) | A router LabDesk cannot read is now `UNKNOWN` (status **not read**). A cable with no read end at all (host ↔ unmanaged switch) keeps CML's "link started" state, with the text "CML state: the link is started and both nodes are running (no device on this cable is read by LabDesk)". |
| OSPF / BGP / HSRP / VRRP / STP / VLAN / trunk / EtherChannel chips and rails | canvas, hover card, Inspector | live CLI (`NodeFacts`, dropped when the node stops) | unchanged (verified live); stale chips show ⏱ and fade |
| Default route chip and blue path | canvas, hover card | live `show ip route 0.0.0.0` / CEF, Junos `show route 0.0.0.0/0 exact` (inet.0 only) | unchanged for IOS. For Junos, routing-instance tables (`CUSTOMER.inet.0`) now count too (mgmt_junos is still excluded). |
| Router-id, AS numbers, peers | hover card, Inspector, chip tooltips | live CLI | unchanged. Junos local AS comes from the live `show configuration routing-options autonomous-system`. |
| PE (host-KVM vJunos): everything | canvas, hover card | nothing live. The chip read "beta", the links were drawn UP and the card said "collector not wired" | A **live, read-only SSH collector** (`probe/junos_ssh.py`, `telemetry.JUNOS_LIVE`): interfaces, BGP (all instances), default route, OSPF neighbours, MACs, local AS, and accounts in the slow tier. With no management IP it shows **not read**. |
| Node state badge / header state | canvas, hover card header | CML / libvirt state, unlabelled | the same state, labelled **CML: …** / **VM: …** (tooltip "CML state: …") |
| Hover card "Probe" / "Probed" row | hover card | collector state + age | a single **Live data** row: `read 7 s ago` / `stale · last read 3 min ago` / `not read yet` / `not read (device not running)` / `not read (… collector not wired yet)` |
| Mgmt IP | hover card, Inspector | state.json (LabDesk), unlabelled | the same value, labelled **saved in LabDesk**, or **read from device** when an interface reports that IP |
| Login rows | hover card, Inspector | `default_logins` table or the **day-0 config** (`parse_day0`), shown as "First login" with a `default` badge | **LabDesk will try: …** with the note *default, not read from device*; saved values show **Saved login** (saved in LabDesk). Day-0 parsing is kept only for choosing what to try. |
| Accounts "On the device" | hover card | live running-config (IOS) | unchanged. Junos now uses the live `show configuration system login \| display set` (hashes never kept). When nothing is read it says "not read (device not running)" or "not read yet". |
| Inspector "Links: n of m up" | Inspector facts | counted CML "STARTED" as up when no telemetry | counts only joined live status, otherwise shows "n (state not read)". A **Live data** row was added. |
| Egress tile NAT / Bridge | canvas | CML connector configuration (`System Bridge` / `NAT`) | kept. It is CML's own setting of a connector, not a device read. |
| Node role / icon (router, switch, host) | canvas | CML node definition | kept. It is the CML node type, not a device read. |
| Startup-config editor, cloud / EtherChannel plans | dialogs (actions) | day-0 config via `node_configuration` | kept. These are editors and action plans, labelled as the CML day-0 configuration, and they never fill a displayed device fact. |

## How it is enforced

* `tests/test_live_only.py`:
  * Lab nodes carry rich day-0 configs and CML interface `ip4` data, and the consoles are dead. The view then
    has no chips, no subnet labels, no blue, no P2P, no confirmed names, router links are `UNKNOWN`, and the card
    reads **Live data: not read yet**. `layer3_addresses` raises if it is ever called.
  * After the consoles answer, the facts appear and nothing from day-0 ever does.
  * PE without a management IP shows **not read**. With the (fixture) SSH collector it shows BGP AS 65000 and
    3 peers, the NIC MAC names the cable end `ge-0/0/0`, and the read-only guard refuses non-`show` commands.
* `tools/live_only_shots.py` takes the live screenshots in `qa/live-only/`. `startup-*` is before the first
  read (everything "not read yet" / "state unknown"), and `live-*` is after about 75 s.
