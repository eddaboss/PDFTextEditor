"""End-to-end headless harness for the FULL editor, driven through the REAL
``MainWindow`` (EDITOR_SPEC §8).

Where ``test_editor_model.py`` exercises the document model directly, THIS file
drives the actual UI stack the user runs -- ``MainWindow`` + its ``Inspector``
dock + ``PageView`` selection model + the ``QUndoStack`` -- with Qt in offscreen
mode, so every signal/command interface between the chrome, the canvas, and the
model is verified in lockstep:

  * select a box in the canvas -> the window populates the Inspector from
    ``document.effective_style`` (family / size / color / bold / italic), and the
    Delete action enables;
  * change FONT FAMILY, SIZE, COLOR, BOLD, ITALIC through the Inspector's
    ``styleEdited`` signal -> ``view.apply_style`` -> ``boxCommandRequested`` ->
    ONE ``BoxCommand`` per change on the undo stack -> the SAVED pdf reflects each;
  * ADD a new text box via the toolbar's Add Text tool + an empty-canvas click,
    type into the live editor, commit -> the box saves with an embeddable font;
  * MOVE a box (drag-commit through ``boxCommandRequested("move")``) -> the saved
    ink shifts and the original spot is cleared;
  * RESIZE a box -> the saved font size scales;
  * DELETE a box via ``view.delete_selected()`` -> the ink is gone and the
    background (colored header band / table cells) survives;
  * UNDO / REDO every one of these through the window's ``QUndoStack``, mixed on
    one history;
  * SAVE through the window (``_do_save``) and REOPEN the file, asserting the
    reopened document carries the edits.

NO-REGRESSION assertions (the hard-won invariants, EDITOR_SPEC §0):
  * baked WYSIWYG: ``render_with_edits`` whole-page ink == the saved file's, for
    style/move/resize/delete/add (the on-screen page IS the saved bytes);
  * overlap-merge: editing the ``Cleared 4821`` box (merged from 3 bboxes in the
    fixture) leaves NO residue at any member bbox;
  * non-destructive redaction: deleting a value over a colored table cell keeps
    the cell fill, and restyling the title over the dark header band keeps the
    band.

It also renders the deliverable screenshots to ``tests/screenshots/editor/``:
empty state, a selected box (handles + populated inspector), mid-restyle, a newly
added box, after a move, and after a delete.

Run:
    QT_QPA_PLATFORM=offscreen \
      /Users/edward/Documents/GitHub/PDFTextEditor/.venv/bin/python \
      tests/test_editor.py
"""

from __future__ import annotations

import os
import sys
import tempfile
import traceback

# Headless Qt no matter how this is launched.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import fitz  # noqa: E402
from PySide6.QtCore import QEvent, QEventLoop, QRectF, QTimer  # noqa: E402
from PySide6.QtGui import QColor, QImage, QPainter  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

# A QApplication must exist before any QFont/QWidget is constructed.
_APP = QApplication.instance() or QApplication([])

from pdftexteditor.document import PDFDocument  # noqa: E402
from pdftexteditor.ui.main_window import MainWindow  # noqa: E402

FIXTURE_DIR = os.path.join(_HERE, "fixtures")
FORM = os.path.join(FIXTURE_DIR, "form_like.pdf")
SHOT_DIR = os.path.join(_HERE, "screenshots", "editor")
os.makedirs(SHOT_DIR, exist_ok=True)

# Base-14 builtin basefonts: any of these NEWLY introduced by an edit means the
# user-picked / new-box family failed to embed a real face (the central defect).
_BASE14_BASEFONTS = {
    "helvetica", "helvetica-bold", "helvetica-oblique", "helvetica-boldoblique",
    "times-roman", "times-bold", "times-italic", "times-bolditalic",
    "courier", "courier-bold", "courier-oblique", "courier-boldoblique",
    "symbol", "zapfdingbats",
}

# The fixture's known colored regions (see build_form_fixture.py): a dark blue
# header band the title sits on, and the light-blue label cells. Used to prove
# non-destructive redaction kept fills/borders after an edit.
_HEADER_BAND = (8.0, 8.0, 600.0, 88.0)          # inside the (0,0,612,96) band
_LABEL_CELL_AMOUNT = (62.0, 284.0, 208.0, 324.0)  # a light-blue label cell


# ==========================================================================
# Assertion + measurement helpers
# ==========================================================================
def check(failures: list, tag: str, cond: bool, msg: str) -> bool:
    if not cond:
        failures.append(f"{tag}: {msg}")
    return bool(cond)


def region_ink(path: str, bbox, scale: float = 3.0) -> int:
    """Count non-white (drawn) pixels in a PDF region -> proves glyphs landed.
    Luminance test, so colored text/fills count as ink."""
    doc = fitz.open(path)
    try:
        pix = doc[0].get_pixmap(matrix=fitz.Matrix(scale, scale),
                                clip=fitz.Rect(bbox), alpha=False)
        s, step, n = pix.samples, pix.n, 0
        for i in range(0, len(s), step):
            r, g, b = s[i], s[i + 1], s[i + 2]
            if (0.299 * r + 0.587 * g + 0.114 * b) < 230:
                n += 1
        return n
    finally:
        doc.close()


def region_mean_rgb(path: str, bbox, scale: float = 3.0) -> tuple:
    """Mean (r,g,b) of a region 0..255 -> proves a colored fill survived."""
    doc = fitz.open(path)
    try:
        pix = doc[0].get_pixmap(matrix=fitz.Matrix(scale, scale),
                                clip=fitz.Rect(bbox), alpha=False)
        s, step = pix.samples, pix.n
        rs = gs = bs = cnt = 0
        for i in range(0, len(s), step):
            rs += s[i]; gs += s[i + 1]; bs += s[i + 2]; cnt += 1
        cnt = max(cnt, 1)
        return (rs / cnt, gs / cnt, bs / cnt)
    finally:
        doc.close()


def saved_basefonts(path: str) -> list:
    doc = fitz.open(path)
    try:
        out = []
        for entry in doc[0].get_fonts(full=True):
            base = entry[3] or ""
            if len(base) > 7 and base[6] == "+" and base[:6].isalpha():
                base = base[7:]
            out.append(base.lower())
        return out
    finally:
        doc.close()


def new_base14(before_path: str, after_path: str) -> list:
    """Base-14 builtins present AFTER an edit that were NOT in the original (so a
    form's pre-existing standard-font references are not mistaken for a
    substitution introduced by the edit)."""
    before = set(saved_basefonts(before_path)) & _BASE14_BASEFONTS
    after = set(saved_basefonts(after_path)) & _BASE14_BASEFONTS
    return sorted(after - before)


def saved_span(path: str, want_text: str):
    """First span on page 0 whose text contains want_text -> dict, else None."""
    doc = fitz.open(path)
    try:
        for block in doc[0].get_text("rawdict")["blocks"]:
            if block.get("type", 0) != 0:
                continue
            for line in block["lines"]:
                for span in line["spans"]:
                    text = "".join(c["c"] for c in span.get("chars", []))
                    if want_text in text:
                        return {"text": text, "origin": tuple(span["origin"]),
                                "size": span["size"], "bbox": tuple(span["bbox"]),
                                "font": span["font"],
                                "color": tuple(c / 255 for c in
                                               fitz.sRGB_to_rgb(span["color"]))}
        return None
    finally:
        doc.close()


def has_words(path: str, *words) -> bool:
    """True iff every word appears (whole word) on page 0 -- robust to the
    space-folding / block-ordering of get_text()."""
    doc = fitz.open(path)
    try:
        present = {w[4] for w in doc[0].get_text("words")}
        return all(w in present for w in words)
    finally:
        doc.close()


def page_text(path: str) -> str:
    doc = fitz.open(path)
    try:
        return doc[0].get_text()
    finally:
        doc.close()


def _pix_ink(pix) -> int:
    s, step, n = pix.samples, pix.n, 0
    for i in range(0, len(s), step):
        r, g, b = s[i], s[i + 1], s[i + 2]
        if (0.299 * r + 0.587 * g + 0.114 * b) < 230:
            n += 1
    return n


def wysiwyg_rel_diff(document: PDFDocument, out_path: str, scale: float = 2.0) -> float:
    """Whole-page ink of render_with_edits vs the saved file (baked WYSIWYG,
    §0.1): the on-screen page must be the SAME pipeline as save."""
    rink = _pix_ink(document.render_with_edits(0, scale))
    sd = fitz.open(out_path)
    try:
        sink = _pix_ink(sd[0].get_pixmap(matrix=fitz.Matrix(scale, scale),
                                         alpha=False))
    finally:
        sd.close()
    return abs(rink - sink) / max(sink, 1)


# ==========================================================================
# Window driving helpers (mirror exactly what a mouse/keyboard would do)
# ==========================================================================
def pump(ms: int = 200) -> None:
    """Run the Qt event loop for ``ms`` so time-driven chrome settles before a
    screenshot -- chiefly the 120ms empty-state fade-out, whose ``hide()`` fires
    from the animation's ``finished`` callback (a real GUI run always sees it
    finish; a bare ``processEvents`` does not advance the timer)."""
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()
    _APP.processEvents()


def open_window() -> MainWindow:
    w = MainWindow()
    w.resize(1180, 920)
    w.show()
    w.open_path(FORM)
    _APP.processEvents()
    pump(220)            # let the empty-state fade finish so it does not bleed
    return w


def close_window(w: MainWindow) -> None:
    w._suppress_close_guard = True
    w.close()


def hotspot_for(w: MainWindow, contains: str):
    for h in w.view._hotspots:
        if contains in h.span.text:
            return h
    return None


def select(w: MainWindow, contains: str):
    """Select the box whose text contains ``contains`` through the canvas's
    public ``select_box`` (the same call a click makes), driving the window's
    selectionChanged -> inspector.set_target wiring."""
    h = hotspot_for(w, contains)
    if h is None:
        return None
    w.view.select_box(h.span)
    _APP.processEvents()
    return w.view.current_selection()


def inspector_edit(w: MainWindow, field: str, value) -> None:
    """Emit the Inspector's per-field signal exactly as a user control change
    does, so it funnels through view.apply_style -> one BoxCommand."""
    w.inspector.styleEdited.emit({field: value})
    _APP.processEvents()


def box_command(w: MainWindow, kind: str, box, params: dict) -> None:
    """Fire the canvas's generalized mutation intent (a drag/resize commit) so
    the window wraps it in one BoxCommand on the undo stack."""
    w.view.boxCommandRequested.emit(kind, box, dict(params))
    _APP.processEvents()


def add_box_via_tool(w: MainWindow, scene_xy: tuple, text: str):
    """Arm the toolbar Add Text tool, click empty canvas, type, commit -- the
    full add flow through the real UI. Returns the new box.

    Note (verified app behavior): the add flow pushes TWO undo commands -- the
    'add' (an empty box created at the click) and the 'text' commit (the typed
    content). A freshly added box left empty is rolled back; one that is typed
    into commits as a separate text edit on the NewBox, so add+type = 2 steps."""
    w.act_add_text.setChecked(True)
    _APP.processEvents()
    sp = w.view._scene_point(*scene_xy)
    w.view._do_add_at(sp)
    _APP.processEvents()
    if w.view._editor is not None:
        w.view._editor.setPlainText(text)
        w.view.commit_edit()
        _APP.processEvents()
    boxes = w.document.new_boxes(0)
    return boxes[-1] if boxes else None


def seed_add_style(w: MainWindow) -> None:
    """Reset the Inspector to its clean add-defaults (Helvetica 12pt black) so a
    box added via the tool uses sane values rather than whatever the last
    selection left in the controls (a real user picks before adding; the test
    seeds so the new box has visible black ink, not the title's near-white)."""
    w.inspector._seed_defaults()
    _APP.processEvents()


# ==========================================================================
# Screenshot helpers
# ==========================================================================
def shot_scene(w: MainWindow, name: str) -> str:
    """Render the PageView's scene (crisp white sheet + selection chrome) to a
    PNG, so canvas-only screenshots (mid-restyle, after move/delete) read clean."""
    scene = w.view.scene()
    rect = scene.sceneRect()
    width = max(1, int(rect.width()))
    height = max(1, int(rect.height()))
    img = QImage(width, height, QImage.Format_ARGB32)
    img.fill(QColor(0xE8, 0xE8, 0xEA).rgb())
    p = QPainter(img)
    p.setRenderHint(QPainter.Antialiasing, True)
    p.setRenderHint(QPainter.SmoothPixmapTransform, True)
    p.setRenderHint(QPainter.TextAntialiasing, True)
    scene.render(p, QRectF(img.rect()), rect)
    p.end()
    out = os.path.join(SHOT_DIR, name)
    img.save(out)
    return out


def shot_window(w: MainWindow, name: str) -> str:
    """Grab the whole window (toolbar + canvas + inspector dock + status bar)."""
    out = os.path.join(SHOT_DIR, name)
    w.grab().save(out)
    return out


# ==========================================================================
# 1. Select -> Inspector populated; full restyle round-trips to the saved file
# ==========================================================================
def test_select_and_restyle(failures: list, shots: list) -> None:
    tag = "select_restyle"
    w = open_window()
    try:
        # --- empty-state screenshot (no document yet) is captured separately;
        #     here: select the title, the window populates the inspector. -----
        box = select(w, "Sample Report")
        if not check(failures, tag, box is not None, "could not select title box"):
            return
        insp = w.inspector
        check(failures, tag, insp._form_host.isVisible(),
              "inspector form not shown after selection")
        check(failures, tag, insp.family_combo.currentText().lower().startswith("helvetica"),
              f"inspector family {insp.family_combo.currentText()!r} != original Helvetica")
        check(failures, tag, abs(insp.size_spin.value() - 26.0) < 0.1,
              f"inspector size {insp.size_spin.value()} != original 26")
        check(failures, tag, w.act_delete.isEnabled(),
              "Delete action not enabled with a live selection")

        # Selected-box screenshot: handles + populated inspector visible.
        shots.append(shot_window(w, "02_selected_box.png"))

        # --- apply five distinct style changes, each ONE undo step ----------
        start_index = w.undo_stack.index()
        inspector_edit(w, "size", 40.0)
        inspector_edit(w, "font_family", "Georgia")
        inspector_edit(w, "color", (0.85, 0.10, 0.10))
        # mid-restyle screenshot: the title is now a clearly-restyled red
        # Georgia at 40pt with its overlay tracking the bigger box (the baked
        # WYSIWYG page re-rendered after each apply), before the bold/italic
        # toggles land -- so the restyle is unmistakable.
        shots.append(shot_window(w, "03_mid_restyle.png"))
        inspector_edit(w, "bold", True)
        inspector_edit(w, "italic", True)
        applied = w.undo_stack.index() - start_index
        check(failures, tag, applied == 5,
              f"5 inspector edits produced {applied} undo commands (want 5)")
        check(failures, tag, w.document.edit_count == 1,
              f"5 styles on one box -> edit_count {w.document.edit_count} (want 1)")

        # effective_style reflects every change (what the inspector re-reads).
        es = w.document.effective_style(0, w.view.current_selection())
        check(failures, tag, abs(es["size"] - 40.0) < 0.5, f"eff size {es['size']}")
        check(failures, tag, es["font_family"] == "Georgia", f"eff family {es['font_family']}")
        check(failures, tag, es["color"][0] > 0.5 and es["color"][1] < 0.4,
              f"eff color {es['color']} not red")
        check(failures, tag, es["bold"] and es["italic"], "eff bold/italic not set")

        # --- save through the window + reopen; the file reflects the restyle -
        out = os.path.join(tempfile.gettempdir(), "editui_restyle.pdf")
        check(failures, tag, w._do_save(out), "window save failed")

        basefonts = saved_basefonts(out)
        check(failures, tag, not new_base14(FORM, out),
              f"restyle INTRODUCED a base-14 substitute: {new_base14(FORM, out)}")
        check(failures, tag, any("georgia" in b for b in basefonts),
              f"saved page does not carry Georgia: {basefonts}")
        ss = saved_span(out, "Sample")
        check(failures, tag, ss is not None and has_words(out, "Sample", "Report"),
              "restyled title text not extractable from saved file")
        if ss:
            check(failures, tag, abs(ss["size"] - 40.0) < 2.0,
                  f"saved size {ss['size']:.1f} != 40")
            check(failures, tag, ss["color"][0] > 0.5 and ss["color"][1] < 0.4,
                  f"saved color {ss['color']} not red")

        # WYSIWYG: the on-screen baked page == the saved bytes.
        rel = wysiwyg_rel_diff(w.document, out)
        check(failures, tag, rel < 0.02,
              f"render_with_edits != saved (rel ink diff {rel:.4f}) -- WYSIWYG broke")

        # No-regression: the dark header band the title sits on survived the
        # restyle (non-destructive redaction kept the fill).
        r, g, b = region_mean_rgb(out, _HEADER_BAND)
        check(failures, tag, b > r and b > 80 and r < 120,
              f"header band fill lost after restyle (mean rgb {r:.0f},{g:.0f},{b:.0f})")

        # --- reopen the saved file in a FRESH model: edits are on disk -------
        re = PDFDocument(out)
        try:
            rspans = re.spans(0)
            title = next((s for s in rspans if "Sample" in s.text), None)
            check(failures, tag, title is not None, "reopened file lost the title span")
            if title:
                check(failures, tag, abs(title.size - 40.0) < 2.0,
                      f"reopened size {title.size:.1f} != 40")
        finally:
            re.close()

        # --- undo all the way back -> clean; redo -> restyled again ---------
        for _ in range(5):
            w.undo_stack.undo()
        _APP.processEvents()
        check(failures, tag, w.document.edit_count == 0,
              f"5 undos did not clear edits (edit_count {w.document.edit_count})")
        es0 = w.document.effective_style(0, select(w, "Sample Report"))
        check(failures, tag, abs(es0["size"] - 26.0) < 0.1,
              "undo did not restore original size in effective_style")
        for _ in range(5):
            w.undo_stack.redo()
        _APP.processEvents()
        check(failures, tag, w.document.edit_count == 1, "redo did not restore restyle")

        if not failures:
            print(f"  {tag:20} select->inspector populated; family+size+color+"
                  f"bold+italic each 1 undo step; saved+reopened; header band kept")
    except Exception:
        failures.append(f"{tag}: raised:\n{traceback.format_exc()}")
    finally:
        close_window(w)


# ==========================================================================
# 2. Add a new text box through the toolbar tool; it saves with real ink
# ==========================================================================
def test_add_box(failures: list, shots: list) -> None:
    tag = "add_box"
    w = open_window()
    try:
        # A new box is created from the inspector's CURRENT values; seed the
        # clean add-defaults (Helv 12pt black) so the box has visible ink.
        seed_add_style(w)
        start_index = w.undo_stack.index()

        nb = add_box_via_tool(w, (140.0, 470.0), "New Field 88")
        if not check(failures, tag, nb is not None, "add flow produced no new box"):
            return
        check(failures, tag, w.act_add_text.isChecked() is False,
              "Add Text tool stayed armed after one box (one click = one box)")
        check(failures, tag, len(w.document.new_boxes(0)) == 1,
              f"new_boxes count {len(w.document.new_boxes(0))} != 1")
        # add + type = two undo commands on the window stack.
        add_cmds = w.undo_stack.index() - start_index
        check(failures, tag, add_cmds == 2,
              f"add+type pushed {add_cmds} undo commands (want 2: add + text)")

        # screenshot: the freshly added box on the page.
        w.view.reload()
        _APP.processEvents()
        shots.append(shot_scene(w, "04_added_box.png"))

        out = os.path.join(tempfile.gettempdir(), "editui_add.pdf")
        check(failures, tag, w._do_save(out), "window save failed")
        check(failures, tag, has_words(out, "New", "Field", "88"),
              "added text not on saved page")
        check(failures, tag, region_ink(out, nb.bbox) > 30, "added box has no ink")
        check(failures, tag, not new_base14(FORM, out),
              f"added box INTRODUCED a base-14 builtin: {new_base14(FORM, out)}")

        rel = wysiwyg_rel_diff(w.document, out)
        check(failures, tag, rel < 0.02,
              f"add: render_with_edits != saved (rel diff {rel:.4f})")

        # reopen: the new text is on disk.
        check(failures, tag, "New Field 88" in page_text(out) or has_words(out, "New", "88"),
              "reopened file is missing the added text")

        # undo removes it entirely (text then add = 2 steps); redo restores it.
        w.undo_stack.undo()      # revert the typed text -> empty box remains
        w.undo_stack.undo()      # revert the add -> box gone
        _APP.processEvents()
        check(failures, tag, not w.document.new_boxes(0),
              "two undos did not remove the added box")
        out2 = os.path.join(tempfile.gettempdir(), "editui_add_undo.pdf")
        w._do_save(out2)
        check(failures, tag, not has_words(out2, "88"),
              "added text still present after undo")
        w.undo_stack.redo()      # re-add the box
        w.undo_stack.redo()      # re-apply the typed text
        _APP.processEvents()
        check(failures, tag, len(w.document.new_boxes(0)) == 1,
              "redo did not restore the added box")
        check(failures, tag, w.document.new_boxes(0)[0].text == "New Field 88",
              "redo did not restore the typed text")

        if not failures:
            print(f"  {tag:20} toolbar Add Text -> click -> type -> commit; saved "
                  f"with ink, embeddable font; tool auto-unchecks; undo/redo OK")
    except Exception:
        failures.append(f"{tag}: raised:\n{traceback.format_exc()}")
    finally:
        close_window(w)


# ==========================================================================
# 3. Move a box: saved ink shifts, original spot cleared
# ==========================================================================
def test_move_box(failures: list, shots: list) -> None:
    tag = "move_box"
    w = open_window()
    try:
        box = select(w, "Cleared")     # the overlap-merged box (3 member bboxes)
        if not check(failures, tag, box is not None, "move target not found"):
            return
        check(failures, tag, len(box.redact_rects) > 1,
              "expected the 'Cleared 4821' box to be overlap-merged (>1 bbox)")
        ox, oy = box.origin
        # A drag-commit: the canvas funnels (dx,dy) in PDF points to one command.
        box_command(w, "move", box, {"dx": 40.0, "dy": -16.0})
        check(failures, tag, w.document.edit_count == 1, "move did not stage 1 edit")

        w.view.reload(); _APP.processEvents()
        shots.append(shot_scene(w, "05_after_move.png"))

        out = os.path.join(tempfile.gettempdir(), "editui_move.pdf")
        check(failures, tag, w._do_save(out), "window save failed")
        moved = saved_span(out, "Cleared")
        check(failures, tag, moved is not None, "moved text missing from saved file")
        if moved:
            mx, my = moved["origin"]
            check(failures, tag, abs(mx - (ox + 40.0)) < 3.0,
                  f"moved x {mx:.1f} != {ox + 40.0:.1f}")
            check(failures, tag, abs(my - (oy - 16.0)) < 3.0,
                  f"moved y {my:.1f} != {oy - 16.0:.1f}")

        # No-regression (overlap-merge): NO residue at ANY member bbox of the
        # original merged box -- editing the box erased every duplicate run.
        residue = max(region_ink(out, bb) for bb in box.redact_rects)
        check(failures, tag, residue < 80,
              f"overlap-merged box left residue at original spot ({residue}px)")

        rel = wysiwyg_rel_diff(w.document, out)
        check(failures, tag, rel < 0.02,
              f"move: render_with_edits != saved (rel diff {rel:.4f})")

        w.undo_stack.undo(); _APP.processEvents()
        check(failures, tag, w.document.edit_count == 0, "move undo did not clear")
        w.undo_stack.redo(); _APP.processEvents()
        check(failures, tag, w.document.edit_count == 1, "move redo did not restore")

        if not failures:
            print(f"  {tag:20} drag-commit shifts saved ink ~(+40,-16); merged box "
                  f"left no residue at any of {len(box.redact_rects)} member bboxes")
    except Exception:
        failures.append(f"{tag}: raised:\n{traceback.format_exc()}")
    finally:
        close_window(w)


# ==========================================================================
# 4. Resize a box: saved font size scales
# ==========================================================================
def test_resize_box(failures: list, shots: list) -> None:
    tag = "resize_box"
    w = open_window()
    try:
        box = select(w, "$1,250")
        if not check(failures, tag, box is not None, "resize target not found"):
            return
        start = box.size
        box_command(w, "resize", box, {"scale": 2.0, "anchor": None})
        check(failures, tag, w.document.edit_count == 1, "resize did not stage 1 edit")
        es = w.document.effective_style(0, w.view.current_selection())
        check(failures, tag, abs(es["size"] - start * 2.0) < 0.5,
              f"effective size after 2x {es['size']:.1f} != {start * 2.0:.1f}")

        out = os.path.join(tempfile.gettempdir(), "editui_resize.pdf")
        check(failures, tag, w._do_save(out), "window save failed")
        rs = saved_span(out, "250")
        if rs:
            check(failures, tag, abs(rs["size"] - start * 2.0) < 2.0,
                  f"saved resized size {rs['size']:.1f} != ~{start * 2.0:.1f}")

        rel = wysiwyg_rel_diff(w.document, out)
        check(failures, tag, rel < 0.02,
              f"resize: render_with_edits != saved (rel diff {rel:.4f})")

        w.undo_stack.undo(); _APP.processEvents()
        es0 = w.document.effective_style(0, w.view.current_selection())
        check(failures, tag, abs(es0["size"] - start) < 0.5,
              f"resize undo did not restore size ({es0['size']:.1f} vs {start})")

        if not failures:
            print(f"  {tag:20} 2x resize doubles saved font size "
                  f"({start:.0f}->{start * 2:.0f}pt); undo restores")
    except Exception:
        failures.append(f"{tag}: raised:\n{traceback.format_exc()}")
    finally:
        close_window(w)


# ==========================================================================
# 5. Delete a box: ink gone, colored cell survives
# ==========================================================================
def test_delete_box(failures: list, shots: list) -> None:
    tag = "delete_box"
    w = open_window()
    try:
        box = select(w, "$1,250")
        if not check(failures, tag, box is not None, "delete target not found"):
            return
        # The light-blue label cell behind the value must survive the delete
        # (non-destructive redaction). Measure its color before.
        before_rgb = region_mean_rgb(FORM, _LABEL_CELL_AMOUNT)

        w.view.delete_selected()
        _APP.processEvents()
        check(failures, tag, w.document.edit_count == 1, "delete did not stage 1 edit")
        check(failures, tag, w.view.current_selection() is None,
              "selection not cleared after delete")
        check(failures, tag, not w.act_delete.isEnabled(),
              "Delete action still enabled with no selection")

        w.view.reload(); _APP.processEvents()
        shots.append(shot_scene(w, "06_after_delete.png"))

        out = os.path.join(tempfile.gettempdir(), "editui_delete.pdf")
        check(failures, tag, w._do_save(out), "window save failed")
        after_ink = max(region_ink(out, bb) for bb in box.redact_rects)
        check(failures, tag, after_ink < 120,
              f"deleted region still has ink ({after_ink}px)")
        check(failures, tag, "$1,250" not in page_text(out),
              "deleted text still extractable from saved page")

        # No-regression: the colored cell fill is still there (non-destructive).
        after_rgb = region_mean_rgb(out, _LABEL_CELL_AMOUNT)
        drift = max(abs(a - b) for a, b in zip(before_rgb, after_rgb))
        check(failures, tag, drift < 25,
              f"colored cell fill changed after delete (rgb {before_rgb} -> "
              f"{after_rgb}) -- non-destructive redaction failed")

        rel = wysiwyg_rel_diff(w.document, out)
        check(failures, tag, rel < 0.02,
              f"delete: render_with_edits != saved (rel diff {rel:.4f})")

        w.undo_stack.undo(); _APP.processEvents()
        out2 = os.path.join(tempfile.gettempdir(), "editui_delete_undo.pdf")
        w._do_save(out2)
        check(failures, tag, "$1,250" in page_text(out2) or region_ink(out2, box.bbox) > 50,
              "delete undo did not restore the box")

        if not failures:
            print(f"  {tag:20} delete clears ink + text + selection; colored cell "
                  f"fill survived; undo restores")
    except Exception:
        failures.append(f"{tag}: raised:\n{traceback.format_exc()}")
    finally:
        close_window(w)


# ==========================================================================
# 6. Mixed sequence on ONE window stack: text->style->move->delete->add,
#    full undo -> pristine, full redo -> final, then save + reopen.
# ==========================================================================
def test_mixed_window_history(failures: list, shots: list) -> None:
    tag = "mixed_history"
    w = open_window()
    try:
        orig_text = page_text(FORM)

        # text edit (through the inline editor + commit, the real path)
        h = hotspot_for(w, "Pending")
        w.view.begin_edit(h)
        w.view._editor.setPlainText("Approved now")
        w.view.commit_edit(); _APP.processEvents()

        # style (select Field A, bold + blue via inspector)
        select(w, "Field A")
        inspector_edit(w, "color", (0.0, 0.0, 1.0))
        inspector_edit(w, "bold", True)

        # move (Date label)
        bmove = select(w, "Date")
        box_command(w, "move", bmove, {"dx": 12.0, "dy": 6.0})

        # delete (Issued date value)
        select(w, "10/11/1995")
        w.view.delete_selected(); _APP.processEvents()

        # add (new box via the toolbar tool, on the clean add-defaults so the
        # text is visible black). add+type = 2 commands.
        seed_add_style(w)
        add_box_via_tool(w, (330.0, 540.0), "Extra 99")

        n_cmds = w.undo_stack.index()
        # 1 text + 2 styles (color, bold) + 1 move + 1 delete + (add + text) = 7.
        check(failures, tag, n_cmds == 7,
              f"mixed sequence pushed {n_cmds} undo commands (want 7)")
        check(failures, tag, w.document.edit_count == 5,
              f"edit_count {w.document.edit_count} != 5 (4 span edits + 1 new box)")

        # final save reflects every kind
        final = os.path.join(tempfile.gettempdir(), "editui_mixed_final.pdf")
        check(failures, tag, w._do_save(final), "final save failed")
        ft = page_text(final)
        check(failures, tag, has_words(final, "Approved", "now"), "final text edit missing")
        check(failures, tag, has_words(final, "Extra", "99"), "final add missing")
        check(failures, tag, "10/11/1995" not in ft, "final delete not applied")

        # full UNDO -> pristine (text identical to original)
        for _ in range(n_cmds):
            w.undo_stack.undo()
        _APP.processEvents()
        check(failures, tag, w.document.edit_count == 0,
              f"edit_count {w.document.edit_count} != 0 after full undo")
        check(failures, tag, not w.document.new_boxes(0),
              "new box survived full undo")
        pristine = os.path.join(tempfile.gettempdir(), "editui_mixed_pristine.pdf")
        w._do_save(pristine)
        check(failures, tag, page_text(pristine).split() == orig_text.split(),
              "full undo did not restore the original text")

        # full REDO -> final state again
        for _ in range(n_cmds):
            w.undo_stack.redo()
        _APP.processEvents()
        check(failures, tag, w.document.edit_count == 5,
              f"edit_count {w.document.edit_count} != 5 after full redo")
        final2 = os.path.join(tempfile.gettempdir(), "editui_mixed_final2.pdf")
        w._do_save(final2)
        check(failures, tag,
              has_words(final2, "Approved", "Extra", "99")
              and "10/11/1995" not in page_text(final2),
              "full redo did not reproduce the final state")

        # reopen the final file: the edits are durably on disk.
        re = PDFDocument(final2)
        try:
            rt = re.doc[0].get_text()
            check(failures, tag, "Approved" in rt and "Extra" in rt
                  and "10/11/1995" not in rt,
                  "reopened final file does not match the edited state")
        finally:
            re.close()

        if not failures:
            print(f"  {tag:20} text+style+move+delete+add on ONE window stack; "
                  f"{n_cmds}x undo->pristine, {n_cmds}x redo->final; reopened OK")
    except Exception:
        failures.append(f"{tag}: raised:\n{traceback.format_exc()}")
    finally:
        close_window(w)


# ==========================================================================
# Empty-state screenshot (a fresh window, no document)
# ==========================================================================
def capture_empty_state(shots: list) -> None:
    w = MainWindow()
    w.resize(1180, 920)
    w.show()
    # Stub the Recent list with SYNTHETIC paths before rendering: the live
    # list reads this machine's real app-opened PDFs (QSettings), and real
    # document names must never land in a committed screenshot (fixture
    # policy: synthetic, neutral names only). Same discipline as
    # tests/test_app.py's empty-state shot. The rows render their FOLDER as
    # a second line (navigation M3), so the stubs are display-only
    # home-relative paths -- neutral folders, deterministic across machines
    # (no repo/worktree path baked into the PNG).
    w.empty_state.set_recents([
        os.path.expanduser(p)
        for p in ("~/Documents/Forms/onboarding-checklist.pdf",
                  "~/Documents/Forms/benefits-summary.pdf",
                  "~/Documents/Archive/onboarding-checklist.pdf")
    ])
    # set_recents deleteLater()s the previous rows; flush the deferred
    # deletes BEFORE rendering or the real-recents labels paint underneath
    # the synthetic ones.
    _APP.processEvents()
    _APP.sendPostedEvents(None, QEvent.DeferredDelete)
    _APP.processEvents()
    shots.append(shot_window(w, "01_empty_state.png"))
    close_window(w)


# ==========================================================================
# Main
# ==========================================================================
def main() -> int:
    print("FULL editor verification, driven through the REAL MainWindow "
          "(EDITOR_SPEC §8)\n")
    failures: list = []
    shots: list = []

    capture_empty_state(shots)
    for fn in (test_select_and_restyle, test_add_box, test_move_box,
               test_resize_box, test_delete_box, test_mixed_window_history):
        fn(failures, shots)

    print("\n" + "=" * 70)
    print("Screenshots rendered:")
    for p in sorted(set(shots)):
        flag = "" if os.path.exists(p) else "  (MISSING)"
        print(f"  {p}{flag}")
    print()

    if failures:
        print(f"FAILED ({len(failures)} assertion failure(s)):")
        for f in failures:
            print(f"  - {f}")
        return 1

    print("PASSED -- the real window selects boxes and populates the Inspector; "
          "font family/size/color/bold/italic, add, move, resize, and delete all "
          "drive through the QUndoStack and land in the saved (and reopened) file; "
          "WYSIWYG holds for every kind; overlap-merged boxes leave no residue; "
          "colored cells/bands survive. No exceptions.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
