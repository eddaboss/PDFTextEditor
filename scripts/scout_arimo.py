"""Ground-truth scout: the date font is Arimo (Arial-metric). Find every Arial-family
font in the bank, report its COARSE rank for each date, and its seed-averaged
render-and-compare SSIM rank (the real engine: render in candidate font, stamp the
degradation measured from the actual scan glyphs, compare per digit). Tells us whether
the verifier already ranks the true font #1 or needs tuning.

    python scripts/scout_arimo.py
"""
import json
import os
import re
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import numpy as np  # noqa: E402
import fitz  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

_app = QApplication.instance() or QApplication([])

from pdftexteditor.document import PDFDocument  # noqa: E402
from pdftexteditor.ocr import engine as E, supermatch as SM  # noqa: E402
from pdftexteditor.ocr import fontbank as FB, degrade as DG  # noqa: E402
from render_verify_date import split_cov, glyph_covs, compare_glyphs, fit_em  # noqa: E402

PDF = os.path.expanduser("~/Downloads/doc05154920260624150538.pdf")
PAGE = 1
PPI = 300.0 / 72.0
DATE_RE = re.compile(r"^\d{1,2}/\d{1,2}/\d{2,4}$|^\d{1,2}/\d{4}$")
FAMILY = re.compile(r"arimo|arial|liberation sans|nimbus sans|helvetica|roboto", re.I)
SCRATCH = os.environ.get("SCRATCH", "/tmp")


def name_index(keys, ttf):
    cache = os.path.join(SCRATCH, "bank_names.json")
    if os.path.exists(cache):
        return json.load(open(cache))
    idx = {}
    for i, k in enumerate(keys):
        try:
            idx[k] = fitz.Font(fontfile=os.path.join(ttf, k)).name or ""
        except Exception:
            idx[k] = ""
        if (i + 1) % 500 == 0:
            print(f"   names {i + 1}/{len(keys)}")
    json.dump(idx, open(cache, "w"))
    return idx


def main():
    doc = PDFDocument(PDF)
    rgb = doc.render_page_image(PAGE, 300.0)
    lines = E.get_engine("auto").recognize(rgb)
    keys = FB._load_fingerprints()["paths"]
    ttf = FB._ensure_ttf_cache()
    names = name_index(keys, ttf)
    fam = [k for k in keys if FAMILY.search(names.get(k, ""))]
    print(f"Arial-family fonts in bank: {len(fam)}")
    for k in fam[:30]:
        print(f"   {k}  {names[k]}")

    dates = [ln for ln in lines if DATE_RE.match(ln.text.strip())]
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
            print(f"{text!r}: no seg"); continue
        cells = [(chars[i], (x0i + a, y0i, x0i + b, y1i)) for i, (a, b) in enumerate(spans)]
        scan_glyphs = glyph_covs(scan_cov, spans)
        inkrows = np.where((scan_cov > 0.3).any(1))[0]
        h_ink = (inkrows.max() - inkrows.min() + 1) if len(inkrows) else region.shape[0]
        base_y = float(inkrows.max() + 1) if len(inkrows) else float(region.shape[0])
        ink, paper = DG.sample_ink_paper(region)
        char_boxes = [(a, 0, b, region.shape[0]) for (a, b) in spans]

        ms = SM._coarse_scores(rgb, cells)
        order = list(np.argsort(-ms))
        rank_of = {int(c): r for r, c in enumerate(order)}
        idx_of = {k: i for i, k in enumerate(keys)}
        print(f"\n=== {text!r} ===")
        print("Arial-family COARSE ranks:")
        fam_ranks = sorted((rank_of.get(idx_of[k], 10 ** 9), k) for k in fam if k in idx_of)
        for r, k in fam_ranks[:12]:
            print(f"   coarse#{r:<5} {k}  {names[k]}")

        # seed-averaged render-and-compare over the coarse top-40 UNION the whole
        # Arial family (force the true regular Arimo/Arial into the pool, since coarse
        # buries it at ~rank 1100 because degradation thickens the strokes).
        cand = list(dict.fromkeys([int(i) for i in order[:40]]
                                  + [idx_of[k] for k in fam if k in idx_of]))
        scored = []
        for ci in cand:
            try:
                fb = open(os.path.join(ttf, keys[ci]), "rb").read()
                em = fit_em(fb, text, h_ink)
                vals = []
                for t in range(9):
                    ctx = {"ppi": PPI, "Hr": region.shape[0], "base_y": base_y,
                           "paper": paper.astype(np.float32), "ink": ink.astype(np.float32),
                           "region": region, "geom": {"char_boxes": char_boxes},
                           "rect": (x0i, y0i, x1i, y1i, t)}
                    strip, _ = doc._synth_strip(ctx, text, em=em, font_bytes=fb, base_y=base_y)
                    vals.append(compare_glyphs(scan_glyphs, DG.coverage(strip), len(chars)))
                scored.append((float(np.mean(vals)), keys[ci]))
            except Exception:
                pass
        scored.sort(key=lambda t: -t[0])
        print("render-and-compare ranking (seed-avg SSIM, top 12; * = Arial-family):")
        for r, (s, k) in enumerate(scored[:12]):
            star = " *" if FAMILY.search(names.get(k, "")) else ""
            print(f"   ssim#{r:<3} {s:.3f}  {k}  {names[k]}{star}")
        fam_in = [(r, s, k) for r, (s, k) in enumerate(scored) if FAMILY.search(names.get(k, ""))]
        print(f"Arial-family in render ranking: {[(r, round(s, 3), k) for r, s, k in fam_in]}")
    doc.close()


if __name__ == "__main__":
    main()
