"""The LabDesk console window: palette, IOS lens, key map, terminal model (pure - Linux and Windows), the
pixel grid, the tab strip, and - Linux only - the pty session, the window and the launcher."""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

# never on the desktop: also when this file runs alone, no window is shown and the user's clipboard is
# not touched (the offscreen platform has a clipboard of its own)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from labdesk_app import launchers  # noqa: E402
from labdesk_app.ui import console
from labdesk_app.ui.console import boxdraw, keymap, lens
from labdesk_app.ui.console import palette as P
from labdesk_app.ui.console.screen import BOLD, FAINT, REVERSE, UNDERLINE, Terminal, plain_text

UUID = "0f1e2d3c-4b5a-4978-8695-a4b3c2d1e0f9"


# ============================================================================ C4 palette
@pytest.mark.parametrize("name", ["phosphor", "soft", "plain"])
def test_every_console_text_colour_reads_at_4_5_to_1(name):
    pal = P.PALETTES[name]
    low = []
    for what, colour in pal.text_colours().items():
        if P.contrast(colour, pal.well) < 4.5:
            low.append(f"{what} {colour} on the well: {P.contrast(colour, pal.well):.2f}")
        f = pal.faint(colour)
        if P.contrast(f, pal.well) < 4.5:
            low.append(f"faint {what} {f}: {P.contrast(f, pal.well):.2f}")
    for what, (fg, bg) in pal.pairs().items():
        if P.contrast(fg, bg) < 4.5:
            low.append(f"{what} {fg} on {bg}: {P.contrast(fg, bg):.2f}")
    assert not low, "\n".join(low)


def test_phosphor_palette_values_and_no_indigo():
    p = P.PHOSPHOR
    assert (p.well, p.chrome, p.body, p.dim, p.bright) == ("#050A07", "#0A110C", "#39FF14", "#2BA516", "#D6FFCC")
    assert (p.red, p.amber) == ("#FF6369", "#FFCA16")              # LabDesk's own status tokens
    assert round(P.contrast(p.body, p.well), 2) == 14.71 and round(P.contrast(p.dim, p.well), 2) == 6.17
    assert p.ansi[2] == p.body and p.ansi[5] == "#FF5CD6"          # ANSI green = body; a real magenta
    assert p.fg("black") == "#4E8A52" and p.fg("default") == p.body and p.fg("ff8800") == "#FF8800"
    assert p.bg("default") is None and p.bg("red") == p.red
    for pal in (P.PHOSPHOR, P.SOFT_PHOSPHOR):                       # nothing indigo in the green console
        every = {v.upper() for v in vars(pal).values() if isinstance(v, str)} | {c.upper() for c in pal.ansi}
        assert not every & {"#5B5BD6", "#8481EA", "#B1A9FF", "#25264F"}
    assert P.faint(p.body, p.well) == "#22910E"                    # t = 0.45
    assert P.contrast(P.faint(p.red, p.well), p.well) >= 4.5       # t reduced for red (x0.6 would give 2.66)


def test_palette_switches():
    assert P.pick(True, False) is P.PHOSPHOR and P.pick(True, True) is P.SOFT_PHOSPHOR
    assert P.pick(False, False) is P.PLAIN and P.pick(False, True) is P.PLAIN
    assert P.SOFT_PHOSPHOR.body == "#7DF56A" and P.SOFT_PHOSPHOR.ansi[2] == "#7DF56A"
    assert P.SOFT_PHOSPHOR.well == P.PHOSPHOR.well and P.SOFT_PHOSPHOR.red == P.PHOSPHOR.red
    assert (P.PLAIN.well, P.PLAIN.body, P.PLAIN.cursor) == ("#111113", "#EDEEF0", "#8481EA")


# ============================================================================ C8 lens
HOSTS = {"R1", "HUB", "dist-sw1.lab"}

MUST_MATCH = [
    ("R1#", [(0, 2, lens.PROMPT), (2, 1, lens.PROMPT)]),
    ("R1>enable", [(0, 2, lens.PROMPT), (2, 1, lens.PROMPT), (3, 6, lens.COMMAND)]),
    ("R1#show ip interface brief", [(0, 2, lens.PROMPT), (2, 1, lens.PROMPT), (3, 23, lens.COMMAND)]),
    ("R1(config)#interface Gi0/1", [(0, 2, lens.PROMPT), (2, 8, lens.CONFIG), (10, 1, lens.PROMPT),
                                    (11, 15, lens.COMMAND)]),
    ("HUB(config-if)#", [(0, 3, lens.PROMPT), (3, 11, lens.CONFIG), (14, 1, lens.PROMPT)]),
    ("dist-sw1.lab#", [(0, 12, lens.PROMPT), (12, 1, lens.PROMPT)]),
    ("% Invalid input detected at '^' marker.", [(0, 39, lens.REFUSAL)]),
    ("% Incomplete command.", [(0, 21, lens.REFUSAL)]),
    ("% Ambiguous command:  \"sh i\"", [(0, 28, lens.REFUSAL)]),
    ("% Unknown command or computer name, or unable to find computer address", [(0, 70, lens.REFUSAL)]),
    ("*Nov 18 17:02:41.118: %LINK-3-UPDOWN: Interface Gi0/3, changed state to down",
     [(0, 22, lens.TIMESTAMP), (22, 14, lens.SEV_MID)]),
    ("*Mar  1 00:00:05.123: %SYS-2-MALLOCFAIL: Memory allocation failed", [(0, 22, lens.TIMESTAMP),
                                                                             (22, 17, lens.SEV_HIGH)]),
    ("%OSPF-4-ERRRCV: Received invalid packet", [(0, 14, lens.SEV_MID)]),
    ("*Nov 18 17:04:11.482: %SYS-5-CONFIG_I: Configured from console", [(0, 22, lens.TIMESTAMP)]),
    (" --More-- ", [(1, 8, lens.MORE)]),
]


@pytest.mark.parametrize("text,spans", MUST_MATCH)
def test_lens_must_match(text, spans):
    assert lens.classify(text, HOSTS) == spans


MUST_NOT_MATCH = [
    "admin@vMX#", "admin@vMX# show version", "root@alpine:/#", "user@host>", "banner motd #",
    " description uplink to R2 #", "description core#", "*> 10.0.0.0/8        10.0.12.2     0   0 65000 i",
    "% Generating 2048 bit RSA keys, keys will be non-exportable...", "%SYS-5-CONFIG_I: Configured from console",
    "%LINEPROTO-5-UPDOWN: Line protocol on Interface Gi0/3, changed state to down",
    "R9#show version",                       # a hostname this session never saw as a prompt
    "Interface              IP-Address      OK? Method Status", "GigabitEthernet0/1 is up, line protocol is up",
    "  10.0.12.0/30 is directly connected, GigabitEthernet0/0", "% 10.0.0.0 overlaps with GigabitEthernet0/0",
    "!", "", "end", "          ^", "Password: ", "--More-- or (q)uit",
]


@pytest.mark.parametrize("text", MUST_NOT_MATCH)
def test_lens_must_not_match(text):
    assert lens.classify(text, HOSTS) == []


def test_lens_caret_row_only_above_invalid_input():
    assert lens.classify("          ^", HOSTS, "% Invalid input detected at '^' marker.") == [(0, 11, lens.REFUSAL)]
    assert lens.classify("          ^", HOSTS, "% Incomplete command.") == []
    assert lens.classify("   ^ ^", HOSTS, "% Invalid input detected at '^' marker.") == []


def test_lens_learns_hostnames_only_from_the_cursor_row_prompt():
    known: set = set()
    assert not lens.learn("R1#show run", 11, known)                  # the cursor is not right behind a prompt
    assert not lens.learn("banner motd #", 13, known) and not lens.learn("admin@vMX#", 10, known)
    assert not lens.learn("Building configuration...", 25, known) and not known
    assert lens.learn("R1#", 3, known) and known == {"R1"}
    assert lens.learn("R1(config-if)#", 14, known) is False and known == {"R1"}       # already known
    assert lens.learn("HUB>", 4, known) and known == {"R1", "HUB"}
    assert lens.classify("R1#show run", known) and not lens.classify("R1#show run", set())


def test_lens_runs_only_on_ios_family_consoles():
    assert lens.applies("iosv") and lens.applies("iosvl2") and lens.applies("iol-xe") and lens.applies("ioll2-xe")
    assert not lens.applies("alpine") and not lens.applies("") and not lens.applies(None)


def test_copied_text_of_a_lensed_transcript_equals_the_raw_text():
    raw = ["R1#show ip ospf neigbor", "                 ^", "% Invalid input detected at '^' marker.", "",
           "*Nov 18 17:02:41.118: %LINK-3-UPDOWN: Interface Gi0/3, changed state to down",
           "R1(config)#interface Gi0/3", " --More-- ", "R1#"]
    t = Terminal(110, 20)
    t.feed("\r\n".join(raw).encode())
    scr = t.screen
    scr.sync()
    known = {"R1"}
    assert any(lens.classify(scr.line(i)[0], known, scr.line(i + 1)[0]) for i in range(len(raw)))  # the lens is busy
    first = scr.first_id + len(scr.history)
    copied = scr.text_between((first, 0), (first + len(raw) - 1, len(raw[-1])))
    assert copied == "\n".join(line.rstrip() for line in raw)


# ============================================================================ key map
def test_keys_send_what_a_real_terminal_sends():
    K = keymap
    assert K.encode(K.K_TAB) == b"\t" and K.encode(0x3F, K.SHIFT, "?") == b"?"
    assert K.encode(0x43, K.CTRL) == b"\x03"                          # Ctrl-C
    assert K.encode(K.K_BRACKET_R, K.CTRL) == b"\x1d"                 # Ctrl-]: leaves a CML console
    assert K.encode(K.K_CIRCUM, K.CTRL | K.SHIFT, "") == b"\x1e"      # Ctrl-Shift-6: the IOS escape
    assert K.encode(K.K_6, K.CTRL | K.SHIFT, "") == b"\x1e"
    assert K.encode(0x5A, K.CTRL) == b"\x1a" and K.encode(0x41, K.CTRL) == b"\x01"     # Ctrl-Z, Ctrl-A
    assert K.encode(K.K_RETURN) == b"\r" and K.encode(K.K_ENTER) == b"\r"
    assert K.encode(K.K_BACKSPACE) == b"\x7f" and K.encode(K.K_ESCAPE) == b"\x1b"
    assert K.encode(K.K_UP) == b"\x1b[A" and K.encode(K.K_DOWN, app_cursor=True) == b"\x1bOB"
    assert K.encode(K.K_LEFT) == b"\x1b[D" and K.encode(K.K_RIGHT) == b"\x1b[C"
    assert K.encode(K.K_PGUP) == b"\x1b[5~" and K.encode(K.K_DELETE) == b"\x1b[3~"
    assert K.encode(0x61, 0, "a") == b"a" and K.encode(0x41, K.SHIFT, "A") == b"A"
    assert K.encode(0x42, K.ALT, "b") == b"\x1bb" and K.encode(0xE9, 0, "é") == "é".encode()
    assert K.encode(0x01000020, K.SHIFT, "") == b""                   # a bare Shift sends nothing


# ============================================================================ C2 terminal model
def _lines(scr, n):
    scr.sync()
    end = scr.first_id + scr.total_lines
    return [scr.line(i) for i in range(end - n, end)]


def test_scrollback_holds_compact_lines_with_ids_that_survive_eviction():
    t = Terminal(80, 5, scrollback=10)
    for i in range(40):
        t.feed(f"line {i}\r\n".encode())
    scr = t.screen
    scr.sync()
    assert len(scr.history) == 10 and scr.first_id == 26             # 36 lines left the top, 26 were evicted
    assert all(isinstance(h, tuple) and isinstance(h[0], str) for h in scr.history)   # never pyte cell rows
    assert scr.line(scr.first_id) == ("line 26", (), False)
    assert scr.line(35) == ("line 35", (), False) and scr.line(3) == ("", (), False)   # evicted -> empty
    assert scr.total_lines == 15 and scr.cursor_line_id() == 40
    assert Terminal(80, 5).screen.max_history == 50_000              # the default length


def test_attribute_runs_faint_reverse_underline_and_colours():
    t = Terminal(80, 5)
    t.feed(b"a\x1b[1mB\x1b[0m \x1b[31mred\x1b[0m \x1b[2mdim\x1b[22m \x1b[7mrev\x1b[27m \x1b[4mund\x1b[0m"
           b" \x1b[38;5;196mx\x1b[38;2;1;2;3my\x1b[0m \x1b[5mblink\x1b[0m")
    text, runs, wrapped = _lines(t.screen, 5)[0]
    assert text == "aB red dim rev und xy blink" and not wrapped
    assert runs == ((1, 1, "default", "default", BOLD), (3, 3, "red", "default", 0),
                    (7, 3, "default", "default", FAINT), (11, 3, "default", "default", REVERSE),
                    (15, 3, "default", "default", UNDERLINE), (19, 1, "ff0000", "default", 0),
                    (20, 1, "010203", "default", 0))                 # blink is ignored


def test_wrap_marker_joins_wrapped_lines_in_a_copy():
    t = Terminal(80, 6)
    t.feed(b"A" * 100 + b"\r\nnext")
    scr = t.screen
    scr.sync()
    assert [scr.screen_line(y)[2] for y in range(3)] == [True, False, False]
    assert scr.text_between((scr.first_id, 0), (scr.first_id + 2, 4)) == "A" * 100 + "\nnext"


def test_alternate_screen_swaps_buffers_and_never_reaches_the_scrollback():
    t = Terminal(80, 5)
    t.feed(b"one\r\ntwo\r\nR1#")
    scr = t.screen
    for mode in (b"1049", b"1047", b"47"):
        before = len(scr.history)
        t.feed(b"\x1b[?" + mode + b"h\x1b[H")
        assert scr.alt
        for i in range(30):
            t.feed(f"alt {i}\r\n".encode())
        assert len(scr.history) == before                            # nothing captured
        t.feed(b"\x1b[?" + mode + b"l")
        scr.sync()
        assert not scr.alt and [scr.screen_line(y)[0] for y in range(3)] == ["one", "two", "R1#"]
    t.feed(b"\x1b[3;4H\x1b[?1049h\x1b[1;1Hfull screen\x1b[?1049l")
    assert (scr.cursor.x, scr.cursor.y) == (3, 2)                    # 1049 restores the cursor


def test_modes_cursor_keys_cursor_visibility_and_replies():
    t = Terminal(80, 5)
    scr = t.screen
    assert not scr.app_cursor_keys and scr.cursor_visible
    t.feed(b"\x1b[?1h\x1b[?25l")
    assert scr.app_cursor_keys and not scr.cursor_visible
    t.feed(b"\x1b[?1l\x1b[?25h")
    assert not scr.app_cursor_keys and scr.cursor_visible
    got = []
    scr.on_reply = got.append
    t.feed(b"ab\x1b[6n\x1b[5n\x1b[c")
    assert got == ["\x1b[1;3R", "\x1b[0n", "\x1b[?6c"]              # DSR / DA answers reach the child
    t.feed(b"\x07\x07")
    assert scr.bells == 2
    t.feed(b"\x1b[?3h")                                              # DECCOLM must not resize the model
    assert scr.columns == 80


def test_resize_keeps_the_text_rows_follow_columns_only_grow():
    t = Terminal(80, 10)
    for i in range(8):
        t.feed(f"row {i}\r\n".encode())
    t.feed(b"R1#")
    scr = t.screen
    assert scr.cursor.y == 8 and not scr.history
    scr.resize(5, 100)                                               # shrink: the top 4 rows go to scrollback
    scr.sync()
    assert (scr.lines, scr.columns, scr.cursor.y) == (5, 100, 4)
    assert [h[0] for h in scr.history] == ["row 0", "row 1", "row 2", "row 3"]
    assert [scr.screen_line(y)[0] for y in range(5)] == ["row 4", "row 5", "row 6", "row 7", "R1#"]
    scr.resize(5, 60)                                                # narrower view: the model keeps 100
    assert scr.columns == 100
    scr.resize(12, 90)
    scr.sync()
    assert (scr.lines, scr.columns) == (12, 100) and scr.screen_line(4)[0] == "R1#"
    t.feed(b"x" * 97)                                                # 100 columns really exist
    scr.sync()
    assert scr.screen_line(4)[0] == "R1#" + "x" * 97 and not scr.screen_line(4)[2]


def test_a_sequence_pyte_cannot_parse_never_ends_the_session():
    t = Terminal(80, 5)
    t.feed(b"ok\x1b[?1;2m\x1b[>4;2m\x1b]0;title\x07\x1b[999;999H\x1b[1;1Hstill")
    t.screen.sync()
    assert plain_text(t.screen.screen_line(0)).startswith("still")
    t.feed(b"\xff\xfe\x00 fine")                                     # bad UTF-8
    t.feed("wide 中文 ok".encode())
    t.screen.sync()
    assert "fine" in t.screen.screen_line(0)[0] and "中文" in plain_text(t.screen.screen_line(0))


def test_feed_speed_is_enough_for_a_long_show_command():
    t = Terminal(110, 42)
    data = ("GigabitEthernet0/1   10.0.12.1   YES NVRAM  up    up\r\n" * 2000).encode()
    t0 = time.perf_counter()
    t.feed(data)
    assert len(t.screen.history) == 2000 - 41 and time.perf_counter() - t0 < 5.0


# ============================================================================ C5 grid (pure parts)
def test_default_rows_and_window_size():
    assert console.default_rows(1080) == 42 and console.default_rows(1044) == 40 and console.default_rows(800) == 30
    assert console.default_rows(300) == 12 and console.default_rows(4000) == 42
    assert console.window_size(110, 42, (9, 21)) == (1022, 970)      # 16+990+16 wide; 38+12+882+12+26 high
    assert console.window_size(110, 40, (9, 21)) == (1022, 928)
    assert console.window_size(80, 12, (9, 21)) == (752, 340)        # the minimum
    assert (console.TAB_STRIP_H, console.STATUS_H, console.PAD_X, console.PAD_Y) == (38, 26, 16, 12)


def test_box_drawing_arms_come_from_the_unicode_names():
    assert boxdraw.arms("─") == {"l": 1, "r": 1} and boxdraw.arms("┃") == {"u": 2, "d": 2}
    assert boxdraw.arms("┌") == {"d": 1, "r": 1} and boxdraw.arms("╔") == {"d": 3, "r": 3}
    assert boxdraw.arms("┼") == {"u": 1, "d": 1, "l": 1, "r": 1}
    assert boxdraw.arms("┄") == {"l": 1, "r": 1}                 # dashed is drawn solid
    assert boxdraw.arms("┝") == {"u": 1, "d": 1, "r": 2} and boxdraw.arms("╭") == {"d": 1, "r": 1}
    assert boxdraw.arms("╱") is None and boxdraw.rects("╳", 9, 21) is None   # diagonals: the glyph
    for size in ((7, 15), (9, 21), (16, 38)):
        w, h = size
        v = boxdraw.rects("│", w, h)
        assert len(v) == 2 and all(r[0] == v[0][0] for r in v)       # one column of pixels ...
        assert min(r[1] for r in v) == 0 and max(r[1] + r[3] for r in v) == h          # ... from top to bottom
        hz = boxdraw.rects("─", w, h)
        assert min(r[0] for r in hz) == 0 and max(r[0] + r[2] for r in hz) == w
        assert boxdraw.rects("█", w, h) == [(0, 0, w, h, 1.0)]  # full block = the whole cell
        assert all(boxdraw.rects(chr(o), w, h) for o in range(0x2580, 0x25A0))


# ============================================================================ Qt parts
@pytest.fixture(scope="module")
def app():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def _pump(app, secs=0.2):
    from PySide6.QtCore import QEventLoop
    end = time.time() + secs
    while time.time() < end:
        app.processEvents(QEventLoop.AllEvents, 20)


def _until(app, cond, timeout=10.0):
    end = time.time() + timeout
    while time.time() < end:
        _pump(app, 0.03)
        if cond():
            return True
    return False


def test_console_font_is_bundled_and_set_in_one_place(app):
    from labdesk_app.ui import theme as T
    assert all((T.CONSOLE_FONT_DIR / f).exists() for f in T.CONSOLE_FONT_FILES)
    assert (T.CONSOLE_FONT_DIR / "LICENSE-Hack.txt").exists()
    if sys.platform != "win32":                                      # the console window is Linux only
        assert T.console_font_registered()
    f, b = T.console_font(11), T.console_font(11, True)
    assert f.families()[0] == "Hack" and "DejaVu Sans Mono" in f.families()
    assert f.pointSizeF() == 11 and not f.kerning() and b.bold() and not f.bold()
    assert f.hintingPreference() == f.HintingPreference.PreferFullHinting


@pytest.mark.linux_only
def test_cell_is_9_by_21_at_11_pt_and_bold_keeps_the_grid(app):
    from PySide6.QtGui import QGuiApplication
    from labdesk_app.ui.console import view as V
    if round(QGuiApplication.primaryScreen().logicalDotsPerInchY()) != 96:
        pytest.skip("needs 96 dpi")
    assert V.cell_metrics(11, 1.0) == (9, 21)
    assert console.window_size(110, 42, V.cell_metrics(11, 1.0)) == (1022, 970)
    m = V.metrics(11, 1.0)
    assert m.baseline == 15                                          # 1 + 14 px below the row top
    for pt in range(8, 21):
        m = V.metrics(pt, 1.0)
        adv = [raw.advancesForGlyphIndexes(raw.glyphIndexesForString("M"))[0].x() for raw in (m.regular, m.bold)]
        assert adv[0] == adv[1] == m.cw, pt                          # whole-pixel advance, same for Bold
        assert round(m.regular.ascent()) + round(m.regular.descent()) <= m.ch, pt     # the glyph box fits


@pytest.mark.linux_only
def test_every_glyph_sits_on_its_cell_and_box_lines_join(app):
    from labdesk_app.ui.console import view as V
    for pt in (9, 11, 14):
        assert V.grid_check(pt)                                      # glyph k of a 110-run at k x cell_w
        m = V.metrics(pt, 1.0)
        lp = V.LinePainter(P.PHOSPHOR, m)
        pm = lp.render(("H" * 110, (), False))
        assert (pm.width(), pm.height()) == (110 * m.cw, m.ch)
        bold = lp.render(("H" * 110, ((0, 110, "default", "default", BOLD),), False))
        assert len(set(V.ink_offsets(bold, m.cw, 110, P.PHOSPHOR.well))) == 1
        # U+2502 on consecutive rows: one continuous column of pixels, top to bottom of every cell
        img = lp.render((" │ │", (), False)).toImage()
        body = img.pixelColor(m.cw + (m.cw - max(1, round(m.cw / 8))) // 2, 0).name().upper()
        assert body == P.PHOSPHOR.body
        xs = [x for x in range(m.cw, 2 * m.cw) if img.pixelColor(x, 0).name().upper() == P.PHOSPHOR.body]
        assert xs and all(img.pixelColor(x, y).name().upper() == P.PHOSPHOR.body
                          for x in xs for y in range(m.ch))


@pytest.mark.linux_only
def test_lens_colours_only_default_foreground_cells(app):
    from labdesk_app.ui.console import view as V
    lp = V.LinePainter(P.PHOSPHOR, V.metrics(11, 1.0))
    spans = tuple(lens.classify("R1(config)#end", {"R1"}))
    fg, bg, bold, _d, _p = lp.cells(("R1(config)#end", (), False), spans)
    assert fg[0] == P.PHOSPHOR.bright and bold[0] and fg[2] == P.PHOSPHOR.amber and bold[2]
    assert fg[10] == P.PHOSPHOR.bright and bold[10] and fg[11] == P.PHOSPHOR.bright and not bold[11]
    fg, bg, _b, _d, _p = lp.cells(("xy", ((0, 1, "black", "default", 0), (1, 1, "black", "green", 0)), False))
    assert fg == [P.PHOSPHOR.black_fg, P.PHOSPHOR.ansi[0]] and bg == [None, P.PHOSPHOR.body]
    fg, bg, _b, _d, _p = lp.cells(("ab cd", ((0, 2, "red", "default", REVERSE), (3, 2, "default", "default", FAINT)),
                                   False), (), (1, 4))
    assert (fg[0], bg[0]) == (P.PHOSPHOR.well, P.PHOSPHOR.red)       # reverse video
    assert (fg[1], bg[1]) == (P.PHOSPHOR.sel_fg, P.PHOSPHOR.sel_bg) == (fg[3], bg[3])     # the selection
    assert fg[4] == P.PHOSPHOR.faint(P.PHOSPHOR.body) and bg[4] is None


class _Model:
    """What a TerminalView needs of a session, without a pty: a screen to feed and a size to follow."""

    def __new__(cls, columns=110, lines=42):
        from PySide6.QtCore import QObject, Signal

        class FakeSession(QObject):
            updated = Signal()
            alive, slices_pending = True, False

            def __init__(self):
                super().__init__()
                self.term = Terminal(columns, lines)
                self.screen = self.term.screen
                self.sent = []

            def write(self, data):
                self.sent.append(data)

            def resize(self, cols, rows):
                self.screen.resize(rows, cols)
                self.updated.emit()

            def feed(self, data):
                self.term.feed(data)
                self.updated.emit()

        return FakeSession()


@pytest.mark.linux_only
def test_a_more_pill_is_round_at_both_ends(app):
    """The '--More--' pill reaches 5 px past its cells: the line image has room for both round ends, also
    for a pill that starts in column 0."""
    from PySide6.QtGui import QColor
    from labdesk_app.ui.console import view as V
    m = V.metrics(11, 1.0)
    lp = V.LinePainter(P.PHOSPHOR, m)
    muted, well = QColor(P.PHOSPHOR.muted).rgb(), QColor(P.PHOSPHOR.well).rgb()
    assert V.LinePainter.bleed(()) == 0 and V.LinePainter.bleed(((0, 2, lens.PROMPT),)) == 0
    for text in (" --More--", "--More--"):
        spans = tuple(lens.classify(text, set()))
        start, length = next((a, n) for a, n, cls in spans if cls == lens.MORE)
        assert V.LinePainter.bleed(spans) == V.PILL_BLEED
        img = lp.render((text, (), False), spans).toImage()
        assert img.width() == len(text) * m.cw + 2 * V.PILL_BLEED and img.height() == m.ch
        mid = m.ch // 2
        xs = [x for x in range(img.width()) if img.pixel(x, mid) == muted]
        left, right = V.PILL_BLEED + start * m.cw - 5, V.PILL_BLEED + (start + length) * m.cw + 4
        assert (xs[0], xs[-1]) == (left, right), text               # the whole pill is inside the image
        assert 0 < left and right < img.width() - 1
        for x in (left, right):                                      # both ends: a full side, round corners
            assert img.pixel(x, 1) != muted and img.pixel(x, m.ch - 2) != muted, (text, x)
        assert img.pixel(left - 1, mid) == well and img.pixel(right + 1, mid) == well
    # in the view the image is drawn that much further left: the text stays on its column
    s = _Model(110, 12)
    v = V.TerminalView(s, P.PHOSPHOR, 11, "iosv")
    v.resize(*console.window_size(110, 12, (m.cw, m.ch - 0)))
    v.show()
    s.feed(b"R1#show run\r\nhostname R1\r\n --More-- ")
    _pump(app, 0.3)
    img = v.grab().toImage()
    y = console.PAD_Y + 2 * m.ch + m.ch // 2
    xs = [x for x in range(img.width()) if img.pixel(x, y) == muted]
    assert (xs[0], xs[-1]) == (console.PAD_X + m.cw - 5, console.PAD_X + 9 * m.cw + 4)
    plain = V.LinePainter(P.PHOSPHOR, m).render(("hostname R1", (), False)).toImage()
    row = img.copy(console.PAD_X, console.PAD_Y + m.ch, plain.width(), m.ch)
    assert row == plain.convertToFormat(row.format())                # an ordinary row: unchanged, on the grid
    v.close()
    v.deleteLater()


@pytest.mark.linux_only
def test_the_clip_mark_and_the_scroll_handle_never_share_a_pixel(app):
    from PySide6.QtGui import QColor
    from labdesk_app.ui.console import view as V
    cw, ch = V.cell_metrics(11, 1.0)
    s = _Model(110, 12)
    v = V.TerminalView(s, P.PHOSPHOR, 11, None)
    v.resize(2 * console.PAD_X + 110 * cw, 2 * console.PAD_Y + 12 * ch)
    v.show()
    _pump(app, 0.3)
    s.feed(b"".join(b"%03d " % i + b"x" * 96 + b"\r\n" for i in range(60)))     # 100 characters a line
    msgs = []
    v.message.connect(lambda text, ms: msgs.append(text))
    v.resize(2 * console.PAD_X + 84 * cw + 4, v.height())           # 84 columns and 4 leftover pixels
    _pump(app, 0.4)
    assert (v.view_columns(), v.screen.columns) == (84, 110) and msgs[-1] == "showing 84 of 110 columns"
    g = v._bar_geometry()
    assert g is not None                                             # there is scrollback: a handle
    clip = console.PAD_X + 84 * cw
    mark = range(clip + V.CLIP_MARK_X - 1, clip + V.CLIP_MARK_X + V.CLIP_MARK_W + 1)
    bar = range(v.width() - V.BAR_RIGHT - V.BAR_W, v.width() - V.BAR_RIGHT)
    assert mark[-1] < bar[0] - 1 and bar[-1] < v.width()             # separate x ranges, a gap between them
    # ... also in the narrowest right padding there can be (16 px, no leftover pixel)
    assert V.CLIP_MARK_X + V.CLIP_MARK_W + 1 < console.PAD_X - V.BAR_RIGHT - V.BAR_W - 1
    img = v.grab().toImage()
    well, muted = QColor(P.PHOSPHOR.well).rgb(), QColor(P.PHOSPHOR.muted).rgb()

    def ink(r):                                                      # the mark's pixels in view row r
        y0 = console.PAD_Y + r * ch
        return [(x - clip, y - y0) for x in mark for y in range(y0, y0 + ch) if img.pixel(x, y) != well]

    rows = range(v.view_rows() - 1)                                  # every row but the empty cursor row
    under = [r for r in rows if g[2] <= console.PAD_Y + r * ch and console.PAD_Y + (r + 1) * ch <= g[2] + g[3]]
    free = [r for r in rows if console.PAD_Y + (r + 1) * ch <= g[2]]
    assert under and free
    assert len(ink(free[0])) >= 8 and all(ink(r) == ink(free[0]) for r in rows)   # the same whole mark on all
    for r in under:                                                  # ... and the handle beside it, whole
        y = console.PAD_Y + r * ch + ch // 2
        assert all(img.pixel(x, y) == muted for x in bar)
        assert all(img.pixel(x, y) == well for x in range(mark[-1] + 1, bar[0]))
    # a row that fits has no mark
    s.feed(b"\r\nshort\r\n")
    _pump(app, 0.2)
    img = v.grab().toImage()
    assert not ink(v.view_rows() - 2)
    v.close()
    v.deleteLater()


@pytest.mark.linux_only
def test_mouse_positions_become_whole_cells(app):
    """A mouse event's position is a QPointF: the cell under it must still be (int, int)."""
    from PySide6.QtCore import QPointF
    from labdesk_app.ui.console import view as V
    cw, ch = V.cell_metrics(11, 1.0)
    s = _Model(110, 12)
    v = V.TerminalView(s, P.PHOSPHOR, 11, None)
    v.resize(2 * console.PAD_X + 110 * cw, 2 * console.PAD_Y + 12 * ch)
    s.feed(b"".join(b"line %d\r\n" % i for i in range(30)))
    for pos, snap in ((QPointF(16.0, 12.0), True), (QPointF(57.5, 40.25), True), (QPointF(57.5, 40.25), False),
                      (QPointF(-5.0, -9.0), True), (QPointF(5000.0, 5000.0), True)):
        lid, col = v._cell_at(pos, snap)
        assert type(lid) is int and type(col) is int, (pos, lid, col)
    top = v.top_id()
    assert v._cell_at(QPointF(16.0, 12.0)) == (top, 0)
    assert v._cell_at(QPointF(16.0 + 4 * cw + cw / 2 + 0.5, 12.0 + ch + 0.9)) == (top + 1, 5)    # snaps
    assert v._cell_at(QPointF(16.0 + 4 * cw + cw / 2 + 0.5, 12.0 + ch + 0.9), snap=False) == (top + 1, 4)
    assert v._cell_at(QPointF(5000.0, 5000.0)) == (top + 11, 110)
    v.deleteLater()



# ---------------------------------------------------------------------------- C6 tab strip
def test_tab_strip_paints_its_own_chrome_in_every_app_theme(app):
    from labdesk_app.ui import themectl
    from labdesk_app.ui.console.tabstrip import ConsoleTabStrip
    ctl = themectl.controller() or themectl.install()
    before = ctl.mode if hasattr(ctl, "mode") else "auto"
    strip = ConsoleTabStrip(P.PHOSPHOR)
    strip.resize(900, 38)
    for label in ("R1", "HUB"):
        strip.add_tab(label)
    strip.set_current(0)
    try:
        for mode in ("light", "dark", "kde"):
            ctl.set_mode(mode)
            _pump(app, 0.05)
            img = strip.grab().toImage()
            assert img.pixelColor(700, 20).name().upper() == P.PHOSPHOR.chrome, mode      # no app QSS leak
            assert img.pixelColor(700, 37).name().upper() == P.PHOSPHOR.hairline, mode
            r = strip.tab_rects()[0]
            assert img.pixelColor(r.center().x(), 30).name().upper() == P.PHOSPHOR.well, mode   # active = well
            assert img.pixelColor(r.center().x(), 0).name().upper() == P.PHOSPHOR.accent, mode  # the 2 px bar
    finally:
        ctl.set_mode(before)
        strip.deleteLater()


def test_tab_strip_layout_states_and_overflow(app):
    from PySide6.QtCore import QPoint, Qt
    from labdesk_app.ui.console import tabstrip as TS
    strip = TS.ConsoleTabStrip(P.PHOSPHOR)
    strip.resize(700, 38)
    assert strip.height() == 38 and strip.focusPolicy() == Qt.NoFocus
    for label in ("R1", "HUB", "a-device-with-a-very-long-name-indeed-0123456789"):
        strip.add_tab(label)
    w = strip._widths()
    assert w[0] == TS.TAB_MIN and w[2] == TS.TAB_MAX and all(TS.TAB_MIN <= x <= TS.TAB_MAX for x in w)
    assert strip.tab_rects()[0].x() == TS.LOGO_W == 32
    # the dot says the SESSION state, by shape, with a tooltip
    assert strip.tabs[0].tooltip() == "session started - waiting for output"
    strip.set_state(0, TS.LIVE)
    assert strip.tabs[0].tooltip() == "session live - output received"
    strip.set_state(0, TS.ENDED, 255)
    assert strip.tabs[0].tooltip() == "session ended (status 255)"
    strip.set_current(1)
    strip.set_unseen(0, True)
    strip.set_unseen(1, True)                                        # the current tab is never 'unseen'
    assert strip.tabs[0].unseen and not strip.tabs[1].unseen
    strip.set_current(0)
    assert not strip.tabs[0].unseen
    # clicks: a tab selects, the cross of the active tab closes, the dots open the menu
    got = []
    strip.selected.connect(lambda i: got.append(("sel", i)))
    strip.close_requested.connect(lambda i: got.append(("close", i)))
    strip.menu_requested.connect(lambda p: got.append(("menu",)))
    rects = strip.tab_rects()
    assert strip._hit(rects[1].center()) == (1, False)
    assert strip._hit(strip._close_rect(rects[0]).center()) == (0, True)
    assert strip._hit(strip._close_rect(rects[1]).center()) == (1, False)       # no cross on an idle tab
    assert strip._hit(QPoint(strip.width() - 10, 10)) == (-2, False)
    # overflow: tabs first shrink to 96 px, then the strip scrolls
    for i in range(12):
        strip.add_tab(f"router-number-{i}")
    assert set(strip._widths()) == {TS.TAB_MIN} and strip._overflow() > 0
    strip.set_current(len(strip.tabs) - 1)
    area = strip._tabs_area()
    last = strip.tab_rects()[-1]
    assert area.left() <= last.left() and last.right() <= area.right() and strip._scroll > 0
    strip.ring_bell(0)
    assert strip.tabs[0].bell_until > time.monotonic()
    strip.grab()
    strip.deleteLater()



def test_a_scrolled_tab_strip_starts_with_a_whole_tab(app):
    """Overflow scrolls a whole tab at a time: no tab (and no session dot) is ever cut by a chevron."""
    from PySide6.QtCore import QPoint, Qt
    from PySide6.QtTest import QTest
    from labdesk_app.ui.console import tabstrip as TS
    strip = TS.ConsoleTabStrip(P.PHOSPHOR)
    strip.resize(700, 38)
    strip.show()
    for i in range(12):
        strip.add_tab(f"PE-CORE-{i}")
        strip.set_state(i, TS.LIVE)                                  # a solid dot
    strip.set_current(0)
    area = strip._tabs_area()
    widths = strip._widths()
    assert set(widths) == {TS.TAB_MIN} and strip._overflow() == 12 * 96 - area.width()
    stops = strip._stops()
    assert stops[0] == 0 and stops == sorted(set(stops)) and stops[-1] >= strip._overflow()
    assert all(p == k * 96 - TS.CHEVRON_W for k, p in enumerate(stops) if k)     # a tab boundary each
    got = []
    strip.selected.connect(got.append)

    def check():
        vis = strip._visible_area()
        rects = strip.tab_rects()
        shown = [i for i, r in enumerate(rects) if strip._shown(r)]
        assert shown and strip._scroll in stops
        first = rects[shown[0]]
        assert first.left() == vis.left() == area.left() + (TS.CHEVRON_W if strip._scroll else 0)
        for i in shown:                                              # every painted dot is whole
            d = strip._dot_rect(rects[i])
            assert vis.left() <= d.left() and d.right() <= vis.right() + 1, (strip._scroll, i)
        for i, r in enumerate(rects):                                # what is not painted cannot be clicked
            if i not in shown and area.contains(r.center()):
                assert strip._hit(r.center())[0] < 0
        return shown

    # not scrolled: no left chevron, a right one
    assert check()[0] == 0 and strip._visible_area().right() == area.right() - TS.CHEVRON_W
    cut = next(i for i, r in enumerate(strip.tab_rects()) if strip._shown(r) and r.right() > area.right())
    strip._hover = cut                                               # the tab the right chevron cuts:
    r = strip.tab_rects()[cut]                                       # no cross is painted, none is hit
    assert strip._hit(QPoint(r.left() + 20, 10)) == (cut, False)
    assert strip._hit(strip._close_rect(r).center())[1] is False
    strip._hover = -1
    assert strip._hit(QPoint(area.left() + 5, 10)) == (0, False)
    assert strip._hit(QPoint(area.right() - 5, 10)) == (TS.RIGHT_CHEVRON, False)
    # the wheel and the chevrons move one stop
    for k in range(1, len(stops)):
        QTest.mouseClick(strip, Qt.LeftButton, Qt.NoModifier, QPoint(area.right() - 5, 10))
        assert strip._scroll == stops[k]
        assert check()[0] == k
    assert strip._visible_area().right() == area.right()            # the end: no right chevron
    last = strip.tab_rects()[-1]
    assert last.right() <= area.right() and strip._shown(last)
    assert strip._hit(QPoint(area.left() + 5, 10)) == (TS.LEFT_CHEVRON, False)
    QTest.mouseClick(strip, Qt.LeftButton, Qt.NoModifier, QPoint(area.left() + 5, 10))
    assert strip._scroll == stops[-2] and not got                   # a chevron scrolls, it selects nothing
    strip._step_scroll(-99)
    assert strip._scroll == 0
    # selecting a tab scrolls it into view, whole, on a stop
    for i in (11, 0, 7, 3, 11, 5):
        strip.set_current(i)
        r = strip.tab_rects()[i]
        vis = strip._visible_area()
        assert vis.left() <= r.left() and r.right() <= vis.right() and i in check(), i
    # the scrolled strip, painted: nothing but chrome between the chevron and the first whole tab's dot
    strip.set_current(11)
    _pump(app, 0.05)
    img = strip.grab().toImage()
    vis = strip._visible_area()
    first = next(r for r in strip.tab_rects() if strip._shown(r))
    dot = strip._dot_rect(first)
    assert img.pixelColor(int(dot.center().x()), int(dot.center().y())).name().upper() == P.PHOSPHOR.body
    for x in range(vis.left(), int(dot.left())):
        assert img.pixelColor(x, 19).name().upper() == P.PHOSPHOR.chrome, x
    # removing tabs keeps the scroll on a stop
    for _ in range(6):
        strip.remove_tab(0)
    assert strip._scroll in strip._stops()
    strip.close()
    strip.deleteLater()


def test_a_tab_without_a_close_cross_gives_its_room_to_the_label(app):
    from PySide6.QtCore import QRect
    from PySide6.QtGui import QFontMetricsF
    from labdesk_app.ui.console import tabstrip as TS
    strip = TS.ConsoleTabStrip(P.PHOSPHOR)
    r = QRect(100, 0, TS.TAB_MIN, 38)
    with_cross, without = strip._label_rect(r, True), strip._label_rect(r, False)
    assert with_cross.left() == without.left() == 100 + 12 + 8 + 8
    assert with_cross.width() == 44 and without.width() == 56        # 96 px tab: 12 px more for the label
    assert with_cross.right() < strip._close_rect(r).left()          # the label never runs under the cross
    assert without.right() == r.right() - TS.PAD                     # 12 px padding at the right
    fm = QFontMetricsF(strip._fonts()[0])
    narrow = fm.elidedText("PE-CORE-1", Qt_ElideMiddle(), with_cross.width())
    wide = fm.elidedText("PE-CORE-1", Qt_ElideMiddle(), without.width())
    assert len(wide) > len(narrow)                                   # more of the name is readable
    # a tab at its natural width shows the whole label either way
    full = QRect(0, 0, TS.TAB_MAX, 38)
    assert strip._label_rect(full, True).width() == TS.TAB_MAX - TS.TAB_EXTRA + 8
    strip.deleteLater()


def Qt_ElideMiddle():
    from PySide6.QtCore import Qt
    return Qt.ElideMiddle


def test_status_line_is_26_px_and_turns_into_the_ended_banner(app):
    from labdesk_app.ui.console.statusline import ConsoleStatusLine
    s = ConsoleStatusLine(P.PHOSPHOR)
    s.resize(600, 26)
    s.set_state("session live")
    s.set_right("110 × 42")
    img = s.grab().toImage()
    assert s.height() == 26 and img.pixelColor(300, 13).name().upper() == P.PHOSPHOR.chrome
    assert img.pixelColor(300, 0).name().upper() == P.PHOSPHOR.hairline
    s.set_state("session ended (status 255)", True, True)
    img = s.grab().toImage()
    assert img.pixelColor(300, 13).name().upper() == P.PHOSPHOR.ended_bg
    assert img.pixelColor(300, 0).name().upper() == P.PHOSPHOR.ended_border
    s.set_state("session ended (status 0)", True, False)             # a clean exit is not an alarm
    assert s.grab().toImage().pixelColor(300, 13).name().upper() == P.PHOSPHOR.chrome
    s.show_message("showing 90 of 110 columns", 2000)
    assert s.flash == "showing 90 of 110 columns"
    s.deleteLater()


def test_dialogs_are_modal_for_their_own_window_only(app):
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QWidget
    from labdesk_app.ui.dialogs import LabDeskDialog
    parent = QWidget()
    assert LabDeskDialog("x", parent).windowModality() == Qt.WindowModal        # the console stays typeable
    assert LabDeskDialog("x").windowModality() != Qt.WindowModal
    parent.deleteLater()


def test_console_command_meta_is_not_part_of_the_request_line():
    cmd = launchers.cml_console("R1", UUID, "admin")
    # Windows: the in-process console transport rides in meta (never in the request line either)
    assert {k: v for k, v in cmd.meta.items() if k != "transport"} == {}
    assert ("transport" in cmd.meta) == (sys.platform == "win32")
    cmd.meta.update(key="lab:cml:n1", kind="cml", node_definition="iosv", lab_title="My lab")
    assert "meta" not in cmd.as_json() and "My lab" not in cmd.as_json()
    assert launchers.ssh_to_device("R1", "10.0.0.1").meta == {"kind": "ssh"}


def test_controller_fills_the_console_meta_from_the_live_node(tmp_path):
    from labdesk_app.controller import LabController
    cred = tmp_path / "cml-credentials.env"
    cred.write_text("CML_ADMIN_USER=admin\n", encoding="utf-8")

    class Client:
        _cred_file = cred

        def node(self, lab_id, ident):
            return {"label": "R1", "state": "BOOTED", "node_definition": "iosv"}

        def console_key(self, lab_id, ident):
            return UUID
    ctl = LabController(Client(), state=object(), telemetry=False)
    ctl.lab_id, ctl.lab_title = "lab1", "PE Hub-Spoke HSRP"
    cmd = ctl.console_command("cml:n1")
    assert {k: v for k, v in cmd.meta.items() if k != "transport"} == {
        "key": "lab1:cml:n1", "kind": "cml", "node_definition": "iosv", "lab_title": "PE Hub-Spoke HSRP"}
    assert cmd.argv[-2:] == ["connect", UUID] and cmd.title == "R1"
    if sys.platform != "win32":
        kvm = ctl.console_command("kvm:vmx1")
        assert kvm.meta["kind"] == "kvm" and kvm.meta["key"] == "lab1:kvm:vmx1"


@pytest.mark.linux_only            # Windows: a command without a console transport goes to Windows Terminal
def test_launcher_refuses_anything_but_ssh_virsh_telnet(app, tmp_path):
    from labdesk_app.ui.console.launcher import ConsolePrefs, ConsoleWindowLauncher
    from PySide6.QtCore import QSettings
    L = ConsoleWindowLauncher(ConsolePrefs(QSettings(str(tmp_path / "c.ini"), QSettings.IniFormat)))
    assert L.supports_look is True and L.neon is True and L.window is None
    for argv in (["bash"], ["/bin/sh", "-c", "id"], [], ["python3", "x.py"]):
        with pytest.raises(ValueError):
            L.open(launchers.ConsoleCommand("R1", argv, {}))
    assert L.window is None and L.live_count() == 0 and L.sessions() == []
    L.set_busy("lab:cml:n1", True, 3)                                 # no window: nothing happens
    _pump(app, 0.05)
    L.deleteLater()


def test_console_prefs_persist_and_clamp(app, tmp_path):
    from PySide6.QtCore import QSettings
    from labdesk_app.ui.console.launcher import ConsolePrefs
    st = QSettings(str(tmp_path / "c.ini"), QSettings.IniFormat)
    p = ConsolePrefs(st)
    assert (p.neon, p.soft, p.highlight, p.font_pt, p.scrollback) == (True, False, True, 11, 50_000)
    seen = []
    p.changed.connect(lambda: seen.append(1))
    p.zoom(1)
    p.zoom(1)
    assert p.font_pt == 13
    for _ in range(20):
        p.zoom(1)
    assert p.font_pt == 20
    for _ in range(20):
        p.zoom(-1)
    assert p.font_pt == 8
    p.zoom(0)
    assert p.font_pt == 11
    p.set("neon", False)
    p.set("highlight", False)
    assert p.palette() is P.PLAIN
    p.set("neon", False)                                             # no change: no signal
    n = len(seen)
    p.set("neon", False)
    assert len(seen) == n
    st.sync()
    q = ConsolePrefs(QSettings(str(tmp_path / "c.ini"), QSettings.IniFormat))
    assert (q.neon, q.highlight, q.font_pt) == (False, False, 11)


def test_console_is_available_only_with_pyte_and_setsid(monkeypatch):
    from labdesk_app import plat
    assert console.available() == (plat.IS_WINDOWS or (plat.IS_LINUX and os.path.exists("/usr/bin/setsid")))
    monkeypatch.setattr(console, "SETSID", "/nonexistent/setsid")
    assert console.available() is plat.IS_WINDOWS          # Windows needs no setsid (in-process streams)
    monkeypatch.undo()
    monkeypatch.setattr(plat, "IS_LINUX", False)
    monkeypatch.setattr(plat, "IS_WINDOWS", False)
    assert console.available() is False


# ============================================================================ Linux: pty session, window
def _fake_ssh(tmp_path: Path) -> str:
    """A long-running child whose argv[0] ends with 'ssh' (python behind a symlink): no network."""
    link = tmp_path / "bin" / "ssh"
    link.parent.mkdir(exist_ok=True)
    if not link.exists():
        link.symlink_to(os.path.realpath(sys.executable))
    return str(link)


ECHO = ("import sys\n"
        "print('R1#', end='', flush=True)\n"
        "for line in sys.stdin:\n"
        "    print('got ' + line.strip(), flush=True)\n"
        "    print('R1#', end='', flush=True)\n")


def _cmd(tmp_path, label="R1", extra=(), key=None, nd="iosv"):
    argv = [_fake_ssh(tmp_path), "-I", "-u", "-c", ECHO, *extra]
    return launchers.ConsoleCommand(label, argv, {}, {"key": key or f"lab:{label}", "kind": "cml",
                                                      "node_definition": nd, "lab_title": "My lab"})


@pytest.mark.linux_only
def test_pty_session_runs_the_argv_on_a_real_tty_and_is_seen_by_telemetry(app, tmp_path):
    from labdesk_app import telemetry
    from labdesk_app.ui.console import session as S
    s = S.PtySession(100, 30)
    states = []
    s.state_changed.connect(states.append)
    assert s.start(_cmd(tmp_path, extra=("connect", UUID)).argv, {"LABDESK_TEST": "1"})
    assert s.state == S.STARTED and s.pid
    assert _until(app, lambda: s.screen.cursor_row_text() == "R1#")
    assert s.state == S.LIVE and states == [S.STARTED, S.LIVE]
    argv = Path(f"/proc/{s.pid}/cmdline").read_bytes().split(b"\0")
    assert argv[0].endswith(b"ssh")                                  # setsid exec'd in place: the pid IS ssh
    assert UUID in telemetry.open_console_uuids(set())               # probing pauses, telemetry unchanged
    assert UUID not in telemetry.open_console_uuids({s.pid})
    env = Path(f"/proc/{s.pid}/environ").read_bytes().split(b"\0")
    assert b"TERM=linux" in env and b"LABDESK_TEST=1" in env
    # a real tty of the model's size, in its own session
    import fcntl, struct, termios                                    # noqa: E401
    assert struct.unpack("HHHH", fcntl.ioctl(s.fd, termios.TIOCGWINSZ, b"\0" * 8))[:2] == (30, 100)
    assert os.getsid(s.pid) == s.pid
    s.write(b"show clock\r")
    assert _until(app, lambda: any("got show clock" in s.screen.line(i)[0]
                                   for i in range(s.screen.first_id, s.screen.first_id + s.screen.total_lines)))
    s.resize(120, 20)
    assert (s.screen.columns, s.screen.lines) == (120, 20)
    assert struct.unpack("HHHH", fcntl.ioctl(s.fd, termios.TIOCGWINSZ, b"\0" * 8))[:2] == (20, 120)
    s.resize(90, 20)                                                 # columns never shrink; the pty agrees
    assert struct.unpack("HHHH", fcntl.ioctl(s.fd, termios.TIOCGWINSZ, b"\0" * 8))[:2] == (20, 120)
    pid = s.pid
    s.close()                                                        # hang-up: the child ends by itself
    assert _until(app, lambda: not os.path.exists(f"/proc/{pid}"), 5.0)
    assert UUID not in telemetry.open_console_uuids(set())
    assert s not in S._CLOSING


@pytest.mark.linux_only
def test_pty_session_end_states(app, tmp_path, monkeypatch):
    from labdesk_app.ui.console import session as S
    s = S.PtySession(80, 24)
    assert s.start([_fake_ssh(tmp_path), "-I", "-c", "import sys; print('bye'); sys.exit(7)"])
    assert _until(app, lambda: s.state == S.ENDED)
    assert s.status == 7 and S.status_text(7) == "session ended (status 7)" and not s.alive
    s.screen.sync()
    assert s.screen.screen_line(0)[0] == "bye"
    s.write(b"ignored")                                              # no fd any more: nothing happens
    # the child cannot start: the tab opens ended, with the error for the status line
    monkeypatch.setattr(S, "SETSID", str(tmp_path / "no-setsid"))
    t = S.PtySession(80, 24)
    assert t.start(["ssh", "x"]) is False
    assert t.state == S.ENDED and "could not start ssh" in t.error and t.pid is None
    t.close()
    s.close()


@pytest.mark.linux_only
def test_long_output_is_fed_in_slices_and_the_scrollback_keeps_it(app, tmp_path):
    from labdesk_app.ui.console import session as S
    s = S.PtySession(110, 42)
    code = "for i in range(3000): print('GigabitEthernet0/1   10.0.12.1   YES NVRAM  up    up', i)"
    assert s.start([_fake_ssh(tmp_path), "-I", "-c", code])
    passes = []
    s.updated.connect(lambda: passes.append(len(s._pending)))
    assert _until(app, lambda: s.state == S.ENDED, 20.0)
    assert s.status == 0 and len(s.screen.history) >= 3000 - 42
    assert max(passes) <= S.PENDING_MAX + S.READ_SIZE                # read only what the slices parse
    assert len(passes) > 3                                           # more than one event-loop pass
    assert s.screen.line(s.screen.first_id)[0].endswith(" 0")
    s.close()


@pytest.fixture()
def console_window(app, tmp_path, monkeypatch):
    from PySide6.QtCore import QSettings
    from labdesk_app.ui import dialogs
    from labdesk_app.ui.console.launcher import ConsolePrefs, ConsoleWindowLauncher
    asked = []
    monkeypatch.setattr(dialogs, "confirm", lambda parent, title, text, verb, **kw: asked.append(verb) or True)
    L = ConsoleWindowLauncher(ConsolePrefs(QSettings(str(tmp_path / "console.ini"), QSettings.IniFormat)))
    L.asked = asked
    yield L
    L.shutdown()
    _pump(app, 0.1)
    L.deleteLater()


@pytest.mark.linux_only
def test_each_new_console_adds_a_tab_and_reopening_switches_or_restarts(app, tmp_path, console_window):
    from labdesk_app.ui.console import session as S
    L = console_window
    notes = []
    L.notice.connect(lambda text, kind: notes.append(text))
    r1, hub = _cmd(tmp_path, "R1"), _cmd(tmp_path, "HUB", extra=("hub",))
    assert L.open(r1) == "LabDesk console tab 'R1'"                  # returns at once, no wait
    assert L.window is None                                          # nothing before the GUI thread runs
    _pump(app, 0.1)
    win = L.window
    assert win is not None and win.parent() is None and [t.label for t in win.tabs] == ["R1"]
    assert win.windowTitle() == "R1 - My lab - LabDesk console"
    L.open(hub)
    _pump(app, 0.1)
    assert [t.label for t in win.tabs] == ["R1", "HUB"] and win.current == 1        # new tab at the right
    assert win.stack.currentWidget() is win.tabs[1].view and win.windowTitle().startswith("HUB - ")
    assert _until(app, lambda: all(t.session.state == S.LIVE for t in win.tabs))
    assert [t.state for t in win.strip.tabs] == ["live", "live"] and L.live_count() == 2
    assert win.status.text.startswith("session live") and "Ctrl-]" in win.status.text
    assert L.sessions() == [{"id": 0, "title": "R1", "state": "live"}, {"id": 1, "title": "HUB", "state": "live"}]
    # the same device again (identical argv): no third tab - its tab is selected
    L.open(_cmd(tmp_path, "R1"))
    assert _until(app, lambda: notes, 3.0)
    assert len(win.tabs) == 2 and win.current == 0
    assert notes[0].startswith("R1 console already open - switched to its tab")
    # the session of R1 ends (the device closed it): the tab stays, ended; reopening restarts it in place
    win.tabs[0].view.send(b"hello\r")
    assert _until(app, lambda: "got hello" in "".join(win.tabs[0].session.screen.line(i)[0] for i in range(3)))
    pid = win.tabs[0].session.pid
    os.kill(pid, 15)                                                 # (a pid this test started)
    assert _until(app, lambda: win.tabs[0].session.state == S.ENDED)
    assert win.strip.tabs[0].state == "ended" and win.strip.tabs[0].status == -15 and L.live_count() == 1
    assert win.status.ended and win.status.failed and "open the device again" in win.status.text
    L.open(_cmd(tmp_path, "R1"))
    assert _until(app, lambda: win.tabs[0].session.state == S.LIVE)
    scr = win.tabs[0].session.screen
    scr.sync()
    text = "\n".join(scr.line(i)[0] for i in range(scr.first_id, scr.first_id + scr.total_lines))
    assert len(win.tabs) == 2 and "got hello" in text and text.count("R1#") >= 2   # scrollback kept
    assert win.tabs[0].session.pid != pid and "R1 console restarted in its tab" in notes
    # LabDesk types a show script on R1's console line: the status line says so (never the terminal)
    L.set_busy("lab:R1", True, 3)
    _pump(app, 0.1)
    assert win.status.text.startswith("LabDesk is running 3 show commands on this console")
    L.set_busy("lab:R1", False)
    _pump(app, 0.1)
    assert win.status.text.startswith("session live")
    # tabs switch with Ctrl+PgDn / Ctrl+Tab (the view's signal), focus stays in the terminal
    win.tabs[0].view.switch_tab.emit(1)
    assert win.current == 1
    win.step_tab(1)
    assert win.current == 0
    # closing a live tab asks; an ended one does not; the last tab closes the window
    pids = [t.session.pid for t in win.tabs]
    win.close_tab(0)
    assert L.asked == ["Close console"] and [t.label for t in win.tabs] == ["HUB"]
    os.kill(pids[1], 15)
    assert _until(app, lambda: win.tabs[0].session.state == S.ENDED)
    win.close_tab(0)
    assert L.asked == ["Close console"] and not win.tabs and not win.isVisible()
    assert _until(app, lambda: not any(os.path.exists(f"/proc/{p}") for p in pids), 5.0)


@pytest.mark.linux_only
def test_window_look_grid_and_close_with_live_tabs(app, tmp_path, console_window, monkeypatch):
    from labdesk_app.ui import dialogs
    from labdesk_app.ui.console import session as S
    from labdesk_app.ui.console import view as V
    L = console_window
    L.open(_cmd(tmp_path, "R1"))
    L.open(_cmd(tmp_path, "SRV", extra=("srv",), nd="alpine"))
    _pump(app, 0.2)
    win = L.window
    cw, ch = V.cell_metrics(11, 1.0)
    win.resize(*console.window_size(110, 42, (cw, ch)))
    _pump(app, 0.4)                                                  # the 150 ms resize debounce
    assert win.grid_ok and win.minimumWidth() == 32 + 80 * cw and win.minimumHeight() == 88 + 12 * ch
    assert win.strip.height() == 38 and win.status.height() == 26
    v = win.tabs[win.current].view
    assert (v.view_columns(), v.view_rows()) == (110, 42) and v.width() == 32 + 110 * cw
    assert (v.screen.columns, v.screen.lines) == (110, 42) and win.status.right == "110 × 42"
    assert _until(app, lambda: all(t.session.state == S.LIVE for t in win.tabs))
    # the lens: only on the IOS tab, and only once the prompt was seen
    r1, srv = win.tabs[0].view, win.tabs[1].view
    assert r1.lens_applies and not srv.lens_applies
    assert _until(app, lambda: r1.known_hosts == {"R1"}) and not srv.known_hosts
    line = r1.screen.line(r1.screen.cursor_line_id())
    assert r1._spans(r1.screen.cursor_line_id(), line) == ((0, 2, lens.PROMPT), (2, 1, lens.PROMPT))
    assert srv._spans(srv.screen.cursor_line_id(), srv.screen.line(srv.screen.cursor_line_id())) == ()
    # look switches apply live to every tab
    L.neon = False
    assert win.strip.pal is P.PLAIN and r1.pal is P.PLAIN and win.status.pal is P.PLAIN
    L.neon = True
    L.prefs.set("soft", True)
    assert r1.pal is P.SOFT_PHOSPHOR
    L.prefs.set("soft", False)
    L.prefs.set("highlight", False)
    assert r1._spans(r1.screen.cursor_line_id(), line) == ()
    L.prefs.set("highlight", True)
    img = win.grab().toImage()
    assert img.pixelColor(win.width() - 60, 20).name().upper() == P.PHOSPHOR.chrome       # strip
    assert img.pixelColor(win.width() // 2, win.height() // 2).name().upper() == P.PHOSPHOR.well
    # text size: live, columns never shrink, Ctrl+Alt+0 restores
    L.prefs.zoom(1)
    _pump(app, 0.3)
    assert v.cell == V.cell_metrics(12, 1.0) and v.screen.columns == 110 and v.view_columns() < 110
    L.prefs.zoom(0)
    _pump(app, 0.3)
    assert v.cell == (cw, ch) and (v.screen.columns, v.view_columns()) == (110, 110)
    # a narrower window clips on the right and says so; the model keeps its columns
    msgs = []
    v.message.connect(lambda text, ms: msgs.append(text))
    win.resize(console.window_size(90, 30, (cw, ch))[0], win.height())
    _pump(app, 0.4)
    assert v.screen.columns == 110 and msgs and msgs[-1] == "showing 90 of 110 columns"
    # real key events: Tab, '?', Ctrl-C, Ctrl-], arrows reach the device; Ctrl+Shift+C does not
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    sent = []
    write = v.session.write
    monkeypatch.setattr(v.session, "write", lambda data: sent.append(data))
    v.setFocus()
    QTest.keyClick(v, Qt.Key_Tab)
    QTest.keyClicks(v, "sh?")
    QTest.keyClick(v, Qt.Key_C, Qt.ControlModifier)
    QTest.keyClick(v, Qt.Key_BracketRight, Qt.ControlModifier)
    QTest.keyClick(v, Qt.Key_AsciiCircum, Qt.ControlModifier | Qt.ShiftModifier)
    QTest.keyClick(v, Qt.Key_Up)
    QTest.keyClick(v, Qt.Key_Return)
    QTest.keyClick(v, Qt.Key_C, Qt.ControlModifier | Qt.ShiftModifier)          # copy, not ^C
    QTest.keyClick(v, Qt.Key_PageUp, Qt.ShiftModifier)                           # scrollback, not sent
    assert b"".join(sent) == b"\tsh?\x03\x1d\x1e\x1b[A\r"
    monkeypatch.setattr(v.session, "write", write)
    cur = win.current
    QTest.keyClick(v, Qt.Key_PageDown, Qt.ControlModifier)                       # next tab
    assert win.current != cur and win.tabs[win.current].view.hasFocus() or not win.isActiveWindow()
    win.select(cur)
    # typing goes to the device; copy returns the raw text
    v.setFocus()
    v.send(b"show version\r")
    assert _until(app, lambda: "got show version" in v.screen.line(v.screen.cursor_line_id() - 1)[0])
    lid = v.screen.cursor_line_id() - 1
    v._sel = ((lid, 0), (lid, 16))
    assert v.selected_text() == "got show version"
    v.copy()
    assert app.clipboard().text() == "got show version"
    # closing the window with live tabs asks once; Cancel keeps everything
    monkeypatch.setattr(dialogs, "confirm", lambda *a, **k: L.asked.append(a[3]) or False)
    win.close()
    assert L.asked[-1] == "Close 2 consoles" and win.isVisible() and L.live_count() == 2
    monkeypatch.setattr(dialogs, "confirm", lambda *a, **k: L.asked.append(a[3]) or True)
    pids = [t.session.pid for t in win.tabs]
    win.close()
    assert not win.tabs and not win.isVisible()
    assert _until(app, lambda: not any(os.path.exists(f"/proc/{p}") for p in pids), 5.0)


@pytest.mark.linux_only
def test_a_real_mouse_selection_copies_and_the_console_menu_opens(app, tmp_path, console_window):
    """Selection driven by real mouse events through the window (their positions are floats), never by
    setting the selection by hand: drag, Ctrl+Shift+C, the console menu, double click, scrollback."""
    from PySide6.QtCore import QPoint, Qt
    from PySide6.QtTest import QTest
    from labdesk_app.ui.console import session as S
    L = console_window
    L.open(_cmd(tmp_path, "R1"))
    _pump(app, 0.1)
    win = L.window
    cw, ch = win.cell()
    win.resize(*console.window_size(110, 14, (cw, ch)))
    _pump(app, 0.4)
    v = win.tabs[0].view
    scr = v.screen
    assert _until(app, lambda: win.tabs[0].session.state == S.LIVE)
    for i in range(30):                                              # one at a time: no type-ahead echo
        v.send(b"line %02d alpha\r" % i)
        assert _until(app, lambda: scr.line(scr.cursor_line_id() - 1)[0] == f"got line {i:02d} alpha"
                      and scr.cursor_row_text() == "R1#")
    assert len(scr.history) > 20
    h = win.windowHandle()
    off = v.mapTo(win, QPoint(0, 0))

    def at(lid, col, dx=2):
        return off + QPoint(console.PAD_X + col * cw + dx, console.PAD_Y + (lid - v.top_id()) * ch + ch // 2)

    def drag(a, b):
        QTest.mousePress(h, Qt.LeftButton, Qt.NoModifier, a)
        _pump(app, 0.03)
        QTest.mouseMove(h, b)
        _pump(app, 0.03)
        QTest.mouseRelease(h, Qt.LeftButton, Qt.NoModifier, b)
        _pump(app, 0.05)

    lid = scr.cursor_line_id() - 1
    cb = app.clipboard()
    cb.setText("BEFORE")
    drag(at(lid, 0), at(lid, 11))                                    # within one line
    assert v._sel == ((lid, 0), (lid, 11))
    assert all(type(x) is int for end in v._sel for x in end)
    assert v.selected_text() == "got line 29"
    assert cb.text() == "BEFORE"                                     # selecting alone does not copy
    if cb.supportsSelection():
        assert cb.text(cb.Mode.Selection) == "got line 29"           # ... but fills the middle-click buffer
    v.setFocus()
    QTest.keyClick(h, Qt.Key_C, Qt.ControlModifier | Qt.ShiftModifier)
    _pump(app, 0.05)
    assert cb.text() == "got line 29"
    # the console menu (three dots, right click) opens while there is a selection; its Copy copies
    menu = win.build_menu(v)
    copy = menu.actions()[0]
    assert copy.text().startswith("Copy") and copy.isEnabled()
    cb.setText("BEFORE")
    copy.trigger()
    assert cb.text() == "got line 29"
    menu.deleteLater()
    got = []
    v.menu_requested.disconnect(win.show_menu)                       # (a real menu would block the test)
    v.menu_requested.connect(got.append)
    QTest.mouseClick(h, Qt.RightButton, Qt.NoModifier, at(lid, 3))
    _pump(app, 0.05)
    assert len(got) == 1 and v.selected_text() == "got line 29"      # a right click keeps the selection
    v.menu_requested.connect(win.show_menu)
    # over two lines: the device's text with the line break
    drag(at(lid - 1, 3), at(lid, 3))
    assert v._sel == ((lid - 1, 3), (lid, 3)) and v.selected_text() == "line 29 alpha\ngot"
    drag(at(lid, 8), at(lid - 1, 3))                                 # dragged backwards
    assert v.selected_text() == "line 29 alpha\ngot line"
    v.copy()
    assert cb.text() == "line 29 alpha\ngot line"
    # a click without a drag clears the selection: Copy is then disabled
    QTest.mouseClick(h, Qt.LeftButton, Qt.NoModifier, at(lid, 5))
    _pump(app, 0.05)
    assert v._sel is None and v.selected_text() == ""
    menu = win.build_menu(v)
    assert not menu.actions()[0].isEnabled()
    menu.deleteLater()
    # double click: the word
    QTest.mouseDClick(h, Qt.LeftButton, Qt.NoModifier, at(lid, 5))
    _pump(app, 0.05)
    assert v._sel == ((lid, 4), (lid, 8)) and v.selected_text() == "line"
    QTest.keyClick(h, Qt.Key_C, Qt.ControlModifier | Qt.ShiftModifier)
    assert cb.text() == "line"
    # ... also on a line that lives in the scrollback, and a drag across scrollback lines
    v.scroll_lines(20)
    _pump(app, 0.1)
    top = v.top_id()
    assert top + 3 < scr.first_id + len(scr.history) and v.scrolled_back == 20
    assert win.status.right.startswith("20 lines up")          # the status line follows the scroll at once...
    v.scroll_to_bottom()
    assert "lines up" not in win.status.right                  # ...and back at the live end, without device output
    v.scroll_lines(20)
    assert win.status.right.startswith("20 lines up") and v.top_id() == top
    words = scr.line(top + 1)[0].split(" ")
    QTest.mouseDClick(h, Qt.LeftButton, Qt.NoModifier, at(top + 1, 1))
    _pump(app, 0.05)
    assert v._sel == ((top + 1, 0), (top + 1, len(words[0]))) and v.selected_text() == words[0]
    drag(at(top, 0), at(top + 3, 0))
    assert v.selected_text() == "".join(scr.line(top + k)[0] + "\n" for k in range(3))
    win.grab()                                                       # paints with the selection
    assert win.build_menu(v).actions()[0].isEnabled()


@pytest.mark.linux_only
def test_a_new_text_size_is_not_unseen_output(app, tmp_path, console_window):
    """Only bytes of the device flag a background tab; resizing (text size, window) and the look do not."""
    from labdesk_app.ui.console import session as S
    L = console_window
    for label in ("R1", "R2", "HUB"):
        L.open(_cmd(tmp_path, label, extra=(label,)))
        _pump(app, 0.1)
    win = L.window
    assert _until(app, lambda: all(t.session.state == S.LIVE for t in win.tabs))
    assert _until(app, lambda: all(t.session.screen.cursor_row_text() == "R1#" for t in win.tabs))
    for i in (0, 1, 2):
        win.select(i)
        _pump(app, 0.25)                                             # the tab takes the window size
    assert [t.unseen for t in win.strip.tabs] == [False, False, False]
    outputs = []
    for t in win.tabs:
        t.session.output.connect(lambda t=t: outputs.append(t.label))
    L.prefs.zoom(1)
    _pump(app, 0.4)
    assert win.tabs[0].view.pt == 12 == win.tabs[2].view.pt
    L.prefs.zoom(0)
    L.prefs.set("soft", True)
    L.prefs.set("soft", False)
    L.prefs.set("highlight", False)
    L.prefs.set("highlight", True)
    win.resize(win.width() + 40, win.height() - 60)
    _pump(app, 0.4)
    assert [t.unseen for t in win.strip.tabs] == [False, False, False] and not outputs
    # the device says something on a background tab: that is unseen output, until the tab is visited
    win.tabs[0].view.send(b"show clock\r")
    assert _until(app, lambda: win.strip.tabs[0].unseen)
    assert [t.unseen for t in win.strip.tabs] == [True, False, False] and set(outputs) == {"R1"}
    win.select(0)
    assert not win.strip.tabs[0].unseen


@pytest.mark.linux_only
def test_main_window_installs_the_console_and_keeps_konsole_as_the_switch(app, tmp_path, monkeypatch):
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from PySide6.QtCore import QSettings
    QSettings.setDefaultFormat(QSettings.IniFormat)
    QSettings.setPath(QSettings.IniFormat, QSettings.UserScope, str(tmp_path))
    from ui_replay import replay_controller
    from labdesk_app.ui import main_window as MW
    from labdesk_app.ui.console.launcher import ConsoleWindowLauncher

    class Injected:
        def open(self, cmd):
            return "no"

    def make(launcher=None):
        ctl = replay_controller(tmp_path)
        ctl.lab_id = None
        ctl.lab_title = None
        win = MW.MainWindow(ctl, launcher)
        win.timer.stop()
        return win

    def drop(win):
        win._closing = True
        win.runner.wait(3000)
        win.deleteLater()

    inj = Injected()
    win = make(inj)
    assert win.launcher is inj and not win.console_menu.menuAction().isVisible()   # never replaced
    drop(win)

    win = make()
    try:
        assert isinstance(win.launcher, ConsoleWindowLauncher) and win.launcher.window is None
        assert win.console_menu.menuAction().isVisible()
        titles = [a.text() for a in win.console_menu.actions() if a.text()]
        assert titles == ["Neon green console", "Softer green", "Highlight prompts and errors", "Larger text",
                          "Smaller text", "Reset text size", "Use Konsole instead"]
        assert win.a_neon.isChecked() and win.a_highlight.isChecked() and not win.a_soft.isChecked()
        win.a_soft.trigger()
        assert win.launcher.prefs.soft and win.settings.value("console/soft") in (True, "true")
        win.a_text_larger.trigger()
        assert win.launcher.prefs.font_pt == 12
        win.a_text_reset.trigger()
        win.a_neon.trigger()
        assert not win.launcher.prefs.neon and not win.a_soft.isEnabled()
        win.launcher.prefs.set("neon", True)                         # changed in the console's own menu
        assert win.a_neon.isChecked()                                # ... the View menu follows
        # the off-switch
        win.a_konsole.trigger()
        assert isinstance(win.launcher, launchers.KonsoleLauncher) and win.settings.value("console/engine") == "konsole"
        assert not win.a_soft.isVisible() and win.a_neon.isVisible() is not None
        win.a_konsole.trigger()
        assert isinstance(win.launcher, ConsoleWindowLauncher)
        # closing LabDesk with live consoles asks once; Cancel keeps LabDesk open
        asked = []
        monkeypatch.setattr(win._console_launcher, "live_count", lambda: 2)
        monkeypatch.setattr(MW, "confirm", lambda parent, title, text, verb, **kw: asked.append(text) or False)
        from PySide6.QtGui import QCloseEvent
        ev = QCloseEvent()
        win.closeEvent(ev)
        assert not ev.isAccepted() and asked == ["2 consoles are open - closing LabDesk ends them"]
    finally:
        drop(win)

    # missing setsid / pyte: the automatic fallback
    from labdesk_app.ui import console as C
    monkeypatch.setattr(C, "available", lambda: False)
    win = make()
    assert isinstance(win.launcher, launchers.KonsoleLauncher) and not win.a_konsole.isVisible()
    drop(win)


@pytest.mark.linux_only
def test_double_click_opens_a_tab_runs_the_show_script_and_flags_the_status_line(app, tmp_path, monkeypatch):
    """MainWindow.open_console with the real console launcher: hint, tab, show script, 'LabDesk is typing'."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from PySide6.QtCore import QSettings
    QSettings.setDefaultFormat(QSettings.IniFormat)
    QSettings.setPath(QSettings.IniFormat, QSettings.UserScope, str(tmp_path))
    monkeypatch.setenv("LABDESK_SHOW_SCRIPTS", str(tmp_path / "show-scripts.json"))
    import ui_replay
    from labdesk_app import show_scripts as SS
    from labdesk_app.telemetry import ActionResult
    from labdesk_app.ui import dialogs
    from labdesk_app.ui import main_window as MW
    from labdesk_app.ui.console.launcher import ConsoleWindowLauncher
    ctl = ui_replay.replay_controller(tmp_path)
    ui_replay.poll_until_settled(ctl)
    win = MW.MainWindow(ctl)
    win.timer.stop()
    win.show()
    win._lab_opened()
    assert _until(app, lambda: win.view_data is not None and win.view_data.nodes and win.scene.nodes, 25.0)
    L = win.launcher
    try:
        assert isinstance(L, ConsoleWindowLauncher) and L.window is None
        keys = {it.label: k for k, it in win.scene.nodes.items()}
        r1 = keys["R1"]
        monkeypatch.setattr(MW, "info", lambda *a, **k: None)
        monkeypatch.setattr(dialogs, "confirm", lambda *a, **k: True)
        monkeypatch.setattr(ctl, "console_command", lambda key: launchers.ConsoleCommand(
            win._label(key), [_fake_ssh(tmp_path), "-I", "-u", "-c", ECHO, key], {},
            {"key": f"{ctl.lab_id}:{key}", "kind": "cml", "node_definition": "iosv", "lab_title": ctl.lab_title}))
        seen, logs = [], []
        log = win.log
        monkeypatch.setattr(win, "log", lambda text, *a, **k: (logs.append(text), log(text, *a, **k))[1])

        def script(key, cmds, **kw):
            end = time.time() + 3
            while time.time() < end and not seen:                     # the GUI thread reads the status line
                time.sleep(0.02)
            return ActionResult(True, "ok")
        monkeypatch.setattr(ctl.telemetry, "run_show_script", script)
        SS.save("R1", SS.ShowScript(["show standby brief", "show ip bgp summary"], True))
        win.open_console(r1)
        assert _until(app, lambda: L.window is not None and L.window.tabs, 10.0)
        con = L.window
        assert [t.label for t in con.tabs] == ["R1"] and con.windowTitle() == "R1 - Protocol Showcase - LabDesk console"
        assert _until(app, lambda: con.status.text.startswith("LabDesk is running 2 show commands"), 10.0)
        seen.append(1)
        assert _until(app, lambda: con.status.text.startswith("session live"), 10.0)
        assert "console: LabDesk console tab 'R1'" in logs
        assert any("first login" in t for t in logs)                  # the first-login hint is unchanged
        win.open_console(r1)                                          # again: no second tab
        assert _until(app, lambda: any("R1 console already open - switched to its tab" in t for t in logs), 5.0)
        assert len(con.tabs) == 1
    finally:
        L.shutdown()
        win._closing = True
        win.runner.wait(3000)
        win.hide()
