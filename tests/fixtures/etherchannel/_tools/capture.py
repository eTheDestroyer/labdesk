"""Capture the EtherChannel fixtures from the running "Switching Lab" (real IOSvL2 outputs).

    .venv/bin/python tests/fixtures/etherchannel/_tools/capture.py STATE [SW1 SW2]

STATE names the lab situation (healthy, partial, fail_on, fail_proto, ...); files are written as
``<STATE>__<node>__<command slug>.txt`` plus an entry in ``manifest.json`` (time, line count).  The command
list is the probe set (telemetry.SW_FAST + SW_SLOW + EC set) plus the heavy show commands the probes never run
(etherchannel detail, interfaces port-channel N, running-config section).  Goes through the CML terminal
server like LabDesk's probes (tests/fixtures/switching/_tools/con.py).
"""
import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[3]
sys.path.insert(0, str(ROOT / "tests" / "fixtures" / "switching" / "_tools"))
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT))
from con import Con  # noqa: E402
from showcase_replay import slug  # noqa: E402

OUT = HERE.parent
EC = ["show etherchannel summary", "show etherchannel detail", "show lacp neighbor", "show lacp internal",
      "show lacp sys-id", "show pagp neighbor", "show pagp internal", "show etherchannel load-balance",
      "show interfaces port-channel 1", "show interfaces port-channel 2", "show interfaces trunk",
      "show spanning-tree", "show running-config | section Port-channel|channel-group"]


def main():
    args = [a for a in sys.argv[1:]]
    state = args.pop(0)
    nodes = [a for a in args if a in ("SW1", "SW2", "R1")] or ["SW1", "SW2"]
    from labdesk_app.telemetry import IOS_FAST, IOS_SLOW, SW_FAST, SW_SLOW
    sw_cmds = EC + [c for c in SW_FAST + SW_SLOW if c not in EC]
    mp = OUT / "manifest.json"
    manifest = json.loads(mp.read_text()) if mp.exists() else {}
    for n in nodes:
        cmds = sw_cmds if n.startswith("SW") else IOS_FAST + IOS_SLOW
        c = Con(n, enable=True)
        m = manifest.setdefault(f"{state}__{n}", {"captured": time.strftime("%Y-%m-%d %H:%M:%S")})
        for cmd in cmds:
            t = time.time()
            out = c.run(cmd, timeout=90)
            f = f"{state}__{n}__{slug(cmd)}.txt"
            (OUT / f).write_text(out)
            m[cmd] = {"file": f, "seconds": round(time.time() - t, 2), "lines": out.count("\n")}
            print(f"{state} {n} {cmd}: {m[cmd]['seconds']} s, {m[cmd]['lines']} lines", flush=True)
        c.close()
    mp.write_text(json.dumps(manifest, indent=1))


if __name__ == "__main__":
    main()
