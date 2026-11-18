"""The IOS lens: display-only emphasis for Cisco IOS console lines.

Pure Python (no Qt).  ``classify`` returns colour spans for one line of text; it never changes the bytes,
the scrollback or what a copy returns.  The view applies it only on tabs of IOS-family devices, only to
cells that have the default foreground, and skips a row that carries any SGR colour.

Rules (nothing else is ever coloured):

* prompt row      ``HOST>`` / ``HOST#`` / ``HOST(config-if)#`` at column 0 whose hostname this session has
                  already seen as a real prompt (``learn``): hostname and the ``>``/``#`` bright bold, the
                  ``(config...)`` part amber bold, the typed command bright
* refusal         a row that starts with ``% Invalid input``, ``% Incomplete command``,
                  ``% Ambiguous command`` or ``% Unknown command`` is red; so is the caret row right above
                  ``% Invalid input``
* syslog          only the ``%FACILITY-N-MNEMONIC`` token: severity 0-2 red, 3-4 amber, 5-7 untouched; a
                  leading ``*Mon dd hh:mm:ss.mmm:`` timestamp of such a row is dim
* ``--More--``    drawn as a pill
"""
from __future__ import annotations

import re

# span classes
PROMPT, CONFIG, COMMAND, REFUSAL, SEV_HIGH, SEV_MID, TIMESTAMP, MORE = (
    "prompt", "config", "command", "refusal", "sev_high", "sev_mid", "timestamp", "more")

IOS_DEFS = frozenset({"iosv", "iosvl2", "iol-xe", "ioll2-xe", "csr1000v", "cat8000v", "cat9000v-q200",
                      "cat9000v-uadp"})                    # Cisco IOS / IOS-XE

PROMPT_RE = re.compile(r"^([A-Za-z][A-Za-z0-9._-]{0,62})(\([A-Za-z0-9._-]+\))?([>#])")
REFUSALS = ("% Invalid input", "% Incomplete command", "% Ambiguous command", "% Unknown command")
CARET_RE = re.compile(r"^ *\^ *$")
SYSLOG_RE = re.compile(r"%[A-Z][A-Z0-9_]*-([0-7])-[A-Z0-9_]+")
STAMP_RE = re.compile(r"^\*?[A-Z][a-z]{2} [ \d]\d \d\d:\d\d:\d\d(?:\.\d{3})?(?: [A-Z]{2,5})?: ")
MORE_RE = re.compile(r"^( *)(--More--) *$")


def applies(node_definition: str | None) -> bool:
    """The lens runs only on consoles of IOS-family devices."""
    return bool(node_definition) and node_definition in IOS_DEFS


def learn(cursor_row: str, cursor_x: int, known_hosts: set) -> bool:
    """Remember the hostname of a real prompt: the row the cursor is on is exactly a prompt and the cursor
    sits right behind it (the device printed it and waits).  Returns True when a new name was learned."""
    m = PROMPT_RE.match(cursor_row)
    if m is None or m.end() != cursor_x or cursor_row[m.end():].strip():
        return False
    host = m.group(1)
    if host in known_hosts:
        return False
    known_hosts.add(host)
    return True


def classify(line_text: str, known_hosts=(), next_text: str = "") -> list[tuple[int, int, str]]:
    """Spans ``(start, length, class)`` for one line.  ``next_text`` is the row below (the caret rule)."""
    if not line_text:
        return []
    m = PROMPT_RE.match(line_text)
    if m is not None and m.group(1) in known_hosts:
        spans = [(0, len(m.group(1)), PROMPT)]
        if m.group(2):
            spans.append((m.start(2), len(m.group(2)), CONFIG))
        spans.append((m.start(3), 1, PROMPT))
        if len(line_text) > m.end():
            spans.append((m.end(), len(line_text) - m.end(), COMMAND))
        return spans
    if line_text.startswith(REFUSALS):
        return [(0, len(line_text), REFUSAL)]
    if next_text.startswith("% Invalid input") and CARET_RE.match(line_text):
        return [(0, len(line_text), REFUSAL)]
    m = MORE_RE.match(line_text)
    if m is not None:
        return [(m.start(2), len(m.group(2)), MORE)]
    m = SYSLOG_RE.search(line_text)
    if m is not None:
        spans = []
        st = STAMP_RE.match(line_text)
        if st is not None and st.end() <= m.start():
            spans.append((0, st.end(), TIMESTAMP))
        sev = int(m.group(1))
        if sev <= 2:
            spans.append((m.start(), m.end() - m.start(), SEV_HIGH))
        elif sev <= 4:
            spans.append((m.start(), m.end() - m.start(), SEV_MID))
        return spans
    return []
