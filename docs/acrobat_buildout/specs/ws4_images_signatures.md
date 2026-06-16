# ws4_images_signatures — Images, stamps & signatures

Target user flow: Edward reuses PDF templates (certificates, offer letters, agendas). He needs to
drop a logo onto a certificate, place his signature on an agreement, and stamp APPROVED on a form —
without disturbing the invisible in-place text-edit pipeline that is the soul of the app.

Scope: insert image from file (PNG/JPEG), place by click or drag-rect, move/resize (Shift = keep
aspect)/delete, undoable, baked via `page.insert_image`. Signature: draw-with-mouse dialog
(smoothed strokes -> transparent PNG) and/or load from file; persistent library in
`~/.pdftexteditor/signatures/`; one-click placement. Stamps: procedurally generated APPROVED /
DRAFT / CONFIDENTIAL / COMPLETED / VOID PNGs placed the same way. Existing-image handling: detect
page images, select + delete (scoped redaction), move (extract + delete + reinsert).

## 0. Feasibility — PyMuPDF 1.27.2.3 probes (run in this venv, all verified 2026-06-09)

- `Page.insert_image(rect, *, alpha=-1, filename, keep_proportion=True, mask, oc, overlay=True,
  pixmap, rotate=0, stream, width, height, xref=0)` exists; returns the image `xref` (int).
- Inserting an RGBA PNG via `stream=` auto-creates an SMask (`extract_image(...)["smask"] != 0`):
  transparent signatures work with no extra steps.
- `Page.get_image_info(xrefs=True)` and `Page.get_image_rects(xref, transform=True)` exist and
  return correct display rects for placed images.
- `Page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_REMOVE, graphics=fitz.PDF_REDACT_LINE_ART_NONE,
  text=fitz.PDF_REDACT_TEXT_NONE)`: removed the image occurrence on THAT page only (pixel-verified,
  red-ink count 25600 -> 0), kept page text ("Jordan Carter" survived), and left the SAME xref's
  occurrence on another page untouched (6400 red px intact). Constants: `PDF_REDACT_IMAGE_REMOVE=1`,
  `PDF_REDACT_TEXT_NONE=1`, `PDF_REDACT_LINE_ART_NONE=0`.
  CAVEAT (verified): a redact rect covering only HALF the image still removes the WHOLE occurrence,
  and `get_image_rects` on the live Page object is stale until reopen — irrelevant to us because the
  bake always works on fresh copies, but never assert on the mutated live page in tests.
- Move-existing recipe verified: `doc.extract_image(xref)` -> base bytes + `smask` xref;
  `fitz.Pixmap(doc, xref)` (alpha=0) + `fitz.Pixmap(base_pix, fitz.Pixmap(doc, smask))` -> alpha=1
  pixmap -> `tobytes("png")` re-inserts with transparency intact.
- `rotate=` accepted on a `/Rotate` page. The exact value that renders upright on screen is pinned
  by test (M1.7), spec default `rotate = page.rotation`.

## 1. Architecture in one paragraph

Images are a third staged-box kind beside `Span`/`NewBox`: an `ImageBox` map on `PDFDocument`,
mutated only through the existing snapshot->install->push `_Command` discipline
(document.py:984-1058), drawn by the single shared bake pipeline `_apply_page_edits`
(document.py:1733) so screen == save == structural-bake with zero new render paths. The canvas adds
one placement mode and one image hotspot/overlay pair that emit `boxCommandRequested` with new
`img_*` kinds; the window maps those kinds onto `BoxCommand` (main_window.py:501) so everything
shares the ONE QUndoStack in lockstep. `self.working` is never mutated; nothing here touches the
text redaction/reinsert path except two clearly-bounded insertion points.

## 2. Model changes — pdftexteditor/document.py

### 2.1 New dataclasses (place after NewBox, document.py:352-401)

```python
@dataclass
class ImageBox:                      # a staged, not-yet-baked placed image
    page_index: int
    rect: tuple                      # (x0,y0,x1,y1) PDF points, display-space like Span bboxes
    image: bytes                     # original PNG/JPEG file bytes (never recoded for placement)
    kind: str = "file"               # "file" | "signature" | "stamp" | "moved"
    natural_px: tuple = (0, 0)       # source pixel size, for aspect + default sizing
    box_id: int = 0                  # per-document counter, like NewBox
    deleted: bool = False
    # Box-protocol duck typing (document.py:239; hotspots read these):
    bbox -> rect; origin -> (x0, y1); size -> rect height; color -> (0,0,0)
    is_horizontal=True; rotation_degrees=0; redact_rects=()
    identity / edit_key -> (page_index, "img", box_id)

@dataclass(frozen=True)
class ExistingImage:                 # an image already in the file (M3)
    page_index: int; xref: int; rect: tuple; occ: int   # occurrence index on page
    identity -> (page_index, "xim", xref, occ); bbox -> rect  # plus same duck-typed fields
```

### 2.2 State + history integration

- `__init__` (near :516): `self._images: dict[tuple, ImageBox] = {}`,
  `self._xim_deletes: dict[tuple, ExistingImage] = {}`, `self._image_id_counter`.
- `_BoxState` (:425) gains `imagebox: "ImageBox | None" = None` and `xim: "ExistingImage | None" = None`,
  reusing the existing `exists: bool` exactly like the `newbox` pattern.
- `_install_state` (:1044): route `key[1] == "img"` -> `_install_image_state` (pop when
  `not exists`, else `replace(state.imagebox, deleted=False)`); `key[1] == "xim"` -> install/remove
  the `_xim_deletes` entry (exists=False means "deletion staged"). Add `_image_state(key)` /
  `_xim_state(key)` snapshot helpers mirroring `_newbox_state` (:1016).
- `_command_box_ref` (:1398): handle `"img"`/`"xim"` keys so undo/redo return the right box for
  `repaint_box`.

### 2.3 Mutators (all via `_push_command`, one command each, same rule as :984)

- `add_image(page_index, rect, image_bytes, kind="file", natural_px=(0,0)) -> ImageBox`
  (kind="img_add"; before = not-exists, after = exists). Validate bytes are PNG/JPEG by magic
  number; clamp rect to page rect.
- `move_image(page_index, box, dx, dy)` — cumulative translate, stored as a new absolute rect
  (kind="img_move").
- `resize_image(page_index, box, rect)` — ABSOLUTE new rect (not a scale; images have no baseline
  fold-in like resize_box :1279) (kind="img_resize").
- `delete_image(page_index, box)` — exists=False (kind="img_delete").
- M3: `existing_images(page_index) -> list[ExistingImage]` — memoized per page like `spans()`
  (:589), built from `self.working[i].get_image_info(xrefs=True)` filtering xref>0; cleared in
  `_invalidate_caches` (:2521). `delete_existing_image(page_index, xim)` stages a `_xim_deletes`
  entry (kind="xim_delete"). `extract_image_bytes(xref) -> (bytes, (w,h))` — `extract_image` +
  SMask recombine per §0 probe; recode to PNG via `Pixmap.tobytes("png")` whenever
  `ext not in ("png","jpeg","jpg")` or an smask exists.
- Accessors: `image_boxes(page_index)`, `image_boxes_all()`, `xim_deletes(page_index)`.

### 2.4 Bake pipeline — the ONLY two insertion points

In `_apply_page_edits` (:1733) and its static twin `_apply_page_edits_for` (:2448) — keep both in
exact lockstep, this is the screen==save invariant:

1. BEFORE the existing text redaction block (:1781): if the page has staged `_xim_deletes`, run a
   separate first redaction pass — `page.add_redact_annot(fitz.Rect(x.rect))` for each, then
   `page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_REMOVE,
   graphics=fitz.PDF_REDACT_LINE_ART_NONE, text=fitz.PDF_REDACT_TEXT_NONE)`. Two passes are
   required because flag sets differ (text pass keeps images, :1784). Probe §0 proves text and
   other pages survive. Runs before `_overlapping_neighbors` capture so the rawdict walk is
   unaffected.
2. AFTER the NewBox draw loop (:1804-1811) and BEFORE the `_apply_page_annots(...)` tail call
   that ws3 added (annots must stay LAST per page — ws3 contract): for each non-deleted
   `ImageBox` on the page (excluding none — images have no live-editor exclusion), call
   `page.insert_image(fitz.Rect(box.rect), stream=box.image, keep_proportion=False,
   rotate=page.rotation)`. Drawn after text so a placed image sits above reinserted text
   (Acrobat behavior); `overlay=True` is the default.

`_apply_page_edits_for` gets two extra params (`images_map`, `xim_map`); `_bake_doc` (:2422)
signature grows accordingly and seeds `by_page` from both maps; `merge` (:2668) passes the OTHER
doc's maps; `_bake_pending_edits` (:2498) extends its empty-guard and clears `_images` +
`_xim_deletes` after baking. `undo_structural`/`redo_structural` (:2797/:2816) clear both new maps
alongside `_edits`/`_new_boxes`.

### 2.5 Page-filter checklist (miss one and screen/save drift — enumerate in PR)

`has_edits` (:1632), `edit_count` (:1640, count non-deleted ImageBoxes + xim deletes),
`render_with_edits` early-out (:2216-2238: add `or image_boxes(page) or xim_deletes(page)`),
`save_as` by_page seeding (:1703-1712), `_bake_doc` guard (:2433), `_bake_pending_edits` guard
(:2505), AND `render_signature(page)` (ws1 M1d registry: fold the page's sorted ImageBox
signatures + xim-delete keys into the tuple so the ws1 pixmap cache misses on image mutations;
assert the miss in test_images). `dirty` needs nothing (set by `_push_command`). Known inherited trap: model `_undo/_redo`
are not cleared by bakes (existing pain point); the window stack rebuild covers the UI — do NOT
"fix" it here.

## 3. Canvas changes — pdftexteditor/ui/page_view.py

- New mode `"place_image"` joining 'select'|'add_text'|'text_edit' (current_mode :1168),
  registered as a handler in ws1 M4c's `_mode_handlers` dispatch (no new inline branch in
  `mousePressEvent`). `enter_place_image_mode(payload: dict)` / `exit_place_image_mode()`; payload =
  `{"image": bytes, "natural_px": (w,h), "kind": str}`. Crosshair cursor; entering flushes the
  editor (:2181) and exits add_text (and vice versa, :1267/:1276). Esc exits (keyPressEvent :2207).
- Placement (in `mousePressEvent` :1566, before the add_text branch): click -> resolve page like
  `_do_add_at` (:1831/:1844), compute `_pdf_point` (:2362), default width
  `min(natural_w_px*72/96, 0.45*page_width_pt)`, height by aspect, click = top-left, clamp to page;
  press-drag >3px slop instead rubber-bands a rect (QGraphicsRectItem at Z 45) with aspect locked to
  the image; release emits `boxCommandRequested("img_add", None, {"rect":..., "image":...,
  "kind":..., "natural_px":...})` and auto-exits the mode (one click = one image, mirrors
  main_window.py:1950).
- `ImageHotspot` — new transparent QGraphicsRectItem subclass at `Z_IMAGE_HOTSPOT = 7` (the ws1
  M4b registry slot; below SpanHotspot 10 so text always wins overlapping clicks, below form
  hotspots at 8). Built inline in `_materialize_page` (:965, hotspot build :1020-1029), NOT via
  the ws1 page-item factory: images are a box kind that must live in the page `_boxes` list so
  identity re-binding (:1034-1042, :1208-1222), selection, and `repaint_box` (:1462) work
  unchanged (the factory's `extra_items` are deliberately outside that machinery). Append
  hotspots for `document.image_boxes(page)` and (M3) `document.existing_images(page)` minus
  staged deletes. Rect from a
  new `_image_scene_rect` that maps `box.rect` through `rotation_matrix` * zoom + sheet origin —
  reuse `_scene_point` (:2352) on both corners.
- Selection: `select_box` on an image-like box (duck test: `identity[1] in ("img","xim")`) mounts a
  new `_ImageSelectionOverlay` — same constant-device-pixel `_ResizeHandle` pattern (:208, z 40/41)
  but RECT semantics: corner drag = free resize, Shift = keep aspect, edge drag = one axis;
  ExistingImage shows NO handles (move/resize of xim is M3 via context-less drag -> macro, see §5).
  Live preview during drag: one `QGraphicsPixmapItem` built from `QImage.fromData(box.image)` at
  z 45, scaled with `Qt.SmoothTransformation`; on release emit
  `boxCommandRequested("img_move", box, {"dx","dy"})` (PDF points, computed via `_pdf_point` deltas)
  or `("img_resize", box, {"rect": (x0,y0,x1,y1)})`. No second-click-to-edit promotion for images.
- `delete_selected` (:1374) and view-level Delete/Backspace (:2207): route to `"img_delete"` /
  `"xim_delete"` when the selection is image-like.
- `is_edited`/cover/editor logic: untouched — images never mount InlineRunEditor.

## 4. Window wiring — pdftexteditor/ui/main_window.py

- `BoxCommand` (:501): extend `kind` set + `_LABELS` with `img_add` ("Insert image"), `img_move`,
  `img_resize`, `img_delete`, `xim_delete` ("Delete page image"); `_apply` dispatches to the §2.3
  mutators. `_on_box_command_requested` (:1925): `img_add` passes box=None and, after push, selects
  the returned ImageBox (`view.select_box(cmd._box)`) — no text-edit drop-in, no cancel path needed
  (placement always carries bytes; there is no "empty image").
- Menus: NO new top-level menu (ws1 M4d skeleton + ws7 table own menu organization). The three
  entries below insert at `self.menu_anchors["tools_objects"]` in the Tools menu:
  - "Image from File…" (Cmd+Shift+I) -> `QFileDialog.getOpenFileName` filter
    "Images (*.png *.jpg *.jpeg)" -> read bytes + `QImage.fromData` for natural_px (reject invalid
    with `_flash_error` :2977) -> `view.enter_place_image_mode(payload)` + toast "Click or drag to
    place the image".
  - "Signature" submenu, rebuilt on `aboutToShow`: one entry per `SignatureLibrary.list()` item
    (QIcon thumbnail from the PNG, 32px) -> place mode; separator; "Draw Signature…" (Cmd+Shift+G)
    opens SignatureDialog; "Signature from File…"; "Manage Signatures…" ->
    `QDesktopServices.openUrl` on the library folder.
  - "Stamp" submenu: APPROVED / DRAFT / CONFIDENTIAL / COMPLETED / VOID -> `stamps.stamp_png(text)`
    -> place mode.
- Toolbar (`_build_toolbar` :1124): one "Insert Image" button + one "Signature" menu-button after
  the Find group; two new SVG entries `image`, `signature` in icons.py `_ICONS` (:23), 24px grid
  line style; hidden with no document (`_sync_actions` :2840 empty-state policy).
- Dialog seam (offscreen-test rule, precedent `_suppress_close_guard` main_window.py:1016): the
  Draw-Signature handler calls `self._signature_dialog_factory()` (attribute defaulting to the real
  class) and the library is `self._signature_library` (constructed once, dir injectable). Tests
  swap both; no `exec()` runs offscreen.
- Selection chrome: in the selectionChanged handler and `Inspector.set_target` call site, guard
  image-like boxes -> `inspector.set_target(None, None)` (placeholder text stays) and status-bar
  style slot shows `Image · {w:.0f} × {h:.0f} pt` (or `Page image · …` for xim). Inspector itself
  is untouched (it is pure chrome, inspector.py:3-6).
- Drag & drop: `CanvasContainer` drop handler (main_window.py:769-797) additionally accepts
  `.png/.jpg/.jpeg` -> read bytes -> place mode (PDF behavior unchanged).

## 5. Move an existing image (M3)

"Move" = one Qt macro (`undo_stack.beginMacro("Move page image")`) containing two BoxCommands:
`xim_delete` of the occurrence + `img_add` (kind="moved") with bytes from
`extract_image_bytes(xref)` at the offset rect. Model history holds two commands; the macro makes
it one UI undo step (precedent: Replace All macro, main_window.py:1626). Gesture: drag on a
selected ExistingImage hotspot arms a move preview exactly like ImageBox; release emits a new
signal-less path — view emits `boxCommandRequested("xim_move", xim, {"dx","dy"})` and the WINDOW
expands it into the macro (the view stays mutation-free). Limitation to document in code: deleting
an occurrence whose rect intersects another image removes that one too (probe §0 half-rect result);
acceptable for template work, called out in the toast ("Moved image — overlapping images on this
spot are replaced").

## 6. Signature subsystem (new files, all under the package => auto-bundled by
collect_submodules, PDFTextEditor.spec:18; no datas entries needed — stamps are procedural)

- `pdftexteditor/signatures.py` — Qt-light `SignatureLibrary(dir=None)`; default
  `os.path.expanduser("~/.pdftexteditor/signatures")`, `os.makedirs(exist_ok=True)` lazily on
  first save. `save(name, png_bytes) -> path` (sanitize to `[A-Za-z0-9 _-]`, dedupe `-2` suffix),
  `list() -> [(name, path)]` newest-first, `load(path) -> bytes`, `delete(path)`. Atomic write
  (temp + `os.replace`, same discipline as document.py:1720).
- `pdftexteditor/stamps.py` — `stamp_png(text, color="#B91C1C") -> bytes`: QImage ARGB32 at 3x,
  QPainter rounded-rect 3px border + bold uppercase text (theme.ui_font, semibold), transparent
  background, returns PNG bytes. Requires QGuiApplication (assert like document.py:1692).
- `pdftexteditor/ui/signature_dialog.py` — `SignatureDialog(QDialog)`:
  - `_SignatureCanvas` (QWidget ~620x220, white card + light baseline rule): mouse press/move/
    release appends stroke point lists; `strokes` attribute is plain data (testable).
  - Module-level PURE function `strokes_to_png(strokes, pen_width=3.0, color=QColor, scale=3)
    -> bytes`: paint onto transparent QImage at 3x supersampling, QPainterPath with midpoint
    quadratic smoothing (`quadTo(p[i], mid(p[i], p[i+1]))`), round cap/join, crop to ink bbox +
    8px margin. This function is the headless test surface.
  - Controls: pen width (2-6), color (Black/Blue), Clear, "Load from file…", name field, "Save to
    library" checkbox (default on), Use Signature / Cancel. Accept => `result_png()` bytes +
    optional library save. Theme via objectName QSS selectors (theme.py:163) — new names prefixed
    `Signature*`.

## 7. Undo / Save / WYSIWYG stories

- Undo: every image mutation = one `BoxCommand` on the shared stack driving the model in lockstep
  (first redo mutates, undo -> `document.undo()`); xim-move = one macro of two. Structural ops bake
  images into `self.working` and rebuild the Qt stack (existing behavior, main_window.py:2519) —
  fine-grained image history is lost at that boundary by design, identical to text.
- Save: `save_as` (:1670) bakes `_images`/`_xim_deletes` through the same `_apply_page_edits`; the
  opened file's bytes and `self.working` stay untouched until a structural bake. Atomic write
  unchanged.
- WYSIWYG: `render_with_edits` shares the pipeline, so the on-screen pixmap is byte-identical logic
  to the saved file — asserted by the existing rel-ink-diff < 0.02 metric in every milestone test.
- Perf note: an image edit makes its page take the staged-edit render path (~20-35ms,
  perf_audit fix #1 pending); `insert_image` itself is sub-ms. Build the hotspot preview QPixmap
  once per materialize, not per paint.

## 8. Milestones

### M1 — ImageBox end-to-end (model + bake + place/move/resize/delete + undo)
Model §2 (minus xim), canvas §3 (minus ExistingImage), window §4 minus Signature/Stamp submenus
(the `tools_objects` anchor ships with "Image from File…" only), drag&drop of image files.
Tests: NEW suite `tests/test_images.py` (harness pattern of tests/test_app.py:35: offscreen, one
QApplication, check(), exit 0). Fixture: temp copies of existing `tests/fixtures/form_like.pdf` +
`rotated_doc.pdf`; test image generated in-test (fitz.Pixmap RGBA, red/blue halves — asymmetric for
orientation asserts). Asserts: add_image stages (edit_count/dirty); region_ink appears inside rect
via render_with_edits; save_as -> reopen: `get_image_info` bbox == rect ±0.5pt, smask != 0 for the
RGBA source; WYSIWYG rel diff < 0.02; undo -> pixels pristine, redo restores; move/resize/delete
round-trip to saved rects; coexistence: a text stage_edit on the same page still passes
new_base14()==[] and text asserts; structural rotate after add bakes the image
(edit_count->0, image survives save); rotated page placement renders the red half on the correct
display side; UI path: enter_place_image_mode + synthesized click places, Delete key deletes,
window.undo_stack round-trips. All 9 existing suites stay green.

### M2 — Signatures + stamps (draw, library, one-click place)
Files §6, Signature/Stamp menus + toolbar button §4, dialog seam.
Tests: extend `tests/test_images.py` (keep one suite; sections). Asserts: `strokes_to_png` on
synthetic strokes -> valid PNG, `QImage.fromData` hasAlphaChannel, ink cropped (size << canvas),
>0 opaque px; `SignatureLibrary(tempdir)` save/list/load/delete + name sanitization (never touches
real `~/.pdftexteditor`); SignatureDialog constructed NON-modally offscreen, strokes injected,
accept -> result_png; place via the window handler with injected library/factory -> saved file has
smask and the text underneath the transparent region keeps its ink count; `stamp_png("APPROVED")`
places and saves. No real-name content anywhere (use "Jordan Carter" strokes label only in test
names).

### M3 — Existing images: detect, delete, move
Model xim parts of §2, second redaction pass §2.4(1), macro move §5, ExistingImage hotspots.
Tests: NEW fixture builder `tests/fixtures/build_image_fixtures.py` -> `image_doc.pdf` (committed):
page 0 = text "Jordan Carter — Acme Corp" + one RGBA gradient image (procedural, no external
files); page 1 reuses the SAME xref (logo scenario) + text. Asserts: `existing_images(0)` returns
one occurrence with the authored rect; stage delete -> save -> reopen: page-0 image ink ~0, page-0
text intact, page-1 occurrence intact (the §0 probe, now as a regression); WYSIWYG parity; move
macro -> ink at new rect / none at old, transparency preserved (smask on the reinserted image),
ONE undo_stack.undo() restores both; structural op after staged xim delete bakes it. Manifest
section appended to tests/fixtures/manifest.md (idempotent, pattern of build_reflow_fixtures.py).

## 9. DE-SCOPE

Excluded deliberately: image rotation handles and crop/opacity controls (template work never needs
them; page-rotation compensation is enough); editing existing-image pixels; clipboard image paste
(Cmd+V is taken by box paste, page_view.py:1426); cryptographic/digital signing (visual signature
only — local single-user app, no cert infrastructure); guaranteed move support for exotic codecs
(JBIG2/JPX occurrences are recoded to PNG via Pixmap, which may grow file size — accepted); multi-
select and image z-reordering; OCR (workstream rule).

## 10. CONFLICT RISK (files other workstreams will touch)

- `pdftexteditor/ui/main_window.py` — `_build_actions`/`_build_menubar`/`_build_toolbar`,
  `BoxCommand` kind set, `_on_box_command_requested`, selectionChanged/inspector guard. Annotations
  and forms workstreams will extend the same closed kind set: this spec establishes dispatch by
  identity tag (`"img"`/`"xim"`); they should mirror with their own tags ("annot", "widget").
- `pdftexteditor/ui/page_view.py` — mode set (via ws1 `_mode_handlers`), `_on_box_press` routing,
  `_materialize_page` hotspot build, keyPress delete routing, z-slot 7 (ws1 registry; annots=5,
  forms=8).
- `pdftexteditor/document.py` — `_apply_page_edits`/`_apply_page_edits_for`/`_bake_doc` signatures,
  `has_edits`/`edit_count`/`render_with_edits` filters, `_invalidate_caches`. Watermark/header-
  footer workstream touches the same bake tail; keep image draw LAST on the page unless a watermark
  "always on top" rule lands later.
- `pdftexteditor/ui/icons.py` `_ICONS` and `theme.py` QSS — additive, low risk.
- Tests: shared fixture dir + manifest.md appends are idempotent; no suite-file overlap.

## 11. Packaging

New modules live under `pdftexteditor/` => auto-bundled (PDFTextEditor.spec:18 collect_submodules).
No data files added (stamps procedural, signatures live in `~/.pdftexteditor/` at runtime, created
with makedirs). `build_app.sh` flow unchanged. No new pip dependencies (Qt + fitz + stdlib only).
