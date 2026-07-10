"""Isolate the synth '2' sizing: cascade-rendered '2' vs single-char-edit '2' vs scan '2',
all from the real compose tile, zoomed, with ink bbox drawn."""
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


def edges_px(ctx, nt):
    doc = ctx["_doc"]
    tile, _ = doc.inplace_compose(ctx, nt)
    le = ctx.get("_live_edges")
    dx0d = ctx["disp_rect"][0]
    ppi = ctx["ppi"]
    if not le or le[0] != nt:
        return tile, None
    return tile, [(e - dx0d) * ppi for e in le[1]]


def crop(tile, edges, i):
    a, b = int(round(edges[i])), int(round(edges[i + 1]))
    return tile[:, max(0, a):min(tile.shape[1], b)]


def show(c, label):
    g = c.mean(2)
    ys, xs = np.where(255 - g > 0.30 * 255)
    z = cv2.resize(c, None, fx=8, fy=8, interpolation=cv2.INTER_NEAREST)
    if len(ys):
        cv2.rectangle(z, (int(xs.min()) * 8, int(ys.min()) * 8),
                      (int(xs.max()) * 8, int(ys.max()) * 8), (0, 0, 220), 1)
        label += f"  {int(ys.max()-ys.min()+1)}x{int(xs.max()-xs.min()+1)}"
    bar = np.full((18, z.shape[1], 3), 240, np.uint8)
    cv2.putText(bar, label, (2, 13), cv2.FONT_HERSHEY_SIMPLEX, 0.4, (0, 0, 0), 1)
    return np.vstack([bar, z])


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
    panels = []

    ctx = dict(ml["ctx"]); ctx["_doc"] = doc
    tile, ed = edges_px(ctx, "5/12/2026")   # cascade
    if ed:
        panels.append(show(crop(tile, ed, 3), "CASCADE 2"))
        panels.append(show(crop(tile, ed, 5), "scan 2"))

    ctx2 = dict(ml["ctx"]); ctx2["_doc"] = doc
    for k in ("_live_edges", "_synth_metrics", "_size_factor", "_scan_band"):
        ctx2.pop(k, None)
    tile2, ed2 = edges_px(ctx2, "5/12/2024")  # single-char: only pos 3 6->2
    if ed2:
        panels.append(show(crop(tile2, ed2, 3), "SINGLE 2"))

    H = max(p.shape[0] for p in panels)
    panels = [np.pad(p, ((0, H - p.shape[0]), (4, 4), (0, 0)), constant_values=255) for p in panels]
    cv2.imwrite("/tmp/diag_2.png", cv2.cvtColor(np.hstack(panels), cv2.COLOR_RGB2BGR))
    print("saved /tmp/diag_2.png")
    doc.close()


if __name__ == "__main__":
    main()
