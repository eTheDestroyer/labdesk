"""Console colours ("Phosphor"): one dim, one neon and one bright green; LabDesk red and amber; no indigo.

Pure Python (no Qt): every colour is a ``#RRGGBB`` string.  Contrast ratios are WCAG 2 ratios; the test
suite checks that every text colour, every faint variant and every text-on-fill pair of all three palettes
reaches 4.5:1 on its own background.
"""
from __future__ import annotations

from dataclasses import dataclass, replace

MIN_CONTRAST = 4.5
FAINT_T = 0.45               # SGR 2: blend toward the well by this much (less when 4.5:1 would be lost)


def rgb(colour: str) -> tuple[int, int, int]:
    c = colour.lstrip("#")
    return int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)


def hexc(r: float, g: float, b: float) -> str:
    return "#{:02X}{:02X}{:02X}".format(*(max(0, min(255, round(v))) for v in (r, g, b)))


def luminance(colour: str) -> float:
    def lin(v: int) -> float:
        s = v / 255.0
        return s / 12.92 if s <= 0.03928 else ((s + 0.055) / 1.055) ** 2.4
    r, g, b = rgb(colour)
    return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b)


def contrast(a: str, b: str) -> float:
    """WCAG contrast ratio of two colours (1.0 .. 21.0)."""
    la, lb = luminance(a), luminance(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def blend(a: str, b: str, t: float) -> str:
    """a * (1 - t) + b * t"""
    ra, ga, ba = rgb(a)
    rb, gb, bb = rgb(b)
    return hexc(ra + (rb - ra) * t, ga + (gb - ga) * t, ba + (bb - ba) * t)


_FAINT_CACHE: dict = {}


def faint(colour: str, well: str) -> str:
    """SGR 2: the colour blended toward the well by ``FAINT_T``, reduced per colour until the result still
    reads at 4.5:1 (a colour that is below 4.5:1 to begin with is returned unchanged)."""
    key = (colour, well)
    out = _FAINT_CACHE.get(key)
    if out is None:
        out = colour
        t = FAINT_T
        while t > 0.0:
            c = blend(colour, well, t)
            if contrast(c, well) >= MIN_CONTRAST:
                out = c
                break
            t = round(t - 0.01, 2)
        _FAINT_CACHE[key] = out
    return out


# pyte colour names (SGR 30-37 / 90-97) -> ANSI index
ANSI_NAMES = {"black": 0, "red": 1, "green": 2, "brown": 3, "blue": 4, "magenta": 5, "cyan": 6, "white": 7,
              "brightblack": 8, "brightred": 9, "brightgreen": 10, "brightbrown": 11, "brightblue": 12,
              "brightmagenta": 13, "brightcyan": 14, "brightwhite": 15}


@dataclass(frozen=True)
class ConsolePalette:
    name: str
    # surfaces
    well: str                # the terminal background
    chrome: str              # tab strip, status line
    raised: str              # hover
    hairline: str
    muted: str               # decoration only (scrollbar handle, pills) - never text
    # text ladder
    dim: str
    body: str
    bright: str              # prompt + typed command (they differ by weight)
    # signals
    red: str                 # refusals, syslog severity 0-2, failed session
    amber: str               # config mode and warnings only
    cursor: str
    accent: str              # the 2 px bar of the active tab
    # text on a fill
    sel_bg: str
    sel_fg: str
    find_bg: str
    find_fg: str
    find_current_bg: str
    ended_bg: str
    ended_border: str
    ended_fg: str
    chip_bg: str
    chip_fg: str
    pill_fg: str             # '--More--' pill text (on ``muted``)
    ansi: tuple              # 16 colours: 0-7 normal, 8-15 bright
    black_fg: str            # ANSI black used as a foreground on the default background

    # ------------------------------------------------------------ resolving device colours
    def fg(self, name: str, bold: bool = False) -> str:
        """Foreground of a pyte colour ('default', an ANSI name, or 'rrggbb' for 256 / true colour)."""
        if name == "default":
            return self.body
        i = ANSI_NAMES.get(name)
        if i is None:
            return "#" + name.upper() if len(name) == 6 else self.body
        if i == 0:
            return self.black_fg
        return self.ansi[i]

    def bg(self, name: str) -> str | None:
        """Background of a pyte colour; None = the well."""
        if name == "default":
            return None
        i = ANSI_NAMES.get(name)
        if i is None:
            return "#" + name.upper() if len(name) == 6 else None
        return self.ansi[i]

    def faint(self, colour: str) -> str:
        return faint(colour, self.well)

    # ------------------------------------------------------------ what the contrast test walks
    def text_colours(self) -> dict[str, str]:
        """Every colour that is drawn as text on the well."""
        out = {"dim": self.dim, "body": self.body, "bright": self.bright, "red": self.red, "amber": self.amber,
               "black_fg": self.black_fg}
        out.update({f"ansi{i}": c for i, c in enumerate(self.ansi) if i != 0})
        return out

    def pairs(self) -> dict[str, tuple[str, str]]:
        """Every text-on-fill pair: name -> (text, background)."""
        return {"selection": (self.sel_fg, self.sel_bg), "find": (self.find_fg, self.find_bg),
                "find current": (self.find_fg, self.find_current_bg),
                "ended row": (self.ended_fg, self.ended_bg), "chip": (self.chip_fg, self.chip_bg),
                "pill": (self.pill_fg, self.muted), "cursor": (self.well, self.cursor),
                "dim on chrome": (self.dim, self.chrome), "body on chrome": (self.body, self.chrome),
                "bright on chrome": (self.bright, self.chrome), "body on raised": (self.body, self.raised),
                "red on chrome": (self.red, self.chrome)}


_ANSI = ("#0B140D", "#FF6369", "#39FF14", "#FFCA16", "#6EA8FF", "#FF5CD6", "#3FE0D0", "#C9F7C0",
         "#4E8A52", "#FF8F94", "#8CFF78", "#FFE066", "#9CC4FF", "#FF9BE6", "#8AF0E4", "#F2FFEE")

PHOSPHOR = ConsolePalette(
    name="phosphor",
    well="#050A07", chrome="#0A110C", raised="#0F1A12", hairline="#16301A", muted="#1C5A14",
    dim="#2BA516", body="#39FF14", bright="#D6FFCC",
    red="#FF6369", amber="#FFCA16", cursor="#39FF14", accent="#39FF14",
    sel_bg="#1B5E12", sel_fg="#D6FFCC",
    find_bg="#39FF14", find_fg="#050A07", find_current_bg="#D6FFCC",
    ended_bg="#2A0C10", ended_border="#FF6369", ended_fg="#FF8F94",
    chip_bg="#312F0E", chip_fg="#FFCA16", pill_fg="#D6FFCC",
    ansi=_ANSI, black_fg="#4E8A52")

_SOFT = "#7DF56A"
SOFT_PHOSPHOR = replace(PHOSPHOR, name="soft", body=_SOFT, cursor=_SOFT, accent=_SOFT, find_bg=_SOFT,
                        ansi=tuple(_SOFT if i == 2 else c for i, c in enumerate(_ANSI)))

PLAIN = ConsolePalette(                 # dark only, independent of the app theme
    name="plain",
    well="#111113", chrome="#18191B", raised="#212225", hairline="#363A3F", muted="#43484E",
    dim="#B0B4BA", body="#EDEEF0", bright="#EDEEF0",
    red="#FF6369", amber="#FFCA16", cursor="#8481EA", accent="#8481EA",
    sel_bg="#25264F", sel_fg="#EDEEF0",
    find_bg="#8481EA", find_fg="#111113", find_current_bg="#EDEEF0",
    ended_bg="#2A0C10", ended_border="#FF6369", ended_fg="#FF8F94",
    chip_bg="#312F0E", chip_fg="#FFCA16", pill_fg="#EDEEF0",
    ansi=tuple({2: "#3DD68C", 8: "#8B8F98"}.get(i, c) for i, c in enumerate(_ANSI)), black_fg="#8B8F98")

PALETTES = {p.name: p for p in (PHOSPHOR, SOFT_PHOSPHOR, PLAIN)}


def pick(neon: bool = True, soft: bool = False) -> ConsolePalette:
    """View > Console: 'Neon green console' off = PLAIN; 'Softer green' = SOFT PHOSPHOR."""
    if not neon:
        return PLAIN
    return SOFT_PHOSPHOR if soft else PHOSPHOR
