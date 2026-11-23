"""GPU renderer of the 3D Wi-Fi site view (labdesk_app/ui/site3d_gl.py).

The tests that draw need a real OpenGL 3.3 context; they are skipped, with the reason, where the machine
has none (no display, a software rasteriser, an old driver).  The string parsing, the camera matrix and the
mesh are pure functions and are tested everywhere.
"""
from __future__ import annotations

import math
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QImage  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from labdesk_app import wifi_site as ws  # noqa: E402
from labdesk_app.ui import site3d_gl as gl  # noqa: E402

np = pytest.importorskip("numpy")

RED_FLOOR = dict(background=(0.0, 0.0, 1.0), floor=(1.0, 0.0, 0.0), wall=(0.5, 0.5, 0.5), obstacle=(0.3, 0.3, 0.3),
                 door=(0.4, 0.4, 0.4), line=(0.5, 0.0, 0.0), heat=False, glow=0.0)


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture(scope="module")
def renderer(app):
    r, why = gl.SceneRenderer.create()
    if r is None:
        pytest.skip(f"no GPU renderer on this machine: {why}")
    yield r
    r.close()


# ------------------------------------------------------------------ helpers
def test_ramp() -> QImage:
    """Red (weak) to green (strong) through yellow, with blue rising too so every entry is distinct."""
    img = QImage(256, 1, QImage.Format_RGB32)
    for i in range(256):
        r, g = min(255, 2 * (255 - i)), min(255, 2 * i)
        img.setPixel(i, 0, 0xFF000000 | (r << 16) | (g << 8) | (i // 2))
    return img


test_ramp.__test__ = False


def ramp_colour(ramp: QImage, v: float, lo: float = -85.0, hi: float = -40.0) -> tuple:
    t = min(max((v - lo) / (hi - lo), 0.0), 1.0) * 255.0
    i = min(int(t), 254)
    a, b, k = ramp.pixelColor(i, 0), ramp.pixelColor(i + 1, 0), t - i
    return tuple(x + (y - x) * k for x, y in ((a.redF(), b.redF()), (a.greenF(), b.greenF()), (a.blueF(), b.blueF())))


def fill_volume(site: ws.Site, voxel: float = 1.0):
    """The model sampled at voxel centres in a plain loop: volume[z, y, x] in dBm."""
    nx, ny, nz = (max(1, round(s / voxel)) for s in (site.width, site.depth, site.height))
    walls = site.walls()
    vol = np.empty((nz, ny, nx), np.float32)
    for k in range(nz):
        for j in range(ny):
            for i in range(nx):
                pt = ((i + 0.5) * site.width / nx, (j + 0.5) * site.depth / ny, (k + 0.5) * site.height / nz)
                vol[k, j, i] = site.best(pt, walls)[0]
    return vol


def orbit(site: ws.Site, yaw: float = -62.0, pitch: float = 34.0, fov: float = 40.0) -> tuple:
    """(eye, target, fov_y) of a camera that sees the whole site, as site3d's orbit camera would."""
    t = (site.width / 2, site.depth / 2, site.height * 0.22)
    diag = math.sqrt(site.width ** 2 + site.depth ** 2 + site.height ** 2)
    d = (diag / 2) / math.tan(math.radians(fov) / 2) * 0.98
    y, p = math.radians(yaw), math.radians(pitch)
    eye = (t[0] + d * math.cos(p) * math.cos(y), t[1] + d * math.cos(p) * math.sin(y), t[2] + d * math.sin(p))
    return eye, t, math.radians(fov)


def project(eye, target, fov: float, w: float, h: float, p) -> tuple:
    """Logical pixel of a world point, with the formulas of site3d.Frame."""
    f, r, u = gl.camera_basis(eye, target)
    focal = (h / 2.0) / math.tan(fov / 2.0)
    d = [p[i] - eye[i] for i in range(3)]
    z = sum(d[i] * f[i] for i in range(3))
    return (w / 2.0 + focal * sum(d[i] * r[i] for i in range(3)) / z,
            h / 2.0 - focal * sum(d[i] * u[i] for i in range(3)) / z)


def rgb_at(img: QImage, x: float, y: float) -> tuple:
    """Colour 0..1 at a *logical* pixel position."""
    s = img.devicePixelRatio()
    c = img.pixelColor(min(max(int(x * s), 0), img.width() - 1), min(max(int(y * s), 0), img.height() - 1))
    return c.redF(), c.greenF(), c.blueF()


def lum(c) -> float:
    return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]


def close_to(a, b, tol: float) -> bool:
    return all(abs(x - y) <= tol for x, y in zip(a, b))


def is_red(c) -> bool:
    return c[0] - max(c[1], c[2]) > 0.35


# ------------------------------------------------------------------ (g) pure functions: no OpenGL needed
@pytest.mark.parametrize("text, want", [
    ("4.6.0 NVIDIA 570.172.08", (4, 6)),
    ("4.5 (Core Profile) Mesa 25.0.7", (4, 5)),
    ("3.0 Mesa 22.1.2", (3, 0)),
    ("3.3.0 NVIDIA 570.172.08", (3, 3)),
    ("OpenGL ES 3.2 Mesa 25.0.7", (3, 2)),
    ("4.60 NVIDIA", (4, 60)),
    ("1.1.0", (1, 1)),
    ("", None),
    (None, None),
    ("garbage", None),
])
def test_parse_gl_version(text, want):
    assert gl.parse_gl_version(text) == want


@pytest.mark.parametrize("name, soft", [
    ("llvmpipe (LLVM 19.1.7, 256 bits)", True),
    ("softpipe", True),
    ("Google SwiftShader", True),
    ("ANGLE (Google, Vulkan 1.3.0 (SwiftShader Device (LLVM 10.0.0)), SwiftShader driver)", True),
    ("GDI Generic", True),
    ("Software Rasterizer", True),
    ("Mesa X11 software renderer", True),
    ("NVIDIA GeForce GTX 1080/PCIe/SSE2", False),
    ("Mesa Intel(R) UHD Graphics 620 (KBL GT2)", False),
    ("AMD Radeon RX 6600 (radeonsi, navi23, LLVM 19.1.7, DRM 3.59)", False),
    ("", False),
    (None, False),
])
def test_software_renderers_are_recognised(name, soft):
    assert gl.is_software_renderer(name) is soft


def test_gl_problem_checks_the_real_version_and_the_renderer():
    nvidia = "NVIDIA GeForce GTX 1080/PCIe/SSE2"
    assert gl.gl_problem("4.6.0 NVIDIA 570.172.08", nvidia, "4.60 NVIDIA") == ""
    assert gl.gl_problem("3.3.0 NVIDIA 570.172.08", nvidia, "3.30 NVIDIA via Cg compiler") == ""
    assert "3.0" in gl.gl_problem("3.0 Mesa 22.1.2", "Mesa Intel(R) UHD Graphics 620", "1.30")
    assert "too old" in gl.gl_problem("2.1 Mesa 22.1.2", "Mesa Intel(R) HD Graphics", None)
    assert "shading language" in gl.gl_problem("3.3 Mesa", "Mesa Intel(R) HD Graphics", "1.30")
    assert "OpenGL ES" in gl.gl_problem("OpenGL ES 3.2 Mesa 25.0.7", "Mesa Intel(R) UHD Graphics 620", None)
    assert gl.gl_problem("", nvidia) != "" and gl.gl_problem(None, None) != ""
    assert gl.gl_problem("4.6.0 NVIDIA", "") != ""
    soft = gl.gl_problem("4.5 (Core Profile) Mesa 25.0.7", "llvmpipe (LLVM 19.1.7, 256 bits)", "4.50")
    assert "llvmpipe" in soft and "LABDESK_GL_SOFTWARE" in soft
    assert gl.gl_problem("4.5 (Core Profile) Mesa 25.0.7", "llvmpipe (LLVM 19.1.7, 256 bits)", "4.50", True) == ""
    # an old version is refused even when software rendering is allowed
    assert gl.gl_problem("3.0 Mesa 25.0.7", "llvmpipe (LLVM 19.1.7, 256 bits)", "1.30", True) != ""


def test_software_is_allowed_only_by_the_environment_switch():
    assert gl.software_allowed({}) is False
    assert gl.software_allowed({"LABDESK_GL_SOFTWARE": "0"}) is False
    assert gl.software_allowed({"LABDESK_GL_SOFTWARE": "1"}) is True


def test_camera_matrix_matches_frame_of_the_painter_view(app):
    from labdesk_app.ui.site3d import Camera, Frame
    site = ws.demo_site()
    corners = [(x, y, z) for x in (0.0, site.width) for y in (0.0, site.depth) for z in (0.0, site.height)]
    for yaw, pitch, w, h, scale in ((-62, 34, 1280, 800, 1.0), (118, 12, 801, 533, 1.5), (20, 70, 640, 480, 2.0)):
        cam = Camera()
        cam.yaw, cam.pitch, cam.dist, cam.target = math.radians(yaw), math.radians(pitch), 48.0, (15.0, 9.0, 1.1)
        fr = Frame(cam, w, h)
        fw, fh = round(w * scale), round(h * scale)
        m = gl.view_projection(fr.eye, cam.target, cam.fov, w, h, fw, fh, 0.05, 900.0)
        for p in corners:
            clip = [sum(m[4 * row + i] * v for i, v in enumerate((*p, 1.0))) for row in range(4)]
            sx, sy = (clip[0] / clip[3] * 0.5 + 0.5) * fw, (clip[1] / clip[3] * 0.5 + 0.5) * fh
            want = fr.proj(p)
            # framebuffer row 0 is the top row of the picture: no mirroring on read-back
            assert abs(sx - want.x() * fw / w) < 1e-6 * fw and abs(sy - want.y() * fh / h) < 1e-6 * fh
            assert -1.0 < clip[2] / clip[3] < 1.0


def test_camera_basis_never_degenerates():
    for eye, target in (((0, 0, 10), (0, 0, 0)), ((1, 1, 1), (1, 1, 1)), ((0, 0, 0), (0, 0, 5))):
        f, r, u = gl.camera_basis(eye, target)
        for v in (f, r, u):
            assert abs(math.sqrt(sum(c * c for c in v)) - 1.0) < 1e-9


def test_mesh_cuts_the_walls_the_camera_looks_over():
    site = ws.demo_site()
    geom = gl.site_geometry(site)
    assert gl.cut_mask(geom["size"], (40.0, -20.0, 20.0)) == 2 | 4
    assert gl.cut_mask(geom["size"], (10.0, 5.0, 2.0)) == 0            # inside: nothing is cut
    full, n_ground, n_floor, n_solid = gl.build_mesh(geom, 0)
    cut, _g, _f, _s = gl.build_mesh(geom, 2 | 4)
    assert len(full) % gl.STRIDE == 0 and 0 < n_ground < n_floor < n_solid == len(full) // gl.STRIDE   # no glass

    def top(data, pred):
        v = [data[i:i + gl.STRIDE] for i in range(0, len(data), gl.STRIDE)]
        return max(p[2] for p in v if p[6] == gl.K_WALL and pred(p))

    assert top(full, lambda p: p[0] > site.width) == site.height        # the wall at x = width, full height
    assert top(cut, lambda p: p[0] > site.width) == gl.KERB_H           # ... cut down to a kerb
    assert top(cut, lambda p: p[0] < 0.0) == site.height                # the far wall keeps its height
    kinds = {full[i + 6] for i in range(0, len(full), gl.STRIDE)}
    assert kinds == {gl.K_GROUND, gl.K_FLOOR, gl.K_WALL, gl.K_OBSTACLE, gl.K_LEAF, gl.K_FRAME}
    tags = {full[i + 7] for i in range(0, len(full), gl.STRIDE) if full[i + 6] == gl.K_WALL}
    assert tags == {-1.0, 0.0, 1.0}                                     # outer walls, then site.walls() by index
    assert gl.box_chord((-5.0, 9.0, 2.0), (1.0, 0.0, 0.0), geom["size"]) == pytest.approx(30.0)
    assert gl.box_chord((-5.0, 9.0, 2.0), (-1.0, 0.0, 0.0), geom["size"]) == 0.0


def test_create_reports_instead_of_raising(app):
    r, why = gl.SceneRenderer.create()
    assert (r is None) != (why == "")
    if r is not None:
        assert r.renderer_name and gl.parse_gl_version(r.version) >= (3, 3)
        r.close()


# ------------------------------------------------------------------ (a) size and scale
def test_image_has_the_requested_size_and_scale(renderer):
    site = ws.demo_site()
    renderer.set_site(site)
    eye, target, fov = orbit(site)
    for w, h, scale in ((640, 400, 1.0), (640, 400, 2.0), (333, 211, 1.5), (500, 300, 1.25)):
        img = renderer.render(w, h, eye, target, fov, gl.RenderOpts(), scale)
        assert (img.width(), img.height()) == (round(w * scale), round(h * scale))
        assert img.devicePixelRatio() == pytest.approx(scale)
        assert img.format() == QImage.Format_RGB32
        assert renderer.last_ms > 0.0 and renderer.last_error == ""


# ------------------------------------------------------------------ (b) camera alignment with site3d.Frame
@pytest.mark.parametrize("scale", [1.0, 2.0, 1.5])
def test_room_edges_are_where_frame_projects_them(renderer, scale):
    from labdesk_app.ui.site3d import Camera, Frame
    site = ws.Site(12.0, 8.0, 3.0)                       # a bare room, seen from above its own floor:
    renderer.set_site(site)                              # all four walls stand at full height
    cam = Camera()
    cam.target, cam.fov = (6.0, 4.5, 0.0), math.radians(50.0)
    cam.yaw, cam.pitch, cam.dist = math.radians(-90.0), math.atan2(14.0, 3.5), math.hypot(14.0, 3.5)
    w, h = 801, 600
    fr = Frame(cam, w, h)
    assert 0.0 < fr.eye[0] < site.width and 0.0 < fr.eye[1] < site.depth
    img = renderer.render(w, h, fr.eye, cam.target, cam.fov, gl.RenderOpts(**RED_FLOOR), scale)
    W, D, H = site.width, site.depth, site.height

    def at(p, towards, px):
        """The picture ``px`` pixels from the projection of ``p`` towards the projection of ``towards``."""
        a, b = fr.proj(p), fr.proj(towards)
        n = math.hypot(b.x() - a.x(), b.y() - a.y())
        return rgb_at(img, a.x() + (b.x() - a.x()) / n * px, a.y() + (b.y() - a.y()) / n * px)

    # the floor ends at the foot of each wall: red just inside the edge, wall just beyond it
    for edge, inward in ((lambda t: (W * t, 0.0, 0.0), (0, 1, 0)), (lambda t: (W * t, D, 0.0), (0, -1, 0)),
                         (lambda t: (0.0, D * t, 0.0), (1, 0, 0)), (lambda t: (W, D * t, 0.0), (-1, 0, 0))):
        for t in (0.03, 0.25, 0.5, 0.75, 0.97):
            p = edge(t)
            inside = tuple(p[i] + inward[i] for i in range(3))
            assert is_red(at(p, inside, 2.0)), (p, "floor expected just inside")
            assert not is_red(at(p, inside, -2.0)), (p, "wall expected just outside")
    # the four floor corners: only the quarter towards the room is floor
    for cx, cy in ((0.0, 0.0), (W, 0.0), (W, D), (0.0, D)):
        c = fr.proj((cx, cy, 0.0))
        mid = fr.proj((W / 2, D / 2, 0.0))
        qx, qy = math.copysign(3.0, mid.x() - c.x()), math.copysign(3.0, mid.y() - c.y())
        assert is_red(rgb_at(img, c.x() + qx, c.y() + qy))
        for ox, oy in ((-qx, qy), (qx, -qy), (-qx, -qy)):
            assert not is_red(rgb_at(img, c.x() + ox, c.y() + oy)), ((cx, cy), ox, oy)
    # the wall heads: the inner face ends and the (brighter) cut face on top begins at height H
    for edge, inward in ((lambda t: (W * t, 0.0, H), (0, 1, 0)), (lambda t: (W * t, D, H), (0, -1, 0)),
                         (lambda t: (0.0, D * t, H), (1, 0, 0)), (lambda t: (W, D * t, H), (-1, 0, 0))):
        for t in (0.3, 0.5, 0.7):
            p = edge(t)
            face = lum(at(p, (p[0], p[1], 0.0), 6.0))
            assert lum(at(p, (p[0], p[1], 0.0), 2.0)) == pytest.approx(face, abs=0.03), (p, "inner face below")
            assert lum(at(p, (p[0], p[1], 0.0), -2.0)) > face + 0.05, (p, "top face above")
    # the four top corners: wall faces towards the room, the top face away from it
    for cx, cy in ((0.0, 0.0), (W, 0.0), (W, D), (0.0, D)):
        c, foot = fr.proj((cx, cy, H)), fr.proj((cx, cy, 0.0))
        n = math.hypot(foot.x() - c.x(), foot.y() - c.y())
        dx, dy = (foot.x() - c.x()) / n, (foot.y() - c.y()) / n
        top, faces = rgb_at(img, c.x() - 3 * dx, c.y() - 3 * dy), rgb_at(img, c.x() + 4 * dx, c.y() + 4 * dy)
        assert lum(top) > lum(faces) + 0.05, ((cx, cy), top, faces)


# ------------------------------------------------------------------ (c) the floor shows the ramp colour
def test_floor_pixel_has_the_ramp_colour_of_the_field(renderer):
    site = ws.demo_site()
    vol = fill_volume(site, 1.0)
    ramp = test_ramp()
    renderer.set_site(site)
    assert renderer.set_field(vol) and renderer.set_ramp(ramp)
    eye, target, fov = orbit(site)
    w, h = 1000, 640
    opts = gl.RenderOpts(glow=0.0, slice_z=1.0)
    for scale in (1.0, 2.0):
        img = renderer.render(w, h, eye, target, fov, opts, scale)
        for x, y in ((5.5, 4.5), (9.5, 3.5), (17.5, 15.5), (24.5, 7.5)):                 # centres of grid squares
            assert not _hidden(gl.site_geometry(site), gl.cut_mask((30.0, 18.0, 5.0), eye), eye, (x, y, 0.0))
            i, j = int(x), int(y)
            v = (float(vol[0, j, i]) + float(vol[1, j, i])) / 2.0        # z = 1.0 lies between two voxel layers
            assert opts.lo_dbm < v < opts.hi_dbm
            got = rgb_at(img, *project(eye, target, fov, w, h, (x, y, 0.0)))
            assert close_to(got, ramp_colour(ramp, v), 0.03), ((x, y), v, got, ramp_colour(ramp, v))
    # below lo_dbm there is no service: the bare floor colour, and the same with the heat map off
    quiet = gl.RenderOpts(glow=0.0, lo_dbm=-30.0, hi_dbm=-20.0, floor=(0.2, 0.3, 0.4))
    img = renderer.render(w, h, eye, target, fov, quiet)
    assert close_to(rgb_at(img, *project(eye, target, fov, w, h, (24.5, 7.5, 0.0))), (0.2, 0.3, 0.4), 0.02)


def test_contour_line_is_drawn_at_its_level(renderer):
    site = ws.Site(20.0, 20.0, 4.0)
    renderer.set_site(site)
    vol = np.empty((4, 20, 20), np.float32)
    vol[:, :, :] = np.linspace(-80.0, -42.0, 20, dtype=np.float32)[None, None, :]      # rises along x only
    ramp = test_ramp()
    assert renderer.set_field(vol) and renderer.set_ramp(ramp)
    eye, target, fov = (10.0, 9.0, 30.0), (10.0, 10.0, 0.0), math.radians(50.0)
    w, h = 800, 800
    line = (0.0, 0.0, 1.0)
    img = renderer.render(w, h, eye, target, fov, gl.RenderOpts(glow=0.0, contours=(-61.0,), line=line))
    x_line = 0.5 + (-61.0 + 80.0) / 2.0                                  # where the field is -61 dBm
    on = rgb_at(img, *project(eye, target, fov, w, h, (x_line, 6.5, 0.0)))
    off = rgb_at(img, *project(eye, target, fov, w, h, (x_line + 0.5, 6.5, 0.0)))
    assert on[2] > 0.5 and on[2] > off[2] + 0.3, (on, off)               # the line, in the line colour
    assert close_to(off, ramp_colour(ramp, -60.0), 0.03)                 # and half a metre on, the plain ramp


# ------------------------------------------------------------------ (d) the glow in the air
def test_glow_shows_in_the_air_and_is_strongest_at_the_access_point(renderer):
    site = ws.demo_site()
    renderer.set_site(site)
    assert renderer.set_field(fill_volume(site, 1.0)) and renderer.set_ramp(test_ramp())
    eye, target, fov = orbit(site)
    w, h = 1000, 640
    dark = dict(background=(0.07, 0.08, 0.09), heat=False)
    off = renderer.render(w, h, eye, target, fov, gl.RenderOpts(glow=0.0, **dark))
    half = renderer.render(w, h, eye, target, fov, gl.RenderOpts(glow=0.6, **dark))
    full = renderer.render(w, h, eye, target, fov, gl.RenderOpts(glow=1.0, **dark))
    ap = site.aps[0]

    def change(img, p):
        a, b = rgb_at(off, *project(eye, target, fov, w, h, p)), rgb_at(img, *project(eye, target, fov, w, h, p))
        return sum(abs(x - y) for x, y in zip(a, b))

    near = (ap.x, site.depth, 3.0)                    # on the far wall, behind the access point
    corner = (site.width - 1.5, site.depth, 3.0)      # on the far wall of the office, behind the concrete wall
    floor = (ap.x + 2.0, ap.y - 2.0, 0.0)             # air over the floor near the access point
    assert change(full, near) > 0.25 and change(full, floor) > 0.25
    assert change(full, near) > 3.0 * change(full, corner)
    assert change(full, near) > change(half, near) > 0.10            # clearly visible at 0.6, stronger at 1
    assert change(full, floor) > change(half, floor) > 0.10
    # outside the room there is no glow at all
    assert rgb_at(full, 5, 5) == rgb_at(off, 5, 5)
    # on a light background the same glow is still there (it is laid over, not added)
    light = dict(background=(0.96, 0.96, 0.97), wall=(0.95, 0.95, 0.96), floor=(0.9, 0.9, 0.92), heat=False)
    a = renderer.render(w, h, eye, target, fov, gl.RenderOpts(glow=0.0, **light))
    b = renderer.render(w, h, eye, target, fov, gl.RenderOpts(glow=0.6, **light))
    q = project(eye, target, fov, w, h, near)
    assert sum(abs(x - y) for x, y in zip(rgb_at(a, *q), rgb_at(b, *q))) > 0.10


def test_glow_never_hides_the_floor(renderer):
    site = ws.demo_site()
    renderer.set_site(site)
    vol = np.full((5, 18, 30), -30.0, np.float32)     # the strongest signal everywhere
    assert renderer.set_field(vol) and renderer.set_ramp(test_ramp())
    eye, target, fov = orbit(site)
    w, h = 800, 500
    opts = dict(heat=False, floor=(0.0, 0.0, 0.0), background=(0.0, 0.0, 0.0), wall=(0.0, 0.0, 0.0),
                obstacle=(0.0, 0.0, 0.0), door=(0.0, 0.0, 0.0), line=(0.0, 0.0, 0.0))
    for glow in (0.6, 1.0):
        cap = gl.GLOW_CAP[0] + (gl.GLOW_CAP[1] - gl.GLOW_CAP[0]) * glow
        black = renderer.render(w, h, eye, target, fov, gl.RenderOpts(glow=glow, **dict(opts, floor=(0.0, 0.0, 0.0))))
        white = renderer.render(w, h, eye, target, fov, gl.RenderOpts(glow=glow, **dict(opts, floor=(1.0, 1.0, 1.0))))
        for p in ((5.5, 4.5, 0.0), (17.5, 14.5, 0.0), (25.5, 2.5, 0.0)):
            assert not _hidden(gl.site_geometry(site), gl.cut_mask((30.0, 18.0, 5.0), eye), eye, p)
            q = project(eye, target, fov, w, h, p)
            seen = rgb_at(white, *q)[2] - rgb_at(black, *q)[2]         # how much of the floor comes through
            # the fog's cap, times the darkest a floor in a cast shadow gets while the heat map is off (0.74)
            assert seen >= (1.0 - cap) * 0.74 - 0.03, (glow, p, seen)


# ------------------------------------------------------------------ (e) light never changes a heat colour
def test_lighting_and_shadows_do_not_change_the_heat_colour(renderer):
    site = ws.demo_site()
    renderer.set_site(site)
    vol = np.full((5, 18, 30), -60.0, np.float32)     # one value everywhere: one colour everywhere
    ramp = test_ramp()
    assert renderer.set_field(vol) and renderer.set_ramp(ramp)
    want = ramp_colour(ramp, -60.0)
    w, h = 1000, 640
    wall_x = site.walls()[0].a                        # the concrete wall across the room
    spots = [(wall_x - 0.5, 9.5), (wall_x + 0.5, 9.5), (wall_x - 2.5, 3.5), (wall_x + 2.5, 3.5),
             (12.5, 7.5), (14.7, 7.5), (2.5, 12.5), (7.5, 16.5), (28.5, 16.5), (0.5, 0.5)]
    seen = {}
    for yaw in (-62.0, 118.0, 28.0, -152.0):          # the light follows the camera: every side gets its turn
        eye, target, fov = orbit(site, yaw=yaw, pitch=50.0)
        img = renderer.render(w, h, eye, target, fov, gl.RenderOpts(glow=0.0))
        cut = gl.cut_mask((site.width, site.depth, site.height), eye)
        geom = gl.site_geometry(site)
        for x, y in spots:
            if _hidden(geom, cut, eye, (x, y, 0.0)):
                continue
            got = rgb_at(img, *project(eye, target, fov, w, h, (x, y, 0.0)))
            assert close_to(got, want, 0.012), (yaw, (x, y), got, want)
            seen[(x, y)] = seen.get((x, y), 0) + 1
        # the walls themselves are lit: faces that look different ways differ, the floor at their feet does not
        if yaw == -62.0:
            faces = [lum(rgb_at(img, *project(eye, target, fov, w, h, p)))
                     for p in ((10.0, site.depth, 3.0), (0.0, 9.0, 3.0), (10.0, site.depth + gl.WALL_T / 2, 5.0))]
            assert min(abs(faces[a] - faces[b]) for a, b in ((0, 1), (0, 2), (1, 2))) > 0.02, faces
    assert all(seen.get(s, 0) >= 1 for s in spots) and sum(seen.get(s, 0) for s in spots) >= 24, seen


def _hidden(geom: dict, cut: int, eye, p) -> bool:
    """Is the floor point ``p`` behind a wall or an obstacle as seen from ``eye``?"""
    w, d, h = geom["size"]
    t, kerb = gl.WALL_T, gl.KERB_H
    boxes = [((-t, -t, 0), (0, d + t, kerb if cut & 1 else h)), ((w, -t, 0), (w + t, d + t, kerb if cut & 2 else h)),
             ((0, -t, 0), (w, 0, kerb if cut & 4 else h)), ((0, d, 0), (w, d + t, kerb if cut & 8 else h))]
    for axis, a, b0, b1, _holes, thick, _role in geom["walls"]:
        lo, hi = ((a - thick / 2, b0, 0), (a + thick / 2, b1, h)) if axis == "x" else \
            ((b0, a - thick / 2, 0), (b1, a + thick / 2, h))
        boxes.append((lo, hi))
    boxes += [((x, y, z0), (x + bw, y + bd, z0 + bh)) for x, y, bw, bd, bh, z0, _k in geom["obstacles"]]
    grow = 0.12                                          # keep clear of edges
    return any(ws.segment_in_box(eye, (p[0], p[1], 0.01), tuple(v - grow for v in lo), tuple(v + grow for v in hi))
               is not None for lo, hi in boxes)


# ------------------------------------------------------------------ (f) nothing crashes
def test_bad_fields_are_refused_without_a_crash(renderer):
    site = ws.demo_site()
    renderer.set_site(site)
    good = np.full((5, 18, 30), -55.0, np.float32)
    assert renderer.set_field(good) is True
    eye, target, fov = orbit(site)
    before = renderer.render(400, 300, eye, target, fov, gl.RenderOpts(glow=0.5))
    for bad in (np.zeros((18, 30), np.float32), np.zeros((2, 3, 4, 5), np.float32), np.zeros((0, 18, 30), np.float32),
                np.zeros(7, np.float32), None, "field", [[1.0, 2.0], [3.0]], object()):
        assert renderer.set_field(bad) is False
    after = renderer.render(400, 300, eye, target, fov, gl.RenderOpts(glow=0.5))
    assert after == before                               # the last good field is still the one shown
    holes = np.full((5, 18, 30), -55.0, np.float32)
    holes[0, 0, 0], holes[1, 2, 3], holes[4, 17, 29] = np.nan, np.inf, -np.inf
    assert renderer.set_field(holes) is True             # holes count as "no signal"
    assert renderer.set_field(np.full((5, 18, 30), np.nan, np.float32)) is True
    img = renderer.render(400, 300, eye, target, fov, gl.RenderOpts(glow=1.0, floor=(0.1, 0.2, 0.3)))
    assert close_to(rgb_at(img, *project(eye, target, fov, 400, 300, (5.5, 4.5, 0.0))), (0.1, 0.2, 0.3), 0.02)
    assert renderer.set_field(np.full((1, 1, 1), -50.0)) is True          # float64 and a single voxel are fine
    assert renderer.set_field([[[-50.0, -60.0]]]) is True
    assert renderer.set_ramp(QImage()) is False and renderer.set_ramp(None) is False
    assert renderer.set_ramp(QImage(16, 4, QImage.Format_ARGB32)) is True  # any size is stretched to 256 x 1


def test_odd_requests_do_not_crash(renderer):
    site = ws.demo_site()
    renderer.set_site(site)
    eye, target, fov = orbit(site)
    for w, h in ((0, 0), (0, 100), (100, 0), (-5, 10), (float("nan"), 10), (None, 10)):
        assert renderer.render(w, h, eye, target, fov, gl.RenderOpts()).isNull()
    for scale in (0.0, -1.0, float("nan"), float("inf")):
        img = renderer.render(64, 48, eye, target, fov, gl.RenderOpts(), scale)
        assert (img.width(), img.height()) == (64, 48)
    huge = renderer.render(300, 200, eye, target, fov, gl.RenderOpts(), 1000.0)      # clamped, not refused
    assert not huge.isNull() and max(huge.width(), huge.height()) <= gl.MAX_FRAME_PX
    del huge
    bg = (0.2, 0.4, 0.6)
    for bad_eye, bad_target, bad_fov in (((float("nan"), 0, 0), target, fov), (eye, eye, fov), (eye, target, 0.0),
                                         (eye, target, float("inf")), ((1, 2), target, fov), (eye, (0, 0, 1e30), 9.0)):
        img = renderer.render(64, 48, bad_eye, bad_target, bad_fov, gl.RenderOpts(background=bg))
        assert (img.width(), img.height()) == (64, 48)
    odd = gl.RenderOpts(contours=tuple(range(-90, -30)), selected_wall=99, slice_z=1e9, lo_dbm=-40.0, hi_dbm=-40.0,
                        glow=7.0, background=(2.0, -1.0, float("nan")), floor=None, wall=(1,), line="x")
    assert not renderer.render(64, 48, eye, target, fov, odd).isNull()
    assert not renderer.render(64, 48, eye, target, fov, None).isNull()
    renderer.set_site(None)                              # not a site: ignored, the old room stays
    renderer.set_site(ws.Site(1.0, 1.0, 2.0))
    assert not renderer.render(64, 48, (3, -3, 3), (0.5, 0.5, 0.5), 0.7, gl.RenderOpts()).isNull()
    inside = renderer.render(64, 48, (0.5, 0.5, 1.0), (0.9, 0.9, 1.0), 1.2, gl.RenderOpts(glow=1.0))
    assert not inside.isNull()


def test_selected_wall_is_tinted(renderer):
    site = ws.demo_site()
    renderer.set_site(site)
    eye, target, fov = orbit(site)
    w, h = 800, 500
    accent = (0.1, 0.1, 1.0)
    wall = site.walls()[0]
    spot = project(eye, target, fov, w, h, (wall.a - 0.08, 14.0, 3.5))
    plain = rgb_at(renderer.render(w, h, eye, target, fov, gl.RenderOpts(glow=0.0, accent=accent)), *spot)
    picked = rgb_at(renderer.render(w, h, eye, target, fov, gl.RenderOpts(glow=0.0, accent=accent, selected_wall=0)),
                    *spot)
    other = rgb_at(renderer.render(w, h, eye, target, fov, gl.RenderOpts(glow=0.0, accent=accent, selected_wall=1)),
                   *spot)
    assert abs(plain[2] - plain[0]) < 0.05 and picked[2] - picked[0] > 0.2 and other == plain


def test_close_twice_and_render_after_close(app):
    r, why = gl.SceneRenderer.create()
    if r is None:
        pytest.skip(f"no GPU renderer on this machine: {why}")
    site = ws.demo_site()
    r.set_site(site)
    eye, target, fov = orbit(site)
    assert not r.render(64, 48, eye, target, fov, gl.RenderOpts()).isNull()
    r.close()
    r.close()
    img = r.render(64, 48, eye, target, fov, gl.RenderOpts(background=(1.0, 0.0, 0.0)), 2.0)
    assert (img.width(), img.height()) == (128, 96) and rgb_at(img, 10, 10) == (1.0, 0.0, 0.0)
    assert r.set_field(np.zeros((2, 2, 2), np.float32)) is False and r.set_ramp(test_ramp()) is False
    r.set_site(site)
    r.close()


def test_fog_noise_tiles_and_spreads_evenly():
    n = gl.fog_noise(16, seed=3)
    assert n.shape == (16, 16, 16) and n.dtype == np.uint8
    counts = np.bincount(n.ravel(), minlength=256)
    assert n.min() == 0 and n.max() == 255 and counts.max() <= 2 * n.size / 256 + 2      # ranked: even spread
    for axis in range(3):                     # smooth across the wrap-around seam as well as inside
        seam = np.abs(np.take(n, 0, axis).astype(int) - np.take(n, -1, axis).astype(int)).mean()
        inside = np.abs(np.diff(n.astype(int), axis=axis)).mean()
        assert seam < 2.0 * inside
    assert np.array_equal(gl.fog_noise(16, seed=3), n)


def test_fog_billows_and_drifts(renderer):
    site = ws.demo_site()
    renderer.set_site(site)
    vol = np.full((5, 18, 30), -45.0, np.float32)
    assert renderer.set_field(vol) and renderer.set_ramp(test_ramp())
    eye, target, fov = orbit(site)
    base = dict(heat=False, background=(0.0, 0.0, 0.0), glow=1.0)
    even = renderer.render(400, 260, eye, target, fov, gl.RenderOpts(fog_detail=0.0, **base))
    fog = renderer.render(400, 260, eye, target, fov, gl.RenderOpts(fog_detail=1.0, **base))
    later = renderer.render(400, 260, eye, target, fov, gl.RenderOpts(fog_detail=1.0, fog_time=40.0, **base))

    def diff(a, b):
        return float(np.mean([abs(rgb_at(a, x, y)[1] - rgb_at(b, x, y)[1])
                              for x in range(100, 300, 5) for y in range(80, 180, 5)]))
    assert diff(fog, even) > 0.02                             # billows: the fog is not an even haze
    assert diff(fog, later) > 0.01                            # and it drifts with time


# ---------------------------------------------------------------------------- the field worked out on the GPU
def test_field_grid_follows_the_budget_and_the_limits():
    nx, ny, nz = gl.field_grid(30, 18, 5, budget=4_000_000)
    assert 3_000_000 < nx * ny * nz < 5_000_000
    assert abs(30 / nx - 5 / nz) < 0.02                             # about cubic voxels
    assert min(gl.field_grid(2, 2, 2, budget=10**9)) >= 2 / (gl.GPU_MIN_CELL_M * 1.01)  # never finer than the floor
    assert max(gl.field_grid(500, 500, 30, budget=10**9, max_side=256)) <= 256
    assert gl.field_grid(500, 1, 2, budget=50)[2] >= 4


def test_field_tables_pack_what_site_rssi_uses():
    s = ws.demo_site()
    s.aps.append(ws.AccessPoint("off", 1, 1, 2, enabled=False))
    tex, n_ap, n_wall, n_obs = gl.field_tables(s)
    assert (n_ap, n_wall, n_obs) == (1, len(s.walls()), len(s.obstacles))
    n_holes = sum(len(w.holes) for w in s.walls())
    assert tex.dtype == np.float32 and tex.shape == (2 * n_ap + 3 * n_wall + 4 * n_obs + 2 * n_holes, 4)
    ap = s.aps[0]
    assert tuple(tex[0]) == pytest.approx((ap.x, ap.y, ap.z, ap.tx_dbm))
    assert tex[1][0] == pytest.approx(s.path_loss(1.0, ap.band), abs=1e-4)
    assert tex[1][1] == ws.CLUTTER[s.clutter][1]
    w = s.walls()[0]
    assert tuple(tex[2]) == pytest.approx((0.0 if w.axis == "x" else 1.0, w.a, w.b0, w.b1))
    empty = ws.Site()
    assert gl.field_tables(empty)[1:] == (0, 0, 0)


def _gpu_vs_reference(renderer, s, budget):
    from labdesk_app import wifi_field as wf
    assert renderer.compute_field(s, budget=budget), renderer.last_error
    nx, ny, nz = renderer.field_shape
    g = wf.Grid(s.width, s.depth, s.height, nx, ny, nz)
    worst = 0.0
    for k, z in enumerate(g.zd):
        layer = renderer.read_field_layer(k)
        assert layer is not None and layer.shape == (ny, nx)
        for j, y in enumerate(g.yd):
            for i, x in enumerate(g.xd):
                want = max(s.best((float(x), float(y), float(z)))[0], gl.NO_FIELD_DBM)
                worst = max(worst, abs(float(layer[j, i]) - want))
    return worst


def test_gpu_field_equals_the_reference_model_on_the_demo_site(renderer):
    assert _gpu_vs_reference(renderer, ws.demo_site(), 6000) < 2e-3
    assert renderer.field_on_gpu


@pytest.mark.parametrize("seed", range(12))
def test_gpu_field_equals_the_reference_model_on_random_sites(renderer, seed):
    import random
    from tests.test_wifi_field import random_site
    assert _gpu_vs_reference(renderer, random_site(random.Random(seed)), 1500) < 2e-3


def test_gpu_field_is_fine_and_fast(renderer):
    s = ws.demo_site()
    renderer.compute_field(s)
    nx, ny, nz = renderer.field_shape
    assert nx * ny * nz > 3_000_000                                 # twenty times the CPU grid
    renderer.compute_field(s)
    assert renderer.field_ms < 50                                    # a drag step, with margin for slow cards
    renderer.set_field(np.full((2, 2, 2), -60.0, np.float32))       # a volume from the CPU takes over again
    assert not renderer.field_on_gpu and renderer.read_field_layer(0) is None


def test_no_access_point_gives_no_signal(renderer):
    s = ws.demo_site()
    s.aps.clear()
    assert renderer.compute_field(s, budget=2000)
    assert np.all(renderer.read_field_layer(0) == gl.NO_FIELD_DBM)


# ---------------------------------------------------------------------------- waves from the access points
def test_wave_phase_grows_at_the_wave_speed_and_repeats():
    assert gl.wave_phase(0.0) == 0.0
    assert gl.wave_phase(0.5) == pytest.approx(0.5 * gl.WAVE_SPEED % gl.WAVE_SPACING)
    period = gl.WAVE_SPACING / gl.WAVE_SPEED
    assert gl.wave_phase(3 * period + 0.1) == pytest.approx(gl.wave_phase(0.1))
    assert 0.0 <= gl.wave_phase(1e6 + 0.37) < gl.WAVE_SPACING
    assert gl.wave_phase(float("nan")) == 0.0


def _wave_frames(renderer, **kw):
    site = ws.demo_site()
    renderer.set_site(site)
    assert renderer.compute_field(site), renderer.last_error
    assert renderer.set_ramp(test_ramp())
    eye, target, fov = orbit(site)
    ap = site.aps[0]
    base = dict(heat=False, background=(0.0, 0.0, 0.0), glow=0.8, air="waves", sources=((ap.x, ap.y, ap.z),))
    base.update(kw)
    return [renderer.render(400, 260, eye, target, fov, gl.RenderOpts(fog_time=t, **base)) for t in (0.0, 0.4)]


def _mean_diff(a, b):
    return float(np.mean([abs(lum(rgb_at(a, x, y)) - lum(rgb_at(b, x, y)))
                          for x in range(20, 380, 4) for y in range(20, 240, 4)]))


def test_waves_show_and_travel(renderer):
    now, later = _wave_frames(renderer)
    plain = _wave_frames(renderer, glow=0.0)[0]
    assert _mean_diff(now, plain) > 0.01                            # the fronts are drawn
    assert _mean_diff(now, later) > 0.005                           # and they move out with time
    quiet = _wave_frames(renderer, sources=())[0]                   # nothing transmits: no fronts, no rings
    assert _mean_diff(quiet, plain) < 0.002


def test_waves_fade_where_there_is_no_signal(renderer):
    site = ws.demo_site()
    renderer.set_site(site)
    renderer.set_field(np.full((5, 18, 30), gl.NO_FIELD_DBM, np.float32))   # no service anywhere
    eye, target, fov = orbit(site)
    ap = site.aps[0]
    opts = dict(heat=False, background=(0.0, 0.0, 0.0), glow=1.0, sources=((ap.x, ap.y, ap.z),))
    waves = renderer.render(400, 260, eye, target, fov, gl.RenderOpts(air="waves", **opts))
    off = renderer.render(400, 260, eye, target, fov, gl.RenderOpts(**dict(opts, glow=0.0)))
    assert _mean_diff(waves, off) < 0.002


def test_bad_wave_sources_do_not_crash(renderer):
    site = ws.demo_site()
    renderer.set_site(site)
    eye, target, fov = orbit(site)
    for src in ((("a", 1, 2),), ((float("nan"), 1, 2),), ((1, 2),), [(1, 2, 3)] * 40, None):
        img = renderer.render(200, 140, eye, target, fov, gl.RenderOpts(air="waves", sources=src, glow=1.0))
        assert not img.isNull()
