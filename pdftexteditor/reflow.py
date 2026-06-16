"""The WRAP / REFLOW LAYOUT ENGINE -- the WYSIWYG keystone (REFLOW_SPEC §R2).

ONE pure function, ``wrap_paragraph``, computes the line layout of a paragraph
from its text + the resolved ``fitz.Font`` + size + column width + alignment +
leading. It is imported by BOTH the model's bake path (``_apply_page_edits``,
hence ``save_as``) AND the on-screen preview, so the screen is the same bytes as
the saved file BY CONSTRUCTION (invariant §R0.1). We NEVER delegate wrapping to
``insert_textbox`` -- its wrap algorithm is not guaranteed to match our on-screen
measurement pixel-for-pixel. We measure with the SAME ``fitz.Font`` object the
save path draws each line with (``engine.fitz_font_for(rf)``), so the wrap is
deterministic and identical on both paths.

Verified in the venv (PyMuPDF 1.27.2.3): ``fitz.Font.text_length(s, fontsize=k)``
is exactly additive over characters (no kerning) for both the base-14 faces and
the document's embedded Times subset, so the greedy fill is reproducible and the
re-wrap of unchanged text lands the original breaks (see ``_WIDTH_SLACK`` below).

This module is Qt-free and import-light (only ``dataclasses`` + a TYPE_CHECKING
hint), so it can be unit-tested headless without constructing a QApplication.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    import fitz


# When re-wrapping a grouped paragraph's UNCHANGED text we want the ORIGINAL line
# breaks back to within sub-pixel tolerance (the "leave zero trace" property,
# §R2.6). The measured column width ``Rx - Lx`` is the max observed line right
# edge, but (a) the resolved face's ``text_length`` differs from the extractor's
# reported bbox width by a few tenths of a point, and (b) the widest original
# line must still FIT on re-wrap. A small slack (one space-width's worth) absorbs
# that metric noise and lands the wrap inside the band that reproduces the source
# breaks (measured on paragraphs.pdf: band [328.08, 339.66], measured width
# 327.80 -- the slack lifts it into the band). It is intentionally tiny so it
# never lets an extra word onto a line that the author broke before.
_WIDTH_SLACK_SPACES = 1.0


@dataclass(frozen=True)
class LaidOutLine:
    """One wrapped line of a paragraph, ready to draw at its baseline.

    For left / center / right lines ``word_origins`` is empty and the whole
    ``text`` is drawn as ONE ``insert_text`` at ``origin``. For a JUSTIFIED line
    (every line except the last and single-word lines) ``word_origins`` carries
    ``(word, x)`` per word so the bake draws each word separately at its
    stretched x -- inter-word stretch is a DRAW concern that ``text_length``
    (additive) cannot encode in a single string.
    """

    text: str            # the substring of this wrapped line (no trailing space)
    origin: tuple        # baseline (x, y) in PDF points where this line is drawn
    width: float         # the line's measured advance at fontsize (PDF points)
    word_origins: tuple = field(default=())  # tuple[(word, x), ...] for justify; () otherwise


@dataclass(frozen=True)
class WrapResult:
    """The full laid-out paragraph: its lines (top to bottom), the union ink
    bbox, and the baseline-to-baseline leading actually used."""

    lines: tuple         # tuple[LaidOutLine, ...] in top-to-bottom order
    bbox: tuple          # union (x0, y0, x1, y1) of the laid-out text, PDF points
    leading: float       # baseline-to-baseline used (PDF points)


def _tokenize(text: str) -> list[str]:
    """Split on any whitespace run into word tokens (no empty tokens). NBSP and
    other space variants are normalized to a regular space upstream (the
    ParagraphBox joins member texts with ASCII spaces), so ``str.split`` here
    breaks on every gap the author intended."""
    return text.split()


def _break_overlong_tokens(
    tokens: list[str], font: "fitz.Font", size: float, column_width: float
) -> list[str]:
    """Split any token whose own advance exceeds ``column_width`` into character-
    level chunks that each fit the column, so a long unbreakable word / URL /
    no-space (CJK) run wraps instead of overflowing the page (REFLOW_SPEC §R2).

    Tokens that already fit pass through UNCHANGED, so ordinary prose is
    byte-for-byte identical to before. An over-wide token is filled greedily one
    character at a time; a single character wider than the whole column (a
    degenerate/zero column) is still emitted alone so the function always
    terminates. The chunks become ordinary tokens, so the downstream greedy fill
    treats them like any other word (no special-casing in the layout loop)."""
    if column_width <= 0:
        return tokens
    out: list[str] = []
    for tok in tokens:
        if font.text_length(tok, fontsize=size) <= column_width:
            out.append(tok)
            continue
        chunk: list[str] = []
        chunk_w = 0.0
        for ch in tok:
            cw = font.text_length(ch, fontsize=size)
            if chunk and (chunk_w + cw) > column_width:
                out.append("".join(chunk))
                chunk = [ch]
                chunk_w = cw
            else:
                chunk.append(ch)
                chunk_w += cw
        if chunk:
            out.append("".join(chunk))
    return out


def wrap_paragraph(
    text: str,
    font: "fitz.Font",
    fontsize: float,
    box_left: float,
    first_baseline_y: float,
    column_width: float,
    *,
    alignment: str = "left",
    leading: float | None = None,
    line_spacing: float = 1.0,
) -> WrapResult:
    """Greedy word-wrap ``text`` into ``column_width`` using ``font``'s OWN glyph
    advances (``text_length``), the single source of truth for both the on-screen
    preview and the saved file (§R2.0).

    Parameters mirror REFLOW_SPEC §R2.1:
      * ``font`` is the resolved face (``engine.fitz_font_for(rf)``) -- the SAME
        object the bake draws each line with, so screen == file.
      * ``box_left`` is the column's left edge x0 (PDF pts); ``first_baseline_y``
        the y of the FIRST line's baseline.
      * ``column_width`` is ``Rx - Lx`` (the measured paragraph column). A small
        internal slack (``_WIDTH_SLACK_SPACES`` space-widths) is added so the
        re-wrap of UNCHANGED text reproduces the original breaks (§R2.6).
      * ``alignment`` in {"left","center","right","justify"}.
      * ``leading`` is baseline-to-baseline (PDF pts); ``None`` -> 1.2*fontsize.
        For a grouped ParagraphBox we PASS the measured leading so reflow of
        unchanged text reproduces the original spacing exactly.
      * ``line_spacing`` multiplies the leading.

    Pure + deterministic. Returns a ``WrapResult``.
    """
    size = max(0.01, fontsize)
    lead = leading if leading is not None else 1.2 * size
    effective_lead = lead * (line_spacing if line_spacing and line_spacing > 0 else 1.0)
    space_w = font.text_length(" ", fontsize=size)
    # The effective fit width: the measured column plus a tiny metric-noise slack.
    fit_width = column_width + _WIDTH_SLACK_SPACES * space_w

    tokens = _tokenize(text)
    # A token wider than the column on its own (a long URL, an unbreakable word,
    # or no-space / CJK text where ``str.split`` yields one giant token) is
    # broken at the CHARACTER level so its ink wraps inside the column instead of
    # running off the page edge (REFLOW_SPEC §R2; the cover/overlay are built from
    # the bbox below, so an off-column line would leave the original ink showing
    # through and draw glyphs past the MediaBox). Each over-wide token is split
    # greedily into chunks that each fit the column; tokens that already fit pass
    # through untouched so normal prose is byte-for-byte unchanged.
    tokens = _break_overlong_tokens(tokens, font, size, column_width)

    # --- greedy fill: list of (words_in_line, advance_without_justify) ---------
    raw_lines: list[tuple[list[str], float]] = []
    cur: list[str] = []
    cur_adv = 0.0
    for tok in tokens:
        tok_w = font.text_length(tok, fontsize=size)
        need = (space_w if cur else 0.0) + tok_w
        if cur and (cur_adv + need) > fit_width:
            raw_lines.append((cur, cur_adv))
            cur = [tok]
            cur_adv = tok_w
        else:
            cur.append(tok)
            cur_adv += need
    if cur:
        raw_lines.append((cur, cur_adv))
    if not raw_lines:
        # Empty paragraph: a single empty line at the baseline so the box still
        # has a place to mount an editor and a non-degenerate bbox.
        raw_lines = [([], 0.0)]

    n_lines = len(raw_lines)
    last_idx = n_lines - 1

    out_lines: list[LaidOutLine] = []
    for i, (words, advance) in enumerate(raw_lines):
        baseline_y = first_baseline_y + i * effective_lead
        joined = " ".join(words)
        n_gaps = max(len(words) - 1, 0)

        if alignment == "justify" and i != last_idx and n_gaps >= 1:
            # Distribute the slack across the inter-word gaps; draw each word at
            # its own x so the line is flush to BOTH edges (column_width, not the
            # slacked fit width -- justify targets the true column).
            extra = (column_width - advance) / n_gaps
            gap = space_w + extra
            word_origins: list[tuple] = []
            x = box_left
            for w in words:
                word_origins.append((w, x))
                x += font.text_length(w, fontsize=size) + gap
            out_lines.append(LaidOutLine(
                text=joined, origin=(box_left, baseline_y),
                width=column_width, word_origins=tuple(word_origins),
            ))
            continue

        if alignment == "right":
            x = box_left + (column_width - advance)
        elif alignment == "center":
            x = box_left + (column_width - advance) / 2.0
        else:  # left, justify-last-line, justify-single-word
            x = box_left
        out_lines.append(LaidOutLine(
            text=joined, origin=(x, baseline_y), width=advance,
        ))

    # --- bbox: horizontally the wider of the column and the ACTUAL max line ink,
    # vertically ascent..descent. Using the real max line right edge (origin.x +
    # line advance) -- not always ``box_left + column_width`` -- guarantees the
    # bbox ENCLOSES any line that still extends past the column (e.g. a single
    # character wider than a degenerate column), so the editor cover + selection
    # overlay never under-cover and the original ink can't show through
    # (REFLOW_SPEC §R2). For normal in-column text this is exactly the old column
    # bbox (every line's ink right edge <= box_left + column_width).
    asc = font.ascender * size
    desc_abs = -font.descender * size  # descender is negative
    last_baseline_y = first_baseline_y + last_idx * effective_lead
    max_ink_right = box_left + column_width
    for ln in out_lines:
        max_ink_right = max(max_ink_right, ln.origin[0] + ln.width)
    bbox = (
        box_left,
        first_baseline_y - asc,
        max_ink_right,
        last_baseline_y + desc_abs,
    )
    return WrapResult(lines=tuple(out_lines), bbox=bbox, leading=effective_lead)


# ===========================================================================
# Rich (mixed-style) wrap: the same greedy fill, but every piece of text
# carries a style key -- (bold, italic) -- with its OWN font for measurement
# and drawing, so a paragraph with a bolded word wraps and draws correctly
# (per-selection styling, the "bold just the highlighted word" feature).
# ===========================================================================

@dataclass(frozen=True)
class RichSegment:
    """One drawable piece of a laid-out rich line: consecutive same-style text
    at an absolute x on the line's baseline."""

    text: str
    x: float
    style: tuple         # the style key, e.g. (bold, italic)


@dataclass(frozen=True)
class RichLine:
    """One wrapped line of a rich paragraph: its baseline origin, measured
    advance, and the same-style segments to draw (each at its own x)."""

    origin: tuple        # (line x0, baseline y) in PDF points
    width: float         # the line's measured advance (PDF points)
    segments: tuple      # tuple[RichSegment, ...] left-to-right


@dataclass(frozen=True)
class RichWrapResult:
    lines: tuple         # tuple[RichLine, ...] top-to-bottom
    bbox: tuple          # union ink bbox (x0, y0, x1, y1)
    leading: float       # baseline-to-baseline used


def wrap_rich(
    runs,
    fonts: dict,
    fontsize: float,
    box_left: float,
    first_baseline_y: float,
    column_width: float,
    *,
    alignment: str = "left",
    leading: float | None = None,
    line_spacing: float = 1.0,
) -> RichWrapResult:
    """Greedy word-wrap MIXED-STYLE text. ``runs`` is a sequence of
    ``(text, style_key)``; ``fonts`` maps each style key to the resolved
    ``fitz.Font`` used for BOTH measurement and (by the caller) drawing, so the
    on-screen render and the saved file agree exactly like ``wrap_paragraph``.

    A "word" may span style boundaries (half-bold words measure as the sum of
    their styled pieces). Spaces take the style of the piece before them.
    Justify stretches inter-word gaps and never draws the spaces.
    """
    size = max(0.01, fontsize)
    lead = leading if leading is not None else 1.2 * size
    effective_lead = lead * (line_spacing if line_spacing and line_spacing > 0 else 1.0)

    def piece_w(text: str, style: tuple) -> float:
        return fonts[style].text_length(text, fontsize=size)

    def space_w(style: tuple) -> float:
        return fonts[style].text_length(" ", fontsize=size)

    # --- tokenize into words; each word = list[(piece_text, style)] ---------
    words: list[list] = []
    cur_word: list = []
    for text, style in runs:
        if style not in fonts:
            raise KeyError(f"wrap_rich: no font for style {style!r}")
        for ch in text:
            if ch.isspace():
                if cur_word:
                    words.append(cur_word)
                    cur_word = []
            elif cur_word and cur_word[-1][1] == style:
                cur_word[-1] = (cur_word[-1][0] + ch, style)
            else:
                cur_word.append((ch, style))
    if cur_word:
        words.append(cur_word)

    def word_w(word: list) -> float:
        return sum(piece_w(t, st) for t, st in word)

    # --- break words wider than the column at the character level -----------
    if column_width > 0:
        broken: list[list] = []
        for word in words:
            if word_w(word) <= column_width:
                broken.append(word)
                continue
            chunk: list = []
            chunk_w = 0.0
            for t, st in word:
                for ch in t:
                    cw = piece_w(ch, st)
                    if chunk and (chunk_w + cw) > column_width:
                        broken.append(chunk)
                        chunk, chunk_w = [], 0.0
                    if chunk and chunk[-1][1] == st:
                        chunk[-1] = (chunk[-1][0] + ch, st)
                    else:
                        chunk.append((ch, st))
                    chunk_w += cw
            if chunk:
                broken.append(chunk)
        words = broken

    # The same metric-noise slack as wrap_paragraph, measured in the style of
    # the first run (arbitrary but deterministic).
    default_style = runs[0][1] if runs else next(iter(fonts))
    fit_width = column_width + _WIDTH_SLACK_SPACES * space_w(default_style)

    # --- greedy fill ---------------------------------------------------------
    raw_lines: list[tuple[list, float]] = []   # (words_in_line, advance)
    cur: list = []
    cur_adv = 0.0
    for word in words:
        gap = space_w(cur[-1][-1][1]) if cur else 0.0   # style of prev word's last piece
        w = word_w(word)
        if cur and (cur_adv + gap + w) > fit_width:
            raw_lines.append((cur, cur_adv))
            cur = [word]
            cur_adv = w
        else:
            cur.append(word)
            cur_adv += gap + w
    if cur:
        raw_lines.append((cur, cur_adv))
    if not raw_lines:
        raw_lines = [([], 0.0)]

    last_idx = len(raw_lines) - 1
    out: list[RichLine] = []
    for i, (line_words, advance) in enumerate(raw_lines):
        baseline_y = first_baseline_y + i * effective_lead
        n_gaps = max(len(line_words) - 1, 0)
        justify_line = (alignment == "justify" and i != last_idx and n_gaps >= 1)

        if alignment == "right" and not justify_line:
            line_x0 = box_left + (column_width - advance)
        elif alignment == "center" and not justify_line:
            line_x0 = box_left + (column_width - advance) / 2.0
        else:
            line_x0 = box_left

        extra = ((column_width - advance) / n_gaps) if justify_line else 0.0

        segments: list[RichSegment] = []
        x = line_x0
        prev_style: tuple | None = None
        for wi, word in enumerate(line_words):
            if wi > 0:
                gap_style = prev_style if prev_style is not None else word[0][1]
                gap_w = space_w(gap_style) + extra
                if (not justify_line and segments
                        and segments[-1].style == gap_style == word[0][1]):
                    # Same style across the space: draw it as part of the run.
                    segments[-1] = RichSegment(
                        text=segments[-1].text + " ", x=segments[-1].x,
                        style=gap_style)
                x += gap_w
            for t, st in word:
                if segments and segments[-1].style == st and not justify_line \
                        and abs((segments[-1].x
                                 + piece_w(segments[-1].text, st)) - x) < 0.01:
                    segments[-1] = RichSegment(
                        text=segments[-1].text + t, x=segments[-1].x, style=st)
                else:
                    segments.append(RichSegment(text=t, x=x, style=st))
                x += piece_w(t, st)
            prev_style = word[-1][1]
        width = (column_width if justify_line else advance)
        out.append(RichLine(origin=(line_x0, baseline_y), width=width,
                            segments=tuple(segments)))

    asc = max(f.ascender for f in fonts.values()) * size
    desc_abs = -min(f.descender for f in fonts.values()) * size
    last_baseline_y = first_baseline_y + last_idx * effective_lead
    max_ink_right = box_left + column_width
    for ln in out:
        max_ink_right = max(max_ink_right, ln.origin[0] + ln.width)
    bbox = (box_left, first_baseline_y - asc, max_ink_right,
            last_baseline_y + desc_abs)
    return RichWrapResult(lines=tuple(out), bbox=bbox, leading=effective_lead)
