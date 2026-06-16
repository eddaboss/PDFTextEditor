"""On-device OCR that turns image-only pages into editable text rendered in a
custom font built from the scanned glyphs (OCR_SPEC).

Public entry point: ``recognize_and_reconstruct`` -- runs entirely off the GUI
thread (the caller renders the page to RGB on the GUI thread, applies the result
back on it). Returns a ``ReconResult`` (scan-font OTF + placed line boxes) or
None when no text is recovered.
"""

from __future__ import annotations

from . import pack
from .engine import OcrLine, OcrPackMissing, get_engine

# Put an already-downloaded OCR pack on sys.path as soon as the package loads, so
# a returning user's OCR just works. The heavy reconstruct deps (opencv, vtracer)
# and the RapidOCR engine live in that pack, so this module imports them LAZILY
# (inside recognize_and_reconstruct, after the pack is confirmed on the path);
# importing pdftexteditor.ocr itself never pulls the pack in.
pack.ensure_on_path()

__all__ = [
    "OcrLine", "OcrPackMissing", "pack",
    "get_engine", "recognize_and_reconstruct",
]


def recognize_and_reconstruct(
    image_rgb, dpi: float,
    base_font_serif: str, base_font_sans: str,
    engine_name: str = "auto",
    family_label: str = "Scanned Text",
):
    """Recognize ``image_rgb`` (page raster at ``dpi``) and rebuild it as editable
    text in a scan-built font. ``base_font_serif`` / ``base_font_sans`` are font
    file paths used to borrow glyphs the page never showed. Pure CPU; safe to run
    on a worker thread. Raises ``OcrPackMissing`` if the downloadable OCR
    component is not installed (the UI offers the download before calling this)."""
    if not pack.ensure_on_path():
        raise OcrPackMissing("The OCR component is not installed.")
    from .reconstruct import reconstruct_page  # needs the pack (opencv, vtracer)
    engine = get_engine(engine_name)
    lines = engine.recognize(image_rgb)
    if not lines:
        return None
    return reconstruct_page(
        image_rgb, dpi, lines,
        base_font_serif=base_font_serif, base_font_sans=base_font_sans,
        family_label=family_label,
    )
