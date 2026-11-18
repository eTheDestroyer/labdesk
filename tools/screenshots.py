#!/usr/bin/env python3
"""Offscreen QA: launch the real MainWindow, optionally open a lab, save screenshots.

QT_QPA_PLATFORM=offscreen .venv/bin/python tools/screenshots.py [--lab TITLE] [--select R1] [--prefix name]
"""
import argparse
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import tempfile as _tempfile  # noqa: E402
os.environ["XDG_CONFIG_HOME"] = _tempfile.mkdtemp(prefix="labdesk-qa-")   # L1: never touch ~/.config/LabDesk

from PySide6.QtCore import QEventLoop, QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from labdesk_app import config  # noqa: E402
from labdesk_app.ui.dialogs import ConfigDialog, OpenLabDialog, PropertiesDialog  # noqa: E402
from labdesk_app.ui.main_window import MainWindow  # noqa: E402

QA = config.APP_DIR / "qa"


def pump(app, secs):
    end = time.time() + secs
    while time.time() < end:
        app.processEvents(QEventLoop.AllEvents, 50)
        time.sleep(0.02)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--lab")
    ap.add_argument("--select")
    ap.add_argument("--prefix", default="main")
    ap.add_argument("--dialogs", action="store_true")
    a = ap.parse_args()
    QA.mkdir(exist_ok=True)
    app = QApplication(sys.argv[:1])
    win = MainWindow()
    win.resize(1360, 860)
    win.show()
    if a.lab:
        win.open_lab_title(a.lab)
    win.poll()
    pump(app, 6)
    if a.select:
        for item in win.scene.nodes.values():
            if item.label == a.select:
                item.setSelected(True)
        pump(app, 0.5)
    win.grab().save(str(QA / f"{a.prefix}.png"))
    print("saved", QA / f"{a.prefix}.png")
    if a.dialogs:
        labs = win.ctl.list_labs()
        d = OpenLabDialog(labs, win)
        d.show(); pump(app, 0.3); d.grab().save(str(QA / "dialog_open.png")); d.close()
        d.tabs.setCurrentIndex(1)
        d.show(); pump(app, 0.3); d.grab().save(str(QA / "dialog_templates.png")); d.close()
        key = win.selected_key()
        if key:
            d = ConfigDialog(a.select, win.ctl.get_config(key), win)
            d.show(); pump(app, 0.3); d.grab().save(str(QA / "dialog_config.png")); d.close()
            d = PropertiesDialog(a.select, {"Name": a.select, "Type": "Cisco IOSv router", "Backend": "CML",
                                            "ID": key[4:]}, {"ip": "", "user": "admin"}, win)
            d.show(); pump(app, 0.3); d.grab().save(str(QA / "dialog_properties.png")); d.close()
        print("saved dialogs")
    win.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
