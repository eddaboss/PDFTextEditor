"""Main window chrome: toolbar, canvas, status bar, undo/redo + dirty wiring.

A polished ``QMainWindow`` built to BUILD_SPEC §6. It owns:

* a real top toolbar (Open, split Save/Save As, Undo, Redo, page nav + indicator,
  zoom out/in + a zoom-% menu), with flat rounded macOS-style tool buttons;
* a neutral-gray ``CanvasContainer`` that hosts the ``PageView`` and accepts
  PDF drops, with a centered empty-state overlay shown while no document is open;
* a 28px status bar (filename, dirty dot, edit count, font chip during an edit,
  clickable zoom %);
* a ``QUndoStack`` whose commands delegate to the document model, wired to the
  Undo/Redo actions and to dirty/clean state (window-modified marker, Save
  enablement, close/quit guard);
* full keyboard shortcuts via ``QAction`` + ``QKeySequence``.

It imports ``PageView`` from ``page_view`` and uses ``PDFDocument`` from
``document``. The font-resolution fidelity contract lives in the model + canvas;
this module is pure chrome and state wiring.

Integration note: the live ``PageView`` API is still settling. This window codes
to the spec's §5.2 signal/method surface but connects defensively (only to
signals/methods the running ``PageView`` actually exposes) so it constructs and
runs against either the spec-compliant canvas or the current MVP. See the
``# ASSUMPTION`` comments and the module docstring in code review.
"""

from __future__ import annotations

import json
import os
import time
from contextlib import contextmanager

from PySide6.QtCore import (
    QEvent,
    QPoint,
    QPropertyAnimation,
    QRectF,
    QSize,
    QStandardPaths,
    Qt,
    QTimer,
    QUrl,
    Signal,
)
from PySide6.QtGui import (
    QAction,
    QBrush,
    QColor,
    QCursor,
    QDesktopServices,
    QGuiApplication,
    QIcon,
    QImage,
    QIntValidator,
    QKeySequence,
    QPageLayout,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QUndoCommand,
    QUndoStack,
)
# Imported at module top (not lazily in _do_print) so PyInstaller's PySide6
# hooks bundle QtPrintSupport into the app (ws6 §2.9; build_app.sh verifies).
from PySide6.QtPrintSupport import QPrintDialog, QPrinter
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QDockWidget,
    QFileDialog,
    QFrame,
    QGraphicsDropShadowEffect,
    QGraphicsOpacityEffect,
    QGridLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMenuBar,
    QMessageBox,
    QPlainTextEdit,
    QProgressDialog,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QStatusBar,
    QTabBar,
    QToolBar,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from .. import __version__, doctools, stamps
from ..document import (
    PDF_ENCRYPT_AES_256,
    PDF_ENCRYPT_NONE,
    PasswordRequired,
    PDFDocument,
)
from ..font_engine import FontEngine
from ..signatures import SignatureLibrary
from ..workspace import Workspace
from . import theme
from .bookmark_panel import BookmarkPanel
from .comments_panel import CommentsPanel
from .doc_dialogs import (
    ExportImagesOptions,
    HeaderFooterOptions,
    SecurityOptions,
    WatermarkOptions,
    _CropDialog,
    _ExportImagesDialog,
    _HeaderFooterDialog,
    _PropertiesDialog,
    _SecurityDialog,
    _WatermarkDialog,
)
from .find_panel import FindReplacePanel
from .inspector import Inspector
from .thumbnail_cache import ThumbnailLoader
from .left_panel import LeftPanel
from .page_view import PageView
from .shortcuts_dialog import AboutDialog, ShortcutsDialog
from .signature_dialog import SignatureDialog, signature_menu_icon
from .thumbnail_sidebar import PageThumbnailSidebar

# Zoom step + clamp (BUILD_SPEC §6.2).
ZOOM_STEP = 1.25
ZOOM_MIN = 0.25
ZOOM_MAX = 6.0
# Preset percentages for the zoom menu.
ZOOM_PRESETS = (0.50, 0.75, 1.00, 1.25, 1.50, 2.00, 4.00)


# ===========================================================================
# Icon factory: vector glyphs drawn with QPainter.
# ===========================================================================
# BUILD_SPEC §6.2 calls for SF Symbols rendered to QIcon, falling back to
# bundled SVGs in assets/icons/. SF Symbols are not exposed as a usable Qt font
# family (verified: QFontDatabase has only .AppleSystemUIFont / Apple Symbols),
# and no assets/icons/ exists yet, so we draw clean monochrome line icons in
# code. This keeps the toolbar fully styled with zero external dependencies;
# swapping in real SF Symbol PNGs or SVGs later means replacing only `make_icon`.
def _icon_from_path(painter_fn, *, size: int = 64, color: str = theme.TOOLBAR_ICON,
                    disabled_color: str = theme.TOOLBAR_ICON_DISABLED) -> QIcon:
    """Render ``painter_fn(painter, n)`` (n = canvas size) into a QIcon with a
    Normal and a Disabled pixmap so toolbar buttons dim correctly."""
    icon = QIcon()
    for state_color, mode in ((color, QIcon.Normal), (disabled_color, QIcon.Disabled)):
        pm = QPixmap(size, size)
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing, True)
        pen = QPen(QColor(state_color))
        pen.setWidthF(size * 0.075)
        pen.setCapStyle(Qt.RoundCap)
        pen.setJoinStyle(Qt.RoundJoin)
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)
        painter_fn(p, size)
        p.end()
        icon.addPixmap(pm, mode)
    return icon


def _draw_open(p: QPainter, n: float) -> None:
    # Folder drawn as ONE continuous outline (tab + body) so the lid and the
    # body do not overlap and double the stroke weight at the top-left, which
    # made this icon read heavier than the others.
    path = QPainterPath()
    path.moveTo(n * 0.16, n * 0.74)          # bottom-left
    path.lineTo(n * 0.16, n * 0.32)          # up the left edge to the tab
    path.lineTo(n * 0.40, n * 0.32)          # tab top
    path.lineTo(n * 0.47, n * 0.42)          # tab notch down to body top
    path.lineTo(n * 0.84, n * 0.42)          # body top edge
    path.lineTo(n * 0.84, n * 0.74)          # down the right edge
    path.closeSubpath()                       # bottom edge back to start
    p.drawPath(path)


def _draw_save(p: QPainter, n: float) -> None:
    # Down arrow into a tray (square.and.arrow.down).
    cx = n * 0.5
    p.drawLine(cx, n * 0.18, cx, n * 0.56)
    arrow = QPainterPath()
    arrow.moveTo(n * 0.34, n * 0.42)
    arrow.lineTo(cx, n * 0.60)
    arrow.lineTo(n * 0.66, n * 0.42)
    p.drawPath(arrow)
    tray = QPainterPath()
    tray.moveTo(n * 0.22, n * 0.66)
    tray.lineTo(n * 0.22, n * 0.80)
    tray.lineTo(n * 0.78, n * 0.80)
    tray.lineTo(n * 0.78, n * 0.66)
    p.drawPath(tray)


def _draw_save_as(p: QPainter, n: float) -> None:
    # Save with a small overlaid square (square.and.arrow.down.on.square).
    cx = n * 0.44
    p.drawLine(cx, n * 0.16, cx, n * 0.48)
    arrow = QPainterPath()
    arrow.moveTo(n * 0.30, n * 0.36)
    arrow.lineTo(cx, n * 0.52)
    arrow.lineTo(n * 0.58, n * 0.36)
    p.drawPath(arrow)
    p.drawRect(n * 0.20, n * 0.58, n * 0.44, n * 0.22)
    p.drawRect(n * 0.52, n * 0.30, n * 0.30, n * 0.30)


def _draw_undo(p: QPainter, n: float) -> None:
    path = QPainterPath()
    path.moveTo(n * 0.30, n * 0.34)
    path.lineTo(n * 0.20, n * 0.44)
    path.lineTo(n * 0.30, n * 0.54)
    p.drawPath(path)
    arc = QPainterPath()
    arc.moveTo(n * 0.20, n * 0.44)
    arc.cubicTo(n * 0.50, n * 0.40, n * 0.80, n * 0.46, n * 0.78, n * 0.74)
    p.drawPath(arc)


def _draw_redo(p: QPainter, n: float) -> None:
    path = QPainterPath()
    path.moveTo(n * 0.70, n * 0.34)
    path.lineTo(n * 0.80, n * 0.44)
    path.lineTo(n * 0.70, n * 0.54)
    p.drawPath(path)
    arc = QPainterPath()
    arc.moveTo(n * 0.80, n * 0.44)
    arc.cubicTo(n * 0.50, n * 0.40, n * 0.20, n * 0.46, n * 0.22, n * 0.74)
    p.drawPath(arc)


def _thicken_chevron_pen(p: QPainter, n: float) -> None:
    """Bump the prev/next chevron stroke from the default icon weight
    (n*0.075) to ~n*0.11 with round caps/joins, so the page nav chevrons read as
    clearly clickable next to the bold page field instead of as faint hairlines
    (review minor). Keeps the current pen color (full-strength when enabled,
    disabled tint when the icon's Disabled pixmap is drawn)."""
    pen = p.pen()
    pen.setWidthF(n * 0.11)
    pen.setCapStyle(Qt.RoundCap)
    pen.setJoinStyle(Qt.RoundJoin)
    p.setPen(pen)


def _draw_chevron_left(p: QPainter, n: float) -> None:
    _thicken_chevron_pen(p, n)
    path = QPainterPath()
    path.moveTo(n * 0.60, n * 0.26)
    path.lineTo(n * 0.38, n * 0.50)
    path.lineTo(n * 0.60, n * 0.74)
    p.drawPath(path)


def _draw_chevron_right(p: QPainter, n: float) -> None:
    _thicken_chevron_pen(p, n)
    path = QPainterPath()
    path.moveTo(n * 0.40, n * 0.26)
    path.lineTo(n * 0.62, n * 0.50)
    path.lineTo(n * 0.40, n * 0.74)
    p.drawPath(path)


def _draw_zoom_out(p: QPainter, n: float) -> None:
    r = n * 0.24
    cx, cy = n * 0.42, n * 0.42
    p.drawEllipse(cx - r, cy - r, r * 2, r * 2)
    p.drawLine(cx - r * 0.5, cy, cx + r * 0.5, cy)
    p.drawLine(n * 0.60, n * 0.60, n * 0.80, n * 0.80)


def _draw_zoom_in(p: QPainter, n: float) -> None:
    r = n * 0.24
    cx, cy = n * 0.42, n * 0.42
    p.drawEllipse(cx - r, cy - r, r * 2, r * 2)
    p.drawLine(cx - r * 0.5, cy, cx + r * 0.5, cy)
    p.drawLine(cx, cy - r * 0.5, cx, cy + r * 0.5)
    p.drawLine(n * 0.60, n * 0.60, n * 0.80, n * 0.80)


def _draw_doc(p: QPainter, n: float) -> None:
    # Empty-state document glyph (doc.text), filled lighter.
    path = QPainterPath()
    path.moveTo(n * 0.26, n * 0.14)
    path.lineTo(n * 0.60, n * 0.14)
    path.lineTo(n * 0.74, n * 0.28)
    path.lineTo(n * 0.74, n * 0.86)
    path.lineTo(n * 0.26, n * 0.86)
    path.closeSubpath()
    p.drawPath(path)
    p.drawLine(n * 0.60, n * 0.14, n * 0.60, n * 0.28)
    p.drawLine(n * 0.60, n * 0.28, n * 0.74, n * 0.28)
    for yy in (0.42, 0.54, 0.66):
        p.drawLine(n * 0.34, n * yy, n * 0.66, n * yy)


def _draw_add_text(p: QPainter, n: float) -> None:
    # An "A" with a small "+" badge (BUILD_SPEC §5.2): the add-text tool glyph.
    # The A is drawn as two legs + a crossbar; the + sits at the upper right.
    a = QPainterPath()
    a.moveTo(n * 0.16, n * 0.74)
    a.lineTo(n * 0.34, n * 0.26)
    a.lineTo(n * 0.52, n * 0.74)
    p.drawPath(a)
    p.drawLine(n * 0.23, n * 0.56, n * 0.45, n * 0.56)   # A crossbar
    # Plus badge, upper right.
    p.drawLine(n * 0.70, n * 0.30, n * 0.70, n * 0.50)
    p.drawLine(n * 0.60, n * 0.40, n * 0.80, n * 0.40)


def _draw_close(p: QPainter, n: float) -> None:
    # A small centered "x" for the tab close affordance (a muted glyph that
    # reads as "close", not the raw red Fusion square).
    p.drawLine(n * 0.34, n * 0.34, n * 0.66, n * 0.66)
    p.drawLine(n * 0.66, n * 0.34, n * 0.34, n * 0.66)


def _draw_trash(p: QPainter, n: float) -> None:
    # A trash can (BUILD_SPEC §5.2): lid, body, and two slats.
    p.drawLine(n * 0.26, n * 0.30, n * 0.74, n * 0.30)   # lid
    # Lid handle.
    p.drawLine(n * 0.42, n * 0.30, n * 0.44, n * 0.22)
    p.drawLine(n * 0.44, n * 0.22, n * 0.56, n * 0.22)
    p.drawLine(n * 0.56, n * 0.22, n * 0.58, n * 0.30)
    # Body (tapered can).
    body = QPainterPath()
    body.moveTo(n * 0.32, n * 0.30)
    body.lineTo(n * 0.36, n * 0.78)
    body.lineTo(n * 0.64, n * 0.78)
    body.lineTo(n * 0.68, n * 0.30)
    p.drawPath(body)
    # Vertical slats.
    p.drawLine(n * 0.44, n * 0.40, n * 0.45, n * 0.68)
    p.drawLine(n * 0.56, n * 0.40, n * 0.55, n * 0.68)


def _draw_select(p: QPainter, n: float) -> None:
    # An arrow cursor (the Select tool): a filled-look pointer outline.
    path = QPainterPath()
    path.moveTo(n * 0.30, n * 0.22)
    path.lineTo(n * 0.30, n * 0.72)
    path.lineTo(n * 0.42, n * 0.60)
    path.lineTo(n * 0.50, n * 0.78)
    path.lineTo(n * 0.58, n * 0.74)
    path.lineTo(n * 0.50, n * 0.56)
    path.lineTo(n * 0.66, n * 0.56)
    path.closeSubpath()
    p.drawPath(path)


def _draw_text_edit(p: QPainter, n: float) -> None:
    # An I-beam over a baseline (the Text Edit tool): a capital-I caret.
    cx = n * 0.5
    p.drawLine(cx, n * 0.24, cx, n * 0.76)        # vertical stem
    p.drawLine(n * 0.40, n * 0.24, n * 0.60, n * 0.24)   # top serif
    p.drawLine(n * 0.40, n * 0.76, n * 0.60, n * 0.76)   # bottom serif


def _draw_find(p: QPainter, n: float) -> None:
    # A magnifier (Find & Replace tool).
    r = n * 0.22
    cx, cy = n * 0.44, n * 0.44
    p.drawEllipse(cx - r, cy - r, r * 2, r * 2)
    p.drawLine(n * 0.60, n * 0.60, n * 0.80, n * 0.80)


def _draw_align_left(p: QPainter, n: float) -> None:
    for i, yy in enumerate((0.30, 0.44, 0.58, 0.72)):
        x1 = n * (0.78 if i % 2 == 0 else 0.62)
        p.drawLine(n * 0.22, n * yy, x1, n * yy)


def _draw_align_center(p: QPainter, n: float) -> None:
    for i, yy in enumerate((0.30, 0.44, 0.58, 0.72)):
        half = (0.28 if i % 2 == 0 else 0.20)
        p.drawLine(n * (0.5 - half), n * yy, n * (0.5 + half), n * yy)


def _draw_align_right(p: QPainter, n: float) -> None:
    for i, yy in enumerate((0.30, 0.44, 0.58, 0.72)):
        x0 = n * (0.22 if i % 2 == 0 else 0.38)
        p.drawLine(x0, n * yy, n * 0.78, n * yy)


def _draw_align_justify(p: QPainter, n: float) -> None:
    for yy in (0.30, 0.44, 0.58, 0.72):
        p.drawLine(n * 0.22, n * yy, n * 0.78, n * yy)


_ICON_DRAWERS = {
    "open": _draw_open,
    "save": _draw_save,
    "save_as": _draw_save_as,
    "undo": _draw_undo,
    "redo": _draw_redo,
    "prev": _draw_chevron_left,
    "next": _draw_chevron_right,
    "zoom_out": _draw_zoom_out,
    "zoom_in": _draw_zoom_in,
    "doc": _draw_doc,
    "add_text": _draw_add_text,
    "delete": _draw_trash,
    "close": _draw_close,
    "select": _draw_select,
    "text_edit": _draw_text_edit,
    "find": _draw_find,
    "align_left": _draw_align_left,
    "align_center": _draw_align_center,
    "align_right": _draw_align_right,
    "align_justify": _draw_align_justify,
}


def make_icon(name: str, color: str = theme.TOOLBAR_ICON) -> QIcon:
    """Return the named icon from the shared SVG icon set (ui/icons.py).

    The old hand-drawn QPainter glyphs (and the "I"/"A+" text buttons) are
    replaced by one coherent line-icon family, so the toolbar and tool strip
    read as a designed set. Call sites only ever pass a logical name, unchanged.
    """
    from .icons import make_icon as _svg_make_icon
    return _svg_make_icon(name, color)


class _PageField(QLineEdit):
    """The footer page-number field: select-all on focus so clicking in (or
    Ctrl+G) lets you type the new page immediately. A plain selectAll() in
    focusInEvent is clobbered by Qt placing the cursor on the mouse release, so
    defer it one tick (QTimer.singleShot(0, ...))."""

    def focusInEvent(self, event) -> None:  # noqa: N802 (Qt override)
        super().focusInEvent(event)
        QTimer.singleShot(0, self.selectAll)


# ===========================================================================
# Undo commands: thin Qt wrappers over the model's GENERALIZED history
# (BUILD_SPEC §1.4 / §5.3). The MODEL is the single owner of edit state; every
# Qt command drives it in LOCKSTEP so the two stacks never drift:
#
#   * the FIRST redo (when the command is pushed) calls the model mutator,
#     which pushes one _Command onto the model's own _undo list;
#   * every undo calls ``document.undo()`` (pops the model command);
#   * every later redo calls ``document.redo()`` (replays it).
#
# Because a fresh command after an undo clears BOTH the Qt redo stack and the
# model's _redo (the mutator clears redo), the model's _undo/_redo stay a 1:1
# mirror of the Qt stack across arbitrary interleavings of text/style/move/
# resize/delete/add. This replaces the old re-staging EditRunCommand, which
# pushed a SECOND model command on undo and would desync once a generalized
# box command shared the stack.
# ===========================================================================
class _ModelCommand(QUndoCommand):
    """Base class: lockstep with the model's generalized history.

    Subclasses implement ``_apply()`` (the FIRST redo's mutator call) and set
    the command label + the (page_index, box) the canvas should repaint. The
    box ref may be updated by ``_apply`` (e.g. ``add_box`` returns the NewBox).
    """

    def __init__(self, document: PDFDocument, view, page_index: int, box):
        super().__init__()
        self._doc = document
        self._view = view
        self._page_index = page_index
        self._box = box
        self._applied = False        # False until the first redo runs the mutator

    # -- subclass hook ----------------------------------------------------
    def _apply(self):
        """Run the model mutator (first redo). May return a new box ref to
        repaint (e.g. the NewBox from add_box)."""
        raise NotImplementedError

    # -- QUndoCommand -----------------------------------------------------
    def redo(self) -> None:
        if not self._applied:
            self._applied = True
            new_box = self._apply()
            if new_box is not None:
                self._box = new_box
        else:
            ref = self._doc.redo()
            if ref is not None:
                self._box = ref[1]
        self._repaint()

    def undo(self) -> None:
        ref = self._doc.undo()
        if ref is not None:
            self._box = ref[1]
        self._repaint()

    # -- repaint ----------------------------------------------------------
    def _repaint(self) -> None:
        # ASSUMPTION (BUILD_SPEC §5.2): the spec PageView exposes
        # ``repaint_box(box)`` (the generalized ``repaint_span``). Fall back to
        # ``repaint_span`` then a full ``reload`` against the current MVP canvas.
        for name in ("repaint_box", "repaint_span"):
            fn = getattr(self._view, name, None)
            if callable(fn):
                try:
                    fn(self._box)
                    return
                except (TypeError, RuntimeError):
                    pass
        reload_fn = getattr(self._view, "reload", None)
        if callable(reload_fn):
            reload_fn()


class EditRunCommand(_ModelCommand):
    """One text edit of one span, driving ``document.stage_edit`` (BUILD_SPEC
    §6.4). Kept as a named class for the text path; box mutations use
    ``BoxCommand``. Both share ``_ModelCommand``'s lockstep undo/redo."""

    def __init__(self, document: PDFDocument, view, page_index: int, span,
                 old_text: str, new_text: str):
        super().__init__(document, view, page_index, span)
        self._old = old_text
        self._new = new_text
        self.setText(self._label(old_text, new_text))

    @staticmethod
    def _truncate(text: str, n: int = 24) -> str:
        text = text.replace("\n", " ")
        return text if len(text) <= n else text[: n - 1] + "…"

    def _label(self, old_text: str, new_text: str) -> str:
        return f"Edit ‘{self._truncate(old_text)}’ → " \
               f"‘{self._truncate(new_text)}’"

    def _apply(self):
        self._doc.stage_edit(self._page_index, self._box, self._new)
        return None


class RichEditCommand(EditRunCommand):
    """A text edit carrying per-selection RICH styling (bold/italic runs).
    Same lockstep history as ``EditRunCommand``; the staged payload adds the
    ``runs`` tuple (None == make the box uniform again)."""

    def __init__(self, document: PDFDocument, view, page_index: int, span,
                 old_text: str, new_text: str, runs: tuple | None):
        super().__init__(document, view, page_index, span, old_text, new_text)
        self._runs = runs
        if runs is not None:
            self.setText("Style text selection")

    def _apply(self):
        self._doc.stage_edit(self._page_index, self._box, self._new,
                             runs=self._runs)
        return None


class BoxCommand(_ModelCommand):
    """The single QUndoCommand driving style/move/resize/delete/add (BUILD_SPEC
    §5.3). ``kind`` selects the model mutator; ``params`` carries its arguments.
    All flow through ``_ModelCommand``'s lockstep history so box mutations and
    text edits interleave correctly on ONE Qt stack.

      kind="style"      params={"overrides": {...}}
      kind="move"       params={"dx":.., "dy":.., ["nudge": True]}
      kind="resize"     params={"scale":.., "anchor":(x,y)|None}
      kind="delete"     params={}
      kind="add"        params={"origin":.., "text":.., "family":.., "size":..,
                                "color":.., "bold":.., "italic":..}
      kind="img_add"    params={"rect":.., "image": bytes, "kind":..,
                                "natural_px":..}   (images & signatures §4)
      kind="img_move"   params={"dx":.., "dy":..}
      kind="img_resize" params={"rect": (x0,y0,x1,y1)}
      kind="img_delete" params={}
      kind="xim_delete" params={}    box=ExistingImage (M3; "xim_move" is
                                NOT a BoxCommand kind -- the window expands
                                it into a macro of xim_delete + img_add)

    Nudge coalescing (text-editing UX §3.1): an arrow-key nudge is a "move"
    with ``params["nudge"]=True``. Consecutive nudges of the SAME box within
    1.5 s merge into ONE Qt command via ``id``/``mergeWith``, and the merge
    calls ``document.coalesce_last_undo()`` so the model history fuses in
    lockstep -- holding an arrow never piles up 40 undo steps, and one undo
    restores the pre-nudge origin on BOTH stacks. ``_NUDGE_ID`` (0xB02) is
    reserved; other workstreams adding command kinds must not reuse it.
    """

    _LABELS = {"style": "Change style", "move": "Move box",
               "resize": "Resize box", "frame_resize": "Resize box",
               "delete": "Delete box",
               "add": "Add text box",
               "img_add": "Insert image", "img_move": "Move image",
               "img_resize": "Resize image", "img_delete": "Delete image",
               "xim_delete": "Delete page image"}
    _NUDGE_ID = 0xB02
    _NUDGE_MERGE_WINDOW_S = 1.5

    def __init__(self, document: PDFDocument, view, page_index: int, box,
                 kind: str, params: dict):
        super().__init__(document, view, page_index, box)
        self._kind = kind
        self._params = dict(params or {})
        self._stamp = time.monotonic()       # for the nudge merge window
        label = self._LABELS.get(kind, "Edit box")
        if kind == "img_add":
            # The payload kind names the thing the user placed: undoing a
            # stamp must read "Undo Insert stamp", not "Undo Insert image".
            noun = {"stamp": "stamp", "signature": "signature"}.get(
                self._params.get("kind"))
            if noun:
                label = f"Insert {noun}"
        self.setText(label)

    def _is_nudge(self) -> bool:
        return self._kind == "move" and bool(self._params.get("nudge"))

    def id(self) -> int:  # noqa: A003 - QUndoCommand API name
        """0xB02 for arrow-key nudges so QUndoStack offers them to
        ``mergeWith``; -1 (never merge) for every other kind, including a
        body-drag move (one gesture = one discrete step)."""
        return self._NUDGE_ID if self._is_nudge() else -1

    def mergeWith(self, other) -> bool:  # noqa: N802 - QUndoCommand API name
        """Merge a follow-up nudge into this one (same box, both nudges,
        <= 1.5 s apart). ``other`` has already run its redo, so the model
        holds two "move" commands; ``coalesce_last_undo`` fuses them and ONLY
        a successful model fuse reports a Qt merge -- the 1:1 lockstep between
        the Qt stack and the model history survives by construction."""
        if not isinstance(other, BoxCommand):
            return False
        if not (self._is_nudge() and other._is_nudge()):
            return False
        mine = getattr(self._box, "identity", None)
        theirs = getattr(other._box, "identity", None)
        if mine is None or mine != theirs:
            return False
        if other._stamp - self._stamp > self._NUDGE_MERGE_WINDOW_S:
            return False
        if not self._doc.coalesce_last_undo():
            return False
        self._params["dx"] = (self._params.get("dx", 0.0)
                              + other._params.get("dx", 0.0))
        self._params["dy"] = (self._params.get("dy", 0.0)
                              + other._params.get("dy", 0.0))
        self._stamp = other._stamp
        return True

    def _apply(self):
        kind, p, page, box = self._kind, self._params, self._page_index, self._box
        if kind == "style":
            self._doc.set_style(page, box, **(p.get("overrides") or {}))
            return None
        if kind == "move":
            self._doc.move_box(page, box, p.get("dx", 0.0), p.get("dy", 0.0))
            return None
        if kind == "resize":
            self._doc.resize_box(page, box, p.get("scale", 1.0),
                                 anchor=p.get("anchor"))
            return None
        if kind == "frame_resize":
            self._doc.resize_text_frame(page, box, p.get("x", 0.0),
                                        p.get("w", 0.0))
            return None
        if kind == "delete":
            self._doc.delete_box(page, box)
            return None
        if kind == "add":
            return self._doc.add_box(
                page, p["origin"], p.get("text", ""), p["family"], p["size"],
                p["color"], p.get("bold", False), p.get("italic", False),
                direction=p.get("direction", (1.0, 0.0)),
                cover=p.get("cover", ()))
        # Placed images (images & signatures §4): the §2.3 model mutators.
        if kind == "img_add":
            return self._doc.add_image(
                page, p["rect"], p["image"], kind=p.get("kind", "file"),
                natural_px=p.get("natural_px", (0, 0)))
        if kind == "img_move":
            self._doc.move_image(page, box, p.get("dx", 0.0),
                                 p.get("dy", 0.0))
            return None
        if kind == "img_resize":
            self._doc.resize_image(page, box, p["rect"])
            return None
        if kind == "img_delete":
            self._doc.delete_image(page, box)
            return None
        if kind == "xim_delete":
            # Existing page image (M3): stage the scoped image-REMOVE
            # redaction for this occurrence.
            self._doc.delete_existing_image(page, box)
            return None
        return None


class AnnotCommand(_ModelCommand):
    """The single QUndoCommand driving ANNOTATION mutations (annotations &
    markup §5.1), next to -- never inside -- BoxCommand (whose kind set is a
    closed window-map invariant). Contract identical to BoxCommand: the first
    redo runs the model mutator, undo calls ``document.undo()``, later redo
    ``document.redo()`` -- the model's ``_AnnotCommand`` entries ride the same
    history, so text and annot commands interleave in 1:1 lockstep.

      kind="add"      ref=None      params={"kind","quads",... AnnotSpec fields}
      kind="delete"   ref=identity  params={}
      kind="move"     ref=identity  params={"dx","dy"}
      kind="contents" ref=identity  params={"text"}
      kind="style"    ref=identity  params={field: value} (stroke/fill/
                                    width/opacity -- staged annots only)

    ``ref`` identities cover BOTH staged annots ((page,"annot",id)) and
    existing file annots ((page,"xref",xref) -- the model folds those into
    its override map). Repaint is BY PAGE (``view.repaint_page``): an
    annotation has no box, and the baked pixmap of the whole page changed.
    Commands tolerate the structural stack wipe (staged annots were baked
    first). ``on_change`` (M4) is invoked after EVERY redo/undo so the
    window can re-list the Comments panel (§5.4) without a blanket
    undo-stack hook firing on unrelated text commands."""

    _LABELS = {"add": "Add annotation", "delete": "Delete annotation",
               "move": "Move annotation", "contents": "Edit note",
               "style": "Style annotation"}

    def __init__(self, document: PDFDocument, view, page_index: int, ref,
                 kind: str, params: dict, on_change=None):
        super().__init__(document, view, page_index, ref)
        self._kind = kind
        self._params = dict(params or {})
        self._on_change = on_change
        label = self._LABELS.get(kind, "Edit annotation")
        annot_kind = self._params.get("kind")
        if kind == "add" and annot_kind:
            label = f"Add {annot_kind}"
        self.setText(label)

    def redo(self) -> None:
        super().redo()
        self._notify()

    def undo(self) -> None:
        super().undo()
        self._notify()

    def _notify(self) -> None:
        if callable(self._on_change):
            try:
                self._on_change()
            except (TypeError, RuntimeError):
                pass

    def _apply(self):
        if self._kind == "add":
            return self._doc.add_annot(self._page_index, **self._params)
        if self._kind == "delete":
            self._doc.delete_annot_box(self._page_index, self._box)
            return None
        if self._kind == "move":
            self._doc.move_annot(self._page_index, self._box,
                                 self._params.get("dx", 0.0),
                                 self._params.get("dy", 0.0))
            return None
        if self._kind == "contents":
            self._doc.set_annot_contents(self._page_index, self._box,
                                         self._params.get("text", ""))
            return None
        if self._kind == "style":
            self._doc.modify_annot(self._page_index, self._box,
                                   **self._params)
            return None
        raise ValueError(f"unsupported annot command kind: {self._kind!r}")

    def _repaint(self) -> None:
        fn = getattr(self._view, "repaint_page", None)
        if callable(fn):
            try:
                fn(self._page_index)
                return
            except (TypeError, RuntimeError):
                pass
        super()._repaint()


class FormFieldCommand(_ModelCommand):
    """One form-field fill (forms §4), beside EditRunCommand/AnnotCommand:
    the single QUndoCommand driving ``document.stage_form_value``. Every
    fill gesture -- text commit, checkbox toggle, radio pick, combo pick --
    is exactly one of these == one model ``_Command`` keyed by the field's
    ``group_key``, so fills interleave with text/box/annot mutations on the
    ONE shared history in 1:1 lockstep (``_ModelCommand`` semantics: first
    redo runs the mutator, undo ``document.undo()``, later redo
    ``document.redo()``). Tolerates the structural-boundary stack wipe by
    construction, like every _ModelCommand. Repaint is BY PAGE (the
    AnnotCommand precedent): a fill has no box, and the baked pixmap of the
    whole page changed."""

    def __init__(self, document: PDFDocument, view, field, value):
        super().__init__(document, view, field.page_index, field)
        self._value = value
        if field.kind == "checkbox":
            label = ("Check" if value else "Uncheck") + f" ‘{field.name}’"
        elif field.kind in ("radio", "combo"):
            label = f"Choose ‘{field.name}’"
        else:
            label = f"Fill ‘{field.name}’"
        self.setText(label)

    def _apply(self):
        self._doc.stage_form_value(self._page_index, self._box, self._value)
        return None

    def _repaint(self) -> None:
        fn = getattr(self._view, "repaint_page", None)
        if callable(fn):
            try:
                fn(self._page_index)
                return
            except (TypeError, RuntimeError):
                pass
        super()._repaint()


class NotePopup(QWidget):
    """The frameless NON-MODAL sticky-note editor (annotations & markup
    §5.3): a QPlainTextEdit + Done button, child of the view's viewport,
    anchored next to the note's anchor rect by the window. Never a QDialog
    (no ``exec()``: the offscreen-test trap); tests reach it as
    ``window._note_popup``.

    ONE commit path (Done / editor focus-out / Cmd+Return) that fires the
    window's callback only when the text CHANGED; Esc cancels without a
    command. ``_closed`` makes commit/cancel idempotent -- hiding a focused
    editor re-enters the filter with a FocusOut, which must not
    double-commit."""

    def __init__(self, viewport, ref, page_index: int, initial: str,
                 on_commit, on_closed):
        super().__init__(viewport)
        self.setObjectName("NotePopup")
        self._ref = ref
        self._page_index = page_index
        self._initial = initial
        self._on_commit = on_commit
        self._on_closed = on_closed
        self._closed = False
        self.setAutoFillBackground(True)
        col = QVBoxLayout(self)
        col.setContentsMargins(8, 8, 8, 8)
        col.setSpacing(6)
        self.editor = QPlainTextEdit(self)
        self.editor.setPlainText(initial)
        self.editor.setFixedSize(230, 110)
        self.editor.setFont(theme.ui_font(12))
        col.addWidget(self.editor)
        row = QHBoxLayout()
        row.addStretch(1)
        self.done_button = QPushButton("Done", self)
        self.done_button.setDefault(True)
        self.done_button.clicked.connect(self.commit)
        row.addWidget(self.done_button)
        col.addLayout(row)
        self.editor.installEventFilter(self)
        self.adjustSize()

    def eventFilter(self, obj, event):
        if obj is self.editor and not self._closed:
            etype = event.type()
            if etype == QEvent.KeyPress:
                if event.key() == Qt.Key_Escape:
                    self.cancel()
                    return True
                if event.key() in (Qt.Key_Return, Qt.Key_Enter) and \
                        event.modifiers() & (Qt.ControlModifier
                                             | Qt.MetaModifier):
                    self.commit()
                    return True
            elif etype == QEvent.FocusOut:
                # Focus moved on (a click elsewhere, the Done press, a tool
                # switch): commit-on-focus-out so typed text is never
                # silently lost. _closed makes the re-entry from our own
                # hide() a no-op.
                self.commit()
        return super().eventFilter(obj, event)

    def commit(self) -> None:
        if self._closed:
            return
        self._closed = True
        text = self.editor.toPlainText()
        self.hide()
        self.deleteLater()
        self._on_closed()
        if text != self._initial:
            self._on_commit(self._ref, self._page_index, text)

    def cancel(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.hide()
        self.deleteLater()
        self._on_closed()


# ===========================================================================
# StructuralCommand: ONE coarse page-op step on the SAME undo stack
# (PAGES_SPEC §6.6). The model already applied + snapshotted the op before this
# command is pushed, so its FIRST redo is a no-op; undo() drives
# document.undo_structural(), later redo() drives document.redo_structural().
# After either, it triggers the window's refresh so canvas/sidebar/tabs rebuild.
# ===========================================================================
class GroupingCommand(QUndoCommand):
    """A manual GROUP / UNGROUP of boxes (user override of automatic paragraph
    detection), undoable by restoring the manual-grouping snapshot. The op has
    ALREADY run in the handler (which captured ``before``), so the first redo
    (on push) is a no-op; later undo/redo restore the before/after snapshots."""

    def __init__(self, document: PDFDocument, window: "MainWindow",
                 before_snap, label: str):
        super().__init__(label)
        self._doc = document
        self._win = window
        self._before = before_snap
        self._after = document.manual_grouping_snapshot()
        self._applied = False

    def redo(self) -> None:
        if not self._applied:
            self._applied = True       # already applied + snapshotted
            return
        self._doc.restore_manual_grouping(self._after)
        self._win._refresh_after_grouping()

    def undo(self) -> None:
        self._doc.restore_manual_grouping(self._before)
        self._win._refresh_after_grouping()


class StructuralCommand(QUndoCommand):
    """A single structural page-op as one QUndoCommand."""

    def __init__(self, document: PDFDocument, window: "MainWindow",
                 label: str = "Page operation"):
        super().__init__(label)
        self._doc = document
        self._win = window
        self._applied = False        # the op already ran in the handler

    def redo(self) -> None:
        if not self._applied:
            self._applied = True     # op already done + snapshotted; do not re-run
            return
        self._doc.redo_structural()
        self._win._refresh_after_structural_undo()

    def undo(self) -> None:
        self._doc.undo_structural()
        self._win._refresh_after_structural_undo()


# ===========================================================================
# DocumentTabBar: one tab per open document (PAGES_SPEC §5.2). The canvas is
# shared; only the active document changes, so a QTabBar (not a full
# QTabWidget) sits just under the toolbar. Hidden when <= 1 document is open.
# ===========================================================================
class DocumentTabBar(QTabBar):
    """The document tab strip. ``tabCloseRequested`` / ``tabMoved`` are Qt's
    built-ins; ``tabActivated`` mirrors ``currentChanged`` for user switches."""

    tabActivated = Signal(int)               # user selected tab idx

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("DocumentTabBar")
        # We PAINT the close glyph + dirty dot ourselves (Qt's side-widget
        # placement could not be centred reliably), so no native close buttons.
        self.setTabsClosable(False)
        self.setMovable(True)
        self.setExpanding(False)
        # No scroll arrows: when many tabs are open they elide their names
        # instead of showing the ugly "< >" overflow buttons next to the tabs.
        self.setUsesScrollButtons(False)
        self.setDrawBase(True)
        self.setElideMode(Qt.ElideRight)
        self.setMouseTracking(True)
        self._syncing = False
        # When wrapped in a full-width strip (so the tabs stay LEFT-anchored
        # instead of macOS centering them), sync() toggles the STRIP's
        # visibility, not the bare bar's. None == standalone (toggle self).
        self._strip = None
        self._dirty: dict[int, bool] = {}    # tab index -> unsaved?
        self._close_rects: dict[int, QRect] = {}  # tab index -> X hit area
        self._hover_close = -1               # tab whose X is hovered
        self.currentChanged.connect(self._on_current_changed)

    def _on_current_changed(self, idx: int) -> None:
        if self._syncing or idx < 0:
            return
        self.tabActivated.emit(idx)

    def sync(self, workspace: Workspace) -> None:
        """Rebuild tabs from ``workspace``: one tab per document, text =
        ``workspace.tab_name(i)``, current = active_index. The close X and the
        dirty dot are PAINTED in ``paintEvent`` (precise, symmetric placement).
        Blocks signals while syncing so a rebuild never echoes as a user switch.
        Hidden at <= 1 document so the single-doc case looks tabless."""
        self._syncing = True
        try:
            while self.count() > workspace.count:
                self.removeTab(self.count() - 1)
            while self.count() < workspace.count:
                self.addTab("")
            self._dirty = {}
            for i in range(workspace.count):
                self.setTabText(i, workspace.tab_name(i))
                self._dirty[i] = bool(workspace.is_dirty(i))
            if workspace.active_index >= 0:
                self.setCurrentIndex(workspace.active_index)
        finally:
            self._syncing = False
        self.update()
        # Toggle the wrapping strip if present (keeps the full-width bar
        # background while the tabs hug the left), else the bare bar.
        (self._strip or self).setVisible(workspace.count > 1)

    # --- painted close X + dirty dot -------------------------------------
    def paintEvent(self, event) -> None:
        """Draw the tabs (text) via the base class, then paint each tab's close X
        and -- when dirty -- its dot. Each glyph's CENTRE is placed exactly half
        the tab height from its edge, so it is the same distance from the right
        (or left) as from the top and bottom: evenly inset by construction."""
        super().paintEvent(event)
        from PySide6.QtCore import QPointF, QRect
        from PySide6.QtGui import QPen
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        self._close_rects = {}
        for i in range(self.count()):
            r = self.tabRect(i)
            if r.isEmpty():
                continue
            half = r.height() / 2.0
            cy = r.center().y() + 0.5
            cx = r.right() - half            # X centre: `half` from the right
            hovered = (i == self._hover_close)
            if hovered:
                p.setPen(Qt.NoPen)
                p.setBrush(QColor(255, 255, 255, 26))
                p.drawEllipse(QPointF(cx, cy), 8.5, 8.5)
            pen = QPen(QColor(theme.TEXT_PRIMARY if hovered
                              else theme.TOOLBAR_ICON), 1.4)
            pen.setCapStyle(Qt.RoundCap)
            p.setPen(pen)
            p.setBrush(Qt.NoBrush)
            a = 3.0
            p.drawLine(QPointF(cx - a, cy - a), QPointF(cx + a, cy + a))
            p.drawLine(QPointF(cx - a, cy + a), QPointF(cx + a, cy - a))
            self._close_rects[i] = QRect(int(cx - 10), int(cy - 10), 20, 20)
            # Status circle (always shown) on the LEFT, mirroring the X:
            #   indigo = unsaved edits; green = the open tab (saved);
            #   orange = another open tab (saved).
            if self._dirty.get(i):
                dot = QColor(theme.ACCENT)
            elif i == self.currentIndex():
                dot = QColor(theme.TAB_DOT_ACTIVE)
            else:
                dot = QColor(theme.TAB_DOT_OTHER)
            p.setPen(Qt.NoPen)
            p.setBrush(dot)
            p.drawEllipse(QPointF(r.left() + half, cy), 3.0, 3.0)
        p.end()

    def set_tab_dirty(self, index: int, dirty: bool) -> None:
        """Update one tab's unsaved state and repaint -- so the OPEN tab's circle
        turns indigo the moment it is edited (not only after switching away)."""
        if 0 <= index < self.count() and self._dirty.get(index) != bool(dirty):
            self._dirty[index] = bool(dirty)
            self.update()

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            pos = event.position().toPoint()
            for i, rect in self._close_rects.items():
                if rect.contains(pos):
                    self.tabCloseRequested.emit(i)
                    return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        pos = event.position().toPoint()
        hov = -1
        for i, rect in self._close_rects.items():
            if rect.contains(pos):
                hov = i
                break
        if hov != self._hover_close:
            self._hover_close = hov
            self.setCursor(Qt.PointingHandCursor if hov >= 0 else Qt.ArrowCursor)
            self.update()
        super().mouseMoveEvent(event)

    def leaveEvent(self, event) -> None:
        if self._hover_close != -1:
            self._hover_close = -1
            self.update()
        super().leaveEvent(event)


# ===========================================================================
# Split dialog (PAGES_SPEC §5.3): the UI computes `ranges`; the model consumes
# them. Three strategies all reduce to a list of (start, end) inclusive ranges.
# ===========================================================================
class _SplitDialog(QDialog):
    """Choose a split strategy and produce the 0-based inclusive page ranges."""

    def __init__(self, page_count: int, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Split PDF")
        self._n = page_count
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 18, 20, 16)
        lay.setSpacing(12)

        header = QLabel("SPLIT METHOD")
        header.setObjectName("InspectorSectionHeader")
        header.setFont(theme.ui_font(11, semibold=True))
        lay.addWidget(header)
        lay.addWidget(QLabel(f"This document has {page_count} pages."))

        self.rb_every = QRadioButton("Every N pages:")
        self.rb_every.setChecked(True)
        row = QHBoxLayout()
        row.addWidget(self.rb_every)
        self.spin_n = QSpinBox()
        self.spin_n.setRange(1, max(1, page_count))
        self.spin_n.setValue(1)
        row.addWidget(self.spin_n)
        row.addStretch(1)
        lay.addLayout(row)

        self.rb_each = QRadioButton("One file per page")
        lay.addWidget(self.rb_each)

        row2 = QHBoxLayout()
        self.rb_at = QRadioButton("Split at page:")
        row2.addWidget(self.rb_at)
        self.spin_k = QSpinBox()
        self.spin_k.setRange(2, max(2, page_count))
        self.spin_k.setValue(min(2, page_count))
        row2.addWidget(self.spin_k)
        row2.addStretch(1)
        lay.addLayout(row2)
        if page_count < 2:
            self.rb_at.setEnabled(False)
            self.spin_k.setEnabled(False)

        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)

    def ranges(self) -> list[tuple[int, int]]:
        """The 0-based inclusive ranges for the chosen strategy."""
        n = self._n
        if self.rb_each.isChecked():
            return [(i, i) for i in range(n)]
        if self.rb_at.isChecked():
            k = self.spin_k.value()             # 1-based split point
            return [(0, k - 2), (k - 1, n - 1)]
        size = self.spin_n.value()
        out: list[tuple[int, int]] = []
        start = 0
        while start < n:
            end = min(start + size - 1, n - 1)
            out.append((start, end))
            start = end + 1
        return out


# ===========================================================================
# Canvas container: gray gutter hosting the PageView + drop target.
# ===========================================================================
class CanvasContainer(QWidget):
    """Neutral-gray host for the ``PageView`` that also accepts ``*.pdf``
    drops (open) and ``*.png/*.jpg/*.jpeg`` drops (arm image placement,
    images & signatures §4), and overlays the empty state while no document
    is open."""

    _IMAGE_EXTS = (".png", ".jpg", ".jpeg")

    def __init__(self, window: "MainWindow"):
        super().__init__()
        self.setObjectName("CanvasContainer")
        self._window = window
        self.setAcceptDrops(True)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.view = PageView(self)
        layout.addWidget(self.view)

        self._drag_active = False

    # --- drag and drop ---------------------------------------------------
    def _pdfs_from_event(self, event) -> list:
        """Every ``.pdf`` local file in the drag, in URL order (navigation
        M3: a multi-file drop opens one tab per file, like Acrobat)."""
        mime = event.mimeData()
        if not mime.hasUrls():
            return []
        return [url.toLocalFile() for url in mime.urls()
                if url.toLocalFile().lower().endswith(".pdf")]

    def _image_from_event(self, event) -> str | None:
        """The first dropped PNG/JPEG path -- placeable only with an open
        document (placement needs a page). PDF drops keep priority."""
        if self._window.document is None:
            return None
        mime = event.mimeData()
        if not mime.hasUrls():
            return None
        for url in mime.urls():
            path = url.toLocalFile()
            if path.lower().endswith(self._IMAGE_EXTS):
                return path
        return None

    def dragEnterEvent(self, event) -> None:
        if self._pdfs_from_event(event) or self._image_from_event(event):
            event.acceptProposedAction()
            self._drag_active = True
            self.update()
        else:
            event.ignore()

    def dragLeaveEvent(self, event) -> None:
        self._drag_active = False
        self.update()

    def dropEvent(self, event) -> None:
        paths = self._pdfs_from_event(event)
        image_path = None if paths else self._image_from_event(event)
        self._drag_active = False
        self.update()
        if paths:
            event.acceptProposedAction()
            # Every PDF in the drop opens (its own tab via the workspace);
            # the last one ends up active, matching a serial File > Open.
            for path in paths:
                self._window.open_path(path)
        elif image_path:
            event.acceptProposedAction()
            self._window._place_image_from_file(image_path)

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        if self._drag_active:
            painter = QPainter(self)
            painter.setRenderHint(QPainter.Antialiasing, True)
            rect = self.rect().adjusted(8, 8, -8, -8)
            # Faint clay-wash fill inside the dashed accent border (the kit's
            # drag-over hint), rounded at --radius-xl (14).
            painter.setPen(Qt.NoPen)
            painter.setBrush(QBrush(theme.color_accent_hover()))
            painter.drawRoundedRect(rect, 14, 14)
            pen = QPen(theme.color_accent())
            pen.setWidth(2)
            pen.setStyle(Qt.DashLine)
            painter.setPen(pen)
            painter.setBrush(Qt.NoBrush)
            painter.drawRoundedRect(rect, 14, 14)
            painter.end()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        # Keep the empty-state overlay filling the canvas so its content stays
        # CENTERED. Doing this here (on the canvas's own resize) uses the real
        # post-layout size; the window's resizeEvent fired before the canvas
        # child was resized, which left the overlay at its default top-left box.
        es = getattr(self._window, "empty_state", None)
        if es is not None and es.parentWidget() is self:
            es.setGeometry(self.rect())


def _abbrev_home(folder: str) -> str:
    """``/Users/<name>/Documents/Forms`` -> ``~/Documents/Forms`` (navigation
    M3): the folder line under a recent file and the Open Recent
    disambiguation both read shorter and never spell out the account name."""
    home = os.path.expanduser("~")
    if folder == home or folder.startswith(home + os.sep):
        return "~" + folder[len(home):]
    return folder


# ===========================================================================
# Empty state overlay (BUILD_SPEC §6.7).
# ===========================================================================
# Card geometry for the Recent gallery. One card = a first-page thumbnail with
# the filename + folder beneath; the grid reflows its column count to the window.
_CARD_W = 188
_THUMB_H = 150
_META_H = 56
_CARD_H = _THUMB_H + _META_H
_CARD_GAP = 16
_CARD_RADIUS = 11
_CARD_BG = theme.CARD_BG          # a warm tile that reads above the canvas gutter
_CARD_BG_HOVER = theme.CARD_BG_HOVER   # a touch brighter under the pointer


class _ThumbLabel(QWidget):
    """The page-preview area at the top of a Recent card: a fixed box that
    paints the first-page pixmap (top-aligned, cropped) with rounded TOP corners
    to match the card, and a quiet placeholder glyph until the pixmap arrives."""

    def __init__(self, w: int, h: int, radius: int):
        super().__init__()
        self.setFixedSize(w, h)
        self._radius = radius
        self._pm: QPixmap | None = None
        self._placeholder = make_icon(
            "doc", color=theme.TOOLBAR_ICON_DISABLED).pixmap(QSize(34, 34))

    def set_pixmap(self, pm: QPixmap) -> None:
        w, h = self.width(), self.height()
        scaled = pm.scaledToWidth(w, Qt.SmoothTransformation)
        if scaled.height() > h:               # crop to the page's TOP edge
            scaled = scaled.copy(0, 0, w, h)
        self._pm = scaled
        self.update()

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h, r = self.width(), self.height(), self._radius
        path = QPainterPath()
        path.moveTo(0, h)
        path.lineTo(0, r)
        path.quadTo(0, 0, r, 0)
        path.lineTo(w - r, 0)
        path.quadTo(w, 0, w, r)
        path.lineTo(w, h)
        path.closeSubpath()
        p.setClipPath(path)
        if self._pm is not None:
            p.fillRect(0, 0, w, h, QColor("#FFFFFF"))
            p.drawPixmap(0, 0, self._pm)
        else:
            p.fillRect(0, 0, w, h, QColor(_CARD_BG_HOVER))
            gx = (w - self._placeholder.width()) // 2
            gy = (h - self._placeholder.height()) // 2
            p.drawPixmap(gx, gy, self._placeholder)


class RecentCard(QFrame):
    """A single Recent document tile: thumbnail + filename + folder. Click opens
    it; a hover-revealed star pins it to the front of the gallery and an × drops
    it from the list (the file itself is untouched). Pinned cards keep the star
    visible so the pin state reads at a glance."""

    def __init__(self, path: str, pinned: bool, on_open, on_remove,
                 on_toggle_pin):
        super().__init__()
        self.path = path
        self._pinned = pinned
        self._on_open = on_open
        self.setObjectName("RecentCard")
        self.setFixedSize(_CARD_W, _CARD_H)
        self.setCursor(Qt.PointingHandCursor)
        self.setProperty("hover", False)
        self.setToolTip(path)
        self.setStyleSheet(_CARD_QSS)

        box = QVBoxLayout(self)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(0)
        self.thumb = _ThumbLabel(_CARD_W - 2, _THUMB_H, _CARD_RADIUS)
        box.addWidget(self.thumb)

        meta = QWidget()
        meta.setFixedHeight(_META_H)
        mcol = QVBoxLayout(meta)
        mcol.setContentsMargins(12, 8, 12, 8)
        mcol.setSpacing(1)
        fm_name = theme.ui_font(13)
        name = QLabel(_elide(os.path.basename(path), fm_name, _CARD_W - 26))
        name.setObjectName("RecentName")
        name.setFont(fm_name)
        name.setStyleSheet(f"color: {theme.TEXT_PRIMARY}; background: transparent;")
        fm_folder = theme.ui_font(11)
        folder = QLabel(_elide(_abbrev_home(os.path.dirname(path)),
                               fm_folder, _CARD_W - 26))
        folder.setObjectName("RecentFolder")
        folder.setFont(fm_folder)
        folder.setStyleSheet(
            f"color: {theme.TEXT_SECONDARY}; background: transparent;")
        mcol.addWidget(name)
        mcol.addWidget(folder)
        box.addWidget(meta)

        # Overlay controls parented on the card, positioned over the thumbnail.
        # Hidden until hover, except the star which stays lit while pinned.
        self._pin_btn = QPushButton(self)
        self._pin_btn.setObjectName("CardOverlay")
        self._pin_btn.setCursor(Qt.PointingHandCursor)
        self._pin_btn.setFixedSize(22, 22)
        self._pin_btn.move(8, 8)
        self._pin_btn.setStyleSheet(_CARD_OVERLAY_QSS)
        self._remove_btn = QPushButton("✕", self)
        self._remove_btn.setObjectName("CardOverlay")
        self._remove_btn.setCursor(Qt.PointingHandCursor)
        self._remove_btn.setFixedSize(22, 22)
        self._remove_btn.move(_CARD_W - 30, 8)
        self._remove_btn.setStyleSheet(_CARD_OVERLAY_QSS)
        self._remove_btn.setToolTip("Remove from Recents")
        self._remove_btn.clicked.connect(lambda: on_remove(path))
        self._pin_btn.clicked.connect(lambda: on_toggle_pin(path))
        self._refresh_pin()
        self._remove_btn.hide()

    def _refresh_pin(self) -> None:
        self._pin_btn.setText("★" if self._pinned else "☆")
        self._pin_btn.setToolTip("Unpin" if self._pinned else "Pin to top")
        # A pinned card advertises its star even at rest; an unpinned one only
        # shows it under the pointer.
        self._pin_btn.setVisible(self._pinned)

    def set_thumbnail(self, pm: QPixmap) -> None:
        self.thumb.set_pixmap(pm)

    def enterEvent(self, event) -> None:
        self._set_hover(True)
        self._pin_btn.show()
        self._remove_btn.show()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self._set_hover(False)
        self._remove_btn.hide()
        self._pin_btn.setVisible(self._pinned)
        super().leaveEvent(event)

    def _set_hover(self, on: bool) -> None:
        self.setProperty("hover", on)
        self.style().unpolish(self)
        self.style().polish(self)

    def mouseReleaseEvent(self, event) -> None:
        # A click anywhere on the card (the overlay buttons consume their own
        # clicks) opens the document.
        if event.button() == Qt.LeftButton and self.rect().contains(event.pos()):
            self._on_open(self.path)
        super().mouseReleaseEvent(event)


class EmptyState(QWidget):
    """Start screen shown on the canvas while no PDF is open. With recent files
    it is a GALLERY: a greeting, an Open button, and a reflowing grid of
    document cards (first-page thumbnails, pin, remove, and a search box once the
    list grows). With no history it falls back to a centered call-to-action so a
    brand-new user still gets a friendly prompt."""

    def __init__(self, on_open, on_open_recent, on_clear_recents=None,
                 on_remove_recent=None, on_toggle_pin=None):
        super().__init__()
        self.setObjectName("EmptyState")
        self.setAttribute(Qt.WA_TransparentForMouseEvents, False)
        self._on_open = on_open
        self._on_open_recent = on_open_recent
        self._on_clear_recents = on_clear_recents
        self._on_remove_recent = on_remove_recent
        self._on_toggle_pin = on_toggle_pin
        self._cards: list[RecentCard] = []
        self._by_path: dict[str, RecentCard] = {}
        self._pinned: set[str] = set()

        # One incremental loader feeds every card's thumbnail (cache-first, then
        # one render per event-loop tick so the grid never freezes the window).
        self._loader = ThumbnailLoader(max_px=600, parent=self)
        self._loader.ready.connect(self._on_thumb_ready)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        self._hero = self._build_hero()
        self._gallery = self._build_gallery()
        outer.addWidget(self._hero)
        outer.addWidget(self._gallery)
        self._hero.hide()
        self._gallery.hide()

    # --- hero (no recents) ------------------------------------------------
    def _build_hero(self) -> QWidget:
        host = QWidget()
        v = QVBoxLayout(host)
        v.setContentsMargins(40, 40, 40, 40)
        v.setAlignment(Qt.AlignCenter)
        # A clay-washed rounded tile holding the document glyph in the accent --
        # the one place the empty state carries the brand color.
        tile = QFrame()
        tile.setObjectName("HeroIconTile")
        tile.setFixedSize(78, 78)
        tile.setStyleSheet(
            f"QFrame#HeroIconTile {{ background: {theme.ACCENT_HOVER}; "
            f"border: 1px solid {theme.ACCENT_BORDER}; border-radius: 18px; }}")
        tile_layout = QVBoxLayout(tile)
        tile_layout.setContentsMargins(0, 0, 0, 0)
        glyph = QLabel()
        glyph.setAlignment(Qt.AlignCenter)
        glyph.setPixmap(make_icon("doc", color=theme.ACCENT).pixmap(QSize(38, 38)))
        tile_layout.addWidget(glyph, 0, Qt.AlignCenter)
        # The ONE editorial-serif display moment (Newsreader): the largest text on
        # screen, the only serif in the chrome.
        headline = QLabel("Open a PDF to start editing")
        headline.setAlignment(Qt.AlignCenter)
        headline.setFont(theme.display_font(33))
        headline.setStyleSheet(f"color: {theme.TEXT_PRIMARY};")
        subtext = QLabel("Click any line of text to edit it in place.")
        subtext.setAlignment(Qt.AlignCenter)
        subtext.setFont(theme.ui_font(13))
        subtext.setStyleSheet(f"color: {theme.TEXT_SECONDARY};")
        button = QPushButton("Open PDF…")
        button.setObjectName("PrimaryButton")
        button.setCursor(Qt.PointingHandCursor)
        button.setFixedHeight(36)
        button.setMinimumWidth(160)
        button.setStyleSheet(_PRIMARY_BUTTON_QSS)
        button.clicked.connect(self._on_open)
        hint = QLabel("or drag a PDF here")
        hint.setAlignment(Qt.AlignCenter)
        hint.setFont(theme.ui_font(12))
        hint.setStyleSheet(f"color: {theme.TEXT_SECONDARY};")
        for w, sp in ((tile, 22), (headline, 8), (subtext, 22),
                      (button, 14), (hint, 0)):
            v.addWidget(w, 0, Qt.AlignCenter)
            if sp:
                v.addSpacing(sp)
        return host

    # --- gallery (with recents) -------------------------------------------
    def _build_gallery(self) -> QWidget:
        host = QWidget()
        v = QVBoxLayout(host)
        v.setContentsMargins(44, 36, 44, 28)
        v.setSpacing(0)

        header = QHBoxLayout()
        greet_col = QVBoxLayout()
        greet_col.setSpacing(2)
        greeting = QLabel("Welcome back")
        greeting.setFont(theme.ui_font(20, semibold=True))
        greeting.setStyleSheet(f"color: {theme.TEXT_PRIMARY};")
        sub = QLabel("Open a document, or pick up where you left off.")
        sub.setFont(theme.ui_font(13))
        sub.setStyleSheet(f"color: {theme.TEXT_SECONDARY};")
        greet_col.addWidget(greeting)
        greet_col.addWidget(sub)
        header.addLayout(greet_col)
        header.addStretch(1)
        open_btn = QPushButton("Open PDF…")
        open_btn.setObjectName("PrimaryButton")
        open_btn.setCursor(Qt.PointingHandCursor)
        open_btn.setFixedHeight(36)
        open_btn.setMinimumWidth(130)
        open_btn.setStyleSheet(_PRIMARY_BUTTON_QSS)
        open_btn.clicked.connect(self._on_open)
        header.addWidget(open_btn, 0, Qt.AlignVCenter)
        v.addLayout(header)
        v.addSpacing(22)

        eyebrow_row = QHBoxLayout()
        title = QLabel("RECENT")
        title.setFont(theme.caps_header_font())
        title.setStyleSheet(f"color: {theme.PANEL_HEADER};")
        eyebrow_row.addWidget(title, 0, Qt.AlignVCenter)
        eyebrow_row.addStretch(1)
        self._search = QLineEdit()
        self._search.setObjectName("RecentSearch")
        self._search.setPlaceholderText("Search recent files")
        self._search.setClearButtonEnabled(True)
        self._search.setFixedWidth(220)
        self._search.setStyleSheet(_SEARCH_QSS)
        self._search.textChanged.connect(self._apply_filter)
        eyebrow_row.addWidget(self._search, 0, Qt.AlignVCenter)
        v.addLayout(eyebrow_row)
        v.addSpacing(12)

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.NoFrame)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._scroll.viewport().setStyleSheet("background: transparent;")
        self._grid_host = QWidget()
        self._grid_host.setStyleSheet("background: transparent;")
        self._grid = QGridLayout(self._grid_host)
        self._grid.setContentsMargins(0, 0, 0, 0)
        self._grid.setHorizontalSpacing(_CARD_GAP)
        self._grid.setVerticalSpacing(_CARD_GAP)
        self._grid.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        self._scroll.setWidget(self._grid_host)
        v.addWidget(self._scroll, 1)

        v.addSpacing(8)
        self._empty_filter = QLabel("No recent files match your search.")
        self._empty_filter.setFont(theme.ui_font(12))
        self._empty_filter.setStyleSheet(f"color: {theme.TEXT_SECONDARY};")
        self._empty_filter.hide()
        v.addWidget(self._empty_filter, 0, Qt.AlignLeft)

        self._clear_link = QPushButton("Clear Recents")
        self._clear_link.setObjectName("ClearRecents")
        self._clear_link.setCursor(Qt.PointingHandCursor)
        self._clear_link.setIcon(make_icon("clear_recent",
                                            color=theme.TEXT_SECONDARY))
        self._clear_link.setIconSize(QSize(14, 14))
        self._clear_link.setStyleSheet(_CLEAR_RECENTS_QSS)
        if self._on_clear_recents is not None:
            self._clear_link.clicked.connect(self._on_clear_recents)
        v.addWidget(self._clear_link, 0, Qt.AlignLeft)
        return host

    # --- population -------------------------------------------------------
    def set_recents(self, paths: list, pinned=None) -> None:
        """Rebuild the gallery from ``paths`` (already filtered to existing
        files; pinned-first ordering decided by the caller). ``pinned`` is the
        set of pinned paths so each card draws its star correctly. An empty list
        shows the hero call-to-action instead."""
        self._pinned = set(pinned or ())
        self._loader.reset()
        for card in self._cards:
            card.setParent(None)
            card.deleteLater()
        self._cards = []
        self._by_path = {}

        if not paths:
            self._gallery.hide()
            self._hero.show()
            return
        self._hero.hide()
        self._gallery.show()
        # The search field only earns its space once the list is long enough to
        # be worth filtering.
        self._search.setVisible(len(paths) > 8)
        if not self._search.isVisible():
            self._search.clear()

        for path in paths:
            card = RecentCard(path, path in self._pinned,
                              self._on_open_recent, self._handle_remove,
                              self._handle_toggle_pin)
            self._cards.append(card)
            self._by_path[path] = card
            self._loader.request(path)
        self._apply_filter()

    def _visible_cards(self) -> list:
        q = self._search.text().strip().lower() if self._search.isVisible() else ""
        out = []
        for card in self._cards:
            match = (not q) or q in os.path.basename(card.path).lower()
            card.setVisible(match)
            if match:
                out.append(card)
        return out

    def _apply_filter(self) -> None:
        visible = self._visible_cards()
        self._empty_filter.setVisible(bool(self._cards) and not visible)
        self._reflow(visible)

    def _reflow(self, cards=None) -> None:
        if cards is None:
            cards = [c for c in self._cards if c.isVisible()]
        while self._grid.count():
            self._grid.takeAt(0)
        avail = self._scroll.viewport().width() if hasattr(self, "_scroll") else 0
        cols = max(1, (avail + _CARD_GAP) // (_CARD_W + _CARD_GAP)) if avail else 3
        for i, card in enumerate(cards):
            self._grid.addWidget(card, i // cols, i % cols,
                                 Qt.AlignTop | Qt.AlignLeft)

    def _on_thumb_ready(self, path: str, pm: QPixmap) -> None:
        card = self._by_path.get(path)
        if card is not None:
            card.set_thumbnail(pm)

    def _handle_remove(self, path: str) -> None:
        if self._on_remove_recent is not None:
            self._on_remove_recent(path)

    def _handle_toggle_pin(self, path: str) -> None:
        if self._on_toggle_pin is not None:
            self._on_toggle_pin(path)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if self._gallery.isVisible():
            self._reflow()


def _elide(text: str, font, width: int) -> str:
    """Middle-truncate ``text`` to ``width`` px in ``font`` with an ellipsis."""
    from PySide6.QtGui import QFontMetrics
    return QFontMetrics(font).elidedText(text, Qt.ElideRight, max(width, 20))


# ASSUMPTION: theme.py (owned by another developer) does not expose a
# primary-button QSS helper, so the empty-state button style is defined here
# from theme tokens. If theme adds one, swap this constant for it.
_PRIMARY_BUTTON_QSS = f"""
QPushButton#PrimaryButton {{
    background: {theme.ACCENT_FILL};
    color: #FFFFFF;
    border: none;
    border-radius: 8px;
    padding: 8px 22px;
    font-size: 14px;
    font-weight: 600;
}}
QPushButton#PrimaryButton:hover {{ background: {theme.ACCENT_PRESSED}; }}
QPushButton#PrimaryButton:pressed {{ background: {theme.ACCENT_DEEP}; }}
"""


# A Recent gallery card: a tile that brightens its border under the pointer
# (the [hover="true"] dynamic property is toggled in RecentCard).
_CARD_QSS = f"""
QFrame#RecentCard {{
    background: {_CARD_BG};
    border: 1px solid {theme.CHROME_BORDER};
    border-radius: {_CARD_RADIUS}px;
}}
QFrame#RecentCard[hover="true"] {{
    background: {_CARD_BG_HOVER};
    border: 1px solid {theme.ACCENT_BORDER};
}}
"""


# The small round star / × controls that float over a card's thumbnail.
_CARD_OVERLAY_QSS = """
QPushButton#CardOverlay {
    background: rgba(0,0,0,.55);
    color: #FFFFFF;
    border: none;
    border-radius: 11px;
    font-size: 12px;
}
QPushButton#CardOverlay:hover { background: rgba(0,0,0,.78); }
"""


# The gallery's recent-file search field.
_SEARCH_QSS = f"""
QLineEdit#RecentSearch {{
    background: {theme.CONTROL_FILL};
    border: 1px solid {theme.CHROME_BORDER};
    border-radius: 7px;
    padding: 5px 9px;
    color: {theme.TEXT_PRIMARY};
    font-size: 12px;
}}
QLineEdit#RecentSearch:focus {{ border: 1px solid {theme.ACCENT_BORDER}; }}
"""


# The "Clear Recents" link: quiet text that only reads as a control on hover
# (it is housekeeping, not a call to action). Uses a light wash so it is
# visible on the dark canvas.
_CLEAR_RECENTS_QSS = f"""
QPushButton#ClearRecents {{
    border: none;
    border-radius: 6px;
    padding: 4px 10px;
    background: transparent;
    color: {theme.TEXT_SECONDARY};
    font-size: 12px;
}}
QPushButton#ClearRecents:hover {{
    background: {theme.WASH_HOVER};
    color: {theme.TEXT_PRIMARY};
}}
"""


# Below this window width the Pages sidebar auto-hides so the document keeps
# its room (responsive chrome); above it the sidebar returns.
_PAGES_AUTOHIDE_W = 1024

# The LEFT Format dock width SCALES with the window: it shrinks toward
# _LEFT_DOCK_MIN as the window narrows (yielding room to the document) and grows
# toward _LEFT_DOCK_MAX when wide. It never disappears -- the Format controls
# stay reachable at every size, just narrower on a small window. The content
# width is RAIL_WIDTH + these (the rail is always 64).
_LEFT_DOCK_MIN_CONTENT = 210      # narrowest Format column on a small window
                                  # (font name + controls still readable)
_LEFT_DOCK_MAX_CONTENT = 252      # never "huge": at a typical window it sits
                                  # below the old fixed width, growing to this
                                  # only on a very wide screen
_LEFT_DOCK_FULL_W = 1640          # window width at which it reaches the max


def _mark_file_recently_used(path: str) -> None:
    """Tell macOS this PDF was used, so it appears in Finder's **Recents** and the
    Dock icon's **Recent Documents**. The app reads/writes PDFs as raw bytes
    (fitz), so unlike a Finder double-click macOS never records the usage itself.
    Two independent parts, each best-effort (a failure never blocks open/save):

      1. Finder "Recents" sorts on ``kMDItemLastUsedDate``, which Spotlight reads
         from the ``com.apple.lastuseddate#PS`` extended attribute (a 16-byte
         ``struct timespec``). We write it via libc ``setxattr`` (``os.setxattr``
         is Linux-only). Verified: Spotlight reflects it within seconds.
      2. The Dock icon's Recent Documents come from
         ``[[NSDocumentController sharedDocumentController]
         noteNewRecentDocumentURL:url]`` -- called the way an NSDocument app would.

    No-op off macOS, and off the headless/test platform so suites never write the
    real user's recents."""
    import sys
    if sys.platform != "darwin":
        return
    if os.environ.get("QT_QPA_PLATFORM", "") == "offscreen":
        return     # headless tests: don't touch the real macOS recents
    path = os.path.abspath(path)
    if not os.path.isfile(path):
        return
    import ctypes
    import ctypes.util
    # (1) Finder "Recents": the last-used-date xattr.
    try:
        import struct
        import time
        libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
        libc.setxattr.argtypes = [ctypes.c_char_p, ctypes.c_char_p,
                                  ctypes.c_void_p, ctypes.c_size_t,
                                  ctypes.c_uint32, ctypes.c_int]
        libc.setxattr.restype = ctypes.c_int
        now = time.time()
        secs = int(now)
        val = struct.pack("<qq", secs, int((now - secs) * 1e9))
        libc.setxattr(os.fsencode(path), b"com.apple.lastuseddate#PS",
                      val, len(val), 0, 0)
    except Exception:  # noqa: BLE001 - cosmetic; never block open/save
        pass
    # (2) Dock icon Recent Documents: NSDocumentController.
    try:
        objc = ctypes.cdll.LoadLibrary(ctypes.util.find_library("objc"))
        objc.objc_getClass.restype = ctypes.c_void_p
        objc.objc_getClass.argtypes = [ctypes.c_char_p]
        objc.sel_registerName.restype = ctypes.c_void_p
        objc.sel_registerName.argtypes = [ctypes.c_char_p]
        msg = objc.objc_msgSend

        def call(recv, sel, *args, argtypes=None, restype=ctypes.c_void_p):
            msg.restype = restype
            msg.argtypes = [ctypes.c_void_p, ctypes.c_void_p] + (argtypes or [])
            return msg(recv, objc.sel_registerName(sel), *args)

        nsstr = call(objc.objc_getClass(b"NSString"), b"stringWithUTF8String:",
                     path.encode("utf-8"), argtypes=[ctypes.c_char_p])
        url = call(objc.objc_getClass(b"NSURL"), b"fileURLWithPath:", nsstr,
                   argtypes=[ctypes.c_void_p])
        dc = call(objc.objc_getClass(b"NSDocumentController"),
                  b"sharedDocumentController")
        if dc and url:
            call(dc, b"noteNewRecentDocumentURL:", url,
                 argtypes=[ctypes.c_void_p], restype=None)
    except Exception:  # noqa: BLE001 - cosmetic; never block open/save
        pass


# ===========================================================================
# The window.
# ===========================================================================
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        # Kick the one-time system font-index scan onto a background thread
        # NOW, so the first paint does not pay it on the GUI thread and the
        # index is (usually) warm before the first edit resolves a font
        # (perf foundation M4a). Safe: the scan is pure fontTools + os.
        FontEngine.prewarm_system_index()
        # Multiple open documents live in the Workspace; ``self.document`` is a
        # read-only alias for the active one (PAGES_SPEC §6.1) so the existing
        # methods that read ``self.document`` keep working through a one-line
        # property shim.
        self.workspace = Workspace()

        self.setWindowTitle("PDF Text Editor")
        self.setMinimumSize(theme.WINDOW_MIN_W if hasattr(theme, "WINDOW_MIN_W") else 900,
                            theme.WINDOW_MIN_H if hasattr(theme, "WINDOW_MIN_H") else 680)
        self.resize(theme.WINDOW_DEFAULT_W if hasattr(theme, "WINDOW_DEFAULT_W") else 1180,
                    theme.WINDOW_DEFAULT_H if hasattr(theme, "WINDOW_DEFAULT_H") else 920)
        self.setStyleSheet(theme.global_stylesheet())

        # Undo stack: commands delegate to the model (BUILD_SPEC §6.4).
        self.undo_stack = QUndoStack(self)

        # Central area: a document tab strip ABOVE the shared canvas (PAGES_SPEC
        # §5.2). The tab bar is hidden when <= 1 doc is open, so the common case
        # looks tabless.
        central = QWidget()
        central.setObjectName("CentralArea")
        col = QVBoxLayout(central)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(0)
        self.tab_bar = DocumentTabBar()
        # Wrap the tab bar in a full-width strip so the tabs stay LEFT-anchored
        # (a bare QTabBar centers its tabs in the leftover space on macOS). The
        # strip carries the bar background + bottom border and spans the column;
        # the bar hugs its tabs at the left, the stretch fills the rest.
        self._tab_strip = QWidget()
        self._tab_strip.setObjectName("DocumentTabStrip")
        _tsl = QHBoxLayout(self._tab_strip)
        # Breathing room ABOVE the tabs (the tabs themselves carry no top margin,
        # so their rect == drawn box and the painted glyphs centre exactly).
        _tsl.setContentsMargins(0, 5, 0, 0)
        _tsl.setSpacing(0)
        self.tab_bar.setSizePolicy(QSizePolicy.Policy.Maximum,
                                   QSizePolicy.Policy.Preferred)
        _tsl.addWidget(self.tab_bar)
        _tsl.addStretch(1)
        self.tab_bar._strip = self._tab_strip
        self._tab_strip.hide()
        col.addWidget(self._tab_strip)
        self.canvas = CanvasContainer(self)
        self.view = self.canvas.view
        col.addWidget(self.canvas, 1)
        self.setCentralWidget(central)

        # Empty-state overlay, parented on the canvas so it tracks its geometry.
        self.empty_state = EmptyState(self.open_pdf, self.open_path,
                                      self._clear_recents, self._remove_recent,
                                      self._toggle_pin)
        self.empty_state.setParent(self.canvas)
        self._refresh_empty_recents()
        self.empty_state.show()

        # Window-wide drag-drop (navigation M3): drops on the toolbar, docks,
        # or status bar delegate to the canvas handlers below, so a PDF
        # dropped ANYWHERE on the window opens.
        self.setAcceptDrops(True)

        self._build_actions()
        self._build_page_actions()
        self._build_toolbar()
        self._build_menubar()
        self._build_annotate_menu()
        self._build_objects_menu()
        self._build_left_dock()
        self._build_pages_dock()
        self._build_statusbar()
        self._wire_view()
        self._wire_undo()
        self._wire_tabs_and_sidebar()
        self._wire_left_panel()

        # Crop scope confirmation (ws6 §2.6): an injectable provider (the §1
        # seam rule, like _password_provider below) so offscreen tests confirm
        # the drag flow without an un-dismissable exec(). Signature:
        # (page_index, rect, page_count) -> "page" | "all" | None (cancel).
        self._crop_scope_provider = self._crop_scope_dialog
        # Password prompting (ws6 §2.7): the same injectable shape, so tests
        # open encrypted fixtures without a modal. Signature:
        # (path, attempt 1..3) -> password | None (cancel).
        self._password_provider = self._ask_password

        self._suppress_close_guard = False

        # Crash-survival persistence (off until main() enables it, so headless
        # test windows never write the real preferences). When on, geometry and
        # the open-tab list are flushed on a debounced move/resize/tab change and
        # one last time on aboutToQuit, so a force-quit or crash still reopens at
        # the last size with the last session -- not just the clean-close path.
        self._persist_enabled = False
        self._geo_timer = QTimer(self)
        self._geo_timer.setSingleShot(True)
        self._geo_timer.setInterval(700)
        self._geo_timer.timeout.connect(self._flush_persisted_state)

        # Crash recovery (autosave): while a doc is dirty, a baked copy is
        # written to the recovery dir every interval so a crash never loses more
        # than the last ~45s of edits. ``_autosave_sigs`` skips rewriting a doc
        # that has not changed since its last copy.
        self._autosave_sigs: dict[int, tuple] = {}
        self._autosave_timer = QTimer(self)
        self._autosave_timer.setInterval(45000)
        self._autosave_timer.timeout.connect(self._autosave_tick)

        self._sync_all()

    # The active document (PAGES_SPEC §6.1): an alias so the existing
    # ``self.document`` reads keep working against the workspace's active doc.
    @property
    def document(self) -> PDFDocument | None:
        return self.workspace.active

    @document.setter
    def document(self, doc: "PDFDocument | None") -> None:
        """Install ``doc`` as the active workspace document IN PLACE (replacing
        the active slot, else appending), without touching the rest of the chrome.
        Kept so callers that historically assigned ``window.document = ...`` to
        swap the model under the same tab continue to work; the workspace stays
        the single owner of the doc list."""
        if doc is None:
            return
        idx = self.workspace.active_index
        if idx >= 0:
            self.workspace._docs[idx] = doc
        else:
            self.workspace.add_document(doc)

    # ===================================================================
    # Actions + shortcuts (BUILD_SPEC §6.3)
    # ===================================================================
    def _build_actions(self) -> None:
        mk = self._make_action
        self.act_open = mk("Open…", QKeySequence.Open, self.open_pdf, icon="open")
        self.act_save = mk("Save", QKeySequence.Save, self.save_pdf, icon="save")
        self.act_save_as = mk("Save As…", QKeySequence.SaveAs, self.save_pdf_as,
                              icon="save_as")
        # Close Tab is PINNED to the explicit Ctrl+W (Cmd+W on macOS) per the
        # navigation workstream's menu table: QKeySequence.Close resolves to
        # Ctrl+F4 on some platform themes (verified on the offscreen theme),
        # which is not the advertised macOS binding.
        self.act_close = mk("Close Tab", QKeySequence("Ctrl+W"),
                            self.close_active_tab)

        self.act_undo = mk("Undo", QKeySequence.Undo, self.undo_stack.undo, icon="undo")
        # macOS redo is Shift+Cmd+Z; the standard key resolves to Ctrl+Y here, so
        # set it explicitly per BUILD_SPEC §6.3.
        self.act_redo = mk("Redo", QKeySequence("Shift+Ctrl+Z"),
                           self.undo_stack.redo, icon="redo")

        # Add Text (checkable tool) + Delete (BUILD_SPEC §5.2 / §5.5).
        # PINNED (EDITOR_SPEC §9): the Add Text shortcut is "T" -- a single,
        # discoverable key matching the toolbar tooltip; it is a window QAction
        # so it fires regardless of focus, while the view guards it from firing
        # mid text-edit. Toggling drives view.enter/exit_add_text_mode.
        self.act_add_text = QAction("Add Text", self)
        self.act_add_text.setCheckable(True)
        self.act_add_text.setShortcut(QKeySequence("T"))
        self.act_add_text.setIcon(make_icon("add_text"))
        self.act_add_text.toggled.connect(self._on_add_text_toggled)
        # Delete uses the platform Delete sequence; the view-level Delete/
        # Backspace handler covers the in-canvas case, this covers the toolbar
        # button + menu. Guarded so it only fires with a live selection.
        self.act_delete = mk("Delete Box", QKeySequence.Delete,
                             self._delete_selected, icon="delete")

        # Find & Replace (REFLOW_SPEC §R5.1): a window action so Cmd+F works
        # regardless of focus. It activates the Find tool in the left strip,
        # which the window maps to the (placeholder) Find panel for now.
        # "&&" is the Qt escape for a literal ampersand (the ws6 Header &&
        # Footer precedent): a single "&" is a mnemonic marker and was being
        # EATEN by every text-rendering surface (menu row, cheatsheet).
        self.act_find = mk("Find && Replace", QKeySequence.Find,
                           self._activate_find, icon="find")

        self.act_prev = mk("Previous Page", QKeySequence("Alt+Ctrl+Left"),
                           self.prev_page, icon="prev")
        self.act_next = mk("Next Page", QKeySequence("Alt+Ctrl+Right"),
                           self.next_page, icon="next")
        self.act_goto = mk("Go to Page…", QKeySequence("Ctrl+G"),
                           self._focus_page_field)
        self.act_first = mk("First Page", QKeySequence("Ctrl+Up"), self.first_page)
        self.act_last = mk("Last Page", QKeySequence("Ctrl+Down"), self.last_page)

        self.act_zoom_in = mk("Zoom In", QKeySequence.ZoomIn, self.zoom_in,
                              icon="zoom_in")
        self.act_zoom_out = mk("Zoom Out", QKeySequence.ZoomOut, self.zoom_out,
                               icon="zoom_out")
        # Ctrl++ on some layouts needs the '=' alias too.
        self.act_zoom_in.setShortcuts([QKeySequence.ZoomIn, QKeySequence("Ctrl+=")])
        self.act_actual_size = mk("Actual Size", QKeySequence("Ctrl+0"),
                                  lambda: self.set_zoom(1.0))
        self.act_fit_page = mk("Fit Page", QKeySequence("Ctrl+9"), self.fit_page)
        self.act_fit_width = mk("Fit Width", QKeySequence("Ctrl+8"), self.fit_width)

        # Page-nav aliases (PageUp/PageDown), no menu entry.
        self.act_prev.setShortcuts([QKeySequence("Alt+Ctrl+Left"), QKeySequence(Qt.Key_PageUp)])
        self.act_next.setShortcuts([QKeySequence("Alt+Ctrl+Right"), QKeySequence(Qt.Key_PageDown)])

        # Window-menu statics (the ws7 M2 table's non-dynamic entries; the
        # open-document list is rebuilt dynamically by the navigation
        # workstream). Minimize/Zoom follow the macOS Window-menu convention.
        self.act_minimize = mk("Minimize", QKeySequence("Ctrl+M"),
                               self.showMinimized)
        self.act_window_zoom = mk("Zoom", None, self._toggle_window_zoom)
        self.act_next_tab = mk("Next Tab", QKeySequence("Ctrl+Tab"),
                               self.next_tab)
        self.act_prev_tab = mk("Previous Tab", QKeySequence("Ctrl+Shift+Tab"),
                               self.prev_tab)

        # Help menu (navigation M2): the generated shortcut cheatsheet
        # (Cmd+/) and About. Both dialogs are NON-MODAL and cached; AboutRole
        # migrates the About entry to the app menu on native macOS menubars.
        self._shortcuts_dialog: ShortcutsDialog | None = None
        self._about_dialog: AboutDialog | None = None
        self.act_shortcuts = mk("Keyboard Shortcuts…", QKeySequence("Ctrl+/"),
                                self._show_shortcuts, icon="keyboard")
        self.act_about = mk("About PDF Text Editor", None, self._show_about,
                            icon="info")
        self.act_about.setMenuRole(QAction.MenuRole.AboutRole)
        # Settings (Preferences on macOS). PreferencesRole migrates it to the
        # app menu on the native Mac menubar (Cmd+,); on Windows it shows in Help.
        self._settings_dialog = None
        self.act_preferences = mk("Settings…", QKeySequence("Ctrl+,"),
                                  self._show_settings)
        self.act_preferences.setMenuRole(QAction.MenuRole.PreferencesRole)

        self._build_edit_ux_actions()
        self._build_markup_actions()
        self._build_doctools_actions()
        self._build_image_actions()
        self._build_forms_actions()

        # All actions live on the window so shortcuts fire regardless of focus.
        for act in (self.act_open, self.act_save, self.act_save_as, self.act_close,
                    self.act_undo, self.act_redo, self.act_add_text, self.act_delete,
                    self.act_find, self.act_prev, self.act_next,
                    self.act_goto, self.act_first, self.act_last, self.act_zoom_in,
                    self.act_zoom_out, self.act_actual_size, self.act_fit_page,
                    self.act_fit_width, self.act_minimize, self.act_window_zoom,
                    self.act_next_tab, self.act_prev_tab,
                    self.act_shortcuts, self.act_about, self.act_preferences,
                    self.act_bold, self.act_italic,
                    self.act_select_tool, self.act_select_text_tool,
                    self.act_text_edit_tool,
                    self.act_cut, self.act_copy, self.act_paste):
            self.addAction(act)

    def _build_edit_ux_actions(self) -> None:
        """Actions owned by the text-editing UX workstream (ws2 M1), kept in
        one helper so later menu/shortcut workstreams merge cleanly (ws2 §8).

        Bold/Italic (§2.4b) route BY STATE in ``_toggle_style_key``: an open
        inline editor styles the text selection (per-run rich runs, staged on
        commit); else a selected box takes one BoxCommand("style") undo step;
        else no-op. Registering the window QAction makes Cmd+B/I deterministic
        regardless of whether the shortcut map or the focused item wins the
        key -- both paths converge on the same routing.

        Select (V) / Text Edit (E) (§2.7b) register the plain tool keys the
        left strip's tooltips have always advertised, driving the SAME handler
        as a strip click; Select Text (S) (§5.3, M4) joins them for the
        text-select tool. ws2 owns V/S/E creation; ws7 M2 only verifies them.

        Cut/Copy/Paste (§4.5) route BY STATE in ``_on_cut/_on_copy/_on_paste``
        (the same §2.4b convergence rule as Bold/Italic): an open inline
        editor takes the editor clipboard paths (runs payload beside plain
        text, sanitized insert); else the box-level paths (copy box / paste a
        NewBox / Cut = copy + ONE delete command). ws2 owns their creation;
        ws7 M2 only adds editor-aware enablement on top. Native edit widgets
        (find field, inspector inputs) accept ShortcutOverride for the
        standard edit keys, so these window shortcuts never steal from
        them."""
        mk = self._make_action
        self.act_bold = mk("Bold", QKeySequence.Bold,
                           lambda: self._toggle_style_key("bold"))
        self.act_italic = mk("Italic", QKeySequence.Italic,
                             lambda: self._toggle_style_key("italic"))
        self.act_select_tool = mk(
            "Select Tool", QKeySequence("V"),
            lambda: self._on_tool_shortcut("select"))
        # Select Text (S) (ws2 M4 §5.3): the plain tool key, same pattern as
        # T/V/E -- arming routes through the one strip handler.
        self.act_select_text_tool = mk(
            "Select Text Tool", QKeySequence("S"),
            lambda: self._on_tool_shortcut("select_text"))
        self.act_text_edit_tool = mk(
            "Text Edit Tool", QKeySequence("E"),
            lambda: self._on_tool_shortcut("text_edit"))
        self.act_cut = mk("Cut", QKeySequence.Cut, self._on_cut, icon="cut")
        self.act_copy = mk("Copy", QKeySequence.Copy, self._on_copy,
                           icon="copy")
        self.act_paste = mk("Paste", QKeySequence.Paste, self._on_paste,
                            icon="paste")
        # Group / Ungroup boxes (manual override of paragraph detection).
        # Shift-click selects several boxes; Group fuses them into one editable
        # paragraph; Ungroup splits a paragraph back into its lines. No
        # accelerator (kept off the shortcut census); reachable from the Edit
        # menu and the box right-click menu.
        self.act_group = mk(
            "Group Selected Boxes", None,
            lambda: self._on_group_requested(self.view.selected_boxes()))
        self.act_ungroup = mk(
            "Ungroup Box", None,
            lambda: self._on_ungroup_requested(self.view.current_selection()))

    def _build_markup_actions(self) -> None:
        """Checkable markup-tool actions (annotations & markup §5.2), kept in
        one self-contained helper (conflict-surface discipline, §9). Bare-key
        shortcuts (H / U / K / N / D) follow the Add Text 'T' precedent:
        window QActions that never fire while the inline editor has focus
        (the focused editor consumes plain keys). Squiggly is menu-only (no
        toolbar slot) and the shape kinds live behind the Shapes button /
        submenu, so they carry no bare keys. The window also holds the
        per-tool SESSION DEFAULTS the model takes as explicit values (§3.1);
        the M3 Inspector ANNOTATION section edits these in place while a
        tool is armed with nothing selected (§5.5)."""
        _draw = {"stroke": (0.85, 0.1, 0.1), "width": 2.0, "fill": None,
                 "opacity": 1.0}
        self._annot_defaults: dict[str, dict] = {
            "highlight": {"stroke": (1.0, 0.85, 0.0)},
            "underline": {"stroke": (0.13, 0.55, 0.13)},
            "strikeout": {"stroke": (0.8, 0.0, 0.0)},
            "squiggly": {"stroke": (0.8, 0.0, 0.0)},
            "note": {"stroke": (1.0, 0.8, 0.0)},
            "ink": dict(_draw),
            "rect": dict(_draw),
            "ellipse": dict(_draw),
            "line": dict(_draw),
            "arrow": dict(_draw),
        }
        specs = [
            ("highlight", "Highlight", "H", "highlight"),
            ("underline", "Underline", "U", "underline"),
            ("strikeout", "Strikethrough", "K", "strikethrough"),
            ("squiggly", "Squiggly", None, "squiggly"),
            ("note", "Sticky Note", "N", "note"),
            ("ink", "Draw Ink", "D", "ink"),
            ("rect", "Rectangle", None, "rect"),
            ("ellipse", "Ellipse", None, "ellipse"),
            ("line", "Line", None, "line"),
            ("arrow", "Arrow", None, "arrow"),
        ]
        self._markup_actions: dict[str, QAction] = {}
        for kind, title, shortcut, icon in specs:
            act = QAction(title, self)
            act.setCheckable(True)
            if shortcut is not None:
                act.setShortcut(QKeySequence(shortcut))
            act.setIcon(make_icon(icon))
            act.toggled.connect(
                lambda checked, k=kind: self._on_markup_toggled(k, checked))
            self.addAction(act)          # window-level: fires regardless of focus
            self._markup_actions[kind] = act
        self.act_markup_highlight = self._markup_actions["highlight"]
        self.act_markup_underline = self._markup_actions["underline"]
        self.act_markup_strikeout = self._markup_actions["strikeout"]
        self.act_markup_squiggly = self._markup_actions["squiggly"]
        self.act_markup_note = self._markup_actions["note"]
        self.act_markup_ink = self._markup_actions["ink"]
        # Annot selection state + the open note popup (M2): the canvas owns
        # the selection, the window mirrors the record to gate Delete
        # Annotation and to anchor the popup.
        self._selected_annot = None
        self._note_popup: NotePopup | None = None
        self.act_delete_annot = self._make_action(
            "Delete Annotation", None, self._delete_selected_annot)
        self.act_delete_annot.setEnabled(False)
        # Show Comments (M4 §5.4): a checkable View-menu toggle mirroring
        # the left strip's Comments tool (the strip stays the source of
        # truth; ``_sync_show_comments_check`` keeps the toggle honest).
        self._comments_check_syncing = False
        self.act_show_comments = QAction("Show Comments", self)
        self.act_show_comments.setCheckable(True)
        self.act_show_comments.setShortcut(QKeySequence("Ctrl+Shift+C"))
        self.act_show_comments.setProperty(
            "cheatsheet_label", "Show/Hide Comments")
        self.act_show_comments.toggled.connect(self._on_show_comments_toggled)
        self.addAction(self.act_show_comments)

    def _on_markup_toggled(self, kind: str, checked: bool) -> None:
        """Arm / disarm the canvas markup mode for ``kind`` (annotations &
        markup §5.2). Mutually exclusive with Add Text and the other markup
        toggles; idempotent both ways so the modeChanged echo never loops."""
        if checked:
            if self.document is None:
                self._markup_actions[kind].setChecked(False)
                return
            if self.act_add_text.isChecked():
                self.act_add_text.setChecked(False)
            for k, other in self._markup_actions.items():
                if k != kind and other.isChecked():
                    other.setChecked(False)
            fn = getattr(self.view, "enter_annot_mode", None)
            if callable(fn):
                try:
                    fn(kind)             # no-op when already armed for kind
                except (TypeError, RuntimeError, ValueError):
                    pass
        else:
            mode_fn = getattr(self.view, "current_mode", None)
            mode = mode_fn() if callable(mode_fn) else ""
            if mode == self._annot_mode_for(kind):
                fn = getattr(self.view, "exit_annot_mode", None)
                if callable(fn):
                    try:
                        fn()
                    except (TypeError, RuntimeError):
                        pass

    @staticmethod
    def _annot_mode_for(kind: str) -> str:
        """The view mode an armed annot tool reads back: "note" and "ink"
        are their own modes; the shape kinds carry the "shape_" prefix and
        the markup kinds the "markup_" prefix (§4.2)."""
        if kind in ("note", "ink"):
            return kind
        if kind in ("rect", "ellipse", "line", "arrow"):
            return "shape_" + kind
        return "markup_" + kind

    @staticmethod
    def _annot_kind_for_mode(mode: str) -> str | None:
        """The inverse of ``_annot_mode_for``: the tool kind a view mode
        names, or None for a non-annot mode."""
        if mode.startswith("markup_"):
            return mode[len("markup_"):]
        if mode.startswith("shape_"):
            return mode[len("shape_"):]
        if mode in ("note", "ink"):
            return mode
        return None

    def _sync_markup_actions(self, mode: str) -> None:
        """Reflect the view's mode in the markup toggles (the Add Text sync
        pattern): exactly the armed kind's action reads checked. The toggle
        handlers are idempotent, so no signal blocking is needed (and the
        mirrored toolbar buttons stay in sync through the action)."""
        kind = self._annot_kind_for_mode(mode)
        for k, act in getattr(self, "_markup_actions", {}).items():
            want = (k == kind)
            if act.isChecked() != want:
                act.setChecked(want)

    def _build_doctools_actions(self) -> None:
        """Actions owned by the page & document tools workstream (ws6 M1),
        kept in one helper so later menu/shortcut workstreams merge cleanly
        (ws6 §6). Each handler follows the dialog-seam rule (§1): the lambda
        calls it with NO arguments so the QAction's ``triggered(checked)``
        bool can never masquerade as the seam argument, and tests pass
        explicit options instead of dismissing a modal. Registered on the
        window so Cmd+D / Cmd+P fire regardless of focus; ``_sync_actions``
        disables them all in the empty state."""
        mk = self._make_action
        self.act_properties = mk(
            "Document Properties…", QKeySequence("Ctrl+D"),
            lambda: self._do_properties())
        self.act_export_images = mk(
            "Pages as Images…", None, lambda: self._do_export_images())
        self.act_export_text = mk(
            "All Text to .txt…", None, lambda: self._do_export_text())
        # Security / optimize / print (ws6 M4): Print + Save Optimized Copy
        # join the File menu at the file_output anchor; Security joins the
        # Document menu at doc_file.
        self.act_print = mk(
            "Print…", QKeySequence("Ctrl+P"), lambda: self._do_print())
        self.act_optimize = mk(
            "Save Optimized Copy…", None, lambda: self._do_optimize())
        self.act_security = mk(
            "Security…", None, lambda: self._do_security())
        # Stamps (ws6 M2): Document-menu entries at the doc_decorate anchor.
        self.act_watermark = mk(
            "Add Watermark…", None, lambda: self._do_watermark())
        self.act_header_footer = mk(
            "Add Header && Footer…", None, lambda: self._do_header_footer())
        # Crop (ws6 M3): a CHECKABLE mode action (the Add Text convention) --
        # checking arms the canvas crop mode; the apply itself runs through
        # _do_crop_apply once a rect is drawn and the scope is confirmed.
        self.act_crop = QAction("Crop Pages…", self)
        self.act_crop.setCheckable(True)
        self.act_crop.toggled.connect(self._on_crop_toggled)
        # OCR (OCR_SPEC): turn scanned/image-only pages into editable text in a
        # font built from the scanned glyphs. Page + whole-document scope.
        self.act_ocr_page = mk(
            "OCR This Page", None, lambda: self._do_ocr(scope="page"))
        self.act_ocr_document = mk(
            "OCR Document", None, lambda: self._do_ocr(scope="document"))
        for act in (self.act_properties, self.act_export_images,
                    self.act_export_text, self.act_print, self.act_optimize,
                    self.act_security, self.act_watermark,
                    self.act_header_footer, self.act_crop,
                    self.act_ocr_page, self.act_ocr_document):
            self.addAction(act)

    def _build_forms_actions(self) -> None:
        """Actions owned by the forms workstream (ws5 M3): Export Flattened
        Copy joins the File menu at the ``file_output`` anchor. Enablement
        is FORM-gated in ``_sync_actions`` (disabled on form-free docs --
        flattening a widget-free file is a no-op copy), and the handler
        keeps the dialog/work split of the doc-tools convention so the
        offscreen suite drives ``_do_export_flattened`` directly."""
        self.act_flatten = self._make_action(
            "Export Flattened Copy…", None, lambda: self._export_flattened())
        self.addAction(self.act_flatten)

    def _build_image_actions(self) -> None:
        """Actions owned by the images & signatures workstream (ws4 M1/M2),
        kept in one helper so later menu/shortcut workstreams merge cleanly
        (ws4 §10). Every handler follows the dialog-seam rule (doc tools
        §1): the lambdas call them with NO arguments and tests pass explicit
        paths / swap the factory instead of dismissing modals."""
        self.act_insert_image = self._make_action(
            "Image from File…", QKeySequence("Ctrl+Shift+I"),
            lambda: self._do_insert_image())
        self.act_insert_image.setIcon(make_icon("image"))
        self.addAction(self.act_insert_image)
        # Signatures & stamps (ws4 M2, §4/§6). Two injectable seams: the
        # library (dir-injectable, constructed ONCE -- lazy, so the default
        # ``~/.pdftexteditor`` folder is never created until a save) and the
        # dialog factory (defaults to the real class; offscreen tests swap a
        # stub so no exec() runs headless).
        self._signature_library = SignatureLibrary()
        self._signature_dialog_factory = SignatureDialog
        self.act_draw_signature = self._make_action(
            "Draw Signature…", QKeySequence("Ctrl+Shift+G"),
            lambda: self._do_draw_signature())
        self.act_draw_signature.setIcon(make_icon("signature"))
        self.act_signature_from_file = self._make_action(
            "Signature from File…", None,
            lambda: self._do_signature_from_file())
        self.act_manage_signatures = self._make_action(
            "Manage Signatures…", None,
            lambda: self._do_manage_signatures())
        for act in (self.act_draw_signature, self.act_signature_from_file,
                    self.act_manage_signatures):
            self.addAction(act)

    def _do_insert_image(self, path: str | None = None) -> None:
        """Tools > Image from File… (images & signatures §4): pick a
        PNG/JPEG and arm the canvas placement mode. ``path`` is the
        offscreen-test seam (no dialog when given)."""
        if self.document is None:
            return
        if path is None:
            path, _ = QFileDialog.getOpenFileName(
                self, "Insert Image", "", "Images (*.png *.jpg *.jpeg)")
        if not path:
            return
        self._place_image_from_file(path)

    def _place_image_from_file(self, path: str, kind: str = "file") -> bool:
        """Read an image file into a placement payload and arm the canvas
        place mode (shared by the menu actions and the canvas drop handler).
        Rejects unreadable/invalid files with a flash; returns True when the
        mode armed."""
        try:
            with open(path, "rb") as fh:
                data = fh.read()
        except OSError:
            self._flash_error(f"Could not read {os.path.basename(path)}")
            return False
        is_png_or_jpeg = (data.startswith(b"\x89PNG\r\n\x1a\n")
                          or data.startswith(b"\xff\xd8\xff"))
        if not is_png_or_jpeg:
            self._flash_error(
                f"{os.path.basename(path)} is not a valid PNG/JPEG image")
            return False
        return self._arm_image_payload(
            data, kind,
            bad_msg=f"{os.path.basename(path)} is not a valid PNG/JPEG image")

    def _arm_image_payload(self, data: bytes, kind: str,
                           bad_msg: str | None = None) -> bool:
        """Arm the canvas place mode with raw image bytes (the one funnel
        for file inserts, drops, signatures, and stamps -- §4): decode the
        natural pixel size, flash on an undecodable payload, toast the
        gesture hint. Returns True when the mode armed."""
        img = QImage.fromData(data)
        if img.isNull():
            self._flash_error(bad_msg or "Not a valid image")
            return False
        fn = getattr(self.view, "enter_place_image_mode", None)
        if not callable(fn):
            return False
        fn({"image": data, "natural_px": (img.width(), img.height()),
            "kind": kind})
        noun = {"signature": "signature", "stamp": "stamp"}.get(kind, "image")
        self._mode_hint("place_image", f"Click or drag to place the {noun}")
        return True

    # -- signatures & stamps (images & signatures M2, §4/§6) -------------
    def _do_draw_signature(self) -> None:
        """Tools > Signature > Draw Signature… (Cmd+Shift+G): run the draw
        dialog through the ``_signature_dialog_factory`` seam, optionally
        save the result to the library, then arm placement. A blank canvas
        (empty ``result_png``) is a quiet no-op after a flash."""
        if self.document is None:
            return
        dlg = self._signature_dialog_factory(self)
        if not dlg.exec():
            return
        png = dlg.result_png()
        if not png:
            self._flash_error("Draw or load a signature first")
            return
        if dlg.save_to_library():
            self._signature_library.save(
                dlg.signature_name() or "Signature", png)
        self._arm_image_payload(png, "signature")

    def _do_signature_from_file(self, path: str | None = None) -> None:
        """Tools > Signature > Signature from File… -- the Image-from-File
        flow with kind="signature" (no library save; one-off placement).
        ``path`` is the offscreen-test seam."""
        if self.document is None:
            return
        if path is None:
            path, _ = QFileDialog.getOpenFileName(
                self, "Signature from File", "",
                "Images (*.png *.jpg *.jpeg)")
        if not path:
            return
        self._place_image_from_file(path, kind="signature")

    def _do_manage_signatures(self) -> None:
        """Tools > Signature > Manage Signatures…: open the library folder
        (created on demand) in the file manager -- rename/delete there; the
        menu re-lists on every open (aboutToShow)."""
        QDesktopServices.openUrl(
            QUrl.fromLocalFile(self._signature_library.ensure_dir()))

    def _place_signature_from_library(self, path: str) -> None:
        """A Signature-menu library entry: load the stored PNG and arm
        placement (one click from menu to page, §4)."""
        if self.document is None:
            return
        try:
            data = self._signature_library.load(path)
        except OSError:
            self._flash_error(
                f"Could not read {os.path.basename(path)} -- was it removed?")
            return
        self._arm_image_payload(data, "signature")

    def _do_place_stamp(self, text: str) -> None:
        """Tools > Stamp > <kind>: generate the procedural stamp PNG and arm
        placement (kind="stamp" rides the ImageBox metadata)."""
        if self.document is None:
            return
        self._arm_image_payload(stamps.stamp_png(text), "stamp")

    def _on_copy(self) -> None:
        """Edit > Copy / Cmd+C (§4.5), routed by state: an open inline editor
        copies its text selection (plain text + the x-pdfte-runs payload);
        the armed TEXT SELECT tool copies its word selection off the page
        (plain text, with the "N words copied" toast -- §5.2/§5.3, taking
        precedence over box copy); else the selected box copies whole
        (in-process clipboard + both system formats). No applicable target:
        no-op."""
        if self.document is None:
            return
        view = self.view
        if getattr(view, "_editor", None) is not None:
            view.editor_copy_selection()
            return
        if getattr(view, "current_mode", lambda: "")() == "select_text":
            fn = getattr(view, "copy_text_selection", None)
            n = fn() if callable(fn) else 0
            if n:
                self._toast(f"{n} word{'s' if n != 1 else ''} copied")
            return
        view.copy_selection()

    def _on_cut(self) -> None:
        """Edit > Cut / Cmd+X (§4.5): the §4.5 Copy, then remove. Editor open:
        the selection is removed in-editor (folds into the eventual commit's
        one undo step). Else: copy the selected box, then ONE
        BoxCommand("delete") -- a single undo step, the copy itself is not
        undoable state."""
        if self.document is None:
            return
        view = self.view
        if getattr(view, "_editor", None) is not None:
            view.editor_cut_selection()
            return
        if view.copy_selection():
            view.delete_selected()

    def _on_paste(self) -> None:
        """Edit > Paste / Cmd+V (§4.5), routed by state: an open inline editor
        inserts at its cursor (our runs keep bold/italic only; foreign text is
        whitespace-normalized -- the §4.3 style-sanity rule); else the canvas
        paste creates ONE NewBox via the §4.4 fallback chain (in-process box
        clipboard, then the system clipboard's runs payload, then plain
        text)."""
        if self.document is None:
            return
        view = self.view
        if getattr(view, "_editor", None) is not None:
            view.editor_paste()
        else:
            view.paste()

    def _toggle_style_key(self, key: str) -> None:
        """Cmd+B / Cmd+I, routed by state (ws2 M1 §2.4b).

        Editor open: toggle the text selection's weight/slant via the per-run
        styling path (one RichEditCommand when the edit commits). The toggle
        target reads from the editor cursor's char format so repeated presses
        alternate. When the per-run path declines (a NewBox / uniform-only
        editor), the edit is committed first (so typed text is never lost) and
        the WHOLE BOX toggles below.

        No editor, box selected: toggle the box's effective bold/italic via
        ``view.apply_style`` -> exactly one BoxCommand("style") undo step.

        Nothing applicable: no-op."""
        if self.document is None:
            return
        view = self.view
        editor = getattr(view, "_editor", None)
        if editor is not None:
            flag_fn = getattr(editor, "_cursor_style_flag", None)
            target = (not flag_fn(key)) if callable(flag_fn) else True
            sel_fn = getattr(view, "apply_style_to_selection", None)
            if callable(sel_fn) and sel_fn({key: target}):
                return
            # NewBox / uniform-only editor: commit the open edit, then fall
            # through to the whole-box route on the (still selected) box.
            self._flush_open_editor()
        box = self._current_selection()
        if box is None:
            return
        page = getattr(box, "page_index", self.view_page_index())
        try:
            current = bool(self.document.effective_style(page, box).get(key))
        except Exception:  # noqa: BLE001 - a stale box ref must not crash
            return
        fn = getattr(view, "apply_style", None)
        if callable(fn):
            fn({key: not current})

    def _on_tool_shortcut(self, tool_id: str) -> None:
        """The V / E tool keys (§2.7b): arm the tool exactly like clicking it
        in the left strip (same handler + strip highlight). Guarded no-op
        while the inline editor is typing -- the focused editor consumes plain
        keys, so the shortcut must never steal a letter mid-edit -- and with
        no document open."""
        if self.document is None:
            return
        if getattr(self.view, "_editor", None) is not None:
            return
        if hasattr(self, "left_panel"):
            self.left_panel.set_active_tool(tool_id)
        self._on_tool_selected(tool_id)

    def _make_action(self, text, shortcut, slot, *, icon: str | None = None) -> QAction:
        act = QAction(text, self)
        if shortcut is not None:
            act.setShortcut(shortcut)
        if icon:
            act.setIcon(make_icon(icon))
        act.triggered.connect(slot)
        return act

    # ===================================================================
    # Toolbar (BUILD_SPEC §6.2)
    # ===================================================================
    def _build_toolbar(self) -> None:
        bar = QToolBar("Main")
        bar.setObjectName("MainToolbar")
        bar.setMovable(False)
        bar.setFloatable(False)
        bar.setIconSize(QSize(theme.ICON_SIZE, theme.ICON_SIZE))
        bar.setToolButtonStyle(Qt.ToolButtonIconOnly)
        bar.setFixedHeight(theme.TOOLBAR_HEIGHT)
        self.addToolBar(Qt.TopToolBarArea, bar)
        self.toolbar = bar

        # Charcoal Studio top bar: the document title is LEFT-anchored (a saved /
        # edited dot + filename + "page N of M"); the editing tools live in the
        # left activity rail; the right side carries the global actions -- undo /
        # redo, a compact zoom stepper, Share, and the primary Save CTA. Open
        # stays in the File menu + the empty-state Open button (it is a one-time
        # entry, not a constant top-bar control in this design).

        # Left-anchored document title: saved/edited dot + filename + page meta.
        titlewrap = QWidget()
        twl = QHBoxLayout(titlewrap)
        # Left breathing room so the status dot is not jammed against the edge.
        twl.setContentsMargins(12, 0, 0, 0)
        # Tight grouping: the saved/edited dot HUGS the filename (one unit), and
        # the page meta sits a touch further out as secondary info -- explicit
        # gaps instead of one loose uniform spacing that detached the dot.
        twl.setSpacing(0)
        self.dirty_dot = QLabel()
        self.dirty_dot.setFixedSize(8, 8)
        self.dirty_dot.hide()
        self.filename_label = QLabel("No document")
        self.filename_label.setObjectName("TitlebarDoc")
        self.filename_label.setFont(theme.ui_font(13, medium=True))
        self.titlebar_meta = QLabel("")
        self.titlebar_meta.setObjectName("TitlebarMeta")
        self.titlebar_meta.setFont(theme.mono_font(12))
        twl.addWidget(self.dirty_dot)
        twl.addSpacing(7)
        twl.addWidget(self.filename_label)
        twl.addSpacing(10)
        twl.addWidget(self.titlebar_meta)
        bar.addWidget(titlewrap)

        # Right spacer.
        right_spacer = QWidget()
        right_spacer.setSizePolicy(QSizePolicy.Policy.Expanding,
                                   QSizePolicy.Policy.Preferred)
        bar.addWidget(right_spacer)

        # The right cluster (undo / redo | zoom stepper | Share | Save) lives in
        # ONE container so the toolbar does not stretch the controls to the full
        # bar height -- they stay compact 30px pills, vertically centered, like
        # the widget's .cs-globals. (Adding buttons straight to a QToolBar makes
        # it fill them to the bar's height -- the "full-height buttons" bug.)
        right = QWidget()
        rl = QHBoxLayout(right)
        # Right margin so the Save pill is not jammed against the window edge
        # (the toolbar's own QSS padding is not reliably honoured for the last
        # widget); this is the breathing room to the right of the CTA.
        rl.setContentsMargins(0, 0, 14, 0)
        # ONE consistent gap between every control (undo · redo | zoom | share |
        # Save) instead of the old ad-hoc 3/4/2/4 mix -- the "inconsistent ugly
        # spacing" the cluster used to read as.
        rl.setSpacing(6)

        def _global_btn(action, tip):
            b = self._tool_button(action, tip)
            b.setObjectName("TopGlobalBtn")
            b.setFixedSize(30, 30)
            b.setIconSize(QSize(18, 18))
            return b

        rl.addWidget(_global_btn(self.act_undo, "Undo"))
        rl.addWidget(_global_btn(self.act_redo, "Redo"))
        div = QFrame()
        div.setFixedSize(1, 20)
        div.setStyleSheet(f"background:{theme.CHROME_BORDER};")
        rl.addWidget(div)

        # Unified zoom bar: [ Fit | - 100% + ] -- ONE bordered pill, no dropdown.
        # A QFrame (not a bare QWidget) so the CONTROL_FILL fill + BORDER_STRONG
        # outline paint reliably; the children are flat/transparent so the pill
        # shows through. All colours come from theme tokens -> correct in both
        # light and dark.
        zoombar = QFrame()
        zoombar.setObjectName("ZoomBar")
        zoombar.setFixedHeight(32)
        zbl = QHBoxLayout(zoombar)
        zbl.setContentsMargins(2, 0, 2, 0)
        zbl.setSpacing(0)
        # Fit: a flat text button -> fit_width.
        zoom_fit_btn = QToolButton()
        zoom_fit_btn.setObjectName("ZoomFitBtn")
        zoom_fit_btn.setText("Fit")
        zoom_fit_btn.setAutoRaise(True)
        zoom_fit_btn.setCursor(Qt.PointingHandCursor)
        zoom_fit_btn.setToolButtonStyle(Qt.ToolButtonTextOnly)
        zoom_fit_btn.setFont(theme.ui_font(13, medium=True))
        zoom_fit_btn.setToolTip("Fit width")
        zoom_fit_btn.clicked.connect(self.fit_width)
        # 1px vertical hairline divider (colour from QSS, NOT an inline hex).
        zoom_div = QFrame()
        zoom_div.setObjectName("ZoomDivider")
        zoom_div.setFixedSize(1, 18)
        # Minus step: the TYPOGRAPHIC minus (U+2212), not an icon glyph.
        self._zoom_out_btn = QToolButton()
        self._zoom_out_btn.setObjectName("ZoomStepBtn")
        self._zoom_out_btn.setText("−")
        self._zoom_out_btn.setAutoRaise(True)
        self._zoom_out_btn.setCursor(Qt.PointingHandCursor)
        self._zoom_out_btn.setToolButtonStyle(Qt.ToolButtonTextOnly)
        self._zoom_out_btn.setFont(theme.ui_font(17))
        self._zoom_out_btn.setToolTip("Zoom out")
        self._zoom_out_btn.setFixedSize(22, 22)
        self._zoom_out_btn.clicked.connect(self.zoom_out)
        # Percent value: a plain clickable button (NO menu) -> actual size (100%).
        self.zoom_button = QToolButton()
        self.zoom_button.setText("100%")
        self.zoom_button.setObjectName("ZoomButton")
        self.zoom_button.setToolTip("Actual size (100%)")
        self.zoom_button.setCursor(Qt.PointingHandCursor)
        self.zoom_button.setToolButtonStyle(Qt.ToolButtonTextOnly)
        self.zoom_button.setFont(theme.mono_font(12, semibold=True))
        self.zoom_button.clicked.connect(lambda: self.set_zoom(1.0))
        # Plus step: the typographic plus.
        self._zoom_in_btn = QToolButton()
        self._zoom_in_btn.setObjectName("ZoomStepBtn")
        self._zoom_in_btn.setText("+")
        self._zoom_in_btn.setAutoRaise(True)
        self._zoom_in_btn.setCursor(Qt.PointingHandCursor)
        self._zoom_in_btn.setToolButtonStyle(Qt.ToolButtonTextOnly)
        self._zoom_in_btn.setFont(theme.ui_font(17))
        self._zoom_in_btn.setToolTip("Zoom in")
        self._zoom_in_btn.setFixedSize(22, 22)
        self._zoom_in_btn.clicked.connect(self.zoom_in)
        zbl.addWidget(zoom_fit_btn)
        zbl.addWidget(zoom_div)
        zbl.addWidget(self._zoom_out_btn)
        zbl.addWidget(self.zoom_button)
        zbl.addWidget(self._zoom_in_btn)
        rl.addWidget(zoombar)
        # Only the zoom bar hides in the empty state.
        self._zoom_group_actions: list[QAction] = [zoombar]

        # Share: reveal the saved file in Finder / Explorer (cross-platform).
        self.act_share = QAction("Reveal in Finder", self)
        self.act_share.setIcon(make_icon("share"))
        self.act_share.triggered.connect(self._do_reveal_file)
        self.addAction(self.act_share)
        self.share_button = _global_btn(self.act_share, "Reveal file in folder")
        rl.addWidget(self.share_button)

        # Primary Save CTA: a clay SPLIT pill -- a "Save" half (icon + label ->
        # act_save) and a caret half (down-chevron -> the Save-options menu),
        # divided by a 1px hairline. The look follows the document UNSAVED state
        # (the same signal driving the top-bar dirty dot): DIRTY = filled clay +
        # a subtle warm lift; CLEAN = a quiet neutral pill, no shadow. The two
        # halves live in a small container so the hairline, the independent hover
        # states, and the caret menu are all styleable via objectNames.
        # Save As stays in the File menu and on Cmd+Shift+S; the caret menu is an
        # ADDITIONAL surface, not a replacement.
        self.save_split = QWidget()
        self.save_split.setObjectName("SaveSplit")
        self.save_split.setFixedHeight(34)
        self.save_split.setProperty("dirty", False)
        savl = QHBoxLayout(self.save_split)
        savl.setContentsMargins(0, 0, 0, 0)
        savl.setSpacing(0)
        # make_icon BAKES the glyph colour into the pixmaps, so a QSS `color:`
        # rule will NOT recolour the icon: pre-build white (dirty) and quiet
        # (clean) variants and swap them in _refresh_save_state.
        self._save_icon_white = make_icon("save", color="#FFFFFF")
        self._save_icon_quiet = make_icon("save", color=theme.TEXT_SECONDARY)
        self._caret_icon_white = make_icon("chevron_down", color="#FFFFFF")
        self._caret_icon_quiet = make_icon("chevron_down", color=theme.TEXT_SECONDARY)
        # A QPushButton (NOT a QToolButton) so the icon + "Save" are CENTERED as
        # one group, and so tests that do findChildren(type(w.save_button)) keep
        # matching a QPushButton.
        self.save_button = QPushButton()
        self.save_button.setObjectName("SaveButton")
        self.save_button.setText("Save")
        self.save_button.setIcon(self._save_icon_white)
        self.save_button.setIconSize(QSize(16, 16))
        self.save_button.setFixedHeight(34)        # fill the 34px split container
        self.save_button.setCursor(Qt.PointingHandCursor)
        self.save_button.setToolTip("Save")
        self.save_button.setProperty("dirty", False)
        self.save_button.clicked.connect(self.act_save.trigger)
        self.save_button.setEnabled(self.act_save.isEnabled())
        self.act_save.changed.connect(
            lambda: self.save_button.setEnabled(self.act_save.isEnabled()))
        self.save_caret = QToolButton()
        self.save_caret.setObjectName("SaveCaret")
        self.save_caret.setFixedSize(27, 34)       # 27 wide, fill the 34px split
        self.save_caret.setCursor(Qt.PointingHandCursor)
        self.save_caret.setToolTip("Save options")
        self.save_caret.setAutoRaise(True)
        self.save_caret.setToolButtonStyle(Qt.ToolButtonIconOnly)
        self.save_caret.setIconSize(QSize(15, 15))
        self.save_caret.setIcon(self._caret_icon_white)
        self.save_caret.setProperty("dirty", False)
        self.save_caret.setPopupMode(QToolButton.InstantPopup)
        self.save_caret.setMenu(self._build_save_menu())
        savl.addWidget(self.save_button)
        savl.addWidget(self.save_caret)
        # A tiny WARM lift (never the old indigo glow), ON only in the dirty
        # state. The colour is read once here (mode is fixed at launch); a low
        # alpha keeps it subtle. The effect is installed ONCE and toggled via
        # setEnabled -- swapping it out with setGraphicsEffect(None) makes Qt
        # delete it, so the next dirty cycle would hit a deleted C++ object.
        self._save_shadow = QGraphicsDropShadowEffect(self.save_split)
        self._save_shadow.setBlurRadius(3)
        self._save_shadow.setOffset(0, 1)
        _warm = theme.color_sheet_shadow()
        _warm.setAlpha(min(_warm.alpha(), 40))
        self._save_shadow.setColor(_warm)
        self._save_shadow.setEnabled(False)
        self.save_split.setGraphicsEffect(self._save_shadow)
        rl.addWidget(self.save_split)

        bar.addWidget(right)

        # --- Off-bar / placeholder objects kept for the existing sync code ---
        # Markup, Insert Image, and Signature moved to the left rail; page nav
        # moved to the footer; Organize lives in the Document menu. These objects
        # must still EXIST (hidden) so the hasattr-guarded sync paths and the
        # signature-menu attach keep working untouched.
        self._edit_group_actions: list[QAction] = []
        self._edit_group_separator = QAction(self)
        self._edit_group_separator.setVisible(False)
        self._page_zoom_separator = QAction(self)
        self._page_zoom_separator.setVisible(False)
        # Parented to the WINDOW (not the toolbar) so they are hidden AND do not
        # show up under toolbar.findChildren -- the slim-top-bar test asserts no
        # Organize button lives on the bar.
        self.add_text_button = self._tool_button(self.act_add_text, "Add text (T)")
        self.add_text_button.setCheckable(True)
        self.add_text_button.setParent(self)
        self.add_text_button.setVisible(False)
        self.insert_image_button = self._tool_button(
            self.act_insert_image, "Insert image")
        self.insert_image_button.setParent(self)
        self.insert_image_button.setVisible(False)
        self.signature_button = QToolButton(self)
        self.signature_button.setObjectName("SignatureButton")
        self.signature_button.setPopupMode(QToolButton.MenuButtonPopup)
        self.signature_button.setToolButtonStyle(Qt.ToolButtonIconOnly)
        self.signature_button.setIconSize(QSize(theme.ICON_SIZE, theme.ICON_SIZE))
        self.signature_button.setAutoRaise(True)
        self.signature_button.setDefaultAction(self.act_draw_signature)
        self.signature_button.setVisible(False)
        self.organize_button = QToolButton(self)
        self.organize_button.setObjectName("OrganizeButton")
        self.organize_button.setPopupMode(QToolButton.InstantPopup)
        self.organize_button.setToolButtonStyle(Qt.ToolButtonTextOnly)
        self.organize_button.setText("Organize")
        self.organize_button.setMenu(self._build_organize_menu())
        self.organize_button.setVisible(False)
        self._organize_separator = QAction(self)
        self._organize_separator.setVisible(False)
        self._organize_action = self._organize_separator

        # Page navigation lives in the FOOTER (built next): create the widgets
        # here, hand the container to _build_statusbar via self._footer_nav.
        # Reads "Page [N] of M": [N] is an editable MONO field in a recessed box;
        # "Page" and "of M" are quiet ui-font words. NO prev/next chevrons (the
        # act_prev/act_next keyboard shortcuts stay intact). Every colour comes
        # from a theme token, so it renders correctly in light AND dark.
        nav_mono = theme.mono_font(12, semibold=True)
        word_font = theme.ui_font(13)
        self.page_prefix = QLabel("Page")
        self.page_prefix.setObjectName("PageLabel")
        self.page_prefix.setFont(word_font)
        self.page_prefix.setAlignment(Qt.AlignVCenter)
        self.page_field = _PageField()
        self.page_field.setObjectName("PageField")
        self.page_field.setFixedSize(34, 28)
        self.page_field.setAlignment(Qt.AlignCenter)
        self.page_field.setFont(nav_mono)
        self.page_field.returnPressed.connect(self._page_field_entered)
        self.page_total = QLabel("of 0")
        self.page_total.setObjectName("PageTotal")
        self.page_total.setFont(word_font)
        self.page_total.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        nav = QWidget()
        nav.setObjectName("FooterNavPill")
        # Let the 28px field drive the height (STATUS_HEIGHT is 30, so it fits);
        # the words read as inline text around it, no surrounding pill.
        nav_l = QHBoxLayout(nav)
        nav_l.setContentsMargins(0, 0, 0, 0)
        nav_l.setSpacing(6)
        nav_l.addWidget(self.page_prefix)
        nav_l.addWidget(self.page_field)
        nav_l.addWidget(self.page_total)
        self._footer_nav = nav
        self._page_group_actions: list[QAction] = []   # filled in _build_statusbar

        # Set the Save split's initial (no-document) look so it starts as the
        # quiet neutral pill rather than an un-polished default.
        self._refresh_save_state()

    def _do_reveal_file(self) -> None:
        """Share = reveal the saved file in the OS file manager (Finder on
        macOS, Explorer on Windows). Opens the containing folder, which is the
        portable way to hand the file off."""
        if self.document is None:
            return
        path = getattr(self.document, "path", "") or ""
        folder = os.path.dirname(path)
        if not folder or not os.path.isdir(folder):
            return
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices
        QDesktopServices.openUrl(QUrl.fromLocalFile(folder))

    def _build_annotate_menu(self) -> None:
        """Insert the markup actions at the Tools menu's ``tools_annotate``
        anchor (the conflict-ledger menu decision: NO new top-level menu;
        anchor insertion only). The four shape kinds sit in a Shapes
        submenu (§5.2); Show Comments inserts at the View menu's
        ``view_panels`` anchor (§5.4)."""
        anchor = self.menu_anchors["tools_annotate"]
        for kind in ("highlight", "underline", "strikeout", "squiggly",
                     "note", "ink"):
            self.menu_tools.insertAction(anchor, self._markup_actions[kind])
        self.menu_shapes = QMenu("Shapes", self)
        for kind in ("rect", "ellipse", "line", "arrow"):
            self.menu_shapes.addAction(self._markup_actions[kind])
        self.menu_tools.insertMenu(anchor, self.menu_shapes)
        self.menu_tools.insertAction(anchor, self.act_delete_annot)
        self.menu_view.insertAction(self.menu_anchors["view_panels"],
                                    self.act_show_comments)

    def _build_objects_menu(self) -> None:
        """Insert the object-placement entries at the Tools menu's
        ``tools_objects`` anchor (images & signatures §4; the
        conflict-ledger menu decision: NO new top-level menu, anchor
        insertion only): "Image from File…" (M1) plus the Signature and
        Stamp submenus (M2). The Signature menu re-lists the library on
        every open; the same QMenu also feeds the toolbar Signature
        menu-button (one menu, two mounts)."""
        anchor = self.menu_anchors["tools_objects"]
        self.menu_tools.insertAction(anchor, self.act_insert_image)
        self.menu_signature = QMenu("Signature", self)
        self.menu_signature.aboutToShow.connect(self._rebuild_signature_menu)
        self._rebuild_signature_menu()
        self.menu_tools.insertMenu(anchor, self.menu_signature)
        self.menu_stamp = QMenu("Stamp", self)
        self._stamp_actions: list[QAction] = []
        for text in stamps.STAMP_KINDS:
            act = QAction(text.capitalize(), self)
            act.triggered.connect(
                lambda _checked=False, t=text: self._do_place_stamp(t))
            self.menu_stamp.addAction(act)
            self._stamp_actions.append(act)
        self.menu_tools.insertMenu(anchor, self.menu_stamp)
        if hasattr(self, "signature_button"):
            self.signature_button.setMenu(self.menu_signature)

    def _rebuild_signature_menu(self) -> None:
        """(Re)populate the Signature menu (§4, on ``aboutToShow``): one
        thumbnail entry per library item (newest first) -> one-click
        placement; then the persistent Draw / From-File / Manage actions.
        ``clear()`` only detaches the persistent actions (they are parented
        on the window), so re-adding them is safe."""
        menu = self.menu_signature
        menu.clear()
        has_doc = self.document is not None
        entries = self._signature_library.list()
        for name, path in entries:
            act = menu.addAction(QIcon(signature_menu_icon(path)), name)
            act.setEnabled(has_doc)
            act.triggered.connect(
                lambda _checked=False, p=path:
                self._place_signature_from_library(p))
        if entries:
            menu.addSeparator()
        menu.addAction(self.act_draw_signature)
        menu.addAction(self.act_signature_from_file)
        menu.addSeparator()
        menu.addAction(self.act_manage_signatures)

    def _tool_button(self, action: QAction, tooltip: str = "") -> QToolButton:
        btn = QToolButton()
        btn.setDefaultAction(action)
        btn.setToolButtonStyle(Qt.ToolButtonIconOnly)
        btn.setIconSize(QSize(theme.ICON_SIZE, theme.ICON_SIZE))
        btn.setAutoRaise(True)
        if tooltip:
            btn.setToolTip(tooltip)
        return btn

    def _build_save_menu(self) -> QMenu:
        """The Save split's caret menu: the save-options surface (Save As…).
        An ADDITIONAL home for act_save_as next to the File menu / Cmd+Shift+S."""
        menu = QMenu(self)
        menu.addAction(self.act_save_as)
        return menu

    # ===================================================================
    # Page & document actions (PAGES_SPEC §5.3 / §6)
    # ===================================================================
    def _build_page_actions(self) -> None:
        """All structural actions live on the window so their shortcuts fire
        regardless of focus (PAGES_SPEC §6.9). They are wired into the Organize
        popup, the Document menu, and the sidebar context menu, which all route
        to the SAME handlers."""
        mk = self._make_action
        self.act_combine = mk("Combine PDFs…", None, self.combine_pdfs)
        self.act_extract = mk("Extract Pages…", None, self.extract_pages_dialog)
        self.act_split = mk("Split PDF…", None, self.split_dialog)
        self.act_rotate_cw = mk("Rotate Page Right",
                                QKeySequence("Ctrl+R"), self.rotate_current_cw)
        self.act_rotate_ccw = mk("Rotate Page Left",
                                 QKeySequence("Ctrl+Shift+R"),
                                 self.rotate_current_ccw)
        self.act_delete_page = mk("Delete Page", None, self.delete_current_page)
        self.act_insert_blank = mk("Insert Blank Page", None,
                                   self.insert_blank_after_current)
        self.act_duplicate_page = mk("Duplicate Page", None,
                                     self.duplicate_current_page)
        self.act_toggle_pages = mk("Show Pages Sidebar",
                                   QKeySequence("Ctrl+Shift+P"),
                                   self.toggle_pages_dock)
        self.act_toggle_pages.setCheckable(True)
        self.act_toggle_pages.setChecked(True)
        # Stable name for the generated shortcuts cheatsheet: the menu label
        # flips Show <-> Hide with state, and a reference sheet must not bake
        # the current toggle state in.
        self.act_toggle_pages.setProperty(
            "cheatsheet_label", "Show/Hide Pages Sidebar")
        for act in (self.act_combine, self.act_extract, self.act_split,
                    self.act_rotate_cw, self.act_rotate_ccw, self.act_delete_page,
                    self.act_insert_blank, self.act_duplicate_page,
                    self.act_toggle_pages):
            self.addAction(act)
        # Show Bookmarks (navigation M1): a checkable View-menu toggle
        # mirroring the left strip's Bookmarks tool, the act_show_comments
        # pattern (the strip stays the source of truth;
        # ``_sync_show_bookmarks_check`` keeps the toggle honest). Cmd+Alt+B:
        # Cmd+B/I are reserved for inline bold/italic (text editing UX).
        self._bookmarks_check_syncing = False
        self.act_toggle_bookmarks = QAction("Show Bookmarks", self)
        self.act_toggle_bookmarks.setCheckable(True)
        self.act_toggle_bookmarks.setShortcut(QKeySequence("Ctrl+Alt+B"))
        self.act_toggle_bookmarks.setProperty(
            "cheatsheet_label", "Show/Hide Bookmarks")
        self.act_toggle_bookmarks.toggled.connect(
            self._on_show_bookmarks_toggled)
        self.addAction(self.act_toggle_bookmarks)
        self._page_actions = [
            self.act_combine, self.act_extract, self.act_split,
            self.act_rotate_cw, self.act_rotate_ccw, self.act_delete_page,
            self.act_insert_blank, self.act_duplicate_page,
        ]

    def _build_organize_menu(self) -> QMenu:
        """The toolbar Organize popup (PAGES_SPEC §5.3). Document-level ops up
        top, current-page ops below; the combine submenu lists open tabs."""
        menu = QMenu(self)
        menu.addAction(self.act_combine)
        self.combine_tabs_menu = menu.addMenu("Combine Open Tab Into This")
        self.combine_tabs_menu.aboutToShow.connect(self._populate_combine_tabs)
        menu.addAction(self.act_extract)
        menu.addAction(self.act_split)
        menu.addSeparator()
        menu.addAction(self.act_rotate_cw)
        menu.addAction(self.act_rotate_ccw)
        menu.addAction(self.act_duplicate_page)
        menu.addAction(self.act_insert_blank)
        menu.addAction(self.act_delete_page)
        return menu

    def _populate_combine_tabs(self) -> None:
        """Rebuild the "Combine Open Tab Into This" submenu from the OTHER open
        documents (PAGES_SPEC §5.3). Disabled when fewer than 2 docs are open."""
        menu = self.combine_tabs_menu
        menu.clear()
        active = self.workspace.active_index
        any_other = False
        for i in range(self.workspace.count):
            if i == active:
                continue
            any_other = True
            title = self.workspace.title(i)
            act = menu.addAction(title)
            act.triggered.connect(
                lambda _checked=False, idx=i: self.combine_open_tab(idx))
        menu.setEnabled(any_other)

    def _populate_open_recent(self) -> None:
        """Rebuild File > Open Recent on every aboutToShow (navigation M2):
        basename entries from ``_recent_paths`` (same-name files get their
        home-abbreviated folder appended so duplicates stay tellable apart),
        full path in the tooltip, and a Clear Menu tail."""
        menu = self.menu_open_recent
        menu.clear()
        paths = self._recent_paths()
        names = [os.path.basename(p) for p in paths]
        for path in paths:
            base = os.path.basename(path)
            label = base
            if names.count(base) > 1:
                label = f"{base} ({_abbrev_home(os.path.dirname(path))})"
            act = menu.addAction(label)
            act.setToolTip(path)
            act.setStatusTip(path)
            act.triggered.connect(
                lambda _checked=False, p=path: self.open_path(p))
        if paths:
            menu.addSeparator()
        clear = menu.addAction(make_icon("clear_recent"), "Clear Menu")
        clear.setEnabled(bool(paths))
        clear.triggered.connect(self._clear_recents)

    def _clear_recents(self) -> None:
        """File > Open Recent > Clear Menu: forget the app's recent-file
        list (the QSettings key only -- the files themselves are untouched,
        so no confirmation). The empty state re-reads so its RECENT rows
        follow."""
        self._recent_store().remove(self._RECENT_KEY)
        if hasattr(self, "empty_state"):
            self._refresh_empty_recents()

    def _populate_window_docs(self) -> None:
        """Rebuild the Window menu's open-document list on every aboutToShow
        (navigation M2): one checkable entry per open tab, checkmark on the
        active one; activating an entry switches tabs through the SAME
        handler as a tab-bar click."""
        m = self.menu_window
        for act in self._window_doc_actions:
            m.removeAction(act)
            act.deleteLater()
        self._window_doc_actions = []
        active = self.workspace.active_index
        for i in range(self.workspace.count):
            act = QAction(self.workspace.title(i), m)
            act.setCheckable(True)
            act.setChecked(i == active)
            act.triggered.connect(
                lambda _checked=False, idx=i: self._on_tab_activated(idx))
            m.addAction(act)
            self._window_doc_actions.append(act)

    def _show_shortcuts(self) -> None:
        """Help > Keyboard Shortcuts… (Cmd+/): the cheatsheet GENERATED from
        the live menubar, so it self-updates as workstreams add actions.
        Non-modal (offscreen-test rule), cached, refreshed on every show."""
        if self._shortcuts_dialog is None:
            self._shortcuts_dialog = ShortcutsDialog(self.menu_bar, self)
        self._shortcuts_dialog.refresh()
        self._shortcuts_dialog.show()
        self._shortcuts_dialog.raise_()
        self._shortcuts_dialog.activateWindow()

    def _show_about(self) -> None:
        """Help > About PDF Text Editor: non-modal, cached."""
        if self._about_dialog is None:
            self._about_dialog = AboutDialog(__version__, make_icon, self)
        self._about_dialog.show()
        self._about_dialog.raise_()
        self._about_dialog.activateWindow()

    def _show_settings(self) -> None:
        """Settings (Preferences on macOS): non-modal, cached. Houses the OCR
        engine toggle today; accounts and the update channel land here later."""
        import sys
        from .settings_dialog import SettingsDialog
        if self._settings_dialog is None:
            self._settings_dialog = SettingsDialog(
                is_mac=(sys.platform == "darwin"),
                current_engine=self._ocr_engine_pref(),
                on_engine_changed=self._set_ocr_engine_pref,
                parent=self)
        else:
            self._settings_dialog.set_current_engine(self._ocr_engine_pref())
        self._settings_dialog.show()
        self._settings_dialog.raise_()
        self._settings_dialog.activateWindow()

    def _sync_toggle_labels(self, *_args) -> None:
        """Show <-> Hide flips for the View menu's checkable panel toggles
        (navigation M2). Label follows the action's checked state (the
        strip/dock state mirrors), not raw widget visibility, so the
        empty state's temporary dock hide never lies about intent."""
        self.act_toggle_pages.setText(
            "Hide Pages Sidebar" if self.act_toggle_pages.isChecked()
            else "Show Pages Sidebar")
        self.act_show_comments.setText(
            "Hide Comments" if self.act_show_comments.isChecked()
            else "Show Comments")
        self.act_toggle_bookmarks.setText(
            "Hide Bookmarks" if self.act_toggle_bookmarks.isChecked()
            else "Show Bookmarks")

    def _build_menubar(self) -> None:
        """The 7-menu skeleton: File / Edit / View / Tools / Document / Window /
        Help (perf foundation M4d), built per the navigation workstream's menu
        table (docs/acrobat_buildout/specs/ws7_navigation_chrome.md §M2 -- the
        registry of record). One builder per menu; later workstreams extend
        exactly one builder by inserting actions at the reserved separator
        anchors in ``self.menu_anchors`` via ``menu.insertAction(anchor, act)``
        -- never by adding a top-level menu or reordering existing actions.
        Dynamic content (Open Recent, the Window open-document list, the
        shortcut cheatsheet, About) lands with the navigation workstream."""
        bar = QMenuBar(self)
        self.setMenuBar(bar)
        self.menu_bar = bar
        # Named insertion anchors: real separator QActions other workstreams
        # insert BEFORE (menu.insertAction(anchor, act)). Registry:
        #   file_output    File      Print… / Export… / Extract Text
        #   edit_extra     Edit      Cut / Copy / Paste (text editing UX)
        #   view_panels    View      panel toggles (comments, …)
        #   tools_annotate Tools     highlight / ink / shapes / notes
        #   tools_objects  Tools     image / signature / stamp
        #   tools_forms    Tools     form tools
        #   doc_transform  Document  crop / page transforms
        #   doc_decorate   Document  watermark / header-footer
        #   doc_file       Document  compress / password / metadata
        self.menu_anchors: dict[str, QAction] = {}
        self.menu_file = self._build_menu_file(bar)
        self.menu_edit = self._build_menu_edit(bar)
        self.menu_view = self._build_menu_view(bar)
        self.menu_tools = self._build_menu_tools(bar)
        self.menu_document = self._build_menu_document(bar)
        self.menu_window = self._build_menu_window(bar)
        self.menu_help = self._build_menu_help(bar)
        # The View menu's checkable panel toggles flip Show <-> Hide with
        # their checked state (navigation M2: the static labels read "Show"
        # even while the panel was open). toggled covers the action-driven
        # paths; the pages-dock visibility hook re-syncs the one path whose
        # setChecked runs under blockSignals.
        for act in (self.act_toggle_pages, self.act_show_comments,
                    self.act_toggle_bookmarks):
            act.toggled.connect(self._sync_toggle_labels)
        self._sync_toggle_labels()

    def _menu_anchor(self, menu: QMenu, key: str) -> QAction:
        """Reserve a named anchor in ``menu``: a separator QAction stored in
        ``self.menu_anchors[key]``. Visually inert until a workstream inserts
        actions before it (QMenu collapses consecutive separators)."""
        anchor = menu.addSeparator()
        self.menu_anchors[key] = anchor
        return anchor

    def _build_menu_file(self, bar: QMenuBar) -> QMenu:
        m = bar.addMenu("File")
        m.addAction(self.act_open)
        # Open Recent (navigation M2): dynamic, rebuilt from _recent_paths()
        # on every aboutToShow, with a Clear Menu tail.
        self.menu_open_recent = QMenu("Open Recent", m)
        self.menu_open_recent.aboutToShow.connect(self._populate_open_recent)
        m.addMenu(self.menu_open_recent)
        m.addSeparator()
        m.addAction(self.act_save)
        m.addAction(self.act_save_as)
        m.addSeparator()
        anchor = self._menu_anchor(m, "file_output")
        # ws6 M1+M4 (doc tools §1): output entries inserted AT the
        # file_output anchor -- the anchor-insertion discipline every
        # workstream follows. Registry order: Save Optimized Copy, Export,
        # Print, Document Properties.
        m.insertAction(anchor, self.act_optimize)
        self.menu_export = QMenu("Export", m)
        self.menu_export.addAction(self.act_export_images)
        self.menu_export.addAction(self.act_export_text)
        m.insertMenu(anchor, self.menu_export)
        m.insertAction(anchor, self.act_print)
        m.insertAction(anchor, self.act_properties)
        # ws5 M3 (forms §4): Export Flattened Copy appends to the END of
        # the file_output group (the append-only anchor discipline);
        # enablement is form-gated in _sync_actions.
        m.insertAction(anchor, self.act_flatten)
        m.addSeparator()
        m.addAction(self.act_close)
        return m

    def _build_menu_edit(self, bar: QMenuBar) -> QMenu:
        m = bar.addMenu("Edit")
        m.addAction(self.act_undo)
        m.addAction(self.act_redo)
        m.addSeparator()
        m.addAction(self.act_delete)
        m.addSeparator()
        # Bold / Italic (ws2 M1 §2.4b): state-routed -- editor selection,
        # else whole selected box.
        m.addAction(self.act_bold)
        m.addAction(self.act_italic)
        m.addSeparator()
        m.addAction(self.act_group)
        m.addAction(self.act_ungroup)
        m.addSeparator()
        m.addAction(self.act_find)
        m.addSeparator()
        anchor = self._menu_anchor(m, "edit_extra")
        # Cut / Copy / Paste (ws2 M3 §4.5): inserted AT the edit_extra anchor
        # (the anchor-insertion discipline every workstream follows, so menu
        # merges stay conflict-free).
        m.insertAction(anchor, self.act_cut)
        m.insertAction(anchor, self.act_copy)
        m.insertAction(anchor, self.act_paste)
        return m

    def _build_menu_view(self, bar: QMenuBar) -> QMenu:
        m = bar.addMenu("View")
        m.addAction(self.act_toggle_pages)
        # Show Bookmarks before the view_panels anchor (the ws7 §M2 registry
        # row); other panel toggles (comments, …) insert AT the anchor.
        m.addAction(self.act_toggle_bookmarks)
        self._menu_anchor(m, "view_panels")
        m.addSeparator()
        m.addAction(self.act_zoom_in)
        m.addAction(self.act_zoom_out)
        m.addAction(self.act_actual_size)
        m.addAction(self.act_fit_page)
        m.addAction(self.act_fit_width)
        m.addSeparator()
        # The window-only page-nav actions get their discoverable menu homes.
        nav = m.addMenu("Page Navigation")
        nav.addAction(self.act_next)
        nav.addAction(self.act_prev)
        nav.addAction(self.act_first)
        nav.addAction(self.act_last)
        nav.addAction(self.act_goto)
        self.menu_page_nav = nav
        return m

    def _build_menu_tools(self, bar: QMenuBar) -> QMenu:
        m = bar.addMenu("Tools")
        # Select Tool (V) / Select Text (S) / Text Edit Tool (E): the
        # advertised tool keys (ws2 §2.7b + §5.3 -- ws2 owns their creation;
        # ws7 M2 only verifies). Order mirrors the left strip.
        m.addAction(self.act_select_tool)
        m.addAction(self.act_select_text_tool)
        m.addAction(self.act_text_edit_tool)
        m.addAction(self.act_add_text)
        m.addSeparator()
        self._menu_anchor(m, "tools_annotate")
        self._menu_anchor(m, "tools_objects")
        self._menu_anchor(m, "tools_forms")
        return m

    def _build_menu_document(self, bar: QMenuBar) -> QMenu:
        """Replaces the former Pages menu: same QActions, same handlers
        (the ``_run_structural`` funnel), only the menu home changes."""
        m = bar.addMenu("Document")
        m.addAction(self.act_combine)
        m.addAction(self.act_extract)
        m.addAction(self.act_split)
        m.addSeparator()
        m.addAction(self.act_rotate_cw)
        m.addAction(self.act_rotate_ccw)
        m.addAction(self.act_duplicate_page)
        m.addAction(self.act_insert_blank)
        m.addAction(self.act_delete_page)
        m.addSeparator()
        anchor = self._menu_anchor(m, "doc_transform")
        # ws6 M3 (doc tools §2.6): Crop Pages inserted AT the doc_transform
        # anchor (the anchor-insertion discipline every workstream follows).
        m.insertAction(anchor, self.act_crop)
        anchor = self._menu_anchor(m, "doc_decorate")
        # ws6 M2 (doc tools §2.4/§2.5): the stamp entries inserted AT the
        # doc_decorate anchor.
        m.insertAction(anchor, self.act_watermark)
        m.insertAction(anchor, self.act_header_footer)
        anchor = self._menu_anchor(m, "doc_file")
        # ws6 M4 (doc tools §2.7): Security inserted AT the doc_file anchor.
        m.insertAction(anchor, self.act_security)
        # OCR (OCR_SPEC): make scanned pages editable. Own separator group.
        m.addSeparator()
        m.addAction(self.act_ocr_page)
        m.addAction(self.act_ocr_document)
        return m

    def _build_menu_window(self, bar: QMenuBar) -> QMenu:
        m = bar.addMenu("Window")
        m.addAction(self.act_minimize)
        m.addAction(self.act_window_zoom)
        m.addSeparator()
        m.addAction(self.act_next_tab)
        m.addAction(self.act_prev_tab)
        # The dynamic open-document list (navigation M2): rebuilt on every
        # aboutToShow, checkmark on the active tab.
        m.addSeparator()
        self._window_doc_actions: list[QAction] = []
        m.aboutToShow.connect(self._populate_window_docs)
        return m

    def _build_menu_help(self, bar: QMenuBar) -> QMenu:
        m = bar.addMenu("Help")
        m.addAction(self.act_preferences)
        m.addSeparator()
        m.addAction(self.act_shortcuts)
        m.addAction(self.act_about)
        return m

    # ===================================================================
    # Pages thumbnail dock (PAGES_SPEC §5.1)
    # ===================================================================
    def _build_pages_dock(self) -> None:
        """Host the PageThumbnailSidebar in a RIGHT dock titled "Pages"
        (REFLOW_SPEC §R4.0). It MAY be closed (unlike the fixed left Tools dock);
        the toolbar toggle + View > Pages re-open it."""
        self.sidebar = PageThumbnailSidebar()
        # Wrap the list in a container that carries its own header row (same
        # pattern as the left panel) so the "Pages" header is laid out by the
        # container's layout and never occluded by an overlapping dock title slot.
        pages_host = QWidget()
        pages_host.setObjectName("PagesHost")
        phl = QVBoxLayout(pages_host)
        phl.setContentsMargins(0, 0, 0, 0)
        phl.setSpacing(0)
        pages_header = QWidget()
        pages_header.setObjectName("PagesHeader")
        pages_header.setFixedHeight(theme.TOOLBAR_HEIGHT)
        phh = QHBoxLayout(pages_header)
        phh.setContentsMargins(16, 0, 8, 0)
        phh.setSpacing(7)
        pages_title = QLabel("Pages")
        pages_title.setObjectName("PagesTitle")
        pages_title.setFont(theme.ui_font(theme.UI_FONT_TITLE, semibold=True))
        phh.addWidget(pages_title)
        # A muted page-count badge beside the title (the widget's .cs-pages count).
        self.pages_count = QLabel("")
        self.pages_count.setObjectName("PagesCount")
        self.pages_count.setFont(theme.ui_font(11))
        phh.addWidget(self.pages_count)
        phh.addStretch(1)
        # An "organize" grid button -> the Organize menu (the widget's .org).
        organize_btn = QToolButton()
        organize_btn.setObjectName("PagesOrganize")
        organize_btn.setIcon(make_icon("grid"))
        organize_btn.setIconSize(QSize(16, 16))
        organize_btn.setAutoRaise(True)
        organize_btn.setCursor(Qt.PointingHandCursor)
        organize_btn.setToolTip("Organize pages")
        organize_btn.setPopupMode(QToolButton.InstantPopup)
        organize_btn.setMenu(self._build_organize_menu())
        phh.addWidget(organize_btn)
        phl.addWidget(pages_header)
        phl.addWidget(self.sidebar, 1)

        dock = QDockWidget("Pages", self)
        dock.setObjectName("PagesDock")
        dock.setFeatures(QDockWidget.DockWidgetClosable)
        dock.setAllowedAreas(Qt.RightDockWidgetArea)
        dock.setTitleBarWidget(QWidget())   # collapse the (occluding) title slot
        dock.setWidget(pages_host)
        dock.setMinimumWidth(150)
        dock.setMaximumWidth(220)
        self.addDockWidget(Qt.RightDockWidgetArea, dock)
        self.pages_dock = dock
        dock.visibilityChanged.connect(self._on_pages_dock_visibility)

    def _on_pages_dock_visibility(self, visible: bool) -> None:
        # Keep the toggle action in sync when the dock is closed via its X.
        if hasattr(self, "act_toggle_pages"):
            self.act_toggle_pages.blockSignals(True)
            self.act_toggle_pages.setChecked(visible)
            self.act_toggle_pages.blockSignals(False)
            # blockSignals suppressed toggled, so flip the label here.
            self._sync_toggle_labels()

    def toggle_pages_dock(self, checked: bool) -> None:
        # The toggle records the preference (act_toggle_pages.isChecked() is
        # already set to ``checked``); the width gate then decides whether it can
        # actually show, so a narrow window still keeps the page room.
        if hasattr(self, "pages_dock"):
            self._apply_responsive_chrome()

    # ===================================================================
    # Tab + sidebar signal wiring (PAGES_SPEC §6.3 / §6.4)
    # ===================================================================
    def _wire_tabs_and_sidebar(self) -> None:
        self.tab_bar.tabActivated.connect(self._on_tab_activated)
        self.tab_bar.tabCloseRequested.connect(self._on_tab_close_requested)
        self.tab_bar.tabMoved.connect(self._on_tab_moved)

        self.sidebar.pageActivated.connect(self._on_thumb_activated)
        self.sidebar.reorderRequested.connect(self._on_reorder)
        self.sidebar.rotateRequested.connect(self._on_rotate_page)
        self.sidebar.deleteRequested.connect(self._on_delete_page)
        self.sidebar.duplicateRequested.connect(self._on_duplicate_page)
        self.sidebar.insertBlankRequested.connect(self._on_insert_blank)

    # ===================================================================
    # Left tool-strip wiring (REFLOW_SPEC §R4.1)
    # ===================================================================
    def _wire_left_panel(self) -> None:
        """Route the tool strip's selection to the view's modes + the Find panel,
        and keep the strip in sync with the view's modeChanged + the Add Text
        action (one source of truth for the armed tool)."""
        self.left_panel.toolSelected.connect(self._on_tool_selected)
        # The rail's one-shot action buttons (Image / Sign) trigger their actions.
        self.left_panel.actionRequested.connect(self._on_rail_action)
        # When the view's mode flips (e.g. ESC exits add-text, or a commit returns
        # to select), reflect it in the strip so the highlighted tool is correct.
        self._connect_signal("modeChanged", self._on_view_mode_changed)

    def _on_rail_action(self, name: str) -> None:
        """A one-shot rail action button fired (Charcoal redesign): Insert Image
        / Draw Signature. These are commands, not sticky modes, so they just
        trigger the existing action."""
        act = {"image": getattr(self, "act_insert_image", None),
               "signature": getattr(self, "act_draw_signature", None)}.get(name)
        if act is not None:
            act.trigger()

    def _on_tool_selected(self, tool_id: str) -> None:
        """A tool was picked in the left strip. Map it to the view's mode calls
        (the same enter/exit_add_text_mode the toolbar drove) + the Find /
        Comments panels."""
        # The strip already reflects the pick; keep the View > Show Comments
        # and Show Bookmarks toggles honest for EVERY branch (including the
        # early returns).
        self._sync_show_comments_check()
        self._sync_show_bookmarks_check()
        if tool_id == "add_text":
            # Drive the existing Add Text action so its checked state, the view's
            # add mode, and the strip all stay in lockstep.
            if not self.act_add_text.isChecked():
                self.act_add_text.setChecked(True)
            return
        # Any non-add tool leaves add-text mode.
        if self.act_add_text.isChecked():
            self.act_add_text.setChecked(False)
        # ... and disarms any markup tool (annotations §5.2: mutually
        # exclusive with the strip tools); no-op unless one was armed.
        fn = getattr(self.view, "exit_annot_mode", None)
        if callable(fn):
            try:
                fn()
            except (TypeError, RuntimeError):
                pass
        if tool_id == "select_text":
            # Arm the view's TEXT SELECT mode (ws2 M4 §5.3): word-level
            # copy-off-the-page; the view clears any box selection itself.
            fn = getattr(self.view, "enter_select_text_mode", None)
            if callable(fn):
                try:
                    fn()
                except (TypeError, RuntimeError):
                    pass
            return
        # Any non-text-select tool disarms it (highlights cleared, hotspot
        # presses re-enabled); no-op unless it was armed. Same for the crop
        # tool (ws6 §2.6) -- it has no strip entry (armed via the Document
        # menu), so ANY strip pick disarms it.
        for name in ("exit_select_text_mode", "exit_crop_mode"):
            fn = getattr(self.view, name, None)
            if callable(fn):
                try:
                    fn()
                except (TypeError, RuntimeError):
                    pass
        if tool_id == "find":
            self._activate_find()
        elif tool_id == "comments":
            self._activate_comments()
        elif tool_id == "bookmarks":
            self._activate_bookmarks()
        elif tool_id in ("select", "text_edit"):
            # Both are SELECT mode in the view; text_edit just biases click-to-edit
            # (the view already double-clicks into text). No extra view call.
            fn = getattr(self.view, "exit_add_text_mode", None)
            if callable(fn):
                try:
                    fn()
                except (TypeError, RuntimeError):
                    pass

    def _on_view_mode_changed(self, mode: str) -> None:
        """Keep the left tool strip's highlighted tool in sync with the view's
        mode (REFLOW_SPEC §R4.1). TEXT_EDIT maps to the Text Edit tool; ADD_TEXT
        to Add Text; SELECT to Select. Markup modes sync the TOOLBAR toggles
        (the markup tools live there, not in the strip -- annotations §5.2)
        and leave the strip on its last tool."""
        # A gesture hint (crop / place_image) dies with its mode.
        self._dismiss_mode_hint(mode)
        self._sync_markup_actions(mode)
        # Keep the checkable Crop action in lockstep with the view's mode
        # (ws6 §2.6): the mode can exit from the view side (Esc, another tool
        # arming), and the check must follow without re-driving the view.
        if hasattr(self, "act_crop") \
                and self.act_crop.isChecked() != (mode == "crop"):
            self.act_crop.blockSignals(True)
            self.act_crop.setChecked(mode == "crop")
            self.act_crop.blockSignals(False)
        # The Inspector's ANNOTATION section follows the armed tool while
        # nothing is selected: it edits the per-tool session defaults
        # (annotations & markup §5.5). A live annot selection owns the panel.
        if getattr(self, "_selected_annot", None) is None:
            self._sync_annot_inspector()
        if self._annot_kind_for_mode(mode) is not None:
            # A markup tool is armed. Leave the contextual panel as-is so the
            # Inspector's annotation-defaults section can show while a tool is
            # armed with nothing selected (annotations & markup §5.5); the armed
            # tool already reads as checked in the rail's Markup palette (its
            # buttons are bound to these actions). The palette is a tool chooser;
            # the per-tool style lives in the Inspector.
            return
        if not hasattr(self, "left_panel"):
            return
        tool = {"add_text": "add_text", "text_edit": "text_edit",
                "select": "select",
                "select_text": "select_text"}.get(mode, "select")
        self.left_panel.set_active_tool(tool)
        self._sync_show_comments_check()
        self._sync_show_bookmarks_check()

    def _activate_find(self) -> None:
        """Open Find & Replace (Cmd+F): arm the Find tool, swap the left column
        to the Find panel, seed the query from the current selection's text, and
        focus the find field (REFLOW_SPEC §R5.1)."""
        if not hasattr(self, "left_panel"):
            return
        self.left_panel.set_active_tool("find")
        # Leaving add-text mode is handled by the tool strip; mirror the state on
        # the Add Text action so the toolbar button is consistent.
        if hasattr(self, "act_add_text") and self.act_add_text.isChecked():
            self.act_add_text.setChecked(False)
        if self.document is not None:
            seed = self._selection_seed_text()
            if seed:
                self.find_panel.set_query(seed)
            else:
                self.find_panel.refresh()
        self.find_panel.focus_find()
        self._sync_show_comments_check()
        self._sync_show_bookmarks_check()

    def _selection_seed_text(self) -> str:
        """A short seed for the find field taken from the current selection's
        text (first line / first few words), or '' when nothing is selected."""
        box = None
        getter = getattr(self.view, "current_selection", None)
        if callable(getter):
            box = getter()
        if box is None or self.document is None:
            return ""
        try:
            text = self.document.staged_text(self.view_page_index(), box)
        except Exception:  # noqa: BLE001
            return ""
        text = (text or "").strip().split("\n", 1)[0].strip()
        # Keep a single word/short phrase, not a whole paragraph line.
        return text[:40]

    def _on_find_closed(self) -> None:
        """The Find panel was dismissed (Esc / close): return to the Select tool
        so the Format panel comes back."""
        if hasattr(self, "left_panel"):
            self.left_panel.set_active_tool("select")
        fn = getattr(self.view, "exit_add_text_mode", None)
        if callable(fn):
            try:
                fn()
            except (TypeError, RuntimeError):
                pass
        self._sync_show_comments_check()
        self._sync_show_bookmarks_check()

    # ------------------------------------------------------------------
    # Comments panel (annotations & markup §5.4) -- left column page 2
    # ------------------------------------------------------------------
    def _activate_comments(self) -> None:
        """Swap the left column to the Comments panel: the strip's Comments
        tool and View > Show Comments both land here. The list refreshes on
        entry so a panel opened after edits is current."""
        if not hasattr(self, "left_panel"):
            return
        self.left_panel.set_active_tool("comments")
        if hasattr(self, "act_add_text") and self.act_add_text.isChecked():
            self.act_add_text.setChecked(False)
        self._refresh_comments()
        self._sync_show_comments_check()
        self._sync_show_bookmarks_check()

    def _on_show_comments_toggled(self, checked: bool) -> None:
        """View > Show Comments (Cmd+Shift+C): show the Comments panel via
        the strip's Comments tool; unchecking returns to Select (the
        Find-close precedent)."""
        if self._comments_check_syncing:
            return
        if checked:
            if self.document is None:
                self._sync_show_comments_check()    # snap back: no document
                return
            self.left_panel.set_active_tool("comments")
            self._on_tool_selected("comments")
        else:
            self.left_panel.set_active_tool("select")
            self._on_tool_selected("select")

    def _sync_show_comments_check(self) -> None:
        """Mirror the Show Comments toggle onto the strip's ACTUAL state
        (the strip is the single source of truth for the active tool; the
        action is just a menu view of it)."""
        if not hasattr(self, "act_show_comments") \
                or not hasattr(self, "left_panel"):
            return                      # construction-order guard
        on = self.left_panel.active_tool() == "comments"
        if self.act_show_comments.isChecked() == on:
            return
        self._comments_check_syncing = True
        try:
            self.act_show_comments.setChecked(on)
        finally:
            self._comments_check_syncing = False

    def _refresh_comments(self) -> None:
        """Re-list the Comments panel (§5.4): runs after every AnnotCommand
        redo/undo (the command's ``on_change`` hook), every ``_sync_all``
        (structural ops, tab switches, open/close, save reload), and on
        panel entry."""
        panel = getattr(self, "comments_panel", None)
        if panel is not None:
            panel.refresh()

    # --- the panel's injected callables (all model/undo coupling) -------
    def _list_all_annots(self) -> list:
        """Every page's AnnotRecords for the Comments panel (§5.4); the
        panel itself sorts them by (page, y)."""
        if self.document is None:
            return []
        records: list = []
        for page in range(self.document.page_count):
            records.extend(self.document.annotations(page))
        return records

    def _jump_to_annot(self, ref) -> None:
        """A Comments-panel row was clicked: scroll the canvas to the
        annot's page and select it by identity (§5.4)."""
        if self.document is None or ref is None:
            return
        ident = getattr(ref, "identity", ref)
        scroll = getattr(self.view, "scroll_to_page", None)
        if callable(scroll):
            scroll(ident[0])
        fn = getattr(self.view, "select_annot_by_identity", None)
        if callable(fn):
            fn(ident)

    def _edit_annot_from_panel(self, ref) -> None:
        """Comments-panel Edit Note: the §5.3 popup on the row's record
        (any kind -- contents edits are valid model-wide)."""
        if ref is not None:
            self.open_note_editor(ref)

    def _delete_annot_from_panel(self, ref) -> None:
        """Comments-panel Delete: ONE undoable 'delete' command for the
        row's annot (staged spec or existing-file override)."""
        if self.document is None or ref is None:
            return
        ident = getattr(ref, "identity", ref)
        self._on_annot_command_requested(
            "delete", ident, {"page_index": ident[0]})

    # ------------------------------------------------------------------
    # Bookmarks panel (navigation M1) -- left column page 3
    # ------------------------------------------------------------------
    def _activate_bookmarks(self) -> None:
        """Swap the left column to the Bookmarks panel: the strip's Bookmarks
        tool and View > Show Bookmarks both land here. The tree refreshes on
        entry so a panel opened after page ops is current."""
        if not hasattr(self, "left_panel"):
            return
        self.left_panel.set_active_tool("bookmarks")
        if hasattr(self, "act_add_text") and self.act_add_text.isChecked():
            self.act_add_text.setChecked(False)
        self._refresh_bookmarks()
        self._sync_show_comments_check()
        self._sync_show_bookmarks_check()

    def _on_show_bookmarks_toggled(self, checked: bool) -> None:
        """View > Show Bookmarks (Cmd+Alt+B): show the Bookmarks panel via
        the strip's Bookmarks tool; unchecking returns to Select (the
        Show Comments precedent)."""
        if self._bookmarks_check_syncing:
            return
        if checked:
            if self.document is None:
                self._sync_show_bookmarks_check()   # snap back: no document
                return
            self.left_panel.set_active_tool("bookmarks")
            self._on_tool_selected("bookmarks")
        else:
            self.left_panel.set_active_tool("select")
            self._on_tool_selected("select")

    def _sync_show_bookmarks_check(self) -> None:
        """Mirror the Show Bookmarks toggle onto the strip's ACTUAL state
        (the strip is the single source of truth for the active tool; the
        action is just a menu view of it)."""
        if not hasattr(self, "act_toggle_bookmarks") \
                or not hasattr(self, "left_panel"):
            return                      # construction-order guard
        on = self.left_panel.active_tool() == "bookmarks"
        if self.act_toggle_bookmarks.isChecked() == on:
            return
        self._bookmarks_check_syncing = True
        try:
            self.act_toggle_bookmarks.setChecked(on)
        finally:
            self._bookmarks_check_syncing = False

    def _refresh_bookmarks(self) -> None:
        """Rebuild the Bookmarks tree from the active doc's outline: runs on
        every ``_sync_all`` (structural ops incl. set_outline + their
        undo/redo, tab switches, open/close, save reloads) and on panel
        entry, so the tree always mirrors ``working``."""
        panel = getattr(self, "bookmark_panel", None)
        if panel is not None:
            panel.refresh()

    def _set_outline(self, entries) -> None:
        """The Bookmarks panel's commit callable: ONE StructuralCommand per
        panel gesture (add/rename/delete), through the same funnel as the
        page ops -- ``set_outline`` snapshots + bakes + mutates, then
        ``_after_structural_op`` rebuilds chrome (incl. this panel's tree)
        and pushes the undoable boundary. Validation failures surface as a
        flash, not a crash (the funnel's except path)."""
        self._run_structural(lambda d: d.set_outline(entries))

    # ------------------------------------------------------------------
    # Find & Replace callbacks (the panel calls these; all model/undo
    # coupling lives here, REFLOW_SPEC §R5.1)
    # ------------------------------------------------------------------
    def _find_search(self, query: str, match_case: bool, whole_word: bool):
        """Cross-page model search -> list[Match] (or [] when no document)."""
        if self.document is None:
            return []
        return self.document.find_all(query, match_case=match_case,
                                      whole_word=whole_word)

    def _find_navigate(self, match) -> None:
        """Scroll the continuous view to ``match``'s page and select+highlight
        its box (re-found by identity, since the live object is rebuilt every
        reload)."""
        if self.document is None or match is None:
            return
        page = match.page_index
        scroll = getattr(self.view, "scroll_to_page", None)
        if callable(scroll):
            scroll(page)
        box = self._box_for_match(match)
        if box is not None:
            select = getattr(self.view, "select_box", None)
            if callable(select):
                select(box)

    def _box_for_match(self, match):
        """Resolve a Match's stable identity to the live box object on its page
        (spans + new boxes)."""
        if self.document is None or match is None:
            return None
        page = match.page_index
        for box in (*self.document.spans(page),
                    *self.document.new_boxes(page)):
            if box.identity == match.box_identity:
                return box
        return None

    def _find_replace(self, match, replacement: str):
        """Replace ONE match: build the box's new staged text (the match span
        swapped) and push it through the normal text-edit command, so the replace
        rewraps paragraphs through the bake and is one undo step (REFLOW_SPEC
        §R5.1)."""
        if self.document is None or match is None:
            return None
        box = self._box_for_match(match)
        if box is None:
            return None
        page = match.page_index
        old_text = self.document.staged_text(page, box)
        new_text = self.document.replaced_text(
            page, box, match.start, match.end, replacement)
        if new_text == old_text:
            return None
        cmd = EditRunCommand(self.document, self.view, page, box,
                             old_text, new_text)
        self.undo_stack.push(cmd)
        self._sync_status()
        return match

    def _find_replace_all(self, query: str, replacement: str,
                          match_case: bool, whole_word: bool) -> int:
        """Replace EVERY occurrence across all pages as ONE undo macro
        (REFLOW_SPEC §R5.1). Each affected box is edited once: all its matches
        are spliced into a single new staged text, so a paragraph with several
        hits reflows once and the whole operation undoes in a single step."""
        if self.document is None or not query:
            return 0
        matches = self.document.find_all(query, match_case=match_case,
                                         whole_word=whole_word)
        if not matches:
            return 0
        # Group matches by box identity so each box is edited exactly once. The
        # spans within a box are replaced right-to-left so earlier offsets stay
        # valid as the text length changes.
        by_box: dict = {}
        for m in matches:
            by_box.setdefault((m.page_index, m.box_identity), []).append(m)

        total = 0
        self.undo_stack.beginMacro(
            f"Replace all ‘{query}’ → ‘{replacement}’")
        try:
            for (page, _identity), group in by_box.items():
                box = self._box_for_match(group[0])
                if box is None:
                    continue
                old_text = self.document.staged_text(page, box)
                new_text = old_text
                for m in sorted(group, key=lambda x: x.start, reverse=True):
                    new_text = (new_text[:m.start] + replacement
                                + new_text[m.end:])
                if new_text == old_text:
                    continue
                cmd = EditRunCommand(self.document, self.view, page, box,
                                     old_text, new_text)
                self.undo_stack.push(cmd)
                total += len(group)
        finally:
            self.undo_stack.endMacro()
        self._sync_status()
        return total

    # ===================================================================
    # Inspector dock (BUILD_SPEC §5.1)
    # ===================================================================
    def _build_left_dock(self) -> None:
        """Host the LEFT panel: an Adobe-style tool strip (Select / Text Edit /
        Add Text / Find) on top of the Format/Inspector panel (REFLOW_SPEC §R4.1).

        A fixed ``QDockWidget`` on the LEFT (replacing the old right Format dock).
        The Inspector reflects the selection via ``set_target`` and writes back
        through ``inspector.styleEdited -> view.apply_style`` (one undo step each);
        the tool strip drives the view's mode + the Find panel."""
        self.inspector = Inspector()
        self.inspector.styleEdited.connect(self._on_style_edited)
        # ANNOTATION style writes (annotations & markup §5.5): one control
        # change = one 'style' AnnotCommand on a staged selection, or a
        # session-default write while a tool is armed with nothing selected.
        self.inspector.annotStyleEdited.connect(self._on_annot_style_edited)
        # A new box is created with the inspector's current font/size/color/bold/
        # italic (BUILD_SPEC §3.5 step 3): give the canvas a 0-arg provider that
        # reads the inspector's live control values at add time.
        provider_fn = getattr(self.view, "set_add_style_provider", None)
        if callable(provider_fn):
            provider_fn(self.inspector.current_values)

        self.left_panel = LeftPanel(self.inspector, make_icon)
        # Tell the canvas which widget is the Format panel so a click into it
        # (to change font / size / colour of HIGHLIGHTED text) does not commit
        # the inline editor out from under the user -- it keeps the edit and the
        # highlight, then the style path re-applies and restores the selection.
        reg = getattr(self.view, "set_format_panel", None)
        if callable(reg):
            reg(self.inspector)

        # The Find & Replace panel lives in the left column's content stack,
        # swapped in when the Find tool is active (REFLOW_SPEC §R5.1). It is pure
        # chrome: the window injects the model search + the staged-replace
        # callbacks so all undo coupling stays here.
        self.find_panel = FindReplacePanel(
            search=self._find_search,
            navigate=self._find_navigate,
            replace=self._find_replace,
            replace_all=self._find_replace_all,
            icon_factory=make_icon,
        )
        self.find_panel.closed.connect(self._on_find_closed)
        self.left_panel.install_find_panel(self.find_panel)

        # The Comments panel is the left column's THIRD stacked page
        # (annotations & markup §5.4) -- installed after the find panel so
        # it takes stack index 2, the conflict-ledger slot. Pure chrome:
        # the window injects the list/jump/edit/delete callables so all
        # model + undo coupling stays here.
        self.comments_panel = CommentsPanel(
            list_annots=self._list_all_annots,
            jump=self._jump_to_annot,
            edit=self._edit_annot_from_panel,
            delete=self._delete_annot_from_panel,
        )
        self.left_panel.install_comments_panel(self.comments_panel)

        # The Bookmarks panel is the left column's FOURTH stacked page
        # (navigation M1) -- installed after the comments panel so it takes
        # stack index 3, the conflict-ledger slot. Pure chrome: the window
        # injects the outline read/write + page jump callables, so the
        # structural-undo coupling stays here (_set_outline funnels ONE
        # StructuralCommand per panel gesture).
        self.bookmark_panel = BookmarkPanel(
            get_outline=lambda: (
                self.document.outline() if self.document else []),
            set_outline=self._set_outline,
            jump_to_page=self._set_view_page,
            current_page=self.view_page_index,
            icon_factory=make_icon,
        )
        self.left_panel.install_bookmark_panel(self.bookmark_panel)

        # The Markup tools palette (Charcoal redesign): shown when the rail's
        # Markup mode is active. Pure chrome -- its buttons drive the existing
        # checkable _markup_actions, so arming/exclusivity/undo all stay here.
        self.markup_panel = self._build_markup_panel()
        self.left_panel.install_markup_panel(self.markup_panel)

        dock = QDockWidget("Tools", self)
        dock.setObjectName("LeftToolDock")
        dock.setFeatures(QDockWidget.NoDockWidgetFeatures)   # fixed, no close/float
        dock.setAllowedAreas(Qt.LeftDockWidgetArea)
        # No custom dock title-bar widget: the dock did not honor a fixed-height
        # title slot, so the panel content overlapped it and the header was
        # occluded. An EMPTY title-bar widget collapses the slot; the "Format" /
        # "Find & Replace" header now lives as the first row INSIDE LeftPanel,
        # laid out by the panel's own layout so nothing can paint over it.
        dock.setTitleBarWidget(QWidget())
        dock.setWidget(self.left_panel)
        # The panel must NOT drive the dock width: with Ignored horizontal
        # policy the dock keeps its set/dragged width and the panel just fills
        # it, so a content change (e.g. the inspector showing its controls on
        # selection) can't auto-resize the dock and resize the canvas out from
        # under a mounting editor.
        self.left_panel.setSizePolicy(QSizePolicy.Policy.Ignored,
                                      QSizePolicy.Policy.Preferred)
        # Rail (64) + Format panel. The dock is USER-RESIZABLE (drag its right
        # edge, like the Pages bar): a width RANGE, not a fixed width, so the
        # separator is draggable. The rail stays 64; the rest is the Format
        # panel the user can widen/narrow to give the document room.
        dock.setMinimumWidth(theme.RAIL_WIDTH + 176)
        dock.setMaximumWidth(theme.RAIL_WIDTH + 384)
        self.addDockWidget(Qt.LeftDockWidgetArea, dock)
        # Keep the historical attribute name so _sync_actions (which toggles the
        # Format dock's visibility in the empty state) keeps working.
        self.format_dock = dock
        self.left_dock = dock
        # Sensible default width on a resizable dock (resizeDocks works where a
        # plain resize() would be overridden by the dock layout).
        self.resizeDocks([dock], [theme.RAIL_WIDTH + 240], Qt.Horizontal)

    def _build_markup_panel(self) -> QWidget:
        """The Markup tools palette (Charcoal redesign): a tidy list of the
        markup tools, shown in the left contextual panel when the rail's Markup
        mode is active. Each row is a QToolButton bound via setDefaultAction to
        the existing checkable ``_markup_actions`` entry, so it shows the tool's
        icon, reflects the armed (checked) state, and arms the tool on click --
        all the model/undo/exclusivity coupling stays in the action handlers."""
        panel = QWidget()
        panel.setObjectName("MarkupPanel")
        # Discoverable handles for the palette rows (used by the chrome tests
        # to verify each markup tool relocated here from the old toolbar group).
        self._markup_panel_buttons: dict[str, QToolButton] = {}
        lay = QVBoxLayout(panel)
        lay.setContentsMargins(14, 14, 14, 14)
        lay.setSpacing(3)
        groups = (
            ("MARKUP", ("highlight", "underline", "strikeout", "squiggly")),
            ("ANNOTATE", ("note", "ink")),
            ("SHAPES", ("rect", "ellipse", "line", "arrow")),
        )
        for gi, (title, kinds) in enumerate(groups):
            if gi:
                lay.addSpacing(12)
            head = QLabel(title)
            head.setObjectName("InspectorSectionHeader")
            head.setFont(theme.caps_header_font())
            lay.addWidget(head)
            lay.addSpacing(2)
            for kind in kinds:
                act = self._markup_actions.get(kind)
                if act is None:
                    continue
                btn = QToolButton()
                btn.setObjectName("MarkupToolButton")
                btn.setDefaultAction(act)   # icon + label + checked state + trigger
                btn.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
                btn.setIconSize(QSize(theme.ICON_SIZE, theme.ICON_SIZE))
                btn.setCursor(Qt.PointingHandCursor)
                self._markup_panel_buttons[kind] = btn
                lay.addWidget(btn, 0, Qt.AlignLeft)
        lay.addStretch(1)
        return panel

    # ===================================================================
    # Status bar (BUILD_SPEC §6.6)
    # ===================================================================
    def _build_statusbar(self) -> None:
        bar = QStatusBar()
        bar.setObjectName("MainStatus")
        bar.setSizeGripEnabled(False)
        bar.setFixedHeight(theme.STATUS_HEIGHT)
        self.setStatusBar(bar)

        # Transient messages route through the filename label (see _toast),
        # NOT QStatusBar.showMessage(), which painted ON TOP of the persistent
        # left-cluster labels and produced overlapping/garbled text.
        self._toast_active = False
        self._toast_timer = QTimer(self)
        self._toast_timer.setSingleShot(True)
        self._toast_timer.timeout.connect(self._restore_status)
        # The view mode a gesture-hint toast belongs to ("crop" /
        # "place_image"): the hint is dismissed the moment the mode exits,
        # instead of lingering until toast expiry while "Esc to cancel" no
        # longer cancels anything.
        self._hint_mode: str | None = None

        # Charcoal Studio footer: ONE centered cluster --
        #   font · size · style    [ ‹ N / M › ]    edit status
        # The filename + saved dot live in the TOP bar's doc title now; the
        # footer carries the page navigator (a self-contained pill) plus the
        # font readout and the edit-state counter.
        center = QWidget()
        cl = QHBoxLayout(center)
        cl.setContentsMargins(0, 0, 0, 0)
        cl.setSpacing(11)
        cl.addStretch(1)

        # Form badge (forms §4): "Form · N fields", shown only with an AcroForm.
        self.form_badge = QLabel()
        self.form_badge.setObjectName("StatusLabel")
        self.form_badge.setFont(theme.ui_font(12))
        self.form_badge.setStyleSheet(f"color: {theme.ACCENT};")
        self.form_badge.hide()
        cl.addWidget(self.form_badge)

        self.font_chip = QLabel()
        self.font_chip.setObjectName("StatusLabel")
        self.font_chip.setFont(theme.ui_font(12))
        self.font_chip.hide()
        cl.addWidget(self.font_chip)

        cl.addWidget(self._footer_nav)        # page navigator (its own pill)

        self.edit_count_label = QLabel("No edits")
        self.edit_count_label.setObjectName("StatusLabel")
        self.edit_count_label.setFont(theme.ui_font(12))
        cl.addWidget(self.edit_count_label)
        cl.addStretch(1)
        bar.addWidget(center, 1)

        # The footer page-nav is the page group _sync_actions hides in the empty
        # state (it is a self-contained pill now -- no surrounding dividers).
        self._page_group_actions = [self._footer_nav]

    # ===================================================================
    # PageView wiring (BUILD_SPEC §5.2 signals, connected defensively)
    # ===================================================================
    def _wire_view(self) -> None:
        # ASSUMPTION (BUILD_SPEC §5.2): the spec-compliant PageView emits these
        # signals. We connect only the ones the running PageView actually has,
        # so the window works against both the spec canvas and the current MVP
        # (whose selection model is still landing). The selection / box-command
        # signals below drive the inspector + the generalized undo command; on
        # the MVP canvas (which lacks them) they simply never fire and the
        # inspector stays in its empty state.
        self._connect_signal("editCommitted", self._on_edit_committed)
        self._connect_signal("editCommittedRich", self._on_edit_committed_rich)
        self._connect_signal("editStarted", self._on_edit_started)
        self._connect_signal("editFinished", self._on_edit_finished)
        self._connect_signal("pageChanged", self._on_page_changed)
        self._connect_signal("zoomChanged", self._on_zoom_changed)
        # Full-editor signals (BUILD_SPEC §3.4).
        self._connect_signal("selectionChanged", self._on_selection_changed)
        self._connect_signal("boxCommandRequested", self._on_box_command_requested)
        self._connect_signal("groupRequested", self._on_group_requested)
        self._connect_signal("ungroupRequested", self._on_ungroup_requested)
        self._connect_signal("multiSelectionChanged",
                             lambda *_: self._sync_actions())
        # Annotation signals (annotations & markup §5.1 / §5.2 / §5.3).
        self._connect_signal("annotCommandRequested",
                             self._on_annot_command_requested)
        self._connect_signal("annotSelectionChanged",
                             self._on_annot_selection_changed)
        self._connect_signal("noteEditRequested", self.open_note_editor)
        # Form fill (forms §4): the canvas reports one fill intent per
        # gesture; the window funnels it into ONE FormFieldCommand.
        self._connect_signal("formCommandRequested",
                             self._on_form_command_requested)
        self._connect_signal("statusMessage", self._toast)
        self._connect_signal("boxAdded", self._on_box_added)
        self._connect_signal("styleApplied", self._on_style_applied)
        self._connect_signal("geometryChanged", self._on_geometry_changed)
        self._connect_signal("boxDeleted", self._on_box_deleted)
        self._connect_signal("overflowChanged", self._on_overflow_changed)
        # Crop tool (ws6 §2.6): the canvas reports the drawn rect; the window
        # owns the scope dialog + the structural op.
        self._connect_signal("cropRectSelected", self._on_crop_rect_selected)

    def _connect_signal(self, name: str, slot) -> None:
        sig = getattr(self.view, name, None)
        if sig is not None and hasattr(sig, "connect"):
            try:
                sig.connect(slot)
            except (TypeError, RuntimeError):
                pass

    def _wire_undo(self) -> None:
        self.undo_stack.canUndoChanged.connect(self._on_can_undo_changed)
        self.undo_stack.canRedoChanged.connect(self._on_can_redo_changed)
        self.undo_stack.cleanChanged.connect(self._on_clean_changed)
        self.undo_stack.undoTextChanged.connect(self._refresh_undo_tooltips)
        self.undo_stack.redoTextChanged.connect(self._refresh_undo_tooltips)

    # ===================================================================
    # PageView signal handlers
    # ===================================================================
    def _on_edit_committed(self, page_index: int, span, new_text: str) -> None:
        """A run was edited in the canvas. Wrap it in an undo command and push.

        We read the span's CURRENT staged text as the command's ``old_text`` so
        undo restores exactly the pre-edit state. The command's first ``redo``
        (run on push) re-stages ``new_text`` -- idempotent with what the canvas
        already staged.
        """
        if self.document is None:
            return
        old_text = self.document.staged_text(page_index, span)
        if new_text == old_text:
            return
        cmd = EditRunCommand(self.document, self.view, page_index, span,
                             old_text, new_text)
        self.undo_stack.push(cmd)
        self._sync_status()

    def _on_edit_committed_rich(self, page_index: int, span,
                                payload: dict) -> None:
        """A commit carrying per-selection rich styling (or clearing it).
        ``payload`` = {"text": str, "runs": tuple | None}."""
        if self.document is None or not isinstance(payload, dict):
            return
        new_text = payload.get("text", "")
        runs = payload.get("runs")
        old_text = self.document.staged_text(page_index, span)
        old_runs = self.document.staged_runs(page_index, span)
        if new_text == old_text and runs == old_runs:
            return
        cmd = RichEditCommand(self.document, self.view, page_index, span,
                              old_text, new_text, runs)
        self.undo_stack.push(cmd)
        self._sync_status()

    def _on_edit_started(self, span, resolved) -> None:
        """Show the font chip for the active edit: family + fidelity dot.

        Also point the Format panel at the box under edit (ws2 M1 §2.6): a
        direct-entry edit (double-click straight into text, no prior select)
        used to leave the inspector in its placeholder state for the whole
        edit. ``set_target`` populates under ``_loading``, so seeding the
        controls here never echoes back as a styleEdited write."""
        family = getattr(resolved, "source_name", None) or getattr(
            resolved, "qt_family", "")
        tier = getattr(resolved, "tier", 3)
        self._show_font_chip(family, tier)
        if self.document is not None and span is not None:
            page = getattr(span, "page_index", self.view_page_index())
            try:
                style = self.document.effective_style(page, span)
            except Exception:  # noqa: BLE001 - stale box must not crash chrome
                style = None
            if style is not None:
                self.inspector.set_target(span, style)

    def _on_edit_finished(self) -> None:
        self.font_chip.hide()

    def _on_page_changed(self, page_index: int) -> None:
        # A page change can leave a stale selection (its overlay is on the old
        # page); clear the inspector so it reflects the new page's empty state
        # until the user selects something. The view owns its own selection
        # lifecycle; this only resets the chrome.
        self.inspector.set_target(None, None)
        self._sync_page_indicator()
        # Continuous scroll moved the current page: highlight it in the right
        # thumbnail sidebar so the strip tracks the scrolled-into-view page
        # (set_current_page does NOT re-emit pageActivated, so this is a pure
        # highlight update with no scroll feedback loop).
        if hasattr(self, "sidebar") and self.document is not None:
            self.sidebar.set_current_page(page_index)
        self._sync_actions()

    def _on_zoom_changed(self, zoom: float) -> None:
        self._sync_zoom_indicator()

    # ===================================================================
    # Full-editor signal handlers (BUILD_SPEC §3.4 / §5.4)
    # ===================================================================
    def _on_group_requested(self, boxes) -> None:
        """Shift-selected boxes -> fuse into ONE editable paragraph (manual
        override of the auto-detector). Undoable as one GroupingCommand. After
        the regroup the merged paragraph is reselected so the user can edit it."""
        if self.document is None or not boxes or len(boxes) < 2:
            return
        before = self.document.manual_grouping_snapshot()
        page = getattr(boxes[0], "page_index", self.view_page_index())
        if not self.document.group_boxes(page, boxes):
            return
        self.undo_stack.push(
            GroupingCommand(self.document, self, before, "Group boxes"))
        self._refresh_after_grouping()
        self._select_paragraph_covering(page, boxes[0])
        self._toast(f"Grouped {len(boxes)} boxes into one", 3000)

    def _on_ungroup_requested(self, box) -> None:
        """A paragraph box -> split back into its individual lines. Undoable."""
        if self.document is None or box is None:
            return
        before = self.document.manual_grouping_snapshot()
        page = getattr(box, "page_index", self.view_page_index())
        if not self.document.ungroup_box(page, box):
            return
        self.undo_stack.push(
            GroupingCommand(self.document, self, before, "Ungroup box"))
        self._refresh_after_grouping()
        self._toast("Ungrouped into separate lines", 3000)

    def _refresh_after_grouping(self) -> None:
        """Rebuild the canvas after a manual group/ungroup changed which boxes
        exist, and resync chrome."""
        self.view.clear_selection()
        self.view.reload()
        self._sync_actions()
        self._sync_status()

    def _select_paragraph_covering(self, page: int, box) -> None:
        """Select the (now merged) paragraph box that covers ``box``'s origin,
        so the user lands on the grouped box ready to edit."""
        try:
            target = getattr(box, "bbox", None)
            if target is None:
                return
            cx = (target[0] + target[2]) / 2.0
            cy = (target[1] + target[3]) / 2.0
            for b in self.document.spans(page):
                bb = b.bbox
                if bb[0] <= cx <= bb[2] and bb[1] <= cy <= bb[3]:
                    self.view.select_box(b)
                    return
        except (AttributeError, IndexError, TypeError):
            pass

    def _on_selection_changed(self, box) -> None:
        """A box was selected (or deselected when ``box is None``). Populate the
        inspector from the model's effective style and refresh action enablement
        + the status hint (BUILD_SPEC §5.4). An image-like selection keeps the
        Inspector in its placeholder state (it is pure TEXT chrome) and shows
        the image size in the status chip instead (images & signatures §4)."""
        if self.document is None or box is None:
            self.inspector.set_target(None, None)
            self._sync_selection_status(None)
            self._sync_actions()
            return
        ident = getattr(box, "identity", None)
        if isinstance(ident, tuple) and len(ident) >= 2 \
                and ident[1] in ("img", "xim"):
            self.inspector.set_target(None, None)
            # The Format panel is pure text chrome: tell the user what the
            # SELECTED image supports instead of asking for a text box.
            self.inspector.set_hint(
                "An image is selected. Drag it to move it; press Delete to "
                "remove it. Style controls apply to text boxes only.")
            x0, y0, x1, y1 = box.rect
            label = "Image" if ident[1] == "img" else "Page image"
            self.font_chip.setText(
                f"<span style='color:{theme.TEXT_SECONDARY};'>"
                f"{label} · {x1 - x0:.0f} × {y1 - y0:.0f} pt</span>")
            self.font_chip.show()
            self._sync_actions()
            return
        page = self.view_page_index()
        try:
            style = self.document.effective_style(page, box)
        except Exception:  # noqa: BLE001 - a stale box ref must not crash chrome
            style = None
        self.inspector.set_target(box, style)
        self._sync_selection_status(style)
        self._sync_actions()

    def _on_box_command_requested(self, kind: str, box, params: dict) -> None:
        """The canvas asks for a box mutation (style/move/resize/delete/add).
        Wrap it in the single ``BoxCommand`` and push it -- the ONE mutation
        route onto the undo stack (BUILD_SPEC §3.4 / §5.3).

        Two add-flow kinds are handled specially:
          * ``"add"``  -- the view passes ``box=None``; after the push we hand the
            freshly created NewBox back to the view (``begin_add_box_edit``) so it
            selects + drops into text edit.
          * ``"cancel_add"`` -- the empty-add cleanup (§3.5): a brand-new box left
            empty. We POP the just-pushed add off the undo stack so a stray click
            that added nothing leaves no box (and no spurious undo step)."""
        if self.document is None:
            return
        if kind == "cancel_add":
            self._cancel_pending_add()
            return
        if kind == "xim_move":
            # Not a BoxCommand kind: the window expands the gesture into
            # the delete + reinsert MACRO (images & signatures §5, M3).
            self._move_existing_image(box, dict(params or {}))
            return
        page = self.view_page_index()
        cmd = BoxCommand(self.document, self.view, page, box, kind, dict(params or {}))
        self.undo_stack.push(cmd)
        if kind == "add":
            new_box = getattr(cmd, "_box", None)
            if new_box is not None:
                self._begin_add_box_edit(new_box)
            # Adding one box turns the tool back off (one click = one box).
            if self.act_add_text.isChecked():
                self.act_add_text.setChecked(False)
        elif kind == "img_add":
            # Select the freshly placed image so its rect handles show
            # (images & signatures §4) -- no text-edit drop-in, no cancel
            # path (placement always carries bytes; no "empty image").
            new_box = getattr(cmd, "_box", None)
            if new_box is not None:
                fn = getattr(self.view, "select_box", None)
                if callable(fn):
                    try:
                        fn(new_box)
                    except (TypeError, RuntimeError):
                        pass
        self._sync_status()
        self._sync_actions()

    def _move_existing_image(self, xim, params: dict) -> None:
        """Move an EXISTING page image (images & signatures §5, M3): ONE Qt
        macro of two BoxCommands -- ``xim_delete`` of the occurrence + an
        ``img_add`` (kind="moved") of the extracted bytes (SMask recombined,
        ``extract_image_bytes``) at the offset rect -- so the model history
        holds two commands while the UI undoes in ONE step (the Replace All
        macro precedent). The reinserted ImageBox is selected, so follow-up
        drags/nudges ride the cheap ``img_move`` path instead of stacking
        macros. Documented limitation (§5, toast): the deletion redaction
        removes ANY image occurrence its rect touches."""
        if self.document is None or xim is None:
            return
        dx = float(params.get("dx", 0.0))
        dy = float(params.get("dy", 0.0))
        if abs(dx) < 1e-9 and abs(dy) < 1e-9:
            return
        page = getattr(xim, "page_index", self.view_page_index())
        try:
            data, natural = self.document.extract_image_bytes(xim.xref)
        except Exception as exc:  # noqa: BLE001 - exotic codec/broken stream
            self._flash_error(f"Could not move this image: {exc}")
            return
        x0, y0, x1, y1 = xim.rect
        new_rect = (x0 + dx, y0 + dy, x1 + dx, y1 + dy)
        self.undo_stack.beginMacro("Move page image")
        try:
            self.undo_stack.push(BoxCommand(
                self.document, self.view, page, xim, "xim_delete", {}))
            add_cmd = BoxCommand(
                self.document, self.view, page, None, "img_add",
                {"rect": new_rect, "image": data, "kind": "moved",
                 "natural_px": natural})
            self.undo_stack.push(add_cmd)
        finally:
            self.undo_stack.endMacro()
        new_box = getattr(add_cmd, "_box", None)
        if new_box is not None:
            fn = getattr(self.view, "select_box", None)
            if callable(fn):
                try:
                    fn(new_box)
                except (TypeError, RuntimeError):
                    pass
        self._toast("Moved image — overlapping images on this spot are "
                    "replaced")
        self._sync_status()
        self._sync_actions()

    def _on_annot_command_requested(self, kind: str, ref, params: dict) -> None:
        """The canvas asks for an annotation mutation (annotations & markup
        §5.1): wrap it in ONE ``AnnotCommand`` and push -- the single annot
        mutation route onto the shared undo stack. ``params`` may carry the
        gesture's own ``page_index`` (continuous scroll: the band can land on
        a non-current page); 'add' fills unspecified style fields from the
        window's per-tool session defaults (§3.1 -- the model takes explicit
        values)."""
        if self.document is None:
            return
        params = dict(params or {})
        page = params.pop("page_index", None)
        if page is None:
            page = self.view_page_index()
        if kind == "add":
            for field, value in self._annot_defaults.get(
                    params.get("kind"), {}).items():
                params.setdefault(field, value)
        cmd = AnnotCommand(self.document, self.view, page, ref, kind, params,
                           on_change=self._refresh_comments)
        self.undo_stack.push(cmd)
        if kind == "add" and params.get("kind") == "note":
            # A fresh sticky note opens its popup editor immediately (§5.3);
            # the note tool stays armed for the next one.
            spec = getattr(cmd, "_box", None)
            if spec is not None:
                self.open_note_editor(spec)
        self._sync_status()
        self._sync_actions()

    def _on_form_command_requested(self, field, params: dict) -> None:
        """The canvas asks for a form fill (forms §4): wrap it in ONE
        ``FormFieldCommand`` and push -- the single fill route onto the
        shared undo stack (pattern: ``_on_box_command_requested``).

        The value is normalized EXACTLY like ``stage_form_value`` will
        normalize it, then no-op-guarded against the current effective
        value: pushing a command whose model mutator stages nothing would
        break the 1:1 Qt/model lockstep (its undo would pop the PREVIOUS
        model command), the same reason ``_on_edit_committed`` drops an
        unchanged text commit."""
        if self.document is None or field is None:
            return
        value = (params or {}).get("value")
        if field.kind == "text":
            value = str(value)
            if field.max_len > 0:
                value = value[: field.max_len]
        elif field.kind == "checkbox":
            value = bool(value)
        else:                              # radio | combo
            value = str(value)
        if value == self.document.effective_form_value(
                field.page_index, field):
            return
        cmd = FormFieldCommand(self.document, self.view, field, value)
        self.undo_stack.push(cmd)
        self._sync_status()
        self._sync_actions()

    def _on_annot_selection_changed(self, record) -> None:
        """The canvas annot selection changed (annotations & markup §4.3):
        mirror the record, gate Delete Annotation on it, and point the
        Inspector's ANNOTATION section at it (§5.5) -- or back at the armed
        tool's session defaults / the empty state when cleared."""
        self._selected_annot = record
        self.act_delete_annot.setEnabled(
            self.document is not None and record is not None)
        if record is not None:
            self.inspector.set_annot_target(record)
        else:
            self._sync_annot_inspector()

    def _armed_annot_kind(self) -> str | None:
        """The armed annot tool kind off the view, or None."""
        fn = getattr(self.view, "annot_mode_kind", None)
        if callable(fn):
            try:
                return fn()
            except (TypeError, RuntimeError):
                return None
        return None

    def _sync_annot_inspector(self) -> None:
        """No annot selected: the Inspector edits the ARMED tool's session
        defaults (annotations & markup §5.5, no undo entries), or drops the
        ANNOTATION section entirely when no annot tool is armed."""
        if not hasattr(self, "inspector"):
            return                    # construction-order guard
        kind = self._armed_annot_kind()
        if self.document is not None and kind is not None:
            target = dict(self._annot_defaults.get(kind, {}))
            target["kind"] = kind
            self.inspector.set_annot_target(target)
        else:
            self.inspector.set_annot_target(None)

    def _on_annot_style_edited(self, change: dict) -> None:
        """One Inspector ANNOTATION control changed (annotations & markup
        §5.5): a STAGED selection takes ONE 'style' AnnotCommand (one undo
        step); with no selection and a tool armed, the change writes the
        per-tool session default in place (no undo entry). Existing file
        annots never reach here (their section is read-only)."""
        if self.document is None or not change:
            return
        record = self._selected_annot
        if record is not None:
            if getattr(record, "is_existing", False):
                return                # §8: no style edits on foreign annots
            self._on_annot_command_requested(
                "style", record.identity,
                {"page_index": record.identity[0], **change})
            return
        kind = self._armed_annot_kind()
        if kind is not None:
            self._annot_defaults.setdefault(kind, {}).update(change)

    def _delete_selected_annot(self) -> None:
        """Tools > Delete Annotation: ONE 'delete' command for the selected
        annotation (a staged spec leaves the map; an existing file annot
        gains a deleted override)."""
        record = self._selected_annot
        if self.document is None or record is None:
            return
        self._on_annot_command_requested(
            "delete", record.identity, {"page_index": record.identity[0]})

    # ------------------------------------------------------------------
    # Note popup (annotations & markup §5.3) -- non-modal, viewport-child
    # ------------------------------------------------------------------
    def open_note_editor(self, ref) -> None:
        """Open the note popup on ``ref`` (an AnnotRecord, an AnnotSpec, or
        an identity tuple) -- the §5.3 seam, reachable programmatically by
        tests (NO QDialog.exec). Anchored next to the note's scene rect;
        commit emits ONE 'contents' command only when the text changed."""
        if self.document is None:
            return
        ident = getattr(ref, "identity", ref)
        page = ident[0]
        record = None
        for rec in self.document.annotations(page):
            if rec.identity == ident:
                record = rec
                break
        if record is None:
            return
        self.close_note_editor()
        popup = NotePopup(self.view.viewport(), record.identity, page,
                          record.contents, self._commit_note_text,
                          self._on_note_popup_closed)
        self._note_popup = popup
        self._position_note_popup(popup, record.identity)
        popup.show()
        popup.raise_()
        popup.editor.setFocus()

    def close_note_editor(self) -> None:
        """Dismiss any open note popup WITHOUT committing (cancel path)."""
        popup = self._note_popup
        if popup is not None:
            popup.cancel()              # idempotent; clears _note_popup
        self._note_popup = None

    def _on_note_popup_closed(self) -> None:
        self._note_popup = None

    def _position_note_popup(self, popup, identity) -> None:
        """Anchor the popup beside the note's scene rect, clamped into the
        viewport so it never opens half off-screen."""
        viewport = self.view.viewport()
        pos = QPoint(24, 24)
        fn = getattr(self.view, "annot_scene_rect", None)
        rect = fn(identity) if callable(fn) else None
        if rect is not None:
            top_right = self.view.mapFromScene(rect.topRight())
            pos = QPoint(int(top_right.x()) + 10, int(top_right.y()) - 4)
        size = popup.sizeHint()
        x = max(4, min(pos.x(), viewport.width() - size.width() - 4))
        y = max(4, min(pos.y(), viewport.height() - size.height() - 4))
        popup.move(x, y)

    def _commit_note_text(self, ref, page_index: int, text: str) -> None:
        """The popup's single commit funnel: ONE 'contents' command. Only
        reachable when the text changed, and only while the note still
        exists (an undo may have removed it under an open popup)."""
        if self.document is None:
            return
        if not any(rec.identity == ref
                   for rec in self.document.annotations(page_index)):
            return
        self._on_annot_command_requested(
            "contents", ref, {"page_index": page_index, "text": text})

    def _cancel_pending_add(self) -> None:
        """Roll back a pending 'add' that was left empty (empty-add cleanup,
        §3.5), removing BOTH the NewBox and its undo step so a stray click leaves
        no box and no redoable ghost.

        Mechanism (verified): revert the model add via ``document.undo()``, then
        mark the top BoxCommand obsolete and pop it with ``undo_stack.undo()`` --
        Qt drops an obsolete command WITHOUT replaying its ``undo`` (so the model
        is reverted exactly once). The command must be the freshly pushed add at
        the top of the stack; if anything else is on top we leave the stack
        alone (defensive: a stale cancel_add never disturbs unrelated history)."""
        idx = self.undo_stack.index()
        if idx > 0:
            top = self.undo_stack.command(idx - 1)
            if isinstance(top, BoxCommand) and getattr(top, "_kind", None) == "add":
                self.document.undo()            # revert the model's add
                top.setObsolete(True)           # mark for removal
                self.undo_stack.undo()          # drop it (skips its undo())
        self.inspector.set_target(None, None)
        self._sync_selection_status(None)
        self._sync_all()

    def _on_box_added(self, box) -> None:
        """The canvas added a box on its own (e.g. via its add flow). Keep the
        status/edit count current; selection is driven by selectionChanged."""
        self._sync_status()
        self._sync_actions()

    def _on_style_applied(self, box, overrides: dict) -> None:
        """After the canvas applied a style live, refresh the inspector so its
        controls reflect the committed effective style (e.g. a size clamp)."""
        self._refresh_inspector_for(box)
        self._sync_status()

    def _on_geometry_changed(self, box) -> None:
        self._refresh_inspector_for(box)
        self._sync_status()

    def _on_box_deleted(self, box) -> None:
        self.inspector.set_target(None, None)
        self._sync_selection_status(None)
        self._sync_status()
        self._sync_actions()

    def _on_overflow_changed(self, overflow_pt: float) -> None:
        """Post / clear a transient status note when the selected paragraph's
        reflowed text grows past (or back within) its box bottom (REFLOW_SPEC
        §R2.5), so the overprint with the content below is never silent. The
        danger edge on the overlay is painted by the view itself."""
        if overflow_pt > 0.5:
            self._toast(
                "Text overflows box — the extra lines draw over the content "
                "below.", 6000)
        else:
            # Clear only an overflow message; leave other transient notes alone.
            msg = self.statusBar().currentMessage()
            if msg.startswith("Text overflows box"):
                self.statusBar().clearMessage()

    # --- inspector -> view (style writes) --------------------------------
    def _on_style_edited(self, overrides: dict) -> None:
        """A single inspector control changed: apply it to the current selection
        LIVE through the view, which funnels to a BoxCommand (BUILD_SPEC §4.2).
        One control change = one undo step."""
        if self.document is None or not overrides:
            return
        # Re-entrancy guard: re-opening the editor below pulls focus off the
        # font combo, whose focus-out re-emits the (unchanged) family -- ignore
        # that echo so a single change is a single apply.
        if getattr(self, "_restyling_editor_box", False):
            return
        # Bold / Italic / Underline / Strikethrough with a text SELECTION inside
        # the open inline editor style JUST the selection (per-selection rich
        # runs, staged on commit) -- not the whole box.
        if set(overrides) <= {"bold", "italic", "underline", "strike"}:
            sel_fn = getattr(self.view, "apply_style_to_selection", None)
            if callable(sel_fn):
                try:
                    if sel_fn(overrides):
                        return
                except (TypeError, RuntimeError):
                    pass
            # Underline / strikethrough are EDITOR-selection styles only (the
            # glyph model carries no underline/strike on a whole non-edited box),
            # so drop them before the whole-box route -- never pass them to
            # set_style (which would reject the kwarg). Bold/italic still flow on.
            overrides = {k: v for k, v in overrides.items()
                         if k not in ("underline", "strike")}
            if not overrides:
                return
        # Whole-box styles (font family / size / colour) while the inline editor
        # is OPEN: apply them to the edited box and re-enter editing with the
        # highlight restored, instead of dropping the edit. The model holds these
        # per box, so they style the whole box (== a full-text highlight).
        editor_fn = getattr(self.view, "apply_style_to_editor_box", None)
        if callable(editor_fn):
            self._restyling_editor_box = True
            try:
                handled = editor_fn(overrides)
            except (TypeError, RuntimeError):
                handled = False
            finally:
                self._restyling_editor_box = False
            if handled:
                self._sync_status()
                return
        fn = getattr(self.view, "apply_style", None)
        if callable(fn):
            try:
                fn(overrides)
                return
            except (TypeError, RuntimeError):
                pass
        # Fallback for a canvas without apply_style: drive the model directly
        # via a BoxCommand against the inspector's current target so the inspector
        # still works standalone (used in headless construction checks).
        box = getattr(self.inspector, "_target", None)
        if box is not None:
            page = self.view_page_index()
            self.undo_stack.push(
                BoxCommand(self.document, self.view, page, box, "style",
                           {"overrides": overrides}))
            self._refresh_inspector_for(box)
            self._sync_status()

    # --- toolbar tool handlers -------------------------------------------
    def _on_add_text_toggled(self, checked: bool) -> None:
        """Arm / disarm the canvas ADD_TEXT mode (BUILD_SPEC §3.2). Keep the
        toolbar button's checked state in sync with the action."""
        if hasattr(self, "add_text_button"):
            self.add_text_button.setChecked(checked)
        if self.document is None:
            if checked:
                self.act_add_text.setChecked(False)
            return
        fn_name = "enter_add_text_mode" if checked else "exit_add_text_mode"
        fn = getattr(self.view, fn_name, None)
        if callable(fn):
            try:
                fn()
            except (TypeError, RuntimeError):
                pass

    def _delete_selected(self) -> None:
        """Toolbar / shortcut Delete: remove the current selection via the view
        (one BoxCommand). No-op when nothing is selected or an editor is open;
        the view guards both."""
        if self.document is None:
            return
        fn = getattr(self.view, "delete_selected", None)
        if callable(fn):
            try:
                fn()
            except (TypeError, RuntimeError):
                pass
        self._sync_status()
        self._sync_actions()

    # --- selection helpers -----------------------------------------------
    def _begin_add_box_edit(self, box) -> None:
        """Hand a freshly added NewBox back to the canvas so it selects it and
        drops into text edit (BUILD_SPEC §3.5 step 4). Prefers the view's
        ``begin_add_box_edit`` (which also arms the empty-add cleanup); falls
        back to ``select_box`` on a canvas without it."""
        for name in ("begin_add_box_edit", "select_box"):
            fn = getattr(self.view, name, None)
            if callable(fn):
                try:
                    fn(box)
                    return
                except (TypeError, RuntimeError):
                    pass

    def _refresh_inspector_for(self, box) -> None:
        """Re-read a box's effective style into the inspector (after a live
        apply / geometry change) so its controls show the committed truth."""
        if self.document is None or box is None:
            return
        current = getattr(self.inspector, "_target", None)
        if current is None or getattr(current, "identity", None) != \
                getattr(box, "identity", object()):
            return
        page = self.view_page_index()
        try:
            style = self.document.effective_style(page, box)
        except Exception:  # noqa: BLE001
            return
        self.inspector.set_target(box, style)

    def _current_selection(self):
        """The canvas's current selection, or None (across API variants)."""
        fn = getattr(self.view, "current_selection", None)
        if callable(fn):
            try:
                return fn()
            except (TypeError, RuntimeError):
                return None
        return None

    def _sync_selection_status(self, style: dict | None) -> None:
        """Show the selected box's family + size in the status font chip, or
        hide it when nothing is selected (BUILD_SPEC §5.4)."""
        if style is None:
            # Only hide if we are not mid text-edit (editStarted owns the chip
            # during typing).
            self.font_chip.hide()
            return
        family = style.get("font_family", "")
        size = style.get("size", 0.0)
        self.font_chip.setText(
            f"<span style='color:{theme.TEXT_SECONDARY};'>"
            f"{family} · {size:g} pt</span>"
        )
        self.font_chip.show()

    def _show_font_chip(self, family: str, tier: int) -> None:
        dot = theme.color_fidelity(tier)
        self.font_chip.setText(f"●  {family}")
        self.font_chip.setStyleSheet(
            f"color: {theme.TEXT_SECONDARY};"
        )
        # Colorize only the leading dot via rich text.
        self.font_chip.setText(
            f"<span style='color:{dot.name()};'>●</span>"
            f"<span style='color:{theme.TEXT_SECONDARY};'>&nbsp;&nbsp;{family}</span>"
        )
        self.font_chip.show()

    # ===================================================================
    # Undo / dirty handlers (BUILD_SPEC §6.4)
    # ===================================================================
    def _on_can_undo_changed(self, can: bool) -> None:
        self.act_undo.setEnabled(can and self.document is not None)

    def _on_can_redo_changed(self, can: bool) -> None:
        self.act_redo.setEnabled(can and self.document is not None)

    def _on_clean_changed(self, clean: bool) -> None:
        dirty = not clean
        self.setWindowModified(dirty)
        self._refresh_dirty_dot()
        self._sync_save_enabled()
        self._sync_status()
        # Flip the OPEN tab's status circle to indigo (edited) / back to green
        # (saved) right away, without waiting for a tab switch to re-sync.
        wsp = getattr(self, "workspace", None)
        if wsp is not None and hasattr(self, "tab_bar"):
            self.tab_bar.set_tab_dirty(wsp.active_index, dirty)

    def _refresh_dirty_dot(self) -> None:
        """The top-bar status dot: GREEN when the document is saved/clean, the
        clay accent when there are unsaved changes. Hidden with no document."""
        # The Save split CTA's clean/dirty LOOK rides the SAME unsaved-state
        # signal as the dirty dot, and MUST run even with no document open --
        # closing the last tab has to reset the CTA to its quiet clean state
        # (_refresh_save_state already maps document-None -> clean).
        self._refresh_save_state()
        if self.document is None:
            self.dirty_dot.hide()
            return
        color = theme.ACCENT if self._has_unsaved() else theme.TAB_DOT_ACTIVE
        self.dirty_dot.setStyleSheet(f"background: {color}; border-radius: 4px;")
        self.dirty_dot.show()

    def _refresh_save_state(self) -> None:
        """Drive the Save split's clean/dirty appearance from the document's
        UNSAVED state (NOT merely enabled/disabled). DIRTY = filled clay + a
        subtle warm lift + white glyphs; CLEAN = a quiet neutral pill, no
        shadow, with TEXT_SECONDARY glyphs. Qt needs an unpolish/polish after a
        dynamic-property change for the [dirty=...] QSS variant to re-apply."""
        if not hasattr(self, "save_split"):
            return
        dirty = self.document is not None and self._has_unsaved()
        for w in (self.save_split, self.save_button, self.save_caret):
            w.setProperty("dirty", dirty)
            w.style().unpolish(w)
            w.style().polish(w)
        # Swap the BAKED icons (a QSS color: rule cannot recolour a QIcon).
        self.save_button.setIcon(
            self._save_icon_white if dirty else self._save_icon_quiet)
        self.save_caret.setIcon(
            self._caret_icon_white if dirty else self._caret_icon_quiet)
        # Warm lift only in the dirty state (never the old indigo glow). Toggle
        # the persistently-installed effect rather than swapping it out (which
        # would have Qt delete it).
        self._save_shadow.setEnabled(dirty)
        # The caret stays usable whenever a document is open (Save As…).
        self.save_caret.setEnabled(self.document is not None)

    def _refresh_undo_tooltips(self, *_args) -> None:
        ut = self.undo_stack.undoText()
        rt = self.undo_stack.redoText()
        self.act_undo.setToolTip(f"Undo {ut}" if ut else "Undo")
        self.act_redo.setToolTip(f"Redo {rt}" if rt else "Redo")

    # ===================================================================
    # File operations (BUILD_SPEC §6.5)
    # ===================================================================
    def open_pdf(self) -> None:
        # Multi-tab: opening a new file no longer discards the current one (it
        # opens into a new tab), so no discard guard here (PAGES_SPEC §6.1). The
        # dialog accepts MULTIPLE files so File > Open matches drag-drop: each
        # selected PDF opens into its own tab via the same de-duping open_path.
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Open PDF", self._dialog_dir(), "PDF files (*.pdf)"
        )
        for path in paths:
            self.open_path(path)

    # How many passwords the user may try per open (ws6 §2.7): three prompts,
    # each immediately re-attempted, then the open aborts with a flash.
    _PASSWORD_ATTEMPTS = 3

    def open_path(self, path: str) -> None:
        """Open ``path`` into a NEW tab and show it (PAGES_SPEC §6.1). De-dups on
        realpath via the Workspace, so re-opening an already-open file just
        switches to its tab. Unlike the single-doc build, opening does NOT close
        the current document.

        Encrypted files no longer crash the open (ws6 §2.7): the first try
        runs with no password; each ``PasswordRequired`` asks the injectable
        ``self._password_provider`` (max 3 prompts, each answer re-tried), and
        a ``None`` answer (Cancel) aborts silently."""
        password = None
        idx = None
        for attempt in range(self._PASSWORD_ATTEMPTS + 1):
            try:
                idx = self.workspace.open(path, password)
                break
            except PasswordRequired:
                if attempt == self._PASSWORD_ATTEMPTS:
                    self._flash_error(
                        f"Could not unlock {os.path.basename(path)}: "
                        f"too many incorrect passwords.")
                    return
                password = self._password_provider(path, attempt + 1)
                if password is None:
                    return     # user cancelled: silent abort
            except Exception as exc:  # noqa: BLE001 - surface any load failure
                QMessageBox.critical(self, "Open failed",
                                     f"Could not open PDF:\n{exc}")
                return
        self._add_recent(path)
        _mark_file_recently_used(path)   # show in Finder Recents + Dock recents
        self._hide_empty_state()
        self._activate_document(idx)
        self._toast(
            f"Opened {os.path.basename(path)}", 2500
        )
        # A form-bearing doc gets the fill hint instead (forms §4; the later
        # toast owns the filename slot). Once per open; no-form docs see
        # zero new chrome.
        if self.document is not None and getattr(
                self.document, "has_form", False):
            n = self.document.form_field_count
            self._toast(
                f"{n} form field{'s' if n != 1 else ''} detected. "
                "Click a field to fill it.", 4000
            )
        elif (self.document is not None
              and not getattr(self, "_ocr_running", False)
              and self.document.image_only_pages()):
            # Scanned/image-only document: OCR it automatically so the text is
            # editable without a menu trip. Deferred so the window paints first;
            # cancellable via the OCR progress dialog.
            self._toast("Scanned document — making the text editable…", 4000)
            QTimer.singleShot(0, lambda: self._do_ocr(scope="document"))

    def _ask_password(self, path: str, attempt: int) -> "str | None":
        """The DEFAULT ``_password_provider``: a modal password prompt.
        Returns the entered password, or ``None`` on Cancel."""
        name = os.path.basename(path)
        label = f"“{name}” is password protected.\nEnter the password:"
        if attempt > 1:
            label = (f"That password was not correct (attempt {attempt} of "
                     f"{self._PASSWORD_ATTEMPTS}).\n{label}")
        text, ok = QInputDialog.getText(
            self, "Password required", label, QLineEdit.Password)
        return text if ok else None

    # --- settings, recents, session (persisted via QSettings) ------------
    _RECENT_KEY = "recentFiles"
    _RECENT_MAX = 8           # Open Recent menu length
    _PINNED_KEY = "pinnedFiles"
    _GALLERY_MAX = 24         # start-screen gallery shows more than the menu
    _SESSION_KEY = "session/state"

    def _settings(self):
        """The ONE QSettings handle (navigation M3): recents + session state
        both live here. Tests monkeypatch THIS (an instance attribute
        returning a temp ``QSettings(path, IniFormat)``) so suites never read
        or write the real preferences."""
        from PySide6.QtCore import QSettings
        return QSettings("eddaboss", "PDF Text Editor")

    def _recent_store(self):
        return self._settings()

    def _recent_skip_prefixes(self) -> list:
        """Path prefixes HIDDEN from the Recent list: the app's own repo
        (fixtures) and transient temp PDFs. A seam beside ``_settings`` so
        tests can relax the temp-dir exclusion and exercise the list with
        synthetic files."""
        import tempfile
        repo = os.path.abspath(
            os.path.join(os.path.dirname(__file__), "..", ".."))
        return [repo] + [os.path.realpath(d) for d in
                         (tempfile.gettempdir(), "/tmp", "/var/folders")]

    def _recent_paths(self, limit: int | None = None) -> list:
        """Recent PDFs for the start screen + Open Recent menu: files opened
        in THIS app (the QSettings store), most recent first. App-only BY
        DESIGN (navigation M3): the old Spotlight merge ran a synchronous
        ``mdfind`` subprocess on the UI thread and leaked system-wide
        document history into the app -- deleted, not optional. Still
        filtered to files that exist, excluding the app's own fixtures and
        transient temp PDFs. ``limit`` defaults to the Open Recent menu length;
        the start-screen gallery passes a larger cap."""
        cap = self._RECENT_MAX if limit is None else limit
        vals = self._recent_store().value(self._RECENT_KEY, []) or []
        if isinstance(vals, str):
            vals = [vals]
        skip_prefixes = self._recent_skip_prefixes()
        seen: set = set()
        out: list = []
        for p in vals:
            ap = os.path.abspath(p)
            rp = os.path.realpath(ap)
            if ap in seen or not os.path.isfile(ap):
                continue
            if any(rp.startswith(pre) for pre in skip_prefixes):
                continue
            seen.add(ap)
            out.append(ap)
            if len(out) >= cap:
                break
        return out

    def _pinned_paths(self) -> list:
        """Pinned start-screen files (their own QSettings key), in pin order,
        filtered to files that still exist. Pinned files survive aging out of
        the recent list and always lead the gallery."""
        vals = self._recent_store().value(self._PINNED_KEY, []) or []
        if isinstance(vals, str):
            vals = [vals]
        seen: set = set()
        out: list = []
        for p in vals:
            ap = os.path.abspath(p)
            if ap in seen or not os.path.isfile(ap):
                continue
            seen.add(ap)
            out.append(ap)
        return out

    def _set_pinned(self, paths: list) -> None:
        self._recent_store().setValue(
            self._PINNED_KEY, [os.path.abspath(p) for p in paths])

    def _gallery_paths(self) -> list:
        """The ordered path list the start-screen gallery renders: pinned files
        first (in pin order), then the most-recent files not already pinned,
        capped at ``_GALLERY_MAX``."""
        pinned = self._pinned_paths()
        pinned_set = set(pinned)
        rest = [p for p in self._recent_paths(self._GALLERY_MAX)
                if p not in pinned_set]
        return (pinned + rest)[: self._GALLERY_MAX]

    def _refresh_empty_recents(self) -> None:
        """Repaint the start-screen gallery from the current recents + pins."""
        if hasattr(self, "empty_state"):
            self.empty_state.set_recents(
                self._gallery_paths(), set(self._pinned_paths()))

    def _remove_recent(self, path: str) -> None:
        """Drop ``path`` from the recent list (and any pin). The file on disk is
        untouched; only the app's memory of it clears. The gallery repaints."""
        ap = os.path.abspath(path)
        vals = self._recent_store().value(self._RECENT_KEY, []) or []
        if isinstance(vals, str):
            vals = [vals]
        self._recent_store().setValue(
            self._RECENT_KEY, [p for p in vals if os.path.abspath(p) != ap])
        self._set_pinned([p for p in self._pinned_paths()
                          if os.path.abspath(p) != ap])
        self._refresh_empty_recents()

    def _toggle_pin(self, path: str) -> None:
        """Pin/unpin ``path`` to the front of the start-screen gallery."""
        ap = os.path.abspath(path)
        pinned = [os.path.abspath(p) for p in self._pinned_paths()]
        if ap in pinned:
            pinned.remove(ap)
        else:
            pinned.insert(0, ap)
        self._set_pinned(pinned)
        self._refresh_empty_recents()

    def _add_recent(self, path: str) -> None:
        path = os.path.abspath(path)
        vals = self._recent_store().value(self._RECENT_KEY, []) or []
        if isinstance(vals, str):
            vals = [vals]
        vals = [p for p in vals if p != path]
        vals.insert(0, path)
        self._recent_store().setValue(self._RECENT_KEY, vals[: self._RECENT_MAX])
        # Ease of use: remember the folder so the NEXT Open dialog starts here
        # instead of re-navigating from home every time. Updated on every open
        # (dialog, drag-drop, Open Recent, Finder), so it always tracks the user's
        # working folder. Test-safe via the monkeypatched _recent_store().
        folder = os.path.dirname(path)
        if os.path.isdir(folder):
            self._recent_store().setValue("lastDir", folder)

    def _dialog_dir(self) -> str:
        """The folder the Open dialog opens in: the last folder a PDF was opened
        from (``_add_recent``), or home when there is none. Test-safe."""
        try:
            d = self._recent_store().value("lastDir")
            if isinstance(d, str) and os.path.isdir(d):
                return d
        except Exception:  # noqa: BLE001 - a missing pref must never block open
            pass
        return os.path.expanduser("~")

    # --- session restore (navigation M3) ----------------------------------
    def _save_session(self) -> None:
        """Snapshot the open tabs into the settings store: file paths in tab
        order, the active index, the active doc's page, and the zoom. Runs on
        the GUARDED close path only (after the dirty guard passes, before
        ``close_all`` empties the workspace); the ``_suppress_close_guard``
        teardown seam returns before it, so test windows never write the real
        preferences. A failed write must never block quitting."""
        try:
            files = [d.path for d in self.workspace.documents()
                     if d.path and os.path.isfile(d.path)]
            state = {
                "files": files,
                "active": self.workspace.active_index,
                "pages": ({self.document.path: self.view_page_index()}
                          if self.document is not None else {}),
                "zoom": self.view_zoom(),
                # Persist the zoom MODE so a fit-page session reopens as
                # fit-page (adapts to the window) instead of locking to the
                # last fit-computed number as a fixed zoom.
                "zoom_mode": getattr(self.view, "_zoom_mode", "fit_page"),
            }
            self._settings().setValue(self._SESSION_KEY, json.dumps(state))
            # Ease of use: reopen at the same size + position next launch.
            self._settings().setValue("windowGeometry", self.saveGeometry())
        except Exception:  # noqa: BLE001 - never block the close on a write
            pass

    def _restore_window_geometry(self) -> None:
        """Restore the window's last size + position (ease of use). Called from
        ``main()`` ALWAYS (even for a CLI/Finder open, which skips session
        restore), so it is never read during test construction. A bad/absent
        value leaves the default ``resize`` from ``__init__`` in place. The
        restored frame is then clamped onto a currently-visible screen so a
        window saved on a now-disconnected monitor never reopens out of reach."""
        try:
            geo = self._settings().value("windowGeometry")
            if geo is not None:
                self.restoreGeometry(geo)
                self._clamp_to_visible_screen()
        except Exception:  # noqa: BLE001 - a bad geometry must never block launch
            pass

    def _clamp_to_visible_screen(self) -> None:
        """If the restored frame is mostly off every connected screen (an
        external display that is gone, or a move between machines with different
        resolutions), recenter it on the primary screen. A window that is
        substantially visible is left exactly where the user had it."""
        try:
            frame = self.frameGeometry()
            frame_area = frame.width() * frame.height()
            if frame_area <= 0:
                return
            best = 0
            for screen in QGuiApplication.screens():
                inter = frame.intersected(screen.availableGeometry())
                best = max(best, inter.width() * inter.height())
            if best >= 0.30 * frame_area:
                return                       # enough of it is reachable
            avail = QGuiApplication.primaryScreen().availableGeometry()
            self.resize(min(self.width(), avail.width()),
                        min(self.height(), avail.height()))
            fg = self.frameGeometry()
            fg.moveCenter(avail.center())
            self.move(fg.topLeft())
        except Exception:  # noqa: BLE001 - never block launch on a clamp failure
            pass

    def _enable_persistence(self) -> None:
        """Turn on crash-survival flushing for the REAL app (called from
        ``main()``). Headless test windows never call this, so they keep writing
        the real preferences only on the suppressed clean-close path -- which the
        teardown seam skips. One last flush fires on ``aboutToQuit``."""
        self._persist_enabled = True
        self._autosave_timer.start()
        app = QGuiApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(self._flush_persisted_state)

    def _autosave_tick(self) -> None:
        """Write a fresh recovery copy of every dirty, file-backed document that
        changed since its last copy. Cheap when nothing is dirty; skips a doc
        whose edit/structural signature is unchanged so an idle dirty doc is not
        re-serialized every tick."""
        if not self._persist_enabled:
            return
        from . import autosave
        for doc in self.workspace.documents():
            try:
                if not doc.dirty or not doc.path or not os.path.isfile(doc.path):
                    continue
                sig = (doc.edit_count, doc.structural_depth,
                       getattr(doc, "_security_dirty", False))
                if self._autosave_sigs.get(id(doc)) == sig:
                    continue
                autosave.write_recovery(doc, doc.path, time.time())
                self._autosave_sigs[id(doc)] = sig
            except Exception:  # noqa: BLE001 - autosave must never disrupt editing
                pass

    def _clear_recovery_for(self, doc) -> None:
        """Drop a document's recovery copy once it is saved or discarded (its
        on-disk state now matches, so there is nothing to recover)."""
        if not self._persist_enabled or doc is None:
            return
        try:
            from . import autosave
            if doc.path:
                autosave.clear_recovery(doc.path)
        except Exception:  # noqa: BLE001
            pass
        self._autosave_sigs.pop(id(doc), None)

    def _offer_recovery(self) -> bool:
        """Offer to reopen unsaved work left by a crash (recovery copies survive
        only an unclean exit). Returns True if any document was recovered, so
        ``main()`` skips the ordinary session restore -- the recovered documents
        are the session that mattered. A reopened doc is marked unsaved so a
        plain Save writes the recovered content back over the original."""
        if not self._persist_enabled:
            return False
        from . import autosave
        entries = [e for e in autosave.scan_recoveries() if e["source_exists"]]
        if not entries:
            autosave.clear_all()      # tidy orphans whose source vanished
            return False
        names = [e["name"] for e in entries]
        listed = "\n".join(f"  •  {n}" for n in names)
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Information)
        box.setWindowTitle("Recover unsaved changes")
        box.setText("PDF Text Editor closed unexpectedly with unsaved changes "
                    f"in {len(names)} document{'s' if len(names) != 1 else ''}.")
        box.setInformativeText(
            f"{listed}\n\nReopen them with the unsaved changes? Saving then "
            "writes the recovered version back over the original.")
        recover_btn = box.addButton("Recover", QMessageBox.AcceptRole)
        box.addButton("Discard", QMessageBox.DestructiveRole)
        box.setDefaultButton(recover_btn)
        box.exec()
        if box.clickedButton() is not recover_btn:
            autosave.clear_all()
            return False
        opened = 0
        for e in entries:
            try:
                doc = PDFDocument(e["recovery_pdf"])
                doc.set_path(e["source"])
                # The recovered edits are baked into the copy's bytes; flag the
                # doc unsaved so the dirty dot shows and Save writes it back.
                doc._dirty = True
                self.workspace.add_document(doc)
                opened += 1
            except Exception:  # noqa: BLE001 - skip an unreadable recovery copy
                pass
        if opened:
            self._hide_empty_state()
            self._activate_document(self.workspace.active_index)
            self._toast(
                f"Recovered {opened} document{'s' if opened != 1 else ''} "
                "with unsaved changes", 5000)
        return opened > 0

    def _kick_persist(self) -> None:
        """Debounce a geometry/session flush after a move, resize, or tab
        change. No-op until ``_enable_persistence`` (so tests never write)."""
        if self._persist_enabled:
            self._geo_timer.start()

    def _flush_persisted_state(self) -> None:
        """Persist geometry + the open-tab session now, so a later crash/kill
        reopens at the last layout. Reuses ``_save_session`` (geometry + file
        list + active page/zoom); guarded so it never raises into the event
        loop."""
        if not self._persist_enabled:
            return
        try:
            self._save_session()
        except Exception:  # noqa: BLE001 - a flush must never disrupt the UI
            pass

    def moveEvent(self, event) -> None:
        super().moveEvent(event)
        self._kick_persist()

    def _restore_session(self) -> None:
        """Reopen the previous session's tabs (the ``_save_session``
        snapshot): files, active tab, the active doc's page, the zoom. Only
        an EMPTY workspace restores (CLI / Finder opens win, ``main()``);
        missing files are skipped; every failure is swallowed -- a broken
        session must never block launch. Unsaved edits are NOT restored (the
        model stages edits in RAM by design; session restore reopens files
        only)."""
        if not self.workspace.is_empty:
            return
        try:
            raw = self._settings().value(self._SESSION_KEY)
            if not raw:
                return
            state = json.loads(raw)
            files = [str(p) for p in state.get("files", [])]
            for path in files:
                if os.path.isfile(path):
                    self.open_path(path)
            if self.workspace.is_empty:
                return
            # Re-find the saved active doc by path: a missing file shifts
            # the indices, so the saved index alone cannot be trusted.
            active = state.get("active", -1)
            if isinstance(active, int) and 0 <= active < len(files):
                active_path = os.path.realpath(files[active])
                for i in range(self.workspace.count):
                    if os.path.realpath(
                            self.workspace.document(i).path) == active_path:
                        self._on_tab_activated(i)
                        break
            # Restore the zoom: only LOCK a zoom the user explicitly fixed;
            # a fit-page/fit-width session reopens in that mode so it adapts to
            # the window, and a legacy session (no mode saved -- its "zoom" was
            # a fit-computed value) defaults to fit-page rather than locking.
            mode = state.get("zoom_mode")
            zoom = state.get("zoom")
            if mode == "fixed" and isinstance(zoom, (int, float)) and zoom > 0:
                self.set_zoom(float(zoom))
            elif mode == "fit_width":
                self._set_zoom_mode("fit_width")
            else:
                self._set_zoom_mode("fit_page")
            pages = state.get("pages") or {}
            page = (pages.get(self.document.path)
                    if self.document is not None else None)
            if isinstance(page, int) and page >= 0:
                self._set_view_page(page)
                self._sync_page_indicator()
        except Exception:  # noqa: BLE001 - never block launch on a bad session
            pass

    def _activate_document(self, idx: int) -> None:
        """Make the document at ``idx`` the active one and re-point the canvas,
        sidebar, and inspector at it (PAGES_SPEC §5.4). Each tab carries its own
        edits + structural undo (they live on the PDFDocument); switching is just
        re-pointing the chrome and rebuilding the Qt undo view. The fine-grained
        Qt history does NOT survive a switch (documented limitation §6.7); the
        model's own history + structural undo persist."""
        self.workspace.switch(idx)
        document = self.workspace.active
        # A note popup must not survive a document swap: its ref belongs to
        # the OLD doc's annot maps (cancel -- never commit across docs).
        self.close_note_editor()
        self.undo_stack.clear()
        self.undo_stack.setClean()
        if document is None:
            self.view.clear_document()
            self.sidebar.set_document(None)
            self.tab_bar.sync(self.workspace)
            self._show_empty_state()
            self._sync_all()
            self._kick_persist()
            return
        self.view.set_document(document)
        self.sidebar.set_document(document)
        self.sidebar.set_current_page(self.view_page_index())
        # Bind the inspector's fidelity hint to THIS document's font engine and
        # clear any stale selection from the previously active file.
        self.inspector.set_font_engine(document.font_engine)
        self.inspector.set_target(None, None)
        # Mirror the Qt undo stack onto THIS doc's structural depth so a tab that
        # was reorganized shows one Undo step per structural op (not just one),
        # with the clean marker at its saved baseline (§6.7).
        self._sync_structural_undo_stack()
        self.tab_bar.sync(self.workspace)
        self._sync_all()
        self._kick_persist()

    def save_pdf(self) -> None:
        """Save in place when a working path exists and the doc is dirty;
        otherwise behave as Save As."""
        if self.document is None:
            return
        # Commit any open inline editor FIRST (the same contract as tab
        # switch, structural ops, export and print): Cmd+S must save the
        # half-typed text the screen shows, and the flush may be exactly what
        # creates the unsaved state, so it runs before the _has_unsaved gate.
        self._flush_open_editor()
        # If never saved to a user-chosen path (still the source) we still
        # write in place over the working path, which IS the spec's Save: the
        # atomic temp+replace lives in save_as. But a brand-new doc with no
        # prior save uses Save As to pick a destination.
        if not self._has_unsaved():
            return
        self._do_save(self.document.path)

    def save_pdf_as(self) -> None:
        if self.document is None:
            return
        # Same open-editor flush contract as save_pdf.
        self._flush_open_editor()
        base, ext = os.path.splitext(os.path.basename(self.document.path))
        suggested = f"{base}-edited{ext or '.pdf'}"
        start_dir = os.path.dirname(self.document.path)
        path, _ = QFileDialog.getSaveFileName(
            self, "Save edited PDF",
            os.path.join(start_dir, suggested), "PDF files (*.pdf)"
        )
        if not path:
            return
        if not path.lower().endswith(".pdf"):
            path += ".pdf"
        if self._do_save(path):
            # Repoint working path and reload so spans/xrefs match the output.
            self.document.set_path(path)
            self._reload_after_save(path)

    @contextmanager
    def _busy(self):
        """Show the wait cursor for the duration of a synchronous heavy
        operation (save, optimize, combine, export, print) so the window reads as
        working instead of frozen. Restored in a finally so an exception never
        leaves the cursor stuck."""
        QGuiApplication.setOverrideCursor(QCursor(Qt.CursorShape.WaitCursor))
        try:
            yield
        finally:
            QGuiApplication.restoreOverrideCursor()

    def _do_save(self, out_path: str) -> bool:
        try:
            with self._busy():
                self.document.save_as(out_path)
        except Exception as exc:  # noqa: BLE001 - surface any write failure
            self._flash_error(f"Save failed: {exc}")
            QMessageBox.critical(self, "Save failed",
                                 f"Could not save PDF:\n{exc}")
            return False
        self.document.mark_clean()
        self.undo_stack.setClean()
        # Saved: the on-disk file now matches, so drop any crash-recovery copy.
        self._clear_recovery_for(self.document)
        # The edited file was just written: register it with macOS so it lands in
        # Finder's Recents + the Dock's Recent Documents like any saved document.
        _mark_file_recently_used(out_path)
        count = self.document.edit_count
        self._toast(
            f"Saved {count} edit{'s' if count != 1 else ''} to "
            f"{os.path.basename(out_path)}", 4000
        )
        self._sync_status()
        return True

    def _reload_after_save(self, path: str) -> None:
        """Reopen the saved file so the model's spans/xrefs reflect what is now
        on disk (Save As repoints the working path). Swaps the active document IN
        PLACE within the workspace so the tab keeps its slot.

        MUST pass the outgoing doc's ``reopen_password`` (ws6 §2.7): the file
        we just wrote may be encrypted (pending user password, else the open
        password), and without it the fresh constructor raises
        ``PasswordRequired`` -- the reload silently failed and the tab kept a
        stale pre-save model after every encrypted Save As."""
        old = self.workspace.active
        password = getattr(old, "reopen_password", None)
        try:
            document = PDFDocument(path, password)
        except Exception:  # noqa: BLE001 - keep the old doc on reload failure
            return
        page = self.view_page_index()
        idx = self.workspace.active_index
        if idx >= 0:
            self.workspace._docs[idx] = document   # in-place swap, keep the slot
        if old is not None:
            old.close()
        self.undo_stack.clear()
        self.undo_stack.setClean()
        self.view.set_document(document)
        self.sidebar.set_document(document)
        self.inspector.set_font_engine(document.font_engine)
        self.inspector.set_target(None, None)
        self._set_view_page(min(page, document.page_count - 1))
        self.sidebar.set_current_page(self.view_page_index())
        self.tab_bar.sync(self.workspace)
        self.setWindowTitle(self._title())
        self._sync_all()

    # ===================================================================
    # Tab handlers (PAGES_SPEC §6.3)
    # ===================================================================
    def _on_tab_activated(self, idx: int) -> None:
        if idx == self.workspace.active_index:
            return
        self._flush_open_editor()
        self._activate_document(idx)

    def _on_tab_close_requested(self, idx: int) -> None:
        """Dirty-guard the doc at ``idx`` (temporarily activating it for the
        dialog), then close it and re-activate the new active index (PAGES_SPEC
        §6.8)."""
        if not 0 <= idx < self.workspace.count:
            return
        if self.workspace.document(idx).dirty:
            prev = self.workspace.active_index
            self.workspace.switch(idx)
            self.tab_bar.sync(self.workspace)
            if not self._confirm_discard_if_dirty():
                # Cancelled: restore the previously active tab.
                self.workspace.switch(prev)
                self.tab_bar.sync(self.workspace)
                return
        new_idx = self.workspace.close(idx)
        if new_idx < 0:
            self._activate_document(-1)
        else:
            self._activate_document(new_idx)

    def _on_tab_moved(self, frm: int, to: int) -> None:
        self.workspace.move_tab(frm, to)
        # The QTabBar already reordered its own tabs; keep the workspace's active
        # index pointing at the same doc (move_tab handles that) and re-sync.
        self.tab_bar.sync(self.workspace)

    def next_tab(self) -> None:
        """Window > Next Tab: cycle to the next open document (wraps). A
        no-op with fewer than two docs open."""
        n = self.workspace.count
        if n > 1:
            self._on_tab_activated((self.workspace.active_index + 1) % n)

    def prev_tab(self) -> None:
        """Window > Previous Tab: cycle to the previous open document (wraps)."""
        n = self.workspace.count
        if n > 1:
            self._on_tab_activated((self.workspace.active_index - 1) % n)

    def _toggle_window_zoom(self) -> None:
        """Window > Zoom (the macOS convention): maximize <-> restore."""
        if self.isMaximized():
            self.showNormal()
        else:
            self.showMaximized()

    def close_active_tab(self) -> None:
        """Cmd+W: close the active tab (the last tab closing empties the window
        back to the empty state). With no document open, do nothing."""
        idx = self.workspace.active_index
        if idx < 0:
            return
        self._on_tab_close_requested(idx)

    # ===================================================================
    # Sidebar handlers (route to the SAME structural ops, PAGES_SPEC §6.4)
    # ===================================================================
    def _on_thumb_activated(self, page_index: int) -> None:
        if self.document is None:
            return
        self._set_view_page(page_index)
        self._sync_page_indicator()

    def _on_reorder(self, src: int, dst: int) -> None:
        self._run_structural(lambda d: d.move_page(src, dst))

    def _on_rotate_page(self, page_index: int, deg: int) -> None:
        self._run_structural(lambda d: d.rotate_page(page_index, deg),
                             new_page=page_index)

    def _on_delete_page(self, page_index: int) -> None:
        if self.document is not None and self.document.page_count <= 1:
            self._flash_error("Cannot delete the only page.")
            return
        new_page = max(0, page_index - 1)
        self._run_structural(lambda d: d.delete_page(page_index),
                             new_page=new_page)

    def _on_duplicate_page(self, page_index: int) -> None:
        self._run_structural(lambda d: d.duplicate_page(page_index),
                             new_page=page_index + 1)

    def _on_insert_blank(self, at: int) -> None:
        self._run_structural(lambda d: d.insert_blank_page(at), new_page=at)

    # ===================================================================
    # Current-page wrappers (toolbar / menu / shortcut)
    # ===================================================================
    def rotate_current_cw(self) -> None:
        if self.document is not None:
            self._on_rotate_page(self.view_page_index(), 90)

    def rotate_current_ccw(self) -> None:
        if self.document is not None:
            self._on_rotate_page(self.view_page_index(), -90)

    def delete_current_page(self) -> None:
        if self.document is not None:
            self._on_delete_page(self.view_page_index())

    def duplicate_current_page(self) -> None:
        if self.document is not None:
            self._on_duplicate_page(self.view_page_index())

    def insert_blank_after_current(self) -> None:
        if self.document is not None:
            self._on_insert_blank(self.view_page_index() + 1)

    # ===================================================================
    # The structural-op funnel (PAGES_SPEC §6.5 / §6.6)
    # ===================================================================
    def _run_structural(self, op, *, new_page: int | None = None) -> None:
        """Run a MUTATING structural op on the active doc, then refresh. Commits
        any open inline editor first (so its text bakes), runs ``op(document)``
        (which snapshots + bakes + mutates), then funnels ONE StructuralCommand
        onto the Qt stack and refreshes canvas/sidebar/tabs/status."""
        doc = self.document
        if doc is None:
            return
        self._flush_open_editor()
        try:
            op(doc)
        except Exception as exc:  # noqa: BLE001 - surface a structural failure
            self._flash_error(str(exc))
            return
        self._after_structural_op(new_page=new_page)

    def _after_structural_op(self, *, new_page: int | None = None) -> None:
        """Single post-op refresh (PAGES_SPEC §6.5): reload the canvas, clamp the
        page, rebuild the sidebar + tab title + status, and push the structural
        boundary onto the Qt undo stack."""
        doc = self.document
        if doc is None:
            return
        target = self.view_page_index() if new_page is None else new_page
        target = max(0, min(target, doc.page_count - 1))
        # set_page won't reload if the index is unchanged + boxes exist; force a
        # reload so a rotate/duplicate at the SAME index still re-renders.
        self.view.set_document(doc) if self.view.document is not doc \
            else self.view.reload()
        self._set_view_page(target)
        self.sidebar.refresh()
        self.sidebar.set_current_page(self.view_page_index())
        self.tab_bar.sync(self.workspace)
        self._push_structural_boundary()
        self._sync_all()

    def _push_structural_boundary(self) -> None:
        """Mirror the Qt undo stack onto the model's structural depth (PAGES_SPEC
        §6.6). The model committed one snapshot per structural op; this rebuilds
        the Qt stack to hold exactly that many StructuralCommands so EVERY
        structural op is reachable by Undo 1:1 (not just the most recent), and a
        depth-cap eviction drops the matching Qt command too. The previous
        fine-grained text/box commands are baked into the op's snapshot and can no
        longer be undone in isolation, so they do not survive the boundary."""
        self._sync_structural_undo_stack()

    def _sync_structural_undo_stack(self) -> None:
        """Rebuild ``undo_stack`` so it holds exactly ``document.structural_depth``
        StructuralCommands, with the clean marker at the saved baseline depth.

        Making the Qt stack a faithful mirror of the model's structural stack
        handles all three cardinality cases uniformly: several consecutive
        structural ops (depth N -> N commands -> N Undo steps), a tab switch onto
        a doc that already carries structural history, and a depth-cap eviction
        (the oldest snapshot dropped -> one fewer command). Each rebuilt command
        is pre-applied, so its first redo is a no-op and the model is never
        re-run; the clean index is placed so dirty/clean tracking is preserved."""
        doc = self.document
        if doc is None:
            self.undo_stack.clear()
            self.undo_stack.setClean()
            return
        depth = doc.structural_depth
        saved = max(0, min(getattr(doc, "_saved_struct_depth", 0), depth))
        block = self.undo_stack.signalsBlocked()
        self.undo_stack.blockSignals(True)
        try:
            self.undo_stack.clear()
            # Push the saved-baseline commands, then mark clean THERE so undoing
            # back to the baseline reads clean; then push the remaining (dirty)
            # structural commands above the clean marker.
            for _ in range(saved):
                self.undo_stack.push(StructuralCommand(doc, self))
            self.undo_stack.setClean()
            for _ in range(depth - saved):
                self.undo_stack.push(StructuralCommand(doc, self))
        finally:
            self.undo_stack.blockSignals(block)
        # Re-emit the state the chrome reads now that signals were blocked during
        # the rebuild, so Undo/Redo enablement + the dirty marker refresh.
        self._on_can_undo_changed(self.undo_stack.canUndo())
        self._on_can_redo_changed(self.undo_stack.canRedo())
        self._on_clean_changed(self.undo_stack.isClean())

    def _refresh_after_structural_undo(self) -> None:
        """Rebuild canvas/sidebar/tabs after a structural undo/redo (PAGES_SPEC
        §6.6). The model swapped the working doc; re-render from it and clamp the
        current page."""
        doc = self.document
        if doc is None:
            return
        page = min(self.view_page_index(), doc.page_count - 1)
        self.view.set_document(doc)
        self._set_view_page(max(0, page))
        self.sidebar.refresh()
        self.sidebar.set_current_page(self.view_page_index())
        self.tab_bar.sync(self.workspace)
        self.inspector.set_font_engine(doc.font_engine)
        self.inspector.set_target(None, None)
        self._sync_all()

    def _flush_open_editor(self) -> None:
        """Commit any open inline editor on the canvas before a structural op /
        tab switch so typed text is not lost."""
        fn = getattr(self.view, "_flush_editor", None)
        if callable(fn):
            try:
                fn()
            except (TypeError, RuntimeError):
                pass

    # ===================================================================
    # Combine / Extract / Split flows (PAGES_SPEC §5.3)
    # ===================================================================
    def combine_pdfs(self) -> None:
        """Append one or more picked PDFs onto the active document."""
        if self.document is None:
            return
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Combine PDFs (append to this document)", self._dialog_dir(),
            "PDF files (*.pdf)")
        if not paths:
            return
        doc = self.document
        self._flush_open_editor()
        try:
            with self._busy():
                for p in paths:
                    doc.merge(p)
        except Exception as exc:  # noqa: BLE001
            self._flash_error(f"Combine failed: {exc}")
            return
        self._after_structural_op(new_page=self.view_page_index())
        self._toast(
            f"Appended {len(paths)} PDF{'s' if len(paths) != 1 else ''}", 3000)

    def combine_open_tab(self, other_idx: int) -> None:
        """Append another OPEN document's pages (with its unsaved edits baked)
        into the active document (PAGES_SPEC §5.3)."""
        if self.document is None or not 0 <= other_idx < self.workspace.count:
            return
        if other_idx == self.workspace.active_index:
            return
        other = self.workspace.document(other_idx)
        self._run_structural(lambda d: d.merge(other),
                             new_page=self.view_page_index())
        self._toast(
            f"Combined {os.path.basename(other.path)} into this document", 3000)

    def extract_pages_dialog(self) -> None:
        """Extract a page range to a NEW file (non-mutating)."""
        if self.document is None:
            return
        total = self.document.page_count
        text, ok = self._prompt_page_range(
            "Extract Pages",
            f"Pages to extract (1–{total}), e.g. 1-3, 5, 8-10:")
        if not ok:
            return
        try:
            indices = self._parse_page_ranges(text, total)
        except ValueError as exc:
            self._flash_error(str(exc))
            QMessageBox.warning(self, "Invalid page range", str(exc))
            return
        if not indices:
            self._flash_error("No pages selected.")
            return
        base = os.path.splitext(os.path.basename(self.document.path))[0]
        out, _ = QFileDialog.getSaveFileName(
            self, "Save extracted PDF",
            os.path.join(os.path.dirname(self.document.path),
                         f"{base}-extract.pdf"),
            "PDF files (*.pdf)")
        if not out:
            return
        if not out.lower().endswith(".pdf"):
            out += ".pdf"
        try:
            self.document.extract_pages(indices, out)
        except Exception as exc:  # noqa: BLE001
            self._flash_error(f"Extract failed: {exc}")
            return
        self._toast(
            f"Extracted {len(indices)} page(s) to {os.path.basename(out)}", 4000)
        ans = QMessageBox.question(
            self, "Open extracted file?",
            "Open the extracted PDF in a new tab?",
            QMessageBox.Yes | QMessageBox.No)
        if ans == QMessageBox.Yes:
            self.open_path(out)

    def split_dialog(self) -> None:
        """Split the active document into multiple files by a chosen strategy."""
        if self.document is None:
            return
        dlg = _SplitDialog(self.document.page_count, self)
        if dlg.exec() != QDialog.Accepted:
            return
        ranges = dlg.ranges()
        out_dir = QFileDialog.getExistingDirectory(
            self, "Choose output folder",
            os.path.dirname(self.document.path))
        if not out_dir:
            return
        try:
            files = self.document.split(ranges, out_dir)
        except Exception as exc:  # noqa: BLE001
            self._flash_error(f"Split failed: {exc}")
            return
        self._toast(
            f"Wrote {len(files)} file{'s' if len(files) != 1 else ''} to "
            f"{os.path.basename(out_dir)}", 5000)

    # ===================================================================
    # Document info & exports (doc-tools workstream M1)
    # ===================================================================
    def _do_properties(self, apply_meta: dict | None = None) -> None:
        """File > Document Properties… (Cmd+D, ws6 §2.1). Dialog-seam rule
        (§1): the modal is constructed ONLY when ``apply_meta`` is None;
        tests pass the fields dict directly. Unchanged fields produce NO
        command; changed fields apply as ONE structural op through
        ``_run_structural`` (the dialog notes that applying bakes pending
        edits first -- the established cost of every structural boundary)."""
        doc = self.document
        if doc is None:
            return
        if apply_meta is None:
            dlg = _PropertiesDialog(doc.properties(), self)
            if dlg.exec() != QDialog.Accepted:
                return
            apply_meta = dlg.fields()
        current = doc.metadata_fields()
        changed = {k: v for k, v in apply_meta.items()
                   if (v or "") != current.get(k, "")}
        if not changed:
            return
        self._run_structural(lambda d: d.set_metadata_fields(changed))
        self._toast("Document properties updated", 3000)

    def _do_export_images(self, opts: ExportImagesOptions | None = None
                          ) -> None:
        """File > Export > Pages as Images… (ws6 §2.3). Window-level, no
        model change: every page renders through ``render_with_edits`` -- the
        save pipeline -- so exports match saved output by construction.
        Files land as ``{stem}_page{n:03d}.{ext}``. Non-mutating, no undo;
        each call re-bakes edited pages (tens of ms per edit), fine for an
        export loop. Dialog-seam rule: the modal only constructs when
        ``opts`` is None."""
        doc = self.document
        if doc is None:
            return
        if opts is None:
            dlg = _ExportImagesDialog(
                doc.page_count, os.path.dirname(doc.path), self)
            if dlg.exec() != QDialog.Accepted:
                return
            opts = dlg.options()
        try:
            indices = self._parse_page_ranges(opts.pages, doc.page_count)
        except ValueError as exc:
            self._flash_error(str(exc))
            return
        fmt = "jpg" if str(opts.fmt).lower() in ("jpg", "jpeg") else "png"
        dpi = max(18, min(int(opts.dpi), 1200))
        zoom = dpi / 72.0
        stem = os.path.splitext(os.path.basename(doc.path))[0]
        # Commit any open inline editor so its text is part of the bake.
        self._flush_open_editor()
        written = 0
        # Per-page render (up to 1200 DPI) can take many seconds, so show a
        # cancellable progress bar rather than freezing the window silently.
        total = len(indices)
        progress = QProgressDialog("Exporting images…", "Cancel", 0, total, self)
        progress.setWindowTitle("Export Images")
        progress.setWindowModality(Qt.WindowModal)
        progress.setMinimumDuration(300)   # stay hidden for a quick 1-2 page job
        try:
            for n, i in enumerate(indices):
                if progress.wasCanceled():
                    break
                progress.setValue(n)
                pix = doc.render_with_edits(i, zoom)
                out = os.path.join(
                    opts.out_dir, f"{stem}_page{i + 1:03d}.{fmt}")
                if fmt == "jpg":
                    pix.save(out, jpg_quality=85)
                else:
                    pix.save(out)
                written += 1
            progress.setValue(total)
        except Exception as exc:  # noqa: BLE001 - surface an export failure
            progress.cancel()
            self._flash_error(f"Export failed: {exc}")
            return
        if progress.wasCanceled() and written < total:
            self._toast(f"Export cancelled after {written} of {total} pages",
                        4000)
            return
        self._toast(
            f"Exported {written} image{'s' if written != 1 else ''} to "
            f"{os.path.basename(os.path.abspath(opts.out_dir))}", 5000)

    def _do_export_text(self, path: str | None = None) -> None:
        """File > Export > All Text to .txt… (ws6 §2.2). The model bakes
        staged edits into the extraction when any exist (WYSIWYG), normalizes
        the NBSP variants, and writes atomically. Non-mutating, no undo.
        Dialog-seam rule: the save-file picker only opens when ``path`` is
        None."""
        doc = self.document
        if doc is None:
            return
        if path is None:
            base = os.path.splitext(os.path.basename(doc.path))[0]
            path, _ = QFileDialog.getSaveFileName(
                self, "Export All Text",
                os.path.join(os.path.dirname(doc.path), f"{base}.txt"),
                "Text files (*.txt)")
            if not path:
                return
            if not path.lower().endswith(".txt"):
                path += ".txt"
        # Commit any open inline editor so its text is part of the bake.
        self._flush_open_editor()
        try:
            with self._busy():
                doc.export_text(path)
        except Exception as exc:  # noqa: BLE001 - surface an export failure
            self._flash_error(f"Export failed: {exc}")
            return
        self._toast(f"Exported text to {os.path.basename(path)}", 4000)

    def _export_flattened(self) -> None:
        """File > Export Flattened Copy… (ws5 M3, forms §4): the picker
        half of the dialog/work split -- asks for a destination (default
        ``<stem>-flat.pdf`` beside the source) and hands off to the
        dialog-free seam below (the modal rule: offscreen tests call
        ``_do_export_flattened`` directly)."""
        doc = self.document
        if doc is None or not getattr(doc, "has_form", False):
            return
        base = os.path.splitext(os.path.basename(doc.path))[0]
        path, _ = QFileDialog.getSaveFileName(
            self, "Export Flattened Copy",
            os.path.join(os.path.dirname(doc.path), f"{base}-flat.pdf"),
            "PDF files (*.pdf)")
        if not path:
            return
        if not path.lower().endswith(".pdf"):
            path += ".pdf"
        self._do_export_flattened(path)

    def _do_export_flattened(self, path: str) -> None:
        """The flatten work seam (forms §4): commit any open inline editor
        (its text/fill joins the bake), then ``save_flattened`` writes a
        widget-free copy -- staged edits and form values baked, every
        widget turned into ordinary page content. NON-MUTATING export: the
        open document keeps its AcroForm, staged state, dirty flag, and
        undo history (no undo entry -- nothing changed)."""
        doc = self.document
        if doc is None:
            return
        self._flush_open_editor()
        try:
            doc.save_flattened(path)
        except Exception as exc:  # noqa: BLE001 - surface an export failure
            self._flash_error(f"Export failed: {exc}")
            return
        self._toast("Exported flattened copy", 4000)

    # ===================================================================
    # Stamps: watermark + header/footer (doc-tools workstream M2)
    # ===================================================================
    def _do_watermark(self, opts: WatermarkOptions | None = None) -> None:
        """Document > Add Watermark… (ws6 §2.4). Dialog-seam rule: the modal
        only constructs when ``opts`` is None; tests pass options directly.
        Applies as ONE structural op through ``_run_structural`` (which also
        surfaces model validation errors as a flash), so undo is a single
        StructuralCommand and WYSIWYG is free -- the stamp lives in working,
        which both the screen and the save read."""
        doc = self.document
        if doc is None:
            return
        if opts is None:
            dlg = _WatermarkDialog(doc.page_count, self)
            if dlg.exec() != QDialog.Accepted:
                return
            opts = dlg.options()
        if not opts.text.strip():
            self._flash_error("Enter the watermark text.")
            return
        try:
            indices = self._parse_page_ranges(opts.pages, doc.page_count)
        except ValueError as exc:
            self._flash_error(str(exc))
            return
        depth0 = doc.structural_depth
        self._run_structural(lambda d: d.add_watermark(
            indices, text=opts.text, base14_code=opts.base14_code,
            fontsize=opts.fontsize, color=opts.color, opacity=opts.opacity,
            angle=opts.angle, position=opts.position, behind=opts.behind))
        # _run_structural flashes a failed op; only a committed snapshot
        # earns the success toast.
        if doc.structural_depth == depth0 + 1:
            n = len(set(indices))
            self._toast(
                f"Watermark added to {n} page{'s' if n != 1 else ''}", 3000)

    def _do_header_footer(self, opts: HeaderFooterOptions | None = None
                          ) -> None:
        """Document > Add Header && Footer… (ws6 §2.5). Dialog-seam rule as
        above. One structural op; the model substitutes the page-number
        tokens at stamp time."""
        doc = self.document
        if doc is None:
            return
        if opts is None:
            dlg = _HeaderFooterDialog(doc.page_count, self)
            if dlg.exec() != QDialog.Accepted:
                return
            opts = dlg.options()
        if not any((v or "").strip() for v in opts.slots.values()):
            self._flash_error("Enter at least one header or footer.")
            return
        try:
            indices = self._parse_page_ranges(opts.pages, doc.page_count)
        except ValueError as exc:
            self._flash_error(str(exc))
            return
        depth0 = doc.structural_depth
        self._run_structural(lambda d: d.add_header_footer(
            indices, slots=opts.slots, base14_code=opts.base14_code,
            fontsize=opts.fontsize, color=opts.color, top=opts.top,
            bottom=opts.bottom, side=opts.side, start_at=opts.start_at))
        if doc.structural_depth == depth0 + 1:
            n = len(set(indices))
            self._toast(
                f"Header/footer added to {n} page{'s' if n != 1 else ''}",
                3000)

    # ===================================================================
    # Crop (doc-tools workstream M3)
    # ===================================================================
    def _on_crop_toggled(self, checked: bool) -> None:
        """Document > Crop Pages… (ws6 §2.6): a checkable MODE action (the
        Add Text convention). Checking arms the canvas crop mode + toasts
        the gesture hint; the canvas reports the drawn rect via
        ``cropRectSelected``, and ``_on_view_mode_changed`` keeps the check
        in lockstep when the mode exits from the view side (Esc, another
        tool arming)."""
        if checked:
            if self.document is None:
                self.act_crop.setChecked(False)
                return
            fn = getattr(self.view, "enter_crop_mode", None)
            if callable(fn):
                try:
                    fn()
                except (TypeError, RuntimeError):
                    return
            self._mode_hint("crop",
                            "Drag a rectangle over a page, Esc to cancel",
                            5000)
        else:
            fn = getattr(self.view, "exit_crop_mode", None)
            if callable(fn):
                try:
                    fn()
                except (TypeError, RuntimeError):
                    pass

    def _crop_scope_dialog(self, page_index: int, rect: tuple,
                           page_count: int) -> "str | None":
        """The DEFAULT ``_crop_scope_provider``: the modal ``_CropDialog``.
        Returns the chosen scope ("page"/"all"), or None on cancel."""
        dlg = _CropDialog(page_index, rect, page_count, self)
        if dlg.exec() != QDialog.Accepted:
            return None
        return dlg.scope()

    def _on_crop_rect_selected(self, page_index: int, rect: tuple) -> None:
        """A crop rect was drawn on the canvas (ws6 §2.6): confirm the scope
        through the injectable provider, then apply. Cancel keeps crop mode
        ARMED so the user can redraw (Esc disarms)."""
        if self.document is None:
            return
        scope = self._crop_scope_provider(page_index, rect,
                                          self.document.page_count)
        if scope is None:
            return
        self._do_crop_apply(page_index, rect, scope)

    def _do_crop_apply(self, page_index: int, rect: tuple,
                       scope: str = "page") -> None:
        """Apply the crop as ONE structural op through ``_run_structural``
        (model validation errors surface as a flash; tests call this
        directly -- the dialog-seam rule). ``scope`` is "page" (just
        ``page_index``) or "all". On success crop mode disarms and the model
        reports the pages it SKIPPED (clamped area below the 36 pt minimum)
        for the toast; on failure the mode stays armed for a redraw.
        ``_after_structural_op``'s full ``view.reload()`` rebuilds the layer
        geometry (``pt_size``) from the new ``page.rect`` -- the coordinate
        shift the model documents."""
        doc = self.document
        if doc is None:
            return
        pages = (list(range(doc.page_count)) if scope == "all"
                 else [page_index])
        skipped: list[int] = []
        depth0 = doc.structural_depth
        self._run_structural(
            lambda d: skipped.extend(d.crop_pages(pages, rect)),
            new_page=page_index)
        if doc.structural_depth != depth0 + 1:
            return    # _run_structural already flashed the failure
        fn = getattr(self.view, "exit_crop_mode", None)
        if callable(fn):
            try:
                fn()
            except (TypeError, RuntimeError):
                pass
        n = len(pages) - len(skipped)
        if skipped:
            self._toast(
                f"Cropped {n} of {len(pages)} pages "
                f"({len(skipped)} skipped below the 36 pt minimum)", 5000)
        else:
            self._toast(f"Cropped {n} page{'s' if n != 1 else ''}", 3000)

    # ===================================================================
    # Security / optimize / print (doc-tools workstream M4)
    # ===================================================================
    def _do_security(self, opts: SecurityOptions | None = None) -> None:
        """Document > Security… (ws6 §2.7). Dialog-seam rule: the modal only
        constructs when ``opts`` is None; tests pass options directly. The
        model stages the change as a PENDING SAVE OPTION (``set_security``)
        -- not an undo entry; the dialog is the revert surface -- so this
        only flips dirty/Save and toasts that it applies on the next save."""
        doc = self.document
        if doc is None:
            return
        if opts is None:
            dlg = _SecurityDialog(doc.encrypts_on_save, self)
            if dlg.exec() != QDialog.Accepted:
                return
            opts = dlg.options()
        if opts.action == "remove":
            doc.set_security(encryption=PDF_ENCRYPT_NONE,
                             user_pw=None, owner_pw=None)
        elif opts.action == "set":
            if not opts.password:
                self._flash_error("Enter a password to protect the file.")
                return
            # One password controls open AND owner rights; permissions stay
            # all-granted (granular checkboxes de-scoped, ws6 §5).
            doc.set_security(encryption=PDF_ENCRYPT_AES_256,
                             user_pw=opts.password, owner_pw=opts.password)
        else:
            self._flash_error(f"Unknown security action: {opts.action!r}")
            return
        self._toast("Security changes apply on next save", 4000)
        self._sync_save_enabled()
        self._sync_status()

    # ===================================================================
    # OCR — scanned pages -> editable text in a scan-built font (OCR_SPEC)
    # ===================================================================
    def _ocr_engine_pref(self) -> str:
        """The effective OCR engine name. Windows has only RapidOCR; macOS
        defaults to Apple Vision and remembers the user's Settings choice."""
        import sys
        if sys.platform != "darwin":
            return "rapidocr"
        val = self._settings().value("ocr/engine", "applevision")
        return val if val in ("applevision", "rapidocr") else "applevision"

    def _set_ocr_engine_pref(self, name: str) -> None:
        """Persist the macOS OCR engine choice made in Settings."""
        if name in ("applevision", "rapidocr"):
            self._settings().setValue("ocr/engine", name)

    def _ocr_base_fonts(self) -> tuple:
        """Resolve absolute font-file paths used to BORROW glyphs the scan never
        showed: a serif (Times) and a sans (Arial/Helvetica). Resolved on the
        GUI thread; the worker only reads the files."""
        serif = (FontEngine.system_face_for("Times New Roman", False, False)
                 or FontEngine.system_face_for("Times", False, False))
        sans = (FontEngine.system_face_for("Arial", False, False)
                or FontEngine.system_face_for("Helvetica", False, False))
        # Last-resort: the bundled DejaVu faces always ship with the app.
        fallback = (FontEngine.system_face_for("DejaVu Sans", False, False))
        return serif or fallback, sans or fallback

    def _do_ocr(self, scope: str = "page") -> None:
        """Recognize scanned page(s) and inject the result as editable text in a
        font built from the scanned glyphs (OCR_SPEC §3/§4). ``scope`` is
        "page" (the current page) or "document" (every image-only page). The
        heavy recognition runs on a daemon thread; results are applied back on
        the GUI thread, one undo step per page."""
        doc = self.document
        if doc is None or getattr(self, "_ocr_running", False):
            return
        self._flush_open_editor()
        if scope == "page":
            pi = self.view_page_index()
            if doc.page_has_text_layer(pi):
                self._toast("This page already has text — OCR is for scans.")
                return
            targets = [pi]
        else:
            targets = doc.image_only_pages()
            if not targets:
                self._toast("No scanned (image-only) pages to OCR.")
                return
        serif, sans = self._ocr_base_fonts()
        if not serif or not sans:
            self._flash_error("OCR needs a base font and none was found.")
            return
        # Render every target page to RGB on the GUI thread (fitz), then hand the
        # images to the worker so no fitz/Qt object crosses the thread boundary.
        try:
            jobs = [(pi, doc.render_page_image(pi, 300.0)) for pi in targets]
        except Exception as exc:                       # noqa: BLE001
            self._flash_error(f"Could not render page for OCR: {exc}")
            return
        self._ocr_begin(jobs, serif, sans)

    def _ocr_begin(self, jobs: list, serif: str, sans: str) -> None:
        import queue
        import threading
        self._ocr_running = True
        self._ocr_cancel = False
        self._ocr_queue: "queue.Queue" = queue.Queue()
        self._ocr_total = len(jobs)
        self._ocr_seen = 0
        self._ocr_applied = 0
        self._ocr_errors: list = []
        self._ocr_progress = QProgressDialog(
            "Recognizing scanned text…", "Cancel", 0, len(jobs), self)
        self._ocr_progress.setWindowTitle("OCR")
        self._ocr_progress.setMinimumDuration(0)
        self._ocr_progress.setValue(0)
        self._ocr_progress.canceled.connect(self._ocr_on_cancel)
        # Engine choice (Settings): macOS user toggle (Apple Vision / RapidOCR);
        # Windows is always RapidOCR. Read on the GUI thread; the worker uses it.
        engine = self._ocr_engine_pref()

        def worker():
            from pdftexteditor.ocr import recognize_and_reconstruct
            for pi, img in jobs:
                if self._ocr_cancel:
                    break
                try:
                    res = recognize_and_reconstruct(
                        img, 300.0, serif, sans, engine_name=engine,
                        family_label=f"Scanned p{pi + 1}")
                except Exception as exc:               # noqa: BLE001
                    res = exc
                self._ocr_queue.put((pi, res))
            self._ocr_queue.put(None)                  # done sentinel

        self._ocr_thread = threading.Thread(
            target=worker, name="ocr-worker", daemon=True)
        self._ocr_thread.start()
        self._ocr_timer = QTimer(self)
        self._ocr_timer.timeout.connect(self._ocr_poll)
        self._ocr_timer.start(60)
        self._sync_actions()

    def _ocr_on_cancel(self) -> None:
        self._ocr_cancel = True

    def _ocr_poll(self) -> None:
        """GUI-thread drain of the OCR worker's result queue: apply finished
        pages (register font + one undo macro of NewBoxes), advance progress,
        finish on the sentinel."""
        import queue
        while True:
            try:
                item = self._ocr_queue.get_nowait()
            except queue.Empty:
                break
            if item is None:
                self._ocr_finish()
                return
            pi, res = item
            self._ocr_seen += 1
            if isinstance(res, Exception):
                self._ocr_errors.append((pi, res))
            elif res is not None and res.lines:
                try:
                    self._ocr_apply_page(pi, res)
                    self._ocr_applied += 1
                except Exception as exc:               # noqa: BLE001
                    self._ocr_errors.append((pi, exc))
            if hasattr(self, "_ocr_progress"):
                self._ocr_progress.setValue(self._ocr_seen)
        if self._ocr_cancel:
            self._ocr_finish()

    def _ocr_apply_page(self, page_index: int, res) -> None:
        """Register the page's scan-built font and add one editable NewBox per
        recognized line as ONE undo step (BoxCommand macro, the
        ``_move_existing_image`` pattern)."""
        FontEngine.register_custom_face(res.family_name, res.otf_bytes)
        self.undo_stack.beginMacro(f"OCR page {page_index + 1}")
        try:
            for lb in res.lines:
                # The reconstruction works in the rendered (display) image; map
                # each origin back to text space + writing direction so the text
                # bakes upright even on a /Rotate page.
                origin, direction = self.document.ocr_text_placement(
                    page_index, lb.origin)
                cover = ()
                if lb.cover:
                    cx0, cy0, cx1, cy1 = self.document.ocr_cover_rect(
                        page_index, lb.cover)
                    cover = (cx0, cy0, cx1, cy1) + tuple(res.bg_color)
                self.undo_stack.push(BoxCommand(
                    self.document, self.view, page_index, None, "add", {
                        "origin": origin, "direction": direction,
                        "cover": cover,
                        "text": lb.text, "family": res.family_name,
                        "size": lb.size, "color": (0.0, 0.0, 0.0),
                        "bold": False, "italic": False}))
        finally:
            self.undo_stack.endMacro()

    def _ocr_finish(self) -> None:
        if not getattr(self, "_ocr_running", False):
            return
        if hasattr(self, "_ocr_timer"):
            self._ocr_timer.stop()
        if hasattr(self, "_ocr_progress"):
            self._ocr_progress.close()
        self._ocr_running = False
        if self._ocr_applied:
            self.view.reload()
            self.sidebar.refresh()
            self._sync_all()
        applied, errs = self._ocr_applied, len(self._ocr_errors)
        if self._ocr_cancel:
            self._toast(f"OCR cancelled — {applied} page(s) done.", 4000)
        elif errs and not applied:
            self._flash_error("OCR failed — no text recognized.")
        elif errs:
            self._toast(f"OCR done: {applied} page(s); {errs} failed.", 5000)
        elif applied:
            self._toast(f"OCR complete — {applied} page(s) now editable.", 4000)
        else:
            self._toast("OCR found no recognizable text.", 4000)
        self._sync_actions()

    def _do_optimize(self, path: str | None = None) -> None:
        """File > Save Optimized Copy… (ws6 §2.8): just the save-file picker
        -- NO options dialog (fixed probe-verified flags; the realism cut).
        Non-mutating export through ``save_optimized_copy`` (the
        ``_baked_copy`` seam, so staged edits are included); the tab does NOT
        repoint to the copy and nothing lands on the undo stack."""
        doc = self.document
        if doc is None:
            return
        if path is None:
            base = os.path.splitext(os.path.basename(doc.path))[0]
            path, _ = QFileDialog.getSaveFileName(
                self, "Save Optimized Copy",
                os.path.join(os.path.dirname(doc.path),
                             f"{base}-optimized.pdf"),
                "PDF files (*.pdf)")
            if not path:
                return
            if not path.lower().endswith(".pdf"):
                path += ".pdf"
        # Commit any open inline editor so its text is part of the bake.
        self._flush_open_editor()
        try:
            with self._busy():
                before, after = doc.save_optimized_copy(path)
        except Exception as exc:  # noqa: BLE001 - surface an export failure
            self._flash_error(f"Optimize failed: {exc}")
            return
        pct = round((after - before) / before * 100) if before else 0
        self._toast(
            f"Optimized: {doctools.human_size(before)} -> "
            f"{doctools.human_size(after)} ({pct:+d}%)", 5000)

    def _do_print(self, printer: "QPrinter | None" = None) -> None:
        """File > Print… (Cmd+P, ws6 §2.9). Dialog-seam rule: the native
        QPrintDialog only opens when ``printer`` is None; tests inject a
        PdfFormat QPrinter. Every page renders through ``render_with_edits``
        -- the save pipeline -- so the print matches the saved file by
        construction. Non-mutating, no undo."""
        doc = self.document
        if doc is None:
            return
        if printer is None:
            printer = QPrinter(QPrinter.PrinterMode.HighResolution)
            # Open the dialog ALREADY in the document's own orientation, so a
            # landscape PDF shows Landscape pre-selected and the user no longer
            # flips it for every file. A tiny page-1 probe render is
            # rotation-aware (a /Rotate 90 page comes back wide), matching the
            # per-page orientation the print loop uses.
            try:
                probe = doc.render_with_edits(0, 0.25)
                printer.setPageOrientation(
                    QPageLayout.Orientation.Landscape
                    if probe.width > probe.height
                    else QPageLayout.Orientation.Portrait)
            except Exception:  # noqa: BLE001 - never block the dialog on a probe
                pass
            dlg = QPrintDialog(printer, self)
            dlg.setMinMax(1, doc.page_count)
            if dlg.exec() != QDialog.Accepted:
                return
        # Commit any open inline editor so its text is part of the bake.
        self._flush_open_editor()
        # The dialog's selected range (fromPage/toPage are 1-based; 0 = all).
        first, last = printer.fromPage(), printer.toPage()
        if first <= 0:
            first, last = 1, doc.page_count
        last = min(doc.page_count, last if last > 0 else doc.page_count)
        if last < first:
            return
        # Render at the printer's resolution, capped at 600 dpi: a 1200 dpi
        # HighResolution device would mean ~5100px-wide page buffers for no
        # visible gain (probe-verified the cap renders cleanly).
        zoom = min(printer.resolution(), 600) / 72.0
        pages = list(range(first - 1, last))

        def _orientation_for(pix) -> "QPageLayout.Orientation":
            # The rendered pixmap is rotation-aware (a /Rotate 90 page comes
            # back wide), so its own aspect is the source of truth.
            return (QPageLayout.Orientation.Landscape if pix.width > pix.height
                    else QPageLayout.Orientation.Portrait)

        # AUTO-ORIENT: match the printer to the page's real shape so a landscape
        # certificate prints landscape WITHOUT the user hand-picking it (and so
        # a manual choice is not silently overridden by a portrait default that
        # then shrinks the wide page). Orientation MUST be set before
        # painter.begin for the first page; it is re-set per page before each
        # newPage so a mixed-orientation document prints each page correctly.
        # Rendering every page can take a while; show the wait cursor (restored
        # on every exit path below, including the painter-begin failure).
        QGuiApplication.setOverrideCursor(QCursor(Qt.CursorShape.WaitCursor))
        first_pix = doc.render_with_edits(pages[0], zoom)
        printer.setPageOrientation(_orientation_for(first_pix))

        painter = QPainter()
        if not painter.begin(printer):
            QGuiApplication.restoreOverrideCursor()
            self._flash_error("Could not start the print job.")
            return
        try:
            for k, i in enumerate(pages):
                pix = first_pix if k == 0 else doc.render_with_edits(i, zoom)
                if k:
                    printer.setPageOrientation(_orientation_for(pix))
                    printer.newPage()
                # .copy() detaches from pix.samples so the buffer may die.
                img = QImage(bytes(pix.samples), pix.width, pix.height,
                             pix.stride, QImage.Format.Format_RGB888).copy()
                # Scale into the printable area preserving aspect, centered;
                # painter coords originate at the page rect's top-left.
                area = printer.pageRect(QPrinter.Unit.DevicePixel)
                scale = min(area.width() / img.width(),
                            area.height() / img.height())
                w = img.width() * scale
                h = img.height() * scale
                painter.drawImage(
                    QRectF((area.width() - w) / 2.0,
                           (area.height() - h) / 2.0, w, h), img)
        except Exception as exc:  # noqa: BLE001 - surface a render failure
            self._flash_error(f"Print failed: {exc}")
            return
        finally:
            painter.end()
            QGuiApplication.restoreOverrideCursor()
        n = last - first + 1
        self._toast(
            f"Sent {n} page{'s' if n != 1 else ''} to the printer", 4000)

    # --- page-range parsing + small prompt -------------------------------
    @staticmethod
    def _parse_page_ranges(text: str, page_count: int) -> list[int]:
        """Parse a 1-based page-range string ("1-3, 5, 8-10") to 0-based indices
        in the given order (PAGES_SPEC §5.3). Raises ``ValueError`` on bad input
        or an out-of-range page."""
        indices: list[int] = []
        if not text or not text.strip():
            raise ValueError("Enter at least one page or range.")
        for part in text.split(","):
            part = part.strip()
            if not part:
                continue
            if "-" in part:
                a, _, b = part.partition("-")
                a, b = a.strip(), b.strip()
                if not a.isdigit() or not b.isdigit():
                    raise ValueError(f"Invalid range: '{part}'")
                start, end = int(a), int(b)
                if start < 1 or end < 1 or start > page_count or end > page_count:
                    raise ValueError(
                        f"Range {part} is outside 1–{page_count}.")
                step = 1 if end >= start else -1
                for p in range(start, end + step, step):
                    indices.append(p - 1)
            else:
                if not part.isdigit():
                    raise ValueError(f"Invalid page: '{part}'")
                p = int(part)
                if p < 1 or p > page_count:
                    raise ValueError(f"Page {p} is outside 1–{page_count}.")
                indices.append(p - 1)
        if not indices:
            raise ValueError("No pages selected.")
        return indices

    def _prompt_page_range(self, title: str, label: str) -> tuple[str, bool]:
        """A tiny modal text prompt for a page-range string."""
        dlg = QDialog(self)
        dlg.setWindowTitle(title)
        lay = QVBoxLayout(dlg)
        lay.addWidget(QLabel(label))
        field = QLineEdit()
        field.setPlaceholderText("e.g. 1-3, 5, 8-10")
        lay.addWidget(field)
        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(dlg.accept)
        buttons.rejected.connect(dlg.reject)
        lay.addWidget(buttons)
        ok = dlg.exec() == QDialog.Accepted
        return field.text(), ok

    # ===================================================================
    # Navigation
    # ===================================================================
    def view_page_index(self) -> int:
        """Read the canvas's current page index across API variants."""
        pi = getattr(self.view, "page_index", 0)
        return pi() if callable(pi) else pi

    def view_zoom(self) -> float:
        z = getattr(self.view, "zoom", 1.0)
        return z() if callable(z) else z

    def _set_view_page(self, index: int) -> None:
        self.view.set_page(index)
        if hasattr(self, "sidebar"):
            self.sidebar.set_current_page(self.view_page_index())

    def prev_page(self) -> None:
        if self.document:
            self._set_view_page(self.view_page_index() - 1)
            self._sync_page_indicator()

    def next_page(self) -> None:
        if self.document:
            self._set_view_page(self.view_page_index() + 1)
            self._sync_page_indicator()

    def first_page(self) -> None:
        if self.document:
            self._set_view_page(0)
            self._sync_page_indicator()

    def last_page(self) -> None:
        if self.document:
            self._set_view_page(self.document.page_count - 1)
            self._sync_page_indicator()

    def _page_field_entered(self) -> None:
        if not self.document:
            return
        try:
            target = int(self.page_field.text()) - 1
        except ValueError:
            self._sync_page_indicator()
            return
        self._set_view_page(max(0, min(target, self.document.page_count - 1)))
        self._sync_page_indicator()
        self.view.setFocus()

    def _focus_page_field(self) -> None:
        if self.document:
            self.page_field.setFocus()
            self.page_field.selectAll()

    # ===================================================================
    # Zoom
    # ===================================================================
    def set_zoom(self, zoom: float) -> None:
        if not self.document:
            return
        zoom = max(ZOOM_MIN, min(zoom, ZOOM_MAX))
        self.view.set_zoom(zoom)
        self._sync_zoom_indicator()

    def zoom_in(self) -> None:
        if self.document:
            self.set_zoom(self.view_zoom() * ZOOM_STEP)

    def zoom_out(self) -> None:
        if self.document:
            self.set_zoom(self.view_zoom() / ZOOM_STEP)

    def fit_page(self) -> None:
        self._set_zoom_mode("fit_page")

    def fit_width(self) -> None:
        self._set_zoom_mode("fit_width")

    def _set_zoom_mode(self, mode: str) -> None:
        # ASSUMPTION (BUILD_SPEC §5.2): PageView.set_zoom_mode("fit_page"|
        # "fit_width") exists. If not, fall back to a sensible fixed zoom so the
        # menu items still do something against the MVP canvas.
        if not self.document:
            return
        fn = getattr(self.view, "set_zoom_mode", None)
        if callable(fn):
            fn(mode)
        else:
            self.set_zoom(1.0)
        self._sync_zoom_indicator()

    # ===================================================================
    # State sync
    # ===================================================================
    def _sync_all(self) -> None:
        self._sync_actions()
        self._sync_page_indicator()
        self._sync_zoom_indicator()
        self._sync_status()
        # Doc-level refreshes (open/close, tab switch, structural op, save
        # reload) all funnel through here -- re-list the Comments panel so
        # its rows track the active document (§5.4 refresh wiring), and
        # rebuild the Bookmarks tree (navigation M1: set_outline + page ops
        # + their undo/redo all land here via _after_structural_op /
        # _refresh_after_structural_undo).
        self._refresh_comments()
        self._refresh_bookmarks()
        # Window chrome (navigation M2): the macOS proxy icon follows the
        # active doc via windowFilePath (set BEFORE the title -- Qt derives a
        # title from the path, and the explicit setWindowTitle must win);
        # the [*] modified marker tracks _has_unsaved() here too, because
        # structural ops dirty the model without touching the Qt undo
        # stack's clean state (_on_clean_changed alone missed them).
        self.setWindowFilePath(
            self.document.path if self.document is not None else "")
        self.setWindowTitle(self._title())
        self.setWindowModified(self._has_unsaved())

    def _sync_actions(self) -> None:
        has_doc = self.document is not None
        for act in (self.act_save_as, self.act_close, self.act_prev, self.act_next,
                    self.act_goto, self.act_first, self.act_last, self.act_zoom_in,
                    self.act_zoom_out, self.act_actual_size, self.act_fit_page,
                    self.act_fit_width, self.act_share):
            act.setEnabled(has_doc)
        self.zoom_button.setEnabled(has_doc)
        self.page_field.setEnabled(has_doc)
        # Hide the page + zoom + edit toolbar groups entirely while no document
        # is open, so the empty state shows no inert white page field, stray
        # "150%", or dangling Add/Delete tools.
        for act in (self._page_group_actions + self._zoom_group_actions
                    + self._edit_group_actions):
            act.setVisible(has_doc)
        self._page_zoom_separator.setVisible(has_doc)
        self._edit_group_separator.setVisible(has_doc)
        # Organize group + the page-management actions track has_doc; Delete Page
        # additionally requires more than one page.
        self._organize_action.setVisible(has_doc)
        self._organize_separator.setVisible(has_doc)
        self.organize_button.setEnabled(has_doc)
        for act in self._page_actions:
            act.setEnabled(has_doc)
        if has_doc:
            self.act_delete_page.setEnabled(self.document.page_count > 1)
        # Hide the Format dock entirely while no document is open: an inert
        # "Select a text box..." panel both reads as unfinished and shoves the
        # centered empty-state CTA left of true window center (the canvas width is
        # reduced by the dock). The dock has NoDockWidgetFeatures, so hiding it
        # leaves no ghost handle; it reappears on open via this same path.
        if hasattr(self, "format_dock"):
            self.format_dock.setVisible(has_doc)
            # Size the Format column to the current window width on open (the
            # resize path keeps it proportional as the window changes).
            if has_doc:
                self._apply_left_dock_width()
        # Mirror the same treatment for the Pages dock: in the empty state a blank
        # 220px gray panel both reads as unfinished and shoves the centered CTA
        # right of true center. Block the dock's visibilityChanged so hiding it
        # here does not flip act_toggle_pages off for the next open; re-show on
        # open via this same path (only when the toggle is checked, so a user who
        # explicitly closed the sidebar keeps it closed).
        if hasattr(self, "pages_dock"):
            self.pages_dock.blockSignals(True)
            self.pages_dock.setVisible(self._pages_dock_should_show())
            self.pages_dock.blockSignals(False)
        self.act_undo.setEnabled(has_doc and self.undo_stack.canUndo())
        self.act_redo.setEnabled(has_doc and self.undo_stack.canRedo())
        # Add Text is available whenever a doc is open; Delete only with a live
        # selection and no open editor (BUILD_SPEC §5.2).
        self.act_add_text.setEnabled(has_doc)
        self.act_find.setEnabled(has_doc)
        # OCR (OCR_SPEC §4): "This Page" only for a page with no text layer
        # (a scan); "Document" whenever a doc is open (it skips text pages).
        if has_doc and not getattr(self, "_ocr_running", False):
            try:
                page_is_scan = not self.document.page_has_text_layer(
                    self.view_page_index())
            except Exception:
                page_is_scan = True
            self.act_ocr_page.setEnabled(page_is_scan)
            self.act_ocr_document.setEnabled(True)
        else:
            self.act_ocr_page.setEnabled(False)
            self.act_ocr_document.setEnabled(False)
        # The ws2 M1/M3 actions track has_doc; their handlers additionally
        # no-op without an applicable target (editor selection / selected
        # box / pasteable clipboard). ws7 M2 layers editor-aware enablement
        # on top of Cut/Copy/Paste.
        for act in (self.act_bold, self.act_italic,
                    self.act_select_tool, self.act_select_text_tool,
                    self.act_text_edit_tool,
                    self.act_cut, self.act_copy, self.act_paste):
            act.setEnabled(has_doc)
        # Group needs >=2 text boxes selected; Ungroup needs a single paragraph.
        sel = self.view.selected_boxes() if has_doc else []
        text_sel = [b for b in sel
                    if not (isinstance(getattr(b, "identity", None), tuple)
                            and len(b.identity) >= 2
                            and b.identity[1] in ("img", "xim"))]
        self.act_group.setEnabled(has_doc and len(text_sel) >= 2)
        single = self.view.current_selection() if has_doc else None
        self.act_ungroup.setEnabled(
            has_doc and len(sel) <= 1
            and getattr(single, "is_paragraph", False))
        # Markup tools (annotations & markup §5.2): enabled with a document;
        # their toolbar buttons hide via _edit_group_actions above. Delete
        # Annotation additionally needs a live annot selection (M2).
        for act in getattr(self, "_markup_actions", {}).values():
            act.setEnabled(has_doc)
            if not has_doc and act.isChecked():
                act.setChecked(False)
        if hasattr(self, "act_delete_annot"):
            if not has_doc:
                self._selected_annot = None
            self.act_delete_annot.setEnabled(
                has_doc and self._selected_annot is not None)
        if hasattr(self, "act_show_comments"):
            self.act_show_comments.setEnabled(has_doc)
        if hasattr(self, "act_toggle_bookmarks"):
            self.act_toggle_bookmarks.setEnabled(has_doc)
        # ws6 M1-M4: the doc-tools entries (Properties / exports / stamps /
        # crop / security / optimize / print) need an open document; beyond
        # that they are stateless.
        for act in (self.act_properties, self.act_export_images,
                    self.act_export_text, self.act_watermark,
                    self.act_header_footer, self.act_crop,
                    self.act_print, self.act_optimize, self.act_security):
            act.setEnabled(has_doc)
        # ws4 M1/M2: the object-placement entries (Insert Image / Draw
        # Signature / Signature from File / the five stamps) need an open
        # document; their toolbar buttons hide via _edit_group_actions
        # above. Manage Signatures stays enabled (it only opens a folder),
        # and the library entries are gated in _rebuild_signature_menu.
        if hasattr(self, "act_insert_image"):
            self.act_insert_image.setEnabled(has_doc)
        if hasattr(self, "act_draw_signature"):
            self.act_draw_signature.setEnabled(has_doc)
            self.act_signature_from_file.setEnabled(has_doc)
        for act in getattr(self, "_stamp_actions", ()):
            act.setEnabled(has_doc)
        if hasattr(self, "left_panel"):
            self.left_panel.set_enabled_tools(has_doc)
        # ws5 M2: the form badge tracks the ACTIVE document's AcroForm
        # census -- visible only for a form-bearing doc (zero new chrome on
        # everything else). _activate_document funnels through here too
        # (_sync_all), so tab switches re-sync it.
        if hasattr(self, "form_badge"):
            if has_doc and getattr(self.document, "has_form", False):
                n = self.document.form_field_count
                self.form_badge.setText(
                    f"Form · {n} field{'s' if n != 1 else ''}")
                self.form_badge.show()
            else:
                self.form_badge.hide()
        # ws5 M3: Export Flattened Copy needs an open FORM doc (forms §4)
        # -- on everything else the entry stays visible but disabled.
        if hasattr(self, "act_flatten"):
            self.act_flatten.setEnabled(
                has_doc and getattr(self.document, "has_form", False))
        has_selection = has_doc and self._current_selection() is not None
        self.act_delete.setEnabled(has_selection)
        if not has_doc and self.act_add_text.isChecked():
            self.act_add_text.setChecked(False)
        if not has_doc and self.act_crop.isChecked():
            self.act_crop.setChecked(False)
        self._sync_save_enabled()

    def _sync_save_enabled(self) -> None:
        # The primary Save pill stays a live indigo CTA whenever a document is
        # open (matching the widget), instead of greying out when clean. Saving
        # a clean doc is a harmless in-place rewrite; save_pdf already flushes
        # any open editor first.
        self.act_save.setEnabled(self.document is not None)

    def _has_unsaved(self) -> bool:
        if self.document is None:
            return False
        # Dirty is driven by the undo stack's clean state OR the model's own dirty
        # flag (PAGES_SPEC §6.8): a structural op clears the Qt stack but leaves
        # the doc dirty, so Save must stay enabled on the model flag too.
        return not self.undo_stack.isClean() or self.document.dirty

    def _sync_page_indicator(self) -> None:
        if self.document is None:
            self.page_field.setText("")
            self.page_field.setValidator(None)
            self.page_total.setText("of 0")
            self.titlebar_meta.setText("")
            if hasattr(self, "pages_count"):
                self.pages_count.setText("")
            return
        total = self.document.page_count
        if hasattr(self, "pages_count"):
            self.pages_count.setText(str(total))
        cur = self.view_page_index() + 1
        self.page_field.setValidator(QIntValidator(1, total, self.page_field))
        self.page_field.setText(str(cur))
        self.page_total.setText(f"of {total}")
        # The top-bar doc title carries the human-readable page meta.
        self.titlebar_meta.setText(f"{cur} / {total}")
        self.act_prev.setEnabled(self.view_page_index() > 0)
        self.act_next.setEnabled(self.view_page_index() < total - 1)

    def _sync_zoom_indicator(self) -> None:
        # With no document open there is nothing being zoomed, so show no
        # percentage (an empty toolbar button + a dash in the status bar)
        # instead of a meaningless "150%". The whole zoom bar is hidden in the
        # empty state, so the step buttons just disable here.
        if self.document is None:
            self.zoom_button.setText("—")
            if hasattr(self, "_zoom_out_btn"):
                self._zoom_out_btn.setEnabled(False)
                self._zoom_in_btn.setEnabled(False)
            return
        z = self.view_zoom()
        self.zoom_button.setText(f"{int(round(z * 100))}%")
        # Disable a step button at the real engine limit, so - / + dim exactly
        # when zoom_out / zoom_in can no longer change the value (a tiny epsilon
        # absorbs float rounding from the *1.25 / /1.25 steps).
        if hasattr(self, "_zoom_out_btn"):
            self._zoom_out_btn.setEnabled(z > ZOOM_MIN + 1e-4)
            self._zoom_in_btn.setEnabled(z < ZOOM_MAX - 1e-4)

    def _toast(self, message: str, ms: int = 3500) -> None:
        """Show a transient message in the filename slot, then restore the real
        filename. Replaces QStatusBar.showMessage(), which overlapped the
        persistent left-cluster labels and garbled the text."""
        self._toast_active = True
        self._hint_mode = None        # an ordinary toast supersedes any hint
        self.filename_label.setText(message)
        self._toast_timer.start(ms)

    def _mode_hint(self, mode: str, message: str, ms: int = 3500) -> None:
        """A gesture-hint toast TIED to a view mode: shown like a toast, but
        dismissed immediately when the mode exits (Esc, placement done,
        another tool arming) so 'Esc to cancel' never outlives the mode."""
        self._toast(message, ms)
        self._hint_mode = mode

    def _dismiss_mode_hint(self, mode: str) -> None:
        """Drop the lingering hint when the view left its mode."""
        if self._hint_mode is not None and self._hint_mode != mode:
            self._toast_timer.stop()
            self._hint_mode = None
            self._restore_status()

    def _restore_status(self) -> None:
        self._toast_active = False
        self._hint_mode = None
        self._sync_status()

    def _edit_census_text(self) -> str:
        """The status-bar counter. Zero STAGED edits with unsaved changes
        (structural page ops, or an undone-after-save divergence) reads
        'Unsaved changes' so the counter never contradicts the dirty dot
        right after the most common page operation."""
        count = self.document.edit_count
        if count:
            return f"{count} edit{'s' if count != 1 else ''}"
        return "Unsaved changes" if self._has_unsaved() else "No edits"

    def _sync_status(self) -> None:
        if self._toast_active:
            # A transient message owns the filename slot; refresh only the
            # secondary labels so the toast text is not clobbered.
            if self.document is not None:
                self.edit_count_label.setText(self._edit_census_text())
                self._refresh_dirty_dot()
            return
        if self.document is None:
            self.filename_label.setText("No document")
            self.titlebar_meta.setText("")
            self.edit_count_label.setText("")
            self._refresh_dirty_dot()
            return
        self.filename_label.setText(os.path.basename(self.document.path))
        self.edit_count_label.setText(self._edit_census_text())
        self._refresh_dirty_dot()

    def _title(self) -> str:
        if self.document is None:
            return "PDF Text Editor"
        name = os.path.basename(self.document.path)
        # [*] is Qt's window-modified placeholder, driven by setWindowModified.
        return f"{name}[*] — PDF Text Editor"

    def _flash_error(self, message: str) -> None:
        self._toast(message, 5000)

    # ===================================================================
    # Empty state
    # ===================================================================
    def _hide_empty_state(self) -> None:
        if self.empty_state.isHidden():
            return
        # Stop intercepting input immediately so state is correct the instant a
        # document opens; the fade below is a purely cosmetic flourish layered on
        # top (BUILD_SPEC §6.7). Hiding does not depend on the animation running.
        self.empty_state.setEnabled(False)

        effect = QGraphicsOpacityEffect(self.empty_state)
        self.empty_state.setGraphicsEffect(effect)
        self._fade_anim = QPropertyAnimation(effect, b"opacity", self)
        self._fade_anim.setDuration(120)
        self._fade_anim.setStartValue(1.0)
        self._fade_anim.setEndValue(0.0)

        def done():
            self.empty_state.hide()
            self.empty_state.setGraphicsEffect(None)

        self._fade_anim.finished.connect(done)
        self._fade_anim.start()

    def _show_empty_state(self) -> None:
        self._refresh_empty_recents()
        self.empty_state.setGraphicsEffect(None)
        self.empty_state.setEnabled(True)
        self.empty_state.show()
        self.empty_state.raise_()
        self._position_empty_state()

    def _position_empty_state(self) -> None:
        self.empty_state.setGeometry(self.canvas.rect())

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._position_empty_state()
        self._apply_responsive_chrome()
        self._kick_persist()

    def _pages_dock_should_show(self) -> bool:
        """The Pages sidebar shows only with a document open, the user's toggle
        on, AND a wide-enough window: it auto-yields its width to the document
        when the window is narrow, so the page stays visible on small screens.
        Widen the window (or it auto-returns above 1024px) to get it back."""
        return (self.document is not None
                and getattr(self, "act_toggle_pages", None) is not None
                and self.act_toggle_pages.isChecked()
                and self.width() >= _PAGES_AUTOHIDE_W)

    def _apply_responsive_chrome(self) -> None:
        """Keep the DOCUMENT visible as the window shrinks: auto-hide the Pages
        sidebar below its width threshold, and SCALE the left Format dock so it
        narrows (yielding room to the document) on a small window and grows back
        on a wide one. Signal-blocked so it never corrupts the Pages toggle."""
        dock = getattr(self, "pages_dock", None)
        if dock is not None:
            want = self._pages_dock_should_show()
            if dock.isVisible() != want:
                dock.blockSignals(True)
                dock.setVisible(want)
                dock.blockSignals(False)
        self._apply_left_dock_width()

    def _apply_left_dock_width(self) -> None:
        """Size the left Format dock proportionally to the window width: it
        shrinks toward the min (rail + narrowest usable Format column) as the
        window narrows and grows toward the max when wide. Always visible -- the
        Format controls stay reachable at every size, just narrower when small."""
        dock = getattr(self, "left_dock", None)
        if dock is None or self.document is None:
            return
        lo = theme.RAIL_WIDTH + _LEFT_DOCK_MIN_CONTENT
        hi = theme.RAIL_WIDTH + _LEFT_DOCK_MAX_CONTENT
        span = max(1, _LEFT_DOCK_FULL_W - theme.WINDOW_MIN_W)
        frac = max(0.0, min(1.0, (self.width() - theme.WINDOW_MIN_W) / span))
        target = int(round(lo + (hi - lo) * frac))
        # A width RANGE (not a fixed width) keeps the separator draggable; the
        # proportional target is applied within it and re-applied on each resize.
        dock.setMinimumWidth(lo)
        dock.setMaximumWidth(hi)
        self.resizeDocks([dock], [target], Qt.Horizontal)

    # --- window-wide drag-drop (navigation M3) -------------------------
    # The window accepts drops everywhere (toolbar, docks, status bar) and
    # delegates to the canvas's handlers, so the same multi-file open AND
    # the same dashed drop affordance serve both surfaces.
    def dragEnterEvent(self, event) -> None:
        self.canvas.dragEnterEvent(event)

    def dragLeaveEvent(self, event) -> None:
        self.canvas.dragLeaveEvent(event)

    def dropEvent(self, event) -> None:
        self.canvas.dropEvent(event)

    # ===================================================================
    # Close / quit guard (BUILD_SPEC §6.4)
    # ===================================================================
    def _confirm_discard_if_dirty(self) -> bool:
        """Return True if it is safe to proceed (saved, discarded, or clean);
        False if the user cancelled."""
        if self.document is None or not self._has_unsaved():
            return True
        name = os.path.basename(self.document.path)
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Warning)
        box.setWindowTitle("Unsaved changes")
        box.setText(f"Do you want to save the changes you made to {name}?")
        box.setInformativeText("Your changes will be lost if you don't save them.")
        save_btn = box.addButton("Save", QMessageBox.AcceptRole)
        box.addButton("Don't Save", QMessageBox.DestructiveRole)
        cancel_btn = box.addButton("Cancel", QMessageBox.RejectRole)
        box.setDefaultButton(save_btn)
        box.setEscapeButton(cancel_btn)
        box.exec()
        clicked = box.clickedButton()
        if clicked is cancel_btn:
            return False
        if clicked is save_btn:
            self.save_pdf()
            return not self._has_unsaved()  # cancelled the save dialog -> stay
        # Don't Save: actually DROP the unsaved changes. Without clearing the
        # dirty state the close guard's ``while any_dirty()`` loop finds the
        # same document still dirty and re-prompts forever -- so "Don't Save"
        # appeared to do nothing. Clear BOTH the model flag and the Qt undo
        # stack's clean index (``_has_unsaved`` reads both).
        self.document.mark_clean()
        self.undo_stack.setClean()
        self._clear_recovery_for(self.document)
        return True

    def _ask_save_all(self, dirty_idx: list) -> str:
        """The consolidated quit prompt when MORE THAN ONE tab is unsaved: lists
        the affected files and offers Save All / Discard All / Review Each /
        Cancel, so quitting with five edited docs is one decision, not five
        modals. Returns one of 'save', 'discard', 'review', 'cancel'."""
        names = [os.path.basename(self.workspace.document(i).path)
                 for i in dirty_idx]
        listed = "\n".join(f"  •  {n}" for n in names)
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Warning)
        box.setWindowTitle("Unsaved changes")
        box.setText(f"You have unsaved changes in {len(names)} documents.")
        box.setInformativeText(
            f"{listed}\n\nSave all of them before quitting? Changes are lost if "
            "you discard them.")
        save_btn = box.addButton("Save All", QMessageBox.AcceptRole)
        discard_btn = box.addButton("Discard All", QMessageBox.DestructiveRole)
        review_btn = box.addButton("Review Each…", QMessageBox.ActionRole)
        cancel_btn = box.addButton("Cancel", QMessageBox.RejectRole)
        box.setDefaultButton(save_btn)
        box.setEscapeButton(cancel_btn)
        box.exec()
        clicked = box.clickedButton()
        if clicked is save_btn:
            return "save"
        if clicked is discard_btn:
            return "discard"
        if clicked is review_btn:
            return "review"
        return "cancel"

    def _guard_dirty_on_close(self) -> bool:
        """Run the close-time dirty guard over EVERY open document. Returns True
        if it is safe to quit (saved/discarded/clean), False if the user
        cancelled. With more than one dirty tab it offers the consolidated
        Save All / Discard All choice first; a single dirty tab (or 'Review
        Each') falls through to the per-file walk."""
        dirty_idx = [i for i in range(self.workspace.count)
                     if self.workspace.document(i).dirty]
        if len(dirty_idx) > 1:
            choice = self._ask_save_all(dirty_idx)
            if choice == "cancel":
                return False
            if choice == "discard":
                for i in dirty_idx:
                    doc = self.workspace.document(i)
                    doc.mark_clean()
                    self._clear_recovery_for(doc)
                self.undo_stack.setClean()
                return True
            if choice == "save":
                for i in dirty_idx:
                    self.workspace.switch(i)
                    self.tab_bar.sync(self.workspace)
                    self.save_pdf()
                    if self.workspace.document(i).dirty:
                        return False   # a save was cancelled/failed -> stay open
                return True
            # 'review' falls through to the per-file walk below.
        # Per-file walk (PAGES_SPEC §6.8): activate each dirty tab for its own
        # discard dialog; also serves the single-dirty case.
        while self.workspace.any_dirty():
            idx = next(i for i in range(self.workspace.count)
                       if self.workspace.document(i).dirty)
            self.workspace.switch(idx)
            self.tab_bar.sync(self.workspace)
            if not self._confirm_discard_if_dirty():
                return False
        return True

    def closeEvent(self, event) -> None:
        if self._suppress_close_guard:
            # Test teardown: skip the dirty guard AND the session write
            # (suites must never touch the real preferences store).
            event.accept()
            return
        if not self._guard_dirty_on_close():
            event.ignore()
            return
        # A clean quit (every doc saved or discarded) leaves nothing to recover,
        # so clear the recovery folder -- a surviving copy then means a crash.
        if self._persist_enabled:
            from . import autosave
            autosave.clear_all()
        # Session snapshot (navigation M3): AFTER the dirty guard passes (the
        # user is really quitting), BEFORE close_all empties the workspace.
        self._save_session()
        self.workspace.close_all()
        event.accept()
