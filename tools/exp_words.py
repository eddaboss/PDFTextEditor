#!/usr/bin/env python3
"""Prototype: WORD-grouped, super-resolved font matching (Edward's approach).

A word is the atomic same-font unit (bold 'Phone:' and regular '(818)...' on one
line are two fonts, so lines are unsafe; words are safe). Pipeline:
  1. OCR -> words, each with its glyph coverages.
  2. Per-word character-INDEPENDENT style signature (weight, contrast, size);
     cluster words into font groups (so bold/regular/heading/body separate).
  3. Within the biggest group, SUPER-RESOLVE each character: align all instances
     sub-pixel at high resolution and average, so the random fax damage cancels
     and a clean, crisp super-glyph remains.
  4. Match the group's super-glyphs against the FULL bank (no whitelist).

Saves a visual proof (single noisy instance vs super-glyph vs matched font) to
~/Desktop/ocr_demos/ -- isolated single glyphs only (not PHI). Prints font names.

    python tools/exp_words.py "/path/to/scan.pdf" [page]
"""
from __future__ import annotations

import os
import sys

import numpy as np
import cv2
import fitz
from scipy.cluster.hierarchy import linkage, fcluster

_V030 = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _V030)
sys.path.insert(0, os.path.join(_V030, "tools"))
sys.path.append(os.path.expanduser("~/Documents/GitHub/PDFTextEditor-ocr"))

from pdftexteditor.ocr import pack, get_engine          # noqa: E402
pack.ensure_on_path()
from pdftexteditor.ocr import fontbank as FB             # noqa: E402
from pdftexteditor.ocr.segment import segment_line       # noqa: E402
from validate_real import auto_orient, fontname          # noqa: E402
import matcher_v2 as V2                                   # noqa: E402

OUT = os.path.expanduser("~/Desktop/ocr_demos")
H = 128          # super-res canvas
_PAD = 12


def extract_words(rgb, lines):
    """[(glyphs)] where each word is [(char, (x0,y0,x1,y1), cov), ...]."""
    HH, WW = rgb.shape[:2]
    words = []
    for ln in lines:
        x0, y0, x1, y1 = ln.bbox
        x0i, y0i = max(0, int(x0)), max(0, int(y0))
        x1i, y1i = min(WW, int(x1) + 1), min(HH, int(y1) + 1)
        if x1i - x0i < 4 or y1i - y0i < 4:
            continue
        strip = rgb[y0i:y1i, x0i:x1i]
        try:
            seg = segment_line(strip, ln.text)
        except Exception:
            seg = None
        if seg is None or not seg.glyphs:
            continue
        gl = []
        for g in seg.glyphs:
            if not g.char.strip() or g.bitmap.size == 0:
                continue
            bw, bh = g.bitmap.shape[1], g.bitmap.shape[0]
            gx0 = x0i + int(g.x0)
            gy0 = y0i + int(g.top_y)
            box = (gx0, gy0, gx0 + bw, gy0 + bh)
            cov = FB._to_alpha(rgb[box[1]:box[3], box[0]:box[2]])
            gl.append((g.char, box, cov))
        # split the line's glyphs into words by the seg word x-ranges
        for w in (seg.words or []):
            wx0, wx1 = x0i + int(w.x0), x0i + int(w.x1)
            members = [it for it in gl
                       if wx0 - 2 <= (it[1][0] + it[1][2]) / 2 <= wx1 + 2]
            if members:
                words.append(members)
        if not seg.words and gl:
            words.append(gl)
    return words


def glyph_style(cov):
    bw = (cov > 0.4).astype(np.uint8)
    ys, xs = np.where(bw)
    if len(ys) < 6:
        return None
    h = ys.max() - ys.min() + 1
    dt = cv2.distanceTransform(bw, cv2.DIST_L2, 3)
    dv = dt[bw > 0]
    if dv.size < 4:
        return None
    mean = max(float(dv.mean()), 1e-3)
    stroke = 2.0 * float(np.percentile(dv, 75))
    return np.array([stroke / max(h, 1), float(dv.std()) / mean, float(h)], np.float32)


def word_sig(word):
    feats = [glyph_style(c) for _ch, _b, c in word]
    feats = [f for f in feats if f is not None]
    return np.median(feats, 0) if feats else None


def hires_tile(cov, thr=0.2):
    ys, xs = np.where(cov > thr)
    if len(ys) < 6:
        return None
    crop = cov[ys.min():ys.max() + 1, xs.min():xs.max() + 1]
    h, w = crop.shape
    sc = (H - 2 * _PAD) / max(h, w)
    nh, nw = max(1, round(h * sc)), max(1, round(w * sc))
    rs = cv2.resize(crop.astype(np.float32), (nw, nh), interpolation=cv2.INTER_CUBIC)
    t = np.zeros((H, H), np.float32)
    oy, ox = (H - nh) // 2, (H - nw) // 2
    t[oy:oy + nh, ox:ox + nw] = np.clip(rs, 0, 1)
    return t


def superres(covs):
    """Sub-pixel align all instances and average so the random fax damage cancels.
    Alignment is done on a LOW-PASSED copy (the halftone speckle wrecks phase
    correlation on the sharp tiles), then the shift is applied to the sharp tile so
    the averaged detail stays crisp. Two passes: align to the median, then re-align
    to the first-pass mean (tightens registration)."""
    tiles = [t for t in (hires_tile(c) for c in covs) if t is not None]
    if len(tiles) < 4:
        return None, len(tiles)
    blur = [cv2.GaussianBlur(t, (0, 0), 2.5) for t in tiles]
    ref = np.median(blur, 0).astype(np.float32)
    for _ in range(2):
        acc = np.zeros((H, H), np.float32)
        accb = np.zeros((H, H), np.float32)
        for t, tb in zip(tiles, blur):
            try:
                (dx, dy), _ = cv2.phaseCorrelate(tb, ref)
            except Exception:
                dx = dy = 0.0
            M = np.float32([[1, 0, -dx], [0, 1, -dy]])
            acc += cv2.warpAffine(t, M, (H, H))
            accb += cv2.warpAffine(tb, M, (H, H))
        ref = (accb / len(tiles)).astype(np.float32)
    return acc / len(tiles), len(tiles)


def hdesc(t):
    def u(v):
        v = v - v.mean()
        nn = np.linalg.norm(v)
        return v / nn if nn > 1e-6 else None
    cb = u(cv2.GaussianBlur(t, (0, 0), 1.0).ravel())
    gx = cv2.Sobel(t, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(t, cv2.CV_32F, 0, 1, ksize=3)
    e = u(np.hypot(gx, gy).ravel())
    if cb is None:
        return None
    v = cb if e is None else np.concatenate([cb, 0.7 * e])
    return v / np.linalg.norm(v)


def render_hires(key, ch):
    f = FB._cand_font(os.path.join(FB._ensure_ttf_cache(), key))
    if f is None or not f.has_glyph(ord(ch)):
        return None
    try:
        return hires_tile(FB._render_glyph_cov(f, ch, em=180.0))
    except Exception:
        return None


def main():
    path = sys.argv[1]
    page = int(sys.argv[2]) if len(sys.argv) > 2 else 0
    d = fitz.open(path)
    pm = d[page].get_pixmap(dpi=300)
    base = np.frombuffer(pm.samples, np.uint8).reshape(pm.height, pm.width, pm.n)[..., :3].copy()
    rgb, lines = auto_orient(base, get_engine("auto"))
    words = extract_words(rgb, lines)
    sigs = [word_sig(w) for w in words]
    keep = [(w, s) for w, s in zip(words, sigs) if s is not None]
    print(f"page {page}: {len(words)} words, {len(keep)} with style signatures")
    if len(keep) < 4:
        print("too few words"); return

    S = np.array([s for _w, s in keep], np.float32)
    Z = (S - S.mean(0)) / (S.std(0) + 1e-6)
    labels = fcluster(linkage(Z, method="ward"), t=1.4, criterion="distance")
    groups = {}
    for (w, _s), lb in zip(keep, labels):
        groups.setdefault(lb, []).extend(w)
    order = sorted(groups, key=lambda g: -len(groups[g]))
    print(f"font groups: {len(order)}  sizes={[len(groups[g]) for g in order[:6]]}")

    keys = FB._load_fingerprints()["paths"]

    def match_group(glyphs):
        """Super-resolve a font group's chars, then match vs the FULL bank by hi-res
        coverage+edge cosine on a coarse shortlist. Returns (scored, supers, size_px)."""
        by_char = {}
        for ch, box, cov in glyphs:
            by_char.setdefault(ch, []).append(cov)
        supers = {}
        for ch, covs in by_char.items():
            sg, n = superres(covs)
            if sg is not None and n >= 3:
                supers[ch] = sg
        sdesc = {ch: hdesc(sg) for ch, sg in supers.items()}
        sdesc = {ch: v for ch, v in sdesc.items() if v is not None}
        if len(sdesc) < 3:
            return None, supers, by_char
        cells = {i: (ch, box) for i, (ch, box, _c) in enumerate(glyphs)}
        shape, _ = V2._coarse_scores(rgb, cells)
        cand = [int(i) for i in np.argsort(-shape)[:40]]
        scored = []
        for ci in cand:
            s, n = 0.0, 0
            for ch, sv in sdesc.items():
                cd = render_hires(keys[ci], ch)
                if cd is not None:
                    hd = hdesc(cd)
                    if hd is not None:
                        s += float(hd @ sv); n += 1
            scored.append((ci, s / n if n else -1.0))
        scored.sort(key=lambda x: -x[1])
        return scored, supers, by_char

    print("\nPER-FONT-GROUP match (full bank, no whitelist):")
    big_scored = big_supers = big_bychar = None
    for gi, g in enumerate(order[:8]):
        glyphs = groups[g]
        if len(glyphs) < 12:
            continue
        sizepx = int(np.median([b[3] - b[1] for _c, b, _cv in glyphs]))
        scored, supers, by_char = match_group(glyphs)
        if scored is None:
            print(f"  group {gi}: {len(glyphs):3d} glyphs  ~{sizepx}px  "
                  f"(too sparse: only {len(supers)} super-chars)")
            continue
        top = ", ".join(f"{fontname(keys[c])} {s:.2f}" for c, s in scored[:3])
        print(f"  group {gi}: {len(glyphs):3d} glyphs  ~{sizepx}px  chars[{''.join(sorted(supers))}]"
              f"\n            -> {top}")
        if big_scored is None:
            big_scored, big_supers, big_bychar = scored, supers, by_char

    if big_scored is None:
        return
    scored, supers, by_char = big_scored, big_supers, big_bychar
    best = keys[scored[0][0]]
    rows = []
    for ch in sorted(supers)[:8]:
        one = hires_tile(by_char[ch][0])
        sg = supers[ch]
        mr = render_hires(best, ch)
        cells_img = []
        for t in (one, sg, mr):
            img = (255 * (1 - (t if t is not None else np.zeros((H, H), np.float32)))).astype(np.uint8)
            cells_img.append(cv2.cvtColor(img, cv2.COLOR_GRAY2BGR))
        rows.append(np.hstack(cells_img))
    if rows:
        os.makedirs(OUT, exist_ok=True)
        sheet = np.vstack([np.pad(r, ((0, 2), (0, 0), (0, 0)), constant_values=180) for r in rows])
        hdr = np.full((22, sheet.shape[1], 3), 245, np.uint8)
        cv2.putText(hdr, f"single | super-res | {fontname(best)}", (4, 16),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (30, 30, 30), 1, cv2.LINE_AA)
        out = os.path.join(OUT, f"superres_p{page}.png")
        cv2.imwrite(out, np.vstack([hdr, sheet]))
        print(f"\nsaved proof -> {out}")


if __name__ == "__main__":
    main()
