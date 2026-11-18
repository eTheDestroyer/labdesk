"""Entry point: python -m labdesk_app [--lab TITLE | --lab-id ID]"""
from __future__ import annotations

import argparse
import signal
import sys

from PySide6.QtCore import QTimer
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication

from . import config
from .ui.main_window import MainWindow


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="labdesk", description="LabDesk - CML lab desktop")
    ap.add_argument("--lab", help="open the CML lab with this title")
    ap.add_argument("--lab-id", help="open the CML lab with this id")
    args, qt_args = ap.parse_known_args(argv)

    QApplication.setStyle("Fusion")
    app = QApplication([sys.argv[0], *qt_args])
    app.setApplicationName("LabDesk")
    app.setDesktopFileName("labdesk")
    app.setWindowIcon(QIcon(str(config.ICON_PATH)))
    from .ui import themectl
    themectl.install(app)                 # Fusion + LabDesk palette / QSS / tokens before any widget exists
    signal.signal(signal.SIGINT, signal.SIG_DFL)

    win = MainWindow()
    win.show()
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
