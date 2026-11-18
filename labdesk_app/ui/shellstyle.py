"""Style sheet for the window shell (L2 toolbar, L4 inspector, L5 log / status bar, L6 empty states, L9
dialogs, L10 rail).  Appended to the ThemeController's QSS; values come from the S1 tokens only."""
from __future__ import annotations

from . import theme as T


def _rgba(c, a: float) -> str:
    return f"rgba({c.red()},{c.green()},{c.blue()},{a:.3f})"


def qss(tk: T.Tokens, native: bool = False) -> str:
    n = lambda i: tk.N(i).name()                      # noqa: E731
    ink = tk.N(12)
    border = n(7) if tk.dark else n(6)
    hl = tk.accent_fill
    down = T.status_fill("down", tk).name()
    down_h = T.status_text("down", tk).name()
    seg_bg = n(3) if not tk.dark else n(3)
    out = f"""
/* L9: role buttons keep their fill even as the dialog default */
QPushButton[role="primary"], QPushButton[role="primary"]:default {{ background: {hl.name()}; color: white;
    border: 1px solid {hl.name()}; }}
QPushButton[role="primary"]:hover {{ background: {hl.lighter(108).name()}; }}
QPushButton[role="primary"]:disabled {{ background: {_rgba(hl, 0.35)}; border-color: transparent;
    color: {_rgba(tk.N(1), 0.85)}; }}
QPushButton[role="destructive"], QPushButton[role="destructive"]:default {{ background: {down}; color: white;
    border: 1px solid {down}; }}
QPushButton[role="destructive"]:hover {{ background: {down_h}; border-color: {down_h}; }}
QPushButton[role="destructive"]:disabled {{ background: {_rgba(T.status_fill("down", tk), 0.30)};
    border-color: transparent; color: {_rgba(tk.N(1), 0.85)}; }}
QPushButton[role="link"], QPushButton[role="link"]:default {{ background: transparent; border: none;
    color: {tk.accent_text.name()}; padding: 2px 4px; min-width: 0; }}
QPushButton[role="link"]:hover {{ text-decoration: underline; background: transparent; }}
QDialogButtonBox QPushButton {{ min-height: 20px; min-width: 88px; padding: 4px 14px; }}
QLabel#fielderror {{ color: {down_h}; }}
/* L2: lab name in the toolbar title block */
QToolButton#labname {{ border: none; background: transparent; padding: 2px 4px; color: {n(12)}; }}
QToolButton#labname:hover {{ text-decoration: underline; background: transparent; }}
/* L5: event log header */
QWidget#loghead {{ background: {n(1) if not tk.dark else n(2)}; border-bottom: 1px solid {border}; }}
QToolButton#segment {{ background: {seg_bg}; color: {n(11)}; border: 1px solid {border}; padding: 2px 10px;
    border-radius: 0; margin: 0; min-height: 18px; }}
QToolButton#segment[pos="first"] {{ border-top-left-radius: 6px; border-bottom-left-radius: 6px; }}
QToolButton#segment[pos="last"] {{ border-top-right-radius: 6px; border-bottom-right-radius: 6px; }}
QToolButton#segment[pos="mid"], QToolButton#segment[pos="last"] {{ border-left: none; }}
QToolButton#segment:hover {{ color: {n(12)}; }}
QToolButton#segment:checked {{ background: {n(1) if not tk.dark else n(5)}; color: {n(12)}; font-weight: 600; }}
QLineEdit#logsearch {{ padding: 2px 8px; min-height: 18px; }}
QTableView#logtable {{ background: {n(1) if not tk.dark else n(1)}; border: none; }}
/* L5 status bar */
QFrame#sbsep {{ color: {border}; background: {border}; max-width: 1px; border: none; margin: 0 4px; }}
QStatusBar QPushButton {{ padding: 1px 10px; min-height: 16px; }}
/* L4 inspector */
QTableWidget#insptable QHeaderView::section {{ font-size: {T.type("caption").pointSizeF():.1f}pt; font-weight: 600;
    color: {n(11)}; padding: 4px 6px; }}
QPushButton#summaryrow, QPushButton#recentrow {{ text-align: left; border: none; border-radius: 6px;
    background: transparent; padding: 0; min-height: 26px; }}
QPushButton#summaryrow:hover, QPushButton#recentrow:hover {{ background: {_rgba(ink, 0.06)}; }}
QPushButton#summaryrow:focus, QPushButton#recentrow:focus {{ border: 1px solid {tk.accent_ring.name()}; }}
QToolButton#more {{ border-radius: 6px; padding: 4px; }}
QToolButton#more:hover {{ background: {_rgba(ink, 0.08)}; }}
QToolButton#more::menu-indicator {{ image: none; width: 0; }}
/* L10 rail toggle */
QToolButton#railtoggle {{ border: none; border-bottom: 1px solid {border}; border-radius: 0; color: {n(11)};
    background: {n(2)}; padding: 4px; }}
QToolButton#railtoggle:hover {{ background: {n(3) if not tk.dark else n(4)}; color: {n(12)}; }}
"""
    if native:
        return out
    return out + f"""
QToolBar {{ padding: 0px 8px; }}
QToolBar QToolButton {{ min-width: 20px; padding: 3px 7px; }}
QToolBar QToolButton#menubutton {{ padding-right: 20px; }}
QListWidget#palette {{ padding-top: 4px; }}
QTabWidget#insptabs QTabBar::tab {{ padding: 4px 8px; margin: 2px 2px 6px 0;
    font-size: {T.type("small").pointSizeF():.1f}pt; }}
"""
