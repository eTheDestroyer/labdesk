"""Event log (L5): a filterable QTableView over a ring-buffer model.

* :class:`LogModel` keeps at most ``CAP`` rows; appends are coalesced by a 100 ms timer and inserted / trimmed
  with batched ``beginInsertRows`` / ``beginRemoveRows`` (no full resets, no per-line repaint storms).
* :class:`LogFilter` (QSortFilterProxyModel): level filter (All / Warnings / Errors) and a search text with
  ``node:R1`` syntax.
* :class:`EventLog` is the dock widget: a 28 px header (segmented level filter + search, Ctrl+F) and the
  table (Time mono N11 · level gutter + S2 glyph · Source chip · Message, elided with the full text in the
  tooltip).  Auto-scrolls only when the view already sits at the bottom.  ``unread_changed(int)`` counts
  errors that arrived while the log was not being looked at.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass

from PySide6.QtCore import (QAbstractTableModel, QModelIndex, QPointF, QRectF, QSize, QSortFilterProxyModel, Qt,
                            QTimer, Signal)
from PySide6.QtGui import QAction, QColor, QGuiApplication, QKeySequence, QPainter, QPen
from PySide6.QtWidgets import (QAbstractItemView, QButtonGroup, QFileDialog, QHBoxLayout, QHeaderView, QLineEdit,
                               QMenu, QStyle, QStyledItemDelegate, QStyleOptionViewItem, QTableView, QToolButton,
                               QVBoxLayout, QWidget)

from . import theme as T

CAP = 2000
COLS = ("Time", "", "Source", "Message")
LEVEL_GLYPH = {"ok": "up", "warn": "warn", "error": "down", "state": "info", "info": None}
LEVEL_STATE = {"ok": "up", "warn": "warn", "error": "down"}
LEVEL_WORD = {"ok": "OK", "warn": "Warning", "error": "Error", "state": "State", "info": "Info"}
_NODE_RE = re.compile(r"^([A-Za-z][\w.-]{0,24})(?::| →| ->| reaches| cannot|'s )")


@dataclass
class Entry:
    ts: str
    level: str
    msg: str
    tag: str | None = None
    hue: str | None = None
    source: str = ""

    def text(self) -> str:
        tag = f"[{self.tag}] " if self.tag else ""
        src = f"{self.source}  " if self.source and not self.msg.startswith(self.source) else ""
        return f"{self.ts}  {LEVEL_WORD.get(self.level, self.level):7s}  {src}{tag}{self.msg}"


class LogModel(QAbstractTableModel):
    def __init__(self, parent=None, cap: int = CAP):
        super().__init__(parent)
        self.rows: list[Entry] = []
        self.cap = cap
        self._pending: list[Entry] = []
        self.node_labels: set[str] = set()
        self._t = QTimer(self)
        self._t.setSingleShot(True)
        self._t.setInterval(100)
        self._t.timeout.connect(self.flush)

    # ------------------------------------------------------------------ feed
    def add(self, e: Entry) -> None:
        if not e.source:
            m = _NODE_RE.match(e.msg)
            if m and (m.group(1) in self.node_labels):
                e.source = m.group(1)
            else:
                first = e.msg.split(" ", 1)[0].rstrip(":")
                if first in self.node_labels:
                    e.source = first
        self._pending.append(e)
        if not self._t.isActive():
            self._t.start()

    def flush(self) -> None:
        if not self._pending:
            return
        new, self._pending = self._pending[-self.cap:], []
        drop = len(self.rows) + len(new) - self.cap
        if drop > 0:
            drop = min(drop, len(self.rows))
            self.beginRemoveRows(QModelIndex(), 0, drop - 1)
            del self.rows[:drop]
            self.endRemoveRows()
        n = len(self.rows)
        self.beginInsertRows(QModelIndex(), n, n + len(new) - 1)
        self.rows.extend(new)
        self.endInsertRows()

    def clear(self) -> None:
        self._pending.clear()
        self.beginResetModel()
        self.rows.clear()
        self.endResetModel()

    # ------------------------------------------------------------------ model
    def rowCount(self, parent=QModelIndex()):  # noqa: N802
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent=QModelIndex()):  # noqa: N802
        return len(COLS)

    def headerData(self, section, orientation, role=Qt.DisplayRole):  # noqa: N802
        if orientation == Qt.Horizontal and role == Qt.DisplayRole:
            return COLS[section]
        return None

    def data(self, idx, role=Qt.DisplayRole):
        if not idx.isValid():
            return None
        e = self.rows[idx.row()]
        c = idx.column()
        if role == Qt.UserRole:
            return e
        if role == Qt.DisplayRole:
            return (e.ts, LEVEL_WORD.get(e.level, ""), e.source, e.msg)[c]
        if role == Qt.ToolTipRole:
            if c == 3:
                return (f"[{e.tag}] " if e.tag else "") + e.msg
            if c == 1:
                return LEVEL_WORD.get(e.level, "")
            if c == 2 and e.source:
                return f"{e.source} - right-click to show only this node"
        return None


class LogFilter(QSortFilterProxyModel):
    LEVELS = {"all": None, "warn": {"warn", "error"}, "error": {"error"}}

    def __init__(self, parent=None):
        super().__init__(parent)
        self.level = "all"
        self.words: list[str] = []
        self.node: str | None = None

    def _refilter(self, change) -> None:
        if hasattr(self, "beginFilterChange"):          # Qt >= 6.9 (invalidateFilter is deprecated)
            self.beginFilterChange()
            change()
            self.endFilterChange(QSortFilterProxyModel.Direction.Rows)
        else:  # pragma: no cover
            change()
            self.invalidateFilter()

    def set_level(self, level: str) -> None:
        self._refilter(lambda: setattr(self, "level", level))

    def set_query(self, q: str) -> None:
        node, words = None, []
        for tok in q.split():
            if tok.lower().startswith("node:") and len(tok) > 5:
                node = tok[5:]
            else:
                words.append(tok.lower())
        self._refilter(lambda: (setattr(self, "node", node), setattr(self, "words", words)))

    def filterAcceptsRow(self, row, parent):  # noqa: N802
        e = self.sourceModel().rows[row]
        lv = self.LEVELS.get(self.level)
        if lv is not None and e.level not in lv:
            return False
        if self.node:
            nl = self.node.lower()
            if e.source.lower() != nl and not re.search(rf"\b{re.escape(nl)}\b", e.msg.lower()):
                return False
        if self.words:
            hay = f"{e.ts} {e.source} {e.tag or ''} {e.msg}".lower()
            return all(w in hay for w in self.words)
        return True


class _Delegate(QStyledItemDelegate):
    def paint(self, p: QPainter, opt: QStyleOptionViewItem, idx):
        e: Entry = idx.data(Qt.UserRole)
        if e is None:
            return super().paint(p, opt, idx)
        tk = T.tokens()
        r = QRectF(opt.rect)
        p.save()
        p.setRenderHint(QPainter.Antialiasing)
        if opt.state & QStyle.State_Selected:
            p.fillRect(opt.rect, tk.accent_subtle)
        elif opt.state & QStyle.State_MouseOver:
            p.fillRect(opt.rect, T.alpha(tk.N(12), 0.04))
        c = idx.column()
        state = LEVEL_STATE.get(e.level)
        if c == 0:
            if state:
                p.fillRect(QRectF(r.left(), r.top() + 2, 3, r.height() - 4), T.status_fill(state, tk))
            p.setFont(T.mono(1.0, "small"))
            p.setPen(tk.N(11))
            p.drawText(r.adjusted(10, 0, -4, 0), Qt.AlignVCenter | Qt.AlignLeft, e.ts)
        elif c == 1:
            g = LEVEL_GLYPH.get(e.level)
            if g:
                col = T.status_fill(state, tk) if state else tk.accent_ring
                T.draw_status_glyph(p, QPointF(r.center().x(), r.center().y()), g, 9.0, col, tk)
        elif c == 2:
            if e.source:
                f = T.type("caption")
                fm = T.fm(f)
                w = min(r.width() - 8, fm.horizontalAdvance(e.source) + 12)
                chip = QRectF(r.left() + 4, r.center().y() - 8, w, 16)
                p.setPen(QPen(tk.N(6) if not tk.dark else tk.N(7), 1))
                p.setBrush(tk.N(3) if not tk.dark else tk.N(4))
                p.drawRoundedRect(chip, 8, 8)
                p.setFont(f)
                p.setPen(tk.N(12))
                p.drawText(chip.adjusted(6, 0, -6, 0), Qt.AlignVCenter | Qt.AlignLeft,
                           fm.elidedText(e.source, Qt.ElideRight, chip.width() - 12))
        else:
            x = r.left() + 4
            if e.tag:
                f = T.type("caption")
                fm = T.fm(f)
                hue = T.text_for(e.hue, tk) if e.hue else tk.N(11)
                w = fm.horizontalAdvance(e.tag) + 10
                chip = QRectF(x, r.center().y() - 8, w, 16)
                p.setPen(Qt.NoPen)
                p.setBrush(T.alpha(QColor(e.hue) if e.hue else tk.N(9), 0.14))
                p.drawRoundedRect(chip, 4, 4)
                p.setFont(f)
                p.setPen(hue)
                p.drawText(chip, Qt.AlignCenter, e.tag)
                x += w + 8
            f = T.type("small")
            p.setFont(f)
            col = {"error": T.status_text("down", tk), "warn": T.status_text("warn", tk),
                   "ok": T.status_text("up", tk)}.get(e.level, tk.N(12))
            p.setPen(col)
            p.drawText(QRectF(x, r.top(), r.right() - x - 6, r.height()), Qt.AlignVCenter | Qt.AlignLeft,
                       T.fm(f).elidedText(e.msg, Qt.ElideRight, r.right() - x - 6))
        p.restore()

    def sizeHint(self, opt, idx):
        return QSize(40, 22)


class EventLog(QWidget):
    """The Event log dock contents."""

    unread_changed = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("eventlog")
        self.model = LogModel(self)
        self.proxy = LogFilter(self)
        self.proxy.setSourceModel(self.model)
        self.unread = 0
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)
        head = QWidget()
        head.setObjectName("loghead")
        head.setFixedHeight(34)
        h = QHBoxLayout(head)
        h.setContentsMargins(8, 4, 8, 4)
        h.setSpacing(0)
        self.level_group = QButtonGroup(self)
        self.level_btns = {}
        for i, (key, text) in enumerate((("all", "All"), ("warn", "Warnings"), ("error", "Errors"))):
            b = QToolButton()
            b.setText(text)
            b.setCheckable(True)
            b.setObjectName("segment")
            b.setProperty("pos", "first" if i == 0 else ("last" if i == 2 else "mid"))
            b.setChecked(key == "all")
            b.setFocusPolicy(Qt.TabFocus)
            b.setFixedHeight(24)
            self.level_group.addButton(b)
            b.clicked.connect(lambda _=False, k=key: self.proxy.set_level(k))
            self.level_btns[key] = b
            h.addWidget(b)
        h.addStretch(1)
        self.search = QLineEdit()
        self.search.setObjectName("logsearch")
        self.search.setPlaceholderText("Search the log  (node:R1)")
        self.search.setClearButtonEnabled(True)
        self.search.setFixedWidth(260)
        self.search.setFixedHeight(24)
        self.search.textChanged.connect(self._query)
        h.addWidget(self.search)
        v.addWidget(head)
        t = self.table = QTableView()
        t.setObjectName("logtable")
        t.setModel(self.proxy)
        t.setItemDelegate(_Delegate(t))
        t.setShowGrid(False)
        t.setWordWrap(False)
        t.setMouseTracking(True)
        t.verticalHeader().setVisible(False)
        t.verticalHeader().setDefaultSectionSize(22)
        t.verticalHeader().setSectionResizeMode(QHeaderView.Fixed)
        t.horizontalHeader().setVisible(False)
        hh = t.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.Fixed)
        hh.setSectionResizeMode(1, QHeaderView.Fixed)
        hh.setSectionResizeMode(2, QHeaderView.Fixed)
        hh.setSectionResizeMode(3, QHeaderView.Stretch)
        self._col_widths()
        t.setSelectionBehavior(QAbstractItemView.SelectRows)
        t.setSelectionMode(QAbstractItemView.ExtendedSelection)
        t.setEditTriggers(QAbstractItemView.NoEditTriggers)
        t.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        t.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        t.setContextMenuPolicy(Qt.CustomContextMenu)
        t.customContextMenuRequested.connect(self._menu)
        v.addWidget(t, 1)
        self._at_bottom = True
        t.verticalScrollBar().valueChanged.connect(self._scrolled)
        self.proxy.rowsInserted.connect(self._inserted)
        self.a_copy = QAction("Copy", self)
        self.a_copy.setShortcut(QKeySequence.Copy)
        self.a_copy.setShortcutContext(Qt.WidgetWithChildrenShortcut)
        self.a_copy.triggered.connect(self.copy_selected)
        t.addAction(self.a_copy)
        self.a_find = QAction("Search the log", self)
        self.a_find.setShortcut(QKeySequence.Find)
        self.a_find.setShortcutContext(Qt.WidgetWithChildrenShortcut)
        self.a_find.triggered.connect(lambda: (self.search.setFocus(), self.search.selectAll()))
        self.addAction(self.a_find)

    def _col_widths(self):
        fm = T.fm(T.mono(1.0, "small"))
        self.table.setColumnWidth(0, int(fm.horizontalAdvance("00:00:00")) + 18)
        self.table.setColumnWidth(1, 22)
        self.table.setColumnWidth(2, 84)

    def theme_changed(self):
        self._col_widths()
        self.table.viewport().update()

    # ------------------------------------------------------------------ feed
    def append(self, msg: str, level: str = "info", tag: str | None = None, hue: str | None = None,
               ts: str | None = None) -> None:
        self.model.add(Entry(ts or time.strftime("%H:%M:%S"), level, str(msg), tag, hue))
        if level == "error" and not self._being_viewed():
            self.unread += 1
            self.unread_changed.emit(self.unread)

    def set_node_labels(self, labels) -> None:
        self.model.node_labels = set(labels)

    def _being_viewed(self) -> bool:
        w = self.window()
        return self.isVisible() and (self.table.hasFocus() or self.search.hasFocus() or self.underMouse()) \
            and w is not None and w.isActiveWindow()

    def mark_read(self) -> None:
        if self.unread:
            self.unread = 0
            self.unread_changed.emit(0)

    def enterEvent(self, ev):
        super().enterEvent(ev)
        self.mark_read()

    def focusInEvent(self, ev):
        super().focusInEvent(ev)
        self.mark_read()

    # ------------------------------------------------------------------ scrolling
    def _scrolled(self, v):
        sb = self.table.verticalScrollBar()
        self._at_bottom = v >= sb.maximum() - 2

    def _inserted(self, *_):
        if self._at_bottom:
            QTimer.singleShot(0, self.table.scrollToBottom)

    def _query(self, text: str):
        self.proxy.set_query(text)

    # ------------------------------------------------------------------ text / menu
    def toPlainText(self) -> str:  # noqa: N802  (QTextEdit compat for older callers / tests)
        self.model.flush()
        return "\n".join(e.text() for e in self.model.rows)

    def _rows_text(self, proxy_rows) -> str:
        out = []
        for r in proxy_rows:
            e = self.proxy.index(r, 0).data(Qt.UserRole)
            if e is not None:
                out.append(e.text())
        return "\n".join(out)

    def copy_selected(self):
        rows = sorted({i.row() for i in self.table.selectionModel().selectedRows()})
        if rows:
            QGuiApplication.clipboard().setText(self._rows_text(rows))

    def copy_visible(self):
        QGuiApplication.clipboard().setText(self._rows_text(range(self.proxy.rowCount())))

    def save_as(self):
        path, _ = QFileDialog.getSaveFileName(self, "Save log as", f"labdesk-log-{time.strftime('%Y%m%d-%H%M')}.txt",
                                              "Text (*.txt *.log)")
        if path:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(self.toPlainText() + "\n")

    def _menu(self, pos):
        m = QMenu(self)
        idx = self.table.indexAt(pos)
        e = idx.data(Qt.UserRole) if idx.isValid() else None
        a = m.addAction("Copy")
        a.setShortcut(QKeySequence.Copy)
        a.setEnabled(bool(self.table.selectionModel().selectedRows()))
        a.triggered.connect(self.copy_selected)
        m.addAction("Copy visible rows").triggered.connect(self.copy_visible)
        m.addAction("Save log as…").triggered.connect(self.save_as)
        if e is not None and e.source:
            m.addSeparator()
            m.addAction(f"Show only {e.source}").triggered.connect(
                lambda: self.search.setText(f"node:{e.source}"))
        if self.search.text():
            m.addAction("Show everything").triggered.connect(self.search.clear)
        m.addSeparator()
        m.addAction("Clear").triggered.connect(self.model.clear)
        m.exec(self.table.viewport().mapToGlobal(pos))
