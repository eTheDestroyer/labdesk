"""LabDesk's own console window (Linux and Windows): tab strip + terminal well + status line.

``palette``, ``screen``, ``keymap``, ``lens`` and ``boxdraw`` are pure Python (no Qt) and import on every
platform; ``session`` / ``window`` need a pty and are imported only on Linux, when the first console opens.
"""
from __future__ import annotations

import os

SETSID = "/usr/bin/setsid"

# the window's fixed layout, px (pure numbers: importable everywhere)
TAB_STRIP_H = 38             # the tab strip is the header
STATUS_H = 26                # the status line closes the window
PAD_X, PAD_Y = 16, 12        # padding inside the terminal well
CHROME_H = TAB_STRIP_H + STATUS_H + 2 * PAD_Y            # 88: everything that is not text rows
DEFAULT_COLUMNS, DEFAULT_ROWS = 110, 42
MIN_COLUMNS, MIN_ROWS = 80, 12


def default_rows(available_height: int, cell_h: int = 21) -> int:
    """Rows of a new console window: 42, fewer when 90 % of the screen height is not enough, never < 12.
    (1080 px available -> 42 rows; a 1044 px work area -> 40 rows.)"""
    return max(MIN_ROWS, min(DEFAULT_ROWS, (int(0.9 * available_height) - CHROME_H) // cell_h))


def window_size(columns: int, rows: int, cell: tuple[int, int] = (9, 21)) -> tuple[int, int]:
    """Window size in px for a grid: 32 + columns x cell_w wide, 88 + rows x cell_h high."""
    return 2 * PAD_X + columns * cell[0], CHROME_H + rows * cell[1]


def available() -> bool:
    """The LabDesk console can run here: pyte importable and (Linux) /usr/bin/setsid present, or Windows
    (consoles are in-process streams there).  Otherwise MainWindow keeps the platform's default launcher
    (Konsole / Windows Terminal)."""
    from ... import plat
    if plat.IS_WINDOWS:
        pass                             # no pty: device consoles run as in-process streams (stream_session)
    elif not plat.IS_LINUX or not os.path.exists(SETSID):
        return False
    try:
        import pyte  # noqa: F401
        import wcwidth  # noqa: F401
    except ImportError:
        return False
    return True
