#!/usr/bin/env python3
"""Experiment: does an ADVANCE-WIDTH / spacing metric channel fix recall?

Degradation-augmented SHAPE pulls the tail up but not into the top-50: under heavy
degradation the silhouette is genuinely ambiguous. Advance widths are geometry, not
ink texture, so they survive degradation and are ORTHOGONAL to shape. The document
really WAS typeset in the true font, so its measured spacing legitimately matches
the true font (and its metric-twins). Test whether fusing a metric score with the
coarse shape score lifts recall@50.

Honest measurement: advances are read from the DEGRADED scan's ink positions (with
noise), NOT from the ground-truth render positions. Caveat: real OCR segmentation
is noisier than this synthetic read, so treat any lift as an upper bound pending a
real-scan check.

    python tools/exp_metric.py [-n 120] [--pool 500]
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

import fitz                                              # noqa: E402
from pdftexteditor.ocr import fontbank as FB             # noqa: E402
from eval_fontmatch import render_clean_line, degrade, EVAL_TEXT  # noqa: E402
from exp_dmatch import cand_clean_cov                    # noqa: E402
from exp_recall import CHARS, scan_char_descs            # noqa: E402


def font_adv_sig(ttf_dir, key):
    """Per-char advance signature (text_length), normalized by its median so it is
    scale-free. None if the font is unreadable."""
    f = FB._cand_font(os.path.join(ttf_dir, key))
    if f is None:
        return None
    a = {}
    for ch in CHARS:
        try:
            if f.has_glyph(ord(ch)):
                a[ch] = float(f.text_length(ch, 100.0))
        except Exception:
            pass
    if len(a) < 6:
        return None
    med = np.median(list(a.values())) or 1.0
    return {c: v / med for c, v in a.items()}


def scan_adv_sig(scan, cells, text):
    """Per-char advance signature measured from the DEGRADED scan: ink-left of each
    glyph, advance = next glyph's ink-left minus this one's, normalized by median."""
    left = {}
    for i, (ch, box) in cells.items():
        x0, y0, x1, y1 = box
        sub = scan[y0:y1, x0:x1]
        if sub.size == 0:
            continue
        cov = FB._to_alpha(sub)
        colmass = (cov > 0.2).sum(0)
        nz = np.where(colmass > 0)[0]
        if len(nz):
            left[i] = x0 + int(nz[0])
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-n", type=int, default=120)
    ap.add_argument("--pool", type=int, default=500)
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()

    fb = FB._load_fingerprints()
    keys = fb["paths"]
    ttf_dir = FB._ensure_ttf_cache()
    rng = random.Random(args.seed)
    test = rng.sample(keys, min(args.n, len(keys)))
    pool = list(dict.fromkeys(test + rng.sample(keys, min(args.pool, len(keys)))))
    pool_idx = {k: i for i, k in enumerate(pool)}
    print(f"pool {len(pool)}, {len(test)} queries")

    t0 = time.time()
    pool_clean = {}      # key -> {ch -> clean coarse descriptor}
    pool_adv = {}        # key -> advance signature
    for i, key in enumerate(pool):
        d = {}
        for ch in CHARS:
            cov = cand_clean_cov(ttf_dir, key, ch)
            if cov is not None:
                dd = FB._descriptor(cov)
                if dd is not None:
                    d[ch] = dd
        pool_clean[key] = d
        pool_adv[key] = font_adv_sig(ttf_dir, key)
        if (i + 1) % 150 == 0:
            print(f"  built {i+1}/{len(pool)} ({time.time()-t0:.0f}s)", flush=True)

    def zscore(a):
        m = a[a > -1e8]
        mu, sd = (m.mean(), m.std()) if m.size else (0.0, 1.0)
        return (a - mu) / (sd or 1.0)

    rank = {m: [] for m in ("shape", "metric", "fused")}
    rec50 = {m: 0 for m in ("shape", "metric", "fused")}
    n = 0
    for s, key in enumerate(test):
        R = np.random.RandomState(2000 + s)
        em = rng.choice([26, 28, 30, 34])
        try:
            clean, cells, _ = render_clean_line(os.path.join(ttf_dir, key), EVAL_TEXT, em)
            scan = degrade(clean, R, 1.0)
        except Exception:
            continue
        sq = scan_char_descs(scan, cells)
        sadv = scan_adv_sig(scan, cells, EVAL_TEXT)
        if not sq or sadv is None:
            continue
        n += 1
        shape = np.full(len(pool), -1e9, np.float32)
        metric = np.full(len(pool), -1e9, np.float32)
        for pi, pk in enumerate(pool):
            pd = pool_clean[pk]
            tot, m = 0.0, 0
            for ch, q in sq.items():
                if ch in pd:
                    tot += float(pd[ch] @ q)
                    m += 1
            if m:
                shape[pi] = tot / m
            pa = pool_adv.get(pk)
            if pa:
                diffs = [abs(sadv[c] - pa[c]) for c in sadv if c in pa]
                if len(diffs) >= 6:
                    metric[pi] = -float(np.mean(diffs))
        fused = zscore(shape) + zscore(metric)
        ti = pool_idx[key]
        for name, arr in (("shape", shape), ("metric", metric), ("fused", fused)):
            r = int((arr > arr[ti]).sum())
            rank[name].append(r)
            rec50[name] += r < 50

    print(f"\n=== metric-channel probe: {n} queries in {len(pool)}-font pool "
          f"({time.time()-t0:.0f}s) ===")
    for name in ("shape", "metric", "fused"):
        a = np.array(rank[name])
        print(f"{name:7s}: rank median {np.median(a):4.0f}  mean {a.mean():4.0f}  "
              f"p90 {np.percentile(a,90):4.0f}   recall@50 {rec50[name]/n*100:5.1f}%")
    print("(metric measured from the degraded scan; real-OCR noise is larger)")


if __name__ == "__main__":
    main()
