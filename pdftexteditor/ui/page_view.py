"""The editable page canvas (BUILD_SPEC §5 + EDITOR_SPEC §3).

A polished ``QGraphicsView`` that renders the current page as a white sheet with
a soft drop shadow on a gray gutter, lays an accurate transparent hotspot over
every text box, and provides TRUE in-place editing PLUS a full Acrobat-style
selection model: single-click selects a box (outline + 8 resize handles) and
populates the inspector; double-click / Enter drops into the inline text editor;
dragging the body MOVES the box; dragging a corner handle RESIZES it (and scales
the font size); an "Add Text" tool creates new boxes by clicking the page; and
Delete removes the selected box. Every committed edit is BAKED into the page by
``document.render_with_edits`` (the same pipeline as ``save_as``), so what is on
screen is exactly what saves -- for text, style, move, resize, delete, and add.

Two distinct modes (EDITOR_SPEC §3.2), layered so the original text path is
untouched:

  SELECT      default; clicks select boxes, drag moves, handle-drag resizes.
  ADD_TEXT    "Add Text" armed; the next empty-canvas click creates a box.
  TEXT_EDIT   the InlineRunEditor is mounted (the existing, unchanged path).

The fidelity contract (BUILD_SPEC §3/§9): the editor, the baked preview, and the
save writer all call the SAME ``FontEngine`` resolution and the SAME baseline
math, so what you type on screen is what lands in the saved file. An EXISTING
span resolves through ``FontEngine.resolve`` (3-tier, reproduces the original
face); a user-added ``NewBox`` resolves through ``FontEngine.resolve_family``
(always embeddable). The view never re-renders text itself: it owns interaction
and selection chrome only; the model's ``render_with_edits`` draws every box.

Geometry (BUILD_SPEC §5.3), all in one place so editor == overlay == insert:
  z   = self.zoom                       (device-independent)
  dpr = self.devicePixelRatioF()        (retina crispness)
  pixmap rendered at z * dpr, QImage.setDevicePixelRatio(dpr); scene units are
  PDF points * z. A PDF point (px, py) maps to scene (px*z + ox, py*z + oy),
  where (ox, oy) is the sheet's top-left offset in the gutter. ``_scene_point``
  forward-maps a rawdict point; ``_pdf_point`` is its exact inverse (used for
  add / move), de-zooming and de-rotating a scene point back to PDF text space.

Pinned implementer choices (EDITOR_SPEC §9), documented at their decision site:
  * Box list:               spans(page) + new_boxes(page)            (§1.8)
  * Edge handles:           proportional-from-opposite-corner, all 8 (§3.3/§6)
  * New-box baseline:       baseline-at-click, box grows upward       (§3.5)
  * Empty-add cleanup:      view requests "cancel_add"; window pops the add
                            command so a stray click leaves no box      (§3.5)
  * Mutation route:         the view NEVER mutates the model directly; it emits
                            ``boxCommandRequested(kind, box, params)`` and the
                            window funnels it into ONE ``BoxCommand`` on the
                            undo stack (so every box mutation interleaves with
                            text edits on a single history, invariant §0.5).
"""

from __future__ import annotations

import math
import time

import fitz

from PySide6.QtCore import (
    QByteArray,
    QEvent,
    QLineF,
    QMimeData,
    QPointF,
    QRect,
    QRectF,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QFontMetricsF,
    QImage,
    QKeyEvent,
    QKeySequence,
    QPainter,
    QPainterPath,
    QPalette,
    QPen,
    QPixmap,
    QPolygonF,
    QTextBlockFormat,
    QTextCharFormat,
    QTextCursor,
    QTextOption,
    QTextFormat,
    QTransform,
)
from PySide6.QtWidgets import (
    QApplication,
    QGraphicsDropShadowEffect,
    QGraphicsEllipseItem,
    QGraphicsItem,
    QGraphicsLineItem,
    QGraphicsPathItem,
    QGraphicsPixmapItem,
    QGraphicsPolygonItem,
    QGraphicsRectItem,
    QGraphicsScene,
    QGraphicsTextItem,
    QGraphicsView,
    QMenu,
    QStyle,
    QStyleOptionGraphicsItem,
    QToolTip,
)

from ..clipboard import (
    RUNS_MIME,
    decode_runs_mime,
    encode_runs_mime,
    sanitize_pasted_text,
)
from ..document import ImageBox, NewBox, PDFDocument, Span
from ..font_engine import ResolvedFont
from . import theme
from .render_cache import PageRenderCache

# --- Scene z-order (BUILD_SPEC §5.5 + EDITOR_SPEC §3.1) -------------------
# Z-SLOT REGISTRY (perf foundation M4b -- the ONE cross-workstream ledger).
# Reserved slots for overlay items added through register_page_item_factory;
# the constants land WITH their workstreams, this comment is the registry:
#   Z_TEXT_SELECT   = 2   text-select highlight bands   (ws2 M4 -- LANDED)
#   Z_ANNOT_HOTSPOT = 5   annotation hotspots           (ws3 annotations -- LANDED)
#   Z_IMAGE_HOTSPOT = 7   image hotspots                (ws4 images -- LANDED)
#   Z_FORM_HOTSPOT  = 8   form-field hotspots           (ws5 forms -- LANDED;
#                         beats images)
# 11-39 is free between the hotspots and the selection chrome. Every reserved
# hotspot slot sits BELOW Z_HOTSPOT=10, so text hotspots always win
# overlapping clicks; Z_PREVIEW_TEXT already owns 6 -- do not reuse it.
Z_SHADOW = -2
Z_SHEET = -1
Z_PIXMAP = 0
Z_TEXT_SELECT = 2       # text-select accent bands: over the pixmap, under the
                        # edited tint / cover (text-editing UX §5.2 z-slot)
Z_EDITED_TINT = 3
Z_COVER = 4
Z_ANNOT_HOTSPOT = 5     # annotation hotspots (annotations & markup §4.1):
                        # below Z_HOTSPOT, so text hotspots win overlap clicks
Z_PREVIEW_TEXT = 6
Z_IMAGE_HOTSPOT = 7     # placed-image hotspots (images & signatures §3): the
                        # ws1 M4b registry slot -- below the ws5 form slot (8)
                        # and the text hotspots (10), so both win overlaps
Z_FORM_HOTSPOT = 8      # form-field hotspots (forms §3): above the image
                        # slot (7) so a field over a background image wins
                        # the click; still below Z_HOTSPOT, so text wins
Z_HOTSPOT = 10
Z_SELECTION = 40        # selection outline (below the editor, above hotspots)
Z_HANDLE = 41           # individual resize handles (topmost interactive chrome)
Z_DRAG_PREVIEW = 45     # cheap live move/resize affordance during a drag
Z_EDITOR = 50

# Zoom clamp (BUILD_SPEC §5.2 / §6.2).
ZOOM_MIN = 0.25
ZOOM_MAX = 6.0

_MIN_PIXEL_SIZE = 1.0

# Selection-chrome metrics (device-independent; handles stay constant size
# across zoom because they are placed in scene units sized off the zoom factor).
_HANDLE_PX = theme.HANDLE_PX     # handle square edge, device-independent px (PAGES_SPEC §4.2)
_OUTLINE_INFLATE = 2.0           # selection outline grows this far past the box
_MIN_DRAG_PX = 3.0               # press-to-move slop before a drag is a drag
_MIN_RESIZE_DIAG = 1.0           # guard against a zero start diagonal

# Handle ids, clockwise from top-left. Edge handles scale proportionally from
# the opposite CORNER in v1 (pinned, EDITOR_SPEC §3.3/§6), so the box keeps its
# aspect ratio and the font scales uniformly with it.
_HANDLES = ("nw", "n", "ne", "e", "se", "s", "sw", "w")
# The 4 corners are always shown; the edge midpoints are suppressed on a box too
# small to host them without crowding the body (see SelectionOverlay.set_box_rect).
_CORNER_HANDLES = frozenset(("nw", "ne", "se", "sw"))
# Compact-handle mode (text-editing UX §3.3a): a box whose scene rect is
# smaller than 90x28 px gets 4 small corner handles only, pushed OUTSIDE the
# outline by their own radius so they never sit on glyphs.
_COMPACT_MIN_W = 90.0
_COMPACT_MIN_H = 28.0
_COMPACT_HANDLE_PX = 6.0
# Move-drag axis discipline (text-editing UX §3.2): a drag that stays within
# this many DISPLAY POINTS of the box's original x (or y) snaps that component
# to zero, so near-straight drags land perfectly straight.
_AXIS_SNAP_PT = 0.75
# Arrow-key nudge step in display points (Shift = the coarse step) (§3.1).
_NUDGE_PT = 1.0
_NUDGE_SHIFT_PT = 10.0
# Ghost move preview opacity (§3.2): the box's own pixels ride the cursor.
_GHOST_OPACITY = 0.65

# Modes (EDITOR_SPEC §3.2; select_text = text-editing UX §5.2; crop = doc
# tools §2.6).
MODE_SELECT = "select"
MODE_ADD_TEXT = "add_text"
MODE_TEXT_EDIT = "text_edit"
MODE_SELECT_TEXT = "select_text"
# Text-markup tool modes (annotations & markup §4.2): one mode per kind,
# ``"markup_" + kind``; the kind is the model's AnnotSpec.kind. All register
# in ``_mode_handlers`` (perf foundation M4c) -- no inline press branches.
_MARKUP_MODE_PREFIX = "markup_"
MARKUP_KINDS = ("highlight", "underline", "strikeout", "squiggly")
_MARKUP_MODES = tuple(_MARKUP_MODE_PREFIX + k for k in MARKUP_KINDS)
# Sticky-note tool mode (annotations & markup §4.2): one click = one note.
MODE_NOTE = "note"
# Freehand-ink tool mode (§4.2): one press-drag stroke = one ink annot.
MODE_INK = "ink"
# Drawn-shape tool modes (§4.2): one mode per kind, ``"shape_" + kind``; the
# kind is the model's AnnotSpec.kind. Registered in ``_mode_handlers`` like
# the markup modes -- no inline press branches.
_SHAPE_MODE_PREFIX = "shape_"
SHAPE_KINDS = ("rect", "ellipse", "line", "arrow")
_SHAPE_MODES = tuple(_SHAPE_MODE_PREFIX + k for k in SHAPE_KINDS)
# The note anchor square a note-mode click stages, in PDF points (§4.3).
_NOTE_ANCHOR_PT = 18.0
# Drag-band / shape commit floor (§4.3): a release whose band diagonal is
# under this many scene px is a stray click, not a markup gesture.
_MARKUP_MIN_DRAG_PX = 3.0
# Ink point decimation (§4.3): a move under this many PDF points from the
# stroke's last recorded point is skipped (keeps strokes light).
_INK_DECIMATE_PT = 0.7
# A shape release whose TEXT-SPACE diagonal is under this many points is a
# stray click, not a shape (§4.3).
_SHAPE_MIN_DIAG_PT = 3.0
# Arrow-head edge length for the live line/arrow preview, in scene px.
_ARROW_HEAD_PX = 10.0
MODE_CROP = "crop"
# Crop rubber-rect floor (doc tools §2.6): a release whose drag spans less
# than this many PDF points on either side is a stray click, not a crop --
# dropped silently instead of popping the scope dialog over a 0x0 rect.
_CROP_MIN_DRAG_PT = 2.0
# Image placement mode (images & signatures §3): armed with a payload
# ({"image": bytes, "natural_px": (w, h), "kind": str}); click places at the
# default size, press-drag rubber-bands an aspect-locked rect. One click =
# one image: the mode auto-exits after the commit.
MODE_PLACE_IMAGE = "place_image"
# Default placement width (PDF points): the source's pixel width at 96dpi,
# capped at this fraction of the page width (images & signatures §3).
_IMAGE_DEFAULT_PAGE_FRAC = 0.45
# Minimum scene-px edge of a live image resize -- the rect can never invert
# or collapse under the cursor.
_IMAGE_MIN_RESIZE_PX = 8.0
# Word hit slop for the text-select tool (§5.2): a press that misses every
# word rect still anchors on the nearest word center within this many times
# the word's line height.
_WORD_SNAP_LINE_HEIGHTS = 1.2
# Triple-click promote distance (scene px), mirroring the editor's §2.2 rule.
_TRIPLE_CLICK_SLOP_PX = 4.0


class SpanHotspot(QGraphicsRectItem):
    """A transparent overlay over one editable box (Span OR NewBox).

    Hover paints a subtle accent wash plus a 1px accent baseline underline so
    the user can see exactly which box (and which baseline) they are about to
    interact with. In SELECT mode a press routes to the view's selection model
    (single-click selects, double-click edits text); the view owns drag state,
    so the hotspot only forwards the initial press target.
    """

    def __init__(self, rect: QRectF, box, baseline_y: float, view: "PageView"):
        super().__init__(rect)
        self.span = box                    # a Span or a NewBox (duck-typed Box)
        self._view = view
        self._baseline_y = baseline_y      # scene y of the run's baseline
        self._hovered = False
        self._editing = False              # True while THIS box is being edited
        self.setPen(QPen(Qt.NoPen))
        self.setBrush(QBrush(Qt.transparent))
        self.setAcceptHoverEvents(True)
        self.setCursor(Qt.IBeamCursor)
        self.setZValue(Z_HOTSPOT)

    @property
    def box(self):
        """The editable box this hotspot covers (Span or NewBox)."""
        return self.span

    def hoverEnterEvent(self, event):
        self._hovered = True
        self.update()
        super().hoverEnterEvent(event)

    def hoverLeaveEvent(self, event):
        self._hovered = False
        self.update()
        super().hoverLeaveEvent(event)

    def mousePressEvent(self, event):
        # The VIEW owns all press routing (it needs a single drag-state owner
        # for move/resize, EDITOR_SPEC §3.3). Defer to it; it decides whether
        # this press selects, starts a drag, or (on the editing box) is ignored.
        if event.button() == Qt.LeftButton and self._view._on_box_press(
            self, event.scenePos()
        ):
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.LeftButton and self._view._on_box_double_click(
            self, event.scenePos()
        ):
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def paint(self, painter: QPainter, option, widget=None):
        # While THIS box is being edited, draw nothing (the editor is the visual
        # focus). A SELECTED box also draws nothing here (the selection overlay
        # reads cleanly over it).
        if self._editing or self._view._is_selected(self.span):
            return
        edited = self._view.is_unsaved_edit(self.span)
        # An UNSAVED edit carries a PERSISTENT ochre mark on the white page -- a
        # faint tint + a thin baseline underline -- so pending changes stay
        # visible at rest, not only on hover (the in-place edit signature). The
        # mark CLEARS once the edit is saved (the run then reads like untouched
        # text). A run with no pending edit shows only a faint clay wash while
        # hovered.
        if not edited and not self._hovered:
            return
        painter.setRenderHint(QPainter.Antialiasing, True)
        r = self.rect()
        if edited:
            wash = theme.color_edited_hover() if self._hovered \
                else theme.color_edited_tint()
            line = theme.color_edited_underline()
            line_w = 1.5
        else:
            wash = theme.color_accent_hover()
            line = theme.color_accent()
            line_w = 1.0
        painter.setPen(Qt.NoPen)
        painter.setBrush(QBrush(wash))
        painter.drawRoundedRect(r, 2.0, 2.0)
        pen = QPen(line)
        pen.setWidthF(line_w)
        pen.setCosmetic(True)
        painter.setPen(pen)
        y = self._baseline_y - r.top()    # rect-local y of the baseline
        painter.drawLine(QPointF(r.left(), y), QPointF(r.right(), y))


class AnnotHotspot(QGraphicsRectItem):
    """A transparent overlay over ONE annotation (annotations & markup
    §4.1): a staged spec or a pre-existing file annot, identified by its
    record's identity. Sits at Z_ANNOT_HOTSPOT (5), BELOW the text hotspots
    (10), so click-to-edit text always wins overlapping presses; Alt+click
    in the view's press routing forces annot-first hit-testing. Built by the
    view's registered page-item factory (perf foundation M4b) and freed by
    ``_dematerialize_page`` via ``layer.extra_items``. The view owns all
    selection/drag state -- the hotspot only forwards its presses."""

    def __init__(self, rect: QRectF, record, view: "PageView"):
        super().__init__(rect)
        self.record = record               # the AnnotRecord this item covers
        self.identity = record.identity
        self.page_index = record.identity[0]
        self._view = view
        self.setPen(QPen(Qt.NoPen))
        self.setBrush(QBrush(Qt.transparent))
        self.setZValue(Z_ANNOT_HOTSPOT)
        self.setCursor(Qt.PointingHandCursor)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton and self._view._on_annot_press(
            self, event.scenePos()
        ):
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.LeftButton and \
                self._view._on_annot_double_click(self, event.scenePos()):
            event.accept()
            return
        super().mouseDoubleClickEvent(event)


class ImageHotspot(QGraphicsRectItem):
    """A transparent overlay over one PLACED IMAGE (images & signatures §3).

    Sits at Z_IMAGE_HOTSPOT (7), below the text hotspots (10) so
    click-to-edit text always wins overlapping presses. Unlike the
    annotation/form hotspots it is NOT built by the ws1 page-item factory:
    images are a BOX kind, so the hotspot lives in the page's ``hotspots`` /
    ``boxes`` identity machinery (selection re-binding, ``repaint_box``)
    exactly like SpanHotspot -- the conflict-ledger decision documented in
    ws4 §3. The view owns all selection/drag state; presses route through
    the same ``_on_box_press`` every box uses."""

    def __init__(self, rect: QRectF, box, view: "PageView"):
        super().__init__(rect)
        self.span = box                    # SpanHotspot duck-typing alias
        self._view = view
        self.setPen(QPen(Qt.NoPen))
        self.setBrush(QBrush(Qt.transparent))
        self.setZValue(Z_IMAGE_HOTSPOT)
        self.setCursor(Qt.SizeAllCursor)

    @property
    def box(self):
        """The ImageBox this hotspot covers (the SpanHotspot contract)."""
        return self.span

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton and self._view._on_box_press(
            self, event.scenePos()
        ):
            event.accept()
            return
        super().mousePressEvent(event)


class AnnotSelectionOverlay(QGraphicsRectItem):
    """The lightweight dashed accent outline over the SELECTED annotation
    (annotations & markup §4.3): no handles, no font scaling -- an annot
    moves whole or not at all. Never hit-tested (NoButton), so presses fall
    through to the AnnotHotspot beneath it; the view repositions the rect
    live during a move drag."""

    def __init__(self, rect: QRectF):
        super().__init__(rect)
        pen = QPen(theme.color_accent())
        pen.setStyle(Qt.DashLine)
        pen.setWidthF(1.4)
        pen.setCosmetic(True)
        self.setPen(pen)
        self.setBrush(QBrush(Qt.transparent))
        self.setZValue(Z_SELECTION)
        self.setAcceptedMouseButtons(Qt.NoButton)


class FieldHotspot(QGraphicsRectItem):
    """An overlay over ONE fillable AcroForm widget (forms §3). Unlike the
    transparent text/annot hotspots it ALWAYS paints a faint accent wash so
    fillable fields are discoverable at a glance (Acrobat-style), a stronger
    wash on hover, and a 1.5px accent outline while its field is the view's
    ``_focused_field``. Sits at Z_FORM_HOTSPOT (8): below the text hotspots
    (10) so click-to-edit text always wins overlapping presses, above the
    reserved image slot (7). Built by the registered page-item factory (perf
    foundation M4b) and freed by ``_dematerialize_page`` via
    ``layer.extra_items``; readonly/button/signature/listbox widgets get no
    hotspot at all (``FormField.fillable``). The view owns all fill routing
    -- the hotspot only forwards its presses."""

    def __init__(self, rect: QRectF, field, view: "PageView"):
        super().__init__(rect)
        self.field = field                 # the model FormField (frozen)
        self.identity = field.identity     # per-widget (radio kids differ)
        self.page_index = field.page_index
        self._view = view
        self._hovered = False
        self._editing = False              # True while ITS editor is mounted
        self.setPen(QPen(Qt.NoPen))
        self.setBrush(QBrush(Qt.transparent))
        self.setAcceptHoverEvents(True)
        self.setZValue(Z_FORM_HOTSPOT)
        self.setCursor(Qt.IBeamCursor if field.kind == "text"
                       else Qt.PointingHandCursor)

    def hoverEnterEvent(self, event):
        self._hovered = True
        self.update()
        super().hoverEnterEvent(event)

    def hoverLeaveEvent(self, event):
        self._hovered = False
        self.update()
        super().hoverLeaveEvent(event)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton and self._view._on_field_press(
            self, event.scenePos()
        ):
            event.accept()
            return
        super().mousePressEvent(event)

    def paint(self, painter: QPainter, option, widget=None):
        # While THIS field's inline editor is mounted, draw nothing (the
        # editor + its white cover are the visual focus).
        if self._editing:
            return
        painter.setRenderHint(QPainter.Antialiasing, True)
        wash = QColor(theme.color_accent())
        wash.setAlpha(36 if self._hovered else 18)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QBrush(wash))
        painter.drawRect(self.rect())
        focused = self._view._focused_field
        if focused is not None and \
                getattr(focused, "identity", None) == self.identity:
            pen = QPen(theme.color_accent())
            pen.setWidthF(1.5)
            pen.setCosmetic(True)
            painter.setPen(pen)
            painter.setBrush(Qt.NoBrush)
            painter.drawRect(self.rect())


class _ResizeHandle(QGraphicsRectItem):
    """One of the 8 resize handles drawn on a selected box (EDITOR_SPEC §3.1).

    A small filled accent square centered on a corner/edge midpoint of the
    box's scene rect. Hit-tested topmost (Z_HANDLE) so a press over a handle
    starts a resize before the body's move. The view owns the drag, so the
    handle only forwards its press with its id."""

    def __init__(self, handle_id: str, view: "PageView"):
        super().__init__(QRectF(-_HANDLE_PX / 2, -_HANDLE_PX / 2,
                                _HANDLE_PX, _HANDLE_PX))
        self.handle_id = handle_id
        self._view = view
        self.setZValue(Z_HANDLE)
        self.setBrush(QBrush(theme.color_accent()))
        pen = QPen(QColor("#FFFFFF"))
        pen.setWidthF(1.0)
        pen.setCosmetic(True)
        self.setPen(pen)
        # Render at a CONSTANT device pixel size regardless of view zoom: the
        # local rect (_HANDLE_PX) is interpreted in device pixels, while setPos
        # still anchors it at the box's scene corner. Previously the square was in
        # scene units, so it ballooned at high zoom and (worse) crowded a small
        # box at low zoom -- making move-vs-resize hit targets ambiguous.
        self.setFlag(QGraphicsItem.ItemIgnoresTransformations, True)
        self.setCursor(_HANDLE_CURSORS.get(handle_id, Qt.SizeAllCursor))

    def set_compact(self, compact: bool) -> None:
        """Resize the handle square for compact mode (text-editing UX §3.3a):
        6px on a small box (so 4 of them fit around it without crowding),
        the full _HANDLE_PX otherwise. Centered on the local origin either
        way; the overlay owns WHERE that origin sits."""
        size = _COMPACT_HANDLE_PX if compact else _HANDLE_PX
        if abs(self.rect().width() - size) < 0.01:
            return
        self.prepareGeometryChange()
        self.setRect(QRectF(-size / 2.0, -size / 2.0, size, size))

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton and self._view._on_handle_press(
            self.handle_id, event.scenePos()
        ):
            event.accept()
            return
        super().mousePressEvent(event)


# Per-handle resize cursors (EDITOR_SPEC §6: hit-test order handles before body).
_HANDLE_CURSORS = {
    "nw": Qt.SizeFDiagCursor, "se": Qt.SizeFDiagCursor,
    "ne": Qt.SizeBDiagCursor, "sw": Qt.SizeBDiagCursor,
    "n": Qt.SizeVerCursor, "s": Qt.SizeVerCursor,
    "e": Qt.SizeHorCursor, "w": Qt.SizeHorCursor,
}


class InlineRunEditor(QGraphicsTextItem):
    """The in-scene inline editor for one box (BUILD_SPEC §5.4, primary path).

    Unchanged in behavior from the text-only editor: a ``QGraphicsTextItem`` in
    text-editor interaction mode, positioned at the box's baseline in the
    resolved font/size/color so it is indistinguishable from set type. Return/
    Enter commit (never insert a newline), Esc cancels, Cmd/Ctrl+A selects the
    run. Selection wraps AROUND this path (EDITOR_SPEC §3); the keys here take
    precedence while TEXT_EDIT is active, so Delete/Backspace edit text and do
    NOT delete the box.
    """

    def __init__(self, text: str, view: "PageView"):
        super().__init__(text)
        self._view = view
        self._cancelled = False
        # The last double-click, as (monotonic seconds, scene QPointF): a third
        # press within the platform double-click interval and <= 4 scene px
        # promotes word-selection into LINE-selection (triple-click, §2.2).
        self._last_double: tuple[float, QPointF] | None = None
        self.setZValue(Z_EDITOR)
        self.setTextInteractionFlags(Qt.TextEditorInteraction)
        self.document().setDocumentMargin(0)
        self.setFlag(QGraphicsItem.ItemIsFocusable, True)
        # A 2px caret so the insertion point is findable at a glance (§2.3).
        # "cursorWidth" on the document layout is the documented Qt mechanism
        # (QTextEdit.setCursorWidth writes the same property; the text control
        # reads it when painting the caret). Probe-verified offscreen.
        self.document().documentLayout().setProperty("cursorWidth", 2)

    def keyPressEvent(self, event: QKeyEvent):
        key = event.key()
        if key in (Qt.Key_Return, Qt.Key_Enter):
            event.accept()
            self._view.commit_edit()
            return
        if key == Qt.Key_Escape:
            event.accept()
            self._cancelled = True
            self._view.cancel_edit()
            return
        # Clipboard interop (§4.2/§4.3): Copy writes BOTH text/plain and the
        # x-pdfte-runs payload; Paste inserts our runs (bold/italic only) or
        # whitespace-normalized plain text. ALL THREE are always handled here
        # -- Qt's native editor clipboard must never run (its copy puts rich
        # HTML on the clipboard; its paste imports foreign fonts/sizes). The
        # window's act_cut/act_copy/act_paste route to the same view helpers,
        # so behavior is identical whether the shortcut map or this focused
        # item wins the key (the §2.4b convergence rule).
        if event.matches(QKeySequence.Copy):
            event.accept()
            self._view.editor_copy_selection()
            return
        if event.matches(QKeySequence.Cut):
            event.accept()
            self._view.editor_cut_selection()
            return
        if event.matches(QKeySequence.Paste):
            event.accept()
            self._view.editor_paste()
            return
        if key == Qt.Key_A and (event.modifiers() & (Qt.ControlModifier
                                                      | Qt.MetaModifier)):
            event.accept()
            cursor = self.textCursor()
            cursor.select(QTextCursor.Document)
            self.setTextCursor(cursor)
            return
        if key in (Qt.Key_B, Qt.Key_I) and (event.modifiers()
                                            & (Qt.ControlModifier
                                               | Qt.MetaModifier)):
            # Cmd/Ctrl+B / +I inside the editor (§2.4a): toggle the selection's
            # weight/slant via the view's per-run styling path. The toggle
            # target is read from the cursor's char format (falling back to the
            # editor's base font), so repeated presses alternate. When the view
            # declines (a NewBox / uniform-only editor), fall through unhandled
            # so the WINDOW action takes the whole-box route -- this keypath
            # stays correct whether the shortcut map or the focus item wins.
            prop = "bold" if key == Qt.Key_B else "italic"
            if self._view.apply_style_to_selection(
                    {prop: not self._cursor_style_flag(prop)}):
                event.accept()
                return
            event.ignore()
            return
        super().keyPressEvent(event)

    def _cursor_style_flag(self, prop: str) -> bool:
        """The current bold/italic state at the cursor (selection-aware): the
        char format's explicit property when set, else the editor's base font.
        This is the value Cmd+B / Cmd+I toggles FROM (§2.4a)."""
        fmt = self.textCursor().charFormat()
        if prop == "bold":
            if fmt.hasProperty(QTextFormat.FontWeight):
                return fmt.fontWeight() >= QFont.DemiBold
            return self.font().bold()
        if fmt.hasProperty(QTextFormat.FontItalic):
            return fmt.fontItalic()
        return self.font().italic()

    def mousePressEvent(self, event):
        # Triple-click = visual line (§2.2): a left press right after a
        # double-click (within the platform interval, <= 4 scene px) selects
        # the wrapped visual line under the click instead of placing a caret.
        if event.button() == Qt.LeftButton and self._last_double is not None:
            t0, p0 = self._last_double
            interval = QApplication.doubleClickInterval() / 1000.0
            pos = event.scenePos()
            if (time.monotonic() - t0 <= interval
                    and math.hypot(pos.x() - p0.x(), pos.y() - p0.y()) <= 4.0):
                self._last_double = None
                self._select_line_at(pos)
                event.accept()
                return
        self._last_double = None
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event):
        # Double-click = word (§2.2). Recorded so a third press in the same
        # spot promotes to line selection (see mousePressEvent).
        if event.button() == Qt.LeftButton:
            pos = event.scenePos()
            self._select_word_at(pos)
            self._last_double = (time.monotonic(), QPointF(pos))
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def _select_word_at(self, scene_pt: QPointF) -> None:
        """Select the word under a scene point (double-click semantics §2.2).
        Exposed as a method so tests drive it without event synthesis."""
        cursor = self.textCursor()
        cursor.setPosition(self.caret_index_at_scene_point(scene_pt))
        cursor.select(QTextCursor.WordUnderCursor)
        self.setTextCursor(cursor)

    def _select_line_at(self, scene_pt: QPointF) -> None:
        """Select the visual (wrapped) line under a scene point (triple-click
        semantics §2.2; LineUnderCursor selects the wrapped line in a
        paragraph editor -- probe-verified)."""
        cursor = self.textCursor()
        cursor.setPosition(self.caret_index_at_scene_point(scene_pt))
        cursor.select(QTextCursor.LineUnderCursor)
        self.setTextCursor(cursor)

    def paint(self, painter: QPainter, option, widget=None):
        # Themed selection (§2.3): the accent band at ~.43 alpha with the
        # glyphs kept in the editor's own ink color -- Qt's default flips
        # selected text to white, which reads as a foreign widget on the page.
        opt = QStyleOptionGraphicsItem(option)
        pal = QPalette(opt.palette)
        pal.setColor(QPalette.Highlight, theme.color_editor_selection())
        pal.setColor(QPalette.HighlightedText, self.defaultTextColor())
        # The blinking insertion caret is the clay accent (QTextControl paints the
        # cursor in the palette's Text role). Glyphs keep the document's own ink
        # via defaultTextColor()/char formats, so only the caret turns clay.
        pal.setColor(QPalette.Text, theme.color_accent())
        opt.palette = pal
        # Suppress Qt's default dotted focus rectangle. It hugs the text item's
        # OWN bounds, which are tighter than the box's selection outline -- so
        # the box appeared to shrink the moment you clicked in to edit. The box
        # boundary is now shown by the persisted selection outline (begin_edit
        # keeps it visible), at the SAME rect whether selected or editing.
        opt.state &= ~QStyle.State_HasFocus
        super().paint(painter, opt, widget)

    def focusOutEvent(self, event):
        # Capture the selection BEFORE Qt processes the focus-out (which clears
        # the visible selection) so the style path can restore the user's
        # highlight after applying a whole-box font/size/colour change.
        cur = self.textCursor()
        self._view._stash_editor_selection(cur.anchor(), cur.position())
        super().focusOutEvent(event)
        if self._cancelled:
            return
        # A click that moved focus into the Format panel (font / size / B I U S),
        # or a modal style dialog opening from it (the colour picker), must NOT
        # commit -- committing tears the editor down and drops the highlight
        # before the style lands. Keep editing; the style path applies the change
        # and restores the selection.
        if QApplication.activeModalWidget() is not None or \
                self._view._focus_moved_to_format_panel():
            return
        self._view.commit_edit(from_focus_out=True)

    def caret_index_at_scene_x(self, scene_x: float) -> int:
        """Caret insertion index nearest a scene x, by cumulative advance.

        DEMOTED (text-editing UX §2.1): this O(n^2) advance walk is no longer a
        caret-placement path of its own -- it survives only as the exception
        fallback inside ``caret_index_at_scene_point`` (the native document
        layout hit-test, exact on one line and on wrapped paragraphs alike)."""
        text = self.toPlainText()
        if not text:
            return 0
        metrics = QFontMetricsF(self.font())
        local_x = scene_x - self.pos().x()
        if local_x <= 0:
            return 0
        best_idx, best_dist = 0, abs(local_x)
        for i in range(1, len(text) + 1):
            advance = metrics.horizontalAdvance(text[:i])
            dist = abs(local_x - advance)
            if dist < best_dist:
                best_idx, best_dist = i, dist
        return best_idx

    def caret_index_at_scene_point(self, scene_pt: QPointF) -> int:
        """Caret insertion index nearest a scene POINT, via Qt's native document
        layout hit-test (REFLOW_SPEC §R3.7). THE caret-placement path for every
        editor (text-editing UX §2.1) -- exact on a single line and on any
        wrapped line of a ParagraphBox. ``mapFromScene`` (not a bare pos()
        subtraction) folds the item rotation in, so the hit-test is also right
        on a rotated page's editor. Falls back to 0 on an empty document and to
        the cumulative-advance walk on a failed hit-test."""
        text = self.toPlainText()
        if not text:
            return 0
        try:
            local = self.mapFromScene(scene_pt)
            layout = self.document().documentLayout()
            pos = layout.hitTest(QPointF(local), Qt.FuzzyHit)
        except Exception:  # noqa: BLE001 - never let a hit-test crash editing
            return self.caret_index_at_scene_x(scene_pt.x())
        if pos < 0:
            return 0
        return min(pos, len(text))


class FormFieldEditor(QGraphicsTextItem):
    """The in-scene inline editor for ONE form text field (forms §3).

    Plain text only -- MuPDF regenerates the widget's appearance from the
    committed string via ``widget.update()``, so no rich styling can survive
    a fill; paste is intercepted to insert the clipboard's plain text. Keys:
    Return commits (on a MULTILINE field Return inserts a newline and
    Cmd/Ctrl+Return commits), Esc cancels, focus-out commits, Cmd/Ctrl+A
    selects all. The VIEW owns the commit/cancel lifecycle (the editor only
    routes its keys), mirrors of ``InlineRunEditor``'s contract."""

    def __init__(self, field, text: str, view: "PageView"):
        super().__init__(text)
        self.field = field                 # the model FormField (frozen)
        self._view = view
        self._cancelled = False
        self.setZValue(Z_EDITOR)
        self.setTextInteractionFlags(Qt.TextEditorInteraction)
        self.document().setDocumentMargin(0)
        self.setFlag(QGraphicsItem.ItemIsFocusable, True)
        # Tab NAVIGATES, never types (forms §3, M3): without this,
        # QGraphicsTextItem.sceneEvent special-cases Tab/Backtab straight
        # into the text control (a literal "\t", bypassing keyPressEvent
        # entirely -- probe-verified). With it, the declined key falls
        # through to the view, whose focusNextPrevChild hands it to
        # ``form_tab_step`` -- which commits THIS editor first, so the
        # mid-edit Tab is one command then a focus move.
        self.setTabChangesFocus(True)
        # The same findable 2px caret the text editor uses (§2.3 precedent).
        self.document().documentLayout().setProperty("cursorWidth", 2)

    def keyPressEvent(self, event: QKeyEvent):
        key = event.key()
        cmd = bool(event.modifiers() & (Qt.ControlModifier | Qt.MetaModifier))
        if key in (Qt.Key_Return, Qt.Key_Enter):
            if self.field.multiline and not cmd:
                super().keyPressEvent(event)   # newline inside the value
                return
            event.accept()
            self._view.commit_form_editor()
            return
        if key == Qt.Key_Escape:
            event.accept()
            self._cancelled = True
            self._view.cancel_form_editor()
            return
        if event.matches(QKeySequence.Paste):
            # Plain text only: Qt's native paste imports rich formatting,
            # which a form value can never carry.
            event.accept()
            md = QApplication.clipboard().mimeData()
            if md is not None and md.hasText():
                self.textCursor().insertText(md.text())
            return
        if key == Qt.Key_A and cmd:
            event.accept()
            cursor = self.textCursor()
            cursor.select(QTextCursor.Document)
            self.setTextCursor(cursor)
            return
        super().keyPressEvent(event)

    def focusOutEvent(self, event):
        super().focusOutEvent(event)
        if not self._cancelled:
            self._view.commit_form_editor()


class SelectionOverlay(QGraphicsItem):
    """The selection outline + 8 resize handles for the selected box.

    Drawn as scene items so it tracks pan/zoom automatically. The outline is a
    1.5px cosmetic accent rect just OUTSIDE the box's scene rect; the 8 handles
    (NW, N, NE, E, SE, S, SW, W) are small filled accent squares centered on the
    scene rect's corners / edge midpoints (EDITOR_SPEC §3.1). It is rebuilt and
    repositioned whenever the selection, zoom, or page changes; it is
    rotation-aware because it is built from the box's already rotation-aware
    scene rect (``_span_scene_rect``)."""

    def __init__(self, view: "PageView"):
        super().__init__()
        self._view = view
        self._rect = QRectF()
        self.setZValue(Z_SELECTION)
        self._handles: list[_ResizeHandle] = []
        self._visible = True        # whether the overlay's handles are shown
        self._edge_ok = True        # whether the box is large enough for edges
        self._overflow = False      # reflowed text grew past the box bottom
        # Compact mode (text-editing UX §3.3a): True while the box's scene
        # rect is under 90x28 px -- 4 small corner handles only, offset
        # OUTSIDE the outline so they never sit on glyphs. Public for tests.
        self.compact = False

    def attach_handles(self, scene) -> None:
        """Create the 8 handle items (separate scene items, each with its own
        cursor + press target). Called once when the overlay is added."""
        for hid in _HANDLES:
            h = _ResizeHandle(hid, self._view)
            scene.addItem(h)
            self._handles.append(h)

    def detach_handles(self, scene) -> None:
        for h in self._handles:
            if h.scene() is not None:
                scene.removeItem(h)
        self._handles = []

    def set_box_rect(self, rect: QRectF) -> None:
        """Position the outline + handles for the box's current scene rect.

        Compact mode (text-editing UX §3.3a): a box under 90x28 scene px gets
        ONLY the 4 corner handles, shrunk to 6px and pushed OUTSIDE the
        outline corner by their own radius -- so a one-word span's handles
        never sit on its glyphs and the body stays grabbable for a MOVE.
        A larger box keeps all 8 full-size handles centered on the rect's
        corners/edge midpoints (edge midpoints still need ~2*_HANDLE_PX of
        clear span on their axis or they collide with the corners)."""
        self.prepareGeometryChange()
        self._rect = QRectF(rect)
        self.setPos(0, 0)
        self.compact = (rect.width() < _COMPACT_MIN_W
                        or rect.height() < _COMPACT_MIN_H)
        self._edge_ok = (not self.compact
                         and rect.width() >= 2.0 * _HANDLE_PX
                         and rect.height() >= 2.0 * _HANDLE_PX)
        for h in self._handles:
            h.set_compact(self.compact)
            h.setPos(self._handle_pos(rect, h.handle_id))
            if self._visible:
                h.setVisible(self._edge_ok or h.handle_id in _CORNER_HANDLES)
        self.update()

    def _handle_pos(self, rect: QRectF, handle_id: str) -> QPointF:
        """Where ``handle_id``'s handle is CENTERED for ``rect``. Normal mode:
        on the rect corner / edge midpoint (the geometry the resize math also
        anchors on). Compact mode: the OUTLINE corner pushed outward
        diagonally by the handle radius, so the square clears the glyphs."""
        if not self.compact:
            return PageView._handle_scene_point(rect, handle_id)
        out = rect.adjusted(-_OUTLINE_INFLATE, -_OUTLINE_INFLATE,
                            _OUTLINE_INFLATE, _OUTLINE_INFLATE)
        r = _COMPACT_HANDLE_PX / 2.0
        pts = {
            "nw": QPointF(out.left() - r, out.top() - r),
            "ne": QPointF(out.right() + r, out.top() - r),
            "se": QPointF(out.right() + r, out.bottom() + r),
            "sw": QPointF(out.left() - r, out.bottom() + r),
        }
        return pts.get(handle_id,
                       PageView._handle_scene_point(rect, handle_id))

    def set_handles_visible(self, visible: bool) -> None:
        self._visible = visible
        for h in self._handles:
            h.setVisible(visible
                         and (self._edge_ok or h.handle_id in _CORNER_HANDLES))

    def set_overflow(self, overflow: bool) -> None:
        """Flag that the selected paragraph's reflowed text grew past its box
        bottom (REFLOW_SPEC §R2.5): paint a danger-colored bottom edge so the
        collision with the content below is never silent."""
        if self._overflow == overflow:
            return
        self._overflow = overflow
        self.update()

    def boundingRect(self) -> QRectF:
        return self._rect.adjusted(-_OUTLINE_INFLATE - 2, -_OUTLINE_INFLATE - 2,
                                   _OUTLINE_INFLATE + 2, _OUTLINE_INFLATE + 2)

    def paint(self, painter: QPainter, option, widget=None):
        if self._rect.isNull():
            return
        painter.setRenderHint(QPainter.Antialiasing, True)
        outline = self._rect.adjusted(
            -_OUTLINE_INFLATE, -_OUTLINE_INFLATE,
            _OUTLINE_INFLATE, _OUTLINE_INFLATE)
        # A light clay selection BAND so the selected line reads as the kit's
        # selection (a calm wash, never a heavy box) -- accent_selection ~= .22,
        # strong enough to register yet still translucent over the text. The
        # resize handles + outline below stay (this is a true box editor).
        painter.setPen(Qt.NoPen)
        painter.setBrush(QBrush(theme.color_accent_selection()))
        painter.drawRect(outline)
        # Two-pass outline (PAGES_SPEC §4.2/§5.7): a white HALO under-stroke so
        # the outline reads on both light and dark page regions, then the accent
        # pen on top. Both cosmetic so the width is zoom-independent.
        halo = QPen(theme.color_selection_halo())
        halo.setWidthF(theme.SELECTION_OUTLINE_W + 1.5)
        halo.setCosmetic(True)
        painter.setPen(halo)
        painter.setBrush(Qt.NoBrush)
        painter.drawRect(outline)
        pen = QPen(theme.color_accent())
        pen.setWidthF(theme.SELECTION_OUTLINE_W)
        pen.setCosmetic(True)
        painter.setPen(pen)
        painter.drawRect(outline)
        # Overflow cue: a thick danger-red stroke along the box BOTTOM when the
        # reflowed text grew past the original box bottom and now draws over the
        # content beneath it (REFLOW_SPEC §R2.5). Drawn last so it sits on top of
        # the accent outline; cosmetic so its weight is zoom-independent.
        if self._overflow:
            danger = QPen(theme.color_danger())
            danger.setWidthF(theme.SELECTION_OUTLINE_W + 1.5)
            danger.setCosmetic(True)
            painter.setPen(danger)
            painter.drawLine(QPointF(outline.left(), outline.bottom()),
                             QPointF(outline.right(), outline.bottom()))


class _ImageSelectionOverlay(SelectionOverlay):
    """Selection chrome for a PLACED IMAGE (images & signatures §3): the
    same accent outline + 8 constant-device-pixel handles as a text box,
    but the handles mean RECT resize -- corner drag = free resize, Shift =
    keep aspect, edge drag = one axis. Those semantics live in the view's
    image-resize drag (``_begin_image_resize`` routes here instead of the
    proportional font-scale resize); this subclass exists so the press
    routing and tests can distinguish the chrome, and so the paragraph
    overflow cue can never paint on an image."""

    def set_overflow(self, overflow: bool) -> None:
        super().set_overflow(False)        # images never overflow


# How many pages beyond the visible band stay materialized (rendered pixmap +
# hotspots). 1 keeps one page above and below hot so a small scroll never shows a
# blank sheet (REFLOW_SPEC §R3.2).
_BUFFER_PAGES = 1
# Extra eviction slack: a page stays materialized until it is this many pages
# outside the visible band, so a small scroll jitter does not thrash re-renders.
_EVICT_MARGIN_PAGES = 2


class _PageLayer:
    """Per-page scene state for the continuous-scroll view (REFLOW_SPEC §R3.1).

    Holds everything the old single-page state held, indexed by page: the page's
    scene Y offset (``y_top``), its PDF-point size + rotation, the lazily rendered
    pixmap/QImage, the scene items, and the page's editable box list + hotspots.
    A layer record persists for the whole (re)load so the scrollbar extent + total
    scene height stay stable even while a page's pixmap is dematerialized."""

    __slots__ = (
        "page_index", "y_top", "x_left", "pt_size", "rotation", "rotation_matrix",
        "image", "pixmap_item", "sheet_item", "shadow_item", "shadow_item2",
        "placeholder_item",
        "hotspots", "boxes", "rendered", "extra_items",
    )

    def __init__(self, page_index: int):
        self.page_index = page_index
        self.y_top = 0.0                     # scene y of this page's sheet top
        # Scene x of this page's sheet LEFT edge. Every page is CENTERED in the
        # column (scene width == widest page + 2*margin), so a narrower page in a
        # mixed-size document is not flush-left under a wider one (REFLOW_SPEC
        # §R3.1). For a single-size doc this is exactly the margin (offset 0).
        self.x_left = float(theme.SHEET_MARGIN)
        self.pt_size = (0.0, 0.0)            # (w, h) in PDF points
        self.rotation = 0
        self.rotation_matrix = fitz.Matrix(1, 0, 0, 1, 0, 0)
        self.image: QImage | None = None     # lazily rendered (None until visible)
        self.pixmap_item = None
        self.sheet_item = None
        self.shadow_item = None
        self.shadow_item2 = None             # the contact (second) page-shadow layer
        self.placeholder_item = None         # thin sheet outline before render
        self.hotspots: list[SpanHotspot] = []
        self.boxes: list = []                # Span/NewBox/ParagraphBox for the page
        self.rendered = False                # whether the pixmap is materialized
        # Items produced by registered page-item factories (perf foundation
        # M4b): annotation/form hotspots etc. Tracked here so dematerialize
        # removes exactly what the factories added for THIS page.
        self.extra_items: list = []

    @property
    def field_hotspots(self) -> list:
        """The page's live ``FieldHotspot`` items (forms §3) -- a DERIVED
        view over ``extra_items``, so it is in lockstep with materialize/
        dematerialize with zero extra bookkeeping. Empty on a form-free doc
        and on a dematerialized page."""
        return [it for it in self.extra_items if isinstance(it, FieldHotspot)]


class PageView(QGraphicsView):
    """The editable page canvas. See module docstring + BUILD_SPEC §5 +
    EDITOR_SPEC §3.

    Continuous vertical scroll (REFLOW_SPEC §R3): every page is stacked top to
    bottom in ONE scene with a gap between sheets, fit-to-width by default, and
    each page's pixmap is rendered lazily as it scrolls into view (+ a small
    buffer). The public signal/method surface is IDENTICAL to the single-page
    canvas so the window drops in unchanged; ``page_index`` is now DERIVED from
    the viewport center, ``set_page`` SCROLLS instead of swapping, and every
    geometry helper is page-aware (each Box carries its ``page_index``)."""

    # --- Signals (MainWindow connects these) -----------------------------
    # Existing (unchanged): text-edit + nav/zoom.
    editCommitted = Signal(int, object, str)    # (page_index, box, new_text)
    # A commit carrying per-selection RICH styling: payload is
    # {"text": str, "runs": tuple[(text, bold, italic), ...] | None}.
    # runs=None means "now uniform" (clears previously staged runs).
    editCommittedRich = Signal(int, object, object)
    editCancelled = Signal()
    editStarted = Signal(object, object)        # (box, ResolvedFont)
    editFinished = Signal()
    pageChanged = Signal(int)                    # new page_index (0-based)
    zoomChanged = Signal(float)                  # new zoom factor

    # New (EDITOR_SPEC §3.4): selection + box mutation intents.
    selectionChanged = Signal(object)            # the selected Box or None
    multiSelectionChanged = Signal(int)          # count of boxes multi-selected
    groupRequested = Signal(object)              # list[box] to fuse into one
    ungroupRequested = Signal(object)            # a ParagraphBox to split
    boxAdded = Signal(object)                     # the new NewBox
    styleApplied = Signal(object, dict)           # (box, applied overrides)
    geometryChanged = Signal(object)              # (box) after a move/resize
    boxDeleted = Signal(object)                    # the deleted Box
    modeChanged = Signal(str)        # "select"|"add_text"|"text_edit"|
                                     # "select_text"|"markup_<kind>"|"crop"
    # Crop mode release (doc tools §2.6): (page_index, (x0, y0, x1, y1)) --
    # the drawn rect in that page's UNROTATED text space (PDF points), the
    # same space crop_pages takes. The view never mutates the model; the
    # window owns the scope dialog + the structural op.
    cropRectSelected = Signal(int, tuple)
    # Emitted when the selected paragraph's reflowed text grows past (or back
    # within) its box bottom, so the window can post a transient status note
    # ("text overflows box") instead of letting the overprint be silent
    # (REFLOW_SPEC §R2.5). overflow_pt > 0 == overflowing.
    overflowChanged = Signal(float)                # overflow amount in PDF points
    # The single generalized mutation intent. The window builds a BoxCommand
    # from (kind, box, params) and pushes it onto the undo stack, so EVERY box
    # mutation funnels through one QUndoCommand type (invariant §0.5):
    #   kind="style"       box=Box   params={"overrides": {...}}
    #   kind="move"        box=Box   params={"dx":..,"dy":..}
    #   kind="resize"      box=Box   params={"scale":..,"anchor":(x,y)}
    #   kind="delete"      box=Box   params={}
    #   kind="add"         box=None  params={"origin","text","family","size",
    #                                        "color","bold","italic"}
    #   kind="cancel_add"  box=NewBox params={}  -> window pops the add command
    boxCommandRequested = Signal(str, object, dict)
    # The annotation mutation intent (annotations & markup §4.3), the exact
    # mirror of boxCommandRequested: the window builds an AnnotCommand from
    # (kind, ref, params) -- the view NEVER mutates the model.
    #   kind="add"  ref=None  params={"kind","page_index","quads",...}
    annotCommandRequested = Signal(str, object, dict)
    # The annot selection changed: the selected AnnotRecord or None (the
    # mirror of selectionChanged; gates the Delete Annotation action).
    annotSelectionChanged = Signal(object)
    # A note hotspot was double-clicked: the window opens its non-modal
    # NotePopup editor on the record (annotations & markup §5.3).
    noteEditRequested = Signal(object)
    # The form-fill mutation intent (forms §3), the mirror of
    # boxCommandRequested for AcroForm widgets: (FormField, {"value": ...}).
    # The window wraps it in ONE FormFieldCommand -- the view NEVER stages
    # a fill itself.
    formCommandRequested = Signal(object, dict)
    # A transient user-facing note (e.g. "No text under selection"); the
    # window routes it to its toast slot.
    statusMessage = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setScene(QGraphicsScene(self))
        self.setRenderHints(
            QPainter.Antialiasing
            | QPainter.SmoothPixmapTransform
            | QPainter.TextAntialiasing
        )
        self.setBackgroundBrush(QBrush(theme.color_canvas_bg()))
        self.setAlignment(Qt.AlignCenter)
        # The vertical scrollbar is pinned ON (not AsNeeded) so the viewport
        # width is STABLE: under fit-width, an AsNeeded bar flickers on/off as
        # the re-fit nudges the page height past the viewport, and each toggle
        # changes the viewport width by the scrollbar's ~15px, re-fitting and
        # re-toggling forever (a fit-width <-> scrollbar oscillation). A
        # continuous multi-page viewer almost always needs the bar anyway, and
        # the slim dark scrollbar style keeps an idle track unobtrusive.
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOn)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.setTransformationAnchor(QGraphicsView.AnchorViewCenter)
        # The page is ALWAYS horizontally centered: keep the scroll RANGE (so we
        # can position it) but render the bar at zero height and snap any
        # horizontal offset back to the middle. The page can never drift
        # off-centre, and a zoom-in trims both sides equally (the page is held in
        # the centre of the viewing area).
        self._recentering_h = False
        hb = self.horizontalScrollBar()
        hb.setFixedHeight(0)
        hb.valueChanged.connect(self._recenter_h)
        hb.rangeChanged.connect(self._recenter_h)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setFrameShape(QGraphicsView.NoFrame)
        # Themed caret/selection for the inline editor.
        pal = self.palette()
        pal.setColor(QPalette.Highlight, theme.color_accent_selection())
        pal.setColor(QPalette.HighlightedText, QColor(theme.TEXT_PRIMARY))
        pal.setColor(QPalette.Text, QColor(theme.TEXT_PRIMARY))
        self.setPalette(pal)

        self.document: PDFDocument | None = None
        self._page_index = 0
        self._zoom = 1.5
        # Baked-pixmap LRU (perf foundation M2): page pixels keyed by
        # (page_index, zoom*dpr, document.render_signature(page)). Equal
        # signatures guarantee equal render_with_edits pixels, so a hit only
        # ever re-displays bytes the real bake pipeline produced for exactly
        # this staged state. Cleared on every document swap (the key does not
        # encode the document); purged per page by repaint_box.
        self._render_cache = PageRenderCache()
        # Fit the WHOLE page by default so the full document stays visible and
        # scales with the window -- on open and on every resize, the page shrinks
        # to stay fully in view as the window gets smaller (instead of fit-width
        # overflowing the height). Until the user picks a fixed zoom or fit-width.
        self._zoom_mode = "fit_page"            # "fixed"|"fit_page"|"fit_width"

        # --- Continuous-scroll layout (REFLOW_SPEC §R3.1) ----------------
        # One _PageLayer per page, built once per (re)load. All pages stack in a
        # single scene; each layer's pixmap is materialized lazily.
        self._layers: list[_PageLayer] = []
        # The widest page in scene px (set in reload), used to center each page.
        self._max_w_scene = 0.0
        self._page_gap = float(getattr(theme, "PAGE_GAP", 18))
        # Coalesce scroll storms: a 0-timer recomputes lazy render + current page
        # once per event loop turn instead of on every scrollbar tick.
        self._scroll_timer = QTimer(self)
        self._scroll_timer.setSingleShot(True)
        self._scroll_timer.setInterval(0)
        self._scroll_timer.timeout.connect(self._on_scroll_settled)
        vbar = self.verticalScrollBar()
        if vbar is not None:
            vbar.valueChanged.connect(self._schedule_scroll_update)
        # True while a zoom relayout is in flight: _update_lazy_render no-ops
        # so transient scroll offsets never bake off-screen pages (see
        # set_zoom).
        self._suspend_lazy = False

        # Gesture zoom (navigation M3): pinch + Cmd+wheel both funnel into
        # _apply_gesture_zoom, which clamps and THROTTLES -- every set_zoom is
        # a full reload, so a 120 ms single-shot applies only the LATEST
        # target instead of relayouting on every gesture tick.
        self._gesture_target: float | None = None
        self._gesture_timer = QTimer(self)
        self._gesture_timer.setSingleShot(True)
        self._gesture_timer.setInterval(120)
        self._gesture_timer.timeout.connect(self._flush_gesture_zoom)
        # Window-resize re-fit THROTTLE (same pain point as gesture zoom): a
        # live window drag fires resizeEvent many times a second, and re-fitting
        # does a full reload() each time, so the page re-rasterized on every
        # frame and the drag stuttered. The fit is now deferred to a short
        # single-shot timer, so the heavy re-render runs ONCE when the drag
        # settles; the page keeps its current raster during the drag (smooth).
        self._resize_timer = QTimer(self)
        self._resize_timer.setSingleShot(True)
        self._resize_timer.setInterval(90)
        self._resize_timer.timeout.connect(self._apply_fit_after_resize)
        # Pinch accumulator, seeded with the live zoom on BeginNativeGesture.
        self._pinch_factor = self._zoom

        # Current-page state mirrors the active page (derived from the viewport
        # center on scroll). These five attributes describe the CURRENT page and
        # back the page-relative geometry helpers + the existing tests, which read
        # ``_sheet_origin`` / ``_page_image`` for page 0 (REFLOW_SPEC §R3.5).
        self._sheet_origin = QPointF(theme.SHEET_MARGIN, theme.SHEET_MARGIN)
        self._page_pt_size = (0.0, 0.0)         # current page size in PDF points
        self._page_image: QImage | None = None  # current page rendered image
        self._page_rotation = 0
        self._rotation_matrix = fitz.Matrix(1, 0, 0, 1, 0, 0)

        self._hotspots: list[SpanHotspot] = []  # current page's hotspots (alias)
        self._boxes: list = []                  # current page's boxes (alias)
        self._editor: InlineRunEditor | None = None
        self._editor_box = None                 # Span or NewBox under the editor
        self._editor_cover = None
        self._editor_hotspot: SpanHotspot | None = None
        self._editor_multiline = False          # True for a ParagraphBox editor
        self._committing = False
        self._closing_editor = False
        # The Format/Inspector panel (registered by the window). A click that
        # moves focus INTO it must not commit the inline editor (styling
        # highlighted text would otherwise tear the editor down and drop the
        # highlight); the editor's focus-out consults this. The saved selection
        # is captured at that focus-out so the style path can restore it.
        self._format_panel = None
        self._editor_saved_selection: tuple[int, int] | None = None
        # Live WYSIWYG re-resolve state: the editor font is re-resolved as the
        # text changes so it always shows the font the COMMIT will bake (an
        # embedded SUBSET that stops covering the typed glyphs falls back to a
        # substitute -- show that immediately instead of surprising the user on
        # exit). Cached from begin_edit; single-line editors only.
        self._editor_eff: dict | None = None
        self._editor_page: int | None = None
        self._editor_pixel_size: float = 0.0
        self._reresolving = False

        # --- Form fill state (forms §3) -----------------------------------
        # The inline FORM editor lives in its own slot beside the text
        # editor (they are mutually exclusive in practice -- mounting either
        # flushes the other -- but the lifecycles never share state).
        # ``_flush_editor`` commits BOTH, so every existing flush call site
        # (zoom/reload/page-jump/mode-arm) covers a half-typed fill too.
        self._form_editor: FormFieldEditor | None = None
        self._form_editor_cover = None          # solid white rect at Z_COVER
        self._form_editor_hotspot: FieldHotspot | None = None
        self._form_editor_seed = ""             # mount-time text (commit diff)
        self._form_committing = False
        self._closing_form_editor = False
        # The focused field (the hotspot outline affordance), a model
        # FormField or None. Identity-compared by the hotspots, so it
        # survives the per-mutation hotspot rebuilds.
        self._focused_field = None
        # Injectable combo-option picker (forms §3; the dialog-seam rule):
        # ``provider(field, scene_rect) -> str | None``. The default pops a
        # QMenu at the hotspot; offscreen tests replace it so no menu ever
        # exec()s.
        self._combo_menu_provider = self._show_combo_menu

        # In-process clipboard for copy/paste-with-formatting (REFLOW_SPEC §R5.3).
        self._clipboard: dict | None = None

        # --- Selection model (EDITOR_SPEC §3) ----------------------------
        self._mode = MODE_SELECT
        self._selection = None                  # the PRIMARY selected Box, or None
        # Multi-object selection (manual group/ungroup): Shift+click adds boxes
        # beyond the primary into ``_multi_extra``; each gets a handle-less
        # outline in ``_multi_overlays``. selected_boxes() returns the whole set.
        self._multi_extra: list = []
        self._multi_overlays: list = []
        # Tool-mode dispatch (perf foundation M4c): per-mode press/key
        # handlers keyed by the RAW ``self._mode`` string. New canvas modes
        # (markup, ink, shapes, forms, ...) register via
        # ``register_mode_handlers`` instead of adding inline branches to
        # mousePressEvent/keyPressEvent. A "press" handler returns True when
        # the event is fully handled (accepted, no super() routing); a "key"
        # handler returns True when the key was consumed.
        self._mode_handlers: dict[str, dict] = {
            MODE_SELECT: {"press": self._press_select,
                          "key": self._key_select},
            MODE_ADD_TEXT: {"press": self._press_add_text,
                            "key": self._key_add_text},
            MODE_SELECT_TEXT: {"press": self._press_select_text,
                               "key": self._key_select_text},
            MODE_CROP: {"press": self._press_crop,
                        "key": self._key_crop},
        }
        # Markup tool modes (annotations & markup §4.2) register through the
        # same dispatch -- one press/key handler pair serves all four kinds
        # (the armed kind is read back off ``self._mode``).
        for _markup_mode in _MARKUP_MODES:
            self._mode_handlers[_markup_mode] = {
                "press": self._press_markup, "key": self._key_markup,
            }
        # The note tool (§4.2) shares the markup key handler (no live band
        # to abort -- Esc just disarms) with its own one-click press.
        self._mode_handlers[MODE_NOTE] = {
            "press": self._press_note, "key": self._key_markup,
        }
        # Freehand ink + drawn shapes (§4.2, M3): one press/key pair serves
        # all four shape kinds (the armed kind is read off ``self._mode``).
        self._mode_handlers[MODE_INK] = {
            "press": self._press_ink, "key": self._key_ink,
        }
        for _shape_mode in _SHAPE_MODES:
            self._mode_handlers[_shape_mode] = {
                "press": self._press_shape, "key": self._key_shape,
            }
        # Image placement (images & signatures §3): registered through the
        # same dispatch -- click places at the default size, drag
        # rubber-bands an aspect-locked rect, Esc aborts/disarms.
        self._mode_handlers[MODE_PLACE_IMAGE] = {
            "press": self._press_place_image, "key": self._key_place_image,
        }
        # Page-item factories (perf foundation M4b): callables
        # ``factory(layer, view) -> list[QGraphicsItem]`` run at the end of
        # every page materialize so later workstreams (annotation/form
        # hotspots) add per-page scene items without forking
        # ``_materialize_page``. Default empty: identical scene graph.
        self._page_item_factories: list = []
        # Annotation hotspots ride that factory seam instead of forking
        # _materialize_page (annotations & markup §4.1): one transparent
        # AnnotHotspot per record, tracked on ``layer.extra_items``. On an
        # annot-free page the factory contributes zero items, so the default
        # scene census is unchanged.
        self.register_page_item_factory(self._annot_hotspot_factory)
        # Form-field hotspots ride the same factory seam (forms §3): one
        # FieldHotspot per FILLABLE widget at Z_FORM_HOTSPOT, tracked on
        # ``layer.extra_items``. On a form-free document the factory
        # contributes zero items, so the default scene census is unchanged.
        self.register_page_item_factory(self._field_hotspot_factory)
        self._overlay: SelectionOverlay | None = None
        # An add we just made + opened the editor on, so an empty commit can be
        # rolled back (empty-add cleanup, §3.5).
        self._pending_add: NewBox | None = None
        # True between requesting an 'add' and the window handing the fresh
        # NewBox back via select_box: makes the add flow self-contained (the
        # view opens the text editor itself, so it works whether or not the
        # window calls begin_add_box_edit, EDITOR_SPEC §3.5 step 4).
        self._awaiting_add = False

        # --- Drag state (the view is the single owner, §3.3/§3.6) --------
        self._drag_kind = None                  # None|"move"|"resize"
        self._drag_box = None
        self._drag_start_scene = QPointF()
        self._drag_armed = False                # crossed the move slop yet
        self._drag_handle = None
        self._drag_start_rect = QRectF()        # box scene rect at press
        self._drag_start_size = 0.0             # box font size at press
        self._drag_anchor_pdf = None            # resize anchor in PDF points
        self._drag_baseline_scene = None        # resize: baseline scene point
        self._drag_start_diag = 1.0
        self._drag_preview = None               # cheap live affordance item
        # Ghost move preview (text-editing UX §3.2): the box's own pixels,
        # copied out of the page pixmap, riding the cursor at 0.65 opacity.
        self._drag_ghost: QGraphicsPixmapItem | None = None
        # 1px accent guide line while a move is axis-locked (Shift or the
        # 0.75pt snap back to the original x/y); None while unlocked.
        self._axis_guide: QGraphicsLineItem | None = None
        self._axis_lock: str | None = None      # "x"|"y" travel axis, or None
        # Style clipboard ("Bring style...", §3.4): the effective style dict
        # captured by Copy Style, applied by Paste Style as ONE style command.
        self._style_clipboard: dict | None = None
        # --- Text-select tool state (text-editing UX §5.2) ----------------
        # The active word selection as (page_index, lo, hi): a contiguous
        # reading-order range of INDICES into document.page_words(page) --
        # indices, not rects, so the selection survives zoom/reload (the
        # accent bands rebuild from the indices after every relayout).
        # Read-only end to end: no commands, no undo entries, no bake.
        self._text_sel: tuple[int, int, int] | None = None
        self._text_sel_items: list[QGraphicsRectItem] = []
        # Live press-drag extension: the anchor word index + its page while
        # the mouse is down (None = no text drag in flight). Single page per
        # drag by design (§7 de-scope: no cross-page selection).
        self._text_drag_page: int | None = None
        self._text_drag_anchor: int | None = None
        # Last double-click as (monotonic seconds, scene point): a third
        # press within the platform double-click interval and a few px
        # promotes word -> LINE selection (triple-click), mirroring the
        # inline editor's §2.2 convention.
        self._text_last_double: tuple[float, QPointF] | None = None
        # --- Crop tool state (doc tools §2.6) ------------------------------
        # The in-flight rubber drag: the pressed page + the (sheet-clamped)
        # anchor corner, plus the dashed accent rect item. Single page per
        # drag by design -- a cropbox is per page; the all-pages scope is the
        # window dialog's job. None everywhere = no crop drag in flight.
        self._crop_page: int | None = None
        self._crop_anchor: QPointF | None = None
        self._crop_rubber: QGraphicsRectItem | None = None
        # Second-click-to-edit (REFLOW_SPEC: single click SELECTS, a second
        # click on the ALREADY-selected box starts text editing with the caret
        # where clicked). Set on a press that lands on the current selection;
        # consumed by a non-armed move release to promote into begin_edit.
        self._press_on_selected = False
        self._press_hotspot = None
        # --- Markup tool state (annotations & markup §4.3) -----------------
        # The live press-drag while a markup tool is armed: (page, anchor
        # scene point), or None. The translucent band item previews the drag
        # at Z_DRAG_PREVIEW; release intersects the band with page_words and
        # emits ONE annotCommandRequested('add', ...).
        self._markup_drag: tuple[int, QPointF] | None = None
        self._markup_band: QGraphicsRectItem | None = None
        # --- Ink / shape tool state (annotations & markup §4.3, M3) --------
        # A live ink stroke: {"page", "points": [PDF pts], "path"} with its
        # QGraphicsPathItem preview at Z_DRAG_PREVIEW; release with >= 2
        # decimated points emits ONE 'add' (one stroke == one undo step).
        self._ink_drag: dict | None = None
        self._ink_item: QGraphicsPathItem | None = None
        # A live shape drag: (page, anchor scene point) with its preview item
        # (rect/ellipse/line + the arrow-head polygon); release emits ONE
        # 'add', a < 3pt text-space diagonal discards (§4.3).
        self._shape_drag: tuple[int, QPointF] | None = None
        self._shape_item = None
        self._shape_arrow_head: QGraphicsPolygonItem | None = None
        # --- Annot selection / move state (annotations & markup §4.3) ------
        # The selected AnnotRecord (mutually exclusive with the box
        # selection), its dashed outline item, and the live press-drag that
        # may become ONE 'move' command on release.
        self._annot_selection = None
        self._annot_overlay: AnnotSelectionOverlay | None = None
        self._annot_drag: dict | None = None
        # --- Image placement / resize state (images & signatures §3) -------
        # The armed payload ({"image", "natural_px", "kind"}), the live
        # press-drag (page, anchor scene point) with its aspect-locked rubber
        # rect, and the live image-resize preview (the source pixels
        # stretched to the dragged rect; built once per drag).
        self._image_place_payload: dict | None = None
        self._image_place_drag: tuple[int, QPointF] | None = None
        self._image_place_rubber: QGraphicsRectItem | None = None
        self._drag_image_preview: QGraphicsPixmapItem | None = None

    # =====================================================================
    # Construction / document
    # =====================================================================
    def set_document(self, document: PDFDocument) -> None:
        # A mounted form editor belongs to the OUTGOING document's fields:
        # tear it down silently BEFORE the swap (committing here would stage
        # onto the wrong doc); the focused field is per-document too.
        self._teardown_form_editor()
        self._focused_field = None
        # Tab switches swap documents on this ONE shared view: doc A's pixels
        # must never key-collide with doc B's (the render-cache key does not
        # encode the document, and two pristine docs share a signature).
        self._render_cache.clear()
        self.document = document
        self._page_index = 0
        self._selection = None
        self._text_sel = None              # word indices are per-document
        self._end_text_drag()
        self._markup_drag = None           # band item dies with the reload
        self._markup_band = None
        self._ink_drag = None              # preview items die with the reload
        self._ink_item = None
        self._shape_drag = None
        self._shape_item = None
        self._shape_arrow_head = None
        self._annot_selection = None       # records are per-document
        self._annot_overlay = None         # item dies with the reload
        self._annot_drag = None
        self._image_place_payload = None   # payloads are per-document
        self._image_place_drag = None
        self._image_place_rubber = None    # item dies with the reload
        self._drag_image_preview = None
        self._end_crop_drag()              # before reload() clears the scene
        self._mode = MODE_SELECT
        self.reload()
        # Apply the fit-to-width default now that the layers (and their sizes)
        # exist, so the widest page fills the viewport on open (REFLOW_SPEC §R3.3).
        if self._zoom_mode in ("fit_page", "fit_width"):
            self._apply_fit_zoom()
        # Start at the top of page 0.
        vbar = self.verticalScrollBar()
        if vbar is not None:
            vbar.setValue(vbar.minimum())
        self._update_lazy_render()
        self._update_current_page(emit=False)
        self.pageChanged.emit(self._page_index)
        self.selectionChanged.emit(None)
        self.annotSelectionChanged.emit(None)

    def clear_document(self) -> None:
        """Drop the document and show an empty scene (the empty-state overlay
        is owned by the container, not the scene)."""
        self._cancel_editor_silent()
        self._teardown_form_editor()       # silent: the doc is going away
        self._focused_field = None
        self._render_cache.clear()
        self.document = None
        self._page_index = 0
        self._layers = []
        self._boxes = []
        self._hotspots = []
        self._page_image = None
        self._selection = None
        self._overlay = None
        self._text_sel = None
        self._text_sel_items = []          # died with scene.clear() below
        self._end_text_drag()
        self._markup_drag = None
        self._markup_band = None           # dies with scene.clear() below
        self._ink_drag = None
        self._ink_item = None              # dies with scene.clear() below
        self._shape_drag = None
        self._shape_item = None            # dies with scene.clear() below
        self._shape_arrow_head = None
        self._annot_selection = None
        self._annot_overlay = None         # dies with scene.clear() below
        self._annot_drag = None
        self._image_place_payload = None
        self._image_place_drag = None
        self._image_place_rubber = None    # dies with scene.clear() below
        self._drag_image_preview = None    # dies with scene.clear() below
        self._crop_rubber = None           # dies with scene.clear() below
        self._crop_page = None
        self._crop_anchor = None
        self._mode = MODE_SELECT
        self.scene().clear()
        self.scene().setSceneRect(QRectF())
        self.selectionChanged.emit(None)
        self.annotSelectionChanged.emit(None)

    # =====================================================================
    # Extension seams (perf foundation M4b / M4c)
    # =====================================================================
    def register_page_item_factory(self, factory) -> None:
        """Register a per-page overlay factory: ``factory(layer, view) ->
        list[QGraphicsItem]``, called at the END of every page materialize.
        Returned items are added to the scene and tracked on
        ``layer.extra_items`` so dematerialize removes them symmetrically.
        Factories own their items' z-values -- pick a slot from the Z-SLOT
        REGISTRY comment at the top of this module (annotation/image/form
        hotspots all sit below Z_HOTSPOT=10, so text hotspots keep winning
        overlapping clicks)."""
        self._page_item_factories.append(factory)

    def register_mode_handlers(self, mode: str, *, press=None,
                               key=None) -> None:
        """Register tool-mode event handlers for ``mode`` (the value
        ``self._mode`` takes while the tool is armed). ``press(event) ->
        bool`` runs for a left press on EMPTY canvas (hotspots/handles accept
        their own presses first); ``key(event) -> bool`` runs for view-level
        key presses. Return True = consumed (no super() routing). New canvas
        modes register here instead of editing mousePressEvent /
        keyPressEvent (perf foundation M4c)."""
        entry = self._mode_handlers.setdefault(mode, {})
        if press is not None:
            entry["press"] = press
        if key is not None:
            entry["key"] = key

    # =====================================================================
    # Navigation / view
    # =====================================================================
    def set_page(self, page_index: int) -> None:
        """Scroll so ``page_index`` sits at the top of the viewport (continuous
        scroll, REFLOW_SPEC §R3.4). Unlike the single-page canvas this does NOT
        swap the rendered page -- all pages already live in the scene; it just
        moves the viewport. A box on the previously centered page can stay
        selected (its page is still in the scene); the current-page derivation +
        pageChanged update follow from the scroll."""
        if not self.document:
            return
        clamped = max(0, min(page_index, self.document.page_count - 1))
        if not self._layers:
            return
        self._flush_editor()
        layer = self._layers[clamped]
        vbar = self.verticalScrollBar()
        if vbar is not None:
            # Align the page's top near the viewport top (small breathing gap).
            target_scene_y = layer.y_top - self._page_gap
            self._scroll_scene_y_to_top(target_scene_y)
        self._update_lazy_render()
        self._update_current_page(emit=True)

    def scroll_to_page(self, page_index: int) -> None:
        """Alias used by the thumbnail click (REFLOW_SPEC §R3.8)."""
        self.set_page(page_index)

    def visible_pages(self) -> list[int]:
        """The page indices currently MATERIALIZED (rendered pixmap on screen),
        i.e. the visible band + buffer (REFLOW_SPEC §R3.8). On a tall document
        this is a strict subset of all pages (lazy render)."""
        return [ly.page_index for ly in self._layers if ly.rendered]

    @property
    def page_index(self) -> int:
        return self._page_index

    def set_zoom(self, zoom: float) -> None:
        clamped = max(ZOOM_MIN, min(zoom, ZOOM_MAX))
        self._zoom_mode = "fixed"
        if abs(clamped - self._zoom) < 1e-6 and self._layers:
            return
        self._flush_editor()
        anchor = self._current_page_anchor()
        self._zoom = clamped
        # Suspend lazy materialization across the reload: its scroll-to-top
        # plus the scrollbar churn used to bake OFF-SCREEN pages at stale
        # offsets, and those transients evicted the previous zoom's visible
        # band from the render cache, so every zoom revisit MISSED
        # (final-review perf fix). The anchor restore below runs the ONE
        # lazy-render pass at the settled offset, materializing exactly the
        # visible band.
        self._suspend_lazy = True
        try:
            self.reload()
        finally:
            self._suspend_lazy = False
        self._restore_page_anchor(anchor)
        self._recenter_h()
        self.zoomChanged.emit(self._zoom)

    def set_zoom_mode(self, mode: str) -> None:
        if mode not in ("fixed", "fit_page", "fit_width"):
            return
        self._zoom_mode = mode
        if mode == "fixed" or not self.document:
            return
        self._apply_fit_zoom()

    @property
    def zoom(self) -> float:
        return self._zoom

    def _apply_fit_zoom(self) -> None:
        """Fit the WIDEST page to the viewport width (fit_width) or the current
        page to the viewport (fit_page) (REFLOW_SPEC §R3.3). Re-fits + re-lays the
        whole stack on resize so no page overflows horizontally."""
        if not self.document or not self._layers:
            return
        vp = self.viewport().size()
        avail_w = max(1, vp.width() - 2 * theme.SHEET_MARGIN)
        avail_h = max(1, vp.height() - 2 * theme.SHEET_MARGIN)
        widest = max((ly.pt_size[0] for ly in self._layers
                      if ly.pt_size[0] > 0), default=0.0)
        if widest <= 0:
            return
        if self._zoom_mode == "fit_width":
            z = avail_w / widest
        else:  # fit_page: fit the CURRENT page fully in view
            cur = self._layers[max(0, min(self._page_index, len(self._layers) - 1))]
            pw, ph = cur.pt_size
            if pw <= 0 or ph <= 0:
                return
            z = min(avail_w / pw, avail_h / ph)
        z = max(ZOOM_MIN, min(z, ZOOM_MAX))
        if abs(z - self._zoom) > 1e-6:
            self._flush_editor()
            anchor = self._current_page_anchor()
            self._zoom = z
            # Same transient suppression as set_zoom (see there).
            self._suspend_lazy = True
            try:
                self.reload()
            finally:
                self._suspend_lazy = False
            self._restore_page_anchor(anchor)
            self.zoomChanged.emit(self._zoom)

    # --- Gesture zoom (navigation M3) ---------------------------------------
    def _apply_gesture_zoom(self, target: float) -> None:
        """The ONE funnel for pinch + Cmd+wheel zoom: clamp ``target`` to the
        zoom bounds and THROTTLE the apply. Every ``set_zoom`` is a full
        ``reload()`` (the documented pain point), so gesture ticks only update
        the pending target; a 120 ms single-shot timer applies the LATEST one
        through the existing ``set_zoom`` (which already commits any open
        editor and restores the page+fraction scroll anchor)."""
        self._gesture_target = max(ZOOM_MIN, min(float(target), ZOOM_MAX))
        if not self._gesture_timer.isActive():
            self._gesture_timer.start()

    def _flush_gesture_zoom(self) -> None:
        """Apply the pending throttled target NOW (the timer tick, or an
        EndNativeGesture forcing the final apply without the 120 ms wait)."""
        self._gesture_timer.stop()
        target, self._gesture_target = self._gesture_target, None
        if target is None or self.document is None:
            return
        if abs(target - self._zoom) < 1e-6:
            return
        self.set_zoom(target)

    def _gesture_base_zoom(self) -> float:
        """The zoom a new gesture step composes on: the PENDING throttled
        target when one exists (wheel ticks inside one throttle window must
        compound, not reset against the stale live zoom), else the live
        zoom."""
        if self._gesture_target is not None:
            return self._gesture_target
        return self._zoom

    def viewportEvent(self, ev) -> bool:
        """macOS trackpad gestures (navigation M3; the viewport receives
        QNativeGestureEvent, not the view). Pinch accumulates into
        ``_pinch_factor`` -- seeded with the current zoom on Begin, scaled by
        ``1 + value()`` per Zoom tick -- and funnels through the throttled
        ``_apply_gesture_zoom``; End forces the final apply. SmartZoom (the
        two-finger double-tap) toggles fit-width <-> 100%."""
        if ev.type() == QEvent.NativeGesture and self.document is not None:
            gtype = ev.gestureType()
            if gtype == Qt.NativeGestureType.BeginNativeGesture:
                self._pinch_factor = self._gesture_base_zoom()
                return True
            if gtype == Qt.NativeGestureType.ZoomNativeGesture:
                self._pinch_factor *= (1.0 + ev.value())
                self._apply_gesture_zoom(self._pinch_factor)
                return True
            if gtype == Qt.NativeGestureType.EndNativeGesture:
                self._flush_gesture_zoom()
                return True
            if gtype == Qt.NativeGestureType.SmartZoomNativeGesture:
                if self._zoom_mode == "fit_width":
                    self.set_zoom(1.0)
                else:
                    self.set_zoom_mode("fit_width")
                return True
        return super().viewportEvent(ev)

    def wheelEvent(self, ev) -> None:
        """Cmd+scroll zooms (navigation M3); a plain scroll stays a scroll.
        Qt maps the macOS Command key onto ControlModifier, so this is the
        portable check. One wheel notch (120 angle-delta units) is one 1.1x
        step; trackpad deltas scale fractionally through the same exponent,
        and the throttle in ``_apply_gesture_zoom`` coalesces the storm."""
        if ev.modifiers() & Qt.ControlModifier and self.document is not None:
            steps = ev.angleDelta().y() / 120.0
            if steps:
                self._apply_gesture_zoom(
                    self._gesture_base_zoom() * (1.1 ** steps))
            ev.accept()
            return
        super().wheelEvent(ev)

    def _recenter_h(self, *args) -> None:
        """Keep the page horizontally CENTERED: snap the (hidden) horizontal
        scroll back to its midpoint so the page never drifts off-centre and a
        zoom-in trims both sides equally."""
        if self._recentering_h:
            return
        hb = self.horizontalScrollBar()
        mid = (hb.minimum() + hb.maximum()) // 2
        if hb.value() != mid:
            self._recentering_h = True
            try:
                hb.setValue(mid)
            finally:
                self._recentering_h = False

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self._zoom_mode in ("fit_page", "fit_width"):
            # Defer the re-fit (a full reload()) to the settle timer so a live
            # window drag does not re-rasterize the page on every frame. The
            # page keeps its current raster during the drag; it re-fits crisply
            # once the drag stops (~90 ms). Restarting the timer on each event
            # means it only fires after the user pauses.
            self._resize_timer.start()
        else:
            # A fixed zoom still needs the lazy band recomputed on a resize (the
            # visible page set changed) and the current page re-derived.
            self._update_lazy_render()
            self._update_current_page(emit=True)
        self._recenter_h()

    def _apply_fit_after_resize(self) -> None:
        """Settle-timer slot: apply the deferred fit re-zoom once the window
        drag has stopped (see ``resizeEvent``)."""
        if self._zoom_mode in ("fit_page", "fit_width"):
            self._apply_fit_zoom()
            self._recenter_h()

    # --- scroll-anchor preservation across a re-layout -------------------
    def _current_page_anchor(self) -> tuple:
        """Capture (page_index, fraction-down-that-page) under the viewport center
        so a zoom/relayout can restore the same reading position."""
        if not self._layers:
            return (0, 0.0)
        center_y = self.mapToScene(self.viewport().rect().center()).y()
        for ly in self._layers:
            h = max(ly.pt_size[1] * self._zoom, 1.0)
            if ly.y_top <= center_y <= ly.y_top + h:
                return (ly.page_index, (center_y - ly.y_top) / h)
        return (self._page_index, 0.0)

    def _restore_page_anchor(self, anchor: tuple) -> None:
        page, frac = anchor
        if not self._layers:
            return
        page = max(0, min(page, len(self._layers) - 1))
        ly = self._layers[page]
        target_center = ly.y_top + frac * ly.pt_size[1] * self._zoom
        self._scroll_scene_y_to_center(target_center)
        self._update_lazy_render()
        self._update_current_page(emit=True)

    def _scroll_scene_y_to_top(self, scene_y: float) -> None:
        """Scroll so scene-y ``scene_y`` lands at the viewport TOP."""
        vbar = self.verticalScrollBar()
        if vbar is None:
            return
        top_now = self.mapToScene(self.viewport().rect().topLeft()).y()
        delta = scene_y - top_now
        vbar.setValue(int(round(vbar.value() + delta)))

    def _scroll_scene_y_to_center(self, scene_y: float) -> None:
        vbar = self.verticalScrollBar()
        if vbar is None:
            return
        center_now = self.mapToScene(self.viewport().rect().center()).y()
        delta = scene_y - center_now
        vbar.setValue(int(round(vbar.value() + delta)))

    # =====================================================================
    # Rendering (continuous scroll: build all layers, lazy-render visible)
    # =====================================================================
    def reload(self) -> None:
        """Rebuild ALL page layers and lazily render the visible band
        (REFLOW_SPEC §R3.1/§R3.2). Every page is stacked top-to-bottom in one
        scene with a gap; the total scene height is known up front from the page
        sizes so the scrollbar extent is correct immediately, and only the
        visible pages (+ buffer) get a pixmap.

        Committed edits are baked per page by ``render_with_edits`` (the SAME
        pipeline as save_as), so WYSIWYG holds per page. An OPEN inline editor is
        COMMITTED first (not dropped) so a mid-edit reload preserves typed text;
        the still-selected box is re-found by IDENTITY across the fresh layers."""
        self._flush_editor()
        scene = self.scene()
        sel_identity = (self._selection.identity
                        if self._selection is not None else None)
        annot_identity = (self._annot_selection.identity
                          if self._annot_selection is not None else None)
        # Multi-selection overlays die with scene.clear(); the extras' identities
        # would be stale after a regroup, so drop the multi-selection on reload.
        had_multi = bool(self._multi_extra)
        self._multi_overlays = []
        self._multi_extra = []
        scene.clear()
        self._layers = []
        self._hotspots = []
        self._boxes = []
        self._page_image = None
        self._overlay = None
        self._annot_overlay = None         # died with scene.clear()
        if had_multi:
            self.multiSelectionChanged.emit(0)
        # The highlight items died with scene.clear(); the (page, lo, hi)
        # INDICES survive and rebuild at the end of this reload (§5.2).
        self._text_sel_items = []
        # Any in-flight crop rubber died with scene.clear() too; drop the
        # drag state (a reload mid-drag means the geometry it anchored on is
        # gone). Crop MODE itself survives -- the user can just redraw.
        self._crop_rubber = None
        self._crop_page = None
        self._crop_anchor = None
        # Same for an in-flight image placement rubber / resize preview
        # (images & signatures §3): the items died with scene.clear(); the
        # armed payload survives so the user can still click to place.
        self._image_place_drag = None
        self._image_place_rubber = None
        self._drag_image_preview = None
        if not self.document:
            scene.setSceneRect(QRectF())
            return

        self._page_index = max(0, min(self._page_index,
                                      self.document.page_count - 1))
        z = self._zoom
        m = theme.SHEET_MARGIN

        # Build a layer per page with its y offset, from the page sizes (cheap:
        # one rect + rotation read per page). The scene is sized to the full stack
        # so the scrollbar is correct before any pixmap renders. We need the
        # WIDEST page before we can center the others, so this first pass records
        # sizes + y offsets and accumulates ``max_w_scene``; a second pass sets
        # each page's centered ``x_left`` and creates its placeholder.
        y = float(self._page_gap)
        max_w_scene = 0.0
        for pi in range(self.document.page_count):
            layer = _PageLayer(pi)
            layer.rotation = self.document.page_rotation(pi)
            layer.rotation_matrix = self.document.rotation_matrix(pi)
            rect = self.document.doc[pi].rect
            # ``page.rect`` ALREADY reflects the page's /Rotate -- a portrait page
            # rotated 90 reports a 792x612 (landscape) rect. The materialize pass
            # sizes the slot from get_pixmap, which is ALSO rotated, so pass 1 must
            # use ``rect`` AS-IS. The old "90/270 swaps w/h" branch DOUBLE-counted
            # rotation: a rotated page got a wrongly-shaped slot, and where the
            # real (rotated) height exceeded the allocated height the page overran
            # into the next one -- the "pages clipping into each other" seen on
            # scanned/rotated image-only pages (which carry /Rotate far more often
            # than born-digital text PDFs, hence the "only image pages clip").
            pw, ph = rect.width, rect.height
            layer.pt_size = (pw, ph)
            layer.y_top = y
            page_h = ph * z
            max_w_scene = max(max_w_scene, pw * z)
            self._layers.append(layer)
            y += page_h + self._page_gap

        total_h = y
        # Remember the widest page (scene px) so materialize can re-center a page
        # exactly after it refines its measured size from the actual render.
        self._max_w_scene = max_w_scene
        scene.setSceneRect(QRectF(0, 0, max_w_scene + 2 * m, total_h))

        # Second pass: center each page horizontally (x = m + (max - page)/2) and
        # drop a thin placeholder sheet so the page reads as present (and is
        # click-targetable for add-on-empty) before its pixmap materializes.
        for layer in self._layers:
            page_w = layer.pt_size[0] * z
            page_h = layer.pt_size[1] * z
            layer.x_left = m + (max_w_scene - page_w) / 2.0
            placeholder = QGraphicsRectItem(
                QRectF(layer.x_left, layer.y_top, page_w, page_h))
            placeholder.setBrush(QBrush(theme.color_sheet_white()))
            placeholder.setPen(QPen(Qt.NoPen))
            placeholder.setZValue(Z_SHEET)
            scene.addItem(placeholder)
            layer.placeholder_item = placeholder

        # Materialize the visible band (+ buffer) and sync the current-page
        # mirror so the geometry helpers + tests have a valid current page.
        self._update_lazy_render(force_current=True)
        self._sync_current_page_mirror()

        # Re-establish the selection overlay for the still-present box.
        if sel_identity is not None:
            fresh = self._box_by_identity(sel_identity)
            if fresh is not None:
                self._selection = fresh
                self._ensure_layer_for_box(fresh)
                self._install_overlay(fresh)
            else:
                self._selection = None
                self.selectionChanged.emit(None)

        # Re-establish the ANNOT selection's dashed outline by identity
        # (records were rebuilt; a vanished record clears -- §4.3).
        if annot_identity is not None:
            fresh_rec = self._annot_record_by_identity(annot_identity)
            if fresh_rec is not None:
                self.select_annot(fresh_rec)
            else:
                self._annot_selection = None
                self.annotSelectionChanged.emit(None)

        # Re-paint the text-select highlight from its (page, lo, hi) word
        # indices -- the §5.2 survival rule: page_words order is zoom-
        # independent, so the indices stay valid and only the scene rects
        # need re-deriving after a zoom/reload.
        if self._mode == MODE_SELECT_TEXT and self._text_sel is not None:
            self._rebuild_text_selection_items()

    # --- lazy materialize / dematerialize --------------------------------
    def _visible_scene_rect(self) -> QRectF:
        return self.mapToScene(self.viewport().rect()).boundingRect()

    def _avg_page_height_scene(self) -> float:
        if not self._layers:
            return 1.0
        hs = [ly.pt_size[1] * self._zoom for ly in self._layers if ly.pt_size[1] > 0]
        return (sum(hs) / len(hs)) if hs else 1.0

    def _layer_scene_rect(self, layer: "_PageLayer") -> QRectF:
        return QRectF(layer.x_left, layer.y_top,
                      layer.pt_size[0] * self._zoom,
                      layer.pt_size[1] * self._zoom)

    def _update_lazy_render(self, force_current: bool = False) -> None:
        """Materialize pages intersecting the visible band (+ buffer) and evict
        pages well outside it (REFLOW_SPEC §R3.2). Cheap to call on every scroll
        tick: only the newly-entered / newly-left pages do work. No-ops while
        ``_suspend_lazy`` is set (a zoom relayout in flight -- transient scroll
        offsets must not bake off-screen pages into the render cache)."""
        if getattr(self, "_suspend_lazy", False):
            return
        if not self.document or not self._layers:
            return
        vis = self._visible_scene_rect()
        buf = _BUFFER_PAGES * self._avg_page_height_scene()
        keep = vis.adjusted(0, -buf, 0, buf)
        evict_buf = (_BUFFER_PAGES + _EVICT_MARGIN_PAGES) * self._avg_page_height_scene()
        keep_evict = vis.adjusted(0, -evict_buf, 0, evict_buf)
        for layer in self._layers:
            r = self._layer_scene_rect(layer)
            if r.intersects(keep):
                if not layer.rendered:
                    self._materialize_page(layer)
            elif layer.rendered and not r.intersects(keep_evict):
                # Never evict the page hosting the live editor or the selection
                # (evicting the selection's page would orphan its overlay and
                # any factory-built extra items bound to the selected box).
                if self._editor_box is not None and \
                        getattr(self._editor_box, "page_index", None) == layer.page_index:
                    continue
                if self._selection is not None and \
                        getattr(self._selection, "page_index", None) == layer.page_index:
                    continue
                self._dematerialize_page(layer)

    def _baked_page_image(self, page_index: int, exclude_span=None) -> QImage:
        """The page's baked pixels as a DPR-tagged ``QImage``, served from the
        render cache when ``(page, zoom*dpr, render_signature)`` hits (perf
        foundation M2).

        WYSIWYG safety: a hit re-displays bytes produced by the SAME
        ``render_with_edits`` pipeline at an EQUAL ``render_signature``, and
        equal signatures guarantee equal pixels (the document.py contract), so
        the cache cannot drift screen from file. Undo/redo at both
        granularities change the signature, making hits against stale staged
        state impossible.

        ``exclude_span`` (the box a live inline editor floats over) BYPASSES
        the cache entirely in both directions: editor-excluded pixels are not
        the page's true baked state and must never be keyed as such. Today no
        materialize caller passes it; the guard protects whoever adds one."""
        z = self._zoom
        dpr = self.devicePixelRatioF() or 1.0
        scale = z * dpr
        signature = None
        if exclude_span is None:
            signature = self.document.render_signature(page_index)
            cached = self._render_cache.get(page_index, scale, signature)
            if cached is not None:
                # Re-tag with the CURRENT dpr: the pixels depend only on the
                # zoom*dpr product (the key), but the paint-time tag must
                # match today's screen (a monitor swap changes dpr while a
                # compensating zoom keeps the product equal).
                cached.setDevicePixelRatio(dpr)
                return cached
        pix = self.document.render_with_edits(page_index, scale, exclude_span)
        image = QImage(pix.samples, pix.width, pix.height, pix.stride,
                       QImage.Format_RGB888).copy()
        image.setDevicePixelRatio(dpr)
        if signature is not None:
            self._render_cache.put(page_index, scale, signature, image)
        return image

    def _materialize_page(self, layer: "_PageLayer") -> None:
        """Render ``layer``'s pixmap (baked, cache-served on a signature hit) +
        build its sheet/shadow/hotspots and add them to the scene at
        ``layer.y_top`` (REFLOW_SPEC §R3.2)."""
        if layer.rendered or self.document is None:
            return
        z = self._zoom
        dpr = self.devicePixelRatioF() or 1.0
        m = theme.SHEET_MARGIN
        image = self._baked_page_image(layer.page_index)
        layer.image = image
        pixmap = QPixmap.fromImage(image)
        page_w = image.width() / dpr
        page_h = image.height() / dpr
        # Refine the layer's measured pt size from the actual render (keeps the
        # display-space size exact, incl. rotation), then re-center the page from
        # that exact width so the sheet/shadow/pixmap/hotspots all share one
        # ``x_left`` (mixed-size docs stay centered, REFLOW_SPEC §R3.1).
        layer.pt_size = (page_w / z, page_h / z)
        layer.x_left = m + (self._max_w_scene - page_w) / 2.0
        x = layer.x_left
        scene = self.scene()

        # The page floats on the warm gutter under TWO soft drop shadows -- the
        # design's --shadow-page: a wide AMBIENT layer plus a tighter CONTACT
        # layer that grounds the sheet at its edge. Both flip warm->deep by mode.
        # Qt allows one QGraphicsEffect per item, so each layer is its own rect
        # (filled with its shadow ink so the blur has something to cast; the white
        # sheet covers the solid cores, leaving only the soft halo).
        def _mk_shadow(blur, off_y, color):
            it = QGraphicsRectItem(QRectF(x, layer.y_top, page_w, page_h))
            it.setBrush(QBrush(color))
            it.setPen(QPen(Qt.NoPen))
            it.setZValue(Z_SHADOW)
            eff = QGraphicsDropShadowEffect()
            eff.setBlurRadius(blur)
            eff.setOffset(0, off_y)
            eff.setColor(color)
            it.setGraphicsEffect(eff)
            scene.addItem(it)
            return it
        a_blur, a_off, a_color = theme.page_shadow_ambient()
        c_blur, c_off, c_color = theme.page_shadow_contact()
        layer.shadow_item = _mk_shadow(a_blur, a_off, a_color)   # ambient layer
        # The contact layer gets its OWN tracked slot (NOT extra_items, which is
        # reset before the factory loop below, which would orphan it -> a shadow
        # that accumulates on every scroll/remat). _dematerialize_page frees it.
        layer.shadow_item2 = _mk_shadow(c_blur, c_off, c_color)

        sheet = QGraphicsRectItem(QRectF(x, layer.y_top, page_w, page_h))
        sheet.setBrush(QBrush(theme.color_sheet_white()))
        sheet.setPen(QPen(Qt.NoPen))
        sheet.setZValue(Z_SHEET)
        scene.addItem(sheet)
        layer.sheet_item = sheet

        pixmap_item = scene.addPixmap(pixmap)
        pixmap_item.setOffset(x, layer.y_top)
        pixmap_item.setZValue(Z_PIXMAP)
        layer.pixmap_item = pixmap_item

        # Hide the placeholder while the real sheet/pixmap are present.
        if layer.placeholder_item is not None:
            layer.placeholder_item.setVisible(False)

        # The page's editable box list: existing spans (overlap-merged + grouped
        # into ParagraphBoxes) + new boxes added from scratch + placed images
        # (images & signatures §3: a BOX kind, inline in this identity
        # machinery -- NOT the page-item factory -- so selection re-binding
        # and repaint_box serve images unchanged).
        spans = self.document.spans(layer.page_index)
        news = self.document.new_boxes(layer.page_index)
        images = self.document.image_boxes(layer.page_index)
        # Existing page images minus the staged deletions (images &
        # signatures §3, M3): a staged-deleted occurrence loses its hotspot
        # immediately (the repaint already shows it gone) and regains it on
        # undo (the repaint re-materializes through here).
        staged_gone = {x.identity
                       for x in self.document.xim_deletes(layer.page_index)}
        xims = [x for x in self.document.existing_images(layer.page_index)
                if x.identity not in staged_gone]
        layer.boxes = list(spans) + list(news) + list(images) + xims
        layer.hotspots = []
        for box in layer.boxes:
            if self._is_image_box(box):
                hotspot = ImageHotspot(self._image_scene_rect(box), box, self)
            else:
                rect = self._span_scene_rect(box)
                baseline_y = self._scene_point(
                    *box.origin, page_index=box.page_index).y()
                hotspot = SpanHotspot(rect, box, baseline_y, self)
            scene.addItem(hotspot)
            layer.hotspots.append(hotspot)

        # Registered page-item factories run LAST (perf foundation M4b), after
        # the page's pixmap/sheet/hotspots exist, so a factory can read the
        # layer's final geometry. Items are tracked on ``layer.extra_items``
        # for symmetric removal in ``_dematerialize_page``. With no factories
        # registered (the default) the scene graph is identical to pre-M4.
        layer.extra_items = []
        for factory in self._page_item_factories:
            for item in (factory(layer, self) or ()):
                if item.scene() is None:
                    scene.addItem(item)
                layer.extra_items.append(item)
        layer.rendered = True

        # If the selection lives on this page, (re)install its overlay now that
        # the page's fresh box objects exist (REFLOW_SPEC §R3.5).
        if self._selection is not None and \
                getattr(self._selection, "page_index", None) == layer.page_index:
            fresh = self._find_box_in_layer(layer, self._selection.identity)
            if fresh is not None:
                self._selection = fresh
                if self._overlay is None:
                    self._install_overlay(fresh)
                else:
                    self._refresh_overlay()
        # Keep the current-page mirror current if THIS is the current page.
        if layer.page_index == self._page_index:
            self._sync_current_page_mirror()

    def _dematerialize_page(self, layer: "_PageLayer") -> None:
        """Free a page's pixmap + scene items but keep its layer record (so the
        scroll position + scene height stay stable, REFLOW_SPEC §R3.2)."""
        scene = self.scene()
        for it in (layer.pixmap_item, layer.sheet_item, layer.shadow_item,
                   layer.shadow_item2):
            if it is not None and it.scene() is not None:
                scene.removeItem(it)
        for hs in layer.hotspots:
            if hs.scene() is not None:
                scene.removeItem(hs)
        for it in layer.extra_items:
            if it.scene() is not None:
                scene.removeItem(it)
        layer.pixmap_item = None
        layer.sheet_item = None
        layer.shadow_item = None
        layer.shadow_item2 = None
        layer.hotspots = []
        layer.boxes = []
        layer.extra_items = []
        layer.image = None
        layer.rendered = False
        if layer.placeholder_item is not None:
            layer.placeholder_item.setVisible(True)

    def _ensure_layer_for_box(self, box) -> "_PageLayer | None":
        """Make sure the page hosting ``box`` is materialized (so its hotspots +
        fresh box objects exist), returning that layer."""
        pi = getattr(box, "page_index", None)
        if pi is None or not (0 <= pi < len(self._layers)):
            return None
        layer = self._layers[pi]
        if not layer.rendered:
            self._materialize_page(layer)
        return layer

    @staticmethod
    def _find_box_in_layer(layer: "_PageLayer", identity) -> object | None:
        if identity is None:
            return None
        for b in layer.boxes:
            if b.identity == identity:
                return b
        return None

    # --- current page (derived from the viewport center) -----------------
    def _current_page_from_scroll(self) -> int:
        """The active page = the one whose sheet contains the viewport CENTER
        (nearest on ties / gaps) (REFLOW_SPEC §R3.4)."""
        if not self._layers:
            return 0
        center_y = self.mapToScene(self.viewport().rect().center()).y()
        best, best_d = 0, float("inf")
        for ly in self._layers:
            top = ly.y_top
            bot = ly.y_top + ly.pt_size[1] * self._zoom
            if top <= center_y <= bot:
                return ly.page_index
            d = min(abs(center_y - top), abs(center_y - bot))
            if d < best_d:
                best, best_d = ly.page_index, d
        return best

    def _page_at_scene_y(self, scene_y: float) -> int:
        """The page index whose sheet (or following gap) contains ``scene_y`` --
        the page a click landed on. Clamps to the nearest page for clicks in a
        gutter above the first / below the last page."""
        if not self._layers:
            return 0
        for ly in self._layers:
            top = ly.y_top - self._page_gap * 0.5
            bot = ly.y_top + ly.pt_size[1] * self._zoom + self._page_gap * 0.5
            if top <= scene_y <= bot:
                return ly.page_index
        # Above the first or below the last page: snap to the nearest end.
        if scene_y < self._layers[0].y_top:
            return 0
        return self._layers[-1].page_index

    def _update_current_page(self, *, emit: bool) -> None:
        new_page = self._current_page_from_scroll()
        if new_page != self._page_index:
            self._page_index = new_page
            self._sync_current_page_mirror()
            if emit:
                self.pageChanged.emit(self._page_index)
        else:
            self._sync_current_page_mirror()

    def _sync_current_page_mirror(self) -> None:
        """Point the current-page attributes (_sheet_origin / _page_image /
        rotation / pt_size + the _boxes/_hotspots aliases) at the current page's
        layer, so the page-relative geometry helpers and the existing tests
        (which read these for page 0) keep working (REFLOW_SPEC §R3.5)."""
        if not self._layers:
            self._page_image = None
            self._boxes = []
            self._hotspots = []
            return
        idx = max(0, min(self._page_index, len(self._layers) - 1))
        layer = self._layers[idx]
        # Mirror the page's CENTERED left edge so the page-relative geometry
        # helpers default to the same x the per-page path uses (REFLOW_SPEC §R3.1).
        self._sheet_origin = QPointF(layer.x_left, layer.y_top)
        self._page_pt_size = layer.pt_size
        self._page_rotation = layer.rotation
        self._rotation_matrix = layer.rotation_matrix
        self._page_image = layer.image
        self._boxes = layer.boxes
        self._hotspots = layer.hotspots

    def _schedule_scroll_update(self, *_args) -> None:
        if self.document is not None:
            self._scroll_timer.start()

    def _on_scroll_settled(self) -> None:
        self._update_lazy_render()
        self._update_current_page(emit=True)

    # =====================================================================
    # Selection model (EDITOR_SPEC §3.3 / §3.4)
    # =====================================================================
    def current_selection(self):
        """The currently selected Box (Span | NewBox), or None."""
        return self._selection

    def current_mode(self) -> str:
        """"select" | "add_text" | "text_edit" | "select_text" |
        "markup_<kind>" | "note" | "ink" | "shape_<kind>" | "crop" |
        "place_image" (EDITOR_SPEC §3.2; select_text = text-editing UX §5.2;
        annot modes = annotations & markup §4.2; place_image = images &
        signatures §3)."""
        if self._editor is not None:
            return MODE_TEXT_EDIT
        return self._mode

    def select_box(self, box) -> None:
        """Programmatically select a box (used after add / after a commit).
        Re-finds the live box by identity so an external caller can pass a stale
        object.

        If an add is awaiting (the window just handed back the freshly created
        NewBox after our 'add' request), this ALSO drops into TEXT_EDIT on it,
        so the add flow is self-contained: a click in ADD_TEXT mode ends with
        the user typing in a live editor regardless of whether the window calls
        ``begin_add_box_edit`` explicitly (EDITOR_SPEC §3.5 step 4)."""
        if box is None:
            self.clear_selection()
            return
        fresh = self._box_by_identity(getattr(box, "identity", None)) or box
        if self._awaiting_add and isinstance(fresh, NewBox):
            self._awaiting_add = False
            self.begin_add_box_edit(fresh)
            return
        self.clear_annot_selection()       # mutual exclusion (annots §4.3)
        self._selection = fresh
        self._install_overlay(fresh)
        self.setFocus(Qt.OtherFocusReason)
        self.selectionChanged.emit(fresh)

    def clear_selection(self) -> None:
        had_multi = bool(self._multi_extra)
        self._clear_multi()
        if self._selection is None and self._overlay is None:
            if had_multi:
                self.multiSelectionChanged.emit(0)
            return
        self._remove_overlay()
        self._selection = None
        self.selectionChanged.emit(None)
        if had_multi:
            self.multiSelectionChanged.emit(0)

    # --- multi-object selection (manual group / ungroup) -----------------
    def selected_boxes(self) -> list:
        """Every selected box: the primary plus any Shift-added extras, deduped
        by identity, primary first. Empty when nothing is selected."""
        out = []
        seen = set()
        for b in ([self._selection] if self._selection is not None else []) \
                + list(self._multi_extra):
            if b is None:
                continue
            ident = getattr(b, "identity", id(b))
            if ident in seen:
                continue
            seen.add(ident)
            out.append(b)
        return out

    def _clear_multi(self) -> None:
        for ov in self._multi_overlays:
            if ov.scene() is not None:
                self.scene().removeItem(ov)
        self._multi_overlays = []
        self._multi_extra = []

    def _toggle_multi(self, box) -> None:
        """Shift+click: add ``box`` to the multi-selection, or remove it if
        already in it (including the primary -- removing the primary promotes
        the next extra). Emits multiSelectionChanged with the new total."""
        if box is None:
            return
        ident = getattr(box, "identity", None)
        # Removing a box already selected?
        if self._is_selected(box):
            # Drop the primary; promote the first extra (if any) to primary.
            self._remove_overlay()
            self._selection = None
            if self._multi_extra:
                promoted = self._multi_extra.pop(0)
                self._rebuild_multi_overlays()
                self._selection = promoted
                self._install_overlay(promoted)
            self.selectionChanged.emit(self._selection)
            self.multiSelectionChanged.emit(len(self.selected_boxes()))
            return
        for i, b in enumerate(self._multi_extra):
            if getattr(b, "identity", None) == ident:
                self._multi_extra.pop(i)
                self._rebuild_multi_overlays()
                self.multiSelectionChanged.emit(len(self.selected_boxes()))
                return
        # Add as an extra. If nothing is primary yet, make it the primary.
        if self._selection is None:
            self.select_box(box)
            self.multiSelectionChanged.emit(1)
            return
        self._multi_extra.append(box)
        self._add_multi_overlay(box)
        self.multiSelectionChanged.emit(len(self.selected_boxes()))

    def _add_multi_overlay(self, box) -> None:
        ov = SelectionOverlay(self)
        self.scene().addItem(ov)
        ov.set_box_rect(self._span_scene_rect(box))
        ov.set_handles_visible(False)          # extras are outline-only
        self._multi_overlays.append(ov)

    def _rebuild_multi_overlays(self) -> None:
        for ov in self._multi_overlays:
            if ov.scene() is not None:
                self.scene().removeItem(ov)
        self._multi_overlays = []
        for b in self._multi_extra:
            self._add_multi_overlay(b)

    def _is_selected(self, box) -> bool:
        return (self._selection is not None and box is not None
                and getattr(box, "identity", None) == self._selection.identity)

    def _box_by_identity(self, identity):
        """Find the live box object for ``identity`` across ALL materialized
        layers (REFLOW_SPEC §R3.5), so a selection on a page that scrolls out and
        back re-binds, and a box on a non-current page is still reachable."""
        if identity is None:
            return None
        for layer in self._layers:
            for b in layer.boxes:
                if b.identity == identity:
                    return b
        # Fall back to the current-page alias (covers the no-layers test path).
        for b in self._boxes:
            if b.identity == identity:
                return b
        return None

    def _install_overlay(self, box) -> None:
        """(Re)build the selection overlay over ``box``. An image-like box
        gets the ``_ImageSelectionOverlay`` (rect-resize handle semantics,
        images & signatures §3); everything else the text SelectionOverlay.
        An EXISTING page image mounts the overlay WITHOUT handles (§5, M3):
        its pixels are file truth, so there is no staged resize -- only the
        whole-occurrence move macro (body drag) and delete."""
        self._remove_overlay()
        if self._is_image_box(box):
            overlay = _ImageSelectionOverlay(self)
        else:
            overlay = SelectionOverlay(self)
        self.scene().addItem(overlay)
        if not self._is_existing_image(box):
            overlay.attach_handles(self.scene())
        overlay.set_box_rect(self._span_scene_rect(box))
        self._overlay = overlay
        self._update_overflow_cue(box)

    def _update_overflow_cue(self, box) -> None:
        """Recompute whether ``box`` (a ParagraphBox under edit) reflowed past its
        box bottom, paint the danger edge on the overlay accordingly, and emit
        ``overflowChanged`` so the window can post / clear the status note
        (REFLOW_SPEC §R2.5). A no-op for non-paragraph / unedited boxes (overflow
        is always 0)."""
        if self._overlay is None or self.document is None or box is None:
            return
        page = getattr(box, "page_index", self._page_index)
        try:
            overflow = self.document.paragraph_overflow(page, box)
        except Exception:  # noqa: BLE001 - a measurement hiccup must not crash
            overflow = 0.0
        self._overlay.set_overflow(overflow > 0.5)
        self.overflowChanged.emit(overflow)

    def _remove_overlay(self) -> None:
        if self._overlay is not None:
            self._overlay.detach_handles(self.scene())
            if self._overlay.scene() is not None:
                self.scene().removeItem(self._overlay)
            self._overlay = None

    def _refresh_overlay(self) -> None:
        """Reposition the overlay for the current selection's scene rect."""
        if self._selection is None or self._overlay is None:
            return
        self._overlay.set_box_rect(self._span_scene_rect(self._selection))
        self._update_overflow_cue(self._selection)

    # =====================================================================
    # Add-text mode (EDITOR_SPEC §3.2 / §3.5)
    # =====================================================================
    def enter_add_text_mode(self) -> None:
        """Arm ADD_TEXT: crosshair cursor; the next empty-canvas click adds a
        box using the inspector's current style."""
        if self._editor is not None:
            self._flush_editor()
        # Arming a different tool disarms text-select / markup / crop /
        # image placement cleanly (highlights + band + rubber cleared,
        # hotspot presses re-enabled); each no-ops unless it was armed.
        self.exit_select_text_mode()
        self.exit_annot_mode()
        self.exit_crop_mode()
        self.exit_place_image_mode()
        self._mode = MODE_ADD_TEXT
        self.viewport().setCursor(Qt.CrossCursor)
        self.modeChanged.emit(MODE_ADD_TEXT)

    def exit_add_text_mode(self) -> None:
        if self._mode != MODE_ADD_TEXT:
            return
        self._mode = MODE_SELECT
        self.viewport().setCursor(Qt.ArrowCursor)
        self.modeChanged.emit(MODE_SELECT)

    # =====================================================================
    # Text Select tool (text-editing UX §5.2) -- read-only end to end:
    # no commands, no undo entries, no bake changes. Words come from
    # document.page_words (the render/save pipeline), so the copied text
    # always equals the screen and the saved file, staged edits included.
    # =====================================================================
    def enter_select_text_mode(self) -> None:
        """Arm TEXT SELECT: I-beam cursor; presses/drags select words off the
        page. Entering clears the box selection and flushes any open editor;
        while armed, box hotspots pass presses through (``_on_box_press``
        declines) so body-drag/resize are unreachable."""
        if self._mode == MODE_SELECT_TEXT:
            return
        if self._editor is not None:
            self._flush_editor()
        self._cancel_drag()
        self.clear_selection()
        if self._mode == MODE_ADD_TEXT:
            self.exit_add_text_mode()
        self.exit_annot_mode()
        self.exit_crop_mode()
        self.exit_place_image_mode()
        self._mode = MODE_SELECT_TEXT
        self.viewport().setCursor(Qt.IBeamCursor)
        self.modeChanged.emit(MODE_SELECT_TEXT)

    def exit_select_text_mode(self) -> None:
        """Disarm TEXT SELECT back to SELECT: every highlight band is removed
        and hotspot presses work again (mode exit clears, §5.2)."""
        if self._mode != MODE_SELECT_TEXT:
            return
        self.clear_text_selection()
        self._end_text_drag()
        self._text_last_double = None
        self._mode = MODE_SELECT
        self.viewport().setCursor(Qt.ArrowCursor)
        self.modeChanged.emit(MODE_SELECT)

    def select_text_range(self, page_index: int, lo: int, hi: int) -> None:
        """Programmatic selection seam (§5.2; tests + Select All drive it):
        clamp ``[lo, hi]`` into the page's word count and highlight it."""
        if self.document is None:
            return
        words = self.document.page_words(page_index)
        if not words:
            self.clear_text_selection()
            return
        lo = max(0, min(int(lo), len(words) - 1))
        hi = max(lo, min(int(hi), len(words) - 1))
        self._set_text_selection(page_index, lo, hi)

    def text_selection_string(self) -> str:
        """The selection as clipboard text (§5.2): words joined by a single
        space within a line, a newline between lines and between blocks."""
        if self._text_sel is None or self.document is None:
            return ""
        page, lo, hi = self._text_sel
        words = self.document.page_words(page)
        if not words or lo >= len(words):
            return ""
        hi = min(hi, len(words) - 1)
        parts: list[str] = []
        prev_key = None
        for i in range(lo, hi + 1):
            wb = words[i]
            key = (wb.block, wb.line)
            if prev_key is not None:
                parts.append(" " if key == prev_key else "\n")
            parts.append(wb.text)
            prev_key = key
        return "".join(parts)

    def copy_text_selection(self) -> int:
        """Cmd+C for the text-select tool: the §5.2 join onto the SYSTEM
        clipboard as plain text (this is "copy text off the page", not the
        box clipboard). Returns the number of words copied (0 = no selection
        / no clipboard), which feeds the window's "N words copied" toast."""
        text = self.text_selection_string()
        if not text:
            return 0
        try:
            cb = QApplication.clipboard()
            if cb is None:
                return 0
            cb.setText(text)
        except Exception:  # noqa: BLE001 - clipboard must not crash the tool
            return 0
        page, lo, hi = self._text_sel
        hi = min(hi, len(self.document.page_words(page)) - 1)
        return hi - lo + 1

    def clear_text_selection(self) -> None:
        self._text_sel = None
        self._remove_text_selection_items()

    def _set_text_selection(self, page: int, lo: int, hi: int) -> None:
        self._text_sel = (page, lo, hi)
        self._rebuild_text_selection_items()

    def _remove_text_selection_items(self) -> None:
        scene = self.scene()
        for it in self._text_sel_items:
            if it.scene() is not None:
                scene.removeItem(it)
        self._text_sel_items = []

    def _rebuild_text_selection_items(self) -> None:
        """Per-LINE merged accent bands at ``Z_TEXT_SELECT`` (over the page
        pixmap, under the cover/editor chrome -- the §5.2 z-slot) for the
        current ``(page, lo, hi)``. Rebuilt wholesale: a selection changes a
        handful of lines at a time, and the rects must re-derive from the
        word indices after every zoom/reload anyway."""
        self._remove_text_selection_items()
        if self._text_sel is None or self.document is None:
            return
        page, lo, hi = self._text_sel
        words = self.document.page_words(page)
        if not words or lo >= len(words):
            self._text_sel = None
            return
        hi = min(hi, len(words) - 1)
        by_line: dict[tuple, QRectF] = {}
        for i in range(lo, hi + 1):
            wb = words[i]
            rect = self._word_scene_rect(page, wb)
            key = (wb.block, wb.line)
            cur = by_line.get(key)
            by_line[key] = rect if cur is None else cur.united(rect)
        color = theme.color_accent()
        color.setAlpha(80)
        scene = self.scene()
        for rect in by_line.values():
            item = QGraphicsRectItem(rect)
            item.setBrush(QBrush(color))
            item.setPen(QPen(Qt.NoPen))
            item.setZValue(Z_TEXT_SELECT)
            # Purely decorative: never intercept presses meant for the words
            # underneath (extending a selection re-presses over the bands).
            item.setAcceptedMouseButtons(Qt.NoButton)
            scene.addItem(item)
            self._text_sel_items.append(item)

    def _word_scene_rect(self, page: int, wb) -> QRectF:
        """A WordBox bbox (rawdict text-space) -> scene rect on ``page``,
        rotation-aware: the same transformed-corners bounding box mapping as
        ``_span_scene_rect``, reading the IMMUTABLE word bbox (page_words
        already baked any staged geometry into it)."""
        x0, y0, x1, y1 = wb.bbox
        z = self._zoom
        corners = [self._display_point(cx, cy, page)
                   for cx, cy in ((x0, y0), (x1, y0), (x1, y1), (x0, y1))]
        xs = [c[0] for c in corners]
        ys = [c[1] for c in corners]
        origin = self._sheet_origin_for(page)
        return QRectF(origin.x() + min(xs) * z, origin.y() + min(ys) * z,
                      (max(xs) - min(xs)) * z, (max(ys) - min(ys)) * z)

    def _word_index_at(self, page: int, scene_pos: QPointF) -> int | None:
        """The page_words index of the word a press at ``scene_pos`` targets:
        rect containment first, else the nearest word CENTER within 1.2x that
        word's line height (so a click in the slim gap between words still
        anchors), else None (§5.2)."""
        if self.document is None:
            return None
        words = self.document.page_words(page)
        best = None
        best_d = None
        for i, wb in enumerate(words):
            rect = self._word_scene_rect(page, wb)
            if rect.contains(scene_pos):
                return i
            c = rect.center()
            d = math.hypot(c.x() - scene_pos.x(), c.y() - scene_pos.y())
            if d <= _WORD_SNAP_LINE_HEIGHTS * max(rect.height(), 1.0) \
                    and (best_d is None or d < best_d):
                best, best_d = i, d
        return best

    def _select_text_line_at(self, page: int, scene_pos: QPointF) -> None:
        """Triple-click: select the whole (block, line) hosting the word
        under the point (§5.2). page_words is reading-order sorted, so the
        line is a contiguous index run around the hit word."""
        idx = self._word_index_at(page, scene_pos)
        if idx is None:
            return
        words = self.document.page_words(page)
        key = (words[idx].block, words[idx].line)
        lo = idx
        while lo > 0 and (words[lo - 1].block, words[lo - 1].line) == key:
            lo -= 1
        hi = idx
        while hi + 1 < len(words) \
                and (words[hi + 1].block, words[hi + 1].line) == key:
            hi += 1
        self._set_text_selection(page, lo, hi)

    def _select_all_text_current_page(self) -> None:
        """Cmd+A / context-menu Select All: every word on the page under the
        viewport center (§5.2)."""
        if self.document is None:
            return
        words = self.document.page_words(self._page_index)
        if words:
            self.select_text_range(self._page_index, 0, len(words) - 1)

    def _update_text_drag(self, scene_pos: QPointF) -> None:
        """Extend the live press-drag to the word under the cursor: the
        selection is the contiguous reading-order range [anchor, current]
        (either direction), single page per drag (§5.2)."""
        page = self._text_drag_page
        if page is None or self.document is None:
            return
        idx = self._word_index_at(page, scene_pos)
        if idx is None:
            return                       # off-words drag keeps the last range
        anchor = self._text_drag_anchor
        self._set_text_selection(page, min(anchor, idx), max(anchor, idx))

    def _end_text_drag(self) -> None:
        self._text_drag_page = None
        self._text_drag_anchor = None

    def _press_select_text(self, event) -> bool:
        """TEXT SELECT press (registered in ``_mode_handlers``, §5.2): anchor
        the selection on the word under the cursor and arm a drag that
        extends it in reading order. A third press hot on the heels of a
        double-click (within the platform interval, a few px) promotes to
        LINE selection -- the editor's triple-click convention (§2.2). A
        press that misses every word clears the selection."""
        if self.document is None:
            return False
        scene_pos = self.mapToScene(event.position().toPoint())
        event.accept()
        page = self._page_at_scene_y(scene_pos.y())
        last = self._text_last_double
        if last is not None:
            dt = time.monotonic() - last[0]
            near = (scene_pos - last[1]).manhattanLength() \
                <= _TRIPLE_CLICK_SLOP_PX
            if dt <= QApplication.doubleClickInterval() / 1000.0 and near:
                self._text_last_double = None
                self._select_text_line_at(page, scene_pos)
                return True
        idx = self._word_index_at(page, scene_pos)
        if idx is None:
            self.clear_text_selection()
            self._end_text_drag()
            return True
        self._text_drag_page = page
        self._text_drag_anchor = idx
        self._set_text_selection(page, idx, idx)
        return True

    def _key_select_text(self, event: QKeyEvent) -> bool:
        """TEXT SELECT keys (§5.2): Cmd+C copies the word selection (takes
        precedence over box copy -- no box can be selected in this mode);
        Cmd+A selects every word on the current page; Esc clears the
        selection, and a second Esc disarms the tool back to SELECT (the
        add-text convention)."""
        mods = event.modifiers()
        cmd = bool(mods & (Qt.ControlModifier | Qt.MetaModifier))
        key = event.key()
        if cmd and key == Qt.Key_C:
            if self.copy_text_selection() > 0:
                event.accept()
                return True
            return False
        if cmd and key == Qt.Key_A:
            event.accept()
            self._select_all_text_current_page()
            return True
        if key == Qt.Key_Escape:
            event.accept()
            if self._text_sel is not None:
                self.clear_text_selection()
            else:
                self.exit_select_text_mode()
            return True
        return False

    # =====================================================================
    # Markup tools (annotations & markup §4.2 / §4.3). The view never
    # mutates the model: every committed gesture exits through ONE
    # ``annotCommandRequested('add', ...)``. Words come from
    # document.page_words (bake-aware), so markup quads follow staged text
    # edits and land exactly on the displayed/saved ink.
    # =====================================================================
    def annot_mode_kind(self) -> str | None:
        """The armed annot tool kind ("highlight"|"underline"|"strikeout"|
        "squiggly"|"note"|"ink"|"rect"|"ellipse"|"line"|"arrow"), or None
        when no annot tool is armed."""
        if self._mode in (MODE_NOTE, MODE_INK):
            return self._mode
        if self._mode.startswith(_MARKUP_MODE_PREFIX):
            return self._mode[len(_MARKUP_MODE_PREFIX):]
        if self._mode.startswith(_SHAPE_MODE_PREFIX):
            return self._mode[len(_SHAPE_MODE_PREFIX):]
        return None

    def _annot_tool_armed(self) -> bool:
        """True while ANY annot tool (markup band / note click / ink stroke /
        shape drag) is armed -- the hotspot press pass-through + mode-exit
        guard (§4.2)."""
        return (self._mode.startswith(_MARKUP_MODE_PREFIX)
                or self._mode.startswith(_SHAPE_MODE_PREFIX)
                or self._mode in (MODE_NOTE, MODE_INK))

    @staticmethod
    def _annot_mode_for_kind(kind: str) -> str:
        """The ``self._mode`` value an armed annot tool kind maps to."""
        if kind in (MODE_NOTE, MODE_INK):
            return kind
        if kind in SHAPE_KINDS:
            return _SHAPE_MODE_PREFIX + kind
        return _MARKUP_MODE_PREFIX + kind

    def enter_annot_mode(self, kind: str) -> None:
        """Arm an annotation tool: the four markup kinds band-select text
        per gesture; "note" places one sticky-note anchor per click; "ink"
        draws one freehand stroke per drag; the four shape kinds rubber-band
        one shape per drag. The tool STAYS armed after each commit (Acrobat
        behavior) until Esc / tool switch. Entering flushes any open editor
        and clears both selections (§4.2)."""
        if (kind not in MARKUP_KINDS and kind not in SHAPE_KINDS
                and kind not in ("note", "ink")):
            raise ValueError(f"unknown annot tool kind: {kind!r}")
        mode = self._annot_mode_for_kind(kind)
        if self._mode == mode:
            return
        if self._editor is not None:
            self._flush_editor()
        self._cancel_drag()
        self.clear_selection()
        self.clear_annot_selection()
        if self._mode == MODE_ADD_TEXT:
            self.exit_add_text_mode()
        self.exit_select_text_mode()
        self.exit_crop_mode()
        self.exit_place_image_mode()
        self._end_markup_drag()            # switching kinds aborts any live
        self._end_ink_drag()               # gesture, whichever tool drew it
        self._end_shape_drag()
        self._mode = mode
        self.viewport().setCursor(Qt.CrossCursor)
        self.modeChanged.emit(mode)

    def exit_annot_mode(self) -> None:
        """Disarm the armed annot tool back to SELECT, aborting any live
        band / stroke / shape preview."""
        if not self._annot_tool_armed():
            return
        self._end_markup_drag()
        self._end_ink_drag()
        self._end_shape_drag()
        self._mode = MODE_SELECT
        self.viewport().setCursor(Qt.ArrowCursor)
        self.modeChanged.emit(MODE_SELECT)

    def _press_note(self, event) -> bool:
        """NOTE press (registered in ``_mode_handlers``): place an 18x18pt
        sticky-note anchor at the click point -- ONE 'add' intent with empty
        contents; the window opens the note popup on the new record (§4.3).
        The tool stays armed for the next note."""
        if self.document is None:
            return False
        scene_pos = self.mapToScene(event.position().toPoint())
        event.accept()
        page = self._page_at_scene_y(scene_pos.y())
        x, y = self._pdf_point(scene_pos, page)
        self.annotCommandRequested.emit("add", None, {
            "kind": "note", "page_index": page, "contents": "",
            "rect": (x, y, x + _NOTE_ANCHOR_PT, y + _NOTE_ANCHOR_PT),
        })
        return True

    def _press_markup(self, event) -> bool:
        """MARKUP press (registered in ``_mode_handlers``): anchor the
        translucent accent band on the pressed page. Hotspot presses pass
        through to here while the tool is armed (the mousePressEvent routing
        + ``_on_box_press`` both decline), so a drag can start over text."""
        if self.document is None:
            return False
        scene_pos = self.mapToScene(event.position().toPoint())
        event.accept()
        page = self._page_at_scene_y(scene_pos.y())
        self._markup_drag = (page, QPointF(scene_pos))
        self._update_markup_band(scene_pos)
        return True

    def _key_markup(self, event: QKeyEvent) -> bool:
        """MARKUP keys: Esc aborts a live band first, else disarms the tool
        back to SELECT (the add-text convention)."""
        if event.key() == Qt.Key_Escape:
            event.accept()
            if self._markup_drag is not None:
                self._end_markup_drag()
            else:
                self.exit_annot_mode()
            return True
        return False

    def _update_markup_band(self, scene_pos: QPointF) -> None:
        """Build/stretch the translucent accent band from the press anchor to
        the cursor (Z_DRAG_PREVIEW; purely decorative, never hit-tested)."""
        if self._markup_drag is None:
            return
        _page, anchor = self._markup_drag
        rect = QRectF(anchor, scene_pos).normalized()
        if self._markup_band is None:
            color = theme.color_accent()
            color.setAlpha(60)
            item = QGraphicsRectItem(rect)
            item.setBrush(QBrush(color))
            item.setPen(QPen(Qt.NoPen))
            item.setZValue(Z_DRAG_PREVIEW)
            item.setAcceptedMouseButtons(Qt.NoButton)
            self.scene().addItem(item)
            self._markup_band = item
        else:
            self._markup_band.setRect(rect)

    def _end_markup_drag(self) -> None:
        self._markup_drag = None
        if self._markup_band is not None:
            if self._markup_band.scene() is not None:
                self.scene().removeItem(self._markup_band)
            self._markup_band = None

    def _finish_markup_drag(self, scene_pos: QPointF) -> None:
        """Release: map the band to text space through ``_pdf_point`` (the
        exact inverse the rest of the canvas uses, rotation-aware), intersect
        with the page's baked words, group hits by (block, line) into one
        quad rect per line, and emit ONE 'add' intent (§4.3). An empty hit
        posts the "No text under selection" toast and emits no command."""
        if self._markup_drag is None:
            return
        page, anchor = self._markup_drag
        kind = self.annot_mode_kind()
        diag = (scene_pos - anchor).manhattanLength()
        self._end_markup_drag()
        if kind is None:
            return
        if diag < _MARKUP_MIN_DRAG_PX:
            return                          # stray click, not a gesture
        p0 = self._pdf_point(anchor, page)
        p1 = self._pdf_point(scene_pos, page)
        band = (min(p0[0], p1[0]), min(p0[1], p1[1]),
                max(p0[0], p1[0]), max(p0[1], p1[1]))
        quads = self._markup_quads_for(page, band)
        if not quads:
            self.statusMessage.emit("No text under selection")
            return
        self.annotCommandRequested.emit("add", None, {
            "kind": kind, "page_index": page, "quads": quads,
        })

    def _markup_quads_for(self, page: int, band: tuple) -> tuple:
        """Words intersecting the text-space ``band``, grouped by
        (block, line) and unioned into ONE quad rect per group (keeps
        Preview's markup rendering clean, §10), in reading order."""
        if self.document is None:
            return ()
        bx0, by0, bx1, by1 = band
        groups: dict[tuple, list] = {}
        for wb in self.document.page_words(page):
            x0, y0, x1, y1 = wb.bbox
            if x0 < bx1 and bx0 < x1 and y0 < by1 and by0 < y1:
                groups.setdefault((wb.block, wb.line), []).append(wb.bbox)
        quads = []
        for key in sorted(groups):
            boxes = groups[key]
            quads.append((min(b[0] for b in boxes), min(b[1] for b in boxes),
                          max(b[2] for b in boxes), max(b[3] for b in boxes)))
        return tuple(quads)

    # =====================================================================
    # Freehand ink + drawn shapes (annotations & markup §4.3, M3). Same
    # discipline as the markup band: live decorative previews at
    # Z_DRAG_PREVIEW, ONE annotCommandRequested('add', ...) per gesture,
    # the view never touches the model.
    # =====================================================================
    def _preview_pen(self) -> QPen:
        """The shared accent pen for the live ink/shape previews (cosmetic:
        constant device width across zoom; purely decorative)."""
        pen = QPen(theme.color_accent())
        pen.setWidthF(2.0)
        pen.setCosmetic(True)
        pen.setCapStyle(Qt.RoundCap)
        pen.setJoinStyle(Qt.RoundJoin)
        return pen

    def _press_ink(self, event) -> bool:
        """INK press (registered in ``_mode_handlers``): anchor a freehand
        stroke on the pressed page -- moves append decimated text-space
        points; the live path preview rides the cursor."""
        if self.document is None:
            return False
        scene_pos = self.mapToScene(event.position().toPoint())
        event.accept()
        page = self._page_at_scene_y(scene_pos.y())
        path = QPainterPath(scene_pos)
        item = QGraphicsPathItem(path)
        item.setPen(self._preview_pen())
        item.setZValue(Z_DRAG_PREVIEW)
        item.setAcceptedMouseButtons(Qt.NoButton)
        self.scene().addItem(item)
        self._ink_item = item
        self._ink_drag = {"page": page, "path": path,
                          "points": [self._pdf_point(scene_pos, page)]}
        return True

    def _key_ink(self, event: QKeyEvent) -> bool:
        """INK keys: Esc aborts a live stroke first, else disarms the tool
        back to SELECT (the markup-band convention)."""
        if event.key() == Qt.Key_Escape:
            event.accept()
            if self._ink_drag is not None:
                self._end_ink_drag()
            else:
                self.exit_annot_mode()
            return True
        return False

    def _update_ink_drag(self, scene_pos: QPointF) -> None:
        """Append one decimated point to the live stroke: moves under
        ``_INK_DECIMATE_PT`` text-space points from the last recorded point
        are skipped (§4.3); the preview path extends with the cursor."""
        st = self._ink_drag
        if st is None:
            return
        x, y = self._pdf_point(scene_pos, st["page"])
        lx, ly = st["points"][-1]
        if math.hypot(x - lx, y - ly) < _INK_DECIMATE_PT:
            return
        st["points"].append((x, y))
        st["path"].lineTo(scene_pos)
        if self._ink_item is not None:
            self._ink_item.setPath(st["path"])

    def _finish_ink_drag(self, scene_pos: QPointF) -> None:
        """Release: a stroke of >= 2 decimated points commits ONE annot (one
        stroke == one undo step, §4.3); fewer points = a stray click,
        discarded. The tool stays armed for the next stroke."""
        self._update_ink_drag(scene_pos)
        st = self._ink_drag
        self._end_ink_drag()
        if st is None or len(st["points"]) < 2:
            return
        self.annotCommandRequested.emit("add", None, {
            "kind": "ink", "page_index": st["page"],
            "points": (tuple(st["points"]),),
        })

    def _end_ink_drag(self) -> None:
        self._ink_drag = None
        if self._ink_item is not None:
            if self._ink_item.scene() is not None:
                self.scene().removeItem(self._ink_item)
            self._ink_item = None

    def _press_shape(self, event) -> bool:
        """SHAPE press (registered in ``_mode_handlers``): anchor the live
        rubber-band preview for the armed kind on the pressed page."""
        if self.document is None:
            return False
        kind = self.annot_mode_kind()
        if kind not in SHAPE_KINDS:
            return False
        scene_pos = self.mapToScene(event.position().toPoint())
        event.accept()
        page = self._page_at_scene_y(scene_pos.y())
        pen = self._preview_pen()
        if kind == "rect":
            item = QGraphicsRectItem(QRectF(scene_pos, scene_pos))
        elif kind == "ellipse":
            item = QGraphicsEllipseItem(QRectF(scene_pos, scene_pos))
        else:                              # line / arrow
            item = QGraphicsLineItem(QLineF(scene_pos, scene_pos))
            if kind == "arrow":
                head = QGraphicsPolygonItem()
                head.setBrush(QBrush(theme.color_accent()))
                head.setPen(QPen(Qt.NoPen))
                head.setZValue(Z_DRAG_PREVIEW)
                head.setAcceptedMouseButtons(Qt.NoButton)
                self.scene().addItem(head)
                self._shape_arrow_head = head
        item.setPen(pen)
        if isinstance(item, (QGraphicsRectItem, QGraphicsEllipseItem)):
            item.setBrush(QBrush(Qt.transparent))
        item.setZValue(Z_DRAG_PREVIEW)
        item.setAcceptedMouseButtons(Qt.NoButton)
        self.scene().addItem(item)
        self._shape_item = item
        self._shape_drag = (page, QPointF(scene_pos))
        return True

    def _key_shape(self, event: QKeyEvent) -> bool:
        """SHAPE keys: Esc aborts a live preview first, else disarms."""
        if event.key() == Qt.Key_Escape:
            event.accept()
            if self._shape_drag is not None:
                self._end_shape_drag()
            else:
                self.exit_annot_mode()
            return True
        return False

    def _update_shape_drag(self, scene_pos: QPointF) -> None:
        """Stretch the live preview from the press anchor to the cursor."""
        if self._shape_drag is None or self._shape_item is None:
            return
        _page, anchor = self._shape_drag
        if isinstance(self._shape_item, QGraphicsLineItem):
            self._shape_item.setLine(QLineF(anchor, scene_pos))
            if self._shape_arrow_head is not None:
                self._shape_arrow_head.setPolygon(
                    self._arrow_head(anchor, scene_pos))
        else:
            self._shape_item.setRect(QRectF(anchor, scene_pos).normalized())

    def _finish_shape_drag(self, scene_pos: QPointF) -> None:
        """Release: map anchor + release through ``_pdf_point`` and emit ONE
        'add' for the armed kind -- rect/ellipse carry the normalized rect,
        line/arrow keep the drag DIRECTION (the head lands at the release
        point). A < 3pt text-space diagonal is a stray click (§4.3)."""
        if self._shape_drag is None:
            return
        page, anchor = self._shape_drag
        kind = self.annot_mode_kind()
        self._end_shape_drag()
        if kind not in SHAPE_KINDS:
            return
        x0, y0 = self._pdf_point(anchor, page)
        x1, y1 = self._pdf_point(scene_pos, page)
        if math.hypot(x1 - x0, y1 - y0) < _SHAPE_MIN_DIAG_PT:
            return                          # stray click, not a shape
        if kind in ("rect", "ellipse"):
            params = {"rect": (min(x0, x1), min(y0, y1),
                               max(x0, x1), max(y0, y1))}
        else:
            params = {"endpoints": ((x0, y0), (x1, y1))}
        params.update({"kind": kind, "page_index": page})
        self.annotCommandRequested.emit("add", None, params)

    def _end_shape_drag(self) -> None:
        self._shape_drag = None
        for attr in ("_shape_item", "_shape_arrow_head"):
            item = getattr(self, attr)
            if item is not None:
                if item.scene() is not None:
                    self.scene().removeItem(item)
                setattr(self, attr, None)

    @staticmethod
    def _arrow_head(p0: QPointF, p1: QPointF) -> QPolygonF:
        """A small filled triangle at ``p1`` pointing along p0 -> p1 for the
        live arrow preview (decorative; the saved annot's open-arrow ending
        is drawn by the PDF viewer)."""
        line = QLineF(p0, p1)
        if line.length() < 1e-6:
            return QPolygonF()
        angle = math.atan2(line.dy(), line.dx())
        spread = math.pi / 7
        left = QPointF(p1.x() - _ARROW_HEAD_PX * math.cos(angle - spread),
                       p1.y() - _ARROW_HEAD_PX * math.sin(angle - spread))
        right = QPointF(p1.x() - _ARROW_HEAD_PX * math.cos(angle + spread),
                        p1.y() - _ARROW_HEAD_PX * math.sin(angle + spread))
        return QPolygonF([p1, left, right])

    # =====================================================================
    # Annotation selection / move / delete (annotations & markup §4.3).
    # Mutually exclusive with the box selection; every mutation exits
    # through ONE annotCommandRequested -- the view never touches the model.
    # =====================================================================
    def current_annot_selection(self):
        """The selected AnnotRecord, or None."""
        return self._annot_selection

    def select_annot(self, record) -> None:
        """Select ``record`` (an AnnotRecord): dashed accent outline at
        Z_SELECTION, no handles. Clears any box selection (mutual
        exclusion)."""
        if record is None:
            self.clear_annot_selection()
            return
        self.clear_selection()
        self._remove_annot_overlay()
        self._annot_selection = record
        rect = self._annot_scene_rect(record.identity[0],
                                      record.display_rect)
        overlay = AnnotSelectionOverlay(
            rect.adjusted(-_OUTLINE_INFLATE, -_OUTLINE_INFLATE,
                          _OUTLINE_INFLATE, _OUTLINE_INFLATE))
        self.scene().addItem(overlay)
        self._annot_overlay = overlay
        self.setFocus(Qt.OtherFocusReason)
        self.annotSelectionChanged.emit(record)

    def clear_annot_selection(self) -> None:
        if self._annot_selection is None and self._annot_overlay is None:
            return
        self._remove_annot_overlay()
        self._annot_selection = None
        self.annotSelectionChanged.emit(None)

    def select_annot_by_identity(self, identity) -> bool:
        """Programmatic selection by identity (panel jump / external
        callers). Returns False -- clearing any annot selection -- when the
        record no longer exists."""
        record = self._annot_record_by_identity(identity)
        if record is None:
            self.clear_annot_selection()
            return False
        self.select_annot(record)
        return True

    def _annot_record_by_identity(self, identity):
        """The CURRENT AnnotRecord for ``identity`` (records are rebuilt on
        every mutation, so live object refs go stale), or None."""
        if identity is None or self.document is None:
            return None
        try:
            records = self.document.annotations(identity[0])
        except Exception:  # noqa: BLE001 - a stale page index must not crash
            return None
        for rec in records:
            if rec.identity == identity:
                return rec
        return None

    def annot_scene_rect(self, identity) -> QRectF | None:
        """The scene rect of an annotation by identity (note-popup anchoring
        + tests), or None when the record is gone."""
        record = self._annot_record_by_identity(identity)
        if record is None:
            return None
        return self._annot_scene_rect(record.identity[0],
                                      record.display_rect)

    def _annot_scene_rect(self, page_index: int, rect: tuple) -> QRectF:
        """A text-space annot rect -> scene rect on ``page_index``: corner-
        mapped through ``_scene_point``, so a rotated page's swap/flip rides
        the same math every other overlay uses."""
        x0, y0, x1, y1 = rect
        pts = [self._scene_point(x, y, page_index=page_index)
               for x, y in ((x0, y0), (x1, y0), (x1, y1), (x0, y1))]
        xs = [p.x() for p in pts]
        ys = [p.y() for p in pts]
        return QRectF(QPointF(min(xs), min(ys)), QPointF(max(xs), max(ys)))

    def _remove_annot_overlay(self) -> None:
        if self._annot_overlay is not None:
            if self._annot_overlay.scene() is not None:
                self.scene().removeItem(self._annot_overlay)
            self._annot_overlay = None

    def _annot_hotspot_factory(self, layer, _view) -> list:
        """The registered page-item factory (perf foundation M4b): one
        transparent AnnotHotspot per record on the page at Z_ANNOT_HOTSPOT,
        tracked on ``layer.extra_items`` for symmetric dematerialize. Zero
        items on an annot-free page (the default census is unchanged)."""
        if self.document is None:
            return []
        try:
            records = self.document.annotations(layer.page_index)
        except Exception:  # noqa: BLE001 - annot read must not break render
            return []
        return [AnnotHotspot(self._annot_scene_rect(layer.page_index,
                                                    rec.display_rect),
                             rec, self)
                for rec in records]

    def _on_annot_press(self, hotspot: "AnnotHotspot",
                        scene_pos: QPointF) -> bool:
        """A press landed on an annot hotspot (or Alt+click forced it).
        SELECT mode only: select the record and arm a potential body-drag
        MOVE -- staged annots and existing notes only; foreign markup
        cannot relocate (§4.3 / §8), so its press is a plain select."""
        if self.document is None or self._mode != MODE_SELECT:
            return False
        if self._editor is not None:
            self._flush_editor()
        self._cancel_drag()
        record = self._annot_record_by_identity(hotspot.identity)
        if record is None:
            return False
        self.select_annot(record)
        movable = (not record.is_existing) or record.kind == "note"
        self._annot_drag = {
            "identity": record.identity, "page": record.identity[0],
            "start": QPointF(scene_pos), "armed": False, "movable": movable,
            "start_rect": (QRectF(self._annot_overlay.rect())
                           if self._annot_overlay is not None else QRectF()),
        }
        return True

    def _on_annot_double_click(self, hotspot: "AnnotHotspot",
                               scene_pos: QPointF) -> bool:
        """Double-click a NOTE hotspot -> the window opens its popup editor
        (§5.3). Other kinds (and armed tools) decline."""
        if self.document is None or self._mode != MODE_SELECT:
            return False
        record = self._annot_record_by_identity(hotspot.identity)
        if record is None or record.kind != "note":
            return False
        self._annot_drag = None        # the first press armed a move; drop it
        self.select_annot(record)
        self.noteEditRequested.emit(record)
        return True

    def _update_annot_drag(self, scene_pos: QPointF) -> None:
        """Live annot move: arm past the 3px slop (movable records only),
        then ride the dashed outline with the cursor -- the cheap drag
        affordance; the page repaints once on release."""
        st = self._annot_drag
        if st is None:
            return
        delta = scene_pos - st["start"]
        if not st["armed"]:
            if abs(delta.x()) < _MIN_DRAG_PX and abs(delta.y()) < _MIN_DRAG_PX:
                return
            if not st["movable"]:
                return                 # foreign markup: press stays a select
            st["armed"] = True
        if self._annot_overlay is not None:
            self._annot_overlay.setRect(st["start_rect"].translated(delta))

    def _finish_annot_drag(self, scene_pos: QPointF) -> None:
        """Release: an armed move emits ONE 'move' intent with the delta in
        PDF text-space points; an unarmed press was just the select."""
        st = self._annot_drag
        self._annot_drag = None
        if st is None or not st["armed"]:
            return
        delta = scene_pos - st["start"]
        dx, dy = self._scene_delta_to_pdf(delta, st["page"])
        if abs(dx) < 1e-6 and abs(dy) < 1e-6:
            if self._annot_overlay is not None:
                self._annot_overlay.setRect(st["start_rect"])
            return
        self.annotCommandRequested.emit("move", st["identity"], {
            "page_index": st["page"], "dx": dx, "dy": dy,
        })

    def _abort_annot_drag(self) -> None:
        """Esc during a live annot move: restore the outline, no command."""
        st = self._annot_drag
        self._annot_drag = None
        if st is None:
            return
        if st["armed"] and self._annot_overlay is not None:
            self._annot_overlay.setRect(st["start_rect"])

    # =====================================================================
    # Crop tool (doc tools §2.6) -- chrome only: the view draws the dashed
    # rubber rect and reports the chosen text-space rect via
    # ``cropRectSelected``; the WINDOW owns the scope dialog and the
    # crop_pages structural op (the view never mutates the model).
    # =====================================================================
    def enter_crop_mode(self) -> None:
        """Arm CROP: crosshair cursor; the next press anchors a dashed
        rubber rect on the pressed page (clamped to its sheet) and the
        release emits ``cropRectSelected`` with the rect in that page's
        unrotated text space. Entering clears the box selection and disarms
        the other tools, so no overlay/handles can swallow the drag."""
        if self._mode == MODE_CROP:
            return
        if self._editor is not None:
            self._flush_editor()
        self._cancel_drag()
        self.clear_selection()
        self.clear_annot_selection()
        if self._mode == MODE_ADD_TEXT:
            self.exit_add_text_mode()
        self.exit_select_text_mode()
        self.exit_annot_mode()
        self.exit_place_image_mode()
        self._mode = MODE_CROP
        self.viewport().setCursor(Qt.CrossCursor)
        self.modeChanged.emit(MODE_CROP)

    def exit_crop_mode(self) -> None:
        """Disarm CROP back to SELECT: any in-flight rubber rect is removed
        and box presses work again. No-op unless the tool was armed."""
        if self._mode != MODE_CROP:
            return
        self._end_crop_drag()
        self._mode = MODE_SELECT
        self.viewport().setCursor(Qt.ArrowCursor)
        self.modeChanged.emit(MODE_SELECT)

    def _press_crop(self, event) -> bool:
        """CROP press (registered in ``_mode_handlers``, M4c dispatch):
        record the pressed page + the anchor corner and start the rubber
        rect. Presses over box hotspots route here too (the §2.6 layering:
        ``_on_box_press`` declines in this mode), so the drag can start
        anywhere on the sheet."""
        if self.document is None:
            return False
        scene_pos = self.mapToScene(event.position().toPoint())
        event.accept()
        page = self._page_at_scene_y(scene_pos.y())
        self._crop_page = page
        self._crop_anchor = self._clamp_to_sheet(scene_pos, page)
        self._update_crop_rubber(scene_pos)
        return True

    def _key_crop(self, event: QKeyEvent) -> bool:
        """CROP keys (the Enter/Esc contract convention): Escape cancels an
        in-flight rubber drag first (the mode stays armed for a redraw),
        else disarms back to SELECT -- the two-stage Esc the text-select
        tool established."""
        if event.key() == Qt.Key_Escape:
            event.accept()
            if self._crop_page is not None:
                self._end_crop_drag()
            else:
                self.exit_crop_mode()
            return True
        return False

    def _clamp_to_sheet(self, scene_pos: QPointF, page: int) -> QPointF:
        """Clamp a scene point into ``page``'s sheet rect, so a drag that
        wanders off the page still yields a rect that lies ON the page (the
        §2.6 sheet clamp)."""
        if not self._layers or not 0 <= page < len(self._layers):
            return QPointF(scene_pos)
        r = self._layer_scene_rect(self._layers[page])
        return QPointF(min(max(scene_pos.x(), r.left()), r.right()),
                       min(max(scene_pos.y(), r.top()), r.bottom()))

    def _update_crop_rubber(self, scene_pos: QPointF) -> None:
        """Grow the dashed accent rubber rect from the anchor to the
        (sheet-clamped) cursor. Created lazily on the first call at
        ``Z_DRAG_PREVIEW`` (the live drag-affordance slot, §2.6) and
        mouse-transparent so it never eats its own drag events."""
        if self._crop_page is None or self._crop_anchor is None:
            return
        pos = self._clamp_to_sheet(scene_pos, self._crop_page)
        if self._crop_rubber is None:
            item = QGraphicsRectItem()
            pen = QPen(theme.color_accent())
            pen.setStyle(Qt.DashLine)
            pen.setWidthF(1.0)
            pen.setCosmetic(True)
            item.setPen(pen)
            wash = theme.color_accent()
            wash.setAlpha(26)
            item.setBrush(QBrush(wash))
            item.setZValue(Z_DRAG_PREVIEW)
            item.setAcceptedMouseButtons(Qt.NoButton)
            self.scene().addItem(item)
            self._crop_rubber = item
        self._crop_rubber.setRect(QRectF(self._crop_anchor, pos).normalized())

    def _finish_crop_drag(self, scene_pos: QPointF) -> None:
        """Release: map BOTH corners through ``_pdf_point`` (the exact
        inverse of ``_scene_point``, so the rect lands in the page's
        unrotated text space -- the space ``crop_pages`` takes) and emit
        ``cropRectSelected``. A degenerate drag (< ``_CROP_MIN_DRAG_PT`` a
        side) is dropped silently: a stray click should not pop the scope
        dialog over a 0x0 rect."""
        page = self._crop_page
        anchor = self._crop_anchor
        self._end_crop_drag()
        if page is None or anchor is None:
            return
        pos = self._clamp_to_sheet(scene_pos, page)
        ax, ay = self._pdf_point(anchor, page)
        bx, by = self._pdf_point(pos, page)
        x0, x1 = sorted((ax, bx))
        y0, y1 = sorted((ay, by))
        if (x1 - x0) < _CROP_MIN_DRAG_PT or (y1 - y0) < _CROP_MIN_DRAG_PT:
            return
        self.cropRectSelected.emit(page, (x0, y0, x1, y1))

    def _end_crop_drag(self) -> None:
        """Remove the rubber rect + clear the drag state (drag end, Esc
        cancel, mode exit, document swap)."""
        if self._crop_rubber is not None \
                and self._crop_rubber.scene() is not None:
            self.scene().removeItem(self._crop_rubber)
        self._crop_rubber = None
        self._crop_page = None
        self._crop_anchor = None

    # =====================================================================
    # Image placement (images & signatures §3) -- chrome only: the view
    # draws the aspect-locked rubber rect and emits ONE
    # ``boxCommandRequested("img_add", ...)`` per gesture; the window owns
    # the BoxCommand. One click = one image: the mode auto-exits.
    # =====================================================================
    def enter_place_image_mode(self, payload: dict) -> None:
        """Arm PLACE_IMAGE with ``payload`` = {"image": bytes, "natural_px":
        (w, h), "kind": str}: crosshair cursor; the next click places the
        image at its default size (top-left at the click), a press-drag
        rubber-bands an aspect-locked rect instead. Entering flushes any
        open editor and disarms every other tool (the enter_* convention)."""
        if not payload or not payload.get("image"):
            return
        if self._editor is not None:
            self._flush_editor()
        self._cancel_drag()
        self.clear_selection()
        self.clear_annot_selection()
        if self._mode == MODE_ADD_TEXT:
            self.exit_add_text_mode()
        self.exit_select_text_mode()
        self.exit_annot_mode()
        self.exit_crop_mode()
        self._end_image_place_drag()
        self._image_place_payload = dict(payload)
        self._mode = MODE_PLACE_IMAGE
        self.viewport().setCursor(Qt.CrossCursor)
        self.modeChanged.emit(MODE_PLACE_IMAGE)

    def exit_place_image_mode(self) -> None:
        """Disarm PLACE_IMAGE back to SELECT, dropping the payload and any
        live rubber rect. No-op unless the mode was armed."""
        if self._mode != MODE_PLACE_IMAGE:
            return
        self._end_image_place_drag()
        self._image_place_payload = None
        self._mode = MODE_SELECT
        self.viewport().setCursor(Qt.ArrowCursor)
        self.modeChanged.emit(MODE_SELECT)

    def _press_place_image(self, event) -> bool:
        """PLACE_IMAGE press (registered in ``_mode_handlers``): anchor the
        gesture on the pressed page. Hotspot presses pass through while the
        tool is armed (the mousePressEvent routing + ``_on_box_press`` both
        decline), so an image can be placed over text."""
        if self.document is None or self._image_place_payload is None:
            return False
        scene_pos = self.mapToScene(event.position().toPoint())
        event.accept()
        page = self._page_at_scene_y(scene_pos.y())
        self._image_place_drag = (page, QPointF(scene_pos))
        return True

    def _key_place_image(self, event: QKeyEvent) -> bool:
        """PLACE_IMAGE keys: Esc aborts a live rubber drag first, else
        disarms the tool back to SELECT (the two-stage Esc convention)."""
        if event.key() == Qt.Key_Escape:
            event.accept()
            if self._image_place_drag is not None:
                self._end_image_place_drag()
            else:
                self.exit_place_image_mode()
            return True
        return False

    def _image_aspect(self) -> float:
        """height / width of the armed payload's source pixels (1.0 when the
        natural size is unknown)."""
        payload = self._image_place_payload or {}
        nw, nh = (payload.get("natural_px") or (0, 0))[:2]
        if nw and nh:
            return float(nh) / float(nw)
        return 1.0

    def _update_image_place_rubber(self, scene_pos: QPointF) -> None:
        """Stretch the aspect-locked rubber rect from the press anchor
        toward the cursor: the WIDTH follows the drag, the height locks to
        the source aspect, and the rect grows in the drag's quadrant."""
        if self._image_place_drag is None:
            return
        _page, anchor = self._image_place_drag
        dx = scene_pos.x() - anchor.x()
        dy = scene_pos.y() - anchor.y()
        if abs(dx) < _MIN_DRAG_PX and abs(dy) < _MIN_DRAG_PX \
                and self._image_place_rubber is None:
            return                       # still inside the click slop
        aspect = self._image_aspect()
        w = max(abs(dx), 1.0)
        h = max(w * aspect, 1.0)
        x0 = anchor.x() if dx >= 0 else anchor.x() - w
        y0 = anchor.y() if dy >= 0 else anchor.y() - h
        rect = QRectF(x0, y0, w, h)
        if self._image_place_rubber is None:
            item = QGraphicsRectItem()
            pen = QPen(theme.color_accent())
            pen.setStyle(Qt.DashLine)
            pen.setWidthF(1.0)
            pen.setCosmetic(True)
            item.setPen(pen)
            wash = theme.color_accent()
            wash.setAlpha(26)
            item.setBrush(QBrush(wash))
            item.setZValue(Z_DRAG_PREVIEW)
            item.setAcceptedMouseButtons(Qt.NoButton)
            self.scene().addItem(item)
            self._image_place_rubber = item
        self._image_place_rubber.setRect(rect)

    def _finish_image_place(self, scene_pos: QPointF) -> None:
        """Release: emit ONE 'img_add' intent -- a sub-slop release places
        at the DEFAULT size (top-left at the click point); a real drag
        places the rubber rect. Both corners map through ``_pdf_point`` (the
        rotation-aware inverse) into text space, and the model clamps into
        the page. The mode auto-exits (one click = one image)."""
        st = self._image_place_drag
        rubber = self._image_place_rubber
        rubber_rect = QRectF(rubber.rect()) if rubber is not None else None
        self._end_image_place_drag()
        payload = self._image_place_payload
        if st is None or payload is None or self.document is None:
            return
        page, anchor = st
        delta = scene_pos - anchor
        dragged = (rubber_rect is not None
                   and (abs(delta.x()) >= _MIN_DRAG_PX
                        or abs(delta.y()) >= _MIN_DRAG_PX))
        if dragged:
            p0 = self._pdf_point(rubber_rect.topLeft(), page)
            p1 = self._pdf_point(rubber_rect.bottomRight(), page)
            rect = (min(p0[0], p1[0]), min(p0[1], p1[1]),
                    max(p0[0], p1[0]), max(p0[1], p1[1]))
        else:
            rect = self._default_image_rect(page, anchor)
        # Land the command on the clicked page (the _do_add_at convention:
        # the window's BoxCommand reads view_page_index).
        if page != self._page_index:
            self._page_index = page
            self._sync_current_page_mirror()
            self.pageChanged.emit(self._page_index)
        params = {
            "rect": rect,
            "image": payload["image"],
            "kind": payload.get("kind", "file"),
            "natural_px": tuple(payload.get("natural_px") or (0, 0)),
        }
        self.exit_place_image_mode()
        self.boxCommandRequested.emit("img_add", None, params)

    def _default_image_rect(self, page: int, anchor_scene: QPointF) -> tuple:
        """The click-to-place rect (images & signatures §3): width = the
        source's pixel width at 96dpi capped at 45% of the page width,
        height by aspect, the CLICK as the top-left corner (text-space; the
        model clamps into the page)."""
        x, y = self._pdf_point(anchor_scene, page)
        payload = self._image_place_payload or {}
        nw, _nh = (payload.get("natural_px") or (0, 0))[:2]
        page_rect = self.document.working[page].rect
        width = min(
            (float(nw) * 72.0 / 96.0) if nw else page_rect.width * 0.25,
            page_rect.width * _IMAGE_DEFAULT_PAGE_FRAC)
        width = max(width, 8.0)
        height = max(width * self._image_aspect(), 8.0)
        return (x, y, x + width, y + height)

    def _end_image_place_drag(self) -> None:
        """Remove the rubber rect + clear the drag state (release, Esc,
        mode exit, document swap)."""
        if self._image_place_rubber is not None \
                and self._image_place_rubber.scene() is not None:
            self.scene().removeItem(self._image_place_rubber)
        self._image_place_rubber = None
        self._image_place_drag = None

    # =====================================================================
    # Style / delete (inspector + keyboard -> the window's command)
    # =====================================================================
    def apply_style(self, overrides: dict) -> None:
        """Apply inspector overrides to the CURRENT selection and stage them
        through undo/redo (EDITOR_SPEC §3.4). ``overrides`` keys (any subset):
        'font_family' (str), 'size' (float), 'color' ((r,g,b) 0..1), 'bold'
        (bool), 'italic' (bool). Emits ``boxCommandRequested("style", box,
        {"overrides": overrides})`` so the window funnels it into one
        ``BoxCommand``. No-op when nothing is selected, overrides is empty,
        or the selection is an image (no text style; ws4 §9)."""
        if self._selection is None or not overrides:
            return
        if self._is_image_box(self._selection):
            return
        box = self._selection
        self.boxCommandRequested.emit("style", box, {"overrides": dict(overrides)})
        # The window mutates the model + repaints via repaint_box; emit the
        # convenience signal for status wiring.
        self.styleApplied.emit(box, dict(overrides))

    def apply_style_to_selection(self, overrides: dict) -> bool:
        """Apply bold/italic to the TEXT SELECTION inside the open inline
        editor (the "bold just the highlighted word" path). Returns True when
        handled; False sends the caller down the whole-box route (no editor, no
        selection, a non-weight override, or a NewBox -- which holds a single
        uniform style). The styled runs are staged when the edit commits."""
        if self._editor is None or self._editor_box is None:
            return False
        if not overrides or not set(overrides) <= {
                "bold", "italic", "underline", "strike"}:
            return False
        # Underline / strikethrough are EDITOR-selection styles applied via the
        # char format (the glyph-run bake persists bold/italic; u/s show while
        # editing). They only apply with an open editor, handled here.
        if getattr(self._editor_box, "box_id", None) is not None \
                and set(overrides) <= {"bold", "italic"}:
            return False              # NewBox: uniform bold/italic only
        cursor = self._editor.textCursor()
        had_selection = cursor.hasSelection()
        if not had_selection:
            # No selection while editing: style the WHOLE text in place. This
            # keeps the editor open instead of the whole-box route, whose
            # reload would commit the edit out from under the user.
            cursor.select(QTextCursor.Document)
        fmt = QTextCharFormat()
        if "bold" in overrides:
            fmt.setFontWeight(QFont.Bold if overrides["bold"] else QFont.Normal)
        if "italic" in overrides:
            fmt.setFontItalic(bool(overrides["italic"]))
        if "underline" in overrides:
            fmt.setFontUnderline(bool(overrides["underline"]))
        if "strike" in overrides:
            fmt.setFontStrikeOut(bool(overrides["strike"]))
        cursor.mergeCharFormat(fmt)
        if had_selection:
            self._editor.setTextCursor(cursor)   # keep the user's selection
        return True

    def set_format_panel(self, widget) -> None:
        """Register the Format/Inspector panel so the inline editor knows a click
        that moves focus INTO it must not commit (keeps the edit + highlight)."""
        self._format_panel = widget

    def _focus_moved_to_format_panel(self) -> bool:
        """True when the widget that just took focus lives inside the registered
        Format panel -- the editor stays open instead of committing."""
        panel = self._format_panel
        if panel is None:
            return False
        w = QApplication.focusWidget()
        while w is not None:
            if w is panel:
                return True
            w = w.parentWidget()
        return False

    def _stash_editor_selection(self, anchor: int, pos: int) -> None:
        """Remember the editor's selection at focus-out so a whole-box restyle
        (which re-opens the editor) can put the highlight back."""
        self._editor_saved_selection = (int(anchor), int(pos))

    def apply_style_to_editor_box(self, overrides: dict) -> bool:
        """Apply a WHOLE-BOX style (font family / size / colour, or bold/italic
        with no per-run path) to the box being inline-edited WITHOUT dropping the
        edit: bake any text change, apply the style as one BoxCommand, then
        re-open the editor on the same box with the prior highlight restored.

        Returns True when an editor was open (handled); False so the caller falls
        back to the normal selected-box route. The model carries font/size/colour
        per BOX (not per run), so this styles the whole box -- which is exactly a
        full-text highlight, the common 'change the font of this line' case."""
        if self._editor is None or self._editor_box is None or not overrides:
            return False
        if not set(overrides) <= {"font_family", "size", "color",
                                  "bold", "italic"}:
            return False
        box = self._editor_box
        identity = getattr(box, "identity", None)
        # Selection to restore: the focus-out stash (set when the panel was
        # clicked) is the truth; fall back to the live cursor.
        if self._editor_saved_selection is not None:
            anchor, pos = self._editor_saved_selection
        else:
            cur = self._editor.textCursor()
            anchor, pos = cur.anchor(), cur.position()
        self._editor_saved_selection = None
        # Bake any text edit first (no-op when unchanged); selects the box.
        self.commit_edit()
        target = (self._box_by_identity(identity)
                  if identity is not None else None) or box
        # Apply the whole-box style as one undo step.
        self.boxCommandRequested.emit("style", target,
                                      {"overrides": dict(overrides)})
        # Re-open the editor on the same box and restore the highlight.
        fresh = (self._box_by_identity(identity)
                 if identity is not None else None)
        if fresh is None:
            return True
        hs = self._hotspot_for(fresh)
        if hs is None:
            return True
        self.begin_edit(hs)
        ed = self._editor
        if ed is not None:
            n = len(ed.toPlainText())
            c = ed.textCursor()
            c.setPosition(min(max(0, anchor), n))
            c.setPosition(min(max(0, pos), n), QTextCursor.KeepAnchor)
            ed.setTextCursor(c)
            ed.setFocus()
        return True

    def _single_line_width_ratio(self, text: str, qfont: "QFont",
                                 resolved: "ResolvedFont") -> float:
        """Scale factor so ``text`` in ``qfont`` occupies the page's baked width
        (same math as begin_edit's nested ``_width_ratio``, reused when the live
        editor font is re-resolved). 1.0 when unknown / out of the safe band."""
        z = self._zoom
        box = self._editor_box
        eff = self._editor_eff or {}
        eff_size = float(eff.get("size", getattr(box, "size", 12.0))
                         or getattr(box, "size", 12.0))
        flat = text.replace("\n", " ") or " "
        try:
            qpx = QFontMetricsF(qfont).horizontalAdvance(flat)
            if qpx <= 0:
                return 1.0
            target_pt = self.document.font_engine.fitz_font_for(
                resolved).text_length(flat, fontsize=eff_size)
            r = (target_pt * z) / qpx
            return r if 0.85 < r < 1.15 else 1.0
        except (AttributeError, ValueError, ZeroDivisionError):
            return 1.0

    def _reresolve_editor_font(self) -> None:
        """Live WYSIWYG: when the typed text changes which font the BAKE will use
        (an embedded SUBSET that no longer covers the glyphs falls back to a
        substitute), switch the live editor to THAT font so what you see is what
        gets committed -- no surprise font change on exit. Single-line only;
        paragraph re-layout stays with the bake."""
        ed = self._editor
        if ed is None or self._committing or self._closing_editor:
            return
        if self._editor_multiline or self._reresolving \
                or self._editor_box is None:
            return
        text = ed.toPlainText() or " "
        try:
            resolved = self._resolve_for_edit(
                self._editor_box, text, self._editor_eff or {},
                self._editor_page)
        except Exception:  # noqa: BLE001 - a resolve hiccup must not break typing
            return
        cur = ed.font()
        if (resolved.qt_family == cur.family()
                and resolved.qt_bold == cur.bold()
                and resolved.qt_italic == cur.italic()):
            return                       # font the bake would use is unchanged
        self._reresolving = True
        try:
            new_font = resolved.qfont(self._editor_pixel_size or cur.pixelSize())
            ratio = self._single_line_width_ratio(text, new_font, resolved)
            if abs(ratio - 1.0) > 0.001:
                new_font.setLetterSpacing(
                    QFont.SpacingType.PercentageSpacing, ratio * 100.0)
            ed.setFont(new_font)
            ed.document().setDefaultFont(new_font)
            # Update the fidelity chip so the dot reflects the new tier (e.g. an
            # embedded face dropping to an amber base-14 substitute once the
            # typed glyphs leave its subset).
            self.editStarted.emit(self._editor_box, resolved)
        finally:
            self._reresolving = False

    @staticmethod
    def _apply_runs_to_editor(editor, runs) -> None:
        """Install staged rich runs as char formats over the editor's text (the
        editor text IS the joined run text, so offsets line up 1:1)."""
        cursor = QTextCursor(editor.document())
        pos = 0
        for text, bold, italic in runs:
            cursor.setPosition(pos)
            cursor.setPosition(pos + len(text), QTextCursor.KeepAnchor)
            fmt = QTextCharFormat()
            fmt.setFontWeight(QFont.Bold if bold else QFont.Normal)
            fmt.setFontItalic(bool(italic))
            cursor.mergeCharFormat(fmt)
            pos += len(text)

    @staticmethod
    def _runs_visual_key(runs) -> tuple:
        """A comparison key for rich runs that ignores whitespace styling and
        run boundaries: the (char, bold, italic) of each NON-SPACE character.
        Two run-lists with the same visible glyphs in the same weights compare
        equal even if a space is bold in one and regular in the other."""
        out = []
        for t, b, i in (runs or ()):
            for ch in t:
                if not ch.isspace():
                    out.append((ch, bool(b), bool(i)))
        return tuple(out)

    @staticmethod
    def _extract_editor_runs(editor, start: int | None = None,
                             end: int | None = None) -> tuple:
        """Read the editor document back as (text, bold, italic) runs,
        optionally clipped to the character range [start, end). A fragment
        without an explicit weight/italic property inherits the editor's BASE
        font (the box's effective style), so untouched text keeps its original
        styling. The commit path reads the whole document (no bounds); the
        clipboard copy path (§4.2) clips to the selection -- ONE walk serves
        both, so what's copied can never disagree with what a commit stages."""
        base = editor.font()
        base_bold, base_italic = base.bold(), base.italic()
        runs: list = []
        block = editor.document().begin()
        while block.isValid():
            it = block.begin()
            while not it.atEnd():
                frag = it.fragment()
                if frag.isValid():
                    fs = frag.position()
                    text = frag.text()
                    if start is not None or end is not None:
                        lo = fs if start is None else max(fs, start)
                        hi = fs + frag.length() if end is None \
                            else min(fs + frag.length(), end)
                        if lo >= hi:
                            it += 1
                            continue
                        text = text[lo - fs: hi - fs]
                    cf = frag.charFormat()
                    if cf.hasProperty(QTextFormat.FontWeight):
                        bold = cf.fontWeight() >= QFont.DemiBold
                    else:
                        bold = base_bold
                    if cf.hasProperty(QTextFormat.FontItalic):
                        italic = cf.fontItalic()
                    else:
                        italic = base_italic
                    runs.append((text, bold, italic))
                it += 1
            # A hard-wrapped paragraph editor holds the bake's lines as separate
            # blocks; each block boundary stands in for the SPACE the bake joined
            # lines with, so emit it back -- else the committed text fuses words
            # across line breaks.
            nxt = block.next()
            if nxt.isValid():
                sep_pos = block.position() + block.length() - 1
                in_range = ((start is None or sep_pos >= start)
                            and (end is None or sep_pos < end))
                if in_range:
                    last = runs[-1] if runs else None
                    runs.append((" ",
                                 last[1] if last else base_bold,
                                 last[2] if last else base_italic))
            block = nxt
        return tuple(runs)

    def delete_selected(self) -> None:
        """Delete the current selection (EDITOR_SPEC §3.4): emit a delete
        command, then clear selection. No-op if nothing selected or a text
        editor is open (Delete in TEXT_EDIT edits text, not the box). An
        image selection routes to its own kind (images & signatures §3)."""
        if self._selection is None or self._editor is not None:
            return
        box = self._selection
        if self._is_existing_image(box):
            kind = "xim_delete"           # scoped redaction stage (M3)
        elif self._is_image_box(box):
            kind = "img_delete"
        else:
            kind = "delete"
        self.boxCommandRequested.emit(kind, box, {})
        self.boxDeleted.emit(box)
        # Selection is cleared by the window's repaint (the box vanishes from
        # the fresh box list on reload); clear locally too so chrome is correct
        # immediately even if the window defers the reload.
        self._remove_overlay()
        self._selection = None
        self.selectionChanged.emit(None)

    # =====================================================================
    # Copy / paste WITH formatting (REFLOW_SPEC §R5.3)
    # =====================================================================
    def copy_selection(self) -> bool:
        """Copy the selected box's effective style + text into the in-process
        clipboard AND the system clipboard (plain text PLUS the x-pdfte-runs
        payload, §4.2, so bold/italic survive a paste into another doc).
        Returns True if a box was copied. No-op while an editor is open (the
        editor path ``editor_copy_selection`` handles in-editor copy) and for
        an image selection (clipboard image interop is de-scoped, ws4 §9)."""
        if self._selection is None or self.document is None:
            return False
        if self._editor is not None:
            return False
        if self._is_image_box(self._selection):
            return False
        box = self._selection
        page = getattr(box, "page_index", self._page_index)
        text = self.document.staged_text(page, box)
        eff = self.document.effective_style(page, box)
        self._clipboard = {
            "text": text,
            "font_family": eff.get("font_family", "Helvetica"),
            "size": float(eff.get("size", 12.0) or 12.0),
            "color": tuple(eff.get("color", (0.0, 0.0, 0.0))),
            "bold": bool(eff.get("bold", False)),
            "italic": bool(eff.get("italic", False)),
            "alignment": eff.get("alignment"),
            "line_spacing": eff.get("line_spacing"),
            "origin": getattr(box, "origin", (72.0, 72.0)),
            "page_index": page,
        }
        # System clipboard: text/plain beside the runs payload (§4.2). Runs
        # come from the staged rich runs when present (a box with a bolded
        # word round-trips it), else ONE uniform run in the effective style.
        runs = self.document.staged_runs(page, box)
        if runs is None:
            runs = ((text, bool(eff.get("bold", False)),
                     bool(eff.get("italic", False))),)
        md = QMimeData()
        md.setText(text)
        md.setData(RUNS_MIME, QByteArray(
            encode_runs_mime(text, runs, eff)))
        self._set_clipboard(md)
        return True

    def paste(self) -> bool:
        """Paste as a NEW box on the canvas. Fallback chain (§4.4): (1) the
        in-process box clipboard (richest -- full style + origin, REFLOW_SPEC
        §R5.3) at a small offset from the copied origin; (2) the system
        clipboard's x-pdfte-runs payload -> a styled NewBox; (3) plain system
        text -> a NewBox in the inspector's add style. If an editor is open
        this returns False (the editor path ``editor_paste`` owns it; the
        window's act_paste routes there first)."""
        if self._editor is not None or self.document is None:
            return False
        if not self._clipboard:
            return self._paste_from_system()
        clip = self._clipboard
        ox, oy = clip.get("origin", (72.0, 72.0))
        # Offset the paste so it does not land exactly over the source.
        origin = (ox + 12.0, oy + 14.0)
        page = max(0, min(self._page_index, self.document.page_count - 1))
        # Clamp the offset origin into the page rect minus an 18pt margin
        # (text-editing UX §3.5): a box copied near the right/bottom edge used
        # to paste OFF the page (the old comment here claimed page-targeting
        # made this fine -- it did not clamp anything).
        origin = self._clamp_origin_to_page(origin, page)
        params = {
            "origin": origin,
            "text": clip.get("text", ""),
            "family": clip.get("font_family", "Helvetica"),
            "size": clip.get("size", 12.0),
            "color": clip.get("color", (0.0, 0.0, 0.0)),
            "bold": clip.get("bold", False),
            "italic": clip.get("italic", False),
        }
        if page != self._page_index:
            self._page_index = page
            self._sync_current_page_mirror()
        self._awaiting_add = False
        self.boxCommandRequested.emit("add", None, params)
        return True

    def _paste_from_system(self) -> bool:
        """System-clipboard canvas paste (§4.4 steps 2-3): one styled NewBox
        from the x-pdfte-runs payload (its full uniform style -- a brand-new
        box may carry a foreign style, unlike an existing one), else plain
        clipboard text in the inspector's add style. Text is single-line
        sanitized (lines join with spaces -- NewBox is single-line by model
        contract). Lands at the viewport center, de-projected through
        ``_pdf_point`` and clamped into the page (§3.5). Emits ONE
        ``boxCommandRequested("add", ...)`` -> one BoxCommand undo step."""
        md = self._clipboard_mime()
        if md is None:
            return False
        decoded = None
        try:
            if md.hasFormat(RUNS_MIME):
                decoded = decode_runs_mime(bytes(md.data(RUNS_MIME)))
        except Exception:  # noqa: BLE001 - foreign data must not crash paste
            decoded = None
        if decoded is not None:
            text = decoded["text"].strip()
            s = decoded["style"]
            style = {"font_family": s["family"], "size": s["size"],
                     "color": s["color"], "bold": s["bold"],
                     "italic": s["italic"]}
        elif md.hasText():
            text = sanitize_pasted_text(md.text()).strip()
            style = self._add_style_defaults()
        else:
            return False
        if not text:
            return False
        center = self.mapToScene(self.viewport().rect().center())
        page = self._page_at_scene_y(center.y())
        origin = self._clamp_origin_to_page(
            self._pdf_point(center, page), page)
        if page != self._page_index:
            self._page_index = page
            self._sync_current_page_mirror()
        self._awaiting_add = False
        self.boxCommandRequested.emit("add", None, {
            "origin": origin,
            "text": text,
            "family": style["font_family"],
            "size": style["size"],
            # Guard against an invisible inherited/clipboard color (white text on
            # the white page body, etc.) so a paste is never silently unreadable.
            "color": self._add_color_visible_on(
                style["color"], page, origin, style["size"]),
            "bold": style["bold"],
            "italic": style["italic"],
        })
        return True

    def _clamp_origin_to_page(self, origin: tuple, page: int,
                              margin: float = 18.0) -> tuple:
        """Clamp a paste origin (PDF text-space) into ``page``'s rect minus an
        18pt margin (text-editing UX §3.5). Dimensions come from the MODEL's
        exact page rect (the layer's ``pt_size`` is refined from the rendered
        pixmap and carries pixel-quantization slack, which would let a clamp
        land fractionally off-page); the rect is the ROTATED display rect, so
        a 90/270 page swaps it back to the text-space dimensions the origin
        lives in. A page too small for the margin is left alone."""
        rotation = 0
        pw = ph = 0.0
        if self.document is not None and 0 <= page < self.document.page_count:
            rect = self.document.doc[page].rect
            pw, ph = float(rect.width), float(rect.height)
            rotation = self.document.page_rotation(page)
        elif 0 <= page < len(self._layers):
            layer = self._layers[page]
            pw, ph = layer.pt_size
            rotation = layer.rotation
        if rotation % 180 == 90:
            pw, ph = ph, pw
        if pw <= 2 * margin or ph <= 2 * margin:
            return origin
        return (min(max(origin[0], margin), pw - margin),
                min(max(origin[1], margin), ph - margin))

    def _can_paste(self) -> bool:
        """Whether ``paste()`` has anything to paste (drives the context
        menu's Paste enablement): the in-process box clipboard, the system
        clipboard's runs payload, or non-blank system text (§4.4)."""
        if self._clipboard:
            return True
        md = self._clipboard_mime()
        if md is None:
            return False
        try:
            if md.hasFormat(RUNS_MIME):
                return True
            return bool(md.hasText() and md.text().strip())
        except Exception:  # noqa: BLE001 - clipboard must not crash chrome
            return False

    # =====================================================================
    # Editor-level clipboard (clipboard interop, §4.2 / §4.3)
    # =====================================================================
    @classmethod
    def _extract_selection_runs(cls, editor) -> tuple:
        """(text, bold, italic) runs for the editor's CURRENT SELECTION: the
        commit extractor's fragment walk (``_extract_editor_runs``) clipped to
        [selectionStart, selectionEnd). A fragment without an explicit
        weight/slant property inherits the editor's BASE font, exactly like
        the commit path, so what's copied is what the bake would draw."""
        cursor = editor.textCursor()
        if not cursor.hasSelection():
            return ()
        return cls._extract_editor_runs(
            editor, cursor.selectionStart(), cursor.selectionEnd())

    def _editor_mime_style(self) -> dict:
        """The open editor's box EFFECTIVE style (the model truth, in PDF
        points -- never the editor's zoom-scaled pixel font), for the wire
        payload's ``style`` field."""
        box = self._editor_box
        page = getattr(box, "page_index", self._page_index)
        try:
            eff = self.document.effective_style(page, box) \
                if self.document is not None else {}
        except Exception:  # noqa: BLE001 - a stale box ref must not crash
            eff = {}
        return eff

    def editor_copy_selection(self) -> bool:
        """Copy the open inline editor's text selection to the system
        clipboard as BOTH ``text/plain`` and the x-pdfte-runs payload (§4.2),
        so bold/italic runs survive a paste into another box while foreign
        apps get clean plain text. The plain text is the joined run text (one
        source of truth for both formats). Returns True when copied."""
        if self._editor is None:
            return False
        runs = self._extract_selection_runs(self._editor)
        if not runs:
            return False
        text = "".join(t for t, _, _ in runs)
        md = QMimeData()
        md.setText(text)
        md.setData(RUNS_MIME, QByteArray(
            encode_runs_mime(text, runs, self._editor_mime_style())))
        return self._set_clipboard(md)

    def editor_cut_selection(self) -> bool:
        """Cut from the open editor: the §4.2 copy, then remove the selection.
        The removal participates in the eventual commit (one undo step via the
        normal Edit/RichEditCommand path); the copy itself is not undoable
        state -- same contract as the box-level Cut (§4.5)."""
        if not self.editor_copy_selection():
            return False
        cursor = self._editor.textCursor()
        cursor.removeSelectedText()
        self._editor.setTextCursor(cursor)
        return True

    def editor_paste(self) -> bool:
        """Paste INTO the open inline editor (§4.3), replacing any selection.

        (a) Our runs payload -> insert per-run with ``QTextCharFormat``
            carrying bold/italic ONLY: family/size/color stay the EDITOR's
            (the style-sanity rule -- foreign fonts never enter a box). A
            NewBox editor (uniform-style by model contract) takes the plain
            text instead.
        (b) Plain text -> inserted with whitespace normalized (line breaks /
            tabs collapse to single spaces; the model has no hard breaks --
            wrap owns line breaks).
        Qt's native rich-HTML paste NEVER runs (the editor intercepts the
        key; the window action routes here). Returns True when inserted."""
        if self._editor is None:
            return False
        md = self._clipboard_mime()
        if md is None:
            return False
        decoded = None
        try:
            if md.hasFormat(RUNS_MIME):
                decoded = decode_runs_mime(bytes(md.data(RUNS_MIME)))
        except Exception:  # noqa: BLE001 - foreign data must not crash paste
            decoded = None
        cursor = self._editor.textCursor()
        if decoded is not None:
            if getattr(self._editor_box, "box_id", None) is not None:
                # NewBox editor: uniform style only -> plain text.
                cursor.insertText(decoded["text"])
            else:
                for t, b, i in decoded["runs"]:
                    fmt = QTextCharFormat()
                    fmt.setFontWeight(QFont.Bold if b else QFont.Normal)
                    fmt.setFontItalic(bool(i))
                    cursor.insertText(t, fmt)
            self._editor.setTextCursor(cursor)
            return True
        if md.hasText():
            text = sanitize_pasted_text(md.text())
            if not text:
                return False
            cursor.insertText(text)
            self._editor.setTextCursor(cursor)
            return True
        return False

    @staticmethod
    def _set_clipboard(md: QMimeData) -> bool:
        """Hand ``md`` to the system clipboard; never let clipboard access
        crash editing (offscreen/headless platforms may lack one)."""
        try:
            cb = QApplication.clipboard()
            if cb is None:
                return False
            cb.setMimeData(md)
            return True
        except Exception:  # noqa: BLE001
            return False

    @staticmethod
    def _clipboard_mime():
        """The system clipboard's current QMimeData, or None (same crash
        discipline as ``_set_clipboard``)."""
        try:
            cb = QApplication.clipboard()
            return cb.mimeData() if cb is not None else None
        except Exception:  # noqa: BLE001
            return None

    # =====================================================================
    # Style clipboard ("Bring style...", text-editing UX §3.4)
    # =====================================================================
    def copy_style(self) -> bool:
        """Capture the selected box's EFFECTIVE style on the view's style
        clipboard: family/size/color/bold/italic always, alignment/
        line_spacing only when the source reports them (a ParagraphBox).
        Pure chrome -- no command, no undo entry. Returns True on capture."""
        if self._selection is None or self.document is None:
            return False
        box = self._selection
        page = getattr(box, "page_index", self._page_index)
        try:
            eff = self.document.effective_style(page, box)
        except Exception:  # noqa: BLE001 - a stale box ref must not crash
            return False
        clip = {
            "font_family": eff.get("font_family", "Helvetica"),
            "size": float(eff.get("size", 12.0) or 12.0),
            "color": tuple(eff.get("color", (0.0, 0.0, 0.0))),
            "bold": bool(eff.get("bold", False)),
            "italic": bool(eff.get("italic", False)),
        }
        if eff.get("alignment") is not None:
            clip["alignment"] = eff["alignment"]
        if eff.get("line_spacing") is not None:
            clip["line_spacing"] = eff["line_spacing"]
        self._style_clipboard = clip
        return True

    def paste_style(self) -> bool:
        """Apply the style clipboard to the selection as ONE
        ``boxCommandRequested("style", ...)`` -> one BoxCommand undo step.
        Keys are FILTERED to the target kind: alignment/line_spacing only
        reach a ParagraphBox (``set_style`` routes paragraph layout onto the
        Edit, not StyleOverride; a Span/NewBox would no-op them but the
        filter keeps the command payload honest). Returns True when emitted."""
        if self._selection is None or self._style_clipboard is None:
            return False
        box = self._selection
        filtered = dict(self._style_clipboard)
        if not getattr(box, "is_paragraph", False):
            filtered.pop("alignment", None)
            filtered.pop("line_spacing", None)
        self.boxCommandRequested.emit("style", box, {"overrides": filtered})
        self.styleApplied.emit(box, dict(filtered))
        return True

    # =====================================================================
    # Right-click context menu (text-editing UX §3.4)
    # =====================================================================
    def contextMenuEvent(self, event):
        """Right-click: delegate to the testable ``_context_menu_for``
        factory. ONLY this method execs the menu (the offscreen-modal rule:
        tests introspect the factory, never exec). A None factory result
        routes to super() so an open inline editor keeps Qt's native
        text-editor menu."""
        menu = None
        if self.document is not None:
            menu = self._context_menu_for(self.mapToScene(event.pos()))
        if menu is None:
            super().contextMenuEvent(event)
            return
        event.accept()
        menu.exec(event.globalPos())

    def _context_menu_for(self, scene_pos: QPointF) -> "QMenu | None":
        """Build (without exec'ing) the context menu for ``scene_pos``:

          (i)   inline editor open -> None (Qt's native editor menu);
          (ii)  over a box hotspot or the current selection -> select that
                box, then Edit Text | Copy / Paste / Copy Style / Paste
                Style | Delete;
          (iii) empty canvas -> Paste (enabled iff pasteable) + Add Text Here;
          (iv)  TEXT SELECT armed (§5.2) -> Copy (enabled iff a word
                selection is active) + Select All for the current page; box
                entries are unreachable while the tool owns the canvas.

        The menu is parented to the view; action shortcuts are display hints
        (the real bindings live on the view/window paths they mirror)."""
        if self.document is None or self._editor is not None:
            return None
        if self._mode == MODE_SELECT_TEXT:
            menu = QMenu(self)
            act_copy = menu.addAction("Copy")
            act_copy.setShortcut(QKeySequence.Copy)
            act_copy.setEnabled(self._text_sel is not None)
            act_copy.triggered.connect(self.copy_text_selection)
            act_all = menu.addAction("Select All")
            act_all.setShortcut(QKeySequence.SelectAll)
            act_all.triggered.connect(self._select_all_text_current_page)
            return menu
        box = self._box_at_scene_pos(scene_pos)
        if box is not None and self._is_image_box(box):
            # Placed image (images & signatures §3): no text entries -- the
            # copy/style/edit actions all assume text state. Delete is the
            # one mutation the menu offers (move/resize are direct gestures).
            if not self._is_selected(box):
                self.select_box(box)
            menu = QMenu(self)
            act_delete = menu.addAction("Delete Image")
            act_delete.setShortcut(QKeySequence.Delete)
            act_delete.triggered.connect(self.delete_selected)
            return menu
        if box is not None:
            # Preserve a multi-selection when right-clicking INSIDE it (so Group
            # stays available); otherwise a plain right-click selects the box.
            multi = self.selected_boxes()
            in_multi = (len(multi) >= 2
                        and any(getattr(b, "identity", None)
                                == getattr(box, "identity", None) for b in multi))
            if not in_multi and not self._is_selected(box):
                self.select_box(box)
                multi = self.selected_boxes()
            menu = QMenu(self)
            pos = QPointF(scene_pos)
            # Group: 2+ text boxes selected -> fuse into one editable paragraph.
            text_sel = [b for b in multi if not self._is_image_box(b)]
            if len(text_sel) >= 2:
                act_group = menu.addAction(
                    f"Group {len(text_sel)} Boxes Into One")
                act_group.triggered.connect(
                    lambda *_, bs=list(text_sel): self.groupRequested.emit(bs))
                menu.addSeparator()
            # Ungroup: a single paragraph box -> split back into its lines.
            elif getattr(box, "is_paragraph", False):
                act_ungroup = menu.addAction("Ungroup Into Lines")
                act_ungroup.triggered.connect(
                    lambda *_, b=box: self.ungroupRequested.emit(b))
                menu.addSeparator()
            act_edit = menu.addAction("Edit Text")
            hotspot = self._hotspot_for(box)
            act_edit.setEnabled(hotspot is not None)
            act_edit.triggered.connect(
                lambda *_, hs=hotspot, p=pos: self.begin_edit(
                    hs, scene_point=p))
            menu.addSeparator()
            act_copy = menu.addAction("Copy")
            act_copy.setShortcut(QKeySequence.Copy)
            act_copy.triggered.connect(self.copy_selection)
            act_paste = menu.addAction("Paste")
            act_paste.setShortcut(QKeySequence.Paste)
            act_paste.setEnabled(self._can_paste())
            act_paste.triggered.connect(self.paste)
            act_copy_style = menu.addAction("Copy Style")
            act_copy_style.triggered.connect(self.copy_style)
            act_paste_style = menu.addAction("Paste Style")
            act_paste_style.setEnabled(self._style_clipboard is not None)
            act_paste_style.triggered.connect(self.paste_style)
            menu.addSeparator()
            act_delete = menu.addAction("Delete")
            act_delete.setShortcut(QKeySequence.Delete)
            act_delete.triggered.connect(self.delete_selected)
            return menu
        menu = QMenu(self)
        pos = QPointF(scene_pos)
        act_paste = menu.addAction("Paste")
        act_paste.setShortcut(QKeySequence.Paste)
        act_paste.setEnabled(self._can_paste())
        act_paste.triggered.connect(self.paste)
        act_add = menu.addAction("Add Text Here")
        act_add.triggered.connect(
            lambda *_, p=pos: self._add_text_here(p))
        return menu

    def _box_at_scene_pos(self, scene_pos: QPointF):
        """The box a right-click at ``scene_pos`` targets, mirroring the
        press-routing rule: the topmost SpanHotspot's box wins; a hit on the
        SelectionOverlay / a resize handle (both painted over the selected
        box) IS the selection; else a point inside the selection's scene rect
        still counts (the outline is inflated past the hotspot)."""
        for item in self.scene().items(scene_pos):
            if isinstance(item, (SpanHotspot, ImageHotspot)):
                return item.box        # z-order: text (10) beats images (7)
            if isinstance(item, (_ResizeHandle, SelectionOverlay)):
                if self._selection is not None:
                    return self._selection
        if self._selection is not None and \
                self._span_scene_rect(self._selection).contains(scene_pos):
            return self._selection
        return None

    def _add_text_here(self, scene_pos: QPointF) -> None:
        """Context-menu "Add Text Here": arm the tool, then place at the
        clicked point (the same add flow as a click in ADD_TEXT mode)."""
        self.enter_add_text_mode()
        self._do_add_at(scene_pos)

    # =====================================================================
    # repaint after a model mutation (window's BoxCommand calls this)
    # =====================================================================
    def repaint_box(self, box) -> None:
        """Refresh ONE page to its current staged state after a box mutation
        (text/style/move/resize/delete/add) or an undo/redo (EDITOR_SPEC §3.7,
        REFLOW_SPEC §R3.6).

        Committed edits are baked into the page pixmap by ``render_with_edits``,
        so a refresh re-renders that page through the save pipeline; the on-screen
        result then matches the saved file exactly. In the continuous view only
        the box's OWN page is re-materialized (a perf win over a whole-view
        reload), and the selection overlay is re-established for the fresh box
        object via the identity match in ``_materialize_page``."""
        if not self.document or box is None:
            return
        page = getattr(box, "page_index", None)
        if page is None or not (0 <= page < len(self._layers)):
            # No layer info (e.g. a stale box / the no-layers test path): fall
            # back to a full reload, which is always correct.
            self.reload()
            return
        # Free the page's stale cache entries now. Correctness never needs
        # this (the mutation already changed render_signature, so the old
        # entries can never hit again); it just releases their memory
        # immediately instead of waiting for LRU aging.
        self._render_cache.purge_page(page)
        sel_identity = (self._selection.identity
                        if self._selection is not None else None)
        layer = self._layers[page]
        was_rendered = layer.rendered
        if was_rendered:
            self._dematerialize_page(layer)
        # Re-render only if it is in (or near) the visible band; else leave it
        # dematerialized and it will lazily re-render on scroll-in.
        self._update_lazy_render()
        if not layer.rendered and was_rendered:
            # It was visible before; keep it materialized so the user sees the
            # change immediately even if the band math is borderline.
            self._materialize_page(layer)
        # Re-bind the selection to the fresh object (the box list was rebuilt).
        if sel_identity is not None:
            fresh = self._box_by_identity(sel_identity)
            if fresh is not None:
                self._selection = fresh
                self._refresh_overlay()
            else:
                self._selection = None
                self._remove_overlay()
                self.selectionChanged.emit(None)
        self._sync_current_page_mirror()

    # Backward-compatible alias: the original window pushed text edits through
    # repaint_span(span). Keep it so EditRunCommand keeps working unchanged.
    def repaint_span(self, span) -> None:
        self.repaint_box(span)

    def repaint_page(self, page_index: int) -> None:
        """Refresh ONE page to its current staged state by index (annotations
        & markup §4.3): the ``repaint_box`` cycle keyed by page instead of by
        box -- the window's AnnotCommand calls this after every annot
        mutation / undo / redo (an annotation has no box to key on). Any box
        selection on the page is re-bound by identity, exactly like
        ``repaint_box``."""
        if not self.document:
            return
        if not (0 <= page_index < len(self._layers)):
            self.reload()                  # stale index: full reload is correct
            return
        self._render_cache.purge_page(page_index)
        sel_identity = (self._selection.identity
                        if self._selection is not None else None)
        layer = self._layers[page_index]
        was_rendered = layer.rendered
        if was_rendered:
            self._dematerialize_page(layer)
        self._update_lazy_render()
        if not layer.rendered and was_rendered:
            self._materialize_page(layer)
        if sel_identity is not None:
            fresh = self._box_by_identity(sel_identity)
            if fresh is not None:
                self._selection = fresh
                self._refresh_overlay()
            else:
                self._selection = None
                self._remove_overlay()
                self.selectionChanged.emit(None)
        # Re-bind the ANNOT selection by identity (annotations §4.3): the
        # records were rebuilt with the page; a vanished record (deleted /
        # override-deleted) clears the selection. The selections are
        # mutually exclusive, so select_annot's clear_selection is a no-op
        # whenever this branch runs.
        if self._annot_selection is not None:
            fresh_rec = self._annot_record_by_identity(
                self._annot_selection.identity)
            if fresh_rec is not None:
                self.select_annot(fresh_rec)
            else:
                self._remove_annot_overlay()
                self._annot_selection = None
                self.annotSelectionChanged.emit(None)
        self._sync_current_page_mirror()

    # =====================================================================
    # Form fill UI (forms §3). The view never stages a fill: every commit
    # exits through ONE ``formCommandRequested(field, {"value": ...})`` --
    # checkbox toggle, radio pick, combo pick, text-editor commit alike --
    # and the window funnels it into ONE FormFieldCommand on the undo stack.
    # =====================================================================
    def _field_hotspot_factory(self, layer, _view) -> list:
        """The registered page-item factory (perf foundation M4b): one
        FieldHotspot per FILLABLE widget on the page at Z_FORM_HOTSPOT,
        tracked on ``layer.extra_items`` for symmetric dematerialize.
        Readonly/button/signature/listbox widgets get no hotspot
        (``FormField.fillable``); a form-free document contributes zero
        items, so the default scene census is unchanged."""
        doc = self.document
        if doc is None or not getattr(doc, "has_form", False):
            return []
        try:
            fields = doc.form_fields(layer.page_index)
        except Exception:  # noqa: BLE001 - widget read must not break render
            return []
        return [FieldHotspot(self._form_scene_rect(f), f, self)
                for f in fields if f.fillable]

    def _form_scene_rect(self, field) -> QRectF:
        """A FormField's UNROTATED text-space rect -> scene rect: widget
        rects share the span coordinate space (forms §0 probe), so the
        annot corner-mapping through ``_scene_point`` serves unchanged."""
        return self._annot_scene_rect(field.page_index, field.rect)

    def _form_field_by_identity(self, identity):
        """The CURRENT FormField for ``identity`` (fields are rebuilt with
        the working doc, so live refs can go stale), or None."""
        if identity is None or self.document is None:
            return None
        try:
            fields = self.document.form_fields(identity[0])
        except Exception:  # noqa: BLE001 - a stale page index must not crash
            return None
        for f in fields:
            if f.identity == identity:
                return f
        return None

    def _set_focused_field(self, field) -> None:
        """Set (or clear, ``None``) the focused form field and repaint the
        field hotspots so exactly one carries the accent outline. Identity
        lives on the model FormField, so the ring survives the per-mutation
        hotspot rebuilds."""
        if field is self._focused_field:
            return
        self._focused_field = field
        for layer in self._layers:
            for it in layer.extra_items:
                if isinstance(it, FieldHotspot):
                    it.update()

    def _on_field_press(self, hotspot: "FieldHotspot",
                        scene_pos: QPointF) -> bool:
        """A press landed on a form-field hotspot (forms §3). SELECT mode
        only -- armed tools (text-select/crop/markup/note/ink/shape) keep
        their pass-through gesture semantics, exactly like the annot
        hotspots. Clears the box/annot selections (a field press focuses
        the FIELD), then routes by kind: checkbox toggles, a radio kid
        picks its on-state (no toggle-off), a combo pops its options menu,
        a text field mounts the inline FormFieldEditor."""
        if self.document is None or self._mode != MODE_SELECT:
            return False
        self._flush_editor()               # commits BOTH open editors
        self._cancel_drag()
        field = self._form_field_by_identity(hotspot.identity)
        if field is None or not field.fillable:
            return False
        self.clear_selection()
        self.clear_annot_selection()
        self._set_focused_field(field)
        self.setFocus(Qt.MouseFocusReason)
        self._route_field_fill(field, hotspot)
        return True

    def _route_field_fill(self, field,
                          hotspot: "FieldHotspot | None" = None) -> None:
        """ONE fill routing for a field activation -- mouse press and
        keyboard (Space/Return on the focused field, M3) alike: a checkbox
        toggles, a radio kid picks its on-state (no toggle-off), a combo
        pops its options menu (the injectable provider seam), a text field
        mounts the inline FormFieldEditor."""
        if field.kind == "checkbox":
            current = self.document.effective_form_value(
                field.page_index, field)
            self.formCommandRequested.emit(
                field, {"value": not bool(current)})
        elif field.kind == "radio":
            # Picking the selected kid again is dropped by the window's
            # no-op guard (PDF radio groups have no toggle-off).
            self.formCommandRequested.emit(
                field, {"value": field.on_state})
        elif field.kind == "combo":
            chosen = self._combo_menu_provider(
                field, self._form_scene_rect(field))
            if chosen is not None:
                self.formCommandRequested.emit(field, {"value": chosen})
        else:                              # text (callers gate on fillable)
            self._mount_form_editor(
                field, hotspot or self._field_hotspot_for(field))

    def _show_combo_menu(self, field, scene_rect: QRectF):
        """The DEFAULT ``_combo_menu_provider``: a QMenu of the combo's
        options anchored under the field, the current effective value
        checked. Returns the chosen option string, or None (dismissed).
        Tests replace the provider seam instead of exec()ing this."""
        menu = QMenu(self)
        current = self.document.effective_form_value(field.page_index, field)
        for option in field.options:
            act = menu.addAction(str(option))
            act.setCheckable(True)
            act.setChecked(str(option) == current)
        pos = self.viewport().mapToGlobal(
            self.mapFromScene(scene_rect.bottomLeft()))
        chosen = menu.exec(pos)
        return chosen.text() if chosen is not None else None

    def _mount_form_editor(self, field, hotspot: "FieldHotspot | None" = None
                           ) -> None:
        """Mount the inline FORM editor over a text field's rect (forms §3):
        a plain-text QGraphicsTextItem in Helvetica at the widget's own
        fontsize (11pt when auto-sized), over a SOLID WHITE cover at Z_COVER
        -- field interiors are white, so the background sampler is wrong
        here by design. Seeds from ``effective_form_value`` (staged wins),
        selected so typing replaces."""
        if self.document is None:
            return
        self._flush_editor()               # a previous field commits first
        self._teardown_form_editor()
        page = field.page_index
        rect = self._form_scene_rect(field)
        value = self.document.effective_form_value(page, field)
        text = "" if value is None else str(value)

        cover = QGraphicsRectItem(rect.adjusted(-1.0, -1.0, 1.0, 1.0))
        cover.setBrush(QBrush(QColor("#FFFFFF")))
        cover.setPen(QPen(Qt.NoPen))
        cover.setZValue(Z_COVER)
        self.scene().addItem(cover)
        self._form_editor_cover = cover

        editor = FormFieldEditor(field, text, self)
        font = QFont("Helvetica")
        # Pixel sizing per the font_engine.py precedent: widget fontsize is
        # in PDF points, scene px = points * zoom (0 = auto-size -> 11pt).
        font.setPixelSize(max(1, round((field.text_fontsize or 11.0)
                                       * self._zoom)))
        editor.setFont(font)
        editor.document().setDefaultFont(font)
        editor.setDefaultTextColor(QColor("#000000"))
        if field.multiline:
            # Soft-wrap to the field's width so typing reads like the
            # regenerated appearance will (MuPDF re-wraps at bake).
            editor.setTextWidth(max(1.0, rect.width()))
        # Anchor at the field's UNROTATED top-left corner mapped through
        # ``_scene_point`` and rotate with the page, mirroring
        # ``_place_text_item`` so typing flows along the displayed axes.
        editor.setRotation(float(self._rotation_for(page) % 360))
        editor.setPos(self._scene_point(field.rect[0], field.rect[1],
                                        page_index=page))
        self.scene().addItem(editor)
        self._form_editor = editor
        self._form_editor_seed = text
        self._form_editor_hotspot = hotspot
        if hotspot is not None:
            hotspot._editing = True
            hotspot.update()
        self._set_focused_field(field)

        editor.setFocus(Qt.MouseFocusReason)
        cursor = editor.textCursor()
        cursor.select(QTextCursor.Document)
        editor.setTextCursor(cursor)

    def commit_form_editor(self) -> None:
        """Commit the open form editor (Return / focus-out / flush): tear it
        down and emit ONE ``formCommandRequested`` -- only when the text
        actually changed from its mount-time seed, so a click-in/click-out
        leaves no command (the window's no-op guard backstops this)."""
        if self._form_editor is None or self._form_committing \
                or self._closing_form_editor:
            return
        self._form_committing = True
        editor = self._form_editor
        field = editor.field
        text = editor.toPlainText()
        seed = self._form_editor_seed
        self._teardown_form_editor()
        self._form_committing = False
        if text != seed:
            self.formCommandRequested.emit(field, {"value": text})

    def cancel_form_editor(self) -> None:
        """Abandon the open form editor (Esc): stage nothing. The field
        keeps its focus ring (Acrobat behavior)."""
        if self._form_editor is None or self._closing_form_editor:
            return
        self._teardown_form_editor()

    def _teardown_form_editor(self) -> None:
        """Remove the form editor + its white cover without emitting a
        commit (the form mirror of ``_teardown_editor``)."""
        if self._form_editor is None and self._form_editor_cover is None:
            return
        self._closing_form_editor = True
        try:
            if self._form_editor is not None \
                    and self._form_editor.scene() is not None:
                self.scene().removeItem(self._form_editor)
            if self._form_editor_cover is not None \
                    and self._form_editor_cover.scene() is not None:
                self.scene().removeItem(self._form_editor_cover)
            hs = self._form_editor_hotspot
            if hs is not None:
                hs._editing = False
                if hs.scene() is not None:
                    hs.update()
        finally:
            self._form_editor = None
            self._form_editor_cover = None
            self._form_editor_hotspot = None
            self._form_editor_seed = ""
            self._closing_form_editor = False

    # --- Tab-order navigation (forms §3, M3) ---------------------------
    def _field_hotspot_for(self, field) -> "FieldHotspot | None":
        """The live FieldHotspot for ``field`` across all materialized
        layers (identity match), or None when its page is not built."""
        ident = getattr(field, "identity", None)
        for layer in self._layers:
            for it in layer.extra_items:
                if isinstance(it, FieldHotspot) and it.identity == ident:
                    return it
        return None

    def focus_field(self, field) -> None:
        """Focus ONE form field (forms §3, M3): jump to its page when it
        lives on another one (the cross-page Tab wrap follows the
        viewport), pull its rect into view, and set the focus ring. A text
        field auto-mounts the inline editor; buttons (checkbox / radio /
        combo) keep the ring and wait for Space/Return
        (``_activate_focused_field``)."""
        if self.document is None:
            return
        # Re-fetch by identity: live FormField refs go stale across the
        # working-doc rebuilds, exactly like the press routing.
        fresh = self._form_field_by_identity(getattr(field, "identity",
                                                     None))
        if fresh is None or not fresh.fillable:
            return
        # A previous field's open editor commits first (idempotent with
        # the form_tab_step flush; direct focus_field callers need it).
        self._flush_editor()
        if fresh.page_index != self._page_index:
            self.set_page(fresh.page_index)
        self.ensureVisible(self._form_scene_rect(fresh), 48, 48)
        self._set_focused_field(fresh)
        if fresh.kind == "text":
            self._mount_form_editor(fresh, self._field_hotspot_for(fresh))
        else:
            # Keys (Space/Return/Tab/Esc) land at the view level.
            self.setFocus(Qt.OtherFocusReason)

    def form_tab_step(self, forward: bool = True) -> bool:
        """Advance the form focus along ``document.form_tab_order()``
        (forms §3, M3): an OPEN form editor commits first through the
        normal flush (the mid-edit Tab is ONE command), then the next /
        previous fillable field takes focus, WRAPPING across pages -- the
        last field tabs to the first. Anchors on the editing field, else
        the focused one; with neither, Tab enters the first field (Backtab
        the last). Returns True when a field took focus (key consumed)."""
        doc = self.document
        if doc is None or not getattr(doc, "has_form", False) \
                or self._mode != MODE_SELECT:
            return False
        current = self._form_editor.field if self._form_editor is not None \
            else self._focused_field
        ident = getattr(current, "identity", None)
        self._flush_editor()               # commits a half-typed value
        order = doc.form_tab_order()
        if not order:
            return False
        idx = None
        if ident is not None:
            for i, f in enumerate(order):
                if f.identity == ident:
                    idx = i
                    break
        if idx is None:
            nxt = order[0] if forward else order[-1]
        else:
            step = 1 if forward else -1
            nxt = order[(idx + step) % len(order)]
        self.focus_field(nxt)
        return True

    def _activate_focused_field(self) -> bool:
        """Space/Return on the FOCUSED field (forms §3, M3): route the
        activation through the one fill dispatcher -- checkbox toggles,
        radio kid picks, combo opens its menu, a text field (focused but
        not editing, e.g. after an Esc cancel) re-mounts its editor.
        Returns True when an activation was routed."""
        focused = self._focused_field
        if focused is None or self._form_editor is not None \
                or self.document is None:
            return False
        field = self._form_field_by_identity(getattr(focused, "identity",
                                                     None))
        if field is None or not field.fillable:
            return False
        self._route_field_fill(field)
        return True

    def focusNextPrevChild(self, next: bool) -> bool:  # noqa: A002 - Qt API
        """Tab/Backtab belong to FORM tab-order navigation while a form
        doc is open in SELECT mode (forms §3, M3): returning False makes
        Qt deliver the press as a KEY event to ``keyPressEvent`` (the
        ``_key_select`` Tab row) instead of moving widget focus -- the
        probe-verified route, since the scene declines the key once the
        editor sets ``tabChangesFocus``. Every other state keeps the
        normal widget focus chain."""
        if self.document is not None \
                and getattr(self.document, "has_form", False) \
                and self._mode == MODE_SELECT:
            return False
        return super().focusNextPrevChild(next)

    # =====================================================================
    # Mouse routing (the view owns drag state, EDITOR_SPEC §3.3)
    # =====================================================================
    def _on_box_press(self, hotspot: SpanHotspot, scene_pos: QPointF) -> bool:
        """A press landed on a box hotspot. Returns True if the view handled it.

        In ADD_TEXT mode a press over a box is treated as an empty-canvas add at
        that point (the user armed the tool; let them place anywhere). In SELECT
        mode: select the box (commit/flush any open editor first), and arm a
        potential body-drag MOVE that promotes on the first real mouse move."""
        if self.document is None:
            return False
        # TEXT SELECT / CROP / PLACE_IMAGE modes (§5.2 / doc tools §2.6 /
        # images §3): hotspots pass presses through -- the words under the
        # box are selectable (or the rubber rect / placement starts there);
        # the box is not targetable (no select, no drag). The view's
        # mousePressEvent routes the press to the armed mode's handler;
        # returning False here keeps any item-level path inert too. ANNOT
        # tools (markup band / note click, §4.2) use the same pass-through:
        # the gesture lands over text, never a box select/drag.
        if self._mode in (MODE_SELECT_TEXT, MODE_CROP, MODE_PLACE_IMAGE) \
                or self._annot_tool_armed():
            return False
        box = hotspot.box
        if self._mode == MODE_ADD_TEXT:
            self._do_add_at(scene_pos)
            return True
        # Shift+click: toggle this box in the multi-selection (for Group), not a
        # fresh single select. Text boxes only -- images/fields don't group.
        if (bool(QApplication.keyboardModifiers() & Qt.ShiftModifier)
                and not self._is_image_box(box)):
            self._flush_editor()
            self._toggle_multi(box)
            return True
        # Don't start a fresh selection on the box currently being text-edited.
        if self._editor is not None and self._editor_box is not None \
                and self._is_same_box(box, self._editor_box):
            return False
        self._flush_editor()
        # A plain (non-Shift) press drops any multi-selection.
        if self._multi_extra:
            self._clear_multi()
            self.multiSelectionChanged.emit(0)
        # Select (or reselect) this box. A press that lands on the box that was
        # ALREADY selected is a candidate "second click" -> if it does not turn
        # into a drag, the release promotes it into a text edit with the caret
        # at the click point (REFLOW_SPEC click-to-edit gesture).
        already = self._is_selected(box)
        if not already:
            self.select_box(box)
        else:
            self.setFocus(Qt.MouseFocusReason)
        # No second-click-to-edit promotion for images (images §3): a placed
        # image never mounts the inline editor, so a pure re-click is inert.
        self._press_on_selected = already and not self._is_image_box(box)
        self._press_hotspot = hotspot
        # Arm a body MOVE drag; it promotes only after the move slop.
        self._arm_move(box, scene_pos)
        return True

    def _on_box_double_click(self, hotspot: SpanHotspot, scene_pos: QPointF) -> bool:
        """Double-click a box -> edit its text (EDITOR_SPEC §3.3). Declines
        while ADD_TEXT, TEXT SELECT, CROP, PLACE_IMAGE, or an ANNOT tool is
        armed (the armed tool owns the canvas; never begin_edit)."""
        if self.document is None or self._mode in (MODE_ADD_TEXT,
                                                   MODE_SELECT_TEXT,
                                                   MODE_CROP,
                                                   MODE_PLACE_IMAGE) \
                or self._annot_tool_armed():
            return False
        self._cancel_drag()
        box = hotspot.box
        if not self._is_selected(box):
            self.select_box(box)
        self.begin_edit(hotspot, scene_x=scene_pos.x(), scene_point=scene_pos)
        return True

    def _on_handle_press(self, handle_id: str, scene_pos: QPointF) -> bool:
        """A press landed on a resize handle of the selected box. An image
        selection takes the RECT-resize drag (images & signatures §3); text
        boxes keep the proportional font-scale resize."""
        if self.document is None or self._selection is None:
            return False
        self._flush_editor()
        sel = self._selection
        if self._is_image_box(sel):
            self._begin_image_resize(handle_id, scene_pos)
        elif isinstance(sel, NewBox):
            # NewBoxes own their geometry -> keep the proportional scale resize.
            self._begin_resize(handle_id, scene_pos)
        else:
            # Existing text boxes (Span / ParagraphBox): drag resizes the FRAME
            # and the text re-wraps to it at CONSTANT font size (never scaled).
            self._begin_frame_resize(handle_id, scene_pos)
        return True

    def mousePressEvent(self, event):
        # Items (hotspots/handles) accept their own presses first; this fires
        # only for presses on EMPTY canvas (sheet/gutter). The per-mode press
        # handler from ``self._mode_handlers`` owns the deselect / add-on-empty
        # behavior (EDITOR_SPEC §3.3; dispatch = perf foundation M4c).
        if event.button() == Qt.LeftButton:
            # Alt+click forces ANNOT-FIRST hit-testing (annotations §4.3):
            # text hotspots normally win overlapping clicks (z 10 vs 5), so
            # Alt is the escape hatch to select a markup that sits under
            # editable text. SELECT mode only; armed tools keep their own
            # press semantics.
            if (self._mode == MODE_SELECT and self.document is not None
                    and event.modifiers() & Qt.AltModifier):
                for it in self.items(event.position().toPoint()):
                    if isinstance(it, AnnotHotspot):
                        if self._on_annot_press(
                                it,
                                self.mapToScene(event.position().toPoint())):
                            event.accept()
                            return
                        break
            item = self.itemAt(event.position().toPoint())
            # A press whose topmost item is a hotspot/handle is box-handled. The
            # SelectionOverlay is painted OVER the selected box (z above the
            # hotspot), so a press that lands on it is a press ON the selection,
            # NOT empty canvas -- treat it as box-handled too, otherwise the
            # overlay would shadow the box and a second click would deselect
            # instead of re-targeting it (this is what the click-to-edit second
            # click relies on; the underlying hotspot still gets the press).
            # The annot items join the tuple for the same reason: an
            # AnnotHotspot press is ITEM-handled (selection §4.3), and the
            # never-hit-tested AnnotSelectionOverlay must not read as empty
            # canvas (the hotspot beneath it gets the press). FieldHotspot
            # joins it too (forms §3): a field press is item-handled in
            # SELECT mode, and the armed-tool flip below restores the
            # pass-through for text-select/crop/annot gestures over fields.
            handled_by_box = isinstance(
                item, (SpanHotspot, ImageHotspot, _ResizeHandle,
                       SelectionOverlay, AnnotHotspot, AnnotSelectionOverlay,
                       FieldHotspot))
            # TEXT SELECT, CROP, and PLACE_IMAGE claim presses even over a
            # box hotspot (§5.2 / doc tools §2.6 / images §3): the hotspot
            # declines them too (_on_box_press returns False in these
            # modes), so routing to the mode handler here keeps ONE press
            # path -- word-anchoring, rubber-anchoring, or image placement
            # -- wherever the press lands. No overlay/handles exist while
            # any of them is armed (entering cleared the selection). ANNOT
            # tools claim presses the same way (annotations §4.2): the band
            # drag / note click must work over text, i.e. over hotspots.
            if handled_by_box and (
                    self._mode in (MODE_SELECT_TEXT, MODE_CROP,
                                   MODE_PLACE_IMAGE)
                    or self._annot_tool_armed()):
                handled_by_box = False
            if not handled_by_box:
                handler = self._mode_handlers.get(self._mode, {}).get("press")
                if handler is not None and handler(event):
                    return
        super().mousePressEvent(event)

    # --- per-mode press handlers (perf foundation M4c) --------------------
    def _press_select(self, event) -> bool:
        """Empty-canvas press in SELECT mode: deselect (unless an inline editor
        is open -- its own focus-out/commit flow owns that press). Always
        returns False so the press continues to the scene/super()."""
        if self._editor is None:
            self._cancel_drag()
            self.clear_selection()
            self.clear_annot_selection()
            # Empty canvas also drops the form-field focus ring (forms §3);
            # a mounted form editor commits through its own focus-out.
            self._set_focused_field(None)
            self.setFocus(Qt.MouseFocusReason)
        return False

    def _press_add_text(self, event) -> bool:
        """Empty-canvas press in ADD_TEXT mode: place a new box at the click
        point (the user armed the tool; let them place anywhere)."""
        self._do_add_at(self.mapToScene(event.position().toPoint()))
        event.accept()
        return True

    def mouseMoveEvent(self, event):
        # A live text-select drag extends the word range (§5.2); it can never
        # coexist with a box drag (entering the mode cancelled drag state).
        if self._text_drag_page is not None:
            self._update_text_drag(self.mapToScene(event.position().toPoint()))
            event.accept()
            return
        # A live markup band drag stretches the accent band (annotations
        # §4.3); mutually exclusive with both drags above by mode.
        if self._markup_drag is not None:
            self._update_markup_band(
                self.mapToScene(event.position().toPoint()))
            event.accept()
            return
        # A live INK stroke appends decimated points (§4.3, M3); a live
        # SHAPE drag stretches its rubber-band preview. Both exclusive with
        # every other drag by mode.
        if self._ink_drag is not None:
            self._update_ink_drag(self.mapToScene(event.position().toPoint()))
            event.accept()
            return
        if self._shape_drag is not None:
            self._update_shape_drag(
                self.mapToScene(event.position().toPoint()))
            event.accept()
            return
        # A live ANNOT move drag rides the dashed outline (§4.3); exclusive
        # with the box drag below (selections are mutually exclusive).
        if self._annot_drag is not None:
            self._update_annot_drag(
                self.mapToScene(event.position().toPoint()))
            event.accept()
            return
        # A live crop drag grows the rubber rect (doc tools §2.6); mutually
        # exclusive with the other drags for the same reason.
        if self._crop_page is not None:
            self._update_crop_rubber(
                self.mapToScene(event.position().toPoint()))
            event.accept()
            return
        # A live image placement drag grows the aspect-locked rubber rect
        # (images & signatures §3); exclusive with the others by mode.
        if self._image_place_drag is not None:
            self._update_image_place_rubber(
                self.mapToScene(event.position().toPoint()))
            event.accept()
            return
        if self._drag_kind is not None:
            self._update_drag(self.mapToScene(event.position().toPoint()))
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self._text_drag_page is not None and event.button() == Qt.LeftButton:
            self._update_text_drag(self.mapToScene(event.position().toPoint()))
            self._end_text_drag()
            event.accept()
            return
        if self._markup_drag is not None and event.button() == Qt.LeftButton:
            self._finish_markup_drag(
                self.mapToScene(event.position().toPoint()))
            event.accept()
            return
        if self._ink_drag is not None and event.button() == Qt.LeftButton:
            self._finish_ink_drag(
                self.mapToScene(event.position().toPoint()))
            event.accept()
            return
        if self._shape_drag is not None and event.button() == Qt.LeftButton:
            self._finish_shape_drag(
                self.mapToScene(event.position().toPoint()))
            event.accept()
            return
        if self._annot_drag is not None and event.button() == Qt.LeftButton:
            self._finish_annot_drag(
                self.mapToScene(event.position().toPoint()))
            event.accept()
            return
        if self._crop_page is not None and event.button() == Qt.LeftButton:
            self._finish_crop_drag(
                self.mapToScene(event.position().toPoint()))
            event.accept()
            return
        if self._image_place_drag is not None \
                and event.button() == Qt.LeftButton:
            self._finish_image_place(
                self.mapToScene(event.position().toPoint()))
            event.accept()
            return
        if self._drag_kind is not None and event.button() == Qt.LeftButton:
            self._finish_drag(self.mapToScene(event.position().toPoint()))
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event):
        # TEXT SELECT double-click = the word under the cursor (§5.2),
        # handled at the VIEW level: hotspot double-clicks must not promote
        # into begin_edit while the tool is armed (_on_box_double_click
        # declines), and the recorded (time, point) arms the triple-click
        # line promote in _press_select_text. Every other mode routes to
        # super() so the topmost item (e.g. the inline editor, M1 §2.2)
        # receives the event -- an editor is never open in this mode.
        if self._mode == MODE_SELECT_TEXT and self.document is not None \
                and event.button() == Qt.LeftButton:
            scene_pos = self.mapToScene(event.position().toPoint())
            page = self._page_at_scene_y(scene_pos.y())
            idx = self._word_index_at(page, scene_pos)
            if idx is not None:
                self._end_text_drag()
                self._set_text_selection(page, idx, idx)
            self._text_last_double = (time.monotonic(), QPointF(scene_pos))
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    @staticmethod
    def _is_same_box(a, b) -> bool:
        return (a is not None and b is not None
                and getattr(a, "identity", None) == getattr(b, "identity", None))

    @staticmethod
    def _is_image_box(box) -> bool:
        """Duck test for an image-like box (images & signatures §3): the
        identity tag is "img" (staged ImageBox) or "xim" (existing page
        image, M3) -- the dispatch convention the window mirrors."""
        ident = getattr(box, "identity", None)
        return (isinstance(ident, tuple) and len(ident) >= 2
                and ident[1] in ("img", "xim"))

    @staticmethod
    def _is_existing_image(box) -> bool:
        """Duck test for an EXISTING page image (identity tag "xim", M3):
        the subset of image-like boxes whose gestures route to the xim
        kinds (delete = scoped redaction stage; move = the window's
        delete+reinsert macro; resize handles never mount)."""
        ident = getattr(box, "identity", None)
        return (isinstance(ident, tuple) and len(ident) >= 2
                and ident[1] == "xim")

    # =====================================================================
    # Drag: MOVE (body) + RESIZE (handle) (EDITOR_SPEC §3.6)
    # =====================================================================
    def _arm_move(self, box, scene_pos: QPointF) -> None:
        self._drag_kind = "move"
        self._drag_box = box
        self._drag_start_scene = QPointF(scene_pos)
        self._drag_armed = False           # not yet past the slop
        self._drag_start_rect = self._span_scene_rect(box)

    def _begin_resize(self, handle_id: str, scene_pos: QPointF) -> None:
        box = self._selection
        self._drag_kind = "resize"
        self._drag_box = box
        self._drag_handle = handle_id
        self._drag_start_scene = QPointF(scene_pos)
        self._drag_armed = True
        rect = self._span_scene_rect(box)
        self._drag_start_rect = rect
        # The EFFECTIVE size at drag start (folds prior resizes/size-sets),
        # mirroring the model's resize fold (resize_box: target = effective
        # size x scale) -- it feeds the live "13.0 pt -> 15.5 pt" readout.
        try:
            eff = self.document.effective_style(
                getattr(box, "page_index", self._page_index), box)
            self._drag_start_size = float(eff.get("size", box.size)
                                          or box.size)
        except Exception:  # noqa: BLE001 - readout only; never block a drag
            self._drag_start_size = float(box.size)
        # Anchor = the OPPOSITE corner of the dragged handle, in scene coords,
        # so the box scales from a fixed point (proportional resize, §6). Convert
        # it to PDF points for the model's resize anchor.
        page = getattr(box, "page_index", self._page_index)
        anchor_scene = self._opposite_corner_scene(rect, handle_id)
        self._drag_anchor_pdf = self._pdf_point(anchor_scene, page)
        self._drag_start_diag = max(
            _MIN_RESIZE_DIAG,
            self._diag(anchor_scene, self._dragged_corner_scene(rect, handle_id)))
        # The model grows the run's font FROM ITS BASELINE ORIGIN (resize is
        # size-only; the baseline stays put), and effective_bbox scales the box
        # about that origin. Remember the baseline scene point so the live preview
        # scales about the SAME point -- otherwise the dashed preview drifts to
        # the opposite corner while the committed text grows from the baseline
        # (the preview-vs-commit drift the review flagged). EDITOR_SPEC §3.6.
        self._drag_baseline_scene = self._scene_point(
            *self.document.effective_origin(page, box), page_index=page) \
            if self.document is not None else QPointF(rect.left(), rect.bottom())

    def _begin_image_resize(self, handle_id: str, scene_pos: QPointF) -> None:
        """Arm the RECT-semantics resize for the selected image (images &
        signatures §3): corner drag = free resize, Shift = keep aspect,
        edge drag = one axis. Reuses the shared drag-state owner with its
        own kind, so Esc-abort / preview teardown ride the existing paths."""
        box = self._selection
        self._drag_kind = "img_resize"
        self._drag_box = box
        self._drag_handle = handle_id
        self._drag_start_scene = QPointF(scene_pos)
        self._drag_armed = True
        self._drag_start_rect = self._image_scene_rect(box)

    def _image_resize_rect_for(self, scene_pos: QPointF) -> QRectF:
        """The live scene rect for the image resize: the dragged corner /
        edge follows the cursor (the opposite side stays fixed), Shift on a
        corner locks the START aspect (uniform scale about the fixed
        corner), and both edges floor at ``_IMAGE_MIN_RESIZE_PX`` so the
        rect can never invert under the cursor."""
        r = QRectF(self._drag_start_rect)
        h = self._drag_handle or ""
        left, top = r.left(), r.top()
        right, bottom = r.right(), r.bottom()
        if "w" in h:
            left = min(scene_pos.x(), right - _IMAGE_MIN_RESIZE_PX)
        if "e" in h:
            right = max(scene_pos.x(), left + _IMAGE_MIN_RESIZE_PX)
        if "n" in h:
            top = min(scene_pos.y(), bottom - _IMAGE_MIN_RESIZE_PX)
        if "s" in h:
            bottom = max(scene_pos.y(), top + _IMAGE_MIN_RESIZE_PX)
        rect = QRectF(QPointF(left, top), QPointF(right, bottom))
        try:
            shift = bool(QApplication.keyboardModifiers() & Qt.ShiftModifier)
        except Exception:  # noqa: BLE001 - modifier read must not kill a drag
            shift = False
        if shift and h in _CORNER_HANDLES and self._drag_start_rect.width() \
                and self._drag_start_rect.height():
            # Keep-aspect: uniform scale about the FIXED corner, using the
            # larger of the two axis scales so the rect tracks the cursor.
            sx = rect.width() / self._drag_start_rect.width()
            sy = rect.height() / self._drag_start_rect.height()
            s = max(sx, sy)
            w = self._drag_start_rect.width() * s
            ht = self._drag_start_rect.height() * s
            anchor_x = right if "w" in h else left
            anchor_y = bottom if "n" in h else top
            x0 = anchor_x - w if "w" in h else anchor_x
            y0 = anchor_y - ht if "n" in h else anchor_y
            rect = QRectF(x0, y0, w, ht)
        return rect.normalized()

    def _begin_frame_resize(self, handle_id: str, scene_pos: QPointF) -> None:
        """Arm a RECT-semantics resize for a TEXT box (Span / ParagraphBox): the
        box's frame follows the cursor and on release the text re-wraps to the
        new WIDTH at constant font size. Reuses the shared drag-state owner so
        Esc-abort / preview teardown ride the existing paths."""
        box = self._selection
        self._drag_kind = "frame_resize"
        self._drag_box = box
        self._drag_handle = handle_id
        self._drag_start_scene = QPointF(scene_pos)
        self._drag_armed = True
        self._drag_start_rect = self._span_scene_rect(box)

    def _frame_resize_rect_for(self, scene_pos: QPointF) -> QRectF:
        """Live scene rect for a text-frame resize: the dragged edge / corner
        follows the cursor, the opposite side stays fixed, floored so it can't
        invert. Only the horizontal extent is committed (the wrap sets height),
        but the full rect drives the live outline."""
        r = QRectF(self._drag_start_rect)
        h = self._drag_handle or ""
        left, top = r.left(), r.top()
        right, bottom = r.right(), r.bottom()
        if "w" in h:
            left = min(scene_pos.x(), right - _IMAGE_MIN_RESIZE_PX)
        if "e" in h:
            right = max(scene_pos.x(), left + _IMAGE_MIN_RESIZE_PX)
        if "n" in h:
            top = min(scene_pos.y(), bottom - _IMAGE_MIN_RESIZE_PX)
        if "s" in h:
            bottom = max(scene_pos.y(), top + _IMAGE_MIN_RESIZE_PX)
        return QRectF(QPointF(left, top), QPointF(right, bottom)).normalized()

    def _show_image_resize_preview(self, rect: QRectF) -> None:
        """Track the overlay + the stretched source pixels with the live
        rect: ONE QGraphicsPixmapItem built from the box's own bytes (once
        per drag), transformed to fill the rect (images & signatures §3)."""
        if self._overlay is not None:
            self._overlay.set_box_rect(rect)
        if self._drag_image_preview is None and self._drag_box is not None:
            qimg = QImage.fromData(self._drag_box.image)
            if not qimg.isNull():
                item = QGraphicsPixmapItem(QPixmap.fromImage(qimg))
                item.setTransformationMode(Qt.SmoothTransformation)
                item.setOpacity(_GHOST_OPACITY)
                item.setZValue(Z_DRAG_PREVIEW)
                self.scene().addItem(item)
                self._drag_image_preview = item
        if self._drag_image_preview is not None:
            pm = self._drag_image_preview.pixmap()
            if pm.width() and pm.height():
                t = QTransform()
                t.translate(rect.left(), rect.top())
                t.scale(rect.width() / pm.width(),
                        rect.height() / pm.height())
                self._drag_image_preview.setTransform(t)
        self._ensure_drag_preview(rect)

    def _update_drag(self, scene_pos: QPointF) -> None:
        if self._drag_kind == "move":
            delta = scene_pos - self._drag_start_scene
            if not self._drag_armed:
                if abs(delta.x()) < _MIN_DRAG_PX and abs(delta.y()) < _MIN_DRAG_PX:
                    return
                self._drag_armed = True
            delta = self._snap_move_delta(delta)
            self._show_move_preview(delta)
            z = self._zoom or 1.0
            self._show_drag_tooltip(
                scene_pos,
                f"Δ {delta.x() / z:.1f}, {delta.y() / z:.1f} pt")
        elif self._drag_kind == "img_resize":
            rect = self._image_resize_rect_for(scene_pos)
            self._show_image_resize_preview(rect)
            z = self._zoom or 1.0
            self._show_drag_tooltip(
                scene_pos,
                f"{rect.width() / z:.0f} × {rect.height() / z:.0f} pt")
        elif self._drag_kind == "resize":
            scale = self._resize_scale_for(scene_pos)
            self._show_resize_preview(scale)
            # Live folded font size (§3.3c): start EFFECTIVE size x scale --
            # the same math resize_box folds into style.size -- so the user
            # sees "13.0 pt -> 15.5 pt" while dragging.
            s0 = self._drag_start_size
            self._show_drag_tooltip(
                scene_pos, f"{s0:.1f} pt → {s0 * scale:.1f} pt")
        elif self._drag_kind == "frame_resize":
            # The frame follows the cursor; the text re-wraps to the new WIDTH
            # at constant size on release (no font scaling). Show the live
            # outline + the width readout.
            rect = self._frame_resize_rect_for(scene_pos)
            if self._overlay is not None:
                self._overlay.set_box_rect(rect)
            z = self._zoom or 1.0
            self._show_drag_tooltip(scene_pos, f"width {rect.width() / z:.0f} pt")

    def _finish_drag(self, scene_pos: QPointF) -> None:
        kind = self._drag_kind
        box = self._drag_box
        self._clear_drag_preview()
        promote_edit = False
        if kind == "move":
            if self._drag_armed and box is not None:
                # Apply the SAME axis discipline the preview showed (§3.2), so
                # an axis-locked ghost commits an axis-locked move.
                delta = self._snap_move_delta(
                    scene_pos - self._drag_start_scene)
                page = getattr(box, "page_index", self._page_index)
                dx, dy = self._scene_delta_to_pdf(delta, page)
                if abs(dx) > 1e-6 or abs(dy) > 1e-6:
                    # An image-like box routes to its own command kind
                    # (images & signatures §3: the window maps "img_move"
                    # onto move_image, and expands "xim_move" into the
                    # delete+reinsert macro, §5); same gesture, same delta
                    # space -- the view stays mutation-free.
                    if self._is_existing_image(box):
                        move_kind = "xim_move"
                    elif self._is_image_box(box):
                        move_kind = "img_move"
                    else:
                        move_kind = "move"
                    self.boxCommandRequested.emit(
                        move_kind, box, {"dx": dx, "dy": dy})
                    self.geometryChanged.emit(box)
            elif (not self._drag_armed and self._press_on_selected
                  and self._press_hotspot is not None):
                # A pure click (no drag) on the box that was ALREADY selected:
                # this is the "second click" -> start editing with the caret at
                # the click point (REFLOW_SPEC click-to-edit gesture).
                promote_edit = True
        elif kind == "img_resize":
            if box is not None:
                rect = self._image_resize_rect_for(scene_pos)
                page = getattr(box, "page_index", self._page_index)
                p0 = self._pdf_point(rect.topLeft(), page)
                p1 = self._pdf_point(rect.bottomRight(), page)
                pdf_rect = (min(p0[0], p1[0]), min(p0[1], p1[1]),
                            max(p0[0], p1[0]), max(p0[1], p1[1]))
                if rect != self._drag_start_rect:
                    self.boxCommandRequested.emit(
                        "img_resize", box, {"rect": pdf_rect})
                    self.geometryChanged.emit(box)
        elif kind == "resize":
            if box is not None:
                scale = self._resize_scale_for(scene_pos)
                if abs(scale - 1.0) > 1e-3:
                    self.boxCommandRequested.emit(
                        "resize", box,
                        {"scale": scale, "anchor": self._drag_anchor_pdf})
                    self.geometryChanged.emit(box)
        elif kind == "frame_resize":
            if box is not None:
                rect = self._frame_resize_rect_for(scene_pos)
                page = getattr(box, "page_index", self._page_index)
                p0 = self._pdf_point(rect.topLeft(), page)
                p1 = self._pdf_point(rect.bottomRight(), page)
                new_x = min(p0[0], p1[0])
                new_w = abs(p1[0] - p0[0])
                if rect != self._drag_start_rect and new_w > 1e-3:
                    self.boxCommandRequested.emit(
                        "frame_resize", box, {"x": new_x, "w": new_w})
                    self.geometryChanged.emit(box)
        hotspot = self._press_hotspot
        self._reset_drag_state()
        if promote_edit and hotspot is not None:
            self.begin_edit(hotspot, scene_x=scene_pos.x(), scene_point=scene_pos)

    def _cancel_drag(self) -> None:
        self._clear_drag_preview()
        self._reset_drag_state()

    def _abort_drag(self) -> None:
        """Esc during a LIVE move/resize preview (§2.5 / §3.2): drop every
        preview affordance and restore the overlay to the box's true staged
        rect. NO command is emitted -- the gesture simply never happened; the
        eventual mouse release routes to super() because the drag state is
        already cleared."""
        self._cancel_drag()
        self._refresh_overlay()

    def _reset_drag_state(self) -> None:
        self._drag_kind = None
        self._drag_box = None
        self._drag_armed = False
        self._drag_handle = None
        self._drag_anchor_pdf = None
        self._drag_baseline_scene = None
        self._press_on_selected = False
        self._press_hotspot = None

    def _resize_scale_for(self, scene_pos: QPointF) -> float:
        """Absolute scale = newDiagonal / startDiagonal from the fixed anchor
        (the opposite corner). Clamped so the box cannot invert or collapse."""
        page = getattr(self._drag_box, "page_index", self._page_index)
        anchor_scene = self._scene_point(*self._drag_anchor_pdf, page_index=page)
        new_diag = self._diag(anchor_scene, scene_pos)
        scale = new_diag / self._drag_start_diag
        return max(0.1, min(scale, 20.0))

    def _show_move_preview(self, delta_scene: QPointF) -> None:
        """Translate the selection overlay + the GHOST PIXMAP + the outline
        preview by the (already axis-snapped) cursor delta WITHOUT restaging
        every pixel (EDITOR_SPEC §3.6, text-editing UX §3.2). The ghost is the
        box's own region copied out of the page pixmap at 0.65 opacity, so the
        user drags what looks like the actual ink, not just a dashed frame."""
        rect = QRectF(self._drag_start_rect).translated(delta_scene)
        if self._overlay is not None:
            self._overlay.set_box_rect(rect)
        self._ensure_drag_ghost()
        if self._drag_ghost is not None:
            self._drag_ghost.setPos(rect.topLeft())
        self._ensure_drag_preview(rect)
        self._update_axis_guide(self._axis_lock)

    def _snap_move_delta(self, delta: QPointF) -> QPointF:
        """Axis discipline for a body move (§3.2). Shift constrains the delta
        to its DOMINANT axis. Without Shift, a component that stays within
        0.75 display points of the box's original x (or y) snaps to zero so a
        near-straight drag lands perfectly straight. Records the locked travel
        axis on ``self._axis_lock`` ("x"=horizontal travel, "y"=vertical,
        None=free) for the guide-line feedback."""
        dx, dy = delta.x(), delta.y()
        lock = None
        try:
            shift = bool(QApplication.keyboardModifiers() & Qt.ShiftModifier)
        except Exception:  # noqa: BLE001 - modifier read must not kill a drag
            shift = False
        if shift:
            if abs(dx) >= abs(dy):
                dy, lock = 0.0, "x"
            else:
                dx, lock = 0.0, "y"
        else:
            snap = _AXIS_SNAP_PT * (self._zoom or 1.0)
            if abs(dx) <= snap:
                dx, lock = 0.0, "y"
            elif abs(dy) <= snap:
                dy, lock = 0.0, "x"
        self._axis_lock = lock
        return QPointF(dx, dy)

    def _ensure_drag_ghost(self) -> None:
        """Lazily build the ghost: the box's region copied out of its page's
        pixmap (scene rect -> device-pixel rect via x_left/y_top and the DPR),
        as a Z_DRAG_PREVIEW QGraphicsPixmapItem at 0.65 opacity (§3.2). Absent
        page pixels (un-materialized layer / no-layers test path) the dashed
        outline preview still carries the gesture alone."""
        if self._drag_ghost is not None or self._drag_box is None:
            return
        page = getattr(self._drag_box, "page_index", self._page_index)
        if not (0 <= page < len(self._layers)):
            return
        layer = self._layers[page]
        if layer.pixmap_item is None:
            return
        src = layer.pixmap_item.pixmap()
        if src.isNull():
            return
        dpr = src.devicePixelRatio() or 1.0
        rect = self._drag_start_rect
        px = QRect(
            int(round((rect.left() - layer.x_left) * dpr)),
            int(round((rect.top() - layer.y_top) * dpr)),
            max(1, int(round(rect.width() * dpr))),
            max(1, int(round(rect.height() * dpr))),
        ).intersected(src.rect())
        if px.isEmpty():
            return
        ghost_pix = src.copy(px)
        ghost_pix.setDevicePixelRatio(dpr)
        item = QGraphicsPixmapItem(ghost_pix)
        item.setTransformationMode(Qt.SmoothTransformation)
        item.setOpacity(_GHOST_OPACITY)
        item.setZValue(Z_DRAG_PREVIEW)
        self.scene().addItem(item)
        item.setPos(rect.topLeft())
        self._drag_ghost = item

    def _update_axis_guide(self, lock: str | None) -> None:
        """Show/hide the 1px accent guide for an axis-locked move (§3.2): a
        line through the box's START position along the travel axis, spanning
        the page sheet, so the locked rail is visible while dragging."""
        if lock is None:
            if self._axis_guide is not None:
                if self._axis_guide.scene() is not None:
                    self.scene().removeItem(self._axis_guide)
                self._axis_guide = None
            return
        page = getattr(self._drag_box, "page_index", self._page_index)
        if 0 <= page < len(self._layers):
            span_rect = self._layer_scene_rect(self._layers[page])
        else:
            span_rect = self._drag_start_rect.adjusted(-200, -200, 200, 200)
        c = self._drag_start_rect.center()
        if lock == "x":     # travelling horizontally: rail at the original y
            line = QLineF(span_rect.left(), c.y(), span_rect.right(), c.y())
        else:               # travelling vertically: rail at the original x
            line = QLineF(c.x(), span_rect.top(), c.x(), span_rect.bottom())
        if self._axis_guide is None:
            item = QGraphicsLineItem()
            pen = QPen(theme.color_accent())
            pen.setWidthF(1.0)
            pen.setCosmetic(True)
            item.setPen(pen)
            item.setZValue(Z_DRAG_PREVIEW)
            self.scene().addItem(item)
            self._axis_guide = item
        self._axis_guide.setLine(line)

    def _show_drag_tooltip(self, scene_pos: QPointF, text: str) -> None:
        """The live readout riding the cursor during a drag: the move delta
        ("Δ 12.0, -3.5 pt") or the folded font size ("13.0 pt → 15.5 pt")
        (§3.2/§3.3c). Best-effort chrome: any platform tooltip hiccup is
        swallowed so it can never break the gesture."""
        try:
            vp = self.viewport()
            QToolTip.showText(
                vp.mapToGlobal(self.mapFromScene(scene_pos)), text, vp)
        except Exception:  # noqa: BLE001
            pass

    def _show_resize_preview(self, scale: float) -> None:
        # Scale the start rect about the BASELINE ORIGIN (the same point the model
        # bake + effective_bbox grow the run from), so the live preview lands
        # exactly where the committed text will (no preview-vs-commit drift). For
        # a Span the baseline is captured at _begin_resize; for a NewBox without a
        # baseline anchor we fall back to the opposite-corner proportional rect.
        anchor = getattr(self, "_drag_baseline_scene", None)
        if anchor is not None:
            rect = self._scaled_rect_about(self._drag_start_rect, anchor, scale)
        else:
            rect = self._scaled_rect(self._drag_start_rect, self._drag_handle, scale)
        if self._overlay is not None:
            self._overlay.set_box_rect(rect)
        self._ensure_drag_preview(rect)

    @staticmethod
    def _scaled_rect_about(rect: QRectF, anchor: QPointF, scale: float) -> QRectF:
        """Proportionally scale ``rect`` about a fixed ``anchor`` scene point."""
        x0 = anchor.x() + (rect.left() - anchor.x()) * scale
        y0 = anchor.y() + (rect.top() - anchor.y()) * scale
        x1 = anchor.x() + (rect.right() - anchor.x()) * scale
        y1 = anchor.y() + (rect.bottom() - anchor.y()) * scale
        return QRectF(QPointF(min(x0, x1), min(y0, y1)),
                      QPointF(max(x0, x1), max(y0, y1)))

    def _ensure_drag_preview(self, rect: QRectF) -> None:
        if self._drag_preview is None:
            item = QGraphicsRectItem()
            pen = QPen(theme.color_accent())
            pen.setWidthF(1.0)
            pen.setCosmetic(True)
            pen.setStyle(Qt.DashLine)
            item.setPen(pen)
            item.setBrush(QBrush(theme.color_accent_hover()))
            item.setZValue(Z_DRAG_PREVIEW)
            self.scene().addItem(item)
            self._drag_preview = item
        self._drag_preview.setRect(rect)

    def _clear_drag_preview(self) -> None:
        """Drop every live-drag affordance: dashed outline, ghost pixmap,
        image-resize preview, axis guide, and the cursor tooltip (§3.2)."""
        for attr in ("_drag_preview", "_drag_ghost", "_drag_image_preview",
                     "_axis_guide"):
            item = getattr(self, attr, None)
            if item is not None:
                if item.scene() is not None:
                    self.scene().removeItem(item)
                setattr(self, attr, None)
        self._axis_lock = None
        try:
            QToolTip.hideText()
        except Exception:  # noqa: BLE001
            pass

    # --- handle / corner geometry ----------------------------------------
    @staticmethod
    def _handle_scene_point(rect: QRectF, handle_id: str) -> QPointF:
        cx = (rect.left() + rect.right()) / 2
        cy = (rect.top() + rect.bottom()) / 2
        pts = {
            "nw": (rect.left(), rect.top()),
            "n": (cx, rect.top()),
            "ne": (rect.right(), rect.top()),
            "e": (rect.right(), cy),
            "se": (rect.right(), rect.bottom()),
            "s": (cx, rect.bottom()),
            "sw": (rect.left(), rect.bottom()),
            "w": (rect.left(), cy),
        }
        x, y = pts[handle_id]
        return QPointF(x, y)

    @staticmethod
    def _opposite_corner_scene(rect: QRectF, handle_id: str) -> QPointF:
        """The fixed anchor for a proportional resize: the corner DIAGONALLY
        opposite the dragged handle (edge handles anchor on the opposite edge's
        far corner, keeping the resize proportional-from-opposite-corner, §6)."""
        opp = {
            "nw": (rect.right(), rect.bottom()),
            "ne": (rect.left(), rect.bottom()),
            "se": (rect.left(), rect.top()),
            "sw": (rect.right(), rect.top()),
            "n": (rect.right(), rect.bottom()),
            "s": (rect.right(), rect.top()),
            "e": (rect.left(), rect.bottom()),
            "w": (rect.right(), rect.bottom()),
        }
        x, y = opp[handle_id]
        return QPointF(x, y)

    @staticmethod
    def _dragged_corner_scene(rect: QRectF, handle_id: str) -> QPointF:
        return PageView._handle_scene_point(rect, handle_id)

    @staticmethod
    def _scaled_rect(rect: QRectF, handle_id: str, scale: float) -> QRectF:
        """A proportional preview rect anchored at the opposite corner."""
        anchor = PageView._opposite_corner_scene(rect, handle_id)
        x0 = anchor.x() + (rect.left() - anchor.x()) * scale
        y0 = anchor.y() + (rect.top() - anchor.y()) * scale
        x1 = anchor.x() + (rect.right() - anchor.x()) * scale
        y1 = anchor.y() + (rect.bottom() - anchor.y()) * scale
        return QRectF(QPointF(min(x0, x1), min(y0, y1)),
                      QPointF(max(x0, x1), max(y0, y1)))

    @staticmethod
    def _diag(a: QPointF, b: QPointF) -> float:
        return math.hypot(b.x() - a.x(), b.y() - a.y())

    # =====================================================================
    # Add a box at a scene point (EDITOR_SPEC §3.5)
    # =====================================================================
    def _do_add_at(self, scene_pos: QPointF) -> None:
        """Convert ``scene_pos`` to a PDF-point baseline origin and request an
        'add' command using the inspector's current style. Pinned: the baseline
        sits AT the click y and the box grows upward (§3.5), matching
        ``_insert_run`` and the model's ``_newbox_bbox``. After the window
        creates the NewBox and calls ``select_box`` + ``begin_edit``, the mode
        returns to SELECT on the text commit."""
        if self.document is None:
            return
        # In the continuous view the click may land on any page; resolve which
        # page it hit and make THAT the current page so the window's 'add'
        # command (which reads view_page_index) targets the right page, and
        # compute the baseline origin in that page's coordinate system.
        page = self._page_at_scene_y(scene_pos.y())
        if page != self._page_index:
            self._page_index = page
            self._sync_current_page_mirror()
            self.pageChanged.emit(self._page_index)
        origin = self._pdf_point(scene_pos, page)
        # Auto-match nearby style (REFLOW_SPEC §R5.4): a box dropped inside / next
        # to existing text inherits that text's font/size/color/bold/italic, so
        # the new run blends in. Blank space (no neighbor in range) -> None ->
        # the inspector's current defaults, exactly today's behavior.
        style = self._add_style_near(page, origin) or self._add_style_defaults()
        params = {
            "origin": origin,
            "text": "",
            "family": style["font_family"],
            "size": style["size"],
            # Never hand a new box an invisible color (e.g. white inherited from
            # a header bar, dropped on the white page body) -- see §R5.4.
            "color": self._add_color_visible_on(
                style["color"], page, origin, style["size"]),
            "bold": style["bold"],
            "italic": style["italic"],
        }
        # Leave ADD_TEXT mode now; arm the add so select_box (called by the
        # window with the fresh NewBox) drops straight into TEXT_EDIT.
        self.exit_add_text_mode()
        self._awaiting_add = True
        self.boxCommandRequested.emit("add", None, params)
        # If no listener created/handed back a box (e.g. a window not wired to
        # boxCommandRequested), disarm so a later real selection is unaffected.
        if self._awaiting_add and not isinstance(self._pending_add, NewBox):
            self._awaiting_add = False

    def begin_add_box_edit(self, box: NewBox) -> None:
        """Called by the window right after it creates a NewBox via the 'add'
        command: select it and drop straight into TEXT_EDIT so the user types
        the new text. The box is remembered as ``_pending_add`` so an empty
        commit/cancel rolls the add back (empty-add cleanup, §3.5)."""
        self._awaiting_add = False           # avoid select_box re-entering here
        fresh = self._box_by_identity(box.identity) or box
        self._pending_add = fresh
        self.select_box(fresh)
        hotspot = self._hotspot_for(fresh)
        if hotspot is not None:
            self.begin_edit(hotspot)

    def _add_style_defaults(self) -> dict:
        """The style a new box is created with. The window overrides this with
        the inspector's current values via ``set_add_style_provider``; absent a
        provider we fall back to a sensible Helvetica 12pt black."""
        if self._add_style_provider is not None:
            try:
                v = self._add_style_provider() or {}
            except Exception:
                v = {}
        else:
            v = {}
        return {
            "font_family": v.get("font_family", "Helvetica"),
            "size": float(v.get("size", 12.0)),
            "color": tuple(v.get("color", (0.0, 0.0, 0.0))),
            "bold": bool(v.get("bold", False)),
            "italic": bool(v.get("italic", False)),
        }

    def _add_style_near(self, page: int, origin: tuple) -> dict | None:
        """Ask the model for the nearest existing text style at the add point
        (REFLOW_SPEC §R5.4). Returns a fully-shaped add-style dict (same keys as
        ``_add_style_defaults``) so a box added in a paragraph column inherits
        that paragraph's font/size/color, or ``None`` when nothing is close
        enough (the caller then uses the inspector defaults). Defensive: any
        model without ``style_near`` or any error yields ``None``."""
        doc = self.document
        if doc is None:
            return None
        finder = getattr(doc, "style_near", None)
        if finder is None:
            return None
        try:
            v = finder(page, origin)
        except Exception:
            return None
        if not v:
            return None
        return {
            "font_family": v.get("font_family", "Helvetica"),
            "size": float(v.get("size", 12.0)),
            "color": tuple(v.get("color", (0.0, 0.0, 0.0))),
            "bold": bool(v.get("bold", False)),
            "italic": bool(v.get("italic", False)),
        }

    _add_style_provider = None

    def set_add_style_provider(self, provider) -> None:
        """Register a 0-arg callable returning the inspector's current style
        dict, so a new box is created with the user's chosen font/size/color/
        bold/italic (EDITOR_SPEC §3.5 step 3). The window wires this to
        ``inspector.current_values``."""
        self._add_style_provider = provider

    # =====================================================================
    # Inline editing (existing path, generalized to Span OR NewBox)
    # =====================================================================
    def begin_edit(self, hotspot: SpanHotspot, scene_x: float | None = None,
                   scene_point: QPointF | None = None) -> None:
        """Mount the in-scene inline editor over a box's run, in its resolved
        font/size/color, on its true baseline (BUILD_SPEC §5.4). Works for an
        existing Span (resolved via the 3-tier ``resolve``) AND a NewBox
        (resolved via ``resolve_family``). Emits ``editStarted`` with the
        ``ResolvedFont`` so the chrome shows the fidelity chip. While TEXT_EDIT
        is active the selection overlay is hidden (the editor is the focus)."""
        if not self.document:
            return
        # Images carry no text: every begin_edit entry point (Enter key,
        # context menu, click-promote) funnels through here, so this ONE
        # guard keeps the inline editor off image boxes (images §3).
        if self._is_image_box(getattr(hotspot, "box", None)):
            return
        self._cancel_editor_silent()
        self._cancel_drag()
        box = hotspot.box
        page = getattr(box, "page_index", self._page_index)
        self._editor_box = box
        self._editor_hotspot = hotspot
        self._editor_multiline = bool(getattr(box, "is_paragraph", False))
        hotspot._editing = True
        hotspot.update()

        # Keep the box OUTLINE visible while editing (drop only the interactive
        # handles), so the box stays the SAME size/shape when you open it to
        # edit instead of collapsing to the editor's tighter text frame. The
        # overlay sits at Z_SELECTION (40), below the editor (50), so its outline
        # frames the box while the editor draws the live text on top.
        if self._overlay is not None:
            self._overlay.set_handles_visible(False)
            self._overlay.setVisible(True)

        staged = self.document.staged_text(page, box)
        # Drive the editor from the box's STAGED effective style, not its
        # original fields, so the live editor is indistinguishable from the baked
        # text after a restyle/resize (editor == baked == saved, BUILD_SPEC §3).
        # effective_style folds any size/scale/family/bold/italic/color override;
        # the font resolves the same way the bake does (resolve_family for a
        # staged family/weight override, else the 3-tier resolve).
        eff = self.document.effective_style(page, box)
        resolved = self._resolve_for_edit(box, staged, eff, page)

        z = self._zoom
        eff_size = float(eff.get("size", box.size) or box.size)
        # A paragraph OCR box scales to FIT its cover, with the SAME factor the bake
        # uses, so the inline editor and the saved page lay out the same lines at the
        # same size (without this the editor renders the raw over-estimated size and
        # overflows while the bake fits -- they disagreed).
        _fit = (self.document.paragraph_fit_factor(box)
                if getattr(box, "is_paragraph", False)
                and len(getattr(box, "cover", ()) or ()) == 7 else 1.0)
        eff_size *= _fit
        pixel_size = max(_MIN_PIXEL_SIZE, eff_size * z)
        font = resolved.qfont(pixel_size)

        def _width_ratio(text: str, qfont: "QFont", target_pt: float | None) -> float:
            """How much to scale ``qfont``'s advance so ``text`` occupies the
            PAGE's width. target_pt = the baked line advance (pts); when None we
            measure it from the resolved fitz face. Returns 1.0 when unknown."""
            try:
                qpx = QFontMetricsF(qfont).horizontalAdvance(text)
                if qpx <= 0:
                    return 1.0
                if target_pt is None:
                    target_pt = self.document.font_engine.fitz_font_for(
                        resolved).text_length(text, fontsize=eff_size)
                r = (target_pt * z) / qpx
                return r if 0.85 < r < 1.15 else 1.0
            except (AttributeError, ValueError, ZeroDivisionError):
                return 1.0

        cover = self._make_cover(box)
        cover.setZValue(Z_COVER)
        self.scene().addItem(cover)
        self._editor_cover = cover

        eff_color = eff.get("color", box.color) or box.color

        # PARAGRAPH: rebuild the editor with the PAGE's own line breaks so its
        # layout is a pixel overlay of the rendered text -- no Qt-vs-fitz wrap
        # drift. The break points are the spaces the bake joined lines with, so
        # swapping those spaces for newlines keeps every character index the
        # same (staged rich runs still map). ``layout`` carries each baked line's
        # text + advance for the per-line width scaling below.
        layout = (self.document.editor_line_layout(page, box)
                  if self._editor_multiline else None)
        display = staged
        self._editor_hard_wrapped = False
        if layout:
            candidate = "\n".join(t for t, _w in layout)
            if candidate.replace("\n", " ") == staged:
                display = candidate
                self._editor_hard_wrapped = True

        editor = InlineRunEditor(display, self)
        if not self._editor_hard_wrapped:
            # Single line (or soft-wrap fallback): width-match the whole text so
            # a centered name/title sits where the page draws it.
            r = _width_ratio((display.replace("\n", " ") or " "), font, None)
            if abs(r - 1.0) > 0.001:
                font.setLetterSpacing(
                    QFont.SpacingType.PercentageSpacing, r * 100.0)
        editor.setFont(font)
        editor.document().setDefaultFont(font)
        editor.setDefaultTextColor(QColor.fromRgbF(*eff_color))
        # Re-mount RICH runs as char formats so the editor shows (and round-
        # trips) per-word styling instead of flattening to uniform: previously
        # STAGED runs (a bolded selection), else a grouped/auto ParagraphBox's
        # INTRINSIC member styling (e.g. a bold "2025" pulled into a group --
        # otherwise the editor and the committed bake would drop its bold).
        staged_runs = self.document.staged_runs(page, box)
        if not staged_runs and getattr(box, "is_paragraph", False):
            staged_runs = self.document.paragraph_runs(box)
        if staged_runs:
            self._apply_runs_to_editor(editor, staged_runs)

        if self._editor_multiline:
            # An OCR paragraph carries box_w: its column width in DISPLAY points
            # (rotation-correct). Wrap the editor to THAT, not the text-space bbox
            # width -- on a /Rotate page the text-space width is the block's display
            # HEIGHT, so wrapping to it stacked every paragraph ~one word per line.
            bw = getattr(box, "box_w", None)
            if bw:
                wrap_w = bw * z
            else:
                x0, y0, x1, y1 = self.document.effective_bbox(page, box)
                wrap_w = (x1 - x0) * z
            editor.setTextWidth(max(1.0, wrap_w))
            align = eff.get("alignment", "left")
            leading_pt = (getattr(box, "leading", 0.0) * _fit) or (eff_size * 1.2)
            if self._editor_hard_wrapped:
                # Use the bake's hard breaks exactly (no Qt re-wrap), and lay out
                # each line to the page: per-block alignment, a leading-matched
                # top margin (lines sit at the baked baselines WITHOUT lifting
                # the first), and width scaling (each line occupies the baked
                # advance, so words land where the page drew them).
                topt = editor.document().defaultTextOption()
                topt.setWrapMode(QTextOption.NoWrap)
                editor.document().setDefaultTextOption(topt)
                self._layout_overlay_blocks(
                    editor, layout, font, align, leading_pt * z, _width_ratio)
            else:
                self._apply_editor_alignment(editor, align, leading_pt * z)

        ascent = QFontMetricsF(font).ascent()
        self._place_text_item(editor, box, ascent)
        # The soft-wrap fallback uses a FIXED line height taller than the font's
        # natural box, whose surplus sits ABOVE the first baseline -- lift the
        # editor so the first baseline still lands on the box origin. The
        # hard-wrap path uses per-block top margins (first block margin 0), so it
        # needs no lift.
        if self._editor_multiline and not self._editor_hard_wrapped:
            leading_pt = (getattr(box, "leading", 0.0) * _fit) or (eff_size * 1.2)
            surplus = leading_pt * z - QFontMetricsF(font).height()
            if surplus > 0:
                editor.setY(editor.y() - surplus)
        self.scene().addItem(editor)
        self._editor = editor
        # Cache what the live re-resolve needs, then watch text changes so the
        # editor font tracks the font the COMMIT will bake (WYSIWYG for an
        # embedded subset that stops covering the typed glyphs).
        self._editor_eff = eff
        self._editor_page = page
        self._editor_pixel_size = pixel_size
        editor.document().contentsChanged.connect(self._reresolve_editor_font)

        editor.setFocus(Qt.MouseFocusReason)
        cursor = editor.textCursor()
        # Caret placement (text-editing UX §2.1): EVERY editor -- single-line
        # Span/NewBox included -- places the caret through the document-layout
        # hit-test, which is exact where the old cumulative-advance walk was
        # nearest-boundary-ish. A legacy caller passing only ``scene_x``
        # synthesizes a point on the first line (the single-line semantic the
        # x-only signature always had).
        if scene_point is None and scene_x is not None:
            scene_point = QPointF(
                scene_x, editor.sceneBoundingRect().top() + 1.0)
        if scene_point is not None and staged:
            cursor.setPosition(editor.caret_index_at_scene_point(scene_point))
        else:
            cursor.select(QTextCursor.Document)
        editor.setTextCursor(cursor)

        self.modeChanged.emit(MODE_TEXT_EDIT)
        self.editStarted.emit(box, resolved)

    @staticmethod
    def _apply_editor_alignment(editor: "InlineRunEditor", alignment: str,
                                line_height_px: float = 0.0) -> None:
        """Mirror the paragraph alignment in the inline editor and, when
        ``line_height_px`` > 0, pin each wrapped line to that FIXED height so the
        editor's line spacing matches the baked paragraph leading exactly (the
        bake re-applies the true alignment/wrap via wrap_paragraph)."""
        amap = {
            "left": Qt.AlignLeft, "center": Qt.AlignHCenter,
            "right": Qt.AlignRight, "justify": Qt.AlignJustify,
        }
        block_fmt = editor.textCursor().blockFormat()
        block_fmt.setAlignment(amap.get(alignment, Qt.AlignLeft))
        if line_height_px > 0:
            # NEVER pin the line height below the font's natural height, or the
            # glyphs overlap: an OCR block's measured leading can be tighter than
            # the rendered face needs (scan lines packed tighter than the em
            # estimate), and a FixedHeight smaller than the glyph box stacks the
            # lines on top of each other. Clamp so lines can never collide.
            natural = QFontMetricsF(editor.document().defaultFont()).height()
            block_fmt.setLineHeight(
                max(line_height_px, natural),
                QTextBlockFormat.LineHeightTypes.FixedHeight.value)
        cur = editor.textCursor()
        cur.select(QTextCursor.Document)
        cur.mergeBlockFormat(block_fmt)
        cur.clearSelection()
        editor.setTextCursor(cur)

    @staticmethod
    def _layout_overlay_blocks(editor, layout, base_font, alignment,
                               leading_px, width_ratio) -> None:
        """Lay a HARD-WRAPPED paragraph editor out as a pixel overlay of the page.
        Each block is one baked line; for each: set alignment, a leading-matched
        top margin (so lines sit at the baked baselines without lifting the
        first line), and a per-line letter-spacing that scales the line to the
        baked advance (so the words occupy exactly the page's width and land
        where the page drew them). ``layout`` = [(text, baked_width_pt), ...]."""
        amap = {
            "left": Qt.AlignLeft, "center": Qt.AlignHCenter,
            "right": Qt.AlignRight, "justify": Qt.AlignJustify,
        }
        doc = editor.document()
        # Qt rounds a line's ascent/descent UP, so a block's real laid-out height
        # exceeds QFontMetricsF.height() by ~1px; measure the ACTUAL height so the
        # per-line top margin makes the baseline-to-baseline gap exactly the baked
        # leading (using .height() left ~1px of extra spacing on every line, which
        # read as the editor being looser than the page it overlays).
        natural = doc.documentLayout().blockBoundingRect(doc.begin()).height()
        if natural <= 0:
            natural = QFontMetricsF(base_font).height()
        top_margin = max(0.0, leading_px - natural)
        block = doc.begin()
        i = 0
        while block.isValid():
            text = block.text()
            baked_w = layout[i][1] if i < len(layout) else 0.0
            cur = QTextCursor(block)
            cur.movePosition(QTextCursor.StartOfBlock)
            cur.movePosition(QTextCursor.EndOfBlock, QTextCursor.KeepAnchor)
            bf = QTextBlockFormat()
            bf.setAlignment(amap.get(alignment, Qt.AlignLeft))
            if i > 0:
                bf.setTopMargin(top_margin)
            cur.mergeBlockFormat(bf)
            if baked_w > 0 and text:
                r = width_ratio(text, base_font, baked_w)
                if abs(r - 1.0) > 0.001:
                    cf = QTextCharFormat()
                    cf.setFontLetterSpacingType(
                        QFont.SpacingType.PercentageSpacing)
                    cf.setFontLetterSpacing(r * 100.0)
                    cur.mergeCharFormat(cf)
            block = block.next()
            i += 1

    def commit_edit(self, from_focus_out: bool = False) -> None:
        """Commit the active edit (Enter / focus-out). Stages the edit only when
        the text changed, emits ``editCommitted`` so the window pushes an undo
        command, tears the editor down, and emits ``editFinished``.

        Empty-add cleanup (EDITOR_SPEC §3.5): if the box under the editor is a
        brand-new box (``_pending_add``) and the committed text is empty /
        whitespace, request ``cancel_add`` so the window pops the add command --
        a stray click that adds nothing leaves no box."""
        if self._editor is None or self._committing or self._closing_editor:
            return
        self._committing = True
        editor, box = self._editor, self._editor_box
        # Read ALL editor state BEFORE teardown (§2.7a): text, base font flags,
        # and the extracted runs. The old code re-read ``editor.font()`` after
        # ``_teardown_editor()`` -- alive only via this local ref after the
        # scene removed the item, which is exactly the kind of
        # use-after-teardown that turns into a crash the day teardown also
        # drops the C++ object. Capture once, use everywhere below.
        new_text = editor.toPlainText()
        # A hard-wrapped paragraph editor holds the bake's line breaks as block
        # separators; collapse them back to the single spaces they replaced so
        # the STAGED text is the continuous paragraph the bake re-wraps. Index
        # parity (space<->newline at the same positions) makes this exact.
        if getattr(self, "_editor_hard_wrapped", False):
            new_text = new_text.replace("\n", " ")
        base_b = editor.font().bold()
        base_i = editor.font().italic()
        # Extract per-selection rich styling (NewBoxes are uniform-only).
        # ``rich_runs`` stays None when every fragment matches the editor's
        # base style (a plain, uniform edit).
        rich_runs = None
        if getattr(box, "box_id", None) is None:
            extracted = self._extract_editor_runs(editor)
            if any((b, i) != (base_b, base_i) for _, b, i in extracted):
                rich_runs = extracted
        pending_add = self._pending_add
        self._teardown_editor()

        page = box.page_index
        is_pending = (pending_add is not None
                      and self._is_same_box(box, pending_add))
        if is_pending and not new_text.strip():
            # A new box left empty -> roll the add back entirely.
            self._pending_add = None
            self._remove_overlay()
            self._selection = None
            self.boxCommandRequested.emit("cancel_add", box, {})
            self.selectionChanged.emit(None)
        else:
            self._pending_add = None
            staged_runs = self.document.staged_runs(page, box) \
                if getattr(box, "box_id", None) is None else None
            # The BASELINE a paragraph starts from is its intrinsic per-member
            # styling (a bold "2025" pulled into a group), not "nothing". A
            # commit that merely reproduces it with unchanged text is a NO-OP --
            # without this, opening a grouped box and committing without typing
            # would stage a bogus rich edit and force a re-bake/reflow.
            baseline_runs = staged_runs
            if baseline_runs is None and getattr(box, "is_paragraph", False):
                baseline_runs = self.document.paragraph_runs(box)
            if rich_runs is not None:
                # Mixed weights/slants: stage the styled runs (unless they just
                # reproduce the intrinsic baseline and the text is unchanged).
                # Compare the per-NON-SPACE-character styling so a bold space
                # vs a regular space (visually identical; the editor attributes
                # them differently than the member runs) is not a "change" --
                # otherwise opening a grouped box and committing without typing
                # would stage a bogus rich edit and re-bake/reflow it.
                if (self._runs_visual_key(rich_runs)
                        != self._runs_visual_key(baseline_runs)
                        or new_text != self.document.staged_text(page, box)):
                    self.editCommittedRich.emit(
                        page, box, {"text": new_text, "runs": rich_runs})
            elif staged_runs is not None:
                # Uniform again, but rich runs were staged before. If the
                # uniform style IS the box's natural style, clear the runs
                # (plain text); if it is a NON-natural style (e.g. the whole
                # text was bolded from inside the editor), keep it as ONE
                # uniform run so the styling survives the commit.
                from ..fonts import is_bold, is_italic
                natural = (is_bold(box.font, box.flags),
                           is_italic(box.font, box.flags))
                if (base_b, base_i) == natural:
                    payload = {"text": new_text, "runs": None}
                else:
                    payload = {"text": new_text,
                               "runs": ((new_text, base_b, base_i),)}
                self.editCommittedRich.emit(page, box, payload)
            elif new_text != self.document.staged_text(page, box):
                self.editCommitted.emit(page, box, new_text)
            # Self-heal an INVISIBLE added box: if this NewBox's color has no
            # contrast against the page where it sits (white inherited from a
            # header bar, dropped on the white body), recolor it to a visible
            # ink. So opening an already-invisible box to edit it makes it
            # readable again -- the recovery path for boxes created before the
            # add-time guard existed. NewBoxes ONLY: a span's white is the
            # document's own header ink on its bar and must never be touched.
            if isinstance(box, NewBox) and new_text.strip():
                vis = self._add_color_visible_on(
                    box.color, page, box.origin, box.size)
                if tuple(vis) != tuple(box.color):
                    self.boxCommandRequested.emit(
                        "style", box, {"overrides": {"color": vis}})
        self._committing = False
        # Selection persists across TEXT_EDIT -> SELECT (committing keeps the
        # box selected); restore the overlay if the box still exists.
        if not is_pending or new_text.strip():
            self._restore_overlay_after_edit()
        self.editFinished.emit()
        self.modeChanged.emit(self.current_mode())

    def cancel_edit(self) -> None:
        """Abandon the active edit (Esc): stage nothing. For a brand-new box
        that was never typed into, roll the add back (empty-add cleanup)."""
        if self._editor is None or self._closing_editor:
            return
        box = self._editor_box
        pending_add = self._pending_add
        self._teardown_editor()
        is_pending = (pending_add is not None
                      and self._is_same_box(box, pending_add))
        if is_pending:
            self._pending_add = None
            self._remove_overlay()
            self._selection = None
            self.boxCommandRequested.emit("cancel_add", box, {})
            self.selectionChanged.emit(None)
        else:
            self._restore_overlay_after_edit()
        self.editCancelled.emit()
        self.editFinished.emit()
        self.modeChanged.emit(self.current_mode())

    def _restore_overlay_after_edit(self) -> None:
        """Bring the selection chrome back after a text commit/cancel (the box
        stays selected, EDITOR_SPEC §3.7)."""
        if self._selection is None:
            return
        fresh = self._box_by_identity(self._selection.identity)
        if fresh is None:
            self._selection = None
            self.selectionChanged.emit(None)
            return
        self._selection = fresh
        if self._overlay is None:
            self._install_overlay(fresh)
        else:
            self._overlay.setVisible(True)
            self._overlay.set_handles_visible(True)
            self._overlay.set_box_rect(self._span_scene_rect(fresh))

    def _teardown_editor(self) -> None:
        """Remove the editor + its white cover without emitting commit/cancel."""
        self._closing_editor = True
        try:
            if self._editor is not None:
                self.scene().removeItem(self._editor)
            if self._editor_cover is not None:
                self.scene().removeItem(self._editor_cover)
            if self._editor_hotspot is not None:
                self._editor_hotspot._editing = False
                if self._editor_hotspot.scene() is not None:
                    self._editor_hotspot.update()
        finally:
            self._editor = None
            self._editor_cover = None
            self._editor_box = None
            self._editor_hotspot = None
            self._editor_multiline = False
            self._closing_editor = False

    def _flush_editor(self) -> None:
        """Commit an OPEN inline editor through the normal commit path before a
        reload-triggered teardown. No-op when no editor is open or a commit/
        teardown is already in flight.

        Covers BOTH editors: an open FORM editor commits first (forms §3) --
        hooking it here means every existing flush call site (zoom, reload,
        page-jump, mode arming, structural ops) keeps a half-typed fill."""
        if self._form_editor is not None and not self._form_committing \
                and not self._closing_form_editor:
            self.commit_form_editor()
        if self._editor is None or self._committing or self._closing_editor:
            return
        self.commit_edit()

    def _cancel_editor_silent(self) -> None:
        """Tear an open editor down with no signals (used by clear_document /
        begin_edit re-entry)."""
        if self._editor is not None or self._editor_cover is not None:
            self._teardown_editor()

    def is_edited(self, box) -> bool:
        """True when ``box`` carries staged text changes (the edited-hover
        affordance). Consults the edit maps for the box's OWN page (§2.7d) --
        the old ``page_index != self._page_index -> False`` short-circuit made
        the amber edited tint vanish on every materialized page except the one
        under the viewport center, which is wrong in the continuous view where
        several pages' hotspots are live at once.

        A NewBox counts as edited ONLY once it actually draws ink. An OCR
        overlay is born invisible (render_mode 3) over the kept scan; the user
        editing it flips it to visible (render_mode 0) via stage_edit. So a
        PRISTINE OCR word (render_mode 3) is NOT edited -- it must stay unmarked
        like any untouched run, or every recovered word wears the persistent
        ochre edited tint at rest, smearing tan "yellow bars" across a scanned
        page. User-added boxes are render_mode 0, so they read as edits as
        before."""
        if not self.document or box is None:
            return False
        if isinstance(box, NewBox):
            return getattr(box, "render_mode", 0) == 0
        page = getattr(box, "page_index", self._page_index)
        return self.document.staged_text(page, box) != box.text

    def is_unsaved_edit(self, box) -> bool:
        """True when ``box`` carries an edit made since the last save. Drives
        the persistent in-place edit signature, which flags PENDING changes and
        clears once they are saved (a saved edit then reads as clean, like the
        original text -- see ``Document.is_edit_unsaved``)."""
        if not self.document or box is None:
            return False
        page = getattr(box, "page_index", self._page_index)
        return self.document.is_edit_unsaved(page, box)

    # =====================================================================
    # Keyboard (view-level, depends on selection/editor state, §3.3/§5.5)
    # =====================================================================
    def keyPressEvent(self, event: QKeyEvent):
        """View-level keys. The inline editor (when focused) consumes its own
        keys first; the per-mode key handler from ``self._mode_handlers`` owns
        the rest (perf foundation M4c). A handler returning True consumed the
        key; otherwise the event routes on to super().

        THE Enter/Esc CONTRACT (text-editing UX §2.5) -- every state, one
        table, enforced by the handlers named on each row:

          state                  Return / Enter           Escape
          ---------------------  -----------------------  ---------------------
          TEXT_EDIT (editor      commit_edit; an empty    cancel_edit; an empty
          focused; keys land in  brand-new box rolls its  brand-new box rolls
          InlineRunEditor)       add back (cancel_add)    its add back
          SELECT + selection     begin_edit on the        clear_selection
          (_key_select)          selection
          SELECT, no selection   unhandled (falls to      unhandled
          (_key_select)          super())
          SELECT + focused form  activates the field      drops the field's
          field (_key_select,    (toggle / pick / menu /  focus ring
          forms M3)              editor re-mount)
          ADD_TEXT armed         SELECT rules first       SELECT rules first
          (_key_add_text)        (selection coexists)     (clears a selection),
                                                          else exit_add_text_mode
          SELECT_TEXT armed      unhandled (falls to      clears the word
          (_key_select_text)     super())                 selection, else disarms
                                                          back to SELECT
          CROP armed             unhandled (falls to      cancels an in-flight
          (_key_crop)            super())                 rubber drag, else
                                                          disarms back to SELECT
          live drag preview      n/a (mouse owns the      aborts the drag, no
          (move/resize)          gesture)                 command (_abort_drag,
                                                          checked first in
                                                          _key_select)
        """
        handler = self._mode_handlers.get(self._mode, {}).get("key")
        if handler is not None and handler(event):
            return
        super().keyPressEvent(event)

    # --- per-mode key handlers (perf foundation M4c) ----------------------
    # Arrow-key nudge directions in DISPLAY space (screen-up == -y) (§3.1).
    _NUDGE_DIRS = {
        Qt.Key_Left: (-1.0, 0.0), Qt.Key_Right: (1.0, 0.0),
        Qt.Key_Up: (0.0, -1.0), Qt.Key_Down: (0.0, 1.0),
    }

    def _key_select(self, event: QKeyEvent) -> bool:
        """View-level keys for SELECT mode: clipboard + selection keys. Fire
        only with no editor mounted (the focused editor consumes its own)."""
        if self._editor is not None:
            return False
        mods = event.modifiers()
        cmd = bool(mods & (Qt.ControlModifier | Qt.MetaModifier))
        key = event.key()
        # Esc during a LIVE drag preview aborts the gesture -- no command
        # (the Enter/Esc contract's drag row, §2.5/§3.2). Checked before the
        # selection rows so the abort always wins over clear_selection.
        if key == Qt.Key_Escape and self._drag_kind is not None:
            event.accept()
            self._abort_drag()
            return True
        # ... and the same abort for a live ANNOT move (annotations §4.3).
        if key == Qt.Key_Escape and self._annot_drag is not None:
            event.accept()
            self._abort_annot_drag()
            return True
        # Selected ANNOTATION keys (§4.3): Delete/Backspace = ONE 'delete'
        # intent; Esc clears. The selections are mutually exclusive, so
        # these rows never shadow the box-selection rows below.
        if self._annot_selection is not None and self._selection is None:
            rec = self._annot_selection
            if key in (Qt.Key_Delete, Qt.Key_Backspace):
                event.accept()
                self.annotCommandRequested.emit(
                    "delete", rec.identity, {"page_index": rec.identity[0]})
                return True
            if key == Qt.Key_Escape:
                event.accept()
                self.clear_annot_selection()
                return True
        # Form tab-order navigation (forms §3, M3): Tab/Backtab walk the
        # fillable fields in document order, wrapping cross-page. The key
        # arrives here for BOTH states -- the focused-field one directly,
        # the mid-edit one because the editor declines Tab
        # (tabChangesFocus) and the view's focusNextPrevChild returns
        # False on a form doc; form_tab_step commits the open editor
        # before moving. A non-form doc never reaches this row (the focus
        # chain consumed the key), so plain docs are untouched.
        if key in (Qt.Key_Tab, Qt.Key_Backtab) and not cmd:
            if self.form_tab_step(key != Qt.Key_Backtab):
                event.accept()
                return True
        # Focused-field keys (forms §3, M3): with a field focused (and no
        # box/annot selection -- the press/focus routing keeps them
        # mutually exclusive), Space or Return activates it and Esc drops
        # the focus ring.
        if (self._focused_field is not None and self._selection is None
                and self._annot_selection is None and not cmd):
            if key in (Qt.Key_Space, Qt.Key_Return, Qt.Key_Enter):
                if self._activate_focused_field():
                    event.accept()
                    return True
            if key == Qt.Key_Escape:
                event.accept()
                self._set_focused_field(None)
                return True
        # Arrow-key nudge (§3.1): 1.0 display-pt steps (Shift = 10.0) through
        # the SAME scene->PDF inverse mapping the move drag uses, so "up" is
        # SCREEN-up on a rotated page too. params carries nudge=True so the
        # window's BoxCommand merges consecutive steps into one undo step.
        if (self._selection is not None and not cmd
                and key in self._NUDGE_DIRS):
            event.accept()
            ux, uy = self._NUDGE_DIRS[key]
            step = _NUDGE_SHIFT_PT if (mods & Qt.ShiftModifier) else _NUDGE_PT
            box = self._selection
            page = getattr(box, "page_index", self._page_index)
            z = self._zoom or 1.0
            dx, dy = self._scene_delta_to_pdf(
                QPointF(ux * step * z, uy * step * z), page)
            if self._is_image_box(box):
                # Image nudge: the image command kind, each step a discrete
                # undo entry (no nudge fuse -- coalesce_last_undo only fuses
                # text "move" commands; images & signatures §3). An existing
                # image's nudge is the move MACRO (§5, M3): the window
                # selects the reinserted ImageBox, so follow-up nudges ride
                # the cheap img_move path.
                kind = ("xim_move" if self._is_existing_image(box)
                        else "img_move")
                self.boxCommandRequested.emit(
                    kind, box, {"dx": dx, "dy": dy})
            else:
                self.boxCommandRequested.emit(
                    "move", box, {"dx": dx, "dy": dy, "nudge": True})
            self.geometryChanged.emit(box)
            return True
        # Copy/paste WITH formatting (REFLOW_SPEC §R5.3): Cmd/Ctrl+C copies the
        # selected box; Cmd/Ctrl+V pastes it as a new box on the current page.
        if cmd and key == Qt.Key_C and self._selection is not None:
            if self.copy_selection():
                event.accept()
                return True
        if cmd and key == Qt.Key_V:
            if self.paste():
                event.accept()
                return True
        if self._selection is not None:
            if key in (Qt.Key_Delete, Qt.Key_Backspace):
                event.accept()
                self.delete_selected()
                return True
            if key in (Qt.Key_Return, Qt.Key_Enter):
                event.accept()
                hotspot = self._hotspot_for(self._selection)
                if hotspot is not None:
                    self.begin_edit(hotspot)
                return True
            if key == Qt.Key_Escape:
                event.accept()
                self.clear_selection()
                return True
        return False

    def _key_add_text(self, event: QKeyEvent) -> bool:
        """ADD_TEXT mode keys: the SELECT-mode set still applies (a selection
        can coexist with the armed tool; with one, Escape clears IT first --
        the long-standing behavior), then a free Escape disarms the tool."""
        if self._key_select(event):
            return True
        if event.key() == Qt.Key_Escape:
            event.accept()
            self.exit_add_text_mode()
            return True
        return False

    def _hotspot_for(self, box) -> SpanHotspot | None:
        """The hotspot covering ``box`` across ALL materialized layers (the box
        may live on a non-current page in the continuous view)."""
        ident = getattr(box, "identity", None)
        for layer in self._layers:
            for hs in layer.hotspots:
                if getattr(hs.box, "identity", None) == ident:
                    return hs
        for hs in self._hotspots:
            if getattr(hs.box, "identity", None) == ident:
                return hs
        return None

    # =====================================================================
    # Geometry helpers (single source for editor + overlay + insert)
    # =====================================================================
    def _resolve_box(self, box, text: str) -> ResolvedFont:
        """Resolve the font the editor draws with, matching what save_as uses:
        an existing Span goes through the 3-tier ``resolve`` (reproduces the
        original face); a NewBox goes through ``resolve_family`` (the user-picked
        family, always embeddable) -- the SAME calls the model's
        ``_apply_page_edits`` makes, so editor == baked == saved."""
        if isinstance(box, NewBox):
            return self.document.font_engine.resolve_family(
                box.font_family, box.bold, box.italic, text or " ")
        page = getattr(box, "page_index", self._page_index)
        return self.document.font_engine.resolve(
            page, box.font, box.flags, text)

    def _resolve_for_edit(self, box, text: str, eff: dict,
                          page: int | None = None) -> ResolvedFont:
        """Resolve the editor's font from the box's STAGED effective style ``eff``,
        mirroring the model's ``_resolve_for_edit`` so the live editor matches the
        baked/saved run. A NewBox (or a Span with a staged family override) routes
        through ``resolve_family`` on the effective family/bold/italic (always
        embeddable); an unstyled Span keeps the 3-tier ``resolve`` that reproduces
        its original face. Falls back to the plain box resolve if anything is
        missing so the editor never crashes on a stale style dict."""
        engine = self.document.font_engine
        if isinstance(box, NewBox):
            return engine.resolve_family(
                box.font_family, box.bold, box.italic, text or " ")
        if page is None:
            page = getattr(box, "page_index", self._page_index)
        from ..document import StyleOverride  # local: avoid a top-level cycle
        edit = self.document._edits.get((page, box.key))
        style = edit.style if edit is not None else StyleOverride()
        # A staged family or weight/slant override means the run is being
        # re-typed in a user-picked face -> resolve_family (embeddable). Otherwise
        # reproduce the original embedded/system face via the 3-tier resolve.
        if style.font_family is not None or style.bold is not None \
                or style.italic is not None:
            family = eff.get("font_family") or box.font
            return engine.resolve_family(
                family, bool(eff.get("bold")), bool(eff.get("italic")),
                text or " ")
        return engine.resolve(page, box.font, box.flags, text)

    # Kept for the existing window's _on_edit_started introspection.
    def _resolve(self, span, text: str) -> ResolvedFont:
        return self._resolve_box(span, text)

    # --- per-page coordinate context -------------------------------------
    def _resolve_page_index(self, page_index: int | None) -> int:
        """Default an omitted page index to the current page; clamp to range."""
        idx = self._page_index if page_index is None else page_index
        if self._layers:
            return max(0, min(idx, len(self._layers) - 1))
        return max(0, idx)

    def _sheet_origin_for(self, page_index: int | None) -> QPointF:
        """The scene top-left of ``page_index``'s sheet. Defaults to the current
        page's origin (== ``_sheet_origin``), so the 2-arg ``_scene_point`` the
        existing tests call keeps resolving against page 0. The x is the page's
        CENTERED ``x_left`` (not the bare margin) so hit-testing, overlays, and the
        scene<->PDF mapping all agree on a centered page (REFLOW_SPEC §R3.1)."""
        if page_index is None or not self._layers:
            return self._sheet_origin
        idx = self._resolve_page_index(page_index)
        layer = self._layers[idx]
        return QPointF(layer.x_left, layer.y_top)

    def _rotation_matrix_for(self, page_index: int | None) -> "fitz.Matrix":
        if page_index is None or not self._layers:
            return self._rotation_matrix
        idx = self._resolve_page_index(page_index)
        return self._layers[idx].rotation_matrix

    def _rotation_for(self, page_index: int | None) -> int:
        if page_index is None or not self._layers:
            return self._page_rotation
        idx = self._resolve_page_index(page_index)
        return self._layers[idx].rotation

    def _page_image_for(self, page_index: int | None) -> "QImage | None":
        if page_index is None or not self._layers:
            return self._page_image
        idx = self._resolve_page_index(page_index)
        return self._layers[idx].image

    def _display_point(self, x: float, y: float,
                       page_index: int | None = None) -> tuple[float, float]:
        """Map a rawdict (text-space) point into the rendered pixmap's DISPLAY
        space via the page rotation matrix. Identity for an unrotated page."""
        p = fitz.Point(x, y) * self._rotation_matrix_for(page_index)
        return p.x, p.y

    def _scene_point(self, x: float, y: float,
                     page_index: int | None = None) -> QPointF:
        """A rawdict point on ``page_index`` -> scene coordinates (display-space *
        zoom + that page's sheet offset). ``page_index=None`` resolves against the
        CURRENT page, so the existing 2-arg call sites + tests stay valid."""
        dx, dy = self._display_point(x, y, page_index)
        z = self._zoom
        origin = self._sheet_origin_for(page_index)
        return QPointF(origin.x() + dx * z, origin.y() + dy * z)

    def _pdf_point(self, scene: QPointF, page_index: int | None = None) -> tuple:
        """The EXACT inverse of ``_scene_point`` (EDITOR_SPEC §6): a scene point
        back to a rawdict (PDF text-space) point on ``page_index``. De-zoom and
        de-offset to display space, then through the inverse rotation matrix back
        to derotated text space. ``page_index=None`` uses the current page."""
        z = self._zoom
        origin = self._sheet_origin_for(page_index)
        dx = (scene.x() - origin.x()) / z
        dy = (scene.y() - origin.y()) / z
        inv = self._invert_matrix(self._rotation_matrix_for(page_index))
        p = fitz.Point(dx, dy) * inv
        return (p.x, p.y)

    def _scene_delta_to_pdf(self, delta_scene: QPointF,
                            page_index: int | None = None) -> tuple:
        """A scene-space delta -> PDF-point delta on ``page_index`` (EDITOR_SPEC
        §3.6): divide by zoom and inverse-rotate the direction. Computed as the
        difference of two de-projected points so rotation is handled uniformly."""
        p0 = self._pdf_point(QPointF(0.0, 0.0), page_index)
        p1 = self._pdf_point(delta_scene, page_index)
        return (p1[0] - p0[0], p1[1] - p0[1])

    @staticmethod
    def _invert_matrix(m: "fitz.Matrix") -> "fitz.Matrix":
        inv = fitz.Matrix(m)
        try:
            inv.invert()
        except Exception:
            return fitz.Matrix(1, 0, 0, 1, 0, 0)
        return inv

    def _span_scene_rect(self, box) -> QRectF:
        """Box STAGED bbox (rawdict points) -> scene rect on the box's OWN page,
        rotation-aware. Works for a Span, NewBox, and ParagraphBox (all expose
        ``.bbox`` + ``.page_index``). For a 90/270 page this swaps width/height,
        so the rect is built from the transformed corners' bounding box.

        The rect is read from ``document.effective_bbox`` (the post-move/
        post-resize/post-size/reflow bbox the bake actually draws), not the
        immutable ``box.bbox``, so the selection outline + handles track the live
        ink (overlay == baked == saved, EDITOR_SPEC §3.7 / REFLOW_SPEC §R2.5)."""
        z = self._zoom
        page = getattr(box, "page_index", None)
        if page is None:
            page = self._page_index
        if self.document is not None:
            x0, y0, x1, y1 = self.document.effective_bbox(page, box)
        else:
            x0, y0, x1, y1 = box.bbox
        corners = [self._display_point(cx, cy, page)
                   for cx, cy in ((x0, y0), (x1, y0), (x1, y1), (x0, y1))]
        xs = [c[0] for c in corners]
        ys = [c[1] for c in corners]
        origin = self._sheet_origin_for(page)
        left = origin.x() + min(xs) * z
        top = origin.y() + min(ys) * z
        return QRectF(left, top, (max(xs) - min(xs)) * z, (max(ys) - min(ys)) * z)

    def _image_scene_rect(self, box) -> QRectF:
        """A placed image's CURRENT staged rect -> scene rect on its page
        (images & signatures §3): the live rect from the model (the map's
        copy -- moves/resizes replace it), corner-mapped through
        ``_scene_point`` so a rotated page's swap/flip rides the same math
        every other overlay uses."""
        page = getattr(box, "page_index", self._page_index)
        if self.document is not None:
            rect = self.document.effective_bbox(page, box)
        else:
            rect = box.rect
        x0, y0, x1, y1 = rect
        pts = [self._scene_point(x, y, page_index=page)
               for x, y in ((x0, y0), (x1, y0), (x1, y1), (x0, y1))]
        xs = [p.x() for p in pts]
        ys = [p.y() for p in pts]
        return QRectF(QPointF(min(xs), min(ys)), QPointF(max(xs), max(ys)))

    def _place_text_item(self, item, box, ascent: float) -> None:
        """Position a QGraphicsTextItem so its glyph baseline lands at the box's
        STAGED origin on its OWN page, rotated to match the page's display
        orientation (BUILD_SPEC §5.3). The baseline is read from
        ``document.effective_origin`` (origin + staged move) so the editor mounts
        on the live baseline after a move, matching the baked text."""
        page = getattr(box, "page_index", None)
        if page is None:
            page = self._page_index
        if self.document is not None:
            origin = self.document.effective_origin(page, box)
        else:
            origin = box.origin
        # A PARAGRAPH editor lays its lines out from the column LEFT edge,
        # centering/justifying each within the column width. So mount it at the
        # box's left edge, NOT the (possibly indented) first line's origin -- a
        # centered/right paragraph's first line is indented, and mounting there
        # would shift the whole block sideways so later lines overflow the box.
        if getattr(box, "is_paragraph", False) and self.document is not None:
            eb = self.document.effective_bbox(page, box)
            origin = (eb[0], origin[1])
        baseline = self._scene_point(*origin, page_index=page)
        rot = float(self._rotation_for(page) % 360)
        # OCR overlay boxes (cover-marked) are placed DEROTATED so they bake + display
        # upright over the scan; the inline editor must therefore show them upright
        # too. Rotating by the page's raw /Rotate spun the editing text 90deg while
        # the scan and baked text read upright (the "text rotates when you click in"
        # bug on scanned /Rotate pages). Gated to OCR boxes; ordinary boxes on a
        # genuinely rotated page still follow the page.
        if len(getattr(box, "cover", ()) or ()) == 7:
            rot = 0.0
        item.setRotation(rot)
        if rot == 0.0:
            item.setPos(baseline.x(), baseline.y() - ascent)
            return
        rad = math.radians(rot)
        dx = ascent * math.sin(rad)
        dy = -ascent * math.cos(rad)
        item.setPos(baseline.x() - dx, baseline.y() - dy)

    def _make_cover(self, box) -> QGraphicsRectItem:
        """A rect covering a box's bbox (hides the rasterized original under the
        live editor). Filled with the box's REAL background color (sampled from
        the rendered page) so editing on a colored cell keeps the fill. For a
        NewBox there is no original ink under it, so the sampled color is just
        the sheet/background -- still correct."""
        rect = self._span_scene_rect(box).adjusted(-1.0, -1.0, 1.0, 1.0)
        cover = QGraphicsRectItem(rect)
        cover.setBrush(QBrush(self._background_color_for(box)))
        cover.setPen(QPen(Qt.NoPen))
        return cover

    def _background_color_for(self, box) -> QColor:
        """The dominant (background) color under a box, sampled from the box's OWN
        page image (REFLOW_SPEC §R3.5). Falls back to sheet white before render."""
        page = getattr(box, "page_index", None)
        ink = getattr(box, "color", None) or (0.0, 0.0, 0.0)
        return self._dominant_bg_color(page, self._span_scene_rect(box), ink)

    def _dominant_bg_color(self, page_index: int | None, scene_rect: QRectF,
                           ink_rgb: tuple | None = None) -> QColor:
        """Dominant (modal) color inside ``scene_rect``, sampled from
        ``page_index``'s rendered image, EXCLUDING pixels that match ``ink_rgb``
        (the glyph color) so a box already full of ink still reports its
        BACKGROUND and not the text. Falls back to sheet white before render.
        Shared by the editor cover (``_background_color_for``) and the add-box
        contrast guard (``_add_color_visible_on``)."""
        img = self._page_image_for(page_index)
        if img is None or img.isNull():
            return theme.color_sheet_white()
        # The page image's (0,0) is the page's CENTERED scene top-left, so sample
        # offsets are relative to x_left/y_top -- not the bare margin (which would
        # mis-sample a centered narrow page in a mixed-size doc, REFLOW_SPEC §R3.1).
        origin = self._sheet_origin_for(page_index)
        x_off = origin.x()
        y_off = origin.y()
        dpr = self.devicePixelRatioF()
        x0 = int((scene_rect.left() - x_off) * dpr)
        y0 = int((scene_rect.top() - y_off) * dpr)
        x1 = int((scene_rect.right() - x_off) * dpr)
        y1 = int((scene_rect.bottom() - y_off) * dpr)
        w, h = img.width(), img.height()
        x0 = max(0, min(x0, w - 1)); x1 = max(x0 + 1, min(x1, w))
        y0 = max(0, min(y0, h - 1)); y1 = max(y0 + 1, min(y1, h))
        # Exclude pixels that ARE the text ink, so the cover picks the
        # BACKGROUND color and not the glyph color. A large BOLD dark name fills
        # more of its box with ink than background, which otherwise makes black
        # the modal color and the whole box goes black on click (the cert bug).
        ir = ig = ib = -999
        if ink_rgb is not None:
            ir = int(max(0.0, min(1.0, ink_rgb[0])) * 255)
            ig = int(max(0.0, min(1.0, ink_rgb[1])) * 255)
            ib = int(max(0.0, min(1.0, ink_rgb[2])) * 255)
        counts: dict[int, int] = {}
        step_x = max(1, (x1 - x0) // 40)
        step_y = max(1, (y1 - y0) // 12)
        for yy in range(y0, y1, step_y):
            for xx in range(x0, x1, step_x):
                argb = img.pixel(xx, yy)
                r = (argb >> 16) & 0xFF
                g = (argb >> 8) & 0xFF
                b = argb & 0xFF
                if ink_rgb is not None and \
                        abs(r - ir) + abs(g - ig) + abs(b - ib) < 70:
                    continue   # this is the glyph ink, not the background
                counts[argb] = counts.get(argb, 0) + 1
        if not counts:
            return theme.color_sheet_white()
        return QColor.fromRgb(max(counts, key=counts.get))

    @staticmethod
    def _relative_luminance(rgb: tuple) -> float:
        """Perceived luminance (0=black, 1=white) of an ``(r, g, b)`` 0..1
        triple -- the Rec. 601 weighting used everywhere else in this file."""
        return 0.299 * rgb[0] + 0.587 * rgb[1] + 0.114 * rgb[2]

    def _add_color_visible_on(self, color: tuple, page_index: int | None,
                              origin: tuple, size: float = 11.0) -> tuple:
        """Keep a NEW box's INHERITED / DEFAULT text color only when it will be
        visible where the box is dropped; otherwise fall back to a contrasting
        ink (black on a light background, white on a dark one).

        Why this exists: a box's color can be inherited from nearby text
        (``_add_style_near``) or from the inspector's last selection
        (``_add_style_defaults``). A document's header text is often WHITE
        because it sits on a dark/colored bar; inherit that white onto a box
        dropped on the white page body and the text is invisible -- typed,
        present, but unreadable. We do NOT touch a deliberate white-on-a-dark-
        bar add: the contrast there is fine, so the color is kept. Only an
        effectively-invisible same-luminance pairing is corrected."""
        try:
            scene = self._scene_point(origin[0], origin[1], page_index)
        except Exception:
            return color
        # Sample the background in the TIGHT band the glyphs will occupy: origin
        # is the baseline, the cap height reaches ~0.72*size above it. Staying
        # inside the glyph band avoids the white page margin just above a colored
        # header bar (which would otherwise read as a light background and
        # wrongly "correct" a legitimate white-on-bar add).
        z = self._zoom
        cap = max(4.0, 0.72 * float(size or 11.0))
        rect = QRectF(scene.x() + 1.0 * z, scene.y() - cap * z, 70.0 * z, cap * z)
        bg = self._dominant_bg_color(page_index, rect)
        text_lum = self._relative_luminance(color)
        bg_lum = self._relative_luminance((bg.redF(), bg.greenF(), bg.blueF()))
        if abs(text_lum - bg_lum) >= 0.30:
            return color                      # visible as-is, leave it alone
        return (0.0, 0.0, 0.0) if bg_lum > 0.5 else (1.0, 1.0, 1.0)
