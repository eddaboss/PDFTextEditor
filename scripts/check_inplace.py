"""FACT check (not a looks-good capture) for the in-place scanned editor.
Proves: editing a line keeps the UNCHANGED characters as the literal scan pixels
(byte-identical), and only the edited run differs. Real OCR pipeline.
Run: PYTHONPATH=. ~/Documents/GitHub/PDFTextEditor/.venv/bin/python scripts/check_inplace.py
"""
import os, sys, tempfile
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import numpy as np, cv2
from PySide6.QtWidgets import QApplication
_app = QApplication.instance() or QApplication([])
from pdftexteditor.document import PDFDocument
from pdftexteditor.ocr import engine as E, reconstruct as R
from pdftexteditor.font_engine import FontEngine
from scripts.diag_ocr_font import make_scan, FONTS
import os.path as P


def main():
    with tempfile.TemporaryDirectory() as d:
        path = P.join(d, "scan.pdf"); make_scan(path)
        doc = PDFDocument(path)
        rgb = doc.render_page_image(0, 300.0)
        lines = E.get_engine("auto").recognize(rgb)
        res = R.reconstruct_page(rgb, 300.0, lines,
                                 P.join(FONTS, "Tinos-Regular.ttf"),
                                 P.join(FONTS, "Arimo[wght].ttf"))
        if res.otf_bytes:
            FontEngine.register_custom_face(res.family_name, res.otf_bytes)
        lb = next(l for l in res.lines if "Orders read back" in l.text)
        o, dirn = doc.ocr_text_placement(0, lb.origin)
        cov = tuple(doc.ocr_cover_rect(0, lb.cover)) + tuple(lb.bg)
        box = doc.add_box(0, o, lb.text, res.family_name, lb.size, (0, 0, 0),
                          False, False, direction=dirn, cover=cov, render_mode=3)
        ctx = doc.scan_edit_context(box, box.text)
        assert ctx is not None, "FAIL: no scan context"
        region = ctx["region"]

        # 1) recompose with the SAME text -> must be ~identical to the scan region
        same = doc.inplace_compose(ctx, box.text)
        d_same = float(np.abs(same.astype(int) - region.astype(int)).mean())
        print(f"recompose-unchanged mean|diff| vs scan = {d_same:.3f}  (want ~0)")

        # 2) edit MD->RN; the untouched PREFIX must be BYTE-IDENTICAL to the scan
        new = box.text.replace("MD", "RN")
        pre = os.path.commonprefix([box.text, new])
        edit = doc.inplace_compose(ctx, new)
        import fitz
        f = fitz.Font(fontfile=ctx["fpath"])
        x_pre = int(ctx["left_px"] + f.text_length(box.text[:len(pre)], ctx["em"]) * ctx["ppi"])
        x_pre = max(0, x_pre - 2)
        ident = np.array_equal(edit[:, :x_pre], region[:, :x_pre])
        print(f"edit MD->RN: prefix [:{x_pre}px] byte-identical to scan = {ident}")
        changed = float(np.abs(edit[:, x_pre:].astype(int) - region[:, x_pre:].astype(int)).mean())
        print(f"           : tail (after prefix) mean|diff| = {changed:.2f}  (want > 0, the edit)")

        cv2.imwrite("/tmp/inplace_region.png", cv2.cvtColor(region, cv2.COLOR_RGB2BGR))
        cv2.imwrite("/tmp/inplace_edit.png", cv2.cvtColor(edit, cv2.COLOR_RGB2BGR))
        print("PASS" if (d_same < 1.5 and ident and changed > 0.5) else "*** CHECK NUMBERS ***")
        doc.close()


if __name__ == "__main__":
    main()
