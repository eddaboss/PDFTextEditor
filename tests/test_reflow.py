"""Headless verification for the REFLOW TEXT MODEL (REFLOW_SPEC §R1 / §R2 / §R5.2).

Covers the smart-paragraph-grouping + auto-reflow build's MODEL layer:

  1. GROUPING correctness on paragraphs.pdf (target body -> ONE box; heading,
     table cells, bullets, note stay separate) and form_like.pdf (ZERO
     ParagraphBoxes) and body_paragraphs.pdf (3 paragraphs of 4 lines, justify
     inferred on the flush-both-edges one).
  2. WRAP-ENGINE purity: wrap_paragraph matches a hand-computed greedy fill;
     widths additive; bbox is the column x ascent..descent.
  3. WYSIWYG for WRAPPED text (the keystone, mirrors test_editor_model's
     wysiwyg_match): a paragraph text edit / each alignment / a line-spacing
     change all render the SAME pixels as the saved file (rel ink diff < 0.02).
  4. NO-TRACE round-trip: swapping one word for a same-width word leaves the
     tail lines' baselines + breaks unchanged; an UNCHANGED re-wrap reproduces
     the original line count.
  5. REFLOW reflows: longer text -> MORE lines, shorter -> FEWER, no overflow.
  6. COMPOSITION with overlap-merge + non-destructive redaction: a paragraph
     edit redacts every member bbox (flattened union) and the heading / table /
     bullets / note all survive intact.
  7. ALIGNMENT + LINE-SPACING actually change the layout (x-origins / baselines).
  8. UNDO/REDO across a paragraph reflow interleaved with align/move/spacing on
     one history; delete + undo restores the paragraph.

Run:
    QT_QPA_PLATFORM=offscreen \
      /Users/edward/Documents/GitHub/PDFTextEditor/.venv/bin/python \
      tests/test_reflow.py
"""

from __future__ import annotations

import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import fitz  # noqa: E402
from PySide6.QtCore import QEvent, QPointF, Qt  # noqa: E402
from PySide6.QtGui import QMouseEvent  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

_APP = QApplication.instance() or QApplication([])

from pdftexteditor.document import PDFDocument, ParagraphBox  # noqa: E402
from pdftexteditor.reflow import wrap_paragraph  # noqa: E402
from pdftexteditor.ui.main_window import MainWindow  # noqa: E402

FIXTURE_DIR = os.path.join(_HERE, "fixtures")
PARAGRAPHS = os.path.join(FIXTURE_DIR, "paragraphs.pdf")
FORM = os.path.join(FIXTURE_DIR, "form_like.pdf")
BODY = os.path.join(FIXTURE_DIR, "body_paragraphs.pdf")


def check(failures, tag, cond, msg):
    if not cond:
        failures.append(f"{tag}: {msg}")
    return cond


def _ctr():
    i = 0
    while True:
        yield i
        i += 1


_CTR = _ctr()


def save(doc):
    out = os.path.join(tempfile.gettempdir(), f"reflow_{next(_CTR)}.pdf")
    doc.save_as(out)
    return out


def _pix_ink(pix):
    s, step, n = pix.samples, pix.n, 0
    for i in range(0, len(s), step):
        r, g, b = s[i], s[i + 1], s[i + 2]
        if (0.299 * r + 0.587 * g + 0.114 * b) < 230:
            n += 1
    return n


def wysiwyg_match(doc, out_path, scale=2.0):
    """Whole-page ink of render_with_edits vs the saved file -- the baked
    WYSIWYG invariant for wrapped text (§R0.1)."""
    rink = _pix_ink(doc.render_with_edits(0, scale))
    sd = fitz.open(out_path)
    try:
        spix = sd[0].get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
    finally:
        sd.close()
    sink = _pix_ink(spix)
    return abs(rink - sink) / max(sink, 1)


def region_ink(path, bbox, scale=3.0):
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


def page_words(path):
    doc = fitz.open(path)
    try:
        return {w[4] for w in doc[0].get_text("words")}
    finally:
        doc.close()


def paragraphs_of(doc):
    return [b for b in doc.spans(0) if isinstance(b, ParagraphBox)]


def the_para(doc, contains):
    for b in paragraphs_of(doc):
        if contains in b.text:
            return b
    return None


def resolved_font(doc, para):
    rf = doc.font_engine.resolve(0, para.font, para.flags, para.text)
    return doc.font_engine.fitz_font_for(rf)


# --------------------------------------------------------------------------
# 1. Grouping correctness
# --------------------------------------------------------------------------
def test_grouping(failures):
    tag = "grouping"

    # paragraphs.pdf: the target body paragraph -> ONE box of 5 lines.
    doc = PDFDocument(PARAGRAPHS)
    boxes = doc.spans(0)
    paras = [b for b in boxes if isinstance(b, ParagraphBox)]
    target = the_para(doc, "quarterly operations")
    check(failures, tag, target is not None and len(target.members) == 5,
          f"target body paragraph not grouped into one 5-line box "
          f"(got {None if target is None else len(target.members)})")

    # The heading, the 8 table cells, the 8 bullet spans must NOT be swallowed.
    others = [b for b in boxes if not isinstance(b, ParagraphBox)]
    has_heading = any(round(b.size) == 20 and "Operations" in b.text.replace(" ", " ")
                      for b in others)
    check(failures, tag, has_heading, "20pt heading was swallowed into a paragraph")
    # The 2-column table (4 rows, label at x0=72 + value at x0=250) sits in the
    # y-band ~257..318. All 8 cells must stay distinct Spans (never grouped).
    table_cells = [b for b in others if 255 <= b.bbox[1] <= 320]
    check(failures, tag, len(table_cells) == 8,
          f"table cells were merged ({len(table_cells)} stayed, want 8 distinct)")
    bullet_texts = [b for b in others if "Confirm" in b.text.replace(" ", " ")
                    or "Submit" in b.text.replace(" ", " ")
                    or "Flag" in b.text.replace(" ", " ")
                    or "Reserve" in b.text.replace(" ", " ")]
    check(failures, tag, len(bullet_texts) == 4,
          f"bullet items did not stay separate (got {len(bullet_texts)} of 4)")
    # The note is its own 2-line paragraph (a real paragraph), distinct from body.
    note = the_para(doc, "parking")
    check(failures, tag, note is not None and len(note.members) == 2
          and note is not target,
          "the note did not stay a distinct 2-line paragraph")
    doc.close()

    # form_like.pdf: ZERO ParagraphBoxes; the two-font Field B lines never merge.
    docf = PDFDocument(FORM)
    fparas = paragraphs_of(docf)
    check(failures, tag, len(fparas) == 0,
          f"form_like.pdf produced {len(fparas)} ParagraphBoxes (want 0)")
    docf.close()

    # body_paragraphs.pdf: 3 paragraphs of 4 lines; justify inferred once.
    docb = PDFDocument(BODY)
    bparas = paragraphs_of(docb)
    check(failures, tag, len(bparas) == 3,
          f"body_paragraphs.pdf produced {len(bparas)} ParagraphBoxes (want 3)")
    check(failures, tag, all(len(p.members) == 4 for p in bparas),
          f"body paragraphs not all 4 lines: {[len(p.members) for p in bparas]}")
    check(failures, tag, any(p.alignment == "justify" for p in bparas),
          "no justify-aligned paragraph inferred on the flush-both-edges block")
    docb.close()

    if not failures:
        print(f"  {tag:18} paragraphs.pdf->1 body box(5)+1 note(2), 8 cells + "
              f"4 bullets + heading kept; form_like->0; body->3x4 (justify ok)")


# --------------------------------------------------------------------------
# 2. Wrap-engine purity
# --------------------------------------------------------------------------
def test_wrap_purity(failures):
    tag = "wrap_purity"
    font = fitz.Font("helv")
    size = 12.0
    text = "alpha beta gamma delta epsilon zeta eta theta iota kappa lambda mu"
    col = 90.0
    res = wrap_paragraph(text, font, size, 100.0, 200.0, col)

    # Hand-computed greedy with the SAME slack the engine applies (1 space-width).
    sw = font.text_length(" ", fontsize=size)
    fit = col + 1.0 * sw
    toks = text.split()
    lines, cur, adv = [], [], 0.0
    for t in toks:
        tw = font.text_length(t, fontsize=size)
        need = (sw if cur else 0.0) + tw
        if cur and adv + need > fit:
            lines.append(" ".join(cur)); cur = [t]; adv = tw
        else:
            cur.append(t); adv += need
    if cur:
        lines.append(" ".join(cur))
    check(failures, tag, [l.text for l in res.lines] == lines,
          f"engine breaks {[l.text for l in res.lines]} != hand greedy {lines}")

    # Widths additive: each line's reported width == sum of its glyph advances.
    for l in res.lines:
        manual = font.text_length(l.text, fontsize=size)
        check(failures, tag, abs(l.width - manual) < 0.01,
              f"line width {l.width} != measured {manual} for {l.text!r}")

    # bbox: column horizontally, ascent..descent vertically.
    asc = font.ascender * size
    desc = -font.descender * size
    n = len(res.lines)
    exp_bbox = (100.0, 200.0 - asc, 100.0 + col,
                200.0 + (n - 1) * res.leading + desc)
    check(failures, tag, all(abs(a - b) < 0.01 for a, b in zip(res.bbox, exp_bbox)),
          f"bbox {res.bbox} != expected {exp_bbox}")

    # No line (non-oversized) exceeds the column + slack.
    over = [l for l in res.lines if l.width > col + sw + 0.5]
    check(failures, tag, not over, f"{len(over)} lines overflow the column")

    if not failures:
        print(f"  {tag:18} greedy fill matches hand-computed; widths additive; "
              f"bbox = column x ascent..descent")


# --------------------------------------------------------------------------
# 3. WYSIWYG for WRAPPED text -- the keystone
# --------------------------------------------------------------------------
def test_wysiwyg_wrapped(failures):
    tag = "wysiwyg_wrap"
    base = the_para(PDFDocument(PARAGRAPHS), "quarterly operations").text
    grow = base + " " + " ".join(["supplementary"] * 12)
    shrink = "A single short replacement line."
    worst = 0.0

    cases = [
        ("grow", grow, None, None),
        ("shrink", shrink, None, None),
        ("left", base + " edit", "left", None),
        ("center", base + " edit", "center", None),
        ("right", base + " edit", "right", None),
        ("justify", base + " edit", "justify", None),
        ("spacing", base + " edit", None, 1.6),
    ]
    for name, text, align, spacing in cases:
        doc = PDFDocument(PARAGRAPHS)
        para = the_para(doc, "quarterly operations")
        doc.stage_edit(0, para, text)
        if align is not None or spacing is not None:
            doc.set_style(0, para, alignment=align, line_spacing=spacing)
        out = save(doc)
        d = wysiwyg_match(doc, out)
        worst = max(worst, d)
        check(failures, tag, d < 0.02,
              f"{name}: render_with_edits != saved (rel ink diff {d:.4f})")
        doc.close()

    if not failures:
        print(f"  {tag:18} grow/shrink + left/center/right/justify + spacing: "
              f"on-screen == saved (worst rel ink diff {worst:.4f})")


# --------------------------------------------------------------------------
# 4. No-trace round-trip
# --------------------------------------------------------------------------
def test_no_trace(failures):
    tag = "no_trace"
    doc = PDFDocument(PARAGRAPHS)
    para = the_para(doc, "quarterly operations")
    font = resolved_font(doc, para)
    col = para.bbox[2] - para.bbox[0]

    def wrap(text):
        return wrap_paragraph(text, font, para.size, para.bbox[0],
                              para.origin[1], col, alignment=para.alignment,
                              leading=para.leading)

    # Unchanged re-wrap reproduces the original 5 lines.
    res = wrap(para.text)
    check(failures, tag, len(res.lines) == 5,
          f"unchanged re-wrap produced {len(res.lines)} lines (want 5 -- "
          f"the original break count)")

    # Swap a word for a same-length word -> the TAIL lines are byte-identical.
    orig = wrap(para.text)
    swapped = wrap(para.text.replace("nine", "five"))
    tail_ok = all(o.text == s.text and abs(o.origin[1] - s.origin[1]) < 0.01
                  for o, s in zip(orig.lines[2:], swapped.lines[2:]))
    check(failures, tag, tail_ok,
          "swapping one word changed the untouched tail lines' breaks/baselines")
    doc.close()
    if not failures:
        print(f"  {tag:18} unchanged re-wrap -> original 5 breaks; one-word swap "
              f"leaves the tail lines unchanged (zero trace)")


# --------------------------------------------------------------------------
# 5. Reflow reflows (line count tracks length; no overflow)
# --------------------------------------------------------------------------
def test_reflow_reflows(failures):
    tag = "reflows"
    doc = PDFDocument(PARAGRAPHS)
    para = the_para(doc, "quarterly operations")
    col = para.bbox[2] - para.bbox[0]

    def lines_for(text):
        d = PDFDocument(PARAGRAPHS)
        p = the_para(d, "quarterly operations")
        d.stage_edit(0, p, text)
        edit = d._edits[d._span_edit_key(0, p)]
        res = PDFDocument._wrap_for_edit(d.font_engine, 0, p, edit)
        n = len(res.lines)
        maxw = max((l.width for l in res.lines), default=0.0)
        d.close()
        return n, maxw

    base = para.text
    short = "Short."
    longer = base + " " + " ".join(["expansion"] * 25)
    n_edit, w_edit = lines_for(base + " trailing addition")
    n_short, w_short = lines_for(short)
    n_long, w_long = lines_for(longer)
    doc.close()

    check(failures, tag, n_short < n_edit < n_long,
          f"line count not monotone with length: short={n_short} "
          f"edit={n_edit} long={n_long}")
    sw = 4.0  # generous space-width allowance for the column+slack overflow test
    for nm, w in (("edit", w_edit), ("short", w_short), ("long", w_long)):
        check(failures, tag, w <= col + sw,
              f"{nm} overflows column: line width {w:.1f} > {col + sw:.1f}")
    if not failures:
        print(f"  {tag:18} short={n_short} < edited={n_edit} < longer={n_long} "
              f"lines; no line overflows column {col:.0f}pt")


# --------------------------------------------------------------------------
# 6. Composition with overlap-merge + non-destructive redaction
# --------------------------------------------------------------------------
def test_composition_redaction(failures):
    tag = "composition"
    doc = PDFDocument(PARAGRAPHS)
    para = the_para(doc, "quarterly operations")

    # redact_rects == flattened union of every member's redact_rects.
    member_rects = [bb for m in para.members for bb in m.redact_rects]
    check(failures, tag, len(para.redact_rects) == len(member_rects)
          and len(para.redact_rects) == 5,
          f"paragraph redact_rects ({len(para.redact_rects)}) != flattened "
          f"members ({len(member_rects)})")

    # baseline ink of must-survive elements.
    boxes = doc.spans(0)
    heading = next(b for b in boxes if not isinstance(b, ParagraphBox)
                   and round(b.size) == 20)
    cell = next(b for b in boxes if not isinstance(b, ParagraphBox)
                and "Meeting" in b.text.replace(" ", " "))
    bullet = next(b for b in boxes if not isinstance(b, ParagraphBox)
                  and "Confirm" in b.text.replace(" ", " "))
    note = the_para(doc, "parking")
    base = {name: region_ink(PARAGRAPHS, box.bbox) for name, box in
            (("heading", heading), ("cell", cell), ("bullet", bullet),
             ("note", note))}

    doc.stage_edit(0, para, "A wholly different and longer replacement paragraph "
                            "that wraps to several lines in the column.")
    out = save(doc)
    # original member ink gone (the word 'quarterly' no longer extractable).
    check(failures, tag, "quarterly" not in page_words(out),
          "original paragraph text survived the redaction")
    # neighbors intact (non-destructive redaction held).
    for name, box in (("heading", heading), ("cell", cell), ("bullet", bullet),
                      ("note", note)):
        after = region_ink(out, box.bbox)
        check(failures, tag, abs(after - base[name]) < base[name] * 0.20 + 40,
              f"{name} ink changed {base[name]}->{after} (redaction not "
              f"non-destructive?)")
    doc.close()
    if not failures:
        print(f"  {tag:18} para edit redacts 5 member bboxes (flattened union); "
              f"heading/table/bullet/note all survive")


# --------------------------------------------------------------------------
# 7. Alignment + line-spacing change the layout
# --------------------------------------------------------------------------
def test_align_spacing_layout(failures):
    tag = "align_spacing"
    base = the_para(PDFDocument(PARAGRAPHS), "quarterly operations").text

    def wrap_with(align=None, spacing=None):
        d = PDFDocument(PARAGRAPHS)
        p = the_para(d, "quarterly operations")
        d.stage_edit(0, p, base + " x")
        if align is not None or spacing is not None:
            d.set_style(0, p, alignment=align, line_spacing=spacing)
        edit = d._edits[d._span_edit_key(0, p)]
        res = PDFDocument._wrap_for_edit(d.font_engine, 0, p, edit)
        d.close()
        return res

    left = wrap_with(align="left")
    center = wrap_with(align="center")
    right = wrap_with(align="right")
    justify = wrap_with(align="justify")

    left_x = [round(l.origin[0], 1) for l in left.lines]
    center_x = [round(l.origin[0], 1) for l in center.lines]
    right_x = [round(l.origin[0], 1) for l in right.lines]
    check(failures, tag, all(x == left_x[0] for x in left_x),
          f"left alignment x-origins not flush: {left_x}")
    check(failures, tag, any(x != left_x[0] for x in center_x),
          "center alignment did not shift any line right")
    check(failures, tag, any(x > left_x[0] for x in right_x),
          "right alignment did not push lines right")
    # justify draws non-last lines per-word.
    just_words = sum(1 for l in justify.lines[:-1] if l.word_origins)
    check(failures, tag, just_words >= 1,
          "justify produced no per-word (stretched) line")

    # line-spacing scales the baseline delta.
    s10 = wrap_with(spacing=None)
    s20 = wrap_with(spacing=2.0)
    d10 = s10.lines[1].origin[1] - s10.lines[0].origin[1]
    d20 = s20.lines[1].origin[1] - s20.lines[0].origin[1]
    check(failures, tag, abs(d20 - 2.0 * d10) < 0.5,
          f"line-spacing 2x did not double the baseline delta ({d10:.1f}->{d20:.1f})")
    if not failures:
        print(f"  {tag:18} left flush; center/right shift; justify per-word; "
              f"spacing 2x doubles leading ({d10:.0f}->{d20:.0f}pt)")


# --------------------------------------------------------------------------
# 8. Undo/redo across a paragraph reflow, interleaved
# --------------------------------------------------------------------------
def test_undo_redo(failures):
    tag = "undo_redo"
    doc = PDFDocument(PARAGRAPHS)
    para = the_para(doc, "quarterly operations")
    orig_words = page_words(PARAGRAPHS)

    doc.stage_edit(0, para, para.text + " appended clause.")
    doc.set_style(0, para, alignment="right")
    doc.move_box(0, para, 6.0, -4.0)
    doc.set_style(0, para, line_spacing=1.5)
    check(failures, tag, doc.edit_count == 1,
          f"edit_count {doc.edit_count} != 1 (one box, several staged changes)")
    out = save(doc)
    d = wysiwyg_match(doc, out)
    check(failures, tag, d < 0.02, f"interleaved reflow WYSIWYG broke ({d:.4f})")
    check(failures, tag, "appended" in page_words(out), "reflow text not saved")

    n = 0
    while doc.can_undo:
        doc.undo()
        n += 1
    check(failures, tag, n == 4 and doc.edit_count == 0,
          f"undo did not unwind 4 steps to pristine (n={n}, "
          f"edit_count={doc.edit_count})")
    out_pristine = save(doc)
    check(failures, tag, "quarterly" in page_words(out_pristine),
          "full undo did not restore the original paragraph")
    while doc.can_redo:
        doc.redo()
    check(failures, tag, doc.edit_count == 1, "redo did not restore the edit")
    doc.close()

    # delete a paragraph + undo restores it.
    doc2 = PDFDocument(PARAGRAPHS)
    p2 = the_para(doc2, "quarterly operations")
    doc2.delete_box(0, p2)
    out_del = save(doc2)
    check(failures, tag, "quarterly" not in page_words(out_del),
          "paragraph delete did not remove the text")
    doc2.undo()
    out_undel = save(doc2)
    check(failures, tag, "quarterly" in page_words(out_undel),
          "paragraph delete undo did not restore the text")
    doc2.close()
    if not failures:
        print(f"  {tag:18} text+align+move+spacing on one box (1 edit); 4x undo "
              f"-> pristine, redo restores; delete+undo round-trips")


# ==========================================================================
# 9. CLICK-TO-EDIT GESTURE through the REAL MainWindow + real mouse events
#    (REFLOW_SPEC click-to-edit: single click SELECTS the box; a SECOND click
#    -- or a double-click -- starts TEXT EDITING with the caret where clicked).
# ==========================================================================
def _pump(n: int = 5) -> None:
    for _ in range(n):
        _APP.processEvents()


def _open_window(path: str) -> MainWindow:
    w = MainWindow()
    w.resize(1300, 950)
    w.show()
    w.open_path(path)
    _pump(6)
    if getattr(w, "empty_state", None) is not None:
        w.empty_state.hide()
    _pump(3)
    return w


def _first_paragraph_hotspot(view):
    """Materialize page 0 and return its first ParagraphBox + the SpanHotspot
    item the user would click on."""
    layer = view._layers[0]
    if not layer.rendered:
        view._materialize_page(layer)
    _pump(2)
    paras = [b for b in layer.boxes if getattr(b, "is_paragraph", False)]
    if not paras:
        return None, None
    box = paras[0]
    return box, view._hotspot_for(box)


def _click_at_scene(view, scene_pt: QPointF, *, double: bool = False) -> None:
    """Dispatch a real left press/release (and, if requested, the follow-up
    double-click event) to the viewport at a scene point, exactly as a mouse
    would. Goes through the live SpanHotspot -> _on_box_press/-release routing."""
    vp = view.viewport()
    view_pt = view.mapFromScene(scene_pt)
    gp = view.viewport().mapToGlobal(view_pt)
    pos = QPointF(view_pt)
    glob = QPointF(gp)
    press = QMouseEvent(QEvent.MouseButtonPress, pos, glob,
                        Qt.LeftButton, Qt.LeftButton, Qt.NoModifier)
    release = QMouseEvent(QEvent.MouseButtonRelease, pos, glob,
                          Qt.LeftButton, Qt.NoButton, Qt.NoModifier)
    _APP.sendEvent(vp, press)
    _APP.sendEvent(vp, release)
    if double:
        dbl = QMouseEvent(QEvent.MouseButtonDblClick, pos, glob,
                          Qt.LeftButton, Qt.LeftButton, Qt.NoModifier)
        _APP.sendEvent(vp, dbl)
        rel2 = QMouseEvent(QEvent.MouseButtonRelease, pos, glob,
                           Qt.LeftButton, Qt.NoButton, Qt.NoModifier)
        _APP.sendEvent(vp, rel2)
    _pump(3)


def _editor_active(view) -> bool:
    return getattr(view, "_editor", None) is not None


def test_click_to_edit_gesture(failures):
    tag = "click_to_edit"
    w = _open_window(PARAGRAPHS)
    view = w.view
    box, hotspot = _first_paragraph_hotspot(view)
    if not check(failures, tag, box is not None and hotspot is not None,
                 "no ParagraphBox/hotspot on page 0 to drive the gesture"):
        w._suppress_close_guard = True
        w.close()
        return

    # A point comfortably inside the paragraph's first line, in SCENE coords.
    r = hotspot.sceneBoundingRect()
    inside = QPointF(r.left() + r.width() * 0.35, r.top() + r.height() * 0.30)

    # --- single click: SELECTS, does NOT enter edit ---
    _click_at_scene(view, inside)
    sel = view.current_selection()
    check(failures, tag, sel is not None
          and getattr(sel, "identity", None) == box.identity,
          "single click did not SELECT the paragraph box")
    check(failures, tag, not _editor_active(view),
          "single click wrongly started TEXT EDITING (should only select)")

    # --- second click on the ALREADY-selected box: starts TEXT EDITING ---
    _click_at_scene(view, inside)
    check(failures, tag, _editor_active(view),
          "second click on the selected box did not start text editing")
    # The caret landed near the click (not pinned to index 0): with a click at
    # ~35% across the first line, a non-trivial caret index is expected.
    editor = view._editor
    caret = editor.textCursor().position() if editor is not None else -1
    check(failures, tag, caret > 0,
          f"second-click edit did not place the caret at the click "
          f"(caret index = {caret})")
    # Esc cancels the editor cleanly, leaving the box selected.
    view._cancel_editor_silent()
    _pump(2)
    check(failures, tag, not _editor_active(view),
          "editor did not tear down after cancel")

    # --- double-click also enters edit (the classic gesture still works) ---
    # Re-fetch the hotspot (a cancel may rebuild the scene items).
    box2, hotspot2 = _first_paragraph_hotspot(view)
    r2 = hotspot2.sceneBoundingRect()
    dpt = QPointF(r2.left() + r2.width() * 0.5, r2.top() + r2.height() * 0.4)
    # ensure it's selected first (double-click selects-then-edits regardless)
    view.select_box(box2)
    _pump(2)
    _click_at_scene(view, dpt, double=True)
    check(failures, tag, _editor_active(view),
          "double-click did not start text editing")
    view._cancel_editor_silent()
    _pump(2)

    w._suppress_close_guard = True
    w.close()
    _pump(2)
    if not failures:
        print(f"  {tag:18} single click selects (no editor); 2nd click + "
              f"double-click both enter edit with the caret at the click")


def main():
    print("Reflow TEXT MODEL verification (REFLOW_SPEC §R1/§R2/§R5.2)\n")
    failures = []
    for fn in (test_grouping, test_wrap_purity, test_wysiwyg_wrapped,
               test_no_trace, test_reflow_reflows, test_composition_redaction,
               test_align_spacing_layout, test_undo_redo,
               test_click_to_edit_gesture):
        fn(failures)
    print("\n" + "=" * 66)
    if failures:
        print(f"FAILED ({len(failures)}):")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("PASSED -- paragraph grouping composes with overlap-merge (target body "
          "grouped, fields/table/bullets/heading kept); the wrap engine is the "
          "single source of truth (WYSIWYG for wrapped text < 0.02, no-trace "
          "re-wrap); reflow reflows; alignment + line-spacing layout; "
          "non-destructive redaction + undo/redo all held.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
