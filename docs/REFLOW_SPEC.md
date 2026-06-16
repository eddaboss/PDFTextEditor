# REFLOW_SPEC — Paragraph grouping, wrap/reflow, continuous scroll, UI redesign

Status: architecture spec for the next build. Written against the live code in
`pdftexteditor/` (document.py, font_engine.py, fonts.py, workspace.py,
ui/page_view.py, ui/inspector.py, ui/thumbnail_sidebar.py, ui/main_window.py,
ui/theme.py) and the four prior specs (BUILD_SPEC, EDITOR_SPEC, PAGES_SPEC).
This spec BUILDS ON those — every symbol it pins exists or extends an existing
one. Section numbers here (§R*) are local to this document.

North star: edit text in an existing PDF so it looks like nothing changed.
Everything below serves "change the words, leave zero trace," for free.

The build has four pillars:

  A. PARAGRAPH GROUPING — consecutive same-paragraph lines become ONE editable box.
  B. WRAP/REFLOW LAYOUT ENGINE — the WYSIWYG keystone; one pure function feeds
     both the on-screen editor/preview AND the saved file.
  C. CONTINUOUS-SCROLL VIEW — vertical scroll of all pages, fit-to-width default.
  D. UI REDESIGN — left tool strip + Format panel; thumbnails move right; clean top bar.

Plus the TEXT TOOLS (find/replace, alignment + spacing, copy/paste-with-format,
auto-match-nearby-style).

---

## §R0. The five non-regression invariants (carry forward verbatim)

These were established in BUILD_SPEC §0 / EDITOR_SPEC §0 / PAGES_SPEC §0. Reflow
must COMPOSE with them, not weaken them. Restated here as the acceptance gate.

1. **SEAMLESS FIDELITY / WYSIWYG.** On-screen == saved file, because
   `render_with_edits` runs the SAME `_apply_page_edits` pipeline as `save_as`.
   For REFLOWED text this means: the wrap layout is computed by ONE pure engine
   (§R2) from the resolved font's metrics + box width, and those exact lines are
   drawn BOTH on screen and on save via per-line `_insert_run` at engine-computed
   origins. We never delegate wrapping to `insert_textbox`. The wrap engine is
   the single source of truth for both paths.

2. **FONT TIERS + `resolve_family`.** Three-tier `resolve` (EMBEDDED / SYSTEM /
   BASE14) reproduces the original face for unchanged-glyph edits; picked
   families route through `resolve_family` (never Tier 1, always embeddable).
   Base-14 floor is always glyph-safe. Identity-H subsets cannot supply new
   glyphs. UNCHANGED.

3. **OVERLAP-MERGE box recognition** (`_merge_overlapping`, `Span.redact_bboxes`).
   Paragraph grouping is an ADDITIONAL pass that runs AFTER `_merge_overlapping`
   and composes with it (§R1.6).

4. **NON-DESTRUCTIVE REDACTION** (`PDF_REDACT_IMAGE_NONE` /
   `PDF_REDACT_LINE_ART_NONE` / `PDF_REDACT_TEXT_REMOVE`). A paragraph box
   redacts ALL its member line bboxes, then redraws the wrapped lines. UNCHANGED.

5. **SHIPPED EDITOR + PAGE MANAGEMENT + UNDO/REDO.** font/size/color/bold-italic,
   add/move/resize/delete; merge/split/rotate/delete/insert/duplicate/reorder,
   thumbnails, multi-doc tabs; generalized + structural undo. All keep working.

---

## §R1. PARAGRAPH GROUPING

### §R1.0 Problem statement, grounded in the fixtures

`tests/fixtures/body_paragraphs.pdf` (probed): each visual line is its OWN
single-line block. PyMuPDF did NOT group them. The page has 12 text blocks =
12 visual lines forming 3 paragraphs:

```
blk0  x0=72.0 y0=86.2   sz=11 Times          "The harbor woke slowly under a thin coastal fog,"
blk1  x0=72.0 y0=104.2  lead=18.0            "boats nudged against their moorings as the tide"
blk2  x0=72.0 y0=122.2  lead=18.0            "wheeled over the breakwater, calling to one anot"
blk3  x0=72.0 y0=140.2  lead=18.0            "while the first dock crews shuffled out with the"
blk4  x0=72.0 y0=168.2  lead=28.0  <-- PARAGRAPH BREAK (gap 28 > 1.4*18)
...
blk8  x0=72.0 y0=250.2  lead=28.0  <-- PARAGRAPH BREAK
...
```

Within a paragraph the baseline-to-baseline leading is exactly 18.0; the
inter-paragraph gap is 28.0. So grouping MUST work ACROSS blocks (not just
within one block), keyed on geometric continuity, and a leading jump (28 vs 18)
is the paragraph boundary signal.

`form_like.pdf` is the negative control: "Field A:" / "Field B:" headings, single
fields, label-next-to-value, a heading at 26pt, body at 11/13pt. None of these
must merge into a paragraph (different size, big gap, list-item shape). Block 3
even has TWO lines of different fonts (Helvetica-Bold "Field B:" + ArialMT
"Pending review") — these must NOT merge (style change).

### §R1.1 Where it runs

New private method `PDFDocument._group_paragraphs(spans: list[Span]) -> list[Span]`,
called at the END of `spans()`, AFTER `_merge_overlapping`:

```python
def spans(self, page_index):
    ...                                   # build raw list 'out' (UNCHANGED)
    merged = self._merge_overlapping(out)        # invariant §R0.3 (UNCHANGED)
    return self._group_paragraphs(merged)        # NEW grouping pass
```

`_merge_overlapping` already attaches `redact_bboxes` to every returned span
(single-member spans get `(bbox,)`). `_group_paragraphs` consumes those merged
spans, so each "line" it sees may itself be an overlap-merged box. The two passes
compose: overlap-merge fuses stacked duplicate runs of ONE line; paragraph
grouping fuses consecutive distinct LINES of one paragraph.

### §R1.2 The grouped paragraph Box: `ParagraphBox`

A NEW frozen dataclass in `document.py`, satisfying the existing `Box` Protocol
(BUILD_SPEC §1.8) so the view's hit-test / selection / overlay machinery treats
it uniformly with `Span` and `NewBox`.

```python
@dataclass(frozen=True)
class ParagraphBox:
    # --- identity / geometry ---
    page_index: int
    bbox: tuple                 # union of all member line bboxes (x0,y0,x1,y1)
    origin: tuple               # baseline (x,y) of the FIRST member line (PDF pts)
    members: tuple              # tuple[Span,...] the constituent lines, in reading order
    # --- resolved paragraph style (from the primary/first member) ---
    text: str                   # member texts joined with a single space per soft break
    size: float                 # the paragraph's body size (from member[0])
    color: tuple                # (r,g,b) 0..1
    font: str                   # rawdict font name of member[0] (for resolve())
    flags: int                  # style bitfield of member[0]
    font_xref: int | None       # member[0].font_xref (3-tier resolve bridge)
    ascender: float             # member[0].ascender (font-relative)
    descender: float            # member[0].descender
    dir: tuple = (1.0, 0.0)     # writing direction (horizontal paragraphs only)
    # --- paragraph layout ---
    leading: float = 0.0        # measured baseline-to-baseline leading (PDF pts)
    alignment: str = "left"     # "left"|"center"|"right"|"justify" (inferred, §R1.5)
    block_index: int = 0        # member[0].block_index (for a stable key)
    line_index: int = 0         # member[0].line_index
    span_index: int = 0         # member[0].span_index
    # --- redaction ---
    redact_bboxes: tuple = ()   # FLATTENED union of every member's redact_rects

    @property
    def redact_rects(self): return self.redact_bboxes or (self.bbox,)
    @property
    def is_horizontal(self): return abs(self.dir[0]-1.0)<1e-3 and abs(self.dir[1])<1e-3
    @property
    def rotation_degrees(self): return 0.0
    @property
    def key(self): return (self.block_index, self.line_index, self.span_index)
    @property
    def global_key(self): return (self.page_index,)+self.key
    @property
    def identity(self): return ("para",)+self.global_key   # NS-prefixed, never collides
    @property
    def is_paragraph(self): return True
```

Notes that PIN behavior:
* **`text`** joins member texts with a single ASCII space at each soft break
  (line breaks inside a paragraph are not semantic; the wrap engine re-derives
  them). Trailing hyphenation (a soft-hyphen `­` or a real `-` at a line end)
  is preserved as-is in v1 — we do NOT de-hyphenate (out of scope; documented).
  Internal ` ` (non-breaking space, which PyMuPDF emits between words) is
  normalized to a regular space in the JOINED text so the wrap engine can break
  on it; the original member spans keep their raw chars (they are only redacted).
* **`redact_bboxes`** is the flattened concatenation
  `tuple(bb for m in members for bb in m.redact_rects)` — every member line's
  every duplicate bbox. Editing the paragraph erases ALL of them (invariant §R0.3/4).
* **`identity`** is `("para", page, blk, line, span)` of `member[0]`. The `"para"`
  namespace prefix guarantees it can never collide with a `Span.global_key`
  (a 4-tuple of ints) or a `NewBox.edit_key` (`(page,"new",id)`), so the view's
  `_box_by_identity` re-find across a reload stays unambiguous.
* A single-line "paragraph" (a heading, a field) is NOT wrapped into a
  `ParagraphBox`; it stays a `Span` (§R1.4 keeps singletons as Spans). This means
  `ParagraphBox` only ever appears for ≥2 grouped lines, so existing single-line
  field/table/heading behavior is byte-for-byte unchanged.

### §R1.3 The grouping algorithm (deterministic)

Input: the overlap-merged span list for one page. Output: a list where runs of
consecutive same-paragraph lines are replaced by ONE `ParagraphBox` and every
other span passes through unchanged.

```
1. CANDIDATES. Keep only HORIZONTAL spans (is_horizontal). Rotated/vertical runs
   never group (they pass straight through). Sort candidates by (y0, x0) — top
   to bottom, then left to right — to get reading order.

2. SEED. Walk the sorted list. Start a new group with the current line L0.

3. EXTEND. For each next line Ln vs the group's LAST line Lprev, append Ln to the
   group iff ALL of the following hold (the paragraph-continuity predicate):

   (a) SAME SIZE.        |Ln.size - L0.size| <= 0.15 * L0.size
   (b) SAME FONT FAMILY. FontEngine._family_norm(Ln.font) == _family_norm(L0.font)
                         AND (Ln.flags & STYLE_BITS) == (L0.flags & STYLE_BITS)
                         where STYLE_BITS = FLAG_BOLD|FLAG_ITALIC|FLAG_SERIF|FLAG_MONO.
                         (A bold/italic change breaks the paragraph — heading vs body.)
   (c) SAME COLOR.       max channel diff <= 0.06 (so a colored callout line splits off).
   (d) CONSISTENT LEADING. gap = Ln.y0 - Lprev.y0  (baseline-to-baseline ≈ top-to-top
                         since sizes match). Let body_lead be the group's leading:
                         the FIRST accepted gap seeds it; subsequent gaps must satisfy
                         0.6*body_lead <= gap <= 1.35*body_lead.
                         A gap > 1.35*body_lead is a NEW paragraph (the 28-vs-18 break).
                         A gap < 0.6*body_lead (overlapping/sub/superscript) does NOT group.
                         Before body_lead is seeded (group has 1 line) accept the first
                         gap only if 0.5*L0.size <= gap <= 2.2*L0.size (sane single-spacing
                         to ~2x leading window), else close the group.
   (e) HORIZONTAL OVERLAP / ALIGNMENT COMPATIBLE (§R1.5 inference shares this):
                         the lines' x-spans must be alignment-consistent. Compute the
                         group's reference left edge Lx = min member x0 and right edge
                         Rx = max member x1. Ln joins iff at least one holds:
                           * left-aligned:  |Ln.x0 - Lx| <= align_tol
                           * right-aligned: |Ln.x1 - Rx| <= align_tol
                           * centered:      |center(Ln) - center(group)| <= align_tol
                         align_tol = max(2.0, 0.5*L0.size) PDF points.
                         A line that satisfies NONE (e.g. an indented list item whose
                         left edge is offset and is not centered/right) does NOT join.
   (f) NO INTERVENING IMAGE/RULE. There is no non-text block or horizontal rule
                         whose y lies strictly between Lprev.y1 and Ln.y0. (v1: we only
                         have text spans here, so this reduces to "no large unexplained
                         vertical gap," already covered by (d). Reserved hook.)

4. CLOSE + EMIT. When Ln fails the predicate, close the current group:
     * size 1  -> emit the single Span UNCHANGED (it is a heading/field/standalone line).
     * size >=2 -> build a ParagraphBox (§R1.2) from the members and emit it.
   Then seed a new group at Ln. Continue.

5. STABLE ORDER. Re-emit results in the original reading order. Non-candidate
   (rotated) spans are spliced back at their reading-order position.
```

Constants live as module-level names so they are tunable + greppable:
`_PARA_SIZE_TOL=0.15`, `_PARA_COLOR_TOL=0.06`, `_PARA_LEAD_LO=0.6`,
`_PARA_LEAD_HI=1.35`, `_PARA_FIRST_GAP_LO=0.5`, `_PARA_FIRST_GAP_HI=2.2`,
`STYLE_BITS = FLAG_BOLD|FLAG_ITALIC|FLAG_SERIF|FLAG_MONO`.

### §R1.4 Why these heuristics, justified against the fixtures

* **body_paragraphs**: all members size 11, family TimesNewRoman, color black,
  left edge 72.0, intra-para gap 18.0, inter-para gap 28.0. (a)-(e) all pass
  within a paragraph; (d) breaks at 28.0 (28 > 1.35*18 = 24.3). Result: 3
  ParagraphBoxes of 4 lines each. EXACTLY the desired outcome.
* **form_like** headings ("Sample Report" 26pt; "Field A:" 13pt bold): each is a
  lone line; even if a body line sits below, (a) size differs (26 vs 11) OR (d)
  the gap is huge OR (b) the bold flag differs. They stay Spans. Block 3's two
  lines fail (b) (Helvetica-Bold vs ArialMT family/flags differ) → stay separate.
* **multi_size** (title 28, subtitle 18, body 13/11, all Arial): consecutive
  lines differ in size at the top (fail (a)); the body section paragraphs group
  normally. Correct.
* A **list** ("• item one / • item two") groups only if the bullets share a left
  edge AND consistent leading; an indented continuation line fails (e) unless it
  aligns — acceptable for v1 (lists become a paragraph box only when they look
  like a justified/flush block, which is visually indistinguishable from a
  paragraph anyway; editing still reflows them coherently).

### §R1.5 Alignment inference (feeds both the box and the Format panel)

For a closed group, infer `alignment` once from the member geometry (used to seed
the wrap engine and the Format panel's alignment control):

```
Lx = min(m.x0), Rx = max(m.x1), W = Rx - Lx   (the paragraph column width)
left_var  = stdev(m.x0 for m in members)
right_var = stdev(m.x1 for m in members)
ctr_var   = stdev((m.x0+m.x1)/2 for m in members)
last = members[-1]
if right_var <= align_tol and left_var > align_tol:        alignment="right"
elif ctr_var <= align_tol and left_var > align_tol and right_var > align_tol:
                                                            alignment="center"
elif left_var <= align_tol and right_var <= align_tol and len(members)>=3 \
     and (Rx - last.x1) <= 0.15*W:                          alignment="justify"
else:                                                        alignment="left"
```

Justify is only inferred when BOTH edges are flush across ≥3 lines and the LAST
line is allowed to be short (the classic justified-block signature). Everything
else is left (the safe default). The body_paragraphs fixture is ragged-right →
"left" (its right edges vary: 355.9 / 368.7 / 372.3 / 355.6). Correct.

`column_width` for the wrap engine is `W = Rx - Lx` (the measured paragraph
column), NOT just `member[0]`'s width — so reflow uses the true block width.

### §R1.6 Composition with `_merge_overlapping` (invariant §R0.3)

`_group_paragraphs` runs on the OUTPUT of `_merge_overlapping`. Each member it
groups is already a clean per-line box carrying its own `redact_rects`. The
ParagraphBox's `redact_bboxes` is the flattened union of member `redact_rects`,
so the overlap-merge semantics are preserved transitively: editing a paragraph
redacts every duplicate fragment of every member line. No change to
`_merge_overlapping` itself.

### §R1.7 Editing identity stability

`ParagraphBox.identity = ("para", page, blk, line, span)` of `member[0]`. Because
`member[0]` is the first line and grouping is deterministic given the page
content, the identity is stable across reloads UNTIL the paragraph's own text is
edited (which changes line bboxes but NOT `member[0]`'s block/line/span key,
since reflow is staged as an `Edit` over the original spans — see §R2.4). After a
SAVE + reload the page is re-extracted fresh; the new ParagraphBox gets a new
`member[0]` key, which is exactly today's behavior for Spans (a fresh object each
reload, re-found by identity). The view's `_box_by_identity` already handles "box
gone after reload → clear selection."

---

## §R2. WRAP / REFLOW LAYOUT ENGINE — the WYSIWYG keystone

### §R2.0 Principle

ONE pure function computes the line layout. It is imported by BOTH the model's
bake path (`_apply_page_edits`, hence `save_as`) AND the view's editor/preview.
We NEVER let PyMuPDF `insert_textbox` wrap, because its wrap algorithm is not
guaranteed to match our on-screen measurement pixel-for-pixel. We measure
ourselves with the SAME `fitz.Font` object that `save_as` draws each line with,
so screen == file by construction.

Verified in the venv (PyMuPDF 1.27.2.3): `fitz.Font.text_length(s, fontsize=k)`
is exactly additive over characters (no kerning):
`text_length("The harbor woke slowly", 11) == sum(text_length(c,11))`. This makes
the wrap deterministic and lets us compute per-character caret advances the same
way the editor does.

### §R2.1 Module + signature

New module `pdftexteditor/reflow.py` (pure, Qt-free, import-light):

```python
from dataclasses import dataclass

@dataclass(frozen=True)
class LaidOutLine:
    text: str            # the substring of this wrapped line (no trailing space)
    origin: tuple        # baseline (x, y) in PDF points where this line is drawn
    width: float         # the line's measured advance at fontsize (PDF points)

@dataclass(frozen=True)
class WrapResult:
    lines: tuple         # tuple[LaidOutLine, ...] in top-to-bottom order
    bbox: tuple          # union (x0,y0,x1,y1) of the laid-out text, PDF points
    leading: float       # baseline-to-baseline used (PDF points)

def wrap_paragraph(
    text: str,
    font: "fitz.Font",          # the resolved face (engine.fitz_font_for(rf))
    fontsize: float,
    box_left: float,            # left edge x0 of the paragraph column (PDF pts)
    first_baseline_y: float,    # y of the FIRST line's baseline (PDF pts)
    column_width: float,        # the wrap width (Rx - Lx), PDF points
    *,
    alignment: str = "left",    # "left"|"center"|"right"|"justify"
    leading: float | None = None,   # baseline-to-baseline; None -> 1.2*fontsize default
    line_spacing: float = 1.0,      # multiplier applied to leading
) -> WrapResult:
    ...
```

### §R2.2 Algorithm (greedy word-wrap, our own measurement)

```
1. METRICS. lead = (leading if leading is not None else font.ascender_descender_leading)
            effective_lead = lead * line_spacing
   Default lead when none given: 1.2 * fontsize (matches typical PDF single-spacing;
   for a grouped ParagraphBox we PASS the measured leading from §R1, so the reflow
   of unchanged text reproduces the original line spacing exactly).
   space_w = font.text_length(" ", fontsize=fontsize)

2. TOKENIZE on whitespace (split on runs of space/ /\t;   already
   normalized to space in ParagraphBox.text). Preserve no leading/trailing blanks.

3. GREEDY FILL. Walk tokens, accumulating the current line. A token fits iff
   (current_advance + (space_w if line non-empty else 0) + token_w) <= column_width.
   token_w = font.text_length(token, fontsize=fontsize).
   When it does not fit, FLUSH the current line and start a new one with the token.
   A single token wider than column_width occupies its own line (NO mid-word break
   in v1 — overflow is allowed for one oversized token, documented; the box width
   was derived from the original which already held the word).

4. PLACE each flushed line:
     baseline_y = first_baseline_y + i * effective_lead       (i = 0-based line index)
     line_advance = sum(token_w) + (n_gaps * space_w)
     x depends on alignment:
        left/justify(non-last): x = box_left
        right:                  x = box_left + (column_width - line_advance)
        center:                 x = box_left + (column_width - line_advance)/2
     For JUSTIFY (all lines except the LAST and except single-token lines):
        extra = (column_width - line_advance) / max(n_gaps, 1)
        the line is emitted as one LaidOutLine whose TEXT is rebuilt with padded
        gaps is NOT possible (text_length is additive, but inter-word stretch is a
        DRAW concern). v1 JUSTIFY: store the per-gap stretch on the line and have
        the insert step draw each WORD separately at computed x (see §R2.5). The
        LaidOutLine for a justified line therefore carries words+xs, not one string.
        -> Represent a justified line as LaidOutLine(text=joined, origin=(box_left,y),
           width=column_width) PLUS an extra field word_origins: tuple[(word, x)].
        Left/center/right lines leave word_origins empty (drawn as one run).

5. BBOX. x0 = box_left, x1 = box_left + column_width (the column), 
   y0 = first_baseline_y - font.ascender*fontsize,
   y1 = last_baseline_y + (-font.descender)*fontsize.
```

`LaidOutLine` gains an optional `word_origins: tuple = ()` field for the justify
case. For left/center/right it stays empty and the line draws as one `insert_text`.

### §R2.3 Metrics source — PINNED and justified

**Use `fitz.Font.text_length` (the model's resolved face), NOT QFontMetricsF, as
the authoritative wrap metric.** Reason: the SAVED file's line widths are produced
by `fitz`'s glyph advances when `insert_text` draws each line. If we wrapped using
Qt's metrics (a different rasterizer/hinting/units) the saved line breaks could
differ from what we measured, breaking invariant §R0.1. By measuring with the
exact `fitz.Font` that `save_as` draws with (`engine.fitz_font_for(rf)`), the
on-screen wrap and the saved wrap are computed from identical numbers.

The on-screen EDITOR still renders with Qt (`ResolvedFont.qfont`), but it does NOT
re-wrap: the editor is fed the SAME `WrapResult.lines` the engine produced (the
editor becomes a multi-line `QGraphicsTextItem` whose explicit `\n`s are placed at
the engine's break points — see §R3.7). So Qt only RASTERIZES our pre-computed
lines; it never decides breaks. Any sub-pixel Qt-vs-fitz advance difference shifts
glyphs within a line by <1px and never changes a break, so the preview tracks the
bake. This is the same fidelity posture the current single-line editor already
relies on (`caret_index_at_scene_x` uses QFontMetricsF only for caret hit-testing,
not for layout).

### §R2.4 How `Edit` carries reflowed paragraph state

`Edit` (document.py) gains THREE optional fields (all default to "unchanged", so
every existing Edit and every test is byte-compatible):

```python
@dataclass
class Edit:
    span: Span | ParagraphBox          # widened: the box being edited
    new_text: str | None = None
    style: StyleOverride = field(default_factory=StyleOverride)
    move: tuple | None = None
    scale: float = 1.0
    deleted: bool = False
    # --- NEW: paragraph reflow payload (None == not a reflow edit) ---
    alignment: str | None = None       # override alignment; None = box's inferred
    line_spacing: float | None = None  # multiplier; None = 1.0
    reflow: bool = False               # True when this box wraps to its column width
```

`is_noop` gains: `and self.alignment is None and self.line_spacing is None`. The
`reflow` flag is set True for any `Edit` whose `span` is a `ParagraphBox` (so the
bake takes the wrap path) — it is derived, set when the Edit is created for a
paragraph, not user-toggled.

The paragraph's column geometry (left edge, width, first baseline, leading,
resolved style) all live ON the `ParagraphBox` the `Edit` points at, so the bake
reconstructs the layout from `edit.span` + the new text + any alignment/spacing
override. No extra plumbing.

### §R2.5 Bake: `_apply_page_edits` / `render_with_edits` insert wrapped lines

`_apply_page_edits` (the shared pipeline) is extended at step 4 (the reinsert
loop). Today it draws ONE run per edit via `_insert_run(origin, text, ...)`. The
change is localized to a single branch:

```python
for edit in edits:
    if edit.deleted: continue
    box = edit.span
    text = edit.effective_text(box)
    rf = self._resolve_for_edit(engine, page_index, box, edit, text)
    fontname = self._register_resolved(engine, page, rf, registered)
    if getattr(box, "is_paragraph", False) or edit.reflow:
        self._insert_paragraph(engine, page, box, edit, text, rf, fontname)
    else:
        self._insert_run(page, self._effective_origin(box, edit), text,
                         self._effective_size(box, edit), fontname,
                         self._effective_color(box, edit), box.dir)
```

`_insert_paragraph` (NEW) is the ONLY new draw code, used identically by the bake,
`save_as`, and `_bake_doc` (so all three stay one pipeline):

```python
@staticmethod
def _insert_paragraph(engine, page, box, edit, text, rf, fontname):
    font = engine.fitz_font_for(rf)                  # SAME face as the metric
    size = PDFDocument._effective_size(box, edit)
    align = edit.alignment or box.alignment
    spacing = edit.line_spacing or 1.0
    dx, dy = edit.move or (0.0, 0.0)
    left = box.bbox[0] + dx
    first_y = box.origin[1] + dy
    width = box.bbox[2] - box.bbox[0]
    result = wrap_paragraph(text, font, size, left, first_y, width,
                            alignment=align, leading=box.leading, line_spacing=spacing)
    color = PDFDocument._effective_color(box, edit)
    for ln in result.lines:
        if ln.word_origins:                          # justified line: per-word draw
            for word, wx in ln.word_origins:
                page.insert_text(fitz.Point(wx, ln.origin[1]), word,
                                 fontsize=size, fontname=fontname, color=color)
        else:
            page.insert_text(fitz.Point(*ln.origin), ln.text,
                             fontsize=size, fontname=fontname, color=color)
```

REDACTION is unchanged: step 1/3 already collect `box.redact_rects` (= every
member line bbox for a ParagraphBox, §R1.2) and `apply_redactions` removes them
before this reinsert. So: redact all member bboxes, then draw the wrapped lines.

The `effective_bbox` for a paragraph (used by the selection overlay) is recomputed
from `WrapResult.bbox` (more/fewer lines change the height) — `effective_bbox`
gains a branch: for a `ParagraphBox` Edit it calls `wrap_paragraph` and returns
`result.bbox` translated by `move`. This keeps the outline + handles hugging the
reflowed ink (overlay == baked == saved, EDITOR_SPEC §3.7).

### §R2.6 Round-trip guarantee for UNCHANGED paragraphs (no-trace test)

When a ParagraphBox is selected but NOT edited, no `Edit` exists for it → the
bake never touches it → the ORIGINAL rasterized lines render. Only an actual text
/alignment/spacing edit triggers `_insert_paragraph`. Even then, if the new text
equals the original joined text and the box's measured `leading` is passed, the
wrap reproduces the original line breaks within sub-pixel tolerance (greedy fill
on the same widths that produced the original breaks). This is the "zero trace"
property for the central use case (swap a date inside a paragraph → the rest of
the paragraph re-lays identically).

---

## §R3. CONTINUOUS-SCROLL VIEW

### §R3.0 Decision: extend `PageView` in place (single class), not a new class

`PageView` is deeply wired (main_window connects ~10 signals + calls
`set_page`/`set_zoom`/`reload`/`repaint_box`/`select_box`/`begin_add_box_edit`/
`current_selection`/`set_add_style_provider`). A parallel `ContinuousPageView`
would duplicate all of it. Instead we REWRITE `PageView`'s scene-layout layer to
host ALL pages stacked vertically, keeping the public signal/method surface
IDENTICAL so `main_window` drops in unchanged. The class keeps its name.

### §R3.1 Scene model — all pages, one scene

```
The scene lays out every page top-to-bottom:

   page 0 sheet at y = GAP
   page 1 sheet at y = GAP + h0*z + GAP
   page k sheet at y = GAP + sum_{i<k}(hi*z + GAP)

A per-page record holds everything the old single-page state held, indexed by page:

class _PageLayer:
    page_index: int
    y_top: float                 # scene y of this page's sheet top (the page OFFSET)
    pt_size: tuple               # (w,h) in PDF points
    rotation: int
    rotation_matrix: fitz.Matrix
    image: QImage | None         # lazily rendered pixmap (None until visible)
    pixmap_item, sheet_item, shadow_item: QGraphicsItem | None
    hotspots: list[SpanHotspot]
    boxes: list                  # Span/NewBox/ParagraphBox for this page
    rendered: bool               # whether the pixmap is currently materialized

self._layers: list[_PageLayer]   # one per page, built once per (re)load
self._page_gap = theme.PAGE_GAP  # NEW theme token, ~16 px device-independent
```

All geometry math gains a per-page Y offset. The existing helpers become
page-relative:

```python
def _scene_point(self, page_index, x, y):    # ADD page_index
    dx, dy = self._display_point(page_index, x, y)
    z = self._zoom
    layer = self._layers[page_index]
    return QPointF(theme.SHEET_MARGIN + dx*z, layer.y_top + dy*z)

def _pdf_point(self, page_index, scene):     # ADD page_index (the inverse)
    ...
```

Every call site that currently uses `self._page_index` for the active page now
passes the box's OWN `box.page_index` (every Box carries it). Hotspots/handles
already hold their box, so they know their page; the SelectionOverlay reads
`box.page_index`. This is the bulk of the mechanical rewrite, and it is purely
"thread page_index through the geometry helpers."

### §R3.2 Lazy rendering (visible pages + buffer)

```
On scroll (verticalScrollBar.valueChanged) and on resize, compute the VISIBLE
scene rect (mapToScene(viewport().rect())). For each _PageLayer:

   visible = layer rect intersects (visible_rect inflated by BUFFER_PAGES * avg_h)
   if visible and not layer.rendered:   _materialize_page(layer)
   if not visible and layer.rendered and beyond a 2-page evict margin:
                                        _dematerialize_page(layer)

BUFFER_PAGES = 1 (one page above + below the viewport stays hot).
```

`_materialize_page` does exactly what today's `reload` does for ONE page: render
via `render_with_edits(page_index, z*dpr)`, build the QImage/sheet/shadow/pixmap
items at `layer.y_top`, build hotspots from `spans()+new_boxes()` (now also
ParagraphBoxes), add to the scene. `_dematerialize_page` removes those scene
items and frees `layer.image` but keeps the layer record (so scroll position +
total scene height are stable). Sheet+shadow for ALL pages can stay cheap (thin
rects) or also be lazy; pin: sheets/shadows are lazy too, only the page strip
outline (a 1px placeholder rect at `y_top`) persists so the scrollbar extent is
correct before render.

Total scene rect height is computed up front from all `pt_size`s (one cheap
`page_rotation`+`rect` read per page at load), so the scrollbar is correct
immediately and lazy rendering only fills pixmaps.

### §R3.3 Fit-to-width default

`self._zoom_mode` defaults to `"fit_width"`. `_apply_fit_zoom` computes the zoom
from the WIDEST page's PDF width (so no page overflows horizontally):
`z = avail_w / max(layer.pt_size[0] for layer)`. On resize, re-fit and re-layout
(recompute `y_top`s, reposition materialized items). `Actual Size`/preset zooms
switch `_zoom_mode="fixed"` as today.

### §R3.4 "Current page" derivation

The active page = the page whose sheet rect contains the viewport CENTER (nearest
center on ties/gaps):

```python
def _current_page_from_scroll(self):
    center_scene = self.mapToScene(self.viewport().rect().center())
    best, best_d = 0, inf
    for layer in self._layers:
        page_top = layer.y_top
        page_bot = layer.y_top + layer.pt_size[1]*self._zoom
        if page_top <= center_scene.y() <= page_bot:
            return layer.page_index
        d = min(abs(center_scene.y()-page_top), abs(center_scene.y()-page_bot))
        if d < best_d: best, best_d = layer.page_index, d
    return best
```

On scroll, if this differs from `self._page_index`, update it and emit the
EXISTING `pageChanged(page_index)` signal (debounced via a 0-timer to coalesce
scroll storms). `main_window._on_page_changed` already updates the page indicator
+ asks the sidebar to highlight — unchanged. `set_page(i)` now SCROLLS so page i's
top aligns near the viewport top (`ensureVisible` on `layer.y_top`), instead of
swapping the rendered page. Thumbnail click → `set_page` → smooth scroll to that
page. Both directions stay in sync.

### §R3.5 Selection / editor / drag in scene coords offset by page Y

* Hotspots and handles already live at absolute scene positions; building them at
  `layer.y_top + dy*z` is the only change. Press routing (`_on_box_press`,
  `_on_handle_press`, `_on_box_double_click`) is unchanged — they receive
  `scenePos` and the hotspot's box, which carries its page.
* `begin_edit` mounts the inline editor at the box's scene baseline (now via the
  page-aware `_scene_point(box.page_index, *origin)`). The white cover, the
  background sampling (`_background_color_for` reads `layer.image` for the box's
  page), and `_place_text_item` all take `box.page_index`.
* Move/resize drags compute deltas via the page-aware `_pdf_point(page_index, …)`.
  A drag stays on the box's own page (cross-page drags are out of scope v1).
* `_box_by_identity` searches across ALL materialized layers' boxes (so a
  selection on a non-visible page that scrolls into view re-binds). If the
  selected box's page is dematerialized, the overlay is hidden and re-shown when
  the page re-materializes (handled in `_materialize_page`: if `_selection` is on
  this page, reinstall the overlay).

### §R3.6 Baked per-page rendering preserved

`_materialize_page` calls `render_with_edits(page_index, z*dpr)` — the SAME baked
pipeline. So WYSIWYG holds per page; nothing about the save/render contract
changes. `repaint_box(box)` re-materializes ONLY `box.page_index`'s layer (find
its layer, dematerialize+materialize it) instead of a whole-view reload, which is
also a perf win. `reload()` becomes "rebuild all layers + re-fit + lazy-render
visible," used on document set / structural op.

### §R3.7 The inline editor for a ParagraphBox (multi-line)

`InlineRunEditor` already is a `QGraphicsTextItem` (which natively supports
multi-line). For a paragraph:
* It is fed the joined paragraph text and set to a FIXED text width =
  `column_width * zoom` via `setTextWidth(...)`, so Qt soft-wraps the visible text
  to the same column. Enter/Return still COMMITS (single-line behavior is kept;
  paragraphs do not insert hard newlines — wrapping is automatic), matching the
  requirement "editing a paragraph box rewraps to the box width."
* On commit, the typed plain text (with Qt's soft wraps stripped to spaces) is the
  new paragraph `text`; the model stages it as a reflow `Edit`, and the bake
  re-wraps via `wrap_paragraph` (the authority). The editor's Qt soft-wrap is
  cosmetic only; the bake decides final breaks. Sub-pixel divergence is invisible
  and never affects saved breaks (§R2.3).
* Caret hit-testing for a click uses `QGraphicsTextItem`'s native
  `document().documentLayout().hitTest(...)` for multi-line, replacing the
  single-line `caret_index_at_scene_x` (kept for single-line Spans/NewBoxes).

### §R3.8 Public API + signals (drop-in for MainWindow) — UNCHANGED surface

Signals (all already connected in `main_window._wire_view`): `editCommitted`,
`editCancelled`, `editStarted`, `editFinished`, `pageChanged`, `zoomChanged`,
`selectionChanged`, `boxAdded`, `styleApplied`, `geometryChanged`, `boxDeleted`,
`modeChanged`, `boxCommandRequested`. Kept identical.

Methods kept identical (signature-compatible): `set_document`, `clear_document`,
`set_page`, `page_index` (property → current page from scroll), `set_zoom`,
`set_zoom_mode`, `zoom`, `reload`, `repaint_box`, `repaint_span`, `select_box`,
`clear_selection`, `current_selection`, `current_mode`, `enter_add_text_mode`,
`exit_add_text_mode`, `apply_style`, `delete_selected`, `begin_add_box_edit`,
`set_add_style_provider`, `begin_edit`, `commit_edit`, `cancel_edit`.

NEW (additive, optional for the window): `scroll_to_page(i)` (alias used by
thumbnail click), `visible_pages() -> list[int]`. These do not break the window.

---

## §R4. UI REDESIGN — left tool strip + Format panel; thumbnails right; clean top bar

### §R4.0 Dock arrangement (QMainWindow)

```
            +--------------------------------------------------+
   top bar  |  TopBar: Open | Save▾ | Undo Redo | Find | zoom  |   (slim, essential only)
            +------+-----------------------------------+-------+
            | LEFT |                                   | RIGHT |
            | TOOL |        Continuous PageView        | Pages |
            | +FMT |        (fit-to-width scroll)      | thumbs|
            +------+-----------------------------------+-------+
   status   |  filename • dirty • edits        chip   zoom %   |
            +--------------------------------------------------+
```

* **LEFT dock** (`LeftToolDock`, replaces today's right Format dock position):
  a `QDockWidget` on `Qt.LeftDockWidgetArea`, `NoDockWidgetFeatures` (fixed). Its
  widget is a new `LeftPanel` = a vertical TOOL STRIP (icon buttons) at the top +
  the existing `Inspector` (Format panel) below it. Width ~300px.
* **RIGHT dock** (`PagesDock`): the existing `PageThumbnailSidebar`, MOVED from
  `Qt.LeftDockWidgetArea` to `Qt.RightDockWidgetArea`. Closable (as today). The
  ONLY change in `_build_pages_dock` is `setAllowedAreas(Qt.RightDockWidgetArea)`
  and `addDockWidget(Qt.RightDockWidgetArea, dock)`.
* **TOP BAR**: the existing `QToolBar`, slimmed to essential actions (§R4.2).
* **CENTER**: the `CanvasContainer` hosting the continuous `PageView`. Unchanged
  container; the view inside is the §R3 rewrite.

### §R4.1 `LeftPanel` = tool strip + Format

A new `QWidget` (`ui/left_panel.py`) laid out vertically:

```
class LeftPanel(QWidget):
    toolSelected = Signal(str)        # "select"|"text_edit"|"add_text"|"find"
    def __init__(self, inspector: Inspector): ...
```

* **Tool strip** (top): a row/grid of checkable `QToolButton`s in an exclusive
  `QButtonGroup` (Adobe-style), each a tool/mode:
  - `Select`     → view default (MODE_SELECT)
  - `Text Edit`  → arms double-click-to-edit affordance (already the default;
                   this is a visual mode indicator that biases single-click-into-edit)
  - `Add Text`   → drives `view.enter_add_text_mode` (replaces the toolbar toggle)
  - `Find`       → opens the Find & Replace panel (§R5.1)
  The strip uses the existing `make_icon` factory (add `find`, `select`, `text`
  drawers). Selecting a tool emits `toolSelected(name)`; the window maps it to the
  view's mode calls (the same `enter_add_text_mode`/`exit_add_text_mode` it calls
  today, plus a no-op for select/text_edit).
* **Format panel** (below the strip): the EXISTING `Inspector` widget, reparented
  here unchanged. It still reflects the selection via `set_target` and writes back
  via `styleEdited`. Alignment + line-spacing controls are ADDED to the Inspector
  (§R5.2), shown only when a `ParagraphBox` is selected.

`main_window._build_inspector_dock` is replaced by `_build_left_dock`, which
constructs `LeftPanel(self.inspector)` and docks it left. The Add-Text toolbar
button + action stay (shortcut "T") but ALSO reflect the tool strip's state (kept
in sync like today's `add_text_button`).

### §R4.2 Clean top bar

Keep ONLY: Open, Save (split with Save As), Undo, Redo, a Find button, a flexible
spacer, page indicator (current/total, now driven by scroll position), zoom
out/%/in. MOVE the Organize/page-management actions entirely into the menu bar's
Pages menu (they already exist there) and the right thumbnail dock's context menu
(already there) — so the top bar is not crowded. The Add Text / Delete buttons
move to the LEFT tool strip (Add Text) and remain on the Delete shortcut +
context. Fixes the display bugs by reducing the bar to a single clean row of
equal-weight tool buttons (the icon-weight normalization already done in
`make_icon` carries over).

### §R4.3 Fit-to-width default

`PageView._zoom_mode = "fit_width"` by default (§R3.3); the window's existing
`fit_width`/`fit_page`/`set_zoom` actions are unchanged and just call into the
view. The zoom indicator reads the resolved numeric zoom as today.

---

## §R5. TEXT TOOLS

### §R5.1 Find & Replace (cross-page, staged replace)

New `ui/find_panel.py` `FindReplacePanel(QWidget)` shown in the LEFT panel (or a
slim drop-down under the top bar) when the Find tool is active:

* Controls: find field, replace field, Match case, Whole word, "Find All",
  "Replace", "Replace All", a result count, prev/next nav.
* Search runs on the MODEL, cross-page, over the CURRENT staged text of every box:
  ```python
  # document.py NEW:
  def find_all(self, query, *, match_case=False, whole_word=False) -> list[Match]
  # Match = (page_index, box_identity, start, end, context)
  ```
  It iterates `spans(page)+new_boxes(page)` for every page (paragraph text is the
  joined paragraph string, so a match can span the original line breaks — exactly
  what the user wants), reading `staged_text(page, box)`.
* "Find All" populates a results list; selecting a result scrolls the continuous
  view to that page (`view.scroll_to_page`) and selects+highlights the box.
* Replace STAGES an edit through the normal command route: it builds the box's new
  staged text (`staged_text` with the match span replaced) and calls
  `view.editCommitted`-equivalent → one `EditRunCommand`/`BoxCommand` per box, so
  every replace is a normal undo step and rewraps paragraphs through the bake.
  "Replace All" pushes them as a `QUndoCommand` macro (begin/end macro) so it
  undoes as one step.
* No new save path: replacements flow through the existing edit pipeline.

### §R5.2 Alignment + line-spacing controls (feed the layout engine)

Add to `Inspector` (shown only when the selection is a `ParagraphBox`):
* `InspectorAlign` — 4 exclusive toggle buttons (left/center/right/justify).
  Emits `styleEdited({"alignment": "left"|...})`.
* `InspectorLineSpacing` — a `QDoubleSpinBox` (0.8–3.0, ×). Emits
  `styleEdited({"line_spacing": value})`.
The window's `_on_style_edited` already forwards the whole dict to
`view.apply_style`. `apply_style` and `document.set_style` gain handling for the
`alignment` / `line_spacing` keys: they set `Edit.alignment` / `Edit.line_spacing`
(NOT `StyleOverride`, which is glyph style). The bake reads them in
`_insert_paragraph` (§R2.5). For a non-paragraph selection these controls are
hidden, so they never apply to a single-line Span.

`set_target` shows/hides these controls based on `getattr(box, "is_paragraph",
False)` and seeds them from `effective_style` (extended to return `alignment` /
`line_spacing` for a paragraph).

### §R5.3 Copy / paste WITH formatting

* The view gains `copy_selection()` / `paste()` (Cmd+C / Cmd+V at view level when
  a box is selected and no editor is open). Copy serializes the selected box's
  effective style + text into a small in-process clipboard payload
  (`{text, font_family, size, color, bold, italic, alignment, line_spacing}`) AND
  the plain text onto the system `QClipboard` (so paste into other apps works).
* Paste creates a `NewBox` (via the existing `add_box` command path) at a default
  offset from the copied box, carrying the copied style — so it pastes "with
  formatting." If an inline editor is OPEN, paste inserts plain text at the caret
  (native `QGraphicsTextItem` behavior).
* No model changes beyond a tiny `_clipboard` dataclass on the view; reuses
  `boxCommandRequested("add", …)`.

### §R5.4 Auto-match nearby style (new/edited text inherits surroundings)

When the Add-Text tool places a NewBox, instead of the inspector's current values
ALONE, the view first asks the model for the nearest existing text style at the
click point:

```python
# document.py NEW:
def style_near(self, page_index, point) -> dict | None:
    # the effective_style of the span/paragraph whose bbox is nearest 'point'
    # within a radius (e.g. 2 line-heights); None if nothing close.
```

`_add_style_defaults` becomes: `style_near(page, origin) or add_style_provider()`.
So a box added in a paragraph column inherits that paragraph's font/size/color;
one added in blank space falls back to the inspector defaults (today's behavior).
The inspector's controls still override after creation. This is the "auto-match
the style of nearby text" requirement, scoped to add (and, optionally, to a fresh
glyph edit where the resolver already reproduces the source style).

---

## §R6. Changes vs Additions (numbered)

ADDITIONS (new code, no behavior change to existing paths until used):
1. `pdftexteditor/reflow.py` — `LaidOutLine`, `WrapResult`, `wrap_paragraph` (§R2).
2. `document.ParagraphBox` dataclass (§R1.2).
3. `document._group_paragraphs` + grouping constants (§R1.3).
4. `document._insert_paragraph` (§R2.5).
5. `document.find_all`, `document.style_near`, `document.Match` (§R5.1/§R5.4).
6. `ui/left_panel.py` `LeftPanel` (§R4.1).
7. `ui/find_panel.py` `FindReplacePanel` (§R5.1).
8. `Inspector` alignment + line-spacing controls (§R5.2).
9. `PageView.copy_selection`/`paste`/`_clipboard`, `scroll_to_page`,
   `visible_pages` (§R5.3/§R3.8).
10. `theme.PAGE_GAP` token (§R3.1).
11. New icon drawers: `find`, `select`, `text`, `align_*`, `line_spacing` (§R4.1).

CHANGES (modify existing code; each keeps tests green):
12. `document.spans` — append `_group_paragraphs` after `_merge_overlapping` (§R1.1).
13. `document.Edit` — add `alignment`/`line_spacing`/`reflow` fields; update
    `is_noop` (§R2.4).
14. `document._apply_page_edits` / `_apply_page_edits_for` — branch to
    `_insert_paragraph` for paragraph/reflow edits (§R2.5).
15. `document.effective_bbox` / `effective_style` — paragraph-aware branches
    (§R2.5/§R5.2).
16. `document.set_style` / `PageView.apply_style` — route `alignment`/
    `line_spacing` to `Edit`, not `StyleOverride` (§R5.2).
17. `document.add_box` style source — `style_near` first (§R5.4).
18. `ui/page_view.py` — continuous-scroll rewrite: `_PageLayer`, per-page Y in all
    geometry helpers, lazy materialize/dematerialize, scroll-driven current page,
    multi-line paragraph editor; SAME public signal/method surface (§R3).
19. `ui/main_window.py` — `_build_left_dock` (tool strip + Inspector left),
    move Pages dock right, slim top bar, wire `LeftPanel.toolSelected` and the
    Find panel; everything else unchanged (§R4).
20. `ui/inspector.py` — host in `LeftPanel`; add the paragraph controls (§R5.2).
21. `ui/thumbnail_sidebar.py` — unchanged code; only its dock AREA changes (§R4.0).

NOT CHANGED (explicit): `font_engine.py`, `fonts.py`, `workspace.py`, the
structural page ops, the save/redaction pipeline shape, undo/redo command classes
(`_ModelCommand`/`BoxCommand`/`EditRunCommand`/`StructuralCommand`) — paragraph
edits flow through the existing `EditRunCommand`/`BoxCommand` unchanged.

---

## §R7. Headless verification plan (must stay green; new checks added)

Run everything with the venv python + `QT_QPA_PLATFORM=offscreen`:
`/Users/edward/Documents/GitHub/PDFTextEditor/.venv/bin/python`.

REGRESSION (must remain green, unchanged): `tests/test_app.py`,
`tests/test_font_fidelity.py`, `tests/test_editor_model.py`, `tests/test_editor.py`,
`tests/test_pages.py`.

NEW checks (add to a `tests/test_reflow.py`, mirroring the existing harness
style — a `failures` list, `check()` helper, exit nonzero on any failure):

1. **Grouping correctness.** On `body_paragraphs.pdf`,
   `len([b for b in doc.spans(0) if getattr(b,'is_paragraph',False)]) == 3`, each
   with 4 members; on `form_like.pdf`, ZERO ParagraphBoxes (all fields/headings
   stay Spans); block 3's two-font lines never merge.

2. **Wrap engine purity.** `wrap_paragraph` on a known string + `fitz.Font('helv')`
   produces breaks identical to a hand-computed greedy fill; widths additive;
   `result.bbox` matches the union of line bboxes.

3. **WYSIWYG for WRAPPED text (the keystone, mirrors `wysiwyg_match`).** Edit a
   ParagraphBox's text (e.g. replace a word so the paragraph grows by a line),
   `save_as`, then compare whole-page ink of `render_with_edits(0, scale)` vs the
   saved file with `wysiwyg_match(doc, out) < 0.02`. Repeat for SHRINK (fewer
   lines), for each alignment (left/center/right/justify), and for a line-spacing
   change. This is the explicit pixel check the build hinges on.

4. **No-trace round-trip.** Replace a single word inside a paragraph with a
   same-width word; assert the OTHER lines' baselines/breaks are unchanged
   (compare `wrap_paragraph` output line origins before/after for the untouched
   tail) — proves "leave zero trace."

5. **Reflow reflows.** A longer replacement yields MORE `WrapResult.lines`; a
   shorter one yields FEWER; text never overflows `column_width` (each non-oversized
   line `width <= column_width + 0.5pt`).

6. **Composition with overlap-merge + redaction.** A paragraph edit's
   `redact_rects` equals the flattened union of all member `redact_rects`; after
   save, the original member ink is gone (region_ink ~0 outside the redrawn lines)
   and non-destructive flags held (images/vector rules intact — reuse the
   `test_editor_model` redaction assertions).

7. **Continuous view.** In a live `MainWindow` on a multi-page fixture
   (`three_page.pdf`): default zoom mode is `fit_width`; the scene height spans all
   pages; scrolling to the bottom updates `view.page_index` to the last page and
   emits `pageChanged`; clicking a thumbnail scrolls the view (its center page ==
   clicked); lazy render materializes only visible (+buffer) pages
   (`len(view.visible_pages()) < page_count` on a tall doc). Selection on page 2
   survives a scroll away and back.

8. **UI layout.** Headless `MainWindow`: the Format/Inspector lives in a LEFT dock,
   the thumbnail sidebar in a RIGHT dock; the Add-Text tool is in the left strip
   and still drives `enter_add_text_mode`; the top bar contains only the slim
   action set.

9. **Text tools.** `find_all("the")` returns cross-page matches; "Replace All"
   stages one undoable macro and rewraps affected paragraphs (WYSIWYG check on the
   result); copy a styled box → paste creates a NewBox with the same effective
   style; `style_near(page, point_in_paragraph)` returns that paragraph's style.

10. **Undo/redo + structural still green** across paragraph edits interleaved with
    style/move/resize/delete/add and a page op (extend `test_mixed_history` shape):
    a paragraph reflow edit undoes/redoes on the same single history and survives a
    `rotate_page`/`move_page` bake.

Acceptance: ALL five legacy suites green AND all ten new checks pass, with the
§R0 invariants intact — most importantly check #3 (WYSIWYG for wrapped text)
under `< 0.02` whole-page ink divergence.
