"""Device palette: what can be dropped on the canvas."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class DeviceType:
    key: str
    title: str
    vendor: str            # cisco | juniper | fortinet | linux | generic
    shape: str             # router | switch | host | cloud | firewall | hub
    backend: str           # cml | kvm
    prefix: str            # default label prefix (R -> R1, R2 ...)
    node_definition: str = ""
    configuration: str = ""       # default CML configuration (external connector mode etc.)
    note: str = ""
    counts_against_limit: bool = True
    tags: tuple = field(default_factory=tuple)


IOS_BASE_CONFIG = """hostname {label}
no ip domain lookup
username admin privilege 15 secret cisco
enable secret cisco
line con 0
 exec-timeout 0 0
 logging synchronous
line vty 0 4
 login local
 transport input ssh telnet
end
"""

DEVICE_TYPES: list[DeviceType] = [
    DeviceType("iosv", "Cisco IOSv router", "cisco", "router", "cml", "R", "iosv",
               note="IOS 15.9 - CML node"),
    DeviceType("iosvl2", "Cisco IOSvL2 switch (managed)", "cisco", "switch", "cml", "SW", "iosvl2",
               note="Managed L2/L3 switch - VLANs, trunks and spanning tree are read live"),
    DeviceType("iol-xe", "Cisco IOL-XE router", "cisco", "router", "cml", "XE", "iol-xe",
               note="IOS-XE 17.18 (IOL) - light weight"),
    DeviceType("alpine", "Alpine Linux host", "linux", "host", "cml", "H", "alpine",
               note="Linux end host - CML node"),
    DeviceType("unmanaged_switch", "Unmanaged switch", "generic", "hub", "cml", "US",
               "unmanaged_switch", note="Dumb L2 hub: no VLANs, no STP, no console. Does not count against the "
                                        "5-node limit",
               counts_against_limit=False),
    DeviceType("ext_cloud", "Internet cloud (always answers 8.8.8.8 / 1.1.1.1)", "generic", "cloud", "cml", "INTERNET",
               "external_connector", configuration="System Bridge",
               note="Simulated Internet on this host (netns labcloud, 100.64.0.1/24): always answers ping to "
                    "8.8.8.8, 1.1.1.1, 9.9.9.9 and 208.67.222.222 - even with no real Internet. A System-Bridge "
                    "connector: does not count against the 5-node limit",
               counts_against_limit=False, tags=("labdesk-cloud",)),
    DeviceType("ext_nat", "NAT connector", "generic", "cloud", "cml", "NAT",
               "external_connector", configuration="NAT",
               note="Internet via the CML VM (NAT)", counts_against_limit=False),
    DeviceType("ext_bridge", "System-Bridge connector", "generic", "cloud", "cml", "BR",
               "external_connector", configuration="System Bridge",
               note="L2 bridge to host virbr0 (192.168.122.0/24)", counts_against_limit=False),
    DeviceType("vjunos", "Juniper vJunos-router", "juniper", "router", "kvm", "JR",
               note="Host KVM VM: 4 vCPU / 5 GB RAM", counts_against_limit=False),
    DeviceType("fortios", "Fortinet FortiOS (unlicensed)", "fortinet", "firewall", "kvm", "FGT",
               note="Host KVM VM: unlicensed - routing/OSPF ok, does NOT forward traffic",
               counts_against_limit=False),
]

BY_KEY = {d.key: d for d in DEVICE_TYPES}
NON_COUNTING_DEFS = {"unmanaged_switch", "external_connector"}


def type_for_cml_node(node: dict) -> DeviceType:
    """Map a CML node dict to a palette type (for drawing)."""
    nd = node.get("node_definition", "")
    if nd == "external_connector":
        if "labdesk-cloud" in (node.get("tags") or []):
            return BY_KEY["ext_cloud"]
        cfg = node.get("configuration")
        if isinstance(cfg, list):
            cfg = " ".join(str(c.get("content", "")) for c in cfg if isinstance(c, dict))
        return BY_KEY["ext_nat"] if "NAT" in str(cfg or "") else BY_KEY["ext_bridge"]
    for d in DEVICE_TYPES:
        if d.backend == "cml" and d.node_definition == nd:
            return d
    if nd in ("ioll2-xe",):
        return BY_KEY["iosvl2"]
    if nd in ("ubuntu", "server"):
        return BY_KEY["alpine"]
    if nd in ("asav",):
        return DeviceType("asav", "Cisco ASAv", "cisco", "firewall", "cml", "ASA", "asav")
    return DeviceType(nd or "unknown", nd or "unknown", "generic", "host", "cml", "N", nd)


def default_config(dtype: DeviceType, label: str) -> str:
    if dtype.node_definition in ("iosv", "iosvl2", "iol-xe", "ioll2-xe"):
        return IOS_BASE_CONFIG.format(label=label)
    if dtype.node_definition == "alpine":
        return f"hostname {label}\n"
    return dtype.configuration


def next_label(prefix: str, existing: set[str]) -> str:
    i = 1
    while f"{prefix}{i}" in existing:
        i += 1
    return f"{prefix}{i}"


# ------------------------------------------------------------------ host-KVM NIC naming (D6)
def guest_ifname(kind: str, nic_index: int) -> str:
    """Provisioning default for the guest interface name of libvirt NIC ``nic_index`` (0-based,
    domiflist order).  vJunos: nic0 = fxp0, nic N+1 = ge-0/0/N.  FortiOS: nic N-1 = portN.
    The live mapping is joined by MAC (D6); this arithmetic is only the default."""
    if nic_index < 0:
        raise ValueError("nic_index must be >= 0")
    if kind == "vjunos":
        return "fxp0" if nic_index == 0 else f"ge-0/0/{nic_index - 1}"
    if kind == "fortios":
        return f"port{nic_index + 1}"
    raise ValueError(f"unknown KVM kind {kind!r}")


def nic_for_ifname(kind: str, name: str) -> int:
    """Inverse of guest_ifname()."""
    import re as _re
    if kind == "vjunos":
        if name == "fxp0":
            return 0
        m = _re.fullmatch(r"ge-0/0/(\d+)(?:\.0)?", name)
        if m:
            return int(m.group(1)) + 1
    elif kind == "fortios":
        m = _re.fullmatch(r"port(\d+)", name)
        if m and int(m.group(1)) >= 1:
            return int(m.group(1)) - 1
    raise ValueError(f"{name!r} is not a {kind} data/management interface")
