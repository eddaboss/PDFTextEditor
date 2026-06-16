# PDF Text Editor — FULL EDITOR SPEC

Authoritative build contract for extending the in-place PDF text editor from a
**text-only** editor into a **full** one: select any text box and change its
font family / size / color / bold / italic; add new text boxes by clicking the
page; move / resize / delete boxes; all driven by a polished inspector panel +
toolbar.

This spec is the architecture layer that the model builder and the UI builder
implement against independently. Every signature and signal below is exact and
load-bearing. Where this spec differs from the current code, **this spec wins**;
where it is silent, `BUILD_SPEC.md` still governs.

**Verified environment (checked in the project venv, not assumed):** PyMuPDF
1.27.2.3 / MuPDF 1.27.2, PySide6 6.11.1, Python 3.13, `fontTools` installed.
Always run with `/Users/edward/Documents/GitHub/PDFTextEditor/.venv/bin/python`;
for any GUI check prefix `QT_QPA_PLATFORM=offscreen`.

**Verified API facts this spec relies on (run in venv):**
- `fitz.Font(...).text_length(text, fontsize=s)` returns the exact advance width
  in points for ANY resolved face — embedded buffer, system face bytes, or a
  base-14 code (`fitz.Font("helv")`). This is how `add_box`/`resize_box` compute
  width without inserting first. (helv "Hello 123" @12pt → 50.69pt.)
- `fitz.Font(...).ascender` / `.descender` are font-relative (× fontsize for
  points): helv = +1.075 / −0.299. Used to derive a new box's bbox from its
  baseline origin.
- `page.insert_text(...)` returns an `int` (line count), **not** geometry — the
  model must compute every inserted box's bbox itself from the resolved face's
  metrics. Do not rely on the return value for layout.

---

## 0. Non-negotiable invariants (do NOT regress)

These are tested (`tests/test_app.py`, `tests/test_font_fidelity.py`) and
specified in `BUILD_SPEC.md`. Every new feature below is layered so these stay
true. Any change that breaks one is a defect, not a tradeoff.

1. **Baked WYSIWYG.** The on-screen page comes from
   `document.render_with_edits()`, which applies edits through the SAME
   `_apply_page_edits` pipeline as `save_as`. What is on screen == the saved
   file. Never reintroduce a flat "cover + redrawn text" preview for COMMITTED
   edits. (The white cover under the LIVE inline editor stays — it hides the
   rasterized original only while typing, and only for the run being edited.)
2. **Overlap-merge box recognition.** `document.spans()` runs
   `_merge_overlapping()` to collapse OVERLAPPING duplicate runs into one box;
   `Span.redact_bboxes` holds all member bboxes; editing redacts all of them.
   Keep this for every edit type below (style/move/resize/delete all redact
   `span.redact_rects`, not a single bbox).
3. **3-tier font resolution.** Embedded original → same-family system → base-14.
   The doc's own embedded fonts are often Identity-H subsets that CANNOT be
   reused for new glyphs. **A user-PICKED font family is NEVER Tier 1** — it must
   resolve to a system / base-14 face (which IS embeddable) via the new
   `resolve_family(...)` so saved output renders correctly. (See §2.)
4. **Non-destructive redaction.** `apply_redactions(images=PDF_REDACT_IMAGE_NONE,
   graphics=PDF_REDACT_LINE_ART_NONE, text=PDF_REDACT_TEXT_REMOVE)` so colored
   table cells / borders / fills survive. Every edit type reuses
   `_apply_page_edits`, so this is automatic — do not add a second redaction path.
5. **Undo/redo + dirty tracking + atomic save.** The model owns an authoritative
   history; the Qt `QUndoStack` drives it; `save_as` stages to a temp file and
   `os.replace`s. All new mutations (style/move/resize/delete/add) flow through
   the SAME history primitive (§1.4) and the SAME save path.

**Privacy:** test only on `tests/fixtures/`. Never read anything under
`/Users/edward/Downloads`.

---

## 1. MODEL — `pdftexteditor/document.py`

The model gains: per-box **style overrides**, **geometry overrides** (move /
resize), a **deleted** flag, and **new boxes** added from scratch — all carried
on a single generalized edit record and a single generalized command history so
`QUndoStack` can drive every operation uniformly. The save/render pipeline
(`_apply_page_edits`) is extended to honor all of them.

### 1.0 Identity model (READ FIRST — it constrains everything)

Existing spans are identified by `Span.key = (block_index, line_index,
span_index)` per page, qualified globally by `Span.global_key = (page_index,
block_index, line_index, span_index)`. New boxes have no rawdict identity, so:

- **Existing-span edits** (text/style/move/resize/delete) are keyed by
  `(page_index, span.key)` exactly as text edits are today.
- **New boxes** get a synthetic, monotonically increasing `box_id` (int) and are
  keyed by `(page_index, "new", box_id)`. They live in a separate map so they
  never collide with rawdict keys and so `spans()` can surface them as
  first-class editable boxes (§1.6).

The `_edits` map (today `dict[(page_index, span.key) -> Edit]`) is **replaced**
by a generalized `dict[edit_key -> Edit]` where `Edit` now carries text + style +
geometry + deleted (§1.2). A NewBox's edits are carried on the NewBox record
itself (§1.3); the same `Edit` semantics apply to its overridable fields.

### 1.1 `StyleOverride` dataclass (NEW)

```python
@dataclass(frozen=True)
class StyleOverride:
    """A partial restyle of one box. Every field is None == 'unchanged from the
    box's original style'; a non-None field overrides it. Carried on Edit and
    applied at render/save time. A user-PICKED family resolves through
    FontEngine.resolve_family (NOT the embedded tier) so it is always
    embeddable; see §2."""
    font_family: str | None = None     # a family from FontEngine.available_families()
    size: float | None = None          # points, > 0
    color: tuple | None = None         # (r, g, b), each 0..1
    bold: bool | None = None
    italic: bool | None = None

    @property
    def is_empty(self) -> bool:
        return (self.font_family is None and self.size is None
                and self.color is None and self.bold is None
                and self.italic is None)

    def merged_with(self, other: "StyleOverride") -> "StyleOverride":
        """Field-wise overlay: other's non-None fields win over self's. Used to
        accumulate successive inspector changes into one override on a box."""
```

`merged_with` lets the inspector apply size, then color, then bold as three
separate undo steps without each clobbering the others — each step merges its
one non-None field onto the box's current override.

### 1.2 `Edit` dataclass (EXTENDED — replaces today's text-only `Edit`)

```python
@dataclass
class Edit:
    """All staged changes to ONE existing span (box). Any subset may be set;
    None/empty == unchanged. Applied in §1.7 order at render/save time."""
    span: Span
    new_text: str | None = None         # None == text unchanged
    style: StyleOverride = StyleOverride()   # field-wise style overrides
    move: tuple | None = None           # (dx, dy) in PDF points, cumulative
    scale: float = 1.0                  # size/geometry multiplier (resize), cumulative
    deleted: bool = False               # True == box removed (redact, no reinsert)

    @property
    def is_noop(self) -> bool:
        """True when this Edit changes nothing vs the span's original — used to
        drop it from _edits so a box reverted to source is not written out."""
```

`new_text` becomes `Optional` (was a bare `str`). Existing call sites that read
`edit.new_text` must treat `None` as "use `span.text`". Provide a helper:

```python
def effective_text(self, span: Span) -> str:
    return self.span.text if self.new_text is None else self.new_text
```

### 1.3 `NewBox` dataclass (NEW)

```python
@dataclass
class NewBox:
    """A text box added from scratch (not from any rawdict span). Self-describing:
    it owns its style directly (no original to override). Surfaced by spans() as
    an editable box so the UI treats it uniformly. Geometry is recomputed from
    the resolved face metrics whenever text/size/family/style changes (§1.6)."""
    page_index: int
    box_id: int                         # synthetic, unique within the document
    origin: tuple                       # baseline (x, y) in PDF points
    bbox: tuple                         # (x0,y0,x1,y1), derived from origin+metrics
    text: str
    font_family: str                    # a family from available_families()
    size: float                         # points
    color: tuple                        # (r, g, b) 0..1
    bold: bool
    italic: bool
    deleted: bool = False               # a deleted NewBox is dropped entirely

    @property
    def edit_key(self) -> tuple:
        return (self.page_index, "new", self.box_id)
```

A `NewBox` is never resolved through Tier 1 (no embedded buffer); its
`font_family` is resolved via `FontEngine.resolve_family(...)` (§2) at
render/save time, which is always embeddable.

### 1.4 Generalized command history (replaces `_Delta`)

Today `_Delta` only carries text. **Replace** it with a record that captures the
FULL before/after edit-state of ONE box, so undo/redo is uniform across text,
style, move, resize, delete, and add. The model stays the single owner of
history; the Qt `QUndoStack` (UI side) holds thin commands that call
`apply_command` / and the model replays.

```python
@dataclass
class _Command:
    """One undoable mutation of one box. before/after are the COMPLETE staged
    state of that box's editable fields, so replaying forward installs `after`
    and backward installs `before`. Works identically for an existing span
    (keyed by edit_key) and a NewBox (keyed by its edit_key)."""
    edit_key: tuple                     # (page_index, span.key) | (page_index,"new",id)
    kind: str                           # "text"|"style"|"move"|"resize"|"delete"|"add"
    before: "_BoxState"                 # full staged state before
    after: "_BoxState"                  # full staged state after
    label: str                          # human label for the QUndoStack
```

`_BoxState` is the snapshot the history stores/restores. For an existing span it
mirrors `Edit` fields; for a NewBox it mirrors the mutable `NewBox` fields plus
`exists` (so "add" toggles existence and "delete" of a NewBox flips it back).

```python
@dataclass
class _BoxState:
    # existing-span overrides (None/defaults == unchanged):
    new_text: str | None = None
    style: StyleOverride = StyleOverride()
    move: tuple | None = None
    scale: float = 1.0
    deleted: bool = False
    # new-box payload (only used when the key is a "new" key):
    newbox: "NewBox | None" = None
    exists: bool = True
```

The model keeps `self._undo: list[_Command]` and `self._redo: list[_Command]`
(unchanged names, generalized payload). `undo()`/`redo()` pop/replay and return
**`(page_index, box_ref)`** so the UI can repaint exactly the affected box, where
`box_ref` is a `Span` (existing) or `NewBox` (new). This generalizes today's
`(page_index, span)` return without breaking the existing-span caller.

### 1.5 `PDFDocument` — exact new/changed method signatures

All staging methods follow one rule: **capture before-state, mutate staged
state, push a `_Command`, clear redo, set dirty** — identical bookkeeping to
today's `stage_edit`. Each returns the affected box ref so the UI repaints it.

```python
# --- text (UNCHANGED signature; now stages onto the generalized Edit) ---
def stage_edit(self, page_index: int, span: Span, new_text: str) -> None: ...

# --- style (NEW) ---
def set_style(self, page: int, box, *, font_family: str | None = None,
              size: float | None = None, color: tuple | None = None,
              bold: bool | None = None, italic: bool | None = None) -> None:
    """Merge these non-None overrides onto `box` (a Span or NewBox) and push a
    "style" command. Each call is ONE undo step. None args leave that attribute
    unchanged. For a NewBox, writes the fields directly; for a Span, accumulates
    a StyleOverride on its Edit. No-op (no command) when nothing actually
    changes."""

# --- move (NEW) ---
def move_box(self, page: int, box, dx: float, dy: float) -> None:
    """Translate `box` by (dx, dy) PDF points (cumulative with any prior move).
    For a Span, accumulates Edit.move; for a NewBox, shifts origin+bbox. One
    drag = one command (the UI coalesces intermediate drag deltas into a single
    command at mouse-release; see §3.6). dx/dy are in PDF point space, already
    de-zoomed and de-rotated by the view."""

# --- resize (NEW) ---
def resize_box(self, page: int, box, scale: float, *,
               anchor: tuple | None = None) -> None:
    """Multiply the box's effective font size by `scale` (cumulative) and scale
    its geometry about `anchor` (default: the box's top-left in PDF points).
    Font size scales proportionally with the box, so resizing a corner handle
    grows/shrinks the type. One handle-drag = one command (coalesced at release).
    `scale` is the ABSOLUTE multiplier relative to the box's ORIGINAL size at the
    start of the drag, not an incremental factor (the UI tracks the start size)."""

# --- delete (NEW) ---
def delete_box(self, page: int, box) -> None:
    """Mark `box` deleted: redact its bbox(es), reinsert nothing. For a NewBox,
    flips its `deleted`/`exists` off (it simply stops being emitted). One
    command; undo restores it."""

# --- add (NEW) ---
def add_box(self, page: int, origin: tuple, text: str, family: str,
            size: float, color: tuple, bold: bool, italic: bool) -> NewBox:
    """Create a NewBox at baseline `origin` (PDF points) with the given style,
    compute its bbox from FontEngine.resolve_family(family,bold,italic,text)
    metrics (§1.6), register it in the new-box map, push an "add" command, and
    return it. The caller (view) immediately selects it and enters text-edit
    mode. An add with empty text that is never typed into is dropped on commit
    (see §3.5) so stray clicks don't litter the document."""
```

`box` is duck-typed: a `Span` or a `NewBox`. Internally each method branches on
`isinstance(box, NewBox)`. A single private helper `self._command(...)` does the
before/after snapshot + push so every mutator is three lines.

### 1.6 New-box geometry (deterministic, from resolved metrics)

A NewBox's bbox is derived, never measured from a draw:

```
rf   = font_engine.resolve_family(family, bold, italic, text)
fobj = fitz.Font of rf (buffer for system, code for base14)   # §2.3
w    = fobj.text_length(text or " ", fontsize=size)           # advance width, pts
asc  =  fobj.ascender  * size                                  # +above baseline
desc = -fobj.descender * size                                  # +below baseline (descender is negative)
x0, y = origin
bbox = (x0, y - asc, x0 + w, y - desc + (0 if desc>=0 else 0))
# i.e. bbox = (x0, y - asc, x0 + w, y + desc_abs); top is above the baseline,
# bottom is below it. redact_rects for a NewBox = (bbox,).
```

Recompute bbox on every text/size/family/bold/italic change so the selection
outline and hit-test track the live content. The same `_insert_run` that draws
existing edits draws a NewBox (horizontal `dir=(1,0)`; new boxes are always
horizontal in v1).

### 1.7 Apply order in `_apply_page_edits` (extended, single pipeline)

`_apply_page_edits` gains NewBoxes and the new override fields. Order per page:

1. **Collect redaction rects.** For every existing-span Edit that is `deleted` OR
   has any text/style/move/resize change, add `span.redact_rects` (every member
   bbox of an overlap-merged box). Deleted-but-not-otherwise-changed boxes
   redact only. (NewBoxes contribute no redaction — there is no original ink.)
2. **Capture overlapping neighbors** to redraw (unchanged `_overlapping_neighbors`
   logic), so a restyle/move that redacts a rect does not clip an unedited
   neighbor.
3. **`apply_redactions`** with the non-destructive flags (invariant §0.4).
4. **Reinsert**, per box, at its EFFECTIVE geometry/style:
   - **Existing span, not deleted:** resolve the font as follows —
     - if the Edit has a `style.font_family` override → `resolve_family(family,
       effective_bold, effective_italic, text)` (user picked it; never Tier 1);
     - else → `resolve(page, span.font, span.flags, text)` (the 3-tier path,
       reproducing the original embedded/system face) exactly as today.
     Effective size = `span.size * scale * (style.size/span.size if size override
     else 1)` → just `style.size or span.size`, then `* scale`. Effective color =
     `style.color or span.color`. Effective origin = `span.origin + move`
     (move applied in text space, then through `dir` as today).
   - **Existing span, deleted:** skip reinsertion (already redacted).
   - **NewBox, not deleted:** resolve via `resolve_family(...)`, insert at its
     `origin` with its own size/color/bold/italic.
   - Neighbors: redraw unchanged (as today).
5. Font registration is per concrete face via `_register_resolved` (unchanged) —
   it already keys by face bytes, so a user-picked system family registers and
   embeds correctly.

`render_with_edits` calls the same `_apply_page_edits`, so style/move/resize/
delete/add all appear on screen exactly as they will save — the WYSIWYG
invariant holds for ALL edit types, not just text. `exclude_span` (the run under
the live editor) keeps working: when a box is in TEXT-edit mode it is redacted
but not reinserted so the editor floats over a clean background.

### 1.8 `spans()` surfaces NewBoxes (so the UI lists every box uniformly)

`spans(page_index)` returns existing spans (overlap-merged, as today) PLUS a
view of the page's non-deleted NewBoxes adapted to the box interface the UI needs
(`bbox`, `origin`, `size`, `color`, `redact_rects`, `is_horizontal`, an identity
key). To avoid forcing NewBox to subclass Span, the UI's hit-test/selection
works against a **`Box` protocol** (a structural type both `Span` and `NewBox`
satisfy): attributes `page_index`, `bbox`, `origin`, `size`, `color`,
`redact_rects`, `is_horizontal`, `rotation_degrees`, and an `identity` key
(`Span.global_key` for spans, `NewBox.edit_key` for new boxes). Document this
protocol in `document.py`; both dataclasses already (or will) expose these.

> Implementation note: keep `spans()` returning the existing-span list and add a
> sibling `new_boxes(page_index) -> list[NewBox]`; the view composes both into
> its box list. Either is acceptable as long as the view can enumerate every
> editable box on a page. Pin whichever you choose in code docs.

### 1.9 Persistence / state surface (extended)

- `has_edits` / `edit_count`: count existing-span Edits that are non-noop PLUS
  non-deleted NewBoxes. `dirty` / `mark_clean` unchanged in meaning.
- `staged_text(page, box)`: returns the box's effective text (existing or new).
- `effective_style(page, box) -> dict`: NEW — returns the box's CURRENT resolved
  style for the inspector to read: `{"font_family": str, "size": float,
  "color": (r,g,b), "bold": bool, "italic": bool}`. For an existing span with no
  override this reports the ORIGINAL family (a display name from the resolver,
  e.g. `resolve(...).source_name`-stripped or the matched system family) so the
  inspector shows the truth, not a blank.
- `save_as` / `render_with_edits`: unchanged signatures; both now honor NewBoxes
  + overrides via the shared `_apply_page_edits`.

---

## 2. FONT ENGINE — `pdftexteditor/font_engine.py`

Two additions. The existing 3-tier `resolve(...)` (for reproducing an EXISTING
span's original face) is untouched and still governs unstyled text edits.

### 2.1 `available_families() -> list[str]` (NEW, classmethod)

```python
@classmethod
def available_families(cls) -> list[str]:
    """Sorted, de-duplicated list of family names for the inspector's picker.
    Union of: the base-14 display families (Helvetica, Times New Roman, Courier
    New) and every distinct `family` in the scanned system index
    (_build_system_index()). Excludes hidden/.dot families (names starting with
    '.'). Cached on the class after first build. This is the menu of families a
    user may PICK; every one of them resolves to an embeddable face via
    resolve_family (§2.2)."""
```

Build once, cache on the class beside `_system_index`. Filter out families whose
name starts with `.` (e.g. `.AppleSystemUIFont`) and obvious non-text faces is
optional polish, not required.

### 2.2 `resolve_family(family, bold, italic, text) -> ResolvedFont` (NEW)

```python
def resolve_family(self, family: str, bold: bool, italic: bool,
                   text: str) -> ResolvedFont:
    """Resolve a USER-PICKED family to an embeddable face. NEVER returns
    TIER_EMBEDDED (there is no original embedded buffer to honor — the user
    chose a fresh family). Algorithm:
      1. TIER_SYSTEM: look up the family + style in the system index
         (system_record_for). If a concrete face exists AND QFontInfo(QFont(
         family){bold,italic}).exactMatch() (so the on-screen QFont matches what
         we embed), return a TIER_SYSTEM ResolvedFont (pdf path = the face path,
         qt_family = family, source_name = family).
      2. TIER_BASE14: otherwise map the family to the nearest base-14 code via a
         serif/mono/sans classification of the family name (reuse fonts._family
         heuristics on the family string) + the bold/italic bits, and return a
         TIER_BASE14 ResolvedFont (always glyph-safe).
    Pure + deterministic + cached on (family, bold, italic, glyph_key) just like
    resolve(), so the live editor and save_as agree. The Qt side
    (rf.qfont(pixel_size)) is identical to resolve()'s, so the inspector preview
    and the on-screen box match the saved output."""
```

Both tiers it can return (SYSTEM, BASE14) are embeddable, satisfying invariant
§0.3. The save path's `_register_resolved` already handles SYSTEM (extract face
bytes, embed) and BASE14 (built-in code) — no save-path change needed.

### 2.3 `ResolvedFont` → `fitz.Font` helper (factor out the tests' duplication)

Both test files inline `resolved_fitz_font(engine, rf)`. Promote it to a method
so the model can build the metrics font for `add_box`/`resize_box` (§1.6) without
re-implementing it:

```python
def fitz_font_for(self, rf: ResolvedFont) -> "fitz.Font":
    """The exact fitz.Font the save path draws with for `rf`:
      EMBEDDED -> fitz.Font(fontbuffer=rf.pdf_fontbuffer)
      SYSTEM   -> fitz.Font(fontbuffer=face_bytes(rec.path, rec.face_index))
      BASE14   -> fitz.Font(rf.base14_code)"""
```

This is the SAME object the tests build; reusing it keeps metrics ==
saved-glyph metrics.

---

## 3. UI — `pdftexteditor/ui/page_view.py` (PageView + selection model)

PageView gains a **selection model** layered ABOVE the existing hotspot/inline-
editor machinery. Selection and text-editing are distinct modes (Acrobat-style):
selecting shows an outline + handles and populates the inspector; double-click /
Enter drops into the existing `InlineRunEditor`. The text-edit path
(`begin_edit`/`commit_edit`/`cancel_edit`/`InlineRunEditor`) is **unchanged in
behavior**; selection wraps around it.

### 3.1 New scene items

- **`SelectionOverlay(QGraphicsItem)`** — draws the selection outline (1.5px
  cosmetic accent rect just outside the box's scene rect) and **8 resize handles**
  (NW, N, NE, E, SE, S, SW, W) as small filled accent squares. Z-value
  `Z_SELECTION = 40` (below `Z_EDITOR = 50`, above `Z_HOTSPOT = 10`). It is
  rebuilt/repositioned whenever the selection or zoom changes. Handles are
  `~9px` device-independent squares centered on the box's scene-rect corners/
  edge midpoints. The overlay is rotation-aware: it is built from the box's
  scene rect via the existing `_span_scene_rect`-style transform (so a /Rotate
  page draws the outline where the glyphs are).
- The existing `SpanHotspot` stays for hover affordance + click target. Add a
  `Z_HANDLE = 41` for individual handle items if you implement handles as
  separate `QGraphicsRectItem`s (recommended, so each handle gets its own cursor
  + drag).

### 3.2 Modes (one enum, mutually exclusive)

```
SELECT      default; clicks select boxes, drag moves, handle-drag resizes.
ADD_TEXT    "Add Text" tool armed; the NEXT empty-canvas click creates a box.
TEXT_EDIT   the InlineRunEditor is mounted (existing behavior).
```

`_mode` is private; expose `enter_add_text_mode()` / `exit_add_text_mode()` and
`current_mode()` (returns the enum/string). Entering TEXT_EDIT is implicit via
`begin_edit`. Selection persists across SELECT⇄TEXT_EDIT (committing text keeps
the box selected). Arming ADD_TEXT sets the canvas cursor to a crosshair.

### 3.3 Interaction rules (EXACT — this is the contract)

| Gesture | Mode | Result |
|---|---|---|
| Click empty canvas | SELECT | **Deselect** (clear selection, hide overlay, emit `selectionChanged(None)`). |
| Click empty canvas | ADD_TEXT | **Add** a box at the click point via `document.add_box(...)` using the inspector's current font/size/color/bold/italic; auto-select it; immediately `begin_edit` it (TEXT_EDIT); return to SELECT after commit. |
| Single click a box | SELECT | **Select** it: show outline+handles, emit `selectionChanged(box)`. No text edit. |
| Double-click a box | SELECT | **Edit text**: `begin_edit` the box (existing InlineRunEditor), placing the caret at the click x. |
| `Enter` while a box is selected (not text-editing) | SELECT | **Edit text** (same as double-click; whole-run selected). |
| Drag a selected box's BODY | SELECT | **Move**: live-translate the box; on release stage ONE `move_box` command (§3.6). |
| Drag a selected box's corner/edge HANDLE | SELECT | **Resize**: live-scale the box + its font size; on release stage ONE `resize_box` command. Corner handles scale proportionally; edge handles scale one axis (v1 may treat all handles as proportional-from-opposite-corner — pin which in code). |
| `Delete` or `Backspace` while a box is selected (not text-editing) | SELECT | **Delete** the box: `delete_box(...)`, clear selection. |
| `Esc` while selected (not text-editing) | SELECT | Deselect. |
| Single click a DIFFERENT box | SELECT | Reselect the new box (commit/flush any open editor first). |

Text-editing keys (Return commits, Esc cancels, Cmd/Ctrl+A selects-all)
**inside** the InlineRunEditor are unchanged and take precedence while
TEXT_EDIT is active — Delete/Backspace there edit text, they do NOT delete the
box. The box-delete shortcut only fires in SELECT mode with no editor mounted.

Mouse-press routing: a press first hit-tests handles (topmost `Z_HANDLE`), then
the selected box body, then any box hotspot, then empty canvas. Implement via
`mousePressEvent`/`mouseMoveEvent`/`mouseReleaseEvent` on the `PageView` (so the
view owns drag state) OR on the items — pin the choice in code; the view-level
handler is recommended because move/resize need a single drag-state owner.

### 3.4 PageView public API additions (EXACT)

```python
# --- signals (MainWindow connects these) ---
selectionChanged = Signal(object)     # the selected Box (Span|NewBox) or None
boxAdded         = Signal(object)     # the new NewBox (for status/undo wiring)
styleApplied     = Signal(object, dict)  # (box, applied-overrides) after apply_style
geometryChanged  = Signal(object)     # (box) after a move/resize commits
boxDeleted       = Signal(object)     # the deleted Box
# existing signals unchanged: editCommitted, editStarted, editFinished,
# pageChanged, zoomChanged, editCancelled.

# --- selection ---
def current_selection(self) -> object | None:
    """The currently selected Box (Span|NewBox), or None."""
def select_box(self, box) -> None:
    """Programmatically select a box (used after add / after a commit)."""
def clear_selection(self) -> None: ...

# --- style (inspector -> view) ---
def apply_style(self, overrides: dict) -> None:
    """Apply inspector overrides to the CURRENT selection LIVE and stage them
    through undo/redo. `overrides` keys (any subset): 'font_family' (str),
    'size' (float), 'color' ((r,g,b) 0..1), 'bold' (bool), 'italic' (bool). Maps
    to document.set_style(...); each call is ONE undo command (the MainWindow
    wraps it — see §4). No-op when nothing is selected. Repaints the box and the
    selection overlay. Emits styleApplied(box, overrides)."""

# --- add-text mode ---
def enter_add_text_mode(self) -> None:
    """Arm ADD_TEXT: crosshair cursor; next empty-canvas click adds a box."""
def exit_add_text_mode(self) -> None: ...
def current_mode(self) -> str: ...    # "select" | "add_text" | "text_edit"

# --- delete ---
def delete_selected(self) -> None:
    """Delete the current selection via document.delete_box(...) (one command),
    then clear selection. No-op if nothing selected or a text editor is open."""
```

`apply_style`, `delete_selected`, `move`, `resize`, and add all stage through the
MainWindow's `QUndoStack` (the view emits an intent signal; the window pushes the
command), MIRRORING how text edits already work (`editCommitted` → window pushes
`EditRunCommand`). Concretely:

- The view calls `document.set_style/move_box/resize_box/delete_box/add_box`
  ONLY via a command the window owns, OR emits a signal the window turns into a
  command. **Pick the signal route** (consistent with `editCommitted`) so the
  model is mutated in exactly one place — the `QUndoCommand.redo`. Define one
  generic intent signal:

```python
boxCommandRequested = Signal(str, object, dict)
# (kind, box, params) -> window builds a BoxCommand and pushes it.
#   kind="style"  params={"overrides": {...}}
#   kind="move"   params={"dx":..,"dy":..}
#   kind="resize" params={"scale":..,"anchor":(x,y)}
#   kind="delete" params={}
#   kind="add"    params={"origin":..,"text":..,"family":..,"size":..,
#                         "color":..,"bold":..,"italic":..}
```

This keeps §0.5 true: every mutation funnels through one `QUndoCommand` type.

### 3.5 Add-text flow (exact)

1. Toolbar "Add Text" toggles ADD_TEXT (`enter_add_text_mode`); cursor →
   crosshair.
2. Empty-canvas click at scene point `p` → convert to PDF-point baseline origin
   (de-zoom, de-offset, de-rotate via the existing helpers, inverted). The
   baseline sits where the user clicked minus the resolved ascent so the first
   glyph's TOP is near the cursor (or place baseline at the click y — pin one;
   "baseline at click, box grows upward" is simplest and matches `_insert_run`).
3. Read the inspector's current values (family/size/color/bold/italic) →
   `boxCommandRequested("add", None, {...})` → window pushes an `add` command →
   `document.add_box(...)` returns the NewBox.
4. The view selects it and `begin_edit`s it (TEXT_EDIT) with an empty/placeholder
   string. The mode returns to SELECT after the text commit.
5. **Empty-add cleanup:** if the user commits the new box with empty/whitespace
   text (or cancels before typing), the `add` command is rolled back (the view
   calls `undo` on the just-pushed add, or the window detects empty text on the
   subsequent `editCommitted` and merges add+empty into a no-op). Pin the
   mechanism; the REQUIREMENT is: a stray click that adds nothing leaves no box.

### 3.6 Move / resize drag mechanics (exact)

- On press over the selected box body (SELECT mode): record `drag_start_scene`,
  the box's start origin/bbox, set `_dragging="move"`. On move: translate the
  selection overlay + a live preview by the cursor delta (do NOT restage every
  pixel). On release: compute `(dx, dy)` in PDF points (`delta_scene / zoom`,
  inverse-rotated) and emit ONE `boxCommandRequested("move", box, {dx,dy})`.
- On press over a handle: record the handle id, the box's start scene rect and
  start size, set `_dragging="resize"`. On move: live-scale the overlay. On
  release: compute the ABSOLUTE `scale` = newSize/startSize (proportional from
  the opposite corner for corner handles) and emit ONE
  `boxCommandRequested("resize", box, {scale, anchor})`. Font size scales with
  the box (the resolved size for reinsertion = startSize × scale).
- Live preview during a drag may be a cheap transformed copy of the overlay /
  the rendered box; the COMMITTED result re-renders through `render_with_edits`
  (baked WYSIWYG). Never stage intermediate drag frames as undo steps — one
  gesture = one command.

### 3.7 Selection ⇄ existing machinery

- `reload()` rebuilds hotspots AND re-establishes the selection overlay for the
  still-selected box (match by identity key across the fresh span list, since a
  new `Span` object is created each reload). If the selected box no longer
  exists (deleted), clear selection.
- `begin_edit` is now reachable from double-click / Enter on a selected box and
  from the add flow; its body is unchanged. While TEXT_EDIT is active the
  selection overlay is hidden (the editor IS the visual focus); it returns on
  commit/cancel.
- `is_edited` / hover washes unchanged; an unedited-but-SELECTED box shows the
  selection outline, not the amber edited tint.

---

## 4. UI — `pdftexteditor/ui/inspector.py` (NEW MODULE)

A docked panel that reads the selected box and writes style changes back through
the window to `view.apply_style(...)`. Pure chrome; it never touches the model
directly.

### 4.1 Widget tree + object names (EXACT, for tests + QSS)

```python
class Inspector(QWidget):           # objectName="Inspector"
    # emitted whenever the user changes a control (debounced for the color/size
    # spin so dragging a spinner is one logical change per settle):
    styleEdited = Signal(dict)      # {field: value} for the ONE field changed

    # public:
    def set_target(self, box, style: dict | None) -> None:
        """Populate every control from `style` (the dict from
        document.effective_style(...)), WITHOUT emitting styleEdited (block
        signals while loading). `box=None` disables the panel (empty state)."""
    def current_values(self) -> dict:   # snapshot of all controls
```

Controls (object names are load-bearing — the app test + QSS reference them):

| Widget | type | objectName | emits |
|---|---|---|---|
| Font family | `QComboBox` (editable=False, populated from `FontEngine.available_families()`) | `InspectorFamily` | `styleEdited({"font_family": str})` |
| Size | `QDoubleSpinBox` (range 1–999, 1 decimal, suffix " pt") | `InspectorSize` | `styleEdited({"size": float})` |
| Color | `QToolButton` showing a swatch; opens `QColorDialog` | `InspectorColor` | `styleEdited({"color": (r,g,b)})` |
| Bold | checkable `QToolButton` (label "B", bold font) | `InspectorBold` | `styleEdited({"bold": bool})` |
| Italic | checkable `QToolButton` (label "I", italic font) | `InspectorItalic` | `styleEdited({"italic": bool})` |

The panel also shows a read-only **fidelity hint** (the same tier dot/label as
the status chip) reflecting how the picked family will resolve
(`resolve_family(...).tier`), so the user sees green/blue/amber for
embedded-not-applicable/system/base-14. Optional but recommended; reuse
`theme.color_fidelity`.

### 4.2 Wiring

- `Inspector.styleEdited(dict)` → `MainWindow` → `view.apply_style(dict)` (which
  funnels to a `BoxCommand("style", ...)` on the undo stack). ONE control change
  = ONE undo step.
- When `view.selectionChanged(box)` fires, the window calls
  `inspector.set_target(box, document.effective_style(page, box))`. `box=None`
  → `set_target(None, None)` disables the panel.
- The family combo is populated ONCE at construction from
  `FontEngine.available_families()` (cached); never per-selection.

### 4.3 Style + theme

Use `theme` tokens only. Add to `theme.py` (NEW tokens, do not hardcode in the
panel): inspector background (reuse `CHROME_BG`), label color (`TEXT_SECONDARY`),
control border (`CHROME_BORDER`), and a QSS block for `#Inspector` controls
mirroring the toolbar's macOS look. Extend `global_stylesheet()` with the
inspector rules so the panel matches the chrome. Bold/Italic checked state uses
`ACCENT_HOVER` + accent border (same as toolbar `:checked`).

---

## 5. UI — `pdftexteditor/ui/main_window.py` (host + toolbar + commands)

### 5.1 Dock the inspector

- Wrap the inspector in a `QDockWidget` on the **right** (`Qt.RightDockWidgetArea`,
  not floatable/closable in v1, fixed-ish width ~260px), OR a fixed right panel
  in a horizontal splitter with the canvas. Pin one; the dock is simpler and
  matches Acrobat. The dock title is "Format". It is disabled (empty state) when
  no box is selected and when no document is open.

### 5.2 Toolbar additions (after the Undo/Redo group, before the spacer)

- **Add Text** — a CHECKABLE `QToolButton` bound to a new `act_add_text`
  (`QKeySequence("T")` or `Ctrl+Shift+T`; pin one, document it). Toggling on →
  `view.enter_add_text_mode()`; toggling off (or after a box is added) →
  `view.exit_add_text_mode()` and un-check. Icon: a new `_draw_add_text`
  (an "A" with a small "+") registered in `_ICON_DRAWERS` as `"add_text"`.
- **Delete** — `act_delete` (`QKeySequence.Delete`) bound to
  `view.delete_selected()`; enabled only when a box is selected and no editor is
  open. Icon: `_draw_trash` → `"delete"`.
- Optional quick controls (family combo + size spin mirrored in the toolbar) are
  NICE-TO-HAVE; the inspector is the source of truth. If added, they share the
  same `apply_style` path — do not create a second mutation route.

Add the new icon drawers following the existing `_icon_from_path` pattern; keep
them monochrome line glyphs. Wire enable/disable in `_sync_actions`:
`act_delete.setEnabled(has_doc and view.current_selection() is not None)`;
`act_add_text.setEnabled(has_doc)`.

### 5.3 The generalized undo command (replaces the text-only `EditRunCommand`)

Keep `EditRunCommand` for text (or fold it in). Add ONE `BoxCommand(QUndoCommand)`
that handles style/move/resize/delete/add by delegating to the model's
generalized history:

```python
class BoxCommand(QUndoCommand):
    def __init__(self, document, view, kind, box, params): ...
    def redo(self):  # apply via document.set_style/move_box/resize_box/
                     # delete_box/add_box (FIRST redo == the user action;
                     # subsequent == replay), then view.repaint_box(box) +
                     # refresh selection overlay.
    def undo(self):  # the model's inverse for `kind`, then repaint.
```

Because the MODEL owns the authoritative before/after (`_Command`/`_BoxState`,
§1.4), `BoxCommand.redo/undo` can be thin: they ask the model to apply/revert the
LAST staged change for that box, OR (cleaner) the model exposes
`apply_box_command(kind, box, params) -> token` and `revert(token)`. Pin one;
the REQUIREMENT is a single QUndoCommand subclass driving all box mutations so
undo/redo interleave correctly with text edits on one stack.

`MainWindow` connects `view.boxCommandRequested(kind, box, params)` →
`self.undo_stack.push(BoxCommand(self.document, self.view, kind, box, params))`.
`view.editCommitted` keeps pushing the text command exactly as today.

### 5.4 selectionChanged wiring + status

- Connect `view.selectionChanged(box)` →
  `inspector.set_target(box, document.effective_style(page, box))` and update a
  status hint (e.g. show the selected box's family/size in the existing
  `font_chip`, or a new "1 box selected" label).
- `act_delete` / inspector enablement update on every `selectionChanged`.
- `edit_count` in the status bar now reflects style/move/resize/delete/add too
  (the model's extended `edit_count`, §1.9).

### 5.5 Keyboard shortcuts (additions; existing ones unchanged)

| Action | Shortcut | Notes |
|---|---|---|
| Add Text (toggle) | `T` (or `Ctrl+Shift+T`) | pin one |
| Delete selected box | `Delete` / `Backspace` | only in SELECT mode; the view guards against firing during TEXT_EDIT |
| Edit selected box text | `Return` / `Enter` | view-level, when a box is selected & no editor open |
| Deselect | `Esc` | view-level |

These are VIEW-level (handled in `PageView.keyPressEvent`) for
Delete/Enter/Esc/box-context keys, because they depend on selection/editor state;
Add Text is a window `QAction` (global). Ensure `PageView` has
`Qt.StrongFocus` (it does) and grabs focus on selection so the keys route to it.

---

## 6. Geometry reference (handles, hit-test, coordinate round-trips)

All in scene/zoom/rotation terms, reusing PageView's existing helpers so editor
== overlay == insert.

- **Box scene rect:** `_span_scene_rect(box)` (already rotation-aware) for any
  Box (works for NewBox too — it has `.bbox`). The selection overlay outline is
  this rect inflated by ~2px cosmetic.
- **Handle positions (scene coords):** for the box scene rect `R`, the 8 handles
  are at the 4 corners and 4 edge midpoints of `R`. Each handle is a
  device-independent `~9px` square centered on its point; use `setCosmetic` /
  fixed-size items so handles stay constant size across zoom. Hit-test order:
  handles (NW…W) before body.
- **Scene → PDF point (for move/add):** invert `_scene_point`:
  `display = (scene - sheet_origin) / zoom`, then through the INVERSE rotation
  matrix (`self._rotation_matrix` inverted; for unrotated pages it's identity).
  Add a private `_pdf_point(scene: QPointF) -> tuple` helper; this is the exact
  inverse of `_scene_point` / `_display_point`. New boxes in v1 are horizontal,
  so origin = the de-rotated baseline point.
- **Move delta:** `(dx, dy)_pdf = (Δscene / zoom)` inverse-rotated. For an
  unrotated page it is just `Δscene / zoom`.
- **Resize scale:** corner-handle drag → `scale = max(MIN, newDiagonal /
  startDiagonal)` measured from the opposite corner (so the anchor stays fixed);
  clamp size to a sane floor (e.g. ≥ 2pt) and a ceiling. Font size for
  reinsertion = `startSize × scale`.
- Rotation handling reuses `_display_point` / `_place_text_item` exactly; new
  boxes are authored horizontal so they need only the base scene mapping.

---

## 7. What changes vs what is added (numbered, unambiguous)

**`document.py`**
1. CHANGE `Edit`: add `style`/`move`/`scale`/`deleted`; make `new_text`
   `Optional`; add `effective_text` + `is_noop`. (§1.2)
2. ADD `StyleOverride` (§1.1), `NewBox` (§1.3), `_Command` + `_BoxState`,
   replacing `_Delta` (§1.4), and a `Box` protocol doc (§1.8).
3. ADD methods: `set_style`, `move_box`, `resize_box`, `delete_box`, `add_box`,
   `effective_style`, `new_boxes` (or fold into `spans()`), plus the private
   `_command(...)` snapshot helper. KEEP `stage_edit` signature; reroute it onto
   the generalized Edit. (§1.5)
4. CHANGE `undo`/`redo`: replay generalized `_Command`s; return `(page_index,
   box_ref)`. (§1.4)
5. CHANGE `_apply_page_edits`: honor `style`/`move`/`scale`/`deleted` on existing
   spans AND draw non-deleted NewBoxes, choosing `resolve` vs `resolve_family`
   per §1.7. KEEP redaction flags, overlap-merge redaction, neighbor redraw.
6. CHANGE `render_with_edits` / `save_as`: unchanged signatures; both inherit the
   new behavior via `_apply_page_edits`. KEEP atomic temp+replace.
7. CHANGE `has_edits`/`edit_count`/`staged_text` to account for NewBoxes +
   non-text edits. (§1.9)

**`font_engine.py`**
8. ADD `available_families()` (§2.1), `resolve_family(...)` (§2.2),
   `fitz_font_for(rf)` (§2.3). KEEP `resolve(...)` and the whole 3-tier path
   untouched (still used for original-span text edits).

**`fonts.py`**
9. KEEP. `resolve_family`'s base-14 fallback reuses `_family`/`is_bold`/
   `is_italic` on the picked family string (expose `_family` or add a thin
   `classify_family(name)` helper if you don't want to import the private).

**`ui/page_view.py`**
10. ADD selection model: `SelectionOverlay` + handle items, `_mode` enum, drag
    state, the §3.3 mouse/key routing, and the §3.4 public API + signals. KEEP
    `SpanHotspot`, `InlineRunEditor`, `begin_edit`/`commit_edit`/`cancel_edit`,
    all geometry helpers, `render_with_edits`-driven `reload`/`repaint_span`
    (generalize `repaint_span` → `repaint_box`).
11. ADD `_pdf_point` (inverse of `_scene_point`) and `apply_style`/
    `delete_selected`/`enter_add_text_mode`/`current_selection`/`select_box`.

**`ui/inspector.py`**
12. NEW MODULE (§4): the `Inspector` panel, `styleEdited` signal, `set_target`.

**`ui/main_window.py`**
13. ADD the inspector dock (§5.1), Add Text + Delete toolbar actions/icons
    (§5.2), `BoxCommand` (§5.3), `selectionChanged`/`boxCommandRequested` wiring
    (§5.4), new shortcuts (§5.5). KEEP all existing chrome/undo/dirty/save flow;
    `EditRunCommand` stays for text (or is folded into `BoxCommand`).

**`ui/theme.py`**
14. ADD inspector QSS tokens + rules in `global_stylesheet()` (§4.3). KEEP all
    existing tokens.

**Tests**
15. KEEP `tests/test_app.py` + `tests/test_font_fidelity.py` PASSING unchanged
    (text-edit fidelity invariants). ADD new coverage for style/move/resize/
    delete/add (resolve_family never Tier 1; a restyled/new box saves with an
    embeddable face and real ink; move/resize geometry; delete removes ink;
    undo/redo across mixed edit types on one stack). Reuse the existing harness
    helpers (`region_ink`, `saved_span_basefonts`).

---

## 8. Headless verification (extend the existing harness)

Every claim below is checkable with `QT_QPA_PLATFORM=offscreen` + the venv
python, no human in the loop:

1. **resolve_family is never Tier 1, always embeddable.** For each
   `available_families()` family × {plain,bold,italic} ×
   `resolve_family(..., "Zephyr 1234 Kx")`: `tier != TIER_EMBEDDED`,
   `fitz_font_for(rf)` covers the text (no tofu), and a save that inserts with it
   leaves NO base-14 font when tier==SYSTEM (and a valid base-14 when it must
   fall to BASE14).
2. **Style apply round-trips.** Select a body run, `apply_style({"size": +N,
   "color": red, "bold": True, "font_family": "Georgia"})`; the rendered page +
   the saved page show the new size/color/weight; `region_ink` > 0; old run text
   gone; the saved face is the picked family (or its embeddable resolution),
   never tofu. WYSIWYG: `render_with_edits` pixmap region matches the saved
   region.
3. **Move/resize geometry.** `move_box(dx,dy)` shifts the box's saved ink by
   ~`(dx,dy)` points (re-extract the span/region and compare origins within a
   small tolerance). `resize_box(scale=2)` doubles the saved font size (compare
   extracted span size).
4. **Delete removes ink.** `delete_box` → the region's `region_ink` drops to ~0
   (background only) and the text is gone from `get_text()`, while a neighboring
   cell/border's ink is unchanged (non-destructive redaction held).
5. **Add box.** `add_box(origin, "Hello 42", "Helvetica", 14, black, F, F)` →
   the saved page has "Hello 42" at ~origin with real ink and an embeddable font;
   undo removes it entirely; redo restores it.
6. **Mixed undo/redo on one stack.** A sequence text→style→move→delete→add then
   5× undo returns the document to pristine (`edit_count == 0`, `get_text()`
   equals the original), and 5× redo reproduces the final state — proving the
   generalized history interleaves all kinds correctly.
7. **Invariants 0.1–0.5 still hold:** `tests/test_app.py` +
   `tests/test_font_fidelity.py` pass unchanged.

---

## 9. Open choices the builders must pin (and where)

These are intentionally left to the implementer; each must be DOCUMENTED in the
module that resolves it, so there is no silent drift:

- New-box keying: `spans()`-composed vs separate `new_boxes()` (§1.8) — pin in
  `document.py`.
- Edge handles: proportional-from-opposite-corner vs single-axis (§3.3/§6) — pin
  in `page_view.py`.
- New-box baseline placement on click: baseline-at-click vs top-at-click (§3.5)
  — pin in `page_view.py`.
- Empty-add cleanup mechanism (§3.5) — pin in `page_view.py`/`main_window.py`.
- Add Text shortcut: `T` vs `Ctrl+Shift+T` (§5.5) — pin in `main_window.py`.
- Inspector host: `QDockWidget` vs splitter panel (§5.1) — pin in
  `main_window.py`.
- `BoxCommand` vs `BoxCommand`+`EditRunCommand` split (§5.3) — pin in
  `main_window.py`.

The HARD requirements (which must NOT be re-decided): the §0 invariants; one
mutation route (a single `QUndoCommand` family driving the model's generalized
history); user-picked families resolve via `resolve_family` and are always
embeddable; every edit type flows through `_apply_page_edits` so WYSIWYG holds.
