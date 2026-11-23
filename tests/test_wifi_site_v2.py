"""Version 2 of the site model: doors and windows anywhere in a wall, outer walls, Israeli materials, 6 GHz,
furniture with heights and per-item caps, room names, the room file format and the room library."""
import json
import math
import os
import random
import sys

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from labdesk_app import wifi_field as wf  # noqa: E402
from labdesk_app import wifi_site as ws  # noqa: E402

ROOMS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "labdesk_app", "rooms")


def flat() -> ws.Site:
    """A small Israeli flat: block outer walls with windows, a mamad, a kitchen, two doors in one wall."""
    s = ws.Site(10.0, 8.0, 2.7, clutter="home")
    s.partitions = [
        ws.Partition("y", 0.0, 0.0, 100.0, "block_20", role="outer", name="South",
                     openings=[ws.window(30.0, 1.4, 0.9, 1.3, "double_glazing", "slits"),
                               ws.window(75.0, 1.2, 0.9, 1.3, "double_glazing")]),
        ws.Partition("x", 40.0, 0.0, 100.0, "block_10",
                     openings=[ws.door(25.0, 0.8, fill="door_hollow"), ws.door(75.0, 0.9, open=True)]),
        ws.Partition("y", 60.0, 40.0, 100.0, "mamad_20", openings=[ws.door(50.0, 0.8, fill="blast_door")]),
    ]
    s.labels = [ws.RoomLabel("Living room", 2.0, 4.0, "living", "סלון"),
                ws.RoomLabel("Safe room", 7.0, 6.5, "mamad", "ממ\"ד")]
    s.obstacles = [ws.furniture("fridge", "Fridge", 0.2, 6.5), ws.furniture("sofa", "Sofa", 1.0, 1.0),
                   ws.Obstacle("Upper cabinets", "kitchen", 0.0, 7.4, 3.0, 0.6, 0.7, z0=1.5)]
    s.aps = [ws.AccessPoint("Router", 3.0, 7.5, 1.5)]
    s.meta = {"title": "Test flat", "title_he": "דירת בדיקה", "kind": "home",
              "source": {"title": "made up", "url": "https://example.org/plan.pdf", "license": "test"}}
    s.normalize()
    return s


# ---------------------------------------------------------------------------- model
def test_version_1_files_load_and_save_unchanged():
    d = ws.demo_site().to_dict()
    assert "format" not in d and "openings" not in json.dumps(d)
    assert ws.Site.from_dict(d).to_dict() == d
    p = d["partitions"][0]
    assert set(p) == {"axis", "at_pct", "start_pct", "end_pct", "material", "door", "loss_db"}


def test_a_window_counts_only_between_its_sill_and_its_head():
    s = ws.Site(10, 10, 3)
    s.partitions = [ws.Partition("x", 50.0, material="concrete_20",
                                 openings=[ws.window(50.0, 2.0, 1.0, 1.0, "double_glazing")])]
    wall = ws.MATERIALS["concrete_20"].loss[1]
    glass = ws.FILLS["double_glazing"].loss[1]
    assert s.wall_loss((2, 5, 1.5), (8, 5, 1.5), "5") == pytest.approx(glass)       # through the pane
    assert s.wall_loss((2, 5, 0.5), (8, 5, 0.5), "5") == pytest.approx(wall)        # under the sill
    assert s.wall_loss((2, 5, 2.5), (8, 5, 2.5), "5") == pytest.approx(wall)        # over the head
    s.partitions[0].openings[0].shutter = "closed"                                  # trisim down
    assert s.wall_loss((2, 5, 1.5), (8, 5, 1.5), "5") == pytest.approx(glass + ws.SHUTTERS["closed"].loss[1])
    s.partitions[0].openings[0].open = True                                         # sash open: shutter still there
    assert s.wall_loss((2, 5, 1.5), (8, 5, 1.5), "5") == pytest.approx(ws.SHUTTERS["closed"].loss[1])


def test_several_doors_in_one_wall_and_overlaps_are_reported():
    s = flat()
    w = s.walls()[1]
    assert len(w.holes) == 2
    hollow, open_ = ws.FILLS["door_hollow"].loss[1], 0.0
    y_a, y_b = (w.holes[0].d0 + w.holes[0].d1) / 2, (w.holes[1].d0 + w.holes[1].d1) / 2
    assert s.wall_loss((2, y_a, 1), (6, y_a, 1), "5") == pytest.approx(hollow)
    assert s.wall_loss((2, y_b, 1), (6, y_b, 1), "5") == pytest.approx(open_)
    s.partitions[1].openings.append(ws.door(26.0, 0.8))                             # on top of the first one
    assert len(s.walls()[1].holes) == 2
    assert any("overlaps" in m for m in s.problems())
    assert s.partitions[1].door is s.partitions[1].openings[0]                      # version 1's door: the first


def test_three_bands_and_the_israeli_limits():
    s = ws.Site(10, 10, 3, clutter="open")
    assert s.path_loss(1.0, "6") == pytest.approx(20 * math.log10(6185) - 27.55)
    assert s.path_loss(1.0, "6") > s.path_loss(1.0, "5") > s.path_loss(1.0, "2.4")
    s.partitions = [ws.Partition("x", 50.0, material="mamad_25")]
    for band in ws.BANDS:
        assert s.wall_loss((2, 5, 1), (8, 5, 1), band) == pytest.approx(ws.MATERIALS["mamad_25"].loss[ws.bi(band)])
    ap = ws.AccessPoint("a", 1, 1, 2, tx_dbm=23.0, band="2.4")
    assert ap.over_limit() and not ws.AccessPoint("b", 1, 1, 2, tx_dbm=23.0, band="6").over_limit()
    s.aps = [ap]
    assert any("legal limit" in m for m in s.problems())
    assert ws.AccessPoint.from_dict({"band": "6"}).band == "6"
    assert ws.Site.from_dict(ws.Site(aps=[ws.AccessPoint("x", 1, 1, band="6")]).to_dict()).aps[0].band == "6"


def test_every_table_entry_is_complete():
    for table in (ws.MATERIALS, ws.FILLS, ws.SHUTTERS):
        for k, m in table.items():
            assert m.title and m.title_he and len(m.loss) == 3 and all(v >= 0 for v in m.loss), k
    for k, kind in ws.OBSTACLE_KINDS.items():
        assert kind.title and kind.title_he and len(kind.per_m) == 3 and len(kind.cap) == 3 and len(kind.size) == 4, k
    assert ws.DOOR_LOSS == {"2.4": 3.0, "5": 4.0, "6": 4.4}                         # version 1's closed door
    for k in ws.V1_MATERIALS:                                                       # old files keep their numbers
        assert ws.MATERIALS[k].loss[:2] == {"drywall": (3, 4), "glass": (2, 3), "wood": (4, 5), "brick": (8, 12),
                                           "concrete": (12, 18), "metal": (20, 26)}[k]


def test_an_item_takes_at_most_its_cap_and_soft_furniture_nothing():
    s = ws.Site(10, 10, 3)
    s.obstacles = [ws.furniture("fridge", "F", 4.65, 4.65)]
    loss = s.wall_loss((1, 5, 1), (9, 5, 1), "5")
    assert loss == pytest.approx(ws.OBSTACLE_KINDS["fridge"].cap[1])
    s.obstacles = [ws.furniture("sofa", "S", 4.0, 4.6)]
    assert s.wall_loss((1, 5, 0.4), (9, 5, 0.4), "5") == 0.0
    s.obstacles = [ws.Obstacle("Shelf", "racking_full", 4.0, 0.0, 1.0, 10.0, 2.0)]   # version 1: dB/m, cap 25
    assert s.wall_loss((1, 5, 1), (9, 5, 1), "5") == pytest.approx(3.5)


def test_a_hanging_item_lets_the_signal_pass_below_it():
    s = ws.Site(10, 10, 3)
    s.obstacles = [ws.Obstacle("Upper cabinets", "kitchen", 4.0, 0.0, 2.0, 10.0, 0.7, z0=1.5)]
    assert s.wall_loss((1, 5, 0.5), (9, 5, 0.5), "5") == 0.0
    assert s.wall_loss((1, 5, 1.8), (9, 5, 1.8), "5") > 0.0


def test_an_access_point_never_sits_on_a_wall_plane():
    s = ws.Site(10, 10, 3)
    s.partitions = [ws.Partition("x", 50.0, material="concrete_20")]
    s.aps = [ws.AccessPoint("a", 5.0, 5.0, 2.0)]
    s.normalize()
    assert abs(s.aps[0].x - 5.0) == pytest.approx(ws.AP_WALL_CLEARANCE)
    assert s.wall_loss((s.aps[0].x, 5, 2), (8, 5, 1), "5") > 0 or s.wall_loss((s.aps[0].x, 5, 2), (2, 5, 1), "5") > 0


def test_room_file_keeps_meta_labels_and_unknown_keys():
    s = flat()
    d = s.to_dict()
    assert d["format"] == "labdesk-room" and d["version"] == 2 and d["meta"]["title_he"] == "דירת בדיקה"
    d["future_key"] = {"x": 1}
    d["partitions"][1]["colour"] = "teal"
    d["partitions"][1]["openings"][0]["hinge"] = "left"
    d["obstacles"][0]["brand"] = "Tadiran"
    again = ws.Site.from_dict(d)
    back = again.to_dict()
    assert back["future_key"] == {"x": 1} and back["partitions"][1]["colour"] == "teal"
    assert back["partitions"][1]["openings"][0]["hinge"] == "left" and back["obstacles"][0]["brand"] == "Tadiran"
    assert [lb.name_he for lb in again.labels] == ["סלון", "ממ\"ד"]
    assert ws.Site.from_dict(back).to_dict() == back
    assert ws.Site.newer_than_supported({"version": 3}) and not ws.Site.newer_than_supported(back)
    spike = ws.Site.from_dict({"format": "labdesk-site", "version": 2, "name": "Old spike", "width": 5})
    assert spike.title == "Old spike"


def test_labels_name_rooms_and_outside_is_left_out_of_the_shares():
    s = ws.Site(10, 4, 3, clutter="home")
    s.partitions = [ws.Partition("x", 70.0, material="block_20", role="outer")]
    s.labels = [ws.RoomLabel("Flat", 3, 2, "living"), ws.RoomLabel("Yard", 8.5, 2, "outside")]
    s.aps = [ws.AccessPoint("a", 2, 2, 2)]
    s.normalize()
    named = {lb.name: r for r, lb in s.named_rooms()}
    assert named["Flat"].area_m2 == pytest.approx(28.0) and s.outside_rects() == list(named["Yard"].rects)
    eng = wf.FieldEngine(s, budget=20000)
    mask = wf.indoor_mask(s, eng.grid)
    assert mask is not None and 0.25 < 1 - mask.mean() < 0.35
    plane = np.where(mask, -45.0, -95.0).astype(np.float32)          # good inside, nothing outside
    assert wf.band_shares(plane, mask)[0] == pytest.approx(100.0)
    assert wf.band_shares(plane)[0] < 80.0


# ---------------------------------------------------------------------------- the engines agree
def random_site_v2(rng: random.Random) -> ws.Site:
    s = ws.Site(rng.uniform(5, 30), rng.uniform(4, 20), rng.uniform(2.5, 6))
    s.clutter = rng.choice(list(ws.CLUTTER))
    fills = list(ws.FILLS)
    for _ in range(rng.randint(0, 5)):
        a, b = sorted((rng.uniform(0, 100), rng.uniform(0, 100)))
        ops = []
        for _k in range(rng.randint(0, 3)):
            if rng.random() < 0.5:
                ops.append(ws.window(rng.uniform(0, 100), rng.uniform(0.5, 2.5), rng.uniform(0.3, 1.5),
                                     rng.uniform(0.5, 1.8), rng.choice(fills), rng.choice(list(ws.SHUTTERS)),
                                     rng.random() < 0.3))
            else:
                ops.append(ws.door(rng.uniform(0, 100), rng.uniform(0.7, 2.0), rng.uniform(1.8, 2.4),
                                   rng.choice(fills), rng.random() < 0.4))
        role = "outer" if rng.random() < 0.2 else "inner"
        s.partitions.append(ws.Partition(rng.choice("xy"), rng.uniform(5, 95), a, b, rng.choice(list(ws.MATERIALS)),
                                         None, rng.choice([None, rng.uniform(1, 50)]), ops, role))
    for n in range(rng.randint(0, 4)):
        kind = rng.choice(list(ws.OBSTACLE_KINDS))
        s.obstacles.append(ws.Obstacle(f"O{n}", kind, rng.uniform(0, s.width), rng.uniform(0, s.depth),
                                       rng.uniform(0.3, 5), rng.uniform(0.3, 5), rng.uniform(0.3, 3),
                                       rng.choice([None, rng.uniform(0.5, 60)]), rng.choice([0.0, rng.uniform(0, 1.5)])))
    for n in range(rng.randint(1, 3)):
        s.aps.append(ws.AccessPoint(f"AP{n}", rng.uniform(0, s.width), rng.uniform(0, s.depth),
                                    rng.uniform(0.3, s.height), rng.uniform(10, 23), rng.choice(list(ws.BANDS))))
    s.normalize()
    return s


def reference(site: ws.Site, grid: wf.Grid) -> np.ndarray:
    walls = site.walls()
    out = np.empty(grid.shape, np.float64)
    for k, z in enumerate(grid.zd):
        for j, y in enumerate(grid.yd):
            for i, x in enumerate(grid.xd):
                out[k, j, i] = site.best((float(x), float(y), float(z)), walls)[0]
    return out


@pytest.mark.parametrize("seed", range(10))
def test_numpy_engine_equals_the_model_on_random_v2_sites(seed):
    s = random_site_v2(random.Random(100 + seed))
    eng = wf.FieldEngine(s, budget=1500)
    assert np.abs(eng.volume() - reference(s, eng.grid)).max() < 2e-3


def test_numpy_engine_equals_the_model_on_the_test_flat():
    s = flat()
    eng = wf.FieldEngine(s, budget=4000)
    assert np.abs(eng.volume() - reference(s, eng.grid)).max() < 2e-3


@pytest.fixture(scope="module")
def renderer():
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    from labdesk_app.ui import site3d_gl as gl
    r, why = gl.SceneRenderer.create()
    if r is None:
        pytest.skip(f"no GPU renderer on this machine: {why}")
    yield r
    r.close()


def _gpu_worst(renderer, s, budget):
    from labdesk_app.ui import site3d_gl as gl
    assert renderer.compute_field(s, budget=budget), renderer.last_error
    nx, ny, nz = renderer.field_shape
    g = wf.Grid(s.width, s.depth, s.height, nx, ny, nz)
    ref = np.maximum(reference(s, g), gl.NO_FIELD_DBM)
    worst = 0.0
    for k in range(nz):
        worst = max(worst, float(np.abs(renderer.read_field_layer(k) - ref[k]).max()))
    return worst


@pytest.mark.parametrize("seed", range(10))
def test_gpu_field_equals_the_model_on_random_v2_sites(renderer, seed):
    assert _gpu_worst(renderer, random_site_v2(random.Random(100 + seed)), 1500) < 2e-3


def test_gpu_field_equals_the_model_on_the_test_flat(renderer):
    assert _gpu_worst(renderer, flat(), 4000) < 2e-3


def test_the_mesh_draws_windows_glass_and_furniture():
    from labdesk_app.ui import site3d_gl as gl
    s = flat()
    s.partitions[1].openings.append(ws.window(50.0, 1.0, 1.0, 1.0, "glass"))
    data, n_ground, n_floor, n_solid = gl.build_mesh(gl.site_geometry(s), 0)
    kinds = {data[i + 6] for i in range(0, len(data), gl.STRIDE)}
    assert {gl.K_WFRAME, gl.K_SHUTTER, gl.K_STEEL, gl.K_LEAF} <= kinds
    glass = {data[i + 6] for i in range(n_solid * gl.STRIDE, len(data), gl.STRIDE)}
    assert glass == {gl.K_GLASS} and n_solid < len(data) // gl.STRIDE           # glass last, on its own
    tags = {data[i + 7] for i in range(0, len(data), gl.STRIDE) if data[i + 6] == gl.K_OBSTACLE}
    assert float(gl.PALETTE.index("fridge")) in tags and float(gl.PALETTE.index("sofa")) in tags


# ---------------------------------------------------------------------------- room library
def test_library_reads_good_and_broken_files(tmp_path):
    from labdesk_app.ui import room_library as rl
    good = tmp_path / "good.json"
    rl.write_room(good, flat())
    (tmp_path / "broken.json").write_text("{not json", encoding="utf-8")
    (tmp_path / "list.json").write_text("[1, 2]", encoding="utf-8")
    (tmp_path / "other.json").write_text(json.dumps({"format": "geojson", "width": 3}), encoding="utf-8")
    (tmp_path / "bom.json").write_bytes(b"\xef\xbb\xbf" + json.dumps(flat().to_dict()).encode())
    entries = {e.path.name: e for e in rl.scan([tmp_path])}
    assert not entries["good.json"].error and entries["good.json"].title_he == "דירת בדיקה"
    assert not entries["bom.json"].error
    assert all(entries[n].error for n in ("broken.json", "list.json", "other.json"))
    e = entries["good.json"]
    assert e.source["url"].startswith("https://") and "1 AP" in e.summary()
    assert e.matches("בדיקה") and e.matches("test flat") and not e.matches("haifa")
    assert rl.read_entry(good).site.to_dict() == flat().to_dict()
    e.site.meta["source"]["url"] = "javascript:alert(1)"                            # only web links are links
    assert "url" not in e.source
    assert rl.slug("דירה בחיפה: 4/5") == "דירה_בחיפה_4_5"
    m = rl.edited_copy_meta({"title": "Flat"})
    assert m == {"title": "Flat (edited)", "based_on": "Flat"} and rl.edited_copy_meta(m) == m


def test_library_dialog_lists_and_opens_a_copy(tmp_path):
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    from labdesk_app.ui import room_library as rl
    rl.write_room(tmp_path / "a.json", flat())
    (tmp_path / "b.json").write_text("{", encoding="utf-8")
    thumbs = rl.Thumbnailer(None, gl=False)
    dlg = rl.RoomLibraryDialog([tmp_path], thumbs=thumbs)
    dlg.render_all()
    assert dlg.list.count() == 2 and thumbs.rendered == 1
    dlg.list.setCurrentRow(0)
    assert dlg.open_btn.isEnabled() and dlg.d_title_he.text() == "דירת בדיקה"
    dlg._accept_current()
    assert dlg.chosen() is not None and dlg.chosen().site.title == "Test flat"
    dlg.list.setCurrentRow(1)                                                       # the broken file says why
    assert not dlg.open_btn.isEnabled() and "cannot" in dlg.d_warn.text()
    thumbs.close()


def test_site_window_loads_a_library_room_in_place(tmp_path):
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    from labdesk_app.ui.site3d import SiteWindow
    saved = []
    win = SiteWindow(ws.demo_site(), lambda d, ui: saved.append(d), "Test", gl=False)
    site_obj = win.site
    win.load_site(flat())
    assert win.site is site_obj and win.view.site is site_obj and win.site.title == "Test flat"
    assert "Test flat" in win.r_title.text() and "example.org" in win.r_source.text() and "made up" in win.r_source.toolTip()
    assert "Living room" in win.rooms_lbl.text()
    win.flush()
    assert saved and saved[-1]["meta"]["title"] == "Test flat"
    win.close()


# ---------------------------------------------------------------------------- the shipped rooms
def shipped():
    return sorted(f for f in os.listdir(ROOMS_DIR) if f.endswith(".json")) if os.path.isdir(ROOMS_DIR) else []


def test_ten_israeli_rooms_ship_each_from_its_own_plan():
    files = shipped()
    assert len(files) == 10, files
    urls = set()
    for name in files:
        raw = json.load(open(os.path.join(ROOMS_DIR, name), encoding="utf-8"))
        s = ws.Site.from_dict(raw)
        m = s.meta
        assert raw.get("format") == "labdesk-room", name
        assert m.get("title") and m.get("title_he") and m.get("kind") in ("home", "office", "workshop", "public"), name
        assert str(m["source"]["url"]).startswith("https://") and m["source"].get("license"), name
        assert m.get("assumptions"), name
        urls.add(m["source"]["url"])
        assert s.to_dict() == ws.Site.from_dict(s.to_dict()).to_dict(), name
        assert s.problems() == [], (name, s.problems())
        assert len(s.aps) >= 1 and s.labels, name
        names = [lb for _r, lb in s.named_rooms() if lb is not None]
        assert len(names) == len(s.labels), (name, "a label shares a room or sits outside every room")
    assert len(urls) == 10


# ---------------------------------------------------------------------------- the link to a phone
def test_link_layer_snr_speed_and_the_phone_side():
    from labdesk_app import wifi_link as wl
    s = ws.Site(10, 10, 3, clutter="home")
    ap = ws.AccessPoint("a", 1, 1, 2, tx_dbm=20.0, band="5")
    near = wl.link(s, -45.0, ap)
    far = wl.link(s, -80.0, ap)
    assert near.up_dbm == pytest.approx(-50.0) and near.uplink_limits             # 15 dBm phone vs 20 dBm AP
    assert near.snr_db == pytest.approx(-50.0 - wl.noise_floor("5"))
    assert near.rate_hi == wl.RATE_2SS[80][-1] and near.rate_lo > far.rate_hi
    assert far.rate_lo == 0.0 and wl.link(s, -110.0, ap).speed_text() == "no connection"
    assert "cannot hear the phone" in wl.link(s, -79.0, ap).speed_text()       # the AP reaches it, not back
    assert wl.phy_rate(-82.0, "2.4") == wl.RATE_2SS[20][0] and wl.phy_rate(-83.0, "2.4") == 0.0
    assert wl.phy_rate(-76.0, "5") == wl.RATE_2SS[80][0]                          # +6 dB for 80 MHz
    assert wl.link(s, -60.0, None) is None
    weak_ap = ws.AccessPoint("b", 1, 1, 2, tx_dbm=10.0, band="2.4")
    assert not wl.link(s, -60.0, weak_ap).uplink_limits                           # a 10 dBm AP: the AP limits
    from labdesk_app.ui.site3d import SiteView
    assert "SNR" in SiteView.link_text(near) and "phone limits" in SiteView.link_text(near)


def test_panel_edits_doors_windows_and_raised_furniture(tmp_path):
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    from labdesk_app.ui.site3d import SiteWindow
    win = SiteWindow(flat(), None, "Test", gl=False)
    win.wall_list.setCurrentRow(0)                                   # the south facade, on the box edge
    assert win.w_outer.isChecked() and win.w_at.value() == 0.0 and win.op_list.count() == 2
    win.op_list.setCurrentRow(0)
    win.op_shutter.setCurrentIndex(win.op_shutter.findData("closed"))
    assert win.site.partitions[0].openings[0].shutter == "closed" and win.site.partitions[0].at_pct == 0.0
    win._add_op("window")
    assert len(win.site.partitions[0].openings) == 3 and win.op_list.count() == 3
    win.op_fill.setCurrentIndex(win.op_fill.findData("low_e"))
    assert win.site.partitions[0].openings[2].fill == "low_e" and "28" in win.op_loss.text()
    win._del_op()
    assert len(win.site.partitions[0].openings) == 2
    assert "2 windows" in win.wall_list.item(0).text() and "outer" in win.wall_list.item(0).text()
    win.wall_list.setCurrentRow(1)                                   # two doors: the quick controls show the first
    assert win.op_list.count() == 2 and win.w_door.currentData() == "closed"
    win.obs_list.setCurrentRow(2)                                    # the upper kitchen cabinets
    assert win.o_z0.value() == pytest.approx(1.5)
    win.o_z0.setValue(1.2)
    assert win.site.obstacles[2].z0 == pytest.approx(1.2)
    win.o_kind.setCurrentIndex(win.o_kind.findData("sofa"))
    assert "Drawn only" in win.o_hint.text() and win.site.obstacles[2].z0 == 0.0
    win.close()


# ---------------------------------------------------------------------------- animations
def test_packets_follow_the_clock_thin_out_through_walls_and_come_back_fewer():
    from labdesk_app import wifi_anim as wa
    s = ws.Site(20, 6, 3, clutter="home")
    ap = ws.AccessPoint("a", 1, 3, 2.5)
    s.aps = [ap]
    open_ = wa.Flow(s, ap, (15, 3, 1))
    s.partitions = [ws.Partition("x", 50.0, material="mamad_25")]
    walled = wa.Flow(s, ap, (15, 3, 1))
    assert open_.packets(10.0) == open_.packets(10.0)                      # a function of time only
    assert open_.packets(10.0) != open_.packets(10.3)                      # ... that moves
    def count(flow, side):
        n = 0
        for k in range(400):
            n += sum(1 for pk in flow.packets(k * 0.05) if not pk.up and side(pk.x))
        return n
    survive_walled = count(walled, lambda x: x > 10.5) / max(1, count(walled, lambda x: x < 9.5))
    survive_open = count(open_, lambda x: x > 10.5) / max(1, count(open_, lambda x: x < 9.5))
    assert walled.rate < open_.rate                                         # a slower link sends fewer
    assert survive_walled < 0.35 * survive_open                             # and most die at the mamad wall
    pk_far = [pk for k in range(200) for pk in open_.packets(k * 0.05) if not pk.up and pk.x > 12]
    assert pk_far and all(pk.dbm < ap.tx_dbm for pk in pk_far)
    assert open_.rate_up < open_.rate                                       # fewer come back from the phone
    assert any(pk.up for k in range(200) for pk in open_.packets(k * 0.05))
    far = wa.Flow(ws.Site(400, 6, 3, aps=[ap], clutter="dense"), ap, (390, 3, 1))   # out of range: none arrive
    assert all(pk.x < 300 for k in range(100) for pk in far.packets(k * 0.5) if not pk.up)


def test_beacon_and_walk_helpers():
    from labdesk_app import wifi_anim as wa
    assert wa.beacon(0.0) == pytest.approx(1.0) and wa.beacon(wa.BEACON_PERIOD * 0.9) < 0.05
    assert wa.beacon(3 * wa.BEACON_PERIOD) == pytest.approx(1.0) and wa.beacon(float("nan")) == 0.0
    path = [(0, 0), (3, 0), (3, 4)]
    assert wa.path_length(path) == 7.0
    assert wa.walk_position(path, 5.0) == pytest.approx((3.0, 2.0))
    assert wa.walk_position(path, 99) == (3, 4) and wa.walk_position([], 1) is None
    s = flat()
    s.aps.append(ws.AccessPoint("Mamad AP", 7.5, 6.5, 2.0))
    samples = wa.walk_samples(s, [(1.0, 4.0), (8.0, 4.0), (8.0, 7.0)], 1.0)
    assert samples[0].dist == 0 and samples[-1].dist == pytest.approx(10.0)
    assert samples[0].dbm == pytest.approx(s.best((1.0, 4.0, 1.0))[0])
    assert samples[0].ap == "Router" and samples[-1].ap == "Mamad AP"
    assert wa.roams(samples)[0].ap == "Mamad AP"                            # it roams into the safe room


def test_view_walks_the_phone_and_saves_the_path():
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    from labdesk_app.ui.site3d import SiteWindow
    saved = []
    win = SiteWindow(flat(), lambda d, ui: saved.append(ui), "Test", gl=False)
    v = win.view
    win.k_draw.setChecked(True)
    assert v.walk_drawing and "Click on the floor" in win.k_status.text()
    v.walk_path += [(1.0, 4.0), (8.0, 4.0)]
    win.k_draw.setChecked(False)                                           # finishing starts the walk
    assert not v.walk_drawing and v.walking and win.k_walk.isChecked()
    v.advance_walk(2.0)
    assert v.walk_dist == pytest.approx(2.0 * v.walk_speed)
    assert "dBm" in win.k_status.text() and "of 7.0 m" in win.k_status.text()
    assert v.phone()[:2] == pytest.approx((1.0 + 2.0 * v.walk_speed, 4.0))
    v.advance_walk(100.0)                                                  # the end: a pause, then round again
    assert v.walk_dist == pytest.approx(7.0)
    v.advance_walk(2.0)
    assert v.walk_dist == 0.0
    v.resize(800, 600)
    img = v.grab()                                                         # trail, phone, packets, beacon paint
    assert not img.isNull()
    win.k_walk.setChecked(False)
    assert not v.walking
    win.flush()
    assert saved[-1]["walk"] == [[1.0, 4.0], [8.0, 4.0]] and saved[-1]["packets"] is True
    again = SiteWindow(flat(), None, "Test", gl=False, ui=saved[-1])
    assert again.view.walk_path == [(1.0, 4.0), (8.0, 4.0)] and not again.view.walking
    win.k_clear.click()
    assert v.walk_path == [] and not win.k_walk.isEnabled()
    win.close()
    again.close()
