"""Interference between the radios of a site (no Qt).

Every radio that transmits is an ``Emitter`` (``Site.emitters``): your access points' radios and the
neighbours' networks.  At a point, on one band:

* **Serving radio**: the strongest of your radios (a neighbour's network never serves you).
* **Interference**: the power of every other radio on the band, each times the share of its spectrum that lands
  in the serving channel (``wifi_channels.overlap_db``).  Same channel: all of it; channels 1 and 6 at
  2.4 GHz: about 1/1000.
* **SINR** = signal - (noise + interference), in dB.  Noise: -95 dBm in 20 MHz, +3 dB per doubling of the width.
* **Sharing the channel**: the radios a device here *waits for* before it may talk (802.11 clear channel
  assessment): a radio on the same channel heard at -82 dBm or more, or any radio whose energy in the channel
  reaches -62 dBm.  Every one of them takes airtime: two access points on one channel each get about half.

What is not counted: who is actually busy (the model assumes every radio transmits; real networks are idle much
of the time, so the SINR is the bad hour), clients' own transmissions, microwave ovens, Bluetooth and radar.

``Metrics`` gives the same numbers for a whole voxel volume (numpy), from the field engine's per-radio volumes;
``reading`` is the point-by-point reference the tests compare it with.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

from . import wifi_channels as wc
from . import wifi_link as wl
from . import wifi_site as ws

CCA_PD_DBM = -82.0                # a Wi-Fi radio waits for a transmission on its channel it hears this loud
CCA_ED_DBM = -62.0                # ... and for any energy this loud in its channel
SAME_CHANNEL_DB = -3.0            # overlap above this: the other radio is on the same channel (its preamble decodes)
NO_DBM = ws.NO_SIGNAL
F32 = np.float32


def noise_dbm(width: int) -> float:
    return wl.NOISE_20MHZ_DBM + 3.0 * math.log2(max(20, int(width)) / 20.0)


def shares(level_dbm: float, overlap_db: float) -> bool:
    """True when a radio whose power here is ``level_dbm`` and whose spectrum lands ``overlap_db`` in my
    channel makes my radio wait (it takes airtime from me)."""
    inside = level_dbm + overlap_db
    return (overlap_db >= SAME_CHANNEL_DB and inside >= CCA_PD_DBM) or inside >= CCA_ED_DBM


def _db(lin: float) -> float:
    return 10.0 * math.log10(lin) if lin > 0.0 else NO_DBM


def _lin(dbm: float) -> float:
    return 10.0 ** (dbm / 10.0) if dbm > NO_DBM else 0.0


# ---------------------------------------------------------------------------- one point
@dataclass
class Reading:
    band: str | None
    signal: float = NO_DBM                     # dBm of the serving radio
    serving: ws.Emitter | None = None
    interference: float = NO_DBM               # dBm that lands in the serving channel from everyone else
    noise: float = wl.NOISE_20MHZ_DBM
    sinr: float = NO_DBM                       # dB
    sharing: list = field(default_factory=list)   # [(Emitter, dBm in my channel)] the radios a device here waits for
    heard: list = field(default_factory=list)     # [(Emitter, dBm)] every radio on the band, strongest first

    def sharing_text(self) -> str:
        """'shares channel 36 with AP2 (−71 dBm), Neighbour (−80 dBm)' or 'channel 36 to itself'."""
        if self.serving is None:
            return ""
        ch = self.serving.channel
        if not self.sharing:
            return f"channel {ch} to itself"
        who = ", ".join(f"{e.name}{' (neighbour)' if e.neighbour else ''} {_fmt(v)}" for e, v in self.sharing[:3])
        more = f" and {len(self.sharing) - 3} more" if len(self.sharing) > 3 else ""
        return f"shares channel {ch} with {who}{more}"


def _fmt(v: float) -> str:
    return f"−{abs(v):.0f} dBm" if v < 0 else f"{v:.0f} dBm"


def reading(site: ws.Site, pt: tuple, band: str, walls=None, emitters=None) -> Reading:
    """Signal, interference, SINR and the radios sharing the channel at ``pt`` on one band."""
    walls = site.walls() if walls is None else walls
    ems = site.emitters(band, own=True, neighbours=True) if emitters is None else emitters
    heard = sorted(((e, site.rssi(e, pt, walls)) for e in ems), key=lambda t: -t[1])
    out = Reading(band, heard=heard)
    own = [(e, v) for e, v in heard if not e.neighbour]
    if not own:
        return out
    s, sv = own[0]
    out.signal, out.serving, out.noise = sv, s, noise_dbm(s.width)
    inter = 0.0
    for e, v in heard:
        if e is s:
            continue
        ov = wc.radio_overlap_db(e, s)
        inter += _lin(v + ov)
        if shares(v, ov):
            out.sharing.append((e, v + ov))
    out.interference = _db(inter)
    out.sinr = sv - _db(_lin(out.noise) + inter)
    return out


def best_reading(site: ws.Site, pt: tuple, band: str | None = None, walls=None) -> Reading:
    """``reading`` on ``band``, or with ``band`` None on the band with the best SINR (a phone picks it)."""
    if band is not None:
        return reading(site, pt, band, walls)
    walls = site.walls() if walls is None else walls
    best = Reading(None)
    for b in ws.BANDS:
        r = reading(site, pt, b, walls)
        if r.serving is not None and (best.serving is None or r.sinr > best.sinr):
            best = r
    return best


def spectrum(site: ws.Site, pt: tuple, band: str, walls=None) -> list[tuple]:
    """Every radio on ``band`` as heard at ``pt``: [(Emitter, dBm)], strongest first (a Wi-Fi analyser)."""
    return reading(site, pt, band, walls).heard


def channel_level(site: ws.Site, pt: tuple, band: str, ch: int, width: int = 20, walls=None) -> float:
    """Total power (dBm) every radio puts into channel ``ch`` (``width`` MHz) at ``pt``."""
    walls = site.walls() if walls is None else walls
    total = 0.0
    for e in site.emitters(band, own=True, neighbours=True):
        total += _lin(site.rssi(e, pt, walls) + wc.channel_overlap_db(e, band, ch, width))
    return _db(total)


# ---------------------------------------------------------------------------- whole volumes (numpy)
def _lin_arr(v: np.ndarray) -> np.ndarray:
    out = np.power(F32(10.0), v * F32(0.1), dtype=F32)
    out[v <= NO_DBM] = 0.0
    return out


def _db_arr(lin: np.ndarray) -> np.ndarray:
    with np.errstate(divide="ignore"):
        out = (F32(10.0) * np.log10(lin)).astype(F32)
    out[~(lin > 0.0)] = NO_DBM
    return out


class Metrics:
    """Serving signal, SINR and the number of radios sharing the channel over a volume, on one band, from
    ``items`` = [(Emitter, dBm volume)] (your radios and the neighbours' on that band)."""

    def __init__(self, items: list[tuple], shape: tuple):
        self.items = items
        own = [(k, e, v) for k, (e, v) in enumerate(items) if not e.neighbour]
        self.signal = np.full(shape, NO_DBM, F32)
        self.sinr = np.full(shape, NO_DBM, F32)
        self.sharing = np.zeros(shape, F32)
        self.interference = np.full(shape, NO_DBM, F32)
        self.serving = np.full(shape, -1, np.int16)            # index into items
        if not own:
            return
        stack = np.stack([v for _k, _e, v in own])
        pick = np.argmax(stack, axis=0)
        self.signal = np.take_along_axis(stack, pick[None], 0)[0].astype(F32)
        lins = [_lin_arr(v) for _e, v in items]
        for j, (k, s, _v) in enumerate(own):
            here = pick == j
            if not here.any():
                continue
            self.serving[here] = k
            inter = np.zeros(shape, F32)
            count = np.zeros(shape, F32)
            for m, (e, v) in enumerate(items):
                if m == k:
                    continue
                ov = wc.radio_overlap_db(e, s)
                if ov <= wc.NO_OVERLAP_DB:
                    continue
                inter += lins[m] * F32(10.0 ** (ov / 10.0))
                inside = v + F32(ov)
                share = inside >= F32(CCA_ED_DBM)
                if ov >= SAME_CHANNEL_DB:
                    share |= inside >= F32(CCA_PD_DBM)
                count += share
            n = F32(10.0 ** (noise_dbm(s.width) / 10.0))
            sinr = self.signal - _db_arr(inter + n)
            self.sinr[here] = sinr[here]
            self.sharing[here] = count[here]
            self.interference[here] = _db_arr(inter)[here]


def combine(per_band: list[Metrics], shape: tuple) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """(signal, SINR, sharing) over several bands: the band with the best SINR wins at every voxel (a phone
    picks it); the signal is the strongest of any band."""
    signal = np.full(shape, NO_DBM, F32)
    sinr = np.full(shape, NO_DBM, F32)
    count = np.zeros(shape, F32)
    for m in per_band:
        np.maximum(signal, m.signal, out=signal)
        better = m.sinr > sinr
        sinr[better] = m.sinr[better]
        count[better] = m.sharing[better]
    return signal, sinr, count


def channel_load(items: list[tuple], band: str, ch: int, width: int, shape: tuple) -> np.ndarray:
    """dBm that every radio of ``items`` puts into channel ``ch`` (``width`` MHz) of ``band``, per voxel."""
    total = np.zeros(shape, F32)
    for e, v in items:
        ov = wc.channel_overlap_db(e, band, ch, width)
        if ov > wc.NO_OVERLAP_DB:
            total += _lin_arr(v) * F32(10.0 ** (ov / 10.0))
    return _db_arr(total)


# ---------------------------------------------------------------------------- the channel plan
@dataclass(frozen=True)
class Conflict:
    a: ws.Emitter                 # your radio
    b: ws.Emitter                 # the radio it waits for (yours or a neighbour's)
    dbm: float                    # b's power in a's channel, at a

    def text(self) -> str:
        band = ws.BAND_TITLES[self.a.band]
        if self.b.neighbour:
            return (f"{self.a.name} hears the neighbours' {self.b.name} in its channel {self.a.channel} ({band}) at "
                    f"{_fmt(self.dbm)}: they share the airtime")
        return (f"{self.a.name} and {self.b.name} overlap on channel {self.a.channel} ({band}) and hear each other at "
                f"{_fmt(self.dbm)}: they take turns on the air. Auto channels may separate them")


def channel_conflicts(site: ws.Site, walls=None) -> list[Conflict]:
    """Pairs of radios where one makes the other wait (``shares``), access point to access point."""
    walls = site.walls() if walls is None else walls
    out = []
    for band in ws.BANDS:
        ems = site.emitters(band, own=True, neighbours=True)
        for a in ems:
            if a.neighbour:
                continue
            for b in ems:
                if b is a or (not b.neighbour and b.index < a.index):
                    continue                                   # one line per pair of your own radios
                ov = wc.radio_overlap_db(b, a)
                if ov <= wc.NO_OVERLAP_DB:
                    continue
                v = site.rssi(b, (a.x, a.y, a.z), walls)
                if shares(v, ov):
                    out.append(Conflict(a, b, v + ov))
    return out


DFS_PENALTY_DBM = -92.0           # planning: a DFS channel counts as if a faint radio sat on it (radar risk)


def auto_channels(site: ws.Site, bands=None) -> list[tuple]:
    """Give your radios the channels that interfere least with each other and with the neighbours (who are
    kept as they are): a greedy start, then improving one radio at a time until nothing gets better.  Widths are
    kept.  Returns [(access point name, band, old channel, new channel)] for the radios that changed."""
    walls = site.walls()
    changes = []
    for band in bands or ws.BANDS:
        ems = site.emitters(band, own=True, neighbours=True)
        own = [e for e in ems if not e.neighbour]
        if not own:
            continue
        others = [e for e in ems if e.neighbour]
        # power of radio j at radio i (mW)
        gain = {(i, j): _lin(site.rssi(b, (a.x, a.y, a.z), walls))
                for i, a in enumerate(own) for j, b in enumerate(own + others) if i != j}
        cands = [wc.plan_channels(band, e.width) or [e.channel] for e in own]
        chan = [e.channel if e.channel in c else c[0] for e, c in zip(own, cands)]

        def as_em(i: int, ch: int) -> ws.Emitter:
            e = own[i]
            return ws.Emitter(e.name, e.x, e.y, e.z, e.tx_dbm, band, ch, e.width, False, e.index)

        def cost_of(i: int, ch: int) -> float:
            me = as_em(i, ch)
            total = _lin(DFS_PENALTY_DBM) if wc.is_dfs(band, ch, me.width) else 0.0
            for j, other in enumerate(own + others):
                if j == i:
                    continue
                o = as_em(j, chan[j]) if j < len(own) else other
                total += gain[(i, j)] * _lin(wc.radio_overlap_db(o, me))       # what I hear
                if j < len(own):
                    total += gain[(j, i)] * _lin(wc.radio_overlap_db(me, o))   # what I do to them
            return total

        order = sorted(range(len(own)), key=lambda i: -sum(gain[(i, j)] for j in range(len(own) + len(others))
                                                           if j != i))
        placed: set = set()
        for i in order:                                   # greedy: the most crowded radio picks first
            best = None
            for ch in cands[i]:
                c = _placed_cost(i, ch, placed, own, others, gain, as_em, chan, band)
                if best is None or c < best[0] - 1e-18:
                    best = (c, ch)
            chan[i] = best[1]
            placed.add(i)
        for _sweep in range(12):                          # then improve one radio at a time
            moved = False
            for i in order:
                now = cost_of(i, chan[i])
                for ch in cands[i]:
                    c = cost_of(i, ch)
                    if c < now * 0.999 - 1e-18:
                        now, chan[i], moved = c, ch, True
            if not moved:
                break
        for i, e in enumerate(own):
            if chan[i] != e.channel or not (site.aps[e.index].radio(band) or ws.Radio()).channel:
                ap = site.aps[e.index]
                ap.set_radio(band, True, channel=chan[i])
                if chan[i] != e.channel:
                    changes.append((e.name, band, e.channel, chan[i]))
    return changes


def _placed_cost(i, ch, placed, own, others, gain, as_em, chan, band) -> float:
    """Greedy step: what radio ``i`` on ``ch`` hears from and does to the radios placed so far and the
    neighbours."""
    me = as_em(i, ch)
    total = _lin(DFS_PENALTY_DBM) if wc.is_dfs(band, ch, me.width) else 0.0
    for j in placed:
        o = as_em(j, chan[j])
        total += gain[(i, j)] * _lin(wc.radio_overlap_db(o, me)) + gain[(j, i)] * _lin(wc.radio_overlap_db(me, o))
    for k, other in enumerate(others):
        total += gain[(i, len(own) + k)] * _lin(wc.radio_overlap_db(other, me))
    return total
