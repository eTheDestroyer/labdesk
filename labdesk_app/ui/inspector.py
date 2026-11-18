"""Inspector dock (L4): a header card (glyph, name, kind line, StatusPill, 'updated 3 s ago'), visible primary
actions (Console · Start/Stop · Config… · ⋯) and per-kind tabs:

    router  Overview | Interfaces | Routing | Protocols | Config
    switch  Overview | VLANs | Ports | STP | MAC | EtherChannel | Config
    host    Overview | Config
    cloud   Overview

Tabs are hidden with ``QTabBar.setTabVisible`` (page widgets are kept; indices stay stable) and depend only
on the node *kind*, never on probe data: a probe that adds or removes data changes a tab's contents (with an
empty state), never which tabs exist.  The last tab is remembered by name per kind.

Selecting a link shows the link page: header 'R1 Gi0/1 ↔ R3 Gi0/0', endpoint mini-cards side by side
(stacked below 360 px), protocols on the link, the traffic sparkline and the simulation controls in a
FlowLayout.  With nothing selected the idle page shows the lab summary (same counters as the status bar)
and a dismissible Tips card whose key chips come from the registered QAction shortcuts.

Selecting an EtherChannel (its bead or lasso on the canvas) shows the EtherChannel page: Po id, protocol and the
modes of both sides, load-balance, STP role/state, trunk VLANs, mismatch reasons and a member table.

Only reads the telemetry model; every console action goes through MainWindow (TaskRunner).
"""
from __future__ import annotations

import html
import time

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QColor, QGuiApplication, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (QAbstractItemView, QBoxLayout, QCheckBox, QComboBox, QFrame, QGridLayout,
                               QHBoxLayout, QHeaderView, QLabel, QMenu, QPlainTextEdit, QPushButton, QScrollArea,
                               QSizePolicy, QStackedWidget, QTableWidget, QTableWidgetItem, QTabWidget, QToolButton,
                               QVBoxLayout, QWidget)

from ..catalog import NON_COUNTING_DEFS
from ..settings import app_settings, as_bool
from . import icons
from . import theme as T
from .scene_model import SIDE_WORDS, ev, link_header
from .theme import STATE_COLORS, STATE_TEXT
from .widgets import Card, FlowLayout, KeyChip, Sparkline, StatusPill, status_badge_html

SIDE_COLORS = {"UP": T.LINK_UP, "ADMIN_DOWN": T.DOWN_RED, "DOWN": T.DOWN_RED, "PROTO_DOWN": T.PROTO_ORANGE,
               "UNKNOWN": T.NEUTRAL, "NOT_APPLICABLE": T.NEUTRAL}
SIDE_GLYPH = {"UP": "up", "ADMIN_DOWN": "admin_down", "DOWN": "down", "PROTO_DOWN": "proto_down",
              "UNKNOWN": "neutral", "NOT_APPLICABLE": "neutral"}
SIDE_TONE = {"UP": "up", "ADMIN_DOWN": "down", "DOWN": "down", "PROTO_DOWN": "proto"}
KIND_WORDS = {"UP": ("UP", T.LINK_UP), "ADMIN_DOWN": ("ADMIN DOWN", T.DOWN_RED), "LINK_DOWN": ("LINK DOWN", T.DOWN_RED),
              "PROTOCOL_DOWN": ("PROTOCOL DOWN", T.PROTO_ORANGE), "CABLE_PULLED": ("CABLE PULLED", T.DOWN_RED),
              "LINK_NOT_STARTED": ("NOT STARTED", T.NEUTRAL), "UNKNOWN": ("UNKNOWN", T.NEUTRAL),
              "LAB_OFF": ("LAB OFF", T.NEUTRAL), "NODE_OFF": ("NODE OFF", T.NEUTRAL)}
KIND_TONE = {"UP": ("up", "up"), "ADMIN_DOWN": ("down", "admin_down"), "LINK_DOWN": ("down", "down"),
             "PROTOCOL_DOWN": ("proto", "proto_down"), "CABLE_PULLED": ("down", "down")}
PATH_WORDS = {"EGRESS": ("reaches the internet exit", T.ROUTE_BLUE),
              "VERIFIED_INTERNET": ("internet verified", T.LINK_UP),
              "BROKEN_AT": ("broken", T.DOWN_RED), "NO_EGRESS": ("no exit", T.AMBER), "NONE": ("no default route", T.NEUTRAL),
              "BLACKHOLE": ("blackhole (Null0)", T.DOWN_RED), "UNRESOLVED": ("next hop unresolved", T.AMBER),
              "LOOP": ("routing loop", T.DOWN_RED), "STALE": ("data too old", T.NEUTRAL), "UNKNOWN": ("learning…", T.NEUTRAL)}
NODE_TONE = {"BOOTED": ("up", "booted"), "running": ("up", "booted"), "STARTED": ("neutral", "booting"),
             "QUEUED": ("neutral", "booting"), "booting": ("neutral", "booting"), "STOPPED": ("neutral", "stopped"),
             "stopped": ("neutral", "stopped"), "DEFINED_ON_CORE": ("neutral", "stopped"),
             "DISCONNECTED": ("down", "disconnected"), "missing": ("down", "disconnected"),
             "unknown": ("down", "disconnected")}

# tab indices (stable: pages are hidden, never removed)
(TAB_OVERVIEW, TAB_IFACES, TAB_ROUTING, TAB_PROTOCOLS, TAB_VLANS, TAB_PORTS, TAB_STP, TAB_MAC, TAB_EC,
 TAB_CONFIG) = range(10)
KIND_TABS = {"router": {TAB_OVERVIEW, TAB_IFACES, TAB_ROUTING, TAB_PROTOCOLS, TAB_CONFIG},
             "switch": {TAB_OVERVIEW, TAB_VLANS, TAB_PORTS, TAB_STP, TAB_MAC, TAB_EC, TAB_CONFIG},
             "host": {TAB_OVERVIEW, TAB_CONFIG},
             "cloud": {TAB_OVERVIEW}}


def node_kind(dtype) -> str:
    """router | switch | host | cloud - from the device type only (never from probe data)."""
    if dtype.shape in ("router", "firewall"):
        return "router"
    if dtype.shape == "switch" and dtype.node_definition not in NON_COUNTING_DEFS:
        return "switch"
    if dtype.shape == "host":
        return "host"
    return "cloud"                    # Internet cloud, NAT / bridge connectors, unmanaged switch


def _bar_icon(color: str) -> QIcon:
    pm = QPixmap(6, 18)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.fillRect(0, 0, 4, 18, QColor(color))
    p.end()
    return QIcon(pm)


_ROLES = (Qt.DisplayRole, Qt.ForegroundRole, Qt.FontRole, Qt.ToolTipRole, Qt.UserRole, Qt.TextAlignmentRole)


class LiveTable(QTableWidget):
    """QTableWidget that updates in place: between ``begin()`` and ``commit()`` the fill code's
    rowCount / insertRow / setItem calls go to a buffer; ``commit()`` then changes only the cells that
    differ (dataChanged, no rebuild, selection and scroll position survive).  Columns are resized to their
    contents only on a reset (row count or node changed), and stay user-resizable."""

    def __init__(self, rows: int, cols: int):
        super().__init__(rows, cols)
        self._buf: list | None = None
        self._key = None

    def begin(self, key=None):
        self._buf = []
        self._next_key = key

    def rowCount(self):  # noqa: N802
        return len(self._buf) if self._buf is not None else super().rowCount()

    def insertRow(self, r):  # noqa: N802
        if self._buf is None:
            return super().insertRow(r)
        self._buf.insert(r, {})

    def setItem(self, r, c, it):  # noqa: N802
        if self._buf is None:
            return super().setItem(r, c, it)
        self._buf[r][c] = it

    def commit(self):
        rows, self._buf = self._buf or [], None
        reset = len(rows) != super().rowCount() or self._next_key != self._key
        self._key = self._next_key
        if len(rows) != super().rowCount():
            super().setRowCount(len(rows))
        for r, row in enumerate(rows):
            for c in range(self.columnCount()):
                new = row.get(c)
                old = self.item(r, c)
                if new is None:
                    if old is not None:
                        self.takeItem(r, c)
                    continue
                if not new.toolTip() and new.text():
                    new.setToolTip(new.text())
                if old is None:
                    super().setItem(r, c, new)
                    continue
                for role in _ROLES:
                    nv = new.data(role)
                    if nv != old.data(role):
                        old.setData(role, nv)
                ni, oi = new.icon(), old.icon()
                if ni.cacheKey() != oi.cacheKey() and not (ni.isNull() and oi.isNull()):
                    old.setIcon(ni)
        if reset:
            self.resizeColumnsToContents()
            hh = self.horizontalHeader()
            avail = self.viewport().width() or self.width()
            if avail > 0 and hh.length() < avail and self.columnCount():
                hh.setStretchLastSection(True)
        self.viewport().update()


def _table(headers: list[str]) -> LiveTable:
    t = LiveTable(0, len(headers))
    t.setObjectName("insptable")
    t.setHorizontalHeaderLabels([h.upper() for h in headers])
    t.verticalHeader().setVisible(False)
    t.setEditTriggers(QAbstractItemView.NoEditTriggers)
    t.setSelectionBehavior(QAbstractItemView.SelectRows)
    t.setSelectionMode(QAbstractItemView.SingleSelection)
    t.setShowGrid(False)
    t.setAlternatingRowColors(True)
    hh = t.horizontalHeader()
    hh.setStretchLastSection(True)
    hh.setSectionResizeMode(QHeaderView.Interactive)
    hh.setDefaultAlignment(Qt.AlignLeft | Qt.AlignVCenter)
    hh.setHighlightSections(False)
    hh.setFont(T.type("caption", upper=True))
    hh.setMinimumSectionSize(36)
    t.verticalHeader().setDefaultSectionSize(24)
    t.verticalHeader().setSectionResizeMode(QHeaderView.Fixed)
    t.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
    t.setHorizontalScrollMode(QAbstractItemView.ScrollPerPixel)
    t.setWordWrap(False)
    t.setFont(T.type("small"))
    return t


def _scroll(w: QWidget) -> QScrollArea:
    s = QScrollArea()
    s.setWidgetResizable(True)
    s.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
    s.setFrameShape(QScrollArea.NoFrame)
    s.setWidget(w)
    return s


def _rich(text: str = "") -> QLabel:
    lab = QLabel(text)
    lab.setWordWrap(True)
    lab.setTextFormat(Qt.RichText)
    lab.setAlignment(Qt.AlignTop | Qt.AlignLeft)
    lab.setTextInteractionFlags(Qt.TextSelectableByMouse | Qt.LinksAccessibleByMouse)
    lab.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
    lab.setMinimumWidth(10)
    return lab


def _muted(text: str = "") -> QLabel:
    lab = QLabel(text)
    lab.setObjectName("muted")
    lab.setWordWrap(True)
    lab.setFont(T.type("small"))
    return lab


def _age_html(age) -> str:
    return f"<span style='color:{T.tokens().N(11).name()}'>updated {T.fmt_age(age)}</span>" if age is not None else ""


def _gray() -> str:
    return T.tokens().N(11).name()


class KVGrid(QWidget):
    """Key / value rows: key small N11, value body N12 (IPs in mono)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.g = QGridLayout(self)
        self.g.setContentsMargins(0, 0, 0, 0)
        self.g.setHorizontalSpacing(12)
        self.g.setVerticalSpacing(4)
        self.g.setColumnStretch(1, 1)
        self._rows: list[tuple[QLabel, QLabel]] = []

    def set_rows(self, rows: list[tuple[str, str, bool]]) -> None:
        """rows: (key, value, mono)."""
        while len(self._rows) < len(rows):
            k, v = QLabel(), QLabel()
            k.setObjectName("muted")
            k.setFont(T.type("small"))
            k.setAlignment(Qt.AlignLeft | Qt.AlignTop)
            v.setWordWrap(True)
            v.setTextInteractionFlags(Qt.TextSelectableByMouse)
            v.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
            i = len(self._rows)
            self.g.addWidget(k, i, 0)
            self.g.addWidget(v, i, 1)
            self._rows.append((k, v))
        for i, (k, v) in enumerate(self._rows):
            on = i < len(rows)
            k.setVisible(on)
            v.setVisible(on)
            if on:
                key, val, mono = rows[i]
                k.setText(key)
                v.setText(val)
                v.setFont(T.mono(0.9) if mono else T.type("body"))


class _SummaryRow(QPushButton):
    """One idle-summary row: label · glyph + value; clickable (selects the first affected item) when unhealthy."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFlat(True)
        self.setObjectName("summaryrow")
        self.setMinimumHeight(28)
        h = QHBoxLayout(self)
        h.setContentsMargins(8, 0, 8, 0)
        self.label = QLabel()
        self.label.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.pill = StatusPill("", "neutral", None, height=20, tabular=True)
        self.chev = QLabel()
        self.chev.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.chev.setFixedWidth(12)
        h.addWidget(self.label, 1)
        h.addWidget(self.pill)
        h.addWidget(self.chev)
        self.key = None
        self.link = False

    def set_row(self, row):
        self.label.setText(row.label)
        glyph = {"up": "up", "down": "down", "warn": "warn", "proto": "proto_down"}.get(row.tone, "neutral")
        self.pill.set_state(row.value, row.tone, glyph)
        self.pill.updateGeometry()
        self.key, self.link = row.key, row.link
        click = row.key is not None
        self.setEnabled(True)
        self.setCursor(Qt.PointingHandCursor if click else Qt.ArrowCursor)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, not click)
        self.setFocusPolicy(Qt.TabFocus if click else Qt.NoFocus)
        self.chev.setPixmap(icons.pixmap("arrow-right", 12, T.tokens().N(11), self.devicePixelRatioF() or 1.0)
                            if click else QPixmap())
        self.setToolTip((row.tip + "\n" if row.tip else "") + ("Click to select it on the canvas" if click else ""))

    def sizeHint(self):
        return QSize(200, 28)


class _EndCard(QFrame):
    """Link endpoint mini-card: node · interface (mono) · address (mono) · side status."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("card")
        v = QVBoxLayout(self)
        v.setContentsMargins(12, 10, 12, 10)
        v.setSpacing(2)
        self.node = QLabel()
        self.node.setFont(T.type("label"))
        self.node.setWordWrap(True)
        self.iface = QLabel()
        self.iface.setFont(T.mono(0.95))
        self.iface.setWordWrap(True)
        self.ip = QLabel()
        self.ip.setFont(T.mono(0.9, "small"))
        self.ip.setObjectName("muted")
        self.ip.setWordWrap(True)
        self.desc = _muted()
        self.pill = StatusPill("", "neutral", None, height=20)
        v.addWidget(self.node)
        v.addWidget(self.iface)
        v.addWidget(self.ip)
        v.addSpacing(4)
        v.addWidget(self.pill, 0, Qt.AlignLeft)
        v.addWidget(self.desc)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)

    def set_end(self, node: str, iface: str, ip: str, side: str | None, desc: str = ""):
        self.node.setText(node)
        self.iface.setText(iface or "—")
        self.ip.setText(ip or "no address seen")
        word = SIDE_WORDS.get(side, "no data") if side else "no data"
        self.pill.set_state(word, SIDE_TONE.get(side, "neutral"), SIDE_GLYPH.get(side, "neutral"))
        self.desc.setText(desc)
        self.desc.setVisible(bool(desc))


class _Ends(QWidget):
    """Two endpoint cards side by side; stacked below 360 px."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.a, self.b = _EndCard(), _EndCard()
        self.box = QBoxLayout(QBoxLayout.LeftToRight, self)
        self.box.setContentsMargins(0, 0, 0, 0)
        self.box.setSpacing(8)
        self.box.addWidget(self.a, 1)
        self.box.addWidget(self.b, 1)

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        d = QBoxLayout.TopToBottom if self.width() < 340 else QBoxLayout.LeftToRight
        if self.box.direction() != d:
            self.box.setDirection(d)


class Inspector(QWidget):
    """Right-hand panel.  ``win`` is the MainWindow (actions, view data, runner)."""

    def __init__(self, win):
        super().__init__()
        self.win = win
        self.setMinimumWidth(300)              # never let content widen the dock (it wraps instead)
        self.key: str | None = None            # selected node key
        self.link_key: str | None = None       # selected link key
        self.kind_now: str | None = None
        self.cards: list[Card] = []
        self._data_t0 = time.time()
        self._age_s: float | None = None
        self._age_t0 = 0.0
        self.settings = app_settings()
        v = QVBoxLayout(self)
        v.setContentsMargins(12, 12, 12, 12)
        v.setSpacing(12)
        self._build_header(v)
        self.stack = QStackedWidget()
        v.addWidget(self.stack, 1)
        self._build_idle()                     # page 0
        self.node_page = QWidget()             # page 1
        nv = QVBoxLayout(self.node_page)
        nv.setContentsMargins(0, 0, 0, 0)
        self.tabs = QTabWidget()
        self.tabs.setObjectName("insptabs")
        self.tabs.setDocumentMode(True)
        tb = self.tabs.tabBar()
        tb.setExpanding(False)
        tb.setElideMode(Qt.ElideNone)
        tb.setUsesScrollButtons(True)
        tb.setDrawBase(False)
        tb.setFont(T.type("small"))
        nv.addWidget(self.tabs, 1)
        self._build_overview()
        self._build_interfaces()
        self._build_routing()
        self._build_protocols()
        self._build_switch_tabs()
        self._build_config()
        self._router_tabs = [self.tabs.widget(i) for i in (1, 2, 3)]
        self.tabs.currentChanged.connect(self._tab_changed)
        self.stack.addWidget(self.node_page)
        self._build_link_page()                # page 2
        self._build_bundle_page()              # page 3
        # 'updated N s ago' ticks from MainWindow's one shared 1 s timer (tick(now)); a local fallback only
        # when the inspector is used without a MainWindow ticker
        if getattr(win, "ticker", None) is None:
            from PySide6.QtCore import QTimer
            self.ticker = QTimer(self)
            self.ticker.setInterval(1000)
            self.ticker.timeout.connect(self._tick)
        else:
            self.ticker = None
        self.show_none()

    # ----------------------------------------------------------------- header card
    def _build_header(self, v):
        self.head = QFrame()
        self.head.setObjectName("card")
        hv = QVBoxLayout(self.head)
        hv.setContentsMargins(12, 12, 12, 12)
        hv.setSpacing(8)
        top = QHBoxLayout()
        top.setSpacing(10)
        self.glyph = QLabel()
        self.glyph.setFixedSize(28, 28)
        top.addWidget(self.glyph, 0, Qt.AlignTop)
        tv = QVBoxLayout()
        tv.setSpacing(0)
        self.title = QLabel("No selection")
        self.title.setFont(T.type("title"))
        self.title.setWordWrap(True)
        self.title.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.title.setMinimumWidth(10)
        self.kind = _rich()
        self.kind.setFont(T.type("small"))
        tv.addWidget(self.title)
        tv.addWidget(self.kind)
        top.addLayout(tv, 1)
        self.copy_btn = QToolButton()
        self.copy_btn.setIcon(icons.icon("edit-copy"))
        self.copy_btn.setIconSize(QSize(16, 16))
        self.copy_btn.setToolTip("Copy as text")
        self.copy_btn.setAutoRaise(True)
        self.copy_btn.clicked.connect(self.copy_as_text)
        top.addWidget(self.copy_btn, 0, Qt.AlignTop)
        hv.addLayout(top)
        srow = QHBoxLayout()
        srow.setSpacing(8)
        self.pill = StatusPill("", "neutral", None)
        srow.addWidget(self.pill)
        self.state = _rich()
        self.state.setFont(T.type("small"))
        srow.addWidget(self.state, 1)
        self.age = QLabel("")
        self.age.setObjectName("muted")
        self.age.setFont(T.tabular("small"))
        self.age.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        srow.addWidget(self.age)
        hv.addLayout(srow)
        # action row
        self.actions_w = QWidget()
        ar = QHBoxLayout(self.actions_w)
        ar.setContentsMargins(0, 2, 0, 0)
        ar.setSpacing(6)
        win = self.win
        self.console_btn = QPushButton(icons.icon("terminal", "onaccent"), "Console")
        self.console_btn.setProperty("role", "primary")
        self.console_btn.setToolTip("Serial console in Konsole (Enter / double-click)")
        self.console_btn.clicked.connect(lambda: self.key and win.open_console(self.key))
        self.startstop_btn = QPushButton("Start")
        self.startstop_btn.clicked.connect(self._start_stop)
        self.config_btn = QPushButton(icons.icon("document-edit"), "Config…")
        self.config_btn.setToolTip("Edit the startup (day-0) configuration")
        self.config_btn.clicked.connect(lambda: self.key and win.edit_config(self.key))
        self.health_btn = QPushButton(icons.icon("view-refresh"), "Check health now")
        self.health_btn.clicked.connect(lambda: win.check_cloud())
        self.more_btn = QToolButton()
        self.more_btn.setObjectName("more")
        self.more_btn.setIcon(icons.icon("overflow-menu"))
        self.more_btn.setIconSize(QSize(16, 16))
        self.more_btn.setToolTip("More actions")
        self.more_btn.setPopupMode(QToolButton.InstantPopup)
        self.more_btn.setAutoRaise(True)
        self.more_menu = QMenu(self)
        self.more_menu.aboutToShow.connect(self._fill_more)
        self.more_btn.setMenu(self.more_menu)
        for b in (self.console_btn, self.startstop_btn, self.config_btn, self.health_btn):
            b.setMinimumHeight(28)
            b.setIconSize(QSize(16, 16))
            ar.addWidget(b)
        ar.addStretch(1)
        ar.addWidget(self.more_btn)
        hv.addWidget(self.actions_w)
        # compat: older code reads inspector.buttons[name]
        self.buttons = {"Console": self.console_btn, "Start": self.startstop_btn, "Stop": self.startstop_btn,
                        "Config…": self.config_btn}
        v.addWidget(self.head)

    def _start_stop(self):
        if not self.key:
            return
        if self.startstop_btn.property("verb") == "stop":
            self.win.stop_node(self.key)
        else:
            self.win.start_node(self.key)

    def _fill_more(self):
        m = self.more_menu
        m.clear()
        if not self.key:
            return
        win, key = self.win, self.key
        cml = key.startswith("cml:")
        kind = self.kind_now
        if cml and kind != "cloud" or (cml and kind == "cloud" and not self._is_internet()):
            a = m.addAction(icons.icon("edit-clear-all", "danger"), "Wipe…")
            a.setEnabled(cml and kind != "cloud")
            a.triggered.connect(lambda: win.wipe_node(key))
        item = win.scene.nodes.get(key)
        if item is not None and item.has_card:
            m.addAction(icons.icon("object-locked"), "Credentials…").triggered.connect(
                lambda: win.edit_credentials(key))
        m.addAction(icons.icon("document-properties"), "Properties…").triggered.connect(
            lambda: win.edit_properties(key))
        r = m.addAction(icons.icon("edit-rename"), "Rename")
        r.setShortcut("F2")
        r.triggered.connect(lambda: win.rename_node(key))
        m.addSeparator()
        d = m.addAction(icons.icon("edit-delete", "danger"), "Delete")
        d.setShortcut("Del")
        d.triggered.connect(win.delete_selected)

    def _is_internet(self) -> bool:
        item = self.win.scene.nodes.get(self.key) if self.key else None
        return bool(item is not None and getattr(item, "is_internet", False))

    # ----------------------------------------------------------------- idle page (L6)
    def _build_idle(self):
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(12)
        self.sum_card = QFrame()
        self.sum_card.setObjectName("card")
        sv = QVBoxLayout(self.sum_card)
        sv.setContentsMargins(12, 12, 12, 8)
        sv.setSpacing(2)
        hd = QHBoxLayout()
        self.sum_title = QLabel("Lab")
        self.sum_title.setFont(T.type("label"))
        self.sum_title.setWordWrap(True)
        self.sum_pill = StatusPill("", "neutral", None, height=20)
        hd.addWidget(self.sum_title, 1)
        hd.addWidget(self.sum_pill)
        sv.addLayout(hd)
        self.sum_sub = _muted("")
        sv.addWidget(self.sum_sub)
        sv.addSpacing(6)
        self.sum_rows: list[_SummaryRow] = []
        self.sum_box = QVBoxLayout()
        self.sum_box.setSpacing(0)
        sv.addLayout(self.sum_box)
        v.addWidget(self.sum_card)
        # tips
        self.tips_card = QFrame()
        self.tips_card.setObjectName("card")
        tv = QVBoxLayout(self.tips_card)
        tv.setContentsMargins(12, 10, 8, 12)
        tv.setSpacing(6)
        th = QHBoxLayout()
        tl = QLabel("Tips")
        tl.setFont(T.type("caption", upper=True))
        tl.setObjectName("muted")
        th.addWidget(tl, 1)
        x = QToolButton()
        x.setIcon(icons.icon("window-close", "muted"))
        x.setIconSize(QSize(12, 12))
        x.setAutoRaise(True)
        x.setToolTip("Dismiss the tips (they do not come back)")
        x.clicked.connect(self._dismiss_tips)
        th.addWidget(x)
        tv.addLayout(th)
        self.tips_grid = QGridLayout()
        self.tips_grid.setHorizontalSpacing(10)
        self.tips_grid.setVerticalSpacing(6)
        self.tips_grid.setColumnStretch(1, 1)
        tv.addLayout(self.tips_grid)
        self.tips_mouse = _muted("")
        self.tips_mouse.setTextFormat(Qt.RichText)
        tv.addSpacing(4)
        tv.addWidget(self.tips_mouse)
        v.addWidget(self.tips_card)
        self.tips_card.setVisible(not as_bool(self.settings.value("inspector/tips_dismissed", False)))
        v.addStretch(1)
        self._tips_built = False
        self.stack.addWidget(_scroll(w))

    def _dismiss_tips(self):
        self.settings.setValue("inspector/tips_dismissed", True)
        self.tips_card.setVisible(False)

    def build_tips(self, actions) -> None:
        """Key chips from the registered QActions only (so a tip never names a shortcut that does not exist)."""
        while self.tips_grid.count():
            it = self.tips_grid.takeAt(0)
            if it.widget() is not None:
                it.widget().deleteLater()
        row = 0
        for a, text in actions:
            sc = a.shortcut()
            if sc.isEmpty():
                continue
            keys = QWidget()
            kh = QHBoxLayout(keys)
            kh.setContentsMargins(0, 0, 0, 0)
            kh.setSpacing(3)
            names = {"Return": "Enter", "Del": "Del", "Esc": "Esc"}
            for part in sc.toString().split("+") if sc.toString() != "+" else ["+"]:
                kh.addWidget(KeyChip(names.get(part, part) if part else "+"))
            kh.addStretch(1)
            lab = QLabel(text)
            lab.setFont(T.type("small"))
            lab.setWordWrap(True)
            self.tips_grid.addWidget(keys, row, 0)
            self.tips_grid.addWidget(lab, row, 1)
            row += 1
        g = _gray()
        self.tips_mouse.setText(f"<b style='color:{T.tokens().N(12).name()}'>Double-click</b> a device: console · "
                                f"<b style='color:{T.tokens().N(12).name()}'>Shift-drag</b> device → device: link · "
                                f"<b style='color:{T.tokens().N(12).name()}'>Right-click</b>: start / stop, simulate, "
                                f"delay / loss · <b style='color:{T.tokens().N(12).name()}'>Ctrl+wheel</b> zoom"
                                .replace("color:" + g, "color:" + g))
        self._tips_built = True

    def show_summary(self, summary, has_lab: bool) -> None:
        self.sum_card.setVisible(True)
        if not has_lab:
            self.sum_title.setText("No lab open")
            self.sum_pill.setVisible(False)
            self.sum_sub.setText("Open a lab (Ctrl+O) to see its health here.")
            rows = []
        else:
            self.sum_title.setText(summary.title)
            st = summary.lab_state
            on = (st or "").upper() in ("STARTED", "BOOTED")
            self.sum_pill.set_state(T.state_label(st), "up" if on else "neutral", "up" if on else "stopped")
            self.sum_pill.setVisible(True)
            self.sum_sub.setText(f"{summary.nodes_total} devices · {summary.links_total} links · "
                                 "select a device or a link for details")
            rows = summary.rows
        while len(self.sum_rows) < len(rows):
            r = _SummaryRow()
            r.clicked.connect(lambda _=False, rr=r: self._summary_clicked(rr))
            self.sum_box.addWidget(r)
            self.sum_rows.append(r)
        for i, r in enumerate(self.sum_rows):
            r.setVisible(i < len(rows))
            if i < len(rows):
                r.set_row(rows[i])

    def _summary_clicked(self, row):
        if row.key is None:
            return
        win = self.win
        win.scene.clearSelection()
        item = (win.scene.links if row.link else win.scene.nodes).get(row.key)
        if item is not None:
            item.setSelected(True)
            win.view.center_on_item(item)

    # ----------------------------------------------------------------- build
    def _build_overview(self):
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 10, 0, 4)
        v.setSpacing(10)
        self.ov_summary = _rich()
        self.ov_summary.setFont(T.type("body"))
        v.addWidget(self.ov_summary)
        self.ov_facts = Card("Facts")
        self.kv = KVGrid()
        self.ov_facts.v.addWidget(self.kv)
        v.addWidget(self.ov_facts)
        self.ov_card = Card("Details")
        self.ov_body = _rich()
        self.ov_card.v.addWidget(self.ov_body)
        v.addWidget(self.ov_card)
        row = QHBoxLayout()
        self.probe_btn = QPushButton(icons.icon("view-refresh"), "Probe now")
        self.probe_btn.setToolTip("Read the router's state over its console right now "
                                  "(output appears in any open console of this router)")
        self.probe_btn.clicked.connect(lambda: self.key and self.win.probe_now(self.key))
        self.probe_open = QCheckBox("Probe even while my console is open")
        self.probe_open.setToolTip("LabDesk normally pauses reading a router while you have its console open.\n"
                                   "With this on, its probe commands appear in your console.")
        self.probe_open.clicked.connect(lambda on: self.key and self.win.set_probe_while_open(self.key, on))
        row.addWidget(self.probe_btn)
        self.creds_btn = QPushButton(icons.icon("object-locked"), "Credentials…")
        self.creds_btn.setToolTip("Username, password and enable secret LabDesk uses on this device "
                                  "(stored in device-creds, mode 0600)")
        self.creds_btn.clicked.connect(lambda: self.key and self.win.edit_credentials(self.key))
        row.addWidget(self.creds_btn)
        self.copy_user_btn = QPushButton(icons.icon("edit-copy"), "User")
        self.copy_user_btn.setToolTip("Copy the login username to the clipboard")
        self.copy_user_btn.clicked.connect(lambda: self.key and self.win.copy_login(self.key, "user"))
        self.copy_pw_btn = QPushButton(icons.icon("edit-copy"), "Password")
        self.copy_pw_btn.setToolTip("Copy the login password to the clipboard (never shown here)")
        self.copy_pw_btn.clicked.connect(lambda: self.key and self.win.copy_login(self.key, "password"))
        row.addWidget(self.copy_user_btn)
        row.addWidget(self.copy_pw_btn)
        row.addStretch(1)
        v.addLayout(row)
        v.addWidget(self.probe_open)
        v.addStretch(1)
        self.tabs.addTab(_scroll(w), "Overview")

    def _empty(self, text: str) -> QLabel:
        lab = QLabel(text)
        lab.setObjectName("muted")
        lab.setAlignment(Qt.AlignCenter)
        lab.setWordWrap(True)
        lab.setMinimumHeight(64)
        lab.setVisible(False)
        return lab

    def _build_interfaces(self):
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 8, 0, 0)
        self.if_table = _table(["Interface", "Status", "Address", "OSPF", "Description"])
        self.if_empty = self._empty("No interface data yet - the router has not been probed.")
        self.if_age = _muted("")
        find = QPushButton(icons.icon("edit-find"), "Find on canvas")
        find.setToolTip("Select and centre the link of the selected interface")
        find.clicked.connect(self._find_iface_link)
        self.if_table.doubleClicked.connect(lambda *_: self._find_iface_link())
        row = QHBoxLayout()
        row.addWidget(self.if_age, 1)
        row.addWidget(find)
        v.addWidget(self.if_empty)
        v.addWidget(self.if_table, 1)
        v.addLayout(row)
        self.tabs.addTab(w, "Interfaces")

    def _build_routing(self):
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 10, 0, 4)
        v.setSpacing(8)
        self.rt_card = Card("Default route")
        self.rt_body = _rich()
        self.rt_card.v.addWidget(self.rt_body)
        self.cards.append(self.rt_card)
        v.addWidget(self.rt_card)
        row = QHBoxLayout()
        self.rt_btn = QPushButton("Load full routing table")
        self.rt_btn.setToolTip("Runs 'show ip route' on the console (waits for the console to be free)")
        self.rt_btn.clicked.connect(self._load_routes)
        self.rt_status = _muted("")
        row.addWidget(self.rt_btn)
        row.addWidget(self.rt_status, 1)
        v.addLayout(row)
        self.rt_text = QPlainTextEdit()
        self.rt_text.setReadOnly(True)
        self.rt_text.setFont(T.mono(0.9))
        self.rt_text.setPlaceholderText("The full table is fetched on demand.")
        v.addWidget(self.rt_text, 1)
        self.tabs.addTab(w, "Routing")

    def _build_protocols(self):
        w = QWidget()
        self.proto_v = QVBoxLayout(w)
        self.proto_v.setContentsMargins(0, 10, 0, 4)
        self.proto_v.setSpacing(10)
        self.proto_empty = self._empty("No OSPF, BGP, HSRP or VRRP seen on this router yet.")
        self.proto_v.addWidget(self.proto_empty)
        self.ospf_card = Card("OSPF neighbours")
        self.ospf_table = _table(["Neighbour", "State", "Role", "Interface", "Address"])
        self.ospf_card.v.addWidget(self.ospf_table)
        self.bgp_card = Card("BGP peers")
        self.bgp_head = _rich()
        self.bgp_table = _table(["Peer", "AS", "State", "Up for", "Prefixes"])
        self.bgp_card.v.addWidget(self.bgp_head)
        self.bgp_card.v.addWidget(self.bgp_table)
        self.fhrp_card = Card("HSRP / VRRP groups")
        self.fhrp_table = _table(["Proto", "Group", "Role", "Prio", "Virtual IP", "Interface"])
        self.fhrp_card.v.addWidget(self.fhrp_table)
        for c in (self.ospf_card, self.bgp_card, self.fhrp_card):
            self.cards.append(c)
            self.proto_v.addWidget(c)
        self.proto_v.addStretch(1)
        self.tabs.addTab(_scroll(w), "Protocols")

    def _build_switch_tabs(self):
        """Managed switch: VLANs | Ports | STP | MAC (hidden for other kinds)."""
        # VLANs
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 8, 0, 0)
        self.vlan_table = _table(["VLAN", "Name", "Status", "Access ports", "Trunks carrying it"])
        self.vlan_empty = self._empty("No VLAN data yet - the switch has not been probed.")
        self.vlan_age = _muted("")
        self.vlan_table.doubleClicked.connect(lambda *_: self._highlight_selected_vlan())
        row = QHBoxLayout()
        row.addWidget(self.vlan_age, 1)
        hb = QPushButton(icons.icon("view-visible"), "Highlight on canvas")
        hb.setToolTip("Turn on the VLAN colouring layer for the selected VLAN")
        hb.clicked.connect(self._highlight_selected_vlan)
        row.addWidget(hb)
        v.addWidget(self.vlan_empty)
        v.addWidget(self.vlan_table, 1)
        v.addLayout(row)
        self.tab_vlans = w
        self.tabs.addTab(w, "VLANs")
        # Ports
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 8, 0, 0)
        self.port_table = _table(["Port", "Status", "Mode", "VLAN", "STP", "Neighbour"])
        self.port_table.doubleClicked.connect(lambda *_: self._find_port_link())
        self.port_empty = self._empty("No port data yet.")
        self.port_age = _muted("")
        find = QPushButton(icons.icon("edit-find"), "Find on canvas")
        find.clicked.connect(self._find_port_link)
        row = QHBoxLayout()
        row.addWidget(self.port_age, 1)
        row.addWidget(find)
        v.addWidget(self.port_empty)
        v.addWidget(self.port_table, 1)
        v.addLayout(row)
        self.tab_ports = w
        self.tabs.addTab(w, "Ports")
        # Spanning tree
        w = QWidget()
        self.stp_v = QVBoxLayout(w)
        self.stp_v.setContentsMargins(0, 10, 0, 4)
        self.stp_v.setSpacing(10)
        self.stp_head = _rich()
        self.stp_v.addWidget(self.stp_head)
        self.stp_empty = self._empty("No STP data yet.")
        self.stp_v.addWidget(self.stp_empty)
        self.stp_cards: list = []
        self.stp_v.addStretch(1)
        self.tab_stp = _scroll(w)
        self.tabs.addTab(self.tab_stp, "STP")
        self.tabs.setTabToolTip(TAB_STP, "Spanning tree")
        # MAC table
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 10, 0, 4)
        self.mac_head = _rich()
        v.addWidget(self.mac_head)
        self.mac_table = _table(["VLAN", "MAC address", "Type", "Port"])
        v.addWidget(self.mac_table, 1)
        self.mac_age = _muted("")
        v.addWidget(self.mac_age)
        self.tab_mac = w
        self.tabs.addTab(w, "MAC")
        self.tabs.setTabToolTip(TAB_MAC, "MAC address table")
        # EtherChannel: one card per Port-channel of this switch (rebuilt only when something changed)
        w = QWidget()
        self.ec_v = QVBoxLayout(w)
        self.ec_v.setContentsMargins(0, 10, 0, 4)
        self.ec_v.setSpacing(10)
        self.ec_head = _rich()
        self.ec_v.addWidget(self.ec_head)
        self.ec_cards: list = []
        self.ec_v.addStretch(1)
        self.tab_ec = _scroll(w)
        self.tabs.addTab(self.tab_ec, "EtherChannel")
        self.tabs.setTabToolTip(TAB_EC, "EtherChannel (LACP / PAgP / static port-channels)")
        self._switch_tabs = [self.tab_vlans, self.tab_ports, self.tab_stp, self.tab_mac, self.tab_ec]

    def _build_config(self):
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 10, 0, 4)
        row = QHBoxLayout()
        self.cfg_age = _muted("")
        self.cfg_btn = QPushButton(icons.icon("view-refresh"), "Refresh")
        self.cfg_btn.setToolTip("Read 'show running-config' again (waits for the console to be free)")
        self.cfg_btn.clicked.connect(self._refresh_config)
        row.addWidget(self.cfg_age, 1)
        row.addWidget(self.cfg_btn)
        v.addLayout(row)
        self.cfg_text = QPlainTextEdit()
        self.cfg_text.setReadOnly(True)
        self.cfg_text.setFont(T.mono(0.9))
        self.cfg_text.setPlaceholderText("Running configuration not read yet.")
        v.addWidget(self.cfg_text, 1)
        note = _muted("Secrets (enable secret, passwords, keys, communities) are redacted.")
        v.addWidget(note)
        self.tabs.addTab(w, "Config")

    def _build_link_page(self):
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(10)
        self.lk_status = _rich()
        v.addWidget(self.lk_status)
        self.lk_ends = _Ends()
        v.addWidget(self.lk_ends)
        self.lk_sides = Card("Addressing")
        self.lk_sides_body = _rich()
        self.lk_sides.v.addWidget(self.lk_sides_body)
        v.addWidget(self.lk_sides)
        self.lk_proto = Card("On this link")
        self.lk_proto_body = _rich()
        self.lk_proto.v.addWidget(self.lk_proto_body)
        v.addWidget(self.lk_proto)
        self.lk_traffic = Card("Traffic")
        self.spark = Sparkline()
        self.lk_traffic.v.addWidget(self.spark)
        v.addWidget(self.lk_traffic)
        sim = Card("Simulate")
        crow = QHBoxLayout()
        crow.setSpacing(6)
        cl = QLabel("Condition")
        cl.setObjectName("muted")
        self.cond_combo = QComboBox()
        self.cond_combo.setToolTip("Network conditions applied by CML on this link (netem)")
        self.cond_combo.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.cond_combo.setMinimumContentsLength(10)
        self.cond_combo.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.cond_apply = QPushButton("Apply")
        self.cond_apply.clicked.connect(self._apply_condition)
        crow.addWidget(cl)
        crow.addWidget(self.cond_combo, 1)
        crow.addWidget(self.cond_apply)
        sim.v.addLayout(crow)
        self.cond_label = _rich()
        sim.v.addWidget(self.cond_label)
        flow_w = QWidget()
        flow = FlowLayout(flow_w, 0, 6, 6)
        self.pull_btn = QPushButton(icons.icon("network-disconnect", "onaccent"), "Pull cable…")
        self.pull_btn.setProperty("role", "destructive")
        self.pull_btn.setToolTip("Stop this CML link, as if the cable were unplugged")
        self.pull_btn.clicked.connect(lambda: self.link_key and self.win.pull_cable(self.link_key))
        self.restore_btn = QPushButton(icons.icon("network-connect"), "Plug back in")
        self.restore_btn.clicked.connect(lambda: self.link_key and self.win.restore_cable(self.link_key))
        self.cond_clear = QPushButton(icons.icon("edit-clear"), "Clear condition")
        self.cond_clear.clicked.connect(lambda: self.link_key and self.win.clear_condition(self.link_key))
        self.packet_btn = QPushButton(icons.icon("activity"), "Send test packet")
        self.packet_btn.setToolTip("Animate a packet across this link (UI only - nothing is sent)")
        self.packet_btn.clicked.connect(lambda: self.link_key and self.win.animate_link(self.link_key))
        for b in (self.pull_btn, self.restore_btn, self.cond_clear, self.packet_btn):
            b.setMinimumHeight(28)
            flow.addWidget(b)
        sim.v.addWidget(flow_w)
        v.addWidget(sim)
        v.addStretch(1)
        self.cards += [self.lk_sides, self.lk_proto, self.lk_traffic]
        self.stack.addWidget(_scroll(w))

    # ----------------------------------------------------------------- helpers
    def tick(self, now: float | None = None):
        now = now or time.time()
        if not self.isVisible():
            return
        for c in self.cards:
            c.tick(now)
        self._tick_age(now)

    _tick = tick

    def _set_age(self, age_s: float | None):
        self._age_s, self._age_t0 = age_s, time.time()
        self._tick_age(self._age_t0)

    def _tick_age(self, now: float):
        if self._age_s is None:
            self.age.setText("")
            return
        self.age.setText(f"updated {T.fmt_age(self._age_s + (now - self._age_t0))}")

    def showEvent(self, ev):
        super().showEvent(ev)
        if self.ticker is not None:
            self.ticker.start()

    def hideEvent(self, ev):
        super().hideEvent(ev)
        if self.ticker is not None:
            self.ticker.stop()

    def _labels(self) -> dict:
        d = self.win.view_data
        return {n.key: n.label for n in d.nodes} if d else {}

    def copy_as_text(self):
        page = self.stack.currentIndex()
        parts = [self.title.text()]
        if page == 1:
            idx = self.tabs.currentIndex()
            cw = self.tabs.currentWidget()
            if cw is self.tab_vlans:
                parts.append(_table_text(self.vlan_table))
            elif cw is self.tab_ports:
                parts.append(_table_text(self.port_table))
            elif cw is self.tab_stp:
                parts.append(self.stp_head.text())
                for c, t in self.stp_cards:
                    parts += [c.title.text(), _table_text(t)]
            elif cw is self.tab_mac:
                parts += [self.mac_head.text(), _table_text(self.mac_table)]
            elif cw is self.tab_ec:
                parts.append(self.ec_head.text())
                for c, t, body in self.ec_cards:
                    parts += [c.title.text(), body.text(), _table_text(t)]
            elif idx == 0:
                parts += [self.ov_summary.text(), self.ov_body.text()]
            elif idx == 1:
                parts.append(_table_text(self.if_table))
            elif idx == 2:
                parts += [self.rt_body.text(), self.rt_text.toPlainText()]
            elif idx == 3:
                parts += [_table_text(self.ospf_table), self.bgp_head.text(), _table_text(self.bgp_table),
                          _table_text(self.fhrp_table)]
            else:
                parts.append(self.cfg_text.toPlainText())
        elif page == 2:
            parts += [self.state.text(), self.lk_status.text(), self.lk_sides_body.text(), self.lk_proto_body.text()]
        elif page == 3:
            parts += [self.state.text(), self.ec_status.text(), self.ec_mm_body.text(), _table_text(self.ec_sides_t),
                      _table_text(self.ec_members_t)]
        txt = "\n".join(_strip_html(p) for p in parts if p)
        QGuiApplication.clipboard().setText(txt)
        if hasattr(self.win, "notify"):
            self.win.notify("Copied to the clipboard", "info", 2500)

    def _header_mode(self, on: bool, actions: bool = True):
        self.head.setVisible(on)
        self.actions_w.setVisible(on and actions)

    def _tab_changed(self, i: int):
        if self.kind_now and i >= 0 and self.stack.currentIndex() == 1:
            self.settings.setValue(f"inspector/tab_{self.kind_now}", self.tabs.tabText(i))

    def _apply_kind(self, kind: str, changed: bool):
        vis = KIND_TABS[kind]
        for i in range(self.tabs.count()):
            self.tabs.setTabVisible(i, i in vis)
            self.tabs.setTabEnabled(i, True)
        if changed or not self.tabs.isTabVisible(self.tabs.currentIndex()):
            want = str(self.settings.value(f"inspector/tab_{kind}", "Overview"))
            idx = next((i for i in range(self.tabs.count()) if self.tabs.tabText(i) == want and i in vis),
                       TAB_OVERVIEW)
            self.tabs.blockSignals(True)
            self.tabs.setCurrentIndex(idx)
            self.tabs.blockSignals(False)

    # ----------------------------------------------------------------- public
    def show_none(self):
        self.key = self.link_key = None
        self.bundle_key = None
        self.kind_now = None
        self.title.setText("No selection")
        self._header_mode(False)
        self._set_age(None)
        win = self.win
        data = getattr(win, "view_data", None)
        if not self._tips_built and hasattr(win, "a_open") and hasattr(win, "tip_actions"):
            self.build_tips(win.tip_actions())
        try:
            from .summary import summarize
            self.show_summary(summarize(data, getattr(win, "cloud_health", None)), data is not None)
        except Exception:  # noqa: BLE001  (the idle page never breaks the inspector)
            self.show_summary(None, False)
        self.stack.setCurrentIndex(0)

    def show_node(self, nv, data):
        from .graphics import device_pixmap
        changed = nv.key != self.key
        self.key, self.link_key = nv.key, None
        self.bundle_key = None
        self._data_t0 = time.time()
        dtype = nv.dtype
        kind = node_kind(dtype)
        kind_changed = changed or kind != self.kind_now
        self.kind_now = kind
        self._header_mode(True)
        self.glyph.setPixmap(device_pixmap(dtype, 28, self.devicePixelRatioF() or 2.0))
        self.title.setText(html.escape(nv.label))
        backend = "host KVM" if dtype.backend == "kvm" else "CML"
        short = dtype.title.split(" (")[0]
        self.kind.setText(f"<span style='color:{_gray()}'>{html.escape(short)} · {html.escape(nv.label)} · "
                          f"{backend}</span>")
        tone, glyph = NODE_TONE.get(nv.state, ("neutral", "neutral"))
        self.pill.set_state(STATE_TEXT.get(nv.state, nv.state).capitalize(), tone, glyph)
        self.pill.setVisible(True)
        nt = nv.tele
        extra = ""
        if nt is not None:
            cs = ev(nt.collector_state)
            word = {"ok": ("live", T.LINK_UP), "learning": ("learning…", T.NEUTRAL),
                    "paused_console": ("paused: console in use", T.NEUTRAL), "console_busy": ("console busy", T.AMBER),
                    "needs_credentials": ("needs enable secret", T.AMBER), "error": ("probe error", T.DOWN_RED),
                    "unverified": ("collector beta / unverified", "#8B5CF6"), "limited": ("limited", T.AMBER),
                    "no_mgmt": ("no mgmt", T.AMBER), "disabled": ("probing off", T.NEUTRAL)}.get(cs)
            if word:
                extra = status_badge_html(word[0], word[1])
                if nt.probing:
                    extra += f" <span style='color:{_gray()}'>(probing…)</span>"
        self.state.setText(extra)
        self._set_age(nt.last_probe_age_s if nt is not None else None)
        is_cml = nv.key.startswith("cml:")
        no_console = dtype.node_definition in NON_COUNTING_DEFS
        internet = dtype.key == "ext_cloud"
        running = nv.state in ("BOOTED", "STARTED", "QUEUED", "running", "booting")
        self.console_btn.setVisible(not no_console and kind != "cloud")
        self.config_btn.setVisible(is_cml and not no_console and kind != "cloud")
        self.startstop_btn.setVisible(not internet)
        self.startstop_btn.setText("Stop" if running else "Start")
        self.startstop_btn.setIcon(icons.icon("media-playback-stop" if running else "media-playback-start"))
        self.startstop_btn.setProperty("verb", "stop" if running else "start")
        self.health_btn.setVisible(internet)
        self._apply_kind(kind, kind_changed)
        router = nt is not None and ev(nt.role) == "router"
        switch = nt is not None and nt.switch is not None
        has_facts = router and bool(nt.ifaces or nt.default_route is not None or nt.config_text)
        if changed:
            self.rt_text.clear()
            self.rt_status.setText("")
        self._fill_overview(nv, data)
        self._fill_facts(nv, data)
        if kind == "router":
            self.if_empty.setVisible(not has_facts)
            self.if_table.setVisible(has_facts)
            if has_facts:
                self._fill_interfaces(nv)
                self._fill_routing(nv)
                self._fill_protocols(nv)
            else:
                self.rt_body.setText(f"<span style='color:{_gray()}'>Not read yet - the router has not been "
                                     "probed (it must be running, with probing on).</span>")
                for c in (self.ospf_card, self.bgp_card, self.fhrp_card):
                    c.setVisible(False)
                self.proto_empty.setVisible(True)
        if kind == "switch":
            for w, t in ((self.vlan_empty, self.vlan_table), (self.port_empty, self.port_table)):
                w.setVisible(not switch)
                t.setVisible(switch)
            self.stp_empty.setVisible(not switch)
            if switch:
                self._fill_switch(nv, data)
            else:
                self.stp_head.setText("")
                self.mac_head.setText(f"<span style='color:{_gray()}'>No MAC table data yet.</span>")
        if nt is not None and (has_facts or switch):
            self._fill_config(nv)
        elif changed:
            self.cfg_text.clear()
            self.cfg_age.setText("running-config not read yet" + (" (hosts are not probed)" if kind == "host" else ""))
        self.stack.setCurrentIndex(1)

    def _fill_facts(self, nv, data):
        nt = nv.tele
        rows = [("Type", nv.dtype.title, False)]
        if nv.key.startswith("cml:"):
            rows.append(("Node definition", nv.raw.get("node_definition", "") or nv.dtype.node_definition, True))
        m = self.win.mgmt_of(nv.key)
        if m:
            rows.append(("Management", f"{m.get('user', '')}@{m['ip']} (saved in LabDesk)", True))
        item = self.win.scene.nodes.get(nv.key) if hasattr(self.win, "scene") else None
        if item is not None and item.has_card:
            first = self.win.first_login_of(nv.key) if hasattr(self.win, "first_login_of") else None
            rows.append(("Login", login_summary(nv.label, first), False))
        if nt is not None and nt.rid:
            rows.append(("Router ID", nt.rid, True))
        if nt is not None and nt.loopback:
            rows.append(("Loopback", nt.loopback, True))
        links = [lv for lv in data.links if nv.key in (lv.a, lv.b)]
        if links:
            known = [lv for lv in links if lv.status is not None]
            if known:
                up = sum(1 for lv in known if ev(lv.status.kind) == "UP")
                rows.append(("Links", f"{up} of {len(links)} up", False))
            else:
                rows.append(("Links", f"{len(links)} (state not read)", False))
        if nt is not None and nt.last_probe_age_s is not None:
            age = nt.last_probe_age_s
            rows.append(("Live data", (f"stale · last read {age:.0f} s ago" if age > 45 else f"read {age:.0f} s ago"),
                         False))
        elif nt is not None:
            rows.append(("Live data", "not read" + (f" ({nt.collector_detail})" if nt.collector_detail else " yet"),
                         False))
        self.kv.set_rows(rows)

    def show_link(self, lv, data):
        changed = lv.key != self.link_key
        self.key, self.link_key = None, lv.key
        self.bundle_key = None
        self.kind_now = None
        self._data_t0 = time.time()
        labels = {n.key: n.label for n in data.nodes}
        self._header_mode(True, actions=False)
        self.glyph.setPixmap(icons.pixmap("network-wired", 28, None, self.devicePixelRatioF() or 2.0, role="muted"))
        self.title.setText(html.escape(link_header(lv, labels)))
        st = lv.status
        tele = lv.tele
        sub = "CML link" if lv.key.startswith("cml:") else "Host-KVM link"
        if tele is not None and tele.subnet_hint:
            sub += f" · {tele.subnet_hint}"
        self.kind.setText(f"<span style='color:{_gray()}'>{html.escape(sub)}</span>")
        self.pill.setVisible(False)
        if st is not None:
            word, col = KIND_WORDS.get(ev(st.kind), (ev(st.kind), T.NEUTRAL))
            text = status_badge_html(word, col)
            if st.text and ev(st.kind) != "UP":
                text += f" &nbsp;<b>{html.escape(st.text)}</b>"
            self.state.setText(text)
            self.lk_status.setText(html.escape(st.plain_text or ""))
        else:
            up = lv.state in ("STARTED", "up")
            self.state.setText(status_badge_html("UP (CML)" if up else lv.state.lower() or "unknown",
                                                 T.LINK_UP if up else T.NEUTRAL))
            self.lk_status.setText("")
        self.lk_status.setVisible(bool(self.lk_status.text()))
        tr = lv.traffic
        self._set_age(time.time() - tr.ts if tr is not None else None)
        # both ends
        for side, node, iface_label, iff, card in (
                ("a", lv.a, lv.label_a, tele.iface_a if tele else None, self.lk_ends.a),
                ("b", lv.b, lv.label_b, tele.iface_b if tele else None, self.lk_ends.b)):
            sv = lv.side_a if side == "a" else lv.side_b
            s = ev(sv) if sv is not None else None
            ip = (iff.ip + (f"/{iff.prefixlen}" if iff.prefixlen is not None else "")
                  if iff is not None and iff.ip else "")
            end = getattr(getattr(lv, "addressing", None), side, None)
            if end is not None and (end.l3 or end.addrs):      # live addressing (CLI + console tap)
                own = [f"{a_ip}/{pl}" if pl is not None else a_ip for ifn, a_ip, pl, _sec in end.addrs
                       if ifn == end.iface]
                ip = (own[0] + (f" +{len(own) - 1}" if len(own) > 1 else "")) if own else \
                    ("no IP" if not end.addrs and end.method == "none" and end.l3 else "")
            card.set_end(labels.get(node, "?"), iface_label or "", ip, s,
                         iff.desc if iff is not None and iff.desc else "")
        rows = []
        ad = getattr(lv, "addressing", None)
        if ad is not None:
            rows.append(addressing_html(ad))
        elif tele is not None and tele.subnet_hint:
            rows.append(f"<span style='color:{_gray()}'>Subnet {html.escape(tele.subnet_hint)}"
                        + (f" · <b style='color:{T.text_hex(T.AMBER)}'>the two ends are not in the same subnet</b>"
                           if lv.addressing_mismatch else "") + "</span>")
        self.lk_sides_body.setText("".join(rows))
        self.lk_sides.setVisible(bool(rows))
        self.lk_sides.set_age(None, time.time())
        # protocols on this link
        lines = []
        if lv.p2p:
            lines.append("<b>Point-to-point</b>: OSPF runs this link as network type point-to-point "
                         "(no DR/BDR election).")
        elif lv.p2p_conflict:
            lines.append(f"<b>P2P?</b> {html.escape(lv.p2p_conflict)}")
        elif tele is not None and tele.bcast:
            lines.append("<b>Broadcast</b>: OSPF elects a DR/BDR even though only two routers share it"
                         + (f" ({html.escape(tele.bcast_detail)})." if tele.bcast_detail else "."))
        if tele is not None:
            for a, b, proto, state in tele.adjacencies:
                lines.append(f"<span style='color:{T.text_hex(T.PROTO_HUES.get(ev(proto), T.NEUTRAL))}'><b>{ev(proto)}"
                             f"</b></span> {html.escape(labels.get(a, '?'))} ↔ {html.escape(labels.get(b, '?'))}: "
                             f"{html.escape(str(state))}")
            seg = (data.telemetry.segments.get(tele.segment_id) if data.telemetry and tele.segment_id else None)
            if seg is not None and ev(seg.kind) == "multi":
                for proto, detail in seg.detail.items():
                    where = "across this switch" if ev(proto) == "BGP" else "on this switch segment"
                    lines.append(f"<span style='color:{T.text_hex(T.PROTO_HUES.get(ev(proto), T.NEUTRAL))}'>"
                                 f"<b>{ev(proto)}</b></span> {where}: {html.escape(detail)}")
        if lv.default_hop is not None:
            d = getattr(tele, "default_direction", None)
            frm, to = (lv.a, lv.b) if d != "b2a" else (lv.b, lv.a)
            lines.append(f"<span style='color:{T.text_hex(T.ROUTE_BLUE)}'><b>Default route</b></span>: traffic for the "
                         f"internet flows {html.escape(labels.get(frm, '?'))} → {html.escape(labels.get(to, '?'))} "
                         f"here ({html.escape(lv.default_hop.code or '')}).")
        self.lk_proto_body.setText("<br>".join(lines) or f"<span style='color:{_gray()}'>No routing protocol "
                                                         "seen on this link.</span>")
        # traffic
        self.spark.set_values(tr.history if tr is not None else [])
        self.lk_traffic.set_age(time.time() - tr.ts if tr is not None else None, time.time())
        # simulate
        if changed or self.cond_combo.count() == 0:
            self.cond_combo.clear()
            for name in self.win.condition_presets():
                self.cond_combo.addItem(name)
        cml = lv.key.startswith("cml:")
        cond = lv.condition
        if cond is not None and cond.enabled:
            self.cond_label.setText(f"<span style='color:{T.text_hex(T.EVENT_HUES['sim'])}'>Active: "
                                    f"{html.escape(cond.label or 'conditioned')}</span>")
        else:
            self.cond_label.setText(f"<span style='color:{_gray()}'>No delay / loss applied.</span>")
        pulled = st is not None and ev(st.kind) == "CABLE_PULLED"
        for w in (self.cond_combo, self.cond_apply):
            w.setEnabled(cml)
        self.cond_clear.setEnabled(cml and cond is not None and cond.enabled)
        self.pull_btn.setEnabled(cml and not pulled and lv.state == "STARTED")
        self.restore_btn.setEnabled(cml and (pulled or lv.state == "STOPPED"))
        self.stack.setCurrentIndex(2)

    # ----------------------------------------------------------------- node tabs
    def _fill_overview(self, nv, data):
        nt = nv.tele
        labels = {n.key: n.label for n in data.nodes}
        if nt is None:
            self.ov_summary.setText("")
        else:
            self.ov_summary.setText(html.escape(nt.summary or ""))
        self.ov_summary.setVisible(bool(self.ov_summary.text()))
        body = []
        router = nt is not None and ev(nt.role) == "router"
        if nt is not None and nt.collector_detail:
            body.append(f"<span style='color:{_gray()}'>{html.escape(nt.collector_detail)}</span>")
        if nt is not None and nt.switch is not None:
            body += self._switch_overview(nt, labels)
        if nv.dtype.key == "ext_cloud":
            body += self._cloud_overview(nv, data, labels)
        if router:
            if nt.default_path is not None:
                st = ev(nt.default_path.state)
                word, col = PATH_WORDS.get(st, (st, T.NEUTRAL))
                body.append(f"<b>Default route</b> {status_badge_html(word, col)}<br>"
                            f"{html.escape(nt.default_path.detail or '')}")
            if nv.fhrp:
                body.append("<b>Gateway redundancy</b><br>" + "<br>".join(
                    f"<span style='color:{T.PROTO_HUES.get(ev(b.proto))}'><b>{ev(b.proto)}</b></span> "
                    f"group {b.group}: <b>{html.escape(b.role)}</b>"
                    + (f" for {html.escape(b.vip)}" if b.vip else "") + f" on {html.escape(b.iface)}"
                    for b in nv.fhrp))
        # links in words
        rows = []
        for lv in data.links:
            if nv.key not in (lv.a, lv.b):
                continue
            mine, other, oif = (lv.label_a, lv.b, lv.label_b) if lv.a == nv.key else (lv.label_b, lv.a, lv.label_a)
            st = lv.status
            if st is not None:
                word, col = KIND_WORDS.get(ev(st.kind), (ev(st.kind), T.NEUTRAL))
            else:
                word, col = ("UP", T.LINK_UP) if lv.state in ("STARTED", "up") else (lv.state, T.NEUTRAL)
            blue = (f" <span style='color:{T.ROUTE_BLUE}'>● default path</span>"
                    if lv.default_hop is not None else "")
            p2p = " <b>P2P</b>" if lv.p2p else ""
            tele = lv.tele
            if tele is not None and tele.trunk:
                p2p += f" <span style='color:{T.text_hex("#1489C0")}'>trunk {html.escape(tele.trunk_text or '')}</span>"
            elif tele is not None and tele.access_vlan is not None:
                p2p += f" <span style='color:{T.PROTO_HUES['VLAN']}'>VLAN {tele.access_vlan}</span>"
            if tele is not None and tele.stp_block is not None:
                b = tele.stp_block
                p2p += (f" <b style='color:{T.STP_BLOCK}'>STP: {html.escape(labels.get(b.node, '?'))} {b.port} "
                        f"{html.escape(b.role_word)} BLK</b>")
            rows.append(f"{html.escape(mine or '·')} → {html.escape(labels.get(other, '?'))} "
                        f"{html.escape(oif or '')} &nbsp;{status_badge_html(word, col)}{p2p}{blue}")
        body.append("<b>Links</b><br>" + ("<br>".join(rows) if rows else "<i>none</i>"))
        if nv.dtype.key == "fortios":
            body.append(f"<span style='color:{T.status_text("warn").name()}'>Unlicensed FortiOS: routing/OSPF work, "
                        "but it does not forward traffic.</span>")
        if not router and nt is not None and nt.switch is None and ev(nt.role) == "switch" and \
                nv.dtype.node_definition == "unmanaged_switch":
            body.append(f"<span style='color:{_gray()}'>Unmanaged switch: no console, no VLANs, no spanning tree - "
                        "every port is in one broadcast domain.</span>")
        self.ov_body.setText("<br><br>".join(body))
        self.ov_card.setVisible(bool(body))
        probed = (router or (nt is not None and nt.switch is not None)) and nv.key.startswith("cml:")
        self.probe_btn.setVisible(probed)
        self.probe_open.setVisible(probed)
        item = self.win.scene.nodes.get(nv.key) if hasattr(self.win, "scene") else None
        self.creds_btn.setVisible(bool(item is not None and item.has_card))
        fl = None
        if item is not None and item.has_card and hasattr(self.win, "login_of"):
            try:
                fl, _ = self.win.login_of(nv.key)
            except Exception:  # noqa: BLE001
                fl = None
        self.copy_user_btn.setVisible(bool(fl is not None and fl.username))
        self.copy_pw_btn.setVisible(bool(fl is not None and fl.password))
        if nt is not None:
            self.probe_open.setChecked(bool(nt.probe_while_open))
            self.probe_btn.setText("Probe now (output appears in your console)" if nt.console_open else "Probe now")

    # ----------------------------------------------------------------- switch tabs
    def _cloud_overview(self, nv, data, labels) -> list[str]:
        """Internet cloud: host-service health, what answers, who uses it, and the fix when it is down."""
        from .. import cloud as _cloud
        h = getattr(self.win, "cloud_health", None)
        conf = _cloud.load_conf()
        col = {"up": T.LINK_UP, "degraded": T.AMBER}.get(h.state if h else "", T.DOWN_RED if h else T.NEUTRAL)
        out = [f"<b>Internet cloud</b> {status_badge_html(h.headline if h else 'checking…', col)}"]
        two = ", ".join(conf.headline_answers[:2])
        rest = ", ".join(conf.headline_answers[2:])
        out.append(f"Always answers ping to <b>{two}</b>" + (f" (also {rest})" if rest else "")
                   + f" - even without real Internet. Routers use <b>{conf.cloud_ip}</b> as their default gateway "
                   f"on <b>{conf.net_text}</b> (.2, .3, … for the routers).")
        if h is not None:
            det = []
            if h.host_ping is not None:
                det.append(f"host → {h.cloud_ip}: {'ok' if h.host_ping else '<b>no reply</b>'}")
            if h.selfping:
                bad = [ip for ip, ok in h.selfping.items() if not ok]
                det.append("namespace self-test: " + ("all answer" if not bad else "no answer from " + ", ".join(bad)))
            if h.edge_gw:
                det.append(f"return path for lab LANs via <b>{html.escape(h.edge_gw)}</b>")
            per = [f"{p} → {g}" for p, g in h.routes if p not in conf.return_prefixes]
            if per:
                det.append("per-prefix: " + html.escape(", ".join(per)))
            if h.status_age_s is not None:
                det.append(f"checked {int(h.status_age_s)} s ago by labdesk-cloud-check.timer")
            out.append(f"<span style='color:{_gray()}'>" + "<br>".join(det) + "</span>")
            if h.hint:
                out.append(f"<span style='color:{T.DOWN_RED}'><b>Fix:</b></span> <code>{html.escape(h.hint)}</code>"
                           + (f"<br><span style='color:{_gray()}'>{html.escape(h.problem)}</span>" if h.problem else ""))
        users = [labels.get(r, r) for r in self.win.cloud_routers(nv.key)]
        out.append("<b>Routers on the cloud</b>: " + (html.escape(", ".join(users)) if users else
                                                       "<i>none yet - Shift-drag a link from a router</i>")
                   + f"<br><span style='color:{_gray()}'>Right-click a router → Auto-configure for Internet cloud</span>")
        return out

    def _switch_overview(self, nt, labels) -> list[str]:
        from ..parsers.ios_l2 import vlan_text
        sw = nt.switch
        out = []
        kind = "Layer-3 switch" if sw.l3 else "Layer-2 switch"
        svis = ", ".join(f"{html.escape(f.short)} {html.escape(f.cidr or f.ip or '')}" for f in sw.svis if f.ip)
        out.append(f"<b>{kind}</b>" + (f" · management {svis}" if svis else "")
                   + (f" · spanning tree <b>{html.escape(sw.stp_label)}</b>" if sw.stp_label else ""))
        if sw.vlans:
            out.append("<b>VLANs</b><br>" + "<br>".join(
                f"<span style='color:{self.win.scene.ctx.vlan_hues.get(v.id, T.NEUTRAL)}'>■</span> "
                f"VLAN {v.id} <b>{html.escape(v.name)}</b>"
                + (f" · access {html.escape(', '.join(v.ports))}" if v.ports else "") for v in sw.vlans))
        if sw.stp:
            rows = []
            for x in sw.stp:
                root = sw.root_of.get(x.vlan) or "?"
                me = x.is_root
                rows.append(f"VLAN {x.vlan}: root bridge <b>{html.escape(root)}</b>"
                            + (f" <span style='color:{T.text_hex(T.ROOT_GOLD)}'>♛ this switch</span>" if me else
                               f" via {html.escape(x.root_port or '?')} (cost {x.root_cost})"))
            out.append("<b>Spanning tree</b><br>" + "<br>".join(rows))
        for port, role, vids in sw.blocked:
            out.append(f"<div style='border-left:3px solid {T.STP_BLOCK}; padding-left:4px'><b>{html.escape(port)}"
                       f"</b> is <b>{html.escape(role)}</b> and <b>blocking</b> for VLAN {vlan_text(vids)}: the "
                       "redundant cable stays up but carries no traffic, so frames cannot loop between the "
                       "switches. If the forwarding path fails, it takes over within seconds (RSTP).</div>")
        if sw.err_disabled:
            out.append(f"<b style='color:{T.DOWN_RED}'>err-disabled</b>: {html.escape(', '.join(sw.err_disabled))} "
                       "(fix the cause, then shutdown / no shutdown)")
        if sw.mac_total is not None:
            out.append(f"<span style='color:{_gray()}'>{sw.mac_total} MAC address"
                       f"{'es' if sw.mac_total != 1 else ''} learned</span>")
        return out

    def _fill_switch(self, nv, data):
        from ..parsers.ios_l2 import vlan_text
        sw = nv.tele.switch
        labels = {n.key: n.label for n in data.nodes}
        hues = self.win.scene.ctx.vlan_hues
        # VLANs
        trunk_ports = {}
        for pr in sw.ports:
            if pr["mode"] == "trunk":
                trunk_ports[pr["port"]] = pr["vlan_text"]
        t = self.vlan_table
        t.begin(self.key)
        for v in sw.vlans:
            r = t.rowCount()
            t.insertRow(r)
            carrying = []
            for x in sw.stp:
                if x.vlan == v.id:
                    carrying = [pt.port for pt in x.ports.values() if pt.port in trunk_ports]
            vid = QTableWidgetItem(_bar_icon(hues.get(v.id, T.NEUTRAL)), str(v.id))
            vid.setData(Qt.UserRole, v.id)
            for c, it in enumerate((vid, QTableWidgetItem(v.name), QTableWidgetItem(v.status),
                                    QTableWidgetItem(", ".join(v.ports) or "—"),
                                    QTableWidgetItem(", ".join(carrying) or "—"))):
                t.setItem(r, c, it)
        t.commit()
        self.vlan_age.setText(f"show vlan brief · {T.fmt_age(sw.ages.get('vlans'))}")
        # Ports
        t = self.port_table
        t.begin(self.key)
        for pr in sw.ports:
            r = t.rowCount()
            t.insertRow(r)
            st = pr["status"]
            col = (T.DOWN_RED if pr["err_disabled"] else T.LINK_UP if st == "connected" else T.NEUTRAL)
            name = QTableWidgetItem(_bar_icon(T.STP_BLOCK if pr["blocked"] else
                                              (T.DOWN_RED if pr["err_disabled"] else "#00000000")), pr["port"])
            name.setData(Qt.UserRole, pr["port"])
            name.setToolTip(pr.get("desc") or "")
            stat = QTableWidgetItem(st)
            stat.setForeground(QColor(col))
            stp = QTableWidgetItem(pr["stp_text"] or "—")
            if pr["blocked"]:
                stp.setForeground(QColor(T.STP_BLOCK))
                fb = stp.font()
                fb.setBold(True)
                stp.setFont(fb)
                stp.setToolTip("spanning tree blocks this port for those VLANs (a backup path, not a fault)")
            for c, it in enumerate((name, stat, QTableWidgetItem(pr["mode"]), QTableWidgetItem(pr["vlan_text"]),
                                    stp, QTableWidgetItem(pr.get("neighbor") or (pr.get("desc") or "")))):
                t.setItem(r, c, it)
        t.commit()
        self.port_age.setText(f"show interfaces status / trunk / spanning-tree · {T.fmt_age(sw.ages.get('ports'))}")
        # Spanning tree: one card per VLAN (rebuilt only when something changed: no flicker / scroll jumps)
        sig = (nv.key, tuple((x.vlan, x.root_id, x.bridge_id, x.root_port, x.root_cost,
                              tuple((pt.port, ev(pt.role), ev(pt.state), pt.cost) for pt in x.ports.values()),
                              (sw.tc.get(x.vlan).changes if sw.tc.get(x.vlan) else None)) for x in sw.stp),
               tuple(sw.root_of.items()))
        self._fill_ec_tab(nv, sw, labels)
        if sig == getattr(self, "_stp_sig", None):
            for c, _ in self.stp_cards:
                c.set_age(sw.ages.get("stp"), time.time())
            self._fill_mac(sw)
            return
        self._stp_sig = sig
        for c, _ in self.stp_cards:
            self.stp_v.removeWidget(c)
            c.deleteLater()
            if c in self.cards:
                self.cards.remove(c)
        self.stp_cards = []
        mode = f"Mode <b>{html.escape(sw.stp_label or sw.stp_mode or '?')}</b>"
        root_for = f" · root bridge for VLAN <b>{vlan_text(sw.root_for)}</b>" if sw.root_for else " · root for none"
        self.stp_head.setText(mode + root_for)
        for x in sw.stp:
            tc = sw.tc.get(x.vlan)
            root = sw.root_of.get(x.vlan) or x.root_address
            card = Card(f"VLAN {x.vlan}" + (f" {sw_name}" if (sw_name := next((v.name for v in sw.vlans
                                                                                 if v.id == x.vlan), "")) else ""))
            info = _rich(f"Root ID <b>{html.escape(x.root_id)}</b> ({html.escape(root or '?')})<br>"
                         f"This bridge <b>{html.escape(x.bridge_id)}</b>"
                         + (f" — <b style='color:{T.text_hex(T.ROOT_GOLD)}'>♛ is the root</b>" if x.is_root else
                            f"<br>Root port <b>{html.escape(x.root_port or '?')}</b>, root path cost {x.root_cost}")
                         + (f"<br><span style='color:{_gray()}'>{tc.changes} topology change"
                            f"{'s' if tc.changes != 1 else ''}"
                            + (f", last {T.fmt_age(tc.last_ago_s)}" if tc.last_ago_s is not None else "")
                            + (f" from {html.escape(tc.from_iface)}" if tc.from_iface else "") + "</span>"
                            if tc else ""))
            card.v.addWidget(info)
            tb = _table(["Port", "Role", "State", "Cost", "Type"])
            for pt in sorted(x.ports.values(), key=lambda q: q.nbr):
                r = tb.rowCount()
                tb.insertRow(r)
                role = QTableWidgetItem(ev(pt.role))
                state = QTableWidgetItem(ev(pt.state))
                if pt.blocked:
                    for it in (role, state):
                        it.setForeground(QColor(T.STP_BLOCK))
                        f = it.font()
                        f.setBold(True)
                        it.setFont(f)
                elif ev(pt.state) == "FWD":
                    state.setForeground(QColor(T.LINK_UP))
                typ = ("P2p" if pt.p2p else "Shr") + (" Edge" if pt.edge else "")
                for c, it in enumerate((QTableWidgetItem(pt.port), role, state, QTableWidgetItem(str(pt.cost)),
                                        QTableWidgetItem(typ))):
                    tb.setItem(r, c, it)
            tb.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
            tb.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
            tb.setFixedHeight(tb.horizontalHeader().sizeHint().height() + 4 + 24 * max(1, tb.rowCount()))
            card.v.addWidget(tb)
            card.set_age(sw.ages.get("stp"), time.time())
            self.stp_v.insertWidget(self.stp_v.count() - 1, card)
            self.cards.append(card)
            self.stp_cards.append((card, tb))
        self._fill_mac(sw)
        _ = labels

    # ----------------------------------------------------------------- EtherChannel (switch tab + bundle page)
    def _fill_ec_tab(self, nv, sw, labels: dict):
        tv = getattr(self.win.view_data, "telemetry", None)
        bundles = tv.bundles if tv is not None else {}
        sides = list(sw.ec_sides)
        sig = (nv.key, tuple((x.po, x.flags, ev(x.proto), x.stp_text, x.trunk_text, x.load_balance,
                              tuple((m.port, m.flags, m.mode, m.partner_node, m.partner_port) for m in x.members),
                              getattr(bundles.get(sw.ec_bundles.get(x.po)), "mismatch_short", None)) for x in sides))
        if sig == getattr(self, "_ec_sig", None):
            return
        self._ec_sig = sig
        for c, _t, _b in self.ec_cards:
            self.ec_v.removeWidget(c)
            c.deleteLater()
        self.ec_cards = []
        if not sides:
            self.ec_head.setText(f"<span style='color:{_gray()}'>No EtherChannel on this switch "
                                 "(<code>show etherchannel summary</code> lists no channel-group).<br>"
                                 "Select two or more parallel cables to another switch (Ctrl-click) and right-click → "
                                 "<b>Bundle into EtherChannel…</b></span>")
            return
        lb = next((x.load_balance for x in sides if x.load_balance), None)
        sysid = next((f"{x.sys_priority}, {x.sys_mac}" for x in sides if x.sys_mac), None)
        self.ec_head.setText(f"<b>{len(sides)}</b> Port-channel{'s' if len(sides) != 1 else ''}"
                             + (f" · load-balance <b>{html.escape(lb)}</b>" if lb else "")
                             + (f" · LACP system <span style='font-family:monospace'>{html.escape(sysid)}</span>"
                                if sysid else ""))
        from ..etherchannel import MODE_WORDS
        for x in sides:
            bd = bundles.get(sw.ec_bundles.get(x.po))
            peer = None
            if bd is not None:
                peer = labels.get(bd.b if bd.a == nv.key else bd.a)
            state = ev(bd.state) if bd is not None else "unknown"
            word, col = EC_STATE_WORDS.get(state, (state, T.NEUTRAL))
            card = Card(f"{x.po} · {_proto_word(x)}" + (f" → {html.escape(peer)}" if peer else ""))
            body = _rich(status_badge_html(word, col)
                         + (f" &nbsp;<b>{bd.bundled}/{bd.total}</b> bundled" if bd is not None else "")
                         + f"<br>Port-channel {html.escape(x.po)}: {html.escape(x.flag_words)}"
                         + (f"<br>mode {html.escape(' / '.join(MODE_WORDS.get(m, m) for m in x.modes))}"
                            if x.modes else "")
                         + (f"<br>STP on {html.escape(x.po)}: <b"
                            + (f" style='color:{T.status_text('warn').name()}'" if _stp_short(x).endswith("BLK") else "")
                            + f">{html.escape(x.stp_text)}</b>" if x.stp_text else "")
                         + (f"<br>trunk VLAN {html.escape(x.trunk_text)}" + (f", native {x.native}" if x.native else "")
                            if x.trunk_text else "")
                         + (f"<br><b style='color:{T.status_text('warn').name()}'>⚠ {html.escape(bd.mismatch_short)}"
                            f"</b>" if bd is not None and bd.mismatch_short else ""))
            card.v.addWidget(body)
            t = _table(["Port", "State", "Mode", "Partner"])
            t.begin(("ec", nv.key, x.po))
            for m in x.members:
                r = t.rowCount()
                t.insertRow(r)
                st = QTableWidgetItem(m.words if m.bundled else f"{_EC_SHORT.get(m.words, m.words)} ({m.flags})")
                st.setForeground(QColor(T.status_text("up" if m.bundled else ("down" if "D" in m.flags else "warn"))))
                partner = (f"{labels.get(m.partner_node, '?')} {m.partner_port or '?'}" if m.partner_node else
                           (f"{labels.get(m.peer_node, '?')} {m.peer_port} (cable)" if m.peer_node else "—"))
                tip = _member_tip(m, labels)
                for c, it in enumerate((QTableWidgetItem(m.port), st, QTableWidgetItem(m.mode or "—"),
                                        QTableWidgetItem(partner))):
                    it.setToolTip(tip)
                    t.setItem(r, c, it)
            t.commit()
            t.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
            t.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
            t.setFixedHeight(t.horizontalHeader().sizeHint().height() + 4 + 24 * max(1, t.rowCount()))
            card.v.addWidget(t)
            if bd is not None:
                btn = QPushButton(icons.icon("edit-find"), "Show on canvas")
                btn.clicked.connect(lambda _=False, k=bd.key: self.win.select_bundle(k))
                row = QHBoxLayout()
                row.addStretch(1)
                row.addWidget(btn)
                card.v.addLayout(row)
            card.set_age(x.age_s, time.time())
            self.ec_v.insertWidget(self.ec_v.count() - 1, card)
            self.ec_cards.append((card, t, body))

    def _build_bundle_page(self):
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(10)
        self.ec_status = _rich()
        v.addWidget(self.ec_status)
        self.ec_mm = Card("Why it does not bundle")
        self.ec_mm_body = _rich()
        self.ec_mm.v.addWidget(self.ec_mm_body)
        v.addWidget(self.ec_mm)
        self.ec_sides = Card("Both sides")
        self.ec_sides_t = _table(["", "A", "B"])
        self.ec_sides_t.setSelectionMode(QAbstractItemView.NoSelection)
        self.ec_sides.v.addWidget(self.ec_sides_t)
        v.addWidget(self.ec_sides)
        self.ec_members = Card("Members")
        self.ec_members_t = _table(["Cable", "State", "Partner (protocol)"])
        self.ec_members.v.addWidget(self.ec_members_t)
        v.addWidget(self.ec_members)
        flow_w = QWidget()
        flow = FlowLayout(flow_w, 0, 6, 6)
        self.ec_sel_btn = QPushButton(icons.icon("edit-select-all"), "Select member cables")
        self.ec_sel_btn.clicked.connect(lambda: self.bundle_key and self.win.select_bundle_members(self.bundle_key))
        self.ec_unbundle_btn = QPushButton(icons.icon("edit-delete", "danger"), "Unbundle…")
        self.ec_unbundle_btn.setToolTip("Remove the channel-group from the members and delete the Port-channel on "
                                        "both switches")
        self.ec_unbundle_btn.clicked.connect(lambda: self.bundle_key and self.win.unbundle(self.bundle_key))
        for b in (self.ec_sel_btn, self.ec_unbundle_btn):
            b.setMinimumHeight(28)
            flow.addWidget(b)
        v.addWidget(flow_w)
        v.addStretch(1)
        self.cards += [self.ec_mm, self.ec_sides, self.ec_members]
        self.stack.addWidget(_scroll(w))

    def show_bundle(self, bd, data):
        """The EtherChannel view (bundle selected on the canvas)."""
        from ..etherchannel import MODE_WORDS
        self.key, self.link_key, self.bundle_key = None, None, bd.key
        self.kind_now = None
        self._data_t0 = time.time()
        labels = {n.key: n.label for n in data.nodes}
        self._header_mode(True, actions=False)
        self.glyph.setPixmap(icons.pixmap("network-wired", 28, None, self.devicePixelRatioF() or 2.0, role="accent"))
        la, lb = labels.get(bd.a, "?"), labels.get(bd.b, "?")
        self.title.setText(html.escape(f"{bd.po_text} · {bd.protocol} · {la} ⟷ {lb}"))
        self.kind.setText(f"<span style='color:{_gray()}'>EtherChannel · {bd.total} member cable"
                          f"{'s' if bd.total != 1 else ''}</span>")
        self.pill.setVisible(False)
        state = ev(bd.state)
        word, col = EC_STATE_WORDS.get(state, (state, T.NEUTRAL))
        self.state.setText(status_badge_html(word, col) + f" &nbsp;<b>{bd.bundled}/{bd.total}</b> bundled")
        age = max((s.age_s or 0) for s in (bd.side_a, bd.side_b) if s is not None) if bd.side_a else None
        self._set_age(age)
        if bd.mismatch_short:
            self.ec_status.setText(f"<b>{html.escape(bd.po_text)} does not bundle</b>: "
                                   f"{html.escape(bd.mismatch_short)}. The member cables carry nothing as a bundle.")
        else:
            self.ec_status.setText(html.escape(bd.explain or ""))
        mm = list(bd.mismatch) + ([] if bd.mismatch else list(bd.problems))
        self.ec_mm.title.setText("<b>Why it does not bundle</b>" if bd.mismatch else "<b>Members not bundled</b>")
        self.ec_mm_body.setText("<br>".join(f"<span style='color:{T.status_text('warn').name()}'>⚠</span> "
                                            f"{html.escape(x)}" for x in mm))
        self.ec_mm.setVisible(bool(mm))
        # both sides
        t = self.ec_sides_t
        t.setHorizontalHeaderLabels(["", la.upper(), lb.upper()])
        sa, sb = bd.side_a, bd.side_b

        def val(side, fn):
            if side is None:
                return "no channel-group"
            try:
                return fn(side) or "—"
            except Exception:  # noqa: BLE001
                return "—"
        rows = [("Port-channel", lambda s_: f"{s_.po} ({s_.flags}) {'up' if s_.up else 'down'}",
                 lambda s_: f"{s_.po}: {s_.flag_words}"),
                ("Protocol", lambda s_: _proto_word(s_), None),
                ("Mode", lambda s_: " / ".join(s_.modes), lambda s_: " / ".join(MODE_WORDS.get(m, m) for m in s_.modes)),
                ("LACP system", lambda s_: f"{s_.sys_priority}, {s_.sys_mac}" if s_.sys_mac else "", None),
                ("Load-balance", lambda s_: s_.load_balance, None),
                ("STP", lambda s_: _stp_short(s_), lambda s_: f"{s_.po}: {s_.stp_text}"),
                ("Trunk VLANs", lambda s_: (s_.trunk_text or "") + (f" (native {s_.native})" if s_.native else ""),
                 None)]
        t.begin(("ecs", bd.key))
        for name, fn, tipfn in rows:
            r = t.rowCount()
            t.insertRow(r)
            k = QTableWidgetItem(name)
            k.setForeground(QColor(_gray()))
            items = [k, QTableWidgetItem(val(sa, fn)), QTableWidgetItem(val(sb, fn))]
            if tipfn is not None:
                for it, side in zip(items[1:], (sa, sb)):
                    it.setToolTip(val(side, tipfn))
            if name == "STP":
                for it, side in zip(items[1:], (sa, sb)):
                    if side is not None and any(ev(st_) == "BLK" for _, st_ in side.stp.values()):
                        it.setForeground(QColor(T.STP_BLOCK))
                        f = it.font()
                        f.setBold(True)
                        it.setFont(f)
            for c, it in enumerate(items):
                t.setItem(r, c, it)
        t.commit()
        t.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        t.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        t.setFixedHeight(t.horizontalHeader().sizeHint().height() + 4 + 24 * t.rowCount())
        # members (one row per cable)
        t = self.ec_members_t
        t.begin(("ecm", bd.key))
        for lk in bd.link_keys:
            ma = next((m for m in (sa.members if sa else []) if m.link_key == lk), None)
            mb = next((m for m in (sb.members if sb else []) if m.link_key == lk), None)
            r = t.rowCount()
            t.insertRow(r)
            cable = f"{ma.port if ma else '?'} ↔ {mb.port if mb else (ma.peer_port if ma else '?')}"
            ok = all(m.bundled for m in (ma, mb) if m is not None)
            words = "bundled" if ok else " ↔ ".join(f"{_EC_SHORT.get(m.words, m.words)} ({m.flags})"
                                                    for m in (ma, mb) if m is not None)
            st = QTableWidgetItem(words)
            st.setForeground(QColor(T.status_text("up" if ok else "warn")))
            pm = next((m for m in (ma, mb) if m is not None and (m.partner_node or m.partner_dev)), None)
            pinfo = _partner_text(pm, labels) if pm is not None else ""
            tip = "<br><br>".join(_member_tip(m, labels) for m in (ma, mb) if m is not None)
            for c, it in enumerate((QTableWidgetItem(cable), st, QTableWidgetItem(pinfo or "no partner"))):
                it.setToolTip(tip)
                t.setItem(r, c, it)
        t.commit()
        t.setFixedHeight(t.horizontalHeader().sizeHint().height() + 4 + 24 * max(1, t.rowCount()) + 14)
        for c in (self.ec_mm, self.ec_sides, self.ec_members):
            c.set_age(age, time.time())
        self.ec_unbundle_btn.setEnabled(any(s_ is not None and s_.group is not None for s_ in (sa, sb)))
        self.stack.setCurrentIndex(3)

    def _fill_mac(self, sw):
        counts = " · ".join(f"VLAN {v}: {n}" for v, n in sorted(sw.mac_counts.items()))
        self.mac_head.setText(f"<b>{sw.mac_total if sw.mac_total is not None else '?'}</b> MAC addresses"
                              + (f" &nbsp;<span style='color:{_gray()}'>({counts})</span>" if counts else ""))
        t = self.mac_table
        t.begin(self.key)
        for e in sw.mac_table:
            r = t.rowCount()
            t.insertRow(r)
            for c, val in enumerate((e.get("vlan"), e.get("mac"), e.get("type"), e.get("port"))):
                t.setItem(r, c, QTableWidgetItem("" if val is None else str(val)))
        t.commit()
        self.mac_age.setText(f"show mac address-table · {T.fmt_age(sw.ages.get('mac_table'))} "
                             "(dynamic entries age out after 300 s)")

    def _highlight_selected_vlan(self):
        row = self.vlan_table.currentRow()
        if row < 0:
            row = next((r for r in range(self.vlan_table.rowCount())
                        if self.vlan_table.item(r, 0).data(Qt.UserRole) != 1), -1)
        if row >= 0:
            self.win.layers.set_vlan(int(self.vlan_table.item(row, 0).data(Qt.UserRole)))

    def _find_port_link(self):
        row = self.port_table.currentRow()
        if row < 0 or not self.key:
            return
        self.win.find_link(self.key, self.port_table.item(row, 0).data(Qt.UserRole))

    def _fill_interfaces(self, nv):
        nt = nv.tele
        t = self.if_table
        t.begin(self.key)
        for f in nt.ifaces:
            s = ev(f.side)
            word = SIDE_WORDS.get(s, s)
            col = SIDE_COLORS.get(s, T.NEUTRAL)
            r = t.rowCount()
            t.insertRow(r)
            name = QTableWidgetItem(_bar_icon(col if s in ("ADMIN_DOWN", "DOWN", "PROTO_DOWN") else "#00000000"),
                                    f.short)
            name.setToolTip(f.name)
            name.setData(Qt.UserRole, f.short)
            stat = QTableWidgetItem(word)
            stat.setForeground(QColor(col))
            fb = stat.font()
            fb.setBold(s != "UP")
            stat.setFont(fb)
            ospf = ""
            if f.ospf_net:
                ospf = {"POINT_TO_POINT": "P2P", "BROADCAST": "BCAST", "LOOPBACK": "LOOP"}.get(f.ospf_net, f.ospf_net)
                if f.ospf_state and f.ospf_state not in ("P2P", "LOOP"):
                    ospf += f" {f.ospf_state}"
                if f.ospf_passive:
                    ospf += " passive"
            ad = (getattr(nt, "addrs", None) or {}).get(f.name.lower())
            ip_item = QTableWidgetItem(f.cidr or "—")
            if ad is not None and ad.configured:
                cid = ad.cidrs
                txt = (cid[0] + (f" +{len(cid) - 1}" if len(cid) > 1 else "")) if cid else \
                    ("unnumbered" if ad.method == "unnumbered" else ad.method)
                ip_item = QTableWidgetItem(txt + (" (dhcp)" if ad.method == "dhcp" and cid else ""))
                ip_item.setToolTip("\n".join([f"{c}{' secondary' if i else ''}" for i, c in enumerate(cid)]
                                              + [f"source: {ad.source}"]))
            for c, it in enumerate((name, stat, ip_item, QTableWidgetItem(ospf),
                                    QTableWidgetItem(f.desc or ""))):
                t.setItem(r, c, it)
        t.commit()
        self.if_age.setText(_strip_html(_age_html(nt.ages.get("ifaces"))))

    def _find_iface_link(self):
        row = self.if_table.currentRow()
        if row < 0 or not self.key:
            return
        short = self.if_table.item(row, 0).data(Qt.UserRole)
        self.win.find_link(self.key, short)

    def _fill_routing(self, nv):
        nt = nv.tele
        dr = nt.default_route
        lines = []
        if dr is None:
            lines.append(f"<span style='color:{_gray()}'>not read yet</span>")
        elif not dr.present:
            lines.append("<b>No default route</b> (no 0.0.0.0/0 in the routing table).")
        else:
            src = {"S*": "a static route", "B*": "BGP", "O*E2": "OSPF (external type 2)",
                   "O*E1": "OSPF (external type 1)", "O*IA": "OSPF (inter-area)", "D*": "EIGRP",
                   "R*": "RIP"}.get(dr.code or "", dr.code or "?")
            lines.append(f"<div style='background:{T.alpha(T.ROUTE_BLUE, 0.10).name(QColor.HexArgb)};"
                         f"border-left:3px solid {T.ROUTE_BLUE}; padding:4px'>"
                         f"<b>0.0.0.0/0</b> &nbsp;<code>{html.escape(dr.source_label or dr.code or '')}</code> "
                         f"— learned from {html.escape(src)}" + (" (via DHCP)" if dr.via_dhcp else "") + "</div>")
            for ip, oif in dr.next_hops:
                lines.append(f"next hop <b>{html.escape(ip or 'directly connected')}</b>"
                             + (f" via {html.escape(oif)}" if oif else ""))
            if dr.blackhole:
                lines.append(f"<b style='color:{T.status_text("down").name()}'>points to Null0 (traffic is dropped)</b>")
            if dr.forwarding_checked:
                lines.append(f"<span style='color:{_gray()}'>next hop confirmed in the forwarding table (CEF)</span>")
        if nt.default_path is not None:
            st = ev(nt.default_path.state)
            word, col = PATH_WORDS.get(st, (st, T.NEUTRAL))
            lines.append(f"<br><b>Path</b> {status_badge_html(word, col)} {html.escape(nt.default_path.detail or '')}")
        self.rt_body.setText("<br>".join(lines))
        self.rt_card.set_age(nt.ages.get("default_route"), time.time())

    def _load_routes(self):
        if not self.key:
            return
        key = self.key
        self.rt_btn.setEnabled(False)
        self.rt_status.setText("reading 'show ip route'…")

        def done(res):
            self.rt_btn.setEnabled(True)
            if self.key != key:
                return
            if res.ok:
                self.rt_text.setPlainText(res.data.get("text") or "")
                self.rt_status.setText(f"read {time.strftime('%H:%M:%S')}")
            else:
                self.rt_status.setText(res.text)

        def fail(e):
            self.rt_btn.setEnabled(True)
            self.rt_status.setText(str(e))
        self.win.runner.submit(self.win.ctl.telemetry.route_table, key, on_done=done, on_error=fail)

    def _fill_protocols(self, nv):
        nt = nv.tele
        labels = self._labels()
        t = self.ospf_table
        t.begin(self.key)
        for n in nt.ospf_nbrs:
            r = t.rowCount()
            t.insertRow(r)
            state = str(n.get("adj_state") or n.get("state") or "")
            cells = [n.get("name") or n.get("neighbor_id") or "?", n.get("state") or "", n.get("role") or "",
                     n.get("iface") or "", n.get("address") or ""]
            for c, val in enumerate(cells):
                it = QTableWidgetItem(str(val))
                if c == 1:
                    it.setForeground(QColor(T.LINK_UP if state.upper().startswith("FULL") else T.AMBER))
                t.setItem(r, c, it)
        t.commit()
        self.ospf_card.setVisible(bool(nt.ospf_nbrs) or any(c.kind == "OSPF" for c in nv.chips))
        self.ospf_card.set_age(nt.ages.get("ospf_nbrs"), time.time())
        bgp = nt.bgp or {}
        peers = bgp.get("peers") or []
        self.bgp_head.setText(f"local AS <b>{html.escape(str(bgp.get('local_as')))}</b>"
                              + (f" · router-id {html.escape(str(bgp.get('rid')))}" if bgp.get("rid") else "")
                              if bgp else "")
        t = self.bgp_table
        t.begin(self.key)
        for p in peers:
            r = t.rowCount()
            t.insertRow(r)
            name = p.get("name")
            cells = [f"{name} ({p.get('ip')})" if name else p.get("ip"),
                     f"{p.get('remote_as')}" + (" eBGP" if p.get("ebgp") else " iBGP"),
                     p.get("state") or "", p.get("uptime") or "",
                     "" if p.get("pfx_rcvd") is None else str(p.get("pfx_rcvd"))]
            for c, val in enumerate(cells):
                it = QTableWidgetItem(str(val))
                if c == 2:
                    it.setForeground(QColor(T.LINK_UP if p.get("established") else T.AMBER))
                t.setItem(r, c, it)
        t.commit()
        self.bgp_card.setVisible(bool(bgp))
        self.bgp_card.set_age(nt.ages.get("bgp"), time.time())
        t = self.fhrp_table
        t.begin(self.key)
        for g in nt.fhrp:
            r = t.rowCount()
            t.insertRow(r)
            cells = [ev(g.proto), g.group, g.role, g.priority if g.priority is not None else "", g.vip or "",
                     g.iface]
            for c, val in enumerate(cells):
                it = QTableWidgetItem(str(val))
                if c == 2:
                    it.setForeground(QColor(T.PROTO_HUES.get(ev(g.proto), T.NEUTRAL)))
                    f = it.font()
                    f.setBold(g.is_primary)
                    it.setFont(f)
                t.setItem(r, c, it)
        t.commit()
        self.fhrp_card.setVisible(bool(nt.fhrp))
        self.fhrp_card.set_age(nt.ages.get("fhrp"), time.time())
        _ = labels
        self.proto_empty.setVisible(not (nt.ospf_nbrs or bgp or nt.fhrp or any(c.kind == "OSPF" for c in nv.chips)))
        for tb in (self.ospf_table, self.bgp_table, self.fhrp_table):
            hs = tb.horizontalScrollBar().sizeHint().height() if tb.horizontalHeader().length() > \
                tb.viewport().width() > 0 else 0
            tb.setFixedHeight(min(220, tb.horizontalHeader().sizeHint().height() + 4 + hs
                                  + 24 * max(1, tb.rowCount())))

    def _fill_config(self, nv):
        nt = nv.tele
        text = nt.config_text
        if text is not None and text != self.cfg_text.toPlainText():
            sb = self.cfg_text.verticalScrollBar().value()
            self.cfg_text.setPlainText(text)
            self.cfg_text.verticalScrollBar().setValue(sb)
        elif text is None:
            self.cfg_text.clear()
        self.cfg_age.setText(f"running-config · {T.fmt_age(nt.config_age_s)}" if nt.config_age_s is not None
                             else "running-config not read yet (needs the console at '#' or an enable secret)")

    def _refresh_config(self):
        if not self.key:
            return
        key = self.key
        self.cfg_btn.setEnabled(False)
        self.cfg_btn.setText("Reading…")

        def done(res):
            self.cfg_btn.setEnabled(True)
            self.cfg_btn.setText("Refresh")
            if not res.ok:
                self.cfg_age.setText(res.text)
                return
            if self.key == key and res.data.get("text"):
                self.cfg_text.setPlainText(res.data["text"])
                self.cfg_age.setText(f"running-config · read {time.strftime('%H:%M:%S')}")

        def fail(e):
            self.cfg_btn.setEnabled(True)
            self.cfg_btn.setText("Refresh")
            self.cfg_age.setText(str(e))
        self.win.runner.submit(self.win.ctl.telemetry.refresh_config, key, on_done=done, on_error=fail)

    def _apply_condition(self):
        if self.link_key and self.cond_combo.currentText():
            self.win.set_condition(self.link_key, self.cond_combo.currentText())


def login_summary(label: str, first=None) -> str:
    """'admin · password + enable secret (default)' - never the password values (the Inspector can be copied).
    ``first``: default_logins.FirstLogin used when nothing is saved."""
    from .. import default_logins as DL
    from ..probe import creds as creds_mod
    d = creds_mod.load_detail(label)
    if d.error:
        return "device-creds unreadable (mode must be 0600)"
    fl, tag = DL.effective(d, first)
    if fl is None:
        return "not set (Credentials…)"
    if not fl.username and not fl.password:
        return "no login: press Enter" + (" · enable: no password" if fl.has_enable and not fl.enable else "") \
            + " (default, not read from device)"
    parts = [fl.username or "no user"]
    have = [w for w, v in (("password", fl.password), ("enable secret", fl.enable)) if v]
    if have:
        parts.append(" + ".join(have))
    if fl.password_encrypted or fl.enable_encrypted:
        parts.append(DL.ENCRYPTED)
    tag_txt = {"default": " (LabDesk will try this default, not read from device)",
               "saved": " (saved in LabDesk)"}.get(tag or "", "")
    if tag == "saved" and d.uses_defaults:
        tag_txt = " (shared default, saved in LabDesk)"
    return " · ".join(parts) + tag_txt


def addressing_html(ad) -> str:
    """Inspector block for LinkFacts.addressing: the subnet(s), one line per end address, the plain
    explanation and where the data came from."""
    state = ev(ad.state)
    head = {"mismatch": f"<b style='color:{T.AMBER}'>{html.escape(ad.label)}</b>",
            "one_sided": f"<b>Subnet {html.escape(ad.nets[0] if ad.nets else ad.label)}</b> "
                         f"<span style='color:{_gray()}'>(½: one side configured)</span>",
            "dhcp": f"<b>{html.escape(ad.label)}</b>",
            "unnumbered": "<b>ip unnumbered</b>"}.get(state, f"<b>Subnet {html.escape(ad.label)}</b>")
    lines = (ad.tooltip or "").split("\n")
    body = [html.escape(x) for x in lines[1:-1]]
    src = html.escape(lines[-1]) if len(lines) > 1 else ""
    out = f"<div style='margin-top:4px'>{head}"
    if body:
        out += "<br>" + "<br>".join(body)
    if ad.explain:
        col = T.text_hex(T.AMBER) if state == "mismatch" else _gray()
        out += f"<br><span style='color:{col}'>{html.escape(ad.explain)}</span>"
    if src:
        out += f"<br><span style='color:{_gray()}; font-size:small'>{src}</span>"
    return out + "</div>"


def _table_text(t: QTableWidget) -> str:
    rows = ["\t".join(t.horizontalHeaderItem(c).text() for c in range(t.columnCount()))]
    for r in range(t.rowCount()):
        rows.append("\t".join((t.item(r, c).text() if t.item(r, c) else "") for c in range(t.columnCount())))
    return "\n".join(rows)


def _strip_html(s: str) -> str:
    import re
    s = re.sub(r"<br\s*/?>|</div>|</p>", "\n", s)
    s = re.sub(r"<[^>]+>", "", s)
    return html.unescape(s).replace("\xa0", " ").strip()


EC_STATE_WORDS = {"up": ("BUNDLED", T.LINK_UP), "partial": ("PARTIAL", T.AMBER), "down": ("DOWN", T.DOWN_RED),
                  "mismatch": ("MISMATCH", T.AMBER), "unknown": ("UNKNOWN", T.NEUTRAL)}


def _proto_word(side) -> str:
    return {"lacp": "LACP", "pagp": "PAgP", "on": "static (mode on)", "none": "none"}.get(ev(side.proto), ev(side.proto))


def _partner_text(m, labels: dict) -> str:
    who = f"{labels.get(m.partner_node, '?')} {m.partner_port or '?'}" if m.partner_node else (m.partner_dev or "?")
    src = (m.partner_source or "").upper()
    extra = f", prio {m.partner_priority}" if m.partner_priority is not None else ""
    return f"{who} ({src}{extra})" if src else who


def _member_tip(m, labels: dict) -> str:
    lines = [f"<b>{html.escape(m.port)}</b>: {html.escape(m.words)} - flags {html.escape(m.flag_words)}"]
    if m.mode:
        lines.append(f"mode {html.escape(m.mode)}")
    if m.lacp_state:
        lines.append(f"LACP state {html.escape(m.lacp_state)}, port priority {m.port_priority}")
    if m.partner_dev or m.partner_node:
        lines.append("partner " + html.escape(_partner_text(m, labels))
                     + (f", partner mode {html.escape(m.partner_mode)}" if m.partner_mode else "")
                     + (f", device {html.escape(m.partner_dev)}" if m.partner_dev else ""))
    if m.peer_node:
        lines.append(f"cable to {html.escape(labels.get(m.peer_node, '?'))} {html.escape(m.peer_port or '?')}")
    if m.problem:
        lines.append(f"<b>IOS:</b> {html.escape(m.problem)}")
    if m.speed or m.duplex or m.vlan is not None:
        lines.append(f"{html.escape(str(m.vlan))} · {html.escape(m.duplex or '?')} · {html.escape(m.speed or '?')}")
    return "<br>".join(lines)


_EC_SHORT = {"suspended": "susp", "stand-alone": "alone", "hot-standby": "standby", "waiting": "wait"}


def _stp_short(side) -> str:
    """'Altn BLK' (all VLANs alike) / 'Altn BLK v10 · Desg FWD v20'."""
    grp: dict[str, list[int]] = {}
    for v, (r, st) in sorted(side.stp.items()):
        grp.setdefault(f"{ev(r)} {ev(st)}", []).append(v)
    if len(grp) == 1:
        return next(iter(grp))
    from ..parsers.ios_l2 import vlan_text
    return " · ".join(f"{k} v{vlan_text(v)}" for k, v in grp.items())
