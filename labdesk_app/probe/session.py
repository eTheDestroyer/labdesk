"""One state-preserving CLI burst over a console transport (D1 + D10 etiquette).

    result = run_burst(transport, IosDialect(), ["show ip interface brief", ...], creds)

Sequence (each step aborts the burst - sending nothing more - when the line is not clean):
 1. wait for the termserver banner ('Escape character is'), drain replayed bytes until the
    line has been quiet 300 ms (cap 2 s); optional single Ctrl-C when the previous burst
    was killed mid-command (needs_recovery).
 2. send CR; classify ONLY the bytes after it: exec prompt '>' / '#', 'Press RETURN',
    'Username:', 'Password:', config mode, pager, [confirm], rommon, boot banner ... .
    Anything but a clean exec prompt / logged-out banner / login prompt -> CONSOLE_BUSY.
 3. log in only if a login prompt actually appeared; 'enable' only when a command needs
    privilege 15 and the line is at '>'.
 4. terminal length 0 / width 0, then every command followed by the sentinel
    ``show clock | include LDK<nonce><n>``; a command's output ends at the echo of its
    sentinel + the prompt, so 'Router#' text inside the output can never end a read.
 5. restore: terminal length 24 / width 80, 'disable' if we enabled, 'exit' only if the
    line was found logged out.  Never exit a session that was found logged in.
Unsolicited syslog lines are stripped from outputs and returned separately.
"""
from __future__ import annotations

import re
import secrets
import time
from dataclasses import dataclass, field

from ..parsers.common import ANSI_RE, SyslogLine, split_syslog
from .creds import DeviceCreds
from .transport import TransportError

PROMPT_RE = re.compile(r"^(?P<host>[A-Za-z0-9._-]+)(?P<cfg>\(config[^)]*\))?(?P<mode>[>#])\s*$")


class BurstStatus:
    OK = "ok"
    PARTIAL = "partial"              # some commands timed out / failed
    BUSY = "console_busy"            # foreign session state: nothing was sent after the probe CR
    NEEDS_CREDS = "needs_credentials"
    TIMEOUT = "timeout"              # hard timeout: ssh killed -> needs_recovery
    ERROR = "error"                  # ssh / transport failure


@dataclass
class CmdOutput:
    command: str
    text: str | None                 # clean output (no echo, no prompt, syslog stripped); None = no sentinel
    seconds: float
    error: str | None = None
    wall: float = 0.0                # time.time() when the answer was complete


@dataclass
class BurstResult:
    status: str
    reason: str = ""
    outputs: dict[str, CmdOutput] = field(default_factory=dict)
    syslog: list[SyslogLine] = field(default_factory=list)
    hostname: str | None = None
    mode_found: str | None = None    # 'logged_out' | 'username' | 'user' ('>') | 'priv' ('#')
    enabled: bool = False
    seconds: float = 0.0
    needs_recovery: bool = False
    transcript: str = ""             # raw (NOT yet redacted) for the probe log; caller redacts


class IosDialect:
    """Cisco IOS / IOS-XE console rules."""
    vendor = "cisco"
    init_cmds = ["terminal length 0", "terminal width 0"]
    restore_cmds = ["terminal length 24", "terminal width 80"]
    priv_prefixes = ("show running-config", "show run", "show startup-config", "show archive")
    # the full config is slow on a serial console (IOSv R1 ~4 KB: 14-17 s measured live, incl. 'Building
    # configuration...'): these get at least slow_cmd_timeout instead of the per-command default
    slow_prefixes = ("show running-config", "show startup-config")
    slow_cmd_timeout = 40.0
    # commands that flush typed-ahead input: the sentinel is sent only after their prompt
    no_typeahead_prefixes = ("ping", "traceroute", "trace ")

    def needs_enable(self, cmd: str) -> bool:
        return cmd.strip().startswith(self.priv_prefixes)

    @staticmethod
    def user_form(cmd: str) -> str:
        """IOSv user EXEC ('>') accepts only a bare 'ping <host>' / 'traceroute <host>': any option
        (repeat, timeout, source, numeric, probe, ttl) is '% Invalid input'.  Verified live on IOSv."""
        parts = cmd.split()
        if len(parts) > 2 and parts[0] in ("ping", "traceroute"):
            return f"{parts[0]} {parts[1]}"
        return cmd

    @staticmethod
    def sentinel(nonce: str, n: int) -> str:
        return f"show clock | include LDK{nonce}{n}"

    @staticmethod
    def new_session(tail: str) -> bool:
        """True when the bytes after our CR show a NEW exec session (logged-out line):
        'Press RETURN' or an exec banner printed before the prompt (syslog lines ignored)."""
        if "press return to get started" in tail.lower():
            return True
        body = [l for l in tail.replace("\r", "\n").split("\n")[:-1] if l.strip()]
        body = [l for l in body if not re.match(r"^\s*\*?(?:[A-Z][a-z]{2} +\d+ [\d:.]+: )?%[A-Z0-9_]+-\d-", l)]
        return len(body) >= 2

    @staticmethod
    def classify(tail: str) -> tuple[str, re.Match | None]:
        """Classify the console after our CR.  Returns (kind, prompt-match)."""
        lines_ = [l.rstrip() for l in tail.replace("\r", "\n").split("\n")]
        nonempty = [l for l in lines_ if l.strip()]
        last = nonempty[-1].strip() if nonempty else ""
        low = tail.lower()
        if "--more--" in low or " --more-- " in low:
            return "pager", None
        if "[confirm]" in low or "[yes/no]" in low or "[yes]" in low:
            return "confirm", None
        if "rommon" in low or "bootstrap" in low or "initial configuration dialog" in low or \
                "self decompressing" in low:
            return "booting", None
        m = PROMPT_RE.match(last)
        if m:
            return ("config" if m["cfg"] else ("priv" if m["mode"] == "#" else "user")), m
        if "press return to get started" in low:
            return "logged_out", None
        if re.search(r"(username|login):\s*$", last, re.I):
            return "username", None
        if re.search(r"password:\s*$", last, re.I):
            return "password", None
        return "unknown", None


class _Reader:
    def __init__(self, transport, deadline: float):
        self.t = transport
        self.deadline = deadline
        self.buf = ""           # normalized text since the last mark
        self.transcript: list[str] = []

    def _feed(self, data: bytes) -> None:
        s = data.decode("utf-8", "replace")
        self.transcript.append(s)
        s = ANSI_RE.sub("", s).replace("\x00", "")
        s = re.sub(r"\r+\n", "\n", s)
        self.buf += s

    def mark(self) -> None:
        self.buf = ""

    def pump(self, timeout: float) -> bool:
        if time.monotonic() > self.deadline:
            raise TimeoutError("burst deadline")
        d = self.t.read(min(timeout, max(0.0, self.deadline - time.monotonic())))
        if d:
            self._feed(d)
            return True
        return False

    def until(self, pred, timeout: float) -> bool:
        end = min(time.monotonic() + timeout, self.deadline)
        while time.monotonic() < end:
            if pred(self.buf):
                return True
            self.pump(min(0.2, max(0.0, end - time.monotonic())))
        if time.monotonic() > self.deadline:
            raise TimeoutError("burst deadline")
        return pred(self.buf)

    def quiet(self, quiet_s: float, cap_s: float) -> None:
        end = min(time.monotonic() + cap_s, self.deadline)
        last = time.monotonic()
        while time.monotonic() < end:
            if self.pump(0.05):
                last = time.monotonic()
            elif time.monotonic() - last >= quiet_s:
                return


def _configure(rd: "_Reader", transport, lines: list[str], res: BurstResult, save: bool) -> bool:
    """configure terminal / lines / end [/ write memory].  Returns False when a line was rejected or
    the prompt got lost (the caller still restores the line)."""
    c0 = time.monotonic()
    errors: list[str] = []
    echo: list[str] = []

    def at_prompt(cfg: bool | None):
        def pred(b):
            m = _prompt_at_end(b)
            if not m:
                return False
            return cfg is None or bool(m["cfg"]) == cfg
        return pred

    rd.mark()
    transport.write(b"configure terminal\r")
    if not rd.until(at_prompt(True), 6.0):
        res.outputs[CONFIG_KEY] = CmdOutput(CONFIG_KEY, None, time.monotonic() - c0, "no config prompt")
        transport.write(b"\x1a\r")             # Ctrl-Z: back to exec whatever happened
        rd.quiet(0.4, 2.0)
        return False
    for line in lines:
        rd.mark()
        transport.write(line.encode() + b"\r")
        if not rd.until(at_prompt(None), 8.0):
            errors.append(f"{line.strip()}: no prompt")
            break
        body, _ = split_syslog(rd.buf)
        echo.append(line)
        for m in _IOS_ERR_RE.finditer(body):
            errors.append(f"{line.strip()}: {m.group(0).strip()}")
    rd.mark()
    transport.write(b"end\r")
    if not rd.until(at_prompt(False), 6.0):
        transport.write(b"\x1a\r")
        rd.quiet(0.5, 2.0)
    res.outputs[CONFIG_KEY] = CmdOutput(CONFIG_KEY, "\n".join(echo) + "\n", time.monotonic() - c0,
                                        "; ".join(errors) if errors else None, wall=time.time())
    if save and not errors:
        s0 = time.monotonic()
        rd.mark()
        transport.write(b"write memory\r")
        saved = False
        for _ in range(3):
            if not rd.until(lambda b: "[confirm]" in b or "[OK]" in b or (b.count("\n") >= 1 and
                                                                            _prompt_at_end(b) is not None), 15.0):
                break
            if "[confirm]" in rd.buf and "[OK]" not in rd.buf:
                rd.mark()
                transport.write(b"\r")
                continue
            rd.until(at_prompt(False), 5.0)
            saved = "[OK]" in rd.buf or "Building configuration" in rd.buf or _prompt_at_end(rd.buf) is not None
            break
        res.outputs[SAVE_KEY] = CmdOutput(SAVE_KEY, "[OK]\n" if saved else None, time.monotonic() - s0,
                                          None if saved else "write memory: no [OK]", wall=time.time())
        if not saved:
            errors.append("write memory failed")
    return not errors


def _prompt_at_end(buf: str) -> re.Match | None:
    tail = buf.rstrip("\n").rsplit("\n", 1)[-1].strip()
    return PROMPT_RE.match(tail)


def _plain_body(buf: str, res: "BurstResult") -> str:
    """Output of a command run without a sentinel: drop the echo line and the closing prompt line."""
    body = buf.rstrip("\n")
    body = body[:body.rfind("\n") + 1] if "\n" in body else ""
    first_nl = body.find("\n")
    body = body[first_nl + 1:] if first_nl >= 0 else ""
    clean, logs = split_syslog(body)
    res.syslog.extend(logs)
    clean = "\n".join(l.rstrip() for l in clean.split("\n")).strip("\n")
    return clean + ("\n" if clean else "")


CONFIG_KEY = "(configure)"            # BurstResult.outputs key of the config-mode step
SAVE_KEY = "(write memory)"
_IOS_ERR_RE = re.compile(r"^\s*% ?(Invalid input|Incomplete command|Ambiguous command|.*[Ee]rror|.*not .*allowed)"
                         r".*$", re.M)


def run_burst(transport, dialect: IosDialect, commands: list[str], creds: DeviceCreds | None = None,
              *, timeout: float = 20.0, cmd_timeout: float = 15.0, needs_recovery: bool = False,
              expect_hostname: str | None = None, start: bool = True,
              config_lines: list[str] | None = None, save_config: bool = False,
              plain: bool = False, stay_logged_in: bool = False) -> BurstResult:
    """Run ``commands`` on a console line, leaving the line as it was found.

    ``plain`` (show scripts the user reads in their own console window): no sentinel lines are typed -
    a command ends when the exec prompt is back and the line has gone quiet - so the console shows
    only the commands and their output.  ``stay_logged_in``: a line found logged out is left at its
    exec prompt (the user is about to use that console) instead of being logged out again.

    ``config_lines`` (user actions only, e.g. "Auto-configure for Internet cloud"): typed in
    ``configure terminal`` BEFORE ``commands`` (privilege 15 is required: the enable secret comes from
    ``creds``), each line waits for the config prompt; IOS '%' errors are collected in
    ``outputs[CONFIG_KEY].error``.  ``save_config`` then runs ``write memory`` (answering a [confirm]).
    The line is always brought back to exec mode with ``end``."""
    creds = creds or DeviceCreds()
    t0 = time.monotonic()
    res = BurstResult(BurstStatus.ERROR)
    rd = _Reader(transport, t0 + timeout)
    nonce = secrets.token_hex(3)
    found_logged_out = False
    try:
        if start:
            transport.start()
        # 1. banner + replay settle
        if not rd.until(lambda b: "Escape character is" in b or "Connected to" in b, 8.0):
            res.status, res.reason = BurstStatus.ERROR, "no termserver banner (console unavailable?)"
            return res
        rd.quiet(0.3, 2.0)
        if needs_recovery:
            transport.write(b"\x03")
            rd.quiet(0.3, 1.0)
        # 2. probe CR and classify only what follows
        rd.mark()
        transport.write(b"\r")
        rd.quiet(0.4, 3.0)
        kind, pm = dialect.classify(rd.buf)
        if kind in ("user", "priv") and dialect.new_session(rd.buf):
            found_logged_out = True          # our CR started a fresh exec session (banner printed)
        if kind == "logged_out":
            found_logged_out = True
            rd.mark()
            transport.write(b"\r")
            rd.quiet(0.5, 4.0)
            kind, pm = dialect.classify(rd.buf)
        res.mode_found = "logged_out" if found_logged_out else kind
        if kind == "username":
            if not (creds.username and creds.password):
                res.status, res.reason = BurstStatus.NEEDS_CREDS, "console asks for a login; no device credentials stored"
                return res
            found_logged_out = True
            rd.mark()
            transport.write(creds.username.encode() + b"\r")
            if not rd.until(lambda b: re.search(r"[Pp]assword:\s*$", b), 5.0):
                res.status, res.reason = BurstStatus.BUSY, "unexpected login dialogue"
                return res
            rd.mark()
            transport.write(creds.password.encode() + b"\r")
            rd.quiet(0.5, 5.0)
            kind, pm = dialect.classify(rd.buf)
            if kind not in ("user", "priv"):
                res.status, res.reason = BurstStatus.NEEDS_CREDS, "login failed"
                return res
        elif kind == "password":
            res.status, res.reason = BurstStatus.BUSY, "console is at a Password: prompt (someone else's dialogue)"
            return res
        if kind not in ("user", "priv"):
            res.status = BurstStatus.BUSY
            res.reason = {"config": "console is in configuration mode",
                          "pager": "console is paging output (--More--)",
                          "confirm": "console is waiting for a confirmation",
                          "booting": "device is booting / in ROMMON"}.get(kind, "console state not recognised")
            return res
        res.hostname = pm["host"] if pm else None
        prompt_host = res.hostname
        mode = kind
        # 3. enable only when needed
        if mode == "user" and (config_lines or any(dialect.needs_enable(c) for c in commands)):
            if config_lines and not creds.enable:
                res.status, res.reason = BurstStatus.NEEDS_CREDS, "configuration needs the enable secret"
                return res
            if not creds.enable:
                # run what we can; privileged commands report 'needs enable secret'
                commands = [c for c in commands if not dialect.needs_enable(c)] + \
                           [c for c in commands if dialect.needs_enable(c)]
            else:
                rd.mark()
                transport.write(b"enable\r")
                if rd.until(lambda b: re.search(r"[Pp]assword:\s*$", b) or _prompt_at_end(b), 5.0) and \
                        re.search(r"[Pp]assword:\s*$", rd.buf):
                    rd.mark()
                    transport.write(creds.enable.encode() + b"\r")
                    rd.quiet(0.4, 4.0)
                pm2 = _prompt_at_end(rd.buf)
                if pm2 and pm2["mode"] == "#":
                    mode, res.enabled = "priv", True
                else:
                    # bad secret: IOS re-prompts; send CRs until we are back at '>'
                    for _ in range(3):
                        if _prompt_at_end(rd.buf):
                            break
                        transport.write(b"\r")
                        rd.quiet(0.4, 3.0)
                    if config_lines:
                        res.status, res.reason = BurstStatus.NEEDS_CREDS, "enable secret rejected"
                        return res
        # 4. commands
        n = 0

        def run_one(cmd: str, ctimeout: float) -> CmdOutput:
            nonlocal n
            n += 1
            sent = dialect.sentinel(nonce, n)
            rd.mark()
            c0 = time.monotonic()
            tag = f"include LDK{nonce}{n}"
            interactive = cmd.startswith(dialect.no_typeahead_prefixes)

            def back(b):
                return b.count("\n") >= 1 and _prompt_at_end(b) is not None
            if plain and not interactive:
                # no sentinel: done = prompt back + line quiet (a prompt-looking line in the middle of an
                # output is followed by more bytes, so the quiet check rejects it)
                transport.write(cmd.encode() + b"\r")
                end = time.monotonic() + ctimeout
                ok_ = False
                while time.monotonic() < end:
                    if not rd.until(back, max(0.2, end - time.monotonic())):
                        break
                    seen = len(rd.buf)
                    rd.quiet(0.35, 1.5)
                    if len(rd.buf) == seen and back(rd.buf):
                        ok_ = True
                        break
                dt = time.monotonic() - c0
                if not ok_:
                    return CmdOutput(cmd, None, dt, "timeout (no prompt)")
                return CmdOutput(cmd, _plain_body(rd.buf, res), dt, wall=time.time())
            if interactive:
                transport.write(cmd.encode() + b"\r")
                if not rd.until(back, max(1.0, ctimeout - 6.0)):
                    # still running (e.g. a bare user-mode traceroute: 30 hops x 3 probes): abort it with the
                    # IOS escape sequence Ctrl-^ x, kill any stray 'x' with Ctrl-U, then ask for a fresh prompt
                    transport.write(b"\x1ex")
                    rd.quiet(0.4, 2.0)
                    transport.write(b"\x15\r")
                    if not rd.until(back, 4.0):
                        return CmdOutput(cmd, None, time.monotonic() - c0, "timeout (no prompt)")
                if plain:
                    return CmdOutput(cmd, _plain_body(rd.buf, res), time.monotonic() - c0, wall=time.time())
                transport.write(sent.encode() + b"\r")
            else:
                transport.write(cmd.encode() + b"\r" + sent.encode() + b"\r")

            def done(b):
                i = b.find(tag)
                if i < 0:
                    return False
                return _prompt_at_end(b[i:]) is not None and b.rstrip().endswith(("#", ">"))

            ok_ = rd.until(done, ctimeout)
            dt = time.monotonic() - c0
            if not ok_:
                return CmdOutput(cmd, None, dt, "timeout (sentinel not seen)")
            buf = rd.buf
            i = buf.find(tag)
            body = buf[:i]
            # drop the prompt+sentinel echo line start and the command echo line
            body = body[:body.rfind("\n") + 1] if "\n" in body else ""
            first_nl = body.find("\n")
            echo = body[:first_nl] if first_nl >= 0 else body
            body = body[first_nl + 1:] if first_nl >= 0 else ""
            if cmd.split()[0] not in echo:
                # echo was interleaved with syslog; best effort: drop through the echoed command
                j = body.find(cmd)
                if j >= 0:
                    body = body[body.find("\n", j) + 1:]
            clean, logs = split_syslog(body)
            res.syslog.extend(logs)
            # trailing prompt line before the sentinel (e.g. 'R1#') is part of the sentinel line already
            clean = "\n".join(l.rstrip() for l in clean.split("\n")).strip("\n")
            return CmdOutput(cmd, clean + ("\n" if clean else ""), dt, wall=time.time())

        for c in dialect.init_cmds:
            out = run_one(c, 5.0)
            if out.text is None:
                res.status, res.reason = BurstStatus.PARTIAL, f"'{c}' got no answer"
                break
        failed = 0
        if config_lines:
            ok_cfg = _configure(rd, transport, config_lines, res, save_config)
            if not ok_cfg:
                failed += 1
        for c in commands:
            if dialect.needs_enable(c) and mode != "priv":
                res.outputs[c] = CmdOutput(c, None, 0.0, "needs enable secret")
                failed += 1
                continue
            ct = max(cmd_timeout, dialect.slow_cmd_timeout) if c.strip().startswith(dialect.slow_prefixes) \
                else cmd_timeout
            out = run_one(c if mode == "priv" else dialect.user_form(c), ct)
            res.outputs[c] = out
            if out.text is None:
                failed += 1
                break                     # line state unknown: stop sending, restore below
        # 5. restore
        for c in dialect.restore_cmds:
            run_one(c, 5.0)
        if res.enabled:
            rd.mark()
            transport.write(b"disable\r")
            rd.until(lambda b: _prompt_at_end(b) is not None, 3.0)
        if found_logged_out and not stay_logged_in:
            transport.write(b"exit\r")
            rd.quiet(0.3, 1.0)
        res.status = BurstStatus.OK if failed == 0 else BurstStatus.PARTIAL
        if failed and not res.reason:
            res.reason = f"{failed} command(s) failed"
        if expect_hostname and prompt_host and prompt_host != expect_hostname:
            res.reason = (res.reason + "; " if res.reason else "") + f"hostname is {prompt_host}"
        return res
    except TimeoutError:
        res.status, res.reason, res.needs_recovery = BurstStatus.TIMEOUT, "burst timed out", True
        return res
    except TransportError as e:
        res.status, res.reason = BurstStatus.ERROR, str(e)
        return res
    finally:
        res.seconds = time.monotonic() - t0
        res.transcript = "".join(rd.transcript)
        try:
            transport.close()
        except Exception:
            pass
