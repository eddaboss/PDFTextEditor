"""Why is the synth digit on the 'From: 05/31/24...' line oversized? Reproduce the edit
(24->25), measure the synth glyph height vs the kept scan digits, and dump the size inputs
(box.size, the line's cap_px/em, the size_factor) to see which one is wrong."""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import numpy as np  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

_app = QApplication.instance() or QApplication([])

from pdftexteditor.document import PDFDocument  # noqa: E402
from pdftexteditor.ocr import engine as E, reconstruct as R  # noqa: E402
from pdftexteditor.font_engine import FontEngine  # noqa: E402

FONTS = os.path.join(ROOT, "pdftexteditor", "assets", "fonts")
TINOS = os.path.join(FONTS, "Tinos-Regular.ttf")
ARIMO = os.path.join(FONTS, "Arimo[wght].ttf")


def measure_line(doc, lb, pg):
    o, d = doc.ocr_text_placement(pg, lb.origin)
    cx = doc.ocr_cover_rect(pg, lb.cover)
    lcs = tuple(tuple(doc.ocr_cover_rect(pg, lc[:4])) + tuple(lc[4:7])
                for lc in (lb.line_covers or ()))
    box = doc.add_box(pg, o, lb.text, lb.family or "Arimo", lb.size, (0, 0, 0), False,
                      False, direction=d, cover=tuple(cx) + tuple(lb.bg), render_mode=3,
                      box_w=lb.box_w, leading=lb.leading, line_covers=lcs)
    meas = doc.measure_box_glyphs(box)
    return box, meas


def glyph_h(tile, edges, i):
    a, b = int(round(edges[i])), int(round(edges[i + 1]))
    a, b = max(0, a), min(tile.shape[1], b)
    if b <= a:
        return 0
    cov = 255.0 - tile[:, a:b].mean(2)
    ys = np.where((cov > 0.30 * 255).any(1))[0]
    return int(ys.max() - ys.min() + 1) if len(ys) else 0


def run(doc, pg, lb, label, line_idx=0):
    box, meas = measure_line(doc, lb, pg)
    lines = (meas or {}).get("lines", [])
    ln = lines[line_idx] if line_idx < len(lines) else None
    if ln is None:
        print(f"[{label}] line {line_idx} not mapped"); return
    ctx = ln["ctx"]
    ot = ctx["orig_text"]
    print(f"\n[{label}] {ot[:48]!r}")
    print(f"   box.size={box.size:.2f}  ctx em={ctx['em']:.2f}  cap_px={ctx['cap_px']:.1f}  ppi={ctx['ppi']:.3f}")
    # the actual scan ink height of THIS line's digit char-boxes (what cap_px should equal)
    geom = ctx.get("geom") or {}
    cb = geom.get("char_boxes")
    if cb and len(cb) == len(ot):
        dh = [b[3] - b[1] for ch, b in zip(ot, cb) if ch.isdigit() and b[3] - b[1] > 2]
        print(f"   digit char-box heights(px): med={int(np.median(dh)) if dh else 0} range={[int(min(dh)),int(max(dh))] if dh else []}")
    idx = next((i for i, c in enumerate(ot) if c.isdigit()), None)
    if idx is None:
        print("   (no digit on this line)"); return
    nt = ot[:idx] + ("5" if ot[idx] != "5" else "8") + ot[idx + 1:]
    c = dict(ctx)
    tile, _ = doc.inplace_compose(c, nt)
    le = c.get("_live_edges")
    if not le or le[0] != nt:
        print("   no live edges"); return
    dx0 = ctx["disp_rect"][0]; ppi = ctx["ppi"]
    edges = [(e - dx0) * ppi for e in le[1]]
    changed = {i for i in range(len(nt)) if i < len(ot) and nt[i] != ot[i]}
    syn_h = [glyph_h(tile, edges, i) for i in changed if nt[i].isdigit()]
    scan_h = [glyph_h(tile, edges, i) for i in range(len(nt))
              if nt[i].isdigit() and i not in changed]
    sh = int(np.median(scan_h)) if scan_h else 0
    print(f"   SYNTH digit h={syn_h}   scan digit median h={sh}   "
          f"oversize={[round(s/max(1,sh),2) for s in syn_h]}")


def main():
    # the broken one
    doc = PDFDocument(os.path.expanduser("~/Downloads/doc05196720260626202216.pdf"))
    rgb = doc.render_page_image(0, 300.0)
    res = R.reconstruct_page(rgb, 300.0, E.get_engine("auto").recognize(rgb), TINOS, ARIMO)
    if res.otf_bytes:
        FontEngine.register_custom_face(res.family_name, res.otf_bytes)
    fromlb = next(l for l in res.lines if l.text.startswith("From:"))
    run(doc, 0, fromlb, "05196 From:")
    doc.close()
    # the known-good one
    doc = PDFDocument(os.path.expanduser("~/Downloads/doc05154920260624150538.pdf"))
    rgb = doc.render_page_image(1, 300.0)
    res = R.reconstruct_page(rgb, 300.0, E.get_engine("auto").recognize(rgb), TINOS, ARIMO)
    if res.otf_bytes:
        FontEngine.register_custom_face(res.family_name, res.otf_bytes)
    datelb = next(l for l in res.lines if "5/16" in l.text and l.text.endswith("2024"))
    run(doc, 1, datelb, "05154 date (known good)", line_idx=1)
    doc.close()


if __name__ == "__main__":
    main()
