"""For EVERY reconstructed date box on page 1, report is_paragraph, #line_covers (as
reconstruct emits them), which raster path an edit takes, and the changed-region size."""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import numpy as np  # noqa: E402
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

    for lb in res.lines:
        if not ("/" in lb.text and sum(c.isdigit() for c in lb.text) >= 3):
            continue
        nlc = len(getattr(lb, "line_covers", []) or [])
        print(f"\nBOX {lb.text!r}  is_paragraph={getattr(lb,'is_paragraph',False)} "
              f"reconstruct.line_covers={nlc}")
        origin, direction = doc.ocr_text_placement(PG, lb.origin)
        cx = doc.ocr_cover_rect(PG, lb.cover)
        cover = tuple(cx) + tuple(lb.bg)
        line_covers = ()
        if getattr(lb, "line_covers", None):
            lcs = []
            for lc in lb.line_covers:
                r = doc.ocr_cover_rect(PG, lc[:4])
                lcs.append(tuple(r) + tuple(lc[4:7]))
            line_covers = tuple(lcs)
        box = doc.add_box(PG, origin, lb.text, lb.family or res.family_name, lb.size,
                          (0, 0, 0), False, False, direction=direction, cover=cover,
                          render_mode=3, box_w=lb.box_w, leading=lb.leading,
                          line_covers=line_covers)
        # which raster path?
        fired = []
        for nm in ("_scanned_lines_raster", "_scanned_inplace_raster",
                   "_scanned_paragraph_raster"):
            orig = getattr(doc, nm)

            def mk(n, o):
                def inner(*a, **k):
                    fired.append(n)
                    return o(*a, **k)
                return inner
            setattr(doc, nm, mk(nm, orig))

        z = 3.0
        arr0 = to_np(doc.render_with_edits(PG, z))
        # edit the LAST digit of the date line only
        dl = lb.text.split("\n")[-1]
        new_dl = dl[:-1] + ("5" if dl[-1] != "5" else "7")
        new = lb.text[:lb.text.rfind(dl)] + new_dl
        doc.stage_edit(PG, box, new)
        arr1 = to_np(doc.render_with_edits(PG, z))
        d = np.abs(arr0.astype(int) - arr1.astype(int)).sum(2)
        ys, xs = np.where(d > 30)
        reg = f"{xs.max()-xs.min()+1}x{ys.max()-ys.min()+1}" if len(ys) else "none"
        print(f"  edit {dl!r}->{new_dl!r}  raster_path={fired}  CHANGED_REGION={reg}px")
        # save a before/after crop of THIS box (faithful path: font = lb.family)
        import cv2
        bb = box.bbox
        rot = doc.working[PG].rotation_matrix
        pts = [fitz.Point(bb[0], bb[1]) * rot, fitz.Point(bb[2], bb[3]) * rot] if False else None
        # crop in page px at zoom z from the cover
        cpts = [fitz.Point(cx[0], cx[1]) * rot, fitz.Point(cx[2], cx[3]) * rot]
        px0 = max(0, int(min(p.x for p in cpts) * z) - 8)
        py0 = max(0, int(min(p.y for p in cpts) * z) - 8)
        px1 = int(max(p.x for p in cpts) * z) + 8
        py1 = int(max(p.y for p in cpts) * z) + 8
        c0 = arr0[py0:py1, px0:px1]; c1 = arr1[py0:py1, px0:px1]

        def lab(im, s):
            bar = np.full((20, im.shape[1], 3), 245, np.uint8)
            cv2.putText(bar, s, (4, 14), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (15, 15, 15), 1, cv2.LINE_AA)
            return np.vstack([bar, im, np.full((6, im.shape[1], 3), 200, np.uint8)])
        out = np.vstack([lab(c0, f"BEFORE {dl}  font={lb.family}"),
                         lab(c1, f"AFTER  {new_dl}")])
        safe = dl.replace("/", "-")
        cv2.imwrite(f"/tmp/faithful_{safe}.png", cv2.cvtColor(out, cv2.COLOR_RGB2BGR))
        print(f"  saved /tmp/faithful_{safe}.png")
    doc.close()


if __name__ == "__main__":
    main()
