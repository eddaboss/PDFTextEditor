#!/usr/bin/env python3
"""Ground-truth comparison of the ACTUAL pipeline output, no reconstruction.

The in-app edit bumped two date fields, each by re-synthesizing ONE digit:
  field 1 @x=1269 : real '5' -> pipeline-synth '6'   (2025 -> 2026)
  field 2 @x=1594 : real '6' -> pipeline-synth '7'   (2026 -> 2027)

So the page gives a clean ground-truth pair for the digit '6': the pipeline SYNTH '6'
(edited, field 1) vs a REAL scanned '6' (original, field 2, before it became '7') --
same digit, same font, same page. We crop both straight from the rendered original and
the delivered edited PDF and lay them side by side, plus the before/after at field 1.

    python tools/compare_pipeline.py
"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import cv2
from PySide6.QtWidgets import QApplication
_APP = QApplication.instance() or QApplication([])

from pdftexteditor.document import PDFDocument

ORIG = os.path.expanduser("~/Downloads/doc05154920260624150538.pdf")
EDITED = os.path.expanduser("~/Desktop/doc05154_edited.pdf")
OUT = os.path.expanduser("~/Desktop/ocr_demos")
PI = 2

# last-digit cells found by the orig-vs-edited pixel diff (show_inapp_edit.py)
CELL_F1 = (1269, 1171, 50, 47)   # field 1 last digit: orig '5' / edited synth '6'
CELL_F2 = (1594, 1171, 49, 45)   # field 2 last digit: orig real '6' / edited synth '7'


def render(path):
    doc = PDFDocument(path)
    doc.normalize_orientations()
    rgb = doc.render_page_image(PI, 300.0)
    doc.close()
    return rgb


def crop(img, cell, pad=5):
    x, y, w, h = cell
    return img[max(0, y - pad):y + h + pad, max(0, x - pad):x + w + pad].copy()


def cell_img(img, title, H=240):
    sc = H / img.shape[0]
    up = cv2.resize(img, (int(round(img.shape[1] * sc)), H), interpolation=cv2.INTER_NEAREST)
    up = np.pad(up, ((0, 0), (0, max(0, 200 - up.shape[1])), (0, 0)), constant_values=255)
    bar = np.full((22, up.shape[1], 3), 245, np.uint8)
    cv2.putText(bar, title, (5, 15), cv2.FONT_HERSHEY_SIMPLEX, 0.42, (130, 60, 0), 1, cv2.LINE_AA)
    return np.vstack([bar, up])


def hjoin(cells, gap=14):
    H = max(c.shape[0] for c in cells)
    cells = [np.pad(c, ((0, H - c.shape[0]), (0, 0), (0, 0)), constant_values=255) for c in cells]
    sepc = np.full((H, gap, 3), 235, np.uint8)
    row = []
    for i, c in enumerate(cells):
        row.append(c)
        if i < len(cells) - 1:
            row.append(sepc)
    return np.hstack(row)


def banner(text, w):
    b = np.full((26, w, 3), 225, np.uint8)
    cv2.putText(b, text, (6, 18), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (40, 40, 40), 1, cv2.LINE_AA)
    return b


def main():
    a = render(ORIG)
    b = render(EDITED)

    real6 = cell_img(crop(a, CELL_F2), "REAL '6' (scanned, field 2)")
    synth6 = cell_img(crop(b, CELL_F1), "SYNTH '6' (actual pipeline, field 1)")
    real5 = cell_img(crop(a, CELL_F1), "BEFORE: real '5' (field 1)")
    synth6b = cell_img(crop(b, CELL_F1), "AFTER: synth '6' (field 1)")
    synth7 = cell_img(crop(b, CELL_F2), "SYNTH '7' (actual pipeline, field 2)")
    real2nbr = cell_img(crop(a, (CELL_F2[0] - 52, CELL_F2[1], 50, CELL_F2[3])), "REAL '2' neighbour")

    gt = hjoin([real6, synth6])
    ba = hjoin([real5, synth6b])
    nb = hjoin([real2nbr, synth7])
    W = max(gt.shape[1], ba.shape[1], nb.shape[1])
    pad = lambda im: np.pad(im, ((0, 0), (0, W - im.shape[1]), (0, 0)), constant_values=255)
    sep = np.full((10, W, 3), 245, np.uint8)
    stack = np.vstack([
        banner("GROUND TRUTH: same digit '6', real scan vs actual-pipeline synth", W), pad(gt), sep,
        banner("BEFORE / AFTER at field 1 (the cell the pipeline rewrote)", W), pad(ba), sep,
        banner("SYNTH '7' vs a real scanned neighbour digit (texture check)", W), pad(nb),
    ])
    os.makedirs(OUT, exist_ok=True)
    out = os.path.join(OUT, "compare_pipeline_real_vs_synth.png")
    cv2.imwrite(out, cv2.cvtColor(stack, cv2.COLOR_RGB2BGR))
    print(f"saved {out}")


if __name__ == "__main__":
    main()
