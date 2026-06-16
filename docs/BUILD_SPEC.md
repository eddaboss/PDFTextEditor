# PDF Text Editor — BUILD SPEC

Authoritative build contract for the in-place PDF text editor. Independent
developers implement modules against the interfaces pinned here; the signatures
and return types are exact and load-bearing. Where this spec differs from the
current MVP, this spec wins.

**Verified environment (do not assume, these were checked in the project venv):**
PyMuPDF 1.27.2.3 / MuPDF 1.27.2, PySide6 6.11.1, Python 3.13, `fontTools`
installed. Always execute with `/Users/edward/Documents/GitHub/PDFTextEditor/.venv/bin/python`.
For any GUI check, prefix with `QT_QPA_PLATFORM=offscreen`.

---

## 0. The core problem this build solves

"True in-place editing that reproduces the ORIGINAL embedded font." Three
verified facts drive every interface below:

1. **Subset embedded fonts cannot be reinserted from their own bytes.** A
   subsetted Type0/Identity-H font (the norm for real PDFs, including every PDF
   PyMuPDF writes) extracts to a buffer where `fitz.Font(fontbuffer=buf)` loads
   but `valid_codepoints()` returns an **empty set** and `has_glyph(c)` returns
   `0` for every character. Inserting with it emits `.notdef` tofu. Verified on
   `tests/fixtures/subset_font.pdf`: `valid_codepoints()==0`, `has_glyph(ord('Z'))==0`.
   So "reproduce the original font" means: use the embedded buffer **only** when
   it is glyph-safe for the new text; otherwise resolve the original family to a
   FULL font file on disk via fontTools and insert with that. Base-14 is the
   last-resort floor, not the default.

2. **There is no font xref on a rawdict span.** The only bridge from a span to
   its embedded `xref` is matching the span's font name against
   `page.get_fonts(full=True)`. The span name is **pre-stripped of the subset
   tag** by MuPDF (`Comic Sans MS Regular`) while `get_fonts` keeps it
   (`HXRMZO+Comic Sans MS Regular`), and CID names differ in punctuation. Exact
   string match fails; **normalization is mandatory** (verified: normalized both
   sides → `comicsansmsregular`, exact match).

3. **Naive Qt family matching lies.** `QFont("ArialMT")` silently resolves to
   `.AppleSystemUIFont` with `QFontInfo.exactMatch()==False` — the same failure
   mode as base-14, hidden. The on-screen editor and the saved file must run
   through the same resolver, and the resolver must verify with `exactMatch()`.

The fidelity contract: **the live editor and the save writer call the same
`FontEngine.resolve(...)` and the same baseline math, so what you type is what
gets saved.**

---

## 1. Module map

| Path | Role | Status |
|---|---|---|
| `pdftexteditor/font_engine.py` | Font resolution: embedded → system → base-14, plus Qt loading | **NEW** |
| `pdftexteditor/document.py` | Model: spans, render, staged edits, undo/redo, save | rewrite save path + Span + undo API |
| `pdftexteditor/fonts.py` | `base14_code`, flag constants, `is_bold`/`is_italic` | **KEEP** as Tier-3 floor, imported by font_engine |
| `pdftexteditor/ui/page_view.py` | Canvas + in-scene inline editor | rewrite editor + preview + geometry |
| `pdftexteditor/ui/main_window.py` | Chrome, actions, undo/dirty wiring, status bar | rewrite |
| `pdftexteditor/ui/theme.py` | Design tokens + global QSS | **NEW** |
| `pdftexteditor/main.py` | Entry point | **UNCHANGED** |

Keep the proven spine: display doc never mutated; edits staged in memory keyed
by span identity; redaction-then-reinsert on save; render-to-pixmap on a
`QGraphicsScene` with transparent span hotspots.

---

## 2. `pdftexteditor/font_engine.py` — the font-resolution API

The single source of truth for "what font do we draw/insert with." Both the UI
preview/editor and `document.save_as` import and call this. **No other module
may construct a `QFont` from a raw PDF font name or call `base14_code` directly.**

### 2.1 Constants and data types

```python
from __future__ import annotations
from dataclasses import dataclass
import io, os, re
import fitz
from PySide6.QtGui import QFont, QFontInfo, QFontDatabase

# Resolution tiers (also the fidelity-dot colors in the UI).
TIER_EMBEDDED = 1   # original embedded buffer, glyph-safe -> green dot
TIER_SYSTEM   = 2   # full system font of the same family -> blue dot
TIER_BASE14   = 3   # base-14 substitute -> amber dot

@dataclass(frozen=True)
class ResolvedFont:
    """The outcome of resolving one edit. Consumed by BOTH the UI and save_as."""
    tier: int                 # TIER_EMBEDDED | TIER_SYSTEM | TIER_BASE14
    exact: bool               # True only when the literal target typeface was reproduced
    # --- PyMuPDF insertion side (what save_as passes to insert_text) ---
    pdf_fontname: str         # registration name to pass as insert_text(fontname=...)
    pdf_fontfile: str | None  # path to a font FILE on disk, or None
    pdf_fontbuffer: bytes | None  # font bytes, or None. Mutually informative with pdf_fontfile.
    base14_code: str | None   # set only when tier == TIER_BASE14 (e.g. "hebo")
    # --- Qt rendering side (what the editor/preview draw with) ---
    qt_family: str            # the family name to construct QFont(qt_family) with
    qt_bold: bool
    qt_italic: bool
    # --- provenance / UI chip ---
    source_name: str          # human label, e.g. "Comic Sans MS (embedded)" / "Arial" / "Helvetica (substitute)"

    def qfont(self, pixel_size: float) -> QFont:
        """Build the QFont the UI draws with, at a device-independent pixel size."""
        f = QFont(self.qt_family)
        f.setBold(self.qt_bold)
        f.setItalic(self.qt_italic)
        f.setPixelSizeF(pixel_size)   # caller passes span.size * zoom
        f.setStyleStrategy(QFont.PreferMatch)
        return f

@dataclass(frozen=True)
class _FaceRecord:
    """One face indexed from the system font tree."""
    path: str                 # absolute path to .ttf/.otf/.ttc
    face_index: int           # face within a .ttc (0 for single-face files)
    postscript: str           # name ID 6
    family: str               # name ID 16 (typographic) or 1
    subfamily: str            # name ID 17 or 2
    full_name: str            # name ID 4
    is_bold: bool
    is_italic: bool
```

### 2.2 The `FontEngine` class — exact signatures

```python
class FontEngine:
    """Resolves the best insertable/renderable font for an edited span.

    One instance per open PDFDocument; it caches per-page xref maps and the
    (process-wide) system font index. Constructed by PDFDocument and shared with
    the PageView so the live editor and the save writer resolve identically.
    """

    # Module-level singletons shared across instances (built once, expensive):
    _system_index: list[_FaceRecord] | None = None        # fontTools face index
    _qt_loaded_families: dict[bytes, str] = {}            # buffer hash -> Qt family

    def __init__(self, doc: fitz.Document) -> None: ...

    # --- (a) registering / extracting a span's original embedded font ------

    def embedded_xref(self, page_index: int, span_font: str, flags: int) -> int | None:
        """Bridge a rawdict span's font name to its embedded font xref.

        Matches `span_font` against page.get_fonts(full=True) using normalize()
        (strip subset tag, drop [space-_,], lowercase). On a normalized collision
        (two genuinely different faces), disambiguate with `flags` bits
        16/4/2 (bold/serif/italic) against weight/style tokens in the basefonts.
        Returns the xref, or None if the font is referenced-but-not-embedded
        (empty extract buffer) or unmatched. Result cached per page.
        """

    def extract_embedded(self, xref: int) -> tuple[str, str, str, bytes] | None:
        """Thin wrapper over doc.extract_font(xref) (the DEFAULT 4-tuple call,
        never info_only — that returns an empty buffer). Returns
        (basefont, ext, type, buffer), or None when buffer is empty (len 0)."""

    # --- (b) glyph-availability check --------------------------------------

    @staticmethod
    def font_covers(font: "fitz.Font", text: str) -> bool:
        """True iff `font` can render EVERY non-space char of `text`.

        Rule (verified): a subset font reports valid_codepoints()==0 and
        has_glyph()==0 for all chars. has_glyph returns a GID (int), 0 == absent.
            return (len(font.valid_codepoints()) > 0
                    and all(font.has_glyph(ord(c)) for c in text if c != ' '))
        """

    # --- (c) THE resolver: priority embedded -> system -> base-14 ----------

    def resolve(self, page_index: int, span_font: str, flags: int,
                new_text: str) -> ResolvedFont:
        """Resolve the best font for an edit, in strict priority order.

        Pure (no page mutation, no insertion). Deterministic: same inputs ->
        same ResolvedFont, so the live preview equals the saved output. The
        returned pdf_fontname is the name save_as registers and passes to
        insert_text; `tier` records which path won. See §3 for the algorithm.
        Results cached on (page_index, span_font, flags, frozenset(new_text)).
        """

    # --- (d) Qt loading helper for the UI ----------------------------------

    @classmethod
    def load_qt_family(cls, buffer: bytes) -> str | None:
        """Register font bytes with QFontDatabase so the editor can render the
        matching family on screen (used for Tier 1, the embedded buffer).

        Wraps QFontDatabase.addApplicationFontFromData(QByteArray(buffer)).
        Returns the first family name on success, None on failure. Cached by
        buffer hash so repeated edits to one span register the font once.
        (Verified: QFontDatabase.addApplicationFontFromData exists in 6.11.1.)
        """

    @classmethod
    def system_face_for(cls, family: str, bold: bool, italic: bool) -> str | None:
        """Resolve a base family + style to an absolute font FILE path on disk
        for Tier-2 embedding into the saved PDF (so the output is portable).
        Looks the matched _FaceRecord up in the system index; returns its path
        (writing the specific .ttc face bytes is save_as's job via face_bytes())."""

    # --- internal helpers (named so callers can rely on behavior) ----------

    @staticmethod
    def normalize(name: str) -> str:
        """re.sub(r'^[A-Z]{6}\\+','',name); re.sub(r'[\\s\\-_,]','',_).lower()."""

    @classmethod
    def _build_system_index(cls) -> list[_FaceRecord]:
        """Enumerate EVERY face of every .ttc/.ttf/.otf under
        /System/Library/Fonts, /System/Library/Fonts/Supplemental,
        /Library/Fonts, ~/Library/Fonts. Read name IDs 6,16/1,17/2,4 with
        fontTools (TTFont/TTCollection, lazy). ~788 faces. Built once, cached on
        the class. fitz.Font sees only TTC face 0, so fontTools is mandatory."""
```

### 2.3 Standalone module helper (used by save_as)

```python
def face_bytes(path: str, face_index: int) -> bytes:
    """Return the standalone bytes of one face from a .ttc/.ttf/.otf so a
    specific .ttc face (e.g. Helvetica-Bold = Helvetica.ttc#1) can be embedded.
    fitz.Font / insert_font take no face index, so we extract with fontTools:
        from fontTools.ttLib import TTFont, TTCollection
        if path.lower().endswith('.ttc'):
            tt = TTCollection(path, lazy=False).fonts[face_index]
        else:
            tt = TTFont(path, fontNumber=face_index)
        b = io.BytesIO(); tt.save(b); return b.getvalue()
    """
```

### 2.4 Alias table (Tier 2)

`resolve` normalizes the stripped base name and maps through this verified table
before constructing the candidate family. Qt mis-resolves the left column to the
system UI font without it; every candidate is checked with `QFontInfo.exactMatch()`.

| Embedded / PostScript base | Installed family (try in order) |
|---|---|
| `ArialMT`, `Arial-*` | `Arial` |
| `TimesNewRomanPSMT`, `Times-Roman`, `Times-*` | `Times New Roman`, then `Times` |
| `Helvetica`, `Helvetica-*` | `Helvetica` |
| `CourierNewPSMT`, `Courier`, `Courier-*` | `Courier New`, then `Courier` |
| `Calibri-*` | `Calibri`, else `Helvetica Neue` |
| `Georgia*` | `Georgia` |
| `ComicSansMS*`, `Comic Sans MS*` | `Comic Sans MS` |
| `CMR*`, `NimbusRom*`, `*Garamond*` | `Times New Roman` |
| mono flag / `*Mono*`, `Consol*`, `Menlo` | `Menlo` |
| serif flag, unknown | `Times New Roman` |
| (default) | `Helvetica` |

---

## 3. The font-fallback ALGORITHM (numbered, authoritative)

`FontEngine.resolve(page_index, span_font, flags, new_text)` executes exactly:

1. **Find the embedded xref.** `xref = self.embedded_xref(page_index, span_font, flags)`.
   - To match: `target = normalize(span_font)`; iterate `page.get_fonts(full=True)`
     7-tuples `(xref, ext, type, basefont, refname, encoding, stream_xref)`;
     candidates are those with `normalize(basefont) == target`.
   - If >1 candidate (real collision), score each by agreement of bold/italic/serif
     tokens in its basefont with `flags` bits 16/4/2; pick the best. If 0, `xref=None`.

2. **Try Tier 1 (embedded buffer).** If `xref is not None`:
   - `res = self.extract_embedded(xref)`; if `res` is None (empty buffer) skip to step 3.
   - `font = fitz.Font(fontbuffer=res[3])`.
   - If `font_covers(font, new_text)` is **True**:
       - `qt_family = load_qt_family(res[3])`. If that returns a family, build the
         ResolvedFont with `tier=TIER_EMBEDDED, exact=True, pdf_fontbuffer=res[3],
         pdf_fontname="EMB", pdf_fontfile=None, qt_family=qt_family,
         qt_bold=is_bold(span_font,flags), qt_italic=is_italic(span_font,flags),
         source_name=f"{strip(span_font)} (embedded)"`. **Return.**
       - If `load_qt_family` fails (Qt can't render the buffer) the SAVE is still
         glyph-safe via the buffer, but the on-screen font won't match — treat as
         a soft failure and fall through to Tier 2 so preview==save. (Embedded
         save fidelity without matching preview violates the contract.)
   - Else (subset / missing glyph): fall through. This is the common case.

3. **Try Tier 2 (full system font of the same family).**
   - Strip subset tag + style suffix from `span_font` to a base family; map through
     the §2.4 alias table to an ordered candidate family list.
   - `bold = is_bold(span_font, flags)`, `italic = is_italic(span_font, flags)`.
   - For each candidate family: `qf = QFont(family); qf.setBold(bold); qf.setItalic(italic)`.
     If `QFontInfo(qf).exactMatch()` is True:
       - `path = system_face_for(family, bold, italic)` (absolute file path; may be a
         `.ttc` — save_as extracts the right face via `face_bytes`).
       - Build ResolvedFont with `tier=TIER_SYSTEM, exact=True, qt_family=family,
         qt_bold=bold, qt_italic=italic, pdf_fontfile=path, pdf_fontname="SYS",
         pdf_fontbuffer=None, source_name=family`. **Return.**
   - If no candidate gives `exactMatch()`, fall through.

4. **Tier 3 (base-14 floor, glyph-safe always).**
   - `code = base14_code(span_font, flags)` (existing `fonts.py`).
   - Map the code to a Qt family for preview: helv-family→`Helvetica`,
     tiro-family→`Times New Roman`, cour-family→`Courier New`.
   - Build ResolvedFont with `tier=TIER_BASE14, exact=False, base14_code=code,
     pdf_fontname=code, pdf_fontfile=None, pdf_fontbuffer=None, qt_family=<mapped>,
     qt_bold=bold, qt_italic=italic, source_name=f"{<mapped>} (substitute)"`. **Return.**

**Never silently emit `.notdef`.** Tier 1 is gated by `font_covers`; if it would
tofu, the algorithm has already fallen through.

---

## 4. `pdftexteditor/document.py` — model layer

### 4.1 `Span` dataclass (capture the embedded font xref per span)

Keep the frozen dataclass and `key`. **Add `font_xref`** so the UI can pass the
xref straight to the resolver/Qt loader without re-deriving it, and **add
`ascender`/`descender`** (already present in rawdict) for accurate baseline math.

```python
@dataclass(frozen=True)
class Span:
    text: str
    bbox: tuple          # (x0,y0,x1,y1) PDF points
    origin: tuple        # baseline (x,y) PDF points
    size: float          # points
    color: tuple         # (r,g,b) 0..1
    font: str            # span font name as rawdict reports it (subset tag pre-stripped)
    flags: int           # PyMuPDF style bitfield
    font_xref: int | None  # NEW: embedded font xref, resolved at extraction via
                           # FontEngine.embedded_xref(); None if not embedded/unmatched
    ascender: float      # NEW: rawdict span ascender (font-relative, * size for px)
    descender: float     # NEW: rawdict span descender (negative)
    block_index: int
    line_index: int
    span_index: int

    @property
    def key(self) -> tuple:
        return (self.block_index, self.line_index, self.span_index)
```

`spans()` populates `font_xref` by calling `self.font_engine.embedded_xref(
page_index, span["font"], span["flags"])` during extraction. Color stays the
existing `sRGB_to_rgb` conversion.

### 4.2 `PDFDocument` — exact method signatures

```python
class PDFDocument:
    def __init__(self, path: str) -> None:
        # opens fitz doc, builds FontEngine(self.doc), inits _edits + _undo/_redo stacks.
        self.path: str               # current working path (mutated by save_as)
        self.font_engine: FontEngine

    def close(self) -> None: ...

    @property
    def page_count(self) -> int: ...

    # rendering
    def render(self, page_index: int, zoom: float) -> "fitz.Pixmap":
        """Page pixmap at `zoom` (matrix = Matrix(zoom, zoom), alpha=False).
        Unchanged from MVP. The UI multiplies zoom by DPR before calling for retina."""

    # extraction
    def spans(self, page_index: int) -> list[Span]:
        """rawdict -> list[Span], skipping empty/whitespace spans and non-text
        blocks. Each Span gets font_xref via self.font_engine.embedded_xref(...)."""

    # editing + undo/redo (model owns the edit history)
    def stage_edit(self, page_index: int, span: Span, new_text: str) -> None:
        """Record/replace the edit for this span and push it onto the undo stack
        (clears the redo stack). If new_text == span.text, removes any staged edit."""

    def undo(self) -> tuple[int, Span] | None:
        """Revert the most recent edit. Returns (page_index, span) of the affected
        span so the UI can repaint it, or None if nothing to undo."""

    def redo(self) -> tuple[int, Span] | None:
        """Re-apply the most recently undone edit. Returns (page_index, span) or None."""

    @property
    def can_undo(self) -> bool: ...
    @property
    def can_redo(self) -> bool: ...

    def staged_text(self, page_index: int, span: Span) -> str:
        """Current staged text for a span, or span.text if unedited."""

    @property
    def has_edits(self) -> bool: ...        # bool(self._edits)
    @property
    def edit_count(self) -> int: ...        # len(self._edits)

    # persistence
    def save_as(self, out_path: str) -> None:
        """Apply all staged edits to a FRESH copy loaded from self.path, write to
        out_path. Reinsertion routes through font_engine.resolve (NOT base14_code
        directly). See §4.3 for the exact loop. Does NOT mutate self.path; the
        caller (Save vs Save As) decides whether to repoint."""

    def set_path(self, path: str) -> None:
        """Repoint the working path after a successful Save As so subsequent saves
        target the new file. The window reloads the doc from `path` afterward."""
```

**Undo/redo placement note:** the model owns a simple `(_undo: list[Edit-delta],
_redo: list[...])` history so `can_undo/can_redo/undo/redo` are testable headless
without Qt. The UI's `QUndoStack` (§6) wraps these by calling `stage_edit`/`undo`/
`redo`; the two never both mutate history — the `QUndoCommand` delegates to the
model. Pick one owner: **the model is authoritative**, `QUndoCommand.redo/undo`
call `document.stage_edit` / `document.undo`. (If a developer prefers a single
stack, they MAY back `can_undo/can_redo` directly with the `QUndoStack`; the
signatures above are the contract either way.)

### 4.3 `save_as` reinsertion loop (exact, with the redaction gotcha)

```python
def save_as(self, out_path: str) -> None:
    import os, tempfile
    out = fitz.open(self.path)
    engine = FontEngine(out)                       # fresh engine bound to the copy
    by_page: dict[int, list[Edit]] = {}
    for (pi, _), edit in self._edits.items():
        by_page.setdefault(pi, []).append(edit)

    for pi, edits in by_page.items():
        page = out[pi]
        # 1) Remove originals FIRST. apply_redactions rebuilds the resource dict
        #    and DROPS any pre-registered font, so register AFTER this.
        for e in edits:
            page.add_redact_annot(fitz.Rect(e.span.bbox))
        page.apply_redactions()
        # 2) Reinsert AFTER redaction.
        for e in edits:
            span = e.span
            rf = engine.resolve(pi, span.font, span.flags, e.new_text)
            if rf.tier == TIER_EMBEDDED:
                page.insert_font(fontname=rf.pdf_fontname, fontbuffer=rf.pdf_fontbuffer)
                fontfile = None
            elif rf.tier == TIER_SYSTEM:
                # rf.pdf_fontfile may be a .ttc; extract the exact face to a buffer
                # and register it (insert_text has fontfile but no fontbuffer).
                rec = engine.system_record_for(rf.qt_family, rf.qt_bold, rf.qt_italic)
                page.insert_font(fontname=rf.pdf_fontname,
                                 fontbuffer=face_bytes(rec.path, rec.face_index))
                fontfile = None
            else:  # TIER_BASE14
                page.insert_font(fontname=rf.base14_code)
                fontfile = None
            page.insert_text(fitz.Point(span.origin), e.new_text,
                             fontsize=span.size, fontname=rf.pdf_fontname,
                             fontfile=fontfile, color=span.color)

    # 3) Atomic write so a mid-write crash never corrupts an existing target.
    fd, tmp = tempfile.mkstemp(suffix=".pdf", dir=os.path.dirname(out_path) or ".")
    os.close(fd)
    out.save(tmp, garbage=4, deflate=True)
    out.close()
    os.replace(tmp, out_path)
```

Notes: `insert_text` takes `fontname` + optional `fontfile`, **never
`fontbuffer`** (TypeError) — registered buffers go through `insert_font` first,
then `insert_text` references the registered name. `TextWriter` is an acceptable
alternative for any span (immune to the resource-dict drop); if used, append at
`span.origin` with the `fitz.Font` built from the resolved buffer.

---

## 5. `pdftexteditor/ui/page_view.py` — canvas + inline editor

### 5.1 Responsibilities

- Render the current page to a pixmap on a `QGraphicsScene`, drawn as a white
  sheet with a drop shadow on the `CANVAS_BG` gutter (40px margin, centered).
- Render at `zoom × devicePixelRatio`, then `QImage.setDevicePixelRatio(dpr)` so
  the sheet is crisp on retina (MVP renders at plain zoom — soft; fix this).
- Lay a transparent `SpanHotspot` (z=10, IBeamCursor) over every span. Hover =
  `ACCENT_HOVER` fill + 1px `ACCENT` baseline underline.
- On click, mount an **in-scene** `InlineRunEditor` (a `QGraphicsTextItem`
  subclass) at the run's baseline, in the resolved font/size/color — visually
  indistinguishable from set type. No yellow box, no blue border (those made the
  MVP feel like a popup). Because it lives in the scene it tracks pan/zoom with
  no manual repositioning (deletes the MVP's `scrollContentsBy` hack).
- Paint committed edits as a persistent preview (white cover + resolved-font
  text) plus an `EDITED_TINT` rect and `EDITED_UNDERLINE`, recomputed on reload.
- The preview text and the saved text come from the **same**
  `font_engine.resolve(...)` — the fidelity contract, testable headless.

### 5.2 Public methods and Qt signals

```python
class PageView(QGraphicsView):
    # Signals (MainWindow connects these for status/undo/dirty wiring)
    editCommitted   = Signal(int, object, str)   # (page_index, span, new_text)
    editCancelled   = Signal()
    editStarted     = Signal(object, object)     # (span, ResolvedFont) -> font chip
    editFinished    = Signal()                    # hide font chip
    pageChanged     = Signal(int)                 # new page_index (0-based)
    zoomChanged     = Signal(float)               # new zoom factor

    # Construction / document
    def set_document(self, document: PDFDocument) -> None: ...
    def clear_document(self) -> None: ...          # show empty state

    # Navigation / view
    def set_page(self, page_index: int) -> None: ...   # clamps; emits pageChanged
    @property
    def page_index(self) -> int: ...
    def set_zoom(self, zoom: float) -> None: ...        # clamps 0.25..6.0; emits zoomChanged
    def set_zoom_mode(self, mode: str) -> None: ...     # "fixed"|"fit_page"|"fit_width"
    @property
    def zoom(self) -> float: ...

    # Editing
    def begin_edit(self, hotspot: "SpanHotspot") -> None: ...
    def commit_edit(self) -> None: ...     # Enter / focus-out / ⌘Return
    def cancel_edit(self) -> None: ...     # Esc: restore original, stage nothing
    def is_edited(self, span: Span) -> bool: ...
    def repaint_span(self, span: Span) -> None: ...  # used by undo/redo to refresh one run

    # Rendering
    def reload(self) -> None: ...          # full re-render of the current page
```

### 5.3 Geometry math (single source, used by editor + preview + insert)

Let `z = self.zoom` (device-independent), `dpr = devicePixelRatio()`.

- **Pixmap render scale:** render the PyMuPDF pixmap at `z * dpr`; on the
  resulting `QImage` call `setDevicePixelRatio(dpr)`. Scene coordinates are then
  in device-independent units = PDF points × `z`.
- **PDF point (px,py) → scene (sx,sy):** `sx = px * z`, `sy = py * z`. (No DPR in
  scene coords; DPR lives only on the pixmap.)
- **Span bbox → scene rect:** `(x0*z, y0*z, (x1-x0)*z, (y1-y0)*z)`.
- **Baseline placement for the Qt editor/preview** (`QGraphicsTextItem` and
  `QGraphicsSimpleTextItem` position by block TOP-LEFT, not baseline):
  ```python
  font = resolved.qfont(span.size * z)          # device-independent px size
  ascent = QFontMetricsF(font).ascent()
  item.setPos(span.origin[0] * z, span.origin[1] * z - ascent)
  item.document().setDocumentMargin(0)          # QGraphicsTextItem only
  ```
  This reuses the MVP's `oy - metrics.ascent()` formula; keep it.
- **Caret hit-test on click:** map click to scene x; walk cumulative
  `QFontMetricsF.horizontalAdvance(prefix)` (or rawdict per-char bbox) to find the
  insertion index nearest the click x; set the caret there.
- **Baseline placement for the PyMuPDF insert (save side):**
  `insert_text(fitz.Point(span.origin), ...)` — `origin` IS the baseline point in
  PDF points; insert at it directly, no ascent offset (PyMuPDF positions text by
  baseline). This is why preview and save agree: Qt offsets up by ascent to draw
  the same glyphs whose baseline sits at `origin.y * z`.
- **Color:** `QColor.fromRgbF(*span.color)` for Qt; `span.color` tuple straight to
  `insert_text(color=...)`.

### 5.4 InlineRunEditor (primary) / fallback

- **Primary:** `InlineRunEditor(QGraphicsTextItem)` with
  `setTextInteractionFlags(Qt.TextEditorInteraction)`, font = `resolved.qfont(...)`,
  `setDefaultTextColor(QColor.fromRgbF(*span.color))`, document margin 0, no
  background/border. White cover rect (z=4) under it hides the rasterized
  original. Intercept `Return`/`Enter`→commit (no newline), `Esc`→cancel (block
  the focus-out commit when Esc caused it), `⌘A`→select run. Caret/selection
  themed via the view palette to `ACCENT` / `ACCENT_SELECTION`.
- **Fallback (only if caret theming is fiddly):** a frameless transparent
  `QLineEdit` in a `QGraphicsProxyWidget` (still in-scene), styled
  `background:transparent;border:none;padding:0;margin:0;
  selection-background-color:rgba(10,102,255,0.28)`, font/color set
  programmatically. Same contract; the MVP's `#fffbe6`/blue box is removed.

### 5.5 Z-order (scene)

shadow `-2` · pixmap `0` · edited tint `3` · white cover `4` · committed preview
text `6` · hotspots `10` · active inline editor `50`.

---

## 6. `pdftexteditor/ui/main_window.py` — chrome

### 6.1 Layout

`QMainWindow` (min 900×680, default 1180×920):
- `QToolBar "Main"` (48px, non-movable, top).
- central `CanvasContainer` (QWidget, `CANVAS_BG`, accepts `*.pdf` drops) holding
  the `PageView`; an empty-state overlay child shown while `document is None`.
- `QStatusBar` (28px).

### 6.2 Toolbar (left→right)

`[Open] [Save ▾]  |  [Undo] [Redo]  |  ⟨spacer⟩  | [‹] [page field / N] [›]  |  [–] [zoom% ▾] [+]`

- **Open** — QToolButton, `⌘O`.
- **Save** — split QToolButton (`MenuButtonPopup`): primary **Save** (`⌘S`),
  menu adds **Save As…** (`⇧⌘S`). Primary acts as Save As when never saved.
  Disabled with no document; primary additionally disabled when not dirty.
- **Undo** (`⌘Z`) / **Redo** (`⇧⌘Z`) — enabled from `QUndoStack`
  `canUndoChanged`/`canRedoChanged`; tooltips show the next command text.
- spacer (`QSizePolicy(Expanding, Preferred)`).
- **Page group:** `‹` (`⌥⌘←`/PageUp), editable `QLineEdit` (width 40, validator
  1..N, Enter jumps) + ` / N` label, `›` (`⌥⌘→`/PageDown).
- **Zoom group:** `–` (`⌘-`), `zoom% ▾` QToolButton with preset menu
  (Fit Page `⌘9`, Fit Width `⌘8`, 50/75/100 `⌘0`/125/150/200/400%), `+` (`⌘+`/`⌘=`).
  Step 1.25, clamp 0.25–6.0.

Icons: SF Symbols rendered to `QIcon` at 20px template/monochrome
(`folder`, `square.and.arrow.down`, `square.and.arrow.down.on.square`,
`arrow.uturn.backward`, `arrow.uturn.forward`, `chevron.left/right`,
`minus/plus.magnifyingglass`); fall back to bundled SVGs in `assets/icons/`.

### 6.3 QActions (all via `QAction` + `QKeySequence`, platform-correct)

`act_open(⌘O) act_save(⌘S) act_save_as(⇧⌘S) act_close(⌘W) act_undo(⌘Z)
act_redo(⇧⌘Z) act_prev(⌥⌘←) act_next(⌥⌘→) act_goto(⌃G) act_first(⌘↑)
act_last(⌘↓) act_zoom_in(⌘+/=) act_zoom_out(⌘-) act_actual_size(⌘0)
act_fit_page(⌘9) act_fit_width(⌘8)`. Use `QKeySequence.Open/Save/SaveAs/Undo/
Redo/ZoomIn/ZoomOut` where they exist.

### 6.4 Undo/redo + dirty wiring

- `QUndoStack` on the window. `EditRunCommand(document, page_index, span,
  old_text, new_text)`: `redo()`→`document.stage_edit(...)` + `view.repaint_span`;
  `undo()`→restage `old_text` (or clear if equal to original) + repaint; `text()`
  = `Edit '…' → '…'` (24-char truncation) for tooltips/menu.
- `PageView.editCommitted` → push an `EditRunCommand` onto the stack.
- `stack.canUndoChanged`/`canRedoChanged` → enable Undo/Redo actions.
- `stack.cleanChanged` → drives dirty: `dirty = not stack.isClean()` → status dot,
  `setWindowModified(dirty)`, `[*]` in title, Save-primary enablement.
- **Close/quit guard:** `closeEvent` / `⌘W` / quit while dirty → native 3-button
  `QMessageBox` (Save default, Cancel escape): "Do you want to save the changes
  you made to {name}?".
- On successful Save/Save As: `stack.setClean()`.

### 6.5 Save flow

- **Save** (`⌘S`): if a working path exists, `document.save_as(temp)`→`os.replace`
  in place (the atomic write lives in `save_as`); else behave as Save As.
- **Save As…** (`⇧⌘S`): `QFileDialog.getSaveFileName` default `"{base}-edited.pdf"`;
  on success `document.set_path(new)` and reload from it so spans/xrefs stay
  consistent. Both paths go through `font_engine.resolve` via `save_as`.

### 6.6 Status bar (28px, `CHROME_BG`, top hairline)

- **Left:** filename (`TEXT_PRIMARY` 13px medium) · dirty dot (8px `ACCENT`, hidden
  when clean) · `N edits` (`TEXT_SECONDARY`, pluralized; `0` → `No edits`).
- **Center (transient):** `showMessage(msg, 4000)` for save confirmations;
  errors styled `DANGER`.
- **Right:** font chip (visible only during an active edit — family + fidelity dot
  green/blue/amber from `ResolvedFont.tier`, fed by `PageView.editStarted`) · zoom%
  (mono, clickable → zoom menu).
- Window title: `{name} — PDF Text Editor`, `[*]` modified marker, `setWindowModified`.

### 6.7 Empty state

Centered overlay on `CANVAS_BG` while `document is None`: SF Symbol `doc.text`
(64px `#C7C7CC`), headline "Open a PDF to start editing" (17px semibold), subtext
"Click any line of text to edit it in place." (13px secondary), primary
**Open PDF…** button (36px, `ACCENT` fill, `⌘O`), "or drag a PDF here" (12px).
`CanvasContainer.setAcceptDrops(True)`; dashed `ACCENT` 2px border on drag-over;
fade out over 120ms on open.

---

## 7. `pdftexteditor/ui/theme.py` — tokens (new)

Module of constants + a `global_stylesheet()` returning the QSS. Colors:
`CANVAS_BG #E8E8EA`, `SHEET_WHITE #FFFFFF`, `SHEET_SHADOW rgba(0,0,0,.22)`,
`CHROME_BG #F5F5F7`, `CHROME_BORDER #D6D6D9`, `TEXT_PRIMARY #1D1D1F`,
`TEXT_SECONDARY #6E6E73`, `ACCENT #0A66FF`, `ACCENT_HOVER rgba(10,102,255,.10)`,
`ACCENT_SELECTION rgba(10,102,255,.28)`, `EDITED_TINT rgba(255,176,32,.16)`,
`EDITED_UNDERLINE #F0A000`, `DANGER #E5484D`, `TOOLBAR_ICON #3A3A3C`,
`TOOLBAR_ICON_DISABLED #B8B8BC`, `DIVIDER #DCDCDF`. UI font
`QFont(".AppleSystemUIFont")` 13px; mono `SF Mono` 12px. Toolbar 48px, status
28px, buttons 32×32 (20px icon, 6px radius), sheet margin 40px, shadow blur 24 /
y-offset 6, focus ring 2px `ACCENT`. Toolbar QSS exactly as in the UI report §3.

---

## 8. Developer checklist (what changes vs MVP)

1. **NEW `font_engine.py`** — `FontEngine` + `ResolvedFont` + `face_bytes` per §2/§3.
2. **NEW `theme.py`** — tokens + QSS per §7.
3. **`fonts.py`** — keep `base14_code`, flag constants, `is_bold`/`is_italic`;
   imported by `font_engine` as the Tier-3 floor. Do not delete.
4. **`document.py`** — add `font_xref`/`ascender`/`descender` to `Span`; add
   `font_engine`; populate `font_xref` in `spans()`; add `undo/redo/can_undo/
   can_redo/set_path`; rewrite `save_as` per §4.3 (resolver + atomic write).
5. **`page_view.py`** — delete the `QLineEdit` popup, `_place_editor`,
   `scrollContentsBy` hack, and the bare-`QFont` preview. Add in-scene
   `InlineRunEditor`, resolver-driven preview, hover accent + baseline underline,
   persistent edited tint, retina render (`zoom×dpr`), sheet shadow + centering,
   the signals in §5.2.
6. **`main_window.py`** — rebuild toolbar (split Save, page field, zoom menu, SF
   icons), `QUndoStack` + Undo/Redo + dirty/clean wiring + `setWindowModified` +
   close guard, Save vs Save As, drag-drop, empty state, full status bar + font chip.
7. **`main.py`** — unchanged.

---

## 9. Headless verification (the fidelity contract, testable)

Run every check with `QT_QPA_PLATFORM=offscreen .venv/bin/python`. Required assertions:

- **Resolver tiers** (no Qt needed): on `tests/fixtures/subset_font.pdf`,
  `resolve(0, "Comic Sans MS Regular", flags, "Zz")` returns `tier==TIER_SYSTEM`
  (subset → falls through Tier 1, system Comic Sans MS matches). On
  `tests/fixtures/body_paragraphs.pdf` a non-subset Times span where the buffer
  covers the new text returns `tier==TIER_EMBEDDED`.
- **Live == saved font:** `begin_edit` a span; assert the `InlineRunEditor`'s
  `QFont.family()` equals `resolve(...).qt_family`, and its scene `y` equals
  `span.origin[1]*zoom - QFontMetricsF(font).ascent()` (proves baseline + font).
- **Save round-trip:** stage an edit, `save_as`, reopen the output, assert the
  edited span renders real glyphs (not `.notdef`) and `get_fonts` shows the
  resolved face — i.e. `subset_font.pdf` edited with new characters produces
  readable text, not tofu (the central bug).
- **Undo/redo/dirty:** `stage_edit`→`can_undo`; `undo`→`staged_text==original`,
  `can_redo`; `edit_count`/`has_edits` track correctly.

Fixtures available: `body_paragraphs`, `multi_size`, `bold_italic`,
`colored_text`, `subset_font` (the fallback case), `mixed_families` under
`tests/fixtures/`.

---

## 10. Verified API facts (so nobody relitigates these)

- `page.get_fonts(full=True)` → **7-tuple** `(xref, ext, type, basefont, refname,
  encoding, stream_xref)`. Span `font` is **subset-tag-stripped**; basefont keeps
  the tag — match via `normalize()` (verified equal: `comicsansmsregular`).
- `doc.extract_font(xref)` default → **4-tuple** `(basefont, ext, type, bytes)`.
  `info_only=1` gives an **empty buffer** — never use it for bytes.
- Subset Type0 buffer: `fitz.Font(fontbuffer=buf)` loads, but
  `valid_codepoints()==0` and `has_glyph(c)==0` for all chars → **unusable for
  insertion** (verified on `subset_font.pdf`). `has_glyph` returns a **GID int**,
  0 == absent.
- `insert_text` params include `fontname` and `fontfile` but **no `fontbuffer`**
  (TypeError). Register buffers via `insert_font(...)` then reference by name.
  `insert_textbox` likewise has `fontfile`, no `fontbuffer`. `TextWriter` exists
  and carries a `fitz.Font` directly (immune to the resource-dict drop).
- `page.apply_redactions()` **drops fonts registered before it** → register
  AFTER redaction (or use `TextWriter`).
- `fitz.Font` reads only **face 0** of a `.ttc` (Helvetica.ttc has 6 faces) — use
  fontTools `face_bytes(path, index)` to embed a specific face.
- `QFontDatabase.addApplicationFontFromData` exists in PySide6 6.11.1 → Tier-1 Qt
  rendering of the embedded buffer.
- `QFont("ArialMT")` mis-resolves to the system UI font with
  `QFontInfo.exactMatch()==False` → every Tier-2 candidate must pass `exactMatch()`.
