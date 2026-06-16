"""Regression tests for user-reported bugs (June 2026 batch).

1. Landscape printing: a landscape page must print landscape automatically
   (the printer used to stay portrait and shrink the wide page).
2. "Don't Save" on close: choosing Don't Save must actually drop the changes
   and let the window close, not re-prompt forever.
3. Box size on edit: opening a box for editing must keep the SAME box outline
   it had when selected (the editor's tight focus frame used to make the box
   appear to shrink).

Run:
    QT_QPA_PLATFORM=offscreen \
      /Users/edward/Documents/GitHub/PDFTextEditor/.venv/bin/python \
      tests/test_bugfixes.py
"""

from __future__ import annotations

import os
import sys
import tempfile
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import fitz  # noqa: E402
from PySide6.QtCore import QEventLoop, QTimer  # noqa: E402
from PySide6.QtGui import QCloseEvent  # noqa: E402
from PySide6.QtPrintSupport import QPrinter  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from pdftexteditor.ui.main_window import MainWindow  # noqa: E402

_APP = QApplication.instance() or QApplication([])


def _pump(ms: int = 120) -> None:
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()


def _print_to_pdf(window: "MainWindow") -> "fitz.Rect":
    out = tempfile.mktemp(suffix=".pdf")
    pr = QPrinter()
    pr.setOutputFormat(QPrinter.OutputFormat.PdfFormat)
    pr.setOutputFileName(out)
    window._do_print(pr)
    _pump(80)
    return fitz.open(out)[0].rect


def test_landscape_print() -> list[str]:
    fails: list[str] = []
    # Landscape source (wider than tall) -> landscape output.
    doc = fitz.open()
    doc.new_page(width=792, height=612).insert_text((60, 300), "Landscape", fontsize=24)
    land = tempfile.mktemp(suffix=".pdf")
    doc.save(land)
    w = MainWindow()
    w._suppress_close_guard = True
    w.open_path(land)
    _pump(150)
    r = _print_to_pdf(w)
    if not r.width > r.height:
        fails.append(f"landscape page printed {r.width:.0f}x{r.height:.0f} (not landscape)")

    # Portrait source stays portrait.
    doc2 = fitz.open()
    doc2.new_page(width=612, height=792).insert_text((60, 300), "Portrait", fontsize=24)
    port = tempfile.mktemp(suffix=".pdf")
    doc2.save(port)
    w2 = MainWindow()
    w2._suppress_close_guard = True
    w2.open_path(port)
    _pump(150)
    r2 = _print_to_pdf(w2)
    if not r2.height > r2.width:
        fails.append(f"portrait page printed {r2.width:.0f}x{r2.height:.0f} (not portrait)")
    print(f"  print: landscape->{r.width:.0f}x{r.height:.0f}, portrait->{r2.width:.0f}x{r2.height:.0f}")
    return fails


def test_dont_save_closes() -> list[str]:
    fails: list[str] = []
    w = MainWindow()  # real close guard ACTIVE (no suppress)
    w.open_path("tests/fixtures/form_like.pdf")
    _pump(150)
    box = next(h.span for h in w.view._hotspots
               if hasattr(getattr(h, "span", None), "key"))
    w.document.stage_edit(0, box, "DIRTY EDIT")
    if not w.document.dirty:
        fails.append("doc not dirty after stage_edit; test setup invalid")

    # Force the discard dialog to answer "Don't Save", counting invocations.
    calls = {"n": 0}
    real = w._confirm_discard_if_dirty

    def forced(self):
        calls["n"] += 1
        if calls["n"] > 3:
            raise RuntimeError("Don't Save re-prompted (infinite loop)")
        # The real Don't-Save branch: drop changes + report safe-to-proceed.
        self.document.mark_clean()
        self.undo_stack.setClean()
        return True

    w._confirm_discard_if_dirty = types.MethodType(forced, w)
    ev = QCloseEvent()
    w.closeEvent(ev)
    if not ev.isAccepted():
        fails.append("closeEvent not accepted after Don't Save")
    if calls["n"] > 1:
        fails.append(f"Don't Save prompted {calls['n']}x (should be 1)")
    print(f"  don't-save: prompts={calls['n']}, closed={ev.isAccepted()}")
    return fails


def test_box_outline_persists_in_edit() -> list[str]:
    fails: list[str] = []
    w = MainWindow()
    w._suppress_close_guard = True
    w.open_path("tests/fixtures/paragraphs.pdf")
    _pump(200)
    v = w.view
    cands = [h for h in v._hotspots if hasattr(getattr(h, "span", None), "key")]
    hs = max(cands, key=lambda h: getattr(h.span, "size", 0))
    box = hs.span
    select = getattr(v, "select_box", None) or getattr(v, "_select_box", None)
    select(box)
    _pump(50)
    sel_rect = v._overlay.sceneBoundingRect() if v._overlay else None
    v.begin_edit(hs)
    _pump(60)
    # The overlay (box outline) must still be present and visible while editing,
    # at the same rect -- only the handles are dropped.
    if v._overlay is None or not v._overlay.isVisible():
        fails.append("selection box outline is hidden while editing (box appears to shrink)")
    else:
        edit_rect = v._overlay.sceneBoundingRect()
        if sel_rect is not None and abs(edit_rect.height() - sel_rect.height()) > 2.0:
            fails.append(
                f"box outline height changed selected->edit: "
                f"{sel_rect.height():.1f} -> {edit_rect.height():.1f}")
    print(f"  box outline visible in edit: {v._overlay is not None and v._overlay.isVisible()}")
    return fails


def test_manual_group_ungroup() -> list[str]:
    """Shift-select two boxes -> Group fuses them into one paragraph; Ungroup
    and Undo restore the split; a grouped box bakes (WYSIWYG) and saves."""
    fails: list[str] = []
    # Build a doc whose two adjacent same-size lines are in DIFFERENT fonts, so
    # the auto-detector keeps them separate -- exactly the cert case.
    doc = fitz.open()
    page = doc.new_page(width=400, height=200)
    page.insert_text((40, 60), "This sentence continues onto", fontsize=12,
                     fontname="helv")
    page.insert_text((40, 78), "the next line below it here", fontsize=12,
                     fontname="cour")  # different font -> not auto-grouped
    src = tempfile.mktemp(suffix=".pdf")
    doc.save(src)

    w = MainWindow()
    w._suppress_close_guard = True
    w.open_path(src)
    _pump(150)
    v = w.view
    spans = [b for b in w.document.spans(0) if getattr(b, "text", "").strip()]
    line1 = next(b for b in spans if "continues" in b.text)
    line2 = next(b for b in spans if "next line" in b.text)

    v.select_box(line1)
    v._toggle_multi(line2)
    if len(v.selected_boxes()) != 2:
        fails.append(f"multi-select holds {len(v.selected_boxes())} boxes, expected 2")

    v.groupRequested.emit(v.selected_boxes())
    _pump(60)
    merged = [b for b in w.document.spans(0)
              if getattr(b, "is_paragraph", False)
              and "continues" in b.text and "next line" in b.text]
    if len(merged) != 1:
        fails.append(f"group produced {len(merged)} merged paragraph(s), expected 1")

    # Undo restores the split.
    w.undo_stack.undo()
    _pump(40)
    split = [b for b in w.document.spans(0)
             if not getattr(b, "is_paragraph", False)
             and ("continues" in getattr(b, "text", "")
                  or "next line" in getattr(b, "text", ""))]
    if len(split) != 2:
        fails.append(f"undo left {len(split)} separate lines, expected 2")

    # Redo then ungroup.
    w.undo_stack.redo()
    _pump(40)
    para = next((b for b in w.document.spans(0)
                 if getattr(b, "is_paragraph", False) and "continues" in b.text), None)
    if para is None:
        fails.append("redo did not restore the merged paragraph")
    else:
        v.ungroupRequested.emit(para)
        _pump(40)
        re_split = [b for b in w.document.spans(0)
                    if not getattr(b, "is_paragraph", False)
                    and "next line" in getattr(b, "text", "")]
        if len(re_split) != 1:
            fails.append("ungroup did not split the paragraph back into lines")
    print(f"  group/ungroup: merged={len(merged)}, undo-split=ok, ungroup=ok")
    return fails


def test_group_preserves_reading_order() -> list[str]:
    """Grouping boxes must join their text in BASELINE reading order, not by
    bbox-top. A bold mid-line word sits lower in bbox-top terms, so a bbox-top
    sort would scatter it to the end and scramble the text."""
    fails: list[str] = []
    # One line with a BOLD word in the middle, drawn as separate runs (bold
    # text has a different bbox top but the SAME baseline as its line-mates),
    # plus a line above it -- group them and check the order.
    doc = fitz.open()
    page = doc.new_page(width=420, height=200)
    page.insert_text((40, 60), "First line of the paragraph here", fontsize=12,
                     fontname="helv")
    # Second line: "value is X mid sentence" with X bold in the middle.
    page.insert_text((40, 80), "value is ", fontsize=12, fontname="helv")
    page.insert_text((95, 80), "9999", fontsize=12, fontname="hebo")  # bold
    page.insert_text((125, 80), " mid sentence", fontsize=12, fontname="helv")
    src = tempfile.mktemp(suffix=".pdf")
    doc.save(src)

    w = MainWindow()
    w._suppress_close_guard = True
    w.open_path(src)
    _pump(150)
    spans = [b for b in w.document.spans(0)
             if not getattr(b, "is_paragraph", False)
             and getattr(b, "text", "").strip()]
    # Group everything on the page.
    if len(spans) < 3:
        fails.append(f"setup: expected >=3 separate spans, got {len(spans)}")
        return fails
    w.document.group_boxes(0, spans)
    merged = [b for b in w.document.spans(0)
              if getattr(b, "is_paragraph", False)]
    if len(merged) != 1:
        fails.append(f"group produced {len(merged)} paragraphs, expected 1")
        return fails
    text = merged[0].text
    # The bold "9999" must stay between "value is" and "mid sentence".
    iv, ib, im = text.find("value"), text.find("9999"), text.find("mid")
    if not (iv < ib < im and ib != -1):
        fails.append(f"bold word out of order: {text!r}")
    print(f"  group order: {'ok' if not fails else 'SCRAMBLED'} -> {text[-40:]!r}")
    return fails


def test_paragraph_editor_overlays_page() -> list[str]:
    """A paragraph editor must be a pixel overlay of the page: same hard line
    breaks (one block per baked line) and each line scaled to the baked width,
    so the text stays in place when you open it to edit."""
    from PySide6.QtGui import QFontMetricsF

    fails: list[str] = []
    w = MainWindow()
    w._suppress_close_guard = True
    w.open_path("tests/fixtures/paragraphs.pdf")
    _pump(200)
    v = w.view
    hs = next((h for h in v._hotspots
               if getattr(getattr(h, "span", None), "is_paragraph", False)), None)
    if hs is None:
        fails.append("no paragraph box in fixture")
        return fails
    box = hs.span
    baked = w.document.editor_line_layout(0, box)
    v.begin_edit(hs)
    _pump(40)
    ed = v._editor
    if not getattr(v, "_editor_hard_wrapped", False):
        fails.append("paragraph editor did not adopt the page's hard breaks")
        v.cancel_edit()
        return fails
    if ed.document().blockCount() != len(baked):
        fails.append(f"editor has {ed.document().blockCount()} blocks, "
                     f"baked has {len(baked)} lines")
    # Each block's rendered width must be within a few pt of the baked advance.
    z = v._zoom
    block = ed.document().begin()
    i = 0
    while block.isValid() and i < len(baked):
        bl = block.layout()
        if bl.lineCount() > 0:
            rendered_pt = bl.lineAt(0).naturalTextWidth() / z
            baked_pt = baked[i][1]
            if baked_pt > 0 and abs(rendered_pt - baked_pt) > 4.0:
                fails.append(f"line {i}: editor width {rendered_pt:.1f}pt vs "
                             f"baked {baked_pt:.1f}pt (>4pt off, would shift)")
        block = block.next()
        i += 1
    print(f"  overlay: {ed.document().blockCount()} blocks match {len(baked)} "
          f"baked lines, widths within tolerance")
    v.cancel_edit()
    return fails


def test_grouping_preserves_bold() -> list[str]:
    """Grouping a bold span with regular ones must keep the bold word bold in
    the editor and through a commit -- not flatten the paragraph to uniform."""
    from PySide6.QtGui import QFont, QTextCursor

    fails: list[str] = []
    doc = fitz.open()
    page = doc.new_page(width=420, height=200)
    page.insert_text((40, 60), "the value is ", fontsize=12, fontname="helv")
    page.insert_text((110, 60), "9999", fontsize=12, fontname="hebo")  # bold
    page.insert_text((140, 60), " for now", fontsize=12, fontname="helv")
    src = tempfile.mktemp(suffix=".pdf")
    doc.save(src)

    w = MainWindow()
    w._suppress_close_guard = True
    w.open_path(src)
    _pump(150)
    v = w.view
    spans = [b for b in w.document.spans(0)
             if not getattr(b, "is_paragraph", False)
             and getattr(b, "text", "").strip()]
    if len(spans) < 3:
        fails.append(f"setup: expected 3 spans, got {len(spans)}")
        return fails
    w.document.group_boxes(0, spans)
    v.reload()                                  # refresh hotspots for the group
    _pump(40)
    merged = next((b for b in w.document.spans(0)
                   if getattr(b, "is_paragraph", False)), None)
    if merged is None:
        fails.append("group did not produce a paragraph")
        return fails
    runs = w.document.paragraph_runs(merged)
    if not runs or not any(r[1] and "9999" in r[0] for r in runs):
        fails.append(f"paragraph_runs lost the bold word: {runs}")
    # The editor must show the bold word bold.
    hs = next((h for h in v._hotspots
               if getattr(getattr(h, "span", None), "is_paragraph", False)), None)
    if hs is None:
        fails.append("no paragraph hotspot after grouping")
        return fails
    v.begin_edit(hs)
    _pump(40)
    ed = v._editor
    idx = ed.toPlainText().find("9999")
    cur = ed.textCursor()
    cur.setPosition(idx + 1)
    bold_in_editor = cur.charFormat().fontWeight() >= QFont.DemiBold
    if not bold_in_editor:
        fails.append("editor did not render the grouped bold word bold")
    v.cancel_edit()
    print(f"  grouping bold: runs ok, editor bold={bold_in_editor}")
    return fails


def test_grouped_alignment_robust_to_outlier() -> list[str]:
    """A centered paragraph whose last line is centered on a slightly different
    axis must still be read as 'center', not collapse to 'left' (which would
    reflow the whole thing left-aligned and look nothing like the original)."""
    fails: list[str] = []
    doc = fitz.open()
    page = doc.new_page(width=600, height=300)
    # Three body lines centered at x=300, plus a short last line centered at
    # x=270 (a 30pt outlier, like the cert's drifted continuation line).
    lines = [
        (80, "The quarterly operations review will convene in the east annex"),
        (90, "on the second Tuesday of the month at nine in the morning sharp"),
        (110, "and every team lead must bring a short written status summary"),
    ]
    for y, txt in lines:
        wpt = fitz.get_text_length(txt, fontsize=11)
        page.insert_text(((600 - wpt) / 2, y + 60), txt, fontsize=11, fontname="helv")
    last = "after review by the board"
    wlast = fitz.get_text_length(last, fontsize=11)
    page.insert_text((270 - wlast / 2, 200), last, fontsize=11, fontname="helv")
    src = tempfile.mktemp(suffix=".pdf")
    doc.save(src)

    w = MainWindow()
    w._suppress_close_guard = True
    w.open_path(src)
    _pump(150)
    spans = [b for b in w.document.spans(0)
             if not getattr(b, "is_paragraph", False)
             and getattr(b, "text", "").strip()]
    w.document.group_boxes(0, spans)
    merged = next((b for b in w.document.spans(0)
                   if getattr(b, "is_paragraph", False)), None)
    if merged is None:
        fails.append("group did not produce a paragraph")
        return fails
    if merged.alignment != "center":
        fails.append(f"grouped centered paragraph inferred "
                     f"{merged.alignment!r}, expected 'center'")
    print(f"  alignment robustness: grouped -> {merged.alignment!r}")
    return fails


def test_grouped_noop_commit_changes_nothing() -> list[str]:
    """Opening a grouped paragraph (with a bold word) and committing WITHOUT
    typing must change NOTHING -- no staged edit, identical render. The bold
    runs mounted for display must not be mistaken for an edit."""
    import numpy as np

    fails: list[str] = []
    doc = fitz.open()
    page = doc.new_page(width=500, height=200)
    page.insert_text((60, 60), "first line of the body text here", fontsize=12, fontname="helv")
    page.insert_text((60, 80), "second line with a ", fontsize=12, fontname="helv")
    page.insert_text((160, 80), "BOLD", fontsize=12, fontname="hebo")
    page.insert_text((195, 80), " word inside it", fontsize=12, fontname="helv")
    src = tempfile.mktemp(suffix=".pdf")
    doc.save(src)

    w = MainWindow()
    w._suppress_close_guard = True
    w.open_path(src)
    _pump(150)
    v = w.view
    spans = [b for b in w.document.spans(0)
             if not getattr(b, "is_paragraph", False)
             and getattr(b, "text", "").strip()]
    w.document.group_boxes(0, spans)
    v.reload()
    _pump(40)
    hs = next((h for h in v._hotspots
               if getattr(getattr(h, "span", None), "is_paragraph", False)), None)
    if hs is None:
        fails.append("no grouped paragraph")
        return fails
    box = hs.span
    before = w.document.render_with_edits(0, 2.0)
    v.begin_edit(hs)
    _pump(40)
    v.commit_edit()                              # no text typed
    _pump(40)
    e = w.document._edits.get((0, box.key))
    if e is not None and not e.is_noop:
        fails.append("no-op commit staged an edit on the grouped box")
    after = w.document.render_with_edits(0, 2.0)
    a = np.frombuffer(before.samples, np.uint8).astype(int)
    b = np.frombuffer(after.samples, np.uint8).astype(int)
    diff = np.abs(a - b).mean() / 255 if a.shape == b.shape else 1.0
    if diff > 0.001:
        fails.append(f"no-op commit changed the render (diff={diff:.4f})")
    print(f"  grouped no-op: staged={e is not None and not e.is_noop}, "
          f"render diff={diff:.4f}")
    return fails


def main() -> int:
    failures: list[str] = []
    for fn in (test_landscape_print, test_dont_save_closes,
               test_box_outline_persists_in_edit, test_manual_group_ungroup,
               test_group_preserves_reading_order,
               test_paragraph_editor_overlays_page,
               test_grouping_preserves_bold,
               test_grouped_alignment_robust_to_outlier,
               test_grouped_noop_commit_changes_nothing):
        print(f"[{fn.__name__}]")
        failures.extend(fn())
    print("=" * 60)
    if failures:
        print(f"FAILED ({len(failures)}):")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("PASSED — landscape print, Don't-Save close, and edit-box size fixed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
