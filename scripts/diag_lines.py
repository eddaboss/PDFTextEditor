"""Draw the SCAN digits' top + baseline across the composed tile, so any synth glyph that
overshoots top/bottom is obvious."""
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
NEW = "5/12/2026"


def main():
    doc = PDFDocument(PDF)
    rgb = doc.render_page_image(PG, 300.0)
    res = R.reconstruct_page(rgb, 300.0, E.get_engine("auto").recognize(rgb), TINOS, ARIMO)
    if res.otf_bytes:
        FontEngine.register_custom_face(res.family_name, res.otf_bytes)
    t = next(lb for lb in res.lines if lb.text.endswith("2024") and "5/16" in lb.text)
    origin, direction = doc.ocr_text_placement(PG, t.origin)
    cx = doc.ocr_cover_rect(PG, t.cover)
    lcs = tuple(tuple(doc.ocr_cover_rect(PG, lc[:4])) + tuple(lc[4:7])
                for lc in (t.line_covers or ()))
    box = doc.add_box(PG, origin, t.text, t.family or res.family_name, t.size, (0, 0, 0),
                      False, False, direction=direction, cover=tuple(cx) + tuple(t.bg),
                      render_mode=3, box_w=t.box_w, leading=t.leading, line_covers=lcs)
    meas = doc.measure_box_glyphs(box)
    ml = meas["lines"][len(box.text.split("\n")) - 1]
    ctx = ml["ctx"]
    ot = ctx["orig_text"]
    ppi = ctx["ppi"]
    dx0d = ctx["disp_rect"][0]
    tile, _ = doc.inplace_compose(ctx, NEW)
    le = ctx.get("_live_edges")
    edges = [(e - dx0d) * ppi for e in le[1]]
    changed = {i for i in range(len(ot)) if NEW[i] != ot[i]}
    cov = 255.0 - tile.mean(2)
    tops, bots = [], []
    syn = []
    for i in range(len(NEW)):
        if not NEW[i].isdigit():
            continue
        a, b = int(round(edges[i])), int(round(edges[i + 1]))
        ys = np.where((cov[:, max(0, a):min(tile.shape[1], b)] > 0.30 * 255).any(1))[0]
        if not len(ys):
            continue
        if i in changed:
            syn.append((NEW[i], int(ys.min()), int(ys.max())))
        else:
            tops.append(int(ys.min())); bots.append(int(ys.max()))
    st, sb = int(np.median(tops)), int(np.median(bots))
    print(f"scan digits: top={st} bot={sb} (h={sb-st+1})")
    for ch, yt, yb in syn:
        print(f"  synth {ch!r}: top={yt} bot={yb} (h={yb-yt+1})  overshoot top={st-yt} bot={yb-sb}")
    z = 6
    big = cv2.resize(tile, None, fx=z, fy=z, interpolation=cv2.INTER_NEAREST)
    cv2.line(big, (0, st * z), (big.shape[1], st * z), (0, 0, 230), 1)
    cv2.line(big, (0, (sb + 1) * z), (big.shape[1], (sb + 1) * z), (0, 0, 230), 1)
    cv2.imwrite("/tmp/diag_lines.png", cv2.cvtColor(big, cv2.COLOR_RGB2BGR))
    print("saved /tmp/diag_lines.png  (red = scan digit top + baseline)")
    doc.close()


if __name__ == "__main__":
    main()
