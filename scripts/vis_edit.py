"""Show the ACTUAL in-place edit: change a digit, re-synthesize ONLY that digit in the
matched font (verify.match_by_synth), degraded to the field's own damage, and composite
it onto the original scan -- every other glyph stays the exact scan pixels.

  05/2026 -> 05/2028   (6 -> 8)
  5/16/2024 -> 5/12/2026  (6 -> 2, 4 -> 6)
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
from pdftexteditor.ocr import engine as E, fontbank as FB, degrade as DG, verify as V  # noqa: E402
from render_verify_date import split_cov, fit_em  # noqa: E402

PDF = os.path.expanduser("~/Downloads/doc05154920260624150538.pdf")
PG = 1
PPI = 300.0 / 72.0
EDITS = {"05/2026": "05/2028", "5/16/2024": "5/12/2026"}


def main():
    doc = PDFDocument(PDF)
    rgb = doc.render_page_image(PG, 300.0)
    lines = E.get_engine("auto").recognize(rgb)
    ttf = FB._ensure_ttf_cache()
    panels = []
    for ln in lines:
        t = ln.text.strip()
        if t not in EDITS:
            continue
        new_t = EDITS[t]
        x0, y0, x1, y1 = ln.bbox
        x0i, y0i = max(0, int(x0)), max(0, int(y0))
        x1i, y1i = min(rgb.shape[1], int(x1) + 1), min(rgb.shape[0], int(y1) + 1)
        region = rgb[y0i:y1i, x0i:x1i].copy()
        chars = [c for c in t if c.strip()]
        new_chars = [c for c in new_t if c.strip()]
        spans = split_cov(DG.coverage(region), len(chars))
        if not spans or len(chars) != len(new_chars):
            print(f"{t}: skip"); continue

        # the matched font for THIS field (what an edit would synthesize in)
        key = V.match_by_synth(doc, region, t, ttf, seeds=9)
        import fitz
        fname = fitz.Font(fontfile=os.path.join(ttf, key)).name if key else "?"
        fb = open(os.path.join(ttf, key), "rb").read()
        print(f"{t!r} -> {new_t!r}   (font: {fname})")

        scan_cov = DG.coverage(region)
        inkrows = np.where((scan_cov > 0.3).any(1))[0]
        h_ink = inkrows.max() - inkrows.min() + 1
        base_y = float(inkrows.max() + 1)
        ink, paper = DG.sample_ink_paper(region)
        Hr = region.shape[0]
        cb_all = [(a, 0, b, Hr) for (a, b) in spans]
        bf = DG.build_residual_filter(region, {"char_boxes": cb_all})
        print(f"   residual filter: {'NONE (=> plain crisp recolour, no speckle)' if bf is None else {k: (round(bf[k], 3) if isinstance(bf.get(k), float) else 'set') for k in ('grain_rate', 'hard_drop', 'target', 'bands')}}")
        out = region.copy()
        for i, (co, cn) in enumerate(zip(chars, new_chars)):
            if co == cn:
                continue                              # untouched glyph: stays EXACT scan
            a, b = spans[i]
            em = fit_em(fb, cn, h_ink)
            ctx = {"ppi": PPI, "Hr": Hr, "base_y": base_y, "paper": paper.astype(np.float32),
                   "ink": ink.astype(np.float32), "region": region,
                   "geom": {"char_boxes": [(a, 0, b, Hr) for (a, b) in spans]},
                   "rect": (x0i, y0i, x1i, y1i)}
            strip, advpx = doc._synth_strip(ctx, cn, em=em, font_bytes=fb, base_y=base_y)
            if t == "05/2026":   # save the raw replaced glyph + a scan glyph for comparison
                scan_g = region[:, spans[0][0]:spans[0][1]]
                zc = cv2.resize(scan_g, None, fx=6, fy=6, interpolation=cv2.INTER_NEAREST)
                zs = cv2.resize(strip, None, fx=6, fy=6, interpolation=cv2.INTER_NEAREST)
                hh = max(zc.shape[0], zs.shape[0])
                zc = np.pad(zc, ((0, hh - zc.shape[0]), (0, 0), (0, 0)), constant_values=255)
                zs = np.pad(zs, ((0, hh - zs.shape[0]), (0, 0), (0, 0)), constant_values=255)
                cv2.imwrite("/tmp/synth_glyph.png", cv2.cvtColor(
                    np.hstack([zc, np.full((hh, 12, 3), 255, np.uint8), zs]), cv2.COLOR_RGB2BGR))
            w = min(strip.shape[1], out.shape[1] - a)
            if w <= 0:
                continue
            # erase old glyph but KEEP the field's paper grain: fill the slot with real
            # scan-paper pixels (speckle and all) so the gap around the new glyph doesn't
            # read as a clean patch next to the grainy scan.
            bgpx = region[scan_cov < 0.12]
            if len(bgpx) >= 8:
                sl = out[:, a:b]
                ridx = np.random.randint(0, len(bgpx), sl.shape[0] * sl.shape[1])
                out[:, a:b] = bgpx[ridx].reshape(sl.shape[0], sl.shape[1], 3)
            else:
                out[:, a:b] = paper.astype(np.uint8)
            sc = (DG.coverage(strip)[:Hr, :w])[..., None]
            tgt = out[:Hr, a:a + w].astype(np.float32)
            out[:Hr, a:a + w] = np.clip(
                tgt * (1 - sc) + strip[:Hr, :w].astype(np.float32) * sc, 0, 255).astype(np.uint8)

        def lab(img, text):
            z = cv2.resize(img, None, fx=3, fy=3, interpolation=cv2.INTER_NEAREST)
            bar = np.full((22, z.shape[1], 3), 245, np.uint8)
            cv2.putText(bar, text, (4, 15), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (15, 15, 15), 1, cv2.LINE_AA)
            return np.vstack([bar, z, np.full((6, z.shape[1], 3), 200, np.uint8)])
        panels.append(lab(region, f"BEFORE  {t}"))
        panels.append(lab(out, f"AFTER   {new_t}   (only changed digits re-synthesized, {fname})"))
        panels.append(np.full((10, lab(out, '').shape[1], 3), 255, np.uint8))
    if panels:
        W = max(p.shape[1] for p in panels)
        panels = [np.pad(p, ((0, 0), (0, W - p.shape[1]), (0, 0)), constant_values=255) for p in panels]
        cv2.imwrite("/tmp/vis_edit.png", cv2.cvtColor(np.vstack(panels), cv2.COLOR_RGB2BGR))
        print("saved /tmp/vis_edit.png")
    doc.close()


if __name__ == "__main__":
    main()
