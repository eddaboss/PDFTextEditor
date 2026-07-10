"""Reproduce the user's case: a GRAINY multi-line form scan. Answer two facts:
(1) is the edited line grouped into a PARAGRAPH box (is_paragraph)?
(2) does an edit keep the UNTOUCHED text's grain (i.e. real scan pixels)?
Run: PYTHONPATH=. ~/Documents/GitHub/PDFTextEditor/.venv/bin/python scripts/check_grainy.py
"""
import os, sys, tempfile, io
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import numpy as np, cv2, fitz
from PySide6.QtWidgets import QApplication
_app = QApplication.instance() or QApplication([])
from pdftexteditor.document import PDFDocument
from pdftexteditor.ocr import engine as E, reconstruct as R, degrade as D
from pdftexteditor.font_engine import FontEngine
import os.path as P

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FONTS = P.join(ROOT, "pdftexteditor", "assets", "fonts")
MONO = P.join(FONTS, "Cousine-Regular.ttf")
LINES = ["ORDER / TELEPHONE ORDERS:",
         "Home Health Physical Therapy frequency and duration 2wk4 1wk2",
         "(x) Orders read back and verified with MD"]


def make_grainy(path):
    f = fitz.Font(fontfile=MONO); EM = 13.0
    W = 60 + max(f.text_length(t, EM) for t in LINES)
    doc = fitz.open(); pg = doc.new_page(width=W, height=170)
    tw = fitz.TextWriter(pg.rect); y = 40
    for t in LINES:
        tw.append((30, y), t, font=f, fontsize=EM); y += 40
    tw.write_text(pg, color=(0.1, 0.1, 0.1))
    pm = pg.get_pixmap(matrix=fitz.Matrix(300 / 72, 300 / 72), alpha=False)
    rgb = np.frombuffer(pm.samples, np.uint8).reshape(pm.height, pm.width, pm.n)[..., :3].copy()
    rgb = D.hard_degrade(rgb.astype(np.float32), np.array([24, 24, 30], np.float32),
                         np.array([247, 244, 240], np.float32), 0.42,
                         np.random.RandomState(3))
    import PIL.Image
    buf = io.BytesIO(); PIL.Image.fromarray(rgb).save(buf, "PNG")
    out = fitz.open(); page = out.new_page(width=W, height=170)
    page.insert_image(page.rect, stream=buf.getvalue()); out.save(path)


def grain(a):
    """High-frequency speckle: std of (image - 3x3 median). Grainy >> clean."""
    m = cv2.medianBlur(a, 3)
    return float(np.abs(a.astype(int) - m.astype(int)).mean())


def main():
    with tempfile.TemporaryDirectory() as d:
        path = P.join(d, "s.pdf"); make_grainy(path)
        doc = PDFDocument(path)
        rgb = doc.render_page_image(0, 300.0)
        res = R.reconstruct_page(rgb, 300.0, E.get_engine("auto").recognize(rgb),
                                 P.join(FONTS, "Tinos-Regular.ttf"),
                                 P.join(FONTS, "Arimo[wght].ttf"))
        if res.otf_bytes:
            FontEngine.register_custom_face(res.family_name, res.otf_bytes)
        print("=== BOXES ===")
        for lb in res.lines:
            print(f"  is_paragraph={lb.is_paragraph}  text={lb.text[:48]!r}")
        lb = next((l for l in res.lines if "Orders read back" in l.text), None)
        if lb is None:
            print("target line not found"); return
        print(f"\nTARGET is_paragraph = {lb.is_paragraph}")
        o, dn = doc.ocr_text_placement(0, lb.origin)
        cov = tuple(doc.ocr_cover_rect(0, lb.cover)) + tuple(lb.bg)
        box = doc.add_box(0, o, lb.text, res.family_name, lb.size, (0, 0, 0),
                          False, False, direction=dn, cover=cov, render_mode=3,
                          box_w=lb.box_w, leading=lb.leading)
        ctx = doc.scan_edit_context(box, box.text)
        print("scan_edit_context built:", ctx is not None,
              "(None => in-place can't run for this box)")
        if ctx is None:
            return
        region = ctx["region"]
        edit = doc.inplace_compose(ctx, box.text.replace("MD", "RN"))
        pre = len(os.path.commonprefix([box.text, box.text.replace("MD", "RN")]))
        xp = max(0, int(ctx["left_px"] + fitz.Font(fontfile=ctx["fpath"]).text_length(
            box.text[:pre], ctx["em"]) * ctx["ppi"]) - 2)
        g_scan = grain(region[:, :xp]); g_edit = grain(edit[:, :xp])
        print(f"\nUNCHANGED prefix grain: scan={g_scan:.2f}  edit={g_edit:.2f}  "
              f"(equal => kept the real grainy pixels)")
        print("byte-identical:", np.array_equal(region[:, :xp], edit[:, :xp]))
        doc.close()


if __name__ == "__main__":
    main()
