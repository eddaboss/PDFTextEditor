#!/usr/bin/env python3
"""Verify the revert-blank fix: commit a width-growing in-place edit, then render the
page with that box EXCLUDED (the live-edit state where the editor floats over the
scan). The box's cover region must show the SCAN ink (not blank paper)."""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools"))
sys.path.append(os.path.expanduser("~/Documents/GitHub/PDFTextEditor-ocr"))

import numpy as np
import fitz
from PySide6.QtWidgets import QApplication
_APP = QApplication.instance() or QApplication([])

from pdftexteditor.document import PDFDocument
from pdftexteditor.ocr import recognize_and_reconstruct
from edit_dates import apply_ocr


def cover_px(doc, pi, cov, scale):
    rot = doc.working[pi].rotation_matrix
    pts = [fitz.Point(cov[0], cov[1]) * rot, fitz.Point(cov[2], cov[1]) * rot,
           fitz.Point(cov[2], cov[3]) * rot, fitz.Point(cov[0], cov[3]) * rot]
    xs = [p.x * scale for p in pts]
    ys = [p.y * scale for p in pts]
    return int(min(xs)), int(min(ys)), int(max(xs)), int(max(ys))


def ink_frac(doc, pi, nb, scale, exclude):
    pix = doc.render_with_edits(pi, scale, exclude_span=nb if exclude else None)
    arr = np.frombuffer(pix.samples, np.uint8).reshape(pix.height, pix.width, pix.n)[..., :3]
    x0, y0, x1, y1 = cover_px(doc, pi, nb.cover, scale)
    x0, y0 = max(0, x0), max(0, y0)
    x1, y1 = min(arr.shape[1], x1), min(arr.shape[0], y1)
    crop = arr[y0:y1, x0:x1]
    if crop.size == 0:
        return None
    return float((crop.mean(2) < 128).mean())


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else \
        "/Users/edward/Downloads/doc05154920260624150538.pdf"
    pi = int(sys.argv[2]) if len(sys.argv) > 2 else 1
    doc = PDFDocument(path)
    doc.normalize_orientations()
    rgb = doc.render_page_image(pi, 300.0)
    res = recognize_and_reconstruct(rgb, 300.0, "", "", "auto", f"p{pi}")
    apply_ocr(doc, pi, res)
    boxes = [b for b in doc.new_boxes(pi) if b.cover and len(b.cover) == 7
             and 3 <= len(b.text.strip()) <= 14 and "\n" not in b.text
             and not getattr(b, "is_paragraph", False)]
    print(f"page {pi} rotation={doc.working[pi].rotation}  single-line boxes={len(boxes)}")
    for b in boxes[:5]:
        orig = b.text
        mid = len(orig) // 2
        doc.stage_edit(pi, b, orig[:mid] + "really" + orig[mid:])
        nb = next((x for x in doc.new_boxes(pi) if x.box_id == b.box_id), None)
        if nb is None or not nb.edit_image:
            continue
        excl = ink_frac(doc, pi, nb, 2.0, exclude=True)     # live-edit state (revert)
        comm = ink_frac(doc, pi, nb, 2.0, exclude=False)    # committed edit
        verdict = ("BLANK (bug)" if (excl is not None and excl < 0.01)
                   else "scan shows (FIXED)")
        print(f"  {orig!r:18}  excluded ink={excl:.3f}  committed ink={comm:.3f}  -> {verdict}")
    doc.close()


if __name__ == "__main__":
    main()
