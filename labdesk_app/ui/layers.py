"""View ▸ Layers: one shared set of checkable QActions, persisted in QSettings.

The same QAction objects drive the menu, the legend rows and the canvas items, so toggling a
layer anywhere updates everything at once (V4/V9).
"""
from __future__ import annotations

from PySide6.QtCore import QObject, QSettings, Signal

from ..settings import app_settings
from PySide6.QtGui import QAction

# key, menu text, default, tooltip
LAYERS = [
    ("chips", "Protocol chips", True, "OSPF / BGP / route-source chips and HSRP/VRRP badges on router tiles"),
    ("p2p", "Point-to-point beads", True, "P2P / P2P? / BCAST beads on links (from the real OSPF network type)"),
    ("default", "Default route (blue)", True, "Paint the path of each router's default route to the internet in blue"),
    ("fhrp", "HSRP / VRRP", True, "FHRP role badges on routers and segment chips on switches"),
    ("ospf", "OSPF adjacency rails", False, "Teal rails along links / chords through switches for OSPF neighbours"),
    ("bgp", "BGP session rails", False, "Purple rails / arcs for BGP sessions"),
    ("traffic", "Traffic", False, "Live bit rate on every link (polls CML simulation_stats every 3 s)"),
    ("l2", "Switching (VLAN / trunk / STP)", True,
     "Managed switches: V10 access chips, TRUNK beads, spanning-tree blocked ends, ROOT crown and switch chips"),
    ("vlan", "VLAN colouring", False, "Highlight the links and ports that carry one VLAN (pick it below)"),
    ("subnets", "Subnets on cables", True,
     "The IPv4 subnet of every routed cable at its midpoint and each end's host part next to the interface "
     "(.1 / .2); ½ = one side configured, ⚠ = the ends are in different subnets"),
]
DEFAULTS = {k: d for k, _, d, _ in LAYERS}


class Layers(QObject):
    changed = Signal(str, bool)
    vlan_changed = Signal(object)          # int | None: the VLAN highlighted by the 'vlan' layer

    def __init__(self, parent=None, settings: QSettings | None = None):
        super().__init__(parent)
        self.settings = settings if settings is not None else app_settings()
        self.actions: dict[str, QAction] = {}
        for key, text, default, tip in LAYERS:
            a = QAction(text, self)
            a.setCheckable(True)
            a.setToolTip(tip)
            val = self.settings.value(f"layers/{key}", default)
            a.setChecked(_as_bool(val, default))
            a.toggled.connect(lambda on, k=key: self._toggled(k, on))
            self.actions[key] = a
        v = self.settings.value("layers/vlan_id", None)
        try:
            self.vlan: int | None = int(v) if v not in (None, "", "None") else None
        except (TypeError, ValueError):
            self.vlan = None

    def set_vlan(self, vlan: int | None) -> None:
        """Pick the VLAN of the colouring layer (and switch the layer on)."""
        self.vlan = vlan
        self.settings.setValue("layers/vlan_id", "" if vlan is None else int(vlan))
        self.vlan_changed.emit(vlan)
        if vlan is not None and not self.on("vlan"):
            self.set("vlan", True)
        elif vlan is not None:
            self.changed.emit("vlan", True)

    def _toggled(self, key: str, on: bool) -> None:
        self.settings.setValue(f"layers/{key}", on)
        self.changed.emit(key, on)

    def on(self, key: str) -> bool:
        a = self.actions.get(key)
        return a.isChecked() if a else DEFAULTS.get(key, False)

    def state(self) -> dict[str, bool]:
        return {k: a.isChecked() for k, a in self.actions.items()}

    def set(self, key: str, on: bool) -> None:
        self.actions[key].setChecked(on)


def _as_bool(v, default: bool) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v.lower() in ("1", "true", "yes", "on")
    if v is None:
        return default
    return bool(v)
