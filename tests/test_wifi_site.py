"""3D site: the room / partition / door model, the multi-wall radio model and the 3D view."""
import math
import os
import sys

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtCore import QPointF  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from labdesk_app import config  # noqa: E402
from labdesk_app import wifi_site as ws  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def box(*partitions, w=10.0, d=10.0, h=3.0):
    s = ws.Site(w, d, h)
    s.partitions = list(partitions)
    return s


# ------------------------------------------------------------------ rooms: percentages of the box
def test_one_open_space():
    rooms = box().rooms()
    assert len(rooms) == 1 and rooms[0].pct == pytest.approx(100.0) and rooms[0].area_m2 == pytest.approx(100.0)


def test_full_walls_divide_by_percentage():
    rooms = box(ws.Partition("x", 30.0)).rooms()
    assert [round(r.pct) for r in rooms] == [70, 30]
    rooms = box(ws.Partition("x", 30.0), ws.Partition("y", 50.0)).rooms()
    assert sorted(round(r.pct) for r in rooms) == [15, 15, 35, 35]
    assert sum(r.pct for r in rooms) == pytest.approx(100.0)


def test_partial_wall_only_divides_where_it_closes_a_room():
    # a wall across only 60 % of the depth closes nothing: still one room
    assert len(box(ws.Partition("x", 50.0, 0.0, 60.0)).rooms()) == 1
    # the demo: a full wall at 70 %, then the right-hand part halved -> 70 / 15 / 15
    assert [round(r.pct) for r in ws.demo_site().rooms()] == [70, 15, 15]


def test_a_door_does_not_join_rooms():
    assert len(box(ws.Partition("x", 50.0, door=ws.Door(open=True))).rooms()) == 2


# ------------------------------------------------------------------ radio
def test_free_space_reference_and_distance():
    s = box()
    s.clutter = "open"
    assert s.path_loss(1.0, "2.4") == pytest.approx(40.2, abs=0.2)          # textbook FSPL at 1 m
    assert s.path_loss(1.0, "5") == pytest.approx(47.3, abs=0.2)
    assert s.path_loss(10.0, "5") - s.path_loss(1.0, "5") == pytest.approx(20.0)   # n = 2: 20 dB per decade
    s.clutter = "dense"
    assert s.path_loss(10.0, "5") - s.path_loss(1.0, "5") == pytest.approx(10.0 * ws.CLUTTER["dense"][1])
    assert s.path_loss(0.5, "5") < s.path_loss(1.0, "5") < s.path_loss(1.01, "5")   # continuous at 1 m


def test_wall_costs_its_material_and_only_when_crossed():
    s = box(ws.Partition("x", 50.0, material="concrete"))
    ap = ws.AccessPoint("AP1", 2.0, 5.0, 2.0)
    same_side, other_side = (4.0, 5.0, 1.0), (8.0, 5.0, 1.0)
    assert s.wall_loss((ap.x, ap.y, ap.z), same_side, "5") == 0.0
    assert s.wall_loss((ap.x, ap.y, ap.z), other_side, "5") == 18.0
    assert s.wall_loss((ap.x, ap.y, ap.z), other_side, "2.4") == 12.0
    free = box()
    assert free.rssi(ap, other_side) - s.rssi(ap, other_side) == pytest.approx(18.0)


def test_partial_wall_is_missed_by_a_path_around_it():
    s = box(ws.Partition("x", 50.0, 0.0, 40.0, "brick"))          # runs over y = 0..4 only
    assert s.wall_loss((2, 2, 1), (8, 2, 1), "5") == 12.0
    assert s.wall_loss((2, 8, 1), (8, 8, 1), "5") == 0.0


def test_open_door_is_a_hole_closed_door_is_a_door():
    wall = ws.Partition("x", 50.0, material="concrete", door=ws.Door(50.0, 1.0, 2.0, open=True))
    s = box(wall)
    a, through_door, beside = (2, 5, 1), (8, 5, 1), (8, 9, 1)
    assert s.crossings(a, through_door, "5") == [((5.0, 5.0, 1.0), 0.0, "Open door")]
    assert s.wall_loss(a, beside, "5") == 18.0                     # misses the door: through the concrete
    wall.door.open = False
    assert s.crossings(a, through_door, "5")[0][1:] == (ws.DOOR_LOSS["5"], "Closed door")
    assert s.wall_loss((2, 5, 2.9), (8, 5, 2.9), "5") == 18.0      # above the door: concrete again


def test_crossings_are_ordered_from_the_access_point():
    s = box(ws.Partition("x", 60.0, material="brick"), ws.Partition("x", 30.0, material="glass"))
    assert [c[2] for c in s.crossings((1, 5, 1), (9, 5, 1), "5")] == ["Glass", "Brick"]
    assert [c[2] for c in s.crossings((9, 5, 1), (1, 5, 1), "5")] == ["Brick", "Glass"]


def test_best_access_point_and_coverage_grid():
    s = box(ws.Partition("x", 50.0, material="metal"))
    s.aps = [ws.AccessPoint("A", 2.0, 5.0, 2.0), ws.AccessPoint("B", 8.0, 5.0, 2.0)]
    assert s.best((1.0, 5.0, 1.0))[1].name == "A" and s.best((9.0, 5.0, 1.0))[1].name == "B"
    s.aps[1].enabled = False
    assert s.best((9.0, 5.0, 1.0))[1].name == "A"
    grid = s.coverage(10, 6)
    assert len(grid) == 6 and len(grid[0]) == 10
    assert grid[3][1] > grid[3][8] + 20                    # behind the metal wall it is much weaker
    s.aps[0].enabled = False
    assert s.best((1.0, 5.0, 1.0)) == (ws.NO_SIGNAL, None)
    assert ws.coverage_shares(s.coverage(4, 4))[-1] == pytest.approx(100.0)


def test_bands_and_shares():
    floors = [b[0] for b in ws.SIGNAL_BANDS]
    assert floors == sorted(floors, reverse=True)
    assert ws.band_index(floors[0]) == 0 and ws.band_index(floors[0] - 0.1) == 1
    assert ws.band_title(-200.0) == ws.NO_SERVICE and ws.band_title(-30.0) == ws.SIGNAL_BANDS[0][1]
    shares = ws.coverage_shares([[-40.0, -40.0], [-100.0, -66.0]])
    assert shares[0] == 50.0 and shares[-1] == 25.0 and sum(shares) == pytest.approx(100.0)


# ------------------------------------------------------------------ persistence, editing
def test_round_trip_and_clamping():
    s = ws.demo_site()
    again = ws.Site.from_dict(s.to_dict())
    assert again.to_dict() == s.to_dict()
    bad = ws.Site.from_dict({"width": "x", "depth": -4, "height": 1e9, "clutter": "?", "slice_z": 99,
                             "partitions": [{"axis": "q", "at_pct": 500, "start_pct": 90, "end_pct": 10,
                                             "material": "cheese", "door": {"width_m": 0}}, "junk"],
                             "aps": [{"name": "", "x": 1e6, "y": -3, "z": 99, "band": "60", "tx_dbm": 999}]})
    assert (bad.width, bad.depth, bad.height) == (12.0, ws.MIN_SIDE, ws.MAX_HEIGHT) and bad.clutter == "light"
    p = bad.partitions[0]
    assert (p.axis, p.at_pct, p.start_pct, p.end_pct, p.material) == ("x", 99.0, 10.0, 90.0, "drywall")
    assert p.door.width_m == 0.5 and len(bad.partitions) == 1
    a = bad.aps[0]
    assert (a.name, a.x, a.y, a.z, a.band, a.tx_dbm) == ("AP", 12.0, 0.0, ws.MAX_HEIGHT, "5", 36.0)
    assert bad.slice_z == bad.height
    assert ws.Site.from_dict(None).to_dict() == ws.Site().to_dict()


def test_door_stays_inside_its_wall():
    s = box(ws.Partition("x", 50.0, 20.0, 40.0, door=ws.Door(100.0, 5.0, 9.0)))       # a 2 m wall, a 5 m door
    w = s.walls()[0]
    assert (w.b0, w.b1) == (2.0, 4.0) and (w.d0, w.d1) == (2.0, 4.0) and w.door_h == s.height
    assert box(ws.Partition("x", 50.0, 30.0, 30.0)).walls() == []                     # zero-length: no wall


def test_add_and_mirror_lab_access_points():
    s = box()
    hand = s.add_ap()
    assert hand.name == "AP1" and 0 < hand.x < s.width and 0 < hand.z <= s.height
    assert s.sync_lab_aps(["AP1", "AP7"]) is True
    assert [(a.name, a.from_lab) for a in s.aps] == [("AP1", True), ("AP7", True)]
    assert s.sync_lab_aps(["AP7", "AP1"]) is False                       # nothing changed
    s.add_ap("Spare")
    assert s.sync_lab_aps(["AP7"]) is True                               # AP1 left the lab; the hand-made one stays
    assert [a.name for a in s.aps] == ["AP7", "Spare"]


# ------------------------------------------------------------------ the 3D view
def test_camera_projection_and_picking_round_trip(app):
    from labdesk_app.ui.site3d import Camera, Frame
    cam = Camera()
    cam.target, cam.dist = (5.0, 4.0, 1.0), 25.0
    fr = Frame(cam, 800, 600)
    centre = fr.proj(cam.target)
    assert (centre.x(), centre.y()) == pytest.approx((400.0, 300.0))
    up = fr.proj((5.0, 4.0, 2.0))
    assert up.y() < centre.y() and up.x() == pytest.approx(centre.x())      # z is up on the screen
    for pt in ((1.0, 1.0, 0.0), (9.0, 7.5, 0.0), (3.0, 6.0, 2.5)):
        sp = fr.proj(pt)
        back = fr.on_plane(sp.x(), sp.y(), pt[2])
        assert back == pytest.approx(pt, abs=1e-6)
    assert fr.proj(tuple(e + 5 * (e - t) for e, t in zip(fr.eye, cam.target))) is None    # behind the eye
    behind = tuple(e + (e - t) for e, t in zip(fr.eye, cam.target))
    clipped = fr.poly([cam.target, (6.0, 4.0, 1.0), behind])
    assert clipped is not None and clipped.count() >= 3                    # clipped at the near plane, not dropped


def test_view_paints_selects_and_drags(app):
    from labdesk_app.ui.site3d import SiteView
    v = SiteView(ws.demo_site(), gl=False)                      # the software renderer: no driver involved
    v.resize(900, 640)
    assert v.uses_gl is False and v.gl_reason == "switched off"
    seen = []
    for heat, ramp in ((True, "classic"), (True, "safe"), (False, "classic")):
        v.set_look(heat=heat, ramp=ramp)
        img = v.grab().toImage()
        assert img.width() == 900 and len({img.pixel(x, 400) for x in range(0, 900, 6)}) > 3   # not blank
        seen.append(img)
    assert seen[0] != seen[1] and seen[0] != seen[2]            # the ramp and the heat switch change the picture
    v.set_look(heat=True, ramp="classic")
    ap = v.site.aps[0]
    fr = v._frame()
    sp = fr.proj((ap.x, ap.y, ap.z))
    assert v.ap_at(sp) == 0 and v.ap_at(QPointF(3, 3)) == -1
    assert v.plane.shape == (v.engine.grid.ny, v.engine.grid.nx) and sum(v.shares) == pytest.approx(100.0)
    before = v.plane.copy()
    ap.x, ap.y = 27.0, 4.0
    v.site_changed()
    assert not (v.plane == before).all() and len(v.rooms) == 3
    assert set(v.contours()) == {-50.0, -60.0, -67.0, -75.0} and v.contours()[-60.0]
    v._set_hover(fr.proj((25.0, 4.0, 0.0)))
    assert v._hover == pytest.approx((25.0, 4.0), abs=1e-6)
    v.grab()                                                    # the hover reading paints too


def test_heat_map_colours(app):
    import numpy as np

    from labdesk_app.ui import site3d as s3
    for name in s3.RAMPS:
        lut = s3.ramp_lut(name)
        assert lut.shape == (256, 4) and (lut[:, 3] == 255).all()
        assert s3.ramp_color(name, -200.0) == s3.ramp_color(name, s3.RAMP_LO)        # clamped at both ends
        assert s3.ramp_color(name, 0.0) == s3.ramp_color(name, s3.RAMP_HI)
        assert s3.ramp_image(name).width() == 256 and len(s3.band_colors(name)) == len(ws.SIGNAL_BANDS)
    strong, weak = s3.ramp_color("classic", -42.0), s3.ramp_color("classic", -84.0)
    assert strong.green() > strong.red() and weak.red() > weak.green()             # green = strong, red = weak
    from labdesk_app.ui import theme as T
    light = [T.luminance(s3.ramp_color("safe", d)) for d in range(-85, -39, 5)]
    assert light == sorted(light)                                                   # 'safe' brightens with signal
    plane = np.array([[-40.0, -60.0], [-84.0, -120.0]], np.float32)
    img = s3.heat_image(plane, "classic")
    assert (img.width(), img.height()) == (2, 2)
    assert img.pixelColor(1, 1).alpha() == 0 and img.pixelColor(0, 0).alpha() == 255   # no service: see-through
    assert img.pixelColor(0, 0).green() > img.pixelColor(0, 1).green()
    assert s3.CONTOURS == (-50.0, -60.0, -67.0, -75.0)


def test_obstacles_take_signal_away_and_are_kept_inside():
    s = box()
    ap = ws.AccessPoint("AP1", 1.0, 5.0, 1.0)
    s.aps = [ap]
    s.obstacles = [ws.Obstacle("Rack", "racking_full", 4.0, 3.0, 2.0, 4.0, 2.5)]
    behind, beside, above = (9.0, 5.0, 1.0), (3.0, 9.5, 1.0), (9.0, 5.0, 2.95)
    free = box()
    assert free.rssi(ap, behind) - s.rssi(ap, behind) == pytest.approx(2.0 * 3.5)      # 2 m inside at 3.5 dB/m
    assert s.rssi(ap, beside) == pytest.approx(free.rssi(ap, beside))
    assert s.crossings((1, 5, 1), behind, "5") == [((5.0, 5.0, 1.0), pytest.approx(7.0), "Rack")]
    assert s.wall_loss((1, 5, 1), behind, "2.4") == pytest.approx(2.0 * 2.5)
    assert s.wall_loss((1, 5, 2.9), above, "5") == 0.0                                 # over the top of it
    s.obstacles[0].loss_db_m = 100.0                                                   # capped: it also bends round
    assert s.wall_loss((1, 5, 1), behind, "5") == ws.OBSTACLE_MAX_LOSS
    assert ws.segment_in_box((0, 0, 0), (1, 1, 1), (2, 2, 2), (3, 3, 3)) is None
    assert ws.segment_in_box((0, 0.5, 0.5), (4, 0.5, 0.5), (1, 0, 0), (2, 1, 1)) == pytest.approx((0.25, 0.5))
    s.obstacles[0].loss_db_m = 6.0
    s.obstacles[0].x, s.obstacles[0].w, s.obstacles[0].h = 99.0, 50.0, 50.0
    s.normalize()
    o = s.obstacles[0]
    assert (o.w, o.h, o.x) == (s.width, s.height, 0.0)
    again = ws.Site.from_dict(s.to_dict())
    assert again.obstacles[0].to_dict() == o.to_dict()


def test_a_wall_can_have_its_own_loss():
    p = ws.Partition("x", 50.0, material="concrete")
    assert p.losses() == (12.0, 18.0, 19.8)
    p.loss_db = 27.0
    assert p.losses() == pytest.approx((18.0, 27.0, 29.7))             # the other bands keep the material's ratio
    s = box(p)
    assert s.wall_loss((2, 5, 1), (8, 5, 1), "5") == 27.0
    assert s.wall_loss((2, 5, 1), (8, 5, 1), "2.4") == pytest.approx(18.0)
    assert ws.Partition.from_dict(p.to_dict()).loss_db == 27.0
    assert ws.Partition.from_dict({"loss_db": 999}).loss_db == ws.MAX_WALL_DB and ws.Partition.from_dict({}).loss_db is None


def test_window_edits_the_site_and_saves(app, tmp_path):
    from labdesk_app.ui.site3d import open_site_window
    state = config.State(tmp_path / "state.json")
    win = open_site_window(state, "lab-1", "Demo", ["AP2", "AP1"], gl=False)
    assert [a.name for a in win.site.aps] == ["AP2", "AP1"] and all(a.from_lab for a in win.site.aps)
    assert win.ap_list.count() == 2 and win.wall_list.count() == 2
    win.sp_w.setValue(40.0)
    assert win.site.width == 40.0 and win.a_x.maximum() == 40.0
    win.wall_list.setCurrentRow(0)
    win.w_at.setValue(25.0)
    win.w_door.setCurrentIndex(win.w_door.findData("closed"))
    p = win.site.partitions[0]
    assert p.at_pct == 25.0 and p.door is not None and not p.door.open
    assert "25 %" in win.wall_list.item(0).text() and "door closed" in win.wall_list.item(0).text()
    assert win.w_loss.value() == 18.0 and p.loss_db is None            # concrete: the typical value
    win.w_loss.setValue(26.0)
    assert p.loss_db == 26.0 and "26 dB" in win.wall_list.item(0).text() and "your value" in win.w_loss_hint.text()
    win.w_mat.setCurrentIndex(win.w_mat.findData("brick"))
    assert p.material == "brick" and p.loss_db is None and win.w_loss.value() == 12.0
    assert win.obs_list.count() == 2
    win.obs_list.setCurrentRow(0)
    win.o_kind.setCurrentIndex(win.o_kind.findData("machine"))
    win.o_loss.setValue(6.0)
    win.o_x.setValue(39.5)                                              # pushed back inside the 40 m room
    o = win.site.obstacles[0]
    assert o.kind == "machine" and o.loss_db_m == 6.0 and o.x == pytest.approx(40.0 - o.w) and win.o_x.value() == o.x
    win._add_obs()
    win._del_obs()
    assert len(win.site.obstacles) == 2 and win.obs_list.count() == 2
    win.v_ramp.setCurrentIndex(win.v_ramp.findData("safe"))
    win.v_glow.setValue(25)
    assert win.view.ramp == "safe" and win.view.glow == 0.25
    assert "10.00 m" in win.w_at_m.text()
    win.w_door.setCurrentIndex(win.w_door.findData("none"))
    assert win.site.partitions[0].door is None and not win.w_door_w.isEnabled()
    win._add_wall()
    win._add_ap()
    assert len(win.site.partitions) == 3 and [a.name for a in win.site.aps][-1] == "AP3"
    win.ap_list.setCurrentRow(0)
    win.radio_rows["5"][3].setValue(10.0)
    win.a_on.setChecked(False)
    assert win.site.aps[0].tx_dbm == 10.0 and not win.site.aps[0].enabled and "off" in win.ap_list.item(0).text()
    assert sum(win.view.shares) == pytest.approx(100.0)
    win._del_wall()
    win.flush()
    lab = config.State(tmp_path / "state.json").lab("lab-1")
    saved = lab["site"]
    assert saved["width"] == 40.0 and len(saved["aps"]) == 3 and saved == win.site.to_dict()
    assert lab["site_ui"] == {"heat": True, "glow": 0.25, "ramp": "safe", "drift": True, "air": "waves",
                              "packets": True, "walk": [], "walk_speed": 1.4, "band": None, "map": "signal",
                              "map_channel": None}
    # reopening reads it back; an access point that left the lab is dropped, the hand-made one stays
    again = open_site_window(config.State(tmp_path / "state.json"), "lab-1", "Demo", ["AP1"], gl=False)
    assert [a.name for a in again.site.aps] == ["AP1", "AP3"] and again.site.width == 40.0
    assert again.view.ramp == "safe" and again.view.glow == 0.25 and again.v_glow.value() == 25
    # no lab: the stand-alone room starts from the demo and is kept apart from the labs
    alone = open_site_window(config.State(tmp_path / "state.json"), gl=False)
    assert math.isclose(alone.site.width, ws.demo_site().width) and [a.name for a in alone.site.aps] == ["AP1"]


def test_main_window_opens_the_site_view(app, tmp_path):
    from PySide6.QtCore import QSettings
    QSettings.setPath(QSettings.IniFormat, QSettings.UserScope, str(tmp_path))
    from ui_replay import replay_controller

    from labdesk_app.ui.main_window import MainWindow

    class _L:
        def open(self, cmd):
            return "no"
    ctl = replay_controller(tmp_path)
    ctl.state = config.State(tmp_path / "site-state.json")
    lab_id, ctl.lab_id = ctl.lab_id, None
    win = MainWindow(ctl, _L())
    win.timer.stop()
    try:
        assert win.a_site.shortcut().toString() == "Ctrl+3"
        win.open_site_view()                                   # no lab: the stand-alone room
        alone = win._site_win
        assert alone.isVisible() and alone.lab_id is None and len(alone.site.aps) == 1
        win.open_site_view()
        assert win._site_win is alone                          # opened again: the same window
        ctl.lab_id = lab_id or "lab-x"
        win.open_site_view()                                   # a lab: its own room, no stray access points
        assert win._site_win is not alone and win._site_win.lab_id == ctl.lab_id
        assert win._site_win.site.aps == [] and not alone.isVisible()
        win._site_win.close()
        assert "site" in config.State(tmp_path / "site-state.json").lab(ctl.lab_id)
    finally:
        win._closing = True
        win.runner.wait(3000)
        win.hide()
        win.deleteLater()


def test_view_shows_waves_by_default_and_can_switch_to_fog(app):
    from labdesk_app.ui.site3d import AIR_MODES, FOG_FRAME_MS, WAVE_FRAME_MS, SiteView
    v = SiteView(ws.demo_site(), gl=False)
    assert v.air == "waves" and v._fog_timer.interval() == WAVE_FRAME_MS
    v.set_look(air="fog")
    assert v.air == "fog" and v._fog_timer.interval() == FOG_FRAME_MS
    v.set_look(air="sparkles")                                      # unknown: ignored
    assert v.air == "fog" and set(AIR_MODES) == {"waves", "fog"}


def test_demo_access_point_hangs_at_three_metres():
    s = ws.demo_site()
    assert len(s.aps) == 1 and s.aps[0].z == 3.0
