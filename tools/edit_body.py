#!/usr/bin/env python3
"""Full-pipeline edit of a BODY-TEXT field: change text via the real stage_edit ->
compose_lines_block -> bake path (all sizing + font matching), then pixel-diff orig vs
edited to find the synth glyph and render REAL vs SYNTH. Name-free fields only. PHI local.

    python tools/edit_body.py "<substring of box text>" "<new full box text>"
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
import importlib.util
spec = importlib.util.spec_from_file_location("ed", "tools/edit_dates.py")
ed = importlib.util.module_from_spec(spec); spec.loader.exec_module(ed)

ORIG = os.path.expanduser("~/Downloads/doc05154920260624150538.pdf")
OUT = os.path.expanduser("~/Desktop/ocr_demos")
PI = 2


def render(path):
    d = PDFDocument(path); d.normalize_orientations(); r = d.render_page_image(PI, 300.0); d.close(); return r


def main():
    target, new = sys.argv[1], sys.argv[2]
    doc = PDFDocument(ORIG); doc.normalize_orientations()
    rgb = doc.render_page_image(PI, 300.0)
    res = recognize_and_reconstruct(rgb, 300.0, "", "", "auto", "p3")
    ed.apply_ocr(doc, PI, res)
    norm = lambda t: (t or "").replace(" ", "")
    box = next((b for b in doc.new_boxes(PI) if norm(target) in norm(b.text)), None)
    if box is None:
        print("target not found"); return
    print(f"editing box id={box.box_id} text={box.text!r} -> {new!r}")
    doc.stage_edit(PI, box, new)
    doc.save_as("/tmp/doc05154_body.pdf"); doc.close()

    a, b = render(ORIG), render("/tmp/doc05154_body.pdf")
    H = min(a.shape[0], b.shape[0]); W = min(a.shape[1], b.shape[1]); a, b = a[:H, :W], b[:H, :W]
    diff = np.abs(a.astype(np.int16) - b.astype(np.int16)).sum(2)
    ys, xs = np.where(diff > 40)
    if not len(ys):
        print("NO CHANGE detected"); return
    y0, y1, x0, x1 = ys.min(), ys.max(), xs.min(), xs.max()
    print(f"changed region x{x0}-{x1} y{y0}-{y1}")
    pad = 16
    ca = a[max(0, y0 - pad):y1 + pad, max(0, x0 - pad):x1 + pad]
    cb = b[max(0, y0 - pad):y1 + pad, max(0, x0 - pad):x1 + pad]
    z = max(1, int(round(220 / max(1, ca.shape[0]))))
    up = lambda im: cv2.resize(im, (im.shape[1] * z, im.shape[0] * z), interpolation=cv2.INTER_NEAREST)
    def lab(im, t):
        bar = np.full((20, max(im.shape[1], 170), 3), 245, np.uint8)
        cv2.putText(bar, t, (4, 14), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (130, 60, 0), 1, cv2.LINE_AA)
        im = np.pad(im, ((0, 0), (0, max(0, bar.shape[1] - im.shape[1])), (0, 0)), constant_values=255)
        return np.vstack([bar, im])
    L, Rr = lab(up(ca), "REAL body"), lab(up(cb), "FULL-PIPELINE synth")
    h = max(L.shape[0], Rr.shape[0])
    padh = lambda im: np.pad(im, ((0, h - im.shape[0]), (0, 0), (0, 0)), constant_values=255)
    out = np.hstack([padh(L), np.full((h, 14, 3), 235, np.uint8), padh(Rr)])
    os.makedirs(OUT, exist_ok=True)
    cv2.imwrite(os.path.join(OUT, "edit_body_check.png"), cv2.cvtColor(out, cv2.COLOR_RGB2BGR))
    print("saved", os.path.join(OUT, "edit_body_check.png"))


if __name__ == "__main__":
    main()
