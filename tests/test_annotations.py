"""Annotations & markup suite (ws3_annotations_markup). M1: annotation model
core + text markup tools. M2: sticky notes + non-modal popup, canvas annot
selection/move/delete, existing-file-annot management via xref overrides.

Asserts (spec ws3 §7 M1 test plan):

  T1: MODEL roundtrip on annot_target.pdf: ``add_annot`` with quads taken
      from ``page_words`` raises ``render_with_edits`` ink in the word's
      region; ``save_as`` -> fitz reopen shows the right annot TYPE, stroke
      COLOR, and quad/vertex count for ALL FOUR markup kinds (highlight /
      underline / strikeout / squiggly); ``document.undo`` removes each from
      the render (ink returns to the pristine count) AND from a fresh save
      (zero annots in the output).
  T2: UI path: arm the Highlight tool (toolbar action), synthesize a real
      press/drag/release over known body text (the tests/test_reflow.py
      event pattern; the press passes THROUGH the box hotspot while the tool
      is armed) -> exactly ONE command lands on ``window.undo_stack``, one
      staged highlight with line-grouped quads exists, the tool STAYS armed,
      and ``window.undo_stack.undo()`` clears the staged map. A drag over
      empty canvas emits NO command and posts the "No text under selection"
      status message. Esc disarms back to select.
  T3: cache keying (§9): ``render_signature`` changes after ``add_annot``
      (and reverts on undo), and the view's pixmap cache MISSES for the new
      signature while the materialized page had a cached entry for the old
      one -- the one-registry contract, no side-channel signature API.
  T4: ``rotate_page`` BAKES the staged highlight into ``working``
      (``working[0].annots()`` non-empty, staged maps empty, annot command
      entries filtered from the model history).
  T5: WYSIWYG: one staged TEXT edit + one staged highlight on the same page;
      ``render_with_edits`` ink vs the saved file's ink, rel diff < 0.02
      (the tests/test_editor.py harness metric -- both sides run the one
      shared pipeline with ``_apply_page_annots`` LAST).
  T6: rotated_doc.pdf (/Rotate 90): a highlight staged over a word lands ON
      the word -- the changed-pixel bbox of pristine-vs-marked renders
      intersects the rotation-matrix-mapped word rect, and the saved file
      carries the highlight with text-space vertices matching the quad.
  T7: chrome: checkable Highlight/Underline/Strikethrough/Squiggly actions
      with the advertised bare-key shortcuts (H/U/K; squiggly menu-only) at
      the Tools menu's ``tools_annotate`` anchor; the toolbar Markup buttons
      hide with no document and show after open (empty-state hiding);
      markup toggles are mutually exclusive with Add Text and each other.

M2 additions (spec ws3 §7 M2 test plan):

  T8: STICKY NOTE UI flow: arm the Note tool, click the canvas -> exactly
      ONE 'add' command (18x18pt anchor, the window's (1,.8,0) default,
      empty contents) and the non-modal NotePopup opens (objectName
      ``NotePopup``, ``window._note_popup``); setPlainText + Done -> ONE
      'contents' command, popup closed, tool still armed; a no-change
      commit emits NO command; the saved file carries the Text annot with
      the typed content at the staged anchor; window undo x2 clears the
      staged map; Esc disarms. Chrome: Sticky Note (N) + Delete Annotation
      sit in the Tools menu; Delete Annotation needs a selection.
  T9: CANVAS selection / move / delete: an AnnotHotspot per record rides
      ``layer.extra_items`` (the M4b factory); select by identity AND by
      hotspot click (dashed overlay, mutual exclusion with box selection);
      body drag = ONE 'move' command with the PDF-point delta, selection
      re-bound by identity after the repaint, undo restores the rect;
      Del = ONE 'delete' command, selection cleared, undo restores; a plain
      click over markup under text selects the BOX (text wins; z 10 vs 5)
      while Alt+click forces the annot.
  T10: EXISTING-annot management (model): ``annotations(1)`` enumerates the
      fixture's pre-existing highlight + note as is_existing records with
      xref identities; delete -> deleted override, region ink drops, the
      saved file lacks the annot, undo restores all of it; note moves fold
      CUMULATIVELY into one override and shift the saved rect; existing
      markup refuses 'move' and any non-contents modify; set_annot_contents
      changes the saved info and undoes; render_signature folds override
      changes (§9 cache contract).
  T11: in-place-edit guard: a staged TEXT edit on the page next to the
      existing highlight saves with the text changed AND the annot intact
      (type + vertices) AND no residue: the edited word's region ink in the
      saved file matches the render_with_edits pipeline (the
      tests/test_font_fidelity.py region_ink pattern) and whole-page ink
      stays WYSIWYG (< 0.02).

M3 additions (spec ws3 §7 M3 test plan):

  T12: MODEL ink + shapes roundtrip: each drawn kind (ink / rect / ellipse
      / line / arrow) model-added then saved -> the reopened annot carries
      the right TYPE, stroke color, border WIDTH, fill (Square), opacity,
      and the arrow's ``line_ends[1] == PDF_ANNOT_LE_OPEN_ARROW``; staged
      ink raises rendered ink; the undo chain returns the page render to
      its pristine ink count and a fresh save to zero annots; geometry
      validation raises for missing points/rect/endpoints.
  T13: UI gestures: an ink press/move/release over the canvas = exactly ONE
      command (one stroke, decimated points, live QGraphicsPathItem preview
      while dragging, tool stays armed); a stationary click commits nothing;
      a rect drag = ONE command whose rect matches the dragged corners; a
      < 3pt diagonal discards; the arrow drag keeps the drag DIRECTION and
      shows the arrowhead preview; Esc aborts a live preview first, then
      disarms. Chrome: Draw Ink (D) + the Shapes submenu sit at the
      tools_annotate anchor; the Shapes toolbar button's default action
      follows the last-used kind; ink/shapes hide with no document.
  T14: INSPECTOR style section: selecting a staged shape shows the
      ANNOTATION section (text sections hidden) seeded from the spec; ONE
      control change = ONE undo step (width spin, stroke emission, No-fill
      toggle, opacity spin) and the saved file carries the new style; a
      PRE-EXISTING file annot grays the section ("Saved annotation" hint,
      emissions ignored); with a shape tool armed and nothing selected the
      same controls edit the per-tool session defaults with NO undo
      entries, and the next add uses them; Esc returns the panel to its
      empty state.

M4 additions (spec ws3 §7 M4 test plan):

  T15: COMMENTS panel: the left column's third stacked page (index 2, the
      conflict-ledger slot) with a Comments tool-strip button and a
      checkable View > Show Comments (Cmd+Shift+C) toggle at the
      ``view_panels`` anchor (disabled with no document). Opening
      annot_target.pdf lists the two pre-existing page-2 file annots
      ("(file)"-tagged, "p.2", contents snippet); staging two page-1
      annots AUTO-refreshes the list (the AnnotCommand on_change hook, no
      manual refresh) with rows sorted by (page, y). Clicking a page-2 row
      jumps (``window.view_page_index() == 1``) and selects the annot by
      identity. The panel's Delete button lands exactly ONE undoable
      command and refreshes; window undo restores the row count. Edit Note
      opens the §5.3 popup seeded with the row's contents; a commit lands
      ONE 'contents' command and the row text updates. A structural rotate
      bakes staged annots ("(file)" on every row, staged maps empty) and a
      tab switch re-lists the new active document; the toggle and the
      strip stay in lockstep both ways.

Fixtures: NEW synthetic annot_target.pdf (build_annot_fixture.py; fake
neutral names only) + existing rotated_doc.pdf. Never writes into
tests/fixtures/; temp output via tempfile.mkdtemp.

Run:
    QT_QPA_PLATFORM=offscreen \
      /Users/edward/Documents/GitHub/PDFTextEditor/.venv/bin/python \
      tests/test_annotations.py
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
from PySide6.QtCore import QEvent, QPointF, Qt  # noqa: E402
from PySide6.QtGui import QKeyEvent, QKeySequence, QMouseEvent  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

# A QApplication must exist before any QFont/QWidget is constructed.
_APP = QApplication.instance() or QApplication([])

from pdftexteditor.document import PDFDocument  # noqa: E402
from pdftexteditor.ui.main_window import AnnotCommand, MainWindow  # noqa: E402

FIXTURE_DIR = os.path.join(_HERE, "fixtures")
ANNOT_TARGET = os.path.join(FIXTURE_DIR, "annot_target.pdf")
ROTATED = os.path.join(FIXTURE_DIR, "rotated_doc.pdf")

# Per-kind defaults (the window's session defaults, spec §3.1).
KIND_STROKE = {
    "highlight": (1.0, 0.85, 0.0),
    "underline": (0.13, 0.55, 0.13),
    "strikeout": (0.8, 0.0, 0.0),
    "squiggly": (0.8, 0.0, 0.0),
}
KIND_TYPE = {
    "highlight": fitz.PDF_ANNOT_HIGHLIGHT,
    "underline": fitz.PDF_ANNOT_UNDERLINE,
    "strikeout": fitz.PDF_ANNOT_STRIKE_OUT,
    "squiggly": fitz.PDF_ANNOT_SQUIGGLY,
}


def check(failures: list[str], tag: str, cond: bool, msg: str) -> bool:
    if not cond:
        failures.append(f"{tag}: {msg}")
    return cond


def pump(n: int = 4) -> None:
    for _ in range(n):
        _APP.processEvents()


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


def click(view, scene_pt: QPointF, modifiers=Qt.NoModifier) -> None:
    """One full press/release click at a scene point."""
    send_mouse(view, scene_pt, QEvent.MouseButtonPress, modifiers=modifiers)
    send_mouse(view, scene_pt, QEvent.MouseButtonRelease,
               buttons=Qt.NoButton, modifiers=modifiers)
    pump(3)


def drag(view, p0: QPointF, p1: QPointF) -> None:
    """A full press / move / release gesture across two scene points."""
    send_mouse(view, p0, QEvent.MouseButtonPress)
    mid = QPointF((p0.x() + p1.x()) / 2, (p0.y() + p1.y()) / 2)
    send_mouse(view, mid, QEvent.MouseMove)
    send_mouse(view, p1, QEvent.MouseMove)
    send_mouse(view, p1, QEvent.MouseButtonRelease, buttons=Qt.NoButton)
    pump(3)


def word_named(words, text: str):
    for wb in words:
        if wb.text == text:
            return wb
    return None


def click_list_item(lst, item) -> None:
    """One real press/release click on a QListWidget row (drives
    itemClicked exactly as a user click would)."""
    rect = lst.visualItemRect(item)
    pos = QPointF(rect.center())
    gp = lst.viewport().mapToGlobal(rect.center())
    _APP.sendEvent(lst.viewport(), QMouseEvent(
        QEvent.MouseButtonPress, pos, QPointF(gp),
        Qt.LeftButton, Qt.LeftButton, Qt.NoModifier))
    _APP.sendEvent(lst.viewport(), QMouseEvent(
        QEvent.MouseButtonRelease, pos, QPointF(gp),
        Qt.LeftButton, Qt.NoButton, Qt.NoModifier))
    pump(3)


def pix_region_ink(pix, bbox: tuple, scale: float) -> int:
    """Non-white pixels (Rec. 601 luma < 230) inside a TEXT-SPACE bbox of an
    UNROTATED page's pixmap rendered at ``scale``. Colored markup ink (e.g.
    the yellow highlight, luma ~204) counts as ink."""
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


def changed_bbox(pix_a, pix_b) -> tuple | None:
    """Pixel-space bbox of every differing pixel between two equal-size
    pixmaps (row-compare fast path), or None when identical."""
    sa, sb = pix_a.samples, pix_b.samples
    n = pix_a.n
    stride = pix_a.width * n
    min_x = min_y = None
    max_x = max_y = None
    for y in range(pix_a.height):
        row_a = sa[y * stride:(y + 1) * stride]
        if row_a == sb[y * stride:(y + 1) * stride]:
            continue
        row_b = sb[y * stride:(y + 1) * stride]
        for x in range(pix_a.width):
            i = x * n
            if row_a[i:i + n] != row_b[i:i + n]:
                if min_x is None or x < min_x:
                    min_x = x
                if max_x is None or x > max_x:
                    max_x = x
                if min_y is None:
                    min_y = y
                max_y = y
    if min_x is None:
        return None
    return (min_x, min_y, max_x + 1, max_y + 1)


def saved_annots(doc: PDFDocument, tmpdir: str, name: str) -> list[dict]:
    """save_as -> reopen -> a plain-data summary of page annots (read while
    the page is alive; PyMuPDF annots unbind once the page dies)."""
    out_path = os.path.join(tmpdir, name)
    doc.save_as(out_path)
    saved = fitz.open(out_path)
    result = []
    for page in saved:
        for a in (page.annots() or ()):
            result.append({
                "page": page.number,
                "type": a.type[0],
                "stroke": tuple(a.colors.get("stroke") or ()),
                "fill": tuple(a.colors.get("fill") or ()),
                "width": (a.border or {}).get("width"),
                "opacity": a.opacity,
                "line_ends": a.line_ends,
                "vertices": [tuple(v) if not isinstance(v, list) else v
                             for v in (a.vertices or [])],
                "content": a.info.get("content", ""),
                "rect": tuple(a.rect),
            })
    saved.close()
    return result


# ==========================================================================
# T1: model roundtrip -- ink raised, all four kinds saved, undo restores
# ==========================================================================
def test_t1_model_roundtrip(failures: list[str]) -> None:
    tag = "T1_model_roundtrip"
    tmpdir = tempfile.mkdtemp(prefix="annot_t1_")
    doc = PDFDocument(ANNOT_TARGET)
    try:
        words = doc.page_words(0)
        targets = {
            "highlight": word_named(words, "quarterly"),
            "underline": word_named(words, "onboarding"),
            "strikeout": word_named(words, "certificate"),
            "squiggly": word_named(words, "proofreading"),
        }
        if not check(failures, tag, all(targets.values()),
                     f"fixture words missing: "
                     f"{[k for k, v in targets.items() if v is None]}"):
            return

        scale = 2.0
        hl_bbox = targets["highlight"].bbox
        pristine_pix = doc.render_with_edits(0, scale)
        pristine_region = pix_region_ink(pristine_pix, hl_bbox, scale)

        for kind, wb in targets.items():
            doc.add_annot(0, kind=kind, quads=(tuple(wb.bbox),),
                          stroke=KIND_STROKE[kind])
        check(failures, tag, len(doc._annots) == 4 and doc.edit_count == 4,
              f"staged census {len(doc._annots)} / count {doc.edit_count}")

        # The highlight raises rendered ink over the word's region.
        marked_region = pix_region_ink(doc.render_with_edits(0, scale),
                                       hl_bbox, scale)
        check(failures, tag, marked_region > pristine_region * 1.5,
              f"highlight did not raise region ink "
              f"({pristine_region} -> {marked_region})")

        # save_as -> reopen: all four kinds with type / stroke / quad count.
        summary = [a for a in saved_annots(doc, tmpdir, "marked.pdf")
                   if a["page"] == 0]
        check(failures, tag, len(summary) == 4,
              f"saved page 0 carries {len(summary)} annots, not 4")
        by_type = {a["type"]: a for a in summary}
        for kind, wb in targets.items():
            a = by_type.get(KIND_TYPE[kind])
            if not check(failures, tag, a is not None,
                         f"saved file lacks a {kind} annot"):
                continue
            stroke_ok = all(abs(c - e) < 0.02 for c, e in
                            zip(a["stroke"], KIND_STROKE[kind]))
            check(failures, tag, stroke_ok,
                  f"{kind} stroke {a['stroke']} != {KIND_STROKE[kind]}")
            check(failures, tag, len(a["vertices"]) == 4,
                  f"{kind} vertex count {len(a['vertices'])} != 4 (1 quad)")

        # undo x4: gone from the RENDER and from a fresh SAVE.
        for _ in range(4):
            doc.undo()
        check(failures, tag, not doc._annots and doc.edit_count == 0,
              "undo did not clear the staged annot map")
        undone_region = pix_region_ink(doc.render_with_edits(0, scale),
                                       hl_bbox, scale)
        check(failures, tag, undone_region == pristine_region,
              f"region ink after undo {undone_region} != pristine "
              f"{pristine_region}")
        undone = saved_annots(doc, tmpdir, "undone.pdf")
        check(failures, tag,
              [a for a in undone if a["page"] == 0] == [],
              "saved page 0 still carries annots after undo")
        # The fixture's PRE-EXISTING page-2 annots ride along untouched.
        check(failures, tag,
              len([a for a in undone if a["page"] == 1]) == 2,
              "pre-existing page-2 annots lost in the save")

        # redo restores one (lockstep sanity on the shared history).
        doc.redo()
        check(failures, tag, len(doc._annots) == 1,
              "redo did not restore the first staged annot")
    finally:
        doc.close()
        shutil.rmtree(tmpdir, ignore_errors=True)


# ==========================================================================
# T2: UI gesture -- one command, line-grouped quads, undo, empty hit, Esc
# ==========================================================================
def test_t2_ui_gesture(failures: list[str]) -> None:
    tag = "T2_ui_gesture"
    w = open_window(ANNOT_TARGET)
    try:
        v = w.view
        doc = w.document
        words = doc.page_words(0)
        wb = word_named(words, "quarterly")
        if not check(failures, tag, wb is not None,
                     "fixture word 'quarterly' missing"):
            return

        w.act_markup_highlight.setChecked(True)
        pump(2)
        check(failures, tag, v.current_mode() == "markup_highlight",
              f"arming put the view in {v.current_mode()!r}")

        r = v._word_scene_rect(0, wb)
        idx0 = w.undo_stack.index()
        drag(v, QPointF(r.left() + 1, r.center().y()),
             QPointF(r.right() - 1, r.center().y()))
        check(failures, tag, w.undo_stack.index() == idx0 + 1,
              f"gesture took {w.undo_stack.index() - idx0} commands, not 1")
        check(failures, tag, isinstance(
            w.undo_stack.command(w.undo_stack.index() - 1), AnnotCommand),
            "pushed command is not an AnnotCommand")
        specs = list(doc._annots.values())
        if check(failures, tag, len(specs) == 1,
                 f"{len(specs)} staged annots after one gesture"):
            spec = specs[0]
            check(failures, tag, spec.kind == "highlight" and spec.quads,
                  f"staged spec wrong: {spec}")
            q = spec.quads[0]
            check(failures, tag,
                  q[0] <= wb.bbox[0] + 0.5 and q[2] >= wb.bbox[2] - 0.5
                  and q[1] <= wb.bbox[1] + 0.5 and q[3] >= wb.bbox[3] - 0.5,
                  f"quad {q} does not cover the dragged word {wb.bbox}")
            check(failures, tag,
                  spec.stroke == KIND_STROKE["highlight"],
                  f"window default stroke not applied: {spec.stroke}")
        check(failures, tag, v.current_mode() == "markup_highlight",
              "tool did not stay armed after the commit")
        check(failures, tag, v._markup_band is None,
              "accent band item leaked after release")

        w.undo_stack.undo()
        pump(2)
        check(failures, tag, not doc._annots,
              "window undo did not clear the staged highlight")

        # Empty hit: a drag over blank canvas -> NO command + the toast text.
        messages: list[str] = []
        v.statusMessage.connect(messages.append)
        layer = v._layers[0]
        blank_y = layer.y_top + layer.pt_size[1] * v._zoom * 0.62
        blank_x = layer.x_left + layer.pt_size[0] * v._zoom * 0.5
        idx1 = w.undo_stack.index()
        drag(v, QPointF(blank_x - 40, blank_y), QPointF(blank_x + 40, blank_y))
        check(failures, tag, w.undo_stack.index() == idx1,
              "empty-canvas drag pushed a command")
        check(failures, tag, "No text under selection" in messages,
              f"empty hit posted {messages!r}")

        # Esc disarms back to select; the action unchecks in sync.
        esc = QKeyEvent(QKeyEvent.Type.KeyPress, Qt.Key_Escape, Qt.NoModifier)
        _APP.sendEvent(v, esc)
        pump(2)
        check(failures, tag, v.current_mode() == "select",
              f"Esc left mode {v.current_mode()!r}")
        check(failures, tag, not w.act_markup_highlight.isChecked(),
              "Esc disarm did not uncheck the toolbar toggle")
    finally:
        close_window(w)


# ==========================================================================
# T3: render_signature + pixmap cache miss after add_annot (§9)
# ==========================================================================
def test_t3_cache_miss(failures: list[str]) -> None:
    tag = "T3_cache_miss"
    w = open_window(ANNOT_TARGET)
    try:
        v = w.view
        doc = w.document
        scale = v._zoom * (v.devicePixelRatioF() or 1.0)
        sig0 = doc.render_signature(0)
        check(failures, tag, v._render_cache.get(0, scale, sig0) is not None,
              "materialized page 0 has no cache entry for its signature")

        words = doc.page_words(0)
        wb = word_named(words, "Jordan")
        doc.add_annot(0, kind="highlight", quads=(tuple(wb.bbox),),
                      stroke=KIND_STROKE["highlight"])
        sig1 = doc.render_signature(0)
        check(failures, tag, sig1 != sig0,
              "render_signature did not change after add_annot")
        check(failures, tag, v._render_cache.get(0, scale, sig1) is None,
              "pixmap cache HIT for the post-add signature (stale pixels)")
        doc.undo()
        check(failures, tag, doc.render_signature(0) == sig0,
              "render_signature did not revert on undo")
    finally:
        close_window(w)


# ==========================================================================
# T4: rotate_page bakes the staged highlight into working
# ==========================================================================
def test_t4_rotate_bakes(failures: list[str]) -> None:
    tag = "T4_rotate_bakes"
    doc = PDFDocument(ANNOT_TARGET)
    try:
        wb = word_named(doc.page_words(0), "Jordan")
        doc.add_annot(0, kind="highlight", quads=(tuple(wb.bbox),),
                      stroke=KIND_STROKE["highlight"])
        doc.rotate_page(0, 90)
        page = doc.working[0]
        baked = list(page.annots() or ())
        check(failures, tag, len(baked) == 1
              and baked[0].type[0] == fitz.PDF_ANNOT_HIGHLIGHT,
              f"working[0] carries {len(baked)} annots after the bake")
        del baked, page
        check(failures, tag, not doc._annots and not doc._annot_overrides,
              "staged annot maps not cleared by the structural bake")
        check(failures, tag,
              not [c for c in doc._undo if type(c).__name__ == "_AnnotCommand"],
              "annot command entries survived the bake in the model history")
        check(failures, tag, doc.page_rotation(0) == 90,
              "rotation itself did not apply")
    finally:
        doc.close()


# ==========================================================================
# T5: WYSIWYG -- text edit + highlight on one page, rel diff < 0.02
# ==========================================================================
def test_t5_wysiwyg(failures: list[str]) -> None:
    tag = "T5_wysiwyg"
    tmpdir = tempfile.mkdtemp(prefix="annot_t5_")
    doc = PDFDocument(ANNOT_TARGET)
    try:
        # One staged TEXT edit (the in-place pipeline) ...
        target = None
        for box in doc.spans(0):
            if "Acme Corp" in getattr(box, "text", ""):
                target = box
                break
        if not check(failures, tag, target is not None,
                     "no 'Acme Corp' span on page 0"):
            return
        doc.stage_edit(0, target,
                       doc.staged_text(0, target).replace("Acme Corp",
                                                          "Acme Group"))
        # ... plus one staged highlight on the SAME page (quads from the
        # bake-aware page_words, so they track the staged text).
        wb = word_named(doc.page_words(0), "review")
        if wb is None:
            wb = doc.page_words(0)[5]
        doc.add_annot(0, kind="highlight", quads=(tuple(wb.bbox),),
                      stroke=KIND_STROKE["highlight"])

        out_path = os.path.join(tmpdir, "wysiwyg.pdf")
        doc.save_as(out_path)
        scale = 2.0
        rink = pix_ink(doc.render_with_edits(0, scale))
        saved = fitz.open(out_path)
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
# T6: rotated page -- the highlight lands on the word
# ==========================================================================
def test_t6_rotated(failures: list[str]) -> None:
    tag = "T6_rotated"
    tmpdir = tempfile.mkdtemp(prefix="annot_t6_")
    doc = PDFDocument(ROTATED)
    try:
        words = doc.page_words(0)
        wb = word_named(words, "ROTATED")
        if not check(failures, tag, wb is not None,
                     "rotated_doc word 'ROTATED' missing"):
            return
        scale = 1.5
        pristine = doc.render(0, scale)
        doc.add_annot(0, kind="highlight", quads=(tuple(wb.bbox),),
                      stroke=KIND_STROKE["highlight"])
        marked = doc.render_with_edits(0, scale)
        check(failures, tag,
              (pristine.width, pristine.height) == (marked.width, marked.height),
              "render sizes diverged")
        diff = changed_bbox(pristine, marked)
        if not check(failures, tag, diff is not None,
                     "highlight changed no pixels on the rotated page"):
            return
        # The word rect mapped through the page rotation matrix into display
        # space, scaled to pixels (the probe §1 mapping).
        m = doc.rotation_matrix(0)
        corners = [fitz.Point(x, y) * m
                   for x, y in ((wb.bbox[0], wb.bbox[1]),
                                (wb.bbox[2], wb.bbox[1]),
                                (wb.bbox[2], wb.bbox[3]),
                                (wb.bbox[0], wb.bbox[3]))]
        xs = [p.x * scale for p in corners]
        ys = [p.y * scale for p in corners]
        word_px = (min(xs), min(ys), max(xs), max(ys))
        overlaps = (diff[0] < word_px[2] and word_px[0] < diff[2]
                    and diff[1] < word_px[3] and word_px[1] < diff[3])
        check(failures, tag, overlaps,
              f"changed-pixel bbox {diff} misses the display-mapped word "
              f"rect {word_px}")
        # The saved file carries the highlight with the text-space quad.
        summary = saved_annots(doc, tmpdir, "rotated_marked.pdf")
        check(failures, tag, len(summary) == 1
              and summary[0]["type"] == fitz.PDF_ANNOT_HIGHLIGHT,
              f"saved rotated page annots: {summary}")
        if summary and summary[0]["vertices"]:
            vx = [p[0] for p in summary[0]["vertices"]]
            vy = [p[1] for p in summary[0]["vertices"]]
            close = (abs(min(vx) - wb.bbox[0]) < 2.0
                     and abs(max(vx) - wb.bbox[2]) < 2.0
                     and abs(min(vy) - wb.bbox[1]) < 2.0
                     and abs(max(vy) - wb.bbox[3]) < 2.0)
            check(failures, tag, close,
                  f"saved quad {summary[0]['vertices']} != word bbox "
                  f"{wb.bbox}")
    finally:
        doc.close()
        shutil.rmtree(tmpdir, ignore_errors=True)


# ==========================================================================
# T7: chrome -- actions, shortcuts, menu anchor, empty-state hiding
# ==========================================================================
def test_t7_chrome(failures: list[str]) -> None:
    tag = "T7_chrome"
    w = open_window(None)                 # NO document: the empty state
    try:
        for kind, key in (("highlight", "H"), ("underline", "U"),
                          ("strikeout", "K")):
            act = w._markup_actions[kind]
            check(failures, tag, act.isCheckable(),
                  f"{kind} action not checkable")
            check(failures, tag, act.shortcut() == QKeySequence(key),
                  f"{kind} shortcut {act.shortcut().toString()!r} != {key}")
        check(failures, tag,
              w.act_markup_squiggly.shortcut().isEmpty(),
              "squiggly (menu-only) must carry no bare key")
        check(failures, tag, not w._markup_actions["highlight"].isEnabled(),
              "markup action enabled with no document")
        # Charcoal redesign: the markup tools moved off the top toolbar into the
        # left rail's Markup palette. The rail Markup mode is disabled with no
        # document; the palette still hosts a row per markup tool.
        check(failures, tag, not w.left_panel._buttons["markup"].isEnabled(),
              "rail Markup mode enabled in the empty state")
        check(failures, tag, "highlight" in w._markup_panel_buttons,
              "Highlight not relocated to the rail Markup palette")

        # Tools menu: the four entries sit BEFORE the tools_annotate anchor.
        acts = w.menu_tools.actions()
        anchor_i = acts.index(w.menu_anchors["tools_annotate"])
        before = [a.text() for a in acts[:anchor_i]]
        for title in ("Highlight", "Underline", "Strikethrough", "Squiggly"):
            check(failures, tag, title in before,
                  f"{title} not inserted at the tools_annotate anchor")

        w.open_path(ANNOT_TARGET)
        pump(6)
        check(failures, tag, w._markup_actions["highlight"].isEnabled(),
              "markup action still disabled after open")
        check(failures, tag, w.left_panel._buttons["markup"].isEnabled(),
              "rail Markup mode still disabled after open")

        # Mutual exclusion with Add Text and between the toggles.
        w.act_markup_highlight.setChecked(True)
        pump(2)
        w.act_add_text.setChecked(True)
        pump(2)
        check(failures, tag, not w.act_markup_highlight.isChecked(),
              "arming Add Text left Highlight checked")
        check(failures, tag, w.view.current_mode() == "add_text",
              f"mode {w.view.current_mode()!r} after arming Add Text")
        w.act_markup_underline.setChecked(True)
        pump(2)
        check(failures, tag, not w.act_add_text.isChecked(),
              "arming Underline left Add Text checked")
        check(failures, tag, w.view.current_mode() == "markup_underline",
              f"mode {w.view.current_mode()!r} after arming Underline")
        w.act_markup_underline.setChecked(False)
        pump(2)
        check(failures, tag, w.view.current_mode() == "select",
              "unchecking the armed toggle did not return to select")
    finally:
        close_window(w)


# ==========================================================================
# T8 (M2): sticky-note UI flow -- one add + one contents, popup seam, save
# ==========================================================================
def test_t8_note_flow(failures: list[str]) -> None:
    tag = "T8_note_flow"
    tmpdir = tempfile.mkdtemp(prefix="annot_t8_")
    w = open_window(ANNOT_TARGET)
    try:
        v, doc = w.view, w.document

        # Chrome: the Sticky Note action + the Tools-menu M2 entries.
        act = w._markup_actions["note"]
        check(failures, tag, act.isCheckable()
              and act.shortcut() == QKeySequence("N"),
              "Sticky Note action not checkable / wrong shortcut")
        titles = [a.text() for a in w.menu_tools.actions()]
        check(failures, tag, "Sticky Note" in titles
              and "Delete Annotation" in titles,
              f"Tools menu lacks the M2 entries: {titles}")
        check(failures, tag, not w.act_delete_annot.isEnabled(),
              "Delete Annotation enabled with nothing selected")

        act.setChecked(True)
        pump(2)
        check(failures, tag, v.current_mode() == "note",
              f"arming Note put the view in {v.current_mode()!r}")

        layer = v._layers[0]
        pt = QPointF(layer.x_left + layer.pt_size[0] * v._zoom * 0.70,
                     layer.y_top + layer.pt_size[1] * v._zoom * 0.55)
        idx0 = w.undo_stack.index()
        click(v, pt)
        check(failures, tag, w.undo_stack.index() == idx0 + 1,
              f"note click took {w.undo_stack.index() - idx0} commands, not 1")
        specs = [s for s in doc._annots.values() if s.kind == "note"]
        if not check(failures, tag, len(specs) == 1,
                     f"{len(specs)} staged notes after one click"):
            return
        spec = specs[0]
        check(failures, tag,
              abs((spec.rect[2] - spec.rect[0]) - 18.0) < 0.01
              and abs((spec.rect[3] - spec.rect[1]) - 18.0) < 0.01,
              f"note anchor {spec.rect} is not 18x18pt")
        check(failures, tag, spec.stroke == (1.0, 0.8, 0.0),
              f"window note default stroke not applied: {spec.stroke}")
        check(failures, tag, spec.contents == "",
              f"fresh note contents {spec.contents!r} != ''")

        popup = w._note_popup
        if not check(failures, tag, popup is not None and popup.isVisible()
                     and popup.objectName() == "NotePopup",
                     "NotePopup did not open on note creation"):
            return
        check(failures, tag, v.current_mode() == "note",
              "note tool did not stay armed across the commit")

        popup.editor.setPlainText("Follow up with Jordan")
        popup.done_button.click()
        pump(2)
        check(failures, tag, w.undo_stack.index() == idx0 + 2,
              "popup Done did not land exactly ONE contents command")
        spec = [s for s in doc._annots.values() if s.kind == "note"][0]
        check(failures, tag, spec.contents == "Follow up with Jordan",
              f"staged contents {spec.contents!r} after commit")
        check(failures, tag, w._note_popup is None,
              "popup still registered after commit")

        # Re-open on the record; a NO-CHANGE commit emits no command.
        w.open_note_editor(spec.identity)
        pump(1)
        if check(failures, tag, w._note_popup is not None,
                 "open_note_editor seam did not open the popup"):
            check(failures, tag,
                  w._note_popup.editor.toPlainText() == spec.contents,
                  "popup did not load the current contents")
            idx1 = w.undo_stack.index()
            w._note_popup.done_button.click()
            pump(1)
            check(failures, tag, w.undo_stack.index() == idx1,
                  "a no-change popup commit pushed a command")

        # The saved file carries the Text annot with the typed content at
        # the staged anchor.
        summary = [a for a in saved_annots(doc, tmpdir, "note.pdf")
                   if a["page"] == 0]
        notes = [a for a in summary if a["type"] == fitz.PDF_ANNOT_TEXT]
        check(failures, tag, len(notes) == 1
              and notes[0]["content"] == "Follow up with Jordan",
              f"saved page-1 notes: {notes}")
        if notes:
            check(failures, tag,
                  abs(notes[0]["rect"][0] - spec.rect[0]) < 2.0
                  and abs(notes[0]["rect"][1] - spec.rect[1]) < 2.0,
                  f"saved note rect {notes[0]['rect']} far from anchor "
                  f"{spec.rect}")

        # Window undo x2 (contents, then add) clears the staged map.
        w.undo_stack.undo()
        w.undo_stack.undo()
        pump(2)
        check(failures, tag, not doc._annots,
              "window undo x2 did not clear the staged note")

        # Esc disarms back to select; the action unchecks in sync.
        esc = QKeyEvent(QKeyEvent.Type.KeyPress, Qt.Key_Escape, Qt.NoModifier)
        _APP.sendEvent(v, esc)
        pump(2)
        check(failures, tag, v.current_mode() == "select",
              f"Esc left mode {v.current_mode()!r}")
        check(failures, tag, not act.isChecked(),
              "Esc disarm did not uncheck the Sticky Note toggle")
    finally:
        close_window(w)
        shutil.rmtree(tmpdir, ignore_errors=True)


# ==========================================================================
# T9 (M2): canvas selection / move / delete + Alt+click annot-first
# ==========================================================================
def test_t9_selection_move_delete(failures: list[str]) -> None:
    tag = "T9_selection_move_delete"
    w = open_window(ANNOT_TARGET)
    try:
        v, doc = w.view, w.document
        w._on_annot_command_requested("add", None, {
            "kind": "note", "page_index": 0,
            "rect": (300.0, 300.0, 318.0, 318.0), "contents": "Draft"})
        pump(2)
        w.close_note_editor()          # the auto-opened popup is not under test
        pump(1)
        ident = [s for s in doc._annots.values() if s.kind == "note"][0].identity

        # The factory built an AnnotHotspot for the staged record.
        layer = v._layers[0]
        hotspots = [it for it in layer.extra_items
                    if type(it).__name__ == "AnnotHotspot"]
        check(failures, tag, any(h.identity == ident for h in hotspots),
              "no AnnotHotspot on layer.extra_items for the staged note")

        # Programmatic selection: overlay + Delete Annotation enablement.
        check(failures, tag, v.select_annot_by_identity(ident),
              "select_annot_by_identity failed")
        sel = v.current_annot_selection()
        check(failures, tag, sel is not None and sel.identity == ident,
              "current_annot_selection mismatch")
        check(failures, tag, v._annot_overlay is not None,
              "no selection overlay item")
        check(failures, tag, w.act_delete_annot.isEnabled(),
              "Delete Annotation not enabled on annot selection")

        # Click-select via the hotspot (after clearing).
        v.clear_annot_selection()
        pump(1)
        check(failures, tag, not w.act_delete_annot.isEnabled(),
              "Delete Annotation stayed enabled after clearing")
        r = v.annot_scene_rect(ident)
        click(v, r.center())
        sel = v.current_annot_selection()
        check(failures, tag, sel is not None and sel.identity == ident,
              "hotspot click did not select the annot")

        # Body drag = ONE 'move' command; the spec rect translates by the
        # exact PDF-point delta; the selection re-binds across the repaint.
        idx0 = w.undo_stack.index()
        z = v._zoom
        drag(v, r.center(), QPointF(r.center().x() + 40,
                                    r.center().y() + 25))
        check(failures, tag, w.undo_stack.index() == idx0 + 1,
              f"move drag took {w.undo_stack.index() - idx0} commands, not 1")
        spec2 = doc._annots[ident]
        check(failures, tag,
              abs(spec2.rect[0] - (300.0 + 40 / z)) < 0.5
              and abs(spec2.rect[1] - (300.0 + 25 / z)) < 0.5,
              f"moved rect {spec2.rect} != start + scene delta / zoom")
        sel = v.current_annot_selection()
        check(failures, tag, sel is not None and sel.identity == ident,
              "annot selection lost across the move repaint")
        w.undo_stack.undo()
        pump(2)
        check(failures, tag,
              doc._annots[ident].rect == (300.0, 300.0, 318.0, 318.0),
              f"undo left rect {doc._annots[ident].rect}")

        # Del = ONE 'delete' command; selection clears; undo restores.
        v.select_annot_by_identity(ident)
        pump(1)
        idx1 = w.undo_stack.index()
        _APP.sendEvent(v, QKeyEvent(QKeyEvent.Type.KeyPress, Qt.Key_Delete,
                                    Qt.NoModifier))
        pump(2)
        check(failures, tag,
              w.undo_stack.index() == idx1 + 1 and ident not in doc._annots,
              "Del did not produce exactly one delete")
        check(failures, tag, v.current_annot_selection() is None,
              "selection survived its annot's delete")
        w.undo_stack.undo()
        pump(1)
        check(failures, tag, ident in doc._annots,
              "undo did not restore the deleted note")

        # Existing page-2 highlight: a PLAIN click over it selects the BOX
        # (text hotspots win, z 10 vs 5); Alt+click forces the annot.
        v.set_page(1)
        pump(4)
        hl = next(r for r in doc.annotations(1)
                  if r.is_existing and r.kind == "highlight")
        hr = v.annot_scene_rect(hl.identity)
        click(v, hr.center())
        check(failures, tag, v.current_annot_selection() is None,
              "plain click over text selected the annot (text must win)")
        check(failures, tag, v.current_selection() is not None,
              "plain click over the line did not select its box")
        click(v, hr.center(), modifiers=Qt.AltModifier)
        sel = v.current_annot_selection()
        check(failures, tag, sel is not None and sel.identity == hl.identity,
              "Alt+click did not select the file highlight")
        # Mutual exclusion both ways.
        check(failures, tag, v.current_selection() is None,
              "box selection coexists with the annot selection")
    finally:
        close_window(w)


# ==========================================================================
# T10 (M2): existing-annot management via xref overrides (model)
# ==========================================================================
def test_t10_existing_management(failures: list[str]) -> None:
    tag = "T10_existing_management"
    tmpdir = tempfile.mkdtemp(prefix="annot_t10_")
    doc = PDFDocument(ANNOT_TARGET)
    try:
        existing = [r for r in doc.annotations(1) if r.is_existing]
        if not check(failures, tag, len(existing) == 2,
                     f"{len(existing)} existing records on page 2"):
            return
        kinds = sorted(r.kind for r in existing)
        check(failures, tag, kinds == ["highlight", "note"],
              f"existing kinds {kinds}")
        hl = next(r for r in existing if r.kind == "highlight")
        note = next(r for r in existing if r.kind == "note")
        check(failures, tag, note.contents == "Reviewed by Riley Morgan",
              f"note contents {note.contents!r}")
        check(failures, tag, isinstance(hl.xref, int)
              and hl.identity == (1, "xref", hl.xref),
              f"existing identity malformed: {hl.identity}")
        check(failures, tag, doc.annotations(0) == [],
              "page-1 records not empty")

        scale = 2.0
        region = hl.display_rect
        ink_with = pix_region_ink(doc.render_with_edits(1, scale),
                                  region, scale)

        # 1) DELETE the existing highlight: override staged, render ink
        # drops, the save lacks it; undo restores all of it.
        doc.delete_annot_box(1, hl)
        ov = doc._annot_overrides.get((1, hl.xref))
        check(failures, tag, ov is not None and ov.deleted,
              "no deleted override staged")
        check(failures, tag, doc.edit_count == 1 and doc.dirty,
              f"override census wrong: count {doc.edit_count}")
        check(failures, tag,
              all(r.xref != hl.xref
                  for r in doc.annotations(1) if r.is_existing),
              "deleted annot still enumerated")
        ink_without = pix_region_ink(doc.render_with_edits(1, scale),
                                     region, scale)
        check(failures, tag, ink_without < ink_with,
              f"delete did not drop region ink ({ink_with} -> "
              f"{ink_without})")
        summary = [a for a in saved_annots(doc, tmpdir, "deleted.pdf")
                   if a["page"] == 1]
        check(failures, tag, len(summary) == 1
              and summary[0]["type"] == fitz.PDF_ANNOT_TEXT,
              f"saved page-2 annots after delete: {summary}")
        doc.undo()
        check(failures, tag, not doc._annot_overrides,
              "undo left the delete override")
        check(failures, tag,
              len([a for a in saved_annots(doc, tmpdir, "restored.pdf")
                   if a["page"] == 1]) == 2,
              "undo did not restore the highlight in the save")
        check(failures, tag,
              pix_region_ink(doc.render_with_edits(1, scale),
                             region, scale) == ink_with,
              "region ink after undo != original")

        # 2) MOVE the existing NOTE: cumulative override, saved rect shifts;
        # existing markup refuses to move.
        r0 = note.display_rect
        doc.move_annot(1, note, 12.5, -7.0)
        rec = next(r for r in doc.annotations(1)
                   if r.is_existing and r.kind == "note")
        check(failures, tag,
              abs(rec.display_rect[0] - (r0[0] + 12.5)) < 1e-6
              and abs(rec.display_rect[1] - (r0[1] - 7.0)) < 1e-6,
              f"record rect not shifted: {rec.display_rect}")
        doc.move_annot(1, note, 0.5, 1.0)
        ov = doc._annot_overrides[(1, note.xref)]
        check(failures, tag,
              abs(ov.dx - 13.0) < 1e-6 and abs(ov.dy + 6.0) < 1e-6,
              f"moves did not fold cumulatively: dx {ov.dx} dy {ov.dy}")
        moved = [a for a in saved_annots(doc, tmpdir, "moved.pdf")
                 if a["page"] == 1 and a["type"] == fitz.PDF_ANNOT_TEXT]
        check(failures, tag, bool(moved)
              and abs(moved[0]["rect"][0] - (r0[0] + 13.0)) < 1.0
              and abs(moved[0]["rect"][1] - (r0[1] - 6.0)) < 1.0,
              f"saved note rect {moved and moved[0]['rect']} ignores the "
              f"override")
        doc.undo()                       # second move only
        ov = doc._annot_overrides[(1, note.xref)]
        check(failures, tag, abs(ov.dx - 12.5) < 1e-6,
              "undo did not pop just the second move")
        doc.undo()                       # first move
        check(failures, tag, not doc._annot_overrides,
              "undo did not clear the move override")
        try:
            doc.move_annot(1, hl, 5.0, 5.0)
            check(failures, tag, False,
                  "move_annot on existing markup did not raise")
        except ValueError:
            pass

        # 3) CONTENTS edit on the existing note; style stays forbidden.
        doc.set_annot_contents(1, note, "Approved by Jordan Carter")
        rec = next(r for r in doc.annotations(1)
                   if r.is_existing and r.kind == "note")
        check(failures, tag, rec.contents == "Approved by Jordan Carter",
              f"record contents {rec.contents!r}")
        edited = [a for a in saved_annots(doc, tmpdir, "contents.pdf")
                  if a["page"] == 1 and a["type"] == fitz.PDF_ANNOT_TEXT]
        check(failures, tag, bool(edited)
              and edited[0]["content"] == "Approved by Jordan Carter",
              f"saved contents: {edited}")
        doc.undo()
        rec = next(r for r in doc.annotations(1)
                   if r.is_existing and r.kind == "note")
        check(failures, tag, rec.contents == "Reviewed by Riley Morgan",
              "undo did not restore the contents")
        try:
            doc.modify_annot(1, hl, stroke=(0.0, 0.0, 1.0))
            check(failures, tag, False,
                  "existing-annot style modify did not raise")
        except ValueError:
            pass

        # 4) Overrides fold into render_signature (§9 cache contract).
        sig0 = doc.render_signature(1)
        doc.delete_annot_box(1, hl)
        check(failures, tag, doc.render_signature(1) != sig0,
              "render_signature ignored an override")
        doc.undo()
        check(failures, tag, doc.render_signature(1) == sig0,
              "render_signature did not revert with the override")
    finally:
        doc.close()
        shutil.rmtree(tmpdir, ignore_errors=True)


# ==========================================================================
# T11 (M2): in-place-edit guard -- text edit beside the file highlight
# ==========================================================================
def test_t11_inplace_edit_guard(failures: list[str]) -> None:
    tag = "T11_inplace_edit_guard"
    tmpdir = tempfile.mkdtemp(prefix="annot_t11_")
    doc = PDFDocument(ANNOT_TARGET)
    try:
        hl = next(r for r in doc.annotations(1)
                  if r.is_existing and r.kind == "highlight")
        # Baseline QuadPoints straight off the working doc (the annot RECT
        # is padded past the quad, so vertices compare against vertices).
        wpage = doc.working[1]
        base_vertices = [
            tuple(v) for v in next(
                a for a in wpage.annots()
                if a.type[0] == fitz.PDF_ANNOT_HIGHLIGHT).vertices]
        del wpage
        target = None
        for box in doc.spans(1):
            if "handoff" in getattr(box, "text", ""):
                target = box
                break
        if not check(failures, tag, target is not None,
                     "no 'handoff' span on page 2"):
            return
        doc.stage_edit(1, target,
                       doc.staged_text(1, target).replace("handoff",
                                                          "transfer"))
        out = os.path.join(tmpdir, "edited.pdf")
        doc.save_as(out)
        saved = fitz.open(out)
        try:
            page = saved[1]
            text = page.get_text()
            check(failures, tag,
                  "transfer" in text and "handoff" not in text,
                  "the in-place text edit did not land")
            annots = list(page.annots() or ())
            check(failures, tag, len(annots) == 2,
                  f"{len(annots)} annots after the edit-save (want 2)")
            hl_saved = next((a for a in annots
                             if a.type[0] == fitz.PDF_ANNOT_HIGHLIGHT), None)
            if check(failures, tag, hl_saved is not None,
                     "highlight lost in the edit-save"):
                saved_vertices = [tuple(v)
                                  for v in (hl_saved.vertices or [])]
                close = (len(saved_vertices) == len(base_vertices)
                         and all(abs(a[0] - b[0]) < 0.5
                                 and abs(a[1] - b[1]) < 0.5
                                 for a, b in zip(saved_vertices,
                                                 base_vertices)))
                check(failures, tag, close,
                      f"highlight vertices drifted: {saved_vertices} "
                      f"vs {base_vertices}")
        finally:
            saved.close()

        # No residue: the edited word's region ink in the SAVED file matches
        # the one shared pipeline's render (region_ink pattern), and the
        # whole page stays WYSIWYG.
        scale = 2.0
        region = tuple(target.bbox)
        rendered = doc.render_with_edits(1, scale)
        saved2 = fitz.open(out)
        try:
            saved_pix = saved2[1].get_pixmap(
                matrix=fitz.Matrix(scale, scale), alpha=False)
        finally:
            saved2.close()
        r_region = pix_region_ink(rendered, region, scale)
        s_region = pix_region_ink(saved_pix, region, scale)
        check(failures, tag, s_region > 0,
              "edited region carries no ink in the saved file")
        check(failures, tag,
              abs(r_region - s_region) / max(s_region, 1) < 0.05,
              f"edited-region residue: render {r_region} vs saved "
              f"{s_region}")
        rel = abs(pix_ink(rendered) - pix_ink(saved_pix)) \
            / max(pix_ink(saved_pix), 1)
        check(failures, tag, rel < 0.02,
              f"page-2 render/saved ink rel diff {rel:.4f} >= 0.02")
    finally:
        doc.close()
        shutil.rmtree(tmpdir, ignore_errors=True)


# ==========================================================================
# T12 (M3): model ink + shapes roundtrip -- type/stroke/width/fill/opacity/
# line_ends survive the save; undo returns the render to pristine ink
# ==========================================================================
def test_t12_ink_shapes_model(failures: list[str]) -> None:
    tag = "T12_ink_shapes_model"
    tmpdir = tempfile.mkdtemp(prefix="annot_t12_")
    doc = PDFDocument(ANNOT_TARGET)
    try:
        scale = 2.0
        pristine = pix_ink(doc.render_with_edits(0, scale))

        doc.add_annot(0, kind="ink",
                      points=(((110.0, 520.0), (150.0, 540.0),
                               (190.0, 525.0), (230.0, 560.0)),),
                      stroke=(0.2, 0.3, 0.8), width=2.5)
        doc.add_annot(0, kind="rect", rect=(100.0, 580.0, 200.0, 640.0),
                      stroke=(0.85, 0.1, 0.1), fill=(1.0, 0.9, 0.6),
                      width=2.0, opacity=0.4)
        doc.add_annot(0, kind="ellipse", rect=(220.0, 580.0, 320.0, 640.0),
                      stroke=(0.1, 0.5, 0.2), width=3.0)
        doc.add_annot(0, kind="line", endpoints=((100.0, 660.0),
                                                 (250.0, 700.0)),
                      stroke=(0.0, 0.0, 0.0), width=1.5)
        doc.add_annot(0, kind="arrow", endpoints=((300.0, 660.0),
                                                  (420.0, 700.0)),
                      stroke=(0.85, 0.1, 0.1), width=2.0)
        check(failures, tag, len(doc._annots) == 5 and doc.edit_count == 5,
              f"staged census {len(doc._annots)} / count {doc.edit_count}")

        marked = pix_ink(doc.render_with_edits(0, scale))
        check(failures, tag, marked > pristine,
              f"drawn annots did not raise page ink ({pristine} -> {marked})")

        summary = [a for a in saved_annots(doc, tmpdir, "drawn.pdf")
                   if a["page"] == 0]
        check(failures, tag, len(summary) == 5,
              f"saved page 0 carries {len(summary)} annots, not 5")

        def close3(a, b):
            return all(abs(c - e) < 0.02 for c, e in zip(a, b))

        ink = next((a for a in summary
                    if a["type"] == fitz.PDF_ANNOT_INK), None)
        if check(failures, tag, ink is not None, "no Ink annot saved"):
            check(failures, tag, close3(ink["stroke"], (0.2, 0.3, 0.8)),
                  f"ink stroke {ink['stroke']}")
            check(failures, tag, abs(ink["width"] - 2.5) < 0.01,
                  f"ink border width {ink['width']}")
            check(failures, tag, len(ink["vertices"]) == 1
                  and len(ink["vertices"][0]) == 4,
                  f"ink strokes/points wrong: {ink['vertices']}")

        sq = next((a for a in summary
                   if a["type"] == fitz.PDF_ANNOT_SQUARE), None)
        if check(failures, tag, sq is not None, "no Square annot saved"):
            check(failures, tag, close3(sq["stroke"], (0.85, 0.1, 0.1)),
                  f"square stroke {sq['stroke']}")
            check(failures, tag, close3(sq["fill"], (1.0, 0.9, 0.6)),
                  f"square fill {sq['fill']}")
            check(failures, tag, abs(sq["opacity"] - 0.4) < 0.01,
                  f"square opacity {sq['opacity']}")
            check(failures, tag, abs(sq["width"] - 2.0) < 0.01,
                  f"square border width {sq['width']}")

        ci = next((a for a in summary
                   if a["type"] == fitz.PDF_ANNOT_CIRCLE), None)
        if check(failures, tag, ci is not None, "no Circle annot saved"):
            check(failures, tag, close3(ci["stroke"], (0.1, 0.5, 0.2))
                  and abs(ci["width"] - 3.0) < 0.01,
                  f"circle stroke/width {ci['stroke']} / {ci['width']}")
            check(failures, tag, ci["fill"] == (),
                  f"fill-less circle saved fill {ci['fill']}")

        lines = [a for a in summary if a["type"] == fitz.PDF_ANNOT_LINE]
        check(failures, tag, len(lines) == 2,
              f"{len(lines)} Line annots saved, not 2")
        arrow = next((a for a in lines
                      if (a["line_ends"] or (0, 0))[1]
                      == fitz.PDF_ANNOT_LE_OPEN_ARROW), None)
        plain = next((a for a in lines if a is not arrow), None)
        if check(failures, tag, arrow is not None,
                 f"no open-arrow line ending saved: "
                 f"{[a['line_ends'] for a in lines]}"):
            check(failures, tag,
                  arrow["vertices"] == [(300.0, 660.0), (420.0, 700.0)],
                  f"arrow endpoints {arrow['vertices']}")
        if check(failures, tag, plain is not None, "plain line missing"):
            check(failures, tag,
                  (plain["line_ends"] or (0, 0))[1]
                  == fitz.PDF_ANNOT_LE_NONE,
                  f"plain line grew an ending {plain['line_ends']}")
            check(failures, tag, abs(plain["width"] - 1.5) < 0.01,
                  f"line border width {plain['width']}")

        # The undo chain returns the page render to its pristine ink count
        # and a fresh save to zero annots.
        for _ in range(5):
            doc.undo()
        check(failures, tag, not doc._annots and doc.edit_count == 0,
              "undo chain left staged annots")
        undone = pix_ink(doc.render_with_edits(0, scale))
        check(failures, tag, undone == pristine,
              f"ink after undo {undone} != pristine {pristine}")
        check(failures, tag,
              [a for a in saved_annots(doc, tmpdir, "undone.pdf")
               if a["page"] == 0] == [],
              "saved page 0 still carries annots after undo")

        # Geometry validation (the add_annot kind gate).
        for bad in ({"kind": "ink"},
                    {"kind": "ink", "points": (((1.0, 2.0),),)},
                    {"kind": "rect"}, {"kind": "ellipse"},
                    {"kind": "line"}, {"kind": "arrow"}):
            try:
                doc.add_annot(0, **bad)
                check(failures, tag, False, f"no raise for {bad}")
            except ValueError:
                pass
    finally:
        doc.close()
        shutil.rmtree(tmpdir, ignore_errors=True)


# ==========================================================================
# T13 (M3): UI ink + shape gestures -- one command each, previews, chrome
# ==========================================================================
def test_t13_ink_shape_gestures(failures: list[str]) -> None:
    tag = "T13_ink_shape_gestures"
    w = open_window(ANNOT_TARGET)
    try:
        v, doc = w.view, w.document
        layer = v._layers[0]

        def at(fx: float, fy: float) -> QPointF:
            return QPointF(layer.x_left + layer.pt_size[0] * v._zoom * fx,
                           layer.y_top + layer.pt_size[1] * v._zoom * fy)

        # Chrome: Draw Ink (D) + the Shapes submenu at the anchor.
        check(failures, tag,
              w.act_markup_ink.shortcut() == QKeySequence("D"),
              f"Draw Ink shortcut "
              f"{w.act_markup_ink.shortcut().toString()!r} != D")
        acts = w.menu_tools.actions()
        anchor_i = acts.index(w.menu_anchors["tools_annotate"])
        before = acts[:anchor_i]
        check(failures, tag, any(a.text() == "Draw Ink" for a in before),
              "Draw Ink not inserted at the tools_annotate anchor")
        shapes_menu_act = next(
            (a for a in before if a.menu() is getattr(w, "menu_shapes",
                                                      None)), None)
        check(failures, tag, shapes_menu_act is not None,
              "Shapes submenu not inserted at the tools_annotate anchor")
        check(failures, tag,
              [a.text() for a in w.menu_shapes.actions()]
              == ["Rectangle", "Ellipse", "Line", "Arrow"],
              f"Shapes submenu entries "
              f"{[a.text() for a in w.menu_shapes.actions()]}")
        # Charcoal redesign: ink + shapes live in the rail's Markup palette,
        # not on the top toolbar; the palette hosts a row for each.
        check(failures, tag, "ink" in w._markup_panel_buttons
              and all(k in w._markup_panel_buttons
                      for k in ("rect", "ellipse", "line", "arrow")),
              "ink / shapes not relocated to the rail Markup palette")

        # --- INK: press / move / release = ONE command, one stroke -------
        w.act_markup_ink.setChecked(True)
        pump(2)
        check(failures, tag, v.current_mode() == "ink",
              f"arming Ink put the view in {v.current_mode()!r}")
        p0, p1, p2 = at(0.20, 0.80), at(0.30, 0.82), at(0.40, 0.78)
        idx0 = w.undo_stack.index()
        send_mouse(v, p0, QEvent.MouseButtonPress)
        # Decimation: a sub-0.7pt move appends no point.
        send_mouse(v, QPointF(p0.x() + 0.3, p0.y()), QEvent.MouseMove)
        check(failures, tag, v._ink_drag is not None
              and len(v._ink_drag["points"]) == 1,
              "sub-decimation move appended a point")
        send_mouse(v, p1, QEvent.MouseMove)
        check(failures, tag, v._ink_item is not None,
              "no live ink path preview during the drag")
        send_mouse(v, p2, QEvent.MouseMove)
        send_mouse(v, p2, QEvent.MouseButtonRelease, buttons=Qt.NoButton)
        pump(3)
        check(failures, tag, w.undo_stack.index() == idx0 + 1,
              f"ink gesture took {w.undo_stack.index() - idx0} commands")
        inks = [s for s in doc._annots.values() if s.kind == "ink"]
        if check(failures, tag, len(inks) == 1,
                 f"{len(inks)} staged ink specs after one stroke"):
            spec = inks[0]
            check(failures, tag, len(spec.points) == 1
                  and len(spec.points[0]) == 3,
                  f"stroke census wrong: "
                  f"{[len(s) for s in spec.points]}")
            end = v._pdf_point(p2, 0)
            check(failures, tag,
                  abs(spec.points[0][-1][0] - end[0]) < 1.0
                  and abs(spec.points[0][-1][1] - end[1]) < 1.0,
                  f"stroke end {spec.points[0][-1]} != release {end}")
        check(failures, tag, v.current_mode() == "ink",
              "ink tool did not stay armed after the commit")
        check(failures, tag, v._ink_item is None,
              "ink preview item leaked after release")

        # A stationary click commits nothing (1 point < the 2-point floor).
        idx1 = w.undo_stack.index()
        click(v, at(0.22, 0.85))
        check(failures, tag, w.undo_stack.index() == idx1,
              "a stationary ink click pushed a command")

        # --- RECT: drag = ONE command, rect matches the corners ----------
        w._markup_actions["rect"].setChecked(True)
        pump(2)
        check(failures, tag, v.current_mode() == "shape_rect",
              f"arming Rectangle put the view in {v.current_mode()!r}")
        check(failures, tag, not w.act_markup_ink.isChecked(),
              "arming Rectangle left Draw Ink checked")
        # (The old toolbar "Shapes" last-used-default button is gone in the
        # Charcoal redesign: each shape is its own row in the rail Markup palette,
        # so there is no shared default action to track.)
        q0, q1 = at(0.55, 0.75), at(0.70, 0.85)
        idx2 = w.undo_stack.index()
        send_mouse(v, q0, QEvent.MouseButtonPress)
        send_mouse(v, q1, QEvent.MouseMove)
        check(failures, tag, v._shape_item is not None,
              "no live shape preview during the drag")
        send_mouse(v, q1, QEvent.MouseButtonRelease, buttons=Qt.NoButton)
        pump(3)
        check(failures, tag, w.undo_stack.index() == idx2 + 1,
              f"rect gesture took {w.undo_stack.index() - idx2} commands")
        rects = [s for s in doc._annots.values() if s.kind == "rect"]
        if check(failures, tag, len(rects) == 1,
                 f"{len(rects)} staged rects after one drag"):
            e0, e1 = v._pdf_point(q0, 0), v._pdf_point(q1, 0)
            want = (min(e0[0], e1[0]), min(e0[1], e1[1]),
                    max(e0[0], e1[0]), max(e0[1], e1[1]))
            got = rects[0].rect
            check(failures, tag,
                  all(abs(g - e) < 1.0 for g, e in zip(got, want)),
                  f"staged rect {got} != dragged corners {want}")
        check(failures, tag, v._shape_item is None,
              "shape preview item leaked after release")

        # < 3pt diagonal discards.
        idx3 = w.undo_stack.index()
        send_mouse(v, q0, QEvent.MouseButtonPress)
        send_mouse(v, QPointF(q0.x() + 1, q0.y() + 1),
                   QEvent.MouseButtonRelease, buttons=Qt.NoButton)
        pump(2)
        check(failures, tag, w.undo_stack.index() == idx3,
              "a tiny shape drag pushed a command")

        # --- ARROW: direction preserved, head preview shown ---------------
        w._markup_actions["arrow"].setChecked(True)
        pump(2)
        check(failures, tag, v.current_mode() == "shape_arrow",
              f"arming Arrow put the view in {v.current_mode()!r}")
        a0, a1 = at(0.20, 0.90), at(0.45, 0.93)
        idx4 = w.undo_stack.index()
        send_mouse(v, a0, QEvent.MouseButtonPress)
        send_mouse(v, a1, QEvent.MouseMove)
        check(failures, tag, v._shape_arrow_head is not None,
              "no arrowhead preview during the arrow drag")
        send_mouse(v, a1, QEvent.MouseButtonRelease, buttons=Qt.NoButton)
        pump(3)
        check(failures, tag, w.undo_stack.index() == idx4 + 1,
              f"arrow gesture took {w.undo_stack.index() - idx4} commands")
        arrows = [s for s in doc._annots.values() if s.kind == "arrow"]
        if check(failures, tag, len(arrows) == 1,
                 f"{len(arrows)} staged arrows after one drag"):
            end = v._pdf_point(a1, 0)
            tip = arrows[0].endpoints[1]
            check(failures, tag,
                  abs(tip[0] - end[0]) < 1.0 and abs(tip[1] - end[1]) < 1.0,
                  f"arrow tip {tip} != release point {end} (direction)")

        # --- Esc: aborts a live preview first, then disarms ----------------
        send_mouse(v, a0, QEvent.MouseButtonPress)
        send_mouse(v, a1, QEvent.MouseMove)
        idx5 = w.undo_stack.index()
        esc = QKeyEvent(QKeyEvent.Type.KeyPress, Qt.Key_Escape,
                        Qt.NoModifier)
        _APP.sendEvent(v, esc)
        pump(2)
        check(failures, tag, v._shape_drag is None
              and v._shape_item is None,
              "Esc did not abort the live shape preview")
        check(failures, tag, v.current_mode() == "shape_arrow",
              "Esc on a live preview disarmed the tool")
        check(failures, tag, w.undo_stack.index() == idx5,
              "the aborted shape drag pushed a command")
        # The orphaned release after the abort must not commit either.
        send_mouse(v, a1, QEvent.MouseButtonRelease, buttons=Qt.NoButton)
        pump(2)
        check(failures, tag, w.undo_stack.index() == idx5,
              "the post-abort release pushed a command")
        _APP.sendEvent(v, QKeyEvent(QKeyEvent.Type.KeyPress, Qt.Key_Escape,
                                    Qt.NoModifier))
        pump(2)
        check(failures, tag, v.current_mode() == "select",
              f"second Esc left mode {v.current_mode()!r}")
        check(failures, tag, not w._markup_actions["arrow"].isChecked(),
              "Esc disarm did not uncheck the Arrow toggle")
    finally:
        close_window(w)


# ==========================================================================
# T14 (M3): Inspector ANNOTATION section + per-tool session defaults
# ==========================================================================
def test_t14_inspector_styles(failures: list[str]) -> None:
    tag = "T14_inspector_styles"
    tmpdir = tempfile.mkdtemp(prefix="annot_t14_")
    w = open_window(ANNOT_TARGET)
    try:
        v, doc, insp = w.view, w.document, w.inspector
        scale = 2.0
        pristine = pix_ink(doc.render_with_edits(0, scale))

        w._on_annot_command_requested("add", None, {
            "kind": "rect", "page_index": 0,
            "rect": (120.0, 520.0, 240.0, 590.0)})
        pump(2)
        ident = next(s for s in doc._annots.values()
                     if s.kind == "rect").identity
        v.select_annot_by_identity(ident)
        pump(2)

        # The section is up, seeded from the spec; text sections hidden.
        check(failures, tag, insp._annot_host.isVisible(),
              "ANNOTATION section hidden on a staged shape selection")
        check(failures, tag, not insp._form_host.isVisible()
              and not insp._empty_hint.isVisible(),
              "text sections still visible under the annot target")
        check(failures, tag, insp.annot_width_spin.value() == 2.0
              and insp.annot_opacity_spin.value() == 100.0
              and insp.annot_nofill_check.isChecked(),
              "controls not seeded from the staged spec's defaults")
        check(failures, tag, insp._annot_fill_row.isVisible(),
              "fill controls hidden for a rect")
        check(failures, tag, insp.annot_width_spin.isEnabled(),
              "controls disabled for a STAGED annot")

        # ONE control change == ONE undo step, each.
        idx0 = w.undo_stack.index()
        insp.annot_width_spin.setValue(4.0)
        pump(2)
        check(failures, tag, w.undo_stack.index() == idx0 + 1,
              f"width change took {w.undo_stack.index() - idx0} commands")
        check(failures, tag, doc._annots[ident].width == 4.0,
              f"staged width {doc._annots[ident].width} != 4.0")
        insp.annotStyleEdited.emit({"stroke": (0.1, 0.2, 0.9)})
        pump(2)
        check(failures, tag, w.undo_stack.index() == idx0 + 2
              and doc._annots[ident].stroke == (0.1, 0.2, 0.9),
              "stroke emission did not land one style command")
        insp.annot_nofill_check.setChecked(False)
        pump(2)
        check(failures, tag, w.undo_stack.index() == idx0 + 3
              and doc._annots[ident].fill == (1.0, 1.0, 1.0),
              f"No-fill toggle: fill {doc._annots[ident].fill}")
        insp.annot_opacity_spin.setValue(40.0)
        pump(2)
        check(failures, tag, w.undo_stack.index() == idx0 + 4
              and abs(doc._annots[ident].opacity - 0.4) < 1e-9,
              f"opacity {doc._annots[ident].opacity} != 0.4")
        # The controls track the committed record across the repaints.
        check(failures, tag, insp.annot_width_spin.value() == 4.0
              and not insp.annot_nofill_check.isChecked(),
              "controls drifted from the committed spec")
        sel = v.current_annot_selection()
        check(failures, tag, sel is not None and sel.identity == ident,
              "annot selection lost across the style repaints")

        # The saved file carries the styled square.
        sq = next((a for a in saved_annots(doc, tmpdir, "styled.pdf")
                   if a["page"] == 0
                   and a["type"] == fitz.PDF_ANNOT_SQUARE), None)
        if check(failures, tag, sq is not None, "styled square not saved"):
            check(failures, tag,
                  all(abs(c - e) < 0.02 for c, e in
                      zip(sq["stroke"], (0.1, 0.2, 0.9)))
                  and all(abs(c - e) < 0.02 for c, e in
                          zip(sq["fill"], (1.0, 1.0, 1.0)))
                  and abs(sq["width"] - 4.0) < 0.01
                  and abs(sq["opacity"] - 0.4) < 0.01,
                  f"saved style drifted: {sq}")

        # Undo chain: 4 style steps + the add return the pristine render.
        for _ in range(5):
            w.undo_stack.undo()
        pump(2)
        check(failures, tag, not doc._annots,
              "undo chain left staged annots")
        check(failures, tag,
              pix_ink(doc.render_with_edits(0, scale)) == pristine,
              "undo chain did not return the page to pristine ink")

        # PRE-EXISTING file annot: grayed section + ignored emissions.
        hl = next(r for r in doc.annotations(1)
                  if r.is_existing and r.kind == "highlight")
        v.select_annot_by_identity(hl.identity)
        pump(2)
        check(failures, tag, insp._annot_host.isVisible()
              and not insp.annot_width_spin.isEnabled()
              and insp._annot_hint.isVisible(),
              "existing annot did not gray the section")
        idx1 = w.undo_stack.index()
        insp.annotStyleEdited.emit({"stroke": (0.0, 0.0, 0.0)})
        pump(2)
        check(failures, tag, w.undo_stack.index() == idx1
              and not doc._annot_overrides,
              "a style emission on a file annot staged something")

        # Armed tool + nothing selected: the controls edit the SESSION
        # DEFAULTS -- no undo entries; the next add uses them.
        v.clear_annot_selection()
        pump(1)
        w._markup_actions["ellipse"].setChecked(True)
        pump(2)
        check(failures, tag, insp._annot_host.isVisible()
              and insp.annot_width_spin.isEnabled()
              and not insp._annot_hint.isVisible(),
              "armed-tool defaults section not editable")
        idx2 = w.undo_stack.index()
        insp.annot_width_spin.setValue(5.0)
        pump(2)
        check(failures, tag, w.undo_stack.index() == idx2,
              "a session-default edit pushed an undo command")
        check(failures, tag,
              w._annot_defaults["ellipse"]["width"] == 5.0,
              f"defaults not updated: {w._annot_defaults['ellipse']}")
        w._on_annot_command_requested("add", None, {
            "kind": "ellipse", "page_index": 0,
            "rect": (300.0, 520.0, 380.0, 560.0)})
        pump(2)
        ell = next(s for s in doc._annots.values() if s.kind == "ellipse")
        check(failures, tag, ell.width == 5.0,
              f"new ellipse ignored the edited default: {ell.width}")

        # Esc disarms: the panel returns to its empty state.
        _APP.sendEvent(v, QKeyEvent(QKeyEvent.Type.KeyPress, Qt.Key_Escape,
                                    Qt.NoModifier))
        pump(2)
        check(failures, tag, not insp._annot_host.isVisible()
              and insp._empty_hint.isVisible(),
              "disarming did not return the panel to its empty state")
    finally:
        close_window(w)
        shutil.rmtree(tmpdir, ignore_errors=True)


# ==========================================================================
# T15 (M4): comments panel -- list, jump, edit, delete, refresh wiring
# ==========================================================================
def test_t15_comments_panel(failures: list[str]) -> None:
    tag = "T15_comments_panel"
    w = open_window(None)
    try:
        panel = w.comments_panel
        lst = panel.list

        # Chrome: stack page 2 (the conflict-ledger slot), the strip's
        # Comments tool, the View-menu toggle at the view_panels anchor.
        check(failures, tag, w.left_panel._stack.indexOf(panel) == 2,
              f"comments panel at stack index "
              f"{w.left_panel._stack.indexOf(panel)}, not 2")
        check(failures, tag, "comments" in w.left_panel._buttons,
              "no Comments button in the left tool strip")
        check(failures, tag, lst.objectName() == "CommentsList",
              f"list objectName {lst.objectName()!r}")
        act = w.act_show_comments
        check(failures, tag, act.isCheckable()
              and act.shortcut() == QKeySequence("Ctrl+Shift+C"),
              "Show Comments not checkable / wrong shortcut")
        check(failures, tag, not act.isEnabled(),
              "Show Comments enabled with no document")
        va = w.menu_view.actions()
        anchor = w.menu_anchors["view_panels"]
        check(failures, tag, act in va
              and va.index(act) < va.index(anchor),
              "Show Comments not before the view_panels anchor")
        check(failures, tag, not panel.edit_button.isEnabled()
              and not panel.delete_button.isEnabled(),
              "row buttons enabled with no selection")

        # Open: the two pre-existing page-2 file annots are listed.
        w.open_path(ANNOT_TARGET)
        pump(6)
        v, doc = w.view, w.document
        check(failures, tag, act.isEnabled(),
              "Show Comments still disabled after open")
        if not check(failures, tag, lst.count() == 2,
                     f"{lst.count()} rows after open, expected 2 file annots"):
            return
        texts = [lst.item(i).text() for i in range(lst.count())]
        check(failures, tag,
              all("(file)" in t and "p.2" in t for t in texts),
              f"file rows mis-tagged: {texts}")
        check(failures, tag,
              any("Reviewed by Riley Morgan" in t for t in texts),
              f"note contents snippet missing: {texts}")

        # Two staged page-1 annots AUTO-refresh the list (the AnnotCommand
        # on_change hook -- no manual refresh in this test).
        wb = word_named(doc.page_words(0), "quarterly")
        if not check(failures, tag, wb is not None,
                     "fixture word 'quarterly' missing"):
            return
        w._on_annot_command_requested("add", None, {
            "kind": "highlight", "page_index": 0, "quads": (tuple(wb.bbox),)})
        pump(2)
        w._on_annot_command_requested("add", None, {
            "kind": "note", "page_index": 0,
            "rect": (90.0, 700.0, 108.0, 718.0),
            "contents": "Check with Jordan"})
        pump(2)
        w.close_note_editor()          # the auto-opened popup is not under test
        pump(1)
        if not check(failures, tag, lst.count() == 4,
                     f"{lst.count()} rows after 2 staged adds (no auto-refresh?)"):
            return
        check(failures, tag, f"{lst.count()} comments"
              == panel.count_label.text(),
              f"count label {panel.count_label.text()!r}")
        rows = [lst.item(i) for i in range(4)]
        pages = [it.data(Qt.UserRole)[0] for it in rows]
        check(failures, tag, pages == sorted(pages) and pages[0] == 0
              and pages[-1] == 1,
              f"rows not sorted by page: {pages}")
        p1_texts = [it.text() for it in rows if it.data(Qt.UserRole)[0] == 0]
        check(failures, tag, len(p1_texts) == 2
              and "Highlight" in p1_texts[0]
              and "Check with Jordan" in p1_texts[1],
              f"page-1 rows not y-sorted staged rows: {p1_texts}")
        check(failures, tag,
              all("(file)" not in t for t in p1_texts),
              f"staged rows carry the (file) tag: {p1_texts}")

        # Show Comments toggle -> the strip's Comments tool + the panel.
        act.setChecked(True)
        pump(3)
        check(failures, tag, w.left_panel.active_tool() == "comments",
              f"toggle armed tool {w.left_panel.active_tool()!r}")
        check(failures, tag,
              w.left_panel._stack.currentWidget() is panel,
              "toggle did not swap the left column to the comments panel")
        check(failures, tag,
              w.left_panel._header_label.text() == "Comments",
              f"header {w.left_panel._header_label.text()!r}")

        # Click a page-2 row: jump + select by identity (the M4 assert).
        # Re-grab the items: entering the panel refreshed (rebuilt) the rows.
        rows = [lst.item(i) for i in range(lst.count())]
        note_row = next(it for it in rows
                        if "(file)" in it.text() and "Riley" in it.text())
        if not check(failures, tag,
                     lst.visualItemRect(note_row).height() > 0,
                     "page-2 row has no visual rect to click"):
            return
        click_list_item(lst, note_row)
        check(failures, tag, w.view_page_index() == 1,
              f"row click left the view on page {w.view_page_index()}")
        sel = v.current_annot_selection()
        check(failures, tag, sel is not None
              and sel.identity == note_row.data(Qt.UserRole),
              "row click did not select the annot by identity")

        # Panel Delete: ONE undoable command + auto-refresh; undo restores.
        hl_row = next(it for it in rows
                      if it.data(Qt.UserRole)[0] == 0 and "Highlight" in it.text())
        click_list_item(lst, hl_row)
        idx0 = w.undo_stack.index()
        panel.delete_button.click()
        pump(2)
        check(failures, tag, w.undo_stack.index() == idx0 + 1,
              f"panel Delete took {w.undo_stack.index() - idx0} commands")
        check(failures, tag, lst.count() == 3,
              f"{lst.count()} rows after panel Delete")
        check(failures, tag,
              not any(s.kind == "highlight" for s in doc._annots.values()),
              "staged highlight survived the panel Delete")
        w.undo_stack.undo()
        pump(2)
        check(failures, tag, lst.count() == 4,
              f"undo restored {lst.count()} rows, not 4")
        check(failures, tag,
              any(s.kind == "highlight" for s in doc._annots.values()),
              "undo did not restore the staged highlight")

        # Edit Note: the popup opens seeded; a commit lands ONE 'contents'
        # command and the row text follows.
        note_item = next(lst.item(i) for i in range(lst.count())
                         if "Check with Jordan" in lst.item(i).text())
        click_list_item(lst, note_item)
        panel.edit_button.click()
        pump(2)
        popup = w._note_popup
        if not check(failures, tag, popup is not None and popup.isVisible(),
                     "panel Edit Note did not open the popup"):
            return
        check(failures, tag,
              popup.editor.toPlainText() == "Check with Jordan",
              "popup not seeded with the row's contents")
        popup.editor.setPlainText("Ping Riley about scope")
        idx1 = w.undo_stack.index()
        popup.done_button.click()
        pump(2)
        check(failures, tag, w.undo_stack.index() == idx1 + 1,
              "popup commit from the panel was not ONE command")
        check(failures, tag,
              any("Ping Riley about scope" in lst.item(i).text()
                  for i in range(lst.count())),
              "edited note text not reflected in the panel")

        # Structural rotate bakes staged annots: every row becomes (file).
        w.rotate_current_cw()
        pump(3)
        check(failures, tag, lst.count() == 4
              and all("(file)" in lst.item(i).text()
                      for i in range(lst.count())),
              "structural bake did not re-list rows as file annots")
        check(failures, tag, not doc._annots and not doc._annot_overrides,
              "staged maps survived the structural bake")

        # Tab switch re-lists the new active document, both directions.
        w.open_path(ROTATED)
        pump(5)
        check(failures, tag, lst.count() == 0,
              f"{lst.count()} rows listed for the annot-free rotated doc")
        w._activate_document(0)
        pump(3)
        check(failures, tag, lst.count() == 4,
              f"{lst.count()} rows after switching back, not 4")

        # The toggle and the strip stay in lockstep both ways.
        check(failures, tag, act.isChecked(),
              "Show Comments unchecked while the comments tool is active")
        w.left_panel._buttons["select"].click()     # the strip path
        pump(2)
        check(failures, tag, not act.isChecked(),
              "picking another strip tool left Show Comments checked")
        act.setChecked(True)
        pump(2)
        check(failures, tag, w.left_panel.active_tool() == "comments",
              "re-checking the toggle did not re-arm the Comments tool")
        act.setChecked(False)                       # the action path
        pump(2)
        check(failures, tag, w.left_panel.active_tool() == "select"
              and w.left_panel._header_label.text() == "Format",
              "unchecking did not return the column to Select/Format")
    finally:
        close_window(w)


# ==========================================================================
def main() -> int:
    failures: list[str] = []
    tests = [
        ("T1_model_roundtrip", lambda: test_t1_model_roundtrip(failures)),
        ("T2_ui_gesture", lambda: test_t2_ui_gesture(failures)),
        ("T3_cache_miss", lambda: test_t3_cache_miss(failures)),
        ("T4_rotate_bakes", lambda: test_t4_rotate_bakes(failures)),
        ("T5_wysiwyg", lambda: test_t5_wysiwyg(failures)),
        ("T6_rotated", lambda: test_t6_rotated(failures)),
        ("T7_chrome", lambda: test_t7_chrome(failures)),
        ("T8_note_flow", lambda: test_t8_note_flow(failures)),
        ("T9_selection_move_delete",
         lambda: test_t9_selection_move_delete(failures)),
        ("T10_existing_management",
         lambda: test_t10_existing_management(failures)),
        ("T11_inplace_edit_guard",
         lambda: test_t11_inplace_edit_guard(failures)),
        ("T12_ink_shapes_model",
         lambda: test_t12_ink_shapes_model(failures)),
        ("T13_ink_shape_gestures",
         lambda: test_t13_ink_shape_gestures(failures)),
        ("T14_inspector_styles",
         lambda: test_t14_inspector_styles(failures)),
        ("T15_comments_panel",
         lambda: test_t15_comments_panel(failures)),
    ]
    for name, fn in tests:
        print(f"[{name}]")
        try:
            fn()
        except Exception:
            failures.append(f"{name}: raised:\n{traceback.format_exc()}")

    print()
    if failures:
        print(f"FAILED ({len(failures)}):")
        for f in failures:
            print(" -", f)
        return 1
    print("test_annotations: all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
