"""Build the ANNOTATIONS & MARKUP fixture: ``annot_target.pdf``.

Two US-Letter pages of embedded-font paragraphs (real macOS system faces via
``fontfile=``, the ``build_fixtures.py`` convention) with FAKE NEUTRAL names
only ("Jordan Carter", "Acme Corp", "Riley Morgan") -- PII-free, invented
content. Page 1 is clean body text for staging markup over known words; page 2
ships TWO PRE-EXISTING annotations (one highlight over "milestone", one sticky
note with content "Reviewed by Riley Morgan") so the existing-annot management
milestone has file annots to override/delete.

Idempotent; writes ONLY inside tests/fixtures/. Run with the project venv:
    QT_QPA_PLATFORM=offscreen \
      /Users/edward/Documents/GitHub/PDFTextEditor/.venv/bin/python \
      tests/fixtures/build_annot_fixture.py
"""

from __future__ import annotations

import os
import sys

import fitz

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(os.path.dirname(HERE))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

FONT_DIR = "/System/Library/Fonts/Supplemental"
FONTS = {
    "arial": f"{FONT_DIR}/Arial.ttf",
    "arial_bold": f"{FONT_DIR}/Arial Bold.ttf",
    "georgia": f"{FONT_DIR}/Georgia.ttf",
    "georgia_bold": f"{FONT_DIR}/Georgia Bold.ttf",
}

BLACK = (0.0, 0.0, 0.0)
NAVY = (0.10, 0.16, 0.45)
SLATE = (0.28, 0.33, 0.40)

LETTER_W, LETTER_H = 612, 792


def _font_for(font_file: str, text: str) -> str:
    """Per-(file,text) registration name (the build_fixtures.py convention)."""
    return "F" + str(abs(hash((font_file, text))) % 100000)


def write(page, xy, text, font_file, size, color=BLACK):
    page.insert_text(
        xy, text, fontname=_font_for(font_file, text),
        fontfile=font_file, fontsize=size, color=color,
    )


PAGE1_BODY = [
    "Jordan Carter opened the quarterly review with a short summary of",
    "the onboarding pipeline and the schedule for the coming month. The",
    "team agreed that the certificate templates from Acme Corp needed a",
    "fresh round of proofreading before the next distribution date, and",
    "Riley Morgan volunteered to coordinate the markup pass this week.",
]

PAGE2_BODY = [
    "The second sheet collects the agenda items carried over from the",
    "previous session. Each milestone below is annotated during review",
    "so the next owner can pick the work up without a separate handoff",
    "meeting. Acme Corp's renewal paperwork remains the largest single",
    "item on the list and is tracked separately by Jordan Carter.",
]


def build(path: str) -> None:
    doc = fitz.open()

    # --- page 1: clean body text (markup staging target) -----------------
    p1 = doc.new_page(width=LETTER_W, height=LETTER_H)
    write(p1, (72, 80), "Review Notes — Acme Corp", FONTS["arial_bold"], 20,
          color=NAVY)
    y = 130
    for line in PAGE1_BODY:
        write(p1, (72, y), line, FONTS["georgia"], 12)
        y += 17
    write(p1, (72, 740), "Prepared by Jordan Carter — internal draft",
          FONTS["arial"], 10, color=SLATE)

    # --- page 2: body text + PRE-EXISTING annots (for M2) ----------------
    p2 = doc.new_page(width=LETTER_W, height=LETTER_H)
    write(p2, (72, 80), "Carried-Over Agenda", FONTS["georgia_bold"], 20,
          color=NAVY)
    y = 130
    for line in PAGE2_BODY:
        write(p2, (72, y), line, FONTS["georgia"], 12)
        y += 17
    write(p2, (72, 740), "Reviewed weekly — Riley Morgan", FONTS["arial"], 10,
          color=SLATE)

    # Pre-existing highlight over the word "milestone" (text-space rect from
    # search_for, the same space the editor's annot geometry uses).
    hits = p2.search_for("milestone")
    assert hits, "page 2 must contain the word 'milestone'"
    hl = p2.add_highlight_annot(quads=[hits[0].quad])
    hl.set_colors(stroke=(1.0, 0.85, 0.0))
    hl.update()

    # Pre-existing sticky note in the right margin next to the body.
    note = p2.add_text_annot(fitz.Point(540, 132), "Reviewed by Riley Morgan",
                             icon="Comment")
    note.set_info(content="Reviewed by Riley Morgan", title="")
    note.update()

    doc.save(path, garbage=4, deflate=True)
    doc.close()


def render_png(pdf_path: str, png_path: str, scale: float = 1.5) -> None:
    """One verification PNG with every page stacked vertically (the
    build_pages_fixtures.py convention)."""
    from PIL import Image

    doc = fitz.open(pdf_path)
    images = []
    for page in doc:
        pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
        images.append(
            Image.frombytes("RGB", (pix.width, pix.height), pix.samples))
    doc.close()
    gap = 16
    width = max(im.width for im in images)
    height = sum(im.height for im in images) + gap * (len(images) - 1)
    sheet = Image.new("RGB", (width, height), (120, 120, 120))
    y = 0
    for im in images:
        sheet.paste(im, ((width - im.width) // 2, y))
        y += im.height + gap
    sheet.save(png_path)


def verify(path: str) -> None:
    """The fixture opens through the real model layer and carries exactly the
    expected pre-existing annots."""
    from pdftexteditor.document import PDFDocument

    doc = fitz.open(path)
    assert doc.page_count == 2, doc.page_count
    page2 = doc[1]                  # keep the page alive while reading annots
    annots = list(page2.annots() or ())
    kinds = sorted(a.type[0] for a in annots)
    assert kinds == sorted([fitz.PDF_ANNOT_HIGHLIGHT, fitz.PDF_ANNOT_TEXT]), kinds
    contents = [a.info.get("content", "") for a in annots]
    assert "Reviewed by Riley Morgan" in contents, contents
    page1 = doc[0]
    assert not list(page1.annots() or ()), "page 1 must ship annot-free"
    n_annots = len(annots)
    del annots, page1, page2
    doc.close()

    model = PDFDocument(path)
    spans = [len(model.spans(i)) for i in range(model.page_count)]
    print(f"annot_target.pdf: 2 pages, spans per page {spans}, "
          f"page-2 annots {n_annots} (1 highlight + 1 note)")
    model.close()


if __name__ == "__main__":
    out = os.path.join(HERE, "annot_target.pdf")
    build(out)
    render_png(out, os.path.join(HERE, "annot_target.png"))
    verify(out)
    print("built", out)
