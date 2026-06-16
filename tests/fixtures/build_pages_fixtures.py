"""Build multi-page PDF fixtures for PAGE & DOCUMENT MANAGEMENT testing.

These exercise the combine/merge, split/separate, reorder, rotate, insert,
duplicate, delete, multi-document, and rotation-handling features. Every fixture
is PII-FREE and built from invented content; each page carries a big, distinct
page label so a reorder / rotate / delete is VISUALLY verifiable in the rendered
PNG (you can read which page moved where at a glance).

Fonts are real macOS system faces embedded via ``fontfile=`` (same convention as
``build_fixtures.py``), so spans extract through the editor's font tiers exactly
like the single-page fixtures.

Run with the project venv:
    QT_QPA_PLATFORM=offscreen \
      /Users/edward/Documents/GitHub/PDFTextEditor/.venv/bin/python \
      tests/fixtures/build_pages_fixtures.py

It writes three .pdf fixtures, renders every page of each to a verification .png
(pages stacked vertically per file), confirms each opens through PDFDocument, and
prints page_count + sizes + rotation for each.
"""

from __future__ import annotations

import os
import sys

import fitz

HERE = os.path.dirname(os.path.abspath(__file__))
# Make ``pdftexteditor`` importable so we can confirm each fixture opens through
# the real model layer (the repo root is two levels up from this file).
REPO_ROOT = os.path.dirname(os.path.dirname(HERE))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

FONT_DIR = "/System/Library/Fonts/Supplemental"
FONTS = {
    "arial": f"{FONT_DIR}/Arial.ttf",
    "arial_bold": f"{FONT_DIR}/Arial Bold.ttf",
    "arial_italic": f"{FONT_DIR}/Arial Italic.ttf",
    "georgia": f"{FONT_DIR}/Georgia.ttf",
    "georgia_bold": f"{FONT_DIR}/Georgia Bold.ttf",
}

# RGB tuples in 0..1.
BLACK = (0.0, 0.0, 0.0)
NAVY = (0.10, 0.16, 0.45)
CRIMSON = (0.79, 0.09, 0.13)
FOREST = (0.13, 0.45, 0.20)
PLUM = (0.45, 0.16, 0.50)
SLATE = (0.28, 0.33, 0.40)
TEAL = (0.07, 0.45, 0.49)

LETTER_W, LETTER_H = 612, 792           # US Letter portrait
LAND_W, LAND_H = 792, 612               # US Letter landscape


def _font_for(font_file: str, text: str) -> str:
    """A per-(file,text) registration name, mirroring build_fixtures.py so each
    distinct run embeds its own face reference."""
    return "F" + str(abs(hash((font_file, text))) % 100000)


def write(page, xy, text, font_file, size, color=BLACK):
    """Place one baseline-aligned run; returns nothing (geometry is explicit)."""
    page.insert_text(
        xy, text, fontname=_font_for(font_file, text),
        fontfile=font_file, fontsize=size, color=color,
    )


def _tint(page, rect, color):
    """Fill a rectangle (used for a colored side band, so a rotate/reorder is
    obvious and so non-destructive redaction has vector art to preserve)."""
    page.draw_rect(fitz.Rect(rect), color=None, fill=color)


# --- three_page.pdf ------------------------------------------------------

def build_three_page(path):
    """Three pages, each visually distinct, with PAGE ONE / TWO / THREE labels.

    Page 1: portrait, navy band, "PAGE ONE", body about a library.
    Page 2: LANDSCAPE (mixed size in the document), forest band, "PAGE TWO",
            different body so a reorder/rotate is unmistakable.
    Page 3: portrait, crimson band, "PAGE THREE", body about a workshop.

    The big label + colored band + per-page body all differ, so dragging a
    thumbnail to reorder, rotating a page, or deleting one is verifiable by eye
    in the rendered PNG.
    """
    doc = fitz.open()

    # --- Page 1: portrait ---
    p1 = doc.new_page(width=LETTER_W, height=LETTER_H)
    _tint(p1, (0, 0, LETTER_W, 70), NAVY)
    write(p1, (54, 48), "PAGE ONE", FONTS["arial_bold"], 30, color=(1, 1, 1))
    write(p1, (54, 130), "The Harbor Library — Reading Room", FONTS["georgia_bold"], 20, color=NAVY)
    body1 = [
        "Tall windows ran the length of the north wall, and the morning light",
        "fell in long bars across the oak tables. A clerk wheeled a cart of",
        "returned volumes between the stacks, re-shelving each by its faded",
        "spine label while the radiators ticked and settled in the cold.",
    ]
    y = 168
    for ln in body1:
        write(p1, (54, y), ln, FONTS["georgia"], 12, color=BLACK)
        y += 20
    write(p1, (54, LETTER_H - 48), "Document A · sheet 1 of 3 · portrait",
          FONTS["arial_italic"], 10, color=SLATE)

    # --- Page 2: LANDSCAPE (mixed page size) ---
    p2 = doc.new_page(width=LAND_W, height=LAND_H)
    _tint(p2, (0, 0, LAND_W, 70), FOREST)
    write(p2, (54, 48), "PAGE TWO", FONTS["arial_bold"], 30, color=(1, 1, 1))
    write(p2, (54, 130), "Field Notes — Coastal Survey (landscape)", FONTS["georgia_bold"], 20, color=FOREST)
    body2 = [
        "The tide charts were spread edge to edge across the wide table, and a",
        "grease pencil rolled toward the seam every time the boat heeled over.",
        "We logged the depth soundings in a column down the right margin, then",
        "sketched the shoal line in the broad space the landscape sheet gave us.",
    ]
    y = 168
    for ln in body2:
        write(p2, (54, y), ln, FONTS["arial"], 12, color=BLACK)
        y += 20
    write(p2, (54, LAND_H - 40), "Document A · sheet 2 of 3 · LANDSCAPE (mixed size)",
          FONTS["arial_italic"], 10, color=SLATE)

    # --- Page 3: portrait ---
    p3 = doc.new_page(width=LETTER_W, height=LETTER_H)
    _tint(p3, (0, 0, LETTER_W, 70), CRIMSON)
    write(p3, (54, 48), "PAGE THREE", FONTS["arial_bold"], 30, color=(1, 1, 1))
    write(p3, (54, 130), "The Repair Workshop — Inventory", FONTS["georgia_bold"], 20, color=CRIMSON)
    body3 = [
        "Hand planes hung in a neat row above the bench, sorted from the heavy",
        "jointer down to a thumb-sized block plane. A jar of brass screws sat",
        "beside the vise, and the floor was swept clean of shavings save for a",
        "single curl that had drifted under the lathe and stayed there for weeks.",
    ]
    y = 168
    for ln in body3:
        write(p3, (54, y), ln, FONTS["georgia"], 12, color=BLACK)
        y += 20
    write(p3, (54, LETTER_H - 48), "Document A · sheet 3 of 3 · portrait",
          FONTS["arial_italic"], 10, color=SLATE)

    doc.save(path, garbage=4, deflate=True)
    doc.close()


# --- two_page.pdf --------------------------------------------------------

def build_two_page(path):
    """A SECOND document (DOC-B) for merge/combine testing. Two portrait pages,
    each with a "DOC-B P1" / "DOC-B P2" label and distinct body, in a teal/plum
    palette so appended pages are obviously from a different source than
    three_page.pdf's navy/forest/crimson."""
    doc = fitz.open()

    p1 = doc.new_page(width=LETTER_W, height=LETTER_H)
    _tint(p1, (0, 0, LETTER_W, 70), TEAL)
    write(p1, (54, 48), "DOC-B P1", FONTS["arial_bold"], 30, color=(1, 1, 1))
    write(p1, (54, 130), "Appendix B — Glossary of Terms", FONTS["georgia_bold"], 20, color=TEAL)
    bodyb1 = [
        "Each entry below is defined in plain language for the casual reader,",
        "with cross-references kept to a minimum so the page stands on its own.",
        "Terms are listed in the order they first appear in the main report,",
        "not alphabetically, to preserve the narrative the author intended.",
    ]
    y = 168
    for ln in bodyb1:
        write(p1, (54, y), ln, FONTS["arial"], 12, color=BLACK)
        y += 20
    write(p1, (54, LETTER_H - 48), "Document B · sheet 1 of 2 (merge source)",
          FONTS["arial_italic"], 10, color=SLATE)

    p2 = doc.new_page(width=LETTER_W, height=LETTER_H)
    _tint(p2, (0, 0, LETTER_W, 70), PLUM)
    write(p2, (54, 48), "DOC-B P2", FONTS["arial_bold"], 30, color=(1, 1, 1))
    write(p2, (54, 130), "Appendix B — References", FONTS["georgia_bold"], 20, color=PLUM)
    bodyb2 = [
        "Sources are grouped by chapter and then by the date they were consulted,",
        "so a reader retracing the argument can follow it in roughly the same",
        "sequence the author did. Where a source was paraphrased rather than",
        "quoted directly, the note says so to keep the record honest.",
    ]
    y = 168
    for ln in bodyb2:
        write(p2, (54, y), ln, FONTS["arial"], 12, color=BLACK)
        y += 20
    write(p2, (54, LETTER_H - 48), "Document B · sheet 2 of 2 (merge source)",
          FONTS["arial_italic"], 10, color=SLATE)

    doc.save(path, garbage=4, deflate=True)
    doc.close()


# --- rotated_doc.pdf -----------------------------------------------------

def build_rotated_doc(path):
    """A single page with /Rotate 90 set, to test rotation HANDLING (the editor
    must map derotated text-space coordinates into the rotated display space via
    rotation_matrix). Content is laid out in unrotated text space; the page's
    /Rotate flag spins the DISPLAY 90° clockwise.

    The page also draws a colored arrow-ish band along what is the LEFT edge in
    text space, so after the 90° display rotation you can see plainly that the
    page is turned (the band lands along the top of the rendered image)."""
    doc = fitz.open()
    page = doc.new_page(width=LETTER_W, height=LETTER_H)

    # A vertical colored band along the text-space left edge (becomes the top
    # after a 90° clockwise display rotation), so the rotation reads at a glance.
    _tint(page, (0, 0, 70, LETTER_H), PLUM)

    write(page, (110, 90), "ROTATED 90°", FONTS["arial_bold"], 30, color=PLUM)
    write(page, (110, 150), "This page has /Rotate 90 set on it.", FONTS["georgia_bold"], 18, color=BLACK)
    body = [
        "The text below is stored in ordinary upright text space; the page's",
        "/Rotate flag turns the displayed sheet ninety degrees clockwise. A",
        "correct editor maps every span's bbox and baseline through the page",
        "rotation_matrix before drawing the selection overlay, so a click lands",
        "on the glyph the user actually sees rather than its unrotated position.",
    ]
    y = 190
    for ln in body:
        write(page, (110, y), ln, FONTS["georgia"], 12, color=BLACK)
        y += 20
    write(page, (110, LETTER_H - 60), "Document C · single page · /Rotate 90",
          FONTS["arial_italic"], 10, color=SLATE)

    page.set_rotation(90)
    doc.save(path, garbage=4, deflate=True)
    doc.close()


FIXTURES = [
    ("three_page.pdf", "3 pages, distinct labels, one landscape", build_three_page),
    ("two_page.pdf", "2 pages (DOC-B) for merge testing", build_two_page),
    ("rotated_doc.pdf", "1 page with /Rotate 90", build_rotated_doc),
]


def render_all_pages_png(pdf_path, png_path):
    """Render EVERY page of the fixture to one PNG, stacked vertically with a
    gap, so a single image shows the whole document for visual inspection of
    page order / rotation / labels."""
    doc = fitz.open(pdf_path)
    mat = fitz.Matrix(1.5, 1.5)
    pix_list = [doc[i].get_pixmap(matrix=mat, alpha=False)
                for i in range(doc.page_count)]
    gap = 16
    width = max(p.width for p in pix_list)
    height = sum(p.height for p in pix_list) + gap * (len(pix_list) - 1)
    # Compose onto a single light-gray canvas via Pillow (already a project dep
    # for screenshots); fall back to per-page PNGs if Pillow is absent.
    try:
        from PIL import Image
    except Exception:
        doc.close()
        # one PNG per page next to the requested path
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


def main():
    from pdftexteditor.document import PDFDocument

    summary = []
    for filename, desc, builder in FIXTURES:
        pdf_path = os.path.join(HERE, filename)
        builder(pdf_path)
        png_path = os.path.join(HERE, filename.replace(".pdf", ".png"))
        w, h = render_all_pages_png(pdf_path, png_path)

        # Probe raw geometry/rotation with fitz.
        doc = fitz.open(pdf_path)
        pages = []
        for i in range(doc.page_count):
            pg = doc[i]
            r = pg.rect
            pages.append({
                "index": i,
                "size": (round(r.width, 1), round(r.height, 1)),
                "rotation": pg.rotation,
                "orientation": "landscape" if r.width > r.height else "portrait",
            })
        page_count = doc.page_count
        doc.close()

        # Confirm it opens through the real model layer and extracts spans.
        pdfdoc = PDFDocument(pdf_path)
        span_counts = [len(pdfdoc.spans(i)) for i in range(pdfdoc.page_count)]
        rotations = [pdfdoc.page_rotation(i) for i in range(pdfdoc.page_count)]
        pdfdoc.close()

        summary.append({
            "file": filename,
            "desc": desc,
            "png": os.path.basename(png_path),
            "page_count": page_count,
            "pages": pages,
            "span_counts": span_counts,
            "model_rotations": rotations,
        })
        print(f"built {filename}: {page_count} page(s) -> {os.path.basename(png_path)} ({w}x{h})")
        for pinfo, sc in zip(pages, span_counts):
            print(f"    page {pinfo['index']}: {pinfo['size']} "
                  f"{pinfo['orientation']} rot={pinfo['rotation']} spans={sc}")
        print(f"    PDFDocument opened OK (page_count={page_count}, "
              f"model_rotations={rotations})")
    return summary


if __name__ == "__main__":
    import json
    result = main()
    print("\n=== JSON SUMMARY ===")
    print(json.dumps(result, indent=2))
