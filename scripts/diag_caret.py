"""Overlay the REAL measured caret edges (measure_box_glyphs -> caret_line_edges) on the
scanned date line, to see whether the 'after-6' boundary (index 4) sits where the 6 ends
or one glyph off. Faithful box build (lb.family + line_covers), real document methods."""
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


def main():
    doc = PDFDocument(PDF)
    rgb = doc.render_page_image(PG, 300.0)
    lines = E.get_engine("auto").recognize(rgb)
    res = R.reconstruct_page(rgb, 300.0, lines, TINOS, ARIMO)
    if res.otf_bytes:
        FontEngine.register_custom_face(res.family_name, res.otf_bytes)
    target = next(lb for lb in res.lines
                  if lb.text.endswith("2024") and "5/16" in lb.text)
    origin, direction = doc.ocr_text_placement(PG, target.origin)
    cx = doc.ocr_cover_rect(PG, target.cover)
    cover = tuple(cx) + tuple(target.bg)
    line_covers = ()
    if getattr(target, "line_covers", None):
        lcs = []
        for lc in target.line_covers:
            r = doc.ocr_cover_rect(PG, lc[:4])
            lcs.append(tuple(r) + tuple(lc[4:7]))
        line_covers = tuple(lcs)
    box = doc.add_box(PG, origin, target.text, target.family or res.family_name,
                      target.size, (0, 0, 0), False, False, direction=direction,
                      cover=cover, render_mode=3, box_w=target.box_w,
                      leading=target.leading, line_covers=line_covers)
    print(f"box.text={box.text!r}  font={box.font_family}")

    meas = doc.measure_box_glyphs(box)
    cur_fp = doc._edit_font_file(box.font_family)
    # the DATE line is the last line
    dl_idx = len(box.text.split("\n")) - 1
    ml = meas["lines"][dl_idx]
    ot = ml["text"]                      # '5/16/2024'
    ppi = ml["ppi"]
    dx0 = ml["x0_disp"]
    region = ml["ctx"]["region"]
    # edges for the ORIGINAL text (what the click on an un-typed digit snaps against)
    edges_disp = doc.caret_line_edges(ml, ot, cur_fp)
    edges_px = [(e - dx0) * ppi for e in edges_disp]   # within the line crop, page px
    print(f"date line ot={ot!r}  ({len(ot)} chars, {len(edges_disp)} edges)")
    for i, (ch, ex) in enumerate(zip(list(ot) + ["END"], edges_px)):
        left = ot[i - 1] if 0 < i <= len(ot) else "."
        print(f"  edge[{i}] x={ex:6.1f}px  (boundary after {left!r})")

    # draw: scan date crop, vertical line at each edge, index above
    crop = region.copy()
    H = crop.shape[0]
    z = 4
    big = cv2.resize(crop, None, fx=z, fy=z, interpolation=cv2.INTER_NEAREST)
    pad = 26
    canvas = np.full((big.shape[0] + pad, big.shape[1], 3), 255, np.uint8)
    canvas[pad:] = big
    for i, ex in enumerate(edges_px):
        X = int(round(ex * z))
        if 0 <= X < canvas.shape[1]:
            col = (0, 0, 220) if i in (4, 5) else (0, 150, 0)  # 4/5 = after-6 / after-/
            cv2.line(canvas, (X, pad), (X, canvas.shape[0]), col, 1)
            cv2.putText(canvas, str(i), (max(0, X - 4), 18),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, col, 1, cv2.LINE_AA)
    cv2.imwrite("/tmp/diag_caret.png", cv2.cvtColor(canvas, cv2.COLOR_RGB2BGR))
    print("saved /tmp/diag_caret.png  (red=edges 4&5: after-6 / after-slash)")
    doc.close()


if __name__ == "__main__":
    main()
