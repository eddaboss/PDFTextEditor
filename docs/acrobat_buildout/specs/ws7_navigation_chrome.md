# WS7 — Navigation, Menus & Chrome

Workstream: `ws7_navigation_chrome`. Repo `/Users/edward/Documents/GitHub/PDFTextEditor`,
venv `.venv` (Python 3.13), stack LOCKED to PySide6 + PyMuPDF (`fitz`) + stdlib. No new pip installs.
Test invocation: `cd <repo> && QT_QPA_PLATFORM=offscreen .venv/bin/python tests/<name>.py` (exit 0 = pass).
Keep ALL suites green after every milestone: test_app, test_font_fidelity, test_editor_model, test_editor,
test_pages, test_reflow, test_richtext, plus test_redesign and test_text_tools (also green today — run them).

User: Edward, HR manager, reuses PDF templates (certificates, agendas, forms). The soul of the app is
invisible in-place edits; nothing here may degrade screen==file parity or silently lose document data.
(The bookmark-wipe data-loss fix this spec discovered was moved forward by the build plan: ws6 M1 carries
the TOC; this workstream verifies it and builds the bookmark UX on top.)

OWNERSHIP: this workstream owns the menu/toolbar ORGANIZATION REGISTRY (the §M2 table). The skeleton
itself is implemented by ws1 M4d from that table; other workstreams insert actions at the reserved
anchors. Arbitration rule: if two workstreams want the same slot, the §M2 table wins; extend it here,
do not fork it.

## 0. Feasibility probes (run on this venv, 2026-06-09, PyMuPDF 1.27.2.3, PySide6 6.11.1)

- `fitz.Document.get_toc` / `set_toc`: EXIST. `set_toc([[1,'Alpha',1],[2,'Beta',2],[1,'Gamma',4]])`
  round-trips exactly through `get_toc()` and through `fitz.open('pdf', doc.tobytes())`.
- `insert_pdf` does NOT carry the TOC: source with 3 entries -> `dst.insert_pdf(src)` -> `dst.get_toc() == []`.
  Confirms the documented open-path bug (document.py:486-489).
- Structural-op remapping (on a doc whose TOC was installed via `set_toc`):
  `move_page(0,3)` and `insert_page(1)` auto-remap TOC page numbers correctly;
  `delete_page(1)` keeps the entry with page == -1 (dangling, not removed).
- `set_toc` validates hierarchy: `[[2,'bad',1]]` raises `ValueError: hierarchy level of item 0 must be 1`.
  `set_toc([])` clears the outline.
- Qt: `Qt.NativeGestureType.ZoomNativeGesture` / `SmartZoomNativeGesture` exist; `QNativeGestureEvent`
  has `gestureType()` and `value()`. `QKeySequence('Ctrl+/')` valid (renders Cmd+/ on macOS).
  `QSettings(path, QSettings.Format.IniFormat)` stores/loads lists. `QAction.MenuRole.AboutRole` exists.
- Surfaces verified in code: `PageView` has NO `wheelEvent`/`viewportEvent`/`event` override (clean gesture
  surface). MainWindow already has a QSettings recent store (`_recent_store`, main_window.py:2210, org
  "eddaboss" app "PDF Text Editor") and `_add_recent` (2254); `_recent_paths` (2214) still shells out to
  `mdfind` synchronously. `act_close` uses `QKeySequence.Close` (1049). `pdftexteditor/__init__.py` has
  `__version__ = "0.1.0"`. Menubar today is File/Edit/Pages/View (`_build_menubar`, 1358).

---

## M1 — Outline APIs + Bookmarks panel

### Model (document.py)
1. **TOC carry: ALREADY LANDED.** The build plan moved the constructor carry into ws6 M1 (same
   lines as its metadata-carry fix), so by the time this workstream runs, `working` already holds
   the source TOC and saves preserve it. Do NOT re-implement it; this milestone VERIFIES it (the
   regression asserts below stay here) and builds the APIs/panel on top. The probe facts in §0
   remain the reference. Everything below is free by construction:
   - `save_as` (1670, opens `fitz.open('pdf', working.tobytes())` at 1700) now writes the outline (probe:
     tobytes round-trip preserves it). The no-op-save bookmark wipe is dead.
   - Structural snapshots (`_begin_structural`/`_finish_structural`, 2532) are whole-bytes — TOC undo free.
   - `render_with_edits` (2202) is unaffected (outline has no page ink) — WYSIWYG parity untouched.
   - rotate/move/insert/duplicate auto-remap TOC targets (probe); `delete_page` leaves page=-1 entries —
     we keep them (Acrobat keeps dead bookmarks too); UI renders them dimmed, click is a no-op.
2. **Accessors.** `def outline(self) -> list[list]:` returns `self.working.get_toc()` (fresh list each call,
   entries `[level, title, page1based]`). No caching; get_toc is cheap at this scale.
3. **Mutator (structural).** `def set_outline(self, entries: list[list]) -> None:` — validate locally
   (non-empty titles coerced to str; first entry level 1; each level in `1..prev+1`; pages int; raise
   `ValueError` before touching the doc), then run inside the structural funnel exactly like `rotate_page`
   (2566): `_begin_structural("Edit bookmarks")` -> `self.working.set_toc(entries or [])` ->
   `_finish_structural(...)`. Rationale: set_toc mutates `working`, and the repo invariant is "only
   structural ops and bakes mutate working" (document.py:478-489) — reuse the funnel rather than invent a
   third undo system. Cost: a bookmark edit bakes pending text edits and wipes fine-grained Qt history
   (existing, documented behavior of every structural op, main_window.py:2519). Accepted; bookmarks are
   occasional ops for this user. `_invalidate_caches` runs via the funnel — harmless and safe.

### UI — new file `pdftexteditor/ui/bookmark_panel.py` (pure chrome, FindReplacePanel pattern, find_panel.py:42)
- `BookmarkPanel(get_outline, set_outline, jump_to_page, current_page, parent=None, icon_factory=None)`.
  Injected callables only; NEVER imports document.py. objectName `BookmarkPanel`.
- Body: `QTreeWidget` objectName `BookmarkTree`, header hidden, items nested by entry level, each item
  stores `(title, page1)` in `Qt.UserRole`. Entries with page == -1 render with `theme.TEXT_SECONDARY`
  foreground and tooltip "Target page was deleted". Footer row: three flat icon buttons
  (objectNames `BookmarkAdd`, `BookmarkRename`, `BookmarkDelete`; icons `bookmark_add`, `text_edit`,
  `delete` via the injected icon factory).
- `refresh()` rebuilds the tree from `get_outline()`; expandAll; preserves selection by (title,page) best
  effort. Called by the window after open, tab switch, and every structural op (see wiring).
- Interactions: single click / Return on an item -> `jump_to_page(page1 - 1)` (skip page==-1).
  **Add**: new sibling after the selected item (or appended at top level), title `f"Page {current_page()+1}"`,
  page `current_page()+1`; insert into the tree, start `editItem` inline rename, then commit.
  **Rename**: `editItem` on selection; commit on `itemChanged`.
  **Delete**: remove the item AND its subtree; commit.
  Commit = flatten the tree depth-first into `[[depth+1, title, page], ...]` (tree depth makes hierarchy
  always valid for set_toc) and call `set_outline(entries)`. No modal dialogs anywhere (offscreen-test rule).
- LeftPanel (left_panel.py): append `("bookmarks", "bookmark", "Bookmarks (Cmd+Alt+B)")` to the END
  of `_TOOLS` (left_panel.py:35 — by now the strip also holds ws2's `select_text` and ws3's
  `comments`); add `install_bookmark_panel(panel)` mirroring
  `install_find_panel` (left_panel.py:128) at stack index 3 (inspector=0, find=1, ws3 comments=2);
  extend `_sync_content` (166) and the header
  label ("Bookmarks"). `toolSelected` already carries a string id — no signature change.

### Wiring (main_window.py)
- Build the panel in `_build_left_dock` (1672) with callables:
  `get_outline = lambda: self.document.outline() if self.document else []`,
  `set_outline = self._set_outline`, `jump_to_page = self._set_view_page` (2742-2801 region),
  `current_page = self.view_page_index`.
- `def _set_outline(self, entries):` -> `self._run_structural(lambda: self.document.set_outline(entries))`
  matching the existing funnel signature at main_window.py:2473. Undo/redo story: ONE `StructuralCommand`
  per bookmark add/rename/delete (label "Edit bookmarks"); `_sync_structural_undo_stack` (2519) already
  rebuilds the Qt stack; dirty/save enablement already covers structural depth (2901-2907).
- Refresh the panel from `_after_structural_op` (so undo/redo of bookmark + page ops update the tree),
  from `_activate_document` (2263), and from `open_path` (2188). Guard with `getattr` per the defensive
  wiring convention (1800-1829).
- New action `act_toggle_bookmarks` ("Show Bookmarks", checkable, `Cmd+Alt+B` — do NOT take Cmd+B/I,
  reserved for inline bold/italic by the text workstream) -> selects/deselects the "bookmarks" tool in
  LeftPanel. Lives in the View menu's panels section (the skeleton exists since ws1 M4d).
- Empty-state policy: bookmarks tool disabled with no document (`set_enabled_tools`, left_panel.py:180,
  and `_sync_actions`, 2840).

### Save / WYSIWYG story
Live: TOC lives on `working`; panel reads it directly. Save: `save_as` serializes `working` — outline
included automatically; zero changes to the bake pipeline (`_apply_page_edits` 1733 untouched).
WYSIWYG: no rendered ink change; the wysiwyg_rel_diff assertions in existing suites are the regression net.

---

## M2 — Menu bar completion, shortcuts, cheatsheet, About, chrome polish

### Menu skeleton (ALREADY LANDED — built by ws1 M4d from the table below)
The 7-menu skeleton with `self.menu_file/_edit/_view/_tools/_document/_window/_help` and the
`self.menu_anchors: dict[str, QAction]` separators was implemented by ws1 M4d. This table STAYS the
registry of record (extend it here when a new slot is needed; never fork it). By the time this
workstream runs, ws2 owns act_cut/act_copy/act_paste + V/E (already registered), and ws3/ws4/ws5/ws6
items sit at their anchors. This milestone delivers the DYNAMIC and missing pieces: Open Recent
submenu, Window-menu open-document list, the explicit Cmd+W pin on Close Tab, toggle-label flips,
editor-aware enablement refinements on ws2's copy/paste actions (disable while an inline editor is
mounted, via `editStarted`/`editFinished` — only if ws2 has not already done so), the cheatsheet,
About, and the chrome polish below.

| Menu | Contents (order) | Notes |
|---|---|---|
| File | Open… (Cmd+O) · **Open Recent ▸** (dynamic, from `_recent_paths`, + "Clear Menu") · sep · Save · Save As… · sep · anchor **`file_output`** (ws_output: Print…/Export…/Extract Text) · sep · Close Tab (**explicit `QKeySequence("Ctrl+W")`** = Cmd+W on mac; StandardKey.Close resolves to Ctrl+F4 on some platforms — pin it) | Open Recent rebuilt in `aboutToShow` |
| Edit | Undo · Redo · sep · Delete Box · sep · Find & Replace (Cmd+F) · sep · anchor **`edit_extra`** (ws2: Cut/Copy/Paste) | act_cut/copy/paste owned by ws2 M3 |
| View | Show/Hide Pages Sidebar (Cmd+Shift+P) · Show/Hide Bookmarks (Cmd+Alt+B) · anchor **`view_panels`** (ws_annotations: Comments panel) · sep · Zoom In/Out/Actual Size/Fit Page/Fit Width (existing acts) · sep · **Page Navigation ▸** (Next/Previous/First/Last/Go to Page… — existing window-only actions get menu homes) | toggle labels FLIP Show↔Hide on visibility change (fix static label, act_toggle_pages at 1308) |
| Tools | Select Tool (**V**) · Text Edit Tool (**E**) · Add Text (T) · sep · anchor **`tools_annotate`** (ws3: highlight/ink/shapes/notes) · anchor **`tools_objects`** (ws4: image/signature/stamp) · anchor **`tools_forms`** (ws5) | V/E owned by ws2 M1; skeleton + anchors by ws1 M4d |
| Document | Combine PDFs… · Extract Pages… · Split PDF… · sep · Rotate Right (Cmd+R) · Rotate Left (Cmd+Shift+R) · Duplicate Page · Insert Blank Page · Delete Page · sep · anchor **`doc_transform`** (ws_pages: Crop) · anchor **`doc_decorate`** (ws_document: Watermark/Header-Footer) · anchor **`doc_file`** (ws_document: Compress/Password/Metadata/Properties) | replaces the Pages menu; same QActions, handlers untouched (`_run_structural` funnel) |
| Window | Minimize (Cmd+M) · Zoom · sep · Next Tab (Ctrl+Tab) · Previous Tab (Ctrl+Shift+Tab) · sep · dynamic open-document list (checkmark on active; rebuilt in `aboutToShow`; activates tab via `workspace.switch`) | |
| Help | Keyboard Shortcuts… (**Cmd+/**) · About PDF Text Editor (MenuRole **AboutRole** -> migrates to the app menu on macOS) | |

- **V/E tool shortcuts**: OWNED BY ws2 M1 (already landed). This milestone only asserts they exist,
  live in the Tools menu, and stay safe while typing (Qt's ShortcutOverride lets the focused
  QGraphicsTextItem consume plain letters, the "T" mechanism). No re-registration.
- **act_copy / act_paste**: OWNED BY ws2 M3 (already landed, at the `edit_extra` anchor). This
  milestone only adds whatever enablement rules ws2 did not ship: disabled when no document, disabled
  while an inline editor is mounted (hook the already connected `editStarted`/`editFinished`,
  _wire_view 1800), act_copy additionally requires a selection (`selectionChanged`) — disabled QAction
  shortcuts do not fire, so the inline editor keeps Cmd+C/V. The view-level key handler
  (page_view.py:2216-2223) stays as-is (fallback).
- **Shortcuts cheatsheet** — new file `pdftexteditor/ui/shortcuts_dialog.py`. `ShortcutsDialog(menubar)`
  walks `menubar` recursively and lists every action with a non-empty shortcut, grouped by top-level menu:
  rows of (text stripped of `&`, `shortcut().toString(QKeySequence.NativeText)`). Two-column grid in a
  QScrollArea, objectName `ShortcutsDialog`, **non-modal** (`show()`, never `exec()` — offscreen-test rule,
  precedent tests/test_app.py:249). Cached instance on the window; `_show_shortcuts()` slot. Because it is
  generated from the live menubar it self-updates as other workstreams add actions — that is the point.
- **About dialog** — small non-modal QDialog (objectName `AboutDialog`): doc icon, "PDF Text Editor",
  `pdftexteditor.__version__`, "Local, private PDF editing." No AI attribution anywhere.
- **Chrome polish bundled here** (all owned by this WS): (a) toolbar Find button text/tooltip
  `"Find && Replace"` to stop the mnemonic eating the `&` (main_window.py:1165); (b) full-height
  `QToolBar::separator` QSS in theme.py (fixes floating tick marks); (c) delete the stale
  `QDockWidget#FormatDock` selector (theme.py:366-373, dock is `LeftToolDock`); (d) ensure window title =
  active doc name + `[*]` modified marker and call `setWindowFilePath` for the macOS proxy icon
  (`_title()`/`setWindowModified` exist at 2165/2369/2838 — verify and extend, don't duplicate).
- Menubar uses the same QActions as toolbar/strip; no handler changes; no model changes in M2.

---

## M3 — Zoom gestures, session restore, recents, drag-drop, icons/theme

### Zoom gestures (page_view.py — new code only, no existing overrides to fight)
- `viewportEvent(ev)` override: on `QEvent.NativeGesture`: `ZoomNativeGesture` -> accumulate
  `self._pinch_factor *= (1.0 + ev.value())`; `BeginNativeGesture` resets to current zoom;
  `EndNativeGesture` forces a final apply. `SmartZoomNativeGesture` (two-finger double-tap) toggles
  `set_zoom_mode('fit_width')` <-> `set_zoom(1.0)`.
- `wheelEvent(ev)`: if `ev.modifiers() & Qt.ControlModifier` (Cmd on macOS) -> zoom by
  `1.1 ** (angleDelta().y()/120)`; else `super().wheelEvent(ev)` (normal scroll).
- Both paths funnel into one **testable** method `def _apply_gesture_zoom(self, target: float) -> None:`
  clamp to ZOOM_MIN/ZOOM_MAX (page_view.py:99) and THROTTLE: a 120 ms single-shot QTimer applies the latest
  target via the existing `set_zoom` (727). Rationale: every `set_zoom` is a full `reload()`
  (page_view.py:732-736 pain point); throttling keeps pinch usable today, and the perf workstream's baked
  pixmap cache makes it smooth later — we do NOT depend on it. `set_zoom` already commits any open editor
  (`_flush_editor`) and restores the page+fraction scroll anchor — keep that, accept top-anchored zoom
  (cursor-centered zoom de-scoped).

### Session restore + settings seam (main_window.py)
- Refactor: `def _settings(self) -> QSettings:` returns `QSettings("eddaboss", "PDF Text Editor")` by
  default; `_recent_store` (2210) delegates to it. TESTS monkeypatch `_settings` to a temp
  `QSettings(path, QSettings.Format.IniFormat)` (probe: list round-trip works) so suites never touch
  Edward's real prefs.
- `def _save_session(self):` writes key `session/state` = `json.dumps({"files": [paths with a real path],
  "active": active_index, "pages": {path: view_page_index for active}, "zoom": view_zoom()})`.
  Call it in `closeEvent` (3048) just before the final `event.accept()` (after the dirty guard passes).
- `def _restore_session(self):` — only when the workspace is empty: parse the key, `open_path` each
  still-existing file (existing dedup + recents update), restore active tab, page, zoom; swallow all
  exceptions (a broken session must never block launch). Called from `main()` after `window.show()`
  unless CLI args supplied.
- `main.py`: accept `sys.argv[1:]` existing `.pdf` paths -> `open_path` each and SKIP session restore;
  also install an app-level event filter for `QEvent.FileOpen` (Finder "Open With") -> `open_path`.
  Keeps `launch.py`/PyInstaller contract intact (launch.py imports `pdftexteditor.main`; no new data files,
  so `PDFTextEditor.spec` needs no `datas=` change; new modules auto-bundle via `collect_submodules`).

### Recents + empty state
- **Delete the `mdfind` branch** of `_recent_paths` (2229-2238): synchronous subprocess on the UI thread,
  and it leaks system-wide Spotlight history into the app. Recents become app-opened-only from QSettings
  (store already exists). Keep the fixture/temp-dir filtering.
- EmptyState rows (826): filename in primary text + dimmed home-abbreviated folder (`~/Documents/Forms`)
  as a second line, full path tooltip — fixes indistinguishable duplicates. Add a small "Clear Recents"
  link under the list and a "Clear Menu" item in File > Open Recent (both call `_clear_recents()` which
  deletes the QSettings key and refreshes; non-destructive to files — no confirm needed).
- Drag-drop: extend `CanvasContainer` handlers (main_window.py:769-797) to open EVERY `.pdf` URL in the
  drop (multi-file -> multiple tabs), and `setAcceptDrops(True)` on MainWindow with delegating
  `dragEnterEvent`/`dropEvent` so drops on the toolbar/docks also work.

### Icons + theme (this WS owns conventions)
- `icons.py` `_ICONS` (icons.py:23) gains: `bookmark`, `bookmark_add`, `info`, `keyboard`, `copy`,
  `paste`, `clear_recent`. Same 24px-grid line language; document the conventions (viewBox 24, stroke
  width, round caps) in the `_ICONS` header comment so other workstreams' icons match. Append-only dict:
  never rename existing keys (make_icon falls back to empty QIcon on unknown names, icons.py:104).
- theme.py stays light-only; changes are the two QSS fixes from M2 plus any `BookmarkTree` styling via
  objectName selector. objectNames are load-bearing for QSS and tests — never rename existing ones.

---

## Test plan (synthetic fixtures only; fake neutral names)

New fixture builder `tests/fixtures/build_nav_fixture.py` -> `tests/fixtures/outline_doc.pdf`:
5 letter-size pages, base-14 text (no font deps), content like "Acme Corp Onboarding Agenda",
"Facilitator: Jordan Carter"; TOC `[[1,"Welcome",1],[2,"Agenda",2],[1,"Policies",4]]` via `set_toc`.
Append an idempotent section to `tests/fixtures/manifest.md` (precedent: build_reflow_fixtures.py).

New suite `tests/test_navigation.py` following the house harness exactly: `QT_QPA_PLATFORM=offscreen`
before Qt imports, repo root on sys.path, ONE module-global QApplication, `check(failures, tag, cond, msg)`,
`main()` returns 1 on failures; absolute fixture paths (do NOT copy test_richtext's cwd dependence);
`window._suppress_close_guard = True` before close; **no modal exec anywhere** (all new dialogs are
non-modal by design); temp outputs via `tempfile.mkdtemp`, cleaned up.

- **M1 asserts**: `PDFDocument(outline_doc).outline()` == fixture TOC; stage one text edit, `save_as(tmp)`,
  `fitz.open(tmp).get_toc()` == fixture TOC (THE data-loss regression); `set_outline` round-trip + saved
  file reflects it; `undo_structural()` restores prior TOC; `delete_page(1)` leaves remapped TOC with a
  -1 dangling entry (matches probe); ValueError on `[[2,'bad',1]]`; window-level: open in MainWindow,
  BookmarkPanel tree has 3 top-level+nested rows, simulated item click changes `view_page_index()`,
  panel Add then `undo_stack.undo()` (StructuralCommand) restores `doc.outline()`; dirty flag set after a
  bookmark edit. Re-run ALL existing suites (TOC carry touches `__init__` — test_pages/test_editor_model
  are the blast-radius net).
- **M2 asserts**: menubar titles exactly `["File","Edit","View","Tools","Document","Window","Help"]`;
  every QAction owned by the window that has a non-empty shortcut is reachable by walking the menubar,
  except a literal allowlist (zoom presets, PageField, tab-cycling) asserted in the test; no two ENABLED
  actions share a shortcut string; `window._show_shortcuts()` -> dialog `isModal() == False`, row count ==
  number of shortcut-bearing menu actions, no "&" in rendered names; About dialog non-modal and contains
  `__version__`; act_copy disabled during `begin_edit`, re-enabled with a selection after `commit_edit`;
  key V/E (via QTest.keyClick on the window) flip `left_panel.active_tool()`; Close Tab shortcut renders
  as Ctrl+W portable form; View toggle label flips after hiding the dock.
- **M3 asserts**: monkeypatched `_settings` -> temp ini; open two fixtures, set page, `_save_session()`;
  fresh MainWindow + same settings + `_restore_session()` -> workspace count/paths/active/page restored;
  missing-file session entry skipped without error; `_recent_paths` contains only app-opened files (and
  returns in <0.2 s — proves no mdfind); `_clear_recents` empties store + empty-state list;
  `view._apply_gesture_zoom(9.0)` clamps to ZOOM_MAX and after pumping the throttle timer
  (QEventLoop+QTimer pattern, tests/test_editor.py:224) `view.zoom` changed exactly once; synthesized
  `QDropEvent` with two file URLs on `CanvasContainer` opens two tabs; `make_icon("bookmark")` non-null
  pixmap. Re-run all suites.

---

## DE-SCOPE (deliberate)

OUT: OCR (global exclusion). TOC carry through `merge`/`extract_pages`/`split` (document.py:2648/2688 —
needs per-range filtering/remapping of entries; the open->edit->save path is the user's actual workflow;
follow-up ticket). Bookmark drag-reorder in the tree (set_outline supports it later; CRUD covers the need).
Bookmark destination x/y fidelity (page-level jumps only; `get_toc(simple=True)` form). Cursor-anchored /
animated zoom and a zoom slider (blocked on the perf workstream's baked-pixmap cache; presets+fit+gestures
suffice). Dark mode. Spotlight-wide recents (removed on purpose — UI-thread stall + privacy noise).
Restoring UNSAVED edits across sessions (the model stages edits in RAM by design; session restore reopens
files only). Help-book/manual content beyond the generated shortcut sheet.

## CONFLICT RISK (files other workstreams will also touch)

- `pdftexteditor/ui/main_window.py` — EVERY workstream adds actions. The skeleton landed with ws1 M4d;
  everyone targets `self.menu_*` attributes + `self.menu_anchors` (this spec's table is the registry
  of record). ws7 lands LAST among the UI workstreams, so M2's census test snapshots the final menus.
- `pdftexteditor/ui/icons.py` — append-only `_ICONS`; conventions comment lands in M3; no key renames.
- `pdftexteditor/ui/theme.py` / objectNames — QSS is objectName-coupled to tests; append selectors only.
- `pdftexteditor/document.py` `__init__`/`save_as` — the TOC carry lives in ws6 M1 (already landed);
  this workstream only adds outline()/set_outline() and must not disturb the constructor order
  (password gate, insert_pdf, metadata carry, TOC carry).
- `pdftexteditor/ui/page_view.py` — ws_annotations adds item types/press routing; my changes are confined
  to new `viewportEvent`/`wheelEvent` methods (verified absent today) — low collision.
- `pdftexteditor/ui/left_panel.py` — ws_annotations' comments panel will be stack index 3; the `_TOOLS`
  tuple grows append-only.
- `pdftexteditor/main.py` — ws_output (print) may also touch argv handling; coordinate on the FileOpen filter.
