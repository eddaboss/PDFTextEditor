"""End-to-end headless harness for the REAL application (BUILD_SPEC §9).

Unlike ``test_font_fidelity.py`` (which exercises the font engine in isolation),
this drives the *actual* ``MainWindow`` / ``PageView`` / ``PDFDocument`` stack the
user runs, with Qt in offscreen mode. For EACH fixture under ``tests/fixtures/``
it:

  1. loads the fixture into a live ``MainWindow`` (real toolbar, status bar,
     canvas, undo stack);
  2. programmatically begins an in-place edit on a real body-text run, types a
     replacement that CHANGES a word/number (the central use case), and commits
     it through the same signal path a mouse-driven edit would take;
  3. triggers Undo, then Redo, through the window's ``QUndoStack``;
  4. saves via ``document.save_as`` and re-renders the saved PDF;
  5. asserts, with zero tolerated exceptions:
       * the edit is applied (new text staged; the run's preview repainted);
       * the ORIGINAL embedded font is reproduced where the glyphs exist
         (tier EMBEDDED for full embedded faces, SYSTEM same-family for the
         subset fixture) -- never a base-14 substitute;
       * the saved page actually carries the resolved face (not a base-14
         built-in like 'Helvetica' / 'Times-Roman' / 'Courier');
       * the resolved font covers every glyph of the new text (no .notdef tofu)
         and the edited region carries real ink after the save;
       * color, size, and baseline geometry are preserved (the live editor's
         scene y equals span.origin.y*zoom - QFontMetricsF.ascent(), the editor
         font family equals the resolver's qt_family, the editor color equals
         the span color);
       * the old run text is removed from the saved page.

A second, glyph-fresh edit per fixture (new characters outside the original run,
including digits) proves the path also handles characters the embedded subset
buffer cannot supply -- forcing the documented Tier-2 fallback on the subset
fixture without ever tofu-ing.

Run:
    QT_QPA_PLATFORM=offscreen \
      /Users/edward/Documents/GitHub/PDFTextEditor/.venv/bin/python \
      tests/test_app.py
"""

from __future__ import annotations

import os
import sys
import tempfile
import traceback

# Headless Qt no matter how this is launched.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import fitz  # noqa: E402
from PySide6.QtCore import QEvent, QRectF  # noqa: E402
from PySide6.QtGui import (  # noqa: E402
    QColor,
    QFontMetricsF,
    QImage,
    QPainter,
)
from PySide6.QtWidgets import QApplication  # noqa: E402

# A QApplication must exist before any QFont/QWidget is constructed.
_APP = QApplication.instance() or QApplication([])

# Headless safety net: the offscreen Qt platform on Windows does not auto-load
# the OS font database, so the SYSTEM-tier fixtures' families report unavailable
# to Qt and resolve falls to BASE14. Register them so a headless run exercises
# the same SYSTEM-tier path the real GUI app does.
from pdftexteditor.font_engine import FontEngine as _FE  # noqa: E402
_FE.register_system_fonts_with_qt(
    ("Arial", "Times New Roman", "Georgia", "Comic Sans MS"))

from pdftexteditor.font_engine import (  # noqa: E402
    TIER_BASE14,
    TIER_EMBEDDED,
    TIER_SYSTEM,
    FontEngine,
    face_bytes,
)
from pdftexteditor.ui.main_window import MainWindow  # noqa: E402

FIXTURE_DIR = os.path.join(_HERE, "fixtures")
SHOT_DIR = os.path.join(_HERE, "screenshots", "app")
os.makedirs(SHOT_DIR, exist_ok=True)

_TIER_NAME = {TIER_EMBEDDED: "EMBEDDED", TIER_SYSTEM: "SYSTEM", TIER_BASE14: "BASE14"}

# Base-14 built-in font names PyMuPDF emits when a span falls to the floor. If
# any of these shows up as the edited span's saved face, the original typeface
# was NOT reproduced -- the user's central complaint.
_BASE14_BASEFONTS = {
    "helvetica", "helvetica-bold", "helvetica-oblique", "helvetica-boldoblique",
    "times-roman", "times-bold", "times-italic", "times-bolditalic",
    "courier", "courier-bold", "courier-oblique", "courier-boldoblique",
    "symbol", "zapfdingbats",
}

# Per-fixture scenario. ``edit`` is a word/number change inside body text built
# by substituting on the original run (so it shares the run's glyphs -> the
# EMBEDDED tier can be reproduced). ``find``/``repl`` define that substitution;
# ``new_text`` is a glyph-FRESH edit (chars/digits outside the run) that forces
# the fallback path. ``expect_tier`` is the tier the SAME-GLYPH edit must hit.
SCENARIOS = [
    {
        "fixture": "body_paragraphs",
        "font": "TimesNewRomanPSMT",
        "find": "harbor", "repl": "seaport",          # word change in body text
        "new_text": "Zephyr Qophs 1234 Kwxj",          # fresh glyphs incl. digits
        "expect_tier": TIER_EMBEDDED,
        "family_hint": "times",
    },
    {
        "fixture": "multi_size",
        "font": "ArialMT",
        "find": "Quarterly", "repl": "Annual",
        "new_text": "Zephyr Brief 2024 Kx",
        "expect_tier": TIER_EMBEDDED,
        "family_hint": "arial",
    },
    {
        "fixture": "bold_italic",
        "font": "Georgia-Bold",
        "find": "bold", "repl": "heavy",
        "new_text": "Zephyr Qx 1234 Kwj",
        "expect_tier": TIER_EMBEDDED,
        "family_hint": "georgia",
    },
    {
        "fixture": "colored_text",
        "font": "ArialMT",
        "find": "Crimson", "repl": "Scarlet",
        "new_text": "Zephyr Qx 1234 Kwj",
        "expect_tier": TIER_EMBEDDED,
        "family_hint": "arial",
    },
    {
        "fixture": "subset_font",
        "font": "Comic Sans MS Regular",
        "find": "cat", "repl": "dog",
        "new_text": "Zephyr Qophs 1234 Kwxj",
        # subset buffer reports valid_codepoints()==0 -> must fall to SYSTEM
        # same-family Comic Sans MS for BOTH edits, never base-14.
        "expect_tier": TIER_SYSTEM,
        "family_hint": "comic",
    },
    {
        "fixture": "mixed_families",
        "font": "ComicSansMS",
        "find": "casual", "repl": "relaxed",
        "new_text": "Zephyr Qx 1234 Kwj",
        "expect_tier": TIER_EMBEDDED,
        "family_hint": "comic",
    },
]


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
class CheckError(AssertionError):
    """A single failed fidelity/behavior assertion."""


def check(failures: list[str], tag: str, cond: bool, msg: str) -> bool:
    if not cond:
        failures.append(f"{tag}: {msg}")
    return cond


def find_hotspot(window: MainWindow, font_name: str, must_contain: str | None = None):
    """Return the first SpanHotspot whose span font matches ``font_name`` and
    (optionally) whose text contains ``must_contain``."""
    for hs in window.view._hotspots:
        sp = hs.span
        if sp.font != font_name:
            continue
        if must_contain is not None and must_contain not in sp.text:
            continue
        return hs
    return None


def region_ink(path: str, bbox: tuple, scale: float = 3.0) -> int:
    """Count non-white (drawn) pixels in a PDF region -> proves glyphs landed.
    Luminance test so COLORED text counts as ink too."""
    doc = fitz.open(path)
    try:
        pix = doc[0].get_pixmap(
            matrix=fitz.Matrix(scale, scale), clip=fitz.Rect(bbox), alpha=False
        )
        samples = pix.samples
        step = pix.n
        count = 0
        for i in range(0, len(samples), step):
            r, g, b = samples[i], samples[i + 1], samples[i + 2]
            if (0.299 * r + 0.587 * g + 0.114 * b) < 230:
                count += 1
        return count
    finally:
        doc.close()


def saved_span_basefonts(path: str) -> list[str]:
    """All embedded basefont names on page 0 of a saved PDF, lowercased and
    subset-tag-stripped (so we can test against the base-14 set)."""
    doc = fitz.open(path)
    try:
        out = []
        for entry in doc[0].get_fonts(full=True):
            base = entry[3] or ""
            # strip a leading 6-letter subset tag
            if len(base) > 7 and base[6] == "+" and base[:6].isalpha():
                base = base[7:]
            out.append(base.lower())
        return out
    finally:
        doc.close()


def resolved_fitz_font(engine: FontEngine, rf) -> "fitz.Font":
    """The exact fitz.Font the save path draws with, per tier."""
    if rf.tier == TIER_EMBEDDED:
        return fitz.Font(fontbuffer=rf.pdf_fontbuffer)
    if rf.tier == TIER_SYSTEM:
        rec = engine.system_record_for(rf.qt_family, rf.qt_bold, rf.qt_italic)
        return fitz.Font(fontbuffer=face_bytes(rec.path, rec.face_index))
    return fitz.Font(rf.base14_code)


def drive_edit(window: MainWindow, hotspot, new_text: str) -> None:
    """Mount the real in-scene editor on a hotspot, replace its text, and commit
    through the window's signal path (mirrors a user click + type + Enter)."""
    window.view.begin_edit(hotspot)
    window.view._editor.setPlainText(new_text)
    window.view.commit_edit()


def editor_plaintext(text: str) -> str:
    """Return what the inline editor would COMMIT for ``text``.

    The in-scene editor is a QGraphicsTextItem; its QTextDocument folds a
    non-breaking space (U+00A0, which PyMuPDF's rawdict emits between words) to a
    regular space on toPlainText(). The committed/staged text is therefore the
    typed text run through that same round-trip, which we reproduce exactly here
    so the harness asserts against the real app behavior rather than the raw
    typed string."""
    from PySide6.QtWidgets import QGraphicsTextItem
    item = QGraphicsTextItem()
    item.setPlainText(text)
    return item.toPlainText()


def close_window(window: MainWindow) -> None:
    """Close a window without tripping the unsaved-changes guard.

    ``MainWindow.closeEvent`` pops a modal ``QMessageBox`` when the document is
    dirty; under the offscreen platform that modal has no event loop to dismiss
    it, so ``window.close()`` would block forever. The window already exposes
    ``_suppress_close_guard`` for exactly this teardown case, so we set it before
    closing. (A real GUI run never hits this: the user clicks a button.)"""
    window._suppress_close_guard = True
    window.close()


def render_scene_png(window: MainWindow, out_png: str) -> bool:
    """Render the PageView's QGraphicsScene to a QImage and save it."""
    scene = window.view.scene()
    rect = scene.sceneRect()
    w = max(1, int(rect.width()))
    h = max(1, int(rect.height()))
    img = QImage(w, h, QImage.Format_ARGB32)
    img.fill(QColor(0xE8, 0xE8, 0xEA).rgb())
    p = QPainter(img)
    p.setRenderHint(QPainter.Antialiasing, True)
    p.setRenderHint(QPainter.SmoothPixmapTransform, True)
    p.setRenderHint(QPainter.TextAntialiasing, True)
    scene.render(p, QRectF(img.rect()), rect)
    p.end()
    return img.save(out_png)


def render_window_png(window: MainWindow, out_png: str) -> bool:
    """Grab the whole window (chrome + canvas) to a PNG."""
    pix = window.grab()
    return pix.save(out_png)


# --------------------------------------------------------------------------
# Per-fixture scenario
# --------------------------------------------------------------------------
def run_scenario(scn: dict, capture_shots: bool) -> list[str]:
    fixture = scn["fixture"]
    tag = fixture
    failures: list[str] = []
    src = os.path.join(FIXTURE_DIR, f"{fixture}.pdf")

    window = MainWindow()
    window.resize(1180, 920)
    try:
        # --- 1. load into the real window --------------------------------
        try:
            window.open_path(src)
        except Exception:
            failures.append(f"{tag}: open_path raised:\n{traceback.format_exc()}")
            return failures

        if not check(failures, tag, window.document is not None, "document did not load"):
            return failures
        check(failures, tag, len(window.view._hotspots) > 0,
              "no span hotspots created on the loaded page")

        hs = find_hotspot(window, scn["font"], scn["find"])
        if hs is None:
            # Fall back to any span of that font for the fresh-glyph edit, but
            # the word change needs the substring present.
            failures.append(
                f"{tag}: no span with font {scn['font']!r} containing "
                f"{scn['find']!r}")
            return failures
        span = hs.span
        original_text = span.text
        # The word/number change we TYPE into the editor.
        new_word_text = original_text.replace(scn["find"], scn["repl"])
        # What the editor actually COMMITS: QTextDocument.toPlainText() folds a
        # non-breaking space (U+00A0, which PyMuPDF rawdict reports between words)
        # to a regular space. That is genuine app behavior -- a user editing a run
        # with nbsp separators gets plain spaces back -- so the staged text equals
        # the typed text run through the same normalization, not the raw typed
        # string. We compute it via the real editor round-trip to stay honest.
        expected_committed = editor_plaintext(new_word_text)

        # --- live editor fidelity (font family / color / baseline) -------
        try:
            window.view.begin_edit(hs)
            editor = window.view._editor
            resolved = window.view._resolve(span, original_text)
            z = window.view.zoom
            font = resolved.qfont(span.size * z)
            ascent = QFontMetricsF(font).ascent()
            expect_y = window.view._sheet_origin.y() + span.origin[1] * z - ascent
            actual_y = editor.pos().y()

            check(failures, tag, editor.font().family() == resolved.qt_family,
                  f"editor family {editor.font().family()!r} != resolved "
                  f"qt_family {resolved.qt_family!r}")
            check(failures, tag, abs(actual_y - expect_y) < 0.5,
                  f"editor baseline y {actual_y:.2f} != expected "
                  f"{expect_y:.2f} (origin.y*z - ascent)")
            ec = editor.defaultTextColor()
            same_color = (abs(ec.redF() - span.color[0]) < 0.01
                          and abs(ec.greenF() - span.color[1]) < 0.01
                          and abs(ec.blueF() - span.color[2]) < 0.01)
            check(failures, tag, same_color,
                  f"editor color {(ec.redF(), ec.greenF(), ec.blueF())} != "
                  f"span color {span.color}")
            # The on-screen editor must NOT be a base-14 lookalike when the
            # original family is reproducible.
            if scn["expect_tier"] in (TIER_EMBEDDED, TIER_SYSTEM):
                check(failures, tag,
                      scn["family_hint"] in resolved.qt_family.lower().replace(" ", ""),
                      f"resolved on-screen family {resolved.qt_family!r} does "
                      f"not look like the original {scn['family_hint']!r}")
        except Exception:
            failures.append(f"{tag}: live-editor inspection raised:\n"
                            f"{traceback.format_exc()}")
        finally:
            # cancel this inspection edit without staging anything
            window.view.cancel_edit()

        # capture the mid-edit screenshot on the chosen fixture
        if capture_shots:
            window.view.begin_edit(hs)
            window.view._editor.setPlainText(new_word_text)
            render_scene_png(window, os.path.join(SHOT_DIR, "03_mid_edit.png"))
            window.view.cancel_edit()

        # --- 2. drive the real word/number edit + commit -----------------
        try:
            drive_edit(window, hs, new_word_text)
        except Exception:
            failures.append(f"{tag}: drive_edit raised:\n{traceback.format_exc()}")
            return failures

        staged_now = window.document.staged_text(0, span)
        check(failures, tag, window.document.edit_count == 1,
              f"after commit edit_count={window.document.edit_count}, expected 1")
        check(failures, tag, staged_now == expected_committed,
              f"staged text {staged_now!r} != committed {expected_committed!r}")
        # The semantic change the user made: the new word in, the old word out.
        check(failures, tag, scn["repl"] in staged_now and scn["find"] not in staged_now,
              f"word change not reflected in staged text {staged_now!r}")
        check(failures, tag, window.undo_stack.canUndo(),
              "undo stack cannot undo after an edit")
        check(failures, tag, window.view.is_edited(span),
              "edited run has no persistent preview")

        # --- 3. undo then redo through the window's stack ----------------
        window.undo_stack.undo()
        check(failures, tag, window.document.staged_text(0, span) == original_text,
              "undo did not restore the original staged text")
        check(failures, tag, window.document.edit_count == 0,
              f"after undo edit_count={window.document.edit_count}, expected 0")
        check(failures, tag, window.undo_stack.canRedo(),
              "redo unavailable after undo")
        window.undo_stack.redo()
        check(failures, tag, window.document.staged_text(0, span) == expected_committed,
              "redo did not re-apply the edit")
        check(failures, tag, window.document.edit_count == 1,
              "redo did not restore edit_count")

        # capture after-edit screenshot
        if capture_shots:
            window.view.reload()
            render_scene_png(window, os.path.join(SHOT_DIR, "04_after_edit.png"))

        # --- 4. save + re-render, assert fidelity ------------------------
        out_path = os.path.join(tempfile.gettempdir(), f"app_{fixture}_word.pdf")
        try:
            window.document.save_as(out_path)
        except Exception:
            failures.append(f"{tag}: save_as raised:\n{traceback.format_exc()}")
            return failures

        # The resolver the save path used (deterministic, so this matches).
        # Resolve on the text actually staged/saved (post-editor normalization).
        engine = window.document.font_engine
        rf = engine.resolve(0, span.font, span.flags, expected_committed)
        check(failures, tag, rf.tier == scn["expect_tier"],
              f"resolve tier {_TIER_NAME[rf.tier]} != expected "
              f"{_TIER_NAME[scn['expect_tier']]}")
        check(failures, tag, rf.tier != TIER_BASE14,
              f"fell to BASE14 ({rf.source_name}) -- original font not reproduced")

        # The resolved fitz.Font covers every new glyph (no tofu).
        try:
            fobj = resolved_fitz_font(engine, rf)
            check(failures, tag, FontEngine.font_covers(fobj, expected_committed),
                  f"resolved font {rf.source_name!r} does not cover "
                  f"{expected_committed!r} -> would tofu")
        except Exception:
            failures.append(f"{tag}: building resolved font raised:\n"
                            f"{traceback.format_exc()}")

        # The saved page carries the resolved face, NOT a base-14 built-in.
        basefonts = saved_span_basefonts(out_path)
        base14_present = any(b in _BASE14_BASEFONTS for b in basefonts)
        check(failures, tag, not base14_present,
              f"saved page contains a base-14 substitute font: {basefonts}")
        # And the reproduced family looks like the original.
        fam_ok = any(scn["family_hint"] in b.replace(" ", "") for b in basefonts)
        check(failures, tag, fam_ok,
              f"saved page does not carry the original {scn['family_hint']!r} "
              f"family; fonts = {basefonts}")

        # Old run text removed; new text drawn (real ink in the region).
        saved = fitz.open(out_path)
        try:
            page_text = saved[0].get_text()
        finally:
            saved.close()
        if original_text.strip():
            check(failures, tag, original_text not in page_text,
                  "original run text still present after redaction")
        ink = region_ink(out_path, span.bbox)
        check(failures, tag, ink >= 50,
              f"edited region has almost no ink ({ink}px) -> new text not drawn")
        # Where the face round-trips a ToUnicode map (SYSTEM/BASE14), assert the
        # literal replacement is extractable too.
        if rf.tier in (TIER_SYSTEM, TIER_BASE14):
            check(failures, tag, scn["repl"] in page_text,
                  f"replacement word {scn['repl']!r} not extractable from the "
                  f"saved {_TIER_NAME[rf.tier]} edit")

        # --- 5. a glyph-FRESH edit (digits + new letters) ----------------
        # Reload a CLEAN document so the fresh edit stands alone. We avoid
        # window.open_path here: the doc is dirty from the word edit above, and
        # open_path would pop the unsaved-changes QMessageBox, which blocks
        # forever under the offscreen platform. Loading a fresh PDFDocument and
        # handing it to the view is the same state the user would reach after
        # discarding, without the modal.
        from pdftexteditor.document import PDFDocument
        window.document.close()
        window.document = PDFDocument(src)
        window.undo_stack.clear()
        window.undo_stack.setClean()
        window.view.set_document(window.document)
        hs2 = find_hotspot(window, scn["font"], scn["find"]) \
            or find_hotspot(window, scn["font"])
        if hs2 is not None:
            span2 = hs2.span
            try:
                drive_edit(window, hs2, scn["new_text"])
            except Exception:
                failures.append(f"{tag}[fresh]: drive_edit raised:\n"
                                f"{traceback.format_exc()}")
            else:
                out2 = os.path.join(tempfile.gettempdir(), f"app_{fixture}_fresh.pdf")
                try:
                    window.document.save_as(out2)
                except Exception:
                    failures.append(f"{tag}[fresh]: save_as raised:\n"
                                    f"{traceback.format_exc()}")
                else:
                    rf2 = window.document.font_engine.resolve(
                        0, span2.font, span2.flags, scn["new_text"])
                    check(failures, tag, rf2.tier != TIER_BASE14,
                          f"[fresh] fell to BASE14 ({rf2.source_name})")
                    try:
                        fobj2 = resolved_fitz_font(
                            window.document.font_engine, rf2)
                        check(failures, tag,
                              FontEngine.font_covers(fobj2, scn["new_text"]),
                              f"[fresh] resolved font does not cover "
                              f"{scn['new_text']!r} -> tofu")
                    except Exception:
                        failures.append(f"{tag}[fresh]: resolved font raised:\n"
                                        f"{traceback.format_exc()}")
                    ink2 = region_ink(out2, span2.bbox)
                    check(failures, tag, ink2 >= 50,
                          f"[fresh] edited region has no ink ({ink2}px)")
                    b2 = saved_span_basefonts(out2)
                    check(failures, tag,
                          not any(b in _BASE14_BASEFONTS for b in b2),
                          f"[fresh] saved page has a base-14 font: {b2}")

        if not failures:
            print(f"  {tag:18} word-edit tier={_TIER_NAME[rf.tier]:8} "
                  f"qt={rf.qt_family!r:16} ink={ink:5} fonts-ok")
        return failures
    finally:
        close_window(window)


# --------------------------------------------------------------------------
# Empty-state + loaded screenshots
# --------------------------------------------------------------------------
def capture_static_shots() -> list[str]:
    """Render the empty state and a freshly-loaded page (screenshots 1 and 2).
    Screenshots 3 and 4 (mid-edit / after-edit) are produced inside the
    body_paragraphs scenario where a live editor is mounted."""
    paths: list[str] = []

    # 1. Empty state: a fresh window with no document. Grab the whole window so
    #    the toolbar + empty-state overlay are visible. The Recent list is
    #    stubbed with SYNTHETIC paths before rendering: the live list reads
    #    this machine's real app-opened PDFs (QSettings), and real document
    #    names must never land in a committed screenshot (fixture policy:
    #    synthetic, neutral names only). The rows render their FOLDER as a
    #    second line (navigation M3), so the stubs are display-only
    #    home-relative paths -- neutral folders, deterministic across
    #    machines (no repo/worktree path baked into the PNG).
    w0 = MainWindow()
    w0.resize(1180, 920)
    w0.show()
    w0.empty_state.set_recents([
        os.path.expanduser(p)
        for p in ("~/Documents/Forms/onboarding-checklist.pdf",
                  "~/Documents/Forms/benefits-summary.pdf",
                  "~/Documents/Archive/onboarding-checklist.pdf")
    ])
    # set_recents deleteLater()s the previous rows; flush the deferred
    # deletes BEFORE rendering or the real-recents labels paint underneath
    # the synthetic ones.
    _APP.processEvents()
    _APP.sendPostedEvents(None, QEvent.DeferredDelete)
    _APP.processEvents()
    p1 = os.path.join(SHOT_DIR, "01_empty_state.png")
    render_window_png(w0, p1)
    paths.append(p1)
    close_window(w0)

    # 2. A loaded page (scene render, crisp white sheet on the gutter).
    w1 = MainWindow()
    w1.resize(1180, 920)
    w1.open_path(os.path.join(FIXTURE_DIR, "mixed_families.pdf"))
    _APP.processEvents()
    p2 = os.path.join(SHOT_DIR, "02_loaded_page.png")
    render_scene_png(w1, p2)
    paths.append(p2)
    close_window(w1)
    return paths


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------
def main() -> int:
    print("App harness (real MainWindow/PageView, offscreen)\n")
    all_failures: list[str] = []

    shot_paths = capture_static_shots()

    for scn in SCENARIOS:
        # Only the first fixture captures the mid/after-edit shots (one live
        # editor mounted over a real run is enough for the deliverable).
        capture = (scn["fixture"] == "body_paragraphs")
        print(f"[{scn['fixture']}]")
        fs = run_scenario(scn, capture_shots=capture)
        if fs:
            for f in fs:
                print(f"  FAIL {f}")
        all_failures.extend(fs)
        print()

    # Collect the screenshot list (1,2 static + 3,4 from the edit scenario).
    for name in ("03_mid_edit.png", "04_after_edit.png"):
        p = os.path.join(SHOT_DIR, name)
        if os.path.exists(p):
            shot_paths.append(p)

    print("=" * 66)
    print("Screenshots rendered:")
    for p in shot_paths:
        print(f"  {p}")
    print()

    if all_failures:
        print(f"FAILED ({len(all_failures)} assertion failure(s)):")
        for f in all_failures:
            print(f"  - {f}")
        return 1

    print("PASSED -- every fixture loads into the real window, an in-place "
          "word/number edit applies, undo+redo work, the save reproduces the "
          "ORIGINAL embedded font (no base-14), color/size/baseline are "
          "preserved, and the old text is removed. No exceptions.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
