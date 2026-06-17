#!/usr/bin/env python3
"""Regression tests for two scanned-page rendering bugs (0.3.1).

1. CLIPPING: a rotated, image-only page must be allocated a vertical slot that
   matches its RENDERED size, or it overruns the next page ("pages clipping
   into each other"). PyMuPDF's ``page.rect`` already reflects /Rotate, and the
   page view materializes each slot from get_pixmap (also rotated), so the
   page-stack pass-1 sizing must use ``rect`` AS-IS. An extra 90/270 w/h swap
   double-counts rotation and under-sizes the slot for a landscape page rotated
   to portrait -- exactly the scanned/faxed pages users hit.

2. EDITED TINT ("yellow bars"): a PRISTINE OCR overlay box (render_mode 3, never
   touched) must NOT read as edited, or the page view paints its persistent ochre
   "edited" tint over every recovered word. Only once the user edits it (flips it
   visible, render_mode 0) does it earn the mark.

Run:  QT_QPA_PLATFORM=offscreen python tests/test_page_layout.py
"""
import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import fitz  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

_APP = QApplication.instance() or QApplication([])

from pdftexteditor.ui.main_window import MainWindow  # noqa: E402


def _pump(n: int = 6) -> None:
    for _ in range(n):
        _APP.processEvents()


def _rotated_image_only_pdf(path: str, pages: int = 3) -> None:
    """``pages`` image-only pages, each a LANDSCAPE mediabox (792x612) with
    /Rotate 90 so it DISPLAYS portrait (612x792). No text layer -- a filled rect
    stands in for the scan. This is the page shape that overran its slot before
    the pass-1 rotation fix (allocated 612 tall, rendered 792 tall)."""
    doc = fitz.open()
    for _ in range(pages):
        page = doc.new_page(width=792, height=612)        # landscape mediabox
        page.draw_rect(page.rect, color=(0.85, 0.85, 0.85),
                       fill=(0.85, 0.85, 0.85), width=0)
        page.set_rotation(90)                              # displays portrait
    doc.save(path)
    doc.close()


def _open(path: str) -> MainWindow:
    w = MainWindow()
    w.resize(1100, 900)
    w.show()
    w.open_path(path)
    _pump()
    if getattr(w, "empty_state", None) is not None:
        w.empty_state.hide()
    _pump()
    return w


def test_rotated_pages_do_not_overlap() -> None:
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "rotated_multipage.pdf")
        _rotated_image_only_pdf(path, pages=3)
        w = _open(path)
        try:
            view = w.view
            for layer in view._layers:           # materialize -> refined sizes
                view._materialize_page(layer)
            _pump()
            z = view.zoom
            layers = view._layers
            assert len(layers) == 3, f"expected 3 pages, got {len(layers)}"
            # The rendered page must display PORTRAIT (taller than wide): proves
            # the slot is sized from the rotated rect, not the unrotated mediabox.
            wd, h = layers[0].pt_size[0], layers[0].pt_size[1]
            assert h > wd, f"rotated page should display portrait, got {wd}x{h}"
            # No page's bottom may cross into the next page's top.
            for i in range(len(layers) - 1):
                top, nxt = layers[i], layers[i + 1]
                bottom_of_top = top.y_top + top.pt_size[1] * z
                assert bottom_of_top <= nxt.y_top + 1.0, (
                    f"page {i} (y_top={top.y_top:.1f}, h={top.pt_size[1] * z:.1f}) "
                    f"overruns page {i + 1} (y_top={nxt.y_top:.1f}) by "
                    f"{bottom_of_top - nxt.y_top:.1f}px -- rotated pages clip")
            print(f"  ok  3 rotated image-only pages stack without overlap "
                  f"(display {wd:.0f}x{h:.0f}pt)")
        finally:
            w._suppress_close_guard = True
            w.close()


def test_pristine_ocr_box_not_edited() -> None:
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "rotated_multipage.pdf")
        _rotated_image_only_pdf(path, pages=1)
        w = _open(path)
        try:
            view, doc = w.view, w.document
            # An OCR overlay word: invisible (render_mode 3) with a paper cover.
            box = doc.add_box(0, (60.0, 120.0), "scan", "Helvetica", 12.0,
                              (0.0, 0.0, 0.0), False, False,
                              cover=(50.0, 108.0, 130.0, 126.0, 1.0, 1.0, 1.0),
                              render_mode=3)
            assert view.is_edited(box) is False, (
                "a pristine OCR overlay box (render_mode 3) must NOT read as "
                "edited -- else every recovered word wears the ochre tint")
            doc.stage_edit(0, box, "EDITED")
            edited = doc._new_boxes[box.edit_key]
            assert edited.render_mode == 0, "edit must flip the box visible"
            assert view.is_edited(edited) is True, (
                "an edited OCR box (render_mode 0) must read as edited")
            print("  ok  pristine OCR box is unmarked; editing earns the tint")
        finally:
            w._suppress_close_guard = True
            w.close()


def main() -> None:
    test_rotated_pages_do_not_overlap()
    test_pristine_ocr_box_not_edited()
    print("\n2 page-layout tests passed.")


if __name__ == "__main__":
    main()
