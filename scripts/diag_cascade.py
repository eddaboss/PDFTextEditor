"""Reproduce the 'anchoring' cascade: with the field already diverged from the scan
(year changed), editing a MIDDLE digit re-bakes every character between the two changes,
not just the ones touched. Measures the changed-region width: a per-char fix should make
it TWO narrow glyphs, not the whole middle.

ot (scan) = 5/16/2024 ; nt (current edit) = 5/12/2026  -> only positions 3 and 8 differ;
positions 4..7 ('/202') are UNCHANGED and must stay scan.
"""
import os
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
from pdftexteditor.ocr import engine as E, reconstruct as R  # noqa: E402
from pdftexteditor.font_engine import FontEngine  # noqa: E402

PDF = os.path.expanduser("~/Downloads/doc05154920260624150538.pdf")
PG = 1
FONTS = os.path.join(ROOT, "pdftexteditor", "assets", "fonts")
TINOS = os.path.join(FONTS, "Tinos-Regular.ttf")
ARIMO = os.path.join(FONTS, "Arimo[wght].ttf")


def to_np(pm):
    return np.frombuffer(pm.samples, np.uint8).reshape(pm.height, pm.width, pm.n)[..., :3].copy()


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
    # dump the NATIVE inplace_compose tile for the date line (no resampling)
    _orig = doc.inplace_compose

    def _wrap(ctx, nt, runs=None):
        t, d = _orig(ctx, nt, runs)
        if t is not None and "/" in str(ctx.get("orig_text", "")):
            z2 = cv2.resize(t, None, fx=5, fy=5, interpolation=cv2.INTER_NEAREST)
            cv2.imwrite("/tmp/cascade_tile.png", cv2.cvtColor(z2, cv2.COLOR_RGB2BGR))
            print(f"  TILE {ctx.get('orig_text')!r}->{nt!r} shape={t.shape}")
        return t, d
    doc.inplace_compose = _wrap

    z = 4.0
    arr0 = to_np(doc.render_with_edits(PG, z))
    new = target.text[:target.text.rfind("\n") + 1] + "5/12/2026"   # 6->2 (pos3), 4->6 (pos8)
    doc.stage_edit(PG, box, new)
    arr1 = to_np(doc.render_with_edits(PG, z))
    d = np.abs(arr0.astype(int) - arr1.astype(int)).sum(2)
    ys, xs = np.where(d > 30)
    if len(ys):
        x0, x1, y0, y1 = xs.min(), xs.max(), ys.min(), ys.max()
        print(f"edit 5/16/2024 -> 5/12/2026  (only pos 3 and 8 differ)")
        print(f"CHANGED REGION: {x1-x0+1}x{y1-y0+1}px  "
              f"(two glyphs ~= 2x20px; whole middle ~= 130px wide)")
        pad = 16
        sl = (slice(max(0, y0-pad), y1+pad), slice(max(0, x0-pad), x1+pad))

        def lab(im, s):
            im = cv2.resize(im, None, fx=2, fy=2, interpolation=cv2.INTER_NEAREST)
            bar = np.full((20, im.shape[1], 3), 245, np.uint8)
            cv2.putText(bar, s, (4, 14), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (15, 15, 15), 1, cv2.LINE_AA)
            return np.vstack([bar, im, np.full((6, im.shape[1], 3), 200, np.uint8)])
        out = np.vstack([lab(arr0[sl], "BEFORE 5/16/2024"),
                         lab(arr1[sl], "AFTER 5/12/2026 (scan /202 in middle should stay)")])
        cv2.imwrite("/tmp/diag_cascade.png", cv2.cvtColor(out, cv2.COLOR_RGB2BGR))
        print("saved /tmp/diag_cascade.png")
    else:
        print("nothing changed")
    doc.close()


if __name__ == "__main__":
    main()
