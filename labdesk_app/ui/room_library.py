"""Room library: pick a ready room (a LabDesk room file) from a gallery with thumbnails, then work on a copy.

A *room file* is ``Site.to_dict()``: version-2 files say ``"format": "labdesk-room"`` and carry ``meta``
(title, Hebrew title, kind, city, era, the source plan with its URL and licence, assumptions).  The rooms
shipped with LabDesk live in ``labdesk_app/rooms``; rooms the user saves go to ``<config>/rooms`` and are
listed too.

* ``read_entry`` / ``scan``  - read one file / every ``*.json`` of some folders.  A broken file becomes an entry
  with ``error`` set (listed greyed), never an exception.
* ``load_into``              - replace a Site's contents in place (the view, its field engine and the GPU
  renderer keep their reference to the same object).
* ``write_room``             - save a site as a room file (atomic; access points mirrored from a lab become
  ordinary ones).
* ``Thumbnailer``            - draws a room with the real ``SiteView`` (GPU when usable), cached in memory and
  on disk by the file's hash, the model version, the theme and the renderer.
* ``RoomLibraryDialog``      - the gallery.

Origin: the Room detail council's library spike (wifi-council/round3/detail/builder_b), with its devils' fixes.
"""
from __future__ import annotations

import copy
import dataclasses
import hashlib
import json
import math
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from PySide6.QtCore import QRectF, QSize, Qt, QTimer
from PySide6.QtGui import QImage, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import (QAbstractItemView, QComboBox, QDialogButtonBox, QFileDialog, QFormLayout,
                               QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QPushButton,
                               QScrollArea, QSizePolicy, QStyle, QStyledItemDelegate, QVBoxLayout, QWidget)

from labdesk_app import wifi_site as ws
from labdesk_app.ui import theme as T
from labdesk_app.ui.dialogs import LabDeskDialog

THUMB_W, THUMB_H = 480, 300          # rendered size; the cards show it at half size (sharp on HiDPI)
THUMB_REV = "t2"                     # bump when the thumbnail look changes (invalidates the disk cache)
ROLE = Qt.UserRole + 1
KIND_TITLES = {"home": "Home", "office": "Office", "workshop": "Workshop", "public": "Public building"}


# ----------------------------------------------------------------------------------------- files
@dataclass
class RoomEntry:
    path: Path
    site: ws.Site | None = None
    error: str = ""                  # why the file cannot be used ('' = it can)
    newer: bool = False              # written by a newer LabDesk: opens, may lose details
    digest: str = ""

    @property
    def meta(self) -> dict:
        return self.site.meta if self.site is not None else {}

    def _s(self, key: str) -> str:
        v = self.meta.get(key)
        return v.strip() if isinstance(v, str) else ""

    @property
    def title(self) -> str:
        return self._s("title") or self.path.stem.replace("_", " ").replace("-", " ").capitalize()

    @property
    def title_he(self) -> str:
        return self._s("title_he")

    @property
    def kind(self) -> str:
        k = self._s("kind")
        return KIND_TITLES.get(k.lower(), k.capitalize()) if k else "Not labelled"

    @property
    def city(self) -> str:
        return self._s("city")

    @property
    def city_he(self) -> str:
        return self._s("city_he")

    @property
    def notes(self) -> str:
        return self._s("notes")

    @property
    def source(self) -> dict:
        s = self.meta.get("source")
        if not isinstance(s, dict):
            return {}
        out = {k: s[k].strip() for k in ("title", "url", "license") if isinstance(s.get(k), str)}
        if not re.match(r"^https?://", out.get("url", ""), re.I):       # only web links are offered as links
            out.pop("url", None)
        return out

    def summary(self) -> str:
        """'12 x 11 m · 2.7 m high · 6 rooms · 9 walls · 5 objects · 1 AP'."""
        s = self.site
        if s is None:
            return ""
        n_ap = len(s.aps)
        return (f"{s.width:g} x {s.depth:g} m · {s.height:g} m high · {len(s.rooms())} rooms · "
                f"{len(s.partitions)} walls · {len(s.obstacles)} objects · {n_ap} AP" + ("" if n_ap == 1 else "s"))

    def matches(self, text: str) -> bool:
        words = text.strip().lower().replace('״', '"').split()
        hay = " ".join((self.title, self.title_he, self.kind, self.city, self.city_he, self.path.name)).lower()
        return all(w in hay.replace('״', '"') for w in words)


def read_entry(path) -> RoomEntry:
    path = Path(path)
    e = RoomEntry(path)
    try:
        data = path.read_bytes()
        e.digest = hashlib.sha1(data).hexdigest()
        raw = json.loads(data.decode("utf-8-sig"))
    except (OSError, UnicodeDecodeError, ValueError) as ex:
        e.error = f"cannot read the file: {ex}"
        return e
    if not isinstance(raw, dict):
        e.error = "not a LabDesk room (the file holds no object)"
        return e
    if raw.get("format") not in (None, *ws.FORMATS_READ):
        e.error = f"not a LabDesk room (format {raw.get('format')!r})"
        return e
    if not any(k in raw for k in ("width", "partitions", "aps")):
        e.error = "not a LabDesk room (no width, walls or access points)"
        return e
    try:
        e.site = ws.Site.from_dict(raw)
    except Exception as ex:  # noqa: BLE001  (a hand-edited file must not take the library down)
        e.error = f"the room cannot be read: {type(ex).__name__}: {ex}"
        return e
    e.newer = ws.Site.newer_than_supported(raw)
    return e


def scan(folders) -> list[RoomEntry]:
    """Every ``*.json`` of the folders (not recursive), sorted by kind then title."""
    out, seen = [], set()
    for folder in folders:
        folder = Path(folder)
        if not folder.is_dir():
            continue
        for p in sorted(folder.glob("*.json")):
            rp = p.resolve()
            if rp not in seen:
                seen.add(rp)
                out.append(read_entry(p))
    out.sort(key=lambda e: (bool(e.error), e.kind.lower(), e.title.lower()))
    return out


def for_export(site: ws.Site) -> ws.Site:
    """A copy to write to a file: access points mirrored from a lab become ordinary ones."""
    out = ws.Site.from_dict(site.to_dict())
    for a in out.aps:
        a.from_lab = False
    return out


def write_room(path, site: ws.Site) -> None:
    path = Path(path)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(for_export(site).to_dict(), indent=1, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def load_into(site: ws.Site, new: ws.Site) -> None:
    """Replace ``site``'s contents with ``new``'s in place (keeps every reference to ``site`` valid)."""
    for f in dataclasses.fields(ws.Site):
        setattr(site, f.name, copy.deepcopy(getattr(new, f.name)))


def edited_copy_meta(meta: dict) -> dict:
    """The meta of a library room once the user changed and saved it: says what it was based on."""
    out = copy.deepcopy(meta)
    title = str(out.get("title") or "Room")
    if not title.endswith("(edited)"):
        out["based_on"] = title
        out["title"] = f"{title} (edited)"
    return out


def slug(text: str) -> str:
    """A file name from a title; Hebrew and other letters are kept (only path-unsafe characters go)."""
    s = re.sub(r'[\\/:*?"<>|\s]+', "_", text.strip()).strip("._")
    return s[:80] or "room"


def builtin_folder() -> Path:
    from labdesk_app import config
    return config.PKG_DIR / "rooms"


def user_folder() -> Path:
    from labdesk_app import config
    return config.CONFIG_DIR / "rooms"


def library_folders() -> list[Path]:
    return [builtin_folder(), user_folder()]


def thumb_cache_dir() -> Path:
    from labdesk_app import plat
    return plat.default_cache_dir() / "room-thumbs"


# ----------------------------------------------------------------------------------------- thumbnails
class Thumbnailer:
    """Draws rooms with the app's own view (one hidden ``SiteView``, reused), so the thumbnail shows what
    opening the room shows: GPU when usable, the QPainter fallback otherwise."""

    def __init__(self, cache_dir=None, gl: bool | None = None):
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self._gl = gl
        self._mem: dict[str, QImage] = {}
        self._view = None
        self.rendered = 0

    def _make_view(self):
        from labdesk_app.ui.site3d import SiteView

        class _ThumbView(SiteView):              # the scene only: no legend, hints, dimensions or labels
            def _paint_legend(self, p, tk): pass
            def _paint_hud(self, p, tk): pass
            def _paint_dimensions(self, p, fr, tk): pass
            def _paint_contour_labels(self, p, fr, tk): pass
            def _paint_room_labels(self, p, fr, tk): pass
            def _paint_hover(self, p, fr, tk): pass

        v = _ThumbView(ws.Site(), gl=self._gl)
        v.setAttribute(Qt.WA_DontShowOnScreen, True)
        v.setMinimumSize(1, 1)
        v.resize(THUMB_W, THUMB_H)
        v.set_look(drift=False, glow=0.45, air="waves")
        v.show()
        return v

    @property
    def view(self):
        if self._view is None:
            self._view = self._make_view()
        return self._view

    def key(self, e: RoomEntry) -> str:
        r = "gpu" if self._view is not None and self._view.uses_gl else "any"
        return f"{e.digest}-v{ws.VERSION}-{'d' if T.tokens().dark else 'l'}-{THUMB_W}x{THUMB_H}-{THUMB_REV}-{r}"

    def cached(self, e: RoomEntry) -> QImage | None:
        return self._mem.get(self.key(e))

    def get(self, e: RoomEntry) -> QImage:
        k = self.key(e)
        img = self._mem.get(k)
        if img is not None:
            return img
        f = self.cache_dir / f"{k}.png" if self.cache_dir else None
        if f is not None and f.is_file():
            img = QImage(str(f))
            if not img.isNull():
                self._mem[k] = img
                return img
        img = self.render(e)
        k = self.key(e)                           # the renderer is known now
        self._mem[k] = img
        if self.cache_dir:
            try:
                self.cache_dir.mkdir(parents=True, exist_ok=True)
                img.save(str(self.cache_dir / f"{k}.png"))
            except OSError:
                pass                              # a read-only cache only costs speed
        return img

    def render(self, e: RoomEntry) -> QImage:
        v = self.view
        load_into(v.site, e.site)
        v.site.normalize()
        v.selected = v.selected_wall = -1
        v.site_changed()
        v.cam.yaw, v.cam.pitch = math.radians(-58.0), math.radians(52.0)
        v.fit()
        v.cam.dist *= 0.74                       # fit() leaves room for labels a thumbnail does not draw
        self.rendered += 1
        return v.grab().toImage()

    def close(self) -> None:
        if self._view is not None:
            self._view.close()
            self._view.deleteLater()
            self._view = None


# ----------------------------------------------------------------------------------------- gallery
CARD_W, CARD_H = 256, 236
PAD = 8


class _CardDelegate(QStyledItemDelegate):
    def __init__(self, dialog):
        super().__init__(dialog)
        self.dialog = dialog

    def sizeHint(self, _opt, _idx) -> QSize:
        return QSize(CARD_W, CARD_H)

    def paint(self, p: QPainter, opt, idx) -> None:
        e: RoomEntry = idx.data(ROLE)
        tk = T.tokens()
        r = QRectF(opt.rect).adjusted(5, 5, -5, -5)
        sel = bool(opt.state & QStyle.State_Selected)
        hover = bool(opt.state & QStyle.State_MouseOver)
        p.save()
        p.setRenderHint(QPainter.Antialiasing)
        path = QPainterPath()
        path.addRoundedRect(r, 8, 8)
        p.fillPath(path, tk.card)
        p.setPen(QPen(tk.accent_ring, 2) if sel else QPen(tk.N(8) if hover else tk.card_border, 1))
        p.drawPath(path)
        th = QRectF(r.left() + PAD, r.top() + PAD, r.width() - 2 * PAD, (r.width() - 2 * PAD) * THUMB_H / THUMB_W)
        img = self.dialog.thumbs.cached(e) if not e.error else None
        clip = QPainterPath()
        clip.addRoundedRect(th, 5, 5)
        p.setClipPath(clip)
        if img is not None and not img.isNull():
            p.setRenderHint(QPainter.SmoothPixmapTransform)
            p.drawImage(th, img)
        else:
            p.fillRect(th, tk.N(4))
            p.setPen(tk.muted)
            p.setFont(T.type("caption"))
            p.drawText(th, Qt.AlignCenter, "Cannot open" if e.error else "Drawing…")
        p.setClipping(False)
        y = th.bottom() + 6
        w = r.width() - 2 * PAD
        x = r.left() + PAD
        f = T.type("label")
        p.setFont(f)
        p.setPen(tk.ink if not e.error else tk.muted)
        p.drawText(QRectF(x, y, w, 20), Qt.AlignLeft | Qt.AlignVCenter, T.fm(f).elidedText(e.title, Qt.ElideRight, w))
        y += 20
        if e.title_he:
            f = T.type("body")
            p.setFont(f)
            p.drawText(QRectF(x, y, w, 19), Qt.AlignRight | Qt.AlignVCenter,
                       T.fm(f).elidedText(e.title_he, Qt.ElideLeft, w))
        y += 20
        f = T.type("caption", weight=500)
        p.setFont(f)
        p.setPen(tk.text("down") if e.error else tk.muted)
        line = e.error if e.error else " · ".join(t for t in (e.kind, e.city) if t)
        p.drawText(QRectF(x, y, w, 16), Qt.AlignLeft | Qt.AlignVCenter, T.fm(f).elidedText(line, Qt.ElideRight, w))
        p.restore()


class RoomLibraryDialog(LabDeskDialog):
    """Lists the room files of ``folders`` with thumbnails; ``chosen()`` is the picked entry after exec()."""

    def __init__(self, folders=None, parent=None, thumbs: Thumbnailer | None = None):
        super().__init__("Room library", parent, header=(
            "document-open", "Room library",
            "Start from a ready room, then change it in the panel. The library file is never changed: you work "
            "on a copy."))
        self.folders = [Path(f) for f in (folders if folders is not None else library_folders())]
        self.thumbs = thumbs or Thumbnailer(thumb_cache_dir())
        self._own_thumbs = thumbs is None
        self.entries: list[RoomEntry] = []
        self._pending: list[RoomEntry] = []
        self._chosen: RoomEntry | None = None

        self.search = QLineEdit()
        self.search.setPlaceholderText("Filter by name, city or kind (English or Hebrew)")
        self.search.setClearButtonEnabled(True)
        self.kind = QComboBox()
        self.folder_lbl = QLabel()
        self.folder_lbl.setObjectName("muted")
        top = QHBoxLayout()
        top.addWidget(self.search, 1)
        top.addWidget(self.kind)

        self.list = QListWidget()
        self.list.setViewMode(QListWidget.IconMode)
        self.list.setResizeMode(QListWidget.Adjust)
        self.list.setMovement(QListWidget.Static)
        self.list.setUniformItemSizes(True)
        self.list.setGridSize(QSize(CARD_W, CARD_H))
        self.list.setSpacing(0)
        self.list.setSelectionMode(QAbstractItemView.SingleSelection)
        self.list.setMouseTracking(True)
        self.list.setFrameShape(QListWidget.NoFrame)
        self.list.setItemDelegate(_CardDelegate(self))
        self.list.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)

        self.big = QLabel()
        self.big.setFixedSize(332, int(332 * THUMB_H / THUMB_W))
        self.big.setAlignment(Qt.AlignCenter)
        self.d_title = QLabel()
        self.d_title.setFont(T.type("title"))
        self.d_title.setWordWrap(True)
        self.d_title_he = QLabel()
        self.d_title_he.setFont(T.type("label", scale=1.1))
        self.d_title_he.setAlignment(Qt.AlignRight)
        self.d_title_he.setLayoutDirection(Qt.RightToLeft)
        self.d_kind, self.d_era, self.d_city, self.d_size, self.d_source, self.d_license, self.d_file, self.d_notes = \
            (QLabel() for _ in range(8))
        for lbl in (self.d_kind, self.d_era, self.d_city, self.d_size, self.d_source, self.d_file, self.d_notes,
                    self.d_license, self.d_title_he):
            lbl.setWordWrap(True)
            lbl.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Minimum)
        self.d_source.setOpenExternalLinks(True)
        self.d_source.setTextInteractionFlags(Qt.TextBrowserInteraction)
        self.d_file.setObjectName("muted")
        self.d_file.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.d_warn = QLabel()
        self.d_warn.setWordWrap(True)
        self.d_warn.setStyleSheet(f"color: {T.tokens().text('warn').name()}")
        form = QFormLayout()
        form.setContentsMargins(0, 0, 0, 0)
        form.addRow("Kind", self.d_kind)
        form.addRow("Built", self.d_era)
        form.addRow("City", self.d_city)
        form.addRow("Size", self.d_size)
        form.addRow("Plan", self.d_source)
        form.addRow("Licence", self.d_license)
        form.addRow("File", self.d_file)
        side = QWidget()
        sv = QVBoxLayout(side)
        sv.setContentsMargins(8, 0, 0, 0)
        sv.addWidget(self.big)
        sv.addWidget(self.d_title)
        sv.addWidget(self.d_title_he)
        sv.addLayout(form)
        sv.addWidget(self.d_notes)
        sv.addWidget(self.d_warn)
        sv.addStretch(1)
        side_scroll = QScrollArea()                    # long notes scroll instead of crowding the form
        side_scroll.setWidget(side)
        side_scroll.setWidgetResizable(True)
        side_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        side_scroll.setFrameShape(QScrollArea.NoFrame)
        side_scroll.setFixedWidth(364)
        mid = QHBoxLayout()
        mid.addWidget(self.list, 1)
        mid.addWidget(side_scroll)

        self.import_btn = QPushButton("Open a file…")
        self.import_btn.setToolTip("Open a room file from anywhere")
        self.import_btn.setAutoDefault(False)
        self.cancel_button()
        self.open_btn = self.add_button("Open a copy", QDialogButtonBox.AcceptRole, "primary", default=True)
        foot = QHBoxLayout()
        foot.addWidget(self.import_btn)
        foot.addWidget(self.folder_lbl, 1)
        self.body.addLayout(top)
        self.body.addLayout(mid, 1)
        self.body.addLayout(foot)

        self.search.textChanged.connect(self._filter)
        self.kind.currentIndexChanged.connect(self._filter)
        self.list.currentItemChanged.connect(lambda cur, _prev: self._show(cur.data(ROLE) if cur else None))
        self.list.itemDoubleClicked.connect(lambda _it: self._accept_current())
        self.import_btn.clicked.connect(self._import)
        self._timer = QTimer(self)
        self._timer.setInterval(0)
        self._timer.timeout.connect(self._render_next)
        self.resize(1200, 760)
        self.refresh()

    # ------------------------------------------------------------------ data
    def refresh(self) -> None:
        self.entries = scan(self.folders)
        kinds = sorted({e.kind for e in self.entries if not e.error})
        cur = self.kind.currentData()
        self.kind.blockSignals(True)
        self.kind.clear()
        self.kind.addItem("All kinds", "")
        for k in kinds:
            self.kind.addItem(k, k)
        self.kind.setCurrentIndex(max(self.kind.findData(cur), 0))
        self.kind.blockSignals(False)
        self.list.clear()
        for e in self.entries:
            it = QListWidgetItem()
            it.setData(ROLE, e)
            it.setData(Qt.DisplayRole, e.title)
            it.setToolTip(str(e.path) + (f"\n{e.error}" if e.error else ""))
            self.list.addItem(it)                 # a broken file stays selectable: the side panel says why
        n = sum(1 for e in self.entries if not e.error)
        self.folder_lbl.setText(f"{n} room{'s' if n != 1 else ''}")
        self.folder_lbl.setToolTip("\n".join(str(f) for f in self.folders))
        self._pending = [e for e in self.entries if not e.error]
        self._filter()
        first = next((i for i in range(self.list.count()) if not self.list.item(i).isHidden()), -1)
        self.list.setCurrentRow(first)
        if first < 0:
            self._show(None)
        self._timer.start()

    def _render_next(self) -> None:
        """One thumbnail per event-loop turn: the dialog shows at once and the cards fill in."""
        if not self._pending:
            self._timer.stop()
            return
        e = self._pending.pop(0)
        self.thumbs.get(e)
        self.list.viewport().update()
        cur = self.list.currentItem()
        if cur is not None and cur.data(ROLE) is e:
            self._show(e)

    def render_all(self) -> None:
        while self._pending:
            self._render_next()

    def _filter(self) -> None:
        text, kind = self.search.text(), self.kind.currentData() or ""
        for i in range(self.list.count()):
            it = self.list.item(i)
            e = it.data(ROLE)
            it.setHidden(not (e.matches(text) and (not kind or e.kind == kind)))

    # ------------------------------------------------------------------ details
    def _show(self, e: RoomEntry | None) -> None:
        self.open_btn.setEnabled(e is not None and not e.error)
        for lbl in (self.d_title, self.d_title_he, self.d_kind, self.d_era, self.d_city, self.d_size, self.d_source,
                    self.d_license, self.d_file, self.d_notes, self.d_warn):
            lbl.clear()
        self.big.clear()
        if e is None:
            self.d_title.setText("No room files found")
            return
        self.d_title.setText(e.title)
        self.d_file.setText(str(e.path))
        if e.error:
            self.d_warn.setText(f"This file cannot be opened: {e.error}")
            self.d_warn.setVisible(True)
            return
        img = self.thumbs.cached(e)
        if img is not None:
            dpr = self.devicePixelRatioF()
            pm = QPixmap.fromImage(img.scaled(int(self.big.width() * dpr), int(self.big.height() * dpr),
                                              Qt.KeepAspectRatio, Qt.SmoothTransformation))
            pm.setDevicePixelRatio(dpr)
            self.big.setPixmap(pm)
        else:
            self.big.setText("Drawing…")
        self.d_title_he.setText(e.title_he)
        self.d_title_he.setVisible(bool(e.title_he))
        self.d_kind.setText(e.kind)
        self.d_era.setText(e._s("era") or "—")
        self.d_city.setText(" · ".join(t for t in (e.city, e.city_he) if t) or "—")
        self.d_size.setText(e.summary())
        src = e.source
        if src.get("url"):
            self.d_source.setText(f'<a href="{_esc(src["url"])}">{_esc(src.get("title") or src["url"])}</a>')
            self.d_source.setToolTip(src["url"])
        else:
            self.d_source.setText(_esc(src.get("title") or "No source given"))
        self.d_license.setText(src.get("license") or "—")
        self.d_notes.setText(e.notes + ("\n\nA planning estimate from the published plan, not a survey of the "
                                        "building." if e.notes else ""))
        self.d_notes.setVisible(bool(e.notes))
        self.d_warn.setText("Made by a newer LabDesk: details this version does not know are kept in the file but "
                            "not shown." if e.newer else "")
        self.d_warn.setVisible(e.newer)

    def accept(self) -> None:                     # Open a copy, Enter, double-click
        self._accept_current()

    def _accept_current(self) -> None:
        it = self.list.currentItem()
        e = it.data(ROLE) if it is not None else None
        if e is None or e.error:
            return
        self._chosen = e
        super().accept()

    def chosen(self) -> RoomEntry | None:
        return self._chosen

    def _import(self) -> None:
        fn, _ = QFileDialog.getOpenFileName(self, "Open a room file", str(user_folder() if user_folder().is_dir()
                                                                             else Path.home()),
                                            "LabDesk room (*.json);;All files (*)")
        if not fn:
            return
        e = read_entry(fn)
        if e.error:
            self.d_warn.setText(f"{Path(fn).name}: {e.error}")
            self.d_warn.setVisible(True)
            return
        self._chosen = e
        super().accept()

    def done(self, r: int) -> None:
        self._timer.stop()
        if self._own_thumbs:
            self.thumbs.close()
        super().done(r)


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
