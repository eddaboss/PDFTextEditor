#!/usr/bin/env python3
"""Put the REAL scanned 05/22/2026 next to the SAME string synthesized through the
CURRENT (unchanged) app pipeline -- the actual _synth_strip path (matched font ->
recolour to scan ink/paper -> apply_measured_damage, calibrated to this line's own
neighbours). Ground truth: 05/22/2026 is a real field on page 3 (the one the app later
bumped to 2027), so the real pixels are right there to compare against.

    python tools/show_date_pipeline.py
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
from pdftexteditor.ocr import recognize_and_reconstruct
from leaveoneout import apply_ocr            # same OCR-box application the app uses

ORIG = os.path.expanduser("~/Downloads/doc05154920260624150538.pdf")
OUT = os.path.expanduser("~/Desktop/ocr_demos")
TARGET = "05/22/2026"
PI = 2


def main():
    doc = PDFDocument(ORIG)
    doc.normalize_orientations()
    rgb = doc.render_page_image(PI, 300.0)
    res = recognize_and_reconstruct(rgb, 300.0, "", "", "auto", "p3")
    apply_ocr(doc, PI, res)
    norm = lambda t: (t or "").replace(" ", "")
    cands = [b for b in doc.new_boxes(PI) if TARGET in norm(b.text)]
    if not cands:
        print("date box not found"); return
    b = min(cands, key=lambda b: len(norm(b.text)))
    ctx = doc.scan_edit_context(b, b.text)
    if ctx is None:
        print("no scan_edit_context"); return
    real = ctx["region"]                                   # real scanned line
    synth, _ = doc._synth_strip(ctx, TARGET)               # CURRENT pipeline synth of same string
    print(f"box text={b.text!r}  real{real.shape}  synth{synth.shape}  dmg={'measured' if ctx.get('dmg') is not None else 'None(crisp)'}")

    def row(img, tag, H=150):
        sc = H / img.shape[0]
        up = cv2.resize(img, (int(img.shape[1] * sc), H), interpolation=cv2.INTER_NEAREST)
        bar = np.full((26, max(up.shape[1], 520), 3), 245, np.uint8)
        cv2.putText(bar, tag, (8, 18), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (130, 60, 0), 2, cv2.LINE_AA)
        up = np.pad(up, ((0, 0), (0, max(0, bar.shape[1] - up.shape[1])), (0, 0)), constant_values=255)
        return np.vstack([bar, up])

    r1 = row(real, "REAL (scanned)            05/22/2026")
    r2 = row(synth, "SYNTH (current app pipeline)  05/22/2026")
    W = max(r1.shape[1], r2.shape[1])
    pad = lambda im: np.pad(im, ((0, 0), (0, W - im.shape[1]), (0, 0)), constant_values=255)
    gap = np.full((12, W, 3), 210, np.uint8)
    stack = np.vstack([pad(r1), gap, pad(r2)])
    os.makedirs(OUT, exist_ok=True)
    out = os.path.join(OUT, "date_pipeline_real_vs_synth.png")
    cv2.imwrite(out, cv2.cvtColor(stack, cv2.COLOR_RGB2BGR))
    print(f"saved {out}")


if __name__ == "__main__":
    main()
