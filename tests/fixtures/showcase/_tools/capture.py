import re, json, time, sys
from pathlib import Path
from console_runner import Con
OUT = Path("/home/lf/cml-labs/labdesk/tests/fixtures/showcase"); OUT.mkdir(parents=True, exist_ok=True)
CMDS = ["show running-config", "show ip interface brief",
 "show interfaces | include (line protocol|Description)",
 "show interfaces description",
 "show ip ospf interface brief", "show ip ospf neighbor", "show ip ospf interface",
 "show standby brief", "show standby", "show vrrp brief", "show vrrp",
 "show ip bgp summary", "show ip bgp", "show ip route", "show ip route 0.0.0.0",
 "show cdp neighbors", "show cdp neighbors detail", "show ip protocols"]
EXTRA = {"R4": ["show ip nat translations", "show ip nat statistics", "show dhcp lease"],
         "R1": ["ping 8.8.8.8", "traceroute 8.8.8.8 numeric timeout 1 ttl 1 6"]}
def slug(c): return re.sub(r"[^a-z0-9]+", "_", c.lower()).strip("_")
manifest = {}
for r in ["R1","R2","R3","R4"]:
    c = Con(r); manifest[r] = {"_connect_s": round(c.connect_time,2)}
    for cmd in CMDS + EXTRA.get(r, []):
        o, t = c.run(cmd, timeout=90)
        f = OUT / f"{r}__{slug(cmd)}.txt"
        f.write_text(o.rstrip() + "\n")
        manifest[r][cmd] = {"file": f.name, "seconds": round(t,2), "lines": o.count("\n")}
        print(r, f"{t:5.2f}s", cmd, flush=True)
    c.close()
(OUT/"manifest.json").write_text(json.dumps(manifest, indent=1))
