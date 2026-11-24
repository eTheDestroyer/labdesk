"""What the floor heat map can show, and how each map is coloured (numpy, no Qt).

* ``signal``  - the strongest of your radios (dBm): the map the view always had.
* ``sinr``    - signal against noise plus everyone else's interference (dB): what decides the speed.
* ``sharing`` - how many other radios a device here waits for before it may talk (``wifi_interference``).
* ``channel`` - how loud one channel is everywhere (dBm that every radio, yours and the neighbours', puts in it):
  where a new network on that channel would have to share.

Every map is drawn with the same colour ramp as the signal: a value is turned into the dBm the ramp would give
that colour (``Scale.to_ramp``), so the graphics card, the software painter and the legend need nothing new.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from . import wifi_channels as wc
from . import wifi_interference as wi
from . import wifi_site as ws

RAMP_LO, RAMP_HI = -85.0, -40.0   # the colour ramp, in dBm: below RAMP_LO the floor shows through
F32 = np.float32
INF = math.inf


@dataclass(frozen=True)
class Scale:
    key: str
    title: str                    # the legend's heading
    lo: float                     # value drawn in the ramp's worst colour
    hi: float                     # ... in its best colour
    classes: tuple                # ((limit, name, limit text), ...) best first
    none_row: tuple | None        # (name, limit text) of what is worse than the last class (drawn see-through)
    contours: tuple               # values with a line on the floor
    unit: str = "dBm"
    clip: bool = False            # values past ``lo`` keep the worst colour instead of going see-through

    @property
    def higher_better(self) -> bool:
        return self.hi > self.lo

    def to_ramp(self, v):
        """The ramp's dBm for a value (works on floats and arrays)."""
        eq = RAMP_LO + (np.asarray(v, dtype=np.float64) - self.lo) * ((RAMP_HI - RAMP_LO) / (self.hi - self.lo))
        if self.clip:
            eq = np.maximum(eq, RAMP_LO + 0.01)
        return eq if np.ndim(eq) else float(eq)

    def fmt(self, v: float) -> str:
        if self.unit == "dBm":
            return f"−{abs(v):.0f} dBm" if v < 0 else f"{v:.0f} dBm"
        if self.unit == "dB":
            return f"{v:.0f} dB"
        n = int(math.ceil(v))
        return f"{n}+ others" if n > 1 else "shared"

    def class_index(self, v: float) -> int:
        for i, (limit, _n, _t) in enumerate(self.classes):
            if (v >= limit) if self.higher_better else (v <= limit):
                return i
        return len(self.classes)

    def class_name(self, v: float) -> str:
        i = self.class_index(v)
        return self.classes[i][1] if i < len(self.classes) else (self.none_row or ("", ""))[0]

    def rows(self) -> list[tuple]:
        """(name, limit text) per legend row: the classes, then the 'none' row if there is one."""
        return [(n, t) for _l, n, t in self.classes] + ([self.none_row] if self.none_row else [])

    def shares(self, plane: np.ndarray, mask: np.ndarray | None = None) -> list[float]:
        """Share (0..100 %) of the plane in each legend row."""
        vals = plane[mask] if mask is not None and mask.shape == plane.shape and mask.any() else plane.ravel()
        idx = np.full(vals.shape, len(self.classes), np.int64)
        for i in range(len(self.classes) - 1, -1, -1):
            limit = self.classes[i][0]
            idx[(vals >= limit) if self.higher_better else (vals <= limit)] = i
        counts = np.bincount(idx, minlength=len(self.rows()))[:len(self.rows())]
        total = max(1, int(counts.sum()))
        return [100.0 * int(c) / total for c in counts]

    def swatch(self, i: int) -> float:
        """The ramp's dBm in the middle of legend row ``i``."""
        if i >= len(self.classes):
            return RAMP_LO - 1.0
        a = self.classes[i][0]
        b = self.hi if i == 0 else self.classes[i - 1][0]
        if not math.isfinite(a):
            a = self.lo
        if not math.isfinite(b):
            b = self.hi
        lo, hi = sorted((self.to_ramp(a), self.to_ramp(b)))
        return (max(lo, RAMP_LO) + min(hi, RAMP_HI)) / 2.0


SIGNAL = Scale("signal", "Wi-Fi signal strength", RAMP_LO, RAMP_HI,
               tuple((f, t, f"−{abs(f):.0f} dBm or better") for f, t, _u in ws.SIGNAL_BANDS),
               (ws.NO_SERVICE, f"below −{abs(ws.SIGNAL_BANDS[-1][0]):.0f} dBm"),
               tuple(b[0] for b in ws.SIGNAL_BANDS if b[0] > RAMP_LO))
SINR = Scale("sinr", "Signal against interference (SINR)", 5.0, 40.0,
             ((30.0, "Excellent", "30 dB or better"), (25.0, "Very good", "25 dB or better"),
              (20.0, "Good", "20 dB or better"), (12.0, "Fair", "12 dB or better"), (5.0, "Poor", "5 dB or better")),
             ("Unusable", "below 5 dB"), (30.0, 25.0, 20.0, 12.0), unit="dB")
SHARING = Scale("sharing", "Radios sharing the channel", 4.0, -0.5,
                ((0.0, "Alone", "the channel is free"), (1.0, "Shared by 2", "1 other radio"),
                 (2.0, "Shared by 3", "2 other radios"), (INF, "Crowded", "3 or more others")),
                None, (0.5, 1.5, 2.5), unit="", clip=True)
CHANNEL = Scale("channel", "Energy on one channel", -45.0, -95.0,
                ((-90.0, "Quiet", "below −90 dBm"), (-82.0, "Faint", "noise only, below −82"),
                 (-72.0, "Shared", "Wi-Fi waits for it, −82…−72"), (-62.0, "Busy", "−72…−62 dBm"),
                 (INF, "Saturated", "−62 dBm or louder")),
                None, (-82.0, -72.0, -62.0), clip=True)
SCALES = {s.key: s for s in (SIGNAL, SINR, SHARING, CHANNEL)}
METRICS = {"signal": "Signal strength", "sinr": "Signal against interference (SINR)",
           "sharing": "Radios sharing the channel", "channel": "Interference on one channel"}
SUBTITLES = {"signal": "Strongest access point", "sinr": "Serving radio against noise and every other radio",
             "sharing": "Other radios a device here waits for", "channel": "Every radio's power in channel"}


def map_volumes(engine, metric: str, band: str | None, channel: tuple | None = None) -> tuple:
    """(values, ramp dBm) volumes ``[z, y, x]`` of a map on the engine's grid, or (None, None) for
    ``signal`` (the engine's own volume is the map).  ``band`` None = every band (the best SINR wins);
    ``channel`` = (band, channel number) for the channel map."""
    shape = engine.grid.shape
    if metric == "channel":
        b, ch = channel if channel else ("5", 36)
        vals = wi.channel_load(engine.band_volumes(b), b, ch, 20, shape)
        return vals, SCALES["channel"].to_ramp(vals).astype(F32)
    if metric not in ("sinr", "sharing"):
        return None, None
    per = [wi.Metrics(engine.band_volumes(b), shape) for b in ((band,) if band else ws.BANDS)]
    signal, sinr, count = wi.combine(per, shape)
    covered = signal >= F32(RAMP_LO)
    if metric == "sinr":
        vals = np.where(covered, sinr, F32(ws.NO_SIGNAL)).astype(F32)
        return vals, SCALES["sinr"].to_ramp(vals).astype(F32)
    ramp = SCALES["sharing"].to_ramp(count).astype(F32)
    ramp[~covered] = ws.NO_SIGNAL
    vals = np.where(covered, count, F32(np.nan)).astype(F32)
    return vals, ramp


def channel_choices(site: ws.Site, band: str | None) -> list[tuple]:
    """[((band, channel), title)] for the channel map: every 20 MHz channel of the band(s) with radios."""
    bands = (band,) if band else [b for b in ws.BANDS if site.emitters(b, own=True, neighbours=True)] or ["5"]
    out = []
    for b in bands:
        for ch in wc.CHANNELS[b]:
            out.append(((b, ch), f"{ws.BAND_TITLES[b]} · channel {ch}" + (" · DFS" if wc.is_dfs(b, ch) else "")))
    return out
