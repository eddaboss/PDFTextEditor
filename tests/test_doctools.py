"""Headless verification for the PAGE & DOCUMENT TOOLS build, milestones M1
(doc-tools spec §2.1-§2.3: document info & exports), M2 (§2.4-§2.5:
watermark + header/footer stamps), M3 (§2.6: crop), and M4 (§2.7-§2.9:
security, optimize, print).

Covers, against the REAL model + the REAL ``MainWindow`` (offscreen):

  1. METADATA CARRY -- the constructor re-carries the source info dict onto
     the working copy (``insert_pdf`` drops it), so Title/Author survive
     open -> edit -> save_as instead of being silently stripped (the
     probe-verified data-loss regression this milestone fixes).
  2. TOC CARRY -- same constructor block, same class of bug: a 3-entry
     outline survives open -> edit -> save_as unchanged.
  3. METADATA EDITOR -- ``set_metadata_fields`` is ONE structural op;
     ``undo_structural`` restores the old title; non-editable keys raise
     before any snapshot is taken.
  4. ``_baked_copy`` SEAM -- save_as still bakes staged edits through the one
     shared pipeline: WYSIWYG rel ink diff < 0.02 between render_with_edits
     and the saved file (the refactor must not fork the funnel).
  5. EXPORT IMAGES -- ``_do_export_images`` (dialog bypassed via options):
     PNG at 144 dpi is exactly page.rect * 2 px with real ink; a staged edit
     changes the exported pixels in the edited band; JPEG writes too.
  6. EXPORT TEXT -- ``_do_export_text``: form-feed page joins, staged edit
     included, NBSP-normalized, fixture names present.
  7. PROPERTIES FLOW -- ``_do_properties`` with unchanged fields pushes no
     command; a changed title applies + undoes through the window stack; the
     dialogs construct offscreen (never exec'd) with prefilled fields.
  8. STAMP HELPERS -- doctools 9-grid presets + token substitution (Qt-free).
  9. WATERMARK INK -- ``add_watermark`` is ONE structural op across mixed
     page sizes; the center region gains real ink on every stamped page;
     ``undo_structural`` returns the region EXACTLY to baseline; the guard
     validations (empty text / bad position / bad page / bad opacity) raise
     BEFORE any snapshot is taken.
 10. OPACITY + BEHIND -- a 0.25-opacity stamp reads lighter (mean luminance)
     than a 1.0 one; ``behind=True`` is still visible on a blank region.
 11. HEADER/FOOTER TOKENS -- "Page 2 of 3" lands on page 2 via ``get_text``
     ({page} counts start_at + k, {pages} is the doc total); {date} renders
     today; the header/footer bands gain ink at the configured margins.
 12. ROTATED PAGE -- a /Rotate 90 page gets an UPRIGHT header in its
     DISPLAYED top band (ink box wider than tall, horizontally centered).
 13. STAMP x EDIT COEXISTENCE -- an in-place edit on a watermarked page
     keeps the page-diagonal watermark intact (the neighbor heuristic's
     redraw band rescues it through the redaction) with WYSIWYG < 0.02.
 14. NO RESIDUE -- moving a box on a stamped page leaves no residue at any
     original member bbox (the test_editor move pattern), watermark intact.
 15. STAMP WINDOW FLOW -- ``_do_watermark`` / ``_do_header_footer`` (dialog
     bypassed): one undoable structural command through the window stack,
     Document-menu anchor placement, offscreen dialog defaults + live
     preview, bad input flashes without raising, empty state disabled.
 16. CROP MODEL -- ``crop_pages`` is ONE structural op; ``page.rect``
     shrinks to the drawn size; spans() re-extract shifted by EXACTLY
     cropbox.tl; a second crop composes (rect in the new text space); a
     staged edit made BEFORE the crop survives in the cropped save with
     WYSIWYG parity; ``undo_structural`` restores the rect; sub-36pt /
     empty-page-list / out-of-range guards raise BEFORE any snapshot.
 17. CROP SCOPE + CLAMP -- all-pages scope on three_page.pdf (mixed sizes)
     clamps per page to each mediabox; a band off page 1's shorter mediabox
     SKIPS just that page (returned, others crop); every-page-skipped
     raises; a /Rotate 90 page crops in unrotated coords with the displayed
     rect swapped.
 18. CROP UI FLOW -- ``act_crop`` (checkable, at the doc_transform anchor)
     arms the canvas crop mode with the gesture toast; a synthesized
     press/drag/release emits ``cropRectSelected`` with the expected
     text-space rect (scope provider seam: cancel keeps the mode armed); a
     stray click emits nothing; Esc disarms + unchecks; ``_do_crop_apply``
     (dialog bypassed) runs the op, reloads (layer pt_size matches), undoes
     through the window stack, and flashes a bad rect without raising;
     ``_CropDialog`` constructs offscreen; empty state disabled.
 19. PASSWORD GATE -- on a runtime-generated AES-256 fixture (never
     committed), ``PDFDocument(path)`` raises ``PasswordRequired`` (no /
     wrong password) BEFORE any content access; the right password opens
     with metadata carried and spans extracting; an edit + save_as writes
     ``needs_pass`` output that authenticates and holds the edit (WYSIWYG
     after auth); ``set_security(encryption=NONE)`` + save yields a plain
     file; dirty flips on ``set_security`` (no undo entry) and clears on
     ``mark_clean``; unknown security keys raise.
 20. SECURITY WINDOW FLOW -- ``open_path`` with an injected
     ``_password_provider`` opens the tab (first ask, wrong-then-right,
     Cancel aborts silently, three failures abort with a flash);
     ``_do_security`` (dialog bypassed) stages remove/set with the
     apply-on-save toast and NO undo command; the encrypted Save As
     reload regression (``_reload_after_save`` passes
     ``reopen_password``); File/Document menu anchor order incl. Cmd+P;
     ``_SecurityDialog`` constructs offscreen (status line, confirm
     matching arms OK, Remove accepts); empty state disabled.
 21. OPTIMIZE -- ``save_optimized_copy`` on the deflate=False fixture:
     after < before with identical page text, non-mutating (no dirty, no
     undo, path keeps pointing at the original); ``_do_optimize`` (picker
     bypassed) includes a staged edit and toasts the size delta; pending
     security kwargs apply to the optimized copy.
 22. PRINT -- ``_do_print`` with an injected PdfFormat QPrinter writes
     page_count == doc pages with real ink per page; a staged edit
     changes printed page 1; fromPage/toPage prints just the range;
     empty state no-op.

Run:
    QT_QPA_PLATFORM=offscreen \
      /Users/edward/Documents/GitHub/PDFTextEditor/.venv/bin/python \
      tests/test_doctools.py
"""

from __future__ import annotations

import datetime
import os
import sys
import tempfile
import traceback

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import fitz  # noqa: E402
import numpy as np  # noqa: E402
from PySide6.QtCore import QEvent, QPointF, Qt  # noqa: E402
from PySide6.QtGui import QKeyEvent, QMouseEvent  # noqa: E402
from PySide6.QtPrintSupport import QPrinter  # noqa: E402
from PySide6.QtWidgets import QApplication, QDialog  # noqa: E402

from pdftexteditor import doctools  # noqa: E402
from pdftexteditor.document import (  # noqa: E402
    PDF_ENCRYPT_AES_256,
    PDF_ENCRYPT_NONE,
    PasswordRequired,
    PDFDocument,
)
from pdftexteditor.ui.doc_dialogs import (  # noqa: E402
    ExportImagesOptions,
    HeaderFooterOptions,
    SecurityOptions,
    WatermarkOptions,
    _CropDialog,
    _ExportImagesDialog,
    _HeaderFooterDialog,
    _PropertiesDialog,
    _SecurityDialog,
    _WatermarkDialog,
)
from pdftexteditor.ui.main_window import MainWindow  # noqa: E402

_APP = QApplication.instance() or QApplication(sys.argv)

FIXTURES = os.path.join(REPO, "tests", "fixtures")
DOC_TOOLS = os.path.join(FIXTURES, "doc_tools.pdf")
THREE_PAGE = os.path.join(FIXTURES, "three_page.pdf")
ROTATED = os.path.join(FIXTURES, "rotated_doc.pdf")
GEORGIA = "/System/Library/Fonts/Supplemental/Georgia.ttf"

TITLE = "Quarterly Review Agenda"
AUTHOR = "Jordan Carter"

# A blank square dead-center on three_page.pdf page 0/2 (612x792): no fixture
# ink lives there, so it isolates the default centered watermark's pixels.
WM_REGION = (276.0, 366.0, 336.0, 426.0)


def check(failures: list, tag: str, cond: bool, msg: str) -> bool:
    if not cond:
        failures.append(f"{tag}: {msg}")
    return cond


def pump(n: int = 4) -> None:
    for _ in range(n):
        _APP.processEvents()


def open_window(path: str) -> MainWindow:
    w = MainWindow()
    w.resize(1300, 950)
    w.show()
    w.open_path(path)
    pump(6)
    return w


def close(w: MainWindow) -> None:
    w._suppress_close_guard = True
    w.close()


def find_span(doc: PDFDocument, page: int, needle: str):
    """The first box on ``page`` whose (space-normalized) text contains
    ``needle``."""
    for b in doc.spans(page):
        if needle in b.text.replace("\u00a0", " "):
            return b
    return None


def png_array(path: str) -> np.ndarray:
    pix = fitz.Pixmap(path)
    return np.frombuffer(bytes(pix.samples), dtype=np.uint8).reshape(
        pix.height, pix.width, pix.n)


def pix_ink(pix) -> int:
    arr = np.frombuffer(bytes(pix.samples), dtype=np.uint8).reshape(
        pix.height, pix.width, pix.n)
    lum = (0.299 * arr[:, :, 0] + 0.587 * arr[:, :, 1]
           + 0.114 * arr[:, :, 2])
    return int((lum < 230).sum())


def array_ink(arr: np.ndarray) -> int:
    lum = (0.299 * arr[:, :, 0] + 0.587 * arr[:, :, 1]
           + 0.114 * arr[:, :, 2])
    return int((lum < 230).sum())


def norm(text: str) -> str:
    """NBSP-normalize extracted text (the fixture whitespace contract)."""
    return text.replace(" ", " ")


def _clip_array(src, bbox, page: int, scale: float) -> np.ndarray:
    """Pixel array of a clipped page region; ``src`` is an open
    ``fitz.Document`` (e.g. ``doc.working``) or a saved-file path."""
    d = fitz.open(src) if isinstance(src, str) else src
    try:
        pix = d[page].get_pixmap(matrix=fitz.Matrix(scale, scale),
                                 clip=fitz.Rect(bbox), alpha=False)
        return np.frombuffer(bytes(pix.samples), dtype=np.uint8).reshape(
            pix.height, pix.width, pix.n)
    finally:
        if isinstance(src, str):
            d.close()


def region_ink(src, bbox, page: int = 0, scale: float = 3.0) -> int:
    """Non-white pixels in a page region (the test_editor region pattern):
    proves stamped/edited glyphs landed -- or that none remain."""
    return array_ink(_clip_array(src, bbox, page, scale))


def region_lum(src, bbox, page: int = 0, scale: float = 3.0) -> float:
    """Mean luminance 0..255 of a region: a MORE translucent stamp reads
    LIGHTER (higher value) over white paper."""
    arr = _clip_array(src, bbox, page, scale)
    lum = (0.299 * arr[:, :, 0] + 0.587 * arr[:, :, 1]
           + 0.114 * arr[:, :, 2])
    return float(lum.mean())


def wysiwyg_rel(doc: PDFDocument, out_path: str, page: int = 0) -> float:
    """Whole-page ink of render_with_edits vs the saved file (the global
    WYSIWYG parity gate, < 0.02)."""
    rink = pix_ink(doc.render_with_edits(page, 2.0))
    sd = fitz.open(out_path)
    try:
        sink = pix_ink(sd[page].get_pixmap(matrix=fitz.Matrix(2, 2),
                                           alpha=False))
    finally:
        sd.close()
    return abs(rink - sink) / max(sink, 1)


# ---------------------------------------------------------------------------
# 1. Metadata carry (the save-strips-Title/Author regression)
# ---------------------------------------------------------------------------
def test_metadata_carry(failures: list) -> None:
    td = tempfile.mkdtemp(prefix="doctools_meta_")
    doc = PDFDocument(DOC_TOOLS)
    try:
        fields = doc.metadata_fields()
        check(failures, "meta.carry_title", fields.get("title") == TITLE,
              f"working title {fields.get('title')!r} != {TITLE!r}")
        check(failures, "meta.carry_author", fields.get("author") == AUTHOR,
              f"working author {fields.get('author')!r} != {AUTHOR!r}")
        check(failures, "meta.pdf_version",
              str(doc.pdf_version).startswith("PDF"),
              f"pdf_version {doc.pdf_version!r} not captured")

        # Clean save: metadata survives with no edits staged.
        out_clean = os.path.join(td, "clean.pdf")
        doc.save_as(out_clean)
        saved = fitz.open(out_clean)
        try:
            check(failures, "meta.clean_save",
                  saved.metadata.get("title") == TITLE
                  and saved.metadata.get("author") == AUTHOR,
                  f"clean save stripped metadata: {saved.metadata!r}")
        finally:
            saved.close()

        # Edited save: the regression case -- an in-place edit then save_as
        # must keep Title/Author AND apply the edit.
        span = find_span(doc, 0, "revenue")
        if not check(failures, "meta.span", span is not None,
                     "no 'revenue' span on page 0 of doc_tools.pdf"):
            return
        new_text = span.text.replace("\u00a0", " ").replace(
            "revenue", "earnings")
        doc.stage_edit(0, span, new_text)
        out_edit = os.path.join(td, "edited.pdf")
        doc.save_as(out_edit)

        # _baked_copy seam guard: render_with_edits and the saved file are
        # the SAME pipeline -- whole-page rel ink diff < 0.02.
        rink = pix_ink(doc.render_with_edits(0, 2.0))
        saved = fitz.open(out_edit)
        try:
            sink = pix_ink(saved[0].get_pixmap(
                matrix=fitz.Matrix(2, 2), alpha=False))
            page_text = saved[0].get_text().replace("\u00a0", " ")
            check(failures, "meta.edited_save",
                  saved.metadata.get("title") == TITLE
                  and saved.metadata.get("author") == AUTHOR,
                  f"edited save stripped metadata: {saved.metadata!r}")
            check(failures, "meta.edit_applied",
                  "earnings" in page_text and "revenue" not in page_text,
                  f"edit not applied in saved page text: {page_text[:120]!r}")
        finally:
            saved.close()
        rel = abs(rink - sink) / max(sink, 1)
        check(failures, "meta.wysiwyg", rel < 0.02,
              f"render vs saved ink rel diff {rel:.4f} >= 0.02")

        # properties() aggregate + fonts_used().
        props = doc.properties()
        check(failures, "meta.props_pages",
              props["page_count"] == 3
              and props["page_sizes"] == [(612.0, 792.0)],
              f"properties pages wrong: {props['page_count']} "
              f"{props['page_sizes']}")
        check(failures, "meta.props_size", props["file_size"] > 0,
              "properties file_size is 0")
        check(failures, "meta.props_meta",
              props["metadata"].get("title") == TITLE
              and not props["encrypted"],
              f"properties metadata/encryption wrong: {props['metadata']!r} "
              f"encrypted={props['encrypted']}")
        fonts = doc.fonts_used()
        names = [f[0] for f in fonts]
        check(failures, "meta.fonts",
              "Georgia Regular" in names and "Georgia Bold" in names
              and all(f[2] for f in fonts),
              f"fonts_used wrong: {fonts!r}")
    finally:
        doc.close()


# ---------------------------------------------------------------------------
# 2. TOC carry (bookmarks survive open -> edit -> save)
# ---------------------------------------------------------------------------
def test_toc_carry(failures: list) -> None:
    td = tempfile.mkdtemp(prefix="doctools_toc_")
    src_path = os.path.join(td, "toc_src.pdf")
    toc = [[1, "Overview", 1], [2, "Detail", 2], [1, "Appendix", 3]]
    src = fitz.open()
    for i in range(3):
        page = src.new_page(width=612, height=792)
        page.insert_text((72, 100), f"Section text on sheet {i + 1}",
                         fontname="F0", fontfile=GEORGIA, fontsize=12)
    src.set_toc(toc)
    src.save(src_path)
    src.close()

    doc = PDFDocument(src_path)
    try:
        check(failures, "toc.carried",
              doc.working.get_toc() == toc,
              f"constructor dropped the TOC: {doc.working.get_toc()!r}")
        span = find_span(doc, 0, "Section text")
        if not check(failures, "toc.span", span is not None,
                     "no editable span in the runtime TOC fixture"):
            return
        doc.stage_edit(0, span, "Amended section text on sheet 1")
        out = os.path.join(td, "toc_out.pdf")
        doc.save_as(out)
        saved = fitz.open(out)
        try:
            check(failures, "toc.survives_save",
                  saved.get_toc() == toc,
                  f"save_as wiped/changed the TOC: {saved.get_toc()!r}")
            check(failures, "toc.edit_applied",
                  "Amended" in saved[0].get_text(),
                  "staged edit missing from the saved TOC fixture")
        finally:
            saved.close()
    finally:
        doc.close()


# ---------------------------------------------------------------------------
# 3. set_metadata_fields as ONE structural op + undo_structural
# ---------------------------------------------------------------------------
def test_metadata_editor(failures: list) -> None:
    doc = PDFDocument(DOC_TOOLS)
    try:
        depth0 = doc.structural_depth
        doc.set_metadata_fields({"title": "Amended Agenda"})
        check(failures, "edit.applied",
              doc.metadata_fields()["title"] == "Amended Agenda",
              f"title not staged: {doc.metadata_fields()!r}")
        check(failures, "edit.one_op",
              doc.structural_depth == depth0 + 1,
              f"set_metadata_fields pushed {doc.structural_depth - depth0} "
              f"snapshots, expected 1")
        check(failures, "edit.dirty", doc.dirty,
              "doc not dirty after a metadata change")
        # Untouched fields merge through unchanged.
        check(failures, "edit.merge",
              doc.metadata_fields()["author"] == AUTHOR,
              f"author lost in the merge: {doc.metadata_fields()!r}")

        doc.undo_structural()
        check(failures, "edit.undo",
              doc.metadata_fields()["title"] == TITLE,
              f"undo_structural did not restore the title: "
              f"{doc.metadata_fields()!r}")
        doc.redo_structural()
        check(failures, "edit.redo",
              doc.metadata_fields()["title"] == "Amended Agenda",
              "redo_structural did not re-apply the title")

        try:
            doc.set_metadata_fields({"creator": "someone"})
            check(failures, "edit.guard", False,
                  "non-editable key did not raise ValueError")
        except ValueError:
            pass
        check(failures, "edit.guard_no_snapshot",
              doc.structural_depth == depth0 + 1,
              "the rejected set still pushed a snapshot")
    finally:
        doc.close()


# ---------------------------------------------------------------------------
# 4. Export pages as images (window flow, dialog bypassed)
# ---------------------------------------------------------------------------
def test_export_images(failures: list) -> None:
    td = tempfile.mkdtemp(prefix="doctools_img_")
    w = open_window(DOC_TOOLS)
    try:
        out1 = os.path.join(td, "clean")
        os.makedirs(out1)
        w._do_export_images(ExportImagesOptions(
            fmt="png", dpi=144, pages="1-3", out_dir=out1))
        names = sorted(os.listdir(out1))
        check(failures, "img.files",
              names == [f"doc_tools_page{n:03d}.png" for n in (1, 2, 3)],
              f"unexpected export filenames: {names}")
        if not names:
            return
        arr = png_array(os.path.join(out1, names[0]))
        # 144 dpi == zoom 2.0: a 612x792pt page is exactly 1224x1584 px.
        check(failures, "img.dims",
              arr.shape[0] == 1584 and arr.shape[1] == 1224,
              f"page 1 PNG is {arr.shape[1]}x{arr.shape[0]}, "
              f"expected 1224x1584")
        check(failures, "img.ink", array_ink(arr) > 1000,
              f"page 1 PNG has almost no ink ({array_ink(arr)}px)")

        # A staged edit must change the exported pixels (the export rides
        # render_with_edits, the save pipeline).
        doc = w.document
        span = find_span(doc, 0, "revenue")
        if not check(failures, "img.span", span is not None,
                     "no 'revenue' span for the edit-diff check"):
            return
        doc.stage_edit(0, span, span.text.replace("\u00a0", " ").replace(
            "revenue", "earnings"))
        out2 = os.path.join(td, "edited")
        os.makedirs(out2)
        w._do_export_images(ExportImagesOptions(
            fmt="png", dpi=144, pages="1", out_dir=out2))
        edited = png_array(os.path.join(out2, "doc_tools_page001.png"))
        y0, y1 = int(span.bbox[1] * 2) - 4, int(span.bbox[3] * 2) + 4
        band_diff = int((arr[y0:y1] != edited[y0:y1]).sum())
        check(failures, "img.edit_diff", band_diff > 0,
              "staged edit did not change the exported PNG in its band")

        # JPEG flavor.
        w._do_export_images(ExportImagesOptions(
            fmt="jpg", dpi=72, pages="2", out_dir=td))
        jpg = os.path.join(td, "doc_tools_page002.jpg")
        check(failures, "img.jpeg", os.path.exists(jpg),
              "JPEG export did not write doc_tools_page002.jpg")
        if os.path.exists(jpg):
            jarr = png_array(jpg)
            check(failures, "img.jpeg_dims", jarr.shape[1] == 612,
                  f"72dpi JPEG width {jarr.shape[1]} != 612")

        # A bad range surfaces as a flash, never an exception or files.
        bad = os.path.join(td, "bad")
        os.makedirs(bad)
        w._do_export_images(ExportImagesOptions(
            fmt="png", dpi=144, pages="0", out_dir=bad))
        check(failures, "img.bad_range", os.listdir(bad) == [],
              "an out-of-range export still wrote files")
    finally:
        close(w)


# ---------------------------------------------------------------------------
# 5. Export all text (window flow)
# ---------------------------------------------------------------------------
def test_export_text(failures: list) -> None:
    td = tempfile.mkdtemp(prefix="doctools_txt_")

    # Clean model-level export first: original wording, no Qt bake needed.
    doc = PDFDocument(DOC_TOOLS)
    try:
        p0 = os.path.join(td, "clean.txt")
        doc.export_text(p0)
        clean = open(p0, encoding="utf-8").read()
        check(failures, "txt.clean", "revenue" in clean,
              "clean export lost the original wording")
    finally:
        doc.close()

    w = open_window(DOC_TOOLS)
    try:
        doc = w.document
        span = find_span(doc, 0, "revenue")
        if not check(failures, "txt.span", span is not None,
                     "no 'revenue' span for the export-text check"):
            return
        doc.stage_edit(0, span, span.text.replace("\u00a0", " ").replace(
            "revenue", "earnings"))
        out = os.path.join(td, "edited.txt")
        w._do_export_text(out)
        if not check(failures, "txt.written", os.path.exists(out),
                     "_do_export_text wrote nothing"):
            return
        text = open(out, encoding="utf-8").read()
        check(failures, "txt.edit", "earnings" in text
              and "revenue" not in text,
              "staged edit missing from the exported text")
        check(failures, "txt.names", AUTHOR in text and "Acme Corp" in text,
              "fixture names missing (NBSP normalization broken?)")
        check(failures, "txt.normalized",
              "\u00a0" not in text and "\u00ad" not in text,
              "exported text still carries NBSP / soft hyphens")
        check(failures, "txt.pages", text.count("\f") == 2,
              f"expected 2 form feeds for 3 pages, got {text.count(chr(12))}")
    finally:
        close(w)


# ---------------------------------------------------------------------------
# 6. Properties window flow + offscreen dialog construction + chrome wiring
# ---------------------------------------------------------------------------
def test_properties_flow(failures: list) -> None:
    w = open_window(DOC_TOOLS)
    try:
        doc = w.document
        depth0 = doc.structural_depth

        # Unchanged fields -> NO structural command.
        w._do_properties(apply_meta=doc.metadata_fields())
        check(failures, "prop.noop", doc.structural_depth == depth0,
              "unchanged properties still pushed a structural op")

        # One changed field applies + undoes/redoes through the WINDOW stack.
        fields = dict(doc.metadata_fields())
        fields["title"] = "Window Amended Agenda"
        w._do_properties(apply_meta=fields)
        check(failures, "prop.applied",
              doc.metadata_fields()["title"] == "Window Amended Agenda",
              f"window flow did not apply: {doc.metadata_fields()!r}")
        check(failures, "prop.one_command",
              doc.structural_depth == depth0 + 1 and w.undo_stack.canUndo(),
              "expected exactly one undoable structural command")
        w.undo_stack.undo()
        check(failures, "prop.undo",
              doc.metadata_fields()["title"] == TITLE,
              "window undo did not restore the title")
        w.undo_stack.redo()
        check(failures, "prop.redo",
              doc.metadata_fields()["title"] == "Window Amended Agenda",
              "window redo did not re-apply the title")

        # Dialogs construct offscreen (never exec'd), prefilled.
        dlg = _PropertiesDialog(doc.properties())
        check(failures, "prop.dialog_fields",
              dlg.fields()["title"] == "Window Amended Agenda"
              and dlg.fields()["author"] == AUTHOR,
              f"_PropertiesDialog not prefilled: {dlg.fields()!r}")
        check(failures, "prop.dialog_fonts",
              dlg.fonts_table.rowCount() == len(doc.fonts_used()),
              "fonts table row count mismatch")
        dlg.deleteLater()
        edlg = _ExportImagesDialog(doc.page_count, os.path.dirname(doc.path))
        opts = edlg.options()
        check(failures, "prop.export_dialog",
              opts.fmt == "png" and opts.dpi == 150 and opts.pages == "1-3"
              and bool(opts.out_dir),
              f"_ExportImagesDialog defaults wrong: {opts!r}")
        edlg.deleteLater()

        # Chrome wiring: shortcut, menu homes, enablement.
        check(failures, "prop.shortcut",
              w.act_properties.shortcut().toString() == "Ctrl+D",
              f"Properties shortcut is "
              f"{w.act_properties.shortcut().toString()!r}")
        file_actions = w.menu_file.actions()
        check(failures, "prop.menu_file",
              w.act_properties in file_actions
              and w.menu_export.menuAction() in file_actions,
              "Properties / Export missing from the File menu")
        check(failures, "prop.menu_export",
              w.menu_export.actions() == [w.act_export_images,
                                          w.act_export_text],
              "Export submenu does not hold exactly the two export actions")
        check(failures, "prop.anchor",
              file_actions.index(w.act_properties)
              < file_actions.index(w.menu_anchors["file_output"]),
              "ws6 entries not inserted BEFORE the file_output anchor")
        check(failures, "prop.enabled", w.act_properties.isEnabled()
              and w.act_export_images.isEnabled()
              and w.act_export_text.isEnabled(),
              "doc-tools actions disabled with a document open")
    finally:
        close(w)

    # Empty state: all three disabled, and the handlers no-op without a doc.
    w2 = MainWindow()
    try:
        w2.show()
        pump(2)
        check(failures, "prop.empty_disabled",
              not w2.act_properties.isEnabled()
              and not w2.act_export_images.isEnabled()
              and not w2.act_export_text.isEnabled(),
              "doc-tools actions enabled in the empty state")
        w2._do_properties(apply_meta={"title": "x"})   # must not raise
        w2._do_export_text(os.path.join(tempfile.gettempdir(), "no.txt"))
    finally:
        close(w2)


# ---------------------------------------------------------------------------
# 8. doctools stamp helpers (Qt-free)
# ---------------------------------------------------------------------------
def test_stamp_helpers(failures: list) -> None:
    # 9-grid presets: center == the page center; corner cells sit at the
    # margin-inset grid's cell centers; unknown names raise.
    check(failures, "helpers.center",
          doctools.preset_point(612, 792, "center") == (306.0, 396.0),
          f"center preset wrong: {doctools.preset_point(612, 792, 'center')}")
    check(failures, "helpers.top_left",
          doctools.preset_point(612, 792, "top-left") == (126.0, 156.0),
          f"top-left preset wrong: "
          f"{doctools.preset_point(612, 792, 'top-left')}")
    check(failures, "helpers.bottom_right",
          doctools.preset_point(612, 792, "bottom-right") == (486.0, 636.0),
          f"bottom-right preset wrong: "
          f"{doctools.preset_point(612, 792, 'bottom-right')}")
    try:
        doctools.preset_point(612, 792, "middle")
        check(failures, "helpers.bad_position", False,
              "unknown position did not raise ValueError")
    except ValueError:
        pass

    # Token substitution: every occurrence of all three tokens; unknown
    # brace text passes through untouched.
    out = doctools.substitute_tokens(
        "p{page}/{pages} {date} (again {page}) {other}",
        page_no=2, total=3, date="2026-06-10")
    check(failures, "helpers.tokens",
          out == "p2/3 2026-06-10 (again 2) {other}",
          f"substitute_tokens wrong: {out!r}")

    # stamp_start: angle 0 centers the run half a width left and voff below.
    sx, sy = doctools.stamp_start((306.0, 396.0), 100.0, 18.0, 0.0)
    check(failures, "helpers.start_flat",
          abs(sx - 256.0) < 1e-9 and abs(sy - 414.0) < 1e-9,
          f"stamp_start(angle=0) wrong: {(sx, sy)}")


# ---------------------------------------------------------------------------
# 9. Watermark ink: one structural op, mixed page sizes, undo to baseline
# ---------------------------------------------------------------------------
def test_watermark_ink(failures: list) -> None:
    doc = PDFDocument(THREE_PAGE)
    try:
        # Per-page center probe regions: page 1 is LANDSCAPE (792x612), so
        # its blank center square sits at the transposed coordinates.
        regions = {0: WM_REGION, 1: (366.0, 276.0, 426.0, 336.0),
                   2: WM_REGION}
        baseline = {i: region_ink(doc.working, r, page=i)
                    for i, r in regions.items()}
        for i, ink in baseline.items():
            check(failures, "wm.blank_center", ink < 50,
                  f"page {i} center not blank before stamping ({ink}px)")

        depth0 = doc.structural_depth
        doc.add_watermark([0, 1, 2], text="DRAFT")
        check(failures, "wm.one_op", doc.structural_depth == depth0 + 1,
              f"add_watermark pushed {doc.structural_depth - depth0} "
              f"snapshots, expected 1")
        check(failures, "wm.dirty", doc.dirty,
              "doc not dirty after add_watermark")
        stamped = {i: region_ink(doc.working, r, page=i)
                   for i, r in regions.items()}
        for i, ink in stamped.items():
            check(failures, "wm.ink", ink > 2000,
                  f"page {i} center gained almost no watermark ink ({ink}px)")

        # The stamp is ordinary page content now: spans() re-extracts it.
        check(failures, "wm.editable_content",
              any("DRAFT" in norm(b.text) for b in doc.spans(0)),
              "stamped watermark text not extracted as a page box")

        # undo_structural restores the EXACT pre-op bytes -> baseline ink.
        doc.undo_structural()
        for i, r in regions.items():
            ink = region_ink(doc.working, r, page=i)
            check(failures, "wm.undo", ink == baseline[i],
                  f"undo left page {i} center at {ink}px "
                  f"(baseline {baseline[i]}px)")
        doc.redo_structural()
        ink = region_ink(doc.working, WM_REGION)
        check(failures, "wm.redo", ink == stamped[0],
              f"redo did not restore the watermark ({ink}px)")
        doc.undo_structural()

        # Guard validations raise BEFORE any snapshot is taken.
        depth1 = doc.structural_depth
        for tag, call in (
            ("empty_text", lambda: doc.add_watermark([0], text="   ")),
            ("bad_position", lambda: doc.add_watermark(
                [0], text="X", position="middle")),
            ("bad_opacity", lambda: doc.add_watermark(
                [0], text="X", opacity=0.0)),
            ("no_pages", lambda: doc.add_watermark([], text="X")),
        ):
            try:
                call()
                check(failures, f"wm.guard_{tag}", False,
                      f"{tag} did not raise ValueError")
            except ValueError:
                pass
        try:
            doc.add_watermark([9], text="X")
            check(failures, "wm.guard_bad_page", False,
                  "out-of-range page did not raise IndexError")
        except IndexError:
            pass
        check(failures, "wm.guard_no_snapshot",
              doc.structural_depth == depth1,
              "a rejected add_watermark still pushed a snapshot")
    finally:
        doc.close()


# ---------------------------------------------------------------------------
# 10. Opacity ordering + behind=True visibility
# ---------------------------------------------------------------------------
def test_watermark_opacity_behind(failures: list) -> None:
    doc = PDFDocument(THREE_PAGE)
    try:
        doc.add_watermark([0], text="DRAFT", opacity=0.25)
        lum_light = region_lum(doc.working, WM_REGION)
        doc.undo_structural()
        doc.add_watermark([0], text="DRAFT", opacity=1.0)
        lum_full = region_lum(doc.working, WM_REGION)
        doc.undo_structural()
        check(failures, "wm.opacity", lum_light > lum_full + 10,
              f"0.25 opacity not measurably lighter: "
              f"{lum_light:.1f} vs {lum_full:.1f}")

        # behind=True (overlay=False) inserts BELOW existing content; on a
        # blank region nothing covers it, so it stays visible.
        doc.add_watermark([0], text="DRAFT", behind=True)
        ink = region_ink(doc.working, WM_REGION)
        check(failures, "wm.behind_visible", ink > 2000,
              f"behind=True watermark invisible on a blank region ({ink}px)")
    finally:
        doc.close()


# ---------------------------------------------------------------------------
# 11. Header/footer page-number tokens + band geometry
# ---------------------------------------------------------------------------
def test_header_footer_tokens(failures: list) -> None:
    doc = PDFDocument(THREE_PAGE)
    try:
        header_band = (246.0, 14.0, 366.0, 42.0)     # centered, baseline 30
        footer_band = (30.0, 758.0, 170.0, 788.0)    # left, baseline 774
        # The fixture's navy band covers the header region, so compare PIXELS
        # before/after (an ink COUNT there is saturated already).
        base_header = _clip_array(doc.working, header_band, 0, 3.0)
        base_footer = _clip_array(doc.working, footer_band, 0, 3.0)

        depth0 = doc.structural_depth
        doc.add_header_footer(
            [0, 1, 2],
            slots={"header_center": "Page {page} of {pages}",
                   "footer_left": "Generated {date}"})
        check(failures, "hf.one_op", doc.structural_depth == depth0 + 1,
              f"add_header_footer pushed {doc.structural_depth - depth0} "
              f"snapshots, expected 1")
        # {page} counts start_at + k, {pages} is the document total.
        for i, expect in enumerate(
                ("Page 1 of 3", "Page 2 of 3", "Page 3 of 3")):
            text = norm(doc.working[i].get_text())
            check(failures, "hf.token", expect in text,
                  f"page {i + 1} text missing {expect!r}")
        today = datetime.date.today().strftime("%Y-%m-%d")
        check(failures, "hf.date",
              f"Generated {today}" in norm(doc.working[0].get_text()),
              "footer {date} token did not render today's date")
        header_diff = int((_clip_array(doc.working, header_band, 0, 3.0)
                           != base_header).any(axis=2).sum())
        footer_diff = int((_clip_array(doc.working, footer_band, 0, 3.0)
                           != base_footer).any(axis=2).sum())
        check(failures, "hf.header_ink", header_diff > 50,
              f"header band unchanged at the configured margins "
              f"({header_diff}px)")
        check(failures, "hf.footer_ink", footer_diff > 50,
              f"footer band unchanged at the configured margins "
              f"({footer_diff}px)")

        doc.undo_structural()
        check(failures, "hf.undo",
              "Page 2 of 3" not in norm(doc.working[1].get_text()),
              "undo_structural did not remove the header")

        # start_at offsets {page} (the dialog's start-numbering spin).
        doc.add_header_footer([0], slots={"header_center": "Sheet {page}"},
                              start_at=5)
        check(failures, "hf.start_at",
              "Sheet 5" in norm(doc.working[0].get_text()),
              "start_at=5 did not offset the {page} token")
        doc.undo_structural()

        # Guards raise before the snapshot.
        depth1 = doc.structural_depth
        for tag, call in (
            ("bad_slot", lambda: doc.add_header_footer(
                [0], slots={"bogus": "x"})),
            ("all_empty", lambda: doc.add_header_footer(
                [0], slots={"header_left": "  ", "footer_right": ""})),
        ):
            try:
                call()
                check(failures, f"hf.guard_{tag}", False,
                      f"{tag} did not raise ValueError")
            except ValueError:
                pass
        check(failures, "hf.guard_no_snapshot",
              doc.structural_depth == depth1,
              "a rejected add_header_footer still pushed a snapshot")
    finally:
        doc.close()


# ---------------------------------------------------------------------------
# 12. /Rotate 90 page gets an UPRIGHT header in the displayed band
# ---------------------------------------------------------------------------
def test_header_rotated_upright(failures: list) -> None:
    doc = PDFDocument(ROTATED)
    try:
        check(failures, "rot.fixture", doc.page_rotation(0) == 90
              and doc.working[0].rect.width == 792.0,
              "rotated_doc.pdf is not the expected /Rotate 90 letter page")
        pix = doc.render(0, 2.0)
        before = np.frombuffer(bytes(pix.samples), dtype=np.uint8).reshape(
            pix.height, pix.width, pix.n)

        doc.add_header_footer([0],
                              slots={"header_center": "Confidential Review"})
        pix2 = doc.render(0, 2.0)
        after = np.frombuffer(bytes(pix2.samples), dtype=np.uint8).reshape(
            pix2.height, pix2.width, pix2.n)
        check(failures, "rot.text",
              "Confidential Review" in norm(doc.working[0].get_text()),
              "rotated header text missing from get_text")

        # The new ink (diff vs the pre-stamp render: robust against the plum
        # band the fixture draws along the displayed top) must read
        # HORIZONTALLY in the displayed band: wider than tall, near the top,
        # horizontally centered. A recipe that forgot derotation/morph would
        # paint a vertical run down the side instead.
        changed = (after != before).any(axis=2)
        ys, xs = np.nonzero(changed)
        if not check(failures, "rot.changed", len(xs) > 50,
                     "rotated header changed almost no pixels"):
            return
        w = int(xs.max() - xs.min())
        h = int(ys.max() - ys.min())
        check(failures, "rot.upright", w > 2 * h,
              f"rotated header ink box {w}x{h}px is not wider than tall")
        check(failures, "rot.top_band", ys.max() < 90,
              f"rotated header ink not in the displayed top band "
              f"(ys.max()={int(ys.max())}px at 2x)")
        cx = (xs.max() + xs.min()) / 2.0
        check(failures, "rot.centered", abs(cx - 792.0) < 50,
              f"rotated header not centered (cx={cx:.0f}px, want ~792)")
    finally:
        doc.close()


# ---------------------------------------------------------------------------
# 13. Stamp x edit coexistence (the neighbor-rescue hazard, WYSIWYG)
# ---------------------------------------------------------------------------
def test_stamp_edit_coexist(failures: list) -> None:
    td = tempfile.mkdtemp(prefix="doctools_stampedit_")
    doc = PDFDocument(THREE_PAGE)
    try:
        # A page-diagonal watermark whose strip CROSSES the body paragraph:
        # its bbox dwarfs the paragraph's redact rects, so the overlap
        # fraction lands in the neighbor heuristic's redraw band (0.02..0.5)
        # and the bake must re-draw it through the redaction (the documented
        # add_watermark hazard) rather than truncating it.
        doc.add_watermark([0], text="CONFIDENTIAL DRAFT", fontsize=80.0,
                          angle=-45.0)
        # The stamp must extract as its OWN box (the _merge_overlapping
        # direction gate): its axis-aligned bbox blankets the page, and
        # merging it into the horizontal boxes would make editing one line
        # silently redact everything under that bbox.
        boxes = doc.spans(0)
        wm_box = next((b for b in boxes
                       if "CONFIDENTIAL DRAFT" in norm(b.text)), None)
        check(failures, "coexist.wm_own_box",
              wm_box is not None and len(wm_box.redact_rects) == 1,
              "diagonal watermark did not extract as its own box")
        para = next((b for b in boxes
                     if "Tall windows" in norm(b.text)), None)
        if not check(failures, "coexist.para", para is not None,
                     "body paragraph not found on the stamped page"):
            return
        check(failures, "coexist.para_separate",
              "CONFIDENTIAL" not in norm(para.text)
              and max(r[3] for r in para.redact_rects) < 300,
              "watermark merged into the body paragraph's box")
        # A strip-only probe region (below the paragraph, inside the
        # descending diagonal): proves the watermark band itself survives.
        strip = (80.0, 250.0, 200.0, 330.0)
        check(failures, "coexist.strip_before",
              region_ink(doc.working, strip) > 2000,
              "watermark strip region empty right after stamping")

        doc.stage_edit(0, para, norm(para.text).replace(
            "morning", "evening"))
        out = os.path.join(td, "coexist.pdf")
        doc.save_as(out)
        saved_text = ""
        sd = fitz.open(out)
        try:
            saved_text = norm(sd[0].get_text())
        finally:
            sd.close()
        check(failures, "coexist.edit_applied",
              "evening" in saved_text and "morning" not in saved_text,
              "staged edit missing from the stamped page's save")
        check(failures, "coexist.wm_text_intact",
              "CONFIDENTIAL DRAFT" in saved_text,
              "watermark text lost through the edit bake")
        check(failures, "coexist.page_intact",
              "Harbor Library" in saved_text
              and "sheet 1 of 3" in saved_text,
              "unrelated page text lost through the edit bake")
        check(failures, "coexist.wm_ink_intact",
              region_ink(out, strip) > 2000,
              "watermark strip ink lost through the edit bake")
        rel = wysiwyg_rel(doc, out)
        check(failures, "coexist.wysiwyg", rel < 0.02,
              f"render vs saved ink rel diff {rel:.4f} >= 0.02")
    finally:
        doc.close()


# ---------------------------------------------------------------------------
# 14. No residue: move a box on a stamped page (test_editor move pattern)
# ---------------------------------------------------------------------------
def test_stamp_no_residue(failures: list) -> None:
    td = tempfile.mkdtemp(prefix="doctools_residue_")
    doc = PDFDocument(THREE_PAGE)
    try:
        doc.add_watermark([0], text="DRAFT")   # default 48pt/45deg center
        footer = next((b for b in doc.spans(0)
                       if "sheet 1 of 3" in norm(b.text)), None)
        if not check(failures, "residue.footer", footer is not None,
                     "footer box not found on the stamped page"):
            return
        doc.move_box(0, footer, 40.0, -16.0)
        out = os.path.join(td, "moved.pdf")
        doc.save_as(out)

        # No residue at ANY original member bbox (the watermark's diagonal
        # stays clear of the footer, so the floor is strict).
        residue = max(region_ink(out, bb) for bb in footer.redact_rects)
        check(failures, "residue.clean", residue < 80,
              f"moved box left residue at its original spot ({residue}px)")
        sd = fitz.open(out)
        try:
            check(failures, "residue.moved_text",
                  "sheet 1 of 3" in norm(sd[0].get_text()),
                  "moved footer text missing from the save")
        finally:
            sd.close()
        check(failures, "residue.wm_intact",
              region_ink(out, WM_REGION) > 2000,
              "watermark ink lost through the move bake")
        rel = wysiwyg_rel(doc, out)
        check(failures, "residue.wysiwyg", rel < 0.02,
              f"render vs saved ink rel diff {rel:.4f} >= 0.02")
    finally:
        doc.close()


# ---------------------------------------------------------------------------
# 15. Stamp window flow: seams, menu anchors, dialogs, empty state
# ---------------------------------------------------------------------------
def test_stamp_window_flow(failures: list) -> None:
    w = open_window(THREE_PAGE)
    try:
        doc = w.document
        baseline = region_ink(doc.working, WM_REGION)
        depth0 = doc.structural_depth

        # Watermark through the seam: ONE undoable structural command.
        w._do_watermark(WatermarkOptions(pages="1-3"))
        pump(4)
        check(failures, "flow.wm_one_command",
              doc.structural_depth == depth0 + 1 and w.undo_stack.canUndo(),
              "watermark flow did not push exactly one structural command")
        check(failures, "flow.wm_ink",
              region_ink(doc.working, WM_REGION) > 2000,
              "watermark flow left no ink at page center")
        check(failures, "flow.wm_toast",
              w.filename_label.text() == "Watermark added to 3 pages",
              f"watermark toast wrong: {w.filename_label.text()!r}")
        w.undo_stack.undo()
        pump(4)
        check(failures, "flow.wm_undo",
              region_ink(doc.working, WM_REGION) == baseline,
              "window undo did not restore the pre-watermark page")

        # Header/footer through the seam: tokens land via the model.
        w._do_header_footer(HeaderFooterOptions(
            slots={"header_center": "Page {page} of {pages}"}, pages="1-3"))
        pump(4)
        check(failures, "flow.hf_token",
              "Page 2 of 3" in norm(doc.working[1].get_text()),
              "header/footer flow did not stamp the page-2 token")
        w.undo_stack.undo()
        pump(4)

        # Bad input flashes, never raises, pushes nothing.
        depth1 = doc.structural_depth
        w._do_watermark(WatermarkOptions(pages="9"))
        w._do_watermark(WatermarkOptions(text="   ", pages="1"))
        w._do_header_footer(HeaderFooterOptions(
            slots={"header_center": "  "}, pages="1"))
        check(failures, "flow.bad_input",
              doc.structural_depth == depth1,
              "bad stamp input still pushed a structural command")

        # Menu placement: both entries live in the Document menu, after the
        # doc_transform anchor and before the doc_decorate anchor.
        acts = w.menu_document.actions()
        check(failures, "flow.menu",
              w.act_watermark in acts and w.act_header_footer in acts,
              "stamp actions missing from the Document menu")
        check(failures, "flow.anchor",
              acts.index(w.menu_anchors["doc_transform"])
              < acts.index(w.act_watermark)
              < acts.index(w.act_header_footer)
              < acts.index(w.menu_anchors["doc_decorate"]),
              "stamp entries not at the doc_decorate anchor slot")
        check(failures, "flow.enabled", w.act_watermark.isEnabled()
              and w.act_header_footer.isEnabled(),
              "stamp actions disabled with a document open")

        # Dialogs construct offscreen (never exec'd) with the spec defaults.
        wd = _WatermarkDialog(doc.page_count)
        opts = wd.options()
        check(failures, "flow.wm_dialog",
              opts.text == "DRAFT" and opts.base14_code == "helv"
              and opts.fontsize == 48.0 and opts.opacity == 0.3
              and opts.angle == 45.0 and opts.position == "center"
              and not opts.behind and opts.pages == "1-3"
              and max(abs(a - b) for a, b in
                      zip(opts.color, (0.8, 0.1, 0.1))) < 0.01,
              f"_WatermarkDialog defaults wrong: {opts!r}")
        wd.deleteLater()
        hd = _HeaderFooterDialog(doc.page_count)
        hd._insert_token("{page}")     # default target: header_center
        hd.slot_edits["header_center"].setText("Page {page} of {pages}")
        hopts = hd.options()
        check(failures, "flow.hf_dialog",
              hopts.fontsize == 9.0 and hopts.start_at == 1
              and hopts.top == 30.0 and hopts.bottom == 18.0
              and hopts.side == 36.0 and hopts.pages == "1-3"
              and hopts.slots["header_center"] == "Page {page} of {pages}"
              and hopts.slots["footer_left"] == "",
              f"_HeaderFooterDialog options wrong: {hopts!r}")
        check(failures, "flow.hf_preview",
              "Page 1 of 3" in hd.preview.text(),
              f"live preview not substituted: {hd.preview.text()!r}")
        hd.deleteLater()
    finally:
        close(w)

    # Empty state: disabled actions, no-op handlers.
    w2 = MainWindow()
    try:
        w2.show()
        pump(2)
        check(failures, "flow.empty_disabled",
              not w2.act_watermark.isEnabled()
              and not w2.act_header_footer.isEnabled(),
              "stamp actions enabled in the empty state")
        w2._do_watermark(WatermarkOptions())             # must not raise
        w2._do_header_footer(HeaderFooterOptions())      # must not raise
    finally:
        close(w2)


# ---------------------------------------------------------------------------
# 16. Crop model: coordinate shift, composition, edit survival, guards
# ---------------------------------------------------------------------------
def test_crop_model(failures: list) -> None:
    td = tempfile.mkdtemp(prefix="doctools_crop_")
    doc = PDFDocument(DOC_TOOLS)
    try:
        check(failures, "crop.fixture",
              doc.working[0].rect == fitz.Rect(0, 0, 612, 792),
              f"unexpected fixture page rect: {doc.working[0].rect}")
        span = find_span(doc, 0, "revenue")
        if not check(failures, "crop.span", span is not None,
                     "no 'revenue' span on page 0 of doc_tools.pdf"):
            return
        bbox0 = tuple(span.bbox)

        depth0 = doc.structural_depth
        skipped = doc.crop_pages([0], (50.0, 60.0, 450.0, 700.0))
        check(failures, "crop.no_skip", skipped == [],
              f"in-bounds crop skipped pages: {skipped}")
        check(failures, "crop.one_op", doc.structural_depth == depth0 + 1,
              f"crop_pages pushed {doc.structural_depth - depth0} "
              f"snapshots, expected 1")
        check(failures, "crop.dirty", doc.dirty,
              "doc not dirty after crop_pages")
        check(failures, "crop.rect",
              doc.working[0].rect == fitz.Rect(0, 0, 400, 640),
              f"cropped page rect wrong: {doc.working[0].rect}")
        cb = doc.working[0].cropbox
        check(failures, "crop.cropbox",
              (cb.x0, cb.y0) == (50.0, 60.0),
              f"cropbox.tl wrong: ({cb.x0}, {cb.y0})")
        check(failures, "crop.other_pages",
              doc.working[1].rect == fitz.Rect(0, 0, 612, 792),
              "single-page crop touched another page")

        # spans() re-extract shifted by EXACTLY cropbox.tl (the §2.6 probe:
        # text at x=72 reads x=22 after a crop at x0=50).
        span2 = find_span(doc, 0, "revenue")
        if not check(failures, "crop.respan", span2 is not None,
                     "'revenue' span lost after the crop"):
            return
        # Exact to extraction precision: rawdict coords are float32, so the
        # shift reads back within ~1e-5 pt of the cropbox.tl delta.
        deltas = tuple(a - b for a, b in zip(bbox0, span2.bbox))
        check(failures, "crop.shift",
              max(abs(d - e) for d, e in
                  zip(deltas, (50.0, 60.0, 50.0, 60.0))) < 1e-4,
              f"span coords not shifted by exactly cropbox.tl: {deltas}")

        # Cropping the already-cropped page composes: the rect is in the NEW
        # text space, so the mediabox offsets accumulate (+ cb.tl per §2.6).
        doc.crop_pages([0], (10.0, 10.0, 350.0, 500.0))
        cb2 = doc.working[0].cropbox
        check(failures, "crop.compose",
              (cb2.x0, cb2.y0) == (60.0, 70.0)
              and doc.working[0].rect == fitz.Rect(0, 0, 340, 490),
              f"second crop did not compose: tl=({cb2.x0}, {cb2.y0}) "
              f"rect={doc.working[0].rect}")

        # undo_structural walks back through both crops to pristine.
        doc.undo_structural()
        check(failures, "crop.undo_one",
              doc.working[0].rect == fitz.Rect(0, 0, 400, 640),
              f"first undo wrong: {doc.working[0].rect}")
        doc.undo_structural()
        check(failures, "crop.undo_pristine",
              doc.working[0].rect == fitz.Rect(0, 0, 612, 792),
              f"second undo did not restore the rect: {doc.working[0].rect}")
        span3 = find_span(doc, 0, "revenue")
        check(failures, "crop.undo_coords",
              span3 is not None
              and max(abs(a - b) for a, b in zip(bbox0, span3.bbox)) < 1e-4,
              "undo did not restore the original span coords")

        # Guards raise BEFORE any snapshot (no phantom undo entry).
        depth1 = doc.structural_depth
        for tag, call in (
            ("small", lambda: doc.crop_pages([0], (100.0, 100.0,
                                                   120.0, 500.0))),
            ("empty_rect", lambda: doc.crop_pages([0], (100.0, 100.0,
                                                        100.0, 100.0))),
            ("no_pages", lambda: doc.crop_pages([], (0.0, 0.0,
                                                     200.0, 200.0))),
        ):
            try:
                call()
                check(failures, f"crop.guard_{tag}", False,
                      f"{tag} did not raise ValueError")
            except ValueError:
                pass
        try:
            doc.crop_pages([9], (0.0, 0.0, 200.0, 200.0))
            check(failures, "crop.guard_bad_page", False,
                  "out-of-range page did not raise IndexError")
        except IndexError:
            pass
        check(failures, "crop.guard_no_snapshot",
              doc.structural_depth == depth1,
              "a rejected crop_pages still pushed a snapshot")
    finally:
        doc.close()

    # A staged edit made BEFORE the crop survives in the cropped save: the
    # structural op bakes it in the OLD coordinates before the origin moves.
    doc = PDFDocument(DOC_TOOLS)
    try:
        span = find_span(doc, 0, "revenue")
        doc.stage_edit(0, span, span.text.replace(" ", " ").replace(
            "revenue", "earnings"))
        doc.crop_pages([0], (50.0, 60.0, 450.0, 700.0))
        out = os.path.join(td, "cropped.pdf")
        doc.save_as(out)
        saved = fitz.open(out)
        try:
            check(failures, "crop.saved_rect",
                  saved[0].rect == fitz.Rect(0, 0, 400, 640),
                  f"saved crop rect wrong: {saved[0].rect}")
            text = norm(saved[0].get_text())
            check(failures, "crop.edit_survives",
                  "earnings" in text and "revenue" not in text,
                  "pre-crop staged edit missing from the cropped save")
        finally:
            saved.close()
        rel = wysiwyg_rel(doc, out)
        check(failures, "crop.wysiwyg", rel < 0.02,
              f"render vs saved ink rel diff {rel:.4f} >= 0.02")
    finally:
        doc.close()


# ---------------------------------------------------------------------------
# 17. Crop scope: mixed page sizes clamp per page; skips; rotated page
# ---------------------------------------------------------------------------
def test_crop_scope_clamp(failures: list) -> None:
    doc = PDFDocument(THREE_PAGE)
    try:
        # three_page.pdf: pages 0/2 are 612x792 portrait, page 1 is a TRUE
        # 792x612 landscape mediabox -- the mixed-size clamp case.
        check(failures, "clamp.fixture",
              doc.working[1].rect == fitz.Rect(0, 0, 792, 612),
              f"page 1 is not the expected landscape: {doc.working[1].rect}")
        depth0 = doc.structural_depth
        skipped = doc.crop_pages([0, 1, 2], (40.0, 50.0, 580.0, 760.0))
        check(failures, "clamp.no_skip", skipped == [],
              f"clamped all-pages crop skipped pages: {skipped}")
        check(failures, "clamp.one_op", doc.structural_depth == depth0 + 1,
              "all-pages crop was not ONE structural op")
        check(failures, "clamp.portrait",
              doc.working[0].rect == fitz.Rect(0, 0, 540, 710)
              and doc.working[2].rect == fitz.Rect(0, 0, 540, 710),
              f"portrait pages wrong: {doc.working[0].rect} "
              f"{doc.working[2].rect}")
        # Page 1's shorter mediabox clamps the band's bottom to y=612.
        check(failures, "clamp.landscape",
              doc.working[1].rect == fitz.Rect(0, 0, 540, 562),
              f"landscape page not clamped per page: {doc.working[1].rect}")
        doc.undo_structural()
        check(failures, "clamp.undo",
              doc.working[1].rect == fitz.Rect(0, 0, 792, 612),
              "undo did not restore the all-pages crop")

        # A band BELOW page 1's mediabox skips just that page: the op
        # succeeds on the rest and returns the skipped index.
        skipped = doc.crop_pages([0, 1, 2], (40.0, 620.0, 580.0, 760.0))
        check(failures, "clamp.partial_skip", skipped == [1],
              f"expected page 1 skipped, got {skipped}")
        check(failures, "clamp.partial_applied",
              doc.working[0].rect == fitz.Rect(0, 0, 540, 140)
              and doc.working[1].rect == fitz.Rect(0, 0, 792, 612),
              f"partial-skip crop wrong: {doc.working[0].rect} "
              f"{doc.working[1].rect}")
        doc.undo_structural()

        # Every page skipped raises (BEFORE any snapshot).
        depth1 = doc.structural_depth
        try:
            doc.crop_pages([1], (40.0, 620.0, 580.0, 760.0))
            check(failures, "clamp.all_skipped", False,
                  "an off-page crop did not raise ValueError")
        except ValueError:
            pass
        check(failures, "clamp.no_snapshot",
              doc.structural_depth == depth1,
              "the rejected crop still pushed a snapshot")
    finally:
        doc.close()

    # /Rotate 90 page: the rect is UNROTATED text space (the §0 probe), so
    # the DISPLAYED rect comes out swapped (350x450 crop -> 450x350 shown).
    doc = PDFDocument(ROTATED)
    try:
        doc.crop_pages([0], (50.0, 50.0, 400.0, 500.0))
        check(failures, "clamp.rotated",
              doc.working[0].rotation == 90
              and doc.working[0].rect == fitz.Rect(0, 0, 450, 350),
              f"rotated crop wrong: rot={doc.working[0].rotation} "
              f"rect={doc.working[0].rect}")
        cb = doc.working[0].cropbox
        check(failures, "clamp.rotated_cropbox",
              (cb.x0, cb.y0) == (50.0, 50.0),
              f"rotated cropbox.tl wrong: ({cb.x0}, {cb.y0})")
    finally:
        doc.close()


# ---------------------------------------------------------------------------
# 18. Crop UI flow: mode arming, synthesized drag, seams, undo, chrome
# ---------------------------------------------------------------------------
def _send_drag(view, a: QPointF, b: QPointF) -> None:
    """Dispatch a real left press at scene point ``a``, a move to ``b``, and
    a release at ``b`` to the viewport (the test_reflow QMouseEvent
    pattern), exactly as a mouse drag would arrive."""
    vp = view.viewport()

    def ev(etype, scene_pt, button, buttons):
        vpt = view.mapFromScene(scene_pt)
        return QMouseEvent(etype, QPointF(vpt),
                           QPointF(vp.mapToGlobal(vpt)),
                           button, buttons, Qt.NoModifier)

    _APP.sendEvent(vp, ev(QEvent.MouseButtonPress, a,
                          Qt.LeftButton, Qt.LeftButton))
    _APP.sendEvent(vp, ev(QEvent.MouseMove, b, Qt.NoButton, Qt.LeftButton))
    _APP.sendEvent(vp, ev(QEvent.MouseButtonRelease, b,
                          Qt.LeftButton, Qt.NoButton))
    pump(3)


def test_crop_ui_flow(failures: list) -> None:
    w = open_window(DOC_TOOLS)
    try:
        view = w.view
        doc = w.document

        # Chrome: a CHECKABLE action at the doc_transform anchor slot.
        acts = w.menu_document.actions()
        check(failures, "ui.menu",
              w.act_crop in acts
              and acts.index(w.act_crop)
              < acts.index(w.menu_anchors["doc_transform"]),
              "act_crop not at the doc_transform anchor slot")
        check(failures, "ui.checkable",
              w.act_crop.isCheckable() and w.act_crop.isEnabled(),
              "act_crop not a checkable, enabled mode action")

        got: list = []
        view.cropRectSelected.connect(lambda p, r: got.append((p, r)))
        seen: list = []
        # Scope provider seam (the §1 dialog-seam rule): record + CANCEL so
        # the modal _CropDialog never constructs in the offscreen run.
        w._crop_scope_provider = \
            lambda p, r, n: (seen.append((p, r)), None)[1]

        # Arm via the action: mode flips, gesture toast, crosshair cursor.
        w.act_crop.trigger()
        pump(2)
        check(failures, "ui.armed", view.current_mode() == "crop",
              f"act_crop did not arm crop mode: {view.current_mode()!r}")
        check(failures, "ui.toast",
              w.filename_label.text()
              == "Drag a rectangle over a page, Esc to cancel",
              f"arm toast wrong: {w.filename_label.text()!r}")
        check(failures, "ui.cursor",
              view.viewport().cursor().shape() == Qt.CrossCursor,
              "crop mode did not set the crosshair cursor")

        # A stray CLICK (no drag) emits nothing -- no 0x0 scope dialog.
        p_click = view._scene_point(200.0, 200.0, 0)
        _send_drag(view, p_click, p_click)
        check(failures, "ui.stray_click", got == [],
              f"a stray click still emitted cropRectSelected: {got}")

        # Synthesized press/drag/release across page 0: the signal carries
        # the TEXT-SPACE rect (viewport-pixel rounding bounds the error).
        target = (100.0, 150.0, 400.0, 500.0)
        _send_drag(view, view._scene_point(target[0], target[1], 0),
                   view._scene_point(target[2], target[3], 0))
        if not check(failures, "ui.emitted", len(got) == 1,
                     f"expected exactly one cropRectSelected, got {got}"):
            return
        page, rect = got[0]
        check(failures, "ui.page", page == 0,
              f"cropRectSelected page {page} != 0")
        err = max(abs(a - b) for a, b in zip(rect, target))
        check(failures, "ui.rect", err < 2.0,
              f"emitted rect off by {err:.2f}pt: {rect}")
        check(failures, "ui.provider", seen == got,
              "the scope provider did not receive the drawn rect")
        check(failures, "ui.cancel_keeps_armed",
              view.current_mode() == "crop"
              and doc.structural_depth == 0,
              "provider cancel must keep crop mode armed and mutate nothing")

        # Esc disarms back to SELECT and unchecks the action (lockstep).
        _APP.sendEvent(view, QKeyEvent(QEvent.KeyPress, Qt.Key_Escape,
                                       Qt.NoModifier))
        pump(2)
        check(failures, "ui.esc",
              view.current_mode() == "select" and not w.act_crop.isChecked(),
              f"Esc did not disarm: mode={view.current_mode()!r} "
              f"checked={w.act_crop.isChecked()}")

        # Apply path (dialog bypassed): ONE undoable structural command,
        # full reload -- the layer geometry tracks the new page.rect (the
        # materialize refinement quantizes to render pixels, hence the
        # tolerance).
        depth0 = doc.structural_depth
        w._do_crop_apply(0, (100.0, 150.0, 400.0, 500.0), "page")
        pump(4)
        check(failures, "ui.apply",
              doc.structural_depth == depth0 + 1 and w.undo_stack.canUndo(),
              "crop apply did not push exactly one structural command")
        check(failures, "ui.apply_rect",
              doc.working[0].rect == fitz.Rect(0, 0, 300, 350),
              f"applied crop rect wrong: {doc.working[0].rect}")
        pt = view._layers[0].pt_size
        check(failures, "ui.layer_pt_size",
              abs(pt[0] - 300.0) < 2.0 and abs(pt[1] - 350.0) < 2.0,
              f"layer pt_size {pt} does not match the cropped page")
        check(failures, "ui.apply_toast",
              w.filename_label.text() == "Cropped 1 page",
              f"apply toast wrong: {w.filename_label.text()!r}")

        # Window undo restores the page AND the layer geometry.
        w.undo_stack.undo()
        pump(4)
        check(failures, "ui.undo",
              doc.working[0].rect == fitz.Rect(0, 0, 612, 792),
              f"window undo did not restore the rect: {doc.working[0].rect}")
        pt = view._layers[0].pt_size
        check(failures, "ui.undo_pt_size",
              abs(pt[0] - 612.0) < 2.0 and abs(pt[1] - 792.0) < 2.0,
              f"layer pt_size {pt} not restored by undo")

        # All-pages scope crops every page of the equal-size fixture.
        w._do_crop_apply(0, (100.0, 150.0, 400.0, 500.0), "all")
        pump(4)
        check(failures, "ui.scope_all",
              all(doc.working[i].rect == fitz.Rect(0, 0, 300, 350)
                  for i in range(3)),
              "all-pages scope did not crop every page")
        check(failures, "ui.scope_all_toast",
              w.filename_label.text() == "Cropped 3 pages",
              f"all-pages toast wrong: {w.filename_label.text()!r}")
        w.undo_stack.undo()
        pump(4)

        # Bad input flashes (model guard), never raises, pushes nothing.
        depth1 = doc.structural_depth
        w._do_crop_apply(0, (0.0, 0.0, 10.0, 10.0), "page")
        check(failures, "ui.bad_rect", doc.structural_depth == depth1,
              "a sub-36pt crop still pushed a structural command")

        # The dialog constructs offscreen (never exec'd): defaults + scope.
        dlg = _CropDialog(0, (100.0, 150.0, 400.0, 500.0), 3)
        check(failures, "ui.dialog_default", dlg.scope() == "page",
              f"_CropDialog default scope {dlg.scope()!r} != 'page'")
        dlg.rb_all.setChecked(True)
        check(failures, "ui.dialog_all", dlg.scope() == "all",
              "the All-pages radio did not flip the scope")
        dlg.deleteLater()

        # Arming another tool disarms crop through the view (lockstep).
        w.act_crop.setChecked(True)
        pump(2)
        w.act_add_text.setChecked(True)
        pump(2)
        check(failures, "ui.tool_swap",
              view.current_mode() == "add_text"
              and not w.act_crop.isChecked(),
              "arming Add Text did not disarm + uncheck crop")
        w.act_add_text.setChecked(False)
        pump(2)
    finally:
        close(w)

    # Empty state: disabled + no-op handlers; checking rolls itself back.
    w2 = MainWindow()
    try:
        w2.show()
        pump(2)
        check(failures, "ui.empty_disabled", not w2.act_crop.isEnabled(),
              "act_crop enabled in the empty state")
        w2._do_crop_apply(0, (0.0, 0.0, 100.0, 100.0), "page")  # no doc
        w2.act_crop.setChecked(True)
        pump(2)
        check(failures, "ui.empty_uncheck", not w2.act_crop.isChecked(),
              "checking crop with no document did not roll back")
    finally:
        close(w2)


# ---------------------------------------------------------------------------
# 19. Password gate (model): runtime AES-256 fixture, save round trips
# ---------------------------------------------------------------------------
PASSWORD = "fixture-pass"


def make_encrypted(td: str, name: str = "locked.pdf",
                   pw: str = PASSWORD) -> str:
    """An AES-256 copy of doc_tools.pdf generated AT RUNTIME into ``td`` --
    the M4 fixture rule: encrypted fixtures are never committed."""
    path = os.path.join(td, name)
    src = fitz.open(DOC_TOOLS)
    try:
        src.save(path, encryption=fitz.PDF_ENCRYPT_AES_256,
                 user_pw=pw, owner_pw=pw)
    finally:
        src.close()
    return path


def test_password_gate(failures: list) -> None:
    td = tempfile.mkdtemp(prefix="doctools_pw_")
    enc = make_encrypted(td)
    check(failures, "pw.fitz_fixture", fitz.open(enc).needs_pass == 1,
          "the runtime fixture is not actually encrypted")

    # PasswordRequired subclasses ValueError (existing open-failure handling
    # keeps working anywhere the loop is not threaded through).
    check(failures, "pw.is_valueerror",
          issubclass(PasswordRequired, ValueError),
          "PasswordRequired is not a ValueError")
    for tag, args in (("none", (enc,)), ("wrong", (enc, "wrong-pass"))):
        try:
            PDFDocument(*args)
            check(failures, f"pw.gate_{tag}", False,
                  f"{tag}-password open did not raise PasswordRequired")
        except PasswordRequired:
            pass

    doc = PDFDocument(enc, PASSWORD)
    try:
        check(failures, "pw.opened",
              doc.original_encrypted and doc.page_count == 3,
              f"encrypted open wrong: enc={doc.original_encrypted} "
              f"pages={doc.page_count}")
        check(failures, "pw.meta_carried",
              doc.metadata_fields()["title"] == TITLE,
              f"metadata lost through the gate: {doc.metadata_fields()!r}")
        check(failures, "pw.props_encrypted",
              doc.properties()["encrypted"] is True,
              "properties() does not report the encryption")
        # Protection survives Save by default: AES-256 + the open password.
        check(failures, "pw.default_security",
              doc.encrypts_on_save and doc.reopen_password == PASSWORD,
              f"default security wrong: encrypts={doc.encrypts_on_save} "
              f"reopen={doc.reopen_password!r}")
        check(failures, "pw.clean_open", not doc.dirty,
              "freshly opened encrypted doc reads dirty")

        # Spans extract off the decrypted working copy; edit + save_as.
        span = find_span(doc, 0, "revenue")
        if not check(failures, "pw.span", span is not None,
                     "no 'revenue' span through the decrypted copy"):
            return
        doc.stage_edit(0, span, span.text.replace(" ", " ").replace(
            "revenue", "earnings"))
        out = os.path.join(td, "resaved.pdf")
        doc.save_as(out)
        rink = pix_ink(doc.render_with_edits(0, 2.0))
        saved = fitz.open(out)
        try:
            check(failures, "pw.saved_encrypted", saved.needs_pass == 1,
                  "save_as of an encrypted doc wrote a plain file")
            check(failures, "pw.saved_auth",
                  saved.authenticate(PASSWORD) != 0,
                  "the saved file rejects the open password")
            text = norm(saved[0].get_text())
            check(failures, "pw.saved_edit",
                  "earnings" in text and "revenue" not in text,
                  "staged edit missing from the encrypted save")
            sink = pix_ink(saved[0].get_pixmap(matrix=fitz.Matrix(2, 2),
                                               alpha=False))
            rel = abs(rink - sink) / max(sink, 1)
            check(failures, "pw.wysiwyg", rel < 0.02,
                  f"render vs encrypted save rel ink diff {rel:.4f} >= 0.02")
        finally:
            saved.close()
    finally:
        doc.close()

    # set_security: a pending SAVE OPTION -- dirty flips with NO undo entry,
    # mark_clean clears it, the next save applies it.
    doc = PDFDocument(enc, PASSWORD)
    try:
        check(failures, "pw.sec_clean", not doc.dirty,
              "fresh doc dirty before set_security")
        doc.set_security(encryption=PDF_ENCRYPT_NONE,
                         user_pw=None, owner_pw=None)
        check(failures, "pw.sec_dirty", doc.dirty,
              "set_security did not flip dirty")
        check(failures, "pw.sec_no_undo",
              doc.structural_depth == 0 and not doc.can_undo_structural,
              "set_security pushed a structural snapshot")
        check(failures, "pw.sec_pending",
              not doc.encrypts_on_save and doc.reopen_password is None,
              "removal not reflected by encrypts_on_save/reopen_password")
        out2 = os.path.join(td, "unlocked.pdf")
        doc.save_as(out2)
        doc.mark_clean()
        check(failures, "pw.sec_mark_clean", not doc.dirty,
              "mark_clean did not clear the security-dirty flag")
        saved = fitz.open(out2)
        try:
            check(failures, "pw.sec_removed", saved.needs_pass == 0,
                  "encryption=NONE still wrote an encrypted file")
            check(failures, "pw.sec_text",
                  "revenue" in norm(saved[0].get_text()),
                  "page text lost through the decrypting save")
        finally:
            saved.close()

        # Unknown keys raise BEFORE any state changes.
        try:
            doc.set_security(bogus=1)
            check(failures, "pw.sec_guard", False,
                  "an unknown security key did not raise ValueError")
        except ValueError:
            pass
        check(failures, "pw.sec_guard_clean",
              not doc.dirty and "bogus" not in doc._security,
              "the rejected set_security still changed state")
    finally:
        doc.close()


# ---------------------------------------------------------------------------
# 20. Security window flow: provider loop, _do_security, reload regression
# ---------------------------------------------------------------------------
def test_security_window_flow(failures: list) -> None:
    td = tempfile.mkdtemp(prefix="doctools_sec_")
    enc_a = make_encrypted(td, "locked_a.pdf")
    enc_b = make_encrypted(td, "locked_b.pdf")
    enc_c = make_encrypted(td, "locked_c.pdf")

    w = MainWindow()
    w.resize(1300, 950)
    w.show()
    pump(2)
    try:
        # Right password on the first prompt opens the tab.
        prompts: list = []
        w._password_provider = \
            lambda p, a: (prompts.append((os.path.basename(p), a)),
                          PASSWORD)[1]
        w.open_path(enc_a)
        pump(6)
        check(failures, "sec.open",
              w.document is not None and w.document.path == enc_a,
              "injected provider did not open the encrypted tab")
        check(failures, "sec.one_prompt",
              prompts == [("locked_a.pdf", 1)],
              f"unexpected prompt sequence: {prompts}")

        # Wrong then right: the second answer is re-tried and succeeds.
        calls: list = []
        answers = ["wrong-pass", PASSWORD]
        w._password_provider = \
            lambda p, a: (calls.append(a), answers[a - 1])[1]
        w.open_path(enc_b)
        pump(6)
        check(failures, "sec.retry",
              w.document is not None and w.document.path == enc_b
              and calls == [1, 2],
              f"wrong-then-right flow failed: calls={calls}")

        # Cancel (None) aborts silently; nothing opens.
        count0 = w.workspace.count
        cancels: list = []
        w._password_provider = lambda p, a: (cancels.append(a), None)[1]
        w.open_path(enc_c)
        pump(2)
        check(failures, "sec.cancel",
              w.workspace.count == count0 and cancels == [1],
              f"cancel did not abort silently: count={w.workspace.count} "
              f"calls={cancels}")

        # Three wrong answers abort with a flash (each answer re-tried).
        wrongs: list = []
        w._password_provider = lambda p, a: (wrongs.append(a), "nope")[1]
        w.open_path(enc_c)
        pump(2)
        check(failures, "sec.exhausted",
              w.workspace.count == count0 and wrongs == [1, 2, 3],
              f"exhausted flow wrong: count={w.workspace.count} "
              f"calls={wrongs}")
        check(failures, "sec.exhausted_flash",
              "too many incorrect passwords"
              in w.filename_label.text().lower(),
              f"no exhaustion flash: {w.filename_label.text()!r}")

        # _do_security (dialog bypassed): remove, then set a NEW password.
        doc = w.document
        w._do_security(SecurityOptions(action="remove"))
        check(failures, "sec.remove",
              not doc.encrypts_on_save and doc.dirty,
              "remove did not stage encryption=NONE + dirty")
        check(failures, "sec.remove_toast",
              w.filename_label.text() == "Security changes apply on next save",
              f"security toast wrong: {w.filename_label.text()!r}")
        check(failures, "sec.no_undo_command",
              doc.structural_depth == 0 and not w.undo_stack.canUndo(),
              "security staged an undo command (it is a save option)")
        check(failures, "sec.save_enabled", w.act_save.isEnabled(),
              "Save did not enable on a pending security change")
        w._do_security(SecurityOptions(action="set", password="new-pass"))
        check(failures, "sec.set",
              doc.encrypts_on_save and doc.reopen_password == "new-pass",
              f"set did not stage the new password: "
              f"{doc.reopen_password!r}")
        # An empty password flashes and leaves the staged state alone.
        w._do_security(SecurityOptions(action="set", password=""))
        check(failures, "sec.empty_pw",
              doc.reopen_password == "new-pass"
              and "password" in w.filename_label.text().lower(),
              "an empty password was accepted (or no flash)")

        # The _reload_after_save regression: Save As writes an ENCRYPTED
        # file; the reload must thread reopen_password through or the tab
        # silently keeps the stale pre-save model.
        out = os.path.join(td, "resaved_enc.pdf")
        old_doc = doc
        if not check(failures, "sec.saved", w._do_save(out),
                     "saving the encrypted doc failed"):
            return
        w.document.set_path(out)
        w._reload_after_save(out)
        pump(4)
        check(failures, "sec.reload_swapped",
              w.document is not old_doc and w.document.path == out,
              "encrypted Save As did not reload the saved file")
        check(failures, "sec.reload_state",
              w.document.original_encrypted
              and w.document.page_count == 3
              and w.document.reopen_password == "new-pass",
              f"reloaded doc wrong: enc={w.document.original_encrypted} "
              f"pages={w.document.page_count} "
              f"pw={w.document.reopen_password!r}")

        # Chrome: anchor order + the Cmd+P shortcut + enablement.
        facts = w.menu_file.actions()
        check(failures, "sec.file_menu_order",
              facts.index(w.act_optimize)
              < facts.index(w.menu_export.menuAction())
              < facts.index(w.act_print)
              < facts.index(w.act_properties)
              < facts.index(w.menu_anchors["file_output"]),
              "file_output registry order wrong (Save Optimized Copy, "
              "Export, Print, Properties)")
        dacts = w.menu_document.actions()
        check(failures, "sec.doc_menu",
              w.act_security in dacts
              and dacts.index(w.menu_anchors["doc_decorate"])
              < dacts.index(w.act_security)
              < dacts.index(w.menu_anchors["doc_file"]),
              "act_security not at the doc_file anchor slot")
        check(failures, "sec.print_shortcut",
              w.act_print.shortcut().toString() == "Ctrl+P",
              f"Print shortcut is {w.act_print.shortcut().toString()!r}")
        check(failures, "sec.enabled",
              w.act_print.isEnabled() and w.act_optimize.isEnabled()
              and w.act_security.isEnabled(),
              "M4 actions disabled with a document open")

        # The dialog constructs offscreen (never exec'd).
        dlg = _SecurityDialog(False)
        check(failures, "sec.dialog_plain",
              "Not encrypted" in dlg.status_label.text()
              and not dlg.btn_remove.isEnabled()
              and not dlg._ok.isEnabled(),
              "plain-doc dialog state wrong")
        dlg.ed_password.setText("abc")
        dlg.ed_confirm.setText("ab")
        check(failures, "sec.dialog_mismatch", not dlg._ok.isEnabled(),
              "mismatched confirm armed Set Password")
        dlg.ed_confirm.setText("abc")
        check(failures, "sec.dialog_match",
              dlg._ok.isEnabled()
              and dlg.options() == SecurityOptions(action="set",
                                                   password="abc"),
              "matching confirm did not arm Set Password")
        dlg.deleteLater()
        dlg = _SecurityDialog(True)
        check(failures, "sec.dialog_enc",
              "AES-256" in dlg.status_label.text()
              and dlg.btn_remove.isEnabled(),
              "encrypted-doc dialog state wrong")
        dlg.btn_remove.click()
        check(failures, "sec.dialog_remove",
              dlg.result() == QDialog.Accepted
              and dlg.options().action == "remove",
              "Remove Security did not accept with action='remove'")
        dlg.deleteLater()
    finally:
        close(w)

    # Empty state: disabled actions, no-op handlers.
    w2 = MainWindow()
    try:
        w2.show()
        pump(2)
        check(failures, "sec.empty_disabled",
              not w2.act_security.isEnabled()
              and not w2.act_print.isEnabled()
              and not w2.act_optimize.isEnabled(),
              "M4 actions enabled in the empty state")
        w2._do_security(SecurityOptions(action="remove"))   # must not raise
        no = os.path.join(td, "no.pdf")
        w2._do_optimize(no)
        check(failures, "sec.empty_noop", not os.path.exists(no),
              "empty-state optimize still wrote a file")
    finally:
        close(w2)


# ---------------------------------------------------------------------------
# 21. Optimize: measurable delta on the deflate=False fixture, non-mutating
# ---------------------------------------------------------------------------
def test_optimize(failures: list) -> None:
    td = tempfile.mkdtemp(prefix="doctools_opt_")

    # Model level, clean doc: the fixture is saved deflate=False, so the
    # fixed flags have a real delta to find; the page text must not change.
    doc = PDFDocument(DOC_TOOLS)
    try:
        out = os.path.join(td, "optimized.pdf")
        before, after = doc.save_optimized_copy(out)
        check(failures, "opt.sizes",
              before == os.path.getsize(DOC_TOOLS)
              and after == os.path.getsize(out),
              f"reported sizes wrong: {(before, after)}")
        check(failures, "opt.shrinks", 0 < after < before,
              f"optimized copy did not shrink: {before} -> {after}")
        src_texts = [norm(doc.working[i].get_text()) for i in range(3)]
        saved = fitz.open(out)
        try:
            check(failures, "opt.text_identical",
                  [norm(saved[i].get_text()) for i in range(3)] == src_texts,
                  "optimize changed the page text")
        finally:
            saved.close()
        check(failures, "opt.non_mutating",
              not doc.dirty and doc.structural_depth == 0
              and doc.path == DOC_TOOLS,
              "save_optimized_copy mutated the document state")
    finally:
        doc.close()

    # Window flow: staged edits ride the bake; the toast reports the delta;
    # pending security kwargs apply to the optimized copy too.
    w = open_window(DOC_TOOLS)
    try:
        doc = w.document
        span = find_span(doc, 0, "revenue")
        if not check(failures, "opt.span", span is not None,
                     "no 'revenue' span for the optimize-edit check"):
            return
        doc.stage_edit(0, span, span.text.replace(" ", " ").replace(
            "revenue", "earnings"))
        out2 = os.path.join(td, "optimized_edit.pdf")
        w._do_optimize(out2)
        toast = w.filename_label.text()
        check(failures, "opt.toast",
              toast.startswith("Optimized: ") and " -> " in toast
              and toast.endswith("%)"),
              f"optimize toast wrong: {toast!r}")
        saved = fitz.open(out2)
        try:
            text = norm(saved[0].get_text())
            check(failures, "opt.edit_included",
                  "earnings" in text and "revenue" not in text,
                  "staged edit missing from the optimized copy")
        finally:
            saved.close()
        check(failures, "opt.edit_kept",
              doc.edit_count == 1 and doc.dirty,
              "optimize consumed the staged edit (it must stay pending)")

        doc.set_security(encryption=PDF_ENCRYPT_AES_256,
                         user_pw="opt-pass", owner_pw="opt-pass")
        out3 = os.path.join(td, "optimized_enc.pdf")
        w._do_optimize(out3)
        saved = fitz.open(out3)
        try:
            check(failures, "opt.security_applies",
                  saved.needs_pass == 1 and saved.authenticate("opt-pass") != 0,
                  "pending security did not apply to the optimized copy")
        finally:
            saved.close()
    finally:
        close(w)


# ---------------------------------------------------------------------------
# 22. Print: injected PdfFormat QPrinter, page parity, edit diff, range
# ---------------------------------------------------------------------------
def _pdf_printer(path: str, dpi: int = 96) -> QPrinter:
    """A headless PdfFormat QPrinter (the §0 probe seam). The format must be
    set BEFORE the resolution: in native-printer mode setResolution snaps to
    the device's supported DPIs (probe-verified 96 -> 300)."""
    pr = QPrinter()
    pr.setOutputFormat(QPrinter.OutputFormat.PdfFormat)
    pr.setOutputFileName(path)
    pr.setResolution(dpi)
    return pr


def _printed_page(path: str, page: int = 0) -> np.ndarray:
    d = fitz.open(path)
    try:
        pix = d[page].get_pixmap(matrix=fitz.Matrix(1.5, 1.5), alpha=False)
        return np.frombuffer(bytes(pix.samples), dtype=np.uint8).reshape(
            pix.height, pix.width, pix.n).copy()
    finally:
        d.close()


def test_print(failures: list) -> None:
    td = tempfile.mkdtemp(prefix="doctools_print_")
    w = open_window(DOC_TOOLS)
    try:
        doc = w.document
        out_clean = os.path.join(td, "printed_clean.pdf")
        w._do_print(_pdf_printer(out_clean))
        if not check(failures, "print.file", os.path.exists(out_clean),
                     "_do_print wrote nothing"):
            return
        pd = fitz.open(out_clean)
        try:
            check(failures, "print.pages",
                  pd.page_count == doc.page_count == 3,
                  f"printed page count {pd.page_count} != {doc.page_count}")
            inks = [pix_ink(pd[i].get_pixmap(matrix=fitz.Matrix(1, 1),
                                             alpha=False))
                    for i in range(pd.page_count)]
            check(failures, "print.ink", all(i > 1000 for i in inks),
                  f"printed pages have almost no ink: {inks}")
        finally:
            pd.close()
        check(failures, "print.toast",
              w.filename_label.text() == "Sent 3 pages to the printer",
              f"print toast wrong: {w.filename_label.text()!r}")

        # A staged edit changes printed page 1 (render_with_edits parity).
        span = find_span(doc, 0, "revenue")
        if not check(failures, "print.span", span is not None,
                     "no 'revenue' span for the print-edit check"):
            return
        doc.stage_edit(0, span, span.text.replace(" ", " ").replace(
            "revenue", "earnings"))
        out_edit = os.path.join(td, "printed_edit.pdf")
        w._do_print(_pdf_printer(out_edit))
        a = _printed_page(out_clean)
        b = _printed_page(out_edit)
        check(failures, "print.edit_diff",
              a.shape == b.shape and bool((a != b).any()),
              "staged edit did not change the printed page 1")

        # fromPage/toPage narrows the run (0/0 = all, the dialog contract).
        out_range = os.path.join(td, "printed_p2.pdf")
        pr = _pdf_printer(out_range)
        pr.setFromTo(2, 2)
        w._do_print(pr)
        pd = fitz.open(out_range)
        try:
            check(failures, "print.range", pd.page_count == 1,
                  f"fromTo(2,2) printed {pd.page_count} pages")
        finally:
            pd.close()
        check(failures, "print.range_toast",
              w.filename_label.text() == "Sent 1 page to the printer",
              f"range toast wrong: {w.filename_label.text()!r}")
    finally:
        close(w)

    # Empty state: the handler no-ops (no file, no crash).
    w2 = MainWindow()
    try:
        w2.show()
        pump(2)
        out_none = os.path.join(td, "printed_none.pdf")
        w2._do_print(_pdf_printer(out_none))
        check(failures, "print.empty_noop", not os.path.exists(out_none),
              "empty-state print still wrote a file")
    finally:
        close(w2)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> int:
    print("Doc-tools M1-M4 harness (document info & exports + stamps + "
          "crop + security/optimize/print, offscreen)\n")
    failures: list[str] = []
    for fn in (test_metadata_carry, test_toc_carry, test_metadata_editor,
               test_export_images, test_export_text, test_properties_flow,
               test_stamp_helpers, test_watermark_ink,
               test_watermark_opacity_behind, test_header_footer_tokens,
               test_header_rotated_upright, test_stamp_edit_coexist,
               test_stamp_no_residue, test_stamp_window_flow,
               test_crop_model, test_crop_scope_clamp, test_crop_ui_flow,
               test_password_gate, test_security_window_flow,
               test_optimize, test_print):
        name = fn.__name__
        print(f"[{name}]")
        try:
            fn(failures)
        except Exception:
            failures.append(f"{name}: raised:\n{traceback.format_exc()}")
        print()

    if failures:
        print(f"FAILED ({len(failures)} assertion failure(s)):")
        for f in failures:
            print(f"  - {f}")
        return 1

    print("PASSED -- metadata + TOC survive open->edit->save (both carries), "
          "set_metadata_fields is one undoable structural op, image/text "
          "exports ride the render_with_edits bake (WYSIWYG), the Properties "
          "flow applies/undoes through the window stack, the M2 stamps "
          "(watermark + tokenized header/footer) land as single structural "
          "ops with rotated-page uprightness, opacity ordering, edit "
          "coexistence, no residue, and WYSIWYG parity, M3 crop shifts "
          "spans by exactly cropbox.tl (clamped per page, composing, "
          "edit-preserving) with the canvas drag -> cropRectSelected -> "
          "_do_crop_apply flow reloading layer geometry and undoing through "
          "the window stack, and M4 gates encrypted opens behind "
          "PasswordRequired + the injectable provider loop, keeps/sets/"
          "removes AES-256 protection through save_as (incl. the encrypted "
          "Save As reload), shrinks the deflate=False fixture via "
          "save_optimized_copy, and prints page-parity ink through an "
          "injected PdfFormat QPrinter.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
