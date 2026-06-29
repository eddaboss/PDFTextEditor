"""Run the REAL OCR font path on a scanned page, clean rerank vs degrade-matched, and
diff the font each cluster gets. Isolates the date field (the cluster that is mostly
digits/slash) and renders a proof: the scanned date next to the date drawn in the
clean pick and the degrade-matched pick, so a wrong-font date is visible.

    python scripts/diag_date_font.py [PDF] [PAGE]
default: ~/Downloads/doc05154920260624150538.pdf, page index 1 (the 2nd page)
"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import numpy as np  # noqa: E402
import cv2  # noqa: E402
import fitz  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

_app = QApplication.instance() or QApplication([])

from pdftexteditor.document import PDFDocument  # noqa: E402
from pdftexteditor.ocr import engine as E, reconstruct as R  # noqa: E402
from pdftexteditor.ocr import supermatch as SM, fontbank as FB  # noqa: E402

PDF = sys.argv[1] if len(sys.argv) > 1 else \
    os.path.expanduser("~/Downloads/doc05154920260624150538.pdf")
PAGE = int(sys.argv[2]) if len(sys.argv) > 2 else 1
FONTS = os.path.join(ROOT, "pdftexteditor", "assets", "fonts")
TINOS = os.path.join(FONTS, "Tinos-Regular.ttf")
ARIMO = os.path.join(FONTS, "Arimo[wght].ttf")


def run(rgb, lines, verify):
    os.environ["SUPERMATCH_VERIFY"] = verify
    R.reconstruct_page(rgb, 300.0, lines, TINOS, ARIMO)
    return {gid: (cells, key) for gid, cells, key in SM.LAST_CLUSTERS}


def date_score(cells):
    chars = [c for c, _b in cells if c.strip()]
    if not chars:
        return 0.0
    return sum(c in "0123456789/" for c in chars) / len(chars)


def order_text(cells):
    return "".join(c for c, _b in sorted(cells, key=lambda t: t[1][0]) if c.strip())


def strip_for(key, text, h=54):
    ttf = os.path.join(FB._ensure_ttf_cache(), key) if key else None
    if not ttf or not os.path.exists(ttf):
        return np.full((h, 220, 3), 230, np.uint8)
    f = fitz.Font(fontfile=ttf)
    em = h * 0.66
    W = int(f.text_length(text, em) + em)
    doc = fitz.open()
    pg = doc.new_page(width=W, height=h)
    tw = fitz.TextWriter(pg.rect)
    tw.append((em * 0.3, h * 0.74), text, font=f, fontsize=em)
    tw.write_text(pg, color=(0, 0, 0))
    pm = pg.get_pixmap(alpha=False)
    return np.frombuffer(pm.samples, np.uint8).reshape(pm.height, pm.width, pm.n)[..., :3].copy()


def label_row(img, text, w):
    img = np.pad(img, ((0, 0), (0, max(0, w - img.shape[1]), ), (0, 0)),
                 constant_values=255) if img.shape[1] < w else img[:, :w]
    bar = np.full((20, w, 3), 245, np.uint8)
    cv2.putText(bar, text, (4, 14), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (20, 20, 20), 1, cv2.LINE_AA)
    return np.vstack([bar, img, np.full((4, w, 3), 210, np.uint8)])


def main():
    print(f"PDF: {PDF}\npage index: {PAGE}")
    doc = PDFDocument(PDF)
    rgb = doc.render_page_image(PAGE, 300.0)
    lines = E.get_engine("auto").recognize(rgb)
    print(f"OCR lines: {len(lines)}")
    for ln in lines:
        print(f"   line {getattr(ln, 'text', '?')!r}")

    off = run(rgb, lines, "0")
    on = run(rgb, lines, "1")

    print("\n=== clusters: clean rerank (off) vs degrade-matched (on) ===")
    rows = []
    for gid in sorted(off):
        co, ko = off[gid]
        _cn, kn = on.get(gid, (None, None))
        txt = order_text(co)[:28]
        ds = date_score(co)
        # max instances of any single char (super-res needs >= 3)
        from collections import Counter
        hist = Counter(c for c, _b in co if c.strip())
        maxinst = max(hist.values()) if hist else 0
        flag = "  <== DATE-ish" if ds >= 0.4 else ""
        chg = "CHANGED" if ko != kn else ""
        print(f"  g{gid:<2} n={len(co):<3} maxinst={maxinst} digit%={ds:.2f} "
              f"{txt!r:30} off={ko} on={kn} {chg}{flag}")
        if ds >= 0.4:
            rows.append((gid, co, ko, kn))

    if not rows:
        print("\nno date-like cluster found")
        return
    # proof montage for the date cluster(s)
    sample = "05/16/2025"
    panels = []
    for gid, cells, ko, kn in rows:
        bx = [b for _c, b in cells]
        x0 = min(int(b[0]) for b in bx); y0 = min(int(b[1]) for b in bx)
        x1 = max(int(b[2]) for b in bx); y1 = max(int(b[3]) for b in bx)
        scan = rgb[max(0, y0 - 4):y1 + 4, max(0, x0 - 4):x1 + 4]
        w = max(scan.shape[1], 360)
        panels.append(label_row(scan, f"g{gid} SCAN  text={order_text(cells)[:24]!r}", w))
        panels.append(label_row(strip_for(ko, sample), f"OFF clean   {ko}", w))
        panels.append(label_row(strip_for(kn, sample), f"ON  degrade {kn}", w))
        panels.append(np.full((8, w, 3), 255, np.uint8))
    W = max(p.shape[1] for p in panels)
    panels = [np.pad(p, ((0, 0), (0, W - p.shape[1]), (0, 0)), constant_values=255) for p in panels]
    out = "/tmp/date_font.png"
    cv2.imwrite(out, cv2.cvtColor(np.vstack(panels), cv2.COLOR_RGB2BGR))
    print(f"\nsaved {out}")
    doc.close()


if __name__ == "__main__":
    main()
