#!/usr/bin/env python3
"""0.3.0 scanned-edit blend: an edited OCR word on a scanned page is drawn as a
recolored + hard-damaged RASTER over its cover (so it matches the scan's ink and
degradation), not crisp vector text -- and an UNEDITED overlay stays pixel-identical
to the scan, and the result saves + reopens stably.

Run:  QT_QPA_PLATFORM=offscreen python tests/test_ocr_blend.py
"""
import io
import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import fitz  # noqa: E402
import numpy as np  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

_APP = QApplication.instance() or QApplication([])

from pdftexteditor.document import PDFDocument  # noqa: E402
from pdftexteditor.ocr import degrade as D  # noqa: E402

SERIF = os.path.join(ROOT, "pdftexteditor", "assets", "fonts", "Tinos-Regular.ttf")
EM = 16.0
MARGIN = 20.0
BASELINE = 50.0
PRE, WORD, POST = "Total ", "sixty", " owed today"


def _scanned_pdf(path):
    """A one-page PDF whose page IS a degraded scan of 'Total sixty owed today'.
    Returns the cover rect (points) of 'sixty' and the paper color."""
    f = fitz.Font(fontfile=SERIF)
    x_word0 = MARGIN + f.text_length(PRE, EM)
    x_word1 = MARGIN + f.text_length(PRE + WORD, EM)
    Wpt = MARGIN + f.text_length(PRE + WORD + POST, EM) + MARGIN
    Hpt = 90.0
    # render clean, then degrade with the hard filter to make a "scan"
    doc = fitz.open(); pg = doc.new_page(width=Wpt, height=Hpt)
    tw = fitz.TextWriter(pg.rect)
    tw.append((MARGIN, BASELINE), PRE + WORD + POST, font=f, fontsize=EM)
    tw.write_text(pg, color=(0.1, 0.1, 0.12))
    zoom = 2.0
    pm = pg.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
    rgb = np.frombuffer(pm.samples, np.uint8).reshape(pm.height, pm.width, pm.n)[..., :3].copy()
    scan = D.hard_degrade(rgb.astype(np.float32), np.array([26, 26, 40], np.float32),
                          np.array([248, 247, 244], np.float32), 0.35,
                          np.random.RandomState(0))
    # embed the degraded raster as the page
    import PIL.Image
    buf = io.BytesIO(); PIL.Image.fromarray(scan).save(buf, "PNG")
    out = fitz.open(); page = out.new_page(width=Wpt, height=Hpt)
    page.insert_image(page.rect, stream=buf.getvalue())
    out.save(path); out.close(); doc.close()
    pad = 0.08 * EM
    cover = (x_word0 - pad, BASELINE - 0.80 * EM, x_word1 + pad, BASELINE + 0.26 * EM,
             248 / 255, 247 / 255, 244 / 255)
    return cover, (x_word0, BASELINE)


def _px(pm):
    a = np.frombuffer(pm.samples, np.uint8).reshape(pm.height, pm.width, pm.n)
    return a[:, :, :3].astype(np.int16)


def main():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "scan.pdf")
        cover, origin = _scanned_pdf(path)
        doc = PDFDocument(path)
        base = _px(doc.render(0, 2.0))

        # invisible OCR overlay over the scanned word, in a bundled matched family
        box = doc.add_box(0, origin, WORD, "Tinos", EM, (0.0, 0.0, 0.0),
                          False, False, cover=cover, render_mode=3)
        inv = _px(doc.render_with_edits(0, 2.0))
        assert int(np.abs(inv - base).max()) == 0, "invisible overlay must match the scan"
        print("  ok  invisible overlay renders pixel-identical to the scan")

        # edit the word -> a blended raster should be built and drawn
        doc.stage_edit(0, box, "forty")
        eb = doc._new_boxes[box.edit_key]
        assert eb.render_mode == 0, "edit flips the box visible"
        assert eb.edit_image and eb.edit_image_rect, "edited scanned word must build a blend raster"
        print(f"  ok  edit built a blend raster ({len(eb.edit_image)} bytes)")

        edited = _px(doc.render_with_edits(0, 2.0))
        # within the cover region the page changed, and the edit is NOT a flat fill
        cx0, cy0, cx1, cy1 = (int(cover[0] * 2), int(cover[1] * 2), int(cover[2] * 2), int(cover[3] * 2))
        reg = edited[cy0:cy1, cx0:cx1]
        assert int(np.abs(edited - base).sum()) > 0, "edited word must change the page"
        g = reg.reshape(-1, 3).mean(1)
        dark = (g < 120).mean(); light = (g > 200).mean()
        assert dark > 0.02 and light > 0.2, f"edit should be ink-on-paper (dark {dark:.2f} light {light:.2f})"
        # 'hard' / degraded: a spread of values, not a single flat black or a smooth ramp
        assert g.min() < 90 and g.max() > 210, "edit must have solid ink AND paper (hard, not flat)"
        print(f"  ok  edited word is ink-on-paper and hard (dark {dark:.2f} light {light:.2f})")

        # save -> reopen -> render must be stable (no save-path corruption)
        out = os.path.join(d, "out.pdf")
        doc.save_as(out)
        doc2 = PDFDocument(out)
        re = _px(doc2.render(0, 2.0))
        assert re.shape == edited.shape
        # the baked save and the on-screen edit must agree (screen == save invariant)
        diff = float(np.abs(re - edited).mean())
        assert diff < 6.0, f"save and screen disagree (mean diff {diff:.2f})"
        print(f"  ok  save -> reopen stable (mean diff {diff:.2f})")
        doc2.close(); doc.close()
    print("\n4 ocr-blend tests passed.")


if __name__ == "__main__":
    main()
