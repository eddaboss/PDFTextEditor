"""Measure the SYNTH digits vs the kept SCAN digits in the real compose tile, per glyph:
ink height, ink width, and ink darkness (mean of the dark pixels). Tells us exactly how
far off sizing / spacing / coloring are so they can be tuned. Uses the live per-char
edges the compose emits, so each glyph is located exactly where it rendered."""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import numpy as np  # noqa: E402
import cv2  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

_app = QApplication.instance() or QApplication([])

from pdftexteditor.document import PDFDocument  # noqa: E402
from pdftexteditor.ocr import engine as E, reconstruct as R  # noqa: E402
from pdftexteditor.font_engine import FontEngine  # noqa: E402

PDF = os.path.expanduser("~/Downloads/doc05154920260624150538.pdf")
PG = 1
FONTS = os.path.join(ROOT, "pdftexteditor", "assets", "fonts")
TINOS = os.path.join(FONTS, "Tinos-Regular.ttf")
ARIMO = os.path.join(FONTS, "Arimo[wght].ttf")
NEW = "5/12/2026"   # vs scan 5/16/2024 -> changed at index 3 and 8


def glyph_stats(col_img):
    """height, width, darkMean, ink_frac (area covered), core (mean grey of solid ink)."""
    g = col_img.mean(2)
    cov = 255.0 - g
    ink = cov > 0.30 * 255
    ys, xs = np.where(ink)
    if not len(ys):
        return 0, 0, 255.0, 0.0, 255.0
    h = int(ys.max() - ys.min() + 1)
    w = int(xs.max() - xs.min() + 1)
    dark = g[ink]
    frac = float(ink.sum()) / float(max(1, h * w))
    solid = cov > 0.60 * 255
    core = float(g[solid].mean()) if solid.any() else float(dark.mean())
    return h, w, float(dark.mean()), frac, core


def main():
    doc = PDFDocument(PDF)
    rgb = doc.render_page_image(PG, 300.0)
    res = R.reconstruct_page(rgb, 300.0, E.get_engine("auto").recognize(rgb), TINOS, ARIMO)
    if res.otf_bytes:
        FontEngine.register_custom_face(res.family_name, res.otf_bytes)
    target = next(lb for lb in res.lines if lb.text.endswith("2024") and "5/16" in lb.text)
    origin, direction = doc.ocr_text_placement(PG, target.origin)
    cx = doc.ocr_cover_rect(PG, target.cover)
    line_covers = ()
    if getattr(target, "line_covers", None):
        line_covers = tuple(tuple(doc.ocr_cover_rect(PG, lc[:4])) + tuple(lc[4:7])
                            for lc in target.line_covers)
    box = doc.add_box(PG, origin, target.text, target.family or res.family_name, target.size,
                      (0, 0, 0), False, False, direction=direction,
                      cover=tuple(cx) + tuple(target.bg), render_mode=3,
                      box_w=target.box_w, leading=target.leading, line_covers=line_covers)
    meas = doc.measure_box_glyphs(box)
    ml = meas["lines"][len(box.text.split("\n")) - 1]
    ctx = ml["ctx"]
    ot = ctx["orig_text"]
    ppi = ctx["ppi"]
    dx0d = ctx["disp_rect"][0]
    tile, disp = doc.inplace_compose(ctx, NEW)
    le = ctx.get("_live_edges")
    if not le or le[0] != NEW:
        print("no live edges"); return
    edges = [(e - dx0d) * ppi for e in le[1]]   # display pt -> tile px
    print(f"tile {tile.shape}, {len(edges)} edges for {NEW!r}")
    changed = {i for i in range(len(ot)) if NEW[i] != ot[i]}
    panels = []
    print(f"  {'i':>2} {'ch':>2} {'kind':>5}  {'h':>3} {'w':>3} {'darkMean':>8} {'inkFrac':>7} {'core':>6}")
    for i in range(len(NEW)):
        a, b = int(round(edges[i])), int(round(edges[i + 1]))
        a = max(0, a); b = min(tile.shape[1], b)
        if b <= a:
            continue
        crop = tile[:, a:b]
        h, w, dm, frac, core = glyph_stats(crop)
        kind = "SYNTH" if i in changed else "scan"
        if NEW[i].isdigit():
            print(f"  {i:>2} {NEW[i]:>2} {kind:>5}  {h:>3} {w:>3} {dm:>8.1f} {frac:>7.2f} {core:>6.1f}")
        z = cv2.resize(crop, None, fx=4, fy=4, interpolation=cv2.INTER_NEAREST)
        bar = np.full((16, z.shape[1], 3), (200, 200, 255) if i in changed else (235, 235, 235), np.uint8)
        cv2.putText(bar, NEW[i], (2, 12), cv2.FONT_HERSHEY_SIMPLEX, 0.4, (0, 0, 0), 1)
        panels.append(np.vstack([bar, z]))
    H = max(p.shape[0] for p in panels)
    panels = [np.pad(p, ((0, H - p.shape[0]), (2, 2), (0, 0)), constant_values=255) for p in panels]
    cv2.imwrite("/tmp/diag_tune.png", cv2.cvtColor(np.hstack(panels), cv2.COLOR_RGB2BGR))
    print("saved /tmp/diag_tune.png  (blue header = synth glyph)")
    doc.close()


if __name__ == "__main__":
    main()
