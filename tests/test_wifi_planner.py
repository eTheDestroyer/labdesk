"""The access point planner: as few access points as reach the target, on channels that do not clash."""
import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from labdesk_app import wifi_interference as wi  # noqa: E402
from labdesk_app import wifi_planner as wp  # noqa: E402
from labdesk_app import wifi_site as ws  # noqa: E402
from tests.test_wifi_site_v2 import flat  # noqa: E402


def _covered(site: ws.Site, target: float) -> float:
    """Share of a fine grid of the floor (outside furniture) at the target, with the model's own numbers."""
    walls = site.walls()
    pts = [(x + 0.25, y + 0.25) for x in np.arange(0, site.width, 0.5) for y in np.arange(0, site.depth, 0.5)]
    pts = [(x, y) for x, y in pts if not any(o.x < x < o.x + o.w and o.y < y < o.y + o.d and o.z0 <= site.slice_z
                                             <= o.z0 + o.h for o in site.obstacles)]
    ok = sum(site.best((x, y, site.slice_z), walls, "5")[0] >= target for x, y in pts)
    return 100.0 * ok / len(pts)


def test_plan_reaches_the_target_with_few_access_points_and_no_shared_channel():
    s = flat()
    before = s.to_dict()
    p = wp.plan(s)
    assert s.to_dict() == before                                        # the site itself is not touched
    assert p.covered_pct >= 97.5 and p.count == len(p.added) >= 1
    assert not p.conflicts and p.width == 80
    new = ws.Site.from_dict(before)
    new.aps = p.aps
    assert _covered(new, -60.0) >= 95.0                                 # the model agrees, point by point
    assert not [c for c in wi.channel_conflicts(new) if not c.b.neighbour]
    for a in p.aps:
        assert 0 < a.x < s.width and 0 < a.y < s.depth and a.z == pytest.approx(s.height - 0.3)
        assert a.radio("5") is not None and a.radio("2.4") is not None   # dual band by default
        assert a.tx_dbm <= ws.LEGAL_EIRP_DBM["5"]
    assert all(n.startswith("AP") for n in p.added)
    assert "access point" in p.summary() and "share a channel" in p.summary()


def test_a_weaker_target_needs_no_more_access_points_and_one_spot_cannot_cover_everything():
    s = flat()
    good = wp.plan(s, wp.PlanOptions(target_dbm=-67.0))
    best = wp.plan(s, wp.PlanOptions(target_dbm=-50.0))
    assert good.count <= best.count and best.count >= 2


def test_plan_keeps_neighbours_and_lab_access_points_and_optionally_yours():
    s = flat()
    s.aps.append(ws.AccessPoint("Neighbour", 9.5, 0.5, 2.0, neighbour=True))
    s.aps.append(ws.AccessPoint("Lab AP", 2.0, 2.0, 2.5, from_lab=True))
    p = wp.plan(s)
    names = [a.name for a in p.aps]
    assert "Router" not in names and "Neighbour" in names and "Lab AP" in names
    assert p.aps[-1].neighbour                                          # yours first, the neighbours after
    kept = wp.plan(s, wp.PlanOptions(keep=True))
    assert "Router" in [a.name for a in kept.aps] and kept.count >= 2


def test_spots_stay_off_walls_and_out_of_the_yard():
    s = flat()
    z = wp.mount_height(s, wp.PlanOptions())
    spots = wp.candidates(s, z)
    walls = s.walls()
    assert len(spots) > 20
    for x, y in spots:
        for w in walls:
            a, b = (x, y) if w.axis == "x" else (y, x)
            assert not (abs(a - w.a) < wp.WALL_CLEAR_M and w.b0 <= b <= w.b1)


def test_a_dense_plan_quiets_2_4_and_narrows_5_ghz_instead_of_sharing():
    hall = ws.Site(40.0, 40.0, 4.0, clutter="open")                       # a hall: everyone hears everyone
    hall.normalize()
    p = wp.plan(hall, wp.PlanOptions(target_dbm=-50.0))
    assert p.count >= 4 and not p.conflicts
    radios = [a.radio("2.4") for a in p.aps]
    assert any(r is None or r.tx_dbm < 20.0 for r in radios)               # 1, 6, 11 are not enough for all
    assert any("2.4 GHz" in n for n in p.notes)
    big = ws.Site(70.0, 70.0, 4.0, clutter="open")
    big.normalize()
    q = wp.plan(big, wp.PlanOptions(target_dbm=-50.0, dual_band=False))
    assert q.count > 4 and q.width < 80 and not q.conflicts                # more than four 80 MHz blocks needed
    assert all(a.radio("2.4") is None for a in q.aps)


def test_window_plans_and_undoes():
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    from labdesk_app.ui.site3d import SiteWindow
    saved = []
    win = SiteWindow(flat(), lambda d, ui: saved.append(d), "Test", gl=False)
    p = win.plan_aps(show_progress=False)
    assert p is not None and [a.name for a in win.site.aps] == [a.name for a in p.aps]
    assert win.ap_list.count() == len(p.aps) and "access point" in win.p_result.text()
    assert win.p_undo.isEnabled()
    win.undo_plan()
    assert [a.name for a in win.site.aps] == ["Router"] and not win.p_undo.isEnabled()
    win.flush()
    assert [a["name"] for a in saved[-1]["aps"]] == ["Router"]
    win.close()
