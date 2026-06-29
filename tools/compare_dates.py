#!/usr/bin/env python3
"""Line up a REAL scanned date against the SYNTHESIZED one of the same string, so the
synth can be judged against ground truth. Original page has a real '05/22/2026'
field; the edited page has the synthesized '05/22/2026' (the field bumped from 2025,
so only its last digit is synthesized, the rest are real scan pixels).

    python tools/compare_dates.py "/orig.pdf" "/edited.pdf" 05/22/2026 [page]
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


def date_crop(path, target, pi):
    """Tight crop of the standalone line whose text == target (closest match)."""
    doc = PDFDocument(path)
    doc.normalize_orientations()
    rgb = doc.render_page_image(pi, 300.0)
    lines = get_engine("auto").recognize(rgb)
    norm = lambda s: s.replace(" ", "")
    cands = [ln for ln in lines if target in norm(ln.text)]
    if not cands:
        doc.close()
        return None
    # the standalone date field = the line whose text is closest to JUST the target
    ln = min(cands, key=lambda l: len(norm(l.text)))
    x0, y0, x1, y1 = (int(v) for v in ln.bbox)
    pad = 8
    crop = rgb[max(0, y0 - pad):y1 + pad, max(0, x0 - pad):x1 + pad].copy()
    doc.close()
    return crop


def main():
    orig, edited, target = sys.argv[1], sys.argv[2], sys.argv[3]
    pi = int(sys.argv[4]) if len(sys.argv) > 4 else 2
    real = date_crop(orig, target, pi)
    synth = date_crop(edited, target, pi)
    if real is None or synth is None:
        print(f"real={real is not None} synth={synth is not None} -- target not found")
        return
    H = 130
    rows = []
    for tag, crop, col in (("REAL  (scanned)   " + target, real, (150, 80, 0)),
                           ("SYNTH (this app)  " + target, synth, (20, 110, 20))):
        sc = H / crop.shape[0]
        c = cv2.resize(crop, (int(crop.shape[1] * sc), H), interpolation=cv2.INTER_CUBIC)
        bar = np.full((22, max(c.shape[1], 360), 3), 245, np.uint8)
        cv2.putText(bar, tag, (6, 16), cv2.FONT_HERSHEY_SIMPLEX, 0.5, col, 1, cv2.LINE_AA)
        c = np.pad(c, ((0, 0), (0, max(0, bar.shape[1] - c.shape[1])), (0, 0)),
                   constant_values=255)
        rows.append(bar)
        rows.append(c)
        rows.append(np.full((10, c.shape[1], 3), 200, np.uint8))
    w = max(r.shape[1] for r in rows)
    rows = [np.pad(r, ((0, 0), (0, w - r.shape[1]), (0, 0)), constant_values=255) for r in rows]
    os.makedirs(OUT, exist_ok=True)
    out = os.path.join(OUT, "date_real_vs_synth.png")
    cv2.imwrite(out, cv2.cvtColor(np.vstack(rows), cv2.COLOR_RGB2BGR))
    print(f"saved {out}")


if __name__ == "__main__":
    main()
