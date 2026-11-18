# LabDesk telemetry model (data layer contract for the UI)

This document is the contract between the data layer (`facts.py`, `telemetry.py`, `parsers/`,
`probe/`) and the UI (`ui/*`). Field names are exact. The UI only reads these objects: it never
parses CLI text, never derives paths, and never compares against string spellings. It uses the
enums in `labdesk_app.facts`.

```
LabController.poll()  (TaskRunner thread, every 3 s; light API polling per D7)
  └─ TelemetryEngine.update(topology)   (cheap; never waits on a console)
        ├─ API diffs (link STARTED→STOPPED → link_history → CABLE_PULLED)
        ├─ schedules CLI bursts on its own 3 daemon threads (D1/D2/D10)
        ├─ join_facts()  → link status (D4), P2P (V3), segments/protocols (D3), default paths (D5)
        └─ TelemetryView  →  LabView.telemetry  (+ promoted fields on NodeView / LinkView)
```

## 1. Where the UI gets it

`LabView` (from `controller.poll()`) carries everything:

| field | type | meaning |
|---|---|---|
| `LabView.telemetry` | `TelemetryView \| None` | the full model. `None` when the controller was built with `telemetry=False` |
| `LabView.running_total` | int | CML-Free licence counter across **all labs**, as before (`CML n/5`) |
| `LabView.this_lab_running` | int | this lab's share, for the tooltip |
| `LabView.limit` | int | 5 |

### NodeView (existing fields unchanged; new fields)

| field | type | notes |
|---|---|---|
| `tele` | `NodeTelemetry \| None` | full per-node model |
| `chips` | `list[Chip]` | ordered `OSPF`, `BGP`, `DEFAULT` (whichever exist). Show at most 3, with `+N` overflow. Hide below zoom 0.6 |
| `fhrp` | `list[FhrpBadge]` | HSRP/VRRP role badges, drawn in a tile corner (V5/V8) |
| `default_chip` | `Chip \| None` | same object as the `DEFAULT` entry in `chips` |
| `collector_state` | `CollectorState \| None` | see §3 |

### LinkView (existing fields unchanged; new fields)

| field | type | notes |
|---|---|---|
| `tele` | `LinkFacts \| None` | full per-link model (§4). `tele.tooltip` is a ready-made multi-line tooltip |
| `status` | `LinkStatus \| None` | **use this for colour, dash and down text**, not `state` |
| `side_a`, `side_b` | `LinkSide \| None` | `a` = `LinkView.a` end |
| `p2p` | bool | solid `P2P` bead (only when `p2p_source` is an *operational* source) |
| `p2p_source` | `P2PSource \| None` | `CONFIG` together with `p2p == False` means the outlined `P2P?` bead |
| `p2p_conflict` | `str \| None` | `'configured p2p, not active'`. Draw `P2P?` with this text as its tooltip |
| `subnet_hint` | `str \| None` | `'/30'`, shown **in the Inspector only** (a /30 alone never draws a bead) |
| `addressing_mismatch` | bool | the two ends are not in the same subnet |
| `addressing` | `LinkAddressing \| None` | the subnet label of the cable (§4a). None = nothing to draw |
| `default_hop` | `RouteHop \| None` | not None means paint this link BLUE (`#2F7BFF`). Direction is in `tele.default_direction` (`'a2b'`/`'b2a'`) |
| `traffic` | `LinkTraffic \| None` | only while the Traffic layer is on or the link is selected |
| `condition` | `LinkCondition \| None` | show `⌁` + `condition.label` when `condition.enabled` |

Additional per-link fields on `tele` that LinkView does not promote:

- `bcast` (bool): the BCAST teaching bead.
- `bcast_detail`: `'DR R2 · BDR R1'`, for the tooltip.
- `protocols`: `dict[Proto, Health]`, for rails on 2-router segments only.
- `adjacencies`: `[(nodeA, nodeB, Proto, state)]`.
- `subnet`: `'10.0.13.0/30'`.

## 2. Enums (`labdesk_app.facts`, all `str` enums)

| enum | values |
|---|---|
| `LinkKind` (D4 priority order) | `LAB_OFF, NODE_OFF, CABLE_PULLED, LINK_NOT_STARTED, UNKNOWN, ADMIN_DOWN, LINK_DOWN, PROTOCOL_DOWN, UP` |
| `LinkSide` | `UP, ADMIN_DOWN, DOWN, PROTO_DOWN, NOT_APPLICABLE` (switch / connector / unprobed end), `UNKNOWN` (no fresh CLI) |
| `Health` | `OK, PARTIAL, DOWN, UNKNOWN` |
| `P2PSource` | `OSPF_OPER, CONFIG, JUNOS_PTTOPT, FORTIOS_P2P, MEDIUM` (`facts.OPER_P2P_SOURCES` = all except CONFIG) |
| `PathState` | `EGRESS, VERIFIED_INTERNET, BROKEN_AT, NO_EGRESS, NONE, BLACKHOLE, UNRESOLVED, LOOP, STALE, UNKNOWN` |
| `CollectorState` | `ok, learning, paused_console, console_busy, needs_credentials, error, no_mgmt, limited, unverified, off, disabled, not_probed` |
| `Proto` | `OSPF, BGP, HSRP, VRRP` |
| `SegmentKind` | `p2p, multi, egress, other` |
| `NodeRole` | `router, switch, egress, host` |
| `PortRole` | `Root, Desg, Altn, Back, Mstr, Disa, Shr, None` (IOS spellings; `facts.ROLE_WORDS` has the words) |
| `PortState` | `FWD, BLK, LRN, LIS, DIS, BKN` (`facts.STATE_WORDS`) |
| `PortMode` | `access, trunk, routed, unknown` |
| `EcProto` | `lacp, pagp, on` (static `mode on`; the summary's Protocol column shows `-`), `none` (no channel-group) |
| `EcState` | `up` (all members bundled), `partial` (1..N-1), `down`, `mismatch` (never bundles), `unknown` (stale) |

`TopoNode.managed` is True for `iosvl2` / `ioll2-xe` (`facts.MANAGED_SWITCH_DEFS`). `TopoNode.collected`
is True for nodes with a CLI collector: IOS routers and managed switches. A managed switch has
`role == switch`, is probed like a router (same etiquette), and its link ends are real sides (not
`NOT_APPLICABLE`).

`facts.DOWN_KINDS = {ADMIN_DOWN, LINK_DOWN, PROTOCOL_DOWN, CABLE_PULLED}`.

### Suggested link rendering per `LinkStatus.kind`

| kind | stroke | canvas text (`status.text`) |
|---|---|---|
| `UP` | normal up colour (blue if `default_hop`) | none |
| `ADMIN_DOWN` | red, dashed, ✕ at the `side_node` end | `'R2 Gi0/4 shut'` |
| `LINK_DOWN` | red | `'R2 Gi0/4 down'` |
| `PROTOCOL_DOWN` | red / amber | `'R2 Gi0/4 line protocol down'` |
| `CABLE_PULLED` | red, gap in the middle | `'cable pulled'` (`api_certain=True`) |
| `LINK_NOT_STARTED` | neutral grey | `'link not started'` |
| `UNKNOWN` | grey dotted | `'state unknown'` (stale CLI) |
| `LAB_OFF` / `NODE_OFF` | neutral | `'lab off'` / `'R3 off'` |

`status.plain_text` is a jargon-free sentence for tooltips and the Overview tab. For example,
"Someone typed shutdown on R2's Gi0/4; R4's side still looks fine." `status.side_node` and
`status.side_iface` say which end is down. `status.stale_sides` lists node keys whose facts were
too old to use.

## 3. Node model: `telemetry.NodeTelemetry`

| field | type | meaning |
|---|---|---|
| `key`, `label`, `role`, `vendor` | | `role: NodeRole`, `vendor: 'cisco'\|'juniper'\|'fortinet'\|'generic'\|'linux'` |
| `collector_state` | `CollectorState` | the chip on the tile (V10). See the table below |
| `collector_detail` | str | plain words, e.g. `'console is in configuration mode (retry in 30 s)'` |
| `probing` | bool | a burst is running right now: show the "probing…" spinner |
| `last_probe_age_s` | float\|None | for the legend's Freshness row |
| `hostname` | str\|None | learned from the prompt. It can differ from the CML label |
| `rid`, `loopback` | str\|None | for the tooltip and Inspector only |
| `chips` | `list[Chip]` | see below |
| `fhrp_badges` | `list[FhrpBadge]` | |
| `default_route` | `DefaultRoute\|None` | |
| `default_path` | `DefaultPath\|None` | |
| `ifaces` | `list[IfFact]` | sorted. Feeds the Inspector's Interfaces tab |
| `ospf_nbrs` | `list[dict]` | `{neighbor_id, name, state, adj_state, role, iface, address}`. `name` is the resolved lab label or None |
| `bgp` | dict\|None | `{local_as, rid, peers:[{ip, name, remote_as, ebgp, state, established, pfx_rcvd, uptime}]}` |
| `fhrp` | `list[FhrpGroup]` | |
| `config_text` | str\|None | **redacted** running-config (Config tab) |
| `config_age_s` | float\|None | age of `config_text` |
| `summary` | str | plain-English Overview line: `'R1: OSPF ok (2 neighbours), BGP none, HSRP Active grp 10, VRRP Master grp 20, default route via R3 (O*E2)'` |
| `ages` | dict | per-fact age in seconds (`ifaces, ospf_nbrs, bgp, fhrp, default_route, connected, config, rid`) |
| `console_open` | bool | a console ssh for this node is open on this host |
| `probe_while_open` | bool | the per-node override is set |
| `switch` | `SwitchView\|None` | managed switches only (§3a) |
| `accounts` | `accounts.DeviceAccounts\|None` | on-device logins parsed from the running-config (CONFIG tier): per account `kind, name, encrypted, scheme` (`'type 9 scrypt'`), `weak`, `privilege`, `detail` (`'encrypted (type 9 scrypt)'` / `'plain text (not encrypted)'`); `plaintext` only for unencrypted ones (hover card only, `repr=False`); hashes are never kept. `password_encryption` = `service password-encryption` |
| `accounts_age_s` | float\|None | age of `accounts` |
| `accounts_note` | str | why `accounts` is None: `'needs enable secret'`, `'reading…'`, `''` |

For a managed switch, `chips` are `ROOT` (only on a root bridge: `'ROOT v10,20'`, drawn as the gold crown
badge above the tile), `L2` or `L3`, `VLAN` (`'2 VLANs'`, VLAN 1 not counted) and `STP` (`'RPVST+'`), and
`summary` is the switch sentence (`facts.switch_summary`):
`'SW2: 2 VLANs, root for none (root is SW1), Gi0/2 blocking (Alternate) for VLAN 10,20 to prevent a loop'`.

### 3a. `telemetry.SwitchView` (`NodeTelemetry.switch`)

| field | meaning |
|---|---|
| `stp_mode`, `stp_label` | `'rapid-pvst'`, `'RPVST+'` |
| `l3`, `routing` | L3 badge: `ip routing` active **and** at least 2 SVIs up with an address (`facts.switch_is_l3`) |
| `vlans` | `[VlanInfo(id, name, status, ports)]`, 1002-1005 hidden |
| `root_for` | VLANs this switch is the root bridge for |
| `root_of` | `{vlan: root bridge name}`: the lab label resolved from bridge MACs, or the MAC if foreign |
| `stp` | `[StpVlanInfo]`: `vlan, protocol, root_priority/address/cost/port, is_root, bridge_priority/address, ports{iface_key: StpPortInfo(port, role, state, cost, prio, nbr, edge, p2p, inconsistent)}`, plus `root_id` / `bridge_id` properties |
| `tc` | `{vlan: TcInfo(changes, last_ago_s, from_iface)}` |
| `ports` | rows `{port, desc, status, mode, vlan_text, stp_text, blocked, err_disabled, side, neighbor}` (the Ports tab) |
| `blocked` | `[(port, role word, [vlans])]` |
| `err_disabled` | list of ports |
| `svis` | the `Vlan*` IfFacts |
| `mac_counts`, `mac_total`, `mac_table` | `{vlan: n}`, total, `[{vlan, mac, type, port}]` (dynamic) |
| `cdp`, `etherchannel` | parsed tables |
| `ec_sides` | `[facts.EcSide]` this switch's Port-channels (the switch Inspector's **EtherChannel** tab) |
| `ec_bundles` | `{'Po1': Bundle.key}` (select the bundle on the canvas) |
| `ages` | per-fact ages |

A bundled EtherChannel member's row in `ports` shows its Port-channel: mode `'trunk · Po2'`, the Po's VLANs and
`stp_text = 'via Po2: Altn BLK'` (`blocked` follows the Po, so the front-panel LEDs turn amber); `blocked` lists the
Po itself (`('Po2', 'Alternate', [10, 20])`), and the switch summary says `'Po2 blocking (Alternate) …'` and
`'Po1 LACP 2/2 to SW2'`.

Raw facts live in `NodeFacts.l2: L2Facts` (`vlans, trunks, ports, stp, stp_mode, root_for, tc,
mac_counts, mac_table, cdp, etherchannel, routing`, each a `Fact`), merged by `facts.l2_facts_update`.

| `collector_state` | chip text |
|---|---|
| `learning` | 'learning…' (booted, first FAST burst not finished) |
| `paused_console` | 'paused: console in use'. Grey out CLI facts; API-derived state keeps rendering |
| `console_busy` | 'console busy' (someone is in config mode, at a pager or at a prompt). Nothing was typed |
| `needs_credentials` | 'needs login / enable secret' |
| `error` | 'probe error' |
| `unverified` | 'beta' (host-KVM router, collector not wired in this release) |
| `limited` | 'limited' (FortiOS unlicensed, only when `get system status` confirms it) |
| `no_mgmt` | 'no mgmt' (KVM) |
| `off`, `disabled`, `not_probed` | no chip |

### `Chip`

| field | type | meaning |
|---|---|---|
| `kind` | str | `'OSPF'`, `'BGP'` or `'DEFAULT'` |
| `text` | str | `'OSPF'`, `'BGP 65000'`, `'O*E2'`, `'B* eBGP'`, `'S* dhcp'`, `'0/0 no exit'`, `'0/0 ✕'`, `'0/0 ✕ blackhole'`, `'0/0 ?'`, `'0/0 loop R3↔R4'` |
| `health` | `Health` | fill. The hue is fixed per `kind` |
| `glyph` | str | `'✓'` FULL/Established, `'…'` converging, `'!'` configured but no neighbour, `'✕'` broken, `'⏱'` stale, `''` |
| `tooltip` | str | plain words, e.g. `'BGP AS 65000: 1 peer up (R4, AS 65004), 2 prefixes received'` |
| `stale` | bool | age > `FRESH_S` (45 s): the glyph is not trusted and is shown as ⏱ |
| `faded` | bool | age > 3 × the tier interval: render at 40 % opacity with ⏱ |
| `outline` | bool | True for `DEFAULT` (outline chip, neutral text) |
| `blue_bar` | bool | `DEFAULT` only: draw the 3 px ROUTE_BLUE left bar (the path is EGRESS or VERIFIED, fresh and unbroken) |
| `age_s` | float\|None | |

### `FhrpBadge`

Fields: `proto` (Proto), `group`, `role` (`Active`, `Standby`, `Master`, `Backup`, `Speak`,
`Listen`, `Init`), `primary` (bool), `iface` (short name), `vip`, `priority`, `tooltip`, `stale`.

### `IfFact` (one interface)

| field | meaning |
|---|---|
| `name`, `short` | `'GigabitEthernet0/4'`, `'Gi0/4'` |
| `ip`, `prefixlen` | `cidr` is a property |
| `admin` | `'up'` or `'down'` |
| `oper` | `'up'` or `'down'` (line protocol) |
| `phys` | `'up'`, `'down'` or `'admin-down'` |
| `method` | `'TFTP'`, `'DHCP'`, … |
| `ospf_net` | `POINT_TO_POINT`, `BROADCAST` or `LOOPBACK` |
| `ospf_state` | `P2P`, `DR`, `BDR`, `DROTH`, `WAIT`, `LOOP` or `DOWN` |
| `ospf_passive` | |
| `ospf_nbrs` | `(full, configured)` |
| `desc` | |
| `mac` | |
| `side` | property returning a `LinkSide`. The Interfaces tab shows the text form `UP`, `DOWN`, `ADMIN DOWN` or `PROTO DOWN` |

### `DefaultRoute`

| field | meaning |
|---|---|
| `present` | |
| `code` | `'S*'`, `'B*'`, `'O*E2'`, … |
| `source_label` | `'S* dhcp'`, `'B* eBGP'`, `'O*E2'` |
| `proto` | |
| `distance` | |
| `next_hops` | `[(ip, out_iface)]` |
| `via_dhcp` | |
| `blackhole` | |
| `from_ip` | |
| `forwarding_checked` | True when `next_hops` came from CEF |

### `FhrpGroup`

| field | meaning |
|---|---|
| `proto` | |
| `iface` | |
| `group` | |
| `state` | |
| `priority` | |
| `preempt` | |
| `vip` | |
| `active` | HSRP: the active router. VRRP: the master |
| `standby` | |
| `is_primary` | property |
| `role` | property |

## 4. Link model: `facts.LinkFacts` (`LinkView.tele`)

| field | type | meaning |
|---|---|---|
| `key` | str | `cml:<link-id>` or `kvm:<i>` |
| `side_a`, `side_b` | `LinkSide` | |
| `iface_a`, `iface_b` | `IfFact\|None` | |
| `status` | `LinkStatus` | `kind, side_node, side_iface, cause, text, plain_text, api_certain, stale_sides` |
| `segment_id` | str | key into `TelemetryView.segments` |
| `p2p`, `p2p_source`, `p2p_conflict` | | V3 |
| `bcast`, `bcast_detail` | | V3 teaching contrast (R1–R2 cross-link: `'DR R2 · BDR R1'`) |
| `subnet`, `subnet_hint` | | `'10.0.13.0/30'`, `'/30'` |
| `addressing_mismatch` | bool | |
| `protocols` | `dict[Proto, Health]` | **only for 2-router (p2p) segments**. These are the rails. Never on router↔switch links |
| `adjacencies` | `list[(nodeA, nodeB, Proto, state)]` | |
| `default_hop` | `RouteHop\|None` | |
| `default_direction` | `'a2b'\|'b2a'\|None` | forwarding direction for the `›` chevrons |
| `tooltip` | str | multi-line, plain words |
| `l2_a`, `l2_b` | `PortL2\|None` | the managed-switch end: `node, port, mode, status, access_vlan, native, trunk_text, allowed_text, vlans, stp{vlan: (PortRole, PortState)}`, plus `blocked_vlans`, `forwarding_vlans`, `err_disabled` |
| `vlans` | list[int] | user VLANs carried (access VLAN, or the trunk's allowed+active VLANs on both ends) |
| `trunk`, `trunk_text`, `native_vlan`, `native_mismatch` | | an 802.1Q trunk: `'10,20'`, native VLAN, mismatch between ends |
| `access_vlan`, `access_side` | | the switch end that is an access port (`'a'`/`'b'`) and its VLAN |
| `stp_block` | `StpBlock\|None` | `side, node, port, role, vlans, partial` (+ `role_word`): an end spanning tree blocks. **The link status stays `UP`** |
| `bundle` | str\|None | `Bundle.key` when the cable is an EtherChannel member (also promoted as `LinkView.bundle`) |
| `ec_a`, `ec_b` | `EcMember\|None` | that end's member state (flags, mode, partner); see §4b |

An err-disabled switch port is `LINK_DOWN` with `cause == 'err-disabled'` and text `'SW1 Gi0/3 err-disabled'`.

UI rendering (layer `l2`): `V10` chip at `access_side`; `TRUNK 10,20` bead (only while `UP`); at
`stp_block.side` a hatched amber pill `'Gi0/2 · Altn BLK'`, a slashed amber cap and a muted dashed half.
Layer `vlan` + `Layers.vlan`: a halo on links whose `vlans` contain the VLAN, others dimmed.

### 4a. Subnets on cables: `facts.LinkAddressing` (`LinkFacts.addressing`, `LinkView.addressing`)

Computed by the pure `facts.link_addressing(link, topo, facts, now, fresh_s)` inside `join_facts` from the
*effective* addressing of both ends (`facts.effective_addrs(nf)`, see "Address sources" below). Router ports
(and routed switch ports) are L3 ends; an L2 switchport, an unprobed host or a connector is not. A router
port's subinterfaces (`Gi0/0.10`, `Gi0/0.20`) belong to its cable.

| field | meaning |
|---|---|
| `state` | `SubnetState`: `match` (both ends share a subnet, or the only L3 end has one), `one_sided` (two L3 ends, one configured), `mismatch`, `dhcp`, `unnumbered` (`none` is never attached) |
| `nets`, `shared` | every network on the cable (the shown one first); the networks both L3 ends have |
| `label` | `'10.0.12.0/30'`, `'192.168.10.0/24 +1'`, `'⚠ mismatch 10.0.13.0/30 ≠ 10.0.31.0/30'`, `'dhcp 192.168.255.x/24'`, `'unnumbered'` (the UI prefixes `½ ` for `one_sided`) |
| `compact` | zoomed-out form: `'…12.0/30'`, `'192.168.10/24'`, `'⚠ mismatch'`, `'dhcp'` |
| `host_a`, `host_b` | `'.1'` / `'.2'` (`facts.host_part`; `''` for /32; `'.1…'` when subinterfaces differ) |
| `a`, `b` | `EndAddr(node, label, iface, l3, addrs=[(iface short, ip, plen, secondary)], method, unnumbered, sources, ts, live_ts, api_ips, role)` |
| `configured_side` | `'a'`/`'b'` for `one_sided` |
| `tooltip`, `explain` | multi-line plain text (first line = headline, last line = source + age); one sentence for the Inspector |
| `source`, `age_s`, `stale` | `'cli'`, `'console'`, `'cli+console'`, `'api'`; age of the oldest address read; stale after 4 × `FRESH_S` without any read (drawn faded) |

`lf.subnet` / `subnet_hint` / `addressing_mismatch` are derived from it when it exists.

**Address sources (provenance in `AddrInfo.source`)**

| source | what | when |
|---|---|---|
| `cli` | `parsers.ios.IP_ADDR_CMD` = `show ip interface \| include line protocol\|Internet address\|Secondary address\|unnumbered\|determined by\|negotiated\|processing disabled` (FAST tier, routers and IOSvL2; ~1.5 s, works at `>`) -> `NodeFacts.addrs`. Falls back to `show ip interface brief` + OSPF brief / connected prefix lengths | every FAST burst (15 s) |
| `console` | **console tap**: `GET /labs/{lab}/nodes/{node}/consoles/0/log?lines=200` (CML's server-side console transcript, ~17 ms, never touches the session), parsed by `parsers.console_tap` (prompt-tracked `interface …` context, `ip address A M [secondary]`, `no ip address [...]`, `ip address dhcp`, `ip unnumbered X`, `default`/`no interface X`; Tab redraws, `?`, backspaces handled; a command followed by an IOS `%` error is ignored; a command on the last log line is re-read after 0.4 s) -> `NodeFacts.addr_overrides` | every `TAP_S` = 1 s while the node's console is open (daemon thread `labdesk-console-tap`), once more right after it closes, and on Refresh |
| `config` | the redacted running-config (`ConfigFacts.addrs`, incl. secondaries / unnumbered) - only fills a missing prefix length | CONFIG tier |
| `api` | `GET /labs/{lab}/layer3_addresses` | full-snapshot cadence, **only when the lab has a System-Bridge connector**: on CML 2.10.1 it reports only DHCP leases snooped on L2 external connectors (verified: empty for every statically addressed interface) |

`effective_addrs` merges per interface by read time: a console override wins only while it is newer than the
last CLI address read, and `prune_overrides` drops it as soon as a later CLI read exists (a burst that started
before the user typed cannot undo the change).  Measured live on CML 2.10.1 (`tools/subnet_live_check.py`,
`qa/subnet_live_log.txt`): typed-in-console -> label on the canvas ≈ 1-2 s; CLI-only (console closed) ≤ one
FAST burst after the console closes.

**Events** (`kind 'SUBNET'`, `proto 'subnet'`): `facts.subnet_changes(before, after)` diffs the effective
addressing after every CLI merge and every tap read (the first read is the baseline): `'R1 Gi0/2: 10.0.13.0/30
configured'` (ok), `'… 10.0.14.0/30 changed (was 10.0.13.0/30)'` (info), `'… removed'` (warn), secondaries as
`'172.16.99.0/24 secondary configured'`; `(console)` is appended when the change came from the tap. A link
entering `mismatch` logs `'R1 Gi0/2 ↔ R3 Gi0/0: addressing mismatch a ≠ b - the two ends cannot reach each
other'` (warn) once, and `'… addressing fixed (…)'` when it clears. `items` = node + cable keys; the UI pulses
the label for 2.4 s.

**UI**: layer `subnets` (*Subnets on cables*, default on). The label is the last row of the midpoint stack
(full at zoom ≥ 0.55, compact ≥ 0.33, only a mismatch `⚠` below); host parts join the interface pills.
`ui.scene_model.place_stacks` places every link's stack so that none overlaps another stack, a node tile /
label / chip, an interface pill or another cable (candidates along the cable, then beside it).

### 4b. EtherChannel: `facts.Bundle` (`TelemetryView.bundles[key]`)

Built by the pure `facts.build_bundles(topo, facts, links, now, fresh_s)` inside `join_facts` from each device's
`NodeFacts.ec: EcFacts` (`summary, lacp_internal, lacp_neighbor, lacp_sys, pagp_internal, pagp_neighbor,
load_balance`, each a `Fact`, plus `problems` from `%EC-5-*` syslog).  A bundle = the parallel member cables of one
Port-channel on each end (paired across the shared cables); the far end may have no channel-group at all.

| field | meaning |
|---|---|
| `key` | `'ec:<node>/Po1\|<node>/Po1'` (`'…/-'` when the far end has no channel-group) |
| `a`, `b`, `side_a`, `side_b` | node keys and `EcSide` (None = no channel-group on that end) |
| `link_keys` | member cables (canvas links), ordered by interface |
| `protocol` | `'LACP'`, `'PAgP'`, `'on'`, or `'LACP≠on'` / `'LACP≠PAgP'` / `'LACP≠none'` when the ends differ |
| `bundled`, `total` | cables bundled on every configured end / member cables |
| `state` | `EcState` (priority: unknown (stale) > mismatch > down > partial > up) |
| `label` | `'Po1 · LACP 2/2'` (the bead); `po_text` `'Po1'` or `'Po1/Po3'` when the numbers differ |
| `mismatch`, `mismatch_short` | plain-English sentences; the short canvas reason `'LACP passive ↔ passive — never bundles'` |
| `problems` | per member: `'SW1 Gi0/2 not bundled (shutdown)'` |
| `stp_block` | `StpBlock` with `port = 'Po2'`: spanning tree blocks the **whole Port-channel** (one marker) |
| `trunk_text`, `native_vlan`, `vlans`, `load_balance` | from the Port-channels |
| `tooltip`, `explain` | multi-line / one sentence for the Inspector |

`EcSide`: `node, label, group, po, proto (EcProto), flags ('SU'/'SD'), up, members, sys_priority, sys_mac (LACP),
load_balance, stp {vlan: (PortRole, PortState)} (the Po's), trunk_text, native, access_vlan, mode_l2, age_s`, plus
`bundled`, `modes`, `flag_words`, `stp_text`.

`EcMember`: `port, flags ('P' bundled, 'I' stand-alone, 's' suspended, 'H' hot-standby, 'D' down, 'w' waiting),
words, bundled, mode (active|passive|desirable|auto|on), link_key, peer_node/peer_port (CML cable),
partner_node/partner_port/partner_source ('lacp' via the partner's `show lacp sys-id` + LACP port number,
'pagp' via the PAgP neighbour name, 'cdp'), partner_dev, partner_mode, port_priority, partner_priority, lacp_state
(bndl/indep/susp/hot-sby/down), speed, duplex, vlan, problem` (the `%EC-5-…` reason while it stays unbundled).

**Mismatch rules** (`etherchannel.check_modes`, per member cable, plus member consistency):
passive ↔ passive and auto ↔ auto never start negotiation; `on` against LACP/PAgP gets no answer; LACP against
PAgP; no channel-group on one end; members of one end with different speed / duplex / VLAN-trunk
(`show interfaces status`); `%EC-5-CANNOT_BUNDLE2` / `L3DONTBNDL2` reasons; a protocol partner that is not the
cabled neighbour.  Partial (`1/2`) is a state, not a mismatch.  A member cable that CML has stopped (`CABLE_PULLED` / `LINK_NOT_STARTED`) never counts as
bundled, even while the switches still list it as `(P)` (in CML the far end keeps carrier; LACP / PAgP drop the
port only after their timeout): stopping both Po1 cables shows `Po1 · LACP 0/2` (down) at once.

**STP on the Port-channel.** `show spanning-tree` / `show interfaces trunk` list `Po1`, not the members:
`facts.port_l2()` resolves a bundled member (flag `P`) to its Port-channel, so `LinkFacts.l2_a/l2_b` (`.po = 'Po1'`)
carry the Po's trunk and STP role/state for display, and every member cable gets the Po's `stp_block` (the UI draws
it once, on the bundle).  An unbundled member (`I`/`s`) runs STP on its own and shows its own row.  IOS reports a
suspended member as up / line protocol down: the link status text becomes `'SW1 Gi0/1 suspended'`
(cause `'EtherChannel suspended'`).

**Probes** (`telemetry.command_sets(node, facts)`, all verified at `>` on IOSvL2, ~1 s each): `show etherchannel
summary` is in SLOW until a channel-group is seen, then in FAST; `show lacp internal / neighbor / sys-id` (LACP in
use) and `show pagp internal / neighbor` (PAgP in use) plus `show etherchannel load-balance` in SLOW; a new protocol
triggers an immediate SLOW read; an `%EC-*` syslog triggers FAST + SLOW.  An IOS router is asked only when its
interface list has a `Port-channel`.  No channel-group = no extra command.

**Events** (`kind 'ETHERCHANNEL'`, `proto 'ec'`, from bundle diffs; first sight = baseline):
`'SW1 Gi0/2 left Po1 (suspended)'` (warn) / `'… bundled into Po1'` (ok), `'SW1 Po1 down'` (error) / `'… up'` (ok),
`'Po1 SW1 ↔ SW2: protocol LACP → LACP≠on'`, `'Po1 SW1 ↔ SW2: ⚠ on ↔ LACP active — never bundles'` (warn) /
`'… mismatch cleared (…)'` (ok), `'EtherChannel removed (SW1 Po2, SW2 Po2)'`.

**UI** (layer `l2`): `ui.graphics.BundleItem` draws an oval lasso around the members at their midpoint (solid =
LACP, dashed = PAgP, dotted = static), one bead `Po1 · LACP 2/2` (shape: rounded LACP / pointed-ends PAgP / square
outlined static; amber `1/2`, red down), an amber `⚠ reason` bead (tier 0), `TRUNK 10,20` (tier 2), the members'
ports as one pill per end (tier 2) and ONE hatched `Po2 · Altn BLK` marker + slashed cap at the blocking end.
`ui.scene_model.link_fans` keeps a bundle's members adjacent (16 px) with a 38 px gap between bundles.  A member
that is not bundled is drawn muted-dashed with a ring badge carrying its flag (`s`, `I`, `D`; words on hover).
Clicking the lasso / bead selects the bundle → Inspector EtherChannel page.

### Actions (controller, blocking)

| call | what it does |
|---|---|
| `bundle_plan(link_keys, protocol='lacp')` | `etherchannel.BundlePlan`: both switches, members, defaults (LACP active/active, PAgP desirable/desirable), next free Po on both, the members' switchport lines (running-config > day-0 when it matches the live mode > live trunk/access facts), `check` (ModeCheck), `l2_conflict`, `console_open` per side |
| `bundle_apply(plan, live, day0)` | `telemetry.configure` on both switches (per-node lock, etiquette, write memory), then the day-0 patch (`etherchannel.patch_day0`; written now if the node is wiped, else stored in `state.json` `labs.<id>.day0_pending_ec` and written by `wipe()`) |
| `unbundle_plan(bundle_key)` / `unbundle_apply(plan, live, day0)` | `no channel-group` on the members (+ their switchport lines, `no shutdown`), `no interface Port-channelN`; day-0 `unpatch_day0` |

### `Segment` (`TelemetryView.segments[segment_id]`)

| field | meaning |
|---|---|
| `id` | |
| `kind` | `SegmentKind` |
| `switch_node` | |
| `members` | `[(node_key, iface label)]`, switch ports excluded |
| `link_keys` | |
| `protocols` | `{Proto: Health}` |
| `adjacencies` | `[(nodeA, nodeB, Proto, state)]` |
| `detail` | `{Proto: 'grp 10: Active R1, Standby R2'}` |
| `bgp_sessions` | `[SegBgpSession(a, b, kind 'iBGP'\|'eBGP'\|'BGP', as_a, as_b, established)]`: BGP sessions whose peer addresses are on this segment |

On `multi` segments (unmanaged switch), draw adjacencies as **chords between router tiles**,
never as rails on the router↔switch links. HSRP and VRRP health live here. The switch's BGP chip names
the session(s) crossing it (`iBGP R1↔R2`, `eBGP A↔B`, `iBGP ×2`; `facts.bgp_segment_chip_text`): a switch
never runs BGP itself.

### `RouteHop` / `DefaultPath` (D5; `TelemetryView.paths[node_key]`, `NodeTelemetry.default_path`)

`RouteHop` fields:

| field | meaning |
|---|---|
| `from_node` | |
| `to_node` | None when the hop is unresolved |
| `link_keys` | 1 key for a direct cable, 2 when the hop goes through a switch |
| `via_ip` | |
| `out_iface` | |
| `code` | |
| `ok` | |

`DefaultPath` fields:

| field | meaning |
|---|---|
| `origin` | |
| `state` | `PathState` |
| `hops` | |
| `broken_at` | link key |
| `broken_kind` | `LinkKind` |
| `egress_node` | |
| `egress_mode` | `'NAT'`, `'Bridge'` or `'Cloud'` (the LabDesk Internet cloud: a System-Bridge connector tagged `labdesk-cloud`) |
| `loop_pair` | |
| `detail` | plain words |
| `ecmp` | |

| `state` | UI |
|---|---|
| `EGRESS` | blue on every hop, egress tile labelled `'NAT egress · <addr>'` or `'Bridge egress'` |
| `VERIFIED_INTERNET` | same, and the egress tile reads `'INTERNET ✓ hh:mm'` (TTL 300 s) |
| `BROKEN_AT` | blue up to the last good hop, then a red ✕ at `broken_at`. If `broken_kind == UNKNOWN`, a neutral '?' instead |
| `STALE` | blue ends at the stale hop, with ⏱ |
| `NO_EGRESS`, `NONE`, `BLACKHOLE`, `UNRESOLVED`, `LOOP` | no blue, only the router chip |
| `UNKNOWN` | nothing (still learning) |

Blue is only ever painted from `LinkFacts.default_hop`. Only hops whose links are all `UP` get
one, so the UI never re-derives the path. Clicking the egress tile only flashes the path (UI
local). **Verify Internet** is the separate action below.

## 5. Traffic, conditions, events

- `LinkTraffic` fields:
  - `bps`, `pps`, `drops` (delta for the interval), `ts`.
  - `history`: up to 60 bps points, filled only for the selected link (sparkline).
  - `directional`: False, because the read/write direction is not yet verified.

  This is polled only while the Traffic layer is on (every 3 s) or a link is selected (every 10 s).
- `LinkCondition`:

  | field | meaning |
  |---|---|
  | `enabled` | |
  | `params` | applied values, non-zero only |
  | `label` | `'600 ms ±20 · 2 Mbit/s'` |
  | `ts` | |

  It is read from the API at startup, so the indicator is truthful.
- `TelemetryEvent`:

  | field | meaning |
  |---|---|
  | `seq` | |
  | `ts` | |
  | `device_ts` | router clock text from syslog |
  | `kind` | `LINK DOWN`, `LINK UP`, `FLAPPING`, `OSPF`, `BGP`, `HSRP`, `VRRP`, `DEFAULT`, `CABLE`, `SIM`, `SYSLOG`, `PROBE`, `STP`, `VLAN`, `TRUNK`, `SUBNET` or `ETHERCHANNEL` |
  | `proto` | hue key: `link`, `ospf`, `bgp`, `hsrp`, `vrrp`, `default`, `sim`, `probe`, `stp`, `vlan`, `trunk`, `subnet` or `ec` |
  | `severity` | `ok`, `info`, `warn` or `error` |
  | `text` | |
  | `items` | node and link keys to flash for 800 ms |
  | `line` | property: `'04:48:59 LINK DOWN R2 Gi0/4 ↔ R4 Gi0/2: R2 side shutdown'` |

  Fetch new events with `ctl.telemetry.events_since(last_seq)`. `TelemetryView.last_event_seq`
  tells you whether there is anything new. The D4 hysteresis is already applied: DOWN needs 2
  fresh CLI polls, or 1 syslog UPDOWN, or an API change. 3 or more transitions within 30 s
  collapse into a single `FLAPPING` entry. Every logged DOWN gets a matching `LINK UP`.

`TelemetryView` fields:

| field | meaning |
|---|---|
| `ts` | |
| `lab_id` | |
| `probing_enabled` | |
| `nodes` | |
| `links` | |
| `segments` | |
| `paths` | |
| `traffic` | |
| `conditions` | |
| `last_event_seq` | |
| `capabilities` | `{element_state_interfaces, link_conditioning, packet_capture, kvm_collectors}` |
| `rid_map` | |
| `fresh_s` | |
| `show_probe_notice` | show the one-time "probe output is visible on any open console" notice, then call `acknowledge_probe_notice()` |
| `bundles` | `{key: facts.Bundle}` EtherChannels (§4b) |

## 6. Actions (blocking: submit them through `TaskRunner`)

All are methods on `ctl.telemetry`. Each returns `ActionResult(ok, text, data)`.

| call | what it does |
|---|---|
| `set_condition(link_key, params=None, preset=None)` | D8 netem conditioning on a CML link. `presets = condition_presets()`, for example `'WAN 50 ms'`, `'Satellite 600 ms'`, `'Lossy 5 %'`, `'Bad Wi-Fi'`, `'DSL 8 Mbit/s'`, `'Congested 1 Mbit/s'`. `params` keys and limits come from `cml_api.LINK_CONDITION_LIMITS`. The applied values are read back. `data['condition']` holds the result |
| `clear_condition(link_key)` | removes conditioning |
| `pull_cable(link_key)` / `restore_cable(link_key)` | CML link stop/start. The link becomes `CABLE_PULLED` (API-certain) and both endpoints are probed immediately |
| `refresh(node_keys=None)` | toolbar Refresh (non-blocking): FAST probe now for the given / all collected nodes; a node whose console is open gets an immediate console-log read instead |
| `probe_now(node_key)` | the "Probe now (output appears in your console)" button. Works even while the node is paused |
| `refresh_config(node_key)` | Config tab Refresh. `data['text']` is the redacted config. If blocked, `ok=False` and `text` starts with `'console in use'` |
| `ping(node_key, target, count=5, source=None)` | `data` holds `{sent, received, loss_pct, rtt_min_ms, rtt_avg_ms, rtt_max_ms, success}` |
| `traceroute(node_key, target, max_ttl=8)` | `data['hops'] = [{hop, ip, name, node_key, link_keys, rtt_ms}]`. `link_keys` are the canvas links from the previous hop, for animating packet travel. The first outside hop maps to the egress node |
| *(note)* | At IOSv user EXEC (`>`, no enable secret stored) only a bare `ping X` / `traceroute X` is accepted, so the session sends that form: 5 pings, `count`/`source` are ignored, and a long trace is aborted with Ctrl-^ x before the timeout |
| `verify_internet(node_key, target=None, targets=('8.8.8.8', '1.1.1.1'))` | pings **every** target in one console burst (`data['targets'] = {ip: ping data}`, plus `success`/`sent`/`received` totals). Success = all answered: the router's path (and its egress) becomes `VERIFIED_INTERNET` for 300 s |
| `configure(node_key, lines, save=True, enable_secret=None)` | a user action (callers ask before typing into an open console): `configure terminal`, the lines (each waits for the config prompt; IOS `%` errors are reported and nothing is saved), `end`, `write memory` (a `[confirm]` is answered), then the FAST tier. The enable secret comes from device-creds, else `enable_secret` (`'cisco'` for Auto-configure). `probe.session.run_burst(config_lines=…, save_config=…)` does the console part |

Settings, all non-blocking:

| call | effect |
|---|---|
| `set_probing(bool)` | the global toolbar switch |
| `set_probe_while_open(node_key, bool)` | the per-node override, behind a warning |
| `set_traffic_layer(bool)` | turns traffic polling on or off |
| `set_selected_link(key \| None)` | enables the 10 s sparkline polling |
| `acknowledge_probe_notice()` | dismisses the one-time notice |

Lifecycle:

- `ctl.shutdown()`: call it on application exit.
- `ctl.telemetry.on_change = callable`: called from a probe thread when a burst finished. The
  UI can use it to request an early `poll()` through a queued signal. Never touch widgets from
  it.

Helper: `telemetry.host_mem_available_gb()` reads MemAvailable for the V8 'Host RAM free'
label. It is cheap and safe on the GUI thread.

### Internet cloud (`labdesk_app.cloud`, `LabController.cloud_plan / cloud_apply`)

Not part of `TelemetryView`: the host service is checked by the UI every `cloud.HEALTH_INTERVAL_MS` (10 s) while
the lab has a node of type `ext_cloud` (`cloud.check_health()` on the TaskRunner -> `cloud.CloudHealth`: `state`
`up|degraded|down|stopped|not_installed|unknown`, `answers`, `selfping`, `host_ping`, `edge_gw`, `routes`,
`problem`, `headline` (`'cloud up — answers 8.8.8.8, 1.1.1.1'`), `hint` (`'sudo systemctl start labdesk-cloud'`),
`tooltip_lines()`). The pure `cloud.health_from(status_text, ns_exists=, host_ping=, installed=)` combines the
root-written `/run/labdesk-cloud/status` with the app's own checks. `RenderCtx.cloud_health` carries it to the
canvas. `LabController.cloud_plan(router_key)` -> `CloudPlan` (interface, next free `100.64.0.x`, OSPF processes,
console open, day-0 state, `needs_wipe` when CML has not wired the cable into the running VM, `routes: RoutePlan`);
`cloud_apply(plan, live, day0, ospf, routes, restart)` -> `[(ok, text)]`. A day-0 patch for a booted node is kept in
`state.json` (`labs.<id>.day0_pending`) and written into CML by `LabController.wipe()` / `wipe_all()`.

## 7. Cadence, etiquette, safety (D1/D2/D7/D10)

**Polling tiers per router (start-to-start):**

- **FAST, 15 s:**
  - `show ip interface brief`
  - `show ip ospf interface brief`
  - `show ip ospf neighbor`
  - `show standby brief`
  - `show vrrp brief`
  - `show ip bgp summary`
  - `show ip route 0.0.0.0`
  - `show ip cef 0.0.0.0/0`
  - `show ip interface | include …` (addresses, secondaries, method - `parsers.ios.IP_ADDR_CMD`, also in `SW_FAST`)

  This takes about 6 s on the serial console. Everything runs at `>`, so no enable is needed.

  **Managed switches** (`telemetry.SW_FAST` / `SW_SLOW`, burst timeout 30 s; all verified at `>`):
  - FAST, 15 s, about 7 s: `show ip interface brief`, `show interfaces status`, `show interfaces trunk`,
    `show spanning-tree`, `show vlan brief`, `show spanning-tree detail | include ^ VLAN|occurr|from`
    (topology-change counters).
  - SLOW, 60 s, about 9 s: `show spanning-tree summary`, `show mac address-table count`,
    `show mac address-table dynamic`, `show cdp neighbors`, `show etherchannel summary`,
    `show interfaces description`, `show ip route connected` (also tells whether `ip routing` is on).
  - EtherChannel (only once a channel-group exists, §4b): `show etherchannel summary` moves to FAST (+~1.5 s);
    SLOW adds `show lacp internal`, `show lacp neighbor`, `show lacp sys-id` (LACP), `show pagp internal`,
    `show pagp neighbor` (PAgP) and `show etherchannel load-balance` (+~5 s; the burst timeout grows by 10 s).
  - STP / VLAN / TRUNK events come from diffs between two bursts (`TelemetryEngine._l2_diff_events`).
- **SLOW, 60 s:**
  - `show interfaces description`
  - `show ip protocols`
  - `show ip route connected`
- **CONFIG:**
  - `show running-config`, redacted.
  - It runs once after learning, but only if the line was found at `#` or an enable secret is
    stored. It also runs after a `%SYS-5-CONFIG_I` syslog, every 10 min, and on demand.

**Freshness:**

- Facts older than `FRESH_S` = 45 s (2 × FAST + 15 s grace; measured worst-case age on this host was 34 s at 2 × FAST = 30 s, which flickered) make link sides `UNKNOWN`. A stale "shut" can never survive
  a `no shut`.
- Chip glyphs become ⏱ and the blue path ends at that point.

**Bursts:**

- At most 3 run in parallel.
- The hard timeout is 20 s, 45 s for config (FAST + `enable` + `show running-config` measured ~27 s on IOSv;
  the running-config itself gets a 40 s per-command limit) and 30 s for trace. A timeout kills ssh and sends a single
  Ctrl-C at the start of the next burst.
- Backoff is exponential up to 120 s.

**Etiquette:**

- **Console in use:** while an ssh process with `connect <console-uuid>` runs on this host,
  that node is `paused_console`. This covers LabDesk Konsole tabs and the user's own ssh. It is
  detected by a /proc scan that excludes LabDesk's own probe processes. While paused, only the console
  tap (CML console-log REST read) runs for it. When the console closes, the node gets an immediate FAST
  burst (still subject to the foreign-session checks below) and one last tap read.
- **Foreign session:** a session found in config mode, at `--More--`, `[confirm]`, `Password:`,
  rommon or the boot dialogue means `console_busy`. Nothing is typed.
- **Restoring the line:** terminal length and width are restored. `disable` is sent only if
  LabDesk enabled. `exit` is sent only if the line was found logged out.

**Credentials:**

- Device credentials live in `~/.config/labdesk/device-creds` (mode 0600, `DEFAULT_ENABLE=…`,
  `R1_ENABLE=…`; see `probe/creds.py`).
- Without that file, everything except `show running-config` from `>` still works.
- The UI writes it (right-click ▸ *Credentials…*, `creds.save`: atomic, 0600, other lines kept;
  `creds.load_detail` tells per-node from `DEFAULT_*`). `TelemetryEngine.credentials_changed(label)` drops the
  secrets cache and schedules a CONFIG read (staggered) so on-device accounts appear without extra commands.
- On-device accounts (`accounts.parse_ios_accounts`) come from the CONFIG tier's `show running-config`; plain-text
  passwords found there are added to the literal redaction list before the probe log is written, and syslog
  event text is literally redacted too. Junos / FortiOS: `telemetry.JUNOS_COMMANDS["accounts"]`,
  `FORTIOS_COMMANDS["accounts"]` (parsers ready, collector not wired).

**Logs and redaction:**

- Probe logs are in `~/.cache/labdesk/probe/<label>.log` (0600, last 50 bursts). They are
  redacted, and literal secret values are removed.
- Syslog lines are stripped from outputs and logged as `SYSLOG` events with the device
  timestamp.

**API polling (D7):**

- Most polls are a light `GET lab` + `GET lab_element_state` (about 15 ms each).
- A full snapshot runs on start, on lab change, after any mutating controller call, every
  30 s, on an element-set change, and after a clock gap.
- `lab_element_state` on 2.10.1 includes interfaces. This was verified, and fixtures are in
  `tests/fixtures/api/`.

## 8. Host-KVM (Junos / FortiOS)

(Switching parsers: `parsers/ios_l2.py`, fixtures `tests/fixtures/switching/`, replay
`tests/switch_replay.py`.)

Parsers and fixtures ship in `parsers/junos.py` and `parsers/fortios.py`. Fixtures are in
`tests/fixtures/junos` (real vJunos captures plus `*.SYNTHETIC.*`) and `tests/fixtures/fortios`
(`*.SYNTHETIC.*`).

**Fact builders:**

- `facts.junos_facts_update` and `facts.fortios_facts_update`.
- The P2P sources they set are `JUNOS_PTTOPT` and `FORTIOS_P2P`.

**Command sets:** `telemetry.JUNOS_COMMANDS` and `telemetry.FORTIOS_COMMANDS`.

**Transport:** `probe/transport.ssh_burst_argv` builds key-only ssh with ControlPersist.

**NIC naming:** `catalog.guest_ifname(kind, nic)` and `catalog.nic_for_ifname(kind, name)`
give the provisioning defaults:

- vJunos: nic0 is fxp0, and nic N+1 is ge-0/0/N.
- FortiOS: nic N-1 is portN.

**Not live yet:** the KVM collector is not wired. KVM routers appear with
`collector_state = unverified` (show 'beta'). Their link ends are `NOT_APPLICABLE`, so a
KVM link shows `UP` (both VMs running) without guest oper state. With zero KVM devices, no
virsh call, timer or UI element exists. `tests/test_telemetry_engine.py::test_no_kvm_means_no_virsh`
enforces this.

## 9. Pure helpers (for tests or the Inspector)

- `facts.join_facts(topo, facts, link_history, now, fresh_s)` returns a `JoinResult` with
  `links`, `segments`, `paths`, `rid_map` and `ip_owner`.
- `facts.link_status(link, topo, facts, link_history, now, fresh_s)`.
- `facts.node_summary(...)`.
- `facts.build_segments(topo)`.
- `facts.build_bundles(...)`, `facts.ec_facts_update(...)`, `etherchannel.check_modes / bundle_commands /
  unbundle_commands / patch_day0 / unpatch_day0 / day0_has_bundle`, `parsers.ios_l2.parse_lacp_* / parse_pagp_* /
  parse_etherchannel_* / ec_syslog_reason` (fixtures: `tests/fixtures/etherchannel/`, replay `tests/ec_replay.py`).
- `facts.link_addressing(...)`, `facts.effective_addrs(nf)`, `facts.apply_addr_cmd(...)`, `facts.subnet_changes(...)`,
  `facts.host_part / short_net / compact_net / dhcp_net`, `parsers.console_tap.parse_log / feed`,
  `ui.scene_model.place_stacks(entries, obstacles, strokes=...)`.
- `telemetry.topology_from_snapshot(lab, nodes, links, interfaces, kvm_nodes, kvm_links, kvm_states)`.
- `tools/telemetry_snapshot.py [--lab "Protocol Showcase"] [--json]` prints the live model.
