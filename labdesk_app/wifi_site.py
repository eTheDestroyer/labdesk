"""3D site model and Wi-Fi propagation (pure Python, no Qt).

A *site* is one box-shaped space (a customer's flat, office or workshop) divided by partition walls.  Each
partition stands at a percentage of the box along one axis, may span only part of the other axis, and holds
any number of openings: doors and windows.  Furniture, machines and racking are boxes.  Access points are
placed anywhere inside the box.

Coordinates are metres: origin at one floor corner, ``x`` along the width, ``y`` along the depth, ``z`` up.

The radio model is the indoor multi-wall model (ITU-R P.1238 style): free-space loss at 1 m, a log-distance
term with a clutter exponent, plus the loss of every wall, door or window the straight line from the access
point to the receiver crosses, plus the loss of the furniture it passes through (capped per item, because a
real signal also bends round it).  Three bands: 2.4, 5 and 6 GHz.

Where the numbers come from: the losses of the Israeli materials, doors, windows and contents are the
Materials council's table (``wifi-council/round3/materials``: ITU-R P.2040 slab model, NIST, NYU and AAU
measurements, ITU-R P.526 for objects), each marked *measured*, *derived* or *estimate*.  The six materials of
version 1 keep their old values, so an old file gives the same wall losses.  It is a planning estimate
(about +-5 dB in an office, +-9 dB in a metal hall, ITU-R P.1238), not a site survey.

File format: a site that uses nothing new saves to version 1's JSON.  Otherwise the file says
``"format": "labdesk-room", "version": 2`` and may carry ``meta`` (title, Hebrew title, kind, city, source
plan, assumptions) and room ``labels``.  Keys this module does not know are kept and written back.
"""
from __future__ import annotations

import math
from dataclasses import InitVar, dataclass, field

FORMAT = "labdesk-room"
VERSION = 2
FORMATS_READ = ("labdesk-room", "labdesk-site")

BANDS = ("2.4", "5", "6")
BAND_MHZ = {"2.4": 2437.0, "5": 5500.0, "6": 6185.0}       # channel 6, channel 100, mid 6 GHz (5945-6425 in Israel)
BAND_TITLES = {"2.4": "2.4 GHz", "5": "5 GHz", "6": "6 GHz"}
# Israel, Ministry of Communications (wireless-regdb IL): most EIRP allowed.  5 GHz: 23 dBm on channels 36-48,
# 20 / 27 dBm with DFS on 52-64 / 100-140; the figure here is the band's best case.  6 GHz: indoor only.
LEGAL_EIRP_DBM = {"2.4": 20.0, "5": 27.0, "6": 23.0}
LEGAL_NOTE = {"2.4": "Israel allows 20 dBm EIRP at 2.4 GHz",
              "5": "Israel allows 23 dBm on channels 36-48 and 27 dBm on 100-140 (with DFS)",
              "6": "Israel allows 23 dBm EIRP at 6 GHz (5945-6425 MHz), indoors only"}


def bi(band: str) -> int:
    """Index of a band in BANDS (and in every loss tuple); 5 GHz for anything unknown."""
    return BANDS.index(band) if band in BANDS else 1


@dataclass(frozen=True)
class Material:
    title: str
    title_he: str
    loss: tuple                  # dB at 2.4, 5, 6 GHz, straight through
    thickness_m: float = 0.12    # for drawing only
    confidence: str = "estimate"


# Walls.  The first six are version 1's (their 2.4 / 5 GHz values unchanged; 6 GHz = 5 GHz + 10 %).
MATERIALS: dict[str, Material] = {
    "drywall": Material("Drywall", "גבס", (3.0, 4.0, 4.4)),
    "glass": Material("Glass", "זכוכית", (2.0, 3.0, 3.3)),
    "wood": Material("Wood", "עץ", (4.0, 5.0, 5.5)),
    "brick": Material("Brick", "לבנים", (8.0, 12.0, 13.2)),
    "concrete": Material("Concrete", "בטון", (12.0, 18.0, 19.8)),
    "metal": Material("Metal sheet", "פח", (20.0, 26.0, 28.6)),
    "gypsum_single": Material("Gypsum partition, single", "קיר גבס יחיד", (3.1, 3.0, 2.0), 0.10, "derived"),
    "gypsum_double": Material("Gypsum partition, double", "קיר גבס כפול", (3.3, 4.8, 4.5), 0.125, "derived"),
    "block_10": Material("Hollow block 10 cm, plastered", "בלוק 10 מטויח", (9.0, 12.0, 13.0), 0.13, "derived"),
    "block_20": Material("Hollow block 20 cm, plastered", "בלוק 20 מטויח", (12.0, 16.0, 17.0), 0.23, "measured"),
    "silicate_10": Material("Silicate block 10 cm", "בלוק סיליקט 10", (5.4, 8.6, 9.6), 0.12),
    "silicate_20": Material("Silicate block 20 cm", "בלוק סיליקט 20", (9.8, 16.1, 17.8), 0.22),
    "ytong_10": Material("Ytong (AAC) 10 cm", "איטונג 10", (1.9, 3.3, 3.9), 0.11),
    "ytong_20": Material("Ytong (AAC) 20 cm", "איטונג 20", (3.0, 5.7, 6.3), 0.21),
    "brick_bauhaus": Material("Plastered brick 25 cm (1930s)", "לבנים מטויחות 25 (באוהאוס)", (9.9, 13.0, 13.3),
                              0.29, "derived"),
    "concrete_15": Material("Reinforced concrete 15 cm", "בטון מזוין 15", (14.4, 24.7, 27.1), 0.18, "derived"),
    "concrete_20": Material("Reinforced concrete 20 cm", "בטון מזוין 20", (17.7, 30.9, 34.0), 0.23, "derived"),
    "concrete_25": Material("Reinforced concrete 25 cm", "בטון מזוין 25", (21.0, 37.1, 40.8), 0.28, "derived"),
    "mamad_20": Material("Mamad wall, inner (RC 20 cm)", "קיר ממ\"ד פנימי 20", (18.7, 31.9, 35.0), 0.23, "derived"),
    "mamad_25": Material("Mamad wall, outer (RC 25 cm)", "קיר ממ\"ד חיצוני 25", (22.0, 38.1, 41.8), 0.28, "derived"),
    "mamad_30": Material("Mamad / mamak wall 30 cm", "קיר ממ\"ד / ממ\"ק 30", (25.3, 44.3, 48.6), 0.33, "derived"),
    "shelter_40": Material("Shelter wall, RC 40 cm", "קיר מקלט 40", (31.9, 56.6, 62.3), 0.43, "derived"),
    "stone_block": Material("Jerusalem stone on block 20", "אבן ירושלמית על בלוק", (14.2, 20.3, 22.3), 0.31, "derived"),
    "stone_rc": Material("Jerusalem stone on concrete 20", "אבן ירושלמית על בטון", (19.8, 35.1, 38.7), 0.31, "derived"),
    "sandwich_panel": Material("Sandwich panel (steel skins)", "פאנל מבודד", (30.0, 35.0, 35.0), 0.10),
    "corrugated_steel": Material("Corrugated steel sheet", "פח גלי", (25.0, 30.0, 30.0), 0.02),
    "glass_double": Material("Double glazing (uncoated)", "זיגוג כפול", (2.0, 4.8, 5.1), 0.05, "derived"),
    "glass_low_e": Material("Low-E coated glazing", "זיגוג Low-E", (25.0, 28.0, 30.0), 0.05, "measured"),
    "curtain_wall": Material("Curtain wall, coated glass", "קיר מסך", (30.0, 32.0, 34.0), 0.08, "measured"),
}
V1_MATERIALS = ("drywall", "glass", "wood", "brick", "concrete", "metal")


@dataclass(frozen=True)
class Fill:
    title: str
    title_he: str
    loss: tuple                  # dB at 2.4, 5, 6 GHz through the closed opening
    where: str = "door"          # "door", "window" or "both": where the panel offers it
    confidence: str = "estimate"


FILLS: dict[str, Fill] = {
    "wood_door": Fill("Wooden door", "דלת עץ", (3.0, 4.0, 4.4)),               # version 1's closed door
    "none": Fill("No door (passage)", "מעבר פתוח", (0.0, 0.0, 0.0)),
    "door_hollow": Fill("Hollow-core door", "דלת חלולה", (1.7, 2.7, 2.9), "door", "derived"),
    "door_solid": Fill("Solid wooden door", "דלת עץ מלאה", (3.5, 4.5, 5.8), "door", "measured"),
    "door_steel": Fill("Steel entry door (pladelet)", "פלדלת", (35.0, 38.0, 40.0)),
    "blast_door": Fill("Mamad blast door", "דלת הדף ממ\"ד", (43.0, 43.0, 43.0), "door", "derived"),
    "glass": Fill("Plain glass", "זכוכית", (1.9, 3.3, 3.0), "both", "measured"),
    "double_glazing": Fill("Double glazing", "זיגוג כפול", (2.0, 4.8, 5.1), "both", "derived"),
    "low_e": Fill("Low-E double glazing", "זיגוג Low-E", (25.0, 28.0, 30.0), "both", "measured"),
    "curtain_wall": Fill("Curtain wall glass", "קיר מסך", (30.0, 32.0, 34.0), "window", "measured"),
    "blast_window": Fill("Mamad steel window, closed", "חלון הדף סגור", (38.0, 40.0, 40.0), "window"),
    "mamad_glazing": Fill("Mamad window, steel leaf open", "חלון ממ\"ד, כנף פלדה פתוחה", (4.3, 2.6, 4.0), "window",
                          "derived"),
}
# roller shutter (trisim) in front of an opening: added to the opening's loss
SHUTTERS: dict[str, Fill] = {
    "none": Fill("No shutter / rolled up", "ללא תריס", (0.0, 0.0, 0.0), "both"),
    "slits": Fill("Trisim down, slits open", "תריס מוריד, חרכים פתוחים", (8.0, 10.0, 10.0), "both"),
    "closed": Fill("Trisim closed", "תריס סגור", (20.0, 25.0, 25.0), "both"),
}
DOOR_LOSS = {b: FILLS["wood_door"].loss[i] for i, b in enumerate(BANDS)}     # version 1's closed door
OPENING_KINDS = ("door", "window")
OPENING_GAP = 1e-3                               # openings of one wall keep at least this much apart (metres)


@dataclass(frozen=True)
class Kind:
    """What stands in the room.  The radio takes ``per_m`` dB for each metre of path inside the box, at most
    ``cap`` dB for one item (the signal bends round it).  ``size`` = typical (w, d, h, z0) in metres."""
    title: str
    title_he: str
    per_m: tuple                 # dB per metre at 2.4, 5, 6 GHz
    cap: tuple                   # most dB one item takes away at 2.4, 5, 6 GHz
    size: tuple = (1.0, 1.0, 1.0, 0.0)
    confidence: str = "estimate"

    @property
    def radio(self) -> bool:
        return any(self.cap)


OBSTACLE_MAX_LOSS = 25.0                         # version 1's cap, for its four kinds
_V1CAP = (OBSTACLE_MAX_LOSS,) * 3


def _item(title: str, he: str, cap: tuple, size: tuple, conf: str = "derived") -> Kind:
    """An item whose loss is one figure per crossing (ITU-R P.526 diffraction round it): the full figure after
    25 cm of path through it."""
    return Kind(title, he, tuple(c / 0.25 for c in cap), cap, size, conf)


OBSTACLE_KINDS: dict[str, Kind] = {
    "racking_light": Kind("Racking, half empty", "מדפים חצי ריקים", (1.0, 1.5, 1.6), _V1CAP, (4.0, 1.0, 2.5, 0.0)),
    "racking_full": Kind("Racking, full", "מדפים מלאים", (2.5, 3.5, 3.8), _V1CAP, (4.0, 1.0, 2.5, 0.0)),
    "stock": Kind("Stacked goods / pallets", "סחורה מוערמת", (2.0, 3.0, 3.3), _V1CAP, (1.2, 1.0, 1.5, 0.0)),
    "machine": Kind("Machine, solid metal", "מכונה", (8.0, 10.0, 10.5), _V1CAP, (2.0, 1.5, 2.0, 0.0)),
    "cnc": _item("CNC machining centre", "מכונת CNC", (17.5, 20.9, 21.5), (2.5, 2.2, 2.6, 0.0)),
    "pallet_empty": _item("Pallet racking, empty", "מדפי משטחים ריקים", (0.5, 0.8, 0.8), (2.7, 1.1, 4.5, 0.0),
                          "estimate"),
    "pallet_dry": _item("Pallet racking, dry goods", "מדפי משטחים, סחורה יבשה", (2.5, 4.0, 4.5),
                        (2.7, 1.1, 4.5, 0.0), "estimate"),
    "pallet_wet": _item("Pallet racking, drinks / liquids", "מדפי משטחים, נוזלים", (33.5, 37.0, 37.5),
                        (2.7, 1.1, 4.5, 0.0)),
    "server_rack": _item("Server rack, full", "ארון תקשורת", (11.3, 14.4, 14.9), (0.6, 1.0, 2.0, 0.0)),
    "metal_cabinet": _item("Metal cabinet / lockers", "ארון מתכת", (12.7, 16.0, 16.5), (0.9, 0.5, 1.95, 0.0)),
    "fridge": _item("Refrigerator", "מקרר", (10.8, 13.8, 14.3), (0.7, 0.7, 1.8, 0.0)),
    "washer": _item("Washing machine", "מכונת כביסה", (9.0, 11.0, 11.5), (0.6, 0.6, 0.85, 0.0), "estimate"),
    "bookshelf": _item("Bookshelf, full", "ספרייה", (7.1, 12.5, 13.7), (0.9, 0.3, 2.0, 0.0), "estimate"),
    "aquarium": _item("Aquarium / water tank", "אקווריום / מכל מים", (9.9, 12.2, 12.5), (1.0, 0.4, 0.5, 0.8)),
    "column": _item("Concrete column", "עמוד בטון", (15.0, 20.0, 21.0), (0.4, 0.4, 3.0, 0.0), "estimate"),
    "wardrobe": _item("Wardrobe, wood, full", "ארון בגדים", (3.0, 4.0, 4.4), (1.8, 0.6, 2.4, 0.0), "estimate"),
    "kitchen": _item("Kitchen run (cabinets)", "מטבח", (6.0, 8.0, 8.5), (3.0, 0.6, 0.9, 0.0), "estimate"),
    # drawn, but too small or too soft to count for the radio (0 dB)
    "sofa": Kind("Sofa", "ספה", (0.0, 0.0, 0.0), (0.0, 0.0, 0.0), (2.2, 0.9, 0.85, 0.0)),
    "bed": Kind("Bed", "מיטה", (0.0, 0.0, 0.0), (0.0, 0.0, 0.0), (1.6, 2.0, 0.5, 0.0)),
    "desk": Kind("Desk", "שולחן עבודה", (0.0, 0.0, 0.0), (0.0, 0.0, 0.0), (1.4, 0.7, 0.75, 0.0)),
    "table": Kind("Table", "שולחן", (0.0, 0.0, 0.0), (0.0, 0.0, 0.0), (1.6, 0.9, 0.75, 0.0)),
}
V1_KINDS = ("racking_light", "racking_full", "stock", "machine")
FURNITURE_SIZES = {k: v.size for k, v in OBSTACLE_KINDS.items()}

# clutter -> (title, path-loss exponent beyond 1 m).  With the walls drawn, open space is 2.0 (ITU-R P.1238:
# the walls themselves are counted, so a steeper exponent would count them twice).
CLUTTER: dict[str, tuple[str, float]] = {
    "open": ("Open space", 2.0),
    "home": ("Home or office (walls drawn)", 2.0),
    "light": ("Workshop, light clutter", 2.2),
    "dense": ("Dense machinery / racking", 2.5),
}

# room roles for labels -> (title, Hebrew title)
ROOM_ROLES: dict[str, tuple[str, str]] = {
    "living": ("Living room", "סלון"), "kitchen": ("Kitchen", "מטבח"), "bedroom": ("Bedroom", "חדר שינה"),
    "mamad": ("Safe room (mamad)", "ממ\"ד"), "bath": ("Bathroom", "אמבטיה"), "wc": ("Toilet", "שירותים"),
    "hall": ("Hall", "מבואה"), "service": ("Service balcony", "מרפסת שירות"), "balcony": ("Balcony", "מרפסת"),
    "stair": ("Stairwell", "חדר מדרגות"), "office": ("Office", "משרד"), "meeting": ("Meeting room", "חדר ישיבות"),
    "class": ("Classroom", "כיתה"), "store": ("Storeroom", "מחסן"), "workshop": ("Workshop", "סדנה"),
    "outside": ("Outside", "חוץ"), "other": ("Room", "חדר"),
}

# received signal bands, strongest first: (floor in dBm, title, what it is good for)
SIGNAL_BANDS: list[tuple[float, str, str]] = [
    (-50.0, "Excellent", "any application"),
    (-60.0, "Very good", "high-density, large transfers"),
    (-67.0, "Good", "voice and video"),
    (-75.0, "Fair", "web and e-mail"),
    (-85.0, "Weak", "unreliable"),
]
NO_SERVICE = "No service"
NO_SIGNAL = -200.0                               # RSSI reported when no access point transmits
UNCERTAINTY_DB = {"home": 5.0, "open": 5.0, "light": 7.0, "dense": 9.0}     # ITU-R P.1238 spread, one sigma

MIN_SIDE, MAX_SIDE = 1.0, 500.0
MIN_HEIGHT, MAX_HEIGHT = 2.0, 30.0
MAX_WALL_DB = 70.0
AP_WALL_CLEARANCE = 0.05                         # an access point never sits on a wall's plane (metres)


def clamp(v: float, lo: float, hi: float) -> float:
    return lo if v < lo else hi if v > hi else v


def _num(v, default: float) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    return f if math.isfinite(f) else default


def _opt(v, lo: float, hi: float) -> float | None:
    return None if v is None else clamp(_num(v, lo), lo, hi)


def _extra(d: dict, known: set) -> dict:
    """The keys of ``d`` this module does not know: kept and written back (forward compatibility)."""
    return {k: v for k, v in d.items() if k not in known}


def scaled(base: tuple, own5: float | None) -> tuple:
    """A (2.4, 5, 6) loss with its 5 GHz figure replaced by ``own5``; the other bands keep their ratio to it."""
    if own5 is None:
        return tuple(base)
    if base[1] <= 0.0:
        return (own5, own5, own5)
    return (own5 * base[0] / base[1], own5, own5 * base[2] / base[1])


# ====================================================================== openings
@dataclass
class Door:
    """Version 1's door; still the type ``Partition.door`` reads and writes (the first door opening)."""
    at_pct: float = 50.0          # door centre along the wall's own run, 0..100 %
    width_m: float = 0.9
    height_m: float = 2.1
    open: bool = True

    def to_dict(self) -> dict:
        return {"at_pct": self.at_pct, "width_m": self.width_m, "height_m": self.height_m, "open": self.open}

    @classmethod
    def from_dict(cls, d: dict) -> Door:
        return cls(clamp(_num(d.get("at_pct"), 50.0), 0.0, 100.0), clamp(_num(d.get("width_m"), 0.9), 0.5, 6.0),
                   clamp(_num(d.get("height_m"), 2.1), 1.5, 6.0), bool(d.get("open", True)))


_OPENING_KEYS = {"kind", "at_pct", "width_m", "sill_m", "height_m", "fill", "open", "shutter", "loss_db"}


@dataclass
class Opening:
    """A door or a window in a partition: centre ``at_pct`` % along the wall's run, ``width_m`` wide, from
    ``sill_m`` above the floor up to ``sill_m + height_m``.  ``open`` = a hole (door leaf or sash open); else
    the ``fill`` attenuates.  A ``shutter`` (trisim) adds its loss in front, open or not."""
    kind: str = "door"
    at_pct: float = 50.0
    width_m: float = 0.9
    sill_m: float = 0.0
    height_m: float = 2.1
    fill: str = "wood_door"
    open: bool = False
    shutter: str = "none"
    loss_db: float | None = None      # own loss at 5 GHz of the closed fill; None = the fill's
    extra: dict = field(default_factory=dict, compare=False, repr=False)

    def losses(self) -> tuple:
        """dB at (2.4, 5, 6 GHz) for a straight line through the opening."""
        fill = (0.0, 0.0, 0.0) if self.open else scaled(FILLS[self.fill].loss, self.loss_db)
        return tuple(f + s for f, s in zip(fill, SHUTTERS[self.shutter].loss))

    def title(self) -> str:
        if self.kind == "door":
            what = ("Open door" if self.open or self.fill == "none" else "Closed door" if self.fill == "wood_door"
                    else f"Closed door ({FILLS[self.fill].title})")
        else:
            what = "Open window" if self.open else f"Window ({FILLS[self.fill].title})"
        return what if self.shutter == "none" else f"{what}, {SHUTTERS[self.shutter].title.lower()}"

    def is_legacy_door(self) -> bool:
        """True when version 1's ``door`` says all there is to say about it."""
        return (self.kind == "door" and self.sill_m == 0.0 and self.fill == "wood_door" and self.shutter == "none"
                and self.loss_db is None and not self.extra and 0.5 <= self.width_m <= 6.0
                and 1.5 <= self.height_m <= 6.0)

    def as_door(self) -> Door:
        return Door(self.at_pct, self.width_m, self.height_m, self.open)

    @classmethod
    def from_door(cls, door: Door) -> Opening:
        return cls("door", door.at_pct, door.width_m, 0.0, door.height_m, "wood_door", door.open)

    def to_dict(self) -> dict:
        return {**self.extra, "kind": self.kind, "at_pct": self.at_pct, "width_m": self.width_m,
                "sill_m": self.sill_m, "height_m": self.height_m, "fill": self.fill, "open": self.open,
                "shutter": self.shutter, "loss_db": self.loss_db}

    @classmethod
    def from_dict(cls, d: dict) -> Opening:
        kind = d.get("kind") if d.get("kind") in OPENING_KINDS else "door"
        win = kind == "window"
        fill = d.get("fill") if d.get("fill") in FILLS else ("double_glazing" if win else "wood_door")
        return cls(kind, clamp(_num(d.get("at_pct"), 50.0), 0.0, 100.0),
                   clamp(_num(d.get("width_m"), 1.2 if win else 0.9), 0.2, 50.0),
                   clamp(_num(d.get("sill_m"), 0.9 if win else 0.0), 0.0, MAX_HEIGHT),
                   clamp(_num(d.get("height_m"), 1.2 if win else 2.1), 0.2, MAX_HEIGHT),
                   fill, bool(d.get("open", False)),
                   d.get("shutter") if d.get("shutter") in SHUTTERS else "none",
                   _opt(d.get("loss_db"), 0.0, MAX_WALL_DB), _extra(d, _OPENING_KEYS))


def window(at_pct: float = 50.0, width_m: float = 1.2, sill_m: float = 0.9, height_m: float = 1.2,
           fill: str = "double_glazing", shutter: str = "none", open: bool = False) -> Opening:
    return Opening("window", at_pct, width_m, sill_m, height_m, fill, open, shutter)


def door(at_pct: float = 50.0, width_m: float = 0.9, height_m: float = 2.1, fill: str = "wood_door",
         open: bool = False, shutter: str = "none") -> Opening:
    return Opening("door", at_pct, width_m, 0.0, height_m, fill, open, shutter)


# ====================================================================== partitions
_PARTITION_KEYS = {"axis", "at_pct", "start_pct", "end_pct", "material", "door", "loss_db", "openings", "role",
                   "thickness_m", "name"}


@dataclass
class Partition:
    """A wall across the box.  ``axis`` is the axis the wall cuts: an ``x`` partition stands at
    ``at_pct`` % of the width and runs along the depth from ``start_pct`` to ``end_pct``.  It holds any number
    of ``openings``; ``door`` (version 1) reads and writes the first door among them."""
    axis: str = "x"
    at_pct: float = 50.0
    start_pct: float = 0.0
    end_pct: float = 100.0
    material: str = "drywall"
    door: InitVar[Door | None] = None
    loss_db: float | None = None      # this wall's own loss at 5 GHz (measured or known); None = the material's
    openings: list[Opening] = field(default_factory=list)
    role: str = "inner"               # "inner" / "outer" (the building's shell: may stand on the box edge)
    thickness_m: float | None = None  # drawing only; None = the material's typical thickness
    name: str = ""
    extra: dict = field(default_factory=dict, compare=False, repr=False)

    def __post_init__(self, door: Door | None) -> None:
        if isinstance(door, Door):
            self.openings.insert(0, Opening.from_door(door))

    def losses(self) -> tuple:
        """Loss through this wall in dB at (2.4, 5, 6 GHz).  An own value is given for 5 GHz; the other bands
        keep the material's ratio to it."""
        return scaled(MATERIALS[self.material].loss, self.loss_db)

    @property
    def thickness(self) -> float:
        return self.thickness_m if self.thickness_m is not None else MATERIALS[self.material].thickness_m

    def to_dict(self) -> dict:
        first = next((o for o in self.openings if o.kind == "door"), None)
        legacy = len(self.openings) == 0 or len(self.openings) == 1 and first is not None and first.is_legacy_door()
        out = {**self.extra, "axis": self.axis, "at_pct": self.at_pct, "start_pct": self.start_pct,
               "end_pct": self.end_pct, "material": self.material, "loss_db": self.loss_db}
        if legacy:
            out["door"] = first.as_door().to_dict() if first else None
        else:
            out["openings"] = [o.to_dict() for o in self.openings]
        if self.role != "inner":
            out["role"] = self.role
        if self.thickness_m is not None:
            out["thickness_m"] = self.thickness_m
        if self.name:
            out["name"] = self.name
        return out

    def is_v1(self) -> bool:
        d = self.to_dict()
        return "openings" not in d and set(d) <= {"axis", "at_pct", "start_pct", "end_pct", "material", "door",
                                                  "loss_db"} and self.material in V1_MATERIALS

    @classmethod
    def from_dict(cls, d: dict) -> Partition:
        a = clamp(_num(d.get("start_pct"), 0.0), 0.0, 100.0)
        b = clamp(_num(d.get("end_pct"), 100.0), 0.0, 100.0)
        if b < a:
            a, b = b, a
        role = "outer" if d.get("role") == "outer" else "inner"
        edge = (0.0, 100.0) if role == "outer" else (1.0, 99.0)      # only the shell may stand on the box edge
        if isinstance(d.get("openings"), list):
            openings = [Opening.from_dict(o) for o in d["openings"] if isinstance(o, dict)]
        elif isinstance(d.get("door"), dict):                        # version 1: one door
            openings = [Opening.from_door(Door.from_dict(d["door"]))]
        else:
            openings = []
        return cls("y" if d.get("axis") == "y" else "x", clamp(_num(d.get("at_pct"), 50.0), *edge), a, b,
                   d.get("material") if d.get("material") in MATERIALS else "drywall", None,
                   _opt(d.get("loss_db"), 0.0, MAX_WALL_DB), openings, role, _opt(d.get("thickness_m"), 0.02, 1.0),
                   str(d.get("name") or ""), _extra(d, _PARTITION_KEYS))


def _get_door(self: Partition) -> Opening | None:
    """The first door itself (an Opening has Door's fields), so ``p.door.open = False`` works as in version 1."""
    return next((o for o in self.openings if o.kind == "door"), None)


def _set_door(self: Partition, door: Door | None) -> None:
    """Replace (or with None remove) the first door; keeps its fill and shutter when there was one."""
    i = next((k for k, o in enumerate(self.openings) if o.kind == "door"), None)
    if door is None:
        if i is not None:
            del self.openings[i]
        return
    if i is None:
        self.openings.insert(0, Opening.from_door(door))
    else:
        o = self.openings[i]
        o.at_pct, o.width_m, o.height_m, o.open = door.at_pct, door.width_m, door.height_m, door.open


Partition.door = property(_get_door, _set_door)       # the dataclass took ``door`` as an init-only argument


# ====================================================================== obstacles, access points, labels
_OBSTACLE_KEYS = {"name", "kind", "x", "y", "w", "d", "h", "loss_db_m", "z0"}


@dataclass
class Obstacle:
    """A box in the room (furniture, a machine, racking, a column): ``x, y`` is its corner nearest the origin,
    ``w`` runs along the width, ``d`` along the depth, ``h`` is its height, ``z0`` how high its bottom is above
    the floor (0 = standing on it)."""
    name: str
    kind: str = "racking_full"
    x: float = 0.0
    y: float = 0.0
    w: float = 4.0
    d: float = 1.0
    h: float = 2.5
    loss_db_m: float | None = None    # own loss per metre at 5 GHz; None = the kind's
    z0: float = 0.0
    extra: dict = field(default_factory=dict, compare=False, repr=False)

    def losses(self) -> tuple:
        """(dB per metre of path inside the box, most dB the item takes away), each at (2.4, 5, 6 GHz)."""
        k = OBSTACLE_KINDS[self.kind]
        return scaled(k.per_m, self.loss_db_m), k.cap

    def to_dict(self) -> dict:
        out = {**self.extra, "name": self.name, "kind": self.kind, "x": self.x, "y": self.y, "w": self.w,
               "d": self.d, "h": self.h, "loss_db_m": self.loss_db_m}
        if self.z0:
            out["z0"] = self.z0
        return out

    @classmethod
    def from_dict(cls, d: dict) -> Obstacle:
        return cls(str(d.get("name") or "Obstacle"), d.get("kind") if d.get("kind") in OBSTACLE_KINDS else "racking_full",
                   _num(d.get("x"), 0.0), _num(d.get("y"), 0.0), clamp(_num(d.get("w"), 4.0), 0.1, MAX_SIDE),
                   clamp(_num(d.get("d"), 1.0), 0.1, MAX_SIDE), clamp(_num(d.get("h"), 2.5), 0.1, MAX_HEIGHT),
                   _opt(d.get("loss_db_m"), 0.0, 400.0), clamp(_num(d.get("z0"), 0.0), 0.0, MAX_HEIGHT),
                   _extra(d, _OBSTACLE_KEYS))


def furniture(kind: str, name: str, x: float, y: float, rotated: bool = False, **kw) -> Obstacle:
    """A new item of a kind at its typical size; ``rotated`` turns it 90 degrees (swaps w and d)."""
    w, d, h, z0 = OBSTACLE_KINDS[kind].size
    if rotated:
        w, d = d, w
    return Obstacle(name, kind, x, y, kw.pop("w", w), kw.pop("d", d), kw.pop("h", h), kw.pop("loss_db_m", None),
                    kw.pop("z0", z0))


@dataclass
class AccessPoint:
    name: str
    x: float
    y: float
    z: float = 2.6
    tx_dbm: float = 20.0          # EIRP: transmit power plus antenna gain
    band: str = "5"
    enabled: bool = True
    from_lab: bool = False        # mirrors an access point node of the open lab

    def over_limit(self) -> bool:
        """True when the EIRP is above what Israel allows in this band."""
        return self.tx_dbm > LEGAL_EIRP_DBM.get(self.band, 99.0) + 1e-9

    def to_dict(self) -> dict:
        return {"name": self.name, "x": self.x, "y": self.y, "z": self.z, "tx_dbm": self.tx_dbm,
                "band": self.band, "enabled": self.enabled, "from_lab": self.from_lab}

    @classmethod
    def from_dict(cls, d: dict) -> AccessPoint:
        return cls(str(d.get("name") or "AP"), _num(d.get("x"), 0.0), _num(d.get("y"), 0.0), _num(d.get("z"), 2.6),
                   clamp(_num(d.get("tx_dbm"), 20.0), 0.0, 36.0), d.get("band") if d.get("band") in BAND_MHZ else "5",
                   bool(d.get("enabled", True)), bool(d.get("from_lab", False)))


@dataclass
class RoomLabel:
    """A name for the derived room that holds the point (x, y).  ``ceiling_m`` (None = the box height) is
    for drawing and for the default access point height; the radio does not use it."""
    name: str
    x: float
    y: float
    role: str = "other"
    name_he: str = ""
    ceiling_m: float | None = None

    def to_dict(self) -> dict:
        return {"name": self.name, "name_he": self.name_he, "role": self.role, "x": self.x, "y": self.y,
                "ceiling_m": self.ceiling_m}

    @classmethod
    def from_dict(cls, d: dict) -> RoomLabel:
        role = d.get("role") if d.get("role") in ROOM_ROLES else "other"
        return cls(str(d.get("name") or ROOM_ROLES[role][0]), _num(d.get("x"), 0.0), _num(d.get("y"), 0.0), role,
                   str(d.get("name_he") or ""), _opt(d.get("ceiling_m"), MIN_HEIGHT, MAX_HEIGHT))


@dataclass(frozen=True)
class Room:
    """One enclosed part of the box: a union of rectangles (x0, y0, x1, y1) in metres."""
    rects: tuple
    area_m2: float
    pct: float

    @property
    def label_point(self) -> tuple[float, float]:
        x0, y0, x1, y1 = max(self.rects, key=lambda r: (r[2] - r[0]) * (r[3] - r[1]))
        return (x0 + x1) / 2, (y0 + y1) / 2

    def contains(self, x: float, y: float) -> bool:
        return any(r[0] <= x <= r[2] and r[1] <= y <= r[3] for r in self.rects)


@dataclass(frozen=True)
class Hole:
    """An opening resolved to metres: run ``d0..d1`` along the wall, height ``z0..z1``, its losses."""
    d0: float
    d1: float
    z0: float
    z1: float
    loss: tuple                       # dB at 2.4, 5, 6 GHz
    opening: Opening


@dataclass(frozen=True)
class Wall:
    """A partition resolved to metres.  ``a`` is the position on the cut axis, ``b0..b1`` the run along
    the other axis; ``holes`` are its openings.  ``door, d0, d1, door_h`` repeat the first door hole the way
    version 1 had them, for drawing code that only knows doors."""
    axis: str
    a: float
    b0: float
    b1: float
    material: str
    door: Opening | None = None
    d0: float = 0.0
    d1: float = 0.0
    door_h: float = 0.0
    loss: tuple = (0.0, 0.0, 0.0)     # dB at 2.4, 5, 6 GHz
    holes: tuple = ()
    role: str = "inner"
    thickness: float = 0.12
    index: int = -1                   # into Site.partitions
    name: str = ""


_SITE_KEYS = {"width", "depth", "height", "clutter", "slice_z", "partitions", "aps", "obstacles", "format", "version",
              "meta", "labels", "name", "source", "north_deg"}


@dataclass
class Site:
    width: float = 12.0
    depth: float = 8.0
    height: float = 3.0
    partitions: list[Partition] = field(default_factory=list)
    aps: list[AccessPoint] = field(default_factory=list)
    obstacles: list[Obstacle] = field(default_factory=list)
    clutter: str = "light"
    slice_z: float = 1.0          # height of the plane the coverage is measured on (a device on a bench)
    labels: list[RoomLabel] = field(default_factory=list)
    # where a library room came from and what it is: title, title_he, kind, city, era, source {title, url,
    # license}, assumptions [...], notes, ...  Kept as given (any JSON) and written back.
    meta: dict = field(default_factory=dict)
    extra: dict = field(default_factory=dict, compare=False, repr=False)

    # ------------------------------------------------------------------ persistence
    def uses_v2(self) -> bool:
        return bool(self.labels or self.meta or self.clutter not in ("open", "light", "dense")
                    or not all(p.is_v1() for p in self.partitions)
                    or any(o.z0 or o.kind not in V1_KINDS for o in self.obstacles)
                    or any(a.band not in ("2.4", "5") for a in self.aps))

    def to_dict(self) -> dict:
        out = {**self.extra, "width": self.width, "depth": self.depth, "height": self.height, "clutter": self.clutter,
               "slice_z": self.slice_z, "partitions": [p.to_dict() for p in self.partitions],
               "aps": [a.to_dict() for a in self.aps], "obstacles": [o.to_dict() for o in self.obstacles]}
        if self.uses_v2():
            out = {"format": FORMAT, "version": VERSION, **({"meta": self.meta} if self.meta else {}), **out}
            if self.labels:
                out["labels"] = [lb.to_dict() for lb in self.labels]
        return out

    @classmethod
    def from_dict(cls, d: dict | None) -> Site:
        d = d if isinstance(d, dict) else {}
        s = cls(clamp(_num(d.get("width"), 12.0), MIN_SIDE, MAX_SIDE),
                clamp(_num(d.get("depth"), 8.0), MIN_SIDE, MAX_SIDE),
                clamp(_num(d.get("height"), 3.0), MIN_HEIGHT, MAX_HEIGHT))
        s.clutter = d.get("clutter") if d.get("clutter") in CLUTTER else "light"
        s.partitions = [Partition.from_dict(p) for p in d.get("partitions") or [] if isinstance(p, dict)]
        s.aps = [AccessPoint.from_dict(a) for a in d.get("aps") or [] if isinstance(a, dict)]
        s.obstacles = [Obstacle.from_dict(o) for o in d.get("obstacles") or [] if isinstance(o, dict)]
        s.slice_z = _num(d.get("slice_z"), 1.0)
        s.labels = [RoomLabel.from_dict(lb) for lb in d.get("labels") or [] if isinstance(lb, dict)]
        meta = d.get("meta")
        s.meta = dict(meta) if isinstance(meta, dict) else {}
        for k in ("name", "source", "north_deg"):          # the council spike's "labdesk-site" keys
            if k in d and k not in s.meta:
                s.meta["title" if k == "name" else k] = d[k]
        s.extra = _extra(d, _SITE_KEYS)
        s.normalize()
        return s

    @staticmethod
    def newer_than_supported(d) -> bool:
        """True for a file written by a later version: the app should open it read-only (no autosave)."""
        return isinstance(d, dict) and _num(d.get("version"), 1.0) > VERSION

    @property
    def title(self) -> str:
        return str(self.meta.get("title") or "")

    def normalize(self) -> None:
        """Keep everything inside the box (after the box was resized or a file was edited by hand), and keep
        access points off the walls' planes (a point on the plane would see through the wall)."""
        self.slice_z = clamp(self.slice_z, 0.0, self.height)
        for o in self.obstacles:
            o.w, o.d, o.h = min(o.w, self.width), min(o.d, self.depth), min(o.h, self.height)
            o.x, o.y = clamp(o.x, 0.0, self.width - o.w), clamp(o.y, 0.0, self.depth - o.d)
            o.z0 = clamp(o.z0, 0.0, self.height - o.h)
        for lb in self.labels:
            lb.x, lb.y = clamp(lb.x, 0.0, self.width), clamp(lb.y, 0.0, self.depth)
        walls = self.walls() if self.aps else []
        for ap in self.aps:
            ap.x, ap.y, ap.z = clamp(ap.x, 0.0, self.width), clamp(ap.y, 0.0, self.depth), clamp(ap.z, 0.0, self.height)
            for w in walls:
                i, j = (0, 1) if w.axis == "x" else (1, 0)
                pos = [ap.x, ap.y]
                gap = pos[i] - w.a
                if abs(gap) < AP_WALL_CLEARANCE and w.b0 - 1e-9 <= pos[j] <= w.b1 + 1e-9:
                    side = (self.width, self.depth)[i]
                    step = AP_WALL_CLEARANCE if (gap >= 0.0 and w.a + AP_WALL_CLEARANCE <= side) or \
                        w.a - AP_WALL_CLEARANCE < 0.0 else -AP_WALL_CLEARANCE
                    pos[i] = w.a + step
                    ap.x, ap.y = pos

    # ------------------------------------------------------------------ geometry
    def walls(self) -> list[Wall]:
        out = []
        for idx, p in enumerate(self.partitions):
            side, run = (self.width, self.depth) if p.axis == "x" else (self.depth, self.width)
            b0, b1 = run * p.start_pct / 100.0, run * p.end_pct / 100.0
            if b1 - b0 <= 1e-9:
                continue
            holes: list[Hole] = []
            for o in p.openings:
                ow = min(o.width_m, b1 - b0)
                c = clamp(b0 + (b1 - b0) * o.at_pct / 100.0, b0 + ow / 2, b1 - ow / 2)
                z0, z1 = min(o.sill_m, self.height), min(o.sill_m + o.height_m, self.height)
                if z1 - z0 <= 1e-6 or ow <= 1e-6:
                    continue
                h = Hole(c - ow / 2, c + ow / 2, z0, z1, o.losses(), o)
                if any(h.d0 <= q.d1 + OPENING_GAP and q.d0 <= h.d1 + OPENING_GAP and h.z0 <= q.z1 + OPENING_GAP
                       and q.z0 <= h.z1 + OPENING_GAP for q in holes):
                    continue                                    # overlaps an earlier opening: dropped
                holes.append(h)
            first = next((h for h in holes if h.opening.kind == "door"), None)
            legacy = {} if first is None else {"door": first.opening, "d0": first.d0, "d1": first.d1,
                                               "door_h": first.z1}
            out.append(Wall(p.axis, side * p.at_pct / 100.0, b0, b1, p.material, loss=p.losses(),
                            holes=tuple(holes), role=p.role, thickness=p.thickness, index=idx, name=p.name, **legacy))
        return out

    def problems(self) -> list[str]:
        """What the model silently corrects, said in words (for the panel)."""
        out = []
        kept = {w.index: {id(h.opening) for h in w.holes} for w in self.walls()}
        for i, p in enumerate(self.partitions):
            for k, o in enumerate(p.openings):
                if i in kept and id(o) not in kept[i]:
                    out.append(f"Wall {i + 1}: opening {k + 1} overlaps another one and is ignored")
        rooms = self.rooms()
        named: dict[int, str] = {}
        for lb in self.labels:
            k = next((i for i, r in enumerate(rooms) if r.contains(lb.x, lb.y)), None)
            if k is None:
                out.append(f"Label {lb.name!r} is outside every room")
            elif k in named:
                out.append(f"Label {lb.name!r} is in the same room as {named[k]!r}: not shown")
            else:
                named[k] = lb.name
        for ap in self.aps:
            if ap.over_limit():
                out.append(f"{ap.name}: {ap.tx_dbm:g} dBm is above the legal limit. {LEGAL_NOTE[ap.band]}.")
        return out

    def rooms(self) -> list[Room]:
        """The enclosed parts of the box, largest first.  A door does not join two rooms."""
        walls = self.walls()
        xs = sorted({0.0, self.width} | {w.a for w in walls if w.axis == "x"}
                    | {b for w in walls if w.axis == "y" for b in (w.b0, w.b1)})
        ys = sorted({0.0, self.depth} | {w.a for w in walls if w.axis == "y"}
                    | {b for w in walls if w.axis == "x" for b in (w.b0, w.b1)})
        xs = [v for i, v in enumerate(xs) if i == 0 or v - xs[i - 1] > 1e-9]
        ys = [v for i, v in enumerate(ys) if i == 0 or v - ys[i - 1] > 1e-9]
        nx, ny = len(xs) - 1, len(ys) - 1
        parent = list(range(nx * ny))

        def find(i: int) -> int:
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i

        def blocked(axis: str, a: float, lo: float, hi: float) -> bool:
            return any(w.axis == axis and abs(w.a - a) < 1e-9 and w.b0 <= lo + 1e-9 and w.b1 >= hi - 1e-9
                       for w in walls)

        for i in range(nx):
            for j in range(ny):
                if i + 1 < nx and not blocked("x", xs[i + 1], ys[j], ys[j + 1]):
                    parent[find(i * ny + j)] = find((i + 1) * ny + j)
                if j + 1 < ny and not blocked("y", ys[j + 1], xs[i], xs[i + 1]):
                    parent[find(i * ny + j)] = find(i * ny + j + 1)
        groups: dict[int, list] = {}
        for i in range(nx):
            for j in range(ny):
                groups.setdefault(find(i * ny + j), []).append((xs[i], ys[j], xs[i + 1], ys[j + 1]))
        total = self.width * self.depth
        rooms = []
        for rects in groups.values():
            area = sum((r[2] - r[0]) * (r[3] - r[1]) for r in rects)
            rooms.append(Room(tuple(rects), area, 100.0 * area / total))
        rooms.sort(key=lambda r: (-r.area_m2, r.rects[0]))
        return rooms

    def named_rooms(self) -> list[tuple[Room, RoomLabel | None]]:
        """Every derived room with the first label whose point lies in it (None: unnamed)."""
        return [(r, next((lb for lb in self.labels if r.contains(lb.x, lb.y)), None)) for r in self.rooms()]

    def outside_rects(self) -> list[tuple]:
        """Floor rectangles of the rooms labelled 'outside' (yard, street, balcony strip): not counted as the
        customer's floor in the shares."""
        return [rect for r, lb in self.named_rooms() if lb is not None and lb.role == "outside" for rect in r.rects]

    def ceiling_at(self, x: float, y: float) -> float:
        """The ceiling height over (x, y): the label of that room says, else the box height."""
        for r, lb in self.named_rooms():
            if r.contains(x, y) and lb is not None and lb.ceiling_m is not None:
                return min(lb.ceiling_m, self.height)
        return self.height

    def envelope(self, material: str = "block_20", inset: dict | None = None) -> list[Partition]:
        """Add the building's outer walls: one ``outer`` partition per side, ``inset`` metres in from the box
        edge (keys "s" y=0, "n" y=depth, "w" x=0, "e" x=width; 0 = on the edge).  Returns the four walls."""
        ins = {"s": 0.0, "n": 0.0, "w": 0.0, "e": 0.0, **(inset or {})}
        xw, xe = ins["w"], self.width - ins["e"]
        ys_, yn = ins["s"], self.depth - ins["n"]
        px = lambda v: 100.0 * v / self.width          # noqa: E731
        py = lambda v: 100.0 * v / self.depth          # noqa: E731
        new = [Partition("y", py(ys_), px(xw), px(xe), material, role="outer", name="South wall"),
               Partition("y", py(yn), px(xw), px(xe), material, role="outer", name="North wall"),
               Partition("x", px(xw), py(ys_), py(yn), material, role="outer", name="West wall"),
               Partition("x", px(xe), py(ys_), py(yn), material, role="outer", name="East wall")]
        self.partitions.extend(new)
        return new

    # ------------------------------------------------------------------ radio
    def crossings(self, a: tuple, b: tuple, band: str, walls: list[Wall] | None = None) -> list[tuple]:
        """Where the straight line a -> b passes through walls and obstacles, nearest to ``a`` first:
        ``(point, loss_db, what)`` with ``what`` a material title, an opening's title or an obstacle's name."""
        k = bi(band)
        out = []
        for w in self.walls() if walls is None else walls:
            i, j = (0, 1) if w.axis == "x" else (1, 0)
            da, db = a[i] - w.a, b[i] - w.a
            if da * db >= 0.0:                      # both ends on the same side (or touching the wall)
                continue
            t = da / (da - db)
            run = a[j] + (b[j] - a[j]) * t
            if run < w.b0 or run > w.b1:
                continue
            pt = tuple(a[m] + (b[m] - a[m]) * t for m in range(3))
            hole = next((h for h in w.holes if h.d0 <= run <= h.d1 and h.z0 <= pt[2] <= h.z1), None)
            if hole is not None:                    # first match: holes of one wall never overlap
                out.append((t, pt, hole.loss[k], hole.opening.title()))
            else:
                out.append((t, pt, w.loss[k], MATERIALS[w.material].title))
        for o in self.obstacles:
            per_m, cap = o.losses()
            if cap[k] <= 0.0 or per_m[k] <= 0.0:
                continue
            hit = segment_in_box(a, b, (o.x, o.y, o.z0), (o.x + o.w, o.y + o.d, o.z0 + o.h))
            if hit is not None:
                t0, t1 = hit
                loss = min(cap[k], per_m[k] * (t1 - t0) * math.dist(a, b))
                tm = (t0 + t1) / 2
                out.append((tm, tuple(a[m] + (b[m] - a[m]) * tm for m in range(3)), loss, o.name))
        out.sort(key=lambda c: c[0])
        return [c[1:] for c in out]

    def wall_loss(self, a: tuple, b: tuple, band: str, walls: list[Wall] | None = None) -> float:
        """dB lost in the walls and obstacles crossed by the straight line a -> b (points are (x, y, z))."""
        return sum(c[1] for c in self.crossings(a, b, band, walls))

    def path_loss(self, distance_m: float, band: str) -> float:
        d = max(distance_m, 0.1)
        ref = 20.0 * math.log10(BAND_MHZ.get(band, BAND_MHZ["5"])) - 27.55       # free-space loss at 1 m
        n = CLUTTER[self.clutter][1] if d > 1.0 else 2.0
        return ref + 10.0 * n * math.log10(d)

    def rssi(self, ap: AccessPoint, pt: tuple, walls: list[Wall] | None = None) -> float:
        d = math.dist((ap.x, ap.y, ap.z), pt)
        return ap.tx_dbm - self.path_loss(d, ap.band) - self.wall_loss((ap.x, ap.y, ap.z), pt, ap.band, walls)

    def best(self, pt: tuple, walls: list[Wall] | None = None) -> tuple[float, AccessPoint | None]:
        """Strongest signal at ``pt`` and the access point it comes from."""
        walls = self.walls() if walls is None else walls
        top, who = NO_SIGNAL, None
        for ap in self.aps:
            if ap.enabled:
                v = self.rssi(ap, pt, walls)
                if v > top:
                    top, who = v, ap
        return top, who

    def coverage(self, nx: int = 48, ny: int = 32, z: float | None = None) -> list[list[float]]:
        """Strongest RSSI at the centre of each cell of an ``nx`` x ``ny`` grid on the plane ``z``
        (rows run along the depth: ``grid[j][i]``)."""
        z = self.slice_z if z is None else z
        walls = self.walls()
        cw, cd = self.width / nx, self.depth / ny
        return [[self.best(((i + 0.5) * cw, (j + 0.5) * cd, z), walls)[0] for i in range(nx)] for j in range(ny)]

    # ------------------------------------------------------------------ editing helpers
    def add_ap(self, name: str | None = None, **kw) -> AccessPoint:
        used = {a.name for a in self.aps}
        if not name:
            n = 1
            while f"AP{n}" in used:
                n += 1
            name = f"AP{n}"
        k = len(self.aps)                      # spread new access points along the diagonal
        fx, fy = ((k % 3) + 1) / 4.0, ((k // 3 % 3) + 1) / 4.0
        x, y = kw.pop("x", self.width * fx), kw.pop("y", self.depth * fy)
        ap = AccessPoint(name, x, y, kw.pop("z", max(0.0, self.ceiling_at(x, y) - 0.4)), **kw)
        self.aps.append(ap)
        self.normalize()
        return ap

    def sync_lab_aps(self, labels) -> bool:
        """Mirror the open lab's access point nodes: add the missing ones, drop mirrored ones that are gone.
        Access points added by hand in the site view are kept.  Returns True when something changed."""
        labels = list(dict.fromkeys(labels))
        before = [(a.name, a.from_lab) for a in self.aps]
        self.aps = [a for a in self.aps if not a.from_lab or a.name in labels]
        for a in self.aps:
            if a.name in labels:
                a.from_lab = True
        have = {a.name for a in self.aps}
        for lbl in labels:
            if lbl not in have:
                self.add_ap(lbl, from_lab=True)
        return before != [(a.name, a.from_lab) for a in self.aps]


def segment_in_box(a: tuple, b: tuple, lo: tuple, hi: tuple) -> tuple[float, float] | None:
    """The part of the segment a -> b that lies inside the axis-aligned box lo..hi, as (t0, t1) with
    0 <= t0 < t1 <= 1 along the segment, or None when it misses the box."""
    t0, t1 = 0.0, 1.0
    for k in range(3):
        d = b[k] - a[k]
        if abs(d) < 1e-12:
            if a[k] < lo[k] or a[k] > hi[k]:
                return None
            continue
        ta, tb = (lo[k] - a[k]) / d, (hi[k] - a[k]) / d
        if ta > tb:
            ta, tb = tb, ta
        t0, t1 = max(t0, ta), min(t1, tb)
        if t1 - t0 <= 1e-12:
            return None
    return t0, t1


def band_index(rssi: float) -> int:
    """Index into SIGNAL_BANDS, or len(SIGNAL_BANDS) for 'no service'."""
    for i, (floor, _t, _u) in enumerate(SIGNAL_BANDS):
        if rssi >= floor:
            return i
    return len(SIGNAL_BANDS)


def band_title(rssi: float) -> str:
    i = band_index(rssi)
    return SIGNAL_BANDS[i][1] if i < len(SIGNAL_BANDS) else NO_SERVICE


def coverage_shares(grid: list[list[float]]) -> list[float]:
    """Share of the floor (0..100 %) in each signal band; the last entry is 'no service'."""
    counts = [0] * (len(SIGNAL_BANDS) + 1)
    for row in grid:
        for v in row:
            counts[band_index(v)] += 1
    total = sum(counts) or 1
    return [100.0 * c / total for c in counts]


def demo_site() -> Site:
    """A starting point: a 30 x 18 x 5 m workshop with two offices split off and one access point."""
    s = Site(30.0, 18.0, 5.0)
    s.partitions = [Partition("x", 70.0, 0.0, 100.0, "concrete", Door(30.0, 1.2)),
                    Partition("y", 50.0, 70.0, 100.0, "drywall", Door(50.0, open=False))]
    s.aps = [AccessPoint("AP1", 8.0, 9.0, 3.0)]
    s.obstacles = [Obstacle("Racking", "racking_full", 13.0, 3.0, 1.2, 9.0, 3.5),
                   Obstacle("Press", "machine", 3.0, 13.0, 3.0, 2.5, 2.2)]
    return s
