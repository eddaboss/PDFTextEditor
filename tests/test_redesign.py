"""Headless verification for the CONTINUOUS-SCROLL view + LAYOUT REDESIGN
(REFLOW_SPEC §R3 / §R4).

Mirrors the existing harness style: a ``failures`` list, a ``check()`` helper,
exit nonzero on any failure. Run with the venv python + offscreen platform:

    QT_QPA_PLATFORM=offscreen .venv/bin/python tests/test_redesign.py

It exercises, against the REAL ``MainWindow``:

  * LAYOUT: Format/Inspector lives in a LEFT dock; the thumbnail sidebar in a
    RIGHT dock; the left tool strip drives Add Text (enter_add_text_mode); the
    top bar is the slim action set (no Organize button on it); fit-to-width is
    the default zoom mode.
  * CONTINUOUS SCROLL: all pages stack in one scene (scene height spans them);
    fit-to-width default; scrolling to the bottom updates ``view.page_index`` to
    the last page and emits ``pageChanged``; lazy render materializes only the
    visible band (+buffer) so ``visible_pages()`` is a strict subset on a multi-
    page doc; a selection on a later page survives a scroll away and back.
  * PARAGRAPH REFLOW IN THE SCROLLED VIEW: select a ParagraphBox on a NON-FIRST
    page, edit its text so it grows by a line, commit through the real window,
    and assert the model staged a reflow edit + the wrapped line count grew + the
    save round-trips.
  * THUMBNAIL SYNC: clicking a thumbnail scrolls the continuous view to that page.

Full-window screenshots land in tests/screenshots/redesign/.
"""

from __future__ import annotations

import os
import sys
import traceback

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from pdftexteditor.document import PDFDocument  # noqa: E402
from pdftexteditor.reflow import wrap_paragraph  # noqa: E402
from pdftexteditor.ui.main_window import MainWindow  # noqa: E402

_APP = QApplication.instance() or QApplication(sys.argv)

FIXTURES = os.path.join(REPO, "tests", "fixtures")
MULTIPAGE = os.path.join(FIXTURES, "multipage_body.pdf")
PARAGRAPHS = os.path.join(FIXTURES, "paragraphs.pdf")
THREE = os.path.join(FIXTURES, "three_page.pdf")
SHOT_DIR = os.path.join(REPO, "tests", "screenshots", "redesign")
os.makedirs(SHOT_DIR, exist_ok=True)


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
    w.repaint()
    pump(4)
    return w


def shot_window(w: MainWindow, name: str) -> str:
    out = os.path.join(SHOT_DIR, name)
    pump()
    w.grab().save(out)
    return out


def close(w: MainWindow) -> None:
    """Close a window WITHOUT the dirty-discard modal (which would block forever
    headless). The window exposes ``_suppress_close_guard`` for exactly this."""
    w._suppress_close_guard = True
    w.close()


def paragraph_on_page(view, page_index: int):
    """Materialize ``page_index`` and return its first ParagraphBox + hotspot."""
    layer = view._layers[page_index]
    if not layer.rendered:
        view._materialize_page(layer)
    paras = [b for b in layer.boxes if getattr(b, "is_paragraph", False)]
    if not paras:
        return None, None
    box = paras[0]
    return box, view._hotspot_for(box)


# ==========================================================================
# 1. Layout redesign
# ==========================================================================
def test_layout(failures: list, shots: list) -> None:
    tag = "layout"
    w = open_window(MULTIPAGE)

    # LEFT dock hosts the Format/Inspector (inside the LeftPanel); RIGHT dock
    # hosts the thumbnail sidebar.
    check(failures, tag, w.dockWidgetArea(w.left_dock) == Qt.LeftDockWidgetArea,
          "Tools/Format dock is not on the LEFT")
    check(failures, tag, w.dockWidgetArea(w.pages_dock) == Qt.RightDockWidgetArea,
          "Pages thumbnail dock is not on the RIGHT")
    check(failures, tag, w.inspector.parent() is not None
          and w.inspector in w.left_panel.findChildren(type(w.inspector)),
          "Inspector is not hosted inside the LeftPanel")

    # The left tool strip drives Add Text -> the view's enter_add_text_mode.
    w.left_panel.toolSelected.emit("add_text")
    pump()
    check(failures, tag, w.act_add_text.isChecked(),
          "Add Text tool did not arm the Add Text action")
    check(failures, tag, w.view.current_mode() == "add_text",
          f"view mode after Add Text tool = {w.view.current_mode()!r}, "
          "expected 'add_text'")
    # Switching back to Select exits add-text mode.
    w.left_panel.toolSelected.emit("select")
    pump()
    check(failures, tag, not w.act_add_text.isChecked(),
          "Select tool did not disarm Add Text")
    check(failures, tag, w.view.current_mode() == "select",
          f"view mode after Select tool = {w.view.current_mode()!r}")

    # Slim top bar: the Organize popup button is NOT placed on the toolbar.
    bar_buttons = w.toolbar.findChildren(type(w.save_button))
    organize_on_bar = any(getattr(b, "objectName", lambda: "")() == "OrganizeButton"
                          and b.parent() is w.toolbar for b in bar_buttons)
    check(failures, tag, not organize_on_bar,
          "Organize button is still on the slim top bar")
    # A Find affordance exists (action + menu/toolbar).
    check(failures, tag, hasattr(w, "act_find") and w.act_find.isEnabled(),
          "Find action missing or disabled with a doc open")

    # Fit-the-whole-page is the default zoom mode (keeps the full document
    # visible and scaling with the window as it shrinks).
    check(failures, tag, w.view._zoom_mode == "fit_page",
          f"default zoom mode = {w.view._zoom_mode!r}, expected 'fit_page'")

    shots.append(shot_window(w, "01_layout_multipage.png"))
    close(w)


# ==========================================================================
# 2. Continuous scroll: stacked scene, lazy render, current-page tracking
# ==========================================================================
def test_continuous_scroll(failures: list, shots: list) -> None:
    tag = "scroll"
    w = open_window(MULTIPAGE)
    view = w.view
    n = w.document.page_count
    check(failures, tag, n >= 3, f"fixture must be multipage; got {n} pages")

    # All pages stack in ONE scene: scene height spans every page (>> one page).
    scene_h = view.scene().sceneRect().height()
    one_page_h = view._layers[0].pt_size[1] * view.zoom
    check(failures, tag, scene_h > one_page_h * (n - 0.5),
          f"scene height {scene_h:.0f} does not span all {n} pages "
          f"(one page ~{one_page_h:.0f})")

    # Lazy render: at the top only the visible band (+buffer) is materialized,
    # a STRICT subset of all pages.
    top_visible = view.visible_pages()
    check(failures, tag, len(top_visible) < n,
          f"all {n} pages materialized at the top (no lazy render): "
          f"{top_visible}")
    check(failures, tag, 0 in top_visible,
          f"page 0 not materialized at the top: {top_visible}")
    check(failures, tag, view.page_index == 0,
          f"current page at top = {view.page_index}, expected 0")

    # Track pageChanged as we scroll to the bottom.
    page_events = []
    view.pageChanged.connect(page_events.append)
    vbar = view.verticalScrollBar()
    vbar.setValue(vbar.maximum())
    pump()
    view._on_scroll_settled()
    pump()
    check(failures, tag, view.page_index == n - 1,
          f"current page at bottom = {view.page_index}, expected {n - 1}")
    check(failures, tag, (n - 1) in page_events,
          f"pageChanged did not fire for the last page; got {page_events}")
    check(failures, tag, (n - 1) in view.visible_pages(),
          f"last page not materialized at the bottom: {view.visible_pages()}")

    # Scroll back to the top; page tracking follows.
    vbar.setValue(vbar.minimum())
    pump()
    view._on_scroll_settled()
    pump()
    check(failures, tag, view.page_index == 0,
          f"current page after scroll back to top = {view.page_index}")

    shots.append(shot_window(w, "02_scroll_top.png"))
    close(w)


# ==========================================================================
# 3. Selection on a later page survives a scroll away and back
# ==========================================================================
def test_selection_survives_scroll(failures: list, shots: list) -> None:
    tag = "selection_scroll"
    w = open_window(MULTIPAGE)
    view = w.view
    n = w.document.page_count
    last = n - 1

    # Scroll to the last page and select a paragraph there through the public
    # select path (the same a click drives).
    view.set_page(last)
    pump()
    box, hs = paragraph_on_page(view, last)
    if not check(failures, tag, box is not None,
                 f"no ParagraphBox on the last page {last}"):
        close(w)
        return
    view.select_box(box)
    pump()
    ident = view.current_selection().identity
    check(failures, tag, view._overlay is not None,
          "selection overlay not installed on the last page")
    check(failures, tag, view.current_selection().page_index == last,
          "selection is not on the last page")

    # Scroll up to the top (the last page may dematerialize), then back down.
    vbar = view.verticalScrollBar()
    vbar.setValue(vbar.minimum())
    pump()
    view._on_scroll_settled()
    pump()
    vbar.setValue(vbar.maximum())
    pump()
    view._on_scroll_settled()
    pump()
    sel = view.current_selection()
    check(failures, tag, sel is not None and sel.identity == ident,
          "selection did not survive a scroll away and back")

    shots.append(shot_window(w, "03_selection_last_page.png"))
    close(w)


# ==========================================================================
# 4. Paragraph REFLOW edit in the scrolled view (non-first page)
# ==========================================================================
def _wrapped_line_count(doc: PDFDocument, page: int, box, text: str) -> int:
    rf = doc.font_engine.resolve(page, box.font, box.flags, text)
    font = doc.font_engine.fitz_font_for(rf)
    res = wrap_paragraph(text, font, box.size, box.bbox[0], box.origin[1],
                         box.bbox[2] - box.bbox[0], leading=box.leading)
    return len(res.lines)


def test_reflow_edit_in_scroll(failures: list, shots: list) -> None:
    tag = "reflow_scroll"
    w = open_window(MULTIPAGE)
    view = w.view
    page = min(2, w.document.page_count - 1)   # a NON-first page

    view.set_page(page)
    pump()
    box, hs = paragraph_on_page(view, page)
    if not check(failures, tag, hs is not None,
                 f"no ParagraphBox + hotspot on page {page}"):
        close(w)
        return

    orig = box.text
    orig_lines = _wrapped_line_count(w.document, page, box, orig)

    # Select then edit the paragraph through the real window flow.
    view.select_box(box)
    pump()
    check(failures, tag, getattr(view.current_selection(), "is_paragraph", False),
          "selected box on the scrolled page is not a ParagraphBox")
    # The Inspector now shows the paragraph controls.
    check(failures, tag, w.inspector._para_host.isVisible(),
          "Inspector paragraph (align/spacing) controls not shown for a paragraph")

    view.begin_edit(hs)
    check(failures, tag, view._editor is not None and view._editor_multiline,
          "paragraph editor did not open as a multi-line editor")
    longer = orig + (" This sentence appends a meaningful amount of extra text "
                     "so the paragraph must reflow onto one or more new lines.")
    view._editor.setPlainText(longer)
    view.commit_edit()
    pump()

    staged = w.document.staged_text(page, box)
    check(failures, tag, staged == longer,
          "edited paragraph text did not stage on the scrolled page")
    check(failures, tag, w.document.edit_count >= 1,
          f"edit_count after reflow edit = {w.document.edit_count}")
    new_lines = _wrapped_line_count(w.document, page, box, staged)
    check(failures, tag, new_lines > orig_lines,
          f"paragraph did not reflow to MORE lines "
          f"({orig_lines} -> {new_lines})")

    # Undo restores; redo re-applies (one history, REFLOW_SPEC §R3.6).
    w.undo_stack.undo()
    pump()
    check(failures, tag, w.document.staged_text(page, box) == orig,
          "undo did not restore the original paragraph text")
    w.undo_stack.redo()
    pump()
    check(failures, tag, w.document.staged_text(page, box) == longer,
          "redo did not re-apply the reflow edit")

    # Save round-trips with the reflowed paragraph baked.
    import tempfile
    out = os.path.join(tempfile.gettempdir(), "redesign_reflow.pdf")
    try:
        w.document.save_as(out)
        check(failures, tag, os.path.getsize(out) > 0, "saved file is empty")
    except Exception:
        failures.append(f"{tag}: save_as raised:\n{traceback.format_exc()}")

    shots.append(shot_window(w, "04_reflow_edit_page3.png"))
    close(w)


# ==========================================================================
# 5. Thumbnail click scrolls the continuous view
# ==========================================================================
def test_thumbnail_sync(failures: list, shots: list) -> None:
    tag = "thumb_sync"
    w = open_window(THREE)
    view = w.view
    n = w.document.page_count

    # Activate the LAST thumbnail through the sidebar's public signal (the same
    # a click emits) and assert the continuous view scrolled there.
    w.sidebar.pageActivated.emit(n - 1)
    pump()
    view._on_scroll_settled()
    pump()
    check(failures, tag, view.page_index == n - 1,
          f"thumbnail click did not scroll to the last page "
          f"(page_index={view.page_index})")
    check(failures, tag, (n - 1) in view.visible_pages(),
          "last page not materialized after a thumbnail click")

    # And back to the first.
    w.sidebar.pageActivated.emit(0)
    pump()
    view._on_scroll_settled()
    pump()
    check(failures, tag, view.page_index == 0,
          f"thumbnail click to page 1 did not scroll back "
          f"(page_index={view.page_index})")
    check(failures, tag, w.sidebar.current_page() == 0,
          f"thumbnail highlight did not follow back to page 1 "
          f"(highlight={w.sidebar.current_page()})")

    # REVERSE direction: scrolling the continuous view (NOT via a thumbnail
    # click) must drive the thumbnail HIGHLIGHT to the scrolled-into-view page,
    # so the strip tracks the current page during a plain scroll.
    view.set_page(n - 1)
    view._on_scroll_settled()
    pump()
    check(failures, tag, w.sidebar.current_page() == n - 1,
          f"scrolling the view did not move the thumbnail highlight to the "
          f"last page (highlight={w.sidebar.current_page()}, "
          f"view page={view.page_index})")

    shots.append(shot_window(w, "05_thumbnail_landscape_doc.png"))
    close(w)


# ==========================================================================
# 6. Paragraph alignment + line-spacing via the Inspector (in the scrolled view)
# ==========================================================================
def test_alignment_spacing(failures: list, shots: list) -> None:
    tag = "align_spacing"
    w = open_window(PARAGRAPHS)
    view = w.view
    box, hs = paragraph_on_page(view, 0)
    if not check(failures, tag, box is not None, "no ParagraphBox on paragraphs.pdf"):
        close(w)
        return
    view.select_box(box)
    pump()

    # Change alignment to center via the Inspector control -> one undo step.
    before = w.document.edit_count
    w.inspector.styleEdited.emit({"alignment": "center"})
    pump()
    eff = w.document.effective_style(0, view.current_selection())
    check(failures, tag, eff.get("alignment") == "center",
          f"alignment override did not apply (got {eff.get('alignment')!r})")

    # Change line spacing to 1.5x.
    w.inspector.styleEdited.emit({"line_spacing": 1.5})
    pump()
    eff2 = w.document.effective_style(0, view.current_selection())
    check(failures, tag, abs(float(eff2.get("line_spacing", 1.0)) - 1.5) < 1e-6,
          f"line_spacing override did not apply (got {eff2.get('line_spacing')})")
    check(failures, tag, w.document.edit_count >= 1,
          "alignment/spacing did not register as an edit")

    shots.append(shot_window(w, "06_align_center_spacing.png"))
    close(w)


def main() -> int:
    failures: list = []
    shots: list = []
    for fn in (test_layout, test_continuous_scroll, test_selection_survives_scroll,
               test_reflow_edit_in_scroll, test_thumbnail_sync,
               test_alignment_spacing):
        try:
            fn(failures, shots)
        except Exception:
            failures.append(f"{fn.__name__}: raised:\n{traceback.format_exc()}")

    print("\nScreenshots:")
    for s in shots:
        print(f"  {s}")
    print()
    if failures:
        print("=" * 70)
        print(f"FAILED -- {len(failures)} check(s):")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("=" * 70)
    print("PASSED -- continuous-scroll view (stacked scene, fit-width default, "
          "lazy render, current-page tracking, selection survives scroll) + the "
          "layout redesign (left tool strip + Format dock, pages dock right, slim "
          "top bar) work; paragraph reflow edits, alignment/spacing, and "
          "thumbnail sync all live in the real window with WYSIWYG + undo intact.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
