"""LabDesk main window: toolbar (L2), docks (palette, inspector, event log), status bar (L5), empty states
(L6), dialogs (L9) and the compact layout mode (L10)."""
from __future__ import annotations

import html
import subprocess
import time
from pathlib import Path

from PySide6.QtCore import QEvent, QMimeData, QObject, QPointF, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QActionGroup, QColor, QDrag, QFont, QIcon, QKeySequence, QPainter
from PySide6.QtWidgets import (QApplication, QDialog, QDockWidget, QFileDialog, QFrame, QGridLayout, QHBoxLayout,
                               QLabel, QListWidget, QListWidgetItem, QMainWindow, QMenu, QPushButton, QScrollArea,
                               QSizePolicy, QToolBar, QToolButton, QVBoxLayout, QWidget)

from .. import cloud, config, kvm, launchers, plat, validate
from .. import default_logins as DL
from .. import show_scripts as SS
from ..catalog import BY_KEY, DEVICE_TYPES, NON_COUNTING_DEFS
from ..cml_api import CMLClient, CMLError, CMLUnreachable, NodeLockedError
from ..controller import LabController, LabView as LabData
from ..probe import creds as creds_mod
from ..settings import STATE_VERSION, app_settings, as_bool
from .canvas import MIME, LabScene, LabView
from .compact import MODES as LAYOUT_MODES, CompactController
from .dialogs import (AboutDialog, CloudConfigDialog, ConfigDialog, CredentialsDialog, OpenLabDialog, ShowScriptDialog,
                      PropertiesDialog, TextPrompt, confirm, info, validator)
from .emptystate import EmptyStateCard
from .eventlog import EventLog
from . import theme as T
from .graphics import STATE_COLORS, STATE_TEXT, BundleItem, LinkItem, NodeItem, device_pixmap
from .hovercard_model import build_card
from .inspector import Inspector
from .layers import Layers
from .legend import Legend
from .scene_model import build_scene_data, ev
from .summary import lab_tone, summarize
from .widgets import DockTitle, InfoBar, Spinner, StatusPill
from . import icons
from .worker import TaskRunner

INSPECTOR_W, INSPECTOR_MIN, INSPECTOR_MIN_COMPACT = 380, 360, 340      # L4 / L10
RAIL_W = 56                                                             # L10 palette icon rail
CML_TIP = ("Running CML nodes across all labs. CML-Free runs at most 5 nodes at a time;\n"
           "unmanaged switches and external connectors do not count.")

PROBE_NOTICE = ("<b>LabDesk reads each router's state through its serial console</b> (read-only <i>show</i> "
                "commands, about every 15 s). While you have a router's console open in LabDesk it pauses that "
                "router. Output of these reads can appear in any <i>other</i> open console of the router "
                "(CML web console, your own ssh). Toolbar <b>Probing</b> switches it off.")


def _sep() -> QWidget:
    f = QFrame()
    f.setFrameShape(QFrame.VLine)
    f.setObjectName("sbsep")
    f.setFixedHeight(12)
    return f


class GlyphLabel(QWidget):
    """Status bar segment: an S2 status glyph + text (small role)."""

    def __init__(self, text: str = "", glyph: str | None = None, parent=None):
        super().__init__(parent)
        h = QHBoxLayout(self)
        h.setContentsMargins(6, 0, 6, 0)
        h.setSpacing(6)
        self._glyph = glyph
        self._tone = "neutral"
        self.dot = QWidget()
        self.dot.setFixedSize(10, 10)
        self.dot.paintEvent = self._paint_dot
        self.label = QLabel(text)
        h.addWidget(self.dot)
        h.addWidget(self.label)
        self.dot.setVisible(glyph is not None)

    def setFont(self, f):  # noqa: N802
        self.label.setFont(f)

    def setText(self, text: str):  # noqa: N802
        self.label.setText(text)

    def text(self) -> str:
        return self.label.text()

    def set_glyph(self, glyph: str | None, tone: str = "neutral"):
        self._glyph, self._tone = glyph, tone
        self.dot.setVisible(glyph is not None)
        self.dot.update()

    def _paint_dot(self, ev):
        if not self._glyph:
            return
        tk = T.tokens()
        p = QPainter(self.dot)
        p.setRenderHint(QPainter.Antialiasing)
        col = T.status_fill(self._tone, tk) if self._tone in T.STATUS else tk.N(9)
        T.draw_status_glyph(p, QPointF(5, 5), self._glyph, 8.0, col, tk)
        p.end()


class _Bridge(QObject):
    """Thread-safe hop from a probe thread to the GUI thread (queued signal)."""
    changed = Signal()


def unavailable_reason(d) -> str:
    """Why a device type cannot be added on this OS ('' = it can)."""
    if d.backend == "kvm" and not plat.HAS_KVM:
        return plat.LINUX_ONLY_TIP
    if d.key == "ext_cloud" and not plat.HAS_CLOUD:
        return plat.CLOUD_LINUX_ONLY_TIP
    return ""


class Palette(QListWidget):
    """Device palette.  ``set_rail(True)`` (L10 compact) shows icons only in a 56 px rail."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setDragEnabled(True)
        self.setIconSize(QSize(40, 40))
        self.setSpacing(2)
        self.setUniformItemSizes(False)
        self.setSelectionMode(QListWidget.SingleSelection)
        self.setVerticalScrollMode(QListWidget.ScrollPerPixel)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setFrameShape(QFrame.NoFrame)
        self.setObjectName("palette")
        self.setWordWrap(True)                         # no ellipsis on long device titles
        self.setTextElideMode(Qt.ElideNone)
        self.rail = False
        self._populate()

    def _section(self, text):
        it = QListWidgetItem(text.upper())
        it.setFlags(Qt.NoItemFlags)
        it.setFont(T.type("caption", upper=True))
        it.setForeground(T.tokens().N(11))
        it.setSizeHint(QSize(10, 30))
        it.setData(Qt.UserRole + 1, text.upper())
        self.addItem(it)

    def refresh_icons(self):
        for i in range(self.count()):
            it = self.item(i)
            if not it.data(Qt.UserRole):
                it.setForeground(T.tokens().N(11))
                it.setFont(T.type("caption", upper=True))

    def _populate(self):
        self._section("Cisco CML routers / hosts")
        for d in DEVICE_TYPES:
            if d.backend == "cml" and d.vendor in ("cisco", "linux") and d.shape not in ("switch", "hub"):
                self._add(d)
        self._section("Switches")
        for d in DEVICE_TYPES:
            if d.backend == "cml" and d.shape in ("switch", "hub"):
                self._add(d)
        self._section("Connectors")
        for d in DEVICE_TYPES:
            if d.backend == "cml" and d.vendor == "generic" and d.shape not in ("switch", "hub"):
                self._add(d)
        self._section("Host KVM (outside CML)" + (" - Linux only" if not plat.HAS_KVM else ""))
        for d in DEVICE_TYPES:
            if d.backend == "kvm":
                self._add(d)

    def _add(self, d):
        title = d.title
        two = len(title) > 24 and " (" in title           # long titles: second line instead of an ellipsis
        if two:
            head, rest = title.split(" (", 1)
            title = f"{head}\n({rest}"
        it = QListWidgetItem(QIcon(device_pixmap(d, 40)), title)
        it.setData(Qt.UserRole, d.key)
        it.setData(Qt.UserRole + 1, title)
        it.setToolTip(f"<b>{d.title}</b><br>{d.note}<br><i>Drag onto the canvas</i>")
        it.setSizeHint(QSize(10, self._row_h(title)))
        why = unavailable_reason(d)
        if why:                                      # LabDesk for Windows: Linux-only device, greyed out
            it.setFlags(it.flags() & ~(Qt.ItemIsEnabled | Qt.ItemIsDragEnabled | Qt.ItemIsSelectable))
            it.setToolTip(f"<b>{d.title}</b><br>{why}")
            it.setData(Qt.UserRole + 2, why)
        self.addItem(it)

    @staticmethod
    def _row_h(title: str) -> int:
        """48 px for one line, 56 for two, 70 when the wrapped second line needs a third."""
        if "\n" not in title:
            return 48
        return 70 if len(title.split("\n", 1)[1]) > 24 else 56

    def set_rail(self, on: bool) -> None:
        if on == self.rail:
            return
        self.rail = on
        self.setIconSize(QSize(32, 32) if on else QSize(40, 40))
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff if on else Qt.ScrollBarAsNeeded)
        for i in range(self.count()):
            it = self.item(i)
            full = it.data(Qt.UserRole + 1) or ""
            if not it.data(Qt.UserRole):                     # section header -> thin divider in the rail
                it.setText("" if on else full)
                it.setSizeHint(QSize(10, 10 if on else 30))
            else:
                it.setText("" if on else full)
                it.setSizeHint(QSize(44, 44) if on else QSize(10, self._row_h(full)))

    def startDrag(self, actions):
        it = self.currentItem()
        if not it or not it.data(Qt.UserRole) or not (it.flags() & Qt.ItemIsEnabled):
            return
        mime = QMimeData()
        mime.setData(MIME, it.data(Qt.UserRole).encode())
        drag = QDrag(self)
        drag.setMimeData(mime)
        drag.setPixmap(it.icon().pixmap(48, 48))
        drag.exec(Qt.CopyAction)


class _PaletteDock(QWidget):
    """Palette + (compact only) the rail's top toggle button."""

    def __init__(self, palette: Palette, parent=None):
        super().__init__(parent)
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)
        self.toggle = QToolButton()
        self.toggle.setObjectName("railtoggle")
        self.toggle.setAutoRaise(True)
        self.toggle.setIconSize(QSize(16, 16))
        self.toggle.setFixedHeight(32)
        self.toggle.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.toggle.setVisible(False)
        v.addWidget(self.toggle)
        v.addWidget(palette, 1)


class MainWindow(QMainWindow):
    def __init__(self, controller: LabController | None = None, launcher=None):
        super().__init__()
        self.ctl = controller or LabController()
        self.launcher = launcher or launchers.default_launcher()
        self.runner = TaskRunner(self)
        self.view_data: LabData | None = None
        self._polling = False
        self._pending_moves: dict[str, float] = {}
        self._cml_ok: bool | None = None
        self._cml_version = ""
        self._last_states: dict[str, str] = {}
        self._busy_ops = 0
        self._ev_seq = 0
        self._verified_at: dict[str, float] = {}
        self._watch: dict = {}                    # EtherChannel apply: member set -> (t0, name, what, forms)
        self._closing = False
        self._notice_shown = False
        self._last_poll_t: float | None = None
        self._budget: tuple[int, int] | None = None
        self._budget_warn = False
        self._compact = False
        self._rail_open = False
        self.cloud_health = None                  # cloud.CloudHealth (only while the lab has an Internet cloud)
        self._cloud_checking = False
        self.settings = app_settings()

        self.setWindowTitle("LabDesk")
        self.setWindowIcon(QIcon(str(config.ICON_PATH)))
        self.resize(1360, 860)

        # one shared 1 s timer: inspector 'updated N s ago', status bar 'Last poll N s ago'
        self.ticker = QTimer(self)
        self.ticker.setInterval(1000)
        self.ticker.timeout.connect(self._tick)
        self.ticker.start()

        self.layers = Layers(self)
        self.scene = LabScene(self)
        self.scene.ctx.layers = self.layers.state()
        self.scene.ctx.telemetry_on = self.ctl.telemetry is not None
        self.view = LabView(self.scene)
        self.setCentralWidget(self.view)
        self.legend = Legend(self.view.viewport(), self.layers)
        self.view.legend = self.legend
        self.infobar = InfoBar(self.view.viewport())
        self.infobar.dismissed.connect(self._probe_notice_ack)
        self.notifybar = InfoBar(self.view.viewport())            # transient warnings / errors
        self.notifybar.setObjectName("notifybar")
        self.empty = EmptyStateCard(self.view.viewport())
        self.empty.open_clicked.connect(self.open_lab_dialog)
        self.empty.template_clicked.connect(lambda: self.open_lab_dialog("templates"))
        self.empty.new_clicked.connect(self.new_lab)
        self.empty.start_cml_clicked.connect(self.start_cml_vm)
        self.empty.lab_chosen.connect(self.open_lab_id)
        self.layers.changed.connect(self._layer_changed)
        self.layers.vlan_changed.connect(lambda _v: self._layer_changed("vlan", self.layers.on("vlan")))
        self._build_docks()
        self._build_toolbar()
        self._build_statusbar()
        self._connect_view()
        self._apply_style()
        self.compactor = CompactController(self, self.settings)
        self.compactor.compactChanged.connect(self._set_compact)
        self._restore_layout(False)

        # probe threads -> GUI: an early (debounced) poll when a burst finished
        self._bridge = _Bridge(self)
        self._early = QTimer(self)
        self._early.setSingleShot(True)
        self._early.setInterval(400)
        self._early.timeout.connect(self.poll)
        self._bridge.changed.connect(lambda: self._early.isActive() or self._early.start())
        if self.ctl.telemetry is not None:
            self.ctl.telemetry.on_change = self._bridge.changed.emit
            self.ctl.telemetry.set_traffic_layer(self.layers.on("traffic"))
            if str(self.layers.settings.value("notices/probe_output", False)).lower() in ("true", "1"):
                self._notice_shown = True
                self.ctl.telemetry.acknowledge_probe_notice()

        self.timer = QTimer(self)
        self.timer.setInterval(config.POLL_INTERVAL_MS)
        self.timer.timeout.connect(self.poll)
        self.timer.start()
        self.ram_timer = QTimer(self)                 # V8: 'Host RAM free' - only while KVM devices exist
        self.ram_timer.setInterval(10_000)
        self.ram_timer.timeout.connect(self._update_ram)
        self.cloud_timer = QTimer(self)               # Internet cloud health - only while the lab has one
        self.cloud_timer.setInterval(cloud.HEALTH_INTERVAL_MS)
        self.cloud_timer.timeout.connect(self.check_cloud)
        self.log("LabDesk started")
        if not self.ctl.lab_id:
            self._show_empty()
            self._sync_lab_actions(False)
        self.inspector.show_none()

    # ------------------------------------------------------------ UI build
    def _apply_style(self):
        """Theme (L1/S1): the app-wide ThemeController owns palette + QSS + tokens; this window only
        re-reads the tokens whenever it re-applies (system scheme switch, View ▸ Theme)."""
        from . import themectl
        self.theme = themectl.controller() or themectl.install()
        if not getattr(self, "_theme_hooked", False):
            self.theme.changed.connect(self._theme_changed)
            self._theme_hooked = True
        self._theme_changed()

    def _theme_changed(self):
        tk = T.tokens()
        self.scene.ctx.tokens = tk
        if hasattr(self.scene, "theme_changed"):
            self.scene.theme_changed()
        self.scene.update()
        self.view.viewport().update()
        if hasattr(self, "legend"):
            self.legend.rebuild()
        if hasattr(self, "theme_actions"):
            for m, a in self.theme_actions.items():
                a.setChecked(m == self.theme.mode)
        for d in (getattr(self, "palette_list", None),):
            if d is not None and hasattr(d, "refresh_icons"):
                d.refresh_icons()
        for t in getattr(self, "dock_titles", {}).values():
            t.theme_changed()
            t.update()
        if hasattr(self, "logview"):
            self.logview.theme_changed()
        if hasattr(self, "empty"):
            self.empty.theme_changed()
        if hasattr(self, "lab_name_btn"):
            self.lab_name_btn.setFont(T.type("label"))
            self._style_wipe()
        if hasattr(self, "rail_dock"):
            self._sync_rail_toggle()
        for w in self.findChildren(QWidget):
            if isinstance(w, (StatusPill, Spinner)):
                w.update()

    def _dock(self, title: str, name: str, widget: QWidget) -> QDockWidget:
        d = QDockWidget(title, self)
        d.setObjectName(name)
        d.setWidget(widget)
        d.setFeatures(QDockWidget.DockWidgetMovable)
        tw = DockTitle(title)
        d.setTitleBarWidget(tw)
        self.dock_titles[name] = tw
        return d

    def _build_docks(self):
        self.dock_titles: dict[str, DockTitle] = {}
        self.palette_list = Palette()
        self.palette = self.palette_list               # (old attribute name)
        self.palette_box = _PaletteDock(self.palette_list)
        self.palette_box.toggle.clicked.connect(self._toggle_rail)
        d = self._dock("Devices", "devices", self.palette_box)
        d.setMinimumWidth(210)
        self.addDockWidget(Qt.LeftDockWidgetArea, d)
        self.rail_dock = d

        self.inspector = Inspector(self)
        sc = QScrollArea()                         # the dock width never follows long text
        sc.setWidgetResizable(True)
        sc.setFrameShape(QFrame.NoFrame)
        sc.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        sc.setWidget(self.inspector)
        d2 = self._dock("Inspector", "inspector", sc)
        d2.setMinimumWidth(INSPECTOR_MIN)
        self.inspector_dock = d2
        self.addDockWidget(Qt.RightDockWidgetArea, d2)

        self.logview = EventLog()
        self.logview.unread_changed.connect(self._unread_changed)
        d3 = self._dock("Event log", "log", self.logview)
        self.log_dock = d3
        self.addDockWidget(Qt.BottomDockWidgetArea, d3)
        self.resizeDocks([d3], [150], Qt.Vertical)
        self.resizeDocks([d2], [INSPECTOR_W], Qt.Horizontal)
        d3.visibilityChanged.connect(lambda on: on and self.logview.mark_read() if self._compact else None)

    def _act(self, text, slot, shortcut=None, tip=None, checkable=False, icon=None):
        a = QAction(text, self)
        if icon:
            a.setIcon(icons.icon(icon))
        if shortcut:
            a.setShortcut(QKeySequence(shortcut))
        tip = tip or text.rstrip("…")
        if shortcut:
            tip += f" ({QKeySequence(shortcut).toString(QKeySequence.NativeText)})"
        a.setToolTip(tip)
        a.setCheckable(checkable)
        a.triggered.connect(slot)
        return a

    def _menu_button(self, text, icon, menu, tip: str = "") -> QToolButton:
        b = QToolButton()
        b.setText(text)
        b.setPopupMode(QToolButton.InstantPopup)
        b.setMenu(menu)
        b.setIcon(icons.icon(icon))
        b.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        b.setToolTip(tip or text)
        b.setObjectName("menubutton")
        return b

    def _title_block(self) -> QWidget:
        """Left of the toolbar: app icon · lab name (the click target: Open lab…) · lab StatusPill."""
        w = QWidget()
        w.setObjectName("titleblock")
        h = QHBoxLayout(w)
        h.setContentsMargins(4, 0, 10, 0)
        h.setSpacing(6)
        ic = QLabel()
        ic.setPixmap(QIcon(str(config.ICON_PATH)).pixmap(22, 22))
        h.addWidget(ic)
        self.lab_name_btn = QToolButton()
        self.lab_name_btn.setObjectName("labname")
        self.lab_name_btn.setText("No lab open")
        self.lab_name_btn.setFont(T.type("label"))
        self.lab_name_btn.setToolTip("Open lab… (Ctrl+O)")
        self.lab_name_btn.setCursor(Qt.PointingHandCursor)
        self.lab_name_btn.setAutoRaise(True)
        self.lab_name_btn.clicked.connect(self.open_lab_dialog)
        h.addWidget(self.lab_name_btn)
        self.lab_pill = StatusPill("", "neutral", "stopped", height=20)
        self.lab_pill.setVisible(False)
        h.addWidget(self.lab_pill)
        return w

    def _build_toolbar(self):
        tb = QToolBar("Main")
        tb.setObjectName("main")
        tb.setMovable(False)
        tb.setFloatable(False)
        tb.setIconSize(QSize(icons.SIZES["toolbar"], icons.SIZES["toolbar"]))      # S6: 18 px
        tb.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.addToolBar(tb)
        self.toolbar = tb
        tb.addWidget(self._title_block())
        tb.addSeparator()
        self.a_new = self._act("New lab", self.new_lab, "Ctrl+N", "Create an empty lab", icon="document-new")
        self.a_open = self._act("Open…", self.open_lab_dialog, "Ctrl+O", "Open a CML lab or import a template",
                                icon="document-open")
        self.a_start = self._act("Start all", self.start_all, None, "Start every node of the lab",
                                 icon="media-playback-start")
        self.a_stop = self._act("Stop all", self.stop_all, None, "Stop every node of the lab",
                                icon="media-playback-stop")
        self.a_wipe = self._act("Wipe all", self.wipe_all, None, "Wipe all node disks (startup configs re-apply)",
                                icon="edit-clear-all")
        self.a_link = self._act("Link mode", self.toggle_link_mode, "L",
                                "Drag from one device to another to link them (or Shift-drag any time)",
                                checkable=True, icon="network-connect")
        self.a_fit = self._act("Fit", lambda: self.view.fit_all(), "F", "Fit topology in view",
                               icon="zoom-fit-best")
        self.a_refresh = self._act("Refresh", self.refresh, "F5",
                                   "Re-read the lab now: probes the selected router / switch (all of them when none "
                                   "is selected); a node whose console is open is not probed - its console log is "
                                   "re-read instead", icon="view-refresh")
        # Lab group
        for a in (self.a_new, self.a_open):
            tb.addAction(a)
        m = QMenu(self)
        m.addAction(self._act("Export YAML…", self.export_yaml, icon="document-save-as"))
        self.a_connection = self._act("CML connection…", self.edit_connection, None,
                                      "CML address, username and password", icon="network-connect")
        m.addAction(self.a_connection)
        da = self._act("Delete lab…", self.delete_lab)
        da.setIcon(icons.icon("edit-delete", "danger"))
        m.addAction(da)
        self.lab_menu_btn = self._menu_button("Lab", "application-menu", m, "Export or delete the lab")
        tb.addWidget(self.lab_menu_btn)
        tb.addSeparator()
        # Run group
        for a in (self.a_start, self.a_stop):
            tb.addAction(a)
        tb.addSeparator()
        tb.addAction(self.a_wipe)
        tb.addSeparator()
        # Canvas group
        tb.addAction(self.a_link)
        tb.addAction(self.a_fit)
        tb.addAction(self.a_refresh)
        tb.addSeparator()
        # View ▸ Layers (V4/V9): the shared, persisted QActions
        vm = QMenu(self)
        lm = vm.addMenu("Layers")
        self._fill_layers_menu(lm)
        self.a_legend = self._act("Legend", self.toggle_legend, None, "Show / hide the legend (?)",
                                  checkable=True)
        self.a_legend.setChecked(True)
        vm.addAction(self.a_legend)
        self.a_log = self._act("Event log", self.toggle_log, "Ctrl+J", "Show / hide the event log", checkable=True)
        self.a_log.setChecked(True)
        vm.addAction(self.a_log)
        self.a_show_pw = self._act("Show passwords on hover", self._set_show_passwords, None,
                                   "Hover card: show stored and plain-text device passwords (off: ••••••)",
                                   checkable=True)
        self.a_show_pw.setChecked(as_bool(self.settings.value("ui/hover_show_passwords", True), True))
        vm.addAction(self.a_show_pw)
        self.a_neon = self._act("Neon green console", self._set_console_neon, None,
                                "Device consoles: neon green text on black (off: your normal Konsole colours). "
                                "Applies to the next console you open",
                                checkable=True)
        self.a_neon.setChecked(as_bool(self.settings.value("console/neon", True), True))
        self.a_neon.setVisible(hasattr(self.launcher, "neon"))
        if hasattr(self.launcher, "neon"):
            self.launcher.neon = self.a_neon.isChecked()
        vm.addAction(self.a_neon)
        vm.addSeparator()
        tm = vm.addMenu("Theme")
        from .themectl import MODES
        grp = QActionGroup(tm)
        grp.setExclusive(True)
        self.theme_actions = {}
        for mode, title in MODES:
            ta = tm.addAction(title)
            ta.setCheckable(True)
            grp.addAction(ta)
            ta.triggered.connect(lambda _c=False, m=mode: self.theme.set_mode(m))
            self.theme_actions[mode] = ta
        lay = vm.addMenu("Layout")
        lgrp = QActionGroup(lay)
        lgrp.setExclusive(True)
        self.layout_actions = {}
        cur = str(self.settings.value("ui/layout", "auto"))
        for mode, title in LAYOUT_MODES:
            la = lay.addAction(title + (" (narrow screens)" if mode == "auto" else ""))
            la.setCheckable(True)
            la.setChecked(mode == cur)
            lgrp.addAction(la)
            la.triggered.connect(lambda _c=False, m=mode: self.compactor.set_mode(m))
            self.layout_actions[mode] = la
        vm.addSeparator()
        vm.addAction(self._act("Fit view", lambda: self.view.fit_all()))
        self.view_menu_btn = self._menu_button("View", "view-visible", vm, "Layers, legend, theme and layout")
        tb.addWidget(self.view_menu_btn)
        self._view_menu = vm
        # Simulate (the 'simulate the connection between routers' asks)
        self.sim_menu = QMenu(self)
        self.sim_menu.aboutToShow.connect(self._fill_sim_menu)
        self.sim_menu_btn = self._menu_button("Simulate", "network-disconnect", self.sim_menu,
                                              "Animate packets, traceroute, ping, Verify Internet")
        tb.addWidget(self.sim_menu_btn)
        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        tb.addWidget(spacer)
        # right: one StatusPill cluster - Probing · CML n/5 · legend/help
        cluster = QWidget()
        ch = QHBoxLayout(cluster)
        ch.setContentsMargins(0, 0, 4, 0)
        ch.setSpacing(6)
        self.a_probing = QAction("Probing: on", self)          # the shared state (menus, telemetry)
        self.a_probing.setCheckable(True)
        self.a_probing.setChecked(True)
        self.a_probing.toggled.connect(self.set_probing)
        self.probing_pill = StatusPill("Probing", "neutral", "up", interactive=True, checkable=True)
        self.probing_pill.setObjectName("probing")
        self.probing_pill.setChecked(True)
        self.probing_pill.setToolTip("Read router state over the serial consoles (read-only show commands).\n"
                                     "Off: only what the CML API knows (node / link up, cable pulled).")
        self.probing_pill.toggled.connect(self.a_probing.setChecked)
        self.a_probing.toggled.connect(self._sync_probing_pill)
        self.probing_pill.setVisible(self.ctl.telemetry is not None)
        ch.addWidget(self.probing_pill)
        self.counter = StatusPill("CML –/5", "neutral", None, interactive=True, tabular=True)
        self.counter.setObjectName("cmlcounter")
        self.counter.setFocusPolicy(Qt.NoFocus)
        self.counter.setCursor(Qt.ArrowCursor)
        self.counter.setToolTip(CML_TIP)
        ch.addWidget(self.counter)
        hm = QMenu(self)
        hm.addAction(self.a_legend)
        hm.addSeparator()
        hm.addAction(self._act("About LabDesk", self.show_about, icon="help-about"))
        hm.aboutToShow.connect(lambda: None)
        self.help_pill = StatusPill("Legend", "neutral", None, interactive=True, checkable=True)
        self.help_pill.setObjectName("legendpill")
        self.help_pill.setChecked(True)
        self.help_pill.setMenu(hm)
        self.help_pill.setToolTip("Legend (?) · the arrow opens Help")
        self.help_pill.toggled.connect(self.a_legend.setChecked)
        self.a_legend.toggled.connect(self.help_pill.setChecked)
        ch.addWidget(self.help_pill)
        tb.addWidget(cluster)
        self._sync_probing_pill(True)
        # Start all hover: warn the budget pill when starting would exceed the CML-Free limit
        bs = tb.widgetForAction(self.a_start)
        if bs is not None:
            bs.installEventFilter(self)
            self._start_btn = bs
        self._style_wipe()
        for b in tb.findChildren(QToolButton):          # L2: 32 px tool buttons, 36-40 px toolbar
            if b.parent() is tb or b.objectName() == "menubutton":
                b.setFixedHeight(32)
        self.lab_name_btn.setFixedHeight(28)
        tb.layout().setContentsMargins(0, 0, 0, 0)
        tb.layout().setSpacing(2)
        tb.setFixedHeight(40)
        # canvas shortcuts as QActions (so the Tips card can list them)
        sc = QAction("Legend", self.view)
        sc.setShortcut(QKeySequence("?"))
        sc.setShortcutContext(Qt.WidgetWithChildrenShortcut)
        sc.triggered.connect(lambda: self.a_legend.setChecked(not self.a_legend.isChecked()))
        self.view.addAction(sc)
        self.a_legend_key = sc
        self.a_console = QAction("Console", self.view)
        self.a_console.setShortcuts([QKeySequence(Qt.Key_Return), QKeySequence(Qt.Key_Enter)])
        self.a_console.setShortcutContext(Qt.WidgetWithChildrenShortcut)
        self.a_console.triggered.connect(lambda: self.selected_key() and self.open_console(self.selected_key()))
        self.view.addAction(self.a_console)
        self.a_rename = QAction("Rename", self.view)
        self.a_rename.setShortcut(QKeySequence(Qt.Key_F2))
        self.a_rename.setShortcutContext(Qt.WidgetWithChildrenShortcut)
        self.a_rename.triggered.connect(lambda: self.selected_key() and self.rename_node(self.selected_key()))
        self.view.addAction(self.a_rename)
        self.a_delete = QAction("Delete", self.view)
        self.a_delete.setShortcut(QKeySequence(QKeySequence.Delete))
        self.a_delete.setShortcutContext(Qt.WidgetWithChildrenShortcut)
        self.a_delete.triggered.connect(self.delete_selected)
        self.view.addAction(self.a_delete)
        self.addAction(self._act("Zoom in", lambda: self.view.zoom(1.15), "Ctrl++"))
        self.addAction(self._act("Zoom out", lambda: self.view.zoom(1 / 1.15), "Ctrl+-"))
        self.addAction(self.a_log)

    def tip_actions(self) -> list:
        """(QAction, text) for the inspector Tips card - only actions that really carry a shortcut."""
        return [(self.a_open, "Open a lab"), (self.a_new, "New empty lab"),
                (self.a_console, "Console of the selected device"), (self.a_link, "Link mode"),
                (self.a_fit, "Fit the topology"), (self.a_refresh, "Refresh (probe now)"),
                (self.a_rename, "Rename the selected device"), (self.a_delete, "Delete the selection"),
                (self.a_legend_key, "Legend"), (self.a_log, "Event log")]

    def _style_wipe(self):
        b = self.toolbar.widgetForAction(self.a_wipe) if hasattr(self, "toolbar") else None
        if b is not None:
            b.setStyleSheet(f"QToolButton {{ color: {T.status_text('down').name()}; }}"
                            f" QToolButton:disabled {{ color: {T.alpha(T.status_text('down'), 0.45).name(QColor.HexArgb)}; }}")
            self.a_wipe.setIcon(icons.icon("edit-clear-all", "danger"))

    def _sync_probing_pill(self, on: bool):
        self.probing_pill.blockSignals(True)
        self.probing_pill.setChecked(on)
        self.probing_pill.blockSignals(False)
        self.probing_pill.set_state("Probing" if on else "Probing off", "up" if on else "neutral",
                                    "up" if on else "neutral")

    def _build_statusbar(self):
        sb = self.statusBar()
        sb.setSizeGripEnabled(False)
        self.lab_label = GlyphLabel("No lab open")
        self.poll_label = QLabel("")
        self.poll_label.setFont(T.tabular("small"))
        self.busy_spin = Spinner(12)
        self.busy_spin.set_running(False)
        self.busy_label = QLabel("")
        self.ram_label = QLabel("")
        self.ram_label.setVisible(False)
        self.err_badge = StatusPill("", "down", "down", height=18, interactive=True)
        self.err_badge.setToolTip("Errors in the event log - click to show it (Ctrl+J)")
        self.err_badge.clicked.connect(lambda: self.toggle_log(True))
        self.err_badge.setVisible(False)
        self.start_cml_btn = QPushButton("Start CML VM")
        self.start_cml_btn.setProperty("role", "primary")
        self.start_cml_btn.setVisible(False)
        self.start_cml_btn.clicked.connect(self.start_cml_vm)
        self.cml_label = GlyphLabel("CML: checking…", "neutral")
        for w in (self.lab_label, self.poll_label, self.busy_label, self.ram_label, self.cml_label):
            w.setFont(T.type("small") if w is not self.poll_label else T.tabular("small"))
        sb.addWidget(self.lab_label)
        self.poll_sep = _sep()
        self.poll_sep.setVisible(False)
        sb.addWidget(self.poll_sep)
        sb.addWidget(self.poll_label)
        sb.addWidget(self.busy_spin)
        sb.addWidget(self.busy_label, 1)
        sb.addPermanentWidget(self.err_badge)
        sb.addPermanentWidget(self.ram_label)
        sb.addPermanentWidget(self.start_cml_btn)
        sb.addPermanentWidget(self.cml_label)
        self.runner.busy_changed.connect(self._on_busy)

    def _on_busy(self, n):
        ops = max(0, n - (1 if self._polling else 0))
        self._busy_ops = ops
        self.busy_label.setText(f"working… ({ops})" if ops else "")
        self.busy_spin.set_running(bool(ops))
        self._update_title()

    def _update_title(self):
        t = f"{self.view_data.title} — LabDesk" if self.view_data else "LabDesk"
        self.setWindowTitle(("• " if self._busy_ops else "") + t)

    def _tick(self):
        now = time.time()
        if self._last_poll_t is not None:
            self.poll_label.setText(f"Last poll {T.fmt_age(now - self._last_poll_t)}")
            self.poll_sep.setVisible(True)
        if self.inspector_dock.isVisible():
            self.inspector.tick(now)

    def eventFilter(self, obj, ev):
        if obj is getattr(self, "_start_btn", None):
            if ev.type() == QEvent.Enter:
                self._warn_budget(self._would_exceed())
            elif ev.type() == QEvent.Leave:
                self._warn_budget(False)
        return super().eventFilter(obj, ev)

    def _would_exceed(self, keys=None) -> bool:
        """True when starting ``keys`` (all stopped licence-counting nodes of the lab) would pass the limit."""
        if self._budget is None or not self.view_data:
            return False
        n, lim = self._budget
        pool = [nv for nv in self.view_data.nodes if keys is None or nv.key in keys]
        need = sum(1 for nv in pool if nv.key.startswith("cml:") and nv.state not in ("BOOTED", "STARTED", "QUEUED")
                   and nv.dtype.node_definition not in NON_COUNTING_DEFS)
        return need > 0 and n + need > lim

    def _warn_budget(self, on: bool):
        self._budget_warn = on
        if self._budget is not None and self._cml_ok is not False:
            n, lim = self._budget
            self.counter.set_state(f"CML {n}/{lim} · limit" if on else f"CML {n}/{lim}", "warn" if on else "neutral",
                                   "warn" if on else None)

    def _unread_changed(self, n: int):
        self.dock_titles["log"].set_badge(n, f"{n} unread error{'s' if n != 1 else ''}")
        self.err_badge.set_state(f"{n} error{'s' if n != 1 else ''}")
        self.err_badge.setVisible(n > 0 and self._compact)

    def toggle_log(self, on=None):
        """Ctrl+J / View ▸ Event log / the status-bar error badge."""
        if on is None:
            on = not self.log_dock.isVisible()
        on = bool(on)
        self.log_dock.setVisible(on)
        self.a_log.blockSignals(True)
        self.a_log.setChecked(on)
        self.a_log.blockSignals(False)
        if on:
            self.resizeDocks([self.log_dock], [150], Qt.Vertical)
            self.logview.mark_read()
            self.err_badge.setVisible(False)

    # ------------------------------------------------------------ compact mode (L10)
    def _set_compact(self, on: bool):
        if on == self._compact:
            return
        self._save_layout()
        self._compact = on
        primary = {self.a_start, self.a_stop, self.a_open}
        for a in self.toolbar.actions():
            b = self.toolbar.widgetForAction(a)
            if isinstance(b, QToolButton) and a not in primary and a.text():
                b.setToolButtonStyle(Qt.ToolButtonIconOnly if on else Qt.ToolButtonTextBesideIcon)
        for b in (self.lab_menu_btn, self.view_menu_btn, self.sim_menu_btn):
            b.setToolButtonStyle(Qt.ToolButtonIconOnly if on else Qt.ToolButtonTextBesideIcon)
        self.inspector_dock.setMinimumWidth(INSPECTOR_MIN_COMPACT if on else INSPECTOR_MIN)
        self._rail_open = False
        self._apply_rail()
        self.legend.set_compact(on)
        self.log_dock.setVisible(not on)
        self.a_log.blockSignals(True)
        self.a_log.setChecked(not on)
        self.a_log.blockSignals(False)
        self.err_badge.setVisible(on and self.logview.unread > 0)
        if not self._restore_layout(on):
            self.resizeDocks([self.inspector_dock], [INSPECTOR_MIN_COMPACT if on else INSPECTOR_W], Qt.Horizontal)
        if on:
            self.log_dock.setVisible(False)
        QTimer.singleShot(0, self.legend.reposition)
        if self.view_data is not None:                 # the viewport changed size: keep the topology in view
            QTimer.singleShot(30, self.view.fit_all)

    def _apply_rail(self):
        d, pal = self.rail_dock, self.palette_list
        rail = self._compact and not self._rail_open
        self.palette_box.toggle.setVisible(self._compact)
        pal.set_rail(rail)
        if rail:
            d.setTitleBarWidget(QWidget())               # zero-height: the dock minimum no longer includes it
            d.setMinimumWidth(RAIL_W)
            d.setMaximumWidth(RAIL_W)
        else:
            d.setTitleBarWidget(self.dock_titles["devices"])
            d.setMaximumWidth(16777215)
            d.setMinimumWidth(210)
            if self._compact:
                self.resizeDocks([d], [230], Qt.Horizontal)
        self._sync_rail_toggle()

    def _sync_rail_toggle(self):
        t = self.palette_box.toggle
        rail = self._compact and not self._rail_open
        t.setIcon(icons.icon("arrow-right" if rail else "arrow-left", "muted"))
        t.setText("" if rail else "Collapse")
        t.setToolButtonStyle(Qt.ToolButtonIconOnly if rail else Qt.ToolButtonTextBesideIcon)
        t.setToolTip("Show the device names" if rail else "Collapse to the icon rail")

    def _toggle_rail(self):
        self._rail_open = not self._rail_open
        self._apply_rail()

    def _layout_key(self, compact: bool) -> str:
        return f"layout/{'compact' if compact else 'full'}"

    def _save_layout(self):
        try:
            self.settings.setValue(self._layout_key(self._compact), self.saveState(STATE_VERSION))
            self.settings.setValue("layout/version", STATE_VERSION)
        except Exception:  # noqa: BLE001
            pass

    def _restore_layout(self, compact: bool) -> bool:
        try:
            if int(self.settings.value("layout/version", 0) or 0) != STATE_VERSION:
                return False
            st = self.settings.value(self._layout_key(compact))
            return bool(st) and self.restoreState(st, STATE_VERSION)
        except Exception:  # noqa: BLE001
            return False

    def _connect_view(self):
        v = self.view
        v.device_dropped.connect(self.on_device_dropped)
        v.link_requested.connect(self.on_link_requested)
        v.node_activated.connect(self.open_console)
        v.node_context.connect(self.node_menu)
        v.link_context.connect(self.link_menu)
        v.bundle_context.connect(self.bundle_menu)
        v.canvas_context.connect(self.canvas_menu)
        v.delete_pressed.connect(self.delete_selected)
        v.link_mode_off.connect(lambda: self.a_link.setChecked(False))
        v.rename_pressed.connect(lambda: self.selected_key() and self.rename_node(self.selected_key()))
        self.scene.selectionChanged.connect(self._selection_changed)
        v.card_provider = self._hover_card_for

    def _fill_layers_menu(self, lm: QMenu) -> None:
        for a in self.layers.actions.values():
            lm.addAction(a)
        lm.addSeparator()
        vm = lm.addMenu("Highlight VLAN")
        vm.setToolTipsVisible(True)
        vm.aboutToShow.connect(lambda m=vm: self._fill_vlan_menu(m))

    def lab_vlans(self) -> dict:
        from .scene_model import lab_vlans
        return lab_vlans(self.view_data.telemetry if self.view_data else None)

    def _fill_vlan_menu(self, m: QMenu) -> None:
        """VLAN picker for the 'VLAN colouring' layer (VLANs learned from the managed switches)."""
        m.clear()
        group = QActionGroup(m)
        vlans = self.lab_vlans()
        on = self.layers.on("vlan")
        a = m.addAction("None (layer off)")
        a.setCheckable(True)
        a.setChecked(not on or self.layers.vlan is None)
        a.triggered.connect(lambda: (self.layers.set_vlan(None), self.layers.set("vlan", False)))
        group.addAction(a)
        if not vlans:
            x = m.addAction("no managed switch data yet")
            x.setEnabled(False)
        for vid, name in vlans.items():
            a = m.addAction(self._vlan_icon(vid), f"VLAN {vid}  {name}")
            a.setCheckable(True)
            a.setChecked(on and self.layers.vlan == vid)
            a.triggered.connect(lambda _=False, v=vid: self.layers.set_vlan(v))
            group.addAction(a)

    def _vlan_icon(self, vid: int) -> QIcon:
        from PySide6.QtGui import QPainter, QPixmap
        pm = QPixmap(14, 14)
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing)
        p.setBrush(QColor(self.scene.ctx.vlan_hues.get(vid, T.NEUTRAL)))
        p.setPen(Qt.NoPen)
        p.drawRoundedRect(1, 3, 12, 8, 3, 3)
        p.end()
        return QIcon(pm)

    def toggle_legend(self, on: bool):
        self.legend.setVisible(on)
        if on:
            self.legend.reposition()

    def _layer_changed(self, key: str, on: bool):
        self.scene.ctx.layers = self.layers.state()
        self.scene.invalidate_layout()
        self.scene.ctx.vlan_hl = self.layers.vlan
        if key == "vlan" and on and self.layers.vlan is None:
            vl = [v for v in self.lab_vlans() if v != 1]
            if vl:
                self.layers.vlan = vl[0]                   # first user VLAN by default
                self.scene.ctx.vlan_hl = vl[0]
        if hasattr(self, "legend"):
            self.legend.set_vlans(self.lab_vlans(), self.scene.ctx.vlan_hues)
        if key == "traffic" and self.ctl.telemetry is not None:
            self.ctl.telemetry.set_traffic_layer(on)
        self.scene.update()

    # -------------------------------------------------------------- logging
    def log(self, msg: str, level: str = "info", tag: str | None = None, hue: str | None = None,
            ts: str | None = None):
        """Append to the event log (L5).  The status bar is not used for log lines any more; errors also
        surface through the canvas notification bar."""
        self.logview.append(msg, level, tag, hue, ts)

    def notify(self, text: str, level: str = "info", timeout_ms: int = 6000):
        """Transient message on top of the canvas (below the probe notice when that is shown)."""
        self.notifybar.offset_y = 12 + (self.infobar.height() + 8 if self.infobar.isVisible() else 0)
        icon_text = html.escape(text)
        self.notifybar.show_text(icon_text, level, timeout_ms)

    def on_error(self, what: str, popup: bool = False):
        def handler(exc):
            text = f"{what}: {exc}"
            self.log(text, "error")
            if popup:
                info(self, "LabDesk", f"Could not {what}.", str(exc))
            else:
                self.notify(text, "error", 8000)
            self.poll()
        return handler

    # ----------------------------------------------------------------- poll
    def poll(self):
        if self._polling or self._closing:
            return
        self._polling = True
        if self.ctl.lab_id:
            self.runner.submit(self.ctl.poll, on_done=self._poll_done, on_error=self._poll_failed)
        else:
            self.runner.submit(self.ctl.client.system_information, on_done=self._sysinfo_done,
                               on_error=self._poll_failed)

    def _sysinfo_done(self, info):
        self._polling = False
        self._last_poll_t = time.time()
        self._set_cml_ok(True, info.get("version", ""))

    def _poll_failed(self, exc):
        self._polling = False
        if isinstance(exc, CMLUnreachable):
            self._set_cml_ok(False)
        elif isinstance(exc, CMLError) and exc.status == 404 and self.ctl.lab_id:
            self.log("The open lab no longer exists on the controller", "warn")
            self.ctl.lab_id = None
            self.clear_canvas()
        else:
            self.log(f"poll failed: {exc}", "error")

    def _set_cml_ok(self, ok: bool, version: str = ""):
        if ok != self._cml_ok:
            if ok:
                self.log(f"CML controller reachable {version}".strip(), "ok")
            else:
                self.log("CML controller unreachable - is the 'cml' VM running?", "error")
        if version:
            self._cml_version = version.split(" ")[0]
        first = self._cml_ok is None
        self._cml_ok = ok
        ver = f"CML {self._cml_version}" if self._cml_version else "CML"
        if ok:
            self.cml_label.setText(f"{ver} · {config.CML_HOST}")
            self.cml_label.set_glyph("up", "up")
            self.cml_label.setToolTip("CML controller online")
            if self._budget is not None:
                self._warn_budget(self._budget_warn)
        else:
            self.cml_label.setText(f"CML offline · {config.CML_HOST}")
            self.cml_label.set_glyph("down", "down")
            self.cml_label.setToolTip("The CML controller does not answer")
            self.counter.set_state("CML offline", "down", "down")
        self.start_cml_btn.setVisible(not ok)
        if not self.ctl.lab_id:
            if (not ok) != self.empty.offline or first:
                self.empty.set_offline(not ok, config.CML_HOST)
                if ok:
                    self._load_recent()
            self._show_empty()

    def _poll_done(self, data: LabData):
        self._polling = False
        self._last_poll_t = time.time()
        if not self._cml_version and not getattr(self, "_ver_asked", False):
            self._ver_asked = True                     # once: the CML version for the status bar / About
            self.runner.submit(self.ctl.client.system_information,
                               on_done=lambda i: self._set_cml_ok(True, i.get("version", "")),
                               on_error=lambda e: None)
        self._set_cml_ok(True)
        if data.lab_id != self.ctl.lab_id or self._closing:
            return
        self.apply_view(data)
        seeded = self.ctl.pop_first_login_seeded() if hasattr(self.ctl, "pop_first_login_seeded") else []
        if seeded:
            self.log(f"first login saved for {', '.join(seeded)} (device-creds, mode 0600; hover a device to "
                     "see it)", "ok", tag="LOGIN")
            self.view.refresh_hover_card()

    def clear_canvas(self):
        for b in list(self.scene.bundles.values()):
            self.scene.removeItem(b)
        self.scene.bundles.clear()
        for l in list(self.scene.links.values()):
            l.detach()
            self.scene.removeItem(l)
        self.scene.links.clear()
        for n in list(self.scene.nodes.values()):
            self.scene.removeItem(n)
        self.scene.nodes.clear()
        self.scene.overlay.set_data([], [])
        self.view_data = None
        self.view.lab_open = False
        self._sync_lab_actions(False)
        self.cloud_timer.stop()
        self._last_states.clear()
        self.lab_label.setText("No lab open")
        self.lab_label.set_glyph(None)
        self.lab_name_btn.setText("No lab open")
        self.lab_pill.setVisible(False)
        self._update_title()
        self.inspector.show_none()
        if not self.ctl.lab_id:
            self._show_empty()
            self._load_recent()

    def _sync_lab_actions(self, has_lab: bool):
        """Lab-scoped toolbar actions are disabled while no lab is open."""
        for a in (self.a_start, self.a_stop, self.a_wipe, self.a_link, self.a_fit):
            a.setEnabled(has_lab)
        for b in (self.lab_menu_btn, self.sim_menu_btn):
            b.setEnabled(has_lab)

    def _show_empty(self):
        """L6: the empty-state card while no lab is open (the legend is hidden then)."""
        show = not self.ctl.lab_id and self.view_data is None
        self.empty.setVisible(show)
        if show:
            self.empty.reposition()
            self.empty.raise_()
            QTimer.singleShot(0, self.empty.reposition)

    def _load_recent(self):
        if self._closing or self._cml_ok is False:
            self.empty.set_recent(None)
            return
        self.empty.set_loading()
        self.runner.submit(self.ctl.list_labs, on_done=lambda labs: self.empty.set_recent(labs, self.ctl.lab_id),
                           on_error=lambda e: self.empty.set_recent(None))

    def apply_view(self, data: LabData):
        first = self.view_data is None or self.view_data.lab_id != data.lab_id
        self.view_data = data
        self.view.overlay_text = ""
        self.view.lab_open = True
        self.empty.hide()
        self._sync_lab_actions(True)
        self._update_title()
        self._update_counter(data)
        self.logview.set_node_labels(n.label for n in data.nodes)

        grabber = self.scene.mouseGrabberItem()
        now = time.time()
        seen = set()
        for nv in data.nodes:
            seen.add(nv.key)
            item = self.scene.nodes.get(nv.key)
            if item is None:
                item = NodeItem(nv.key, nv.label, nv.dtype)
                item.moved.connect(self.on_node_moved)
                item.clicked.connect(self._node_clicked)
                item.setFlag(NodeItem.ItemIsMovable, not self.view.link_mode)
                self.scene.addItem(item)
                self.scene.nodes[nv.key] = item
                item.setPos(nv.x, nv.y)
            elif item is not grabber and now - self._pending_moves.get(nv.key, 0) > 8:
                if (item.pos().x(), item.pos().y()) != (nv.x, nv.y):
                    item.setPos(nv.x, nv.y)
            item.set_label(nv.label)
            item.dtype = nv.dtype
            item.set_state(nv.state)
            item.set_view(nv)
            old = self._last_states.get(nv.key)
            if old is not None and old != nv.state:
                self.log(f"{nv.label}: {STATE_TEXT.get(old, old)} → {STATE_TEXT.get(nv.state, nv.state)}", "state")
            self._last_states[nv.key] = nv.state
        for key in list(self.scene.nodes):
            if key not in seen:
                item = self.scene.nodes.pop(key)
                for l in list(item.links):
                    l.detach()
                    self.scene.links.pop(l.key, None)
                    self.scene.removeItem(l)
                self.scene.removeItem(item)
                self._last_states.pop(key, None)
        sd = build_scene_data(data, self._verified_at)
        seen_l = set()
        for lv in data.links:
            a, b = self.scene.nodes.get(lv.a), self.scene.nodes.get(lv.b)
            if not a or not b:
                continue
            seen_l.add(lv.key)
            li = self.scene.links.get(lv.key)
            if li is None or li.a is not a or li.b is not b:
                if li:
                    li.detach()
                    self.scene.removeItem(li)
                li = LinkItem(lv.key, a, b, lv.label_a, lv.label_b, lv.dashed)
                self.scene.addItem(li)
                self.scene.links[lv.key] = li
            li.label_a, li.label_b = lv.label_a, lv.label_b
            fan = sd.fans.get(lv.key, 0.0)
            if fan != li.fan:
                li.fan = fan
                li.adjust()
            li.set_state(lv.state)
            li.set_view(lv)
        for key in list(self.scene.links):
            if key not in seen_l:
                li = self.scene.links.pop(key)
                li.detach()
                self.scene.removeItem(li)
        self._sync_bundles(data)
        ctx = self.scene.ctx
        ctx.labels = {n.key: n.label for n in data.nodes}
        ctx.path_marks = sd.path_marks
        ctx.egress_caption = sd.egress_caption
        ctx.switch_chips = sd.switch_chips
        ctx.fhrp_tint = sd.fhrp_tint
        ctx.vlan_hues, ctx.vlan_names = sd.vlan_hues, sd.vlans
        ctx.vlan_hl = self.layers.vlan
        if self.layers.on("vlan") and ctx.vlan_hl is None and [v for v in sd.vlans if v != 1]:
            ctx.vlan_hl = self.layers.vlan = [v for v in sd.vlans if v != 1][0]
        self.legend.set_vlans(sd.vlans, sd.vlan_hues)
        ctx.telemetry_on = data.telemetry is not None
        self.scene.overlay.set_data(sd.chords, sd.arcs)
        self.scene.invalidate_layout()
        self._apply_telemetry(data)
        has_cloud = any(n.dtype.key == "ext_cloud" for n in data.nodes)
        if has_cloud and not self.cloud_timer.isActive():
            self.cloud_timer.start()
            self.check_cloud()
        elif not has_cloud and self.cloud_timer.isActive():
            self.cloud_timer.stop()
        self.scene.ensure_ticking()
        self.scene.update()
        if first:
            QTimer.singleShot(0, self.view.fit_all)
        self.update_inspector()
        self._update_summary()
        self.view.refresh_hover_card()

    def _apply_telemetry(self, data: LabData):
        tv = data.telemetry
        if tv is None:
            return
        # events -> log (protocol hue + text tag) + 800 ms flash of the affected items (V10)
        evs = self.ctl.telemetry.events_since(self._ev_seq) if self.ctl.telemetry is not None else []
        for e in evs:
            self._ev_seq = max(self._ev_seq, e.seq)
            hue = T.EVENT_HUES.get(e.proto, T.NEUTRAL)
            level = {"ok": "ok", "warn": "warn", "error": "error"}.get(e.severity, "info")
            ts = e.device_ts or time.strftime("%H:%M:%S", time.localtime(e.ts))
            self.log(e.text, level if e.kind != "SYSLOG" else "info", tag=e.kind, hue=hue, ts=ts)
            if e.items:
                self.scene.flash(e.items, hue)
            if e.kind == "SUBNET":
                self.scene.pulse_subnet(e.items, {"ok": T.LINK_UP, "warn": T.AMBER}.get(e.severity, T.SUBNET_HUE))
        # legend freshness row
        rows = [(n.label, n.last_probe_age_s) for n in sorted(tv.nodes.values(), key=lambda n: n.label)
                if ev(n.role) == "router" or n.switch is not None]
        self.legend.set_freshness(rows)
        if tv.show_probe_notice and not self._notice_shown:
            self._notice_shown = True
            self.infobar.show_text(PROBE_NOTICE)
        if self.a_probing.isChecked() != tv.probing_enabled:
            self.a_probing.blockSignals(True)
            self.a_probing.setChecked(tv.probing_enabled)
            self.a_probing.blockSignals(False)
        self.a_probing.setText("Probing: on" if tv.probing_enabled else "Probing: off")
        self._sync_probing_pill(tv.probing_enabled)
        has_kvm = any(n.key.startswith("kvm:") for n in data.nodes)
        self.ram_label.setVisible(has_kvm)
        if has_kvm and not self.ram_timer.isActive():
            self.ram_timer.start()
            self._update_ram()
        elif not has_kvm and self.ram_timer.isActive():
            self.ram_timer.stop()

    def _update_counter(self, data: LabData):
        n, lim = data.running_total, data.limit
        self._budget = (n, lim)
        self.counter.setToolTip(f"Running CML nodes across all labs: {n} of {lim}.\n"
                                f"CML-Free runs at most {lim} nodes at a time; this lab uses {data.this_lab_running}.\n"
                                "Unmanaged switches and external connectors do not count.")
        self._warn_budget(self._budget_warn)

    def _update_summary(self):
        """Status bar + toolbar title block + idle inspector: one LabSummary (same counters everywhere)."""
        data = self.view_data
        if data is None:
            return
        sm = summarize(data, self.cloud_health)
        tone = lab_tone(data.lab_state)
        self.lab_label.setText(f"{data.title} · {sm.running_text}")
        self.lab_label.set_glyph("up" if sm.nodes_running and sm.nodes_running == sm.nodes_total else
                                 ("stopped" if not sm.nodes_running else "warn"),
                                 "up" if sm.nodes_running == sm.nodes_total and sm.nodes_total else
                                 ("neutral" if not sm.nodes_running else "warn"))
        self.lab_label.setToolTip(f"{sm.nodes_total} devices · {sm.links_up}/{sm.links_total} links up")
        self.lab_name_btn.setText(data.title)
        self.lab_pill.set_state(T.state_label(data.lab_state).upper(), tone, "up" if tone == "up" else "stopped")
        self.lab_pill.setVisible(True)
        if self.inspector.stack.currentIndex() == 0:
            self.inspector.show_summary(sm, True)

    def _update_ram(self):
        from ..telemetry import host_mem_available_gb
        gb = host_mem_available_gb()
        if gb is not None:
            st = "down" if gb < 3 else ("warn" if gb < 6 else None)
            txt = f"{gb:.1f} GB"
            self.ram_label.setText(f"Host RAM free: <b style='color:{T.status_text(st).name()}'>{txt}</b>" if st
                                   else f"Host RAM free: {txt}")

    def _probe_notice_ack(self):
        self.layers.settings.setValue("notices/probe_output", True)     # one-time, across restarts
        if self.ctl.telemetry is not None:
            self.ctl.telemetry.acknowledge_probe_notice()

    # ------------------------------------------------------------ selection
    def selected_key(self) -> str | None:
        sel = [i for i in self.scene.selectedItems() if isinstance(i, NodeItem)]
        return sel[0].key if sel else None

    def selected_link_key(self) -> str | None:
        sel = [i for i in self.scene.selectedItems() if isinstance(i, LinkItem)]
        return sel[0].key if sel else None

    def selected_link_keys(self) -> list[str]:
        return [i.key for i in self.scene.selectedItems() if isinstance(i, LinkItem)]

    def selected_bundle_key(self) -> str | None:
        sel = [i for i in self.scene.selectedItems() if isinstance(i, BundleItem)]
        return sel[0].key if sel else None

    def _selection_changed(self):
        self.scene.invalidate_layout()
        self.scene.update()
        if self.ctl.telemetry is not None:
            self.ctl.telemetry.set_selected_link(self.selected_link_key())
        self.update_inspector()

    def update_inspector(self):
        if not self.view_data:
            self.inspector.show_none()
            return
        key = self.selected_key()
        if key:
            nv = next((n for n in self.view_data.nodes if n.key == key), None)
            if nv:
                self.inspector.show_node(nv, self.view_data)
                return
        bk = self.selected_bundle_key()
        tv = self.view_data.telemetry
        if bk and tv is not None and bk in tv.bundles:
            self.inspector.show_bundle(tv.bundles[bk], self.view_data)
            return
        lk = self.selected_link_key()
        if lk:
            lv = next((l for l in self.view_data.links if l.key == lk), None)
            if lv:
                self.inspector.show_link(lv, self.view_data)
                return
        self.inspector.show_none()

    def mgmt_of(self, key: str) -> dict | None:
        if not self.ctl.lab_id:
            return None
        try:
            return self.ctl.state.lab(self.ctl.lab_id)["mgmt"].get(key)
        except Exception:  # noqa: BLE001
            return None

    def find_link(self, node_key: str, iface_short: str):
        if not self.view_data:
            return
        for lv in self.view_data.links:
            if (lv.a == node_key and lv.label_a == iface_short) or (lv.b == node_key and lv.label_b == iface_short):
                li = self.scene.links.get(lv.key)
                if li:
                    self.scene.clearSelection()
                    li.setSelected(True)
                    self.view.center_on_item(li)
                    self.scene.flash([lv.key], T.NEUTRAL)
                return
        self.notify(f"{iface_short} has no link on the canvas", "info", 4000)

    def _node_clicked(self, key: str):
        """Clicking the egress tile flashes the blue default paths once (UI only, no probe)."""
        item = self.scene.nodes.get(key)
        if item is None or item.kind != "cloud" or not self.view_data:
            return
        keys = [l.key for l in self.view_data.links if l.default_hop is not None]
        if keys:
            self.scene.flash(keys + [key], T.ROUTE_BLUE)
    # ----------------------------------------------------------- lab actions
    def new_lab(self):
        title = TextPrompt.ask(self, "New lab", "Lab title", "LabDesk Lab", "Create Lab",
                               validator(validate.lab_title))
        if not title:
            return
        self.log(f"creating lab '{title}'")
        self.runner.submit(self.ctl.new_lab, title.strip(), on_done=lambda _: self._lab_opened(),
                           on_error=self.on_error("create lab", True))

    def open_lab_dialog(self, page: str = "labs"):
        if not isinstance(page, str):
            page = "labs"
        self.runner.submit(self.ctl.list_labs, on_done=lambda labs: self._show_open_dialog(labs, page),
                           on_error=self.on_error("list labs", True))

    def _show_open_dialog(self, labs, page: str = "labs"):
        dlg = OpenLabDialog(labs, self, page)
        if dlg.exec() != OpenLabDialog.Accepted or not dlg.result_value:
            return
        if dlg.result_kind == "lab":
            self.runner.submit(self.ctl.open_lab, dlg.result_value, on_done=lambda _: self._lab_opened(),
                               on_error=self.on_error("open lab", True))
        else:
            self.log(f"importing {dlg.result_value}")
            self.runner.submit(self.ctl.import_yaml, dlg.result_value, on_done=lambda _: self._lab_opened(),
                               on_error=self.on_error("import", True))

    def show_about(self):
        AboutDialog(self._cml_version, self).exec()

    def open_lab_id(self, lab_id: str):
        self.runner.submit(self.ctl.open_lab, lab_id, on_done=lambda _: self._lab_opened(),
                           on_error=self.on_error("open lab"))

    def open_lab_title(self, title: str):
        def done(res):
            if res:
                self._lab_opened()
            else:
                self.log(f"lab '{title}' not found", "warn")
        self.runner.submit(self.ctl.open_lab_by_title, title, on_done=done, on_error=self.on_error("open lab"))

    def _lab_opened(self):
        self.clear_canvas()
        self.log(f"opened lab '{self.ctl.lab_title}'", "ok")
        self._polling = False
        self.poll()

    def export_yaml(self):
        if not self.ctl.lab_id:
            return
        path, _ = QFileDialog.getSaveFileName(self, "Export lab", f"{self.ctl.lab_title}.yaml", "YAML (*.yaml)")
        if path:
            self.runner.submit(self.ctl.export_yaml, path, on_done=lambda _: self.log(f"exported {path}", "ok"),
                               on_error=self.on_error("export", True))

    def delete_lab(self):
        if not self.ctl.lab_id:
            return
        if not confirm(self, "Delete lab", f"Delete the lab “{self.ctl.lab_title}” on the controller?",
                       "Delete Lab", detail="Its nodes are stopped and wiped first. This cannot be undone."):
            return
        self.runner.submit(self.ctl.delete_lab, on_done=lambda _: (self.clear_canvas(),
                                                                   self.log("lab deleted", "ok"), self.poll()),
                           on_error=self.on_error("delete lab", True))

    def start_all(self):
        if self.ctl.lab_id:
            if self._would_exceed():
                n, lim = self._budget
                self._warn_budget(True)
                QTimer.singleShot(6000, lambda: self._warn_budget(False))
                self.notify(f"Starting every node would pass the CML-Free limit ({n} of {lim} already running) - "
                            "CML refuses the nodes over the limit.", "warn", 8000)
            self.log("starting all nodes")
            self.runner.submit(self.ctl.start_all, on_done=lambda m: m and self.log(m, "warn"),
                               on_error=self.on_error("start all", True))

    def stop_all(self):
        if self.ctl.lab_id:
            self.log("stopping all nodes")
            self.runner.submit(self.ctl.stop_all, on_error=self.on_error("stop all", True))

    def wipe_all(self):
        if not self.ctl.lab_id:
            return
        if not confirm(self, "Wipe lab", "Wipe the disks of all (stopped) nodes?", "Wipe Lab",
                       detail="Running configs are lost; startup configs apply on next boot."):
            return
        self.runner.submit(self.ctl.wipe_all, on_done=lambda _: self.log("lab wiped", "ok"),
                           on_error=self.on_error("wipe (stop the lab first)", True))

    def toggle_link_mode(self, _checked=False):
        self.view.set_link_mode(self.a_link.isChecked())
        if self.a_link.isChecked():
            self.log("link mode: drag from one device to another (Esc to leave)")

    def edit_connection(self, first_run: bool = False) -> bool:
        """CML connection dialog (Lab menu ▸ CML connection…): saves the credentials file, then points the
        client, consoles and probes at the (maybe new) address."""
        from .connection_dialog import CmlConnectionDialog
        dlg = CmlConnectionDialog(self, first_run=first_run, settings=self.settings)
        if not dlg.exec() or not dlg.saved:
            return False
        host = config.reload_cml_host()
        self.ctl.client.set_host(host)
        if self.ctl.telemetry is not None:
            self.ctl.telemetry.cml_user = dlg.saved_user
        self.log(f"CML connection saved ({host})", "ok")
        self.poll()
        return True

    def _start_cml_windows(self):
        """Windows: VMware Workstation 'vmrun start <vmx>' when configured, else tell the user what to do."""
        vmx = str(self.settings.value("cml/vmx_path", "") or "").strip()
        vmrun = plat.vmrun_exe()
        if not (vmx and vmrun and Path(vmx).exists()):
            why = ("VMware Workstation's vmrun was not found" if not vmrun else
                   "no CML VM file (.vmx) is set" if not vmx else f"{vmx} does not exist")
            info(self, "Start CML VM",
                 "Start the CML virtual machine in VMware Workstation (or Hyper-V), then wait about 2 minutes: "
                 "LabDesk reconnects by itself.",
                 f"LabDesk can start it for you when VMware Workstation is installed and the CML VM's .vmx file "
                 f"is set under Lab ▸ CML connection… (now: {why}).")
            return None
        return [vmrun, "-T", "ws", "start", vmx, "nogui"]

    def start_cml_vm(self):
        start_cmd = None
        if plat.IS_WINDOWS:
            start_cmd = self._start_cml_windows()
            if start_cmd is None:
                return
        self.start_cml_btn.setEnabled(False)
        self.log("starting CML VM (" + ("vmrun start" if start_cmd else "virsh start cml")
                 + ") and waiting for the API…")

        def work():
            try:
                if start_cmd:
                    kvm.run(start_cmd, timeout=120)
                else:
                    kvm.run(kvm.virsh("start", config.CML_VM_NAME), timeout=60)
            except kvm.KvmError as e:
                if "already active" not in str(e) and "already running" not in str(e).lower():
                    raise
            end = time.time() + 900
            client = CMLClient()
            while time.time() < end:
                if client.is_ready():
                    return True
                time.sleep(5)
            raise CMLError("CML did not become ready within 15 minutes")

        def done(_):
            self.start_cml_btn.setEnabled(True)
            self.log("CML is ready", "ok")
            if not self.ctl.lab_id and self.ctl.state.last_lab:
                self.open_lab_id(self.ctl.state.last_lab)
            self.poll()

        def fail(e):
            self.start_cml_btn.setEnabled(True)
            self.on_error("start CML", True)(e)
        self.runner.submit(work, on_done=done, on_error=fail)

    # ---------------------------------------------------------- build actions
    def on_device_dropped(self, type_key: str, x: float, y: float):
        if not self.ctl.lab_id:
            info(self, "LabDesk", "Open or create a lab first.")
            return
        d = BY_KEY[type_key]
        why = unavailable_reason(d)
        if why:
            info(self, d.title, why)
            return
        if d.backend == "kvm":
            text = (f"Create a host-KVM VM '{d.title}' via libvirt?\n\n{d.note}\n\n"
                    "The disk is a thin overlay on a base image uploaded once to the 'images' pool. "
                    "NIC 1 (management) is on the libvirt 'default' network (virbr0).")
            warn = kvm.memory_warning(d.key)
            if warn:
                text += f"\n\nWARNING: {warn}"
            if not confirm(self, "Create host VM", f"Create a host-KVM VM “{d.title}” via libvirt?", "Create VM",
                           destructive=False, detail=text.split("\n\n", 1)[1]):
                return
        self.log(f"adding {d.title}…")
        if type_key == "ext_cloud":
            self.runner.submit(self.ctl.add_device, type_key, x, y, on_done=self._cloud_added,
                               on_error=self.on_error(f"add {d.title}", True))
            return
        self.runner.submit(self.ctl.add_device, type_key, x, y,
                           on_done=lambda key: (self.log(f"added {d.title}", "ok"), self.poll()),
                           on_error=self.on_error(f"add {d.title}", True))

    # ------------------------------------------------------- Internet cloud
    def _cloud_added(self, key: str):
        conf = cloud.load_conf()
        self.log(f"added the Internet cloud (System-Bridge connector on {conf.bridge}, gateway {conf.cloud_ip}) - "
                 "Shift-drag a link from a router to it, then Auto-configure", "ok")
        self.poll()
        self.check_cloud()

        def warn(h):
            self._cloud_done(h)
            if h.state == "not_installed":
                info(self, "Internet cloud",
                     "The cloud's host service is not installed yet. Install it once (needs root):",
                     f"{cloud.INSTALL_HINT}\n\nIt creates the network namespace 'labcloud' on virbr0 that "
                     "answers 8.8.8.8 / 1.1.1.1; your host's own routing is not changed.")
        self.runner.submit(cloud.check_health, on_done=warn, on_error=lambda e: None)

    def check_cloud(self):
        if self._cloud_checking or self._closing:
            return
        self._cloud_checking = True

        def fail(exc):
            self._cloud_checking = False
            self.log(f"cloud health check failed: {exc}", "warn")
        self.runner.submit(cloud.check_health, on_done=self._cloud_done, on_error=fail)

    def _cloud_done(self, h):
        self._cloud_checking = False
        old = self.cloud_health
        self.cloud_health = h
        self.scene.ctx.cloud_health = h
        for item in self.scene.nodes.values():
            if item.is_internet:
                item.refresh_tooltip()
                item.update()
        if old is None or old.state != h.state:
            if h.ok:
                self.log(h.headline, "ok", tag="CLOUD", hue=T.LINK_UP)
            elif h.state == "unsupported":
                self.log(h.headline, "warn", tag="CLOUD")
            elif h.state != "unknown":
                self.log(f"{h.headline}" + (f" - fix: {h.hint}" if h.hint else ""), "error", tag="CLOUD",
                         hue=T.DOWN_RED)
        key = self.selected_key()
        if key and self.scene.nodes.get(key) is not None and self.scene.nodes[key].is_internet:
            self.update_inspector()
        self._update_summary()

    def cloud_routers(self, cloud_key: str | None = None) -> list[str]:
        """Router keys cabled to an Internet cloud (all clouds, or ``cloud_key``)."""
        out = []
        for lv in (self.view_data.links if self.view_data else []):
            pair = self._cloud_router_pair(lv.a, lv.b)
            if pair and (cloud_key is None or pair[1] == cloud_key) and pair[0] not in out:
                out.append(pair[0])
        return out

    def auto_configure_cloud(self, key: str):
        lbl = self._label(key)
        self.log(f"{lbl}: planning the Internet cloud uplink…")

        def show(plan):
            dlg = CloudConfigDialog(plan, self.cloud_health, self)
            if dlg.exec() != QDialog.Accepted:
                self.log(f"{lbl}: auto-configure cancelled")
                return
            ch = dlg.choices()
            if ch["live"] and plan.console_open and not confirm(
                    self, "Console open", f"{lbl}'s console is open. Type the configuration on that console line now?",
                    "Type on Console", destructive=False, cancel="Day-0 Only",
                    detail="“Day-0 Only” updates only the day-0 config and the return path."):
                ch["live"] = False
            self.log(f"{lbl}: configuring {plan.iface} {plan.ip}/{plan.conf.prefixlen}, default via "
                     f"{plan.conf.cloud_ip}…")

            def done(steps):
                for ok, text in steps:
                    self.log(f"{lbl}: {text}", "ok" if ok else "warn", tag="CLOUD",
                             hue=T.LINK_UP if ok else T.AMBER)
                self.poll()
                if ch["live"] and steps and steps[0][0]:
                    QTimer.singleShot(4000, lambda: self.verify_internet(key))
            if ch["restart"]:
                self.log(f"{lbl}: will wipe & restart after storing the day-0 config (~3 min until it is back)")
            self.runner.submit(self.ctl.cloud_apply, plan, ch["live"], ch["day0"], ch["ospf"], ch["routes"],
                               ch["restart"], on_done=done, on_error=self.on_error(f"auto-configure {lbl}", True))
        self.runner.submit(self.ctl.cloud_plan, key, on_done=show, on_error=self.on_error(f"auto-configure {lbl}", True))

    def copy_cloud_fix(self):
        h = self.cloud_health
        QApplication.clipboard().setText(h.hint if h is not None and h.hint else cloud.FIX_HINT)
        self.log("copied: " + (h.hint if h is not None and h.hint else cloud.FIX_HINT))

    def on_link_requested(self, a: str, b: str, retried: bool = False):
        self.log("creating link…")
        self.runner.submit(self.ctl.add_link, a, b, on_done=lambda res: self._link_done(res, a, b),
                           on_error=lambda e: self._link_failed(e, a, b, retried))

    def _cloud_router_pair(self, a: str, b: str) -> tuple[str, str] | None:
        """(router key, cloud key) when a link joins a CML IOS router and the Internet cloud."""
        ia, ib = self.scene.nodes.get(a), self.scene.nodes.get(b)
        if ia is None or ib is None:
            return None
        for r, c in ((ia, ib), (ib, ia)):
            if c.is_internet and r.dtype.shape == "router" and r.dtype.vendor == "cisco" and r.key.startswith("cml:"):
                return r.key, c.key
        return None

    def _link_done(self, res, a: str | None = None, b: str | None = None):
        self.log(res.text, "ok")
        self.poll()
        pair = self._cloud_router_pair(a, b) if a and b else None
        if pair is not None:
            rk = pair[0]
            if confirm(
                    self, "Internet cloud", f"{self._label(rk)} is now cabled to the Internet cloud. Auto-configure it "
                    "now?", "Auto-configure…", destructive=False, cancel="Not Now",
                    detail=f"Next free {cloud.load_conf().net_text} address, default route to the cloud, day-0 config, "
                    "return path - you see the exact commands before anything is applied."):
                QTimer.singleShot(1500, lambda: self.auto_configure_cloud(rk))
            return
        if res.needs_restart:
            names = ", ".join(lbl for _, lbl in res.needs_restart)
            if confirm(
                    self, "Link added to a booted node", f"Wipe & restart {names} now?", "Wipe && Restart",
                    cancel="Keep Running",
                    detail=f"{names} already booted. CML adds the new interface, but it usually stays DOWN inside the "
                    "running VM until the node is wiped and restarted. The running config is lost; the startup "
                    "config is re-applied."):
                for key, lbl in res.needs_restart:
                    self.log(f"wipe & restart {lbl}")
                    self.runner.submit(self.ctl.wipe_and_restart, key, on_error=self.on_error(f"restart {lbl}", True))

    def _link_failed(self, exc, a, b, retried):
        if isinstance(exc, NodeLockedError) and not retried:
            if confirm(
                    self, "Interfaces locked", f"Stop and wipe {exc.node_label} now and retry the link?",
                    "Wipe && Retry",
                    detail=f"{exc.node_label} needs a new interface, but CML locks a node's interfaces while it has a "
                    "disk. The running config is lost; the startup config re-applies on next boot."):
                key = a if self._label(a) == exc.node_label else b

                def wipe_then_link():
                    self.ctl.wipe(key)
                    return self.ctl.add_link(a, b)
                self.runner.submit(wipe_then_link, on_done=lambda res: self._link_done(res, a, b),
                                   on_error=lambda e: self._link_failed(e, a, b, True))
                return
        self.on_error("link", True)(exc)

    def on_node_moved(self, key: str, x: float, y: float):
        self._pending_moves[key] = time.time()
        self.runner.submit(self.ctl.move, key, x, y, on_error=self.on_error("move"))

    def rename_node(self, key: str):
        item = self.scene.nodes.get(key)
        if not item:
            return
        if key.startswith("kvm:"):
            info(self, "Rename", "Host-KVM VMs keep their libvirt name; delete and re-create to rename.")
            return
        new = TextPrompt.ask(self, "Rename", f"New name for {item.label}", item.label, "Rename",
                             validator(validate.cml_label))
        if new and new.strip() != item.label:
            self.runner.submit(self.ctl.rename, key, new.strip(),
                               on_done=lambda n: (self.log(f"renamed {item.label} → {n}", "ok"), self.poll()),
                               on_error=self.on_error("rename", True))

    def delete_selected(self):
        nodes = [i for i in self.scene.selectedItems() if isinstance(i, NodeItem)]
        links = [i for i in self.scene.selectedItems() if isinstance(i, LinkItem)]
        if not nodes and not links:
            return
        what = ", ".join(n.label for n in nodes) or f"{len(links)} link(s)"
        extra = ""
        if any(n.key.startswith("kvm:") for n in nodes):
            extra = "\n\nHost VMs are destroyed and their disks (overlays) removed."
        verb = "Delete Node" if len(nodes) == 1 and not links else ("Delete Link" if len(links) == 1 and not nodes
                                                                    else "Delete")
        if not confirm(self, "Delete", f"Delete {what}?", verb, detail=extra.strip()):
            return
        for l in links:
            self.runner.submit(self.ctl.delete_link, l.key, on_done=lambda _: (self.log("link deleted", "ok"),
                                                                               self.poll()),
                               on_error=self.on_error("delete link", True))
        for n in nodes:
            self.log(f"deleting {n.label}…")
            self.runner.submit(self.ctl.delete, n.key,
                               on_done=lambda _, lbl=n.label: (self.log(f"deleted {lbl}", "ok"), self.poll()),
                               on_error=self.on_error(f"delete {n.label}", True))

    def start_node(self, key):
        lbl = self._label(key)
        if self._would_exceed({key}):
            n, lim = self._budget
            self._warn_budget(True)
            QTimer.singleShot(6000, lambda: self._warn_budget(False))
            self.notify(f"{lbl} would pass the CML-Free limit ({n} of {lim} nodes already running) - stop another "
                        "node first.", "warn", 8000)
        self.log(f"starting {lbl}")
        self.runner.submit(self.ctl.start, key, on_done=lambda m: (m and self.log(m, "warn"), self.poll()),
                           on_error=self.on_error(f"start {lbl}", True))

    def stop_node(self, key):
        lbl = self._label(key)
        self.log(f"stopping {lbl}")
        self.runner.submit(self.ctl.stop, key, on_done=lambda _: self.poll(),
                           on_error=self.on_error(f"stop {lbl}", True))

    def wipe_node(self, key):
        lbl = self._label(key)
        if not confirm(self, "Wipe", f"Stop {lbl} (if running) and wipe its disk?", "Wipe Node",
                       detail="The startup configuration is applied again on next boot."):
            return
        self.runner.submit(self.ctl.wipe, key, on_done=lambda _: (self.log(f"{lbl} wiped", "ok"), self.poll()),
                           on_error=self.on_error(f"wipe {lbl}", True))

    def edit_config(self, key):
        lbl = self._label(key)
        self.runner.submit(self.ctl.get_config, key, on_done=lambda text: self._config_dialog(key, lbl, text),
                           on_error=self.on_error("read config", True))

    def _config_dialog(self, key, lbl, text):
        dlg = ConfigDialog(lbl, text, self)
        if dlg.exec() != ConfigDialog.Accepted:
            return
        new = dlg.text()

        def work():
            if dlg.wipe_after:
                self.ctl.wipe(key)
            self.ctl.set_config(key, new)
            if dlg.wipe_after:
                self.ctl.start(key)
        self.runner.submit(work, on_done=lambda _: self.log(f"startup config of {lbl} saved"
                                                            + (" - wiped and restarting" if dlg.wipe_after else ""),
                                                            "ok"),
                           on_error=self.on_error("save config", True))

    def edit_properties(self, key):
        lbl = self._label(key)
        nv = next((n for n in (self.view_data.nodes if self.view_data else []) if n.key == key), None)

        def load():
            return self.ctl.mgmt(key)

        def show(mgmt):
            info = {"Name": lbl, "Type": nv.dtype.title if nv else "", "Backend": "host KVM (libvirt)"
                    if key.startswith("kvm:") else "CML", "ID": key.split(":", 1)[1]}
            if mgmt.get("auto"):
                info["Detected IP"] = mgmt.get("ip")
            dlg = PropertiesDialog(lbl, info, mgmt, self)
            if dlg.exec() == PropertiesDialog.Accepted:
                try:
                    self.ctl.set_mgmt(key, dlg.ip.text().strip(), dlg.user.text().strip())
                    self.log(f"{lbl}: management address saved", "ok")
                    self.update_inspector()
                except ValueError as e:
                    info(self, "Properties", str(e))
        self.runner.submit(load, on_done=show, on_error=self.on_error("properties", True))

    # --------------------------------------------------------------- device credentials + hover card
    def edit_credentials(self, key: str):
        """Per-device login / password / enable secret, stored in device-creds (0600) - never in state.json,
        LabDesk.ini, CML or the log.  The probes use them for login and enable."""
        lbl = self._label(key)
        detail = creds_mod.load_detail(lbl)
        first = self.first_login_of(key)
        dlg = CredentialsDialog(lbl, detail, self, first_login=first)
        if dlg.exec() != CredentialsDialog.Accepted:
            return
        try:
            if dlg.reset_requested and first is not None:
                creds_mod.reset_to_default(lbl, first.creds_values())
            else:
                creds_mod.save(lbl, dlg.values(), use_defaults=dlg.use_defaults())
        except (ValueError, OSError) as e:
            info(self, "Credentials", f"Not saved: {e}")
            return
        what = ("reset to its first-login default" if dlg.reset_requested
                else "now uses the default login" if dlg.use_defaults() else "credentials saved")
        self.log(f"{lbl}: {what} (device-creds, mode 0600)", "ok")
        if self.ctl.telemetry is not None:
            try:
                self.ctl.telemetry.credentials_changed(None if dlg.use_defaults() else lbl)
            except Exception:  # noqa: BLE001
                pass
        self.update_inspector()
        self.view.refresh_hover_card()

    def _set_console_neon(self, on: bool):
        self.settings.setValue("console/neon", bool(on))
        if hasattr(self.launcher, "neon"):
            self.launcher.neon = bool(on)
        self.log("console look: " + ("neon green" if on else "normal Konsole colours")
                 + " - applies to the next console you open", "info")

    def _set_show_passwords(self, on: bool):
        self.settings.setValue("ui/hover_show_passwords", bool(on))
        self.view.refresh_hover_card()

    def first_login_of(self, key: str):
        """default_logins.FirstLogin of a node (day-0 config / platform table / cml-credentials.env), or None."""
        item = self.scene.nodes.get(key)
        nv = getattr(item, "nv", None)
        if nv is None:
            return None
        env = DL.load_env() if nv.dtype.backend == "kvm" else None
        return DL.for_node(nv, env)

    def login_of(self, key: str):
        """(effective FirstLogin, 'default' | 'saved' | None) - what the card, menu and console hint show."""
        return DL.effective(creds_mod.load_detail(self._label(key)), self.first_login_of(key))

    def copy_login(self, key: str, what: str):
        fl, _ = self.login_of(key)
        val = (fl.username if what == "user" else fl.password) if fl is not None else None
        lbl = self._label(key)
        if not val:
            self.notify(f"{lbl}: no {'username' if what == 'user' else 'password'} to copy", "warn", 4000)
            return
        QApplication.clipboard().setText(val)
        self.log(f"{lbl}: {'username' if what == 'user' else 'password'} copied to the clipboard", "info")

    def _first_console_hint(self, key: str):
        """First console of a node in this session: event log + toast with its login."""
        seen = self.__dict__.setdefault("_console_hint_seen", set())
        lbl = self._label(key)
        if (self.ctl.lab_id, lbl) in seen:
            return
        seen.add((self.ctl.lab_id, lbl))
        fl, tag = self.login_of(key)
        show = self.a_show_pw.isChecked()
        text = DL.console_hint(lbl, fl, show=show)
        if not text:
            return
        # the event log keeps only lab-default passwords in clear; a saved / cml-credentials.env one is masked
        lab_default = tag == "default" and fl.source != "cml-credentials.env"
        log_text = text if lab_default else DL.console_hint(lbl, fl, show=False)
        self.log(log_text + ("" if lab_default else " - right-click ▸ Copy password"), "info", tag="LOGIN")
        self.notify(text, "info", 10000)

    def _hover_card_for(self, item):
        nv = item.nv
        if nv is None:
            return None
        detail = creds_mod.load_detail(nv.label)
        mg = self.mgmt_of(nv.key) or {}
        model = build_card(nv, creds=detail, show_passwords=self.a_show_pw.isChecked(), mgmt_ip=mg.get("ip"),
                           first_login=self.first_login_of(nv.key))
        dpr = self.devicePixelRatioF() or 1.0
        ck = (nv.dtype.key, T.generation(), round(dpr, 2))
        cache = self.__dict__.setdefault("_card_icons", {})
        icon = cache.get(ck)
        if icon is None:
            icon = cache[ck] = device_pixmap(nv.dtype, 30, max(2.0, dpr))
        return model, icon

    def _label(self, key):
        item = self.scene.nodes.get(key)
        return item.label if item else key

    # --------------------------------------------------------------- consoles
    def open_console(self, key: str):
        item = self.scene.nodes.get(key)
        if item and item.dtype.node_definition in NON_COUNTING_DEFS:
            self.log(f"{item.label} has no console", "warn")
            return
        lbl = self._label(key)
        self.log(f"opening console of {lbl}…")
        self._first_console_hint(key)

        def work():
            cmd = self.ctl.console_command(key)
            return self.launcher.open(cmd)

        def opened(m):
            self.log(f"console: {m}", "ok")
            script = self.show_script_of(key)
            if script is not None and script.active:
                self.run_show_script(key, script.commands, quiet_when_down=True)
        self.runner.submit(work, on_done=opened, on_error=self.on_error(f"console {lbl}", True))

    # --------------------------------------------------------------- per-device show script
    IOS_CONSOLE_DEFS = ("iosv", "iosvl2", "iol-xe", "ioll2-xe")

    def has_show_script(self, key: str) -> bool:
        """Show scripts are typed on a Cisco IOS console of a CML node (routers and managed switches)."""
        item = self.scene.nodes.get(key)
        return bool(item and key.startswith("cml:") and item.dtype.node_definition in self.IOS_CONSOLE_DEFS)

    def show_script_of(self, key: str):
        if not self.has_show_script(key):
            return None
        try:
            return SS.load(self._label(key))
        except Exception:  # noqa: BLE001  (a broken file must never stop a console from opening)
            return None

    def _show_script_suggestion(self, key: str) -> list[str]:
        item = self.scene.nodes.get(key)
        tele = self._tele()
        nf = tele.facts.get(key) if tele is not None else None
        return SS.suggest(nf, is_switch=bool(item and item.dtype.node_definition in ("iosvl2", "ioll2-xe")))

    def edit_show_script(self, key: str):
        lbl = self._label(key)
        item = self.scene.nodes.get(key)
        others = {}
        try:
            others = {k: v for k, v in SS.load_all().items() if k != lbl and v.commands}
        except Exception:  # noqa: BLE001
            pass
        live = bool(item and item.state == "BOOTED" and self._tele() is not None)
        dlg = ShowScriptDialog(lbl, self.show_script_of(key), self._show_script_suggestion(key), others,
                               can_run=live, parent=self)
        if dlg.exec() != ShowScriptDialog.Accepted:
            return
        try:
            script = dlg.result_script()
            SS.save(lbl, script)
        except (ValueError, OSError) as e:
            info(self, "Show script", f"Not saved: {e}")
            return
        if script is None:
            self.log(f"{lbl}: show script removed", "ok")
            return
        when = "runs when its console opens" if script.on_open else "runs only from the menu"
        self.log(f"{lbl}: show script saved ({len(script.commands)} command(s), {when})", "ok")
        if dlg.run_now:
            self.run_show_script(key, script.commands)

    def run_show_script(self, key: str, commands: list[str] | None = None, quiet_when_down: bool = False):
        """Type the device's show script on its console now (output appears in the open console window)."""
        lbl = self._label(key)
        item = self.scene.nodes.get(key)
        if commands is None:
            script = self.show_script_of(key)
            commands = script.commands if script is not None else []
        if not commands:
            self.log(f"{lbl} has no show script yet (right-click ▸ Show script…)", "warn")
            return
        if self._tele() is None or not item or item.state != "BOOTED":
            if not quiet_when_down:
                self.log(f"{lbl}: show script not run - the device is not running", "warn")
            return
        self.log(f"{lbl}: running its show script ({len(commands)} command(s))…")
        self._action(f"show script {lbl}", self._tele().run_show_script, key, list(commands))

    def _open_mgmt(self, key, proto):
        m = self.ctl.state.lab(self.ctl.lab_id)["mgmt"].get(key) or {}
        ip, user = m.get("ip"), m.get("user", "admin")
        lbl = self._label(key)

        def work():
            nonlocal ip
            if not ip and key.startswith("kvm:"):
                ip = kvm.mgmt_ip(key[4:])
            if not ip:
                raise CMLError("no management IP set (Properties…)")
            if proto == "ssh":
                return self.launcher.open(launchers.ssh_to_device(lbl, ip, user))
            if proto == "telnet":
                return self.launcher.open(launchers.telnet_to_device(lbl, ip))
            url = launchers.http_url(ip, proto == "https")
            launchers.open_url(url)
            return f"opened {url}"
        self.runner.submit(work, on_done=lambda m: self.log(m, "ok"), on_error=self.on_error(proto, True))


    # ------------------------------------------------------- telemetry actions
    def _tele(self):
        return self.ctl.telemetry

    def set_probing(self, on: bool):
        if self._tele() is not None:
            self._tele().set_probing(on)
        self.a_probing.setText("Probing: on" if on else "Probing: off")
        self.log("console probing " + ("on" if on else "off - only CML API state is shown"), "info")
        self.poll()

    def set_probe_while_open(self, key: str, on: bool):
        if self._tele() is None:
            return
        if on and not confirm(
                self, "Probe while the console is open",
                f"LabDesk will keep reading {self._label(key)} while you have its console open.", "Keep Probing",
                destructive=False, detail="Its show commands (and their output) will appear in your console window."):
            self.update_inspector()
            return
        self._tele().set_probe_while_open(key, on)
        self.poll()

    def _action(self, what: str, fn, *args, on_ok=None, **kw):
        """Run a blocking telemetry action in the TaskRunner and log its ActionResult."""
        def done(res):
            ok = getattr(res, "ok", True)
            text = getattr(res, "text", str(res))
            self.log(f"{what}: {text}", "ok" if ok else "warn")
            if ok and on_ok:
                on_ok(res)
            self.poll()
        self.runner.submit(fn, *args, on_done=done, on_error=self.on_error(what), **kw)

    def refresh(self):
        """Toolbar Refresh (F5): an immediate API poll, plus an immediate probe of the selected node (or of
        every router / managed switch) - never typed into a console that is open (etiquette)."""
        tele = self._tele()
        if tele is not None and self.ctl.lab_id:
            key = self.selected_key()
            keys = [key] if key else None

            def done(res):
                if getattr(res, "text", ""):
                    self.log(f"refresh: {res.text}", "info")
                self.poll()
            self.runner.submit(tele.refresh, keys, on_done=done, on_error=self.on_error("refresh"))
        self.poll()

    def probe_now(self, key: str):
        if self._tele() is None:
            return
        self.log(f"probing {self._label(key)} now…")
        self._action(f"probe {self._label(key)}", self._tele().probe_now, key)

    def condition_presets(self) -> list[str]:
        if self._tele() is None:
            return []
        return list(self._tele().condition_presets())

    def _link_name(self, key: str) -> str:
        lv = next((l for l in (self.view_data.links if self.view_data else []) if l.key == key), None)
        if not lv:
            return key
        return f"{self._label(lv.a)} {lv.label_a} ↔ {self._label(lv.b)} {lv.label_b}"

    def set_condition(self, key: str, preset: str):
        if self._tele() is None:
            return
        self.log(f"{self._link_name(key)}: applying '{preset}'…")
        self._action(f"condition {self._link_name(key)}", self._tele().set_condition, key, preset=preset)

    def clear_condition(self, key: str):
        if self._tele() is not None:
            self._action(f"clear condition {self._link_name(key)}", self._tele().clear_condition, key)

    def pull_cable(self, key: str):
        if self._tele() is None:
            return
        if not confirm(self, "Pull cable", f"Simulate unplugging the cable {self._link_name(key)}?", "Pull Cable",
                       detail="CML stops the link; both routers see the interface go down."):
            return
        self._action(f"pull cable {self._link_name(key)}", self._tele().pull_cable, key)

    def restore_cable(self, key: str):
        if self._tele() is not None:
            self._action(f"plug back in {self._link_name(key)}", self._tele().restore_cable, key)

    # ---------------------------------------------------- simulate: packets
    def _legs(self, start: str, link_keys: list[str], last_caption: str = "", fail: bool = False) -> tuple[list, str]:
        """Link keys walked from node `start` -> PacketItem legs (direction per link)."""
        legs = []
        cur = start
        for i, lk in enumerate(link_keys):
            li = self.scene.links.get(lk)
            if li is None:
                break
            rev = li.a.key != cur
            cur = li.a.key if rev else li.b.key
            last = i == len(link_keys) - 1
            legs.append((li, rev, last_caption if last else "", fail and last))
        return legs, cur

    def animate_default_path(self, key: str) -> bool:
        """'Send a packet to the internet': UI-only animation along the router's default path (blue
        hops from the model), ending at the egress - or dropped with ✕ where the path breaks."""
        nv = next((n for n in (self.view_data.nodes if self.view_data else []) if n.key == key), None)
        p = nv.tele.default_path if nv is not None and nv.tele is not None else None
        if p is None or not p.hops:
            why = p.detail if p is not None and p.detail else "no default path known yet"
            self.log(f"{self._label(key)}: nothing to animate - {why}", "warn")
            self.scene.flash([key], T.DOWN_RED)
            return False
        state = ev(p.state)
        legs = []
        cur = key
        for i, hop in enumerate(p.hops):
            last = i == len(p.hops) - 1
            broken = last and state in ("BROKEN_AT", "STALE") and p.broken_at in hop.link_keys
            if hop.to_node and self.scene.nodes.get(hop.to_node) and \
                    self.scene.nodes[hop.to_node].kind == "cloud":
                cap = "→ internet"
            else:
                cap = self._label(hop.to_node) if hop.to_node else (hop.via_ip or "?")
            if broken:
                cap = "dropped here"
                self.log(f"{self._label(key)}: packet dropped - {p.detail or 'path broken'}", "warn",
                         tag="SIM", hue=T.EVENT_HUES["sim"])
            if hop.via_ip and not broken:
                cap += f" · {hop.via_ip}" if cap != "→ internet" else ""
            hl, cur = self._legs(cur, hop.link_keys, cap, broken)
            legs += hl
        self.scene.add_packet(legs, T.ROUTE_BLUE)
        return True

    def animate_link(self, key: str):
        li = self.scene.links.get(key)
        if li is None:
            return
        lv = li.lv
        st = ev(lv.status.kind) if lv is not None and lv.status is not None else ("UP" if li.state == "STARTED" else "")
        fail = st not in ("UP", "")
        cap = f"dropped: {lv.status.text}" if fail and lv is not None and lv.status else li.b.label
        self.scene.add_packet([(li, False, cap, fail)], T.ROUTE_BLUE if not fail else T.DOWN_RED)

    def traceroute(self, key: str, target: str = "8.8.8.8"):
        if self._tele() is None:
            return
        lbl = self._label(key)
        self.log(f"{lbl}: traceroute {target} (runs on the console, ~10 s)…")

        def done(res):
            if not res.ok:
                self.log(f"traceroute from {lbl}: {res.text}", "warn")
                return
            hops = res.data.get("hops") or []
            legs = []
            cur = key
            for h in hops:
                rtt = f" · {h['rtt_ms']:.0f} ms" if h.get("rtt_ms") is not None else ""
                cap = f"{h['hop']}. {h.get('name') or h.get('ip') or '*'}{rtt}"
                if h.get("link_keys"):
                    hl, cur = self._legs(cur, h["link_keys"], cap)
                    legs += hl
            if legs:
                self.scene.add_packet(legs, T.ROUTE_BLUE)
            self.log(f"traceroute from {lbl} to {target}: {res.text}", "ok")
            self.poll()
        self.runner.submit(self._tele().traceroute, key, target, on_done=done,
                           on_error=self.on_error(f"traceroute {lbl}"))

    def ping_dialog(self, key: str):
        target = TextPrompt.ask(self, "Ping", f"Ping from {self._label(key)} to (IP address or host name)",
                                "8.8.8.8", "Ping", validator(validate.host), "e.g. 8.8.8.8")
        if target:
            self.ping(key, target.strip())

    def ping(self, key: str, target: str):
        if self._tele() is None:
            return
        lbl = self._label(key)
        self.log(f"{lbl}: ping {target}…")

        def done(res):
            d = res.data or {}
            avg = f", avg {d.get('rtt_avg_ms')} ms" if d.get("rtt_avg_ms") is not None else ""
            self.log(f"{lbl} → {target}: {res.text}{avg}", "ok" if res.ok else "warn")
            self.scene.flash([key], T.LINK_UP if res.ok else T.DOWN_RED)
        self.runner.submit(self._tele().ping, key, target, on_done=done, on_error=self.on_error(f"ping {lbl}"))

    def verify_internet(self, key: str):
        if self._tele() is None:
            return
        lbl = self._label(key)
        self.log(f"{lbl}: Verify Internet (ping 8.8.8.8 and 1.1.1.1 from the router)…")
        self.animate_default_path(key)

        def done(res):
            if res.ok:
                nv = next((n for n in self.view_data.nodes if n.key == key), None) if self.view_data else None
                p = nv.tele.default_path if nv is not None and nv.tele is not None else None
                if p is not None and p.egress_node:
                    self._verified_at[p.egress_node] = time.time()
                self.log(f"{lbl} reaches the internet: {res.text}", "ok", tag="INTERNET ✓", hue=T.LINK_UP)
            else:
                self.log(f"{lbl} cannot reach the internet: {res.text}", "warn")
                self.scene.flash([key], T.DOWN_RED)
            self.poll()
        self.runner.submit(self._tele().verify_internet, key, on_done=done,
                           on_error=self.on_error(f"verify internet {lbl}"))

    def _routers(self) -> list:
        if not self.view_data:
            return []
        return [n for n in sorted(self.view_data.nodes, key=lambda n: n.label)
                if n.tele is not None and ev(n.tele.role) == "router"]

    def _fill_sim_menu(self):
        m = self.sim_menu
        m.clear()
        routers = self._routers()
        if not routers:
            a = m.addAction("Open a lab with running routers to simulate traffic")
            a.setEnabled(False)
            return
        live = [n for n in routers if n.key.startswith("cml:") and n.state == "BOOTED"]
        for title, fn, tip, pool in (
                ("Send a packet to the internet from", self.animate_default_path,
                 "Animate the router's default route hop by hop (UI only, nothing is sent)", routers),
                ("Traceroute 8.8.8.8 from", self.traceroute,
                 "Real traceroute on the router's console, then animate the hops it reported", live),
                ("Ping… from", self.ping_dialog, "Real ping on the router's console", live),
                ("Verify Internet from", self.verify_internet,
                 "Ping 8.8.8.8 and 1.1.1.1 from the router; on success the egress tile shows INTERNET ✓", live)):
            sub = m.addMenu(title)
            sub.setToolTipsVisible(True)
            for n in pool:
                a = sub.addAction(n.label)
                a.setToolTip(tip)
                a.triggered.connect(lambda _=False, k=n.key, f=fn: f(k))
            sub.setEnabled(bool(pool))
        m.addSeparator()
        a = m.addAction("Animate every default path")
        a.triggered.connect(lambda: [self.animate_default_path(n.key) for n in routers])
        m.addSeparator()
        hint = m.addAction("Delay / loss / pull a cable: right-click a link")
        hint.setEnabled(False)

    # ------------------------------------------------------------ menus
    def node_menu(self, key: str, gpos):
        item = self.scene.nodes.get(key)
        if not item:
            return
        m = QMenu(self)
        no_console = item.dtype.node_definition in NON_COUNTING_DEFS
        a = m.addAction(icons.icon("utilities-terminal"), "Console")
        a.setEnabled(not no_console)
        a.triggered.connect(lambda: self.open_console(key))
        mg = self.mgmt_of(key)
        if mg or key.startswith("kvm:"):
            sub = m.addMenu(icons.icon("network-connect"), "Management")
            for proto, label in (("ssh", "SSH"), ("telnet", "Telnet"), ("http", "HTTP"), ("https", "HTTPS")):
                sub.addAction(label).triggered.connect(lambda _=False, p=proto: self._open_mgmt(key, p))
        nt = item.nv.tele if item.nv is not None else None
        router = nt is not None and ev(nt.role) == "router"
        if nt is not None and nt.switch is not None and key.startswith("cml:"):
            live = item.state == "BOOTED"
            pr = m.addAction(icons.icon("view-refresh"),
                             "Probe now (output appears in your console)" if nt.console_open else "Probe now")
            pr.setEnabled(live)
            pr.triggered.connect(lambda: self.probe_now(key))
            po = m.addAction("Probe even while my console is open")
            po.setCheckable(True)
            po.setChecked(bool(nt.probe_while_open))
            po.toggled.connect(lambda on: self.set_probe_while_open(key, on))
            if nt.switch.vlans:
                hv = m.addMenu(icons.icon("view-visible"), "Highlight VLAN")
                self._fill_vlan_menu(hv)
        if router:
            sim = m.addMenu(icons.icon("network-disconnect"), "Simulate")
            sim.addAction("Send a packet to the internet (animation)").triggered.connect(
                lambda: self.animate_default_path(key))
            live = key.startswith("cml:") and item.state == "BOOTED"
            for text, fn in (("Traceroute 8.8.8.8", self.traceroute), ("Ping…", self.ping_dialog),
                             ("Verify Internet", self.verify_internet)):
                act = sim.addAction(text)
                act.setEnabled(live)
                act.triggered.connect(lambda _=False, f=fn: f(key))
            if key.startswith("cml:"):
                pr = m.addAction(icons.icon("view-refresh"),
                                 "Probe now (output appears in your console)" if nt.console_open else "Probe now")
                pr.setEnabled(live)
                pr.triggered.connect(lambda: self.probe_now(key))
                po = m.addAction("Probe even while my console is open")
                po.setCheckable(True)
                po.setChecked(bool(nt.probe_while_open))
                po.toggled.connect(lambda on: self.set_probe_while_open(key, on))
        if router and key.startswith("cml:") and item.dtype.vendor == "cisco" and \
                any(n.is_internet for n in self.scene.nodes.values()):
            linked = key in self.cloud_routers()
            ac = m.addAction(icons.icon("network-wired"), "Auto-configure for Internet cloud…" if linked
                             else "Auto-configure for Internet cloud… (link it to the cloud first)")
            ac.setEnabled(linked)
            ac.triggered.connect(lambda: self.auto_configure_cloud(key))
        if item.is_internet:
            h = self.cloud_health
            st = m.addAction(icons.icon("dialog-information"), h.headline if h is not None else "cloud: checking…")
            st.setEnabled(False)
            for rk in self.cloud_routers(key):
                m.addAction(icons.icon("network-wired"), f"Auto-configure {self._label(rk)} for the cloud…").triggered.connect(
                    lambda _=False, k=rk: self.auto_configure_cloud(k))
            m.addAction(icons.icon("view-refresh"), "Check cloud health now").triggered.connect(self.check_cloud)
            if h is not None and h.hint:
                m.addAction(icons.icon("edit-copy"), f"Copy fix command ({h.hint})").triggered.connect(self.copy_cloud_fix)
        if item.kind == "cloud" and self.view_data and any(l.default_hop is not None for l in self.view_data.links):
            m.addAction(icons.icon("view-visible"), "Flash the default paths").triggered.connect(
                lambda: self._node_clicked(key))
        m.addSeparator()
        m.addAction(icons.icon("media-playback-start"), "Start").triggered.connect(lambda: self.start_node(key))
        m.addAction(icons.icon("media-playback-stop"), "Stop").triggered.connect(lambda: self.stop_node(key))
        if key.startswith("cml:"):
            m.addAction(icons.icon("edit-clear-all", "danger"), "Wipe…").triggered.connect(lambda: self.wipe_node(key))
            c = m.addAction(icons.icon("document-edit"), "Edit startup config…")
            c.setEnabled(not no_console)
            c.triggered.connect(lambda: self.edit_config(key))
        m.addSeparator()
        m.addAction(icons.icon("edit-rename"), "Rename… (F2)").triggered.connect(lambda: self.rename_node(key))
        if self.has_show_script(key):
            script = self.show_script_of(key)
            n_cmds = len(script.commands) if script is not None else 0
            m.addAction(icons.icon("document-edit"),
                        f"Show script… ({n_cmds} command{'s' if n_cmds != 1 else ''})" if n_cmds
                        else "Show script…").triggered.connect(lambda: self.edit_show_script(key))
            rs = m.addAction(icons.icon("media-playback-start"), "Run show script now")
            rs.setEnabled(bool(n_cmds) and item.state == "BOOTED")
            rs.triggered.connect(lambda: self.run_show_script(key))
        if item.has_card:
            m.addAction(icons.icon("object-locked"), "Credentials…").triggered.connect(
                lambda: self.edit_credentials(key))
            m.addAction(icons.icon("edit-copy"), "Copy login").triggered.connect(lambda: self.copy_login(key, "user"))
            m.addAction(icons.icon("edit-copy"), "Copy password").triggered.connect(
                lambda: self.copy_login(key, "password"))
        m.addAction(icons.icon("document-properties"), "Properties…").triggered.connect(
            lambda: self.edit_properties(key))
        m.addAction(icons.icon("edit-delete", "danger"), "Delete… (Del)").triggered.connect(self.delete_selected)
        m.exec(gpos)

    def link_menu(self, key: str, gpos):
        m = QMenu(self)
        li = self.scene.links.get(key)
        lv = li.lv if li is not None else None
        cml = key.startswith("cml:")
        if self._tele() is not None and cml:
            cond = m.addMenu(icons.icon("network-wireless"), "Condition (delay / loss)")
            for name in self.condition_presets():
                cond.addAction(name).triggered.connect(lambda _=False, n=name: self.set_condition(key, n))
            active = lv is not None and lv.condition is not None and lv.condition.enabled
            c = m.addAction(icons.icon("edit-clear"), "Clear condition" + (f" ({lv.condition.label})" if active else ""))
            c.setEnabled(active)
            c.triggered.connect(lambda: self.clear_condition(key))
            pulled = lv is not None and lv.status is not None and ev(lv.status.kind) == "CABLE_PULLED"
            if pulled or (li is not None and li.state == "STOPPED"):
                m.addAction(icons.icon("network-connect"), "Plug the cable back in").triggered.connect(
                    lambda: self.restore_cable(key))
            else:
                m.addAction(icons.icon("network-disconnect"), "Pull the cable…").triggered.connect(
                    lambda: self.pull_cable(key))
            m.addAction(icons.icon("activity"), "Send a test packet across (animation)").triggered.connect(
                lambda: self.animate_link(key))
            if li is not None and lv is not None and lv.bundle:
                m.addSeparator()
                bd = self._bundle(lv.bundle)
                po = bd.po_text if bd is not None else "EtherChannel"
                m.addAction(icons.icon("view-visible"), f"Select {po} (EtherChannel)").triggered.connect(
                    lambda: self.select_bundle(lv.bundle))
                m.addAction(icons.icon("edit-delete", "danger"), f"Unbundle {po}…").triggered.connect(
                    lambda: self.unbundle(lv.bundle))
            elif li is not None and self._ec_capable(li):
                keys = self._bundle_candidates(key)
                m.addSeparator()
                a = m.addAction(icons.icon("network-wired"), "Bundle into EtherChannel…"
                                + (f" ({len(keys)} cables)" if len(keys) > 1 else ""))
                a.setEnabled(len(keys) > 1)
                if len(keys) < 2:
                    a.setText("Bundle into EtherChannel… (needs 2+ parallel cables)")
                a.triggered.connect(lambda: self.bundle_dialog(keys))
            pair = self._cloud_router_pair(li.a.key, li.b.key) if li is not None else None
            if pair is not None:
                m.addAction(icons.icon("network-wired"),
                            f"Auto-configure {self._label(pair[0])} for Internet cloud…").triggered.connect(
                    lambda: self.auto_configure_cloud(pair[0]))
            m.addSeparator()
        m.addAction(icons.icon("edit-delete", "danger"), "Delete link…").triggered.connect(self.delete_selected)
        m.exec(gpos)

    # ----------------------------------------------------------- EtherChannel
    def _sync_bundles(self, data: LabData) -> None:
        """One BundleItem per facts.Bundle (lasso + bead + STP marker over the member cables)."""
        tv = data.telemetry
        want = tv.bundles if tv is not None else {}
        for key in list(self.scene.bundles):
            if key not in want:
                it = self.scene.bundles.pop(key)
                self.scene.removeItem(it)
        for key, bd in want.items():
            members = [self.scene.links[lk] for lk in bd.link_keys if lk in self.scene.links]
            a, b = self.scene.nodes.get(bd.a), self.scene.nodes.get(bd.b)
            if not members or a is None or b is None:
                if key in self.scene.bundles:
                    self.scene.removeItem(self.scene.bundles.pop(key))
                continue
            it = self.scene.bundles.get(key)
            if it is None:
                it = BundleItem(key, members, a, b)
                self.scene.addItem(it)
                self.scene.bundles[key] = it
            it.set_bundle(bd, members, a, b)
        self._watch_bundles()

    def _bundle(self, key: str | None):
        tv = self.view_data.telemetry if self.view_data else None
        return tv.bundles.get(key) if (tv is not None and key) else None

    def _ec_capable(self, li) -> bool:
        return (li.key.startswith("cml:") and li.a.kind == "l2switch" and li.b.kind == "l2switch"
                and li.a.key != li.b.key)

    def _bundle_candidates(self, key: str) -> list[str]:
        """The cables 'Bundle into EtherChannel…' starts from: the Ctrl-click selection when it holds 2+ parallel
        links between the same two switches, else every parallel cable between the two switches of ``key`` that is
        not already in a bundle."""
        li = self.scene.links.get(key)
        if li is None:
            return []
        pair = {li.a.key, li.b.key}
        free = [lv.key for lv in (self.view_data.links if self.view_data else [])
                if {lv.a, lv.b} == pair and not lv.bundle and lv.key.startswith("cml:")]
        sel = [k for k in self.selected_link_keys() if k in free]
        return sorted(sel if len(sel) > 1 and key in sel else free,
                      key=lambda k: (self.scene.links[k].label_a if self.scene.links[k].a.key == li.a.key
                                     else self.scene.links[k].label_b))

    def select_bundle(self, key: str) -> None:
        it = self.scene.bundles.get(key)
        if it is None:
            self.notify("That EtherChannel is not on the canvas (yet)", "info", 4000)
            return
        self.scene.clearSelection()
        it.setSelected(True)
        self.view.centerOn(it.geom()[0])
        self.scene.flash([key] + list(self._bundle(key).link_keys if self._bundle(key) else []), T.EC_HUE[0])

    def select_bundle_members(self, key: str) -> None:
        bd = self._bundle(key)
        if bd is None:
            return
        self.scene.clearSelection()
        for lk in bd.link_keys:
            li = self.scene.links.get(lk)
            if li is not None:
                li.setSelected(True)

    def bundle_menu(self, key: str, gpos):
        bd = self._bundle(key)
        m = QMenu(self)
        if bd is not None:
            st = m.addAction(icons.icon("dialog-information"), f"{bd.label} · {ev(bd.state)}")
            st.setEnabled(False)
            if bd.mismatch_short:
                w = m.addAction(icons.icon("dialog-warning"), bd.mismatch_short)
                w.setEnabled(False)
            m.addSeparator()
        m.addAction(icons.icon("edit-select-all"), "Select member cables").triggered.connect(
            lambda: self.select_bundle_members(key))
        m.addAction(icons.icon("view-refresh"), "Probe both switches now").triggered.connect(
            lambda: [self.probe_now(k) for k in ((bd.a, bd.b) if bd is not None else ())])
        m.addSeparator()
        m.addAction(icons.icon("edit-delete", "danger"), "Unbundle…").triggered.connect(lambda: self.unbundle(key))
        m.exec(gpos)

    def _confirm_consoles(self, sides: list[tuple[str, bool]]) -> bool | None:
        """Ask before typing on an open user console.  True = type, False = day-0 only, None = cancel."""
        opened = [lbl for lbl, op in sides if op]
        if not opened:
            return True
        who = " and ".join(opened)
        return confirm(self, "Console open", f"{who}'s console is open. Type the configuration on that console "
                       "line now?", "Type on Console", destructive=False, cancel="Day-0 Only",
                       detail="“Day-0 Only” updates only the CML day-0 configs.")

    def bundle_dialog(self, link_keys: list[str]):
        if len(link_keys) < 2:
            self.notify("Select two or more parallel cables between the same two switches (Ctrl-click)", "info")
            return
        self.log("EtherChannel: planning the bundle…", tag="ETHERCHANNEL", hue=T.EC_HUE[0])

        def show(plan):
            from .dialogs import BundleDialog
            dlg = BundleDialog(plan, self)
            if dlg.exec() != QDialog.Accepted:
                self.log("EtherChannel: cancelled")
                return
            ch = dlg.choices()
            if ch["live"]:
                ok = self._confirm_consoles([(plan.a.label, plan.a.console_open), (plan.b.label, plan.b.console_open)])
                if ok is False:
                    ch["live"] = False
            name = f"Po{plan.po} {plan.a.label} ↔ {plan.b.label}"
            self.log(f"EtherChannel {name}: {plan.protocol.upper()} {plan.a.mode}/{plan.b.mode} on "
                     f"{len(plan.a.members)} cables…", tag="ETHERCHANNEL", hue=T.EC_HUE[0])

            def done(steps):
                for ok_, text in steps:
                    self.log(f"EtherChannel {name}: {text}", "ok" if ok_ else "warn", tag="ETHERCHANNEL",
                             hue=T.EC_HUE[0] if ok_ else T.AMBER)
                if ch["live"] and all(ok_ for ok_, _ in steps[:2]):
                    self._watch[frozenset(plan.link_keys)] = (time.time(), name, "bundle", plan.check.forms)
                    self.notify(f"{name}: applied - watching until the bundle forms…", "info", 5000)
                self.poll()
            self.runner.submit(self.ctl.bundle_apply, plan, ch["live"], ch["day0"], on_done=done,
                               on_error=self.on_error("EtherChannel", True))
        self.runner.submit(self.ctl.bundle_plan, list(link_keys), on_done=show,
                           on_error=self.on_error("EtherChannel", True))

    def unbundle(self, key: str):
        def show(plan):
            sides = plan["sides"]
            if not sides:
                self.notify("Nothing to unbundle: no channel-group on either side", "info")
                return
            cmds = "\n\n".join(f"! {s['label']}\n" + "\n".join(s["commands"]) for s in sides)
            if not confirm(self, "Unbundle", f"Unbundle {plan['label']}? The member cables become separate links "
                           "again (spanning tree will block all but one).", "Unbundle",
                           detail=cmds + "\n\nThe CML day-0 configs are updated too."):
                return
            live = self._confirm_consoles([(s["label"], s["console_open"]) for s in sides])
            if live is None:
                return

            def done(steps):
                for ok_, text in steps:
                    self.log(f"Unbundle {plan['label']}: {text}", "ok" if ok_ else "warn", tag="ETHERCHANNEL",
                             hue=T.EC_HUE[0] if ok_ else T.AMBER)
                self._watch[frozenset(plan["link_keys"])] = (time.time(), plan["label"], "unbundle", True)
                self.poll()
            self.runner.submit(self.ctl.unbundle_apply, plan, bool(live), True, on_done=done,
                               on_error=self.on_error("unbundle", True))
        self.runner.submit(self.ctl.unbundle_plan, key, on_done=show, on_error=self.on_error("unbundle", True))

    def _watch_bundles(self) -> None:
        """After Apply: report when the bundle forms (or that it cannot), and when an unbundle is complete."""
        if not getattr(self, "_watch", None):
            return
        tv = self.view_data.telemetry if self.view_data else None
        if tv is None:
            return
        now = time.time()
        for ident, (t0, name, what, forms) in list(self._watch.items()):
            bd = next((b for b in tv.bundles.values() if frozenset(b.link_keys) == ident), None)
            if what == "bundle" and bd is not None and ev(bd.state) == "up":
                self.log(f"EtherChannel {name}: formed - {bd.label} ({now - t0:.0f} s)", "ok", tag="ETHERCHANNEL",
                         hue=T.EC_HUE[0])
                self.notify(f"{bd.label} formed", "ok", 5000)
                self._watch.pop(ident)
            elif what == "bundle" and bd is not None and ev(bd.state) == "mismatch" and not forms:
                self.log(f"EtherChannel {name}: configured, and as predicted it does not bundle: {bd.mismatch_short}",
                         "warn", tag="ETHERCHANNEL", hue=T.AMBER)
                self._watch.pop(ident)
            elif what == "unbundle" and bd is None:
                self.log(f"Unbundle {name}: done - the cables are separate links again ({now - t0:.0f} s)", "ok",
                         tag="ETHERCHANNEL", hue=T.EC_HUE[0])
                self._watch.pop(ident)
            elif now - t0 > 150:
                self.log(f"{'EtherChannel' if what == 'bundle' else 'Unbundle'} {name}: no result after 150 s"
                         + (f" (state {ev(bd.state)}: {bd.explain})" if bd is not None else ""), "warn",
                         tag="ETHERCHANNEL", hue=T.AMBER)
                self._watch.pop(ident)

    def canvas_menu(self, spos, gpos):
        if not self.ctl.lab_id:
            return
        m = QMenu(self)
        sub = m.addMenu("Add device here")
        for d in DEVICE_TYPES:
            a = sub.addAction(QIcon(device_pixmap(d, 24)), d.title)
            a.triggered.connect(lambda _=False, k=d.key: self.on_device_dropped(k, spos.x(), spos.y()))
            if unavailable_reason(d):
                a.setEnabled(False)
                a.setToolTip(unavailable_reason(d))
        m.addAction("Fit view").triggered.connect(self.view.fit_all)
        lm = m.addMenu("Layers")
        self._fill_layers_menu(lm)
        m.exec(gpos)

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        for w in (self.infobar, self.notifybar, self.empty):
            if w.isVisible():
                w.reposition()

    def closeEvent(self, ev):
        self._save_layout()
        self._closing = True
        self.timer.stop()
        self.ticker.stop()
        self.ram_timer.stop()
        self.cloud_timer.stop()
        if self.ctl.telemetry is not None:
            self.ctl.telemetry.on_change = None
        try:
            self.ctl.shutdown()                 # kill in-flight console ssh, stop probe threads
        except Exception:  # noqa: BLE001
            pass
        self.runner.wait(3000)
        super().closeEvent(ev)
