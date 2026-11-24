"""Wi-Fi channels in Israel and how much two channels interfere (pure Python, no Qt).

* **Channels.**  What the Ministry of Communications allows (wireless-regdb, country IL):

  - 2.4 GHz: channels 1-13 (2402-2482 MHz), 20 or 40 MHz wide, 20 dBm EIRP.
  - 5 GHz: channels 36-64 (5150-5350 MHz, indoors, 23 dBm; 52-64 need radar detection, DFS) and 100-140
    (5470-5725 MHz, DFS, 27 dBm), 20 / 40 / 80 / 160 MHz wide.  Channel 144 and 149-165 are not allowed.
  - 6 GHz: channels 1-93 (5945-6425 MHz, indoors only, 23 dBm), 20 / 40 / 80 / 160 MHz wide.

  A wide channel is a fixed block of 20 MHz channels (36-48 is the 80 MHz block of channel 36, 40, 44 or 48);
  the *primary* channel is the one of the block an access point announces.  At 2.4 GHz a 40 MHz channel takes
  the channel four above the primary (four below for 10-13).

* **Overlap.**  How much of a transmitter's power lands in a receiver's channel: the 802.11 OFDM transmit
  spectrum mask (flat to W/2 - 1 MHz, -20 dB at W/2 + 1 MHz, -28 dB at W, -40 dB at 1.5 W, -45 dB beyond 2 W)
  integrated over the receiver's channel.  Channels 1 and 6 at 2.4 GHz come out about 30 dB apart, 1 and 3
  about 3 dB: the familiar "1, 6, 11".  The mask is the worst a transmitter may be; real ones are cleaner far
  from their channel, so the far-away figures are pessimistic.
"""
from __future__ import annotations

import math
from functools import lru_cache

BANDS = ("2.4", "5", "6")
WIDTHS = {"2.4": (20, 40), "5": (20, 40, 80, 160), "6": (20, 40, 80, 160)}
DEFAULT_WIDTH = {"2.4": 20, "5": 80, "6": 80}
CHANNELS = {
    "2.4": tuple(range(1, 14)),
    "5": (36, 40, 44, 48, 52, 56, 60, 64, 100, 104, 108, 112, 116, 120, 124, 128, 132, 136, 140),
    "6": tuple(range(1, 94, 4)),
}
DFS_5 = frozenset(range(52, 145, 4))
# where the bonded blocks of 5 / 6 GHz start counting (channel numbers step 4 per 20 MHz)
_BLOCK_BASE = {"5": (36, 100), "6": (1,)}
NO_OVERLAP_DB = -100.0            # what overlap_db returns when nothing lands in the receiver's channel


def channel_mhz(band: str, ch: int) -> float:
    """Centre frequency of a 20 MHz channel."""
    if band == "2.4":
        return 2407.0 + 5.0 * ch
    if band == "6":
        return 5950.0 + 5.0 * ch
    return 5000.0 + 5.0 * ch


def is_channel(band: str, ch) -> bool:
    return isinstance(ch, int) and ch in CHANNELS.get(band, ())


def block(band: str, ch: int, width: int) -> tuple[int, ...] | None:
    """The 20 MHz channels a ``width`` MHz channel with primary ``ch`` occupies, or None when Israel does not
    allow it (or the width does not exist in the band)."""
    if not is_channel(band, ch) or width not in WIDTHS.get(band, ()):
        return None
    n = width // 20
    if band == "2.4":
        if width == 20:
            return (ch,)
        other = ch + 4 if ch <= 9 else ch - 4
        return tuple(sorted((ch, other))) if is_channel(band, other) else None
    base = max(b for b in _BLOCK_BASE[band] if b <= ch)
    start = base + ((ch - base) // (4 * n)) * (4 * n)
    chans = tuple(start + 4 * i for i in range(n))
    return chans if all(is_channel(band, c) for c in chans) else None


def widths(band: str, ch: int) -> list[int]:
    """The widths a primary channel can have."""
    return [w for w in WIDTHS.get(band, ()) if block(band, ch, w) is not None]


def fit_width(band: str, ch: int, width: int) -> int:
    """``width`` when it is possible on ``ch``, else the widest possible one below it (at least 20)."""
    ok = widths(band, ch)
    if width in ok:
        return width
    below = [w for w in ok if w <= width]
    return max(below) if below else 20


def centre_mhz(band: str, ch: int, width: int) -> float:
    """Centre frequency of the whole (bonded) channel."""
    chans = block(band, ch, width) or (ch,)
    return (channel_mhz(band, chans[0]) + channel_mhz(band, chans[-1])) / 2.0


def block_starts(band: str, width: int) -> list[int]:
    """One primary channel per possible block of this width (its lowest channel), low to high."""
    out = []
    for ch in CHANNELS.get(band, ()):
        b = block(band, ch, width)
        if b is not None and b[0] == ch and (band != "2.4" or width == 20 or ch <= 9):
            out.append(ch)
    return out


def plan_channels(band: str, width: int) -> list[int]:
    """The channels a planner picks from: 1 / 6 / 11 at 2.4 GHz and 20 MHz (the three that do not overlap),
    else one primary channel per block."""
    if band == "2.4" and width == 20:
        return [1, 6, 11]
    if band == "2.4":
        return [1, 9] if width == 40 else [1, 6, 11]       # 1+5 and 9+13
    return block_starts(band, width)


def default_channel(band: str, width: int, k: int) -> int:
    """The channel the ``k``-th access point on a band gets when none was set: the planner's list in turn."""
    opts = plan_channels(band, width) or list(CHANNELS.get(band, (1,)))
    return opts[k % len(opts)]


def is_dfs(band: str, ch: int, width: int = 20) -> bool:
    """True when the channel (any part of a bonded one) needs radar detection: it may go quiet for a minute
    at start-up and jump channel when a radar is heard."""
    return band == "5" and any(c in DFS_5 for c in (block(band, ch, width) or (ch,)))


def legal_eirp(band: str, ch: int, width: int = 20) -> float:
    """Most EIRP (dBm) Israel allows on this channel (the strictest part of a bonded channel)."""
    if band == "2.4":
        return 20.0
    if band == "6":
        return 23.0
    chans = block(band, ch, width) or (ch,)
    return min(23.0 if c <= 64 else 27.0 for c in chans)


def channel_title(band: str, ch: int, width: int) -> str:
    """'36 · 80 MHz (36-48)', '6 · 20 MHz', '100 · 40 MHz (100-104) · DFS'."""
    chans = block(band, ch, width) or (ch,)
    span = f" ({chans[0]}-{chans[-1]})" if len(chans) > 1 else ""
    return f"{ch} · {width} MHz{span}" + (" · DFS" if is_dfs(band, ch, width) else "")


def span_mhz(band: str) -> tuple[float, float]:
    """Lowest and highest frequency of the band's allowed channels."""
    chans = CHANNELS[band]
    return channel_mhz(band, chans[0]) - 10.0, channel_mhz(band, chans[-1]) + 10.0


# ---------------------------------------------------------------------------- overlap
def mask_db(offset_mhz: float, width: int) -> float:
    """The 802.11 OFDM transmit spectrum mask, dB relative to the channel's flat top, ``offset_mhz`` from its
    centre."""
    w = float(width)
    off = abs(offset_mhz)
    pts = ((0.0, 0.0), (w / 2 - 1.0, 0.0), (w / 2 + 1.0, -20.0), (w, -28.0), (1.5 * w, -40.0), (2.0 * w, -45.0))
    if off >= pts[-1][0]:
        return -45.0
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        if off <= x1:
            return y0 + (y1 - y0) * (off - x0) / (x1 - x0)
    return -45.0


_STEP = 0.25                      # MHz: integration step


@lru_cache(maxsize=4096)
def _overlap(delta: float, tx_w: int, rx_w: int) -> float:
    reach = 2.5 * tx_w                                     # beyond this the transmitter's power is negligible
    n = int(round(2 * reach / _STEP))
    total = inside = 0.0
    lo, hi = delta - rx_w / 2.0, delta + rx_w / 2.0         # the receiver's channel, relative to the transmitter
    for i in range(n):
        f = -reach + (i + 0.5) * _STEP
        p = 10.0 ** (mask_db(f, tx_w) / 10.0)
        total += p
        if lo <= f <= hi:
            inside += p
    return 10.0 * math.log10(inside / total) if inside > 0.0 else NO_OVERLAP_DB


def overlap_db(tx_mhz: float, tx_w: int, rx_mhz: float, rx_w: int) -> float:
    """Share (dB, at most 0) of a transmitter's power, centred on ``tx_mhz`` and ``tx_w`` MHz wide, that falls
    in a receiver's channel centred on ``rx_mhz``, ``rx_w`` MHz wide."""
    return _overlap(round(rx_mhz - tx_mhz, 3), int(tx_w), int(rx_w))


def radio_overlap_db(tx, rx) -> float:
    """``overlap_db`` between two radios (anything with band, channel, width); NO_OVERLAP_DB across bands."""
    if tx.band != rx.band:
        return NO_OVERLAP_DB
    return overlap_db(centre_mhz(tx.band, tx.channel, tx.width), tx.width,
                      centre_mhz(rx.band, rx.channel, rx.width), rx.width)


def channel_overlap_db(tx, band: str, ch: int, width: int = 20) -> float:
    """How much of a radio's power lands in ``ch`` (``width`` MHz wide) of ``band``."""
    if tx.band != band:
        return NO_OVERLAP_DB
    return overlap_db(centre_mhz(tx.band, tx.channel, tx.width), tx.width, centre_mhz(band, ch, width), width)
