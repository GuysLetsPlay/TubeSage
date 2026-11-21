"""Build TubeSage's app icon (tubesage.ico) with PySide6 — no PIL needed."""
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtCore import QBuffer, QIODevice, QPointF  # noqa: E402
from PySide6.QtGui import (QColor, QIcon, QLinearGradient, QPainter, QPen,
                           QPixmap, QPolygonF)  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
app = QApplication(sys.argv)


def draw(size: int) -> QPixmap:
    pm = QPixmap(size, size)
    pm.fill(QColor(0, 0, 0, 0))
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    m = max(2, size // 16)                      # margin
    r = size // 5                               # corner radius
    grad = QLinearGradient(0, 0, 0, size)
    grad.setColorAt(0.0, QColor("#4a9eff"))
    grad.setColorAt(1.0, QColor("#1c5fb8"))
    p.setPen(QPen(QColor("#0e3a75"), max(1, size // 64)))
    p.setBrush(grad)
    p.drawRoundedRect(m, m, size - 2 * m, size - 2 * m, r, r)
    # white play triangle, optically centered
    c = size / 2.0
    s = size * 0.42
    tri = QPolygonF([QPointF(c - s * 0.32, c - s / 2),
                     QPointF(c - s * 0.32, c + s / 2),
                     QPointF(c + s * 0.45, c)])
    p.setPen(QPen(QColor("#ffffff"), max(1, size // 96)))
    p.setBrush(QColor("#ffffff"))
    p.drawPolygon(tri)
    p.end()
    return pm


def png_bytes(size: int) -> bytes:
    buf = QBuffer()
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    draw(size).save(buf, "PNG")
    return bytes(buf.data())


# ---- wrap the PNGs into a single .ico (PNG entries, Vista+) ----
sizes = (16, 24, 32, 48, 64, 128, 256)
png_data = {s: png_bytes(s) for s in sizes}
ico_path = os.path.join(BASE, "tubesage.ico")
with open(ico_path, "wb") as f:
    f.write(struct.pack("<HHH", 0, 1, len(sizes)))       # ICONDIR
    offset = 6 + 16 * len(sizes)
    for s in sizes:
        data = png_data[s]
        f.write(struct.pack("<BBBBHHII",
                            s % 256, s % 256, 0, 0, 1, 32,
                            len(data), offset))
        offset += len(data)
    for s in sizes:
        f.write(png_data[s])
print("icon written:", ico_path, os.path.getsize(ico_path), "bytes")
assert QIcon(ico_path).pixmap(64, 64).width() == 64, "ico unreadable"
print("ICO-OK")

