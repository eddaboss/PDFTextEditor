"""Segmentation-map quality harness (regression gate for _scan_geometry / _fit_boxes).

Runs the REAL map (measure_box_glyphs) over every reconstructed line on every page and
scores how plausible the per-character boxes are -- WITHOUT labels -- so a change to the
map can be proven better on the bad fields and equal-or-better everywhere else.

Per line metrics:
  degen   = # chars whose box width is degenerate (<3px, or >2.6x the line's median width)
  corr    = Pearson corr of per-char box widths vs the matched font's advance widths
            (1.0 = box widths track the font's real letter widths; low/neg = mis-split)
Lower `degen` and higher `corr` are better.

Usage:
  python tools/map_quality.py            # write baseline -> scratchpad/map_baseline.json
  python tools/map_quality.py compare    # compare current run vs the saved baseline
"""
import json
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import numpy as np  # noqa: E402
import fitz  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

_app = QApplication.instance() or QApplication([])

from pdftexteditor.document import PDFDocument  # noqa: E402
from pdftexteditor.ocr import engine as E, reconstruct as R  # noqa: E402
from pdftexteditor.font_engine import FontEngine  # noqa: E402

PDF = os.path.expanduser("~/Downloads/doc05154920260624150538.pdf")
FONTS = os.path.join(ROOT, "pdftexteditor", "assets", "fonts")
TINOS = os.path.join(FONTS, "Tinos-Regular.ttf")
ARIMO = os.path.join(FONTS, "Arimo[wght].ttf")
BASELINE = ("/private/tmp/claude-501/-Users-edward-Documents/"
            "be3020c1-d9b2-4cb5-8439-1919762b0f04/scratchpad/map_baseline.json")


def _line_covers_textspace(doc, lb, pg):
    if not getattr(lb, "line_covers", None):
        return ()
    return tuple(tuple(doc.ocr_cover_rect(pg, lc[:4])) + tuple(lc[4:7])
                for lc in lb.line_covers)


def _score_line(text, char_boxes, fpath):
    """Return (degen_count, corr, widths) for one measured line."""
    idx = [i for i, c in enumerate(text) if not c.isspace()]
    widths = [float(char_boxes[i][2] - char_boxes[i][0]) for i in idx]
    if len(widths) < 2:
        return 0, 1.0, widths
    med = float(np.median(widths)) or 1.0
    degen = sum(1 for w in widths if w < 3.0 or w > 2.6 * med)
    corr = 0.0
    try:
        f = fitz.Font(fontfile=fpath) if fpath else None
        if f is not None:
            exp = [float(f.text_length(text[i], 100.0)) for i in idx]
            if np.std(widths) > 1e-6 and np.std(exp) > 1e-6:
                corr = float(np.corrcoef(widths, exp)[0, 1])
    except Exception:
        pass
    return degen, corr, widths


def run():
    doc = PDFDocument(PDF)
    results = {}
    for pg in range(doc.page_count):
        rgb = doc.render_page_image(pg, 300.0)
        res = R.reconstruct_page(rgb, 300.0, E.get_engine("auto").recognize(rgb),
                                 TINOS, ARIMO)
        if res is None:
            continue
        if res.otf_bytes:
            FontEngine.register_custom_face(res.family_name, res.otf_bytes)
        for lb in res.lines:
            t = (lb.text or "").strip()
            if not t or not any(c.isalnum() for c in t):
                continue
            try:
                origin, direction = doc.ocr_text_placement(pg, lb.origin)
                cx = doc.ocr_cover_rect(pg, lb.cover)
                box = doc.add_box(pg, origin, lb.text,
                                  lb.family or res.family_name, lb.size,
                                  (0, 0, 0), False, False, direction=direction,
                                  cover=tuple(cx) + tuple(lb.bg), render_mode=3,
                                  box_w=lb.box_w, leading=lb.leading,
                                  line_covers=_line_covers_textspace(doc, lb, pg))
                meas = doc.measure_box_glyphs(box)
                fpath = doc._edit_font_file(box.font_family)
            except Exception as e:
                continue
            if not meas:
                continue
            for ml in meas.get("lines") or []:
                if not ml:
                    continue
                lt = ml["text"]
                cb = (ml["ctx"].get("geom") or {}).get("char_boxes")
                if not cb or len(cb) != len(lt) or not lt.strip():
                    continue
                degen, corr, widths = _score_line(lt, cb, fpath)
                results[f"p{pg}:{lt}"] = {
                    "text": lt, "degen": degen, "corr": round(corr, 3),
                    "widths": [round(w, 1) for w in widths]}
    doc.close()
    return results


def summarize(results, tag):
    n = len(results)
    tot_degen = sum(r["degen"] for r in results.values())
    corrs = [r["corr"] for r in results.values()]
    bad = sorted((r for r in results.values() if r["degen"] > 0 or r["corr"] < 0.4),
                 key=lambda r: (r["corr"], -r["degen"]))
    print(f"[{tag}] {n} lines | total degenerate boxes={tot_degen} | "
          f"mean corr={np.mean(corrs):.3f} | lines with issues={len(bad)}")
    for r in bad[:18]:
        print(f"    degen={r['degen']} corr={r['corr']:+.2f}  {r['text']!r}")
    return {"n": n, "tot_degen": tot_degen, "mean_corr": float(np.mean(corrs))}


def main():
    cur = run()
    if len(sys.argv) > 1 and sys.argv[1] == "compare" and os.path.exists(BASELINE):
        base = json.load(open(BASELINE))
        print("=== BASELINE (saved map) ===")
        bs = summarize(base, "baseline")
        print("\n=== CURRENT (with changes) ===")
        cs = summarize(cur, "current")
        print("\n=== PER-LINE REGRESSIONS (worse than baseline) ===")
        any_reg = False
        for k, r in cur.items():
            b = base.get(k)
            if not b:
                continue
            if r["degen"] > b["degen"] or r["corr"] < b["corr"] - 0.05:
                any_reg = True
                print(f"    WORSE {r['text']!r}: degen {b['degen']}->{r['degen']} "
                      f"corr {b['corr']:+.2f}->{r['corr']:+.2f}")
        if not any_reg:
            print("    none -- no line regressed")
        print(f"\nNET: degen {bs['tot_degen']}->{cs['tot_degen']}, "
              f"corr {bs['mean_corr']:.3f}->{cs['mean_corr']:.3f}")
    else:
        json.dump(cur, open(BASELINE, "w"), indent=0)
        print(f"saved baseline -> {BASELINE}")
        summarize(cur, "baseline")


if __name__ == "__main__":
    main()
