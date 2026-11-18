"""Entry point: python -m labdesk_app [--lab TITLE | --lab-id ID]"""
from __future__ import annotations

import argparse
import os
import signal
import sys

from PySide6.QtCore import QTimer
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication

from . import config, plat
from .ui.main_window import MainWindow


def _windowed_exe_streams() -> None:
    """LabDesk.exe is a windowed app: sys.stdout / sys.stderr are None there.  Send them to a log file
    (%LOCALAPPDATA%\\LabDesk\\cache\\labdesk.log) so tracebacks are kept instead of lost."""
    if sys.stdout is not None and sys.stderr is not None:
        return
    try:
        config.CACHE_DIR.mkdir(parents=True, exist_ok=True)
        log = open(config.CACHE_DIR / "labdesk.log", "a", encoding="utf-8", buffering=1)  # noqa: SIM115
    except OSError:
        log = open(os.devnull, "w", encoding="utf-8")  # noqa: SIM115
    if sys.stdout is None:
        sys.stdout = log
    if sys.stderr is None:
        sys.stderr = log


def _smoke_exit(app: QApplication, win: MainWindow) -> None:
    """CI smoke test: LABDESK_SMOKE_EXIT_MS=<ms> quits after that long and writes 'ok <title>' to
    LABDESK_SMOKE_FILE (proves the frozen exe imports everything and builds the main window)."""
    ms = os.environ.get("LABDESK_SMOKE_EXIT_MS")
    if not ms:
        return

    def done():
        out = os.environ.get("LABDESK_SMOKE_FILE")
        if out:
            with open(out, "w", encoding="utf-8") as fh:
                fh.write(f"ok {win.windowTitle()} {QApplication.platformName()}\n")
        win.close()
        app.quit()
    QTimer.singleShot(int(ms), done)


def main(argv=None) -> int:
    _windowed_exe_streams()
    ap = argparse.ArgumentParser(prog="labdesk", description="LabDesk - CML lab desktop")
    ap.add_argument("--lab", help="open the CML lab with this title")
    ap.add_argument("--lab-id", help="open the CML lab with this id")
    ap.add_argument("--setup", action="store_true", help="show the CML connection dialog first")
    args, qt_args = ap.parse_known_args(argv)

    if plat.IS_WINDOWS:
        try:                                  # own taskbar group + icon instead of python.exe's
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("LabDesk.LabDesk")
        except (AttributeError, OSError):
            pass
    QApplication.setStyle("Fusion")
    app = QApplication([sys.argv[0], *qt_args])
    app.setApplicationName("LabDesk")
    app.setDesktopFileName("labdesk")
    app.setWindowIcon(QIcon(str(config.ICON_PATH)))
    from .ui import themectl
    themectl.install(app)                 # Fusion + LabDesk palette / QSS / tokens before any widget exists
    signal.signal(signal.SIGINT, signal.SIG_DFL)

    smoke = bool(os.environ.get("LABDESK_SMOKE_EXIT_MS"))
    if (args.setup or config.needs_setup()) and not smoke:
        # first run (no credentials file / no CML password; always on a fresh Windows install)
        from .ui.connection_dialog import CmlConnectionDialog
        dlg = CmlConnectionDialog(None, first_run=True)
        dlg.exec()
        config.reload_cml_host()

    win = MainWindow()
    win.show()
    _smoke_exit(app, win)
    if args.lab_id:
        win.open_lab_id(args.lab_id)
    elif args.lab:
        win.open_lab_title(args.lab)
    elif win.ctl.state.last_lab:
        win.open_lab_id(win.ctl.state.last_lab)
    QTimer.singleShot(0, win.poll)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
