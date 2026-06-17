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
import numpy as np  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

_APP = QApplication.instance() or QApplication([])

from pdftexteditor.document import PDFDocument  # noqa: E402
from pdftexteditor.ocr.reconstruct import _group_lines_into_areas  # noqa: E402
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


def test_lines_group_into_paragraphs() -> None:
    """OCR lines fuse into ONE area per paragraph; a separated field stays its own
    box. Drives the reconstruct grouping directly (no OCR engine), so it is
    deterministic. Coords are display pixels (x0,y0,x1,y1,baseline,em,...)."""
    def ln(x0, y0, x1, y1, txt):
        return {"x0": x0, "y0": y0, "x1": x1, "y1": y1, "baseline": y1 - 3,
                "text": txt, "em": 16.0, "conf": 0.9}
    lines = [
        ln(40, 60, 300, 78, "Paragraph line one of a tight block"),    # para A
        ln(40, 80, 300, 98, "paragraph line two continues here"),      # +18 gap
        ln(40, 100, 300, 118, "and paragraph line three ends it"),
        ln(40, 200, 260, 218, "Provider: Acme Home Health"),           # far below
        ln(40, 260, 200, 278, "Phone: 555 0100"),                      # field
    ]
    areas = _group_lines_into_areas(lines)
    multi = [a for a in areas if len(a) >= 2]
    singles = [a for a in areas if len(a) == 1]
    assert len(areas) == 3, f"want 3 areas (1 para + 2 fields), got {len(areas)}"
    assert len(multi) == 1 and len(multi[0]) == 3, (
        f"the tight 3-line block must fuse into ONE area, got {[len(a) for a in areas]}")
    assert len(singles) == 2, "the two separated fields must stay single boxes"
    print("  ok  3 tight lines fuse into one paragraph; 2 fields stay separate")


def _flat_scan(path: str) -> None:
    doc = fitz.open()
    page = doc.new_page(width=400, height=300)
    page.draw_rect(page.rect, color=(0.96, 0.96, 0.94),
                   fill=(0.96, 0.96, 0.94), width=0)
    doc.save(path)
    doc.close()


def _px(pm):
    a = np.frombuffer(pm.samples, np.uint8).reshape(pm.height, pm.width, pm.n)
    return a[..., :3].astype(np.int16)


def test_paragraph_box_invisible_then_edits_in_area() -> None:
    """A paragraph OCR box renders pixel-identical to the scan until edited; the
    edit flips it visible and bakes its reflowed text INSIDE the area -- on a
    rotated scan too (the tile is placed via insert_image(rotate=page.rotation))."""
    for rot in (0, 90):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "s.pdf")
            doc0 = fitz.open()
            page = doc0.new_page(width=400, height=300)
            page.draw_rect(page.rect, color=(0.96, 0.96, 0.94),
                           fill=(0.96, 0.96, 0.94), width=0)
            if rot:
                page.set_rotation(rot)
            doc0.save(p)
            doc0.close()
            doc = PDFDocument(p)
            try:
                scan = _px(doc.render(0, 2.0))
                disp_cover = (36.0, 56.0, 250.0, 118.0)
                cover = tuple(doc.ocr_cover_rect(0, disp_cover)) + (0.96, 0.96, 0.94)
                origin, direction = doc.ocr_text_placement(0, (40.0, 70.0))
                box = doc.add_box(
                    0, origin, "Scanned block line one and line two and three",
                    "Tinos", 11.0, (0, 0, 0), False, False, direction=direction,
                    cover=cover, render_mode=3, box_w=214.0, leading=16.0)
                inv = _px(doc.render_with_edits(0, 2.0))
                assert int(np.abs(inv - scan).max()) == 0, (
                    f"rot={rot}: invisible paragraph overlay must match the scan")
                doc.stage_edit(0, box, "EDITED paragraph text replacing the whole "
                                       "scanned block as one editable unit now")
                eb = doc._new_boxes[box.edit_key]
                assert eb.render_mode == 0 and eb.edit_image, (
                    f"rot={rot}: edited paragraph must flip visible + build a tile")
                edited = _px(doc.render_with_edits(0, 2.0))
                ch = np.abs(edited - scan).sum(2) > 24
                ys, xs = np.where(ch)
                ax0, ay0, ax1, ay1 = (v * 2 for v in disp_cover)
                assert ch.any() and xs.min() >= ax0 - 14 and xs.max() <= ax1 + 14 \
                    and ys.min() >= ay0 - 14 and ys.max() <= ay1 + 14, (
                    f"rot={rot}: the edited text must land INSIDE the area, not "
                    f"misplaced (changed x[{xs.min()}-{xs.max()}] "
                    f"y[{ys.min()}-{ys.max()}] vs area x[{int(ax0)}-{int(ax1)}])")
            finally:
                doc.close()
    print("  ok  paragraph box invisible until edited, edit lands in area (rot 0 + 90)")


def test_paragraph_box_mounts_multiline_editor() -> None:
    """A paragraph NewBox opens the MULTI-LINE inline editor (edit the block as
    one) without crashing on the span-only editor paths, and committing rebuilds
    the paragraph tile. Checked BEFORE pumping (offscreen loses editor focus)."""
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "s.pdf")
        _flat_scan(p)
        w = _open(p)
        try:
            doc, view = w.document, w.view
            cover = (36.0, 56.0, 250.0, 118.0, 0.96, 0.96, 0.94)
            box = doc.add_box(0, (40.0, 70.0),
                              "Para one and para two and para three here",
                              "Tinos", 11.0, (0, 0, 0), False, False,
                              cover=cover, render_mode=3, box_w=210.0, leading=16.0)
            view.reload()
            _pump()
            for layer in view._layers:
                view._materialize_page(layer)
            hs = view._hotspot_for(box)
            assert hs is not None, "paragraph box must have an editable hotspot"
            view.begin_edit(hs)                  # no pump: offscreen focus-out commits
            assert view._editor is not None, "paragraph box must mount an editor"
            assert view._editor_multiline is True, (
                "a paragraph box must open the MULTI-LINE editor (edit as one)")
            view._editor.setPlainText("Edited paragraph as one block now")
            view.commit_edit()
            _pump()
            eb = doc._new_boxes[box.edit_key]
            assert eb.render_mode == 0 and eb.edit_image and eb.is_paragraph, (
                "committing a paragraph edit must keep it a paragraph + build a tile")
            print("  ok  paragraph box opens the multi-line editor + commits as one")
        finally:
            w._suppress_close_guard = True
            w.close()


def main() -> None:
    test_rotated_pages_do_not_overlap()
    test_pristine_ocr_box_not_edited()
    test_lines_group_into_paragraphs()
    test_paragraph_box_invisible_then_edits_in_area()
    test_paragraph_box_mounts_multiline_editor()
    print("\n5 page-layout tests passed.")


if __name__ == "__main__":
    main()
