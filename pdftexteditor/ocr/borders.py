"""Table / page BORDER map for a scanned page.

A scanned form's structure lives in its RULE LINES: the page frame, the table grid,
and section dividers. This module finds those lines (long horizontal and vertical dark
runs) so we can (a) show them in the debug overlay and (b) use them as hard cell
boundaries when splitting an over-merged OCR row into per-cell boxes.

Pipeline:
  1. Binarize at a cutoff high enough to catch a FAINT rule (a long-run OPEN rejects text
     at any cutoff, so the cutoff only controls how faint a rule we still see).
  2. OPEN with a long horizontal / vertical element -> only rule-length runs survive.
  3. CLOSE small breaks so a rule stays one segment; length+thinness filter rejects blocks.
  4. REFINE against the grid: each line snaps its ends to the perpendicular rules that
     cross it, so a rule faint near a crossing still runs cell-edge to cell-edge -- and a
     short mark that covers only a sliver of its cell (a stray stub) is dropped.
"""
from __future__ import annotations

import numpy as np

_SNAP_TOL = 16          # px: an endpoint this close to a crossing rule is "at" it
_MIN_COVER = 0.35       # a real rule fills at least this much of the cell it sits in


def _raw_lines(image_rgb, min_frac, dark_thresh):
    import cv2
    gray = image_rgb.mean(2) if image_rgb.ndim == 3 else image_rgb.astype(float)
    bw = (gray < dark_thresh).astype(np.uint8)
    H, W = bw.shape
    hk, vk = max(18, W // 35), max(18, H // 35)
    hmask = cv2.morphologyEx(
        bw, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (hk, 1)))
    vmask = cv2.morphologyEx(
        bw, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (1, vk)))
    hmask = cv2.morphologyEx(
        hmask, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_RECT, (W // 25, 1)))
    vmask = cv2.morphologyEx(
        vmask, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_RECT, (1, H // 25)))

    def _segs(mask, vert):
        n, _lbl, stats, _cen = cv2.connectedComponentsWithStats(mask, 8)
        out = []
        for i in range(1, n):
            x, y, w, h, _a = stats[i]
            length = h if vert else w
            thick = w if vert else h
            if length > min_frac * (H if vert else W) \
                    and thick < max(8, min(30, int(length * 0.12))):
                out.append((int(x), int(y), int(x + w), int(y + h)))
        return out

    return _segs(hmask, False), _segs(vmask, True)


def _faint_h_rules(image_rgb, vlines, faint_thresh=238.0):
    """Recover VERY light horizontal rules the dark-cutoff pass misses. Within each vertical
    BAND (between adjacent vertical rules / page edges), a rule is a THIN row where almost
    the whole band is at least faintly inked. Text is sparse per row so it never fills a
    band; a filled block is too many rows tall. Returns ``(x0, y0, x1, y1)`` boxes."""
    gray = image_rgb.mean(2) if image_rgb.ndim == 3 else image_rgb.astype(float)
    H, W = gray.shape
    faint = gray < faint_thresh
    xs = sorted(set([0, W - 1] + [int((x0 + x1) / 2) for (x0, _y0, x1, _y1) in vlines]))
    out = []
    for xa, xb in zip(xs, xs[1:]):
        a, b = xa + 2, xb - 2                   # tiny inset; adjacent bands stay touching so
        if b - a < 80:                          # a rule split by a SHORT table rule rejoins
            continue
        frac = faint[:, a:b].mean(axis=1)       # per-row inked fraction within the band
        y = 0
        while y < H:
            if frac[y] > 0.80:                  # almost the whole band is inked on this row
                y0 = y
                while y < H and frac[y] > 0.55:
                    y += 1
                if 1 <= (y - y0) <= 8:          # THIN -> a rule, not a text line / fill block
                    yc = (y0 + y) // 2
                    out.append((a, yc - 1, b, yc + 1))
            else:
                y += 1
    return out


def _follow(present, lo, hi, max_gap):
    """Walk a boolean presence profile outward from [lo, hi], bridging gaps up to
    ``max_gap``, returning the run's full bounds. Stops at sustained absence."""
    a = int(lo)
    while a > 0:
        win = present[max(0, a - max_gap):a]
        nz = np.flatnonzero(win)
        if nz.size == 0:
            break
        a = max(0, a - max_gap) + int(nz[0])
    b, n = int(hi), len(present)
    while b < n:
        win = present[b:min(n, b + max_gap)]
        nz = np.flatnonzero(win)
        if nz.size == 0:
            break
        b = b + int(nz[-1]) + 1
    return a, b


def _faint_full(image_rgb, vertical, min_peak=0.85, max_band=16,
                max_gap=40, faint_thresh=238.0):
    """Detect page-spanning FAINT rules the dark pass misses (a rule dark in one cell and
    faint across the rest reads as a 'box'). The clean discriminator, measured: a full-span
    rule is a THIN band (<= ~16px) in which almost the whole span is at least faintly inked
    (peak fraction > ~0.85), whereas a text block is either THICK or fills far less of the
    span. So this fires only on real full-span rules -- never on text -- and returns each at
    its true extent (the faint run, small gaps bridged). ``vertical`` scans columns for
    full-height rules; otherwise rows for full-width rules."""
    gray = image_rgb.mean(2) if image_rgb.ndim == 3 else image_rgb.astype(float)
    H, W = gray.shape
    faint = gray < faint_thresh
    if vertical:
        m = max(20, int(H * 0.04))
        frac = faint[m:H - m, :].mean(0)        # per-column inked fraction over the height
        span = H
    else:
        m = max(20, int(W * 0.04))
        frac = faint[:, m:W - m].mean(1)        # per-row inked fraction over the width
        span = W
    out = []
    i = 0
    while i < len(frac):
        if frac[i] > 0.55:
            i0 = i
            while i < len(frac) and frac[i] > 0.40:
                i += 1
            if (i - i0) <= max_band and frac[i0:i].max() >= min_peak:
                c = i0 + int(np.argmax(frac[i0:i]))       # the band's strongest line
                if vertical:
                    prof = (faint[:, max(0, c - 1):c + 2]).max(1)
                else:
                    prof = (faint[max(0, c - 1):c + 2, :]).max(0)
                a, b = _follow(prof, span // 2 - 1, span // 2 + 1, max_gap)
                out.append((c - 1, a, c + 1, b) if vertical else (a, c - 1, b, c + 1))
        else:
            i += 1
    return out


def _faint_v_dividers(image_rgb, faint_thresh=238.0, iso_gap=20):
    """Detect short FAINT vertical dividers (e.g. a row's column separators) that are too
    light for the dark pass and too short for the length filter. The discriminator is
    ISOLATION: a real rule is a thin vertical strip with WHITE on both sides, whereas a
    letter stroke has the rest of its glyph beside it. A faint vertical OPEN finds long thin
    runs; keeping only the isolated ones yields the dividers without the letter strokes."""
    import cv2
    gray = image_rgb.mean(2) if image_rgb.ndim == 3 else image_rgb.astype(float)
    H, W = gray.shape
    faint = (gray < faint_thresh).astype(np.uint8)
    vk = max(60, H // 26)                                   # min run a divider must survive
    vmask = cv2.morphologyEx(
        faint, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (1, vk)))
    n, _lbl, stats, _cen = cv2.connectedComponentsWithStats(vmask, 8)
    out = []
    for i in range(1, n):
        x, y, w, h, _a = stats[i]
        if w > 14 or h < vk:                                # must be thin and long
            continue
        xc = x + w // 2
        lo, hi = xc - iso_gap, xc + iso_gap
        if lo < 0 or hi >= W:
            continue
        left = (gray[y:y + h, lo] < faint_thresh).mean()    # neighbours mostly white?
        right = (gray[y:y + h, hi] < faint_thresh).mean()
        if left < 0.30 and right < 0.30:
            out.append((xc - 1, int(y), xc + 1, int(y + h)))
    return out


def _refine(lines, perp, vertical):
    """Snap each line's ends to the perpendicular rules that cross it (completing a rule
    faint near a crossing) and drop a line that fills too little of its cell (a stray stub).
    ``perp`` are the perpendicular lines; ``vertical`` is True when refining vertical rules
    (so the line runs along Y and crosses horizontal rules)."""
    if not lines:
        return []
    # Perpendicular rules as (center_along_this_axis, span_lo, span_hi).
    if vertical:
        cross = [((y0 + y1) / 2.0, min(x0, x1), max(x0, x1)) for (x0, y0, x1, y1) in perp]
    else:
        cross = [((x0 + x1) / 2.0, min(y0, y1), max(y0, y1)) for (x0, y0, x1, y1) in perp]
    out = []
    for (x0, y0, x1, y1) in lines:
        if vertical:
            pos, a, b, thick = (x0 + x1) / 2.0, y0, y1, x1 - x0
        else:
            pos, a, b, thick = (y0 + y1) / 2.0, x0, x1, y1 - y0
        # Centers of perpendicular rules that actually cross this line's position.
        cs = sorted(c for (c, lo, hi) in cross if lo - _SNAP_TOL <= pos <= hi + _SNAP_TOL)
        if len(cs) >= 2:
            left = min(cs, key=lambda c: abs(c - a))
            right = min(cs, key=lambda c: abs(c - b))
            if left == right:                         # both ends hug one rule -> stub
                continue
            if left > right:
                left, right = right, left
            cell = right - left
            if cell <= 0 or (b - a) / cell < _MIN_COVER:
                continue                              # covers too little of the cell
            a, b = left, right                        # complete to the cell edges
        if vertical:
            out.append((x0, int(round(a)), x1, int(round(b))))
        else:
            out.append((int(round(a)), y0, int(round(b)), y1))
    return out


def _dedupe(lines, vertical):
    """Drop duplicate / overlapping collinear lines left after snapping."""
    out = []
    for ln in sorted(lines, key=lambda l: ((l[1] + l[3]) if not vertical else (l[0] + l[2]))):
        c = (ln[1] + ln[3]) / 2.0 if not vertical else (ln[0] + ln[2]) / 2.0
        lo, hi = (ln[0], ln[2]) if not vertical else (ln[1], ln[3])
        merged = False
        for i, ex in enumerate(out):
            ec = (ex[1] + ex[3]) / 2.0 if not vertical else (ex[0] + ex[2]) / 2.0
            elo, ehi = (ex[0], ex[2]) if not vertical else (ex[1], ex[3])
            if abs(c - ec) <= 12 and not (hi < elo - 8 or lo > ehi + 8):
                nlo, nhi = min(lo, elo), max(hi, ehi)
                out[i] = ((nlo, ex[1], nhi, ex[3]) if not vertical
                          else (ex[0], nlo, ex[2], nhi))
                merged = True
                break
        if not merged:
            out.append(ln)
    return out


def detect_borders(image_rgb: "np.ndarray", min_frac: float = 0.03,
                   dark_thresh: float = 205.0, min_len: float = 0.0) -> "tuple[list, list]":
    """Detect rule lines on a page raster. Returns ``(h_lines, v_lines)`` where each is a
    list of ``(x0, y0, x1, y1)`` bounding boxes in the SAME pixel frame as ``image_rgb``.

    ``min_len`` (px) drops any line shorter than it -- pass ~2.5x the page's text-line
    height so a letter stroke or a short underline (about one line tall) is never read as a
    rule, while genuine short cell rules on a small-text grid survive. SCALE-AWARE: tied to
    the text size, not a fixed fraction, so it holds across a big dark grid and a large-text
    card alike."""
    if image_rgb is None or image_rgb.size == 0:
        return [], []
    hl, vl = _raw_lines(image_rgb, min_frac, dark_thresh)
    # Per-band local faint rules merge + snap with the dark rules.
    hl = hl + _dedupe(_faint_h_rules(image_rgb, vl), vertical=False)
    hl2 = _refine(hl, vl, vertical=False)            # snap H ends to crossing V rules
    vl2 = _refine(vl, hl, vertical=True)             # snap V ends to crossing H rules
    # PAGE-SPANNING faint rules are already at their true extent -- add them AFTER refine so
    # it cannot snap them back inward to a cell boundary; dedupe then unions a boxed cell
    # rule with its full-span twin, yielding the full-width / full-height line.
    hl2 = hl2 + _faint_full(image_rgb, vertical=False)
    vl2 = vl2 + _faint_full(image_rgb, vertical=True)
    hl, vl = _dedupe(hl2, False), _dedupe(vl2, True)
    if min_len > 0:
        hl = [r for r in hl if (r[2] - r[0]) >= min_len]
        vl = [r for r in vl if (r[3] - r[1]) >= min_len]
    # Short ISOLATED faint dividers (a row's column separators): validated by isolation, not
    # length, so they are exempt from min_len. Connect each end to a horizontal rule within a
    # small gap so they join the grid (e.g. up to the row's bottom line).
    div = _faint_v_dividers(image_rgb)
    if div:
        h_ys = sorted((y0 + y1) / 2.0 for (_x0, y0, _x1, y1) in hl)
        snapped = []
        for (x0, y0, x1, y1) in div:
            connected = False
            for hy in h_ys:                          # an end within reach of a rule -> snap
                if y0 - 25 <= hy <= y0 + 25:
                    y0 = hy
                    connected = True
                if y1 - 25 <= hy <= y1 + 25:
                    y1 = hy
                    connected = True
            if connected:                            # keep only grid-joined dividers (a
                snapped.append((x0, int(y0), x1, int(y1)))   # floating logo edge is dropped)
        vl = _dedupe(vl + snapped, True)
    return hl, vl
