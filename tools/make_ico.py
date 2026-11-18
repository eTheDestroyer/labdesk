#!/usr/bin/env python3
"""Render labdesk.svg into a multi-size Windows icon (packaging/labdesk.ico) with Qt only (no Pillow).

    QT_QPA_PLATFORM=offscreen .venv/bin/python tools/make_ico.py

ICO = 6-byte header + one 16-byte directory entry per size + PNG-compressed images (Vista+ format)."""
from __future__ import annotations

import struct
import sys
from pathlib import Path

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, Qt
from PySide6.QtGui import QGuiApplication, QImage, QPainter
from PySide6.QtSvg import QSvgRenderer

ROOT = Path(__file__).resolve().parent.parent
SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)


def png(renderer: QSvgRenderer, size: int) -> bytes:
    img = QImage(size, size, QImage.Format_ARGB32_Premultiplied)
    img.fill(Qt.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.Antialiasing)
    p.setRenderHint(QPainter.SmoothPixmapTransform)
    renderer.render(p)
    p.end()
    ba = QByteArray()
    buf = QBuffer(ba)
    buf.open(QIODevice.WriteOnly)
    img.save(buf, "PNG")
    return bytes(ba)


def main(out: Path = ROOT / "packaging" / "labdesk.ico") -> Path:
    _app = QGuiApplication.instance() or QGuiApplication(sys.argv[:1])
    r = QSvgRenderer(str(ROOT / "labdesk.svg"))
    images = [(s, png(r, s)) for s in SIZES]
    head = struct.pack("<HHH", 0, 1, len(images))
    offset = len(head) + 16 * len(images)
    entries, blobs = b"", b""
    for s, data in images:
        dim = 0 if s >= 256 else s                     # 0 means 256
        entries += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(data), offset)
        offset += len(data)
        blobs += data
    out.write_bytes(head + entries + blobs)
    return out


if __name__ == "__main__":
    print(main())
