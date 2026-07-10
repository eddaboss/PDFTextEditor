#!/usr/bin/env python3
"""Headless end-to-end edit through the app's FULL pipeline with the new super-res
matcher: orient -> OCR page -> apply editable boxes -> find two date boxes -> bump
the year -> save the edited PDF. PHI stays local (this subprocess + the output file);
only counts and the user-supplied dates are printed.

    python tools/edit_dates.py "/path/in.pdf" /path/out.pdf [page]
"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.append(os.path.expanduser("~/Documents/GitHub/PDFTextEditor-ocr"))

from PySide6.QtWidgets import QApplication
_APP = QApplication.instance() or QApplication([])

from pdftexteditor.document import PDFDocument
from pdftexteditor.font_engine import FontEngine
from pdftexteditor.ocr import recognize_and_reconstruct


def apply_ocr(doc, pi, res):
    """Replicate main_window._ocr_apply_page headlessly: register per-box faces and
    add one invisible (render_mode 3) NewBox per area in its matched font."""
    if res.otf_bytes:
        FontEngine.register_custom_face(res.family_name, res.otf_bytes)
    for lb in res.lines:
        fam = getattr(lb, "family", "") or res.family_name
        if getattr(lb, "otf_bytes", b""):
            FontEngine.register_custom_face(fam, lb.otf_bytes)
    prgb = doc.render_page_image(pi, 300.0)
    added = 0
    for lb in res.lines:
        origin, direction = doc.ocr_text_placement(pi, lb.origin)
        cover = ()
        if lb.cover:
            cx0, cy0, cx1, cy1 = doc.ocr_cover_rect(pi, lb.cover)
            cover = (cx0, cy0, cx1, cy1) + tuple(lb.bg)
        line_covers = ()
        if getattr(lb, "line_covers", None):
            lcs = []
            for lc in lb.line_covers:
                lx0, ly0, lx1, ly1 = doc.ocr_cover_rect(pi, lc[:4])
                lcs.append((lx0, ly0, lx1, ly1) + tuple(lc[4:7]))
            line_covers = tuple(lcs)
        if cover:
            nl = len([s for s in (lb.text or "").split("\n") if s.strip()]) or 1
            cover = doc._tight_cover(pi, cover, prgb, nlines=nl)
        fam = getattr(lb, "family", "") or res.family_name
        text = lb.text
        if cover:
            text = doc.respace_ocr_text(pi, cover, line_covers, lb.text, fam,
                                        lb.size, direction, page_rgb=prgb)
        doc.add_box(pi, origin, text, fam, lb.size, (0.0, 0.0, 0.0), False, False,
                    direction=direction, cover=cover, render_mode=3,
                    box_w=lb.box_w, leading=lb.leading, alignment="left",
                    line_covers=line_covers)
        added += 1
    # stash the OCR-time per-glyph font map so the editor resolves font at the caret
    if getattr(res, "font_map", None) is not None:
        cache = getattr(doc, "_pfm_cache", None)
        if cache is None:
            cache = doc._pfm_cache = {}
        cache[pi] = res.font_map
    return added


def fontname(fam):
    try:
        from pdftexteditor.ocr import fontbank
        p = fontbank.font_file_for(fam)
        if p:
            from pdftexteditor.font_engine import detect_ttf_style
            f, b, i = detect_ttf_style(p)
            return f"{f}{' Bold' if b else ''}{' Italic' if i else ''}"
    except Exception:
        pass
    return fam


def main():
    inp, outp = sys.argv[1], sys.argv[2]
    pi = int(sys.argv[3]) if len(sys.argv) > 3 else 2

    doc = PDFDocument(inp)
    doc.normalize_orientations()
    print(f"pages={doc.page_count}  editing page index {pi}")
    rgb = doc.render_page_image(pi, 300.0)
    res = recognize_and_reconstruct(rgb, 300.0, "", "", "auto", f"Scanned p{pi+1}")
    if res is None:
        print("OCR found no text"); return
    n = apply_ocr(doc, pi, res)
    boxes = doc.new_boxes(pi)
    print(f"applied {n} OCR boxes; {len(boxes)} boxes on page")

    def norm(t):
        return t.replace(" ", "").replace("\n", " ").strip()

    EDITS = [("05/22/2025", "05/22/2026"), ("05/22/2026", "05/22/2027")]
    done = []
    for target, repl in EDITS:
        match = None
        for b in boxes:
            if target in norm(b.text):
                # prefer a box that IS essentially just the date
                if match is None or len(norm(b.text)) < len(norm(match.text)):
                    match = b
        if match is None:
            print(f"  NOT FOUND: a box containing {target}")
            continue
        new_text = match.text.replace(target.split("/")[-1], repl.split("/")[-1], 1)
        # what font does the PAGE MAP resolve at this date's caret region?
        synth = "?"
        try:
            pm = doc._page_font_map(pi)
            cov = match.cover[:4] if (match.cover and len(match.cover) >= 4) else match.bbox
            ppi = 300.0 / 72.0
            fr = pm.font_for_rect(cov[0] * ppi, cov[1] * ppi, cov[2] * ppi, cov[3] * ppi) if pm else None
            if fr:
                from pdftexteditor.font_engine import detect_ttf_style
                ff, fb_, fi = detect_ttf_style(fr[0])
                synth = f"{ff}{' Bold' if fb_ else ''}{' Italic' if fi else ''}"
        except Exception as e:
            synth = f"err {e!r}"[:40]
        print(f"  box id={match.box_id} x0={match.bbox[0]:.0f}: {norm(match.text)!r} "
              f"-> {repl}  | box-default={fontname(match.font_family)}  "
              f"| SYNTH(page-map)={synth}")
        doc.stage_edit(pi, match, new_text)
        done.append((target, repl))

    if not done:
        print("no edits staged"); return
    doc.save_as(outp)
    doc.close()
    print(f"\nSAVED edited PDF -> {outp}  ({len(done)} dates changed)")


if __name__ == "__main__":
    main()
