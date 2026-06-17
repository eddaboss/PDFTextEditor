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
    """Recover (ink_rgb, paper_rgb) from the scan. ``ink_mask`` is a boolean of where
    the existing text strokes are (from the OCR'd-text render aligned to the scan);
    we take the darkest fraction WITHIN the strokes, which keeps the ink's hue and
    excludes paper noise. Without a mask we fall back to a darkness threshold."""
    flat = scan_rgb.reshape(-1, 3).astype(np.float32)
    g = flat.mean(1)
    order = np.argsort(g)
    n = len(g)
    paper = np.median(flat[order[int(n * (1 - paper_frac)):]], axis=0)
    if ink_mask is not None and ink_mask.sum() >= 20:
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
