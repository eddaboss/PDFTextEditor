"""Test the ACTUAL verify.py feature-weighted degrade-matched score (not compare_glyphs)
on the dates, seed-averaged, and report Arial Bold's rank. Run it a couple of times to
see if it's stable.
"""
import os
import re
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import numpy as np  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

_app = QApplication.instance() or QApplication([])

from pdftexteditor.document import PDFDocument  # noqa: E402
from pdftexteditor.ocr import engine as E, supermatch as SM  # noqa: E402
from pdftexteditor.ocr import fontbank as FB, degrade as DG, verify as V  # noqa: E402
from render_verify_date import split_cov  # noqa: E402

PDF = os.path.expanduser("~/Downloads/doc05154920260624150538.pdf")
PG = 1
DATE_RE = re.compile(r"^\d{1,2}/\d{1,2}/\d{2,4}$|^\d{1,2}/\d{4}$")
SEEDS = 9


def main():
    doc = PDFDocument(PDF)
    rgb = doc.render_page_image(PG, 300.0)
    lines = E.get_engine("auto").recognize(rgb)
    keys = FB._load_fingerprints()["paths"]
    ttf = FB._ensure_ttf_cache()
    import fitz
    for ln in lines:
        t = ln.text.strip()
        if not DATE_RE.match(t):
            continue
        x0, y0, x1, y1 = ln.bbox
        x0i, y0i = max(0, int(x0)), max(0, int(y0))
        x1i, y1i = min(rgb.shape[1], int(x1) + 1), min(rgb.shape[0], int(y1) + 1)
        region = rgb[y0i:y1i, x0i:x1i].copy()
        chars = [c for c in t if c.strip()]
        spans = split_cov(DG.coverage(region), len(chars))
        if not spans:
            continue
        cells = [(chars[i], (x0i + a, y0i, x0i + b, y1i)) for i, (a, b) in enumerate(spans)]
        char_boxes = [(a, 0, b, region.shape[0]) for (a, b) in spans]
        prof = DG.build_residual_filter(region, {"char_boxes": char_boxes})
        ink, paper = DG.sample_ink_paper(region)
        rep = V.scan_word_repr(rgb, cells)
        ms = SM._coarse_scores(rgb, cells)
        cand = [int(i) for i in np.argsort(-ms)[:40]]
        scored = []
        for ci in cand:
            vals = []
            for s in range(SEEDS):
                m = V.score_against(rep, os.path.join(ttf, keys[ci]),
                                    residual=(prof, ink, paper, s) if prof else None)
                if m is not None:
                    vals.append(V.combined(m))
            if vals:
                scored.append((float(np.mean(vals)), keys[ci]))
        scored.sort(key=lambda x: -x[0])
        print(f"\n{t!r}: verify feature-weighted score, top 6:")
        for s, k in scored[:6]:
            nm = fitz.Font(fontfile=os.path.join(ttf, k)).name
            star = " <== ARIAL BOLD" if k == "00121.ttf" else ""
            print(f"   {s:.3f}  {k}  {nm}{star}")
        abr = [r for r, (s, k) in enumerate(scored) if k == "00121.ttf"]
        print(f"   Arial Bold (00121) rank: {abr[0] if abr else 'not in top40'}")
    doc.close()


if __name__ == "__main__":
    main()
