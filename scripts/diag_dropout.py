"""Quantify, for the 05/2026 date, the two degradation-fidelity gaps Edward flagged:
(1) background grain too heavy vs the real (tinted) paper, and (2) hard per-pixel
dropouts (missing ink pixels) inside the strokes that the soft synth fade misses.
Zooms scan vs Arial Bold synth and prints interior-dropout rate + background grain.
"""
import os
import re
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import numpy as np  # noqa: E402
import cv2  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

_app = QApplication.instance() or QApplication([])

from pdftexteditor.document import PDFDocument  # noqa: E402
from pdftexteditor.ocr import engine as E, fontbank as FB, degrade as DG  # noqa: E402
from render_verify_date import split_cov, fit_em  # noqa: E402

PDF = os.path.expanduser("~/Downloads/doc05154920260624150538.pdf")
PPI = 300.0 / 72.0


def stats(cov, label):
    ink = (cov > 0.4).astype(np.uint8)
    # the INTENDED solid stroke = close the ink (fill its own holes); dropout = pixels
    # inside that intended shape that are actually missing (white).
    footprint = cv2.morphologyEx(ink, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8)) > 0
    nfoot = int(footprint.sum())
    drop = int((footprint & (cov < 0.3)).sum())
    bg = ~(cv2.dilate(ink, np.ones((5, 5), np.uint8)) > 0)
    nbg = int(bg.sum())
    bg_speck = int(((cov > 0.08) & bg).sum())
    print(f"  {label:10}: footprint={nfoot:5d} dropout={drop / max(nfoot,1)*100:5.1f}%  "
          f"bg={nbg:5d} bg_grain={bg_speck / max(nbg,1)*100:5.1f}%")


def zoom(rgb, k=7):
    return cv2.resize(rgb, (rgb.shape[1] * k, rgb.shape[0] * k), interpolation=cv2.INTER_NEAREST)


def main():
    doc = PDFDocument(PDF)
    rgb = doc.render_page_image(1, 300.0)
    lines = E.get_engine("auto").recognize(rgb)
    ttf = FB._ensure_ttf_cache()
    ln = next(l for l in lines if re.match(r"^\d{1,2}/\d{4}$", l.text.strip()))
    text = ln.text.strip()
    x0, y0, x1, y1 = ln.bbox
    x0i, y0i = max(0, int(x0)), max(0, int(y0))
    x1i, y1i = min(rgb.shape[1], int(x1) + 1), min(rgb.shape[0], int(y1) + 1)
    region = rgb[y0i:y1i, x0i:x1i].copy()
    chars = [c for c in text if c.strip()]
    scan_cov = DG.coverage(region)
    spans = split_cov(scan_cov, len(chars))
    inkrows = np.where((scan_cov > 0.3).any(1))[0]
    h_ink = inkrows.max() - inkrows.min() + 1
    base_y = float(inkrows.max() + 1)
    gmean = region.mean(2)
    dark = gmean < float(np.percentile(gmean, 85)) * 0.7
    ink, paper = DG.sample_ink_paper(region, dark)
    cb = [(a, 0, b, region.shape[0]) for (a, b) in spans]

    fb = open(os.path.join(ttf, "00121.ttf"), "rb").read()
    em = fit_em(fb, text, h_ink)
    ctx = {"ppi": PPI, "Hr": region.shape[0], "base_y": base_y,
           "paper": paper.astype(np.float32), "ink": ink.astype(np.float32),
           "region": region, "geom": {"char_boxes": cb}, "rect": (x0i, y0i, x1i, y1i)}
    strip, _ = doc._synth_strip(ctx, text, em=em, font_bytes=fb, base_y=base_y)
    synth_cov = DG.coverage(strip)

    prof = DG.build_residual_filter(region, {"char_boxes": cb})
    print(f"{text!r}: measured grain_rate={prof.get('grain_rate'):.3f} "
          f"hard_drop={prof.get('hard_drop'):.3f}")
    stats(scan_cov, "SCAN")
    stats(synth_cov, "SYNTH AB")

    h = max(region.shape[0], strip.shape[0])
    w = max(region.shape[1], strip.shape[1])

    def fit(im):
        return np.pad(im, ((0, h - im.shape[0]), (0, w - im.shape[1]), (0, 0)),
                      constant_values=255)
    sa, sb = fit(region), fit(strip)
    out = np.vstack([zoom(sa), np.full((6, w * 7, 3), 200, np.uint8), zoom(sb)])
    cv2.imwrite("/tmp/dropout.png", cv2.cvtColor(out, cv2.COLOR_RGB2BGR))
    print("saved /tmp/dropout.png (top=scan, bottom=synth Arial Bold, 7x)")
    doc.close()


if __name__ == "__main__":
    main()
