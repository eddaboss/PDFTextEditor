# WS3: Annotations & Markup

Workstream: ws3_annotations_markup. Full Acrobat-style markup on the PySide6 + PyMuPDF editor at
/Users/edward/Documents/GitHub/PDFTextEditor. Stack locked: PySide6 + PyMuPDF (fitz) + stdlib, no
new pip installs. The user is Edward, an HR manager marking up certificates, agendas, and forms.
The soul of the app is invisible in-place text editing; nothing here may degrade the
redact-and-reinsert pipeline or its WYSIWYG contract.

## 1. Feasibility: PyMuPDF probe results (`.venv/bin/python`, fitz 1.27.2.3, run for this spec)

- `fitz.Page` has ALL of: `add_{highlight,underline,strikeout,squiggly,text,ink,rect,circle,
  line}_annot`, `annots`, `delete_annot`, `load_annot`. `fitz.Annot` has ALL of:
  `set_{colors,border,opacity,rect,info,line_ends,open,flags}`, `update`, `info`, `rect`, `type`,
  `vertices`, `colors`, `border`, `opacity`, `flags`, `xref`. Constants
  `PDF_ANNOT_LE_{NONE,OPEN_ARROW,CLOSED_ARROW}` and `PDF_ANNOT_{HIGHLIGHT,UNDERLINE,STRIKE_OUT,
  SQUIGGLY,TEXT,INK,SQUARE,CIRCLE,LINE}` all exist.
- Roundtrip: highlight (quads), text note (contents), ink (2.5pt), square (stroke+fill, opacity
  0.4), line with `set_line_ends(LE_NONE, LE_OPEN_ARROW)`; `doc.tobytes()` then `fitz.open("pdf",
  bytes)`: all 5 survive with correct type/colors/opacity/border/vertices/contents, also through
  a second tobytes/reopen cycle (the save_as path shape). `insert_pdf` carries annots (2/2), so
  the constructor deep copy is safe.
- `page.get_pixmap()` default includes annots (ink identical to `annots=True`, larger than
  `annots=False`): render_with_edits and the saved file show annots for free.
- Rotated pages: with `set_rotation(90)`, `get_text("words")` rects come back in UNROTATED text
  space (same as `search_for`/rawdict), and feeding them to `add_highlight_annot` renders
  correctly in the rotated display (pixel-diff bbox matched the rotation-matrix-mapped word
  rect). So all annot geometry here lives in the text space the codebase already uses
  (`rotation_matrix` document.py:577, `_pdf_point` page_view.py:2362).
- Mutating reopened annots: `set_rect` moves persist, `xref` stable, `delete_annot` works. Flags:
  created highlight 4 (PRINT), text note 28 (PRINT|NoZoom|NoRotate): saved annots are visible AND
  printable in Preview/Acrobat by default.

## 2. Architecture: a staged annotation layer mirroring the text-edit staging

Annotations are STAGED in memory on PDFDocument, exactly like text edits, and applied to the
fresh copy inside the one shared pipeline. `self.working` is never mutated by annotation ops
(invariant at document.py:478-489); annots become real PDF objects in (a) every
`render_with_edits` frame, (b) `save_as` output, (c) the structural bake. Screen == file by
construction. Pre-existing annots already live in `working` (`insert_pdf` carries them,
probe-verified) and render automatically; the model manages them through a small override map
(delete / edit contents / move notes) applied to the fresh copy by xref. Why staged: direct
mutation of working would need byte-snapshot undo per stroke (30-cap structural stack, wipes
fine-grained history) and break the "only structural ops mutate working" invariant; staged specs
reuse the cheap per-object command undo.

## 3. Model changes (pdftexteditor/document.py)

### 3.1 Dataclasses (place near NewBox, document.py:352)

```python
@dataclass(frozen=True)
class AnnotSpec:
    page_index: int
    annot_id: int                   # itertools.count on the document, like NewBox
    kind: str  # highlight|underline|strikeout|squiggly|note|ink|rect|ellipse|line|arrow
    quads: tuple | None = None; rect: tuple | None = None  # markup quad rects / note-shape rect
    points: tuple | None = None; endpoints: tuple | None = None  # ink strokes / line (p1,p2)
    stroke: tuple = (0.85, 0.1, 0.1); fill: tuple | None = None   # fill: rect/ellipse only
    width: float = 2.0; opacity: float = 1.0; contents: str = ""  # ALL geometry in text space
    @property
    def identity(self): return (self.page_index, "annot", self.annot_id)

@dataclass(frozen=True)
class AnnotOverride:                # for annots already in the FILE, keyed by xref
    deleted: bool = False; contents: str | None = None
    dx: float = 0.0; dy: float = 0.0   # notes only
```

Per-kind defaults (window holds session defaults, model takes explicit values): highlight stroke
(1, .85, 0); underline (.13, .55, .13); strikeout/squiggly (.8, 0, 0); note (1, .8, 0);
ink/shapes stroke (.85, .1, .1), width 2.0, fill None, opacity 1.0.

### 3.2 State, identity, enumeration

- `self._annots: dict[identity, AnnotSpec]`; `self._annot_overrides: dict[(page, xref), AnnotOverride]`; `self._annot_id_counter = itertools.count(1)`.
- `annotations(page_index) -> list[AnnotRecord]`: unified read API for hotspots + comments panel.
  AnnotRecord is a tiny view object: `identity`, `kind`, `display_rect` (text space), `contents`,
  `is_existing: bool`, `xref|None`, `spec|None`. Existing annots enumerate
  `self.working[page].annots()` (skip `PDF_ANNOT_POPUP` and links), apply overrides (skip
  deleted, shift note rects, swap contents). Memoized per page in `_annot_records_cache`;
  invalidated by every annot mutator and by `_invalidate_caches` (document.py:2521).
- Markup quad computation REUSES `PDFDocument.page_words(page_index)` from ws2 M4 (already
  landed: cached, text space, and bake-aware — it reflects staged text edits because it runs the
  render_with_edits pipeline). Do NOT add a second words API. Since annots are applied to the
  same baked copy, quads computed from these words land on the text as displayed, including
  pages with staged edits.

### 3.3 Mutators (each: one undo command, clear redo, dirty; pattern of document.py:984)

- `add_annot(page_index, **spec_fields) -> AnnotSpec`; `modify_annot(page_index, ref, **fields)`
  (stroke/fill/width/opacity/contents; staged only; existing annots allow `contents` only,
  stored as override)
- `move_annot(page_index, ref, dx, dy)`: staged: rebuild spec with translated geometry (pure
  Python). Existing: only kind Text (notes), folds dx/dy into the override (cumulative, like
  move_box document.py:1253); other existing kinds raise ValueError, the view never offers it.
- `delete_annot_box(page_index, ref)`: staged: remove from map; existing: override deleted=True.
  `set_annot_contents(page, ref, text)` = modify_annot sugar (note popup + comments panel).

Undo: introduce `_AnnotCommand(key, before, after)` (before/after: AnnotSpec|AnnotOverride|None)
pushed onto the SAME `self._undo` list as `_Command` (document.py:448); `undo()/redo()`
(document.py:1373/1386) gain an isinstance dispatch and return `(page_index, identity)` for annot
entries, keeping the Qt-stack/model-stack lockstep (main_window.py:381-397) intact with mixed
text+annot histories. `_bake_pending_edits` (document.py:2498) additionally filters annot entries
out of `_undo`/`_redo` when clearing the staged maps (the Qt stack is wiped right after by
`_sync_structural_undo_stack` main_window.py:2519; the filter keeps the model mirror sane).

### 3.4 Pipeline hook: `_apply_page_annots(out_page, page_index)`

Called at the END of `_apply_page_edits` (document.py:1733) and `_apply_page_edits_for`
(document.py:2448), after redaction + reinsertion, so it runs identically for render_with_edits,
save_as, and the structural bake (single-pipeline invariant, document.py:1733-1758).

1. Overrides first: iterate `out_page.annots()`, match `annot.xref` against `_annot_overrides`
   (xrefs are byte-identical in the fresh copy, probe-verified). deleted:
   `out_page.delete_annot(annot)`; dx/dy: `annot.set_rect(annot.rect + (dx,dy,dx,dy))`;
   contents: `annot.set_info(content=...)`; then `annot.update()` (never after delete).
2. Staged specs for the page, in annot_id order:
   - markup kinds: `out_page.add_<kind>_annot(quads=[fitz.Rect(q).quad for q in spec.quads])`;
     `set_colors(stroke=spec.stroke)`; `set_opacity` if < 1; `update()`.
   - note: `add_text_annot(fitz.Point(rect[:2]), spec.contents, icon="Comment")`;
     `set_info(content=spec.contents, title="")` (Title stays empty: no identity management);
     `set_colors(stroke=...)`; `set_open(False)`; `update()`.
   - ink: `add_ink_annot([list(s) for s in spec.points])`; then stroke color, border width,
     opacity, `update()`. rect/ellipse: `add_rect_annot(fitz.Rect(spec.rect))` /
     `add_circle_annot(...)`; `set_colors(stroke=..., fill=spec.fill)` (omit fill kw when None);
     border width, opacity, `update()`.
   - line/arrow: `add_line_annot(fitz.Point(p1), fitz.Point(p2))`; arrow adds
     `set_line_ends(fitz.PDF_ANNOT_LE_NONE, fitz.PDF_ANNOT_LE_OPEN_ARROW)`; then stroke color,
     border width, opacity, `update()`.

Fast-path fix: `render_with_edits` (document.py:2202) falls back to a plain render when a page
has zero edits; the condition must become "no edits AND no staged annots AND no overrides for
this page", else new annots never appear. Same guard in `save_as`'s per-page loop and
`_bake_pending_edits`'s page list. `has_edits` (:1631) and `edit_count` (:1639) must include
annot staging counts so structural ops bake them and the status bar counts them; `dirty` (:1652)
ORs `bool(_annots) or bool(_annot_overrides)`.

### 3.5 Save / bake / WYSIWYG story

- `save_as` (document.py:1670): staged annots become real annot objects in the output via the
  pipeline above; pre-existing annots ride along in the working bytes; overrides applied on the
  copy. Output opens in Preview/Acrobat with editable, printable annots (flags probe, section 1).
- Structural ops: `_bake_pending_edits` writes staged annots + overrides INTO `working` (same
  `_apply_page_annots`, target = working page), clears both maps and the annot command entries;
  baked annots become "existing" with fresh xrefs, so page delete/reorder carries them naturally
  and structural undo restores pre-op bytes including annots. `merge`/`extract_pages`/`split`
  route through `_bake_doc` (document.py:2422) -> `_apply_page_edits_for`, so exported/merged
  files include staged annots automatically.
- WYSIWYG: render_with_edits and save_as share `_apply_page_annots`, and `get_pixmap` default
  renders annots (probe), so the harness metric (render_with_edits ink vs saved-file ink, rel
  diff < 0.02, tests/test_editor.py:208) keeps passing: both sides contain identical annots.
  Existing pain point unchanged: a pre-existing annot overlapping an edited box keeps its stale
  appearance over new text; staged annots are applied after redaction so they are unaffected.

## 4. Canvas changes (pdftexteditor/ui/page_view.py)

### 4.1 New z-slot and hotspot

`Z_ANNOT_HOTSPOT = 5` in the z-constant block (page_view.py:86) — the slot assigned by the ws1
M4b registry (6 is already taken by Z_PREVIEW_TEXT; images=7, forms=8). Below Z_HOTSPOT(10):
text hotspots ALWAYS win overlapping clicks, so click-to-edit text is untouched.
New `AnnotHotspot(QGraphicsRectItem)` mirroring SpanHotspot (page_view.py:126): transparent,
stores `record.identity` + page_index, scene rect = `record.display_rect` through the existing
`_scene_point` math (page_view.py:2352; annot rects are text-space, probe section 1). Built via
ws1 M4b's `register_page_item_factory` (items from `document.annotations(i)`, tracked on
`layer.extra_items`), NOT by forking `_materialize_page`; freed automatically by
`_dematerialize_page`.

### 4.2 Tool modes

`_mode` gains: `markup_highlight`, `markup_underline`, `markup_strikeout`, `markup_squiggly`,
`note`, `ink`, `shape_rect`, `shape_ellipse`, `shape_line`, `shape_arrow`. Press/move/release
and key handling register as handlers in ws1 M4c's `_mode_handlers` dispatch — never new inline
branches in `mousePressEvent`/`keyPressEvent`. API:
`enter_annot_mode(kind)` / `exit_annot_mode()`; emits existing `modeChanged` (page_view.py:530).
Entering any annot mode flushes the editor (`_flush_editor` :2181), clears box selection, sets a
crosshair cursor; Esc exits to select (keyPressEvent :2207). Tools stay armed after each commit
(Acrobat behavior) until Esc/tool switch.

### 4.3 Gestures (all in mousePress/Move/Release, alongside existing drag state 1616-1718)

- Markup: press-drag draws a translucent accent band (QGraphicsRectItem, z=45). Release: map band
  corners with `_pdf_point` to text space; intersect with `document.page_words(page)`; group hit
  words by (block_no, line_no) (words fields 5,6), union per group into one quad rect; emit
  `annotCommandRequested('add', None, {spec fields})`. Empty hit: "No text under selection" toast
  via a new `statusMessage(str)` signal, no command.
- Note: single click places an 18x18pt anchor rect, emits 'add' with empty contents, then the
  window opens the note popup editor on the new record.
- Ink: press starts a stroke; moves append `_pdf_point` points (decimate: skip < 0.7pt apart);
  live QGraphicsPathItem preview (z=45). Release with >= 2 points commits ONE annot = one stroke
  = one undo step; fewer: discard. Shapes: press-drag live preview (rect/ellipse items;
  line/arrow QGraphicsLineItem + polygon arrowhead); release emits 'add'; < 3pt diagonal: discard.
- Select mode: clicking an AnnotHotspot (when no SpanHotspot is on top) selects the annot: new
  `_annot_selection` state + dashed accent outline (lightweight `AnnotSelectionOverlay`, z=40, no
  font-scaling handles). Alt+click forces annot-first hit-testing under text. Body drag with 3px
  slop = move (staged annots and existing notes only); release emits 'move' {dx, dy} in PDF
  points. Del/Backspace emits 'delete'. Esc clears. Annot selection and box selection are
  mutually exclusive (extend `_on_box_press` page_view.py:1514, `mousePressEvent` :1566).
- New signals: `annotCommandRequested(str, object, dict)` (mirror of boxCommandRequested
  page_view.py:546), `annotSelectionChanged(object)`, `statusMessage(str)`. New APIs:
  `select_annot_by_identity(identity)`; `repaint_page(page_index)` = dematerialize +
  rematerialize one page (the repaint_box cycle, page_view.py:1462, without box rebind), rebuild
  annot hotspots, re-bind annot selection by identity. AnnotCommand calls it.

## 5. Window wiring (pdftexteditor/ui/main_window.py)

### 5.1 AnnotCommand on the shared QUndoStack

New `AnnotCommand(document, view, page_index, ref, kind, params)` next to BoxCommand
(main_window.py:501) with kinds `add|move|delete|contents|style`; do NOT extend BoxCommand's
closed kind set (window map invariant). Contract identical: first redo runs the model mutator,
undo calls `document.undo()`, later redo `document.redo()`, then `view.repaint_page(page)` +
comments-panel refresh. Wire `annotCommandRequested` in `_wire_view` (main_window.py:1800) via
`_connect_signal`. Commands tolerate the structural stack wipe (staged annots were baked).

### 5.2 Toolbar + menu + shortcuts

- Toolbar (`_build_toolbar` main_window.py:1124): new "Markup" group after the Find button:
  Highlight, Underline, Strikethrough, Note, Ink toggles + a Shapes QToolButton (MenuButtonPopup;
  menu Rectangle/Ellipse/Line/Arrow, last-used becomes the button default). Checkable, mutually
  exclusive with Select/Add Text (window listens to `modeChanged` and unchecks, like the Add Text
  sync); hidden with no document (`_sync_actions` main_window.py:2840). New icons in icons._ICONS
  (icons.py:23): highlight, underline, strikethrough, squiggly, note, ink, shape.
- NO new top-level menu (ws1 M4d skeleton + ws7 table own menu organization). Markup actions
  insert at `self.menu_anchors["tools_annotate"]` in the Tools menu: Highlight (H), Underline
  (U), Strikethrough (K), Squiggly, Sticky Note (N), Draw Ink (D), Shapes submenu, Delete
  Annotation (enabled on annot selection). Show Comments (Cmd+Shift+C, checkable) inserts at
  `menu_anchors["view_panels"]` in the View menu. Bare-key tool shortcuts follow the Add Text 'T' precedent (main_window.py:1064) and
  never fire while the inline editor has focus. Squiggly is menu-only (toolbar space). Annot
  toasts route through `_toast` (main_window.py:2934); the view's `statusMessage` connects to it.

### 5.3 Note popup editor (NO modal dialogs: offscreen-test trap, tests map)

Frameless non-modal QWidget (QPlainTextEdit + Done button), child of the view viewport, anchored
next to the note's scene rect, objectName `NotePopup`. Opens on note creation, double-click of a
note hotspot, or Edit in the comments panel. Commit on Done / focus-out / Cmd+Return; emits one
`annotCommandRequested('contents', ref, {'text': ...})` only if changed; Esc cancels. Test seam:
`MainWindow.open_note_editor(ref)` + widget reachable as `window._note_popup` (no QDialog.exec).

### 5.4 Comments panel (left dock, third stacked page)

LeftPanel (left_panel.py:43) gains a "Comments" tool-strip button + stacked page hosting a new
pure-chrome `CommentsPanel` (new file pdftexteditor/ui/comments_panel.py), built like
FindReplacePanel (find_panel.py:42): injected callables `list_annots()`, `jump(ref)`, `edit(ref)`,
`delete(ref)`; zero model imports. QListWidget objectName `CommentsList`; rows sorted by (page,
y): kind glyph, "p.N", contents snippet or kind label, "(file)" suffix for pre-existing annots.
Click = jump (scroll_to_page + select_annot_by_identity); Edit Note / Delete buttons act on the
selected row. MainWindow calls `refresh()` after every AnnotCommand redo/undo,
`_after_structural_op`, and tab switch; Show Comments toggles `set_active_tool('comments')`.

### 5.5 Style controls (M3)

Inspector (inspector.py:91) gains an ANNOTATION section shown via new `set_annot_target(record)`
(text sections hidden): stroke swatch, width spin 0.5-12, fill swatch + "No fill" check (shapes
only), opacity spin 10-100%. Emits new `annotStyleEdited(dict)` (one field per emission,
mirroring the styleEdited invariant inspector.py:96); the window turns each into
`AnnotCommand('style', ref, {field: value})`. Pre-existing file annots: section grayed ("Saved
annotation" hint). With a markup/shape TOOL armed and nothing selected, the same controls edit
the window's per-tool session defaults (no undo entries).

## 6. Undo/redo summary

| Action | Command | Undo result |
|---|---|---|
| Add markup/note/ink/shape | AnnotCommand('add') -> document.add_annot | spec removed, page repaints |
| Move staged annot / existing note | AnnotCommand('move') | geometry/override delta reverted |
| Edit note text / style change (staged) | AnnotCommand('contents'/'style') | previous spec or override restored |
| Delete staged / delete existing | AnnotCommand('delete') | spec restored / override removed |
| Structural op | StructuralCommand (existing) | byte snapshot restores baked annots wholesale |

Annot and text commands interleave on the one QUndoStack, 1:1 lockstep with the model stacks
(main_window.py:381-397); structural ops wipe fine-grained history exactly as today.

## 7. Milestones (each lands alone, all suites green: the 7 named + test_redesign + test_text_tools)

### M1: Annotation model core + text markup tools
Model: AnnotSpec, `_annots`, add/delete mutators, `_AnnotCommand` undo dispatch,
`_apply_page_annots` (markup kinds), fast-path + dirty/edit_count/has_edits updates,
`annotations()` (staged only); quads via ws2's `page_words()`. Canvas: Z_ANNOT_HOTSPOT=5,
markup modes + band gesture (via ws1 `_mode_handlers`), `annotCommandRequested`, `repaint_page`.
Window: AnnotCommand, toolbar Markup group, Tools-menu markup entries at the `tools_annotate`
anchor, empty-state hiding.
Test: NEW suite `tests/test_annotations.py` (harness pattern of tests/test_app.py:35: offscreen,
module QApplication, check() accumulator, absolute paths, `_suppress_close_guard`). Fixture: NEW
`tests/fixtures/build_annot_fixture.py` -> `annot_target.pdf`: 2 pages, embedded-font paragraphs,
fake names only ("Jordan Carter", "Acme Corp", "Riley Morgan"); page 2 ships pre-existing annots
(one highlight + one note, content "Reviewed by Riley Morgan") for M2. Asserts: add highlight
over quads from page_words raises render_with_edits ink in the region; save_as -> fitz reopen
shows annot type/stroke color/quad count for all four markup kinds; document.undo removes it from
render AND save; UI path: arm highlight tool, synthesize press/drag/release over known text
(precedent tests/test_reflow.py:552), exactly one command on window.undo_stack, window undo
clears it; rotate_page bakes the staged highlight into working (`working[i].annots()` non-empty,
staged map empty); WYSIWYG rel-diff < 0.02 with one text edit + one highlight on the same page;
on rotated_doc (build_pages_fixtures) the highlight lands on the word (region ink check).

### M2: Sticky notes, selection/move/delete, existing-annot management
Model: note kind, AnnotOverride + `_annot_overrides`, move_annot/modify_annot/set_annot_contents,
existing-annot enumeration in `annotations()`, override application + bake. Canvas: note mode,
AnnotHotspot + selection overlay, move drag, Del, Alt+click. Window: note popup (5.3), Delete
Annotation action.
Test (extend test_annotations.py): note add via click -> popup setPlainText + commit -> one 'add'
+ one 'contents' command; saved file shows the Text annot with content, popup closed; move staged
note (undo restores rect); delete existing page-2 highlight -> saved file lacks it, undo restores
it; edit existing note contents -> saved info changes; in-place-edit guard: stage a text edit by
the existing highlight, save: text changed AND annot intact AND no residue (region_ink pattern
tests/test_font_fidelity.py:204).

### M3: Ink + shapes + style controls
Model: ink/rect/ellipse/line/arrow kinds in `_apply_page_annots`, modify_annot style fields.
Canvas: ink and shape modes with live previews, point decimation. Window: Shapes toolbar button +
submenu, Inspector ANNOTATION section + annotStyleEdited wiring, per-tool defaults.
Test (extend): each kind model-added then saved: reopened annot type, stroke color, border width,
fill (Square), opacity, arrow `line_ends[1] == PDF_ANNOT_LE_OPEN_ARROW` (all probe-verified);
UI ink drag -> exactly one command; inspector style emission -> one undo step, saved color
matches; undo chain returns the page render to its pristine ink count.

### M4: Comments panel
New comments_panel.py + LeftPanel third page + Show Comments toggle + refresh wiring.
Test (extend): open annot_target.pdf, add 2 staged annots on page 1: panel rows == staged +
existing, sorted; click a page-2 row: `window.view_page_index() == 1` and annot selected; panel
Delete produces an undoable command and refreshes; undo restores the row count.

## 8. DE-SCOPE (deliberate)

No annotation replies/threads or review statuses; no FreeText/typewriter annot (Add Text covers
it); no polygon/polyline/cloud shapes; no stamps or signatures (objects WS); no annot copy/paste;
no flatten-annotations command; no author identity (Title left empty: no real names anywhere); no
style editing or arbitrary moving of PRE-EXISTING file annots (delete + note-move + contents
only: `set_rect` on existing Ink/markup does not relocate vertices reliably); no fix for the
pre-existing stale-appearance pain point (file annot over an edited box, orthogonal to this WS).
(Markup over staged text edits follows the EDITED geometry because ws2's `page_words` is
bake-aware — the former original-geometry limitation is gone.)

## 9. CONFLICT RISK (files other workstreams will also touch)

- `pdftexteditor/ui/main_window.py`: toolbar groups, menubar, `_sync_actions`, `_wire_view`, new
  command classes. Highest collision surface (print/zoom/forms/document WS all land here). Keep
  additions in self-contained `_build_markup_toolbar_group()` / `_build_annotate_menu()` helpers.
- `pdftexteditor/ui/page_view.py`: z-constants, mode and press routing. Text-selection and
  images/forms WS add their own modes/hotspots: this WS claims z=5 for annot hotspots and the
  `markup_*|note|ink|shape_*` mode names; images own 7, forms own 8 (ws1 M4b registry).
- `pdftexteditor/document.py`: `_apply_page_edits` tail hook, dirty/edit_count/has_edits,
  `_invalidate_caches`, `_bake_pending_edits`. Forms and document-tools (watermark) WS touch the
  same pipeline; the `_apply_page_annots` call must stay LAST per page (ws4 image insertion goes
  before it). Cache keying: fold this page's sorted staged-annot specs + override items into
  `PDFDocument.render_signature` (the ws1 M1d registry) in the same change that adds the state —
  no separate annot_state_sig API; assert a cache miss after add_annot in test_annotations.
- `pdftexteditor/ui/left_panel.py` / `inspector.py`: bookmarks/outline WS adds another panel
  page; coordinate the tool-strip id namespace ('comments' claimed here). `tests/`: new suite +
  fixture builder are additive; suites list grows to 10.

## 10. Implementation notes / gotchas

- Call `set_colors`/`set_border`/`set_opacity` BEFORE `update()` (the probe's proven order),
  never `update()` after `delete_annot`; `set_opacity` only when < 1.0 (unset reads -1).
- One quad per (block, line) word group keeps Preview's markup selection rendering clean.
- The view never mutates the model (page_view.py:44-48): every gesture exits through
  `annotCommandRequested`. Packaging: all new code lives under `pdftexteditor/` so
  collect_submodules auto-bundles it (PDFTextEditor.spec:18); no data files, no new deps. Suite
  run: `cd <repo> && QT_QPA_PLATFORM=offscreen .venv/bin/python tests/test_annotations.py`;
  fixture gen is idempotent, writes only inside tests/fixtures/, fake names only.
