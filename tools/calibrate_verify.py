#!/usr/bin/env python3
"""Phase 0: does rebuild-and-compare beat the live matcher, and does degrade-matching
help? On the labeled synthetic set from eval_fontmatch, for each sampled font:

  * run the LIVE matcher (fontbank.identify) -> exact@1
  * rerank the coarse top-K by the verifier's shape cosine, CLEAN candidate -> exact@1
  * rerank again with the candidate DEGRADE-MATCHED to the scan's residual -> exact@1
  * on the matcher's MISSES (where the true font is still in the coarse top-K), how
    often does each verifier recover it
  * time a full K-candidate rerank for the 200-500ms budget

    python tools/calibrate_verify.py [-n 30] [-k 40] [--ease 1.0]
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

from pdftexteditor.ocr import degrade as DG  # noqa: E402
from pdftexteditor.ocr import fontbank as FB  # noqa: E402
from pdftexteditor.ocr import verify as V  # noqa: E402
from eval_fontmatch import render_clean_line, degrade, EVAL_TEXT  # noqa: E402


def coarse_order(scan, cells):
    fb = FB._load_fingerprints()
    bank = fb["desc"]
    F = bank.shape[0]
    per: dict = {}
    for ch, box in cells.values():
        if ch in FB._CIDX:
            d = FB._glyph_descriptor(scan, box)
            if d is not None:
                per.setdefault(FB._CIDX[ch], []).append(d)
    score = np.zeros(F, np.float32)
    wsum = np.zeros(F, np.float32)
    for cidx, ds in per.items():
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
    ms = np.full(F, -1.0, np.float32)
    valid = wsum > 0
    ms[valid] = score[valid] / wsum[valid]
    return np.argsort(-ms)


def best_of(rep, ttf, keys, cand_ids, residual=None):
    """Argmax of the verifier's combined score over candidate ids (scan pooled once).
    Returns key or None."""
    best, bestv = None, -1.0
    for ci in cand_ids:
        m = V.score_against(rep, os.path.join(ttf, keys[ci]), residual=residual)
        if m is not None:
            s = V.combined(m)
            if s > bestv:
                bestv, best = s, ci
    return keys[best] if best is not None else None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("-n", type=int, default=30)
    ap.add_argument("-k", type=int, default=40, help="coarse candidates reranked")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--ease", type=float, default=1.0)
    args = ap.parse_args()

    fp = FB._load_fingerprints()
    keys = fp["paths"]
    idx_of = {kk: i for i, kk in enumerate(keys)}
    ttf = FB._ensure_ttf_cache()
    print(f"matcher: {FB.__file__}\nbank: {len(keys)} fonts | K={args.k} | "
          f"ease={args.ease} | fine-bank={FB._load_fine_bank() is not None}")

    rng = random.Random(args.seed)
    sample = rng.sample(keys, min(args.n, len(keys)))

    n_ok = coarse_in_k = m_exact = vc_exact = vd_exact = 0
    miss_rec = mc_rec = md_rec = 0
    t_clean = t_deg = 0.0

    for k, key in enumerate(sample):
        try:
            R = np.random.RandomState(1000 + k)
            em = rng.choice([26, 28, 30, 34])
            clean, cells, _ = render_clean_line(os.path.join(ttf, key), EVAL_TEXT, em)
            scan = degrade(clean, R, args.ease)
        except Exception as e:
            print(f"  fail {key}: {repr(e)[:60]}")
            continue
        cells_list = list(cells.values())
        order = coarse_order(scan, cells)
        cand_ids = [int(c) for c in order[:args.k]]
        ti = idx_of[key]
        in_k = ti in cand_ids
        coarse_in_k += in_k
        n_ok += 1

        res = FB.identify(scan, cells, topk=1)
        me = res.get("best") == key
        m_exact += me

        geom = {"char_boxes": [b for _c, b in cells_list]}
        prof = DG.build_residual_filter(scan, geom)
        residual = None
        if prof is not None:
            ink, paper = DG.sample_ink_paper(scan)
            residual = (prof, ink, paper, 7)

        rep = V.scan_word_repr(scan, cells_list)
        t0 = time.time()
        pick_c = best_of(rep, ttf, keys, cand_ids)
        t_clean += time.time() - t0
        ce = pick_c == key
        vc_exact += ce

        de = ce
        if residual is not None:
            t0 = time.time()
            pick_d = best_of(rep, ttf, keys, cand_ids, residual)
            t_deg += time.time() - t0
            de = pick_d == key
        vd_exact += de

        if (not me) and in_k:
            miss_rec += 1
            mc_rec += ce
            md_rec += de
        if (k + 1) % 10 == 0:
            print(f"  ...{k + 1}/{len(sample)}")

    if n_ok == 0:
        print("no samples")
        return
    pc = lambda x: f"{x / n_ok * 100:5.1f}%"  # noqa: E731
    print(f"\n=== exact@1 over {n_ok} fonts (ease {args.ease}) ===")
    print(f"coarse recall@{args.k} : {pc(coarse_in_k)}  (ceiling for any rerank)")
    print(f"matcher (identify) : {pc(m_exact)}  ({m_exact}/{n_ok})")
    print(f"verifier CLEAN     : {pc(vc_exact)}  ({vc_exact}/{n_ok})")
    print(f"verifier DEGRADED  : {pc(vd_exact)}  ({vd_exact}/{n_ok})")
    if miss_rec:
        print(f"\non matcher MISSES that are recoverable (true in top-{args.k}): "
              f"{miss_rec}")
        print(f"  verifier CLEAN recovered    : {mc_rec}/{miss_rec}")
        print(f"  verifier DEGRADED recovered : {md_rec}/{miss_rec}")
    print(f"\n=== timing: full {args.k}-candidate rerank per word ===")
    print(f"clean    : {t_clean / n_ok * 1000:6.0f} ms")
    if t_deg:
        print(f"degraded : {t_deg / n_ok * 1000:6.0f} ms   (target 200-500ms)")
    print("note: EVAL_TEXT is a ~50-char line; a real word is far shorter.")


if __name__ == "__main__":
    main()
