#!/usr/bin/env python3
"""Experiment: does DEGRADATION-MATCHED re-rank beat clean-template re-rank?

Hypothesis (from the baseline finding that the clean fine re-rank is net-negative
under realistic degradation): if we compare the scanned glyph against each
candidate font's glyph DEGRADED to a matching level (best over a few severities),
the fine detail becomes usable instead of noise, and exact@1 rises above both
coarse-only and the current clean fine re-rank.

Reranks the coarse top-K by max-over-severities cosine of the fine descriptor
(scan vs degraded-candidate). Reports exact@1 for coarse-only, current fine, and
degradation-matched, on the SAME samples (and conditioned on recall succeeding).

    python tools/exp_dmatch.py [-n 30] [-k 15]
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

from pdftexteditor.ocr import fontbank as FB          # noqa: E402
from scan_degrade import letterfilter as LF            # noqa: E402
from eval_fontmatch import (render_clean_line, degrade, coarse_rank,  # noqa: E402
                            EVAL_TEXT, _INK, _PAPER)

# severities to try per candidate; best match wins (no severity estimate needed)
_SEVS = [(0.10, 11), (0.22, 23), (0.34, 37)]
_CLEAN_COV: dict = {}


def cand_clean_cov(ttf_dir: str, key: str, ch: str):
    k = (key, ch)
    if k in _CLEAN_COV:
        return _CLEAN_COV[k]
    f = FB._cand_font(os.path.join(ttf_dir, key))
    cov = None
    try:
        if f is not None and f.has_glyph(ord(ch)):
            cov = FB._render_glyph_cov(f, ch)
    except Exception:
        cov = None
    _CLEAN_COV[k] = cov
    return cov


def coarse_order(scan, cells):
    """Coarse shortlist (indices) + the scan's per-char fine descriptor."""
    fb = FB._load_fingerprints()
    bank = fb["desc"]
    F = bank.shape[0]
    per_char: dict = {}
    scan_fine: dict = {}
    for ch, box in cells.values():
        if ch not in FB._CIDX:
            continue
        x0, y0, x1, y1 = box
        cell = scan[max(0, y0):y1, max(0, x0):x1]
        if cell.size == 0 or cell.shape[0] < 4 or cell.shape[1] < 4:
            continue
        alpha = FB._to_alpha(cell)
        d = FB._glyph_descriptor(scan, box)
        if d is not None:
            per_char.setdefault(FB._CIDX[ch], []).append(d)
        fd = FB._fine_from_cov(alpha)
        if fd is not None:
            scan_fine.setdefault(ch, []).append(fd)
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
    sf = {c: FB._unit(np.mean(v, 0)) for c, v in scan_fine.items()}
    sf = {c: v for c, v in sf.items() if v is not None}
    return order, sf


def dmatch_score(ttf_dir, key, scan_fine):
    tot, n = 0.0, 0
    for ch, sd in scan_fine.items():
        cov = cand_clean_cov(ttf_dir, key, ch)
        if cov is None:
            continue
        rgb = np.repeat((255.0 * (1.0 - cov))[..., None], 3, 2).astype(np.uint8)
        best = -1.0
        for sev, seed in _SEVS:
            deg = LF.hard_degrade(rgb, _INK, _PAPER, sev, np.random.RandomState(seed))
            dd = FB._fine_from_cov(FB._to_alpha(deg))
            if dd is not None:
                best = max(best, float(dd @ sd))
        if best > -1.0:
            tot += best
            n += 1
    return tot / n if n else -1.0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-n", type=int, default=30)
    ap.add_argument("-k", type=int, default=15, help="coarse candidates to re-rank")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--fast", action="store_true",
                    help="skip the live-render dmatch (prebuilt-bank stages only)")
    args = ap.parse_args()

    fb = FB._load_fingerprints()
    keys = fb["paths"]
    idx_of = {k: i for i, k in enumerate(keys)}
    ttf_dir = FB._ensure_ttf_cache()
    rng = random.Random(args.seed)
    sample = rng.sample(keys, min(args.n, len(keys)))

    n = 0
    ex_coarse = ex_fine = ex_fshape = ex_dmatch = 0
    rn = 0           # recall (true in top-k)
    ex_coarse_r = ex_fine_r = ex_fshape_r = ex_dmatch_r = 0   # conditioned on recall
    t0 = time.time()
    for s, key in enumerate(sample):
        R = np.random.RandomState(1000 + s)
        em = rng.choice([26, 28, 30, 34])
        try:
            clean, cells, _ = render_clean_line(os.path.join(ttf_dir, key), EVAL_TEXT, em)
            scan = degrade(clean, R, 1.0)
            order, scan_fine = coarse_order(scan, cells)
        except Exception as e:
            print("  fail", key, repr(e)[:60])
            continue
        n += 1
        cand = [int(i) for i in order[:args.k]]
        true_idx = idx_of[key]
        in_topk = true_idx in cand
        rn += in_topk
        coarse_top = keys[cand[0]]
        # fine re-rank over the SAME candidate set: full (shape - feature penalty)
        # vs shape-cosine only, to isolate which component hurts.
        ffull = FB._refine_bank(scan, cells, cand)
        fine_top = keys[ffull[0][0]] if ffull else None
        lam = FB._FEAT_LAMBDA
        FB._FEAT_LAMBDA = 0.0
        fsh = FB._refine_bank(scan, cells, cand)
        FB._FEAT_LAMBDA = lam
        fshape_top = keys[fsh[0][0]] if fsh else None
        if args.fast:
            dmatch_top = None
        else:
            dm = sorted(((keys[i], dmatch_score(ttf_dir, keys[i], scan_fine)) for i in cand),
                        key=lambda x: -x[1])
            dmatch_top = dm[0][0] if dm else None
        ec = coarse_top == key
        ef = fine_top == key
        es = fshape_top == key
        ed = dmatch_top == key
        ex_coarse += ec
        ex_fine += ef
        ex_fshape += es
        ex_dmatch += ed
        if in_topk:
            ex_coarse_r += ec
            ex_fine_r += ef
            ex_fshape_r += es
            ex_dmatch_r += ed
        print(f"  [{n:3d}] {key}  coarse:{'Y' if ec else '.'} "
              f"fine:{'Y' if ef else '.'} fshape:{'Y' if es else '.'} "
              f"dmatch:{'Y' if ed else '.'} "
              f"{'' if in_topk else '(RECALL MISS)'}", flush=True)

    print(f"\n=== degradation-matched re-rank: {n} fonts, K={args.k}, "
          f"{time.time()-t0:.0f}s ===")
    print(f"recall@{args.k}        : {rn/n*100:5.1f}%  (ceiling for any re-rank)")
    print(f"exact@1 coarse        : {ex_coarse/n*100:5.1f}%")
    print(f"exact@1 fine full(now): {ex_fine/n*100:5.1f}%")
    print(f"exact@1 fine shape-only:{ex_fshape/n*100:5.1f}%")
    print(f"exact@1 DMATCH        : {ex_dmatch/n*100:5.1f}%")
    if rn:
        print(f"--- conditioned on recall success (the re-rank's real job) ---")
        print(f"exact@1 coarse    |R  : {ex_coarse_r/rn*100:5.1f}%")
        print(f"exact@1 fine full |R  : {ex_fine_r/rn*100:5.1f}%")
        print(f"exact@1 fine shape|R  : {ex_fshape_r/rn*100:5.1f}%")
        print(f"exact@1 DMATCH    |R  : {ex_dmatch_r/rn*100:5.1f}%")


if __name__ == "__main__":
    main()
