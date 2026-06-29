#!/usr/bin/env python3
"""Render tight crops of the date lines from the original and edited PDFs, stacked,
so the edit can be eyeballed (did the year change and does the new digit blend?).
Tight date crops only -> minimal PHI; output local to ~/Desktop/ocr_demos/.

    python tools/verify_edit.py "/orig.pdf" "/edited.pdf" [page]
"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.append(os.path.expanduser("~/Documents/GitHub/PDFTextEditor-ocr"))

import numpy as np
import cv2
from PySide6.QtWidgets import QApplication
_APP = QApplication.instance() or QApplication([])

from pdftexteditor.document import PDFDocument
from pdftexteditor.ocr import pack, get_engine
pack.ensure_on_path()

OUT = os.path.expanduser("~/Desktop/ocr_demos")


def date_crops(path, pi):
    doc = PDFDocument(path)
    doc.normalize_orientations()
    rgb = doc.render_page_image(pi, 300.0)
    lines = get_engine("auto").recognize(rgb)
    crops = []
    for ln in lines:
        t = ln.text.replace(" ", "")
        if "/22/202" in t:
            x0, y0, x1, y1 = (int(v) for v in ln.bbox)
            pad = 6
            crop = rgb[max(0, y0 - pad):y1 + pad, max(0, x0 - pad):x1 + pad].copy()
            crops.append((y0, t, crop))
    doc.close()
    crops.sort(key=lambda c: c[0])
    return crops


def main():
    orig, edited = sys.argv[1], sys.argv[2]
    pi = int(sys.argv[3]) if len(sys.argv) > 3 else 2
    rows = []
    for tag, path in (("ORIGINAL", orig), ("EDITED", edited)):
        for _y, t, crop in date_crops(path, pi):
            h = 64
            sc = h / crop.shape[0]
            crop = cv2.resize(crop, (int(crop.shape[1] * sc), h))
            bar = np.full((20, max(crop.shape[1], 320), 3), 245, np.uint8)
            cv2.putText(bar, f"{tag}: {t}", (4, 15), cv2.FONT_HERSHEY_SIMPLEX,
                        0.5, (30, 30, 30), 1, cv2.LINE_AA)
            crop = np.pad(crop, ((0, 0), (0, max(0, bar.shape[1] - crop.shape[1])), (0, 0)),
                          constant_values=255)
            rows.append(np.vstack([bar, crop, np.full((8, crop.shape[1], 3), 200, np.uint8)]))
    w = max(r.shape[1] for r in rows)
    rows = [np.pad(r, ((0, 0), (0, w - r.shape[1]), (0, 0)), constant_values=255) for r in rows]
    os.makedirs(OUT, exist_ok=True)
    out = os.path.join(OUT, "date_edit_check.png")
    cv2.imwrite(out, cv2.cvtColor(np.vstack(rows), cv2.COLOR_RGB2BGR))
    print(f"saved {out}")


if __name__ == "__main__":
    main()
