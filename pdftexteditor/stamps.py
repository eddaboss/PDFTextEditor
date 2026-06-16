"""Procedural stamp PNGs (images & signatures §6): APPROVED / DRAFT /
CONFIDENTIAL / COMPLETED / VOID rendered on demand -- no bundled image
assets (packaging §11), so ``collect_submodules`` alone ships them.

``stamp_png(text)`` paints the classic rubber-stamp look (rounded-rect
border + bold uppercase text, transparent background) onto an ARGB32
QImage at 3x supersampling and returns PNG bytes; the placement pipeline
treats the result exactly like any imported image (the RGBA stream gets
an auto-SMask on ``insert_image``, so the page shows through everywhere
but the ink).
"""

from __future__ import annotations

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QRectF, Qt
from PySide6.QtGui import QColor, QFontMetricsF, QImage, QPainter, QPen

from .ui import theme

# The five stamp kinds (§4), in menu order.
STAMP_KINDS = ("APPROVED", "DRAFT", "CONFIDENTIAL", "COMPLETED", "VOID")

# Geometry at 1x, all scaled by _SCALE for the supersampled render. The text
# size is chosen so the default placement width (min(natural_px * 72/96,
# 45% of the page) -- page_view._default_image_rect) lands a letter-page
# stamp around 240-275 pt wide, the Acrobat ballpark.
_SCALE = 3
_TEXT_PX = 30
_PAD_X = 16
_PAD_Y = 7
_BORDER = 3        # the §6 3px rounded-rect border
_RADIUS = 6


def stamp_png(text: str, color: str = "#B91C1C") -> bytes:
    """Render ``text`` (uppercased) as a transparent stamp PNG.

    Requires a constructed ``QGuiApplication`` (QPainter text rendering
    resolves fonts through Qt); raises ``RuntimeError`` with a clear message
    rather than letting QFontDatabase abort the process (the
    ``_baked_copy`` guard discipline).
    """
    from PySide6.QtGui import QGuiApplication
    if QGuiApplication.instance() is None:
        raise RuntimeError(
            "stamp_png requires a QGuiApplication: QPainter text rendering "
            "uses the Qt font stack. Construct a QApplication first.")
    label = (text or "").upper()
    font = theme.ui_font(_TEXT_PX * _SCALE, semibold=True)
    metrics = QFontMetricsF(font)
    text_w = metrics.horizontalAdvance(label)
    text_h = metrics.height()
    border = _BORDER * _SCALE
    w = int(text_w + 2 * _PAD_X * _SCALE + 2 * border) + 1
    h = int(text_h + 2 * _PAD_Y * _SCALE + 2 * border) + 1

    img = QImage(w, h, QImage.Format_ARGB32_Premultiplied)
    img.fill(Qt.transparent)
    painter = QPainter(img)
    painter.setRenderHint(QPainter.Antialiasing, True)
    painter.setRenderHint(QPainter.TextAntialiasing, True)
    ink = QColor(color)
    pen = QPen(ink)
    pen.setWidthF(border)
    painter.setPen(pen)
    painter.setBrush(Qt.NoBrush)
    inset = border / 2.0 + 0.5
    painter.drawRoundedRect(
        QRectF(inset, inset, w - 2 * inset, h - 2 * inset),
        _RADIUS * _SCALE, _RADIUS * _SCALE)
    painter.setFont(font)
    painter.drawText(QRectF(0, 0, w, h), Qt.AlignCenter, label)
    painter.end()

    data = QByteArray()
    buf = QBuffer(data)
    buf.open(QIODevice.WriteOnly)
    img.save(buf, "PNG")
    buf.close()
    return bytes(data)
