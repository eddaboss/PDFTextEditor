"""Headless verification for the FORMS build, milestone M1 (ws5_forms §2/§6:
model layer + fixture; no UI).

Covers, against the REAL ``PDFDocument`` model (offscreen Qt only where the
bake pipeline needs the font resolver):

  1. DETECT -- ``has_form`` is True on the synthetic AcroForm fixture
     (``acroform.pdf``, radio group built from raw xref objects) and FALSE on
     ``form_like.pdf`` (the FLAT lookalike regression control: zero widgets,
     ``form_fields`` empty), with ``form_field_count`` reporting the field
     census.
  2. ENUMERATION -- one ``FormField`` per widget with the right kind / name /
     baseline value / options / on-state / rect / flags (multiline, fontsize,
     max_len); radio kids are separate entries sharing one ``group_key``;
     ``form_tab_order`` walks pages in widget order excluding non-fillable
     kinds.
  3. STAGING + UNDO LOCKSTEP -- text / checkbox / radio / combo fills are one
     model command each on the SHARED history; ``edit_count``/``dirty`` track
     the census; undo returns ``(page, FormField)`` and restores the prior
     staged value; redo reapplies; staging the BASELINE back unstages (the
     no-op Edit drop) and a full undo chain reads clean; a non-option combo
     value raises before any state changes.
  4. RENDER == SAVE -- ``render_with_edits`` ink appears inside the staged
     field rects and the whole-page rel ink diff vs the saved file is < 0.02
     (exact in practice: same fresh-copy pipeline); the saved file reopens
     with values AND live widgets (still fillable); the multiline field
     renders its newline value; the fixture bytes on disk are untouched
     before the save (disk isolation).
  5. EDIT + FILL COEXIST -- an in-place text edit on the heading span of the
     SAME page (redact-and-reinsert) and a staged fill both land in one save;
     the widget and its value survive the redaction pass.
  6. STRUCTURAL BAKE -- ``rotate_page`` bakes staged fills into ``working``
     (maps cleared, widget values live in the working doc) and the rotated
     save renders identically to the screen.
  7. FLATTEN -- ``save_flattened`` writes a widget-free copy
     (``is_form_pdf`` False) whose filled value is REAL PAGE TEXT with ink
     parity against the unflattened save, while the open document keeps its
     AcroForm, staged state, and dirty flag (non-mutating export).
  8. RENDER_SIGNATURE REGISTRY -- staged form values fold into the ONE
     cache-key tuple: ``stage_form_value`` misses, undo back to pristine
     returns the exact pre-stage signature, other pages are unaffected.
  9. EXTRACT / MERGE CARRY -- ``extract_pages`` output carries the staged
     fill; merging a form doc into another applies the SOURCE doc's staged
     values to the inserted pages (widgets ride ``insert_pdf``).

Milestone M2 (canvas fill UI + window command + badge, ws5_forms §3/§4),
against the REAL MainWindow + PageView offscreen:

 10. HOTSPOTS / BADGE / TOAST -- ``layer.field_hotspots`` builds one
     FieldHotspot per fillable widget at Z_FORM_HOTSPOT on the AcroForm doc
     and stays EMPTY for ``paragraphs.pdf`` and ``form_like.pdf`` (zero new
     chrome); the status badge shows "Form · N fields" only for the form
     doc (tab switches re-sync it); ``open_path`` toasts the fill hint once
     per open.
 11. CLICK FILLS -- driving ``view._on_field_press`` directly: a checkbox
     press is ONE FormFieldCommand on the window's undo stack with the
     model staged in lockstep; a radio kid picks its on-state and a repeat
     press is a NO-OP (guarded, no phantom Qt command); the combo routes
     through the injectable ``_combo_menu_provider`` seam (no QMenu exec
     offscreen) and a dismissed menu stages nothing; undo/redo walk the
     fills back/forward on BOTH stacks.
 12. TEXT EDITOR -- a text-field press mounts the FormFieldEditor (white
     cover at Z_COVER, seeded from the effective value); commit with no
     change pushes nothing; a real commit is ONE command whose ink lands in
     the field rect of the REPAINTED page pixmap and matches the saved
     file's render pixel-for-pixel at the same scale (WYSIWYG through the
     real repaint path); undo restores the baseline pixels, redo restores
     the fill; Esc cancels without a command; a zoom-triggered
     ``_flush_editor`` commits a half-typed value; a MULTILINE field takes
     Return as a newline and Cmd+Return as the commit. No modal ever runs.

Milestone M3 (tab-order navigation + flatten UI, ws5_forms §3/§4):

 13. TAB NAVIGATION -- REAL Tab/Backtab presses on the view (QTest, so the
     whole delivery chain runs: the editor declines the key via
     ``tabChangesFocus``, the view's ``focusNextPrevChild`` returns False
     on a form doc, and ``form_tab_step`` consumes it) walk every fillable
     widget in ``form_tab_order`` -- text fields auto-mount their editor,
     the page-0 tail crosses to the page-1 field with ``view_page_index()``
     following, the last field WRAPS to the first, and Shift+Tab reverses
     the walk (no phantom commands from the unchanged commits); a mid-edit
     Tab commits the half-typed value as ONE command then focuses the next
     field; ``focus_field`` + Space toggles the focused checkbox, Return
     pops the focused combo's menu through the provider seam, Esc drops
     the focus ring; ``form_tab_step`` declines on a form-free doc.
 14. FLATTEN EXPORT -- ``act_flatten`` sits in the File menu's file_output
     group, enabled only for form docs (the form_like.pdf tab disables it,
     the tab switch back re-enables); ``window._do_export_flattened``
     writes a widget-free copy (``is_form_pdf`` False, the staged value as
     real page text) with field-rect ink parity against the LIVE layer
     pixmap, while the open document keeps its AcroForm, staged state,
     dirty flag, and Qt undo history (non-mutating, no undo entry), and
     the toast lands. No modal ever runs.

Run:
    QT_QPA_PLATFORM=offscreen \
      /Users/edward/Documents/GitHub/PDFTextEditor/.venv/bin/python \
      tests/test_forms.py
"""

from __future__ import annotations

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
from PySide6.QtCore import QRect, Qt  # noqa: E402
from PySide6.QtGui import QImage, QKeyEvent, QTextCursor  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from pdftexteditor.document import FormField, PDFDocument  # noqa: E402

_APP = QApplication.instance() or QApplication(sys.argv)

from pdftexteditor.ui.main_window import (  # noqa: E402
    FormFieldCommand,
    MainWindow,
)
from pdftexteditor.ui.page_view import (  # noqa: E402
    FieldHotspot,
    FormFieldEditor,
    Z_FORM_HOTSPOT,
)

FIXTURES = os.path.join(REPO, "tests", "fixtures")
ACROFORM = os.path.join(FIXTURES, "acroform.pdf")
FORM_LIKE = os.path.join(FIXTURES, "form_like.pdf")
PARAGRAPHS = os.path.join(FIXTURES, "paragraphs.pdf")

# acroform.pdf page width in PDF points (the builder's letter size): the
# layer-image ink helper derives its render scale from it.
PAGE_W_PT = 612.0

# The widget census the builder commits (build_acroform_fixture.py).
EMPLOYEE_RECT = (220.0, 130.0, 480.0, 152.0)
NOTES_RECT = (220.0, 170.0, 480.0, 240.0)
ACK_RECT = (220.0, 262.0, 236.0, 278.0)
DEPT_RECT = (220.0, 300.0, 400.0, 322.0)
DEPARTMENTS = ("People Ops", "Finance", "Engineering")


def check(failures: list, tag: str, cond: bool, msg: str) -> bool:
    if not cond:
        failures.append(f"{tag}: {msg}")
    return cond


def fields_by_key(doc: PDFDocument, page: int = 0) -> dict:
    """Page fields keyed by name (radio kids by ``name/on_state``)."""
    out = {}
    for f in doc.form_fields(page):
        key = f"{f.name}/{f.on_state}" if f.kind == "radio" else f.name
        out[key] = f
    return out


def pix_array(pix) -> np.ndarray:
    return np.frombuffer(bytes(pix.samples), dtype=np.uint8).reshape(
        pix.height, pix.width, pix.n)


def array_ink(arr: np.ndarray) -> int:
    lum = (0.299 * arr[:, :, 0] + 0.587 * arr[:, :, 1]
           + 0.114 * arr[:, :, 2])
    return int((lum < 230).sum())


def pix_ink(pix, rect=None, scale: float = 2.0) -> int:
    """Non-white pixels of a pixmap, optionally restricted to a text-space
    ``rect`` scaled by ``scale`` (the field-rect ink measure)."""
    arr = pix_array(pix)
    if rect is not None:
        x0, y0, x1, y1 = (int(round(v * scale)) for v in rect)
        arr = arr[y0:y1, x0:x1]
    return array_ink(arr)


def saved_pix(path: str, page: int = 0, scale: float = 2.0):
    sd = fitz.open(path)
    try:
        return sd[page].get_pixmap(matrix=fitz.Matrix(scale, scale),
                                   alpha=False)
    finally:
        sd.close()


def saved_widgets(path: str) -> list[tuple]:
    """(page, name, value, on_state) for every widget of a saved file."""
    sd = fitz.open(path)
    try:
        out = []
        for pi in range(sd.page_count):
            for w in sd[pi].widgets():
                out.append((pi, w.field_name, w.field_value, w.on_state()))
        return out
    finally:
        sd.close()


def stage_standard_fills(doc: PDFDocument) -> dict:
    """The four-kind staging block reused across tests; returns the staged
    fields keyed like ``fields_by_key``."""
    fields = fields_by_key(doc)
    doc.stage_form_value(0, fields["employee_name"], "Jordan Carter")
    doc.stage_form_value(0, fields["ack_policy"], True)
    doc.stage_form_value(0, fields["department"], "Finance")
    doc.stage_form_value(0, fields["shift/Night"], "Night")
    return fields


# --- M2 UI harness helpers (the tests/test_annotations.py pattern) ---------
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
    # Fills live in FILL FORM mode (three-mode feature): enter it so the
    # fillable-field hotspots materialize. No-op on a form-free doc.
    from pdftexteditor.ui.page_view import TOP_FILL_FORM
    w.view.set_top_mode(TOP_FILL_FORM)
    pump(3)
    return w


def close_window(w: MainWindow) -> None:
    """Close WITHOUT the dirty-discard modal (blocks forever offscreen)."""
    w._suppress_close_guard = True
    w.close()
    pump(2)


def hotspot_named(view, name: str, on_state: str | None = None):
    """The CURRENT FieldHotspot for a page-0 field (hotspots are rebuilt on
    every repaint, so tests re-fetch instead of holding refs)."""
    for hs in view._layers[0].field_hotspots:
        if hs.field.name == name and (on_state is None
                                      or hs.field.on_state == on_state):
            return hs
    return None


def press_field(view, hotspot) -> bool:
    """Drive the view's field-press routing directly (spec M2: the
    ``_on_field_press`` equivalent of a real click on the hotspot)."""
    handled = view._on_field_press(hotspot, hotspot.rect().center())
    pump(3)
    return handled


def layer_rect_ink(view, rect_pdf, page: int = 0) -> int:
    """Non-white pixels inside a text-space rect of the page's LIVE layer
    image -- the pixmap the user actually sees, produced by the real
    repaint path. The render scale is derived from the image width so the
    measure is zoom-independent."""
    img: QImage = view._layers[page].image
    s = img.width() / PAGE_W_PT
    x0, y0, x1, y1 = (int(round(v * s)) for v in rect_pdf)
    crop = img.copy(QRect(x0, y0, x1 - x0, y1 - y0)).convertToFormat(
        QImage.Format_RGB888)
    h, wd, bpl = crop.height(), crop.width(), crop.bytesPerLine()
    arr = np.frombuffer(bytes(crop.constBits()), dtype=np.uint8)
    arr = arr.reshape(h, bpl)[:, : wd * 3].reshape(h, wd, 3)
    return array_ink(arr)


def view_scale(view, page: int = 0) -> float:
    """The layer image's render scale (pixels per PDF point)."""
    return view._layers[page].image.width() / PAGE_W_PT


# --- M3 navigation helpers --------------------------------------------------
def tab_key(view, back: bool = False) -> None:
    """One REAL Tab/Backtab press on the view widget (spec M3): exercises
    the whole delivery chain -- QGraphicsView's event pre-pass offers the
    key to the scene (a mounted FormFieldEditor DECLINES it via
    tabChangesFocus), the view's focusNextPrevChild returns False on a
    form doc, and the key lands in keyPressEvent -> form_tab_step."""
    if back:
        QTest.keyClick(view, Qt.Key_Backtab, Qt.ShiftModifier)
    else:
        QTest.keyClick(view, Qt.Key_Tab)
    pump(3)


def view_key(view, key, mods=Qt.NoModifier) -> None:
    """A synthesized view-level key (the M2 editor-key pattern)."""
    view.keyPressEvent(QKeyEvent(QKeyEvent.Type.KeyPress, key, mods))
    pump(3)


def order_field(doc: PDFDocument, name: str, on_state: str | None = None):
    """A FormField from ``form_tab_order`` by name (radio kids by state)."""
    for f in doc.form_tab_order():
        if f.name == name and (on_state is None or f.on_state == on_state):
            return f
    return None


def focus_sig(view):
    """The focused field as a comparable (page, name, on_state) tuple."""
    f = view._focused_field
    return None if f is None else (f.page_index, f.name, f.on_state)


# ---------------------------------------------------------------------------
# 1. detect: has_form / form_field_count / the flat regression control
# ---------------------------------------------------------------------------
def test_detect(failures: list) -> None:
    doc = PDFDocument(ACROFORM)
    try:
        check(failures, "detect.has_form", doc.has_form is True,
              f"has_form {doc.has_form!r} is not True on acroform.pdf")
        check(failures, "detect.count", doc.form_field_count == 6,
              f"form_field_count {doc.form_field_count} != 6")
        check(failures, "detect.p0_widgets", len(doc.form_fields(0)) == 6,
              f"page 0 widget census {len(doc.form_fields(0))} != 6")
        check(failures, "detect.p1_widgets", len(doc.form_fields(1)) == 1,
              f"page 1 widget census {len(doc.form_fields(1))} != 1")
        check(failures, "detect.memo",
              doc.form_fields(0) is doc.form_fields(0),
              "form_fields is not memoized per page")
    finally:
        doc.close()

    flat = PDFDocument(FORM_LIKE)
    try:
        check(failures, "detect.flat_false", flat.has_form is False,
              f"has_form {flat.has_form!r} is not False on form_like.pdf")
        check(failures, "detect.flat_count", flat.form_field_count == 0,
              f"form_like form_field_count {flat.form_field_count} != 0")
        check(failures, "detect.flat_fields", flat.form_fields(0) == [],
              f"form_like form_fields not empty: {flat.form_fields(0)!r}")
        check(failures, "detect.flat_tab", flat.form_tab_order() == [],
              "form_like form_tab_order not empty")
    finally:
        flat.close()


# ---------------------------------------------------------------------------
# 2. enumeration: kinds / names / options / on-states / rects / tab order
# ---------------------------------------------------------------------------
def test_enumeration(failures: list) -> None:
    doc = PDFDocument(ACROFORM)
    try:
        fields = fields_by_key(doc)
        check(failures, "enum.names",
              sorted(fields) == ["ack_policy", "department", "employee_name",
                                 "notes", "shift/Day", "shift/Night"],
              f"page 0 field keys wrong: {sorted(fields)}")

        emp = fields.get("employee_name")
        if check(failures, "enum.emp", emp is not None,
                 "employee_name missing"):
            check(failures, "enum.emp_kind",
                  emp.kind == "text" and not emp.multiline
                  and not emp.readonly,
                  f"employee_name kind/flags wrong: {emp!r}")
            check(failures, "enum.emp_value", emp.value == "",
                  f"employee_name baseline {emp.value!r} != ''")
            check(failures, "enum.emp_rect", emp.rect == EMPLOYEE_RECT,
                  f"employee_name rect {emp.rect} != {EMPLOYEE_RECT}")
            check(failures, "enum.emp_fontsize", emp.text_fontsize == 11.0,
                  f"employee_name fontsize {emp.text_fontsize} != 11.0")
            check(failures, "enum.emp_maxlen", emp.max_len == 0,
                  f"employee_name max_len {emp.max_len} != 0")
            check(failures, "enum.emp_identity",
                  emp.identity == (0, "form", "employee_name", emp.xref)
                  and emp.group_key == (0, "form", "employee_name")
                  and emp.xref > 0,
                  f"employee_name identity/group_key wrong: {emp.identity} "
                  f"{emp.group_key}")

        notes = fields.get("notes")
        if check(failures, "enum.notes", notes is not None, "notes missing"):
            check(failures, "enum.notes_multiline",
                  notes.kind == "text" and notes.multiline,
                  f"notes not a multiline text field: {notes!r}")

        ack = fields.get("ack_policy")
        if check(failures, "enum.ack", ack is not None,
                 "ack_policy missing"):
            check(failures, "enum.ack_kind",
                  ack.kind == "checkbox" and ack.value is False
                  and ack.on_state == "Yes",
                  f"ack_policy kind/value/on_state wrong: {ack!r}")

        dept = fields.get("department")
        if check(failures, "enum.dept", dept is not None,
                 "department missing"):
            check(failures, "enum.dept_kind",
                  dept.kind == "combo" and dept.options == DEPARTMENTS
                  and dept.value == "People Ops",
                  f"department kind/options/value wrong: {dept!r}")

        day, night = fields.get("shift/Day"), fields.get("shift/Night")
        if check(failures, "enum.radio", day is not None
                 and night is not None, "shift radio kids missing"):
            check(failures, "enum.radio_kind",
                  day.kind == "radio" and night.kind == "radio",
                  f"shift kids not radio: {day.kind} {night.kind}")
            check(failures, "enum.radio_states",
                  day.on_state == "Day" and night.on_state == "Night",
                  f"radio on-states wrong: {day.on_state} {night.on_state}")
            check(failures, "enum.radio_baseline",
                  day.value == "Off" and night.value == "Off",
                  f"radio group baseline not 'Off': {day.value!r}")
            check(failures, "enum.radio_group",
                  day.group_key == night.group_key == (0, "form", "shift")
                  and day.identity != night.identity,
                  "radio kids must share group_key with distinct identities")

        p1 = doc.form_fields(1)
        check(failures, "enum.manager",
              len(p1) == 1 and p1[0].name == "manager_name"
              and p1[0].kind == "text" and p1[0].page_index == 1,
              f"page 1 enumeration wrong: {p1!r}")

        order = [(f.page_index, f.name, f.on_state)
                 for f in doc.form_tab_order()]
        check(failures, "enum.tab_order",
              order == [(0, "employee_name", None), (0, "notes", None),
                        (0, "ack_policy", "Yes"), (0, "department", None),
                        (0, "shift", "Day"), (0, "shift", "Night"),
                        (1, "manager_name", None)],
              f"form_tab_order wrong: {order}")
    finally:
        doc.close()


# ---------------------------------------------------------------------------
# 3. staging: four kinds, lockstep undo/redo, baseline unstaging, census
# ---------------------------------------------------------------------------
def test_staging_undo(failures: list) -> None:
    doc = PDFDocument(ACROFORM)
    try:
        fields = fields_by_key(doc)
        emp, ack = fields["employee_name"], fields["ack_policy"]
        dept, night = fields["department"], fields["shift/Night"]

        check(failures, "stage.pristine",
              doc.edit_count == 0 and not doc.dirty and not doc.can_undo,
              "fresh doc not pristine")

        doc.stage_form_value(0, emp, "Jordan Carter")
        doc.stage_form_value(0, ack, True)
        doc.stage_form_value(0, dept, "Finance")
        doc.stage_form_value(0, night, "Night")
        check(failures, "stage.census",
              doc.edit_count == 4 and doc.dirty and doc.has_edits,
              f"4 fills: edit_count {doc.edit_count}, dirty {doc.dirty}")
        check(failures, "stage.effective",
              doc.effective_form_value(0, emp) == "Jordan Carter"
              and doc.effective_form_value(0, ack) is True
              and doc.effective_form_value(0, dept) == "Finance"
              and doc.effective_form_value(0, night) == "Night",
              "effective_form_value does not reflect the staged fills")
        check(failures, "stage.day_kid_shares",
              doc.effective_form_value(0, fields["shift/Day"]) == "Night",
              "radio kids must share the staged group value")

        # Restaging the same value is a no-op (no phantom undo entries).
        depth = len(doc._undo)
        doc.stage_form_value(0, emp, "Jordan Carter")
        check(failures, "stage.noop_restage", len(doc._undo) == depth,
              "restaging an identical value pushed a command")

        # Lockstep undo: one command per fill, FormField box refs.
        ref = doc.undo()
        check(failures, "stage.undo_ref",
              ref is not None and ref[0] == 0
              and isinstance(ref[1], FormField) and ref[1].name == "shift",
              f"undo box ref wrong: {ref!r}")
        check(failures, "stage.undo_state",
              doc.edit_count == 3
              and doc.effective_form_value(0, night) == "Off",
              f"undo did not unstage the radio fill: {doc.edit_count}")
        ref = doc.redo()
        check(failures, "stage.redo",
              ref is not None and doc.edit_count == 4
              and doc.effective_form_value(0, night) == "Night",
              "redo did not restore the radio fill")

        # A second value for a staged field replaces in ONE command; undo
        # returns to the PREVIOUS staged value, not the baseline.
        doc.stage_form_value(0, emp, "Riley Morgan")
        check(failures, "stage.replace",
              doc.effective_form_value(0, emp) == "Riley Morgan"
              and doc.edit_count == 4,
              "restaging a new value must replace, not add")
        doc.undo()
        check(failures, "stage.undo_to_prior",
              doc.effective_form_value(0, emp) == "Jordan Carter",
              f"undo of a replace must restore the prior staged value, got "
              f"{doc.effective_form_value(0, emp)!r}")

        # Staging the BASELINE back unstages (mirrors the no-op Edit drop).
        doc.stage_form_value(0, emp, "")
        check(failures, "stage.revert_unstages",
              emp.group_key not in doc._form_edits and doc.edit_count == 3,
              "staging the baseline value back must drop the entry")
        doc.undo()
        check(failures, "stage.revert_undo",
              doc.effective_form_value(0, emp) == "Jordan Carter",
              "undoing the revert must restore the staged value")

        # Full undo chain reads clean again (census-derived dirty).
        while doc.can_undo:
            doc.undo()
        check(failures, "stage.clean_after_undo",
              doc.edit_count == 0 and not doc.dirty
              and not doc._form_edits,
              f"full undo: edit_count {doc.edit_count}, dirty {doc.dirty}")
        doc.redo()
        check(failures, "stage.redirty", doc.edit_count == 1 and doc.dirty,
              "redo must re-dirty")

        # Combo validation: a non-option raises BEFORE any state changes.
        count = doc.edit_count
        try:
            doc.stage_form_value(0, dept, "Logistics")
            check(failures, "stage.combo_guard", False,
                  "non-option combo value did not raise")
        except ValueError:
            check(failures, "stage.combo_guard_state",
                  doc.edit_count == count,
                  "failed combo staging changed state")

        # Non-fillable kinds are rejected (form_tab_order excludes them).
        bogus = FormField(page_index=0, name="sig", kind="signature",
                          rect=(0, 0, 10, 10), xref=999, value="")
        try:
            doc.stage_form_value(0, bogus, "x")
            check(failures, "stage.kind_guard", False,
                  "staging a signature field did not raise")
        except ValueError:
            pass
    finally:
        doc.close()


# ---------------------------------------------------------------------------
# 4. render == save: field-rect ink, whole-page parity, reopen, isolation
# ---------------------------------------------------------------------------
def test_render_save_parity(failures: list) -> None:
    td = tempfile.mkdtemp(prefix="forms_parity_")
    with open(ACROFORM, "rb") as fh:
        source_bytes = fh.read()
    doc = PDFDocument(ACROFORM)
    try:
        fields = stage_standard_fills(doc)
        doc.stage_form_value(0, fields["notes"], "Line one\nLine two")

        base = doc.render(0, 2.0)        # the unfilled working doc
        pix = doc.render_with_edits(0, 2.0)
        # Empty-baseline fields GAIN ink; the combo (baseline "People Ops" ->
        # "Finance") just has to CHANGE -- the new value can be narrower.
        for tag, rect in (("employee", EMPLOYEE_RECT), ("notes", NOTES_RECT),
                          ("ack", ACK_RECT)):
            gained = pix_ink(pix, rect) - pix_ink(base, rect)
            check(failures, f"parity.{tag}_ink", gained > 0,
                  f"{tag} field rect gained no ink after staging "
                  f"({pix_ink(base, rect)} -> {pix_ink(pix, rect)})")
        check(failures, "parity.dept_ink",
              pix_ink(pix, DEPT_RECT) != pix_ink(base, DEPT_RECT)
              and pix_ink(pix, DEPT_RECT) > 0,
              f"dept field rect did not re-render the staged combo value "
              f"({pix_ink(base, DEPT_RECT)} -> {pix_ink(pix, DEPT_RECT)})")

        # Disk isolation: nothing above touched the fixture bytes.
        with open(ACROFORM, "rb") as fh:
            check(failures, "parity.disk_isolation",
                  fh.read() == source_bytes,
                  "staging/rendering mutated the fixture on disk")

        out = os.path.join(td, "filled.pdf")
        doc.save_as(out)
        spix = saved_pix(out)
        rink, sink = pix_ink(pix), pix_ink(spix)
        rel = abs(rink - sink) / max(sink, 1)
        check(failures, "parity.wysiwyg", rel < 0.02,
              f"render vs saved ink rel diff {rel:.4f} >= 0.02 "
              f"({rink} vs {sink})")
        for rect in (EMPLOYEE_RECT, NOTES_RECT, ACK_RECT, DEPT_RECT):
            check(failures, "parity.rect_match",
                  pix_ink(pix, rect) == pix_ink(spix, rect),
                  f"field-rect ink differs render vs saved at {rect}")

        # Reopen: values persist AND the form stays a live, fillable form.
        got = {(pi, name): (value, on)
               for (pi, name, value, on) in saved_widgets(out)}
        check(failures, "parity.reopen_values",
              got.get((0, "employee_name"), ("",))[0] == "Jordan Carter"
              and got.get((0, "ack_policy"), ("",))[0] == "Yes"
              and got.get((0, "department"), ("",))[0] == "Finance"
              and got.get((1, "manager_name"), ("x",))[0] == "",
              f"saved widget values wrong: {got!r}")
        kid_values = sorted(
            value for (pi, name, value, _on) in saved_widgets(out)
            if name == "shift")
        check(failures, "parity.reopen_radio",
              kid_values == ["Night", "Off"],
              f"saved radio kid states wrong: {kid_values}")
        sd = fitz.open(out)
        try:
            check(failures, "parity.reopen_form",
                  bool(sd.is_form_pdf) and int(sd.is_form_pdf) == 6,
                  f"saved file lost its AcroForm: {sd.is_form_pdf!r}")
            note_text = sd[0].load_widget(
                [w.xref for w in sd[0].widgets()
                 if w.field_name == "notes"][0]).field_value
            check(failures, "parity.reopen_multiline",
                  note_text == "Line one\nLine two",
                  f"multiline value wrong after reopen: {note_text!r}")
        finally:
            sd.close()

        # The reopened save renders the SAME pixels through the model too.
        redoc = PDFDocument(out)
        try:
            check(failures, "parity.reopen_pristine",
                  redoc.has_form and redoc.edit_count == 0
                  and not redoc.dirty,
                  "reopened save must be clean (values are baked baselines)")
            check(failures, "parity.reopen_baseline",
                  fields_by_key(redoc)["employee_name"].value
                  == "Jordan Carter",
                  "reopened baseline must be the saved value")
        finally:
            redoc.close()
    finally:
        doc.close()


# ---------------------------------------------------------------------------
# 5. a text edit + a fill coexist on one page (redaction interplay)
# ---------------------------------------------------------------------------
def test_edit_fill_coexist(failures: list) -> None:
    td = tempfile.mkdtemp(prefix="forms_coexist_")
    doc = PDFDocument(ACROFORM)
    try:
        heading = None
        for b in doc.spans(0):
            if "Onboarding" in b.text.replace(" ", " "):
                heading = b
                break
        if not check(failures, "coexist.span", heading is not None,
                     "no 'Onboarding' heading span on page 0"):
            return
        new_text = heading.text.replace(" ", " ").replace(
            "Onboarding", "Orientation")
        doc.stage_edit(0, heading, new_text)
        fields = fields_by_key(doc)
        doc.stage_form_value(0, fields["employee_name"], "Jordan Carter")
        check(failures, "coexist.census", doc.edit_count == 2,
              f"edit + fill census {doc.edit_count} != 2")

        pix = doc.render_with_edits(0, 2.0)
        out = os.path.join(td, "coexist.pdf")
        doc.save_as(out)
        spix = saved_pix(out)
        rel = abs(pix_ink(pix) - pix_ink(spix)) / max(pix_ink(spix), 1)
        check(failures, "coexist.wysiwyg", rel < 0.02,
              f"edit+fill WYSIWYG rel diff {rel:.4f} >= 0.02")

        sd = fitz.open(out)
        try:
            text = sd[0].get_text().replace(" ", " ")
            check(failures, "coexist.edit_applied",
                  "Orientation" in text and "Onboarding" not in text,
                  f"heading edit missing from saved page: {text[:80]!r}")
            values = {w.field_name: w.field_value for w in sd[0].widgets()}
            check(failures, "coexist.widget_survives",
                  values.get("employee_name") == "Jordan Carter"
                  and len(values) == 5,
                  f"widgets/value lost through the redaction pass: "
                  f"{values!r}")
        finally:
            sd.close()
        check(failures, "coexist.fill_ink",
              pix_ink(spix, EMPLOYEE_RECT) == pix_ink(pix, EMPLOYEE_RECT)
              and pix_ink(spix, EMPLOYEE_RECT) > 0,
              "filled value ink missing or mismatched in the edited save")
    finally:
        doc.close()


# ---------------------------------------------------------------------------
# 6. structural ops bake fills into working (rotate_page)
# ---------------------------------------------------------------------------
def test_structural_bake(failures: list) -> None:
    td = tempfile.mkdtemp(prefix="forms_rotate_")
    doc = PDFDocument(ACROFORM)
    try:
        fields = stage_standard_fills(doc)
        del fields
        doc.rotate_page(0, 90)
        check(failures, "rotate.maps_cleared",
              not doc._form_edits and doc.edit_count == 0,
              f"rotate left staged fills: {doc._form_edits!r}")
        baked = {w.field_name: w.field_value
                 for w in doc.working[0].widgets()}
        check(failures, "rotate.baked_into_working",
              baked.get("employee_name") == "Jordan Carter"
              and baked.get("ack_policy") == "Yes"
              and baked.get("department") == "Finance",
              f"fills not baked into working: {baked!r}")
        check(failures, "rotate.baseline_refreshed",
              fields_by_key(doc)["employee_name"].value == "Jordan Carter",
              "form_fields baseline stale after the bake")
        check(failures, "rotate.rotated",
              doc.page_rotation(0) == 90, "page rotation not applied")

        out = os.path.join(td, "rotated.pdf")
        pix = doc.render_with_edits(0, 2.0)
        doc.save_as(out)
        spix = saved_pix(out)
        rel = abs(pix_ink(pix) - pix_ink(spix)) / max(pix_ink(spix), 1)
        check(failures, "rotate.wysiwyg", rel < 0.02,
              f"rotated save rel diff {rel:.4f} >= 0.02")
        check(failures, "rotate.saved_value",
              dict((n, v) for (_p, n, v, _o) in saved_widgets(out))
              .get("employee_name") == "Jordan Carter",
              "baked fill missing from the rotated save")

        # undo_structural restores the pre-rotate, unfilled working doc.
        doc.undo_structural()
        check(failures, "rotate.undo_structural",
              doc.page_rotation(0) == 0
              and fields_by_key(doc)["employee_name"].value == "",
              "undo_structural must restore the unfilled baseline")
    finally:
        doc.close()


# ---------------------------------------------------------------------------
# 7. save_flattened: widget-free output, ink parity, source untouched
# ---------------------------------------------------------------------------
def test_flatten(failures: list) -> None:
    td = tempfile.mkdtemp(prefix="forms_flatten_")
    doc = PDFDocument(ACROFORM)
    try:
        stage_standard_fills(doc)
        out = os.path.join(td, "filled.pdf")
        flat = os.path.join(td, "flat.pdf")
        doc.save_as(out)
        doc.save_flattened(flat)

        fd = fitz.open(flat)
        try:
            check(failures, "flatten.no_form",
                  bool(fd.is_form_pdf) is False,
                  f"flattened output still a form: {fd.is_form_pdf!r}")
            widget_count = sum(1 for pi in range(fd.page_count)
                               for _w in fd[pi].widgets())
            check(failures, "flatten.no_widgets", widget_count == 0,
                  f"{widget_count} widgets survived the flatten")
            check(failures, "flatten.value_is_text",
                  "Jordan Carter" in fd[0].get_text(),
                  "filled value did not become real page text")
            fpix = fd[0].get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
        finally:
            fd.close()
        spix = saved_pix(out)
        rel = abs(pix_ink(fpix) - pix_ink(spix)) / max(pix_ink(spix), 1)
        check(failures, "flatten.ink_parity", rel < 0.02,
              f"flattened vs filled save ink rel diff {rel:.4f} >= 0.02")

        # Non-mutating export: the open doc keeps form + staged state.
        check(failures, "flatten.source_untouched",
              doc.has_form and len(doc._form_edits) == 4 and doc.dirty
              and doc.path == ACROFORM,
              "save_flattened mutated the open document's state")
    finally:
        doc.close()


# ---------------------------------------------------------------------------
# 8. render_signature: the one cache-key registry folds form values
# ---------------------------------------------------------------------------
def test_signature_registry(failures: list) -> None:
    doc = PDFDocument(ACROFORM)
    try:
        fields = fields_by_key(doc)
        emp = fields["employee_name"]
        sig0_p0 = doc.render_signature(0)
        sig0_p1 = doc.render_signature(1)
        check(failures, "sig.stable", doc.render_signature(0) == sig0_p0,
              "render_signature unstable across repeated calls")

        doc.stage_form_value(0, emp, "Jordan Carter")
        check(failures, "sig.miss_after_stage",
              doc.render_signature(0) != sig0_p0,
              "stage_form_value did not change page 0's signature")
        check(failures, "sig.other_page",
              doc.render_signature(1) == sig0_p1,
              "a page-0 fill changed page 1's signature")

        staged_sig = doc.render_signature(0)
        doc.stage_form_value(0, emp, "Riley Morgan")
        check(failures, "sig.miss_on_change",
              doc.render_signature(0) not in (sig0_p0, staged_sig),
              "restaging a different value did not change the signature")

        doc.undo()
        check(failures, "sig.undo_prior",
              doc.render_signature(0) == staged_sig,
              "undo did not restore the prior staged signature")
        doc.undo()
        check(failures, "sig.pristine_back",
              doc.render_signature(0) == sig0_p0,
              "undo to baseline did not restore the PRISTINE signature")
    finally:
        doc.close()


# ---------------------------------------------------------------------------
# 9. extract / merge carry staged fills (the _bake_doc companions)
# ---------------------------------------------------------------------------
def test_extract_merge_carry(failures: list) -> None:
    doc = PDFDocument(ACROFORM)
    try:
        stage_standard_fills(doc)

        data = doc.extract_pages([0])
        ex = fitz.open("pdf", data)
        try:
            values = {w.field_name: w.field_value for w in ex[0].widgets()}
            check(failures, "carry.extract",
                  values.get("employee_name") == "Jordan Carter"
                  and values.get("department") == "Finance",
                  f"extract_pages dropped staged fills: {values!r}")
        finally:
            ex.close()
        check(failures, "carry.extract_nonmutating",
              len(doc._form_edits) == 4,
              "extract_pages must not clear the staged fills")

        host = PDFDocument(FORM_LIKE)
        try:
            first = host.merge(doc)
            values = {w.field_name: w.field_value
                      for w in host.working[first].widgets()}
            check(failures, "carry.merge",
                  values.get("employee_name") == "Jordan Carter",
                  f"merge dropped the source doc's staged fills: {values!r}")
            check(failures, "carry.merge_form", host.has_form,
                  "merged-in widgets did not make the host a form")
        finally:
            host.close()
        check(failures, "carry.merge_nonmutating",
              len(doc._form_edits) == 4 and doc.has_form,
              "merge must not mutate the SOURCE document's staged state")
    finally:
        doc.close()


# ---------------------------------------------------------------------------
# 10. M2 UI: hotspot census, zero-chrome controls, badge, open toast
# ---------------------------------------------------------------------------
def test_ui_hotspots_badge(failures: list) -> None:
    w = open_window(ACROFORM)
    try:
        v = w.view
        layer = v._layers[0]
        spots = layer.field_hotspots
        check(failures, "ui.hotspot_census", len(spots) == 6,
              f"page 0 field hotspots {len(spots)} != 6 (all 6 widgets are "
              f"fillable)")
        check(failures, "ui.hotspot_z",
              all(hs.zValue() == Z_FORM_HOTSPOT for hs in spots),
              f"hotspot z values {sorted({hs.zValue() for hs in spots})} != "
              f"[{Z_FORM_HOTSPOT}]")
        check(failures, "ui.hotspot_tracked",
              all(hs in layer.extra_items for hs in spots),
              "field hotspots must ride layer.extra_items (factory seam)")
        kinds = sorted({hs.field.kind for hs in spots})
        check(failures, "ui.hotspot_kinds",
              kinds == ["checkbox", "combo", "radio", "text"],
              f"hotspot kinds wrong: {kinds}")
        idents = {hs.identity for hs in spots}
        check(failures, "ui.hotspot_identities", len(idents) == 6,
              "radio kids must be separate hotspots (per-widget identity)")

        check(failures, "ui.badge_visible",
              w.form_badge.isVisible()
              and w.form_badge.text() == "Form · 6 fields",
              f"badge wrong on the form doc: visible="
              f"{w.form_badge.isVisible()} text={w.form_badge.text()!r}")
        check(failures, "ui.open_toast",
              w.filename_label.text()
              == "6 form fields detected. Click a field to fill it.",
              f"open toast wrong: {w.filename_label.text()!r}")

        # Zero new chrome on form-free docs (the spec's regression control
        # pair): no hotspots, no badge -- in new tabs of the SAME window.
        for path, tag in ((FORM_LIKE, "form_like"), (PARAGRAPHS, "plain")):
            w.open_path(path)
            pump(6)
            spots = w.view._layers[0].field_hotspots
            check(failures, f"ui.{tag}_no_hotspots", spots == [],
                  f"{tag}: {len(spots)} field hotspots on a form-free doc")
            check(failures, f"ui.{tag}_no_badge",
                  not w.form_badge.isVisible(),
                  f"{tag}: form badge visible on a form-free doc")
            check(failures, f"ui.{tag}_no_form_toast",
                  "form field" not in w.filename_label.text(),
                  f"{tag}: fill-hint toast on a form-free doc: "
                  f"{w.filename_label.text()!r}")

        # Switching back to the form tab re-syncs the badge (open_path
        # de-dups on realpath, so this is a tab switch).
        w.open_path(ACROFORM)
        pump(6)
        check(failures, "ui.badge_resync",
              w.form_badge.isVisible()
              and w.form_badge.text() == "Form · 6 fields",
              "badge must come back on the tab switch to the form doc")
    finally:
        close_window(w)


# ---------------------------------------------------------------------------
# 11. M2 UI: checkbox / radio / combo presses -> one command each, lockstep
# ---------------------------------------------------------------------------
def test_ui_click_fills(failures: list) -> None:
    w = open_window(ACROFORM)
    try:
        v, doc, stack = w.view, w.document, w.undo_stack

        # Checkbox: one press = ONE FormFieldCommand, model staged.
        ack = hotspot_named(v, "ack_policy")
        base_ink = layer_rect_ink(v, ACK_RECT)
        check(failures, "ui.ack_press", press_field(v, ack) is True,
              "checkbox press not handled")
        check(failures, "ui.ack_command",
              stack.count() == 1
              and isinstance(stack.command(0), FormFieldCommand)
              and stack.undoText() == "Check ‘ack_policy’",
              f"checkbox press: count {stack.count()}, "
              f"undoText {stack.undoText()!r}")
        check(failures, "ui.ack_staged",
              doc._form_edits.get((0, "form", "ack_policy")) is True
              and doc.edit_count == 1,
              f"checkbox not staged in lockstep: {doc._form_edits!r}")
        check(failures, "ui.ack_ink",
              layer_rect_ink(v, ACK_RECT) > base_ink,
              "checked box gained no ink in the repainted page pixmap")
        check(failures, "ui.ack_focus",
              v._focused_field is not None
              and v._focused_field.name == "ack_policy",
              "checkbox press must focus the field")

        # Radio kid: picks its on-state; a REPEAT press is a no-op (the
        # window guard -- no phantom Qt command over a no-op model stage).
        night = hotspot_named(v, "shift", "Night")
        press_field(v, night)
        check(failures, "ui.radio_pick",
              stack.count() == 2
              and doc.effective_form_value(0, night.field) == "Night",
              f"radio press: count {stack.count()}, value "
              f"{doc.effective_form_value(0, night.field)!r}")
        night = hotspot_named(v, "shift", "Night")
        press_field(v, night)
        check(failures, "ui.radio_repeat_noop", stack.count() == 2,
              "re-picking the selected radio kid must push nothing")

        # Combo: routes through the injectable provider seam (no QMenu
        # exec offscreen); a dismissed menu (None) stages nothing.
        v._combo_menu_provider = lambda field, rect: None
        dept = hotspot_named(v, "department")
        press_field(v, dept)
        check(failures, "ui.combo_dismissed", stack.count() == 2,
              "a dismissed combo menu must push nothing")
        v._combo_menu_provider = lambda field, rect: "Finance"
        dept = hotspot_named(v, "department")
        press_field(v, dept)
        check(failures, "ui.combo_pick",
              stack.count() == 3
              and doc.effective_form_value(0, dept.field) == "Finance",
              f"combo pick: count {stack.count()}, value "
              f"{doc.effective_form_value(0, dept.field)!r}")

        # Undo/redo walk both stacks in lockstep back to pristine.
        while stack.canUndo():
            stack.undo()
        pump(3)
        check(failures, "ui.fills_undo_pristine",
              doc.edit_count == 0 and not doc._form_edits
              and layer_rect_ink(v, ACK_RECT) == base_ink,
              f"full undo: edit_count {doc.edit_count}, ack ink "
              f"{layer_rect_ink(v, ACK_RECT)} != {base_ink}")
        stack.redo()
        pump(3)
        check(failures, "ui.fills_redo",
              doc.edit_count == 1
              and doc._form_edits.get((0, "form", "ack_policy")) is True,
              "redo did not restore the checkbox fill")
    finally:
        close_window(w)


# ---------------------------------------------------------------------------
# 12. M2 UI: the inline text FormFieldEditor end to end
# ---------------------------------------------------------------------------
def test_ui_text_editor(failures: list) -> None:
    td = tempfile.mkdtemp(prefix="forms_ui_editor_")
    w = open_window(ACROFORM)
    try:
        v, doc, stack = w.view, w.document, w.undo_stack
        base_ink = layer_rect_ink(v, EMPLOYEE_RECT)

        # Mount: editor + white cover, seeded from the (empty) baseline.
        emp = hotspot_named(v, "employee_name")
        press_field(v, emp)
        check(failures, "ui.editor_mounted",
              isinstance(v._form_editor, FormFieldEditor)
              and v._form_editor_cover is not None
              and v._form_editor.toPlainText() == ""
              and emp._editing is True,
              "text press must mount the seeded FormFieldEditor + cover")

        # Commit with NO change: editor closes, nothing pushed.
        v.commit_form_editor()
        pump(3)
        check(failures, "ui.editor_noop_commit",
              v._form_editor is None and stack.count() == 0,
              f"unchanged commit pushed {stack.count()} command(s)")

        # A real commit: ONE command, staged in lockstep, ink in the rect.
        emp = hotspot_named(v, "employee_name")
        press_field(v, emp)
        v._form_editor.setPlainText("Jordan Carter")
        v.commit_form_editor()
        pump(4)
        filled_ink = layer_rect_ink(v, EMPLOYEE_RECT)
        check(failures, "ui.editor_commit",
              stack.count() == 1 and stack.undoText() == "Fill ‘employee_name’"
              and doc._form_edits.get((0, "form", "employee_name"))
              == "Jordan Carter",
              f"editor commit: count {stack.count()}, "
              f"undoText {stack.undoText()!r}, staged {doc._form_edits!r}")
        check(failures, "ui.editor_ink", filled_ink > base_ink,
              f"committed value gained no ink "
              f"({base_ink} -> {filled_ink})")

        # WYSIWYG through the REAL repaint: the on-screen layer pixmap's
        # field-rect ink equals the saved file's render at the same scale.
        s = view_scale(v)
        out = os.path.join(td, "ui_filled.pdf")
        doc.save_as(out)
        spix = saved_pix(out, scale=s)
        check(failures, "ui.editor_wysiwyg",
              pix_ink(spix, EMPLOYEE_RECT, scale=s) == filled_ink,
              f"screen vs saved field-rect ink differ: {filled_ink} vs "
              f"{pix_ink(spix, EMPLOYEE_RECT, scale=s)}")

        # Undo restores the BASELINE pixels; redo restores the fill.
        stack.undo()
        pump(3)
        check(failures, "ui.editor_undo",
              doc.edit_count == 0
              and layer_rect_ink(v, EMPLOYEE_RECT) == base_ink,
              "undo did not restore the unfilled pixels")
        stack.redo()
        pump(3)
        check(failures, "ui.editor_redo",
              doc.effective_form_value(0, emp.field) == "Jordan Carter"
              and layer_rect_ink(v, EMPLOYEE_RECT) == filled_ink,
              "redo did not restore the filled pixels")

        # Re-mount seeds from the STAGED value; Esc cancels, no command.
        emp = hotspot_named(v, "employee_name")
        press_field(v, emp)
        check(failures, "ui.editor_reseed",
              v._form_editor.toPlainText() == "Jordan Carter",
              f"editor must seed from the staged value, got "
              f"{v._form_editor.toPlainText()!r}")
        v._form_editor.setPlainText("WRONG")
        v._form_editor.keyPressEvent(QKeyEvent(
            QKeyEvent.Type.KeyPress, Qt.Key_Escape, Qt.NoModifier))
        pump(3)
        check(failures, "ui.editor_esc",
              v._form_editor is None and stack.count() == 1
              and doc.effective_form_value(0, emp.field) == "Jordan Carter",
              "Esc must cancel without a command")

        # A zoom-triggered flush COMMITS a half-typed value (one command).
        emp = hotspot_named(v, "employee_name")
        press_field(v, emp)
        v._form_editor.setPlainText("Riley Morgan")
        v.set_zoom(v.zoom * 1.25)
        pump(4)
        check(failures, "ui.editor_flush",
              v._form_editor is None and stack.count() == 2
              and doc.effective_form_value(0, emp.field) == "Riley Morgan",
              f"zoom flush: count {stack.count()}, value "
              f"{doc.effective_form_value(0, emp.field)!r}")

        # Multiline: Return inserts a newline; Cmd+Return commits.
        notes = hotspot_named(v, "notes")
        press_field(v, notes)
        editor = v._form_editor
        editor.setPlainText("Line one")
        cursor = editor.textCursor()
        cursor.movePosition(QTextCursor.End)
        editor.setTextCursor(cursor)
        editor.keyPressEvent(QKeyEvent(
            QKeyEvent.Type.KeyPress, Qt.Key_Return, Qt.NoModifier))
        check(failures, "ui.editor_multiline_return",
              v._form_editor is editor,
              "Return must NOT commit a multiline field")
        editor.textCursor().insertText("Line two")
        editor.keyPressEvent(QKeyEvent(
            QKeyEvent.Type.KeyPress, Qt.Key_Return, Qt.ControlModifier))
        pump(3)
        check(failures, "ui.editor_multiline_commit",
              v._form_editor is None
              and doc.effective_form_value(0, notes.field)
              == "Line one\nLine two",
              f"Cmd+Return commit wrong: "
              f"{doc.effective_form_value(0, notes.field)!r}")
    finally:
        close_window(w)


# ---------------------------------------------------------------------------
# 13. M3 UI: Tab-order navigation (cross-page wrap, mid-edit commit, keys)
# ---------------------------------------------------------------------------
def test_ui_tab_navigation(failures: list) -> None:
    w = open_window(ACROFORM)
    try:
        v, doc, stack = w.view, w.document, w.undo_stack

        # The full Tab walk: every fillable widget in form_tab_order,
        # page 0 crossing to page 1 (the viewport follows) and the last
        # field WRAPPING back to the first; text fields auto-mount their
        # editor, buttons (checkbox/radio kids/combo) just take the ring.
        expected = [(0, "employee_name", None), (0, "notes", None),
                    (0, "ack_policy", "Yes"), (0, "department", None),
                    (0, "shift", "Day"), (0, "shift", "Night"),
                    (1, "manager_name", None), (0, "employee_name", None)]
        expected_editors = [True, True, False, False, False, False,
                            True, True]
        expected_pages = [0, 0, 0, 0, 0, 0, 1, 0]
        walk, editors, pages = [], [], []
        for _ in expected:
            tab_key(v)
            walk.append(focus_sig(v))
            editors.append(v._form_editor is not None)
            pages.append(w.view_page_index())
        check(failures, "nav.tab_walk", walk == expected,
              f"Tab walk wrong:\n  got  {walk}\n  want {expected}")
        check(failures, "nav.editor_mounts", editors == expected_editors,
              f"text fields must auto-mount the editor: {editors}")
        check(failures, "nav.page_follows", pages == expected_pages,
              f"view_page_index must follow the walk: {pages} != "
              f"{expected_pages}")

        # Shift+Tab reverses (and re-wraps backwards across the pages).
        tab_key(v, back=True)
        check(failures, "nav.backtab_wrap",
              focus_sig(v) == (1, "manager_name", None)
              and w.view_page_index() == 1,
              f"Backtab from the first field must wrap to the last: "
              f"{focus_sig(v)} page {w.view_page_index()}")
        tab_key(v, back=True)
        check(failures, "nav.backtab_step",
              focus_sig(v) == (0, "shift", "Night")
              and w.view_page_index() == 0,
              f"Backtab must step to the previous field: {focus_sig(v)}")

        # The whole walk committed only UNCHANGED editors: zero commands.
        check(failures, "nav.no_phantom_commands", stack.count() == 0,
              f"{stack.count()} command(s) from a pure navigation walk")

        # Mid-edit Tab: the half-typed value commits as ONE command, then
        # the NEXT field takes focus with its own editor.
        v.focus_field(order_field(doc, "employee_name"))
        pump(2)
        check(failures, "nav.focus_field_mounts",
              v._form_editor is not None
              and v._form_editor.field.name == "employee_name",
              "focus_field on a text field must mount its editor")
        v._form_editor.setPlainText("Riley Morgan")
        tab_key(v)
        check(failures, "nav.midedit_commit",
              stack.count() == 1
              and stack.undoText() == "Fill ‘employee_name’"
              and doc._form_edits.get((0, "form", "employee_name"))
              == "Riley Morgan",
              f"mid-edit Tab: count {stack.count()}, undoText "
              f"{stack.undoText()!r}, staged {doc._form_edits!r}")
        check(failures, "nav.midedit_next",
              focus_sig(v) == (0, "notes", None)
              and v._form_editor is not None
              and v._form_editor.field.name == "notes",
              f"mid-edit Tab must focus the next field: {focus_sig(v)}")

        # Space toggles the FOCUSED checkbox (focus_field on a button
        # field flushes the open editor -- unchanged, so no command).
        ack = order_field(doc, "ack_policy")
        v.focus_field(ack)
        pump(2)
        check(failures, "nav.focus_button",
              v._form_editor is None
              and focus_sig(v) == (0, "ack_policy", "Yes"),
              f"focus_field on a checkbox must wait for keys: "
              f"{focus_sig(v)}, editor {v._form_editor!r}")
        view_key(v, Qt.Key_Space)
        check(failures, "nav.space_toggle",
              stack.count() == 2
              and doc.effective_form_value(0, ack) is True,
              f"Space on the focused checkbox: count {stack.count()}, "
              f"value {doc.effective_form_value(0, ack)!r}")

        # Return on the FOCUSED combo opens its options menu through the
        # injectable provider seam (no QMenu exec offscreen).
        seen = []
        v._combo_menu_provider = \
            lambda f, r: (seen.append(f.name), "Engineering")[1]
        dept = order_field(doc, "department")
        v.focus_field(dept)
        pump(2)
        view_key(v, Qt.Key_Return)
        check(failures, "nav.return_combo",
              seen == ["department"] and stack.count() == 3
              and doc.effective_form_value(0, dept) == "Engineering",
              f"Return on the focused combo: provider calls {seen}, count "
              f"{stack.count()}, value "
              f"{doc.effective_form_value(0, dept)!r}")

        # Esc drops the focus ring.
        view_key(v, Qt.Key_Escape)
        check(failures, "nav.esc_clears", v._focused_field is None,
              "Esc must clear the field focus")

        # A form-free doc declines the step entirely (the Tab key keeps
        # its normal widget-focus meaning there).
        w.open_path(FORM_LIKE)
        pump(6)
        check(failures, "nav.form_free_decline",
              w.view.form_tab_step(True) is False,
              "form_tab_step must decline on a form-free doc")
    finally:
        close_window(w)


# ---------------------------------------------------------------------------
# 14. M3 UI: File > Export Flattened Copy (enablement + the work seam)
# ---------------------------------------------------------------------------
def test_ui_flatten(failures: list) -> None:
    td = tempfile.mkdtemp(prefix="forms_flatten_ui_")
    w = open_window(ACROFORM)
    try:
        v, doc, stack = w.view, w.document, w.undo_stack

        # Menu placement: appended to the END of the file_output group
        # (right after ws6's Properties entry), enabled on the form doc.
        file_actions = w.menu_file.actions()
        check(failures, "flatui.menu_member",
              w.act_flatten in file_actions
              and file_actions.index(w.act_flatten)
              == file_actions.index(w.act_properties) + 1,
              "act_flatten must directly follow act_properties in File")
        check(failures, "flatui.enabled", w.act_flatten.isEnabled(),
              "act_flatten must be enabled on a form doc")

        # Stage a fill through the real window funnel, then export via
        # the dialog-free seam.
        emp = fields_by_key(doc)["employee_name"]
        w._on_form_command_requested(emp, {"value": "Jordan Carter"})
        pump(4)
        dirty_before = doc.dirty
        count_before = stack.count()
        screen_ink = layer_rect_ink(v, EMPLOYEE_RECT)
        check(failures, "flatui.staged_ink", screen_ink > 0,
              "staged fill must show on the live layer before the export")
        s = view_scale(v)
        out = os.path.join(td, "flat_ui.pdf")
        w._do_export_flattened(out)
        pump(2)

        fd = fitz.open(out)
        try:
            widget_count = sum(1 for pi in range(fd.page_count)
                               for _wd in fd[pi].widgets())
            check(failures, "flatui.no_form",
                  bool(fd.is_form_pdf) is False and widget_count == 0,
                  f"flattened output: is_form_pdf {fd.is_form_pdf!r}, "
                  f"{widget_count} widget(s) left")
            check(failures, "flatui.value_is_text",
                  "Jordan Carter" in fd[0].get_text(),
                  "staged value did not become real page text")
            fpix = fd[0].get_pixmap(matrix=fitz.Matrix(s, s), alpha=False)
        finally:
            fd.close()
        check(failures, "flatui.wysiwyg",
              pix_ink(fpix, EMPLOYEE_RECT, scale=s) == screen_ink,
              f"flattened field-rect ink "
              f"{pix_ink(fpix, EMPLOYEE_RECT, scale=s)} != on-screen "
              f"{screen_ink}")

        # Non-mutating export: the OPEN doc keeps its AcroForm, staged
        # state, dirty flag, and Qt history (no undo entry).
        check(failures, "flatui.source_untouched",
              doc.has_form
              and doc._form_edits.get((0, "form", "employee_name"))
              == "Jordan Carter"
              and doc.dirty == dirty_before
              and stack.count() == count_before
              and doc.path == ACROFORM,
              "_do_export_flattened mutated the open document")
        check(failures, "flatui.toast",
              w.filename_label.text() == "Exported flattened copy",
              f"flatten toast wrong: {w.filename_label.text()!r}")

        # The form-free control disables the action; the tab switch back
        # re-enables it (_sync_actions runs on activation).
        w.open_path(FORM_LIKE)
        pump(6)
        check(failures, "flatui.form_free_disabled",
              not w.act_flatten.isEnabled(),
              "act_flatten must be disabled on form_like.pdf")
        w.open_path(ACROFORM)
        pump(6)
        check(failures, "flatui.reenabled", w.act_flatten.isEnabled(),
              "act_flatten must re-enable on the switch back")
    finally:
        close_window(w)


def main() -> int:
    print("Forms harness (M1 model layer + M2 canvas fill UI + M3 tab "
          "navigation & flatten UI, offscreen)\n")
    failures: list[str] = []
    for fn in (test_detect, test_enumeration, test_staging_undo,
               test_render_save_parity, test_edit_fill_coexist,
               test_structural_bake, test_flatten, test_signature_registry,
               test_extract_merge_carry, test_ui_hotspots_badge,
               test_ui_click_fills, test_ui_text_editor,
               test_ui_tab_navigation, test_ui_flatten):
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

    print("PASSED -- has_form detects the synthetic AcroForm (and stays "
          "False on the flat lookalike), form_fields enumerates kinds/"
          "options/on-states/rects with radio kids sharing one group_key, "
          "stage_form_value rides the shared command history in lockstep "
          "(replace/revert/undo/redo, census-derived dirty), "
          "render_with_edits ink equals the saved file's in and out of the "
          "field rects, an in-place text edit and a fill coexist through "
          "the redaction pass, rotate_page bakes fills into working, "
          "save_flattened writes a widget-free copy with ink parity while "
          "the open doc keeps its staged state, render_signature folds "
          "form values into the one registry, and extract/merge carry "
          "staged fills. M2: FieldHotspots materialize at Z_FORM_HOTSPOT "
          "only on form docs (zero chrome elsewhere), the badge + open "
          "toast track the AcroForm census, checkbox/radio/combo presses "
          "and FormFieldEditor commits are one FormFieldCommand each in "
          "Qt/model lockstep, undo/redo restore the on-screen pixels, the "
          "repainted field-rect ink equals the saved file's render, Esc "
          "cancels, flush commits half-typed values, multiline fields take "
          "newlines, and no modal ever runs. M3: real Tab/Backtab presses "
          "walk form_tab_order with cross-page wrap and the viewport "
          "following (text fields auto-mount their editor, zero phantom "
          "commands), a mid-edit Tab commits one command then moves on, "
          "Space/Return activate the focused button/combo, Esc drops the "
          "focus ring, form-free docs keep their normal Tab, and Export "
          "Flattened Copy (form-gated in the File menu) writes a "
          "widget-free copy with on-screen ink parity while the open doc "
          "keeps everything.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
