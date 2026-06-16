# WS6: Page & Document Tools

Acrobat-style document utilities: crop, watermark, header/footer, optimize, password
protection, metadata/properties, image export, text extract, print. The user reuses
PDF templates; nothing here may degrade the in-place text-edit pipeline. All
features ride the EXISTING funnels: structural ops via `_begin_structural`/
`_finish_structural` (document.py:2532), UI via `_run_structural`
(main_window.py:2473), save via `save_as` (document.py:1670). Stack locked:
PySide6 + PyMuPDF 1.27.2.3 + stdlib. No new pip installs.
## 0. Feasibility (all probed in this venv, PyMuPDF 1.27.2.3, PySide6 offscreen)

- `Page.set_cropbox(rect)` exists; rect is in MEDIABOX coordinates (MuPDF y-down).
  Probe: text at x=72, `set_cropbox(Rect(50,50,400,500))` -> rawdict bbox x=22, i.e.
  text space shifts by cropbox top-left; `page.rect` becomes 350x450. Survives
  `tobytes()` round trip, works on /Rotate 90 pages (rect still given unrotated).
  `page.derotation_matrix` exists.
- `Page.insert_text` kwargs include `fontname, fontfile, color, fill_opacity,
  stroke_opacity, morph, rotate, overlay, render_mode, oc`. Probe: morph rotation
  45 deg + fill_opacity=0.25 + overlay=False inserted fine. `rotate=45` raises
  ValueError (rotate kw is 90-multiples only; arbitrary angles must use morph).
- Rotated-page recipe (probed): insertion coords are UNROTATED space even on
  /Rotate'd pages. `p = disp_point * page.derotation_matrix; insert_text(p, ...,
  morph=(p, fitz.Matrix(1,1).prerotate(angle + page.rotation)))` renders text
  starting at the displayed point, reading at `angle` on screen (probe: ink box
  399..588 wide at pivot x=396 for angle=0 on a /Rotate 90 page).
- `Page.insert_image` kwargs include `rect, stream, pixmap, rotate, overlay,
  keep_proportion, alpha, mask, oc, xref`. `rotate=45` raises ValueError (90s only).
  Probe on /Rotate 90 page: rect mapped via derotation_matrix + `rotate=page.rotation`
  keeps the image upright on screen (asymmetric-image probe). `Pixmap.set_alpha`
  exists; alpha PNG via `fitz.Pixmap(pix, 1)` + `set_alpha` + `tobytes("png")`
  inserts with transparency.
- Encryption: `fitz.PDF_ENCRYPT_AES_256` (=5), `PDF_ENCRYPT_NONE` (=1), all 8
  `PDF_PERM_*` constants exist. `Document.save` accepts `encryption, permissions,
  owner_pw, user_pw`. Probe: saved AES-256 file has `needs_pass=1`; `insert_pdf`
  before auth raises ValueError "document closed or encrypted" (the known open crash,
  document.py:486-489); `authenticate("pw")` returns 2; `insert_pdf` after auth works
  and the copy's bytes are decrypted (`needs_pass=0`). `Document.permissions` exists.
- `Document.set_metadata` exists, survives `tobytes()`. CRITICAL probe: the
  constructor's `insert_pdf` deep copy DROPS metadata (working title reads ''), so
  today every save silently strips Title/Author from the original file. M1 fixes this.
- `Document.save` kwargs `garbage, clean, deflate, deflate_images, deflate_fonts,
  use_objstms, linear, preserve_metadata` all present; `ez_save` exists.
- `Page.get_pixmap(dpi=200)` works (612x792pt -> 1653x2339px). `Pixmap.save(path,
  jpg_quality=85)` writes JPEG by extension; PNG likewise. `get_text("text")`,
  `Document.get_page_fonts`, `page.get_fonts(full=True)` all present.
- `PySide6.QtPrintSupport` imports in this venv. QPrinter requires a constructed
  QApplication (SIGABRT otherwise). Probe offscreen: QPrinter(HighResolution, 1200dpi)
  with `OutputFormat.PdfFormat` + QPainter.drawImage + `newPage()` wrote a valid
  2-page PDF. This is the headless test seam for print.
## 1. Architecture

New files (auto-bundled via `collect_submodules('pdftexteditor')`,
PDFTextEditor.spec:18):
- `pdftexteditor/doctools.py`: Qt-free helpers: token substitution, 9-grid preset
  geometry, properties aggregation. Headless-testable.
- `pdftexteditor/ui/doc_dialogs.py`: `_WatermarkDialog, _HeaderFooterDialog,
  _PropertiesDialog, _SecurityDialog, _ExportImagesDialog,
  _CropDialog`. Pure chrome, never touch the model (Inspector pattern,
  inspector.py:3-6).

Dialog seam rule (offscreen modal trap, tests/test_app.py:249): every MainWindow tool
method takes an optional options object and only constructs its dialog when it is
None: `_do_watermark(opts=None)`, `_do_header_footer(opts=None)`,
`_do_crop_apply(page_index, rect, scope)`, `_do_export_images(opts=None)`,
`_do_export_text(path=None)`, `_do_print(printer=None)`, `_do_optimize(path=None)`,
`_do_properties(apply_meta=None)`, `_do_security(opts=None)`. Tests pass explicit
options. Password prompting goes through an injectable `self._password_provider:
Callable[[str, int], str|None]` (default QInputDialog.getText, Password echo).

Shared model refactor: extract `_baked_copy() -> fitz.Document` from save_as body
(document.py:1700-1716: fresh `fitz.open("pdf", working.tobytes())` + per-page
`_apply_page_edits`, with the QGuiApplication guard from 1692). MUST preserve ws1
M1a's persistent-engine change: the bake resolves through `self.font_engine`, never
a cold `FontEngine(out)`. `save_as`, `export_text`, and `save_optimized_copy` all
call it; render parity is preserved because it is the same pipeline
`render_with_edits` uses (document.py:2202). ws5 (forms, lands later) hooks
`_apply_form_values(out)` into this same helper — keep it a single seam.

Menus: insert into the ws1 M4d skeleton via `self.menu_anchors` — never build menus here.
At `file_output` (File menu): "Save Optimized Copy...", "Export" submenu ("Pages as
Images...", "All Text to .txt..."), "Print..." (Cmd+P), "Document Properties..."
(Cmd+D). The Document menu already exists (ws1 skeleton): "Crop Pages..." at
`doc_transform`; "Add Watermark..." and "Add Header & Footer..." at `doc_decorate`;
"Security..." at `doc_file`. All new QActions registered in
`_build_actions` (main_window.py:1043) and added to the window so shortcuts fire
regardless of focus (invariant, 1052); all disabled in the empty state via
`_sync_actions` (2840).
## 2. Features
### 2.1 Document Properties + metadata editor (M1)
UI: File > Document Properties... (Cmd+D). One dialog, two halves: editable
Description fields (Title/Author/Subject/Keywords) on top; read-only grid below:
file path, on-disk size, page count, unique page sizes (pt and inches, from
`page.rect`), PDF version, encryption status, fonts table (name, type, embedded).
Model (document.py):
- Constructor fix at 488: after `insert_pdf`, carry the info dict:
  `self.working.set_metadata({k: src.metadata.get(k) or "" for k in _META_KEYS})`
  with `_META_KEYS = ("title","author","subject","keywords","creator","producer",
  "creationDate","modDate")`; store `self.pdf_version = src.metadata.get("format")`
  (format/encryption are derived, not settable). Standalone bug fix: saves stop
  stripping the original metadata.
- TOC carry (absorbed from ws7 M1 — same lines, same kind of data-loss bug, lands
  here so bookmarks stop being wiped sooner): in the same constructor block, read
  `toc = src.get_toc()` BEFORE closing the source and `if toc:
  self.working.set_toc(toc)` (probe-verified in the ws7 spec: insert_pdf drops the
  TOC; set_toc + tobytes round-trips it). M1 test: open a tempfile doc built with a
  3-entry TOC, stage one edit, save_as, reopen -> `get_toc()` unchanged. ws7 M1
  keeps the outline()/set_outline() APIs and the BookmarkPanel; it must NOT
  re-implement the carry. Final constructor order (with M4): password gate,
  insert_pdf, metadata carry, TOC carry.
- `metadata_fields() -> dict` (working.metadata, the 4 editable keys).
- `set_metadata_fields(fields)`: `_begin_structural(); working.set_metadata(merged);
  _finish_structural()`. Undo = one StructuralCommand (bytes snapshot restores it).
- `properties() -> dict` and `fonts_used() -> list[tuple[str,str,bool]]` (aggregate
  `working.get_page_fonts(i)` over all pages, dedupe on basefont; embedded =
  ext != "n/a", probe-verified).
Wiring: OK with changed fields -> `_run_structural(lambda d: d.set_metadata_fields(f))`
(main_window.py:2473); unchanged -> no command. Dialog help notes that applying
metadata bakes pending edits (structural boundary wipes fine-grained undo,
main_window.py:2509), the established cost of every structural op. Save: metadata
lives in working, flows through save_as untouched. WYSIWYG: n/a.
### 2.2 Extract all text to .txt (M1)
UI: File > Export > All Text to .txt... (QFileDialog.getSaveFileName when path=None).
Model: `export_text(out_path)`: src = `_baked_copy()` if `has_edits` else `working`
(guarded; closes the copy); `"\f".join(page.get_text("text") for ...)`; normalize
U+00A0 to space, drop U+00AD (NBSP contract, tests/test_app.py:234); atomic temp +
`os.replace` (pattern at document.py:2743). Non-mutating: no undo, no dirty.
WYSIWYG: staged edits ARE included via the bake.
### 2.3 Export pages as PNG/JPEG (M1)
UI: File > Export > Pages as Images...: format (PNG/JPEG), DPI combo (72/150/300/600,
editable 18..1200), page range string (reuse `_parse_page_ranges`,
main_window.py:2687), destination folder. Files: `{stem}_page{n:03d}.{ext}`.
Window-level, no model change: per page `pix = document.render_with_edits(i,
dpi/72.0)`; `pix.save(path)` for PNG, `pix.save(path, jpg_quality=85)` for JPEG
(probe-verified). render_with_edits is the save pipeline, so exports match saved
output by construction. Result via `_toast` (main_window.py:2934). Non-mutating; no
undo. Perf: each call re-bakes that page (~20-35ms/edit); fine for an export loop.
### 2.4 Watermark, text only (M2; image watermarks DE-SCOPED — ws4's image placement
covers logos, text covers DRAFT/CONFIDENTIAL)
UI: Document > Add Watermark...: text controls: text, font
(Helvetica/Times/Courier + B/I via `fonts.base14_code_for_family`, fonts.py:79),
size (48), color (0.8,0.1,0.1); opacity slider 5..100% (default 30%),
angle (-180..180, presets 0/45/-45), 9-grid position preset (default
center), "Behind page content" checkbox, page range.
Model: `add_watermark(pages, *, text, base14_code="helv", fontsize=48.0,
color=(0.8,0.1,0.1), opacity=0.3, angle=45.0,
position="center", behind=False)` as ONE structural op (`_begin_structural`, loop,
`_finish_structural`); the inherited snapshot/bake/mutate ordering means a mid-loop
failure leaves no phantom undo entry (document.py:2532-2563).
PyMuPDF bake per page (coords computed in DISPLAYED `page.rect` space, then mapped;
recipes probe-verified in section 0). Anchor = 9-grid cell center, 36pt margin
(`doctools.preset_point`).
- Text: w = `fitz.Font(code).text_length(text, fontsize)`; voff = fontsize *
  (font.ascender + font.descender) / 2; start_disp = center - rot2d(angle) @
  (w/2, -voff); `p = start_disp * page.derotation_matrix`; `page.insert_text(p,
  text, fontsize=, fontname=code, color=, fill_opacity=opacity, morph=(p,
  fitz.Matrix(1,1).prerotate(angle + page.rotation)), overlay=not behind)`. rot2d
  sign is locked by the M2 ink test (flip if mirrored); the angle=0 rotated-page
  base case is probe-verified.
Undo: one StructuralCommand; `undo_structural` restores pre-op bytes.
Save/WYSIWYG: stamp lives in working; `render_with_edits` and `save_as` both read
working, so screen == file with zero extra work. Stamped text becomes ordinary page
content: spans() re-extracts after `_invalidate_caches`, so a text watermark is
afterwards editable/deletable like any box (documented behavior, not a bug).
Known hazards (comment them in code): (a) the neighbor heuristic (document.py:2304)
drops unedited spans >=50% inside an edited box's redact rect; a page-diagonal
watermark is far larger than any single redact rect, so it falls in the redraw band
(rotated redraw supported, document.py:2315,2344); the M2 edit-on-watermarked-page
test locks this. (b) The inline editor's flat cover color (page_view.py:2456) looks
wrong while editing OVER a translucent watermark; cosmetic, commit is correct.
### 2.5 Header & footer with page-number tokens (M2)
UI: Document > Add Header & Footer...: six fields (header/footer x left/center/
right), tokens `{page}` `{pages}` `{date}` (insert buttons), "start numbering at"
spin, font trio + size (default Helvetica 9), color, margins (top/bottom baseline
30pt, side 36pt), page range, live preview of the substituted page-1 string.
Model: `add_header_footer(pages, *, slots: dict[str,str], base14_code="helv",
fontsize=9.0, color=(0,0,0), top=30.0, bottom=18.0, side=36.0, start_at=1)` as ONE
structural op. Per page, per non-empty slot: `doctools.substitute_tokens(template,
page_no=start_at+k, total, date)` (date `%Y-%m-%d`); displayed-space baseline y =
`top` (header) or `page.rect.height - bottom` (footer); x = side / centered via
`text_length` / right-aligned; insert with the derotation + `morph=(p,
Matrix(1,1).prerotate(page.rotation))` recipe, opacity 1, overlay=True. Tokens are
substituted AT STAMP TIME: static content, later reorders do not renumber (the
dialog states this). Undo/save/WYSIWYG: identical to watermark.
### 2.6 Crop pages (M3)
UI: Document > Crop Pages... checks a new `act_crop` and calls
`view.enter_crop_mode()`; toast "Drag a rectangle over a page, Esc to cancel". On
release a `_CropDialog` shows the rect (pt) with scope radios (This page / All
pages) + Apply/Cancel.
Canvas (page_view.py): new mode string `'crop'` alongside add_text, registered as a
handler in ws1 M4c's `_mode_handlers` dispatch (no new inline branch in
`mousePressEvent`): `enter_crop_mode()/exit_crop_mode()`, crosshair cursor; the
press handler records the anchor and the pressed page; drag draws a dashed accent QGraphicsRectItem at
Z=45, clamped to that page's sheet; release maps both corners via `_pdf_point`
(page_view.py:2362, exact inverse of `_scene_point`, so the rect is in unrotated
text space) and emits a new signal `cropRectSelected(int, tuple)`. No model mutation
in the view (invariant, page_view.py:44).
Window: `_on_crop_rect_selected` -> `_CropDialog` -> `_do_crop_apply(page_index,
rect, scope)` -> `_run_structural(lambda d: d.crop_pages(pages, rect))` -> exit crop
mode. Connect defensively via `_connect_signal` (main_window.py:1800).
Model: `crop_pages(pages: list[int], rect: tuple)` structural op. Per page:
`cb = page.cropbox; media = fitz.Rect(rect) + (cb.x0, cb.y0, cb.x0, cb.y0);
media &= page.mediabox`; skip page if either dimension < 36pt (collect, report);
`page.set_cropbox(media)`; ValueError if every page skipped. The text-space-to-
mediabox shift by cropbox.tl is probe-verified (72 -> 22 with x0=50).
Coordinate safety: `_begin_structural` bakes pending edits BEFORE the cropbox moves
the rawdict origin, `_finish_structural` runs `_invalidate_caches` (document.py:
2521), so no staged Edit or cached span spans the shift; `_after_structural_op` does
a full `view.reload()` (main_window.py:2489) rebuilding `_PageLayer.pt_size` from
the new `page.rect`. This honors the crop trap flagged in the model map.
Undo: one StructuralCommand (snapshot restores the old cropbox). Save: cropbox is in
working, flows through save_as. WYSIWYG: render reads working; parity is free.
### 2.7 Password protect / remove password (M4)
Open gate (fixes the encrypted-open crash, document.py:486):
- New `PasswordRequired(ValueError)` in document.py. `PDFDocument.__init__(path,
  password=None)`: after `fitz.open(path)`, if `src.needs_pass` and (no password or
  `src.authenticate(password) == 0`): raise PasswordRequired. Store
  `self.original_encrypted`, `self._open_password`. Post-auth `insert_pdf` yields a
  DECRYPTED working copy (probe-verified), so FontEngine/render/edit are untouched.
- `Workspace.open(path, password=None)` (workspace.py:83) passes it through.
- `open_path` (main_window.py:2188): loop (max 3 attempts) catching PasswordRequired,
  asking `self._password_provider(path, attempt)`; None -> silent abort.
Security settings (save-time state, like Acrobat's Security tab):
- Model: `self._security = {"encryption", "user_pw", "owner_pw", "permissions"}`,
  defaulting to AES-256 + the open password when `original_encrypted` (protection
  survives Save), else `PDF_ENCRYPT_NONE`. `set_security(**kw)` sets
  `_security_dirty`; `dirty` (document.py:1652) ORs it; `mark_clean` (1662) clears
  it. `_save_kwargs() -> dict` builds `encryption/user_pw/owner_pw/permissions`
  (all PDF_PERM_* bits; granular permissions de-scoped).
- save_as line 1724 becomes `out.save(tmp, garbage=4, deflate=True,
  **self._save_kwargs())`.
- `_reload_after_save` (main_window.py:2357) reopens the saved file: it MUST pass
  `document.reopen_password` (pending user_pw, else `_open_password`) or saving an
  encrypted file crashes the reload. Regression test required.
UI: Document > Security...: status line ("Not encrypted" / "AES-256 password set"),
set/change password (password + confirm), "Remove Security" (encryption NONE, clear
pws). Applies via `set_security`; toast "Security changes apply on next save". Undo:
NOT on the undo stack (a pending save option, not a document mutation); the dialog
itself is the revert surface, and says so.
### 2.8 Compress / optimize (M4)
UI: File > Save Optimized Copy...: just the Save-file picker — NO options dialog
(_OptimizeDialog DE-SCOPED; fixed sensible flags, all probe-verified). Model:
`save_optimized_copy(out_path) -> tuple[int, int]`: `out = self._baked_copy()`;
`out.save(tmp, garbage=4, deflate=True, clean=True, deflate_images=True,
deflate_fonts=True, use_objstms=True, **self._save_kwargs())`;
atomic replace; return (before, after) byte sizes (before from
`os.path.getsize(self.path)`). Window toasts "Optimized: 1.4 MB -> 0.9 MB (-36%)".
Non-mutating export: the tab does NOT repoint to the copy; no undo.
### 2.9 Print via QtPrintSupport (M4)
UI: File > Print... (Cmd+P). `_do_print(printer: QPrinter|None = None)`: when None,
`QPrinter(QPrinter.PrinterMode.HighResolution)` + `QPrintDialog(pr, self)` with page
range set; rejected -> return. Loop selected pages (fromPage/toPage, 0 = all):
`zoom = min(printer.resolution(), 600) / 72.0`; `pix = document.render_with_edits(i,
zoom)` (staged edits included, parity with save by construction); wrap as
`QImage(bytes(pix.samples), pix.width, pix.height, pix.stride,
QImage.Format.Format_RGB888).copy()` (copy keeps samples alive); scale into
`printer.pageRect(QPrinter.Unit.DevicePixel)` preserving aspect via
QPainter.drawImage; `printer.newPage()` between pages (probe-verified offscreen,
PdfFormat). Import QtPrintSupport at module top of main_window.py so PyInstaller's
PySide6 hooks bundle it; M4 runs build_app.sh to confirm. Non-mutating; no undo.
## 3. Undo / save / WYSIWYG summary

| Feature | Mutation | Undo | Save | WYSIWYG |
|---|---|---|---|---|
| Metadata edit | structural | StructuralCommand | in working | n/a |
| Watermark | structural | StructuralCommand | in working | free (working renders) |
| Header/footer | structural | StructuralCommand | in working | free |
| Crop | structural | StructuralCommand | in working | free, full reload |
| Security | pending save option | none (dialog reverts) | save_as kwargs | n/a |
| Optimize / image export / txt / print | none | none | export-only | via bake/render_with_edits |
## 4. Milestones. Each lands with ALL suites green (test_app, test_font_fidelity,
test_editor_model, test_editor, test_pages, test_reflow, test_richtext, plus
test_redesign, test_text_tools, and the new tests/test_doctools.py).

M1 "Document info & exports": constructor metadata carry + pdf_version capture;
`_baked_copy` refactor; properties/fonts_used/metadata_fields/set_metadata_fields/
export_text; export-images flow; Properties dialog; File menu entries. Tests (new
tests/test_doctools.py, harness pattern of tests/test_app.py: offscreen,
module-global QApplication, check() list, `_suppress_close_guard`, absolute paths):
metadata survives open -> save_as (regression for the probe-verified loss);
set_metadata_fields then undo_structural restores the old title; export PNG at
144 dpi has pixel size = page.rect * 2 and nonzero ink; with a staged edit the
exported PNG differs from the unedited render (ink-diff pattern,
tests/test_editor.py:208); export_text contains the edited string NBSP-normalized
plus the fixture's "Jordan Carter". Fixture: new builder
tests/fixtures/build_doctools_fixture.py -> doc_tools.pdf (3 letter pages, title
"Quarterly Review Agenda", author "Jordan Carter", body "Acme Corp ...", saved with
deflate=False so M4's optimize has a measurable delta); update fixtures/manifest.md.

M2 "Stamps": doctools.py helpers (presets, tokens); add_watermark (text only);
add_header_footer; both dialogs; Document-menu entries at the ws1 anchors. Tests:
text watermark on
three_page.pdf center region gains ink; opacity 0.25 lighter (mean luminance) than
1.0; behind=True still visible on a blank region; undo_structural returns region ink
to baseline; header tokens render "Page 2 of 3" on page 2 (`get_text`);
rotated_doc.pdf gets an upright header (ink box wider than tall in the displayed
band); WYSIWYG: render_with_edits vs saved file rel ink diff < 0.02 after stamping
plus one in-place edit on the same page; the edited box leaves no residue
(tests/test_editor.py:537 pattern).

M3 "Crop": crop_pages model op; PageView crop mode + cropRectSelected; _CropDialog +
wiring; act_crop. Tests: crop_pages([0], rect) shrinks page.rect to the expected
size; spans() re-extracts with coords shifted by exactly cropbox.tl; a staged edit
made BEFORE crop survives in the cropped save; undo_structural restores the rect;
all-pages scope on three_page.pdf (mixed sizes) clamps per page; sub-36pt rect
raises. UI: enter_crop_mode -> synthesized press/drag/release (QMouseEvent pattern,
tests/test_reflow.py:552) emits cropRectSelected with the expected text-space rect;
`_do_crop_apply` (dialog bypassed) runs the op and reloads; layer pt_size matches.

M4 "Security, optimize, print": PasswordRequired + constructor gate + Workspace/
open_path threading + _password_provider; _security/set_security/_save_kwargs +
save_as change + reopen_password + _reload_after_save fix; save_optimized_copy;
_do_print; Security dialog (optimize has none — fixed flags); build_app.sh smoke build. Tests: encrypted
fixture generated AT RUNTIME into tempfile (never committed): doc_tools.pdf copy
saved AES-256 user_pw "fixture-pass"; PDFDocument(path) raises PasswordRequired;
PDFDocument(path, "fixture-pass") opens, spans extract, edit + save_as yields
needs_pass=1 output that authenticates and contains the edited text;
set_security(encryption=NONE) + save -> needs_pass=0; open_path with injected
provider opens the tab; dirty flips on set_security, clears on mark_clean. Optimize:
on the deflate=False fixture, after < before with identical page text. Print:
`_do_print` with an injected PdfFormat QPrinter to tempfile yields page_count == doc
pages, nonzero ink; with a staged edit, printed page 1 ink differs from clean.
## 5. DE-SCOPE
Excluded deliberately: OCR (mandated out); image downsampling/recompression in
optimize (only fitz save flags are probe-verified; resampling needs a raster
pipeline this app lacks); IMAGE watermarks entirely (realism cut by the build plan:
ws4's image placement covers logos; the alpha-pixmap recipe in §0 stays probed for a
future follow-up); an optimize options dialog (fixed flags, see 2.8);
arbitrary font families in stamps (base-14 trio only,
avoiding save-side font registration outside the bake pipeline, document.py:2362);
a manager UI for editing existing stamps (stamps are page content; remove via
structural undo or box delete); granular permission checkboxes (all-permissions
AES-256 only); per-page distinct crop rects in one apply; print layout options
beyond the native dialog; linearized saves; repointing the tab at the optimized copy.
## 6. CONFLICT RISK (files other workstreams will also touch)
- `pdftexteditor/ui/main_window.py`: `_build_actions` (1043), `_build_menubar`
  (1358), `_sync_actions` (2840) are hot spots for EVERY workstream; keep ws6
  additions in contiguous `# ws6` blocks.
- `pdftexteditor/document.py` constructor (478-490): RESOLVED by the build plan —
  ws6 M1 carries BOTH metadata and TOC (absorbed from ws7 M1); ws6 M4 adds the
  password gate. Order: password gate, insert_pdf, metadata carry, TOC carry. ws7
  builds the bookmark APIs/panel on top without touching the carry.
- `document.py` save_as (1670-1731): ws6 adds `_save_kwargs` + `_baked_copy`
  (preserving ws1's persistent FontEngine); the TOC needs no save_as change (it
  lives in working bytes after the M1 carry). ws5 later hooks `_apply_form_values`
  into `_baked_copy`.
- `pdftexteditor/ui/page_view.py` mode routing (1566, 2207, 2352): annotation and
  text-selection workstreams add modes/items; ws6 adds only the 'crop' branch and
  one signal; keep the mode-string layering contract.
- `pdftexteditor/workspace.py` `open()` (83): password kwarg; trivial but shared.
- `tests/fixtures/manifest.md`: every workstream appends.
- PDFTextEditor.spec: no edit expected (QtPrintSupport bundled by PyInstaller's
  PySide6 hooks once imported); M4 runs build_app.sh to verify.
