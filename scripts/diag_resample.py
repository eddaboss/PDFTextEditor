"""PROVE the bake resamples unchanged pixels: insert the UNMODIFIED scan crop as an
image over its own cover, re-render, diff vs the original render. Nonzero diff on
identical content == insert_image + re-render alters every pixel."""
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
    if res.otf_bytes:
        FontEngine.register_custom_face(res.family_name, res.otf_bytes)
    target = next(lb for lb in res.lines
                  if "/" in lb.text and sum(c.isdigit() for c in lb.text) >= 3)
    cx = doc.ocr_cover_rect(PG, target.cover)
    page = doc.working[PG]
    rot = page.rotation_matrix
    scale = 300.0 / 72.0
    cpts = [fitz.Point(cx[0], cx[1]) * rot, fitz.Point(cx[2], cx[3]) * rot]
    rx0, ry0 = int(min(p.x for p in cpts) * scale), int(min(p.y for p in cpts) * scale)
    rx1, ry1 = int(max(p.x for p in cpts) * scale), int(max(p.y for p in cpts) * scale)
    crop = rgb[ry0:ry1, rx0:rx1]
    ok, buf = cv2.imencode(".png", cv2.cvtColor(crop, cv2.COLOR_RGB2BGR))

    for z in (1.5, 2.0, 4.0):
        full0 = to_np(page.get_pixmap(matrix=fitz.Matrix(z, z), alpha=False))
        page.insert_image(fitz.Rect(cx), stream=bytes(buf), keep_proportion=False,
                          rotate=page.rotation)
        full1 = to_np(page.get_pixmap(matrix=fitz.Matrix(z, z), alpha=False))
        zx0, zy0 = int(min(p.x for p in cpts) * z), int(min(p.y for p in cpts) * z)
        zx1, zy1 = int(max(p.x for p in cpts) * z), int(max(p.y for p in cpts) * z)
        a = full0[zy0:zy1, zx0:zx1].astype(int)
        b = full1[zy0:zy1, zx0:zx1].astype(int)
        d = np.abs(a - b).sum(2)
        nz, tot = int((d > 10).sum()), d.size
        print(f"z={z}: round-trip IDENTICAL scan crop -> {nz}/{tot} ({100*nz/max(tot,1):.1f}%) "
              f"pixels differ by >10 (max diff sum={int(d.max())})")
        if z == 2.0:
            out = np.hstack([full0[zy0:zy1, zx0:zx1],
                             np.full((zy1-zy0, 6, 3), 0, np.uint8),
                             full1[zy0:zy1, zx0:zx1],
                             np.full((zy1-zy0, 6, 3), 0, np.uint8),
                             cv2.applyColorMap((np.clip(d, 0, 255)).astype(np.uint8),
                                               cv2.COLORMAP_JET)])
            cv2.imwrite("/tmp/diag_resample.png", cv2.cvtColor(out, cv2.COLOR_RGB2BGR))
            print("  saved /tmp/diag_resample.png  [orig | round-trip | diff-heatmap]")
        # undo the insert by reloading source bytes is complex; just note diffs accumulate
        page.clean_contents()
    doc.close()


if __name__ == "__main__":
    main()
