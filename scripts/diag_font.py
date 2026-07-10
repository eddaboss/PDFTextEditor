"""What synth font does the REAL edit ctx (with the re-laid char_boxes map) pick for the
date, vs a minimal ctx? Isolates whether the map re-layout shifted the font match."""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

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


def nm(fp):
    try:
        return fitz.Font(fontfile=fp).name if fp and os.path.exists(fp) else fp
    except Exception:
        return fp


def main():
    doc = PDFDocument(PDF)
    rgb = doc.render_page_image(PG, 300.0)
    res = R.reconstruct_page(rgb, 300.0, E.get_engine("auto").recognize(rgb), TINOS, ARIMO)
    if res.otf_bytes:
        FontEngine.register_custom_face(res.family_name, res.otf_bytes)
    target = next(lb for lb in res.lines if lb.text.endswith("2024") and "5/16" in lb.text)
    origin, direction = doc.ocr_text_placement(PG, target.origin)
    cx = doc.ocr_cover_rect(PG, target.cover)
    line_covers = ()
    if getattr(target, "line_covers", None):
        line_covers = tuple(tuple(doc.ocr_cover_rect(PG, lc[:4])) + tuple(lc[4:7])
                            for lc in target.line_covers)
    box = doc.add_box(PG, origin, target.text, target.family or res.family_name, target.size,
                      (0, 0, 0), False, False, direction=direction,
                      cover=tuple(cx) + tuple(target.bg), render_mode=3,
                      box_w=target.box_w, leading=target.leading, line_covers=line_covers)
    meas = doc.measure_box_glyphs(box)
    ml = meas["lines"][len(box.text.split("\n")) - 1]
    ctx = ml["ctx"]
    print("date line ctx HAS char_boxes:", bool((ctx.get("geom") or {}).get("char_boxes")))
    sf = doc._edit_synth_font(ctx)
    print(f"_edit_synth_font(real ctx) -> {os.path.basename(sf) if sf else None}  ({nm(sf)})")
    sm = doc._synth_metrics(ctx)
    print(f"_synth_metrics synth_fpath -> {os.path.basename(sm[2]) if sm[2] else None}  ({nm(sm[2])})")
    doc.close()


if __name__ == "__main__":
    main()
