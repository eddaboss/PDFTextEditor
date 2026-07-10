#!/usr/bin/env python3
"""Show EXACTLY what the in-app edit changed: pixel-diff the original vs the edited
page, find the regions whose pixels actually changed (the synthesized glyphs -- the
rest of the scan is left byte-identical), and dump a zoomed ORIG | EDITED | DIFF
triptych per changed region. This reveals what the live pipeline really does, instead
of reconstructing it.

    python tools/show_inapp_edit.py [page]
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
DPI = 300.0


def render(path, pi):
    doc = PDFDocument(path)
    doc.normalize_orientations()
    rgb = doc.render_page_image(pi, DPI)
    doc.close()
    return rgb


def main():
    pi = int(sys.argv[1]) if len(sys.argv) > 1 else 2
    a = render(ORIG, pi)
    b = render(EDITED, pi)
    H = min(a.shape[0], b.shape[0]); W = min(a.shape[1], b.shape[1])
    a, b = a[:H, :W], b[:H, :W]
    diff = np.abs(a.astype(np.int16) - b.astype(np.int16)).sum(2)
    changed = (diff > 40).astype(np.uint8)
    changed = cv2.dilate(changed, np.ones((9, 25), np.uint8))   # join glyph runs
    n, lab, stats, _ = cv2.connectedComponentsWithStats(changed, 8)
    regions = [stats[i] for i in range(1, n) if stats[i, cv2.CC_STAT_AREA] > 300]
    print(f"page {pi}: {len(regions)} changed region(s)")
    os.makedirs(OUT, exist_ok=True)
    pad = 10
    for idx, st in enumerate(sorted(regions, key=lambda s: (s[1], s[0]))):
        x, y, w, h = st[0], st[1], st[2], st[3]
        X0, Y0 = max(0, x - pad), max(0, y - pad)
        X1, Y1 = min(W, x + w + pad), min(H, y + h + pad)
        ca, cb = a[Y0:Y1, X0:X1], b[Y0:Y1, X0:X1]
        cd = diff[Y0:Y1, X0:X1]
        heat = np.zeros_like(ca); heat[..., 0] = np.clip(cd, 0, 255)  # red where changed
        sc = max(1, int(round(260 / max(1, ca.shape[0]))))
        def up(img):
            return cv2.resize(img, None, fx=sc, fy=sc, interpolation=cv2.INTER_NEAREST)
        def lab_(img, t):
            bar = np.full((18, img.shape[1], 3), 245, np.uint8)
            cv2.putText(bar, t, (4, 13), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (130, 60, 0), 1, cv2.LINE_AA)
            return np.vstack([bar, img])
        sep = np.full((6, up(ca).shape[1], 3), 215, np.uint8)
        stack = np.vstack([lab_(up(ca), f"ORIG  region {idx} @({X0},{Y0})"), sep,
                           lab_(up(cb), "EDITED (in-app output)"), sep,
                           lab_(up(heat), "DIFF (red = pixels the app changed)")])
        out = os.path.join(OUT, f"inapp_edit_p{pi}_r{idx}.png")
        cv2.imwrite(out, cv2.cvtColor(stack, cv2.COLOR_RGB2BGR))
        print(f"  region {idx}: bbox=({x},{y},{w},{h}) area={st[cv2.CC_STAT_AREA]} -> {out}")


if __name__ == "__main__":
    main()
