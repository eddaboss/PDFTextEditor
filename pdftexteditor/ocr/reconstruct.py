"""Turn a scanned page + its OCR lines into (a) a custom scan-built font and
(b) editable text boxes positioned on the page (OCR_SPEC §3 step 5/6, §8 Tier 1).

This is the engine-agnostic core that ties OCR -> segmentation -> font build
-> placed text together. It runs entirely off the GUI thread (pure numpy / cv2 /
fontTools / vtracer -- no fitz, no Qt): the caller renders the page to an RGB
array on the GUI thread, hands it here, and applies the returned boxes + font
back on the GUI thread.

Output geometry is in PDF POINTS so it drops straight into ``add_box`` /
``NewBox`` (baseline ``origin``, point ``size``). One box per LINE: the built
font's own advances + measured space carry the intra-line spacing, which
reproduces the scan's rhythm without per-word drift.
"""

from __future__ import annotations

import hashlib
import statistics
from dataclasses import dataclass, field

import cv2
import numpy as np

from .fontbuild import GlyphSample, build_font
from .segment import segment_line

# The font always covers this set so editing never hits a blank glyph: observed
# chars are traced, the rest are borrowed from the base font (OCR_SPEC §8).
DEFAULT_CHARSET = set(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
    ".,;:!?'\"`-–—()[]{}/\\%&@#$*+=<>_|~^ ‘’“”")

_X_RATIO_SERIF = 0.45      # x-height / em (Times-class)
_X_RATIO_SANS = 0.52       # x-height / em (Helvetica-class)
_DEFAULT_GAP_EM = 0.06     # fallback inter-glyph gap
_DEFAULT_SPACE_EM = 0.30   # fallback inter-word gap


@dataclass
class LineBox:
    """One reconstructed editable line: baseline ``origin`` (PDF points), the
    recognized ``text``, the estimated point ``size``, OCR ``confidence``, and
    ``cover`` -- the recognized text's rectangle in DISPLAY points
    (x0,y0,x1,y1), painted in the paper color before the rebuilt text is drawn
    so the scanned glyphs underneath are replaced, not doubled."""

    origin: tuple
    text: str
    size: float
    confidence: float
    cover: tuple = ()


@dataclass
class ReconResult:
    """Everything the GUI thread needs to inject the OCR result."""

    otf_bytes: bytes
    family_name: str
    lines: list = field(default_factory=list)        # list[LineBox]
    traced_chars: str = ""                            # chars traced from the scan
    n_lines: int = 0
    bg_color: tuple = (1.0, 1.0, 1.0)                # sampled paper color


def _paper_color(image_rgb: np.ndarray) -> tuple:
    """The page's dominant light/paper color (r,g,b in 0..1): the median of the
    bright pixels, so a cover rect blends into the scan instead of flashing
    pure white on an off-white scan."""
    flat = image_rgb.reshape(-1, 3).astype(np.float32)
    lum = flat.mean(axis=1)
    thresh = max(180.0, float(np.percentile(lum, 60)))
    bright = flat[lum > thresh]
    if bright.shape[0] == 0:
        bright = flat
    c = np.median(bright, axis=0)
    return (float(c[0] / 255), float(c[1] / 255), float(c[2] / 255))


def _serif_guess(samples: "dict[str, GlyphSample]") -> bool:
    """Cheap serif/sans guess from stroke-contrast: serif faces have high
    thick/thin contrast, sans are near-uniform. Measured on a few stems."""
    contrasts = []
    for ch in "Inlhdb":
        s = samples.get(ch)
        if s is None or s.bitmap.size == 0:
            continue
        rows = s.bitmap.sum(axis=1)          # ink per row of a vertical-stem glyph
        rows = rows[rows > 0]
        if rows.size >= 4:
            contrasts.append(float(rows.max()) / max(1.0, float(np.median(rows))))
    if not contrasts:
        return True                          # default serif
    return statistics.median(contrasts) > 1.8


def reconstruct_page(image_rgb: np.ndarray, dpi: float, ocr_lines: list,
                     base_font_serif: str, base_font_sans: str,
                     family_label: str = "Scanned Text") -> "ReconResult | None":
    """Build the scan font + placed line boxes for one page.

    ``image_rgb`` is the page raster at ``dpi``; ``ocr_lines`` a list of
    ``engine.OcrLine``; ``base_font_serif`` / ``base_font_sans`` are absolute
    paths to TTF/OTF files used to borrow glyphs the scan never showed. Returns
    None when no usable text was recovered.
    """
    if image_rgb is None or not ocr_lines:
        return None
    ppi = dpi / 72.0
    H, W = image_rgb.shape[:2]

    # --- pass 1: segment every line, harvest glyph instances + metrics --------
    char_inst: dict = {}
    gap_em_list: list = []
    space_em_list: list = []
    em_list: list = []
    raw_lines: list = []          # (origin_px, baseline_px, text, em_px, conf)
    x_ratio = _X_RATIO_SERIF      # provisional; refined after serif guess

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
        em_px = (seg.x_height_px / x_ratio) if seg.x_height_px else (y1i - y0i) * 0.8
        if em_px <= 1:
            continue
        em_list.append(em_px)
        if seg.space_px:
            space_em_list.append(seg.space_px / em_px)
        first_x = seg.words[0].x0
        origin_px = (x0i + first_x, y0i + seg.baseline_y)
        # Cover rect: the recognized line's pixel box, padded, in display points.
        pad = max(2.0, 0.06 * em_px)
        cover_pt = ((x0 - pad) / ppi, (y0 - pad) / ppi,
                    (x1 + pad) / ppi, (y1 + pad) / ppi)
        raw_lines.append((origin_px, ln.text, em_px, ln.confidence, cover_pt))
        glyphs = sorted(seg.glyphs, key=lambda g: g.x0)
        for i, g in enumerate(glyphs):
            baseline_row = g.baseline_y - g.top_y
            char_inst.setdefault(g.char, []).append(
                (g.bitmap, baseline_row, em_px))
            if i + 1 < len(glyphs):
                gap = glyphs[i + 1].x0 - (g.x0 + g.bitmap.shape[1])
                if -0.1 * em_px < gap < 0.6 * em_px:
                    gap_em_list.append(gap / em_px)

    if not raw_lines or not char_inst:
        return None

    ref_em = statistics.median(em_list)
    median_gap_em = statistics.median(gap_em_list) if gap_em_list else _DEFAULT_GAP_EM
    median_gap_px = median_gap_em * ref_em

    # --- pass 2: one representative glyph per char, normalized to ref em -------
    samples: dict = {}
    for ch, lst in char_inst.items():
        lst.sort(key=lambda t: t[0].shape[0])         # by height
        bitmap, baseline_row, em_px = lst[len(lst) // 2]
        s = ref_em / em_px
        if abs(s - 1.0) > 0.02:
            h, w = bitmap.shape
            bitmap = cv2.resize(
                bitmap.astype(np.uint8),
                (max(1, round(w * s)), max(1, round(h * s))),
                interpolation=cv2.INTER_AREA if s < 1 else cv2.INTER_NEAREST
            ).astype(bool)
            baseline_row *= s
        samples[ch] = GlyphSample(
            char=ch, bitmap=bitmap, baseline_row=baseline_row,
            advance_px=bitmap.shape[1] + median_gap_px, lsb_px=0.0)

    serif = _serif_guess(samples)
    base_path = base_font_serif if serif else base_font_sans

    xs = [samples[c].bitmap.shape[0] for c in "xeoacnsu" if c in samples]
    xheight_px = statistics.median(xs) if xs else 0.45 * ref_em
    space_em = statistics.median(space_em_list) if space_em_list else _DEFAULT_SPACE_EM
    space_px = max(0.12, space_em - median_gap_em) * ref_em

    charset = set(samples.keys()) | DEFAULT_CHARSET
    digest = hashlib.sha1(
        ("".join(l[1] for l in raw_lines)).encode("utf-8")).hexdigest()[:6]
    family_name = f"{family_label} {digest}"

    otf = build_font(samples, ref_em, family_name, charset,
                     base_font_path=base_path, xheight_px=xheight_px,
                     space_px=space_px)

    lines = []
    for origin_px, text, em_px, conf, cover_pt in raw_lines:
        size_pt = em_px / ppi
        origin_pt = (origin_px[0] / ppi, origin_px[1] / ppi)
        lines.append(LineBox(origin=origin_pt, text=text, size=size_pt,
                             confidence=conf, cover=cover_pt))

    return ReconResult(otf_bytes=otf, family_name=family_name, lines=lines,
                       traced_chars="".join(sorted(samples.keys())),
                       n_lines=len(lines), bg_color=_paper_color(image_rgb))
