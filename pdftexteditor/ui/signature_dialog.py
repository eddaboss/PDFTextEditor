"""Draw-a-signature dialog (images & signatures §6): mouse strokes on a
white card, smoothed into a transparent PNG for placement and the
persistent library.

PURE CHROME, the doc_dialogs discipline: the dialog never touches the
model or the library -- ``MainWindow._do_draw_signature`` reads
``result_png()`` / ``signature_name()`` / ``save_to_library()`` after
accept and does the rest. The window reaches it only through the
``_signature_dialog_factory`` seam, so offscreen tests swap in a stub and
no ``exec()`` ever runs headless; the dialog itself is also drivable
non-modally (``show()`` + injected strokes + ``accept()``).

``strokes_to_png`` is the module-level PURE export core (the headless test
surface): plain ``[(x, y), ...]`` stroke lists in, PNG bytes out -- no
widget required.
"""

from __future__ import annotations

import math

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QImage, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from . import theme

# Drawing surface size (§6: ~620x220) -- wide and short, like a signature
# line on an agreement.
_CANVAS_W, _CANVAS_H = 620, 220

# Pen colors offered by the dialog (§6: Black / Blue). Near-black reads as
# real ink; the blue is a classic ballpoint shade.
PEN_COLORS = (("Black", "#1A1A1A"), ("Blue", "#1D4ED8"))

# Export margin around the ink, in OUTPUT pixels (§6: crop to ink bbox +
# 8px margin).
_CROP_MARGIN_PX = 8.0


def _stroke_paths(strokes) -> list[QPainterPath]:
    """One smoothed ``QPainterPath`` per stroke -- midpoint quadratic
    smoothing (``quadTo(p[i], mid(p[i], p[i+1]))``), the §6 recipe. Shared
    by the live canvas paint and the PNG export so what you draw is exactly
    what places. Single-point strokes become a dot (a zero-length segment
    under a round-cap pen)."""
    paths: list[QPainterPath] = []
    for stroke in strokes or ():
        pts = [QPointF(float(x), float(y)) for x, y in stroke]
        if not pts:
            continue
        path = QPainterPath(pts[0])
        if len(pts) == 1:
            path.lineTo(pts[0].x() + 0.01, pts[0].y())
        elif len(pts) == 2:
            path.lineTo(pts[1])
        else:
            for i in range(1, len(pts) - 1):
                mid = QPointF((pts[i].x() + pts[i + 1].x()) / 2.0,
                              (pts[i].y() + pts[i + 1].y()) / 2.0)
                path.quadTo(pts[i], mid)
            path.lineTo(pts[-1])
        paths.append(path)
    return paths


def strokes_to_png(strokes, pen_width: float = 3.0,
                   color: QColor | None = None, scale: int = 3) -> bytes:
    """PURE export core (§6): smoothed strokes -> transparent PNG bytes at
    ``scale``x supersampling, cropped to the ink bbox + 8px margin. Returns
    ``b""`` for empty/blank input (the caller's no-op signal). Needs only a
    QGuiApplication (QImage painting), never a widget."""
    paths = _stroke_paths(strokes)
    if not paths:
        return b""
    bound = QRectF(paths[0].boundingRect())
    for p in paths[1:]:
        bound = bound.united(p.boundingRect())
    # Pad by the pen's radius plus the crop margin (converted into stroke
    # space) so round caps and the 8px breathing room survive the crop.
    pad = pen_width / 2.0 + _CROP_MARGIN_PX / float(scale)
    bound = bound.adjusted(-pad, -pad, pad, pad)
    w = max(1, int(math.ceil(bound.width() * scale)))
    h = max(1, int(math.ceil(bound.height() * scale)))

    img = QImage(w, h, QImage.Format_ARGB32_Premultiplied)
    img.fill(Qt.transparent)
    painter = QPainter(img)
    painter.setRenderHint(QPainter.Antialiasing, True)
    painter.scale(scale, scale)
    painter.translate(-bound.left(), -bound.top())
    pen = QPen(QColor(color) if color is not None else QColor(26, 26, 26))
    pen.setWidthF(pen_width)
    pen.setCapStyle(Qt.RoundCap)
    pen.setJoinStyle(Qt.RoundJoin)
    painter.setPen(pen)
    painter.setBrush(Qt.NoBrush)
    for p in paths:
        painter.drawPath(p)
    painter.end()

    data = QByteArray()
    buf = QBuffer(data)
    buf.open(QIODevice.WriteOnly)
    img.save(buf, "PNG")
    buf.close()
    return bytes(data)


class _SignatureCanvas(QWidget):
    """The drawing surface: a white card with a light baseline rule.
    Mouse press starts a stroke, moves append points, release ends it --
    ``strokes`` is PLAIN data (``[[(x, y), ...], ...]``, §6) so tests
    inject and read it without synthesizing events. A loaded PNG
    (Signature from File inside the dialog) shows fitted on the card and
    is dropped the moment the user draws again."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("SignatureCanvas")
        self.setFixedSize(_CANVAS_W, _CANVAS_H)
        self.setCursor(Qt.CrossCursor)
        self.strokes: list[list[tuple[float, float]]] = []
        self.loaded_png: bytes | None = None
        self.pen_width = 3.0
        self.pen_color = QColor(PEN_COLORS[0][1])
        self._drawing = False

    # -- input ----------------------------------------------------------
    def mousePressEvent(self, event):
        if event.button() != Qt.LeftButton:
            return super().mousePressEvent(event)
        if self.loaded_png is not None:
            self.loaded_png = None        # drawing replaces a loaded file
        self._drawing = True
        pos = event.position()
        self.strokes.append([(pos.x(), pos.y())])
        self.update()

    def mouseMoveEvent(self, event):
        if not self._drawing or not self.strokes:
            return super().mouseMoveEvent(event)
        pos = event.position()
        self.strokes[-1].append((pos.x(), pos.y()))
        self.update()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton and self._drawing:
            self._drawing = False
            self.update()
            return
        super().mouseReleaseEvent(event)

    # -- state ----------------------------------------------------------
    def clear(self) -> None:
        self.strokes = []
        self.loaded_png = None
        self._drawing = False
        self.update()

    def set_loaded_png(self, data: bytes) -> None:
        """Show a file-loaded signature instead of strokes (Clear or a new
        stroke discards it)."""
        self.loaded_png = data
        self.strokes = []
        self._drawing = False
        self.update()

    # -- paint ----------------------------------------------------------
    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        card = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        painter.setPen(QPen(QColor(theme.CHROME_BORDER)))
        painter.setBrush(QColor("#FFFFFF"))
        painter.drawRoundedRect(card, 8, 8)
        # The baseline rule a signature naturally sits on.
        baseline_y = self.height() * 0.72
        rule = QPen(QColor(0, 0, 0, 28))
        rule.setWidthF(1.0)
        painter.setPen(rule)
        painter.drawLine(QPointF(24, baseline_y),
                         QPointF(self.width() - 24, baseline_y))
        if self.loaded_png is not None:
            img = QImage.fromData(self.loaded_png)
            if not img.isNull():
                target = QRectF(self.rect()).adjusted(16, 16, -16, -16)
                scaled = img.scaled(
                    int(target.width()), int(target.height()),
                    Qt.KeepAspectRatio, Qt.SmoothTransformation)
                painter.drawImage(QPointF(
                    target.left() + (target.width() - scaled.width()) / 2,
                    target.top() + (target.height() - scaled.height()) / 2),
                    scaled)
            painter.end()
            return
        pen = QPen(self.pen_color)
        pen.setWidthF(self.pen_width)
        pen.setCapStyle(Qt.RoundCap)
        pen.setJoinStyle(Qt.RoundJoin)
        painter.setPen(pen)
        painter.setBrush(Qt.NoBrush)
        for path in _stroke_paths(self.strokes):
            painter.drawPath(path)
        painter.end()


class SignatureDialog(QDialog):
    """Tools > Signature > Draw Signature… (§6). Accept = "Use Signature":
    the window reads ``result_png()`` (the smoothed transparent PNG, or the
    loaded file's bytes verbatim) plus ``signature_name()`` /
    ``save_to_library()`` for the optional library save."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("SignatureDialog")
        self.setWindowTitle("Draw Signature")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 18, 20, 16)
        lay.setSpacing(12)

        header = QLabel("DRAW SIGNATURE")
        header.setObjectName("InspectorSectionHeader")
        header.setFont(theme.ui_font(11, semibold=True))
        lay.addWidget(header)

        self.canvas = _SignatureCanvas(self)
        lay.addWidget(self.canvas)

        # Pen controls + canvas actions in one row.
        controls = QHBoxLayout()
        controls.setSpacing(8)
        controls.addWidget(QLabel("Pen"))
        self.pen_spin = QSpinBox()
        self.pen_spin.setObjectName("SignaturePenWidth")
        self.pen_spin.setRange(2, 6)
        self.pen_spin.setValue(3)
        self.pen_spin.valueChanged.connect(self._sync_pen)
        controls.addWidget(self.pen_spin)
        self.color_combo = QComboBox()
        self.color_combo.setObjectName("SignatureColor")
        for label, _hexv in PEN_COLORS:
            self.color_combo.addItem(label)
        self.color_combo.currentIndexChanged.connect(self._sync_pen)
        controls.addWidget(self.color_combo)
        controls.addStretch(1)
        self.clear_button = QPushButton("Clear")
        self.clear_button.setObjectName("SignatureClear")
        self.clear_button.clicked.connect(self.canvas.clear)
        controls.addWidget(self.clear_button)
        self.load_button = QPushButton("Load from file…")
        self.load_button.setObjectName("SignatureLoad")
        self.load_button.clicked.connect(self._load_from_file)
        controls.addWidget(self.load_button)
        lay.addLayout(controls)

        # Name + library save.
        name_row = QHBoxLayout()
        name_row.setSpacing(8)
        name_row.addWidget(QLabel("Name"))
        self.name_field = QLineEdit()
        self.name_field.setObjectName("SignatureName")
        self.name_field.setPlaceholderText("Signature name")
        name_row.addWidget(self.name_field, 1)
        self.save_check = QCheckBox("Save to library")
        self.save_check.setObjectName("SignatureSaveCheck")
        self.save_check.setChecked(True)
        name_row.addWidget(self.save_check)
        lay.addLayout(name_row)

        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("Use Signature")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)
        self._sync_pen()

    # -- controls -> canvas ----------------------------------------------
    def _sync_pen(self) -> None:
        self.canvas.pen_width = float(self.pen_spin.value())
        self.canvas.pen_color = QColor(
            PEN_COLORS[self.color_combo.currentIndex()][1])
        self.canvas.update()

    def _load_from_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Load Signature Image", "", "Images (*.png *.jpg *.jpeg)")
        if not path:
            return
        try:
            with open(path, "rb") as fh:
                data = fh.read()
        except OSError:
            return
        if QImage.fromData(data).isNull():
            return
        self.canvas.set_loaded_png(data)

    # -- results the window reads after accept ---------------------------
    def result_png(self) -> bytes:
        """The signature PNG: a loaded file's bytes verbatim (already a
        valid image; placement never recodes), else the smoothed strokes
        export. ``b""`` when blank -- the window's no-op signal."""
        if self.canvas.loaded_png is not None:
            return self.canvas.loaded_png
        return strokes_to_png(self.canvas.strokes,
                              pen_width=float(self.pen_spin.value()),
                              color=QColor(
                                  PEN_COLORS[self.color_combo.currentIndex()][1]))

    def signature_name(self) -> str:
        return self.name_field.text().strip()

    def save_to_library(self) -> bool:
        return self.save_check.isChecked()


def signature_menu_icon(png_bytes_or_path) -> QPixmap:
    """A 32px menu thumbnail for a library entry (§4): the PNG scaled to
    fit, transparency preserved. Accepts bytes or a path."""
    if isinstance(png_bytes_or_path, (bytes, bytearray)):
        pm = QPixmap()
        pm.loadFromData(bytes(png_bytes_or_path))
    else:
        pm = QPixmap(png_bytes_or_path)
    if pm.isNull():
        return QPixmap()
    return pm.scaled(32, 32, Qt.KeepAspectRatio, Qt.SmoothTransformation)
