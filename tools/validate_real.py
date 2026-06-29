#!/usr/bin/env python3
"""Real-scan validation: OCR a scanned PDF, match its font with the CURRENT matcher
and the v2 prototype, and render a side-by-side contact sheet so the matched fonts
can be eyeballed against the real scan (especially digit lines).

PHI-safe: takes the file path as an argument (no patient names baked into the repo),
prints only font NAMES + counts (never document text), and writes the only
text-bearing artifact to ~/Desktop/ocr_demos/ (local).

    python tools/validate_real.py "/path/to/scan.pdf" [page]
"""
from __future__ import annotations

import os
import sys

import numpy as np
import cv2
import fitz

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.append(os.path.expanduser("~/Documents/GitHub/PDFTextEditor-ocr"))

from pdftexteditor.ocr import pack, get_engine          # noqa: E402
pack.ensure_on_path()
from pdftexteditor.ocr import fontbank as FB             # noqa: E402
from pdftexteditor.ocr.segment import segment_line       # noqa: E402
from pdftexteditor.font_engine import detect_ttf_style    # noqa: E402
import matcher_v2 as V2                                   # noqa: E402

OUT = os.path.expanduser("~/Desktop/ocr_demos")


def page_cells(rgb, lines):
    """Per-line glyph cells {key:(char,(x0,y0,x1,y1))} + the line records for render."""
    cells, recs = {}, []
    H, W = rgb.shape[:2]
    for ln in lines:
        x0, y0, x1, y1 = ln.bbox
        x0i, y0i = max(0, int(x0)), max(0, int(y0))
        x1i, y1i = min(W, int(x1) + 1), min(H, int(y1) + 1)
        if x1i - x0i < 4 or y1i - y0i < 4:
            continue
        strip = rgb[y0i:y1i, x0i:x1i]
        try:
            seg = segment_line(strip, ln.text)
        except Exception:
            seg = None
        if seg is None or not seg.glyphs:
            continue
        lc = {}
        for g in seg.glyphs:
            if not g.char.strip() or g.bitmap.size == 0:
                continue
            gx = x0i + int(g.x0)
            box = (gx, y0i, gx + int(g.bitmap.shape[1]), y1i)
            cells[len(cells)] = (g.char, box)
            lc[len(lc)] = (g.char, box)
        if lc:
            recs.append(dict(text=ln.text, box=(x0i, y0i, x1i, y1i), cells=lc))
    return cells, recs


def fontname(key):
    try:
        fam, b, i = detect_ttf_style(os.path.join(FB._ensure_ttf_cache(), key))
        return f"{fam}{' Bold' if b else ''}{' Italic' if i else ''}"
    except Exception:
        return key


def render_text(key, text, target_h):
    f = fitz.Font(fontfile=os.path.join(FB._ensure_ttf_cache(), key))
    em = max(8.0, target_h / 0.7)
    w = max(10, int(f.text_length(text, em)) + int(em))
    h = int(em * 1.7)
    doc = fitz.open()
    pg = doc.new_page(width=w, height=h)
    tw = fitz.TextWriter(pg.rect, color=(0, 0, 0))
    tw.append((em * 0.3, em * 1.2), text, font=f, fontsize=em)
    tw.write_text(pg)
    pm = pg.get_pixmap(alpha=False)
    img = np.frombuffer(pm.samples, np.uint8).reshape(pm.height, pm.width, pm.n)[..., :3]
    sc = target_h / img.shape[0]
    return cv2.resize(img, (max(1, int(img.shape[1] * sc)), target_h),
                      interpolation=cv2.INTER_AREA)


def label(text, w, h=20):
    bar = np.full((h, w, 3), 245, np.uint8)
    cv2.putText(bar, text, (4, h - 6), cv2.FONT_HERSHEY_SIMPLEX, 0.45,
                (40, 40, 40), 1, cv2.LINE_AA)
    return bar


def auto_orient(base, engine):
    """Scanned pages can be sideways/upside-down. Try all 4 rotations; keep the ones
    whose OCR lines are HORIZONTAL (w/h>1.5), then pick the orientation whose glyphs
    give the most confident whole-page font match (upright text matches best).
    General: no per-file hardcoding."""
    best = None
    for k in (0, 1, 2, 3):
        rgb = np.ascontiguousarray(np.rot90(base, k))
        lines = engine.recognize(rgb)
        if not lines:
            continue
        ar = [(x1 - x0) / max(1, y1 - y0) for (x0, y0, x1, y1) in (l.bbox for l in lines)]
        if np.median(ar) < 1.5:
            continue                      # vertical text -> wrong orientation
        cells, _ = page_cells(rgb, lines)
        conf = FB.identify(rgb, cells, topk=1).get("confidence", 0.0) if cells else -1
        if best is None or conf > best[0]:
            best = (conf, k, rgb, lines)
    if best is None:
        return base, engine.recognize(base)
    print(f"orientation: rot{best[1]*90} (match conf {best[0]:.3f})")
    return best[2], best[3]


def main():
    path = sys.argv[1]
    page = int(sys.argv[2]) if len(sys.argv) > 2 else 0
    d = fitz.open(path)
    pm = d[page].get_pixmap(dpi=300)
    base = np.frombuffer(pm.samples, np.uint8).reshape(pm.height, pm.width, pm.n)[..., :3].copy()
    rgb, lines = auto_orient(base, get_engine("auto"))
    cells, recs = page_cells(rgb, lines)
    print(f"page {page}: {len(lines)} OCR lines, {len(cells)} glyph cells, "
          f"{len(recs)} usable lines")

    cur = FB.identify(rgb, cells, topk=5)
    v2 = V2.identify_v2(rgb, cells, topk=5)
    print(f"\nWHOLE-PAGE font match (n_glyphs={cur.get('n_glyphs')}):")
    print(f"  CURRENT: {fontname(cur['best'])}   conf {cur['confidence']:.3f}")
    print(f"           top5: {[fontname(k) for k,_ in cur['topk']]}")
    print(f"  V2     : {fontname(v2['best'])}   conf {v2['confidence']:.3f}")
    print(f"           top5: {[fontname(k) for k,_ in v2['topk']]}")

    # contact sheet: digit lines first, then a few others
    recs.sort(key=lambda r: (not any(c.isdigit() for c in r["text"]), r["box"][1]))
    rows, W = [], 1100
    for r in recs[:8]:
        x0, y0, x1, y1 = r["box"]
        crop = rgb[y0:y1, x0:x1]
        th = max(18, y1 - y0)
        try:
            curr = render_text(cur["best"], r["text"], th)
            v2r = render_text(v2["best"], r["text"], th)
        except Exception:
            continue
        for tag, im in (("SCAN", crop), ("CURRENT", curr), ("V2", v2r)):
            strip = im
            if strip.shape[1] > W:
                sc = W / strip.shape[1]
                strip = cv2.resize(strip, (W, int(strip.shape[0] * sc)))
            strip = np.pad(strip, ((0, 0), (0, max(0, W - strip.shape[1])), (0, 0)),
                           constant_values=255)
            rows.append(label(tag, W))
            rows.append(strip)
        rows.append(np.full((10, W, 3), 180, np.uint8))
    if rows:
        os.makedirs(OUT, exist_ok=True)
        sheet = np.vstack(rows)
        out = os.path.join(OUT, f"real_validation_p{page}.png")
        cv2.imwrite(out, cv2.cvtColor(sheet, cv2.COLOR_RGB2BGR))
        print(f"\nsaved contact sheet -> {out}")


if __name__ == "__main__":
    main()
