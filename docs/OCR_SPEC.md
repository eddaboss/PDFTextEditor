# OCR_SPEC — On-Device OCR for image-only pages

Status: **IMPLEMENTED (Tier 1, June 2026).** Goal: turn image-only / scanned PDF
pages into text the app can search, select, and **edit in place**, fully offline,
on macOS and Windows, reusing the existing span/box model. No cloud (no Google
Document AI).

All cited `file:line` hooks were verified to exist at authoring time.

## What shipped (vs this plan)

Built the **ClearScan-class** path, not the "Helvetica/black" MVP this plan
opened with: a scanned page is REPLACED with editable text rendered in a **custom
OTF font built from the page's own scanned glyphs**, so it looks like the scan and
every letter is real editable text (Decisions block + §8 Tier 1). New package
`pdftexteditor/ocr/`:
- `engine.py` — RapidOCR (PP-OCR / ONNX) primary + optional Apple Vision path.
- `segment.py` — connected-component + text-guided per-line segmentation:
  word boxes, line baseline, and clean per-character glyph harvesting.
- `fontbuild.py` — cv2 binarize → vtracer trace → fontTools T2/CFF: a real
  embeddable OTF. Observed chars traced from the scan; missing chars BORROWED
  from a serif/sans base font (decomposed), so editing never hits a blank glyph.
- `reconstruct.py` — orchestrates page → font + one editable line-box per line
  (PDF points), with the font's own advances + measured space carrying spacing.

Integration: `FontEngine.register_custom_face` makes the scan font resolve as a
TIER_SYSTEM face (whole existing bake path, **zero new bake code**);
`PDFDocument.render_page_image` / `page_has_text_layer` / `image_only_pages`;
`MainWindow` "OCR This Page" / "OCR Document" (Document menu) run a daemon-thread
worker + QTimer poll, applying each page as ONE undo step (BoxCommand macro).

Validated end-to-end: a previously image-only PDF becomes real extractable text
(19/19 word recall clean; 24/24 on a sans-serif 200-dpi noisy+rotated+JPEG scan),
the scan font embeds on save, and one undo reverts a page's OCR.

Not done (matches plan): invisible searchable-layer mode (§3 opt-in), per-word
typeface/bold-italic estimation, Tier-2 ML glyph generation (§8). PyInstaller
bundling of onnxruntime/models is deferred (Mac-first, runs from source).

## Decisions (Edward, June 2026)
- **Scope: a personal macOS "free Acrobat" right now.** Mac-first is fine; the
  cross-platform/Windows packaging and "shippable to the office" concerns are
  DEFERRED and must not constrain the design.
- **OCR result = REPLACE the page with real editable text rendered in a CUSTOM FONT
  BUILT FROM THE SCANNED GLYPHS** (ClearScan-class). The font reproduces the exact
  scanned letterforms, so the page looks **pixel-identical** to the scan, but EVERY
  letter is now real, selectable, editable text. The original scan image is not kept
  as a picture; the letters themselves become text.
- **Editing uses that custom scan-built font**, so edits match the surrounding text.
  Pipeline: OCR per-character boxes (rapidocr) → vectorize each glyph (vtracer) →
  assemble a TTF (fontTools) → bake the recognized text at the original
  positions/sizes in that font. CPU, **fast (seconds/page)**, existing tools.
- **Missing-character handling (so "no shortcuts" is real):** letters that appeared
  on the page → exact traced shapes; letters that NEVER appeared → filled from the
  closest-matching real font (font identification) so every character exists and
  stays roughly in style. The ONE genuinely-unavailable piece is ML-*generating* the
  exact distorted shape of an unseen letter (no ready on-device Latin engine, per §8
  Tier 2) — a separate future stretch, explicitly NOT a corner cut from the core.
- **Optionally deskew** ("correct for lean") before recognition.
- **Languages = English only.** **Scope = personal macOS tool, fully built out, no
  shortcuts.** Needs OCR (rapidocr/onnxruntime) + vtracer + fontTools; NOT
  PyTorch/FontDiffuser.

---

## 1. Goal & output modes

Two modes, both worth shipping:

- **Editable text (DEFAULT).** Each recognized word becomes a `NewBox` via
  `add_box` (`document.py:2191`) with an estimated font/size/baseline. It bakes
  through the exact same `resolve_family` → `_insert_run` path as user-typed text
  (`font_engine.py:586`, `document.py:4566`) with **zero new bake code**. This is
  the product's reason to exist: edit scanned text in place.
- **Invisible searchable layer (opt-in).** Render-mode-3 text placed over the
  untouched scan (Adobe `SEARCHABLE_IMAGE_EXACT` model): search + copy without
  altering appearance. Needs one small new branch in `_insert_run`
  (`document.py:4566`) — see §3.6.

Default = editable text: matches the editing product, needs no new bake code, and
the scan can optionally be kept underneath. We do **not** build Adobe's ClearScan
custom-font synthesis — the app already embeds real system/Base14 faces, which is
simpler and good enough.

### How Adobe does it (reference)
- *Searchable Image*: keep scan pixels, add an invisible OCR layer (search/select,
  zero visual change).
- *Editable Text / ClearScan*: replace scanned glyphs with editable text, synthesize
  custom fonts to match the scan.
- Pipeline both share: deskew/denoise/orientation → layout analysis → detect →
  recognize + language model → estimate font/size/baseline → emit text + confidence
  (low-confidence words flagged as "suspects").

---

## 2. Engine decision

**Primary, cross-platform: RapidOCR (PaddleOCR PP-OCRv5 models on ONNX Runtime).**
- **Packaging (the decisive reason):** `onnxruntime` is one clean shared lib;
  `rapidocr` is pure Python. No PaddlePaddle runtime, no DLL hunting — fits the
  PyInstaller `.app`/`.exe` model. (PaddleOCR-native is a documented PyInstaller
  nightmare; rejected.)
- **Quality:** PP-OCRv5 mobile (~5M params) is OmniDocBench-SOTA among non-VLM
  engines; detection + recognition + angle in one pipeline; per-line confidence;
  `return_word_box=True` gives per-word boxes — exactly the geometry needed to place
  `NewBox`es.
- **Speed:** ~0.2–1 s/page CPU. **Footprint:** ~100 MB (ORT ~30 MB + mobile models
  ~20 MB + numpy/Pillow). **License:** Apache-2.0 (engine + models).
- **Cross-platform:** identical code path on macOS arm64/x64 and Windows x64.

**macOS fast path: Apple Vision** (`VNRecognizeTextRequest`, `accurate`) via `ocrmac`.
Zero added bundle bytes (framework is in the OS), uses the Neural Engine, excellent on
printed text. Use **line-level** observations (accurate-mode per-character bbox is
unreliable); split to words on whitespace.

**Rejected:** PaddleOCR-native (packaging), EasyOCR (stale, weakest accuracy, ~1.5 GB
PyTorch), docTR/Surya (slow on CPU / GPL), Windows.Media.Ocr (needs MSIX package
identity, unreachable from a PyInstaller `.exe`).

**Abstraction:** one internal interface `recognize(image, dpi) -> [Word{text,
bbox_px, confidence, line_id}]`, two adapters (RapidOCR, AppleVision), selected as
`AppleVision if macOS and enabled else RapidOCR`. Everything downstream is
engine-agnostic. A new `pdftexteditor/ocr/` module hosts the adapters + reconstruction.

---

## 3. Pipeline (stage → app hook)

1. **Detect no-text pages** — new `page_has_text_layer(i)` / `image_only_pages()` next
   to `spans()` (`document.py:1130`). Signal: `len(spans(i)) == 0` (reuses the
   type-0/type-1 block split at `document.py:1151`); cheap pre-check
   `page.get_text("text").strip() == ""`. Only OCR pages with no real text layer.
2. **Render at OCR DPI** — existing `get_pixmap(matrix=Matrix(zoom,zoom))` (`render`,
   `document.py:1104`). Default **300 dpi** (zoom ≈ 4.17); 400–600 option for small
   print. Convert pixmap → numpy/PIL buffer.
3. **Preprocess** — minimal for MVP: PP-OCRv5 does deskew/angle internally; Apple
   Vision handles orientation. Optional grayscale + light denoise only if accuracy
   demands. Full Adobe-style filter stack deferred to P3.
4. **Detect + recognize** — one engine call/page → word/line boxes (pixels) +
   confidence.
5. **Reconstruct into app geometry:**
   - pixels→points: `pt = px / (dpi/72)`; match PyMuPDF Y-axis convention (flip if
     needed) that `add_box` expects.
   - **Baseline:** bottom-left of the word box nudged up by the descender fraction;
     per-line, fit a common baseline so words align.
   - **Font size:** word-box pixel height ÷ (dpi/72), rounded.
   - **Family/style:** default `"Helvetica"` (guaranteed in `available_families()`);
     `bold=italic=False` for MVP. `_newbox_bbox` (`document.py:2209`) re-derives the
     real width from the resolved font advance, so slight width error self-corrects.
6. **Inject:**
   - **Editable (default):** one `add_box(page, origin, text, family, size,
     color=black, bold, italic)` per word (`document.py:2191`). Word granularity keeps
     boxes individually editable and width estimates tight.
   - **Searchable layer (opt-in):** new render-mode-3 branch in `_insert_run`
     (`document.py:4566`) — `shape.insert_text(..., render_mode=3)` at the baseline,
     no redaction, scan untouched. NewBox carries `searchable=True` to pick the
     invisible branch; resolve/registration unchanged.

---

## 4. Integration

- **Menu:** "OCR This Page" + "OCR Document" in the Document menu via `_menu_anchor`
  in `_build_menu_document` (`main_window.py:2917`), alongside crop/watermark/security.
  Small dialog: mode (editable/searchable), DPI, engine (auto/RapidOCR/AppleVision).
- **Enablement:** no-text detection grays out OCR on pages that already have text;
  "OCR Document" silently skips text pages.
- **Undo / structural model:**
  - Bulk page/document OCR → `_run_structural(op)` (`main_window.py:4964`): `op(doc)`
    stages all NewBoxes, committed as **one** `StructuralCommand` (skeleton
    `_begin_structural`→mutate→`_finish_structural`, `document.py:4879`; template
    `insert_blank_page`). One undo reverts a whole page's OCR.
  - Interactive single-region OCR (later) → batched `BoxCommand`s in
    `undo_stack.beginMacro/endMacro`, mirroring `_move_existing_image`
    (`main_window.py:4020`).
- **Threading:** follow the app's async precedent `prewarm_system_index`
  (`font_engine.py:775`): `threading.Thread(daemon=True)` for render+inference; GUI
  thread polls completion via a `QTimer` (50 ms `inspector.py` pattern). All
  `add_box`/font-resolver/Qt work runs **back on the GUI thread** (the
  `QGuiApplication` guards require it). No QThread/QRunnable introduced.
- **Progress:** minimal indicator driven by the QTimer poll ("page X of N"); cancellable.
- **Confidence:** inject all recognized text; words below ~0.6 get a visual "suspect"
  flag (tint/marker) for review. Never drop text silently. Threshold exposed later.

---

## 5. Bundling & cross-platform (PyInstaller)

- **New deps:** `rapidocr`, `onnxruntime` (PyInstaller-friendly); `ocrmac` +
  `pyobjc-framework-Vision` **macOS-only** — gate behind `sys.platform == "darwin"`,
  never import on Windows.
- **Models:** **vendor** PP-OCRv5 mobile ONNX (det ~8 MB + rec ~10 MB + cls ~1 MB)
  into the bundle under `_MEIPASS`, pointed at via a RapidOCR config YAML. Offline at
  install (the share-with-the-office goal). First-run download stays a fallback for
  higher-accuracy server models.
- **Spec specifics:** `--add-data` the `.onnx` + config; `--collect-binaries
  onnxruntime` / hidden imports so the native libs ship. macOS Vision adds 0 bytes.
- **Size bump:** ~80–110 MB. Far below EasyOCR's ~1.5 GB.

---

## 6. Performance (CPU-only)

- Per-page: render (~tens of ms @ 300 dpi) + inference (~0.2–1 s). Interactive-OK.
- All OCR off the GUI thread; inject on the GUI thread. Document OCR is sequential on
  the worker, posting per-page results for incremental, cancellable UI.
- **Lazy engine init:** build the RapidOCR session **once** on first use, cache and
  reuse across pages/documents (model load is the cost). Mirrors `FontEngine` lazy/prewarm.
- Cache `image_only_pages()` per document; invalidate on structural edits (like
  `_finish_structural`). Don't re-OCR a page that already has staged NewBoxes.
- DPI is the speed/accuracy dial: 300 default, escalate only for flagged small print.

---

## 7. Phasing, risks, open questions

### Phasing
- **P1 (MVP):** RapidOCR only, editable mode, "OCR This Page," 300 dpi, Helvetica/black,
  word-level NewBoxes via the structural op, daemon-thread + QTimer, basic progress,
  confidence stored + shown as a color flag. Bundled mobile models.
- **P2:** "OCR Document" (sequential, cancellable), Apple Vision fast path on macOS,
  OCR options dialog, suspect-word UI.
- **P3:** invisible searchable-layer mode (render-mode-3 branch in `_insert_run`),
  keep-scan-underneath option, app-side deskew/binarize for rough scans, optional
  server models.
- **P4 (maybe):** per-word typeface/size/bold-italic estimation, region-select OCR,
  multi-column reading-order improvements.

### Risks
- **Baseline/coordinate mapping** is the trickiest part (engines give cap/line boxes,
  not baselines; PyMuPDF Y-axis vs engine convention). Mitigation: per-line baseline
  fit + `_newbox_bbox` self-correction; validate on real scans early.
- **Word explosion** on dense pages → many NewBoxes. Mitigation: one undo step via the
  structural op; consider line-level boxes if per-word proves heavy.
- **PyInstaller + onnxruntime native libs** needs a clean-machine packaging test on
  both OSes (especially Windows).
- **Poor scans** without preprocessing may underperform; P3 adds filters.
- **Apple Vision accurate-mode char-bbox bug** — use line-level only (planned).

### Open questions
1. ~~Replace vs keep scan underneath~~ → **RESOLVED: replace, appearance-faithful** (see Decisions).
2. ~~Languages~~ → **RESOLVED: English only** (see Decisions).
3. Fully **offline at install** (vendor models, the default), or first-run download OK? — pending.
4. OK with the **~80–110 MB** bundle bump? — pending (tied to the on-hold install).
5. **Invisible searchable-layer** mode in scope at all, or editable-only? — pending (P3).

---

## 8. Font fidelity — making the replacement look like the scan (researched)

The "replace with the exact same text/font, including distortions, and let edits use
that same font even for letters not in the scan" goal is **two features**, at very
different maturity. Verdict from research:

- **No ready-to-bundle engine does the full vision.** Nobody has fused "build a font
  from scanned glyphs" + "infer unseen letters in that style" into a shippable
  desktop tool. The closest full-pipeline FOSS reference (`ncraun/smoothscan`) is
  GPL/Linux/2013 — architecture reference only, not a dependency.

### Tier 0 — system-font match (today's MVP default)
Pick a bundled real font, size+position from the OCR box. Edits work for all
characters, but it does **not** look like the scan. Always-works fallback.

### Tier 1 — ClearScan-class custom font (the achievable "looks like the scan" core)
- **Pipeline:** rasterize (PyMuPDF) → OCR per-character boxes (Tesseract/rapidocr) →
  crop+threshold each glyph → **vtracer** bitmap→SVG outline → cluster by character,
  pick a representative outline → **fontTools** `FontBuilder` builds a real TTF from
  the Bezier paths + box-derived advances → embed via **pikepdf**/PyMuPDF. The
  displayed text then uses the **actual scanned letterforms with their distortions**.
- **On-device:** CPU-only, no ML, ~50 MB; every dep has prebuilt mac+win wheels
  (vtracer Rust wheels, fontTools pure-Python, pikepdf ships QPDF). Cross-platform,
  bundleable, shippable as a normal feature.
- **Fundamental limitation (same one Adobe ClearScan has):** the custom font only
  contains glyphs that **appeared on the page**. Typing a character the scan never
  showed (a `Q`, `z`, `7`, `;` that wasn't there) renders as a blank box. Plus
  ligature/ToUnicode gotchas hurting copy/search.
- **The clean fix:** keep **untouched scanned text** in the Tier-1 custom font, and
  default **new/edited text to a normal bundled font** (Tier 0). Sidesteps the
  blank-box problem entirely. (Optional experimental "match scan style for new text"
  toggle later.)

### Tier 2 — infer unseen glyphs in-style (the full vision; research-grade, NOT now)
- **Best repo:** `yeungchenwa/FontDiffuser` (AAAI'24, diffusion, pretrained). Fallback
  `clovaai/mxfont` (MIT, weaker). Adobe's vector method (VecFusion) has no code.
- **Why it's not shippable now:** FontDiffuser is **non-commercial license**,
  **CJK-trained** (Latin quality unbenchmarked, likely needs retraining), **raster-only
  96×96** (needs vectorization bolt-on), and **PyTorch ~800 MB–1.5 GB** bundle. Runs on
  **Apple Silicon MPS** (~5–15 s/glyph → 10–30 min one-time bake for a full Latin set,
  *if* quality holds), but **Windows integrated GPU has no ready path** (would need a
  hand-built ONNX/DirectML export). Net: weeks-to-months of ML work + real risk the
  Latin output looks "off." Park as an opt-in, Apple-Silicon-first, personal-use
  prototype; do not bundle PyTorch into the shipped app until proven.

### Recommended font-fidelity path
**Tier 1 for display (untouched scanned text in the rebuilt scan font) + Tier 0 for
new/edited text.** That delivers most of the visual magic on-device and cross-platform
now, and is exactly the compromise Adobe itself never cleanly crossed. Tier 2 stays a
separate, optional research bet.

---

## Relevant files
- `pdftexteditor/document.py` — model (`add_box` 2191, `_newbox_bbox` 2209), bake
  (`_insert_run` 4566), no-text hook (~`spans` 1130 / `page_words` 4410), structural
  skeleton (`_begin_structural`/`_finish_structural` ~4879), render (`render` 1104).
- `pdftexteditor/font_engine.py` — `resolve_family` 586, async precedent
  `prewarm_system_index` 775.
- `pdftexteditor/ui/main_window.py` — menu anchors (`_build_menu_document` 2917),
  `_run_structural` 4964, `BoxCommand` macro pattern (~`_move_existing_image` 4020).
- New: `pdftexteditor/ocr/` — engine adapters (RapidOCR, AppleVision) + reconstruction.
