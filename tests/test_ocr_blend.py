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


LINE = "Total sixty owed today"


def _scanned_line_pdf(path, degraded):
    """A one-page PDF whose page IS a (clean OR degraded) scan of the whole LINE.
    Returns the cover rect (points) spanning the WHOLE line + the baseline origin,
    so the composite sees every word."""
    f = fitz.Font(fontfile=SERIF)
    Wpt = MARGIN + f.text_length(LINE, EM) + MARGIN
    Hpt = 80.0
    doc = fitz.open(); pg = doc.new_page(width=Wpt, height=Hpt)
    tw = fitz.TextWriter(pg.rect)
    tw.append((MARGIN, BASELINE), LINE, font=f, fontsize=EM)
    tw.write_text(pg, color=(0.07, 0.07, 0.09))
    pm = pg.get_pixmap(matrix=fitz.Matrix(2.0, 2.0), alpha=False)
    rgb = np.frombuffer(pm.samples, np.uint8).reshape(
        pm.height, pm.width, pm.n)[..., :3].copy()
    if degraded:
        rgb = D.hard_degrade(rgb.astype(np.float32), np.array([26, 26, 40], np.float32),
                             np.array([248, 247, 244], np.float32), 0.40,
                             np.random.RandomState(2))
    import PIL.Image
    buf = io.BytesIO(); PIL.Image.fromarray(rgb).save(buf, "PNG")
    out = fitz.open(); page = out.new_page(width=Wpt, height=Hpt)
    page.insert_image(page.rect, stream=buf.getvalue())
    out.save(path); out.close(); doc.close()
    pad = 0.10 * EM
    cover = (MARGIN - pad, BASELINE - 0.80 * EM,
             MARGIN + f.text_length(LINE, EM) + pad, BASELINE + 0.26 * EM,
             248 / 255, 247 / 255, 244 / 255)
    return cover, (MARGIN, BASELINE)


def _speckle_frac(region_i16):
    """Fraction of dark pixels that are ISOLATED (no dark 4-neighbour). Hard toner
    damage scatters stray ink + breaks strokes -> speckle; clean antialiased text
    keeps dark pixels inside solid strokes -> ~0. So this rises with degradation."""
    g = region_i16.mean(2)
    dm = g < 110
    nb = np.zeros_like(dm)
    nb[1:, :] |= dm[:-1, :]; nb[:-1, :] |= dm[1:, :]
    nb[:, 1:] |= dm[:, :-1]; nb[:, :-1] |= dm[:, 1:]
    iso = dm & ~nb
    return float(iso.sum()) / max(int(dm.sum()), 1)


def _inked_cols(region_i16):
    """(min, max) inked column in the region, or None."""
    dark = region_i16.mean(2) < 130
    cols = np.where(dark.any(0))[0]
    return (int(cols.min()), int(cols.max())) if len(cols) else None


PARA = ["First account line", "Second middle row", "Third closing line"]


def _scanned_paragraph_pdf(path):
    """A one-page PDF whose page IS a clean scan of 3 stacked lines. Returns
    (union_cover, line_covers, origin) -- the real per-line geometry a paragraph
    OCR box carries, so the unified per-line engine can be exercised."""
    f = fitz.Font(fontfile=SERIF)
    lead = 1.7 * EM
    widest = max(f.text_length(t, EM) for t in PARA)
    Wpt = MARGIN + widest + MARGIN
    Hpt = MARGIN + lead * len(PARA) + MARGIN
    doc = fitz.open(); pg = doc.new_page(width=Wpt, height=Hpt)
    tw = fitz.TextWriter(pg.rect)
    baselines = []
    y = MARGIN + EM
    for t in PARA:
        tw.append((MARGIN, y), t, font=f, fontsize=EM)
        baselines.append(y); y += lead
    tw.write_text(pg, color=(0.07, 0.07, 0.09))
    pm = pg.get_pixmap(matrix=fitz.Matrix(2.0, 2.0), alpha=False)
    rgb = np.frombuffer(pm.samples, np.uint8).reshape(
        pm.height, pm.width, pm.n)[..., :3].copy()
    import PIL.Image
    buf = io.BytesIO(); PIL.Image.fromarray(rgb).save(buf, "PNG")
    out = fitz.open(); page = out.new_page(width=Wpt, height=Hpt)
    page.insert_image(page.rect, stream=buf.getvalue())
    out.save(path); out.close(); doc.close()
    pad = 0.12 * EM; paper = (248 / 255, 247 / 255, 244 / 255)
    line_covers = []
    for t, bl in zip(PARA, baselines):
        lw = f.text_length(t, EM)
        line_covers.append((MARGIN - pad, bl - 0.80 * EM,
                            MARGIN + lw + pad, bl + 0.26 * EM) + paper)
    ux0 = min(c[0] for c in line_covers); uy0 = min(c[1] for c in line_covers)
    ux1 = max(c[2] for c in line_covers); uy1 = max(c[3] for c in line_covers)
    return (ux0, uy0, ux1, uy1) + paper, tuple(line_covers), (MARGIN, baselines[0])


def test_paragraph_unifies_on_single_line_engine():
    """The box unification: a PARAGRAPH (multi-line OCR box) edits on the SAME
    single-line in-place engine, ONE line at a time. Editing the middle line keeps
    every UNTOUCHED line byte-identical scan pixels and re-synthesizes only the
    changed line; the edit bakes + saves + reopens stably."""
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "para.pdf")
        union, line_covers, origin = _scanned_paragraph_pdf(path)
        doc = PDFDocument(path)
        base = _px(doc.render(0, 2.0))
        box = doc.add_box(0, origin, "\n".join(PARA), "Tinos", EM, (0.0, 0.0, 0.0),
                          False, False, cover=union, render_mode=3,
                          box_w=(union[2] - union[0]), leading=1.7 * EM,
                          line_covers=line_covers)
        assert box.is_paragraph and len(box.line_covers) == 3
        # unedited overlay: the page is still pixel-identical to the scan
        inv = _px(doc.render_with_edits(0, 2.0))
        assert int(np.abs(inv - base).max()) == 0, \
            "unedited paragraph overlay must match the scan exactly"
        print("  ok  paragraph overlay renders pixel-identical to the scan")

        # edit ONLY the middle line
        new = "First account line\nSecond MIDDLE row\nThird closing line"
        doc.stage_edit(0, box, new)
        eb = doc._new_boxes[box.edit_key]
        assert eb.render_mode == 0 and eb.edit_image and eb.edit_image_rect, \
            "a paragraph edit must build a blend raster"

        # The composite (300 dpi) is the precise place to assert the invariant: each
        # untouched line band is BYTE-IDENTICAL to the scan crop; the edited band is not.
        out = doc.compose_lines_block(box, new)
        assert out is not None, "compose_lines_block must run on a 3-line box"
        canvas, _disp = out
        ppi = 300.0 / 72.0
        prgb = doc.render_page_image(0, 300.0)
        bx0 = int(round(union[0] * ppi)); by0 = int(round(union[1] * ppi))
        bx1 = int(round(union[2] * ppi)); by1 = int(round(union[3] * ppi))
        crop = prgb[by0:by1, bx0:bx1]
        wmin = min(canvas.shape[1], crop.shape[1])
        diffs = []
        for cov in line_covers:
            a0 = max(0, int(round(cov[1] * ppi)) - by0)
            a1 = min(crop.shape[0], int(round(cov[3] * ppi)) - by0)
            diffs.append(float(np.abs(canvas[a0:a1, :wmin].astype(int)
                                      - crop[a0:a1, :wmin].astype(int)).mean()))
        assert diffs[0] == 0.0 and diffs[2] == 0.0, \
            f"untouched paragraph lines must be byte-identical scan pixels (got {diffs})"
        assert diffs[1] > 1.0, f"edited line must re-synthesize (got {diffs})"
        print(f"  ok  paragraph keeps untouched lines 1-for-1 (line diffs "
              f"{[round(x, 2) for x in diffs]})")

        # bake -> save -> reopen must be stable (screen == file)
        edited = _px(doc.render_with_edits(0, 2.0))
        outp = os.path.join(d, "po.pdf"); doc.save_as(outp)
        doc2 = PDFDocument(outp); re = _px(doc2.render(0, 2.0))
        assert re.shape == edited.shape
        assert float(np.abs(re - edited).mean()) < 6.0, \
            "paragraph save and screen disagree"
        print("  ok  paragraph edit save -> reopen stable")
        doc2.close(); doc.close()


def test_clean_stays_clean_and_reflows():
    """The composite keeps a CLEAN line's edit clean (no hard-degrade speckle the
    neighbours lack) and reflows cleanly on a delete -- the two things the user
    called out. A DEGRADED line's edit, by contrast, must pick up the damage."""
    import tempfile

    def run(degraded, new_text):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "scan.pdf")
            cover, origin = _scanned_line_pdf(path, degraded)
            doc = PDFDocument(path)
            box = doc.add_box(0, origin, LINE, "Tinos", EM, (0.0, 0.0, 0.0),
                              False, False, cover=cover, render_mode=3)
            base = _px(doc.render_with_edits(0, 2.0))
            doc.stage_edit(0, box, new_text)
            eb = doc._new_boxes[box.edit_key]
            assert eb.edit_image and eb.edit_image_rect, \
                "single-line scanned edit must build a composite raster"
            edited = _px(doc.render_with_edits(0, 2.0))
            cx0, cy0 = int(cover[0] * 2), int(cover[1] * 2)
            cx1, cy1 = int(cover[2] * 2), int(cover[3] * 2)
            doc.close()
            return base[cy0:cy1, cx0:cx1], edited[cy0:cy1, cx0:cx1]

    # REPLACE one word: clean edit must be far less speckled than the degraded one.
    _, clean_reg = run(False, "Total forty owed today")
    _, deg_reg = run(True, "Total forty owed today")
    sc, sd = _speckle_frac(clean_reg), _speckle_frac(deg_reg)
    assert sc < 0.5 * sd, \
        f"clean edit must NOT be hard-degraded like a faxy one (speckle clean {sc:.3f} vs degraded {sd:.3f})"
    assert sc < 0.04, f"clean edit should be near speckle-free (got {sc:.3f})"
    print(f"  ok  clean edit stays clean (speckle {sc:.3f} << degraded {sd:.3f})")

    # DELETE a word: it is ERASED in place (the scan stays the canvas), so the
    # line loses that word's ink while the rest stays the original pixels.
    base_reg, del_reg = run(False, "Total owed today")
    d_base = int((base_reg.mean(2) < 130).sum())
    d_del = int((del_reg.mean(2) < 130).sum())
    assert d_del < d_base * 0.92, \
        f"deleting a word must remove its ink ({d_base} -> {d_del} dark px)"
    print(f"  ok  delete removes the word's ink ({d_base} -> {d_del} dark px)")


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
    test_clean_stays_clean_and_reflows()
    test_paragraph_unifies_on_single_line_engine()
    print("\n7 ocr-blend tests passed.")


if __name__ == "__main__":
    main()
