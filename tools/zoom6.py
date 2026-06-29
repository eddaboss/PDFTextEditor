#!/usr/bin/env python3
"""Diagnose the synth '6': REAL 6 | CLEAN matched-font 6 (no damage) | DEGRADED synth 6,
zoomed large. If the CLEAN font 6 already differs in shape from the REAL 6, it is a FONT
problem (no degradation can fix it). If the clean shape matches but the degraded one looks
wrong, it is a DEGRADATION problem.

    python tools/zoom6.py
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
from leaveoneout import apply_ocr

ORIG = os.path.expanduser("~/Downloads/doc05154920260624150538.pdf")
OUT = os.path.expanduser("~/Desktop/ocr_demos")
PI = 2


def tight(c):
    g = c.mean(2).astype(np.float32)
    cov = np.clip(255.0 - g, 0, None)
    cols = np.where(cov.sum(0) > cov.sum(0).max() * 0.06)[0]
    rows = np.where(cov.sum(1) > cov.sum(1).max() * 0.06)[0]
    if cols.size and rows.size:
        return c[rows.min():rows.max() + 1, cols.min():cols.max() + 1]
    return c


def main():
    doc = PDFDocument(ORIG); doc.normalize_orientations()
    rgb = doc.render_page_image(PI, 300.0)
    res = recognize_and_reconstruct(rgb, 300.0, "", "", "auto", "p3")
    apply_ocr(doc, PI, res)
    norm = lambda t: (t or "").replace(" ", "")
    b = min([b for b in doc.new_boxes(PI) if "05/22/2026" in norm(b.text)],
            key=lambda b: len(norm(b.text)))
    ctx = doc.scan_edit_context(b, b.text)

    # REAL 6 = rightmost digit of the real line region
    region = ctx["region"]
    glyphs = [g for g in (getattr(ctx.get("seg"), "glyphs", None) or []) if (g.char or "").strip() == "6"]
    g6 = glyphs[-1] if glyphs else None
    if g6 is not None:
        bh, bw = g6.bitmap.shape[:2]
        x0, y0 = int(round(g6.x0)), int(round(g6.top_y))
        real6 = region[max(0, y0):y0 + bh, max(0, x0):x0 + bw]
    else:
        real6 = region[:, -region.shape[0]:]

    # CLEAN matched-font 6 (force the crisp recolour by disabling the filter) vs DEGRADED
    from pdftexteditor.ocr import degrade as _dg
    prof = _dg.build_residual_filter(ctx["region"], ctx.get("geom"))
    bands = (prof or {}).get("bands", {})
    bg = (prof or {}).get("bg")
    print("residual by distance:", {k: round(float(v.mean()), 2) for k, v in bands.items()},
          "| speck density=%.3f darkness~%.2f" % ((bg["density"], float(bg["vals"].mean())) if bg else (0, 0)))
    ctx["_bfilt"] = None                      # crisp recolour, no degradation
    clean6, _ = doc._synth_strip(ctx, "6")
    ctx.pop("_bfilt", None)                    # rebuild + apply the inverted-map filter
    deg6, _ = doc._synth_strip(ctx, "6")
    doc.close()

    cells = []
    for img, tag in ((real6, "REAL 6"), (tight(clean6), "CLEAN font 6 (no damage)"), (tight(deg6), "DEGRADED synth 6")):
        t = tight(img)
        z = cv2.resize(t, (t.shape[1] * 9, t.shape[0] * 9), interpolation=cv2.INTER_NEAREST)
        bar = np.full((26, max(z.shape[1], 260), 3), 245, np.uint8)
        cv2.putText(bar, tag, (6, 18), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (130, 60, 0), 1, cv2.LINE_AA)
        z = np.pad(z, ((0, 0), (0, max(0, bar.shape[1] - z.shape[1])), (0, 0)), constant_values=255)
        cells.append(np.vstack([bar, z]))
    H = max(c.shape[0] for c in cells)
    cells = [np.pad(c, ((0, H - c.shape[0]), (12, 12), (0, 0)), constant_values=255) for c in cells]
    os.makedirs(OUT, exist_ok=True)
    out = os.path.join(OUT, "zoom6_diag.png")
    cv2.imwrite(out, cv2.cvtColor(np.hstack(cells), cv2.COLOR_RGB2BGR))
    print(f"saved {out}")


if __name__ == "__main__":
    main()
