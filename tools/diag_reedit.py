#!/usr/bin/env python3
"""Diagnose the re-edit duplication: stage a WIDTH-GROWING in-place edit on a real
OCR box and check whether _edit_exclude_for's `moved` test misfires -- if it reads
an in-place (unmoved) edit as "moved", the committed bake is NOT hidden on re-edit,
so bake + live preview both render = the doubling Edward saw.
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
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools"))
from edit_dates import apply_ocr


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else \
        "/Users/edward/Downloads/doc05154920260624150538.pdf"
    pi = int(sys.argv[2]) if len(sys.argv) > 2 else 2
    doc = PDFDocument(path)
    doc.normalize_orientations()
    rot = doc.working[pi].rotation
    rgb = doc.render_page_image(pi, 300.0)
    res = recognize_and_reconstruct(rgb, 300.0, "", "", "auto", f"p{pi}")
    apply_ocr(doc, pi, res)
    boxes = [b for b in doc.new_boxes(pi) if b.cover and len(b.cover) == 7
             and len(b.text.strip()) >= 3 and "\n" not in b.text
             and not getattr(b, "is_paragraph", False)]
    boxes.sort(key=lambda b: len(b.text))   # shortest single-line fields first
    print(f"page {pi}  rotation={rot}  candidate boxes={len(boxes)}")
    for b in boxes[:4]:
        orig = b.text
        # live-preview disp BEFORE any commit (no edit_image_rect yet -> offset 0)
        _cb = doc.scan_edit_context(b, b.ocr_text or b.text)
        disp_before = tuple(round(float(v), 1) for v in _cb["disp_rect"]) if _cb else None
        # width-growing insertion: drop 'really' into the middle of the text
        mid = len(orig) // 2
        new = orig[:mid] + "really" + orig[mid:]
        doc.stage_edit(pi, b, new)
        nb = next((x for x in doc.new_boxes(pi) if x.box_id == b.box_id), None)
        if nb is None:
            continue
        cov = nb.cover
        eir = getattr(nb, "edit_image_rect", ()) or ()
        has_img = bool(getattr(nb, "edit_image", b""))
        old_moved = (has_img and len(cov) == 7 and len(eir) == 4
                     and (abs(eir[0] - cov[0]) > 2.0 or abs(eir[1] - cov[1]) > 2.0))
        new_moved = False
        if has_img and len(cov) == 7 and len(eir) == 4:
            ox = max(0.0, min(eir[2], cov[2]) - max(eir[0], cov[0]))
            oy = max(0.0, min(eir[3], cov[3]) - max(eir[1], cov[1]))
            ca = max(1e-6, (cov[2] - cov[0]) * (cov[3] - cov[1]))
            new_moved = (ox * oy) / ca < 0.5
        # live-preview disp on RE-EDIT (box now has edit_image_rect). With the fix the
        # offset is 0, so disp_after == disp_before (no shift); the old code shifted it.
        _ca = doc.scan_edit_context(nb, nb.ocr_text or "")
        disp_after = tuple(round(float(v), 1) for v in _ca["disp_rect"]) if _ca else None
        shift = (round(disp_after[0] - disp_before[0], 1)
                 if disp_before and disp_after else None)
        print(f"\n  box {nb.box_id}: {orig!r} -> +really")
        print(f"    NEW moved={new_moved}  (dup {'NOT ' if not new_moved else ''}fixed)")
        print(f"    disp_before={disp_before}  disp_after={disp_after}")
        print(f"    re-edit x-shift = {shift}  {'<<< STILL SHIFTS' if shift not in (0.0, -0.0, None) else '(no shift = FIXED)'}")
    doc.close()


if __name__ == "__main__":
    main()
