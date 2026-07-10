#!/usr/bin/env python3
"""End-to-end gate: does the NEW matcher beat the current one on the real bank?

New pipeline (all validated piecewise):
  1. candidate generation = FUSE coarse shape score + advance-width METRIC score
     over the whole 3,948-font bank (metric is degradation-robust + orthogonal;
     lifts recall from ~73% to ~88% in the probe).
  2. re-rank the top-K by the fine SHAPE descriptor with the harmful _glyph_features
     penalty OFF (shape-only beat full re-rank ~65% vs ~50% within recall).

Compares exact@1 of the new pipeline vs the live FB.identify (current production)
on the SAME degraded queries against the SAME full bank. --jitter adds positional
noise to the advance read to gauge sensitivity to real-OCR segmentation noise.

    python tools/exp_full.py [-n 80] [-k 50] [--jitter 0]
"""
from __future__ import annotations

import argparse
import os
import random
import sys
import time

import numpy as np

_V030 = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _V030)
sys.path.insert(0, os.path.join(_V030, "tools"))
sys.path.append(os.path.expanduser("~/Documents/GitHub/PDFTextEditor-ocr"))

from pdftexteditor.ocr import fontbank as FB             # noqa: E402
from eval_fontmatch import render_clean_line, degrade, EVAL_TEXT  # noqa: E402
from exp_recall import CHARS, scan_char_descs            # noqa: E402
from exp_metric import font_adv_sig                       # noqa: E402

_CIX = [FB._CIDX[c] for c in CHARS]


def coarse_scores_all(scan, cells):
    """Coarse shape score for every bank font (the same math identify uses), plus
    the scan's per-char coarse descriptors."""
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
    ms = np.full(F, -1e9, np.float32)
    ms[valid] = score[valid] / wsum[valid]
    return ms


def scan_adv_sig_jit(scan, cells, jitter, R):
    left = {}
    for i, (ch, box) in cells.items():
        x0, y0, x1, y1 = box
        sub = scan[y0:y1, x0:x1]
        if sub.size == 0:
            continue
        cov = FB._to_alpha(sub)
        nz = np.where((cov > 0.2).sum(0) > 0)[0]
        if len(nz):
            j = int(R.randint(-jitter, jitter + 1)) if jitter else 0
            left[i] = x0 + int(nz[0]) + j
    per: dict = {}
    for i, (ch, _b) in cells.items():
        if i in left and (i + 1) in left:
            a = left[i + 1] - left[i]
            if a > 0:
                per.setdefault(ch, []).append(a)
    a = {c: float(np.median(v)) for c, v in per.items()}
    if len(a) < 6:
        return None
    med = np.median(list(a.values())) or 1.0
    return {c: v / med for c, v in a.items()}


def zscore(a):
    m = a[a > -1e8]
    mu, sd = (m.mean(), m.std()) if m.size else (0.0, 1.0)
    return (a - mu) / (sd or 1.0)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-n", type=int, default=80)
    ap.add_argument("-k", type=int, default=50, help="candidates to re-rank")
    ap.add_argument("--jitter", type=int, default=0, help="advance-read noise (px)")
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()

    fb = FB._load_fingerprints()
    keys = fb["paths"]
    idx_of = {k: i for i, k in enumerate(keys)}
    ttf_dir = FB._ensure_ttf_cache()

    print("building advance signatures for the whole bank...", flush=True)
    t0 = time.time()
    all_adv = {}
    for k in keys:
        all_adv[k] = font_adv_sig(ttf_dir, k)
    print(f"  {len(all_adv)} sigs in {time.time()-t0:.0f}s", flush=True)
    # pack advances into arrays for vectorized metric scoring
    achars = list(CHARS)
    adv_mat = np.full((len(keys), len(achars)), np.nan, np.float32)
    for fi, k in enumerate(keys):
        a = all_adv.get(k)
        if a:
            for ci, ch in enumerate(achars):
                if ch in a:
                    adv_mat[fi, ci] = a[ch]

    rng = random.Random(args.seed)
    test = rng.sample(keys, min(args.n, len(keys)))
    ex_base = ex_new = recall_fused = 0
    n = 0
    t1 = time.time()
    for s, key in enumerate(test):
        R = np.random.RandomState(2000 + s)
        Rj = np.random.RandomState(5000 + s)
        em = rng.choice([26, 28, 30, 34])
        try:
            clean, cells, _ = render_clean_line(os.path.join(ttf_dir, key), EVAL_TEXT, em)
            scan = degrade(clean, R, 1.0)
        except Exception:
            continue
        sq = scan_char_descs(scan, cells)
        sadv = scan_adv_sig_jit(scan, cells, args.jitter, Rj)
        if not sq or sadv is None:
            continue
        n += 1
        true_idx = idx_of[key]
        # baseline (current production)
        base = FB.identify(scan, cells, topk=1)
        ex_base += bool(base["topk"]) and base["topk"][0][0] == key
        # NEW: fused candidate generation
        shape = coarse_scores_all(scan, cells)
        sv = np.array([sadv.get(c, np.nan) for c in achars], np.float32)
        mask = ~np.isnan(sv)
        d = np.abs(adv_mat[:, mask] - sv[mask])
        valid = (~np.isnan(d)).sum(1) >= 6
        metric = np.full(len(keys), -1e9, np.float32)
        metric[valid] = -np.nanmean(d[valid], axis=1)
        fused = zscore(shape) + zscore(metric)
        order = np.argsort(-fused)
        cand = [int(i) for i in order[:args.k]]
        recall_fused += true_idx in cand
        # re-rank with shape-only fine (feature penalty off)
        lam = FB._FEAT_LAMBDA
        FB._FEAT_LAMBDA = 0.0
        rr = FB._refine_bank(scan, cells, cand)
        FB._FEAT_LAMBDA = lam
        new_top = keys[rr[0][0]] if rr else None
        ex_new += new_top == key
        if (n) % 20 == 0:
            print(f"  {n}: base {ex_base/n*100:.0f}%  new {ex_new/n*100:.0f}%  "
                  f"recall@{args.k} {recall_fused/n*100:.0f}%  ({time.time()-t1:.0f}s)",
                  flush=True)

    print(f"\n=== END-TO-END on full {len(keys)}-font bank: {n} queries, K={args.k}, "
          f"jitter={args.jitter}px ({time.time()-t1:.0f}s) ===")
    print(f"exact@1 BASELINE (current identify): {ex_base/n*100:5.1f}%")
    print(f"exact@1 NEW (fused gen + shape rerank): {ex_new/n*100:5.1f}%")
    print(f"fused recall@{args.k}: {recall_fused/n*100:5.1f}%  (new ceiling)")


if __name__ == "__main__":
    main()
