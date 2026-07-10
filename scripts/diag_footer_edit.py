"""Why does editing the footer black-out 'Association'? Compose the real edit, save the
tile, and overlay the char-box map so we can see where the cover/erase goes wrong."""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import numpy as np  # noqa: E402
import cv2  # noqa: E402
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


def main():
    doc = PDFDocument(PDF)
    rgb = doc.render_page_image(PG, 300.0)
    res = R.reconstruct_page(rgb, 300.0, E.get_engine("auto").recognize(rgb), TINOS, ARIMO)
    if res.otf_bytes:
        FontEngine.register_custom_face(res.family_name, res.otf_bytes)
    lb = next(l for l in res.lines if "20-3001" in l.text or l.text.strip().startswith("©"))
    o, d = doc.ocr_text_placement(PG, lb.origin)
    cx = doc.ocr_cover_rect(PG, lb.cover)
    lcs = tuple(tuple(doc.ocr_cover_rect(PG, lc[:4])) + tuple(lc[4:7])
                for lc in (lb.line_covers or ()))
    cover = tuple(cx) + tuple(lb.bg)
    text = lb.text
    if os.environ.get("APP_BUILD"):       # mirror main_window OCR-overlay: tight cover + respace
        _nl = len([s for s in lb.text.split("\n") if s.strip()]) or 1
        cover = doc._tight_cover(PG, cover, rgb, nlines=_nl)
        text = doc.respace_ocr_text(PG, cover, lcs, lb.text, lb.family or res.family_name,
                                    lb.size, d, page_rgb=rgb)
        print(f"[APP_BUILD] tight cover h={cover[3]-cover[1]:.1f}  respaced={text!r}")
    box = doc.add_box(PG, o, text, lb.family or res.family_name, lb.size, (0, 0, 0),
                      False, False, direction=d, cover=cover,
                      render_mode=3, box_w=lb.box_w, leading=lb.leading, line_covers=lcs)
    ctx = doc.scan_edit_context(box, box.text)
    ot = ctx["orig_text"]
    print(f"OCR text ({len(ot)}): {ot!r}")
    geom = ctx.get("geom") or {}
    cb = geom.get("char_boxes")
    print(f"char_boxes: {len(cb) if cb else None}  vs text len {len(ot)}  match={cb is not None and len(cb)==len(ot)}")

    # the edit the user made: change the year digit (single char near the start) so the
    # whole 'American Heart Association...' suffix has to reflow as kept scan pixels.
    import sys as _s
    nt = _s.argv[1] if len(_s.argv) > 1 else (ot[:5] + "4" + ot[6:])  # 2026 -> 2024
    p = 0
    while p < min(len(ot), len(nt)) and ot[p] == nt[p]:
        p += 1
    s = 0
    while s < min(len(ot), len(nt)) - p and ot[-1 - s] == nt[-1 - s]:
        s += 1
    print(f"new ({len(nt)}): {nt!r}")
    print(f"diff: prefix p={p} ({ot[:p]!r})  suffix s={s} ({ot[len(ot)-s:]!r})  changed_orig={ot[p:len(ot)-s]!r} changed_new={nt[p:len(nt)-s]!r}")

    tile, disp = doc.inplace_compose(dict(ctx), nt)
    if tile is None:
        print("tile None"); return
    cv2.imwrite("/tmp/footer_edit_tile.png", cv2.cvtColor(tile, cv2.COLOR_RGB2BGR))
    print(f"tile {tile.shape} saved /tmp/footer_edit_tile.png")

    # find any near-black horizontal run in the tile (the redaction bar)
    g = tile.mean(2)
    dark_cols = (g < 60).mean(0)            # fraction of column that is near-black
    runs = []
    i = 0
    while i < len(dark_cols):
        if dark_cols[i] > 0.4:
            j = i
            while j < len(dark_cols) and dark_cols[j] > 0.4:
                j += 1
            runs.append((i, j, float(dark_cols[i:j].mean())))
            i = j
        else:
            i += 1
    print(f"near-black column runs (x0,x1,density): {[(a,b,round(c,2)) for a,b,c in runs if b-a>=8]}")

    # overlay char_boxes (text-region px) on the original scan region for comparison
    region = ctx["region"]
    over = region.copy()
    if cb:
        for i, b in enumerate(cb):
            x0, y0, x1, y1 = [int(v) for v in b]
            col = (0, 0, 230) if ot[i] != " " else (0, 200, 0)
            cv2.rectangle(over, (x0, y0), (x1, y1), col, 1)
    z = cv2.resize(over, None, fx=2, fy=2, interpolation=cv2.INTER_NEAREST)
    cv2.imwrite("/tmp/footer_charboxes.png", cv2.cvtColor(z, cv2.COLOR_RGB2BGR))
    print("char_box overlay saved /tmp/footer_charboxes.png")
    doc.close()


if __name__ == "__main__":
    main()
