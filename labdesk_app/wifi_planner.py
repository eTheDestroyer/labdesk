"""Plan the access points of a site: how many, where, and on which channels (numpy, no Qt).

The goal is a floor in one colour of the signal map: at least ``target_dbm`` (−60 dBm, "Very good", green by
default) on ``goal_pct`` of the customer's floor, with as few access points as possible, and with no two of them
making each other wait (sharing a channel).

1. **The floor to cover.**  The measuring plane of the site, on a grid of about ``CELLS`` points, without the
   rooms labelled outside and without the floor inside furniture (a wardrobe needs no Wi-Fi).
2. **Where an access point may go.**  A grid of ceiling spots over the indoor floor (plus the middle of every
   room), off the walls and out of furniture that reaches the mounting height.  The signal of every spot over the
   whole floor is worked out once, with the same model as the map (``FieldEngine`` on a one-layer grid).
3. **How many.**  Greedy cover: the spot that turns the most still-uncovered floor green, again and again until
   the goal is met.  Then every access point is moved to the spot that serves the floor best with the others in
   place, and the planner tries to do the same with one access point fewer, as long as that works.
4. **No disturbance.**  Channels from ``wifi_interference.auto_channels`` (the neighbours' networks are counted);
   when two of your access points would still share one, narrower channels are tried (more of them fit:
   80, then 40, then 20 MHz).  At 2.4 GHz, where only 1, 6 and 11 do not overlap, a radio that would share is
   turned down and then switched off: the plan's band carries the floor.

An estimate from the model, like the map: a site survey after installing is still worth it.
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from types import SimpleNamespace

import numpy as np

from . import wifi_channels as wc
from . import wifi_field as wf
from . import wifi_interference as wi
from . import wifi_site as ws

F32 = np.float32
CELLS = 12_000                    # floor points the plan is checked on
CANDIDATES = 360                  # about this many ceiling spots are tried
HEADROOM_DB = 6.0                 # tie-break: margin above the target counts up to this much
WALL_CLEAR_M = 0.25               # an access point keeps this far from a wall
MIN_TX_24 = 8.0                   # dBm: 2.4 GHz radios are turned down this far before they are switched off
TARGETS = [(-50.0, "Excellent · dark green"), (-60.0, "Very good · green"), (-67.0, "Good · yellow-green")]


@dataclass
class PlanOptions:
    target_dbm: float = -60.0     # the signal every spot should get
    goal_pct: float = 98.0        # ... on this share of the indoor floor
    band: str = "5"               # the band that has to reach it (what phones use)
    dual_band: bool = True        # every access point also gets a 2.4 GHz radio (for old devices)
    tx_dbm: float = 20.0          # EIRP of the plan's band
    height: float | None = None   # mounting height (None: 30 cm under the ceiling)
    keep: bool = False            # keep your access points and add to them (else they are replaced)
    max_aps: int = 40


@dataclass
class Plan:
    aps: list                     # the site's new list of access points (yours, then the neighbours as they were)
    added: list                   # names of the access points the plan placed
    count: int                    # how many access points serve the floor (placed + kept)
    covered_pct: float            # share of the indoor floor at the target or better
    reachable_pct: float          # the most any number of access points could cover (walls can make 100 % impossible)
    target_dbm: float
    band: str
    width: int                    # channel width of the plan's band
    conflicts: list = field(default_factory=list)      # your access points that still share a channel
    neighbour_conflicts: list = field(default_factory=list)
    sinr_ok_pct: float = 0.0      # share of the floor with SINR of 25 dB or better on the plan's band
    notes: list = field(default_factory=list)
    seconds: float = 0.0

    def summary(self) -> str:
        band = ws.BAND_TITLES[self.band]
        n = self.count
        lines = [f"{n} access point{'s' if n != 1 else ''}: {self.covered_pct:.0f} % of the floor at "
                 f"−{abs(self.target_dbm):.0f} dBm or better on {band}"]
        if self.reachable_pct < 99.95:
            lines.append(f"Walls and furniture keep {100.0 - self.reachable_pct:.0f} % of the floor below it however "
                         "many access points there are; the goal counts the rest.")
        if not self.conflicts:
            lines.append(f"Channels: {self.width} MHz, no two of your access points share a channel.")
        else:
            pairs = "; ".join(f"{c.a.name} and {c.b.name} on {c.a.channel} ({c.dbm:.0f} dBm)"
                              for c in self.conflicts[:3])
            lines.append(f"Channels: {self.width} MHz. {len(self.conflicts)} pair(s) are too close to have a channel "
                         f"each and take turns: {pairs}" + (" …" if len(self.conflicts) > 3 else ""))
        if self.neighbour_conflicts:
            lines.append(f"{len(self.neighbour_conflicts)} radio(s) still hear a neighbour's network on their "
                         "channel (out of your hands).")
        lines.append(f"SINR 25 dB or better on {self.sinr_ok_pct:.0f} % of the floor.")
        lines += self.notes
        return "\n".join(lines)


# ---------------------------------------------------------------------------- the floor and the spots
def mount_height(site: ws.Site, opts: PlanOptions) -> float:
    z = opts.height if opts.height is not None else site.height - 0.3
    return ws.clamp(z, 0.5, max(0.5, site.height - 0.05))


def _plane_engine(site: ws.Site) -> tuple[wf.FieldEngine, np.ndarray]:
    """A field engine on a one-layer grid at the measuring height, and the floor mask ``[y, x]`` to cover."""
    eng = wf.FieldEngine(site)
    indoor = max(1.0, site.width * site.depth - sum((x1 - x0) * (y1 - y0) for x0, y0, x1, y1 in site.outside_rects()))
    cell = max(0.12, math.sqrt(indoor / CELLS))
    g = wf.Grid(site.width, site.depth, site.height, max(2, round(site.width / cell)), max(2, round(site.depth / cell)), 1)
    g.zd = np.array([site.slice_z], dtype=np.float64)
    g.z = g.zd.astype(F32)
    eng.grid = g
    eng._walls = site.walls()
    mask = wf.indoor_mask(site, g)
    mask = np.ones((g.ny, g.nx), bool) if mask is None else mask.copy()
    z = site.slice_z
    for o in site.obstacles:                                  # the floor inside furniture needs no Wi-Fi
        if o.z0 <= z <= o.z0 + o.h:
            mask[np.ix_((g.yd > o.y) & (g.yd < o.y + o.d), (g.xd > o.x) & (g.xd < o.x + o.w))] = False
    return eng, mask


def _off_walls(x: float, y: float, walls) -> bool:
    for w in walls:
        a, b = (x, y) if w.axis == "x" else (y, x)
        if abs(a - w.a) < WALL_CLEAR_M and w.b0 - WALL_CLEAR_M <= b <= w.b1 + WALL_CLEAR_M:
            return False
    return True


def candidates(site: ws.Site, z: float) -> list[tuple[float, float]]:
    """Ceiling spots an access point may take: a grid over the indoor floor and the middle of every room."""
    outside = site.outside_rects()
    walls = site.walls()
    indoor = max(1.0, site.width * site.depth - sum((x1 - x0) * (y1 - y0) for x0, y0, x1, y1 in outside))
    step = max(0.5, math.sqrt(indoor / CANDIDATES))

    def ok(x: float, y: float) -> bool:
        if not (0.1 < x < site.width - 0.1 and 0.1 < y < site.depth - 0.1):
            return False
        if any(x0 <= x <= x1 and y0 <= y <= y1 for x0, y0, x1, y1 in outside):
            return False
        if any(o.x - 0.1 <= x <= o.x + o.w + 0.1 and o.y - 0.1 <= y <= o.y + o.d + 0.1 and o.z0 - 0.2 <= z <= o.z0 + o.h
               for o in site.obstacles):
            return False
        return _off_walls(x, y, walls)

    out = []
    for room, _lb in site.named_rooms():
        x, y = room.label_point
        if ok(x, y):
            out.append((round(x, 2), round(y, 2)))
    nx, ny = max(1, int(site.width / step)), max(1, int(site.depth / step))
    for j in range(ny):
        for i in range(nx):
            x, y = (i + 0.5) * site.width / nx, (j + 0.5) * site.depth / ny
            if ok(x, y):
                out.append((round(x, 2), round(y, 2)))
    return list(dict.fromkeys(out))


# ---------------------------------------------------------------------------- choosing the spots
class _Cover:
    """Coverage arithmetic over the candidate signal matrix ``S[c, cell]`` (dBm) and the fixed floor ``base``."""

    def __init__(self, S: np.ndarray, base: np.ndarray, target: float):
        self.S, self.base, self.T = S, base, F32(target)
        self.ok = S >= self.T                                 # [c, cell]

    def best(self, chosen: list[int], skip: int | None = None) -> np.ndarray:
        b = self.base.copy()
        for k, c in enumerate(chosen):
            if k != skip:
                np.maximum(b, self.S[c], out=b)
        return b

    def score(self, signal: np.ndarray) -> tuple[int, float]:
        """(cells covered, a tie-break: how far short the rest is, and a little headroom on the covered floor,
        which pulls an access point towards the middle of the floor it serves rather than its edge)."""
        return int((signal >= self.T).sum()), float(np.clip(signal - self.T, -20.0, HEADROOM_DB).sum())

    def greedy(self, chosen: list[int], need: int, max_n: int) -> list[int]:
        cur = self.best(chosen)
        while len(chosen) < max_n:
            miss = cur < self.T
            if int((~miss).sum()) >= need:
                break
            gains = (self.ok & miss[None, :]).sum(axis=1)
            c = int(np.argmax(gains))
            if gains[c] <= 0:
                break
            chosen.append(c)
            np.maximum(cur, self.S[c], out=cur)
        return chosen

    def refine(self, chosen: list[int], fixed: int, sweeps: int = 4) -> tuple[list[int], tuple]:
        """Move each (not fixed) access point to the spot that serves the floor best with the others in place."""
        chosen = list(chosen)
        for _ in range(sweeps):
            moved = False
            for k in range(fixed, len(chosen)):
                others = self.best(chosen, skip=k)
                with_each = np.maximum(self.S, others[None, :])
                cov = (with_each >= self.T).sum(axis=1)
                top = cov.max()
                tied = np.flatnonzero(cov == top)
                short = np.clip(with_each[tied] - self.T, -20.0, HEADROOM_DB).sum(axis=1)
                c = int(tied[int(np.argmax(short))])
                if c != chosen[k] and (top, short.max()) > self.score(np.maximum(others, self.S[chosen[k]])):
                    chosen[k] = c
                    moved = True
            if not moved:
                break
        return chosen, self.score(self.best(chosen))


def _room_name(site: ws.Site, x: float, y: float) -> str:
    for room, lb in site.named_rooms():
        if lb is not None and room.contains(x, y):
            return lb.name
    return ""


def _names(site: ws.Site, spots: list, taken: set) -> list[str]:
    out = []
    for n, (x, y) in enumerate(spots, 1):
        room = _room_name(site, x, y)
        name = f"AP {room}" if room else f"AP{n}"
        k = 2
        base = name
        while name in taken:
            name = f"{base} {k}"
            k += 1
        taken.add(name)
        out.append(name)
    return out


# ---------------------------------------------------------------------------- channels
def _own_conflicts(site: ws.Site, band: str, walls) -> tuple[list, list]:
    own, theirs = [], []
    for c in wi.channel_conflicts(site, walls):
        if c.a.band != band:
            continue
        (theirs if c.b.neighbour else own).append(c)
    return own, theirs


def _channels(site: ws.Site, opts: PlanOptions, placed: set, notes: list) -> tuple[int, list, list]:
    """Channels for every radio; the widest width of the plan's band with no two of your radios sharing one."""
    walls = site.walls()
    band = opts.band
    widths = [w for w in (80, 40, 20) if w in wc.WIDTHS[band]] if band != "2.4" else [20]
    chosen = widths[-1]
    own: list = []
    theirs: list = []
    for w in widths:
        for i in placed:
            site.aps[i].set_radio(band, True, width=w, channel=0)
        wi.auto_channels(site, bands=[band])
        own, theirs = _own_conflicts(site, band, walls)
        chosen = w
        if not own:
            break
    if chosen != widths[0]:
        notes.append(f"{ws.BAND_TITLES[band]} at {chosen} MHz: wider channels would make access points share.")
    for i in placed:                                          # stay under what the channel allows
        ap = site.aps[i]
        r = ap.radio(band)
        if r is not None:
            ap.set_radio(band, True, tx_dbm=min(r.tx_dbm, wc.legal_eirp(band, r.channel, r.width)))
    if opts.dual_band and band != "2.4":
        _quiet_24(site, placed, walls, notes)
    return chosen, own, theirs


def _quiet_24(site: ws.Site, placed: set, walls, notes: list) -> None:
    """2.4 GHz has three channels that do not overlap: turn down, then switch off, the radios that would share."""
    off: list[str] = []
    lowered: set = set()
    for _round in range(60):
        wi.auto_channels(site, bands=["2.4"])
        own, _ = _own_conflicts(site, "2.4", walls)
        own = [c for c in own if c.a.index in placed or c.b.index in placed]
        if not own:
            break
        tally: dict[int, int] = {}
        for c in own:
            for e in (c.a, c.b):
                if e.index in placed:
                    tally[e.index] = tally.get(e.index, 0) + 1
        i = max(tally, key=lambda k: (tally[k], site.aps[k].radio("2.4").tx_dbm if site.aps[k].radio("2.4") else 0))
        ap = site.aps[i]
        r = ap.radio("2.4")
        if r is None:
            break
        if r.tx_dbm - 3.0 >= MIN_TX_24:
            ap.set_radio("2.4", True, tx_dbm=r.tx_dbm - 3.0)
            lowered.add(ap.name)
        else:
            ap.set_radio("2.4", False)
            off.append(ap.name)
    if off:
        notes.append("2.4 GHz switched off on " + (", ".join(off) if len(off) <= 4 else f"{len(off)} access points") + " (it would share one of its three channels).")
    turned = sorted(lowered - set(off))
    if turned:
        notes.append("2.4 GHz turned down on " + (", ".join(turned) if len(turned) <= 4 else f"{len(turned)} access points") + " so neighbouring cells do not share.")


# ---------------------------------------------------------------------------- the planner
def plan(site: ws.Site, opts: PlanOptions | None = None, progress=None) -> Plan:
    """Work out the access points for ``site`` (which is not changed): see the module's text.  ``progress``
    (optional) is called with (fraction 0..1, what is being done); returning False from it stops early."""
    opts = opts or PlanOptions()
    t0 = time.monotonic()
    band = opts.band if opts.band in ws.BANDS else "5"
    z = mount_height(site, opts)

    def tell(f: float, what: str) -> None:
        if progress is not None and progress(f, what) is False:
            raise InterruptedError("stopped")

    work = ws.Site.from_dict(site.to_dict())
    kept = [a for a in work.aps if a.neighbour or a.from_lab or opts.keep]
    work.aps = kept
    eng, mask = _plane_engine(work)
    cells = int(mask.sum())
    if cells == 0:
        raise ValueError("there is no indoor floor to cover")

    def field_of(x: float, y: float, zz: float, tx: float) -> np.ndarray:
        e = SimpleNamespace(x=x, y=y, z=zz, tx_dbm=tx, band=band)
        return eng.ap_field(e)[0][mask]

    base = np.full(cells, ws.NO_SIGNAL, F32)
    fixed_spots = []
    for e in work.emitters(band):
        np.maximum(base, field_of(e.x, e.y, e.z, e.tx_dbm), out=base)
        fixed_spots.append((e.x, e.y))
    tell(0.02, "finding where access points can go")
    spots = candidates(work, z)
    if not spots:
        raise ValueError("no place on the ceiling is clear of walls and furniture")
    S = np.empty((len(spots), cells), F32)
    for k, (x, y) in enumerate(spots):
        S[k] = field_of(x, y, z, opts.tx_dbm)
        if k % 8 == 0:
            tell(0.05 + 0.6 * k / len(spots), f"working out the signal from spot {k + 1} of {len(spots)}")
    cov = _Cover(S, base, opts.target_dbm)
    reach = np.maximum(S.max(axis=0), base)
    reachable = int((reach >= cov.T).sum())
    need = int(math.ceil(cells * opts.goal_pct / 100.0))
    if reachable < need:          # walls keep part of the floor from ever reaching it: the goal is of the rest
        need = int(math.ceil(reachable * opts.goal_pct / 100.0))
    tell(0.7, "choosing the fewest access points")
    chosen = cov.greedy([], need, opts.max_aps)
    chosen, sc = cov.refine(chosen, 0)
    # one fewer, as long as that still reaches the goal
    while len(chosen) > 1:
        tell(0.8, f"trying {len(chosen) - 1} access points")
        signal = cov.best(chosen)
        unique = []
        for k in range(len(chosen)):
            alone = cov.best(chosen, skip=k)
            unique.append(int(((signal >= cov.T) & (alone < cov.T)).sum()))
        done = False
        for k in sorted(range(len(chosen)), key=lambda k: unique[k])[:6 if len(chosen) <= 10 else 3]:
            trial, tsc = cov.refine(chosen[:k] + chosen[k + 1:], 0, sweeps=3)
            if tsc[0] >= need:
                chosen, sc, done = trial, tsc, True
                break
        if not done:
            break
    tell(0.9, "picking channels")
    placed_spots = [spots[c] for c in chosen]
    names = _names(work, placed_spots, {a.name for a in work.aps})
    for (x, y), name in zip(placed_spots, names):
        ap = ws.AccessPoint(name, x, y, z, opts.tx_dbm, band)
        if opts.dual_band and band != "2.4":
            ap.set_radio("2.4", True, tx_dbm=min(opts.tx_dbm, ws.LEGAL_EIRP_DBM["2.4"]))
        work.aps.append(ap)
    own_first = [a for a in work.aps if not a.neighbour]
    work.aps = own_first + [a for a in work.aps if a.neighbour]
    placed = {i for i, a in enumerate(work.aps) if a.name in set(names)}
    notes: list[str] = []
    width, own, theirs = _channels(work, opts, placed, notes)
    # the result on the floor, with the channels as planned
    final = np.full(cells, ws.NO_SIGNAL, F32)
    items = []
    for e in work.emitters(band, own=True, neighbours=True):
        v = eng.ap_field(e)[0]
        items.append((e, v[None]))
        if not e.neighbour:
            np.maximum(final, v[mask], out=final)
    m = wi.Metrics(items, (1, eng.grid.ny, eng.grid.nx))
    sinr = m.sinr[0][mask]
    if sc[0] < need and len(chosen) >= opts.max_aps:
        notes.append(f"Stopped at {opts.max_aps} access points: the goal needs more.")
    return Plan(aps=work.aps, added=names, count=len([a for a in work.aps if not a.neighbour and a.enabled]),
                covered_pct=100.0 * float((final >= cov.T).sum()) / cells,
                reachable_pct=100.0 * reachable / cells, target_dbm=opts.target_dbm, band=band, width=width,
                conflicts=own, neighbour_conflicts=theirs, sinr_ok_pct=100.0 * float((sinr >= 25.0).sum()) / cells,
                notes=notes, seconds=time.monotonic() - t0)
