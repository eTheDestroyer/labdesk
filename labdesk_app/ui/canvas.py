"""Topology canvas: QGraphicsScene/View with palette drops, link mode and context menus."""
from __future__ import annotations

import time

from PySide6.QtCore import QEvent, QLineF, QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QBrush, QColor, QPainter, QPen, QPixmap, QTransform
from PySide6.QtWidgets import QGraphicsLineItem, QGraphicsScene, QGraphicsView

from ..catalog import BY_KEY
from . import theme as T
from .graphics import AdjacencyOverlay, BundleItem, LinkItem, NodeItem, PacketItem, RenderCtx
from .hovercard import HOVER_DELAY_MS, HoverCard

MIME = "application/x-labdesk-device"


class LabScene(QGraphicsScene):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setSceneRect(QRectF(-2000, -1500, 4000, 3000))
        self.nodes: dict[str, NodeItem] = {}
        self.links: dict[str, LinkItem] = {}
        self.bundles: dict[str, BundleItem] = {}          # EtherChannels (facts.Bundle.key)
        self.empty_hint = "Drag a device from the palette onto the canvas"
        self.ctx = RenderCtx()
        self.overlay = AdjacencyOverlay()
        self.addItem(self.overlay)
        self.packets: list[PacketItem] = []
        # one shared scene tick (V10 flashes, LED pulse, probing spinner, packet animation);
        # it only runs while something is animating
        self.ticker = QTimer(self)
        self.ticker.setInterval(33)
        self.ticker.timeout.connect(self._tick)
        self._slots: dict[str, float] = {}
        self._slots_lod: float | None = None
        self._labels_key: float | None = None
        self._grid: dict = {}                     # zoom bucket -> QBrush (S7 tiled dot grid)

    # ----------------------------------------------------------------- midpoint stack layout
    def invalidate_layout(self) -> None:
        """Re-solve where each link's midpoint stack goes on the next paint (data, layers, moves)."""
        self._slots_lod = None
        self._labels_key = None

    def theme_changed(self) -> None:
        self._grid.clear()
        self.invalidate_layout()
        for n in self.nodes.values():
            n.sync_spinner()
        self.update()

    # ----------------------------------------------------------------- S8 label collision pass
    def ensure_labels(self, lod: float) -> None:
        """LabelLayout: runs once per topology / telemetry change (invalidate_layout) and zoom bucket, never
        per item paint.  Claims rects in priority order (alarm > protocol chip > subnet > interface name)
        and suppresses a label that overlaps an earlier claim by more than 30 %; paint() only reads the
        resulting ``hidden_labels`` flags (hover / selection reveal them)."""
        key = round(lod, 3)
        if self._labels_key == key:
            return
        self._labels_key = key
        if self.links:
            self.stack_slot(next(iter(self.links)), lod)          # stacks placed first (same lod)
        claimed: list[QRectF] = []
        for n in self.nodes.values():
            claimed.append(n.mapRectToScene(n.tile_rect()))
            claimed.append(n.mapRectToScene(n.name_rect(lod)))
        entries = []
        for n in self.nodes.values():
            for prio, lid, r in n.label_entries(lod):
                entries.append((prio, n, lid, n.mapRectToScene(r)))
        for li in self.links.values():
            for prio, lid, r in li.label_entries(lod):
                entries.append((prio, li, lid, r))
        entries.sort(key=lambda e: e[0])
        hidden: dict = {}
        for prio, item, lid, r in entries:
            area = max(1.0, r.width() * r.height())
            if prio > 0:
                worst = 0.0
                for c in claimed:
                    if c.intersects(r):
                        i = c.intersected(r)
                        worst = max(worst, i.width() * i.height() / area)
                        if worst > 0.30:
                            break
                if worst > 0.30:
                    hidden.setdefault(item, set()).add(lid)
                    continue
            claimed.append(r)
        for item in list(self.nodes.values()) + list(self.links.values()):
            new = hidden.get(item, set())
            if new != item.hidden_labels:
                item.hidden_labels = new
                item.update()
        for n in self.nodes.values():
            n.sync_spinner()

    def stack_slot(self, key: str, lod: float) -> tuple | None:
        """Distance along link ``key`` for its midpoint stack (scene_model.place_stacks: no two stacks overlap)."""
        if getattr(self, "_solving", False):
            return None
        if self._slots_lod is None or abs(self._slots_lod - lod) > 1e-3:
            from .scene_model import place_stacks
            self._solving = True
            try:
                self._slots = self._solve(lod, place_stacks)
            finally:
                self._solving = False
            self._slots_lod = lod
        return self._slots.get(key)

    def _solve(self, lod: float, place_stacks) -> dict:
        entries, obstacles, strokes = [], [], {}
        for k, li in self.links.items():
            c = li.stack_candidates(lod)
            if c is not None:
                cc = li.stack_candidates(lod, compact=True)
                entries.append((k, c[0], c[1], c[2], cc))
            obstacles += li.end_pill_rects(lod)
            path, L = li.path, li.path.length()
            n = max(2, int(L / 8))
            strokes[k] = [(pt.x() - 3, pt.y() - 3, 6.0, 6.0)
                          for pt in (path.pointAtPercent(path.percentAtLength(L * i / n)) for i in range(n + 1))]
        for n in self.nodes.values():
            for r in n.obstacle_rects(lod):
                obstacles.append((r.x(), r.y(), r.width(), r.height()))
        return place_stacks(entries, obstacles, strokes=strokes)

    # ----------------------------------------------------------------- animation
    def flash(self, keys, hue: str) -> None:
        now = time.monotonic()
        for k in keys:
            self.ctx.flash[k] = (now, hue)
        self.ensure_ticking()

    def pulse_subnet(self, link_keys, hue: str) -> None:
        """A changed subnet label pulses for T.PULSE_S (on top of the 800 ms event flash)."""
        now = time.monotonic()
        for k in link_keys:
            if k in self.links:
                self.ctx.subnet_pulse[k] = (now, hue)
        self.ensure_ticking()

    def add_packet(self, legs, color=T.ROUTE_BLUE) -> PacketItem | None:
        if not legs:
            return None
        pk = PacketItem(legs, color)
        self.addItem(pk)
        self.packets.append(pk)
        self.ensure_ticking()
        return pk

    def needs_tick(self) -> bool:
        now = time.monotonic()
        return (bool(self.packets) or any(now - t0 < 0.8 for t0, _ in self.ctx.flash.values())
                or any(now - t0 < T.PULSE_S for t0, _ in self.ctx.subnet_pulse.values())
                or any(n.animating for n in self.nodes.values()))

    def ensure_ticking(self) -> None:
        if not self.ticker.isActive() and self.needs_tick():
            self.ticker.start()

    def _tick(self) -> None:
        now = time.monotonic()
        self.ctx.now = now
        self.ctx.pulse = (now % 1.2) / 1.2
        self.ctx.spin = (now * 360.0) % 360.0
        for k, (t0, _) in list(self.ctx.flash.items()):
            item = self.nodes.get(k) or self.links.get(k) or self.bundles.get(k)
            if item is not None:
                item.update()
            if now - t0 > 0.85:
                del self.ctx.flash[k]
        for k, (t0, _) in list(self.ctx.subnet_pulse.items()):
            item = self.links.get(k)
            if item is not None:
                item.update()
            if now - t0 > T.PULSE_S + 0.05:
                del self.ctx.subnet_pulse[k]
        for n in self.nodes.values():
            if n.animating:
                n.update()
        for pk in list(self.packets):
            pk.tick(now)
            if pk.done:
                self.packets.remove(pk)
                self.removeItem(pk)
        if not self.needs_tick():
            self.ticker.stop()

    def node_moved_hook(self) -> None:
        self.invalidate_layout()
        self.overlay.update()
        for b in self.bundles.values():
            b.adjust()

    GRID = 24.0              # S7: 24 px dot pitch, a stronger dot every 5th

    def grid_brush(self, zoom: float) -> QBrush:
        """One device-resolution tile (5 x 5 dots) per zoom bucket, mapped back to scene units."""
        bucket = max(0.05, round(zoom * 20) / 20)
        hit = self._grid.get(bucket)
        if hit is not None:
            return hit
        tk = self.ctx.tokens
        period = self.GRID * 5
        px = max(4, int(round(period * bucket)))
        pm = QPixmap(px, px)
        pm.fill(Qt.transparent)
        q = QPainter(pm)
        q.setRenderHint(QPainter.Antialiasing)
        q.setPen(Qt.NoPen)
        step = px / 5.0
        # N6 dots / N7 every 5th (one step stronger on the light canvas, where N6 vanishes)
        small, big = (tk.N(6), tk.N(7)) if tk.dark else (tk.N(7), tk.N(8))
        for i in range(5):
            for j in range(5):
                major = i == 0 and j == 0
                q.setBrush(big if major else small)
                r = (1.8 if major else 1.3) / 2 * max(1.0, bucket ** 0.5) * 1.15
                q.drawEllipse(QPointF(i * step + 0.5, j * step + 0.5), r, r)
        q.end()
        br = QBrush(pm)
        br.setTransform(QTransform().scale(period / px, period / px))
        self._grid[bucket] = br
        return br

    def drawBackground(self, p: QPainter, rect: QRectF):
        tk = self.ctx.tokens
        p.fillRect(rect, tk.base)
        zoom = p.worldTransform().m11()
        if zoom >= T.LOD_GRID:
            p.fillRect(rect, self.grid_brush(zoom))
        self.ensure_labels(zoom)


class LabView(QGraphicsView):
    device_dropped = Signal(str, float, float)        # type key, x, y
    link_requested = Signal(str, str)                 # node key a, b
    node_activated = Signal(str)                      # double-click / Enter -> console
    node_context = Signal(str, object)                # key, global pos
    link_context = Signal(str, object)
    bundle_context = Signal(str, object)              # EtherChannel key, global pos
    canvas_context = Signal(object, object)           # scene pos, global pos
    delete_pressed = Signal()
    rename_pressed = Signal()
    link_mode_off = Signal()

    def __init__(self, scene: LabScene, parent=None):
        super().__init__(scene, parent)
        self.setRenderHints(QPainter.Antialiasing | QPainter.TextAntialiasing)
        self.setAcceptDrops(True)
        self.setDragMode(QGraphicsView.RubberBandDrag)
        self.setViewportUpdateMode(QGraphicsView.SmartViewportUpdate)
        self.setTransformationAnchor(QGraphicsView.AnchorUnderMouse)
        self.link_mode = False
        self._link_src: NodeItem | None = None
        self._rubber: QGraphicsLineItem | None = None
        self._hover_target: NodeItem | None = None
        self.overlay_text = ""
        self.lab_open = False                        # L6: zero-node drop target only while a lab is open
        self._pan = None
        self.legend = None
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setFrameShape(QGraphicsView.NoFrame)
        scene.ctx.tokens = T.tokens(self.palette())
        # live hover card (ui/hovercard.py): shown HOVER_DELAY_MS after the pointer rests on a device tile
        self.card_provider = None                    # callable(NodeItem) -> (CardModel, QPixmap | None) | None
        self.hover_card = HoverCard(self.viewport())
        self._hover_item: NodeItem | None = None
        self._hover_pos = None
        self._hover_timer = QTimer(self)
        self._hover_timer.setSingleShot(True)
        self._hover_timer.setInterval(HOVER_DELAY_MS)
        self._hover_timer.timeout.connect(self._show_hover_card)
        scene.ctx.hover_card = True

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        for w in self.viewport().children():
            if hasattr(w, "reposition"):
                w.reposition()

    def scrollContentsBy(self, dx, dy):
        # QAbstractScrollArea scrolls viewport children too: keep the overlays pinned
        super().scrollContentsBy(dx, dy)
        for w in self.viewport().children():
            if hasattr(w, "reposition") and getattr(w, "isVisible", lambda: False)():
                w.reposition()

    def zoom_level(self) -> float:
        return self.transform().m11()

    def center_on_item(self, item) -> None:
        self.centerOn(item.sceneBoundingRect().center() if not isinstance(item, LinkItem)
                      else item.path.pointAtPercent(0.5))

    # ---------------------------------------------------------------- helpers
    def node_at(self, view_pos) -> NodeItem | None:
        for it in self.items(view_pos):
            if isinstance(it, NodeItem):
                return it
        return None

    def link_at(self, view_pos) -> LinkItem | None:
        for it in self.items(view_pos):
            if isinstance(it, LinkItem):
                return it
        return None

    def node_or_link_at(self, view_pos):
        for it in self.items(view_pos):
            if isinstance(it, (NodeItem, LinkItem)):
                return it
        return None

    def set_link_mode(self, on: bool):
        self.link_mode = on
        self.viewport().setCursor(Qt.CrossCursor if on else Qt.ArrowCursor)
        for n in self.scene().nodes.values():
            n.setFlag(NodeItem.ItemIsMovable, not on)

    def zoom(self, factor: float):
        self.hide_hover_card()
        cur = self.transform().m11()
        if 0.25 <= cur * factor <= 3.0:
            self.scale(factor, factor)

    def fit_all(self):
        self.hide_hover_card()
        items = self.scene().nodes.values()
        if not items:
            self.resetTransform()
            self.centerOn(0, 0)
            return
        r = QRectF()
        for it in items:
            r = r.united(it.content_rect())
        r.adjust(-24, -24, 24, 24)
        vp = self.viewport().rect()
        content = [it.content_rect() for it in items]

        def apply(lw: float) -> None:
            avail_w = max(200, vp.width() - lw)
            s = min(avail_w / max(1.0, r.width()), vp.height() / max(1.0, r.height()), 1.25)
            self.resetTransform()
            self.scale(s, s)
            c = r.center()
            self.centerOn(c.x() - lw / 2 / s, c.y())

        apply(0)
        lg = self.legend
        if lg is not None and lg.isVisible():
            # keep the topology clear of the legend overlay - only when they would actually overlap
            lr = self.mapToScene(lg.geometry()).boundingRect()
            if any(lr.intersects(cr) for cr in content):
                apply(lg.width() + 16)

    # ------------------------------------------------------------ hover card
    def hide_hover_card(self) -> None:
        self._hover_timer.stop()
        self._hover_item = None
        self.hover_card.dismiss()

    def _track_hover(self, view_pos) -> None:
        if self.card_provider is None or self._pan is not None or self._rubber is not None:
            return
        node = self.node_at(view_pos)
        if node is not None and node.has_card and \
                node.card_hit(node.mapFromScene(self.mapToScene(view_pos)), self.zoom_level()):
            self._hover_pos = view_pos
            if node is not self._hover_item:
                self.hover_card.dismiss()
                self._hover_item = node
                self._hover_timer.start()
            elif not self.hover_card.isVisible() and not self._hover_timer.isActive():
                self._hover_timer.start()
            return
        if self._hover_item is not None or self.hover_card.isVisible():
            self.hide_hover_card()

    def _show_hover_card(self) -> None:
        node = self._hover_item
        if node is None or node.scene() is not self.scene() or self.card_provider is None:
            return
        try:
            got = self.card_provider(node)
        except Exception:  # noqa: BLE001  - a card must never break the canvas
            got = None
        if not got:
            return
        model, icon = got
        self.hover_card.show_card(model, icon, self._hover_pos)

    def refresh_hover_card(self) -> None:
        """New facts arrived: rebuild the visible card in place (or drop it if its node is gone)."""
        key = self.hover_card.node_key
        if not self.hover_card.isVisible() or key is None:
            return
        node = self.scene().nodes.get(key)
        if node is None or node is not self._hover_item or self.card_provider is None:
            self.hide_hover_card()
            return
        try:
            got = self.card_provider(node)
        except Exception:  # noqa: BLE001
            got = None
        if got:
            self.hover_card.set_model(got[0], got[1])

    def viewportEvent(self, ev):  # noqa: N802
        if ev.type() in (QEvent.Leave, QEvent.DragEnter):
            self.hide_hover_card()
        return super().viewportEvent(ev)

    # ------------------------------------------------------------ drag & drop
    def dragEnterEvent(self, ev):
        if ev.mimeData().hasFormat(MIME):
            ev.acceptProposedAction()
        else:
            super().dragEnterEvent(ev)

    def dragMoveEvent(self, ev):
        if ev.mimeData().hasFormat(MIME):
            ev.acceptProposedAction()
        else:
            super().dragMoveEvent(ev)

    def dropEvent(self, ev):
        if ev.mimeData().hasFormat(MIME):
            key = bytes(ev.mimeData().data(MIME)).decode()
            if key in BY_KEY:
                pos = self.mapToScene(ev.position().toPoint())
                self.device_dropped.emit(key, pos.x(), pos.y())
            ev.acceptProposedAction()
        else:
            super().dropEvent(ev)

    # ------------------------------------------------------------------ mouse
    def mousePressEvent(self, ev):
        self.hide_hover_card()
        if ev.button() == Qt.MiddleButton or (ev.button() == Qt.LeftButton and ev.modifiers() & Qt.AltModifier):
            self._pan = ev.position().toPoint()
            self.viewport().setCursor(Qt.ClosedHandCursor)
            ev.accept()
            return
        node = self.node_at(ev.position().toPoint())
        if ev.button() == Qt.LeftButton and node and (self.link_mode or ev.modifiers() & Qt.ShiftModifier):
            self._link_src = node
            p = node.scenePos()
            self._rubber = self.scene().addLine(QLineF(p, p), QPen(QColor("#F2A93B"), 2.5, Qt.DashLine))
            self._rubber.setZValue(50)
            ev.accept()
            return
        super().mousePressEvent(ev)

    def mouseMoveEvent(self, ev):
        if ev.buttons() != Qt.NoButton:
            self.hide_hover_card()               # dragging a tile, rubber band, pan
        else:
            self._track_hover(ev.position().toPoint())
        if self._pan is not None:
            p = ev.position().toPoint()
            d = p - self._pan
            self._pan = p
            self.horizontalScrollBar().setValue(self.horizontalScrollBar().value() - d.x())
            self.verticalScrollBar().setValue(self.verticalScrollBar().value() - d.y())
            ev.accept()
            return
        if self._rubber and self._link_src:
            p2 = self.mapToScene(ev.position().toPoint())
            self._rubber.setLine(QLineF(self._link_src.scenePos(), p2))
            tgt = self.node_at(ev.position().toPoint())
            if tgt is self._link_src:
                tgt = None
            if tgt is not self._hover_target:
                if self._hover_target:
                    self._hover_target.link_highlight = False
                    self._hover_target.update()
                self._hover_target = tgt
                if tgt:
                    tgt.link_highlight = True
                    tgt.update()
            ev.accept()
            return
        super().mouseMoveEvent(ev)

    def mouseReleaseEvent(self, ev):
        if self._pan is not None:
            self._pan = None
            self.viewport().setCursor(Qt.CrossCursor if self.link_mode else Qt.ArrowCursor)
            ev.accept()
            return
        if self._rubber and self._link_src:
            tgt = self.node_at(ev.position().toPoint())
            self.scene().removeItem(self._rubber)
            self._rubber = None
            if self._hover_target:
                self._hover_target.link_highlight = False
                self._hover_target.update()
                self._hover_target = None
            src, self._link_src = self._link_src, None
            if tgt and tgt is not src:
                self.link_requested.emit(src.key, tgt.key)
            ev.accept()
            return
        super().mouseReleaseEvent(ev)

    def mouseDoubleClickEvent(self, ev):
        self.hide_hover_card()
        node = self.node_at(ev.position().toPoint())
        if node:
            self.node_activated.emit(node.key)
            ev.accept()
            return
        super().mouseDoubleClickEvent(ev)

    def contextMenuEvent(self, ev):
        self.hide_hover_card()
        node = self.node_at(ev.pos())
        if node:
            if not node.isSelected():
                self.scene().clearSelection()
                node.setSelected(True)
            self.node_context.emit(node.key, ev.globalPos())
            return
        bundle = next((it for it in self.items(ev.pos()) if isinstance(it, BundleItem)), None)
        link = self.link_at(ev.pos())
        if bundle is not None and (link is None or not link.isSelected()):
            self.scene().clearSelection()
            bundle.setSelected(True)
            self.bundle_context.emit(bundle.key, ev.globalPos())
            return
        if link:
            # keep a Ctrl-click multi-selection of links (Bundle into EtherChannel…); else select just this one
            sel_links = [i for i in self.scene().selectedItems() if isinstance(i, LinkItem)]
            if not (link.isSelected() and len(sel_links) > 1):
                self.scene().clearSelection()
                link.setSelected(True)
            self.link_context.emit(link.key, ev.globalPos())
            return
        self.canvas_context.emit(self.mapToScene(ev.pos()), ev.globalPos())

    def wheelEvent(self, ev):
        self.hide_hover_card()
        if ev.modifiers() & Qt.ControlModifier:
            self.zoom(1.15 if ev.angleDelta().y() > 0 else 1 / 1.15)
            ev.accept()
            return
        super().wheelEvent(ev)

    def keyPressEvent(self, ev):
        self.hide_hover_card()
        k = ev.key()
        if k in (Qt.Key_Return, Qt.Key_Enter):
            sel = [i for i in self.scene().selectedItems() if isinstance(i, NodeItem)]
            if sel:
                self.node_activated.emit(sel[0].key)
                return
        if k == Qt.Key_Delete:
            self.delete_pressed.emit()
            return
        if k == Qt.Key_F2:
            self.rename_pressed.emit()
            return
        if k == Qt.Key_Escape and self.link_mode:
            self.set_link_mode(False)
            self.link_mode_off.emit()
        super().keyPressEvent(ev)

    def drawForeground(self, p: QPainter, rect: QRectF):
        """L6: a lab with zero nodes gets a dashed 'Drag a device here' target (no lab open = the
        EmptyStateCard widget; ``overlay_text`` is a legacy free-text message)."""
        if self.scene().nodes:
            return
        if not self.overlay_text and not self.lab_open:
            return
        tk = T.tokens()
        p.save()
        p.resetTransform()
        p.setRenderHint(QPainter.Antialiasing)
        vr = QRectF(self.viewport().rect())
        if self.overlay_text:
            p.setFont(T.type("title"))
            p.setPen(tk.N(11))
            p.drawText(vr, Qt.AlignCenter, self.overlay_text)
            p.restore()
            return
        w, h = min(420.0, vr.width() - 48), min(220.0, vr.height() - 48)
        r = QRectF(vr.center().x() - w / 2, vr.center().y() - h / 2, w, h)
        col = T.alpha(tk.N(11), 0.40)
        pen = QPen(col, 2)
        pen.setDashPattern([3, 2])                    # 6/4 px at width 2
        p.setPen(pen)
        p.setBrush(T.alpha(tk.N(11), 0.03))
        p.drawRoundedRect(r, 12, 12)
        p.setFont(T.type("title"))
        p.setPen(tk.N(11))
        p.drawText(r.adjusted(0, 0, 0, -18), Qt.AlignCenter, "Drag a device here")
        p.setFont(T.type("small"))
        p.drawText(r.adjusted(16, 40, -16, 0), Qt.AlignCenter | Qt.TextWordWrap,
                   "from the Devices palette, or right-click to add one")
        p.restore()
