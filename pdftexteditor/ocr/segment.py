"""Recover per-character glyph geometry from a recognized text LINE.

RapidOCR (and Apple Vision) return a box + string per LINE, not per glyph, but
the scan-font builder needs ink-tight per-character bitmaps and the editable
layer needs per-WORD boxes. This module bridges that gap with connected-
component analysis guided by the recognized string:

  * binarize the line strip (Otsu, ink = dark),
  * find connected components and merge vertically-stacked parts (the dot of
    i/j, the bars of =/:/; ) into per-column glyph groups,
  * estimate the line baseline from the group bottoms,
  * split the groups into WORDS at the wide inter-word gaps, aligned to the
    spaces in the recognized text,
  * harvest clean per-character glyph samples ONLY from words whose group count
    matches the word's character count (unambiguous 1:1 mapping) -- the font
    needs just a few clean instances per character, so conservative harvesting
    beats forcing a touching-character split and producing garbage glyphs.

CPU-only, depends on cv2 + numpy. Coordinates are LINE-LOCAL pixels unless
noted; the caller maps them to page pixels / PDF points.
"""

from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np


@dataclass
class GlyphInstance:
    """One clean, harvested character glyph, line-local pixels."""

    char: str
    bitmap: np.ndarray      # ink-tight bool (True = ink)
    x0: float               # ink-left in the line strip
    baseline_y: float       # line baseline (strip-local rows)
    top_y: float            # ink-top of this glyph (strip-local rows)


@dataclass
class WordBox:
    """One recognized word with its ink box in the line strip (local px)."""

    text: str
    x0: float
    x1: float
    top: float
    bottom: float


@dataclass
class LineSeg:
    """Result of segmenting one line strip."""

    baseline_y: float
    x_height_px: float
    words: list           # list[WordBox]
    glyphs: list          # list[GlyphInstance] (clean harvest only)
    space_px: float       # measured inter-word gap (or 0 if single word)


def binarize(strip: np.ndarray) -> np.ndarray:
    """Otsu binarize a grayscale/RGB line strip -> bool (True = ink/dark)."""
    if strip.ndim == 3:
        gray = cv2.cvtColor(strip, cv2.COLOR_RGB2GRAY)
    else:
        gray = strip
    _, bw = cv2.threshold(gray, 0, 255,
                          cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    return bw > 0


def _components(binary: np.ndarray) -> list:
    """Connected components as (x0, y0, x1, y1, area, label), x-sorted."""
    n, labels, stats, _ = cv2.connectedComponentsWithStats(
        binary.astype(np.uint8), connectivity=8)
    comps = []
    for lbl in range(1, n):                 # 0 == background
        x, y, w, h, area = stats[lbl]
        comps.append((x, y, x + w, y + h, int(area), lbl))
    comps.sort(key=lambda c: c[0])
    return comps, labels


def _merge_vertical(comps: list) -> list:
    """Merge components that belong to ONE glyph (a part stacked above/below
    another, like the dot of i/j or the bars of =/:/;): two comps merge when
    their x-intervals overlap by most of the narrower one. Returns merged groups
    as dicts with union bbox + member labels."""
    groups = []
    for x0, y0, x1, y1, area, lbl in comps:
        placed = False
        for g in groups:
            ox0, ox1 = g["x0"], g["x1"]
            inter = min(x1, ox1) - max(x0, ox0)
            narrow = min(x1 - x0, ox1 - ox0)
            if narrow > 0 and inter >= 0.6 * narrow:
                g["x0"] = min(g["x0"], x0)
                g["x1"] = max(g["x1"], x1)
                g["y0"] = min(g["y0"], y0)
                g["y1"] = max(g["y1"], y1)
                g["area"] += area
                g["labels"].append(lbl)
                placed = True
                break
        if not placed:
            groups.append(dict(x0=x0, y0=y0, x1=x1, y1=y1, area=area,
                               labels=[lbl]))
    groups.sort(key=lambda g: g["x0"])
    return groups


def _baseline(groups: list) -> tuple:
    """Estimate (baseline_y, x_height_px) from group geometry. Baseline is the
    modal bottom of non-descender groups; x-height is the modal height of the
    short (x-height) groups."""
    if not groups:
        return 0.0, 0.0
    bottoms = np.array([g["y1"] for g in groups], dtype=float)
    heights = np.array([g["y1"] - g["y0"] for g in groups], dtype=float)
    # Most letters rest on the baseline; descenders dip below. The 60th
    # percentile of bottoms is a robust baseline (above the few descenders).
    baseline = float(np.percentile(bottoms, 60))
    # x-height: median height of groups whose top is at/above baseline and that
    # are not tall ascenders/caps -- approximate by the lower-median height.
    short = heights[heights <= np.percentile(heights, 60)]
    xh = float(np.median(short)) if short.size else float(np.median(heights))
    return baseline, xh


def _split_words(groups: list, text: str, min_gap: float) -> list:
    """Split x-sorted groups into words at the inter-word gaps, aligned to the
    number of space-delimited tokens in ``text``. A gap qualifies as a word
    break only if it exceeds ``min_gap`` (a fraction of the em), which keeps
    intra-word letter gaps from being mistaken for spaces; among qualifying
    gaps the (len(tokens)-1) widest are chosen. Returns list of
    (token, [group,...])."""
    tokens = text.split()
    if not groups or not tokens:
        return []
    if len(tokens) == 1 or len(groups) <= 1:
        return [(text.strip(), groups)]
    gaps = []                               # (gap_width, index_after)
    for i in range(len(groups) - 1):
        gap = groups[i + 1]["x0"] - groups[i]["x1"]
        if gap > min_gap:
            gaps.append((gap, i + 1))
    # The (len(tokens)-1) widest QUALIFYING gaps are the word breaks.
    n_breaks = min(len(tokens) - 1, len(gaps))
    cut_idx = sorted(idx for _, idx in
                     sorted(gaps, reverse=True)[:n_breaks])
    chunks = []
    start = 0
    for c in cut_idx:
        chunks.append(groups[start:c])
        start = c
    chunks.append(groups[start:])
    # Pair chunks with tokens positionally; if counts differ, zip the shorter.
    out = []
    for tok, chunk in zip(tokens, chunks):
        if chunk:
            out.append((tok, chunk))
    return out


def segment_line(strip: np.ndarray, text: str) -> "LineSeg | None":
    """Segment one binarized-able line strip + its recognized ``text`` into
    word boxes and clean per-character glyph samples (see module docstring)."""
    binary = binarize(strip)
    if not binary.any():
        return None
    comps, _ = _components(binary)
    # Drop specks: components far smaller than the median (noise / stray dots
    # that are not real glyph parts). Keep small comps that could be punctuation
    # by area floor relative to the strip height.
    if not comps:
        return None
    areas = np.array([c[4] for c in comps])
    floor = max(2.0, 0.02 * float(np.median(areas)))
    comps = [c for c in comps if c[4] >= floor]
    groups = _merge_vertical(comps)
    if not groups:
        return None
    baseline, xh = _baseline(groups)
    em = xh / 0.45 if xh else 0.0           # rough em from x-height (serif)
    min_gap = max(0.14 * em, 3.0)           # word-break floor

    word_chunks = _split_words(groups, text, min_gap)
    words: list[WordBox] = []
    glyphs: list[GlyphInstance] = []
    space_samples: list[float] = []
    prev_word_x1 = None
    for tok, chunk in word_chunks:
        if not chunk:
            continue
        wx0 = min(g["x0"] for g in chunk)
        wx1 = max(g["x1"] for g in chunk)
        wt = min(g["y0"] for g in chunk)
        wb = max(g["y1"] for g in chunk)
        words.append(WordBox(text=tok, x0=wx0, x1=wx1, top=wt, bottom=wb))
        if prev_word_x1 is not None:
            space_samples.append(wx0 - prev_word_x1)
        prev_word_x1 = wx1
        # Harvest glyphs only when the group count matches the token length
        # exactly -- an unambiguous 1:1 char<->group mapping.
        token_chars = list(tok)
        if len(chunk) == len(token_chars):
            for g, ch in zip(chunk, token_chars):
                bm = _group_bitmap(binary, g)
                if bm is None:
                    continue
                glyphs.append(GlyphInstance(
                    char=ch, bitmap=bm, x0=float(g["x0"]),
                    baseline_y=baseline, top_y=float(g["y0"])))
    space_px = float(np.median(space_samples)) if space_samples else 0.0
    return LineSeg(baseline_y=baseline, x_height_px=xh, words=words,
                   glyphs=glyphs, space_px=space_px)


def _group_bitmap(binary: np.ndarray, g: dict) -> "np.ndarray | None":
    """Ink-tight bool bitmap for one merged group (its bounding box of ink)."""
    sub = binary[g["y0"]:g["y1"], g["x0"]:g["x1"]]
    if sub.size == 0 or not sub.any():
        return None
    return sub.copy()
