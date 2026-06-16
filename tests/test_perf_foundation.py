"""Mechanism asserts for the performance foundation (ws1_perf_foundation).

Performance work must never be trusted on timings alone (CI boxes vary), so
this suite asserts the MECHANISMS -- call counts, cache identity, signature
behavior -- and prints timings informationally. M1 coverage:

  A1: the bake pipeline reuses the document's persistent FontEngine: two
      ``render_with_edits`` calls with staged edits construct ZERO new engines
      and produce byte-identical pixels.
  A2: ``_overlapping_neighbors`` is memoized: one cache entry (and one rawdict
      walk) across three identical renders; a new staged edit changes the key;
      ``_invalidate_caches`` empties the memo.
  A3 (parity keystone): render_with_edits ink vs the saved file's ink stays
      within rel diff < 0.02 for a span edit, a paragraph reflow edit, and a
      NewBox add -- the WYSIWYG soul under the new caching.
  A4: ``render_signature`` is stable across repeated calls, changes on every
      fine-grained mutator and on undo/redo, and changes generation on
      structural ops and structural undo.

M2 coverage (the baked-pixmap LRU cache in PageView, keyed
``(page, zoom*dpr, render_signature)``):

  B1: dematerialize + rematerialize of an edited page makes ZERO
      ``render_with_edits`` calls and displays pixels byte-equal to a fresh
      bake (a hit can only re-display real-pipeline bytes).
  B2: ``stage_edit`` + ``repaint_box`` is exactly ONE fresh render (the purge
      + changed signature force the miss) with pixel parity.
  B3: ``rotate_page`` through the window's structural funnel re-renders (the
      generation bump misses) and the cache serves hits again afterwards.
  B4: ``undo_stack.undo()`` of a text edit misses and restores the PRISTINE
      pixels byte-for-byte (full-undo-pristine, the WYSIWYG soul).
  B5: the cache never exceeds capacity while scrolling a 30-page doc end to
      end; ``set_document`` clears it (two docs must never key-collide) and
      the swapped-in document's pixels display; ``clear_document`` empties it.

M3 coverage (one-Shape-per-page batched reinsertion):

  C1: a paragraph edit + a NewBox bake with ZERO ``page.insert_text`` calls
      (every run routes through the page batch Shape) and exactly ONE
      ``Shape.commit`` per edited page -- for save_as, render_with_edits, and
      a two-edited-page save.
  C2: editing a base-14 chrome span AND an embedded-Arial value span on the
      same page introduces no base-14 substitution (``new_base14 == []``) and
      both original face families survive in the saved file.
  C3: a justified paragraph's saved per-word pen origins match the wrap
      engine's ``word_origins`` within 0.5 pt (the per-word draw contract:
      justify draws each word at its own stretched x).
  C4: writing direction survives the batch: an edit on the /Rotate 90 page
      reads vertically in DISPLAY space (rect through rotation_matrix taller
      than wide), and a run drawn rotated in TEXT space (the ``morph`` path)
      keeps its rawdict line dir and tall bbox after an edit.

M4 coverage (foundation seams):

  D1: the system font index builds exactly ONCE under concurrency (two
      threads racing ``_build_system_index`` -> one ``_scan_system_faces``),
      and the threaded ``prewarm_system_index`` yields the same
      ``available_families`` list as a cold synchronous build.
  D2: the page-item factory seam: a registered factory runs per materialized
      page with (layer, view), its items live on ``layer.extra_items`` and in
      the scene, dematerialize removes them, re-materialize re-creates them;
      with NO factories the scene item census is exactly the pre-M4 set
      (placeholder + sheet/shadow/pixmap/hotspots), guarding against
      accidental extra items. Plus the eviction-guard fix: the page hosting
      the SELECTION is never dematerialized by the lazy-render band (the
      guard the old code only applied to the editor's page).
  D3: menubar census: exactly the 7 skeleton titles (File/Edit/View/Tools/
      Document/Window/Help); every pre-existing QAction is still reachable
      by walking the menubar, with its original shortcut; ``menu_anchors``
      holds all 9 reserved anchors, each a separator parented per the ws7
      menu table (the registry of record).
  D4: tool-mode dispatch: current_mode() walks select -> add_text ->
      text_edit -> select through the real transitions, and a NEW mode
      registered via ``register_mode_handlers`` receives press/key events
      with no inline branch edits (the extension seam later workstreams use).

Fixtures: existing synthetic fixtures only (form_like.pdf, paragraphs.pdf,
multipage_body.pdf, rotated_doc.pdf, three_page.pdf) plus tempfile docs
synthesized in-test with fake neutral names. Never writes into
tests/fixtures/ or tests/screenshots/; temp output goes through
tempfile.mkdtemp.

Run:
    QT_QPA_PLATFORM=offscreen \
      /Users/edward/Documents/GitHub/PDFTextEditor/.venv/bin/python \
      tests/test_perf_foundation.py
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import threading
import time
import traceback

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import fitz  # noqa: E402
from PySide6.QtCore import QEvent, QPointF, QRectF, Qt  # noqa: E402
from PySide6.QtGui import (  # noqa: E402
    QImage,
    QKeyEvent,
    QKeySequence,
    QMouseEvent,
)
from PySide6.QtWidgets import QApplication, QGraphicsRectItem  # noqa: E402

# A QApplication must exist before the font resolver runs (save_as guard).
_APP = QApplication.instance() or QApplication([])

import pdftexteditor.document as document_mod  # noqa: E402
from pdftexteditor.document import PDFDocument  # noqa: E402
from pdftexteditor.font_engine import FontEngine  # noqa: E402
from pdftexteditor.ui.main_window import EditRunCommand, MainWindow  # noqa: E402

FIXTURE_DIR = os.path.join(_HERE, "fixtures")
FORM = os.path.join(FIXTURE_DIR, "form_like.pdf")
PARAGRAPHS = os.path.join(FIXTURE_DIR, "paragraphs.pdf")
MULTIPAGE = os.path.join(FIXTURE_DIR, "multipage_body.pdf")
ROTATED = os.path.join(FIXTURE_DIR, "rotated_doc.pdf")
THREE = os.path.join(FIXTURE_DIR, "three_page.pdf")

# Base-14 BaseFont names as they appear in saved files (lowercased), for the
# "an edit never introduces a base-14 substitution" gate (mirrors
# tests/test_editor.py).
_BASE14_BASEFONTS = {
    "helvetica", "helvetica-bold", "helvetica-oblique", "helvetica-boldoblique",
    "times-roman", "times-bold", "times-italic", "times-bolditalic",
    "courier", "courier-bold", "courier-oblique", "courier-boldoblique",
    "symbol", "zapfdingbats",
}


def check(failures: list[str], tag: str, cond: bool, msg: str) -> bool:
    if not cond:
        failures.append(f"{tag}: {msg}")
    return cond


def find_box(doc: PDFDocument, page: int, contains: str,
             paragraph: bool | None = None):
    """First box on ``page`` whose text contains ``contains`` (optionally
    filtered to paragraph / non-paragraph boxes)."""
    for s in doc.spans(page):
        if paragraph is not None and getattr(s, "is_paragraph", False) != paragraph:
            continue
        if contains in s.text:
            return s
    return None


def _pix_ink(pix) -> int:
    s, step, n = pix.samples, pix.n, 0
    for i in range(0, len(s), step):
        r, g, b = s[i], s[i + 1], s[i + 2]
        if (0.299 * r + 0.587 * g + 0.114 * b) < 230:
            n += 1
    return n


def wysiwyg_rel_diff(document: PDFDocument, out_path: str,
                     page: int = 0, scale: float = 2.0) -> float:
    """Whole-page ink of render_with_edits vs the saved file (baked WYSIWYG,
    BUILD_SPEC invariant §0.1): the on-screen page must be the SAME pipeline as
    save. Mirrors tests/test_editor.py's helper."""
    rink = _pix_ink(document.render_with_edits(page, scale))
    sd = fitz.open(out_path)
    try:
        sink = _pix_ink(sd[page].get_pixmap(matrix=fitz.Matrix(scale, scale),
                                            alpha=False))
    finally:
        sd.close()
    return abs(rink - sink) / max(sink, 1)


# ==========================================================================
# A1: persistent FontEngine -- zero constructions during cached renders
# ==========================================================================
def test_a1_persistent_engine(failures: list[str]) -> None:
    tag = "A1_persistent_engine"
    doc = PDFDocument(FORM)
    b1 = find_box(doc, 0, "Cleared")
    b2 = find_box(doc, 0, "Pending")
    if not check(failures, tag, b1 is not None and b2 is not None,
                 "fixture spans not found"):
        return
    doc.stage_edit(0, b1, b1.text.replace("4821", "9135"))
    doc.stage_edit(0, b2, b2.text.replace("review", "final"))

    real_engine = document_mod.FontEngine
    counts = {"n": 0}

    class CountingEngine(real_engine):
        def __init__(self, *a, **k):
            counts["n"] += 1
            super().__init__(*a, **k)

    document_mod.FontEngine = CountingEngine
    try:
        t0 = time.perf_counter()
        p1 = doc.render_with_edits(0, 2.0)
        t1 = time.perf_counter()
        p2 = doc.render_with_edits(0, 2.0)
        t2 = time.perf_counter()
    finally:
        document_mod.FontEngine = real_engine

    print(f"  [info] render_with_edits, 2 edits: "
          f"{(t1 - t0) * 1000:.1f} ms cold, {(t2 - t1) * 1000:.1f} ms warm")
    check(failures, tag, counts["n"] == 0,
          f"FontEngine constructed {counts['n']}x during renders (want 0: "
          f"the persistent engine must serve the bake pipeline)")
    check(failures, tag, p1.samples == p2.samples,
          "repeated render_with_edits produced different pixels")


# ==========================================================================
# A2: _overlapping_neighbors memo -- entry count + walk count
# ==========================================================================
def test_a2_neighbors_memo(failures: list[str]) -> None:
    tag = "A2_neighbors_memo"
    doc = PDFDocument(FORM)
    b1 = find_box(doc, 0, "Cleared")
    if not check(failures, tag, b1 is not None, "fixture span not found"):
        return
    doc.stage_edit(0, b1, b1.text.replace("4821", "7042"))

    real_walk = PDFDocument._overlapping_neighbors
    counts = {"n": 0}

    def counting_walk(out, page_index, edited_keys, redact_rects):
        counts["n"] += 1
        return real_walk(out, page_index, edited_keys, redact_rects)

    PDFDocument._overlapping_neighbors = staticmethod(counting_walk)
    try:
        for _ in range(3):
            doc.render_with_edits(0, 2.0)
    finally:
        PDFDocument._overlapping_neighbors = staticmethod(real_walk)

    check(failures, tag, len(doc._neighbors_cache) == 1,
          f"{len(doc._neighbors_cache)} memo entries after 3 identical "
          f"renders (want 1)")
    check(failures, tag, counts["n"] == 1,
          f"rawdict walk ran {counts['n']}x across 3 identical renders "
          f"(want 1: misses only on a changed key)")

    # A new staged edit changes edited_keys + redact_rects -> a second entry.
    b2 = find_box(doc, 0, "Pending")
    if not check(failures, tag, b2 is not None, "second fixture span not found"):
        return
    doc.stage_edit(0, b2, b2.text.replace("review", "queued"))
    doc.render_with_edits(0, 2.0)
    check(failures, tag, len(doc._neighbors_cache) == 2,
          f"{len(doc._neighbors_cache)} memo entries after a new staged edit "
          f"(want 2: the key must fold in the edit set)")

    doc._invalidate_caches()
    check(failures, tag, len(doc._neighbors_cache) == 0,
          "memo not emptied by _invalidate_caches")


# ==========================================================================
# A3: parity keystone -- render ink == saved-file ink under the new caching
# ==========================================================================
def test_a3_wysiwyg_parity(failures: list[str], tmpdir: str) -> None:
    tag = "A3_wysiwyg_parity"

    # Scenario 1: a span edit (the central use case).
    doc = PDFDocument(FORM)
    b = find_box(doc, 0, "Cleared")
    if check(failures, tag, b is not None, "span-edit target not found"):
        doc.stage_edit(0, b, b.text.replace("4821", "9135"))
        out = os.path.join(tmpdir, "span_edit.pdf")
        doc.save_as(out)
        diff = wysiwyg_rel_diff(doc, out)
        print(f"  [info] span edit rel diff: {diff:.4f}")
        check(failures, tag, diff < 0.02,
              f"span edit: render vs saved ink rel diff {diff:.4f} >= 0.02")

    # Scenario 2: a paragraph reflow edit (longer text -> rewrap).
    doc = PDFDocument(PARAGRAPHS)
    para = find_box(doc, 0, "quarterly", paragraph=True)
    if check(failures, tag, para is not None, "paragraph target not found"):
        doc.stage_edit(
            0, para,
            para.text.replace(
                "quarterly operations review",
                "newly extended quarterly operations planning review",
            ),
        )
        out = os.path.join(tmpdir, "reflow_edit.pdf")
        doc.save_as(out)
        diff = wysiwyg_rel_diff(doc, out)
        print(f"  [info] paragraph reflow rel diff: {diff:.4f}")
        check(failures, tag, diff < 0.02,
              f"reflow edit: render vs saved ink rel diff {diff:.4f} >= 0.02")

    # Scenario 3: a NewBox add (no original ink, draw-only path).
    doc = PDFDocument(FORM)
    doc.add_box(0, (96.0, 540.0), "Reviewed by Jordan Carter 2026",
                "Helvetica", 13.0, (0.0, 0.0, 0.0), False, False)
    out = os.path.join(tmpdir, "newbox_add.pdf")
    doc.save_as(out)
    diff = wysiwyg_rel_diff(doc, out)
    print(f"  [info] NewBox add rel diff: {diff:.4f}")
    check(failures, tag, diff < 0.02,
          f"NewBox add: render vs saved ink rel diff {diff:.4f} >= 0.02")


# ==========================================================================
# A4: render_signature -- stable, mutation-sensitive, generation-aware
# ==========================================================================
def test_a4_render_signature(failures: list[str]) -> None:
    tag = "A4_render_signature"
    doc = PDFDocument(FORM)

    sig0 = doc.render_signature(0)
    check(failures, tag, sig0 == doc.render_signature(0),
          "signature not stable across repeated calls (pristine)")

    b = find_box(doc, 0, "Cleared")
    if not check(failures, tag, b is not None, "fixture span not found"):
        return

    # stage_edit changes it ...
    doc.stage_edit(0, b, b.text.replace("4821", "9135"))
    sig_text = doc.render_signature(0)
    check(failures, tag, sig_text != sig0, "signature unchanged by stage_edit")
    check(failures, tag, sig_text == doc.render_signature(0),
          "signature not stable across repeated calls (edited)")

    # ... and only for the edited page (per-page key).
    other = PDFDocument(MULTIPAGE)
    o_sig1 = other.render_signature(1)
    ob = other.spans(0)[0]
    other.stage_edit(0, ob, ob.text + " x")
    check(failures, tag, other.render_signature(1) == o_sig1,
          "page-0 edit changed page-1's signature")

    # set_style / move_box each change it.
    doc.set_style(0, b, size=15.0)
    sig_style = doc.render_signature(0)
    check(failures, tag, sig_style != sig_text,
          "signature unchanged by set_style")
    doc.move_box(0, b, 4.0, -2.0)
    sig_move = doc.render_signature(0)
    check(failures, tag, sig_move != sig_style,
          "signature unchanged by move_box")

    # undo/redo flow through the same staged state (no extra hook needed).
    doc.undo()
    sig_undo = doc.render_signature(0)
    check(failures, tag, sig_undo != sig_move, "signature unchanged by undo")
    check(failures, tag, sig_undo == sig_style,
          "undo did not restore the pre-move signature")
    doc.redo()
    check(failures, tag, doc.render_signature(0) == sig_move,
          "redo did not restore the post-move signature")

    # A NewBox folds into the signature too.
    doc.add_box(0, (300.0, 500.0), "Acme Corp note", "Helvetica", 12.0,
                (0.0, 0.0, 0.0), False, False)
    sig_box = doc.render_signature(0)
    check(failures, tag, sig_box != sig_move,
          "signature unchanged by add_box")

    # Structural ops bump the generation (element 0): rotate bakes + mutates.
    gen_before = doc.render_signature(0)[0]
    doc.rotate_page(0, 90)
    sig_rot = doc.render_signature(0)
    check(failures, tag, sig_rot[0] != gen_before,
          "rotate_page did not change the signature generation")
    # Structural undo restores pre-rotate bytes but MUST still re-key (the
    # working doc was swapped): generation bumps again.
    doc.undo_structural()
    sig_sundo = doc.render_signature(0)
    check(failures, tag, sig_sundo[0] != sig_rot[0],
          "undo_structural did not change the signature generation")


# ==========================================================================
# B: the baked-pixmap LRU cache in PageView (window-level helpers below)
# ==========================================================================
def pump(n: int = 4) -> None:
    for _ in range(n):
        _APP.processEvents()


def open_window(path: str) -> MainWindow:
    w = MainWindow()
    w.resize(1300, 950)
    w.show()
    w.open_path(path)
    pump(6)
    if getattr(w, "empty_state", None) is not None:
        w.empty_state.hide()
    pump(2)
    return w


def close_window(w: MainWindow) -> None:
    """Close WITHOUT the dirty-discard modal (blocks forever offscreen)."""
    w._suppress_close_guard = True
    w.close()


def install_render_counter(doc: PDFDocument) -> dict:
    """Shadow the document's bound ``render_with_edits`` with a counting shim
    (instance attribute; removed by ``remove_render_counter``)."""
    real = doc.render_with_edits
    counts = {"n": 0}

    def shim(page_index, zoom, exclude_span=None):
        counts["n"] += 1
        return real(page_index, zoom, exclude_span)

    doc.render_with_edits = shim
    return counts


def remove_render_counter(doc: PDFDocument) -> None:
    try:
        del doc.render_with_edits
    except AttributeError:
        pass


def fresh_qimage(view, doc: PDFDocument, page: int) -> QImage:
    """A page QImage built EXACTLY like PageView's materialize path, straight
    from the bake pipeline (no cache): the byte-for-byte parity reference."""
    dpr = view.devicePixelRatioF() or 1.0
    pix = doc.render_with_edits(page, view.zoom * dpr)
    img = QImage(pix.samples, pix.width, pix.height, pix.stride,
                 QImage.Format_RGB888).copy()
    img.setDevicePixelRatio(dpr)
    return img


def test_b1_cache_hit(failures: list[str]) -> None:
    """Demat + remat of an edited page: zero bake calls, byte-equal pixels."""
    tag = "B1_cache_hit"
    w = open_window(FORM)
    try:
        view, doc = w.view, w.document
        b = find_box(doc, 0, "Cleared")
        if not check(failures, tag, b is not None, "fixture span not found"):
            return
        doc.stage_edit(0, b, b.text.replace("4821", "9135"))
        view.repaint_box(b)         # purge + bake the edited page (and cache it)
        pump()
        layer = view._layers[0]
        if not check(failures, tag, layer.rendered, "page 0 not materialized"):
            return

        counts = install_render_counter(doc)
        try:
            t0 = time.perf_counter()
            view._dematerialize_page(layer)
            view._materialize_page(layer)
            t1 = time.perf_counter()
        finally:
            remove_render_counter(doc)
        print(f"  [info] edited-page re-entry (cache hit): "
              f"{(t1 - t0) * 1000:.1f} ms")
        check(failures, tag, counts["n"] == 0,
              f"re-materialize made {counts['n']} render_with_edits calls "
              f"(want 0: the cache must serve the hit)")
        check(failures, tag, layer.image is not None
              and layer.image == fresh_qimage(view, doc, 0),
              "cached pixels != fresh render_with_edits pixels (byte parity)")
    finally:
        close_window(w)


def test_b2_repaint_misses_once(failures: list[str]) -> None:
    """stage_edit + repaint_box = exactly one fresh bake, with pixel parity."""
    tag = "B2_repaint_miss"
    w = open_window(FORM)
    try:
        view, doc = w.view, w.document
        b = find_box(doc, 0, "Pending")
        if not check(failures, tag, b is not None, "fixture span not found"):
            return
        counts = install_render_counter(doc)
        try:
            doc.stage_edit(0, b, b.text.replace("review", "final"))
            view.repaint_box(b)
            pump()
        finally:
            remove_render_counter(doc)
        check(failures, tag, counts["n"] == 1,
              f"repaint_box after stage_edit made {counts['n']} renders "
              f"(want exactly 1: purge + changed signature = one miss)")
        layer = view._layers[0]
        check(failures, tag, layer.image is not None
              and layer.image == fresh_qimage(view, doc, 0),
              "repainted pixels != fresh render_with_edits pixels")
    finally:
        close_window(w)


def test_b3_rotate_misses(failures: list[str]) -> None:
    """A structural rotate re-renders (generation bump), then hits again."""
    tag = "B3_rotate_miss"
    w = open_window(FORM)
    try:
        view, doc = w.view, w.document
        layer = view._layers[0]
        pre_size = layer.image.size() if layer.image is not None else None
        if not check(failures, tag, pre_size is not None,
                     "page 0 not materialized"):
            return

        # Pristine pages cache too: a plain demat/remat round trip is a hit.
        counts = install_render_counter(doc)
        try:
            view._dematerialize_page(layer)
            view._materialize_page(layer)
        finally:
            remove_render_counter(doc)
        check(failures, tag, counts["n"] == 0,
              f"pristine re-materialize made {counts['n']} renders (want 0)")

        # Rotate through the window's structural funnel: the generation bump
        # changes render_signature, so the reload MUST re-bake the page.
        counts = install_render_counter(doc)
        try:
            w.rotate_current_cw()
            pump()
        finally:
            remove_render_counter(doc)
        check(failures, tag, counts["n"] >= 1,
              "rotate_page did not force a fresh render (stale cache hit?)")
        layer = view._layers[0]            # reload rebuilt the layers
        if check(failures, tag, layer.image is not None,
                 "page 0 not materialized after rotate"):
            check(failures, tag, layer.image.size() != pre_size,
                  "post-rotate image kept the pre-rotate dimensions "
                  "(stale pixels displayed)")

        # And the cache serves the rotated state on the next re-entry.
        counts = install_render_counter(doc)
        try:
            view._dematerialize_page(layer)
            view._materialize_page(layer)
        finally:
            remove_render_counter(doc)
        check(failures, tag, counts["n"] == 0,
              f"post-rotate re-materialize made {counts['n']} renders "
              f"(want 0: rotated state must cache like any other)")
    finally:
        close_window(w)


def test_b4_undo_pristine(failures: list[str]) -> None:
    """Undo of a text edit misses, and re-renders the PRISTINE pixels."""
    tag = "B4_undo_pristine"
    w = open_window(FORM)
    try:
        view, doc = w.view, w.document
        b = find_box(doc, 0, "Cleared")
        if not check(failures, tag, b is not None, "fixture span not found"):
            return
        pristine = fresh_qimage(view, doc, 0)   # before any edit

        cmd = EditRunCommand(doc, view, 0, b, b.text,
                             b.text.replace("4821", "9135"))
        w.undo_stack.push(cmd)                  # redo() = stage_edit + repaint
        pump()
        check(failures, tag, doc.edit_count == 1,
              f"edit_count {doc.edit_count} != 1 after the pushed edit")
        layer = view._layers[0]
        check(failures, tag, layer.image is not None
              and layer.image != pristine,
              "the staged edit did not change the displayed pixels")

        counts = install_render_counter(doc)
        try:
            w.undo_stack.undo()
            pump()
        finally:
            remove_render_counter(doc)
        check(failures, tag, counts["n"] == 1,
              f"undo repaint made {counts['n']} renders (want exactly 1: the "
              f"purge + reverted signature force one miss)")
        check(failures, tag, doc.edit_count == 0,
              f"edit_count {doc.edit_count} != 0 after undo")
        layer = view._layers[0]
        check(failures, tag, layer.image is not None
              and layer.image == pristine,
              "undo did not restore the pristine pixels byte-for-byte")
    finally:
        close_window(w)


def test_b6_zoom_revisit_hits(failures: list[str], tmpdir: str) -> None:
    """A zoom toggle away and back re-serves the visible band from the cache
    (final-review perf regression): set_zoom's reload must not bake
    off-screen transients that evict the previous zoom's band, and the
    steady-state toggle renders NOTHING."""
    tag = "B6_zoom_revisit"
    path = _build_thirty_page(tmpdir)
    w = open_window(path)
    try:
        view, doc = w.view, w.document
        view.set_zoom(1.0)
        pump(3)
        view.scroll_to_page(11)
        pump(3)
        z = view.zoom
        # First toggle warms both zoom levels' bands.
        view.set_zoom(z * 1.5)
        pump(3)
        view.set_zoom(z)
        pump(3)
        # Steady state: the SECOND toggle must be all cache hits.
        counts = install_render_counter(doc)
        try:
            view.set_zoom(z * 1.5)
            pump(3)
            up = counts["n"]
            view.set_zoom(z)
            pump(3)
            back = counts["n"] - up
        finally:
            remove_render_counter(doc)
        check(failures, tag, up == 0,
              f"steady-state zoom-in re-rendered {up} page(s) (want 0 -- "
              f"transient materialization evicted the band)")
        check(failures, tag, back == 0,
              f"steady-state zoom-back re-rendered {back} page(s) (want 0)")
        vis = [ly.page_index for ly in view._layers if ly.rendered]
        check(failures, tag, 11 in vis,
              f"page 11 not materialized after the toggle (visible={vis})")
    finally:
        close_window(w)


def _build_thirty_page(tmpdir: str) -> str:
    """A 30-page synthetic doc (fake neutral names only), built in a tempdir
    -- never into tests/fixtures/ (mirrors tests/test_pages.py's synth)."""
    out = fitz.open()
    try:
        for i in range(30):
            page = out.new_page(width=612, height=792)
            page.insert_text((72, 90),
                             f"Acme Corp operations brief -- page {i + 1}",
                             fontsize=18)
            for ln in range(14):
                page.insert_text(
                    (72, 130 + 24 * ln),
                    f"Jordan Carter reviewed item {ln + 1} with Riley Morgan.",
                    fontsize=11,
                )
        path = os.path.join(tmpdir, "thirty_page.pdf")
        out.save(path, garbage=4, deflate=True)
    finally:
        out.close()
    return path


def test_b5_capacity_and_doc_swap(failures: list[str], tmpdir: str) -> None:
    """LRU bound under an end-to-end scroll; doc swap clears; new doc shows."""
    tag = "B5_capacity_swap"
    path = _build_thirty_page(tmpdir)
    w = open_window(path)
    other = None
    try:
        view, doc = w.view, w.document
        cache = view._render_cache
        cap = cache.capacity
        check(failures, tag, w.document.page_count == 30,
              f"synthetic doc has {w.document.page_count} pages (want 30)")

        # Scroll end to end; the cache length must NEVER exceed capacity.
        vbar = view.verticalScrollBar()
        worst = 0
        step = max(1, vbar.maximum() // 40)
        for v in range(vbar.minimum(), vbar.maximum() + 1, step):
            vbar.setValue(v)
            pump(2)
            worst = max(worst, len(cache))
        vbar.setValue(vbar.maximum())
        pump(2)
        worst = max(worst, len(cache))
        check(failures, tag, worst <= cap,
              f"cache grew to {worst} entries scrolling 30 pages "
              f"(capacity {cap})")
        check(failures, tag, len(cache) > 0,
              "cache empty after scrolling (nothing was ever cached?)")

        # Re-entry onto a recently-visited page is a pure hit (and instant).
        last = view._layers[-1]
        if not last.rendered:
            view._materialize_page(last)
        counts = install_render_counter(doc)
        try:
            t0 = time.perf_counter()
            view._dematerialize_page(last)
            view._materialize_page(last)
            t1 = time.perf_counter()
        finally:
            remove_render_counter(doc)
        print(f"  [info] 30-page doc, scroll re-entry hit: "
              f"{(t1 - t0) * 1000:.1f} ms")
        check(failures, tag, counts["n"] == 0,
              f"re-entry onto a cached page made {counts['n']} renders")

        # Document swap MUST clear (two pristine docs share a signature, so
        # without the clear doc A's pixels could display for doc B)...
        other = PDFDocument(FORM)
        clears = {"n": 0}
        real_clear = cache.clear

        def spy_clear():
            clears["n"] += 1
            real_clear()

        cache.clear = spy_clear
        try:
            view.set_document(other)
            pump(2)
        finally:
            del cache.clear
        check(failures, tag, clears["n"] >= 1,
              "set_document did not clear the render cache")
        # ... and the swapped-in document's own pixels must display.
        layer = view._layers[0]
        check(failures, tag, layer.image is not None
              and layer.image == fresh_qimage(view, other, 0),
              "post-swap page 0 pixels != the new document's fresh render")

        view.clear_document()
        pump()
        check(failures, tag, len(cache) == 0,
              f"clear_document left {len(cache)} cache entries (want 0)")
    finally:
        close_window(w)
        if other is not None:
            other.close()


# ==========================================================================
# C: one-Shape-per-page batched reinsertion (M3)
# ==========================================================================
def saved_basefonts(path: str) -> list:
    """Page-0 BaseFont names of a saved file, subset prefixes stripped,
    lowercased (mirrors tests/test_editor.py)."""
    doc = fitz.open(path)
    try:
        out = []
        for entry in doc[0].get_fonts(full=True):
            base = entry[3] or ""
            if len(base) > 7 and base[6] == "+" and base[:6].isalpha():
                base = base[7:]
            out.append(base.lower())
        return out
    finally:
        doc.close()


def new_base14(before_path: str, after_path: str) -> list:
    """Base-14 builtins present AFTER an edit that were NOT in the original
    (a form's pre-existing standard-font references are not mistaken for a
    substitution introduced by the edit)."""
    before = set(saved_basefonts(before_path)) & _BASE14_BASEFONTS
    after = set(saved_basefonts(after_path)) & _BASE14_BASEFONTS
    return sorted(after - before)


class _BakeCallCounter:
    """Context manager counting ``fitz.Page.insert_text`` calls and
    ``fitz.Shape.commit`` calls (class-level patches, restored on exit). After
    M3 the bake pipeline must never call ``page.insert_text`` (each call is a
    private one-shot Shape with its own content-stream commit); every run
    accumulates on ONE page Shape committed once.

    ``commit`` counts only commits issued by pdftexteditor's own code (caller
    attribution via the shim's parent frame): pymupdf's ``apply_redactions``
    unconditionally commits an internal Shape of its own per redaction pass
    (replacement-text/fill machinery -- content-empty for our blind
    redactions, predates M3), which must not be mistaken for a second batch.
    ``foreign_commit`` records those for transparency."""

    def __init__(self):
        self.insert_text = 0
        self.commit = 0
        self.foreign_commit = 0

    def __enter__(self):
        self._real_it = fitz.Page.insert_text
        self._real_commit = fitz.Shape.commit
        counter = self

        def shim_it(page_self, *a, **k):
            counter.insert_text += 1
            return counter._real_it(page_self, *a, **k)

        def shim_commit(shape_self, *a, **k):
            caller = sys._getframe(1).f_code.co_filename
            if os.path.join("pdftexteditor", "") in caller:
                counter.commit += 1
            else:
                counter.foreign_commit += 1
            return counter._real_commit(shape_self, *a, **k)

        fitz.Page.insert_text = shim_it
        fitz.Shape.commit = shim_commit
        return self

    def __exit__(self, *exc):
        fitz.Page.insert_text = self._real_it
        fitz.Shape.commit = self._real_commit
        return False


def _page_chars(path: str, page: int = 0) -> list[tuple]:
    """Every glyph on ``page`` of a saved file as (char, pen_x, pen_y) from
    rawdict -- char origins are the exact pen positions the bake drew at."""
    doc = fitz.open(path)
    try:
        out = []
        for blk in doc[page].get_text("rawdict")["blocks"]:
            if blk.get("type", 0) != 0:
                continue
            for line in blk["lines"]:
                for span in line["spans"]:
                    for c in span.get("chars", []):
                        out.append((c["c"], c["origin"][0], c["origin"][1]))
        return out
    finally:
        doc.close()


def test_c1_batched_shape(failures: list[str], tmpdir: str) -> None:
    """Zero page.insert_text in the bake; exactly one Shape.commit per page."""
    tag = "C1_batched_shape"
    doc = PDFDocument(PARAGRAPHS)
    para = find_box(doc, 0, "quarterly", paragraph=True)
    if not check(failures, tag, para is not None, "paragraph target not found"):
        return
    # A multi-line paragraph edit (many runs) plus a NewBox (draw-only run):
    # before M3 this was one page.insert_text per line + one for the box.
    doc.stage_edit(
        0, para,
        para.text.replace("quarterly operations review",
                          "newly extended quarterly operations planning review"),
    )
    doc.add_box(0, (96.0, 720.0), "Filed by Riley Morgan for Acme Corp",
                "Helvetica", 11.0, (0.0, 0.0, 0.0), False, False)

    out = os.path.join(tmpdir, "c1_batch.pdf")
    with _BakeCallCounter() as counts:
        t0 = time.perf_counter()
        doc.save_as(out)
        t1 = time.perf_counter()
    print(f"  [info] paragraph-page save_as: {(t1 - t0) * 1000:.1f} ms")
    check(failures, tag, counts.insert_text == 0,
          f"save_as made {counts.insert_text} page.insert_text calls "
          f"(want 0: every run must ride the page batch Shape)")
    check(failures, tag, counts.commit == 1,
          f"save_as committed {counts.commit} Shapes (want exactly 1 for the "
          f"single edited page)")

    # The live render is the SAME pipeline: zero insert_text, one commit.
    with _BakeCallCounter() as counts:
        doc.render_with_edits(0, 2.0)
    check(failures, tag, counts.insert_text == 0,
          f"render_with_edits made {counts.insert_text} page.insert_text "
          f"calls (want 0)")
    check(failures, tag, counts.commit == 1,
          f"render_with_edits committed {counts.commit} Shapes (want 1)")
    doc.close()

    # Two edited pages -> exactly two commits (one Shape PER page).
    doc = PDFDocument(MULTIPAGE)
    s0, s1 = doc.spans(0)[0], doc.spans(1)[0]
    doc.stage_edit(0, s0, s0.text + " amended")
    doc.stage_edit(1, s1, s1.text + " amended")
    out2 = os.path.join(tmpdir, "c1_two_pages.pdf")
    with _BakeCallCounter() as counts:
        doc.save_as(out2)
    check(failures, tag, counts.insert_text == 0,
          f"two-page save_as made {counts.insert_text} page.insert_text "
          f"calls (want 0)")
    check(failures, tag, counts.commit == 2,
          f"two-page save_as committed {counts.commit} Shapes (want exactly "
          f"2: one per edited page)")
    doc.close()


def test_c2_no_new_base14(failures: list[str], tmpdir: str) -> None:
    """Batching keeps font semantics: no base-14 introduced, faces survive."""
    tag = "C2_no_new_base14"
    doc = PDFDocument(FORM)
    chrome = find_box(doc, 0, "Sample Report")      # base-14 Helvetica-Bold
    value = find_box(doc, 0, "Cleared")             # embedded ArialMT
    if not check(failures, tag, chrome is not None and value is not None,
                 "fixture spans not found"):
        return
    doc.stage_edit(0, chrome, chrome.text.replace("Sample", "Annual"))
    doc.stage_edit(0, value, value.text.replace("4821", "9135"))
    out = os.path.join(tmpdir, "c2_faces.pdf")
    doc.save_as(out)
    doc.close()

    nb = new_base14(FORM, out)
    check(failures, tag, nb == [],
          f"batched save introduced base-14 substitution(s): {nb}")
    fonts = saved_basefonts(out)
    check(failures, tag, any("arial" in f for f in fonts),
          f"embedded Arial family did not survive the batched save: {fonts}")
    check(failures, tag, any("helvetica" in f for f in fonts),
          f"base-14 Helvetica chrome did not survive the batched save: "
          f"{fonts}")


def test_c3_justify_word_origins(failures: list[str], tmpdir: str) -> None:
    """Justified lines: saved pen origins == wrap engine word_origins."""
    tag = "C3_justify_origins"
    doc = PDFDocument(PARAGRAPHS)
    para = find_box(doc, 0, "quarterly", paragraph=True)
    if not check(failures, tag, para is not None, "paragraph target not found"):
        return
    # Force a rewrap AND the justify path (word_origins only exist on
    # justified non-last lines with >= 2 words).
    doc.stage_edit(
        0, para,
        para.text.replace("quarterly operations review",
                          "extended quarterly operations planning review"),
    )
    doc.set_style(0, para, alignment="justify")
    edit = doc._edits.get((0, para.key))
    if not check(failures, tag, edit is not None, "no staged edit found"):
        return
    wrap = doc._wrap_for_edit_cached(0, para, edit)
    just_lines = [ln for ln in wrap.lines if ln.word_origins]
    if not check(failures, tag, len(just_lines) >= 2,
                 f"only {len(just_lines)} justified lines (want >= 2: the "
                 f"fixture paragraph must wrap)"):
        return

    out = os.path.join(tmpdir, "c3_justify.pdf")
    doc.save_as(out)
    doc.close()

    # Per-word draw contract (reflow word_origins): each word's first glyph
    # pen origin in the SAVED file matches the engine's stretched x within
    # 0.5 pt on the line's baseline.
    chars = _page_chars(out)
    worst = 0.0
    for ln in just_lines:
        baseline = ln.origin[1]
        for word, wx in ln.word_origins:
            best = None
            for ch, x, y in chars:
                if ch == word[0] and abs(y - baseline) < 1.0:
                    d = abs(x - wx)
                    if best is None or d < best:
                        best = d
            if not check(failures, tag, best is not None,
                         f"word {word!r} of a justified line not found at "
                         f"baseline y={baseline:.1f} in the saved file"):
                return
            worst = max(worst, best)
    print(f"  [info] justify: {len(just_lines)} lines, worst pen-origin "
          f"delta {worst:.3f} pt")
    check(failures, tag, worst <= 0.5,
          f"saved word origins drift {worst:.3f} pt from wrap word_origins "
          f"(want <= 0.5)")


def test_c4_rotated_direction(failures: list[str], tmpdir: str) -> None:
    """Writing direction survives the batch on both rotation axes."""
    tag = "C4_rotated_direction"

    # (a) /Rotate 90 page: the reinserted text stays upright in TEXT space,
    # so in DISPLAY space (through rotation_matrix) the word reads vertically
    # -- its displayed rect is taller than wide (mirrors test_pages rotated
    # coverage: the flag spins the display, not the content).
    doc = PDFDocument(ROTATED)
    target = find_box(doc, 0, "ninety")
    if not check(failures, tag, target is not None,
                 "rotated-page target not found"):
        return
    doc.stage_edit(0, target, target.text.replace("ninety", "NINETYQX"))
    out = os.path.join(tmpdir, "c4_rotated.pdf")
    doc.save_as(out)
    doc.close()
    sd = fitz.open(out)
    try:
        page = sd[0]
        check(failures, tag, page.rotation == 90,
              f"saved /Rotate {page.rotation} != 90")
        hits = [w for w in page.get_text("words") if w[4] == "NINETYQX"]
        if check(failures, tag, len(hits) == 1,
                 f"{len(hits)} NINETYQX words in the saved file (want 1)"):
            disp = fitz.Rect(hits[0][:4]) * page.rotation_matrix
            check(failures, tag, disp.height > disp.width,
                  f"replacement reads horizontally on the rotated display "
                  f"(rect {disp}): writing direction lost in the batch")
    finally:
        sd.close()

    # (b) The morph path: a run drawn ROTATED IN TEXT SPACE (rawdict dir !=
    # (1,0)) must keep its direction through Shape.insert_text(morph=...).
    src = os.path.join(tmpdir, "c4_morph_src.pdf")
    d = fitz.open()
    try:
        p = d.new_page(width=612, height=792)
        p.insert_text((72, 700), "Acme Corp ledger margin note", fontsize=14,
                      morph=(fitz.Point(72, 700), fitz.Matrix(90)))
        p.insert_text((250, 400), "Jordan Carter baseline text", fontsize=12)
        d.save(src)
    finally:
        d.close()
    doc = PDFDocument(src)
    vert = find_box(doc, 0, "ledger")
    if not check(failures, tag, vert is not None, "vertical run not found"):
        return
    check(failures, tag, abs(vert.dir[0]) < 0.01 and abs(vert.dir[1] + 1) < 0.01,
          f"fixture vertical run has dir {vert.dir} (expected (0,-1))")
    doc.stage_edit(0, vert, vert.text.replace("ledger", "ARCHIVEQX"))
    out2 = os.path.join(tmpdir, "c4_morph.pdf")
    doc.save_as(out2)
    doc.close()
    sd = fitz.open(out2)
    try:
        found = None
        for blk in sd[0].get_text("rawdict")["blocks"]:
            if blk.get("type", 0) != 0:
                continue
            for line in blk["lines"]:
                t = "".join(c["c"] for sp in line["spans"]
                            for c in sp.get("chars", []))
                if "ARCHIVEQX" in t:
                    found = (line["dir"], line["spans"][0]["bbox"])
        if check(failures, tag, found is not None,
                 "morphed replacement not found in the saved file"):
            (dx, dy), bb = found
            check(failures, tag, abs(dx) < 0.01 and abs(dy + 1) < 0.01,
                  f"morphed replacement dir ({dx:.3f},{dy:.3f}) != (0,-1): "
                  f"morph not carried through the batch")
            check(failures, tag, (bb[3] - bb[1]) > (bb[2] - bb[0]),
                  f"morphed replacement bbox {bb} is wider than tall "
                  f"(flattened to horizontal)")
    finally:
        sd.close()


# ==========================================================================
# D: foundation seams (M4)
# ==========================================================================
def test_d1_font_index_thread(failures: list[str]) -> None:
    """Concurrent index builds collapse to ONE scan; prewarm == cold list."""
    tag = "D1_font_index_thread"

    # The cold synchronous reference (also the state we restore at the end).
    real_index = FontEngine._build_system_index()
    real_families = FontEngine.available_families()
    real_scan_attr = FontEngine.__dict__["_scan_system_faces"]

    calls = {"n": 0}

    def counting_scan(cls):
        calls["n"] += 1
        return real_scan_attr.__func__(cls)

    FontEngine._scan_system_faces = classmethod(counting_scan)
    try:
        # Two threads race into _build_system_index from a cold class: the
        # double-checked lock must admit exactly ONE scan.
        FontEngine._system_index = None
        FontEngine._available_families = None
        barrier = threading.Barrier(2)
        results = [None, None]

        def build(slot: int) -> None:
            barrier.wait()
            results[slot] = FontEngine._build_system_index()

        threads = [threading.Thread(target=build, args=(i,)) for i in (0, 1)]
        t0 = time.perf_counter()
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)
        t1 = time.perf_counter()
        print(f"  [info] concurrent font-index build: {(t1 - t0) * 1000:.1f} ms,"
              f" {len(FontEngine._system_index or [])} faces")
        check(failures, tag, calls["n"] == 1,
              f"two concurrent builders ran the scan {calls['n']}x (want "
              f"exactly 1: the lock must admit one build)")
        check(failures, tag, results[0] is results[1]
              and results[0] is FontEngine._system_index,
              "racing builders did not both receive the ONE cached index")

        # prewarm_system_index (the startup path): one background build, and
        # available_families afterwards matches the cold synchronous list.
        FontEngine._system_index = None
        FontEngine._available_families = None
        calls["n"] = 0
        worker = FontEngine.prewarm_system_index()
        worker.join(timeout=60)
        check(failures, tag, not worker.is_alive(),
              "prewarm thread did not finish in 60 s")
        check(failures, tag, calls["n"] == 1,
              f"prewarm ran the scan {calls['n']}x (want 1)")
        fams = FontEngine.available_families()
        check(failures, tag, fams == real_families,
              "prewarmed available_families() differs from a cold synchronous "
              "build")
        check(failures, tag, calls["n"] == 1,
              "available_families after prewarm re-ran the scan (must reuse "
              "the prewarmed index)")
    finally:
        FontEngine._scan_system_faces = real_scan_attr
        FontEngine._system_index = real_index
        FontEngine._available_families = real_families


def _scene_census_expected(view) -> int:
    """Every scene item the view is EXPECTED to own in an idle, unselected
    state: one placeholder per layer, plus shadow+sheet+pixmap+hotspots+
    extra_items per rendered layer. Any surplus = an accidental item."""
    n = 0
    for ly in view._layers:
        n += 1                                   # placeholder (always built)
        if ly.rendered:
            n += 3 + len(ly.hotspots) + len(ly.extra_items)
    return n


def test_d2_page_item_factory(failures: list[str]) -> None:
    """Factory lifecycle on three_page.pdf + the no-factory census guard."""
    tag = "D2_page_item_factory"
    w = open_window(THREE)
    try:
        view = w.view
        view.clear_selection()
        pump()

        # Guard: with NO factories, extra_items stays empty everywhere and the
        # scene census is exactly the pre-M4 item set.
        check(failures, tag,
              all(ly.extra_items == [] for ly in view._layers),
              "extra_items non-empty with no factories registered")
        census = len(view.scene().items())
        expected = _scene_census_expected(view)
        check(failures, tag, census == expected,
              f"no-factory scene census {census} != expected {expected} "
              f"(accidental extra items?)")

        # Register a factory returning one rect item per page.
        made: list[tuple[int, object]] = []

        def factory(layer, view_arg):
            item = QGraphicsRectItem(QRectF(layer.x_left, layer.y_top, 24, 24))
            item.setZValue(5)            # a reserved overlay slot (registry)
            made.append((layer.page_index, view_arg))
            return [item]

        view.register_page_item_factory(factory)
        view.reload()                    # re-materializes the visible band
        pump(2)

        rendered = [ly for ly in view._layers if ly.rendered]
        if not check(failures, tag, len(rendered) >= 1,
                     "no rendered layers after reload"):
            return
        check(failures, tag,
              all(len(ly.extra_items) == 1 for ly in rendered),
              f"per-page extra_items "
              f"{[len(ly.extra_items) for ly in rendered]} != 1 per "
              f"materialized page")
        check(failures, tag,
              all(it.scene() is view.scene()
                  for ly in rendered for it in ly.extra_items),
              "factory items not added to the scene")
        check(failures, tag, all(v is view for _pi, v in made),
              "factory not called with the PageView")
        census = len(view.scene().items())
        expected = _scene_census_expected(view)
        check(failures, tag, census == expected,
              f"factory scene census {census} != expected {expected}")

        # Dematerialize removes + clears; re-materialize re-creates.
        layer = rendered[0]
        old_item = layer.extra_items[0]
        view._dematerialize_page(layer)
        check(failures, tag, layer.extra_items == [],
              "dematerialize left extra_items populated")
        check(failures, tag, old_item.scene() is None,
              "dematerialize left the factory item in the scene")
        view._materialize_page(layer)
        check(failures, tag, len(layer.extra_items) == 1
              and layer.extra_items[0] is not old_item
              and layer.extra_items[0].scene() is view.scene(),
              "re-materialize did not re-create the factory item")
    finally:
        close_window(w)


def test_d2b_selection_eviction_guard(failures: list[str],
                                      tmpdir: str) -> None:
    """The lazy-render band never evicts the SELECTED box's page (the guard
    the old code promised in its comment but only applied to the editor)."""
    tag = "D2b_eviction_guard"
    path = _build_thirty_page(tmpdir)
    w = open_window(path)
    try:
        view = w.view
        layer0 = view._layers[0]
        if not layer0.rendered:
            view._materialize_page(layer0)
        if not check(failures, tag, bool(layer0.boxes),
                     "page 0 has no boxes"):
            return
        view.select_box(layer0.boxes[0])
        pump()

        # Scroll to the far end: page 0 leaves the eviction band.
        vbar = view.verticalScrollBar()
        vbar.setValue(vbar.maximum())
        pump(3)
        check(failures, tag, layer0.rendered,
              "the selection's page was dematerialized by the scroll band "
              "(eviction guard regression)")
        check(failures, tag,
              any(not ly.rendered for ly in view._layers[1:6]),
              "no unselected near-top page was evicted (the scroll never "
              "exercised the eviction path; test inconclusive)")

        # Clearing the selection releases the pin: the next band update may
        # evict page 0 like any other off-band page.
        view.clear_selection()
        view._update_lazy_render()
        pump()
        check(failures, tag, not layer0.rendered,
              "page 0 stayed pinned after the selection cleared")
    finally:
        close_window(w)


def test_d3_menubar_census(failures: list[str]) -> None:
    """7 skeleton titles; pre-existing actions reachable with their original
    shortcuts; all 9 anchors present, separators, parented per the ws7 table."""
    tag = "D3_menubar_census"
    w = open_window(THREE)
    try:
        bar = w.menu_bar
        titles = [a.text() for a in bar.actions()]
        check(failures, tag,
              titles == ["File", "Edit", "View", "Tools", "Document",
                         "Window", "Help"],
              f"menubar titles {titles}")

        # PySide 6.11 hazard (probe-verified on 6.11.1): a recursive menu walk
        # that lets the intermediate QMenu/QAction wrappers die mid-walk lets
        # shiboken/gc DELETE the live C++ menus and their separator actions
        # out from under the window. Keeping every wrapper alive in ``keep``
        # for the duration of the walk is the verified-safe pattern -- the
        # ws7 shortcut cheatsheet (which walks the menubar the same way) must
        # use it too.
        keep: list = []

        def walk(menu) -> list:
            keep.append(menu)
            out = []
            for act in menu.actions():
                keep.append(act)
                sub = act.menu()
                if sub is not None:
                    out.extend(walk(sub))
                elif not act.isSeparator():
                    out.append(act)
            return out

        reachable: list = []
        for top in bar.actions():
            keep.append(top)
            sub = top.menu()
            if sub is not None:
                reachable.extend(walk(sub))

        # Every pre-existing QAction keeps a menubar home + original shortcut.
        pre_existing = [
            ("act_open", w.act_open, QKeySequence(QKeySequence.Open)),
            ("act_save", w.act_save, QKeySequence(QKeySequence.Save)),
            ("act_save_as", w.act_save_as, QKeySequence(QKeySequence.SaveAs)),
            # Close Tab is pinned to the explicit Ctrl+W by the navigation
            # workstream's menu table (the registry of record): StandardKey
            # Close resolves to Ctrl+F4 on this platform theme.
            ("act_close", w.act_close, QKeySequence("Ctrl+W")),
            ("act_undo", w.act_undo, QKeySequence(QKeySequence.Undo)),
            ("act_redo", w.act_redo, QKeySequence("Shift+Ctrl+Z")),
            ("act_delete", w.act_delete, QKeySequence(QKeySequence.Delete)),
            ("act_find", w.act_find, QKeySequence(QKeySequence.Find)),
            ("act_add_text", w.act_add_text, QKeySequence("T")),
            ("act_next", w.act_next, QKeySequence("Alt+Ctrl+Right")),
            ("act_prev", w.act_prev, QKeySequence("Alt+Ctrl+Left")),
            ("act_first", w.act_first, QKeySequence("Ctrl+Up")),
            ("act_last", w.act_last, QKeySequence("Ctrl+Down")),
            ("act_goto", w.act_goto, QKeySequence("Ctrl+G")),
            ("act_zoom_in", w.act_zoom_in, QKeySequence(QKeySequence.ZoomIn)),
            ("act_zoom_out", w.act_zoom_out,
             QKeySequence(QKeySequence.ZoomOut)),
            ("act_actual_size", w.act_actual_size, QKeySequence("Ctrl+0")),
            ("act_fit_page", w.act_fit_page, QKeySequence("Ctrl+9")),
            ("act_fit_width", w.act_fit_width, QKeySequence("Ctrl+8")),
            ("act_toggle_pages", w.act_toggle_pages,
             QKeySequence("Ctrl+Shift+P")),
            ("act_rotate_cw", w.act_rotate_cw, QKeySequence("Ctrl+R")),
            ("act_rotate_ccw", w.act_rotate_ccw,
             QKeySequence("Ctrl+Shift+R")),
            ("act_combine", w.act_combine, None),
            ("act_extract", w.act_extract, None),
            ("act_split", w.act_split, None),
            ("act_duplicate_page", w.act_duplicate_page, None),
            ("act_insert_blank", w.act_insert_blank, None),
            ("act_delete_page", w.act_delete_page, None),
        ]
        for name, act, seq in pre_existing:
            check(failures, tag, any(a is act for a in reachable),
                  f"{name} not reachable from the menubar")
            if seq is not None:
                check(failures, tag, act.shortcut() == seq,
                      f"{name} shortcut {act.shortcut().toString()!r} != "
                      f"original {seq.toString()!r}")

        # The 9 reserved anchors: present, separators, parented per the table.
        table = {
            "file_output": w.menu_file,
            "edit_extra": w.menu_edit,
            "view_panels": w.menu_view,
            "tools_annotate": w.menu_tools,
            "tools_objects": w.menu_tools,
            "tools_forms": w.menu_tools,
            "doc_transform": w.menu_document,
            "doc_decorate": w.menu_document,
            "doc_file": w.menu_document,
        }
        check(failures, tag, sorted(w.menu_anchors) == sorted(table),
              f"menu_anchors keys {sorted(w.menu_anchors)}")
        for key, menu in table.items():
            anchor = w.menu_anchors.get(key)
            if not check(failures, tag, anchor is not None,
                         f"anchor {key} missing"):
                continue
            check(failures, tag, anchor.isSeparator(),
                  f"anchor {key} is not a separator")
            check(failures, tag, anchor in menu.actions(),
                  f"anchor {key} not parented in the {menu.title()} menu")
        # Multi-anchor menus keep the table's order (insertions stay sorted).
        tools_acts = w.menu_tools.actions()
        check(failures, tag,
              tools_acts.index(w.menu_anchors["tools_annotate"])
              < tools_acts.index(w.menu_anchors["tools_objects"])
              < tools_acts.index(w.menu_anchors["tools_forms"]),
              "Tools anchors out of table order")
        doc_acts = w.menu_document.actions()
        check(failures, tag,
              doc_acts.index(w.menu_anchors["doc_transform"])
              < doc_acts.index(w.menu_anchors["doc_decorate"])
              < doc_acts.index(w.menu_anchors["doc_file"]),
              "Document anchors out of table order")
    finally:
        close_window(w)


def test_d4_mode_dispatch(failures: list[str]) -> None:
    """current_mode() transitions + the register_mode_handlers seam."""
    tag = "D4_mode_dispatch"
    w = open_window(FORM)
    try:
        view, doc = w.view, w.document
        check(failures, tag, view.current_mode() == "select",
              f"initial mode {view.current_mode()!r} != 'select'")
        view.enter_add_text_mode()
        check(failures, tag, view.current_mode() == "add_text",
              f"armed mode {view.current_mode()!r} != 'add_text'")
        view.exit_add_text_mode()
        check(failures, tag, view.current_mode() == "select",
              f"disarmed mode {view.current_mode()!r} != 'select'")

        b = find_box(doc, 0, "Cleared")
        hotspot = view._hotspot_for(b) if b is not None else None
        if not check(failures, tag, hotspot is not None,
                     "fixture hotspot not found"):
            return
        view.begin_edit(hotspot)
        pump()
        check(failures, tag, view.current_mode() == "text_edit",
              f"editing mode {view.current_mode()!r} != 'text_edit'")
        view._cancel_editor_silent()
        pump()
        check(failures, tag, view.current_mode() == "select",
              f"post-edit mode {view.current_mode()!r} != 'select'")

        # The extension seam: a NEW mode registers press/key handlers and the
        # view routes events to them with zero inline-branch edits.
        hits = {"press": 0, "key": 0}

        def on_press(event) -> bool:
            hits["press"] += 1
            event.accept()
            return True

        def on_key(event) -> bool:
            hits["key"] += 1
            event.accept()
            return True

        view.register_mode_handlers("future_tool", press=on_press, key=on_key)
        view._mode = "future_tool"
        try:
            key_ev = QKeyEvent(QEvent.KeyPress, Qt.Key_A, Qt.NoModifier, "a")
            view.keyPressEvent(key_ev)
            press_ev = QMouseEvent(
                QEvent.MouseButtonPress, QPointF(2.0, 2.0), Qt.LeftButton,
                Qt.LeftButton, Qt.NoModifier)
            view.mousePressEvent(press_ev)
        finally:
            view._mode = "select"
        check(failures, tag, hits["key"] == 1,
              f"registered key handler ran {hits['key']}x (want 1)")
        check(failures, tag, hits["press"] == 1,
              f"registered press handler ran {hits['press']}x (want 1)")
    finally:
        close_window(w)


def main() -> int:
    failures: list[str] = []
    tmpdir = tempfile.mkdtemp(prefix="perf_foundation_")
    tests = [
        ("A1_persistent_engine", lambda: test_a1_persistent_engine(failures)),
        ("A2_neighbors_memo", lambda: test_a2_neighbors_memo(failures)),
        ("A3_wysiwyg_parity", lambda: test_a3_wysiwyg_parity(failures, tmpdir)),
        ("A4_render_signature", lambda: test_a4_render_signature(failures)),
        ("B1_cache_hit", lambda: test_b1_cache_hit(failures)),
        ("B2_repaint_miss", lambda: test_b2_repaint_misses_once(failures)),
        ("B3_rotate_miss", lambda: test_b3_rotate_misses(failures)),
        ("B4_undo_pristine", lambda: test_b4_undo_pristine(failures)),
        ("B5_capacity_swap",
         lambda: test_b5_capacity_and_doc_swap(failures, tmpdir)),
        ("B6_zoom_revisit",
         lambda: test_b6_zoom_revisit_hits(failures, tmpdir)),
        ("C1_batched_shape", lambda: test_c1_batched_shape(failures, tmpdir)),
        ("C2_no_new_base14", lambda: test_c2_no_new_base14(failures, tmpdir)),
        ("C3_justify_origins",
         lambda: test_c3_justify_word_origins(failures, tmpdir)),
        ("C4_rotated_direction",
         lambda: test_c4_rotated_direction(failures, tmpdir)),
        ("D1_font_index_thread",
         lambda: test_d1_font_index_thread(failures)),
        ("D2_page_item_factory",
         lambda: test_d2_page_item_factory(failures)),
        ("D2b_eviction_guard",
         lambda: test_d2b_selection_eviction_guard(failures, tmpdir)),
        ("D3_menubar_census", lambda: test_d3_menubar_census(failures)),
        ("D4_mode_dispatch", lambda: test_d4_mode_dispatch(failures)),
    ]
    try:
        for name, fn in tests:
            print(f"[{name}]")
            try:
                fn()
            except Exception:
                failures.append(f"{name}: raised:\n{traceback.format_exc()}")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    print()
    if failures:
        print(f"FAILED ({len(failures)}):")
        for f in failures:
            print(" -", f)
        return 1
    print("test_perf_foundation: all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
