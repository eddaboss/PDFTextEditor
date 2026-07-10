#!/usr/bin/env python3
"""Severity sweep of the speckling engine on the synth '6', vs the REAL 6, so we can SEE
which level matches instead of guessing. REAL | sev 0.06 | 0.12 | 0.20 | 0.30 | 0.42.

    python tools/sweep6.py
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
from pdftexteditor.ocr import recognize_and_reconstruct, degrade
from leaveoneout import apply_ocr

ORIG = os.path.expanduser("~/Downloads/doc05154920260624150538.pdf")
OUT = os.path.expanduser("~/Desktop/ocr_demos")
PI = 2


def tight(c):
    g = c.mean(2).astype(np.float32)
    cov = np.clip(255.0 - g, 0, None)
    cols = np.where(cov.sum(0) > cov.sum(0).max() * 0.06)[0]
    rows = np.where(cov.sum(1) > cov.sum(1).max() * 0.06)[0]
    return c[rows.min():rows.max() + 1, cols.min():cols.max() + 1] if cols.size and rows.size else c


def main():
    doc = PDFDocument(ORIG); doc.normalize_orientations()
    rgb = doc.render_page_image(PI, 300.0)
    res = recognize_and_reconstruct(rgb, 300.0, "", "", "auto", "p3")
    apply_ocr(doc, PI, res)
    norm = lambda t: (t or "").replace(" ", "")
    b = min([b for b in doc.new_boxes(PI) if "05/22/2026" in norm(b.text)], key=lambda b: len(norm(b.text)))
    ctx = doc.scan_edit_context(b, b.text)
    ink, paper = ctx["ink"], ctx["paper"]

    # real 6
    g6 = [g for g in (getattr(ctx.get("seg"), "glyphs", None) or []) if (g.char or "").strip() == "6"][-1]
    bh, bw = g6.bitmap.shape[:2]
    real6 = ctx["region"][max(0, int(g6.top_y)):int(g6.top_y) + bh, max(0, int(g6.x0)):int(g6.x0) + bw]

    # clean colored synth 6 (no damage)
    saved = ctx["dmg"]; ctx["dmg"] = None
    clean6, _ = doc._synth_strip(ctx, "6")
    ctx["dmg"] = saved
    doc.close()

    cells = [("REAL 6", real6)]
    for sev in (0.06, 0.12, 0.20, 0.30, 0.42):
        d = degrade.degrade_patch(clean6.astype(np.float32), np.asarray(ink, np.float32),
                                  np.asarray(paper, np.float32), sev, seed=7)
        cells.append((f"sev {sev:.2f}", tight(d)))

    out_cells = []
    for tag, img in cells:
        t = tight(img)
        z = cv2.resize(t, (t.shape[1] * 9, t.shape[0] * 9), interpolation=cv2.INTER_NEAREST)
        bar = np.full((24, max(z.shape[1], 150), 3), 245, np.uint8)
        cv2.putText(bar, tag, (5, 17), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (130, 60, 0), 1, cv2.LINE_AA)
        z = np.pad(z, ((0, 0), (0, max(0, bar.shape[1] - z.shape[1])), (0, 0)), constant_values=255)
        out_cells.append(np.vstack([bar, z]))
    H = max(c.shape[0] for c in out_cells)
    out_cells = [np.pad(c, ((0, H - c.shape[0]), (10, 10), (0, 0)), constant_values=255) for c in out_cells]
    os.makedirs(OUT, exist_ok=True)
    out = os.path.join(OUT, "sweep6.png")
    cv2.imwrite(out, cv2.cvtColor(np.hstack(out_cells), cv2.COLOR_RGB2BGR))
    print(f"saved {out}")


if __name__ == "__main__":
    main()
