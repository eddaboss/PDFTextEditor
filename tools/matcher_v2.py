#!/usr/bin/env python3
"""Prototype v2 font matcher: fused (shape + advance-width) candidate generation,
then fine SHAPE re-rank with the harmful _glyph_features penalty OFF.

Validated in the harness to beat the current identify on exact@1 (32.5% -> ~44%
on the full bank). Drop-in shape: identify_v2(scan_rgb, cells) mirrors
fontbank.identify's signature/return. The advance-width signature bank is built
once from the TTF cache (text_length only, no rendering) and cached to npy.
"""
from __future__ import annotations

import os
import numpy as np

from pdftexteditor.ocr import fontbank as FB

# chars used for the shape-metric AND advance signatures (digits + telling letters)
CHARS = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPRSTUVWY"
_ADV = None          # (F, len(CHARS)) normalized advance bank, nan where missing


def _build_adv_bank():
    global _ADV
    if _ADV is not None:
        return _ADV
    keys = FB._load_fingerprints()["paths"]
    ttf_dir = FB._ensure_ttf_cache()
    cache = os.path.join(FB.bank_dir(), "font_adv_sig.npy")
    if os.path.exists(cache):
        a = np.load(cache)
        if a.shape == (len(keys), len(CHARS)):
            _ADV = a
            return _ADV
    mat = np.full((len(keys), len(CHARS)), np.nan, np.float32)
    for fi, key in enumerate(keys):
        f = FB._cand_font(os.path.join(ttf_dir, key))
        if f is None:
            continue
        row = []
        for ch in CHARS:
            try:
                row.append(float(f.text_length(ch, 100.0)) if f.has_glyph(ord(ch))
                           else np.nan)
            except Exception:
                row.append(np.nan)
        row = np.array(row, np.float32)
        med = np.nanmedian(row)
        if med and np.isfinite(med):
            mat[fi] = row / med
    try:
        np.save(cache, mat)
    except Exception:
        pass
    _ADV = mat
    return _ADV


def _lines_from_cells(cells):
    """Group cells into text lines by vertical overlap, each sorted left-to-right.
    Returns list of [(char, (x0,y0,x1,y1)), ...]."""
    items = [(c, b) for c, b in cells.values()]
    items.sort(key=lambda it: (it[1][1], it[1][0]))
    lines, cur, cy = [], [], None
    for ch, b in items:
        ymid = (b[1] + b[3]) / 2
        h = b[3] - b[1]
        if cy is None or abs(ymid - cy) <= 0.6 * max(h, 1):
            cur.append((ch, b))
            cy = ymid if cy is None else 0.5 * (cy + ymid)
        else:
            lines.append(sorted(cur, key=lambda it: it[1][0]))
            cur, cy = [(ch, b)], ymid
    if cur:
        lines.append(sorted(cur, key=lambda it: it[1][0]))
    return lines


def _scan_adv_sig(cells):
    """Per-char advance signature measured from the scan's glyph x-positions
    (left-edge to next left-edge within a line), normalized by median."""
    cix = {c: i for i, c in enumerate(CHARS)}
    per = {}
    for line in _lines_from_cells(cells):
        for j in range(len(line) - 1):
            ch = line[j][0]
            if ch not in cix:
                continue
            adv = line[j + 1][1][0] - line[j][1][0]
            if adv > 0:
                per.setdefault(ch, []).append(adv)
    sig = np.full(len(CHARS), np.nan, np.float32)
    for ch, vs in per.items():
        sig[cix[ch]] = np.median(vs)
    med = np.nanmedian(sig)
    return sig / med if med and np.isfinite(med) else None


def _coarse_scores(scan_rgb, cells):
    fb = FB._load_fingerprints()
    bank = fb["desc"]
    F = bank.shape[0]
    per_char = {}
    for ch, box in cells.values():
        if ch not in FB._CIDX:
            continue
        d = FB._glyph_descriptor(scan_rgb, box)
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
    ms = np.full(F, -1e9, np.float32)
    valid = wsum > 0
    ms[valid] = score[valid] / wsum[valid]
    return ms, int(sum(len(v) for v in per_char.values()))


def _z(a):
    m = a[a > -1e8]
    mu, sd = (m.mean(), m.std()) if m.size else (0.0, 1.0)
    return (a - mu) / (sd or 1.0)


def identify_v2(scan_rgb, cells, topk: int = 5, k: int = 50, wmetric: float = 1.0,
                restrict=None):
    """Fused shape+metric candidate generation -> penalty-off fine shape re-rank.
    ``restrict``: optional iterable of allowed font indices (curated bank subset);
    fonts outside it are excluded from the ranking. Returns
    {best, confidence, margin, topk, n_glyphs} like fontbank.identify."""
    keys = FB._load_fingerprints()["paths"]
    shape, n_glyphs = _coarse_scores(scan_rgb, cells)
    if n_glyphs == 0:
        return dict(best=None, confidence=0.0, margin=0.0, topk=[], n_glyphs=0)
    fused = _z(shape)
    sig = _scan_adv_sig(cells)
    if sig is not None:
        adv = _build_adv_bank()
        mask = ~np.isnan(sig)
        if mask.sum() >= 6:
            d = np.abs(adv[:, mask] - sig[mask])
            metric = np.full(len(keys), -1e9, np.float32)
            ok = (~np.isnan(d)).sum(1) >= 6
            metric[ok] = -np.nanmean(d[ok], axis=1)
            fused = fused + wmetric * _z(metric)
    if restrict is not None:
        allow = np.zeros(len(keys), bool)
        allow[np.fromiter(restrict, int)] = True
        fused = np.where(allow, fused, -1e18)
    order = np.argsort(-fused)
    cand = [int(i) for i in order[:k]]
    lam = FB._FEAT_LAMBDA
    FB._FEAT_LAMBDA = 0.0
    try:
        rr = FB._refine_bank(scan_rgb, cells, cand)
    finally:
        FB._FEAT_LAMBDA = lam
    top = [(keys[i], float(s)) for i, s in rr[:topk]]
    if not top:
        return dict(best=None, confidence=0.0, margin=0.0, topk=[], n_glyphs=n_glyphs)
    best_s = top[0][1]
    second = top[1][1] if len(top) > 1 else 0.0
    return dict(best=top[0][0], confidence=best_s, margin=best_s - second,
                topk=top, n_glyphs=n_glyphs)
