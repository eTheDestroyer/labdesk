"""Designed empty states (L6) as real, keyboard-accessible widgets on the canvas viewport.

:class:`EmptyStateCard`: the centred 420 px card shown while no lab is open - 64 px app icon, 'No lab open'
(display role), one N11 line, primary *Open lab…* (Ctrl+O), secondary *From template…*, text link
*New empty lab* (Ctrl+N) and up to five *Recent labs* (name, StatusPill, node count).  The recent list is
filled asynchronously from the same lab-list call the Open dialog uses; it shows a skeleton while loading
and disappears quietly when CML cannot be reached.  ``set_offline(True)`` turns it into the *CML is offline*
card with *Start CML VM* as the primary action.

The zero-node target ('Drag a device here') is painted by the view (it is not interactive).
"""
from __future__ import annotations

from PySide6.QtCore import QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QIcon, QPainter, QPen
from PySide6.QtWidgets import (QGraphicsDropShadowEffect, QHBoxLayout, QLabel, QPushButton, QSizePolicy, QVBoxLayout,
                               QWidget)

from .. import config, plat
from . import theme as T
from .widgets import StatusPill

WIDTH = 420


class _Skeleton(QWidget):
    """Three grey bars while the recent-lab list loads."""

    def sizeHint(self):
        return QSize(WIDTH - 56, 3 * 30)

    def paintEvent(self, ev):
        tk = T.tokens()
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        p.setBrush(tk.N(3) if not tk.dark else tk.N(4))
        for i, w in enumerate((0.62, 0.48, 0.55)):
            y = i * 30 + 8
            p.drawRoundedRect(QRectF(8, y, (self.width() - 16) * w, 12), 6, 6)
            p.drawRoundedRect(QRectF(self.width() - 92, y, 56, 12), 6, 6)
        p.end()


class _RecentRow(QPushButton):
    """One recent lab: name (label role) · StatusPill · '5 nodes'.  The whole row is the button."""

    def __init__(self, lab: dict, parent=None):
        super().__init__(parent)
        self.lab_id = lab.get("id")
        self.setObjectName("recentrow")
        self.setCursor(Qt.PointingHandCursor)
        self.setFlat(True)
        self.setMinimumHeight(32)
        title = lab.get("lab_title") or lab.get("title") or self.lab_id or "?"
        self.setToolTip(f"Open “{title}”")
        self.setAccessibleName(f"Open lab {title}")
        h = QHBoxLayout(self)
        h.setContentsMargins(10, 0, 10, 0)
        h.setSpacing(8)
        name = QLabel(title)
        name.setFont(T.type("body", weight=600))
        name.setAttribute(Qt.WA_TransparentForMouseEvents)
        h.addWidget(name, 1)
        state = str(lab.get("state", ""))
        pill = StatusPill(T.state_label(state), "up" if state.upper() in ("STARTED", "BOOTED") else "neutral",
                          "up" if state.upper() in ("STARTED", "BOOTED") else "stopped", height=20)
        h.addWidget(pill)
        n = lab.get("node_count")
        cnt = QLabel(f"{n} node{'s' if n != 1 else ''}" if n is not None else "")
        cnt.setObjectName("muted")
        cnt.setFont(T.tabular("small"))
        cnt.setMinimumWidth(56)
        cnt.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        cnt.setAttribute(Qt.WA_TransparentForMouseEvents)
        h.addWidget(cnt)

    def sizeHint(self):
        return QSize(WIDTH - 56, 32)


class EmptyStateCard(QWidget):
    open_clicked = Signal()
    template_clicked = Signal()
    new_clicked = Signal()
    start_cml_clicked = Signal()
    lab_chosen = Signal(str)

    def __init__(self, viewport: QWidget):
        super().__init__(viewport)
        self.setObjectName("emptycard")
        self.setFixedWidth(WIDTH)
        self.offline = False
        v = QVBoxLayout(self)
        v.setContentsMargins(28, 28, 28, 24)
        v.setSpacing(0)
        self.icon = QLabel()
        self.icon.setAlignment(Qt.AlignHCenter)
        self.icon.setPixmap(QIcon(str(config.ICON_PATH)).pixmap(64, 64))
        v.addWidget(self.icon)
        v.addSpacing(14)
        self.title = QLabel("No lab open")
        self.title.setAlignment(Qt.AlignHCenter)
        v.addWidget(self.title)
        v.addSpacing(6)
        self.sub = QLabel("Open a lab from the CML controller, start one from a template, or begin with an "
                          "empty canvas.")
        self.sub.setObjectName("muted")
        self.sub.setWordWrap(True)
        self.sub.setAlignment(Qt.AlignHCenter | Qt.AlignTop)
        self.sub.setFixedWidth(WIDTH - 56)
        v.addWidget(self.sub)
        v.addSpacing(20)
        row = QHBoxLayout()
        row.setSpacing(8)
        row.addStretch(1)
        self.primary = QPushButton("Open lab…")
        self.primary.setProperty("role", "primary")
        self.primary.setToolTip("Open a lab on the CML controller (Ctrl+O)")
        self.primary.setMinimumWidth(120)
        self.primary.clicked.connect(self._primary)
        self.secondary = QPushButton("From template…")
        self.secondary.setToolTip("Import a template as a new lab")
        self.secondary.setMinimumWidth(120)
        self.secondary.clicked.connect(self.template_clicked)
        row.addWidget(self.primary)
        row.addWidget(self.secondary)
        row.addStretch(1)
        v.addLayout(row)
        v.addSpacing(6)
        self.link = QPushButton("New empty lab")
        self.link.setProperty("role", "link")
        self.link.setToolTip("Create an empty lab on the controller (Ctrl+N)")
        self.link.setCursor(Qt.PointingHandCursor)
        self.link.clicked.connect(self.new_clicked)
        v.addWidget(self.link, 0, Qt.AlignHCenter)
        # recent labs
        self.recent = QWidget()
        rv = QVBoxLayout(self.recent)
        rv.setContentsMargins(0, 18, 0, 0)
        rv.setSpacing(2)
        self.recent_head = QLabel("Recent labs")
        self.recent_head.setObjectName("muted")
        rv.addWidget(self.recent_head)
        rv.addSpacing(4)
        self.skeleton = _Skeleton()
        rv.addWidget(self.skeleton)
        self.rows_box = QVBoxLayout()
        self.rows_box.setSpacing(0)
        rv.addLayout(self.rows_box)
        v.addWidget(self.recent)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Preferred)
        sh = QGraphicsDropShadowEffect(self)
        sh.setBlurRadius(36)
        sh.setOffset(0, 8)
        self._shadow = sh
        self.setGraphicsEffect(sh)
        self.theme_changed()
        self.hide()

    # ------------------------------------------------------------------ state
    def theme_changed(self):
        tk = T.tokens()
        self.title.setFont(T.type("display"))
        self.recent_head.setFont(T.type("caption", upper=True))
        self._shadow.setColor(T.alpha("#101828", 0.10))
        self._shadow.setEnabled(not tk.dark)
        self.update()

    def _primary(self):
        (self.start_cml_clicked if self.offline else self.open_clicked).emit()

    def set_offline(self, offline: bool, host: str = "") -> None:
        self.offline = offline
        if offline:
            self.title.setText("CML is offline")
            self.sub.setText(f"The CML controller{(' at ' + host) if host else ''} does not answer. Start its VM; "
                             "LabDesk reconnects once the API is up.")
            self.primary.setText("Start CML VM")
            self.primary.setToolTip("vmrun start (VMware) or start it yourself, then wait for the API"
                                    if plat.IS_WINDOWS else "virsh start cml, then wait for the API")
        else:
            self.title.setText("No lab open")
            self.sub.setText("Open a lab from the CML controller, start one from a template, or begin with an "
                             "empty canvas.")
            self.primary.setText("Open lab…")
            self.primary.setToolTip("Open a lab on the CML controller (Ctrl+O)")
        self.secondary.setVisible(not offline)
        self.link.setVisible(not offline)
        if offline:
            self.recent.setVisible(False)
        self.reposition()

    def set_loading(self) -> None:
        self._clear_rows()
        self.skeleton.setVisible(True)
        self.recent.setVisible(not self.offline)
        self.reposition()

    def set_recent(self, labs: list[dict] | None, current: str | None = None) -> None:
        """``None`` = CML unreachable: hide the section quietly."""
        self._clear_rows()
        self.skeleton.setVisible(False)
        labs = [lab for lab in (labs or []) if lab.get("id") != current]
        if not labs or self.offline:
            self.recent.setVisible(False)
            self.reposition()
            return
        order = sorted(labs, key=lambda lab: str(lab.get("modified") or lab.get("created") or ""), reverse=True)
        for lab in order[:5]:
            r = _RecentRow(lab)
            r.clicked.connect(lambda _=False, i=r.lab_id: self.lab_chosen.emit(i))
            self.rows_box.addWidget(r)
        self.recent.setVisible(True)
        self.reposition()
        QTimer.singleShot(0, self.reposition)

    def _clear_rows(self):
        while self.rows_box.count():
            it = self.rows_box.takeAt(0)
            if it.widget() is not None:
                it.widget().deleteLater()

    # ------------------------------------------------------------------ geometry / paint
    def showEvent(self, ev):
        super().showEvent(ev)
        self.reposition()

    def reposition(self):
        par = self.parentWidget()
        if par is None:
            return
        self.recent.layout().invalidate()
        self.recent.layout().activate()
        self.recent.updateGeometry()
        lay = self.layout()
        lay.invalidate()
        lay.activate()
        h = lay.heightForWidth(WIDTH) if lay.hasHeightForWidth() else lay.sizeHint().height()
        self.resize(WIDTH, h)
        self.move(max(16, (par.width() - WIDTH) // 2), max(16, (par.height() - h) // 2 - 20))

    def paintEvent(self, ev):
        tk = T.tokens()
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        p.setPen(QPen(tk.N(7) if tk.dark else tk.N(6), 1))
        p.setBrush(tk.N(1) if not tk.dark else tk.N(3))
        p.drawRoundedRect(r, T.RADIUS_LG, T.RADIUS_LG)
        p.end()
