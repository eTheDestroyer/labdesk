"""Interactive console helper for the Switching Lab (capture + live checks; not used by the app).

    .venv/bin/python tests/fixtures/switching/_tools/console.py SW2 [--user] "show spanning-tree" ...
    .venv/bin/python tests/fixtures/switching/_tools/console.py SW1 --cfg "interface Gi0/1" "shutdown"

Goes through the CML terminal server exactly like LabDesk's probes (ssh ... connect <uuid>).
--user stays at '>' (checks what works for the probes); default enables (secret 'cisco').
"""
import re
import sys
import time
from pathlib import Path

import pexpect

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))
from labdesk_app import launchers  # noqa: E402
from labdesk_app.cml_api import CMLClient  # noqa: E402

ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
LAB = "Switching Lab"


def console_uuid(label: str) -> str:
    c = CMLClient()
    lab = c.find_lab(LAB)
    for n in c.nodes(lab["id"]):
        if n["label"] == label:
            return c.console_key(lab["id"], n["id"])
    raise SystemExit(f"no node {label}")


class Con:
    def __init__(self, name: str, enable: bool = True):
        self.name = name
        argv = launchers.cml_ssh_base("admin") + ["connect", console_uuid(name)]
        self.p = pexpect.spawn(argv[0], argv[1:], timeout=30, maxread=200000,
                               env={"PATH": "/usr/bin:/bin", "TERM": "vt100"})
        self.p.expect([rb"Escape character is.*\n", pexpect.TIMEOUT], timeout=15)
        self.prompt = re.compile(rf"\r?\n{re.escape(name)}(\([\w\-]+\))?([>#]) ?$".encode())
        self.sync(enable)

    def drain(self):
        time.sleep(0.3)
        try:
            self.p.read_nonblocking(100000, timeout=0.3)
        except Exception:
            pass

    def sync(self, enable):
        for _ in range(8):
            self.p.send("\r")
            i = self.p.expect([rb"[>#] ?$", rb"\[yes/no\]", rb"RETURN to get started", rb"--More--",
                               rb"Password: ?$", pexpect.TIMEOUT], timeout=5)
            if i == 0:
                self.drain()
                break
            if i == 1:
                self.p.sendline("no")
            if i == 3:
                self.p.send("q")
            if i == 4:                      # someone's half-typed enable: let it fail back to '>'
                self.p.send("\r")
        self.p.send("end\r")
        self.p.expect(rb"[#>]")
        self.drain()
        if enable:
            self.p.sendline("enable")
            i = self.p.expect([rb"Password:", rb"#\s*$", rb">\s*$"], timeout=5)
            if i == 0:
                self.p.sendline("cisco")
                self.p.expect(rb"#\s*$")
        else:
            self.p.sendline("disable")
            self.p.expect(rb">\s*$")
        for c in ("terminal length 0", "terminal width 0"):
            self.p.sendline(c)
            self.p.expect(rb"[#>]\s*$")
        self.drain()

    def run(self, cmd, timeout=60):
        self.p.sendline(cmd)
        self.p.expect(re.escape(cmd.encode()[-20:]), timeout=timeout)
        self.p.expect(self.prompt, timeout=timeout)
        out = self.p.before.decode("utf-8", "replace")
        out = ANSI.sub("", out).replace("\r\n", "\n").replace("\r", "")
        return out.lstrip("\n")

    def cfg(self, lines):
        self.p.sendline("configure terminal")
        self.p.expect(rb"\(config\)#")
        for ln in lines:
            self.p.sendline(ln)
            self.p.expect(rb"\)#\s*$")
        self.p.sendline("end")
        self.p.expect(rb"#\s*$")

    def close(self):
        try:
            for c in ("terminal length 24", "terminal width 80"):
                self.p.sendline(c)
                self.p.expect(rb"[#>]\s*$", timeout=5)
            self.p.sendline("disable")
            self.p.expect(rb">\s*$", timeout=5)
        except Exception:
            pass
        self.p.sendcontrol("]")
        self.p.close(force=True)


if __name__ == "__main__":
    args = sys.argv[1:]
    name = args.pop(0)
    user = "--user" in args
    args = [a for a in args if a != "--user"]
    c = Con(name, enable=not user)
    if args and args[0] == "--cfg":
        c.cfg(args[1:])
        print("configured")
    else:
        for cmd in args:
            print(f"### {cmd}\n{c.run(cmd)}")
    c.close()
