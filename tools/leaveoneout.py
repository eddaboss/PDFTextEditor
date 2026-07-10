#!/usr/bin/env python3
"""GENERAL leave-one-real-glyph-out gate for the edit degradation.

For ANY scanned page: OCR it through the real pipeline, then for each recognized
glyph, REGENERATE that same character through the actual edit pipeline (_synth_strip,
calibrated to the line's own neighbours) and score the synth glyph against the REAL
scanned glyph it replaces. No tuning to any one file: the metrics are ratios of
synth-to-real, so a degradation that generalizes scores ~1.0 across every font and
scanner; doc05154 is just one sample.

Metrics per glyph (synth vs real, both tight-cropped to ink):
  fill   ink density in the glyph box  -> STROKE WEIGHT (boldness). ratio >1 = too bold
  dark   mean ink strength             -> too-dark vs faded
  rough  edge pixels / ink             -> EDGE BREAKUP. real fax edges are raggeder
  ssim   structural similarity (synth resized to real)

    python tools/leaveoneout.py <pdf-or-image> [page] [tag]
Writes a metrics summary + a real|synth montage to ~/Desktop/ocr_demos/.
"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.append(os.path.expanduser("~/Documents/GitHub/PDFTextEditor-ocr"))

import numpy as np
import cv2
from PySide6.QtWidgets import QApplication
_APP = QApplication.instance() or QApplication([])

from pdftexteditor.document import PDFDocument
from pdftexteditor.font_engine import FontEngine
from pdftexteditor.ocr import recognize_and_reconstruct, degrade

try:
    from skimage.metrics import structural_similarity as _ssim
except Exception:
    _ssim = None

OUT = os.path.expanduser("~/Desktop/ocr_demos")
DPI = 300.0


def apply_ocr(doc, pi, res):
    """Register matched faces + add one invisible OCR box per line (mirrors the app)."""
    if res.otf_bytes:
        FontEngine.register_custom_face(res.family_name, res.otf_bytes)
    for lb in res.lines:
        fam = getattr(lb, "family", "") or res.family_name
        if getattr(lb, "otf_bytes", b""):
            FontEngine.register_custom_face(fam, lb.otf_bytes)
    prgb = doc.render_page_image(pi, DPI)
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
    if getattr(res, "font_map", None) is not None:
        doc.__dict__.setdefault("_pfm_cache", {})[pi] = res.font_map


def tight(cell):
    """Tight-crop an RGB cell to its ink bbox; return (crop, coverage_map) or None."""
    cov = degrade.coverage(cell)
    ys, xs = np.where(cov > 0.40)
    if ys.size < 12:
        return None
    y0, y1, x0, x1 = ys.min(), ys.max() + 1, xs.min(), xs.max() + 1
    return cell[y0:y1, x0:x1], cov[y0:y1, x0:x1]


def metrics(realc, synthc):
    rc = tight(realc); sc = tight(synthc)
    if rc is None or sc is None:
        return None
    (rcrop, rcov), (scrop, scov) = rc, sc
    rink, sink = rcov > 0.45, scov > 0.45
    if rink.sum() < 12 or sink.sum() < 12:
        return None
    # reject contaminated pairs so the gate measures degradation, not OCR mis-seg:
    #  - aspect mismatch => the real crop caught >1 char or is misaligned with the synth
    #  - near-solid fill => a degenerate render (the "black bar" cases on receipts)
    ar = rcrop.shape[1] / max(1, rcrop.shape[0])
    as_ = scrop.shape[1] / max(1, scrop.shape[0])
    if max(ar, as_) / max(min(ar, as_), 1e-3) > 1.7:
        return None
    if rink.mean() > 0.82 or sink.mean() > 0.82:
        return None
    fill_r = rink.mean(); fill_s = sink.mean()
    dark_r = float(rcov[rink].mean()); dark_s = float(scov[sink].mean())
    def rough(ink):
        er = cv2.erode(ink.astype(np.uint8), np.ones((3, 3), np.uint8))
        edge = ink & (er == 0)
        return edge.sum() / max(1, ink.sum())
    rough_r, rough_s = rough(rink), rough(sink)
    # interior GRAIN: high-frequency energy of the coverage inside the stroke. Per-pixel
    # salt-and-pepper reads as HIGH grain; real clumped toner reads LOWER. Synth resized to
    # the real cell first so grain is compared at the same resolution.
    def grain(cov, ink):
        er = cv2.erode(ink.astype(np.uint8), np.ones((3, 3), np.uint8)) > 0
        reg = er if er.sum() >= 8 else ink
        lap = cv2.Laplacian(cov.astype(np.float32), cv2.CV_32F, ksize=1)
        return float(np.std(lap[reg])) if reg.any() else 0.0
    sgcov = cv2.resize(scov, (rcov.shape[1], rcov.shape[0]), interpolation=cv2.INTER_AREA)
    grain_r, grain_s = grain(rcov, rink), grain(sgcov, sgcov > 0.45)
    ss = None
    if _ssim is not None:
        sg = cv2.resize((scov * 255).astype(np.uint8), (rcov.shape[1], rcov.shape[0]))
        rg = (rcov * 255).astype(np.uint8)
        if min(rg.shape) >= 7:
            ss = float(_ssim(rg, sg))
    return {"fill_ratio": fill_s / max(fill_r, 1e-3),
            "dark_ratio": dark_s / max(dark_r, 1e-3),
            "rough_ratio": rough_s / max(rough_r, 1e-3),
            "grain_ratio": grain_s / max(grain_r, 1e-3),
            "ssim": ss}


def load_doc(path):
    """A PDFDocument for a PDF, or for a bare scanned image wrapped as a 1-page PDF."""
    import fitz
    if path.lower().endswith((".pdf",)):
        return PDFDocument(path)
    pix = fitz.Pixmap(path)
    pdf = fitz.open()
    pg = pdf.new_page(width=pix.width * 72.0 / DPI, height=pix.height * 72.0 / DPI)
    pg.insert_image(pg.rect, filename=path)
    tmp = os.path.join("/tmp", "loo_" + os.path.basename(path) + ".pdf")
    pdf.save(tmp)
    return PDFDocument(tmp)


def run(path, pi, tag, max_glyphs=120, emit=True):
    doc = load_doc(path)
    doc.normalize_orientations()
    rgb = doc.render_page_image(pi, DPI)
    res = recognize_and_reconstruct(rgb, DPI, "", "", "auto", tag)
    if res is None:
        print(f"{tag}: OCR found nothing"); return []
    apply_ocr(doc, pi, res)
    rows, crows, pairs = [], [], []
    for b in doc.new_boxes(pi):
        try:
            ctx = doc.scan_edit_context(b, b.text)
        except Exception:
            ctx = None
        if not ctx or ctx.get("dmg") is None:
            continue                                   # clean line: no degradation to match
        region = ctx["region"]
        for g in (getattr(ctx.get("seg"), "glyphs", None) or []):
            c = (g.char or "").strip()
            if not c or len(rows) >= max_glyphs:
                continue
            bh, bw = g.bitmap.shape[:2]
            gx0, gy0 = int(round(g.x0)), int(round(g.top_y))
            realc = region[max(0, gy0):gy0 + bh, max(0, gx0):gx0 + bw]
            if realc.shape[0] < 8 or realc.shape[1] < 5:
                continue
            try:
                sm = doc._synth_metrics(ctx)               # scan-calibrated sizing (sem + condense)
                if not sm:
                    continue
                synth_font, lfb, synth_fpath, em, sem, cond = sm
                base = (False, False, None)
                cb2 = (ctx.get("geom") or {}).get("char_boxes")
                paper_u8 = np.clip(ctx["paper"], 0, 255).astype(np.uint8)
                Hr, Wr, ppi = ctx["Hr"], ctx["Wr"], ctx["ppi"]
                cbase = ctx.get("char_baseline")
                lby = float(np.median(cbase)) if cbase else None
                hg = bool(cb2) and len(cb2) == len(b.text)
                # THE APP'S PATH: _render_run_strip renders the synth AND seats it to the line's
                # scanned glyphs (band-seat) + condenses -- the exact sizing the app ships. Calling
                # _synth_strip directly, as before, skipped that and came out oversized.
                args = (c, [base], base, sem, lfb, synth_fpath, cond, lby, synth_font, ppi,
                        region, cb2, b.text, paper_u8, hg, Hr, Wr)
                strip, _ = doc._render_run_strip(ctx, *args)
                saved = ctx.get("dmg"); ctx["dmg"] = None; ctx.pop("_lr:" + c, None)
                cstrip, _ = doc._render_run_strip(ctx, *args)               # crisp control
                ctx["dmg"] = saved; ctx.pop("_lr:" + c, None)
            except Exception:
                continue
            if strip is None or cstrip is None:
                continue
            sc, cc = tight(strip), tight(cstrip)
            if sc is None or cc is None:
                continue
            m = metrics(realc, sc[0])
            mc = metrics(realc, cc[0])
            if m is None or mc is None:
                continue
            m["char"] = c
            rows.append(m); crows.append(mc)
            if len(pairs) < 24:
                pairs.append((realc, sc[0], c))            # already seated by _render_run_strip
    doc.close()
    if emit:
        summarize(rows, tag)
        summarize(crows, tag + "  [CRISP control / degradation OFF]")
        if pairs:
            montage(pairs, tag)
    return rows, pairs


def corpus(folder, tag="corpus"):
    """Run the gate over every image/PDF in a folder and aggregate (the general gate)."""
    import glob
    files = sorted(f for f in glob.glob(os.path.join(folder, "*"))
                   if f.lower().endswith((".jpg", ".jpeg", ".png", ".pdf")))
    allrows, allpairs, perfile = [], [], []
    for f in files:
        t = os.path.basename(f).split(".")[0]
        try:
            rows, pairs = run(f, 0, t, emit=False)
        except Exception as e:
            print(f"  {t}: FAILED {type(e).__name__}: {e}"); continue
        n = len(rows)
        allrows += rows
        if len(allpairs) < 30 and pairs:
            allpairs += pairs[:2]
        gd = np.mean([abs(np.log(max(r[k], 1e-3))) for r in rows
                      for k in ("fill_ratio", "rough_ratio") if r.get(k) is not None]) if rows else float("nan")
        perfile.append((t, n, gd))
        print(f"  {t:14} {n:4d} glyphs   gendev {gd:.3f}")
    print("\n--- per-file done; AGGREGATE across corpus ---")
    summarize(allrows, tag)
    if allpairs:
        montage(allpairs, tag)
    return allrows


def summarize(rows, tag):
    if not rows:
        print(f"{tag}: no scorable glyphs"); return
    print(f"\n=== {tag}: {len(rows)} glyphs ===")
    print("  metric        median   |dev|   within15%   (target: median 1.0, |dev| low, within high)")
    for k in ("fill_ratio", "dark_ratio", "rough_ratio", "grain_ratio"):
        v = np.array([r[k] for r in rows if r.get(k) is not None], float)
        if v.size == 0:
            continue
        dev = np.mean(np.abs(np.log(np.clip(v, 1e-3, None))))     # symmetric over/under spread
        within = float(np.mean(np.abs(v - 1.0) <= 0.15))
        print(f"  {k:12} {np.median(v):6.2f}  {dev:6.2f}   {within*100:5.0f}%")
    ss = np.array([r["ssim"] for r in rows if r.get("ssim") is not None], float)
    if ss.size:
        print(f"  {'ssim':12} {np.median(ss):6.2f}  (mean {ss.mean():.2f})")
    # one scalar generalization score: how consistently synth matches real per glyph.
    devs = []
    for r in rows:
        for k in ("fill_ratio", "rough_ratio", "grain_ratio"):
            if r.get(k) is not None:
                devs.append(abs(np.log(max(r[k], 1e-3))))
    if devs:
        print(f"  --> GEN score (mean |log| over fill+rough+grain, lower=more general): {np.mean(devs):.3f}")


def montage(pairs, tag):
    cells = []
    for realc, synthc, c in pairs:
        H = 70
        sc = H / realc.shape[0]              # ONE scale per pair, from the REAL glyph, so the
        def up(im):                          # synth shows at its TRUE size relative to the real
            return cv2.resize(im, (max(1, int(round(im.shape[1] * sc))),
                                   max(1, int(round(im.shape[0] * sc)))),
                              interpolation=cv2.INTER_NEAREST)
        r, s = up(realc), up(synthc)
        w = max(r.shape[1], s.shape[1], 24)
        h = max(r.shape[0], s.shape[0])
        pad = lambda x: np.pad(x, ((0, h - x.shape[0]), (0, w - x.shape[1]), (0, 0)),
                               constant_values=255)
        gap = np.full((4, w, 3), 200, np.uint8)
        cells.append(np.vstack([pad(r), gap, pad(s)]))
    H = max(c.shape[0] for c in cells)
    cells = [np.pad(c, ((0, H - c.shape[0]), (2, 2), (0, 0)), constant_values=235) for c in cells]
    rowimg = np.hstack(cells)
    bar = np.full((20, rowimg.shape[1], 3), 245, np.uint8)
    cv2.putText(bar, f"{tag}  top row REAL / bottom row SYNTH (actual pipeline)",
                (5, 14), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (130, 60, 0), 1, cv2.LINE_AA)
    os.makedirs(OUT, exist_ok=True)
    out = os.path.join(OUT, f"loo_{tag}.png")
    cv2.imwrite(out, cv2.cvtColor(np.vstack([bar, rowimg]), cv2.COLOR_RGB2BGR))
    print(f"  montage -> {out}")


if __name__ == "__main__":
    path = sys.argv[1]
    if os.path.isdir(path):
        corpus(path, sys.argv[2] if len(sys.argv) > 2 else "corpus")
    else:
        pi = int(sys.argv[2]) if len(sys.argv) > 2 else 0
        tag = sys.argv[3] if len(sys.argv) > 3 else os.path.basename(path).split(".")[0]
        run(path, pi, tag)
