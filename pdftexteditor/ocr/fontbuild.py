"""Build a real, embeddable OpenType (CFF) font from the glyph bitmaps of a
scanned page -- the "ClearScan-class" core of the OCR feature (OCR_SPEC §8,
Tier 1).

Each character that ACTUALLY appeared on the page is traced from its own
scanned bitmap (cv2 binarize -> vtracer bitmap->vector -> fontTools T2 charstring)
so the rebuilt text reproduces the exact scanned letterforms WITH their
distortions. Characters the page never showed are BORROWED from a real base
font (Times/Helvetica), scaled to the scan's measured x-height, so the font
covers the whole working charset and editing never produces a blank box
(OCR_SPEC "missing-character handling").

Coordinate model (the part the earlier proof got wrong):
  * The font em is ``UPM`` units tall. A glyph drawn at ``size`` points is
    ``px_per_em = size * dpi/72`` pixels tall on the page render, so one source
    pixel is ``k = UPM / px_per_em`` font units.
  * Every glyph is positioned against its LINE's baseline, not its own box:
    a source point ``(px, py)`` in the ink-tight crop maps to
    ``x_em = k*px``, ``y_em = k*(baseline_row - py)`` -- baseline at y_em=0,
    ascenders positive. Using the shared line baseline (``baseline_row`` is the
    crop-top->baseline distance) is what stops glyphs from jumping vertically.
  * The pen origin is the glyph's ink-left, so the advance is the typical
    ink-left-to-next-ink-left distance (``advance_px``); that reproduces the
    original inter-glyph spacing on average and feeds ``_newbox_bbox`` widths.

CPU-only, no ML. Pure dependencies: cv2 (binarize), vtracer (trace), fontTools
(assemble). Returns OTF bytes that ``FontEngine.register_custom_face`` embeds.
"""

from __future__ import annotations

import io
import logging
from dataclasses import dataclass

# fontTools logs benign INFO/WARNING chatter when building/round-tripping our
# synthetic font (head timestamp range, skipped composite refs). The output is
# verified correct elsewhere, so keep its logger quiet.
logging.getLogger("fontTools").setLevel(logging.ERROR)

import numpy as np
import vtracer
from fontTools.fontBuilder import FontBuilder
from fontTools.misc.transform import Transform
from fontTools.pens.t2CharStringPen import T2CharStringPen
from fontTools.pens.transformPen import TransformPen
from fontTools.pens.boundsPen import ControlBoundsPen
from fontTools.svgLib.path import parse_path

UPM = 1000              # font units per em
_DEF_ASCENT = 0.80      # fraction of em above baseline (typo/win ascent)
_DEF_DESCENT = 0.20     # fraction of em below baseline (magnitude)


@dataclass
class GlyphSample:
    """One representative scanned glyph, ink-tight.

    ``bitmap`` is a 2-D boolean array (True == ink), the smallest box that
    contains the glyph's ink. ``baseline_row`` is the distance in source pixels
    from the TOP of ``bitmap`` down to the text baseline (so a descender has
    ``baseline_row < bitmap.height``; an x-height letter has
    ``baseline_row ~= bitmap.height``). ``advance_px`` is the glyph's typical
    pen advance in source pixels (ink-left to the next glyph's ink-left).
    """

    char: str
    bitmap: np.ndarray
    baseline_row: float
    advance_px: float
    lsb_px: float = 0.0      # pen-origin -> ink-left distance (left side bearing)


def _glyph_name(ch: str) -> str:
    """A safe, unique glyph name for a character (uniXXXX / uXXXXXX)."""
    cp = ord(ch)
    return f"uni{cp:04X}" if cp <= 0xFFFF else f"u{cp:06X}"


_PAD = 2


def _trace_bitmap(bitmap: np.ndarray) -> "list":
    """Vectorize an ink-tight boolean bitmap via vtracer.

    Returns a list of ``(d, tx, ty)``: each SVG sub-path's data string plus the
    ``translate(tx,ty)`` vtracer attaches to position that contour (vtracer
    emits ONE <path> per contour, each in its own frame -- dropping the
    translate collapses multi-contour glyphs onto the origin). Ink is rendered
    black on white and traced in binary mode.
    """
    h, w = bitmap.shape
    if h == 0 or w == 0 or not bitmap.any():
        return []
    # Pad so contours never clip the bitmap edge (keeps vtracer's outline closed
    # around glyphs whose ink touches the crop border).
    canvas = np.zeros((h + 2 * _PAD, w + 2 * _PAD), dtype=bool)
    canvas[_PAD:_PAD + h, _PAD:_PAD + w] = bitmap
    H, W = canvas.shape
    # vtracer wants flat RGBA pixels: ink -> black opaque, bg -> white opaque.
    rgba = np.empty((H, W, 4), dtype=np.uint8)
    rgba[..., :3] = np.where(canvas[..., None], 0, 255)
    rgba[..., 3] = 255
    pixel_list = [tuple(int(v) for v in px) for px in rgba.reshape(-1, 4)]
    svg = vtracer.convert_pixels_to_svg(
        pixel_list, size=(W, H), colormode="binary",
        mode="spline", filter_speckle=2, corner_threshold=60,
        path_precision=3,
    )
    return _svg_paths(svg)


def _svg_paths(svg: str) -> "list":
    """Extract ``(d, tx, ty)`` per <path> from vtracer's SVG (d-string + its
    translate offset)."""
    import re
    out = []
    for m in re.finditer(r'<path\b[^>]*?\bd="([^"]+)"[^>]*?>', svg):
        tag = m.group(0)
        d = m.group(1)
        tm = re.search(r'translate\(\s*([-\d.]+)[,\s]+([-\d.]+)\s*\)', tag)
        tx = float(tm.group(1)) if tm else 0.0
        ty = float(tm.group(2)) if tm else 0.0
        out.append((d, tx, ty))
    return out


def _charstring(sample: GlyphSample, k: float, glyph_set) -> "tuple":
    """Build a T2 charstring for one traced glyph. Returns (charstring, advance,
    xmin) or None when the glyph traced empty."""
    paths = _trace_bitmap(sample.bitmap)
    if not paths:
        return None
    pad = 2
    advance = max(1, round(sample.advance_px * k))
    pen = T2CharStringPen(advance, glyph_set)
    # Map vtracer pixel space (origin top-left, +y down, padded by `_PAD`) to font
    # units. The pen origin (x_em=0) sits ``lsb_px`` LEFT of the ink-tight crop's
    # left edge, so the glyph keeps its real left side bearing; baseline at y=0.
    #   x_em = k*(px - _PAD) + k*lsb_px ;  y_em = k*(baseline_row - (py - _PAD))
    tf = Transform(k, 0.0, 0.0, -k,
                   k * (sample.lsb_px - _PAD),
                   k * (sample.baseline_row + _PAD))
    _draw_paths(paths, tf, pen)
    cs = pen.getCharString()
    bp = ControlBoundsPen(glyph_set)
    try:
        _draw_paths(paths, tf, bp)
        xmin = bp.bounds[0] if bp.bounds else 0
    except Exception:
        xmin = 0
    return cs, advance, int(round(xmin))


def _draw_paths(paths, tf, pen) -> None:
    """Replay each ``(d, tx, ty)`` contour through ``tf`` pre-translated by the
    contour's own vtracer offset."""
    for d, tx, ty in paths:
        parse_path(d, TransformPen(pen, tf.translate(tx, ty)))


class _GlyphSetStub(dict):
    """T2CharStringPen/ControlBoundsPen only need a mapping-like glyph set to
    look up component glyphs; traced glyphs have no components, so an empty dict
    suffices."""


def build_font(samples: "dict[str, GlyphSample]", px_per_em: float,
               family_name: str, charset: "set[str]",
               base_font_path: "str | None" = None,
               xheight_px: "float | None" = None,
               space_px: "float | None" = None) -> bytes:
    """Assemble an OTF (CFF) from ``samples`` covering ``charset``.

    ``px_per_em`` ties source pixels to font units (= size_pt * dpi/72 of the
    cluster the samples came from). Observed chars are traced; the rest of
    ``charset`` is borrowed from ``base_font_path`` (scaled to ``xheight_px``)
    when given. Returns OTF bytes ready for ``page.insert_font(fontbuffer=...)``
    / ``FontEngine.register_custom_face``.
    """
    if px_per_em <= 0:
        raise ValueError("px_per_em must be positive")
    k = UPM / px_per_em
    gs = _GlyphSetStub()

    charstrings: dict[str, object] = {}
    metrics: dict[str, tuple] = {}
    cmap: dict[int, str] = {}
    order: list[str] = [".notdef"]

    # .notdef: empty box-less glyph with a nominal advance.
    notdef_pen = T2CharStringPen(round(0.5 * UPM), gs)
    charstrings[".notdef"] = notdef_pen.getCharString()
    metrics[".notdef"] = (round(0.5 * UPM), 0)

    traced: set[str] = set()
    for ch, sample in samples.items():
        if ch in (" ", "\t") or ch not in charset:
            continue
        built = _charstring(sample, k, gs)
        if built is None:
            continue
        cs, advance, xmin = built
        name = _glyph_name(ch)
        if name in charstrings:
            continue
        charstrings[name] = cs
        metrics[name] = (advance, xmin)
        cmap[ord(ch)] = name
        order.append(name)
        traced.add(ch)

    # Borrow every still-missing char from the base font, scaled to match the
    # scan's x-height so the inserted letters sit at the right size.
    missing = sorted(c for c in charset
                     if c not in traced and c not in (" ", "\t"))
    if missing and base_font_path:
        _add_borrowed(missing, base_font_path, samples, traced, xheight_px,
                      px_per_em, k, charstrings, metrics, cmap, order, gs)

    # Always provide a space (advance only). Prefer a measured space width;
    # else fall back to a fraction of the median glyph advance.
    space_adv = (max(1, round(space_px * k)) if space_px
                 else _estimate_space_advance(samples, k, px_per_em))
    sp_pen = T2CharStringPen(space_adv, gs)
    charstrings["space"] = sp_pen.getCharString()
    metrics["space"] = (space_adv, 0)
    cmap[0x20] = "space"
    order.append("space")

    fb = FontBuilder(unitsPerEm=UPM, isTTF=False)
    fb.setupGlyphOrder(order)
    fb.setupCharacterMap(cmap)
    ps_name = "".join(c for c in family_name if c.isalnum()) or "ScanFont"
    fb.setupCFF(ps_name, {"FullName": family_name, "FamilyName": family_name,
                          "Weight": "Regular"}, charstrings, {})
    fb.setupHorizontalMetrics(metrics)
    asc = round(_DEF_ASCENT * UPM)
    desc = round(_DEF_DESCENT * UPM)
    fb.setupHorizontalHeader(ascent=asc, descent=-desc)
    fb.setupNameTable({
        "familyName": family_name,
        "styleName": "Regular",
        "psName": ps_name,
        "fullName": family_name,
    })
    fb.setupOS2(sTypoAscender=asc, sTypoDescender=-desc, sTypoLineGap=0,
                usWinAscent=asc, usWinDescent=desc,
                sCapHeight=asc, sxHeight=round(0.5 * UPM))
    fb.setupPost(isFixedPitch=0, underlinePosition=-100, underlineThickness=50)
    # Stamp a fixed, loader-accepted head timestamp (seconds since the 1904 Mac
    # epoch) and stop fontTools rewriting `modified` to "now" on save, so MuPDF
    # does not warn about an out-of-range timestamp on the embedded font.
    fb.font.recalcTimestamp = False
    try:
        fb.font["head"].created = fb.font["head"].modified = 2732793282
    except Exception:
        pass
    buf = io.BytesIO()
    fb.font.save(buf)
    return buf.getvalue()


def _estimate_space_advance(samples, k, px_per_em) -> int:
    """A reasonable space width: ~quarter em, or the median glyph advance/2."""
    advs = [s.advance_px for s in samples.values() if s.advance_px > 0]
    if advs:
        med = float(np.median(advs))
        return max(1, round(0.55 * med * k))
    return round(0.25 * UPM)


def _add_borrowed(missing, base_font_path, samples, traced, xheight_px,
                  px_per_em, k, charstrings, metrics, cmap, order, gs) -> None:
    """Copy outlines for ``missing`` chars from the base font, scaled so the
    base font's x-height matches the scan's measured ``xheight_px`` (em units).
    Borrowed glyphs sit on the same baseline (base fonts are already
    baseline-relative)."""
    from fontTools.ttLib import TTFont
    from fontTools.pens.recordingPen import DecomposingRecordingPen
    base = TTFont(base_font_path, fontNumber=0, lazy=True)
    base_upm = base["head"].unitsPerEm
    base_glyphs = base.getGlyphSet()
    base_cmap = base.getBestCmap()
    bx = base.get("OS/2")
    base_xheight = (bx.sxHeight if bx is not None and getattr(bx, "sxHeight", 0)
                    else 0.5 * base_upm)
    # Target x-height in em units. xheight_px is the scan's measured x-height in
    # source px; convert to em with k. Fall back to 0.5em.
    target_xh = (xheight_px * k) if xheight_px else 0.5 * UPM
    scale = (target_xh / base_xheight) if base_xheight else (UPM / base_upm)
    for ch in missing:
        cp = ord(ch)
        gname = base_cmap.get(cp)
        if gname is None:
            continue
        name = _glyph_name(ch)
        if name in charstrings:
            continue
        pen = T2CharStringPen(0, gs)
        tf = Transform(scale, 0, 0, scale, 0, 0)
        tpen = TransformPen(pen, tf)
        try:
            # Decompose composites (accents, some punctuation) into contours so
            # the borrowed glyph has no dangling component references, then
            # replay through the scale transform into the CFF pen.
            rec = DecomposingRecordingPen(base_glyphs)
            base_glyphs[gname].draw(rec)
            rec.replay(tpen)
        except Exception:
            continue
        adv = round(base_glyphs[gname].width * scale) if hasattr(
            base_glyphs[gname], "width") else round(0.5 * UPM)
        try:
            adv = round(base["hmtx"][gname][0] * scale)
        except Exception:
            pass
        cs = pen.getCharString()
        charstrings[name] = cs
        metrics[name] = (max(1, adv), 0)
        cmap[cp] = name
        order.append(name)
    base.close()
