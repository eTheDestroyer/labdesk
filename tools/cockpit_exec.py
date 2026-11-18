#!/usr/bin/env python3
"""Run a whitelisted CML maintenance command on the controller through Cockpit.

CML exposes some global settings (e.g. "SSH terminal server public key authentication")
only in its Cockpit plugin, which runs ``sudo /usr/share/cockpit/virl2/manage-compute.sh ...``.
This helper logs into Cockpit as CML_SYSADMIN_USER (credentials read from the env file,
never printed) and runs the same script over Cockpit's websocket "stream" channel.

Usage:  cockpit_exec.py --get-defaults
        cockpit_exec.py --set-defaults sshPubkey on
        cockpit_exec.py --start-virl2-target          (restart CML services)
"""
from __future__ import annotations

import base64
import http.cookiejar
import json
import os
import ssl
import sys
import urllib.request

import websocket  # websocket-client

HOST = os.environ.get("CML_HOST", "192.168.122.60")
CRED_FILE = os.path.expanduser(os.environ.get("CML_CRED_FILE", "~/cml-labs/cml-credentials.env"))
SCRIPT = "/usr/share/cockpit/virl2/manage-compute.sh"
ALLOWED = {"--get-defaults", "--set-defaults", "--start-virl2-target"}


def _creds() -> tuple[str, str]:
    vals = {}
    with open(CRED_FILE, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                vals[k.strip()] = v.strip().strip('"').strip("'")
    return vals["CML_SYSADMIN_USER"], vals["CML_SYSADMIN_PASS"]


def run(args: list[str], timeout: float = 600) -> tuple[str, dict]:
    if not args or args[0] not in ALLOWED:
        raise SystemExit(f"only {sorted(ALLOWED)} are allowed")
    user, pw = _creds()
    ctx = ssl._create_unverified_context()
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=ctx),
                                         urllib.request.HTTPCookieProcessor(jar))
    auth = base64.b64encode(f"{user}:{pw}".encode()).decode()
    opener.open(urllib.request.Request(f"https://{HOST}:9090/cockpit/login",
                                       headers={"Authorization": "Basic " + auth}), timeout=15)
    cookie = "; ".join(f"{c.name}={c.value}" for c in jar)
    ws = websocket.create_connection(f"wss://{HOST}:9090/cockpit/socket", cookie=cookie,
                                     sslopt={"cert_reqs": ssl.CERT_NONE, "check_hostname": False},
                                     header=["Origin: https://" + HOST + ":9090"],
                                     subprotocols=["cockpit1"], timeout=timeout)
    ws.send("\n" + json.dumps({"command": "init", "version": 1, "host": "localhost"}))
    ws.send("\n" + json.dumps({"command": "open", "channel": "c1", "payload": "stream",
                               "spawn": ["/usr/bin/sudo", SCRIPT, *args], "err": "out"}))
    out, close = [], {}
    while True:
        msg = ws.recv()
        if isinstance(msg, bytes):
            msg = msg.decode("utf-8", "replace")
        chan, _, body = msg.partition("\n")
        if chan == "c1":
            out.append(body)
        elif chan == "":
            ctl = json.loads(body)
            if ctl.get("command") == "close" and ctl.get("channel") == "c1":
                close = ctl
                break
            if ctl.get("command") == "ping":
                ws.send("\n" + json.dumps({"command": "pong"}))
    ws.close()
    return "".join(out), close


if __name__ == "__main__":
    text, ctl = run(sys.argv[1:])
    print(text)
    print("exit:", ctl.get("exit-status"), ctl.get("problem", ""))
