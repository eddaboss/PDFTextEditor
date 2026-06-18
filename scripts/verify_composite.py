"""End-to-end check of the pixel-preserving composite scanned-edit path.

Drives the REAL Document path: build a one-page 'scan', inject an invisible OCR
NewBox (add_box, render_mode=3, cover), stage an edit (delete / replace a word),
bake with render_with_edits, and crop the line. Kept words must be the original
scan pixels; only changed words are synthesized; the line reflows clean.

Run: PYTHONPATH=. .venv/bin/python scripts/verify_composite.py
Writes /tmp/composite_*.png (clean + degraded x delete + replace).
NEUTRAL text only -- no real names.
"""
import os, sys, tempfile
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import numpy as np, cv2, fitz
from PySide6.QtGui import QGuiApplication

_app = QGuiApplication.instance() or QGuiApplication([])

from pdftexteditor.document import PDFDocument as Document
from pdftexteditor.ocr import degrade as D

LINE = "the quick brown fox jumps over"
SIZE = 15
X0, BASE = 40.0, 60.0           # left, baseline (pt)
PAGE_W, PAGE_H = 360.0, 96.0
DPI = 300.0
SERIF = "/System/Library/Fonts/Supplemental/Times New Roman.ttf"


def make_scan_pdf(degraded: bool) -> str:
    """Render the line, optionally degrade to look scanned, return a PDF path
    whose single page IS that raster (an image-only scan page)."""
    doc = fitz.open(); pg = doc.new_page(width=PAGE_W, height=PAGE_H)
    f = fitz.Font(fontfile=SERIF); tw = fitz.TextWriter(pg.rect)
    tw.append((X0, BASE), LINE, font=f, fontsize=SIZE)
    tw.write_text(pg, color=(0.04, 0.04, 0.04))
    pm = pg.get_pixmap(matrix=fitz.Matrix(DPI / 72, DPI / 72), alpha=False)
    rgb = np.frombuffer(pm.samples, np.uint8).reshape(
        pm.height, pm.width, pm.n)[..., :3].copy()
    if degraded:
        R = np.random.RandomState(7)
        rgb = D.hard_degrade(rgb.astype(np.float32),
                             np.array([22, 22, 26], np.float32),
                             np.array([247, 246, 243], np.float32), 0.42, R)
    out = fitz.open(); opg = out.new_page(width=PAGE_W, height=PAGE_H)
    ok, buf = cv2.imencode(".png", cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR))
    opg.insert_image(opg.rect, stream=buf.tobytes())
    p = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False).name
    out.save(p)
    return p


def cover_and_origin(doc: Document):
    """Measure the line's ink bbox (pt) for the cover + the paper colour."""
    rgb = doc.render_page_image(0, DPI)
    ppi = DPI / 72.0
    g = rgb.mean(2)
    dark = g < float(np.percentile(g, 85)) * 0.7
    ys, xs = np.where(dark.any(1))[0], np.where(dark.any(0))[0]
    pad = 2
    cx0 = (xs.min() - pad) / ppi; cy0 = (ys.min() - pad) / ppi
    cx1 = (xs.max() + pad) / ppi; cy1 = (ys.max() + pad) / ppi
    bright = rgb.reshape(-1, 3)[g.reshape(-1) > np.percentile(g, 80)]
    paper = tuple(float(c) / 255 for c in np.median(bright, 0))
    return (cx0, cy0, cx1, cy1) + paper, (cx0, BASE)


def bake_line_crop(doc: Document, cover) -> np.ndarray:
    z = 4.0
    pm = doc.render_with_edits(0, z)
    arr = np.frombuffer(pm.samples, np.uint8).reshape(
        pm.height, pm.width, pm.n)[..., :3]
    x0, y0, x1, y1 = (int(c * z) for c in cover[:4])
    return arr[max(0, y0 - 8):y1 + 8, max(0, x0 - 8):x1 + 8].copy()


def label(img, text):
    bar = np.full((22, img.shape[1], 3), 245, np.uint8)
    cv2.putText(bar, text, (6, 15), cv2.FONT_HERSHEY_SIMPLEX, 0.45,
                (10, 10, 10), 1, cv2.LINE_AA)
    return np.vstack([bar, img])


def run(degraded: bool):
    tag = "degraded" if degraded else "clean"
    path = make_scan_pdf(degraded)
    rows = []
    for new_text, op in [(LINE, "ORIGINAL (no edit)"),
                         ("the quick fox jumps over", "DELETE 'brown'"),
                         ("the quick brown cat jumps over", "REPLACE fox->cat")]:
        doc = Document(path)
        cover, origin = cover_and_origin(doc)
        box = doc.add_box(0, origin, LINE, "Tinos", SIZE, (0, 0, 0),
                          False, False, cover=cover, render_mode=3)
        if new_text != LINE:
            doc.stage_edit(0, box, new_text)
            cur = doc._new_boxes[box.edit_key]
            print(f"  [{tag}] {op:22s} edit_image={'yes' if cur.edit_image else 'NO (fell back!)'}")
        crop = bake_line_crop(doc, cover)
        rows.append(label(crop, f"{tag}: {op}"))
        doc.close() if hasattr(doc, "close") else None
    w = max(r.shape[1] for r in rows)
    rows = [np.pad(r, ((0, 0), (0, w - r.shape[1]), (0, 0)),
                   constant_values=235) for r in rows]
    seps = []
    for r in rows:
        seps += [r, np.full((4, w, 3), 150, np.uint8)]
    stacked = np.vstack(seps[:-1])
    outp = f"/tmp/composite_{tag}.png"
    cv2.imwrite(outp, cv2.cvtColor(stacked, cv2.COLOR_RGB2BGR))
    print(f"  saved {outp}")


if __name__ == "__main__":
    print("font:", Document.__module__)
    run(False)
    run(True)
