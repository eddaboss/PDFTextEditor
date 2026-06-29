"""Trace WHICH raster path an edit to the 2-line date box takes, and the box attrs."""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import numpy as np  # noqa: E402
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
    target = None
    for lb in res.lines:
        if "/" in lb.text and sum(c.isdigit() for c in lb.text) >= 3:
            target = lb
            break
    print(f"target.text={target.text!r}")
    origin, direction = doc.ocr_text_placement(PG, target.origin)
    cx = doc.ocr_cover_rect(PG, target.cover)
    cover = tuple(cx) + tuple(target.bg)
    line_covers = ()
    if getattr(target, "line_covers", None):
        lcs = []
        for lc in target.line_covers:
            lx0, ly0, lx1, ly1 = doc.ocr_cover_rect(PG, lc[:4])
            lcs.append((lx0, ly0, lx1, ly1) + tuple(lc[4:7]))
        line_covers = tuple(lcs)
    box = doc.add_box(PG, origin, target.text, res.family_name, target.size,
                      (0, 0, 0), False, False, direction=direction, cover=cover,
                      render_mode=3, box_w=target.box_w, leading=target.leading,
                      line_covers=line_covers)
    print("box attrs:")
    for a in ("text", "ocr_text", "is_paragraph", "line_covers", "box_w", "reflow",
              "render_mode", "font_family"):
        v = getattr(box, a, "<none>")
        if a == "line_covers":
            v = f"len={len(v) if v else 0}"
        print(f"  {a} = {v!r}")

    # wrap inplace_compose to DUMP the per-line tile (the thing that should keep the
    # prefix scan + synth only the changed glyph)
    import cv2
    _orig_ic = doc.inplace_compose

    def ic_wrap(ctx, nt, runs=None):
        tile, disp = _orig_ic(ctx, nt, runs)
        ot = ctx.get("orig_text", "")
        if tile is not None and "/" in str(ot):
            z = cv2.resize(tile, None, fx=5, fy=5, interpolation=cv2.INTER_NEAREST)
            cv2.imwrite("/tmp/diag_tile.png", cv2.cvtColor(z, cv2.COLOR_RGB2BGR))
            print(f"  DUMPED tile for line {ot!r}->{nt!r}  tile={tile.shape}")
        return tile, disp
    doc.inplace_compose = ic_wrap

    _orig_clb = doc.compose_lines_block

    def clb_wrap(b, nt, **k):
        out = _orig_clb(b, nt, **k)
        if out is not None:
            canvas, disp = out[0], out[1]
            z = cv2.resize(canvas, None, fx=4, fy=4, interpolation=cv2.INTER_NEAREST)
            cv2.imwrite("/tmp/diag_canvas.png", cv2.cvtColor(z, cv2.COLOR_RGB2BGR))
            print(f"  DUMPED canvas {canvas.shape} disp={tuple(round(v,1) for v in disp)}")
        return out
    doc.compose_lines_block = clb_wrap

    # wrap the raster methods to see which fires
    orig_lines = doc._scanned_lines_raster
    orig_inplace = doc._scanned_inplace_raster
    orig_para = doc._scanned_paragraph_raster

    def w(name, fn):
        def inner(*a, **k):
            print(f"  >> CALLED {name}  args_text={a[1] if len(a) > 1 else '?'!r}")
            return fn(*a, **k)
        return inner
    doc._scanned_lines_raster = w("_scanned_lines_raster", orig_lines)
    doc._scanned_inplace_raster = w("_scanned_inplace_raster", orig_inplace)
    doc._scanned_paragraph_raster = w("_scanned_paragraph_raster", orig_para)

    def to_np(pm):
        return np.frombuffer(pm.samples, np.uint8).reshape(
            pm.height, pm.width, pm.n)[..., :3].copy()

    z = 4.0
    arr0 = to_np(doc.render_with_edits(PG, z))
    t = target.text
    new = t[:-1] + ("5" if t[-1] != "5" else "7")
    print(f"edit -> {new!r}")
    doc.stage_edit(PG, box, new)
    arr1 = to_np(doc.render_with_edits(PG, z))
    d = np.abs(arr0.astype(int) - arr1.astype(int)).sum(2)
    ys, xs = np.where(d > 30)
    if len(ys):
        import cv2
        y0, y1, x0, x1 = int(ys.min()), int(ys.max()), int(xs.min()), int(xs.max())
        print(f"CHANGED REGION: {x1-x0+1}x{y1-y0+1}px "
              f"(narrow=one glyph, ~200+px wide=whole line)")
        pad = 20
        sl = (slice(max(0, y0-pad), y1+pad), slice(max(0, x0-pad), x1+pad))
        c0, c1 = arr0[sl], arr1[sl]

        def lab(im, s):
            im = cv2.resize(im, None, fx=2, fy=2, interpolation=cv2.INTER_NEAREST)
            bar = np.full((22, im.shape[1], 3), 245, np.uint8)
            cv2.putText(bar, s, (4, 15), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (15, 15, 15), 1, cv2.LINE_AA)
            return np.vstack([bar, im, np.full((6, im.shape[1], 3), 200, np.uint8)])
        w = max(c0.shape[1], c1.shape[1]) * 2
        out = np.vstack([lab(c0, "BEFORE"), lab(c1, f"AFTER (only last digit should be synth)")])
        cv2.imwrite("/tmp/diag_path.png", cv2.cvtColor(out, cv2.COLOR_RGB2BGR))
        # also overlay the changed-pixel mask so we SEE which glyphs moved
        mask = (d[sl] > 30).astype(np.uint8) * 255
        cv2.imwrite("/tmp/diag_mask.png", cv2.resize(mask, None, fx=2, fy=2,
                    interpolation=cv2.INTER_NEAREST))
        print("saved /tmp/diag_path.png + /tmp/diag_mask.png")
    else:
        print("nothing changed")
    doc.close()


if __name__ == "__main__":
    main()
