"""3D site view: the room, its walls, doors and obstacles, the access points and a Wi-Fi heat map.

* ``SiteView``   - the 3D canvas: orbit / zoom / pan, drag an access point, hover the floor for a reading.
* ``SiteWindow`` - the view plus the side panel (space, walls, machines and racking, access points, view).

Two ways to draw the scene, chosen at run time:

* **Graphics card** (``site3d_gl.SceneRenderer``, OpenGL 3.3 into an off-screen image): lit walls, a smooth
  heat map on the floor and the signal as a glow filling the air.
* **Software** (QPainter): the same room and the same smooth heat map with contour lines, no glow.  Used when
  there is no usable graphics card, and by the tests, so the view never depends on a driver.

Either way the labels, access points, legend and the reading under the pointer are painted here with QPainter
on top.  The model lives in ``labdesk_app.wifi_site``, the fast field in ``labdesk_app.wifi_field`` (no Qt).
"""
from __future__ import annotations

import math
import os
from pathlib import Path

import numpy as np
from PySide6.QtCore import QElapsedTimer, QPointF, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QImage, QPainter, QPen, QPixmap, QPolygonF, QTransform
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog, QFormLayout, QGroupBox, QHBoxLayout,
                               QLabel, QListWidget, QPushButton, QScrollArea, QSlider, QVBoxLayout, QWidget)

from .. import wifi_field as wf
from .. import wifi_anim as wa
from .. import wifi_link as wl
from .. import wifi_site as ws
from . import theme as T

NEAR = 0.05                       # near clipping plane (m)
FOG_FRAME_MS = 50                 # how often drifting fog is redrawn
WAVE_FRAME_MS = 33                # how often moving waves are redrawn (smoother: the fronts are sharp)
AIR_MODES = {"waves": "Waves from the access points", "fog": "Fog"}

# ---------------------------------------------------------------------------- heat-map colours
RAMP_LO, RAMP_HI = -85.0, -40.0   # below RAMP_LO there is no service: the floor shows through
CONTOURS = tuple(b[0] for b in ws.SIGNAL_BANDS if b[0] > RAMP_LO)        # lines at the band limits
# name -> (title, stops (dBm, colour) from weak to strong).  'classic' is what Wi-Fi planning tools show;
# 'safe' keeps its order for viewers who cannot tell red from green (lightness rises with the signal).
RAMPS: dict[str, tuple[str, tuple]] = {
    "classic": ("Classic: green to red", ((-85, "#b5121b"), (-75, "#f25c19"), (-67, "#ffc20a"), (-60, "#a3d12b"),
                                          (-50, "#23a046"), (-40, "#00612d"))),
    "safe": ("Colour-blind safe", ((-85, "#440154"), (-75, "#3b528b"), (-67, "#21918c"), (-60, "#5ec962"),
                                   (-50, "#b5de2b"), (-40, "#fde725"))),
}
INK = "#14181d"                   # contour and grid lines on the heat map (the same in both themes)
OBSTACLE_COLOR = "#46607f"        # machines and racking: steel blue, apart from the grey walls
_LUT: dict[str, np.ndarray] = {}


def ramp_lut(name: str) -> np.ndarray:
    """256 x 4 uint8 RGBA table of a ramp: entry 0 is RAMP_LO, entry 255 is RAMP_HI."""
    lut = _LUT.get(name)
    if lut is None:
        stops = RAMPS.get(name, RAMPS["classic"])[1]
        xs = [(d - RAMP_LO) / (RAMP_HI - RAMP_LO) * 255.0 for d, _c in stops]
        cols = np.array([[QColor(c).red(), QColor(c).green(), QColor(c).blue()] for _d, c in stops], float)
        pos = np.arange(256.0)
        lut = np.empty((256, 4), np.uint8)
        for ch in range(3):
            lut[:, ch] = np.clip(np.interp(pos, xs, cols[:, ch]) + 0.5, 0, 255)
        lut[:, 3] = 255
        _LUT[name] = lut
    return lut


def ramp_color(name: str, dbm: float) -> QColor:
    lut = ramp_lut(name)
    r, g, b, _a = lut[int(ws.clamp((dbm - RAMP_LO) / (RAMP_HI - RAMP_LO) * 255.0, 0, 255))]
    return QColor(int(r), int(g), int(b))


def ramp_image(name: str) -> QImage:
    """The ramp as a 256 x 1 image (what the graphics-card renderer samples)."""
    lut = np.ascontiguousarray(ramp_lut(name))
    return QImage(lut.data, 256, 1, 256 * 4, QImage.Format_RGBA8888).copy()


def heat_image(plane: np.ndarray, ramp: str) -> QImage:
    """A ``[y, x]`` plane of dBm as an image, one pixel per cell; transparent where there is no service."""
    idx = np.clip((plane - RAMP_LO) * (255.0 / (RAMP_HI - RAMP_LO)), 0, 255).astype(np.uint8)
    rgba = ramp_lut(ramp)[idx]
    rgba[plane < RAMP_LO] = 0
    rgba = np.ascontiguousarray(rgba)
    ny, nx = plane.shape
    return QImage(rgba.data, nx, ny, nx * 4, QImage.Format_RGBA8888_Premultiplied).copy()


def band_colors(ramp: str) -> list[QColor]:
    """One swatch per signal band, strongest first: the ramp colour in the middle of the band."""
    floors = [b[0] for b in ws.SIGNAL_BANDS]
    tops = [RAMP_HI] + floors[:-1]
    return [ramp_color(ramp, (lo + min(hi, RAMP_HI)) / 2) for lo, hi in zip(floors, tops)]


def fmt_dbm(v: float) -> str:
    return f"−{abs(v):.0f} dBm" if v < 0 else f"{v:.0f} dBm"


def _rgb(c) -> tuple:
    c = QColor(c)
    return (c.redF(), c.greenF(), c.blueF())


# ============================================================================ camera
class Camera:
    """Orbit camera: looks at ``target`` from ``dist`` metres away (yaw around z, pitch above the floor)."""

    def __init__(self):
        self.yaw = math.radians(-62.0)
        self.pitch = math.radians(34.0)
        self.dist = 20.0
        self.target = (0.0, 0.0, 0.0)
        self.fov = math.radians(40.0)

    def key(self) -> tuple:
        return (round(self.yaw, 5), round(self.pitch, 5), round(self.dist, 4),
                tuple(round(v, 4) for v in self.target))


class Frame:
    """The camera resolved for one widget size: eye, basis, focal length."""

    def __init__(self, cam: Camera, w: float, h: float):
        cp, sp = math.cos(cam.pitch), math.sin(cam.pitch)
        cy_, sy_ = math.cos(cam.yaw), math.sin(cam.yaw)
        t = cam.target
        self.eye = (t[0] + cam.dist * cp * cy_, t[1] + cam.dist * cp * sy_, t[2] + cam.dist * sp)
        f = (-cp * cy_, -cp * sy_, -sp)
        n = math.hypot(f[0], f[1]) or 1e-9
        r = (f[1] / n, -f[0] / n, 0.0)
        self.f, self.r = f, r
        self.u = (r[1] * f[2], -r[0] * f[2], r[0] * f[1] - r[1] * f[0])
        self.focal = (h / 2.0) / math.tan(cam.fov / 2.0)
        self.cx, self.cy = w / 2.0, h / 2.0

    def cam(self, p) -> tuple:
        e, r, u, f = self.eye, self.r, self.u, self.f
        d0, d1, d2 = p[0] - e[0], p[1] - e[1], p[2] - e[2]
        return (d0 * r[0] + d1 * r[1], d0 * u[0] + d1 * u[1] + d2 * u[2], d0 * f[0] + d1 * f[1] + d2 * f[2])

    def screen(self, c) -> QPointF:
        return QPointF(self.cx + self.focal * c[0] / c[2], self.cy - self.focal * c[1] / c[2])

    def proj(self, p) -> QPointF | None:
        c = self.cam(p)
        return self.screen(c) if c[2] >= NEAR else None

    def depth(self, p) -> float:
        return math.dist(self.eye, p)

    def poly(self, pts) -> QPolygonF | None:
        """Project a 3D polygon, clipped against the near plane."""
        cs = [self.cam(p) for p in pts]
        if all(c[2] >= NEAR for c in cs):
            return QPolygonF([self.screen(c) for c in cs])
        out = []
        for i, a in enumerate(cs):
            b = cs[(i + 1) % len(cs)]
            ain, bin_ = a[2] >= NEAR, b[2] >= NEAR
            if ain:
                out.append(a)
            if ain != bin_:
                t = (NEAR - a[2]) / (b[2] - a[2])
                out.append((a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t, NEAR))
        return QPolygonF([self.screen(c) for c in out]) if len(out) >= 3 else None

    def ray(self, sx: float, sy: float) -> tuple:
        a, b = sx - self.cx, self.cy - sy
        d = tuple(self.f[i] * self.focal + self.r[i] * a + self.u[i] * b for i in range(3))
        n = math.sqrt(d[0] * d[0] + d[1] * d[1] + d[2] * d[2])
        return tuple(v / n for v in d)

    def on_plane(self, sx: float, sy: float, z: float) -> tuple | None:
        """Where the view ray through a screen point meets the horizontal plane at height ``z``."""
        d = self.ray(sx, sy)
        if abs(d[2]) < 1e-9:
            return None
        t = (z - self.eye[2]) / d[2]
        if t <= 0:
            return None
        return (self.eye[0] + d[0] * t, self.eye[1] + d[1] * t, z)


# ============================================================================ the 3D canvas
class SiteView(QWidget):
    edited = Signal()             # the site was changed in the view (an access point was dragged)
    ap_picked = Signal(int)       # index of the access point that was clicked
    walk_changed = Signal()       # the phone walk's path, state or reading changed

    def __init__(self, site: ws.Site, parent=None, gl: bool | None = None):
        """``gl``: True / False forces the graphics-card or the software renderer; None picks the graphics
        card when one is usable (LABDESK_NO_GL=1 in the environment switches it off)."""
        super().__init__(parent)
        self.site = site
        self.cam = Camera()
        self.engine = wf.FieldEngine(site)
        self.selected = -1
        self.selected_wall = -1
        self.show_heat = True
        self.glow = 0.6               # 0 = no fog in the air ... 1 = thickest
        self.drift = True             # the waves move out / the fog's billows drift (looks only)
        self.air = "waves"            # what fills the air: AIR_MODES
        self.packets = True           # data packets stream between the serving access point and the phone
        self.walk_path: list[tuple[float, float]] = []      # floor points the phone walks through
        self.walk_drawing = False     # clicks on the floor add points to the path
        self.walking = False
        self.walk_dist = 0.0          # metres along the path
        self.walk_speed = wa.WALK_SPEED
        self._walk_hold = 0.0         # seconds left to wait at the end of the path before walking it again
        self._walk_key = None
        self._walk_cache: list = []
        self._flow_key = None
        self._flow = None
        self._last_tick = 0.0
        self._fog_t = 0.0
        self._clock = QElapsedTimer()
        self._clock.start()
        self._fog_timer = QTimer(self)
        self._fog_timer.setInterval(WAVE_FRAME_MS if self.air == "waves" else FOG_FRAME_MS)
        self._fog_timer.timeout.connect(self._fog_tick)
        self.ramp = "classic"
        self.shares: list[float] = [0.0] * (len(ws.SIGNAL_BANDS) + 1)
        self._want_gl = gl if gl is not None else os.environ.get("LABDESK_NO_GL") != "1"
        self._gl = None               # site3d_gl.SceneRenderer once created
        self._gl_tried = False
        self.gl_reason = ""           # why the graphics-card renderer is not used ('' = it is)
        self._gl_site_rev = self._gl_field_rev = self._gl_ramp = None
        self._gl_key = None
        self._gl_img: QImage | None = None
        self._rev = 0                 # bumped on every change of the field
        self._geom_rev = 0            # bumped when the room, a wall, a door or an obstacle changed
        self._geom_sig = None
        self._walls: list[ws.Wall] = []
        self._rooms: list[ws.Room] = []
        self._plane = np.zeros((2, 2), np.float32)
        self._contours: dict[float, list] = {}
        self._contours_rev = -1
        self._static_key = None
        self._static_pm: QPixmap | None = None
        self._hover = None            # (x, y) on the floor under the pointer
        self._hover_pos = QPointF()
        self._drag = None             # ('orbit' | 'pan' | 'ap', last QPointF)
        self.setMouseTracking(True)
        self.setMinimumSize(420, 320)
        self.setFocusPolicy(Qt.StrongFocus)
        self.site_changed()
        self.fit()

    def sizeHint(self) -> QSize:
        return QSize(820, 600)

    # ---------------------------------------------------------------- model -> view
    def site_changed(self) -> None:
        """Call after the site was edited: recomputes walls, rooms, the field and the floor's shares."""
        s = self.site
        sig = (s.width, s.depth, s.height, repr([p.to_dict() for p in s.partitions]),
               repr([o.to_dict() for o in s.obstacles]))
        if sig != self._geom_sig:
            self._geom_sig = sig
            self._geom_rev += 1
            self._walls = s.walls()
            self._rooms = s.rooms()
        self.engine.volume()
        self._plane = self.engine.slice()
        self.shares = wf.band_shares(self._plane, wf.indoor_mask(s, self.engine.grid))
        self._rev += 1
        self._sync_fog_timer()                         # an access point may have come on or gone off
        self.update()

    @property
    def plane(self) -> np.ndarray:
        """The field on the measuring plane, ``[y, x]`` dBm: what the floor heat map shows."""
        return self._plane

    @property
    def rooms(self) -> list[ws.Room]:
        return self._rooms

    @property
    def uses_gl(self) -> bool:
        return self._renderer() is not None

    @property
    def field_on_gpu(self) -> bool:
        """True when the signal shown was worked out on the graphics card (not only drawn there)."""
        return self._gl is not None and bool(getattr(self._gl, "field_on_gpu", False))

    def gpu_note(self) -> str:
        """'GeForce GTX 1080 · signal every 9 cm': which card draws the view and how fine its field is."""
        gl = self._gl
        if gl is None:
            return ""
        name = (gl.renderer_name or "graphics card").split("/")[0].replace("NVIDIA ", "").strip()
        if not self.field_on_gpu:
            return name
        nx, ny, nz = gl.field_shape
        cell = min(self.site.width / nx, self.site.depth / ny, self.site.height / nz) * 100
        return f"{name} · signal every {cell:.0f} cm"

    def fit(self) -> None:
        s = self.site
        self.cam.target = (s.width / 2, s.depth / 2, s.height * 0.35)
        diag = math.sqrt(s.width ** 2 + s.depth ** 2 + s.height ** 2)
        self.cam.dist = (diag / 2) / math.tan(self.cam.fov / 2) * 1.12
        self.update()

    def set_look(self, heat: bool | None = None, glow: float | None = None, ramp: str | None = None,
                 drift: bool | None = None, air: str | None = None) -> None:
        if air is not None and air in AIR_MODES:
            self.air = air
        if heat is not None:
            self.show_heat = heat
        if glow is not None:
            self.glow = ws.clamp(glow, 0.0, 1.0)
        if ramp is not None and ramp in RAMPS:
            self.ramp = ramp
        if drift is not None:
            self.drift = bool(drift)
        self._sync_fog_timer()
        self.update()

    # ---------------------------------------------------------------- animation
    def fog_moving(self) -> bool:
        return self.drift and self.glow > 0.0 and self._gl is not None and self.isVisible()

    def animating(self) -> bool:
        """Something on screen moves: waves or fog, beacon flashes, data packets, or the walking phone."""
        if not self.isVisible():
            return False
        return self.walking or self.fog_moving() or (self.drift and any(a.enabled for a in self.site.aps))

    def _sync_fog_timer(self) -> None:
        self._fog_timer.setInterval(WAVE_FRAME_MS if self.air == "waves" or self.walking else FOG_FRAME_MS)
        if self.animating():
            if not self._fog_timer.isActive():
                self._last_tick = self._clock.elapsed() / 1000.0
                self._fog_timer.start()
        elif self._fog_timer.isActive():
            self._fog_timer.stop()

    def _fog_tick(self) -> None:
        if not self.animating():
            self._fog_timer.stop()
            return
        now = self._clock.elapsed() / 1000.0
        dt, self._last_tick = min(max(now - self._last_tick, 0.0), 0.25), now
        if self.walking:
            self.advance_walk(dt)
        self.update()

    # ---------------------------------------------------------------- the walking phone
    def set_walk_path(self, points) -> None:
        self.walk_path = [(ws.clamp(float(x), 0.0, self.site.width), ws.clamp(float(y), 0.0, self.site.depth))
                          for x, y in points]
        self.walk_dist = 0.0
        if len(self.walk_path) < 2:
            self.walking = False
        self._sync_fog_timer()
        self.walk_changed.emit()
        self.update()

    def start_drawing_walk(self) -> None:
        self.walking = False
        self.walk_drawing = True
        self.walk_path = []
        self.walk_dist = 0.0
        self.setCursor(Qt.CrossCursor)
        self._sync_fog_timer()
        self.walk_changed.emit()
        self.update()

    def finish_drawing_walk(self) -> None:
        """End the path: with two points or more the phone starts walking it."""
        if not self.walk_drawing:
            return
        self.walk_drawing = False
        self.setCursor(Qt.ArrowCursor)
        self.walk_dist = 0.0
        self.set_walking(len(self.walk_path) >= 2)

    def set_walking(self, on: bool) -> None:
        self.walking = bool(on) and len(self.walk_path) >= 2
        if self.walking and self.walk_dist >= wa.path_length(self.walk_path):
            self.walk_dist = 0.0
        self._last_tick = self._clock.elapsed() / 1000.0
        self._sync_fog_timer()
        self.walk_changed.emit()
        self.update()

    def advance_walk(self, dt: float) -> None:
        total = wa.path_length(self.walk_path)
        if total <= 0.0:
            self.walking = False
            return
        if self._walk_hold > 0.0:
            self._walk_hold -= dt
            if self._walk_hold <= 0.0:
                self.walk_dist = 0.0
        else:
            self.walk_dist = min(total, self.walk_dist + dt * self.walk_speed)
            if self.walk_dist >= total:
                self._walk_hold = 1.5                       # a breath at the end, then round again
        self.walk_changed.emit()

    def walk_samples(self) -> list:
        key = (tuple(self.walk_path), self._rev, self.site.slice_z)
        if key != self._walk_key:
            self._walk_key = key
            self._walk_cache = wa.walk_samples(self.site, self.walk_path, self.site.slice_z)
        return self._walk_cache

    def phone(self) -> tuple | None:
        """Where the phone is: on the walk when there is one in progress, else under the pointer."""
        if len(self.walk_path) >= 2 and (self.walking or self.walk_dist > 0.0):
            x, y = wa.walk_position(self.walk_path, self.walk_dist)
            return (x, y, self.site.slice_z)
        if self._hover is not None and self._drag is None and not self.walk_drawing:
            return (self._hover[0], self._hover[1], self.site.slice_z)
        return None

    def walk_status(self) -> str:
        """'−63 dBm · Good · AP1 · 288-649 Mbit/s · 12.5 of 30.0 m' for the panel."""
        if self.walk_drawing:
            n = len(self.walk_path)
            return f"Click on the floor to add points ({n} so far); double-click, Enter or right-click to finish."
        if len(self.walk_path) < 2:
            return "Draw a path through the rooms; a phone walks it and reads the signal as it goes."
        x, y = wa.walk_position(self.walk_path, self.walk_dist)
        v, ap = self.site.best((x, y, self.site.slice_z), self._walls)
        total = wa.path_length(self.walk_path)
        where = f"{self.walk_dist:.1f} of {total:.1f} m"
        if ap is None:
            return f"No access point · {where}"
        lk = wl.link(self.site, v, ap)
        return f"{fmt_dbm(v)} · {ws.band_title(v)} · {ap.name} · {lk.speed_text()} · {where}"

    def showEvent(self, ev):
        super().showEvent(ev)
        self._sync_fog_timer()

    def hideEvent(self, ev):
        super().hideEvent(ev)
        self._sync_fog_timer()

    # ---------------------------------------------------------------- graphics-card renderer
    def _renderer(self):
        if not self._gl_tried:
            self._gl_tried = True
            if not self._want_gl:
                self.gl_reason = "switched off"
            else:
                try:
                    from .site3d_gl import SceneRenderer
                    self._gl, self.gl_reason = SceneRenderer.create()
                except Exception as e:  # noqa: BLE001  (a broken driver must never take the view down)
                    self._gl, self.gl_reason = None, f"{type(e).__name__}: {e}"
        return self._gl

    def _drop_gl(self, why: str) -> None:
        gl, self._gl = self._gl, None
        self.gl_reason = why
        try:
            if gl is not None:
                gl.close()
        except Exception:  # noqa: BLE001
            pass

    def _gl_frame(self, tk) -> QImage | None:
        gl = self._renderer()
        if gl is None:
            return None
        scale = 1.0 if self._drag is not None else self.devicePixelRatioF()
        if self.drift and self.glow > 0.0:
            self._fog_t = self._clock.elapsed() / 1000.0
        key = (self.cam.key(), self._rev, self._geom_rev, T.generation(), self.width(), self.height(), scale,
               self.show_heat, round(self.glow, 3), self.ramp, self.selected_wall, self.site.slice_z,
               round(self._fog_t, 3), self.air, tuple((a.x, a.y, a.z) for a in self.site.aps if a.enabled))
        if key == self._gl_key and self._gl_img is not None:
            return self._gl_img
        try:
            from .site3d_gl import RenderOpts
            if self._gl_site_rev != self._geom_rev:
                gl.set_site(self.site)
                self._gl_site_rev = self._geom_rev
            if self._gl_field_rev != self._rev:
                # the graphics card works the field out itself, on a grid about twenty times finer than the
                # CPU's; the CPU volume is the fallback (and stays the source of the numbers on the panel)
                if not gl.compute_field(self.site):
                    gl.set_field(self.engine.volume())
                self._gl_field_rev = self._rev
            if self._gl_ramp != self.ramp:
                gl.set_ramp(ramp_image(self.ramp))
                self._gl_ramp = self.ramp
            fr = self._frame()
            opts = RenderOpts(slice_z=self.site.slice_z, heat=self.show_heat, glow=self.glow, contours=CONTOURS,
                              lo_dbm=RAMP_LO, hi_dbm=RAMP_HI, background=_rgb(tk.base), floor=_rgb(tk.N(3)),
                              wall=_rgb(tk.N(10) if tk.dark else tk.N(8)), obstacle=_rgb(OBSTACLE_COLOR),
                              door=_rgb("#a9794a"),
                              line=_rgb(INK), selected_wall=self.selected_wall, accent=_rgb(tk.accent_ring),
                              fog_time=self._fog_t, air=self.air,
                              sources=tuple((a.x, a.y, a.z) for a in self.site.aps if a.enabled))
            img = gl.render(self.width(), self.height(), fr.eye, self.cam.target, self.cam.fov, opts, scale)
        except Exception as e:  # noqa: BLE001
            self._drop_gl(f"{type(e).__name__}: {e}")
            return None
        if img is None or img.isNull():
            self._drop_gl("the renderer returned no image")
            return None
        self._gl_img, self._gl_key = img, key
        if not self._fog_timer.isActive():
            self._sync_fog_timer()                     # the renderer has just come up
        return img

    def closeEvent(self, ev):
        self._fog_timer.stop()
        self._drop_gl(self.gl_reason or "closed")
        self._gl_tried = False
        super().closeEvent(ev)

    # ---------------------------------------------------------------- painting
    def _frame(self) -> Frame:
        return Frame(self.cam, max(1, self.width()), max(1, self.height()))

    def paintEvent(self, _ev):
        tk = T.tokens()
        fr = self._frame()
        p = QPainter(self)
        img = self._gl_frame(tk)
        if img is not None:
            p.drawImage(QRectF(0, 0, self.width(), self.height()), img)
            p.setRenderHint(QPainter.Antialiasing)
        else:
            dpr = self.devicePixelRatioF()
            key = (self.cam.key(), self._rev, T.generation(), self.show_heat, self.ramp, self.width(),
                   self.height(), dpr)
            if key != self._static_key or self._static_pm is None:
                pm = QPixmap(int(self.width() * dpr), int(self.height() * dpr))
                pm.setDevicePixelRatio(dpr)
                pm.fill(tk.base)
                pp = QPainter(pm)
                self._paint_static(pp, fr, tk)
                pp.end()
                self._static_pm, self._static_key = pm, key
            p.drawPixmap(0, 0, self._static_pm)
            p.setRenderHint(QPainter.Antialiasing)
            self._paint_sorted(p, fr, tk, solids=True)
            self._paint_front_edges(p, fr, tk)
        self._paint_dimensions(p, fr, tk)
        self._paint_room_labels(p, fr, tk)
        if self.show_heat and self._drag is None:
            self._paint_contour_labels(p, fr, tk)
        now = self._clock.elapsed() / 1000.0
        self._paint_walk(p, fr, tk)
        if self.packets and self.drift:
            self._paint_packets(p, fr, now)
        if img is not None:
            self._paint_sorted(p, fr, tk, solids=False)
        self._paint_phone(p, fr, tk)
        self._paint_hover(p, fr, tk)
        self._paint_legend(p, tk)
        self._paint_hud(p, tk)
        p.end()

    def _outer_walls(self) -> list[tuple]:
        """(inward normal, a point on the wall, floor corner a, floor corner b)"""
        w, d = self.site.width, self.site.depth
        return [((0, 1, 0), (0, 0, 0), (0, 0), (w, 0)), ((0, -1, 0), (0, d, 0), (w, d), (0, d)),
                ((1, 0, 0), (0, 0, 0), (0, d), (0, 0)), ((-1, 0, 0), (w, 0, 0), (w, 0), (w, d))]

    def _is_back_wall(self, fr: Frame, wall) -> bool:
        n, pt = wall[0], wall[1]
        return sum((fr.eye[i] - pt[i]) * n[i] for i in range(3)) > 0     # the eye is on the room's side

    def contours(self) -> dict[float, list]:
        """Contour lines of the measuring plane in metres: level -> [((x0, y0), (x1, y1)), ...]."""
        if self._contours_rev != self._rev:
            s = self.site
            ny, nx = self._plane.shape
            cw, cd = s.width / nx, s.depth / ny
            self._contours = {
                lv: [(((a[0] + 0.5) * cw, (a[1] + 0.5) * cd), ((b[0] + 0.5) * cw, (b[1] + 0.5) * cd))
                     for a, b in wf.contour_segments(self._plane, lv)] for lv in CONTOURS}
            self._contours_rev = self._rev
        return self._contours

    def _paint_static(self, p: QPainter, fr: Frame, tk) -> None:
        """Software renderer, background layer (cached while the camera and the site stand still):
        back walls, floor, heat map, contour lines, metre grid."""
        s = self.site
        w, d, h = s.width, s.depth, s.height
        p.setRenderHint(QPainter.Antialiasing)
        edge = QPen(tk.N(8), 1.0)
        for wall in self._outer_walls():
            if self._is_back_wall(fr, wall):
                (ax, ay), (bx, by) = wall[2], wall[3]
                poly = fr.poly([(ax, ay, 0), (bx, by, 0), (bx, by, h), (ax, ay, h)])
                if poly is not None:
                    p.setPen(edge)
                    p.setBrush(T.alpha(tk.N(5), 0.55))
                    p.drawPolygon(poly)
        corners = [fr.proj(c) for c in ((0, 0, 0), (w, 0, 0), (w, d, 0), (0, d, 0))]
        floor = fr.poly([(0, 0, 0), (w, 0, 0), (w, d, 0), (0, d, 0)])
        if floor is not None:
            p.setPen(Qt.NoPen)
            p.setBrush(tk.N(3))
            p.drawPolygon(floor)
        if self.show_heat and None not in corners:
            img = heat_image(self._plane, self.ramp)
            src = QPolygonF([QPointF(0, 0), QPointF(img.width(), 0), QPointF(img.width(), img.height()),
                             QPointF(0, img.height())])
            warp = QTransform()
            if QTransform.quadToQuad(src, QPolygonF(corners), warp):
                p.save()
                p.setRenderHint(QPainter.SmoothPixmapTransform)
                p.setTransform(warp, True)
                p.drawImage(0, 0, img)
                p.restore()
            lines = [(a, b) for segs in self.contours().values() for (x0, y0), (x1, y1) in segs
                     for a, b in [(fr.proj((x0, y0, 0.0)), fr.proj((x1, y1, 0.0)))] if a is not None and b is not None]
            casing = QColor(255, 255, 255, 140)
            for pen in (QPen(casing, 3.6, Qt.SolidLine, Qt.RoundCap), QPen(QColor(INK), 1.4, Qt.SolidLine, Qt.RoundCap)):
                p.setPen(pen)
                for a, b in lines:
                    p.drawLine(a, b)
        step = 1.0 if max(w, d) <= 60 else 5.0 if max(w, d) <= 250 else 10.0
        grid = QColor(INK)
        grid.setAlphaF(0.10)
        p.setPen(QPen(grid, 1.0))
        x = step
        while x < w - 1e-6:
            a, b = fr.proj((x, 0, 0)), fr.proj((x, d, 0))
            if a is not None and b is not None:
                p.drawLine(a, b)
            x += step
        y = step
        while y < d - 1e-6:
            a, b = fr.proj((0, y, 0)), fr.proj((w, y, 0))
            if a is not None and b is not None:
                p.drawLine(a, b)
            y += step
        if floor is not None:
            p.setPen(QPen(tk.N(9), 1.2))
            p.setBrush(Qt.NoBrush)
            p.drawPolygon(floor)

    def _paint_dimensions(self, p: QPainter, fr: Frame, tk) -> None:
        s = self.site
        w, d, h = s.width, s.depth, s.height
        off = max(w, d) * 0.045
        y_edge = -off if fr.eye[1] < d / 2 else d + off
        x_edge = -off if fr.eye[0] < w / 2 else w + off
        marks = [((w / 2, y_edge, 0), f"{w:g} m"), ((x_edge, d / 2, 0), f"{d:g} m")]
        corners = [(0, 0), (w, 0), (w, d), (0, d)]
        left = min(corners, key=lambda c: (fr.proj((c[0], c[1], h / 2)) or QPointF(1e9, 0)).x())
        hp = fr.proj((left[0], left[1], h / 2))
        f = T.type("caption")
        p.setFont(f)
        p.setPen(tk.muted)
        fm_ = T.fm(f)
        for pt, text in marks:
            sp = fr.proj(pt)
            if sp is not None:
                tw = fm_.horizontalAdvance(text)
                p.drawText(QRectF(sp.x() - tw / 2 - 4, sp.y() - 9, tw + 8, 18), Qt.AlignCenter, text)
        if hp is not None:
            text = f"{h:g} m"
            tw = fm_.horizontalAdvance(text)
            p.drawText(QRectF(hp.x() - tw - 14, hp.y() - 9, tw + 8, 18), Qt.AlignCenter, text)

    def _paint_room_labels(self, p: QPainter, fr: Frame, tk) -> None:
        if len(self._rooms) < 2:
            return
        by_rect = {r.rects: lb for r, lb in self.site.named_rooms()} if self.site.labels else {}
        taken: list[QRectF] = []                       # largest rooms first; a label that would cover one is left out
        for n, room in enumerate(self._rooms, 1):
            lb = by_rect.get(room.rects)
            x, y = (lb.x, lb.y) if lb is not None else room.label_point
            sp = fr.proj((x, y, 0.0))
            if sp is None:
                continue
            if lb is not None:
                text = lb.name + (f" · {lb.name_he}" if lb.name_he else "")
                if lb.role != "outside":
                    text += f" · {room.area_m2:.0f} m²"
            else:
                text = f"Room {n} · {room.pct:.0f} %"
            w = T.pill_width(text)
            box = QRectF(sp.x() - w / 2 - 3, sp.y() - 11, w + 6, 22)
            if any(box.intersects(t) for t in taken):
                continue
            taken.append(box)
            T.draw_pill(p, sp, text, tk, text_color=tk.ink)

    def _paint_contour_labels(self, p: QPainter, fr: Frame, tk) -> None:
        """One dBm label per contour line, at the point of the line nearest the viewer."""
        used: list[QPointF] = []
        ex, ey = fr.eye[0], fr.eye[1]
        for level, segs in self.contours().items():
            if len(segs) < 6:                         # a speck, not a line worth naming
                continue
            (x0, y0), (x1, y1) = min(segs, key=lambda sg: (sg[0][0] - ex) ** 2 + (sg[0][1] - ey) ** 2)
            sp = fr.proj(((x0 + x1) / 2, (y0 + y1) / 2, 0.0))
            if sp is None or any(abs(sp.x() - u.x()) < 70 and abs(sp.y() - u.y()) < 18 for u in used):
                continue
            used.append(sp)
            T.draw_pill(p, sp, fmt_dbm(level), tk, text_color=tk.ink, bold=True)

    # ................................................................ walls, doors, obstacles, access points
    def _wall_pt(self, wall: ws.Wall, run: float, z: float) -> tuple:
        return (wall.a, run, z) if wall.axis == "x" else (run, wall.a, z)

    def _wall_items(self, fr: Frame, tk) -> list[tuple]:
        """Software renderer: (depth, draw callable) for every partition panel and door leaf."""
        s = self.site
        h = s.height
        items = []
        for wi, wall in enumerate(self._walls):
            cuts = sorted({wall.b0, wall.b1} | {o.a for o in self._walls
                                                if o.axis != wall.axis and wall.b0 < o.a < wall.b1})
            fill = T.alpha(tk.N(9), 0.30 + 0.40 * min(1.0, wall.loss[1] / 26.0))
            sel = wi == self.selected_wall
            pen = QPen(tk.accent_ring if sel else T.alpha(tk.N(12), 0.55), 2.0 if sel else 1.0)
            for s0, s1 in zip(cuts, cuts[1:]):
                pts = [(s0, 0.0)]
                if wall.door is not None:
                    h0, h1 = max(wall.d0, s0), min(wall.d1, s1)
                    if h1 - h0 > 1e-6:                 # the door is a notch cut up from the floor
                        pts += [(h0, 0.0), (h0, wall.door_h), (h1, wall.door_h), (h1, 0.0)]
                pts += [(s1, 0.0), (s1, h), (s0, h)]
                world = [self._wall_pt(wall, r, z) for r, z in pts]
                poly = fr.poly(world)
                if poly is None:
                    continue
                mid = self._wall_pt(wall, (s0 + s1) / 2, h / 2)
                items.append((fr.depth(mid), lambda p, poly=poly, fill=fill, pen=pen: self._draw_poly(
                    p, poly, fill, pen)))
            if wall.door is not None:
                dw = wall.d1 - wall.d0
                if wall.door.open:                    # the leaf stands open at 90 degrees, hinged at d0
                    side = s.width if wall.axis == "x" else s.depth
                    sw = dw if wall.a + dw <= side else -dw
                    a0, a1 = wall.a, wall.a + sw
                    if wall.axis == "x":
                        world = [(a0, wall.d0, 0), (a1, wall.d0, 0), (a1, wall.d0, wall.door_h),
                                 (a0, wall.d0, wall.door_h)]
                    else:
                        world = [(wall.d0, a0, 0), (wall.d0, a1, 0), (wall.d0, a1, wall.door_h),
                                 (wall.d0, a0, wall.door_h)]
                    fill_d, pen_d = T.alpha(QColor("#a9794a"), 0.45), QPen(T.alpha(tk.N(12), 0.6), 1.0, Qt.DashLine)
                else:
                    world = [self._wall_pt(wall, wall.d0, 0), self._wall_pt(wall, wall.d1, 0),
                             self._wall_pt(wall, wall.d1, wall.door_h), self._wall_pt(wall, wall.d0, wall.door_h)]
                    fill_d, pen_d = QColor("#a9794a"), QPen(tk.N(12), 1.0)
                poly = fr.poly(world)
                if poly is not None:
                    cen = tuple(sum(pt[i] for pt in world) / 4 for i in range(3))
                    items.append((fr.depth(cen) - 0.01, lambda p, poly=poly, fill=fill_d, pen=pen_d:
                                  self._draw_poly(p, poly, fill, pen)))
        return items

    def _obstacle_items(self, fr: Frame, tk) -> list[tuple]:
        """Software renderer: the visible faces of every obstacle box, shaded by which way they face."""
        items = []
        base = QColor(OBSTACLE_COLOR).lighter(125)
        pen = QPen(QColor(INK), 1.0)
        for o in self.site.obstacles:
            x0, y0, x1, y1, h = o.x, o.y, o.x + o.w, o.y + o.d, o.h
            faces = [([(x0, y0, h), (x1, y0, h), (x1, y1, h), (x0, y1, h)], 100, (0, 0, 1)),
                     ([(x0, y0, 0), (x1, y0, 0), (x1, y0, h), (x0, y0, h)], 118, (0, -1, 0)),
                     ([(x0, y1, 0), (x1, y1, 0), (x1, y1, h), (x0, y1, h)], 118, (0, 1, 0)),
                     ([(x0, y0, 0), (x0, y1, 0), (x0, y1, h), (x0, y0, h)], 135, (-1, 0, 0)),
                     ([(x1, y0, 0), (x1, y1, 0), (x1, y1, h), (x1, y0, h)], 135, (1, 0, 0))]
            for world, dark, n in faces:
                cen = tuple(sum(pt[i] for pt in world) / 4 for i in range(3))
                if sum((fr.eye[i] - cen[i]) * n[i] for i in range(3)) <= 0:      # facing away
                    continue
                poly = fr.poly(world)
                if poly is not None:
                    items.append((fr.depth(cen), lambda p, poly=poly, fill=base.darker(dark):
                                  self._draw_poly(p, poly, fill, pen)))
        return items

    @staticmethod
    def _draw_poly(p: QPainter, poly: QPolygonF, fill: QColor, pen: QPen) -> None:
        p.setPen(pen)
        p.setBrush(fill)
        p.drawPolygon(poly)

    def ap_radius(self, fr: Frame, ap: ws.AccessPoint) -> float:
        c = fr.cam((ap.x, ap.y, ap.z))
        return ws.clamp(fr.focal * 0.24 / max(c[2], NEAR), 8.0, 17.0)

    def _draw_ap(self, p: QPainter, fr: Frame, tk, idx: int) -> None:
        from .graphics import draw_glyph
        ap = self.site.aps[idx]
        top, foot = fr.proj((ap.x, ap.y, ap.z)), fr.proj((ap.x, ap.y, 0.0))
        if top is None:
            return
        sel = idx == self.selected
        if foot is not None:
            stem = QColor(INK)
            stem.setAlphaF(0.7)
            p.setPen(QPen(stem, 1.0, Qt.DashLine))
            p.drawLine(foot, top)
            ring = fr.poly([(ap.x + 0.22 * math.cos(t), ap.y + 0.22 * math.sin(t), 0.0)
                            for t in (i * math.pi / 8 for i in range(16))])
            if ring is not None:
                stem.setAlphaF(0.35)
                p.setPen(Qt.NoPen)
                p.setBrush(stem)
                p.drawPolygon(ring)
        r = self.ap_radius(fr, ap)
        if ap.enabled and self.drift:                        # the beacon: a flash as each wave leaves
            from .site3d_gl import WAVE_SPACING, WAVE_SPEED
            b = wa.beacon(self._clock.elapsed() / 1000.0, WAVE_SPACING / WAVE_SPEED)
            if b > 0.02:
                p.setPen(QPen(T.alpha(tk.accent_ring, 0.65 * b), 2.0))
                p.setBrush(T.alpha(tk.accent_ring, 0.22 * b))
                grow = r * (1.0 + 1.6 * (1.0 - b))
                p.drawEllipse(top, grow + 3, grow + 3)
        if sel:
            p.setPen(Qt.NoPen)
            p.setBrush(T.alpha(tk.accent_ring, 0.30))
            p.drawEllipse(top, r + 5, r + 5)
        p.setPen(QPen(tk.accent_ring if ap.enabled else tk.N(8), 2.0))
        p.setBrush(tk.card)
        p.drawEllipse(top, r, r)
        g = r * 1.25
        draw_glyph(p, QRectF(top.x() - g / 2, top.y() - g / 2 - r * 0.08, g, g), "ap",
                   QColor(tk.ink if ap.enabled else tk.muted))
        text = ap.name if ap.enabled else f"{ap.name} · off"
        if sel:
            text += f" · {ws.BAND_TITLES[ap.band]} · {ap.tx_dbm:g} dBm"
        T.draw_pill(p, QPointF(top.x(), top.y() - r - 12), text, tk, text_color=tk.ink, bold=sel,
                    border=tk.accent_ring if sel else None)

    def _paint_sorted(self, p: QPainter, fr: Frame, tk, solids: bool) -> None:
        """Access points, and for the software renderer (``solids``) also walls, doors and obstacles, far to
        near."""
        items = self._wall_items(fr, tk) + self._obstacle_items(fr, tk) if solids else []
        for i, ap in enumerate(self.site.aps):
            items.append((fr.depth((ap.x, ap.y, ap.z)), lambda p, i=i: self._draw_ap(p, fr, tk, i)))
        items.sort(key=lambda it: -it[0])
        for _d, draw in items:
            draw(p)

    def _paint_front_edges(self, p: QPainter, fr: Frame, tk) -> None:
        """Software renderer: the walls between the eye and the room are see-through outlines."""
        h = self.site.height
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(T.alpha(tk.N(10), 0.75), 1.0))
        for wall in self._outer_walls():
            if not self._is_back_wall(fr, wall):
                (ax, ay), (bx, by) = wall[2], wall[3]
                poly = fr.poly([(ax, ay, 0), (bx, by, 0), (bx, by, h), (ax, ay, h)])
                if poly is not None:
                    p.drawPolygon(poly)

    def _paint_packets(self, p: QPainter, fr: Frame, now: float) -> None:
        """Data packets between the serving access point and the phone (looks only; see wifi_anim)."""
        ph = self.phone()
        if ph is None:
            return
        _v, ap = self.site.best(ph, self._walls)
        if ap is None:
            return
        key = (id(ap), ap.x, ap.y, ap.z, ap.tx_dbm, ap.band, round(ph[0], 2), round(ph[1], 2), ph[2], self._rev)
        if key != self._flow_key:
            self._flow_key, self._flow = key, wa.Flow(self.site, ap, ph, self._walls)
        p.setPen(Qt.NoPen)
        for pk in self._flow.packets(now):
            sp = fr.proj((pk.x, pk.y, pk.z))
            if sp is None:
                continue
            c = ramp_color(self.ramp, pk.dbm)
            if pk.up:                                        # from the phone: small, with a light rim
                p.setBrush(T.alpha(QColor("#ffffff"), 0.55 * pk.alpha))
                p.drawEllipse(sp, 4.4, 4.4)
                c.setAlphaF(pk.alpha)
                p.setBrush(c)
                p.drawEllipse(sp, 2.8, 2.8)
            else:
                glow = QColor(c)
                glow.setAlphaF(0.30 * pk.alpha)
                p.setBrush(glow)
                p.drawEllipse(sp, 8.0, 8.0)
                c.setAlphaF(pk.alpha)
                p.setBrush(c)
                p.drawEllipse(sp, 4.0, 4.0)
                p.setBrush(T.alpha(QColor("#ffffff"), 0.75 * pk.alpha))
                p.drawEllipse(sp, 1.6, 1.6)

    def _paint_walk(self, p: QPainter, fr: Frame, tk) -> None:
        """The walk's path, and behind the phone a trail coloured by the signal it read (a survey line)."""
        pts = self.walk_path
        if not pts and not self.walk_drawing:
            return
        ink = QColor(INK) if not tk.dark or self.show_heat else QColor(tk.ink)
        floor = [fr.proj((x, y, 0.03)) for x, y in pts]
        if self.walk_drawing and self._hover is not None and pts:
            floor.append(fr.proj((self._hover[0], self._hover[1], 0.03)))
        pen = QPen(T.alpha(ink, 0.75), 1.6, Qt.DashLine)
        p.setPen(pen)
        for a, b in zip(floor, floor[1:]):
            if a is not None and b is not None:
                p.drawLine(a, b)
        if len(pts) >= 2:
            samples = self.walk_samples()
            done = [sm for sm in samples if sm.dist <= self.walk_dist + 1e-6]
            prev = None
            for sm in done:
                sp = fr.proj((sm.x, sm.y, 0.03))
                if sp is not None and prev is not None:
                    p.setPen(QPen(QColor(INK), 6.5, Qt.SolidLine, Qt.RoundCap))
                    p.drawLine(prev, sp)
                    p.setPen(QPen(ramp_color(self.ramp, sm.dbm), 4.5, Qt.SolidLine, Qt.RoundCap))
                    p.drawLine(prev, sp)
                prev = sp
            for sm in wa.roams(done):
                sp = fr.proj((sm.x, sm.y, 0.03))
                if sp is not None:
                    T.draw_pill(p, QPointF(sp.x(), sp.y() - 16), f"Roams to {sm.ap}", tk, text_color=tk.ink,
                                border=tk.accent_ring)
        p.setPen(QPen(tk.card, 1.5))
        p.setBrush(ink)
        for sp in floor[:len(pts)]:
            if sp is not None:
                p.drawEllipse(sp, 3.5, 3.5)

    def _paint_phone(self, p: QPainter, fr: Frame, tk) -> None:
        """The walking phone: a handset at the measuring height, its screen in the colour of its signal."""
        if len(self.walk_path) < 2 or not (self.walking or self.walk_dist > 0.0):
            return
        x, y = wa.walk_position(self.walk_path, self.walk_dist)
        z = self.site.slice_z
        top, foot = fr.proj((x, y, z)), fr.proj((x, y, 0.03))
        if top is None:
            return
        v, ap = self.site.best((x, y, z), self._walls)
        if foot is not None:
            p.setPen(QPen(T.alpha(QColor(INK), 0.7), 1.0, Qt.DashLine))
            p.drawLine(foot, top)
        body = QRectF(top.x() - 6.5, top.y() - 11.5, 13, 23)
        p.setPen(QPen(tk.card, 1.5))
        p.setBrush(QColor("#1b1d22"))
        p.drawRoundedRect(body, 3.0, 3.0)
        p.setPen(Qt.NoPen)
        p.setBrush(ramp_color(self.ramp, v) if ap is not None else tk.N(6))
        p.drawRoundedRect(body.adjusted(2.0, 3.0, -2.0, -4.0), 1.5, 1.5)
        text = f"{fmt_dbm(v)} · {ap.name}" if ap is not None else "No signal"
        T.draw_pill(p, QPointF(top.x(), top.y() - 24), text, tk, text_color=tk.ink, bold=True)

    def _paint_hover(self, p: QPainter, fr: Frame, tk) -> None:
        """The reading under the pointer: the straight path from the serving access point, everything it
        crosses, and the signal that arrives."""
        if self._hover is None or self._drag is not None:
            return
        s = self.site
        x, y = self._hover
        pt = (x, y, s.slice_z)
        v, ap = s.best(pt, self._walls)
        floor_pt, at = fr.proj((x, y, 0.0)), fr.proj(pt)
        if at is None or floor_pt is None:
            return
        ink = QColor(INK) if not tk.dark or self.show_heat else QColor(tk.ink)
        p.setPen(QPen(ink, 1.0))
        p.drawLine(floor_pt, at)
        if ap is not None:
            src = fr.proj((ap.x, ap.y, ap.z))
            if src is not None:
                p.setPen(QPen(ink, 1.3, Qt.DashLine))
                p.drawLine(src, at)
            for cpt, loss, what in s.crossings((ap.x, ap.y, ap.z), pt, ap.band, self._walls):
                sp = fr.proj(cpt)
                if sp is None:
                    continue
                p.setPen(QPen(tk.card, 1.5))
                p.setBrush(tk.ink)
                p.drawEllipse(sp, 4.0, 4.0)
                T.draw_pill(p, QPointF(sp.x(), sp.y() - 14),
                            f"{what} −{round(loss, 1):g} dB" if loss else what, tk, text_color=tk.ink)
        p.setPen(QPen(tk.card, 1.5))
        p.setBrush(tk.ink)
        p.drawEllipse(at, 3.5, 3.5)
        if ap is None:
            text = "No access point is transmitting"
        else:
            dist = math.dist((ap.x, ap.y, ap.z), pt)
            text = f"{fmt_dbm(v)} · {ws.band_title(v)} · from {ap.name}, {dist:.1f} m"
        w = T.pill_width(text, pt=8.5, bold=True, height=20.0, pad=8.0)
        cx = ws.clamp(self._hover_pos.x() + 14 + w / 2, w / 2 + 6, self.width() - w / 2 - 6)
        cy = ws.clamp(self._hover_pos.y() + 22, 16, self.height() - 40)
        T.draw_pill(p, QPointF(cx, cy), text, tk, text_color=tk.ink, pt=8.5, bold=True, height=20.0, pad=8.0)
        lk = wl.link(s, v, ap)
        if lk is not None:
            more = self.link_text(lk)
            w2 = T.pill_width(more, pt=8.0, height=18.0, pad=8.0)
            cx2 = ws.clamp(self._hover_pos.x() + 14 + w2 / 2, w2 / 2 + 6, self.width() - w2 / 2 - 6)
            T.draw_pill(p, QPointF(cx2, cy + 21), more, tk, text_color=tk.ink, pt=8.0, height=18.0, pad=8.0)

    @staticmethod
    def link_text(lk) -> str:
        """'SNR 31 dB · 288-649 Mbit/s · phone heard at −72 dBm (the phone limits) · ±5 dB'."""
        if lk.rate_hi <= 0.0:
            return f"No usable link · phone heard at {fmt_dbm(lk.up_dbm)} · ±{lk.spread_db:g} dB"
        up = f"phone heard at {fmt_dbm(lk.up_dbm)}" + (" (the phone limits)" if lk.uplink_limits else "")
        return f"SNR {lk.snr_db:.0f} dB · {lk.speed_text()} · {up} · ±{lk.spread_db:g} dB"

    def legend_rect(self) -> QRectF:
        h = 62 + 20 * (len(ws.SIGNAL_BANDS) + 1) + 30
        return QRectF(12, self.height() - h - 34, 286, h)      # bottom left: the corner the room leaves free

    def _paint_legend(self, p: QPainter, tk) -> None:
        """The key, on the picture itself: a colour bar, the bands with their share of the floor, and what
        kind of number this is."""
        r = self.legend_rect()
        if self.width() < r.width() + 160 or self.height() < r.height() + 60:
            return
        p.save()
        p.setPen(QPen(tk.card_border, 1.0))
        p.setBrush(T.alpha(tk.card, 0.94))
        p.drawRoundedRect(r, 10, 10)
        x, y, w = r.left() + 12, r.top() + 10, r.width() - 24
        p.setPen(tk.ink)
        p.setFont(T.type("label"))
        p.drawText(QRectF(x, y, w, 18), Qt.AlignLeft | Qt.AlignVCenter, "Wi-Fi signal strength")
        p.setFont(T.type("caption", weight=500))
        p.setPen(tk.muted)
        p.drawText(QRectF(x, y + 18, w, 16), Qt.AlignLeft | Qt.AlignVCenter,
                   f"Strongest access point, {self.site.slice_z:g} m above the floor")
        bar = QRectF(x, y + 40, w, 9)
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        p.drawImage(bar, ramp_image(self.ramp))
        p.setPen(QPen(tk.card_border, 1.0))
        p.setBrush(Qt.NoBrush)
        p.drawRect(bar)
        cols = band_colors(self.ramp) + [tk.N(3)]
        rows = [(t, f"{fmt_dbm(f)} or better") for f, t, _u in ws.SIGNAL_BANDS]
        rows.append((ws.NO_SERVICE, f"below {fmt_dbm(ws.SIGNAL_BANDS[-1][0])}"))
        f_name, f_num = T.type("caption"), T.tabular("caption")
        ry = bar.bottom() + 8
        for i, (name, limit) in enumerate(rows):
            p.setPen(QPen(tk.card_border, 1.0))
            p.setBrush(cols[i])
            p.drawRoundedRect(QRectF(x, ry + 4, 12, 12), 3, 3)
            p.setFont(f_name)
            p.setPen(tk.ink)
            p.drawText(QRectF(x + 20, ry, 76, 20), Qt.AlignLeft | Qt.AlignVCenter, name)
            p.setFont(f_num)
            p.setPen(tk.muted)
            p.drawText(QRectF(x + 96, ry, 118, 20), Qt.AlignLeft | Qt.AlignVCenter, limit)
            p.setPen(tk.ink)
            share = self.shares[i]
            p.drawText(QRectF(x + w - 46, ry, 46, 20), Qt.AlignRight | Qt.AlignVCenter,
                       "< 1 %" if 0 < share < 0.5 else f"{share:.0f} %")
            ry += 20
        p.setFont(T.type("caption", weight=500))
        p.setPen(tk.muted)
        p.drawText(QRectF(x, ry + 2, w, 30), Qt.AlignLeft | Qt.TextWordWrap,
                   f"% = share of the {'indoor ' if self.site.outside_rects() else ''}floor. Predicted "
                   f"±{ws.UNCERTAINTY_DB.get(self.site.clutter, 5.0):g} dB from distance, walls and obstacles: "
                   "an estimate, not a site survey.")
        p.restore()

    def _paint_hud(self, p: QPainter, tk) -> None:
        f = T.type("caption", weight=500)
        p.setFont(f)
        p.setPen(tk.muted)
        hint = "Drag: orbit · Wheel: zoom · Right-drag: pan · Drag an access point to move it · " \
               "Double-click: fit"
        if self._gl is None and self._gl_tried and self._want_gl:
            hint = f"No waves or fog in the air: no usable graphics card ({self.gl_reason}). " + hint
        p.drawText(QRectF(10, self.height() - 24, self.width() - 20, 18), Qt.AlignLeft | Qt.AlignVCenter,
                   T.fm(f).elidedText(hint, Qt.ElideRight, self.width() - 20))
        note = self.gpu_note()
        if note:
            p.drawText(QRectF(10, 8, self.width() - 20, 18), Qt.AlignRight | Qt.AlignVCenter,
                       T.fm(f).elidedText(note, Qt.ElideLeft, self.width() - 20))
        if not any(a.enabled for a in self.site.aps):
            p.setFont(T.type("label"))
            p.drawText(QRectF(0, 10, self.width(), 24), Qt.AlignCenter,
                       "No access point is transmitting – add one in the panel")

    # ---------------------------------------------------------------- interaction
    def ap_at(self, pos: QPointF) -> int:
        fr = self._frame()
        best, hit = 1e9, -1
        for i, ap in enumerate(self.site.aps):
            sp = fr.proj((ap.x, ap.y, ap.z))
            if sp is None:
                continue
            dist = math.hypot(sp.x() - pos.x(), sp.y() - pos.y())
            if dist <= self.ap_radius(fr, ap) + 4 and dist < best:
                best, hit = dist, i
        return hit

    def select_ap(self, idx: int) -> None:
        self.selected = idx if 0 <= idx < len(self.site.aps) else -1
        self.update()

    def select_wall(self, idx: int) -> None:
        self.selected_wall = idx
        self.update()

    def mousePressEvent(self, ev):
        pos = ev.position()
        if self.walk_drawing:
            if ev.button() == Qt.LeftButton:
                hit = self._frame().on_plane(pos.x(), pos.y(), 0.0)
                s = self.site
                if hit is not None and 0 <= hit[0] <= s.width and 0 <= hit[1] <= s.depth:
                    self.walk_path.append((hit[0], hit[1]))
                    self.walk_changed.emit()
            elif ev.button() == Qt.RightButton:
                self.finish_drawing_walk()
            self.update()
            return
        if ev.button() == Qt.LeftButton:
            hit = self.ap_at(pos)
            if hit >= 0:
                self.selected = hit
                self.ap_picked.emit(hit)
                self._drag = ("ap", pos)
            else:
                self._drag = ("orbit", pos)
        elif ev.button() in (Qt.RightButton, Qt.MiddleButton):
            self._drag = ("pan", pos)
        self.update()

    def mouseMoveEvent(self, ev):
        pos = ev.position()
        if self._drag is None:
            self._set_hover(pos)
            if not self.walk_drawing:
                self.setCursor(Qt.PointingHandCursor if self.ap_at(pos) >= 0 else Qt.ArrowCursor)
            return
        mode, last = self._drag
        dx, dy = pos.x() - last.x(), pos.y() - last.y()
        cam = self.cam
        if mode == "orbit":
            cam.yaw -= dx * 0.008
            cam.pitch = ws.clamp(cam.pitch + dy * 0.008, math.radians(6), math.radians(88))
        elif mode == "pan":
            fr = self._frame()
            k = cam.dist / fr.focal
            fh = math.hypot(fr.f[0], fr.f[1]) or 1e-9
            fwd = (fr.f[0] / fh, fr.f[1] / fh)
            cam.target = (cam.target[0] - fr.r[0] * dx * k + fwd[0] * dy * k,
                          cam.target[1] - fr.r[1] * dx * k + fwd[1] * dy * k, cam.target[2])
        elif mode == "ap" and 0 <= self.selected < len(self.site.aps):
            ap = self.site.aps[self.selected]
            hit = self._frame().on_plane(pos.x(), pos.y(), ap.z)
            if hit is not None:
                ap.x = ws.clamp(hit[0], 0.0, self.site.width)
                ap.y = ws.clamp(hit[1], 0.0, self.site.depth)
                self.site_changed()
                self.edited.emit()
        self._drag = (mode, pos)
        self.update()

    def mouseReleaseEvent(self, _ev):
        self._drag = None
        self.update()

    def mouseDoubleClickEvent(self, ev):
        if self.walk_drawing:
            self.finish_drawing_walk()
            return
        if self.ap_at(ev.position()) < 0:
            self.fit()

    def keyPressEvent(self, ev):
        if self.walk_drawing and ev.key() in (Qt.Key_Return, Qt.Key_Enter):
            self.finish_drawing_walk()
        elif self.walk_drawing and ev.key() == Qt.Key_Escape:
            self.walk_drawing = False
            self.setCursor(Qt.ArrowCursor)
            self.set_walk_path([])
        else:
            super().keyPressEvent(ev)

    def wheelEvent(self, ev):
        s = self.site
        diag = math.sqrt(s.width ** 2 + s.depth ** 2 + s.height ** 2)
        self.cam.dist = ws.clamp(self.cam.dist * 0.9 ** (ev.angleDelta().y() / 120.0), 0.8, diag * 12)
        self.update()

    def leaveEvent(self, _ev):
        if self._hover is not None:
            self._hover = None
            self.update()

    def _set_hover(self, pos: QPointF) -> None:
        hit = self._frame().on_plane(pos.x(), pos.y(), 0.0)
        s = self.site
        hover = (hit[0], hit[1]) if hit is not None and 0 <= hit[0] <= s.width and 0 <= hit[1] <= s.depth else None
        if hover != self._hover or hover is not None:
            self._hover, self._hover_pos = hover, pos
            self.update()


# ============================================================================ window: view + panel
def _spin(lo: float, hi: float, step: float, suffix: str, decimals: int = 1) -> QDoubleSpinBox:
    s = QDoubleSpinBox()
    s.setRange(lo, hi)
    s.setSingleStep(step)
    s.setDecimals(decimals)
    s.setSuffix(suffix)
    s.setKeyboardTracking(False)
    return s


def _combo(items) -> QComboBox:
    c = QComboBox()
    for key, title in items:
        c.addItem(title, key)
        c.setItemData(c.count() - 1, title, Qt.ToolTipRole)
    c.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)     # long names must not widen the panel
    c.setMinimumContentsLength(16)
    c.view().setMinimumWidth(c.view().sizeHintForColumn(0) + 24)              # the open list shows them whole
    return c


def _set_combo(c: QComboBox, key) -> None:
    i = c.findData(key)
    c.setCurrentIndex(i if i >= 0 else 0)


class SiteWindow(QWidget):
    """The 3D site view with its side panel.  ``save`` receives ``site.to_dict()`` and the view settings
    after every change."""

    def __init__(self, site: ws.Site, save=None, title: str = "", parent=None, gl: bool | None = None,
                 ui: dict | None = None):
        super().__init__(parent, Qt.Window)
        self.site = site
        self._save = save
        self._loading = False
        self.setWindowTitle("3D site – Wi-Fi heat map" + (f" – {title}" if title else ""))
        self.view = SiteView(site, gl=gl)
        ui = ui if isinstance(ui, dict) else {}
        self.view.set_look(heat=bool(ui.get("heat", True)), ramp=ui.get("ramp"), drift=bool(ui.get("drift", True)),
                           air=ui.get("air") if isinstance(ui.get("air"), str) else None,
                           glow=ui.get("glow") if isinstance(ui.get("glow"), (int, float)) else None)
        self.view.packets = bool(ui.get("packets", True))
        walk = ui.get("walk")
        if isinstance(walk, list):
            try:
                self.view.set_walk_path([(float(a), float(b)) for a, b in walk][:200])
            except (TypeError, ValueError):
                pass
        if isinstance(ui.get("walk_speed"), (int, float)) and 0.1 <= ui["walk_speed"] <= 50:
            self.view.walk_speed = float(ui["walk_speed"])
        self.view.edited.connect(self._view_edited)
        self.view.ap_picked.connect(self._ap_picked)
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(500)
        self._save_timer.timeout.connect(self.flush)

        panel = QWidget()
        pv = QVBoxLayout(panel)
        pv.setContentsMargins(12, 12, 12, 12)
        pv.setSpacing(12)
        for box in (self._room_box(), self._view_box(), self._walk_box(), self._space_box(), self._walls_box(),
                    self._obstacles_box(), self._aps_box()):
            pv.addWidget(box)
        pv.addStretch(1)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setWidget(panel)
        scroll.setFixedWidth(404)
        scroll.setFrameShape(QScrollArea.NoFrame)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        lay.addWidget(self.view, 1)
        lay.addWidget(scroll)
        self.resize(1280, 800)
        self.reload()

    def ui_state(self) -> dict:
        return {"heat": self.view.show_heat, "glow": self.view.glow, "ramp": self.view.ramp, "drift": self.view.drift,
                "air": self.view.air, "packets": self.view.packets,
                "walk": [[round(x, 3), round(y, 3)] for x, y in self.view.walk_path], "walk_speed": self.view.walk_speed}

    # ---------------------------------------------------------------- panel sections
    def _packets_toggled(self, on: bool) -> None:
        self.view.packets = bool(on)
        self.view.update()
        self._save_timer.start()

    def _walk_box(self) -> QGroupBox:
        box = QGroupBox("Phone walk")
        v = QVBoxLayout(box)
        row = QHBoxLayout()
        self.k_draw = QPushButton("Draw a path")
        self.k_draw.setCheckable(True)
        self.k_draw.setToolTip("Click points on the floor; double-click, Enter or right-click to finish (Esc cancels)")
        self.k_walk = QPushButton("Walk")
        self.k_walk.setCheckable(True)
        self.k_clear = QPushButton("Clear")
        for b_ in (self.k_draw, self.k_walk, self.k_clear):
            row.addWidget(b_)
        row.addStretch(1)
        speed_row = QHBoxLayout()
        speed_row.addWidget(QLabel("Pace"))
        self.k_speed = _combo([(1.4, "Walking (1.4 m/s)"), (4.2, "3 x"), (14.0, "10 x")])
        speed_row.addWidget(self.k_speed, 1)
        self.k_status = QLabel()
        self.k_status.setWordWrap(True)
        v.addLayout(row)
        v.addLayout(speed_row)
        v.addWidget(self.k_status)
        self.k_draw.toggled.connect(self._walk_draw_toggled)
        self.k_walk.toggled.connect(self._walk_play_toggled)
        self.k_clear.clicked.connect(lambda: (self.view.set_walk_path([]), self._save_timer.start()))
        self.k_speed.currentIndexChanged.connect(self._walk_speed_changed)
        self.view.walk_changed.connect(self._walk_changed)
        i = min(range(self.k_speed.count()), key=lambda k: abs(self.k_speed.itemData(k) - self.view.walk_speed))
        self.k_speed.setCurrentIndex(i)
        self._walk_changed()
        return box

    def _walk_draw_toggled(self, on: bool) -> None:
        if on and not self.view.walk_drawing:
            self.view.start_drawing_walk()
            self.view.setFocus()
        elif not on and self.view.walk_drawing:
            self.view.finish_drawing_walk()

    def _walk_play_toggled(self, on: bool) -> None:
        if on != self.view.walking:
            self.view.set_walking(on)

    def _walk_speed_changed(self) -> None:
        self.view.walk_speed = float(self.k_speed.currentData())
        self._save_timer.start()

    def _walk_changed(self) -> None:
        v = self.view
        for btn, state in ((self.k_draw, v.walk_drawing), (self.k_walk, v.walking)):
            if btn.isChecked() != state:
                btn.blockSignals(True)
                btn.setChecked(state)
                btn.blockSignals(False)
        self.k_walk.setEnabled(len(v.walk_path) >= 2 and not v.walk_drawing)
        self.k_clear.setEnabled(bool(v.walk_path))
        self.k_status.setText(v.walk_status())
        if not v.walking:
            self._save_timer.start()

    def _room_box(self) -> QGroupBox:
        box = QGroupBox("Room")
        v = QVBoxLayout(box)
        self.r_title = QLabel()
        self.r_title.setWordWrap(True)
        self.r_title.setFont(T.type("label"))
        self.r_title_he = QLabel()
        self.r_title_he.setWordWrap(True)
        self.r_title_he.setAlignment(Qt.AlignRight)
        self.r_title_he.setLayoutDirection(Qt.RightToLeft)
        self.r_source = QLabel()
        self.r_source.setWordWrap(True)
        self.r_source.setOpenExternalLinks(True)
        self.r_source.setTextInteractionFlags(Qt.TextBrowserInteraction)
        self.r_source.setObjectName("muted")
        self.problems_lbl = QLabel()
        self.problems_lbl.setWordWrap(True)
        self.problems_lbl.setStyleSheet(f"color: {T.tokens().text('warn').name()}")
        self.problems_lbl.setVisible(False)
        row = QHBoxLayout()
        self.r_library = QPushButton("Room library…")
        self.r_library.setToolTip("Start from one of the ready rooms (Israeli homes and workplaces drawn from "
                                  "published plans) or open a room file")
        self.r_save = QPushButton("Save as file…")
        self.r_save.setToolTip("Save this room as a file you can open again or share")
        row.addWidget(self.r_library)
        row.addWidget(self.r_save)
        row.addStretch(1)
        v.addWidget(self.r_title)
        v.addWidget(self.r_title_he)
        v.addWidget(self.r_source)
        v.addLayout(row)
        v.addWidget(self.problems_lbl)
        self.r_library.clicked.connect(self.open_library)
        self.r_save.clicked.connect(self.save_as)
        return box

    def _show_room_meta(self) -> None:
        m = self.site.meta
        title = str(m.get("title") or "") or "Your own room"
        self.r_title.setText(title)
        he = str(m.get("title_he") or "")
        self.r_title_he.setText(he)
        self.r_title_he.setVisible(bool(he))
        src = m.get("source") if isinstance(m.get("source"), dict) else {}
        url = str(src.get("url") or "")
        city = str(m.get("city") or "")
        text = ""
        if url.startswith(("http://", "https://")):
            from .room_library import _esc
            text = f'Drawn from <a href="{_esc(url)}">the published plan</a>' + (f" · {_esc(city)}" if city else "")
        elif city:
            text = city
        self.r_source.setText(text)
        self.r_source.setToolTip("\n".join(str(v) for v in (src.get("title"), m.get("era"), src.get("license"))
                                           if v))
        self.r_source.setVisible(bool(text))

    def load_site(self, new: ws.Site) -> None:
        """Replace the room by ``new`` (a library room or a file) and save it as this window's site."""
        from .room_library import load_into
        load_into(self.site, new)
        self.site.normalize()
        self.view.selected = self.view.selected_wall = -1
        self.reload()
        self.view.fit()
        self._save_timer.start()

    def open_library(self) -> None:
        from .room_library import RoomLibraryDialog
        dlg = RoomLibraryDialog(parent=self)
        if dlg.exec() and dlg.chosen() is not None:
            self.load_site(dlg.chosen().site)

    def save_as(self) -> None:
        from .room_library import edited_copy_meta, slug, user_folder, write_room
        folder = user_folder()
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError:
            folder = Path.home()
        name = slug(self.site.title or "room") + ".json"
        fn, _ = QFileDialog.getSaveFileName(self, "Save the room", str(folder / name),
                                            "LabDesk room (*.json)")
        if not fn:
            return
        site = ws.Site.from_dict(self.site.to_dict())
        if site.meta.get("source") and not str(site.meta.get("title", "")).endswith("(edited)"):
            site.meta = edited_copy_meta(site.meta)
        try:
            write_room(fn, site)
        except OSError as e:
            self.problems_lbl.setText(f"Could not save: {e}")
            self.problems_lbl.setVisible(True)

    def _view_box(self) -> QGroupBox:
        box = QGroupBox("Heat map")
        f = QFormLayout(box)
        self.v_heat = QCheckBox("Heat map on the floor")
        self.v_heat.setChecked(self.view.show_heat)
        self.v_ramp = _combo([(k, t) for k, (t, _s) in RAMPS.items()])
        _set_combo(self.v_ramp, self.view.ramp)
        self.v_glow = QSlider(Qt.Horizontal)
        self.v_glow.setRange(0, 100)
        self.v_glow.setValue(int(round(self.view.glow * 100)))
        self.v_glow.setToolTip("How strongly the signal shows in the air (0 = floor only). The colour is the signal\n"
                               "where it is; the waves' pace and the fog's billows are only for looks.\n"
                               "Needs a graphics card with OpenGL 3.3.")
        self.v_air = _combo(list(AIR_MODES.items()))
        _set_combo(self.v_air, self.view.air)
        self.v_air.setToolTip("Waves: fronts leave every access point in all directions and fade as the signal\n"
                              "weakens, so you see where walls and machines take it away.\nFog: a haze as thick as "
                              "the signal is strong.")
        self.v_drift = QCheckBox("Moving")
        self.v_drift.setChecked(self.view.drift)
        self.v_drift.setToolTip("Let the waves travel out / the fog drift (redraws the view 20 to 30 times a second).\n"
                                "Real Wi-Fi travels at the speed of light: the pace is slowed down to be seen.")
        self.v_slice = _spin(0, ws.MAX_HEIGHT, 0.1, " m", 2)
        self.v_slice.setToolTip("Height of the device that receives: 1 m is a device on a bench")
        self.v_clutter = _combo([(k, t) for k, (t, _n) in ws.CLUTTER.items()])
        f.addRow(self.v_heat)
        f.addRow("Colours", self.v_ramp)
        f.addRow("In the air", self.v_air)
        f.addRow("Strength", self.v_glow)
        f.addRow("", self.v_drift)
        self.v_packets = QCheckBox("Data packets to the phone")
        self.v_packets.setChecked(self.view.packets)
        self.v_packets.setToolTip("Packets stream between the serving access point and the phone (the walking phone,\n"
                                  "or the spot under the pointer). Their pace follows the expected speed; they thin\n"
                                  "out through walls; fewer come back when the phone's weaker signal limits the link.\n"
                                  "Looks only: real Wi-Fi is instant.")
        f.addRow("", self.v_packets)
        f.addRow("Measured at", self.v_slice)
        f.addRow("Environment", self.v_clutter)
        self.v_heat.toggled.connect(lambda on: self._look_edited(heat=on))
        self.v_ramp.currentIndexChanged.connect(lambda _i: self._look_edited(ramp=self.v_ramp.currentData()))
        self.v_glow.valueChanged.connect(lambda val: self._look_edited(glow=val / 100.0))
        self.v_drift.toggled.connect(lambda on: self._look_edited(drift=on))
        self.v_packets.toggled.connect(self._packets_toggled)
        self.v_air.currentIndexChanged.connect(lambda _i: self._look_edited(air=self.v_air.currentData()))
        self.v_slice.valueChanged.connect(self._view_edited_panel)
        self.v_clutter.currentIndexChanged.connect(self._view_edited_panel)
        return box

    def _space_box(self) -> QGroupBox:
        box = QGroupBox("Space")
        f = QFormLayout(box)
        self.sp_w = _spin(ws.MIN_SIDE, ws.MAX_SIDE, 0.5, " m")
        self.sp_d = _spin(ws.MIN_SIDE, ws.MAX_SIDE, 0.5, " m")
        self.sp_h = _spin(ws.MIN_HEIGHT, ws.MAX_HEIGHT, 0.1, " m")
        f.addRow("Width", self.sp_w)
        f.addRow("Depth", self.sp_d)
        f.addRow("Height", self.sp_h)
        self.rooms_lbl = QLabel()
        self.rooms_lbl.setWordWrap(True)
        f.addRow("Rooms", self.rooms_lbl)
        for s in (self.sp_w, self.sp_d, self.sp_h):
            s.valueChanged.connect(self._space_edited)
        return box

    def _list_box(self, title: str, add_text: str, on_select, on_add, on_del) -> tuple:
        box = QGroupBox(title)
        v = QVBoxLayout(box)
        lst = QListWidget()
        lst.setMinimumHeight(76)
        lst.setMaximumHeight(190)
        lst.currentRowChanged.connect(on_select)
        v.addWidget(lst)
        row = QHBoxLayout()
        add = QPushButton(add_text)
        add.clicked.connect(on_add)
        rem = QPushButton("Remove")
        rem.clicked.connect(on_del)
        row.addWidget(add)
        row.addWidget(rem)
        row.addStretch(1)
        v.addLayout(row)
        form = QWidget()
        f = QFormLayout(form)
        f.setContentsMargins(0, 0, 0, 0)
        v.addWidget(form)
        return box, lst, rem, form, f

    def _walls_box(self) -> QGroupBox:
        box, self.wall_list, self.wall_del, self.wall_form, f = self._list_box(
            "Dividing walls", "Add wall", self._wall_selected, self._add_wall, self._del_wall)
        self.w_axis = _combo([("x", "Splits the width (left / right)"), ("y", "Splits the depth (front / back)")])
        self.w_at = _spin(1, 99, 1, " %", 0)
        self.w_slider = QSlider(Qt.Horizontal)
        self.w_slider.setRange(1, 99)
        self.w_at_m = QLabel()
        at_row = QHBoxLayout()
        at_row.addWidget(self.w_slider, 1)
        at_row.addWidget(self.w_at)
        self.w_from = _spin(0, 100, 5, " %", 0)
        self.w_to = _spin(0, 100, 5, " %", 0)
        run_row = QHBoxLayout()
        run_row.addWidget(self.w_from)
        run_row.addWidget(QLabel("to"))
        run_row.addWidget(self.w_to)
        run_row.addStretch(1)
        self.w_mat = _combo([(k, f"{m.title} · {m.title_he}") for k, m in ws.MATERIALS.items()])
        self.w_loss = _spin(0, ws.MAX_WALL_DB, 1, " dB", 1)
        self.w_loss.setToolTip("What this wall takes away at 5 GHz. It starts at a typical value for the\n"
                               "material; set it to what you measured or know for this wall.")
        self.w_loss_hint = QLabel()
        self.w_loss_hint.setWordWrap(True)
        self.w_door = _combo([("none", "No door"), ("open", "Door, open"), ("closed", "Door, closed")])
        self.w_door_at = _spin(0, 100, 5, " %", 0)
        self.w_door_w = _spin(0.5, 6.0, 0.1, " m")
        self.w_outer = QCheckBox("Outer wall (the building's shell)")
        self.w_outer.setToolTip("An outer wall may stand on the edge of the box and carries the facade's windows.\n"
                                "On the edge it is drawn but changes nothing inside (the field is computed\n"
                                "inside the box only).")
        # every door and window of the wall
        self.op_list = QListWidget()
        self.op_list.setMinimumHeight(60)
        self.op_list.setMaximumHeight(130)
        self.op_add_door = QPushButton("Add door")
        self.op_add_win = QPushButton("Add window")
        self.op_del = QPushButton("Remove")
        op_row = QHBoxLayout()
        for b_ in (self.op_add_door, self.op_add_win, self.op_del):
            op_row.addWidget(b_)
        op_row.addStretch(1)
        self.op_form = QWidget()
        of = QFormLayout(self.op_form)
        of.setContentsMargins(0, 0, 0, 0)
        self.op_fill = _combo([(k, f"{m.title} · {m.title_he}") for k, m in ws.FILLS.items()])
        self.op_open = QCheckBox("Open")
        self.op_open.setToolTip("The door leaf or window sash stands open: no loss from the fill (a trisim still counts)")
        self.op_shutter = _combo([(k, m.title) for k, m in ws.SHUTTERS.items()])
        self.op_at = _spin(0, 100, 1, " %", 0)
        self.op_w = _spin(0.2, 50.0, 0.1, " m")
        self.op_sill = _spin(0.0, ws.MAX_HEIGHT, 0.1, " m")
        self.op_h = _spin(0.2, ws.MAX_HEIGHT, 0.1, " m")
        self.op_loss = QLabel()
        self.op_loss.setObjectName("muted")
        of.addRow("Fill", self.op_fill)
        of.addRow("", self.op_open)
        of.addRow("Trisim", self.op_shutter)
        of.addRow("Position", self.op_at)
        of.addRow("Width", self.op_w)
        of.addRow("Sill", self.op_sill)
        of.addRow("Height", self.op_h)
        of.addRow("", self.op_loss)
        f.addRow("Direction", self.w_axis)
        f.addRow("Position", at_row)
        f.addRow("", self.w_at_m)
        f.addRow("Runs from", run_row)
        f.addRow("Material", self.w_mat)
        f.addRow("Loss at 5 GHz", self.w_loss)
        f.addRow("", self.w_loss_hint)
        f.addRow("", self.w_outer)
        f.addRow("Door", self.w_door)
        f.addRow("Door position", self.w_door_at)
        f.addRow("Door width", self.w_door_w)
        f.addRow(QLabel("Doors and windows in this wall"))
        f.addRow(self.op_list)
        f.addRow(op_row)
        f.addRow(self.op_form)
        self.op_list.currentRowChanged.connect(self._op_selected)
        self.op_add_door.clicked.connect(lambda: self._add_op("door"))
        self.op_add_win.clicked.connect(lambda: self._add_op("window"))
        self.op_del.clicked.connect(self._del_op)
        for w in (self.op_at, self.op_w, self.op_sill, self.op_h):
            w.valueChanged.connect(self._op_edited)
        for c in (self.op_fill, self.op_shutter):
            c.currentIndexChanged.connect(self._op_edited)
        self.op_open.toggled.connect(self._op_edited)
        self.w_outer.toggled.connect(self._wall_edited)
        self.w_slider.valueChanged.connect(lambda val: self.w_at.setValue(val))
        self.w_at.valueChanged.connect(lambda val: self.w_slider.setValue(int(val)))
        for w in (self.w_at, self.w_from, self.w_to, self.w_door_at, self.w_door_w, self.w_loss):
            w.valueChanged.connect(self._wall_edited)
        for c in (self.w_axis, self.w_door):
            c.currentIndexChanged.connect(self._wall_edited)
        self.w_mat.currentIndexChanged.connect(self._wall_material_changed)
        return box

    def _obstacles_box(self) -> QGroupBox:
        box, self.obs_list, self.obs_del, self.obs_form, f = self._list_box(
            "Furniture, machines and racking", "Add item", self._obs_selected, self._add_obs, self._del_obs)
        self.o_kind = _combo([(k, f"{m.title} · {m.title_he}") for k, m in ws.OBSTACLE_KINDS.items()])
        self.o_x = _spin(0, ws.MAX_SIDE, 0.25, " m", 2)
        self.o_y = _spin(0, ws.MAX_SIDE, 0.25, " m", 2)
        self.o_w = _spin(0.2, ws.MAX_SIDE, 0.25, " m", 2)
        self.o_d = _spin(0.2, ws.MAX_SIDE, 0.25, " m", 2)
        self.o_h = _spin(0.2, ws.MAX_HEIGHT, 0.1, " m", 2)
        self.o_loss = _spin(0, 400, 0.5, " dB/m", 1)
        self.o_loss.setToolTip("What each metre of path through it takes away at 5 GHz (one obstacle never\n"
                               f"takes more than {ws.OBSTACLE_MAX_LOSS:g} dB: the signal also bends round it).")
        f.addRow("Kind", self.o_kind)
        f.addRow("From the left", self.o_x)
        f.addRow("From the front", self.o_y)
        f.addRow("Size along width", self.o_w)
        f.addRow("Size along depth", self.o_d)
        f.addRow("Height", self.o_h)
        self.o_z0 = _spin(0.0, ws.MAX_HEIGHT, 0.1, " m", 2)
        self.o_z0.setToolTip("How high its bottom is above the floor (upper cabinets, a TV on the wall)")
        f.addRow("Raised by", self.o_z0)
        self.o_hint = QLabel()
        self.o_hint.setObjectName("muted")
        self.o_hint.setWordWrap(True)
        f.addRow("Loss at 5 GHz", self.o_loss)
        f.addRow("", self.o_hint)
        for w in (self.o_x, self.o_y, self.o_w, self.o_d, self.o_h, self.o_loss, self.o_z0):
            w.valueChanged.connect(self._obs_edited)
        self.o_kind.currentIndexChanged.connect(self._obs_kind_changed)
        return box

    def _aps_box(self) -> QGroupBox:
        box, self.ap_list, self.ap_del, self.ap_form, f = self._list_box(
            "Access points", "Add access point", self._ap_selected, self._add_ap, self._del_ap)
        self.a_x = _spin(0, ws.MAX_SIDE, 0.25, " m", 2)
        self.a_y = _spin(0, ws.MAX_SIDE, 0.25, " m", 2)
        self.a_z = _spin(0, ws.MAX_HEIGHT, 0.1, " m", 2)
        self.a_pwr = _spin(0, 36, 1, " dBm", 0)
        self.a_pwr.setToolTip("EIRP: transmit power plus antenna gain. Israel allows 20 dBm at 2.4 GHz, 23 dBm on\n"
                              "5 GHz channels 36-48 (27 dBm on 100-140 with DFS) and 23 dBm at 6 GHz (indoors).")
        self.a_band = _combo(list(ws.BAND_TITLES.items()))
        self.a_on = QCheckBox("Transmitting")
        f.addRow("From the left", self.a_x)
        f.addRow("From the front", self.a_y)
        f.addRow("Mounting height", self.a_z)
        f.addRow("Power", self.a_pwr)
        f.addRow("Band", self.a_band)
        f.addRow("", self.a_on)
        for w in (self.a_x, self.a_y, self.a_z, self.a_pwr):
            w.valueChanged.connect(self._ap_edited)
        self.a_band.currentIndexChanged.connect(self._ap_edited)
        self.a_on.toggled.connect(self._ap_edited)
        return box

    # ---------------------------------------------------------------- site -> panel
    def reload(self) -> None:
        """Fill the whole panel from the site (after loading, or after the lab's access points changed)."""
        s = self.site
        self._loading = True
        try:
            self.sp_w.setValue(s.width)
            self.sp_d.setValue(s.depth)
            self.sp_h.setValue(s.height)
            self.v_slice.setMaximum(s.height)
            self.v_slice.setValue(s.slice_z)
            _set_combo(self.v_clutter, s.clutter)
        finally:
            self._loading = False
        self._fill_lists()
        self._show_room_meta()
        self._refresh()

    @staticmethod
    def wall_text(n: int, p: ws.Partition) -> str:
        what = "width" if p.axis == "x" else "depth"
        doors = [o for o in p.openings if o.kind == "door"]
        wins = len(p.openings) - len(doors)
        if len(doors) == 1 and not wins:
            extra = " · door open" if doors[0].open else " · door closed"
        else:
            bits = ([f"{len(doors)} door" + ("s" if len(doors) > 1 else "")] if doors else []) + \
                   ([f"{wins} window" + ("s" if wins > 1 else "")] if wins else [])
            extra = (" · " + ", ".join(bits)) if bits else ""
        name = f"{p.name} · " if p.name else ""
        outer = "outer, " if p.role == "outer" else ""
        return (f"Wall {n} · {name}{outer}{what} at {p.at_pct:.0f} % · {ws.MATERIALS[p.material].title} "
                f"{p.losses()[1]:g} dB{extra}")

    @staticmethod
    def obs_text(o: ws.Obstacle) -> str:
        return f"{o.name} · {ws.OBSTACLE_KINDS[o.kind].title} · {o.w:g} x {o.d:g} x {o.h:g} m"

    @staticmethod
    def ap_text(a: ws.AccessPoint) -> str:
        return (f"{a.name} · {ws.BAND_TITLES[a.band]} · {a.tx_dbm:g} dBm"
                + ("" if a.enabled else " · off") + (" · from the lab" if a.from_lab else ""))

    def _fill_lists(self) -> None:
        self._loading = True
        try:
            for lst, texts in ((self.wall_list, [self.wall_text(n, p) for n, p in
                                                 enumerate(self.site.partitions, 1)]),
                               (self.obs_list, [self.obs_text(o) for o in self.site.obstacles]),
                               (self.ap_list, [self.ap_text(a) for a in self.site.aps])):
                row = lst.currentRow()
                lst.clear()
                lst.addItems(texts)
                lst.setCurrentRow(min(max(row, 0), lst.count() - 1))
        finally:
            self._loading = False
        self._wall_selected(self.wall_list.currentRow())
        self._obs_selected(self.obs_list.currentRow())
        self._ap_selected(self.ap_list.currentRow())

    def _refresh(self, save: bool = False) -> None:
        """After any change: recompute the view, the list texts and the rooms."""
        self.site.normalize()
        self.view.site_changed()
        for lst, texts in ((self.wall_list, [self.wall_text(n, p) for n, p in enumerate(self.site.partitions, 1)]),
                           (self.obs_list, [self.obs_text(o) for o in self.site.obstacles]),
                           (self.ap_list, [self.ap_text(a) for a in self.site.aps])):
            for i, text in enumerate(texts):
                it = lst.item(i)
                if it is not None:
                    it.setText(text)
        rooms = self.view.rooms
        if len(rooms) == 1:
            self.rooms_lbl.setText(f"One open space · {rooms[0].area_m2:.1f} m²")
        else:
            names = {r.rects: lb for r, lb in self.site.named_rooms()} if self.site.labels else {}
            self.rooms_lbl.setText("\n".join(
                (f"{names[r.rects].name} · {r.area_m2:.1f} m²" if names.get(r.rects) is not None
                 else f"Room {n} · {r.pct:.0f} % · {r.area_m2:.1f} m²") for n, r in enumerate(rooms, 1)))
        problems = self.site.problems()
        self.problems_lbl.setText("\n".join(problems))
        self.problems_lbl.setVisible(bool(problems))
        if save:
            self._save_timer.start()

    def flush(self) -> None:
        self._save_timer.stop()
        if self._save is not None:
            self._save(self.site.to_dict(), self.ui_state())

    def closeEvent(self, ev):
        self.flush()
        self.view.close()
        super().closeEvent(ev)

    # ---------------------------------------------------------------- panel -> site
    def _look_edited(self, **kw) -> None:
        self.view.set_look(**kw)
        self._save_timer.start()

    def _space_edited(self) -> None:
        if self._loading:
            return
        s = self.site
        s.width, s.depth, s.height = self.sp_w.value(), self.sp_d.value(), self.sp_h.value()
        s.normalize()
        self._loading = True
        self.v_slice.setMaximum(s.height)
        self.v_slice.setValue(s.slice_z)
        self._loading = False
        self._ap_selected(self.ap_list.currentRow())
        self._obs_selected(self.obs_list.currentRow())
        self._wall_selected(self.wall_list.currentRow())
        self._refresh(save=True)
        self.view.fit()

    def _view_edited_panel(self) -> None:
        if self._loading:
            return
        self.site.slice_z = self.v_slice.value()
        self.site.clutter = self.v_clutter.currentData()
        self._refresh(save=True)

    # .......................................................... walls
    def _wall_selected(self, row: int) -> None:
        ok = 0 <= row < len(self.site.partitions)
        self.wall_form.setEnabled(ok)
        self.wall_del.setEnabled(ok)
        self.view.select_wall(row if ok else -1)
        if not ok:
            self.w_at_m.setText("")
            self.w_loss_hint.setText("")
            return
        p = self.site.partitions[row]
        self._loading = True
        try:
            _set_combo(self.w_axis, p.axis)
            self.w_at.setValue(p.at_pct)
            self.w_slider.setValue(int(round(p.at_pct)))
            self.w_from.setValue(p.start_pct)
            self.w_to.setValue(p.end_pct)
            _set_combo(self.w_mat, p.material)
            self.w_loss.setValue(p.losses()[1])
            _set_combo(self.w_door, "none" if p.door is None else "open" if p.door.open else "closed")
            self.w_door_at.setValue(p.door.at_pct if p.door else 50)
            self.w_door_w.setValue(p.door.width_m if p.door else 0.9)
            self.w_door_at.setEnabled(p.door is not None)
            self.w_door_w.setEnabled(p.door is not None)
            self.w_outer.setChecked(p.role == "outer")
            edge = p.role == "outer"
            self.w_at.setRange(0 if edge else 1, 100 if edge else 99)
            self.w_slider.setRange(0 if edge else 1, 100 if edge else 99)
            self.w_at.setValue(p.at_pct)
        finally:
            self._loading = False
        self._wall_hint(p)
        self._fill_ops()

    # .......................................................... doors and windows of the selected wall
    @staticmethod
    def op_text(o: ws.Opening) -> str:
        size = f"{o.width_m:g} x {o.height_m:g} m" + (f", sill {o.sill_m:g} m" if o.sill_m else "")
        return f"{o.title()} · at {o.at_pct:.0f} % · {size} · {o.losses()[1]:g} dB"

    def _cur_wall(self) -> ws.Partition | None:
        row = self.wall_list.currentRow()
        return self.site.partitions[row] if 0 <= row < len(self.site.partitions) else None

    def _fill_ops(self) -> None:
        p = self._cur_wall()
        self._loading = True
        try:
            row = self.op_list.currentRow()
            self.op_list.clear()
            if p is not None:
                self.op_list.addItems([self.op_text(o) for o in p.openings])
                self.op_list.setCurrentRow(min(max(row, 0), self.op_list.count() - 1))
        finally:
            self._loading = False
        self._op_selected(self.op_list.currentRow())

    def _op_selected(self, row: int) -> None:
        p = self._cur_wall()
        ok = p is not None and 0 <= row < len(p.openings)
        self.op_form.setEnabled(ok)
        self.op_del.setEnabled(ok)
        if not ok or self._loading:
            return
        o = p.openings[row]
        self._loading = True
        try:
            _set_combo(self.op_fill, o.fill)
            self.op_open.setChecked(o.open)
            _set_combo(self.op_shutter, o.shutter)
            self.op_at.setValue(o.at_pct)
            self.op_w.setValue(o.width_m)
            self.op_sill.setValue(o.sill_m)
            self.op_h.setValue(o.height_m)
        finally:
            self._loading = False
        self._op_hint(o)

    def _op_hint(self, o: ws.Opening) -> None:
        l24, l5, l6 = o.losses()
        self.op_loss.setText(f"Takes away {l24:g} / {l5:g} / {l6:g} dB at 2.4 / 5 / 6 GHz")

    def _op_edited(self) -> None:
        p = self._cur_wall()
        row = self.op_list.currentRow()
        if self._loading or p is None or not 0 <= row < len(p.openings):
            return
        o = p.openings[row]
        o.fill, o.open, o.shutter = self.op_fill.currentData(), self.op_open.isChecked(), self.op_shutter.currentData()
        o.at_pct, o.width_m = self.op_at.value(), self.op_w.value()
        o.sill_m, o.height_m = self.op_sill.value(), self.op_h.value()
        o.loss_db = None
        it = self.op_list.item(row)
        if it is not None:
            it.setText(self.op_text(o))
        self._op_hint(o)
        self._wall_selected_quiet(p)
        self._refresh(save=True)

    def _wall_selected_quiet(self, p: ws.Partition) -> None:
        """The quick door controls follow an edit made in the openings list."""
        self._loading = True
        try:
            _set_combo(self.w_door, "none" if p.door is None else "open" if p.door.open else "closed")
            self.w_door_at.setValue(p.door.at_pct if p.door else 50)
            self.w_door_w.setValue(p.door.width_m if p.door else 0.9)
            self.w_door_at.setEnabled(p.door is not None)
            self.w_door_w.setEnabled(p.door is not None)
        finally:
            self._loading = False

    def _add_op(self, kind: str) -> None:
        p = self._cur_wall()
        if p is None:
            return
        used = [o.at_pct for o in p.openings]
        at = next((a for a in (50.0, 25.0, 75.0, 12.0, 88.0, 37.0, 63.0) if all(abs(a - u) > 8 for u in used)), 50.0)
        p.openings.append(ws.window(at) if kind == "window" else ws.door(at))
        self._fill_ops()
        self.op_list.setCurrentRow(len(p.openings) - 1)
        self._wall_selected_quiet(p)
        self._refresh(save=True)

    def _del_op(self) -> None:
        p = self._cur_wall()
        row = self.op_list.currentRow()
        if p is not None and 0 <= row < len(p.openings):
            del p.openings[row]
            self._fill_ops()
            self._wall_selected_quiet(p)
            self._refresh(save=True)

    def _wall_hint(self, p: ws.Partition) -> None:
        side, where = (self.site.width, "from the left") if p.axis == "x" else (self.site.depth, "from the front")
        self.w_at_m.setText(f"{side * p.at_pct / 100:.2f} m {where}")
        l24 = p.losses()[0]
        own = "your value" if p.loss_db is not None else "typical for the material"
        self.w_loss_hint.setText(f"{l24:.3g} dB at 2.4 GHz · {own}")

    def _wall_material_changed(self) -> None:
        """A new material brings its own typical loss (an own value belonged to the old material)."""
        row = self.wall_list.currentRow()
        if self._loading or not 0 <= row < len(self.site.partitions):
            return
        p = self.site.partitions[row]
        p.material, p.loss_db = self.w_mat.currentData(), None
        self._loading = True
        self.w_loss.setValue(p.losses()[1])
        self._loading = False
        self._wall_edited()

    def _wall_edited(self) -> None:
        row = self.wall_list.currentRow()
        if self._loading or not 0 <= row < len(self.site.partitions):
            return
        p = self.site.partitions[row]
        p.axis = self.w_axis.currentData()
        p.at_pct = self.w_at.value()
        a, b = self.w_from.value(), self.w_to.value()
        p.start_pct, p.end_pct = min(a, b), max(a, b)
        typical = ws.MATERIALS[p.material].loss[1]
        p.loss_db = None if abs(self.w_loss.value() - typical) < 0.05 else self.w_loss.value()
        mode = self.w_door.currentData()
        if mode == "none":
            p.door = None
        else:
            height = p.door.height_m if p.door else 2.1
            p.door = ws.Door(self.w_door_at.value(), self.w_door_w.value(), height, mode == "open")
        self.w_door_at.setEnabled(p.door is not None)
        self.w_door_w.setEnabled(p.door is not None)
        p.role = "outer" if self.w_outer.isChecked() else "inner"
        self._wall_hint(p)
        self._fill_ops()
        self._refresh(save=True)

    def _add_wall(self) -> None:
        n = len(self.site.partitions)
        axis = "x" if n % 2 == 0 else "y"
        self.site.partitions.append(ws.Partition(axis, 50.0 if n < 2 else 25.0 + 50.0 * (n % 2), door=ws.Door()))
        self._fill_lists()
        self.wall_list.setCurrentRow(len(self.site.partitions) - 1)
        self._refresh(save=True)

    def _del_wall(self) -> None:
        row = self.wall_list.currentRow()
        if 0 <= row < len(self.site.partitions):
            del self.site.partitions[row]
            self._fill_lists()
            self._refresh(save=True)

    # .......................................................... machines and racking
    def _obs_selected(self, row: int) -> None:
        ok = 0 <= row < len(self.site.obstacles)
        self.obs_form.setEnabled(ok)
        self.obs_del.setEnabled(ok)
        if not ok:
            return
        o = self.site.obstacles[row]
        s = self.site
        self._loading = True
        try:
            _set_combo(self.o_kind, o.kind)
            self.o_w.setMaximum(s.width)
            self.o_d.setMaximum(s.depth)
            self.o_h.setMaximum(s.height)
            self.o_x.setMaximum(s.width)
            self.o_y.setMaximum(s.depth)
            for spin, val in ((self.o_x, o.x), (self.o_y, o.y), (self.o_w, o.w), (self.o_d, o.d), (self.o_h, o.h),
                              (self.o_loss, o.losses()[0][1]), (self.o_z0, o.z0)):
                spin.setValue(val)
        finally:
            self._loading = False
        self._obs_hint(o)

    def _obs_hint(self, o: ws.Obstacle) -> None:
        k = ws.OBSTACLE_KINDS[o.kind]
        if not k.radio:
            self.o_hint.setText("Drawn only: too soft to change the signal.")
        else:
            self.o_hint.setText(f"Takes away at most {k.cap[0]:g} / {k.cap[1]:g} / {k.cap[2]:g} dB at "
                                f"2.4 / 5 / 6 GHz ({k.confidence}); the signal also bends round it.")

    def _obs_kind_changed(self) -> None:
        row = self.obs_list.currentRow()
        if self._loading or not 0 <= row < len(self.site.obstacles):
            return
        o = self.site.obstacles[row]
        o.kind, o.loss_db_m = self.o_kind.currentData(), None
        w, d, h, z0 = ws.OBSTACLE_KINDS[o.kind].size                 # a new kind comes at its typical size
        o.w, o.d, o.h, o.z0 = min(w, self.site.width), min(d, self.site.depth), min(h, self.site.height), z0
        self._loading = True
        for spin, val in ((self.o_loss, o.losses()[0][1]), (self.o_w, o.w), (self.o_d, o.d), (self.o_h, o.h),
                          (self.o_z0, o.z0)):
            spin.setValue(val)
        self._loading = False
        self._obs_edited()

    def _obs_edited(self) -> None:
        row = self.obs_list.currentRow()
        if self._loading or not 0 <= row < len(self.site.obstacles):
            return
        o = self.site.obstacles[row]
        o.x, o.y, o.w, o.d, o.h = (self.o_x.value(), self.o_y.value(), self.o_w.value(), self.o_d.value(),
                                   self.o_h.value())
        o.z0 = self.o_z0.value()
        self._obs_hint(o)
        typical = ws.OBSTACLE_KINDS[o.kind].per_m[1]
        o.loss_db_m = None if abs(self.o_loss.value() - typical) < 0.05 else self.o_loss.value()
        self._refresh(save=True)
        self._loading = True                           # the room keeps the box inside: show where it ended up
        self.o_x.setValue(o.x)
        self.o_y.setValue(o.y)
        self._loading = False

    def _add_obs(self) -> None:
        s = self.site
        used = {o.name for o in s.obstacles}
        n = 1
        while f"Obstacle {n}" in used:
            n += 1
        s.obstacles.append(ws.Obstacle(f"Obstacle {n}", "racking_full", s.width * 0.4, s.depth * 0.3,
                                       min(1.2, s.width), min(4.0, s.depth), min(2.5, s.height)))
        self._fill_lists()
        self.obs_list.setCurrentRow(len(s.obstacles) - 1)
        self._refresh(save=True)

    def _del_obs(self) -> None:
        row = self.obs_list.currentRow()
        if 0 <= row < len(self.site.obstacles):
            del self.site.obstacles[row]
            self._fill_lists()
            self._refresh(save=True)

    # .......................................................... access points
    def _ap_selected(self, row: int) -> None:
        ok = 0 <= row < len(self.site.aps)
        self.ap_form.setEnabled(ok)
        self.ap_del.setEnabled(ok)
        self.view.select_ap(row if ok else -1)
        if not ok:
            return
        a = self.site.aps[row]
        self._loading = True
        try:
            self.a_x.setMaximum(self.site.width)
            self.a_y.setMaximum(self.site.depth)
            self.a_z.setMaximum(self.site.height)
            self.a_x.setValue(a.x)
            self.a_y.setValue(a.y)
            self.a_z.setValue(a.z)
            self.a_pwr.setValue(a.tx_dbm)
            _set_combo(self.a_band, a.band)
            self.a_on.setChecked(a.enabled)
        finally:
            self._loading = False

    def _ap_picked(self, idx: int) -> None:
        self.ap_list.setCurrentRow(idx)

    def _ap_edited(self) -> None:
        row = self.ap_list.currentRow()
        if self._loading or not 0 <= row < len(self.site.aps):
            return
        a = self.site.aps[row]
        a.x, a.y, a.z = self.a_x.value(), self.a_y.value(), self.a_z.value()
        a.tx_dbm, a.band, a.enabled = self.a_pwr.value(), self.a_band.currentData(), self.a_on.isChecked()
        self._refresh(save=True)

    def _view_edited(self) -> None:
        """An access point was dragged in the 3D view."""
        row = self.ap_list.currentRow()
        if 0 <= row < len(self.site.aps):
            a = self.site.aps[row]
            self._loading = True
            self.a_x.setValue(a.x)
            self.a_y.setValue(a.y)
            self._loading = False
        self._save_timer.start()

    def _add_ap(self) -> None:
        self.site.add_ap()
        self._fill_lists()
        self.ap_list.setCurrentRow(len(self.site.aps) - 1)
        self._refresh(save=True)

    def _del_ap(self) -> None:
        row = self.ap_list.currentRow()
        if 0 <= row < len(self.site.aps):
            del self.site.aps[row]
            self._fill_lists()
            self._refresh(save=True)

    def sync_lab_aps(self, labels) -> None:
        if self.site.sync_lab_aps(labels):
            self._fill_lists()
            self._refresh(save=True)


def open_site_window(state, lab_id: str | None = None, title: str = "", ap_labels=(), parent=None,
                     gl: bool | None = None) -> SiteWindow:
    """Open the site of a lab (kept in LabDesk's state file next to the lab's other local data), or the
    stand-alone site when no lab is open.  A site that was never saved starts from the demo room."""
    store = state.lab(lab_id) if lab_id else state.data
    raw = store.get("site")
    site = ws.Site.from_dict(raw) if isinstance(raw, dict) else ws.demo_site()
    if lab_id and not isinstance(raw, dict):
        site.aps = []                          # a lab's site shows the lab's own access points

    def save(d: dict, ui: dict) -> None:
        st = state.lab(lab_id) if lab_id else state.data
        st["site"], st["site_ui"] = d, ui
        state.save()

    win = SiteWindow(site, save, title, parent, gl=gl, ui=store.get("site_ui"))
    if lab_id:
        win.sync_lab_aps(ap_labels)
    return win
