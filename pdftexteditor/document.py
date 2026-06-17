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

    @property
    def rotation_degrees(self) -> float:
        """Writing-direction angle in degrees, CCW positive. line['dir'] is
        (cos θ, -sin θ) in PDF space, so θ = atan2(-dir[1], dir[0])."""
        return math.degrees(math.atan2(-self.dir[1], self.dir[0]))


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
    def rotation_degrees(self) -> float:
        return 0.0

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
    def rotation_degrees(self) -> float: ...
    @property
    def identity(self) -> tuple: ...


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

    @property
    def rotation_degrees(self) -> float:
        return math.degrees(math.atan2(-self.dir[1], self.dir[0]))

    def with_changes(self, **kwargs) -> "NewBox":
        return replace(self, **kwargs)


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
    def rotation_degrees(self) -> float:
        return 0.0

    @property
    def dir(self) -> tuple:
        return (1.0, 0.0)

    @property
    def redact_rects(self) -> tuple:
        """An ImageBox has no original page ink, so it contributes NO
        redaction (Box-protocol parity with NewBox; unused by the bake)."""
        return ()

    def with_changes(self, **kwargs) -> "ImageBox":
        return replace(self, **kwargs)


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
    def rotation_degrees(self) -> float:
        return 0.0

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

    def _scanned_edit_raster(self, box: "NewBox", new_text: str):
        """For an edited scanned-OCR box (one carrying a 7-tuple cover), render
        ``new_text`` in the matched bundled font, recover the scan's ink/paper and
        local dropout severity from the word's region, hard-damage it (ocr.degrade),
        and return ``(png_bytes, rect)`` to draw over the cover so the edit matches
        the scan's colour + degradation. None if anything is unavailable (the caller
        then falls back to plain vector text). Non-rotated pages (the common scan)."""
        import io
        import numpy as np
        cover = box.cover
        if not (cover and len(cover) == 7) or not new_text.strip():
            return None
        try:
            import cv2
            from .ocr import degrade, fontmatch
            # The raster is rotation-naive (placed axis-aligned over the cover), so
            # on a /Rotate page it would land wrong. Fall back to vector text there.
            if self.page_rotation(box.page_index) % 360 != 0:
                return None
            x0, y0, x1, y1 = (float(c) for c in cover[:4])
            fpath = os.path.join(fontmatch._DIR,
                                 fontmatch._CANDIDATES.get(box.font_family, ""))
            if not fontmatch._CANDIDATES.get(box.font_family) or not os.path.exists(fpath):
                return None
            dpi = 300.0
            ppi = dpi / 72.0
            page_rgb = self.render_page_image(box.page_index, dpi)
            H, W = page_rgb.shape[:2]
            rx0, ry0 = max(0, int(x0 * ppi)), max(0, int(y0 * ppi))
            rx1, ry1 = min(W, int(x1 * ppi)), min(H, int(y1 * ppi))
            region = page_rgb[ry0:ry1, rx0:rx1]
            if region.size == 0 or region.shape[0] < 4 or region.shape[1] < 6:
                return None
            g = region.mean(2)
            paper_guess = float(np.percentile(g, 85))
            dark = g < paper_guess * 0.7                     # scanned ink pixels
            ink, paper = degrade.sample_ink_paper(region, dark)
            m = dark.astype(np.uint8)
            if int(m.sum()) >= 8:                            # dropout fraction -> severity
                closed = cv2.morphologyEx(m, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))
                sev = float(np.clip((int(closed.sum()) - int(m.sum())) /
                                    max(int(closed.sum()), 1), 0.05, 0.6))
            else:
                sev = 0.25
            f = fitz.Font(fontfile=fpath)
            em = max(8.0, float(box.size))
            runw = f.text_length(new_text, em)
            doc = fitz.open()
            pg = doc.new_page(width=runw + 2 * em, height=em * 3)
            tw = fitz.TextWriter(pg.rect)
            tw.append((em, em * 2.0), new_text, font=f, fontsize=em)
            tw.write_text(pg, color=(0.06, 0.06, 0.06))
            pm = pg.get_pixmap(matrix=fitz.Matrix(ppi, ppi), alpha=False)
            wr = np.frombuffer(pm.samples, np.uint8).reshape(
                pm.height, pm.width, pm.n)[..., :3].copy()
            cov = (255 - wr.mean(2)) / 255.0
            ys = np.where(cov.max(1) > 0.12)[0]
            xs = np.where(cov.max(0) > 0.12)[0]
            if not len(ys) or not len(xs):
                return None
            wr = wr[ys.min():ys.max() + 1, xs.min():xs.max() + 1]
            deg = degrade.degrade_patch(wr, ink, paper, sev,
                                        seed=box.box_id * 131 + len(new_text))
            ok, buf = cv2.imencode(".png", cv2.cvtColor(deg, cv2.COLOR_RGB2BGR))
            if not ok:
                return None
            # Fill the ORIGINAL word's box so the edit can never overflow into the
            # neighbouring scanned words (a same-length edit barely distorts; this
            # avoids the "fortydays" abutment). insert_image scales to this rect.
            rect = (x0, y0, x1, y1)
            return bytes(buf), rect
        except Exception:
            return None

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
            from .ocr import fontmatch
            from .reflow import wrap_paragraph
            x0, y0, x1, y1 = (float(c) for c in cover[:4])
            pr, pg, pb = (float(c) for c in cover[4:7])
            area_w, area_h = x1 - x0, y1 - y0
            if area_w < 4 or area_h < 4:
                return None
            fpath = os.path.join(fontmatch._DIR,
                                 fontmatch._CANDIDATES.get(box.font_family, ""))
            if not fontmatch._CANDIDATES.get(box.font_family) \
                    or not os.path.exists(fpath):
                return None
            em = max(8.0, float(box.size))
            lead = box.leading or em * 1.3
            col_w = max(8.0, (box.box_w or area_w) - 4.0)
            f = fitz.Font(fontfile=fpath)
            # Draw on a tile sized to the AREA (PDF pts); wrap to the column.
            doc = fitz.open()
            pg_ = doc.new_page(width=area_w, height=area_h)
            pg_.draw_rect(pg_.rect, color=(pr, pg, pb), fill=(pr, pg, pb), width=0)
            tw = fitz.TextWriter(pg_.rect)
            result = wrap_paragraph(new_text, f, em, 2.0, em, col_w, leading=lead)
            drew = False
            for ln in result.lines:
                if ln.text and 0 <= ln.origin[1] <= area_h + em:
                    tw.append((ln.origin[0], ln.origin[1]), ln.text,
                              font=f, fontsize=em)
                    drew = True
            if drew:
                tw.write_text(pg_, color=(0.1, 0.1, 0.1))
            ppi = 300.0 / 72.0
            pm = pg_.get_pixmap(matrix=fitz.Matrix(ppi, ppi), alpha=False)
            rgb = np.frombuffer(pm.samples, np.uint8).reshape(
                pm.height, pm.width, pm.n)[..., :3].copy()
            ok, buf = cv2.imencode(".png", cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR))
            if not ok:
                return None
            return bytes(buf), (x0, y0, x1, y1)
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
        """A deep, hashable-enough snapshot of the manual-group state for undo."""
        return (
            {p: list(v) for p, v in self._manual_groups.items()},
            {p: set(v) for p, v in self._manual_ungroups.items()},
        )

    def restore_manual_grouping(self, snap: tuple) -> None:
        groups, ungroups = snap
        self._manual_groups = {p: list(v) for p, v in groups.items()}
        self._manual_ungroups = {p: set(v) for p, v in ungroups.items()}
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
        """Split ``box`` (a ParagraphBox) back into its individual lines. If it
        was a MANUAL group, drop that group; if it is an AUTO-detected paragraph,
        record an ungroup override so it stays split. Returns True on change."""
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
    # each mutator is a few lines. Existing spans key by (page, span.key);
    # NewBoxes key by their edit_key.

    @staticmethod
    def _span_edit_key(page_index: int, span: Span) -> tuple:
        return (page_index, span.key)

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
            if cur is None or new_text == cur.text:
                return
            before = self._newbox_state(key)
            updated = replace(cur, text=new_text)
            # An invisible OCR overlay (render_mode 3) that the user edits becomes
            # real, visible text: flip it to fill so it draws, and its cover now
            # paints, whiting out the scanned word it replaces. Untouched OCR
            # words stay invisible over the original scan pixels.
            if cur.render_mode != 0:
                updated = replace(updated, render_mode=0)
            # 0.3.0: a scanned-OCR word carries a cover; render its edit as a
            # recolored + hard-damaged raster (sampled from the scan under the
            # cover) so it blends, instead of drawing crisp vector text. Falls back
            # to plain text if the raster cannot be built.
            if cur.cover and len(cur.cover) == 7:
                # A paragraph area reflows as a whole-tile raster (rotation-safe);
                # a single word uses the per-word degrade raster (precise blend,
                # non-rotated only). Either falls back to plain text on failure.
                raster = (self._scanned_paragraph_raster(updated, new_text)
                          if cur.is_paragraph
                          else self._scanned_edit_raster(updated, new_text))
                if raster is not None:
                    updated = replace(updated, edit_image=raster[0],
                                      edit_image_rect=raster[1])
                else:
                    updated = replace(updated, edit_image=b"", edit_image_rect=())
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
        adjacent same-style runs. None/empty -> None (uniform)."""
        if not runs:
            return None
        out: list[list] = []
        for t, b, i in runs:
            if not t:
                continue
            b, i = bool(b), bool(i)
            if out and out[-1][1] == b and out[-1][2] == i:
                out[-1][0] += t
            else:
                out.append([t, b, i])
        if not out:
            return None
        return tuple((t, b, i) for t, b, i in out)

    def staged_runs(self, page_index: int, box) -> tuple | None:
        """The box's staged rich runs (tuple of (text, bold, italic)), or None
        when the box is uniform (no per-selection styling staged)."""
        if isinstance(box, NewBox):
            return None
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

    # --- move (NEW) ---
    def move_box(self, page: int, box, dx: float, dy: float) -> None:
        """Translate ``box`` by (dx, dy) PDF points (cumulative with any prior
        move) and push ONE 'move' command (BUILD_SPEC §1.5). For a Span this
        accumulates ``Edit.move``; for a NewBox it shifts origin + bbox."""
        if abs(dx) < 1e-9 and abs(dy) < 1e-9:
            return
        if isinstance(box, NewBox):
            key = box.edit_key
            before = self._newbox_state(key)
            b = self._new_boxes.get(key)
            if b is None:
                return
            ox, oy = b.origin
            x0, y0, x1, y1 = b.bbox
            updated = replace(b, origin=(ox + dx, oy + dy),
                              bbox=(x0 + dx, y0 + dy, x1 + dx, y1 + dy))
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
                leading: float = 0.0, alignment: str = "left") -> NewBox:
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
                     box_w=box_w, leading=float(leading), alignment=alignment)
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
        # A multi-line PARAGRAPH (OCR area) is bounded by its known area rect, the
        # cover -- already in this page's text space and rotation-correct. Use it
        # directly so the selection frame / editor column span the whole block
        # (single-line metric math can't, and doesn't know the line count).
        if box.is_paragraph and box.cover and len(box.cover) == 7:
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
        edit = self._edits.get((page_index, box.key))
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
        edit = self._edits.get((page_index, box.key))
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
            return {"font_family": cur.font_family, "size": cur.size,
                    "color": cur.color, "bold": cur.bold, "italic": cur.italic}
        edit = self._edits.get((page, box.key))
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
            run_styles = {(b, i) for _, b, i in edit.runs}
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
        neighbors = self._overlapping_neighbors_cached(
            out, page_index, edited_keys, redact_rects
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
                continue
            # OCR cover: paint the scanned text region in the paper color FIRST
            # (its own committed shape, so it sits under the text shape) so the
            # edited text replaces the scan glyphs instead of doubling them. Only
            # for a VISIBLE box: an invisible OCR overlay (render_mode 3) must let
            # the untouched scan show through, so it carries its cover unused
            # until the word is edited (which flips it visible).
            if box.render_mode == 0 and box.cover and len(box.cover) == 7:
                x0, y0, x1, y1, r, g, b = box.cover
                page.draw_rect(fitz.Rect(x0, y0, x1, y1),
                               color=(r, g, b), fill=(r, g, b), width=0)
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
            for t, b, i in edit.runs:
                by_style.setdefault((b, i), []).append(t)
            for style_key, parts in by_style.items():
                rf_k = PDFDocument._resolve_run(engine, page_index, box, edit,
                                                "".join(parts), *style_key)
                fonts[style_key] = engine.fitz_font_for(rf_k)
            return wrap_rich(
                [(t, (b, i)) for t, b, i in edit.runs], fonts, size, left,
                first_y, width, alignment=align, leading=leading,
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
            for t, b, i in edit.runs:
                rf_run = PDFDocument._resolve_run(engine, page_index, span,
                                                  edit, t, b, i)
                fname = PDFDocument._register_resolved(engine, page, rf_run,
                                                       registered)
                w = engine.fitz_font_for(rf_run).text_length(t, fontsize=size)
                drawn.append((t, fname, w))
                total_w += w
            origin = PDFDocument._maybe_recenter(page, span, edit, origin,
                                                 total_w)
            x, y = origin
            for t, fname, w in drawn:
                PDFDocument._insert_run(shape, (x, y), t, size, fname, color,
                                        span.dir)
                x += w
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
        if not span.is_horizontal or edit.move is not None:
            return origin
        pw = float((page.rect * page.derotation_matrix).width)
        cx = (span.bbox[0] + span.bbox[2]) / 2.0
        if pw > 0 and abs(cx - pw / 2.0) <= pw * 0.06:
            return (cx - new_width / 2.0, origin[1])
        return origin

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
            # One resolve/register per distinct style key, with ALL of that
            # style's text so glyph-coverage checks see every character.
            by_style: dict[tuple, list] = {}
            for t, b, i in edit.runs:
                by_style.setdefault((b, i), []).append(t)
            fonts: dict[tuple, object] = {}
            fnames: dict[tuple, str] = {}
            for style_key, parts in by_style.items():
                rf_k = PDFDocument._resolve_run(engine, page_index, box, edit,
                                                "".join(parts), *style_key)
                fnames[style_key] = PDFDocument._register_resolved(
                    engine, page, rf_k, reg)
                fonts[style_key] = engine.fitz_font_for(rf_k)
            result = wrap_rich(
                [(t, (b, i)) for t, b, i in edit.runs], fonts, size, left,
                first_y, width, alignment=align, leading=leading,
                line_spacing=spacing,
            )
            for ln in result.lines:
                for seg in ln.segments:
                    PDFDocument._insert_run(
                        shape, (seg.x, ln.origin[1]), seg.text, size,
                        fnames[seg.style], color, (1.0, 0.0),
                    )
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
        edit = self._edits.get((page, box.key))
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
        edit = self._edits.get((page, box.key))
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
                    self._resolve_run(engine, page, box, edit, t, b, i)
                ).text_length(t, fontsize=size)
                for t, b, i in edit.runs
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
        edit = self._edits.get((page, box.key))
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
        )
        hit = self._neighbors_cache.get(key)
        if hit is not None:
            return hit
        result = self._overlapping_neighbors(
            out, page_index, edited_keys, redact_rects
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
    ) -> list[dict]:
        """Unedited spans on ``page_index`` whose ink overlaps a redaction rect.

        Returns a list of dicts (text/origin/size/color/font/flags) for each
        neighbor span that is NOT itself being edited but whose bbox materially
        intersects one of the edited spans' redaction rects. These are redrawn
        after redaction so the rect does not silently truncate them. The overlap
        must be a real area intersection (more than a hairline touch) so spans
        that merely abut an edited rect are not needlessly redrawn.
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
                    for rect in redact_rects:
                        inter = sbbox & rect
                        if not inter.is_empty:
                            inside += inter.get_area()
                    frac = min(inside / sarea, 1.0)
                    if frac < 0.02 or frac >= 0.5:
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
                  xim_map: dict | None = None) -> None:
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
                                              xim_map)

    @staticmethod
    def _apply_page_edits_for(target: "fitz.Document", engine: "FontEngine",
                              page_index: int, edits: list["Edit"],
                              newbox_map: dict,
                              annots_map: dict | None = None,
                              overrides_map: dict | None = None,
                              images_map: dict | None = None,
                              xim_map: dict | None = None) -> None:
        """``_apply_page_edits`` for ARBITRARY newbox/annot/image maps (used
        when baking a foreign doc whose staged state lives in the passed maps,
        not ``self``). The body mirrors ``_apply_page_edits`` exactly but
        draws the foreign maps' state for ``page_index`` instead of
        ``self``'s -- keep BOTH in lockstep (the screen == save invariant)."""
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
        neighbors = PDFDocument._overlapping_neighbors(
            target, page_index, edited_keys, redact_rects)
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
                page.draw_rect(fitz.Rect(x0, y0, x1, y1),
                               color=(r, g, b), fill=(r, g, b), width=0)
                # 0.3.0: blended raster for an edited scanned word / paragraph
                # tile (see _apply_page_edits; keep both seams in lockstep).
                if box.edit_image and box.edit_image_rect:
                    page.insert_image(fitz.Rect(box.edit_image_rect),
                                      stream=box.edit_image, keep_proportion=False,
                                      rotate=page.rotation)
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
                       self._xim_deletes)
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
