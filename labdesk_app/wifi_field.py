"""The Wi-Fi field of a site as a 3D grid (numpy, no Qt).

``FieldEngine`` evaluates the radio model of ``labdesk_app.wifi_site`` (free-space loss at 1 m, a clutter
exponent, a loss per wall and per obstacle on the straight line) at every voxel of the room instead of at one
point at a time.  ``Site.rssi`` stays the reference: the engine gives the same numbers (the tests compare
them voxel by voxel), about a thousand times faster.

Three things make it fast enough to follow the mouse:

* **Separable geometry.**  Voxel centres are a product x * y * z and every wall is a vertical plane, so where
  a ray crosses a wall only needs 2D arrays.  The only 3D work per access point is one squared distance, one
  logarithm and one term per door and per obstacle.
* **One cached volume per access point.**  Moving an access point recomputes one volume; the rest is a
  running maximum.
* **A voxel budget.**  The grid is sized from a fixed number of voxels, not a fixed voxel edge, so a large
  hall costs the same as a small room (it is only coarser).

Array layout everywhere: ``[z, y, x]`` (C order, x fastest), float32, dBm.
"""
from __future__ import annotations

import math

import numpy as np

from . import wifi_site as ws

F32 = np.float32
VOXEL_BUDGET = 180_000            # voxels in the live grid (about 2 ms per access point on a desktop CPU)
MIN_CELL_M = 0.10                 # never finer than this


class Grid:
    """Voxel centres of an nx * ny * nz grid over a width * depth * height box."""

    def __init__(self, width: float, depth: float, height: float, nx: int, ny: int, nz: int):
        self.width, self.depth, self.height = float(width), float(depth), float(height)
        self.nx, self.ny, self.nz = nx, ny, nz
        # float64 copies drive the cheap 1D / 2D geometry (so the numbers agree with the pure-Python model);
        # the float32 copies drive the 3D arithmetic
        self.xd = (np.arange(nx) + 0.5) * (width / nx)
        self.yd = (np.arange(ny) + 0.5) * (depth / ny)
        self.zd = (np.arange(nz) + 0.5) * (height / nz)
        self.x, self.y, self.z = self.xd.astype(F32), self.yd.astype(F32), self.zd.astype(F32)
        self.shape = (nz, ny, nx)
        self.size = nz * ny * nx

    @classmethod
    def for_budget(cls, width: float, depth: float, height: float, budget: int = VOXEL_BUDGET) -> Grid:
        """Cubic voxels sized so that the whole box takes about ``budget`` of them (at least 4 layers)."""
        cell = max(MIN_CELL_M, (width * depth * height / max(1, budget)) ** (1.0 / 3.0))
        nz = max(4, round(height / cell))
        cell = max(cell, math.sqrt(width * depth * nz / max(1, budget)))
        return cls(width, depth, height, max(2, round(width / cell)), max(2, round(depth / cell)), nz)


def _slab(a: float, p: np.ndarray, lo: float, hi: float) -> tuple[np.ndarray, np.ndarray]:
    """Ray parameters (0 at the source ``a``, 1 at the receivers ``p``) between which the ray is inside
    lo..hi on this axis.  1D in, 1D out."""
    dp = p - a
    par = dp == 0
    with np.errstate(divide="ignore", invalid="ignore"):
        t1, t2 = (lo - a) / dp, (hi - a) / dp
    tin, tout = np.minimum(t1, t2), np.maximum(t1, t2)
    if par.any():
        inside = lo <= a <= hi
        tin[par] = -np.inf if inside else np.inf
        tout[par] = np.inf if inside else -np.inf
    return tin, tout


def indoor_mask(site: ws.Site, grid: Grid) -> np.ndarray | None:
    """``[y, x]`` True where the floor belongs to the customer: everything but the rooms labelled 'outside'
    (yard, street strip).  None when nothing is outside."""
    rects = site.outside_rects()
    if not rects:
        return None
    m = np.ones((grid.ny, grid.nx), bool)
    for x0, y0, x1, y1 in rects:
        m[np.ix_((grid.yd >= y0) & (grid.yd <= y1), (grid.xd >= x0) & (grid.xd <= x1))] = False
    return m


class FieldEngine:
    """The strongest-signal volume of a site, kept up to date cheaply.

    Call :meth:`volume` after any change to the site: it rebuilds what the change touched (everything when
    the room, a wall, a door, an obstacle or the clutter changed; one volume when an access point moved)."""

    def __init__(self, site: ws.Site, budget: int = VOXEL_BUDGET):
        self.site = site
        self.budget = budget
        self.grid = Grid.for_budget(site.width, site.depth, site.height, budget)
        self._geometry = None                        # signature of everything but the access points
        self._walls: list[ws.Wall] = []
        self._cache: dict[tuple, tuple] = {}         # (index of the access point, band) -> (key, dBm volume)
        self.band: str | None = None                 # the band volume() shows (None = the strongest of any)
        self._best: np.ndarray | None = None
        self._best_key = None
        self.computed = 0                            # volumes actually recomputed (for the cache test)

    # ------------------------------------------------------------------ one access point
    def _wall_terms(self, ax: float, ay: float, band: str) -> tuple[np.ndarray, list]:
        """2D part of the wall loss for a source at (ax, ay): ``wall2d[y, x]`` in dB assuming every opening is
        solid wall, and for each opening (door, window) what to give back where the ray crosses the wall between
        its sill and its head.  Openings of one wall never overlap (``Site.walls``), so adding every give-back
        equals the model's "first matching opening"."""
        g = self.grid
        k = ws.bi(band)
        wall2d = np.zeros((g.ny, g.nx), F32)
        holes = []
        for w in self._walls:
            loss = w.loss[k]
            if w.axis == "x":                         # plane x = a, runs along y
                da, db = ax - w.a, g.xd - w.a
                cross = (da * db) < 0
                t = da / np.where(cross, da - db, 1.0)                         # (nx,)
                run = ay + (g.yd[:, None] - ay) * t[None, :]                   # (ny, nx)
                hit = cross[None, :] & (run >= w.b0) & (run <= w.b1)
            else:                                     # plane y = a, runs along x
                da, db = ay - w.a, g.yd - w.a
                cross = (da * db) < 0
                t = da / np.where(cross, da - db, 1.0)                         # (ny,)
                run = ax + (g.xd[None, :] - ax) * t[:, None]
                hit = cross[:, None] & (run >= w.b0) & (run <= w.b1)
            wall2d += hit * F32(loss)
            for h in w.holes:
                saving = loss - h.loss[k]
                ind = hit & (run >= h.d0) & (run <= h.d1)
                if saving and ind.any():
                    holes.append((w.axis, t, ind * F32(saving), h.z0, h.z1))
        return wall2d, holes

    def _obstacle_loss(self, out: np.ndarray, src: tuple, d: np.ndarray, band: str) -> None:
        """Subtract the obstacles' loss from ``out`` (a dBm volume); ``d`` is the path-length volume.  Only the
        block of columns whose plan-view ray touches the box is visited."""
        g = self.grid
        k = ws.bi(band)
        for o in self.site.obstacles:
            per_ms, caps = o.losses()
            per_m, cap = per_ms[k], caps[k]
            if per_m <= 0 or cap <= 0:
                continue
            tix, tox = _slab(src[0], g.xd, o.x, o.x + o.w)
            tiy, toy = _slab(src[1], g.yd, o.y, o.y + o.d)
            ent = np.maximum(np.maximum(tix[None, :], tiy[:, None]), 0.0).astype(F32)
            ext = np.minimum(np.minimum(tox[None, :], toy[:, None]), 1.0).astype(F32)
            m = ext > ent
            if not m.any():
                continue
            rows, cols = np.flatnonzero(m.any(axis=1)), np.flatnonzero(m.any(axis=0))
            ys, xs = slice(rows[0], rows[-1] + 1), slice(cols[0], cols[-1] + 1)
            tiz, toz = (v.astype(F32) for v in _slab(src[2], g.zd, o.z0, o.z0 + o.h))
            seg = np.minimum(ext[None, ys, xs], toz[:, None, None])
            seg -= np.maximum(ent[None, ys, xs], tiz[:, None, None])
            np.maximum(seg, 0.0, out=seg)
            seg *= d[:, ys, xs]
            seg *= F32(per_m)
            np.minimum(seg, F32(cap), out=seg)
            out[:, ys, xs] -= seg

    def ap_field(self, ap) -> np.ndarray:
        """The dBm volume of one access point or radio (anything with x, y, z, tx_dbm, band)."""
        g = self.grid
        n = ws.CLUTTER[self.site.clutter][1]
        dx, dy, dz = g.x - F32(ap.x), g.y - F32(ap.y), g.z - F32(ap.z)
        d2 = ((dy * dy)[:, None] + (dx * dx)[None, :])[None, :, :] + (dz * dz)[:, None, None]
        np.maximum(d2, 0.01, out=d2)                       # the model clamps the distance at 0.1 m
        d = np.sqrt(d2) if self.site.obstacles else None
        out = np.log(d2, out=d2)                           # natural log: numpy has a SIMD float32 loop
        k = 5.0 / math.log(10.0)                           # 10 n log10(d) = n * k * ln(d^2)
        if n >= 2.0:                                       # exponent n beyond 1 m, 2 inside 1 m
            far = out * F32(k * n)
            out *= F32(k * 2.0)
            np.maximum(out, far, out=out)
        else:
            out *= np.where(out > 0, F32(k * n), F32(k * 2.0))
        ref = 20.0 * math.log10(ws.BAND_MHZ.get(ap.band, ws.BAND_MHZ["5"])) - 27.55
        np.subtract(F32(ap.tx_dbm - ref), out, out=out)
        wall2d, holes = self._wall_terms(ap.x, ap.y, ap.band)
        out -= wall2d[None, :, :]
        for axis, t, saving2d, z0, z1 in holes:            # give the opening's saving back between sill and head
            zc = ap.z + (g.zd[:, None] - ap.z) * t[None, :]                  # (nz, nx) or (nz, ny)
            inside = ((zc >= z0) & (zc <= z1)).astype(F32)
            if axis == "x":
                out += saving2d[None, :, :] * inside[:, None, :]
            else:
                out += saving2d[None, :, :] * inside[:, :, None]
        if d is not None:
            self._obstacle_loss(out, (ap.x, ap.y, ap.z), d, ap.band)
        return out

    # ------------------------------------------------------------------ cache + combine
    def _sync(self) -> None:
        s = self.site
        geometry = (s.width, s.depth, s.height, s.clutter, self.budget,
                    tuple(repr(p.to_dict()) for p in s.partitions),
                    tuple(repr(o.to_dict()) for o in s.obstacles))
        if geometry != self._geometry:
            self._geometry = geometry
            self.grid = Grid.for_budget(s.width, s.depth, s.height, self.budget)
            self._walls = s.walls()
            self._cache.clear()
            self._best_key = None
        ems = s.emitters(self.band)
        live = {(e.index, e.band): e for e in ems}
        for k in [k for k in self._cache if k[0] >= len(s.aps)]:
            del self._cache[k]
        vols = [self._vol(e) for e in ems]
        best_key = (self.band, tuple(sorted((k, self._cache[k][0]) for k in live)))
        if best_key != self._best_key or self._best is None:
            self._best_key = best_key
            if not vols:
                self._best = np.full(self.grid.shape, ws.NO_SIGNAL, F32)
            else:
                best = vols[0].copy()
                for v in vols[1:]:
                    np.maximum(best, v, out=best)
                self._best = best

    def _vol(self, e) -> np.ndarray:
        """The volume of one radio, from the cache when it has not moved."""
        k, key = (e.index, e.band), (e.x, e.y, e.z, e.tx_dbm, e.band)
        hit = self._cache.get(k)
        if hit is None or hit[0] != key:
            hit = self._cache[k] = (key, self.ap_field(e))
            self.computed += 1
        return hit[1]

    def band_volumes(self, band: str) -> list[tuple]:
        """[(Emitter, dBm volume)] of every radio on ``band``: yours and the neighbours' (for interference).
        The arrays are the engine's own: do not modify them."""
        self._sync()
        return [(e, self._vol(e)) for e in self.site.emitters(band, own=True, neighbours=True)]

    def at_slice(self, vol: np.ndarray, z: float | None = None) -> np.ndarray:
        """Any ``[z, y, x]`` volume on this grid cut at height ``z``, as ``slice`` does."""
        g = self.grid
        z = self.site.slice_z if z is None else z
        pos = ws.clamp(z / g.height * g.nz - 0.5, 0.0, g.nz - 1.0)
        i0 = min(int(pos), g.nz - 2) if g.nz > 1 else 0
        f = pos - i0
        if g.nz == 1 or f <= 0.0:
            return vol[i0]
        return vol[i0] * F32(1.0 - f) + vol[i0 + 1] * F32(f)

    def volume(self) -> np.ndarray:
        """Strongest signal at every voxel, ``[z, y, x]`` float32 dBm (``NO_SIGNAL`` when nothing transmits).
        The array is the engine's own: do not modify it."""
        self._sync()
        return self._best

    def slice(self, z: float | None = None) -> np.ndarray:
        """The horizontal plane at height ``z`` (default: the site's measuring height), ``[y, x]``,
        interpolated between the two voxel layers around it: the same values a 3D texture lookup gives."""
        return self.at_slice(self.volume(), z)


def band_shares(plane: np.ndarray, mask: np.ndarray | None = None) -> list[float]:
    """Share of a plane (0..100 %) in each signal band; the last entry is 'no service'.  The numpy twin of
    ``wifi_site.coverage_shares``.  ``mask`` (same shape, True = count it) leaves cells out, e.g. the yard."""
    floors = np.array([b[0] for b in ws.SIGNAL_BANDS], F32)
    # floors are descending: count how many floors each value reaches
    reached = (plane[..., None] >= floors).sum(axis=-1)            # 0 = no service ... len = top band
    idx = len(floors) - reached                                    # 0 = top band ... len = no service
    if mask is not None and mask.shape == idx.shape and mask.any():
        idx = idx[mask]
    counts = np.bincount(idx.ravel(), minlength=len(floors) + 1)
    total = max(1, int(counts.sum()))
    return [100.0 * int(c) / total for c in counts]


def contour_segments(plane: np.ndarray, level: float) -> list[tuple]:
    """Marching squares on a ``[y, x]`` plane: line segments ``((x0, y0), (x1, y1))`` in cell-centre grid
    coordinates (cell (j, i) has its centre at x = i, y = j) where the plane crosses ``level``."""
    a = plane >= level
    ny, nx = plane.shape
    if ny < 2 or nx < 2:
        return []
    case = (a[:-1, :-1] * 1 + a[:-1, 1:] * 2 + a[1:, 1:] * 4 + a[1:, :-1] * 8).astype(np.uint8)
    out = []
    for j, i in zip(*np.nonzero((case != 0) & (case != 15))):
        j, i = int(j), int(i)
        v0, v1, v2, v3 = (float(plane[j, i]), float(plane[j, i + 1]), float(plane[j + 1, i + 1]),
                          float(plane[j + 1, i]))

        def cut(va: float, vb: float) -> float:
            return 0.5 if vb == va else ws.clamp((level - va) / (vb - va), 0.0, 1.0)
        edges = {"b": (i + cut(v0, v1), j), "r": (i + 1, j + cut(v1, v2)),
                 "t": (i + cut(v3, v2), j + 1), "l": (i, j + cut(v0, v3))}
        pairs = {1: ["lb"], 2: ["br"], 3: ["lr"], 4: ["rt"], 5: ["lt", "br"], 6: ["bt"], 7: ["lt"], 8: ["lt"],
                 9: ["bt"], 10: ["lb", "rt"], 11: ["rt"], 12: ["lr"], 13: ["br"], 14: ["lb"]}[int(case[j, i])]
        for pair in pairs:
            out.append((edges[pair[0]], edges[pair[1]]))
    return out
