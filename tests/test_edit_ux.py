"""Text-editing UX suite (ws2_text_editing_ux). M1: inline editor feel +
edit-flow papercuts.

Asserts (spec ws2 §2 M1 test plan):

  T1: ``begin_edit`` with a ``scene_point`` mid-word places the caret within
      +-1 of the metrics ground truth -- on a SINGLE-LINE editor (cumulative
      QFontMetricsF advance) and on a wrapped PARAGRAPH editor's second visual
      line (QTextLine.cursorToX), i.e. the hitTest path is the one caret
      placement everywhere (§2.1).
  T2: ``_select_word_at`` selects exactly the word under the point and
      ``_select_line_at`` the visual (wrapped) line (double-/triple-click
      semantics, §2.2), driven through the test helpers without event
      synthesis.
  T3: Cmd+B as a QKeyEvent on an editor SELECTION -> commit -> ``staged_runs``
      carries a bold run beside a regular one (mirrors test_richtext); the
      window ``act_bold`` with a box selected (no editor) flips
      ``effective_style()["bold"]`` in exactly ONE undo step and undo restores
      it (§2.4).
  T4: edit -> undo -> ``document.dirty is False`` AND ``act_save`` disabled
      (the dirty-truth fix, §2.7c).
  T5: a DIRECT ``begin_edit`` (no prior select) populates the inspector's
      family combo -- no placeholder during a direct-entry edit (§2.6).
  T6: ``act_select_tool`` / ``act_text_edit_tool`` exist with the advertised
      plain V / E shortcuts and toggle the left strip's active tool; Cmd+B/I
      live on act_bold/act_italic (§2.7b / §2.4b).
  T7: ``commit_edit`` still stages the right text AND run payloads after the
      read-before-teardown reorder (§2.7a): mixed runs stage, a select-all
      un-bold clears them back to None, and a full retype lands verbatim.
  T8: ``is_edited`` consults the box's OWN page (§2.7d): a staged edit on
      page 1 reads edited while the viewport's current page is 0.

M2 (box manipulation feel, spec ws2 §3 test plan):

  T9:  arrow-key nudge: Key_Right moves ``effective_origin`` x by +1pt; FIVE
       rapid nudges grow ``undo_stack.count()`` by exactly 1 (id/mergeWith +
       ``coalesce_last_undo``) and ONE undo restores the original origin with
       ``document.can_undo`` mirroring the Qt stack (lockstep proof); Shift
       nudges step 10pt; on rotated_doc.pdf Key_Up moves the box SCREEN-up
       (the scene rect's top decreases) (§3.1).
  T10: ``_context_menu_for`` over a box returns exactly {Edit Text, Copy,
       Paste, Copy Style, Paste Style, Delete}; empty canvas returns
       {Paste, Add Text Here}; an open editor returns None; Copy Style on A
       then Paste Style on B copies family/size/color/bold in ONE undo step
       and undo restores B (§3.4).
  T11: a paste whose source origin sits at the page edge lands INSIDE the
       page rect minus the 18pt margin (§3.5).
  T12: a small box selects with ``overlay.compact`` True and exactly 4
       visible (6px) corner handles; a large paragraph box keeps all 8
       full-size handles (§3.3a).
  T13: arming a body move shows the ghost pixmap riding the cursor; a
       near-straight drag axis-snaps (lock + guide line); Esc aborts the
       live drag with NO command and restores the overlay (§3.2 / §2.5).

M3 (clipboard interop, spec ws2 §4 test plan):

  T14: editor select-all -> Copy puts BOTH ``text/plain`` and the
       x-pdfte-runs payload on the system clipboard; the JSON decodes to the
       staged styling (a bold "review" run beside a regular run) with the
       plain text equal to the joined run text and the style carrying the
       box's effective family/size (§4.1 / §4.2).
  T15: external plain text ("From Acme Corp\\nAgenda") pasted into the editor
       lands whitespace-normalized (\\n -> single space) with UNIFORM char
       formats; the commit stages the text with no rich runs (§4.3b).
  T16: a bold-run selection copied from box A's editor and pasted into box
       B's editor keeps bold/italic ONLY (B's family/size/color stay); the
       commit stages mixed runs on B (§4.3a).
  T17: with the in-process box clipboard empty, ``paste()`` falls back to the
       system clipboard: plain text -> ONE add command, NewBox text matches,
       origin inside the page; a runs-mime payload -> a styled NewBox
       carrying the payload's family/size/bold/color; an empty clipboard
       declines (§4.4 / §3.5).
  T18: Edit menu gains Cut/Copy/Paste at the edit_extra anchor with the
       standard shortcuts, routed by state: editor open -> editor copy/paste;
       box selected -> box copy / Cut = copy + ONE delete step (§4.5).

M4 (Text Select tool, spec ws2 §5 test plan):

  T19: ``page_words(0)`` on form_like.pdf returns reading-order-sorted
       WordBoxes whose joined text carries known fixture words; staging an
       edit that replaces a word makes ``page_words`` reflect the STAGED
       word (the old one gone -- the words come from the bake pipeline);
       ``document.undo()`` reverts, and the memo returns the identical
       cached tuple on a repeat call (cache invalidation proof, §5.1).
  T20: ``enter_select_text_mode`` + ``select_text_range`` over a known span
       -> ``text_selection_string()`` equals the expected join (space within
       a line, newline between lines); a Cmd+C QKeyEvent lands that text on
       the SYSTEM clipboard (§5.2).
  T21: a double-click synthesized at a word's scene center selects exactly
       that word; the third press within the double-click interval promotes
       to the whole LINE (§5.2).
  T22: rotated_doc.pdf: selecting a word on the /Rotate 90 page paints a
       highlight band that intersects the page's RENDERED ink (sampled from
       the materialized layer image -- the rotation_matrix mapping proof,
       §5.2).
  T23: chrome + teardown (§5.3): the left strip gains the Select Text tool
       (tooltip "Select Text (S)"), ``act_select_text_tool`` carries the
       plain S shortcut and arms the mode; act_copy routes to the word
       selection with the "N words copied" toast; the context menu offers
       exactly {Copy, Select All}; Esc clears then disarms; switching back
       to the Select tool removes every highlight item and re-enables
       hotspot presses.

Fixtures: existing synthetic set only (form_like.pdf, paragraphs.pdf,
two_page.pdf, rotated_doc.pdf -- fake neutral content). Never writes into
tests/fixtures/.

Run:
    QT_QPA_PLATFORM=offscreen \
      /Users/edward/Documents/GitHub/PDFTextEditor/.venv/bin/python \
      tests/test_edit_ux.py
"""

from __future__ import annotations

import os
import sys
import traceback

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from PySide6.QtCore import QByteArray, QEvent, QMimeData, QPointF, Qt  # noqa: E402
from PySide6.QtGui import (  # noqa: E402
    QFontMetricsF,
    QKeyEvent,
    QKeySequence,
    QMouseEvent,
    QTextCursor,
)
from PySide6.QtWidgets import QApplication  # noqa: E402

# A QApplication must exist before any QFont/QWidget is constructed.
_APP = QApplication.instance() or QApplication([])

from pdftexteditor.clipboard import (  # noqa: E402
    RUNS_MIME,
    decode_runs_mime,
    encode_runs_mime,
)
from pdftexteditor.document import PDFDocument  # noqa: E402
from pdftexteditor.ui.main_window import MainWindow  # noqa: E402

FIXTURE_DIR = os.path.join(_HERE, "fixtures")
FORM = os.path.join(FIXTURE_DIR, "form_like.pdf")
PARAGRAPHS = os.path.join(FIXTURE_DIR, "paragraphs.pdf")
TWO_PAGE = os.path.join(FIXTURE_DIR, "two_page.pdf")
ROTATED = os.path.join(FIXTURE_DIR, "rotated_doc.pdf")


def check(failures: list[str], tag: str, cond: bool, msg: str) -> bool:
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
    if getattr(w, "empty_state", None) is not None:
        w.empty_state.hide()
    pump(2)
    return w


def close_window(w: MainWindow) -> None:
    """Close WITHOUT the dirty-discard modal (blocks forever offscreen)."""
    w._suppress_close_guard = True
    w.close()


def hotspot_for(view, contains: str, paragraph: bool | None = None):
    """First current-page hotspot whose box text contains ``contains``."""
    for hs in view._hotspots:
        box = hs.box
        if paragraph is not None and \
                bool(getattr(box, "is_paragraph", False)) != paragraph:
            continue
        if contains in getattr(box, "text", ""):
            return hs
    return None


def cmd_key_event(key) -> QKeyEvent:
    return QKeyEvent(QKeyEvent.Type.KeyPress, key, Qt.ControlModifier)


def line_point_scene(editor, char_index: int) -> tuple[QPointF, object]:
    """The scene point at ``char_index`` (vertical mid-line) plus the QTextLine
    hosting it, computed from the editor's own block layout -- the geometry
    truth the caret hit-test must reproduce. Block-aware: a paragraph editor
    mirrors the page's hard line breaks, so ``char_index`` may fall in any
    block, not just the first."""
    doc = editor.document()
    block = doc.findBlock(char_index)
    layout = block.layout()
    local_idx = char_index - block.position()
    line = layout.lineForTextPosition(local_idx)
    x_local, _ = line.cursorToX(local_idx)
    origin = layout.position()
    local = QPointF(origin.x() + x_local,
                    origin.y() + line.y() + line.height() * 0.5)
    return editor.mapToScene(local), line


def char_on_visual_line(editor, line_no: int):
    """A representative character index on the ``line_no``-th (0-based) VISUAL
    line of the editor, counting wrapped lines across every block."""
    doc = editor.document()
    count = 0
    block = doc.begin()
    while block.isValid():
        bl = block.layout()
        for li in range(bl.lineCount()):
            if count == line_no:
                line = bl.lineAt(li)
                k = line.textStart() + min(6, max(1, line.textLength() - 1))
                return block.position() + k
            count += 1
        block = block.next()
    return None


def visual_line_count(editor) -> int:
    doc = editor.document()
    total = 0
    block = doc.begin()
    while block.isValid():
        total += block.layout().lineCount()
        block = block.next()
    return total


# ==========================================================================
# T1: caret precision through the hitTest path (single-line + paragraph)
# ==========================================================================
def test_t1_caret_precision(failures: list[str]) -> None:
    tag = "T1_caret_precision"
    # --- single-line Span editor: QFontMetricsF cumulative-advance truth ---
    w = open_window(FORM)
    try:
        v = w.view
        hs = hotspot_for(v, "Pending")
        if not check(failures, tag, hs is not None, "form fixture span missing"):
            return
        v.begin_edit(hs)
        ed = v._editor
        text = ed.toPlainText()
        metrics = QFontMetricsF(ed.font())
        k = 4                                    # mid "Pending"
        x = ed.pos().x() + metrics.horizontalAdvance(text[:k])
        y = ed.pos().y() + metrics.ascent() * 0.5
        v.cancel_edit()
        pump(2)
        v.begin_edit(hs, scene_point=QPointF(x, y))
        ed2 = v._editor
        got = ed2.textCursor().position()
        check(failures, tag, abs(got - k) <= 1,
              f"single-line caret {got} not within +-1 of {k}")
        check(failures, tag, not ed2.textCursor().hasSelection(),
              "point click must place a caret, not select-all")
        v.cancel_edit()
        pump(2)
    finally:
        close_window(w)

    # --- paragraph editor: second VISUAL line via the block layout truth ---
    w = open_window(PARAGRAPHS)
    try:
        v = w.view
        hs = hotspot_for(v, "quarterly", paragraph=True)
        if not check(failures, tag, hs is not None, "paragraph box missing"):
            return
        v.begin_edit(hs)
        ed = v._editor
        # The paragraph editor mirrors the page's hard line breaks (each visual
        # line is its own block), so count visual lines ACROSS blocks.
        nlines = visual_line_count(ed)
        if not check(failures, tag, nlines >= 2,
                     f"paragraph editor did not wrap ({nlines} visual lines)"):
            v.cancel_edit()
            return
        k = char_on_visual_line(ed, 1)          # a char on the 2nd visual line
        if not check(failures, tag, k is not None, "no 2nd visual line"):
            v.cancel_edit()
            return
        pt, _ = line_point_scene(ed, k)
        v.cancel_edit()
        pump(2)
        v.begin_edit(hs, scene_point=pt)
        got = v._editor.textCursor().position()
        check(failures, tag, abs(got - k) <= 1,
              f"paragraph line-2 caret {got} not within +-1 of {k}")
        v.cancel_edit()
        pump(2)
    finally:
        close_window(w)


# ==========================================================================
# T2: double-click=word / triple-click=line helpers
# ==========================================================================
def test_t2_word_line_selection(failures: list[str]) -> None:
    tag = "T2_word_line_selection"
    w = open_window(PARAGRAPHS)
    try:
        v = w.view
        hs = hotspot_for(v, "quarterly", paragraph=True)
        if not check(failures, tag, hs is not None, "paragraph box missing"):
            return
        v.begin_edit(hs)
        ed = v._editor
        text = ed.toPlainText()
        s = text.find("operations")
        if not check(failures, tag, s >= 0, "'operations' not in paragraph"):
            v.cancel_edit()
            return
        pt, line = line_point_scene(ed, s + 5)   # mid-word point
        ed._select_word_at(pt)
        sel = ed.textCursor().selectedText()
        check(failures, tag, sel == "operations",
              f"word selection {sel!r} != 'operations'")

        ed._select_line_at(pt)
        cur = ed.textCursor()
        sel_line = cur.selectedText()
        expected = text[line.textStart(): line.textStart()
                        + line.textLength()]
        check(failures, tag, "operations" in sel_line,
              f"line selection {sel_line!r} lost the word")
        check(failures, tag, sel_line.strip() == expected.strip(),
              f"line selection {sel_line!r} != visual line "
              f"{expected!r}")
        # The selection spans ONE visual line: both ends sit on line 0's range.
        layout = ed.document().firstBlock().layout()
        l_start = layout.lineForTextPosition(cur.selectionStart())
        end_pos = max(cur.selectionStart(), cur.selectionEnd() - 1)
        l_end = layout.lineForTextPosition(end_pos)
        check(failures, tag,
              l_start.lineNumber() == l_end.lineNumber() == line.lineNumber(),
              f"line selection crosses lines "
              f"({l_start.lineNumber()}..{l_end.lineNumber()})")
        v.cancel_edit()
        pump(2)
    finally:
        close_window(w)


# ==========================================================================
# T3: Cmd+B both paths (editor selection / whole selected box)
# ==========================================================================
def test_t3_bold_both_paths(failures: list[str]) -> None:
    tag = "T3_bold_paths"
    w = open_window(FORM)
    try:
        v = w.view
        hs = hotspot_for(v, "Pending")
        if not check(failures, tag, hs is not None, "form fixture span missing"):
            return
        box = hs.box

        # (a) editor path: select "review", Cmd+B QKeyEvent, commit.
        v.begin_edit(hs)
        ed = v._editor
        t = ed.toPlainText()
        s = t.lower().find("review")
        cur = ed.textCursor()
        cur.setPosition(s)
        cur.setPosition(s + 6, QTextCursor.KeepAnchor)
        ed.setTextCursor(cur)
        ed.keyPressEvent(cmd_key_event(Qt.Key_B))
        v.commit_edit()
        pump(4)
        runs = w.document.staged_runs(0, box)
        check(failures, tag, runs is not None
              and any(b for _, b, _ in runs)
              and any(not b for _, b, _ in runs),
              f"editor Cmd+B did not stage mixed runs: {runs}")
        w.undo_stack.undo()
        pump(2)
        check(failures, tag, w.document.staged_runs(0, box) is None,
              "undo did not clear the staged runs")

        # (b) window action path: box selected, NO editor -> one style step.
        hs2 = hotspot_for(v, "Cleared")
        if not check(failures, tag, hs2 is not None, "second span missing"):
            return
        box2 = hs2.box
        v.select_box(box2)
        pump(2)
        check(failures, tag, v._editor is None, "no editor expected")
        before = bool(w.document.effective_style(0, box2).get("bold"))
        idx0 = w.undo_stack.index()
        w.act_bold.trigger()
        pump(4)
        after = bool(w.document.effective_style(0, box2).get("bold"))
        check(failures, tag, after == (not before),
              f"act_bold did not flip bold ({before} -> {after})")
        # index() delta == pushed steps (count() can stay flat when a push
        # truncates a previously undone tail).
        check(failures, tag, w.undo_stack.index() == idx0 + 1,
              f"act_bold advanced the stack by "
              f"{w.undo_stack.index() - idx0} steps, not 1")
        w.undo_stack.undo()
        pump(2)
        restored = bool(w.document.effective_style(0, box2).get("bold"))
        check(failures, tag, restored == before,
              f"undo did not restore bold ({restored} != {before})")
        w.undo_stack.undo()    # drop nothing further: stack is at base now
        pump(2)
    finally:
        close_window(w)


# ==========================================================================
# T4: dirty truth -- Save disables after undoing back to the opened state
# ==========================================================================
def test_t4_dirty_after_undo(failures: list[str]) -> None:
    tag = "T4_dirty_after_undo"
    w = open_window(FORM)
    try:
        v = w.view
        hs = hotspot_for(v, "Pending")
        if not check(failures, tag, hs is not None, "form fixture span missing"):
            return
        check(failures, tag, w.document.dirty is False,
              "doc dirty right after open")
        # Charcoal redesign: Save is an always-live indigo CTA whenever a
        # document is open (it no longer greys out when clean); the clean state
        # is asserted via document.dirty above, not via the button.
        check(failures, tag, w.act_save.isEnabled(),
              "Save should be a live CTA with a document open")
        v.begin_edit(hs)
        cur = v._editor.textCursor()        # begin_edit selected all
        cur.insertText("Approved review")
        v.commit_edit()
        pump(4)
        check(failures, tag, w.document.dirty is True, "edit did not dirty")
        check(failures, tag, w.act_save.isEnabled(),
              "Save disabled with a staged edit")
        w.undo_stack.undo()
        pump(4)
        check(failures, tag, w.document.dirty is False,
              "document.dirty still True after undoing the only edit")
        check(failures, tag, w.act_save.isEnabled(),
              "Save stays a live CTA after undoing back to the opened state")
        w.undo_stack.redo()
        pump(2)
        check(failures, tag, w.document.dirty is True,
              "redo did not re-dirty")
        w.undo_stack.undo()
        pump(2)
    finally:
        close_window(w)


# ==========================================================================
# T4b: dirty truth vs the LAST SAVE -- undoing a saved edit reads dirty
# (final-review regression: mark_clean records the fine-grained baseline)
# ==========================================================================
def test_t4b_dirty_after_save_undo(failures: list[str]) -> None:
    tag = "T4b_dirty_after_save_undo"
    import shutil
    import tempfile
    tmp = os.path.join(tempfile.mkdtemp(prefix="t4b_"), "form_like.pdf")
    shutil.copy(FORM, tmp)
    w = open_window(tmp)
    try:
        v = w.view
        hs = hotspot_for(v, "Pending")
        if not check(failures, tag, hs is not None, "form fixture span missing"):
            return
        v.begin_edit(hs)
        cur = v._editor.textCursor()
        cur.insertText("Approved review")
        v.commit_edit()
        pump(4)
        check(failures, tag, w._do_save(tmp), "in-place save failed")
        pump(2)
        check(failures, tag, w.document.dirty is False,
              "doc not clean right after save")
        w.undo_stack.undo()
        pump(4)
        # The screen no longer matches the disk: the saved edit was undone.
        check(failures, tag, w.document.dirty is True,
              "doc reads clean after undoing a SAVED edit (screen != disk)")
        check(failures, tag, w.act_save.isEnabled(),
              "Save disabled after undoing a saved edit")
        # Redo brings the screen back to what the disk holds -> clean again.
        w.undo_stack.redo()
        pump(4)
        check(failures, tag, w.document.dirty is False,
              "redo back to the saved state still reads dirty")
        # Undo once more, then tab away and back: the undone-after-save
        # state must STAY dirty and saveable (no permanent screen/disk lie).
        w.undo_stack.undo()
        pump(4)
        w.open_path(TWO_PAGE)
        pump(4)
        w._on_tab_activated(0)
        pump(4)
        check(failures, tag, w.document.dirty is True,
              "dirty lost across a tab switch")
        check(failures, tag, w.act_save.isEnabled(),
              "Save disabled after switch-away/back (state unsaveable)")
    finally:
        close_window(w)


# ==========================================================================
# T4c: Cmd+S flushes the open inline editor -- the file holds what the
# screen shows (final-review regression: save_pdf/_save_pdf_as flush)
# ==========================================================================
def test_t4c_save_flushes_editor(failures: list[str]) -> None:
    tag = "T4c_save_flushes_editor"
    import shutil
    import tempfile
    tmp = os.path.join(tempfile.mkdtemp(prefix="t4c_"), "form_like.pdf")
    shutil.copy(FORM, tmp)
    w = open_window(tmp)
    try:
        v = w.view
        hs = hotspot_for(v, "Pending")
        if not check(failures, tag, hs is not None, "form fixture span missing"):
            return
        v.begin_edit(hs)
        cur = v._editor.textCursor()
        cur.movePosition(QTextCursor.End)
        v._editor.setTextCursor(cur)
        cur.insertText(" TYPEDLIVE")
        pump(2)
        # Cmd+S with the editor still open: the half-typed text must land.
        w.save_pdf()
        pump(3)
        import fitz
        on_disk = fitz.open(tmp)[0].get_text("text").replace("\xa0", " ")
        check(failures, tag, "TYPEDLIVE" in on_disk,
              "Cmd+S saved WITHOUT the typed text the screen shows")
        check(failures, tag, v._editor is None,
              "inline editor still open after save (flush did not commit)")
        check(failures, tag, w.document.dirty is False,
              "doc dirty after a save that included the flush")
    finally:
        close_window(w)


# ==========================================================================
# T5: inspector live during a direct-entry edit
# ==========================================================================
def test_t5_inspector_live(failures: list[str]) -> None:
    tag = "T5_inspector_live"
    w = open_window(FORM)
    try:
        v = w.view
        hs = hotspot_for(v, "Pending")
        if not check(failures, tag, hs is not None, "form fixture span missing"):
            return
        check(failures, tag, v.current_selection() is None,
              "selection unexpectedly set before the direct edit")
        v.begin_edit(hs)                 # DIRECT: no select_box first
        pump(2)
        target = getattr(w.inspector, "_target", None)
        check(failures, tag,
              target is not None and getattr(target, "identity", None)
              == hs.box.identity,
              "inspector not targeting the box under direct edit")
        family = w.inspector.family_combo.currentText()
        check(failures, tag, bool(family.strip()),
              "inspector family combo empty (placeholder) during edit")
        check(failures, tag, w.inspector.family_combo.isEnabled(),
              "inspector disabled during direct-entry edit")
        v.cancel_edit()
        pump(2)
    finally:
        close_window(w)


# ==========================================================================
# T6: V / E tool shortcuts + Cmd+B/I action registration
# ==========================================================================
def test_t6_tool_shortcuts(failures: list[str]) -> None:
    tag = "T6_tool_shortcuts"
    w = open_window(FORM)
    try:
        check(failures, tag,
              w.act_select_tool.shortcut() == QKeySequence("V"),
              f"Select Tool shortcut {w.act_select_tool.shortcut().toString()!r}")
        check(failures, tag,
              w.act_text_edit_tool.shortcut() == QKeySequence("E"),
              f"Text Edit Tool shortcut "
              f"{w.act_text_edit_tool.shortcut().toString()!r}")
        check(failures, tag,
              w.act_bold.shortcut() == QKeySequence(QKeySequence.Bold),
              f"Bold shortcut {w.act_bold.shortcut().toString()!r}")
        check(failures, tag,
              w.act_italic.shortcut() == QKeySequence(QKeySequence.Italic),
              f"Italic shortcut {w.act_italic.shortcut().toString()!r}")
        # Both live in the Tools menu (ws2 owns creation, above Add Text).
        tools = w.menu_tools.actions()
        check(failures, tag,
              w.act_select_tool in tools and w.act_text_edit_tool in tools,
              "V/E actions not housed in the Tools menu")
        check(failures, tag,
              tools.index(w.act_select_tool) < tools.index(w.act_add_text),
              "Select Tool not above Add Text in the Tools menu")
        # Bold/Italic live in the Edit menu.
        edit_acts = w.menu_edit.actions()
        check(failures, tag,
              w.act_bold in edit_acts and w.act_italic in edit_acts,
              "Bold/Italic not housed in the Edit menu")

        w.act_text_edit_tool.trigger()
        pump(2)
        check(failures, tag, w.left_panel.active_tool() == "text_edit",
              f"E did not arm text_edit (got {w.left_panel.active_tool()!r})")
        w.act_select_tool.trigger()
        pump(2)
        check(failures, tag, w.left_panel.active_tool() == "select",
              f"V did not arm select (got {w.left_panel.active_tool()!r})")

        # Guard: with the inline editor open, the plain keys must not act --
        # the editor stays mounted and the strip keeps showing the TEXT_EDIT
        # state the mode sync put it in when the editor opened.
        hs = hotspot_for(w.view, "Pending")
        w.view.begin_edit(hs)
        pump(2)
        tool_during_edit = w.left_panel.active_tool()
        w.act_select_tool.trigger()
        pump(2)
        check(failures, tag, w.view._editor is not None,
              "tool shortcut closed the editor mid-typing")
        check(failures, tag,
              w.left_panel.active_tool() == tool_during_edit,
              "tool shortcut changed the strip while the editor was typing")
        w.view.cancel_edit()
        pump(2)
    finally:
        close_window(w)


# ==========================================================================
# T7: commit_edit regression after the read-before-teardown reorder
# ==========================================================================
def test_t7_commit_teardown(failures: list[str]) -> None:
    tag = "T7_commit_teardown"
    w = open_window(FORM)
    try:
        v = w.view
        hs = hotspot_for(v, "Pending")
        if not check(failures, tag, hs is not None, "form fixture span missing"):
            return
        box = hs.box
        original = box.text

        # 1) Stage MIXED runs: bold one word via the per-selection path.
        v.begin_edit(hs)
        ed = v._editor
        t = ed.toPlainText()
        s = t.lower().find("review")
        cur = ed.textCursor()
        cur.setPosition(s)
        cur.setPosition(s + 6, QTextCursor.KeepAnchor)
        ed.setTextCursor(cur)
        v.apply_style_to_selection({"bold": True})
        v.commit_edit()
        pump(4)
        runs = w.document.staged_runs(0, box)
        check(failures, tag, runs is not None,
              "mixed-run commit staged nothing")

        # 2) Reopen, select-all UN-bold, commit: hits the uniform-again branch
        #    (the one that used to read editor state after teardown). Runs
        #    must clear back to None with the text intact.
        hs = hotspot_for(v, "Pending") or hs
        v.begin_edit(hs)
        ed = v._editor
        cur = ed.textCursor()
        cur.select(QTextCursor.Document)
        ed.setTextCursor(cur)
        v.apply_style_to_selection({"bold": False})
        v.commit_edit()
        pump(4)
        check(failures, tag, w.document.staged_runs(0, box) is None,
              f"uniform-again commit kept runs: "
              f"{w.document.staged_runs(0, box)}")
        # Compare nbsp-normalized: QTextDocument.toPlainText() maps U+00A0 to
        # a plain space (long-standing Qt behavior, identical ink in the bake),
        # so the round-tripped text differs only in that codepoint.
        check(failures, tag,
              w.document.staged_text(0, box).replace("\xa0", " ")
              == original.replace("\xa0", " "),
              f"uniform-again commit corrupted the text: "
              f"{w.document.staged_text(0, box)!r}")

        # 3) Full retype: the staged text lands verbatim.
        hs = hotspot_for(v, "Pending") or hs
        v.begin_edit(hs)
        cur = v._editor.textCursor()
        cur.select(QTextCursor.Document)
        cur.insertText("Approved figures")
        v.commit_edit()
        pump(4)
        check(failures, tag,
              w.document.staged_text(0, box) == "Approved figures",
              f"retype staged {w.document.staged_text(0, box)!r}")
        while w.undo_stack.canUndo():
            w.undo_stack.undo()
        pump(2)
    finally:
        close_window(w)


# ==========================================================================
# T8: is_edited consults the box's OWN page (cross-page fix)
# ==========================================================================
def test_t8_is_edited_cross_page(failures: list[str]) -> None:
    tag = "T8_is_edited_cross_page"
    w = open_window(TWO_PAGE)
    try:
        v = w.view
        doc = w.document
        spans1 = [s for s in doc.spans(1) if s.text.strip()]
        if not check(failures, tag, bool(spans1), "no spans on page 1"):
            return
        box = spans1[0]
        check(failures, tag, v.page_index == 0,
              f"expected current page 0, got {v.page_index}")
        check(failures, tag, v.is_edited(box) is False,
              "pristine page-1 span reads edited")
        doc.stage_edit(1, box, box.text + " amended")
        check(failures, tag, v.is_edited(box) is True,
              "staged page-1 edit invisible to is_edited while page 0 is "
              "current (the cross-page short-circuit)")
        doc.undo()
        check(failures, tag, v.is_edited(box) is False,
              "is_edited stuck True after the model undo")
    finally:
        close_window(w)


# ==========================================================================
# T9: arrow-key nudge with coalesced undo (lockstep proof) + rotated page
# ==========================================================================
def key_event(key, mods=Qt.NoModifier) -> QKeyEvent:
    return QKeyEvent(QKeyEvent.Type.KeyPress, key, mods)


def test_t9_nudge_coalesced(failures: list[str]) -> None:
    tag = "T9_nudge"
    w = open_window(FORM)
    try:
        v = w.view
        hs = hotspot_for(v, "Pending")
        if not check(failures, tag, hs is not None, "form fixture span missing"):
            return
        box = hs.box
        v.select_box(box)
        pump(2)
        orig = w.document.effective_origin(0, box)
        count0 = w.undo_stack.count()
        idx0 = w.undo_stack.index()

        # One nudge: +1.0 pt along x on an unrotated page.
        v.keyPressEvent(key_event(Qt.Key_Right))
        pump(2)
        ox1 = w.document.effective_origin(0, box)[0]
        check(failures, tag, abs(ox1 - (orig[0] + 1.0)) < 1e-4,
              f"Key_Right moved x by {ox1 - orig[0]:.4f} pt, not 1.0")

        # Four more rapid nudges: all five merge into ONE Qt command.
        for _ in range(4):
            v.keyPressEvent(key_event(Qt.Key_Right))
        pump(2)
        ox5 = w.document.effective_origin(0, box)[0]
        check(failures, tag, abs(ox5 - (orig[0] + 5.0)) < 1e-4,
              f"five nudges moved x by {ox5 - orig[0]:.4f} pt, not 5.0")
        check(failures, tag, w.undo_stack.count() == count0 + 1,
              f"five nudges grew the stack by "
              f"{w.undo_stack.count() - count0} steps, not 1")
        check(failures, tag,
              w.document.can_undo == w.undo_stack.canUndo(),
              "model/Qt can_undo diverged after the merge")

        # ONE undo restores the ORIGINAL origin -- the model history fused in
        # lockstep, so the single Qt step wraps a single model command.
        w.undo_stack.undo()
        pump(2)
        back = w.document.effective_origin(0, box)
        check(failures, tag,
              abs(back[0] - orig[0]) < 1e-4 and abs(back[1] - orig[1]) < 1e-4,
              f"one undo left origin at {back}, expected {orig}")
        check(failures, tag,
              w.document.can_undo == w.undo_stack.canUndo(),
              "model/Qt can_undo diverged after the undo")
        check(failures, tag, w.undo_stack.index() == idx0,
              "one undo did not return the stack index to its baseline "
              "(the merged step is more than one index deep)")

        # Shift = the coarse 10 pt step.
        v.keyPressEvent(key_event(Qt.Key_Down, Qt.ShiftModifier))
        pump(2)
        oy = w.document.effective_origin(0, box)[1]
        check(failures, tag, abs(oy - (orig[1] + 10.0)) < 1e-4,
              f"Shift+Down moved y by {oy - orig[1]:.4f} pt, not 10.0")
        w.undo_stack.undo()
        pump(2)
    finally:
        close_window(w)

    # Rotated page: Key_Up must move the box SCREEN-up (display-space top
    # decreases), proving the nudge rides the same inverse mapping as the
    # move drag.
    w = open_window(ROTATED)
    try:
        v = w.view
        hs = hotspot_for(v, "ROTATED")
        if not check(failures, tag, hs is not None, "rotated fixture span missing"):
            return
        v.select_box(hs.box)
        pump(2)
        z = v.zoom
        top0 = v._span_scene_rect(v.current_selection()).top()
        v.keyPressEvent(key_event(Qt.Key_Up))
        pump(2)
        top1 = v._span_scene_rect(v.current_selection()).top()
        check(failures, tag, 0.3 * z < (top0 - top1) < 1.7 * z,
              f"rotated Key_Up moved scene top by {top0 - top1:.2f} px "
              f"(expected ~{z:.2f} up)")
        w.undo_stack.undo()
        pump(2)
    finally:
        close_window(w)


# ==========================================================================
# T10: context-menu factory + Copy/Paste Style
# ==========================================================================
def menu_titles(menu) -> list[str]:
    return [a.text() for a in menu.actions() if not a.isSeparator()]


def test_t10_context_menu(failures: list[str]) -> None:
    tag = "T10_context_menu"
    w = open_window(FORM)
    try:
        v = w.view
        hs = hotspot_for(v, "Pending")
        if not check(failures, tag, hs is not None, "form fixture span missing"):
            return

        # (ii) over a box: the six titles, exactly, in order; the box selects.
        center = v._span_scene_rect(hs.box).center()
        menu = v._context_menu_for(center)
        if not check(failures, tag, menu is not None, "no menu over a box"):
            return
        titles = menu_titles(menu)
        check(failures, tag,
              titles == ["Edit Text", "Copy", "Paste", "Copy Style",
                         "Paste Style", "Delete"],
              f"box menu titles {titles}")
        sel = v.current_selection()
        check(failures, tag,
              sel is not None and sel.identity == hs.box.identity,
              "right-click did not select the box under the cursor")
        # Paste/Paste Style start disabled (nothing copied yet).
        by_title = {a.text(): a for a in menu.actions() if not a.isSeparator()}
        check(failures, tag, not by_title["Paste"].isEnabled(),
              "Paste enabled with an empty clipboard")
        check(failures, tag, not by_title["Paste Style"].isEnabled(),
              "Paste Style enabled with an empty style clipboard")

        # (iii) empty canvas (the gutter): Paste + Add Text Here.
        v.clear_selection()
        empty_menu = v._context_menu_for(QPointF(1.0, 1.0))
        if check(failures, tag, empty_menu is not None, "no empty-canvas menu"):
            check(failures, tag,
                  menu_titles(empty_menu) == ["Paste", "Add Text Here"],
                  f"empty-canvas titles {menu_titles(empty_menu)}")

        # (i) editor open -> None (Qt's native editor menu).
        v.begin_edit(hs)
        pump(2)
        check(failures, tag, v._context_menu_for(center) is None,
              "factory built a menu while the inline editor is open")
        v.cancel_edit()
        pump(2)

        # Copy Style on A ("Sample Report", Helvetica-Bold 26) -> Paste Style
        # on B ("Pending review", Arial 13): one undo step, B matches A,
        # undo restores B.
        hs_a = hotspot_for(v, "Sample Report")
        hs_b = hotspot_for(v, "Pending")
        if not check(failures, tag, hs_a is not None and hs_b is not None,
                     "style source/target spans missing"):
            return
        v.select_box(hs_a.box)
        pump(2)
        check(failures, tag, v.copy_style(), "copy_style declined")
        clip = dict(v._style_clipboard or {})
        b_before = w.document.effective_style(0, hs_b.box)
        check(failures, tag,
              clip.get("size") != b_before.get("size")
              or clip.get("font_family") != b_before.get("font_family"),
              "fixture regression: A and B styles already identical")
        v.select_box(hs_b.box)
        pump(2)
        idx0 = w.undo_stack.index()
        check(failures, tag, v.paste_style(), "paste_style declined")
        pump(4)
        b_after = w.document.effective_style(0, hs_b.box)
        check(failures, tag,
              b_after.get("font_family") == clip.get("font_family"),
              f"family {b_after.get('font_family')!r} != "
              f"{clip.get('font_family')!r}")
        check(failures, tag,
              abs(float(b_after.get("size", 0)) - clip["size"]) < 0.01,
              f"size {b_after.get('size')} != {clip['size']}")
        check(failures, tag,
              all(abs(x - y) < 0.01 for x, y in
                  zip(tuple(b_after.get("color", ())), clip["color"])),
              f"color {b_after.get('color')} != {clip['color']}")
        check(failures, tag,
              bool(b_after.get("bold")) == clip["bold"],
              f"bold {b_after.get('bold')} != {clip['bold']}")
        check(failures, tag, w.undo_stack.index() == idx0 + 1,
              f"Paste Style took {w.undo_stack.index() - idx0} steps, not 1")
        w.undo_stack.undo()
        pump(2)
        b_restored = w.document.effective_style(0, hs_b.box)
        check(failures, tag,
              b_restored.get("font_family") == b_before.get("font_family")
              and abs(float(b_restored.get("size", 0))
                      - float(b_before.get("size", 0))) < 0.01,
              f"undo left B at {b_restored}")
    finally:
        close_window(w)


# ==========================================================================
# T11: paste origin clamps into the page rect (minus the 18pt margin)
# ==========================================================================
def test_t11_paste_clamp(failures: list[str]) -> None:
    tag = "T11_paste_clamp"
    w = open_window(FORM)
    try:
        v = w.view
        doc = w.document
        hs = hotspot_for(v, "Pending")
        if not check(failures, tag, hs is not None, "form fixture span missing"):
            return
        v.select_box(hs.box)
        pump(2)
        check(failures, tag, v.copy_selection(), "copy_selection declined")
        rect = doc.working[0].rect
        # Source origin AT the bottom-right page corner: the +12,+14 paste
        # offset pushes it past the edge; the clamp must pull it back in.
        v._clipboard["origin"] = (rect.width - 2.0, rect.height - 2.0)
        n0 = len(doc.new_boxes(0))
        check(failures, tag, v.paste(), "paste declined")
        pump(4)
        v._flush_editor()       # paste drops into text edit; commit unchanged
        pump(2)
        news = doc.new_boxes(0)
        if not check(failures, tag, len(news) == n0 + 1,
                     f"paste added {len(news) - n0} boxes"):
            return
        ox, oy = news[-1].origin
        check(failures, tag,
              18.0 - 1e-6 <= ox <= rect.width - 18.0 + 1e-6,
              f"pasted origin x {ox:.1f} outside [18, {rect.width - 18:.1f}]")
        check(failures, tag,
              18.0 - 1e-6 <= oy <= rect.height - 18.0 + 1e-6,
              f"pasted origin y {oy:.1f} outside [18, {rect.height - 18:.1f}]")
        while w.undo_stack.canUndo():
            w.undo_stack.undo()
        pump(2)
    finally:
        close_window(w)


# ==========================================================================
# T12: compact resize handles on a small box; all 8 on a large one
# ==========================================================================
def visible_handles(view) -> list:
    return [h for h in view._overlay._handles if h.isVisible()]


def test_t12_compact_handles(failures: list[str]) -> None:
    tag = "T12_compact_handles"
    w = open_window(FORM)
    try:
        v = w.view
        hs = hotspot_for(v, "Pending")
        if not check(failures, tag, hs is not None, "form fixture span missing"):
            return
        v.select_box(hs.box)
        pump(2)
        rect = v._span_scene_rect(hs.box)
        check(failures, tag, rect.height() < 28.0,
              f"fixture span unexpectedly tall ({rect.height():.1f} px)")
        check(failures, tag, v._overlay.compact is True,
              "small box did not enter compact handle mode")
        vis = visible_handles(v)
        check(failures, tag, len(vis) == 4,
              f"compact box shows {len(vis)} handles, not 4")
        check(failures, tag,
              all(h.handle_id in ("nw", "ne", "se", "sw") for h in vis),
              "compact mode shows non-corner handles")
        check(failures, tag,
              all(abs(h.rect().width() - 6.0) < 0.01 for h in vis),
              "compact handles are not 6px")
        # Compact handles sit OUTSIDE the box rect (offset past the outline).
        nw = next(h for h in vis if h.handle_id == "nw")
        check(failures, tag,
              nw.pos().x() < rect.left() and nw.pos().y() < rect.top(),
              "compact NW handle not offset outside the box corner")
    finally:
        close_window(w)

    w = open_window(PARAGRAPHS)
    try:
        v = w.view
        hs = hotspot_for(v, "quarterly", paragraph=True)
        if not check(failures, tag, hs is not None, "paragraph box missing"):
            return
        v.select_box(hs.box)
        pump(2)
        check(failures, tag, v._overlay.compact is False,
              "large paragraph box wrongly compact")
        vis = visible_handles(v)
        check(failures, tag, len(vis) == 8,
              f"large box shows {len(vis)} handles, not 8")
        check(failures, tag,
              all(abs(h.rect().width() - 11.0) < 0.01 for h in vis),
              "large-box handles are not full size")
    finally:
        close_window(w)


# ==========================================================================
# T13: ghost-pixmap move preview, axis snap, Esc-abort
# ==========================================================================
def test_t13_ghost_drag_esc(failures: list[str]) -> None:
    tag = "T13_ghost_drag"
    w = open_window(FORM)
    try:
        v = w.view
        hs = hotspot_for(v, "Pending")
        if not check(failures, tag, hs is not None, "form fixture span missing"):
            return
        box = hs.box
        orig = w.document.effective_origin(0, box)
        count0 = w.undo_stack.count()
        start_rect = v._span_scene_rect(box)
        center = start_rect.center()

        # Press on the hotspot arms a move; the first real motion shows the
        # ghost (the box's own pixels) at the dragged offset.
        v._on_box_press(hs, center)
        v._update_drag(center + QPointF(40.0, 25.0))
        check(failures, tag, v._drag_armed, "drag did not arm past the slop")
        if check(failures, tag, v._drag_ghost is not None,
                 "no ghost pixmap item during the move drag"):
            check(failures, tag,
                  abs(v._drag_ghost.opacity() - 0.65) < 1e-6,
                  f"ghost opacity {v._drag_ghost.opacity()}")
            gp = v._drag_ghost.pos()
            check(failures, tag,
                  abs(gp.x() - (start_rect.left() + 40.0)) < 0.5
                  and abs(gp.y() - (start_rect.top() + 25.0)) < 0.5,
                  f"ghost at ({gp.x():.1f},{gp.y():.1f}), expected start "
                  f"+(40,25)")
            check(failures, tag, not v._drag_ghost.pixmap().isNull(),
                  "ghost pixmap is empty")
        check(failures, tag, v._axis_lock is None,
              "diagonal drag wrongly axis-locked")

        # A near-straight horizontal drag snaps dy to 0 and shows the guide.
        v._update_drag(center + QPointF(60.0, 0.3))
        check(failures, tag, v._axis_lock == "x",
              f"near-straight drag lock {v._axis_lock!r}, expected 'x'")
        check(failures, tag, v._axis_guide is not None,
              "axis lock has no guide line")
        if v._drag_ghost is not None:
            check(failures, tag,
                  abs(v._drag_ghost.pos().y() - start_rect.top()) < 0.01,
                  "axis snap did not zero the ghost's dy")

        # Esc aborts: every preview affordance gone, NO command, origin
        # untouched, overlay back on the box's true rect.
        v.keyPressEvent(key_event(Qt.Key_Escape))
        pump(2)
        check(failures, tag, v._drag_kind is None, "Esc left the drag live")
        check(failures, tag, v._drag_ghost is None, "Esc left the ghost")
        check(failures, tag, v._drag_preview is None, "Esc left the outline")
        check(failures, tag, v._axis_guide is None, "Esc left the guide")
        check(failures, tag, w.undo_stack.count() == count0,
              "Esc-aborted drag pushed a command")
        after = w.document.effective_origin(0, box)
        check(failures, tag,
              abs(after[0] - orig[0]) < 1e-6 and abs(after[1] - orig[1]) < 1e-6,
              f"Esc-aborted drag moved the box to {after}")
        if v._overlay is not None:
            orect = v._overlay._rect
            check(failures, tag,
                  abs(orect.left() - start_rect.left()) < 0.5
                  and abs(orect.top() - start_rect.top()) < 0.5,
                  "overlay not restored to the box rect after Esc")
        # Selection survives the abort.
        sel = v.current_selection()
        check(failures, tag,
              sel is not None and sel.identity == box.identity,
              "Esc-abort dropped the selection")
    finally:
        close_window(w)


# ==========================================================================
# T14: editor Copy writes text/plain + the x-pdfte-runs payload
# ==========================================================================
def norm(text: str) -> str:
    """nbsp-normalize for comparisons: QTextDocument.toPlainText() maps
    U+00A0 to a plain space while fragment/clipboard text keeps it."""
    return text.replace("\xa0", " ")


def select_all(editor) -> None:
    cur = editor.textCursor()
    cur.select(QTextCursor.Document)
    editor.setTextCursor(cur)


def bold_word(view, editor, word: str) -> bool:
    """Select ``word`` in the open editor and bold it via the per-run path."""
    t = editor.toPlainText()
    s = t.lower().find(word)
    if s < 0:
        return False
    cur = editor.textCursor()
    cur.setPosition(s)
    cur.setPosition(s + len(word), QTextCursor.KeepAnchor)
    editor.setTextCursor(cur)
    return view.apply_style_to_selection({"bold": True})


def test_t14_editor_copy_formats(failures: list[str]) -> None:
    tag = "T14_editor_copy"
    w = open_window(FORM)
    try:
        v = w.view
        hs = hotspot_for(v, "Pending")
        if not check(failures, tag, hs is not None, "form fixture span missing"):
            return
        v.begin_edit(hs)
        ed = v._editor
        if not check(failures, tag, bold_word(v, ed, "review"),
                     "could not bold 'review' in the editor"):
            v.cancel_edit()
            return
        editor_text = ed.toPlainText()
        select_all(ed)
        ed.keyPressEvent(cmd_key_event(Qt.Key_C))
        md = QApplication.clipboard().mimeData()
        if not check(failures, tag, md is not None, "clipboard empty after copy"):
            v.cancel_edit()
            return
        check(failures, tag, md.hasText(), "no text/plain beside the runs mime")
        if not check(failures, tag, md.hasFormat(RUNS_MIME),
                     "runs mime missing from the editor copy"):
            v.cancel_edit()
            return
        dec = decode_runs_mime(bytes(md.data(RUNS_MIME)))
        if not check(failures, tag, dec is not None, "runs payload undecodable"):
            v.cancel_edit()
            return
        check(failures, tag, norm(dec["text"]) == norm(editor_text),
              f"payload text {dec['text']!r} != editor text {editor_text!r}")
        check(failures, tag, norm(md.text()) == norm(dec["text"]),
              "plain text and runs payload disagree")
        check(failures, tag,
              "".join(t for t, _, _ in dec["runs"]) == dec["text"],
              "payload text is not the joined run text")
        check(failures, tag,
              any(b and t.strip() == "review" for t, b, _ in dec["runs"]),
              f"no bold 'review' run in {dec['runs']}")
        check(failures, tag, any(not b for _, b, _ in dec["runs"]),
              f"no regular run beside the bold one in {dec['runs']}")
        eff = w.document.effective_style(0, hs.box)
        check(failures, tag,
              dec["style"]["family"] == eff.get("font_family"),
              f"payload family {dec['style']['family']!r} != "
              f"{eff.get('font_family')!r}")
        check(failures, tag,
              abs(dec["style"]["size"] - float(eff.get("size", 0))) < 0.01,
              f"payload size {dec['style']['size']} != {eff.get('size')}")
        v.cancel_edit()
        pump(2)
    finally:
        close_window(w)


# ==========================================================================
# T15: external plain paste into the editor is whitespace-normalized
# ==========================================================================
def test_t15_sanitized_paste(failures: list[str]) -> None:
    tag = "T15_sanitized_paste"
    w = open_window(FORM)
    try:
        v = w.view
        hs = hotspot_for(v, "Pending")
        if not check(failures, tag, hs is not None, "form fixture span missing"):
            return
        QApplication.clipboard().setText("From Acme Corp\nAgenda")
        v.begin_edit(hs)
        ed = v._editor
        select_all(ed)
        ed.keyPressEvent(cmd_key_event(Qt.Key_V))
        check(failures, tag, ed.toPlainText() == "From Acme Corp Agenda",
              f"pasted text {ed.toPlainText()!r} not \\n-normalized")
        runs = v._extract_editor_runs(ed)
        base = ed.font()
        check(failures, tag,
              all((b, i) == (base.bold(), base.italic()) for _, b, i in runs),
              f"external paste produced non-uniform formats: {runs}")
        v.commit_edit()
        pump(4)
        check(failures, tag,
              w.document.staged_text(0, hs.box) == "From Acme Corp Agenda",
              f"staged {w.document.staged_text(0, hs.box)!r}")
        check(failures, tag, w.document.staged_runs(0, hs.box) is None,
              "uniform external paste staged rich runs")
        while w.undo_stack.canUndo():
            w.undo_stack.undo()
        pump(2)
    finally:
        close_window(w)


# ==========================================================================
# T16: bold runs survive an editor-to-editor paste across boxes
# ==========================================================================
def test_t16_cross_box_runs(failures: list[str]) -> None:
    tag = "T16_cross_box_runs"
    w = open_window(FORM)
    try:
        v = w.view
        hs_a = hotspot_for(v, "Pending")
        hs_b = hotspot_for(v, "Cleared")
        if not check(failures, tag, hs_a is not None and hs_b is not None,
                     "fixture spans missing"):
            return

        # Copy "Pending **review**" out of A's editor (no commit needed).
        v.begin_edit(hs_a)
        ed = v._editor
        if not check(failures, tag, bold_word(v, ed, "review"),
                     "could not bold 'review' in A"):
            v.cancel_edit()
            return
        select_all(ed)
        ed.keyPressEvent(cmd_key_event(Qt.Key_C))
        a_text = ed.toPlainText()
        v.cancel_edit()
        pump(2)

        # Paste into B: bold/italic apply, B's own face/size stay.
        v.begin_edit(hs_b)
        ed_b = v._editor
        b_font_family = ed_b.font().family()
        select_all(ed_b)
        ed_b.keyPressEvent(cmd_key_event(Qt.Key_V))
        check(failures, tag, norm(ed_b.toPlainText()) == norm(a_text),
              f"B editor text {ed_b.toPlainText()!r} != copied {a_text!r}")
        check(failures, tag, ed_b.font().family() == b_font_family,
              "paste changed B's base font family (style-sanity rule)")
        v.commit_edit()
        pump(4)
        runs = w.document.staged_runs(0, hs_b.box)
        if check(failures, tag, runs is not None,
                 "cross-box paste staged no rich runs"):
            check(failures, tag,
                  any(b and t.strip() == "review" for t, b, _ in runs),
                  f"bold 'review' run lost across boxes: {runs}")
            check(failures, tag, any(not b for _, b, _ in runs),
                  f"regular run lost across boxes: {runs}")
        check(failures, tag,
              norm(w.document.staged_text(0, hs_b.box)) == norm(a_text),
              f"B staged {w.document.staged_text(0, hs_b.box)!r}")
        while w.undo_stack.canUndo():
            w.undo_stack.undo()
        pump(2)
    finally:
        close_window(w)


# ==========================================================================
# T17: canvas paste falls back to the system clipboard (plain + styled)
# ==========================================================================
def test_t17_system_canvas_paste(failures: list[str]) -> None:
    tag = "T17_system_paste"
    w = open_window(FORM)
    try:
        v = w.view
        doc = w.document
        cb = QApplication.clipboard()
        rect = doc.working[0].rect

        # Empty everything -> paste declines.
        v._clipboard = {}
        cb.clear()
        check(failures, tag, v.paste() is False,
              "paste succeeded with empty clipboards")

        # Plain system text -> ONE add command, text + in-page origin.
        cb.setText("Jordan Carter")
        check(failures, tag, v._can_paste(), "_can_paste False with text")
        idx0 = w.undo_stack.index()
        n0 = len(doc.new_boxes(0))
        check(failures, tag, v.paste() is True, "plain system paste declined")
        pump(4)
        v._flush_editor()          # paste drops into text edit; no text change
        pump(2)
        news = doc.new_boxes(0)
        if not check(failures, tag, len(news) == n0 + 1,
                     f"plain paste added {len(news) - n0} boxes"):
            return
        check(failures, tag, w.undo_stack.index() == idx0 + 1,
              f"plain paste took {w.undo_stack.index() - idx0} steps, not 1")
        nb = news[-1]
        check(failures, tag, nb.text == "Jordan Carter",
              f"NewBox text {nb.text!r}")
        check(failures, tag,
              18.0 - 1e-6 <= nb.origin[0] <= rect.width - 18.0 + 1e-6
              and 18.0 - 1e-6 <= nb.origin[1] <= rect.height - 18.0 + 1e-6,
              f"pasted origin {nb.origin} outside the page margins")

        # Runs-mime payload -> a styled NewBox (full style: it is a NEW box).
        md = QMimeData()
        md.setText("Riley Morgan")
        md.setData(RUNS_MIME, QByteArray(encode_runs_mime(
            "Riley Morgan", [("Riley Morgan", True, False)],
            {"family": "Courier", "size": 16.0, "color": (0.8, 0.1, 0.1),
             "bold": True, "italic": False})))
        cb.setMimeData(md)
        check(failures, tag, v.paste() is True, "styled system paste declined")
        pump(4)
        v._flush_editor()
        pump(2)
        nb = doc.new_boxes(0)[-1]
        check(failures, tag, nb.text == "Riley Morgan",
              f"styled NewBox text {nb.text!r}")
        check(failures, tag, nb.font_family == "Courier",
              f"styled NewBox family {nb.font_family!r}")
        check(failures, tag, abs(nb.size - 16.0) < 0.01,
              f"styled NewBox size {nb.size}")
        check(failures, tag, nb.bold is True, "styled NewBox not bold")
        check(failures, tag,
              all(abs(c - e) < 0.01 for c, e in zip(nb.color, (0.8, 0.1, 0.1))),
              f"styled NewBox color {nb.color}")
        while w.undo_stack.canUndo():
            w.undo_stack.undo()
        pump(2)
    finally:
        close_window(w)


# ==========================================================================
# T18: Edit menu Cut/Copy/Paste -- placement, shortcuts, state routing
# ==========================================================================
def test_t18_edit_menu_clipboard(failures: list[str]) -> None:
    tag = "T18_edit_menu"
    w = open_window(FORM)
    try:
        v = w.view
        cb = QApplication.clipboard()
        for act, std in ((w.act_cut, QKeySequence.Cut),
                         (w.act_copy, QKeySequence.Copy),
                         (w.act_paste, QKeySequence.Paste)):
            check(failures, tag, act.shortcut() == QKeySequence(std),
                  f"{act.text()} shortcut {act.shortcut().toString()!r}")
            check(failures, tag, act.isEnabled(),
                  f"{act.text()} disabled with a document open")
        edit_acts = w.menu_edit.actions()
        anchor = w.menu_anchors["edit_extra"]
        for act in (w.act_cut, w.act_copy, w.act_paste):
            if check(failures, tag, act in edit_acts,
                     f"{act.text()} not in the Edit menu"):
                check(failures, tag,
                      edit_acts.index(act) < edit_acts.index(anchor),
                      f"{act.text()} not inserted at the edit_extra anchor")

        # Editor open: Copy/Paste route to the editor selection.
        hs = hotspot_for(v, "Pending")
        if not check(failures, tag, hs is not None, "form fixture span missing"):
            return
        v.begin_edit(hs)
        ed = v._editor
        editor_text = ed.toPlainText()      # begin_edit selected all
        w.act_copy.trigger()
        pump(2)
        check(failures, tag, norm(cb.text()) == norm(editor_text),
              f"act_copy with an editor open copied {cb.text()!r}")
        cb.setText("Acme Note")
        w.act_paste.trigger()
        pump(2)
        check(failures, tag, "Acme Note" in ed.toPlainText(),
              f"act_paste did not reach the editor ({ed.toPlainText()!r})")
        v.cancel_edit()
        pump(2)

        # No editor, box selected: Cut = copy + ONE delete step.
        hs2 = hotspot_for(v, "Cleared")
        if not check(failures, tag, hs2 is not None, "second span missing"):
            return
        box_text = hs2.box.text
        v.select_box(hs2.box)
        pump(2)
        idx0 = w.undo_stack.index()
        w.act_cut.trigger()
        pump(4)
        check(failures, tag, w.undo_stack.index() == idx0 + 1,
              f"Cut took {w.undo_stack.index() - idx0} steps, not 1")
        check(failures, tag, norm(cb.text()) == norm(box_text),
              f"Cut clipboard {cb.text()!r} != {box_text!r}")
        check(failures, tag,
              cb.mimeData() is not None and cb.mimeData().hasFormat(RUNS_MIME),
              "Cut did not attach the runs payload")
        check(failures, tag, v.current_selection() is None,
              "Cut left the deleted box selected")
        w.undo_stack.undo()
        pump(2)
    finally:
        close_window(w)


# ==========================================================================
# M4 helpers: viewport-level mouse synthesis for the text-select tool
# ==========================================================================
def send_mouse(view, scene_pt: QPointF, etype,
               button=Qt.LeftButton, buttons=Qt.LeftButton) -> None:
    """Dispatch one mouse event to the viewport at a scene point, exactly as
    a real click would arrive (the tests/test_reflow.py pattern)."""
    vp = view.viewport()
    view_pt = view.mapFromScene(scene_pt)
    gp = vp.mapToGlobal(view_pt)
    ev = QMouseEvent(etype, QPointF(view_pt), QPointF(gp),
                     button, buttons, Qt.NoModifier)
    _APP.sendEvent(vp, ev)


def click_word(view, scene_pt: QPointF, *, double: bool = False) -> None:
    """A full press/release pair (plus the double-click follow-up when
    requested) at a scene point."""
    send_mouse(view, scene_pt, QEvent.MouseButtonPress)
    send_mouse(view, scene_pt, QEvent.MouseButtonRelease,
               buttons=Qt.NoButton)
    if double:
        send_mouse(view, scene_pt, QEvent.MouseButtonDblClick)
        send_mouse(view, scene_pt, QEvent.MouseButtonRelease,
                   buttons=Qt.NoButton)
    pump(2)


def word_index(words, text: str) -> int:
    for i, wb in enumerate(words):
        if wb.text == text:
            return i
    return -1


# ==========================================================================
# T19: page_words -- bake-aware, sorted, cache invalidates through undo
# ==========================================================================
def test_t19_page_words(failures: list[str]) -> None:
    tag = "T19_page_words"
    doc = PDFDocument(FORM)
    words = doc.page_words(0)
    if not check(failures, tag, len(words) > 0, "no words extracted"):
        return
    keys = [(wb.block, wb.line, wb.word) for wb in words]
    check(failures, tag, keys == sorted(keys),
          "page_words not sorted by (block, line, word)")
    joined = " ".join(wb.text for wb in words)
    for known in ("Sample", "Report", "Pending", "review", "Cleared"):
        check(failures, tag, known in joined,
              f"fixture word {known!r} missing from page_words")

    # Stage an edit replacing a word: page_words must show the STAGED word
    # (the bake pipeline, not the pristine page).
    target = None
    for b in doc.spans(0):
        if "Pending" in getattr(b, "text", ""):
            target = b
            break
    if not check(failures, tag, target is not None,
                 "'Pending review' span missing"):
        return
    doc.stage_edit(0, target, target.text.replace("Pending", "Zephyr"))
    edited = doc.page_words(0)
    joined_e = " ".join(wb.text for wb in edited)
    check(failures, tag, "Zephyr" in joined_e,
          "staged word missing from page_words")
    check(failures, tag, "Pending" not in joined_e,
          "replaced word still present in page_words")

    # Undo reverts -- the per-page memo entry must have been dropped by the
    # undo's _install_state pass (cache invalidation proof).
    doc.undo()
    reverted = doc.page_words(0)
    joined_r = " ".join(wb.text for wb in reverted)
    check(failures, tag, "Pending" in joined_r and "Zephyr" not in joined_r,
          "undo did not revert page_words")
    check(failures, tag, doc.page_words(0) is reverted,
          "repeat call did not serve the cached tuple")


# ==========================================================================
# T20: select_text_range / text_selection_string / Cmd+C -> system clipboard
# ==========================================================================
def test_t20_select_and_copy(failures: list[str]) -> None:
    tag = "T20_select_copy"
    w = open_window(FORM)
    try:
        v = w.view
        v.enter_select_text_mode()
        pump(2)
        check(failures, tag, v.current_mode() == "select_text",
              f"mode {v.current_mode()!r} after enter_select_text_mode")
        words = w.document.page_words(0)
        i_pending = word_index(words, "Pending")
        i_review = word_index(words, "review")
        if not check(failures, tag, 0 <= i_pending < i_review,
                     "'Pending review' words missing"):
            return
        v.select_text_range(0, i_pending, i_review)
        check(failures, tag,
              v.text_selection_string() == "Pending review",
              f"single-line join {v.text_selection_string()!r}")
        check(failures, tag, len(v._text_sel_items) == 1,
              f"{len(v._text_sel_items)} highlight bands for one line")

        # A range spanning the title + subtitle lines joins with a newline
        # between lines and single spaces within them.
        i_first = word_index(words, "Sample")
        i_last = word_index(words, "data)")
        if check(failures, tag, 0 <= i_first < i_last,
                 "title/subtitle words missing"):
            v.select_text_range(0, i_first, i_last)
            expected = ("Sample Report\n"
                        "Synthetic verification record (no real data)")
            check(failures, tag, v.text_selection_string() == expected,
                  f"multi-line join {v.text_selection_string()!r}")
            check(failures, tag, len(v._text_sel_items) >= 2,
                  "multi-line selection painted fewer than 2 bands")
            for it in v._text_sel_items:
                check(failures, tag, abs(it.zValue() - 2.0) < 1e-9,
                      f"highlight z {it.zValue()}, expected Z_TEXT_SELECT=2")

        # Cmd+C as a QKeyEvent through the view's mode-key handler lands the
        # join on the SYSTEM clipboard.
        cb = QApplication.clipboard()
        cb.clear()
        v.keyPressEvent(cmd_key_event(Qt.Key_C))
        pump(2)
        check(failures, tag,
              cb.text() == ("Sample Report\n"
                            "Synthetic verification record (no real data)"),
              f"Cmd+C clipboard {cb.text()!r}")

        # Cmd+A selects every word on the current page.
        v.keyPressEvent(cmd_key_event(Qt.Key_A))
        pump(1)
        check(failures, tag, v._text_sel == (0, 0, len(words) - 1),
              f"Cmd+A selected {v._text_sel}")
    finally:
        close_window(w)


# ==========================================================================
# T21: double-click = word, third press = line (synthesized mouse events)
# ==========================================================================
def test_t21_double_triple_click(failures: list[str]) -> None:
    tag = "T21_double_triple"
    w = open_window(FORM)
    try:
        v = w.view
        v.enter_select_text_mode()
        pump(2)
        words = w.document.page_words(0)
        i_rec = word_index(words, "record")
        if not check(failures, tag, i_rec >= 0, "'record' word missing"):
            return
        center = v._word_scene_rect(0, words[i_rec]).center()
        click_word(v, center, double=True)
        check(failures, tag, v.text_selection_string() == "record",
              f"double-click selected {v.text_selection_string()!r}")

        # The third press within the double-click interval promotes the word
        # selection to its whole visual line.
        click_word(v, center)
        check(failures, tag,
              v.text_selection_string()
              == "Synthetic verification record (no real data)",
              f"triple-click selected {v.text_selection_string()!r}")

        # A plain press elsewhere (after the interval state cleared) anchors
        # a fresh single-word selection.
        i_sample = word_index(words, "Sample")
        click_word(v, v._word_scene_rect(0, words[i_sample]).center())
        check(failures, tag, v.text_selection_string() == "Sample",
              f"fresh press selected {v.text_selection_string()!r}")
    finally:
        close_window(w)


# ==========================================================================
# T22: rotated page -- the highlight band intersects the rendered ink
# ==========================================================================
def test_t22_rotated_highlight(failures: list[str]) -> None:
    tag = "T22_rotated"
    w = open_window(ROTATED)
    try:
        v = w.view
        v.enter_select_text_mode()
        pump(2)
        words = w.document.page_words(0)
        i_rot = word_index(words, "ROTATED")
        if not check(failures, tag, i_rot >= 0, "'ROTATED' word missing"):
            return
        v.select_text_range(0, i_rot, i_rot)
        if not check(failures, tag, len(v._text_sel_items) == 1,
                     "no highlight band on the rotated page"):
            return
        rect = v._text_sel_items[0].rect()
        # On the /Rotate 90 page the horizontal text-space word must paint a
        # VERTICAL band in display space (taller than wide).
        check(failures, tag, rect.height() > rect.width(),
              f"rotated band {rect.width():.0f}x{rect.height():.0f} is not "
              "vertical")
        # Sample the materialized page image (display space) under the band:
        # the highlight must cover actual glyph ink, proving the bbox went
        # through the rotation_matrix mapping (the region_ink pattern).
        layer = v._layers[0]
        if not check(failures, tag, layer.image is not None,
                     "page 0 image not materialized"):
            return
        img = layer.image
        origin = v._sheet_origin_for(0)
        x0 = max(0, int(rect.left() - origin.x()))
        y0 = max(0, int(rect.top() - origin.y()))
        x1 = min(img.width(), int(rect.right() - origin.x()))
        y1 = min(img.height(), int(rect.bottom() - origin.y()))
        check(failures, tag, x1 > x0 and y1 > y0,
              "highlight band lies outside the page image")
        ink = 0
        for yy in range(y0, y1):
            for xx in range(x0, x1):
                argb = img.pixel(xx, yy)
                r, g, b = (argb >> 16) & 255, (argb >> 8) & 255, argb & 255
                if 0.299 * r + 0.587 * g + 0.114 * b < 230:
                    ink += 1
        check(failures, tag, ink > 50,
              f"only {ink} ink px under the rotated highlight band")
    finally:
        close_window(w)


# ==========================================================================
# T23: chrome -- tool strip / S shortcut / toast / context menu / teardown
# ==========================================================================
def test_t23_tool_chrome_teardown(failures: list[str]) -> None:
    tag = "T23_chrome"
    w = open_window(FORM)
    try:
        v = w.view
        # Charcoal redesign: Select Text is no longer a rail button (the rail is
        # the slim mode set Select / Text / Add / Markup / Notes / Outline /
        # Find). It stays fully reachable via its plain 'S' shortcut and the
        # Tools menu, which is what this chrome check now asserts.
        check(failures, tag, "select_text" not in w.left_panel._buttons,
              "Select Text should no longer be a rail button")
        check(failures, tag,
              w.act_select_text_tool.shortcut() == QKeySequence("S"),
              "act_select_text_tool shortcut is not plain S")
        check(failures, tag,
              w.act_select_text_tool in w.menu_tools.actions(),
              "Select Text Tool missing from the Tools menu")

        # The S handler still arms the mode (it just has no rail button to light).
        w._on_tool_shortcut("select_text")
        pump(2)
        check(failures, tag, v.current_mode() == "select_text",
              f"mode {v.current_mode()!r} after S")

        # Hotspot presses fall through while the tool is armed.
        hs = hotspot_for(v, "Cleared")
        if not check(failures, tag, hs is not None, "fixture span missing"):
            return
        check(failures, tag,
              v._on_box_press(hs, hs.sceneBoundingRect().center()) is False,
              "hotspot press not passed through in select_text mode")
        check(failures, tag, v.current_selection() is None,
              "a box got selected while the text tool was armed")

        # act_copy routes to the word selection and toasts "N words copied".
        words = w.document.page_words(0)
        i_p, i_r = word_index(words, "Pending"), word_index(words, "review")
        v.select_text_range(0, i_p, i_r)
        cb = QApplication.clipboard()
        cb.clear()
        w.act_copy.trigger()
        pump(2)
        check(failures, tag, cb.text() == "Pending review",
              f"act_copy copied {cb.text()!r}")
        check(failures, tag, "2 words copied" in w.filename_label.text(),
              f"toast {w.filename_label.text()!r}")

        # Context menu case (iv): exactly Copy + Select All.
        menu = v._context_menu_for(QPointF(200.0, 200.0))
        if check(failures, tag, menu is not None,
                 "no context menu in select_text mode"):
            titles = [a.text() for a in menu.actions() if not a.isSeparator()]
            check(failures, tag, titles == ["Copy", "Select All"],
                  f"context menu titles {titles}")

        # Esc clears the selection first, then disarms back to SELECT.
        v.keyPressEvent(key_event(Qt.Key_Escape))
        pump(1)
        check(failures, tag,
              v._text_sel is None and v.current_mode() == "select_text",
              "first Esc did not just clear the selection")
        v.keyPressEvent(key_event(Qt.Key_Escape))
        pump(1)
        check(failures, tag, v.current_mode() == "select",
              "second Esc did not disarm the tool")

        # Re-arm, select, then switch back via the strip handler: every
        # highlight item is gone and hotspot presses work again.
        w._on_tool_shortcut("select_text")
        pump(1)
        v.select_text_range(0, i_p, i_r)
        check(failures, tag, len(v._text_sel_items) > 0,
              "no highlight to tear down")
        w._on_tool_shortcut("select")
        pump(2)
        check(failures, tag, v.current_mode() == "select",
              f"mode {v.current_mode()!r} after switching back")
        check(failures, tag, v._text_sel is None and not v._text_sel_items,
              "switching tools left highlight state behind")
        check(failures, tag,
              v._on_box_press(hs, hs.sceneBoundingRect().center()) is True,
              "hotspot press still disabled after leaving select_text")
        check(failures, tag, v.current_selection() is not None,
              "hotspot press did not reselect the box")
        v._cancel_drag()
        v.clear_selection()
    finally:
        close_window(w)


# ==========================================================================
def main() -> int:
    failures: list[str] = []
    tests = [
        ("T1_caret_precision", lambda: test_t1_caret_precision(failures)),
        ("T2_word_line_selection",
         lambda: test_t2_word_line_selection(failures)),
        ("T3_bold_paths", lambda: test_t3_bold_both_paths(failures)),
        ("T4_dirty_after_undo", lambda: test_t4_dirty_after_undo(failures)),
        ("T4b_dirty_after_save_undo",
         lambda: test_t4b_dirty_after_save_undo(failures)),
        ("T4c_save_flushes_editor",
         lambda: test_t4c_save_flushes_editor(failures)),
        ("T5_inspector_live", lambda: test_t5_inspector_live(failures)),
        ("T6_tool_shortcuts", lambda: test_t6_tool_shortcuts(failures)),
        ("T7_commit_teardown", lambda: test_t7_commit_teardown(failures)),
        ("T8_is_edited_cross_page",
         lambda: test_t8_is_edited_cross_page(failures)),
        ("T9_nudge", lambda: test_t9_nudge_coalesced(failures)),
        ("T10_context_menu", lambda: test_t10_context_menu(failures)),
        ("T11_paste_clamp", lambda: test_t11_paste_clamp(failures)),
        ("T12_compact_handles",
         lambda: test_t12_compact_handles(failures)),
        ("T13_ghost_drag", lambda: test_t13_ghost_drag_esc(failures)),
        ("T14_editor_copy", lambda: test_t14_editor_copy_formats(failures)),
        ("T15_sanitized_paste", lambda: test_t15_sanitized_paste(failures)),
        ("T16_cross_box_runs", lambda: test_t16_cross_box_runs(failures)),
        ("T17_system_paste",
         lambda: test_t17_system_canvas_paste(failures)),
        ("T18_edit_menu",
         lambda: test_t18_edit_menu_clipboard(failures)),
        ("T19_page_words", lambda: test_t19_page_words(failures)),
        ("T20_select_copy", lambda: test_t20_select_and_copy(failures)),
        ("T21_double_triple",
         lambda: test_t21_double_triple_click(failures)),
        ("T22_rotated", lambda: test_t22_rotated_highlight(failures)),
        ("T23_chrome", lambda: test_t23_tool_chrome_teardown(failures)),
    ]
    for name, fn in tests:
        print(f"[{name}]")
        try:
            fn()
        except Exception:
            failures.append(f"{name}: raised:\n{traceback.format_exc()}")

    # Release any test-owned QMimeData from the offscreen clipboard BEFORE
    # interpreter teardown: leaving it installed segfaults the offscreen
    # platform's clipboard destructor AFTER all output (the spec §1 probe
    # note) -- which would turn a fully green run into exit 139.
    try:
        QApplication.clipboard().clear()
    except Exception:  # noqa: BLE001 - never let cleanup mask the verdict
        pass

    print()
    if failures:
        print(f"FAILED ({len(failures)}):")
        for f in failures:
            print(" -", f)
        return 1
    print("test_edit_ux: all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
