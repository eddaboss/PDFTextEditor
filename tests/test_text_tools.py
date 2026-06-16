"""Headless verification for the TEXT TOOLS build (REFLOW_SPEC §R5.1 / §R5.3):
Find & Replace (cross-page, staged + undoable, WYSIWYG after reflow) and
Copy / Paste WITH formatting.

Covers, against the REAL model + the REAL ``MainWindow``:

  1. MODEL find_all -- cross-page, case-insensitive by default, match-case +
     whole-word options, matches over a multi-line ParagraphBox's JOINED text
     (a hit can span the original soft line breaks), Match carries a stable
     identity + char span; replaced_text splices a new staged text.
  2. WINDOW Find panel -- Cmd+F opens it; Find navigates (scrolls the continuous
     view to the match's page and selects its box); Replace stages ONE undoable
     text edit through the normal command route; Replace All across pages is ONE
     undo macro and one undo restores everything.
  3. WYSIWYG after a Replace-driven reflow -- render_with_edits == the saved file
     (rel ink diff < 0.02), the keystone invariant, held for a paragraph that
     grows/shrinks lines from a replace.
  4. SAVED file carries the replacements (round-trip through save_as).
  5. COPY / PASTE with formatting -- copy a styled box puts text+style on the
     view clipboard; paste creates a NewBox carrying the same font/size/color.

Run:
    QT_QPA_PLATFORM=offscreen \
      /Users/edward/Documents/GitHub/PDFTextEditor/.venv/bin/python \
      tests/test_text_tools.py
"""

from __future__ import annotations

import os
import re
import sys
import tempfile
import traceback

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import fitz  # noqa: E402
import numpy as np  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from pdftexteditor.document import Match, PDFDocument  # noqa: E402
from pdftexteditor.ui.main_window import MainWindow  # noqa: E402

_APP = QApplication.instance() or QApplication(sys.argv)

FIXTURES = os.path.join(REPO, "tests", "fixtures")
MULTIPAGE = os.path.join(FIXTURES, "multipage_body.pdf")
BODY = os.path.join(FIXTURES, "body_paragraphs.pdf")


def pump(n: int = 4) -> None:
    for _ in range(n):
        _APP.processEvents()


def check(failures: list, tag: str, cond: bool, msg: str) -> bool:
    if not cond:
        failures.append(f"{tag}: {msg}")
    return cond


def open_window(path: str) -> MainWindow:
    w = MainWindow()
    w.resize(1300, 950)
    w.show()
    w.open_path(path)
    pump(6)
    if getattr(w, "empty_state", None) is not None:
        w.empty_state.hide()
    pump(2)
    return w


def close(w: MainWindow) -> None:
    w._suppress_close_guard = True
    w.close()


def box_for(doc: PDFDocument, m: Match):
    for b in (*doc.spans(m.page_index), *doc.new_boxes(m.page_index)):
        if b.identity == m.box_identity:
            return b
    return None


def ink_diff(a: np.ndarray, b: np.ndarray) -> float:
    h = min(a.shape[0], b.shape[0])
    wd = min(a.shape[1], b.shape[1])
    ia = a[:h, :wd, :3].astype(np.int32).mean(2) < 250
    ib = b[:h, :wd, :3].astype(np.int32).mean(2) < 250
    diff = np.logical_xor(ia, ib).sum()
    denom = max(int(ia.sum()) + int(ib.sum()), 1)
    return float(diff) / denom


# ---------------------------------------------------------------------------
# 1. MODEL: find_all + replaced_text
# ---------------------------------------------------------------------------
def test_model_find(failures: list) -> None:
    doc = PDFDocument(MULTIPAGE)

    # A word unique to page 3 (index 2): one hit, on page 2, inside a paragraph.
    ms = doc.find_all("budget")
    check(failures, "find.unique", len(ms) == 1 and ms[0].page_index == 2,
          f"'budget' should be one hit on page index 2, got "
          f"{[(m.page_index) for m in ms]}")
    if ms:
        m = ms[0]
        box = box_for(doc, m)
        check(failures, "find.box_is_para", getattr(box, "is_paragraph", False),
              "'budget' should live in a ParagraphBox")
        # The match's start/end slice the box's joined text correctly.
        hay = doc.staged_text(m.page_index, box)
        check(failures, "find.slice", hay[m.start:m.end] == "budget",
              f"slice mismatch: {hay[m.start:m.end]!r}")

    # Cross-page: a common word appears on every page.
    everywhere = doc.find_all("the")
    pages = sorted({m.page_index for m in everywhere})
    check(failures, "find.crosspage", pages == [0, 1, 2, 3],
          f"'the' should hit all 4 pages, got {pages}")

    # Whole-word filters out within-word hits ('the' inside 'they'/'another').
    sub = len(doc.find_all("the"))
    whole = len(doc.find_all("the", whole_word=True))
    check(failures, "find.whole_word", whole < sub,
          f"whole-word ({whole}) should be fewer than substring ({sub})")

    # Match-case: 'BUDGET' (uppercase) has zero hits; default is insensitive.
    check(failures, "find.case", len(doc.find_all("BUDGET", match_case=True)) == 0
          and len(doc.find_all("BUDGET")) == 1,
          "match-case should distinguish BUDGET; default insensitive")

    # A match that SPANS a paragraph's original soft line break. On page 3 the
    # text '...came in under the projected ceiling, largely because\ntwo of the
    # planned...' breaks 'because two' across lines; the joined text has a space,
    # so a query across the break is findable.
    cross = doc.find_all("because two")
    check(failures, "find.span_break", len(cross) >= 1,
          "a query spanning a soft line break should match the joined text")

    # replaced_text splices the new staged text.
    if ms:
        m = ms[0]
        box = box_for(doc, m)
        new = doc.replaced_text(m.page_index, box, m.start, m.end, "forecast")
        check(failures, "replaced_text",
              "forecast" in new and "budget" not in new,
              "replaced_text should swap the match span")


# ---------------------------------------------------------------------------
# 2 + 3 + 4. WINDOW: Find panel navigate / replace / replace-all + WYSIWYG + save
# ---------------------------------------------------------------------------
def test_window_find_replace(failures: list) -> None:
    w = open_window(MULTIPAGE)
    try:
        doc = w.document
        panel = w.find_panel

        # Cmd+F opens the panel + focuses it (we call the action's slot).
        w._activate_find()
        pump(2)
        check(failures, "panel.shown",
              w.left_panel._stack.currentWidget() is panel,
              "Find tool should swap the left column to the Find panel")
        check(failures, "panel.tool_armed",
              w.left_panel.active_tool() == "find",
              "Find tool should be the armed tool")

        # Find a word on page 3 (index 2) and navigate to it.
        panel.find_edit.setText("budget")
        pump(3)
        panel.find_next()
        pump(4)
        check(failures, "nav.page", w.view_page_index() == 2,
              f"find_next should scroll to page 2, got {w.view_page_index()}")
        sel = w.view.current_selection()
        check(failures, "nav.select", getattr(sel, "identity", None) is not None,
              "find_next should select the matched box")

        # Replace ONE: 'budget' -> 'forecast' is one undoable edit.
        before_count = w.undo_stack.count()
        panel.replace_edit.setText("forecast")
        pump(2)
        panel.replace_current()
        pump(4)
        check(failures, "replace.one_undo",
              w.undo_stack.count() == before_count + 1,
              "Replace should push exactly one undo command")
        check(failures, "replace.staged",
              len(doc.find_all("budget")) == 0
              and len(doc.find_all("forecast")) >= 1,
              "Replace should swap budget->forecast in the staged text")
        check(failures, "replace.dirty", doc.dirty,
              "Replace should dirty the document")

        # WYSIWYG: the reflowed paragraph renders the same pixels as the saved
        # file (the keystone). Render the box's page and compare.
        m = doc.find_all("forecast")[0]
        page = m.page_index
        scale = 2.0
        pm = doc.render_with_edits(page, scale)
        screen = np.frombuffer(pm.samples, dtype=np.uint8).reshape(
            pm.height, pm.width, pm.n)
        out = os.path.join(tempfile.mkdtemp(), "rep.pdf")
        doc.save_as(out)
        sd = fitz.open(out)
        sp = sd[page].get_pixmap(matrix=fitz.Matrix(scale, scale))
        saved = np.frombuffer(sp.samples, dtype=np.uint8).reshape(
            sp.height, sp.width, sp.n)
        rel = ink_diff(screen, saved)
        check(failures, "replace.wysiwyg", rel < 0.02,
              f"WYSIWYG after replace must be < 0.02, got {rel:.4f}")
        # Saved file carries the replacement.
        saved_txt = "\n".join(sd[i].get_text("text")
                              for i in range(sd.page_count))
        check(failures, "replace.saved", "forecast" in saved_txt
              and "budget" not in saved_txt,
              "saved file should carry the replacement")
        sd.close()

        # Replace All across pages is ONE undo macro. Undo it in one step.
        cs_before = len(doc.find_all("the", match_case=True))
        check(failures, "replaceall.precond", cs_before > 4,
              "fixture should have several lowercase 'the' to replace")
        panel.find_edit.setText("the")
        panel.replace_edit.setText("THE")
        panel.case_check.setChecked(True)   # case-sensitive so we can undo-count
        pump(2)
        macro_before = w.undo_stack.count()
        panel.replace_all()
        pump(4)
        check(failures, "replaceall.macro",
              w.undo_stack.count() == macro_before + 1,
              "Replace All should push exactly ONE macro command")
        check(failures, "replaceall.applied",
              len(doc.find_all("the", match_case=True)) == 0,
              "Replace All should swap every lowercase 'the'")
        w.undo_stack.undo()
        pump(4)
        check(failures, "replaceall.undo",
              len(doc.find_all("the", match_case=True)) == cs_before,
              "one undo must restore ALL replace-all edits (single macro)")
        w.undo_stack.redo()
        pump(4)

        # Esc / close returns to the Select tool + Format panel.
        panel.close_panel()
        pump(2)
        check(failures, "panel.closed",
              w.left_panel.active_tool() == "select"
              and w.left_panel._stack.currentWidget() is w.inspector,
              "closing Find should return to Select + the Format panel")
    finally:
        close(w)


# ---------------------------------------------------------------------------
# 5. COPY / PASTE with formatting
# ---------------------------------------------------------------------------
def test_copy_paste(failures: list) -> None:
    w = open_window(BODY)
    try:
        doc = w.document
        view = w.view
        # Select the first paragraph box (a >=2-line ParagraphBox).
        para = None
        for b in doc.spans(0):
            if getattr(b, "is_paragraph", False):
                para = b
                break
        check(failures, "copy.has_para", para is not None,
              "body_paragraphs should yield a ParagraphBox to copy")
        if para is None:
            return
        view.select_box(para)
        pump(2)
        src_style = doc.effective_style(0, para)

        ok = view.copy_selection()
        check(failures, "copy.ok", ok and view._clipboard is not None,
              "copy_selection should populate the view clipboard")
        clip = view._clipboard or {}
        check(failures, "copy.style",
              abs(clip.get("size", -1) - src_style["size"]) < 1e-6
              and clip.get("color") == tuple(src_style["color"]),
              "clipboard should carry the source size + color")

        n_before = len(doc.new_boxes_all())
        ok = view.paste()
        pump(4)
        check(failures, "paste.ok", ok, "paste should create a new box")
        new_boxes = doc.new_boxes_all()
        check(failures, "paste.created", len(new_boxes) == n_before + 1,
              "paste should add exactly one NewBox")
        if len(new_boxes) == n_before + 1:
            nb = new_boxes[-1]
            check(failures, "paste.text", nb.text == clip.get("text"),
                  "pasted box should carry the copied text")
            check(failures, "paste.size",
                  abs(nb.size - clip.get("size")) < 1e-6,
                  "pasted box should carry the copied size")
            check(failures, "paste.color",
                  tuple(nb.color) == clip.get("color"),
                  "pasted box should carry the copied color")
    finally:
        close(w)


def main() -> int:
    failures: list = []
    for fn in (test_model_find, test_window_find_replace, test_copy_paste):
        try:
            fn(failures)
        except Exception:  # noqa: BLE001
            failures.append(f"{fn.__name__}: EXCEPTION\n"
                            + traceback.format_exc())
    # copy_selection now installs a QMimeData (text/plain + the x-pdfte-runs
    # payload, ws2 M3) on the system clipboard; release it BEFORE interpreter
    # teardown or the offscreen platform's clipboard destructor segfaults
    # AFTER all output (exit 139 on a green run -- spec ws2 §1 probe note).
    try:
        QApplication.clipboard().clear()
    except Exception:  # noqa: BLE001 - never let cleanup mask the verdict
        pass
    if failures:
        print("FAILED:")
        for f in failures:
            print("  -", f)
        return 1
    print("PASSED -- find_all is cross-page (case / whole-word / paragraph-"
          "spanning); the Find panel navigates the continuous view, Replace "
          "stages one undoable edit and Replace All is one macro; WYSIWYG holds "
          "(< 0.02) after a replace-driven reflow and the saved file carries the "
          "edits; copy/paste WITH formatting round-trips style. No exceptions.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
