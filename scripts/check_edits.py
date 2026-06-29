"""Test the user's exact failing edits on a clean scan: INSERT a word mid-line,
and DELETE a mid-line word. Verify no garbage + untouched parts stay exact.
Run: PYTHONPATH=. ~/Documents/GitHub/PDFTextEditor/.venv/bin/python scripts/check_edits.py
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
        path = P.join(d, "s.pdf"); make_scan(path)
        doc = PDFDocument(path)
        rgb = doc.render_page_image(0, 300.0)
        res = R.reconstruct_page(rgb, 300.0, E.get_engine("auto").recognize(rgb),
                                 P.join(FONTS, "Tinos-Regular.ttf"), P.join(FONTS, "Arimo[wght].ttf"))
        if res.otf_bytes:
            FontEngine.register_custom_face(res.family_name, res.otf_bytes)
        lb = next(l for l in res.lines if "Orders read back" in l.text)
        o, dn = doc.ocr_text_placement(0, lb.origin)
        cov = tuple(doc.ocr_cover_rect(0, lb.cover)) + tuple(lb.bg)
        box = doc.add_box(0, o, lb.text, res.family_name, lb.size, (0, 0, 0),
                          False, False, direction=dn, cover=cov, render_mode=3)
        ctx = doc.scan_edit_context(box, box.text)
        ot = ctx["orig_text"]; region = ctx["region"]
        print("orig:", repr(ot))

        for label, new in [("INSERT 'testing'", ot.replace("and verified", "and testing verified")),
                           ("DELETE 'and'", ot.replace("read back and verified", "read back verified"))]:
            tile = doc.inplace_compose(ctx, new)
            # untouched common prefix must be byte-identical to the scan
            pre = len(os.path.commonprefix([ot, new]))
            import fitz; f = fitz.Font(fontfile=ctx["fpath"])
            base_em = ctx["em"]; full = f.text_length(ot, base_em) * ctx["ppi"]
            scale = (ctx["right_px"] - ctx["left_px"]) / full
            xp = int(ctx["left_px"] + f.text_length(ot[:pre], base_em * scale) * ctx["ppi"]) - 2
            ident = np.array_equal(tile[:, :max(0, xp)], region[:, :max(0, xp)])
            fn = "/tmp/edit_" + label.split()[0].lower() + ".png"
            cv2.imwrite(fn, cv2.cvtColor(tile, cv2.COLOR_RGB2BGR))
            print(f"{label:18s} -> new={new!r}")
            print(f"  untouched prefix [:{max(0,xp)}px] byte-identical = {ident}   saved {fn}")
        doc.close()


if __name__ == "__main__":
    main()
