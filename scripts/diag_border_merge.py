"""Why did the OCR merge rows across the table rules? Find the over-merged box, list its
line y-gaps, list the detected horizontal rules, and show which gaps SHOULD have a rule
between them but the grouping's _hrule_between missed."""
import os
import sys
import statistics

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import numpy as np  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

_app = QApplication.instance() or QApplication([])

from pdftexteditor.document import PDFDocument  # noqa: E402
from pdftexteditor.ocr import engine as E, reconstruct as R  # noqa: E402
from pdftexteditor.ocr.borders import detect_borders  # noqa: E402

PDF = os.path.expanduser(sys.argv[1] if len(sys.argv) > 1
                         else "~/Downloads/doc05154920260624150538.pdf")
FONTS = os.path.join(ROOT, "pdftexteditor", "assets", "fonts")
TINOS = os.path.join(FONTS, "Tinos-Regular.ttf")
ARIMO = os.path.join(FONTS, "Arimo[wght].ttf")


def main():
    doc = PDFDocument(PDF)
    ppi = 300.0 / 72.0
    for pg in range(doc.page_count):
        rgb = doc.render_page_image(pg, 300.0)
        ocr = E.get_engine("auto").recognize(rgb)
        res = R.reconstruct_page(rgb, 300.0, ocr, TINOS, ARIMO)
        box = next((lb for lb in res.lines if "Nurses Service" in lb.text
                    or "05/31" in lb.text or "www.nso" in lb.text), None)
        if box is None:
            continue
        print(f"\n=== PAGE {pg}: over-merged box ===")
        print(f"text lines = {len(box.text.split(chr(10)))}")
        for i, t in enumerate(box.text.split("\n")):
            print(f"   line {i}: {t[:50]!r}")
        # line covers -> pixel y bands (cover is PDF points; * ppi -> px on the upright render)
        lcs = box.line_covers or ()
        bands = [(lc[1] * ppi, lc[3] * ppi, lc[0] * ppi, lc[2] * ppi) for lc in lcs]  # y0,y1,x0,x1 px
        # the rules the GROUPING used (reconstruct.py:369 recompute)
        hs = []
        try:
            import json
            # mimic reconstruct: median OCR line height
            heights = []
            for ln in ocr:
                ys = [p[1] for p in ln.quad]
                heights.append(max(ys) - min(ys))
            _ml = 2.5 * statistics.median(heights) if heights else 0.0
            h_rules, v_rules = detect_borders(rgb, min_len=_ml)
        except Exception as e:
            print("detect_borders failed:", e); h_rules = []
        rule_ys = sorted((y0 + y1) / 2.0 for (x0, y0, x1, y1) in h_rules)
        print(f"  grouping min_len(px)={_ml:.0f}  detected {len(h_rules)} h-rules at y(px)="
              f"{[round(y) for y in rule_ys]}")
        # overlay rules (document.page_border_lines -> display pts; * ppi back to px approx)
        try:
            oh, ov = doc.page_border_lines(pg)
            overlay_ys = sorted((seg[1] + seg[3]) / 2.0 * ppi for seg in oh)
            print(f"  OVERLAY (magenta) {len(oh)} h-rules at y(px)={[round(y) for y in overlay_ys]}")
        except Exception as e:
            print("overlay borders failed:", e)
        # for each consecutive line gap, is there a rule between, and does it x-overlap?
        print("  --- per inter-line gap ---")
        for i in range(len(bands) - 1):
            y_lo = bands[i][1]           # line i bottom
            y_hi = bands[i + 1][0]       # line i+1 top
            x_lo = max(bands[i][2], bands[i + 1][2])
            x_hi = min(bands[i][3], bands[i + 1][3])
            betw = [round(y) for y in rule_ys if y_lo < y < y_hi]
            near = [round(y) for y in rule_ys if y_lo - 8 < y < y_hi + 8]
            print(f"   gap {i}->{i+1}: y=({y_lo:.0f},{y_hi:.0f}) x=({x_lo:.0f},{x_hi:.0f})  "
                  f"rules strictly-between={betw}  rules within 8px={near}")
        doc.close()
        return
    doc.close()


if __name__ == "__main__":
    main()
