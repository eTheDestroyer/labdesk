"""The numpy field engine against the reference model, voxel by voxel."""
import os
import random
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from labdesk_app import wifi_field as wf  # noqa: E402
from labdesk_app import wifi_site as ws  # noqa: E402


def reference(site: ws.Site, grid: wf.Grid) -> np.ndarray:
    out = np.empty(grid.shape, np.float64)
    for k, z in enumerate(grid.zd):
        for j, y in enumerate(grid.yd):
            for i, x in enumerate(grid.xd):
                out[k, j, i] = site.best((float(x), float(y), float(z)))[0]
    return out


def random_site(rng: random.Random) -> ws.Site:
    s = ws.Site(rng.uniform(6, 40), rng.uniform(5, 30), rng.uniform(2.5, 8))
    s.clutter = rng.choice(list(ws.CLUTTER))
    for _ in range(rng.randint(0, 4)):
        a, b = sorted((rng.uniform(0, 100), rng.uniform(0, 100)))
        door = ws.Door(rng.uniform(0, 100), rng.uniform(0.7, 2.5), rng.uniform(1.8, 3.0), rng.random() < 0.5) \
            if rng.random() < 0.7 else None
        s.partitions.append(ws.Partition(rng.choice("xy"), rng.uniform(5, 95), a, b, rng.choice(list(ws.MATERIALS)),
                                         door, rng.choice([None, rng.uniform(1, 30)])))
    for n in range(rng.randint(0, 3)):
        s.obstacles.append(ws.Obstacle(f"O{n}", rng.choice(list(ws.OBSTACLE_KINDS)), rng.uniform(0, s.width),
                                       rng.uniform(0, s.depth), rng.uniform(0.5, 6), rng.uniform(0.5, 6),
                                       rng.uniform(0.5, 4), rng.choice([None, rng.uniform(0.5, 12)])))
    for n in range(rng.randint(1, 3)):
        s.aps.append(ws.AccessPoint(f"AP{n}", rng.uniform(0, s.width), rng.uniform(0, s.depth),
                                    rng.uniform(0.3, s.height), rng.uniform(10, 23), rng.choice(["2.4", "5"])))
    s.normalize()
    return s


def test_engine_equals_the_reference_model_on_the_demo_site():
    s = ws.demo_site()
    eng = wf.FieldEngine(s, budget=6000)
    vol = eng.volume()
    assert vol.dtype == np.float32 and vol.shape == eng.grid.shape
    assert np.abs(vol - reference(s, eng.grid)).max() < 1e-3


@pytest.mark.parametrize("seed", range(12))
def test_engine_equals_the_reference_model_on_random_sites(seed):
    s = random_site(random.Random(seed))
    eng = wf.FieldEngine(s, budget=1500)
    assert np.abs(eng.volume() - reference(s, eng.grid)).max() < 2e-3


def test_only_what_changed_is_recomputed():
    s = ws.demo_site()
    s.add_ap("AP2", x=25.0, y=4.0)
    eng = wf.FieldEngine(s, budget=4000)
    first = eng.volume()
    assert eng.computed == 2
    assert eng.volume() is first and eng.computed == 2               # nothing changed
    s.aps[1].x = 26.0
    moved = eng.volume()
    assert eng.computed == 3 and not np.array_equal(moved, first)    # one access point moved: one volume
    s.aps[1].enabled = False
    assert eng.computed == 3 and np.abs(eng.volume() - reference(s, eng.grid)).max() < 1e-3
    s.partitions[0].loss_db = 30.0
    eng.volume()
    assert eng.computed == 4                                         # a wall changed: every live access point
    s.aps[0].enabled = False
    assert float(eng.volume().max()) == ws.NO_SIGNAL


def test_grid_follows_a_voxel_budget():
    for dims in ((30, 18, 5), (120, 80, 10), (500, 500, 30), (2, 2, 2)):
        g = wf.Grid.for_budget(*dims, budget=180_000)
        assert g.size <= 180_000 * 1.25 and g.nz >= 4 and g.nx >= 2 and g.ny >= 2
        assert g.width / g.nx >= wf.MIN_CELL_M * 0.99
    assert wf.Grid.for_budget(30, 18, 5).size > 100_000              # the budget is actually used


def test_slice_interpolates_between_layers_and_shares_match():
    s = ws.demo_site()
    eng = wf.FieldEngine(s, budget=6000)
    vol, g = eng.volume(), eng.grid
    assert np.array_equal(eng.slice(float(g.zd[1])), vol[1])
    mid = eng.slice(float((g.zd[1] + g.zd[2]) / 2))
    assert np.allclose(mid, (vol[1] + vol[2]) / 2, atol=1e-4)
    assert np.array_equal(eng.slice(0.0), vol[0]) and np.array_equal(eng.slice(99.0), vol[-1])
    plane = eng.slice()
    assert wf.band_shares(plane) == pytest.approx(ws.coverage_shares(plane.tolist()))
    assert sum(wf.band_shares(plane)) == pytest.approx(100.0)


def test_contours_lie_on_the_level():
    yy, xx = np.mgrid[0:40, 0:60].astype(np.float32)
    plane = -40.0 - np.hypot(xx - 30, yy - 20)                       # cones around (30, 20): circles
    segs = wf.contour_segments(plane, -50.0)
    assert len(segs) > 40
    for (x0, y0), (x1, y1) in segs:
        for x, y in ((x0, y0), (x1, y1)):
            assert abs(np.hypot(x - 30, y - 20) - 10.0) < 0.25
    assert wf.contour_segments(plane, 0.0) == [] and wf.contour_segments(plane[:1], -50.0) == []
