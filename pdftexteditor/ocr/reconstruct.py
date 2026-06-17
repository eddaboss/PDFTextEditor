"""Turn a scanned page + its OCR lines into editable text boxes positioned on the
page, rendered in a MATCHED REAL FONT (0.3.0: no vtracer scan-built font).

Recognition -> per-word boxes (origin/size/cover) -> classify the document font
(serif / sans / mono) and pick a bundled family metric-matched to the common
document fonts (Tinos = Times, Arimo = Arial, Cousine = Courier) -> place invisible
editable text over the kept scan. When a word is edited the box flips visible and
the edit seam (document.py) recolors + hard-damages it to match the scan
(ocr/degrade.py), so the edit blends instead of drawing crisp black.

This replaces the old ClearScan-style scan-built font (vtracer): that produced
distorted glyphs and was the bulk of the "looks wrong" problem. Sizing is anchored
to the OCR box height (reliable), placement is one box per WORD (editing one word
never disturbs its neighbours), and the cover is the scanned-word rect painted in
the paper colour only once the word is edited. Pure numpy / cv2; runs off the GUI
thread.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field

import cv2
import numpy as np

from . import fontmatch
from .segment import segment_line

# Bundled families (in assets/fonts), metric-compatible with the three fonts most
# scanned documents use. Classification picks one; the engine embeds it normally.
_FAMILY = {"serif": "Tinos", "sans": "Arimo", "mono": "Cousine"}

_X_RATIO_SERIF = 0.45      # x-height / em (Times-class)
_X_RATIO_SANS = 0.52       # x-height / em (Helvetica-class)
_DEFAULT_GAP_EM = 0.06
_DEFAULT_SPACE_EM = 0.30
_BOX_EM_MIXED = 0.98       # box height / em for a mixed-case line
_BOX_EM_CAPS = 0.72        # box height / em for an all-caps line


@dataclass
class LineBox:
    """One reconstructed editable word: baseline ``origin`` (PDF points), the
    recognized ``text``, the estimated point ``size``, OCR ``confidence``, and
    ``cover`` -- the scanned word's rectangle in DISPLAY points (x0,y0,x1,y1),
    painted in ``bg`` (this word's OWN local background colour, not the page-wide
    paper tone) before the edited text is drawn."""

    origin: tuple
    text: str
    size: float
    confidence: float
    cover: tuple = ()
    bg: tuple = (1.0, 1.0, 1.0)


@dataclass
class ReconResult:
    """Everything the GUI thread needs to inject the OCR result. ``family`` is a
    bundled font family the boxes render in (no per-page custom font in 0.3.0, so
    ``otf_bytes`` is empty and the caller skips custom-face registration)."""

    otf_bytes: bytes
    family_name: str
    lines: list = field(default_factory=list)
    traced_chars: str = ""
    n_lines: int = 0
    bg_color: tuple = (1.0, 1.0, 1.0)


def _paper_color(image_rgb: np.ndarray) -> tuple:
    """The page's dominant light/paper colour (r,g,b in 0..1): the median of the
    bright pixels, so a cover rect blends into the scan."""
    flat = image_rgb.reshape(-1, 3).astype(np.float32)
    lum = flat.mean(axis=1)
    thresh = max(180.0, float(np.percentile(lum, 60)))
    bright = flat[lum > thresh]
    if bright.shape[0] == 0:
        bright = flat
    c = np.median(bright, axis=0)
    return (float(c[0] / 255), float(c[1] / 255), float(c[2] / 255))


def _local_paper_color(image_rgb: np.ndarray, x0: float, y0: float,
                       x1: float, y1: float, fallback: tuple) -> tuple:
    """The background colour right around ONE word: the median of the brighter
    pixels in its (padded) box, so an edited word's cover matches its OWN cell
    (white, light blue, ...) instead of the page-wide paper median, which on a
    mixed form paints every edit a single off-white. Dark glyphs are the minority
    in the box, so the top luminance band is the local background. Falls back to
    the page paper colour when the crop has too little to go on."""
    h, w = image_rgb.shape[:2]
    xi0, yi0 = max(0, int(x0)), max(0, int(y0))
    xi1, yi1 = min(w, int(x1) + 1), min(h, int(y1) + 1)
    if xi1 - xi0 < 2 or yi1 - yi0 < 2:
        return fallback
    crop = image_rgb[yi0:yi1, xi0:xi1].reshape(-1, 3).astype(np.float32)
    lum = crop.mean(axis=1)
    bright = crop[lum >= np.percentile(lum, 70)]
    if bright.shape[0] < 8:
        return fallback
    c = np.median(bright, axis=0)
    return (float(c[0] / 255), float(c[1] / 255), float(c[2] / 255))


def _line_em_px(box_h: float, text: str, x_height_px: float, x_ratio: float) -> float:
    """Estimate the font em (pixels) for one OCR line, anchored to the OCR box
    height (reliable). The measured x-height refines it only on a mixed-case line
    where it agrees with the box, which avoids over-sizing all-caps/sparse lines."""
    letters = [c for c in text if c.isalpha()]
    caps = bool(letters) and sum(c.isupper() for c in letters) / len(letters) > 0.7
    em_box = box_h / (_BOX_EM_CAPS if caps else _BOX_EM_MIXED)
    em_xh = (x_height_px / x_ratio) if x_height_px else 0.0
    if (not caps) and em_xh and 0.75 * em_box <= em_xh <= 1.25 * em_box:
        return em_xh
    return em_box


def reconstruct_page(image_rgb: np.ndarray, dpi: float, ocr_lines: list,
                     base_font_serif: str, base_font_sans: str,
                     family_label: str = "Scanned Text") -> "ReconResult | None":
    """Build placed word boxes + the matched family for one page. ``image_rgb`` is
    the page raster at ``dpi``; ``ocr_lines`` a list of ``engine.OcrLine``. The
    ``base_font_*`` args are kept for signature compatibility (no longer used to
    build a font). Returns None when no usable text was recovered."""
    if image_rgb is None or not ocr_lines:
        return None
    ppi = dpi / 72.0
    H, W = image_rgb.shape[:2]

    rep_bitmap: dict = {}     # char -> representative ink bitmap (for serif guess)
    advances: list = []       # per-glyph advance (px) for the mono test
    space_em_list: list = []
    em_list: list = []
    raw_lines: list = []      # (origin_px, text, em_px, conf, cover_pt)
    x_ratio = _X_RATIO_SERIF

    page_paper = _paper_color(image_rgb)   # fallback when a word crop is sparse
    for ln in ocr_lines:
        x0, y0, x1, y1 = ln.bbox
        x0i, y0i = max(0, int(x0)), max(0, int(y0))
        x1i, y1i = min(W, int(x1) + 1), min(H, int(y1) + 1)
        if x1i - x0i < 3 or y1i - y0i < 3:
            continue
        strip = image_rgb[y0i:y1i, x0i:x1i]
        seg = segment_line(strip, ln.text)
        if seg is None or not seg.words:
            continue
        em_px = _line_em_px(y1i - y0i, ln.text, seg.x_height_px, x_ratio)
        if em_px <= 1:
            continue
        em_list.append(em_px)
        if seg.space_px:
            space_em_list.append(seg.space_px / em_px)
        pad = max(2.0, 0.06 * em_px)
        baseline_px = y0i + seg.baseline_y
        for w in seg.words:
            if not w.text.strip():
                continue
            w_origin_px = (x0i + w.x0, baseline_px)
            w_cover_pt = ((x0i + w.x0 - pad) / ppi, (y0i + w.top - pad) / ppi,
                          (x0i + w.x1 + pad) / ppi, (y0i + w.bottom + pad) / ppi)
            w_bg = _local_paper_color(
                image_rgb, x0i + w.x0 - pad, y0i + w.top - pad,
                x0i + w.x1 + pad, y0i + w.bottom + pad, page_paper)
            raw_lines.append(
                (w_origin_px, w.text, em_px, ln.confidence, w_cover_pt, w_bg))
        glyphs = sorted(seg.glyphs, key=lambda g: g.x0)
        for i, g in enumerate(glyphs):
            if g.char not in rep_bitmap and g.bitmap.size:
                rep_bitmap[g.char] = g.bitmap
            if i + 1 < len(glyphs):
                advances.append(glyphs[i + 1].x0 - g.x0)

    if not raw_lines or not rep_bitmap:
        return None

    family = fontmatch.classify_family(rep_bitmap)

    lines = []
    for origin_px, text, em_px, conf, cover_pt, w_bg in raw_lines:
        size_pt = em_px / ppi
        origin_pt = (origin_px[0] / ppi, origin_px[1] / ppi)
        lines.append(LineBox(origin=origin_pt, text=text, size=size_pt,
                             confidence=conf, cover=cover_pt, bg=w_bg))

    return ReconResult(otf_bytes=b"", family_name=family, lines=lines,
                       traced_chars="".join(sorted(rep_bitmap.keys())),
                       n_lines=len(lines), bg_color=page_paper)
