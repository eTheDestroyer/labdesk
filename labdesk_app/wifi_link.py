"""What a signal level means for a phone: signal-to-noise, expected speed and the link back (pure Python).

The Radio realism council's link layer, kept deliberately simple and stated as assumptions:

* **Client.** A phone with two antennas (2x2), transmitting 15 dBm EIRP (14 dBm at 6 GHz).  The access point
  hears the phone over the same path, so the phone's signal at the access point is ``client EIRP - path loss``
  with ``path loss = AP EIRP - RSSI``.  Where the phone's signal is the weaker one, *the uplink limits* the
  link: the phone sees bars but its uploads (and its TCP acknowledgements) crawl.
* **Noise.** -95 dBm in 20 MHz, +3 dB for every doubling of the channel width (thermal noise plus a typical
  receiver noise figure).  SNR = signal - noise.
* **Speed.** 802.11ax (Wi-Fi 6) receiver minimum sensitivities (IEEE 802.11-2021 table 27-51) pick the highest
  rate the weaker direction can sustain; rates are 2 spatial streams, 0.8 us guard interval.  Because the
  model's own spread is about +-5 dB (ITU-R P.1238), the speed is given as a range: the rate at the signal
  minus the spread and at the signal plus the spread.  Real throughput is about 60-70 % of the PHY rate.
* **Channel widths** as installed by default: 20 MHz at 2.4 GHz, 80 MHz at 5 and 6 GHz.

No interference from other networks or other access points on the same channel is counted.
"""
from __future__ import annotations

from dataclasses import dataclass

from . import wifi_site as ws

CLIENT_EIRP_DBM = {"2.4": 15.0, "5": 15.0, "6": 14.0}
NOISE_20MHZ_DBM = -95.0
WIDTH_MHZ = {"2.4": 20, "5": 80, "6": 80}
# 802.11ax minimum sensitivity at 20 MHz (dBm), MCS 0..11
SENSITIVITY_20 = (-82, -79, -77, -74, -70, -66, -65, -64, -59, -57, -54, -52)
# PHY rate (Mbit/s) for 2 spatial streams, 0.8 us GI, MCS 0..11, by channel width
RATE_2SS = {
    20: (17.2, 34.4, 51.6, 68.8, 103.2, 137.6, 154.9, 172.1, 206.5, 229.4, 258.1, 286.8),
    40: (34.4, 68.8, 103.2, 137.6, 206.5, 275.3, 309.7, 344.1, 412.9, 458.8, 516.2, 573.5),
    80: (72.1, 144.1, 216.2, 288.2, 432.4, 576.5, 648.5, 720.6, 864.7, 960.8, 1080.9, 1201.0),
}


def _doublings(width: int) -> int:
    return {20: 0, 40: 1, 80: 2, 160: 3}.get(width, 0)


def noise_floor(band: str) -> float:
    return NOISE_20MHZ_DBM + 3.0 * _doublings(WIDTH_MHZ.get(band, 20))


def phy_rate(rssi: float, band: str) -> float:
    """Highest 2x2 PHY rate (Mbit/s) a receiver gets at ``rssi`` dBm; 0 below the lowest rate."""
    width = WIDTH_MHZ.get(band, 20)
    shift = 3.0 * _doublings(width)
    rate = 0.0
    for mcs, sens in enumerate(SENSITIVITY_20):
        if rssi >= sens + shift:
            rate = RATE_2SS[width][mcs]
    return rate


@dataclass(frozen=True)
class Link:
    down_dbm: float              # the access point at the phone
    up_dbm: float                # the phone at the access point
    snr_db: float                # of the weaker direction
    uplink_limits: bool          # the phone is heard worse than it hears
    rate_lo: float               # Mbit/s, at the weaker signal minus the model's spread
    rate_hi: float               # ... plus the spread
    spread_db: float
    band: str

    def speed_text(self) -> str:
        if self.rate_hi <= 0.0:
            if self.uplink_limits and phy_rate(self.down_dbm + self.spread_db, self.band) > 0.0:
                return "no link: the phone hears the AP, the AP cannot hear the phone"
            return "no connection"
        lo, hi = round(self.rate_lo), round(self.rate_hi)
        return f"up to {hi} Mbit/s" if lo == hi else (f"0-{hi} Mbit/s" if lo == 0 else f"{lo}-{hi} Mbit/s")


def link(site: ws.Site, rssi: float, ap: ws.AccessPoint | None) -> Link | None:
    """The link between ``ap`` and a phone where ``ap``'s signal is ``rssi`` dBm (None without an AP)."""
    if ap is None:
        return None
    path_loss = ap.tx_dbm - rssi
    up = CLIENT_EIRP_DBM.get(ap.band, 15.0) - path_loss
    weaker = min(rssi, up)
    spread = ws.UNCERTAINTY_DB.get(site.clutter, 5.0)
    return Link(rssi, up, weaker - noise_floor(ap.band), up < rssi, phy_rate(weaker - spread, ap.band),
                phy_rate(weaker + spread, ap.band), spread, ap.band)
