"""Bands, channels and interference: the channel tables, radios on access points, neighbours' networks, the
SINR / sharing / channel maps (numpy against the point-by-point reference), the channel plan and the panel."""
import math
import os

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from labdesk_app import wifi_channels as wc  # noqa: E402
from labdesk_app import wifi_field as wf  # noqa: E402
from labdesk_app import wifi_interference as wi  # noqa: E402
from labdesk_app import wifi_link as wl  # noqa: E402
from labdesk_app import wifi_maps as wm  # noqa: E402
from labdesk_app import wifi_site as ws  # noqa: E402


# ---------------------------------------------------------------------------- channel tables
def test_israeli_channel_tables():
    assert wc.CHANNELS["2.4"] == tuple(range(1, 14))
    assert 144 not in wc.CHANNELS["5"] and 149 not in wc.CHANNELS["5"] and 140 in wc.CHANNELS["5"]
    assert wc.CHANNELS["6"][0] == 1 and wc.CHANNELS["6"][-1] == 93           # 5945-6425 MHz
    assert wc.block("5", 44, 80) == (36, 40, 44, 48)
    assert wc.block("5", 132, 80) is None and wc.widths("5", 140) == [20]    # 144 is not allowed
    assert wc.block("5", 120, 160) == tuple(range(100, 129, 4))
    assert wc.block("2.4", 11, 40) == (7, 11) and wc.block("2.4", 1, 40) == (1, 5)
    assert wc.fit_width("5", 140, 80) == 20 and wc.fit_width("5", 132, 80) == 40
    assert wc.centre_mhz("5", 36, 80) == pytest.approx(5210.0)
    assert wc.plan_channels("2.4", 20) == [1, 6, 11]
    assert wc.plan_channels("5", 80) == [36, 52, 100, 116]
    assert wc.plan_channels("6", 160) == [1, 33, 65]
    assert [wc.default_channel("2.4", 20, k) for k in range(4)] == [1, 6, 11, 1]
    assert wc.is_dfs("5", 52) and not wc.is_dfs("5", 36) and wc.is_dfs("5", 36, 160)
    assert wc.legal_eirp("5", 36) == 23.0 and wc.legal_eirp("5", 100, 80) == 27.0
    assert wc.legal_eirp("5", 36, 160) == 23.0 and wc.legal_eirp("2.4", 6) == 20.0 and wc.legal_eirp("6", 5) == 23.0
    assert wc.channel_title("5", 100, 40) == "100 · 40 MHz (100-104) · DFS"


def test_overlap_follows_the_spectrum_mask():
    f = lambda a, b: wc.overlap_db(wc.channel_mhz("2.4", a), 20, wc.channel_mhz("2.4", b), 20)  # noqa: E731
    assert f(6, 6) == pytest.approx(0.0, abs=0.05)
    assert -4.0 < f(1, 3) < -2.0                        # heavy overlap
    assert f(1, 6) < -25.0                              # the classic non-overlapping pair
    levels = [f(1, b) for b in range(1, 14)]
    assert all(a >= b for a, b in zip(levels, levels[1:]))
    assert f(1, 6) == pytest.approx(f(6, 1), abs=1e-6)  # same widths: symmetric
    # a 20 MHz radio inside an 80 MHz channel lands there whole; the 80 MHz radio spreads over four
    assert wc.overlap_db(5180, 20, 5210, 80) == pytest.approx(0.0, abs=0.1)
    assert wc.overlap_db(5210, 80, 5180, 20) == pytest.approx(-6.0, abs=0.5)
    assert wc.overlap_db(5210, 80, 5530, 80) == wc.NO_OVERLAP_DB


# ---------------------------------------------------------------------------- radios on access points
def test_radios_switch_and_promote():
    ap = ws.AccessPoint("AP", 1, 1)
    assert [r.band for r in ap.radios()] == ["5"]
    assert ap.set_radio("2.4", True, tx_dbm=18, channel=6)
    assert [(r.band, r.channel, r.tx_dbm) for r in ap.radios()] == [("5", 0, 20.0), ("2.4", 6, 18.0)]
    assert ap.set_radio("5", False)                     # the first radio off: 2.4 GHz becomes the first
    assert ap.band == "2.4" and ap.channel == 6 and [r.band for r in ap.radios()] == ["2.4"]
    assert [r.band for r in ap.radios(every=True)] == ["2.4", "5"]
    assert not ap.set_radio("2.4", False)               # the last radio stays on
    assert ap.set_radio("5", True) and {r.band for r in ap.radios()} == {"2.4", "5"}
    assert ap.radio("6") is None and ap.radio("5").enabled


def test_access_point_file_round_trip_and_old_files():
    old = {"name": "AP1", "x": 1.0, "y": 2.0, "z": 2.6, "tx_dbm": 20.0, "band": "5", "enabled": True,
           "from_lab": False}
    assert ws.AccessPoint.from_dict(old).to_dict() == old              # version 1 stays version 1
    s = ws.demo_site()
    assert not s.uses_v2()
    s.aps[0].set_radio("2.4", True, channel=11)
    s.aps[0].extra["vendor_note"] = "kept"
    s.add_ap("N", neighbour=True)
    assert s.uses_v2()
    d = s.to_dict()
    back = ws.Site.from_dict(d)
    assert back.to_dict() == d and back.aps[1].neighbour and back.aps[0].more[0].channel == 11
    assert back.aps[0].extra == {"vendor_note": "kept"}
    bad = ws.AccessPoint.from_dict({**old, "channel": "x", "width": True, "radios": [{"band": "7"}, 5]})
    assert bad.channel == 0 and bad.width == 0 and bad.more[0].band == "5"


def test_emitters_work_out_channels_and_problems_say_what_changed():
    s = ws.Site(20, 10, 3)
    for k in range(4):
        s.add_ap(x=2 + 4 * k, y=5, band="2.4")
    s.aps[3].channel = 14                               # Japan only: not in Israel
    s.add_ap("Next door", neighbour=True, x=19, y=9)
    ems = s.emitters("2.4")
    assert [e.channel for e in ems] == [1, 6, 11, 1]    # the planner's list in turn; 14 replaced
    assert all(not e.neighbour for e in ems)
    assert [e.name for e in s.emitters(own=False, neighbours=True)] == ["Next door"]
    assert s.emitters(own=False, neighbours=True)[0].channel == 36           # counted apart from yours
    s.aps[1].width = 160
    s.aps[1].tx_dbm = 30
    probs = s.problems()
    assert any("channel 14 is not allowed" in p for p in probs)
    assert any("160 MHz is not possible" in p for p in probs)
    assert any("30 dBm on channel 6 is above the legal limit of 20" in p for p in probs)
    s.aps[2].enabled = False
    assert len(s.emitters("2.4")) == 3 and len(s.emitters("2.4", every=True)) == 4
    s.aps[1].tx_dbm = 20
    v, who = s.best((2.0, 5.0, 1.0))
    assert who.name == "AP1" and who.band == "2.4"
    assert s.best((19.0, 9.0, 1.0), band="5") == (ws.NO_SIGNAL, None)          # neighbours never serve


# ---------------------------------------------------------------------------- engines agree with the reference
def _busy_site() -> ws.Site:
    s = ws.demo_site()
    s.aps[0].set_radio("2.4", True, channel=6)
    s.add_ap("AP2", x=22.0, y=5.0, channel=36)          # co-channel with AP1 on 5 GHz
    s.aps[1].set_radio("2.4", True, channel=3)          # partly overlaps channel 6
    s.add_ap("Next door", neighbour=True, x=28.0, y=16.0, channel=40, width=20)
    s.aps[2].set_radio("2.4", True, channel=6)
    return s


def test_band_filter_of_the_field_engine():
    s = _busy_site()
    eng = wf.FieldEngine(s, budget=3000)
    g = eng.grid
    for band in (None, "2.4", "5"):
        eng.band = band
        vol = eng.volume()
        for k, j, i in ((0, 0, 0), (g.nz - 1, g.ny // 2, g.nx // 3), (g.nz // 2, g.ny - 1, g.nx - 1)):
            want = s.best((float(g.xd[i]), float(g.yd[j]), float(g.zd[k])), band=band)[0]
            assert vol[k, j, i] == pytest.approx(want, abs=2e-3)
    assert {e.name for e, _v in eng.band_volumes("2.4")} == {"AP1", "AP2", "Next door"}


def test_numpy_maps_match_the_point_reference():
    s = _busy_site()
    eng = wf.FieldEngine(s, budget=4000)
    g = eng.grid
    rng = np.random.default_rng(3)
    walls = s.walls()
    for band in ("2.4", "5"):
        m = wi.Metrics(eng.band_volumes(band), g.shape)
        load = wi.channel_load(eng.band_volumes(band), band, wc.CHANNELS[band][2], 20, g.shape)
        shared = 0
        for _ in range(80):
            k, j, i = (int(rng.integers(n)) for n in (g.nz, g.ny, g.nx))
            pt = (float(g.xd[i]), float(g.yd[j]), float(g.zd[k]))
            r = wi.reading(s, pt, band, walls)
            assert m.signal[k, j, i] == pytest.approx(r.signal, abs=2e-3)
            assert m.sinr[k, j, i] == pytest.approx(r.sinr, abs=5e-3)
            assert m.sharing[k, j, i] == len(r.sharing)
            assert s.emitters(band, neighbours=True)[m.serving[k, j, i]] == r.serving or \
                eng.band_volumes(band)[m.serving[k, j, i]][0] == r.serving
            lvl = wi.channel_level(s, pt, band, wc.CHANNELS[band][2], 20, walls)
            assert load[k, j, i] == pytest.approx(lvl, abs=5e-3)
            shared += len(r.sharing)
        assert shared > 0                                # the site really has radios sharing a channel


def test_interference_lowers_sinr_and_speed():
    s = ws.Site(20, 10, 3)
    s.add_ap("A", x=3, y=5, channel=36)
    pt = (10.0, 5.0, 1.0)
    alone = wi.reading(s, pt, "5")
    assert alone.sinr == pytest.approx(alone.signal - wi.noise_dbm(80), abs=1e-6) and not alone.sharing
    s.add_ap("B", x=17, y=5, channel=36)
    both = wi.reading(s, pt, "5")
    assert both.sinr < alone.sinr - 10 and [e.name for e, _ in both.sharing] == ["B"]
    s.aps[1].channel = 100
    apart = wi.reading(s, pt, "5")
    assert apart.sinr == pytest.approx(alone.sinr, abs=0.1) and not apart.sharing
    a = s.emitters("5")[0]
    clean, dirty = wl.link(s, both.signal, a), wl.link(s, both.signal, a, both.interference)
    assert dirty.rate_hi < clean.rate_hi and dirty.snr_db < clean.snr_db and dirty.width == 80
    narrow = ws.Emitter("n", 3, 5, 2.6, 20, "5", 36, 20)
    assert wl.link(s, -60.0, narrow).rate_hi <= wl.RATE_2SS[20][-1]
    assert "channel 36 to itself" == alone.sharing_text()
    best = wi.best_reading(s, pt)
    assert best.band in ws.BANDS and best.serving is not None


def test_conflicts_and_auto_channels():
    s = ws.Site(24, 12, 3)
    for k in range(3):
        s.add_ap(x=4 + 8 * k, y=6, band="2.4", channel=6)
        s.aps[-1].set_radio("5", True, channel=36)
    s.add_ap("Next door", neighbour=True, x=23, y=11, band="5", channel=52)
    before = wi.channel_conflicts(s)
    assert len([c for c in before if c.a.band == "2.4"]) == 3 and "take turns" in before[0].text()
    changes = wi.auto_channels(s)
    assert changes
    assert sorted(e.channel for e in s.emitters("2.4")) == [1, 6, 11]
    five = [e.channel for e in s.emitters("5")]
    assert len(set(five)) == 3 and 52 not in five          # away from each other and from the neighbour
    assert not [c for c in wi.channel_conflicts(s) if not c.b.neighbour]
    assert wi.auto_channels(s) == []                       # a second run changes nothing


# ---------------------------------------------------------------------------- the maps' colour scales
def test_scales_map_onto_the_ramp():
    assert wm.SIGNAL.to_ramp(-70.0) == pytest.approx(-70.0)
    assert wm.SINR.to_ramp(5.0) == pytest.approx(wm.RAMP_LO) and wm.SINR.to_ramp(40.0) == pytest.approx(wm.RAMP_HI)
    assert wm.SHARING.to_ramp(0) > wm.SHARING.to_ramp(1) > wm.SHARING.to_ramp(3) > wm.RAMP_LO
    assert wm.SHARING.to_ramp(9) > wm.RAMP_LO                      # clipped: never see-through
    assert wm.CHANNEL.to_ramp(-95) == pytest.approx(wm.RAMP_HI) and wm.CHANNEL.to_ramp(-20) > wm.RAMP_LO
    assert wm.SINR.class_name(26) == "Very good" and wm.SINR.class_name(1) == "Unusable"
    assert wm.SHARING.class_name(0) == "Alone" and wm.SHARING.class_name(5) == "Crowded"
    assert wm.CHANNEL.class_name(-70) == "Busy"
    plane = np.array([[35.0, 22.0], [8.0, -200.0]], np.float32)
    sh = wm.SINR.shares(plane)
    assert sh == [25.0, 0.0, 25.0, 0.0, 25.0, 25.0] and sum(sh) == pytest.approx(100.0)
    assert wm.SHARING.shares(np.array([0, 1, np.nan, 7], np.float32)) == pytest.approx([100 / 3, 100 / 3, 0, 100 / 3])
    sig = np.array([[-45.0, -70.0], [-90.0, -55.0]], np.float32)
    assert wm.SIGNAL.shares(sig) == wf.band_shares(sig)
    for sc in wm.SCALES.values():
        assert len(sc.rows()) == len(sc.classes) + (1 if sc.none_row else 0)
        assert all(wm.RAMP_LO <= sc.swatch(i) <= wm.RAMP_HI for i in range(len(sc.classes)))


def test_map_volumes():
    s = _busy_site()
    eng = wf.FieldEngine(s, budget=3000)
    assert wm.map_volumes(eng, "signal", None) == (None, None)
    for kind in ("sinr", "sharing", "channel"):
        vals, ramp = wm.map_volumes(eng, kind, "5", ("5", 36))
        assert vals.shape == ramp.shape == eng.grid.shape and np.isfinite(ramp).all()
    vals, _ = wm.map_volumes(eng, "sinr", None)
    five, _ = wm.map_volumes(eng, "sinr", "5")
    assert (vals >= five - 1e-4).all()                             # every band: the best of them
    assert [k for k, _t in wm.channel_choices(s, "2.4")][:2] == [("2.4", 1), ("2.4", 2)]


# ---------------------------------------------------------------------------- the panel
@pytest.fixture
def app():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def test_panel_bands_maps_spectrum_and_radios(app):
    from labdesk_app.ui import site3d as s3
    s = _busy_site()
    win = s3.SiteWindow(s, gl=False)
    win.resize(1300, 860)
    win.show()
    app.processEvents()
    v = win.view
    assert win.c_chan.isEnabled() is False and "2.4 GHz: AP1 6, AP2 3" in win.c_plan.text()
    win.c_metric.setCurrentIndex(win.c_metric.findData("sinr"))
    assert v.map_kind == "sinr" and v.scale is wm.SINR and len(v.shares) == len(wm.SINR.rows())
    assert sum(v.shares) == pytest.approx(100.0) and v.heat_plane.shape == v.plane.shape
    win.c_band.setCurrentIndex(win.c_band.findData("2.4"))
    assert v.band == "2.4" and v.engine.band == "2.4"
    win.spectrum.channel_clicked.emit("2.4", 6)                    # click a channel in the analyser
    assert v.map_kind == "channel" and v.map_channel == ("2.4", 6) and win.c_chan.isEnabled()
    assert win.c_chan.currentData() == ("2.4", 6) and win.spectrum.selected == ("2.4", 6)
    assert set(win.spectrum.lines) == {"2.4"} and len(win.spectrum.lines["2.4"]) == 2     # at AP1: the others
    assert "At AP1" in win.c_probe.text()
    v._set_hover(v.rect().center())
    win.update_spectrum()
    assert "pointer" in win.c_probe.text() and len(win.spectrum.lines["2.4"]) == 3
    v.grab()                                                       # legend, hover and contours paint
    win.spectrum.grab()
    st = win.ui_state()
    assert st["band"] == "2.4" and st["map"] == "channel" and st["map_channel"] == ["2.4", 6]
    again = s3.SiteWindow(ws.Site.from_dict(s.to_dict()), gl=False, ui=st)
    assert (again.view.band, again.view.map_kind, again.view.map_channel) == ("2.4", "channel", ("2.4", 6))
    again.close()
    # the radio rows
    win.ap_list.setCurrentRow(0)
    chk6, ch6, wd6, pw6 = win.radio_rows["6"]
    assert not chk6.isChecked() and not ch6.isEnabled()
    chk6.setChecked(True)
    assert s.aps[0].radio("6") is not None and ch6.isEnabled() and ch6.currentText().startswith("Auto")
    ch6.setCurrentIndex(ch6.findData(33))
    wd6.setCurrentIndex(wd6.findData(160))
    pw6.setValue(17)
    r6 = s.aps[0].radio("6")
    assert (r6.channel, r6.width, r6.tx_dbm) == (33, 160, 17.0)
    assert "6 GHz ch 33" in win.ap_list.item(0).text()
    for b in ("5", "2.4"):
        win.radio_rows[b][0].setChecked(False)
    assert [r.band for r in s.aps[0].radios()] == ["6"]
    win.radio_rows["6"][0].setChecked(False)                       # refused: the last radio stays on
    assert win.radio_rows["6"][0].isChecked() and [r.band for r in s.aps[0].radios()] == ["6"]
    n = len(s.aps)
    win._add_neighbour()
    assert len(s.aps) == n + 1 and s.aps[-1].neighbour and s.aps[-1].name == "Neighbour 1"
    assert win.a_neigh.isChecked() and "neighbours" in win.ap_list.item(n).text()
    win._add_ap()
    assert {r.band for r in s.aps[-1].radios()} == {"2.4", "5"}
    changes = win.auto_channels()
    assert win.c_plan.text().startswith("Auto channels") and isinstance(changes, list)
    win.close()


def test_field_tables_take_one_band():
    from labdesk_app.ui import site3d_gl as gl
    s = _busy_site()
    assert gl.field_tables(s)[1] == 4                             # AP1 and AP2 on two bands; no neighbour
    tex, n_ap, _w, _o = gl.field_tables(s, "2.4")
    assert n_ap == 2 and all(tex[2 * k + 1][2] == 0.0 for k in range(n_ap))      # band index 0
    assert math.isclose(tex[0][3], s.aps[0].radio("2.4").tx_dbm)
