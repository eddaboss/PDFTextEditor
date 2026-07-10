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
    # Per-line covers (DISPLAY points) for a paragraph: one (x0,y0,x1,y1,r,g,b) per
    # recognized line, in the SAME top->bottom order as the ``\n``-joined ``text``.
    # Each entry IS a single-line cover, so a paragraph edits as N single lines on
    # the same in-place engine (scan pixels kept per line). Empty for a single line.
    line_covers: list = field(default_factory=list)
    # This BOX's own matched font, chosen from ONLY this box's glyphs (not the page
    # average): ``family`` the family name, ``otf_bytes`` the bank TTF when matched
    # (empty -> a bundled family). A mono body and a bold-sans header on the same page
    # therefore get different fonts, so an edit matches the box it is in.
    family: str = ""
    otf_bytes: bytes = b""


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
    # Page-level per-glyph SUPER-RESOLUTION font map (supermatch.PageFontMap), built
    # from the page-wide word segmentation so numbers/fields pool enough to match.
    # The editor queries it per caret; None when the bank is absent.
    font_map: object = None


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


_MAX_SPACES = 60         # safety clamp on the number of spaces emitted for one gap
_DEFAULT_SPACE_EM = 0.28  # a single space is ~0.28 em when no measured width is available


def _gap_to_spacer(gap_px: float, single_space_px: float, em_px: float,
                   allow_join: bool = False) -> str:
    """The spacer string between two tokens for a measured ``gap_px``: ``''`` (joined,
    a mid-word split) when ``allow_join`` and the gap is sub-space; otherwise
    ``round(gap / single_space)`` DISCRETE spaces -- never a tab, so a wide gap edits as
    many individual, deletable spaces rather than one undividable field. ``single_space_px``
    is the page-wide single-space width; falls back to ``_DEFAULT_SPACE_EM * em``."""
    sp = single_space_px if (single_space_px and single_space_px > 1.0) else (
        _DEFAULT_SPACE_EM * em_px if em_px else 0.0)
    if allow_join and em_px and gap_px < 0.30 * em_px:
        return ""                                  # mid-word fragment split -> no space
    if sp <= 0.0:
        return " "
    n = max(1, int(round(gap_px / sp)))
    return " " * min(n, _MAX_SPACES)


def _requantize_from_words(words: list, fallback_text: str,
                           single_space_px: float, em_px: float) -> str:
    """Rebuild a line's text from its measured ``words`` (``(x0, x1, text)`` tuples),
    re-spacing each inter-word gap by width so a wide gap edits as MULTIPLE spaces (or a
    tab) instead of one big space. ``single_space_px`` is the PAGE-WIDE single-space
    estimate (robust on form pages, where a per-line median is just the field gap). Only
    ever re-spaces: if the rebuilt word tokens would differ, keeps the engine text."""
    if not words or len(words) < 2:
        return fallback_text
    out = [words[0][2]]
    for i in range(1, len(words)):
        gap = float(words[i][0]) - float(words[i - 1][1])
        out.append(_gap_to_spacer(gap, single_space_px, em_px))
        out.append(words[i][2])
    rebuilt = "".join(out)
    if rebuilt.split() != fallback_text.split():   # word tokens must be unchanged
        return fallback_text
    return rebuilt


def _page_single_space(gaps: list) -> float:
    """The width of ONE space (px), from the REAL single spaces between words -- not the
    huge field gaps elsewhere on the page. Those wide gaps are garbage for this estimate,
    so start at the median and iteratively re-center on the dominant low cluster, dropping
    anything more than 1.75x the running estimate. The result is the median word-to-word
    space, which a normal gap rounds to 1 of while a wide field gap rounds to several."""
    a = np.array([float(g) for g in gaps if g > 0], dtype=float)
    if a.size == 0:
        return 0.0
    sp = float(np.median(a))
    for _ in range(6):
        small = a[a <= 1.75 * sp]
        if small.size == 0:
            break
        nsp = float(np.median(small))
        if abs(nsp - sp) < 0.5:
            sp = nsp
            break
        sp = nsp
    return sp


def _respace_area(area: list) -> None:
    """Re-space ONE box (area) off its OWN median word gap. Spaces scale with font, so the
    single-space width is measured PER BOX -- the trimmed median of this box's inter-word
    gaps (``_page_single_space`` drops the wide field gaps as outliers). Every gap is then
    re-expressed as ``round(gap / median)`` spaces, in place on each line's ``text``."""
    gaps: list = []
    for ln in area:
        words = ln.get("_words") or []
        for i in range(1, len(words)):
            g = words[i][0] - words[i - 1][1]
            if g > 0:
                gaps.append(g)
    if len(gaps) < 2:
        return
    med = _page_single_space(gaps)              # this box's single-space width (px)
    if med <= 1.0:
        return
    for ln in area:
        words = ln.get("_words")
        if words and len(words) >= 2:
            ln["text"] = _requantize_from_words(words, ln["text"], med, 0.0)


def recover_dropped_lines(image_rgb: np.ndarray, lines: list, engine) -> list:
    """Re-OCR a focused crop of any same-column line BLOCK whose vertical PITCH jumps
    to ~2x the page's normal line pitch with real ink in the gap. The full-page text
    DETECTOR silently drops a faint line (e.g. a faint address row) even though that
    same line reads fine from a focused crop -- which leaves the surviving lines welded
    across the gap into one wrong box, or a present line with broken geometry. Returns
    the recovered ``OcrLine``s (deduped against the existing detections); ``[]`` when
    nothing qualifies, which is the common case -- so a clean page pays NO extra OCR.

    General by construction: it keys ONLY on relative line pitch (median of the page's
    own same-column line spacings) and ink presence vs the page's own paper -- no document
    coordinates, strings, or absolute thresholds. ``engine`` is the same OcrEngine used
    for the page, so the crop re-OCR is identical on the RapidOCR/Windows path."""
    from .engine import OcrLine
    if len(lines) < 2 or image_rgb is None:
        return []
    H, W = image_rgb.shape[:2]
    g = image_rgb.mean(2) if image_rgb.ndim == 3 else image_rgb
    cov = (255.0 - g.astype(np.float32)) / 255.0
    items = sorted(lines, key=lambda o: o.bbox[1])
    boxes = [o.bbox for o in items]

    def _next_same_col(i):
        ax0, ay0, ax1, ay1 = boxes[i]
        for j in range(i + 1, len(items)):
            bx0, by0, bx1, by1 = boxes[j]
            if min(ax1, bx1) - max(ax0, bx0) > 0.5 * min(ax1 - ax0, bx1 - bx0):
                return j
        return -1

    pitches = []
    for i in range(len(items)):
        j = _next_same_col(i)
        if j >= 0:
            p = boxes[j][1] - boxes[i][1]
            if 0 < p < (boxes[i][3] - boxes[i][1]) * 4:
                pitches.append(p)
    if not pitches:
        return []
    med = float(np.median(pitches))
    seen = list(boxes)
    out: list = []
    for i in range(len(items)):
        j = _next_same_col(i)
        if j < 0:
            continue
        ax0, ay0, ax1, ay1 = boxes[i]
        bx0, by0, bx1, by1 = boxes[j]
        if by0 - ay0 < 1.6 * med:                       # < ~one skipped row -> no gap
            continue
        cx0, cx1 = int(max(0, min(ax0, bx0))), int(min(W, max(ax1, bx1)))
        gy0, gy1 = int(max(0, ay0)), int(min(H, by1))
        # require real ink BETWEEN the two lines (an empty gap is a true blank, not a miss)
        gap = cov[int(ay1):int(by0), cx0:cx1] if by0 > ay1 else cov[gy0:gy1, cx0:cx1]
        if gap.size == 0 or float((gap > 0.30).mean()) < 0.01:
            continue
        crop = image_rgb[gy0:gy1, cx0:cx1]
        if crop.shape[0] < 8 or crop.shape[1] < 8:
            continue
        try:
            rec = engine.recognize(np.ascontiguousarray(crop))
        except Exception:
            continue
        for o in rec:
            ox0, oy0, ox1, oy1 = o.bbox
            px = (ox0 + cx0, oy0 + gy0, ox1 + cx0, oy1 + gy0)
            if o.confidence < 0.5 or not o.text.strip():
                continue
            pa = (px[2] - px[0]) * (px[3] - px[1])
            dup = False
            for s in seen:
                ix = min(px[2], s[2]) - max(px[0], s[0])
                iy = min(px[3], s[3]) - max(px[1], s[1])
                if ix > 0 and iy > 0 and ix * iy > 0.3 * min(
                        pa, (s[2] - s[0]) * (s[3] - s[1])):
                    dup = True
                    break
            if dup:
                continue
            out.append(OcrLine(quad=[[px[0], px[1]], [px[2], px[1]],
                                     [px[2], px[3]], [px[0], px[3]]],
                               text=o.text, confidence=o.confidence))
            seen.append(px)
    return out


def reconstruct_page(image_rgb: np.ndarray, dpi: float, ocr_lines: list,
                     base_font_serif: str, base_font_sans: str,
                     family_label: str = "Scanned Text",
                     progress_cb=None) -> "ReconResult | None":
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
    all_ratios: list = []     # every inter-word gap as a fraction of em -> single-space ratio
    x_ratio = _X_RATIO_SERIF

    page_paper = _paper_color(image_rgb)   # fallback when a line crop is sparse
    for ln in ocr_lines:
        x0, y0, x1, y1 = ln.bbox
        x0i, y0i = max(0, int(x0)), max(0, int(y0))
        x1i, y1i = min(W, int(x1) + 1), min(H, int(y1) + 1)
        if x1i - x0i < 3 or y1i - y0i < 3:
            continue
        strip = image_rgb[y0i:y1i, x0i:x1i]
        try:
            seg = segment_line(strip, ln.text)
        except Exception:                          # a single bad strip must not lose the page
            seg = None
        if seg is None or not seg.words:
            continue
        em_px = _line_em_px(y1i - y0i, ln.text, seg.x_height_px, x_ratio)
        if em_px <= 1:
            continue
        if seg.space_px:
            space_em_list.append(seg.space_px / em_px)
        # Per-line glyphs, so the font can be matched PER BOX (not page-wide): a
        # page mixing a mono body with a bold-sans header must not average them.
        line_rep: dict = {}
        line_cells: list = []
        glyphs = sorted(seg.glyphs, key=lambda g: g.x0)
        for i, g in enumerate(glyphs):
            if g.char not in rep_bitmap and g.bitmap.size:
                rep_bitmap[g.char] = g.bitmap
            if g.bitmap.size and g.char not in line_rep:
                line_rep[g.char] = g.bitmap
            if g.bitmap.size and g.char.strip():
                gx = x0i + int(g.x0)
                cell = (g.char, (gx, y0i, gx + int(g.bitmap.shape[1]), y1i))
                cells[len(cells)] = cell
                line_cells.append(cell)
            if i + 1 < len(glyphs):
                advances.append(glyphs[i + 1].x0 - g.x0)
        if ln.text.strip():
            lbg = _local_paper_color(image_rgb, x0i, y0i, x1i, y1i, page_paper)
            # CAPS-NORMALISED x-height for grouping: an all-caps / digits-only line measures
            # its "x-height" as the CAP height (~1.4x a lowercase x-height), so a name or a
            # number in a form block would read as a bigger size and orphan from its block.
            # Divide it back to an equivalent x-height when the line has no lowercase.
            xh = float(seg.x_height_px or 0.0)
            nxh = xh / 1.4 if (xh > 0 and not any(c.islower() for c in ln.text)) else xh
            # Keep this line's word boxes (strip-relative x is fine -- re-spacing uses gap
            # WIDTHS only) and feed every inter-word gap, as a fraction of THIS line's em,
            # into the page-wide pool. A space-to-em RATIO is size-invariant, so one page
            # estimate fits a small body line and a big header alike (an absolute px median
            # was too wide for small lines, leaving their gaps stuck at one space). The
            # actual re-spacing waits until after the loop, when the page ratio is known.
            words = [(float(w.x0), float(w.x1), w.text) for w in (seg.words or [])]
            for i in range(1, len(words)):
                g = words[i][0] - words[i - 1][1]
                if g > 0 and em_px > 1:
                    all_ratios.append(g / em_px)
            raw_lines.append({
                "x0": x0i, "y0": y0i, "x1": x1i, "y1": y1i,
                "baseline": y0i + seg.baseline_y,
                "text": ln.text.strip(), "_words": words,
                "em": em_px, "xh": xh, "nxh": nxh,
                "conf": ln.confidence, "bg": lbg,
                "_rep": line_rep, "_cells": line_cells,
            })

    if not raw_lines or not rep_bitmap:
        return None

    # PAGE-WIDE match, kept ONLY as a fallback for an area too sparse to match on its
    # own. Identify the font from the shipped bank (~4k fonts) by shape-matching the
    # scanned glyphs; falls back to the 3-family classifier when the bank is absent or
    # the match is too weak (match_font returns None).
    page_family = fontmatch.classify_family(rep_bitmap)
    page_otf = b""
    try:
        _m = fontbank.match_font(image_rgb, cells)
    except Exception:
        _m = None
    if _m is not None:
        page_otf, page_family = _m

    def _match_area(area: list) -> tuple:
        """Match the font from ONLY this area's glyphs, so a header in a different
        font than the body gets ITS font, not the page average."""
        arep: dict = {}
        acells: dict = {}
        for ln in area:
            for c, bm in ln.get("_rep", {}).items():
                arep.setdefault(c, bm)
            for cell in ln.get("_cells", []):
                acells[len(acells)] = cell
        if not arep:
            return page_family, page_otf
        fam = fontmatch.classify_family(arep)
        otf = b""
        try:
            m = fontbank.match_font(image_rgb, acells)
        except Exception:
            m = None
        if m is not None:
            otf, fam = m
        return fam, otf

    # ONE editable box per AREA, not per word/line. A box per word turned a page
    # into confetti; what the user wants is to edit a whole paragraph as one. Lines
    # fuse into an area when the next sits directly below, left-aligned, at a
    # similar size with tight leading (one paragraph); a form FIELD (an isolated
    # line, or a row separated by a big gap) stays its own single box. Each area
    # carries its OWN matched font.
    # SPACING is re-quantized PER BOX (``_respace_area`` below), off each box's OWN median
    # word gap -- spaces scale with font, so one page value is wrong. The page-wide ratio is
    # kept ONLY for merged fragment joins (those lines lose their word boxes, so the per-box
    # pass can't reach them); the word boxes stay on every un-merged line for that pass.
    sp_ratio = _page_single_space(all_ratios) if len(all_ratios) >= 8 else 0.0
    # TABLE / PAGE RULE map: a box must never cross a ruled cell border, else two separate
    # cells fuse into one box. Detect the rules (text-scaled length floor, from the OCR line
    # heights) and feed them to the merge + group steps as HARD boundaries.
    h_rules, v_rules = [], []
    try:
        from .borders import detect_borders
        _hs = [l["y1"] - l["y0"] for l in raw_lines]
        _ml = 2.5 * statistics.median(_hs) if _hs else 0.0
        h_rules, v_rules = detect_borders(image_rgb, min_len=_ml)
    except Exception:
        h_rules, v_rules = [], []
    # First SPLIT any detection that spans a ruled column border into per-cell sub-lines,
    # then rebuild real text lines from horizontally-split fragments BEFORE grouping (a
    # fragmented prose line no longer leaks out as its own overlapping box). Both steps treat
    # a vertical rule as a hard boundary, so a box never ends up crossing one.
    raw_lines = _split_lines_at_vrules(raw_lines, v_rules, sp_ratio)
    raw_lines = _merge_row_fragments(raw_lines, sp_ratio, v_rules)
    lines = []
    areas = list(_group_lines_into_areas(raw_lines, h_rules))
    n_areas = len(areas) or 1
    # The box's font_family here is only a DISPLAY/fallback default for the invisible
    # overlay -- font is NOT a box property. The authoritative per-glyph font is
    # resolved at EDIT time from the page-level super-resolution map (ocr/pagefont.py),
    # keyed to the caret position, so a box that mixes fonts renders each glyph in its
    # own. Keep the cheap per-area match for the display default only.
    for ai, area in enumerate(areas):
        _respace_area(area)            # quantize THIS box's gaps off its own median space
        _fam, _otf = _match_area(area)
        lines.append(_area_to_box(area, ppi, family=_fam, otf_bytes=_otf))
        if progress_cb is not None:    # REAL per-area progress (the heavy step)
            progress_cb((ai + 1) / n_areas)

    # PAGE FONT MAP: split every line into its WORDS (page-wide) and build the
    # per-glyph super-resolution font map from them -- the words pool by font so a
    # scattered number field has enough instances to super-resolve, unlike a per-box
    # derivation. The editor reads this map per caret position.
    page_words: list = []
    for ln in raw_lines:
        cells = ln.get("_cells", [])
        words = ln.get("_words", [])
        x0i = ln["x0"]
        if words:
            for (wx0, wx1, _wt) in words:
                a, b = x0i + wx0, x0i + wx1
                wc = [c for c in cells if a - 2 <= (c[1][0] + c[1][2]) / 2 <= b + 2]
                if wc:
                    page_words.append(wc)
        elif cells:
            page_words.append(cells)
    # PER-GLYPH font map: cluster same-shape glyphs across the page and match each cluster,
    # so DIFFERENT PARTS of one field can carry different fonts (a label + its value, etc.).
    # This is the map the editor reads at edit time; it must NOT be a per-field/per-page
    # single-font pick. The cluster matcher itself is the render-and-compare reresolver.
    try:
        from . import supermatch
        font_map = supermatch.build_page_map(image_rgb, page_words)
    except Exception:
        font_map = None

    return ReconResult(otf_bytes=page_otf, family_name=page_family, lines=lines,
                       traced_chars="".join(sorted(rep_bitmap.keys())),
                       n_lines=len(lines), bg_color=page_paper, font_map=font_map)


def _merge_line_group(group: list, em: float, space_em_ratio: float = 0.0) -> dict:
    """Fuse several same-row fragments (left->right) into one line dict. Text is joined
    gap-aware AND width-quantized: a sub-space gap is a mid-word split (no space), and a
    real gap becomes ``round(gap / single_space)`` spaces -- or one TAB when it spans
    >= ``_TAB_SPACES`` single spaces -- so a wide label-to-value gap edits as many spaces
    (or a tab), not one. The single space is ``space_em_ratio * em`` (size-aware)."""
    g = sorted(group, key=lambda l: l["x0"])
    text = g[0]["text"]
    ss = space_em_ratio * em
    for prev, l in zip(g, g[1:]):
        gap = l["x0"] - prev["x1"]
        text += _gap_to_spacer(gap, ss, em, allow_join=True) + l["text"]
    rep: dict = {}
    cells: list = []
    for l in g:
        for c, bm in l.get("_rep", {}).items():
            rep.setdefault(c, bm)
        cells += l.get("_cells", [])
    return {
        "x0": min(l["x0"] for l in g), "y0": min(l["y0"] for l in g),
        "x1": max(l["x1"] for l in g), "y1": max(l["y1"] for l in g),
        "baseline": statistics.median([l["baseline"] for l in g]),
        "text": text, "em": em,
        "xh": statistics.median([l.get("xh", 0.0) for l in g]),
        "nxh": statistics.median([l.get("nxh", 0.0) for l in g]),
        "conf": min(l["conf"] for l in g),
        "bg": g[0].get("bg", (1.0, 1.0, 1.0)), "_rep": rep, "_cells": cells,
    }


def _split_lines_at_vrules(raw_lines: list, v_rules: list,
                           space_em_ratio: float = 0.0) -> list:
    """Split a detection that ALREADY spans a ruled column border (one OCR line covering
    two cells) into one sub-line per cell, partitioning its words + glyph cells at each
    crossing vertical rule. Without this, preventing fragment fusion is not enough -- a
    single cross-cell detection stays one box. Sub-line text is rebuilt gap-aware from its
    own words; a line no rule crosses is returned unchanged."""
    if not v_rules:
        return raw_lines
    out: list = []
    for ln in raw_lines:
        words = ln.get("_words") or []
        lh = max(ln["y1"] - ln["y0"], 1.0)
        x0i = ln["x0"]
        xcs = sorted({int((x0 + x1) / 2) for (x0, y0, x1, y1) in v_rules
                      if ln["x0"] < (x0 + x1) / 2 < ln["x1"]
                      and min(y1, ln["y1"]) - max(y0, ln["y0"]) > 0.4 * lh})
        if not xcs or not words:
            out.append(ln)
            continue
        cells = ln.get("_cells") or []
        bounds = [ln["x0"] - 1] + xcs + [ln["x1"] + 1]
        for a, b in zip(bounds, bounds[1:]):
            sw = [(wx0, wx1, wt) for (wx0, wx1, wt) in words
                  if a <= x0i + (wx0 + wx1) / 2 < b]
            if not sw:
                continue
            ss = space_em_ratio * ln["em"]
            txt = sw[0][2]
            for p, c in zip(sw, sw[1:]):
                txt += _gap_to_spacer(c[0] - p[1], ss, ln["em"], allow_join=True) + c[2]
            sc = [c for c in cells if a <= (c[1][0] + c[1][2]) / 2 < b]
            nl = dict(ln)
            nl.update(x0=int(x0i + min(w[0] for w in sw)),
                      x1=int(x0i + max(w[1] for w in sw)),
                      text=txt, _words=sw, _cells=sc)
            out.append(nl)
    return out


def _vrule_between(v_rules, x_lo, x_hi, y_lo, y_hi) -> bool:
    """True if a VERTICAL rule sits in the x-gap (x_lo, x_hi) and overlaps the rows
    (y_lo, y_hi) -- i.e. a cell border separates two fragments, so they must not fuse."""
    if not v_rules:
        return False
    for (x0, y0, x1, y1) in v_rules:
        xc = (x0 + x1) / 2.0
        if x_lo < xc < x_hi and min(y1, y_hi) - max(y0, y_lo) > 0:
            return True
    return False


def _hrule_between(h_rules, y_lo, y_hi, x_lo, x_hi) -> bool:
    """True if a HORIZONTAL rule sits in the y-gap (y_lo, y_hi) and overlaps the columns
    (x_lo, x_hi) -- i.e. a cell border separates an area from the next line below it."""
    if not h_rules or x_hi <= x_lo:
        return False
    for (x0, y0, x1, y1) in h_rules:
        yc = (y0 + y1) / 2.0
        if y_lo < yc < y_hi and min(x1, x_hi) - max(x0, x_lo) > 0:
            return True
    return False


def _merge_row_fragments(raw_lines: list, space_em_ratio: float = 0.0,
                         v_rules: list = None) -> list:
    """Detectors (notably Apple Vision on dense/justified prose) sometimes split ONE
    physical text line into several horizontal fragments -- a left, middle and right
    piece of the same row. Left un-merged, the full-width sibling lines group into the
    paragraph while the stray fragments (which no longer share the left edge) become their
    OWN boxes; the paragraph's union cover then spans them and the boxes OVERLAP. Rebuild
    real lines off the page's row structure: cluster detections that share a text ROW
    (vertical overlap with the seed) and fuse the ones a SMALL horizontal gap apart
    (intra-line fragments of one physical line). A LARGE x-gap is a column boundary between
    SEPARATE fields and is left split, so unrelated fields stay separate boxes.
    ``space_em_ratio`` (space px / em) sizes the quantized join of fused fragments."""
    if len(raw_lines) < 2:
        return raw_lines
    items = sorted(raw_lines, key=lambda l: l["y0"])
    used = [False] * len(items)
    out: list = []
    for i, a in enumerate(items):
        if used[i]:
            continue
        used[i] = True
        ah = max(a["y1"] - a["y0"], 1.0)
        row = [a]
        for j in range(i + 1, len(items)):
            if used[j]:
                continue
            b = items[j]
            bh = max(b["y1"] - b["y0"], 1.0)
            ov = min(a["y1"], b["y1"]) - max(a["y0"], b["y0"])   # overlap with the SEED row
            # Two boxes share a text ROW only when their heights are COMPARABLE. One physical
            # line at a single font size has a fixed ascender-to-descender envelope, so two
            # fragments of it differ in box height by at most ~1.3x; a box over 2x another's
            # height is a different row band or a non-text GRAPHIC. Without this gate a tall
            # logo box vertically overlaps several short header rows and welds them all into
            # one scrambled line (the nso-logo header bug). The gate only ever SPLITS a row
            # (the tall outlier opens its own), never fuses, so it cannot create a scramble.
            if ov >= 0.5 * min(ah, bh) and max(ah, bh) < 2.0 * min(ah, bh):
                row.append(b)
                used[j] = True
        row.sort(key=lambda l: l["x0"])
        em = statistics.median([l["em"] for l in row])
        # Fuse only the pieces a SMALL gap apart (one physical line the detector split into
        # fragments); a LARGE x-gap is a column boundary between SEPARATE fields and is left
        # split, so two unrelated fields never get welded into one box.
        groups: list = [[row[0]]]
        for l in row[1:]:
            prev = groups[-1][-1]
            # Fuse only an intra-line fragment AND only when no ruled cell border falls inside
            # the FUSED span -- a vertical rule means SEPARATE cells, never one box. Check the
            # whole span (min x0 .. max x1), not the gap, so OVERLAPPING fragments (negative
            # gap) are handled too; a rule merely AT an edge is benign and does not block.
            if l["x0"] - prev["x1"] <= 2.2 * em and not _vrule_between(
                    v_rules, min(prev["x0"], l["x0"]), max(prev["x1"], l["x1"]),
                    min(prev["y0"], l["y0"]), max(prev["y1"], l["y1"])):
                groups[-1].append(l)
            else:
                groups.append([l])                                # column boundary -> separate
        for gp in groups:
            out.append(gp[0] if len(gp) == 1 else _merge_line_group(gp, em, space_em_ratio))
    return out


def _group_lines_into_areas(raw_lines: list, h_rules: list = None) -> list:
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
            # A horizontal rule between the area and this line is a row border: keep them in
            # SEPARATE boxes so a box never crosses a ruled line. Test against the two lines'
            # CENTRES, not their ink edges: a ruled cell border usually sits right at a line's
            # ink bottom (the text rests on the rule), so it lands a pixel INSIDE that line's
            # bbox and an edge-gap test (last.y1 < rule < ln.y0) misses it -- which is exactly
            # what fused a label row with the cell below it across its border. A DETECTED rule
            # (long + column-spanning, never text) between two text-line centres is always a
            # real between-row border, so the centre span is the robust boundary.
            ax0 = min(l["x0"] for l in area)
            ax1 = max(l["x1"] for l in area)
            last_yc = 0.5 * (last["y0"] + last["y1"])
            ln_yc = 0.5 * (ln["y0"] + ln["y1"])
            if _hrule_between(h_rules, last_yc, ln_yc,
                              max(ax0, ln["x0"]), min(ax1, ln["x1"])):
                continue
            # Size similarity by CAPS-NORMALISED X-HEIGHT vs the area's MEDIAN, not box-height
            # em vs the last line. A paragraph line carrying a tall cap + a descender gets a
            # taller OCR box (box-height em balloons), and an all-caps name in a form block
            # reads a bigger raw x-height; either wrongly fails a strict gate and orphans the
            # line into an OVERLAPPING box. The normalised x-height is stable across one font
            # size, and comparing to the area median shrugs off a single noisy neighbour while
            # still separating a genuinely bigger header. Falls back to em when x-height is
            # missing.
            amed = statistics.median([l.get("nxh") or 0.0 for l in area])
            lnx = ln.get("nxh") or 0.0
            if amed > 1 and lnx > 1:
                if not (0.7 <= lnx / amed <= 1.4):
                    continue
            elif not (0.7 <= ln["em"] / max(last["em"], 1e-3) <= 1.4):
                continue
            if best is None or vgap < best_gap:
                best, best_gap = area, vgap
        if best is None:
            areas.append([ln])
        elif _join_nests_other_area(best, ln, areas):
            # A join must not widen the area's union cover so it NEWLY overlaps a SEPARATE
            # existing area -- the multi-column-list doubled-box: a narrow column entry
            # (e.g. one bullet) pulls in a full-width continuation row whose width reaches
            # across the sibling column boxes sitting between them, drawing a cover AROUND
            # another box's whole content. Keep this line as its own area instead.
            areas.append([ln])
        else:
            best.append(ln)
    return areas


def _join_nests_other_area(area: list, ln: dict, areas: list) -> bool:
    """True iff appending ``ln`` to ``area`` would widen ``area``'s union cover so it
    overlaps ANOTHER existing area that ``area`` does not already overlap. Pure geometry
    in relative coords -- the signature of a column entry swallowing a wider continuation
    row that engulfs sibling column boxes between them. A normal paragraph is a no-op:
    its lines are similar width and no separate box sits between them, so the widened
    cover overlaps nothing new."""
    def _cover(ls):
        return (min(l["x0"] for l in ls), min(l["y0"] for l in ls),
                max(l["x1"] for l in ls), max(l["y1"] for l in ls))

    def _ov(r, s):
        return (min(r[2], s[2]) - max(r[0], s[0]) > 0
                and min(r[3], s[3]) - max(r[1], s[1]) > 0)

    cur = _cover(area)
    new = _cover(area + [ln])
    for a in areas:
        if a is area:
            continue
        ar = _cover(a)
        if _ov(new, ar) and not _ov(cur, ar):
            return True
    return False


def _area_to_box(area: list, ppi: float, family: str = "",
                 otf_bytes: bytes = b"") -> "LineBox":
    """One AREA (line dicts, top->bottom) -> a LineBox in PDF points. Origin = the
    first line's left baseline; cover = the area's union rect (+pad); ``bg`` = the
    area's own local background. ``family``/``otf_bytes`` are THIS area's matched font.
    For >= 2 lines it is a reflowable PARAGRAPH (``box_w`` = column width, ``leading``
    = median baseline-to-baseline gap) so the whole block edits + wraps as one box; a
    single line leaves those unset."""
    em = statistics.median([l["em"] for l in area])
    pad = max(2.0, 0.06 * em)
    x0 = min(l["x0"] for l in area)
    y0 = min(l["y0"] for l in area)
    x1 = max(l["x1"] for l in area)
    y1 = max(l["y1"] for l in area)
    cover_pt = ((x0 - pad) / ppi, (y0 - pad) / ppi,
                (x1 + pad) / ppi, (y1 + pad) / ppi)
    origin_pt = (area[0]["x0"] / ppi, area[0]["baseline"] / ppi)
    # Keep the recognized LINE BREAKS (join with newlines, not spaces): a form
    # block (name / address / phone / DOB) must keep its lines, not reflow into
    # one prose run. The editor shows the lines and the bake draws each at its own
    # baseline; a single-line area has no newline and behaves as before.
    box = LineBox(origin=origin_pt, text="\n".join(l["text"] for l in area),
                  size=em / ppi, confidence=min(l["conf"] for l in area),
                  cover=cover_pt, bg=area[0].get("bg", (1.0, 1.0, 1.0)),
                  family=family, otf_bytes=otf_bytes)
    if len(area) >= 2:
        gaps = [area[i]["baseline"] - area[i - 1]["baseline"]
                for i in range(1, len(area))]
        box.box_w = (x1 - x0) / ppi
        box.leading = statistics.median(gaps) / ppi
        box.is_paragraph = True
        # Each line's OWN tight cover (its ink bbox + the same pad), in text order,
        # so the unified raster can run the single-line in-place engine per line and
        # keep every untouched line's scan pixels 1-for-1. Each carries its OWN local
        # background, so a line over a shaded cell blends to that cell, not line 0's.
        box.line_covers = [
            ((l["x0"] - pad) / ppi, (l["y0"] - pad) / ppi,
             (l["x1"] + pad) / ppi, (l["y1"] + pad) / ppi)
            + tuple(l.get("bg", box.bg))
            for l in area]
    return box
