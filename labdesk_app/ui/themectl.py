"""ThemeController (L1 c/d + S1/S5): Fusion palette + QSS + tokens, applied app-wide and re-applied when the
system colour scheme or the application palette changes.

Modes: ``auto`` (follow the system light/dark preference), ``light``, ``dark`` (LabDesk's own neutral +
indigo tables) and ``kde`` ("KDE colours": the user's own palette and icon theme, native Fusion tabs and
check boxes, only the LabDesk cards / canvas tokens on top).
"""
from __future__ import annotations

import tempfile
from pathlib import Path

from PySide6.QtCore import QEvent, QObject, Qt, Signal
from PySide6.QtGui import QColor, QGuiApplication, QPalette
from PySide6.QtWidgets import QApplication

from ..settings import app_settings
from . import icons
from . import theme as T

MODES = (("auto", "Auto (follow system)"), ("light", "Light"), ("dark", "Dark"), ("kde", "KDE colours"))


def build_palette(tk: T.Tokens) -> QPalette:
    n = tk.N
    pal = QPalette()
    roles = {
        QPalette.Window: n(2), QPalette.WindowText: n(12), QPalette.Base: n(1) if not tk.dark else n(1),
        QPalette.AlternateBase: n(2) if not tk.dark else n(2), QPalette.Text: n(12),
        QPalette.Button: n(1) if not tk.dark else n(3), QPalette.ButtonText: n(12),
        QPalette.BrightText: QColor("#FFFFFF"), QPalette.Light: n(1) if not tk.dark else n(5),
        QPalette.Midlight: n(3) if not tk.dark else n(4), QPalette.Mid: n(7), QPalette.Dark: n(9),
        QPalette.Shadow: QColor(0, 0, 0, 90), QPalette.Highlight: tk.accent_fill,
        QPalette.HighlightedText: QColor("#FFFFFF"), QPalette.Link: tk.accent_text,
        QPalette.LinkVisited: tk.accent_text, QPalette.PlaceholderText: n(10) if not tk.dark else n(10),
        QPalette.ToolTipBase: n(12) if not tk.dark else n(4), QPalette.ToolTipText: n(1) if not tk.dark else n(12),
    }
    try:
        roles[QPalette.Accent] = tk.accent_fill
    except AttributeError:  # pragma: no cover (Qt < 6.6)
        pass
    for role, col in roles.items():
        pal.setColor(QPalette.All, role, col)
    for role in (QPalette.WindowText, QPalette.Text, QPalette.ButtonText):
        pal.setColor(QPalette.Disabled, role, n(8))
    pal.setColor(QPalette.Disabled, QPalette.Highlight, n(6))
    pal.setColor(QPalette.Inactive, QPalette.Highlight, tk.accent_fill)
    return pal


def _asset_dir() -> Path:
    d = Path(tempfile.gettempdir()) / f"labdesk-qss-{QGuiApplication.applicationPid() if QGuiApplication.instance() else 0}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _write_svg(name: str, body: str) -> str:
    p = _asset_dir() / name
    if not p.exists() or p.read_text(encoding="utf-8") != body:
        p.write_text(body, encoding="utf-8")
    return p.as_posix()


def _arrow(color: QColor, up: bool = False) -> str:
    d = "M2.5 6.5 L5 4 L7.5 6.5" if up else "M2.5 4 L5 6.5 L7.5 4"
    return _write_svg(f"arrow-{'up' if up else 'down'}-{color.name()[1:]}.svg",
                      f'<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10" viewBox="0 0 10 10">'
                      f'<path d="{d}" fill="none" stroke="{color.name()}" stroke-width="1.5" '
                      f'stroke-linecap="round" stroke-linejoin="round"/></svg>')


def _tick(color: QColor) -> str:
    return _write_svg(f"tick-{color.name()[1:]}.svg",
                      f'<svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 14 14">'
                      f'<path d="M3.5 7.2 L6 9.6 L10.6 4.6" fill="none" stroke="{color.name()}" stroke-width="1.8" '
                      f'stroke-linecap="round" stroke-linejoin="round"/></svg>')


def _rgba(c: QColor, a: float) -> str:
    return f"rgba({c.red()},{c.green()},{c.blue()},{a:.3f})"


def build_qss(tk: T.Tokens, native: bool = False) -> str:
    """Application style sheet (S5).  ``native`` (KDE colours): only the LabDesk-specific bits."""
    n = lambda i: tk.N(i).name()                      # noqa: E731
    ink = tk.N(12)
    border = n(7) if tk.dark else n(6)
    border_strong = n(8) if tk.dark else n(7)
    card = tk.card.name()
    down, down_h = T.status_fill("down", tk).name(), T.status_text("down", tk).name()
    hl = tk.accent_fill
    common = f"""
QLabel#muted, QLabel[muted="true"] {{ color: {n(11)}; }}
QFrame#card {{ background: {card}; border: 1px solid {border}; border-radius: {T.RADIUS_MD}px; }}
QPushButton[role="primary"], QPushButton[default="true"][role="primary"] {{
    background: {hl.name()}; color: white; border: 1px solid {hl.name()}; }}
QPushButton[role="primary"]:hover {{ background: {tk.accent_fill.lighter(108).name()}; }}
QPushButton[role="destructive"] {{ background: {down}; color: white; border: 1px solid {down}; }}
QPushButton[role="destructive"]:hover {{ background: {down_h}; }}
QPushButton[role="link"] {{ background: transparent; border: none; color: {tk.accent_text.name()};
    padding: 2px 4px; min-width: 0; }}
QPushButton[role="link"]:hover {{ text-decoration: underline; }}
"""
    if native:
        return common
    arrow = _arrow(tk.N(11))
    tick = _tick(QColor("white"))
    tooltip_bg, tooltip_fg = (n(12), n(1)) if not tk.dark else (n(4), n(12))
    return common + f"""
QMainWindow::separator {{ background: {border}; width: 1px; height: 1px; }}
QMainWindow::separator:hover {{ background: {tk.accent_ring.name()}; }}
QDockWidget::title {{ padding: 7px 12px 5px 12px; background: {n(2)}; color: {n(11)};
    border-bottom: 1px solid {border}; text-align: left; }}
QToolBar {{ background: {n(1) if not tk.dark else n(2)}; border: none; border-bottom: 1px solid {border};
    spacing: 2px; padding: 3px 8px; }}
QToolBar::separator {{ background: {border}; width: 1px; margin: 8px 6px; }}
QToolBar QToolButton {{ border: 1px solid transparent; border-radius: {T.RADIUS_SM}px; padding: 4px 8px;
    color: {n(12)}; background: transparent; }}
QToolBar QToolButton:hover {{ background: {_rgba(ink, 0.08)}; }}
QToolBar QToolButton:pressed {{ background: {_rgba(ink, 0.14)}; }}
QToolBar QToolButton:checked {{ background: {_rgba(hl, 0.18)}; border: 1px solid {hl.name()}; }}
QToolBar QToolButton:disabled {{ color: {n(8)}; }}
QToolButton[popupMode="2"] {{ padding-right: 18px; }}
QToolButton::menu-indicator {{ image: url({arrow}); subcontrol-origin: padding;
    subcontrol-position: right center; right: 5px; width: 10px; height: 10px; }}
QToolButton::menu-indicator:pressed, QToolButton::menu-indicator:open {{ top: 0px; left: 0px; }}
QStatusBar {{ background: {n(2)}; color: {n(11)}; border-top: 1px solid {border}; min-height: 24px; }}
QStatusBar::item {{ border: none; }}
QStatusBar QLabel {{ color: {n(11)}; padding: 0 6px; }}
QToolTip {{ background: {tooltip_bg}; color: {tooltip_fg}; border: 1px solid {n(7) if tk.dark else n(12)};
    padding: 5px 8px; border-radius: 6px; }}
QPushButton {{ background: {n(1) if not tk.dark else n(3)}; color: {n(12)}; border: 1px solid {border_strong};
    border-radius: {T.RADIUS_SM}px; padding: 4px 12px; min-height: 18px; }}
QPushButton:hover {{ background: {n(3) if not tk.dark else n(4)}; border-color: {n(8)}; }}
QPushButton:pressed {{ background: {n(4) if not tk.dark else n(5)}; }}
QPushButton:focus {{ border: 1px solid {tk.accent_ring.name()}; }}
QPushButton:disabled {{ color: {n(8)}; background: {n(2) if not tk.dark else n(2)}; border-color: {border}; }}
QPushButton:flat {{ background: transparent; border: none; }}
QPushButton:flat:hover {{ background: {_rgba(ink, 0.08)}; }}
QPushButton:default {{ background: {hl.name()}; color: white; border-color: {hl.name()}; }}
QPushButton:default:hover {{ background: {tk.accent_fill.lighter(108).name()}; }}
QDialogButtonBox QPushButton {{ min-width: 88px; min-height: 20px; }}
QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox, QPlainTextEdit, QTextEdit {{
    background: {n(1) if not tk.dark else n(1)}; color: {n(12)}; border: 1px solid {border_strong};
    border-radius: {T.RADIUS_SM}px; selection-background-color: {hl.name()}; selection-color: white; }}
QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox {{ padding: 4px 8px; min-height: 18px; }}
QLineEdit:focus, QSpinBox:focus, QComboBox:focus, QPlainTextEdit:focus, QTextEdit:focus {{
    border: 1px solid {tk.accent_ring.name()}; }}
QComboBox::drop-down {{ border: none; width: 20px; }}
QComboBox::down-arrow {{ image: url({arrow}); width: 10px; height: 10px; }}
QComboBox QAbstractItemView {{ background: {n(1) if not tk.dark else n(2)}; border: 1px solid {border};
    selection-background-color: {tk.accent_subtle.name()}; selection-color: {n(12)}; outline: none; }}
QTextEdit[readOnly="true"], QPlainTextEdit[readOnly="true"] {{ background: {n(1)}; }}
QMenu {{ background: {n(1) if not tk.dark else n(2)}; color: {n(12)}; border: 1px solid {border_strong};
    padding: 4px; }}
QMenu::item {{ padding: 5px 28px 5px 10px; border-radius: 4px; margin: 0 2px; }}
QMenu::item:selected {{ background: {tk.accent_subtle.name()}; color: {n(12)}; }}
QMenu::item:disabled {{ color: {n(8)}; }}
QMenu::separator {{ height: 1px; background: {border}; margin: 4px 8px; }}
QMenu::icon {{ padding-left: 6px; }}
QMenu::indicator, QCheckBox::indicator {{ width: 14px; height: 14px; border-radius: 3px;
    border: 1px solid {n(8)}; background: {n(1) if not tk.dark else n(3)}; }}
QMenu::indicator {{ margin-left: 6px; }}
QMenu::indicator:checked, QCheckBox::indicator:checked {{ background: {hl.name()}; border-color: {hl.name()};
    image: url({tick}); }}
QMenu::indicator:exclusive {{ border-radius: 7px; }}
QMenu::right-arrow {{ image: url({_arrow(tk.N(11))}); width: 10px; height: 10px; }}
QRadioButton::indicator {{ width: 14px; height: 14px; }}
QTabWidget::pane {{ border: none; border-top: 1px solid {border}; top: -1px; }}
QTabBar {{ qproperty-drawBase: 0; }}
QTabBar::tab {{ background: transparent; color: {n(11)}; padding: 4px 10px; margin: 3px 2px 5px 0;
    border-radius: {T.RADIUS_SM}px; min-height: 18px; }}
QTabBar::tab:hover {{ background: {n(3) if not tk.dark else n(4)}; color: {n(12)}; }}
QTabBar::tab:selected {{ background: {tk.accent_subtle.name()}; color: {tk.accent_ring.name() if not tk.dark else tk.accent_text.name()};
    font-weight: 600; }}
QTabBar QToolButton {{ background: {n(2)}; border: 1px solid {border}; border-radius: 4px; }}
QHeaderView {{ background: transparent; border: none; }}
QHeaderView::section {{ background: {n(2) if not tk.dark else n(2)}; color: {n(11)}; padding: 4px 8px;
    border: none; border-bottom: 1px solid {border}; }}
QTableView, QTableWidget, QTreeView, QListView {{ background: {n(1)}; alternate-background-color: {n(2)};
    gridline-color: {n(4)}; border: none; selection-background-color: {tk.accent_subtle.name()};
    selection-color: {n(12)}; outline: none; }}
QTableView::item, QTreeView::item {{ padding: 2px 6px; }}
QListWidget#palette {{ background: {n(2)}; }}
QListWidget#palette::item {{ border-radius: {T.RADIUS_SM}px; padding: 2px 4px; margin: 1px 6px; color: {n(12)}; }}
QListWidget#palette::item:hover {{ background: {n(3) if not tk.dark else n(4)}; }}
QListWidget#palette::item:selected {{ background: {tk.accent_subtle.name()}; color: {n(12)}; }}
QScrollArea {{ border: none; background: transparent; }}
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 2px; }}
QScrollBar::handle:vertical, QScrollBar::handle:horizontal {{ background: {n(7) if not tk.dark else n(7)};
    border-radius: 3px; min-height: 24px; min-width: 24px; }}
QScrollBar::handle:hover {{ background: {n(8)}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; border: none; background: none; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: none; }}
QSplitter::handle {{ background: {border}; }}
QGroupBox {{ border: 1px solid {border}; border-radius: {T.RADIUS_MD}px; margin-top: 14px; padding: 10px 8px 8px 8px; }}
QGroupBox::title {{ subcontrol-origin: margin; left: 10px; padding: 0 4px; color: {n(11)}; }}
"""


class ThemeController(QObject):
    """One per app.  ``changed`` fires after the palette, QSS, tokens and icon cache were updated."""

    changed = Signal()

    def __init__(self, app: QApplication | None = None, settings=None, mode: str | None = None):
        super().__init__(app)
        self.app = app or QApplication.instance()
        self.settings = settings if settings is not None else app_settings()
        self.mode = mode or str(self.settings.value("ui/theme", "auto"))
        if self.mode not in dict(MODES):
            self.mode = "auto"
        self._busy = False
        self._sys_pal = QPalette(self.app.palette())
        self.app.setStyle("Fusion")
        hints = self.app.styleHints()
        try:
            hints.colorSchemeChanged.connect(self._scheme_changed)
        except AttributeError:  # pragma: no cover
            pass
        self.app.installEventFilter(self)
        self.apply()

    # ------------------------------------------------------------------ mode
    def set_mode(self, mode: str) -> None:
        if mode not in dict(MODES):
            return
        self.mode = mode
        self.settings.setValue("ui/theme", mode)
        self.apply()

    def system_dark(self) -> bool:
        try:
            cs = self.app.styleHints().colorScheme()
            if cs == Qt.ColorScheme.Dark:
                return True
            if cs == Qt.ColorScheme.Light:
                return False
        except AttributeError:  # pragma: no cover
            pass
        return self._sys_pal.window().color().lightness() < 128

    def is_dark(self) -> bool:
        if self.mode == "light":
            return False
        if self.mode == "dark":
            return True
        return self.system_dark()

    # ------------------------------------------------------------------ apply
    def apply(self) -> None:
        if self._busy:
            return
        self._busy = True
        try:
            hints = self.app.styleHints()
            want = {"light": Qt.ColorScheme.Light, "dark": Qt.ColorScheme.Dark}.get(self.mode, Qt.ColorScheme.Unknown)
            try:
                if hints.colorScheme() != want and self.mode in ("light", "dark"):
                    hints.setColorScheme(want)
                elif self.mode in ("auto", "kde"):
                    hints.unsetColorScheme()
            except AttributeError:  # pragma: no cover (Qt < 6.8)
                pass
            dark = self.is_dark()
            if self.mode == "kde":
                tk = T.build_tokens(self._sys_pal.window().color().lightness() < 128, self._sys_pal, "kde")
                self.app.setPalette(self._sys_pal)
            else:
                tk = T.build_tokens(dark, None, "dark" if dark else "light")
                self.app.setPalette(build_palette(tk))
            T.set_current(tk)
            icons.set_theme_mode(self.mode)
            from . import shellstyle
            native = self.mode == "kde"
            self.app.setStyleSheet(build_qss(tk, native=native) + shellstyle.qss(tk, native))
        finally:
            self._busy = False
        self.changed.emit()

    def _scheme_changed(self, *_):
        if self._busy:
            return
        if self.mode in ("auto", "kde"):
            self.apply()

    def eventFilter(self, obj, ev):
        if ev.type() == QEvent.ApplicationPaletteChange and obj is self.app and not self._busy:
            # the platform theme pushed a new palette (KDE scheme switch): remember it, then re-apply ours
            self._sys_pal = QPalette(self.app.palette())
            self.apply()
        return False


_CONTROLLER: list = [None]


def controller() -> ThemeController | None:
    return _CONTROLLER[0]


def install(app: QApplication | None = None, settings=None, mode: str | None = None) -> ThemeController:
    """Create (or return) the app's ThemeController."""
    if _CONTROLLER[0] is None or _CONTROLLER[0].app is not (app or QApplication.instance()):
        _CONTROLLER[0] = ThemeController(app, settings, mode)
    elif mode:
        _CONTROLLER[0].set_mode(mode)
    return _CONTROLLER[0]
