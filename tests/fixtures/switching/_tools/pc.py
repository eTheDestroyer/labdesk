"""Run shell commands on an alpine PC console of the Switching Lab (login cisco/cisco if asked)."""
import re
import sys
import time
from pathlib import Path

import pexpect

sys.path.insert(0, str(Path(__file__).resolve().parent))
from console import ANSI, console_uuid, launchers  # noqa: E402


def run(name, cmds):
    argv = launchers.cml_ssh_base("admin") + ["connect", console_uuid(name)]
    p = pexpect.spawn(argv[0], argv[1:], timeout=30, env={"PATH": "/usr/bin:/bin", "TERM": "vt100"})
    p.expect([rb"Escape character is.*\n", pexpect.TIMEOUT], timeout=15)
    shell = rb"[$#] ?(?:\x1b\[6n)?$"
    p.send("\r")
    i = p.expect([rb"login: ?$", shell, pexpect.TIMEOUT], timeout=8)
    if i != 1:
        if i == 2:
            p.send("\r")
            p.expect(rb"login: ?$", timeout=8)
        time.sleep(0.3)
        p.sendline("cisco")
        p.expect(rb"[Pp]assword: ?$", timeout=10)
        time.sleep(0.3)
        p.sendline("cisco")
        p.expect(shell, timeout=15)
    out = []
    for c in cmds:
        p.sendline(c + "; echo __DO''NE__")
        p.expect(rb"\n__DONE__", timeout=60)
        txt = ANSI.sub("", p.before.decode("utf-8", "replace")).replace("\r", "")
        out.append(f"### {c}\n{txt.split(chr(10), 1)[-1] if chr(10) in txt else txt}")
    p.sendline("exit")
    time.sleep(0.5)
    p.sendcontrol("]")
    p.close(force=True)
    return "\n".join(out)


if __name__ == "__main__":
    print(run(sys.argv[1], sys.argv[2:]))
