"""Render-and-verify font match for the date field, the way it was actually asked:
for each candidate font, drive the REAL synth+degrade engine (document._synth_strip)
to render the date text in that font and stamp the degradation MEASURED FROM THE
ACTUAL SCANNED DATE GLYPHS onto it, then compare that image to the scan (SSIM).
Walk the candidates, keep the best. Only the date's own glyphs are used.

    python scripts/render_verify_date.py [PDF] [PAGE]
"""
import os
import re
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import numpy as np  # noqa: E402
import cv2  # noqa: E402
import fitz  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

_app = QApplication.instance() or QApplication([])

from skimage.metrics import structural_similarity as ssim  # noqa: E402
from pdftexteditor.document import PDFDocument  # noqa: E402
from pdftexteditor.ocr import engine as E, supermatch as SM  # noqa: E402
from pdftexteditor.ocr import fontbank as FB, degrade as DG  # noqa: E402

PDF = sys.argv[1] if len(sys.argv) > 1 else \
    os.path.expanduser("~/Downloads/doc05154920260624150538.pdf")
PAGE = int(sys.argv[2]) if len(sys.argv) > 2 else 1
PPI = 300.0 / 72.0
DATE_RE = re.compile(r"^\d{1,2}/\d{1,2}/\d{2,4}$|^\d{1,2}/\d{4}$")
FONTS = os.path.join(ROOT, "pdftexteditor", "assets", "fonts")


def split_cov(cov, n):
    col = cov.sum(0)
    if col.max() <= 1e-6:
        return None
    xs = np.where(col > 0.10 * col.max())[0]
    if len(xs) < n:
        return None
    a0, a1 = int(xs.min()), int(xs.max()) + 1
    sm = np.convolve(col, np.ones(3) / 3, mode="same")
    minsep = max(2, (a1 - a0) // (n * 2))
    chosen = []
    for idx in np.argsort(sm):
        x = int(idx)
        if a0 + 2 < x < a1 - 2 and all(abs(x - c) >= minsep for c in chosen):
            chosen.append(x)
        if len(chosen) >= n - 1:
            break
    chosen.sort()
    bnd = [a0] + chosen + [a1]
    return [(bnd[i], bnd[i + 1]) for i in range(len(bnd) - 1)]


def fit_em(font_bytes, text, h_target):
    """em so the candidate's rendered ink height ~ the scan's (px)."""
    f = fitz.Font(fontbuffer=font_bytes)
    em0 = 100.0
    W = int(f.text_length(text, em0) + 2 * em0)
    doc = fitz.open(); pg = doc.new_page(width=W, height=em0 * 3)
    tw = fitz.TextWriter(pg.rect); tw.append((em0, em0 * 2), text, font=f, fontsize=em0)
    tw.write_text(pg)
    pm = pg.get_pixmap(alpha=False)
    a = np.frombuffer(pm.samples, np.uint8).reshape(pm.height, pm.width, pm.n)[..., :3]
    rows = np.where(((255.0 - a.mean(2)) / 255.0 > 0.3).any(1))[0]
    h = (rows.max() - rows.min() + 1) if len(rows) else 1
    return em0 * (h_target / max(h, 1)) / PPI


def ink_crop(cov):
    rows = np.where((cov > 0.3).any(1))[0]
    cols = np.where((cov > 0.3).any(0))[0]
    if not len(rows) or not len(cols):
        return None
    return cov[rows.min():rows.max() + 1, cols.min():cols.max() + 1]


def glyph_covs(cov, spans):
    return [ink_crop(cov[:, a:b]) for (a, b) in spans]


def compare_whole(scan_cov, synth_cov):
    """Whole-word compare: ink-crop both, align at top-left, SSIM over a common canvas.
    Keeps advance/spacing (the right font's word lines up; a wrong-width font does not)."""
    a, b = ink_crop(scan_cov), ink_crop(synth_cov)
    if a is None or b is None or a.size < 30 or b.size < 30:
        return -1.0
    H, W = max(a.shape[0], b.shape[0]), max(a.shape[1], b.shape[1])
    pa = np.zeros((H, W), np.float32); pa[:a.shape[0], :a.shape[1]] = a
    pb = np.zeros((H, W), np.float32); pb[:b.shape[0], :b.shape[1]] = b
    return float(ssim(pa, pb, data_range=1.0))


def compare_glyphs(scan_glyphs, synth_cov, n):
    """Per-digit SSIM: split the rendered date into n glyphs, ink-crop each, resize to
    the matching scan digit, and average. Registered, so it scores letterform+damage,
    not position."""
    sp = split_cov(synth_cov, n)
    if not sp or len(sp) != n:
        return -1.0
    sg = glyph_covs(synth_cov, sp)
    sims = []
    for a, b in zip(scan_glyphs, sg):
        if a is None or b is None or a.size < 9 or b.size < 9:
            continue
        br = cv2.resize(b.astype(np.float32), (max(a.shape[1], 2), max(a.shape[0], 2)))
        sims.append(ssim(a.astype(np.float32), br, data_range=1.0))
    return float(np.mean(sims)) if sims else -1.0


def main():
    print(f"PDF: {PDF}\npage index: {PAGE}")
    doc = PDFDocument(PDF)
    rgb = doc.render_page_image(PAGE, 300.0)
    lines = E.get_engine("auto").recognize(rgb)
    keys = FB._load_fingerprints()["paths"]
    ttf = FB._ensure_ttf_cache()
    dates = [ln for ln in lines if DATE_RE.match(ln.text.strip())]
    print(f"dates: {[d.text for d in dates]}")

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
            print(f"  {text!r}: no segmentation"); continue
        # page-coord cells for coarse ranking; region-local boxes for the degrade geom
        cells = [(chars[i], (x0i + a, y0i, x0i + b, y1i)) for i, (a, b) in enumerate(spans)]
        char_boxes = [(a, 0, b, region.shape[0]) for (a, b) in spans]
        scan_glyphs = glyph_covs(scan_cov, spans)

        inkrows = np.where((scan_cov > 0.3).any(1))[0]
        h_ink = (inkrows.max() - inkrows.min() + 1) if len(inkrows) else region.shape[0]
        base_y = float(inkrows.max() + 1) if len(inkrows) else float(region.shape[0])
        ink, paper = DG.sample_ink_paper(region)
        ctx = {"ppi": PPI, "Hr": region.shape[0], "base_y": base_y,
               "paper": paper.astype(np.float32), "ink": ink.astype(np.float32),
               "region": region, "geom": {"char_boxes": char_boxes},
               "rect": (x0i, y0i, x1i, y1i), "fpath": os.path.join(FONTS, "Arimo[wght].ttf")}

        ms = SM._coarse_scores(rgb, cells)
        cand = [int(i) for i in np.argsort(-ms)[:40]]
        scored = []
        for ci in cand:
            try:
                fb = open(os.path.join(ttf, keys[ci]), "rb").read()
                em = fit_em(fb, text, h_ink)
                ctx.pop("_bfilt", None)               # rebuild residual fresh per render seed
                strip, _ = doc._synth_strip(ctx, text, em=em, font_bytes=fb, base_y=base_y)
                s = compare_whole(scan_cov, DG.coverage(strip))
                scored.append((s, ci, strip))
            except Exception as e:
                if len(scored) < 2:
                    print(f"      cand {keys[ci]} err: {repr(e)[:50]}")
        scored.sort(key=lambda t: -t[0])
        print(f"\n  {text!r}: top render-and-compare (whole word via _synth_strip + degrade):")
        for s, ci, _st in scored[:6]:
            star = " <-- ARIAL BOLD" if keys[ci] == "00121.ttf" else ""
            print(f"      {keys[ci]}  ssim={s:.3f}{star}")
        abr = [r for r, (s, ci, _st) in enumerate(scored) if keys[ci] == "00121.ttf"]
        print(f"      Arial Bold (00121) rank: {abr[0] if abr else 'not in top40'}")
        if not scored:
            continue
        bs, bci, bstrip = scored[0]
        w = max(region.shape[1], bstrip.shape[1], 360)

        def lab(img, t):
            img = np.pad(img, ((0, 0), (0, max(0, w - img.shape[1])), (0, 0)), constant_values=255) \
                if img.shape[1] < w else img[:, :w]
            bar = np.full((20, w, 3), 245, np.uint8)
            cv2.putText(bar, t, (4, 14), cv2.FONT_HERSHEY_SIMPLEX, 0.46, (15, 15, 15), 1, cv2.LINE_AA)
            return np.vstack([bar, img, np.full((5, w, 3), 200, np.uint8)])
        panels.append(lab(region, f"SCAN  {text}"))
        panels.append(lab(bstrip, f"BEST RENDER+DEGRADE  {keys[bci]}  ssim={bs:.3f}"))
        panels.append(np.full((10, w, 3), 255, np.uint8))
    if panels:
        W = max(p.shape[1] for p in panels)
        panels = [np.pad(p, ((0, 0), (0, W - p.shape[1]), (0, 0)), constant_values=255) for p in panels]
        cv2.imwrite("/tmp/date_render.png", cv2.cvtColor(np.vstack(panels), cv2.COLOR_RGB2BGR))
        print("\nsaved /tmp/date_render.png")
    doc.close()


if __name__ == "__main__":
    main()
