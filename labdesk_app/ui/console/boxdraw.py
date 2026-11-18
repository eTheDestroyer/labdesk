"""Box-drawing (U+2500-257F) and block elements (U+2580-259F) as whole-pixel rectangles filling the cell,
so lines join from cell to cell at every text size.  Pure Python: ``rects`` returns rectangles relative to
the cell origin; the view fills them.

Arms and weights of a box character are derived from its Unicode name (light, heavy, double; dashed is
drawn solid; arcs are drawn square).  The three diagonals return None: the view falls back to the glyph.
"""
from __future__ import annotations

import re
import unicodedata
from functools import lru_cache

_DIRS = {"UP": ("u",), "DOWN": ("d",), "LEFT": ("l",), "RIGHT": ("r",), "HORIZONTAL": ("l", "r"),
         "VERTICAL": ("u", "d")}
_WEIGHTS = {"LIGHT": 1, "SINGLE": 1, "HEAVY": 2, "DOUBLE": 3}
_DASH = re.compile(r"(DOUBLE|TRIPLE|QUADRUPLE) DASH ")


def is_box(ch: str) -> bool:
    return "─" <= ch <= "▟"


@lru_cache(maxsize=None)
def arms(ch: str) -> dict | None:
    """{'u'|'d'|'l'|'r': weight} of a box-drawing character (1 light, 2 heavy, 3 double), or None."""
    try:
        name = unicodedata.name(ch)
    except ValueError:
        return None
    if not name.startswith("BOX DRAWINGS ") or "DIAGONAL" in name:
        return None
    name = _DASH.sub("", name[len("BOX DRAWINGS "):])
    out: dict = {}
    weight = 1
    for clause in name.split(" AND "):
        words = clause.split()
        ws = [_WEIGHTS[w] for w in words if w in _WEIGHTS]
        if ws:
            weight = ws[0]
        for w in words:
            for d in _DIRS.get(w, ()):
                out[d] = weight
    return out or None


def _block(ch: str, w: int, h: int):
    """Block elements: list of (x, y, w, h, alpha)."""
    o = ord(ch)
    hw, hh = w // 2, h // 2
    if o == 0x2580:
        return [(0, 0, w, hh, 1.0)]
    if 0x2581 <= o <= 0x2588:
        n = round(h * (o - 0x2580) / 8)
        return [(0, h - n, w, n, 1.0)]
    if 0x2589 <= o <= 0x258F:
        n = round(w * (0x2590 - o) / 8)
        return [(0, 0, n, h, 1.0)]
    if o == 0x2590:
        return [(hw, 0, w - hw, h, 1.0)]
    if 0x2591 <= o <= 0x2593:
        return [(0, 0, w, h, (0.25, 0.5, 0.75)[o - 0x2591])]
    if o == 0x2594:
        return [(0, 0, w, max(1, round(h / 8)), 1.0)]
    if o == 0x2595:
        n = max(1, round(w / 8))
        return [(w - n, 0, n, h, 1.0)]
    quads = {0x2596: "c", 0x2597: "d", 0x2598: "a", 0x2599: "acd", 0x259A: "ad", 0x259B: "abc", 0x259C: "abd",
             0x259D: "b", 0x259E: "bc", 0x259F: "bcd"}.get(o)
    if quads is None:
        return None
    box = {"a": (0, 0, hw, hh), "b": (hw, 0, w - hw, hh), "c": (0, hh, hw, h - hh), "d": (hw, hh, w - hw, h - hh)}
    return [(*box[q], 1.0) for q in quads]


def rects(ch: str, w: int, h: int):
    """Rectangles (x, y, w, h, alpha) that draw ``ch`` in a ``w`` x ``h`` cell, or None (use the glyph)."""
    if ch >= "▀":
        return _block(ch, w, h)
    a = arms(ch)
    if a is None:
        return None
    t = max(1, round(w / 8))                 # light stroke
    thick = {1: t, 2: 2 * t + 1, 3: t}
    d = t + max(1, t)                        # double: rail offset from the centre line
    cx, cy = (w - t) // 2, (h - t) // 2      # top-left of the light centre pixel block
    out = []

    def perp_double(side: str) -> bool:
        return a.get(side) == 3

    def rail_stop(arm: str, side: str, other: str) -> int:
        """Where a rail of ``arm`` ends near the centre, as a signed offset along the arm's direction
        (positive = toward the arm's own edge)."""
        if perp_double(side):
            return d                          # inner corner: turns into that arm's rail
        if perp_double(other):
            return -d                         # outer corner
        return 0

    for arm, wt in a.items():
        horizontal = arm in ("l", "r")
        sides = ("u", "d") if horizontal else ("l", "r")
        if wt == 3:
            for side, other, off in ((sides[0], sides[1], -d), (sides[1], sides[0], d)):
                stop = rail_stop(arm, side, other)
                out.append(_arm_rect(arm, off, t, stop, cx, cy, w, h, t))
        else:
            th = thick[wt]
            # a single arm ends on the far rail of a double line it meets, else just past the centre
            opposite = {"l": "r", "r": "l", "u": "d", "d": "u"}[arm]
            if perp_double(sides[0]) and perp_double(sides[1]) and opposite not in a:
                stop = d
            elif perp_double(sides[0]) or perp_double(sides[1]):
                stop = -d
            else:
                stop = -(max(thick.get(a.get(sides[0], 0), 0), thick.get(a.get(sides[1], 0), 0), th) // 2)
            out.append(_arm_rect(arm, -(th - t) // 2, th, stop, cx, cy, w, h, t))
    return [(x, y, rw, rh, 1.0) for x, y, rw, rh in out if rw > 0 and rh > 0]


def _arm_rect(arm: str, off: int, th: int, stop: int, cx: int, cy: int, w: int, h: int, t: int):
    """One stroke of an arm: ``off`` = perpendicular offset of its top/left edge from the centre block,
    ``stop`` = signed distance from the centre block toward the arm's edge where it ends."""
    if arm == "r":
        x0 = cx + stop
        return (x0, cy + off, w - x0, th)
    if arm == "l":
        return (0, cy + off, cx - stop + t, th)
    if arm == "d":
        y0 = cy + stop
        return (cx + off, y0, th, h - y0)
    return (cx + off, 0, th, cy - stop + t)
