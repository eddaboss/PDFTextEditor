"""Build the DOCUMENT TOOLS fixture (doc-tools workstream M1).

``doc_tools.pdf``: three US-Letter pages of invented meeting-agenda content
(fake neutral names only: Acme Corp, Jordan Carter, Riley Morgan) carrying a
REAL info dictionary -- title "Quarterly Review Agenda", author "Jordan
Carter" -- so the metadata-carry regression (``insert_pdf`` drops the info
dict, stripping Title/Author on every save) is testable against a committed
file. Saved with ``deflate=False`` so the optimize milestone has a measurable
compression delta on the SAME fixture.

Fonts are real macOS system faces embedded via ``fontfile=`` (the
``build_fixtures.py`` convention), so spans extract through the editor's font
tiers like every other fixture. Body lines are spaced 28 pt apart (well past
the paragraph-grouping first-gap ceiling at 12 pt) so each line stays its own
editable box. The word "revenue" appears EXACTLY once in the document (page 1)
so edit-then-export tests have an unambiguous target.

Run with the project venv:
    QT_QPA_PLATFORM=offscreen \
      /Users/edward/Documents/GitHub/PDFTextEditor/.venv/bin/python \
      tests/fixtures/build_doctools_fixture.py

It writes doc_tools.pdf, renders a stacked verification doc_tools.png, then
reopens the fixture through PDFDocument and prints metadata / page / span
facts.
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
GEORGIA = f"{FONT_DIR}/Georgia.ttf"
GEORGIA_BOLD = f"{FONT_DIR}/Georgia Bold.ttf"

BLACK = (0.0, 0.0, 0.0)
NAVY = (0.10, 0.16, 0.45)
SLATE = (0.28, 0.33, 0.40)

LETTER_W, LETTER_H = 612, 792
TITLE = "Quarterly Review Agenda"
AUTHOR = "Jordan Carter"
SUBJECT = "Acme Corp quarterly review"
KEYWORDS = "agenda, review, acme"
# Body lines 28 pt apart at 12 pt so each stays its OWN box (the grouping
# first-gap ceiling is 2.2 * size = 26.4 pt).
LINE_STEP = 28


def _font_for(font_file: str, text: str) -> str:
    """Per-(file,text) registration name (build_fixtures.py convention); the
    embedded face still dedupes to one xref per file."""
    return "F" + str(abs(hash((font_file, text))) % 100000)


def write(page, xy, text, font_file, size, color=BLACK):
    page.insert_text(
        xy, text, fontname=_font_for(font_file, text),
        fontfile=font_file, fontsize=size, color=color,
    )


def _page(doc, head, lines):
    page = doc.new_page(width=LETTER_W, height=LETTER_H)
    page.draw_rect(fitz.Rect(0, 0, LETTER_W, 64), color=None, fill=NAVY)
    write(page, (54, 42), head, GEORGIA_BOLD, 22, color=(1, 1, 1))
    y = 120
    for ln in lines:
        write(page, (54, y), ln, GEORGIA, 12, color=BLACK)
        y += LINE_STEP
    return page


def build(path: str) -> None:
    doc = fitz.open()

    p1 = _page(doc, TITLE, [
        "Acme Corp will convene the quarterly review on the second Tuesday.",
        "Acme Corp expects a short revenue summary from every team lead.",
        "Prepared by Jordan Carter for distribution to Riley Morgan.",
        "Attendance is in person, with the bridge line open for the field.",
    ])
    write(p1, (54, LETTER_H - 48), "Agenda sheet 1 of 3", GEORGIA, 10,
          color=SLATE)

    p2 = _page(doc, "Session Topics", [
        "Opening remarks and a recap of the actions logged last quarter.",
        "Operations walk-through: staffing, facilities, and the move plan.",
        "Riley Morgan presents the customer-feedback digest for the period.",
        "Open floor for questions submitted ahead of the session.",
    ])
    write(p2, (54, LETTER_H - 48), "Agenda sheet 2 of 3", GEORGIA, 10,
          color=SLATE)

    p3 = _page(doc, "Closing Notes", [
        "Decisions reached are recorded in the minutes within two days.",
        "Jordan Carter circulates the minutes to every attendee for review.",
        "Corrections are due back before the end of the following week.",
        "The next session date is confirmed at adjournment.",
    ])
    write(p3, (54, LETTER_H - 48), "Agenda sheet 3 of 3", GEORGIA, 10,
          color=SLATE)

    doc.set_metadata({
        "title": TITLE,
        "author": AUTHOR,
        "subject": SUBJECT,
        "keywords": KEYWORDS,
    })
    # deflate=False on purpose: raw content + font streams give the optimize
    # milestone a measurable before/after byte delta on this same fixture.
    doc.save(path, deflate=False)
    doc.close()


def render_all_pages_png(pdf_path: str, png_path: str):
    """Stacked verification PNG (build_pages_fixtures.py pattern)."""
    doc = fitz.open(pdf_path)
    mat = fitz.Matrix(1.5, 1.5)
    pix_list = [doc[i].get_pixmap(matrix=mat, alpha=False)
                for i in range(doc.page_count)]
    gap = 16
    width = max(p.width for p in pix_list)
    height = sum(p.height for p in pix_list) + gap * (len(pix_list) - 1)
    try:
        from PIL import Image
    except Exception:
        doc.close()
        base, ext = os.path.splitext(png_path)
        for i, p in enumerate(pix_list):
            p.save(f"{base}_p{i + 1}{ext}")
        return width, height
    canvas = Image.new("RGB", (width, height), (228, 230, 234))
    yoff = 0
    for p in pix_list:
        img = Image.frombytes("RGB", (p.width, p.height), p.samples)
        canvas.paste(img, (0, yoff))
        yoff += p.height + gap
    canvas.save(png_path)
    doc.close()
    return width, height


def main() -> None:
    from pdftexteditor.document import PDFDocument

    pdf_path = os.path.join(HERE, "doc_tools.pdf")
    png_path = os.path.join(HERE, "doc_tools.png")
    build(pdf_path)
    w, h = render_all_pages_png(pdf_path, png_path)
    size = os.path.getsize(pdf_path)
    print(f"built doc_tools.pdf: {size} B (deflate=False) -> "
          f"doc_tools.png ({w}x{h})")

    raw = fitz.open(pdf_path)
    print(f"  on-disk metadata: title={raw.metadata['title']!r} "
          f"author={raw.metadata['author']!r}")
    revenue_hits = sum(
        raw[i].get_text("text").replace("\u00a0", " ").count("revenue")
        for i in range(raw.page_count))
    print(f"  'revenue' occurrences (whole doc): {revenue_hits}")
    assert revenue_hits == 1, "fixture contract: exactly one 'revenue'"
    raw.close()

    doc = PDFDocument(pdf_path)
    print(f"  PDFDocument: pages={doc.page_count} "
          f"metadata_fields={doc.metadata_fields()}")
    print(f"  spans per page: "
          f"{[len(doc.spans(i)) for i in range(doc.page_count)]}")
    print(f"  fonts_used: {doc.fonts_used()}")
    doc.close()


if __name__ == "__main__":
    main()
