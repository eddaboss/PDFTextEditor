"""On-device OCR that turns image-only pages into editable text rendered in a
custom font built from the scanned glyphs (OCR_SPEC).

Public entry point: ``recognize_and_reconstruct`` -- runs entirely off the GUI
thread (the caller renders the page to RGB on the GUI thread, applies the result
back on it). Returns a ``ReconResult`` (scan-font OTF + placed line boxes) or
None when no text is recovered.
"""

from __future__ import annotations

import numpy as np

from .engine import OcrLine, get_engine
from .reconstruct import LineBox, ReconResult, reconstruct_page

__all__ = [
    "OcrLine", "LineBox", "ReconResult",
    "get_engine", "reconstruct_page", "recognize_and_reconstruct",
]


def recognize_and_reconstruct(
    image_rgb: "np.ndarray", dpi: float,
    base_font_serif: str, base_font_sans: str,
    engine_name: str = "auto",
    family_label: str = "Scanned Text",
) -> "ReconResult | None":
    """Recognize ``image_rgb`` (page raster at ``dpi``) and rebuild it as editable
    text in a scan-built font. ``base_font_serif`` / ``base_font_sans`` are font
    file paths used to borrow glyphs the page never showed. Pure CPU; safe to run
    on a worker thread."""
    engine = get_engine(engine_name)
    lines = engine.recognize(image_rgb)
    if not lines:
        return None
    return reconstruct_page(
        image_rgb, dpi, lines,
        base_font_serif=base_font_serif, base_font_sans=base_font_sans,
        family_label=family_label,
    )
