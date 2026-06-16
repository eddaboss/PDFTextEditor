# WS1: Performance & Rendering Foundation

Workstream: `ws1_perf_foundation`. Goal: make the editor fast on real multi-page documents and
keep it fast as the Acrobat-clone features land on top, plus the minimal structural seams the
other workstreams need (overlay item registration, tool-mode dispatch, menu scaffolding).
**Zero user-visible behavior change except speed.** The soul of the app (invisible in-place
edits, screen == saved file) must hold byte-for-byte; every milestone gates on the full suite set.

Stack lock: PySide6 + PyMuPDF 1.27.2.3 (`fitz`) + stdlib. No new pip installs. Suites run as
`cd <repo> && QT_QPA_PLATFORM=offscreen .venv/bin/python tests/<name>.py` (exit 0 = pass).
Gate set for EVERY milestone: test_app, test_font_fidelity, test_editor_model, test_editor,
test_pages, test_reflow, test_richtext, PLUS the two extra green suites test_redesign and
test_text_tools (they exist and pass today; do not let them regress silently).

## Performance targets (from the measured audit, 60-page dense doc unless noted)

| Path | Today | Target | Milestone |
|---|---|---|---|
| Scroll re-entry onto an edited dense page | 34-106 ms | <= 5 ms (cache hit) | M2 |
| Zoom toggle back to a recently used zoom, edits staged | ~101 ms | <= 10 ms/page (hit) | M2 |
| Commit on a 40-line paragraph page (repaint_box) | ~125 ms | <= 60 ms | M1+M3 |
| render_with_edits, dense page, 2 edits | ~103 ms | <= 50 ms | M1+M3 |
| First MainWindow construction (cold font index) | ~304 ms | <= 200 ms | M4 |
| Plain unedited page render | ~1 ms | unchanged | guard |

Targets are engineering goals, not test assertions (CI boxes vary). Tests assert *mechanism*
(call counts, cache identity, parity), and print timings informationally.

## Feasibility probes (run 2026-06-09 against .venv, PyMuPDF 1.27.2.3; all verified)

- `fitz.Page.new_shape` exists; `fitz.Shape.insert_text` signature accepts `fontsize, fontname,
  fontfile, color, morph, rotate, render_mode` (same template as `page.insert_text`, which is
  itself a one-shot Shape: probe read of `pymupdf/__init__.py:15216` shows Shape.insert_text
  calls `page.insert_font` + builds the same BT/Tm/Tf/TJ content).
- Batching timing probe (200 runs of `'x y z'`, embedded Arial buffer registered once):
  `page.insert_text` x200 = **55.9 ms**; one `Shape` with 200 `insert_text` + single `commit()` =
  **9.6 ms** (5.8x); `fitz.TextWriter` (200 appends + 1 `write_text`) = **4.1 ms**.
- `fitz.TextWriter` works end-to-end (append with `fitz.Font(fontbuffer=...)` and
  `fitz.Font('helv')`, per-color writers, save + reopen extracts all words) BUT it embeds
  base-14 picks as Type0/Identity-H CID fonts (probe: `('cid','Type0','Helvetica')` in
  `get_fonts`), changing the file's font semantics vs today's built-in Type1 base-14. That is why
  M3 uses Shape batching (byte-semantics preserving) and TextWriter is DE-SCOPED.
- `fitz.Document.tobytes`, `fitz.Pixmap` samples access, `page.insert_font(fontname=, fontbuffer=)`
  all present (already in production use).
- `FontEngine._build_system_index` (font_engine.py:700) is pure fontTools + os, no Qt: safe to
  run off the GUI thread. (`load_qt_family` / QFontDatabase paths stay on the GUI thread.)

---

## M1: Model hot-path caching (document.py, font_engine.py)

No UI changes. Three cache fixes plus one new query API that M2 builds on.

### 1a. Persistent font resolution in the bake pipeline
Today `render_with_edits` constructs `FontEngine(out)` per frame (document.py:2246) and
`save_as` does the same (document.py:1703 region), so the resolve cache
(font_engine.py:345-349) is cold every repaint: ~5-7 ms per edit per frame, plus a repeated
`extract_font` pdf_load_stream of the embedded buffer.

Change: pass the long-lived `self.font_engine` (rebound on every working-doc mutation by
`_invalidate_caches`, document.py:2527) into `_apply_page_edits` from both `render_with_edits`
(document.py:2247) and `save_as` (the `engine = FontEngine(out)` line). Safe because:
- the throwaway `out` is `fitz.open("pdf", self.working.tobytes())`, byte-identical to
  `working`, and `resolve`/`resolve_family`/`fitz_font_for` only READ the bound doc
  (embedded_xref/extract_embedded); same bytes, same answers.
- precedent: the wrap path already resolves against `self.font_engine`
  (`_wrap_for_edit_cached`, document.py:1831), so working-vs-copy resolution parity is already a
  relied-upon invariant.
- `_register_resolved` (document.py:2362) keeps registering fonts on the OUT page; it only uses
  the engine for `system_record_for` (class-level index, doc-independent).
- Subtlety to verify in review: today's `FontEngine(out)` resolves neighbors AFTER
  `apply_redactions` ran on `out`; `self.font_engine` resolves against pre-redaction `working`.
  Render and save change together through the single pipeline, so screen == file cannot drift;
  tier decisions are locked by test_app's scenario table and test_font_fidelity.
- Do NOT touch `_bake_doc` (document.py:2435): its target can be a FOREIGN doc (merge), where
  `FontEngine(target)` is required.

### 1b. Memoize `_overlapping_neighbors`
The full rawdict walk (document.py:2257) costs 6-21.5 ms per frame and its inputs only change
when working content or the edit set changes. Add an instance-level wrapper
`_overlapping_neighbors_cached(out, page_index, edited_keys, redact_rects)` used by
`_apply_page_edits` (call site document.py:1774): key =
`(self._spans_generation, page_index, frozenset(edited_keys), tuple(map(tuple, redact_rects)))`,
value = the neighbors list (treat as read-only, same convention as spans()). Store in a new
`self._neighbors_cache: dict` initialized beside `_wrap_cache` (document.py:500-510), cleared in
`_invalidate_caches` (document.py:2521), simple FIFO cap of 32 entries. The static
`_overlapping_neighbors` stays as-is for `_apply_page_edits_for` (foreign-doc bake,
document.py:2460).

### 1c. `face_bytes` memo
`fitz_font_for` re-extracts the .ttc face on EVERY SYSTEM-tier call (font_engine.py:633-635 ->
face_bytes font_engine.py:789, a full TTCollection parse+save). Add a module-level
`_face_bytes_cache: dict[(path, face_index), bytes]` inside font_engine.py consulted by
`face_bytes`. Process-lifetime, consistent with the existing class-level font caches
(font_engine.py:227-229). Benefits both wrap measurement and `_register_resolved`
(document.py:2396).

### 1d. New API: `PDFDocument.render_signature(page_index) -> tuple`
The cache key contract for M2. Returns a hashable tuple that is EQUAL iff
`render_with_edits(page_index, z)` is guaranteed to produce the same pixels at any fixed z:
`(self._spans_generation,` per-page sorted tuple of Edit signatures, per-page sorted tuple of
NewBox signatures`)`. Edit signature = `(span.key, effective_text, style tuple
(family,size,color,bold,italic), move, round(scale,6), deleted, alignment, line_spacing, runs)`
(field set mirrors `_wrap_for_edit_cached`'s sig at document.py:1819-1827 plus move/deleted).
NewBox signature = `(box_id, origin, text, font_family, size, color, bold, italic, deleted)`
(dataclass fields, document.py:352-370). `_spans_generation` (document.py:500) finally becomes
load-bearing: it already bumps in `_invalidate_caches`, which runs after every bake, structural
op, and structural undo/redo (document.py:2562, 2797, 2818). Fine-grained undo/redo mutate
`_edits`/`_new_boxes` directly, so the signature changes with them automatically: no hook needed.
ARCHITECTURE RULE (cross-workstream): `render_signature` is the ONE cache-key registry. Any later
workstream that adds staged per-page render state (ws3 annots/overrides, ws4 image boxes +
xim deletes, ws5 form values) MUST fold its sorted per-page state into this tuple in the same
change that adds the state, and assert a cache miss after its mutators in its own suite. No
side-channel signature APIs (no separate annot_state_sig etc.).

### Undo / save / WYSIWYG story (M1)
No new mutations, no new commands. Undo/redo at both granularities flow through existing state
that the signature and caches key on. save_as output must stay byte-equivalent in *rendered
pixels* (the suites' wysiwyg_match rel-diff < 0.02 and basefont-set checks are the gate; exact
byte equality is not promised today either because timestamps/xref order vary).

### Test plan (M1)
New suite `tests/test_perf_foundation.py` (harness pattern of tests/test_app.py:35: offscreen
env first, one module-global QApplication, `check(failures, ...)` accumulation, exit 0; absolute
fixture paths; never write into tests/fixtures/ or tests/screenshots/; temp files via
`tempfile.mkdtemp`). Fixtures: existing `form_like.pdf` + `paragraphs.pdf` +
`multipage_body.pdf` only; plus a tempfile-built 30-page synthetic doc generated in-test with
fitz using fake names ("Jordan Carter", "Acme Corp"), mirroring tests/test_pages.py:106-117.
Asserts:
- A1: with 2 staged edits, `render_with_edits` twice -> identical `pix.samples` bytes; a
  counting monkeypatch on `document.FontEngine` records ZERO constructions during the calls.
- A2: `_neighbors_cache` gains exactly one entry across 3 identical renders; a `stage_edit`
  changes the key (second entry); `_invalidate_caches` empties it.
- A3 (parity keystone): `render_with_edits` ink vs saved-file render ink, rel diff < 0.02, for a
  span edit, a paragraph reflow edit, and a NewBox add (copy the wysiwyg helper from
  tests/test_editor.py:208).
- A4: `render_signature` is stable across repeated calls; changes on stage_edit/set_style/
  move_box/undo/redo; changes generation on rotate_page and on undo_structural.
- Full gate set green (test_editor_model's all-families sweep is the tier-parity regression net
  for 1a).

---

## M2: Baked-pixmap cache in the view (page_view.py)

The single biggest user-felt win: stop re-baking edited pages on every scroll re-entry, zoom
revisit, and tab switch.

### Design
New module `pdftexteditor/ui/render_cache.py` (lives under the package so PyInstaller's
`collect_submodules('pdftexteditor')`, PDFTextEditor.spec:18, bundles it automatically; datas
untouched). Class `PageRenderCache`:
- key: `(page_index, round(zoom * dpr, 4), document.render_signature(page_index))`
- value: the rendered `QImage` (already DPR-tagged; `QPixmap.fromImage` on reuse is cheap)
- LRU, default capacity 12 entries (covers the lazy band of ~4-8 materialized pages plus one
  zoom revisit; an A4 at zoom 2 x dpr 2 is ~25 MB, so worst case ~300 MB transiently, in line
  with the measured 232-265 MB RSS today; capacity is a constructor arg for tuning)
- `purge_page(page_index)` and `clear()`.

Wiring in PageView:
- `_materialize_page` (page_view.py:965): before `self.document.render_with_edits(...)` at
  page_view.py:973, look up the cache; on miss, render, build the QImage exactly as today
  (page_view.py:974-976), then insert. Everything else (sheet/shadow/hotspots/boxes rebuild,
  page_view.py:990-1030) is unchanged and always re-runs (it is cheap and reads staged geometry).
- `repaint_box` (page_view.py:1462): call `cache.purge_page(box.page_index)` before the
  dematerialize/rematerialize cycle. Correctness does not need this (the signature changed), but
  it frees stale entries immediately.
- `set_document` (page_view.py:652) and `clear_document` (page_view.py:671): `cache.clear()`
  (tab switches swap documents on one shared view; never let doc A's pixels key-collide with
  doc B: the signature does not encode the document).
- The live-editor path is NOT cached: any future call carrying `exclude_span` bypasses the cache
  entirely (today `_materialize_page` never passes it; keep the bypass as a guard for whoever
  adds one).
- Zoom (`reload`, page_view.py:835) needs no changes: old-zoom entries age out via LRU, and
  revisiting a zoom hits. Fit-mode resize storms (page_view.py:781-784) stop re-baking pages
  whose width did not change, and bounded-LRU caps the rest.

### Undo / save / WYSIWYG story (M2)
Undo/redo (both stacks) change `render_signature`, so hits are impossible against stale state;
`repaint_box` already runs on every command redo/undo (main_window.py BoxCommand). save_as never
touches this cache (it renders its own copy). WYSIWYG: a cache hit re-displays bytes produced by
the same `render_with_edits` pipeline, asserted by pixel-equality tests below.

### Test plan (M2)
Extend `tests/test_perf_foundation.py`:
- B1: wrap the active document's `render_with_edits` with a counting shim; materialize page 0
  with one staged edit, force `_dematerialize_page` + `_materialize_page` (the
  tests/test_redesign.py:96-105 lazy-render pattern): second materialize makes ZERO
  render_with_edits calls and the displayed `layer.image` equals a fresh
  `document.render_with_edits` pixmap byte-for-byte.
- B2: `stage_edit` then `repaint_box` -> exactly one new render call; pixels match fresh render.
- B3: `rotate_page` (via `_run_structural` in a window, or document API + `reload`) -> miss.
- B4: undo_stack.undo() of a text edit -> miss, and the re-rendered page equals the pristine
  render (full-undo-pristine pattern of tests/test_editor.py:480-482).
- B5: cache length never exceeds capacity after scrolling a 30-page tempfile doc end to end;
  `set_document(other)` empties it.
- Full gate set green (test_redesign exercises the lazy band; test_pages exercises tab/structural
  interplay).

---

## M3: Batched reinsertion (one Shape per page bake)

`_insert_run` (document.py:2319) issues one `page.insert_text` per line, and per WORD for
justified lines (`_insert_paragraph`, document.py:2025-2036); each call internally builds a
Shape, calls `insert_font` (resource scan), and commits a content stream (~0.83 ms each; a
40-line paragraph is ~33 ms; probe: 5.8x saving when batched).

### Design
- In `_apply_page_edits` (document.py:1733) and its static twin `_apply_page_edits_for`
  (document.py:2448), create `shape = page.new_shape()` right after `apply_redactions`, thread
  it through the insertion phase (neighbors at 1791-1797, `_reinsert_edit` at 1801,
  NewBoxes at 1804-1811), and `shape.commit()` once at the end.
- `_insert_run` gains a `shape` parameter and calls `shape.insert_text(point, text, fontsize=,
  fontname=, color=, morph=)` instead of `page.insert_text`. The morph math (document.py:2345)
  carries over verbatim: probe confirmed Shape.insert_text accepts `morph`, so rotated runs ride
  the same batch.
- Font registration is UNCHANGED: `_register_resolved` keeps calling `page.insert_font` with the
  face-hash names (document.py:2384-2401, invariant: names derive from buffer hashes). Shape's
  internal `insert_font(fontname=name)` then resolves the already-registered resource.
- `_insert_paragraph` / `_insert_run` callers in `_reinsert_edit` (document.py:1882-2036) pass
  the shape down; rich single-line runs (`wrap_rich` segments) batch identically.
- One Shape per page per bake keeps the content-stream layering equivalent to today (all our
  insertions already land after redaction in apply order); a single commit emits one combined
  block instead of N appended blocks. Visual order among our own runs is preserved because
  Shape concatenates `text_cont` in call order.

### Undo / save / WYSIWYG story (M3)
Pure mechanics change inside the single shared pipeline (`_apply_page_edits` serves save_as,
render_with_edits, and the structural bake, document.py:1742-1745), so screen and file change
together by construction. No undo surface touched. The M2 pixmap cache keys are agnostic to this.

### Test plan (M3)
Extend `tests/test_perf_foundation.py`:
- C1: counting monkeypatch on `fitz.Page.insert_text`: a paragraph edit + a NewBox on
  paragraphs.pdf bakes with ZERO `page.insert_text` calls (everything routed through the Shape)
  and exactly one `Shape.commit` per edited page.
- C2: two-distinct-faces page (form_like.pdf has base-14 chrome + embedded Arial values): after
  save_as, `saved_basefonts` diff introduces no base-14 (`new_base14 == []` pattern,
  tests/test_editor.py:150-156) and both original face families survive.
- C3: justified paragraph: saved words' x-origins match `wrap_paragraph(...).lines[i]
  .word_origins` within 0.5 pt (the per-word draw contract, reflow.py:196-210).
- C4: rotated_doc.pdf edit keeps writing direction (word bbox taller than wide for the 90-degree
  page, mirroring tests/test_pages.py rotated coverage).
- Full gate set green, with test_font_fidelity + test_reflow + test_richtext as the explicit
  fidelity gates named by the audit.

---

## M4: Foundation seams + startup (page_view.py, main_window.py, font_engine.py)

Small refactors later workstreams build on. No behavior change; existing suites are the net.

### 4a. System font index off the first paint
`MainWindow` first construction pays ~150 ms of `_build_system_index` on the UI thread. Add to
FontEngine: class-level `threading.Lock` `_index_lock`; `_build_system_index` acquires it
(double-checked: re-test `cls._system_index` inside the lock). New classmethod
`FontEngine.prewarm_system_index()` that spawns a daemon `threading.Thread` running
`_build_system_index`. Call it at the top of `MainWindow.__init__` (main_window.py:963). Pure
fontTools + os (probe-verified, font_engine.py:709-780), no Qt objects cross threads. If the
user opens the family picker first, `available_families` blocks on the lock until the build
finishes (sub-200 ms worst case, current behavior anyway).

### 4b. Page-item factory hook (the overlay registration seam)
WS for annotations/forms/images need scene items per page without forking `_materialize_page`.
Add to PageView: `register_page_item_factory(factory: Callable[[_PageLayer, PageView],
list[QGraphicsItem]])` storing into `self._page_item_factories: list`. At the END of
`_materialize_page` (after page_view.py:1030, before the selection re-bind) call each factory,
add returned items to the scene, track them on a new `layer.extra_items: list`.
`_dematerialize_page` (page_view.py:1047) removes + clears `extra_items`. Document the z-slot
REGISTRY as a comment beside the existing constants (page_view.py:86) — Z_PREVIEW_TEXT already
owns 6, so reserved free slots are: `Z_TEXT_SELECT = 2` (ws2 text-select highlights),
`Z_ANNOT_HOTSPOT = 5` (ws3), `Z_IMAGE_HOTSPOT = 7` (ws4), `Z_FORM_HOTSPOT = 8` (ws5); 11-39 is
free between hotspots and selection. The constants themselves land with their workstreams; ws1
only writes the registry comment. All three hotspot slots sit below Z_HOTSPOT=10 so text
hotspots always win overlapping clicks. Default: zero factories, identical scene graph.
Also fix in passing (one-line, behavior-preserving-intent): the eviction guard at
page_view.py:959-962 claims to protect the selection's page but only checks `_editor_box`; add
the `self._selection` page check the comment promises, so future overlay items on a selected
page are not orphaned (the map flags this as an existing latent bug).

### 4c. Tool-mode dispatch tidy
Keep `current_mode()` returning the same strings ('select' | 'add_text' | 'text_edit',
page_view.py:1168) and all signals intact. Extract the mode-conditional branches of
`mousePressEvent` (page_view.py:1566) and key handling (page_view.py:2207) into per-mode
private methods (`_press_select`, `_press_add_text`, ...) dispatched from a small
`self._mode_handlers` dict. This is a pure code motion so WS2+ can add 'highlight', 'ink',
'shape', 'form' modes by registering a handler instead of editing a 150-line if-chain.
`enter_add_text_mode`/`exit_add_text_mode` (page_view.py:1267) unchanged.

### 4d. Menu-bar scaffolding (the ONE deliberate visible change in ws1)
Split `_build_menubar` (main_window.py:1358) into per-menu builders AND land the final 7-menu
skeleton in the same move: `self.menu_file/_edit/_view/_tools/_document/_window/_help`, built
exactly per the ws7 §M2 table (docs/acrobat_buildout/specs/ws7_navigation_chrome.md — that table
stays the registry of record; ws1 implements it). Today's Pages-menu actions move into the
Document menu; View gains the Page Navigation submenu homes; Tools holds Add Text (T). Every
reserved anchor from the table lands now as a real separator QAction stored in
`self.menu_anchors: dict[str, QAction]` (`file_output`, `edit_extra`, `view_panels`,
`tools_annotate`, `tools_objects`, `tools_forms`, `doc_transform`, `doc_decorate`, `doc_file`).
Dynamic content (Open Recent, Window doc list, cheatsheet, About) stays in ws7 M2. No QAction
handlers change; only menu placement does — this is the one exception to "zero visible change"
in this workstream, accepted so every later workstream inserts at anchors instead of
restructuring menus. Later workstreams extend exactly one builder via `menu.insertAction(anchor,
act)`, which kills most merge conflicts in main_window.py.

### Test plan (M4)
Extend `tests/test_perf_foundation.py`:
- D1: monkeypatch-count `_build_system_index` bodies: two concurrent threads -> exactly one
  build; `prewarm_system_index()` then `available_families()` returns the same list as a cold
  synchronous call.
- D2: register a factory returning one `QGraphicsRectItem`; open three_page.pdf; assert one item
  per materialized page (`layer.extra_items`), removed after `_dematerialize_page`, re-created
  on re-materialize; with no factories the scene item census matches pre-M4 (guard against
  accidental extra items).
- D3: menubar census: titles == [File, Edit, View, Tools, Document, Window, Help]; every
  pre-existing QAction is still reachable by walking the menubar with its original shortcut;
  `window.menu_anchors` contains all 9 anchor keys and each anchor's parent menu matches the
  ws7 table.
- D4: gesture regression: test_reflow's synthesized mouse press/dblclick flows (its own suite)
  plus a direct `current_mode()` transition check select -> add_text -> text_edit -> select.
- Full gate set green.

---

## CONFLICT RISK (files other workstreams will touch)
- `pdftexteditor/ui/page_view.py`: every UI workstream. Land M4b/M4c EARLY (they are what the
  others build on); annotations/forms must use the factory hook + mode dispatch, not new inline
  branches.
- `pdftexteditor/ui/main_window.py`: menus (M4d) and the command sink. Other streams insert
  actions at the `menu_anchors` separators; do not reorder existing actions or add new
  top-level menus (the ws7 table is the registry; extend it there if a slot is missing).
- `pdftexteditor/document.py`: `_apply_page_edits` is shared ground (M1/M3 here; annotations/
  watermark/forms bake there later). Keep the Shape threading parameter-based so a later
  "annotation layer" pass can append to the same shape or add its own after commit.
- `pdftexteditor/font_engine.py`: M1c/M4a; the forms workstream will add an AcroForm DR-font
  entry point; coordinate on the class-level cache + new lock.
- `tests/test_perf_foundation.py`: new, ws1-owned; other streams add their own suites.

## DE-SCOPE (deliberate exclusions)
- TextWriter migration: faster still (probe 4.1 ms vs 9.6 ms) but converts base-14 output to
  embedded Type0/CID fonts, changing saved-file font semantics; revisit only if Shape batching
  misses the commit-latency target.
- Threaded/async page rendering and thumbnail idle queues: sync + cache hits the targets for
  one-user document sizes; Qt font APIs are GUI-thread-bound and not worth the hazard.
- `working.tobytes()` per frame (3.5 ms at 60 pages), spans() extraction, wrap engine, save_as
  speed, structural-undo RAM (30 snapshots), eager thumbnails: all measured healthy by the
  audit; explicitly do not "fix".
- Smooth zoom UI (Ctrl+wheel/pinch/slider): a navigation-workstream feature; ws1 only makes
  `set_zoom` cheap enough for it.
- Model `_undo`/`_redo` stale-after-structural trap (document.py pain point): real, but it is
  an API-correctness fix with undo-semantics implications, not perf; flag to the workstream that
  owns headless model APIs.

## Milestone summary (each lands with the full gate set green + the new suite)
1. M1 model hot-path caching: persistent FontEngine in render/save pipeline, neighbors memo,
   face_bytes memo, `render_signature` API.
2. M2 baked-pixmap LRU cache in PageView keyed on `render_signature` (depends on M1).
3. M3 one-Shape-per-page batched reinsertion in `_apply_page_edits` (independent of M2).
4. M4 seams: background font index, page-item factory + z-slot registry, tool-mode dispatch,
   7-menu skeleton with `menu_anchors` (per the ws7 table), eviction-guard selection fix.
