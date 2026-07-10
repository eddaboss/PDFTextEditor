"""Pinpoint where the date WORD loses its per-glyph font in the live edit-time matcher
(pagefont). For the date line: does _scan_geometry segment it (else pagefont.build
drops the word), and does _match_cluster_tiles return a font or None (super-res needs
3+ instances/char; a date has 1-2)?
"""
import os
import re
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import numpy as np  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

_app = QApplication.instance() or QApplication([])

from pdftexteditor.document import PDFDocument  # noqa: E402
from pdftexteditor.ocr import engine as E, fontbank as FB  # noqa: E402
from pdftexteditor.ocr import pagefont as PF, supermatch as SM  # noqa: E402

PDF = os.path.expanduser("~/Downloads/doc05154920260624150538.pdf")
PG = 1
DATE_RE = re.compile(r"^\d{1,2}/\d{1,2}/\d{2,4}$|^\d{1,2}/\d{4}$")


def main():
    doc = PDFDocument(PDF)
    rgb = doc.render_page_image(PG, 300.0)
    lines = E.get_engine("auto").recognize(rgb)
    ttf = FB._ensure_ttf_cache()
    for ln in lines:
        t = ln.text.strip()
        if not DATE_RE.match(t):
            continue
        x0, y0, x1, y1 = ln.bbox
        region = rgb[max(0, int(y0)):int(y1) + 1, max(0, int(x0)):int(x1) + 1]
        geom = doc._scan_geometry(region, t)
        cb = (geom or {}).get("char_boxes")
        print(f"\n{t!r}: _scan_geometry char_boxes={len(cb) if cb else None} vs text={len(t)}"
              f"  -> pagefont.build {'DROPS this word' if not cb or len(cb) != len(t) else 'keeps it'}")
        if not cb or len(cb) != len(t):
            continue
        g = region.mean(2)
        cov = (255.0 - g.astype(np.float32)) / 255.0
        tiles_by_char = {}
        for ch, bx in zip(t, cb):
            if ch == " " or bx is None:
                continue
            bx0, by0, bx1, by1 = (int(v) for v in bx)
            if bx1 - bx0 < 3 or by1 - by0 < 3:
                continue
            tile = cov[by0:by1, bx0:bx1]
            if tile.size and float((tile > 0.3).sum()) >= 6:
                tiles_by_char.setdefault(ch, []).append(tile)
        inst = {c: len(v) for c, v in tiles_by_char.items()}
        n_superres = sum(1 for c, v in tiles_by_char.items() if SM._superres(v) is not None)
        print(f"   instances/char={inst}  (super-res needs >= {SM._MIN_INSTANCES})")
        print(f"   chars that CAN super-resolve: {n_superres} (need >= {SM._MIN_CHARS})")
        key = PF._match_cluster_tiles(tiles_by_char)
        nm = "None"
        if key:
            try:
                import fitz
                nm = fitz.Font(fontfile=os.path.join(ttf, key)).name
            except Exception:
                nm = key
        print(f"   _match_cluster_tiles -> {key} ({nm})")
    doc.close()


if __name__ == "__main__":
    main()
