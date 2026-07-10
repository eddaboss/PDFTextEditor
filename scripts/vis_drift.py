"""For each date, rank the fonts (seed-averaged degrade-matched compare), then render the
date in EVERY font ranked above Arial Bold, plus Arial Bold and the scan, so we can judge
whether the drift from the true font is acceptable.
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
import fitz  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

_app = QApplication.instance() or QApplication([])

from pdftexteditor.document import PDFDocument  # noqa: E402
from pdftexteditor.ocr import engine as E, supermatch as SM  # noqa: E402
from pdftexteditor.ocr import fontbank as FB, degrade as DG  # noqa: E402
from render_verify_date import split_cov, glyph_covs, compare_glyphs, fit_em  # noqa: E402

PDF = os.path.expanduser("~/Downloads/doc05154920260624150538.pdf")
PG = 1
PPI = 300.0 / 72.0
DATE_RE = re.compile(r"^\d{1,2}/\d{1,2}/\d{2,4}$|^\d{1,2}/\d{4}$")
SEEDS = 9
AB = "00121.ttf"


def render_clean(key, text, ttf, h=56):
    f = fitz.Font(fontfile=os.path.join(ttf, key))
    em = h * 0.66
    W = int(f.text_length(text, em) + em)
    doc = fitz.open(); pg = doc.new_page(width=W, height=h)
    tw = fitz.TextWriter(pg.rect)
    tw.append((em * 0.3, h * 0.74), text, font=f, fontsize=em)
    tw.write_text(pg, color=(0, 0, 0))
    pm = pg.get_pixmap(alpha=False)
    return np.frombuffer(pm.samples, np.uint8).reshape(pm.height, pm.width, pm.n)[..., :3].copy()


def main():
    doc = PDFDocument(PDF)
    rgb = doc.render_page_image(PG, 300.0)
    lines = E.get_engine("auto").recognize(rgb)
    keys = FB._load_fingerprints()["paths"]
    ttf = FB._ensure_ttf_cache()
    panels = []
    for ln in lines:
        t = ln.text.strip()
        if not DATE_RE.match(t):
            continue
        x0, y0, x1, y1 = ln.bbox
        x0i, y0i = max(0, int(x0)), max(0, int(y0))
        x1i, y1i = min(rgb.shape[1], int(x1) + 1), min(rgb.shape[0], int(y1) + 1)
        region = rgb[y0i:y1i, x0i:x1i].copy()
        chars = [c for c in t if c.strip()]
        scan_cov = DG.coverage(region)
        spans = split_cov(scan_cov, len(chars))
        if not spans:
            continue
        cells = [(chars[i], (x0i + a, y0i, x0i + b, y1i)) for i, (a, b) in enumerate(spans)]
        scan_glyphs = glyph_covs(scan_cov, spans)
        inkrows = np.where((scan_cov > 0.3).any(1))[0]
        h_ink = inkrows.max() - inkrows.min() + 1
        base_y = float(inkrows.max() + 1)
        ink, paper = DG.sample_ink_paper(region)
        char_boxes = [(a, 0, b, region.shape[0]) for (a, b) in spans]
        ms = SM._coarse_scores(rgb, cells)
        cand = [int(i) for i in np.argsort(-ms)[:40]]
        if FB._CIDX and AB in keys:
            ab_idx = keys.index(AB)
            if ab_idx not in cand:
                cand.append(ab_idx)
        scored = []
        for ci in cand:
            try:
                fb = open(os.path.join(ttf, keys[ci]), "rb").read()
                em = fit_em(fb, t, h_ink)
                vals = []
                for s in range(SEEDS):
                    ctx = {"ppi": PPI, "Hr": region.shape[0], "base_y": base_y,
                           "paper": paper.astype(np.float32), "ink": ink.astype(np.float32),
                           "region": region, "geom": {"char_boxes": char_boxes},
                           "rect": (x0i, y0i, x1i, y1i, s)}
                    strip, _ = doc._synth_strip(ctx, t, em=em, font_bytes=fb, base_y=base_y)
                    vals.append(compare_glyphs(scan_glyphs, DG.coverage(strip), len(chars)))
                scored.append((float(np.mean(vals)), keys[ci]))
            except Exception:
                pass
        scored.sort(key=lambda x: -x[0])
        ab_rank = next((r for r, (s, k) in enumerate(scored) if k == AB), len(scored))
        above = [(s, k) for (s, k) in scored[:ab_rank]]
        print(f"\n{t!r}: Arial Bold rank {ab_rank}; {len(above)} fonts above it")

        w = 460

        def row(img, label):
            img = np.pad(img, ((0, 0), (0, max(0, w - img.shape[1])), (0, 0)), constant_values=255) \
                if img.shape[1] < w else img[:, :w]
            bar = np.full((20, w, 3), 245, np.uint8)
            cv2.putText(bar, label, (4, 14), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (15, 15, 15), 1, cv2.LINE_AA)
            return np.vstack([bar, img, np.full((4, w, 3), 210, np.uint8)])
        panels.append(row(cv2.resize(region, None, fx=2, fy=2, interpolation=cv2.INTER_NEAREST),
                          f"=== {t}  SCAN ==="))
        for s, k in above:
            nm = fitz.Font(fontfile=os.path.join(ttf, k)).name
            panels.append(row(render_clean(k, t, ttf), f"{s:.3f}  {nm}"))
        nm = fitz.Font(fontfile=os.path.join(ttf, AB)).name
        panels.append(row(render_clean(AB, t, ttf), f"{dict(scored).get(AB,0):.3f}  {nm}  (TRUE)"))
        panels.append(np.full((10, w, 3), 255, np.uint8))
    if panels:
        W = max(p.shape[1] for p in panels)
        panels = [np.pad(p, ((0, 0), (0, W - p.shape[1]), (0, 0)), constant_values=255) for p in panels]
        cv2.imwrite("/tmp/drift.png", cv2.cvtColor(np.vstack(panels), cv2.COLOR_RGB2BGR))
        print("\nsaved /tmp/drift.png")
    doc.close()


if __name__ == "__main__":
    main()
