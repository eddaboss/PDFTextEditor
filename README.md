# PDF Text Editor

A local, Acrobat-style PDF text editor for macOS. Click any line of text in a
born-digital PDF, retype it in place, and save a new PDF where the original
glyphs are removed and replaced with text set in the document's **own font**.

Built on PySide6 (Qt) for the UI and PyMuPDF (`fitz`) for all PDF work.

## What works

- **Open** a PDF, render pages, navigate, and zoom (fixed / fit-page / fit-width).
- **In-place editing.** Click any run of text and an in-scene editor mounts
  directly over it in the run's real font family, size, color, weight, italic,
  and baseline, so editing is visually indistinguishable from set type. No popup
  box. Enter commits, Esc cancels.
- **Full editor controls.** A Format inspector panel lets you change a selected
  box's **font family** (303 families: base-14 + installed system fonts),
  **size**, **color**, and **bold/italic**. Picked fonts are embedded on save, so
  they render correctly anywhere (the engine never tries to reuse the document's
  locked-in subset fonts for a restyle).
- **Add / move / resize / delete boxes.** An Add Text tool drops a new text box
  anywhere on the page; selected boxes can be dragged to move, corner-dragged to
  resize, and deleted. Single click selects (outline + 8 handles); double-click
  edits text.
- **Box recognition.** Overlapping duplicate runs (some PDFs draw a value twice)
  merge into one editable box, so editing it erases every copy instead of leaving
  residue.
- **Original-font reproduction** (the core feature). On save, each edited run is
  removed by redaction and the new text is reinserted in a font resolved through
  three tiers:
  1. **Embedded** — the document's own embedded font buffer, reused when it
     covers the new glyphs. Bold/italic/regular faces of the same family on the
     same page are kept distinct.
  2. **System same-family** — when the embedded font is subsetted and lacks a
     needed glyph, a full system face of the same family (e.g. real Comic Sans
     MS) is used instead of tofu.
  3. **Base-14** — last resort only, for exotic CID/symbolic cases.
  The on-screen editor and the saved file go through the **same** resolver, so
  what you type is what lands in the PDF.
- **Undo / redo / dirty tracking**, full keyboard shortcuts, drag-and-drop, an
  empty state, and a status bar with a live font chip + fidelity dot showing
  which tier the current edit resolved to.
- **Safe write-back.** Redaction preserves underlying images and line art
  (non-destructive flags), preserves unedited neighbor runs whose ink overlaps
  the edit rect, and keeps rotated runs/pages oriented (morph + rotation matrix).
  The open document is never mutated; saving writes a fresh copy atomically.

Verified end to end by `tests/test_font_fidelity.py` and `tests/test_app.py`
across six fixtures embedding real Arial, Times New Roman, Georgia, and Comic
Sans MS (including a deliberately subsetted case). Both suites pass headless.

## Honest limitations

- **Embedded-tier edited text may not be selectable/searchable in the output.**
  When an edit reuses the original subsetted embedded font, glyphs are drawn by
  glyph id without a ToUnicode map, so the new run looks perfect but does not
  always copy-paste or search as text. System-font and base-14 edits remain
  fully extractable. (Future work: attach a ToUnicode CMap or re-embed a full
  face.)
- **No paragraph reflow.** Edits are per text run / line, placed at the original
  baseline. A much longer replacement can overrun the line; surrounding
  paragraphs do not re-wrap.
- **Born-digital only.** Scanned/image PDFs have no extractable text; OCR is not
  wired in.

## Setup

```bash
python3.13 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

## Run

```bash
./run.sh
# or
.venv/bin/python -m pdftexteditor.main
```

## Tests

The canonical suite list (every suite must exit 0; run each as
`QT_QPA_PLATFORM=offscreen .venv/bin/python tests/<name>.py`):

```
test_app             end-to-end harness over the real MainWindow stack
test_font_fidelity   font engine resolution tiers + coverage
test_editor_model    full-editor model + font layers (staged edits, parity)
test_editor          full editor driven through the real MainWindow UI
test_pages           structural page ops, workspace multi-doc, thumbnails
test_reflow          paragraph grouping + auto-reflow text model
test_richtext        per-selection bold/italic runs
test_redesign        continuous-scroll view + layout redesign
test_text_tools      find & replace + copy/paste with formatting
test_perf_foundation render cache, signatures, batched bake, menu skeleton
test_edit_ux         text-editing UX: caret, selection, B/I, dirty truth
```

Screenshots land in `tests/screenshots/` (app chrome, per-fixture font
before/after, page-view states).

## Architecture

- `pdftexteditor/font_engine.py` — `FontEngine` + `ResolvedFont`: locate a
  span's embedded font, check glyph coverage, resolve the best face across the
  three tiers, and load the matching family into Qt for the editor/preview.
- `pdftexteditor/document.py` — `PDFDocument`: render, extract styled `Span`s
  (each carrying its embedded font xref + baseline metrics), an authoritative
  undo/redo edit history, and a resolver-routed `save_as`.
- `pdftexteditor/ui/page_view.py` — the editable canvas: page sheet with shadow,
  per-span hotspots, the in-scene `InlineRunEditor`, and live previews that
  mirror the saved output.
- `pdftexteditor/ui/main_window.py` — toolbar, status bar, empty state,
  undo/redo + dirty wiring, save flow, shortcuts.
- `pdftexteditor/ui/theme.py` — design tokens + global stylesheet.
- `pdftexteditor/fonts.py` — base-14 mapping used only by the Tier-3 fallback.
- `docs/BUILD_SPEC.md` — the interface + algorithm spec the build was held to.
- `_probe/` — throwaway API-investigation scripts kept as reference.

## Roadmap

- ToUnicode/full re-embed so embedded-tier edits stay searchable.
- Paragraph reflow within a block.
- Scanned-PDF OCR; richer CID / right-to-left / vertical coverage.
- Package to a standalone `.app` with PyInstaller.
