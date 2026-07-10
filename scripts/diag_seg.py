"""Show, for the date crop, the RAW connected-component ink boxes vs the TRUE column-ink
valleys (real digit gaps) vs the final edges _scan_geometry produced. Reveals exactly why
_fit_boxes mis-distributes (touching comps? wrong split valleys?)."""
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
    ot = ml["text"]
    region = ml["ctx"]["region"]
    geom = ml["ctx"]["geom"]
    cb = geom.get("char_boxes")
    print(f"text={ot!r}")
    print("FINAL char_boxes (x0..x1, width):")
    for ch, b in zip(ot, cb):
        print(f"  {ch!r}: x[{int(b[0])}..{int(b[2])}] w={int(b[2]-b[0])}")

    # raw connected components (same threshold path as _scan_geometry)
    g = region.mean(2).astype(np.uint8)
    _, bw = cv2.threshold(g, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    if (bw > 0).mean() > 0.5:
        bw = 255 - bw
    nC, lab, stats, _ = cv2.connectedComponentsWithStats((bw > 0).astype(np.uint8), 8)
    comps = sorted([(int(stats[i, 0]), int(stats[i, 0] + stats[i, 2]), int(stats[i, 4]))
                    for i in range(1, nC) if stats[i, 4] >= 2])
    print(f"\nRAW connected comps ({len(comps)} vs {len(ot)} chars):")
    for x0, x1, area in comps:
        print(f"  x[{x0}..{x1}] w={x1-x0} area={area}")

    # column-ink profile + valleys
    cov = (255.0 - g.astype(float))
    col = cov.sum(0)
    col_s = cv2.GaussianBlur(col.reshape(1, -1), (1, 9), 0).ravel()
    valleys = [x for x in range(2, len(col_s) - 2)
               if col_s[x] < col_s[x - 1] and col_s[x] <= col_s[x + 1]
               and col_s[x] < 0.35 * col_s.max()]
    print(f"\nTRUE ink valleys (real gaps, x): {valleys}")

    # visualize: image + final edges (red) + comp bounds (cyan) + valleys (green)
    z = 5
    big = cv2.resize(region, None, fx=z, fy=z, interpolation=cv2.INTER_NEAREST)
    canv = np.full((big.shape[0] + 60, big.shape[1], 3), 255, np.uint8)
    canv[60:] = big
    for x0, x1, _a in comps:
        for X in (x0, x1):
            cv2.line(canv, (X * z, 44), (X * z, canv.shape[0]), (200, 160, 0), 1)
    for v in valleys:
        cv2.line(canv, (v * z, 28), (v * z, canv.shape[0]), (0, 170, 0), 1)
    for i, b in enumerate(cb):
        X = int(b[2]) * z
        cv2.line(canv, (X, 60), (X, canv.shape[0]), (0, 0, 220), 1)
    cv2.putText(canv, "red=final char edges  cyan=raw comps  green=true valleys",
                (4, 14), cv2.FONT_HERSHEY_SIMPLEX, 0.4, (0, 0, 0), 1, cv2.LINE_AA)
    cv2.imwrite("/tmp/diag_seg.png", cv2.cvtColor(canv, cv2.COLOR_RGB2BGR))
    print("\nsaved /tmp/diag_seg.png")
    doc.close()


if __name__ == "__main__":
    main()
