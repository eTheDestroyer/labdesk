"""GPU renderer for the 3D Wi-Fi site view: the room, the floor heat map and the glow in the air.

``SceneRenderer`` owns a private OpenGL 3.3 context on an off-screen surface.  It never opens a window:
every frame is drawn into framebuffer objects and read back into a ``QImage`` that the caller paints its
labels, access points and legend over with QPainter.  The camera is the one of ``site3d.Frame`` (same
focal length, same basis), so both layers line up to the pixel.

Passes of one frame:

1. shadow map of the walls, doors and obstacles (depth seen from the light);
2. eye distance of every solid, so the glow knows where to stop;
3. the room, multisampled: floor heat map with contour lines and a metre grid (unlit: no light or shadow
   ever changes a heat colour), lit walls / doors / obstacles, a faint shadow on the ground outside;
4. the glow: a ray march through the field texture (about one ray per logical pixel), front to back,
   stopped by the solids; laid "over" the room with a depth-aware upsample, so it works on light and dark
   themes.

At one device pixel per logical pixel the frame is drawn at twice the size and averaged down on the GPU.

The radio field is worked out here too (``compute_field``): a fragment shader evaluates the model of
``wifi_site.Site.rssi`` at every voxel of a grid of about four million points, one layer per draw, straight into
the 3D field texture (about 1 ms on a GTX 1080, against 180 000 points in 3 ms for the CPU engine), so door
beams and wall shadows come out sharp.  A volume computed elsewhere can still be handed over with
``set_field`` (the fallback when the shader cannot run).  The colours come as a 256 x 1 ramp (``set_ramp``).  ``SceneRenderer.create()`` returns ``(None, reason)`` instead of raising when the
machine has no usable GPU path (no display, OpenGL older than 3.3, a software rasteriser): the caller
then keeps its QPainter view.

All methods must be called from the thread that called ``create()`` (the GUI thread).
"""
from __future__ import annotations

import math
import os
import re
import time
import weakref
from dataclasses import dataclass

from PySide6.QtCore import QRect
from PySide6.QtGui import (QGuiApplication, QImage, QMatrix4x4, QOffscreenSurface, QOpenGLContext, QSurfaceFormat,
                           QVector3D)

from .. import wifi_site as ws

try:                                        # both are optional: without them the view stays on QPainter
    import numpy as np
    from PySide6.QtOpenGL import (QOpenGLBuffer, QOpenGLFramebufferObject, QOpenGLFramebufferObjectFormat,
                                  QOpenGLShader, QOpenGLShaderProgram, QOpenGLTexture, QOpenGLVertexArrayObject)
    _IMPORT_ERROR = ""
except ImportError as _e:                   # pragma: no cover - depends on the installation
    np = None
    _IMPORT_ERROR = str(_e)

NEAR = 0.05                         # near clipping plane (m), as in site3d.Frame
WALL_T = 0.15                       # wall thickness (m)
KERB_H = 0.25                       # what is left of an outer wall the camera looks over (m)
SHADOW_PX = 2048
SUPERSAMPLE_PX = 2_400_000          # frames up to this many pixels are drawn twice as large at scale 1
GLOW_STEPS = 192                    # most steps of one ray (the graphics card has room for this many)
GLOW_CAP = (0.42, 0.80)             # most opacity the fog can reach on one pixel, at glow 0 and at glow 1
FOG_SCALE = (11.0, 4.3)             # metres one tile of the billow noise spans: big and small billows
FOG_NOISE_PX = 32                   # the tiling noise texture is this many texels a side
MAX_WAVE_SOURCES = 16                # access points that send waves (the strongest first)
WAVE_SPACING = 2.5                  # metres between two wave fronts
WAVE_SPEED = 2.0                    # metres per second the fronts grow (slowed down a lot: real waves are instant)
WAVE_CAP = (0.55, 0.92)             # most opacity of the waves on one pixel, at glow 0 and at glow 1
OUTLINE_INK = (0.03, 0.04, 0.05)    # silhouettes of walls, doors and obstacles
OUTLINE_STRENGTH = 0.70
MAX_CONTOURS = 12
MAX_FRAME_PX = 8192
MAX_FIELD_SIDE = 1024
GPU_VOXEL_BUDGET = 4_000_000          # voxels of the field the graphics card computes (the CPU grid has 180 000)
GPU_MIN_CELL_M = 0.05               # never finer than this
GEO_ROW = 1024                      # texels per row of the packed radio model
NO_FIELD_DBM = -140.0               # what "no signal" is stored as (anything this low is "no service")
SOFTWARE_MARKERS = ("llvmpipe", "softpipe", "swiftshader", "gdi generic", "software", "swrast",
                    "basic render driver")

GL_FLOAT, GL_TRIANGLES, GL_TEXTURE_2D, GL_TEXTURE0 = 0x1406, 0x0004, 0x0DE1, 0x84C0
GL_DEPTH_TEST, GL_BLEND, GL_CULL_FACE = 0x0B71, 0x0BE2, 0x0B44
GL_COLOR_BUFFER_BIT, GL_DEPTH_BUFFER_BIT = 0x4000, 0x0100
GL_ONE, GL_ONE_MINUS_SRC_ALPHA, GL_SRC_ALPHA = 1, 0x0303, 0x0302
GL_RGB8, GL_R32F, GL_RGBA16F, GL_LINEAR = 0x8051, 0x822E, 0x881A, 0x2601
GL_RENDERER, GL_VERSION, GL_SHADING_LANGUAGE_VERSION = 0x1F01, 0x1F02, 0x8B8C
GL_TEXTURE_3D, GL_MAX_3D_TEXTURE_SIZE = 0x806F, 0x8073
GL_RED, GL_PACK_ALIGNMENT = 0x1903, 0x0D05

# vertex kinds
K_GROUND, K_FLOOR, K_WALL, K_OBSTACLE, K_LEAF, K_FRAME = 0.0, 1.0, 2.0, 3.0, 4.0, 5.0
K_GLASS, K_WFRAME, K_SHUTTER, K_STEEL = 6.0, 7.0, 8.0, 9.0
STRIDE = 8                          # floats per vertex: position 3, normal 3, kind 1, tag 1


# ============================================================================ what the caller sets
@dataclass
class RenderOpts:
    slice_z: float = 1.0          # height (m) of the plane the floor heat map shows
    heat: bool = True             # paint the heat map on the floor
    glow: float = 0.6             # 0 = no glow in the air ... 1 = strongest
    contours: tuple = ()          # dBm levels drawn as thin lines on the floor heat map
    lo_dbm: float = -85.0         # ramp start; below it there is "no service"
    hi_dbm: float = -40.0         # ramp end
    background: tuple = (0.07, 0.08, 0.09)   # linear-ish RGB 0..1 from the app theme
    floor: tuple = (0.17, 0.18, 0.20)        # colour of floor with no service / heat off
    wall: tuple = (0.66, 0.67, 0.70)         # base colour of walls
    obstacle: tuple = (0.40, 0.45, 0.52)
    door: tuple = (0.72, 0.54, 0.34)
    line: tuple = (0.05, 0.06, 0.07)         # grid and contour line colour
    selected_wall: int = -1       # index into site.walls() to tint, -1 = none
    accent: tuple = (0.36, 0.36, 0.84)       # what the selected wall is tinted with (theme accent)
    fog_time: float = 0.0         # seconds: the fog's billows drift slowly with it
    fog_detail: float = 0.75      # 0 = an even haze ... 1 = billowing fog (looks only: the colour is the signal)
    air: str = "fog"              # what fills the air: "fog" (a haze), or "waves" (shells from the sources)
    sources: tuple = ()           # (x, y, z) of the access points that send waves


# ============================================================================ pure helpers (no OpenGL)
def parse_gl_version(text) -> tuple[int, int] | None:
    """(major, minor) out of a GL_VERSION or GL_SHADING_LANGUAGE_VERSION string, None when there is none.
    '4.6.0 NVIDIA 570.172', '3.0 Mesa 22.1', 'OpenGL ES 3.2 Mesa', '4.60 NVIDIA' (-> 4, 60)."""
    m = re.search(r"(\d+)\.(\d+)", text or "")
    return (int(m.group(1)), int(m.group(2))) if m else None


def is_gles(version_text) -> bool:
    return "opengl es" in (version_text or "").lower()


def is_software_renderer(renderer_text) -> bool:
    """True for the CPU rasterisers (Mesa llvmpipe / softpipe, SwiftShader, the Windows fallbacks)."""
    low = (renderer_text or "").lower()
    return any(m in low for m in SOFTWARE_MARKERS)


def software_allowed(env=None) -> bool:
    return (os.environ if env is None else env).get("LABDESK_GL_SOFTWARE", "") == "1"


def gl_problem(version_text, renderer_text, glsl_text=None, allow_software: bool = False) -> str:
    """Why this OpenGL cannot be used, in words for the user; '' when it can.  The strings are the ones
    glGetString really returned (QOpenGLContext.format() repeats what was asked for, not what was got)."""
    if not version_text or not renderer_text:
        return "the graphics driver did not report an OpenGL version"
    if is_gles(version_text):
        return f"the driver offers OpenGL ES ({version_text}); desktop OpenGL 3.3 is needed"
    v = parse_gl_version(version_text)
    if v is None:
        return f"the OpenGL version could not be read ({version_text!r})"
    if v < (3, 3):
        return f"OpenGL {v[0]}.{v[1]} is too old ({renderer_text}); 3.3 is needed"
    if glsl_text is not None:
        g = parse_gl_version(glsl_text)
        if g is None or g < (3, 30):
            return f"the shading language {glsl_text!r} is too old ({renderer_text}); 3.30 is needed"
    if is_software_renderer(renderer_text) and not allow_software:
        return (f"{renderer_text} draws on the processor, which is too slow for this view "
                "(set LABDESK_GL_SOFTWARE=1 to use it anyway)")
    return ""


def camera_basis(eye, target) -> tuple:
    """(forward, right, up) exactly as site3d.Frame builds them: right = forward x z, up = right x forward."""
    d = [float(target[i]) - float(eye[i]) for i in range(3)]
    n = math.sqrt(d[0] * d[0] + d[1] * d[1] + d[2] * d[2])
    f = (d[0] / n, d[1] / n, d[2] / n) if n > 1e-12 else (0.0, 1.0, 0.0)
    h = math.hypot(f[0], f[1])
    r = (f[1] / h, -f[0] / h, 0.0) if h > 1e-9 else (1.0, 0.0, 0.0)       # straight up / down: any right
    u = (r[1] * f[2], -r[0] * f[2], r[0] * f[1] - r[1] * f[0])
    return f, r, u


def view_projection(eye, target, fov_y: float, width: float, height: float, fb_w: int, fb_h: int,
                    near: float = NEAR, far: float = 1000.0) -> list:
    """World -> clip matrix (row-major, 16 floats) for a framebuffer of ``fb_w`` x ``fb_h`` pixels showing a
    view of ``width`` x ``height`` logical pixels.  Row 0 of the framebuffer is the *top* of the picture
    (the projection is flipped), so the read-back needs no mirroring; a point lands where
    ``site3d.Frame.proj`` puts it, times ``fb_w / width``."""
    f, r, u = camera_basis(eye, target)
    sx, sy = fb_w / width, fb_h / height
    focal = (height / 2.0) / math.tan(fov_y / 2.0)
    e = [float(v) for v in eye]
    rr = (*r, -(r[0] * e[0] + r[1] * e[1] + r[2] * e[2]))
    uu = (*u, -(u[0] * e[0] + u[1] * e[1] + u[2] * e[2]))
    ff = (*f, -(f[0] * e[0] + f[1] * e[1] + f[2] * e[2]))
    kx, ky = 2.0 * focal * sx / fb_w, 2.0 * focal * sy / fb_h
    ox, oy = 2.0 * (width * sx / 2.0) / fb_w - 1.0, 2.0 * (height * sy / 2.0) / fb_h - 1.0
    a, b = (far + near) / (far - near), -2.0 * far * near / (far - near)
    return ([kx * rr[i] + ox * ff[i] for i in range(4)] + [-ky * uu[i] + oy * ff[i] for i in range(4)]
            + [a * ff[i] + (b if i == 3 else 0.0) for i in range(4)] + list(ff))


def cut_mask(size, eye) -> int:
    """Which outer walls stand between the eye and the room and are cut down to a kerb:
    bit 0 the wall at x = 0, bit 1 at x = width, bit 2 at y = 0, bit 3 at y = depth."""
    return ((1 if eye[0] < 0.0 else 0) | (2 if eye[0] > size[0] else 0)
            | (4 if eye[1] < 0.0 else 0) | (8 if eye[1] > size[1] else 0))


def box_chord(eye, direction, size) -> float:
    """Metres of the ray from ``eye`` along ``direction`` that lie inside the room box (0 when it misses)."""
    n = math.sqrt(sum(v * v for v in direction)) or 1.0
    t0, t1 = 0.0, math.inf
    for k in range(3):
        d = direction[k] / n
        if abs(d) < 1e-12:
            if not 0.0 <= eye[k] <= size[k]:
                return 0.0
            continue
        a, b = -eye[k] / d, (size[k] - eye[k]) / d
        t0, t1 = max(t0, min(a, b)), min(t1, max(a, b))
    return max(t1 - t0, 0.0) if math.isfinite(t1) else 0.0


def _quad(out: list, a, b, c, d, n, kind: float, tag: float) -> None:
    for p in (a, b, c, a, c, d):
        out.extend((p[0], p[1], p[2], n[0], n[1], n[2], kind, tag))


def _box(out: list, lo, hi, kind: float, tag: float = -1.0) -> None:
    x0, y0, z0 = lo
    x1, y1, z1 = hi
    if x1 - x0 <= 1e-6 or y1 - y0 <= 1e-6 or z1 - z0 <= 1e-6:
        return
    _quad(out, (x0, y0, z0), (x0, y1, z0), (x0, y1, z1), (x0, y0, z1), (-1, 0, 0), kind, tag)
    _quad(out, (x1, y0, z0), (x1, y1, z0), (x1, y1, z1), (x1, y0, z1), (1, 0, 0), kind, tag)
    _quad(out, (x0, y0, z0), (x1, y0, z0), (x1, y0, z1), (x0, y0, z1), (0, -1, 0), kind, tag)
    _quad(out, (x0, y1, z0), (x1, y1, z0), (x1, y1, z1), (x0, y1, z1), (0, 1, 0), kind, tag)
    _quad(out, (x0, y0, z1), (x1, y0, z1), (x1, y1, z1), (x0, y1, z1), (0, 0, 1), kind, tag)
    if z0 > 1e-6:                                    # the underside shows only when the box hangs
        _quad(out, (x0, y0, z0), (x1, y0, z0), (x1, y1, z0), (x0, y1, z0), (0, 0, -1), kind, tag)


def site_geometry(site) -> dict:
    """What the renderer needs of a ``wifi_site.Site``, copied (the site may change afterwards).  Each wall:
    (axis, position, run from, run to, openings, thickness, role); each opening: (run from, run to, sill, head,
    kind, open, fill, shutter); each obstacle: (x, y, w, d, h, z0, kind)."""
    def num(v, lo, hi):
        v = float(v)
        return min(max(v if math.isfinite(v) else lo, lo), hi)

    w, d, h = num(site.width, 0.1, 5000.0), num(site.depth, 0.1, 5000.0), num(site.height, 0.1, 500.0)
    walls = []
    for wl in site.walls():
        side, run = (w, d) if wl.axis == "x" else (d, w)
        holes = [(num(hl.d0, 0.0, run), num(hl.d1, 0.0, run), num(hl.z0, 0.0, h), num(hl.z1, 0.0, h),
                  hl.opening.kind, bool(hl.opening.open), hl.opening.fill, hl.opening.shutter)
                 for hl in getattr(wl, "holes", ()) if hl.d1 - hl.d0 > 1e-6]
        walls.append(("y" if wl.axis == "y" else "x", num(wl.a, 0.0, side), num(wl.b0, 0.0, run),
                      num(wl.b1, 0.0, run), holes, num(getattr(wl, "thickness", WALL_T), 0.02, 1.0),
                      getattr(wl, "role", "inner")))
    boxes = [(num(o.x, 0.0, w), num(o.y, 0.0, d), num(o.w, 0.0, w), num(o.d, 0.0, d), num(o.h, 0.0, h),
              num(getattr(o, "z0", 0.0), 0.0, h), getattr(o, "kind", ""))
             for o in getattr(site, "obstacles", ())]
    return {"size": (w, d, h), "walls": walls, "obstacles": boxes}


# furniture colours (linear-ish RGB 0..1), by obstacle kind; anything else takes RenderOpts.obstacle
FURNITURE_RGB = {
    "fridge": (0.86, 0.87, 0.88), "washer": (0.90, 0.90, 0.91), "metal_cabinet": (0.50, 0.53, 0.57),
    "server_rack": (0.16, 0.17, 0.19), "cnc": (0.36, 0.52, 0.60), "machine": (0.38, 0.50, 0.58),
    "bookshelf": (0.55, 0.40, 0.27), "wardrobe": (0.70, 0.55, 0.40), "kitchen": (0.80, 0.76, 0.70),
    "sofa": (0.42, 0.47, 0.58), "bed": (0.82, 0.80, 0.74), "desk": (0.66, 0.53, 0.39), "table": (0.62, 0.49, 0.36),
    "aquarium": (0.40, 0.62, 0.78), "column": (0.64, 0.64, 0.62),
    "pallet_empty": (0.86, 0.45, 0.16), "pallet_dry": (0.78, 0.62, 0.42), "pallet_wet": (0.30, 0.48, 0.70),
}
PALETTE = list(FURNITURE_RGB)
GLAZED = ("glass", "double_glazing", "low_e", "curtain_wall", "mamad_glazing")
STEEL = ("door_steel", "blast_door")


def _furniture(out: list, x, y, bw, bd, bh, z0, kind, tag) -> None:
    """Drawn shapes inside the radio box (the radio sees the box)."""
    x1, y1, z1 = x + bw, y + bd, z0 + bh
    long_x = bw >= bd

    def b(lo, hi):
        _box(out, lo, hi, K_OBSTACLE, tag)

    if kind == "sofa":
        seat = 0.45 * bh
        b((x, y, z0), (x1, y1, z0 + seat))
        if long_x:
            b((x, y1 - 0.25 * bd, z0 + seat), (x1, y1, z1))
            b((x, y, z0 + seat), (x + 0.12 * bw, y1, z0 + 0.7 * bh))
            b((x1 - 0.12 * bw, y, z0 + seat), (x1, y1, z0 + 0.7 * bh))
        else:
            b((x1 - 0.25 * bw, y, z0 + seat), (x1, y1, z1))
            b((x, y, z0 + seat), (x1, y + 0.12 * bd, z0 + 0.7 * bh))
            b((x, y1 - 0.12 * bd, z0 + seat), (x1, y1, z0 + 0.7 * bh))
    elif kind == "bed":
        b((x, y, z0), (x1, y1, z0 + min(bh, 0.5)))
        if long_x:
            b((x, y, z0), (x + 0.06, y1, z0 + 1.0))
        else:
            b((x, y1 - 0.06, z0), (x1, y1, z0 + 1.0))
    elif kind in ("desk", "table"):
        top = 0.04
        b((x, y, z1 - top), (x1, y1, z1))
        for lx, ly in ((x, y), (x1 - 0.05, y), (x, y1 - 0.05), (x1 - 0.05, y1 - 0.05)):
            b((lx, ly, z0), (lx + 0.05, ly + 0.05, z1 - top))
    elif kind in ("pallet_empty", "pallet_dry", "pallet_wet"):
        for lx in (x, x1 - 0.08):                                       # uprights at the ends, beams per level
            b((lx, y, z0), (lx + 0.08, y1, z1))
        levels = max(1, int(bh / 1.4))
        for k in range(levels + 1):
            zb = z0 + k * bh / (levels + 0.0001) - (0.1 if k == levels else 0.0)
            b((x, y, zb), (x1, y1, zb + 0.1))
            if kind != "pallet_empty" and k < levels:                   # loads on the shelf
                b((x + 0.1, y + 0.05, zb + 0.1), (x1 - 0.1, y1 - 0.05, zb + 0.1 + 0.7 * bh / (levels + 0.0001)))
    else:
        b((x, y, z0), (x1, y1, z1))


def build_mesh(geom: dict, cut: int = 0) -> tuple:
    """The room as triangles: ``(floats, first_floor_vertex, first_solid_vertex, first_glass_vertex)``.  The
    ground outside comes first, then the floor, then everything that can cast a shadow, then window glass
    (drawn last, blended, and left out of the shadow and distance passes)."""
    w, d, h = geom["size"]
    T = WALL_T
    out: list = []
    glass: list = []
    far = 6.0 * math.sqrt(w * w + d * d) + 60.0
    for lo, hi in (((-far, -far), (w + far, -T)), ((-far, d + T), (w + far, d + far)),
                   ((-far, -T), (-T, d + T)), ((w + T, -T), (w + far, d + T))):       # a frame round the room
        _quad(out, (lo[0], lo[1], 0), (hi[0], lo[1], 0), (hi[0], hi[1], 0), (lo[0], hi[1], 0), (0, 0, 1),
              K_GROUND, -1.0)
    n_ground = len(out) // STRIDE
    _quad(out, (0, 0, 0), (w, 0, 0), (w, d, 0), (0, d, 0), (0, 0, 1), K_FLOOR, -1.0)
    n_floor = len(out) // STRIDE

    kerb = min(KERB_H, h)
    walls = geom["walls"]

    def outer_on(axis, a):
        return any(r[0] == axis and r[6] == "outer" and abs(r[1] - a) < 1e-6 for r in walls)

    # the plain box walls only where no outer wall of the building stands on that edge
    for axis, a, bit, lo, hi in (("x", 0.0, 1, (-T, -T, 0), (0, d + T)), ("x", w, 2, (w, -T, 0), (w + T, d + T)),
                                 ("y", 0.0, 4, (0, -T, 0), (w, 0)), ("y", d, 8, (0, d, 0), (w, d + T))):
        if not outer_on(axis, a):
            _box(out, lo, (hi[0], hi[1], kerb if cut & bit else h), K_WALL)

    for idx, (axis, a, b0, b1, holes, thick, role) in enumerate(walls):
        side = w if axis == "x" else d
        t = max(0.05, thick)
        on_edge = role == "outer" and (a <= 1e-6 or a >= side - 1e-6)
        if on_edge:
            a0, a1 = (a - t, a) if a <= 1e-6 else (a, a + t)                 # on the box edge: thickness outside
        else:
            a0, a1 = max(a - t / 2, 0.0), min(a + t / 2, side)
        top = h
        if role == "outer":                                                  # cut when the camera looks over it
            bit = (1 if a < side / 2 else 2) if axis == "x" else (4 if a < side / 2 else 8)
            if cut & bit:
                top = kerb
        outward = -1.0 if a < side / 2 else 1.0

        def part(r0, r1, z0, z1, c0=a0, c1=a1, kind=K_WALL, tag=float(idx), axis=axis, top=top, lst=out):
            if top < h:                                                      # a cut facade: only the kerb
                if kind != K_WALL:
                    return
                z1 = min(z1, top)
            if axis == "x":
                _box(lst, (c0, r0, z0), (c1, r1, z1), kind, tag)
            else:
                _box(lst, (r0, c0, z0), (r1, c1, z1), kind, tag)

        cur = b0
        for (d0, d1, z0, z1, kind, is_open, fill, shutter) in sorted(holes, key=lambda q: q[0]):
            if d0 > cur:
                part(cur, d0, 0.0, h)
            lo_ = max(d0, cur)
            part(lo_, d1, 0.0, z0)                                           # under the sill
            part(lo_, d1, z1, h)                                             # over the head
            cur = max(cur, d1)
            j = min(0.05, (d1 - d0) / 6.0)
            p0, p1 = a0 - 0.02, a1 + 0.02
            fk = K_WFRAME if kind == "window" else K_FRAME
            part(d0, d0 + j, z0, z1, p0, p1, fk, -1.0)
            part(d1 - j, d1, z0, z1, p0, p1, fk, -1.0)
            part(d0 + j, d1 - j, z1 - j, z1, p0, p1, fk, -1.0)
            mid = (a0 + a1) / 2
            if kind == "window":
                part(d0 + j, d1 - j, z0, z0 + j, p0, p1, fk, -1.0)                # sill
                if d1 - d0 > 1.0 and fill != "curtain_wall":
                    m = (d0 + d1) / 2
                    part(m - 0.025, m + 0.025, z0, z1, mid - 0.025, mid + 0.025, fk, -1.0)
            if role == "outer" and fill not in ("blast_window", "curtain_wall") and z1 + 0.22 <= h:
                c0, c1 = (a0 - 0.18, a0) if outward < 0 else (a1, a1 + 0.18)     # the trisim box over it
                part(d0, d1, z1, z1 + 0.22, c0, c1, K_WFRAME, -1.0)
            down = {"closed": 1.0, "slits": 0.85}.get(shutter, 0.0)
            if down:                                                         # the shutter itself, outside
                c0, c1 = (a0 - 0.05, a0 - 0.02) if outward < 0 else (a1 + 0.02, a1 + 0.05)
                part(d0, d1, z1 - down * (z1 - z0), z1, c0, c1, K_SHUTTER, -1.0)
            if kind == "window" or (fill in GLAZED and not is_open):
                if fill == "blast_window" and not is_open:
                    part(d0 + j, d1 - j, z0 + j, z1 - j, mid - 0.03, mid + 0.03, K_STEEL, -1.0)
                elif not is_open and fill in GLAZED:
                    part(d0 + j, d1 - j, z0 + j, z1 - j, mid - 0.006, mid + 0.006, K_GLASS, -1.0, lst=glass)
                continue
            l0, l1, ltop = d0 + j, d1 - j, z1 - j
            if fill == "none":                                               # a passage: no leaf
                continue
            leaf = K_STEEL if fill in STEEL else K_LEAF
            if not is_open:
                part(l0, l1, z0, ltop, mid - 0.022, mid + 0.022, leaf, -1.0)
                continue
            leaves = [(l0, l1 - l0, 1.0)] if l1 - l0 <= 1.6 else [(l0, (l1 - l0) / 2, 1.0), (l1, (l1 - l0) / 2, -1.0)]
            for hinge, length, sgn in leaves:                                # each leaf stands square to its wall
                r0, r1 = (hinge, hinge + 0.045) if sgn > 0 else (hinge - 0.045, hinge)
                if a1 + length <= side - 0.02:
                    part(r0, r1, z0, ltop, a1, a1 + length, leaf, -1.0)
                else:
                    part(r0, r1, z0, ltop, max(a0 - length, 0.0), a0, leaf, -1.0)
        if cur < b1:
            part(cur, b1, 0.0, h)

    for i, (x, y, bw, bd, bh, z0, kind) in enumerate(geom["obstacles"]):
        tag = float(PALETTE.index(kind)) if kind in FURNITURE_RGB else -1.0
        _furniture(out, x, y, min(bw, w - x), min(bd, d - y), bh, z0, kind, tag)
    n_solid = len(out) // STRIDE
    out.extend(glass)
    return out, n_ground, n_floor, n_solid


def fog_noise(n: int = FOG_NOISE_PX, seed: int = 7):
    """n x n x n uint8 noise that tiles in every direction: white noise smoothed with a wrap-around Gaussian
    (sigma 1.6 texels), then ranked so its values spread evenly over 0..255."""
    rng = np.random.default_rng(seed)
    f = np.fft.fftn(rng.random((n, n, n)))
    k = np.fft.fftfreq(n)
    k2 = k[:, None, None] ** 2 + k[None, :, None] ** 2 + k[None, None, :] ** 2
    smooth = np.real(np.fft.ifftn(f * np.exp(-2.0 * (np.pi * 1.6) ** 2 * k2)))
    ranks = np.empty(smooth.size, np.int64)
    ranks[np.argsort(smooth, axis=None)] = np.arange(smooth.size)
    return (ranks * 255 // (smooth.size - 1)).astype(np.uint8).reshape(n, n, n)


def field_grid(width: float, depth: float, height: float, budget: int = GPU_VOXEL_BUDGET,
               max_side: int = MAX_FIELD_SIDE) -> tuple[int, int, int]:
    """(nx, ny, nz) of the field the graphics card computes: cubic voxels, about ``budget`` of them, no finer
    than GPU_MIN_CELL_M, no side longer than ``max_side`` texels.  Voxel centres are at (i + 0.5) * side / n,
    as in ``wifi_field.Grid``."""
    w, d, h = (max(float(v), 1e-3) for v in (width, depth, height))
    cell = max(GPU_MIN_CELL_M, (w * d * h / max(1, budget)) ** (1.0 / 3.0), max(w, d, h) / max(2, max_side))
    return (max(2, min(max_side, round(w / cell))), max(2, min(max_side, round(d / cell))),
            max(4, min(max_side, round(h / cell))))


def field_tables(site, band: str | None = None) -> tuple:
    """The radio model of a site packed for the field shader: ``(texels, n_ap, n_wall, n_obstacle)`` with
    ``texels`` a float32 array of RGBA rows, in this order:

    * access point (2 texels): (x, y, z, EIRP), (loss at 1 m, clutter exponent, band index 0 / 1 / 2, 0);
    * wall (3 texels): (axis 0 = x / 1 = y, position, run from, run to), (loss at 2.4, 5, 6 GHz, 0),
      (index of its first opening texel, number of openings, 0, 0);
    * obstacle (4 texels): (x0, y0, z0, 0), (x1, y1, z1, 0), (dB/m at 2.4, 5, 6, 0), (cap at 2.4, 5, 6, 0);
    * opening (2 texels, after the obstacles): (run from, run to, sill, head), (loss at 2.4, 5, 6, 0).

    Only the radios that transmit (on ``band``, None = every band; never the neighbours' networks) and the
    obstacles that take something away are listed.  The numbers
    are the ones ``Site.rssi`` uses (band index = ``wifi_site.bi``)."""
    n = ws.CLUTTER.get(site.clutter, ws.CLUTTER["light"])[1]
    rows: list = []
    aps = site.emitters(band) if hasattr(site, "emitters") else [ap for ap in site.aps if ap.enabled]
    for ap in aps:
        ref = 20.0 * math.log10(ws.BAND_MHZ.get(ap.band, ws.BAND_MHZ["5"])) - 27.55
        rows += [(ap.x, ap.y, ap.z, ap.tx_dbm), (ref, n, float(ws.bi(ap.band)), 0.0)]
    walls = site.walls()
    obstacles = [o for o in site.obstacles if any(c > 0 for c in o.losses()[1]) and any(m > 0 for m in o.losses()[0])]
    first_hole = len(rows) + 3 * len(walls) + 4 * len(obstacles)
    hole_rows: list = []
    for w in walls:
        rows += [(0.0 if w.axis == "x" else 1.0, w.a, w.b0, w.b1), (*w.loss, 0.0),
                 (float(first_hole + len(hole_rows)), float(len(w.holes)), 0.0, 0.0)]
        for h in w.holes:
            hole_rows += [(h.d0, h.d1, h.z0, h.z1), (*h.loss, 0.0)]
    for o in obstacles:
        per_m, cap = o.losses()
        rows += [(o.x, o.y, o.z0, 0.0), (o.x + o.w, o.y + o.d, o.z0 + o.h, 0.0), (*per_m, 0.0), (*cap, 0.0)]
    rows += hole_rows
    if not rows:
        rows = [(0.0, 0.0, 0.0, 0.0)]
    return np.asarray(rows, dtype=np.float32), len(aps), len(walls), len(obstacles)


def wave_phase(seconds: float) -> float:
    """Radius (m) of the innermost wave front at this time: the fronts grow at WAVE_SPEED, one every WAVE_SPACING."""
    t = float(seconds) if math.isfinite(float(seconds)) else 0.0
    return (t * WAVE_SPEED) % WAVE_SPACING


def default_ramp() -> QImage:
    """Red (weak) through orange and yellow to green (strong): used until the caller sets its own ramp."""
    stops = ((0.0, (214, 40, 40)), (0.33, (244, 128, 36)), (0.62, (250, 214, 52)), (1.0, (46, 184, 92)))
    img = QImage(256, 1, QImage.Format_RGBA8888)
    for i in range(256):
        t = i / 255.0
        for (t0, c0), (t1, c1) in zip(stops, stops[1:]):
            if t <= t1:
                k = (t - t0) / (t1 - t0)
                rgb = [round(c0[j] + (c1[j] - c0[j]) * k) for j in range(3)]
                break
        img.setPixel(i, 0, 0xFF000000 | (rgb[0] << 16) | (rgb[1] << 8) | rgb[2])
    return img


# ============================================================================ shaders
SCENE_VS = """#version 330 core
layout(location=0) in vec3 aPos; layout(location=1) in vec3 aNor; layout(location=2) in vec2 aKT;
uniform mat4 vp;
out vec3 wpos; out vec3 wnor; flat out vec2 kt;
void main(){ wpos = aPos; wnor = aNor; kt = aKT; gl_Position = vp * vec4(aPos, 1.0); }
"""

# depth seen from the light (mode 0) or distance from the eye (mode 1), as a float colour
DEPTH_FS = """#version 330 core
in vec3 wpos; out vec4 frag; uniform vec3 eye; uniform int mode;
void main(){ float d = mode == 0 ? gl_FragCoord.z : length(wpos - eye); frag = vec4(d, d, d, 1.0); }
"""

SCENE_FS = """#version 330 core
in vec3 wpos; in vec3 wnor; flat in vec2 kt; out vec4 frag;
uniform sampler3D field; uniform sampler2D ramp; uniform sampler2D shadowMap;
uniform mat4 lightVP; uniform vec3 lightDir; uniform vec3 size; uniform vec3 res;
uniform vec3 cBg; uniform vec3 cFloor; uniform vec3 cWall; uniform vec3 cObstacle; uniform vec3 cDoor;
uniform vec3 cLine; uniform vec3 cAccent; uniform vec3 camPos;
uniform float sliceZ; uniform float heat; uniform float lo; uniform float hi; uniform float px;
uniform int nContour; uniform float contour[%(contours)d]; uniform float selWall; uniform float gridStep;
uniform float shadowRange;
uniform float waveOn; uniform int nSrc; uniform vec3 src[%(max_src)d]; uniform float phase; uniform float spacing;
uniform vec3 palette[%(palette)d];

float lum(vec3 c){ return dot(c, vec3(0.2126, 0.7152, 0.0722)); }
// where a wave front meets this surface: a thin ring, as strong as the signal there (0 where there is none)
float waveRing(vec3 q){
  if (waveOn < 0.5) return 0.0;
  float ring = 0.0;
  for (int s = 0; s < nSrc; s++) {
    float d = distance(q, src[s]), x = (d - phase) / spacing;
    if (x < -0.5) continue;
    float off = abs(x - floor(x + 0.5)) * spacing;                  // metres to the nearest front
    float w = max(fwidth(d), 1e-4), hw = 0.06 + 1.1 * w;
    ring = max(ring, (1.0 - smoothstep(hw - w, hw + w, off)) * smoothstep(0.2, 1.0, d));
  }
  float sig = clamp((texture(field, clamp(q / size, 0.0, 1.0)).r - lo) / (hi - lo), 0.0, 1.0);
  return ring * smoothstep(0.0, 0.15, sig) * (0.35 + 0.65 * sig);
}

// the field on the slice plane at a position in voxels (voxel centres at whole numbers): a cubic B-spline
// across the floor, made of four linear taps, so that contour lines come out smooth
float tap(vec2 p, float z){ return textureLod(field, vec3(p / res.xy, z), 0.0).r; }
float fieldAt(vec2 st){
  vec2 i = floor(st), f = st - i, f2 = f * f, f3 = f2 * f;
  vec2 w0 = (1.0 - 3.0 * f + 3.0 * f2 - f3) / 6.0, w1 = (3.0 * f3 - 6.0 * f2 + 4.0) / 6.0;
  vec2 w3 = f3 / 6.0, w2 = 1.0 - w0 - w1 - w3, g0 = w0 + w1, g1 = w2 + w3;
  vec2 p0 = i - 0.5 + w1 / g0, p1 = i + 1.5 + w3 / g1;
  float z = sliceZ / size.z;
  return g0.y * (g0.x * tap(p0, z) + g1.x * tap(vec2(p1.x, p0.y), z))
       + g1.y * (g0.x * tap(vec2(p0.x, p1.y), z) + g1.x * tap(p1, z));
}
float shadow(vec3 p, float ndl){
  vec3 q = (lightVP * vec4(p, 1.0)).xyz * 0.5 + 0.5;
  if (q.x < 0.0 || q.x > 1.0 || q.y < 0.0 || q.y > 1.0) return 1.0;
  float bias = mix(0.16, 0.04, ndl) / shadowRange, lit = 0.0, tx = 1.6 / %(shadow)d.0;
  for (int i = -2; i <= 2; i++) for (int j = -2; j <= 2; j++)
    lit += q.z - bias <= texture(shadowMap, q.xy + vec2(i, j) * tx).r ? 1.0 : 0.0;
  return lit / 25.0;
}
float gridLine(vec2 p, float every, float halfw){
  vec2 q = p / every, fw = max(fwidth(q), 1e-6), d = abs(fract(q - 0.5) - 0.5) / fw;
  vec2 a = (1.0 - smoothstep(halfw - 0.5, halfw + 0.5, d)) * (1.0 - smoothstep(0.12, 0.40, fw));
  return max(a.x, a.y);
}
void main(){
  int kind = int(kt.x + 0.5);
  vec3 n = normalize(wnor);
  float ndl = max(dot(n, lightDir), 0.0);
  vec2 st = wpos.xy / size.xy * res.xy - 0.5, sx = dFdx(st), sy = dFdy(st);     // voxels, and voxels per pixel
  if (kind == 1) {                                  // the floor: data, never lit while the heat map is on
    vec3 c = cFloor;
    if (heat > 0.5) {
      float v = fieldAt(st);
      float rough = max(length(vec2(dFdx(v), dFdy(v))), 1e-6);      // dB per pixel
      float t = clamp((v - lo) / (hi - lo), 0.0, 1.0);
      vec3 hc = textureLod(ramp, vec2((t * 255.0 + 0.5) / 256.0, 0.5), 0.0).rgb;
      c = mix(c, hc, clamp((v - lo) / rough + 0.5, 0.0, 1.0));
      float off = 1e9, hw = 0.75 * px;
      for (int i = 0; i < nContour; i++) off = min(off, abs(v - contour[i]));
      if (nContour > 0) {                           // the exact slope of the field gives an even line
        vec2 gv = vec2(fieldAt(st + vec2(0.5, 0.0)) - fieldAt(st - vec2(0.5, 0.0)),
                       fieldAt(st + vec2(0.0, 0.5)) - fieldAt(st - vec2(0.0, 0.5)));      // dB per voxel
        float g = max(length(vec2(dot(gv, sx), dot(gv, sy))), 1e-6);                        // dB per pixel
        float keep = (1.0 - smoothstep(0.8, 1.5, off))              // no blot where the field lies flat,
                   * (1.0 - smoothstep(0.7, 2.2, g * px))           // no lines where they would crowd,
                   * (1.0 - smoothstep(5.0, 8.0, length(gv)));      // nor where the model jumps (a wall's shadow)
        float line = (1.0 - smoothstep(hw - 0.5, hw + 0.5, off / g)) * keep;
        float casing = (1.0 - smoothstep(hw + 1.1 * px - 0.5, hw + 1.1 * px + 0.5, off / g)) * keep;
        c = mix(c, vec3(1.0), 0.55 * casing);                       // a light casing, then the dark line in it
        c = mix(c, cLine, 0.92 * line);
      }
    } else {
      c *= 1.0 - 0.26 * (1.0 - shadow(wpos + n * 0.03, ndl));
    }
    c = mix(c, vec3(1.0), 0.70 * waveRing(wpos));
    float ga = max(0.11 * gridLine(wpos.xy, gridStep, 0.5 * px), 0.20 * gridLine(wpos.xy, gridStep * 5.0, 0.7 * px));
    vec3 other = vec3(1.0 - smoothstep(0.35, 0.55, lum(c)));
    c = mix(c, mix(other, cLine, smoothstep(0.08, 0.25, abs(lum(cLine) - lum(c)))), ga);
    frag = vec4(c, 1.0); return;
  }
  float sh = shadow(wpos + n * 0.03, ndl);
  if (kind == 0) {                                  // the ground outside: background with a faint shadow
    float d = length(max(max(-wpos.xy - %(wall)f, wpos.xy - size.xy - %(wall)f), 0.0));
    float thrown = 0.22 * (1.0 - sh) * (1.0 - smoothstep(2.0, 14.0, d));
    frag = vec4(cBg * (1.0 - max(thrown, 0.20 * exp(-d / 0.8))), 1.0); return;
  }
  if (kind == 6) {                                  // window glass: a light tint, more opaque when seen edge-on
    float edgeOn = 1.0 - abs(dot(n, normalize(wpos - camPos)));
    frag = vec4(mix(vec3(0.62, 0.80, 0.90), vec3(1.0), 0.25 * ndl), 0.20 + 0.35 * edgeOn * edgeOn); return;
  }
  vec3 base = kind == 2 ? cWall : kind == 4 ? cDoor : kind == 5 ? cDoor * 0.62
            : kind == 7 ? vec3(0.80, 0.81, 0.83) : kind == 8 ? vec3(0.70, 0.72, 0.74) : kind == 9 ? vec3(0.44, 0.47, 0.51)
            : (kt.y >= 0.0 ? palette[int(kt.y + 0.5)] : cObstacle);
  if (kind == 2 && abs(kt.y - selWall) < 0.5) base = mix(base, cAccent, 0.28);
  float light = mix(0.40, 0.92, 0.5 + 0.5 * n.z) + 0.52 * ndl * sh;
  if (n.z > 0.5) light += 0.18;                                             // tops catch the light
  if (n.z < 0.5) light *= mix(0.70, 1.0, smoothstep(0.0, 0.9, wpos.z));     // darker where it meets the floor
  vec3 c = base * light;
  float ring = waveRing(wpos + n * 0.01);
  if (ring > 0.0) {                                 // a front sweeping over a wall takes the signal's colour
    float sig = clamp((texture(field, clamp(wpos / size, 0.0, 1.0)).r - lo) / (hi - lo), 0.0, 1.0);
    vec3 rc = textureLod(ramp, vec2((sig * 255.0 + 0.5) / 256.0, 0.5), 0.0).rgb;
    c = mix(c, mix(rc, vec3(1.0), 0.35), 0.80 * ring);
  }
  frag = vec4(c, 1.0);
}
""" % {"contours": MAX_CONTOURS, "shadow": SHADOW_PX, "wall": WALL_T, "max_src": MAX_WAVE_SOURCES,
       "palette": len(FURNITURE_RGB)}

FULL_VS = """#version 330 core
void main(){ gl_Position = vec4(gl_VertexID == 1 ? 3.0 : -1.0, gl_VertexID == 2 ? 3.0 : -1.0, 0.0, 1.0); }
"""

# The radio field itself, one horizontal layer of voxels per draw: the model of wifi_site.Site.rssi (free-space
# loss at 1 m, a clutter exponent beyond 1 m, a loss per wall / door crossed on the straight line, dB per metre
# inside an obstacle, capped), strongest access point wins.  The model comes packed by field_tables().
FIELD_FS = """#version 330 core
out vec4 frag;
uniform sampler2D geo; uniform int nAp; uniform int nWall; uniform int nObs;
uniform vec3 size; uniform vec3 res; uniform float layer;
vec4 G(int i){ return texelFetch(geo, ivec2(i %% %(row)d, i / %(row)d), 0); }
void main(){
  vec3 p = vec3(gl_FragCoord.xy, layer + 0.5) * size / res;          // the voxel centre, metres
  int w0 = 2 * nAp, o0 = w0 + 3 * nWall;
  float best = %(none)f;
  for (int k = 0; k < nAp; k++) {
    vec4 A = G(2 * k), B = G(2 * k + 1);
    vec3 a = A.xyz;
    float dist = distance(a, p), d = max(dist, 0.1);
    float loss = B.x + 10.0 * (d > 1.0 ? B.y : 2.0) * log2(d) * 0.30102999566;
    int band = int(B.z + 0.5);
    for (int i = 0; i < nWall; i++) {
      vec4 W = G(w0 + 3 * i);
      int ax = W.x > 0.5 ? 1 : 0, along = 1 - ax;
      float da = a[ax] - W.y, db = p[ax] - W.y;
      if (da * db >= 0.0) continue;                                    // both ends on one side
      float t = da / (da - db), run = a[along] + (p[along] - a[along]) * t;
      if (run < W.z || run > W.w) continue;
      float z = a.z + (p.z - a.z) * t, here = G(w0 + 3 * i + 1)[band];
      vec4 R = G(w0 + 3 * i + 2);
      int first = int(R.x + 0.5), cnt = int(R.y + 0.5);
      for (int j = 0; j < cnt; j++) {                                  // openings never overlap: first match
        vec4 H = G(first + 2 * j);
        if (run >= H.x && run <= H.y && z >= H.z && z <= H.w) { here = G(first + 2 * j + 1)[band]; break; }
      }
      loss += here;
    }
    for (int i = 0; i < nObs; i++) {
      vec4 lo = G(o0 + 4 * i), hi = G(o0 + 4 * i + 1);
      float t0 = 0.0, t1 = 1.0;
      for (int c = 0; c < 3; c++) {
        float dc = p[c] - a[c];
        if (abs(dc) < 1e-12) {
          if (a[c] < lo[c] || a[c] > hi[c]) { t1 = -1.0; break; }
          continue;
        }
        float ta = (lo[c] - a[c]) / dc, tb = (hi[c] - a[c]) / dc;
        t0 = max(t0, min(ta, tb)); t1 = min(t1, max(ta, tb));
        if (t1 - t0 <= 1e-12) { t1 = -1.0; break; }
      }
      if (t1 > t0) loss += min(G(o0 + 4 * i + 3)[band], G(o0 + 4 * i + 2)[band] * (t1 - t0) * dist);
    }
    best = max(best, A.w - loss);
  }
  frag = vec4(best, 0.0, 0.0, 1.0);
}
""" % {"row": GEO_ROW, "none": NO_FIELD_DBM}

# One ray per glow pixel through the room box, front to back, jittered, stopped by the solids.
GLOW_FS = """#version 330 core
out vec4 frag;
uniform sampler3D field; uniform sampler2D ramp; uniform sampler2D distTex;
uniform vec3 size; uniform vec3 eye; uniform vec3 camF; uniform vec3 camR; uniform vec3 camU;
uniform vec2 centre; uniform float focal; uniform float lo; uniform float hi; uniform float strength;
uniform float stepLen; uniform vec3 fadeLo; uniform vec3 fadeHi; uniform float tune[10]; uniform int div;
uniform float dark; uniform float clearDist;
uniform sampler3D noiseTex; uniform float time; uniform float detail; uniform float cap;
float ign(vec2 p){ return fract(52.9829189 * fract(dot(p, vec2(0.06711056, 0.00583715)))); }
// billows: two octaves of a tiling noise, drifting slowly; 0..1, about 0.5 on average
float billow(vec3 p){
  return 0.62 * texture(noiseTex, p / %(big)f + vec3(0.021, 0.013, 0.004) * time).r
       + 0.38 * texture(noiseTex, p / %(small)f - vec3(0.016, -0.010, 0.007) * time + 0.37).r;
}
void main(){
  ivec2 full = ivec2(gl_FragCoord.xy) * div;                       // the frame pixel this ray stands for
  vec2 fp = vec2(full) + 0.5;
  vec3 rd = normalize(camF * focal + camR * (fp.x - centre.x) + camU * (centre.y - fp.y));
  vec3 inv = 1.0 / (rd + vec3(equal(rd, vec3(0.0))) * 1e-9);
  vec3 t0 = -eye * inv, t1 = (size - eye) * inv, a = min(t0, t1), b = max(t0, t1);
  float tmin = max(max(a.x, a.y), max(a.z, 0.0)), tmax = min(min(b.x, b.y), b.z);
  tmax = min(tmax, texelFetch(distTex, min(full, textureSize(distTex, 0) - 1), 0).r);
  float len = tmax - tmin;
  if (len <= 0.0) { frag = vec4(0.0); return; }
  float n = clamp(ceil(len / stepLen), 1.0, %(steps)d.0), dt = len / n, jit = ign(gl_FragCoord.xy);
  float white0 = mix(tune[7], tune[4], dark), white1 = mix(tune[8], tune[5], dark), gain = mix(tune[9], tune[6], dark);
  vec3 acc = vec3(0.0); float trans = 1.0;
  for (int i = 0; i < int(n); i++) {
    float t = tmin + (float(i) + jit) * dt;
    vec3 p = eye + rd * t;
    float s = (texture(field, p / size).r - lo) / (hi - lo);
    if (s <= 0.0) continue;
    vec3 e = min(p * fadeLo, (size - p) * fadeHi);                  // soft where a side of the box is open
    float edge = clamp(min(e.x, min(e.y, e.z)), 0.0, 1.0);
    // a haze that is full at hi_dbm, plus a bright core round the access point
    float haze = exp(tune[0] * (min(s, 1.0) - 1.0)), core = tune[3] * exp(tune[2] * (min(s, tune[1]) - 1.0));
    float dens = strength * (haze + core) * smoothstep(0.0, 0.14, s);
    dens *= edge * edge * (3.0 - 2.0 * edge) * smoothstep(0.0, clearDist, t);   // and clear round the viewer
    // fog: the density billows round the signal's own value (the average is kept), and a billow is lit
    // on its top and shaded underneath, which is what makes it read as a volume
    float b = billow(p), above = billow(p + vec3(0.0, 0.0, 0.55));
    dens *= mix(1.0, 0.12 + 1.76 * smoothstep(0.30, 0.72, b), detail);
    float lit = mix(1.0, clamp(1.0 + 3.2 * (b - above), 0.45, 1.55), detail);
    float al = 1.0 - exp(-dens * dt);
    vec3 c = texture(ramp, vec2((clamp(s, 0.0, 1.0) * 255.0 + 0.5) / 256.0, 0.5)).rgb;
    float tops = detail * smoothstep(1.0, 1.45, lit);              // the lit tops of billows turn misty white
    c = mix(c, vec3(1.0), min(white0 + white1 * smoothstep(0.85, 1.5, s) + 0.42 * tops, 0.85)) * gain * lit;
    acc += trans * al * c; trans *= 1.0 - al;
    if (trans < 0.01) break;
  }
  frag = vec4(acc, 1.0 - trans) * cap;
}
""" % {"steps": GLOW_STEPS, "big": FOG_SCALE[0], "small": FOG_SCALE[1]}

# Waves: the access points send out shells (spheres) that grow at a steady pace in every direction.  Each ray
# finds where it meets the shells exactly (no marching, so the shells stay sharp); a shell takes the colour of
# the signal where it is and fades with it, so it dims behind every wall and obstacle and is gone where there
# is no service.  A shell seen edge-on is brighter (more of it along the view), which draws it as rings.
WAVE_FS = """#version 330 core
out vec4 frag;
uniform sampler3D field; uniform sampler2D ramp; uniform sampler2D distTex;
uniform vec3 size; uniform vec3 eye; uniform vec3 camF; uniform vec3 camR; uniform vec3 camU;
uniform vec2 centre; uniform float focal; uniform float lo; uniform float hi; uniform int div;
uniform int nSrc; uniform vec3 src[%(max_src)d];
uniform float phase; uniform float spacing; uniform float strength; uniform float dark; uniform float cap;
uniform float clearDist;
void main(){
  ivec2 full = ivec2(gl_FragCoord.xy) * div;
  vec2 fp = vec2(full) + 0.5;
  vec3 rd = normalize(camF * focal + camR * (fp.x - centre.x) + camU * (centre.y - fp.y));
  vec3 inv = 1.0 / (rd + vec3(equal(rd, vec3(0.0))) * 1e-9);
  vec3 t0 = -eye * inv, t1 = (size - eye) * inv, a = min(t0, t1), b = max(t0, t1);
  float tmin = max(max(a.x, a.y), max(a.z, 0.0)), tmax = min(min(b.x, b.y), b.z);
  tmax = min(tmax, texelFetch(distTex, min(full, textureSize(distTex, 0) - 1), 0).r);
  if (tmax <= tmin) { frag = vec4(0.0); return; }
  vec3 sumC = vec3(0.0); float sumA = 0.0, keep = 1.0;
  for (int s = 0; s < nSrc; s++) {
    vec3 oc = eye - src[s];
    float bb = dot(oc, rd), cc = dot(oc, oc);
    float tc = clamp(-bb, tmin, tmax);                                 // the shells this stretch can meet
    float rmin = length(oc + rd * tc), rmax = max(length(oc + rd * tmin), length(oc + rd * tmax));
    int k0 = max(0, int(ceil((rmin - phase) / spacing))), k1 = min(int(floor((rmax - phase) / spacing)), k0 + 64);
    for (int k = k0; k <= k1; k++) {
      float R = phase + float(k) * spacing, disc = bb * bb - (cc - R * R);
      if (disc < 0.0) continue;
      float sq = sqrt(disc);
      for (int side = 0; side < 2; side++) {
        float t = -bb + (side == 0 ? -sq : sq);
        if (t < tmin || t > tmax) continue;
        vec3 p = eye + rd * t;
        float sig = (texture(field, p / size).r - lo) / (hi - lo);
        if (sig <= 0.0) continue;
        float facing = abs(dot(rd, (p - src[s]) / R));
        float al = strength * (0.30 + 0.70 * min(sig, 1.0)) * smoothstep(0.0, 0.18, sig) / max(facing, 0.16);
        al *= smoothstep(0.15, 0.9, R) * smoothstep(0.0, clearDist, t);  // no blob on the source or the eye
        al = 1.0 - exp(-al);
        vec3 c = texture(ramp, vec2((clamp(sig, 0.0, 1.0) * 255.0 + 0.5) / 256.0, 0.5)).rgb;
        c = mix(c, vec3(1.0), mix(0.10, 0.22, dark) * smoothstep(0.6, 1.2, sig)) * mix(0.92, 1.15, dark);
        sumC += c * al; sumA += al; keep *= 1.0 - al;
      }
    }
  }
  float A = (1.0 - keep) * cap;
  frag = vec4(sumA > 0.0 ? sumC / sumA * A : vec3(0.0), A);
}
""" % {"max_src": MAX_WAVE_SOURCES}

# Outlines: where the distance to the eye jumps (a silhouette), a dark line.  Creases (wall meets floor) bend the
# distance only a little and stay unlined, so the picture is not cluttered.
EDGE_FS = """#version 330 core
out vec4 frag;
uniform sampler2D distTex; uniform vec3 ink; uniform float strength; uniform int reach;
float D(ivec2 q){ return texelFetch(distTex, clamp(q, ivec2(0), textureSize(distTex, 0) - 1), 0).r; }
void main(){
  ivec2 p = ivec2(gl_FragCoord.xy);
  float d = D(p), e = 0.0;
  ivec2 ks[4] = ivec2[4](ivec2(1, 0), ivec2(0, 1), ivec2(1, 1), ivec2(1, -1));
  for (int i = 0; i < 4; i++) {
    float a = D(p + ks[i] * reach), b = D(p - ks[i] * reach);
    e = max(e, abs(a + b - 2.0 * d) / max(min(d, min(a, b)), 0.05));
  }
  float edge = smoothstep(0.012, 0.045, e) * strength;
  frag = vec4(ink * edge, edge);
}
"""

# The glow laid over the frame: each frame pixel takes the glow rays around it whose depth matches its own.
COMPOSITE_FS = """#version 330 core
out vec4 frag;
uniform sampler2D glowTex; uniform sampler2D distTex; uniform int div;
float ign(vec2 p){ return fract(52.9829189 * fract(dot(p, vec2(0.06711056, 0.00583715)))); }
void main(){
  ivec2 gmax = textureSize(glowTex, 0) - 1, dmax = textureSize(distTex, 0) - 1;
  float d = texelFetch(distTex, ivec2(gl_FragCoord.xy), 0).r;
  vec2 g = (gl_FragCoord.xy - 0.5) / float(div), b = floor(g), f = g - b;
  vec4 sum = vec4(0.0); float wsum = 0.0;
  for (int j = 0; j < 2; j++) for (int i = 0; i < 2; i++) {
    ivec2 q = min(ivec2(b) + ivec2(i, j), gmax);
    float dq = texelFetch(distTex, min(q * div, dmax), 0).r;
    float w = (i == 0 ? 1.0 - f.x : f.x) * (j == 0 ? 1.0 - f.y : f.y) + 1e-4;
    w /= 0.008 + abs(d - dq) / max(min(d, dq), 0.05);
    sum += w * texelFetch(glowTex, q, 0); wsum += w;
  }
  vec4 c = sum / wsum;
  c.rgb += (ign(gl_FragCoord.xy + 17.0) - 0.5) / 255.0 * step(0.002, c.a);      // no banding in 8 bits
  frag = vec4(max(c.rgb, 0.0), c.a);
}
"""

# How the glow follows the signal (s = 0 at lo_dbm, 1 at hi_dbm):  density = haze + core, with
# haze = exp(k_haze * (s - 1)), core = amount * exp(k_core * (s - 1)), s held at s_cap;  then, for a dark and
# for a light background: white mixed into the ramp colour, more white in the core, brightness.
GLOW_TUNE = (4.2, 1.6, 9.0, 0.30,   0.16, 0.30, 1.25,   0.10, 0.18, 1.0)
GLOW_FADE = (0.16, 0.14)             # the glow thins out towards an open side / the open top over this share
GLOW_GAIN = 7.0                     # optical depth of the haze along the view at full signal and glow = 1


# ============================================================================ the renderer
class SceneRenderer:
    """Draws one site into a QImage on the GPU.  Get one with ``SceneRenderer.create()``."""

    def __init__(self):                                   # use create(): this only declares the fields
        self.renderer_name = ""
        self.version = ""
        self.last_ms = 0.0
        self.last_error = ""
        self._ctx = self._surface = self._fn = None
        self._progs: dict = {}
        self._fbos: dict = {}
        self._vao = self._vao_full = self._vbo = None
        self._field_tex = self._ramp_tex = None
        self._noise_tex = self._geo_tex = None
        self._xfn = None
        self._max_3d = 256
        self._field_shape = (1, 1, 1)
        self.field_on_gpu = False                         # True while the field shown was computed here
        self.field_ms = 0.0                               # time the last compute_field took
        self._geom = None
        self._mesh_key = None
        self._counts = (0, 0, 0, 0)

    # ------------------------------------------------------------------ life cycle
    @classmethod
    def create(cls) -> tuple["SceneRenderer | None", str]:
        """``(renderer, "")``, or ``(None, reason)`` when this machine cannot draw the view on the GPU."""
        if _IMPORT_ERROR:
            return None, f"a module the GPU view needs is missing: {_IMPORT_ERROR}"
        if QGuiApplication.instance() is None:
            return None, "no QGuiApplication is running"
        self = cls()
        try:
            reason = self._open()
        except Exception as e:                            # never let a driver quirk take the app down
            reason = f"OpenGL setup failed: {type(e).__name__}: {e}"
        if reason:
            self.close()
            return None, reason
        return self, ""

    def _open(self) -> str:
        fmt = QSurfaceFormat()
        fmt.setRenderableType(QSurfaceFormat.OpenGL)      # Wayland / EGL hands out OpenGL ES otherwise
        fmt.setVersion(3, 3)
        fmt.setProfile(QSurfaceFormat.CoreProfile)
        fmt.setDepthBufferSize(24)
        ctx = QOpenGLContext()
        ctx.setFormat(fmt)
        if not ctx.create():
            return "no OpenGL context could be created (no display, or no OpenGL 3.3 driver)"
        self._ctx = ctx
        surface = QOffscreenSurface()
        surface.setFormat(ctx.format())
        surface.create()
        self._surface = surface
        if not surface.isValid() or not ctx.makeCurrent(surface):
            return "the OpenGL context could not be made current on an off-screen surface"
        fn = self._fn = ctx.functions()
        # what the driver really gives; ctx.format() only repeats the request on some platforms
        self.version = fn.glGetString(GL_VERSION) or ""
        self.renderer_name = fn.glGetString(GL_RENDERER) or ""
        problem = gl_problem(self.version, self.renderer_name, fn.glGetString(GL_SHADING_LANGUAGE_VERSION) or "",
                             software_allowed())
        if problem:
            return problem
        self._progs = {"scene": self._program(SCENE_VS, SCENE_FS), "depth": self._program(SCENE_VS, DEPTH_FS),
                       "glow": self._program(FULL_VS, GLOW_FS), "composite": self._program(FULL_VS, COMPOSITE_FS),
                       "edge": self._program(FULL_VS, EDGE_FS), "field": self._program(FULL_VS, FIELD_FS),
                       "waves": self._program(FULL_VS, WAVE_FS)}
        self._xfn = ctx.extraFunctions()
        try:
            self._max_3d = int(fn.glGetIntegerv(GL_MAX_3D_TEXTURE_SIZE) or 256)
        except Exception:                                 # the binding returns nothing useful: the 3.3 minimum
            self._max_3d = 256
        self._vao_full = QOpenGLVertexArrayObject()
        self._vao = QOpenGLVertexArrayObject()
        if not (self._vao_full.create() and self._vao.create()):
            return "vertex array objects are not available"
        self._vao.bind()
        self._vbo = QOpenGLBuffer()
        self._vbo.create()
        self._vbo.bind()
        for p in (self._progs["scene"], self._progs["depth"]):
            p.bind()
            for loc, n, off in ((0, 3, 0), (1, 3, 12), (2, 2, 24)):
                p.enableAttributeArray(loc)
                p.setAttributeBuffer(loc, GL_FLOAT, off, n, STRIDE * 4)
        self._vao.release()
        self._upload_field(np.full((1, 1, 1), NO_FIELD_DBM, np.float32))
        self._upload_ramp(default_ramp())
        app = QGuiApplication.instance()
        ref = weakref.ref(self)
        app.aboutToQuit.connect(lambda: ref() and ref().close())      # free the GL objects before Qt goes
        return ""

    @staticmethod
    def _program(vs: str, fs: str):
        p = QOpenGLShaderProgram()
        if not (p.addShaderFromSourceCode(QOpenGLShader.Vertex, vs)
                and p.addShaderFromSourceCode(QOpenGLShader.Fragment, fs) and p.link()):
            raise RuntimeError("a shader did not compile: " + (p.log() or "no log").strip()[:300])
        return p

    def _current(self) -> bool:
        ctx = self._ctx
        if ctx is None or self._fn is None:
            return False
        if QOpenGLContext.currentContext() is ctx:
            return True
        return bool(ctx.makeCurrent(self._surface))

    def close(self) -> None:
        """Free everything.  Safe to call twice; render() afterwards returns a plain background."""
        ctx, self._ctx = self._ctx, None
        if ctx is None:
            return
        try:
            if self._surface is not None and ctx.makeCurrent(self._surface):
                for tex in (self._field_tex, self._ramp_tex, self._noise_tex, self._geo_tex):
                    if tex is not None:
                        tex.destroy()
                self._fbos.clear()
                self._progs.clear()
                for obj in (self._vbo, self._vao, self._vao_full):
                    if obj is not None:
                        obj.destroy()
                ctx.doneCurrent()
        except Exception as e:                            # a dying driver must not stop the app closing
            self.last_error = f"{type(e).__name__}: {e}"
        self._field_tex = self._ramp_tex = self._noise_tex = self._geo_tex = None
        self._vbo = self._vao = self._vao_full = self._fn = self._xfn = None
        self._fbos, self._progs = {}, {}
        if self._surface is not None:
            self._surface.destroy()
        self._surface = None

    # ------------------------------------------------------------------ what to draw
    def set_site(self, site) -> None:
        """Rebuild the geometry: call when the room, a wall, a door or an obstacle changed."""
        try:
            self._geom = site_geometry(site)
        except Exception as e:
            self.last_error = f"site not usable: {type(e).__name__}: {e}"
            return
        self._mesh_key = None

    def set_field(self, volume) -> bool:
        """The signal in dBm at voxel centres, ``volume[z, y, x]``, spanning the whole room box.
        Returns False (and keeps the previous field) when the array cannot be used."""
        try:
            arr = np.asarray(volume, dtype=np.float32)
            if arr.ndim != 3 or min(arr.shape) < 1 or max(arr.shape) > MAX_FIELD_SIDE:
                self.last_error = f"field of shape {getattr(arr, 'shape', None)} ignored: need (nz, ny, nx)"
                return False
            arr = np.nan_to_num(arr, nan=NO_FIELD_DBM, posinf=NO_FIELD_DBM, neginf=NO_FIELD_DBM)
            arr = np.ascontiguousarray(np.clip(arr, NO_FIELD_DBM, 60.0), dtype=np.float32)
            if not self._current():
                return False
            self._upload_field(arr)
            self.field_on_gpu = False
            return True
        except Exception as e:
            self.last_error = f"field not usable: {type(e).__name__}: {e}"
            return False

    def compute_field(self, site, budget: int = GPU_VOXEL_BUDGET, band: str | None = None) -> bool:
        """Work out the signal of ``site`` on the graphics card, straight into the field texture, on a grid
        about twenty times finer than the CPU's (``field_grid``).  The numbers are those of ``Site.rssi``.
        Returns False (and keeps the previous field) when it cannot; the caller then hands over a volume
        computed on the CPU with ``set_field``."""
        t0 = time.perf_counter()
        try:
            if not self._current():
                return False
            nx, ny, nz = field_grid(site.width, site.depth, site.height, budget,
                                    min(MAX_FIELD_SIDE, max(16, self._max_3d)))
            texels, n_ap, n_wall, n_obs = field_tables(site, band)
            self._upload_geo(texels)
            self._alloc_field(nx, ny, nz)
            fn, xfn = self._fn, self._xfn
            layer = self._fbo("field_layer", nx, ny, GL_R32F)
            layer.bind()
            fn.glViewport(0, 0, nx, ny)
            fn.glDisable(GL_DEPTH_TEST)
            fn.glDisable(GL_BLEND)
            self._vao_full.bind()
            p = self._progs["field"]
            p.bind()
            self._geo_tex.bind(0)
            p.setUniformValue1i("geo", 0)
            p.setUniformValue1i("nAp", n_ap)
            p.setUniformValue1i("nWall", n_wall)
            p.setUniformValue1i("nObs", n_obs)
            p.setUniformValue("size", QVector3D(float(site.width), float(site.depth), float(site.height)))
            p.setUniformValue("res", QVector3D(nx, ny, nz))
            self._field_tex.bind(1)
            for k in range(nz):
                p.setUniformValue1f("layer", float(k))
                fn.glDrawArrays(GL_TRIANGLES, 0, 3)
                fn.glActiveTexture(GL_TEXTURE0 + 1)       # the layer just drawn -> slice k of the field
                xfn.glCopyTexSubImage3D(GL_TEXTURE_3D, 0, 0, 0, k, 0, 0, nx, ny)
                fn.glActiveTexture(GL_TEXTURE0)
            layer.release()
            fn.glFinish()
            self.field_on_gpu = True
            return True
        except Exception as e:
            self.last_error = f"field not computed on the GPU: {type(e).__name__}: {e}"
            return False
        finally:
            self.field_ms = (time.perf_counter() - t0) * 1000.0

    @property
    def field_shape(self) -> tuple:
        """(nx, ny, nz) of the field texture."""
        return self._field_shape

    def read_field_layer(self, k: int):
        """Voxel layer ``k`` of the field last made by ``compute_field``, ``[y, x]`` float32 dBm, read back from
        the card (for checks: the view itself never reads the field back).  None when there is none."""
        if not self.field_on_gpu or not self._current():
            return None
        nx, ny, nz = self._field_shape
        if not 0 <= k < nz:
            return None
        layer = self._fbos["field_layer"]
        layer.bind()
        fn = self._fn
        fn.glViewport(0, 0, nx, ny)
        fn.glDisable(GL_DEPTH_TEST)
        fn.glDisable(GL_BLEND)
        self._vao_full.bind()
        p = self._progs["field"]                          # its uniforms are still those of the last compute
        p.bind()
        self._geo_tex.bind(0)
        p.setUniformValue1f("layer", float(k))
        fn.glDrawArrays(GL_TRIANGLES, 0, 3)
        out = np.zeros((ny, nx), np.float32)
        fn.glPixelStorei(GL_PACK_ALIGNMENT, 4)
        fn.glReadPixels(0, 0, nx, ny, GL_RED, GL_FLOAT, out)
        layer.release()
        return out

    def _upload_geo(self, texels) -> None:
        """The packed radio model as an RGBA32F texture, GEO_ROW texels a row."""
        n = len(texels)
        rows = (n + GEO_ROW - 1) // GEO_ROW
        data = np.zeros((rows * GEO_ROW, 4), np.float32)
        data[:n] = texels
        tex = self._geo_tex
        if tex is None or tex.height() != rows:
            if tex is not None:
                tex.destroy()
            tex = QOpenGLTexture(QOpenGLTexture.Target2D)
            tex.setSize(GEO_ROW, rows)
            tex.setFormat(QOpenGLTexture.RGBA32F)
            tex.setMipLevels(1)
            tex.allocateStorage(QOpenGLTexture.RGBA, QOpenGLTexture.Float32)
            tex.setMinMagFilters(QOpenGLTexture.Nearest, QOpenGLTexture.Nearest)
            tex.setWrapMode(QOpenGLTexture.ClampToEdge)
            self._geo_tex = tex
        tex.setData(QOpenGLTexture.RGBA, QOpenGLTexture.Float32, data.tobytes())

    def _alloc_field(self, nx: int, ny: int, nz: int) -> None:
        tex = self._field_tex
        if tex is None or self._field_shape != (nx, ny, nz):
            if tex is not None:
                tex.destroy()
            tex = QOpenGLTexture(QOpenGLTexture.Target3D)
            tex.setSize(nx, ny, nz)
            tex.setFormat(QOpenGLTexture.R32F)
            tex.setMipLevels(1)
            tex.allocateStorage(QOpenGLTexture.Red, QOpenGLTexture.Float32)
            tex.setMinMagFilters(QOpenGLTexture.Linear, QOpenGLTexture.Linear)
            tex.setWrapMode(QOpenGLTexture.ClampToEdge)
            self._field_tex, self._field_shape = tex, (nx, ny, nz)

    def _upload_field(self, arr) -> None:
        nz, ny, nx = arr.shape
        self._alloc_field(nx, ny, nz)
        self._field_tex.setData(QOpenGLTexture.Red, QOpenGLTexture.Float32, arr.tobytes())

    def _noise(self):
        """A tiling 3D noise (smoothed white noise, made uniform), made once: what the fog billows with."""
        if self._noise_tex is None:
            tex = QOpenGLTexture(QOpenGLTexture.Target3D)
            n = FOG_NOISE_PX
            tex.setSize(n, n, n)
            tex.setFormat(QOpenGLTexture.R8_UNorm)
            tex.setMipLevels(1)
            tex.allocateStorage(QOpenGLTexture.Red, QOpenGLTexture.UInt8)
            tex.setMinMagFilters(QOpenGLTexture.Linear, QOpenGLTexture.Linear)
            tex.setWrapMode(QOpenGLTexture.Repeat)
            tex.setData(QOpenGLTexture.Red, QOpenGLTexture.UInt8, fog_noise(n).tobytes())
            self._noise_tex = tex
        return self._noise_tex

    def set_ramp(self, image) -> bool:
        """The colours of the heat map: a 256 x 1 QImage, pixel i for (v - lo_dbm) / (hi_dbm - lo_dbm) = i / 255."""
        try:
            if not isinstance(image, QImage) or image.isNull() or not self._current():
                return False
            self._upload_ramp(image)
            return True
        except Exception as e:
            self.last_error = f"ramp not usable: {type(e).__name__}: {e}"
            return False

    def _upload_ramp(self, image: QImage) -> None:
        img = image.convertToFormat(QImage.Format_RGBA8888)
        if img.width() != 256 or img.height() != 1:
            img = img.scaled(256, 1)
        if self._ramp_tex is not None:
            self._ramp_tex.destroy()
        tex = QOpenGLTexture(QOpenGLTexture.Target2D)
        tex.setSize(256, 1)
        tex.setFormat(QOpenGLTexture.RGBA8_UNorm)
        tex.setMipLevels(1)
        tex.allocateStorage(QOpenGLTexture.RGBA, QOpenGLTexture.UInt8)
        tex.setData(QOpenGLTexture.RGBA, QOpenGLTexture.UInt8, bytes(img.constBits())[:256 * 4])
        tex.setMinMagFilters(QOpenGLTexture.Linear, QOpenGLTexture.Linear)
        tex.setWrapMode(QOpenGLTexture.ClampToEdge)
        self._ramp_tex = tex

    # ------------------------------------------------------------------ one frame
    def render(self, width, height, eye, target, fov_y, opts, scale=1.0) -> QImage:
        """The scene seen from ``eye`` looking at ``target`` (metres, z up) as a QImage of ``width`` x
        ``height`` logical pixels, ``scale`` device pixels each.  Never raises: on any failure the picture
        is the plain background and ``last_error`` says why."""
        t0 = time.perf_counter()
        opts = opts if opts is not None else RenderOpts()
        img = None
        size = self._frame_size(width, height, scale)
        if size is not None:
            prev = QOpenGLContext.currentContext()
            prev_surface = prev.surface() if prev is not None and prev is not self._ctx else None
            try:
                if self._current():
                    img = self._draw(size, eye, target, float(fov_y), opts)
            except Exception as e:
                self.last_error = f"{type(e).__name__}: {e}"
            if prev_surface is not None:                  # leave another context the way we found it
                prev.makeCurrent(prev_surface)
            if img is None:
                img = QImage(size[2], size[3], QImage.Format_RGB32)
                img.fill(_qrgb(_rgb(opts.background, (0.07, 0.08, 0.09))))
            img.setDevicePixelRatio(size[4])
        self.last_ms = (time.perf_counter() - t0) * 1000.0
        return img if img is not None else QImage()

    def _fog_uniforms(self, p, o: RenderOpts, glow: float, size, eye, f, r, u, w, h, fov_y: float, cut: int) -> None:
        self._noise().bind(3)
        p.setUniformValue1i("noiseTex", 3)
        p.setUniformValue1f("time", float(_f(o.fog_time, 0.0)) % 10000.0)
        p.setUniformValue1f("detail", min(max(_f(o.fog_detail, 0.75), 0.0), 1.0))
        p.setUniformValue1f("cap", GLOW_CAP[0] + (GLOW_CAP[1] - GLOW_CAP[0]) * glow)
        # density per metre of air: set by how much air the view looks through, so a small office and a
        # large hall, a look from above and a look from inside all show a haze of the same weight
        ref = 0.6 * math.sqrt(size[2] * min(size[0], size[1]))
        for k, (ax, ay) in enumerate(((0.0, 0.0), (-0.4, -0.4), (0.4, -0.4), (-0.4, 0.4), (0.4, 0.4))):
            d = [f[i] * (h / 2.0) / math.tan(fov_y / 2.0) + r[i] * ax * w + u[i] * ay * h for i in range(3)]
            ref = max(ref, box_chord(eye, d, size) * (1.0 if k == 0 else 0.7))
        p.setUniformValue1f("strength", GLOW_GAIN * glow ** 1.3 / max(ref, 0.3))
        voxel = min(size[i] / self._field_shape[i] for i in range(3))
        p.setUniformValue1f("stepLen", max(0.6 * voxel, 0.02))
        side_fade = 1.0 / max(GLOW_FADE[0] * min(size[0], size[1]), 0.01)
        top_fade = 1.0 / max(GLOW_FADE[1] * size[2], 0.01)
        p.setUniformValue("fadeLo", QVector3D(side_fade if cut & 1 else 1e3, side_fade if cut & 4 else 1e3, 1e3))
        p.setUniformValue("fadeHi", QVector3D(side_fade if cut & 2 else 1e3, side_fade if cut & 8 else 1e3,
                                              top_fade))
        for i, v in enumerate(GLOW_TUNE):
            p.setUniformValue1f(f"tune[{i}]", v)

    @staticmethod
    def _wave_sources(p, o: RenderOpts | None) -> None:
        """Where the waves start and how far they have come; ``o`` None = no waves (rings off)."""
        srcs = []
        for pt in tuple(o.sources or ())[:MAX_WAVE_SOURCES] if o is not None else ():
            try:
                v = [float(c) for c in pt][:3]
            except (TypeError, ValueError):
                continue
            if len(v) == 3 and all(math.isfinite(c) for c in v):
                srcs.append(v)
        p.setUniformValue1f("waveOn", 1.0 if srcs else 0.0)
        p.setUniformValue1i("nSrc", len(srcs))
        for i, v in enumerate(srcs):
            p.setUniformValue(f"src[{i}]", QVector3D(*v))
        p.setUniformValue1f("phase", wave_phase(_f(o.fog_time, 0.0)) if o is not None else 0.0)
        p.setUniformValue1f("spacing", WAVE_SPACING)

    def _wave_uniforms(self, p, o: RenderOpts, glow: float, size) -> None:
        self._wave_sources(p, o)
        p.setUniformValue1f("strength", 0.05 + 0.30 * glow)
        p.setUniformValue1f("cap", WAVE_CAP[0] + (WAVE_CAP[1] - WAVE_CAP[0]) * glow)

    @staticmethod
    def _frame_size(width, height, scale) -> tuple | None:
        """(logical w, logical h, framebuffer w, framebuffer h, scale) or None for an empty picture."""
        try:
            w, h, s = float(width), float(height), float(scale)
        except (TypeError, ValueError):
            return None
        if not (math.isfinite(w) and math.isfinite(h) and w >= 1.0 and h >= 1.0):
            return None
        if not math.isfinite(s) or s <= 0.0:
            s = 1.0
        s = min(s, MAX_FRAME_PX / w, MAX_FRAME_PX / h)
        return w, h, max(1, round(w * s)), max(1, round(h * s)), s

    def _fbo(self, key: str, w: int, h: int, internal: int, depth: bool = False, samples: int = 0):
        fbo = self._fbos.get(key)
        if fbo is None or fbo.width() != w or fbo.height() != h:
            fbo = None
            self._fbos.pop(key, None)                     # free the old one first
            ff = QOpenGLFramebufferObjectFormat()
            ff.setAttachment(QOpenGLFramebufferObject.Depth if depth else QOpenGLFramebufferObject.NoAttachment)
            ff.setInternalTextureFormat(internal)
            if samples:
                ff.setSamples(samples)
            fbo = QOpenGLFramebufferObject(w, h, ff)
            if not fbo.isValid():
                raise RuntimeError(f"framebuffer '{key}' {w}x{h} could not be made")
            self._fbos[key] = fbo
        return fbo

    def _mesh(self, cut: int) -> None:
        """Upload the triangles for this set of cut-down walls (only when it changed)."""
        key = (id(self._geom), cut)
        if key == self._mesh_key:
            return
        if self._geom is None:
            self._counts = (0, 0, 0, 0)
        else:
            data, n_ground, n_floor, n_solid = build_mesh(self._geom, cut)
            raw = np.asarray(data, dtype=np.float32).tobytes()
            self._vbo.bind()
            self._vbo.allocate(raw, len(raw))
            self._counts = (n_ground, n_floor, n_solid, len(data) // STRIDE)
        self._mesh_key = key

    def _draw(self, frame: tuple, eye, target, fov_y: float, o: RenderOpts) -> QImage | None:
        w, h, out_w, out_h, scale = frame
        # at one device pixel per logical pixel the frame is drawn twice as large and averaged down: that is
        # what smooths the edges of the glow, which multisampling alone cannot reach
        ss = 2 if scale < 1.5 and out_w * out_h <= SUPERSAMPLE_PX else 1
        fw, fh = out_w * ss, out_h * ss
        samples = 4 if fw * fh <= 4_200_000 else 2        # by size only: dragging and resting share the buffers
        eye, target = [float(v) for v in eye][:3], [float(v) for v in target][:3]
        if len(eye) != 3 or len(target) != 3 or not all(math.isfinite(v) for v in (*eye, *target, fov_y)):
            raise ValueError("camera is not finite")
        fov_y = min(max(fov_y, 0.02), 3.0)
        fn = self._fn
        bg = _rgb(o.background, (0.07, 0.08, 0.09))
        size = self._geom["size"] if self._geom else (1.0, 1.0, 1.0)
        self._mesh(cut_mask(size, eye) if self._geom else 0)
        n_ground, n_floor, n_all, n_total = self._counts          # n_all: the solids; glass after them
        cut = self._mesh_key[1]

        # camera: the near plane moves out when the eye is far from the room (depth precision), never in
        centre = (size[0] / 2, size[1] / 2, size[2] / 2)
        radius = math.sqrt(size[0] ** 2 + size[1] ** 2 + size[2] ** 2) / 2 + 2 * WALL_T
        away = max(math.dist(eye, centre) - radius, 0.0)
        near, far = max(NEAR, 0.4 * away), (math.dist(eye, centre) + radius) * 6.0 + 60.0
        vp = QMatrix4x4(*view_projection(eye, target, fov_y, w, h, fw, fh, near, far))
        f, r, u = camera_basis(eye, target)

        # light: over the viewer's shoulder, so the faces in view are the lit ones from every side
        az = (math.atan2(-f[1], -f[0]) if math.hypot(f[0], f[1]) > 1e-6 else math.radians(-62.0)) + math.radians(28.0)
        el = math.radians(52.0)
        light = (math.cos(el) * math.cos(az), math.cos(el) * math.sin(az), math.sin(el))
        rad = radius + 1.0
        lvp = QMatrix4x4()
        lvp.ortho(-rad, rad, -rad, rad, 0.5, 2 * rad + 1.5)
        lvp.lookAt(QVector3D(*(centre[i] + light[i] * (rad + 1.0) for i in range(3))), QVector3D(*centre),
                   QVector3D(0, 0, 1))

        fn.glEnable(GL_DEPTH_TEST)
        fn.glDisable(GL_BLEND)
        fn.glDisable(GL_CULL_FACE)
        fn.glDepthMask(True)
        fn.glColorMask(True, True, True, True)
        self._vao.bind()
        glow = min(max(_f(o.glow, 0.0), 0.0), 1.0)
        lo = _f(o.lo_dbm, -85.0)
        hi = _f(o.hi_dbm, -40.0)
        if hi - lo < 1e-3:
            hi = lo + 1e-3
        do_glow = glow > 0.0 and n_all > 0

        # 1 shadow map, 2 eye distance of the solids
        p = self._progs["depth"]
        p.bind()
        shadow = self._fbo("shadow", SHADOW_PX, SHADOW_PX, GL_R32F, depth=True)
        shadow.bind()
        fn.glViewport(0, 0, SHADOW_PX, SHADOW_PX)
        fn.glClearColor(1.0, 1.0, 1.0, 1.0)
        fn.glClear(GL_COLOR_BUFFER_BIT | GL_DEPTH_BUFFER_BIT)
        p.setUniformValue("vp", lvp)
        p.setUniformValue1i("mode", 0)
        if n_all > n_floor:
            fn.glDrawArrays(GL_TRIANGLES, n_floor, n_all - n_floor)
        dist = None
        if True:                                          # the glow and the outlines both need it
            dist = self._fbo("dist", fw, fh, GL_R32F, depth=True)
            dist.bind()
            fn.glViewport(0, 0, fw, fh)
            fn.glClearColor(1e9, 1e9, 1e9, 1.0)
            fn.glClear(GL_COLOR_BUFFER_BIT | GL_DEPTH_BUFFER_BIT)
            p.setUniformValue("vp", vp)
            p.setUniformValue("eye", QVector3D(*eye))
            p.setUniformValue1i("mode", 1)
            fn.glDrawArrays(GL_TRIANGLES, n_ground, n_all - n_ground)

        # 3 the room
        try:
            target_fbo = self._fbo("msaa", fw, fh, GL_RGB8, depth=True, samples=samples)
            final = self._fbo("final", fw, fh, GL_RGB8)
        except RuntimeError:                              # no multisampling here: draw straight into the frame
            self._fbos.pop("msaa", None)
            target_fbo = final = self._fbo("final_depth", fw, fh, GL_RGB8, depth=True)
        target_fbo.bind()
        fn.glViewport(0, 0, fw, fh)
        fn.glClearColor(bg[0], bg[1], bg[2], 1.0)
        fn.glClear(GL_COLOR_BUFFER_BIT | GL_DEPTH_BUFFER_BIT)
        if n_all:
            p = self._progs["scene"]
            p.bind()
            self._field_tex.bind(0)
            self._ramp_tex.bind(1)
            fn.glActiveTexture(GL_TEXTURE0 + 2)
            fn.glBindTexture(GL_TEXTURE_2D, shadow.texture())
            p.setUniformValue1i("field", 0)
            p.setUniformValue1i("ramp", 1)
            p.setUniformValue1i("shadowMap", 2)
            p.setUniformValue("vp", vp)
            p.setUniformValue("lightVP", lvp)
            p.setUniformValue("lightDir", QVector3D(*light))
            p.setUniformValue1f("shadowRange", 2 * rad + 1.0)
            p.setUniformValue("size", QVector3D(*size))
            p.setUniformValue("res", QVector3D(*self._field_shape))
            dflt = RenderOpts()
            for name, field in (("cBg", "background"), ("cFloor", "floor"), ("cWall", "wall"), ("cDoor", "door"),
                                ("cObstacle", "obstacle"), ("cLine", "line"), ("cAccent", "accent")):
                p.setUniformValue(name, QVector3D(*_rgb(getattr(o, field, None), getattr(dflt, field))))
            p.setUniformValue1f("sliceZ", min(max(_f(o.slice_z, 1.0), 0.0), size[2]))
            p.setUniformValue1f("heat", 1.0 if o.heat else 0.0)
            p.setUniformValue1f("lo", lo)
            p.setUniformValue1f("hi", hi)
            p.setUniformValue1f("px", fw / w)
            levels = [v for v in (_f(c, math.nan) for c in tuple(o.contours or ())[:MAX_CONTOURS]) if math.isfinite(v)]
            p.setUniformValue1i("nContour", len(levels))
            for i, v in enumerate(levels):
                p.setUniformValue1f(f"contour[{i}]", v)
            p.setUniformValue1f("selWall", float(_f(o.selected_wall, -1.0)) if _f(o.selected_wall, -1) >= 0 else -9.0)
            side = max(size[0], size[1])
            p.setUniformValue1f("gridStep", 1.0 if side <= 60 else 5.0 if side <= 250 else 10.0)
            p.setUniformValue("camPos", QVector3D(*eye))
            for i, k in enumerate(PALETTE):
                p.setUniformValue(f"palette[{i}]", QVector3D(*FURNITURE_RGB[k]))
            self._wave_sources(p, o if (str(o.air) == "waves" and glow > 0.0) else None)
            fn.glDrawArrays(GL_TRIANGLES, 0, n_all)
            if n_total > n_all:                           # window glass: see-through, over what is behind it
                fn.glEnable(GL_BLEND)
                fn.glBlendFunc(GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA)
                fn.glDepthMask(False)
                fn.glDrawArrays(GL_TRIANGLES, n_all, n_total - n_all)
                fn.glDepthMask(True)
                fn.glDisable(GL_BLEND)
        if target_fbo is not final:
            QOpenGLFramebufferObject.blitFramebuffer(final, target_fbo)

        # 4 the glow in the air
        if do_glow:
            div = max(1, round(fw / w))                              # about one ray per logical pixel
            gw, gh = (fw + div - 1) // div, (fh + div - 1) // div
            haze = self._fbo("glow", gw, gh, GL_RGBA16F)
            haze.bind()
            fn.glViewport(0, 0, gw, gh)
            fn.glDisable(GL_DEPTH_TEST)
            self._vao_full.bind()
            waves = str(o.air) == "waves"
            p = self._progs["waves" if waves else "glow"]
            p.bind()
            self._field_tex.bind(0)
            self._ramp_tex.bind(1)
            fn.glActiveTexture(GL_TEXTURE0 + 2)
            fn.glBindTexture(GL_TEXTURE_2D, dist.texture())
            p.setUniformValue1i("field", 0)
            p.setUniformValue1i("ramp", 1)
            p.setUniformValue1i("distTex", 2)
            lum = 0.2126 * bg[0] + 0.7152 * bg[1] + 0.0722 * bg[2]
            dark = 1.0 - min(max((lum - 0.35) / 0.3, 0.0), 1.0)
            for name, v in (("size", size), ("eye", eye), ("camF", f), ("camR", r), ("camU", u)):
                p.setUniformValue(name, QVector3D(*v))
            p.setUniformValue("centre", w * (fw / w) / 2.0, h * (fh / h) / 2.0)
            p.setUniformValue1f("focal", (h / 2.0) / math.tan(fov_y / 2.0) * (fh / h))
            p.setUniformValue1f("lo", lo)
            p.setUniformValue1f("hi", hi)
            p.setUniformValue1i("div", div)
            p.setUniformValue1f("dark", dark)
            p.setUniformValue1f("clearDist", 0.35 * math.sqrt(size[2] * min(size[0], size[1])))
            if waves:
                self._wave_uniforms(p, o, glow, size)
            else:
                self._fog_uniforms(p, o, glow, size, eye, f, r, u, w, h, fov_y, cut)
            fn.glDrawArrays(GL_TRIANGLES, 0, 3)

            final.bind()
            fn.glViewport(0, 0, fw, fh)
            fn.glEnable(GL_BLEND)
            fn.glBlendFunc(GL_ONE, GL_ONE_MINUS_SRC_ALPHA)
            p = self._progs["composite"]
            p.bind()
            fn.glActiveTexture(GL_TEXTURE0)
            fn.glBindTexture(GL_TEXTURE_2D, haze.texture())
            p.setUniformValue1i("glowTex", 0)
            p.setUniformValue1i("distTex", 2)
            p.setUniformValue1i("div", div)
            fn.glDrawArrays(GL_TRIANGLES, 0, 3)
            fn.glDisable(GL_BLEND)

        # 5 outlines on top, so the room's shape stays crisp through the glow
        final.bind()
        fn.glViewport(0, 0, fw, fh)
        fn.glDisable(GL_DEPTH_TEST)
        fn.glEnable(GL_BLEND)
        fn.glBlendFunc(GL_ONE, GL_ONE_MINUS_SRC_ALPHA)
        self._vao_full.bind()
        p = self._progs["edge"]
        p.bind()
        fn.glActiveTexture(GL_TEXTURE0)
        fn.glBindTexture(GL_TEXTURE_2D, dist.texture())
        p.setUniformValue1i("distTex", 0)
        p.setUniformValue("ink", QVector3D(*OUTLINE_INK))
        p.setUniformValue1f("strength", OUTLINE_STRENGTH)
        p.setUniformValue1i("reach", max(1, int(ss)))
        fn.glDrawArrays(GL_TRIANGLES, 0, 3)
        fn.glDisable(GL_BLEND)
        fn.glActiveTexture(GL_TEXTURE0)
        if ss > 1:
            small = self._fbo("out", out_w, out_h, GL_RGB8)
            QOpenGLFramebufferObject.blitFramebuffer(small, QRect(0, 0, out_w, out_h), final, QRect(0, 0, fw, fh),
                                                     GL_COLOR_BUFFER_BIT, GL_LINEAR)
            final = small
        final.bind()
        img = final.toImage(False)                        # the projection is already top row first
        final.release()
        return img if img.format() == QImage.Format_RGB32 else img.convertToFormat(QImage.Format_RGB32)


def _f(v, default: float) -> float:
    try:
        v = float(v)
    except (TypeError, ValueError):
        return default
    return v if math.isfinite(v) else default


def _rgb(c, default: tuple) -> tuple:
    try:
        out = tuple(min(max(_f(c[i], default[i]), 0.0), 1.0) for i in range(3))
    except (TypeError, IndexError, KeyError):
        return tuple(default)
    return out


def _qrgb(c: tuple) -> int:
    return 0xFF000000 | (round(c[0] * 255) << 16) | (round(c[1] * 255) << 8) | round(c[2] * 255)
