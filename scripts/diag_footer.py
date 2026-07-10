"""Why does the footer line never get a map? Measure it, and test whether _relayout_word
(my numeric-field repair) throws on its numeric words and kills the whole line's map."""
import os
import sys
import traceback

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
FONTS = os.path.join(ROOT, "pdftexteditor", "assets", "fonts")
TINOS = os.path.join(FONTS, "Tinos-Regular.ttf")
ARIMO = os.path.join(FONTS, "Arimo[wght].ttf")


def build(doc, pg, lb, prgb=None, app=False):
    o, d = doc.ocr_text_placement(pg, lb.origin)
    cx = doc.ocr_cover_rect(pg, lb.cover)
    lcs = tuple(tuple(doc.ocr_cover_rect(pg, lc[:4])) + tuple(lc[4:7])
                for lc in (lb.line_covers or ()))
    cover = tuple(cx) + tuple(lb.bg)
    text = lb.text
    fam = lb.family or "Arimo"
    if app:                                   # mirror main_window's OCR-overlay construction
        _nl = len([s for s in lb.text.split("\n") if s.strip()]) or 1
        cover = doc._tight_cover(pg, cover, prgb, nlines=_nl)
        if cover:
            text = doc.respace_ocr_text(pg, cover, lcs, lb.text, fam, lb.size, d, page_rgb=prgb)
    return doc.add_box(pg, o, text, fam, lb.size, (0, 0, 0), False,
                       False, direction=d, cover=cover, render_mode=3,
                       box_w=lb.box_w, leading=lb.leading, line_covers=lcs)


def main():
    doc = PDFDocument(PDF)
    for pg in range(doc.page_count):
        rgb = doc.render_page_image(pg, 300.0)
        res = R.reconstruct_page(rgb, 300.0, E.get_engine("auto").recognize(rgb), TINOS, ARIMO)
        if res is None:
            continue
        if res.otf_bytes:
            FontEngine.register_custom_face(res.family_name, res.otf_bytes)
        foot = [lb for lb in res.lines if "American Heart" in lb.text or "R3/" in lb.text
                or lb.text.count(" ") >= 6]
        import fitz, cv2
        for lb in foot:
            if "20-3001" not in lb.text and "R3/2" not in lb.text and "American Heart" not in lb.text:
                continue
            o, d = doc.ocr_text_placement(pg, lb.origin)
            cx = doc.ocr_cover_rect(pg, lb.cover)
            cover0 = tuple(cx) + tuple(lb.bg)
            _nl = len([s for s in lb.text.split("\n") if s.strip()]) or 1
            covT = doc._tight_cover(pg, cover0, rgb, nlines=_nl)
            if abs(cover0[3] - cover0[1]) < 1e-6:
                continue
            print(f"\nFOOTER pg{pg}  {lb.text[:50]!r}")
            print(f"  cover orig h={cover0[3]-cover0[1]:.1f}  tight h={covT[3]-covT[1]:.1f}")
            # crop the page render at the ORIGINAL cover via the same rot/ppi mapping _tight_cover uses
            ppi = 300.0 / 72.0
            page = doc.working[pg]; rot = page.rotation_matrix
            H, W = rgb.shape[:2]
            pts = [fitz.Point(px, py) * rot for px, py in
                   ((cover0[0], cover0[1]), (cover0[2], cover0[1]),
                    (cover0[2], cover0[3]), (cover0[0], cover0[3]))]
            rx0, ry0 = max(0, int(round(min(p.x for p in pts) * ppi))), max(0, int(round(min(p.y for p in pts) * ppi)))
            rx1, ry1 = min(W, int(round(max(p.x for p in pts) * ppi))), min(H, int(round(max(p.y for p in pts) * ppi)))
            crop = rgb[ry0:ry1, rx0:rx1]
            dark = crop.mean(2) < 165.0
            colcount = dark.sum(1)
            ch = crop.shape[0]
            prof = "".join("#" if c > 0.5 * (colcount.max() or 1) else ("." if c > 0 else " ")
                           for c in colcount)
            print(f"  crop {crop.shape[:2]}  row-ink profile (top..bot): [{prof}]")
            covdy = covT[1] - cover0[1]
            kt = int(round((covT[1] - cover0[1]) * ppi)); kb = int(round((covT[3] - cover0[1]) * ppi))
            print(f"  tight keeps rows {kt}..{kb} of 0..{ch}")
            z = cv2.resize(crop, None, fx=8, fy=8, interpolation=cv2.INTER_NEAREST)
            cv2.rectangle(z, (0, max(0, kt) * 8), (z.shape[1] - 1, min(ch, kb) * 8), (0, 0, 220), 2)
            cv2.imwrite(f"/tmp/diag_footer_pg{pg}.png", cv2.cvtColor(z, cv2.COLOR_RGB2BGR))

            def seg_diag(cr):
                g = cr.mean(2).astype(np.uint8)
                _, bw = cv2.threshold(g, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
                if (bw > 0).mean() > 0.5:
                    bw = 255 - bw
                ink = bw > 0
                H, Wr = ink.shape
                n, lab, st, _c = cv2.connectedComponentsWithStats(ink.astype(np.uint8), 8)
                struct = {i for i in range(1, n) if st[i, cv2.CC_STAT_WIDTH] >= 0.55 * Wr}
                rows = (ink.sum(1)).astype(float)
                otsu_t = cv2.threshold(g, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)[0]
                thr = 0.30 * (rows.max() or 1)
                bands, r = [], 0
                while r < H:
                    if rows[r] >= thr:
                        s = r
                        while r < H and rows[r] >= thr:
                            r += 1
                        bands.append((s, r))
                    else:
                        r += 1
                return dict(otsu=otsu_t, ink_frac=round(float(ink.mean()), 3), n=n - 1,
                           struct=len(struct), nbands=len(bands), maxrow=int(rows.max()))

            # crop at tight cover the same way
            ptsT = [fitz.Point(px, py) * rot for px, py in
                    ((covT[0], covT[1]), (covT[2], covT[1]), (covT[2], covT[3]), (covT[0], covT[3]))]
            tx0, ty0 = max(0, int(round(min(p.x for p in ptsT) * ppi))), max(0, int(round(min(p.y for p in ptsT) * ppi)))
            tx1, ty1 = min(W, int(round(max(p.x for p in ptsT) * ppi))), min(H, int(round(max(p.y for p in ptsT) * ppi)))
            cropT = rgb[ty0:ty1, tx0:tx1]
            print(f"  ORIG  crop: {seg_diag(crop)}")
            print(f"  TIGHT crop: {seg_diag(cropT)}")
    doc.close()


if __name__ == "__main__":
    main()
