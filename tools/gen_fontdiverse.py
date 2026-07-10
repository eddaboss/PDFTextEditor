#!/usr/bin/env python3
"""Generate a FONT-DIVERSE degraded-line corpus to test the edit degradation across many
fonts (the "any font" axis) with CLEAN, high-res inputs -- avoiding the OCR-on-low-res
garbage that made the ShabbyPages paper samples unscorable. Each line is rendered in a
different Google/system font and degraded INDEPENDENTLY (datagen's parametric degradation,
which is unrelated to the app's apply_measured_damage), then saved as a PNG the gate can
leave-one-glyph-out. Run tools/leaveoneout.py on the output folder afterwards.

    python tools/gen_fontdiverse.py [n_fonts] [level]
"""
import os
import sys
import random

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.append(os.path.expanduser("~/Documents/GitHub/PDFTextEditor-ocr"))

import numpy as np
import cv2
import fitz
from scan_degrade import datagen


def latin_ok(fp):
    """True only if the font actually has the Latin glyphs our test line needs (skips the
    Arabic/CJK/Telugu/emoji/symbol fonts in Google Fonts that would render as tofu)."""
    try:
        f = fitz.Font(fontfile=fp)
        return all(f.has_glyph(ord(c)) for c in "AaGgRe05789")
    except Exception:
        return False

OUT = os.path.expanduser("~/Documents/ocr_assets/fontdiverse")
TEXT = "Order 0123 ABCDE fghij 4567 Sample 89"     # broad glyph coverage for leave-one-out


def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 18
    level = sys.argv[2] if len(sys.argv) > 2 else "medium"
    rng = random.Random(20260625)
    fonts = datagen.discover_fonts()
    # spread across the (alphabetised) list for serif/sans/mono variety, but keep only Latin-
    # capable fonts (walk the spread, testing each, until we have n).
    step = max(1, len(fonts) // (n * 6))
    picks = []
    for fp in fonts[::step]:
        if latin_ok(fp):
            picks.append(fp)
        if len(picks) >= n:
            break
    os.makedirs(OUT, exist_ok=True)
    made = 0
    for i, fp in enumerate(picks):
        ink = [rng.randint(15, 55)] * 3
        paper = [rng.randint(245, 254)] * 3
        clean = datagen.render_clean(TEXT, fp, 52, ink, paper)
        if clean is None or clean.shape[1] < 60:
            continue
        spec = datagen.level_spec(rng, level)
        deg = datagen.apply_spec(clean, spec, seed=1000 + i)
        out = os.path.join(OUT, f"fd_{i:02d}.png")
        cv2.imwrite(out, cv2.cvtColor(deg, cv2.COLOR_RGB2BGR))
        made += 1
        print(f"  fd_{i:02d}  {os.path.basename(fp)[:40]}")
    print(f"wrote {made} degraded font-diverse lines to {OUT}")


if __name__ == "__main__":
    main()
