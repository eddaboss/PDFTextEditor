"""Images, stamps & signatures suite (ws4_images_signatures). M1: ImageBox
end-to-end -- model + bake + place/move/resize/delete + undo. M2:
signatures + stamps -- draw, library, one-click place. M3: existing
images -- detect, page-scoped delete, move-as-macro.

Asserts (spec ws4 §8 M1 test plan):

  T1: MODEL staging: ``add_image`` on a temp copy of form_like.pdf stages
      (edit_count 1, dirty), validates bytes by magic number (a non-image
      payload raises BEFORE any state changes), clamps an off-page rect into
      the page, and raises ``render_with_edits`` ink inside the placed rect
      (the region was pixel-verified blank in the pristine fixture).
  T2: SAVE round trip: ``save_as`` -> fitz reopen shows exactly one image
      whose ``get_image_info`` bbox equals the staged rect ±0.5pt, with
      ``extract_image(...)["smask"] != 0`` for the RGBA source (transparency
      preserved); WYSIWYG: ``render_with_edits`` ink vs the saved file's
      ink, rel diff < 0.02 (the one-shared-pipeline metric).
  T3: UNDO/REDO: undo of the add returns ``render_with_edits`` to
      byte-identical PRISTINE pixels (the fast path -- zero staged state)
      and edit_count to 0; redo restores the staged box and the ink.
  T4: MOVE / RESIZE / DELETE round-trip: each mutation is ONE model command;
      the saved file's image bbox tracks the staged rect ±0.5pt after a
      move (rect + delta) and a resize (the absolute rect); delete saves a
      file with NO image; the undo chain walks all the way back.
  T5: COEXISTENCE: a text ``stage_edit`` on the same page beside the placed
      image still saves with the word change applied, NO new base-14 face
      introduced (new_base14 == [] pattern: saved basefonts minus the
      fixture's own), and the image bbox intact -- the in-place pipeline is
      undisturbed (the soul invariant).
  T6: STRUCTURAL bake: ``rotate_page`` after ``add_image`` bakes the image
      into ``working`` (staged map empty, edit_count 0) and the image
      survives a subsequent save.
  T7: ROTATED page placement (rotated_doc.pdf, /Rotate 90): the bake passes
      ``rotate=page.rotation`` so the ASYMMETRIC test image (red left half,
      blue right half) reads upright on screen -- the red centroid lands
      LEFT of the blue centroid in display space, both inside the
      rotation-matrix-mapped rect (probe-pinned behavior).
  T8: CACHE keying (§2.5): ``render_signature`` changes after ``add_image``
      (and reverts on undo), and the view's pixmap cache MISSES for the new
      signature while the materialized page had an entry for the old one --
      the one-registry contract, no side-channel signature APIs.
  T9: UI placement: ``enter_place_image_mode`` + a synthesized CLICK places
      ONE BoxCommand (default width = min(natural_px*72/96, 0.45*page_w),
      height by aspect, click = top-left), auto-exits the mode, and selects
      the new box (``_ImageSelectionOverlay`` mounted); a press-DRAG places
      the aspect-locked rubber rect instead; Delete key = ONE command; the
      window undo stack walks the whole chain back to pristine; Esc disarms
      the armed mode.
  T10: UI move/resize gestures: a body drag on the selected image = ONE
      'img_move' command with the scene delta / zoom; a SE-handle drag =
      ONE 'img_resize' command tracking the dragged rect; the selection
      survives both repaints by identity.
  T11: CHROME: "Image from File…" (Cmd+Shift+I) sits BEFORE the Tools
      menu's ``tools_objects`` anchor; the toolbar Insert-Image button
      hides with no document and shows after open; the action enables only
      with a document; ``_do_insert_image(path=...)`` (the dialog seam)
      arms the canvas place mode and a bad payload flashes instead of
      arming; dropping a PNG on the canvas arms place mode (PDF drops keep
      opening).

M2 sections (spec §8 M2 test plan):

  T12: ``strokes_to_png`` (the PURE export core, §6): synthetic strokes ->
      valid PNG with an alpha channel and >0 opaque pixels, CROPPED to the
      ink bbox + margin (size << the 620x220 canvas at 3x), ink in the
      requested pen color; a single-point stroke renders a dot; empty
      strokes -> ``b""``.
  T13: ``SignatureLibrary(tempdir)`` CRUD: lazy dir creation (nothing on
      disk until the first save -- the real ``~/.pdftexteditor`` is NEVER
      touched), save/list/load/delete round trip, ``-2`` dedupe on a name
      collision, path-hostile names sanitized to ``[A-Za-z0-9 _-]``,
      newest-first listing.
  T14: SignatureDialog driven NON-modally offscreen: real mouse events on
      the canvas append plain-data strokes, Clear resets, pen controls sync
      to the canvas, injected strokes + ``accept()`` -> ``result_png()``
      transparent PNG, name field / save-to-library checkbox readable.
  T15: window placement through the injected seams (stub
      ``_signature_dialog_factory`` + tempdir ``_signature_library``):
      ``_do_draw_signature`` saves to the library and arms placement
      (kind="signature"); a drag places it OVER the "Notes:" text line with
      the strokes confined to the image's far corners, and the word's ink
      count survives under the transparent region on screen AND in the
      saved file (smask intact) while the placed rect's total ink grows --
      transparent signature over text, the §8 visual-probe claim.
  T16: ``stamp_png``: valid transparent PNG (corner alpha 0), red border +
      text ink, width tracks the label, case-insensitive input; placing
      APPROVED via ``_do_place_stamp`` + a click saves a file whose stamp
      region carries red ink with the smask intact; one undo returns to
      pristine.
  T17: M2 chrome census: Signature + Stamp submenus sit before the
      ``tools_objects`` anchor; the Signature menu re-lists the (injected)
      library on ``aboutToShow`` with 32px thumbnail icons; Draw Signature…
      carries Cmd+Shift+G and gates on a document like the stamp entries;
      the toolbar Signature menu-button (popup = the same menu, default
      action = Draw) hides in the empty state; the dialog factory defaults
      to the real SignatureDialog class.

M3 sections (spec §8 M3 test plan):

  T18: ``existing_images`` detection: one occurrence per page of
      image_doc.pdf at the AUTHORED rect (imported from the builder, so
      the assert can never drift), the same shared xref on both pages,
      memoized per page (same list object) and refreshed by
      ``_invalidate_caches``; ``delete_existing_image`` stages ONE
      'xim_delete' command (edit_count/dirty/render_signature miss on its
      page only; re-deleting the same occurrence is a no-op), undo returns
      to byte-identical pristine pixels, redo restores.
  T19: page-scoped DELETE round trip (the §0 probe as a regression): the
      staged deletion removes the page-0 occurrence's ink on screen AND in
      the saved file (rel parity < 0.02) while the page-0 TEXT and the
      SAME xref's page-1 occurrence survive untouched; a structural rotate
      after the staged delete bakes it (maps clear, working loses the
      occurrence, page 1 keeps its own).
  T20: MOVE-AS-MACRO through the window: ``extract_image_bytes``
      recombines the SMask (alpha-carrying PNG at the source pixel size);
      ``xim_move`` expands into ONE Qt macro (xim_delete + img_add
      kind="moved") -- one undo-stack step -- that stages both halves,
      selects the reinserted ImageBox, saves ink at the new rect with the
      smask intact and none at the old, keeps screen == saved file, and
      ONE ``undo_stack.undo()`` restores BOTH (pristine census + the
      occurrence back).
  T21: UI: the xim hotspot materializes in the page's box machinery
      (ImageHotspot at the occurrence rect), click selects it with the
      image overlay mounted WITHOUT handles (no staged resize for file
      truth) and the "Page image · W x H pt" status chip, a body drag
      fires the macro (the moved ImageBox takes the selection), the
      Delete key stages ONE xim_delete whose repaint drops the hotspot,
      and undo brings the hotspot back.

Fixtures: temp copies of the existing synthetic form_like.pdf +
rotated_doc.pdf; the test image is generated IN-TEST (fitz.Pixmap RGBA,
red/blue halves + a transparent stripe -- asymmetric for the orientation
asserts). M3 uses the committed image_doc.pdf (build_image_fixtures.py:
one RGBA gradient logo xref shared by two pages, fake neutral names only).
Signature/stamp pixels are procedural (strokes_to_png / stamp_png);
library names use fake neutral people names only. Never writes into
tests/fixtures/; temp output via tempfile.mkdtemp.

Run:
    QT_QPA_PLATFORM=offscreen \
      /Users/edward/Documents/GitHub/PDFTextEditor/.venv/bin/python \
      tests/test_images.py
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import traceback

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import fitz  # noqa: E402
from PySide6.QtCore import QEvent, QMimeData, QPointF, QUrl, Qt  # noqa: E402
from PySide6.QtGui import (  # noqa: E402
    QColor,
    QDragEnterEvent,
    QDropEvent,
    QImage,
    QKeyEvent,
    QKeySequence,
    QMouseEvent,
)
from PySide6.QtWidgets import QApplication, QToolButton  # noqa: E402

# A QApplication must exist before any QFont/QWidget is constructed.
_APP = QApplication.instance() or QApplication([])

from pdftexteditor.document import PDFDocument  # noqa: E402
from pdftexteditor.signatures import SignatureLibrary  # noqa: E402
from pdftexteditor.stamps import STAMP_KINDS, stamp_png  # noqa: E402
from pdftexteditor.ui.main_window import MainWindow  # noqa: E402
from pdftexteditor.ui.signature_dialog import (  # noqa: E402
    SignatureDialog,
    strokes_to_png,
)

FIXTURE_DIR = os.path.join(_HERE, "fixtures")
FORM_LIKE = os.path.join(FIXTURE_DIR, "form_like.pdf")
ROTATED = os.path.join(FIXTURE_DIR, "rotated_doc.pdf")
IMAGE_DOC = os.path.join(FIXTURE_DIR, "image_doc.pdf")

# The authored logo geometry comes from the BUILDER (single source of
# truth), so the M3 asserts can never drift from the committed fixture.
if FIXTURE_DIR not in sys.path:
    sys.path.insert(0, FIXTURE_DIR)
from build_image_fixtures import (  # noqa: E402
    LOGO_PX,
    PAGE0_LOGO_RECT,
    PAGE1_LOGO_RECT,
)

# A pixel-verified BLANK region of form_like.pdf page 0 (probe: region ink 0
# in (380, 600, 540, 720)) -- placements land here so the ink delta is pure
# image ink.
BLANK_RECT = (400.0, 610.0, 520.0, 690.0)

# Base-14 built-in basefonts (the tests/test_app.py set): the coexistence
# check asserts the save introduces NO NEW one beyond the fixture's own
# pre-existing form chrome (form_like carries Helvetica widgets natively).
_BASE14_BASEFONTS = {
    "helvetica", "helvetica-bold", "helvetica-oblique", "helvetica-boldoblique",
    "times-roman", "times-bold", "times-italic", "times-bolditalic",
    "courier", "courier-bold", "courier-oblique", "courier-boldoblique",
    "symbol", "zapfdingbats",
}


def check(failures: list[str], tag: str, cond: bool, msg: str) -> bool:
    if not cond:
        failures.append(f"{tag}: {msg}")
    return cond


def pump(n: int = 4) -> None:
    for _ in range(n):
        _APP.processEvents()


def make_test_png(w: int = 120, h: int = 80) -> bytes:
    """The in-test RGBA source: LEFT half red, RIGHT half blue (asymmetric
    for the rotation-orientation assert) + a transparent bottom stripe so
    the inserted image must carry an SMask."""
    pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, w, h), 1)
    pix.set_rect(fitz.IRect(0, 0, w // 2, h), (255, 0, 0, 255))
    pix.set_rect(fitz.IRect(w // 2, 0, w, h), (0, 0, 255, 255))
    pix.set_rect(fitz.IRect(0, h - 10, w, h), (0, 0, 0, 0))
    return pix.tobytes("png")


def temp_copy(tmpdir: str, fixture_path: str) -> str:
    dst = os.path.join(tmpdir, os.path.basename(fixture_path))
    shutil.copy(fixture_path, dst)
    return dst


def open_window(path: str | None) -> MainWindow:
    w = MainWindow()
    w.resize(1300, 950)
    w.show()
    if path is not None:
        w.open_path(path)
    pump(6)
    if getattr(w, "empty_state", None) is not None:
        w.empty_state.hide()
    pump(2)
    return w


def close_window(w: MainWindow) -> None:
    """Close WITHOUT the dirty-discard modal (blocks forever offscreen)."""
    w._suppress_close_guard = True
    w.close()


def send_mouse(view, scene_pt: QPointF, etype,
               button=Qt.LeftButton, buttons=Qt.LeftButton,
               modifiers=Qt.NoModifier) -> None:
    """Dispatch one mouse event to the viewport at a scene point, exactly as
    a real click would arrive (the tests/test_reflow.py pattern)."""
    vp = view.viewport()
    view_pt = view.mapFromScene(scene_pt)
    gp = vp.mapToGlobal(view_pt)
    ev = QMouseEvent(etype, QPointF(view_pt), QPointF(gp),
                     button, buttons, modifiers)
    _APP.sendEvent(vp, ev)


def click(view, scene_pt: QPointF) -> None:
    send_mouse(view, scene_pt, QEvent.MouseButtonPress)
    send_mouse(view, scene_pt, QEvent.MouseButtonRelease, buttons=Qt.NoButton)
    pump(3)


def drag(view, p0: QPointF, p1: QPointF) -> None:
    send_mouse(view, p0, QEvent.MouseButtonPress)
    mid = QPointF((p0.x() + p1.x()) / 2, (p0.y() + p1.y()) / 2)
    send_mouse(view, mid, QEvent.MouseMove)
    send_mouse(view, p1, QEvent.MouseMove)
    send_mouse(view, p1, QEvent.MouseButtonRelease, buttons=Qt.NoButton)
    pump(3)


def pix_region_ink(pix, bbox: tuple, scale: float) -> int:
    """Non-white pixels (Rec. 601 luma < 230) inside a TEXT-SPACE bbox of an
    UNROTATED page's pixmap rendered at ``scale``."""
    x0 = max(0, int(bbox[0] * scale))
    y0 = max(0, int(bbox[1] * scale))
    x1 = min(pix.width, int(bbox[2] * scale) + 1)
    y1 = min(pix.height, int(bbox[3] * scale) + 1)
    s, n = pix.samples, pix.n
    stride = pix.width * n
    count = 0
    for y in range(y0, y1):
        row = y * stride
        for x in range(x0, x1):
            i = row + x * n
            r, g, b = s[i], s[i + 1], s[i + 2]
            if 0.299 * r + 0.587 * g + 0.114 * b < 230:
                count += 1
    return count


def pix_ink(pix) -> int:
    """Whole-pixmap ink count (the tests/test_editor.py helper)."""
    s, step, n = pix.samples, pix.n, 0
    for i in range(0, len(s), step):
        r, g, b = s[i], s[i + 1], s[i + 2]
        if (0.299 * r + 0.587 * g + 0.114 * b) < 230:
            n += 1
    return n


def saved_image_infos(doc: PDFDocument, tmpdir: str, name: str) -> list[dict]:
    """save_as -> fitz reopen -> page-0 ``get_image_info(xrefs=True)`` plus
    each xref's smask (read while the doc is alive)."""
    out_path = os.path.join(tmpdir, name)
    doc.save_as(out_path)
    saved = fitz.open(out_path)
    try:
        infos = []
        for info in saved[0].get_image_info(xrefs=True):
            entry = {"xref": info["xref"], "bbox": tuple(info["bbox"])}
            if info["xref"]:
                entry["smask"] = saved.extract_image(info["xref"])["smask"]
            infos.append(entry)
        return infos
    finally:
        saved.close()


def rect_close(a: tuple, b: tuple, tol: float = 0.5) -> bool:
    return all(abs(x - y) <= tol for x, y in zip(a, b))


def saved_basefonts(path: str) -> set[str]:
    """Page-0 basefonts of a saved PDF, lowercased, subset tags stripped
    (the tests/test_app.py pattern)."""
    doc = fitz.open(path)
    try:
        out = set()
        for entry in doc[0].get_fonts(full=True):
            base = entry[3] or ""
            if len(base) > 7 and base[6] == "+" and base[:6].isalpha():
                base = base[7:]
            out.add(base.lower())
        return out
    finally:
        doc.close()


def red_blue_centroids(pix) -> tuple:
    """((rx, ry, n_red), (bx, by, n_blue)) pixel centroids of the saturated
    red / blue regions -- the orientation probe for the asymmetric image."""
    s, n, w = pix.samples, pix.n, pix.width
    rx = ry = rc = bx = by = bc = 0
    for y in range(pix.height):
        row = y * w * n
        for x in range(w):
            i = row + x * n
            r, g, b = s[i], s[i + 1], s[i + 2]
            if r > 200 and g < 80 and b < 80:
                rx += x; ry += y; rc += 1
            elif b > 200 and r < 80 and g < 80:
                bx += x; by += y; bc += 1
    return ((rx / max(rc, 1), ry / max(rc, 1), rc),
            (bx / max(bc, 1), by / max(bc, 1), bc))


# --------------------------------------------------------------------------
# M2 helpers: signature strokes, QImage scans, stamp-red region counts
# --------------------------------------------------------------------------
# The "Notes:" word on form_like page 0 (probe-verified bbox): T15 places a
# transparent signature OVER it and asserts its ink survives.
NOTES_WORD = (60.0, 397.2, 97.3, 413.7)

# Synthetic signature strokes on the dialog's 620x220 canvas: a tiny anchor
# dot at the top-left + a zigzag scribble confined to the bottom-right, so
# the exported PNG spans most of the canvas but stays TRANSPARENT in the
# upper-middle band -- the placed word lands there in T15.
SIG_DOT = [(2.0, 2.0), (6.0, 6.0)]
SIG_SCRIBBLE = [(300.0 + i * 10.0, 150.0 + (i % 2) * 40.0) for i in range(30)]


def qimage_opaque_count(img: QImage, step: int = 2) -> int:
    """Sampled count of near-opaque pixels (alpha > 200)."""
    count = 0
    for y in range(0, img.height(), step):
        for x in range(0, img.width(), step):
            if img.pixelColor(x, y).alpha() > 200:
                count += 1
    return count


def qimage_sample_ink_color(img: QImage) -> QColor | None:
    """The first fully-opaque pixel's color (None when the image is blank)."""
    for y in range(img.height()):
        for x in range(img.width()):
            c = img.pixelColor(x, y)
            if c.alpha() > 250:
                return c
    return None


def pix_region_red(pix, bbox: tuple, scale: float) -> int:
    """Saturated-red pixels (the stamp ink, #B91C1C-ish) inside a TEXT-SPACE
    bbox of an unrotated page's pixmap rendered at ``scale``."""
    x0 = max(0, int(bbox[0] * scale))
    y0 = max(0, int(bbox[1] * scale))
    x1 = min(pix.width, int(bbox[2] * scale) + 1)
    y1 = min(pix.height, int(bbox[3] * scale) + 1)
    s, n = pix.samples, pix.n
    stride = pix.width * n
    count = 0
    for y in range(y0, y1):
        row = y * stride
        for x in range(x0, x1):
            i = row + x * n
            r, g, b = s[i], s[i + 1], s[i + 2]
            if r > 120 and g < 90 and b < 90:
                count += 1
    return count


def canvas_stroke(canvas, pts) -> None:
    """Synthesize one mouse stroke (press / moves / release) on the
    signature canvas widget."""
    p0 = QPointF(*pts[0])
    _APP.sendEvent(canvas, QMouseEvent(
        QEvent.MouseButtonPress, p0, canvas.mapToGlobal(p0),
        Qt.LeftButton, Qt.LeftButton, Qt.NoModifier))
    for p in pts[1:]:
        pt = QPointF(*p)
        _APP.sendEvent(canvas, QMouseEvent(
            QEvent.MouseMove, pt, canvas.mapToGlobal(pt),
            Qt.NoButton, Qt.LeftButton, Qt.NoModifier))
    pend = QPointF(*pts[-1])
    _APP.sendEvent(canvas, QMouseEvent(
        QEvent.MouseButtonRelease, pend, canvas.mapToGlobal(pend),
        Qt.LeftButton, Qt.NoButton, Qt.NoModifier))
    pump(2)


class _StubSignatureDialog:
    """The ``_signature_dialog_factory`` test double: pretends the user
    drew ``png`` and accepted with the library save on -- no modal loop,
    no widgets (the dialog-seam rule)."""

    def __init__(self, png: bytes, name: str = "Jordan Carter"):
        self._png = png
        self._name = name

    def __call__(self, parent=None):      # the factory protocol
        return self

    def exec(self) -> int:
        return 1

    def result_png(self) -> bytes:
        return self._png

    def save_to_library(self) -> bool:
        return True

    def signature_name(self) -> str:
        return self._name


# ==========================================================================
# T1: model staging -- census, validation, clamp, rendered ink
# ==========================================================================
def test_t1_model_staging(failures: list[str]) -> None:
    tag = "T1_model_staging"
    tmpdir = tempfile.mkdtemp(prefix="img_t1_")
    doc = PDFDocument(temp_copy(tmpdir, FORM_LIKE))
    try:
        png = make_test_png()
        scale = 2.0
        pristine_ink = pix_region_ink(doc.render_with_edits(0, scale),
                                      BLANK_RECT, scale)
        check(failures, tag, pristine_ink == 0,
              f"placement region not blank pristine ({pristine_ink}px)")

        # Validation raises BEFORE any state changes.
        try:
            doc.add_image(0, BLANK_RECT, b"not an image at all")
            failures.append(f"{tag}: non-image bytes did not raise")
        except ValueError:
            pass
        check(failures, tag, doc.edit_count == 0 and not doc.dirty,
              "failed validation left staged state behind")

        box = doc.add_image(0, BLANK_RECT, png, natural_px=(120, 80))
        check(failures, tag, doc.edit_count == 1,
              f"edit_count {doc.edit_count} != 1 after add_image")
        check(failures, tag, doc.dirty, "doc not dirty after add_image")
        check(failures, tag, doc.has_edits, "has_edits False after add_image")
        check(failures, tag, box.identity == (0, "img", 1),
              f"unexpected identity {box.identity}")
        ink = pix_region_ink(doc.render_with_edits(0, scale),
                             BLANK_RECT, scale)
        check(failures, tag, ink > 1000,
              f"placed rect carries almost no rendered ink ({ink}px)")

        # An off-page rect clamps into the page (the §2.3 clamp).
        page_rect = doc.working[0].rect
        clamped = doc.add_image(0, (page_rect.x1 - 50, page_rect.y1 - 30,
                                    page_rect.x1 + 70, page_rect.y1 + 50),
                                png)
        x0, y0, x1, y1 = clamped.rect
        check(failures, tag,
              x1 <= page_rect.x1 + 1e-6 and y1 <= page_rect.y1 + 1e-6
              and x0 >= page_rect.x0 - 1e-6 and y0 >= page_rect.y0 - 1e-6,
              f"off-page rect not clamped: {clamped.rect}")
        check(failures, tag,
              abs((x1 - x0) - 120.0) < 1e-6 and abs((y1 - y0) - 80.0) < 1e-6,
              f"clamp distorted the size: {clamped.rect}")
    finally:
        doc.close()
        shutil.rmtree(tmpdir, ignore_errors=True)


# ==========================================================================
# T2: save round trip -- bbox ±0.5pt, smask, WYSIWYG < 0.02
# ==========================================================================
def test_t2_save_roundtrip(failures: list[str]) -> None:
    tag = "T2_save_roundtrip"
    tmpdir = tempfile.mkdtemp(prefix="img_t2_")
    doc = PDFDocument(temp_copy(tmpdir, FORM_LIKE))
    try:
        doc.add_image(0, BLANK_RECT, make_test_png(), natural_px=(120, 80))
        infos = saved_image_infos(doc, tmpdir, "roundtrip.pdf")
        if not check(failures, tag, len(infos) == 1,
                     f"saved page carries {len(infos)} images, expected 1"):
            return
        check(failures, tag, rect_close(infos[0]["bbox"], BLANK_RECT),
              f"saved bbox {infos[0]['bbox']} != staged rect {BLANK_RECT}")
        check(failures, tag, infos[0].get("smask", 0) != 0,
              "RGBA source saved without an SMask (transparency lost)")

        # WYSIWYG: rendered ink == saved ink (rel diff < 0.02).
        scale = 2.0
        rink = pix_ink(doc.render_with_edits(0, scale))
        saved = fitz.open(os.path.join(tmpdir, "roundtrip.pdf"))
        try:
            sink = pix_ink(saved[0].get_pixmap(
                matrix=fitz.Matrix(scale, scale), alpha=False))
        finally:
            saved.close()
        rel = abs(rink - sink) / max(sink, 1)
        check(failures, tag, rel < 0.02,
              f"render vs saved ink rel diff {rel:.4f} >= 0.02 "
              f"({rink} vs {sink})")
    finally:
        doc.close()
        shutil.rmtree(tmpdir, ignore_errors=True)


# ==========================================================================
# T3: undo -> byte-identical pristine pixels; redo restores
# ==========================================================================
def test_t3_undo_redo(failures: list[str]) -> None:
    tag = "T3_undo_redo"
    tmpdir = tempfile.mkdtemp(prefix="img_t3_")
    doc = PDFDocument(temp_copy(tmpdir, FORM_LIKE))
    try:
        scale = 1.5
        pristine = doc.render_with_edits(0, scale)
        doc.add_image(0, BLANK_RECT, make_test_png(), natural_px=(120, 80))
        staged_ink = pix_region_ink(doc.render_with_edits(0, scale),
                                    BLANK_RECT, scale)
        doc.undo()
        check(failures, tag, doc.edit_count == 0,
              f"edit_count {doc.edit_count} != 0 after undo")
        after = doc.render_with_edits(0, scale)
        check(failures, tag, after.samples == pristine.samples,
              "undo did not return byte-identical pristine pixels")
        doc.redo()
        check(failures, tag, doc.edit_count == 1
              and len(doc.image_boxes(0)) == 1,
              "redo did not restore the staged image")
        ink2 = pix_region_ink(doc.render_with_edits(0, scale),
                              BLANK_RECT, scale)
        check(failures, tag, ink2 == staged_ink,
              f"redo ink {ink2} != staged ink {staged_ink}")
    finally:
        doc.close()
        shutil.rmtree(tmpdir, ignore_errors=True)


# ==========================================================================
# T4: move / resize / delete round-trip to saved rects
# ==========================================================================
def test_t4_move_resize_delete(failures: list[str]) -> None:
    tag = "T4_move_resize_delete"
    tmpdir = tempfile.mkdtemp(prefix="img_t4_")
    doc = PDFDocument(temp_copy(tmpdir, FORM_LIKE))
    try:
        png = make_test_png()
        box = doc.add_image(0, BLANK_RECT, png, natural_px=(120, 80))

        doc.move_image(0, box, -30.0, 25.0)
        moved = (BLANK_RECT[0] - 30.0, BLANK_RECT[1] + 25.0,
                 BLANK_RECT[2] - 30.0, BLANK_RECT[3] + 25.0)
        infos = saved_image_infos(doc, tmpdir, "moved.pdf")
        check(failures, tag, len(infos) == 1
              and rect_close(infos[0]["bbox"], moved),
              f"saved moved bbox {infos} != {moved}")

        target = (100.0, 100.0, 190.0, 160.0)
        doc.resize_image(0, doc.image_boxes(0)[0], target)
        infos = saved_image_infos(doc, tmpdir, "resized.pdf")
        check(failures, tag, len(infos) == 1
              and rect_close(infos[0]["bbox"], target),
              f"saved resized bbox {infos} != {target}")

        doc.delete_image(0, doc.image_boxes(0)[0])
        check(failures, tag, doc.edit_count == 0,
              f"edit_count {doc.edit_count} != 0 after delete")
        infos = saved_image_infos(doc, tmpdir, "deleted.pdf")
        check(failures, tag, len(infos) == 0,
              f"deleted image still in the saved file: {infos}")

        # The undo chain walks all the way back: delete -> resize -> move ->
        # add, each restoring its before-state.
        doc.undo()
        check(failures, tag, doc.image_boxes(0)
              and rect_close(doc.image_boxes(0)[0].rect, target),
              "undo(delete) did not restore the resized rect")
        doc.undo()
        check(failures, tag, rect_close(doc.image_boxes(0)[0].rect, moved),
              "undo(resize) did not restore the moved rect")
        doc.undo()
        check(failures, tag, rect_close(doc.image_boxes(0)[0].rect,
                                        BLANK_RECT),
              "undo(move) did not restore the original rect")
        doc.undo()
        check(failures, tag, not doc.image_boxes(0) and doc.edit_count == 0,
              "undo(add) did not unstage the image")
    finally:
        doc.close()
        shutil.rmtree(tmpdir, ignore_errors=True)


# ==========================================================================
# T5: coexistence with an in-place text edit (the soul invariant)
# ==========================================================================
def test_t5_text_coexistence(failures: list[str]) -> None:
    tag = "T5_text_coexistence"
    tmpdir = tempfile.mkdtemp(prefix="img_t5_")
    src = temp_copy(tmpdir, FORM_LIKE)
    fixture_fonts = saved_basefonts(src)
    doc = PDFDocument(src)
    try:
        target = None
        for span in doc.spans(0):
            if "Synthetic" in getattr(span, "text", ""):
                target = span
                break
        if not check(failures, tag, target is not None,
                     "no 'Synthetic' span on form_like page 0"):
            return
        doc.stage_edit(0, target,
                       doc.staged_text(0, target).replace("Synthetic",
                                                          "Generated"))
        doc.add_image(0, BLANK_RECT, make_test_png(), natural_px=(120, 80))
        out_path = os.path.join(tmpdir, "coexist.pdf")
        doc.save_as(out_path)

        saved = fitz.open(out_path)
        try:
            text = saved[0].get_text()
            infos = saved[0].get_image_info(xrefs=True)
        finally:
            saved.close()
        check(failures, tag, "Generated" in text and "Synthetic" not in text,
              "text edit did not survive beside the placed image")
        check(failures, tag, len(infos) == 1
              and rect_close(tuple(infos[0]["bbox"]), BLANK_RECT),
              f"image bbox wrong beside the text edit: {infos}")
        # No NEW base-14 face: the reinserted run keeps the original family
        # (form_like natively carries Helvetica form chrome -- only a NEW
        # base-14 face would mean the pipeline degraded).
        new_base14 = [b for b in (saved_basefonts(out_path) - fixture_fonts)
                      if b in _BASE14_BASEFONTS]
        check(failures, tag, new_base14 == [],
              f"save introduced base-14 face(s): {new_base14}")
    finally:
        doc.close()
        shutil.rmtree(tmpdir, ignore_errors=True)


# ==========================================================================
# T6: structural rotate bakes the staged image into working
# ==========================================================================
def test_t6_rotate_bakes(failures: list[str]) -> None:
    tag = "T6_rotate_bakes"
    tmpdir = tempfile.mkdtemp(prefix="img_t6_")
    doc = PDFDocument(temp_copy(tmpdir, FORM_LIKE))
    try:
        doc.add_image(0, BLANK_RECT, make_test_png(), natural_px=(120, 80))
        doc.rotate_page(0, 90)
        check(failures, tag, not doc._images and doc.edit_count == 0,
              "staged image map not cleared by the structural bake")
        baked = doc.working[0].get_image_info(xrefs=True)
        check(failures, tag, len(baked) == 1,
              f"working[0] carries {len(baked)} images after the bake")
        infos = saved_image_infos(doc, tmpdir, "rotated_baked.pdf")
        check(failures, tag, len(infos) == 1
              and rect_close(infos[0]["bbox"], BLANK_RECT),
              f"baked image did not survive the save: {infos}")
    finally:
        doc.close()
        shutil.rmtree(tmpdir, ignore_errors=True)


# ==========================================================================
# T7: rotated-page placement reads upright (red half display-LEFT)
# ==========================================================================
def test_t7_rotated_placement(failures: list[str]) -> None:
    tag = "T7_rotated_placement"
    tmpdir = tempfile.mkdtemp(prefix="img_t7_")
    doc = PDFDocument(temp_copy(tmpdir, ROTATED))
    try:
        check(failures, tag, doc.page_rotation(0) == 90,
              "rotated fixture lost its /Rotate 90")
        rect = (100.0, 100.0, 220.0, 180.0)   # probe-verified blank
        doc.add_image(0, rect, make_test_png(), natural_px=(120, 80))
        scale = 1.5
        pix = doc.render_with_edits(0, scale)
        (rcx, rcy, rc), (bcx, bcy, bc) = red_blue_centroids(pix)
        if not check(failures, tag, rc > 500 and bc > 500,
                     f"image halves not found ({rc} red px, {bc} blue px)"):
            return
        # Upright on screen (probe-pinned: rotate=page.rotation): the source
        # image's LEFT half (red) renders display-LEFT of the blue half.
        check(failures, tag, rcx < bcx,
              f"red centroid x {rcx:.0f} not left of blue {bcx:.0f} -- "
              "image not upright on the rotated page")
        # Both halves land inside the rotation-matrix-mapped display rect.
        m = doc.rotation_matrix(0)
        pts = [fitz.Point(x, y) * m
               for x, y in ((rect[0], rect[1]), (rect[2], rect[1]),
                            (rect[2], rect[3]), (rect[0], rect[3]))]
        xs = [p.x * scale for p in pts]
        ys = [p.y * scale for p in pts]
        disp = (min(xs) - 2, min(ys) - 2, max(xs) + 2, max(ys) + 2)
        for cx, cy, name in ((rcx, rcy, "red"), (bcx, bcy, "blue")):
            check(failures, tag,
                  disp[0] <= cx <= disp[2] and disp[1] <= cy <= disp[3],
                  f"{name} centroid ({cx:.0f},{cy:.0f}) outside the "
                  f"display-mapped rect {disp}")
        # And the saved file renders identically (WYSIWYG on the rotation).
        out_path = os.path.join(tmpdir, "rotated_placed.pdf")
        doc.save_as(out_path)
        saved = fitz.open(out_path)
        try:
            spix = saved[0].get_pixmap(matrix=fitz.Matrix(scale, scale),
                                       alpha=False)
        finally:
            saved.close()
        (srx, _, _), (sbx, _, _) = red_blue_centroids(spix)
        check(failures, tag, srx < sbx,
              "saved file's image orientation differs from the screen")
    finally:
        doc.close()
        shutil.rmtree(tmpdir, ignore_errors=True)


# ==========================================================================
# T8: render_signature folds image state; the view cache misses
# ==========================================================================
def test_t8_cache_miss(failures: list[str]) -> None:
    tag = "T8_cache_miss"
    tmpdir = tempfile.mkdtemp(prefix="img_t8_")
    w = open_window(temp_copy(tmpdir, FORM_LIKE))
    try:
        v, doc = w.view, w.document
        scale = v._zoom * (v.devicePixelRatioF() or 1.0)
        sig0 = doc.render_signature(0)
        check(failures, tag, v._render_cache.get(0, scale, sig0) is not None,
              "materialized page 0 has no cache entry for its signature")
        doc.add_image(0, BLANK_RECT, make_test_png(), natural_px=(120, 80))
        sig1 = doc.render_signature(0)
        check(failures, tag, sig1 != sig0,
              "render_signature did not change after add_image")
        check(failures, tag, v._render_cache.get(0, scale, sig1) is None,
              "pixmap cache HIT for the post-add signature (stale pixels)")
        doc.move_image(0, doc.image_boxes(0)[0], 5.0, 5.0)
        check(failures, tag, doc.render_signature(0) != sig1,
              "render_signature did not change after move_image")
        doc.undo()
        check(failures, tag, doc.render_signature(0) == sig1,
              "render_signature did not revert on undo")
        doc.undo()
        check(failures, tag, doc.render_signature(0) == sig0,
              "render_signature did not revert to pristine on full undo")
    finally:
        close_window(w)
        shutil.rmtree(tmpdir, ignore_errors=True)


# ==========================================================================
# T9: UI placement -- click, drag, Delete key, undo chain, Esc
# ==========================================================================
def test_t9_ui_placement(failures: list[str]) -> None:
    tag = "T9_ui_placement"
    tmpdir = tempfile.mkdtemp(prefix="img_t9_")
    w = open_window(temp_copy(tmpdir, FORM_LIKE))
    try:
        v, doc = w.view, w.document
        png = make_test_png()

        # Click-to-place: ONE command, default size, auto-exit, selection.
        v.enter_place_image_mode({"image": png, "natural_px": (120, 80),
                                  "kind": "file"})
        pump(2)
        check(failures, tag, v.current_mode() == "place_image",
              f"mode {v.current_mode()!r} after enter_place_image_mode")
        idx0 = w.undo_stack.index()
        click(v, v._scene_point(420, 640, page_index=0))
        check(failures, tag, w.undo_stack.index() == idx0 + 1,
              f"click placed {w.undo_stack.index() - idx0} commands, not 1")
        check(failures, tag, v.current_mode() == "select",
              "place mode did not auto-exit after the click")
        boxes = doc.image_boxes(0)
        if not check(failures, tag, len(boxes) == 1, "no staged image"):
            return
        x0, y0, x1, y1 = boxes[0].rect
        # Default width = min(120*72/96 = 90, 0.45*612 = 275.4) = 90;
        # height by the 80/120 aspect = 60; click = top-left.
        check(failures, tag,
              abs((x1 - x0) - 90.0) < 0.5 and abs((y1 - y0) - 60.0) < 0.5,
              f"default placement size {(x1 - x0, y1 - y0)} != (90, 60)")
        check(failures, tag, abs(x0 - 420) < 1.0 and abs(y0 - 640) < 1.0,
              f"top-left {(x0, y0)} != click point (420, 640)")
        sel = v.current_selection()
        check(failures, tag, sel is not None
              and getattr(sel, "identity", (None,) * 3)[1] == "img",
              "fresh image not selected after the place")
        check(failures, tag,
              type(v._overlay).__name__ == "_ImageSelectionOverlay",
              f"overlay {type(v._overlay).__name__} is not the image overlay")

        # Delete key = ONE command; selection clears.
        idx1 = w.undo_stack.index()
        _APP.sendEvent(v, QKeyEvent(QKeyEvent.Type.KeyPress, Qt.Key_Delete,
                                    Qt.NoModifier))
        pump(2)
        check(failures, tag, w.undo_stack.index() == idx1 + 1
              and not doc.image_boxes(0),
              "Delete key did not produce exactly one img_delete")
        check(failures, tag, v.current_selection() is None,
              "selection survived its image's delete")

        # The window stack walks the whole chain back to pristine.
        w.undo_stack.undo()
        pump(1)
        check(failures, tag, len(doc.image_boxes(0)) == 1,
              "undo(delete) did not restore the image")
        w.undo_stack.undo()
        pump(1)
        check(failures, tag, not doc.image_boxes(0) and doc.edit_count == 0,
              "undo(add) did not return to pristine")
        w.undo_stack.redo()
        pump(1)
        check(failures, tag, len(doc.image_boxes(0)) == 1,
              "redo(add) did not restore the image")
        w.undo_stack.undo()
        pump(1)

        # Drag-to-place: the aspect-locked rubber rect lands as drawn.
        v.enter_place_image_mode({"image": png, "natural_px": (120, 80),
                                  "kind": "file"})
        pump(1)
        idx2 = w.undo_stack.index()
        drag(v, v._scene_point(100, 300, page_index=0),
             v._scene_point(220, 420, page_index=0))
        check(failures, tag, w.undo_stack.index() == idx2 + 1,
              "drag-place did not land exactly one command")
        boxes = doc.image_boxes(0)
        if check(failures, tag, len(boxes) == 1, "no drag-placed image"):
            x0, y0, x1, y1 = boxes[0].rect
            check(failures, tag, abs(x0 - 100) < 1.5 and abs(y0 - 300) < 1.5
                  and abs((x1 - x0) - 120) < 1.5,
                  f"drag-placed rect {boxes[0].rect} != drawn rect")
            # Aspect locked to the source (80/120): height = width * 2/3.
            check(failures, tag,
                  abs((y1 - y0) - (x1 - x0) * (80.0 / 120.0)) < 1.5,
                  f"drag-placed rect {boxes[0].rect} not aspect-locked")
        w.undo_stack.undo()
        pump(1)

        # Esc disarms the armed mode back to select.
        v.enter_place_image_mode({"image": png, "natural_px": (120, 80),
                                  "kind": "file"})
        pump(1)
        _APP.sendEvent(v, QKeyEvent(QKeyEvent.Type.KeyPress, Qt.Key_Escape,
                                    Qt.NoModifier))
        pump(1)
        check(failures, tag, v.current_mode() == "select",
              "Esc did not disarm place_image")
    finally:
        close_window(w)
        shutil.rmtree(tmpdir, ignore_errors=True)


# ==========================================================================
# T10: UI move / resize gestures on the selected image
# ==========================================================================
def test_t10_ui_move_resize(failures: list[str]) -> None:
    tag = "T10_ui_move_resize"
    tmpdir = tempfile.mkdtemp(prefix="img_t10_")
    w = open_window(temp_copy(tmpdir, FORM_LIKE))
    try:
        v, doc = w.view, w.document
        w._on_box_command_requested("img_add", None, {
            "rect": BLANK_RECT, "image": make_test_png(),
            "kind": "file", "natural_px": (120, 80)})
        pump(2)
        box = doc.image_boxes(0)[0]
        sel = v.current_selection()
        check(failures, tag, sel is not None
              and sel.identity == box.identity,
              "img_add did not select the placed image")

        # Body drag = ONE 'img_move' with the scene delta / zoom.
        r = v._image_scene_rect(box)
        idx0 = w.undo_stack.index()
        z = v._zoom
        drag(v, r.center(), QPointF(r.center().x() + 40,
                                    r.center().y() + 25))
        check(failures, tag, w.undo_stack.index() == idx0 + 1,
              f"move drag took {w.undo_stack.index() - idx0} commands, not 1")
        moved = doc.image_boxes(0)[0]
        check(failures, tag,
              abs(moved.rect[0] - (BLANK_RECT[0] + 40 / z)) < 0.75
              and abs(moved.rect[1] - (BLANK_RECT[1] + 25 / z)) < 0.75,
              f"moved rect {moved.rect} != start + scene delta / zoom")
        sel = v.current_selection()
        check(failures, tag, sel is not None
              and sel.identity == box.identity,
              "image selection lost across the move repaint")

        # SE-handle drag = ONE 'img_resize' tracking the dragged rect.
        r = v._image_scene_rect(moved)
        idx1 = w.undo_stack.index()
        check(failures, tag,
              v._on_handle_press("se", QPointF(r.right(), r.bottom())),
              "handle press not taken for the image selection")
        v._update_drag(QPointF(r.right() + 30, r.bottom() + 15))
        v._finish_drag(QPointF(r.right() + 30, r.bottom() + 15))
        pump(2)
        check(failures, tag, w.undo_stack.index() == idx1 + 1,
              f"resize took {w.undo_stack.index() - idx1} commands, not 1")
        resized = doc.image_boxes(0)[0]
        check(failures, tag,
              abs(resized.rect[2] - (moved.rect[2] + 30 / z)) < 0.75
              and abs(resized.rect[3] - (moved.rect[3] + 15 / z)) < 0.75
              and abs(resized.rect[0] - moved.rect[0]) < 0.01
              and abs(resized.rect[1] - moved.rect[1]) < 0.01,
              f"resized rect {resized.rect} does not track the SE drag "
              f"from {moved.rect}")
        # One undo per gesture restores each prior rect.
        w.undo_stack.undo()
        pump(1)
        check(failures, tag,
              rect_close(doc.image_boxes(0)[0].rect, moved.rect, 0.01),
              "undo(resize) did not restore the moved rect")
        w.undo_stack.undo()
        pump(1)
        check(failures, tag,
              rect_close(doc.image_boxes(0)[0].rect, BLANK_RECT, 0.01),
              "undo(move) did not restore the placed rect")
    finally:
        close_window(w)
        shutil.rmtree(tmpdir, ignore_errors=True)


# ==========================================================================
# T11: chrome -- menu anchor, toolbar hide/show, seam, drag&drop
# ==========================================================================
def test_t11_chrome(failures: list[str]) -> None:
    tag = "T11_chrome"
    tmpdir = tempfile.mkdtemp(prefix="img_t11_")
    png_path = os.path.join(tmpdir, "synthetic_logo.png")
    with open(png_path, "wb") as fh:
        fh.write(make_test_png())
    bad_path = os.path.join(tmpdir, "not_an_image.png")
    with open(bad_path, "wb") as fh:
        fh.write(b"this is not image data")

    w = open_window(None)                 # NO document: the empty state
    try:
        check(failures, tag,
              w.act_insert_image.shortcut() == QKeySequence("Ctrl+Shift+I"),
              f"shortcut {w.act_insert_image.shortcut().toString()!r} != "
              "Ctrl+Shift+I")
        check(failures, tag, not w.act_insert_image.isEnabled(),
              "Insert Image enabled with no document")
        # Charcoal redesign: Insert Image is a rail ACTION button (bottom of the
        # activity rail), not a top-toolbar button; it is disabled with no doc.
        check(failures, tag,
              not w.left_panel._action_buttons["image"].isEnabled(),
              "rail Insert-Image action enabled in the empty state")
        # Tools menu: the entry sits BEFORE the tools_objects anchor.
        acts = w.menu_tools.actions()
        anchor_i = acts.index(w.menu_anchors["tools_objects"])
        check(failures, tag,
              "Image from File…" in [a.text() for a in acts[:anchor_i]],
              "Image from File… not inserted at the tools_objects anchor")
        # The seam refuses to arm without a document.
        w._do_insert_image(path=png_path)
        pump(1)
        check(failures, tag, w.view.current_mode() == "select",
              "place mode armed with no document open")

        w.open_path(temp_copy(tmpdir, FORM_LIKE))
        pump(6)
        check(failures, tag, w.act_insert_image.isEnabled(),
              "Insert Image still disabled after open")
        check(failures, tag, w.left_panel._action_buttons["image"].isEnabled(),
              "rail Insert-Image action still disabled after open")

        # The dialog seam arms the canvas place mode with the file payload.
        w._do_insert_image(path=png_path)
        pump(1)
        check(failures, tag, w.view.current_mode() == "place_image",
              "seam did not arm place_image")
        payload = w.view._image_place_payload or {}
        check(failures, tag, payload.get("natural_px") == (120, 80),
              f"payload natural_px {payload.get('natural_px')} != (120, 80)")
        w.view.exit_place_image_mode()
        pump(1)

        # A non-image payload flashes instead of arming.
        check(failures, tag, not w._place_image_from_file(bad_path),
              "bad image payload reported success")
        check(failures, tag, w.view.current_mode() == "select",
              "bad image payload armed place mode")

        # Dropping a PNG on the canvas arms place mode (the drop handler).
        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile(png_path)])
        pos = QPointF(w.canvas.rect().center())
        enter = QDragEnterEvent(pos.toPoint(), Qt.CopyAction, mime,
                                Qt.LeftButton, Qt.NoModifier)
        _APP.sendEvent(w.canvas, enter)
        check(failures, tag, enter.isAccepted(),
              "image dragEnter not accepted with a document open")
        drop = QDropEvent(pos, Qt.CopyAction, mime, Qt.LeftButton,
                          Qt.NoModifier)
        _APP.sendEvent(w.canvas, drop)
        pump(2)
        check(failures, tag, w.view.current_mode() == "place_image",
              "PNG drop did not arm place_image")
        w.view.exit_place_image_mode()
    finally:
        close_window(w)
        shutil.rmtree(tmpdir, ignore_errors=True)


# ==========================================================================
# T12: strokes_to_png -- the pure export core (alpha, crop, color, dot)
# ==========================================================================
def test_t12_strokes_to_png(failures: list[str]) -> None:
    tag = "T12_strokes_to_png"
    # A scribble confined to a 160x80 patch of the 620x220 canvas: the
    # export must crop to the ink, not the canvas.
    patch = [(100.0 + i * 8.0, 60.0 + (i % 2) * 80.0) for i in range(21)]
    png = strokes_to_png([patch], pen_width=3.0)
    check(failures, tag, png.startswith(b"\x89PNG\r\n\x1a\n"),
          "export is not a PNG")
    img = QImage.fromData(png)
    if not check(failures, tag, not img.isNull(), "export does not decode"):
        return
    check(failures, tag, img.hasAlphaChannel(),
          "export carries no alpha channel")
    # Cropped: the strokes span 160x80 (+ pen + 8px margin) at 3x -- far
    # below the full 620x220 canvas at 3x.
    check(failures, tag, img.width() < 620 * 3 * 0.5
          and img.height() < 220 * 3 * 0.7,
          f"export {img.width()}x{img.height()} not cropped to the ink")
    # ... but it does cover the ink bbox: 160 + pen/2*2 + margin*2/scale,
    # times the 3x supersample (±4px slack for ceil/AA).
    expect_w = (160 + 3.0 + 16.0 / 3.0) * 3
    check(failures, tag, abs(img.width() - expect_w) < 6,
          f"export width {img.width()} != ink bbox + margin ({expect_w:.0f})")
    opaque = qimage_opaque_count(img)
    check(failures, tag, opaque > 200,
          f"almost no opaque ink in the export ({opaque} samples)")
    corner = img.pixelColor(0, 0).alpha()
    check(failures, tag, corner == 0,
          f"margin corner not transparent (alpha {corner})")

    # Pen color rides through (the dialog's Blue).
    blue = strokes_to_png([patch], pen_width=3.0, color=QColor("#1D4ED8"))
    ink = qimage_sample_ink_color(QImage.fromData(blue))
    check(failures, tag, ink is not None and ink.blue() > 150
          and ink.blue() > ink.red() + 60,
          f"blue pen did not produce blue ink ({ink and ink.name()})")

    # A single-point stroke = a visible dot; empty input = b"".
    dot = strokes_to_png([[(50.0, 50.0)]], pen_width=4.0)
    dot_img = QImage.fromData(dot)
    check(failures, tag, not dot_img.isNull()
          and qimage_opaque_count(dot_img, step=1) > 0,
          "single-point stroke rendered no dot")
    check(failures, tag, strokes_to_png([]) == b""
          and strokes_to_png(None) == b"",
          "empty strokes did not export b''")


# ==========================================================================
# T13: SignatureLibrary CRUD in a tempdir (never the real ~/.pdftexteditor)
# ==========================================================================
def test_t13_library_crud(failures: list[str]) -> None:
    tag = "T13_library_crud"
    tmpdir = tempfile.mkdtemp(prefix="img_t13_")
    sig_dir = os.path.join(tmpdir, "sigs")
    png = strokes_to_png([SIG_SCRIBBLE])
    try:
        lib = SignatureLibrary(sig_dir)
        check(failures, tag, lib.dir == sig_dir,
              "injected dir not honored")
        # Lazy: constructing + listing creates NOTHING on disk.
        check(failures, tag, lib.list() == [] and not os.path.isdir(sig_dir),
              "library dir created before the first save")

        p1 = lib.save("Jordan Carter", png)
        check(failures, tag, os.path.basename(p1) == "Jordan Carter.png"
              and os.path.dirname(p1) == sig_dir and os.path.isfile(p1),
              f"save landed at {p1}")
        check(failures, tag, lib.load(p1) == png,
              "load(path) did not round-trip the bytes")

        # Name collision dedupes with the -2 suffix.
        p2 = lib.save("Jordan Carter", png)
        check(failures, tag,
              os.path.basename(p2) == "Jordan Carter-2.png",
              f"dedupe produced {os.path.basename(p2)}")

        # Path-hostile names sanitize to [A-Za-z0-9 _-].
        p3 = lib.save("Riley/Morgan: <sig>?", png)
        base = os.path.splitext(os.path.basename(p3))[0]
        check(failures, tag,
              os.path.dirname(p3) == sig_dir
              and all(c.isalnum() or c in " _-" for c in base)
              and "Riley" in base,
              f"hostile name not sanitized: {p3!r}")

        # Newest-first: age p1 well below the rest.
        os.utime(p1, (1_000_000, 1_000_000))
        names = [name for name, _path in lib.list()]
        check(failures, tag, len(names) == 3 and names[-1] == "Jordan Carter",
              f"list not newest-first: {names}")

        lib.delete(p2)
        check(failures, tag,
              [n for n, _p in lib.list()]
              == [n for n in names if n != "Jordan Carter-2"],
              "delete did not drop exactly the deleted entry")
        check(failures, tag, not os.path.exists(p2),
              "delete left the file behind")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ==========================================================================
# T14: SignatureDialog driven non-modally offscreen
# ==========================================================================
def test_t14_dialog_drive(failures: list[str]) -> None:
    tag = "T14_dialog_drive"
    dlg = SignatureDialog()
    try:
        dlg.show()                        # NON-modal: no exec() offscreen
        pump(2)
        check(failures, tag, dlg.canvas.strokes == [],
              "fresh canvas not empty")
        check(failures, tag, dlg.save_to_library(),
              "Save-to-library not checked by default")

        # Real mouse events append one plain-data stroke.
        canvas_stroke(dlg.canvas,
                      [(60.0, 100.0), (120.0, 60.0), (180.0, 120.0),
                       (240.0, 80.0)])
        check(failures, tag, len(dlg.canvas.strokes) == 1
              and len(dlg.canvas.strokes[0]) == 4
              and dlg.canvas.strokes[0][0] == (60.0, 100.0),
              f"stroke not recorded as plain data: {dlg.canvas.strokes}")

        # Clear resets the surface.
        dlg.clear_button.click()
        pump(1)
        check(failures, tag, dlg.canvas.strokes == [],
              "Clear did not reset the strokes")

        # Pen controls sync to the canvas (live preview parity, §6).
        dlg.pen_spin.setValue(5)
        dlg.color_combo.setCurrentIndex(1)        # Blue
        check(failures, tag, dlg.canvas.pen_width == 5.0
              and dlg.canvas.pen_color == QColor("#1D4ED8"),
              "pen controls did not sync to the canvas")

        # Injected strokes + accept -> the transparent result PNG.
        dlg.canvas.strokes = [list(SIG_SCRIBBLE)]
        dlg.name_field.setText("Jordan Carter")
        dlg.accept()
        pump(1)
        check(failures, tag, dlg.result() == 1, "accept did not Accept")
        png = dlg.result_png()
        img = QImage.fromData(png)
        check(failures, tag, png.startswith(b"\x89PNG") and not img.isNull()
              and img.hasAlphaChannel()
              and qimage_opaque_count(img) > 100,
              "accepted dialog produced no usable signature PNG")
        ink = qimage_sample_ink_color(img)
        check(failures, tag, ink is not None and ink.blue() > 150,
              f"result ignored the Blue pen ({ink and ink.name()})")
        check(failures, tag, dlg.signature_name() == "Jordan Carter",
              f"signature_name() = {dlg.signature_name()!r}")
    finally:
        dlg.deleteLater()
        pump(1)


# ==========================================================================
# T15: window placement via injected seams -- transparent over text
# ==========================================================================
def test_t15_window_signature_place(failures: list[str]) -> None:
    tag = "T15_window_signature_place"
    tmpdir = tempfile.mkdtemp(prefix="img_t15_")
    w = open_window(temp_copy(tmpdir, FORM_LIKE))
    try:
        v, doc = w.view, w.document
        scale = 2.0
        pristine = doc.render_with_edits(0, scale)
        word_ink0 = pix_region_ink(pristine, NOTES_WORD, scale)
        if not check(failures, tag, word_ink0 > 50,
                     f"'Notes:' word region carries no ink ({word_ink0}px)"):
            return

        png = strokes_to_png([SIG_DOT, SIG_SCRIBBLE], pen_width=3.0)
        lib = SignatureLibrary(os.path.join(tmpdir, "sigs"))
        w._signature_library = lib
        w._signature_dialog_factory = _StubSignatureDialog(png)

        # The seam flow: dialog (stub) -> library save -> armed placement.
        w._do_draw_signature()
        pump(2)
        check(failures, tag, v.current_mode() == "place_image",
              "_do_draw_signature did not arm placement")
        payload = v._image_place_payload or {}
        check(failures, tag, payload.get("kind") == "signature",
              f"payload kind {payload.get('kind')!r} != 'signature'")
        entries = lib.list()
        check(failures, tag,
              [n for n, _p in entries] == ["Jordan Carter"]
              and lib.load(entries[0][1]) == png,
              f"library save wrong: {entries}")

        # Drag-place over the Notes line: the rect spans x 40..360 from
        # y 380, so the word sits in the image's TRANSPARENT upper band
        # (the strokes live in the top-left dot + bottom-right scribble).
        idx0 = w.undo_stack.index()
        drag(v, v._scene_point(40, 380, page_index=0),
             v._scene_point(360, 480, page_index=0))
        check(failures, tag, w.undo_stack.index() == idx0 + 1,
              "drag placement did not land exactly one command")
        boxes = doc.image_boxes(0)
        if not check(failures, tag, len(boxes) == 1, "no staged signature"):
            return
        box = boxes[0]
        check(failures, tag, box.kind == "signature",
              f"staged kind {box.kind!r} != 'signature'")
        rect = box.rect
        check(failures, tag,
              rect[0] <= NOTES_WORD[0] and rect[2] >= NOTES_WORD[2]
              and rect[1] <= NOTES_WORD[1] and rect[3] >= NOTES_WORD[3],
              f"placed rect {rect} does not cover the word {NOTES_WORD}")

        # On screen: the word's ink survives under the transparent band.
        staged = doc.render_with_edits(0, scale)
        word_ink1 = pix_region_ink(staged, NOTES_WORD, scale)
        check(failures, tag,
              abs(word_ink1 - word_ink0) / max(word_ink0, 1) < 0.02,
              f"word ink changed under the transparent region "
              f"({word_ink0} -> {word_ink1})")
        # ... while the placed rect gained the stroke ink.
        rect_ink0 = pix_region_ink(pristine, rect, scale)
        rect_ink1 = pix_region_ink(staged, rect, scale)
        check(failures, tag, rect_ink1 > rect_ink0 + 300,
              f"placed rect gained no stroke ink "
              f"({rect_ink0} -> {rect_ink1})")

        # Saved file: smask intact, word ink intact, strokes present.
        out_path = os.path.join(tmpdir, "signed.pdf")
        doc.save_as(out_path)
        saved = fitz.open(out_path)
        try:
            infos = saved[0].get_image_info(xrefs=True)
            smask = (saved.extract_image(infos[0]["xref"])["smask"]
                     if infos and infos[0]["xref"] else 0)
            spix = saved[0].get_pixmap(matrix=fitz.Matrix(scale, scale),
                                       alpha=False)
        finally:
            saved.close()
        check(failures, tag, len(infos) == 1 and smask != 0,
              f"saved signature lost its SMask ({infos})")
        sword = pix_region_ink(spix, NOTES_WORD, scale)
        check(failures, tag,
              abs(sword - word_ink0) / max(word_ink0, 1) < 0.02,
              f"saved file lost the word under the signature "
              f"({word_ink0} -> {sword})")
        check(failures, tag,
              pix_region_ink(spix, rect, scale) > rect_ink0 + 300,
              "saved file shows no signature strokes in the placed rect")
    finally:
        close_window(w)
        shutil.rmtree(tmpdir, ignore_errors=True)


# ==========================================================================
# T16: procedural stamps -- PNG shape + place/save/undo through the window
# ==========================================================================
def test_t16_stamps(failures: list[str]) -> None:
    tag = "T16_stamps"
    png = stamp_png("APPROVED")
    img = QImage.fromData(png)
    check(failures, tag, png.startswith(b"\x89PNG") and not img.isNull(),
          "stamp_png is not a decodable PNG")
    check(failures, tag, img.hasAlphaChannel()
          and img.pixelColor(1, 1).alpha() == 0,
          "stamp background/corner not transparent")
    red = 0
    for y in range(0, img.height(), 3):
        for x in range(0, img.width(), 3):
            c = img.pixelColor(x, y)
            if c.alpha() > 200 and c.red() > 120 and c.green() < 90:
                red += 1
    check(failures, tag, red > 300,
          f"stamp carries almost no red ink ({red} samples)")
    # Width tracks the label; input is uppercased either way.
    widths = {kind: QImage.fromData(stamp_png(kind)).width()
              for kind in STAMP_KINDS}
    check(failures, tag,
          widths["VOID"] < widths["APPROVED"] < widths["CONFIDENTIAL"],
          f"stamp widths do not track the label: {widths}")
    check(failures, tag,
          QImage.fromData(stamp_png("approved")).width()
          == widths["APPROVED"],
          "lowercase input did not render the uppercase stamp")

    tmpdir = tempfile.mkdtemp(prefix="img_t16_")
    w = open_window(temp_copy(tmpdir, FORM_LIKE))
    try:
        v, doc = w.view, w.document
        w._do_place_stamp("APPROVED")
        pump(2)
        check(failures, tag, v.current_mode() == "place_image"
              and (v._image_place_payload or {}).get("kind") == "stamp",
              "_do_place_stamp did not arm a stamp placement")
        idx0 = w.undo_stack.index()
        click(v, v._scene_point(150, 480, page_index=0))
        check(failures, tag, w.undo_stack.index() == idx0 + 1,
              "stamp click did not land exactly one command")
        boxes = doc.image_boxes(0)
        if not check(failures, tag, len(boxes) == 1, "no staged stamp"):
            return
        check(failures, tag, boxes[0].kind == "stamp",
              f"staged kind {boxes[0].kind!r} != 'stamp'")
        rect = boxes[0].rect

        scale = 2.0
        on_screen = pix_region_red(doc.render_with_edits(0, scale),
                                   rect, scale)
        check(failures, tag, on_screen > 300,
              f"stamp renders almost no red on screen ({on_screen}px)")
        out_path = os.path.join(tmpdir, "stamped.pdf")
        doc.save_as(out_path)
        saved = fitz.open(out_path)
        try:
            infos = saved[0].get_image_info(xrefs=True)
            smask = (saved.extract_image(infos[0]["xref"])["smask"]
                     if infos and infos[0]["xref"] else 0)
            spix = saved[0].get_pixmap(matrix=fitz.Matrix(scale, scale),
                                       alpha=False)
        finally:
            saved.close()
        check(failures, tag, len(infos) == 1 and smask != 0,
              f"saved stamp lost its SMask ({infos})")
        in_file = pix_region_red(spix, rect, scale)
        check(failures, tag, in_file > 300,
              f"saved file shows almost no stamp ink ({in_file}px)")
        check(failures, tag,
              abs(on_screen - in_file) / max(in_file, 1) < 0.02,
              f"stamp WYSIWYG drift: {on_screen} vs {in_file}")

        w.undo_stack.undo()
        pump(1)
        check(failures, tag, not doc.image_boxes(0) and doc.edit_count == 0,
              "one undo did not unstage the stamp")
    finally:
        close_window(w)
        shutil.rmtree(tmpdir, ignore_errors=True)


# ==========================================================================
# T17: M2 chrome -- submenus, library re-list, toolbar button, seams
# ==========================================================================
def test_t17_m2_chrome(failures: list[str]) -> None:
    tag = "T17_m2_chrome"
    tmpdir = tempfile.mkdtemp(prefix="img_t17_")
    w = open_window(None)                 # NO document: the empty state
    try:
        # Determinism + privacy: swap in a tempdir library BEFORE asserting
        # menu contents so the real machine's folder never matters.
        lib = SignatureLibrary(os.path.join(tmpdir, "sigs"))
        w._signature_library = lib
        w.menu_signature.aboutToShow.emit()      # re-list = the menu hook
        pump(1)

        check(failures, tag,
              w.act_draw_signature.shortcut() == QKeySequence("Ctrl+Shift+G"),
              f"Draw Signature shortcut "
              f"{w.act_draw_signature.shortcut().toString()!r}")
        check(failures, tag, not w.act_draw_signature.isEnabled()
              and not w.act_signature_from_file.isEnabled()
              and not any(a.isEnabled() for a in w._stamp_actions),
              "signature/stamp placement enabled with no document")
        check(failures, tag, w.act_manage_signatures.isEnabled(),
              "Manage Signatures disabled (it only opens a folder)")
        # Charcoal redesign: Signature is a rail ACTION button, not a top-toolbar
        # button; it is disabled with no document.
        check(failures, tag,
              not w.left_panel._action_buttons["signature"].isEnabled(),
              "rail Signature action enabled in the empty state")

        # Both submenus sit BEFORE the tools_objects anchor.
        acts = w.menu_tools.actions()
        anchor_i = acts.index(w.menu_anchors["tools_objects"])
        before = [a.text() for a in acts[:anchor_i]]
        check(failures, tag,
              "Signature" in before and "Stamp" in before
              and before.index("Image from File…")
              < before.index("Signature") < before.index("Stamp"),
              f"objects entries out of order at the anchor: {before}")

        # Empty library: the static entries only.
        sig_texts = [a.text() for a in w.menu_signature.actions()
                     if a.text()]
        check(failures, tag,
              sig_texts == ["Draw Signature…", "Signature from File…",
                            "Manage Signatures…"],
              f"empty-library Signature menu: {sig_texts}")
        stamp_texts = [a.text() for a in w.menu_stamp.actions()]
        check(failures, tag,
              stamp_texts == ["Approved", "Draft", "Confidential",
                              "Completed", "Void"],
              f"Stamp menu: {stamp_texts}")

        # The toolbar button: popup = the SAME menu, click = Draw.
        check(failures, tag,
              w.signature_button.menu() is w.menu_signature
              and w.signature_button.defaultAction() is w.act_draw_signature
              and w.signature_button.popupMode()
              == QToolButton.MenuButtonPopup,
              "Signature toolbar button not the Draw + popup menu-button")
        # The dialog seam defaults to the real class.
        check(failures, tag, w._signature_dialog_factory is SignatureDialog,
              "_signature_dialog_factory does not default to SignatureDialog")

        # A saved signature appears (newest first, thumbnail icon) on the
        # next menu open, enabled once a document is up.
        lib.save("Riley Morgan", strokes_to_png([SIG_SCRIBBLE]))
        w.open_path(temp_copy(tmpdir, FORM_LIKE))
        pump(6)
        check(failures, tag, w.act_draw_signature.isEnabled()
              and w.act_signature_from_file.isEnabled()
              and all(a.isEnabled() for a in w._stamp_actions),
              "signature/stamp actions still disabled after open")
        check(failures, tag,
              w.left_panel._action_buttons["signature"].isEnabled(),
              "rail Signature action still disabled after open")
        w.menu_signature.aboutToShow.emit()
        pump(1)
        entries = w.menu_signature.actions()
        check(failures, tag,
              entries and entries[0].text() == "Riley Morgan"
              and not entries[0].icon().isNull()
              and entries[0].isEnabled(),
              "library entry missing from the re-listed Signature menu")
        check(failures, tag,
              [a.text() for a in entries if a.text()]
              == ["Riley Morgan", "Draw Signature…", "Signature from File…",
                  "Manage Signatures…"],
              f"re-listed Signature menu wrong: "
              f"{[a.text() for a in entries if a.text()]}")

        # The from-file seam arms a SIGNATURE placement (kind survives).
        png_path = os.path.join(tmpdir, "sig_from_file.png")
        with open(png_path, "wb") as fh:
            fh.write(strokes_to_png([SIG_SCRIBBLE]))
        w._do_signature_from_file(path=png_path)
        pump(1)
        check(failures, tag, w.view.current_mode() == "place_image"
              and (w.view._image_place_payload or {}).get("kind")
              == "signature",
              "_do_signature_from_file did not arm a signature placement")
        w.view.exit_place_image_mode()
    finally:
        close_window(w)
        shutil.rmtree(tmpdir, ignore_errors=True)


# ==========================================================================
# T18: existing_images detection -- memo, identity, one staged delete
# ==========================================================================
def test_t18_existing_images(failures: list[str]) -> None:
    tag = "T18_existing_images"
    tmpdir = tempfile.mkdtemp(prefix="img_t18_")
    doc = PDFDocument(temp_copy(tmpdir, IMAGE_DOC))
    try:
        xims0 = doc.existing_images(0)
        xims1 = doc.existing_images(1)
        if not check(failures, tag, len(xims0) == 1 and len(xims1) == 1,
                     f"occurrences per page {(len(xims0), len(xims1))} "
                     "!= (1, 1)"):
            return
        x0, x1 = xims0[0], xims1[0]
        check(failures, tag, rect_close(x0.rect, PAGE0_LOGO_RECT),
              f"page-0 rect {x0.rect} != authored {PAGE0_LOGO_RECT}")
        check(failures, tag, rect_close(x1.rect, PAGE1_LOGO_RECT),
              f"page-1 rect {x1.rect} != authored {PAGE1_LOGO_RECT}")
        check(failures, tag, x0.xref > 0 and x0.xref == x1.xref,
              f"logo xref not shared: {x0.xref} vs {x1.xref}")
        check(failures, tag,
              x0.identity == (0, "xim", x0.xref, 0)
              and x0.edit_key == x0.identity and x0.bbox == x0.rect,
              f"identity/duck-typing wrong: {x0.identity}")
        # Memoized per page; _invalidate_caches refreshes the memo.
        check(failures, tag, doc.existing_images(0) is xims0,
              "existing_images(0) not memoized (new list per call)")
        doc._invalidate_caches()
        fresh = doc.existing_images(0)
        check(failures, tag, fresh is not xims0 and fresh == xims0,
              "memo did not refresh (same object) or content drifted")

        # ONE staged delete: census + page-scoped cache key.
        scale = 1.5
        pristine = doc.render_with_edits(0, scale)
        sig0 = doc.render_signature(0)
        sig1 = doc.render_signature(1)
        doc.delete_existing_image(0, fresh[0])
        check(failures, tag, doc.edit_count == 1 and doc.dirty
              and doc.has_edits and len(doc.xim_deletes(0)) == 1,
              f"staged delete census wrong (count {doc.edit_count})")
        check(failures, tag, doc.render_signature(0) != sig0,
              "render_signature(0) did not miss after the staged delete")
        check(failures, tag, doc.render_signature(1) == sig1,
              "render_signature(1) changed for a page-0 delete")
        # Re-deleting the SAME occurrence is a no-op (no second command).
        doc.delete_existing_image(0, fresh[0])
        check(failures, tag, doc.edit_count == 1 and len(doc._undo) == 1,
              "re-deleting the same occurrence pushed a second command")
        # Undo -> byte-identical pristine pixels; redo restores.
        doc.undo()
        check(failures, tag, doc.edit_count == 0 and not doc.dirty
              and not doc.xim_deletes(0),
              "undo did not unstage the delete")
        after = doc.render_with_edits(0, scale)
        check(failures, tag, after.samples == pristine.samples,
              "undo did not return byte-identical pristine pixels")
        doc.redo()
        check(failures, tag, doc.edit_count == 1
              and len(doc.xim_deletes(0)) == 1,
              "redo did not restore the staged delete")
    finally:
        doc.close()
        shutil.rmtree(tmpdir, ignore_errors=True)


# ==========================================================================
# T19: page-scoped delete -- text + other-page occurrence survive
# ==========================================================================
def test_t19_scoped_delete(failures: list[str]) -> None:
    tag = "T19_scoped_delete"
    tmpdir = tempfile.mkdtemp(prefix="img_t19_")
    doc = PDFDocument(temp_copy(tmpdir, IMAGE_DOC))
    try:
        scale = 1.5
        ink0 = pix_region_ink(doc.render(0, scale), PAGE0_LOGO_RECT, scale)
        ink1 = pix_region_ink(doc.render(1, scale), PAGE1_LOGO_RECT, scale)
        if not check(failures, tag, ink0 > 2000 and ink1 > 1000,
                     f"pristine logo ink too small ({ink0}, {ink1})"):
            return
        doc.delete_existing_image(0, doc.existing_images(0)[0])

        # Screen: the page-0 occurrence is gone, page 1 untouched.
        gone = pix_region_ink(doc.render_with_edits(0, scale),
                              PAGE0_LOGO_RECT, scale)
        check(failures, tag, gone < 0.02 * ink0,
              f"staged delete left {gone} ink px on screen (was {ink0})")
        keep = pix_region_ink(doc.render_with_edits(1, scale),
                              PAGE1_LOGO_RECT, scale)
        check(failures, tag, keep == ink1,
              f"page-1 occurrence changed on screen ({keep} != {ink1})")

        # Saved file: no page-0 image, text intact, page-1 intact, parity.
        out_path = os.path.join(tmpdir, "deleted.pdf")
        doc.save_as(out_path)
        saved = fitz.open(out_path)
        try:
            check(failures, tag, not saved[0].get_image_info(xrefs=True),
                  "saved page 0 still carries an image occurrence")
            check(failures, tag,
                  len(saved[1].get_image_info(xrefs=True)) == 1,
                  "the same xref's page-1 occurrence did not survive")
            text = saved[0].get_text()
            check(failures, tag, "Jordan Carter" in text
                  and "certificate template" in text,
                  "page-0 text lost by the image-REMOVE redaction")
            spix = saved[0].get_pixmap(matrix=fitz.Matrix(scale, scale),
                                       alpha=False)
            skeep = pix_region_ink(
                saved[1].get_pixmap(matrix=fitz.Matrix(scale, scale),
                                    alpha=False), PAGE1_LOGO_RECT, scale)
        finally:
            saved.close()
        check(failures, tag, skeep == ink1,
              f"saved page-1 occurrence ink {skeep} != pristine {ink1}")
        rink = pix_ink(doc.render_with_edits(0, scale))
        sink = pix_ink(spix)
        rel = abs(rink - sink) / max(sink, 1)
        check(failures, tag, rel < 0.02,
              f"screen vs saved rel diff {rel:.4f} >= 0.02 "
              f"({rink} vs {sink})")

        # A structural op bakes the staged delete into working.
        doc.rotate_page(0, 90)
        check(failures, tag, not doc._xim_deletes and doc.edit_count == 0,
              "structural bake did not clear the staged xim delete")
        check(failures, tag, not doc.working[0].get_image_info(xrefs=True),
              "working[0] still carries the occurrence after the bake")
        check(failures, tag,
              len(doc.working[1].get_image_info(xrefs=True)) == 1,
              "working[1] lost its occurrence in the structural bake")
    finally:
        doc.close()
        shutil.rmtree(tmpdir, ignore_errors=True)


# ==========================================================================
# T20: move-as-macro -- extract + delete + reinsert, ONE UI undo step
# ==========================================================================
def test_t20_move_macro(failures: list[str]) -> None:
    tag = "T20_move_macro"
    tmpdir = tempfile.mkdtemp(prefix="img_t20_")
    w = open_window(temp_copy(tmpdir, IMAGE_DOC))
    try:
        v, doc = w.view, w.document
        scale = 1.5
        xim = doc.existing_images(0)[0]

        # extract_image_bytes recombines the SMask: alpha-carrying PNG at
        # the source pixel size, valid for add_image.
        data, natural = doc.extract_image_bytes(xim.xref)
        check(failures, tag, data[:8] == b"\x89PNG\r\n\x1a\n",
              "extracted bytes are not PNG")
        check(failures, tag, tuple(natural) == LOGO_PX,
              f"extracted natural size {natural} != source {LOGO_PX}")
        qimg = QImage.fromData(data)
        check(failures, tag, qimg.hasAlphaChannel(),
              "SMask not recombined (no alpha channel)")

        dx, dy = -80.0, 250.0
        new_rect = (xim.rect[0] + dx, xim.rect[1] + dy,
                    xim.rect[2] + dx, xim.rect[3] + dy)
        idx0 = w.undo_stack.index()
        w._on_box_command_requested("xim_move", xim, {"dx": dx, "dy": dy})
        pump(2)
        check(failures, tag, w.undo_stack.index() == idx0 + 1,
              f"the move macro took {w.undo_stack.index() - idx0} UI steps, "
              "not 1")
        # Both halves staged: the deletion + the kind="moved" ImageBox.
        check(failures, tag, len(doc.xim_deletes(0)) == 1
              and doc.edit_count == 2,
              f"macro staged census wrong (count {doc.edit_count})")
        boxes = doc.image_boxes(0)
        if not check(failures, tag, len(boxes) == 1
                     and boxes[0].kind == "moved",
                     f"reinserted ImageBox missing/wrong: {boxes}"):
            return
        check(failures, tag, rect_close(boxes[0].rect, new_rect, 0.01),
              f"reinserted rect {boxes[0].rect} != offset rect {new_rect}")
        sel = v.current_selection()
        check(failures, tag, sel is not None
              and sel.identity == boxes[0].identity,
              "the reinserted ImageBox did not take the selection")

        # Save: ink at the new rect with the smask intact, none at the old,
        # text + page 1 untouched, screen == file.
        out_path = os.path.join(tmpdir, "moved.pdf")
        doc.save_as(out_path)
        saved = fitz.open(out_path)
        try:
            infos = saved[0].get_image_info(xrefs=True)
            check(failures, tag, len(infos) == 1
                  and rect_close(tuple(infos[0]["bbox"]), new_rect),
                  f"saved occurrence not at the moved rect: {infos}")
            check(failures, tag,
                  saved.extract_image(infos[0]["xref"])["smask"] != 0,
                  "transparency lost through the move (no SMask)")
            spix = saved[0].get_pixmap(matrix=fitz.Matrix(scale, scale),
                                       alpha=False)
            check(failures, tag, "Jordan Carter" in saved[0].get_text(),
                  "page-0 text lost through the move macro")
            check(failures, tag,
                  len(saved[1].get_image_info(xrefs=True)) == 1,
                  "page-1 occurrence lost through the page-0 move")
        finally:
            saved.close()
        old_ink = pix_region_ink(spix, PAGE0_LOGO_RECT, scale)
        new_ink = pix_region_ink(spix, new_rect, scale)
        check(failures, tag, new_ink > 2000 and old_ink < 0.02 * new_ink,
              f"moved ink wrong (old rect {old_ink}, new rect {new_ink})")
        rink = pix_ink(doc.render_with_edits(0, scale))
        sink = pix_ink(spix)
        rel = abs(rink - sink) / max(sink, 1)
        check(failures, tag, rel < 0.02,
              f"screen vs saved rel diff {rel:.4f} >= 0.02")

        # ONE undo restores BOTH halves; one redo re-applies both.
        w.undo_stack.undo()
        pump(2)
        check(failures, tag, not doc.xim_deletes(0)
              and not doc.image_boxes(0) and doc.edit_count == 0,
              "one undo did not restore both macro halves")
        ink_back = pix_region_ink(doc.render_with_edits(0, scale),
                                  PAGE0_LOGO_RECT, scale)
        check(failures, tag, ink_back > 2000,
              "the occurrence's ink did not come back after the undo")
        w.undo_stack.redo()
        pump(2)
        check(failures, tag, len(doc.xim_deletes(0)) == 1
              and len(doc.image_boxes(0)) == 1,
              "one redo did not re-apply both macro halves")
    finally:
        close_window(w)
        shutil.rmtree(tmpdir, ignore_errors=True)


# ==========================================================================
# T21: UI -- xim hotspot, no-handle overlay, drag macro, Delete key
# ==========================================================================
def test_t21_ui_existing_image(failures: list[str]) -> None:
    tag = "T21_ui_existing_image"
    tmpdir = tempfile.mkdtemp(prefix="img_t21_")
    w = open_window(temp_copy(tmpdir, IMAGE_DOC))
    try:
        v, doc = w.view, w.document
        xim = doc.existing_images(0)[0]

        def xim_hotspots():
            return [h for h in v._layers[0].hotspots
                    if getattr(h.box, "identity", (None,) * 4)[1] == "xim"]

        spots = xim_hotspots()
        if not check(failures, tag, len(spots) == 1,
                     f"{len(spots)} xim hotspots materialized, expected 1"):
            return
        check(failures, tag, type(spots[0]).__name__ == "ImageHotspot",
              f"xim hotspot is a {type(spots[0]).__name__}")
        expected = v._image_scene_rect(xim)
        check(failures, tag,
              (spots[0].rect().topLeft() - expected.topLeft()).manhattanLength()
              < 2.0,
              "xim hotspot rect does not sit on the occurrence rect")

        # Click selects: image overlay WITHOUT handles + the status chip.
        click(v, expected.center())
        sel = v.current_selection()
        if not check(failures, tag, sel is not None
                     and sel.identity == xim.identity,
                     "click did not select the existing image"):
            return
        check(failures, tag,
              type(v._overlay).__name__ == "_ImageSelectionOverlay"
              and v._overlay._handles == [],
              "xim overlay wrong (must be the image overlay, NO handles)")
        check(failures, tag, "Page image" in w.font_chip.text()
              and "120 × 80 pt" in w.font_chip.text(),
              f"status chip wrong for a page image: {w.font_chip.text()!r}")

        # Body drag fires the macro; the moved ImageBox takes the selection.
        idx0 = w.undo_stack.index()
        z = v._zoom
        drag(v, expected.center(), QPointF(expected.center().x() - 30,
                                           expected.center().y() + 90))
        check(failures, tag, w.undo_stack.index() == idx0 + 1,
              f"drag-move took {w.undo_stack.index() - idx0} UI steps, not 1")
        boxes = doc.image_boxes(0)
        if not check(failures, tag, len(boxes) == 1
                     and boxes[0].kind == "moved"
                     and len(doc.xim_deletes(0)) == 1,
                     "drag did not stage the delete + reinsert pair"):
            return
        check(failures, tag,
              abs(boxes[0].rect[0] - (xim.rect[0] - 30 / z)) < 0.75
              and abs(boxes[0].rect[1] - (xim.rect[1] + 90 / z)) < 0.75,
              f"moved rect {boxes[0].rect} != start + scene delta / zoom")
        sel = v.current_selection()
        check(failures, tag, sel is not None
              and sel.identity == boxes[0].identity,
              "selection did not hand over to the reinserted ImageBox")
        check(failures, tag, not xim_hotspots(),
              "the staged-deleted occurrence kept its hotspot")
        img_spots = [h for h in v._layers[0].hotspots
                     if getattr(h.box, "identity", (None,) * 3)[1] == "img"]
        check(failures, tag, len(img_spots) == 1,
              "the reinserted ImageBox has no hotspot")

        # ONE undo: both halves back, the xim hotspot re-materializes.
        w.undo_stack.undo()
        pump(2)
        check(failures, tag, doc.edit_count == 0 and len(xim_hotspots()) == 1,
              "undo did not restore the occurrence's hotspot")

        # Delete key on the selected occurrence = ONE xim_delete.
        click(v, expected.center())
        sel = v.current_selection()
        if not check(failures, tag, sel is not None
                     and sel.identity == xim.identity,
                     "re-click did not select the existing image"):
            return
        idx1 = w.undo_stack.index()
        _APP.sendEvent(v, QKeyEvent(QKeyEvent.Type.KeyPress, Qt.Key_Delete,
                                    Qt.NoModifier))
        pump(2)
        check(failures, tag, w.undo_stack.index() == idx1 + 1
              and len(doc.xim_deletes(0)) == 1
              and not doc.image_boxes(0),
              "Delete key did not stage exactly one xim_delete")
        check(failures, tag, v.current_selection() is None
              and not xim_hotspots(),
              "selection/hotspot survived the staged deletion")
        w.undo_stack.undo()
        pump(2)
        check(failures, tag, doc.edit_count == 0 and len(xim_hotspots()) == 1,
              "undo(delete) did not bring the occurrence back")
    finally:
        close_window(w)
        shutil.rmtree(tmpdir, ignore_errors=True)


# ==========================================================================
TESTS = [
    test_t1_model_staging,
    test_t2_save_roundtrip,
    test_t3_undo_redo,
    test_t4_move_resize_delete,
    test_t5_text_coexistence,
    test_t6_rotate_bakes,
    test_t7_rotated_placement,
    test_t8_cache_miss,
    test_t9_ui_placement,
    test_t10_ui_move_resize,
    test_t11_chrome,
    test_t12_strokes_to_png,
    test_t13_library_crud,
    test_t14_dialog_drive,
    test_t15_window_signature_place,
    test_t16_stamps,
    test_t17_m2_chrome,
    test_t18_existing_images,
    test_t19_scoped_delete,
    test_t20_move_macro,
    test_t21_ui_existing_image,
]


def main() -> int:
    print("Images & signatures suite (M1: ImageBox end-to-end; "
          "M2: signatures + stamps; M3: existing images; offscreen)\n")
    failures: list[str] = []
    for test in TESTS:
        name = test.__name__
        before = len(failures)
        try:
            test(failures)
        except Exception:
            failures.append(f"{name}: raised:\n{traceback.format_exc()}")
        status = "ok" if len(failures) == before else "FAIL"
        print(f"  {name:32} {status}")

    print()
    if failures:
        print(f"FAILED ({len(failures)} assertion failure(s)):")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("PASSED -- placed images stage/move/resize/delete through one "
          "shared pipeline (screen == saved file), survive structural "
          "bakes, render upright on rotated pages, key the pixmap cache, "
          "and the UI places/moves/resizes/deletes in single undo steps; "
          "drawn signatures export as cropped transparent PNGs, persist in "
          "the injectable library, place over text without erasing it, and "
          "the procedural stamps place/save/undo the same way; existing "
          "page images detect at their authored rects, delete page-scoped "
          "(text + shared-xref siblings survive), and move as one-undo "
          "macros with transparency intact.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
