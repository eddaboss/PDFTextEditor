"""Make an EDIT to a scanned page look like it was always part of the scan.

The OCR engine already gives us editable text in a font built from the page's own
glyphs (``reconstruct.py``), so the SHAPE of an edit is right. What it lacks is the
two things that otherwise give an edit away: the wrong INK COLOUR (the scan-built
font draws flat black) and the absence of the scanner's per-letter DEGRADATION (the
edit is crisp while its neighbours are broken/faded). This module supplies both,
deterministically -- no ML, no font bank, no shipped assets:

  COLOUR : recover the document's ink and paper from the scan under a stroke mask
           (sampling under the strokes preserves hue; a global "darkest pixels"
           estimate gets hijacked by paper speckle and washes colour to grey).
  DAMAGE : a hard, stroke-aware, clumped per-pixel filter calibrated to the LOCAL
           neighbours' severity. Every output pixel is ink / grey / paper -- never a
           smooth gradient (a smooth blend reads as fake). Fresh randomness per edit,
           so no two edited letters share a damage pattern.

Pure numpy + OpenCV; safe to run on the OCR worker thread.
"""
from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np


# --------------------------------------------------------------------------- #
#  colour
# --------------------------------------------------------------------------- #
def sample_ink_paper(scan_rgb: np.ndarray, ink_mask: np.ndarray | None = None,
                     paper_frac: float = 0.40, ink_frac: float = 0.30):
    """Recover (ink_rgb, paper_rgb) from the scan. INK is taken from the SOLID-INK CORES
    (the deep stroke interior, high coverage), which GATES the white dropout specks inside
    the letters AND the light antialiased edge pixels. Both pull a plain darkest-fraction
    mean light, so a faxy glyph's ink reads as mid-grey instead of its true dark core (the
    edited glyph then looks too faint). ``ink_mask`` (the OCR'd stroke render) narrows the
    core search to the strokes when available; otherwise the cores are found by coverage."""
    flat = scan_rgb.reshape(-1, 3).astype(np.float32)
    g = flat.mean(1)
    order = np.argsort(g)
    n = len(g)
    paper = np.median(flat[order[int(n * (1 - paper_frac)):]], axis=0)
    # coverage vs the scan's own paper/darkest: the high-coverage pixels are the solid ink.
    gimg = scan_rgb.mean(2).astype(np.float32)
    dk = float(np.percentile(gimg, 3))
    cov = np.clip((paper.mean() - gimg) / max(paper.mean() - dk, 1e-3), 0.0, 1.0)
    core = cov > 0.7
    if ink_mask is not None:
        core = core & ink_mask
    if int(np.count_nonzero(core)) < 10:
        core = cov > 0.5
        if ink_mask is not None:
            core = core & ink_mask
    if int(np.count_nonzero(core)) >= 5:
        # DEEPEST HALF of the cores, not their median: the cov>0.7 set still includes the
        # 0.7-coverage rim, whose lighter pixels drag the median up to a mid-grey -- so the synth's
        # ink never reaches the real stroke's dark peak and reads grey next to it. Take the darker
        # half of the core pixels for the true deep-stroke colour. Self-calibrating: a faint scan's
        # cores are all faint, so its ink stays faint.
        cpx = flat[core.reshape(-1)]
        o = np.argsort(cpx.mean(1))
        ink = np.median(cpx[o[:max(1, len(o) // 2)]], axis=0)
    elif ink_mask is not None and ink_mask.sum() >= 20:
        px = scan_rgb[ink_mask].astype(np.float32)
        o = np.argsort(px.mean(1))
        ink = np.median(px[o[:max(1, int(len(px) * ink_frac))]], axis=0)
    else:
        thr = paper.mean() * 0.6
        ink_px = flat[g < thr]
        ink = np.median(ink_px if len(ink_px) >= 20 else flat[order[:max(1, n // 100)]], axis=0)
    return ink.astype(np.float32), paper.astype(np.float32)


def coverage(rgb: np.ndarray) -> np.ndarray:
    """Ink coverage in 0..1 (1 = full ink), normalised to the image's own ink/paper
    levels so it is independent of the source colour."""
    g = rgb.mean(2).astype(np.float32) if rgb.ndim == 3 else rgb.astype(np.float32)
    p, ink = np.percentile(g, 90), np.percentile(g, 5)
    return np.clip((p - g) / max(p - ink, 1e-3), 0.0, 1.0)


# --------------------------------------------------------------------------- #
#  damage filter
# --------------------------------------------------------------------------- #
@dataclass
class Style:
    """Qualitative knobs of the damage filter. Defaults reproduce the tuned toner-
    failure look; only ``sev`` (the spatial intensity, passed separately) normally
    varies per region. These could later be measured per page (grey-fraction, edge
    sharpness, dropout clumpiness) for page-adaptive style, still without any model."""
    edge_thin: float = 1.0      # ragged stroke-edge removal weight
    break_rate: float = 1.0     # clumped across-stroke break weight
    clump_cell: float = 3.0     # break-clump size (px); bigger = chunkier holes
    grey_rate: float = 1.0      # grey half-ink fringe weight
    stray_rate: float = 1.0     # stray-ink scatter weight
    blur: float = 0.3           # gaussian MTF sigma
    noise: float = -1.0         # read-noise std; <0 => derive from sev


def _field(shape, R, cell):
    """Smooth low-frequency random field in 0..1 with clumps ~cell px."""
    h, w = shape
    sh, sw = max(2, h // cell), max(2, w // cell)
    small = R.rand(sh, sw).astype(np.float32)
    return np.clip(cv2.resize(small, (w, h), interpolation=cv2.INTER_LINEAR), 0, 1)


def hard_degrade(clean_rgb, ink, paper, sev, R, style: "Style | None" = None):
    """Apply hard, STROKE-AWARE pixel damage to a clean RGB image. Toner thins
    raggedly at stroke EDGES (removal weighted by distance-from-edge) and breaks
    across strokes in CLUMPS. ``sev`` is a scalar or HxW map in 0..1. Returns
    hard-pixel uint8 (every pixel ink / grey / paper)."""
    st = style if style is not None else Style()
    cell = max(2, int(round(st.clump_cell)))
    M = coverage(clean_rgb) > 0.45
    H, W = M.shape
    sevmap = np.full((H, W), float(sev), np.float32) if np.isscalar(sev) else sev.astype(np.float32)
    ink_b = np.broadcast_to(ink.reshape(1, 1, 3), clean_rgb.shape)
    paper_b = np.broadcast_to(paper.reshape(1, 1, 3), clean_rgb.shape)
    grey_b = np.broadcast_to((ink * 0.5 + paper * 0.5).reshape(1, 1, 3), clean_rgb.shape)
    out = paper_b.astype(np.float32).copy()

    depth = cv2.distanceTransform(M.astype(np.uint8), cv2.DIST_L2, 3)
    depthn = depth / max(depth.max(), 1.0)
    p_edge = np.clip(sevmap * (1.35 - 1.1 * depthn) * st.edge_thin, 0.0, 0.97)
    clump = _field((H, W), R, cell=cell)
    gaps = clump < (sevmap * 0.5 * st.break_rate * (1.2 - 0.4 * depthn))
    removed = M & ((R.rand(H, W) < p_edge) | gaps)

    greyf = R.rand(H, W).astype(np.float32)
    greypix = M & ~removed & (depthn < 0.5) & (greyf < sevmap * 0.65 * st.grey_rate)
    inkpix = M & ~removed & ~greypix
    out[inkpix] = ink_b[inkpix]
    out[greypix] = grey_b[greypix]

    Mu = M.astype(np.uint8)
    outside = (cv2.dilate(Mu, np.ones((2, 2), np.uint8)) > 0) & ~M
    stray = outside & (R.rand(H, W) < sevmap * 0.10 * st.stray_rate)
    out[stray] = ink_b[stray]

    if st.blur > 0:
        out = cv2.GaussianBlur(out, (0, 0), st.blur)
    nz = st.noise if st.noise >= 0 else (2.0 + 4.0 * float(np.mean(sevmap)))
    out += R.randn(H, W, 3).astype(np.float32) * nz
    return np.clip(out, 0, 255).astype(np.uint8)


def local_severity(scan_rgb, ink_mask, box, pad=46):
    """Measure how degraded the existing text is in a window around ``box`` (the
    local neighbours), as a dropout fraction in 0..1. This is the signal that drives
    the per-edit filter so an edit matches its neighbours, not a file-wide average."""
    x0, y0, x1, y1 = box
    H, W = scan_rgb.shape[:2]
    Y0, Y1, X0, X1 = max(0, y0 - pad), min(H, y1 + pad), max(0, x0 - pad), min(W, x1 + pad)
    m = ink_mask[Y0:Y1, X0:X1]
    s = scan_rgb[Y0:Y1, X0:X1]
    core = cv2.erode(m.astype(np.uint8), np.ones((2, 2), np.uint8)) > 0
    use = core if core.sum() > 30 else m
    if use.sum() < 10:
        return 0.3
    _, paper = sample_ink_paper(scan_rgb, ink_mask)
    norm = s[use].astype(np.float32).mean(1) / max(paper.mean(), 1.0)
    return float(np.clip(np.mean(norm > 0.62), 0.05, 0.85))


# --------------------------------------------------------------------------- #
#  the edit entry point used by the render/save seam
# --------------------------------------------------------------------------- #
def degrade_patch(clean_text_rgb, ink, paper, severity, seed, style: "Style | None" = None):
    """Colour + damage one rendered edit patch. ``clean_text_rgb`` is the new text
    rendered (any neutral colour on paper) at the patch size; ``ink``/``paper`` are
    recovered from the scan; ``severity`` is the local dropout level. Returns a
    hard-pixel uint8 RGB patch ready to composite over the page."""
    return hard_degrade(clean_text_rgb.astype(np.float32),
                        np.asarray(ink, np.float32), np.asarray(paper, np.float32),
                        float(severity), np.random.RandomState(int(seed) & 0x7FFFFFFF), style)


# --------------------------------------------------------------------------- #
#  map-residual degradation MATCH (measure the neighbours, copy their damage)
# --------------------------------------------------------------------------- #
def measure_damage(region_rgb, geom=None):
    """Measure the LOCAL toner damage of the REAL scanned glyphs around an edit, so a
    synthesized glyph can be damaged the SAME way (Edward's map-residual idea), PIXEL BY
    PIXEL -- never a smooth gradient. Two measurements, both from ``coverage`` (the inverse
    of the binary map's push-to-white residual):
      interior  the neighbours' per-pixel ink levels in the stroke INTERIOR (eroded, so
                the antialiased rim is not counted) -- the within-glyph dither.
      border    coverage keyed by pixel-DISTANCE outside the glyph edge (1..4 px). The
                residual around a glyph is NOT uniform: dense aliasing right at the edge,
                faint specks falling off with distance, clean far away. The synth later
                draws its border pixels from the SAME distance bands, so the speckle hugs
                the glyph the way the scan's does.
    A clean glyph's interior is solid ink, so a clean line returns None (synth stays
    crisp). Speckle is edge-relative, so it is never scattered randomly across the page."""
    cov = coverage(region_rgb)
    H, W = cov.shape
    mask = np.zeros((H, W), bool)
    boxes = (geom.get("char_boxes") or geom.get("boxes")) if geom else None
    if boxes:
        for b in boxes:
            x0, y0, x1, y1 = (int(round(float(v))) for v in b[:4])
            if x1 > x0 and y1 > y0:
                mask[max(0, y0):min(H, y1), max(0, x0):min(W, x1)] = True
    else:
        mask[:] = True
    ink = (mask & (cov > 0.45)).astype(np.uint8)
    if int(ink.sum()) < 40:
        return None
    k3 = np.ones((3, 3), np.uint8)
    solid = cv2.morphologyEx(ink, cv2.MORPH_CLOSE, k3)          # fill dropout holes
    interior = (cv2.erode(solid, k3) > 0) & mask                # drop the antialiased rim
    iv = cov[interior]
    if iv.size < 40:
        iv = cov[(solid > 0) & mask]                            # thin strokes: keep all
        if iv.size < 20:
            return None
    iv = np.clip(iv, 0.0, 1.0).astype(np.float32)
    # Skip damage ONLY for a genuinely pristine interior (a clean DIGITAL glyph: ink is
    # essentially solid at full strength). A faxed glyph -- even one whose interior reads
    # "solid" by a loose bar -- actually sits a notch BELOW full ink with a soft, textured
    # body and edges; reproducing that ~0.95 level + its spread is what keeps a synth glyph
    # from dropping a too-dark, too-sharp "perfect letter" onto a faxed line. The old bar
    # (mean>0.9, <6% below 0.6) treated a light fax as clean and bailed to the crisp recolour.
    if float(iv.mean()) > 0.985 and float(np.mean(iv < 0.85)) < 0.01:
        return None

    def _sub(a):                                                # cap, deterministic stride
        a = np.asarray(a, np.float32)
        return a[:: a.size // 2048][:2048] if a.size > 2048 else a

    # DARK/SHARP-EDGE detection (an OPTION, not the default): some faxes pool toner at the
    # stroke edge so the body stays near-solid right up to the boundary and then drops off
    # fast -- a sharp DARK edge -- instead of fading out softly. Measure the rim (1px inside
    # the ink). When the rim is near-solid AND falls off hard to the d=1 halo just outside,
    # the scan's edge is dark, and the synth should KEEP its font's own (darker) edge rather
    # than lightening it. When the rim is soft, leave the default soft-edge path alone.
    din = cv2.distanceTransform(solid, cv2.DIST_L2, 3)
    rim = (din >= 0.5) & (din < 1.5) & mask
    rim_cov = float(cov[rim].mean()) if rim.any() else 0.0

    # BORDER profile: coverage at each pixel distance (1..4) OUTSIDE the real glyph ink,
    # measured only NEAR the glyphs. d=1 holds the edge aliasing, d=2..4 the faint specks
    # that taper off; beyond that the paper is clean. The synth reproduces this per band.
    dout = cv2.distanceTransform((ink == 0).astype(np.uint8), cv2.DIST_L2, 3)
    near = cv2.dilate(ink, np.ones((11, 11), np.uint8)) > 0
    border = {}
    for d in (1, 2, 3, 4):
        vals = cov[(np.abs(dout - d) <= 0.5) & near]
        if vals.size >= 8:
            border[int(d)] = _sub(np.clip(vals, 0.0, 1.0))
    # Three criteria so a SOFT edge can't trip it (the option must be conditional): the rim is
    # near-solid (>0.78), it falls hard to the d=1 halo (drop >0.50), AND the halo itself dies
    # fast (d2 << d1) -- a gradual/soft edge keeps d2 close to d1 and a lower rim, so it stays
    # on the default soft path. Measured: LEE sharp rim .86/drop .66/(d2/d1) .09 -> True;
    # a soft glyph rim .67/drop .40/(d2/d1) .28 -> False.
    d1m = float(np.mean(border[1])) if 1 in border else 0.0
    d2m = float(np.mean(border[2])) if 2 in border else 0.0
    edge_dark = bool(rim_cov > 0.78 and (rim_cov - d1m) > 0.50
                     and d2m <= 0.18 * max(d1m, 0.01))
    return {"interior": _sub(iv), "border": border, "edge_dark": edge_dark}


def build_residual_filter(region_rgb, geom=None):
    """Edward's INVERTED-MAP residual. Normalise the line so ink->1 and paper->0 (coverage):
    that value IS the per-pixel push-distance between pure paper and pure ink -- how far each
    pixel had to move to go pure, which is exactly the degradation at that pixel. Collect those
    coverage values by SIGNED DISTANCE from the hole-filled stroke edge: deep interior holds
    the ink's fade/dropouts, the edge band holds the soft/jagged transition, just outside holds
    the speckle. Reproducing this distribution per distance recreates the real fade + broken
    edges + speckle on a new letter. Returns {distance: coverage_samples} or None (clean line)."""
    cov = coverage(region_rgb)
    H, W = cov.shape
    mask = np.zeros((H, W), bool)
    boxes = (geom.get("char_boxes") or geom.get("boxes")) if geom else None
    if boxes:
        for b in boxes:
            x0, y0, x1, y1 = (int(round(float(v))) for v in b[:4])
            if x1 > x0 and y1 > y0:
                mask[max(0, y0):min(H, y1), max(0, x0):min(W, x1)] = True
    else:
        mask[:] = True
    inkm = (cov > 0.5) & mask
    if int(inkm.sum()) < 40:
        return None
    solid = cv2.morphologyEx(inkm.astype(np.uint8), cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8)) > 0
    din = cv2.distanceTransform(solid.astype(np.uint8), cv2.DIST_L2, 3)
    dout = cv2.distanceTransform((~solid).astype(np.uint8), cv2.DIST_L2, 3)
    sd = np.rint(np.where(solid, din, -dout)).astype(np.int32)
    near = cv2.dilate(mask.astype(np.uint8), np.ones((7, 7), np.uint8)) > 0
    prof = {}
    for d in range(-2, 14):                          # include the immediate OUTSIDE (-1,-2): the
        band = (sd == d) & near                       # hard black pixels that protrude past the edge
        if int(band.sum()) >= 10:                     # are the scan's jaggedness == the "speckle"
            s = np.clip(cov[band], 0.0, 1.0).astype(np.float32)
            if s.size > 1500:
                s = s[:: s.size // 1500][:1500]
            prof[int(d)] = s
    if not prof:
        return None
    # PAPER GRAIN: the real paper is NOT flat white -- EVERY pixel carries a faint grain (mostly
    # ~0 coverage, some up to ~0.15). The synth strip paints flat 255, which reads as a whiteout
    # block next to the grainy real paper. Pool the surrounding paper's coverage so the synth can
    # lay the SAME per-pixel grain on its background.
    # FAR paper only (dout >= 3): the 1-2px stroke halo is grainy antialiasing spillover; pooling
    # it made the synth lay grain on ~every bg pixel (4x the real paper's grain). The far paper is
    # the true background texture.
    pg = cov[(cov < 0.30) & (~solid) & (dout >= 3.0)]
    bg = np.clip(pg, 0.0, 0.4).astype(np.float32) if pg.size >= 100 else None
    if bg is not None and bg.size > 5000:
        bg = bg[:: bg.size // 5000][:5000]
    # GRAIN RATE: how OFTEN the real far paper actually carries grain, so the synth lays grain at
    # the same frequency instead of on every background pixel (the over-speckle).
    grain_rate = float((pg > 0.08).mean()) if (pg is not None and pg.size >= 100) else 0.0
    # HARD DROPOUT: the scan punches sharp WHITE pixels clean through solid ink -- distinct from
    # the soft coherent fade. Measure that rate inside the solid strokes so the synth reproduces
    # it (per-pixel sharp knockouts), the way every other channel is measured-then-stamped.
    inside = solid & mask
    ni = int(inside.sum())
    hard_drop = float(((cov < 0.12) & inside).sum()) / ni if ni >= 20 else 0.0
    # SHARPNESS: the real ink is crisp -- coverage sits near 0 or near 1, few mid-greys. A soft
    # synth (grey antialiased ramps) reads heavier and confuses weight matching. Measure how few
    # mid-tones the scan has so the synth can be hardened to the same crispness.
    inked = cov[(cov > 0.15) & near]
    mid_frac = float(((inked >= 0.3) & (inked <= 0.7)).mean()) if inked.size >= 20 else 0.5
    hard_edge = float(np.clip(1.0 - 2.0 * mid_frac, 0.0, 1.0))
    # TARGET DARKNESS: the mean coverage over the real STROKE pixels = how dark/faint the
    # neighbours' ink actually is. The synth glyph is scaled to this so it reads the same colour
    # even when the matched font is heavier than the (often faint) scanned digits.
    strokes = cov[(cov > 0.30) & mask]
    target = float(strokes.mean()) if strokes.size >= 20 else None
    # CORE DARKNESS: the scan's SOLID ink (its deep stroke interior) is much darker than its
    # stroke MEAN -- the antialiased edges pull the mean light. Matching only the mean leaves
    # the synth flat-grey with no dark core, so it reads lighter than the scan even at the same
    # mean. Capture the core (a high percentile of the stroke coverage) so the synth's solid
    # pixels can be floored to it; it ADAPTS (a faint scan has a faint core too).
    core = float(np.percentile(strokes, 80)) if strokes.size >= 20 else None
    return {"bands": prof, "bg": bg, "target": target, "core": core,
            "grain_rate": grain_rate, "hard_drop": hard_drop, "hard_edge": hard_edge}


def apply_residual_filter(clean_strip_rgb, ink, paper, prof, R):
    """Stamp the inverted-map residual onto a CLEAN synth letter. For each pixel take its
    SIGNED DISTANCE from the letter edge and DRAW a coverage value from the real scan's
    distribution at that distance, so the synth fades, breaks and speckles per pixel exactly
    like its neighbours. Composite to RGB at the recovered ink/paper (grey where the scan is
    grey -- the degradation lives in the residual, not a hard binary)."""
    ink = np.asarray(ink, np.float32).reshape(1, 1, 3)
    paper = np.asarray(paper, np.float32).reshape(1, 1, 3)
    bands = prof.get("bands") if isinstance(prof, dict) else prof
    bg = prof.get("bg") if isinstance(prof, dict) else None
    he = prof.get("hard_edge") if isinstance(prof, dict) else None
    g = clean_strip_rgb.mean(2) if clean_strip_rgb.ndim == 3 else clean_strip_rgb
    base = np.clip((255.0 - g) / 255.0, 0.0, 1.0)
    solid = base > 0.45
    H, W = solid.shape
    if not bands or not solid.any():
        c3 = base[..., None]
        return np.clip(paper * (1 - c3) + ink * c3, 0, 255).astype(np.uint8)
    din = cv2.distanceTransform(solid.astype(np.uint8), cv2.DIST_L2, 3)
    dout = cv2.distanceTransform((~solid).astype(np.uint8), cv2.DIST_L2, 3)
    sd = np.rint(np.where(solid, din, -dout)).astype(np.int32)
    ds = sorted(bands)
    dmin, dmax = ds[0], ds[-1]
    inter = [d for d in ds if d >= 1]
    solid_d = max(inter, key=lambda d: float(bands[d].mean())) if inter else dmax
    cov2 = np.zeros((H, W), np.float32)
    # Coherent field for the SOLID CORE only; the edge is laid SCATTERED (hard 1px jaggedness).
    # cell=3 (was 2): the interior mottle patches are COARSER, so they survive the downsample
    # to normal viewing zoom instead of averaging back to flat grey (the synth read too clean
    # next to the scan's coarse fax breaks at 1x-2.5x view; only visible at 8x+).
    fld = _field((H, W), R, cell=3)
    fld = np.clip(fld + (R.rand(H, W).astype(np.float32) - 0.5) * 0.12, 0.0, 1.0)
    for d in range(dmin, dmax + 1):
        band = sd == d
        nb = int(band.sum())
        if not nb:
            continue
        src = d if d <= solid_d else solid_d        # past the solid band, reuse it (stay solid)
        s = bands.get(src)
        if s is None:
            s = bands[min(ds, key=lambda k: abs(k - src))]
        if d <= 0:
            # OUTSIDE the letterform / boundary: keep ONLY the hard protruding spikes (high
            # samples) and DROP the continuous low-grey antialiasing rim -- that rim is what
            # widens every stroke and bolds it. Result: sparse hard black specks protruding past
            # the edge (the speckle), no added width. This is measured per line, so a clean scan
            # yields few spikes and a faxy scan yields many.
            samp = s[R.randint(0, s.size, nb)]
            samp = np.where(samp >= 0.45, samp, 0.0)
            if he:                                  # crisp scan -> shed most protruding spikes so a
                samp = np.where(R.rand(nb) < (1.0 - float(he)), samp, 0.0)  # clean digit is not furry
        elif d == 1:
            # inner edge, scattered: white bites raggedly INTO the stroke (no added width).
            samp = s[R.randint(0, s.size, nb)]
        else:
            # SOLID CORE: coherent-field sample of the real interior distribution. A gentle floor
            # (25th pct) keeps the coherent field from opening holes in a thick stroke WITHOUT
            # darkening it -- the old 55th-pct floor pushed the interior darker than the real
            # (faded/grey) ink, which read as the synth being a darker colour than its neighbours.
            ss = np.sort(s)
            samp = ss[np.clip((fld[band] * (ss.size - 1)).astype(np.int64), 0, ss.size - 1)]
            # NO floor: reproduce the real interior coverage distribution as-is so the synth is as
            # FAINT (or dark) as its neighbours. The coherent field keeps low draws clumped (a few
            # grey patches like the real faded ink), not salt-and-pepper holes. A floor here clipped
            # the light/faded pixels and made the synth read as a darker colour than the scan.
        cov2[band] = samp
    deep = sd > dmax
    if deep.any():                                  # thick interior keeps the solid level
        ss = np.sort(bands[solid_d])
        cov2[deep] = ss[np.clip((fld[deep] * (ss.size - 1)).astype(np.int64), 0, ss.size - 1)]
    # DARKNESS MATCH: scale the WHOLE synth glyph so its mean stroke coverage equals the real
    # neighbours' (the measured ``target``). This makes the synth exactly as faint/dark as the
    # scan it sits in -- correcting both the deep-interior over-darkening and a matched font that
    # is heavier than the (often faint) real ink, so a synth digit never reads as a darker colour.
    target = prof.get("target") if isinstance(prof, dict) else None
    glyph = cov2 > 0.05
    if target and glyph.any():
        cur = float(cov2[glyph].mean())
        if cur > 1e-3:
            cov2[glyph] *= np.clip(float(target) / cur, 0.4, 1.3)
    # CORE FLOOR: the band fill + mean match leave the synth's SOLID ink flat-grey; floor the
    # solid-core pixels up to the scan's dark core so the synth's deep ink reads as dark as the
    # scan's (a high percentile, so it never exceeds the real ink and adapts to a faint scan).
    core = prof.get("core") if isinstance(prof, dict) else None
    if core and target and core > target + 0.02:
        # >0.85 (was >0.55): floor ONLY the already-darkest pixels up to the scan's core. The old
        # 0.55 gate darkened the whole interior to near-solid, erasing the coarse toner-starvation
        # BREAKS that make the scan read as faxed -- the synth came out a uniform dark block that
        # looked "not degraded" at normal zoom. Leaving mid-tones (0.55-0.85) light keeps the breaks.
        solid = cov2 > 0.85
        if solid.any():
            cov2[solid] = np.maximum(cov2[solid],
                                     np.clip(float(core) - 0.06 + 0.10 * fld[solid], 0.0, 1.0))
    # EDGE CRISPNESS: the scan's measured crispness (``hard_edge``) was never applied, so the synth
    # keeps a furry mid-grey fringe of half-ink speckle around every stroke. On a crisp scan (a clean
    # digit) that fringe reads as roughness the real ink does not have -- and it is far more visible now
    # the ink is dark. Push coverage away from the visual threshold (0.5) in proportion to crispness:
    # sub-threshold fringe fades to paper (clean edge), solid ink stays solid. Width is preserved (the
    # 0.5 boundary barely moves); only the fringe below it is cleaned. Scaled by ``hard_edge`` so a
    # genuinely degraded/soft scan (faxy caps) barely moves and keeps its ragged look.
    if he and he > 0.05:
        cov2 = np.clip(0.5 + (cov2 - 0.5) * (1.0 + 1.3 * float(he)), 0.0, 1.0)
    # HARD DROPOUTS: the scan punches sharp WHITE pixels clean through solid ink. Reproduce them
    # at the measured ``hard_drop`` rate by knocking that fraction of the synth's solid pixels to
    # 0 -- per-pixel and sharp, unlike the soft coherent fade that the bands already supply.
    hard_drop = prof.get("hard_drop") if isinstance(prof, dict) else None
    if hard_drop and hard_drop > 0.01:
        knock = (cov2 > 0.45) & (R.rand(H, W) < float(hard_drop))
        cov2[knock] = 0.0
    # CONNECTIVITY: never fully SEVER a stroke. The bites/drops above rag the EDGES (the fax look
    # Edward wants), but on a 2-3px digit stroke or slash they can cut clean through and break the
    # glyph ("text getting cut"). Floor the clean stroke's MEDIAL RIDGE -- the 1px local-maxima
    # centreline of the interior distance transform -- to the glyph's own measured darkness. Only
    # ~1px per stroke, so the ragged edges are preserved; just the centre can't drop out. A faint
    # scan floors to its faint target, so its thin strokes still break like the real ink.
    if din is not None:
        _maxf = cv2.dilate(din, np.ones((3, 3), np.float32))
        ridge = (din >= _maxf) & (din >= 1.0)
        if ridge.any():
            _lvl = float(prof.get("target")) if (isinstance(prof, dict) and prof.get("target")) else 0.6
            cov2[ridge] = np.maximum(cov2[ridge], min(_lvl, 0.9))
    # PAPER GRAIN: every background pixel that isn't already inked gets a faint coverage drawn
    # from the real paper distribution, so the synth paper is grainy like the scan (no flat-255
    # whiteout block). Most draws are ~0, so it stays paper -- just never perfectly flat.
    if bg is not None and bg.size:
        plain = (sd < 0) & (cov2 < 0.02)
        npl = int(plain.sum())
        if npl:
            cov2[plain] = bg[R.randint(0, bg.size, npl)]
    c3 = np.clip(cov2, 0.0, 1.0)[..., None]
    out = paper * (1 - c3) + ink * c3
    return np.clip(out, 0, 255).astype(np.uint8)


def apply_measured_damage(strip_rgb, ink, paper, prof, R):
    """Damage a CLEAN synth strip to match a measured neighbour profile
    (``measure_damage``) by HARD, PER-PIXEL toner damage -- NOT a gradient. INTERIOR glyph
    pixels draw their ink level from the neighbours' interior samples (the within-glyph
    dither). BORDER pixels (just outside the glyph) draw from the neighbours' profile for
    THEIR pixel-distance from the edge, so the aliasing + speckle hugs the glyph and tapers
    off exactly like the scan; pixels beyond the measured bands stay clean paper. ``prof``
    is None on a clean line -> the plain crisp recolour. Returns hard-pixel uint8 RGB."""
    ink = np.asarray(ink, np.float32).reshape(1, 1, 3)
    paper = np.asarray(paper, np.float32).reshape(1, 1, 3)
    g = strip_rgb.mean(2) if strip_rgb.ndim == 3 else strip_rgb
    cov = np.clip((255.0 - g.astype(np.float32)) / 255.0, 0.0, 1.0)
    H, W = cov.shape
    M = cov > 0.45                                              # the glyph's own pixels
    interior = None if prof is None else prof.get("interior")
    interior = None if interior is None else np.asarray(interior, np.float32)
    if interior is None or interior.size < 8 or not M.any():
        c3 = cov[..., None]
        return np.clip(paper * (1 - c3) + ink * c3, 0, 255).astype(np.uint8)
    if prof.get("edge_dark"):
        cov2 = cov.copy()
        inr = cov > 0.80
        if inr.any():
            cov2[inr] = interior[R.randint(0, interior.size, int(inr.sum()))]
    else:
        retain = np.ones((H, W), np.float32)
        m = cov > 0.05
        retain[m] = interior[R.randint(0, interior.size, int(m.sum()))]
        cov2 = cov * retain
    # Faint OUTER specks beyond the glyph edge (d>=2 only; the font's soft edge already
    # supplies the d=1 aliasing, so reproducing d=1 too would double the rim).
    border = prof.get("border") or {}
    if border:
        dout = cv2.distanceTransform((~M).astype(np.uint8), cv2.DIST_L2, 3)
        dr = np.rint(dout).astype(np.int32)
        for d, samp in border.items():
            if int(d) < 2:
                continue
            samp = np.asarray(samp, np.float32)
            band = (dr == int(d)) & ~M
            nb = int(band.sum())
            if nb and samp.size:
                cov2[band] = np.maximum(cov2[band], samp[R.randint(0, samp.size, nb)])
    c3 = np.clip(cov2, 0.0, 1.0)[..., None]
    out = paper * (1 - c3) + ink * c3
    return np.clip(out, 0, 255).astype(np.uint8)
