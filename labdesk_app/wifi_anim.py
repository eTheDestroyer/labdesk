"""Animations of the Wi-Fi in the 3D site view, as pure functions of time (no Qt, no state to drift).

* **Data packets** between an access point and a phone.  Packet ``i`` leaves at ``i / rate`` seconds and moves
  at ``PACKET_SPEED``, so where every packet is follows from the clock alone.  The pace follows the expected
  speed of the link (``wifi_link``); fewer packets come back when the phone's weaker uplink limits the link.
  At every wall, door or obstacle a packet survives with probability ``10 ** (-loss / SURVIVAL_DB)`` (a 12 dB
  block wall lets about half through, a mamad wall about one in ten) and its signal drops by the loss, so the
  stream visibly thins and reddens behind walls.  Where the signal falls below what a phone can decode, the
  packet fades out.  Looks only: real Wi-Fi moves at the speed of light and is not made of dots.
* **Walking phone.**  A path of floor points; the phone walks it at ``speed`` m/s.  ``walk_samples`` reads the
  model every ``step`` metres along the path (signal, serving access point), which the view draws as a trail
  coloured by signal, like a survey walk.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from . import wifi_link as wl
from . import wifi_site as ws

PACKET_SPEED = 7.0              # metres per second (slowed down: real waves are instant)
SURVIVAL_DB = 40.0              # a packet survives a loss of L dB with probability 10 ** (-L / SURVIVAL_DB)
MIN_RATE, MAX_RATE = 1.5, 14.0  # packets per second downstream, from no speed to the fastest link
DECODE_DBM = -82.0              # below this nothing is decoded (802.11 MCS0 at 20 MHz)
WALK_SPEED = 1.4                # metres per second: walking pace
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

    def __init__(self, site: ws.Site, ap: ws.AccessPoint, phone: tuple, walls=None):
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
        self.link = wl.link(site, down, ap)
        rate_down = self.link.rate_hi if self.link else 0.0
        top = wl.RATE_2SS[wl.WIDTH_MHZ.get(ap.band, 20)][-1]
        self.rate = MIN_RATE + (MAX_RATE - MIN_RATE) * min(1.0, rate_down / top) ** 0.6
        up_rate = wl.phy_rate(self.link.up_dbm + self.link.spread_db, ap.band) if self.link else 0.0
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


@dataclass(frozen=True)
class WalkSample:
    dist: float                 # metres along the path
    x: float
    y: float
    dbm: float
    ap: str                     # the serving access point's name ('' = none)


def walk_samples(site: ws.Site, points, z: float, step: float = 0.25) -> list[WalkSample]:
    """The model read every ``step`` metres along the path, at height ``z`` (the measuring height)."""
    total = path_length(points)
    if not points or total <= 0.0:
        return []
    walls = site.walls()
    n = max(1, int(math.ceil(total / step)))
    out = []
    for k in range(n + 1):
        d = min(k * step, total)
        x, y = walk_position(points, d)
        v, ap = site.best((x, y, z), walls)
        out.append(WalkSample(d, x, y, v, ap.name if ap is not None else ""))
    return out


def roams(samples: list[WalkSample]) -> list[WalkSample]:
    """The samples where the serving access point changes (the phone roams)."""
    return [b for a, b in zip(samples, samples[1:]) if a.ap and b.ap and a.ap != b.ap]
