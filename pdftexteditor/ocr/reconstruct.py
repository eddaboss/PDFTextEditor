"""Turn a scanned page + its OCR lines into editable text boxes positioned on the
page, rendered in a MATCHED REAL FONT (0.3.0: no vtracer scan-built font).

Recognition -> group lines into AREAS (one box per paragraph; a form field stays
its own box) -> identify the document font (the shipped bank, ~4k fonts; else a
bundled serif/sans/mono family) -> place invisible editable text over the kept
scan. Editing a box flips it visible; a single word recolors + hard-damages to
match the scan (ocr/degrade.py), a paragraph reflows as a local-coloured tile
(document.py).

This replaces the old ClearScan-style scan-built font (vtracer): that produced
distorted glyphs and was the bulk of the "looks wrong" problem. Sizing is anchored
to the OCR box height (reliable), and a box covers a whole AREA (a paragraph edits
as ONE unit instead of confetti per word) with its rect painted in the area's OWN
local background colour once the box is edited. Pure numpy / cv2; off the GUI thread.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field

import cv2
import numpy as np

from . import fontbank, fontmatch
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
    """One reconstructed editable AREA -- a paragraph or a single line. ``origin``
    is the first line's left baseline (DISPLAY points), ``text`` the recognized
    text (lines joined with a single space), ``size`` the body point size, and
    ``cover`` the area's rectangle in DISPLAY points (x0,y0,x1,y1) painted in
    ``bg`` (the area's OWN local background colour, not the page-wide paper tone)
    under an edit. For a multi-line area ``is_paragraph`` is True and ``box_w``
    (column width, display pts) + ``leading`` (baseline-to-baseline) drive the
    reflow so the whole paragraph edits + wraps as ONE box; a single line leaves
    them unset and behaves like before."""

    origin: tuple
    text: str
    size: float
    confidence: float
    cover: tuple = ()
    box_w: float | None = None
    leading: float = 0.0
    is_paragraph: bool = False
    bg: tuple = (1.0, 1.0, 1.0)


@dataclass
class ReconResult:
    """Everything the GUI thread needs to inject the OCR result. ``family`` is the
    matched bank font (``otf_bytes`` holds its bytes, registered as a custom face)
    or, on a weak/absent match, a bundled family (``otf_bytes`` empty)."""

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
    """The background colour right around ONE area: the median of the brighter
    pixels in its (padded) box, so an edited box's cover matches its OWN cell
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
    """Build placed area boxes + the matched family for one page. ``image_rgb`` is
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
    raw_lines: list = []      # per-line geometry dicts (display PIXELS)
    cells: dict = {}          # id -> (char, scan bbox) for bank font ID
    x_ratio = _X_RATIO_SERIF

    page_paper = _paper_color(image_rgb)   # fallback when a line crop is sparse
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
        if seg.space_px:
            space_em_list.append(seg.space_px / em_px)
        if ln.text.strip():
            lbg = _local_paper_color(image_rgb, x0i, y0i, x1i, y1i, page_paper)
            raw_lines.append({
                "x0": x0i, "y0": y0i, "x1": x1i, "y1": y1i,
                "baseline": y0i + seg.baseline_y, "text": ln.text.strip(),
                "em": em_px, "conf": ln.confidence, "bg": lbg,
            })
        glyphs = sorted(seg.glyphs, key=lambda g: g.x0)
        for i, g in enumerate(glyphs):
            if g.char not in rep_bitmap and g.bitmap.size:
                rep_bitmap[g.char] = g.bitmap
            if g.bitmap.size and g.char.strip():
                gx = x0i + int(g.x0)
                cells[len(cells)] = (g.char,
                                     (gx, y0i, gx + int(g.bitmap.shape[1]), y1i))
            if i + 1 < len(glyphs):
                advances.append(glyphs[i + 1].x0 - g.x0)

    if not raw_lines or not rep_bitmap:
        return None

    # Identify the document's ACTUAL font from the shipped bank (~4k fonts) by
    # shape-matching the scanned glyphs, and embed it, so an edit matches the
    # document instead of one of three fallback families. Falls back to the
    # 3-family classifier when the bank is absent or the match is too weak
    # (match_font returns None); then there is no per-page custom face.
    family = fontmatch.classify_family(rep_bitmap)
    otf_bytes = b""
    try:
        matched = fontbank.match_font(image_rgb, cells)
    except Exception:
        matched = None
    if matched is not None:
        otf_bytes, family = matched

    # ONE editable box per AREA, not per word/line. A box per word turned a page
    # into confetti; what the user wants is to edit a whole paragraph as one. Lines
    # fuse into an area when the next sits directly below, left-aligned, at a
    # similar size with tight leading (one paragraph); a form FIELD (an isolated
    # line, or a row separated by a big gap) stays its own single box.
    lines = [_area_to_box(area, ppi)
             for area in _group_lines_into_areas(raw_lines)]

    return ReconResult(otf_bytes=otf_bytes, family_name=family, lines=lines,
                       traced_chars="".join(sorted(rep_bitmap.keys())),
                       n_lines=len(lines), bg_color=page_paper)


def _group_lines_into_areas(raw_lines: list) -> list:
    """Cluster per-line geometry dicts (display px) into reading-order AREAS. A
    line joins an existing area when it sits DIRECTLY BELOW that area's last line
    (a small vertical gap), shares its LEFT edge (same column), and is a similar
    size -- the signature of one paragraph. It joins the CLOSEST such area; if none
    qualifies it opens a new area, so a form field or a new row stays its own box."""
    areas: list = []
    for ln in sorted(raw_lines, key=lambda l: (l["y0"], l["x0"])):
        best, best_gap = None, None
        for area in areas:
            last = area[-1]
            lh = max(last["y1"] - last["y0"], 1.0)
            vgap = ln["y0"] - last["y1"]
            if not (-0.4 * lh <= vgap <= 0.9 * lh):
                continue
            if abs(ln["x0"] - last["x0"]) > 1.6 * last["em"]:
                continue
            if not (0.7 <= ln["em"] / max(last["em"], 1e-3) <= 1.4):
                continue
            if best is None or vgap < best_gap:
                best, best_gap = area, vgap
        if best is None:
            areas.append([ln])
        else:
            best.append(ln)
    return areas


def _area_to_box(area: list, ppi: float) -> "LineBox":
    """One AREA (line dicts, top->bottom) -> a LineBox in PDF points. Origin = the
    first line's left baseline; cover = the area's union rect (+pad); ``bg`` = the
    area's own local background. For >= 2 lines it is a reflowable PARAGRAPH
    (``box_w`` = column width, ``leading`` = median baseline-to-baseline gap) so the
    whole block edits + wraps as one box; a single line leaves those unset."""
    em = statistics.median([l["em"] for l in area])
    pad = max(2.0, 0.06 * em)
    x0 = min(l["x0"] for l in area)
    y0 = min(l["y0"] for l in area)
    x1 = max(l["x1"] for l in area)
    y1 = max(l["y1"] for l in area)
    cover_pt = ((x0 - pad) / ppi, (y0 - pad) / ppi,
                (x1 + pad) / ppi, (y1 + pad) / ppi)
    origin_pt = (area[0]["x0"] / ppi, area[0]["baseline"] / ppi)
    box = LineBox(origin=origin_pt, text=" ".join(l["text"] for l in area),
                  size=em / ppi, confidence=min(l["conf"] for l in area),
                  cover=cover_pt, bg=area[0].get("bg", (1.0, 1.0, 1.0)))
    if len(area) >= 2:
        gaps = [area[i]["baseline"] - area[i - 1]["baseline"]
                for i in range(1, len(area))]
        box.box_w = (x1 - x0) / ppi
        box.leading = statistics.median(gaps) / ppi
        box.is_paragraph = True
    return box
