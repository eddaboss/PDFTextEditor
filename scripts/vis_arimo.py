"""Visual truth check: render the date in specific Arial-family fonts through the real
synth+degrade engine and lay them next to the scan, so we can SEE whether (a) the
degrader reproduces the scan's stroke thickness and (b) which weight matches.
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
from render_verify_date import split_cov, glyph_covs, compare_glyphs, fit_em  # noqa: E402

PDF = os.path.expanduser("~/Downloads/doc05154920260624150538.pdf")
PPI = 300.0 / 72.0
DATE_RE = re.compile(r"^\d{1,2}/\d{1,2}/\d{2,4}$|^\d{1,2}/\d{4}$")
SHOW = [("00121.ttf", "Arial Bold"), ("01404.ttf", "Arimo Regular")]


def gated_ink(region, scan_cov):
    """Ink from the stroke CORES, gating the white dropout specks. Take the pixels deep
    inside the strokes (coverage > 0.7 == the solid ink, excludes specks and the light
    antialiased edges) and median them -- the true dark ink, not the speck-polluted mean."""
    core = scan_cov > 0.7
    if int(core.sum()) < 10:
        core = scan_cov > 0.5
    px = region[core].astype(np.float32)
    return np.median(px, axis=0) if len(px) else None


def main():
    doc = PDFDocument(PDF)
    rgb = doc.render_page_image(1, 300.0)
    lines = E.get_engine("auto").recognize(rgb)
    ttf = FB._ensure_ttf_cache()
    dates = [ln for ln in lines if DATE_RE.match(ln.text.strip())]
    panels = []
    for ln in dates:
        text = ln.text.strip()
        x0, y0, x1, y1 = ln.bbox
        x0i, y0i = max(0, int(x0)), max(0, int(y0))
        x1i, y1i = min(rgb.shape[1], int(x1) + 1), min(rgb.shape[0], int(y1) + 1)
        region = rgb[y0i:y1i, x0i:x1i].copy()
        chars = [c for c in text if c.strip()]
        scan_cov = DG.coverage(region)
        spans = split_cov(scan_cov, len(chars))
        if not spans:
            continue
        scan_glyphs = glyph_covs(scan_cov, spans)
        inkrows = np.where((scan_cov > 0.3).any(1))[0]
        h_ink = inkrows.max() - inkrows.min() + 1
        base_y = float(inkrows.max() + 1)
        # the edit path's own dark mask, fed to the now-fixed engine sample_ink_paper
        gmean = region.mean(2)
        dark = gmean < float(np.percentile(gmean, 85)) * 0.7
        ink, paper = DG.sample_ink_paper(region, dark)
        cb = [(a, 0, b, region.shape[0]) for (a, b) in spans]
        core = region[(scan_cov > 0.7)].astype(np.float32)
        scan_core = np.round(np.median(core, 0), 0) if len(core) else None
        print(f"\n{text!r}: engine ink (fixed)={np.round(ink,0)}  scan_core_grey={scan_core}")
        w = max(region.shape[1] * 2, 420)

        def lab(img, t):
            img = np.pad(img, ((0, 0), (0, max(0, w - img.shape[1])), (0, 0)), constant_values=255) \
                if img.shape[1] < w else img[:, :w]
            bar = np.full((20, w, 3), 245, np.uint8)
            cv2.putText(bar, t, (4, 14), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (15, 15, 15), 1, cv2.LINE_AA)
            return np.vstack([bar, img, np.full((4, w, 3), 205, np.uint8)])
        panels.append(lab(region, f"SCAN  {text}"))
        for key, nm in SHOW:
            fb = open(os.path.join(ttf, key), "rb").read()
            em = fit_em(fb, text, h_ink)
            ctx = {"ppi": PPI, "Hr": region.shape[0], "base_y": base_y,
                   "paper": paper.astype(np.float32), "ink": ink.astype(np.float32),
                   "region": region, "geom": {"char_boxes": cb},
                   "rect": (x0i, y0i, x1i, y1i)}
            strip, _ = doc._synth_strip(ctx, text, em=em, font_bytes=fb, base_y=base_y)
            s = compare_glyphs(scan_glyphs, DG.coverage(strip), len(chars))
            panels.append(lab(strip, f"{nm}  (fixed ink)  ssim={s:.3f}"))
        panels.append(np.full((10, w, 3), 255, np.uint8))
    W = max(p.shape[1] for p in panels)
    panels = [np.pad(p, ((0, 0), (0, W - p.shape[1]), (0, 0)), constant_values=255) for p in panels]
    cv2.imwrite("/tmp/arimo_vis.png", cv2.cvtColor(np.vstack(panels), cv2.COLOR_RGB2BGR))
    print("saved /tmp/arimo_vis.png")
    doc.close()


if __name__ == "__main__":
    main()
