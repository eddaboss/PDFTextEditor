#!/usr/bin/env python3
"""Baseline + regression eval for the scanned-glyph font matcher.

Measures the EXACT-match question Edward cares about ("close is not enough"):
render a mixed letters+digits line in a known bank font, degrade it like a real
scan, run the live matcher (``pdftexteditor.ocr.fontbank.identify``), and report:

  * exact@1   -- top-1 IS the rendered font (the real target)
  * recall@5/@10 -- the true font is in the top-5 / top-10 after fine re-rank
  * coarse rank -- where the true font sits in the COARSE shortlist (recall gate:
    the fine stage cannot recover a font the coarse funnel dropped)
  * digit shape sim -- fine-descriptor cosine between the TRUE font's digits and
    the CHOSEN font's digits (how wrong the numbers look when the match is off)

Runs the REAL v030 runtime matcher with its shipped banks; pulls the synthetic
degradation from the scan_degrade research tree. No training, deterministic.

    python tools/eval_fontmatch.py [-n 200] [--seed 7] [--sheet]
"""
from __future__ import annotations

import argparse
import os
import random
import sys
import time

import numpy as np

_V030 = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_OCR = os.path.expanduser("~/Documents/GitHub/PDFTextEditor-ocr")
sys.path.insert(0, _V030)               # canonical pdftexteditor (the live matcher)
sys.path.append(_OCR)                   # scan_degrade degradation helpers

import fitz  # noqa: E402
from pdftexteditor.ocr import fontbank as FB  # noqa: E402
from scan_degrade import letterfilter as LF   # noqa: E402

# A line with broad letter coverage AND many digits (the weak spot).
EVAL_TEXT = "The quick brown fox jumps 0123456789 over 42 lazy dogs 5,678.90 AB"
DIGITS = "0123456789"
_INK = np.array([22, 22, 25], np.float32)
_PAPER = np.array([245, 244, 241], np.float32)
OUT_DIR = os.path.expanduser("~/Desktop/ocr_demos")


def render_clean_line(font_path: str, text: str, em: float, pad: float = 0.5):
    """Render ``text`` in ``font_path`` (per-char placement so boxes are exact),
    return (clean_rgb uint8, cells {i: (char,(x0,y0,x1,y1))}, fitz.Font)."""
    f = fitz.Font(fontfile=font_path)
    advs = [f.text_length(ch, em) for ch in text]
    xs, x = [], em * pad
    for a in advs:
        xs.append(x)
        x += a
    W, H = int(x + em * pad), int(em * 2.0)
    doc = fitz.open()
    pg = doc.new_page(width=W, height=H)
    by = em * 1.45
    tw = fitz.TextWriter(pg.rect, color=(0, 0, 0))
    for i, ch in enumerate(text):
        tw.append((xs[i], by), ch, font=f, fontsize=em)
    tw.write_text(pg)
    pm = pg.get_pixmap(alpha=False)
    img = np.frombuffer(pm.samples, np.uint8).reshape(
        pm.height, pm.width, pm.n)[..., :3].copy()
    cells = {}
    for i, ch in enumerate(text):
        if ch.strip():
            cells[i] = (ch, (int(xs[i]), 0, int(xs[i] + advs[i]), H))
    return img, cells, f


def degrade(clean: np.ndarray, R: np.random.RandomState,
            ease: float = 1.0) -> np.ndarray:
    H, W = clean.shape[:2]
    lo = R.uniform(0.06, 0.12) * ease
    hi = lo + R.uniform(0.18, 0.34) * ease
    sev = lo + (hi - lo) * LF.field((H, W), R, cell=55)
    return LF.hard_degrade(clean, _INK, _PAPER, sev, R)


def coarse_rank(scan: np.ndarray, cells: dict, true_idx: int) -> int:
    """0-based rank of the true font in the COARSE shortlist (replicates the coarse
    scoring in identify), so we can see recall before the fine re-rank."""
    fb = FB._load_fingerprints()
    bank = fb["desc"]
    F = bank.shape[0]
    per_char: dict = {}
    for ch, box in cells.values():
        if ch not in FB._CIDX:
            continue
        d = FB._glyph_descriptor(scan, box)
        if d is not None:
            per_char.setdefault(FB._CIDX[ch], []).append(d)
    score = np.zeros(F, np.float32)
    wsum = np.zeros(F, np.float32)
    for cidx, ds in per_char.items():
        q = np.mean(ds, 0)
        q -= q.mean()
        nq = np.linalg.norm(q)
        if nq < 1e-6:
            continue
        q /= nq
        col = bank[:, cidx, :]
        pres = np.any(col != 0, axis=1)
        score += np.where(pres, col @ q, 0.0)
        wsum += pres.astype(np.float32)
    valid = wsum > 0
    ms = np.full(F, -1.0, np.float32)
    ms[valid] = score[valid] / wsum[valid]
    order = np.argsort(-ms)
    hit = np.where(order == true_idx)[0]
    return int(hit[0]) if len(hit) else F


def digit_shape_sim(ttf_dir: str, key_true: str, key_pick: str) -> "float | None":
    """Mean fine-descriptor cosine of the two fonts' DIGIT glyphs (1.0 == identical
    digit shapes). Quantifies how wrong the numbers look when the pick is off."""
    if key_true == key_pick:
        return 1.0
    pa = os.path.join(ttf_dir, key_true)
    pb = os.path.join(ttf_dir, key_pick)
    sims = []
    for d in DIGITS:
        da = FB._cand_char_desc(pa, d)
        db = FB._cand_char_desc(pb, d)
        if da is not None and db is not None:
            sims.append(float(da @ db))
    return float(np.mean(sims)) if sims else None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("-n", type=int, default=200, help="fonts to sample")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--ease", type=float, default=1.0,
                    help="<1 lighter degradation, >1 harsher")
    ap.add_argument("--sheet", action="store_true", help="write example strips")
    args = ap.parse_args()

    fp = FB._load_fingerprints()
    if fp is None:
        print("ERROR: coarse bank not found")
        sys.exit(1)
    keys = fp["paths"]
    idx_of = {k: i for i, k in enumerate(keys)}
    ttf_dir = FB._ensure_ttf_cache()
    if ttf_dir is None:
        print("ERROR: ttf cache missing")
        sys.exit(1)
    fine = FB._load_fine_bank() is not None
    print(f"matcher: {FB.__file__}")
    print(f"bank: {len(keys)} fonts | fine-bank: {fine} | edge-bank: "
          f"{FB.edge_bank_available()}")

    rng = random.Random(args.seed)
    sample = rng.sample(keys, min(args.n, len(keys)))
    exact = top5 = top10 = 0
    coarse_in50 = coarse_exact = 0
    coarse_ranks, digit_sims, miss_digit_sims = [], [], []
    fails = 0
    strips = []
    t0 = time.time()
    for k, key in enumerate(sample):
        try:
            R = np.random.RandomState(1000 + k)
            em = rng.choice([26, 28, 30, 34])
            clean, cells, _ = render_clean_line(os.path.join(ttf_dir, key),
                                                EVAL_TEXT, em)
            scan = degrade(clean, R, args.ease)
            res = FB.identify(scan, cells, topk=10)
            tk = [t[0] for t in res["topk"]]
        except Exception as e:
            fails += 1
            if fails <= 5:
                print(f"  fail {key}: {repr(e)[:70]}")
            continue
        if not tk:
            fails += 1
            continue
        is_exact = tk[0] == key
        exact += is_exact
        top5 += key in tk[:5]
        top10 += key in tk[:10]
        cr = coarse_rank(scan, cells, idx_of[key])
        coarse_ranks.append(cr)
        coarse_in50 += cr < 50
        coarse_exact += cr == 0
        ds = digit_shape_sim(ttf_dir, key, tk[0])
        if ds is not None:
            digit_sims.append(ds)
            if not is_exact:
                miss_digit_sims.append(ds)
        if args.sheet and len(strips) < 40:
            strips.append((scan, key, tk[0], is_exact))

    n = len(coarse_ranks)
    if n == 0:
        print("no successful samples")
        return
    print(f"\n=== font-match baseline: {n} fonts ({fails} failed) "
          f"{time.time()-t0:.0f}s ===")
    print(f"exact@1 FINE : {exact/n*100:5.1f}%   ({exact}/{n})   <- after fine re-rank")
    print(f"exact@1 COARSE:{coarse_exact/n*100:5.1f}%   ({coarse_exact}/{n})   <- "
          f"coarse argmax only (is fine helping or hurting?)")
    print(f"recall@5    : {top5/n*100:5.1f}%")
    print(f"recall@10   : {top10/n*100:5.1f}%")
    print(f"coarse<50   : {coarse_in50/n*100:5.1f}%   (recall gate; true font in "
          f"the fine shortlist)")
    cr = np.array(coarse_ranks)
    print(f"coarse rank : median {np.median(cr):.0f}  p90 {np.percentile(cr,90):.0f}"
          f"  max {cr.max()}")
    if digit_sims:
        print(f"digit shape sim (chosen vs true): mean {np.mean(digit_sims):.3f}")
    if miss_digit_sims:
        print(f"  on MISSES only ({len(miss_digit_sims)}): mean "
              f"{np.mean(miss_digit_sims):.3f}  min {np.min(miss_digit_sims):.3f}"
              f"  (1.0=identical digits; lower=numbers visibly wrong)")

    if args.sheet and strips:
        import cv2
        os.makedirs(OUT_DIR, exist_ok=True)
        rows = []
        for scan, kt, kp, ok in strips:
            bar = np.full((18, scan.shape[1], 3), 245, np.uint8)
            col = (20, 120, 20) if ok else (180, 60, 0)
            cv2.putText(bar, f"{kt} -> {kp} {'OK' if ok else 'MISS'}", (4, 13),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.4, col, 1, cv2.LINE_AA)
            rows.append(np.vstack([bar, scan,
                                   np.full((4, scan.shape[1], 3), 200, np.uint8)]))
        w = max(r.shape[1] for r in rows)
        rows = [np.pad(r, ((0, 0), (0, w - r.shape[1]), (0, 0)),
                       constant_values=255) for r in rows]
        sheet = np.vstack(rows)
        path = os.path.join(OUT_DIR, "fontmatch_baseline.png")
        cv2.imwrite(path, cv2.cvtColor(sheet, cv2.COLOR_RGB2BGR))
        print(f"saved {path}")


if __name__ == "__main__":
    main()
