"""Design-system guards (S1 contrast validator, S2 glyphs, S4 type, S7 ring/label geometry, S8 tiers)."""
from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from labdesk_app.ui import theme as T  # noqa: E402

UI = Path(__file__).resolve().parent.parent / "labdesk_app" / "ui"


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


# ------------------------------------------------------------------ S1 / S2 contrast
@pytest.mark.parametrize("dark", [False, True])
@pytest.mark.parametrize("state", ["up", "down", "proto", "warn"])
def test_status_text_tokens_reach_4_5_on_n1_to_n3(dark, state):
    tk = T.build_tokens(dark)
    for i in (1, 2, 3):
        c = T.contrast(T.status_text(state, tk), tk.N(i))
        assert c >= 4.5, f"{state} text on N{i} ({'dark' if dark else 'light'}): {c:.2f}"


@pytest.mark.parametrize("dark", [False, True])
def test_neutral_text_and_accent_ring(dark):
    tk = T.build_tokens(dark)
    for i in (1, 2, 3):
        assert T.contrast(tk.N(11), tk.N(i)) >= 4.5
        assert T.contrast(tk.N(12), tk.N(i)) >= 7.0
        assert T.contrast(tk.accent_ring, tk.N(i)) >= 3.0          # non-text (WCAG 1.4.11)


def test_amber_is_never_text_on_light():
    tk = T.build_tokens(False)
    assert T.contrast(T.status_fill("warn", tk), tk.N(2)) < 3.0       # why it needs the WARN.text outline
    assert T.text_for(T.AMBER, tk).name().upper() == T.STATUS["warn"][2]
    assert T.SEVERITY_HUES["warn"] in (T.STATUS["warn"][2], T.STATUS["warn"][3])


# ------------------------------------------------------------------ S2 glyphs
LINK_ENDS = ("up", "down", "proto_down", "admin_down")


def _signature(path):
    br = path.boundingRect()
    # filled area by sampling (fill rule respected)
    from PySide6.QtCore import QPointF
    n, inside = 40, 0
    for i in range(n):
        for j in range(n):
            if path.contains(QPointF(br.left() + (i + 0.5) * br.width() / n, br.top() + (j + 0.5) * br.height() / n)):
                inside += 1
    return round(br.width() / br.height(), 2), round(inside / (n * n), 2)


def test_one_shape_per_state_and_link_ends_are_not_congruent(app):
    sigs = {s: _signature(T.status_glyph(s, 20.0)) for s in LINK_ENDS}
    vals = list(sigs.values())
    for i in range(len(vals)):
        for j in range(i + 1, len(vals)):
            a, b = vals[i], vals[j]
            assert abs(a[0] - b[0]) > 0.15 or abs(a[1] - b[1]) > 0.08, (LINK_ENDS[i], LINK_ENDS[j], a, b)
    # pinned silhouettes
    assert sigs["up"][0] == 1.0 and 0.7 < sigs["up"][1] < 0.85            # filled circle
    assert sigs["down"][0] < 0.5                                           # a single bar
    assert sigs["proto_down"][1] < 0.6                                     # hollow ring
    assert sigs["admin_down"][1] > 0.9                                     # filled square
    for s in ("booted", "disconnected", "booting", "warn", "info", "blocking"):
        assert not T.status_glyph(s, 12.0).isEmpty()


def test_link_end_glyph_map_never_uses_x():
    assert set(T.LINK_END_GLYPH.values()) - {None} <= {"up", "down", "proto_down", "admin_down", "neutral"}


# ------------------------------------------------------------------ S4 typography
def test_no_literal_point_sizes_or_monospace_family_outside_theme():
    bad = []
    for f in UI.glob("*.py"):
        if f.name == "theme.py":
            continue
        for n, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            if re.search(r"setPointSizeF?\(", line) or re.search(r"QFont\(\s*['\"]monospace", line):
                bad.append(f"{f.name}:{n}: {line.strip()}")
    assert not bad, "\n".join(bad)


def test_type_roles_follow_base_size(app):
    T.pin_fonts("Noto Sans", "Noto Sans Mono", 10.0)
    try:
        assert abs(T.type("caption").pointSizeF() - 7.5) < 0.01
        assert abs(T.type("title").pointSizeF() - 13.0) < 0.01
        assert T.type("micro").pointSizeF() >= 7.0
        assert T.type("caption") is T.type("caption")                 # cached, not rebuilt per paint
        assert T.mono().fixedPitch() or T.mono().styleHint() == T.mono().StyleHint.Monospace
    finally:
        T._PINNED.clear()
        T._FONT_CACHE.clear()


# ------------------------------------------------------------------ S7 ring / label geometry
@pytest.mark.parametrize("placement", ["below", "left", "right"])
def test_selection_ring_never_touches_the_name(app, placement):
    from labdesk_app.catalog import BY_KEY
    from labdesk_app.ui.graphics import LABEL_GAP, NodeItem, ring_rect
    assert LABEL_GAP == 8.0
    n = NodeItem("k", "R1-long-name", BY_KEY["iosv"])
    n.label_side = lambda: placement
    for lod in (0.5, 0.8, 1.0, 1.6):
        ring = ring_rect(n.kind, n.SIZE)
        lab = n.name_rect(lod)
        assert not ring.intersects(lab), (placement, lod, ring, lab)


def test_lod_tiers_are_three_constants():
    assert (T.LOD_TIER1, T.LOD_TIER2, T.LOD_GRID) == (0.6, 0.9, 0.5)
    src = (UI / "graphics.py").read_text(encoding="utf-8")
    assert not re.search(r"lod\s*[<>]=?\s*0\.(45|55|8)\b", src)


def test_composite_dimming_floor():
    from labdesk_app.ui.graphics import dim_factor
    assert dim_factor(0.45, 0.28) == 0.40
    assert dim_factor(0.45) == 0.45
    assert dim_factor() == 1.0


# ------------------------------------------------------------------ S6 icons / theme controller
def test_icons_recolour_and_cache(app):
    from labdesk_app.ui import icons
    for name in ("document-open", "media-playback-start", "utilities-terminal", "network-disconnect",
                 "view-list-details", "activity", "edit-delete"):
        assert icons.available(name), name
    pm = icons.render("document-open", 16, 2.0)
    assert not pm.isNull() and pm.width() == 32
    assert icons.render("document-open", 16, 2.0) is pm
    svg = icons.recolour((icons.ICON_DIR / "document-open-16.svg").read_text(encoding="utf-8"), T.build_tokens(True).N(12))
    assert T.N_DARK[11].lower() in svg.lower()


def test_theme_controller_modes(app, tmp_path):
    from PySide6.QtCore import QSettings
    from labdesk_app.ui import themectl
    st = QSettings(str(tmp_path / "t.ini"), QSettings.IniFormat)
    ctl = themectl.ThemeController(app, st, "dark")
    assert T.tokens().dark and app.palette().window().color().name().upper() == T.N_DARK[1]
    ctl.set_mode("light")
    assert not T.tokens().dark and st.value("ui/theme") == "light"
    assert "QTabBar::tab:selected" in app.styleSheet()
    ctl.set_mode("kde")
    assert "QTabBar::tab" not in app.styleSheet()          # native Fusion tabs in KDE colours mode
    ctl.set_mode("light")


def test_tabular_figures_align_digits(app):
    """S4: counters use tabular figures ('tnum' on the sans font, or mono when the family lacks it)."""
    from PySide6.QtGui import QFontMetricsF
    T.pin_fonts("Noto Sans", "Noto Sans Mono", 10.0)
    try:
        fm = QFontMetricsF(T.tabular("small"))
        widths = {fm.horizontalAdvance(d * 3) for d in "0123456789"}
        assert max(widths) - min(widths) < 0.5, widths
    finally:
        T._PINNED.clear()
        T._FONT_CACHE.clear()
