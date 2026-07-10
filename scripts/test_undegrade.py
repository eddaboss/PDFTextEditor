"""Flip the matching: instead of degrading candidates to match the scan, UN-degrade the
scanned glyphs (despeckle + fill dropouts to recover the clean letterform) and compare
clean-to-clean against clean candidate renders. Reports where Arial Bold lands.
"""
import os
import re
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import numpy as np  # noqa: E402
import cv2  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

_app = QApplication.instance() or QApplication([])

from pdftexteditor.document import PDFDocument  # noqa: E402
from pdftexteditor.ocr import engine as E, fontbank as FB  # noqa: E402

PDF = os.path.expanduser("~/Downloads/doc05154920260624150538.pdf")
PG = 1
DATE_RE = re.compile(r"^\d{1,2}/\d{1,2}/\d{2,4}$|^\d{1,2}/\d{4}$")
ARIAL = {"00121.ttf": "Arial Bold", "00188.ttf": "Arial Reg", "00199.ttf": "Arial Black",
         "00247.ttf": "Arial Narrow Bold", "01404.ttf": "Arimo Reg"}


def undegrade(cov):
    """RESTORE the glyph (middle ground, not hard binary): despeckle, fill the dropout
    holes so the strokes are solid again, smooth the jagged edge, but keep the grey
    antialiasing so the letterform detail survives. Returns coverage (1.0 = ink)."""
    u8 = np.clip(cov * 255, 0, 255).astype(np.uint8)
    # 1. despeckle while preserving edges/structure
    d = cv2.fastNlMeansDenoising(u8, None, h=14, templateWindowSize=5, searchWindowSize=13)
    # 2. fill the dropout holes inside strokes (grayscale close), strokes go solid again
    d = cv2.morphologyEx(d, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3)))
    # 3. smooth the jagged edge into a clean antialiased boundary (keep grey)
    d = cv2.GaussianBlur(d, (0, 0), 0.7)
    return (d.astype(np.float32) / 255.0)


def main():
    doc = PDFDocument(PDF)
    rgb = doc.render_page_image(PG, 300.0)
    lines = E.get_engine("auto").recognize(rgb)
    ttf = FB._ensure_ttf_cache()
    keys = FB._load_fingerprints()["paths"]
    import fitz

    for ln in lines:
        t = ln.text.strip()
        if not DATE_RE.match(t):
            continue
        x0, y0, x1, y1 = ln.bbox
        region = rgb[max(0, int(y0)):int(y1) + 1, max(0, int(x0)):int(x1) + 1]
        geom = doc._scan_geometry(region, t)
        cb = (geom or {}).get("char_boxes")
        if not cb or len(cb) != len(t):
            continue
        g = region.mean(2)
        cov = (255.0 - g.astype(np.float32)) / 255.0
        from skimage.metrics import structural_similarity as ssim
        RES = 64
        # clean scan glyph tiles per char (high-res, SHARP -- the glyph is clean now)
        scan_tiles = {}
        cells = {}
        for ch, bx in zip(t, cb):
            if ch == " " or bx is None or ch not in FB._CIDX:
                continue
            bx0, by0, bx1, by1 = (int(v) for v in bx)
            if bx1 - bx0 < 3 or by1 - by0 < 3:
                continue
            tile = cov[by0:by1, bx0:bx1]
            nt = FB._normtile(undegrade(tile), RES)
            if nt is not None:
                scan_tiles.setdefault(ch, []).append(nt)
            cells[len(cells)] = (ch, (int(x0) + bx0, int(y0) + by0,
                                      int(x0) + bx1, int(y0) + by1))
        scan_tiles = {c: np.mean(v, 0) for c, v in scan_tiles.items() if v}

        ms = _coarse(rgb, cells)
        cand = [int(i) for i in np.argsort(-ms)[:40]]
        scored = []
        for ci in cand:
            f = FB._cand_font(os.path.join(ttf, keys[ci]))
            if f is None:
                continue
            sims = []
            for ch, st in scan_tiles.items():
                try:
                    if f.has_glyph(ord(ch)):
                        cr = FB._normtile(FB._render_glyph_cov(f, ch), RES)
                        if cr is not None:
                            sims.append(ssim(st, cr, data_range=1.0))
                except Exception:
                    pass
            if sims:
                scored.append((float(np.mean(sims)), keys[ci]))
        scored.sort(key=lambda x: -x[0])
        print(f"\n{t!r}: UNDEGRADE-and-compare (clean scan vs clean candidate), top 6:")
        for s, k in scored[:6]:
            nm = ARIAL.get(k) or fitz.Font(fontfile=os.path.join(ttf, k)).name
            star = "  <==" if k in ARIAL else ""
            print(f"   {s:.3f}  {k}  {nm}{star}")
        abr = [r for r, (s, k) in enumerate(scored) if k == "00121.ttf"]
        print(f"   Arial Bold (00121) rank: {abr[0] if abr else 'not in top40'}")
    doc.close()


def _coarse(scan, cells):
    fb = FB._load_fingerprints()
    bank = fb["desc"]
    F = bank.shape[0]
    score = np.zeros(F, np.float32)
    wsum = np.zeros(F, np.float32)
    per = {}
    for ch, box in cells.values():
        if ch in FB._CIDX:
            d = FB._glyph_descriptor(scan, box)
            if d is not None:
                per.setdefault(FB._CIDX[ch], []).append(d)
    for cidx, ds in per.items():
        q = np.mean(ds, 0); q -= q.mean(); nq = np.linalg.norm(q)
        if nq < 1e-6:
            continue
        q /= nq
        col = bank[:, cidx, :]; pres = np.any(col != 0, axis=1)
        score += np.where(pres, col @ q, 0.0); wsum += pres.astype(np.float32)
    ms = np.full(F, -1e9, np.float32); v = wsum > 0
    ms[v] = score[v] / wsum[v]
    return ms


if __name__ == "__main__":
    main()
