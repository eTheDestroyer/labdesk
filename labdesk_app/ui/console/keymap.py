"""Key presses -> the bytes a terminal sends (TERM=linux).  Pure Python: Qt key codes and modifiers are
passed as plain ints, so this module (and its tests) needs no Qt.
"""
from __future__ import annotations

# Qt.KeyboardModifier bits
SHIFT, CTRL, ALT, META = 0x02000000, 0x04000000, 0x08000000, 0x10000000

# Qt.Key codes
K_ESCAPE, K_TAB, K_BACKTAB, K_BACKSPACE, K_RETURN, K_ENTER = 0x01000000, 0x01000001, 0x01000002, 0x01000003, \
    0x01000004, 0x01000005
K_INSERT, K_DELETE, K_HOME, K_END = 0x01000006, 0x01000007, 0x01000010, 0x01000011
K_LEFT, K_UP, K_RIGHT, K_DOWN, K_PGUP, K_PGDN = 0x01000012, 0x01000013, 0x01000014, 0x01000015, 0x01000016, \
    0x01000017
K_F1 = 0x01000030
K_SPACE, K_2, K_6, K_AT, K_QUESTION = 0x20, 0x32, 0x36, 0x40, 0x3F
K_BRACKET_L, K_BACKSLASH, K_BRACKET_R, K_CIRCUM, K_UNDERSCORE = 0x5B, 0x5C, 0x5D, 0x5E, 0x5F
K_MINUS, K_SLASH = 0x2D, 0x2F

_ARROWS = {K_UP: "A", K_DOWN: "B", K_RIGHT: "C", K_LEFT: "D"}
_TILDE = {K_HOME: 1, K_INSERT: 2, K_DELETE: 3, K_END: 4, K_PGUP: 5, K_PGDN: 6}
_FKEYS = [b"\x1b[[A", b"\x1b[[B", b"\x1b[[C", b"\x1b[[D", b"\x1b[[E", b"\x1b[17~", b"\x1b[18~", b"\x1b[19~",
          b"\x1b[20~", b"\x1b[21~", b"\x1b[23~", b"\x1b[24~"]
_CTRL_PUNCT = {K_SPACE: 0x00, K_2: 0x00, K_AT: 0x00, K_BRACKET_L: 0x1B, K_BACKSLASH: 0x1C, K_BRACKET_R: 0x1D,
               K_6: 0x1E, K_CIRCUM: 0x1E, K_UNDERSCORE: 0x1F, K_MINUS: 0x1F, K_SLASH: 0x1F, K_QUESTION: 0x7F}


def encode(key: int, modifiers: int = 0, text: str = "", app_cursor: bool = False) -> bytes:
    """The bytes for one key press, or b'' when the key sends nothing (a bare modifier, ...).

    Ctrl-] is 0x1D (leaves a CML console), Ctrl-Shift-6 / Ctrl-^ is 0x1E (the IOS escape character),
    Ctrl-C is 0x03, Tab is 0x09, '?' is just '?' - the device gets exactly what a real terminal sends."""
    ctrl, alt, shift = bool(modifiers & CTRL), bool(modifiers & ALT), bool(modifiers & SHIFT)
    esc = b"\x1b" if alt else b""
    if key in (K_RETURN, K_ENTER):
        return esc + b"\r"
    if key == K_BACKSPACE:
        return esc + (b"\x08" if ctrl else b"\x7f")
    if key == K_TAB:
        return b"\x1b[Z" if shift else esc + b"\t"
    if key == K_BACKTAB:
        return b"\x1b[Z"
    if key == K_ESCAPE:
        return esc + b"\x1b"
    if key in _ARROWS:
        return (b"\x1bO" if app_cursor else b"\x1b[") + _ARROWS[key].encode()
    if key in _TILDE:
        return b"\x1b[%d~" % _TILDE[key]
    if K_F1 <= key < K_F1 + 12:
        return _FKEYS[key - K_F1]
    if ctrl:
        if 0x41 <= key <= 0x5A:                              # Ctrl-A .. Ctrl-Z
            return esc + bytes([key - 0x40])
        if key in _CTRL_PUNCT:
            return esc + bytes([_CTRL_PUNCT[key]])
        return b""
    if text:
        return esc + text.encode("utf-8", "replace")
    return b""
