"""Run a command on the CML controller VM through its Cockpit web console (port 9090).

CML's own SSH (port 1122) is off by default; Cockpit is always there.  LabDesk uses it for the few things
that live INSIDE the CML VM:

* Windows (VMware): the Internet cloud runs inside the CML VM (``labdesk-cloud`` service, started only
  when ``systemd-detect-virt`` says *vmware*).  LabDesk reads its status file and writes its routes file
  through Cockpit, exactly as it reads / writes the files on a Linux host.
* ``tools/make_windows_bundle.py``: prepares the CML VM for VMware (fixed NIC names, the cloud service).

Login: CML_SYSADMIN_USER / CML_SYSADMIN_PASS from the credentials file (never logged).  ``sudo=True``
runs the command as root with ``sudo -S`` - the password goes on the process's stdin, never on a
command line.  Stdlib + websocket-client only (a LabDesk dependency on every platform).
"""
from __future__ import annotations

import base64
import http.cookiejar
import json
import ssl
import urllib.error
import urllib.request
from dataclasses import dataclass

from . import config


class CockpitError(RuntimeError):
    pass


@dataclass
class Result:
    status: int | None
    output: str
    problem: str = ""

    @property
    def ok(self) -> bool:
        return self.status == 0 and not self.problem


def sysadmin_login(cred_file=None) -> tuple[str, str]:
    try:
        vals = config.load_env_file(cred_file or config.CRED_FILE)
    except OSError as e:
        raise CockpitError(f"credentials file not readable: {e}") from None
    user, pw = vals.get("CML_SYSADMIN_USER") or "sysadmin", vals.get("CML_SYSADMIN_PASS")
    if not pw:
        raise CockpitError("no CML_SYSADMIN_PASS in the credentials file (CML's Cockpit login)")
    return user, pw


class Cockpit:
    def __init__(self, host: str | None = None, user: str | None = None, password: str | None = None,
                 port: int = 9090, timeout: float = 60.0, cred_file=None):
        if user is None or password is None:
            user, password = sysadmin_login(cred_file)
        self.host = host or config.CML_HOST
        self.port = port
        self.user, self._pw = user, password
        self.timeout = timeout

    def __repr__(self) -> str:                       # never the password
        return f"Cockpit({self.user}@{self.host}:{self.port})"

    def _connect(self):
        import websocket                              # websocket-client
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        jar = http.cookiejar.CookieJar()
        opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=ctx),
                                             urllib.request.HTTPCookieProcessor(jar))
        base = f"https://{self.host}:{self.port}"
        auth = base64.b64encode(f"{self.user}:{self._pw}".encode()).decode()
        try:
            opener.open(urllib.request.Request(base + "/cockpit/login", headers={"Authorization": "Basic " + auth}),
                        timeout=15)
        except urllib.error.HTTPError as e:
            raise CockpitError("Cockpit refused the CML sysadmin login" if e.code in (401, 403)
                               else f"Cockpit login: HTTP {e.code}") from None
        except (urllib.error.URLError, OSError) as e:
            raise CockpitError(f"Cockpit on {self.host}:{self.port} unreachable ({getattr(e, 'reason', e)})") from None
        cookie = "; ".join(f"{c.name}={c.value}" for c in jar)
        try:
            ws = websocket.create_connection(f"wss://{self.host}:{self.port}/cockpit/socket", cookie=cookie,
                                             sslopt={"cert_reqs": ssl.CERT_NONE, "check_hostname": False},
                                             header=["Origin: " + base], subprotocols=["cockpit1"],
                                             timeout=self.timeout)
        except Exception as e:  # noqa: BLE001
            raise CockpitError(f"Cockpit socket: {e}") from None
        ws.send("\n" + json.dumps({"command": "init", "version": 1, "host": "localhost"}))
        return ws

    def run(self, argv: list[str], stdin: bytes | str | None = None, sudo: bool = False,
            timeout: float | None = None) -> Result:
        """Run ``argv`` in the CML VM (stdout + stderr merged).  ``stdin`` is sent, then EOF."""
        ws = self._connect()
        if timeout is not None:
            ws.settimeout(timeout)
        spawn = (["sudo", "-S", "-p", "", "--"] if sudo else []) + list(argv)
        try:
            ws.send("\n" + json.dumps({"command": "open", "channel": "c1", "payload": "stream",
                                       "spawn": spawn, "err": "out"}))
            # text frames are UTF-8: binary payloads must be sent base64-encoded by the caller
            data = (self._pw + "\n") if sudo else ""
            if stdin:
                data += stdin.decode("utf-8") if isinstance(stdin, bytes) else stdin
            for i in range(0, len(data), 32768):
                ws.send("c1\n" + data[i:i + 32768])
            ws.send("\n" + json.dumps({"command": "done", "channel": "c1"}))
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
        except Exception as e:  # noqa: BLE001  (websocket timeouts / resets -> one error type)
            raise CockpitError(f"Cockpit command failed: {e}") from None
        finally:
            try:
                ws.close()
            except Exception:  # noqa: BLE001
                pass
        text = "".join(out)
        if sudo:
            text = text.replace(self._pw, "<hidden>")
        return Result(close.get("exit-status"), text, close.get("problem", "") or "")

    def sh(self, script: str, stdin=None, sudo: bool = False, timeout: float | None = None) -> Result:
        return self.run(["/bin/sh", "-c", script], stdin=stdin, sudo=sudo, timeout=timeout)

    def read_file(self, path: str) -> str | None:
        r = self.run(["/bin/cat", "--", path], timeout=20)
        return r.output if r.ok else None

    def write_file(self, path: str, text: str) -> None:
        """Atomic replace of a file the sysadmin user owns (e.g. /var/lib/labdesk-cloud/routes.conf)."""
        script = ('set -e; d=$(dirname "$1"); t=$(mktemp "$d/.labdesk.XXXXXX"); cat > "$t"; chmod 0644 "$t"; '
                  'mv -f "$t" "$1"')
        r = self.run(["/bin/sh", "-c", script, "sh", path], stdin=text, timeout=30)
        if not r.ok:
            raise CockpitError(f"writing {path} in the CML VM failed: {(r.problem or r.output).strip()}")
