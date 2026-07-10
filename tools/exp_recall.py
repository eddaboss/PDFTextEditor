#!/usr/bin/env python3
"""Experiment: does DEGRADATION-AUGMENTED candidate generation fix recall?

The baseline shows the coarse stage drops the true font ~1/3 of the time under
realistic degradation (true font ranked ~1000+), and recall is the hard ceiling.
Hypothesis: the coarse descriptor is brittle because it compares a CLEAN bank
template to a DEGRADED scan. If each font's descriptor is also stored at a few
degradation levels and we score by the BEST-matching level, the true font should
climb back into the shortlist.

Tests within a font pool (test fonts + distractors): true-font median RANK and
recall@50, scoring each pool font by CLEAN descriptor vs by MAX-over-severities
(clean + degraded variants). Lower rank / higher recall for the augmented variant
== degradation-augmented candidate generation fixes recall.

    python tools/exp_recall.py [-n 120] [--pool 500]
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

from pdftexteditor.ocr import fontbank as FB           # noqa: E402
from scan_degrade import letterfilter as LF             # noqa: E402
from eval_fontmatch import render_clean_line, degrade, EVAL_TEXT, _INK, _PAPER  # noqa: E402
from exp_dmatch import cand_clean_cov                    # noqa: E402

# discriminative-ish chars present in EVAL_TEXT (digits + telling letters)
CHARS = "0123456789aeginorstuvAB"
SEVS = [None, 0.12, 0.24, 0.38]   # None == clean template; others == degraded levels


def font_descs(ttf_dir, key, ch):
    """Coarse descriptors for (font, char) at each severity in SEVS (cached)."""
    cov = cand_clean_cov(ttf_dir, key, ch)
    if cov is None:
        return None
    rgb = np.repeat((255.0 * (1.0 - cov))[..., None], 3, 2).astype(np.uint8)
    out = []
    for j, sev in enumerate(SEVS):
        if sev is None:
            d = FB._descriptor(cov)
        else:
            deg = LF.hard_degrade(rgb, _INK, _PAPER, sev,
                                  np.random.RandomState(100 + j))
            d = FB._descriptor(FB._to_alpha(deg))
        out.append(d)
    return out


def scan_char_descs(scan, cells):
    """Scan's coarse descriptor per char (mean of instances, re-normalized)."""
    per: dict = {}
    for ch, box in cells.values():
        if ch not in CHARS:
            continue
        d = FB._glyph_descriptor(scan, box)
        if d is not None:
            per.setdefault(ch, []).append(d)
    out = {}
    for ch, ds in per.items():
        q = np.mean(ds, 0)
        q -= q.mean()
        nq = np.linalg.norm(q)
        if nq > 1e-6:
            out[ch] = q / nq
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-n", type=int, default=120, help="test fonts (queries)")
    ap.add_argument("--pool", type=int, default=500, help="candidate pool size")
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()

    fb = FB._load_fingerprints()
    keys = fb["paths"]
    ttf_dir = FB._ensure_ttf_cache()
    rng = random.Random(args.seed)
    test = rng.sample(keys, min(args.n, len(keys)))
    pool = list(dict.fromkeys(test + rng.sample(keys, min(args.pool, len(keys)))))
    print(f"pool {len(pool)} fonts, {len(test)} queries, sevs={SEVS}, chars={CHARS}")

    # precompute pool descriptors (clean + degraded) per (font, char)
    t0 = time.time()
    pool_desc = {}      # key -> {ch -> [desc per sev]}
    for i, key in enumerate(pool):
        d = {}
        for ch in CHARS:
            v = font_descs(ttf_dir, key, ch)
            if v is not None:
                d[ch] = v
        pool_desc[key] = d
        if (i + 1) % 100 == 0:
            print(f"  built {i+1}/{len(pool)} ({time.time()-t0:.0f}s)", flush=True)

    nlev = len(SEVS)
    pool_idx = {k: i for i, k in enumerate(pool)}
    rank = {m: [] for m in ("clean", "maxall", "sevest")}
    rec50 = {m: 0 for m in ("clean", "maxall", "sevest")}
    lstar_hist = np.zeros(nlev, int)
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
        if not sq:
            continue
        n += 1
        # per-severity-level population scores: sc[L, font]
        sc = np.full((nlev, len(pool)), -1.0, np.float32)
        for pi, pk in enumerate(pool):
            pd = pool_desc[pk]
            tot = [0.0] * nlev
            m = 0
            for ch, q in sq.items():
                if ch not in pd:
                    continue
                v = pd[ch]
                for L in range(nlev):
                    tot[L] += float(v[L] @ q)
                m += 1
            if m:
                for L in range(nlev):
                    sc[L, pi] = tot[L] / m
        ti = pool_idx[key]
        # CLEAN: level 0 only.  MAXALL: best level per font (the naive aug).
        # SEVEST: estimate the scan's level as the one with the most confident top
        # match, then rank ALL fonts at THAT level (no per-font inflation).
        clean = sc[0]
        maxall = sc.max(0)
        lstar = int(np.argmax([sc[L].max() for L in range(nlev)]))
        lstar_hist[lstar] += 1
        sevest = sc[lstar]
        for name, arr in (("clean", clean), ("maxall", maxall), ("sevest", sevest)):
            r = int((arr > arr[ti]).sum())
            rank[name].append(r)
            rec50[name] += r < 50

    print(f"\n=== recall probe: {n} queries in a {len(pool)}-font pool, "
          f"sevs={SEVS} ({time.time()-t0:.0f}s) ===")
    print(f"severity picked (L*): {dict(enumerate(lstar_hist))}  (0=clean)")
    for name in ("clean", "maxall", "sevest"):
        a = np.array(rank[name])
        print(f"{name:7s}: rank median {np.median(a):4.0f}  mean {a.mean():4.0f}  "
              f"p90 {np.percentile(a,90):4.0f}   recall@50 {rec50[name]/n*100:5.1f}%")


if __name__ == "__main__":
    main()
