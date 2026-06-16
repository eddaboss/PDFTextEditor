# PAGES_SPEC — Page & Document Management

Architect spec for the next build. Adds **combine/merge**, **split/extract**,
**page operations** (reorder / rotate / insert / duplicate / delete via a
thumbnail sidebar), **multiple open PDFs as document tabs**, and two **UX polish**
items (searchable font picker, more-visible selection). This spec PINS exact
interfaces so the model builder and the UI builder cannot drift.

Read order: this section list is the contract. Anything not pinned here is an
implementer choice, but the **signatures, signal shapes, and the §0 invariants
are HARD** — do not re-decide them.

Prereqs: read `docs/BUILD_SPEC.md` and `docs/EDITOR_SPEC.md` first. This spec
extends, never replaces, their numbering. Use the project venv for everything:
`/Users/edward/Documents/GitHub/PDFTextEditor/.venv/bin/python`; headless tests
set `QT_QPA_PLATFORM=offscreen`. **Privacy: test only on `tests/fixtures/`. Never
read `/Users/edward/Downloads`.**

---

## 0. Non-negotiable invariants (carry forward verbatim, do NOT regress)

These are the five EDITOR_SPEC §0 invariants. Page/document ops are layered so
they stay true. Any change that breaks one is a defect, not a tradeoff.

1. **Baked WYSIWYG.** The on-screen page comes from
   `document.render_with_edits()`, which applies edits through the SAME
   `_apply_page_edits` pipeline as `save_as`. What is on screen == the saved
   file. (The white cover under the LIVE inline editor stays.)
2. **Overlap-merge box recognition.** `document.spans()` runs
   `_merge_overlapping()`; `Span.redact_bboxes` holds every member bbox; editing
   redacts all of them.
3. **3-tier font resolution.** Embedded original → same-family system → base-14.
   A user-PICKED family is NEVER Tier 1; it resolves via `resolve_family(...)`.
4. **Non-destructive redaction.** `apply_redactions(images=PDF_REDACT_IMAGE_NONE,
   graphics=PDF_REDACT_LINE_ART_NONE, text=PDF_REDACT_TEXT_REMOVE)` so colored
   cells / borders / fills survive.
5. **Undo/redo + dirty tracking + atomic save.** The model owns an authoritative
   history; the Qt `QUndoStack` drives it; `save_as` stages to a temp file and
   `os.replace`s.

**The page/document layer adds ONE structural rule that protects all five:**

> **§0.6 — BAKE-THEN-MUTATE.** A structural op (rotate / delete / insert /
> duplicate / move / merge / extract / split) MUST first **bake** every pending
> text/box edit into the working document via the existing `_apply_page_edits`
> pipeline, THEN apply the structure change to the now-baked working document,
> THEN clear the staged-edit maps. After a bake, the working `fitz.Document` IS
> the edited content, so WYSIWYG (§0.1) and the editor (invariants 2–4) keep
> working unchanged on the restructured document, and no staged edit is ever
> orphaned by a page-index shift.

---

## 1. THE CORE DESIGN PROBLEM — and the pinned solution

### 1.1 Why today's model breaks under structural ops

`PDFDocument` today **never mutates** `self.doc` (the display document). It stages
text/box edits in `self._edits` / `self._new_boxes`, keyed by **page index**
(`(page_index, span.key)` and `(page_index, "new", box_id)`), and only realizes
them against a **fresh copy of `self.path`** at `save_as` / `render_with_edits`
time.

A page-structure op changes which content lives at which index (delete shifts
everything down; move permutes; insert/duplicate inject; merge appends a foreign
doc's pages; rotate changes a page's geometry). If we changed page order while
keeping the index-keyed staged edits, **every staged edit would point at the
wrong page** — orphaned or mis-applied. And `save_as` re-opens `self.path` from
disk, which has no knowledge of any structure change at all.

### 1.2 The pinned model: a mutable WORKING document

Introduce a single mutable working `fitz.Document` per open file. **This is the
one substantive change to the model's architecture.**

```
PDFDocument.working : fitz.Document    # the mutable working copy
```

- On `__init__`, `working` is a **deep copy** of the opened file:
  `self.working = fitz.open(); self.working.insert_pdf(fitz.open(path))`
  (or `self.working = fitz.open(path)` then operate on it — pin the copy form so
  `self.path` on disk is never touched; see §3.0).
- **All reads now go through `working`, not a re-open of `self.path`.**
  `spans()`, `render()`, `page_rotation()`, `rotation_matrix()`, `page_count`
  read `self.working`. `render_with_edits()` and `save_as()` open their fresh
  copy **from `self.working`** (via an in-memory `self.working.tobytes()` →
  `fitz.open("pdf", data)`), NOT from `self.path`.
- `save_as(out_path)` applies staged edits to a fresh copy of `self.working` and
  writes it. After a structural op (which baked + cleared edits), there are no
  staged edits, so `save_as` simply writes the working doc.

This is the **smallest** change that makes WYSIWYG survive structural ops: the
existing edit pipeline is reused verbatim; only its **source document** changes
from "fresh open of `self.path`" to "fresh open of `self.working` bytes."

### 1.3 BAKE: the structural-op preamble (pin this exactly)

Every structural method calls a private `_bake_pending_edits()` FIRST:

```python
def _bake_pending_edits(self) -> None:
    """Realize ALL staged text/box edits INTO self.working, then clear them.
    After this returns, self.working visually equals what save_as would have
    produced, and _edits / _new_boxes are empty. Idempotent (no-op when clean)."""
    if not self._edits and not self._new_boxes:
        return
    engine = FontEngine(self.working)          # bound to the working doc
    by_page = {}                                # same grouping as save_as
    for (pi, _), edit in self._edits.items():
        if not edit.is_noop:
            by_page.setdefault(pi, []).append(edit)
    for box in self.new_boxes_all():
        by_page.setdefault(box.page_index, [])
    for pi, edits in by_page.items():
        self._apply_page_edits(self.working, engine, pi, edits)
    self._edits.clear()
    self._new_boxes.clear()
    # box ids keep counting up (never reuse) so a later add is unambiguous.
```

`_apply_page_edits` is **unchanged** — it already takes `(out_doc, engine,
page_index, edits)` and mutates `out_doc[page_index]` in place. We point it at
`self.working` instead of a throwaway copy. (It currently also draws
`self.new_boxes(page_index)`; that reads the same maps we clear after, so the
new boxes ARE drawn during the bake. Pin: `_apply_page_edits` keeps reading
`self.new_boxes(...)` — bake passes the page-keyed map and the NewBox draw is
implicit, exactly as `save_as` does today.)

WYSIWYG cross-check (§0.1) survives because bake uses the identical pipeline
`save_as` and `render_with_edits` use. The font engine is rebuilt against
`self.working` so embedded-xref lookups resolve against the baked doc.

### 1.4 Structural op skeleton (every method follows this)

```python
def <structural_op>(self, ...):
    self._snapshot_for_undo()          # push working bytes BEFORE the change (§1.5)
    self._bake_pending_edits()         # §0.6 — realize + clear staged edits
    <fitz mutation on self.working>    # delete_page/move_page/insert_pdf/...
    self.font_engine = FontEngine(self.working)   # rebind: structure changed
    self._invalidate_caches()          # drop spans()/xref caches if any
    self._dirty = True
    return <result>                    # new index, file path, etc. (per method)
```

`_invalidate_caches()` resets the FontEngine's per-doc caches by rebinding a
fresh `FontEngine(self.working)` (the class-level system index + Qt-loaded
families are process-wide and stay). There is no span cache to clear today;
`spans()` re-reads `working` each call.

### 1.5 UNDO for structural ops: a bytes-snapshot stack (pinned, justified)

Text/box edits use the fine-grained `_Command` history. Structural ops are
coarse (they permute whole pages and bake edits), so a fine-grained inverse is
fragile. **Pin a separate bytes-snapshot undo stack for structural ops:**

```
PDFDocument._struct_undo : list[bytes]   # working-doc snapshots, pre-op
PDFDocument._struct_redo : list[bytes]
```

- `_snapshot_for_undo()` pushes `self.working.tobytes()` onto `_struct_undo` and
  clears `_struct_redo`, BEFORE the op mutates `working`. (Snapshot is taken
  before bake, so undo restores the pre-bake state exactly: the un-baked staged
  edits are also gone after the op, which is acceptable — see "Interaction" below.)
- `undo_structural()` → if `_struct_undo`: push current `working.tobytes()` onto
  `_struct_redo`, `self.working = fitz.open("pdf", _struct_undo.pop())`, rebind
  the FontEngine, clear `_edits`/`_new_boxes`, set dirty. Returns `True` if it
  undid something.
- `redo_structural()` → symmetric.

**Why a separate stack, not the unified `QUndoStack`:** the existing
`QUndoStack` drives the model's fine-grained `_Command` history in lockstep
(EDITOR_SPEC §5.3). Forcing a whole-document bytes snapshot into that stack would
mean a single Qt command whose `undo` replaces `self.working` AND that must
coexist with span-keyed text commands whose `_box` refs become invalid the
instant the document is restructured. Pinning structural undo to its own stack
keeps each history internally consistent.

**Pinned UI consequence (HARD):** a structural op **bakes and clears the
text/box edit history**. Therefore, when a structural op is applied, the
**window MUST also `undo_stack.clear()` + `undo_stack.setClean()` is NOT called**
(the doc is dirty), i.e. the Qt fine-grained stack is reset and the window's
Undo/Redo now drive `undo_structural()/redo_structural()`. To keep ONE visible
Undo affordance, the window wraps each structural op in a `StructuralCommand`
(QUndoCommand, §6.6) pushed onto the SAME `QUndoStack` AFTER clearing it, so the
user sees a single coherent undo timeline. Pin: **a structural op first commits
any open inline editor, then clears the fine-grained Qt stack, then pushes one
`StructuralCommand`.** Mixed fine-grained text-edit-then-structural-then-text-edit
works because each structural boundary resets the fine-grained stack and the
`StructuralCommand`'s own undo restores the working bytes.

> Rationale for the "bake clears fine-grained history" tradeoff: after a bake the
> staged edits no longer exist as edits (they are now ink in `working`), so there
> is nothing for the fine-grained stack to undo against. Undoing PAST a
> structural boundary restores the working bytes (which still contain the baked
> ink), which is the correct visual result. This is the same coarse-grained model
> Acrobat uses for page ops.

### 1.6 Interaction of bake + dirty + save (pin the truth table)

| State | `dirty` | `save_as` source | undo affordance |
|---|---|---|---|
| only text/box edits staged | True | fresh copy of `working`, apply `_edits` | fine-grained `_Command` |
| after a structural op | True | fresh copy of `working` (edits already baked+cleared) | `_struct_undo` via `StructuralCommand` |
| clean (just saved) | False | n/a | empty |

`dirty` stays a single bool set by ANY mutation (text, box, or structural),
cleared by `mark_clean()`. `edit_count` (status bar) keeps counting staged
fine-grained edits; after a structural op it reads 0 staged edits but the doc is
still `dirty` (the structural change is unsaved) — the status bar must show the
dirty dot off `dirty`, not off `edit_count` (it already does: `_has_unsaved()`
reads the Qt stack clean state — see §6.8 for the wiring fix).

---

## 2. DocumentManager / Workspace (multiple open PDFs → tabs)

New module: `pdftexteditor/workspace.py`. Holds N open `PDFDocument`s and an
active index. Qt-free (pure model; tab CHROME is the UI builder's job, §5.4).

```python
class Workspace:
    def __init__(self) -> None:
        self._docs: list[PDFDocument] = []
        self._active: int = -1            # -1 when empty

    # --- queries ---
    @property
    def count(self) -> int: ...
    @property
    def is_empty(self) -> bool: ...
    @property
    def active_index(self) -> int: ...           # -1 when empty
    @property
    def active(self) -> PDFDocument | None: ...   # the active doc, or None
    def document(self, idx: int) -> PDFDocument: ...
    def documents(self) -> list[PDFDocument]: ...
    def index_of(self, doc: PDFDocument) -> int: ...   # -1 if absent
    def title(self, idx: int) -> str: ...        # basename(path) + " •" if dirty
    def any_dirty(self) -> bool: ...

    # --- mutation ---
    def open(self, path: str) -> int:
        """Open `path` into a new PDFDocument, append it, make it active,
        RETURN its index. If `path` is already open (same realpath), do NOT
        re-open: switch to and return the existing index (pin: de-dup on
        os.path.realpath)."""
    def add_document(self, doc: PDFDocument) -> int:
        """Append an already-constructed PDFDocument (used by split→open-result),
        make it active, return its index."""
    def close(self, idx: int) -> int:
        """Close the doc at idx (calls doc.close()), remove it, and return the
        NEW active index (-1 when the workspace becomes empty). Active index is
        re-clamped: closing the active tab activates the neighbor to its right,
        else the new last tab."""
    def switch(self, idx: int) -> None:
        """Make idx active. Out-of-range is a no-op."""
    def move_tab(self, src: int, dst: int) -> None:
        """Reorder open tabs (drag-reorder of the tab bar). Keeps `active`
        pointing at the same document object."""
    def close_all(self) -> None: ...
```

**Pinned semantics**
- `open()` de-dups by `os.path.realpath(path)`; a second open of the same file
  switches to the existing tab (matches Preview/Acrobat).
- `close()` must call `doc.close()` to release the fitz handle.
- The Workspace does NOT own dirty-guard dialogs; the **window** asks
  `workspace.document(idx).dirty` and shows the discard dialog before
  `workspace.close(idx)` (reusing `_confirm_discard_if_dirty`, §6.8).
- One `Workspace` per `MainWindow`. The window's existing `self.document`
  becomes a thin alias for `workspace.active` (§6.1).

---

## 3. PDFDocument structural methods — EXACT signatures

All operate on `self.working`, all follow the §1.4 skeleton (snapshot → bake →
fitz mutation → rebind → dirty). 0-based page indices throughout. Out-of-range
indices raise `IndexError` (caller validates first). Each is **Qt-free for the
geometry** but, because `_bake_pending_edits` may run the font resolver, each
**requires a `QGuiApplication` ONLY when there are pending edits to bake** —
guard exactly like `save_as` does (raise a clear `RuntimeError` if edits are
pending and no `QGuiApplication`; no-op-safe when clean). Pin: when `_edits` and
`_new_boxes` are empty, structural ops run with NO Qt dependency (so they are
unit-testable headless without a QApplication for the clean-doc case).

### 3.0 Init change

```python
def __init__(self, path: str) -> None:
    self.path = path
    src = fitz.open(path)
    self.working = fitz.open()
    self.working.insert_pdf(src)           # deep copy; self.path bytes untouched
    src.close()
    self.font_engine = FontEngine(self.working)
    ...                                     # _edits, _new_boxes, _next_box_id,
                                            # _undo/_redo, _dirty as today
    self._struct_undo: list[bytes] = []
    self._struct_redo: list[bytes] = []
```

`self.doc` is **renamed to `self.working`** everywhere in `document.py`
(`render`, `spans`, `page_rotation`, `rotation_matrix`, `page_count`, `close`).
Keep a read-only `@property doc` aliasing `self.working` so any external reader
(page_view reads `document.font_engine`, not `document.doc`) is unaffected —
**audit: page_view.py and main_window.py do NOT reference `document.doc`
directly** (verified — they go through `spans`, `render_with_edits`, `page_count`,
`page_rotation`, `rotation_matrix`, `font_engine`). `save_as` /
`render_with_edits` change their `fitz.open(self.path)` to open from
`self.working.tobytes()` (§1.2).

### 3.1 rotate_page

```python
def rotate_page(self, i: int, deg: int) -> None:
    """Rotate page i by `deg` (must be one of -270..270 in 90° steps; the new
    absolute rotation is (current + deg) % 360). Bakes pending edits first,
    then page.set_rotation(new_abs). Dirty."""
```
- Uses `self.working[i].set_rotation((self.working[i].rotation + deg) % 360)`.
- WYSIWYG note: `set_rotation` sets `/Rotate`; the existing `rotation_matrix()` /
  `_display_point` path already handles rotated pages, so the view renders the
  rotated page correctly with no further change.

### 3.2 delete_page

```python
def delete_page(self, i: int) -> None:
    """Delete page i. Refuses to delete the LAST remaining page (raises
    ValueError 'cannot delete the only page'). Bakes first, then
    self.working.delete_page(i). Dirty."""
```

### 3.3 insert_blank_page

```python
def insert_blank_page(self, at: int, width: float | None = None,
                      height: float | None = None) -> int:
    """Insert a blank page BEFORE index `at` (at == page_count appends at end).
    width/height in PDF points; when None, inherit the size of the page
    currently at min(at, page_count-1) (so a blank matches its neighbor).
    Bakes first, then self.working.new_page(pno=at, width=w, height=h).
    Returns the index of the new page (== at). Dirty."""
```

### 3.4 duplicate_page

```python
def duplicate_page(self, i: int) -> int:
    """Duplicate page i, inserting the copy immediately AFTER i. Bakes first,
    then self.working.fullcopy_page(i, to=i + 1) (fitz inserts the copy and we
    pin it to land at i+1). Returns the new copy's index (i + 1). Dirty."""
```
- `fullcopy_page(pno, to=-1)` copies including annotations/content; pin `to=i+1`.

### 3.5 move_page

```python
def move_page(self, src: int, dst: int) -> None:
    """Reorder: move the page at `src` so it ends up at index `dst` in the
    final ordering (drag-to-reorder semantics — `dst` is the destination slot
    AFTER removal). No-op when src == dst. Bakes first, then
    self.working.move_page(src, dst_for_fitz). Dirty."""
```
- **Pin the index convention (drag semantics, this is the #1 drift risk —
  VERIFIED against PyMuPDF 1.27.2.3, see §7 table):** `move_page(src, dst)` means
  "the dragged thumbnail, dropped so it becomes the `dst`-th page in the resulting
  list" (destination-slot == python `seq.pop(src); seq.insert(dst, x)`). fitz's
  `Document.move_page(pno, to)` inserts the page BEFORE `to` counting in the
  ORIGINAL indexing, and `to` must be in `0..page_count-1` (`to == page_count`
  raises `ValueError("bad page number(s)")`). The **verified** translation is:
  ```python
  if dst == src:
      return                       # no-op
  to = dst if dst <= src else dst + 1
  if to >= self.working.page_count:
      self.working.move_page(src, -1)   # append to the very end (to=count errors)
  else:
      self.working.move_page(src, to)
  ```
  Note the boundary is `dst <= src` (NOT `dst < src`), and the end-append case
  uses `to = -1`. This was confirmed exhaustively across all 16 (src,dst) pairs on
  a 4-page doc. Implement and **unit-test against the explicit permutation table**
  (§7 `test_move_page_permutations`) so the translation stays locked. The
  thumbnail sidebar emits `(src, dst)` in this destination-slot convention (§5.1).

### 3.6 merge (combine / append another PDF)

```python
def merge(self, other: "str | PDFDocument", at: int | None = None) -> int:
    """Insert ALL pages of `other` into this document. `other` may be a path
    (opened read-only and closed here) or another open PDFDocument (its WORKING
    doc is the source, so its unsaved edits come along — pin: if `other` is a
    PDFDocument, snapshot other.working baked: call other._bake_pending_edits()
    on a COPY, i.e. merge from other.working.tobytes() AFTER baking a temp copy,
    so we never mutate the other doc). `at` is the insertion index (None == append
    at end). Bakes THIS doc first, then self.working.insert_pdf(src, start_at=at).
    Returns the index of the FIRST inserted page. Dirty."""
```
- Path form: `src = fitz.open(path); self.working.insert_pdf(src, start_at=at);
  src.close()`. When `at is None`, omit `start_at` (fitz appends).
- PDFDocument form (combine two open tabs): build the source bytes from a baked
  copy of `other` WITHOUT mutating `other`:
  ```python
  tmp = fitz.open("pdf", other.working.tobytes())
  # bake other's pending edits into tmp via a throwaway engine + _apply_page_edits
  # (reuse a small static helper bake_into(tmp, other._edits, other._new_boxes))
  self.working.insert_pdf(tmp, start_at=at); tmp.close()
  ```
  Pin a module helper so the bake logic is shared, not duplicated:
  `PDFDocument._bake_doc(doc, edits_map, newbox_map) -> None` (the body of
  `_bake_pending_edits` parameterized over the target doc + maps).
- **Reorder of appended docs** (spec goal "reorder appended docs"): after a merge
  the appended pages are normal pages; reordering them is just `move_page` /
  thumbnail drag. No separate API.

### 3.7 extract_pages (selected pages → a new PDF)

```python
def extract_pages(self, indices: list[int], out_path: str | None = None
                  ) -> str | bytes:
    """Build a NEW PDF containing ONLY the pages in `indices` (in the given
    order; duplicates allowed). Does NOT modify this document. Bakes a COPY of
    self.working first (so edits are realized in the extract) WITHOUT mutating
    self.working or clearing this doc's edits. If out_path is given, writes there
    (atomic temp+replace like save_as) and returns out_path; else returns the
    PDF bytes. Requires QGuiApplication only if there are pending edits."""
```
- Implementation: `tmp = fitz.open("pdf", self.working.tobytes())`; bake this
  doc's edits INTO `tmp` (via `_bake_doc(tmp, self._edits, self._new_boxes)`);
  `out = fitz.open(); out.insert_pdf(tmp, ...)` selecting `indices` — pin: build
  by iterating `for idx in indices: out.insert_pdf(tmp, from_page=idx,
  to_page=idx)` so arbitrary order + duplicates work (a single `select()` cannot
  duplicate or reorder freely). Write/return.
- **Non-mutating** — this is the difference from `delete_page`. Extract is the
  primitive the "extract selected pages to a new PDF" UI action calls (§5.5).

### 3.8 split (one PDF → multiple files)

```python
def split(self, ranges: list[tuple[int, int]], out_dir: str,
          stem: str | None = None) -> list[str]:
    """Write one output PDF per (start, end) INCLUSIVE page range in `ranges`,
    into out_dir. File names: f"{stem or basename}-{n:02d}.pdf" (1-based n).
    Each output is built like extract_pages([start..end]) on a baked copy. Does
    NOT modify this document. Returns the list of written paths (atomic writes).
    Requires QGuiApplication only if there are pending edits."""
```
- Convenience splitters the UI offers (§5.5) all reduce to `ranges`:
  - "every N pages": `[(0,N-1),(N,2N-1),...]`
  - "one file per page": `[(0,0),(1,1),...]`
  - "at page K": `[(0,K-1),(K,page_count-1)]`
  The UI computes `ranges`; the model only consumes `ranges`. Pin: the model
  exposes `split(ranges, ...)` ONLY; no per-strategy methods.

### 3.9 Thumbnail render helper (for the sidebar)

```python
def render_thumbnail(self, page_index: int, max_px: int = 180) -> "fitz.Pixmap":
    """A small pixmap of page `page_index` from self.working (NO staged edits
    baked — thumbnails show structure, and baking per-thumbnail is too costly;
    the sidebar re-requests after any op, by which point structural ops have
    baked anyway). Scale = max_px / max(page_w_px, page_h_px) at 72dpi, clamped
    so the long edge is `max_px`. Uses page.get_pixmap(matrix, alpha=False)."""
```
- Pin: thumbnails render from `self.working` WITHOUT `render_with_edits`. Staged
  *text* edits are not reflected in thumbnails (acceptable: thumbnails convey
  page STRUCTURE / order / rotation, not glyph-level edits). After any structural
  op the edits are baked into `working`, so post-op thumbnails are accurate. The
  CURRENT page in the big canvas still shows full WYSIWYG via `render_with_edits`.

### 3.10 Structural undo surface (model)

```python
@property
def can_undo_structural(self) -> bool: ...
@property
def can_redo_structural(self) -> bool: ...
def undo_structural(self) -> bool: ...   # returns True if it changed working
def redo_structural(self) -> bool: ...
```

---

## 4. UX POLISH — model/engine support

### 4.1 Searchable font family picker (engine side)

`FontEngine.available_families()` already returns the sorted 303-family list;
no engine change required. The searchable combo (§5.6) filters over this list
client-side. Pin: **do not** add server-side filtering to the engine — the list
is small and cached.

### 4.2 More-visible selection (theme tokens)

Add tokens to `theme.py` (the UI builder consumes them in §5.7):

```python
SELECTION_OUTLINE_W = 2.25          # was 1.5 (SelectionOverlay pen width)
SELECTION_OUTLINE_HALO = "rgba(255,255,255,.9)"   # white under-stroke
HANDLE_PX = 11.0                    # was 9.0 (page_view _HANDLE_PX)
```
And a halo color accessor `color_selection_halo() -> QColor`. These are the ONLY
theme additions for selection visibility; the rest is page_view applying them
(§5.7).

---

## 5. UI — exact widget surface

New module: `pdftexteditor/ui/thumbnail_sidebar.py`. Tabs + merge/split actions
live in `main_window.py`. The searchable combo lives in `inspector.py`. Selection
visibility lives in `page_view.py` + `theme.py`.

### 5.1 PageThumbnailSidebar

A `QListWidget` in `IconMode` showing one rendered thumbnail per page of the
ACTIVE document, with the current page highlighted, drag-to-reorder, a context
menu, and click-to-navigate.

```python
class PageThumbnailSidebar(QListWidget):
    # signals (window connects these) -------------------------------------
    pageActivated = Signal(int)                 # click/keyboard select -> navigate
    reorderRequested = Signal(int, int)         # (src, dst) DESTINATION-SLOT (§3.5)
    rotateRequested = Signal(int, int)          # (page_index, deg)  deg in {90,-90,180}
    deleteRequested = Signal(int)               # page_index
    duplicateRequested = Signal(int)            # page_index
    insertBlankRequested = Signal(int)          # insert BEFORE this index
                                                #   (and an "append" via index==count)

    def set_document(self, doc: PDFDocument | None) -> None:
        """Rebuild all thumbnails from `doc` (or clear when None)."""
    def refresh(self) -> None:
        """Re-render every thumbnail from the current document (after any op)."""
    def set_current_page(self, page_index: int) -> None:
        """Highlight the row for page_index WITHOUT emitting pageActivated."""
    def current_page(self) -> int: ...
```

**Pinned behavior**
- **Drag-to-reorder:** `setDragDropMode(InternalMove)`. On an internal move,
  intercept (override `dropEvent`, compute src row + dst row), emit
  `reorderRequested(src, dst)` in the §3.5 destination-slot convention, and DO
  NOT let the list reorder its own items — the window applies `move_page`, then
  calls `refresh()` so the list rebuilds from the new truth (pin: model is the
  source of truth; the list never reorders itself optimistically, avoiding a
  desync if the op fails).
- **Context menu** (right-click a thumbnail): Rotate Right (90), Rotate Left
  (-90), Rotate 180, Duplicate Page, Insert Blank Before, Insert Blank After,
  Delete Page. Each emits its signal with the row index. Delete is disabled when
  `count == 1`.
- **Click → navigate:** `currentRowChanged` (user-driven only) emits
  `pageActivated(row)`. `set_current_page` sets the row with signals blocked so
  canvas→sidebar sync never loops back.
- **Thumbnail item:** icon from `doc.render_thumbnail(i)`, label `str(i+1)`, a
  small rotation badge is optional. Selected row uses the accent highlight.
- The sidebar is a `QDockWidget("Pages")` on the LEFT, width ~160, toggled by a
  toolbar button + `View > Pages` (Cmd+Shift+P). Pin: `NoDockWidgetFeatures`
  off — it MAY be closable (unlike the fixed Format dock), with a toolbar toggle.

### 5.2 Document TAB bar

Pin a `QTabBar` (not full `QTabWidget` — the canvas is shared, only the active
document changes) docked just under the toolbar, above the canvas.

```python
class DocumentTabBar(QTabBar):
    tabActivated = Signal(int)          # user selected tab idx
    tabCloseRequested = Signal(int)     # Qt's built-in; window guards dirty
    tabMoved2 = Signal(int, int)        # (from, to) reorder (Qt tabMoved)

    def sync(self, workspace: Workspace) -> None:
        """Rebuild tabs from workspace: one tab per document, text =
        workspace.title(i) (basename + ' •' if dirty), current = active_index.
        Blocks signals while syncing."""
```
- `setTabsClosable(True)`, `setMovable(True)`, `setExpanding(False)`,
  `setUsesScrollButtons(True)`, `setDrawBase(True)`.
- The tab bar is **hidden when `workspace.count <= 1`** (single-doc looks
  tabless, like Preview) — pin this so the common case is uncluttered.
- `tabMoved` → `workspace.move_tab(from, to)`.
- A `+` corner button (or the existing Open action) adds a tab.

### 5.3 Merge / Split menu + toolbar actions and dialogs

Add a small **"Pages"** menu (or reuse a menu bar; the app currently has no menu
bar — pin: ADD a native menu bar with File / Edit / Pages / View, since page ops
need discoverable homes, and put the existing actions there too). Toolbar gets a
compact **"Organize"** group: a single popup button (`QToolButton`,
`MenuButtonPopup`) with: Combine PDFs…, Extract Pages…, Split PDF…, plus Rotate /
Delete / Insert / Duplicate that act on the CURRENT page.

Actions + their flows (window methods, §6.5):

- **Combine / Merge** `act_combine` → `combine_pdfs()`:
  `QFileDialog.getOpenFileNames` (multi-select) → for each picked path,
  `workspace.active.merge(path)` (append). Then `_after_structural_op()`. A
  variant **"Combine open tab into this"** uses `merge(other_doc)` — offered when
  ≥2 tabs are open (a submenu listing the other open titles).
- **Extract Pages** `act_extract` → `extract_pages_dialog()`: a small dialog with
  a page-range field ("e.g. 1-3, 5, 8-10", 1-based) → parse to 0-based `indices`
  → `QFileDialog.getSaveFileName` → `workspace.active.extract_pages(indices,
  out_path)` → ask "Open the extracted file?" → if yes `workspace.open(out_path)`.
  Does NOT change the source doc (no `_after_structural_op` on the source).
- **Split** `act_split` → `split_dialog()`: radio choices {Every N pages (N
  spin), One file per page, At page K}, an output-folder picker → compute
  `ranges` → `workspace.active.split(ranges, out_dir)` → toast "Wrote K files".
- **Rotate/Delete/Insert/Duplicate current page**: thin wrappers over the active
  doc's structural methods on `view.page_index`, then `_after_structural_op()`.
  These ALSO back the sidebar's context-menu signals (§6.4 routes both to the
  same handlers).

Dialogs: pin them as small modal `QDialog`s built in `main_window.py` (no new
module needed); the page-range parser is a static helper
`MainWindow._parse_page_ranges(text, page_count) -> list[int]` returning 0-based
indices (raises `ValueError` on bad input → window shows an inline error).

### 5.4 Tabs ↔ canvas wiring

When the active document changes (`tabActivated`, `open`, `close`):
`view.set_document(workspace.active)`, `sidebar.set_document(workspace.active)`,
`inspector.set_font_engine(active.font_engine)`, reset Qt undo stack to the
active doc's structural undo state, refresh all chrome. Pin: **each tab carries
its own edits + structural undo** (they live on the `PDFDocument`), so switching
tabs is just re-pointing the view/sidebar/inspector and rebuilding the Qt stack
view. The window keeps the Qt `QUndoStack` bound to the active doc; on switch it
`undo_stack.clear()`s (the per-doc fine-grained `_Command` history lives in the
model and is not replayed into Qt across switches — pin: fine-grained Qt history
does not survive a tab switch; the model's own `_undo`/`_redo` and `_struct_*`
persist, and Undo after a switch drives the model directly via a rebound command —
see §6.7). This is acceptable and matches per-document undo expectations.

### 5.5 Extract/Split source-of-truth

Extract and Split are **read-only** on the source document (they bake a COPY).
They never appear in the structural undo stack of the source. The only state
change is possibly opening the result as a new tab.

### 5.6 Searchable font combo (Inspector)

Make `InspectorFamily` editable + type-to-filter over the 303 families, WITHOUT
changing the `styleEdited({"font_family": ...})` contract.

Pin the implementation (drop-in inside `Inspector._build`):
```python
self.family_combo = QComboBox()
self.family_combo.setObjectName("InspectorFamily")
self.family_combo.setEditable(True)                     # was False
self.family_combo.setInsertPolicy(QComboBox.NoInsert)   # typing never adds items
self.family_combo.addItems(FontEngine.available_families())
completer = self.family_combo.completer()
completer.setCompletionMode(QCompleter.PopupCompletion)
completer.setFilterMode(Qt.MatchContains)               # type-to-filter substring
completer.setCaseSensitivity(Qt.CaseInsensitive)
self.family_combo.currentIndexChanged.connect(self._on_family_index)   # commit
self.family_combo.lineEdit().editingFinished.connect(self._on_family_commit)
```
**Pinned commit rule (avoid spurious edits):** emit `styleEdited({"font_family":
fam})` ONLY when the chosen text is an EXACT family in
`available_families()` (case-insensitive). Free-typed text that does not match a
known family is reverted to the target's current family on `editingFinished`
(never emit a non-existent family — invariant §0.3 requires an embeddable
family). `set_target`'s `_select_family` still works (it `findText`s and selects).
The `_loading` guard still suppresses echo during population. Pin: keep
`_on_family_changed`'s body (refresh fidelity + emit) but gate it behind the
exact-match check; rename the slot wiring per above but preserve the emitted
dict shape EXACTLY so the window/model are untouched.

### 5.7 More-visible selection (page_view)

In `page_view.py`, raise selection prominence using the §4.2 tokens:
- `SelectionOverlay.paint`: draw a **two-pass** outline — first a `HALO` white
  cosmetic pen at `SELECTION_OUTLINE_W + 1.5`, then the accent pen at
  `SELECTION_OUTLINE_W` — so the outline reads on both light and dark page
  regions. Keep `_OUTLINE_INFLATE`.
- `_HANDLE_PX = theme.HANDLE_PX` (11.0); handles keep the white 1px border they
  already draw, now over a larger square (easier grab + more obvious).
- Optionally add a faint accent **fill wash** (`color_accent_hover`) inside the
  selection rect at `Z_SELECTION - 1` so the selected box is obvious at a glance.
  Pin: wash alpha ≤ .10 so it never obscures text.
These are visual-only; they do NOT change geometry math, hit-testing, or any
signal — invariant-safe by construction.

---

## 6. MainWindow wiring — exact integration

### 6.1 Workspace replaces the single document

- `self.workspace = Workspace()`. `self.document` becomes a read-only convenience
  alias: replace all `self.document` reads with `self.workspace.active` (or keep
  `self.document` as `@property` returning `self.workspace.active` to minimize
  churn — **pin this property approach** so existing methods (`save_pdf`,
  `_sync_status`, etc.) keep working with a one-line shim).
- `open_path(path)` → `idx = self.workspace.open(path)` then
  `_activate_document(idx)` (the new method that does the §5.4 wiring) instead of
  closing the old doc.

### 6.2 New child widgets

In `__init__` after the canvas:
- `self.tab_bar = DocumentTabBar()` placed above the canvas (a top-area widget or
  a 2nd toolbar row); hidden when `count<=1`.
- `self.sidebar = PageThumbnailSidebar()` in a left `QDockWidget("Pages")`.
- A native `QMenuBar` (File/Edit/Pages/View) — move existing actions in, add the
  new page/organize actions.

### 6.3 Tab-bar signals

```python
self.tab_bar.tabActivated.connect(self._on_tab_activated)       # switch + rewire
self.tab_bar.tabCloseRequested.connect(self._on_tab_close_requested)  # dirty-guard then workspace.close
self.tab_bar.tabMoved.connect(self.workspace.move_tab)
```

### 6.4 Sidebar signals (route to the SAME handlers as the toolbar)

```python
self.sidebar.pageActivated.connect(self._on_thumb_activated)    # view.set_page
self.sidebar.reorderRequested.connect(self._on_reorder)         # move_page + after_op
self.sidebar.rotateRequested.connect(self._on_rotate_page)      # rotate_page + after_op
self.sidebar.deleteRequested.connect(self._on_delete_page)
self.sidebar.duplicateRequested.connect(self._on_duplicate_page)
self.sidebar.insertBlankRequested.connect(self._on_insert_blank)
```
Each handler: validate, flush any open editor, call the active doc's structural
method, then `_after_structural_op()`.

### 6.5 The structural-op funnel

```python
def _after_structural_op(self, *, new_page: int | None = None) -> None:
    """Single post-op refresh: rebind FontEngine already done in the model;
    rebuild the Qt undo stack as a structural boundary, refresh canvas + sidebar
    + tab title + status."""
    doc = self.workspace.active
    self.view.set_document(doc) if <full reload needed> else self.view.reload()
    # pin: prefer view.reload() + view.set_page(new_page or clamp) so selection/
    # zoom are preserved where possible; set_document only when the doc identity
    # changed (tab switch).
    self.sidebar.refresh()
    self.sidebar.set_current_page(self.view.page_index)
    self.tab_bar.sync(self.workspace)
    self._push_structural_boundary()     # §6.6
    self._sync_all()
```

`_push_structural_boundary()`:
```python
def _push_structural_boundary(self) -> None:
    self.undo_stack.clear()              # fine-grained history reset at the boundary
    self.undo_stack.push(StructuralCommand(self.workspace.active, self))
```

### 6.6 StructuralCommand (QUndoCommand)

```python
class StructuralCommand(QUndoCommand):
    """One coarse structural step on the undo stack. Its FIRST redo is a no-op
    (the model already applied + snapshotted the op before this command was
    pushed); undo() calls doc.undo_structural(); later redo() calls
    doc.redo_structural(). After undo/redo it triggers the window's
    _refresh_after_structural_undo() to rebuild canvas/sidebar/tabs."""
    def __init__(self, doc, window): ...
    def redo(self):
        if not self._applied: self._applied = True; return  # op already done
        self._doc.redo_structural(); self._win._refresh_after_structural_undo()
    def undo(self):
        self._doc.undo_structural(); self._win._refresh_after_structural_undo()
```
Pin: the model op runs in the handler (`_on_rotate_page` etc.) BEFORE the command
is pushed, and the model's `_snapshot_for_undo` already recorded the pre-state, so
the command's first redo must NOT re-run the op (it would double-apply). This
mirrors the existing `_ModelCommand` "first redo is the apply" inversion but here
the apply happens in the handler and the command just owns undo/redo navigation.

`_refresh_after_structural_undo()`: `view.reload()` (or `set_document` if page
count/identity changed), `sidebar.refresh()`, clamp current page, `_sync_all()`.

### 6.7 Per-tab undo on switch

On `_activate_document(idx)`: `undo_stack.clear()` then, if the activated doc has
structural history, push a single `StructuralCommand` so Undo is available; the
fine-grained `_Command` history stays in the model but is not mirrored into Qt
across switches (documented limitation, §5.4). Acceptable: switching tabs and
undoing drives the model's own history; pin a follow-up note that full
per-tab fine-grained Qt restoration is out of scope for THIS build.

### 6.8 Dirty / save across tabs

- `_has_unsaved()` → `self.workspace.active is not None and
  (not self.undo_stack.isClean() or self.workspace.active.dirty)` — pin: OR in
  the model `dirty` so a structural op (which clears the Qt stack but leaves the
  doc dirty) still enables Save. This is the §1.6 truth-table fix.
- `save_pdf` / `save_pdf_as` operate on `workspace.active` (via the `document`
  property). After a Save, `workspace.active.mark_clean()`,
  `undo_stack.setClean()`, and the structural undo stacks MAY be left intact
  (undo-after-save is allowed, it just re-dirties). `tab_bar.sync` updates the
  dirty marker.
- Close guard: `_on_tab_close_requested(idx)` runs the discard dialog against
  `workspace.document(idx)` (temporarily activating it for the dialog), then
  `workspace.close(idx)` and re-activates the new active index. Window-close guard
  iterates `workspace.any_dirty()`.

### 6.9 Defensive wiring stays

Keep the existing `_connect_signal` / `getattr` defensive pattern for the new
sidebar/tab signals so the window still constructs if a builder lands a partial
widget. New actions are added to the menu bar AND remain window `QAction`s so
shortcuts fire regardless of focus.

---

## 7. Headless verification plan

All tests run with the venv python + `QT_QPA_PLATFORM=offscreen`, follow the
existing harness style (a `main()` collecting `failures`, `check(...)`, exit 1 on
any failure, print a PASSED line), and test ONLY on `tests/fixtures/`. Because
every fixture is **single-page**, structural tests **synthesize multi-page
fixtures in `tempfile`** by `insert_pdf`-ing fixtures together (never write into
`tests/fixtures/`).

New file `tests/test_pages.py`. Reuse `find_span`, `save`, `wysiwyg_match`,
`_pix_ink`, `region_ink` from `test_editor_model.py` (import or copy the helpers).

**Model tests (no Qt needed for clean-doc cases; construct a `QApplication` once
for the bake-with-edits cases, like the other tests):**

1. `test_working_doc_isolation` — opening a fixture and mutating `working` never
   changes the bytes of `self.path` on disk (hash the file before/after).
2. `test_rotate` — `rotate_page(0, 90)`; `page_rotation(0) == 90`; save; reopen;
   rotation persists; **WYSIWYG**: `wysiwyg_match(doc, saved) < 0.02`.
3. `test_delete_page` — build a 3-page temp doc; `delete_page(1)`;
   `page_count == 2`; the page that was at index 2 is now at 1 (assert by a known
   span text per source page); refuse-last-page raises `ValueError`.
4. `test_insert_blank` — `insert_blank_page(1, w, h)`; `page_count+1`; new page is
   blank (`spans(1) == []`); inherits neighbor size when None.
5. `test_duplicate` — `duplicate_page(0)`; `page_count+1`; `spans(0)` text ==
   `spans(1)` text.
6. `test_move_page_permutations` — **the off-by-one lock**: build a 4-page temp
   doc tagged A/B/C/D; for ALL 16 `(src,dst)` pairs assert `move_page(src,dst)`
   yields `expect("ABCD", src, dst)` where `expect` is python
   `seq.pop(src); seq.insert(dst, x)`. Spot-checked verified results (must match):
   `(0,3)→BCDA`, `(3,0)→DABC`, `(1,2)→ACBD`, `(2,1)→ACBD`, `(1,3)→ACDB`. The end
   cases (`dst == page_count-1` from any earlier `src`) exercise the `to == count`
   append-via-`-1` branch — they are the ones the naive `dst+1` translation got
   wrong, so they MUST be in the table.
7. `test_merge_path` — merge fixture B into fixture A (append); `page_count`
   sums; first page of B appears at the right index; **non-mutating to B's file**.
8. `test_merge_doc_bakes_other` — open two docs, stage a text edit on `other`,
   `merge(other)`; the merged page carries the EDITED text (other's edit baked
   into the inserted pages); `other` itself is unchanged (still dirty, edit still
   staged).
9. `test_extract` — `extract_pages([2,0])` from a 3-page doc → a 2-page file whose
   pages are source-2 then source-0 (order honored, duplicates allowed); source
   `page_count` unchanged.
10. `test_split` — `split([(0,0),(1,2)], tmpdir)` on a 3-page doc → 2 files of 1
    and 2 pages; source unchanged.
11. `test_bake_then_mutate_preserves_edits` — stage a text edit on page 1, then
    `move_page(1,0)`; the edited text now renders on page 0 (edit baked, not
    orphaned); `wysiwyg_match` after a fresh `save_as` < 0.02; **no staged edit
    remains** (`edit_count == 0` but `dirty is True`).
12. `test_structural_undo` — rotate then `undo_structural()` restores rotation 0;
    `redo_structural()` re-applies; bytes round-trip (page_count + rotation match).
13. `test_invariants_after_structural` — after a delete+merge sequence, run a
    style + a move + a delete edit on a surviving page and assert the EDITOR_SPEC
    §0 checks still hold: overlap-merge residue-free (`region_ink` at member
    bboxes), non-destructive redaction (a colored cell/border survives), saved
    face is not base-14 tofu, `render_with_edits == saved`.

**Workspace tests** (`tests/test_workspace.py` or folded into `test_pages.py`):

14. `test_workspace_open_dedup` — opening the same path twice returns the same
    index, `count == 1`.
15. `test_workspace_close_active` — open 3, close active middle → active becomes
    the right neighbor; close down to 0 → `active_index == -1`.
16. `test_workspace_move_tab` — reorder keeps `active` pointing at the same doc.

**UI / app tests** (extend `tests/test_app.py` and/or `tests/test_editor.py`,
driving the REAL `MainWindow` headless):

17. `test_tabs_app` — open two fixtures into the window; tab bar shows 2 tabs and
    is visible; switching tabs re-points the canvas (`view.document` identity
    changes) and the sidebar (`sidebar.current_page` resets).
18. `test_sidebar_reorder_app` — synthesize a 3-page temp doc, open it; emit
    `sidebar.reorderRequested(0, 2)`; assert the active doc's page order changed
    and `sidebar` thumbnail count is unchanged; Undo (toolbar) restores order.
19. `test_searchable_font` — Inspector combo is editable; typing "geor" then
    committing "Georgia" emits exactly one `styleEdited({"font_family":
    "Georgia"})`; free-typed junk emits NOTHING and reverts.
20. `test_selection_visible` — after selecting a box, `SelectionOverlay` pen width
    == `theme.SELECTION_OUTLINE_W` and `_HANDLE_PX == theme.HANDLE_PX` (a cheap
    structural assertion that the polish landed).

**Regression gate (MUST stay green, run all four):**
`test_app.py`, `test_font_fidelity.py`, `test_editor_model.py`, `test_editor.py`.

Run line for the new file:
```
QT_QPA_PLATFORM=offscreen \
  /Users/edward/Documents/GitHub/PDFTextEditor/.venv/bin/python tests/test_pages.py
```

---

## 8. Changes vs. additions (numbered)

**ADDITIONS (new files):**
1. `pdftexteditor/workspace.py` — `Workspace` (multi-doc, active index, tabs
   backing). §2.
2. `pdftexteditor/ui/thumbnail_sidebar.py` — `PageThumbnailSidebar`. §5.1.
3. `tests/test_pages.py` (+ optional `tests/test_workspace.py`) — §7.

**CHANGES (existing files):**
4. `pdftexteditor/document.py`:
   - rename `self.doc` → `self.working`; add `@property doc` alias; deep-copy on
     init (§3.0).
   - `render`, `spans`, `page_rotation`, `rotation_matrix`, `page_count`, `close`
     read `self.working`.
   - `save_as` / `render_with_edits` open their fresh copy from
     `self.working.tobytes()` (not `self.path`). §1.2.
   - add `_bake_pending_edits()`, static `_bake_doc(...)`, `_snapshot_for_undo()`,
     `_invalidate_caches()` (rebind engine). §1.3–1.4.
   - add structural methods: `rotate_page`, `delete_page`, `insert_blank_page`,
     `duplicate_page`, `move_page`, `merge`, `extract_pages`, `split`,
     `render_thumbnail`, and `can_undo_structural`/`can_redo_structural`/
     `undo_structural`/`redo_structural`. §3.
5. `pdftexteditor/ui/main_window.py`:
   - `self.workspace`; `document` becomes a property over `workspace.active`.
   - add `DocumentTabBar`, the left Pages dock, a `QMenuBar`, the Organize toolbar
     group, and all structural actions + dialogs + handlers. §5.2/§5.3/§6.
   - add `StructuralCommand`; `_after_structural_op`, `_push_structural_boundary`,
     `_refresh_after_structural_undo`, `_activate_document`, the tab/sidebar
     handlers, `_parse_page_ranges`. §6.
   - `_has_unsaved()` ORs in `workspace.active.dirty`. §6.8.
6. `pdftexteditor/ui/inspector.py`:
   - `InspectorFamily` becomes editable with a `QCompleter` (`MatchContains`,
     case-insensitive), `NoInsert`; exact-match commit rule; emitted dict shape
     UNCHANGED. §5.6.
7. `pdftexteditor/ui/page_view.py`:
   - `_HANDLE_PX = theme.HANDLE_PX`; `SelectionOverlay.paint` two-pass halo+accent
     at `theme.SELECTION_OUTLINE_W`; optional faint selection wash. §5.7.
   - NO change to geometry/hit-test/signals.
8. `pdftexteditor/ui/theme.py`:
   - add `SELECTION_OUTLINE_W`, `SELECTION_OUTLINE_HALO`, `HANDLE_PX`,
     `color_selection_halo()`. §4.2.

**UNCHANGED (do not touch the logic):** `font_engine.py` (303-family list reused
as-is), `fonts.py`, `_apply_page_edits`'s body, `_merge_overlapping`, the 3-tier
resolvers, the redaction flags, the fine-grained `_Command` history. The whole
edit/font/redaction core is reused verbatim; the page layer only changes the
**source document** edits are applied to (working vs. a re-open of path) and adds
structure ops in front of the same pipeline.

---

## 9. Pinned decisions (the things builders must NOT re-decide)

1. **Working document.** One mutable `fitz.Document` per `PDFDocument`
   (`self.working`); reads + the edit pipeline source point at it; `self.path`
   bytes are never mutated. §1.2/§3.0.
2. **Bake-then-mutate (§0.6).** Every structural op bakes staged edits via
   `_apply_page_edits` into `working`, then mutates structure, then clears the
   edit maps. No orphaned edits, WYSIWYG preserved.
3. **Structural undo = bytes-snapshot stack** (`_struct_undo`/`_struct_redo`),
   surfaced to Qt via one `StructuralCommand` per op; the fine-grained stack is
   reset at each structural boundary. §1.5/§6.6.
4. **`move_page(src, dst)` is destination-slot semantics**; translate to fitz with
   `to = dst if dst < src else dst+1`; locked by a permutation unit test. §3.5/§7.
5. **`extract_pages` / `split` are non-mutating** (bake a COPY); `delete_page` /
   `merge` / `move_page` / `rotate` / `insert` / `duplicate` mutate. §3.7/§3.8.
6. **Workspace** owns N docs + active index, de-dups opens by realpath, each tab
   carries its own edits + structural undo. §2/§5.4.
7. **Tab bar hidden at ≤1 doc**; **Pages sidebar** is a left dock; model is the
   reorder source of truth (the list never optimistically reorders). §5.1/§5.2.
8. **Searchable font combo** keeps the EXACT `styleEdited({"font_family":...})`
   contract; only exact-match families commit. §5.6.
9. **Selection visibility** is a theme + paint change only; zero geometry/signal
   impact. §5.7.
10. **The five §0 invariants** plus **§0.6** are HARD and are gated by
    `test_pages.py` + the four existing test files staying green. §0/§7.
