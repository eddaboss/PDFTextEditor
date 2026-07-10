#!/usr/bin/env python3
"""Regression test for the OCR overlay invariant (OCR_SPEC, Acrobat-style).

An OCR text box is added INVISIBLE (render_mode 3) over the kept scan, so the
page renders pixel-identical to the original; its cover (the scanned word region)
is painted only once the word is EDITED, which flips it visible. This guards both
halves with the real document bake path and no OCR engine.

Run:  QT_QPA_PLATFORM=offscreen python tests/test_ocr_overlay.py
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


def _synthetic_pdf(path: str) -> None:
    """A one-page PDF standing in for a SCAN: a white sheet with one black word
    ("WORD"). The overlay's edit path sizes replacement glyphs to the covered
    scan's cap-height and degrades them, so the fixture must carry REAL glyphs
    to measure -- a blank field has none and the sizing reads the whole covered
    region as one giant character. The invisible overlay must leave this word
    byte-for-byte intact."""
    doc = fitz.open()
    page = doc.new_page(width=300, height=200)
    page.draw_rect(fitz.Rect(0, 0, 300, 200), color=(1, 1, 1),
                   fill=(1, 1, 1), width=0)
    page.insert_text((105, 113), "WORD", fontsize=18, color=(0, 0, 0))
    doc.save(path)
    doc.close()


def _px(pm):
    a = np.frombuffer(pm.samples, np.uint8).reshape(pm.height, pm.width, pm.n)
    return a[:, :, :3].astype(np.int16)


def main():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "syn.pdf")
        _synthetic_pdf(path)
        doc = PDFDocument(path)
        base = _px(doc.render(0, 2.0))

        # Invisible OCR box whose cover is the scanned word's region.
        cover = (103.0, 99.0, 150.0, 114.0, 1.0, 1.0, 1.0)  # white paper cover
        box = doc.add_box(0, (105.0, 113.0), "WORD", "Helvetica", 18.0,
                          (0.0, 0.0, 0.0), False, False,
                          cover=cover, render_mode=3)

        invisible = _px(doc.render_with_edits(0, 2.0))
        assert invisible.shape == base.shape
        diff = np.abs(invisible - base)
        assert int(diff.max()) == 0, (
            f"invisible OCR overlay must render identical to the scan; "
            f"max pixel diff was {int(diff.max())}")
        print("  ok  invisible overlay renders pixel-identical (cover not painted)")

        # Editing the word makes it visible: the cover (white) + the new glyphs
        # now draw, so the page changes -- and only inside the covered word
        # region. A SAME-LENGTH replacement stays at the scan's glyph size
        # within the cover (a longer word would rightly extend past it, which
        # is a separate behaviour, not the locality this guards).
        doc.stage_edit(0, box, "MEMO")
        edited = _px(doc.render_with_edits(0, 2.0))
        edited_box = doc._new_boxes[box.edit_key]
        assert edited_box.render_mode == 0, "edit must flip the box visible"
        changed = np.abs(edited - base).sum(axis=2) > 8
        assert changed.any(), "an edited word must change the page"
        ys, xs = np.where(changed)
        cx0, cy0, cx1, cy1 = (cover[0] * 2, cover[1] * 2, cover[2] * 2, cover[3] * 2)
        assert (xs.min() >= cx0 - 2 and xs.max() <= cx1 + 2
                and ys.min() >= cy0 - 2 and ys.max() <= cy1 + 2), (
            "the edit must stay within the covered word region")
        print("  ok  edit flips the box visible and changes only the covered word")
        doc.close()
    print("\n2 ocr-overlay tests passed.")


if __name__ == "__main__":
    main()
