"""Headless verification for the NAVIGATION build, milestones M1-M3
(outline preservation + Bookmarks panel; menu completion + cheatsheet;
zoom gestures + session restore + app-only recents + drag-drop + icons).

Covers, against the REAL model + the REAL ``MainWindow`` (offscreen):

  1. OUTLINE READ -- ``PDFDocument(outline_doc).outline()`` returns the
     fixture TOC exactly, as a FRESH list per call (mutating the returned
     list never leaks into the document).
  2. TOC SURVIVES SAVE -- stage one in-place text edit, ``save_as``: the
     saved file's ``get_toc()`` still equals the fixture TOC and the edit
     applied (THE bookmark-wipe data-loss regression; the carry itself
     landed with the doc-tools workstream -- this VERIFIES it on the
     committed outline fixture).
  3. SET_OUTLINE -- round-trips through ``outline()``; is ONE structural op
     (depth + 1); the saved file reflects it; ``undo_structural`` restores
     the prior TOC; ``redo_structural`` re-applies; an empty list clears.
  4. VALIDATION -- ``[[2,'bad',1]]`` raises ValueError (first level must be
     1), as do a level skip, an empty title, a non-int page, and a malformed
     entry -- all BEFORE any snapshot (structural_depth unchanged, outline
     untouched).
  5. DELETE-PAGE DANGLING -- ``delete_page(1)`` auto-remaps later targets
     and leaves the deleted page's entry at page == -1 (kept, like Acrobat,
     not dropped).
  6. PANEL TREE -- open in MainWindow: BookmarkPanel sits at left-stack
     index 3, the tree nests the fixture TOC (2 top-level rows, "Agenda"
     under "Welcome", 3 rows total), the strip's Bookmarks tool swaps the
     stack + header.
  7. PANEL JUMP -- a simulated click on "Policies" moves the canvas to page
     index 3; "Agenda" to index 1.
  8. PANEL ADD + UNDO -- the Add button appends "Page N" for the current
     page as ONE StructuralCommand (undo depth + 1, dirty set, Save
     enabled); ``undo_stack.undo()`` restores the prior outline AND the
     tree.
  9. PANEL RENAME + DELETE -- an inline rename (itemChanged) commits the
     new title (deferred one event-loop turn -- pumped); deleting "Welcome"
     removes its SUBTREE ("Agenda" goes with it), leaving just "Policies".
 10. DANGLING ROW -- after a window-level ``delete_page(1)`` the "Agenda"
     row renders dimmed (TEXT_SECONDARY foreground) with the deleted-page
     tooltip, and clicking it does NOT move the canvas.
 11. CHROME CENSUS -- the bookmarks strip button exists and disables in the
     empty state; View > Show Bookmarks (Cmd+Alt+B) sits BEFORE the
     view_panels anchor, disables with no document, and stays in lockstep
     with the strip both ways; ``make_icon("bookmark")`` /
     ``make_icon("bookmark_add")`` render non-null pixmaps.

Milestone M2 (menu bar completion, cheatsheet, About, chrome polish):

 12. MENU COMPLETION -- menubar titles are EXACTLY the 7-menu skeleton;
     File > Open Recent sits right after Open…; Close Tab is pinned to the
     explicit Ctrl+W; Help carries Keyboard Shortcuts… (Cmd+/) + About
     (AboutRole); the ws6 Document/File entries appear EXACTLY once across
     the whole menubar (integrated, not duplicated).
 13. SHORTCUT REACHABILITY -- every shortcut-bearing QAction owned by the
     window is reachable by walking the menubar (empty allowlist: the
     zoom-preset duplicate was removed, tab cycling + page-nav live in
     Window/View).
 14. SHORTCUT UNIQUENESS -- no two ENABLED actions share a shortcut string
     (the Ctrl+0 zoom-preset duplicate that made Cmd+0 ambiguous is gone).
 15. CHEATSHEET -- ``_show_shortcuts()`` shows a cached NON-MODAL dialog
     whose row count equals an independent census of shortcut-bearing menu
     actions; names carry no mnemonic markers ("Find & Replace" keeps its
     literal ampersand via the && escape); every row has a key string.
 16. ABOUT -- ``_show_about()`` shows a cached non-modal AboutDialog whose
     version label carries ``pdftexteditor.__version__``.
 17. OPEN RECENT -- the submenu rebuilds from ``_recent_paths`` on
     aboutToShow (STUBBED with synthetic paths -- never real machine
     state): basename rows, same-name rows disambiguated by folder, full
     path in the tooltip, Clear Menu tail; ``_clear_recents`` empties the
     store (a TEMP QSettings ini -- never the real prefs); an empty list
     leaves Clear Menu disabled.
 18. WINDOW DOC LIST -- the Window menu lists every open document on
     aboutToShow with the checkmark on the active tab; activating an entry
     switches tabs.
 19. V/E KEYS + CLIPBOARD ENABLEMENT (ws2 ownership VERIFIED, not
     re-implemented) -- real key clicks V/E flip the strip tool;
     Cut/Copy/Paste disabled with no document, enabled with one, and STAY
     enabled while an inline editor is mounted because ws2 routes them BY
     STATE (act_copy copies the editor selection -- disabling them would
     break that landed contract, see test_edit_ux T18).
 20. WINDOW CHROME -- toggle labels flip Show <-> Hide (pages dock close
     via X included); the title is "name[*] -- PDF Text Editor" with
     windowFilePath set for the macOS proxy icon and the modified marker
     tracking unsaved state (structural dirt included).

Milestone M3 (zoom gestures, session restore, app-only recents, drag-drop,
icons):

 21. GESTURE ZOOM FUNNEL -- ``_apply_gesture_zoom`` clamps to
     ZOOM_MIN/ZOOM_MAX and THROTTLES: nothing applies synchronously, a
     burst of targets coalesces into ONE ``set_zoom`` (one zoomChanged)
     when the 120 ms single-shot fires.
 22. NATIVE GESTURES + Cmd+WHEEL -- synthesized QNativeGestureEvents:
     Begin seeds the pinch with the live zoom, Zoom ticks accumulate
     ``* (1 + value)``, End forces the final apply IMMEDIATELY (no 120 ms
     wait); SmartZoom toggles fit-width <-> 100%; a Ctrl(Cmd)-modified
     wheelEvent zooms by 1.1 per notch through the same throttle while a
     PLAIN wheel stays a scroll; no document = no gesture zoom.
 23. SESSION SAVE/RESTORE -- against a monkeypatched ``_settings`` temp
     ini (never the real prefs): ``_save_session`` writes files in tab
     order + active index + the active doc's page + zoom;
     ``_restore_session`` on a fresh window reopens the tabs and restores
     active/page/zoom.
 24. SESSION EDGES -- a missing-file entry is skipped without error (the
     surviving file still opens, active re-found by path); broken JSON
     and an absent key never raise; restore is a NO-OP on a non-empty
     workspace; a natural ``close()`` writes the session ONCE through the
     seam while the ``_suppress_close_guard`` teardown path writes
     NOTHING (the privacy contract every suite relies on).
 25. RECENTS APP-ONLY -- ``_recent_paths`` source carries no
     mdfind/subprocess (the Spotlight merge is DELETED, not dormant);
     against a stubbed store of synthetic paths it returns in < 0.2 s,
     keeps store order, drops missing files and dedups, and the
     repo/temp skip-prefixes hide fixtures + transient PDFs (the
     ``_recent_skip_prefixes`` seam relaxes the temp exclusion so the
     synthetic files are visible at all).
 26. EMPTY-STATE RECENTS -- rows render the filename (RecentName) over a
     dimmed folder line (RecentFolder, home-abbreviated to ``~/...``)
     with the full path as tooltip; the Clear Recents link shows with the
     list, empties the stubbed store via ``_clear_recents``, and hides
     with the emptied list (title + rows gone).
 27. MULTI-FILE DRAG-DROP -- a synthesized two-PDF QDropEvent on the
     CanvasContainer opens BOTH tabs (non-PDF URLs in the same drop are
     ignored); the WINDOW-level delegating handlers accept the same drag
     (toolbar/dock drops) and open both; a non-PDF-only drag is ignored.
 28. NEW ICONS -- info / keyboard / cut / copy / paste / clear_recent
     render non-null pixmaps and are wired (About, the cheatsheet, the
     Edit clipboard trio).

Run:
    QT_QPA_PLATFORM=offscreen \
      /Users/edward/Documents/GitHub/PDFTextEditor/.venv/bin/python \
      tests/test_navigation.py
"""

from __future__ import annotations

import inspect
import json
import os
import re
import shutil
import sys
import tempfile
import time
import traceback

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import fitz  # noqa: E402
from PySide6.QtCore import (  # noqa: E402
    QEvent,
    QEventLoop,
    QMimeData,
    QPoint,
    QPointF,
    QSettings,
    Qt,
    QTimer,
    QUrl,
)
from PySide6.QtGui import (  # noqa: E402
    QAction,
    QColor,
    QDragEnterEvent,
    QDropEvent,
    QKeySequence,
    QNativeGestureEvent,
    QPointingDevice,
    QWheelEvent,
)
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QLabel, QPushButton  # noqa: E402

from pdftexteditor import __version__  # noqa: E402
from pdftexteditor.document import PDFDocument  # noqa: E402
from pdftexteditor.ui import theme  # noqa: E402
from pdftexteditor.ui.icons import make_icon  # noqa: E402
from pdftexteditor.ui.main_window import MainWindow  # noqa: E402
from pdftexteditor.ui.page_view import ZOOM_MAX, ZOOM_MIN  # noqa: E402

_APP = QApplication.instance() or QApplication(sys.argv)

FIXTURES = os.path.join(REPO, "tests", "fixtures")
OUTLINE_DOC = os.path.join(FIXTURES, "outline_doc.pdf")
THREE_PAGE = os.path.join(FIXTURES, "three_page.pdf")

MENU_TITLES = ["File", "Edit", "View", "Tools", "Document", "Window", "Help"]

# The fixture contract (build_nav_fixture.py).
TOC = [[1, "Welcome", 1], [2, "Agenda", 2], [1, "Policies", 4]]


def check(failures: list, tag: str, cond: bool, msg: str) -> bool:
    if not cond:
        failures.append(f"{tag}: {msg}")
    return cond


def pump(n: int = 6) -> None:
    for _ in range(n):
        _APP.processEvents()


def wait_ms(ms: int = 250) -> None:
    """Pump the event loop for ``ms`` real milliseconds (the QEventLoop +
    QTimer pattern, tests/test_editor.py) -- the gesture throttle is a
    wall-clock 120 ms single-shot."""
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()
    pump(2)


def flush_deferred() -> None:
    """Flush deleteLater()d widgets (set_recents rebuilds its rows that way;
    the test_app screenshot-harness discipline)."""
    pump(2)
    _APP.sendPostedEvents(None, QEvent.DeferredDelete)
    pump(2)


def ini_settings(path: str):
    """A factory for temp-ini QSettings stubs: tests monkeypatch
    ``window._settings`` with this so NOTHING touches the real
    preferences."""
    return lambda: QSettings(path, QSettings.Format.IniFormat)


def recent_rows(w: MainWindow) -> list:
    # The start screen is now a gallery of RecentCard tiles (one per recent
    # file); the cards carry the source path + the name/folder labels.
    from pdftexteditor.ui.main_window import RecentCard
    return w.empty_state.findChildren(RecentCard)


def open_window(path: str | None = OUTLINE_DOC) -> MainWindow:
    w = MainWindow()
    w.resize(1300, 950)
    w.show()
    if path:
        w.open_path(path)
    pump(6)
    return w


def close(w: MainWindow) -> None:
    w._suppress_close_guard = True
    w.close()


def find_span(doc: PDFDocument, page: int, needle: str):
    for b in doc.spans(page):
        if needle in b.text.replace(" ", " "):
            return b
    return None


def tree_rows(tree) -> list:
    """Every QTreeWidgetItem, depth-first."""
    out = []

    def walk(item):
        out.append(item)
        for k in range(item.childCount()):
            walk(item.child(k))

    for i in range(tree.topLevelItemCount()):
        walk(tree.topLevelItem(i))
    return out


def find_row(tree, title: str):
    for item in tree_rows(tree):
        if item.text(0) == title:
            return item
    return None


def walk_menubar(menubar, keep: list) -> list:
    """Every non-separator action reachable from the menubar, depth-first,
    DUPLICATES KEPT (the completion test counts occurrences). ``keep`` must
    outlive the walk: PySide 6.11 deletes live C++ menus when the
    intermediate wrappers die mid-walk (the test_perf_foundation D3
    pattern)."""
    out = []

    def walk(menu) -> None:
        keep.append(menu)
        for act in menu.actions():
            keep.append(act)
            sub = act.menu()
            if sub is not None:
                walk(sub)
            elif not act.isSeparator():
                out.append(act)

    for top in menubar.actions():
        keep.append(top)
        sub = top.menu()
        if sub is not None:
            walk(sub)
    return out


def shortcut_strings(act) -> list:
    return [s.toString() for s in act.shortcuts() if not s.isEmpty()]


# ---------------------------------------------------------------------------
# 1. outline() read API
# ---------------------------------------------------------------------------
def test_outline_read(failures: list) -> None:
    doc = PDFDocument(OUTLINE_DOC)
    try:
        got = doc.outline()
        check(failures, "read.toc", got == TOC,
              f"outline() != fixture TOC: {got!r}")
        # Fresh list per call: mutating the result never leaks into the doc.
        got.append([1, "Bogus", 5])
        got[0][1] = "Mutated"
        check(failures, "read.fresh", doc.outline() == TOC,
              f"outline() leaked a caller mutation: {doc.outline()!r}")
    finally:
        doc.close()


# ---------------------------------------------------------------------------
# 2. TOC survives open -> edit -> save (the data-loss regression)
# ---------------------------------------------------------------------------
def test_toc_survives_save(failures: list) -> None:
    td = tempfile.mkdtemp(prefix="nav_save_")
    doc = PDFDocument(OUTLINE_DOC)
    try:
        span = find_span(doc, 0, "handbook")
        if not check(failures, "save.span", span is not None,
                     "no 'handbook' span on page 1 of outline_doc.pdf"):
            return
        new_text = span.text.replace(" ", " ").replace(
            "handbook", "manual")
        doc.stage_edit(0, span, new_text)
        out = os.path.join(td, "outline_out.pdf")
        doc.save_as(out)
        saved = fitz.open(out)
        try:
            check(failures, "save.toc", saved.get_toc() == TOC,
                  f"save_as wiped/changed the TOC: {saved.get_toc()!r}")
            text = saved[0].get_text().replace(" ", " ")
            check(failures, "save.edit",
                  "manual" in text and "handbook" not in text,
                  f"staged edit missing from saved page 1: {text[:120]!r}")
        finally:
            saved.close()
    finally:
        doc.close()
        shutil.rmtree(td, ignore_errors=True)


# ---------------------------------------------------------------------------
# 3. set_outline: one structural op, saved, undo/redo
# ---------------------------------------------------------------------------
def test_set_outline(failures: list) -> None:
    td = tempfile.mkdtemp(prefix="nav_set_")
    doc = PDFDocument(OUTLINE_DOC)
    try:
        new = [[1, "Alpha", 1], [2, "Beta", 2], [2, "Gamma", 3],
               [1, "Delta", 5]]
        depth0 = doc.structural_depth
        doc.set_outline(new)
        check(failures, "set.applied", doc.outline() == new,
              f"set_outline round trip failed: {doc.outline()!r}")
        check(failures, "set.one_op", doc.structural_depth == depth0 + 1,
              f"set_outline was not ONE structural op: depth "
              f"{depth0} -> {doc.structural_depth}")
        check(failures, "set.dirty", doc.dirty,
              "set_outline did not dirty the document")
        out = os.path.join(td, "set_out.pdf")
        doc.save_as(out)
        saved = fitz.open(out)
        try:
            check(failures, "set.saved", saved.get_toc() == new,
                  f"saved file missing the new outline: {saved.get_toc()!r}")
        finally:
            saved.close()
        doc.undo_structural()
        check(failures, "set.undo", doc.outline() == TOC,
              f"undo_structural did not restore the TOC: {doc.outline()!r}")
        doc.redo_structural()
        check(failures, "set.redo", doc.outline() == new,
              f"redo_structural did not re-apply: {doc.outline()!r}")
        # Empty list clears the outline (probe-locked set_toc behavior).
        doc.set_outline([])
        check(failures, "set.clear", doc.outline() == [],
              f"set_outline([]) did not clear: {doc.outline()!r}")
    finally:
        doc.close()
        shutil.rmtree(td, ignore_errors=True)


# ---------------------------------------------------------------------------
# 4. set_outline validation (guard-before-snapshot)
# ---------------------------------------------------------------------------
def test_outline_validation(failures: list) -> None:
    doc = PDFDocument(OUTLINE_DOC)
    try:
        depth0 = doc.structural_depth
        bad = [
            ("first_level", [[2, "bad", 1]]),
            ("level_skip", [[1, "A", 1], [3, "B", 2]]),
            ("empty_title", [[1, "   ", 1]]),
            ("page_not_int", [[1, "A", "1"]]),
            ("malformed", [[1, "A"]]),
            ("level_not_int", [["1", "A", 1]]),
        ]
        for tag, entries in bad:
            try:
                doc.set_outline(entries)
                check(failures, f"valid.{tag}", False,
                      f"{entries!r} did not raise ValueError")
            except ValueError:
                pass
            except Exception as exc:  # noqa: BLE001
                check(failures, f"valid.{tag}", False,
                      f"{entries!r} raised {type(exc).__name__}, "
                      f"not ValueError")
        check(failures, "valid.no_snapshot",
              doc.structural_depth == depth0,
              f"a rejected set_outline took a snapshot: depth "
              f"{depth0} -> {doc.structural_depth}")
        check(failures, "valid.untouched", doc.outline() == TOC,
              f"a rejected set_outline touched the doc: {doc.outline()!r}")
        check(failures, "valid.clean", not doc.dirty,
              "a rejected set_outline dirtied the document")
    finally:
        doc.close()


# ---------------------------------------------------------------------------
# 5. delete_page leaves a remapped TOC with a dangling -1 entry
# ---------------------------------------------------------------------------
def test_delete_page_dangling(failures: list) -> None:
    doc = PDFDocument(OUTLINE_DOC)
    try:
        doc.delete_page(1)      # "Agenda" targeted the deleted page 2
        expect = [[1, "Welcome", 1], [2, "Agenda", -1], [1, "Policies", 3]]
        check(failures, "dangle.remap", doc.outline() == expect,
              f"delete_page TOC remap wrong: {doc.outline()!r} "
              f"(expected {expect!r})")
    finally:
        doc.close()


# ---------------------------------------------------------------------------
# 6. BookmarkPanel tree structure + stack slot
# ---------------------------------------------------------------------------
def test_panel_tree(failures: list) -> None:
    w = open_window()
    try:
        panel = w.bookmark_panel
        check(failures, "tree.slot",
              w.left_panel._stack.indexOf(panel) == 3,
              f"BookmarkPanel not at stack index 3: "
              f"{w.left_panel._stack.indexOf(panel)}")
        tree = panel.tree
        rows = tree_rows(tree)
        check(failures, "tree.rows", len(rows) == 3,
              f"expected 3 rows, got {len(rows)}")
        check(failures, "tree.top_level",
              tree.topLevelItemCount() == 2,
              f"expected 2 top-level rows, got {tree.topLevelItemCount()}")
        welcome = tree.topLevelItem(0)
        check(failures, "tree.nesting",
              welcome is not None and welcome.text(0) == "Welcome"
              and welcome.childCount() == 1
              and welcome.child(0).text(0) == "Agenda",
              "'Agenda' is not nested under 'Welcome'")
        check(failures, "tree.expanded",
              welcome is not None and welcome.isExpanded(),
              "tree did not expandAll on refresh")
        # The strip's Bookmarks tool swaps the stack + header.
        w.left_panel._buttons["bookmarks"].click()
        pump()
        check(failures, "tree.swap",
              w.left_panel._stack.currentWidget() is panel,
              "Bookmarks tool did not swap the left stack to the panel")
        check(failures, "tree.header",
              w.left_panel._header_label.text() == "Bookmarks",
              f"header label: {w.left_panel._header_label.text()!r}")
    finally:
        close(w)


# ---------------------------------------------------------------------------
# 7. click a bookmark row -> the canvas jumps
# ---------------------------------------------------------------------------
def test_panel_jump(failures: list) -> None:
    w = open_window()
    try:
        panel = w.bookmark_panel
        check(failures, "jump.start", w.view_page_index() == 0,
              f"unexpected start page {w.view_page_index()}")
        policies = find_row(panel.tree, "Policies")
        if check(failures, "jump.row", policies is not None,
                 "no 'Policies' row in the tree"):
            panel.tree.setCurrentItem(policies)
            panel.tree.itemClicked.emit(policies, 0)
            pump()
            check(failures, "jump.page4", w.view_page_index() == 3,
                  f"'Policies' click landed on page index "
                  f"{w.view_page_index()}, not 3")
        agenda = find_row(panel.tree, "Agenda")
        if check(failures, "jump.row2", agenda is not None,
                 "no 'Agenda' row in the tree"):
            panel.tree.setCurrentItem(agenda)
            panel.tree.itemClicked.emit(agenda, 0)
            pump()
            check(failures, "jump.page2", w.view_page_index() == 1,
                  f"'Agenda' click landed on page index "
                  f"{w.view_page_index()}, not 1")
    finally:
        close(w)


# ---------------------------------------------------------------------------
# 8. panel Add -> one StructuralCommand; Qt undo restores outline + tree
# ---------------------------------------------------------------------------
def test_panel_add_undo(failures: list) -> None:
    w = open_window()
    try:
        doc = w.document
        panel = w.bookmark_panel
        w._set_view_page(2)     # Add targets the CURRENT page (-> "Page 3")
        pump()
        depth0 = doc.structural_depth
        count0 = w.undo_stack.count()
        panel.tree.setCurrentItem(None)     # no selection: append top level
        panel.add_button.click()
        pump()
        expect = TOC + [[1, "Page 3", 3]]
        check(failures, "add.applied", doc.outline() == expect,
              f"Add did not append 'Page 3': {doc.outline()!r}")
        check(failures, "add.one_command",
              doc.structural_depth == depth0 + 1
              and w.undo_stack.count() == count0 + 1,
              f"Add was not ONE StructuralCommand: depth {depth0} -> "
              f"{doc.structural_depth}, qt {count0} -> "
              f"{w.undo_stack.count()}")
        check(failures, "add.dirty", doc.dirty and w._has_unsaved(),
              "bookmark add did not set the dirty flag")
        check(failures, "add.save_enabled", w.act_save.isEnabled(),
              "Save did not enable after a bookmark edit")
        check(failures, "add.tree_row",
              find_row(panel.tree, "Page 3") is not None,
              "the new row is missing from the rebuilt tree")
        w.undo_stack.undo()
        pump()
        check(failures, "add.undo", doc.outline() == TOC,
              f"Qt undo did not restore the outline: {doc.outline()!r}")
        check(failures, "add.undo_tree",
              find_row(panel.tree, "Page 3") is None
              and len(tree_rows(panel.tree)) == 3,
              "undo did not rebuild the tree")
    finally:
        close(w)


# ---------------------------------------------------------------------------
# 9. panel rename (itemChanged commit) + delete (subtree)
# ---------------------------------------------------------------------------
def test_panel_rename_delete(failures: list) -> None:
    w = open_window()
    try:
        doc = w.document
        panel = w.bookmark_panel
        welcome = find_row(panel.tree, "Welcome")
        if not check(failures, "ren.row", welcome is not None,
                     "no 'Welcome' row"):
            return
        # The inline editor commits through itemChanged; the panel defers
        # the outline commit one event-loop turn (pump fires it).
        welcome.setText(0, "Welcome Day")
        pump()
        expect = [[1, "Welcome Day", 1], [2, "Agenda", 2],
                  [1, "Policies", 4]]
        check(failures, "ren.applied", doc.outline() == expect,
              f"rename did not commit: {doc.outline()!r}")
        # Renaming back to the SAME title is a no-op (no phantom command).
        depth1 = doc.structural_depth
        row = find_row(panel.tree, "Welcome Day")
        if row is not None:
            row.setText(0, "Welcome Day")
            pump()
        check(failures, "ren.noop", doc.structural_depth == depth1,
              "an unchanged rename pushed a structural command")
        # Delete removes the item AND its subtree ("Agenda" goes with it).
        row = find_row(panel.tree, "Welcome Day")
        if check(failures, "del.row", row is not None,
                 "no 'Welcome Day' row after rename"):
            panel.tree.setCurrentItem(row)
            panel.delete_button.click()
            pump()
            check(failures, "del.subtree",
                  doc.outline() == [[1, "Policies", 4]],
                  f"delete did not remove the subtree: {doc.outline()!r}")
            check(failures, "del.tree",
                  len(tree_rows(panel.tree)) == 1,
                  f"tree rows after delete: {len(tree_rows(panel.tree))}")
    finally:
        close(w)


# ---------------------------------------------------------------------------
# 10. dangling entries render dimmed and do not jump
# ---------------------------------------------------------------------------
def test_panel_dangling(failures: list) -> None:
    w = open_window()
    try:
        panel = w.bookmark_panel
        w._run_structural(lambda d: d.delete_page(1))
        pump()
        agenda = find_row(panel.tree, "Agenda")
        if not check(failures, "dang.row", agenda is not None,
                     "no 'Agenda' row after delete_page"):
            return
        check(failures, "dang.ref",
              agenda.data(0, Qt.UserRole) == ("Agenda", -1),
              f"dangling ref wrong: {agenda.data(0, Qt.UserRole)!r}")
        check(failures, "dang.dimmed",
              agenda.foreground(0).color().name()
              == QColor(theme.TEXT_SECONDARY).name(),
              f"dangling row not dimmed: "
              f"{agenda.foreground(0).color().name()}")
        check(failures, "dang.tip",
              agenda.toolTip(0) == "Target page was deleted",
              f"dangling tooltip wrong: {agenda.toolTip(0)!r}")
        w._set_view_page(0)
        pump()
        panel.tree.setCurrentItem(agenda)
        panel.tree.itemClicked.emit(agenda, 0)
        pump()
        check(failures, "dang.no_jump", w.view_page_index() == 0,
              f"a dangling click moved the canvas to "
              f"{w.view_page_index()}")
    finally:
        close(w)


# ---------------------------------------------------------------------------
# 11. chrome census: strip button, menu home, toggle lockstep, icons
# ---------------------------------------------------------------------------
def test_chrome_census(failures: list) -> None:
    # Empty state first: tool + action disabled with no document.
    w0 = open_window(path=None)
    try:
        btn = w0.left_panel._buttons.get("bookmarks")
        check(failures, "census.strip", btn is not None,
              "no 'bookmarks' button in the tool strip")
        check(failures, "census.empty_tool",
              btn is not None and not btn.isEnabled(),
              "bookmarks tool enabled with no document")
        check(failures, "census.empty_action",
              not w0.act_toggle_bookmarks.isEnabled(),
              "Show Bookmarks enabled with no document")
    finally:
        close(w0)

    w = open_window()
    try:
        check(failures, "census.tool_on",
              w.left_panel._buttons["bookmarks"].isEnabled(),
              "bookmarks tool stayed disabled with a document open")
        # Menu home: in the View menu, BEFORE the view_panels anchor.
        acts = w.menu_view.actions()
        check(failures, "census.in_view",
              w.act_toggle_bookmarks in acts,
              "Show Bookmarks is not in the View menu")
        anchor = w.menu_anchors["view_panels"]
        if w.act_toggle_bookmarks in acts and anchor in acts:
            check(failures, "census.before_anchor",
                  acts.index(w.act_toggle_bookmarks) < acts.index(anchor),
                  "Show Bookmarks sits after the view_panels anchor")
        check(failures, "census.shortcut",
              w.act_toggle_bookmarks.shortcut().toString() == "Ctrl+Alt+B",
              f"shortcut: {w.act_toggle_bookmarks.shortcut().toString()!r}")
        # Toggle -> strip lockstep.
        w.act_toggle_bookmarks.setChecked(True)
        pump()
        check(failures, "census.toggle_on",
              w.left_panel.active_tool() == "bookmarks"
              and w.left_panel._stack.currentWidget() is w.bookmark_panel,
              "checking Show Bookmarks did not open the panel")
        # Strip -> toggle lockstep (picking another tool unchecks).
        w.left_panel._buttons["select"].click()
        pump()
        check(failures, "census.toggle_off",
              not w.act_toggle_bookmarks.isChecked(),
              "leaving the Bookmarks tool left the toggle checked")
        # Unchecking the toggle returns to Select.
        w.act_toggle_bookmarks.setChecked(True)
        pump()
        w.act_toggle_bookmarks.setChecked(False)
        pump()
        check(failures, "census.uncheck_select",
              w.left_panel.active_tool() == "select",
              f"unchecking left tool {w.left_panel.active_tool()!r}")
        # Icons render real pixmaps (append-only _ICONS additions).
        for name in ("bookmark", "bookmark_add"):
            check(failures, f"census.icon_{name}",
                  not make_icon(name).pixmap(24, 24).isNull(),
                  f"make_icon({name!r}) rendered a null pixmap")
    finally:
        close(w)


# ---------------------------------------------------------------------------
# 12. M2: menu completion -- skeleton filled, integrated, not duplicated
# ---------------------------------------------------------------------------
def test_menu_completion(failures: list) -> None:
    w = open_window()
    try:
        titles = [a.text() for a in w.menu_bar.actions()]
        check(failures, "menu.titles", titles == MENU_TITLES,
              f"menubar titles {titles}")
        # The bar is forced non-native so File/Edit/View… live IN the window
        # on macOS too (Clay-styled), not in the global menu bar.
        check(failures, "menu.in_window",
              w.menu_bar.isNativeMenuBar() is False,
              f"menu bar is native: {w.menu_bar.isNativeMenuBar()}")
        # Open Recent: a submenu right after Open….
        file_acts = w.menu_file.actions()
        recent_act = w.menu_open_recent.menuAction()
        check(failures, "menu.recent_home",
              file_acts.index(recent_act) == file_acts.index(w.act_open) + 1,
              "Open Recent is not directly after Open…")
        # Close Tab: pinned to the explicit, portable Ctrl+W.
        check(failures, "menu.close_w",
              w.act_close.shortcut().toString() == "Ctrl+W",
              f"Close Tab shortcut {w.act_close.shortcut().toString()!r}")
        check(failures, "menu.close_home", w.act_close in file_acts,
              "Close Tab is not in the File menu")
        # Help: Settings… + Keyboard Shortcuts… (Cmd+/) + About (AboutRole).
        # With the non-native bar, the PreferencesRole Settings item is no
        # longer hoisted into a macOS app menu, so it stays here in-window.
        help_acts = [a for a in w.menu_help.actions() if not a.isSeparator()]
        check(failures, "menu.help",
              help_acts == [w.act_preferences, w.act_shortcuts, w.act_about],
              f"Help menu actions {[a.text() for a in help_acts]}")
        check(failures, "menu.help_key",
              w.act_shortcuts.shortcut() == QKeySequence("Ctrl+/"),
              f"cheatsheet shortcut "
              f"{w.act_shortcuts.shortcut().toString()!r}")
        check(failures, "menu.about_role",
              w.act_about.menuRole() == QAction.MenuRole.AboutRole,
              f"About menu role {w.act_about.menuRole()!r}")
        # The ws6/ws3 contributions are INTEGRATED (exactly one menubar
        # home each), never duplicated by the completion pass.
        keep: list = []
        all_acts = walk_menubar(w.menu_bar, keep)
        for name in ("act_print", "act_optimize", "act_properties",
                     "act_export_images", "act_export_text", "act_security",
                     "act_watermark", "act_header_footer", "act_crop",
                     "act_combine", "act_rotate_cw", "act_delete_page",
                     "act_shortcuts", "act_about"):
            act = getattr(w, name)
            n = sum(1 for a in all_acts if a is act)
            check(failures, f"menu.once_{name}", n == 1,
                  f"{name} appears {n}x in the menubar (expected once)")
    finally:
        close(w)


# ---------------------------------------------------------------------------
# 13. M2: every shortcut-bearing window action is menubar-reachable
# ---------------------------------------------------------------------------
def test_shortcut_reachability(failures: list) -> None:
    w = open_window()
    try:
        keep: list = []
        reachable = {id(a) for a in walk_menubar(w.menu_bar, keep)}
        # The allowlist is EMPTY by design: the zoom presets carry no
        # shortcut any more (the Ctrl+0 duplicate was removed), tab cycling
        # lives in the Window menu, page nav in View > Page Navigation.
        allowlist: frozenset = frozenset()
        missing = []
        for act in w.findChildren(QAction):
            if not shortcut_strings(act):
                continue
            if act.text() in allowlist:
                continue
            if id(act) not in reachable:
                missing.append(f"{act.text()!r} {shortcut_strings(act)}")
        check(failures, "reach.all", not missing,
              f"shortcut-bearing actions with no menubar home: {missing}")
    finally:
        close(w)


# ---------------------------------------------------------------------------
# 14. M2: no two ENABLED actions share a shortcut string
# ---------------------------------------------------------------------------
def test_shortcut_uniqueness(failures: list) -> None:
    w = open_window()
    try:
        owners: dict = {}
        for act in w.findChildren(QAction):
            if not act.isEnabled():
                continue
            for s in shortcut_strings(act):
                owners.setdefault(s, []).append(act.text() or "<untitled>")
        clashes = {s: names for s, names in owners.items() if len(names) > 1}
        check(failures, "unique.enabled", not clashes,
              f"ambiguous ENABLED shortcuts (Qt fires neither): {clashes}")
        # The historical offender stays fixed: Ctrl+0 belongs to Actual
        # Size alone (the 100% zoom preset used to duplicate it).
        check(failures, "unique.ctrl0",
              owners.get("Ctrl+0") == ["Actual Size"],
              f"Ctrl+0 owners {owners.get('Ctrl+0')}")
    finally:
        close(w)


# ---------------------------------------------------------------------------
# 15. M2: the generated shortcuts cheatsheet (Cmd+/)
# ---------------------------------------------------------------------------
def test_cheatsheet(failures: list) -> None:
    w = open_window()
    try:
        w._show_shortcuts()
        pump()
        dlg = w._shortcuts_dialog
        if not check(failures, "sheet.shown", dlg is not None,
                     "_show_shortcuts left no dialog"):
            return
        check(failures, "sheet.name",
              dlg.objectName() == "ShortcutsDialog",
              f"objectName {dlg.objectName()!r}")
        check(failures, "sheet.nonmodal", not dlg.isModal(),
              "cheatsheet is modal (offscreen-test rule violation)")
        check(failures, "sheet.visible", dlg.isVisible(),
              "cheatsheet not visible after _show_shortcuts")
        # Cached: a second show reuses the instance.
        w._show_shortcuts()
        pump()
        check(failures, "sheet.cached", w._shortcuts_dialog is dlg,
              "_show_shortcuts rebuilt the dialog instead of caching it")
        # Row count == an INDEPENDENT census of shortcut-bearing menu
        # actions (deduped -- an action's first menu home wins).
        keep: list = []
        seen: set = set()
        expected = 0
        for act in walk_menubar(w.menu_bar, keep):
            if id(act) in seen or act.shortcut().isEmpty():
                continue
            seen.add(id(act))
            expected += 1
        check(failures, "sheet.rows", len(dlg.rows) == expected,
              f"{len(dlg.rows)} rows != {expected} shortcut-bearing menu "
              f"actions")
        groups = {g for g, _n, _k in dlg.rows}
        check(failures, "sheet.groups", groups <= set(MENU_TITLES),
              f"row groups outside the menubar titles: {groups}")
        bad_names = [n for _g, n, _k in dlg.rows
                     if "&&" in n or re.search(r"&(?=\S)", n)]
        check(failures, "sheet.mnemonics", not bad_names,
              f"mnemonic markers leaked into rendered names: {bad_names}")
        check(failures, "sheet.keys",
              all(k for _g, _n, k in dlg.rows),
              "a cheatsheet row rendered an empty shortcut")
        # The && escape renders the literal ampersand (the Find papercut).
        names = [n for _g, n, _k in dlg.rows]
        check(failures, "sheet.find_amp", "Find & Replace" in names,
              f"'Find & Replace' missing/mangled in {names[:12]}…")
        dlg.close()
    finally:
        close(w)


# ---------------------------------------------------------------------------
# 16. M2: About dialog
# ---------------------------------------------------------------------------
def test_about_dialog(failures: list) -> None:
    w = open_window(path=None)
    try:
        w._show_about()
        pump()
        dlg = w._about_dialog
        if not check(failures, "about.shown", dlg is not None,
                     "_show_about left no dialog"):
            return
        check(failures, "about.name", dlg.objectName() == "AboutDialog",
              f"objectName {dlg.objectName()!r}")
        check(failures, "about.nonmodal", not dlg.isModal(),
              "About is modal (offscreen-test rule violation)")
        check(failures, "about.visible", dlg.isVisible(),
              "About not visible after _show_about")
        check(failures, "about.version",
              __version__ in dlg.version_label.text(),
              f"version label {dlg.version_label.text()!r} missing "
              f"{__version__!r}")
        w._show_about()
        check(failures, "about.cached", w._about_dialog is dlg,
              "_show_about rebuilt the dialog instead of caching it")
        dlg.close()
    finally:
        close(w)


# ---------------------------------------------------------------------------
# 17. M2: Open Recent submenu (synthetic stub -- never real machine state)
# ---------------------------------------------------------------------------
def test_open_recent_menu(failures: list) -> None:
    td = tempfile.mkdtemp(prefix="nav_recent_")
    w = open_window(path=None)
    try:
        synth = [os.path.join(td, "forms", "onboarding.pdf"),
                 os.path.join(td, "archive", "onboarding.pdf"),
                 os.path.join(td, "handbook.pdf")]
        w._recent_paths = lambda *a: list(synth)   # instance stub: synthetic
        w.menu_open_recent.aboutToShow.emit()
        pump(2)
        acts = [a for a in w.menu_open_recent.actions()
                if not a.isSeparator()]
        check(failures, "recent.count", len(acts) == 4,
              f"{len(acts)} rows (want 3 entries + Clear Menu)")
        labels = [a.text() for a in acts[:3]]
        check(failures, "recent.disambiguated",
              labels[0].startswith("onboarding.pdf (")
              and labels[1].startswith("onboarding.pdf (")
              and labels[0] != labels[1],
              f"duplicate basenames not folder-disambiguated: {labels}")
        check(failures, "recent.plain", labels[2] == "handbook.pdf",
              f"unique basename got decorated: {labels[2]!r}")
        check(failures, "recent.tooltips",
              [a.toolTip() for a in acts[:3]] == synth,
              "tooltips do not carry the full paths")
        clear = acts[3]
        check(failures, "recent.clear_row",
              clear.text() == "Clear Menu" and clear.isEnabled(),
              f"Clear Menu row wrong: {clear.text()!r} "
              f"enabled={clear.isEnabled()}")

        # Clear Menu empties the store -- a TEMP ini, never the real prefs.
        ini = os.path.join(td, "recents.ini")
        w._recent_store = (
            lambda: QSettings(ini, QSettings.Format.IniFormat))
        w._recent_store().setValue(w._RECENT_KEY, synth)
        w._recent_store().sync()
        w._clear_recents()
        left = w._recent_store().value(w._RECENT_KEY, []) or []
        check(failures, "recent.cleared", list(left) == [],
              f"_clear_recents left {left!r} in the store")

        # Empty list: the submenu still opens, Clear Menu disabled.
        w._recent_paths = lambda *a: []
        w.menu_open_recent.aboutToShow.emit()
        pump(2)
        acts = [a for a in w.menu_open_recent.actions()
                if not a.isSeparator()]
        check(failures, "recent.empty",
              len(acts) == 1 and acts[0].text() == "Clear Menu"
              and not acts[0].isEnabled(),
              f"empty-list submenu wrong: "
              f"{[(a.text(), a.isEnabled()) for a in acts]}")
    finally:
        close(w)
        shutil.rmtree(td, ignore_errors=True)


# ---------------------------------------------------------------------------
# 18. M2: Window menu's dynamic open-document list
# ---------------------------------------------------------------------------
def test_window_doc_list(failures: list) -> None:
    w = open_window()
    try:
        w.open_path(THREE_PAGE)
        pump(4)
        check(failures, "wdocs.two_tabs", w.workspace.count == 2,
              f"workspace count {w.workspace.count}")
        w.menu_window.aboutToShow.emit()
        pump(2)
        entries = w._window_doc_actions
        check(failures, "wdocs.count", len(entries) == 2,
              f"{len(entries)} Window-menu doc entries")
        titles = [a.text() for a in entries]
        expect = [w.workspace.title(i) for i in range(2)]
        check(failures, "wdocs.titles", titles == expect,
              f"doc entries {titles} != tabs {expect}")
        checked = [a.isChecked() for a in entries]
        check(failures, "wdocs.checkmark",
              checked == [False, True],
              f"checkmark not on the active tab: {checked}")
        # Activating an entry switches tabs through the tab handler.
        entries[0].trigger()
        pump(4)
        check(failures, "wdocs.switch", w.workspace.active_index == 0,
              f"entry 0 left active_index {w.workspace.active_index}")
        w.menu_window.aboutToShow.emit()
        pump(2)
        checked = [a.isChecked() for a in w._window_doc_actions]
        check(failures, "wdocs.recheck", checked == [True, False],
              f"checkmark did not follow the switch: {checked}")
    finally:
        close(w)


# ---------------------------------------------------------------------------
# 19. M2: V/E tool keys + Cut/Copy/Paste enablement (ws2 ownership VERIFIED)
# ---------------------------------------------------------------------------
def test_keys_and_clipboard_enablement(failures: list) -> None:
    # Empty window: the ws2 clipboard actions disable without a document.
    w0 = open_window(path=None)
    try:
        for name in ("act_cut", "act_copy", "act_paste"):
            check(failures, f"keys.empty_{name}",
                  not getattr(w0, name).isEnabled(),
                  f"{name} enabled with no document")
    finally:
        close(w0)

    w = open_window()
    try:
        v = w.view
        # Real key clicks flip the strip tool (the shortcut map, not a
        # direct trigger).
        w.activateWindow()
        pump(2)
        QTest.keyClick(w, Qt.Key_E)
        pump(2)
        check(failures, "keys.e",
              w.left_panel.active_tool() == "text_edit",
              f"E armed {w.left_panel.active_tool()!r}, not text_edit")
        QTest.keyClick(w, Qt.Key_V)
        pump(2)
        check(failures, "keys.v",
              w.left_panel.active_tool() == "select",
              f"V armed {w.left_panel.active_tool()!r}, not select")

        # ws2's landed contract (test_edit_ux T18, the conflict-ledger #8
        # arbitration): Cut/Copy/Paste are enabled with a document and
        # STAY enabled while an inline editor is mounted, because they
        # route BY STATE -- act_copy copies the editor's text selection
        # with the runs payload. Disabling them here (the literal spec
        # bullet) would break that landed behavior, so M2 VERIFIES the
        # routing instead.
        for name in ("act_cut", "act_copy", "act_paste"):
            check(failures, f"keys.doc_{name}",
                  getattr(w, name).isEnabled(),
                  f"{name} disabled with a document open")
        layer = v._layers[0]
        if not layer.rendered:
            v._materialize_page(layer)
        pump(2)
        hs = next((h for h in v._hotspots
                   if "handbook" in getattr(h.box, "text", "")), None)
        if not check(failures, "keys.span", hs is not None,
                     "no 'handbook' hotspot on page 1"):
            return
        v.begin_edit(hs)
        pump(2)
        if not check(failures, "keys.editor", v._editor is not None,
                     "begin_edit mounted no editor"):
            return
        editor_text = v._editor.toPlainText()
        check(failures, "keys.copy_enabled", w.act_copy.isEnabled(),
              "act_copy disabled while the inline editor is mounted "
              "(breaks the ws2 editor-copy route)")
        cb = QApplication.clipboard()
        cb.clear()
        w.act_copy.trigger()
        pump(2)
        check(failures, "keys.copy_routes",
              cb.text().replace(" ", " ")
              == editor_text.replace(" ", " "),
              f"act_copy with an editor open copied {cb.text()!r}")
        v.cancel_edit()
        pump(2)
        cb.clear()
    finally:
        close(w)


# ---------------------------------------------------------------------------
# 20. M2: toggle labels + window title / proxy-icon chrome
# ---------------------------------------------------------------------------
def test_window_chrome(failures: list) -> None:
    w0 = open_window(path=None)
    try:
        check(failures, "chrome.empty_title",
              w0.windowTitle() == "PDF Text Editor",
              f"empty title {w0.windowTitle()!r}")
        check(failures, "chrome.empty_path", w0.windowFilePath() == "",
              f"empty windowFilePath {w0.windowFilePath()!r}")
    finally:
        close(w0)

    w = open_window()
    try:
        # Title + macOS proxy icon + clean modified state.
        check(failures, "chrome.title",
              w.windowTitle() == "outline_doc.pdf[*] — PDF Text Editor",
              f"title {w.windowTitle()!r}")
        check(failures, "chrome.file_path",
              w.windowFilePath() == w.document.path,
              f"windowFilePath {w.windowFilePath()!r}")
        check(failures, "chrome.clean", not w.isWindowModified(),
              "fresh document reads window-modified")
        # Structural dirt (no Qt-stack involvement) flips the [*] marker.
        w._run_structural(lambda d: d.set_outline([[1, "Only", 1]]))
        pump(2)
        check(failures, "chrome.dirty", w.isWindowModified(),
              "a structural edit did not set the modified marker")

        # Pages toggle label flips, including the dock-X path.
        check(failures, "chrome.pages_hide",
              w.act_toggle_pages.text() == "Hide Pages Sidebar",
              f"open-dock label {w.act_toggle_pages.text()!r}")
        w.pages_dock.close()
        pump(2)
        check(failures, "chrome.pages_show",
              w.act_toggle_pages.text() == "Show Pages Sidebar",
              f"closed-dock label {w.act_toggle_pages.text()!r}")
        w.act_toggle_pages.trigger()
        pump(2)
        check(failures, "chrome.pages_back",
              w.act_toggle_pages.text() == "Hide Pages Sidebar"
              and w.pages_dock.isVisible(),
              "menu re-open did not restore the dock + Hide label")
        # Bookmarks + Comments toggles flip with their checked state.
        w.act_toggle_bookmarks.setChecked(True)
        pump(2)
        check(failures, "chrome.bm_hide",
              w.act_toggle_bookmarks.text() == "Hide Bookmarks",
              f"checked label {w.act_toggle_bookmarks.text()!r}")
        w.left_panel._buttons["select"].click()
        pump(2)
        check(failures, "chrome.bm_show",
              w.act_toggle_bookmarks.text() == "Show Bookmarks",
              f"unchecked label {w.act_toggle_bookmarks.text()!r}")
        check(failures, "chrome.comments_show",
              w.act_show_comments.text() == "Show Comments",
              f"comments label {w.act_show_comments.text()!r}")
        w.act_show_comments.setChecked(True)
        pump(2)
        check(failures, "chrome.comments_hide",
              w.act_show_comments.text() == "Hide Comments",
              f"checked comments label {w.act_show_comments.text()!r}")
    finally:
        close(w)


# ---------------------------------------------------------------------------
# 21. M3: the throttled gesture-zoom funnel (clamp + coalesce)
# ---------------------------------------------------------------------------
def test_gesture_zoom_funnel(failures: list) -> None:
    w = open_window()
    try:
        v = w.view
        emissions: list = []
        v.zoomChanged.connect(lambda z: emissions.append(z))
        z0 = v.zoom
        v._apply_gesture_zoom(9.0)
        check(failures, "gz.deferred", abs(v.zoom - z0) < 1e-9,
              f"gesture zoom applied synchronously: {z0} -> {v.zoom}")
        check(failures, "gz.timer", v._gesture_timer.isActive(),
              "the 120 ms throttle timer is not running")
        check(failures, "gz.clamp_max", v._gesture_target == ZOOM_MAX,
              f"target {v._gesture_target} != ZOOM_MAX {ZOOM_MAX}")
        # A burst inside one throttle window only updates the TARGET.
        v._apply_gesture_zoom(0.01)
        check(failures, "gz.clamp_min", v._gesture_target == ZOOM_MIN,
              f"target {v._gesture_target} != ZOOM_MIN {ZOOM_MIN}")
        v._apply_gesture_zoom(9.0)
        wait_ms(300)
        check(failures, "gz.applied", abs(v.zoom - ZOOM_MAX) < 1e-9,
              f"throttle did not apply the LATEST target: {v.zoom}")
        check(failures, "gz.once", len(emissions) == 1,
              f"a 3-call burst produced {len(emissions)} zoom reloads "
              f"(want exactly 1)")
        check(failures, "gz.idle", v._gesture_target is None
              and not v._gesture_timer.isActive(),
              "the funnel did not return to idle after the apply")
    finally:
        close(w)


# ---------------------------------------------------------------------------
# 22. M3: pinch / smart-zoom / Cmd+wheel events
# ---------------------------------------------------------------------------
def test_gesture_events(failures: list) -> None:
    dev = QPointingDevice.primaryPointingDevice()

    def ng(gtype, value: float = 0.0) -> QNativeGestureEvent:
        pt = QPointF(50.0, 50.0)
        return QNativeGestureEvent(gtype, dev, 0, pt, pt, pt, value,
                                   QPointF(0.0, 0.0))

    def wheel(mods, dy: int = 120) -> QWheelEvent:
        pt = QPointF(50.0, 50.0)
        return QWheelEvent(pt, pt, QPoint(0, 0), QPoint(0, dy),
                           Qt.NoButton, mods, Qt.NoScrollPhase, False)

    w = open_window()
    try:
        v = w.view
        emissions: list = []
        v.zoomChanged.connect(lambda z: emissions.append(z))
        v.set_zoom(1.0)
        pump(2)
        emissions.clear()
        # Pinch: Begin seeds with the live zoom, Zoom ticks accumulate
        # *(1+value), End forces the final apply IMMEDIATELY.
        check(failures, "ev.begin",
              v.viewportEvent(ng(Qt.NativeGestureType.BeginNativeGesture)),
              "BeginNativeGesture was not consumed")
        v.viewportEvent(ng(Qt.NativeGestureType.ZoomNativeGesture, 0.5))
        v.viewportEvent(ng(Qt.NativeGestureType.ZoomNativeGesture, 0.2))
        check(failures, "ev.accumulate",
              v._gesture_target is not None
              and abs(v._gesture_target - 1.8) < 1e-9,
              f"pinch target {v._gesture_target} != 1.0*1.5*1.2")
        v.viewportEvent(ng(Qt.NativeGestureType.EndNativeGesture))
        check(failures, "ev.end_immediate", abs(v.zoom - 1.8) < 1e-6,
              f"EndNativeGesture did not apply immediately: {v.zoom}")
        check(failures, "ev.end_once", len(emissions) == 1,
              f"the pinch produced {len(emissions)} reloads (want 1)")
        # SmartZoom (two-finger double-tap) toggles fit-width <-> 100%.
        v.viewportEvent(ng(Qt.NativeGestureType.SmartZoomNativeGesture))
        pump(2)
        check(failures, "ev.smart_fit", v._zoom_mode == "fit_width",
              f"SmartZoom from fixed left mode {v._zoom_mode!r}")
        v.viewportEvent(ng(Qt.NativeGestureType.SmartZoomNativeGesture))
        pump(2)
        check(failures, "ev.smart_back",
              v._zoom_mode == "fixed" and abs(v.zoom - 1.0) < 1e-9,
              f"SmartZoom from fit_width left ({v._zoom_mode!r}, {v.zoom})")
        # Cmd+wheel zooms 1.1 per notch through the throttle.
        v.set_zoom(1.0)
        pump(2)
        emissions.clear()
        v.wheelEvent(wheel(Qt.ControlModifier))
        check(failures, "ev.wheel_target",
              v._gesture_target is not None
              and abs(v._gesture_target - 1.1) < 1e-9
              and abs(v.zoom - 1.0) < 1e-9,
              f"Cmd+wheel target/zoom: {v._gesture_target}/{v.zoom}")
        wait_ms(300)
        check(failures, "ev.wheel_applied",
              abs(v.zoom - 1.1) < 1e-9 and len(emissions) == 1,
              f"Cmd+wheel apply: zoom {v.zoom}, {len(emissions)} reloads")
        # A PLAIN wheel stays a scroll: no zoom, no pending target.
        zb = v.zoom
        v.wheelEvent(wheel(Qt.NoModifier, dy=-120))
        pump(2)
        check(failures, "ev.plain_wheel",
              abs(v.zoom - zb) < 1e-9 and v._gesture_target is None,
              "an unmodified wheel changed the zoom / queued a target")
    finally:
        close(w)

    # No document: gestures fall through untouched.
    w0 = open_window(path=None)
    try:
        v0 = w0.view
        z0 = v0.zoom
        v0.viewportEvent(ng(Qt.NativeGestureType.ZoomNativeGesture, 0.5))
        v0.wheelEvent(wheel(Qt.ControlModifier))
        pump(2)
        check(failures, "ev.no_doc",
              abs(v0.zoom - z0) < 1e-9 and v0._gesture_target is None,
              "gesture zoom ran with no document open")
    finally:
        close(w0)


# ---------------------------------------------------------------------------
# 23. M3: session save + restore through the _settings seam
# ---------------------------------------------------------------------------
def test_session_restore(failures: list) -> None:
    td = tempfile.mkdtemp(prefix="nav_session_")
    ini = os.path.join(td, "prefs.ini")
    w = open_window(path=None)
    try:
        w._settings = ini_settings(ini)     # BEFORE any open: temp ini only
        w.open_path(OUTLINE_DOC)
        w.open_path(THREE_PAGE)
        pump(4)
        w._on_tab_activated(0)
        pump(2)
        w._set_view_page(2)
        w.set_zoom(2.0)
        pump(2)
        w._save_session()
        raw = QSettings(ini, QSettings.Format.IniFormat).value(
            w._SESSION_KEY)
        if not check(failures, "sess.written", bool(raw),
                     "_save_session wrote nothing to the temp ini"):
            return
        state = json.loads(raw)
        check(failures, "sess.files",
              state.get("files") == [OUTLINE_DOC, THREE_PAGE],
              f"saved files {state.get('files')!r}")
        check(failures, "sess.active", state.get("active") == 0,
              f"saved active {state.get('active')!r}")
        check(failures, "sess.page",
              state.get("pages") == {OUTLINE_DOC: 2},
              f"saved pages {state.get('pages')!r}")
        check(failures, "sess.zoom", state.get("zoom") == 2.0,
              f"saved zoom {state.get('zoom')!r}")
    finally:
        close(w)

    w2 = open_window(path=None)
    try:
        w2._settings = ini_settings(ini)
        w2._restore_session()
        pump(6)
        check(failures, "sess.count", w2.workspace.count == 2,
              f"restored {w2.workspace.count} tabs (want 2)")
        paths = [w2.workspace.document(i).path
                 for i in range(w2.workspace.count)]
        check(failures, "sess.paths", paths == [OUTLINE_DOC, THREE_PAGE],
              f"restored paths {paths!r}")
        check(failures, "sess.active_restored",
              w2.workspace.active_index == 0,
              f"restored active {w2.workspace.active_index}")
        check(failures, "sess.page_restored", w2.view_page_index() == 2,
              f"restored page {w2.view_page_index()}")
        check(failures, "sess.zoom_restored",
              abs(w2.view_zoom() - 2.0) < 1e-9,
              f"restored zoom {w2.view_zoom()}")
        # Restore is a NO-OP on a non-empty workspace (CLI opens win).
        w2._restore_session()
        pump(2)
        check(failures, "sess.noop", w2.workspace.count == 2,
              f"a second restore re-opened tabs: {w2.workspace.count}")
    finally:
        close(w2)
        shutil.rmtree(td, ignore_errors=True)


# ---------------------------------------------------------------------------
# 24. M3: session edge cases + the close-path write contract
# ---------------------------------------------------------------------------
def test_session_edges(failures: list) -> None:
    td = tempfile.mkdtemp(prefix="nav_sessedge_")
    try:
        # A missing-file entry (and the active index pointing AT it) is
        # skipped without error; the surviving file still opens.
        ini = os.path.join(td, "missing.ini")
        missing = os.path.join(td, "gone.pdf")
        # zoom_mode "fixed" so the explicit 1.7 zoom is restored (a fit-page
        # session would instead re-fit to the window, by design).
        state = {"files": [missing, THREE_PAGE], "active": 0,
                 "pages": {missing: 3}, "zoom": 1.7, "zoom_mode": "fixed"}
        s = QSettings(ini, QSettings.Format.IniFormat)
        s.setValue("session/state", json.dumps(state))
        s.sync()
        w = open_window(path=None)
        try:
            w._settings = ini_settings(ini)
            w._restore_session()
            pump(6)
            check(failures, "edge.skip", w.workspace.count == 1
                  and w.workspace.document(0).path == THREE_PAGE,
                  f"missing-file session restored {w.workspace.count} tabs")
            check(failures, "edge.zoom", abs(w.view_zoom() - 1.7) < 1e-9,
                  f"zoom not restored around the missing file: "
                  f"{w.view_zoom()}")
            check(failures, "edge.page", w.view_page_index() == 0,
                  "a page keyed to the MISSING file was applied")
        finally:
            close(w)

        # Broken JSON / an absent key never raise and open nothing.
        for tag, ini_name, raw in (("broken", "broken.ini", "{not json"),
                                   ("absent", "absent.ini", None)):
            p = os.path.join(td, ini_name)
            if raw is not None:
                s = QSettings(p, QSettings.Format.IniFormat)
                s.setValue("session/state", raw)
                s.sync()
            w = open_window(path=None)
            try:
                w._settings = ini_settings(p)
                w._restore_session()     # must not raise
                pump(2)
                check(failures, f"edge.{tag}", w.workspace.is_empty,
                      f"a {tag} session opened {w.workspace.count} tabs")
            finally:
                close(w)

        # The natural close path writes the session through the seam...
        ini2 = os.path.join(td, "close.ini")
        w = open_window(path=None)
        w._settings = ini_settings(ini2)
        w.open_path(OUTLINE_DOC)
        pump(4)
        w.close()       # clean doc: no dirty guard, real closeEvent path
        pump(2)
        raw = QSettings(ini2, QSettings.Format.IniFormat).value(
            "session/state")
        check(failures, "edge.close_writes",
              bool(raw) and OUTLINE_DOC in json.loads(raw)["files"],
              f"a natural close wrote {raw!r}")
        # ...and the suppressed teardown path writes NOTHING (every suite
        # closes through it -- the real prefs stay untouched).
        ini3 = os.path.join(td, "suppressed.ini")
        w = open_window(path=None)
        w._settings = ini_settings(ini3)
        w.open_path(OUTLINE_DOC)
        pump(4)
        close(w)
        pump(2)
        check(failures, "edge.suppressed_silent",
              QSettings(ini3, QSettings.Format.IniFormat).value(
                  "session/state") is None,
              "the _suppress_close_guard path wrote a session")
    finally:
        shutil.rmtree(td, ignore_errors=True)


# ---------------------------------------------------------------------------
# 25. M3: recents are app-only (mdfind DELETED), filtered, fast
# ---------------------------------------------------------------------------
def test_recents_app_only(failures: list) -> None:
    # The Spotlight merge is gone from the SOURCE, not just dormant: no
    # mdfind invocation, no subprocess import on the UI thread. (The
    # docstring may still NAME mdfind as the deleted design -- the check
    # targets executable references only.)
    src = inspect.getsource(MainWindow._recent_paths)
    check(failures, "rec.no_mdfind",
          '"mdfind"' not in src and "'mdfind'" not in src
          and "subprocess." not in src and "import subprocess" not in src,
          "_recent_paths still invokes mdfind/subprocess")

    td = tempfile.mkdtemp(prefix="nav_recents_")
    ini = os.path.join(td, "prefs.ini")
    real_a = os.path.join(td, "onboarding-checklist.pdf")
    real_b = os.path.join(td, "benefits-summary.pdf")
    for p in (real_a, real_b):
        with open(p, "w") as fh:
            fh.write("synthetic")
    missing = os.path.join(td, "old-agenda.pdf")
    w = open_window(path=None)
    try:
        w._settings = ini_settings(ini)
        w._recent_store().setValue(
            w._RECENT_KEY, [real_a, missing, OUTLINE_DOC, real_b])
        w._recent_store().sync()
        # With the REAL skip prefixes everything here is hidden (repo
        # fixtures + temp files): the list shows ONLY paths the app vouches
        # for -- and returns instantly (no subprocess to wait on).
        t0 = time.perf_counter()
        got = w._recent_paths()
        dt = time.perf_counter() - t0
        check(failures, "rec.fast", dt < 0.2,
              f"_recent_paths took {dt:.3f}s (mdfind would)")
        check(failures, "rec.filtered", got == [],
              f"repo/temp paths leaked through the filter: {got!r}")
        # Relax ONLY the temp exclusion through the seam: store order kept,
        # the missing file dropped, the repo fixture still hidden.
        w._recent_skip_prefixes = lambda: [REPO]
        got = w._recent_paths()
        check(failures, "rec.app_only", got == [real_a, real_b],
              f"recents {got!r} != app-opened {[real_a, real_b]!r}")
        # Duplicates dedup to the first (most recent) occurrence.
        w._recent_store().setValue(w._RECENT_KEY, [real_a, real_a, real_b])
        got = w._recent_paths()
        check(failures, "rec.dedup", got == [real_a, real_b],
              f"duplicate store entries leaked: {got!r}")
    finally:
        close(w)
        shutil.rmtree(td, ignore_errors=True)


# ---------------------------------------------------------------------------
# 26. M3: empty-state recents -- folder rows + Clear Recents
# ---------------------------------------------------------------------------
def test_empty_state_recents(failures: list) -> None:
    td = tempfile.mkdtemp(prefix="nav_empty_")
    ini = os.path.join(td, "prefs.ini")
    real_a = os.path.join(td, "onboarding-checklist.pdf")
    real_b = os.path.join(td, "benefits-summary.pdf")
    for p in (real_a, real_b):
        with open(p, "w") as fh:
            fh.write("synthetic")
    w = open_window(path=None)
    try:
        w._settings = ini_settings(ini)
        w._recent_skip_prefixes = lambda: [REPO]
        w._recent_store().setValue(w._RECENT_KEY, [real_a, real_b])
        w._recent_store().sync()
        w._show_empty_state()
        flush_deferred()
        rows = recent_rows(w)
        if not check(failures, "empty.rows", len(rows) == 2,
                     f"{len(rows)} recent rows (want 2)"):
            return
        # Read the source path off each card (the visible name/folder labels
        # are elided to the card width, so they are not exact-match safe).
        names = [os.path.basename(r.path) for r in rows]
        folders = [os.path.dirname(r.path) for r in rows]
        tips = [r.toolTip() for r in rows]
        check(failures, "empty.names",
              names == ["onboarding-checklist.pdf", "benefits-summary.pdf"],
              f"row names {names!r}")
        check(failures, "empty.folders", folders == [td, td],
              f"row folders {folders!r}")
        check(failures, "empty.tooltips", tips == [real_a, real_b],
              "tooltips do not carry the full paths")
        check(failures, "empty.clear_shown",
              w.empty_state._clear_link.isVisibleTo(w.empty_state),
              "Clear Recents link hidden while the list shows")
        # The folder line home-abbreviates (display-only synthetic path).
        w.empty_state.set_recents(
            [os.path.expanduser("~/Documents/Forms/onboarding.pdf")])
        flush_deferred()
        rows = recent_rows(w)
        check(failures, "empty.abbrev",
              len(rows) == 1 and rows[0].findChild(
                  QLabel, "RecentFolder").text() == "~/Documents/Forms",
              "folder line is not home-abbreviated")
        # Clear Recents: empties the stubbed store, the rows, the link.
        w._show_empty_state()
        flush_deferred()
        w.empty_state._clear_link.click()
        flush_deferred()
        left = w._recent_store().value(w._RECENT_KEY, []) or []
        check(failures, "empty.cleared_store", list(left) == [],
              f"Clear Recents left {left!r} in the store")
        check(failures, "empty.cleared_rows", len(recent_rows(w)) == 0,
              "Clear Recents left rows in the empty state")
        check(failures, "empty.cleared_link",
              not w.empty_state._gallery.isVisibleTo(w.empty_state)
              and w.empty_state._hero.isVisibleTo(w.empty_state),
              "Clear Recents did not fall back to the hero call-to-action")
    finally:
        close(w)
        shutil.rmtree(td, ignore_errors=True)


# ---------------------------------------------------------------------------
# 27. M3: multi-file drag-drop -- canvas + window-wide
# ---------------------------------------------------------------------------
def test_multi_drop(failures: list) -> None:
    td = tempfile.mkdtemp(prefix="nav_drop_")
    notes = os.path.join(td, "notes.txt")
    with open(notes, "w") as fh:
        fh.write("not a pdf")
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(OUTLINE_DOC),
                  QUrl.fromLocalFile(THREE_PAGE),
                  QUrl.fromLocalFile(notes)])

    def enter_ev() -> QDragEnterEvent:
        ev = QDragEnterEvent(QPoint(40, 40), Qt.CopyAction, mime,
                             Qt.NoButton, Qt.NoModifier)
        ev.ignore()
        return ev

    def drop_ev() -> QDropEvent:
        return QDropEvent(QPointF(40, 40), Qt.CopyAction, mime,
                          Qt.NoButton, Qt.NoModifier)

    # Canvas drop: BOTH PDFs open (the .txt is ignored), affordance resets.
    w = open_window(path=None)
    try:
        ev = enter_ev()
        w.canvas.dragEnterEvent(ev)
        check(failures, "drop.canvas_enter",
              ev.isAccepted() and w.canvas._drag_active,
              "the canvas did not accept a two-PDF drag")
        w.canvas.dropEvent(drop_ev())
        pump(6)
        paths = [w.workspace.document(i).path
                 for i in range(w.workspace.count)]
        check(failures, "drop.canvas_tabs",
              paths == [OUTLINE_DOC, THREE_PAGE],
              f"canvas drop opened {paths!r}")
        check(failures, "drop.affordance", not w.canvas._drag_active,
              "the drop affordance stayed on after the drop")
    finally:
        close(w)

    # Window-wide drop (toolbar/dock surface): the delegating handlers
    # accept the same drag and open both tabs.
    w2 = open_window(path=None)
    try:
        ev = enter_ev()
        w2.dragEnterEvent(ev)
        check(failures, "drop.window_enter",
              ev.isAccepted() and w2.canvas._drag_active,
              "the WINDOW did not accept a two-PDF drag")
        w2.dropEvent(drop_ev())
        pump(6)
        check(failures, "drop.window_tabs", w2.workspace.count == 2,
              f"window drop opened {w2.workspace.count} tabs")
        # A drag with no PDFs is ignored on both surfaces.
        mime2 = QMimeData()
        mime2.setUrls([QUrl.fromLocalFile(notes)])
        ev2 = QDragEnterEvent(QPoint(40, 40), Qt.CopyAction, mime2,
                              Qt.NoButton, Qt.NoModifier)
        ev2.ignore()
        w2.dragEnterEvent(ev2)
        check(failures, "drop.non_pdf",
              not ev2.isAccepted() and not w2.canvas._drag_active,
              "a non-PDF drag was accepted")
    finally:
        close(w2)
        shutil.rmtree(td, ignore_errors=True)


# ---------------------------------------------------------------------------
# 28. M3: the new chrome icons render and are wired
# ---------------------------------------------------------------------------
def test_new_icons(failures: list) -> None:
    for name in ("info", "keyboard", "cut", "copy", "paste",
                 "clear_recent"):
        check(failures, f"icon.{name}",
              not make_icon(name).pixmap(24, 24).isNull(),
              f"make_icon({name!r}) rendered a null pixmap")
    w = open_window(path=None)
    try:
        for act_name, _icon in (("act_about", "info"),
                                ("act_shortcuts", "keyboard"),
                                ("act_cut", "cut"), ("act_copy", "copy"),
                                ("act_paste", "paste")):
            check(failures, f"icon.wired_{act_name}",
                  not getattr(w, act_name).icon().isNull(),
                  f"{act_name} carries no icon")
    finally:
        close(w)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> int:
    print("Navigation M1+M2+M3 harness (outline APIs + bookmarks panel + "
          "menu completion + gestures/session/recents/drop, offscreen)\n")
    failures: list[str] = []
    for fn in (test_outline_read, test_toc_survives_save, test_set_outline,
               test_outline_validation, test_delete_page_dangling,
               test_panel_tree, test_panel_jump, test_panel_add_undo,
               test_panel_rename_delete, test_panel_dangling,
               test_chrome_census,
               test_menu_completion, test_shortcut_reachability,
               test_shortcut_uniqueness, test_cheatsheet,
               test_about_dialog, test_open_recent_menu,
               test_window_doc_list, test_keys_and_clipboard_enablement,
               test_window_chrome,
               test_gesture_zoom_funnel, test_gesture_events,
               test_session_restore, test_session_edges,
               test_recents_app_only, test_empty_state_recents,
               test_multi_drop, test_new_icons):
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

    print("PASSED -- the fixture outline reads/round-trips through "
          "outline()/set_outline() (one structural op each, "
          "guard-before-snapshot validation, undo/redo restoring), the TOC "
          "survives open->edit->save on the committed fixture, delete_page "
          "remaps targets and keeps dangling entries at -1, and the "
          "BookmarkPanel (left stack index 3) nests/jumps/adds/renames/"
          "deletes with one StructuralCommand per gesture, dimmed dangling "
          "rows, and a View-menu toggle in lockstep with the strip. M2: the "
          "7-menu skeleton is COMPLETE (Open Recent rebuilt per show with "
          "folder-disambiguated rows + Clear Menu, Window's open-document "
          "list with the active checkmark, Help's generated cheatsheet + "
          "About, Cmd+W pinned); every shortcut-bearing action is "
          "menubar-reachable and unambiguous; V/E key clicks flip the strip "
          "tool and the ws2 clipboard actions keep their state routing; "
          "toggle labels flip Show<->Hide and the window title carries the "
          "[*] marker + proxy-icon file path. M3: pinch + Cmd+wheel funnel "
          "through ONE clamped, 120 ms-throttled gesture-zoom apply (End "
          "flushes immediately, SmartZoom toggles fit-width<->100%, plain "
          "scroll untouched); the session (files/active/page/zoom) saves on "
          "the guarded close and restores on an argv-less launch through "
          "the _settings temp-ini seam, skipping missing files and "
          "swallowing broken state; recents are app-only (the mdfind "
          "Spotlight merge is DELETED) and fast, with filename+folder "
          "empty-state rows and a Clear Recents link; multi-PDF drops open "
          "every file (canvas AND window-wide); the new chrome icons "
          "render and are wired.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
