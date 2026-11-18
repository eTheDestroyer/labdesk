"""Dialogs (L9): one LabDeskDialog shell - 16 px margins, 8 px rows, 16 px sections, a footer
QDialogButtonBox with standard roles (KDE button order is kept) and role-styled buttons (primary = accent
fill, secondary = bordered, destructive primary = DOWN fill; 28 px, min width 88).

* :func:`confirm` replaces Yes/No questions with explicit verbs ('Pull Cable' / 'Cancel').
* :class:`TextPrompt` (New lab, Rename, Ping) validates inline (no bare input dialogs).
* :class:`OpenLabDialog`: CML labs table (Name, State pill, Nodes, Modified) + filter, and the Templates
  card grid; only Open lab / Templates carry an in-body header.
* :class:`AboutDialog` (Help menu): icon, version + git hash, CML / Qt versions, 'Copy diagnostics'
  (never reads the credentials file; the CML host is redacted unless asked for).
"""
from __future__ import annotations

import html as _html
import platform
import subprocess
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QGuiApplication, QIcon, QPainter, QPen
from PySide6.QtCore import QRectF
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout,
                               QFrame, QGridLayout, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QPlainTextEdit,
                               QPushButton, QScrollArea, QSizePolicy, QTableWidget, QTableWidgetItem, QTabBar,
                               QVBoxLayout, QWidget)

from .. import config, plat
from . import icons
from . import theme as T
from .widgets import StatusPill


# ============================================================================ shell
class LabDeskDialog(QDialog):
    """Base dialog: ``self.body`` (QVBoxLayout) + ``self.buttons`` (QDialogButtonBox footer)."""

    def __init__(self, title: str, parent=None, *, header: tuple | None = None):
        super().__init__(parent)
        self.setWindowTitle(title)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 16, 16, 16)
        outer.setSpacing(16)
        if header is not None:
            outer.addWidget(self._header(*header))
        self.body = QVBoxLayout()
        self.body.setSpacing(8)
        outer.addLayout(self.body, 1)
        self.buttons = QDialogButtonBox()
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        outer.addWidget(self.buttons)

    def _header(self, icon_name: str, title: str, subtitle: str = "") -> QWidget:
        w = QWidget()
        h = QHBoxLayout(w)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(12)
        ic = QLabel()
        ic.setPixmap(icons.pixmap(icon_name, 32, None, self.devicePixelRatioF() or 1.0, role="accent"))
        ic.setFixedSize(32, 32)
        h.addWidget(ic, 0, Qt.AlignTop)
        v = QVBoxLayout()
        v.setSpacing(2)
        self.header_title = QLabel(title)
        self.header_title.setFont(T.type("title"))
        v.addWidget(self.header_title)
        self.header_sub = QLabel(subtitle)
        self.header_sub.setObjectName("muted")
        self.header_sub.setWordWrap(True)
        self.header_sub.setVisible(bool(subtitle))
        v.addWidget(self.header_sub)
        h.addLayout(v, 1)
        return w

    def add_button(self, text: str, role=QDialogButtonBox.AcceptRole, kind: str = "secondary",
                   default: bool = False) -> QPushButton:
        """kind: primary | secondary | destructive."""
        b = self.buttons.addButton(text, role)
        if kind != "secondary":
            b.setProperty("role", kind)
            b.style().unpolish(b)
            b.style().polish(b)
        b.setAutoDefault(default)
        b.setDefault(default)
        return b

    def cancel_button(self, text: str = "Cancel") -> QPushButton:
        b = self.buttons.addButton(QDialogButtonBox.Cancel)
        b.setText(text)
        b.setAutoDefault(False)
        return b


def _msg_label(text: str, rich: bool = False) -> QLabel:
    lab = QLabel(text)
    lab.setWordWrap(True)
    lab.setTextFormat(Qt.RichText if rich else Qt.PlainText)
    lab.setTextInteractionFlags(Qt.TextSelectableByMouse)
    lab.setMinimumWidth(380)
    lab.setMaximumWidth(520)
    return lab


def confirm(parent, title: str, text: str, verb: str, *, destructive: bool = True, detail: str = "",
            cancel: str = "Cancel", icon_name: str | None = None) -> bool:
    """Explicit-verb confirmation ('Wipe Lab' / 'Cancel').  Returns True when the verb was chosen."""
    dlg = LabDeskDialog(title, parent)
    row = QHBoxLayout()
    row.setSpacing(12)
    name = icon_name or ("dialog-warning" if destructive else "dialog-information")
    ic = QLabel()
    ic.setPixmap(icons.pixmap(name, 32, None, dlg.devicePixelRatioF() or 1.0,
                              role="danger" if destructive else "accent"))
    ic.setFixedSize(32, 32)
    row.addWidget(ic, 0, Qt.AlignTop)
    col = QVBoxLayout()
    col.setSpacing(6)
    main = _msg_label(text)
    main.setFont(T.type("body", weight=600))
    col.addWidget(main)
    if detail:
        d = _msg_label(detail)
        d.setObjectName("muted")
        col.addWidget(d)
    row.addLayout(col, 1)
    dlg.body.addLayout(row)
    dlg.cancel_button(cancel)
    b = dlg.add_button(verb, QDialogButtonBox.AcceptRole, "destructive" if destructive else "primary",
                       default=not destructive)
    b.setMinimumWidth(max(88, b.sizeHint().width()))
    return dlg.exec() == QDialog.Accepted


def info(parent, title: str, text: str, detail: str = "", rich: bool = False) -> None:
    dlg = LabDeskDialog(title, parent)
    row = QHBoxLayout()
    row.setSpacing(12)
    ic = QLabel()
    ic.setPixmap(icons.pixmap("dialog-information", 32, None, dlg.devicePixelRatioF() or 1.0, role="accent"))
    ic.setFixedSize(32, 32)
    row.addWidget(ic, 0, Qt.AlignTop)
    col = QVBoxLayout()
    col.addWidget(_msg_label(text, rich))
    if detail:
        d = _msg_label(detail, rich)
        d.setObjectName("muted")
        col.addWidget(d)
    row.addLayout(col, 1)
    dlg.body.addLayout(row)
    dlg.add_button("OK", QDialogButtonBox.AcceptRole, "primary", default=True)
    dlg.exec()


class TextPrompt(LabDeskDialog):
    """One line of input with inline validation: ``validate(text) -> error message or ''``."""

    def __init__(self, title: str, label: str, text: str = "", verb: str = "OK", validate=None,
                 placeholder: str = "", parent=None):
        super().__init__(title, parent)
        self._validate = validate or (lambda t: "" if t.strip() else "Enter a value")
        lab = QLabel(label)
        lab.setWordWrap(True)
        self.body.addWidget(lab)
        self.edit = QLineEdit(text)
        self.edit.setPlaceholderText(placeholder)
        self.edit.setMinimumWidth(340)
        self.edit.selectAll()
        lab.setBuddy(self.edit)
        self.body.addWidget(self.edit)
        self.error = QLabel("")
        self.error.setObjectName("fielderror")
        self.error.setWordWrap(True)
        self.error.setFont(T.type("small"))
        self.body.addWidget(self.error)
        self.cancel_button()
        self.ok = self.add_button(verb, QDialogButtonBox.AcceptRole, "primary", default=True)
        self.edit.textChanged.connect(self._check)
        self._check(self.edit.text())

    def _check(self, text: str):
        err = self._validate(text)
        self.error.setText(err)
        self.error.setStyleSheet(f"color: {T.status_text('down').name()};")
        self.error.setVisible(bool(err) and bool(text))
        self.ok.setEnabled(not err)

    def value(self) -> str:
        return self.edit.text().strip()

    @classmethod
    def ask(cls, parent, title, label, text="", verb="OK", validate=None, placeholder="") -> str | None:
        dlg = cls(title, label, text, verb, validate, placeholder, parent)
        return dlg.value() if dlg.exec() == QDialog.Accepted else None


def validator(fn):
    """Wrap a labdesk_app.validate function as a TextPrompt validator (message without the repr)."""
    def check(text: str) -> str:
        if not text.strip():
            return "Required"
        try:
            fn(text)
            return ""
        except ValueError as e:
            msg = str(e)
            return msg.split(": ", 1)[1].capitalize() if ": " in msg else msg
    return check


# ============================================================================ Open lab / Templates
def _fmt_modified(v) -> str:
    if not v:
        return ""
    try:
        d = datetime.fromisoformat(str(v).replace("Z", "+00:00")).astimezone()
        now = datetime.now(d.tzinfo)
        if d.date() == now.date():
            return d.strftime("today %H:%M")
        return d.strftime("%d %b %Y")
    except ValueError:
        return str(v)[:16]


def template_info(p: Path) -> dict:
    out = {"path": str(p), "title": p.stem, "description": "", "nodes": None}
    try:
        import yaml
        d = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        lab = d.get("lab", {}) or {}
        out["title"] = lab.get("title", p.stem)
        out["description"] = (lab.get("description") or "").strip()
        out["nodes"] = len(d.get("nodes", []) or [])
    except Exception:  # noqa: BLE001
        pass
    return out


class TemplateCard(QFrame):
    clicked = Signal()
    activated = Signal()

    def __init__(self, info: dict, parent=None):
        super().__init__(parent)
        self.info = info
        self.selected = False
        self.setObjectName("tplcard")
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setMinimumHeight(112)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        v = QVBoxLayout(self)
        v.setContentsMargins(14, 12, 14, 12)
        v.setSpacing(4)
        t = QLabel(info["title"])
        t.setFont(T.type("label"))
        t.setWordWrap(True)
        v.addWidget(t)
        d = QLabel(info["description"] or "No description.")
        d.setObjectName("muted")
        d.setFont(T.type("small"))
        d.setWordWrap(True)
        d.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        v.addWidget(d, 1)
        n = info.get("nodes")
        c = QLabel(f"{n} nodes" if n is not None else "")
        c.setObjectName("muted")
        c.setFont(T.tabular("small"))
        v.addWidget(c)
        self.setToolTip(info["description"])

    def set_selected(self, on: bool):
        self.selected = on
        self.update()

    def mousePressEvent(self, ev):
        self.setFocus()
        self.clicked.emit()

    def mouseDoubleClickEvent(self, ev):
        self.activated.emit()

    def keyPressEvent(self, ev):
        if ev.key() in (Qt.Key_Return, Qt.Key_Enter, Qt.Key_Space):
            self.clicked.emit()
            if ev.key() != Qt.Key_Space:
                self.activated.emit()
            return
        super().keyPressEvent(ev)

    def focusInEvent(self, ev):
        super().focusInEvent(ev)
        self.clicked.emit()

    def paintEvent(self, ev):
        tk = T.tokens()
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        if self.selected:
            p.setPen(QPen(tk.accent_ring, 2))
            p.setBrush(tk.accent_subtle)
            r = r.adjusted(0.5, 0.5, -0.5, -0.5)
        else:
            p.setPen(QPen(tk.N(7) if tk.dark else tk.N(6), 1))
            p.setBrush(tk.N(1) if not tk.dark else tk.N(3))
        p.drawRoundedRect(r, T.RADIUS_MD, T.RADIUS_MD)
        p.end()


class OpenLabDialog(LabDeskDialog):
    """Pick an existing CML lab, or import a template / YAML file.  ``page``: 'labs' | 'templates'."""

    def __init__(self, labs: list[dict], parent=None, page: str = "labs"):
        super().__init__("Open lab", parent, header=("document-open", "Open lab",
                                                     f"Labs on the CML controller ({config.CML_HOST})"))
        self.resize(720, 520)
        self.result_kind = None       # "lab" | "template"
        self.result_value = None
        self.labs = labs
        self.tabbar = QTabBar()
        self.tabbar.setExpanding(False)
        self.tabbar.addTab(f"CML labs ({len(labs)})")
        self.tabbar.addTab("Templates")
        self.tabbar.currentChanged.connect(self._page)
        self.body.addWidget(self.tabbar)
        from PySide6.QtWidgets import QStackedWidget
        self.stack = QStackedWidget()
        self.body.addWidget(self.stack, 1)
        # ---- labs
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)
        self.filter = QLineEdit()
        self.filter.setPlaceholderText("Filter labs")
        self.filter.setClearButtonEnabled(True)
        self.filter.textChanged.connect(self._filter)
        lay.addWidget(self.filter)
        t = self.table = QTableWidget(len(labs), 4)
        t.setHorizontalHeaderLabels(["Name", "State", "Nodes", "Modified"])
        t.verticalHeader().hide()
        t.verticalHeader().setDefaultSectionSize(32)
        t.setShowGrid(False)
        t.setAlternatingRowColors(True)
        t.setSelectionBehavior(QAbstractItemView.SelectRows)
        t.setSelectionMode(QAbstractItemView.SingleSelection)
        t.setEditTriggers(QAbstractItemView.NoEditTriggers)
        hh = t.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.Stretch)
        hh.setSectionResizeMode(1, QHeaderView.Fixed)
        t.setColumnWidth(1, 112)
        for c in (2, 3):
            hh.setSectionResizeMode(c, QHeaderView.ResizeToContents)
        hh.setDefaultAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        hh.setHighlightSections(False)
        for r, lab in enumerate(labs):
            it = QTableWidgetItem(lab.get("lab_title", lab.get("id")))
            it.setData(Qt.UserRole, lab.get("id"))
            it.setFont(T.type("body", weight=600))
            t.setItem(r, 0, it)
            st = str(lab.get("state", ""))
            on = st.upper() in ("STARTED", "BOOTED")
            holder = QWidget()
            hl = QHBoxLayout(holder)
            hl.setContentsMargins(6, 0, 10, 0)
            hl.addWidget(StatusPill(T.state_label(st), "up" if on else "neutral", "up" if on else "stopped",
                                    height=20))
            hl.addStretch(1)
            si = QTableWidgetItem("")
            si.setData(Qt.UserRole, T.state_label(st))
            si.setToolTip(T.state_label(st))
            t.setItem(r, 1, si)
            t.setCellWidget(r, 1, holder)
            n = QTableWidgetItem(str(lab.get("node_count", "")))
            n.setFont(T.tabular("body"))
            n.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            t.setItem(r, 2, n)
            m = QTableWidgetItem(_fmt_modified(lab.get("modified") or lab.get("created")))
            m.setForeground(T.tokens().N(11))
            t.setItem(r, 3, m)
        if labs:
            t.selectRow(0)
        t.doubleClicked.connect(self._accept_lab)
        lay.addWidget(t, 1)
        self.empty = QLabel("No labs on the controller yet - create one or start from a template.")
        self.empty.setObjectName("muted")
        self.empty.setVisible(not labs)
        lay.addWidget(self.empty)
        self.stack.addWidget(w)
        # ---- templates
        tw = QWidget()
        tl = QVBoxLayout(tw)
        tl.setContentsMargins(0, 0, 0, 0)
        tl.setSpacing(8)
        sub = QLabel("Each template is imported as a new lab on the controller.")
        sub.setObjectName("muted")
        tl.addWidget(sub)
        sc = QScrollArea()
        sc.setWidgetResizable(True)
        sc.setFrameShape(QFrame.NoFrame)
        grid_w = QWidget()
        grid = QGridLayout(grid_w)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setSpacing(12)
        self.cards: list[TemplateCard] = []
        for i, p in enumerate(sorted(config.TEMPLATES_DIR.glob("*.yaml"))):
            card = TemplateCard(template_info(p))
            card.clicked.connect(lambda c=card: self._select_card(c))
            card.activated.connect(lambda c=card: (self._select_card(c), self._accept_tpl()))
            grid.addWidget(card, i // 2, i % 2)
            self.cards.append(card)
        grid.setRowStretch(len(self.cards) // 2 + 1, 1)
        sc.setWidget(grid_w)
        tl.addWidget(sc, 1)
        self.stack.addWidget(tw)
        # ---- footer
        self.browse = self.buttons.addButton("Import YAML file…", QDialogButtonBox.ActionRole)
        self.browse.setAutoDefault(False)
        self.browse.clicked.connect(self._browse)
        self.cancel_button()
        self.open_btn = self.add_button("Open", QDialogButtonBox.AcceptRole, "primary", default=True)
        self.buttons.accepted.disconnect()
        self.buttons.accepted.connect(self._accept)
        if self.cards:
            self._select_card(self.cards[0])
        self.tabbar.setCurrentIndex(1 if page == "templates" else 0)
        self._page(self.tabbar.currentIndex())

    # compat: the old dialog exposed a QTabWidget
    @property
    def tabs(self):
        return self.tabbar

    def _page(self, i: int):
        self.stack.setCurrentIndex(i)
        if i == 0:
            self.header_title.setText("Open lab")
            self.header_sub.setText(f"Labs on the CML controller ({config.CML_HOST})")
            self.open_btn.setText("Open")
            self.open_btn.setEnabled(bool(self.labs))
            self.filter.setFocus()
        else:
            self.header_title.setText("New lab from a template")
            self.header_sub.setText("Pick a starting topology; you can change everything afterwards.")
            self.open_btn.setText("Create Lab")
            self.open_btn.setEnabled(bool(self.cards))
        self.setWindowTitle("Open lab" if i == 0 else "Templates")

    def _filter(self, text: str):
        q = text.lower().strip()
        first = None
        for r in range(self.table.rowCount()):
            hit = not q or q in (self.table.item(r, 0).text() or "").lower()
            self.table.setRowHidden(r, not hit)
            if hit and first is None:
                first = r
        if first is not None:
            self.table.selectRow(first)

    def _select_card(self, card):
        for c in self.cards:
            c.set_selected(c is card)

    def _accept_lab(self):
        row = self.table.currentRow()
        if row >= 0 and not self.table.isRowHidden(row):
            self.result_kind, self.result_value = "lab", self.table.item(row, 0).data(Qt.UserRole)
            self.accept()

    def _accept_tpl(self):
        sel = next((c for c in self.cards if c.selected), None)
        if sel:
            self.result_kind, self.result_value = "template", sel.info["path"]
            self.accept()

    def _browse(self):
        path, _ = QFileDialog.getOpenFileName(self, "Import CML topology", str(Path.home()),
                                              "CML topology (*.yaml *.yml)")
        if path:
            self.result_kind, self.result_value = "template", path
            self.accept()

    def _accept(self):
        if self.tabbar.currentIndex() == 0:
            self._accept_lab()
        else:
            self._accept_tpl()


# ============================================================================ startup config / properties
class ConfigDialog(LabDeskDialog):
    def __init__(self, label: str, text: str, parent=None):
        super().__init__(f"Startup configuration - {label}", parent)
        self.resize(680, 600)
        info_ = QLabel("Stored in CML as the node's day-0 configuration. It is applied on the "
                       "<b>first boot after a wipe</b>; a running node keeps its current config.")
        info_.setWordWrap(True)
        info_.setObjectName("muted")
        self.body.addWidget(info_)
        self.edit = QPlainTextEdit()
        self.edit.setFont(T.mono())
        self.edit.setPlainText(text)
        self.edit.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.body.addWidget(self.edit, 1)
        self.wipe_after = False
        self.wipe_btn = self.add_button("Save, Wipe && Restart", QDialogButtonBox.AcceptRole)
        self.wipe_btn.setToolTip("Save, wipe the node's disk and start it again: the new config applies now "
                                 "(running config is lost)")
        self.wipe_btn.clicked.connect(lambda: setattr(self, "wipe_after", True))
        self.cancel_button()
        self.add_button("Save", QDialogButtonBox.AcceptRole, "primary", default=True)

    def text(self) -> str:
        return self.edit.toPlainText()


class PropertiesDialog(LabDeskDialog):
    def __init__(self, label: str, info: dict, mgmt: dict, parent=None):
        super().__init__(f"Properties - {label}", parent)
        self.resize(480, 0)
        form = QFormLayout()
        form.setHorizontalSpacing(16)
        form.setVerticalSpacing(8)
        form.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
        for k, v in info.items():
            lab = QLabel(str(v))
            lab.setTextInteractionFlags(Qt.TextSelectableByMouse)
            if k in ("ID", "Detected IP"):
                lab.setFont(T.mono(1.0, "small"))
            key = QLabel(k)
            key.setObjectName("muted")
            form.addRow(key, lab)
        self.ip = QLineEdit(mgmt.get("ip", ""))
        self.ip.setPlaceholderText("optional, e.g. 192.168.122.50")
        self.ip.setFont(T.mono())
        self.user = QLineEdit(mgmt.get("user", "admin"))
        form.addRow("Management IP", self.ip)
        form.addRow("Login user", self.user)
        self.body.addLayout(form)
        hint = QLabel("With a management IP the context menu offers SSH / Telnet / HTTP(S).")
        hint.setWordWrap(True)
        hint.setObjectName("muted")
        self.body.addWidget(hint)
        self.error = QLabel("")
        self.error.setStyleSheet(f"color: {T.status_text('down').name()};")
        self.error.setFont(T.type("small"))
        self.body.addWidget(self.error)
        self.cancel_button()
        self.ok = self.add_button("Save", QDialogButtonBox.AcceptRole, "primary", default=True)
        self.ip.textChanged.connect(self._check)
        self.user.textChanged.connect(self._check)
        self._check()

    def _check(self, *_):
        from .. import validate
        err = ""
        if self.ip.text().strip():
            try:
                validate.host(self.ip.text())
            except ValueError:
                err = "Not an IP address or host name"
        if not err and self.user.text().strip():
            try:
                validate.username(self.user.text())
            except ValueError:
                err = "User names use letters, digits, '_', '.', '-'"
        self.error.setText(err)
        self.error.setVisible(bool(err))
        self.ok.setEnabled(not err)


# ============================================================================ Credentials
def secret_field(text: str = "", placeholder: str = "") -> QLineEdit:
    """A password QLineEdit with a trailing show / hide eye."""
    e = QLineEdit(text)
    e.setEchoMode(QLineEdit.Password)
    e.setPlaceholderText(placeholder)
    e.setFont(T.mono())
    act = e.addAction(icons.icon("view-visible", "muted"), QLineEdit.TrailingPosition)
    act.setToolTip("Show")

    def toggle():
        show = e.echoMode() == QLineEdit.Password
        e.setEchoMode(QLineEdit.Normal if show else QLineEdit.Password)
        act.setIcon(icons.icon("view-hidden" if show else "view-visible", "muted"))
        act.setToolTip("Hide" if show else "Show")
    act.triggered.connect(toggle)
    e.eye = act                     # QA / tests
    return e


class ShowScriptDialog(LabDeskDialog):
    """One device's show script: the read-only commands LabDesk types in its console when it is opened
    (``show_scripts``).  *Suggest* fills the editor from what the device is running right now (live facts);
    *Copy from* takes another device's saved script.  ``result_script()`` is the validated script,
    ``run_now`` says the user asked to run it straight away."""

    def __init__(self, label: str, script, suggestion: list[str], others: dict | None = None,
                 can_run: bool = True, parent=None):
        from PySide6.QtWidgets import QComboBox
        from .. import show_scripts as SS
        super().__init__(f"Show script - {label}", parent,
                         header=("utilities-terminal", f"Show script for {label}",
                                 "The show commands LabDesk types for you in this device's console. "
                                 "Each device has its own script."))
        self.SS = SS
        self.label = label
        self.suggestion = list(suggestion)
        self.others = dict(others or {})
        self.run_now = False
        self.resize(520, 0)
        self.edit = QPlainTextEdit()
        self.edit.setFont(T.mono())
        self.edit.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.edit.setPlaceholderText("one command per line, for example:\nshow ip interface brief\nshow standby brief")
        self.edit.setMinimumHeight(190)
        self.edit.setPlainText(script.text() if script is not None else "")
        self.body.addWidget(self.edit)
        row = QHBoxLayout()
        row.setSpacing(8)
        self.suggest_btn = QPushButton("Suggest from live data")
        self.suggest_btn.setToolTip("Fill the script from what this device is running right now "
                                    "(HSRP, BGP, OSPF, VLANs… read from the device)")
        self.suggest_btn.clicked.connect(self._suggest)
        row.addWidget(self.suggest_btn)
        self.copy_from = QComboBox()
        self.copy_from.addItem("Copy from…")
        for name in sorted(self.others):
            self.copy_from.addItem(name)
        self.copy_from.setEnabled(bool(self.others))
        self.copy_from.activated.connect(self._copy)
        row.addWidget(self.copy_from)
        row.addStretch(1)
        self.clear_btn = QPushButton("Clear")
        self.clear_btn.clicked.connect(lambda: self.edit.setPlainText(""))
        row.addWidget(self.clear_btn)
        self.body.addLayout(row)
        self.on_open = QCheckBox("Run this script each time I open the console")
        self.on_open.setChecked(script.on_open if script is not None else True)
        self.body.addWidget(self.on_open)
        hint = QLabel("Only show, ping and traceroute commands: the script can never change a device. "
                      "A line starting with ! is a comment. An empty script removes it. "
                      "Saved in the LabDesk settings folder (show-scripts.json).")
        hint.setWordWrap(True)
        hint.setObjectName("muted")
        hint.setFont(T.type("small"))
        self.body.addWidget(hint)
        self.error = QLabel("")
        self.error.setWordWrap(True)
        self.error.setStyleSheet(f"color: {T.status_text('down').name()};")
        self.error.setFont(T.type("small"))
        self.body.addWidget(self.error)
        self.run_btn = self.add_button("Save and run now", QDialogButtonBox.ActionRole)
        self.run_btn.setToolTip("Save the script and type it on the console now" if can_run
                                else "The device is not running")
        self.run_btn.clicked.connect(self._run)
        self._can_run = can_run
        self.cancel_button()
        self.ok = self.add_button("Save", QDialogButtonBox.AcceptRole, "primary", default=True)
        self.edit.textChanged.connect(self._check)
        self._check()

    def _suggest(self):
        self.edit.setPlainText("\n".join(self.suggestion))

    def _copy(self, index: int):
        name = self.copy_from.itemText(index)
        other = self.others.get(name)
        if other is not None:
            self.edit.setPlainText(other.text())
        self.copy_from.setCurrentIndex(0)

    def commands(self) -> list[str]:
        return self.SS.parse(self.edit.toPlainText())

    def _check(self, *_):
        err = ""
        cmds: list[str] = []
        try:
            cmds = self.commands()
        except self.SS.ScriptError as e:
            err = str(e)
        self.error.setText(err)
        self.error.setVisible(bool(err))
        self.ok.setEnabled(not err)
        self.run_btn.setEnabled(not err and bool(cmds) and self._can_run)

    def _run(self):
        self.run_now = True
        self.accept()

    def result_script(self):
        """The script to save; ``None`` = remove this device's script (the editor is empty)."""
        cmds = self.commands()
        return self.SS.ShowScript(cmds, self.on_open.isChecked()) if cmds else None


class CredentialsDialog(LabDeskDialog):
    """Login user, password and enable secret LabDesk uses on one device's console (probes: login prompt,
    'enable').  Stored in ``~/.config/labdesk/device-creds`` (0600) by ``probe.creds.save``; the values never go
    to the event log, state.json, LabDesk.ini or CML.  *Use the default login* edits the shared DEFAULT_* set
    instead and drops this device's own values."""

    FIELDS = ("USERNAME", "PASSWORD", "ENABLE")
    NAMES = {"USERNAME": "Username", "PASSWORD": "Password", "ENABLE": "Enable secret"}

    def __init__(self, label: str, detail, parent=None, first_login=None):
        super().__init__(f"Credentials - {label}", parent,
                         header=("object-locked", f"Credentials for {label}",
                                 "The login LabDesk uses on this device's console (probes and auto-configure)."))
        self.label = label
        self.detail = detail
        self.resize(460, 0)
        self.defaults_box = QCheckBox("Use the default login (shared by every device without its own)")
        self.defaults_box.setChecked(bool(detail.uses_defaults and detail.defaults))
        self.body.addWidget(self.defaults_box)
        self.scope = QLabel("")
        self.scope.setFont(T.type("caption", upper=True))
        self.scope.setObjectName("muted")
        self.body.addWidget(self.scope)
        form = QFormLayout()
        form.setHorizontalSpacing(16)
        form.setVerticalSpacing(8)
        form.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.user = QLineEdit()
        self.user.setFont(T.mono())
        self.password = secret_field()
        self.enable = secret_field()
        form.addRow("Username", self.user)
        form.addRow("Password", self.password)
        form.addRow("Enable secret", self.enable)
        self.body.addLayout(form)
        hint = QLabel("Saved in ~/.config/labdesk/device-creds (mode 0600) - never in the event log, LabDesk.ini, "
                      "state.json or CML. An empty field falls back to the default login.")
        hint.setWordWrap(True)
        hint.setObjectName("muted")
        hint.setFont(T.type("small"))
        self.body.addWidget(hint)
        if detail.error:
            warn = QLabel(detail.error)
            warn.setWordWrap(True)
            warn.setStyleSheet(f"color: {T.status_text('warn').name()};")
            self.body.addWidget(warn)
        self.error = QLabel("")
        self.error.setStyleSheet(f"color: {T.status_text('down').name()};")
        self.error.setFont(T.type("small"))
        self.body.addWidget(self.error)
        self.first_login = first_login
        self.reset_requested = False
        if first_login is not None:
            fl_hint = QLabel(f"First login (default): {first_login.summary(show=False)}"
                             + (f" - {first_login.note}" if first_login.note else ""))
            fl_hint.setWordWrap(True)
            fl_hint.setObjectName("muted")
            fl_hint.setFont(T.type("small"))
            self.body.addWidget(fl_hint)
            self.reset_btn = self.add_button("Reset to default", QDialogButtonBox.ResetRole)
            self.reset_btn.setToolTip("Replace this device's saved login with its first-login default "
                                      "(day-0 config / platform default)")
            self.reset_btn.clicked.connect(self._reset)
        self.cancel_button()
        self.ok = self.add_button("Save", QDialogButtonBox.AcceptRole, "primary", default=True)
        self._own = dict(detail.own)
        self._dflt = dict(detail.defaults)
        self._mode = None
        self.defaults_box.toggled.connect(self._switch)
        for e in (self.user, self.password, self.enable):
            e.textChanged.connect(self._check)
        self._switch(self.defaults_box.isChecked())

    def _fields(self):
        return dict(zip(self.FIELDS, (self.user, self.password, self.enable)))

    def _switch(self, use_defaults: bool):
        # keep what was typed in the other mode
        if self._mode is not None:
            store = self._dflt if self._mode else self._own
            for k, e in self._fields().items():
                store[k] = e.text()
        self._mode = bool(use_defaults)
        src = self._dflt if use_defaults else self._own
        for k, e in self._fields().items():
            e.blockSignals(True)
            e.setText(src.get(k, "") or "")
            e.blockSignals(False)
            if use_defaults:
                e.setPlaceholderText("not set")
            else:
                d = self._dflt.get(k)
                e.setPlaceholderText(("default: " + (d if k == "USERNAME" else "set")) if d else "not set")
        self.scope.setText("Default login - all devices without their own" if use_defaults
                           else f"Login for {self.label} only")
        self._check()

    def _check(self, *_):
        from ..probe.creds import check_value
        err = ""
        for k, e in self._fields().items():
            msg = check_value(k, e.text())
            if msg:
                err = f"{self.NAMES[k]}: {msg}"
                break
        self.error.setText(err)
        self.error.setVisible(bool(err))
        self.ok.setEnabled(not err)

    def _reset(self):
        self.reset_requested = True
        self.accept()

    def use_defaults(self) -> bool:
        return self.defaults_box.isChecked()

    def values(self) -> dict:
        return {k: e.text() for k, e in self._fields().items()}


# ============================================================================ About
def git_hash() -> str:
    build = config.APP_DIR / "BUILD_INFO"                # LabDesk.exe: written by the Windows CI build
    if plat.FROZEN:
        try:
            return build.read_text(encoding="utf-8").strip()[:40]
        except OSError:
            return ""
    try:
        out = subprocess.run(["git", "-C", str(config.APP_DIR), "rev-parse", "--short", "HEAD"],
                             capture_output=True, text=True, timeout=2, **plat.no_window_kwargs())
        return out.stdout.strip() if out.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


class AboutDialog(LabDeskDialog):
    def __init__(self, cml_version: str = "", parent=None):
        super().__init__("About LabDesk", parent)
        from .. import __version__
        import PySide6
        from PySide6.QtCore import qVersion
        gh = git_hash()
        self.version = __version__ + (f" ({gh})" if gh else "")
        self.cml_version = cml_version or "not connected"
        self.qt = f"Qt {qVersion()} · PySide6 {PySide6.__version__} · Python {platform.python_version()}"
        v = QVBoxLayout()
        v.setSpacing(6)
        ic = QLabel()
        ic.setPixmap(QIcon(str(config.ICON_PATH)).pixmap(96, 96))
        ic.setAlignment(Qt.AlignHCenter)
        v.addWidget(ic)
        v.addSpacing(6)
        name = QLabel("LabDesk")
        name.setFont(T.type("display"))
        name.setAlignment(Qt.AlignHCenter)
        v.addWidget(name)
        ver = QLabel(f"Version {self.version}")
        ver.setFont(T.tabular("small"))
        ver.setObjectName("muted")
        ver.setAlignment(Qt.AlignHCenter)
        v.addWidget(ver)
        v.addSpacing(8)
        tag = QLabel("Build and run Cisco CML labs with live protocol state and real serial consoles.")
        tag.setWordWrap(True)
        tag.setAlignment(Qt.AlignHCenter)
        v.addWidget(tag)
        v.addSpacing(8)
        form = QFormLayout()
        form.setHorizontalSpacing(12)
        form.setLabelAlignment(Qt.AlignRight)
        for k, val in (("CML", self.cml_version), ("Toolkit", self.qt),
                       ("Licence", "Personal project · bundled Breeze icons: LGPL-3.0-or-later")):
            kl = QLabel(k)
            kl.setObjectName("muted")
            vl = QLabel(val)
            vl.setWordWrap(True)
            vl.setFont(T.type("small"))
            vl.setTextInteractionFlags(Qt.TextSelectableByMouse)
            form.addRow(kl, vl)
        v.addLayout(form)
        self.body.addLayout(v)
        self.include_host = QCheckBox("Include the CML host address")
        self.include_host.setChecked(False)
        self.body.addWidget(self.include_host)
        cp = self.buttons.addButton("Copy Diagnostics", QDialogButtonBox.ActionRole)
        cp.setAutoDefault(False)
        cp.clicked.connect(self.copy_diagnostics)
        self.add_button("Close", QDialogButtonBox.AcceptRole, "primary", default=True)
        self.setMinimumWidth(460)

    def diagnostics(self) -> str:
        """Never reads the credentials file; the CML host is redacted unless the box is ticked."""
        host = config.CML_HOST if self.include_host.isChecked() else "<redacted>"
        return "\n".join([f"LabDesk {self.version}", f"CML {self.cml_version} at {host}", self.qt,
                          f"{platform.system()} {platform.release()} · {QGuiApplication.platformName()}",
                          f"Theme {T.tokens().mode} · font {T.base_font().family()} {T.base_pt():g} pt"])

    def copy_diagnostics(self):
        QGuiApplication.clipboard().setText(self.diagnostics())


class CloudConfigDialog(LabDeskDialog):
    """'Auto-configure for Internet cloud': shows the plan (address, commands, day-0, OSPF, return path)
    and lets the user pick the steps.  Nothing is typed into a console before the user clicks Apply."""

    def __init__(self, plan, health=None, parent=None):
        super().__init__(f"Auto-configure {plan.router_label} for the Internet cloud", parent)
        self.plan = plan
        self.resize(660, 700)
        v = self.body
        conf = plan.conf
        two = ", ".join(conf.headline_answers[:2])
        head = QLabel(
            f"<b>{_html.escape(plan.router_label)} {_html.escape(plan.iface)}</b> ⟷ "
            f"<b>{_html.escape(plan.cloud_label)}</b> (simulated Internet, answers {two})<br>"
            f"Address <b>{plan.ip}/{conf.prefixlen}</b>"
            + (" (already configured)" if plan.reused else " (next free in " + conf.net_text + ")")
            + f", default route via <b>{conf.cloud_ip}</b>.")
        head.setWordWrap(True)
        v.addWidget(head)
        if health is not None and not health.ok:
            warn = QLabel(f"<span style='color:{T.status_text("warn").name()}'><b>{_html.escape(health.headline)}</b> - pings will fail "
                          f"until the host service runs: <code>{_html.escape(health.hint)}</code></span>")
            warn.setWordWrap(True)
            warn.setTextInteractionFlags(Qt.TextSelectableByMouse)
            v.addWidget(warn)
        self.cb_live = QCheckBox(f"Configure {plan.router_label} now through its console "
                                 "(enable secret 'cisco', then write memory)")
        self.cb_live.setChecked(plan.booted)
        self.cb_live.setEnabled(plan.booted)
        if not plan.booted:
            self.cb_live.setText(self.cb_live.text() + f" - {plan.router_label} is not running")
        v.addWidget(self.cb_live)
        if plan.console_open:
            note = QLabel(f"<span style='color:{T.status_text("warn").name()}'>{_html.escape(plan.router_label)}'s console is open on this "
                          "host. LabDesk will type the lines below on that same console line (you will see them). "
                          "If you are in configuration mode or at a --More-- prompt it stops without typing. "
                          "Untick the box above to only update the day-0 config.</span>")
            note.setWordWrap(True)
            v.addWidget(note)
        self.cb_restart = QCheckBox(f"Wipe && restart {plan.router_label} afterwards")
        self.cb_restart.setToolTip("CML wires a cable added after the node's first boot into the VM only after a "
                                   "wipe. Running config is lost; it boots from the updated day-0 config (~3 min).")
        self.cb_restart.setChecked(plan.needs_wipe)
        self.cb_restart.setVisible(plan.needs_wipe or plan.booted)
        if plan.needs_wipe:
            nw = QLabel(f"<span style='color:{T.status_text("warn").name()}'><b>{_html.escape(plan.iface)}</b> is not wired into the running "
                        f"{_html.escape(plan.router_label)} yet: CML connects a cable added after the node's first "
                        "boot only once the node is wiped. The console step is skipped when you wipe.</span>")
            nw.setWordWrap(True)
            v.addWidget(nw)
            self.cb_live.setChecked(False)
        v.addWidget(self.cb_restart)
        if plan.booted:
            v.addWidget(self._sub("Needed only when the new cable stays down inside the router: CML connects a "
                                  "cable added after the node's first boot once the node is wiped (~3 min; it "
                                  "boots from the updated day-0 config)."))
        if plan.day0_has:
            d0 = "Day-0 config in CML already contains the uplink"
        elif plan.day0_now:
            d0 = f"Store it in {plan.router_label}'s CML day-0 config (the node is wiped: written now)"
        else:
            d0 = (f"Store it in {plan.router_label}'s CML day-0 config (CML locks it until the node is wiped: "
                  "LabDesk writes it on the next wipe)")
        self.cb_day0 = QCheckBox(d0.split(" (")[0])
        self.cb_day0.setChecked(not plan.day0_has)
        v.addWidget(self.cb_day0)
        if " (" in d0:
            v.addWidget(self._sub("(" + d0.split(" (", 1)[1]))
        self.cb_ospf = QCheckBox("Originate the default route into OSPF (default-information originate"
                                 + (f", process {', '.join(plan.ospf)})" if plan.ospf else ")"))
        self.cb_ospf.setChecked(bool(plan.ospf))
        self.cb_ospf.setVisible(bool(plan.ospf))
        v.addWidget(self.cb_ospf)
        rp = plan.routes
        self.cb_routes = QCheckBox("Return path for hosts behind the router")
        self.cb_routes.setChecked(True)
        v.addWidget(self.cb_routes)
        v.addWidget(self._sub(rp.text + " (written to /var/lib/labdesk-cloud/routes.conf, applied by "
                              "labdesk-cloud-routes.path - no password)"))
        if not plan.routes_writable:
            from .. import cloud as _cloud
            cmd = _cloud.manual_routes_command(_cloud.render_routes(rp.edge, rp.routes))
            nw = QLabel(f"<span style='color:{T.status_text("warn").name()}'>/var/lib/labdesk-cloud is not writable for you (cloud not "
                        "installed?). Run once:</span><br><code>" + _html.escape(cmd) + "</code>")
            nw.setWordWrap(True)
            nw.setTextInteractionFlags(Qt.TextSelectableByMouse)
            v.addWidget(nw)
        cmds = QLabel("Commands (configure terminal)")
        cmds.setFont(T.type("caption", upper=True))
        cmds.setObjectName("muted")
        v.addSpacing(8)
        v.addWidget(cmds)
        self.preview = QPlainTextEdit()
        self.preview.setReadOnly(True)
        self.preview.setFont(T.mono())
        v.addWidget(self.preview, 1)
        self.cb_ospf.toggled.connect(self._refresh)
        self._refresh()
        self.cancel_button()
        self.apply_btn = self.add_button("Apply", QDialogButtonBox.AcceptRole, "primary", default=True)

    @staticmethod
    def _sub(text: str) -> QLabel:
        lab = QLabel(text)
        lab.setWordWrap(True)
        lab.setObjectName("muted")
        lab.setFont(T.type("small"))
        lab.setContentsMargins(24, 0, 0, 0)
        return lab

    def _refresh(self):
        from .. import cloud as _cloud
        p = self.plan
        lines = _cloud.live_commands(p.iface, p.ip, p.conf, p.ospf if self.cb_ospf.isChecked() else [])
        self.preview.setPlainText("\n".join(lines) + "\nend\nwrite memory\n")

    def choices(self) -> dict:
        restart = self.cb_restart.isChecked()
        return {"live": self.cb_live.isChecked() and not restart, "day0": self.cb_day0.isChecked() or restart,
                "ospf": self.cb_ospf.isChecked() and bool(self.plan.ospf), "routes": self.cb_routes.isChecked(),
                "restart": restart}


# ============================================================================ EtherChannel
class BundleDialog(LabDeskDialog):
    """'Bundle into EtherChannel…': protocol (LACP / PAgP / Static on), the mode of each side (defaults LACP
    active/active, PAgP desirable/desirable) with an inline warning for combinations that never form, the
    Port-channel number (next free on both switches), the members' switchport config copied onto the
    Port-channel, optional load-balance, and the exact commands for both switches.  Nothing is typed before
    Apply."""

    def __init__(self, plan, parent=None):
        from PySide6.QtWidgets import QButtonGroup, QComboBox, QRadioButton, QSpinBox, QTabWidget
        from .. import etherchannel as EC
        a, b = plan.a, plan.b
        super().__init__("Bundle into EtherChannel", parent,
                         header=("network-wired", f"Bundle {len(a.members)} cables {a.label} ⟷ {b.label}",
                                 " · ".join(f"{EC.short_ifname(x)}↔{EC.short_ifname(y)}"
                                            for x, y in zip(a.members, b.members))))
        self.plan = plan
        self.EC = EC
        self.resize(720, 760)
        v = self.body
        form = QGridLayout()
        form.setHorizontalSpacing(12)
        form.setVerticalSpacing(8)
        r = 0
        form.addWidget(self._key("Protocol"), r, 0)
        prow = QHBoxLayout()
        prow.setSpacing(18)
        self.proto_group = QButtonGroup(self)
        for i, (key, text, tip) in enumerate((
                ("lacp", "LACP", "IEEE 802.3ad: modes active (starts) / passive (answers)"),
                ("pagp", "PAgP", "Cisco PAgP: modes desirable (starts) / auto (answers)"),
                ("on", "Static on", "no negotiation at all: both sides 'mode on'"))):
            rb = QRadioButton(text)
            rb.setToolTip(tip)
            rb.setProperty("proto", key)
            self.proto_group.addButton(rb, i)
            prow.addWidget(rb)
            if key == plan.protocol:
                rb.setChecked(True)
        prow.addStretch(1)
        form.addLayout(prow, r, 1)
        r += 1
        self.mode_a, self.mode_b = QComboBox(), QComboBox()
        for lbl, cb in ((a.label, self.mode_a), (b.label, self.mode_b)):
            form.addWidget(self._key(f"{lbl} mode"), r, 0)
            cb.setMinimumWidth(160)
            row = QHBoxLayout()
            row.addWidget(cb)
            row.addStretch(1)
            form.addLayout(row, r, 1)
            r += 1
        self.check = QLabel()
        self.check.setWordWrap(True)
        self.check.setTextFormat(Qt.RichText)
        form.addWidget(self.check, r, 1)
        r += 1
        form.addWidget(self._key("Port-channel"), r, 0)
        self.po = QSpinBox()
        self.po.setRange(1, EC.MAX_PO)
        self.po.setValue(plan.po)
        self.po.setPrefix("Po")
        self.po.setMinimumWidth(96)
        self.po_note = QLabel()
        self.po_note.setObjectName("muted")
        self.po_note.setFont(T.type("small"))
        row = QHBoxLayout()
        row.setSpacing(10)
        row.addWidget(self.po)
        row.addWidget(self.po_note, 1)
        form.addLayout(row, r, 1)
        r += 1
        form.addWidget(self._key("L2 mode"), r, 0)
        self.cb_l2 = QCheckBox(f"Copy the members' switchport config onto the Port-channel: {EC.l2_summary(a.l2)}")
        self.cb_l2.setChecked(True)
        self.cb_l2.setToolTip("from " + " and ".join(x for x in plan.l2_source if x))
        form.addWidget(self.cb_l2, r, 1)
        r += 1
        if plan.l2_conflict:
            w = QLabel(f"<span style='color:{T.status_text('warn').name()}'>⚠ {_html.escape(plan.l2_conflict)}</span>")
            w.setWordWrap(True)
            form.addWidget(w, r, 1)
            r += 1
        form.addWidget(self._key("Load-balance"), r, 0)
        self.cb_lb = QCheckBox("Set")
        self.lb = QComboBox()
        self.lb.addItems(list(EC.LOAD_BALANCE))
        self.lb.setEnabled(False)
        self.cb_lb.toggled.connect(self.lb.setEnabled)
        row = QHBoxLayout()
        row.setSpacing(10)
        row.addWidget(self.cb_lb)
        row.addWidget(self.lb)
        lbn = QLabel("port-channel load-balance (global, both switches)")
        lbn.setObjectName("muted")
        lbn.setFont(T.type("small"))
        row.addWidget(lbn, 1)
        form.addLayout(row, r, 1)
        form.setColumnStretch(1, 1)
        v.addLayout(form)
        v.addSpacing(8)
        booted = a.booted and b.booted
        self.cb_live = QCheckBox("Configure both switches now through their consoles (enable secret 'cisco', "
                                 "then write memory)")
        self.cb_live.setChecked(booted)
        self.cb_live.setEnabled(booted)
        v.addWidget(self.cb_live)
        opened = [s.label for s in (a, b) if s.console_open]
        if opened:
            note = QLabel(f"<span style='color:{T.status_text('warn').name()}'>{_html.escape(', '.join(opened))}: "
                          "console open on this host. LabDesk asks before typing on it (you will see the lines); "
                          "it stops without typing if you are in configuration mode or at a --More-- prompt.</span>")
            note.setWordWrap(True)
            v.addWidget(note)
        self.cb_day0 = QCheckBox("Store it in the CML day-0 configs of both switches (written at the next wipe "
                                 "while they run)")
        self.cb_day0.setChecked(True)
        v.addWidget(self.cb_day0)
        cap = QLabel("Commands (configure terminal)")
        cap.setFont(T.type("caption", upper=True))
        cap.setObjectName("muted")
        v.addSpacing(8)
        v.addWidget(cap)
        self.tabs = QTabWidget()
        self.prev_a, self.prev_b = QPlainTextEdit(), QPlainTextEdit()
        for lbl, pe in ((a.label, self.prev_a), (b.label, self.prev_b)):
            pe.setReadOnly(True)
            pe.setFont(T.mono())
            self.tabs.addTab(pe, lbl)
        v.addWidget(self.tabs, 1)
        self.cancel_button()
        self.apply_btn = self.add_button("Apply", QDialogButtonBox.AcceptRole, "primary", default=True)
        self.proto_group.buttonToggled.connect(lambda *_: self._proto_changed())
        for w in (self.mode_a, self.mode_b, self.lb):
            w.currentIndexChanged.connect(lambda *_: self._refresh())
        self.po.valueChanged.connect(lambda *_: self._refresh())
        self.cb_lb.toggled.connect(lambda *_: self._refresh())
        self.cb_l2.toggled.connect(lambda *_: self._refresh())
        self._proto_changed()

    @staticmethod
    def _key(text: str) -> QLabel:
        lab = QLabel(text)
        lab.setObjectName("muted")
        lab.setFont(T.type("small"))
        return lab

    def protocol(self) -> str:
        b = self.proto_group.checkedButton()
        return b.property("proto") if b is not None else "lacp"

    def _proto_changed(self):
        EC = self.EC
        proto = self.protocol()
        da, db = EC.DEFAULT_MODES[proto]
        for cb, d in ((self.mode_a, da), (self.mode_b, db)):
            cb.blockSignals(True)
            cb.clear()
            for m in EC.MODES[proto]:
                cb.addItem(EC.MODE_WORDS[m], m)
            cb.setCurrentIndex(EC.MODES[proto].index(d))
            cb.setEnabled(len(EC.MODES[proto]) > 1)
            cb.blockSignals(False)
        self._refresh()

    def _sync_plan(self):
        p = self.plan
        p.protocol = self.protocol()
        p.a.mode = self.mode_a.currentData() or self.EC.DEFAULT_MODES[p.protocol][0]
        p.b.mode = self.mode_b.currentData() or self.EC.DEFAULT_MODES[p.protocol][1]
        p.po = self.po.value()
        p.load_balance = self.lb.currentText() if self.cb_lb.isChecked() else None
        if not self.cb_l2.isChecked():
            p.a.l2, p.b.l2 = [], []
        elif not p.a.l2 and getattr(self, "_l2_saved", None):
            p.a.l2, p.b.l2 = self._l2_saved
        if self.cb_l2.isChecked():
            self._l2_saved = (list(p.a.l2), list(p.b.l2))
        p.check = self.EC.check_modes(p.a.mode, p.b.mode)

    def _refresh(self):
        self._sync_plan()
        p = self.plan
        c = p.check
        if c.forms:
            self.check.setText(f"<span style='color:{T.status_text('up').name()}'>✓ forms: {_html.escape(p.a.label)} "
                               f"{p.a.mode} ↔ {_html.escape(p.b.label)} {p.b.mode}</span>")
        else:
            self.check.setText(f"<span style='color:{T.status_text('warn').name()}'><b>⚠ {_html.escape(c.reason)}</b>"
                               f"<br>{_html.escape(c.explain)}</span>")
        taken = p.po in set(p.used)
        self.po_note.setText(f"Po{p.po} is already used on a switch" if taken else
                             (f"next free (used: {', '.join(f'Po{x}' for x in p.used)})" if p.used else "next free"))
        errs = self.EC.validate_plan(p) + ([f"Port-channel {p.po} is already in use"] if taken else [])
        self.apply_btn.setEnabled(not errs)
        self.apply_btn.setToolTip("; ".join(errs))
        self.prev_a.setPlainText("\n".join(p.commands("a")) + "\nend\nwrite memory\n")
        self.prev_b.setPlainText("\n".join(p.commands("b")) + "\nend\nwrite memory\n")

    def choices(self) -> dict:
        self._sync_plan()
        return {"live": self.cb_live.isChecked(), "day0": self.cb_day0.isChecked()}
