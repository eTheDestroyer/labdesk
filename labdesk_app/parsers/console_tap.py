"""Passive console tap: interface-address commands typed on an IOS console, read from CML's own
console log (``GET /labs/{lab}/nodes/{node}/consoles/0/log``) - nothing is ever typed.

Why: while somebody has a node's console open, LabDesk pauses probing that node (D10 etiquette),
so a subnet typed in that console would only show up after the console closes.  The CML console
log is a server-side transcript of the line; reading it through the REST API (about 17 ms) does
not touch the session at all.  Verified live on CML 2.10.1: the typed line appears in the log
about 0.3 s after Enter.

Log format (one line per CR/LF): ``|0004887327|R1(config-subif)#ip add 172.16.99.1 255.255.255.0 sec``
(the number is a per-console millisecond counter).  The log holds the raw echo, so line edits
show up as ``\\b \\b`` and a Tab completion re-draws the line (``R1>sh`` then ``R1>show  clo``):
both are handled.  Pure functions only (no I/O); fixtures: tests/fixtures/switching/R1__console_log_*.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .common import ANSI_RE, IP, SYSLOG_RE, expand_iface, mask_to_len

LOG_RE = re.compile(r"^\|(?P<ts>\d+)\|(?P<text>.*)$")
PROMPT_RE = re.compile(r"^(?P<host>[A-Za-z0-9][A-Za-z0-9._-]*)(?P<cfg>\((?P<mode>config[^)]*)\))?(?P<p>[>#])"
                       r"(?P<cmd>.*)$")


@dataclass
class LogLine:
    ts: int
    text: str


@dataclass
class AddrCmd:
    """One accepted address command.  ``op``: set | add_sec | del_sec | del_primary | clear | dhcp |
    unnumbered | no_unnumbered."""
    iface: str                       # full device name ('GigabitEthernet0/0.10')
    op: str
    ip: str | None = None
    prefixlen: int | None = None
    src: str | None = None           # unnumbered source interface
    ts: int = 0                      # console log counter of the command line
    text: str = ""                   # the command as typed (address commands only - no secrets)


@dataclass
class TapState:
    """Per-node cursor + CLI context, kept between reads."""
    cursor: tuple[int, int] | None = None     # (log ts, lines consumed at that ts)
    iface: str | None = None                  # interface of the current config-if / config-subif mode
    host: str | None = None
    lines_seen: int = 0
    primed: bool = False                      # the first read only establishes the context


def _edit(s: str) -> str:
    """Apply terminal backspaces (overwrite semantics) and drop ANSI / control characters."""
    s = ANSI_RE.sub("", s)
    buf: list[str] = []
    pos = 0
    for ch in s:
        if ch in ("\b", "\x7f"):
            pos = max(0, pos - 1)
        elif ch == "\r":
            pos = 0
        elif ch < " " and ch != "\t":
            continue
        else:
            if pos < len(buf):
                buf[pos] = ch
            else:
                buf.append(ch)
            pos += 1
    return "".join(buf).rstrip()


def parse_log(text: str | None) -> list[LogLine]:
    out = []
    for raw in (text or "").split("\n"):
        m = LOG_RE.match(raw)
        if m:
            out.append(LogLine(int(m["ts"]), _edit(m["text"])))
    return out


def _prefix(tok: str, word: str, min_len: int = 2) -> bool:
    t = tok.lower()
    return len(t) >= min(min_len, len(word)) and word.startswith(t)


def _is_error(text: str) -> bool:
    s = text.strip()
    if not s:
        return False
    if SYSLOG_RE.match(s):
        return False
    return s.startswith("%") or set(s) == {"^"}


def _ip_plen(tokens: list[str]) -> tuple[str | None, int | None, list[str]]:
    """'10.0.0.1 255.255.255.252 ...' | '10.0.0.1/30 ...' -> (ip, plen, rest)."""
    if not tokens:
        return None, None, []
    t0 = tokens[0]
    m = re.fullmatch(r"(" + IP + r")/(\d{1,2})", t0)
    if m:
        return m[1], int(m[2]), tokens[1:]
    if re.fullmatch(IP, t0) and len(tokens) >= 2 and re.fullmatch(IP, tokens[1]):
        return t0, mask_to_len(tokens[1]), tokens[2:]
    return None, None, tokens


def _iface_name(tokens: list[str]) -> str | None:
    name = "".join(tokens)
    if not name or not re.search(r"\d", name):
        return None
    return expand_iface(name[:1].upper() + name[1:])


def parse_command(cmd: str, mode: str | None, ctx_iface: str | None) -> tuple[str | None, AddrCmd | None]:
    """One executed command -> (new interface context, address change or None).
    ``mode`` is the prompt's config mode ('config', 'config-if', 'config-subif', ... or None)."""
    tokens = cmd.split()
    if not tokens or mode is None:
        return None, None
    first = tokens[0].lower()
    in_if = mode in ("config-if", "config-subif")
    ctx = ctx_iface if in_if else None
    if first == "do" or first == "end" or first == "exit":
        return (ctx if first == "do" else None), None
    if _prefix(first, "interface", 3):
        if len(tokens) >= 2 and _prefix(tokens[1], "range", 2):
            return None, None
        return _iface_name(tokens[1:]), None
    neg = first == "no"
    dflt = _prefix(first, "default", 3)
    body = tokens[1:] if (neg or dflt) else tokens
    if (neg or dflt) and body and _prefix(body[0], "interface", 3):
        name = _iface_name(body[1:])
        return ctx, (AddrCmd(name, "clear", text=cmd) if name else None)
    if not in_if or ctx is None or len(body) < 2 or body[0].lower() != "ip":
        return ctx, None
    kw = body[1]
    rest = body[2:]
    if _prefix(kw, "address", 2):
        if dflt or (neg and not rest):
            return ctx, AddrCmd(ctx, "clear", text=cmd)
        if rest and _prefix(rest[0], "dhcp", 2):
            return ctx, AddrCmd(ctx, "clear" if neg else "dhcp", text=cmd)
        ip, plen, tail = _ip_plen(rest)
        if ip is None or plen is None:
            return ctx, None
        sec = bool(tail) and _prefix(tail[0], "secondary", 3)
        if neg:
            return ctx, AddrCmd(ctx, "del_sec" if sec else "del_primary", ip, plen, text=cmd)
        return ctx, AddrCmd(ctx, "add_sec" if sec else "set", ip, plen, text=cmd)
    if _prefix(kw, "unnumbered", 2):
        if neg or dflt:
            return ctx, AddrCmd(ctx, "no_unnumbered", text=cmd)
        src = _iface_name(rest)
        return ctx, (AddrCmd(ctx, "unnumbered", src=src, text=cmd) if src else None)
    return ctx, None


@dataclass
class FeedResult:
    cmds: list[AddrCmd] = field(default_factory=list)
    pending: bool = False            # stopped at a last command line that may still get an error
    config_done: bool = False        # '%SYS-5-CONFIG_I' seen (left config mode)
    reset: bool = False              # the log restarted (node rebooted / cleared)


def feed(state: TapState, log: list[LogLine], final: bool = False) -> FeedResult:
    """Consume the log lines after ``state.cursor``.

    Verified live on CML 2.10.1: the log line of a prompt is created when IOS prints the prompt and is
    then *updated in place* (same counter) while the user types; Enter starts the next line.  So the last
    prompt line is always "open" and is never consumed, and a command only counts once a LATER prompt
    exists - IOS prints its '% ...' error (or the '^' marker) before that prompt, so an error between the
    two rejects the command.  A same-prompt line that starts with the same text is a Tab/'?' redraw.
    ``final`` is accepted for compatibility and has no effect."""
    res = FeedResult()
    if not log:
        return res
    start = 0
    if state.cursor is not None:
        cts, cn = state.cursor
        if log[-1].ts < cts:
            res.reset = True                 # the counter went backwards: the node restarted
            state.cursor, state.iface = None, None
        else:
            seen = 0
            start = len(log)
            for i, ln in enumerate(log):
                if ln.ts < cts:
                    continue
                if ln.ts == cts and seen < cn:
                    seen += 1
                    continue
                start = i
                break
    prompts = [i for i, ln in enumerate(log) if PROMPT_RE.match(ln.text)]
    last_prompt = prompts[-1] if prompts else None
    stop = last_prompt if last_prompt is not None and last_prompt >= start else start
    if last_prompt is not None and last_prompt >= start and PROMPT_RE.match(log[last_prompt].text)["cmd"].strip():
        res.pending = True                   # something typed on the open line, Enter not seen yet
    priming = not state.primed
    i = start
    while i < stop:
        ln = log[i]
        m = PROMPT_RE.match(ln.text)
        if not m:
            if SYSLOG_RE.match(ln.text.strip()) and "CONFIG_I" in ln.text:
                res.config_done = True
            i += 1
            continue
        state.host = m["host"]
        mode = m["mode"]
        cmd = m["cmd"].strip()
        nxt = log[i + 1] if i + 1 < len(log) else None
        if nxt is not None and cmd:
            nm = PROMPT_RE.match(nxt.text)
            if nm and nm["host"] == m["host"] and nm["cfg"] == m["cfg"] and \
                    " ".join(nm["cmd"].split()).startswith(" ".join(cmd.split())) and nm["cmd"].strip() != cmd:
                i += 1                                    # Tab redraw: the next line supersedes this one
                continue
        if cmd.endswith("?"):
            i += 1
            continue
        j = i + 1
        rejected = False
        while j < len(log) and not PROMPT_RE.match(log[j].text):
            if _is_error(log[j].text):
                rejected = True
            j += 1
        if mode is None:
            state.iface = None
        if not rejected:
            new_ctx, ac = parse_command(cmd, mode, state.iface)
            if mode is not None:
                state.iface = new_ctx
            if ac is not None and not priming:
                ac.ts = ln.ts
                res.cmds.append(ac)
        i += 1
    if stop > start:
        last = log[stop - 1]
        n = sum(1 for ln in log[:stop] if ln.ts == last.ts)
        state.cursor = (last.ts, n)
    elif state.cursor is None and start > 0:
        last = log[start - 1]
        state.cursor = (last.ts, sum(1 for ln in log[:start] if ln.ts == last.ts))
    state.primed = True
    state.lines_seen += max(0, stop - start)
    return res
