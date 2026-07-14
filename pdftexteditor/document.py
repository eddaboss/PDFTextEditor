"""PDF model layer: load, render, extract styled text spans, stage edits, save.

The display document is never mutated. Edits are staged in memory keyed by span
identity, with a full undo/redo history, and only written out when the user
saves -- against a FRESH copy loaded from disk. Write-back is true removal
(redaction of the original bbox) followed by reinsertion of the new text at the
original baseline.

The reinsertion font is NOT a blind base-14 substitute. Every reinsertion routes
through ``FontEngine.resolve`` (see ``font_engine.py``), which reproduces the
document's ORIGINAL embedded typeface whenever it is glyph-safe, falls back to a
full system font of the same family, and only drops to base-14 as a last resort.
The live editor (PageView) and this save path call the SAME resolver, so what
the user sees on screen is what lands in the file (the fidelity contract,
BUILD_SPEC §3 / §9).
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import itertools
import math
import os
import re
import tempfile
from dataclasses import dataclass, field, replace
from typing import Protocol, runtime_checkable

import fitz

from . import doctools
from .debuglog import log as dlog
from .font_engine import (
    TIER_BASE14,
    TIER_EMBEDDED,
    TIER_SYSTEM,
    FontEngine,
    ResolvedFont,
    face_bytes,
)
from .fonts import FLAG_BOLD, FLAG_ITALIC, FLAG_MONO, FLAG_SERIF
from .reflow import wrap_paragraph, wrap_rich

# Geometry floors for resize (BUILD_SPEC §6): never let a box shrink to a
# degenerate size, and clamp the absolute resize multiplier.
_MIN_FONT_SIZE = 2.0          # points
_MIN_RESIZE_SCALE = 0.05      # absolute multiplier floor
_MIN_FRAME_WIDTH = 12.0       # points: a resized text frame never collapses
# Cap the structural-undo depth (PAGES_SPEC §1.5). Each entry is a full
# working.tobytes() snapshot (~1.5 MB on the fixtures), so an uncapped stack
# grows memory unboundedly across a long page-management session. When the cap
# is exceeded the OLDEST snapshot is dropped; the window drops the matching Qt
# command in lockstep so the visible Undo count stays in sync.
_STRUCT_UNDO_CAP = 30

# PDF info-dictionary keys (doc-tools M1). ``insert_pdf`` copies PAGES only --
# it silently DROPS the source info dict and the outline (probe-verified on
# PyMuPDF 1.27.2.3) -- so the constructor re-carries both onto the working
# copy. The first four are the user-editable Description fields in the
# Properties dialog; the rest ride along so a save never strips what the
# original file declared. ``format``/``encryption`` are derived by fitz, not
# settable, and are kept separately (``pdf_version``).
_META_KEYS = ("title", "author", "subject", "keywords",
              "creator", "producer", "creationDate", "modDate")
_EDITABLE_META_KEYS = ("title", "author", "subject", "keywords")

# Crop floor (doc-tools M3): pages where the clamped crop box falls below this
# on either side are skipped (a sub-36pt page is unusable and Acrobat applies
# the same minimum); ``crop_pages`` raises when EVERY page is skipped.
_MIN_CROP_PT = 36.0

# Security (doc-tools M4). The encryption selectors are re-exported so the UI
# layer drives ``set_security`` without importing fitz. Granular permission
# checkboxes are de-scoped: an encrypted save always grants every permission
# bit -- the password protects OPENING the file, which is the actual need.
PDF_ENCRYPT_AES_256 = fitz.PDF_ENCRYPT_AES_256
PDF_ENCRYPT_NONE = fitz.PDF_ENCRYPT_NONE
_PDF_PERM_ALL = (fitz.PDF_PERM_ACCESSIBILITY | fitz.PDF_PERM_ANNOTATE
                 | fitz.PDF_PERM_ASSEMBLE | fitz.PDF_PERM_COPY
                 | fitz.PDF_PERM_FORM | fitz.PDF_PERM_MODIFY
                 | fitz.PDF_PERM_PRINT | fitz.PDF_PERM_PRINT_HQ)
# The ``Document.save`` encryption kwargs ``set_security`` may stage.
_SECURITY_KEYS = ("encryption", "user_pw", "owner_pw", "permissions")


class PasswordRequired(ValueError):
    """Raised by ``PDFDocument`` when ``path`` is password protected and no
    (or a wrong) password was supplied. The gate fires BEFORE any content
    access: touching an unauthenticated handle (metadata, ``insert_pdf``)
    poisons MuPDF's decryption caches, so even a later successful
    ``authenticate`` on the same handle yields a corrupt copy
    (probe-verified "aes padding out of range" / zlib stream errors). The UI
    catches this and re-tries with a password from its injectable provider."""

# --- Paragraph-grouping constants (REFLOW_SPEC §R1.3), module-level so they are
# tunable + greppable. The grouping pass runs AFTER _merge_overlapping and fuses
# consecutive same-paragraph LINES into one ParagraphBox.
_PARA_SIZE_TOL = 0.15        # |size diff| <= this * primary size
_PARA_COLOR_TOL = 0.06       # max per-channel color diff
_PARA_LEAD_LO = 0.6          # gap >= this * body_lead to stay in the paragraph
_PARA_LEAD_HI = 1.35         # gap <= this * body_lead; above is a NEW paragraph
_PARA_FIRST_GAP_LO = 0.5     # before body_lead is seeded: gap >= this * size
_PARA_FIRST_GAP_HI = 2.2     # ...and gap <= this * size (single..~2x leading)
# A bold/italic/serif/mono change breaks the paragraph (heading vs body, callout).
_STYLE_BITS = FLAG_BOLD | FLAG_ITALIC | FLAG_SERIF | FLAG_MONO

# Space-variant characters that a user types/reads as a plain space. PDF text
# extraction frequently yields a NO-BREAK SPACE (U+00A0) where the visible glyph
# is an ordinary gap, plus thin / figure / narrow-NBSP variants. Find/Replace
# normalizes these to ASCII space on BOTH the haystack and the query so a search
# for "Cleared 4821" matches "Cleared\xa04821" regardless of whether the text was
# grouped into a ParagraphBox (which already normalizes NBSP) or left as a single
# Span (REFLOW_SPEC §R5.1). Every entry is a SINGLE code point that maps to a
# SINGLE space, so match offsets line up 1:1 with the original string and the
# staged replacement still targets the right characters.
_SPACE_VARIANTS = "    ⁠"
_SPACE_NORMALIZE = {ord(c): " " for c in _SPACE_VARIANTS}


def _normalize_spaces(text: str) -> str:
    """Collapse single-code-point space variants (NBSP etc.) to ASCII space
    without changing string length, so search offsets map 1:1 to the source."""
    return text.translate(_SPACE_NORMALIZE)


# A MARKERLESS list whose marker is part of the same span (a numbered or lettered
# enumeration like "1. ", "a) ", or an inline bullet "- ") has no separate marker
# glyph for the row-shared / style-mismatch heuristic to catch, so the items
# would wrongly fuse into one reflowing block. A line that BEGINS with such a
# prefix starts a NEW list item and must not extend the current paragraph
# (REFLOW_SPEC §R1.3, markerless-list mitigation). Matches a leading number /
# single letter / roman-ish token followed by '.' or ')', or a bullet/dash glyph.
_LIST_PREFIX_RE = re.compile(
    r"^\s*(?:\(?(?:\d{1,3}|[A-Za-z]|[ivxIVX]{1,4})[.)]|[-*•·–▪◦])\s+\S"
)


def _starts_list_item(text: str) -> bool:
    """True when ``text`` begins with an inline list marker (numbered / lettered
    / bulleted) that should break paragraph grouping at this line."""
    return bool(_LIST_PREFIX_RE.match(_normalize_spaces(text)))


@dataclass(frozen=True)
class Span:
    """One run of same-styled text, with everything needed to re-render it.

    ``font_xref`` is the bridge to the document's embedded font: it is resolved
    at extraction time via ``FontEngine.embedded_xref`` so the UI can hand it
    straight to the Qt loader / save writer without re-deriving the match.
    ``ascender``/``descender`` are the rawdict per-span metrics (font-relative;
    multiply by ``size`` for pixels) used for accurate baseline geometry.
    """

    text: str
    bbox: tuple            # (x0, y0, x1, y1) in PDF points
    origin: tuple          # baseline (x, y) in PDF points
    size: float            # font size in points
    color: tuple           # (r, g, b) each 0..1
    font: str              # span font name as rawdict reports it (subset tag pre-stripped)
    flags: int             # PyMuPDF style bitfield
    font_xref: int | None  # embedded font xref (FontEngine.embedded_xref); None if not embedded
    ascender: float        # rawdict span ascender (font-relative; * size for px)
    descender: float       # rawdict span descender (negative; font-relative)
    block_index: int
    line_index: int
    span_index: int
    dir: tuple = (1.0, 0.0)  # line['dir'] writing direction (cos, -sin); (1,0)=horizontal
    page_index: int = 0      # page this span belongs to (global identity)
    # Every bbox to redact when removing this box. Usually just (bbox,), but a
    # box merged from OVERLAPPING duplicate runs (some PDFs draw a value twice,
    # e.g. a full span plus fragments) carries all of them, so editing the box
    # erases every duplicate instead of leaving residue behind.
    redact_bboxes: tuple = ()
    # True only for a GENUINE page-centered single line (a title/subtitle alone on
    # its baseline row). Set at extraction; ``_maybe_recenter`` re-centers ONLY these,
    # so a table/form cell (which shares its row with other cells) keeps its LEFT edge
    # fixed on a resize/edit instead of sliding (Edward: "the first letter jumps").
    centered_line: bool = False

    @property
    def key(self) -> tuple:
        return (self.block_index, self.line_index, self.span_index)

    @property
    def redact_rects(self) -> tuple:
        """The bboxes to redact for this box (its own, plus any merged
        duplicates). Falls back to the single bbox for an unmerged span."""
        return self.redact_bboxes or (self.bbox,)

    @property
    def global_key(self) -> tuple:
        """Page-qualified identity: unique across the whole document, so a
        preview/repaint can tell whether a span belongs to the displayed page."""
        return (self.page_index, self.block_index, self.line_index,
                self.span_index)

    @property
    def identity(self) -> tuple:
        """The stable per-box key the view re-finds a box by across a reload
        (Box protocol, BUILD_SPEC §1.8). For a Span this is its global_key."""
        return self.global_key

    @property
    def is_horizontal(self) -> bool:
        """True when the run reads left-to-right (no page/text rotation)."""
        return abs(self.dir[0] - 1.0) < 1e-3 and abs(self.dir[1]) < 1e-3

@dataclass(frozen=True)
class ParagraphBox:
    """A grouped multi-line PARAGRAPH: consecutive same-paragraph ``Span`` lines
    fused into ONE editable, reflowable box (REFLOW_SPEC §R1.2).

    Satisfies the ``Box`` Protocol so the view's hit-test / selection / overlay
    machinery treats it uniformly with ``Span`` and ``NewBox``. Only ever appears
    for >= 2 grouped lines (a single-line "paragraph" stays a ``Span``), so the
    existing single-line field / table / heading behavior is byte-for-byte
    unchanged. It carries the paragraph's joined text, every member line's
    redaction rects (so editing it erases all of them), the resolved style of the
    primary (first) member, the measured leading, and the inferred alignment --
    everything the wrap engine and the bake need to reflow it.
    """

    # --- identity / geometry ---
    page_index: int
    bbox: tuple                 # union of all member line bboxes (x0,y0,x1,y1)
    origin: tuple               # baseline (x,y) of the FIRST member line (PDF pts)
    members: tuple              # tuple[Span,...] the constituent lines, reading order
    # --- resolved paragraph style (from the primary/first member) ---
    text: str                   # member texts joined with a single space per soft break
    size: float                 # the paragraph's body size (from members[0])
    color: tuple                # (r,g,b) 0..1
    font: str                   # rawdict font name of members[0] (for resolve())
    flags: int                  # style bitfield of members[0]
    font_xref: int | None       # members[0].font_xref (3-tier resolve bridge)
    ascender: float             # members[0].ascender (font-relative)
    descender: float            # members[0].descender
    dir: tuple = (1.0, 0.0)     # writing direction (horizontal paragraphs only)
    # --- paragraph layout ---
    leading: float = 0.0        # measured baseline-to-baseline leading (PDF pts)
    alignment: str = "left"     # "left"|"center"|"right"|"justify" (inferred, §R1.5)
    block_index: int = 0        # members[0].block_index (for a stable key)
    line_index: int = 0         # members[0].line_index
    span_index: int = 0         # members[0].span_index
    # --- redaction ---
    redact_bboxes: tuple = ()   # FLATTENED union of every member's redact_rects

    @property
    def key(self) -> tuple:
        return (self.block_index, self.line_index, self.span_index)

    @property
    def redact_rects(self) -> tuple:
        return self.redact_bboxes or (self.bbox,)

    @property
    def global_key(self) -> tuple:
        return (self.page_index, self.block_index, self.line_index,
                self.span_index)

    @property
    def identity(self) -> tuple:
        # NS-prefixed so it can never collide with a Span.global_key (4-tuple of
        # ints) or a NewBox.edit_key ((page,"new",id)).
        return ("para",) + self.global_key

    @property
    def is_horizontal(self) -> bool:
        return abs(self.dir[0] - 1.0) < 1e-3 and abs(self.dir[1]) < 1e-3

    @property
    def is_paragraph(self) -> bool:
        return True


@runtime_checkable
class Box(Protocol):
    """Structural interface satisfied by BOTH ``Span`` (an existing rawdict run)
    and ``NewBox`` (a box added from scratch), so the UI's hit-test / selection /
    overlay machinery works against either uniformly (BUILD_SPEC §1.8).

    ``identity`` is the stable per-box key the view uses to re-find a box across
    a reload (a fresh ``Span`` object is created every reload): ``Span.global_key``
    for spans, ``NewBox.edit_key`` for new boxes.
    """

    page_index: int
    bbox: tuple
    origin: tuple
    size: float
    color: tuple

    @property
    def redact_rects(self) -> tuple: ...
    @property
    def is_horizontal(self) -> bool: ...
    @property
    def identity(self) -> tuple: ...


@dataclass(frozen=True)
class RunStyle:
    """Optional per-RUN font/colour for a SCANNED box, carried as the 4th element
    of a ``(text, bold, italic, RunStyle)`` run so the editor + bake can colour or
    re-face just a selected word. FROZEN (hashable) so it can be a dict/group key.
    ``None`` field == inherit the box's base. The legacy 3-tuple has no 4th element
    and behaves exactly as before (the vector path never sets one), so widening is
    back-compatible. (Per-run SIZE is intentionally omitted -- the inline editor
    measures in pixels, with no reliable per-fragment point size to read back.)"""

    font_family: str | None = None
    color: tuple | None = None          # (r, g, b) 0..1
    underline: bool = False             # per-run underline (vector text-layer spans)
    strike: bool = False                # per-run strikethrough (vector text-layer spans)

    @property
    def is_empty(self) -> bool:
        return (self.font_family is None and self.color is None
                and not self.underline and not self.strike)


@dataclass(frozen=True)
class StyleOverride:
    """A partial restyle of one box (BUILD_SPEC §1.1). Every field is
    ``None`` == 'unchanged from the box's original style'; a non-None field
    overrides it. Carried on ``Edit`` and applied at render/save time. A
    user-PICKED ``font_family`` resolves through ``FontEngine.resolve_family``
    (NOT the embedded tier) so it is always embeddable (BUILD_SPEC §2)."""

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
        """Field-wise overlay: ``other``'s non-None fields win over ``self``'s.
        Lets the inspector apply size, then color, then bold as three separate
        undo steps without each clobbering the others."""
        return StyleOverride(
            font_family=other.font_family if other.font_family is not None
            else self.font_family,
            size=other.size if other.size is not None else self.size,
            color=other.color if other.color is not None else self.color,
            bold=other.bold if other.bold is not None else self.bold,
            italic=other.italic if other.italic is not None else self.italic,
        )


@dataclass
class Edit:
    """All staged changes to ONE existing span (box) (BUILD_SPEC §1.2). Any
    subset may be set; ``None``/empty == unchanged. Applied in BUILD_SPEC §1.7
    order at render/save time."""

    span: "Span | ParagraphBox"            # the box being edited (widened §R2.4)
    new_text: str | None = None             # None == text unchanged
    style: StyleOverride = field(default_factory=StyleOverride)
    move: tuple | None = None               # (dx, dy) in PDF points, cumulative
    scale: float = 1.0                      # size/geometry multiplier (resize), cumulative
    deleted: bool = False                   # True == box removed (redact, no reinsert)
    # --- paragraph reflow payload (None == not overridden; REFLOW_SPEC §R2.4) ---
    alignment: str | None = None            # override alignment; None = box's inferred
    line_spacing: float | None = None       # leading multiplier; None = 1.0
    # --- box FRAME (resize): the column the text wraps into, in PDF points.
    # Set by dragging a resize handle. The text re-wraps to ``box_w`` at the
    # box's CONSTANT font size (never scaled), so resizing a box changes its
    # shape and the text reflows -- a single line wraps to several when narrowed,
    # a paragraph reflows. None == the box's natural (text-tight) column.
    box_x: float | None = None              # column left edge
    box_w: float | None = None              # column width
    # --- per-selection rich styling (None == uniform style) -------------------
    # tuple[(text, bold, italic), ...]: the box's text split into styled runs,
    # produced by bolding/italicizing a SELECTION inside the inline editor. When
    # set it is the authoritative text+weight content; ``new_text`` mirrors the
    # joined string so staged_text/find/replace keep working on plain text.
    runs: tuple | None = None

    @property
    def reflow(self) -> bool:
        """True when this Edit's box wraps to a column width -- a ``ParagraphBox``
        always wraps, and ANY box the user has resized (``box_w`` set) wraps to
        its new frame at constant font size (REFLOW_SPEC §R2.4)."""
        return getattr(self.span, "is_paragraph", False) or self.box_w is not None

    def effective_text(self, span: "Span | ParagraphBox | None" = None) -> str:
        s = span if span is not None else self.span
        if self.runs is not None:
            return "".join(r[0] for r in self.runs)
        return s.text if self.new_text is None else self.new_text

    @property
    def is_noop(self) -> bool:
        """True when this Edit changes nothing vs the box's original -- used to
        drop it from ``_edits`` so a box reverted to source is not written out."""
        return (
            (self.new_text is None or self.new_text == self.span.text)
            and self.runs is None
            and self.style.is_empty
            and (self.move is None or (abs(self.move[0]) < 1e-6
                                       and abs(self.move[1]) < 1e-6))
            and abs(self.scale - 1.0) < 1e-9
            and not self.deleted
            and self.alignment is None
            and self.line_spacing is None
            and self.box_w is None
        )


@dataclass
class NewBox:
    """A text box added from scratch (not from any rawdict span) (BUILD_SPEC
    §1.3). Self-describing: it owns its style directly (no original to override).
    Surfaced by ``new_boxes()`` so the UI treats it uniformly. Geometry is
    recomputed from the resolved face metrics whenever text/size/family/style
    changes (BUILD_SPEC §1.6)."""

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
    # Writing direction in TEXT space (cos θ, -sin θ), like Span.dir. Defaults
    # to horizontal; OCR on a /Rotate page sets it so the baked text displays
    # upright with the page rotation, instead of horizontal-in-text-space which
    # bakes sideways.
    dir: tuple = (1.0, 0.0)
    # OCR cover: a TEXT-space rect + paper color (x0,y0,x1,y1,r,g,b) painted
    # UNDER the text at bake time so the scanned glyphs are replaced, not
    # doubled. Empty for ordinary added boxes. Painted only when the box is
    # VISIBLE (render_mode 0); an invisible OCR overlay carries its cover unused
    # until the word is edited.
    cover: tuple = ()
    # PDF text render mode at bake: 0 = fill (visible, normal text), 3 = invisible
    # (in the content stream, so selectable/searchable, but draws no marks). OCR
    # adds its text render_mode=3 over the untouched scan image so the page stays
    # pixel-identical to the scan; editing a word flips it to 0 so the cover then
    # whites out the scanned word and the replacement shows. Ordinary boxes stay 0.
    render_mode: int = 0
    # 0.3.0 scanned-edit blend: a degraded+recolored RGB raster (PNG bytes) of the
    # edited word and its placement rect (text-space). Set ONLY for an edited
    # scanned-OCR box (one with a cover); when present the bake draws this image
    # over the cover instead of crisp vector text, so the edit matches the scan's
    # ink colour and degradation. Empty for every ordinary box.
    edit_image: bytes = b""
    edit_image_rect: tuple = ()
    # Paragraph (multi-line AREA) support, for OCR areas that fuse several scanned
    # lines into ONE editable block (the user edits a paragraph as one). When
    # ``box_w`` is set the box is a reflowable paragraph: the inline editor goes
    # multi-line (keys off ``is_paragraph``) and wraps to ``box_w``, ``leading`` is
    # the baseline-to-baseline gap, ``alignment`` the block alignment. Unset for an
    # ordinary single-line box / form field, which behaves exactly as before.
    box_w: float | None = None
    leading: float = 0.0
    alignment: str = "left"
    # The ORIGINAL recognized text of a scanned-OCR box, fixed at OCR time. The
    # in-place edit composes (original scan + diff from THIS to the new text), so it
    # must stay the text that the scan actually shows even after ``text`` is edited
    # and committed -- otherwise a re-edit diffs an already-edited string against the
    # untouched scan and re-synthesizes garbage. Empty for non-OCR boxes.
    ocr_text: str = ""
    # Per-line covers for a PARAGRAPH OCR box: one TEXT-space (x0,y0,x1,y1,r,g,b) per
    # recognized line, same top->bottom order as ``ocr_text``'s ``\n``-joined lines.
    # The unified raster runs the single-line in-place engine on each line against
    # its own cover + original line text, so every untouched line keeps its scan
    # pixels 1-for-1 and only changed glyphs are synthesized. Empty for a single line
    # (which uses ``cover`` directly), so the single-line path is unchanged.
    line_covers: tuple = ()
    # Per-selection RICH styling staged from the inline editor: a tuple of
    # (text, bold, italic) covering ``text``, so a scanned box can carry per-run
    # bold/italic (a bolded word, newly typed bold text) through commit + re-edit.
    # Empty = uniform (the box's own bold/italic). Mirrors Edit.runs for Spans.
    runs: tuple = ()
    # True once the scanned box's REAL weight/slant has been detected (from the
    # edge-matched bank face) and written onto ``bold``/``italic``. A scanned box
    # is created with bold/italic from the shape match (serif/sans/mono only), so
    # its weight is unknown until the edge-match runs at first edit; this flag
    # stops a second open from re-seeding (and clobbering a user's style change).
    style_seeded: bool = False
    # True once the user EXPLICITLY picked a font family for a scanned box: the
    # in-place synth then renders that family's variant FILE instead of the scan
    # edge-match (which otherwise always wins and silently discards the pick).
    font_picked: bool = False

    @property
    def is_paragraph(self) -> bool:
        return self.box_w is not None

    @property
    def edit_key(self) -> tuple:
        return (self.page_index, "new", self.box_id)

    @property
    def identity(self) -> tuple:
        return self.edit_key

    @property
    def redact_rects(self) -> tuple:
        """A NewBox has no original ink, so it contributes NO redaction; this is
        present only to satisfy the Box protocol and is unused by the apply
        pipeline (BUILD_SPEC §1.7 step 1)."""
        return (self.bbox,)

    @property
    def is_horizontal(self) -> bool:
        return abs(self.dir[0] - 1.0) < 1e-3 and abs(self.dir[1]) < 1e-3


# Sentinel for "no staged form value" in a _BoxState: a staged value of None
# could never be told apart from "unset", so form commands carry this marker
# when their before/after state is the unstaged baseline (forms §2).
_FORM_UNSET = object()

# fitz widget type -> this model's form-field kind string (forms §0 constants).
_WIDGET_KIND_BY_TYPE = {
    fitz.PDF_WIDGET_TYPE_TEXT: "text",
    fitz.PDF_WIDGET_TYPE_CHECKBOX: "checkbox",
    fitz.PDF_WIDGET_TYPE_RADIOBUTTON: "radio",
    fitz.PDF_WIDGET_TYPE_COMBOBOX: "combo",
    fitz.PDF_WIDGET_TYPE_LISTBOX: "listbox",
    fitz.PDF_WIDGET_TYPE_SIGNATURE: "signature",
    fitz.PDF_WIDGET_TYPE_BUTTON: "button",
}

# Form-field kinds the fill machinery accepts (forms §8: listbox renders but
# is not interactive; buttons/signatures are never fillable).
_FILLABLE_FORM_KINDS = ("text", "checkbox", "radio", "combo")


@dataclass(frozen=True)
class FormField:
    """One WIDGET of an AcroForm field, as ``form_fields`` enumerates it
    (forms §2). Like a Span, a frozen READ view rebuilt from ``self.working``
    on demand; staged values live in ``_form_edits`` keyed by ``group_key``
    so the working doc is never mutated by a fill. Radio KIDS are separate
    FormFields (each its own clickable widget) sharing one ``group_key``.

    ``rect`` is UNROTATED page space -- the same space span bboxes live in
    (probe §0: display rect == ``widget.rect * page.rotation_matrix``).
    ``value`` is the BASELINE (the value in the working doc): str for
    text/combo, bool for checkbox, the selected kid's on-state str or "Off"
    for a radio group."""

    page_index: int
    name: str                           # the PDF field name (/T)
    kind: str   # text|checkbox|radio|combo|listbox|signature|button
    rect: tuple                         # (x0, y0, x1, y1), unrotated space
    xref: int                           # this widget's annot xref
    value: object                       # baseline value (see docstring)
    on_state: str | None = None         # checkbox / radio kid on-state
    options: tuple = ()                 # combo/list choice_values
    flags: int = 0                      # /Ff field flags
    multiline: bool = False             # text: PDF_TX_FIELD_IS_MULTILINE
    readonly: bool = False              # PDF_FIELD_IS_READ_ONLY
    text_fontsize: float = 0.0          # 0 = auto-size
    max_len: int = 0                    # 0 = unlimited

    @property
    def identity(self) -> tuple:
        """Per-WIDGET identity (canvas items): radio kids differ by xref."""
        return (self.page_index, "form", self.name, self.xref)

    @property
    def group_key(self) -> tuple:
        """Per-FIELD staging key: radio kids share it (one value per field)."""
        return (self.page_index, "form", self.name)

    @property
    def fillable(self) -> bool:
        """True when this widget takes a fill (forms §3): the canvas builds
        a FieldHotspot only for these; ``stage_form_value`` rejects the rest.
        ONE registry for both gates -- readonly/button/signature/listbox
        widgets stay invisible to interaction (their appearance is baked)."""
        return not self.readonly and self.kind in _FILLABLE_FORM_KINDS


@dataclass
class ImageBox:
    """A STAGED placed image (images & signatures §2.1): a third box kind
    beside Span/NewBox. Like every other staged state it lives only in the
    model's maps and becomes real ink on every fresh copy the one shared
    pipeline bakes (``page.insert_image`` in ``_apply_page_edits``), so
    screen == file by construction and ``self.working`` is never touched by
    an image op. ``rect`` is rawdict text-space PDF points (the same
    derotated space Span bboxes live in -- probe-verified: ``insert_image``
    takes the rect in unrotated coordinates and ``rotate=page.rotation``
    renders it upright on a /Rotate page). ``image`` is the ORIGINAL
    PNG/JPEG file bytes, never recoded for placement, so quality and file
    size match the source exactly."""

    page_index: int
    rect: tuple                      # (x0, y0, x1, y1) text-space PDF points
    image: bytes                     # original PNG/JPEG bytes (validated)
    kind: str = "file"               # "file" | "signature" | "stamp" | "moved"
    natural_px: tuple = (0, 0)       # source pixel size (aspect + default size)
    box_id: int = 0                  # per-document counter, like NewBox
    deleted: bool = False

    # --- Box-protocol duck typing (hotspots/overlay read these) ----------
    @property
    def edit_key(self) -> tuple:
        return (self.page_index, "img", self.box_id)

    @property
    def identity(self) -> tuple:
        return self.edit_key

    @property
    def bbox(self) -> tuple:
        return self.rect

    @property
    def origin(self) -> tuple:
        return (self.rect[0], self.rect[3])

    @property
    def size(self) -> float:
        return self.rect[3] - self.rect[1]

    @property
    def color(self) -> tuple:
        return (0.0, 0.0, 0.0)

    @property
    def is_horizontal(self) -> bool:
        return True

    @property
    def dir(self) -> tuple:
        return (1.0, 0.0)

    @property
    def redact_rects(self) -> tuple:
        """An ImageBox has no original page ink, so it contributes NO
        redaction (Box-protocol parity with NewBox; unused by the bake)."""
        return ()


@dataclass(frozen=True)
class ExistingImage:
    """An image occurrence ALREADY IN THE FILE (images & signatures §2.1,
    M3): one ``get_image_info(xrefs=True)`` entry, identified by its page,
    its image ``xref``, and its occurrence index on that page (the same
    xref can appear on several pages -- the shared-logo scenario -- or even
    twice on one page). ``rect`` is the occurrence's bbox in UNROTATED
    text space (probe-verified: ``get_image_info`` round-trips the
    ``insert_image`` rect exactly on /Rotate 0/90/180/270 pages with
    ``keep_proportion=False``), the same space Span bboxes and
    ``add_redact_annot`` rects live in, so the hotspot mapping and the
    deletion redaction pass take it as-is.

    Frozen and never staged-mutable: deleting stages a ``_xim_deletes``
    entry (a scoped image-REMOVE redaction at bake time); moving is a
    window-level macro of xim_delete + img_add (§5)."""

    page_index: int
    xref: int
    rect: tuple                      # (x0, y0, x1, y1) text-space PDF points
    occ: int                         # occurrence index on the page (0-based)

    # --- Box-protocol duck typing (hotspots/overlay read these) ----------
    @property
    def edit_key(self) -> tuple:
        return (self.page_index, "xim", self.xref, self.occ)

    @property
    def identity(self) -> tuple:
        return self.edit_key

    @property
    def bbox(self) -> tuple:
        return self.rect

    @property
    def origin(self) -> tuple:
        return (self.rect[0], self.rect[3])

    @property
    def size(self) -> float:
        return self.rect[3] - self.rect[1]

    @property
    def color(self) -> tuple:
        return (0.0, 0.0, 0.0)

    @property
    def is_horizontal(self) -> bool:
        return True

    @property
    def dir(self) -> tuple:
        return (1.0, 0.0)

    @property
    def redact_rects(self) -> tuple:
        """NOT consumed by the text redaction pass: an ExistingImage's
        deletion runs through the separate image-REMOVE pass (§2.4(1)),
        never the text pass (flag sets differ). Box-protocol parity only."""
        return ()


# Magic numbers ``add_image`` accepts: PNG and JPEG only (the Insert-Image
# file filter); anything else raises before any state changes.
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_JPEG_MAGIC = b"\xff\xd8\xff"

# Floor for a staged image edge (PDF points): a degenerate resize/clamp can
# never collapse the rect to zero (it would be unselectable and draw nothing).
_MIN_IMAGE_EDGE_PT = 1.0


# Text-markup annotation kinds: each maps to a ``page.add_<kind>_annot(quads=)``
# adder. The full kind set (ink/shapes) lands milestone by milestone.
_MARKUP_KINDS = ("highlight", "underline", "strikeout", "squiggly")
# Drawn-shape kinds (annotations & markup §3.4 step 2): rect/ellipse carry a
# ``rect`` (+ optional fill); line/arrow carry ``endpoints`` (the arrow is a
# Line annot with an open-arrow line ending).
_SHAPE_KINDS = ("rect", "ellipse", "line", "arrow")

# PDF annot type -> this model's kind string, for enumerating annotations that
# already live in the FILE (annotations & markup §3.2). Unmapped types fall
# back to the lowercased PyMuPDF type name -- still a valid record, just one
# the view offers no kind-specific affordances for.
_KIND_BY_PDF_TYPE = {
    fitz.PDF_ANNOT_HIGHLIGHT: "highlight",
    fitz.PDF_ANNOT_UNDERLINE: "underline",
    fitz.PDF_ANNOT_STRIKE_OUT: "strikeout",
    fitz.PDF_ANNOT_SQUIGGLY: "squiggly",
    fitz.PDF_ANNOT_TEXT: "note",
    fitz.PDF_ANNOT_INK: "ink",
    fitz.PDF_ANNOT_SQUARE: "rect",
    fitz.PDF_ANNOT_CIRCLE: "ellipse",
    fitz.PDF_ANNOT_LINE: "line",
}


@dataclass(frozen=True)
class AnnotSpec:
    """One STAGED annotation (annotations & markup §3.1). Like an Edit/NewBox,
    a spec lives only in the model's maps; it becomes a real PDF annot object
    on every fresh copy the one shared pipeline bakes (``render_with_edits``,
    ``save_as``, the structural bake), so screen == file by construction and
    ``self.working`` is never touched by annotation ops.

    ALL geometry is in rawdict text space (the same derotated space Span
    bboxes live in -- probe §1 verified ``add_highlight_annot`` accepts
    text-space quads and renders correctly on a /Rotate page)."""

    page_index: int
    annot_id: int                       # monotonic per document, like NewBox
    kind: str   # highlight|underline|strikeout|squiggly|note|ink|rect|ellipse|line|arrow
    quads: tuple | None = None          # markup: one (x0,y0,x1,y1) per line group
    rect: tuple | None = None           # note anchor / rect / ellipse
    points: tuple | None = None         # ink: tuple of strokes (each a point tuple)
    endpoints: tuple | None = None      # line/arrow: ((x0,y0),(x1,y1))
    stroke: tuple = (0.85, 0.1, 0.1)
    fill: tuple | None = None           # rect/ellipse only
    width: float = 2.0
    opacity: float = 1.0
    contents: str = ""

    @property
    def identity(self) -> tuple:
        return (self.page_index, "annot", self.annot_id)

    def with_changes(self, **kwargs) -> "AnnotSpec":
        return replace(self, **kwargs)


@dataclass(frozen=True)
class AnnotOverride:
    """A staged change to an annotation ALREADY IN THE FILE, keyed by
    ``(page_index, xref)`` (annotations & markup §3.1). Applied to the fresh
    copy by xref inside ``_apply_page_annots`` (xrefs are byte-identical in
    the copy, probe-verified). Only delete / contents / note-move are
    supported -- never style or arbitrary geometry of foreign annots."""

    deleted: bool = False
    contents: str | None = None
    dx: float = 0.0                     # notes only; cumulative
    dy: float = 0.0


@dataclass(frozen=True)
class AnnotRecord:
    """Unified READ view of one annotation for hotspots + the comments panel
    (annotations & markup §3.2): a staged ``AnnotSpec`` or a pre-existing file
    annot, normalized to one surface. ``display_rect`` is text space."""

    identity: tuple                     # spec identity | (page, "xref", xref)
    kind: str
    display_rect: tuple
    contents: str
    is_existing: bool = False
    xref: int | None = None
    spec: AnnotSpec | None = None


@dataclass(frozen=True)
class Match:
    """One Find & Replace hit (REFLOW_SPEC §R5.1).

    A match is located by the box's stable ``identity`` (not the live object,
    which is rebuilt every reload) plus a ``[start, end)`` character span inside
    that box's CURRENT staged text. Because a ``ParagraphBox``'s staged text is
    the joined paragraph string, a single match can span the original soft line
    breaks -- exactly what the user wants when editing flowing text. ``context``
    is a short snippet for the results list; ``box`` is the live box object at
    search time (the panel re-finds it by identity before acting, so a stale ref
    after an edit is harmless)."""

    page_index: int
    box_identity: tuple
    start: int
    end: int
    text: str               # the matched substring (the box's staged text slice)
    context: str            # a trimmed one-line snippet around the match
    box: object = None      # the live box object at search time (re-found on use)


@dataclass(frozen=True)
class WordBox:
    """One word on a page AS THE BAKE LAYS IT DOWN (text-editing UX §5.1).

    Produced by ``PDFDocument.page_words``: ``bbox`` is rawdict text-space
    (the same derotated space Span bboxes live in -- probe §1 verified
    ``get_text("words")`` coords match rawdict span bboxes even on a rotated
    page), so the view maps it through the existing ``rotation_matrix`` /
    ``_scene_point`` path. ``(block, line, word)`` is PyMuPDF's reading-order
    key; ``page_words`` returns words sorted by it, so the text-select tool's
    contiguous ``[lo, hi]`` index ranges ARE reading-order ranges."""

    bbox: tuple             # (x0, y0, x1, y1) in rawdict text-space points
    text: str
    block: int
    line: int
    word: int


@dataclass
class _BoxState:
    """The snapshot the history stores / restores for ONE box (BUILD_SPEC §1.4).
    For an existing span it mirrors ``Edit`` fields; for a NewBox it mirrors the
    mutable NewBox fields (carried whole) plus ``exists`` (so 'add' toggles
    existence and 'delete' of a NewBox flips it back)."""

    # existing-span overrides (None/defaults == unchanged):
    new_text: str | None = None
    style: StyleOverride = field(default_factory=StyleOverride)
    move: tuple | None = None
    scale: float = 1.0
    deleted: bool = False
    # paragraph reflow overrides (None == unchanged; REFLOW_SPEC §R2.4):
    alignment: str | None = None
    line_spacing: float | None = None
    # box FRAME from a resize: the column (left, width) the text wraps into at
    # CONSTANT font size (None == the box's natural text-tight column).
    box_x: float | None = None
    box_w: float | None = None
    # per-selection rich runs (None == uniform):
    runs: tuple | None = None
    # new-box payload (only used when the key is a "new" key):
    newbox: "NewBox | None" = None
    # placed-image payload (only used when the key is an "img" key; images &
    # signatures §2.2 -- reuses ``exists`` exactly like the newbox pattern):
    imagebox: "ImageBox | None" = None
    # existing-image payload (only used when the key is an "xim" key; images
    # & signatures §2.2, M3): ``exists=False`` means "deletion staged" (the
    # ``_xim_deletes`` entry is INSTALLED), the inverse of the imagebox
    # convention because the staged state is the deletion itself.
    xim: "ExistingImage | None" = None
    exists: bool = True
    # staged form value (only used when the key is a "form" key; forms §2):
    # _FORM_UNSET == no staged entry (the widget's baseline shows).
    form_value: object = _FORM_UNSET


@dataclass
class _Command:
    """One undoable mutation of one box (BUILD_SPEC §1.4). ``before`` / ``after``
    are the COMPLETE staged state of that box's editable fields, so replaying
    forward installs ``after`` and backward installs ``before``. Works
    identically for an existing span (keyed by its edit_key) and a NewBox."""

    edit_key: tuple                     # (page_index, span.key) | (page_index,"new",id)
    kind: str                           # "text"|"style"|"move"|"resize"|"delete"|"add"
    before: _BoxState
    after: _BoxState
    label: str
    span: "Span | None" = None          # the Span (for existing-span keys), else None


@dataclass
class _AnnotCommand:
    """One undoable ANNOTATION mutation (annotations & markup §3.3). Rides the
    SAME ``self._undo`` list as ``_Command`` -- ``undo()``/``redo()`` dispatch
    on isinstance -- so annot and text commands interleave on one history in
    lockstep with the Qt stack. ``before``/``after`` are the COMPLETE staged
    state for the key: an ``AnnotSpec`` (staged keys), an ``AnnotOverride``
    (existing-annot keys), or None (absent)."""

    key: tuple          # AnnotSpec identity (page,"annot",id) | (page, xref)
    kind: str           # "annot_add"|"annot_delete"|"annot_move"|... (never "move":
                        # coalesce_last_undo's nudge fuse must not match these)
    before: object
    after: object
    label: str = ""


class PDFDocument:
    """The editable document model.

    Owns the open ``fitz.Document`` (never mutated), a ``FontEngine`` bound to
    it, the staged-edit map, and an authoritative undo/redo history. The Qt
    ``QUndoStack`` in the UI delegates to ``stage_edit``/``undo``/``redo`` here;
    the model is the single owner of edit state, so the EDIT-STATE surface
    (stage_edit/undo/redo/dirty/staged_text) is testable headless without Qt.

    The SAVE path (``save_as``) is NOT Qt-free: it routes every reinsertion
    through ``FontEngine.resolve``, which uses QFont/QFontInfo/QFontDatabase and
    therefore requires a constructed ``QGuiApplication``. ``save_as`` guards on
    this and raises a clear error rather than letting Qt abort the process.
    """

    def __init__(self, path: str, password: str | None = None) -> None:
        self.path = path
        # A single MUTABLE working document per open file (PAGES_SPEC §1.2/§3.0).
        # It is a DEEP COPY of the file on disk, so structural ops mutate
        # ``self.working`` while ``self.path`` bytes are never touched. ALL reads
        # (spans/render/rotation/page_count) and the edit pipeline's source open
        # go through ``self.working`` (its ``tobytes()``), so WYSIWYG + the text
        # editor survive a restructure unchanged.
        src = fitz.open(path)
        # Password gate (doc-tools M4) -- FIRST, before ANY content access
        # (the constructor-order contract: gate -> insert_pdf -> metadata ->
        # TOC). ``needs_pass`` is the file-level fact (it stays set after
        # authenticate); a missing or wrong password raises PasswordRequired
        # for the window's provider loop instead of the old crash ("document
        # closed or encrypted" out of insert_pdf). Post-auth the insert_pdf
        # deep copy below comes out DECRYPTED (probe-verified), so the
        # FontEngine / render / edit pipeline are untouched by encryption.
        self.original_encrypted = bool(src.needs_pass)
        if self.original_encrypted and (
                not password or src.authenticate(password) == 0):
            src.close()
            raise PasswordRequired(
                f"{os.path.basename(path)} is password protected")
        self._open_password = password if self.original_encrypted else None
        self.working = fitz.open()
        self.working.insert_pdf(src)
        # Metadata + TOC carry (doc-tools M1): ``insert_pdf`` copies pages
        # only, silently dropping the source info dict and the outline, so
        # before this carry every save stripped Title/Author/... and wiped the
        # bookmarks of the original file. ``set_metadata``/``set_toc`` both
        # survive the ``tobytes()`` round trips the bake/save pipeline relies
        # on (probe-verified). Constructor order contract (build plan):
        # password gate (security milestone) -> insert_pdf -> metadata carry
        # -> TOC carry.
        src_meta = src.metadata or {}
        self.working.set_metadata(
            {k: src_meta.get(k) or "" for k in _META_KEYS})
        # ``format`` is derived from the file header, not settable on the
        # working copy; keep it for the Properties dialog.
        self.pdf_version = src_meta.get("format") or ""
        toc = src.get_toc()
        if toc:
            self.working.set_toc(toc)
        # Editable OCR layer carry: ``insert_pdf`` drops document-level embedded
        # files, so copy our OCR-layer blob across before ``src`` closes
        # (restore_ocr_layer reads it back from ``working`` after load).
        try:
            from .ocr import layer_io
            if layer_io.EMB_NAME in src.embfile_names():
                self.working.embfile_add(
                    layer_io.EMB_NAME, src.embfile_get(layer_io.EMB_NAME))
        except Exception:
            pass
        src.close()
        self.font_engine = FontEngine(self.working)
        # spans() memo: ``self.working`` content (and only it) determines the
        # extracted + overlap-merged + paragraph-grouped boxes; staged _edits are
        # applied downstream (render_with_edits / effective_bbox), never here. So
        # cache the grouped result per page index, keyed by a generation counter
        # that bumps whenever the working doc changes (every bake / structural op
        # calls _invalidate_caches). Continuous scroll re-materializes pages as
        # they re-enter the buffer band; without this each pass re-ran rawdict
        # extraction + _merge_overlapping + _group_paragraphs (incl. the O(n^2)
        # row_shared scan) and re-churned Qt font resolution (minor perf finding).
        self._spans_generation = 0
        self._spans_cache: dict[int, list] = {}
        # Paragraph wrap memo: effective_bbox / hotspot builds re-resolve the face
        # and re-run wrap_paragraph for an edited ParagraphBox on EVERY call, and
        # continuous scroll rebuilds those repeatedly. Cache the WrapResult keyed
        # by (box.identity, page, wrap-signature) where the signature captures
        # every Edit field the wrap depends on (text, size, alignment, spacing,
        # font/bold/italic). A changed edit yields a new key automatically; the
        # whole memo clears on any working-doc change (minor perf finding).
        self._wrap_cache: dict[tuple, "WrapResult"] = {}
        # Overlapping-neighbors memo: the full rawdict walk in
        # ``_overlapping_neighbors`` costs 6-21 ms per frame and its inputs only
        # change when the working content (the spans generation) or the edit set
        # (edited keys + redaction rects) changes. Keyed on exactly those inputs
        # by ``_overlapping_neighbors_cached``; values are read-only lists (the
        # spans() convention). Cleared wholesale by ``_invalidate_caches``;
        # FIFO-capped so a long session cannot grow it without bound.
        self._neighbors_cache: dict[tuple, list] = {}
        # page_words memo (text-editing UX §5.1): page_index -> tuple[WordBox].
        # An edited page's words cost one ~35ms bake (the same per-page
        # pipeline render_with_edits runs); a clean page's are ~free. Entries
        # drop per page in ``_install_state`` (the one choke point every
        # mutator AND undo/redo replay passes through) and the whole map
        # clears in ``_invalidate_caches`` (working-doc changed).
        self._words_cache: dict[int, tuple] = {}
        # edit_key -> Edit, where edit_key = (page_index, span.key) for existing
        # spans (BUILD_SPEC §1.0). NewBoxes live in their own map below so their
        # ("new", id) keys never collide with rawdict keys.
        self._edits: dict[tuple, Edit] = {}
        # edit_key -> NewBox for boxes added from scratch. Keyed by
        # (page_index, "new", box_id).
        self._new_boxes: dict[tuple, NewBox] = {}
        # Manual box grouping (user override of the automatic paragraph
        # detection, applied in _extract_spans AFTER _group_paragraphs):
        #   _manual_groups[page] = list of frozenset(global_key) -- each set is
        #       a bag of span/paragraph member keys the USER forced into ONE
        #       ParagraphBox (e.g. a continuation line the detector split off
        #       because its font drifted). Honored even across font/alignment
        #       differences -- when the user says it is one box, it is.
        #   _manual_ungroups[page] = set of frozenset(global_key) -- member-key
        #       sets the user forced back APART into individual lines.
        self._manual_groups: dict[int, list[frozenset]] = {}
        self._manual_ungroups: dict[int, set[frozenset]] = {}
        # Monotonic id source for new boxes (never reused, so undo/redo of an
        # add keeps the same identity).
        self._next_box_id = 0
        # STAGED placed images (images & signatures §2.2): edit_key ->
        # ImageBox, keyed (page_index, "img", box_id). Mirrors the NewBox
        # staging -- ``self.working`` is never mutated by an image op; the
        # boxes become real ink on the fresh copy inside ``_apply_page_edits``
        # (after the NewBox draw, before the annot tail -- the bake-order
        # contract).
        self._images: dict[tuple, ImageBox] = {}
        # Monotonic id source for placed images (itertools.count: never
        # reused, so undo/redo of an add keeps the same identity).
        self._image_id_counter = itertools.count(1)
        # STAGED deletions of images already IN the file (images & signatures
        # §2.2, M3): edit_key (page, "xim", xref, occ) -> ExistingImage. Each
        # entry becomes a scoped image-REMOVE redaction pass on the fresh
        # copy (FIRST in ``_apply_page_edits`` -- the bake-order contract);
        # ``self.working`` is never mutated by a deletion stage.
        self._xim_deletes: dict[tuple, ExistingImage] = {}
        # existing_images() memo: page_index -> list[ExistingImage]. Depends
        # only on ``self.working`` (like spans()), so it clears wholesale in
        # ``_invalidate_caches`` and never per-mutation.
        self._xim_cache: dict[int, list] = {}
        # STAGED annotations (annotations & markup §3.2): identity -> AnnotSpec.
        # Mirrors the text-edit staging -- ``self.working`` is never mutated by
        # an annotation op; specs become real annot objects on the fresh copy
        # inside ``_apply_page_annots`` (the one shared pipeline's tail).
        self._annots: dict[tuple, AnnotSpec] = {}
        # Staged changes to annots already IN the file: (page, xref) ->
        # AnnotOverride (delete / contents / note-move only).
        self._annot_overrides: dict[tuple, AnnotOverride] = {}
        # Monotonic id source for staged annots (itertools.count: never reused,
        # so undo/redo of an add keeps the same identity).
        self._annot_id_counter = itertools.count(1)
        # annotations() memo: page_index -> list[AnnotRecord]. Dropped per page
        # by every annot mutator (via _install_annot_state) and cleared whole
        # by _invalidate_caches (working-doc changed).
        self._annot_records_cache: dict[int, list] = {}
        # STAGED form values (forms §2): (page, "form", name) -> value.
        # Mirrors the text-edit staging -- ``self.working`` is never mutated
        # by a fill; values are applied to the fresh copy the one shared
        # pipeline opens (``_apply_form_values``), so screen == file by the
        # same construction as text edits.
        self._form_edits: dict[tuple, object] = {}
        # form_fields() memo: page_index -> list[FormField]. Baselines read
        # from ``self.working`` widgets, so the whole map clears in
        # _invalidate_caches (working-doc changed); staged values live in
        # ``_form_edits`` and never affect the enumeration.
        self._form_fields_cache: dict[int, list] = {}
        # Generalized undo/redo history (BUILD_SPEC §1.4): each entry captures
        # one box's complete before/after staged state, so replaying is uniform
        # across text/style/move/resize/delete/add.
        self._undo: list[_Command] = []
        self._redo: list[_Command] = []
        # True while there is unsaved staged work. Set by any edit-mutating
        # operation, cleared by mark_clean (after a successful save).
        self._dirty = False
        # Save-time security state (doc-tools M4): like Acrobat's Security
        # tab, a PENDING SAVE OPTION rather than a document mutation -- it is
        # NOT on the undo stack (the Security dialog is the revert surface,
        # and says so). An originally encrypted file defaults to re-encrypting
        # with its open password so protection survives Save; permissions are
        # all-granted (granular checkboxes de-scoped).
        if self.original_encrypted:
            self._security = {"encryption": PDF_ENCRYPT_AES_256,
                              "user_pw": self._open_password,
                              "owner_pw": self._open_password,
                              "permissions": _PDF_PERM_ALL}
        else:
            self._security = {"encryption": PDF_ENCRYPT_NONE,
                              "user_pw": None, "owner_pw": None,
                              "permissions": _PDF_PERM_ALL}
        # True while ``set_security`` changed the pending options since the
        # last save; ORed into ``dirty``, cleared by ``mark_clean``.
        self._security_dirty = False
        # Structural-op undo: a coarse bytes-snapshot stack (PAGES_SPEC §1.5).
        # Page permutations bake + clear the fine-grained edit maps, so a
        # fine-grained inverse is fragile; instead each structural op snapshots
        # the working bytes BEFORE the change, and undo/redo restore a snapshot.
        self._struct_undo: list[bytes] = []
        self._struct_redo: list[bytes] = []
        # Pre-op bytes staged by _begin_structural but NOT yet committed to
        # _struct_undo: a structural op only commits its snapshot once the bake +
        # fitz mutation succeed (_finish_structural), so a failure mid-op never
        # leaks a phantom undo entry that points at an op that never happened.
        self._pending_snapshot: bytes | None = None
        # Set True by _finish_structural when the depth cap evicted the oldest
        # snapshot, so the window can drop its matching Qt command in lockstep.
        self.structural_dropped_oldest = False
        # The structural-stack depth that corresponds to the on-disk / pristine
        # state. Updated on save (mark_clean) and reset on open. The structural
        # dirtiness is "current depth != this baseline", so undoing every op back
        # to the saved state reads clean again rather than staying dirty forever.
        # -1 = the saved state lives on a discarded redo branch (unreachable),
        # so the doc can never read structurally clean until the next save.
        self._saved_struct_depth = 0
        # The fine-grained history depth at the last save (mark_clean) -- the
        # QUndoStack clean-index idea applied to the model: "undone back to the
        # OPENED state" is NOT "matches the last save". undo()/redo() derive
        # the staged-dirty flag from this baseline, and a command pushed while
        # below it (discarding the redo branch that held the saved state) or a
        # history-restructuring coalesce invalidates it to -1 (unreachable
        # until the next save).
        self._saved_undo_depth = 0
        # Whether staged fine-grained state existed at the last save: the disk
        # then holds working+staged baked together, while every structural
        # snapshot holds PRE-bake bytes -- so a structural undo landing back on
        # the saved depth only truly matches the disk when nothing was staged
        # at save time.
        self._saved_had_staged = False

    @property
    def doc(self) -> "fitz.Document":
        """Read-only alias for the working document (PAGES_SPEC §3.0). External
        readers historically named ``document.doc``; all reads now resolve to the
        mutable working copy."""
        return self.working

    def close(self) -> None:
        self.working.close()

    @property
    def page_count(self) -> int:
        return self.working.page_count

    # --- rendering -------------------------------------------------------
    def render(self, page_index: int, zoom: float) -> "fitz.Pixmap":
        """Page pixmap at ``zoom`` (matrix = Matrix(zoom, zoom), alpha=False).

        The UI multiplies ``zoom`` by the device pixel ratio before calling so
        the sheet is crisp on retina; this method itself is DPR-agnostic.
        """
        page = self.working[page_index]
        matrix = fitz.Matrix(zoom, zoom)
        return page.get_pixmap(matrix=matrix, alpha=False)

    def render_page_image(self, page_index: int, dpi: float = 300.0):
        """Render a page to an HxWx3 uint8 RGB numpy array at ``dpi`` (OCR_SPEC
        §3.2). Used to hand a scanned page to the OCR worker; the render itself
        stays on the GUI thread (fitz), the heavy OCR runs off it."""
        import numpy as np
        zoom = dpi / 72.0
        page = self.working[page_index]
        pm = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
        arr = np.frombuffer(pm.samples, dtype=np.uint8).reshape(
            pm.height, pm.width, pm.n)
        if pm.n >= 3:
            return np.ascontiguousarray(arr[..., :3])
        return np.repeat(arr[..., :1], 3, axis=2)

    def detect_upright_rotation(self, page_index: int, dpi: float = 130.0) -> int:
        """CONTENT-based upright orientation for a page. The stored ``/Rotate`` on these
        scans is unreliable, so this OCRs the page at all four 90-degree rotations and picks
        the one that reads upright by two geometric cues that do not depend on OCR
        confidence (the macOS engine recognizes text at any rotation):

          * text-line quads are WIDE (w > h) -> the reading axis is horizontal, and
          * ink sits toward the TOP of each line (Latin caps / x-height are dense, descenders
            sparse) -> the page is right-side-up rather than upside-down.

        Returns a ``/Rotate`` value in {0, 90, 180, 270}; falls back to the current rotation
        when there is too little text to decide."""
        page = self.working[page_index]
        orig = page.rotation
        try:
            from .ocr.engine import get_engine
            eng = get_engine("auto")
        except Exception:
            return orig
        import numpy as np
        z = dpi / 72.0
        cand: dict = {}
        try:
            for r in (0, 90, 180, 270):
                page.set_rotation(r)
                pm = page.get_pixmap(matrix=fitz.Matrix(z, z), alpha=False)
                a = np.frombuffer(pm.samples, np.uint8).reshape(
                    pm.height, pm.width, pm.n)[..., :3]
                gray = a.mean(2)
                try:
                    lines = eng.recognize(np.ascontiguousarray(a))
                except Exception:
                    lines = []
                if len(lines) < 3:
                    continue
                aspects, com_sum, wsum = [], 0.0, 0.0
                for ln in lines:
                    xs = [p[0] for p in ln.quad]
                    ys = [p[1] for p in ln.quad]
                    w = max(xs) - min(xs)
                    h = max(1.0, max(ys) - min(ys))
                    aspects.append(w / h)
                    x0, x1 = int(min(xs)), int(max(xs))
                    y0, y1 = int(max(0, min(ys))), int(max(ys))
                    crop = 255.0 - gray[y0:y1, max(0, x0):x1]
                    if crop.size and crop.sum() > 0:
                        rows = crop.sum(1)
                        com = (np.arange(len(rows)) * rows).sum() / rows.sum() \
                            / max(1, len(rows))
                        com_sum += com * ln.confidence
                        wsum += ln.confidence
                if aspects:
                    cand[r] = (float(np.mean(aspects)),
                               com_sum / wsum if wsum else 0.5)
        finally:
            page.set_rotation(orig)
        if not cand:
            return orig
        horiz = {r: v for r, v in cand.items() if v[0] > 1.0}   # reading axis horizontal
        pool = horiz or cand
        return min(pool, key=lambda r: pool[r][1])              # ink toward top -> upright

    def normalize_orientations(self) -> None:
        """FIRST step for a document: set every page to its content-detected upright
        rotation, so all downstream work (display, OCR, the border map, editing) runs on an
        upright page and the stored ``/Rotate`` never matters again. Runs once per document
        (the OCR probe is not free); leaves a page untouched when its orientation cannot be
        decided."""
        if getattr(self, "_oriented", False):
            return
        self._oriented = True
        # Content-detect orientation ONLY for SCANNED pages (a raster covering
        # >=half the page) -- the stored /Rotate on a scan is unreliable. A digital
        # page's /Rotate + text direction are authoritative, and OCR-detecting one
        # can WRONGLY flip an already-upright page (a Comic Sans body page read as
        # 180), which then edits/saves at the rotated position. Gate on
        # scanned_pages(), NOT page_has_text_layer: a sideways scan often carries a
        # little digital text -- a Bates stamp, page number, or fax header -- and a
        # text-presence test would wrongly treat it as an upright digital page and
        # leave it sideways. Image-coverage catches the scan through the stray text.
        scanned = set(self.scanned_pages())
        for i in range(self.working.page_count):
            try:
                if i not in scanned:
                    continue
                r = self.detect_upright_rotation(i)
                if self.working[i].rotation != r:
                    self.working[i].set_rotation(r)
            except Exception:
                pass

    def render_baked_image(self, page_index: int, dpi: float = 300.0):
        """Like ``render_page_image`` but with staged edits BAKED in (the pixels the scene
        actually shows). The MAP must measure THIS, not the original scan, so an edited box
        maps its EDITED text instead of the glyphs it used to show. Falls back to the raw
        scan render if the bake path is unavailable."""
        import numpy as np
        try:
            pm = self.render_with_edits(page_index, dpi / 72.0)
        except Exception:
            return self.render_page_image(page_index, dpi)
        arr = np.frombuffer(pm.samples, dtype=np.uint8).reshape(pm.height, pm.width, pm.n)
        if pm.n >= 3:
            return np.ascontiguousarray(arr[..., :3])
        return np.repeat(arr[..., :1], 3, axis=2)

    def page_border_lines(self, page_index: int, dpi: float = 300.0):
        """Detect the table / page RULE lines on a page (the grid + frame + section
        dividers) -- the BORDER map. Returns ``(h_lines, v_lines)`` where each is a list of
        ``(x0, y0, x1, y1)`` rects in DISPLAY POINTS (the scene frame the debug overlay
        draws in). Detection runs on the display-space render, so a /Rotate page needs no
        extra transform; px -> points is the plain ``dpi`` scale. Eventually combined with
        the OCR box map to split an over-merged table row into per-cell boxes."""
        from .ocr.borders import detect_borders
        try:
            img = self.render_page_image(page_index, dpi)
        except Exception:
            return [], []
        # Scale-aware length floor: a rule is far longer than the page's text, while a
        # letter stroke is about one text line. Tie the floor to the OCR text-line height so
        # a big-text card's strokes are rejected without dropping a small-text grid's short
        # cell rules. Best-effort: no OCR -> no floor.
        min_len = 0.0
        try:
            import numpy as np
            from .ocr.engine import get_engine
            hs = [max(p[1] for p in ln.quad) - min(p[1] for p in ln.quad)
                  for ln in get_engine("auto").recognize(np.ascontiguousarray(img))]
            if hs:
                min_len = 2.5 * float(np.median(hs))
        except Exception:
            min_len = 0.0
        s = 72.0 / float(dpi)
        h, v = detect_borders(img, min_len=min_len)
        # THICKNESS filter: a genuine rule is a THIN line -- its ink forms a shallow band
        # perpendicular to its length. Heavy dense scanned TEXT (a near-black VIN) trips
        # detect_borders as one long horizontal run; every downstream consumer then treats it as
        # a rule -- the tile-restore stamps the old scan back, and _restore_line_in_cover redraws
        # a solid ink bar OVER the edited tile (the "delete one letter -> the prefix goes solid
        # black" bug). Measure the median longest dark run PERPENDICULAR to the line, in a
        # text-height window around it: a thin rule stays small (its own thickness), a glyph mass
        # runs tall. Drop the tall ones. Fixing it here fixes every consumer at once
        # (page_border_lines is the single detection source). vcap ties to the page's text height
        # so it is GENERAL, not tuned to one scan.
        import numpy as np
        gray = img.mean(2) if img.ndim == 3 else img
        Hs, Ws = gray.shape[:2]
        th = max(8, int(round(min_len / 2.5))) if min_len else 20   # ~ median text height (px)
        vcap = max(9.0, 0.28 * th)

        def _thin(rule, vertical):
            x0, y0, x1, y1 = (int(round(c)) for c in rule)
            xa, xb = max(0, min(x0, x1)), min(Ws, max(x0, x1) + 1)
            ya, yb = max(0, min(y0, y1)), min(Hs, max(y0, y1) + 1)
            if xb <= xa or yb <= ya:
                return True
            if vertical:                                     # run ALONG x (perp to the line)
                win = gray[ya:yb, max(0, xa - th):min(Ws, xb + th)] < 130
                lines = [win[j, :] for j in range(0, win.shape[0], 3)]
            else:                                            # run ALONG y (perp to the line)
                win = gray[max(0, ya - th):min(Hs, yb + th), xa:xb] < 130
                lines = [win[:, j] for j in range(0, win.shape[1], 3)]
            if not lines:
                return True
            runs = []
            for ln in lines:                                 # longest contiguous dark run
                best = r = 0
                for val in ln:
                    r = r + 1 if val else 0
                    if r > best:
                        best = r
                runs.append(best)
            return float(np.median(runs)) <= vcap
        h = [r for r in h if _thin(r, False)]
        v = [r for r in v if _thin(r, True)]
        scale = lambda L: [(x0 * s, y0 * s, x1 * s, y1 * s) for (x0, y0, x1, y1) in L]
        return scale(h), scale(v)

    def _cached_border_lines(self, page_index: int):
        """``page_border_lines`` but memoized -- detection runs OCR, far too slow to
        repeat on every move/bake. The scan never changes, so the map is stable;
        ``_invalidate_caches`` drops it after a structural op (page geometry change)."""
        cache = getattr(self, "_border_lines_cache", None)
        if cache is None:
            cache = self._border_lines_cache = {}
        if page_index not in cache:
            try:
                cache[page_index] = self.page_border_lines(page_index)
            except Exception:
                cache[page_index] = ([], [])
            dlog("BORDER", "detect", page=page_index + 1,
                 h=len(cache[page_index][0]), v=len(cache[page_index][1]))
        return cache[page_index]

    def _borders_text_space(self, page_index: int):
        """The page's rule lines in TEXT space (the frame a NewBox cover lives in),
        so border geometry can be intersected with covers / tiles. ``page_border_lines``
        is DISPLAY space; map it back through the page derotation (identity at /Rotate 0).
        Returns ``(h_lines, v_lines)`` as ``(x0,y0,x1,y1)`` text-space rects."""
        h, v = self._cached_border_lines(page_index)
        if not (h or v):
            return [], []
        der = self.working[page_index].derotation_matrix
        def mp(lines):
            out = []
            for (x0, y0, x1, y1) in lines:
                pts = [fitz.Point(x, y) * der for x, y in
                       ((x0, y0), (x1, y0), (x1, y1), (x0, y1))]
                out.append((min(p.x for p in pts), min(p.y for p in pts),
                            max(p.x for p in pts), max(p.y for p in pts)))
            return out
        return mp(h), mp(v)

    def _border_rects_in(self, page_index: int, rect, pad: float = 0.0):
        """The portions of rule lines that fall INSIDE ``rect`` (text space) --
        i.e. the table-border pixels a cover/tile overlaps. The collision set the
        immutable-border logic acts on. ``pad`` widens each rule slightly (points)
        so a thin/faint rule whose detected position is a hair off the actual ink is
        still fully covered. Empty when the page has no rules crossing ``rect``."""
        x0, y0, x1, y1 = rect
        h, v = self._borders_text_space(page_index)
        out = []
        for (bx0, by0, bx1, by1) in (list(h) + list(v)):
            # pad ACROSS the rule (its thin axis) only, so a near-vertical rule grows
            # in x and a near-horizontal one in y, never ballooning into a fat block.
            if (bx1 - bx0) <= (by1 - by0):            # vertical-ish
                bx0, bx1 = bx0 - pad, bx1 + pad
            else:                                     # horizontal-ish
                by0, by1 = by0 - pad, by1 + pad
            ix0, iy0 = max(x0, bx0), max(y0, by0)
            ix1, iy1 = min(x1, bx1), min(y1, by1)
            if ix1 - ix0 > 0.05 and iy1 - iy0 > 0.05:
                out.append((ix0, iy0, ix1, iy1))
        return out

    @staticmethod
    def _cover_fill_rects(rect, keep):
        """The sub-rects that tile ``rect`` MINUS the ``keep`` rects -- i.e. the cover
        area to paper-fill while leaving the border strips untouched. Pure geometry
        (a grid built from the keep edges); returns [] only when keep fully covers."""
        x0, y0, x1, y1 = rect
        if not keep:
            return [(x0, y0, x1, y1)]
        xs = sorted({x0, x1} | {e for r in keep for e in (r[0], r[2])
                                if x0 < e < x1})
        ys = sorted({y0, y1} | {e for r in keep for e in (r[1], r[3])
                                if y0 < e < y1})
        out = []
        for i in range(len(xs) - 1):
            for j in range(len(ys) - 1):
                cx0, cx1, cy0, cy1 = xs[i], xs[i + 1], ys[j], ys[j + 1]
                if cx1 - cx0 < 0.05 or cy1 - cy0 < 0.05:
                    continue
                mx, my = (cx0 + cx1) / 2.0, (cy0 + cy1) / 2.0
                if any(r[0] <= mx <= r[2] and r[1] <= my <= r[3] for r in keep):
                    continue                       # this cell IS a border -> leave it
                out.append((cx0, cy0, cx1, cy1))
        return out

    def _fill_cover_preserving_borders(self, page, rect, color, page_index: int):
        """Vacate ``rect`` (paint it ``color``/paper to blank a moved box's original
        spot) WITHOUT painting over any table-border rule that crosses it -- so the
        rule stays exactly in place, immutable. No borders -> one plain fill."""
        keep = self._border_rects_in(page_index, rect, pad=1.0)
        fills = self._cover_fill_rects(rect, keep)
        dlog("BORDER", "vacate", page=page_index + 1, n_rule=len(keep),
             n_fill=len(fills), cover=tuple(round(c, 1) for c in rect))
        for (cx0, cy0, cx1, cy1) in fills:
            page.draw_rect(fitz.Rect(cx0, cy0, cx1, cy1),
                           color=color, fill=color, width=0)

    def _strip_borders_from_tile(self, png_bytes: bytes, text_rect, page_index: int):
        """Remove table-border ink from a LIFTED scan tile (the moved box's raster)
        and inpaint the gap, so a moved box does not carry the rule to its new spot
        and a letter the rule crossed (e.g. a '2' with a vertical rule through it) is
        repaired from its own surrounding ink. No-op when no rule crosses the tile.

        The tile is a DISPLAY-space crop of the cover (upright even on a /Rotate
        page -- the lift crops ``page_rgb`` at the cover's DISPLAY bounds). So the
        rule geometry is taken in DISPLAY space against the cover's DISPLAY rect;
        mapping via the text-space cover instead would TRANSPOSE the mask on a
        rotated page (a /Rotate 270 cover is tall+narrow in text but the tile is
        wide+short), which is why the strip silently missed the rule before."""
        page = self.working[page_index]
        rm = page.rotation_matrix
        tx0, ty0, tx1, ty1 = text_rect
        pts = [fitz.Point(px, py) * rm for px, py in
               ((tx0, ty0), (tx1, ty0), (tx1, ty1), (tx0, ty1))]
        dx0, dy0 = min(p.x for p in pts), min(p.y for p in pts)
        dx1, dy1 = max(p.x for p in pts), max(p.y for p in pts)
        h, v = self._cached_border_lines(page_index)        # DISPLAY space
        rects = []
        for (bx0, by0, bx1, by1) in (list(h) + list(v)):
            if (bx1 - bx0) <= (by1 - by0):                  # vertical-ish: pad in x
                bx0, bx1 = bx0 - 1.0, bx1 + 1.0
            else:                                           # horizontal-ish: pad in y
                by0, by1 = by0 - 1.0, by1 + 1.0
            ix0, iy0 = max(dx0, bx0), max(dy0, by0)
            ix1, iy1 = min(dx1, bx1), min(dy1, by1)
            if ix1 - ix0 > 0.05 and iy1 - iy0 > 0.05:
                rects.append((ix0, iy0, ix1, iy1))
        dlog("BORDER", "strip", page=page_index + 1, n_rule=len(rects),
             tile_disp=(round(dx0, 1), round(dy0, 1), round(dx1, 1), round(dy1, 1)))
        if not rects:
            return png_bytes
        try:
            import cv2
            import numpy as np
            arr = cv2.imdecode(np.frombuffer(png_bytes, np.uint8),
                               cv2.IMREAD_COLOR)
            if arr is None:
                return png_bytes
            Hh, Ww = arr.shape[:2]
            sx = Ww / max(dx1 - dx0, 1e-6)
            sy = Hh / max(dy1 - dy0, 1e-6)
            mask = np.zeros((Hh, Ww), np.uint8)
            for (ix0, iy0, ix1, iy1) in rects:
                px0 = max(0, int((ix0 - dx0) * sx) - 1)
                px1 = min(Ww, int(round((ix1 - dx0) * sx)) + 1)
                py0 = max(0, int((iy0 - dy0) * sy) - 1)
                py1 = min(Hh, int(round((iy1 - dy0) * sy)) + 1)
                if px1 > px0 and py1 > py0:
                    mask[py0:py1, px0:px1] = 255
            if not mask.any():
                return png_bytes
            out = cv2.inpaint(arr, mask, 3, cv2.INPAINT_TELEA)
            ok, buf = cv2.imencode(".png", out)
            return buf.tobytes() if ok else png_bytes
        except Exception:
            return png_bytes

    def _crossed_char_range(self, box):
        """The contiguous index range ``(i0, i1)`` of the glyphs a table rule crosses
        in ``box``, or None. The per-character ``char_boxes`` (scan-region px) are
        intersected with the rule lines mapped into that same display crop. The move
        path routes exactly these glyphs through the in-place EDIT pipeline so they
        re-render clean from the matched font, instead of carrying the scarred rule."""
        if not (box.cover and len(box.cover) == 7):
            return None
        pi = box.page_index
        h, v = self._cached_border_lines(pi)
        if not (h or v):
            return None
        page = self.working[pi]
        rm = page.rotation_matrix
        tx0, ty0, tx1, ty1 = box.cover[:4]
        pts = [fitz.Point(px, py) * rm for px, py in
               ((tx0, ty0), (tx1, ty0), (tx1, ty1), (tx0, ty1))]
        dx0, dy0 = min(p.x for p in pts), min(p.y for p in pts)
        dx1, dy1 = max(p.x for p in pts), max(p.y for p in pts)
        ctx = self.scan_edit_context(box, box.text)
        geom = (ctx or {}).get("geom") or {}
        cb = geom.get("char_boxes")
        text = (ctx or {}).get("orig_text") or box.text
        if ctx is None or not cb or len(cb) != len(text):
            return None
        ppi = ctx["ppi"]
        bxr = []
        for (bx0, by0, bx1, by1) in (list(h) + list(v)):
            if (bx1 - bx0) <= (by1 - by0):                   # vertical-ish: pad in x
                bx0, bx1 = bx0 - 1.0, bx1 + 1.0
            else:                                            # horizontal-ish: pad in y
                by0, by1 = by0 - 1.0, by1 + 1.0
            ix0, ix1 = max(dx0, bx0), min(dx1, bx1)
            iy0, iy1 = max(dy0, by0), min(dy1, by1)
            if ix1 - ix0 > 0.05 and iy1 - iy0 > 0.05:
                bxr.append(((ix0 - dx0) * ppi, (ix1 - dx0) * ppi))   # region px
        if not bxr:
            return None
        hits = [i for i, (cx0, cy0, cx1, cy1) in enumerate(cb)
                if text[i].strip()
                and any(cx1 > b0 and cx0 < b1 for (b0, b1) in bxr)]
        if not hits:
            return None
        return (min(hits), max(hits))

    def _restore_line_in_cover(self, page, cover_rect, page_index: int) -> None:
        """SURGICAL table-line fix: a moved box that sat ON a rule leaves the rule
        with a gap in the box's footprint after the vacate. Redraw ONLY the rule
        segment(s) that cross THIS cover, clipped to the cover, at the rule's measured
        scan ink width -- so the line stays continuous WITHOUT redrawing any other
        line on the page. No rule crossing this cover -> no-op."""
        h, v = self._borders_text_space(page_index)
        if not (h or v):
            return
        try:
            import cv2
            import numpy as np
            ppi = 300.0 / 72.0
            rm = self.working[page_index].rotation_matrix
            der = self.working[page_index].derotation_matrix
            rot = self.working[page_index].rotation

            def disp(rect):
                p = [fitz.Point(px, py) * rm for px, py in
                     ((rect[0], rect[1]), (rect[2], rect[1]),
                      (rect[2], rect[3]), (rect[0], rect[3]))]
                return (min(q.x for q in p), min(q.y for q in p),
                        max(q.x for q in p), max(q.y for q in p))

            ccx0, ccy0, ccx1, ccy1 = disp(cover_rect)
            scan = None
            for rule in (list(h) + list(v)):
                rdx0, rdy0, rdx1, rdy1 = disp(rule)
                ix0, iy0 = max(rdx0, ccx0), max(rdy0, ccy0)
                ix1, iy1 = min(rdx1, ccx1), min(rdy1, ccy1)
                if ix1 - ix0 < 0.1 or iy1 - iy0 < 0.1:
                    continue                          # rule does not cross this cover
                vertical = (rdx1 - rdx0) <= (rdy1 - rdy0)
                if scan is None:
                    scan = self.render_page_image(page_index, 300.0)
                    Hs, Ws = scan.shape[:2]
                # Measure the rule's ink band + colour from its FULL length (mostly
                # clean outside the cover; a glyph on it is a low fraction, excluded).
                pad = 4.0
                mx0 = max(0.0, rdx0 - (pad if vertical else 0.0))
                my0 = max(0.0, rdy0 - (0.0 if vertical else pad))
                mx1, my1 = rdx1 + (pad if vertical else 0.0), \
                    rdy1 + (0.0 if vertical else pad)
                msx0, msy0 = int(mx0 * ppi), int(my0 * ppi)
                msx1, msy1 = min(Ws, int(round(mx1 * ppi))), \
                    min(Hs, int(round(my1 * ppi)))
                if msx1 - msx0 < 1 or msy1 - msy0 < 1:
                    continue
                ms = scan[msy0:msy1, msx0:msx1]
                dm = ms.mean(2) < 130
                col = np.array([33, 33, 33], np.uint8)
                dk = ms.reshape(-1, 3)[dm.reshape(-1)]
                if len(dk) >= 5:
                    col = np.median(dk, axis=0).astype(np.uint8)
                if vertical:
                    bi = np.where(dm.mean(0) > 0.35)[0]
                    if not bi.size:
                        continue
                    blo, bhi = mx0 + bi.min() / ppi, mx0 + (bi.max() + 1) / ppi
                    dwx0, dwx1, dwy0, dwy1 = blo, bhi, iy0, iy1   # band x, cover y
                else:
                    bi = np.where(dm.mean(1) > 0.35)[0]
                    if not bi.size:
                        continue
                    blo, bhi = my0 + bi.min() / ppi, my0 + (bi.max() + 1) / ppi
                    dwx0, dwx1, dwy0, dwy1 = ix0, ix1, blo, bhi   # cover x, band y
                Ww = max(1, int(round((dwx1 - dwx0) * ppi)))
                Hh = max(1, int(round((dwy1 - dwy0) * ppi)))
                bar = np.empty((Hh, Ww, 3), np.uint8)
                bar[:] = col
                wp = [fitz.Point(px, py) * der for px, py in
                      ((dwx0, dwy0), (dwx1, dwy0), (dwx1, dwy1), (dwx0, dwy1))]
                wtx0, wty0 = min(p.x for p in wp), min(p.y for p in wp)
                wtx1, wty1 = max(p.x for p in wp), max(p.y for p in wp)
                ok, buf = cv2.imencode(".png", cv2.cvtColor(bar, cv2.COLOR_RGB2BGR))
                if ok:
                    page.insert_image(fitz.Rect(wtx0, wty0, wtx1, wty1),
                                      stream=buf.tobytes(), keep_proportion=False,
                                      rotate=rot)
        except Exception:
            pass

    def _sample_scan_region(self, box: "NewBox", dpi: float = 300.0):
        """Recover ``(ink, paper, severity)`` from the SCAN under ``box.cover`` --
        the colour + damage signal an edit must match. The cover is text space; map
        it through the page rotation matrix to the DISPLAY-space render so it works
        on a /Rotate page too (the old raster bailed on rotation, which is why
        rotated edits never blended). None when the region is too small."""
        import numpy as np
        cover = box.cover
        if not (cover and len(cover) == 7):
            return None
        try:
            import cv2
            from .ocr import degrade
            x0, y0, x1, y1 = (float(c) for c in cover[:4])
            ppi = dpi / 72.0
            page = self.working[box.page_index]
            page_rgb = self.render_page_image(box.page_index, dpi)   # display space
            H, W = page_rgb.shape[:2]
            rot = page.rotation_matrix                               # text -> display
            pts = [fitz.Point(px, py) * rot
                   for px, py in ((x0, y0), (x1, y0), (x1, y1), (x0, y1))]
            rx0 = max(0, int(min(p.x for p in pts) * ppi))
            ry0 = max(0, int(min(p.y for p in pts) * ppi))
            rx1 = min(W, int(max(p.x for p in pts) * ppi))
            ry1 = min(H, int(max(p.y for p in pts) * ppi))
            region = page_rgb[ry0:ry1, rx0:rx1]
            if region.size == 0 or region.shape[0] < 4 or region.shape[1] < 6:
                return None
            g = region.mean(2)
            dark = g < float(np.percentile(g, 85)) * 0.7            # scanned ink pixels
            ink, paper = degrade.sample_ink_paper(region, dark)
            # If the cover has no real ink (blank/sparse region), sampling returns
            # ~paper, which would render the edit INVISIBLE (paper on paper). Floor
            # the ink to a dark tone so an edit over a sparse cover stays legible;
            # when real ink is present this is well below the sampled value and never
            # fires, so a true colour match is preserved.
            if float(ink.mean()) > float(paper.mean()) * 0.72:
                ink = (paper * 0.18).astype(np.float32)
            m = dark.astype(np.uint8)
            if int(m.sum()) >= 8:                                   # dropout -> severity
                closed = cv2.morphologyEx(m, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))
                sev = float(np.clip((int(closed.sum()) - int(m.sum())) /
                                    max(int(closed.sum()), 1), 0.05, 0.6))
            else:
                sev = 0.25
            # The ORIGINAL ink's vertical band as a fraction of the cover height: the
            # edit is sized + positioned to match THIS, not the padded box, so it has
            # the same x-height/cap-height as the word it replaces (the em estimate is
            # unreliable -- often 50% too big). Rows that actually carry ink.
            rows = np.where(dark.any(1))[0]
            Hr = dark.shape[0]
            if len(rows) >= 2 and Hr > 0:
                ink_vfrac = (float(rows.min()) / Hr, float(rows.max() + 1) / Hr)
            else:
                ink_vfrac = (0.0, 1.0)
            return ink, paper, sev, ink_vfrac
        except Exception:
            return None

    def _edit_font_file(self, family: str, bold: bool = False,
                        italic: bool = False) -> "str | None":
        """The TTF to render an edit in, in the requested STYLE: the matched BANK
        face -- or its real bold/italic SIBLING file -- if the box uses one, else
        the bundled family's matching variant file. Emphasis ALWAYS comes from a
        real variant FILE (never a synthetic embolden/slant); when no sibling
        exists we keep the base face un-emphasized. None when nothing is on disk.
        The default (bold=italic=False) reproduces the old single-arg behaviour."""
        from .ocr import fontbank, fontmatch
        fpath = fontbank.font_file_for(family)
        if fpath is not None:
            if bold or italic:
                sib = fontbank.variant_file_for(fpath, bold, italic)
                if sib:
                    fpath = sib
            return fpath if os.path.exists(fpath) else None
        # Bundled family (Tinos/Arimo/Cousine) variant file.
        fpath = fontmatch.variant_file_for(family, bold, italic)
        if fpath is None:
            cand = fontmatch._CANDIDATES.get(family)
            # Guard: an empty candidate would join to the DIR itself (a directory,
            # which os.path.exists passes) and shadow the system fallback below.
            fpath = os.path.join(fontmatch._DIR, cand) if cand else None
        if fpath and os.path.exists(fpath):
            return fpath
        # A user-PICKED system family (so font-picking on a scan box works for any
        # installed face, not just the bundled three): resolve via the font engine,
        # extracting the exact .ttc face when needed so the bold/italic FILE is right.
        try:
            rec = self.font_engine.system_record_for(family, bool(bold), bool(italic))
            if rec is not None:
                fp = self._face_file(rec)
                if fp and os.path.exists(fp):
                    return fp
        except Exception:
            pass
        return None

    def _face_file(self, rec) -> "str | None":
        """A fitz-loadable SINGLE-face file for a system face record. A plain
        .ttf/.otf face 0 is used directly; a .ttc or non-zero face index has its
        bytes extracted (font_engine.face_bytes) to a cached temp .ttf, so the
        bold/italic face loads correctly instead of always .ttc face 0."""
        p = getattr(rec, "path", None)
        if not p:
            return None
        idx = int(getattr(rec, "face_index", 0) or 0)
        if idx == 0 and p.lower().endswith((".ttf", ".otf")):
            return p if os.path.exists(p) else None
        cache = self.__dict__.setdefault("_face_file_cache", {})
        key = (p, idx)
        if key in cache and os.path.exists(cache[key]):
            return cache[key]
        try:
            import tempfile, hashlib
            from .font_engine import face_bytes
            data = face_bytes(p, idx)
            h = hashlib.md5(f"{p}:{idx}".encode()).hexdigest()[:12]
            out = os.path.join(tempfile.gettempdir(), f"pdfte_face_{h}.ttf")
            with open(out, "wb") as fh:
                fh.write(data)
            cache[key] = out
            return out
        except Exception:
            return p if os.path.exists(p) else None

    def _render_text_px(self, f, text, em, ppi):
        """Render ``text`` at ``em`` and return (pixmap_rgb, ink_rows, ink_cols,
        baseline_px). Baseline is at em*2 in the tile."""
        import numpy as np
        runw = f.text_length(text, em)
        doc = fitz.open()
        pg = doc.new_page(width=runw + 2 * em, height=em * 3)
        tw = fitz.TextWriter(pg.rect)
        tw.append((em, em * 2.0), text, font=f, fontsize=em)
        tw.write_text(pg, color=(0.06, 0.06, 0.06))
        pm = pg.get_pixmap(matrix=fitz.Matrix(ppi, ppi), alpha=False)
        a = np.frombuffer(pm.samples, np.uint8).reshape(
            pm.height, pm.width, pm.n)[..., :3].copy()
        cov = (255 - a.mean(2)) / 255.0
        ys = np.where(cov.max(1) > 0.12)[0]
        xs = np.where(cov.max(0) > 0.12)[0]
        return a, ys, xs, em * 2.0 * ppi

    def _scanned_edit_raster(self, box: "NewBox", new_text: str, orig_text: str = ""):
        """Render an edited single scanned word to LOOK LIKE THE SCAN: recoloured to
        the scan's ink/paper, per-pixel degraded, SIZED so its em reproduces the
        ORIGINAL word's measured ink extent (using the original word's OWN letters,
        so ascenders/descenders cancel and the edit isn't over/under-sized), and
        PLACED on the original baseline. Returns (png, rect) or None."""
        import numpy as np
        cover = box.cover
        if not (cover and len(cover) == 7) or not new_text.strip():
            return None
        try:
            import cv2
            from .ocr import degrade
            x0, y0, x1, y1 = (float(c) for c in cover[:4])
            fpath = self._edit_font_file(box.font_family)
            if fpath is None:
                return None
            samp = self._sample_scan_region(box)
            if samp is None:
                return None
            ink, paper, sev, ink_vfrac = samp
            ppi = 300.0 / 72.0
            f = fitz.Font(fontfile=fpath)
            fy0, fy1 = ink_vfrac
            cov_h = y1 - y0
            rotated = self.page_rotation(box.page_index) % 360 != 0
            dlog("RENDER", "edit_raster_fallback", rotated=rotated, new=new_text)
            orig_ink_h = max(1.0, (fy1 - fy0) * cov_h)   # original ink extent (pt)
            # The em that reproduces the original word's ink extent, measured with the
            # ORIGINAL word's letters so its ascender/descender profile is matched.
            ot = (orig_text or "").strip()
            if ot:
                _, oys, _, _ = self._render_text_px(f, ot, 100.0, ppi)
                ratio = ((oys.max() - oys.min() + 1) / ppi / 100.0) if len(oys) else 0.0
                em = orig_ink_h / ratio if ratio > 1e-3 else float(box.size)
            else:
                em = float(box.size)
            em = float(np.clip(em, 5.0, 200.0))
            wr0, ys, xs, by_px = self._render_text_px(f, new_text, em, ppi)
            if not len(ys) or not len(xs):
                return None
            asc_pt = (by_px - ys.min()) / ppi            # edit ascender above baseline
            desc_pt = ((ys.max() + 1) - by_px) / ppi     # edit descender below
            w_pt = (xs.max() - xs.min() + 1) / ppi
            wr = wr0[ys.min():ys.max() + 1, xs.min():xs.max() + 1]
            deg = degrade.degrade_patch(wr, ink, paper, sev,
                                        seed=box.box_id * 131 + len(new_text))
            ok, buf = cv2.imencode(".png", cv2.cvtColor(deg, cv2.COLOR_RGB2BGR))
            if not ok:
                return None
            if not rotated:
                # Place on the recovered baseline; descenders hang below it.
                baseline_y = float(box.origin[1])
                return bytes(buf), (x0, baseline_y - asc_pt, x0 + w_pt,
                                    baseline_y + desc_pt)
            # Rotated: fall back to the cover ink band (baseline geometry would need
            # the writing direction; not yet handled there).
            ty0 = y0 + fy0 * cov_h
            th = max(1.0, (fy1 - fy0) * cov_h)
            aspect = wr.shape[1] / max(wr.shape[0], 1)
            return bytes(buf), (x0, ty0, x0 + min(x1 - x0, aspect * th), ty0 + th)
        except Exception:
            return None

    def _scanned_composite_raster(self, box: "NewBox", new_text: str,
                                  orig_text: str = ""):
        """Render an edited single scanned LINE by KEEPING the original scanned
        pixels of every word the user did NOT change and synthesizing ONLY the
        words that changed, then reflowing them on the original baseline with the
        measured inter-word spacing (so deleting a word closes the gap cleanly).

        The kept words ARE the real scan, so they blend perfectly; a synthesized
        word is recoloured to the scan's ink and degraded only to the line's OWN
        local severity, so a clean line yields a clean edit. Word-level keeps the
        kept/synth seams in the whitespace between words, where they are invisible.

        The original word boxes are recovered on the fly by re-segmenting the
        cover region with the original text (``ocr/segment.py``) -- the same code
        the OCR used -- so nothing extra has to be threaded through the box.
        Non-rotated pages only; returns (png, rect), or None to make the caller
        fall back to the whole-line re-render (which handles rotation)."""
        import numpy as np
        cover = box.cover
        ot = (orig_text or "").strip()
        if not (cover and len(cover) == 7) or not new_text.strip() or not ot:
            return None
        if self.page_rotation(box.page_index) % 360 != 0:
            return None                          # rotated -> caller falls back
        try:
            import cv2
            import difflib
            from .ocr import degrade
            from .ocr.segment import segment_line
            x0, y0, x1, y1 = (float(c) for c in cover[:4])
            fpath = self._edit_font_file(box.font_family)
            if fpath is None:
                return None
            ppi = 300.0 / 72.0
            page_rgb = self.render_page_image(box.page_index, 300.0)  # text==display
            H, W = page_rgb.shape[:2]
            rx0, ry0 = max(0, int(round(x0 * ppi))), max(0, int(round(y0 * ppi)))
            rx1, ry1 = min(W, int(round(x1 * ppi))), min(H, int(round(y1 * ppi)))
            if rx1 - rx0 < 6 or ry1 - ry0 < 4:
                return None
            region = np.ascontiguousarray(page_rgb[ry0:ry1, rx0:rx1])
            Hr, Wr = region.shape[:2]
            # Locate each ORIGINAL word's ink box by re-segmenting with the old text.
            seg = segment_line(region, ot)
            if seg is None or not seg.words:
                return None
            base_y = float(seg.baseline_y)
            # Recover ink / paper colour + local damage severity from THIS line.
            gmean = region.mean(2)
            dark = gmean < float(np.percentile(gmean, 85)) * 0.7
            ink, paper = degrade.sample_ink_paper(region, dark)
            if float(ink.mean()) > float(paper.mean()) * 0.72:
                ink = (paper * 0.18).astype(np.float32)
            # Local damage of THIS line (its own neighbours). local_severity needs
            # the INTENDED ink footprint (where ink should be), so close the dark
            # mask to fill toner dropout, then it measures how much of that footprint
            # has FADED: ~0.05 for crisp text, high for a faxy line. This is what
            # keeps a synth word CLEAN when the words around it are clean (the ask).
            try:
                footprint = cv2.morphologyEx(dark.astype(np.uint8), cv2.MORPH_CLOSE,
                                             np.ones((5, 5), np.uint8)) > 0
                sev = float(degrade.local_severity(region, footprint, (0, 0, Wr, Hr)))
            except Exception:
                sev = 0.06
            # em that reproduces the original line's ink height, measured with the
            # line's OWN letters so its ascender/descender profile is matched.
            f = fitz.Font(fontfile=fpath)
            rows = np.where(dark.any(1))[0]
            orig_ink_h = (float(rows.max() - rows.min() + 1) / ppi
                          if len(rows) >= 2 else (y1 - y0) * 0.7)
            _, oys, _, _ = self._render_text_px(f, ot, 100.0, ppi)
            ratio = ((oys.max() - oys.min() + 1) / ppi / 100.0) if len(oys) else 0.0
            em = float(np.clip(orig_ink_h / ratio if ratio > 1e-3 else float(box.size),
                               5.0, 200.0))
            # Paper-coloured tile the size of the region; kept words paste their real
            # scan pixels, synth words paste their own recoloured+degraded patch.
            paper_u8 = np.clip(paper, 0, 255).astype(np.uint8)
            tile = np.empty((Hr, Wr, 3), np.uint8)
            tile[:] = paper_u8

            def blit(src, x, y):
                sh, sw = src.shape[:2]
                sx0, sy0 = max(0, -x), max(0, -y)
                dx0, dy0 = max(0, x), max(0, y)
                w = min(sw - sx0, Wr - dx0)
                h = min(sh - sy0, Hr - dy0)
                if w > 0 and h > 0:
                    tile[dy0:dy0 + h, dx0:dx0 + w] = src[sy0:sy0 + h, sx0:sx0 + w]

            # Word-level diff: KEEP unchanged words (their real pixels), SYNTH the
            # changed/new ones. A 'delete' just drops the word; reflow closes the gap.
            orig_tokens, new_tokens = ot.split(), new_text.split()
            sm = difflib.SequenceMatcher(a=orig_tokens, b=new_tokens, autojunk=False)
            placements = []                      # ('keep', WordBox) | ('synth', str)
            for tag, i1, i2, j1, j2 in sm.get_opcodes():
                if tag == "equal":
                    for off in range(i2 - i1):
                        a, b = i1 + off, j1 + off
                        if a < len(seg.words) and seg.words[a].text == orig_tokens[a]:
                            placements.append(("keep", seg.words[a]))
                        else:
                            placements.append(("synth", new_tokens[b]))
                elif tag in ("replace", "insert"):
                    for b in range(j1, j2):
                        placements.append(("synth", new_tokens[b]))
                # 'delete' -> contributes nothing
            if not placements:
                return None
            # Lay the words out left to right, starting at the original line's left
            # edge, advancing by word width + the measured inter-word space.
            space_px = (seg.space_px if seg.space_px and seg.space_px > 1
                        else 0.25 * em * ppi)
            x_cursor = float(min(w.x0 for w in seg.words))
            for idx, (kind, item) in enumerate(placements):
                if kind == "keep":
                    w = item
                    wx0, wx1 = max(0, int(w.x0)), min(Wr, int(max(w.x1, w.x0 + 1)))
                    wt, wb = max(0, int(w.top)), min(Hr, int(max(w.bottom, w.top + 1)))
                    if wx1 <= wx0 or wb <= wt:
                        continue
                    patch = region[wt:wb, wx0:wx1]
                    blit(patch, int(round(x_cursor)), wt)   # keep its baseline row
                    x_cursor = round(x_cursor) + patch.shape[1] + space_px
                else:
                    wr0, ys, xs, by_px = self._render_text_px(f, item, em, ppi)
                    if not len(ys) or not len(xs):
                        x_cursor += space_px
                        continue
                    crop = wr0[ys.min():ys.max() + 1, xs.min():xs.max() + 1]
                    if sev <= 0.12:
                        # Clean neighbours -> just recolour to the scan ink, KEEPING
                        # the antialiasing. No hard-degrade: that aliases + thickens
                        # the glyph, making an edit look damaged next to crisp text.
                        cov = ((255.0 - crop.mean(2)) / 255.0)[..., None]
                        deg = np.clip(
                            paper.reshape(1, 1, 3) * (1.0 - cov) +
                            ink.reshape(1, 1, 3) * cov, 0, 255).astype(np.uint8)
                    else:
                        deg = degrade.degrade_patch(
                            crop, ink, paper, sev,
                            seed=box.box_id * 131 + idx * 17 + len(item) * 7)
                    dy0 = int(round(base_y - (by_px - ys.min())))
                    blit(deg, int(round(x_cursor)), dy0)
                    x_cursor = round(x_cursor) + deg.shape[1] + space_px
            ok, buf = cv2.imencode(".png", cv2.cvtColor(tile, cv2.COLOR_RGB2BGR))
            if not ok:
                return None
            return bytes(buf), (rx0 / ppi, ry0 / ppi, rx1 / ppi, ry1 / ppi)
        except Exception:
            return None

    def measure_box_glyphs(self, box: "NewBox", page_rgb=None) -> "dict | None":
        """MEASURE every scanned glyph in a box ONCE: render the page, segment each
        line, and record per-line the character x-edges + the line's vertical band, all
        in DISPLAY points. This is the fixed geometry the custom caret engine maps
        clicks against and draws the caret on; typed characters are folded in later by
        cheap arithmetic (no re-segmentation). Returns ``{"lines": [per-line dict or
        None]}`` or None."""
        line_covers = tuple(getattr(box, "line_covers", ()) or ())
        if not line_covers:
            if box.cover and len(box.cover) == 7:
                line_covers = (tuple(box.cover),)
            else:
                return None
        orig_lines = (getattr(box, "ocr_text", "") or box.text or "").split("\n")
        try:
            if page_rgb is None:
                page_rgb = self.render_page_image(box.page_index, 300.0)
        except Exception:
            return None
        # The matched font measures the gap characters between glyph anchors (its
        # proportional advances, scaled to the ink span). Load it ONCE for the box.
        font = None
        try:
            fp = self._edit_font_file(getattr(box, "font_family", ""))
            if fp:
                font = fitz.Font(fontfile=fp)
        except Exception:
            font = None
        lines = []
        for i, lc in enumerate(line_covers):
            ot_line = (orig_lines[i] if i < len(orig_lines) else "").strip()
            ctx = self.scan_edit_context(box, cover_override=lc,
                                         text_override=ot_line, page_rgb=page_rgb)
            if ctx is None:
                lines.append(None)
                continue
            ppi = ctx["ppi"]
            dx0, dy0, dx1, dy1 = ctx["disp_rect"]
            edges_px = self._char_edges(ctx, ot_line, font)
            lines.append({
                "text": ot_line,
                "edges": [dx0 + e / ppi for e in edges_px],   # display points
                "top": dy0, "bottom": dy1,
                "baseline": dy0 + ctx["base_y"] / ppi,
                "em": ctx["em"], "ppi": ppi, "x0_disp": dx0,
                # Keep the per-line context so the LIVE composite reuses it (no
                # re-segment / re-match per keystroke -- the scan doesn't change as you
                # type). The local-font match also caches ON this ctx, so it runs once
                # per line, not once per keystroke.
                "ctx": ctx,
            })
        return {"lines": lines} if any(lines) else None

    def reflow_scan_words(self, box: "NewBox", box_w_pts: float, page_rgb=None):
        """Re-wrap a scanned OCR box's WORDS to a new column width, KEEPING each word's
        scanned pixels -- only the line breaks change; glyph size + look stay. Crops each
        recognized word from the scan (via the char-box map), greedily fills lines to
        ``box_w_pts`` wide, and re-lays the crops on a uniform per-line baseline. Returns
        ``(png_bytes, text-space rect)`` to drop in as the box's raster overlay, or None
        when the word map is unavailable."""
        import numpy as np
        import cv2
        meas = self.measure_box_glyphs(box, page_rgb=page_rgb)
        if not meas:
            return None
        ppi = 300.0 / 72.0
        words, spaces, baselines = [], [], []
        for ln in (meas.get("lines") or []):
            if not ln:
                continue
            ctx = ln.get("ctx") or {}
            region = ctx.get("region")
            cb = ((ctx.get("geom") or {}).get("char_boxes"))
            ot = ctx.get("orig_text", "")
            base = ctx.get("base_y")
            if region is None or not cb or len(cb) != len(ot) or base is None:
                return None
            bln = ln.get("baseline")
            if bln is not None:
                baselines.append(float(bln))
            # Crop each word to its line's OWN ink band -- the contiguous band the baseline
            # sits in -- NOT the full line-cover crop. A loose cover overlaps the next line,
            # and carrying that overlap is exactly what dragged the neighbouring line's
            # pixels into the word and inflated the spacing.
            bt_band, bb_band = self._line_band(region, base)
            i, n = 0, len(ot)
            while i < n:
                if ot[i] == " ":
                    i += 1
                    continue
                j = i
                while j < n and ot[j] != " ":
                    j += 1
                if cb[i] and cb[j - 1]:
                    bx0 = max(0, int(cb[i][0]))
                    bx1 = min(region.shape[1], int(cb[j - 1][2]))
                    if bx1 > bx0:
                        words.append({
                            "crop": np.ascontiguousarray(region[bt_band:bb_band, bx0:bx1]),
                            "w": bx1 - bx0,
                            "asc": float(base - bt_band),
                            "desc": float(bb_band - base)})
                        k = j
                        while k < n and ot[k] == " ":
                            k += 1
                        if k < n and cb[k]:
                            g = float(cb[k][0]) - float(cb[j - 1][2])
                            if g > 0:
                                spaces.append(g)
                i = j
        if not words:
            return None
        space_px = float(np.median(spaces)) if spaces else 0.3 * ppi * float(box.size)
        box_w_px = max(int(round(box_w_pts * ppi)), max(w["w"] for w in words))
        lines, cur, cx = [], [], 0.0
        for w in words:                                   # greedy wrap to width
            gap = space_px if cur else 0.0
            if cur and cx + gap + w["w"] > box_w_px:
                lines.append(cur)
                cur, cx, gap = [], 0.0, 0.0
            x = cx + gap
            cur.append((x, w))
            cx = x + w["w"]
        if cur:
            lines.append(cur)
        asc = max(w["asc"] for w in words)                # tallest ascent / deepest descent
        desc = max(w["desc"] for w in words)
        # Line pitch = the scan's OWN baseline-to-baseline distance, straight from the map,
        # so reflowed lines sit exactly as tight as they did in the scan (not the inflated
        # line-cover height). Fall back to the glyph extent only for a single line, which
        # gives no pitch to measure.
        if len(baselines) >= 2:
            bl = sorted(baselines)
            lead = int(round(float(np.median(
                [bl[i + 1] - bl[i] for i in range(len(bl) - 1)])) * ppi))
        else:
            lead = int(round(asc + desc))
        lead = max(1, lead)
        H = max(1, int(round(lead * (len(lines) - 1) + asc + desc)))
        W = max(1, box_w_px)
        bg = box.cover[4:7] if (box.cover and len(box.cover) == 7) else (1.0, 1.0, 1.0)
        canvas = np.empty((H, W, 3), np.uint8)
        canvas[:] = np.array([int(round(c * 255)) for c in bg], np.uint8)
        for li, lwords in enumerate(lines):
            base_y = li * lead + asc
            for (x, w) in lwords:
                crop = w["crop"]
                ch, cw = crop.shape[:2]
                top, x0 = int(round(base_y - w["asc"])), int(round(x))
                y0, y1 = max(0, top), min(H, top + ch)
                x1 = min(W, x0 + cw)
                if y1 > y0 and x1 > max(0, x0):
                    canvas[y0:y1, max(0, x0):x1] = crop[y0 - top:y1 - top,
                                                        max(0, -x0):x1 - x0]
        ok, buf = cv2.imencode(".png", cv2.cvtColor(canvas, cv2.COLOR_RGB2BGR))
        if not ok:
            return None
        # Text-space rect: place the reflowed raster at the box's cover display top-left,
        # sized W x H display px, mapped back to text space (same derotation the bake uses).
        page = self.working[box.page_index]
        cv = box.cover
        cpts = [fitz.Point(px, py) * page.rotation_matrix for px, py in
                ((cv[0], cv[1]), (cv[2], cv[1]), (cv[2], cv[3]), (cv[0], cv[3]))]
        dx0, dy0 = min(p.x for p in cpts), min(p.y for p in cpts)
        disp = ((dx0, dy0), (dx0 + W / ppi, dy0), (dx0 + W / ppi, dy0 + H / ppi),
                (dx0, dy0 + H / ppi))
        tpts = [fitz.Point(px, py) * page.derotation_matrix for px, py in disp]
        rect = (min(p.x for p in tpts), min(p.y for p in tpts),
                max(p.x for p in tpts), max(p.y for p in tpts))
        return bytes(buf), rect

    def _tight_cover(self, page_index: int, cover, page_rgb=None, nlines=1,
                     cross_only=False):
        """Shrink a scanned line/area COVER to its ACTUAL ink, per box -- so a line with
        no descenders has no blank 'chin' below it and adjacent boxes stop overlapping.
        Measured from the scan (not a fixed shave): crop the cover's display box, split the
        ink into horizontal bands, keep the box's own `nlines` text lines and drop only the
        small bleed bands an over-tall cover caught from the adjacent line, then map the
        tight box back to text space. `nlines` is the anchor: we never shrink below it, so a
        genuinely short line is never lost. Returns a new cover (same bg tail) or the
        original if it can't measure."""
        import numpy as np
        if not (cover and len(cover) >= 4):
            return cover
        try:
            ppi = 300.0 / 72.0
            if page_rgb is None:
                page_rgb = self.render_page_image(page_index, 300.0)
            H, W = page_rgb.shape[:2]
            page = self.working[page_index]
            rot, dm = page.rotation_matrix, page.derotation_matrix
            x0, y0, x1, y1 = (float(c) for c in cover[:4])
            pts = [fitz.Point(px, py) * rot for px, py in
                   ((x0, y0), (x1, y0), (x1, y1), (x0, y1))]
            dx0, dy0 = min(p.x for p in pts), min(p.y for p in pts)
            dx1, dy1 = max(p.x for p in pts), max(p.y for p in pts)
            rx0, ry0 = max(0, int(round(dx0 * ppi))), max(0, int(round(dy0 * ppi)))
            rx1, ry1 = min(W, int(round(dx1 * ppi))), min(H, int(round(dy1 * ppi)))
            crop = page_rgb[ry0:ry1, rx0:rx1]
            if crop.size == 0:
                return cover
            dark = crop.mean(2) < 165.0
            colcount = dark.sum(1)                       # ink columns per row
            ch, cw = crop.shape[0], crop.shape[1]
            maxc = int(colcount.max()) or 1
            # NOISE FLOOR for "is this row ink": a faint speck in the inter-line gap (fax dust,
            # a few px) must NOT count as a row -- otherwise it bridges two lines into one band
            # and the cover never tightens off the neighbour (the line-below bleed). Real text
            # and descender rows sit far above this; only stray specks fall below.
            floor = max(2, int(round(0.05 * maxc)))
            rows = np.where(colcount > floor)[0]
            if not rows.size:
                return cover
            # Split the ink into horizontal bands at blank runs between rows.
            gap = max(4, int(round(0.04 * ch)))
            bands = []
            bt = prev = int(rows[0])
            for r in rows[1:]:
                if int(r) - prev > gap:
                    bands.append([bt, prev])
                    bt = int(r)
                prev = int(r)
            bands.append([bt, prev])
            all_bands = [list(b) for b in bands]         # full set, for the midpoint cap below
            max_h = max(b[1] - b[0] + 1 for b in bands)
            # TIGHT: strip extra lines beyond the box's own line count (bleed an over-tall
            # cover caught from a neighbour), dropping the shorter edge first.
            while len(bands) > max(1, int(nlines)):
                h0 = bands[0][1] - bands[0][0] + 1
                h1 = bands[-1][1] - bands[-1][0] + 1
                bands.pop(0 if h0 <= h1 else -1)
            top, bot = bands[0][0], bands[-1][1]
            # DESCENDER EXCEPTION: a tight box must NEVER white out a descender. Below the
            # body, pull the bottom down through this line's own tail ink -- a few NARROW
            # descender glyphs (y/g/p/j) -- stopping at a WIDE row (the next line's body). The
            # reach is HARD-CAPPED at the midpoint to the next ink band: a descender physically
            # cannot cross into the next line, so this excludes the next line's cap tops AND the
            # stray specks in the inter-line gap, while keeping real descenders -- which a
            # colcount threshold cannot do, since a thin descender and a gap speck are the same
            # few pixels.
            reach = min(ch - 1, bot + int(round(0.40 * max_h)))
            nxt = next((b[0] for b in all_bands if b[0] > bot), None)
            if nxt is not None:
                reach = min(reach, (bot + int(nxt)) // 2)
            r = bot + 1
            while r <= reach:
                if colcount[r] > 0.5 * maxc:             # wide row => next line body, stop
                    break
                if colcount[r] > 0:                      # narrow tail ink => descender, keep
                    bot = r
                r += 1
            cols = np.where(dark[top:bot + 1].any(0))[0]
            if not cols.size:
                return cover
            t_dy0 = dy0 + max(0, top - 1) / ppi
            t_dy1 = dy0 + min(ch, bot + 2) / ppi
            if cross_only:
                # Tighten ONLY the cross-line (vertical) extent; keep the line's full reading
                # extent. For per-line covers the horizontal span must stay as OCR mapped it --
                # only the vertical bleed into the line below is the bug.
                t_dx0, t_dx1 = dx0, dx1
            else:
                # HORIZONTAL: snap to the glyph's FULL ink, not the dark-threshold column. The
                # old +1/+2 px shrank onto the last glyph's antialiased edge -- and on a rotated
                # page the crop round-trip ate even that -- so the final letter was sliced off
                # (the cover "cutoff"). Use a FAINT (paper-relative) threshold so the tapered edge
                # counts, plus a stroke-scaled margin, still capped at the ORIGINAL cover so it
                # never grows past OCR's layout (field/paragraph-safe). Vertical stays tight above,
                # which is what keeps stacked lines from merging -- only the reading extent relaxes.
                mgn = max(10, int(round(0.22 * (bot - top + 1))))
                faint = crop.mean(2) < 215.0
                fc = np.where(faint[top:bot + 1].any(0))[0]
                lo = min(int(cols.min()), int(fc.min())) if fc.size else int(cols.min())
                hi = max(int(cols.max()), int(fc.max())) if fc.size else int(cols.max())
                t_dx0 = dx0 + max(0, lo - mgn) / ppi
                t_dx1 = dx0 + min(cw, hi + mgn) / ppi
            tpts = [fitz.Point(px, py) * dm for px, py in
                    ((t_dx0, t_dy0), (t_dx1, t_dy0), (t_dx1, t_dy1), (t_dx0, t_dy1))]
            nx0, ny0 = min(p.x for p in tpts), min(p.y for p in tpts)
            nx1, ny1 = max(p.x for p in tpts), max(p.y for p in tpts)
            return (nx0, ny0, nx1, ny1) + tuple(cover[4:])
        except Exception:
            return cover

    def _line_band(self, region, base_y):
        """Vertical ``(top, bottom)`` of the ONE text line whose baseline is ``base_y``
        within ``region`` (a line-cover crop). A loose cover overlaps the neighbouring
        line; this returns just the contiguous ink band the baseline sits in, so a reflow
        crop carries only its own line. Falls back to the whole region if it can't tell."""
        import numpy as np
        h = region.shape[0]
        try:
            dark = region.mean(2) < 165.0
        except Exception:
            return 0, h
        rows = np.where(dark.any(1))[0]
        if not rows.size:
            return 0, h
        b = int(round(float(base_y)))
        colcount = dark.sum(1)
        maxc = int(colcount.max()) or 1
        gap = max(3, int(round(0.10 * h)))
        bands = []
        bt = prev = int(rows[0])
        for r in rows[1:]:
            if int(r) - prev > gap:
                bands.append([bt, prev])
                bt = int(r)
            prev = int(r)
        bands.append([bt, prev])
        # The band that contains the baseline; else the nearest.
        bi = next((k for k, bd in enumerate(bands) if bd[0] - 2 <= b <= bd[1] + 2), None)
        if bi is None:
            bi = min(range(len(bands)),
                     key=lambda k: min(abs(bands[k][0] - b), abs(bands[k][1] - b)))
        top, bot = bands[bi][0], bands[bi][1]
        bh = bot - top + 1
        # DESCENDER EXCEPTION: pull the bottom down through this line's own NARROW tail ink
        # (a y/g/p/j descender), within a descender's reach, stopping at a WIDE row (the next
        # line's body). No tail ink => stays tight.
        reach = min(h - 1, bot + int(round(0.40 * bh)))
        r = bot + 1
        while r <= reach:
            if colcount[r] > 0.5 * maxc:
                break
            if colcount[r] > 0:
                bot = r
            r += 1
        return max(0, top - 1), min(h, bot + 2)

    def reflow_box(self, page: int, box, box_w_pts: float) -> None:
        """RESIZE = REFLOW: re-wrap a scanned OCR box's words to ``box_w_pts`` (the new
        DISPLAY column width), KEEPING each word's scanned pixels at constant size -- only
        the line breaks change. Builds the re-laid raster, flips the box visible at it,
        and pushes ONE 'reflow' command. The original spot vacates via the cover (like a
        move). No-op when the words can't be mapped."""
        if not isinstance(box, NewBox):
            return
        key = box.edit_key
        before = self._newbox_state(key)
        b = self._new_boxes.get(key)
        if b is None:
            return
        out = self.reflow_scan_words(b, box_w_pts)
        if out is None:
            return
        raster, rect = out
        updated = replace(b, edit_image=raster, edit_image_rect=tuple(rect),
                          render_mode=0, box_w=float(box_w_pts), bbox=tuple(rect))
        after = _BoxState(newbox=updated, exists=True)
        self._push_command(key, None, "reflow", before, after, "Reflow box")

    def respace_box(self, box: "NewBox", page_rgb=None) -> "str | None":
        """Re-quantize ONE box's inter-word spacing from the SCAN, PER BOX. Spaces scale
        with font, so the single-space width is this box's OWN median word gap: measure
        every gap from the char-box MAP (works on any box, incl. merged-fragment prose
        where word boxes are gone), take the trimmed median (wide field gaps dropped as
        outliers), then re-express each gap as ``round(gap / median)`` spaces. Returns the
        re-spaced text (newline-joined) or None when it can't measure or nothing changes.

        Orientation needs no special-casing: each line's crop is taken through the cover
        mapping, which lands it UPRIGHT on a /Rotate page as on a 0deg one, so the map
        segments the line left-to-right either way and the gaps below are reading-order
        distances for every box on the same path."""
        import numpy as np
        meas = self.measure_box_glyphs(box, page_rgb=page_rgb)
        if not meas:
            return None
        line_runs = []          # per line: (text, [(run_start, run_end, gap_px)] or None)
        all_gaps = []
        for ln in (meas.get("lines") or []):
            if not ln:
                line_runs.append(None)
                continue
            cb = ((ln.get("ctx") or {}).get("geom") or {}).get("char_boxes")
            ot = (ln.get("ctx") or {}).get("orig_text", "")
            if not cb or len(cb) != len(ot):
                line_runs.append((ot, None))
                continue
            runs = []
            i, n = 0, len(ot)
            while i < n:
                if ot[i] == " " and i > 0 and cb[i - 1]:
                    j = i
                    while j < n and ot[j] == " ":
                        j += 1
                    if j < n and cb[j]:
                        gap = float(cb[j][0]) - float(cb[i - 1][2])
                        if gap > 0:
                            runs.append((i, j, gap))
                            all_gaps.append(gap)
                    i = j
                else:
                    i += 1
            line_runs.append((ot, runs))
        if len(all_gaps) < 2:
            return None
        a = np.array(all_gaps, dtype=float)         # trimmed median = the box single space
        med = float(np.median(a))
        for _ in range(6):
            small = a[a <= 1.75 * med]
            if not small.size:
                break
            nm = float(np.median(small))
            if abs(nm - med) < 0.5:
                med = nm
                break
            med = nm
        if med <= 1.0:
            return None
        out = []
        for entry in line_runs:
            if entry is None:
                out.append("")
                continue
            ot, runs = entry
            if not runs:
                out.append(ot)
                continue
            res, prev = [], 0
            for (rs, re_, gap) in runs:
                res.append(ot[prev:rs])
                res.append(" " * max(1, int(round(gap / med))))
                prev = re_
            res.append(ot[prev:])
            out.append("".join(res))
        new = "\n".join(out)
        cur = (getattr(box, "ocr_text", "") or box.text or "")
        return new if new != cur else None

    def respace_ocr_text(self, page_index: int, cover, line_covers, text: str,
                         family: str, size: float, direction, page_rgb=None) -> str:
        """Re-space an OCR overlay line/paragraph from the SCAN at apply time.

        Builds a throwaway overlay box from the same params the apply loop already has
        and runs ``respace_box`` on it, which segments each line from the scan's own
        char map and re-quantizes every gap to that box's median space -- the SAME path
        for every box regardless of page rotation. Returns the re-spaced text, or the
        original on any failure."""
        if not (cover and len(cover) == 7) or not (text or "").strip():
            return text
        try:
            b = NewBox(page_index=page_index, box_id=-1, origin=(0.0, 0.0),
                       bbox=tuple(cover[:4]), text=text, font_family=family,
                       size=float(size), color=(0.0, 0.0, 0.0), bold=False,
                       italic=False, cover=tuple(cover), dir=direction,
                       render_mode=3, ocr_text=text)
            if line_covers:
                b.line_covers = tuple(line_covers)
            new = self.respace_box(b, page_rgb=page_rgb)
            return new if new else text
        except Exception:
            return text

    def caret_line_edges(self, measured_line: dict, line_text: str,
                         fpath: "str | None" = None) -> list:
        """Live per-character x-edges (display pts) for ONE line's CURRENT text, from
        the measured SCAN edges: kept characters stay at their scanned x, the changed
        middle is laid out by the font's advances, and the kept suffix shifts by the
        width delta. Pure arithmetic over cached numbers -- no segmentation, no render
        -- so it is cheap enough to run on every keystroke. ``fpath`` is the font used
        to measure typed-character widths (the box / local font)."""
        scan = measured_line["edges"]
        ot = measured_line["text"]
        em = float(measured_line.get("em", 12.0))
        nt = line_text if line_text is not None else ot
        if nt == ot or not scan:
            return list(scan)
        # EXACT edges from the LAST compose of THIS text, if the preview already ran for
        # it -- the caret then sits precisely where inplace_compose placed the glyphs
        # (spaces included). Falls through to the advance arithmetic below only when the
        # composite for the current text has not been computed yet.
        _ctx = measured_line.get("ctx")
        if _ctx:
            _le = _ctx.get("_live_edges")
            if _le and _le[0] == nt and len(_le[1]) == len(nt) + 1:
                return list(_le[1])
        try:
            # Cache the face: this runs on every caret paint/keystroke of an edited
            # line, so a fresh fitz.Font each call would tax typing (the user's "must
            # not slow down writing"). Pure measurement, safe to reuse.
            if fpath:
                cache = self.__dict__.setdefault("_caret_font_cache", {})
                f = cache.get(fpath)
                if f is None:
                    f = cache[fpath] = fitz.Font(fontfile=fpath)
            else:
                f = None
        except Exception:
            f = None
        # common prefix p + common suffix s (in characters), bounded to the scan edges
        p = 0
        lim = min(len(ot), len(nt), len(scan) - 1)
        while p < lim and ot[p] == nt[p]:
            p += 1
        s = 0
        while s < len(ot) - p and s < len(nt) - p and ot[-1 - s] == nt[-1 - s]:
            s += 1
        # Lay the typed middle out with the SAME font/size/condense the compose RENDERS it
        # at (cap-anchored sem * horizontal condense), not the box em -- otherwise the caret
        # drifts from the glyphs it is meant to sit between. Cumulative text_length keeps the
        # font's kerning. Falls back to the box font at em when the synth metrics are absent.
        ctx = measured_line.get("ctx")
        sm = self._synth_metrics(ctx) if ctx else None
        synth_font = sm[0] if sm else None
        sem = sm[4] if sm else em
        condense = sm[5] if sm else 1.0
        xp = scan[p]                                   # prefix ends here (display pt)
        edges = list(scan[:p + 1])                     # kept prefix edges
        mid = nt[p:len(nt) - s]
        if synth_font is not None and mid:
            import numpy as np
            ppi = float(measured_line.get("ppi") or (300.0 / 72.0))
            # MIDDLE WIDTH = the ACTUAL rendered INK width that inplace_compose places,
            # NOT the font advance: a glyph's advance is wider than its ink (a digit
            # especially), so the advance-based caret drifted to the RIGHT of the rendered
            # glyph -- "type a 1 over a 0 and the caret lands in the wrong place". Render
            # the changed middle once and measure its ink span the SAME way the compose
            # crops it (col coverage > 0.20), x condense. Cached by (text, size) so caret
            # blinks/moves don't re-render -- cheap enough for every keystroke.
            key = (mid, round(float(sem), 2))
            cache = self.__dict__.setdefault("_caret_inkw_cache", {})
            ink_w = cache.get(key)
            if ink_w is None:
                try:
                    a, _ys, _xs, _b = self._render_text_px(synth_font, mid, sem, ppi)
                    cov = (255.0 - a.mean(2)) / 255.0
                    cols = np.where(cov.max(0) > 0.20)[0]
                    ink_w = (float(cols.max() - cols.min() + 1) if cols.size
                             else float(synth_font.text_length(mid, sem)) * ppi)
                except Exception:
                    ink_w = float(synth_font.text_length(mid, sem)) * ppi
                cache[key] = ink_w
            synth_w_disp = (ink_w * condense) / ppi    # tile px -> display pts
            adv_total = float(synth_font.text_length(mid, sem)) or 1.0
            for k in range(1, len(mid) + 1):
                frac = float(synth_font.text_length(mid[:k], sem)) / adv_total
                edges.append(xp + synth_w_disp * frac)
        else:
            x = xp
            for ch in mid:
                x += float(f.text_length(ch, em)) if f else 0.6 * em
                edges.append(x)
        x = edges[-1] if edges else xp
        shift = x - scan[len(ot) - s]                  # suffix slides by the width delta
        for j in range(1, s + 1):                      # kept suffix edges, shifted
            edges.append(scan[len(ot) - s + j] + shift)
        return edges

    def scan_edit_context(self, box: "NewBox", orig_text: str = "", *,
                          cover_override: tuple = None,
                          text_override: "str | None" = None,
                          page_rgb=None):
        """Everything needed to edit a scanned LINE in place, computed ONCE (it
        renders the page, so the live editor caches it instead of paying it per
        keystroke). Characters map to x via the MATCHED font's advances -- the scan
        IS that font, so they line up -- anchored at the line's left ink edge and
        the recovered baseline. Returns a dict or None (rotated/no cover/no font).

        ``cover_override`` / ``text_override`` let a caller build the context for
        ONE line of a paragraph (its own cover + its own original text) instead of
        the box's whole cover, and ``page_rgb`` passes an already-rendered page so a
        multi-line block renders the page once, not once per line -- this is what
        lets a paragraph ride the exact single-line engine, one line at a time."""
        import numpy as np
        cover = cover_override if cover_override is not None else box.cover
        # The compose base is the box's ORIGINAL recognized text (what the scan
        # actually shows), so a re-edit of a committed box still diffs against the
        # scan, not the already-edited string. Falls back to current text pre-OCR.
        ot = (text_override if text_override is not None
              else (getattr(box, "ocr_text", "") or orig_text or box.text or "")).strip()
        if not (cover and len(cover) == 7) or not ot:
            return None
        try:
            import cv2
            from .ocr import degrade
            from .ocr.segment import segment_line
            x0, y0, x1, y1 = (float(c) for c in cover[:4])
            fpath = self._edit_font_file(box.font_family)
            if fpath is None:
                return None
            ppi = 300.0 / 72.0
            page = self.working[box.page_index]
            if page_rgb is None:
                page_rgb = self.render_page_image(box.page_index, 300.0)
            H, W = page_rgb.shape[:2]
            # cover is TEXT space; the render is DISPLAY space. Map the cover through
            # the page rotation so the crop lands on the line even on a /Rotate page
            # (the displayed scan is upright, so the cropped line stays horizontal).
            rot = page.rotation_matrix
            pts = [fitz.Point(px, py) * rot
                   for px, py in ((x0, y0), (x1, y0), (x1, y1), (x0, y1))]
            rx0 = max(0, int(round(min(p.x for p in pts) * ppi)))
            ry0 = max(0, int(round(min(p.y for p in pts) * ppi)))
            rx1 = min(W, int(round(max(p.x for p in pts) * ppi)))
            ry1 = min(H, int(round(max(p.y for p in pts) * ppi)))
            if rx1 - rx0 < 6 or ry1 - ry0 < 4:
                return None
            # MOVE OFFSET: a moved OCR box keeps its scan SOURCE (cover) at the ORIGINAL
            # spot (the crop above) but RENDERS at the moved spot. Shift the display rect
            # by the box's accumulated move (edit_image_rect - cover, block-level, text
            # space) mapped through the page rotation, so the tile / caret / live preview
            # all land where the box now is. Zero when un-moved (edit_image_rect empty or
            # still over the cover), so in-place editing is byte-identical to before.
            # DISPLAY-space move of the box (0 for an in-place edit, incl. one that grew
            # wider; nonzero only for a dragged box). Same source of truth as the hide-
            # bake + vacate, so the live preview lands exactly where the bake will.
            odx, ody = self._box_display_move(box) or (0.0, 0.0)
            region = np.ascontiguousarray(page_rgb[ry0:ry1, rx0:rx1])
            Hr, Wr = region.shape[:2]
            # The character-indexed binary map for THIS line's crop + THIS line's text
            # (a paragraph builds one per line, since the caller passes a per-line cover
            # + text_override). It is the single source of truth for the glyph boxes,
            # the caret edges, the baseline/cap height, and the word spacing below.
            geom = self._scan_geometry(region, ot)
            gmean = region.mean(2)
            dark = gmean < float(np.percentile(gmean, 85)) * 0.7
            ink, paper = degrade.sample_ink_paper(region, dark)
            if float(ink.mean()) > float(paper.mean()) * 0.72:
                ink = (paper * 0.18).astype(np.float32)
            try:
                fp = cv2.morphologyEx(dark.astype(np.uint8), cv2.MORPH_CLOSE,
                                      np.ones((5, 5), np.uint8)) > 0
                sev = float(degrade.local_severity(region, fp, (0, 0, Wr, Hr)))
            except Exception:
                sev = 0.06
            rows = np.where(dark.any(1))[0]
            cols = np.where(dark.any(0))[0]
            if not len(rows) or not len(cols):
                return None
            ink_top, ink_bot = int(rows.min()), int(rows.max() + 1)
            left_px = float(cols.min())
            right_px = float(cols.max() + 1)
            f = fitz.Font(fontfile=fpath)
            # The matched font at the OCR-measured size already reproduces the scan
            # (the box HEIGHT drove lb.size), so synth glyphs come out the SAME size
            # as the scanned ones. Deriving em from the dark-mask ink height instead
            # UNDER-sizes them, because the mask clips faint ascender tips.
            em = float(np.clip(float(box.size), 5.0, 200.0))
            # Baseline = the TEXT baseline, NOT the lowest ink: parens "(" ")" and
            # descenders dip below the line, so ink_bot would drop synth glyphs too
            # low. segment_line's baseline is the modal glyph bottom (robust to the
            # few descenders), so a stamped char sits ON the line like the scan.
            seg = segment_line(region, ot)
            # Baseline + cap height come from the binary MAP -- the SAME source of truth
            # the glyph boxes do -- so the synth sits on the line and is cap-sized exactly
            # where the cover is computed. The map already isolates this line's dominant
            # ink band (dropping next-line bleed) and strips rules, so its baseline is the
            # text baseline. Fall back to the segmenter / dark-mask band only when the map
            # is unavailable (a blank or cursive-only line).
            if geom is not None:
                # Baseline comes from the map's band bottom, which is descender-safe (it snaps
                # up off any sub-baseline bleed/descender tail), so the synth sits ON the line.
                # Cap height comes from the UPPERCASE / DIGIT glyphs: the text tells us which
                # they are, and they run cap-line to baseline with NO descender, so their MEDIAN
                # box HEIGHT is the cap height (a plain height cluster is inflated by descenders
                # and the x-height/cap mix). Fall back to the height cluster with no caps/digits.
                base_y = float(geom["baseline"])
                cbx = geom.get("char_boxes")
                ud_h = ([float(b[3] - b[1]) for ch, b in zip(ot, cbx)
                         if (ch.isupper() or ch.isdigit()) and b[3] - b[1] > 2]
                        if cbx and len(cbx) == len(ot) else [])
                if len(ud_h) >= 2:
                    cap_px = max(4.0, float(np.median(ud_h)))
                else:
                    hs = sorted(float(b[3] - b[1]) for b in
                                (cbx or geom.get("boxes") or []) if b[3] - b[1] > 0)
                    if len(hs) >= 4:
                        med = hs[len(hs) // 2]
                        keep = [h for h in hs if h <= 1.4 * med] or hs
                        mx = max(keep)
                        cluster = [h for h in keep if h >= 0.85 * mx] or keep
                        cap_px = max(4.0, float(np.median(cluster)))
                    else:
                        cap_px = max(4.0, base_y - float(geom["captop"]))
            else:
                base_y = float(seg.baseline_y) if (seg and seg.words) else float(ink_bot)
                cap_px = max(4.0, float(base_y) - float(ink_top))
            # PER-CHARACTER baseline: where each glyph's REAL NON-DESCENDER NEIGHBOURS sit
            # (their ink bottom), so an edited glyph is placed on the SAME line as the letters
            # around it -- not on a line-global baseline a long descender/bleed tail can drag
            # off. Most glyphs are non-descenders resting on the baseline; the descenders
            # (gjpqy) are the known exceptions, excluded from the anchor (a replacement that IS
            # a descender then renders its own tail below). Built ONCE here -- a keystroke just
            # indexes it. win=4, frac=0.35: tuned + cross-correlation-verified to land synth
            # glyphs within ~0.1px of their real neighbours (worst case ~9px -> ~1px).
            char_baseline = None
            cbx = geom.get("char_boxes") if geom is not None else None
            if cbx and len(cbx) == len(ot):
                import numpy as np
                desc = set("gjpqy")
                # Each non-descender's OWN map-box BOTTOM is its baseline. The box comes
                # from the Otsu/connected-component map, which is far more robust than an
                # ink-threshold probe: the old _glyph_ink_bottom under-read faint fax ink
                # and returned a row ~7px ABOVE the real bottom at some glyphs, dragging the
                # neighbour-median baseline up so the synth was placed too high (the "edit
                # sits too high" bug). A descender slot (gjpqy) or a non-alnum takes the
                # median box-bottom of its nearest NON-descender neighbours, so a descender
                # replacement still lands on the line and renders its own tail below.
                # Measure each non-descender's INK bottom at the SAME 0.30 coverage the
                # synth's ink is measured/placed against -- the CC/Otsu box bottom sits
                # ~0.5px lower (Otsu keeps fainter edge ink), which left the synth a
                # consistent half-pixel low. Search only within the glyph's own box rows.
                covf = np.clip((255.0 - (region.mean(2) if region.ndim == 3
                                         else region.astype(float))) / 255.0, 0.0, 1.0)

                def _ink_bot(bx):
                    x0, y0, x1, y1 = int(bx[0]), int(bx[1]), int(bx[2]), int(bx[3])
                    sub = covf[max(0, y0):min(Hr, y1 + 1), max(0, x0):min(Wr, x1)]
                    rr = np.where((sub > 0.30).any(1))[0]
                    return float(y0 + int(rr.max())) if rr.size else float(y1)
                own = [_ink_bot(cbx[i]) if (ch.isalnum() and ch not in desc) else None
                       for i, ch in enumerate(ot)]
                char_baseline = []
                for p_i in range(len(ot)):
                    nb = [own[q] for q in range(max(0, p_i - 6), min(len(ot), p_i + 7))
                          if own[q] is not None]
                    # The baseline is where the SOLID glyphs REACH; a faint fax glyph's ink
                    # under-reaches it, so a per-glyph bottom wobbles (the box11 31-40 spread)
                    # and would place a synth too high there. Take a HIGH percentile of nearby
                    # non-descender ink bottoms -- windowed so it still follows slight scan
                    # skew, but a short faint glyph can't drag the line up. Descenders (gjpqy)
                    # excluded; a descender replacement renders its own tail below this anchor.
                    char_baseline.append(float(np.percentile(nb, 80)) if nb
                                         else float(base_y))
            ctx = {"region": region, "Hr": Hr, "Wr": Wr, "ppi": ppi,
                   "rect": (x0, y0, x1, y1),                  # TEXT space (bake)
                   "disp_rect": (rx0 / ppi + odx, ry0 / ppi + ody,
                                 rx1 / ppi + odx, ry1 / ppi + ody),
                   "rotation": int(page.rotation), "seg": seg,
                   "page_index": int(box.page_index),
                   "ink": ink, "paper": paper, "sev": sev, "fpath": fpath,
                   "left_px": left_px, "right_px": right_px,
                   "ink_top": ink_top, "ink_bot": ink_bot, "cap_px": cap_px,
                   "base_y": base_y, "char_baseline": char_baseline,
                   "em": em, "orig_text": ot,
                   "want_bold": bool(getattr(box, "bold", False)),
                   "want_italic": bool(getattr(box, "italic", False)),
                   "style_seeded": bool(getattr(box, "style_seeded", False)),
                   "font_picked": bool(getattr(box, "font_picked", False)),
                   "geom": geom, "dmg": self._safe_measure_damage(region, geom)}
            return ctx
        except Exception:
            return None

    def update_scan_edit_style(self, box: "NewBox", overrides: dict) -> "NewBox":
        """Apply whole-box font_family / size / color to a scanned box DIRECTLY on
        the model (no undo command, no page repaint) so the OPEN inline editor can
        re-bake its preview in place WITHOUT committing/reopening (no kick-out).
        Bold/italic do NOT come here -- those are per-run via the selection path.
        Persists with the box (the commit bake reads the fields). Returns the
        updated stored box, or the current one when nothing changed."""
        if not isinstance(box, NewBox):
            return box
        cur = self._new_boxes.get(box.edit_key, box)
        o = overrides or {}
        fields: dict = {}
        if o.get("font_family"):
            fields["font_family"] = o["font_family"]
            fields["font_picked"] = True          # override the scan edge-match
        if o.get("size") and float(o["size"]) > 0:
            fields["size"] = float(o["size"])
        if o.get("color") is not None:
            fields["color"] = tuple(o["color"])
        if not fields:
            return cur
        updated = replace(cur, **fields)
        if updated == cur:
            return cur
        self._new_boxes[box.edit_key] = updated
        self._dirty = True
        return updated

    def seed_scan_style(self, box: "NewBox", ctx: dict = None) -> "NewBox":
        """Detect a scanned box's REAL weight/slant from its edge-matched bank face
        and write it onto ``bold``/``italic`` ONCE (first open), so the inspector
        shows the truth and the in-place bake renders the scan as-is until the user
        changes it. Returns the (possibly updated) stored box. No-op when already
        seeded, not a scanned box, or detection fails (left unseeded to retry).
        Updates the stored NewBox in place -- no undo command, it is initialisation
        that reflects what the scan already shows."""
        if not isinstance(box, NewBox):
            return box
        cur = self._new_boxes.get(box.edit_key, box)
        if getattr(cur, "style_seeded", False) or not (
                cur.cover and len(cur.cover) == 7):
            return cur
        try:
            c = ctx
            if c is None:
                cover = cur.cover
                if cur.is_paragraph and cur.line_covers:
                    cover = cur.line_covers[0]      # detect from the first line
                c = self.scan_edit_context(
                    cur, getattr(cur, "ocr_text", "") or cur.text,
                    cover_override=(cover if cover is not cur.cover else None))
            if c is None:
                return cur
            from .font_engine import detect_ttf_style
            fpath = self._edit_synth_font(c)
            if not fpath:
                return cur
            _fam, bold, italic = detect_ttf_style(fpath)
        except Exception:
            return cur
        updated = replace(cur, bold=bool(bold), italic=bool(italic),
                          style_seeded=True)
        self._new_boxes[box.edit_key] = updated
        return updated

    def _font_cap_ratio(self, fpath: "str | None") -> float:
        """A font's CAP height as a fraction of its em (rendered 'H' ink-height / em),
        cached. Lets the synth be sized so its caps equal the SCAN's cap height,
        independent of the font's advance widths."""
        cache = self.__dict__.setdefault("_cap_ratio_cache", {})
        if fpath in cache:
            return cache[fpath]
        import numpy as np
        r = 0.7
        try:
            f = fitz.Font(fontfile=fpath)
            em = 100.0
            doc = fitz.open()
            pg = doc.new_page(width=em * 2, height=em * 3)
            tw = fitz.TextWriter(pg.rect)
            tw.append((em * 0.5, em * 2.0), "H", font=f, fontsize=em)
            tw.write_text(pg, color=(0, 0, 0))
            pm = pg.get_pixmap(alpha=False)
            a = np.frombuffer(pm.samples, np.uint8).reshape(
                pm.height, pm.width, pm.n)[..., :3]
            ys = np.where(((255.0 - a.mean(2)) / 255.0).max(1) > 0.2)[0]
            if len(ys):
                r = float(ys.max() - ys.min() + 1) / em
        except Exception:
            pass
        cache[fpath] = r
        return r

    def _font_x_ratio(self, fpath: "str | None") -> float:
        """A font's X-HEIGHT as a fraction of its em (rendered 'x' ink-height / em), cached.
        Paired with _font_cap_ratio this gives the font's x-height/cap-height proportion, so a
        PURE x-height run seated to a scanned CAP band can be sized to its true x-height instead
        of being stretched to fill the cap band."""
        cache = self.__dict__.setdefault("_x_ratio_cache", {})
        if fpath in cache:
            return cache[fpath]
        import numpy as np
        r = 0.5
        try:
            f = fitz.Font(fontfile=fpath)
            em = 100.0
            doc = fitz.open()
            pg = doc.new_page(width=em * 2, height=em * 3)
            tw = fitz.TextWriter(pg.rect)
            tw.append((em * 0.5, em * 2.0), "x", font=f, fontsize=em)
            tw.write_text(pg, color=(0, 0, 0))
            pm = pg.get_pixmap(alpha=False)
            a = np.frombuffer(pm.samples, np.uint8).reshape(
                pm.height, pm.width, pm.n)[..., :3]
            ys = np.where(((255.0 - a.mean(2)) / 255.0).max(1) > 0.2)[0]
            if len(ys):
                r = float(ys.max() - ys.min() + 1) / em
        except Exception:
            pass
        cache[fpath] = r
        return r

    @staticmethod
    def _safe_measure_damage(region, geom):
        """The neighbour DAMAGE profile for this line (degrade.measure_damage), measured
        once and cached on the ctx so live typing never re-measures. None on any failure
        or a clean line, in which case the synth stays the crisp recolour."""
        try:
            from .ocr import degrade
            return degrade.measure_damage(region, geom)
        except Exception:
            return None

    @staticmethod
    def _glyph_ink_bottom(region, x0, x1, frac: float = 0.35, row_hi: "int | None" = None):
        """The lowest row whose column-summed ink in [x0,x1) reaches ``frac`` of that span's
        peak -- the bottom of a glyph's main ink mass (robust to faint bleed specks). Rows at
        or below ``row_hi`` only (drops next-line bleed). Returns a float row, or None when the
        span holds no ink. This reads where a scanned glyph actually SITS -- the baseline an
        edited glyph copies from its neighbours."""
        import numpy as np
        W = region.shape[1]
        x0 = max(0, int(round(x0)))
        x1 = min(W, int(round(x1)))
        if x1 - x0 < 1:
            return None
        crop = region[:row_hi, x0:x1] if row_hi else region[:, x0:x1]
        g = crop.mean(2) if crop.ndim == 3 else crop.astype(float)
        rows = np.clip((255.0 - g) / 255.0, 0.0, 1.0).sum(1)
        if rows.size == 0 or rows.max() <= 1e-6:
            return None
        rr = np.where(rows >= frac * rows.max())[0]
        return float(rr.max()) if rr.size else None

    def _synth_strip(self, ctx: dict, text: str, em: "float | None" = None,
                     font_bytes: "bytes | None" = None, base_y: "float | None" = None,
                     ink: "object" = None):
        """A region-height strip of ``text`` rendered in the matched font on the
        recovered baseline, recoloured to the scan ink and degraded to the line's
        own severity (clean line -> clean glyphs). ``em`` overrides the context size
        (the compose passes the scan-calibrated em). ``font_bytes`` overrides the box
        font with the font matched AT THE CARET (so an inserted char on a mixed-font
        line takes the local font). ``ink`` overrides the recolour ink (a per-run
        colour); None uses the sampled scan ink. Returns (strip_rgb, advance_px)."""
        import numpy as np
        ppi = ctx["ppi"]
        em = float(em if em is not None else ctx["em"])
        Hr = ctx["Hr"]
        base_y = float(base_y) if base_y is not None else ctx["base_y"]
        paper = ctx["paper"]
        ink = ctx["ink"] if ink is None else ink     # per-run colour override
        paper_u8 = np.clip(paper, 0, 255).astype(np.uint8)
        try:
            f = (fitz.Font(fontbuffer=font_bytes) if font_bytes
                 else fitz.Font(fontfile=ctx["fpath"]))
        except Exception:
            f = fitz.Font(fontfile=ctx["fpath"])
        runw = float(f.text_length(text, em))
        advpx = runw * ppi
        doc = fitz.open()
        pg = doc.new_page(width=runw + 2 * em, height=em * 3)
        tw = fitz.TextWriter(pg.rect)
        tw.append((em, em * 2.0), text, font=f, fontsize=em)
        tw.write_text(pg, color=(0.06, 0.06, 0.06))
        pm = pg.get_pixmap(matrix=fitz.Matrix(ppi, ppi), alpha=False)
        a = np.frombuffer(pm.samples, np.uint8).reshape(
            pm.height, pm.width, pm.n)[..., :3]
        c0 = int(round(em * ppi))
        adv_w = max(1, int(round(advpx)))
        # rows so the render's baseline (em*2*ppi) lands exactly on base_y in the strip.
        # (A former -1 fudge here pushed the ink one row BELOW the baseline -- measured
        # +1.0px low against the scan; removed so the font baseline maps straight to base_y.)
        off = int(round(em * 2.0 * ppi - base_y))
        # Render BELOW the cover band too, so a typed DESCENDER (j,g,p,q,y) keeps its tail
        # instead of being clipped at the baseline -- a tight no-descender line has ~0 room
        # below the baseline. The extra rows are paper for non-descender text (no ink), and
        # inplace_compose only grows the tile down when the strip actually has ink there.
        dmar = max(0, int(np.ceil(abs(float(getattr(f, "descender", -0.21) or -0.21))
                                  * em * ppi)) + 2)
        sh = Hr + dmar
        strip = np.empty((sh, adv_w, 3), np.uint8); strip[:] = paper_u8
        for r in range(sh):
            sr = r + off
            if 0 <= sr < a.shape[0]:
                seg = a[sr, c0:c0 + adv_w]
                if seg.shape[0] < adv_w:
                    strip[r, :seg.shape[0]] = seg
                else:
                    strip[r] = seg[:adv_w]
        # KEEP the below-cover rows ONLY when this text actually has a descender there; else
        # trim back to the cover BEFORE degrading, so a non-descender edit does not get the
        # filter's below-baseline speckle stamped into an area it never needed (Edward: only
        # descenders should leak out of the box, nothing else should degrade extra area).
        if sh > Hr:
            _below = (255.0 - strip[Hr:].mean(2)) / 255.0
            if not (_below > 0.30).any():
                strip = np.ascontiguousarray(strip[:Hr])
        # Recolour to the scan's ink/paper AND match the neighbours' degradation: the
        # cached ``dmg`` profile (measured from the surrounding real glyphs via the map)
        # fades + mottles the synth to the SAME toner damage as the text around it, so the
        # edit does not read as a crisp letter pasted on a faxy line. SELF-CALIBRATED --
        # a clean line measures ~no damage and this returns the plain crisp recolour (the
        # old "in what world is this X correct" cross-hatch can no longer appear).
        # INVERTED-MAP RESIDUAL filter (Edward's design): the inverted map measures, per pixel,
        # how far the scan had to be pushed to reach pure black/white -- that residual IS the
        # degradation. Measure its distribution by distance-from-edge on the real neighbours
        # ONCE, then stamp the same per-pixel residual onto each synth letter so it fades,
        # breaks and speckles like its surroundings. Built once per line and cached on the ctx.
        from .ocr import degrade
        # LOCAL residual (faithful profile from the RIGHT glyphs): measure the damage
        # signature from the SAME-CHARACTER scanned glyphs on the line (the letter's own
        # haze/dropout/speckle) PLUS the immediate neighbour glyphs (the local damage
        # LEVEL) -- NOT one whole-line average -- so the synth reflects what the real glyphs
        # around it actually have, and a clean spot stays clean while a wrecked spot wrecks.
        # Cached per run text on the ctx.
        _lrk = "_lr:" + text
        bfilt = ctx.get(_lrk, "unset")
        if bfilt == "unset":
            bfilt = self._local_residual_filter(ctx, text)
            ctx[_lrk] = bfilt
        if bfilt is None:                                   # clean line -> plain crisp recolour
            cov = ((255.0 - strip.mean(2)) / 255.0)[..., None]
            out = np.clip(paper.reshape(1, 1, 3) * (1.0 - cov) +
                          ink.reshape(1, 1, 3) * cov, 0, 255).astype(np.uint8)
        else:
            seed = abs(hash((ctx.get("rect"), text, em))) & 0x7FFFFFFF
            out = degrade.apply_residual_filter(strip, ink, paper, bfilt, np.random.RandomState(seed))
        # PER-GLYPH leak-out: only a column with a real DESCENDER keeps the below-cover region.
        # The residual filter stamps speckle below EVERY glyph; without this, an ascender (k,h)
        # or any non-descender in the same run shows degradation beneath it in the extended
        # rows. Clear rows below the cover wherever the CLEAN glyph had no descender ink there.
        if out.shape[0] > Hr:
            _cl = (255.0 - strip[Hr:].mean(2)) / 255.0
            _dcol = (_cl > 0.30).any(0)                  # columns with a real descender tail
            if not _dcol.all():
                out[Hr:, ~_dcol] = paper_u8
        return out, advpx

    @staticmethod
    def _line_band_rows(region):
        """Rows of the region's PRIMARY text line = the largest contiguous run of DENSE
        rows (>=25% of peak column-coverage). A PARAGRAPH line's crop can include the next
        line's top across an inter-line white gap; this returns just the edited line's row
        span so damage/size measurement never mixes in the neighbour. Single-line crops are
        one run -> the full text extent. Returns (lo, hi) inclusive, or None."""
        import numpy as np
        if region is None:
            return None
        g = (255.0 - np.asarray(region).mean(2)) / 255.0
        prof = (g > 0.4).mean(1)
        mx = float(prof.max()) if prof.size else 0.0
        if mx <= 0:
            return None
        dense = np.where(prof >= 0.25 * mx)[0]
        if not dense.size:
            return None
        runs = np.split(dense, np.where(np.diff(dense) > 1)[0] + 1)
        big = max(runs, key=len)
        return int(big.min()), int(big.max())

    def _local_residual_filter(self, ctx: dict, text: str):
        """The degradation residual (degrade.build_residual_filter) measured from the
        SAME-CHARACTER scanned glyphs on the line (the letter's own damage signature) PLUS
        the immediate neighbour glyphs of THIS edit (the local damage level), instead of
        one whole-line average. So a synthed glyph carries the haze / pixel-dropout /
        speckle that the REAL glyphs around it actually have, at the level actually present
        there. Falls back to the whole-line residual when the local scope is too small to
        profile, so a degraded page still degrades the synth. Returns the residual or None
        (a genuinely clean line -> crisp recolour)."""
        from .ocr import degrade
        geom = ctx.get("geom") or {}
        cb = geom.get("char_boxes")
        ot = ctx.get("orig_text") or ""
        region = ctx.get("region")
        if region is None:
            return None
        # A PARAGRAPH line's crop can swallow the next line's top; its char_boxes then span
        # BOTH lines (a ~47px box on a ~30px line). Measuring damage over that dilutes the
        # ink darkness/edges with inter-line paper -> the residual OVER-degrades and the synth
        # renders thin, eaten, "mangled" (Edward's date edit). Clip every measured box to the
        # edited line's own rows; single-line crops are one dense run -> unchanged.
        _band = self._line_band_rows(region)

        def _clip(boxes):
            if _band is None:
                return list(boxes)
            _lo, _hi = _band
            return [(b[0], max(b[1], _lo), b[2], min(b[3], _hi + 1))
                    for b in boxes if b and min(b[3], _hi + 1) > max(b[1], _lo)]
        if not cb or len(cb) != len(ot):
            return degrade.build_residual_filter(
                region, {"char_boxes": _clip(cb)} if cb else geom)
        sel = set()
        chars = set(text)
        for i, c in enumerate(ot):                  # same character, anywhere on the line
            if c in chars and i < len(cb) and cb[i]:
                sel.add(i)
        pos = ctx.get("_edit_pos")                  # immediate neighbours of THIS edit
        if pos:
            p, ns = int(pos[0]), int(pos[1])
            for i in (list(range(max(0, p - 4), p))
                      + list(range(ns, min(len(cb), ns + 4)))):
                if 0 <= i < len(cb) and cb[i]:
                    sel.add(i)
        boxes = _clip([cb[i] for i in sorted(sel) if cb[i]])
        prof = (degrade.build_residual_filter(region, {"char_boxes": boxes})
                if boxes else None)
        # Local scope too clean/sparse to profile -> use the whole line so the synth still
        # picks up real page damage rather than rendering crisp on a degraded scan.
        return prof if prof is not None else degrade.build_residual_filter(
            region, {"char_boxes": _clip(cb)})

    def _local_font(self, ctx: dict, x_center: float):
        """The font AT THE CARET, matched from a COUPLE OF WORDS of scanned glyphs
        around ``x_center`` (region px) -- not one word (too few glyphs to identify a
        font) and not the whole box (too coarse for a line that mixes fonts). Returns
        ``(font_bytes, em)`` to OVERRIDE the box font for the synthesized run, or
        ``(None, None)`` when the local font is the SAME as the box font (no override,
        keep the box's scan-calibrated em) or no confident local match. Cached per
        x-bucket so live typing does not re-match every keystroke."""
        cache = ctx.setdefault("_local_font_cache", {})
        bucket = int(x_center // 30)
        if bucket in cache:
            return cache[bucket]
        out = (None, None)
        seg = ctx.get("seg")
        region = ctx.get("region")
        glyphs = [g for g in (getattr(seg, "glyphs", None) or []) if g.char.strip()]
        if glyphs and region is not None and len(glyphs) >= 8:
            # the ~18 glyphs nearest the caret = a couple of words.
            near = sorted(glyphs, key=lambda g: abs(
                (g.x0 + g.bitmap.shape[1] / 2.0) - x_center))[:18]
            try:
                from .ocr import fontbank
                cells = {i: (g.char, (int(g.x0), int(g.top_y),
                                      int(g.x0 + g.bitmap.shape[1]),
                                      int(g.top_y + g.bitmap.shape[0])))
                         for i, g in enumerate(near)}
                m = fontbank.match_font(region, cells)
                local_path = fontbank.font_file_for(m[1]) if m else None
            except Exception:
                m, local_path = None, None
            # Override only when the local run is a DIFFERENT font than the box (so a
            # single-font box is untouched and keeps its scan-calibrated em).
            if m is not None and local_path and local_path != ctx.get("fpath"):
                out = (m[0], float(ctx["em"]))
        cache[bucket] = out
        return out

    @staticmethod
    def _scan_geometry(region, text: "str | None" = None) -> "dict | None":
        """The binary SOURCE OF TRUTH for one line crop (Edward's design). Inverts to a
        colour-independent ink mask, isolates THIS line's band (dropping the next line's
        bleed), and returns exact per-glyph ink boxes + baseline/cap/x-height + the
        inter-glyph gaps. Everything downstream -- glyph position, the caret, the COVER
        extent when a glyph is replaced, the synth size + font match, and word spacing --
        reads from this instead of re-segmenting on its own. Returns dict or None.
          boxes: [(x0,y0,x1,y1)] left->right (vertically-overlapping parts merged)
          captop/baseline/xheight, x0/x1 (line ink extent), gaps: [(gap_px, left_idx)]
        When ``text`` is given it ALSO returns the character-indexed map every editor
        consumer reads (char_boxes / edges / inter_px -- see _char_index_geometry); when
        None it returns only the raw-box dict (backward compatible)."""
        import numpy as np
        import cv2
        if region is None or getattr(region, "size", 0) == 0:
            return None
        g = (region.mean(2) if region.ndim == 3 else region).astype(np.uint8)
        _, bw = cv2.threshold(g, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
        if (bw > 0).mean() > 0.5:           # picked background, not ink -> flip
            bw = 255 - bw
        ink = bw > 0
        H, Wreg = ink.shape
        n, lab, stats, _c = cv2.connectedComponentsWithStats(ink.astype(np.uint8), 8)
        if n <= 1:
            return None
        # STRIP form STRUCTURE (rules / underlines / field-border rectangles): any
        # component spanning most of the line WIDTH is never a glyph -- it bridges every
        # letter into one blob (collapsing the whole line to a single box), and a rule
        # also fakes the baseline. Drop its pixels before measuring band OR glyphs, so a
        # ruled/boxed form field segments into real per-glyph boxes like any other line.
        struct = {i for i in range(1, n)
                  if stats[i, cv2.CC_STAT_WIDTH] >= 0.55 * Wreg}
        if struct:
            # A rule that TOUCHES the text fuses with the glyphs into ONE wide
            # component. Zeroing the whole blob (the old behaviour) deleted the glyphs
            # too -> char-box geometry failed -> the fallback erase whited out the
            # rule region. Instead strip only the rule's FULL-WIDTH ROWS, then re-find
            # components so the freed glyphs re-emerge as their own boxes; the precise
            # char-box erase band then spares the rule. Standalone wide rules (no
            # fused text) are dropped whole below.
            for i in struct:
                comp = lab == i
                rule_rows = comp.sum(1) >= 0.55 * Wreg
                if rule_rows.any():
                    ink[comp & rule_rows[:, None]] = False
            n, lab, stats, _c = cv2.connectedComponentsWithStats(
                ink.astype(np.uint8), 8)
            if n <= 1:
                return None
            struct = {i for i in range(1, n)
                      if stats[i, cv2.CC_STAT_WIDTH] >= 0.55 * Wreg}
        for i in struct:
            ink[lab == i] = False
        rows = ink.sum(1).astype(float)
        if rows.max() <= 0:
            return None
        thr = 0.30 * rows.max()             # the dominant horizontal text band = this line
        bands, r = [], 0
        while r < H:
            if rows[r] >= thr:
                s = r
                while r < H and rows[r] >= thr:
                    r += 1
                bands.append((s, r))
            else:
                r += 1
        if not bands:
            return None
        xtop, baseline = max(bands, key=lambda b: b[1] - b[0])
        # The 0.30*max band threshold also admits sub-baseline descender + inter-line bleed
        # ink. On a tight header that ink forms a long tail that drags the band bottom several
        # px BELOW where the letters sit, so a synth glyph placed on the baseline lands too low
        # (a g replaced by an h sat under the line). Detect it: the dense x-height/cap BODY
        # ends in a sharp row-sum drop (the "knee"); find the steepest drop in the band's lower
        # half and snap the baseline up to it -- but ONLY when the band bottom is a meaningful
        # distance below the knee (a real tail). On clean lines the knee and the band bottom
        # nearly coincide (just anti-alias taper), so the baseline is left exactly as found.
        xheight0 = max(1, baseline - xtop)
        lo = xtop + (baseline - xtop) // 2
        hi = min(baseline, H - 1)
        if hi - lo >= 2:
            _, knee = max(((rows[r - 1] - rows[r], r) for r in range(lo + 1, hi + 1)),
                          key=lambda d: d[0])
            if knee < baseline and (baseline - knee) >= max(3, round(0.08 * xheight0)):
                baseline = knee
        xheight = max(1, baseline - xtop)
        # CLIP each component to THIS line's vertical BAND (cap line .. baseline+descender).
        # On a tight fax scan adjacent rows bleed/fuse into one component that spans ~the
        # whole crop; clipping the box to the line band cuts that bleed off EVEN when the ink
        # is physically fused across the inter-line gap (a center-test keep-filter cannot).
        # So per-glyph heights -- and thus the cap height, baseline, and the degradation
        # interior read -- stay this line's real text, not a two-row envelope. The cover the
        # crop came from stays loose (byte-identity); only the measured boxes are tightened.
        band_top = max(0, int(round(xtop - 0.5 * xheight)))       # admit caps / ascenders
        band_bot = min(H, int(round(baseline + 0.55 * xheight)))  # admit descenders, not next row
        keep = []
        for i in range(1, n):
            if i in struct or stats[i, 4] < 2:
                continue
            cx0 = int(stats[i, 0])
            cx1 = cx0 + int(stats[i, 2])
            cy0 = max(int(stats[i, 1]), band_top)
            cy1 = min(int(stats[i, 1]) + int(stats[i, 3]), band_bot)
            if cy1 - cy0 >= 2:                       # keep only its in-band part
                keep.append([cx0, cy0, cx1, cy1])
        keep.sort(key=lambda c: c[0])
        merged = []                         # merge x-overlapping parts (i-dot, etc.)
        for c in keep:
            if merged:
                p = merged[-1]
                ov = min(c[2], p[2]) - max(c[0], p[0])
                narrow = min(c[2] - c[0], p[2] - p[0])
                if narrow > 0 and ov >= 0.6 * narrow:
                    p[0] = min(p[0], c[0]); p[1] = min(p[1], c[1])
                    p[2] = max(p[2], c[2]); p[3] = max(p[3], c[3])
                    continue
            merged.append(c[:])
        if not merged:
            return None
        boxes = [(c[0], c[1], c[2], c[3]) for c in merged]
        gaps = [(boxes[i + 1][0] - boxes[i][2], i) for i in range(len(boxes) - 1)]
        out = {"boxes": boxes, "captop": min(b[1] for b in boxes), "baseline": baseline,
               "xheight": xheight, "x0": min(b[0] for b in boxes),
               "x1": max(b[2] for b in boxes), "gaps": gaps}
        if text is not None:            # the character-indexed source of truth
            # Per-column ink over THIS line's band (struct rules already removed from
            # ink): lets _fit_boxes split a touching pair at the real gap instead of
            # slicing a solid glyph down its midpoint.
            col_ink = ink[band_top:band_bot].sum(0).astype(float)
            ci = PDFDocument._char_index_geometry(boxes, text, xheight, col_ink)
            if ci is not None:
                out.update(ci)
                # PER-GLYPH BOX (the y): replace each char box's shared line-band top/bottom
                # with the glyph's OWN real ink top/bottom, measured in its x-slice within this
                # line's band. _char_index_geometry gives good x; this gives true per-glyph
                # HEIGHT -- the basis for sizing a synth glyph to the SAME ink extent as the
                # scanned glyph it sits among (ink-to-ink, both measured this way). Spaces and
                # ink-free slices keep the band y.
                cbx = out.get("char_boxes")
                if cbx:
                    band = ink[band_top:band_bot]
                    Wb = band.shape[1]
                    # DROP HORIZONTAL RULE ROWS before measuring per-glyph height. A table
                    # border / underline can dip into the bottom of the band under PART of the
                    # line (its dense core is stripped as ``struct`` above, but the anti-aliased
                    # TAPERED edge rows fall under the 0.55*W width test while still carrying a
                    # continuous ink run no glyph stroke can). Left in, that run stretches every
                    # glyph box over it down to the rule (the "iability Insurance boxes all reach
                    # the line floor and fuse" bug). A rule is the one thing with a run FAR longer
                    # than a glyph; drop those rows so each box keeps its own ink extent. General:
                    # the threshold scales with line width and x-height, never the document.
                    if band.size and band.any():
                        # A row is a RULE row if it holds a continuous ink run far longer than any
                        # glyph stroke -- erosion by a 1xT bar survives only where such a run exists.
                        # T scales with line width and x-height, never the document.
                        T = max(int(0.12 * Wb), 3 * xheight)
                        er = cv2.erode(band.astype(np.uint8), np.ones((1, T), np.uint8),
                                       borderType=cv2.BORDER_CONSTANT, borderValue=0)
                        rule_rows = np.where(er.any(1))[0]
                        # A rule lives BELOW the baseline (a table border / underline under the
                        # text). Heavy bold text -- a dense VIN, a stencil caps line -- has glyph
                        # BODIES whose bottom rows merge into a long horizontal run too, and those
                        # sit ABOVE the baseline; clearing from them chopped the real glyph height
                        # and the synth then seated short and mangled at top/bottom. Only rows at or
                        # below the baseline may count as a rule.
                        _blr = int(round(baseline)) - band_top
                        rule_rows = rule_rows[rule_rows >= _blr]
                        if rule_rows.size:
                            # The rule sits at the band FLOOR (below the descenders). Clear from
                            # just above its topmost dense row to the bottom -- that also takes the
                            # dithered fax fringe one row up (scattered dots, too short to be a run
                            # but still rule ink that would drag a glyph box down).
                            band = band.copy()
                            band[max(0, int(rule_rows.min()) - 2):] = False
                    ncb = []
                    for bx in cbx:
                        if bx is None:
                            ncb.append(bx)
                            continue
                        x0, y0, x1, y1 = (int(v) for v in bx)
                        sx0, sx1 = max(0, min(x0, Wb)), max(0, min(x1, Wb))
                        sl = band[:, sx0:sx1] if sx1 > sx0 else band[:, :0]
                        rs = np.where(sl.any(1))[0] if sl.size else np.empty(0, int)
                        if rs.size:
                            ncb.append((x0, band_top + int(rs.min()),
                                        x1, band_top + int(rs.max()) + 1))
                        else:
                            ncb.append((x0, y0, x1, y1))
                    out["char_boxes"] = ncb
        return out

    @staticmethod
    def _fit_boxes(run: list, m: int, col_ink=None) -> list:
        """Coerce x-sorted ink BOXES ``run`` (each (x0,y0,x1,y1)) to exactly ``m`` of
        them, so they map 1:1 to a word's ``m`` characters: MERGE the closest-spaced
        pair (over-segmented stroke fragments touch, so they merge before real
        neighbours) until <= m, then SPLIT until == m. Y-extent unions on merge /
        carries on split, so each char box still bounds the WHOLE glyph. Repeated
        identical letters thus become DISTINCT boxes purely by position.

        ``col_ink`` (per-region-column ink, length >= region width) lets the SPLIT
        pick the box with the DEEPEST internal ink VALLEY (a real touching-glyph gap)
        and cut AT that valley -- so a solid glyph (an "8" has no inter-glyph valley,
        only an inked waist) is NOT bisected through its strokes. Falls back to the
        old widest-at-midpoint split when no column profile is given."""
        import numpy as np
        bx = sorted(([int(b[0]), int(b[1]), int(b[2]), int(b[3])] for b in run),
                    key=lambda b: b[0])
        if not bx or m <= 0:
            return []
        while len(bx) > m and len(bx) > 1:
            gi = min(range(len(bx) - 1), key=lambda i: bx[i + 1][0] - bx[i][2])
            a, b = bx[gi], bx[gi + 1]
            bx[gi:gi + 2] = [[min(a[0], b[0]), min(a[1], b[1]),
                              max(a[2], b[2]), max(a[3], b[3])]]
        # REBALANCE a count-matches-but-MISALIGNED word. The merge/split loops only
        # fire on a count mismatch; but a touching glyph pair can show up as ONE wide
        # box while an over-segmented glyph elsewhere contributes a thin FRAGMENT
        # (a sliver butted hard against its neighbour), so the COUNT happens to match
        # while the boxes do not line up with the characters -- the real "(8)18 / 4287:
        # one box covers two digits, the next is a 4px sliver" bug. Detect that signature
        # (a box >=1.5x the per-char width AND a sub-fragment touching a neighbour) and
        # MERGE the fragment, dropping the count so the valley split below re-cuts the
        # wide box at its true ink gap. Self-scaled, no absolute px threshold.
        exp = (bx[-1][2] - bx[0][0]) / max(1, m)   # expected per-char width (stable)
        _guard = 0
        while len(bx) > 1 and _guard < 8:
            _guard += 1
            widths = [b[2] - b[0] for b in bx]
            if max(widths) < 1.5 * exp:            # no merged pair -> nothing to fix
                break
            frag = None
            for i, w in enumerate(widths):
                if w >= 0.35 * exp:
                    continue
                gl = (bx[i][0] - bx[i - 1][2]) if i > 0 else 1e9
                gr = (bx[i + 1][0] - bx[i][2]) if i < len(bx) - 1 else 1e9
                if min(gl, gr) <= 0.12 * exp:      # butted against a neighbour = fragment
                    frag = i
                    break
            if frag is None:
                break
            i = frag
            gl = (bx[i][0] - bx[i - 1][2]) if i > 0 else 1e9
            gr = (bx[i + 1][0] - bx[i][2]) if i < len(bx) - 1 else 1e9
            j = i - 1 if (i > 0 and (i == len(bx) - 1 or gl <= gr)) else i + 1
            lo, hi = min(i, j), max(i, j)
            a, b = bx[lo], bx[hi]
            bx[lo:hi + 1] = [[min(a[0], b[0]), min(a[1], b[1]),
                              max(a[2], b[2]), max(a[3], b[3])]]
        have_prof = col_ink is not None and len(col_ink) > 0
        while len(bx) < m:
            best_i, best_cut, best_score = None, None, None
            for i, (x0, y0, x1, y1) in enumerate(bx):
                if x1 - x0 < 4:
                    continue
                if have_prof and x1 <= len(col_ink):
                    seg = np.asarray(col_ink[x0:x1], dtype=float)
                    w = len(seg)
                    lo = max(1, int(0.18 * w))
                    hi = max(lo + 1, int(0.82 * w))
                    if hi <= lo:
                        continue
                    rel = lo + int(np.argmin(seg[lo:hi]))
                    typ = float(np.median(seg)) or 1.0
                    # lower score == deeper RELATIVE valley == more likely a real gap
                    score = float(seg[rel]) / max(typ, 1.0)
                    cut = x0 + rel
                else:                            # no profile: prefer the widest box
                    score = -(x1 - x0)
                    cut = (x0 + x1) // 2
                if best_score is None or score < best_score:
                    best_i, best_cut, best_score = i, cut, score
            if best_i is None:                   # nothing wide enough to split
                last = bx[-1]                     # pad zero-width boxes at the right
                while len(bx) < m:
                    bx.append([last[2], last[1], last[2], last[3]])
                break
            x0, y0, x1, y1 = bx[best_i]
            cut = min(max(int(best_cut), x0 + 1), x1 - 1)
            bx[best_i:best_i + 1] = [[x0, y0, cut, y1], [cut, y0, x1, y1]]
        return [tuple(b) for b in bx]

    @staticmethod
    def _relayout_word(chars: str, fit: list, col_ink=None) -> list:
        """REPAIR a spatially-implausible per-character fit. ``_fit_boxes`` is ink-first:
        it trusts the connected-component boxes and only coerces the COUNT, so when
        touching, degraded glyphs merge with NO ink valley to split (a 'l6' on a faint
        fax), it can collapse a digit to ~1px and leave a neighbour 2-3x too wide while
        the count still matches. Detect that (a sliver or a blob vs the line's median),
        and RE-LAY the KNOWN characters across the SAME ink span by their expected
        relative widths, snapping each internal boundary to a real ink valley ONLY when
        one sits within a small window. Fires ONLY on a degenerate fit, so a cleanly
        segmented line is returned byte-identical. No font needed -- a coarse width prior
        (narrow i/l/1/punctuation vs wide m/w) is enough to stop the 1px collapse."""
        import numpy as np
        n = len(chars)
        if n < 2 or len(fit) != n:
            return fit
        # Repair ANY degenerate fit (the gate below fires ONLY on a sliver/blob fit, so a
        # cleanly segmented word is returned byte-identical). Heavy TOUCHING letters fuse with
        # no ink valley to split and collapse to 1px slivers + an overlapping blob; a real
        # per-character advance-width prior re-lays them sanely. (The old code bailed on any
        # word containing letters -- "variable widths" -- and only repaired digit fields, so a
        # heavy word like 'EFFECTIVE' kept its broken boxes; the per-glyph width table fixes that.)
        widths = [float(f[2] - f[0]) for f in fit]
        med = float(np.median(widths)) or 1.0
        if not any(w < max(2.0, 0.22 * med) or w > 3.0 * med for w in widths):
            return fit                                # clean fit -> leave it exactly
        # Per-character RELATIVE advance widths (Helvetica-ish em fractions). Accurate enough
        # as a layout prior for letters AND digits; for a FUSED run with no ink valleys the DP
        # falls back to these proportions, giving sane per-glyph boxes instead of slivers.
        _ADVW = {"i": .22, "j": .22, "l": .22, "I": .28, "f": .30, "t": .30, "r": .33,
                 "J": .50, "L": .56, "m": .83, "w": .72, "M": .83, "W": .94,
                 " ": .28, ".": .28, ",": .28, "/": .30, ":": .28, ";": .28, "'": .19,
                 "!": .28, "-": .33, "(": .33, ")": .33, "|": .26, "1": .50}

        def _uw(c):
            if c in _ADVW:
                return _ADVW[c]
            if c.isupper():
                return 0.70
            if c.isdigit():
                return 0.56
            if c.islower():
                return 0.52
            return 0.56
        x0 = int(round(min(f[0] for f in fit)))
        x1 = int(round(max(f[2] for f in fit)))
        span = max(1, x1 - x0)
        y0 = min(f[1] for f in fit)
        y1 = max(f[3] for f in fit)
        # Per-character width bounds (region px) from a coarse prior -- narrow glyphs
        # (1 / . , punctuation) are slimmer than digits. The boundaries are then placed at
        # the REAL lowest-ink columns (the actual inter-digit gaps) GLOBALLY within those
        # bounds, NOT laid out evenly -- even spacing drifts off the real gaps on
        # unevenly-spaced digits (a 2|0 boundary landing 14px inside the 0).
        units = [_uw(c) for c in chars]
        u = span / (sum(units) or 1.0)
        lo = [max(2, int(round(0.45 * units[i] * u))) for i in range(n)]
        hi = [max(lo[i] + 1, int(round(1.9 * units[i] * u))) for i in range(n)]
        # even-prior boundaries: the fallback when there is no usable ink profile.
        even, acc = [x0], 0.0
        for k in range(n):
            acc += units[k]
            even.append(x0 + span * acc / (sum(units) or 1.0))
        prof = None
        if col_ink is not None and len(col_ink) >= x1 and x1 > x0:
            p = np.asarray(col_ink[x0:x1], dtype=float)
            if p.size >= 3:                              # light smooth: ignore 1px noise
                p = np.convolve(p, np.ones(3) / 3.0, mode="same")
            prof = np.concatenate([p, p[-1:]])           # index `span` valid (no cut there)
        if prof is None:
            bnds = even
        else:
            # COST = cut at a real gap (low ink) + a regularizer that keeps each glyph near
            # its expected width. Pure min-ink clusters the cuts at the deepest valleys
            # (slivering a digit, ballooning its neighbour); the width term anchors them to
            # the model so the cut lands at the gap NEAREST the expected boundary.
            nink = prof / (float(prof[:span].max()) or 1.0)   # 0..1, low at gaps
            exp = [max(1.0, units[i] * u) for i in range(n)]
            LAM = 0.6
            INF = 1e18
            dp = np.full((n + 1, span + 1), INF)
            bk = np.full((n + 1, span + 1), -1, dtype=int)
            dp[0, 0] = 0.0
            for i in range(1, n + 1):
                li, hii, cut, e = lo[i - 1], hi[i - 1], (i < n), exp[i - 1]
                for pp in range(span + 1):
                    base = dp[i - 1, pp]
                    if base >= INF:
                        continue
                    p_lo, p_hi = pp + li, min(span, pp + hii)
                    for q in range(p_lo, p_hi + 1):
                        wp = ((q - pp) - e) / e
                        c = base + LAM * wp * wp + (float(nink[q]) if cut else 0.0)
                        if c < dp[i, q]:
                            dp[i, q] = c
                            bk[i, q] = pp
            if dp[n, span] >= INF:
                bnds = even                              # no feasible width-bounded cut
            else:
                rb = [0] * (n + 1)
                rb[n] = span
                for i in range(n, 0, -1):
                    rb[i - 1] = int(bk[i, rb[i]])
                bnds = [x0 + rb[i] for i in range(n + 1)]
        return [(int(round(bnds[k])), y0, int(round(bnds[k + 1])), y1)
                for k in range(n)]

    @staticmethod
    def _segment_words(bx: list, word_lens: list) -> "list | None":
        """Partition x-sorted ink ``bx`` boxes into one contiguous run per word, by DP
        that jointly honours the text's per-word LETTER COUNTS and the real ink GAPS.
        Cost is lexicographic: PRIMARY = total |run_size - letter_count| (so a run can
        never bleed across a word boundary even when the space is tight -- e.g. the
        narrow space around '/', which a 'largest-gaps' or char-count-estimate splitter
        gets wrong), SECONDARY = sum of gaps left UNBROKEN inside runs (so among equal-
        count segmentations it breaks at the largest gaps). No positional estimate, no
        gap threshold. Returns W (lo, hi_exclusive) index ranges, or None if B < W."""
        B, W = len(bx), len(word_lens)
        if W == 0 or B < W:
            return None
        gpre = [0.0] * B                          # gpre[m] = sum of gaps before box m
        for m in range(1, B):
            gpre[m] = gpre[m - 1] + max(0.0, float(bx[m][0] - bx[m - 1][2]))
        BIG = gpre[B - 1] + 1.0                    # one count-error outweighs all gaps
        INF = float("inf")
        dp = [[INF] * (B + 1) for _ in range(W + 1)]
        bk = [[-1] * (B + 1) for _ in range(W + 1)]
        dp[0][0] = 0.0
        for j in range(1, W + 1):
            cj = word_lens[j - 1]
            for i in range(j, B - (W - j) + 1):   # leave >=1 box for each later word
                best, bi = INF, -1
                for ip in range(j - 1, i):        # run = boxes ip .. i-1
                    prev = dp[j - 1][ip]
                    if prev == INF:
                        continue
                    cost = prev + BIG * abs((i - ip) - cj) + (gpre[i - 1] - gpre[ip])
                    if cost < best:
                        best, bi = cost, ip
                dp[j][i], bk[j][i] = best, bi
        if dp[W][B] == INF:
            return None
        runs, i = [], B
        for j in range(W, 0, -1):
            ip = bk[j][i]
            if ip < 0:
                return None
            runs.append((ip, i))
            i = ip
        runs.reverse()
        return runs

    @staticmethod
    def _char_index_geometry(boxes: list, text: str,
                             xheight: float, col_ink=None) -> "dict | None":
        """Align the binary map's x-sorted ink ``boxes`` to TEXT character indices -- the
        single primitive every edit consumer needs ('where is character i?'), so the
        caret, the cover/erase, and the font-match tiles all answer it IDENTICALLY. Splits
        boxes into one run per whitespace token (char-count-proportional estimate snapped
        to the nearest real ink gap, so an intra-number gap is not mistaken for a word
        space), then fits each run to that word's letter count (``_fit_boxes``).
        Returns ``{char_boxes, edges, inter_px}``:
          char_boxes: one (x0,y0,x1,y1) per character (spaces are zero-width boxes at the
                      gap; repeated letters are DISTINCT entries BY INDEX, not identity)
          edges:      len(text)+1 monotone caret boundaries (edges[i] = left of char i)
          inter_px:   median inter-letter gap (natural intra-word spacing)
        Returns None when there is less ink than text (caller falls back to font advances
        -- never guess a box where there is no ink)."""
        import re
        import numpy as np
        n = len(text)
        if n == 0 or not boxes:
            return None
        words = []                                   # (char_start, char_len) per word
        ci = 0
        for tok in re.findall(r"\S+|\s+", text):
            if not tok.isspace():
                words.append((ci, len(tok)))
            ci += len(tok)
        W = len(words)
        if W == 0:
            return None
        bx = sorted((tuple(int(v) for v in b) for b in boxes), key=lambda b: b[0])
        if W == 1:
            runs = [bx]
        else:
            runs_idx = PDFDocument._segment_words(bx, [c for _, c in words])
            if runs_idx is None:
                return None                          # fewer ink clusters than words
            runs = [bx[lo:hi] for (lo, hi) in runs_idx]
        char_boxes = [None] * n
        for (cs, clen), run in zip(words, runs):
            fit = PDFDocument._fit_boxes(run, clen, col_ink)
            if len(fit) != clen:
                return None
            # REPAIR a degenerate fit (touching glyphs split to a 1px sliver) by re-laying
            # the word's known characters across its ink span; a clean fit is unchanged.
            fit = PDFDocument._relayout_word(text[cs:cs + clen], fit, col_ink)
            for k in range(clen):
                char_boxes[cs + k] = fit[k]
        seen = [b for b in char_boxes if b is not None]
        if not seen:
            return None
        y0d, y1d = min(b[1] for b in seen), max(b[3] for b in seen)
        # SPACES (and any unmapped index) get a REAL-WIDTH box spanning the ink gap
        # between the words they separate, split evenly across a run of consecutive
        # spaces. A zero-width space would collapse the word gap whenever a word-initial
        # character is edited (the kept space then carries no width); a real-width space
        # also lets the caret sit inside it. Leading/trailing spaces (no word on one
        # side) stay zero-width.
        i = 0
        while i < n:
            if char_boxes[i] is not None:
                i += 1
                continue
            j = i
            while j < n and char_boxes[j] is None:
                j += 1
            lx = char_boxes[i - 1][2] if i > 0 else None
            rx = char_boxes[j][0] if j < n else None
            if lx is None:
                lx = rx if rx is not None else int(bx[0][0])
            if rx is None or rx < lx:
                rx = lx
            cnt = j - i
            step = (rx - lx) / cnt if rx > lx else 0.0
            for t in range(cnt):
                a = int(round(lx + step * t))
                b = int(round(lx + step * (t + 1)))
                char_boxes[i + t] = (a, y0d, max(a, b), y1d)
            i = j
        edges = ([float(char_boxes[i][0]) for i in range(n)]
                 + [float(char_boxes[n - 1][2])])
        for i in range(1, n + 1):                    # monotonic caret boundaries
            if edges[i] < edges[i - 1]:
                edges[i] = edges[i - 1]
        inter = [bx[i + 1][0] - bx[i][2] for i in range(len(bx) - 1)
                 if 0 < (bx[i + 1][0] - bx[i][2]) < 0.6 * float(xheight)]
        inter_px = float(np.median(inter)) if inter else 0.15 * float(xheight)
        return {"char_boxes": char_boxes, "edges": edges, "inter_px": inter_px}

    @staticmethod
    def geom_char_box(geom: "dict | None", i: int) -> "tuple | None":
        """The (x0,y0,x1,y1) ink box for CHARACTER INDEX ``i`` from the map, or None.
        The index->box bridge the cover/erase uses: it asks for the box at a character
        index directly, so two identical adjacent glyphs (an 'HH') are never conflated."""
        cb = (geom or {}).get("char_boxes")
        if not cb:
            return None
        return cb[max(0, min(int(i), len(cb) - 1))]

    def _char_edges(self, ctx: dict, text: str, font=None) -> list:
        """Per-character x-EDGES (region px, ``len(text)+1``; edges[i] = left of char i)
        for one line: the binary MAP's edges when it mapped this line, else a font-advance
        layout across the measured ink span. The SINGLE edge source shared by the caret
        measure and the in-place compose, so the caret position and the cut position agree
        by construction (no second segmenter to drift against the map)."""
        geom = ctx.get("geom")
        n = len(text)
        if geom and geom.get("edges") and len(geom["edges"]) == n + 1:
            return [float(e) for e in geom["edges"]]
        left = float(ctx.get("left_px", 0.0))
        right = float(ctx.get("right_px", left + 1.0))
        em = float(ctx.get("em", 12.0)) * float(ctx.get("ppi", 1.0))
        if n == 0:
            return [left, max(left + 1.0, right)]

        def adv(ch):
            try:
                w = float(font.text_length(ch, em)) if font else 0.0
            except Exception:
                w = 0.0
            return w if w > 0 else (0.30 * em if ch == " " else 0.55 * em)

        ws = [adv(c) for c in text]
        tot = sum(ws) or 1.0
        edges, c = [left], 0.0
        for k in range(n):
            c += ws[k]
            edges.append(left + (right - left) * c / tot)
        return edges

    def _scan_glyph_tiles(self, ctx: dict) -> dict:
        """Per-character coverage tiles for the font matcher, cut from the scan at each
        character's OWN ink box (``char_boxes`` from the binary map) -- the exact glyph
        extent horizontally AND vertically, so a tile holds the whole letter with no
        next-line bleed and no coarse cap band. Width is preserved (box left..right) so
        the matcher still tells a condensed face from a normal one. Falls back to the
        font-advance edges + cap band only when the map did not segment this line."""
        import numpy as np
        region = ctx.get("region")
        if region is None:
            return {}
        g = region.mean(2) if region.ndim == 3 else region
        cov = (255.0 - g.astype(np.float32)) / 255.0
        ot = ctx["orig_text"]
        cb = (ctx.get("geom") or {}).get("char_boxes")
        tiles = {}
        if cb and len(cb) == len(ot):
            for ch, box in zip(ot, cb):
                if not ch.isalnum() or ch in tiles:
                    continue
                x0, y0, x1, y1 = (int(v) for v in box)
                if x1 - x0 >= 4 and y1 - y0 >= 4:
                    sub = cov[y0:y1, x0:x1]
                    if sub.size and float((sub > 0.3).sum()) >= 6:
                        tiles[ch] = sub
            return tiles
        edges = self._char_edges(ctx, ot)            # fallback: advances + cap band
        top = int(ctx["ink_top"])
        bot = int(round(ctx.get("base_y", ctx["ink_bot"])))
        if bot - top < 4:
            bot = int(ctx["ink_bot"])
        for i, ch in enumerate(ot):
            if not ch.isalnum() or ch in tiles or i + 1 >= len(edges):
                continue
            x0, x1 = int(round(edges[i])), int(round(edges[i + 1]))
            if x1 - x0 >= 4:
                sub = cov[top:bot, x0:x1]
                if sub.size and float((sub > 0.3).sum()) >= 6:
                    tiles[ch] = sub
        return tiles

    def _page_font_map(self, page_index: int):
        """The page's PER-WORD font map (ocr/pagefont.PageFontMap): each WORD on the page is
        matched to its own font, so one box can carry MULTIPLE fonts. Built at OCR time by
        _ocr_apply_page (while the page is still UNROTATED, so pagefont's text-space covers
        line up with the render) and stashed here. The editor resolves each word's font from
        it (_edit_synth_font). This is the word-for-word matcher that had been built
        (ocr/pagefont) but left unwired. Pre-built at OCR time; rebuilt lazily here when the
        cache was cleared (e.g. the decorative-font toggle flipped). Returns None on failure."""
        cache = getattr(self, "_pfm_cache", None)
        if cache is None:
            cache = self._pfm_cache = {}
        if page_index not in cache:
            try:
                from .ocr import pagefont
                cache[page_index] = pagefont.build(self, page_index)
            except Exception:
                cache[page_index] = None
        return cache.get(page_index)

    def _box_display_move(self, box) -> "tuple | None":
        """The edit raster's displacement from its cover, measured in DISPLAY space
        (the upright frame the editor renders in). ``(0.0, 0.0)`` for an IN-PLACE edit
        -- even one that GREW wider, because a grow extends the display RIGHT edge but
        keeps the display ORIGIN -- and nonzero only when the box was actually dragged
        somewhere else. ``None`` when the box carries no edit raster.

        This is the SINGLE source of truth for "did this box move?" -- the hide-bake,
        the render offset, and the cover-vacate all read it, so they can never disagree
        (the disagreement was the re-edit doubling / shift / blanking). The old per-site
        tests compared the rect's min-corner in TEXT space, but on a /Rotate 90/180/270
        page a width-grow maps to a -x/-y shift of that corner, so an in-place edit read
        as "moved". Mapping both corners through the page rotation first removes that:
        on a non-/Rotate page the rotation is identity, so this is the plain delta."""
        cov = getattr(box, "cover", ()) or ()
        eir = getattr(box, "edit_image_rect", ()) or ()
        if not (getattr(box, "edit_image", b"") and len(cov) >= 4 and len(eir) >= 4):
            return None
        try:
            rot = self.working[box.page_index].rotation_matrix

            def origin(r):
                pts = [fitz.Point(r[0], r[1]) * rot, fitz.Point(r[2], r[1]) * rot,
                       fitz.Point(r[2], r[3]) * rot, fitz.Point(r[0], r[3]) * rot]
                return min(p.x for p in pts), min(p.y for p in pts)

            cx, cy = origin(cov)
            ex, ey = origin(eir)
            return (ex - cx, ey - cy)
        except Exception:
            return (float(eir[0]) - float(cov[0]), float(eir[1]) - float(cov[1]))

    def _ttf_style_cached(self, path: str):
        """``(family, bold, italic)`` for a bank/font file, cached per path (the tables read
        opens the file)."""
        c = getattr(self, "_ttf_style_cache", None)
        if c is None:
            c = self._ttf_style_cache = {}
        v = c.get(path)
        if v is None:
            from .font_engine import detect_ttf_style
            try:
                v = detect_ttf_style(path)
            except Exception:
                v = ("", False, False)
            c[path] = v
        return v

    def _synth_weight_reconcile(self, cand: "str | None", ctx: dict) -> "str | None":
        """Keep a typed glyph as BOLD (or not) as the scan it sits in. The per-word matchers rank
        on a shape descriptor that is weight-blind, so a bold scan can match the REGULAR sibling of
        the same family -- Courier New Bold vs Regular have identical letterforms, so the synth came
        out thin next to bold text and the user had to hand-bold it. When the picked font and the
        run's OWN font (``ctx['fpath']``, resolved at OCR time from the whole box) are the SAME
        family at a DIFFERENT weight, trust the run's font: it is the one weight signal that does
        NOT depend on re-measuring the degraded ink (degradation thins strokes, which is what fools
        the matcher in the first place). Only a same-family weight FLIP is corrected -- a genuinely
        different per-word family is left exactly as the matcher chose it.

        Known edge (post-1.0): if ONE box mixes the same family at two weights (a bold label + a
        regular value in the same typeface), this snaps the edited word to the box's weight. Rare
        for scanned letters/forms, where a box is one weight; revisit with degrade-matched weight
        measurement if it shows up."""
        base = ctx.get("fpath")
        if not base or not cand or base == cand:
            return cand
        fam_b, bold_b, _ = self._ttf_style_cached(base)
        fam_c, bold_c, _ = self._ttf_style_cached(cand)
        if fam_b and fam_c and fam_b.lower() == fam_c.lower() and bold_b != bold_c:
            return base
        return cand

    def _edit_synth_font(self, ctx: dict) -> "str | None":
        """The font file to synthesize an edit in -- resolved PER GLYPH at the caret
        from the page-level SUPER-RESOLUTION map (ocr/supermatch): the caret region's
        glyphs vote their font group, whose font was matched by pooling + super-resolving
        every same-font glyph on the page (strong even for a lone sparse field). Falls
        back to the edge render-and-compare, then the box font, when the map is missing."""
        cached = ctx.get("_synth_fpath")
        if cached is not None:
            return cached
        import os
        out = ctx.get("fpath")
        # The user EXPLICITLY picked a family: render in THAT family's file.
        if ctx.get("font_picked") and out:
            ctx["_synth_fpath"] = out
            return out
        # PER-WORD font (PRIMARY): the page map (ocr/pagefont) keyed to the EDITED WORD's
        # DISPLAY rect, so each word in a box keeps its OWN font instead of the whole line
        # majority-voting to one font (the per-box bug). _edit_disp_rect is set by the compose.
        try:
            _pi = ctx.get("page_index")
            _er = ctx.get("_edit_disp_rect")
            _pm = self._page_font_map(_pi) if _pi is not None else None
            if _pm is not None and _er:
                _fr = _pm.font_for_rect(_er[0], _er[1], _er[2], _er[3])
                if _fr and os.path.exists(_fr[0]):
                    _rf = self._synth_weight_reconcile(_fr[0], ctx)
                    ctx["_synth_fpath"] = _rf
                    return _rf
        except Exception:
            pass
        # SHORT FIELD ONLY (a lone date / number, <=14 chars): a short field is one font, so
        # the whole-word render-and-compare is safe there. A LONGER line can mix fonts (a label
        # in one face, its value in another), so it must NOT go through this per-field matcher --
        # it would flatten the whole line to one font (the 'EFFECTIVE DATE ... = Impact for the
        # whole box' bug). Longer lines fall through to the PER-GLYPH page map below.
        otext = (ctx.get("orig_text") or "").strip()
        region = ctx.get("region")
        if region is not None and 2 <= len(otext.replace(" ", "")) <= 14:
            rect = ctx.get("rect") or ()
            mkey = (ctx.get("page_index"),
                    tuple(round(float(v), 1) for v in rect[:4]) if len(rect) >= 4 else None,
                    otext)
            mc = getattr(self, "_synth_match_cache", None)
            if mc is None:
                mc = self._synth_match_cache = {}
            if mkey in mc:
                p = mc[mkey]
                if p and os.path.exists(p):
                    ctx["_synth_fpath"] = p
                    ctx["_synth_exact"] = True
                    return p
            else:
                try:
                    from .ocr import verify, fontbank
                    ttf = fontbank._ensure_ttf_cache()
                    key = verify.match_by_synth(self, region, otext, ttf) if ttf else None
                    p = os.path.join(ttf, key) if key else None
                    p = p if (p and os.path.exists(p)) else None
                    mc[mkey] = p
                    if p:
                        ctx["_synth_fpath"] = p
                        ctx["_synth_exact"] = True
                        return p
                except Exception:
                    pass
        # FALLBACK: the page map over the WHOLE LINE (display pts) -- only reached when there
        # was no edited-word rect. pagefont stores centers in DISPLAY pts, so query in that
        # frame; the old rect*ppi (text-space px) query missed every center and fell back to an
        # arbitrary nearest glyph, which is how the whole box collapsed to one font.
        try:
            pi = ctx.get("page_index")
            dr = ctx.get("disp_rect")
            pm = self._page_font_map(pi) if pi is not None else None
            if pm is not None and dr:
                fr = pm.font_for_rect(dr[0], dr[1], dr[2], dr[3])
                if fr and os.path.exists(fr[0]):
                    _rf = self._synth_weight_reconcile(fr[0], ctx)
                    ctx["_synth_fpath"] = _rf
                    return _rf
        except Exception:
            pass
        # FALLBACK: the edge render-and-compare over this region's glyphs.
        try:
            from .ocr import fontbank
            if fontbank.edge_bank_available():
                tiles = self._scan_glyph_tiles(ctx)
                res = fontbank.match_edge(tiles) if tiles else None
                if res and res.get("best"):
                    ttf = fontbank._ensure_ttf_cache()
                    p = os.path.join(ttf, res["best"]) if ttf else None
                    if p and os.path.exists(p):
                        out = p
        except Exception:
            pass
        out = self._synth_weight_reconcile(out, ctx)
        ctx["_synth_fpath"] = out
        return out

    def _synth_metrics(self, ctx: dict):
        """The SINGLE sizing source for an in-place edit: the synth font, its bytes, its
        path, the scan-calibrated box em, the cap-anchored synth size ``sem`` and the
        horizontal ``condense``. Both the rendered glyphs (inplace_compose) AND the caret
        edge measure read from here, so the caret tracks the glyphs as they actually render
        (the old caret laid the typed middle out at the box em with no condense, drifting
        from the cap-anchored, condensed render). Cached on the ctx."""
        cached = ctx.get("_synth_metrics")
        if cached is not None:
            return cached
        import numpy as np
        ppi = ctx["ppi"]
        ot = ctx["orig_text"]
        left_px, right_px = ctx["left_px"], ctx["right_px"]
        base_em = float(ctx["em"])
        # The SCAN crop -- the size-fit block below measures the real glyph heights from
        # it (through the map's char_boxes). It was referenced unqualified ("region"),
        # which raised NameError on every call and was swallowed by the block's bare
        # except, silently killing the map-driven size fit and leaving the synth at the
        # noisy cap-anchored em (wrong size). Bind it from the ctx so the fit runs.
        region = ctx.get("region")
        try:
            f = fitz.Font(fontfile=ctx["fpath"])
        except Exception:
            f = None
        full_adv = float(f.text_length(ot, base_em)) * ppi if (f and ot) else 0.0
        scale = ((right_px - left_px) / full_adv) if full_adv > 1 else 1.0
        em = base_em * scale
        synth_fpath = self._edit_synth_font(ctx)
        # STYLE: render the requested bold/italic as a REAL VARIANT FILE, never a
        # synthetic embolden/slant. The edge-match already encodes the SCAN's own
        # weight/slant, so only swap when the wanted style differs from what the
        # matched face actually IS; keep the matched face when no sibling exists.
        wb = bool(ctx.get("want_bold", False))
        wi = bool(ctx.get("want_italic", False))
        # Only swap once the box is SEEDED (its real weight/slant is known); an
        # unseeded box would have want=(False,False) from the OCR default and would
        # wrongly un-bold a bold scan. Unseeded -> use the raw edge-match as-is.
        if synth_fpath and ctx.get("style_seeded") and not ctx.get("_synth_exact"):
            try:
                from .font_engine import detect_ttf_style
                from .ocr import fontbank
                _fam, mb, mi = detect_ttf_style(synth_fpath)
                if (wb, wi) != (bool(mb), bool(mi)):
                    sib = fontbank.variant_file_for(synth_fpath, wb, wi)
                    if sib:
                        synth_fpath = sib
            except Exception:
                pass
        synth_font, lfb = f, None
        try:
            synth_font = fitz.Font(fontfile=synth_fpath)
            with open(synth_fpath, "rb") as _fh:
                lfb = _fh.read()
        except Exception:
            synth_fpath = ctx["fpath"]
            try:                                  # keep lfb in sync with the fallback
                with open(synth_fpath, "rb") as _fh:
                    lfb = _fh.read()
            except Exception:
                lfb = None
        cap_px = ctx.get("cap_px")
        cap_ratio = self._font_cap_ratio(synth_fpath)
        sem = (float(cap_px) / (ppi * cap_ratio)
               if (cap_px and cap_ratio > 0.1) else em)
        # SIZE via RENDER-MEASURE-FIT against the SCAN's OWN glyphs (route size through the
        # map): cap_px is class-blind AND ~10% noisy, and the matched font's cap/x-height
        # proportion differs from the scan's, so one em sized to caps leaves LOWERCASE wrong
        # (and vice versa). Instead, size to whichever class the line is MOSTLY made of:
        # measure the scan's median ink height for that class (its own map glyphs, at the
        # SAME 0.30 coverage the synth is measured at, band-limited to each glyph's box rows
        # so next-line bleed can't inflate it), render the font's same-class reference, and
        # scale so they match. The synth then comes out the SAME height as its real neighbours.
        XH = set("aceimnorsuvwxz")          # x-height lowercase only (no ascender/descender)
        try:
            cbx = (ctx.get("geom") or {}).get("char_boxes")
            if synth_font is not None and region is not None \
                    and cbx and len(cbx) == len(ot):
                covr = np.clip((255.0 - (region.mean(2) if region.ndim == 3
                                         else region.astype(float))) / 255.0, 0.0, 1.0)
                Hreg = region.shape[0]

                def _scan_h(i):
                    bx = cbx[i]
                    x0, y0, x1, y1 = int(bx[0]), int(bx[1]), int(bx[2]), int(bx[3])
                    sub = covr[max(0, y0):min(Hreg, y1 + 1), x0:x1]
                    ys = np.where((sub > 0.30).any(1))[0]
                    return float(ys.max() - ys.min()) if ys.size else None
                lowers = sum(1 for c in ot if c.islower())
                capsd = sum(1 for c in ot if c.isupper() or c.isdigit())
                if lowers > capsd:
                    ids = [i for i, c in enumerate(ot) if c in XH]
                    ref = "eoacsmnurvwxz"
                else:
                    ids = [i for i, c in enumerate(ot) if c.isupper() or c.isdigit()]
                    ref = "".join(c for c in ot if c.isupper() or c.isdigit())[:12] or "HE0"
                hts = [h for i in ids if (h := _scan_h(i)) is not None and h > 2]
                if len(hts) >= 2:
                    target_h = float(np.median(hts))   # PER-CHAR median of scan glyph heights

                    def _ref_med_h(em):
                        # Median of the ref glyphs' INDIVIDUAL 0.30 heights -- measured the
                        # SAME way as target_h (per char). The union extent would be ~8%
                        # taller (round-letter overshoot stacks), which sized the synth big.
                        aimg, _y, _x, _b = self._render_text_px(synth_font, ref, em, ppi)
                        fc = np.clip((255.0 - aimg.mean(2)) / 255.0, 0.0, 1.0)
                        pres = (fc > 0.30).any(0)
                        hs, i, N = [], 0, len(pres)
                        while i < N:
                            if pres[i]:
                                j = i
                                while j < N and pres[j]:
                                    j += 1
                                ys = np.where((fc[:, i:j] > 0.30).any(1))[0]
                                if ys.size:
                                    hs.append(float(ys.max() - ys.min()))
                                i = j
                            else:
                                i += 1
                        return float(np.median(hs)) if hs else 0.0
                    # Fit at the ACTUAL render size, iterated (antialiasing makes height
                    # vs em slightly non-linear at small sizes); converges in 2-3 passes.
                    for _ in range(3):
                        h_now = _ref_med_h(sem)
                        if h_now <= 1:
                            break
                        sem = float(sem * target_h / h_now)
        except Exception:
            pass
        synth_line = float(synth_font.text_length(ot, sem)) * ppi if (synth_font and ot) else 0.0
        condense = (float(np.clip((right_px - left_px) / synth_line, 0.7, 1.3))
                    if synth_line > 1 else 1.0)
        out = (synth_font, lfb, synth_fpath, em, sem, condense)
        ctx["_synth_metrics"] = out
        return out

    @staticmethod
    def _char_styles(runs, n: int, base: tuple) -> list:
        """Per-character ``(bold, italic, RunStyle|None)`` for the new text, from a
        ``(text, bold, italic[, RunStyle])`` run tuple. Pads to ``n`` with ``base``
        (a 3-tuple ``(bold, italic, None)``). ``runs`` falsy -> every char base. The
        optional 4th element carries per-run colour/family; a legacy 3-tuple run
        yields ``rs=None`` (== the old behaviour)."""
        if not runs:
            return [base] * n
        arr = []
        for seg in runs:
            try:
                t, b, i = seg[0], bool(seg[1]), bool(seg[2])
                rs = seg[3] if len(seg) > 3 else None
            except Exception:
                # Malformed run: still CONSUME its text length as base style so the
                # remaining chars don't desync (dropping it shifted every later
                # char's style by len(t)).
                t = seg[0] if (seg and isinstance(seg[0], str)) else ""
                b, i, rs = base[0], base[1], None
            arr.extend([(b, i, rs)] * len(t))
        if len(arr) < n:
            arr.extend([base] * (n - len(arr)))
        return arr[:n]

    @staticmethod
    def _runs_from_styles(text, styles, base):
        """Build runs from a per-char ``(bold, italic, RunStyle|None)`` styles list.
        Returns None when every char is the base style (uniform -> fast path). Emits
        a 4-tuple ``(t, b, i, rs)`` for a run with a RunStyle, else a 3-tuple."""
        if not text:
            return None
        out: list[list] = []
        for ch, st in zip(text, styles):
            if out and out[-1][1] == st:
                out[-1][0] += ch
            else:
                out.append([ch, st])
        if all(st == base for st in styles):
            return None
        res = []
        for t, st in out:
            b, i, rs = st
            res.append((t, b, i, rs) if rs is not None else (t, b, i))
        return tuple(res)

    def _slice_runs_per_line(self, runs, new_lines, base):
        """Split the whole-text (text,bold,italic) runs into ONE run tuple per line,
        aligned to each line's STRIPPED text (what compose_lines_block feeds
        inplace_compose). ``new_lines`` is ``new_text.split('\\n')``; the joiner put
        exactly ONE separator char between lines, so we skip it. Returns
        list[tuple|None] (None = a uniform line). All-uniform / no runs -> all None."""
        if not runs:
            return [None] * len(new_lines)
        total = sum(len(ln) for ln in new_lines) + max(0, len(new_lines) - 1)
        styles = self._char_styles(runs, total, base)
        out, pos = [], 0
        for ln in new_lines:
            lead = len(ln) - len(ln.lstrip())
            stripped = ln.strip()
            seg = styles[pos + lead: pos + lead + len(stripped)]
            out.append(self._runs_from_styles(stripped, seg, base))
            pos += len(ln) + 1                       # +1 for the inter-line separator
        return out

    def _compose_changed_strip(self, ctx, text, styles, base, sem, lfb,
                               synth_fpath, base_y):
        """Render the changed middle as one strip, each consecutive RUN of equal
        style ``(bold, italic, RunStyle|None)`` in its OWN variant FILE (the
        bold/italic sibling, or a picked family) and its OWN colour -- never a
        synthetic embolden/slant. A single uniform base run takes the exact one-strip
        path (byte-identical to the no-style bake); multiple runs render per run and
        concatenate left-to-right by advance."""
        import numpy as np
        segs = []                                     # [[text, (bold,italic,rs)], ...]
        for ch, st in zip(text, styles):
            if segs and segs[-1][1] == st:
                segs[-1][0] += ch
            else:
                segs.append([ch, st])
        if not segs:
            return None
        bcache = {}

        def style_bytes(st):
            b, i, rs = st
            key = (b, i, rs.font_family if rs else None)
            if key in bcache:
                return bcache[key]
            data = lfb
            try:
                if rs is not None and rs.font_family:
                    # A picked per-run family: that family's real variant FILE.
                    fp = self._edit_font_file(rs.font_family, b, i)
                    if fp:
                        with open(fp, "rb") as fh:
                            data = fh.read()
                elif (b, i) != (base[0], base[1]):
                    # Weight/slant differs from base: the real sibling of the face.
                    from .ocr import fontbank
                    sib = fontbank.variant_file_for(synth_fpath, b, i)
                    if sib:
                        with open(sib, "rb") as fh:
                            data = fh.read()
            except Exception:
                data = lfb
            bcache[key] = data
            return data

        def style_ink(st):
            rs = st[2]
            if rs is not None and rs.color:
                return np.array([float(c) * 255.0 for c in rs.color], np.float32)
            return None                              # None -> the sampled scan ink

        if len(segs) == 1:
            st = segs[0][1]
            strip, _ = self._synth_strip(ctx, text, em=sem, font_bytes=style_bytes(st),
                                         base_y=base_y, ink=style_ink(st))
            return strip
        parts = []
        for seg_txt, st in segs:
            sstrip, _ = self._synth_strip(ctx, seg_txt, em=sem, font_bytes=style_bytes(st),
                                          base_y=base_y, ink=style_ink(st))
            if sstrip is not None:
                parts.append(sstrip)
        if not parts:
            return None
        return np.ascontiguousarray(np.hstack(parts))

    @staticmethod
    def _contig_extent(rowmask, gap=3):
        """(top, bottom) of the FIRST contiguous ink run in a per-row bool mask, tolerating
        internal holes up to ``gap``-1 px but STOPPING at a real inter-line gap. A scanned
        line's cover routinely dips a few px into the NEXT line's ascenders; a plain
        ys.max() then jumps the measured baseline DOWN across the blank inter-line strip
        onto that next line, sizing the synth glyph to the wrong (much taller) band -- the
        'EFFECTIVE DATE' digit came out ~47px when the real digit is ~31px. Walking down
        from the glyph top and breaking at the first multi-px paper gap pins top/bottom to
        THIS line's own glyph."""
        import numpy as np
        ys = np.where(rowmask)[0]
        if not ys.size:
            return None
        top = int(ys[0]); bot = top; g = 0
        for r in range(top + 1, len(rowmask)):
            if rowmask[r]:
                bot = r; g = 0
            else:
                g += 1
                if g >= gap:
                    break
        return top, bot

    def _render_run_strip(self, ctx, run_text, run_styles, base, sem, lfb,
                          synth_fpath, condense, local_by, synth_font, ppi,
                          region, cb, ot, paper_u8, have_geom, Hr, Wr):
        """Render ONE run of text as a placed synth strip: compose it in the matched
        font, apply the box's UNIFORM size factor, seat it on the scanned glyphs'
        height-class band, ink-crop (returning the dropped right side-bearing),
        horizontal condense, and pad typed leading/trailing spaces back as paper.
        Returns ``(strip, rbear)`` -- ``strip`` is None when the run is empty/blank,
        ``rbear`` the last glyph's dropped right bearing (0.0 default)."""
        import numpy as np
        import cv2
        strip = None
        if run_text.strip():
            strip = self._compose_changed_strip(
                ctx, run_text, run_styles, base, sem, lfb,
                synth_fpath, local_by)
        if strip is not None and have_geom and local_by is not None:
            # UNIFORM SIZE MATCH -- ONE factor for the whole BOX, applied to EVERY strip no
            # matter which letters it contains, so a lone ascender ('t', which has no
            # x-height glyph to measure) and a word ('test') scale IDENTICALLY and the size
            # never jumps between the first letter and the rest. The matched bank font
            # renders taller than the scan at the cap-anchored ``sem`` by a CONSTANT ratio;
            # measure it once from a reference of the line's dominant class (x-height
            # letters, else caps/digits) rendered the SAME way the strips are (so AA and
            # degradation are included) and measured PER GLYPH like the scan. Cached.
            # A NUMERIC run (date / amount: digits + separators) is ONE height class with no
            # ascenders/descenders, so its true size IS the scan DIGIT BAND. The coarse uniform
            # size_factor below can scale such a run UP past the strip and CLIP its top (a '5'
            # lost its top bar and read as a 'b'); skip the coarse pre-scale for a numeric run
            # and let the exact band-seat size it.
            _run_num = bool(run_text) and all(
                (c.isdigit() or c in "/.,:-+$%() ") for c in run_text)
            sf = ctx.get("_size_factor")
            if sf is None:
                sf = 1.0
                try:
                    XREF = set("acemnorsuvwxz")   # x-height bodies (no dot/asc/descender)
                    lowers = sum(1 for c in ot if c.islower())
                    capsd = sum(1 for c in ot if c.isupper() or c.isdigit())
                    xh = lowers >= max(1, capsd)

                    def _is_ref(c):
                        return (c in XREF) if xh else (c.isupper() or c.isdigit())
                    covF = (255.0 - region.mean(2)) / 255.0
                    scan_hs = []                  # scan's OWN ref glyphs (map), per glyph
                    for i, oc in enumerate(ot):
                        if _is_ref(oc) and i < len(cb) and cb[i]:
                            bx0, by0, bx1, by1 = (int(v) for v in cb[i])
                            # Measure the glyph's ink extent over its own columns, within its OWN
                            # box rows +/- a 3px pad (the box runs ~1px tight, so the pad recovers
                            # the true top/bottom WITHOUT reaching into the line/header above or a
                            # rule below in these columns -- a full-height search there inflated
                            # the height and ballooned the synth).
                            _br0 = max(0, by0 - 3); _br1 = min(covF.shape[0], by1 + 4)
                            _ext = self._contig_extent(
                                (covF[_br0:_br1, bx0:bx1] > 0.30).any(1))
                            if _ext:
                                scan_hs.append(float(_ext[1] - _ext[0]))
                    ref = "".join(c for c in dict.fromkeys(ot) if _is_ref(c))[:12] \
                        or ("noaeumrcsvx" if xh else "HENRSAOTU")
                    rstrip = self._compose_changed_strip(
                        ctx, ref, [(base[0], base[1], None)] * len(ref), base, sem,
                        lfb, synth_fpath, local_by)
                    synth_hs = []                 # synth ref, per glyph (segment by gaps)
                    if rstrip is not None:
                        rc = (255.0 - rstrip.mean(2)) / 255.0
                        pres = (rc > 0.30).any(0)
                        gi, N = 0, len(pres)
                        while gi < N:
                            if pres[gi]:
                                gj = gi
                                while gj < N and pres[gj]:
                                    gj += 1
                                ys = np.where((rc[:, gi:gj] > 0.30).any(1))[0]
                                if ys.size:
                                    synth_hs.append(float(ys.max() - ys.min()))
                                gi = gj
                            else:
                                gi += 1
                    if scan_hs and synth_hs:
                        sf = float(np.clip(np.median(scan_hs) / max(1.0, np.median(synth_hs)),
                                           0.55, 1.55))
                except Exception:
                    sf = 1.0
                ctx["_size_factor"] = sf
            if abs(sf - 1.0) > 0.03 and not _run_num:
                Hs = strip.shape[0]
                Hres = max(1, int(round(Hs * sf)))
                interp = cv2.INTER_AREA if sf < 1.0 else cv2.INTER_LINEAR
                res = cv2.resize(strip, (strip.shape[1], Hres), interpolation=interp)
                out = np.empty_like(strip)
                out[:] = paper_u8
                shift = int(round(float(local_by) * (1.0 - sf)))  # keep baseline fixed
                rr = np.arange(Hs)
                srr = rr - shift
                mm = (srr >= 0) & (srr < Hres)
                out[rr[mm]] = res[srr[mm]]
                strip = np.ascontiguousarray(out)
            # EXACT seat to the scanned glyphs' band for a SAME height-class run (digits,
            # caps, x-height bodies): scale + position the changed ink so its top reaches the
            # scanned glyphs' top and it grows down to the baseline (+1px for the 300dpi map
            # bottom reading high vs the render). A run with a tall ascender/descender keeps
            # the NATURAL font render (correct descenders + spacing) via the uniform sf above
            # -- re-cutting those by class distorts descenders/spacing, so it is left alone.
            # A no-cap/no-digit LOWERCASE run's dense body is the x-HEIGHT, not the cap line, so
            # seating it to fill the cap band renders it cap-tall (verified against tight glyph
            # lines: 'typing' filled the whole cap band). Size such a run's body to the x-HEIGHT
            # target band*(_xcr) where _xcr = font x-height/cap-height; ascenders then reach ~the
            # cap line, descenders drop below the baseline. _xcr==1 for any run with a cap/digit.
            _lc = (any(c.islower() for c in (run_text or ""))
                   and not any((c.isupper() or c.isdigit()) for c in (run_text or "")))
            _xcr = 1.0
            if _lc:
                _cr = self._font_cap_ratio(synth_fpath)
                _xcr = float(min(1.0, self._font_x_ratio(synth_fpath) / max(0.1, _cr)))
            band = ctx.get("_seat_band") or ctx.get("_scan_band")
            if band is None:
                band = (-1, -1)
                try:
                    _lo = sum(1 for c in ot if c.islower())
                    _cd = sum(1 for c in ot if c.isupper() or c.isdigit())
                    _xh = _lo >= max(1, _cd)
                    _XR = set("acemnorsuvwxz")
                    _cF = (255.0 - region.mean(2)) / 255.0
                    _tops, _bots = [], []
                    for _i, _oc in enumerate(ot):
                        _r = (_oc in _XR) if _xh else (_oc.isupper() or _oc.isdigit())
                        if _r and _i < len(cb) and cb[_i]:
                            _x0, _y0, _x1, _y1 = (int(v) for v in cb[_i])
                            # Search ONLY this glyph's OWN box rows (+/- a 3px pad for the
                            # ~1px-tight box), NOT the full crop height: otherwise ink from the
                            # line / header ABOVE (or a rule below) in these same columns drags
                            # the band top to row 0, which made the seat scale the digit UP and
                            # mis-place it (a '5' ballooned and read as a 'b').
                            _r0 = max(0, _y0 - 3); _r1 = min(_cF.shape[0], _y1 + 4)
                            # DENSE rows only (>=25% of this glyph's peak column-coverage): the
                            # .any(1) extent catches degradation speckle a few px above/below and
                            # puts the band on the LINE, not the TEXT -- so pure-x-height runs (which
                            # fall back to THIS band) rode a couple px off. Speckle rows are sparse.
                            _gp = (_cF[_r0:_r1, _x0:_x1] > 0.30).mean(1)
                            if _gp.size and _gp.max() > 0:
                                _gd = np.where(_gp >= 0.25 * _gp.max())[0]
                                if _gd.size:
                                    # LARGEST CONTIGUOUS run only. A too-tall glyph box (a
                                    # PARAGRAPH line whose crop swallowed the next line's top)
                                    # puts the neighbour's ink in a SECOND dense run across the
                                    # inter-line whitespace; plain min/max jumps that gap and
                                    # doubles the band (a date rendered ~1.6x tall). Splitting on
                                    # gaps >1 row and keeping the longest run pins the band to this
                                    # glyph's own body. Single-line boxes are one run -> unchanged.
                                    _rn = np.split(_gd, np.where(np.diff(_gd) > 1)[0] + 1)
                                    _bg = max(_rn, key=len)
                                    _tops.append(_r0 + int(_bg.min()))
                                    _bots.append(_r0 + int(_bg.max()))
                    if _tops:
                        # Anchor BOTH edges to the digits' robust extent: the cap line at the
                        # MEDIAN top, the baseline at the 80th-PERCENTILE bottom. The fuller
                        # digits define where the row truly sits -- a plain median bottom runs
                        # ~1px high (synth lands short of the baseline) and a per-glyph height
                        # over-reaches the top by ~1px (synth too tall up top). The median top +
                        # high-pct bottom pins the synth to the scanned digits' real top and
                        # bottom on every page.
                        band = (int(round(float(np.median(_tops)))),
                                int(round(float(np.percentile(_bots, 80)))))
                except Exception:
                    band = (-1, -1)
                ctx["_scan_band"] = band
            if band[0] >= 0 and strip is not None:
                try:
                    _sc = (255.0 - strip.mean(2)) / 255.0
                    _iy = np.where((_sc > 0.30).any(1))[0]
                    if _iy.size:
                        _st, _sb = int(_iy.min()), int(_iy.max())
                        _sh = _sb - _st + 1
                        _bbot = band[1]          # seat to the scan baseline EXACTLY: a +1
                        # here scaled the synth 1px taller and dropped it below the line, so it
                        # read as "too tall" / hanging below the scanned digits.
                        _th = _bbot - band[0] + 1
                        # caps/digits -> branch 1 (fill band); LOWERCASE -> branch 2 (x-height body)
                        if _th > 1 and (_sh <= 1.15 * _th or _run_num) and not _lc:  # same class, or a numeric run -> seat to band
                            _scl = _th / float(_sh)
                            _g = strip[_st:_sb + 1]
                            _nh = max(1, int(round(_g.shape[0] * _scl)))
                            _gr = cv2.resize(
                                _g, (_g.shape[1], _nh),
                                interpolation=cv2.INTER_AREA if _scl < 1 else cv2.INTER_LINEAR)
                            _o = np.empty_like(strip); _o[:] = paper_u8
                            _dtop = band[0]              # top anchored, grows down to baseline
                            _a = max(0, _dtop); _b = min(strip.shape[0], _dtop + _nh)
                            if _b > _a:
                                _o[_a:_b] = _gr[_a - _dtop:_a - _dtop + (_b - _a)]
                                strip = np.ascontiguousarray(_o)
                        elif _th > 1:
                            # MIXED-HEIGHT run (typed words with ascenders/descenders): the seat
                            # above squashes the whole extent into the cap band. Instead pin the
                            # CAP-line-to-baseline to the band and let ascenders/descenders run
                            # past it. Cap-line + baseline are read from the DENSE body cluster
                            # (the x-height/cap mass, above any descender). Without this a
                            # word fell to the coarse size_factor and rendered oversized + clean
                            # next to the scan (the "fell back to shit" on multi-letter typing).
                            _dens = (_sc > 0.30).mean(1)
                            _mx = float(_dens.max())
                            _dmask = ((_dens >= 0.5 * _mx) if _mx > 0
                                      else np.zeros(len(_dens), bool))
                            _rows = np.where(_dmask)[0]
                            # baseline is KNOWN -- _synth_strip rendered the glyph with its baseline
                            # at base_y / local_by (off = em*2*ppi - base_y). Do NOT re-measure it
                            # from the degraded ink: _rows.max() lands on a descender TAIL (g/j/p/q/y)
                            # or wanders with dropout, so _caph spanned the whole glyph and scaling
                            # that into the x-height band COMPRESSED the 'y' into the line with no
                            # descent -- non-deterministically, because the dropout is random. Anchor
                            # on the known baseline; the descender below it then renders full length
                            # every time. (No-descender runs have _rows.max() == baseline already, so
                            # this is a no-op for them.)
                            _base = (int(round(local_by)) if local_by is not None
                                     else (int(_rows.max()) if _rows.size else _sb))
                            _base = max(1, min(_base, strip.shape[0] - 1))
                            # Cap-line anchor = TOP of the dense cluster that CONTAINS the baseline
                            # (the x-height line on a lowercase line, the cap-line on a caps line) --
                            # NOT the ink top _st and NOT the global _dmask.min(). A row of 'i'/'j'
                            # dots, or a lone ascender/cap tip, forms a SEPARATE dense band floating
                            # ABOVE the body across a paper gap; _dmask.min() snapped onto it and
                            # over-measured the height, so the seat scaled the whole run ~27% too
                            # small (it read as tiny + sitting low). Walk UP from the baseline and
                            # stop once a real gap (>2 blank rows) is cleared, dropping that floating
                            # band; gap<=2 tolerates the body's own 1px holes. An all-tall run
                            # (caps/digits) has no gap above the body, so _top stays at the ink top
                            # -- a no-op.
                            _top = _base; _g = 0
                            for _r in range(_base - 1, -1, -1):
                                if _dmask[_r]:
                                    _top = _r; _g = 0
                                else:
                                    _g += 1
                                    if _g > 2:
                                        break
                            _caph = _base - _top + 1
                            # MIXED run (has a cap or digit): its cap line is reached by SPARSE
                            # verticals (H, F, l) that the density walk-up misses -- it stops at the
                            # x-height, then _th/_caph scales the whole run UP and the caps punch
                            # ABOVE the band. The strip is a clean synth (no speckle), so its real
                            # ink top ``_st`` IS the cap line; anchor _caph there so caps land ON the
                            # band. Lowercase runs keep the x-height body walk-up (+ _xcr) as-is.
                            if any(c.isupper() or c.isdigit() for c in (run_text or "")):
                                _top = min(_top, _st)
                                _caph = _base - _top + 1
                            # After the uniform size_factor the strip's x-height is already ~ _th,
                            # so this fine re-scale should be NEAR 1. If the dense-mask walk-up
                            # FRAGMENTED -- fax/degradation pixel-dropout splits the bowl across a
                            # >2px gap, so the walk-up stops early -- _caph comes out far too small
                            # and _th/_caph EXPLODES the glyph (a 'y' stretched ~2x, its bowl pushed
                            # above the paste band and clipped to a bare '/'; non-deterministic
                            # because the dropout is random per render). Trust the size_factor (no
                            # re-scale) when _caph is implausibly small, and clamp so a noisy
                            # measurement can never blow the glyph up or crush it.
                            # _xcr<1 for a lowercase run: seat its x-height BODY to the x-height
                            # target (band * font x/cap), so it is not stretched to cap height.
                            _scl = (_th * _xcr / float(_caph)) if _caph > 0.6 * _th else 1.0
                            _scl = max(0.6, min(_scl, 1.5))
                            if _caph > 1:
                                _nh = max(1, int(round(strip.shape[0] * _scl)))
                                _gr = cv2.resize(
                                    strip, (strip.shape[1], _nh),
                                    interpolation=cv2.INTER_AREA if _scl < 1 else cv2.INTER_LINEAR)
                                _o = np.empty_like(strip); _o[:] = paper_u8
                                _off = band[1] - int(round(_base * _scl))   # baseline -> band[1]
                                _s0 = max(0, -_off); _s1 = min(_nh, strip.shape[0] - _off)
                                if _s1 > _s0:
                                    _o[_s0 + _off:_s1 + _off] = _gr[_s0:_s1]
                                    strip = np.ascontiguousarray(_o)
                except Exception:
                    pass
        if strip is not None and run_text.strip():     # TRACE synth-piece sizing (crushed-glyph bug)
            _dsc = (255.0 - strip.mean(2)) / 255.0
            _diy = np.where((_dsc > 0.30).any(1))[0]
            _dib = ctx.get("_seat_band") or ctx.get("_scan_band")
            dlog("RENDER", "run_seat", run=run_text[:10],
                 sf=round(float(ctx.get("_size_factor") or -1), 3),
                 band=str(tuple(int(v) for v in _dib) if _dib else None),
                 seat=(ctx.get("_seat_band") is not None),
                 inkH=int(_diy.max() - _diy.min() + 1) if _diy.size else 0,
                 stripH=int(strip.shape[0]), stripW=int(strip.shape[1]),
                 fill=round(float((_dsc > 0.30).mean()), 2))
        rbear = 0.0
        if strip is not None:
            scov = (255.0 - strip.mean(2)) / 255.0
            cols = np.where(scov.max(0) > 0.20)[0]
            if cols.size:
                # The changed run's LAST glyph right side-bearing (advance space past its ink),
                # dropped by the ink-crop. Without it the kept suffix butts against a narrow
                # last glyph (e.g. a 'j') -- the ~1px gap instead of normal letter spacing.
                rbear = float(strip.shape[1] - (int(cols.max()) + 1))
                strip = np.ascontiguousarray(strip[:, cols.min():cols.max() + 1])
            if abs(condense - 1.0) > 0.02 and strip.shape[1] > 1:
                rbear *= condense                         # bearing scales with the strip
                nw = max(1, int(round(strip.shape[1] * condense)))
                strip = np.ascontiguousarray(cv2.resize(
                    strip, (nw, strip.shape[0]), interpolation=cv2.INTER_AREA))
            # WEIGHT MATCH: the per-word matched font can be LIGHTER than the scanned ink (a bold
            # condensed scan matched to a regular/thin bank face), so the synth run reads thin next
            # to the scan (measured 0.4-0.7x stroke on this card). Measure the scan line's own
            # stroke width (60th-pct distance transform x2) and this strip's, and THICKEN the synth
            # by grayscale erosion (dark spreads) to match -- font-agnostic, works for any face.
            # Fractional via a blended extra erode so the step is not a coarse 2px. Only thickens,
            # never thins, capped so a mis-match can't blob. Scan stroke cached per ctx.
            _ssw = ctx.get("_scan_sw")
            if _ssw is None:
                _ssw = 0.0
                if region is not None:
                    _rm = ((255.0 - region.mean(2)) / 255.0 > 0.4).astype(np.uint8)
                    if int(_rm.sum()) >= 20:
                        _rdt = cv2.distanceTransform(_rm, cv2.DIST_L2, 3)
                        _ssw = float(np.percentile(_rdt[_rm > 0], 60)) * 2.0
                ctx["_scan_sw"] = _ssw
            if _ssw > 0 and strip is not None:
                _tm = ((255.0 - strip.mean(2)) / 255.0 > 0.4).astype(np.uint8)
                if int(_tm.sum()) >= 10:
                    _tdt = cv2.distanceTransform(_tm, cv2.DIST_L2, 3)
                    _tsw = float(np.percentile(_tdt[_tm > 0], 60)) * 2.0
                    _f = float(np.clip((_ssw - _tsw) / 2.0, 0.0, 2.5))
                    if _f > 0.05:
                        _k = np.ones((3, 3), np.uint8)
                        _e = cv2.erode(strip, _k, iterations=int(_f))
                        _fr = _f - int(_f)
                        if _fr > 0.05:
                            _e2 = cv2.erode(_e, _k)
                            _e = _e.astype(np.float32) * (1 - _fr) + _e2.astype(np.float32) * _fr
                        strip = np.ascontiguousarray(np.clip(_e, 0, 255).astype(np.uint8))
            # ITALIC MATCH: the OCR has NO slant detection, so an italic scanned line (the card's
            # body text) gets an upright box and the synth run comes out upright while the scan
            # leans. Measure the scan line's slant -- the horizontal shear that best verticalises
            # its strokes (peaks the column-sum energy) -- and SHEAR the synth to match, so inserted
            # text leans with the line. Only applied for a clearly-italic line (>0.08); upright
            # lines measure ~0 and are untouched. Cached per ctx. (Oblique of the matched face, not
            # a true italic file -- matching the LEAN is the goal here; a true italic FILE is the
            # separate font-family pass.)
            _sl = ctx.get("_scan_slant")
            if _sl is None:
                _sl = 0.0
                if region is not None:
                    _im = ((255.0 - region.mean(2)) / 255.0 > 0.4).astype(np.uint8)
                    if int(_im.sum()) >= 50:
                        _Hh, _Ww = _im.shape; _md = _Hh / 2.0; _bv = -1.0; _bs = 0.0
                        for _sv in np.arange(0.0, 0.45, 0.03):
                            _M = np.float32([[1, _sv, -_sv * _md], [0, 1, 0]])
                            _shd = cv2.warpAffine(_im, _M, (_Ww, _Hh), flags=cv2.INTER_NEAREST)
                            _cs = _shd.sum(0).astype(np.float64); _vv = float((_cs ** 2).mean())
                            if _vv > _bv:
                                _bv, _bs = _vv, float(_sv)
                        _sl = _bs
                ctx["_scan_slant"] = _sl
            if _sl > 0.08 and strip is not None:
                _H2, _W2 = strip.shape[:2]; _md2 = _H2 / 2.0
                _pad = int(np.ceil(_sl * _H2))
                _cv = np.empty((_H2, _W2 + 2 * _pad, 3), np.uint8); _cv[:] = paper_u8
                _cv[:, _pad:_pad + _W2] = strip
                _Mi = np.float32([[1, -_sl, _sl * _md2], [0, 1, 0]])
                strip = cv2.warpAffine(
                    _cv, _Mi, (_W2 + 2 * _pad, _H2), flags=cv2.INTER_LINEAR,
                    borderMode=cv2.BORDER_CONSTANT, borderValue=tuple(int(v) for v in paper_u8))
                # re-crop to the sheared ink so the added _pad columns do not become GAPS around
                # the leaned run (they read as extra spaces).
                _isc = (255.0 - strip.mean(2)) / 255.0
                _icc = np.where(_isc.max(0) > 0.20)[0]
                strip = np.ascontiguousarray(
                    strip[:, _icc.min():_icc.max() + 1] if _icc.size else strip)
            # PRESERVE typed leading/trailing SPACES: the ink-crop above drops them (no ink),
            # which would butt a newly typed word against the kept suffix and make the caret
            # over-run the glyphs. Pad their advance back as paper so the gap (and the caret)
            # are right.
            sp = float(synth_font.text_length(" ", sem)) * ppi * condense
            lpad = int(round((len(run_text) - len(run_text.lstrip(" "))) * sp))
            rpad = int(round((len(run_text) - len(run_text.rstrip(" "))) * sp))
            if (lpad or rpad) and strip is not None:
                padded = np.empty((strip.shape[0], strip.shape[1] + lpad + rpad, 3), np.uint8)
                padded[:] = paper_u8
                padded[:, lpad:lpad + strip.shape[1]] = strip
                strip = padded
        return strip, rbear

    def _font_median_gap(self, font, em, ppi):
        """The MEDIAN inter-letter gap of ONE font at ``em``, in render px (before condense).
        A per-FONT-TYPE spacing value: an inserted glyph is then spaced the way THAT font
        naturally sets letters, instead of the line-wide scan span (a mean/odd-wide pair, or a
        mix of fonts, skews that). MEDIAN, not average, so a single fat pair can't drag it.
        Measured once per (font, em) from a representative lowercase string and cached."""
        import numpy as np
        try:
            key = ((getattr(font, "name", "") or "?"),
                   round(float(em), 1), round(float(ppi), 2))
        except Exception:
            key = None
        cache = getattr(self, "_font_gap_cache", None)
        if cache is None:
            cache = self._font_gap_cache = {}
        if key is not None and key in cache:
            return cache[key]
        gap = 0.12 * float(em) * float(ppi)            # fallback: ~12% of em
        try:
            s = "etaoinshrdlcumwfgvy"                  # common lowercase, varied widths
            runw = float(font.text_length(s, em))
            d = fitz.open(); pg = d.new_page(width=runw + 2 * em, height=em * 3)
            tw = fitz.TextWriter(pg.rect)
            tw.append((em, em * 2.0), s, font=font, fontsize=em)
            tw.write_text(pg)
            pm = pg.get_pixmap(matrix=fitz.Matrix(ppi, ppi), alpha=False)
            a = np.frombuffer(pm.samples, np.uint8).reshape(
                pm.height, pm.width, pm.n)[..., :3]
            col = ((255.0 - a.mean(2)) / 255.0 > 0.30).any(0)
            runs, i, N = [], 0, len(col)
            while i < N:
                if col[i]:
                    j = i
                    while j < N and col[j]:
                        j += 1
                    runs.append((i, j)); i = j
                else:
                    i += 1
            gaps = [runs[k + 1][0] - runs[k][1] for k in range(len(runs) - 1)]
            gaps = [g for g in gaps if g > 0]
            if gaps:
                gap = float(np.median(gaps))
        except Exception:
            pass
        if key is not None:
            cache[key] = gap
        return gap

    def _clone_glyphs(self, region, ctx, Hr, Wr, paper_u8, xof, src_idx,
                      count, lead_pad, geom, gapm, cb=None):
        """Lay COUNT copies of the well-segmented scanned glyph ``src_idx`` at that glyph's OWN
        caret advance (bowl-to-bowl gap = advance - bowl width; a rightward descender overhangs
        and interleaves under the next glyph via a min-blend). Used to make a typed char that
        duplicates a scanned neighbour (a 'y' next to the 'y' in 'by') out of the neighbour's
        REAL pixels -- a synth copy never matches the scan exactly, so it reads wrong. ``lead_pad``
        paper columns precede the first copy (None => the source glyph's own ink-left bearing, so
        the strip's left edge lands on the source's caret cell and butts onto a trimmed scan crop).
        Returns (strip, gap, ybl, blw, bloff, inkoff) or None when the source is not croppable."""
        import numpy as np
        cx0 = max(0, min(int(round(xof(src_idx))), Wr))
        cx1 = max(cx0, min(int(round(xof(src_idx + 1))), Wr))
        # TIGHTEN to the glyph's OWN char box: a loose caret cell can reach past the glyph and
        # drag in a faint sliver of the NEXT glyph's ink, which inflates the measured width -> a
        # degenerate advance gap AND the clone shoved a whole letter-width off its neighbour. Only
        # trims (never extends past the caret cell), so a tight box is a no-op.
        if cb is not None and 0 <= src_idx < len(cb) and cb[src_idx]:
            cx1 = max(cx0 + 1, min(cx1, int(round(cb[src_idx][2])) + 1))
            cx0 = max(cx0, min(int(round(cb[src_idx][0])), cx1 - 1))
        if cx1 <= cx0:
            return None
        cs = np.array(region[0:Hr, cx0:cx1])
        cov = (255.0 - cs.mean(2)) / 255.0
        rc = (cov > 0.30).mean(1)
        if (rc > 0.85).any():                     # drop a full-width table rule the column caught
            cs[rc > 0.85] = paper_u8
            cov = (255.0 - cs.mean(2)) / 255.0
        cols = np.where(cov.max(0) > 0.20)[0]
        if not cols.size:
            return None
        gl = np.ascontiguousarray(cs[:, int(cols.min()):int(cols.max()) + 1])
        by = int(round(float(ctx.get("base_y") or Hr)))
        bm = ((255.0 - gl[:max(1, by)].mean(2)) / 255.0) > 0.30
        bc = np.where(bm.any(0))[0]
        blw = int(bc.max() - bc.min() + 1) if bc.size else gl.shape[1]
        bloff = int(bc.min()) if bc.size else 0
        inkoff = int(cols.min())
        ybl = cx0 + inkoff + bloff                 # source bowl left in tile cols
        adv = int(round(xof(src_idx + 1) - xof(src_idx)))
        gap = adv - blw                            # natural bowl-to-bowl gap for THIS glyph
        if gap < 1:                                # degenerate advance -> fall back
            gap = max(1, int(round(float((geom or {}).get("inter_px") or gapm))))
        pitch = blw + gap
        lead = inkoff if lead_pad is None else lead_pad
        cw = lead + (count - 1) * pitch + gl.shape[1]
        canvas = np.empty((gl.shape[0], cw, 3), np.uint8)
        canvas[:] = paper_u8
        for k in range(count):
            sx = lead + k * pitch
            sl = canvas[:, sx:sx + gl.shape[1]]
            np.minimum(sl, gl, out=sl)             # descenders interleave, keep darker
        return (np.ascontiguousarray(canvas), gap, ybl, blw, bloff, inkoff)

    def inplace_compose(self, ctx: dict, new_text: str, runs=None,
                        force_synth=None):
        """Re-lay the line as a TEXT EDITOR would, on the scan's paper. The untouched
        COMMON PREFIX + SUFFIX stay the ORIGINAL SCAN PIXELS (the suffix shifts to
        reflow), only the changed MIDDLE is synthesized in the matched font, and the
        tile GROWS to the right to fit (insert never gets clipped). Returns
        ``(tile_rgb, disp_rect)`` -- the tile's placement in DISPLAY points.

        ``runs`` is the optional per-selection styling: a tuple of
        ``(text, bold, italic)`` whose joined text == ``new_text``. A character
        counts as CHANGED (re-synthesized in its own variant FILE) when its TEXT
        or its STYLE differs from the scan's base style -- so bolding an existing,
        otherwise-unedited word re-bakes just that word. ``runs=None`` (no styling)
        reduces EXACTLY to the text-only diff, so the common path is unchanged."""
        import numpy as np
        Hr, Wr, ppi = ctx["Hr"], ctx["Wr"], ctx["ppi"]
        region = ctx["region"]
        left_px, right_px = ctx["left_px"], ctx["right_px"]
        paper_u8 = np.clip(ctx["paper"], 0, 255).astype(np.uint8)
        dx0d, dy0d, _, dy1d = ctx["disp_rect"]
        ot = ctx["orig_text"]
        nt = new_text if new_text is not None else ot

        def disp_for(w, th=None):                     # tile (w,h) px -> display rect
            bot = dy1d if th is None else (dy0d + th / ppi)
            return (dx0d, dy0d, dx0d + w / ppi, bot)

        # Per-character style of the NEW text, and the box's BASE (scan) style. A char
        # is "kept as scan pixels" only when its text AND style match the base, so a
        # pure style change (no text edit) still re-bakes the restyled run.
        base = (bool(ctx.get("want_bold", False)),
                bool(ctx.get("want_italic", False)), None)
        nstyles = self._char_styles(runs, len(nt), base)
        styled = any(st != base for st in nstyles)
        dlog("RENDER", "inplace_compose", ot=ot, nt=nt, styled=styled)

        if nt == ot and not styled and force_synth is None:
            dlog("RENDER", "compose_kept_scan", text=ot)   # no edit -> original scan pixels
            return np.ascontiguousarray(region).copy(), disp_for(Wr)
        try:
            f = fitz.Font(fontfile=ctx["fpath"])
        except Exception:
            return np.ascontiguousarray(region).copy(), disp_for(Wr)
        # PER-WORD font: locate the EDITED word and stash its DISPLAY rect so _edit_synth_font
        # queries the page map AT THAT WORD (each word in a box keeps its own font) instead of
        # over the whole line, which majority-voted to ONE font for the box -- the per-box bug.
        try:
            _cb = (ctx.get("geom") or {}).get("char_boxes")
            _dr = ctx.get("disp_rect")
            if _cb and _dr and len(_cb) == len(ot):
                _pp = 0
                while _pp < len(ot) and _pp < len(nt) and ot[_pp] == nt[_pp]:
                    _pp += 1
                _wi = min(max(_pp, 0), len(ot) - 1)
                _w0 = _wi
                while _w0 > 0 and not ot[_w0 - 1].isspace():
                    _w0 -= 1
                _w1 = _wi
                while _w1 < len(ot) and not ot[_w1].isspace():
                    _w1 += 1
                _bxs = [_cb[k] for k in range(_w0, _w1) if k < len(_cb) and _cb[k]]
                if _bxs:
                    ctx["_edit_disp_rect"] = (
                        _dr[0] + min(b[0] for b in _bxs) / ppi,
                        _dr[1] + min(b[1] for b in _bxs) / ppi,
                        _dr[0] + max(b[2] for b in _bxs) / ppi,
                        _dr[1] + max(b[3] for b in _bxs) / ppi)
        except Exception:
            pass
        # Synth font + size + condense from the SHARED metrics (the caret edge measure reads
        # the same, so the caret tracks the glyphs as they render). em scales the box font to
        # the scan's ink span.
        synth_font, lfb, synth_fpath, em, sem, condense = self._synth_metrics(ctx)
        # Which TTF the synth glyph is actually drawn in (vs the scan). basename guarded so
        # this log line can never raise (a throwing dlog arg aborts the edit -- see no-kickout).
        dlog("RENDER", "synth_font",
             base=os.path.basename(ctx.get("fpath") or "") or "?",
             synth=os.path.basename(synth_fpath or "") or "?",
             em=round(float(em), 1), condense=round(float(condense), 2),
             seeded=bool(ctx.get("style_seeded")))

        # The diff: common prefix p + common suffix s; the changed middle is synth. A
        # char only extends the prefix/suffix when BOTH its text and its style match
        # the scan base -- a restyled-but-same-text char falls into the changed middle.
        def cs(i):
            return nstyles[i] if 0 <= i < len(nstyles) else base
        # CAP the common prefix/suffix at difflib's leading/trailing MATCHED-BLOCK sizes, so a
        # COINCIDENTAL repeated char can't extend them past the real unchanged run. Typing
        # 'today' before the ':' of 'by:' made the RAW common suffix grab 'y:' (both strings end
        # 'y:'), which kept the ORIGINAL 'by' y as scan but placed it at the END (under the
        # 'today' y) and synthesized a fake y into 'by'. difflib aligns by longest contiguous
        # block, so it keeps 'red by' (the real y in place) and leaves only ':' as the suffix.
        import difflib as _dl
        _mb = _dl.SequenceMatcher(None, ot, nt, autojunk=False).get_matching_blocks()
        _pcap = _mb[0].size if (_mb and _mb[0].a == 0 and _mb[0].b == 0) else 0
        _rb = [b for b in _mb if b.size > 0]
        _scap = (_rb[-1].size if (_rb and _rb[-1].a + _rb[-1].size == len(ot)
                                  and _rb[-1].b + _rb[-1].size == len(nt)) else 0)
        p = 0
        while p < _pcap and ot[p] == nt[p] and cs(p) == base:
            p += 1
        s = 0
        while s < _scap and s < len(ot) - p and s < len(nt) - p \
                and ot[-1 - s] == nt[-1 - s] and cs(len(nt) - 1 - s) == base:
            s += 1
        # The edit's character span (prefix end .. suffix start in ot), so _synth_strip can
        # measure the degradation from the IMMEDIATE NEIGHBOUR glyphs of this exact edit.
        ctx["_edit_pos"] = (p, len(ot) - s)
        # FORCE a range to re-synthesize even when its text is UNCHANGED (a glyph a
        # table rule crossed on a moved box: re-render it clean from the matched font
        # instead of carrying the scarred scan). Shrinks the kept prefix/suffix to
        # expose exactly [i0..i1]; a no-op edit forces precisely that range.
        if force_synth is not None:
            fi0, fi1 = int(force_synth[0]), int(force_synth[1])
            sfix = max(0, len(nt) - 1 - fi1)
            if nt == ot and not styled:
                p, s = max(0, min(fi0, len(nt))), min(sfix, len(nt))
            else:
                p, s = max(0, min(p, fi0)), max(0, min(s, sfix))
        changed_new = nt[p:len(nt) - s]
        changed_styles = nstyles[p:len(nt) - s]

        # POSITION every character from the binary MAP's edges -- the SAME edges the caret
        # measure reads (via _char_edges) -- so the composite and the caret agree BY
        # CONSTRUCTION (a cut lands exactly where the caret sits). Falls back to a font-
        # advance layout only when the map could not segment this line.
        oedges = self._char_edges(ctx, ot, f)

        def xof(i):
            return oedges[max(0, min(int(i), len(oedges) - 1))]

        xp = xof(p)                                   # prefix ends (left of char p)
        xs_orig = xof(len(ot) - s)                    # suffix starts in the original
        # EXACT SEAT: size the synth to the glyph RIGHT NEXT TO the edit, not a whole-line
        # percentile band (which mixed caps with digits and ran ~1px short). Find the nearest
        # same-class scanned neighbour and seat to ITS true ink box -- _contig_extent stops at
        # the first ink gap, so it returns the glyph's real cap/x-to-baseline run and ignores
        # any sub-baseline next-line bleed. The synth then comes out EXACTLY as tall as the
        # glyphs beside it. Reset per call; falls back to the line band when no neighbour fits.
        ctx["_seat_band"] = None
        _geomc = ctx.get("geom")
        _cbb = (_geomc or {}).get("char_boxes")
        if _cbb and region is not None and (nt[p:len(nt) - s]).strip():

            _ASC = set("bdfhklt")
            _XH = set("acemnorsuvwxz")
            _DESC = set("gjpqy")

            def _cls(c):
                if c.isdigit() or c.isupper() or c in _ASC:
                    return "t"          # tall: cap / digit / ascender -> reaches the cap line
                if c in _XH:
                    return "x"          # x-height body -> reaches the x line
                if c in _DESC:
                    return "g"          # descender (kept natural by the band-seat skip)
                return None
            _tgt = next((_cls(c) for c in nt[p:len(nt) - s] if _cls(c)), None)
            _nbb = None
            if _tgt is not None:                    # nearest SAME-SUB-CLASS scanned neighbour,
                _cand = ([k for k in range(p - 1, max(-1, p - 6), -1)]   # searching a few out
                         + [k for k in range(len(ot) - s, min(len(ot), len(ot) - s + 5))])
                for _ni in _cand:
                    if 0 <= _ni < len(ot) and _ni < len(_cbb) and _cbb[_ni] \
                            and _cls(ot[_ni]) == _tgt:
                        _nbb = _cbb[_ni]
                        break
            if _nbb is not None:
                _cfn = (255.0 - region.mean(2)) / 255.0
                _nx0, _ny0, _nx1, _ny1 = (int(v) for v in _nbb)
                _nr0 = max(0, _ny0 - 3)
                _nr1 = min(_cfn.shape[0], _ny1 + 4)
                # FULL ink extent (min..max), NOT _contig_extent: a fax-degraded digit has a
                # faint middle that reads as a >3px row gap, and _contig_extent stops there --
                # underreading a 46px digit as ~31px, so the synth seated too short. The tight
                # per-line cover (set at OCR) already excludes the next line, so the full span
                # is this glyph's true cap/x-to-baseline height.
                _nrows = np.where((_cfn[_nr0:_nr1, _nx0:_nx1] > 0.30).any(1))[0]
                if _nrows.size:
                    _bt, _bb = _nr0 + int(_nrows.min()), _nr0 + int(_nrows.max())
                    # The OCR char box is unreliable for sizing: it can be too SMALL (a 48px box on
                    # a 66px digit -> synth too short) OR too BIG (a box spilling onto the next line
                    # -> synth too tall / an ascender clipped). Prefer the contiguous ink BLOB that
                    # CONTAINS the box centre over the FULL line column -- one glyph's real cap-to-
                    # baseline run, split by white from other lines. Bridge <=4px faint fax dropouts.
                    # Fall back to the box+pad reading only when the centre lands in an ink gap (a
                    # faint-middle degraded glyph) or the blob is implausibly tall (merged with a
                    # neighbouring line in very tight leading).
                    _fm = (_cfn[:, _nx0:_nx1] > 0.30).any(1)
                    _ctr = max(0, min((_ny0 + _ny1) // 2, len(_fm) - 1))
                    _rw = np.where(_fm)[0]
                    _seg = None
                    if _rw.size:
                        _cs2 = _ce2 = int(_rw[0])
                        for _rv in _rw[1:]:
                            _rv = int(_rv)
                            if _rv - _ce2 <= 4:
                                _ce2 = _rv
                            else:
                                if _cs2 <= _ctr <= _ce2:
                                    _seg = (_cs2, _ce2); break
                                _cs2 = _ce2 = _rv
                        if _seg is None and _cs2 <= _ctr <= _ce2:
                            _seg = (_cs2, _ce2)
                    if _seg is not None and (_seg[1] - _seg[0] + 1) <= 0.85 * _cfn.shape[0]:
                        _bt, _bb = _seg
                    # TIGHTEN to the DENSE glyph rows: the extent above uses .any(1), so a few
                    # degradation-speckle pixels a couple px ABOVE/BELOW the real glyph pull the
                    # band onto the LINE, not the TEXT -- the seated synth then renders a couple px
                    # too tall and rides a hair high (verified against tight glyph lines). Trim to
                    # the rows that carry >=25% of the glyph's PEAK column-coverage; a faint-middle
                    # row is still bracketed by the dense top/bottom so this never shrinks a real
                    # glyph, it only sheds the sparse speckle.
                    _dp = (_cfn[_bt:_bb + 1, _nx0:_nx1] > 0.30).mean(1)
                    if _dp.size and _dp.max() > 0:
                        _dr = np.where(_dp >= 0.25 * _dp.max())[0]
                        if _dr.size:
                            _bt, _bb = _bt + int(_dr.min()), _bt + int(_dr.max())
                    ctx["_seat_band"] = (_bt, _bb)
        # COVER / erase BY CHARACTER INDEX, never by fuzzy x-overlap -- that fuzzy lookup
        # was the root of the 'HH' double-erase: two identical adjacent glyphs got re-
        # discovered by position and conflated, wiping both. The diff gives p / s as
        # character indices, which map DIRECTLY to the map's per-char boxes: keep the
        # prefix up to char (p-1)'s right edge, restart the suffix at char (len-s)'s left
        # edge, erase exactly the span between. char_boxes keeps repeated letters distinct
        # by index, so only the intended glyph is ever cleared. The new glyph then takes
        # the OLD glyph's exact ink slot (same left edge, same surrounding gaps); the rest
        # of the line shifts only by the WIDTH difference, so the scan's own inter-letter
        # spacing is preserved and the replacement does not look obvious.
        geom = ctx.get("geom")
        cb = (geom or {}).get("char_boxes")
        have_geom = bool(cb) and len(cb) == len(ot)
        xh = float((geom or {}).get("xheight") or (em * ppi))
        # NORMAL inter-letter span = the MATCHED FONT's own median letter gap (per font type),
        # scaled by the box condense, NOT the line-wide scan span -- the scan median mixes fonts
        # and gets dragged by the odd wide pair (an inserted glyph then sat too far from the
        # prefix). Falls back to the scan value if the font can't be measured.
        gapm = (self._font_median_gap(synth_font, sem, ppi) * condense
                if synth_font is not None
                else float((geom or {}).get("inter_px") or 0.15 * xh))
        pre_x1, suf_x0, cx1 = xp, xs_orig, xs_orig
        old_left = xp
        if have_geom:
            ns = len(ot) - s                          # first KEPT suffix char index
            pre_x1 = float(cb[p - 1][2]) if p > 0 else float((geom or {}).get("x0", xp))
            suf_x0 = float(cb[ns][0]) if s > 0 else float(right_px)
            # The OLD glyph(s) being replaced occupy the ink slot [old_left, cx1]; the new
            # glyph takes that SAME slot so the gaps around it stay intact. (Append at the
            # end has no old slot -> start one inter-letter gap past the prefix.)
            old_left = float(cb[p][0]) if p < len(ot) else (pre_x1 + gapm)
            cx1 = float(cb[ns - 1][2]) if ns - 1 >= p else old_left

        # FONT (bank edge/contour match), cap-anchored SIZE ``sem`` and horizontal CONDENSE
        # all come from _synth_metrics above -- height-anchored so a narrow/wide face does not
        # blow the glyph up, condensed so the synth is not fatter than its scanned neighbours.
        # Render the synth, then CROP it to its own ink so we place the GLYPH, not the
        # font's side bearings.
        # Place the synth on the baseline of the glyph's REAL NEIGHBOURS (the per-char map),
        # not the line-global baseline -- so an edit beside a descender or inter-line bleed
        # still sits on the line where the surrounding letters do.
        cbase = ctx.get("char_baseline")
        local_by = (cbase[max(0, min(p, len(cbase) - 1))]
                    if cbase and len(cbase) == len(ot) else None)
        # CASCADE FIX: when TWO (or more) NON-adjacent chars change (e.g. a date
        # 5/16/2024 -> 5/12/2026, only index 3 and 8 differ), the changed MIDDLE
        # [p, len(nt)-s) sweeps the UNCHANGED chars between them ('/202') -- the old
        # single-strip render re-synthesized all of them, losing those scan pixels.
        # Instead build the middle as a left-to-right concat of pieces: each maximal
        # run of CHANGED chars is synthesized (one _render_run_strip), each maximal
        # run of UNCHANGED chars is the REAL SCAN crop of those glyphs' boxes. So only
        # the actually-edited glyphs are re-rendered; everything else stays scan.
        mid_lo, mid_hi = p, len(nt) - s
        # mid_hi indexes the NEW text; on an insert it runs past the end of ``ot`` (the new
        # text is longer), so guard the ot lookup -- an out-of-range position is by
        # definition a changed (inserted) char. Without this guard a multi-char insert/delete
        # raised IndexError here, inplace_compose threw, and the editor fell back to
        # re-rasterizing the WHOLE line in the synth font (the "multi-char edit falls apart"
        # path); single-char edits never hit it because their mid stays inside ot.
        # Build the changed MIDDLE keeping every UNCHANGED glyph as its real SCAN crop and
        # synthesizing ONLY the genuinely changed/added runs -- for ANY edit, INSERT and DELETE
        # included. A char DIFF (difflib) aligns the old and new middles, so a scattered
        # multi-insert (typing into several spots, or any length-changing edit) does NOT
        # re-synthesize the unchanged glyphs between the edits. The old cascade only handled
        # EQUAL-LENGTH edits (a char-by-char nt[i]!=ot[i] scan); any insert/delete fell back to
        # one whole-middle synth that re-rendered every unchanged glyph in the span -- the "we
        # are not doing in-place edits" bug. Single-region edits (no unchanged middle glyphs)
        # still take the one-strip path below, byte-identical to before.
        ot_mid = ot[mid_lo:len(ot) - s]
        nt_mid = nt[mid_lo:mid_hi]
        ops = None
        if (nt != ot and not styled and force_synth is None
                and have_geom and cb and len(cb) == len(ot) and len(ot_mid) > 0):
            import difflib
            ops = difflib.SequenceMatcher(None, ot_mid, nt_mid,
                                          autojunk=False).get_opcodes()
        def _run_reliable(a, b):
            # Are glyph boxes [a:b) usable as a SCAN CROP? Heavy touching text mis-segments
            # into 1px slivers / overlapping boxes; cropping those duplicates or clips glyphs
            # (the 'EFFECTI8FEVE' garble). Such a run is re-synthesized instead (same text,
            # clean render) -- only well-segmented runs (digits, spaced letters) stay scan.
            for k in range(a, b):
                bx = cb[k] if 0 <= k < len(cb) else None
                if not bx or (bx[2] - bx[0]) < 4:
                    return False
                if k > a and cb[k - 1] and bx[0] < cb[k - 1][2] - 1:
                    return False
            return True

        keep_scan = ops is not None and any(
            t == "equal" and _run_reliable(mid_lo + i1, mid_lo + i2)
            for t, i1, i2, j1, j2 in ops)
        # COPY-NOT-SYNTH for a DUPLICATED glyph: inserting a char IDENTICAL to an immediate
        # scanned neighbour (a 'y' typed next to the 'y' in 'by') -- a synthesized copy never
        # matches the scan exactly so it reads wrong, AND because the string is the same whether
        # the new glyph was typed before or after the original, the diff can place the synth in
        # the "wrong" slot. Cloning the neighbour's REAL scan pixels fixes both: the two glyphs
        # are now pixel-identical, so the slot no longer matters and there is no weight mismatch.
        # ONLY for a pure single-char insert whose char equals a well-segmented scanned neighbour;
        # the cloned strip then flows through the SAME erase/place/paste path as a synth glyph.
        copy_strip = None
        _copy_gap = None
        _copy_lead = None
        if (have_geom and not styled and force_synth is None
                and changed_new and len(set(changed_new)) == 1
                and p == len(ot) - s):                   # insert of ONE repeated char
            _cch = changed_new[0]
            _ci = None
            if 0 <= p - 1 < len(ot) and ot[p - 1] == _cch and _run_reliable(p - 1, p):
                _ci = p - 1
            elif (len(ot) - s) < len(ot) and ot[len(ot) - s] == _cch \
                    and _run_reliable(len(ot) - s, len(ot) - s + 1):
                _ci = len(ot) - s
            if _ci is not None:
                _res = self._clone_glyphs(region, ctx, Hr, Wr, paper_u8, xof, _ci,
                                          len(changed_new), 0, geom, gapm, cb=cb)
                if _res is not None:
                    copy_strip, _copy_gap, _ybl, _blw, _bloff, _inkoff = _res
                    if _ci == p - 1:        # first clone bowl one gap past the source bowl right
                        _copy_lead = _ybl + _blw + _copy_gap - _bloff
                    dlog("RENDER", "copy_glyph", ch=changed_new, src=int(_ci),
                         n=len(changed_new))
        # mid_rel: EXACT per-character x-edges WITHIN the changed strip (tile px, one per
        # changed_new char + the trailing edge). The live caret reads these (via _live_edges)
        # so it sits on the real glyph boundaries -- kept-scan pieces carry their scanned edges,
        # clones the clone pitch, synth the font advances. None => caret falls back to the synth
        # arithmetic (only right when the whole middle is one plain synth render).
        mid_rel = None
        _tail_gap = gapm      # gap opened before the kept suffix (tightened for a trailing clone)
        if copy_strip is not None:
            strip, rbear = copy_strip, 0.0
            _pit = float(_blw + _copy_gap)
            _pw = float(strip.shape[1])
            mid_rel = [min(float(_k) * _pit, _pw)
                       for _k in range(len(changed_new))] + [_pw]
        elif keep_scan:
            dlog("RENDER", "cascade_mixed", nt=nt)
            pieces = []
            _erel = [0.0]                              # char boundaries within the mixed strip
            _ex = 0.0
            _oi = 0
            while _oi < len(ops):
                tag, i1, i2, j1, j2 = ops[_oi]
                if tag == "equal" and _run_reliable(mid_lo + i1, mid_lo + i2):
                    # KEEP real scan pixels. Crop on the MONOTONIC caret edges (not cb[i]
                    # boxes, which can overlap): edge[a]..edge[b] spans the glyphs WITH their
                    # inter-letter gaps and can never duplicate a neighbour.
                    a, b = mid_lo + i1, mid_lo + i2
                    # CLONE-AFTER: the very next op is an INSERT of the char this run ENDS with
                    # (a repeated-char insert typed against an identical scanned glyph, e.g. more
                    # y's after the 'y' in 'by'). Drop that last glyph from the scan crop and
                    # rebuild it + the inserted copies from its REAL pixels -- so a SECOND edit
                    # elsewhere in the box no longer forces the whole run to synthesize.
                    _cn = 0
                    if (b - 1 >= a and _oi + 1 < len(ops)
                            and ops[_oi + 1][0] == "insert"):
                        _run = nt_mid[ops[_oi + 1][3]:ops[_oi + 1][4]]
                        if (_run and len(set(_run)) == 1 and _run[0] == ot[b - 1]
                                and _run_reliable(b - 1, b)):
                            _cn = len(_run)
                    _res = (self._clone_glyphs(region, ctx, Hr, Wr, paper_u8, xof,
                                               b - 1, _cn + 1, None, geom, gapm, cb=cb)
                            if _cn else None)
                    if _res is not None:
                        x0 = max(0, min(int(round(xof(a))), Wr))
                        x1 = max(x0, min(int(round(xof(b - 1))), Wr))  # exclude the cloned glyph
                        if x1 > x0:
                            pieces.append(np.ascontiguousarray(region[0:Hr, x0:x1]))
                        for _c in range(a + 1, b):     # kept chars a..b-2 at scanned edges
                            _erel.append(_ex + float(xof(_c) - xof(a)))
                        _ex += float(x1 - x0)
                        _cst = _res[0]
                        _pit = float(_res[3] + _res[1])           # blw + gap = clone pitch
                        for _c in range(1, _cn + 2):  # source + clones at the clone pitch
                            _erel.append(_ex + float(_c) * _pit)
                        _ex += float(_cst.shape[1])
                        _erel[-1] = _ex               # snap last boundary to the piece end
                        pieces.append(_cst)
                        dlog("RENDER", "copy_glyph", ch=_run, src=int(b - 1), n=_cn)
                        _oi += 2                       # consumed the equal run AND the insert
                        continue
                    x0 = max(0, min(int(round(xof(a))), Wr))
                    x1 = max(x0, min(int(round(xof(b))), Wr))
                    if x1 > x0:
                        pieces.append(np.ascontiguousarray(region[0:Hr, x0:x1]))
                    for _c in range(a + 1, b + 1):    # kept chars at their scanned edges
                        _erel.append(_ex + float(xof(_c) - xof(a)))
                    _ex += float(x1 - x0)
                    _oi += 1
                    continue
                _oi += 1
                if tag == "delete":
                    continue
                sa, sb = mid_lo + j1, mid_lo + j2      # changed/added OR degenerate-equal -> synth
                if sb <= sa:
                    continue
                rstr, _ = self._render_run_strip(
                    ctx, nt[sa:sb], nstyles[sa:sb], base, sem, lfb, synth_fpath,
                    condense, local_by, synth_font, ppi, region, cb, ot,
                    paper_u8, have_geom, Hr, Wr)
                if rstr is not None:
                    if rstr.shape[0] != Hr:            # top-align to Hr rows (paper fill)
                        fit = np.empty((Hr, rstr.shape[1], 3), np.uint8)
                        fit[:] = paper_u8
                        rows = min(Hr, rstr.shape[0])
                        fit[:rows] = rstr[:rows]
                        rstr = fit
                    pieces.append(np.ascontiguousarray(rstr))
                    _rw = float(rstr.shape[1])
                    _nc = sb - sa
                    if synth_font is not None and _nc > 0:
                        _tot = float(synth_font.text_length(nt[sa:sb], sem)) or 1.0
                        for _c in range(1, _nc + 1):
                            _erel.append(_ex + _rw * (float(
                                synth_font.text_length(nt[sa:sa + _c], sem)) / _tot))
                    else:
                        for _c in range(1, _nc + 1):
                            _erel.append(_ex + _rw * float(_c) / float(max(1, _nc)))
                    _ex += _rw
            mixed = (np.ascontiguousarray(np.hstack(pieces))
                     if pieces else None)
            strip, rbear = mixed, 0.0
            if mixed is not None and len(_erel) == len(changed_new) + 1:
                _erel[-1] = float(mixed.shape[1])
                mid_rel = _erel
        else:
            # BOUNDARY CLONE: a typed run duplicating the scanned glyph right BEFORE (prefix's
            # last) or AFTER (suffix's first) the change is cloned from THAT glyph's REAL pixels;
            # only genuinely new glyphs synthesize. This is the pure-insert path (no unchanged
            # middle glyph for the cascade to anchor on), so the neighbour lives in the kept
            # prefix/suffix -- e.g. inserting 'niucs' before the scanned 's' of 'stered' clones
            # that trailing 's'. changed_new = [lead == ot[p-1]] + [synth] + [trail == ot[ns]];
            # a one-gap paper pad separates a synth part from a clone part.
            _ns = len(ot) - s
            _ld = _tr = 0
            if have_geom and not styled and force_synth is None and changed_new:
                if p - 1 >= 0 and _run_reliable(p - 1, p):
                    _lc = ot[p - 1]
                    while _ld < len(changed_new) and changed_new[_ld] == _lc:
                        _ld += 1
                if _ns < len(ot) and _run_reliable(_ns, _ns + 1):
                    _tc = ot[_ns]
                    while _tr < len(changed_new) - _ld and changed_new[-1 - _tr] == _tc:
                        _tr += 1
            _smid = changed_new[_ld:len(changed_new) - _tr]
            _parts, _drel, _dx = [], [0.0], 0.0
            _ok = bool(_ld or _tr)
            _gpad = max(1, int(round(gapm)))
            if _ok and _ld:
                _r = self._clone_glyphs(region, ctx, Hr, Wr, paper_u8, xof,
                                        p - 1, _ld, 0, geom, gapm, cb=cb)
                if _r is not None:
                    _pit = float(_r[3] + _r[1])
                    for _k in range(1, _ld + 1):
                        _drel.append(_dx + float(_k) * _pit)
                    _dx += float(_r[0].shape[1]); _drel[-1] = _dx
                    _parts.append(_r[0])
                    _copy_gap = _r[1]
                    _copy_lead = _r[2] + _r[3] + _r[1] - _r[4]   # snap lead to source rhythm
                else:
                    _ok = False
            if _ok and _smid:
                if _parts:
                    _pd = np.empty((Hr, _gpad, 3), np.uint8); _pd[:] = paper_u8
                    _parts.append(_pd); _dx += _gpad
                _rstr, _ = self._render_run_strip(
                    ctx, _smid, changed_styles[_ld:len(changed_new) - _tr], base, sem, lfb,
                    synth_fpath, condense, local_by, synth_font, ppi, region, cb, ot,
                    paper_u8, have_geom, Hr, Wr)
                if _rstr is None:
                    _ok = False
                else:
                    if _rstr.shape[0] != Hr:
                        _fit = np.empty((Hr, _rstr.shape[1], 3), np.uint8); _fit[:] = paper_u8
                        _rr = min(Hr, _rstr.shape[0]); _fit[:_rr] = _rstr[:_rr]; _rstr = _fit
                    _rw = float(_rstr.shape[1]); _nc = len(_smid)
                    if synth_font is not None and _nc:
                        _tot = float(synth_font.text_length(_smid, sem)) or 1.0
                        for _k in range(1, _nc + 1):
                            _drel.append(_dx + _rw * (float(
                                synth_font.text_length(_smid[:_k], sem)) / _tot))
                    else:
                        for _k in range(1, _nc + 1):
                            _drel.append(_dx + _rw * float(_k) / float(max(1, _nc)))
                    _dx += _rw
                    _parts.append(np.ascontiguousarray(_rstr))
            if _ok and _tr:
                _r = self._clone_glyphs(region, ctx, Hr, Wr, paper_u8, xof,
                                        _ns, _tr, 0, geom, gapm, cb=cb)
                if _r is not None:
                    # space the clone (and the gap before the kept suffix) by the source glyph's
                    # OWN advance gap, not the wide font-median gapm -- so it sits as tight as the
                    # scan sets that letter (the cloned 's' snug against the scanned 's', not adrift).
                    _tail_gap = float(_r[1])
                    _tpad = max(1, int(round(_r[1])))
                    if _parts:
                        _pd = np.empty((Hr, _tpad, 3), np.uint8); _pd[:] = paper_u8
                        _parts.append(_pd); _dx += _tpad
                    _pit = float(_r[3] + _r[1])
                    for _k in range(1, _tr + 1):
                        _drel.append(_dx + float(_k) * _pit)
                    _dx += float(_r[0].shape[1]); _drel[-1] = _dx
                    _parts.append(_r[0])
                else:
                    _ok = False
            if _ok and _parts and len(_drel) == len(changed_new) + 1:
                strip = np.ascontiguousarray(np.hstack(_parts))
                rbear = 0.0
                mid_rel = _drel
                dlog("RENDER", "boundary_clone", ch=changed_new,
                     lead=int(_ld), trail=int(_tr))
            else:
                strip, rbear = self._render_run_strip(
                    ctx, changed_new, changed_styles, base, sem, lfb, synth_fpath,
                    condense, local_by, synth_font, ppi, region, cb, ot, paper_u8,
                    have_geom, Hr, Wr)
        synth_w = (float(strip.shape[1]) if strip is not None
                   else (float(synth_font.text_length(changed_new, sem)) * ppi * condense
                         if changed_new else 0.0))
        if have_geom:
            erase_start = pre_x1                       # keep prefix glyph, clear the rest
            # Cut the kept SUFFIX at the THINNEST real ink column between the changed
            # span and the first kept char -- NOT the geometric midpoint. char_boxes
            # split TOUCHING glyphs at a midpoint guess (_fit_boxes), so a midpoint
            # so0 can land INSIDE the old glyph; the suffix is then copied from
            # region[so0:] and re-imports the old glyph's right half (the "edited
            # glyph covers only half the original" bug). The thinnest column is the
            # true boundary, so the copy starts in (near-)paper past the old ink. The
            # pasted suffix glyph position is shift+suf_x0 (independent of so0), so
            # moving so0 only changes how much leading gap is copied -- safe.
            so0 = int(round((cx1 + suf_x0) / 2.0))     # default = midpoint
            if s > 0:
                bt = max(0, int(ctx.get("ink_top", 0)))
                bb = min(Hr, int(ctx.get("ink_bot", Hr)))
                lo = max(0, min(int(round(cx1)), int(round(suf_x0))))
                hi = min(Wr, max(int(round(cx1)), int(round(suf_x0))) + 4)
                if bb - bt >= 2 and hi - lo >= 2:
                    band = region[bt:bb, lo:hi]
                    gband = band.mean(2) if band.ndim == 3 else band
                    colink = (255.0 - gband.astype(np.float32)).sum(0)
                    so0 = lo + int(np.argmin(colink))   # thinnest column = the real gap
            # NEVER start the suffix crop RIGHT of the first kept glyph's own left edge: on a
            # pure INSERT cx1==suf_x0, so the thinnest-column search above scans INSIDE the
            # first suffix glyph's ink ([suf_x0, suf_x0+4]) and lands past its stem, clipping
            # it (the inserted 'y' left the following 'r' with its left stem cut off). Clamp so
            # the crop begins at-or-before that glyph; for a real REPLACE the gap column is
            # already left of suf_x0, so this is a no-op there.
            so0 = max(0, min(so0, int(round(suf_x0)), Wr))
            if synth_w > 0 and p < len(ot) - s:
                # REPLACE (a glyph was actually replaced -- changed_old is non-empty, i.e.
                # p < len(ot)-s): the new glyph fills the OLD glyph's ink slot, so its
                # surrounding gaps are preserved; the suffix shifts ONLY by the width
                # difference, leaving every other inter-letter gap on the line as scanned.
                # NOTE: a pure MIDDLE INSERT has changed_old empty (p == len(ot)-s) and must
                # fall to the INSERT branch -- the old `p < len(ot)` test sent it here, which
                # opened NO gap so the suffix butted against the inserted run (the j|u bug).
                xink = old_left
                shift = synth_w - max(0.0, cx1 - old_left)
            elif synth_w > 0:
                # INSERT: a brand-new glyph needs ONE inter-letter gap before the kept suffix.
                # Open the suffix by the glyph's INK width + one gap (_tail_gap = the scan's OWN
                # inter-letter spacing -- gapm normally, or a trailing CLONE's tighter advance gap
                # so the cloned glyph sits snug against the scanned one it duplicates). Do NOT also
                # add the run's right side-bearing (rbear): the gap already IS the full visible
                # ink-to-ink gap, so adding rbear on top DOUBLED the space after a single glyph.
                xink = old_left
                shift = synth_w + _tail_gap - max(0.0, suf_x0 - old_left)
            else:
                # DELETE: close the hole to ONE natural gap (0 beside a space) so the
                # survivors keep normal spacing, not a double-width hole.
                gj = 0.0 if ((p > 0 and ot[p - 1].isspace())
                             or (ns < len(ot) and ot[ns].isspace())) else gapm
                xink = pre_x1
                shift = (pre_x1 + gj) - suf_x0
            xs_new = so0 + shift
            # INSERT LEADING GAP: an inserted glyph is anchored at old_left = the NEXT glyph's
            # slot, so it inherits THAT glyph's leading gap -- which can be far wider than a
            # normal letter gap (the 'e   y' over-wide space before an inserted 'y', because the
            # scanned 'e r' pair happened to sit ~6px apart). When the change LEADS with an
            # insert (a pure insert, OR a cascade whose first op is 'insert'), pull the whole
            # placed run left so the inserted glyph sits ONE normal gap (gapm) past the prefix;
            # xs_new moves with it, so the inter-piece spacing is untouched and only the leading
            # gap tightens. A true REPLACE keeps old_left (the new glyph fills the old slot).
            _lead_ins = ((p == len(ot) - s) or
                         (keep_scan and ops and ops[0][0] == "insert"))
            if _lead_ins and synth_w > 0:
                # a CLONE snaps EXACTLY onto its source glyph's caret rhythm (push OR pull), so
                # original->first-clone matches clone->clone; a synth insert only pulls left to one
                # normal inter-letter gap past the prefix (never pushed right past its slot).
                if _copy_lead is not None:
                    _delta = xink - _copy_lead
                else:
                    _delta = max(0.0, xink - (pre_x1 + gapm))
                if _delta:
                    xink -= _delta
                    xs_new -= _delta
        else:
            xink = xp
            erase_start = xp
            so0 = max(0, int(round(xs_orig)))
            xs_new = xp + synth_w + gapm
        so1 = min(Wr, int(round(right_px)) + 2)
        suffix_w = max(0, so1 - so0)

        # GROW the tile to hold everything (no clip): prefix + synth + shifted suffix.
        # ``pad`` is ONLY a rounding / anti-clip margin past the real content extent --
        # NOT a visible trailing gap. 0.35*em added ~one space-width of blank paper to the
        # right whenever the line grew past the cover, which baked an "extra space at the
        # end" every time an edit got longer than the scanned word. right_edge already holds
        # every glyph, so a couple of pixels is all that is needed to absorb rounding.
        pad = max(2, int(0.04 * em * ppi))
        right_edge = max(xink + synth_w, xs_new + suffix_w)  # actual content extent
        Wg = max(Wr, int(np.ceil(right_edge)) + pad)         # never below the cover
        dlog("RENDER", "tile_grow", Wr=int(Wr), right_edge=round(float(right_edge), 1),
             Wg=int(Wg), pad=int(pad), p=int(p), s=int(s),
             synth_w=round(float(synth_w), 1), suffix_w=int(suffix_w), styled=styled)
        # DESCENDER ROOM: the synth strip renders below the cover band now, so GROW the tile
        # (and the display rect, via disp_for(tile_h)) downward -- but ONLY when the strip
        # actually has ink past the cover bottom, so a typed descender keeps its tail while a
        # non-descender edit stays exactly Hr.
        sib = Hr - 1
        if strip is not None:
            _ssc = (255.0 - strip.mean(2)) / 255.0
            _siy = np.where((_ssc > 0.30).any(1))[0]
            if _siy.size:
                sib = int(_siy.max())
        tile_h = max(Hr, sib + 2)
        tile = np.empty((tile_h, Wg, 3), np.uint8)
        tile[:] = paper_u8
        tile[:Hr, :Wr] = region                       # exact scan in the original span
        # Confine EVERY edit to THIS LINE's vertical band. A loose cover overlaps the next
        # line, so a full-height erase/paste would clear or horizontally SHIFT that line's top
        # (it bled into the cover) -- the "next line gets cut and moved" bug. The map's
        # char_boxes are clipped to this line's band, so their top..bottom span is this line's
        # ink; every row OUTSIDE it stays the untouched scan, leaving the neighbour line exactly
        # as it was. Falls back to the dark-mask band when the map is unavailable.
        if have_geom:
            ly0 = max(0, min(int(b[1]) for b in cb) - 1)
            ly1 = min(Hr, max(int(b[3]) for b in cb) + 1)
            # The char boxes can UNDER-measure a tall glyph's real ink (a 48px box on a 66px
            # digit), so this box-derived paste band would RE-CLIP a correctly-seated synth back
            # to the short box height (the "synth digit too small" bug). Widen to the seat band --
            # the scanned neighbour's true cap-to-baseline ink on THIS line -- so the synth keeps
            # the height it was seated to. Still this line's own band, so no next-line bleed.
            _sb = ctx.get("_seat_band")
            if _sb and int(_sb[1]) > int(_sb[0]):
                ly0 = max(0, min(ly0, int(_sb[0])))
                ly1 = min(Hr, max(ly1, int(_sb[1]) + 1))
        else:
            bpad = max(1, int(0.12 * (ctx["ink_bot"] - ctx["ink_top"])))
            ly0, ly1 = max(0, ctx["ink_top"] - bpad), min(Hr, ctx["ink_bot"] + bpad)
        ly1s = min(tile_h, max(ly1, sib + 2))         # strip paste band, incl. the descender
        tile[ly0:ly1s, max(0, int(round(erase_start))):] = paper_u8  # clear old glyph+suffix
        if suffix_w > 0:                              # shifted suffix (real scan pixels, in Hr)
            dxa = max(0, int(round(xs_new)))
            ww = min(so1 - so0, Wg - dxa)
            if ww > 0:
                tile[ly0:ly1, dxa:dxa + ww] = region[ly0:ly1, so0:so0 + ww]
        if strip is not None:                         # synth middle, in matched font (+ tail)
            dxa = max(0, int(round(xink)))
            ww = min(strip.shape[1], Wg - dxa)
            # The strip can be a pixel SHY of the descender-extended paste band (sib+2),
            # so paste only the rows it actually has -- a 1px shortfall used to raise a
            # broadcast error that dropped the whole edit onto the broken fallback raster
            # (the "tiny dotted digit" on an ungrouped line). The rows below stay paper.
            rb = min(ly1s, strip.shape[0])
            if ww > 0 and rb > ly0:
                tile[ly0:rb, dxa:dxa + ww] = strip[ly0:rb, :ww]
        # PAPER GRAIN in the erased gaps: the erase + tile fill leave FLAT paper_u8 between the
        # kept scan and the synth glyph, which reads as a clean whiteout band beside the grainy
        # real paper. Replace any still-exactly-flat paper pixel in the changed band with a faint
        # grain sampled from THIS line's own paper, so the inter-glyph gap matches the scan.
        try:
            pool = region.reshape(-1, 3)
            pg = pool[pool.mean(1) > 200]
            if len(pg) >= 50:
                ex = max(0, int(round(erase_start)))
                sub = tile[ly0:ly1, ex:]
                flat = np.abs(sub.astype(np.int16) - np.asarray(paper_u8, np.int16)).sum(2) < 5
                nf = int(flat.sum())
                if nf:
                    sub[flat] = pg[np.random.RandomState(98765).randint(0, len(pg), nf)]
        except Exception:
            pass
        # TABLE-LINE PRESERVE: the erase + grain above clear the whole edited band, which
        # wipes (and speckles) any table rule that sits in it -- a digit's OCR box can absorb
        # the rule above the line (box top -> the rule row), dragging the erase band up onto
        # it. The MOVE path keeps borders (_fill_cover_preserving_borders); the in-place path
        # did not. Restore the detected rule pixels straight from the original scan so an edit
        # never breaks or degrades a table line.
        self._restore_borders_in_tile(tile, region, ctx, Hr, Wr)
        # LIVE CARET EDGES: emit the EXACT per-character x-edges (display pts) of THIS
        # composite -- prefix at its scanned positions, the synth middle laid across its
        # real rendered width ``synth_w`` (SPACES included, so the caret advances on a
        # typed space), the suffix at its shifted paste position. The caret reads these so
        # it sits where the glyphs actually render, instead of re-deriving them with its
        # own arithmetic (which dropped trailing-space width and drifted off). Cached on
        # the ctx by text; caret_line_edges returns it on a match.
        try:
            def _D(_px):
                return dx0d + float(_px) / ppi
            _oe = oedges
            _comp = [_D(_oe[i]) for i in range(min(p + 1, len(_oe)))]
            _mid = changed_new
            if _mid and mid_rel is not None and len(mid_rel) == len(_mid) + 1:
                # EXACT edges from the composed pieces (kept-scan/clone/synth widths), so the
                # caret lands on the real glyphs even for a cloned or multi-edit middle.
                for _k in range(1, len(_mid) + 1):
                    _comp.append(_D(xink + mid_rel[_k]))
            elif _mid and synth_font is not None:
                _at = float(synth_font.text_length(_mid, sem)) or 1.0
                for _k in range(1, len(_mid) + 1):
                    _comp.append(_D(xink + synth_w
                                    * (float(synth_font.text_length(_mid[:_k], sem)) / _at)))
            for _j in range(1, s + 1):
                _q = len(ot) - s + _j
                _comp.append(_D(_oe[_q] - so0 + xs_new) if _q < len(_oe)
                             else (_comp[-1] if _comp else _D(xs_new)))
            ctx["_live_edges"] = (nt, _comp)
        except Exception:
            ctx["_live_edges"] = None
        return tile, disp_for(Wg, tile.shape[0])

    def _restore_borders_in_tile(self, tile, region, ctx, Hr, Wr):
        """Copy any detected HORIZONTAL table-rule pixels back from the ORIGINAL scan
        ``region`` into the composed ``tile``, so the in-place edit's erase/grain never
        breaks or degrades a table line above/below the text. The page's display-space
        border lines map into this line's crop via the ctx ``disp_rect`` (move-adjusted
        display origin) + ``ppi``. No detected rule in this line -> no-op.

        ponytail: horizontal-only and restores just the original [:Wr] span. A VERTICAL
        rule that crosses an edited glyph is the separate force_synth/_strip_borders case
        -- blanket-restoring its column here would revert the fresh synth glyph -- and a
        widening insert does not re-extend a rule into the grown columns. Add those if a
        real case needs them; this fixes the reported horizontal-rule-above-the-line wipe."""
        try:
            h, _v = self._cached_border_lines(int(ctx["page_index"]))
        except Exception:
            return
        if not h:
            return
        ppi = float(ctx["ppi"])
        dy0 = float(ctx["disp_rect"][1])
        rH, rW = region.shape[:2]
        w = min(Wr, rW)
        import numpy as np

        def _inkfrac(a, b):                                    # ink coverage of region rows [a:b)
            a = max(0, a); b = min(rH, b)
            if b <= a or w <= 0:
                return 0.0
            return float(((255.0 - region[a:b, :w].mean(2)) / 255.0 > 0.35).mean())

        for (bx0, by0, bx1, by1) in h:                         # horizontal rule -> row band
            r0 = max(0, int(round((min(by0, by1) - dy0) * ppi)) - 1)
            r1 = min(Hr, rH, int(round((max(by0, by1) - dy0) * ppi)) + 2)
            if not (r1 > r0 and w > 0):
                continue
            # A genuine rule is an ISOLATED thin line -- paper on at least one side (a table grid
            # line, or an underline with text only above it). Heavy dense scanned TEXT (a near-black
            # VIN) trips the border detector as one long horizontal run; restoring that band copies
            # the ORIGINAL un-shifted glyphs back over a delete/insert edit and the whole line
            # smears (the "backspace breaks the rest of the line" bug). Ink on BOTH sides means the
            # band sits inside a text mass, not a rule -> skip it.
            if min(_inkfrac(r0 - 4, r0), _inkfrac(r1, r1 + 4)) > 0.25:
                continue
            tile[r0:r1, :w] = region[r0:r1, :w]
            # WIDENING insert: the line grew past the original scan width, so the rule would
            # stop short under the inserted text (it overflowed past the cell border). Extend
            # the rule into the grown columns by repeating its OWN rightmost pixels -- if there
            # is no rule at that right edge (region slice is paper) this tiles paper, a no-op.
            tw = tile.shape[1]
            if tw > w:
                _rep = region[r0:r1, max(0, w - 6):w]
                if _rep.shape[1] > 0:
                    _reps = int(np.ceil((tw - w) / _rep.shape[1]))
                    tile[r0:r1, w:] = np.tile(_rep, (1, _reps, 1))[:, :tw - w]

    def _scanned_inplace_raster(self, box: "NewBox", new_text: str,
                                orig_text: str = "", runs: tuple | None = None,
                                force_synth=None):
        """Bake form of the in-place edit: reflow the line (scan pixels for the
        untouched runs, synth for the added/restyled ones) and return ``(png,
        rect)``. ``runs`` carries per-selection bold/italic so a bolded word bakes
        in its real variant FILE. ``force_synth=(i0,i1)`` re-synthesizes that index
        range from the matched font even when its text is UNCHANGED (a moved glyph a
        table rule crossed)."""
        ctx = self.scan_edit_context(box, orig_text or box.text)
        if ctx is None:
            dlog("RENDER", "inplace_raster_none", reason="no_ctx")
            return None
        # Unchanged text with NO restyle -> nothing to bake (the scan shows). A
        # style-only change (a run differs from the base in weight/slant OR carries
        # a RunStyle colour/family) still needs a bake. Runs may be 3- or 4-tuples.
        base = (bool(ctx.get("want_bold")), bool(ctx.get("want_italic")))
        styled = bool(runs) and any(
            (bool(r[1]), bool(r[2])) != base or (len(r) > 3 and r[3] is not None)
            for r in runs)
        if (new_text or "").strip() == ctx["orig_text"] and not styled \
                and force_synth is None:
            dlog("RENDER", "inplace_raster_none", reason="unchanged_text")
            return None
        tile, disp = self.inplace_compose(ctx, new_text, runs,
                                          force_synth=force_synth)
        if tile is None:
            dlog("RENDER", "inplace_raster_none", reason="tile_none")
            return None
        try:
            import cv2
            ok, buf = cv2.imencode(".png", cv2.cvtColor(tile, cv2.COLOR_RGB2BGR))
            if not ok:
                return None
            # The tile is in DISPLAY space and may have GROWN past the cover; map its
            # display rect back to TEXT space (the bake places it with rotate=page
            # rotation), so a longer line lands correctly on a /Rotate page too.
            derot = self.working[box.page_index].derotation_matrix
            pts = [fitz.Point(disp[0], disp[1]) * derot, fitz.Point(disp[2], disp[1]) * derot,
                   fitz.Point(disp[2], disp[3]) * derot, fitz.Point(disp[0], disp[3]) * derot]
            rect = (min(p.x for p in pts), min(p.y for p in pts),
                    max(p.x for p in pts), max(p.y for p in pts))
            dlog("RENDER", "inplace_raster_ok",
                 tw=int(tile.shape[1]), th=int(tile.shape[0]),
                 disp=tuple(round(float(v), 1) for v in disp),
                 rect=tuple(round(float(v), 1) for v in rect))
            return bytes(buf), rect
        except Exception:
            dlog("RENDER", "inplace_raster_none", reason="encode_or_map_exc")
            return None

    def compose_lines_block(self, box: "NewBox", new_text: str, page_rgb=None,
                            ctxs=None, runs=None):
        """Compose a PARAGRAPH edit as N single lines on the scan, returning
        ``(canvas_rgb, disp_rect)`` (DISPLAY points) -- the multi-line analog of
        ``inplace_compose``. Each line runs the SAME single-line engine
        (``scan_edit_context`` + ``inplace_compose``); untouched lines stay the real
        scan crop (inter-line texture and all), changed lines keep their own
        prefix/suffix scan pixels and synthesize only the diff, pasted at their own
        scan position (indentation + baseline preserved), and the block GROWS right
        for overflow. ``page_rgb`` lets the live editor pass a once-rendered page so a
        keystroke does not re-render. Returns None if it cannot run (caller falls
        back). The page is rendered ONCE for all lines."""
        import numpy as np
        line_covers = tuple(getattr(box, "line_covers", ()) or ())
        cover = box.cover
        if len(line_covers) < 2 or not (cover and len(cover) == 7):
            return None
        orig_lines = (box.ocr_text or box.text or "").split("\n")
        new_lines = (new_text or "").split("\n")
        # The editor preserves line membership (one \n per recognized line). If the
        # counts ever drift, pairing lines to covers would be wrong -> bail (the
        # caller falls back to the old whole-block raster) rather than mis-place text.
        if len(new_lines) != len(line_covers) or len(orig_lines) != len(line_covers):
            return None
        try:
            ppi = 300.0 / 72.0
            page = self.working[box.page_index]
            if page_rgb is None:
                page_rgb = self.render_page_image(box.page_index, 300.0)
            H, W = page_rgb.shape[:2]
            rot = page.rotation_matrix
            # Block crop = the whole cover in DISPLAY pixels (the real scan, gaps and
            # all -- the base every untouched line shows through).
            x0, y0, x1, y1 = (float(c) for c in cover[:4])
            cpts = [fitz.Point(px, py) * rot for px, py in
                    ((x0, y0), (x1, y0), (x1, y1), (x0, y1))]
            bx0 = max(0, int(round(min(p.x for p in cpts) * ppi)))
            by0 = max(0, int(round(min(p.y for p in cpts) * ppi)))
            bx1 = min(W, int(round(max(p.x for p in cpts) * ppi)))
            by1 = min(H, int(round(max(p.y for p in cpts) * ppi)))
            if bx1 - bx0 < 6 or by1 - by0 < 4:
                return None
            # Per-LINE rich runs (bold/italic/colour of each line), sliced from the
            # whole box runs so a paragraph styles per line just like a single line.
            base = (bool(getattr(box, "bold", False)),
                    bool(getattr(box, "italic", False)), None)
            per_line = self._slice_runs_per_line(runs, new_lines, base) if runs else None
            # TOP edge (page px) of every line cover. A paragraph line's cover can extend DOWN
            # into the next line's top (the OCR covers overlap); the changed line's tile then
            # carries erased/degraded PAPER in its lower rows, which -- pasted over the canvas --
            # would clip the top of the line below (Edward: "12/31 is cutting the line below").
            # Clamp each tile's paste bottom to the next line's cover top so only the real scan
            # of the line below ever shows there. The changed line's own text sits in the tile
            # TOP, well above this, so it is never cut.
            line_top_px = []
            for _lc in line_covers:
                _lx0, _ly0, _lx1, _ly1 = (float(c) for c in _lc[:4])
                _lp = [fitz.Point(px, py) * rot for px, py in
                       ((_lx0, _ly0), (_lx1, _ly0), (_lx1, _ly1), (_lx0, _ly1))]
                line_top_px.append(min(p.y for p in _lp) * ppi)
            # Compose only the CHANGED lines; unchanged lines stay the base scan crop.
            composed = []                              # (px_left, px_top, tile, line_index)
            max_right_px = bx1
            for i, lc in enumerate(line_covers):
                o = (orig_lines[i] or "").strip()
                n = (new_lines[i] or "").strip()
                lruns = per_line[i] if per_line else None
                # Compose when the TEXT changed OR this line carries per-run styling
                # (a bolded word on otherwise-unchanged text still needs a re-bake).
                if n == o and not lruns:
                    continue
                # Reuse the cached per-line context (the live editor passes them) so a
                # keystroke does NOT re-segment the line or re-match its font -- the scan
                # is unchanged; only the typed text differs. Falls back to computing it
                # (the one-time bake path passes no cache).
                ctx = (ctxs[i] if ctxs and i < len(ctxs) and ctxs[i] is not None
                       else self.scan_edit_context(box, cover_override=lc,
                                                   text_override=o, page_rgb=page_rgb))
                if ctx is None:
                    # A CHANGED line whose scan_edit_context failed cannot be
                    # composed, but the canvas is seeded from the real scan crop,
                    # so skipping it would silently keep the ORIGINAL text (lost
                    # edit). Bail the whole block to the _scanned_paragraph_raster
                    # fallback. Only an unchanged-but-styled line is safe to skip.
                    if n != o:
                        return None
                    continue
                tile, disp = self.inplace_compose(ctx, n, lruns)
                if tile is None:
                    continue
                pl = int(round(disp[0] * ppi))         # line tile left/top in page px
                pt = int(round(disp[1] * ppi))
                composed.append((pl, pt, tile, i))
                max_right_px = max(max_right_px, pl + tile.shape[1])
            crop = np.ascontiguousarray(page_rgb[by0:by1, bx0:bx1])
            Hc = crop.shape[0]
            Wc = max(crop.shape[1], max_right_px - bx0)
            pr, pgc, pb = (float(c) for c in cover[4:7])
            paper_u8 = np.clip(np.array([pr * 255, pgc * 255, pb * 255], np.float32),
                               0, 255).astype(np.uint8)
            canvas = np.empty((Hc, Wc, 3), np.uint8)
            canvas[:] = paper_u8                       # right-extension fill (if grown)
            canvas[:, :crop.shape[1]] = crop           # real scan incl. inter-line gaps
            for pl, pt, tile, li in composed:          # paste each changed line in place
                ox, oy = pl - bx0, pt - by0
                th, tw = tile.shape[:2]
                sx0, sy0 = max(0, ox), max(0, oy)
                sx1, sy1 = min(Wc, ox + tw), min(Hc, oy + th)
                # never paste DOWN over the line below: clamp to the next line's cover top so
                # this tile's overhanging paper rows don't clip it (the line's own text is higher).
                if li + 1 < len(line_top_px):
                    sy1 = min(sy1, int(round(line_top_px[li + 1])) - by0)
                if sx1 <= sx0 or sy1 <= sy0:
                    continue
                canvas[sy0:sy1, sx0:sx1] = tile[sy0 - oy:sy1 - oy, sx0 - ox:sx1 - ox]
            disp_rect = (bx0 / ppi, by0 / ppi, (bx0 + Wc) / ppi, by1 / ppi)
            return canvas, disp_rect
        except Exception:
            return None

    def _scanned_lines_raster(self, box: "NewBox", new_text: str,
                              runs: tuple | None = None):
        """UNIFIED scanned-OCR bake raster. A single-line box (no per-line covers) is
        the N==1 case and delegates to ``_scanned_inplace_raster`` (byte-identical, no
        regression); a paragraph composes via ``compose_lines_block`` and PNG-encodes
        it, mapping the (possibly grown) display rect back to TEXT space for the bake.
        Returns ``(png_bytes, text_rect)`` or None. ``runs`` carries per-selection
        bold/italic (single-line path; paragraph per-run is not wired yet)."""
        line_covers = tuple(getattr(box, "line_covers", ()) or ())
        if len(line_covers) < 2:
            # A paragraph WITHOUT per-line covers (a stale box from before this field
            # existed) returns None so the caller falls back to the whole-block raster;
            # a true single line takes the unchanged in-place path.
            if getattr(box, "is_paragraph", False):
                return None
            return self._scanned_inplace_raster(box, new_text,
                                                box.ocr_text or box.text, runs=runs)
        out = self.compose_lines_block(box, new_text, runs=runs)
        if out is None:
            return None
        canvas, disp = out
        try:
            import cv2
            ok, buf = cv2.imencode(".png", cv2.cvtColor(canvas, cv2.COLOR_RGB2BGR))
            if not ok:
                return None
            derot = self.working[box.page_index].derotation_matrix
            dpts = [fitz.Point(disp[0], disp[1]) * derot, fitz.Point(disp[2], disp[1]) * derot,
                    fitz.Point(disp[2], disp[3]) * derot, fitz.Point(disp[0], disp[3]) * derot]
            rect = (min(p.x for p in dpts), min(p.y for p in dpts),
                    max(p.x for p in dpts), max(p.y for p in dpts))
            return bytes(buf), rect
        except Exception:
            return None

    def paragraph_fit_factor(self, box: "NewBox", text: "str | None" = None) -> float:
        """How much to scale a paragraph OCR box's size + leading DOWN so its lines
        fit the cover (1.0 = fits as-is; never > 1). SHARED by the inline editor and
        the bake so the two lay the block out identically -- without this the editor
        renders at the raw (often over-estimated) size while the bake fits, and they
        disagree. Uses the recognized line breaks; no re-wrapping."""
        cover = box.cover
        if not (cover and len(cover) == 7):
            return 1.0
        fpath = self._edit_font_file(box.font_family)
        if not fpath:
            return 1.0
        try:
            f = fitz.Font(fontfile=fpath)
            em = max(8.0, float(box.size))
            lead = max(float(box.leading or 0.0), em * 1.2)
            x0, y0, x1, y1 = (float(c) for c in cover[:4])
            # The cover's DISPLAY dimensions, not text-space: on a /Rotate page the
            # text-space cover is tall+narrow (lines run vertically), so fitting the
            # block to it shrank the editor to a sliver. Map the cover through the page
            # rotation so area_w is the column's on-screen width and area_h its height.
            rotm = self.working[box.page_index].rotation_matrix
            _pts = [fitz.Point(px, py) * rotm for px, py in
                    ((x0, y0), (x1, y0), (x1, y1), (x0, y1))]
            area_w = max(p.x for p in _pts) - min(p.x for p in _pts)
            area_h = max(p.y for p in _pts) - min(p.y for p in _pts)
            src = text if text is not None else (box.text or "")
            lines = [t.strip() for t in src.split("\n") if t.strip()]
            n = max(1, len(lines))
            widest = max((f.text_length(t, em) for t in lines), default=1.0)
            return float(min(1.0, (area_w - 4.0) / max(widest, 1.0),
                             (area_h - 2.0) / max(n * lead, 1.0)))
        except Exception:
            return 1.0

    def _scanned_paragraph_raster(self, box: "NewBox", new_text: str):
        """For an edited PARAGRAPH (a multi-line OCR area), render the reflowed
        text onto a paper-coloured tile the size of the area and return
        ``(png_bytes, rect)``. The bake places it with ``insert_image(rotate=
        page.rotation)``, so -- unlike the single-word degrade raster, which is
        axis-aligned and bails on /Rotate -- a paragraph edit lands correctly on
        rotated scans too. The paper colour comes from the cover (sampled at OCR
        time), so the tile replaces the scanned block and blends with the page."""
        import numpy as np
        cover = box.cover
        if not (cover and len(cover) == 7) or not new_text.strip():
            return None
        try:
            import cv2
            from .ocr import degrade
            from .reflow import wrap_paragraph
            x0, y0, x1, y1 = (float(c) for c in cover[:4])
            # The cover's ON-SCREEN (display) dimensions, NOT text-space. On a /Rotate page
            # the text-space cover is tall+narrow (the column runs vertically in text space),
            # so building the tile in text-space dims drew HORIZONTAL text into a narrow tile
            # that the bake's rotate=page.rotation then turned VERTICAL on the page. Build the
            # tile upright in the column's display orientation, exactly like compose_lines_block.
            page = self.working[box.page_index]
            cpts = [fitz.Point(px, py) * page.rotation_matrix for px, py in
                    ((x0, y0), (x1, y0), (x1, y1), (x0, y1))]
            ddx0, ddy0 = min(p.x for p in cpts), min(p.y for p in cpts)
            area_w = max(p.x for p in cpts) - ddx0
            area_h = max(p.y for p in cpts) - ddy0
            if area_w < 4 or area_h < 4:
                return None
            fpath = self._edit_font_file(box.font_family)
            if fpath is None:
                return None
            f = fitz.Font(fontfile=fpath)
            lines_txt = [t.strip() for t in new_text.split("\n")]
            # Render EXACTLY the recognized lines -- one per baseline, NO re-wrapping
            # -- so the bake matches the inline editor line-for-line (the bake's wrap
            # engine disagreed with Qt's and re-wrapped, overflowing the cover). Size
            # + leading scaled by the SHARED fit factor so a too-big estimate fits
            # the cover instead of overflowing, identically to the editor.
            fit = self.paragraph_fit_factor(box, new_text)
            em = max(8.0, float(box.size)) * fit
            lead = max(box.leading or 0.0, max(8.0, float(box.size)) * 1.2) * fit
            doc = fitz.open()
            pg_ = doc.new_page(width=area_w, height=area_h)
            pg_.draw_rect(pg_.rect, color=(1, 1, 1), fill=(1, 1, 1), width=0)
            tw = fitz.TextWriter(pg_.rect)
            drew = False
            y = em
            for lt in lines_txt:
                if lt and y <= area_h + em:
                    tw.append((2.0, y), lt, font=f, fontsize=em)
                    drew = True
                y += lead
            if drew:
                tw.write_text(pg_, color=(0.06, 0.06, 0.06))
            ppi = 300.0 / 72.0
            pm = pg_.get_pixmap(matrix=fitz.Matrix(ppi, ppi), alpha=False)
            clean = np.frombuffer(pm.samples, np.uint8).reshape(
                pm.height, pm.width, pm.n)[..., :3].copy()
            # 2) Recover the scan's ink/paper + local damage and RECOLOR + DEGRADE the
            #    whole tile, so a paragraph edit blends exactly like a single word
            #    (this is the colour/damage that the old flat-grey tile skipped).
            samp = self._sample_scan_region(box)
            if samp is not None:
                ink, paper, sev, _ = samp
            else:
                pr, pg_c, pb = (float(c) for c in cover[4:7])
                ink = np.array([24.0, 24.0, 24.0], np.float32)
                paper = np.array([pr * 255, pg_c * 255, pb * 255], np.float32)
                sev = 0.2
            out = degrade.degrade_patch(clean, ink, paper, sev,
                                        seed=box.box_id * 131 + len(new_text))
            ok, buf = cv2.imencode(".png", cv2.cvtColor(out, cv2.COLOR_RGB2BGR))
            if not ok:
                return None
            # Place the upright tile by DEROTATING its display box back to text space (the
            # same mapping _scanned_lines_raster uses), so the bake's rotate=page.rotation
            # lands it upright, never vertical.
            derot = page.derotation_matrix
            dpts = [fitz.Point(px, py) * derot for px, py in
                    ((ddx0, ddy0), (ddx0 + area_w, ddy0),
                     (ddx0 + area_w, ddy0 + area_h), (ddx0, ddy0 + area_h))]
            rect = (min(p.x for p in dpts), min(p.y for p in dpts),
                    max(p.x for p in dpts), max(p.y for p in dpts))
            return bytes(buf), rect
        except Exception:
            return None

    def ocr_text_placement(self, page_index: int,
                           display_origin_pt: tuple) -> tuple:
        """Map an OCR baseline ``display_origin_pt`` (points in the RENDERED /
        display-space image, what ``render_page_image`` produced) to the
        ``(text_origin, direction)`` ``add_box`` needs. On a ``/Rotate`` page,
        display space != text space, so OCR text placed naively bakes sideways;
        this applies the page's derotation so the baked text displays upright."""
        page = self.working[page_index]
        derot = page.derotation_matrix
        pt = fitz.Point(display_origin_pt) * derot
        # The text-space writing direction of horizontal (L->R) display text is
        # (1,0) carried through the derotation's linear part.
        dx, dy = derot.a, derot.b
        n = math.hypot(dx, dy) or 1.0
        return (pt.x, pt.y), (dx / n, dy / n)

    def ocr_cover_rect(self, page_index: int, display_rect: tuple) -> tuple:
        """Map a DISPLAY-space rect (x0,y0,x1,y1 points) to the TEXT-space AABB
        ``draw_rect`` needs, so an OCR cover lands on the scanned text even on a
        ``/Rotate`` page (axis-aligned for 0/90/180/270, the common cases)."""
        derot = self.working[page_index].derotation_matrix
        x0, y0, x1, y1 = display_rect
        pts = [fitz.Point(x0, y0) * derot, fitz.Point(x1, y0) * derot,
               fitz.Point(x1, y1) * derot, fitz.Point(x0, y1) * derot]
        xs = [p.x for p in pts]
        ys = [p.y for p in pts]
        return (min(xs), min(ys), max(xs), max(ys))

    def page_has_text_layer(self, page_index: int) -> bool:
        """True when the page already has extractable text (so OCR is not
        needed). Cheap: a non-empty ``get_text('text')`` (OCR_SPEC §3.1)."""
        try:
            return bool(self.working[page_index].get_text("text").strip())
        except Exception:
            return True

    def image_only_pages(self) -> list[int]:
        """Indices of pages with NO real text layer that DO carry image ink --
        the scanned pages OCR should target (OCR_SPEC §3.1). A page with neither
        text nor images is blank, not image-only, and is skipped."""
        out: list[int] = []
        for i in range(self.page_count):
            if self.page_has_text_layer(i):
                continue
            try:
                has_img = bool(self.working[i].get_images(full=False))
            except Exception:
                has_img = False
            if has_img:
                out.append(i)
        return out

    def scanned_pages(self) -> list[int]:
        """Indices of pages that ARE a scan -- a raster image covering at least
        half the page -- REGARDLESS of whether they also carry a text layer. This
        is the set a fresh re-OCR can rebuild: it includes pages whose OCR text
        was already baked into the PDF (saved then reopened), which
        ``image_only_pages`` excludes because they now have extractable text. The
        half-page coverage test keeps genuine digital pages that merely embed a
        small logo out, so re-OCR never strips real text."""
        out: list[int] = []
        for i in range(self.page_count):
            page = self.working[i]
            parea = abs(page.rect.width * page.rect.height)
            if parea <= 0:
                continue
            covered = 0.0
            try:
                for img in page.get_images(full=True):
                    for r in page.get_image_rects(img[0]):
                        covered = max(covered, abs(r.width * r.height) / parea)
                        if covered >= 0.5:
                            break
                    if covered >= 0.5:
                        break
            except Exception:
                covered = 0.0
            if covered >= 0.5:
                out.append(i)
        return out

    def strip_text_layer(self, page_indices) -> None:
        """Remove the text layer from the given pages while KEEPING the scanned
        image (and any vector art) -- a full-page redaction with images and line
        art protected, so only text is deleted. Turns a baked-OCR scan back into
        an image-only page that can be re-OCR'd from a clean slate. Structural:
        mutates ``working`` and is undoable (call via the window's
        ``_run_structural`` funnel)."""
        self._begin_structural()
        for i in page_indices:
            if not (0 <= i < self.page_count):
                continue
            page = self.working[i]
            page.add_redact_annot(page.rect)
            page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE,
                                  graphics=fitz.PDF_REDACT_LINE_ART_NONE)
        self._finish_structural()

    def restore_ocr_layer(self) -> int:
        """If this document was saved with an embedded OCR layer, rebuild it so
        the page edits EXACTLY like a fresh OCR. Re-registers the scan fonts,
        repopulates ``_new_boxes`` + ``_pfm_cache``, then strips the now-redundant
        baked OCR text from the working pages (the restored boxes are the editable
        source of truth) -- keeping the scan image + any edited rasters -- so the
        baked text never creates a conflicting generic-text hotspot and never
        stacks a second copy on the next save. Marks the document clean: a
        restored layer matches what's on disk, so it must NOT read as unsaved.
        Returns the number of OCR boxes restored (0 when there's no layer)."""
        from .ocr import layer_io
        n = layer_io.restore_layer(self)
        if not n:
            return 0
        pages = sorted({b.page_index for b in self.new_boxes_all()
                        if (getattr(b, "ocr_text", "") or "")
                        or getattr(b, "render_mode", 0) == 3})
        for pi in pages:
            try:
                pg = self.working[pi]
                if pg.get_text("text").strip():
                    pg.add_redact_annot(pg.rect)
                    pg.apply_redactions(
                        images=fitz.PDF_REDACT_IMAGE_NONE,
                        graphics=fitz.PDF_REDACT_LINE_ART_NONE)
            except Exception:
                continue
        self._invalidate_caches()
        self.mark_clean()
        return n

    def page_rotation(self, page_index: int) -> int:
        """The page's /Rotate value (0/90/180/270)."""
        return self.working[page_index].rotation

    def rotation_matrix(self, page_index: int) -> "fitz.Matrix":
        """Maps rawdict (derotated text-space) coordinates into the DISPLAY
        space the rendered pixmap uses. Identity for an unrotated page.

        ``get_pixmap`` renders in rotated display space, but ``get_text`` returns
        derotated text-space coordinates, so the UI must apply this matrix to
        every bbox/origin before scaling into scene units, or hotspots/covers/
        previews land in the wrong place on a rotated page.
        """
        return self.working[page_index].rotation_matrix

    # --- extraction ------------------------------------------------------
    def spans(self, page_index: int) -> list[Span]:
        """rawdict -> list[Span], skipping empty/whitespace spans and non-text
        blocks. Each Span's ``font_xref`` is resolved via the FontEngine so the
        embedded original font can be located later without re-matching.

        Memoized per page for the current working-doc generation (a bake /
        structural op bumps the generation via ``_invalidate_caches``): the
        extraction + overlap-merge + paragraph-grouping depend only on
        ``self.working``, so a cache hit returns the same frozen-dataclass boxes
        without re-running the scan (minor perf finding). The returned list is the
        cached object; callers treat it read-only (they build their own lists)."""
        cached = self._spans_cache.get(page_index)
        if cached is not None:
            return cached
        result = self._extract_spans(page_index)
        self._spans_cache[page_index] = result
        return result

    def _extract_spans(self, page_index: int) -> list[Span]:
        """The uncached extraction body for ``spans`` (see its docstring)."""
        page = self.working[page_index]
        raw = page.get_text("rawdict")
        out: list[Span] = []
        for bi, block in enumerate(raw["blocks"]):
            if block.get("type", 0) != 0:  # 0 = text, 1 = image
                continue
            for li, line in enumerate(block["lines"]):
                line_dir = tuple(line.get("dir", (1.0, 0.0)))
                for si, span in enumerate(line["spans"]):
                    text = "".join(c["c"] for c in span.get("chars", []))
                    if not text.strip():
                        continue
                    font_xref = self.font_engine.embedded_xref(
                        page_index, span["font"], span["flags"]
                    )
                    out.append(
                        Span(
                            text=text,
                            bbox=tuple(span["bbox"]),
                            origin=tuple(span["origin"]),
                            size=span["size"],
                            color=tuple(
                                c / 255 for c in fitz.sRGB_to_rgb(span["color"])
                            ),
                            font=span["font"],
                            flags=span["flags"],
                            font_xref=font_xref,
                            ascender=span.get("ascender", 0.0),
                            descender=span.get("descender", 0.0),
                            block_index=bi,
                            line_index=li,
                            span_index=si,
                            dir=line_dir,
                            page_index=page_index,
                        )
                    )
        # ALIGNMENT RECOGNITION: mark GENUINE centered single lines (a title/subtitle
        # alone on its baseline row, near the page's horizontal centre) so
        # ``_maybe_recenter`` re-centers only those. A line that SHARES its baseline
        # with another span is a table/form row -> its cell stays left-anchored, so a
        # resize or a different-size edit leaves the first letter where it was.
        pw = float((page.rect * page.derotation_matrix).width)

        def _is_centered(sp: "Span") -> bool:
            if not sp.is_horizontal or pw <= 0:
                return False
            cx = (sp.bbox[0] + sp.bbox[2]) / 2.0
            if abs(cx - pw / 2.0) > pw * 0.06:
                return False
            return not any(o is not sp and abs(o.origin[1] - sp.origin[1]) <= 2.0
                           for o in out)
        out = [replace(sp, centered_line=_is_centered(sp)) for sp in out]
        merged = self._merge_overlapping(out)
        boxes = self._group_paragraphs(merged)
        return self._apply_manual_grouping(page_index, boxes, merged)

    @staticmethod
    def _merge_overlapping(spans: list["Span"]) -> list["Span"]:
        """Collapse spans whose boxes substantially OVERLAP into one editable
        box. Some PDFs draw a value as several overlapping runs (a full span
        plus partial duplicates), which would otherwise become stacked hotspots
        where editing one leaves the others behind. Adjacent, non-overlapping
        runs (a label next to its value, a date next to a field) never merge --
        only a real area overlap does -- so distinct fields stay separate.

        The merged box keeps the PRIMARY run's text/font/baseline (the longest,
        most complete run) and records every member's bbox in ``redact_bboxes``
        so editing it erases all the duplicates at once.

        Spans only merge when their WRITING DIRECTIONS match: the duplicates
        this pass exists for are re-stamped at hair offsets, so they always
        share the original run's direction. A ROTATED run's axis-aligned bbox
        is mostly empty space -- a page-diagonal text watermark's bbox
        (doc-tools M2) blankets every horizontal line on the page, and
        without the direction gate the whole page would collapse into ONE
        box, where editing one line silently redacts everything else.
        Cross-direction ink overlap is handled at bake time by the
        neighbor-rescue redraw instead.
        """
        n = len(spans)
        if n <= 1:
            return [replace(s, redact_bboxes=(s.bbox,)) for s in spans]

        rects = [fitz.Rect(s.bbox) for s in spans]
        areas = [max(r.get_area(), 1e-6) for r in rects]
        parent = list(range(n))

        def find(i: int) -> int:
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i

        # Sweep by vertical position: sorted by top edge, only compare spans
        # whose y-ranges can overlap, so a dense text page stays near-linear
        # instead of O(n^2). Overlapping duplicates share nearly the same y.
        order = sorted(range(n), key=lambda i: rects[i].y0)
        for a in range(n):
            i = order[a]
            ri = rects[i]
            for b in range(a + 1, n):
                j = order[b]
                if rects[j].y0 >= ri.y1:
                    break  # later spans start even lower; none can overlap ri
                inter = ri & rects[j]
                if inter.is_empty:
                    continue
                # Direction gate (see docstring): only same-direction runs
                # can be overlapping duplicates of each other.
                di, dj = spans[i].dir, spans[j].dir
                if abs(di[0] - dj[0]) > 1e-3 or abs(di[1] - dj[1]) > 1e-3:
                    continue
                # Merge only on a real overlap of the smaller box, not a hairline
                # touch between abutting runs.
                if inter.get_area() > 0.30 * min(areas[i], areas[j]):
                    parent[find(i)] = find(j)

        groups: dict[int, list[int]] = {}
        for i in range(n):
            groups.setdefault(find(i), []).append(i)

        merged: list["Span"] = []
        for idxs in groups.values():
            members = [spans[i] for i in idxs]
            if len(members) == 1:
                merged.append(replace(members[0], redact_bboxes=(members[0].bbox,)))
                continue
            # Primary = longest stripped text, tie-broken by box area.
            primary = max(
                members,
                key=lambda s: (len(s.text.strip()), fitz.Rect(s.bbox).get_area()),
            )
            union = fitz.Rect(primary.bbox)
            for m in members:
                union |= fitz.Rect(m.bbox)
            merged.append(replace(
                primary,
                bbox=tuple(union),
                redact_bboxes=tuple(tuple(m.bbox) for m in members),
            ))
        return merged

    # --- paragraph grouping (REFLOW_SPEC §R1) ----------------------------
    def _group_paragraphs(self, spans: list["Span"]
                          ) -> list["Span | ParagraphBox"]:
        """Fuse runs of consecutive same-paragraph LINES into one
        ``ParagraphBox`` (REFLOW_SPEC §R1.3), composing with ``_merge_overlapping``
        (it runs on that pass's output, invariant §R0.3). Every span that is NOT
        part of a >=2-line paragraph passes through UNCHANGED, so single-line
        fields / headings / table cells stay ``Span``s and existing behavior is
        byte-for-byte preserved.

        The pass is deterministic: HORIZONTAL spans are sorted into reading order
        (y0, x0) and walked; a line joins the current group iff it agrees with the
        primary line on size, font family + style bits, color, AND with the
        group's running leading + alignment-compatible left/right/center edge. A
        leading jump (a paragraph break) or any style/geometry change closes the
        group. A list item is kept separate because its line carries a marker
        glyph to its left (a short, differently-styled span at the line start),
        which lands as a non-candidate / style-mismatch sibling on the same row.
        """
        candidates = [s for s in spans if s.is_horizontal]
        passthrough = [s for s in spans if not s.is_horizontal]
        if len(candidates) < 2:
            return list(spans)

        order = sorted(candidates, key=lambda s: (round(s.bbox[1], 2), s.bbox[0]))
        # A line that shares its baseline row with ANOTHER candidate (a label and
        # its value, a bullet glyph and its text) is part of a multi-column row,
        # not a free-flowing paragraph line: grouping it would merge across an
        # unrelated column. Mark these so they never seed/extend a paragraph;
        # they pass through as their own Spans.
        row_shared: set[int] = set()
        for a in range(len(order)):
            ya = order[a].bbox[1]
            ha = order[a].bbox[3] - order[a].bbox[1]
            for b in range(a + 1, len(order)):
                if order[b].bbox[1] - ya > 0.5 * max(ha, 1.0):
                    break
                # Same baseline row (vertical overlap), side by side horizontally.
                if abs(order[b].origin[1] - order[a].origin[1]) <= 0.5 * max(ha, 1.0):
                    row_shared.add(id(order[a]))
                    row_shared.add(id(order[b]))

        groups: list[list["Span"]] = []
        current: list["Span"] = []
        body_lead: float | None = None

        def close() -> None:
            nonlocal current, body_lead
            if current:
                groups.append(current)
            current = []
            body_lead = None

        for s in order:
            if id(s) in row_shared:
                # A multi-column row line is never grouped: close any open group
                # and emit this line as its own singleton group.
                close()
                groups.append([s])
                continue
            if not current:
                current = [s]
                body_lead = None
                continue
            prev = current[-1]
            primary = current[0]
            gap = s.bbox[1] - prev.bbox[1]
            if self._para_continues(primary, prev, s, current, gap, body_lead):
                if body_lead is None:
                    body_lead = gap
                current.append(s)
            else:
                close()
                current = [s]
                body_lead = None
        close()

        # Build the output in reading order: a >=2-line group becomes a
        # ParagraphBox; everything else passes through as its member Span(s).
        out: list["Span | ParagraphBox"] = []
        for grp in groups:
            if len(grp) >= 2:
                out.append(self._build_paragraph(grp))
            else:
                out.extend(grp)
        out.extend(passthrough)
        # Stable reading order across paragraphs + passthrough + singletons.
        out.sort(key=lambda b: (round(b.bbox[1], 2), b.bbox[0]))
        return out

    @staticmethod
    def _para_continues(primary: "Span", prev: "Span", s: "Span",
                        members: list["Span"], gap: float,
                        body_lead: float | None) -> bool:
        """The paragraph-continuity predicate (REFLOW_SPEC §R1.3 step 3): does
        line ``s`` belong to the same paragraph as ``primary`` (group head) given
        the previous line ``prev`` and the running ``body_lead``? ALL of size,
        font family+style, color, leading, and alignment-compatible edge must
        hold."""
        # (0) A line that BEGINS a new inline list item (a numbered / lettered /
        # bulleted prefix that is part of the line's own span, with no separate
        # marker glyph) breaks the group, so a markerless enumeration stays line-
        # per-line instead of fusing into one reflowing block (REFLOW_SPEC §R1.3).
        # Continuation lines of a single wrapped item carry no prefix and still
        # join.
        if _starts_list_item(s.text):
            return False
        # (a) SAME SIZE.
        if abs(s.size - primary.size) > _PARA_SIZE_TOL * max(primary.size, 1e-6):
            return False
        # (b) SAME FONT FAMILY + STYLE BITS.
        if FontEngine._family_norm(s.font) != FontEngine._family_norm(primary.font):
            return False
        if (s.flags & _STYLE_BITS) != (primary.flags & _STYLE_BITS):
            return False
        # (c) SAME COLOR.
        if max(abs(a - b) for a, b in zip(s.color, primary.color)) > _PARA_COLOR_TOL:
            return False
        # (d) CONSISTENT LEADING.
        if body_lead is None:
            lo = _PARA_FIRST_GAP_LO * primary.size
            hi = _PARA_FIRST_GAP_HI * primary.size
            if not (lo <= gap <= hi):
                return False
        else:
            if not (_PARA_LEAD_LO * body_lead <= gap <= _PARA_LEAD_HI * body_lead):
                return False
        # (e) ALIGNMENT-COMPATIBLE EDGE. The paragraph BODY reference edges come
        # from the body lines, NOT the head: a standard first-line / hanging
        # indent makes the HEAD's left edge differ from the body by a tab stop
        # (>> align_tol), so seeding lx/rx from the head poisons the reference and
        # the indented opening sentence is wrongly split off (REFLOW_SPEC §R1.3).
        # While the group is still just the head (len==1) the candidate IS the
        # first body line: anchor the body band on IT (s) and let the head be more
        # indented on the left, requiring only that the head not extend further
        # left than the body and that the candidate fit within the head's right.
        # Once >=2 members exist, take lx/rx from members[1:] (the body) so the
        # indented head never widens/narrows the band.
        align_tol = max(2.0, 0.5 * primary.size)
        # One tab stop of allowed first-line / hanging indent either way.
        indent_tol = 4.0 * primary.size
        if len(members) == 1:
            # Only the head is grouped so far; the candidate ``s`` is the first
            # BODY line. We cannot yet know the body left edge, so allow the head
            # and the candidate to differ on the LEFT by up to one tab stop (a
            # first-line indent: head left > body left; or a hanging indent: body
            # left > head left). The right/center edge tests stay exact against
            # the head -- exactly the original predicate, just with the left test
            # widened to the indent tolerance so an indented opening line groups.
            head = members[0]
            left_ok = abs(s.bbox[0] - head.bbox[0]) <= indent_tol
            right_ok = abs(s.bbox[2] - head.bbox[2]) <= align_tol
            gc = (head.bbox[0] + head.bbox[2]) / 2.0
            sc = (s.bbox[0] + s.bbox[2]) / 2.0
            center_ok = abs(sc - gc) <= align_tol
            if not (left_ok or right_ok or center_ok):
                return False
            return True
        # Body band from the body lines (members[1:]), excluding the indented head
        # so a first-line / hanging indent never widens or shifts the reference.
        body = members[1:]
        lx = min(m.bbox[0] for m in body)
        rx = max(m.bbox[2] for m in body)
        left_ok = abs(s.bbox[0] - lx) <= align_tol
        right_ok = abs(s.bbox[2] - rx) <= align_tol
        gc = (lx + rx) / 2.0
        sc = (s.bbox[0] + s.bbox[2]) / 2.0
        center_ok = abs(sc - gc) <= align_tol
        if not (left_ok or right_ok or center_ok):
            return False
        return True

    def _build_paragraph(self, members: list["Span"]) -> "ParagraphBox":
        """Construct a ``ParagraphBox`` from >=2 grouped member lines
        (REFLOW_SPEC §R1.2/§R1.5): join the member texts, union their bboxes,
        flatten their redaction rects, measure the leading, and infer alignment.
        ``members`` are in reading order (the grouping pass appends top-to-bottom).
        """
        primary = members[0]
        # Joined text: normalize NBSP / thin / figure space variants -> ASCII
        # space so the wrap engine can break on them and Find/Replace sees the
        # same whitespace as a single-line Span (REFLOW_SPEC §R5.1); one ASCII
        # space per soft (line) break, member text stripped of edge ws.
        joined = " ".join(
            _normalize_spaces(m.text).strip() for m in members
        ).strip()
        # Union bbox.
        x0 = min(m.bbox[0] for m in members)
        y0 = min(m.bbox[1] for m in members)
        x1 = max(m.bbox[2] for m in members)
        y1 = max(m.bbox[3] for m in members)
        # Measured leading: median LINE-to-LINE baseline gap. Cluster members by
        # baseline FIRST (a visual line can be several spans -- a bold word, an
        # inline field -- sharing one baseline); using every member's gap would
        # count ~0 same-line gaps and crush the leading to near-zero (which then
        # stacks the reflowed lines on top of each other).
        line_bases = sorted({round(m.origin[1], 0) for m in members})
        gaps = [line_bases[i] - line_bases[i - 1]
                for i in range(1, len(line_bases))]
        gaps.sort()
        leading = gaps[len(gaps) // 2] if gaps else 1.2 * primary.size
        alignment = self._infer_alignment(members)
        redact = tuple(bb for m in members for bb in m.redact_rects)
        return ParagraphBox(
            page_index=primary.page_index,
            bbox=(x0, y0, x1, y1),
            origin=primary.origin,
            members=tuple(members),
            text=joined,
            size=primary.size,
            color=primary.color,
            font=primary.font,
            flags=primary.flags,
            font_xref=primary.font_xref,
            ascender=primary.ascender,
            descender=primary.descender,
            dir=primary.dir,
            leading=leading,
            alignment=alignment,
            block_index=primary.block_index,
            line_index=primary.line_index,
            span_index=primary.span_index,
            redact_bboxes=redact,
        )

    @staticmethod
    def _infer_alignment(members: list["Span"]) -> str:
        """Infer the paragraph alignment from member-line geometry (REFLOW_SPEC
        §R1.5), ROBUST to one stray line so a centered paragraph whose continuation
        line drifted (or a manual group with a slightly-off last line) is still
        read as 'center' rather than collapsing to 'left'.

        Members are first clustered into VISUAL lines by baseline (a line can be
        several spans -- a bold word splits a line into runs); then alignment is
        decided by how many lines agree on an edge/center within tolerance of the
        MEDIAN, not by standard deviation (which one outlier inflates)."""
        import statistics
        by_base: dict[float, list] = {}
        for m in members:
            by_base.setdefault(round(m.origin[1], 0), []).append(m)
        lines = []
        for base in sorted(by_base):
            ms = by_base[base]
            lines.append((min(s.bbox[0] for s in ms),
                          max(s.bbox[2] for s in ms)))
        if not lines:
            return "left"
        lefts = [lx for lx, _ in lines]
        rights = [rx for _, rx in lines]
        centers = [(lx + rx) / 2.0 for lx, rx in lines]
        tol = max(2.0, 0.5 * members[0].size)

        def agree(vals: list[float], target: float) -> float:
            return sum(1 for v in vals if abs(v - target) <= tol) / len(vals)

        left_score = agree(lefts, statistics.median(lefts))
        right_score = agree(rights, statistics.median(rights))
        center_score = agree(centers, statistics.median(centers))
        thresh = 0.6                               # a clear majority of lines

        # Both edges flush across >=3 lines -> justify (last line may be short).
        if (left_score >= thresh and right_score >= thresh
                and len(lines) >= 3):
            return "justify"
        # Otherwise pick the strongest non-left signal; a flush LEFT edge always
        # reads as left even if the centers happen to line up.
        if center_score >= thresh and left_score < thresh and right_score < thresh:
            return "center"
        if right_score >= thresh and left_score < thresh:
            return "right"
        return "left"

    # =====================================================================
    # Manual box grouping (user override of automatic paragraph detection).
    # The detector errs toward keeping lines separate when their font, size,
    # color, or alignment diverge -- correct for headings/captions/table cells,
    # but wrong when a paragraph's continuation line drifted (e.g. its embedded
    # font got substituted by a prior edit). These let the USER force the call:
    # GROUP several boxes into one editable paragraph, or UNGROUP one back into
    # its lines. Keyed by stable line global_keys so the choice survives reloads.
    # =====================================================================

    @staticmethod
    def _box_member_keys(box) -> frozenset:
        """The stable line-level global_keys a box covers: a ParagraphBox's
        member lines, or a Span's own key. Used to identify a box across reloads
        and to assemble/split manual groups."""
        members = getattr(box, "members", None)
        if members:
            return frozenset(m.global_key for m in members)
        gk = getattr(box, "global_key", None)
        return frozenset((gk,)) if gk is not None else frozenset()

    def _invalidate_spans(self, page_index: int | None = None) -> None:
        """Drop the span/derived memos so the next spans() re-applies grouping.
        Lighter than ``_invalidate_caches`` (no FontEngine rebind): grouping
        changed which boxes exist, not the working doc."""
        self._spans_generation += 1
        self._spans_cache.clear()
        self._wrap_cache.clear()
        self._words_cache.clear()

    def manual_grouping_snapshot(self) -> tuple:
        """A deep, hashable-enough snapshot of the manual-group state for undo.
        Also captures the NEW-BOX table: a SCANNED box ungroup splits a multi-line
        OCR box into per-line NewBoxes (it edits ``_new_boxes`` directly), and the
        same GroupingCommand restores it. NewBox is frozen, so a shallow dict copy
        is a safe snapshot."""
        return (
            {p: list(v) for p, v in self._manual_groups.items()},
            {p: set(v) for p, v in self._manual_ungroups.items()},
            dict(self._new_boxes),
        )

    def restore_manual_grouping(self, snap: tuple) -> None:
        groups, ungroups = snap[0], snap[1]
        self._manual_groups = {p: list(v) for p, v in groups.items()}
        self._manual_ungroups = {p: set(v) for p, v in ungroups.items()}
        if len(snap) > 2:
            self._new_boxes = dict(snap[2])
            self._dirty = True
        self._invalidate_spans()

    def group_boxes(self, page_index: int, boxes: list) -> bool:
        """Force ``boxes`` (>=2 Spans/ParagraphBoxes on one page) into ONE
        editable ParagraphBox, honoring the user over the auto-detector even
        across font/size/alignment differences. Returns True when a group was
        recorded. Undo via manual_grouping_snapshot/restore."""
        keys: set = set()
        for b in boxes:
            keys |= self._box_member_keys(b)
        if len(keys) < 2:
            return False
        new_group = frozenset(keys)
        groups = self._manual_groups.setdefault(page_index, [])
        # Drop any existing manual group that overlaps (re-grouping supersedes).
        groups[:] = [g for g in groups if g.isdisjoint(new_group)]
        groups.append(new_group)
        # A prior ungroup of exactly these lines is now overridden.
        ung = self._manual_ungroups.get(page_index)
        if ung:
            ung.discard(new_group)
        self._invalidate_spans(page_index)
        return True

    def ungroup_box(self, page_index: int, box) -> bool:
        """Split ``box`` back into its individual lines. A SCANNED OCR box (a
        ``NewBox`` with per-line covers) splits into one single-line scan box per
        line so each field on a rule-less card edits on its own. A native-text
        ParagraphBox drops its manual group / records an ungroup override. Returns
        True on change."""
        # SCANNED box: the span-paragraph path below can't touch it (no member
        # lines), which is why ungroup silently did nothing on a scanned card.
        if isinstance(box, NewBox):
            cur = self._new_boxes.get(box.edit_key)
            lcs = tuple(getattr(cur, "line_covers", ()) or ()) if cur else ()
            if cur is not None and cur.is_paragraph and len(lcs) >= 2:
                return self._ungroup_scan_box(cur, lcs)
            return False
        if not getattr(box, "is_paragraph", False):
            return False
        keys = self._box_member_keys(box)
        if len(keys) < 2:
            return False
        groups = self._manual_groups.get(page_index)
        removed = False
        if groups:
            before = len(groups)
            groups[:] = [g for g in groups if not (g & keys)]
            removed = len(groups) < before
        if not removed:
            # An auto-paragraph: force it apart.
            self._manual_ungroups.setdefault(page_index, set()).add(keys)
        self._invalidate_spans(page_index)
        return True

    def _ungroup_scan_box(self, box: "NewBox", lcs: tuple) -> bool:
        """Replace a multi-line scanned OCR ``box`` with one SINGLE-LINE scan box
        per ``line_cover`` (top->bottom order matches ``ocr_text``'s lines), so each
        recognized field becomes its own editable box that still keeps its scan
        pixels in place. Each new box carries its OWN line cover + original line
        text and starts as an invisible overlay (render_mode 3 -> scan shows) until
        edited. Undoable via the manual-grouping snapshot, which captures
        ``_new_boxes``."""
        ocr_lines = (box.ocr_text or box.text or "").split("\n")
        cur_lines = (box.text or "").split("\n")
        del self._new_boxes[box.edit_key]
        for i, lc in enumerate(lcs):
            ot = ocr_lines[i] if i < len(ocr_lines) else ""
            tx = cur_lines[i] if i < len(cur_lines) else ot
            # Per-line baseline = the box's line-0 baseline stepped down by its
            # leading; x = the line cover's left. The 7-tuple cover drives the bbox
            # and the in-place edit, so this origin only seats the editor mount.
            oy = float(box.origin[1]) + i * float(box.leading or 0.0)
            bid = self._next_box_id
            self._next_box_id += 1
            nb = NewBox(
                page_index=box.page_index, box_id=bid,
                origin=(float(lc[0]), oy), bbox=(0.0, 0.0, 0.0, 0.0),
                text=tx, font_family=box.font_family, size=box.size,
                color=box.color, bold=box.bold, italic=box.italic, dir=box.dir,
                cover=tuple(lc), render_mode=3, box_w=None, leading=0.0,
                ocr_text=ot, line_covers=())
            nb = replace(nb, bbox=self._newbox_bbox(nb))
            self._new_boxes[nb.edit_key] = nb
        self._invalidate_spans(box.page_index)
        self._dirty = True
        return True

    def _apply_manual_grouping(self, page_index: int, boxes: list,
                               merged: list) -> list:
        """Apply the user's GROUP / UNGROUP overrides to the auto-grouped
        ``boxes`` (REFLOW manual override). ``merged`` is the line-level span
        list (pre-paragraph-grouping) used to rebuild groups from member keys."""
        groups = self._manual_groups.get(page_index) or []
        ungroups = self._manual_ungroups.get(page_index) or set()
        if not groups and not ungroups:
            return boxes

        # line global_key -> the line Span (authoritative geometry/style).
        line_by_key = {s.global_key: s for s in merged}

        # (1) UNGROUP: any ParagraphBox whose member set was forced apart becomes
        # its individual member Spans again.
        out: list = []
        for b in boxes:
            if (getattr(b, "is_paragraph", False)
                    and self._box_member_keys(b) in ungroups):
                out.extend(b.members)
            else:
                out.append(b)
        boxes = out

        # (2) GROUP: gather the lines for each manual group and fuse them into one
        # ParagraphBox, removing the boxes they came from. Lines are taken from
        # the authoritative line map so the build sees real geometry.
        for group_keys in groups:
            member_lines = [line_by_key[k] for k in group_keys
                            if k in line_by_key]
            if len(member_lines) < 2:
                continue                       # keys went stale after an edit
            # Reading order = BASELINE (origin.y), then left-to-right. Sorting on
            # the bbox TOP scatters spans that share a line but differ in height
            # -- a bold word's bbox top sits lower than its regular line-mates,
            # so a bold mid-line run (e.g. a bold "2025") would sort to the end
            # and the joined text would scramble. Same-line spans share an exact
            # baseline, so this keeps them in true reading order.
            member_lines.sort(
                key=lambda s: (round(s.origin[1], 1), s.bbox[0]))
            # Remove every box fully absorbed by this group (the user selected
            # whole boxes, so each box is entirely in or out of group_keys).
            boxes = [b for b in boxes
                     if not (self._box_member_keys(b) <= group_keys)]
            boxes.append(self._build_paragraph(member_lines))

        boxes.sort(key=lambda b: (round(b.bbox[1], 2), b.bbox[0]))
        return boxes

    def new_boxes(self, page_index: int) -> list["NewBox"]:
        """The non-deleted boxes ADDED FROM SCRATCH on ``page_index`` (BUILD_SPEC
        §1.8). The view composes ``spans(page_index)`` + ``new_boxes(page_index)``
        into its full box list so every editable box is enumerated uniformly.
        Kept as a sibling of ``spans()`` (the pinned choice, BUILD_SPEC §9) so
        the overlap-merge path stays untouched."""
        return [b for b in self._new_boxes.values()
                if b.page_index == page_index and not b.deleted]

    # =====================================================================
    # editing + generalized undo/redo (the model owns the history)
    # =====================================================================
    # Every staging method follows one rule (BUILD_SPEC §1.5): capture the
    # box's COMPLETE before-state, mutate staged state, push a _Command, clear
    # redo, set dirty. The private _command() helper does the snapshot + push so
    # each mutator is a few lines. Existing spans key by (page, box.identity);
    # NewBoxes key by their edit_key.
    #
    # identity, NOT box.key: a ParagraphBox.key == its members[0] Span.key (both
    # (block,line,span)), so on a dense form where a paragraph and an overlapping
    # single line are both editable, keying _edits by box.key made their staged
    # edits share ONE slot -- a commit on one clobbered the other and the edit
    # reverted on reopen. identity is ("para",)-prefixed for a ParagraphBox so
    # the two never collide. box.key is left untouched (redaction keys off the
    # raw (block,line,span) via edited_keys, so it must stay the member indices).

    @staticmethod
    def _span_edit_key(page_index: int, span: Span) -> tuple:
        return (page_index, span.identity)

    def _span_state(self, key: tuple) -> _BoxState:
        """Snapshot an existing box's current staged state into a _BoxState."""
        e = self._edits.get(key)
        if e is None:
            return _BoxState()
        return _BoxState(new_text=e.new_text, style=e.style, move=e.move,
                         scale=e.scale, deleted=e.deleted,
                         alignment=e.alignment, line_spacing=e.line_spacing,
                         box_x=e.box_x, box_w=e.box_w, runs=e.runs)

    @staticmethod
    def _edit_from_state(span, state: _BoxState) -> "Edit":
        """A transient Edit mirroring ``state`` so the _effective_* helpers (which
        take an Edit) can read the box's current staged geometry/style without an
        installed _edits entry -- used mid-mutation to compute the current
        effective size before applying a delta."""
        return Edit(span=span, new_text=state.new_text, style=state.style,
                    move=state.move, scale=state.scale, deleted=state.deleted,
                    alignment=state.alignment, line_spacing=state.line_spacing,
                    box_x=state.box_x, box_w=state.box_w, runs=state.runs)

    def _newbox_state(self, key: tuple) -> _BoxState:
        """Snapshot a new box's current state (a copy of the NewBox + exists)."""
        b = self._new_boxes.get(key)
        if b is None:
            return _BoxState(newbox=None, exists=False)
        return _BoxState(newbox=replace(b), exists=not b.deleted)

    def _install_span_state(self, key: tuple, span,
                            state: _BoxState) -> None:
        """Write a box's staged state back, dropping a no-op Edit so a box
        reverted to source is not emitted. ``span`` may be a Span or a
        ParagraphBox (both share the (page, key) keyspace)."""
        edit = Edit(span=span, new_text=state.new_text, style=state.style,
                    move=state.move, scale=state.scale, deleted=state.deleted,
                    alignment=state.alignment, line_spacing=state.line_spacing,
                    box_x=state.box_x, box_w=state.box_w, runs=state.runs)
        if edit.is_noop:
            self._edits.pop(key, None)
        else:
            self._edits[key] = edit

    def _install_newbox_state(self, key: tuple, state: _BoxState) -> None:
        """Write a new box's state back; a non-existent state removes it."""
        if state.newbox is None or not state.exists:
            self._new_boxes.pop(key, None)
        else:
            self._new_boxes[key] = replace(state.newbox, deleted=False)

    def _install_form_state(self, key: tuple, state: _BoxState) -> None:
        """Write a form field's staged value back (forms §2): ``_FORM_UNSET``
        drops the entry (the widget's baseline shows again), anything else
        stages it. Undo/redo replay funnels through here via
        ``_install_state``, so one choke point owns the map."""
        if state.form_value is _FORM_UNSET:
            self._form_edits.pop(key, None)
        else:
            self._form_edits[key] = state.form_value

    def _image_state(self, key: tuple) -> _BoxState:
        """Snapshot a placed image's current state (a copy of the ImageBox +
        exists) -- the ``_newbox_state`` pattern (images & signatures §2.2)."""
        b = self._images.get(key)
        if b is None:
            return _BoxState(imagebox=None, exists=False)
        return _BoxState(imagebox=replace(b), exists=not b.deleted)

    def _install_image_state(self, key: tuple, state: _BoxState) -> None:
        """Write a placed image's state back; non-existent removes it."""
        if state.imagebox is None or not state.exists:
            self._images.pop(key, None)
        else:
            self._images[key] = replace(state.imagebox, deleted=False)

    def _xim_state(self, key: tuple) -> _BoxState:
        """Snapshot an existing image's staged state (images & signatures
        §2.2, M3). The staged state IS the deletion: an installed
        ``_xim_deletes`` entry reads ``exists=False`` (deletion staged),
        no entry reads ``exists=True`` (the file occurrence stands)."""
        x = self._xim_deletes.get(key)
        if x is None:
            return _BoxState(xim=None, exists=True)
        return _BoxState(xim=x, exists=False)

    def _install_xim_state(self, key: tuple, state: _BoxState) -> None:
        """Write an existing image's staged state back: ``exists=False``
        installs the deletion entry, ``exists=True`` removes it (undo of a
        delete restores the file occurrence)."""
        if state.exists or state.xim is None:
            self._xim_deletes.pop(key, None)
        else:
            self._xim_deletes[key] = state.xim

    def _install_state(self, key: tuple, span: Span | None,
                       state: _BoxState) -> None:
        # Every fine-grained mutation AND every undo/redo replay funnels
        # through here, so this is the ONE choke point for dropping the
        # page's ``page_words`` memo (text-editing UX §5.1): the staged state
        # this page bakes just changed, so its word extraction is stale.
        self._words_cache.pop(key[0], None)
        if key[1] == "new":
            self._install_newbox_state(key, state)
        elif key[1] == "form":
            self._install_form_state(key, state)
        elif key[1] == "img":
            self._install_image_state(key, state)
        elif key[1] == "xim":
            self._install_xim_state(key, state)
        else:
            self._install_span_state(key, span, state)

    def _push_command(self, key: tuple, span: Span | None, kind: str,
                      before: _BoxState, after: _BoxState, label: str) -> None:
        """Install ``after`` and record the command (clearing redo, dirtying).
        Skips the push when nothing actually changed."""
        self._install_state(key, span, after)
        # Pushing while BELOW the last-save history depth discards the redo
        # branch that held the saved state: the baseline becomes unreachable
        # (QUndoStack's clean-index rule), so it invalidates until the next
        # save -- a later depth coincidence must not read clean.
        if self._saved_undo_depth > len(self._undo):
            self._saved_undo_depth = -1
        self._undo.append(_Command(edit_key=key, kind=kind, before=before,
                                   after=after, label=label, span=span))
        self._redo.clear()
        self._dirty = True

    def coalesce_last_undo(self) -> bool:
        """Fuse the top TWO history commands into one (text-editing UX §3.1).

        The arrow-key nudge fires one ``move_box`` per keypress; the Qt side
        merges consecutive nudge ``BoxCommand``s via ``mergeWith``, and calls
        this so the model's history fuses in LOCKSTEP -- one Qt command keeps
        equaling one model command, so a single undo of a held arrow restores
        the pre-nudge origin and ``can_undo`` mirrors the Qt stack exactly.

        Guarded: only fuses when both top commands are kind "move" on the SAME
        ``edit_key``. Returns True when fused (the caller's ``mergeWith`` must
        only report a Qt merge when the model fused too); any other shape is
        left untouched and returns False. The fused command spans
        ``older.before -> newer.after``, exactly what replaying both would
        install (the _BoxState snapshots are complete, not deltas)."""
        if len(self._undo) < 2:
            return False
        newer = self._undo[-1]
        older = self._undo[-2]
        if newer.kind != "move" or older.kind != "move":
            return False
        if newer.edit_key != older.edit_key:
            return False
        self._undo.pop()
        self._undo.pop()
        self._undo.append(_Command(edit_key=newer.edit_key, kind="move",
                                   before=older.before, after=newer.after,
                                   label=newer.label, span=newer.span))
        # The fuse rewrote the top of the history: a last-save baseline at or
        # above the fused pair no longer identifies the saved state.
        if self._saved_undo_depth >= len(self._undo):
            self._saved_undo_depth = -1
        return True

    # --- text (signature extended with optional rich runs) ---
    def stage_edit(self, page_index: int, box, new_text: str,
                   runs: tuple | None = None) -> None:
        """Record/replace the TEXT edit for ``box`` (a Span OR a NewBox) and push
        it onto the undo stack (clearing redo). If ``new_text`` equals the box's
        CURRENT staged text nothing happens; for a Span, reverting to the
        ORIGINAL text drops the staged edit. The NewBox branch lets a freshly
        added box's typed text commit through the SAME path the inline editor
        uses for existing runs.

        ``runs`` (optional) carries per-selection RICH styling: a tuple of
        ``(text, bold, italic)`` segments covering the whole new text, produced
        by bolding/italicizing a selection inside the inline editor. When given,
        it is authoritative (``new_text`` is recomputed from it); when None, the
        edit is uniform and any previously staged runs are cleared."""
        if isinstance(box, NewBox):
            # A NewBox owns its text directly. Update it and recompute the bbox
            # from the resolved face metrics (§1.6) so the selection outline /
            # hit-test track the new content. One "text" command; undo restores
            # the prior text + bbox via the captured _BoxState.
            key = box.edit_key
            cur = self._new_boxes.get(key)
            nruns = self._normalize_runs(runs)
            cur_runs = (cur.runs or None) if cur is not None else None
            dlog("EDITOR", "stage_edit", cur=(cur is not None),
                 curtext=(cur.text if cur is not None else None), new=new_text,
                 para=(bool(cur.is_paragraph) if cur is not None else None),
                 cover7=(bool(cur.cover and len(cur.cover) == 7)
                         if cur is not None else None))
            if cur is None or (new_text == cur.text and nruns == cur_runs):
                dlog("EDITOR", "stage_skip", reason="no_cur" if cur is None
                     else "text_and_runs_unchanged")
                return
            before = self._newbox_state(key)
            updated = replace(cur, text=new_text, runs=(nruns or ()))
            # 0.3.0: a scanned-OCR word carries a cover; render its edit as a
            # recolored + hard-damaged raster (sampled from the scan under the
            # cover) so it blends, instead of drawing crisp vector text. Falls back
            # to plain text if the raster cannot be built.
            if cur.cover and len(cur.cover) == 7:
                # REVERT-TO-SCAN: if the text is back to the ORIGINAL recognized text, do NOT
                # synthesize anything (not even the fallback raster, which would draw clean
                # vector-ish glyphs in a foreign font and white out the scan -- the "re-edit
                # jumps to a new font/position" bug). Go straight to the invisible overlay so
                # the real scan shows, and drop the stale edit rect so the bbox stops shifting.
                _norm = lambda s: " ".join((s or "").split())
                if _norm(new_text) == _norm(getattr(cur, "ocr_text", "")) \
                        and not nruns:
                    # Reverted to the original scanned text AND no per-run styling:
                    # invisible overlay, the real scan shows. (With runs present the
                    # text is unchanged but a style differs -> fall through to bake it.)
                    updated = replace(updated, edit_image=b"", edit_image_rect=(),
                                      render_mode=3)
                else:
                    # UNIFIED raster: a paragraph and a single line ride the SAME in-place
                    # engine -- a paragraph is just N single lines, each keeping the scan
                    # pixels of the words the user did not change and synthesizing only the
                    # changed ones. A paragraph falls back to the old whole-block raster if
                    # the per-line engine cannot run; a single line falls back to the
                    # whole-line degrade raster, then to plain text.
                    raster = (
                        (self._scanned_lines_raster(updated, new_text, runs=nruns)
                         or self._scanned_paragraph_raster(updated, new_text))
                        if cur.is_paragraph
                        else (self._scanned_inplace_raster(updated, new_text, cur.text,
                                                           runs=nruns)
                              or self._scanned_edit_raster(updated, new_text, cur.text)))
                    dlog("RENDER", "raster_done", para=bool(cur.is_paragraph),
                         ok=(raster is not None),
                         rect=(tuple(round(float(v), 1) for v in raster[1])
                               if raster else None))
                    if raster is not None:
                        # A VISIBLE recolored/degraded raster overlay (render_mode 0) whose
                        # cover whites out the scanned word under it.
                        updated = replace(updated, edit_image=raster[0],
                                          edit_image_rect=raster[1], render_mode=0)
                    else:
                        # Bake could not run: fall back to the invisible overlay so the scan
                        # shows rather than drawing clean vector text in a foreign font.
                        updated = replace(updated, edit_image=b"", edit_image_rect=(),
                                          render_mode=3)
            elif cur.render_mode != 0:
                # An ordinary (non-scan) overlay the user edits becomes real visible text.
                updated = replace(updated, render_mode=0)
            updated = replace(updated, bbox=self._newbox_bbox(updated))
            after = _BoxState(newbox=updated, exists=True)
            self._push_command(key, None, "text", before, after,
                               self._text_label(cur.text, new_text))
            return
        key = self._span_edit_key(page_index, box)
        before = self._span_state(key)
        runs = self._normalize_runs(runs)
        cur_text = before.new_text if before.new_text is not None else box.text
        if runs is None:
            # Plain (uniform) text: no-op only when the text is unchanged AND
            # there are no staged rich runs to clear (a plain commit over a
            # rich edit means "make it uniform again" -- a real change).
            if new_text == cur_text and before.runs is None:
                return
            after = replace(
                before,
                new_text=None if new_text == box.text else new_text,
                runs=None,
            )
        else:
            if runs == before.runs:
                return
            joined = "".join(r[0] for r in runs)
            new_text = joined
            after = replace(
                before,
                new_text=None if joined == box.text else joined,
                runs=runs,
            )
        self._push_command(key, box, "text", before, after,
                           self._text_label(cur_text, new_text))

    @staticmethod
    def _normalize_runs(runs) -> tuple | None:
        """Canonicalize rich runs: drop empties, coerce flags to bool, merge
        adjacent same-style runs. None/empty -> None (uniform). Tolerates an
        optional 4th element (a RunStyle for a scanned box's per-run colour/family);
        a legacy 3-tuple keeps rs=None and re-emits a 3-tuple, so the vector path is
        byte-identical (only adjacent runs with the SAME rs coalesce)."""
        if not runs:
            return None
        out: list[list] = []
        for seg in runs:
            t = seg[0]
            if not t:
                continue
            b, i = bool(seg[1]), bool(seg[2])
            rs = seg[3] if len(seg) > 3 else None
            if out and out[-1][1] == b and out[-1][2] == i and out[-1][3] == rs:
                out[-1][0] += t
            else:
                out.append([t, b, i, rs])
        if not out:
            return None
        return tuple((t, b, i) if rs is None else (t, b, i, rs)
                     for t, b, i, rs in out)

    def staged_runs(self, page_index: int, box) -> tuple | None:
        """The box's staged rich runs (tuple of (text, bold, italic)), or None
        when the box is uniform (no per-selection styling staged)."""
        if isinstance(box, NewBox):
            cur = self._new_boxes.get(box.edit_key, box)
            return tuple(cur.runs) if getattr(cur, "runs", None) else None
        e = self._edits.get(self._span_edit_key(page_index, box))
        return e.runs if e is not None else None

    def paragraph_runs(self, box) -> tuple | None:
        """The INTRINSIC per-member styling of a (grouped or auto) ParagraphBox
        as (text, bold, italic) runs whose joined text equals the box's text --
        so the editor and bake preserve a bold/italic member span (e.g. a bold
        '2025' pulled into a group) instead of flattening the paragraph to its
        first member's style. Returns None when every member shares the box's
        base style (a truly uniform paragraph needs no rich runs)."""
        members = getattr(box, "members", None)
        if not members:
            return None
        from .fonts import is_bold, is_italic
        base = (is_bold(box.font, box.flags), is_italic(box.font, box.flags))
        runs: list = []
        mixed = False
        for i, m in enumerate(members):
            t = _normalize_spaces(m.text).strip()
            if not t:
                continue
            if runs:
                runs.append((" ", base[0], base[1]))   # the soft-break space
            b, it = is_bold(m.font, m.flags), is_italic(m.font, m.flags)
            if (b, it) != base:
                mixed = True
            runs.append((t, b, it))
        if not mixed or not runs:
            return None
        return tuple(runs)

    # --- style (NEW) ---
    def set_style(self, page: int, box, *, font_family: str | None = None,
                  size: float | None = None, color: tuple | None = None,
                  bold: bool | None = None, italic: bool | None = None,
                  alignment: str | None = None,
                  line_spacing: float | None = None) -> None:
        """Merge these non-None overrides onto ``box`` (a Span, NewBox, or
        ParagraphBox) and push ONE 'style' command (BUILD_SPEC §1.5). None args
        leave that attribute unchanged. No-op (no command) when nothing actually
        changes. A picked ``font_family`` resolves via ``resolve_family`` at
        render/save time (never Tier 1).

        ``alignment`` / ``line_spacing`` are PARAGRAPH layout params (REFLOW_SPEC
        §R5.2): they route to ``Edit.alignment`` / ``Edit.line_spacing`` (NOT
        ``StyleOverride``, which is glyph style) and the bake reads them in
        ``_insert_paragraph``. They only apply to a ParagraphBox; passing them on
        a Span/NewBox is a no-op (the inspector hides those controls there)."""
        # Paragraph layout overrides go on a separate command branch so they
        # commute with the glyph-style merge and round-trip through undo.
        if (alignment is not None or line_spacing is not None) \
                and getattr(box, "is_paragraph", False):
            if isinstance(box, NewBox):
                # A scanned multi-line OCR box is a NewBox, NOT a Span: it has no
                # ``.key`` and lives in ``_new_boxes``, so the Span paragraph path
                # (_span_edit_key) would crash. Store layout on the NewBox itself.
                self._set_newbox_paragraph_layout(page, box, alignment,
                                                  line_spacing)
            else:
                self._set_paragraph_layout(page, box, alignment, line_spacing)
            # Fall through to apply any glyph-style delta in the same call too.
        delta = StyleOverride(font_family=font_family, size=size, color=color,
                              bold=bold, italic=italic)
        if delta.is_empty:
            return
        if isinstance(box, NewBox):
            key = box.edit_key
            before = self._newbox_state(key)
            b = self._new_boxes.get(key)
            if b is None:
                return
            new_fields = {}
            if font_family is not None:
                new_fields["font_family"] = font_family
                # A picked family overrides the scan edge-match (a scanned box).
                if b.cover and len(b.cover) == 7:
                    new_fields["font_picked"] = True
            if size is not None and size > 0:
                new_fields["size"] = size
            if color is not None:
                new_fields["color"] = color
            if bold is not None:
                new_fields["bold"] = bold
            if italic is not None:
                new_fields["italic"] = italic
            updated = replace(b, **new_fields)
            updated = replace(updated, bbox=self._newbox_bbox(updated))
            if updated == b:
                return
            after = _BoxState(newbox=updated, exists=True)
            self._push_command(key, None, "style", before, after,
                               f"Style {self._fmt_style(delta)}")
        else:
            key = self._span_edit_key(page, box)
            before = self._span_state(key)
            # Guard a non-positive size the same way the NewBox branch does:
            # drop it so effective_style never reports a <=0 pt value back to the
            # inspector (the span branch previously stored it verbatim).
            if size is not None and size <= 0:
                delta = replace(delta, size=None)
                if delta.is_empty:
                    return
            merged = before.style.merged_with(delta)
            after = replace(before, style=merged)
            # Size <-> scale fold: the inspector always passes the ABSOLUTE size
            # it displays, which already includes any prior resize scale (see
            # effective_style: size = (style.size or box.size) * edit.scale). If a
            # resize is in play (scale != 1.0), storing that absolute size verbatim
            # while leaving scale set would multiply it AGAIN at bake time
            # (_effective_size), so the box would jump. Fold the scale into the
            # stored size and reset scale to 1.0 so the stored absolute IS the
            # effective size. No-op when no size delta or scale already 1.0.
            new_scale = before.scale
            if delta.size is not None and abs(before.scale - 1.0) > 1e-9:
                new_scale = 1.0
            if merged == before.style and abs(new_scale - before.scale) < 1e-9:
                return
            after = replace(after, scale=new_scale)
            self._push_command(key, box, "style", before, after,
                               f"Style {self._fmt_style(delta)}")

    def _set_paragraph_layout(self, page: int, box, alignment: str | None,
                              line_spacing: float | None) -> None:
        """Stage a paragraph ALIGNMENT and/or LINE-SPACING change as one command
        (REFLOW_SPEC §R5.2). These live on ``Edit.alignment`` / ``Edit.line_spacing``
        (layout params, not glyph style) so the bake re-wraps via ``_insert_paragraph``.
        No-op when the value matches the current staged value."""
        key = self._span_edit_key(page, box)
        before = self._span_state(key)
        new_align = before.alignment if alignment is None else alignment
        new_spacing = before.line_spacing if line_spacing is None else line_spacing
        if line_spacing is not None and (line_spacing <= 0
                                         or abs(line_spacing - 1.0) < 1e-9):
            # A 1.0 (or non-positive) multiplier is the default -> clear it.
            new_spacing = None
        if (new_align == before.alignment
                and new_spacing == before.line_spacing):
            return
        after = replace(before, alignment=new_align, line_spacing=new_spacing)
        parts = []
        if alignment is not None:
            parts.append(alignment)
        if line_spacing is not None:
            parts.append(f"spacing {line_spacing:g}x")
        self._push_command(key, box, "style", before, after,
                           "Paragraph " + ", ".join(parts))

    def _set_newbox_paragraph_layout(self, page: int, box, alignment: str | None,
                                     line_spacing: float | None) -> None:
        """Paragraph alignment for a NewBox paragraph (a scanned multi-line OCR
        box is a NewBox, not a Span). ``alignment`` maps to the NewBox's own
        field via its command machinery so it round-trips through undo. Line
        spacing has no NewBox field and the in-place scan bake keeps each line on
        its scanned baseline, so a line-spacing change is a no-op here (rather
        than crashing the Span path). No command when nothing changes."""
        key = box.edit_key
        b = self._new_boxes.get(key)
        if b is None:
            return
        new_fields = {}
        if alignment is not None and alignment != b.alignment:
            new_fields["alignment"] = alignment
        if not new_fields:
            return
        before = self._newbox_state(key)
        updated = replace(b, **new_fields)
        after = _BoxState(newbox=updated, exists=True)
        self._push_command(key, None, "style", before, after,
                           f"Paragraph {alignment}")

    # --- move (NEW) ---
    def _lift_scan_raster(self, box: "NewBox"):
        """The box's OWN scanned pixels as ``(png_bytes, text-space rect)`` so a MOVE
        can carry the scanned text with the box. An un-edited OCR overlay (render_mode
        3) draws NOTHING, so moving it alone shifts only the invisible box and strands
        the text on the page. Lifting it to a raster (the in-place engine run with
        UNCHANGED text returns the kept scan pixels; a single line falls back to its raw
        scan crop) lets the text ride the move, and the cover then vacates the original
        spot. None if it cannot build -- the move then just shifts the box as before."""
        try:
            # If a table rule crosses a glyph, route ONLY those glyphs through the
            # in-place EDIT pipeline so the moved letter re-renders clean from the
            # matched font instead of carrying the scarred rule.
            rng = self._crossed_char_range(box)
            if rng is not None:
                piped = self._scanned_inplace_raster(
                    box, box.text, box.text, force_synth=rng)
                if piped is not None:
                    return piped
            if box.line_covers:                       # multi-line: per-line scan raster
                r = (self._scanned_lines_raster(box, box.text)
                     or self._scanned_paragraph_raster(box, box.text))
                if r is not None:
                    return r
            r = self._scanned_inplace_raster(box, box.text, box.text)
            if r is not None:
                return r
            import cv2
            import numpy as np
            ctx = self.scan_edit_context(box, box.text)
            if not ctx:
                return None
            region = np.ascontiguousarray(ctx["region"])
            ok, buf = cv2.imencode(".png", cv2.cvtColor(region, cv2.COLOR_RGB2BGR))
            return (buf.tobytes(), tuple(ctx["rect"])) if ok else None
        except Exception:
            return None

    def move_box(self, page: int, box, dx: float, dy: float) -> None:
        """Translate ``box`` by (dx, dy) PDF points (cumulative with any prior
        move) and push ONE 'move' command (BUILD_SPEC §1.5). For a Span this
        accumulates ``Edit.move``; for a NewBox it shifts origin + bbox. An un-edited
        OCR overlay is LIFTED to a raster first so its scanned text travels with it."""
        if abs(dx) < 1e-9 and abs(dy) < 1e-9:
            return
        if isinstance(box, NewBox):
            key = box.edit_key
            before = self._newbox_state(key)
            b = self._new_boxes.get(key)
            if b is None:
                return
            # LIFT an un-edited OCR overlay so its SCANNED TEXT travels with the box:
            # a render_mode-3 overlay draws nothing, so moving it alone shifts only the
            # invisible box and strands the text (the "outline detaches from its text"
            # bug). Rasterize its scan pixels as the edit_image and flip it visible; the
            # cover then vacates the original spot and the tile rides the move below.
            if (getattr(b, "render_mode", 0) == 3 and b.cover
                    and len(b.cover) == 7 and not b.edit_image):
                lifted = self._lift_scan_raster(b)
                if lifted is not None:
                    b = replace(b, edit_image=lifted[0],
                                edit_image_rect=tuple(lifted[1]), render_mode=0)
            ox, oy = b.origin
            x0, y0, x1, y1 = b.bbox
            updated = replace(b, origin=(ox + dx, oy + dy),
                              bbox=(x0 + dx, y0 + dy, x1 + dx, y1 + dy))
            # An OCR box bakes its edited text as a raster at a FIXED text-space rect;
            # translate THAT so the text moves with the box. The cover stays put: it
            # keeps erasing the line's ORIGINAL position (moving scanned text = blank
            # it here, draw it there), so a move does not leave a ghost behind.
            if b.edit_image and b.edit_image_rect and len(b.edit_image_rect) == 4:
                ex0, ey0, ex1, ey1 = b.edit_image_rect
                updated = replace(updated,
                                  edit_image_rect=(ex0 + dx, ey0 + dy,
                                                   ex1 + dx, ey1 + dy))
            after = _BoxState(newbox=updated, exists=True)
            self._push_command(key, None, "move", before, after, "Move box")
        else:
            key = self._span_edit_key(page, box)
            before = self._span_state(key)
            pmx, pmy = before.move if before.move is not None else (0.0, 0.0)
            after = replace(before, move=(pmx + dx, pmy + dy))
            self._push_command(key, box, "move", before, after, "Move box")

    # --- resize (NEW) ---
    def resize_box(self, page: int, box, scale: float, *,
                   anchor: tuple | None = None) -> None:
        """Multiply the box's effective font size by ``scale`` (ABSOLUTE relative
        to the box's ORIGINAL size at drag start, NOT incremental -- the UI
        tracks the start size, BUILD_SPEC §1.5) and scale its geometry about
        ``anchor`` (default: the box's top-left in PDF points). Font size scales
        proportionally with the box. One handle-drag = one command."""
        scale = max(_MIN_RESIZE_SCALE, scale)
        if isinstance(box, NewBox):
            key = box.edit_key
            before = self._newbox_state(key)
            b = self._new_boxes.get(key)
            if b is None:
                return
            new_size = max(_MIN_FONT_SIZE, b.size * scale)
            if abs(new_size - b.size) < 1e-9:
                return
            updated = replace(b, size=new_size)
            updated = replace(updated, bbox=self._newbox_bbox(updated))
            after = _BoxState(newbox=updated, exists=True)
            self._push_command(key, None, "resize", before, after, "Resize box")
        else:
            key = self._span_edit_key(page, box)
            before = self._span_state(key)
            # The view derives ``scale`` from the box's DISPLAYED (effective) rect
            # diagonal, so it is relative to the box's CURRENT effective size, not
            # the immutable original. Fold it into an absolute target size and
            # store that in style.size with scale reset to 1.0, so resize and an
            # absolute size-set share one representation (they commute, and a
            # later inspector size-set reads/replaces a clean absolute). Without
            # the fold a prior size override would be multiplied by the new scale
            # at bake time.
            cur_eff = self._effective_size(box, self._edit_from_state(box, before))
            target = max(_MIN_FONT_SIZE, cur_eff * scale)
            new_style = replace(before.style, size=target)
            if (new_style == before.style and abs(before.scale - 1.0) < 1e-9):
                return
            after = replace(before, style=new_style, scale=1.0)
            self._push_command(key, box, "resize", before, after, "Resize box")

    def resize_text_frame(self, page: int, box, x: float, w: float) -> None:
        """Resize an existing TEXT box's FRAME to left ``x`` / width ``w`` (PDF
        points) WITHOUT scaling the font: the text re-wraps to the new width at
        its CURRENT size -- a single line wraps to several when narrowed, a
        paragraph reflows. One handle-drag = one command. NewBoxes are skipped
        (they own their own geometry and keep the scale resize)."""
        if isinstance(box, (NewBox, ImageBox, ExistingImage)):
            return
        w = max(_MIN_FRAME_WIDTH, float(w))
        key = self._span_edit_key(page, box)
        before = self._span_state(key)
        after = replace(before, box_x=float(x), box_w=w)
        if after == before:
            return
        self._push_command(key, box, "frame_resize", before, after, "Resize box")

    # --- delete (NEW) ---
    def delete_box(self, page: int, box) -> None:
        """Mark ``box`` deleted: redact its bbox(es), reinsert nothing. For a
        NewBox it simply stops being emitted. One command; undo restores it
        (BUILD_SPEC §1.5)."""
        if isinstance(box, NewBox):
            key = box.edit_key
            before = self._newbox_state(key)
            b = self._new_boxes.get(key)
            if b is None or b.deleted:
                return
            after = _BoxState(newbox=replace(b, deleted=True), exists=False)
            self._push_command(key, None, "delete", before, after, "Delete box")
        else:
            key = self._span_edit_key(page, box)
            before = self._span_state(key)
            if before.deleted:
                return
            after = replace(before, deleted=True)
            self._push_command(key, box, "delete", before, after, "Delete box")

    # --- add (NEW) ---
    def add_box(self, page: int, origin: tuple, text: str, family: str,
                size: float, color: tuple, bold: bool, italic: bool,
                direction: tuple = (1.0, 0.0), cover: tuple = (),
                render_mode: int = 0, box_w: float | None = None,
                leading: float = 0.0, alignment: str = "left",
                line_covers: tuple = ()) -> NewBox:
        """Create a NewBox at baseline ``origin`` (PDF points) with the given
        style, compute its bbox from ``resolve_family`` metrics (BUILD_SPEC
        §1.6), register it, push an 'add' command, and return it. The caller
        selects it and enters text-edit mode. ``direction`` is the TEXT-space
        writing direction (default horizontal; OCR sets it for rotated pages).
        ``cover`` is an optional (x0,y0,x1,y1,r,g,b) painted under the text
        (OCR replaces the scanned glyphs instead of doubling them)."""
        box_id = self._next_box_id
        self._next_box_id += 1
        box = NewBox(page_index=page, box_id=box_id, origin=tuple(origin),
                     bbox=(0.0, 0.0, 0.0, 0.0), text=text, font_family=family,
                     size=size, color=tuple(color), bold=bold, italic=italic,
                     dir=tuple(direction), cover=tuple(cover),
                     render_mode=int(render_mode),
                     box_w=box_w, leading=float(leading), alignment=alignment,
                     line_covers=tuple(tuple(lc) for lc in (line_covers or ())),
                     # A scanned-OCR box (it has a cover) fixes its ORIGINAL text now,
                     # so re-edits always diff against the scan, not an edited string.
                     ocr_text=(text if cover else ""))
        box = replace(box, bbox=self._newbox_bbox(box))
        key = box.edit_key
        before = _BoxState(newbox=None, exists=False)
        after = _BoxState(newbox=replace(box), exists=True)
        self._push_command(key, None, "add", before, after, "Add text box")
        return self._new_boxes[key]

    def _newbox_bbox(self, box: NewBox) -> tuple:
        """Derive a NewBox's bbox from its resolved face metrics (BUILD_SPEC
        §1.6). top = baseline - ascent*size; bottom = baseline + |descender|*size;
        width = the resolved face's advance for the text at the size. For a
        rotated box (``dir`` != horizontal) the text rectangle is rotated about
        the baseline origin and the AABB of its corners is returned."""
        # An OCR box (it has a 7-tuple cover) is bounded by the SCANNED line/area,
        # already in this page's text space and rotation-correct -- not by single-
        # line font metrics, which under-measure the scan's real width and leave the
        # tail ("with MD") outside the selection frame. Use the EDITED extent when
        # the box carries an edit raster (an insert may have grown the line past the
        # original cover), else the original cover.
        if box.cover and len(box.cover) == 7:
            if box.edit_image_rect and len(box.edit_image_rect) == 4:
                return tuple(box.edit_image_rect)
            return tuple(box.cover[:4])
        rf = self.font_engine.resolve_family(
            box.font_family, box.bold, box.italic, box.text)
        fobj = self.font_engine.fitz_font_for(rf)
        w = fobj.text_length(box.text or " ", fontsize=box.size)
        asc = fobj.ascender * box.size
        desc_abs = -fobj.descender * box.size      # descender is negative
        x0, y = box.origin
        dx, dy = box.dir
        if abs(dx - 1.0) < 1e-3 and abs(dy) < 1e-3:
            return (x0, y - asc, x0 + w, y + desc_abs)
        # Rotated: baseline runs along dir; "up" (toward ascenders) is (dy, -dx).
        ux, uy = dy, -dx
        corners = [
            (x0 + asc * ux, y + asc * uy),
            (x0 - desc_abs * ux, y - desc_abs * uy),
            (x0 + w * dx + asc * ux, y + w * dy + asc * uy),
            (x0 + w * dx - desc_abs * ux, y + w * dy - desc_abs * uy),
        ]
        xs = [c[0] for c in corners]
        ys = [c[1] for c in corners]
        return (min(xs), min(ys), max(xs), max(ys))

    # --- placed images (images & signatures §2.3) --------------------------
    def add_image(self, page_index: int, rect: tuple, image_bytes: bytes,
                  kind: str = "file", natural_px: tuple = (0, 0)) -> ImageBox:
        """Stage ONE placed image on ``page_index`` (one 'img_add' command,
        clears redo, dirties -- the ``add_box`` discipline). ``rect`` is
        text-space PDF points, clamped into the page rect; ``image_bytes``
        must be PNG or JPEG (magic-number check -- raises ``ValueError``
        before any state changes). Returns the staged box."""
        if not (isinstance(image_bytes, (bytes, bytearray))
                and (bytes(image_bytes).startswith(_PNG_MAGIC)
                     or bytes(image_bytes).startswith(_JPEG_MAGIC))):
            raise ValueError("add_image requires PNG or JPEG bytes")
        if not 0 <= page_index < self.page_count:
            raise IndexError(f"page index {page_index} out of range")
        rect = self._clamp_image_rect(page_index, rect)
        box_id = next(self._image_id_counter)
        box = ImageBox(page_index=page_index, rect=rect,
                       image=bytes(image_bytes), kind=kind,
                       natural_px=tuple(natural_px), box_id=box_id)
        key = box.edit_key
        before = _BoxState(imagebox=None, exists=False)
        after = _BoxState(imagebox=replace(box), exists=True)
        noun = {"stamp": "stamp", "signature": "signature"}.get(kind, "image")
        self._push_command(key, None, "img_add", before, after,
                           f"Insert {noun}")
        return self._images[key]

    def move_image(self, page_index: int, box, dx: float, dy: float) -> None:
        """Translate a staged image by (dx, dy) text-space points -- stored
        as a new ABSOLUTE rect (cumulative with prior moves). One 'img_move'
        command."""
        if abs(dx) < 1e-9 and abs(dy) < 1e-9:
            return
        key = box.edit_key
        cur = self._images.get(key)
        if cur is None:
            return
        before = self._image_state(key)
        x0, y0, x1, y1 = cur.rect
        updated = replace(cur, rect=(x0 + dx, y0 + dy, x1 + dx, y1 + dy))
        after = _BoxState(imagebox=updated, exists=True)
        self._push_command(key, None, "img_move", before, after, "Move image")

    def resize_image(self, page_index: int, box, rect: tuple) -> None:
        """Set a staged image's ABSOLUTE new rect (not a scale: images have
        no baseline/size fold-in like ``resize_box``). The rect is normalized
        and floored at ``_MIN_IMAGE_EDGE_PT`` per side. One 'img_resize'
        command; a no-change rect pushes nothing."""
        key = box.edit_key
        cur = self._images.get(key)
        if cur is None:
            return
        x0, y0, x1, y1 = rect
        x0, x1 = sorted((float(x0), float(x1)))
        y0, y1 = sorted((float(y0), float(y1)))
        x1 = max(x1, x0 + _MIN_IMAGE_EDGE_PT)
        y1 = max(y1, y0 + _MIN_IMAGE_EDGE_PT)
        new_rect = (x0, y0, x1, y1)
        if all(abs(a - b) < 1e-9 for a, b in zip(new_rect, cur.rect)):
            return
        before = self._image_state(key)
        after = _BoxState(imagebox=replace(cur, rect=new_rect), exists=True)
        self._push_command(key, None, "img_resize", before, after,
                           "Resize image")

    def delete_image(self, page_index: int, box) -> None:
        """Remove a staged image (exists=False; undo restores it whole). One
        'img_delete' command."""
        key = box.edit_key
        cur = self._images.get(key)
        if cur is None or cur.deleted:
            return
        before = self._image_state(key)
        after = _BoxState(imagebox=replace(cur, deleted=True), exists=False)
        self._push_command(key, None, "img_delete", before, after,
                           "Delete image")

    def image_boxes(self, page_index: int) -> list["ImageBox"]:
        """The non-deleted staged images on ``page_index``, in placement
        (box_id) order -- the bake's draw order."""
        return sorted((b for b in self._images.values()
                       if b.page_index == page_index and not b.deleted),
                      key=lambda b: b.box_id)

    def image_boxes_all(self) -> list["ImageBox"]:
        """Every non-deleted staged image (edit_count / bake seeding)."""
        return [b for b in self._images.values() if not b.deleted]

    def _clamp_image_rect(self, page_index: int, rect: tuple) -> tuple:
        """Normalize ``rect`` and clamp it into the page's unrotated rect
        (the space ``insert_image`` takes), preserving the minimum edge."""
        page_rect = self.working[page_index].rect
        x0, y0, x1, y1 = rect
        x0, x1 = sorted((float(x0), float(x1)))
        y0, y1 = sorted((float(y0), float(y1)))
        w = min(max(x1 - x0, _MIN_IMAGE_EDGE_PT), page_rect.width)
        h = min(max(y1 - y0, _MIN_IMAGE_EDGE_PT), page_rect.height)
        x0 = min(max(x0, page_rect.x0), page_rect.x1 - w)
        y0 = min(max(y0, page_rect.y0), page_rect.y1 - h)
        return (x0, y0, x0 + w, y0 + h)

    # --- existing page images (images & signatures §2.3, M3) ---------------
    def existing_images(self, page_index: int) -> list["ExistingImage"]:
        """Every image occurrence already IN the file on ``page_index``, in
        ``get_image_info`` order (one record per occurrence -- a shared xref
        yields one per placement). Memoized per page like ``spans()``: the
        records depend only on ``self.working``, so the memo clears wholesale
        in ``_invalidate_caches`` (bake / structural op) and never
        per-mutation. ``rect`` comes straight off ``info["bbox"]`` --
        probe-verified unrotated text space on every /Rotate value. Staged
        deletions do NOT filter here (the read is the file truth); callers
        that want the live view subtract ``xim_deletes(page_index)``."""
        cached = self._xim_cache.get(page_index)
        if cached is not None:
            return cached
        result: list[ExistingImage] = []
        if 0 <= page_index < self.working.page_count:
            occ = 0
            for info in self.working[page_index].get_image_info(xrefs=True):
                xref = int(info.get("xref", 0) or 0)
                if xref <= 0:
                    continue        # inline image: no xref to delete/extract
                result.append(ExistingImage(
                    page_index=page_index, xref=xref,
                    rect=tuple(float(v) for v in info["bbox"]), occ=occ))
                occ += 1
        self._xim_cache[page_index] = result
        return result

    def delete_existing_image(self, page_index: int, xim: "ExistingImage"
                              ) -> None:
        """Stage the deletion of one existing image occurrence (ONE
        'xim_delete' command; undo restores the file occurrence). The bake
        runs a page-scoped image-REMOVE redaction over ``xim.rect`` FIRST in
        the pipeline -- §0 probe: page text and the same xref's occurrences
        on other pages survive. Documented limitation (§5): a second image
        whose occurrence intersects the redaction rect is removed too
        (apply_redactions removes the WHOLE occurrence a rect touches)."""
        key = xim.edit_key
        if key in self._xim_deletes:
            return
        before = self._xim_state(key)
        after = _BoxState(xim=xim, exists=False)
        self._push_command(key, None, "xim_delete", before, after,
                           "Delete page image")

    def xim_deletes(self, page_index: int) -> list["ExistingImage"]:
        """The staged existing-image deletions on ``page_index`` (the
        bake's redaction list), in (xref, occ) order."""
        return sorted((x for x in self._xim_deletes.values()
                       if x.page_index == page_index),
                      key=lambda x: (x.xref, x.occ))

    def extract_image_bytes(self, xref: int) -> tuple[bytes, tuple]:
        """``(image_bytes, (w, h))`` for an image xref of ``self.working``,
        ready for ``add_image`` (the move-macro payload, §5). The original
        stream is returned VERBATIM when it is already PNG/JPEG with no
        SMask (the no-recode rule); a masked or exotic-codec image is
        recombined per the §0 probe -- ``Pixmap(doc, xref)`` base +
        ``Pixmap(base, Pixmap(doc, smask))`` -- and recoded to PNG so the
        transparency rides along (file size may grow; accepted, §9)."""
        img = self.working.extract_image(xref)
        ext = (img.get("ext") or "").lower()
        smask = int(img.get("smask", 0) or 0)
        if ext in ("png", "jpeg", "jpg") and not smask:
            return img["image"], (int(img.get("width", 0)),
                                  int(img.get("height", 0)))
        base = fitz.Pixmap(self.working, xref)
        if base.colorspace is None or base.colorspace.n > 3:
            base = fitz.Pixmap(fitz.csRGB, base)   # PNG needs gray/RGB
        if smask:
            base = fitz.Pixmap(base, fitz.Pixmap(self.working, smask))
        return base.tobytes("png"), (base.width, base.height)

    # --- annotations (annotations & markup §3.2 / §3.3) -------------------
    def annotations(self, page_index: int) -> list["AnnotRecord"]:
        """Unified read API for annotation hotspots + the comments panel
        (annotations & markup §3.2): the annots already IN the file
        (``self.working``, with staged overrides applied -- deleted ones
        dropped, note moves shifting the rect, contents swaps showing)
        followed by the page's staged ``AnnotSpec``s in ``annot_id`` order.
        Memoized per page; every annot mutator and ``_invalidate_caches``
        drop the entry."""
        cached = self._annot_records_cache.get(page_index)
        if cached is not None:
            return cached
        records: list[AnnotRecord] = []
        if 0 <= page_index < self.working.page_count:
            page = self.working[page_index]
            for annot in (page.annots() or ()):
                atype = annot.type[0]
                if atype in (fitz.PDF_ANNOT_POPUP, fitz.PDF_ANNOT_LINK):
                    continue        # structural companions, not user marks
                ov = self._annot_overrides.get((page_index, annot.xref))
                if ov is not None and ov.deleted:
                    continue
                # ``annot.rect`` relates to the UNROTATED page (PyMuPDF
                # contract) -- the same derotated text space staged geometry
                # lives in, so one display_rect convention serves both.
                rect = annot.rect
                if ov is not None and (ov.dx or ov.dy):
                    rect = rect + (ov.dx, ov.dy, ov.dx, ov.dy)
                contents = annot.info.get("content", "")
                if ov is not None and ov.contents is not None:
                    contents = ov.contents
                records.append(AnnotRecord(
                    identity=(page_index, "xref", annot.xref),
                    kind=_KIND_BY_PDF_TYPE.get(
                        atype, (annot.type[1] or "annot").lower()),
                    display_rect=tuple(rect),
                    contents=contents, is_existing=True,
                    xref=annot.xref, spec=None))
        records.extend(
            AnnotRecord(identity=spec.identity, kind=spec.kind,
                        display_rect=self._annot_display_rect(spec),
                        contents=spec.contents, is_existing=False,
                        xref=None, spec=spec)
            for spec in sorted(self._annots.values(), key=lambda s: s.annot_id)
            if spec.page_index == page_index
        )
        self._annot_records_cache[page_index] = records
        return records

    @staticmethod
    def _annot_display_rect(spec: "AnnotSpec") -> tuple:
        """A spec's text-space bounding rect for hotspots / the panel jump."""
        if spec.quads:
            return (min(q[0] for q in spec.quads),
                    min(q[1] for q in spec.quads),
                    max(q[2] for q in spec.quads),
                    max(q[3] for q in spec.quads))
        if spec.rect is not None:
            return tuple(spec.rect)
        if spec.points:
            xs = [p[0] for s in spec.points for p in s]
            ys = [p[1] for s in spec.points for p in s]
            return (min(xs), min(ys), max(xs), max(ys))
        if spec.endpoints is not None:
            (x0, y0), (x1, y1) = spec.endpoints
            return (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))
        return (0.0, 0.0, 0.0, 0.0)

    def add_annot(self, page_index: int, **fields) -> "AnnotSpec":
        """Stage ONE annotation on ``page_index`` (one undo command, clears
        redo, dirties -- the ``stage_edit`` discipline). ``fields`` are the
        AnnotSpec fields (kind plus its geometry/style); geometry is text
        space. Returns the new spec."""
        kind = fields.get("kind")
        if (kind not in _MARKUP_KINDS and kind not in _SHAPE_KINDS
                and kind not in ("note", "ink")):
            raise ValueError(f"unsupported annotation kind: {kind!r}")
        if kind in _MARKUP_KINDS and not fields.get("quads"):
            raise ValueError(f"markup annot {kind!r} requires non-empty quads")
        if kind == "note" and fields.get("rect") is None:
            raise ValueError("note annot requires an anchor rect")
        if kind == "ink":
            points = fields.get("points")
            if not points or any(len(s) < 2 for s in points):
                raise ValueError(
                    "ink annot requires strokes of >= 2 points each")
        if kind in ("rect", "ellipse") and fields.get("rect") is None:
            raise ValueError(f"{kind} annot requires a rect")
        if kind in ("line", "arrow") and fields.get("endpoints") is None:
            raise ValueError(f"{kind} annot requires endpoints")
        spec = AnnotSpec(page_index=page_index,
                         annot_id=next(self._annot_id_counter), **fields)
        self._push_annot_command(spec.identity, "annot_add", None, spec,
                                 f"Add {kind}")
        return spec

    def delete_annot_box(self, page_index: int, ref) -> None:
        """Delete an annotation (identified by its identity tuple, its
        AnnotSpec, or an AnnotRecord). One undo command. A STAGED annot's
        spec leaves the map (undo restores it); an EXISTING file annot gets
        a ``deleted=True`` override applied to every fresh copy by xref --
        ``self.working`` stays untouched (§3.3)."""
        key = self._annot_key_for(ref)
        if len(key) == 3:
            cur = self._annots.get(key)
            if cur is None:
                raise ValueError(f"no staged annotation for {key!r}")
            self._push_annot_command(key, "annot_delete", cur, None,
                                     f"Delete {cur.kind}")
            return
        rec = self._existing_annot_record(key)
        if rec is None:
            raise ValueError(f"no file annotation for {key!r}")
        cur = self._annot_overrides.get(key)
        base = cur if cur is not None else AnnotOverride()
        self._push_annot_command(key, "annot_delete", cur,
                                 replace(base, deleted=True),
                                 "Delete annotation")

    def move_annot(self, page_index: int, ref, dx: float, dy: float) -> None:
        """Move an annotation by (dx, dy) text-space points: ONE undo
        command (§3.3). A STAGED spec's geometry is rebuilt translated (pure
        Python); an EXISTING file annot folds the delta into its override
        CUMULATIVELY (the move_box convention) -- notes only, every other
        existing kind raises (``set_rect`` does not relocate markup/ink
        vertices reliably, §8) and the view never offers it."""
        key = self._annot_key_for(ref)
        if len(key) == 3:
            cur = self._annots.get(key)
            if cur is None:
                raise ValueError(f"no staged annotation for {key!r}")
            self._push_annot_command(key, "annot_move", cur,
                                     self._translated_spec(cur, dx, dy),
                                     f"Move {cur.kind}")
            return
        rec = self._existing_annot_record(key)
        if rec is None:
            raise ValueError(f"no file annotation for {key!r}")
        if rec.kind != "note":
            raise ValueError(
                f"existing {rec.kind!r} annots cannot move (notes only)")
        cur = self._annot_overrides.get(key)
        base = cur if cur is not None else AnnotOverride()
        self._push_annot_command(key, "annot_move", cur,
                                 replace(base, dx=base.dx + dx,
                                         dy=base.dy + dy),
                                 "Move note")

    def modify_annot(self, page_index: int, ref, **fields) -> None:
        """Change an annotation's editable fields: ONE undo command (§3.3).
        STAGED specs accept stroke/fill/width/opacity/contents; EXISTING
        file annots accept ``contents`` ONLY (stored as an override -- never
        style or geometry of foreign annots, §8)."""
        key = self._annot_key_for(ref)
        if len(key) == 3:
            allowed = {"stroke", "fill", "width", "opacity", "contents"}
            bad = set(fields) - allowed
            if bad:
                raise ValueError(f"unsupported annot fields: {sorted(bad)}")
            cur = self._annots.get(key)
            if cur is None:
                raise ValueError(f"no staged annotation for {key!r}")
            label = ("Edit note" if set(fields) == {"contents"}
                     else f"Style {cur.kind}")
            self._push_annot_command(key, "annot_modify", cur,
                                     cur.with_changes(**fields), label)
            return
        if set(fields) != {"contents"}:
            raise ValueError(
                "existing annotations allow contents changes only")
        rec = self._existing_annot_record(key)
        if rec is None:
            raise ValueError(f"no file annotation for {key!r}")
        cur = self._annot_overrides.get(key)
        base = cur if cur is not None else AnnotOverride()
        self._push_annot_command(key, "annot_modify", cur,
                                 replace(base,
                                         contents=str(fields["contents"])),
                                 "Edit note")

    def set_annot_contents(self, page_index: int, ref, text: str) -> None:
        """``modify_annot`` sugar for the note popup + comments panel."""
        self.modify_annot(page_index, ref, contents=text)

    @staticmethod
    def _annot_key_for(ref) -> tuple:
        """Normalize an annot reference (AnnotSpec / AnnotRecord / identity
        tuple / (page, xref) pair) to its map key: the spec identity for a
        staged annot, ``(page, xref)`` for an existing file annot."""
        ident = getattr(ref, "identity", ref)
        if isinstance(ident, tuple):
            if len(ident) == 3 and ident[1] == "annot":
                return ident
            if len(ident) == 3 and ident[1] == "xref":
                return (ident[0], ident[2])
            if len(ident) == 2 and all(isinstance(v, int) for v in ident):
                return ident
        raise ValueError(f"not an annotation reference: {ref!r}")

    def _existing_annot_record(self, key: tuple) -> "AnnotRecord | None":
        """The live (override-applied) record for an existing-annot
        ``(page, xref)`` key, or None when the xref is unknown on that page
        / already override-deleted."""
        for rec in self.annotations(key[0]):
            if rec.is_existing and rec.xref == key[1]:
                return rec
        return None

    @staticmethod
    def _translated_spec(spec: "AnnotSpec", dx: float,
                         dy: float) -> "AnnotSpec":
        """``spec`` with every geometry field shifted by (dx, dy) -- one
        place that knows all four geometry shapes, so 'move' works for any
        staged kind."""
        changes: dict = {}
        if spec.quads is not None:
            changes["quads"] = tuple(
                (q[0] + dx, q[1] + dy, q[2] + dx, q[3] + dy)
                for q in spec.quads)
        if spec.rect is not None:
            changes["rect"] = (spec.rect[0] + dx, spec.rect[1] + dy,
                               spec.rect[2] + dx, spec.rect[3] + dy)
        if spec.points is not None:
            changes["points"] = tuple(
                tuple((p[0] + dx, p[1] + dy) for p in stroke)
                for stroke in spec.points)
        if spec.endpoints is not None:
            (x0, y0), (x1, y1) = spec.endpoints
            changes["endpoints"] = ((x0 + dx, y0 + dy), (x1 + dx, y1 + dy))
        return spec.with_changes(**changes)

    def _install_annot_state(self, key: tuple, state) -> None:
        """Write one annot key's complete staged state back (None removes).
        The single choke point every annot mutation AND undo/redo replay
        passes through, so it owns dropping the page's records memo."""
        self._annot_records_cache.pop(key[0], None)
        if len(key) == 3 and key[1] == "annot":
            if state is None:
                self._annots.pop(key, None)
            else:
                self._annots[key] = state
        else:                            # (page, xref): existing-annot override
            if state is None:
                self._annot_overrides.pop(key, None)
            else:
                self._annot_overrides[key] = state

    def _push_annot_command(self, key: tuple, kind: str, before, after,
                            label: str) -> None:
        """Install ``after`` and record the command on the SAME history list
        as text/box commands (clearing redo, dirtying)."""
        self._install_annot_state(key, after)
        # Same clean-index invalidation as _push_command: a push below the
        # last-save depth makes the saved state unreachable.
        if self._saved_undo_depth > len(self._undo):
            self._saved_undo_depth = -1
        self._undo.append(_AnnotCommand(key=key, kind=kind, before=before,
                                        after=after, label=label))
        self._redo.clear()
        self._dirty = True

    def _page_has_annots(self, page_index: int) -> bool:
        """True when this page has staged annots OR overrides -- the bake /
        fast-path guard (annotations & markup §3.4)."""
        return (any(s.page_index == page_index for s in self._annots.values())
                or any(k[0] == page_index for k in self._annot_overrides))

    # --- forms: AcroForm detect / enumerate / fill (forms §2) -------------
    @property
    def has_form(self) -> bool:
        """True when the working doc carries an AcroForm with fields.
        ``is_form_pdf`` returns a truthy field COUNT (or False), so wrap."""
        return bool(self.working.is_form_pdf)

    @property
    def form_field_count(self) -> int:
        """The AcroForm field count (the status-badge number)."""
        return int(self.working.is_form_pdf or 0)

    def form_fields(self, page_index: int) -> list["FormField"]:
        """One ``FormField`` PER WIDGET on ``page_index`` (radio kids are
        separate entries sharing a ``group_key``), in ``page.widgets()``
        enumeration order. Memoized like ``spans()`` -- baselines depend only
        on ``self.working``, so the memo clears with the working doc
        (``_invalidate_caches``); staged values never affect it. The returned
        list is the cached object; callers treat it read-only."""
        cached = self._form_fields_cache.get(page_index)
        if cached is not None:
            return cached
        # Radio group baseline: the on-state of the kid whose field_value is
        # not "Off" (the selected kid reads its own on-state, probe §0), else
        # "Off". Resolved over the whole group before building the kids.
        # ``page`` must stay referenced while the widgets are read: a Widget
        # holds only a weakref to its parent page (on_state() dies otherwise).
        page = self.working[page_index]
        radio_value: dict[str, str] = {}
        widgets = list(page.widgets())
        for w in widgets:
            if w.field_type == fitz.PDF_WIDGET_TYPE_RADIOBUTTON:
                radio_value.setdefault(w.field_name, "Off")
                if w.field_value not in (None, "", "Off"):
                    radio_value[w.field_name] = w.field_value
        out: list[FormField] = []
        for w in widgets:
            kind = _WIDGET_KIND_BY_TYPE.get(w.field_type, "button")
            flags = int(w.field_flags or 0)
            if kind == "checkbox":
                value: object = (w.field_value != "Off")
            elif kind == "radio":
                value = radio_value[w.field_name]
            else:
                value = w.field_value or ""
            out.append(FormField(
                page_index=page_index,
                name=w.field_name,
                kind=kind,
                rect=tuple(w.rect),
                xref=w.xref,
                value=value,
                on_state=(w.on_state()
                          if kind in ("checkbox", "radio") else None),
                options=tuple(w.choice_values or ()),
                flags=flags,
                multiline=(kind == "text"
                           and bool(flags & fitz.PDF_TX_FIELD_IS_MULTILINE)),
                readonly=bool(flags & fitz.PDF_FIELD_IS_READ_ONLY),
                text_fontsize=float(w.text_fontsize or 0.0),
                max_len=int(w.text_maxlen or 0),
            ))
        self._form_fields_cache[page_index] = out
        return out

    def effective_form_value(self, page_index: int,
                             field: "FormField") -> object:
        """The field's CURRENT value: the staged one, else the baseline."""
        return self._form_edits.get(field.group_key, field.value)

    def form_tab_order(self) -> list["FormField"]:
        """Every FILLABLE widget, all pages in order, fields in widget
        enumeration order (what Acrobat does absent an explicit /Tabs, §8).
        Readonly fields and button/signature/listbox kinds are excluded."""
        order: list[FormField] = []
        for pi in range(self.page_count):
            for f in self.form_fields(pi):
                if f.readonly or f.kind not in _FILLABLE_FORM_KINDS:
                    continue
                order.append(f)
        return order

    def stage_form_value(self, page_index: int, field: "FormField",
                         value) -> None:
        """Record/replace the staged value for ``field``'s group as ONE
        command on the shared history (forms §2). Normalization: text -> str
        (truncated to ``max_len`` when set), checkbox -> bool, radio/combo ->
        str (a combo value must be one of ``field.options``). Staging the
        BASELINE value back drops the entry (mirrors the no-op Edit drop), so
        a field reverted to source is unstaged and the pristine render
        signature returns."""
        if field.readonly or field.kind not in _FILLABLE_FORM_KINDS:
            raise ValueError(
                f"form field {field.name!r} ({field.kind}"
                f"{', readonly' if field.readonly else ''}) is not fillable")
        if field.kind == "text":
            value = str(value)
            if field.max_len > 0:
                value = value[:field.max_len]
        elif field.kind == "checkbox":
            value = bool(value)
        else:                            # radio | combo
            value = str(value)
            if field.kind == "combo" and value not in field.options:
                raise ValueError(
                    f"{value!r} is not an option of combo field "
                    f"{field.name!r}: {list(field.options)}")
        key = field.group_key
        staged = (self._form_edits[key] if key in self._form_edits
                  else _FORM_UNSET)
        before = _BoxState(form_value=staged)
        after = _BoxState(
            form_value=_FORM_UNSET if value == field.value else value)
        if after.form_value is staged or (
                staged is not _FORM_UNSET
                and after.form_value is not _FORM_UNSET
                and after.form_value == staged):
            return                       # nothing actually changes
        if field.kind == "checkbox":
            label = ("Check" if value else "Uncheck") + f" ‘{field.name}’"
        elif field.kind in ("radio", "combo"):
            label = f"Choose ‘{self._truncate(str(value))}’"
        else:
            label = f"Fill ‘{field.name}’"
        self._push_command(key, field, "form", before, after, label)

    def _page_has_form_edits(self, page_index: int) -> bool:
        """True when this page has staged form values -- the render
        fast-path guard (forms §2)."""
        return any(k[0] == page_index for k in self._form_edits)

    def _apply_form_values(self, target: "fitz.Document",
                           only_page: int | None = None) -> None:
        """Write every staged form value into ``target``'s widgets and
        regenerate their appearances (``widget.update()``) -- the ONE helper
        every pipeline consumer calls (``_baked_copy``, ``render_with_edits``,
        ``_bake_pending_edits``, extract/split/merge). Pure MuPDF, no Qt, and
        probe-proven safe before or after redactions (§0): redacting a rect
        over a widget leaves the widget and its value intact.

        Matching is by ``field_name`` (same-name widgets are one PDF field);
        a radio value selects only the kid whose ``on_state()`` equals it
        (MuPDF flips the siblings' /AS to /Off itself)."""
        if not self._form_edits:
            return
        by_page: dict[int, dict[str, object]] = {}
        for (pi, _tag, name), value in self._form_edits.items():
            if only_page is not None and pi != only_page:
                continue
            by_page.setdefault(pi, {})[name] = value
        for pi, values in by_page.items():
            for w in target[pi].widgets():
                if w.field_name not in values:
                    continue
                value = values[w.field_name]
                if w.field_type == fitz.PDF_WIDGET_TYPE_RADIOBUTTON:
                    if w.on_state() != value:
                        continue        # only the matching kid is set
                w.field_value = value
                w.update()

    # --- generalized undo / redo ----------------------------------------
    def undo(self) -> tuple[int, object] | None:
        """Revert the most recent mutation. Returns ``(page_index, box_ref)``
        where box_ref is a Span (existing) or NewBox (new), so the UI repaints
        exactly the affected box (BUILD_SPEC §1.4). None when the stack is
        empty."""
        if not self._undo:
            return None
        cmd = self._undo.pop()
        if isinstance(cmd, _AnnotCommand):
            # Annot entries ride the same list (annotations & markup §3.3):
            # install the BEFORE state and return (page, identity) so the
            # mixed text+annot history keeps the Qt/model lockstep intact.
            self._install_annot_state(cmd.key, cmd.before)
            self._redo.append(cmd)
            self._dirty = len(self._undo) != self._saved_undo_depth
            return (cmd.key[0], cmd.key)
        self._install_state(cmd.edit_key, cmd.span, cmd.before)
        self._redo.append(cmd)
        # Dirty truth (text-editing UX §2.7c): staged-dirty is "history depth
        # differs from the LAST-SAVE baseline" (the model clean index), so
        # undoing back to the saved state reads clean (Save disables, the Qt
        # stack reaches its clean index in lockstep) while undoing PAST a save
        # reads dirty -- the screen no longer matches the disk even though the
        # census against the opened state is zero. Structural-bake dirtiness
        # is carried by the depth term in ``dirty``.
        self._dirty = len(self._undo) != self._saved_undo_depth
        return self._command_box_ref(cmd)

    def redo(self) -> tuple[int, object] | None:
        """Re-apply the most recently undone mutation. Returns ``(page_index,
        box_ref)`` or None when the redo stack is empty."""
        if not self._redo:
            return None
        cmd = self._redo.pop()
        if isinstance(cmd, _AnnotCommand):
            self._install_annot_state(cmd.key, cmd.after)
            self._undo.append(cmd)
            self._dirty = len(self._undo) != self._saved_undo_depth
            return (cmd.key[0], cmd.key)
        self._install_state(cmd.edit_key, cmd.span, cmd.after)
        self._undo.append(cmd)
        # Same baseline-derived dirty as undo() (§2.7c): redoing back UP to
        # the last-save depth reads clean again; anywhere else reads dirty.
        self._dirty = len(self._undo) != self._saved_undo_depth
        return self._command_box_ref(cmd)

    def _command_box_ref(self, cmd: _Command) -> tuple[int, object]:
        """The (page_index, box_ref) a command affects for the UI to repaint."""
        page = cmd.edit_key[0]
        if cmd.edit_key[1] == "new":
            box = self._new_boxes.get(cmd.edit_key)
            if box is None and cmd.after.newbox is not None:
                box = cmd.after.newbox          # an undone 'add': the removed box
            elif box is None and cmd.before.newbox is not None:
                box = cmd.before.newbox
            return page, box
        if cmd.edit_key[1] == "img":
            box = self._images.get(cmd.edit_key)
            if box is None and cmd.after.imagebox is not None:
                box = cmd.after.imagebox        # an undone 'add': the removed box
            elif box is None and cmd.before.imagebox is not None:
                box = cmd.before.imagebox
            return page, box
        if cmd.edit_key[1] == "xim":
            # The ExistingImage record itself (frozen, never restaged): the
            # UI repaints its page whether the delete was staged or undone.
            box = cmd.after.xim or cmd.before.xim
            return page, box
        # "form" keys carry the FormField in the span slot (forms §2), so the
        # shared return below hands the UI a ref with .page_index/.identity.
        return page, cmd.span

    def history_depth(self) -> int:
        """Current length of the universal model-history stack (``_undo``).
        ``_ModelCommand.redo`` in ui/main_window.py reads this to tell whether a
        replayed command actually pushed onto the model."""
        return len(self._undo)

    @property
    def can_undo(self) -> bool:
        return bool(self._undo)

    @property
    def can_redo(self) -> bool:
        return bool(self._redo)

    # --- state reads -----------------------------------------------------
    def staged_text(self, page_index: int, box) -> str:
        """Current staged text for a box (BUILD_SPEC §1.9): the new box's text,
        or an existing span's staged/original text."""
        if isinstance(box, NewBox):
            cur = self._new_boxes.get(box.edit_key)
            return cur.text if cur is not None else box.text
        edit = self._edits.get(self._span_edit_key(page_index, box))
        return edit.effective_text(box) if edit is not None else box.text

    def editor_line_layout(self, page_index: int, box) -> list | None:
        """The bake's exact per-line layout for a PARAGRAPH box, as
        ``[(text, baked_width_pt), ...]`` -- so the inline editor can reproduce
        the page's OWN line breaks AND scale each line to the page's OWN width,
        making the editor a pixel overlay of the rendered text (no Qt-vs-fitz
        wrap or width drift). ``baked_width_pt`` is the line's advance the page
        was drawn with. Returns None for a non-paragraph box (single lines
        never wrap and need no break/width matching)."""
        if not getattr(box, "is_paragraph", False):
            return None
        if isinstance(box, NewBox):
            # A paragraph NewBox (OCR area) is not a rawdict span: it has no
            # ``.key`` / ``_edits`` entry and no member layout to mirror. Returning
            # None routes the editor to its SOFT-WRAP path (wrap to effective_bbox),
            # which edits the block fine; the authoritative reflow happens in the
            # bake's paragraph raster on commit.
            return None
        edit = self._edits.get(self._span_edit_key(page_index, box))
        e = edit if edit is not None else Edit(span=box)
        result = self._wrap_for_edit_cached(page_index, box, e)
        out = []
        for ln in result.lines:
            if hasattr(ln, "segments"):
                text = "".join(seg.text for seg in ln.segments)
            else:
                text = getattr(ln, "text", "")
            out.append((text, float(getattr(ln, "width", 0.0))))
        return out

    def effective_style(self, page: int, box) -> dict:
        """The box's CURRENT resolved style for the inspector to read (BUILD_SPEC
        §1.9): ``{"font_family", "size", "color", "bold", "italic"}``. For an
        existing span with no override this reports the ORIGINAL family (a
        display name from the resolver) so the inspector shows the truth."""
        if isinstance(box, NewBox):
            cur = self._new_boxes.get(box.edit_key) or box
            # A scanned box's REAL weight/slant is unknown at OCR time (bold/italic seed
            # False); detect it from the edge-matched face on first read (no-op once
            # seeded) so the format bar shows whether the scan is bold/italic.
            if cur.cover and len(cur.cover) == 7 and not cur.style_seeded:
                cur = self.seed_scan_style(cur)
            return {"font_family": cur.font_family, "size": cur.size,
                    "color": cur.color, "bold": cur.bold, "italic": cur.italic}
        edit = self._edits.get(self._span_edit_key(page, box))
        style = edit.style if edit is not None else StyleOverride()
        from .fonts import is_bold, is_italic
        orig_bold = is_bold(box.font, box.flags)
        orig_italic = is_italic(box.font, box.flags)
        bold = style.bold if style.bold is not None else orig_bold
        italic = style.italic if style.italic is not None else orig_italic
        # UNIFORMLY-styled rich runs (e.g. the user bolded the whole text from
        # inside the editor) are the truth for B/I -- report them so the
        # inspector buttons and the editor's base font match the staged ink.
        if edit is not None and edit.runs:
            run_styles = {(r[1], r[2]) for r in edit.runs}   # index: runs may carry a 4th RunStyle
            if len(run_styles) == 1:
                bold, italic = next(iter(run_styles))
        if style.font_family is not None:
            family = style.font_family
        else:
            rf = self.font_engine.resolve(page, box.font, box.flags, box.text)
            family = rf.qt_family
        size = style.size if style.size is not None else box.size
        if edit is not None and abs(edit.scale - 1.0) > 1e-9:
            size = size * edit.scale
        color = style.color if style.color is not None else box.color
        result = {"font_family": family, "size": size, "color": color,
                  "bold": bold, "italic": italic}
        # A ParagraphBox also reports its layout (alignment + line spacing) so the
        # Format panel can seed the alignment / line-spacing controls (§R5.2).
        if getattr(box, "is_paragraph", False):
            align = box.alignment
            spacing = 1.0
            if edit is not None:
                if edit.alignment is not None:
                    align = edit.alignment
                if edit.line_spacing is not None:
                    spacing = edit.line_spacing
            result["alignment"] = align
            result["line_spacing"] = spacing
        return result

    def style_near(self, page_index: int, point: tuple,
                   *, max_radius: float | None = None) -> dict | None:
        """Auto-match-nearby-style (REFLOW_SPEC §R5.4): the effective add-style of
        the existing text box (span / paragraph / new box) whose ink is NEAREST
        ``point`` on ``page_index``, or ``None`` when nothing is close enough.

        Used by the Add-Text tool so a box dropped inside a paragraph column
        inherits that text's font / size / color / bold / italic instead of a
        hardcoded default; a box dropped in blank space (no neighbor within the
        radius) returns ``None`` and the caller falls back to the inspector
        defaults. The returned dict carries ONLY the add-relevant style keys
        (``font_family``, ``size``, ``color``, ``bold``, ``italic``) -- paragraph
        layout (alignment / spacing) is deliberately omitted, since a fresh
        single-line box is not a paragraph.

        "Nearest" is the smallest distance from ``point`` to the box's bbox (zero
        when inside it). The default radius is generous (~2.5 line-heights of the
        nearest candidate's own size, floored at a few points) so a click on the
        blank baseline row just under a paragraph still matches it, while a click
        far out in white space does not. Pass ``max_radius`` to override.
        """
        if not (0 <= page_index < self.page_count):
            return None
        px, py = float(point[0]), float(point[1])
        best = None
        best_d = math.inf
        for box in (*self.spans(page_index), *self.new_boxes(page_index)):
            bx0, by0, bx1, by1 = self.effective_bbox(page_index, box)
            # Distance from the point to the box rect (0 when inside it).
            dx = max(bx0 - px, 0.0, px - bx1)
            dy = max(by0 - py, 0.0, py - by1)
            d = math.hypot(dx, dy)
            if d < best_d:
                best_d, best = d, box
        if best is None:
            return None
        radius = max_radius
        if radius is None:
            size = float(getattr(best, "size", 12.0) or 12.0)
            radius = max(2.5 * size, 12.0)
        if best_d > radius:
            return None
        style = self.effective_style(page_index, best)
        # Strip to the add-relevant keys (drop any paragraph layout fields).
        return {k: style[k] for k in
                ("font_family", "size", "color", "bold", "italic")
                if k in style}

    # =====================================================================
    # Find & Replace (REFLOW_SPEC §R5.1) -- a pure model query; the panel
    # stages every replacement through the SAME edit pipeline (stage_edit /
    # EditRunCommand) so a replace reflows + stays WYSIWYG + is one undo step.
    # =====================================================================
    def find_all(self, query: str, *, match_case: bool = False,
                 whole_word: bool = False) -> list["Match"]:
        """Every occurrence of ``query`` across ALL pages, in reading order
        (REFLOW_SPEC §R5.1). Searches the CURRENT staged text of every box --
        ``spans(page)`` (which now yields ``ParagraphBox``es) plus
        ``new_boxes(page)`` -- so a match can span a paragraph's original soft
        line breaks (the joined paragraph string is searched as one run), and
        already-staged edits are seen. Returns ``Match`` records keyed by the
        box's stable ``identity`` so the panel survives reloads.

        ``match_case`` toggles case sensitivity (default: insensitive).
        ``whole_word`` requires word boundaries on both sides. Overlapping
        matches are not returned (the scan advances past each hit, like a normal
        editor's Find)."""
        if not query:
            return []
        matches: list[Match] = []
        flags = 0 if match_case else re.IGNORECASE
        # Normalize space variants on the QUERY so a query typed with an ASCII
        # space matches text whose source used NBSP / thin space / etc.
        pat = re.escape(_normalize_spaces(query))
        if whole_word:
            pat = r"\b" + pat + r"\b"
        try:
            rx = re.compile(pat, flags)
        except re.error:
            return []
        for page in range(self.page_count):
            for box in (*self.spans(page), *self.new_boxes(page)):
                hay = self.staged_text(page, box)
                if not hay:
                    continue
                # Search a space-normalized COPY of the haystack so a single-line
                # Span with an NBSP gap is searchable the same as a grouped
                # ParagraphBox (REFLOW_SPEC §R5.1). The normalization is length-
                # preserving (each variant is one code point -> one space), so the
                # match offsets index the ORIGINAL ``hay`` 1:1 -- the staged
                # replacement (replaced_text / stage_edit) still targets the right
                # characters of the real text.
                norm = _normalize_spaces(hay)
                for m in rx.finditer(norm):
                    s, e = m.start(), m.end()
                    if s == e:           # never match an empty span
                        continue
                    matches.append(Match(
                        page_index=page,
                        box_identity=box.identity,
                        start=s,
                        end=e,
                        text=hay[s:e],
                        context=self._match_context(hay, s, e),
                        box=box,
                    ))
        return matches

    @staticmethod
    def _match_context(hay: str, start: int, end: int, pad: int = 24) -> str:
        """A one-line snippet around ``[start, end)`` for the results list."""
        lo = max(0, start - pad)
        hi = min(len(hay), end + pad)
        snippet = hay[lo:hi].replace("\n", " ")
        if lo > 0:
            snippet = "…" + snippet
        if hi < len(hay):
            snippet = snippet + "…"
        return snippet

    def replaced_text(self, page_index: int, box, start: int, end: int,
                      replacement: str) -> str:
        """The box's CURRENT staged text with ``[start, end)`` swapped for
        ``replacement`` (REFLOW_SPEC §R5.1). The panel feeds this to the normal
        text-edit command (``stage_edit`` via ``EditRunCommand``) so the replace
        rewraps paragraphs through the bake and undoes as one step. Returns the
        original text unchanged when the span is out of range."""
        hay = self.staged_text(page_index, box)
        if not (0 <= start <= end <= len(hay)):
            return hay
        return hay[:start] + replacement + hay[end:]

    @staticmethod
    def _truncate(text: str, n: int = 24) -> str:
        text = (text or "").replace("\n", " ")
        return text if len(text) <= n else text[: n - 1] + "…"

    def _text_label(self, old_text: str, new_text: str) -> str:
        return (f"Edit ‘{self._truncate(old_text)}’ → "
                f"‘{self._truncate(new_text)}’")

    @staticmethod
    def _fmt_style(delta: StyleOverride) -> str:
        parts = []
        if delta.font_family is not None:
            parts.append(delta.font_family)
        if delta.size is not None:
            parts.append(f"{delta.size:g}pt")
        if delta.color is not None:
            parts.append("color")
        if delta.bold is not None:
            parts.append("bold" if delta.bold else "regular")
        if delta.italic is not None:
            parts.append("italic" if delta.italic else "upright")
        return ", ".join(parts) or "change"

    @property
    def has_edits(self) -> bool:
        return (bool(self._edits) or bool(self.new_boxes_all())
                or bool(self._images) or bool(self._xim_deletes)
                or bool(self._annots) or bool(self._annot_overrides)
                or bool(self._form_edits))

    def new_boxes_all(self) -> list["NewBox"]:
        """Every non-deleted NewBox across all pages (for edit_count / dirty)."""
        return [b for b in self._new_boxes.values() if not b.deleted]

    @property
    def edit_count(self) -> int:
        """Count of non-noop existing-span Edits PLUS non-deleted NewBoxes
        (BUILD_SPEC §1.9) PLUS non-deleted placed images + staged
        existing-image deletions PLUS staged annotations + existing-annot
        overrides PLUS staged form values (the single staged-state census the
        Save button and the dirty derivation read -- cross-workstream
        contract)."""
        span_edits = sum(1 for e in self._edits.values() if not e.is_noop)
        return (span_edits + len(self.new_boxes_all())
                + len(self.image_boxes_all()) + len(self._xim_deletes)
                + len(self._annots) + len(self._annot_overrides)
                + len(self._form_edits))

    @property
    def structural_depth(self) -> int:
        """Number of committed structural snapshots on the undo stack — i.e. how
        many forward page-ops are currently reachable by ``undo_structural``."""
        return len(self._struct_undo)

    @property
    def dirty(self) -> bool:
        """True while there are unsaved changes: staged fine-grained edits
        (``_dirty``), a structural depth that differs from the saved baseline,
        OR pending security changes (``set_security`` -- save options count as
        unsaved work so the Save button enables). Set by any edit/undo/redo
        that changes state and cleared by ``mark_clean`` after a successful
        save. Undoing every structural op back to the saved baseline reads
        clean again (the depth matches), so the tab's dirty marker clears
        instead of sticking forever (PAGES_SPEC §6.8)."""
        return (self._dirty or self._security_dirty
                or self.structural_depth != self._saved_struct_depth)

    def mark_clean(self) -> None:
        """Mark the current edit + structural state as saved; subsequent edits or
        page-ops re-dirty. Records the current structural depth as the clean
        baseline so a later undo back to here reads clean again. Pending
        security changes are now ON DISK (``_save_kwargs`` applied them), so
        the security-dirty flag clears too -- the options themselves persist
        for the next save. Also records the fine-grained history depth and
        whether staged state existed (the model-side clean index), so undoing
        a SAVED edit reads dirty -- screen != disk -- instead of conflating
        "back to the opened state" with "matches the last save"."""
        self._dirty = False
        self._security_dirty = False
        self._saved_struct_depth = self.structural_depth
        self._saved_undo_depth = len(self._undo)
        self._saved_had_staged = self.has_edits

    def is_edit_unsaved(self, page_index: int, box) -> bool:
        """True when ``box`` carries an edit made SINCE the last save -- the
        in-place edit signature shows only for UNSAVED changes and clears once
        saved (the edit itself persists, it just stops being flagged).

        An edit is on disk iff its undo command sits at or below the saved
        baseline (``_saved_undo_depth``); commands above it are pending. A
        ``-1`` baseline means the saved state is no longer reachable (undo/redo
        crossed it), so every currently-applied edit reads as unsaved."""
        depth = self._saved_undo_depth
        if depth < 0:
            depth = 0
        unsaved = self._undo[depth:]
        if not unsaved:
            return False
        key = (box.edit_key if isinstance(box, NewBox)
               else self._span_edit_key(page_index, box))
        return any(cmd.edit_key == key for cmd in unsaved)

    # --- document info / metadata (doc-tools M1) ---------------------------
    def metadata_fields(self) -> dict:
        """The four user-editable Description fields (Properties dialog), read
        from ``self.working`` -- the constructor carry seeded them from the
        original file, so they reflect what the next save writes."""
        md = self.working.metadata or {}
        return {k: md.get(k) or "" for k in _EDITABLE_META_KEYS}

    def set_metadata_fields(self, fields: dict) -> None:
        """Set editable Description fields as ONE structural op: undo is one
        ``StructuralCommand`` (the pre-op bytes snapshot restores the old info
        dict). Only the four editable keys are accepted -- format/encryption
        are derived, creator/producer/dates belong to the producing software --
        anything else raises ``ValueError`` before the snapshot is taken."""
        unknown = set(fields) - set(_EDITABLE_META_KEYS)
        if unknown:
            raise ValueError(
                f"not an editable metadata field: {sorted(unknown)}")
        md = self.working.metadata or {}
        merged = {k: md.get(k) or "" for k in _META_KEYS}
        merged.update({k: fields[k] or "" for k in fields})
        self._begin_structural()
        self.working.set_metadata(merged)
        self._finish_structural()

    # --- bookmarks / outline (navigation M1) -------------------------------
    def outline(self) -> list[list]:
        """The document outline as ``[[level, title, page1based], ...]`` --
        a FRESH list per call, read straight off ``self.working`` (the
        constructor's TOC carry seeded it from the source file, so it reflects
        what the next save writes). ``get_toc`` is cheap at this scale; no
        caching. Entries whose target page was deleted carry ``page == -1``
        (fitz keeps them dangling rather than dropping them -- so do we;
        Acrobat keeps dead bookmarks too)."""
        return self.working.get_toc()

    def set_outline(self, entries: list[list]) -> None:
        """Replace the outline with ``entries`` as ONE structural op (undo is
        one snapshot, exactly like ``rotate_page``): ``set_toc`` mutates
        ``working``, and only structural ops and bakes may do that, so it rides
        the shared funnel rather than inventing a third undo system. Cost: a
        bookmark edit bakes pending text edits and collapses fine-grained Qt
        history (the documented behavior of EVERY structural op) -- accepted;
        bookmark edits are occasional ops.

        Validation runs BEFORE the snapshot (the guard-before-snapshot
        pre-pass every structural op follows): each entry is
        ``[level, title, page]`` with a non-empty title (coerced to str), an
        int page (``-1`` = dangling is legal), the first level 1 and each
        level in ``1..prev+1`` (the ``set_toc`` hierarchy rule, surfaced here
        so a bad tree never reaches the doc). An empty list clears the
        outline."""
        cleaned: list[list] = []
        prev_level = 0
        for i, entry in enumerate(entries or []):
            try:
                level, title, page = entry[0], entry[1], entry[2]
            except (TypeError, IndexError, KeyError):
                raise ValueError(
                    f"outline entry {i} must be [level, title, page]")
            if not isinstance(level, int) or isinstance(level, bool):
                raise ValueError(f"outline entry {i}: level must be an int")
            if i == 0 and level != 1:
                raise ValueError("hierarchy level of item 0 must be 1")
            if not 1 <= level <= prev_level + 1:
                raise ValueError(
                    f"outline entry {i}: level {level} not in "
                    f"1..{prev_level + 1}")
            title = str(title if title is not None else "").strip()
            if not title:
                raise ValueError(f"outline entry {i}: title is empty")
            if not isinstance(page, int) or isinstance(page, bool):
                raise ValueError(f"outline entry {i}: page must be an int")
            cleaned.append([level, title, page])
            prev_level = level
        self._begin_structural()
        self.working.set_toc(cleaned)
        self._finish_structural()

    def fonts_used(self) -> list[tuple[str, str, bool]]:
        """``(basefont, type, embedded)`` for every unique face any page
        references, deduped on basefont. ``embedded`` is ``ext != "n/a"``
        (probe-verified: an embedded TrueType reports e.g. ``ttf``/``Type0``;
        a base-14 built-in reports ext ``n/a``)."""
        seen: dict[str, tuple[str, str, bool]] = {}
        for i in range(self.page_count):
            # (xref, ext, type, basefont, refname, encoding) per reference.
            for entry in self.working.get_page_fonts(i):
                ext, ftype, base = entry[1], entry[2], entry[3] or ""
                if base and base not in seen:
                    seen[base] = (base, ftype, ext != "n/a")
        return sorted(seen.values())

    def properties(self) -> dict:
        """Read-only aggregate for the Properties dialog: file facts from
        ``self.path`` on disk, page facts + metadata from ``self.working`` (so
        structural changes are reflected live), faces via ``fonts_used``."""
        try:
            file_size = os.path.getsize(self.path)
        except OSError:
            file_size = 0
        md = self.working.metadata or {}
        return {
            "path": self.path,
            "file_size": file_size,
            "page_count": self.page_count,
            "page_sizes": doctools.unique_page_sizes(
                (self.working[i].rect.width, self.working[i].rect.height)
                for i in range(self.page_count)),
            "pdf_version": getattr(self, "pdf_version", "") or "",
            "encrypted": bool(getattr(self, "original_encrypted", False)),
            "metadata": {k: md.get(k) or "" for k in _META_KEYS},
            "fonts": self.fonts_used(),
        }

    # --- security: password protection (doc-tools M4) ---------------------
    def set_security(self, **kw) -> None:
        """Update the pending save-time security options -- any of
        ``encryption`` / ``user_pw`` / ``owner_pw`` / ``permissions``, the
        ``Document.save`` encryption kwargs. NOT a structural op: nothing in
        ``working`` changes, so there is no undo entry; the Security dialog
        is the revert surface. Flips the security-dirty flag (``dirty`` ORs
        it) so Save enables; ``mark_clean`` clears the flag after the save
        while the options persist for subsequent saves. Unknown keys raise
        ``ValueError`` before any state changes."""
        unknown = set(kw) - set(_SECURITY_KEYS)
        if unknown:
            raise ValueError(f"not a security option: {sorted(unknown)}")
        self._security.update(kw)
        self._security_dirty = True

    @property
    def encrypts_on_save(self) -> bool:
        """Whether the next save writes an encrypted file. Drives the
        Security dialog's status line and ``reopen_password``."""
        return self._security["encryption"] != PDF_ENCRYPT_NONE

    @property
    def reopen_password(self) -> str | None:
        """What the window must pass when re-opening a file THIS document
        just saved (``_reload_after_save``): the pending user password, else
        the password the document was opened with; ``None`` when the save is
        unencrypted. Without it, saving an encrypted file failed the reload
        (the fresh constructor raised ``PasswordRequired``) and silently kept
        the stale pre-save model."""
        if not self.encrypts_on_save:
            return None
        return self._security["user_pw"] or self._open_password

    def _save_kwargs(self) -> dict:
        """The encryption kwargs every full-document writer passes to
        ``Document.save`` (``save_as`` and ``save_optimized_copy``): the
        pending ``_security`` state, with the owner password defaulting to
        the user password so one password controls both (the dialog sets
        them together; granular permissions are de-scoped to all-granted)."""
        if not self.encrypts_on_save:
            return {"encryption": PDF_ENCRYPT_NONE}
        sec = self._security
        return {"encryption": sec["encryption"],
                "user_pw": sec["user_pw"] or "",
                "owner_pw": sec["owner_pw"] or sec["user_pw"] or "",
                "permissions": int(sec["permissions"])}

    # --- persistence -----------------------------------------------------
    def _baked_copy(self) -> "fitz.Document":
        """A FRESH ``fitz.Document`` copy of ``self.working`` with every staged
        edit baked in through the one shared pipeline (``_apply_page_edits``)
        -- the same pipeline ``render_with_edits`` draws from, so anything
        written from this copy matches the screen by construction (doc-tools
        §1). ``save_as`` / ``export_text`` / ``save_optimized_copy`` /
        ``save_flattened`` all funnel through here, and staged form values
        land on the copy first (forms §2); keep it the single seam.

        Resolves through the LONG-LIVED ``self.font_engine``, never a cold
        ``FontEngine(out)``: ``out`` is a byte-identical copy of ``working``
        and resolve/resolve_family/fitz_font_for only READ their bound doc, so
        resolving against ``working`` gives the same answers while keeping the
        resolve/xref caches warm across saves and renders (perf foundation
        M1a). ``_invalidate_caches`` rebinds the engine on every working-doc
        mutation, so it can never go stale.

        The CALLER owns the returned document and must close it. Requires a
        constructed ``QGuiApplication`` (the font resolver uses Qt); raises
        ``RuntimeError`` with a clear message rather than letting
        ``QFontDatabase`` abort the process with a SIGABRT."""
        from PySide6.QtGui import QGuiApplication
        if QGuiApplication.instance() is None:
            raise RuntimeError(
                "PDFDocument._baked_copy requires a QGuiApplication: the font "
                "resolver uses QFont/QFontInfo/QFontDatabase. Construct a "
                "QApplication before saving or exporting (only the edit-state "
                "surface is Qt-free)."
            )
        out = fitz.open("pdf", self.working.tobytes())
        try:
            # Staged form values FIRST, before the per-page edit loop (forms
            # §2): the ONE seam, so save_as / export_text / optimize /
            # save_flattened all get fills for free. Probe-proven safe
            # against the redactions below (§0).
            self._apply_form_values(out)
            engine = self.font_engine

            by_page: dict[int, list[Edit]] = {}
            for (page_index, _), edit in self._edits.items():
                if edit.is_noop:
                    continue
                by_page.setdefault(page_index, []).append(edit)
            # Pages that only have NEW boxes (no span edits) must still be
            # processed so their added text is drawn.
            for box in self.new_boxes_all():
                by_page.setdefault(box.page_index, [])
            # ... pages with only placed images (images & signatures §2.5)...
            for box in self.image_boxes_all():
                by_page.setdefault(box.page_index, [])
            # ... pages with only staged existing-image deletions (M3) ...
            for x in self._xim_deletes.values():
                by_page.setdefault(x.page_index, [])
            # ... and pages with only staged annots / annot overrides, so the
            # pipeline tail (_apply_page_annots) runs for them too (§3.4).
            for spec in self._annots.values():
                by_page.setdefault(spec.page_index, [])
            for (pi, _xref) in self._annot_overrides:
                by_page.setdefault(pi, [])

            for page_index, edits in by_page.items():
                self._apply_page_edits(out, engine, page_index, edits)
            # Persist the editable OCR layer INSIDE the file so reopening restores
            # the exact fresh-OCR edit state (scan-preserving covers + original
            # text + per-glyph fonts) instead of the generic existing-text editor.
            try:
                from .ocr import layer_io
                _blob = layer_io.serialize_layer(self)
                if _blob:
                    # A reopened file already contains the embedded layer, so a
                    # plain embfile_add raises ValueError('...already exists') and
                    # the stale copy survives -- update in place when present.
                    if layer_io.EMB_NAME in out.embfile_names():
                        out.embfile_upd(layer_io.EMB_NAME, _blob)
                    else:
                        out.embfile_add(layer_io.EMB_NAME, _blob)
            except Exception as _e:
                dlog("OCR", "embed_layer_failed", err=repr(_e))
        except Exception:
            out.close()
            raise
        return out

    def save_as(self, out_path: str) -> None:
        """Apply all staged edits to a FRESH copy of ``self.working`` (via
        ``_baked_copy`` -- the one shared bake funnel) and write the result to
        ``out_path``. Reinsertion routes EVERY edit through
        ``FontEngine.resolve`` so the document's original typeface is
        reproduced (BUILD_SPEC §4.3). Does NOT mutate ``self.path``; the caller
        decides whether to repoint via ``set_path``.

        For each page: redact ALL edited spans' original bboxes first
        (``apply_redactions`` rebuilds the resource dict and drops any
        pre-registered font), THEN register the resolved font and reinsert each
        replacement at its original baseline/size/color.

        The source is ``self.working`` (the mutable working doc, PAGES_SPEC
        §1.2), NOT a re-open of ``self.path``, so any structural change (which is
        baked into ``self.working``) is included in the save. After a structural
        op the edit maps are already baked + cleared, so this simply writes the
        working doc. The constructor's metadata + TOC carry live in working
        bytes, so both flow through untouched.

        Requires a constructed ``QGuiApplication`` (the font resolver uses Qt).
        Raises ``RuntimeError`` with a clear message if none exists, rather than
        letting ``QFontDatabase`` abort the process with a SIGABRT.

        Pending security applies here (``_save_kwargs``): the output is
        encrypted/decrypted per ``set_security``, defaulting to keeping an
        originally encrypted file protected with its open password.
        """
        out = self._baked_copy()
        try:
            # Atomic write: stage to a temp file in the destination dir and
            # os.replace, so a mid-write crash never corrupts an existing
            # target.
            self._atomic_write(out, out_path, self._save_kwargs())
        finally:
            out.close()

    def save_optimized_copy(self, out_path: str) -> tuple[int, int]:
        """Write a size-optimized COPY of the document (File > Save Optimized
        Copy, doc-tools §2.8) and return ``(before, after)`` byte sizes
        (before = the file on disk at ``self.path``). Fixed probe-verified
        fitz flags -- garbage collection, content-stream cleaning, deflate
        for streams/images/fonts, object streams; image RE-sampling is
        de-scoped (it needs a raster pipeline this app lacks). Staged edits
        are included via ``_baked_copy`` (the one bake seam) and pending
        security applies (``_save_kwargs``). Non-mutating export: no undo,
        no dirty change, and the document does NOT repoint to the copy."""
        try:
            before = os.path.getsize(self.path)
        except OSError:
            before = 0
        out = self._baked_copy()
        try:
            self._atomic_write(out, out_path, {
                "clean": True, "deflate_images": True, "deflate_fonts": True,
                "use_objstms": True, **self._save_kwargs()})
        finally:
            out.close()
        return before, os.path.getsize(out_path)

    def save_flattened(self, out_path: str) -> None:
        """Write a FLATTENED copy (File > Export Flattened Copy, forms §2):
        identical to ``save_as`` through the bake (staged edits + form values
        via ``_baked_copy``), then ``Document.bake(annots=False,
        widgets=True)`` turns every widget into ordinary page content -- the
        output's ``is_form_pdf`` is False and filled values become real page
        text with ink parity (probe §0). ``annots=False`` keeps annotations
        as live annotations; only widgets flatten. Non-mutating export: no
        ``set_path``, no ``mark_clean``, staged state untouched."""
        out = self._baked_copy()
        try:
            out.bake(annots=False, widgets=True)
            self._atomic_write(out, out_path, self._save_kwargs())
        finally:
            out.close()

    def export_text(self, out_path: str) -> None:
        """Write the whole document's plain text to ``out_path`` (File >
        Export > All Text, doc-tools §2.2), pages joined by form feeds. Staged
        edits ARE included: the source is ``_baked_copy()`` whenever any edit
        is staged (WYSIWYG by construction), else ``self.working`` directly so
        a clean doc exports without Qt. Extraction whitespace is normalized
        for the .txt consumer: NBSP variants fold to ASCII space and soft
        hyphens (U+00AD) drop -- raw extraction yields U+00A0 between words
        (the NBSP contract). Non-mutating: no undo entry, no dirty flip;
        atomic temp + ``os.replace`` like every other writer."""
        src = self._baked_copy() if self.has_edits else None
        doc = src if src is not None else self.working
        try:
            text = "\f".join(
                doc[i].get_text("text") for i in range(doc.page_count))
        finally:
            if src is not None:
                src.close()
        text = _normalize_spaces(text).replace("\u00ad", "")
        out_dir = os.path.dirname(os.path.abspath(out_path)) or "."
        fd, tmp = tempfile.mkstemp(suffix=".txt", dir=out_dir)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(text)
            os.replace(tmp, out_path)
        except Exception:
            if os.path.exists(tmp):
                os.remove(tmp)
            raise

    def _apply_page_edits(
        self,
        out: "fitz.Document",
        engine: "FontEngine",
        page_index: int,
        edits: list["Edit"],
        extra_redact_spans: tuple = (),
        exclude_newbox_key: tuple | None = None,
    ) -> None:
        """Apply ALL staged edits for one page of an open copy through a SINGLE
        pipeline (BUILD_SPEC §1.7), shared by ``save_as`` and
        ``render_with_edits`` so the on-screen page is the same bytes as the
        saved file (baked WYSIWYG, invariant §0.1). Apply order:

          1. Collect redaction rects for every span Edit that is deleted OR has
             any text/style/move/resize change (every member bbox of an
             overlap-merged box, invariant §0.2). NewBoxes add no redaction.
          2. Capture overlapping neighbors to redraw.
          3. apply_redactions with the non-destructive flags (invariant §0.4).
          4. Reinsert each non-deleted span at its EFFECTIVE geometry/style
             (resolve_family when the family/bold/italic is overridden, else the
             3-tier resolve), then draw the page's NewBoxes via resolve_family.

        ``extra_redact_spans`` are removed but NOT reinserted: the run a live
        editor is mounted over (so the editor floats over a clean background).

        The whole insertion phase draws into ONE ``fitz.Shape`` committed once
        at the end (perf foundation M3): identical content per run, one
        content-stream insert per page instead of one per run. See
        ``_insert_run``.
        """
        page = out[page_index]

        # Staged EXISTING-IMAGE deletions run FIRST as their own redaction
        # pass (images & signatures §2.4(1), M3): the flag set differs from
        # the text pass below (IMAGE_REMOVE vs TEXT_REMOVE -- two passes are
        # required), and running before the ``_overlapping_neighbors``
        # capture keeps the rawdict walk unaffected. §0 probe: removes the
        # occurrence on THIS page only; page text and the same xref's
        # occurrences on other pages survive.
        self._apply_xim_deletes(page, self.xim_deletes(page_index))

        # Unedited neighbors whose ink overlaps a redaction rect would be
        # clipped by the blind rectangle redaction; capture them to redraw.
        edited_keys = {e.span.key for e in edits}
        edited_keys |= {s.key for s in extra_redact_spans}
        # Redact EVERY member bbox of each edited box (an overlap-merged box
        # carries all its duplicate runs in redact_rects), so editing it erases
        # all the duplicates rather than leaving fragments behind. Every edit
        # kind that changes or removes the original ink contributes its rects;
        # a noop Edit (filtered upstream) never reaches here.
        redact_rects = [fitz.Rect(bb) for e in edits for bb in e.span.redact_rects]
        redact_rects += [
            fitz.Rect(bb) for s in extra_redact_spans for bb in s.redact_rects
        ]
        # Parallel per-rect writing directions so the neighbor rescue can tell a
        # same-direction residue (drop) from a cross-direction watermark overlap
        # (redraw) -- see _overlapping_neighbors.
        redact_dirs = [tuple(e.span.dir) for e in edits for _ in e.span.redact_rects]
        redact_dirs += [
            tuple(s.dir) for s in extra_redact_spans for _ in s.redact_rects
        ]
        neighbors = self._overlapping_neighbors_cached(
            out, page_index, edited_keys, redact_rects, redact_dirs
        )

        # Remove originals first (apply_redactions drops pre-registered fonts,
        # so font registration happens after). Non-destructive flags keep the
        # underlying images and vector graphics (table borders, fills) intact.
        for rect in redact_rects:
            page.add_redact_annot(rect)
        if redact_rects:
            page.apply_redactions(
                images=fitz.PDF_REDACT_IMAGE_NONE,
                graphics=fitz.PDF_REDACT_LINE_ART_NONE,
                text=fitz.PDF_REDACT_TEXT_REMOVE,
            )

        # One Shape per page bake: every run below accumulates here and lands
        # in a single commit (M3). Created after apply_redactions (which
        # rewrites the content streams) so the batch appends to the final
        # post-redaction contents, exactly where per-run insert_text landed.
        shape = page.new_shape()
        registered: set[str] = set()
        for nb in neighbors:
            rf = engine.resolve(page_index, nb["font"], nb["flags"], nb["text"])
            fontname = self._register_resolved(engine, page, rf, registered)
            self._insert_run(
                shape, nb["origin"], nb["text"], nb["size"],
                fontname, nb["color"], nb["dir"],
            )
        for edit in edits:
            if edit.deleted:
                continue                # redacted above; reinsert nothing
            self._reinsert_edit(engine, page, page_index, edit, registered,
                                shape)
        # NewBoxes: no redaction (no original ink); resolve via resolve_family
        # (always embeddable, never Tier 1) and draw at the box's own baseline.
        for box in self.new_boxes(page_index):
            if box.deleted or box.edit_key == exclude_newbox_key:
                # The box being edited is excluded so the live editor floats over the
                # bare scan. But a MOVED box must keep its ORIGINAL spot VACATED during
                # edit too -- otherwise excluding it re-reveals the scanned text there
                # ("I edit the moved box and everything jumps back"). So for the excluded
                # box, if it has TRULY moved off its cover (DISPLAY-space move, so a mere
                # width-grow on a /Rotate page does NOT count -- that wrongly blanked the
                # scan on revert), still paint the cover (vacate).
                _mv = self._box_display_move(box)
                if (not box.deleted and box.edit_key == exclude_newbox_key
                        and box.cover and len(box.cover) == 7 and _mv is not None
                        and (abs(_mv[0]) > 2.0 or abs(_mv[1]) > 2.0)):
                    cx0, cy0, cx1, cy1, cr, cg, cb = box.cover
                    page.draw_rect(fitz.Rect(cx0, cy0, cx1, cy1),
                                   color=(cr, cg, cb), fill=(cr, cg, cb), width=0)
                continue
            # OCR cover: paint the scanned text region in the paper color FIRST
            # (its own committed shape, so it sits under the text shape) so the
            # edited text replaces the scan glyphs instead of doubling them. Only
            # for a VISIBLE box: an invisible OCR overlay (render_mode 3) must let
            # the untouched scan show through, so it carries its cover unused
            # until the word is edited (which flips it visible).
            if box.render_mode == 0 and box.cover and len(box.cover) == 7:
                x0, y0, x1, y1, r, g, b = box.cover
                # The composed tile (edit_image) ALREADY carries the kept scan plus the
                # blanked + re-rendered text band, and the live preview blits exactly
                # that with no fill. So for an IN-PLACE edit the solid cover fill is
                # redundant AND destructive: it paints the WHOLE cover in paper, wiping
                # any rule / table border / box that passes through the cover but OUTSIDE
                # the text band -- and it made the bake diverge from the editor. Fill ONLY
                # to vacate the original spot when the tile has been MOVED off it, or when
                # there is no tile (an edit rendered as vector text). edit_image_rect is
                # the SAME text-space frame as the cover (both feed insert_image /
                # draw_rect), so an un-moved tile's top-left coincides with the cover's;
                # a move shifts it away.
                eir = box.edit_image_rect
                # Skip the (destructive) cover fill ONLY when the tile fully COVERS the
                # original cover -- an in-place edit whose tile spans the same or a grown
                # footprint, so no original scan can peek out. A REFLOW re-wraps to a
                # DIFFERENT footprint (narrower, or a different line count) and a MOVE shifts
                # the tile off the cover; in both, the tile no longer covers the original,
                # so we MUST vacate it -- else the old scan shows through (the reflow's
                # right-hand column bleeding past the new, narrower tile).
                covers = (bool(box.edit_image) and len(eir) == 4
                          and eir[0] <= x0 + 2.0 and eir[1] <= y0 + 2.0
                          and eir[2] >= x1 - 2.0 and eir[3] >= y1 - 2.0)
                if not covers:
                    # Vacate the moved box's original spot with a plain paper fill,
                    # then SURGICALLY redraw only the rule segment(s) that crossed
                    # THIS box's footprint (so a line the box sat on stays continuous),
                    # clipped to the cover -- no other line on the page is touched.
                    page.draw_rect(fitz.Rect(x0, y0, x1, y1),
                                   color=(r, g, b), fill=(r, g, b), width=0)
                    self._restore_line_in_cover(page, (x0, y0, x1, y1), page_index)
                # 0.3.0: an edited scanned word is drawn as a recolored+degraded
                # raster over the cover, so it blends with the scan instead of
                # crisp vector text. (Ordinary boxes never carry edit_image.)
                if box.edit_image and box.edit_image_rect:
                    # ``rotate=page.rotation`` makes the tile display upright on a
                    # /Rotate scan (no-op at 0). A paragraph tile carries its own
                    # paper + wrapped text, so it covers the block correctly even
                    # on rotated pages where axis-aligned vector text could not.
                    page.insert_image(fitz.Rect(box.edit_image_rect),
                                      stream=box.edit_image, keep_proportion=False,
                                      rotate=page.rotation)
                    # Table lines are the TOP layer: if the tile now sits ON a rule,
                    # redraw that rule segment over the tile (clipped to it), so the
                    # moved box never covers a line. Surgical -- only the lines this
                    # tile overlaps; no other line is touched.
                    self._restore_line_in_cover(
                        page, tuple(box.edit_image_rect), page_index)
                    continue
            # An UNTOUCHED paragraph overlay (invisible, render_mode 3) draws
            # nothing: the scan shows through and the box is purely the editing
            # affordance. A single invisible run of a whole joined paragraph would
            # otherwise spill off-column. Edited paragraphs took the tile path above.
            if box.is_paragraph and box.render_mode == 3:
                continue
            rf = engine.resolve_family(box.font_family, box.bold, box.italic,
                                       box.text)
            fontname = self._register_resolved(engine, page, rf, registered)
            self._insert_run(shape, box.origin, box.text, box.size, fontname,
                             box.color, box.dir, render_mode=box.render_mode)
        shape.commit()                  # no-op when nothing was drawn
        # Placed images AFTER the text Shape commit and BEFORE the annot tail
        # (images & signatures §2.4(2); the bake-order contract): drawn after
        # text so a placed image sits above reinserted text (Acrobat
        # behavior). ``rect`` is unrotated text space and
        # ``rotate=page.rotation`` renders it upright on a /Rotate page (both
        # probe-verified); ``overlay=True`` is the insert_image default.
        for box in self.image_boxes(page_index):
            page.insert_image(fitz.Rect(box.rect), stream=box.image,
                              keep_proportion=False, rotate=page.rotation)
        # Annotations LAST (annotations & markup §3.4; bake-tail contract:
        # image insertion slots in before this call): staged specs become real
        # annot objects and existing-annot overrides apply, identically for
        # render_with_edits, save_as, page_words, and the structural bake.
        self._apply_page_annots(page, page_index)

    # --- existing images: pipeline head (images & signatures §2.4(1)) -----
    @staticmethod
    def _apply_xim_deletes(page, xims: list) -> None:
        """The scoped image-REMOVE redaction pass, shared by BOTH pipelines
        (``_apply_page_edits`` / ``_apply_page_edits_for``) so the two stay
        in lockstep. One redact annot per staged deletion, then one
        ``apply_redactions`` with the image-only flag set (text/line-art
        untouched; §0 probe). No-op without staged deletions, so the common
        path adds nothing."""
        if not xims:
            return
        for x in xims:
            page.add_redact_annot(fitz.Rect(x.rect))
        page.apply_redactions(
            images=fitz.PDF_REDACT_IMAGE_REMOVE,
            graphics=fitz.PDF_REDACT_LINE_ART_NONE,
            text=fitz.PDF_REDACT_TEXT_NONE,
        )

    # --- annotations: pipeline tail (annotations & markup §3.4) -----------
    def _apply_page_annots(self, page, page_index: int) -> None:
        """Apply THIS document's staged annots + overrides to one page of a
        fresh copy (or to ``self.working`` during the structural bake)."""
        self._apply_page_annots_for(page, page_index, self._annots,
                                    self._annot_overrides)

    @staticmethod
    def _apply_page_annots_for(page, page_index: int, annots_map: dict,
                               overrides_map: dict) -> None:
        """The static body, shared with foreign-doc bakes (``_bake_doc``).

        1. Overrides first: match ``annot.xref`` against the override map;
           delete / shift / swap contents, then ``annot.update()`` (NEVER
           after delete -- the probe's proven order).
        2. Staged specs for the page in ``annot_id`` order, via the per-kind
           ``add_*_annot`` adders; ``set_colors``/``set_opacity`` BEFORE
           ``update()`` (probe §1)."""
        if any(k[0] == page_index for k in overrides_map):
            for annot in list(page.annots() or ()):
                ov = overrides_map.get((page_index, annot.xref))
                if ov is None:
                    continue
                if ov.deleted:
                    page.delete_annot(annot)
                    continue
                if ov.dx or ov.dy:
                    r = annot.rect
                    annot.set_rect(r + (ov.dx, ov.dy, ov.dx, ov.dy))
                if ov.contents is not None:
                    annot.set_info(content=ov.contents)
                annot.update()
        specs = sorted(
            (s for s in annots_map.values() if s.page_index == page_index),
            key=lambda s: s.annot_id,
        )
        for spec in specs:
            PDFDocument._insert_annot(page, spec)

    @staticmethod
    def _insert_annot(page, spec: "AnnotSpec") -> None:
        """Realize ONE staged spec as a PDF annot object on ``page``."""
        if spec.kind in _MARKUP_KINDS:
            quads = [fitz.Rect(q).quad for q in (spec.quads or ())]
            if not quads:
                return
            adder = getattr(page, f"add_{spec.kind}_annot")
            annot = adder(quads=quads)
            annot.set_colors(stroke=spec.stroke)
            if spec.opacity < 1.0:
                annot.set_opacity(spec.opacity)
            annot.update()
            return
        if spec.kind == "note":
            # Title stays EMPTY: no author identity management anywhere
            # (§3.4 / §8). set_colors/set_open BEFORE update (probe §1).
            annot = page.add_text_annot(
                fitz.Point(spec.rect[0], spec.rect[1]), spec.contents,
                icon="Comment")
            annot.set_info(content=spec.contents, title="")
            annot.set_colors(stroke=spec.stroke)
            annot.set_open(False)
            if spec.opacity < 1.0:
                annot.set_opacity(spec.opacity)
            annot.update()
            return
        if spec.kind == "ink":
            annot = page.add_ink_annot([list(s) for s in spec.points])
            PDFDocument._style_drawn_annot(annot, spec)
            return
        if spec.kind in ("rect", "ellipse"):
            adder = (page.add_rect_annot if spec.kind == "rect"
                     else page.add_circle_annot)
            annot = adder(fitz.Rect(spec.rect))
            PDFDocument._style_drawn_annot(annot, spec, fill=spec.fill)
            return
        if spec.kind in ("line", "arrow"):
            (x0, y0), (x1, y1) = spec.endpoints
            annot = page.add_line_annot(fitz.Point(x0, y0),
                                        fitz.Point(x1, y1))
            if spec.kind == "arrow":
                annot.set_line_ends(fitz.PDF_ANNOT_LE_NONE,
                                    fitz.PDF_ANNOT_LE_OPEN_ARROW)
            PDFDocument._style_drawn_annot(annot, spec)
            return
        raise ValueError(f"unsupported staged annot kind: {spec.kind!r}")

    @staticmethod
    def _style_drawn_annot(annot, spec: "AnnotSpec", fill=None) -> None:
        """Shared style tail for the DRAWN kinds (ink / rect / ellipse /
        line / arrow): stroke (+ fill when given), border width, opacity --
        all BEFORE ``update()`` (the probe's proven order, §10)."""
        if fill is not None:
            annot.set_colors(stroke=spec.stroke, fill=fill)
        else:
            annot.set_colors(stroke=spec.stroke)
        annot.set_border(width=spec.width)
        if spec.opacity < 1.0:
            annot.set_opacity(spec.opacity)
        annot.update()

    def _wrap_for_edit_cached(self, page_index: int, box, edit: "Edit"):
        """Memoized ``_wrap_for_edit`` (minor perf finding). The key captures the
        box identity, page, and every Edit field the wrap depends on, so a changed
        edit misses the cache and recomputes while a repeated effective_bbox /
        hotspot build during a scroll reuses the prior result. Cleared wholesale on
        any working-doc change via ``_invalidate_caches``."""
        st = edit.style
        sig = (
            box.identity, page_index,
            edit.effective_text(box),
            st.size, st.font_family, st.bold, st.italic,
            round(edit.scale, 6),
            edit.alignment, edit.line_spacing,
            edit.box_x, edit.box_w,
            edit.runs,
        )
        hit = self._wrap_cache.get(sig)
        if hit is not None:
            return hit
        result = self._wrap_for_edit(self.font_engine, page_index, box, edit)
        # Bound the memo so a long editing session can't grow it without limit;
        # the working set during a scroll is tiny (the visible paragraphs).
        if len(self._wrap_cache) > 256:
            self._wrap_cache.clear()
        self._wrap_cache[sig] = result
        return result

    @staticmethod
    def _wrap_for_edit(engine: "FontEngine", page_index: int, box, edit: "Edit"):
        """Compute the ``WrapResult`` for a paragraph ``box`` under ``edit`` using
        the SAME resolved face the bake draws with (REFLOW_SPEC §R2.5). The wrap
        engine is the single source of truth, so the on-screen overlay/preview and
        the saved file are computed from identical numbers (invariant §R0.1).

        Column geometry comes from the ORIGINAL box (left, width, first baseline,
        leading); the effective text/size/alignment/line-spacing come from the
        edit. The move is applied by the caller (it is the same translation for
        the bbox and every drawn line)."""
        text = edit.effective_text(box)
        size = PDFDocument._effective_size(box, edit)
        # ``box.alignment``/``box.leading`` exist on a ParagraphBox; a Span
        # routed here (because the user RESIZED it, box_w set) has neither, so
        # default them -- a resized single line wraps left-aligned at its own
        # natural leading.
        align = (edit.alignment if edit.alignment is not None
                 else getattr(box, "alignment", None) or "left")
        spacing = edit.line_spacing if edit.line_spacing is not None else 1.0
        leading = getattr(box, "leading", 0.0) or (size * 1.2)
        # The FRAME from a resize wins over the box's natural column, so the text
        # wraps to the dragged width at the box's CONSTANT size.
        left = edit.box_x if edit.box_x is not None else box.bbox[0]
        first_y = box.origin[1]
        width = (edit.box_w if edit.box_w is not None
                 else box.bbox[2] - box.bbox[0])
        if edit.runs:
            # Rich paragraph: measure with one face per style key so the bbox
            # (cover/overlay/overflow) tracks the true mixed-weight ink. The
            # returned RichWrapResult exposes the same .bbox/.leading surface
            # the callers read.
            fonts: dict[tuple, object] = {}
            by_style: dict[tuple, list] = {}
            for run in edit.runs:                # underline (opt. 4th elem) doesn't
                t, b, i = run[0], run[1], run[2]  # change width -> key on (bold, italic)
                by_style.setdefault((b, i), []).append(t)
            for style_key, parts in by_style.items():
                rf_k = PDFDocument._resolve_run(engine, page_index, box, edit,
                                                "".join(parts), *style_key)
                fonts[style_key] = engine.fitz_font_for(rf_k)
            return wrap_rich(
                [(run[0], (run[1], run[2])) for run in edit.runs], fonts, size,
                left, first_y, width, alignment=align, leading=leading,
                line_spacing=spacing,
            )
        rf = PDFDocument._resolve_for_edit(engine, page_index, box, edit, text)
        font = engine.fitz_font_for(rf)
        return wrap_paragraph(
            text, font, size, left, first_y, width,
            alignment=align, leading=leading, line_spacing=spacing,
        )

    @staticmethod
    def _reinsert_edit(engine: "FontEngine", page: "fitz.Page", page_index: int,
                       edit: "Edit", registered: set,
                       shape: "fitz.Shape") -> None:
        """Reinsert ONE edited box (redaction already done upstream). The SINGLE
        implementation shared by ``_apply_page_edits`` and the structural-bake
        ``_apply_page_edits_for``, so the live render, save, and bake can never
        drift. Handles: paragraphs (uniform + rich), single-line uniform text,
        single-line RICH runs (per-selection bold/italic), and the centered-line
        re-centering (certificate names stay centered at any length). All runs
        draw into the caller's page-batch ``shape`` (M3); fonts still register
        on ``page``."""
        span = edit.span
        text = edit.effective_text(span)
        # Paragraphs always wrap; a RESIZED box (box_w set) wraps to its new
        # frame at constant size too -- both take the wrap draw path.
        if edit.reflow:
            rf = PDFDocument._resolve_for_edit(engine, page_index, span, edit,
                                               text)
            fontname = PDFDocument._register_resolved(engine, page, rf,
                                                      registered)
            PDFDocument._insert_paragraph(engine, page, page_index, span, edit,
                                          text, rf, fontname, shape, registered)
            return
        size = PDFDocument._effective_size(span, edit)
        color = PDFDocument._effective_color(span, edit)
        origin = PDFDocument._effective_origin_static(span, edit)
        if edit.runs:
            # RICH single line: resolve each styled run to its own face, draw
            # the runs sequentially, advancing x by each run's measured width.
            drawn: list[tuple] = []
            total_w = 0.0
            for run in edit.runs:
                t, b, i = run[0], run[1], run[2]
                rs = run[3] if len(run) > 3 else None
                ul = bool(rs.underline) if rs is not None else False
                st = bool(rs.strike) if rs is not None else False
                rf_run = PDFDocument._resolve_run(engine, page_index, span,
                                                  edit, t, b, i)
                fname = PDFDocument._register_resolved(engine, page, rf_run,
                                                       registered)
                w = engine.fitz_font_for(rf_run).text_length(t, fontsize=size)
                drawn.append((t, fname, w, ul, st))
                total_w += w
            origin = PDFDocument._maybe_recenter(page, span, edit, origin,
                                                 total_w)
            x, y = origin
            uls: list = []
            sts: list = []
            for t, fname, w, ul, st in drawn:
                PDFDocument._insert_run(shape, (x, y), t, size, fname, color,
                                        span.dir)
                if ul:
                    uls.append(((x, y), w, span.dir))
                if st:
                    sts.append(((x, y), w, span.dir))
                x += w
            PDFDocument._stroke_underlines(shape, uls, size, color)
            PDFDocument._stroke_strikes(shape, sts, size, color)
            return
        rf = PDFDocument._resolve_for_edit(engine, page_index, span, edit, text)
        fontname = PDFDocument._register_resolved(engine, page, rf, registered)
        new_w = engine.fitz_font_for(rf).text_length(text, fontsize=size)
        origin = PDFDocument._maybe_recenter(page, span, edit, origin, new_w)
        PDFDocument._insert_run(shape, origin, text, size, fontname, color,
                                span.dir)

    @staticmethod
    def _maybe_recenter(page: "fitz.Page", span, edit: "Edit", origin: tuple,
                        new_width: float) -> tuple:
        """Re-center a horizontally-CENTERED single line (a certificate name, a
        title) so a different-length replacement stays centered on the same
        center point instead of anchoring to the old left edge. Only triggers
        when the original line is centered on the page (within 6% of page
        width) and the user did not deliberately move the box.

        ``span.bbox`` and the insert origin live in UNROTATED text space, so
        the centered test must use the unrotated page width: ``page.rect`` is
        rotation-aware (display space), and on a /Rotate 90/270 page its width
        is the display width -- comparing the text-space center against it
        meant the test could NEVER pass and a swapped certificate name saved
        anchored to the old left edge. Mapping the rect through
        ``derotation_matrix`` is identity on unrotated pages."""
        # Re-center ONLY a genuine centered single line (set at extraction: near the
        # page centre AND alone on its baseline row). A table/form cell is NOT flagged,
        # so it keeps its left edge -> a resize or different-size edit leaves the first
        # letter put instead of sliding (Edward's "the first letter jumps").
        if not span.is_horizontal or not getattr(span, "centered_line", False):
            return origin
        cx = (span.bbox[0] + span.bbox[2]) / 2.0
        # Re-center the BASE on the page, then ADD the move so the two stay CONSISTENT.
        # Dropping re-center the instant a move existed made a centered line snap by the
        # centering offset on the FIRST drag (it landed left of the drop point); adding
        # the move keeps it grabbable AND jump-free.
        mvx = edit.move[0] if edit.move is not None else 0.0
        return (cx - new_width / 2.0 + mvx, origin[1])

    @staticmethod
    def _resolve_run(engine: "FontEngine", page_index: int, span,
                     edit: "Edit", run_text: str, bold: bool,
                     italic: bool) -> ResolvedFont:
        """Resolve the face for ONE styled run of a rich edit. A run whose
        weight/slant matches the box's NATURAL style reuses the original face
        through the 3-tier ``resolve`` (seamless fidelity); a run the user
        restyled re-weights the family via ``resolve_family`` (embeddable)."""
        from .fonts import is_bold, is_italic
        style = edit.style
        if style.font_family is not None:
            return engine.resolve_family(style.font_family, bold, italic,
                                         run_text)
        nat_bold = is_bold(span.font, span.flags)
        nat_italic = is_italic(span.font, span.flags)
        if (bold == nat_bold and italic == nat_italic
                and style.bold is None and style.italic is None):
            return engine.resolve(page_index, span.font, span.flags, run_text)
        base = engine.resolve(page_index, span.font, span.flags, span.text)
        return engine.resolve_family(base.qt_family, bold, italic, run_text)

    @staticmethod
    def _insert_paragraph(engine: "FontEngine", page: "fitz.Page",
                          page_index: int, box, edit: "Edit", text: str, rf,
                          fontname: str, shape: "fitz.Shape",
                          registered: set | None = None) -> None:
        """Draw a reflowed paragraph: redaction of all member bboxes already
        happened upstream; here we draw each WRAPPED line via ``insert_text`` at
        the wrap engine's computed origins (NEVER ``insert_textbox`` -- the engine
        owns the breaks, REFLOW_SPEC §R2.5). The member bboxes were erased in the
        redaction step, so this redraws the paragraph flowed to its column width.
        Every line/word/segment draws into the caller's page-batch ``shape``
        (M3): a 40-line paragraph is one content-stream commit, not 40.

        Uses the SAME ``rf`` / ``fontname`` the caller resolved + registered, and
        the SAME ``fitz_font_for(rf)`` the wrap measured with, so screen == file.
        A rich edit (``edit.runs``) takes the ``wrap_rich`` path: one face per
        style key, measured and drawn per segment."""
        size = PDFDocument._effective_size(box, edit)
        # ParagraphBox carries alignment/leading; a Span routed here (resized,
        # box_w set) has neither, so default them.
        align = (edit.alignment if edit.alignment is not None
                 else getattr(box, "alignment", None) or "left")
        spacing = edit.line_spacing if edit.line_spacing is not None else 1.0
        leading = getattr(box, "leading", 0.0) or (size * 1.2)
        dx, dy = edit.move if edit.move is not None else (0.0, 0.0)
        # Honour the resize FRAME (box_x/box_w) so the bake wraps to the SAME
        # column the wrap measurement used (screen == file).
        base_left = edit.box_x if edit.box_x is not None else box.bbox[0]
        base_width = (edit.box_w if edit.box_w is not None
                      else box.bbox[2] - box.bbox[0])
        left = base_left + dx
        first_y = box.origin[1] + dy
        width = base_width
        color = PDFDocument._effective_color(box, edit)

        if edit.runs:
            reg = registered if registered is not None else set()

            def _fields(run):                    # (text, bold, italic, underline, strike)
                rs = run[3] if len(run) > 3 else None
                return (run[0], run[1], run[2],
                        bool(rs.underline) if rs is not None else False,
                        bool(rs.strike) if rs is not None else False)
            # One resolve/register per distinct style key, with ALL of that
            # style's text so glyph-coverage checks see every character. The key
            # includes underline/strike so a seg carries them to the stroke pass; the
            # face itself resolves off (bold, italic) only (they are drawn, not a face).
            by_style: dict[tuple, list] = {}
            for run in edit.runs:
                t, b, i, ul, st = _fields(run)
                by_style.setdefault((b, i, ul, st), []).append(t)
            fonts: dict[tuple, object] = {}
            fnames: dict[tuple, str] = {}
            for style_key, parts in by_style.items():
                b, i, _ul, _st = style_key
                rf_k = PDFDocument._resolve_run(engine, page_index, box, edit,
                                                "".join(parts), b, i)
                fnames[style_key] = PDFDocument._register_resolved(
                    engine, page, rf_k, reg)
                fonts[style_key] = engine.fitz_font_for(rf_k)
            result = wrap_rich(
                [(t, (b, i, ul, st)) for t, b, i, ul, st in map(_fields, edit.runs)],
                fonts, size, left, first_y, width, alignment=align,
                leading=leading, line_spacing=spacing,
            )
            uls: list = []
            sts: list = []
            for ln in result.lines:
                for seg in ln.segments:
                    PDFDocument._insert_run(
                        shape, (seg.x, ln.origin[1]), seg.text, size,
                        fnames[seg.style], color, (1.0, 0.0),
                    )
                    if seg.style[2]:             # underline flag in the style key
                        w = fonts[seg.style].text_length(seg.text, fontsize=size)
                        uls.append(((seg.x, ln.origin[1]), w, (1.0, 0.0)))
                    if seg.style[3]:             # strike flag in the style key
                        w = fonts[seg.style].text_length(seg.text, fontsize=size)
                        sts.append(((seg.x, ln.origin[1]), w, (1.0, 0.0)))
            PDFDocument._stroke_underlines(shape, uls, size, color)
            PDFDocument._stroke_strikes(shape, sts, size, color)
            return

        font = engine.fitz_font_for(rf)
        result = wrap_paragraph(
            text, font, size, left, first_y, width,
            alignment=align, leading=leading, line_spacing=spacing,
        )
        for ln in result.lines:
            if ln.word_origins:
                # Justified line: draw each word at its stretched x.
                for word, wx in ln.word_origins:
                    PDFDocument._insert_run(
                        shape, (wx, ln.origin[1]), word, size, fontname,
                        color, (1.0, 0.0),
                    )
            elif ln.text:
                PDFDocument._insert_run(
                    shape, ln.origin, ln.text, size, fontname, color,
                    (1.0, 0.0),
                )

    @staticmethod
    def _resolve_for_edit(engine: "FontEngine", page_index: int, span,
                          edit: "Edit", text: str) -> ResolvedFont:
        """Choose the resolver for a span reinsertion (BUILD_SPEC §1.7 step 4):
        a user-PICKED ``font_family`` (or a bold/italic override paired with a
        family) routes through ``resolve_family`` (always embeddable, never
        Tier 1); otherwise the existing 3-tier ``resolve`` reproduces the
        original embedded/system face. A bold/italic override WITHOUT a family
        pick still reproduces the original family but with the new weight/slant,
        so it goes through resolve_family on the original's resolved family."""
        style = edit.style
        if style.font_family is not None:
            from .fonts import is_bold, is_italic
            bold = style.bold if style.bold is not None \
                else is_bold(span.font, span.flags)
            italic = style.italic if style.italic is not None \
                else is_italic(span.font, span.flags)
            return engine.resolve_family(style.font_family, bold, italic, text)
        if style.bold is not None or style.italic is not None:
            # Reweight the ORIGINAL family. Resolve the original first to learn
            # its family name, then re-resolve that family with the new bits via
            # resolve_family (embeddable). If the original is an embedded buffer,
            # its qt_family is the closest match -- good enough for a reweight.
            from .fonts import is_bold, is_italic
            base = engine.resolve(page_index, span.font, span.flags, span.text)
            bold = style.bold if style.bold is not None \
                else is_bold(span.font, span.flags)
            italic = style.italic if style.italic is not None \
                else is_italic(span.font, span.flags)
            return engine.resolve_family(base.qt_family, bold, italic, text)
        return engine.resolve(page_index, span.font, span.flags, text)

    @staticmethod
    def _effective_size(span: Span, edit: "Edit") -> float:
        size = edit.style.size if edit.style.size is not None else span.size
        return max(_MIN_FONT_SIZE, size * edit.scale)

    @staticmethod
    def _effective_color(span: Span, edit: "Edit") -> tuple:
        return edit.style.color if edit.style.color is not None else span.color

    def _effective_origin(self, span: Span, edit: "Edit") -> tuple:
        """The span's baseline origin shifted by Edit.move (in text space).
        ``move`` is already in PDF point space (de-zoomed, de-rotated by the
        view), so it adds directly to the rawdict origin before _insert_run
        applies the writing direction."""
        if edit.move is None:
            return span.origin
        dx, dy = edit.move
        ox, oy = span.origin
        return (ox + dx, oy + dy)

    def effective_origin(self, page: int, box) -> tuple:
        """The box's STAGED baseline origin in rawdict text-space points (the
        point the bake draws the run at), so the inline editor mounts on the live
        baseline after a move -- not the original spot. For a NewBox the origin is
        kept current on the box itself; for a Span it is origin + Edit.move."""
        if isinstance(box, NewBox):
            cur = self._new_boxes.get(box.edit_key) or box
            return cur.origin
        if isinstance(box, ImageBox):
            cur = self._images.get(box.edit_key) or box
            return cur.origin
        if isinstance(box, ExistingImage):
            return box.origin       # frozen file truth (moves are macros)
        edit = self._edits.get(self._span_edit_key(page, box))
        if edit is None:
            return box.origin
        if getattr(box, "is_paragraph", False) or not box.is_horizontal:
            return self._effective_origin(box, edit)
        # Horizontal single line: include the centered-line re-centering so the
        # editor opens exactly where the baked (possibly re-centered) ink sits.
        origin, _ = self._staged_line_layout(page, box, edit)
        return origin

    def effective_bbox(self, page: int, box) -> tuple:
        """The box's STAGED (post-move / post-resize / post-size) bbox in rawdict
        text-space points, mirroring what ``render_with_edits`` actually bakes
        (BUILD_SPEC §0.1). The selection overlay is built from this so the outline
        + handles track the live ink instead of locking over the original spot.

        For a NewBox the bbox is already kept current by ``_newbox_bbox`` whenever
        text/size/family changes, so its staged bbox IS ``box.bbox``. For a Span
        we recompute: scale the box about the baseline origin by the size ratio
        (effective_size / original_size -- captures BOTH a resize and an absolute
        size override), then translate by ``Edit.move`` -- the same two transforms
        the bake applies (size grows the glyph from the baseline; move shifts it).
        """
        if isinstance(box, NewBox):
            cur = self._new_boxes.get(box.edit_key) or box
            return cur.bbox
        if isinstance(box, ImageBox):
            # The map's rect IS the staged truth (move/resize replace it),
            # so the overlay/hotspot track the live rect (images §3).
            cur = self._images.get(box.edit_key) or box
            return cur.rect
        if isinstance(box, ExistingImage):
            return box.rect         # frozen file truth (moves are macros)
        edit = self._edits.get(self._span_edit_key(page, box))
        if edit is None or edit.is_noop:
            return box.bbox
        # A reflowing box (a ParagraphBox, OR any box the user RESIZED so box_w
        # is set) recomputes its union bbox from the wrap engine (more/fewer
        # lines change the height) so the overlay tracks the reflowed ink
        # (REFLOW_SPEC §R2.5). Translate by any move.
        if edit.reflow:
            result = self._wrap_for_edit_cached(page, box, edit)
            bx0, by0, bx1, by1 = result.bbox
            dx, dy = edit.move if edit.move is not None else (0.0, 0.0)
            return (bx0 + dx, by0 + dy, bx1 + dx, by1 + dy)
        x0, y0, x1, y1 = box.bbox
        ox, oy = box.origin
        # Size ratio about the baseline origin: the glyph grows from its
        # baseline, so ascent (oy-y0), descent (y1-oy) and width all scale.
        ratio = self._effective_size(box, edit) / max(box.size, 1e-6)
        sy0 = oy + (y0 - oy) * ratio
        sy1 = oy + (y1 - oy) * ratio
        dx, dy = edit.move if edit.move is not None else (0.0, 0.0)
        if box.is_horizontal:
            # Horizontal single line: the bake measures the staged text with
            # the resolved face(s) and re-centers a page-centered line, so the
            # box FITS THE TEXT -- mirror that exactly (origin already carries
            # the move; the y extent scales about the baseline as above).
            origin, width = self._staged_line_layout(page, box, edit)
            nx0 = origin[0]
            nx1 = nx0 + max(width, 6.0)   # floor: an empty box stays clickable
            return (nx0, sy0 + dy, nx1, sy1 + dy)
        sx0 = ox + (x0 - ox) * ratio
        sx1 = ox + (x1 - ox) * ratio
        return (sx0 + dx, sy0 + dy, sx1 + dx, sy1 + dy)

    def _staged_line_layout(self, page: int, box, edit: "Edit") -> tuple:
        """(origin, width) of the single-line ink the bake will draw for this
        edit: the effective origin (move applied), the total advance measured
        with the SAME per-run resolved faces the bake draws with, and the
        centered-line re-centering. Mirrors ``_reinsert_edit`` exactly so the
        selection overlay / hotspot / inline editor sit ON the baked ink."""
        engine = self.font_engine
        size = self._effective_size(box, edit)
        if edit.runs:
            width = sum(
                engine.fitz_font_for(
                    self._resolve_run(engine, page, box, edit,
                                      run[0], run[1], run[2])
                ).text_length(run[0], fontsize=size)
                for run in edit.runs
            )
        else:
            text = edit.effective_text(box)
            rf = self._resolve_for_edit(engine, page, box, edit, text)
            width = engine.fitz_font_for(rf).text_length(text, fontsize=size)
        origin = self._effective_origin(box, edit)
        origin = self._maybe_recenter(self.doc[page], box, edit, origin, width)
        return origin, width

    def paragraph_overflow(self, page: int, box) -> float:
        """How far (PDF points) a ParagraphBox's REFLOWED text extends past its
        ORIGINAL box bottom (REFLOW_SPEC §R2.5). > 0 means a longer replacement /
        larger line spacing grew the paragraph taller than its original frame, so
        the extra lines now draw over whatever sits beneath the box -- the UI
        surfaces this (a danger edge on the overlay + a status note) instead of
        letting the collision happen silently. 0 for a non-paragraph, an unedited
        box, or text that still fits.

        Compared against the ORIGINAL box bottom (``box.bbox[3]``), not the
        reflowed bbox, so shrinking text (fewer lines) reports no overflow. The
        move's dy shifts both the text and the frame together, so it cancels and
        is intentionally not added here."""
        if not getattr(box, "is_paragraph", False):
            return 0.0
        # A scanned-OCR NewBox paragraph is NOT a reflowing ParagraphBox: its frame
        # (edit_image_rect via _newbox_bbox) GROWS to fit the edit, so the extent is
        # never hidden and there is no `.key` into the span-edit map. Report no
        # overflow cleanly instead of raising AttributeError for the UI to swallow.
        if isinstance(box, NewBox):
            return 0.0
        edit = self._edits.get(self._span_edit_key(page, box))
        if edit is None or edit.is_noop:
            return 0.0
        result = self._wrap_for_edit_cached(page, box, edit)
        reflowed_bottom = result.bbox[3]
        original_bottom = box.bbox[3]
        return max(0.0, reflowed_bottom - original_bottom)

    def render_with_edits(
        self, page_index: int, zoom: float, exclude_span: "Span | None" = None
    ) -> "fitz.Pixmap":
        """Pixmap of ``page_index`` with all staged edits BAKED IN, so the
        on-screen page matches exactly what ``save_as`` produces (true
        background, table lines intact, no flat cover). ``exclude_span``, if
        given, is the run a live editor is mounted over: it is removed but not
        reinserted so the editor floats over a clean background.

        Falls back to the plain ``render`` when the page has no edits (and no
        excluded span), avoiding a fresh open. Requires a ``QGuiApplication``
        once there is work, since the resolver uses Qt; falls back to the plain
        render if none exists rather than aborting.
        """
        page_edits = [
            e for (pi, _), e in self._edits.items()
            if pi == page_index and not e.is_noop
        ]
        # The run under a live editor: a Span is removed-but-not-reinserted via
        # extra_redact_spans; a NewBox under the editor is simply skipped from
        # the page's new_boxes draw (it has no original ink to redact).
        exclude_newbox_key = None
        if exclude_span is not None and isinstance(exclude_span, NewBox):
            exclude_newbox_key = exclude_span.edit_key
            extra = ()
        elif exclude_span is not None:
            page_edits = [
                e for e in page_edits if e.span.key != exclude_span.key
            ]
            extra = (exclude_span,)
        else:
            extra = ()

        has_new = any(not exclude_newbox_key or b.edit_key != exclude_newbox_key
                      for b in self.new_boxes(page_index))
        # The fast path must ALSO see staged annots / overrides / placed
        # images / existing-image deletions for this page, else a new
        # highlight or a placed image never appears on screen (annotations
        # §3.4; images & signatures §2.5) -- and staged form values
        # (forms §2), else a fill never shows.
        needs_edit_pipeline = (bool(page_edits) or bool(extra) or has_new
                               or bool(self.image_boxes(page_index))
                               or bool(self.xim_deletes(page_index))
                               or self._page_has_annots(page_index))
        if not needs_edit_pipeline \
                and not self._page_has_form_edits(page_index):
            return self.render(page_index, zoom)

        # A page with ONLY form edits skips ``_apply_page_edits`` entirely
        # (forms §2): open the copy, write the values (``widget.update()``
        # regenerates appearances inside MuPDF), pixmap. No FontEngine, no
        # redactions, no Qt -- so it also skips the QGuiApplication guard.
        if not needs_edit_pipeline:
            out = fitz.open("pdf", self.working.tobytes())
            try:
                self._apply_form_values(out, only_page=page_index)
                return out[page_index].get_pixmap(
                    matrix=fitz.Matrix(zoom, zoom), alpha=False
                )
            finally:
                out.close()

        from PySide6.QtGui import QGuiApplication
        if QGuiApplication.instance() is None:
            return self.render(page_index, zoom)

        out = fitz.open("pdf", self.working.tobytes())
        try:
            # Staged form values land on the same fresh copy BEFORE the edit
            # pipeline (forms §2; probe-proven order-safe against redactions).
            self._apply_form_values(out, only_page=page_index)
            # The persistent engine (see save_as): a fresh FontEngine(out) here
            # cost ~5-7 ms of cold resolve + extract_font per edit per FRAME;
            # ``out`` is byte-identical to ``working`` so the long-lived engine
            # resolves identically, and render + save share the one engine so
            # screen == file by construction (perf foundation M1a).
            engine = self.font_engine
            self._apply_page_edits(
                out, engine, page_index, page_edits, extra_redact_spans=extra,
                exclude_newbox_key=exclude_newbox_key,
            )
            return out[page_index].get_pixmap(
                matrix=fitz.Matrix(zoom, zoom), alpha=False
            )
        finally:
            out.close()

    def render_signature(self, page_index: int) -> tuple:
        """A hashable signature of EVERYTHING that determines this page's
        rendered pixels at a fixed zoom: equal signatures GUARANTEE
        ``render_with_edits(page_index, z)`` produces the same pixels (perf
        foundation M1d). The view's pixmap cache keys on it.

        Composition: ``(_spans_generation, sorted Edit signatures, sorted
        NewBox signatures)`` for the page.

        - ``_spans_generation`` covers the working-doc content: every bake,
          structural op, and structural undo/redo bumps it via
          ``_invalidate_caches``, so a restructure can never collide with a
          pre-restructure signature.
        - Each Edit signature carries every field the bake reads (the
          ``_wrap_for_edit_cached`` signature set plus move/deleted). Noop
          edits are skipped, mirroring ``render_with_edits``'s own filter, so
          an edit undone back to the original yields the PRISTINE signature
          (maximally precise: those pixels really are identical).
        - Each NewBox signature carries the full dataclass payload including
          ``deleted`` (a deleted box draws nothing, but keeping it keyed is a
          safe spurious miss, never a wrong hit).

        Fine-grained undo/redo mutate ``_edits``/``_new_boxes`` directly, so the
        signature tracks them with no extra hook.

        ARCHITECTURE RULE (cross-workstream): this is the ONE cache-key
        registry. Any workstream adding staged per-page render state (annots,
        images, form values, ...) MUST fold its sorted per-page state into this
        tuple in the same change that adds the state -- no side-channel
        signature APIs."""
        edit_sigs = []
        for (pi, _), e in self._edits.items():
            if pi != page_index or e.is_noop:
                continue
            st = e.style
            edit_sigs.append((
                e.span.key,
                e.effective_text(),
                (st.font_family, st.size, st.color, st.bold, st.italic),
                e.move,
                round(e.scale, 6),
                e.deleted,
                e.alignment,
                e.line_spacing,
                e.box_x,
                e.box_w,
                e.runs,
            ))
        box_sigs = []
        for b in self._new_boxes.values():
            if b.page_index != page_index:
                continue
            box_sigs.append((
                b.box_id, b.origin, b.text, b.font_family, b.size,
                b.color, b.bold, b.italic, b.deleted,
                # OCR-edit fields the bake reads that text/origin do not fully cover:
                # render_mode flips an overlay visible, and an edited box's pixels ride
                # edit_image at edit_image_rect (a grown insert widens the rect at the
                # same text). Folding them in keeps the pixmap cache honest.
                b.render_mode, tuple(b.cover), tuple(b.edit_image_rect),
                len(b.edit_image),
                # Per-run styling + a picked-family override change the baked pixels
                # at the SAME text/size, so fold them in or the pixmap cache goes
                # stale (a per-run colour/bold edit would not re-render).
                tuple(b.runs), b.font_picked,
            ))
        # Placed images (images & signatures §2.5): every ImageBox field the
        # bake reads (the bytes themselves carried by length -- no mutator
        # ever swaps a box's bytes, and box_ids are never reused, so the id +
        # rect uniquely determine the drawn pixels within a generation).
        img_sigs = []
        for b in self._images.values():
            if b.page_index != page_index:
                continue
            img_sigs.append((
                b.box_id, b.rect, b.kind, b.natural_px, b.deleted,
                len(b.image),
            ))
        # Staged existing-image deletions (images & signatures §2.5, M3):
        # the key alone determines the redaction pass (the rect rides in the
        # frozen record the key names), so the sorted keys are the signature.
        xim_sigs = tuple(sorted(
            (x.xref, x.occ) for x in self._xim_deletes.values()
            if x.page_index == page_index))
        # Staged annotations + existing-annot overrides (annotations & markup
        # §9): folded into the ONE registry tuple, no side-channel signature
        # APIs. Every AnnotSpec field the bake reads is carried, so any annot
        # mutation (incl. undo/redo, which edit the maps directly) misses.
        annot_sigs = []
        for s in self._annots.values():
            if s.page_index != page_index:
                continue
            annot_sigs.append((
                s.annot_id, s.kind, s.quads, s.rect, s.points, s.endpoints,
                s.stroke, s.fill, s.width, s.opacity, s.contents,
            ))
        ov_sigs = []
        for (pi, xref), ov in self._annot_overrides.items():
            if pi != page_index:
                continue
            ov_sigs.append((xref, ov.deleted, ov.contents, ov.dx, ov.dy))
        # Staged form values (forms §10): folded into the ONE registry tuple.
        # A reverted fill never stores an entry (stage_form_value drops the
        # key at baseline), so the pristine signature returns after undo.
        form_sigs = []
        for (pi, _tag, name), value in self._form_edits.items():
            if pi != page_index:
                continue
            form_sigs.append((name, value))
        # Sort on the leading identity element only: keys/box_ids/annot_ids/
        # xrefs/field names are unique per page, and later elements may mix
        # None with non-None (uncomparable).
        edit_sigs.sort(key=lambda s: s[0])
        box_sigs.sort(key=lambda s: s[0])
        img_sigs.sort(key=lambda s: s[0])
        annot_sigs.sort(key=lambda s: s[0])
        ov_sigs.sort(key=lambda s: s[0])
        form_sigs.sort(key=lambda s: s[0])
        return (self._spans_generation, tuple(edit_sigs), tuple(box_sigs),
                tuple(img_sigs), xim_sigs, tuple(annot_sigs), tuple(ov_sigs),
                tuple(form_sigs))

    def page_words(self, page_index: int) -> tuple:
        """Every word on ``page_index`` AS BAKED (text-editing UX §5.1):
        ``WordBox``es sorted in reading order ``(block, line, word)``.

        Words come from the SAME pipeline the screen and the saved file come
        from: a page with no staged edits and no new boxes reads
        ``get_text("words")`` straight off ``self.working`` (cheap -- no
        copy); an edited page bakes a fresh byte-copy through
        ``_apply_page_edits`` exactly like ``render_with_edits``/``save_as``
        (resolving through the persistent ``self.font_engine``), then
        extracts words from the BAKED page. So the text-select tool can never
        disagree with the screen or the saved file -- staged edits included
        -- by construction (WYSIWYG invariant §0.2).

        Memoized per page in ``self._words_cache``: every mutator and every
        undo/redo replay passes through ``_install_state`` (which drops the
        page's entry) and every working-doc change funnels through
        ``_invalidate_caches`` (which clears the map), so a stale entry is
        unreachable. Coordinates are rawdict text-space (see ``WordBox``).

        Like ``render_with_edits``, the bake path needs a constructed
        ``QGuiApplication`` (the font resolver uses Qt); without one the
        PRISTINE page's words are returned uncached -- never a wrong cache
        entry, mirroring render's plain-render fallback."""
        cached = self._words_cache.get(page_index)
        if cached is not None:
            return cached
        page_edits = [
            e for (pi, _), e in self._edits.items()
            if pi == page_index and not e.is_noop
        ]
        has_new = bool(self.new_boxes(page_index))
        if not page_edits and not has_new:
            raw = self.working[page_index].get_text("words")
        else:
            from PySide6.QtGui import QGuiApplication
            if QGuiApplication.instance() is None:
                return self._make_word_boxes(
                    self.working[page_index].get_text("words"))
            out = fitz.open("pdf", self.working.tobytes())
            try:
                self._apply_page_edits(out, self.font_engine, page_index,
                                       page_edits)
                raw = out[page_index].get_text("words")
            finally:
                out.close()
        words = self._make_word_boxes(raw)
        self._words_cache[page_index] = words
        return words

    @staticmethod
    def _make_word_boxes(raw) -> tuple:
        """``get_text("words")`` 8-tuples -> reading-order WordBox tuple."""
        return tuple(
            WordBox(bbox=(w[0], w[1], w[2], w[3]), text=w[4],
                    block=w[5], line=w[6], word=w[7])
            for w in sorted(raw, key=lambda t: (t[5], t[6], t[7]))
        )

    def _overlapping_neighbors_cached(
        self,
        out: "fitz.Document",
        page_index: int,
        edited_keys: set[tuple],
        redact_rects: list["fitz.Rect"],
        redact_dirs: list[tuple] | None = None,
    ) -> list[dict]:
        """Memoized ``_overlapping_neighbors`` for the instance bake pipeline
        (perf foundation M1b). Sound because the walk runs BEFORE this page's
        redactions, when ``out``'s page bytes equal ``self.working``'s, so its
        result is fully determined by (working generation, page, edited keys,
        redaction rects) -- exactly the cache key. The cached list is read-only
        by convention (same as ``spans()``); callers only iterate it. The static
        ``_overlapping_neighbors`` stays for ``_apply_page_edits_for`` (foreign-
        doc bakes, where no per-instance generation exists)."""
        key = (
            self._spans_generation,
            page_index,
            frozenset(edited_keys),
            tuple(tuple(r) for r in redact_rects),
            tuple(redact_dirs) if redact_dirs is not None else None,
        )
        hit = self._neighbors_cache.get(key)
        if hit is not None:
            return hit
        result = self._overlapping_neighbors(
            out, page_index, edited_keys, redact_rects, redact_dirs
        )
        # FIFO cap: dicts iterate in insertion order, so evict the oldest. The
        # working set is tiny (the pages currently being re-rendered).
        if len(self._neighbors_cache) >= 32:
            self._neighbors_cache.pop(next(iter(self._neighbors_cache)))
        self._neighbors_cache[key] = result
        return result

    @staticmethod
    def _overlapping_neighbors(
        out: "fitz.Document",
        page_index: int,
        edited_keys: set[tuple],
        redact_rects: list["fitz.Rect"],
        redact_dirs: list[tuple] | None = None,
    ) -> list[dict]:
        """Unedited spans on ``page_index`` whose ink overlaps a redaction rect.

        Returns a list of dicts (text/origin/size/color/font/flags) for each
        neighbor span that is NOT itself being edited but whose bbox materially
        intersects one of the edited spans' redaction rects. These are redrawn
        after redaction so the rect does not silently truncate them. The overlap
        must be a real area intersection (more than a hairline touch) so spans
        that merely abut an edited rect are not needlessly redrawn.

        ``redact_dirs`` (parallel to ``redact_rects``) carries each covering
        rect's SOURCE-span writing direction. A neighbor mostly inside a rect is
        normally dropped as residue (a duplicate of the box being removed), but
        a rotated watermark's axis-aligned bbox blankets every horizontal body
        line -- so a same->different direction overlap is never a same-box
        duplicate (mirror of ``_merge_overlapping``'s direction gate). When a
        neighbor's direction differs from EVERY covering rect's direction, it is
        redrawn regardless of the overlap fraction. Absent ``redact_dirs`` the
        old fraction-only behaviour is preserved.
        """
        page = out[page_index]
        raw = page.get_text("rawdict")
        neighbors: list[dict] = []
        for bi, block in enumerate(raw["blocks"]):
            if block.get("type", 0) != 0:
                continue
            for li, line in enumerate(block["lines"]):
                line_dir = tuple(line.get("dir", (1.0, 0.0)))
                for si, span in enumerate(line["spans"]):
                    if (bi, li, si) in edited_keys:
                        continue
                    text = "".join(c["c"] for c in span.get("chars", []))
                    if not text.strip():
                        continue
                    sbbox = fitz.Rect(span["bbox"])
                    if sbbox.is_empty or sbbox.is_infinite:
                        continue
                    sarea = sbbox.get_area()
                    if sarea <= 0:
                        continue
                    # Fraction of this span's area that falls inside the
                    # redaction. A span MOSTLY inside is a duplicate/fragment of
                    # the box being removed (do NOT rescue it -- that is exactly
                    # the residue bug). A span only edge-clipped (small fraction)
                    # is a distinct neighbor to redraw.
                    inside = 0.0
                    cover_dirs: list[tuple] = []
                    for ri, rect in enumerate(redact_rects):
                        inter = sbbox & rect
                        if not inter.is_empty:
                            inside += inter.get_area()
                            if redact_dirs is not None:
                                cover_dirs.append(redact_dirs[ri])
                    frac = min(inside / sarea, 1.0)
                    if frac < 0.02:
                        continue
                    if frac >= 0.5:
                        # >=50% inside = residue of the removed box ONLY when the
                        # neighbor shares a covering rect's writing direction. A
                        # cross-direction overlap (e.g. a diagonal watermark's
                        # bbox blanketing this body line) is never a same-box
                        # duplicate -- redraw it instead of erasing it.
                        same_dir = any(
                            abs(line_dir[0] - d[0]) <= 1e-3
                            and abs(line_dir[1] - d[1]) <= 1e-3
                            for d in cover_dirs
                        )
                        if same_dir or not cover_dirs:
                            continue
                    neighbors.append({
                        "text": text,
                        "origin": tuple(span["origin"]),
                        "size": span["size"],
                        "color": tuple(
                            c / 255 for c in fitz.sRGB_to_rgb(span["color"])
                        ),
                        "font": span["font"],
                        "flags": span["flags"],
                        "dir": line_dir,
                    })
        return neighbors

    @staticmethod
    def _insert_run(
        shape: "fitz.Shape",
        origin: tuple,
        text: str,
        fontsize: float,
        fontname: str,
        color: tuple,
        direction: tuple,
        render_mode: int = 0,
    ) -> None:
        """Draw one run at its baseline ``origin`` into the page's batch
        ``shape``, preserving writing direction. ``render_mode`` is the PDF text
        render mode (0 = fill/visible, 3 = invisible-but-selectable, used for the
        OCR text layer placed over a kept scan image).

        BATCHING (perf foundation M3): runs accumulate on ONE ``fitz.Shape``
        per page bake, committed once by the ``_apply_page_edits`` caller.
        ``page.insert_text`` is itself a one-shot Shape (new_shape +
        Shape.insert_text + commit), so each batched call builds the IDENTICAL
        BT/Tm/Tf/TJ content block -- only the per-call commit (content-stream
        insert + resource scan, ~0.8 ms each) is amortized to one per page.
        Visual order among our runs is preserved: Shape concatenates its text
        content in call order. Font registration stays on the PAGE
        (``_register_resolved``); Shape's internal ``insert_font(fontname=)``
        then resolves the already-registered resource by name, and base-14
        codes pass through as built-in Type1 exactly as before (the reason
        Shape was chosen over TextWriter, which converts base-14 to CID).

        For horizontal runs this is a plain ``insert_text``. For rotated/vertical
        runs (line['dir'] != (1,0)) we morph about the baseline origin so the
        replacement keeps the original orientation instead of being flattened to
        horizontal. ``rotate`` only accepts 0/90/180/270, so we use ``morph`` for
        arbitrary angles, which covers every case uniformly (Shape.insert_text
        accepts the same ``morph``, so rotated runs ride the same batch).
        """
        point = fitz.Point(origin)
        angle = math.degrees(math.atan2(-direction[1], direction[0]))
        if abs(angle) < 1e-3:
            shape.insert_text(
                point, text, fontsize=fontsize, fontname=fontname, color=color,
                render_mode=render_mode,
            )
            return
        # Rotate about the baseline origin so the run pivots in place.
        morph = (point, fitz.Matrix(angle))
        shape.insert_text(
            point, text, fontsize=fontsize, fontname=fontname,
            color=color, morph=morph, render_mode=render_mode,
        )

    @staticmethod
    def _stroke_underlines(shape: "fitz.Shape", segs: list, size: float,
                           color: tuple) -> None:
        """Stroke an underline under each run in ``segs`` = [(baseline_origin,
        width, direction), ...] on the page's batch ``shape``. The line sits
        ~0.11*size below the baseline, PERPENDICULAR to the writing direction (so
        rotated runs underline along their own line), thickness ~0.055*size. One
        ``finish`` per call: every run of a single edit shares that edit's ink
        colour, so a single stroke pass is exact. No-op on an empty list."""
        if not segs:
            return
        off = 0.11 * size
        for (ox, oy), w, (dx, dy) in segs:
            px, py = -dy * off, dx * off        # down-perpendicular (PDF y-down)
            shape.draw_line(fitz.Point(ox + px, oy + py),
                            fitz.Point(ox + w * dx + px, oy + w * dy + py))
        shape.finish(color=color, width=max(0.4, 0.055 * size))

    @staticmethod
    def _stroke_strikes(shape: "fitz.Shape", segs: list, size: float,
                        color: tuple) -> None:
        """Stroke a strikethrough THROUGH each run in ``segs`` = [(baseline_origin,
        width, direction), ...] on the page's batch ``shape``. Twin of
        ``_stroke_underlines`` but the line sits ~0.3*size ABOVE the baseline
        (through the glyph body near x-height/2, at ``baseline_y - 0.3*size``),
        PERPENDICULAR to the writing direction, thickness ~0.055*size. One
        ``finish`` per call. No-op on an empty list."""
        if not segs:
            return
        off = -0.3 * size                       # up-perpendicular (PDF y-down)
        for (ox, oy), w, (dx, dy) in segs:
            px, py = -dy * off, dx * off
            shape.draw_line(fitz.Point(ox + px, oy + py),
                            fitz.Point(ox + w * dx + px, oy + w * dy + py))
        shape.finish(color=color, width=max(0.4, 0.055 * size))

    @staticmethod
    def _face_name(prefix: str, buffer: bytes) -> str:
        """A registration name UNIQUE to a concrete face buffer.

        ``insert_text`` keys a page's font resources by name, so the name MUST
        differ between distinct faces or the second face silently reuses the
        first's glyphs. Hashing the actual bytes gives each Regular/Bold/Italic
        face (and each distinct embedded buffer) its own stable name.
        """
        return prefix + hashlib.sha1(buffer).hexdigest()[:12]

    @staticmethod
    def _register_resolved(
        engine: FontEngine,
        page: "fitz.Page",
        rf,
        registered: set[str],
    ) -> str:
        """Register the resolved font on ``page`` (idempotent per page) and
        return the registration name to pass to ``insert_text``.

        The registration name is derived per concrete FACE (from the face
        bytes), never a constant, so two distinct faces of the same tier on one
        page do not collide on a single name and clobber each other.

        - TIER_EMBEDDED: register the original embedded buffer directly, so the
          saved glyphs are the document's own typeface. Name = 'E' + buffer hash.
        - TIER_SYSTEM: extract the specific face (a .ttc may hold many) to
          standalone bytes via ``face_bytes`` and embed them, so the output is
          portable. Name = 'S' + face-bytes hash.
        - TIER_BASE14: the base-14 code IS a built-in font name PyMuPDF accepts
          directly on ``insert_text``; no registration needed.
        """
        if rf.tier == TIER_EMBEDDED:
            name = PDFDocument._face_name("E", rf.pdf_fontbuffer)
            if name not in registered:
                page.insert_font(fontname=name, fontbuffer=rf.pdf_fontbuffer)
                registered.add(name)
            return name

        if rf.tier == TIER_SYSTEM:
            rec = engine.system_record_for(
                rf.qt_family, rf.qt_bold, rf.qt_italic
            )
            if rec is not None:
                buffer = face_bytes(rec.path, rec.face_index)
                name = PDFDocument._face_name("S", buffer)
                if name not in registered:
                    page.insert_font(fontname=name, fontbuffer=buffer)
                    registered.add(name)
                return name
            # No concrete face on disk despite an exactMatch() family; fall back
            # to the base-14 floor so the write stays glyph-safe rather than
            # emitting tofu.
            return rf.base14_code or "helv"

        # TIER_BASE14: built-in font code, accepted by insert_text as-is.
        return rf.base14_code or rf.pdf_fontname

    # =====================================================================
    # BAKE-THEN-MUTATE: page & document structural operations (PAGES_SPEC §3)
    # =====================================================================
    # Every structural op follows ONE skeleton (PAGES_SPEC §1.4):
    #   1. _snapshot_for_undo()    push working bytes BEFORE the change
    #   2. _bake_pending_edits()   realize + clear staged text/box edits (§0.6)
    #   3. <fitz mutation on self.working>
    #   4. _invalidate_caches()    rebind FontEngine (structure changed)
    #   5. self._dirty = True
    # so WYSIWYG + the box editor keep working on the restructured doc and no
    # staged edit is ever orphaned by a page-index shift.

    @staticmethod
    def _bake_doc(target: "fitz.Document", edits_map: dict, newbox_map: dict,
                  annots_map: dict | None = None,
                  overrides_map: dict | None = None,
                  images_map: dict | None = None,
                  xim_map: dict | None = None,
                  owner: "PDFDocument | None" = None) -> None:
        """Realize the staged edits in ``edits_map`` / ``newbox_map`` (and the
        staged annotations / overrides, annotations & markup §3.5, and the
        placed images + existing-image deletions, images & signatures §2.4)
        INTO ``target`` via the SAME ``_apply_page_edits`` pipeline
        ``save_as`` uses (PAGES_SPEC
        §1.3/§3.6). Shared by ``_bake_pending_edits`` (target == self.working)
        and by ``merge``/``extract_pages``/``split`` (target == a throwaway
        copy of another doc), so the bake logic exists once.

        ``target`` MUST be a copy whose page indices align with the keys in the
        maps. Does NOT clear the maps (the caller owns that)."""
        annots_map = annots_map or {}
        overrides_map = overrides_map or {}
        images_map = images_map or {}
        xim_map = xim_map or {}
        if not edits_map and not newbox_map and not annots_map \
                and not overrides_map and not images_map and not xim_map:
            return
        engine = FontEngine(target)
        by_page: dict[int, list[Edit]] = {}
        for (pi, _), edit in edits_map.items():
            if not edit.is_noop:
                by_page.setdefault(pi, []).append(edit)
        # Pages that only carry NEW boxes still need a pass so the box is drawn.
        for box in newbox_map.values():
            if not box.deleted:
                by_page.setdefault(box.page_index, [])
        # ... pages with only placed images / existing-image deletions ...
        for box in images_map.values():
            if not box.deleted:
                by_page.setdefault(box.page_index, [])
        for x in xim_map.values():
            by_page.setdefault(x.page_index, [])
        # ... and pages with only staged annots / overrides (the tail hook).
        for spec in annots_map.values():
            by_page.setdefault(spec.page_index, [])
        for (pi, _xref) in overrides_map:
            by_page.setdefault(pi, [])
        for pi, edits in by_page.items():
            PDFDocument._apply_page_edits_for(target, engine, pi, edits,
                                              newbox_map, annots_map,
                                              overrides_map, images_map,
                                              xim_map, owner=owner)

    @staticmethod
    def _apply_page_edits_for(target: "fitz.Document", engine: "FontEngine",
                              page_index: int, edits: list["Edit"],
                              newbox_map: dict,
                              annots_map: dict | None = None,
                              overrides_map: dict | None = None,
                              images_map: dict | None = None,
                              xim_map: dict | None = None,
                              owner: "PDFDocument | None" = None) -> None:
        """``_apply_page_edits`` for ARBITRARY newbox/annot/image maps (used
        when baking a foreign doc whose staged state lives in the passed maps,
        not ``self``). The body mirrors ``_apply_page_edits`` exactly but
        draws the foreign maps' state for ``page_index`` instead of
        ``self``'s -- keep BOTH in lockstep (the screen == save invariant).

        ``owner`` is the PDFDocument whose ``_restore_line_in_cover`` (table-rule
        redraw) applies to ``target``. Only ``_bake_pending_edits`` passes it
        (its target IS ``self.working`` with aligned page indices), so a moved
        OCR box that crossed a table rule bakes the rule back on structural ops
        too. merge/split/extract leave it ``None`` -- their throwaway target's
        indices don't align, so the surgical redraw would land on the wrong
        page (behavior unchanged for them)."""
        page = target[page_index]
        # Existing-image deletions FIRST, mirroring ``_apply_page_edits``
        # exactly (images & signatures §2.4(1), M3).
        PDFDocument._apply_xim_deletes(page, sorted(
            (x for x in (xim_map or {}).values()
             if x.page_index == page_index),
            key=lambda x: (x.xref, x.occ)))
        edited_keys = {e.span.key for e in edits}
        redact_rects = [fitz.Rect(bb) for e in edits
                        for bb in e.span.redact_rects]
        redact_dirs = [tuple(e.span.dir) for e in edits
                       for _ in e.span.redact_rects]
        neighbors = PDFDocument._overlapping_neighbors(
            target, page_index, edited_keys, redact_rects, redact_dirs)
        for rect in redact_rects:
            page.add_redact_annot(rect)
        if redact_rects:
            page.apply_redactions(
                images=fitz.PDF_REDACT_IMAGE_NONE,
                graphics=fitz.PDF_REDACT_LINE_ART_NONE,
                text=fitz.PDF_REDACT_TEXT_REMOVE,
            )
        # Same one-Shape-per-page batch as ``_apply_page_edits`` (M3).
        shape = page.new_shape()
        registered: set[str] = set()
        for nb in neighbors:
            rf = engine.resolve(page_index, nb["font"], nb["flags"], nb["text"])
            fontname = PDFDocument._register_resolved(engine, page, rf, registered)
            PDFDocument._insert_run(shape, nb["origin"], nb["text"], nb["size"],
                                    fontname, nb["color"], nb["dir"])
        for edit in edits:
            if edit.deleted:
                continue
            PDFDocument._reinsert_edit(engine, page, page_index, edit,
                                       registered, shape)
        for box in newbox_map.values():
            if box.page_index != page_index or box.deleted:
                continue
            if box.render_mode == 0 and box.cover and len(box.cover) == 7:
                x0, y0, x1, y1, r, g, b = box.cover
                # Lockstep with _apply_page_edits: vacate the cover ONLY when the tile does
                # not fully cover it (reflow to a new footprint, or a move). An in-place
                # edit's tile spans the cover, so skipping the fill keeps any rule / border
                # that passes through the cover intact (non-destructive).
                eir = box.edit_image_rect
                covers = (bool(box.edit_image) and len(eir) == 4
                          and eir[0] <= x0 + 2.0 and eir[1] <= y0 + 2.0
                          and eir[2] >= x1 - 2.0 and eir[3] >= y1 - 2.0)
                if not covers:
                    page.draw_rect(fitz.Rect(x0, y0, x1, y1),
                                   color=(r, g, b), fill=(r, g, b), width=0)
                    # Redraw the rule segment(s) the vacated box sat on, clipped
                    # to the cover -- lockstep with _apply_page_edits. Only when
                    # owner's page indices align with target (see docstring).
                    if owner is not None:
                        owner._restore_line_in_cover(
                            page, (x0, y0, x1, y1), page_index)
                # 0.3.0: blended raster for an edited scanned word / paragraph
                # tile (see _apply_page_edits; keep both seams in lockstep).
                if box.edit_image and box.edit_image_rect:
                    page.insert_image(fitz.Rect(box.edit_image_rect),
                                      stream=box.edit_image, keep_proportion=False,
                                      rotate=page.rotation)
                    if owner is not None:
                        owner._restore_line_in_cover(
                            page, tuple(box.edit_image_rect), page_index)
                    continue
            if box.is_paragraph and box.render_mode == 3:
                continue                # invisible paragraph overlay: scan shows
            rf = engine.resolve_family(box.font_family, box.bold, box.italic,
                                       box.text)
            fontname = PDFDocument._register_resolved(engine, page, rf, registered)
            PDFDocument._insert_run(shape, box.origin, box.text, box.size,
                                    fontname, box.color, box.dir,
                                    render_mode=box.render_mode)
        shape.commit()                  # no-op when nothing was drawn
        # Placed images after the text, before the annot tail -- mirroring
        # ``_apply_page_edits`` exactly (images & signatures §2.4(2)).
        img_boxes = sorted(
            (b for b in (images_map or {}).values()
             if b.page_index == page_index and not b.deleted),
            key=lambda b: b.box_id)
        for box in img_boxes:
            page.insert_image(fitz.Rect(box.rect), stream=box.image,
                              keep_proportion=False, rotate=page.rotation)
        # Annotations LAST, mirroring ``_apply_page_edits`` (annotations §3.4).
        PDFDocument._apply_page_annots_for(page, page_index, annots_map or {},
                                           overrides_map or {})

    @staticmethod
    def _effective_origin_static(span: Span, edit: "Edit") -> tuple:
        if edit.move is None:
            return span.origin
        dx, dy = edit.move
        ox, oy = span.origin
        return (ox + dx, oy + dy)

    def _bake_pending_edits(self) -> None:
        """Realize ALL staged text/box edits INTO ``self.working`` via the
        existing edit pipeline, THEN clear the staged maps (PAGES_SPEC §0.6).
        After this returns, ``self.working`` visually equals what ``save_as``
        would have produced and ``_edits`` / ``_new_boxes`` are empty. Idempotent
        (a no-op when already clean), so a structural op on a clean doc needs no
        Qt. Box ids keep counting up so a later add stays unambiguous."""
        if not self._edits and not self._new_boxes and not self._images \
                and not self._xim_deletes and not self._annots \
                and not self._annot_overrides and not self._form_edits:
            return
        # Staged form fills bake FIRST, before the QGuiApplication guard
        # (forms §2): ``_apply_form_values`` is pure MuPDF, so a form-only
        # bake stays headless. Structural ops therefore bake fills into
        # ``working`` exactly like text edits.
        if self._form_edits:
            self._apply_form_values(self.working)
            self._form_edits.clear()
        # The font resolver needs Qt; guard the same way save_as does so a clean
        # structural op is headless but a dirty one fails loudly rather than
        # aborting the process in QFontDatabase. Annot-only and image-only
        # staging needs no font work, so it stays headless-safe (mirrors the
        # clean-doc no-op).
        from PySide6.QtGui import QGuiApplication
        if (self._edits or self._new_boxes) \
                and QGuiApplication.instance() is None:
            raise RuntimeError(
                "Baking pending edits before a structural op requires a "
                "QGuiApplication (the font resolver uses Qt). Construct a "
                "QApplication, or apply the structural op on a clean document."
            )
        self._bake_doc(self.working, self._edits, self._new_boxes,
                       self._annots, self._annot_overrides, self._images,
                       self._xim_deletes, owner=self)
        self._edits.clear()
        self._new_boxes.clear()
        # Baked images are now real ink in ``working`` (and staged
        # existing-image deletions really removed); the staged maps clear
        # (their _Command entries stay on the model history like every other
        # box kind -- the window's stack rebuild covers the UI).
        self._images.clear()
        self._xim_deletes.clear()
        # Baked annots are now real objects in ``working`` (fresh xrefs): the
        # staged maps clear, and the annot entries are filtered out of the
        # fine-grained history so the model mirror stays sane (the Qt stack is
        # wiped right after by _sync_structural_undo_stack) (§3.3).
        self._annots.clear()
        self._annot_overrides.clear()
        self._annot_records_cache.clear()
        self._undo = [c for c in self._undo
                      if not isinstance(c, _AnnotCommand)]
        self._redo = [c for c in self._redo
                      if not isinstance(c, _AnnotCommand)]

    def _invalidate_caches(self) -> None:
        """Rebind a fresh ``FontEngine`` against the (restructured)
        ``self.working`` so per-doc xref/resolve caches do not point at stale page
        indices (PAGES_SPEC §1.4). The class-level system index + Qt-loaded
        families are process-wide and stay. Also bump the spans() generation +
        clear its memo, since a bake / structural op changed ``self.working``."""
        self.font_engine = FontEngine(self.working)
        self._spans_generation += 1
        self._spans_cache.clear()
        self._wrap_cache.clear()
        self._neighbors_cache.clear()
        self._words_cache.clear()
        self._annot_records_cache.clear()
        self._form_fields_cache.clear()
        self._xim_cache.clear()
        # Border map is keyed to the scan render; a structural op can change page
        # geometry, so drop it (re-detected lazily on next move/bake).
        if getattr(self, "_border_lines_cache", None):
            self._border_lines_cache.clear()
        # Per-region synth-font match memo: keyed to the scan pixels under a cover; a
        # re-OCR / structural op can change which box owns a region, so drop it.
        if getattr(self, "_synth_match_cache", None):
            self._synth_match_cache.clear()

    def _begin_structural(self) -> None:
        """The shared preamble for a MUTATING structural op: STAGE the pre-op
        bytes (so undo restores the exact pre-op working doc) and bake pending
        edits. The snapshot is NOT committed to ``_struct_undo`` yet — it is only
        committed in ``_finish_structural`` AFTER the fitz mutation succeeds, so a
        bake/mutation failure between begin and finish leaves no phantom undo
        entry (PAGES_SPEC §1.5). Capture happens BEFORE the bake so the snapshot
        is the true pre-op working doc."""
        self._pending_snapshot = self.working.tobytes()
        self._bake_pending_edits()

    def _finish_structural(self) -> None:
        """The shared epilogue for a MUTATING structural op: COMMIT the staged
        pre-op snapshot onto the undo stack (the bake + mutation have now
        succeeded), clear redo, enforce the depth cap, rebind caches, and dirty.

        ``structural_dropped_oldest`` records whether the cap evicted the oldest
        snapshot so the UI can drop the matching Qt command in lockstep."""
        self.structural_dropped_oldest = False
        if self._pending_snapshot is not None:
            # A structural op pushed while BELOW the saved structural depth
            # discards the redo branch holding the saved state: invalidate the
            # baseline (clean-index rule) so a later depth coincidence cannot
            # read clean.
            if self._saved_struct_depth > len(self._struct_undo):
                self._saved_struct_depth = -1
            self._struct_undo.append(self._pending_snapshot)
            self._pending_snapshot = None
            self._struct_redo.clear()
            # Enforce the depth cap: dropping the OLDEST snapshot also lowers the
            # saved baseline so dirty tracking stays correct (an invalidated -1
            # baseline stays invalid), and flags the UI to drop its oldest
            # command.
            if len(self._struct_undo) > _STRUCT_UNDO_CAP:
                self._struct_undo.pop(0)
                if self._saved_struct_depth >= 0:
                    self._saved_struct_depth = max(
                        0, self._saved_struct_depth - 1)
                self.structural_dropped_oldest = True
        self._invalidate_caches()
        self._dirty = True

    # --- rotate ----------------------------------------------------------
    def rotate_page(self, i: int, deg: int) -> None:
        """Rotate page ``i`` by ``deg`` (a multiple of 90 in -270..270); the new
        absolute rotation is ``(current + deg) % 360``. Bakes pending edits first,
        then ``page.set_rotation``. Dirty. The existing ``rotation_matrix`` path
        renders the rotated page correctly with no further change."""
        if not 0 <= i < self.page_count:
            raise IndexError(f"page index {i} out of range")
        self._begin_structural()
        page = self.working[i]
        page.set_rotation((page.rotation + deg) % 360)
        self._finish_structural()

    # --- delete ----------------------------------------------------------
    def delete_page(self, i: int) -> None:
        """Delete page ``i``. Refuses to delete the LAST remaining page (raises
        ``ValueError``). Bakes first, then ``self.working.delete_page(i)``."""
        if not 0 <= i < self.page_count:
            raise IndexError(f"page index {i} out of range")
        if self.page_count <= 1:
            raise ValueError("cannot delete the only page")
        self._begin_structural()
        self.working.delete_page(i)
        self._finish_structural()

    # --- insert blank ----------------------------------------------------
    def insert_blank_page(self, at: int, width: float | None = None,
                          height: float | None = None) -> int:
        """Insert a blank page BEFORE index ``at`` (``at == page_count`` appends).
        ``width``/``height`` in PDF points; when ``None``, inherit the size of the
        page currently at ``min(at, page_count-1)`` so a blank matches its
        neighbor. Returns the new page's index (``== at``)."""
        if not 0 <= at <= self.page_count:
            raise IndexError(f"insert index {at} out of range")
        if width is None or height is None:
            ref = self.working[min(at, self.page_count - 1)]
            rect = ref.rect
            width = rect.width if width is None else width
            height = rect.height if height is None else height
        self._begin_structural()
        self.working.new_page(pno=at, width=width, height=height)
        self._finish_structural()
        return at

    # --- duplicate -------------------------------------------------------
    def duplicate_page(self, i: int) -> int:
        """Duplicate page ``i``, inserting the copy immediately AFTER it. Bakes
        first, then ``self.working.fullcopy_page(i, to=i+1)`` (annotations +
        content included). Returns the new copy's index (``i + 1``)."""
        if not 0 <= i < self.page_count:
            raise IndexError(f"page index {i} out of range")
        self._begin_structural()
        self.working.fullcopy_page(i, to=i + 1)
        self._finish_structural()
        return i + 1

    # --- move (reorder) --------------------------------------------------
    def move_page(self, src: int, dst: int) -> None:
        """Reorder: move the page at ``src`` so it ends up at index ``dst`` in the
        FINAL ordering (drag-to-reorder destination-slot semantics, PAGES_SPEC
        §3.5 — ``dst`` is the slot AFTER removal, i.e. python
        ``seq.pop(src); seq.insert(dst, x)``). No-op when ``src == dst``.

        fitz's ``move_page(pno, to)`` inserts BEFORE ``to`` in the ORIGINAL
        indexing and rejects ``to == page_count``; the verified translation is
        ``to = dst if dst <= src else dst + 1`` with the end-append case using
        ``to = -1`` (locked by ``test_move_page_permutations``)."""
        n = self.page_count
        if not 0 <= src < n:
            raise IndexError(f"src index {src} out of range")
        if not 0 <= dst < n:
            raise IndexError(f"dst index {dst} out of range")
        if dst == src:
            return
        self._begin_structural()
        to = dst if dst <= src else dst + 1
        if to >= self.working.page_count:
            self.working.move_page(src, -1)    # append (to == count errors)
        else:
            self.working.move_page(src, to)
        self._finish_structural()

    # --- crop (doc-tools M3) -----------------------------------------------
    def crop_pages(self, pages: list[int], rect: tuple) -> list[int]:
        """Crop ``pages`` to ``rect`` as ONE structural op (doc-tools §2.6).
        ``rect`` is ``(x0, y0, x1, y1)`` in the CURRENT text space of the page
        -- the unrotated rawdict space ``spans()`` / the view's ``_pdf_point``
        use, i.e. relative to the current cropbox origin -- so the same rect
        applies uniformly across an all-pages scope. Per page it is shifted
        into MEDIABOX coordinates (``+ cropbox.tl``: ``set_cropbox`` rects are
        mediabox-relative, probe-verified by the second-crop composition) and
        clamped to the mediabox (``set_cropbox`` raises "CropBox not in
        MediaBox" otherwise). Pages where the clamped box falls below
        ``_MIN_CROP_PT`` on either side are SKIPPED; the skipped indices are
        returned so the window can report them, and ``ValueError`` is raised
        when EVERY page is skipped. The whole pre-pass runs BEFORE the
        snapshot (the guard-before-snapshot rule the stamp ops follow), so a
        rejected call leaves no phantom undo entry.

        Coordinate safety: cropping MOVES the text-space origin -- every span
        coordinate shifts by exactly the new ``cropbox.tl`` delta (probe:
        text at x=72 reads x=22 after a crop at x0=50). ``_begin_structural``
        bakes pending edits BEFORE the origin moves and ``_finish_structural``
        runs ``_invalidate_caches``, so no staged edit or cached span spans
        the shift; the window's post-op ``view.reload()`` rebuilds the layer
        geometry from the new ``page.rect``. Works on /Rotate'd pages (the
        rect is unrotated; the displayed rect swaps sides as usual). Undo:
        one StructuralCommand (the snapshot restores the old cropbox). The
        crop lives in working, so render and save agree for free."""
        indices = self._validate_stamp_pages(pages)
        sel = fitz.Rect(rect)
        sel.normalize()
        if sel.is_empty or sel.is_infinite:
            raise ValueError("crop rectangle is empty")
        targets: list[tuple[int, fitz.Rect]] = []
        skipped: list[int] = []
        for i in indices:
            page = self.working[i]
            cb = page.cropbox
            media = fitz.Rect(sel) + (cb.x0, cb.y0, cb.x0, cb.y0)
            media &= page.mediabox
            if (media.is_empty or media.width < _MIN_CROP_PT
                    or media.height < _MIN_CROP_PT):
                skipped.append(i)
            else:
                targets.append((i, media))
        if not targets:
            raise ValueError(
                f"crop rectangle is smaller than {_MIN_CROP_PT:.0f} pt on "
                f"every selected page")
        self._begin_structural()
        for i, media in targets:
            self.working[i].set_cropbox(media)
        self._finish_structural()
        return skipped

    # --- stamps: watermark / header & footer (doc-tools M2) ----------------
    def _validate_stamp_pages(self, pages) -> list[int]:
        """Shared page-list guard for the stamp + crop ops, run BEFORE the
        structural snapshot so a rejected call leaves no phantom undo entry
        (the same guard-before-snapshot rule as ``set_metadata_fields``).
        Deduplicates while preserving order: stamping the same page twice in
        one op would just double the ink (and darken a translucent
        watermark)."""
        if not pages:
            raise ValueError("no pages selected")
        seen: list[int] = []
        for i in pages:
            if not 0 <= i < self.page_count:
                raise IndexError(f"page index {i} out of range")
            if i not in seen:
                seen.append(i)
        return seen

    @staticmethod
    def _insert_stamp(page: "fitz.Page", text: str, *, code: str,
                      fontsize: float, color: tuple, opacity: float,
                      angle: float, start_disp: tuple,
                      overlay: bool) -> None:
        """One stamped run via the probe-verified rotated-page recipe
        (doc-tools §0): insertion coordinates are UNROTATED text space even on
        /Rotate'd pages, so the displayed-space start point maps through
        ``derotation_matrix``, and the morph pivot rotates by ``angle +
        page.rotation`` so the run reads at ``angle`` ON SCREEN regardless of
        the page's /Rotate. ``rotate=`` kwarg is 90-multiples only; arbitrary
        angles MUST use morph (probe: ``rotate=45`` raises ValueError)."""
        p = fitz.Point(start_disp) * page.derotation_matrix
        page.insert_text(
            p, text, fontsize=fontsize, fontname=code, color=color,
            fill_opacity=opacity,
            morph=(p, fitz.Matrix(1, 1).prerotate(angle + page.rotation)),
            overlay=overlay)

    def add_watermark(self, pages: list[int], *, text: str,
                      base14_code: str = "helv", fontsize: float = 48.0,
                      color: tuple = (0.8, 0.1, 0.1), opacity: float = 0.3,
                      angle: float = 45.0, position: str = "center",
                      behind: bool = False) -> None:
        """Stamp a translucent text watermark across ``pages`` as ONE
        structural op (doc-tools §2.4; one undo step restores the pre-op
        bytes). The run is centered on the 9-grid ``position`` anchor of each
        page's DISPLAYED rect and reads at ``angle`` on screen (positive
        rises to the right); ``behind=True`` inserts below existing content
        (``overlay=False``), so page art covers it where they overlap.

        The stamp lives in ``self.working``: ``render_with_edits`` and
        ``save_as`` both read working, so screen == file with zero extra
        work, and after ``_invalidate_caches`` the stamped text re-extracts
        through ``spans()`` as ORDINARY page content -- editable/deletable
        like any box (documented behavior, not a bug).

        Known hazards, locked by the M2 tests:
        (a) the neighbor heuristic (``_overlapping_neighbors``) drops
            unedited spans >= 50% inside an edited box's redact rects; a
            page-diagonal watermark is far LARGER than any single redact
            rect, so its overlap fraction lands in the redraw band
            (0.02..0.5) and the bake re-draws it (rotated redraw is
            supported) instead of silently truncating it. Extraction-side,
            ``_merge_overlapping``'s direction gate keeps the rotated stamp
            a SEPARATE box even though its axis-aligned bbox blankets the
            page, so editing nearby horizontal text never swallows it.
        (b) the inline editor's flat cover color looks wrong while editing
            OVER a translucent watermark; cosmetic only -- the commit bakes
            correctly through the shared pipeline."""
        if not (text or "").strip():
            raise ValueError("watermark text is empty")
        if not 0.0 < opacity <= 1.0:
            raise ValueError(f"opacity {opacity} outside (0, 1]")
        if fontsize <= 0:
            raise ValueError(f"fontsize {fontsize} must be positive")
        if position not in doctools.GRID_POSITIONS:
            raise ValueError(f"unknown watermark position: {position!r}")
        indices = self._validate_stamp_pages(pages)
        font = fitz.Font(base14_code)   # bad code raises before the snapshot
        run_w = font.text_length(text, fontsize)
        # Glyph-box vertical center above the baseline; with the run width
        # this centers the ink on the anchor (doctools.stamp_start).
        voff = fontsize * (font.ascender + font.descender) / 2.0
        self._begin_structural()
        for i in indices:
            page = self.working[i]
            rect = page.rect    # displayed-space dimensions (rotation-aware)
            anchor = doctools.preset_point(rect.width, rect.height, position)
            self._insert_stamp(
                page, text, code=base14_code, fontsize=fontsize, color=color,
                opacity=opacity, angle=angle,
                start_disp=doctools.stamp_start(anchor, run_w, voff, angle),
                overlay=not behind)
        self._finish_structural()

    def add_header_footer(self, pages: list[int], *, slots: dict,
                          base14_code: str = "helv", fontsize: float = 9.0,
                          color: tuple = (0.0, 0.0, 0.0), top: float = 30.0,
                          bottom: float = 18.0, side: float = 36.0,
                          start_at: int = 1) -> None:
        """Stamp header/footer lines with page-number tokens across ``pages``
        as ONE structural op (doc-tools §2.5). ``slots`` maps
        ``doctools.HF_SLOTS`` names to template strings; empty/missing slots
        are skipped. Tokens ``{page}`` / ``{pages}`` / ``{date}`` are
        substituted AT STAMP TIME (``{page}`` counts ``start_at + k`` over
        the given pages, ``{pages}`` is the document page count, ``{date}``
        is today as %Y-%m-%d): the stamped text is static content, so later
        reorders do not renumber (the dialog states this).

        Geometry is computed in the DISPLAYED page rect -- baseline y =
        ``top`` (header) or ``rect.height - bottom`` (footer); x = ``side`` /
        centered / right-aligned via ``Font.text_length`` -- then mapped
        through the same derotation + morph recipe as the watermark, so a
        /Rotate'd page gets an upright header in its displayed band. Opacity
        1, always overlay. Undo/save/WYSIWYG: identical to the watermark."""
        unknown = set(slots) - set(doctools.HF_SLOTS)
        if unknown:
            raise ValueError(f"unknown header/footer slot: {sorted(unknown)}")
        filled = {k: v for k, v in slots.items() if (v or "").strip()}
        if not filled:
            raise ValueError("every header/footer slot is empty")
        if fontsize <= 0:
            raise ValueError(f"fontsize {fontsize} must be positive")
        indices = self._validate_stamp_pages(pages)
        font = fitz.Font(base14_code)
        date = _dt.date.today().strftime("%Y-%m-%d")
        total = self.page_count
        self._begin_structural()
        for k, i in enumerate(indices):
            page = self.working[i]
            rect = page.rect
            for slot, template in filled.items():
                line = doctools.substitute_tokens(
                    template, page_no=start_at + k, total=total, date=date)
                run_w = font.text_length(line, fontsize)
                band, align = slot.split("_", 1)
                if align == "left":
                    x = side
                elif align == "center":
                    x = (rect.width - run_w) / 2.0
                else:
                    x = rect.width - side - run_w
                y = top if band == "header" else rect.height - bottom
                self._insert_stamp(
                    page, line, code=base14_code, fontsize=fontsize,
                    color=color, opacity=1.0, angle=0.0, start_disp=(x, y),
                    overlay=True)
        self._finish_structural()

    # --- merge (combine / append) ----------------------------------------
    def merge(self, other: "str | PDFDocument", at: int | None = None) -> int:
        """Insert ALL pages of ``other`` into this document. ``other`` may be a
        path (opened read-only + closed here) or another open ``PDFDocument``
        (its WORKING doc is the source, with its unsaved edits BAKED into a
        throwaway copy so ``other`` is never mutated). ``at`` is the insertion
        index (``None`` == append). Bakes THIS doc first, then ``insert_pdf``.
        Returns the index of the FIRST inserted page."""
        n = self.page_count
        if at is None:
            first = n
        else:
            if not 0 <= at <= n:
                raise IndexError(f"merge index {at} out of range")
            first = at
        self._begin_structural()
        if isinstance(other, PDFDocument):
            tmp = fitz.open("pdf", other.working.tobytes())
            try:
                # Bake other's pending edits into the throwaway copy so they
                # come along, WITHOUT mutating other or clearing its edits.
                # The OTHER doc applies its own staged form values (forms §2);
                # widgets ride insert_pdf (probe §0).
                self._bake_doc(tmp, other._edits, other._new_boxes,
                               other._annots, other._annot_overrides,
                               other._images, other._xim_deletes)
                other._apply_form_values(tmp)
                if at is None:
                    self.working.insert_pdf(tmp)
                else:
                    self.working.insert_pdf(tmp, start_at=at)
            finally:
                tmp.close()
        else:
            src = fitz.open(other)
            try:
                if at is None:
                    self.working.insert_pdf(src)
                else:
                    self.working.insert_pdf(src, start_at=at)
            finally:
                src.close()
        self._finish_structural()
        return first

    # --- extract (selected pages -> a NEW pdf; non-mutating) -------------
    def extract_pages(self, indices: list[int], out_path: str | None = None
                      ) -> "str | bytes":
        """Build a NEW PDF containing ONLY the pages in ``indices`` (in the given
        order; duplicates allowed). Does NOT modify this document (PAGES_SPEC
        §3.7): it bakes a COPY of ``self.working`` so edits are realized in the
        extract without touching ``self.working`` or clearing this doc's edits.
        Writes ``out_path`` (atomic temp+replace) and returns it, else returns the
        PDF bytes. Requires a ``QGuiApplication`` only when edits are pending."""
        for idx in indices:
            if not 0 <= idx < self.page_count:
                raise IndexError(f"extract index {idx} out of range")
        tmp = fitz.open("pdf", self.working.tobytes())
        out = fitz.open()
        try:
            self._bake_doc(tmp, self._edits, self._new_boxes,
                           self._annots, self._annot_overrides, self._images,
                           self._xim_deletes)
            self._apply_form_values(tmp)        # fills ride along (forms §2)
            for idx in indices:
                out.insert_pdf(tmp, from_page=idx, to_page=idx)
            if out_path is None:
                return out.tobytes()
            self._atomic_write(out, out_path)
            return out_path
        finally:
            out.close()
            tmp.close()

    # --- split (one pdf -> multiple files; non-mutating) -----------------
    def split(self, ranges: list[tuple[int, int]], out_dir: str,
              stem: str | None = None) -> list[str]:
        """Write one output PDF per ``(start, end)`` INCLUSIVE page range into
        ``out_dir``. File names: ``f"{stem or basename}-{n:02d}.pdf"`` (1-based
        n). Each output is built like ``extract_pages([start..end])`` on a baked
        COPY. Does NOT modify this document. Returns the written paths."""
        n_pages = self.page_count
        for (start, end) in ranges:
            if not (0 <= start <= end < n_pages):
                raise IndexError(f"split range ({start},{end}) out of range")
        if stem is None:
            stem = os.path.splitext(os.path.basename(self.path))[0]
        tmp = fitz.open("pdf", self.working.tobytes())
        written: list[str] = []
        try:
            self._bake_doc(tmp, self._edits, self._new_boxes,
                           self._annots, self._annot_overrides, self._images,
                           self._xim_deletes)
            self._apply_form_values(tmp)        # fills ride along (forms §2)
            for n, (start, end) in enumerate(ranges, start=1):
                out = fitz.open()
                try:
                    out.insert_pdf(tmp, from_page=start, to_page=end)
                    path = os.path.join(out_dir, f"{stem}-{n:02d}.pdf")
                    self._atomic_write(out, path)
                    written.append(path)
                finally:
                    out.close()
        finally:
            tmp.close()
        return written

    @staticmethod
    def _atomic_write(out: "fitz.Document", out_path: str,
                      save_kwargs: dict | None = None) -> None:
        """Stage to a temp file in the destination dir then ``os.replace`` (the
        same atomic write ``save_as`` uses) so a mid-write crash never corrupts
        an existing target. ``save_kwargs`` layers extra ``Document.save``
        options over the baseline ``garbage=4, deflate=True``: ``save_as``
        passes ``_save_kwargs()`` (pending encryption) and
        ``save_optimized_copy`` adds the optimize flags; extract/split keep
        the plain baseline (their outputs are new, unprotected files)."""
        out_dir = os.path.dirname(os.path.abspath(out_path)) or "."
        fd, tmp = tempfile.mkstemp(suffix=".pdf", dir=out_dir)
        os.close(fd)
        try:
            out.save(tmp, garbage=4, deflate=True, **(save_kwargs or {}))
            os.replace(tmp, out_path)
        except Exception:
            if os.path.exists(tmp):
                os.remove(tmp)
            raise

    # --- thumbnails ------------------------------------------------------
    def render_thumbnail(self, page_index: int, max_px: int = 180
                         ) -> "fitz.Pixmap":
        """A small pixmap of page ``page_index`` from ``self.working`` (NO staged
        text edits baked — thumbnails convey page STRUCTURE / order / rotation,
        and baking per-thumbnail is too costly; after any structural op the edits
        are already baked into ``working`` so post-op thumbnails are accurate).
        The long edge is scaled to ``max_px`` at 72dpi."""
        if not 0 <= page_index < self.page_count:
            raise IndexError(f"page index {page_index} out of range")
        page = self.working[page_index]
        rect = page.rect
        long_edge = max(rect.width, rect.height, 1.0)
        scale = max_px / long_edge
        return page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)

    # --- structural undo / redo ------------------------------------------
    @property
    def can_undo_structural(self) -> bool:
        return bool(self._struct_undo)

    @property
    def can_redo_structural(self) -> bool:
        return bool(self._struct_redo)

    def undo_structural(self) -> bool:
        """Restore the previous working-doc snapshot (PAGES_SPEC §1.5/§3.10).
        BAKES any state staged AFTER the structural op into the current bytes
        FIRST (the tab-switch stack rebuild makes this undo reachable with
        live staged state; without the bake that state was silently destroyed
        -- or worse, surviving staged annots/fills reattached to the wrong
        restored page), then pushes those bytes onto the redo stack so redo
        recovers everything the screen showed, swaps ``self.working`` to the
        popped snapshot, and rebinds the FontEngine. The bake leaves every
        staged map empty, so the staged-edit flag clears truthfully and
        overall dirtiness is the depth-vs-baseline test (the ``dirty``
        property); undoing back to the saved state reads clean. Returns
        ``True`` if it changed ``working``."""
        if not self._struct_undo:
            return False
        self._bake_pending_edits()
        self._struct_redo.append(self.working.tobytes())
        snapshot = self._struct_undo.pop()
        self.working.close()
        self.working = fitz.open("pdf", snapshot)
        self._invalidate_caches()
        # Staged maps are empty (baked above). If the last save baked staged
        # state into the disk file, every structural snapshot (PRE-bake bytes)
        # differs from the disk even at the saved depth, so the doc must stay
        # dirty; otherwise dirtiness is purely the depth-vs-baseline test.
        self._dirty = bool(self._saved_had_staged)
        return True

    def redo_structural(self) -> bool:
        """Re-apply the most recently undone structural snapshot. Symmetric to
        ``undo_structural``: state staged since the undo is baked into the
        bytes pushed onto the undo stack, so undoing again recovers it."""
        if not self._struct_redo:
            return False
        self._bake_pending_edits()
        self._struct_undo.append(self.working.tobytes())
        snapshot = self._struct_redo.pop()
        self.working.close()
        self.working = fitz.open("pdf", snapshot)
        self._invalidate_caches()
        # Same disk-vs-snapshot reasoning as undo_structural.
        self._dirty = bool(self._saved_had_staged)
        return True

    def set_path(self, path: str) -> None:
        """Repoint the working path after a successful Save As so subsequent
        saves target the new file. The window reloads the doc from ``path``
        afterward (which rebuilds spans/xrefs against the saved output)."""
        self.path = path
