"""Run a date edit through the ACTUAL app flow and show before/after at the date.
Uses doc.render_with_edits (the app's real renderer, which handles page rotation) for
both the original and the edited page, and the app's page.rotation_matrix to locate
the date, so the crop is exactly what the app draws. Dates are not PHI.
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
    lines = E.get_engine("auto").recognize(rgb)
    res = R.reconstruct_page(rgb, 300.0, lines, TINOS, ARIMO)
    if res is None:
        print("reconstruct None"); return
    if res.otf_bytes:
        FontEngine.register_custom_face(res.family_name, res.otf_bytes)

    target = None
    for lb in res.lines:
        if "/" in lb.text and sum(c.isdigit() for c in lb.text) >= 3:
            target = lb
            break
    if target is None:
        print("no date box"); return
    print(f"date box: {target.text!r}  | matched font: {res.family_name}")

    origin, direction = doc.ocr_text_placement(PG, target.origin)
    cx = doc.ocr_cover_rect(PG, target.cover)
    cover = tuple(cx) + tuple(target.bg)
    box = doc.add_box(PG, origin, target.text, res.family_name, target.size,
                      (0, 0, 0), False, False, direction=direction, cover=cover,
                      render_mode=3, box_w=target.box_w, leading=target.leading)

    z = 4.0
    arr0 = to_np(doc.render_with_edits(PG, z))         # BEFORE (app render)
    t = target.text.strip()
    new = t[:-1] + ("5" if t[-1] != "5" else "7")       # change ONLY the last digit
    doc.stage_edit(PG, box, new)
    arr1 = to_np(doc.render_with_edits(PG, z))          # AFTER (app render)
    print(f"edit: {t.splitlines()[-1]!r} -> {new.splitlines()[-1]!r}")

    # the app re-synthesizes ONLY changed chars, so the pixel diff IS the changed region
    d = np.abs(arr0.astype(int) - arr1.astype(int)).sum(2)
    ys, xs = np.where(d > 30)
    if len(ys) == 0:
        print("nothing changed?!"); doc.close(); return
    y0, y1, x0, x1 = int(ys.min()), int(ys.max()), int(xs.min()), int(xs.max())
    changed_frac = float((d > 30).sum()) / float((to_np(doc.render_with_edits(PG, z)).mean(2) < 200).sum() + 1)
    print(f"CHANGED REGION: {x1 - x0 + 1}x{y1 - y0 + 1}px at x[{x0}:{x1}] y[{y0}:{y1}] "
          f"-- one glyph if narrow, the whole line if wide")

    pad = 36
    sl = (slice(max(0, y0 - pad), y1 + pad), slice(max(0, x0 - 4 * pad), x1 + 4 * pad))
    c0, c1 = arr0[sl], arr1[sl]
    w = max(c0.shape[1], c1.shape[1])

    def lab(im, label):
        im = np.pad(im, ((0, 0), (0, max(0, w - im.shape[1])), (0, 0)), constant_values=255)
        bar = np.full((22, im.shape[1], 3), 245, np.uint8)
        cv2.putText(bar, label, (4, 15), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (15, 15, 15), 1, cv2.LINE_AA)
        return np.vstack([bar, im, np.full((6, im.shape[1], 3), 200, np.uint8)])

    out = np.vstack([lab(c0, f"BEFORE  ...{t.splitlines()[-1]}"),
                     lab(c1, f"AFTER (only the boxed glyph is synth, font {res.family_name})")])
    cv2.imwrite("/tmp/edit_color.png", cv2.cvtColor(out, cv2.COLOR_RGB2BGR))
    print("saved /tmp/edit_color.png  (the changed glyph in context, before vs after)")
    doc.close()


if __name__ == "__main__":
    main()
