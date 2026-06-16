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
    """A one-page PDF with a solid gray field, so there is real content that an
    overlay must leave byte-for-byte intact."""
    doc = fitz.open()
    page = doc.new_page(width=300, height=200)
    page.draw_rect(fitz.Rect(0, 0, 300, 200), color=(0.6, 0.6, 0.6),
                   fill=(0.6, 0.6, 0.6), width=0)
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

        # Invisible OCR box with a cover over the middle of the page.
        cover = (100.0, 80.0, 200.0, 120.0, 1.0, 1.0, 1.0)  # white paper cover
        box = doc.add_box(0, (105.0, 110.0), "WORD", "Helvetica", 18.0,
                          (0.0, 0.0, 0.0), False, False,
                          cover=cover, render_mode=3)

        invisible = _px(doc.render_with_edits(0, 2.0))
        assert invisible.shape == base.shape
        diff = np.abs(invisible - base)
        assert int(diff.max()) == 0, (
            f"invisible OCR overlay must render identical to the scan; "
            f"max pixel diff was {int(diff.max())}")
        print("  ok  invisible overlay renders pixel-identical (cover not painted)")

        # Editing the word makes it visible: the cover (white) + black text now
        # draw, so the page changes -- and only inside the cover region.
        doc.stage_edit(0, box, "EDITED")
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
