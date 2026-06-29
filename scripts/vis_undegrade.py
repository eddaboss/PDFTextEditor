"""Show the glyphs the undegrade actually produces: for each date glyph, the raw scan,
my undegraded version, and Arial Bold's clean render of that character, side by side.
"""
import os
import re
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import numpy as np  # noqa: E402
import cv2  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

_app = QApplication.instance() or QApplication([])

from pdftexteditor.document import PDFDocument  # noqa: E402
from pdftexteditor.ocr import engine as E, fontbank as FB  # noqa: E402
from test_undegrade import undegrade  # noqa: E402

PDF = os.path.expanduser("~/Downloads/doc05154920260624150538.pdf")
PG = 1
DATE_RE = re.compile(r"^\d{1,2}/\d{1,2}/\d{2,4}$|^\d{1,2}/\d{4}$")
S = 80


def to_img(cov):
    img = (255 * (1 - np.clip(cov, 0, 1))).astype(np.uint8)
    h, w = img.shape
    sc = (S - 6) / max(h, w, 1)
    nh, nw = max(1, int(round(h * sc))), max(1, int(round(w * sc)))
    r = cv2.resize(img, (nw, nh), interpolation=cv2.INTER_NEAREST)
    canvas = np.full((S, S), 255, np.uint8)
    oy, ox = (S - nh) // 2, (S - nw) // 2
    canvas[oy:oy + nh, ox:ox + nw] = r
    return cv2.cvtColor(canvas, cv2.COLOR_GRAY2BGR)


def labelcol(text):
    c = np.full((S, 150, 3), 245, np.uint8)
    cv2.putText(c, text, (6, S // 2), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (15, 15, 15), 1, cv2.LINE_AA)
    return c


def main():
    doc = PDFDocument(PDF)
    rgb = doc.render_page_image(PG, 300.0)
    lines = E.get_engine("auto").recognize(rgb)
    ttf = FB._ensure_ttf_cache()
    arial = FB._cand_font(os.path.join(ttf, "00121.ttf"))
    panels = []
    for ln in lines:
        t = ln.text.strip()
        if not DATE_RE.match(t):
            continue
        x0, y0, x1, y1 = ln.bbox
        region = rgb[max(0, int(y0)):int(y1) + 1, max(0, int(x0)):int(x1) + 1]
        geom = doc._scan_geometry(region, t)
        cb = (geom or {}).get("char_boxes")
        if not cb or len(cb) != len(t):
            continue
        cov = (255.0 - region.mean(2).astype(np.float32)) / 255.0
        scan_row, clean_row, arial_row = [labelcol(f"{t}  SCAN")], [labelcol("UNDEGRADE")], [labelcol("Arial Bold")]
        for ch, bx in zip(t, cb):
            if ch == " " or bx is None:
                continue
            bx0, by0, bx1, by1 = (int(v) for v in bx)
            if bx1 - bx0 < 3 or by1 - by0 < 3:
                continue
            tile = cov[by0:by1, bx0:bx1]
            scan_row.append(to_img(tile))
            clean_row.append(to_img(undegrade(tile)))
            ar = FB._render_glyph_cov(arial, ch) if (ch in FB._CIDX and arial.has_glyph(ord(ch))) \
                else np.zeros((10, 10), np.float32)
            arial_row.append(to_img(ar))
        for r in (scan_row, clean_row, arial_row):
            panels.append(np.hstack(r))
        panels.append(np.full((10, panels[-1].shape[1], 3), 255, np.uint8))
    W = max(p.shape[1] for p in panels)
    panels = [np.pad(p, ((0, 0), (0, W - p.shape[1]), (0, 0)), constant_values=255) for p in panels]
    cv2.imwrite("/tmp/undegrade_glyphs.png", np.vstack(panels))
    print("saved /tmp/undegrade_glyphs.png")
    doc.close()


if __name__ == "__main__":
    main()
