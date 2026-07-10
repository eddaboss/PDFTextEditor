"""Match the date field's font using ONLY that field's own glyphs (never pooled with
other fields, which may be a different font). The date is too short for supermatch's
super-resolution, so match it with the degrade-matched verifier over its own digits.

    python scripts/fix_date_font.py [PDF] [PAGE]
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

from pdftexteditor.document import PDFDocument  # noqa: E402
from pdftexteditor.ocr import engine as E, supermatch as SM  # noqa: E402
from pdftexteditor.ocr import fontbank as FB, verify as V  # noqa: E402

PDF = sys.argv[1] if len(sys.argv) > 1 else \
    os.path.expanduser("~/Downloads/doc05154920260624150538.pdf")
PAGE = int(sys.argv[2]) if len(sys.argv) > 2 else 1
DATE_RE = re.compile(r"^\d{1,2}/\d{1,2}/\d{2,4}$|^\d{1,2}/\d{4}$")


def split_n(strip, n):
    """Split a field strip into exactly n glyph columns by cutting at the n-1
    lowest-ink valleys (robust to slightly touching digits)."""
    g = strip.mean(2)
    paper, ink = np.percentile(g, 90), np.percentile(g, 5)
    cov = np.clip((paper - g) / max(paper - ink, 1e-3), 0.0, 1.0)
    col = cov.sum(0)
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
    bounds = [a0] + chosen + [a1]
    return [(bounds[i], bounds[i + 1]) for i in range(len(bounds) - 1)]


def date_cells(rgb, ln):
    x0, y0, x1, y1 = ln.bbox
    x0i, y0i = max(0, int(x0)), max(0, int(y0))
    x1i, y1i = min(rgb.shape[1], int(x1) + 1), min(rgb.shape[0], int(y1) + 1)
    strip = rgb[y0i:y1i, x0i:x1i]
    chars = [c for c in ln.text if c.strip()]
    spans = split_n(strip, len(chars))
    if not spans:
        return None, (x0i, y0i, x1i, y1i)
    return [(chars[i], (x0i + a, y0i, x0i + b, y1i)) for i, (a, b) in enumerate(spans)], \
        (x0i, y0i, x1i, y1i)


def pick(rgb, cells, keys, ttf, residual=None, k=40):
    rep = V.scan_word_repr(rgb, cells)
    if not rep:
        return None, -1.0
    ms = SM._coarse_scores(rgb, cells)
    best, bestv = None, -1.0
    for ci in [int(i) for i in np.argsort(-ms)[:k]]:
        m = V.score_against(rep, os.path.join(ttf, keys[ci]), residual=residual)
        if m is not None:
            s = V.combined(m)
            if s > bestv:
                bestv, best = s, keys[ci]
    return best, bestv


def render_text(key, text, ttf, h=56):
    p = os.path.join(ttf, key) if key else None
    if not p or not os.path.exists(p):
        return np.full((h, 200, 3), 235, np.uint8)
    f = fitz.Font(fontfile=p)
    em = h * 0.62
    W = int(f.text_length(text, em) + em)
    doc = fitz.open()
    pg = doc.new_page(width=W, height=h)
    tw = fitz.TextWriter(pg.rect)
    tw.append((em * 0.3, h * 0.72), text, font=f, fontsize=em)
    tw.write_text(pg, color=(0, 0, 0))
    pm = pg.get_pixmap(alpha=False)
    return np.frombuffer(pm.samples, np.uint8).reshape(pm.height, pm.width, pm.n)[..., :3].copy()


def row(img, text, w):
    img = np.pad(img, ((0, 0), (0, max(0, w - img.shape[1])), (0, 0)), constant_values=255) \
        if img.shape[1] < w else img[:, :w]
    bar = np.full((20, w, 3), 245, np.uint8)
    cv2.putText(bar, text, (4, 14), cv2.FONT_HERSHEY_SIMPLEX, 0.46, (15, 15, 15), 1, cv2.LINE_AA)
    return np.vstack([bar, img, np.full((5, w, 3), 200, np.uint8)])


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
        cells, (x0, y0, x1, y1) = date_cells(rgb, ln)
        if not cells:
            print(f"  {ln.text!r}: no cells"); continue
        clean, cv_ = pick(rgb, cells, keys, ttf, residual=None)
        residual = SM._cluster_residual(rgb, cells)
        deg, dv = pick(rgb, cells, keys, ttf, residual=residual)
        print(f"\n  {ln.text!r}  ({len(cells)} glyphs, own field only)")
        print(f"    per-field CLEAN        : {clean}  ({cv_:.3f})")
        print(f"    per-field DEGRADE-MATCH: {deg}  ({dv:.3f})")
        scan = rgb[max(0, y0 - 4):y1 + 4, max(0, x0 - 4):x1 + 4]
        w = max(scan.shape[1], 380)
        panels.append(row(scan, f"SCAN  {ln.text}", w))
        panels.append(row(render_text(clean, ln.text, ttf), f"CLEAN  {clean}", w))
        panels.append(row(render_text(deg, ln.text, ttf), f"DEGRADE-MATCH  {deg}", w))
        panels.append(np.full((10, w, 3), 255, np.uint8))
    if panels:
        W = max(p.shape[1] for p in panels)
        panels = [np.pad(p, ((0, 0), (0, W - p.shape[1]), (0, 0)), constant_values=255) for p in panels]
        cv2.imwrite("/tmp/date_fix.png", cv2.cvtColor(np.vstack(panels), cv2.COLOR_RGB2BGR))
        print("\nsaved /tmp/date_fix.png")
    doc.close()


if __name__ == "__main__":
    main()
