"""Capture the Switching Lab fixtures (real outputs at the user-exec '>' prompt, like the probes).

    .venv/bin/python tests/fixtures/switching/_tools/capture.py [--prefix after_shut_]
"""
import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parents[2]))
from con import Con, LAB  # noqa: E402
from showcase_replay import slug  # noqa: E402

from labdesk_app.cml_api import CMLClient  # noqa: E402

OUT = HERE.parent
CMDS = ["show vlan brief", "show interfaces trunk", "show interfaces status", "show spanning-tree",
        "show spanning-tree summary", "show mac address-table count", "show mac address-table dynamic",
        "show ip interface brief", "show cdp neighbors", "show etherchannel summary",
        "show spanning-tree detail | include ^ VLAN|occurr|from", "show ip route connected",
        "show interfaces description"]


def main():
    prefix = sys.argv[sys.argv.index("--prefix") + 1] if "--prefix" in sys.argv else ""
    nodes = [a for a in sys.argv[1:] if a in ("SW1", "SW2", "R1")] or ["SW1", "SW2", "R1"]
    manifest_p = OUT / "manifest.json"
    manifest = json.loads(manifest_p.read_text()) if manifest_p.exists() else {}
    from labdesk_app.telemetry import IOS_FAST, IOS_SLOW, SW_FAST, SW_SLOW
    for n in nodes:
        c = Con(n, enable=False)
        m = manifest.setdefault(prefix + n, {})
        extra = IOS_FAST + IOS_SLOW if n.startswith("R") else SW_FAST + SW_SLOW
        for cmd in CMDS + [x for x in extra if x not in CMDS]:
            t = time.time()
            out = c.run(cmd)
            f = f"{prefix}{n}__{slug(cmd)}.txt"
            (OUT / f).write_text(out)
            m[cmd] = {"file": f, "seconds": round(time.time() - t, 2), "lines": out.count("\n"), "mode": ">"}
            print(f"{n} {cmd}: {m[cmd]['seconds']} s, {m[cmd]['lines']} lines")
        c.close()
    manifest_p.write_text(json.dumps(manifest, indent=1))
    if not prefix or "--snapshot" in sys.argv:
        cl = CMLClient()
        lab = cl.find_lab(LAB)
        snap = cl.snapshot(lab["id"])
        (OUT / "cml_snapshot.json").write_text(json.dumps(
            {"lab": snap.lab, "nodes": snap.nodes, "links": snap.links, "interfaces": snap.interfaces}, indent=1))
        print("snapshot saved")


if __name__ == "__main__":
    main()
