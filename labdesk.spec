# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller build of LabDesk for Windows: one windowed LabDesk.exe (no console window).

    pip install -r requirements.txt pyinstaller
    pyinstaller --noconfirm --clean labdesk.spec          ->  dist/LabDesk.exe

Run-time data (templates, the app icon, the UI icon set and an optional BUILD_INFO written by CI) is
unpacked next to the code (sys._MEIPASS), at the same relative paths as in the repo, so
config.APP_DIR / PKG_DIR work unchanged.  Built by .github/workflows/windows.yml.
"""
import os

ROOT = os.path.abspath(SPECPATH)

datas = [
    (os.path.join(ROOT, "templates"), "templates"),
    (os.path.join(ROOT, "labdesk.svg"), "."),
    (os.path.join(ROOT, "labdesk_app", "ui", "icons"), os.path.join("labdesk_app", "ui", "icons")),
    (os.path.join(ROOT, "labdesk_app", "rooms"), os.path.join("labdesk_app", "rooms")),
]
if os.path.exists(os.path.join(ROOT, "BUILD_INFO")):
    datas.append((os.path.join(ROOT, "BUILD_INFO"), "."))

# Qt / Python modules LabDesk never imports (keeps the exe small)
excludes = [
    "tkinter", "unittest", "pydoc_data", "pytest", "_pytest", "pexpect", "ptyprocess",
    "pyte", "wcwidth",          # the LabDesk console window is Linux only (Windows keeps Windows Terminal)
    "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.QtWebEngineQuick", "PySide6.QtWebChannel",
    "PySide6.QtWebSockets", "PySide6.QtQml", "PySide6.QtQuick", "PySide6.QtQuickWidgets", "PySide6.QtQuick3D",
    "PySide6.Qt3DCore", "PySide6.Qt3DRender", "PySide6.QtMultimedia", "PySide6.QtMultimediaWidgets",
    "PySide6.QtPdf", "PySide6.QtPdfWidgets", "PySide6.QtCharts", "PySide6.QtDataVisualization", "PySide6.QtGraphs",
    "PySide6.QtBluetooth", "PySide6.QtNfc", "PySide6.QtPositioning", "PySide6.QtLocation", "PySide6.QtSensors",
    "PySide6.QtSerialPort", "PySide6.QtSql", "PySide6.QtTest", "PySide6.QtDesigner", "PySide6.QtHelp",
    "PySide6.QtOpenGLWidgets", "PySide6.QtPrintSupport", "PySide6.QtRemoteObjects",
    "PySide6.QtScxml", "PySide6.QtSpatialAudio", "PySide6.QtTextToSpeech", "PySide6.QtUiTools",
    "PySide6.QtXml", "PySide6.QtConcurrent", "PySide6.QtHttpServer", "PySide6.QtNetworkAuth",
]

a = Analysis(
    [os.path.join(ROOT, "packaging", "labdesk_entry.py")],
    pathex=[ROOT],
    binaries=[],
    datas=datas,
    hiddenimports=["paramiko", "psutil", "yaml", "PySide6.QtSvg"],
    hookspath=[],
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [("X utf8", None, "OPTION")],      # Python UTF-8 mode: files are UTF-8 whatever the Windows code page is
    name="LabDesk",
    icon=os.path.join(ROOT, "packaging", "labdesk.ico"),
    console=False,              # windowed app: no console window behind LabDesk
    disable_windowed_traceback=False,
    debug=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
)
