"""Pick the bundled family whose glyph SHAPES best match the scanned text.

A stroke-contrast serif/sans heuristic does not survive scan degradation (dropout
inflates a sans face's apparent contrast to serif levels), so 0.3.0 matches actual
SHAPES: each candidate family (Tinos = Times, Arimo = Arial, Cousine = Courier --
the three fonts most scanned documents use) is fingerprinted by rendering reference
glyphs to normalized, blurred, zero-mean unit-norm coverage tiles; the scanned
glyphs are turned into the same descriptor and matched by correlation. Robust to
degradation because the blur + averaging wash out the dropout while the silhouette
survives. Tiny (3 families, cached once); pure cv2 / fitz / numpy.
"""
from __future__ import annotations

import os

import cv2
import fitz
import numpy as np

_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "assets", "fonts")
# family name -> bundled file. Order = preference on ties.
_CANDIDATES = {"Tinos": "Tinos-Regular.ttf", "Arimo": "Arimo[wght].ttf",
               "Cousine": "Cousine-Regular.ttf"}
_REF = "aeonrstilcdhmugbpAERNTHSILOG23456789"
S = 24
_FILL = S - 4
_BLUR = 0.6
_RENDER_EM = 64
_fp_cache: dict | None = None


def _normtile(cov: np.ndarray) -> np.ndarray | None:
    ys, xs = np.where(cov > 0.15)
    if len(ys) < 4:
        return None
    crop = cov[ys.min():ys.max() + 1, xs.min():xs.max() + 1]
    h, w = crop.shape
    sc = _FILL / max(h, w)
    nh, nw = max(1, round(h * sc)), max(1, round(w * sc))
    rs = cv2.resize(crop.astype(np.float32), (nw, nh), interpolation=cv2.INTER_AREA)
    tile = np.zeros((S, S), np.float32)
    oy, ox = (S - nh) // 2, (S - nw) // 2
    tile[oy:oy + nh, ox:ox + nw] = rs
    return tile


def _descriptor(cov: np.ndarray) -> np.ndarray | None:
    t = _normtile(cov)
    if t is None:
        return None
    v = cv2.GaussianBlur(t, (0, 0), _BLUR).reshape(-1).astype(np.float32)
    v -= v.mean()
    n = np.linalg.norm(v)
    return v / n if n > 1e-6 else None


def _render_cov(font: "fitz.Font", ch: str) -> np.ndarray | None:
    if not font.has_glyph(ord(ch)):
        return None
    em = _RENDER_EM
    doc = fitz.open()
    pg = doc.new_page(width=int(font.text_length(ch, em) + 2 * em), height=int(em * 3))
    tw = fitz.TextWriter(pg.rect)
    tw.append((em, em * 2.0), ch, font=font, fontsize=em)
    tw.write_text(pg)
    pm = pg.get_pixmap(alpha=False)
    img = np.frombuffer(pm.samples, np.uint8).reshape(pm.height, pm.width, pm.n)[..., :3]
    return (255 - img.mean(2)) / 255.0


def _fingerprints() -> dict:
    """{family: {char: descriptor}} for the candidate bundled families (cached)."""
    global _fp_cache
    if _fp_cache is not None:
        return _fp_cache
    fp: dict = {}
    for fam, fn in _CANDIDATES.items():
        path = os.path.join(_DIR, fn)
        if not os.path.exists(path):
            continue
        try:
            f = fitz.Font(fontfile=path)
        except Exception:
            continue
        d = {}
        for ch in _REF:
            cov = _render_cov(f, ch)
            if cov is None:
                continue
            desc = _descriptor(cov)
            if desc is not None:
                d[ch] = desc
        if d:
            fp[fam] = d
    _fp_cache = fp
    return fp


def classify_family(rep_bitmap: "dict[str, np.ndarray]", default: str = "Tinos") -> str:
    """Best-matching bundled family for the scanned glyphs. ``rep_bitmap`` maps a
    character to a representative ink bitmap (bool/0-1) harvested from the scan."""
    fp = _fingerprints()
    if not fp:
        return default
    qd = {}
    for ch, bm in rep_bitmap.items():
        if ch not in _REF or bm is None or bm.size == 0:
            continue
        d = _descriptor(bm.astype(np.float32))
        if d is not None:
            qd[ch] = d
    if not qd:
        return default
    best, best_s = default, -2.0
    for fam, fd in fp.items():
        sims = [float(fd[ch] @ qd[ch]) for ch in qd if ch in fd]
        if not sims:
            continue
        s = sum(sims) / len(sims)
        if s > best_s:
            best, best_s = fam, s
    return best
