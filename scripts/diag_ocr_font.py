"""Trace the REAL OCR font path: what family a box gets, whether the edit font
resolves, and what the edit actually renders in. Reproduces the 'edit is Arial,
not the scan font' bug end to end. Neutral boilerplate text, no PHI."""
import os, sys, tempfile
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import numpy as np, cv2, fitz
from PySide6.QtWidgets import QApplication
_app = QApplication.instance() or QApplication([])

from pdftexteditor.document import PDFDocument
from pdftexteditor.ocr import engine as E, reconstruct as R
from pdftexteditor.font_engine import FontEngine

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FONTS = os.path.join(ROOT, "pdftexteditor", "assets", "fonts")
MONO = os.path.join(FONTS, "Cousine-Regular.ttf")
LINES = ["ORDER / TELEPHONE ORDERS:",
         "Home Health Physical Therapy frequency and duration 2wk4 1wk2",
         "(x) Orders read back and verified with MD"]
EM = 13.0


def make_scan(path):
    f = fitz.Font(fontfile=MONO)
    W = 60 + max(f.text_length(t, EM) for t in LINES)
    doc = fitz.open(); pg = doc.new_page(width=W, height=170)
    tw = fitz.TextWriter(pg.rect); y = 40
    for t in LINES:
        tw.append((30, y), t, font=f, fontsize=EM); y += 40
    tw.write_text(pg, color=(0.10, 0.10, 0.10))
    pm = pg.get_pixmap(matrix=fitz.Matrix(300 / 72, 300 / 72), alpha=False)
    rgb = np.frombuffer(pm.samples, np.uint8).reshape(pm.height, pm.width, pm.n)[..., :3].copy()
    import PIL.Image, io
    buf = io.BytesIO(); PIL.Image.fromarray(rgb).save(buf, "PNG")
    out = fitz.open(); page = out.new_page(width=W, height=170)
    page.insert_image(page.rect, stream=buf.getvalue()); out.save(path); out.close(); doc.close()


def main():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "scan.pdf"); make_scan(path)
        doc = PDFDocument(path)
        rgb = doc.render_page_image(0, 300.0)
        eng = E.get_engine("auto")
        print("ENGINE:", type(eng).__name__)
        lines = eng.recognize(rgb)
        print("OCR lines:", [l.text for l in lines])
        res = R.reconstruct_page(rgb, 300.0, lines,
                                 os.path.join(FONTS, "Tinos-Regular.ttf"),
                                 os.path.join(FONTS, "Arimo[wght].ttf"))
        if res is None:
            print("reconstruct returned None"); return
        print(f"\n=== MATCH RESULT ===")
        print("family_name:", res.family_name, "| otf_bytes:", len(res.otf_bytes),
              "| n_lines:", res.n_lines)
        if res.otf_bytes:
            FontEngine.register_custom_face(res.family_name, res.otf_bytes)
        for lb in res.lines:
            print(f"  box: size={lb.size:.1f} para={lb.is_paragraph} text={lb.text[:46]!r}")

        # resolve the edit font exactly as the document would
        fpath = doc._edit_font_file(res.family_name)
        print(f"\n_edit_font_file({res.family_name!r}) -> {fpath}")
        if fpath:
            try:
                ftest = fitz.Font(fontfile=fpath)
                print("   fitz.Font opens it OK:", ftest.name)
            except Exception as e:
                print("   fitz.Font FAILS:", e)
        else:
            print("   -> NONE: edit raster cannot build, bake falls back to vector text")

        # build the box for line 3 and edit it, see if a raster is built
        target = None
        for lb in res.lines:
            if "Orders read back" in lb.text or "read back" in lb.text:
                target = lb; break
        target = target or res.lines[-1]
        origin, direction = doc.ocr_text_placement(0, target.origin)
        cx = doc.ocr_cover_rect(0, target.cover)
        cover = tuple(cx) + tuple(target.bg)
        box = doc.add_box(0, origin, target.text, res.family_name, target.size,
                          (0, 0, 0), False, False, direction=direction, cover=cover,
                          render_mode=3, box_w=target.box_w, leading=target.leading)
        new = target.text.replace("MD", "RN") if "MD" in target.text else target.text + " X"
        doc.stage_edit(0, box, new)
        eb = doc._new_boxes[box.edit_key]
        print(f"\n=== EDIT of {target.text[:40]!r} -> {new[:40]!r} ===")
        print("   edit_image built (raster blend):", bool(eb.edit_image))

        # What FONT does the LIVE INLINE EDITOR use for this family? (the red-box
        # text the user types into). If Qt does not know the custom face, it falls
        # back to a default sans -> "Arial".
        from PySide6.QtGui import QFont, QFontDatabase, QFontInfo
        fams = set(QFontDatabase.families())
        print("\n=== EDITOR (Qt) FONT for", res.family_name, "===")
        print("   family registered in QFontDatabase:", res.family_name in fams)
        qf = QFont(res.family_name, 12)
        print("   QFont(family).exactMatch():", qf.exactMatch(),
              "| QFontInfo resolves to:", QFontInfo(qf).family())
        # what the document's editor-resolver picks
        try:
            rf = FontEngine.resolve_family(res.family_name, False, False, new)
            print("   FontEngine.resolve_family ->", rf)
        except Exception as e:
            print("   resolve_family err:", e)

        # bake + crop the line
        z = 4.0; pm = doc.render_with_edits(0, z)
        arr = np.frombuffer(pm.samples, np.uint8).reshape(pm.height, pm.width, pm.n)[..., :3]
        x0, y0, x1, y1 = (int(c * z) for c in cover[:4])
        crop = arr[max(0, y0 - 10):y1 + 10, max(0, x0 - 6):x1 + 80]
        cv2.imwrite("/tmp/repro_real.png", cv2.cvtColor(crop, cv2.COLOR_RGB2BGR))
        print("   saved /tmp/repro_real.png")
        doc.close()


if __name__ == "__main__":
    main()
