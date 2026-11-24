"""Animations of the Wi-Fi in the 3D site view, as pure functions of time (no Qt, no state to drift).

* **Data packets** between an access point and a phone.  Packet ``i`` leaves at ``i / rate`` seconds and moves
  at ``PACKET_SPEED``, so where every packet is follows from the clock alone.  The pace follows the expected
  speed of the link (``wifi_link``); fewer packets come back when the phone's weaker uplink limits the link.
  At every wall, door or obstacle a packet survives with probability ``10 ** (-loss / SURVIVAL_DB)`` (a 12 dB
  block wall lets about half through, a mamad wall about one in ten) and its signal drops by the loss, so the
  stream visibly thins and reddens behind walls.  Where the signal falls below what a phone can decode, the
  packet fades out.  Looks only: real Wi-Fi moves at the speed of light and is not made of dots.
* **Walking phone.**  A path of floor points (clicked, or drawn freehand and thinned by ``simplify``); the phone
  walks it at ``speed`` m/s.  ``walk_samples`` reads the model every ``step`` metres along the path as the phone
  would (the radio it connects to, its band and channel, signal and SINR), which the view draws as a trail
  coloured by signal, like a survey walk.  ``heading`` is where the walker looks (for the phone's-eye view).
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from . import wifi_interference as wi
from . import wifi_link as wl
from . import wifi_site as ws

PACKET_SPEED = 7.0              # metres per second (slowed down: real waves are instant)
SURVIVAL_DB = 40.0              # a packet survives a loss of L dB with probability 10 ** (-L / SURVIVAL_DB)
MIN_RATE, MAX_RATE = 1.5, 14.0  # packets per second downstream, from no speed to the fastest link
DECODE_DBM = -82.0              # below this nothing is decoded (802.11 MCS0 at 20 MHz)
WALK_SPEED = 1.4                # metres per second: walking pace
PHONE_FLOOR_DBM = -90.0         # a phone does not stay connected to a weaker signal than this
BEACON_PERIOD = 1.25            # seconds between two beacon flashes of an access point (slowed down)


def _hash01(i: int, salt: int) -> float:
    """A fixed pseudo-random number in [0, 1) for packet ``i`` (so a packet's fate never flickers)."""
    x = (i * 0x9E3779B1 + salt * 0x85EBCA77) & 0xFFFFFFFF
    x ^= x >> 15
    x = (x * 0x2C1B3C6D) & 0xFFFFFFFF
    x ^= x >> 12
    x = (x * 0x297A2D39) & 0xFFFFFFFF
    x ^= x >> 15
    return x / 4294967296.0


@dataclass(frozen=True)
class Packet:
    x: float
    y: float
    z: float
    dbm: float                  # the signal it carries here (for its colour)
    alpha: float                # 0..1
    up: bool                    # from the phone to the access point


class Flow:
    """The packets between one access point and one phone position (both fixed while it lives)."""

    def __init__(self, site: ws.Site, ap, phone: tuple, walls=None, interference_dbm: float | None = None):
        self.ap, self.phone = ap, tuple(phone)
        a = (ap.x, ap.y, ap.z)
        self.dist = max(math.dist(a, self.phone), 0.05)
        # crossings as (fraction along AP -> phone, loss)
        self.cross = []
        for pt, loss, _what in site.crossings(a, self.phone, ap.band, walls):
            f = math.dist(a, pt) / self.dist
            self.cross.append((f, loss))
        self.site = site
        down = ap.tx_dbm - site.path_loss(self.dist, ap.band) - sum(c[1] for c in self.cross)
        self.link = wl.link(site, down, ap, interference_dbm)
        rate_down = self.link.rate_hi if self.link else 0.0
        top = wl.RATE_2SS[self.link.width][-1]
        self.rate = MIN_RATE + (MAX_RATE - MIN_RATE) * min(1.0, rate_down / top) ** 0.6
        up_rate = wl.phy_rate(self.link.up_dbm + self.link.spread_db - self.link.interference_db, ap.band,
                              self.link.width) if self.link else 0.0
        self.rate_up = self.rate * (min(1.0, up_rate / rate_down) if rate_down > 0 else 0.0) * 0.5
        self.client_eirp = wl.CLIENT_EIRP_DBM.get(ap.band, 15.0)

    def _signal(self, f: float, up: bool) -> float:
        """Signal of a packet at fraction ``f`` of the way from where it left."""
        d = max(f * self.dist, 0.1)
        if not up:
            lost = sum(loss for c, loss in self.cross if c <= f)
            return self.ap.tx_dbm - self.site.path_loss(d, self.ap.band) - lost
        lost = sum(loss for c, loss in self.cross if c >= 1.0 - f)
        return self.client_eirp - self.site.path_loss(d, self.ap.band) - lost

    def _one(self, i: int, born: float, now: float, up: bool) -> Packet | None:
        f = (now - born) * PACKET_SPEED / self.dist
        if not 0.0 <= f <= 1.0:
            return None
        walls = [c for c in self.cross if (c[0] >= 1.0 - f if up else c[0] <= f)]
        for k, (_c, loss) in enumerate(walls):                    # its fate at each wall is fixed by its number
            if _hash01(i, k * 2 + (1 if up else 0)) > 10.0 ** (-loss / SURVIVAL_DB):
                return None
        dbm = self._signal(f, up)
        fade = min(1.0, max(0.0, (dbm - (DECODE_DBM - 6.0)) / 6.0))          # gone below what a phone decodes
        fade *= min(1.0, f * self.dist / 0.4, (1.0 - f) * self.dist / 0.4 + 0.25)   # in and out softly
        if fade <= 0.02:
            return None
        src, dst = ((self.ap.x, self.ap.y, self.ap.z), self.phone) if not up else (self.phone,
                                                                                   (self.ap.x, self.ap.y, self.ap.z))
        x, y, z = (src[k] + (dst[k] - src[k]) * f for k in range(3))
        return Packet(x, y, z, dbm, fade, up)

    def packets(self, now: float) -> list[Packet]:
        """Every packet in flight at time ``now`` (seconds)."""
        out = []
        travel = self.dist / PACKET_SPEED
        for rate, up in ((self.rate, False), (self.rate_up, True)):
            if rate <= 0.0:
                continue
            first, last = math.ceil((now - travel) * rate), math.floor(now * rate)
            for i in range(max(first, last - 400), last + 1):
                pk = self._one(i, i / rate, now, up)
                if pk is not None:
                    out.append(pk)
        return out


def beacon(now: float, period: float = BEACON_PERIOD) -> float:
    """0..1: how bright an access point's beacon flash is at ``now`` (1 just as it fires, then fading)."""
    if not math.isfinite(now):
        return 0.0
    age = now % period
    return math.exp(-age / (0.18 * period))


# ---------------------------------------------------------------------------- walking phone
def path_length(points) -> float:
    return sum(math.dist(a, b) for a, b in zip(points, points[1:]))


def walk_position(points, dist: float) -> tuple[float, float] | None:
    """The point ``dist`` metres along the path (clamped to its ends); None for an empty path."""
    if not points:
        return None
    if len(points) == 1 or dist <= 0.0:
        return tuple(points[0])
    left = dist
    for a, b in zip(points, points[1:]):
        seg = math.dist(a, b)
        if left <= seg and seg > 0.0:
            k = left / seg
            return (a[0] + (b[0] - a[0]) * k, a[1] + (b[1] - a[1]) * k)
        left -= seg
    return tuple(points[-1])


def heading(points, dist: float, ahead: float = 1.2) -> tuple[float, float] | None:
    """Unit floor direction the walker faces ``dist`` metres along the path: towards the point ``ahead`` metres
    further on (so a hand-drawn wobble or a sharp corner turns the view smoothly); None when it cannot tell."""
    total = path_length(points)
    if len(points) < 2 or total <= 0.0:
        return None
    d = min(max(dist, 0.0), total)
    a, b = d, min(total, d + ahead)
    if b - a < ahead * 0.5:                    # at the end: look the way the last stretch went
        a = max(0.0, b - ahead)
    pa, pb = walk_position(points, a), walk_position(points, b)
    dx, dy = pb[0] - pa[0], pb[1] - pa[1]
    n = math.hypot(dx, dy)
    return (dx / n, dy / n) if n > 1e-6 else None


def simplify(points, tol: float = 0.04) -> list[tuple[float, float]]:
    """A hand-drawn line thinned to the points that matter (Ramer-Douglas-Peucker, ``tol`` metres off at most)."""
    pts = [tuple(map(float, p)) for p in points]
    out = [pts[0]] if pts else []
    for q in pts[1:]:                          # drop repeats first
        if math.dist(q, out[-1]) > 1e-6:
            out.append(q)
    if len(out) < 3:
        return out
    keep = [False] * len(out)
    keep[0] = keep[-1] = True
    stack = [(0, len(out) - 1)]
    while stack:
        i, j = stack.pop()
        (ax, ay), (bx, by) = out[i], out[j]
        L = math.hypot(bx - ax, by - ay)
        far, k = -1.0, -1
        for m in range(i + 1, j):
            px, py = out[m]
            if L < 1e-9:
                dd = math.hypot(px - ax, py - ay)
            else:
                dd = abs((bx - ax) * (ay - py) - (ax - px) * (by - ay)) / L
            if dd > far:
                far, k = dd, m
        if k >= 0 and far > tol:
            keep[k] = True
            stack += [(i, k), (k, j)]
    return [p for p, kp in zip(out, keep) if kp]


@dataclass(frozen=True)
class WalkSample:
    dist: float                 # metres along the path
    x: float
    y: float
    dbm: float                  # signal of the radio the phone is connected to
    ap: str                     # the serving access point's name ('' = none)
    band: str = ""              # its band, channel and width
    channel: int = 0
    width: int = 0
    sinr: float = ws.NO_SIGNAL  # dB


def phone_reading(site: ws.Site, pt: tuple, band: str | None = None, walls=None):
    """What a phone at ``pt`` connects to (a ``wifi_interference.Reading``): on ``band``, its strongest radio; with
    ``band`` None the band that gives it the fastest link; nothing (``serving`` None) below ``PHONE_FLOOR_DBM``, as band steering does (a strong 2.4 GHz signal next to
    the router still loses to a good 5 GHz one, which is wider)."""
    walls = site.walls() if walls is None else walls
    best, fast = wi.Reading(band), -1.0
    for b in ((band,) if band is not None else ws.BANDS):
        rd = wi.reading(site, pt, b, walls)
        if rd.serving is None or rd.signal < PHONE_FLOOR_DBM:
            continue
        lk = wl.link(site, rd.signal, rd.serving, rd.interference)
        speed = lk.rate_hi if lk is not None else 0.0
        if speed > fast or (speed == fast and rd.sinr > best.sinr):
            best, fast = rd, speed
    return best


def heard(site: ws.Site, pt: tuple, walls=None) -> list:
    """[(Emitter, dBm)] every radio on every band, yours and the neighbours', strongest first (a phone's list)."""
    walls = site.walls() if walls is None else walls
    out = [(e, site.rssi(e, pt, walls)) for e in site.emitters(None, own=True, neighbours=True)]
    return sorted(out, key=lambda t: -t[1])


def walk_samples(site: ws.Site, points, z: float, step: float = 0.25, band: str | None = None) -> list[WalkSample]:
    """The model read every ``step`` metres along the path, at height ``z`` (the measuring height), the way a
    phone connects (``phone_reading``)."""
    total = path_length(points)
    if not points or total <= 0.0:
        return []
    walls = site.walls()
    n = max(1, int(math.ceil(total / step)))
    out = []
    for k in range(n + 1):
        d = min(k * step, total)
        x, y = walk_position(points, d)
        rd = phone_reading(site, (x, y, z), band, walls)
        e = rd.serving
        if e is None:
            out.append(WalkSample(d, x, y, ws.NO_SIGNAL, ""))
        else:
            out.append(WalkSample(d, x, y, rd.signal, e.name, e.band, e.channel, e.width, rd.sinr))
    return out


def roams(samples: list[WalkSample]) -> list[WalkSample]:
    """The samples where the phone moves to another access point or band (it roams)."""
    return [b for a, b in zip(samples, samples[1:]) if a.ap and b.ap and (a.ap, a.band) != (b.ap, b.band)]


def roam_text(samples: list[WalkSample], at: WalkSample) -> str:
    """'Roamed to AP2 · 5 GHz', or 'Switched to 5 GHz' when the phone only changed band on the same access point."""
    i = samples.index(at) if at in samples else 0
    prev = samples[i - 1] if i > 0 else None
    band = ws.BAND_TITLES.get(at.band, "")
    if prev is not None and prev.ap == at.ap:
        return f"Switched to {band}"
    return f"Roamed to {at.ap}" + (f" · {band}" if band else "")
