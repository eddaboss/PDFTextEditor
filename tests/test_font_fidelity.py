"""Font-fidelity tests for pdftexteditor.font_engine.

For every fixture under tests/fixtures/ this exercises the full edit -> resolve
-> save -> re-render path twice per fixture:

  * a SAME-GLYPHS edit  (new text drawn only from characters already present)
  * a NEW-GLYPHS  edit  (characters outside the original run, e.g. "Zq9 Kwx")

and asserts, for each:

  1. resolve(...) raises no exception and the save round-trips with no exception;
  2. the original span text is GONE from the saved page;
  3. the new text is PRESENT -- proven the tofu-proof way: the actually-resolved
     font object covers every glyph of the new string (font_covers == True), AND
     the edited region carries real ink after the save (glyphs were drawn);
  4. the chosen tier is the EMBEDDED original whenever the embedded buffer can
     supply the glyphs (NOT base-14). The lone subset fixture is the documented
     exception: its buffer reports valid_codepoints()==0, so it MUST fall through
     to the full system font of the SAME family (Tier 2) -- never base-14.

Before/after PNGs land in tests/screenshots/font/ for visual confirmation.

Run:
    QT_QPA_PLATFORM=offscreen \
      /Users/edward/Documents/GitHub/PDFTextEditor/.venv/bin/python \
      tests/test_font_fidelity.py
"""

from __future__ import annotations

import os
import sys
import tempfile
import traceback

import fitz

# Run headless Qt no matter how the test is launched.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from PySide6.QtWidgets import QApplication  # noqa: E402

# A QApplication must exist before QFont* objects are constructed.
_APP = QApplication.instance() or QApplication([])

# Headless safety net: the offscreen Qt platform on Windows does not auto-load
# the OS font database, so the SYSTEM-tier fixtures' families report unavailable
# to Qt and resolve falls to BASE14. Register them so a headless run exercises
# the same SYSTEM-tier path the real GUI app does.
from pdftexteditor.font_engine import FontEngine as _FE  # noqa: E402
_FE.register_system_fonts_with_qt(
    ("Arial", "Times New Roman", "Georgia", "Comic Sans MS"))

from pdftexteditor.font_engine import (  # noqa: E402
    FontEngine,
    TIER_BASE14,
    TIER_EMBEDDED,
    TIER_SYSTEM,
    face_bytes,
)

FIXTURE_DIR = os.path.join(_HERE, "fixtures")
SHOT_DIR = os.path.join(_HERE, "screenshots", "font")
os.makedirs(SHOT_DIR, exist_ok=True)

_TIER_NAME = {TIER_EMBEDDED: "EMBEDDED", TIER_SYSTEM: "SYSTEM", TIER_BASE14: "BASE14"}

# Fixtures and which span (by its rawdict font name) each scenario edits, plus
# the SAME-GLYPHS and NEW-GLYPHS replacement strings.
#
# subset_font is the documented fallback fixture: its embedded buffer is
# glyph-stripped (valid_codepoints()==0), so BOTH edits must resolve to the
# full system Comic Sans MS (Tier 2), never base-14.
SCENARIOS = [
    {
        "fixture": "body_paragraphs",
        "font": "TimesNewRomanPSMT",
        "same": "boats nudged the moorings",   # ascii the run already uses
        "new": "Zephyr Qophs 1234 Kwxj",       # fresh glyphs
        "expect_tier": TIER_EMBEDDED,
    },
    {
        "fixture": "multi_size",
        "font": "ArialMT",
        "same": "Quarterly Report",
        "new": "Zephyr Brief 2024 Kx",
        "expect_tier": TIER_EMBEDDED,
    },
    {
        "fixture": "bold_italic",
        "font": "Georgia-Bold",
        "same": "This bold restated",
        "new": "Zephyr Qx 1234 Kwj",
        "expect_tier": TIER_EMBEDDED,
    },
    {
        "fixture": "colored_text",
        "font": "Arial-ItalicMT",
        "same": "Recolored italic",
        "new": "Zephyr Qx 1234 Kwj",
        "expect_tier": TIER_EMBEDDED,
    },
    {
        "fixture": "subset_font",
        "font": "Comic Sans MS Regular",
        "same": "Subset restated",
        "new": "Zephyr Qophs 1234 Kwxj",
        "expect_tier": TIER_SYSTEM,   # the fallback case (subset buffer can't cover)
    },
    {
        "fixture": "mixed_families",
        "font": "ComicSansMS",
        "same": "A friendly heading",
        "new": "Zephyr Qx 1234 Kwj",
        "expect_tier": TIER_EMBEDDED,
    },
]


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def grab_span(path: str, want_font: str) -> dict | None:
    """Return the first non-empty span on page 0 whose rawdict font == want_font."""
    doc = fitz.open(path)
    try:
        for block in doc[0].get_text("rawdict")["blocks"]:
            if block.get("type", 0) != 0:
                continue
            for line in block["lines"]:
                for span in line["spans"]:
                    text = "".join(c["c"] for c in span.get("chars", []))
                    if text.strip() and span["font"] == want_font:
                        return {
                            "font": span["font"],
                            "flags": span["flags"],
                            "bbox": tuple(span["bbox"]),
                            "origin": tuple(span["origin"]),
                            "size": span["size"],
                            "color": tuple(
                                c / 255 for c in fitz.sRGB_to_rgb(span["color"])
                            ),
                            "text": text,
                        }
        return None
    finally:
        doc.close()


def resolved_font_object(engine: FontEngine, rf) -> "fitz.Font":
    """The exact fitz.Font the save path draws with, per tier."""
    if rf.tier == TIER_EMBEDDED:
        return fitz.Font(fontbuffer=rf.pdf_fontbuffer)
    if rf.tier == TIER_SYSTEM:
        rec = engine.system_record_for(rf.qt_family, rf.qt_bold, rf.qt_italic)
        return fitz.Font(fontbuffer=face_bytes(rec.path, rec.face_index))
    return fitz.Font(rf.base14_code)


def save_with_edit(src: str, span: dict, new_text: str, out_path: str):
    """Apply one staged edit through the resolver and write out_path.

    Mirrors BUILD_SPEC §4.3 exactly: redact the original FIRST (apply_redactions
    rebuilds the resource dict and drops pre-registered fonts), then resolve and
    reinsert at the original baseline. Returns the ResolvedFont.
    """
    out = fitz.open(src)
    engine = FontEngine(out)
    page = out[0]

    rf = engine.resolve(0, span["font"], span["flags"], new_text)

    page.add_redact_annot(fitz.Rect(span["bbox"]))
    page.apply_redactions()

    if rf.tier == TIER_EMBEDDED:
        page.insert_font(fontname=rf.pdf_fontname, fontbuffer=rf.pdf_fontbuffer)
        fontname = rf.pdf_fontname
    elif rf.tier == TIER_SYSTEM:
        rec = engine.system_record_for(rf.qt_family, rf.qt_bold, rf.qt_italic)
        page.insert_font(
            fontname=rf.pdf_fontname,
            fontbuffer=face_bytes(rec.path, rec.face_index),
        )
        fontname = rf.pdf_fontname
    else:  # TIER_BASE14
        page.insert_font(fontname=rf.base14_code)
        fontname = rf.base14_code

    # origin IS the baseline point in PDF points; insert there directly.
    page.insert_text(
        fitz.Point(span["origin"]),
        new_text,
        fontsize=span["size"],
        fontname=fontname,
        color=span["color"],
    )

    fd, tmp = tempfile.mkstemp(suffix=".pdf", dir=os.path.dirname(out_path) or ".")
    os.close(fd)
    out.save(tmp, garbage=4, deflate=True)
    out.close()
    os.replace(tmp, out_path)
    return rf


def region_ink(path: str, bbox: tuple, scale: float = 3.0) -> int:
    """Count non-white (drawn) pixels in a PDF region; proves glyphs were laid
    down. Uses a luminance test, not a single channel, so COLORED text (e.g.
    crimson, which has a high red channel) is counted as ink too."""
    doc = fitz.open(path)
    try:
        pix = doc[0].get_pixmap(
            matrix=fitz.Matrix(scale, scale),
            clip=fitz.Rect(bbox),
            alpha=False,
        )
        samples = pix.samples
        step = pix.n  # 3 for RGB
        count = 0
        for i in range(0, len(samples), step):
            r, g, b = samples[i], samples[i + 1], samples[i + 2]
            # Rec. 601 luma; anything materially off white background is ink.
            luma = 0.299 * r + 0.587 * g + 0.114 * b
            if luma < 230:
                count += 1
        return count
    finally:
        doc.close()


def render_png(path: str, out_png: str, scale: float = 2.0) -> None:
    doc = fitz.open(path)
    try:
        doc[0].get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False).save(out_png)
    finally:
        doc.close()


# --------------------------------------------------------------------------
# The test body
# --------------------------------------------------------------------------
def run_scenario(scn: dict) -> list[str]:
    """Run both edits for one fixture; return a list of failure strings (empty == pass)."""
    failures: list[str] = []
    fixture = scn["fixture"]
    src = os.path.join(FIXTURE_DIR, f"{fixture}.pdf")
    span = grab_span(src, scn["font"])
    if span is None:
        return [f"{fixture}: could not find span with font {scn['font']!r}"]

    # Baseline before-edit screenshot once per fixture.
    render_png(src, os.path.join(SHOT_DIR, f"{fixture}_before.png"))

    for variant, new_text in (("same", scn["same"]), ("new", scn["new"])):
        tag = f"{fixture}[{variant}]"
        out_path = os.path.join(
            tempfile.gettempdir(), f"fontfidelity_{fixture}_{variant}.pdf"
        )
        try:
            engine = FontEngine(fitz.open(src))
            rf = engine.resolve(0, span["font"], span["flags"], new_text)
        except Exception:
            failures.append(f"{tag}: resolve() raised:\n{traceback.format_exc()}")
            continue

        # --- 4. tier expectation -------------------------------------------
        if rf.tier == TIER_BASE14:
            failures.append(
                f"{tag}: fell to BASE14 ({rf.source_name}); the original family "
                f"should reproduce via EMBEDDED or SYSTEM."
            )
        if rf.tier != scn["expect_tier"]:
            failures.append(
                f"{tag}: tier {_TIER_NAME[rf.tier]} != expected "
                f"{_TIER_NAME[scn['expect_tier']]}"
            )

        # --- 3a. tofu-proof: the resolved font covers EVERY new glyph ------
        try:
            font_obj = resolved_font_object(engine, rf)
        except Exception:
            failures.append(f"{tag}: building resolved font raised:\n{traceback.format_exc()}")
            continue
        if not FontEngine.font_covers(font_obj, new_text):
            failures.append(
                f"{tag}: resolved font {rf.source_name!r} does NOT cover "
                f"{new_text!r} -> would emit .notdef tofu."
            )

        # --- 1. save round-trips with no exception -------------------------
        try:
            rf_saved = save_with_edit(src, span, new_text, out_path)
        except Exception:
            failures.append(f"{tag}: save_with_edit raised:\n{traceback.format_exc()}")
            continue
        if rf_saved.tier != rf.tier:
            failures.append(
                f"{tag}: save tier {_TIER_NAME[rf_saved.tier]} != resolve tier "
                f"{_TIER_NAME[rf.tier]} (resolver not deterministic)"
            )

        saved = fitz.open(out_path)
        try:
            page_text = saved[0].get_text()
        finally:
            saved.close()

        # --- 2. old text gone ----------------------------------------------
        if span["text"].strip() and span["text"] in page_text:
            failures.append(f"{tag}: original text still present after redaction.")

        # --- 3b. new text present: real ink in the edited region -----------
        ink = region_ink(out_path, span["bbox"])
        if ink < 50:
            failures.append(
                f"{tag}: edited region has almost no ink ({ink} px) -> "
                f"new text not drawn."
            )
        # Where extraction round-trips (system / base14 faces carry ToUnicode),
        # also assert the literal string is back. Embedded subset buffers draw
        # by GID without a round-tripping ToUnicode map, so this check is gated
        # on tier to avoid a false negative on a visually-correct edit.
        if rf.tier in (TIER_SYSTEM, TIER_BASE14):
            if new_text not in page_text:
                failures.append(
                    f"{tag}: new text {new_text!r} not extractable from the "
                    f"saved {_TIER_NAME[rf.tier]} edit."
                )

        # --- after screenshot ---------------------------------------------
        render_png(out_path, os.path.join(SHOT_DIR, f"{fixture}_{variant}_after.png"))

        print(
            f"  {tag:28} tier={_TIER_NAME[rf.tier]:8} "
            f"qt={rf.qt_family!r:18} ink={ink:5} covers=OK"
        )

    return failures


def main() -> int:
    print("Font-fidelity tests (resolve -> save -> re-render)\n")
    all_failures: list[str] = []
    for scn in SCENARIOS:
        print(f"[{scn['fixture']}]")
        all_failures.extend(run_scenario(scn))
        print()

    print("=" * 64)
    if all_failures:
        print(f"FAILED ({len(all_failures)} assertion failure(s)):\n")
        for f in all_failures:
            print(f"  - {f}")
        print(f"\nScreenshots: {SHOT_DIR}")
        return 1

    print("PASSED — all fixtures reproduce their ORIGINAL font (EMBEDDED where")
    print("the buffer covers the glyphs; SYSTEM same-family for the subset case);")
    print("no edit fell to base-14, old text gone, new glyphs drawn, no tofu.")
    print(f"\nScreenshots: {SHOT_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
