"""Build a PII-free synthetic FORM fixture for the full-editor feature work.

This mirrors the visual structure of a real background-check / report form
WITHOUT carrying any real data: every label, value, name, and date is invented.
It exists to exercise the *new* editor features against a document that looks
like the ones a user actually opens:

  * a solid colored header bar with WHITE bold text,
  * a two-column field table -- left cells with a light fill + bold labels,
    right cells with values on plain white,
  * one value drawn as OVERLAPPING DUPLICATE runs (a full span plus partial
    fragments at a slightly offset baseline), so the document.spans() overlap-
    merge collapses them into a single editable box (Span.redact_bboxes > 1),
  * a couple of dates ("1/8/2026", "10/11/1995"),
  * a large EMPTY lower area for adding brand-new text boxes, and
  * MULTIPLE font families: base-14 Helvetica / Helvetica-Bold for the chrome,
    plus an embedded Arial TrueType face for the values.

Run with the project venv (Qt offscreen so nothing pops a window):

    QT_QPA_PLATFORM=offscreen \
      /Users/edward/Documents/GitHub/PDFTextEditor/.venv/bin/python \
      tests/fixtures/build_form_fixture.py

It writes form_like.pdf, renders form_like.png (2x), and prints a confirmation
that the overlapping value merges into one box at the spans() layer.
"""

from __future__ import annotations

import os

import fitz

HERE = os.path.dirname(os.path.abspath(__file__))
FONT_DIR = "/System/Library/Fonts/Supplemental"

# Embedded TrueType faces (real macOS Arial). These are full faces, embeddable
# and glyph-complete, standing in for the "values" a user would retype.
ARIAL = f"{FONT_DIR}/Arial.ttf"
ARIAL_BOLD = f"{FONT_DIR}/Arial Bold.ttf"

# Base-14 built-in PDF fonts (NO file needed): PyMuPDF accepts these fontname
# codes on insert_text directly. Used for the form chrome so the page carries
# a base-14 family alongside the embedded TrueType one.
HELV = "helv"        # Helvetica
HELV_BOLD = "hebo"   # Helvetica-Bold

# RGB tuples in 0..1, PyMuPDF convention.
BLACK = (0.0, 0.0, 0.0)
WHITE = (1.0, 1.0, 1.0)
HEADER_BG = (0.16, 0.32, 0.55)   # solid blue header bar
LABEL_FILL = (0.90, 0.93, 0.97)  # light blue-gray label cell fill
GRID = (0.62, 0.67, 0.74)        # table border lines
INK = (0.10, 0.12, 0.16)         # near-black body text

PAGE_W, PAGE_H = 612, 792  # US Letter

# Table geometry (PDF points, top-left origin space PyMuPDF draws in).
TABLE_X = 60
TABLE_RIGHT = 552
COL_SPLIT = 210                  # x where the label column ends / value begins
ROW_TOP = 150                    # y of the first row's top edge
ROW_H = 44                       # row height
TEXT_PAD_X = 12                  # left padding for text inside a cell
BASELINE_DROP = 28               # baseline offset from a row's top edge


def _fontname(prefix: str, fontfile: str) -> str:
    """Stable, per-(face) registration name for an embedded TrueType run.

    insert_text keys page font resources by name, so distinct faces need
    distinct names; reusing one name across the same face is fine and lets
    PyMuPDF embed it once.
    """
    return prefix + str(abs(hash(fontfile)) % 100000)


def _text(page, pos, text, *, fontfile=None, fontname=None, size, color=BLACK):
    """Draw one run. Either an embedded TrueType (fontfile=) or a base-14
    built-in (fontname= one of helv/hebo/...). Returns the run's end x via a
    crude width estimate so inline fragments can be offset."""
    if fontfile is not None:
        name = _fontname("F", fontfile)
        page.insert_text(pos, text, fontname=name, fontfile=fontfile,
                         fontsize=size, color=color)
    else:
        page.insert_text(pos, text, fontname=fontname, fontsize=size,
                         color=color)
    return pos[0] + size * 0.5 * len(text)


def build_form(path: str) -> None:
    doc = fitz.open()
    page = doc.new_page(width=PAGE_W, height=PAGE_H)

    # --- header bar: solid colored fill + WHITE bold text -----------------
    header = fitz.Rect(0, 0, PAGE_W, 96)
    page.draw_rect(header, color=None, fill=HEADER_BG)
    # Title in base-14 Helvetica-Bold, white.
    _text(page, (TABLE_X, 56), "Sample Report",
          fontname=HELV_BOLD, size=26, color=WHITE)
    # A thin subtitle, also white, regular weight.
    _text(page, (TABLE_X, 80), "Synthetic verification record (no real data)",
          fontname=HELV, size=11, color=(0.86, 0.90, 0.96))

    # --- two-column field table -------------------------------------------
    # rows: (label, value, value_is_embedded). The "Field A:" row's value is
    # the OVERLAPPING-DUPLICATE one, drawn separately below.
    rows = [
        ("Field A:", None),                 # value drawn as overlap duplicates
        ("Field B:", "Pending review"),
        ("Date:", "1/8/2026"),
        ("Amount:", "$1,250.00"),
        ("Issued:", "10/11/1995"),
    ]

    for i, (label, value) in enumerate(rows):
        top = ROW_TOP + i * ROW_H
        bot = top + ROW_H
        label_cell = fitz.Rect(TABLE_X, top, COL_SPLIT, bot)
        value_cell = fitz.Rect(COL_SPLIT, top, TABLE_RIGHT, bot)
        # Light fill on the label cell; value cell stays white.
        page.draw_rect(label_cell, color=GRID, fill=LABEL_FILL, width=0.8)
        page.draw_rect(value_cell, color=GRID, fill=WHITE, width=0.8)
        baseline = top + BASELINE_DROP
        # Bold label in base-14 Helvetica-Bold.
        _text(page, (TABLE_X + TEXT_PAD_X, baseline), label,
              fontname=HELV_BOLD, size=13, color=INK)
        # Value in EMBEDDED Arial (different family from the chrome).
        if value is not None:
            _text(page, (COL_SPLIT + TEXT_PAD_X, baseline), value,
                  fontfile=ARIAL, size=13, color=INK)

    # --- the OVERLAPPING-DUPLICATE value (Field A) ------------------------
    # Some real PDFs stamp a value as a full string PLUS partial fragments at a
    # hair-offset baseline (re-stamps, form-flatten artifacts). We reproduce
    # that here so document._merge_overlapping collapses them into ONE box
    # whose redact_bboxes carries every member -- the box-recognition contract.
    fa_top = ROW_TOP + 0 * ROW_H
    fa_baseline = fa_top + BASELINE_DROP
    fa_x = COL_SPLIT + TEXT_PAD_X
    value_text = "Cleared 4821"
    # 1) the full value
    _text(page, (fa_x, fa_baseline), value_text,
          fontfile=ARIAL, size=13, color=INK)
    # 2) the SAME full value re-stamped at a sub-point offset baseline -- this
    #    is a near-exact area overlap, so it merges with (1).
    _text(page, (fa_x + 0.4, fa_baseline + 0.6), value_text,
          fontfile=ARIAL, size=13, color=INK)
    # 3) a partial fragment ("Cleared") over the front of the value, also at a
    #    tiny offset -- a third overlapping member of the same box.
    _text(page, (fa_x + 0.2, fa_baseline + 0.3), "Cleared",
          fontfile=ARIAL_BOLD, size=13, color=INK)

    # --- large EMPTY lower area for ADDING new text boxes -----------------
    # A faint guide rule + caption marks the open canvas; everything below the
    # table (~y=380 down to the footer) is intentionally blank.
    open_top = ROW_TOP + len(rows) * ROW_H + 40
    _text(page, (TABLE_X, open_top), "Notes:",
          fontname=HELV_BOLD, size=12, color=(0.45, 0.50, 0.57))
    page.draw_line(fitz.Point(TABLE_X, open_top + 10),
                   fitz.Point(TABLE_RIGHT, open_top + 10),
                   color=(0.80, 0.83, 0.88), width=0.6)
    # (No further text: the band from here to the bottom margin is empty
    #  space for the "Add Text" tool to place new boxes.)

    # --- footer -----------------------------------------------------------
    _text(page, (TABLE_X, PAGE_H - 48), "Form ID: SAMPLE-0000  |  Page 1 of 1",
          fontname=HELV, size=9, color=(0.50, 0.54, 0.60))

    doc.save(path, garbage=4, deflate=True)
    doc.close()


def render_png(pdf_path: str, png_path: str) -> tuple[int, int]:
    doc = fitz.open(pdf_path)
    pix = doc[0].get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
    pix.save(png_path)
    size = (pix.width, pix.height)
    doc.close()
    return size


def confirm_overlap_merge(pdf_path: str) -> dict:
    """Open via the project's PDFDocument and confirm the Field A value merged
    into ONE box carrying multiple redact_bboxes. Returns a small report."""
    import sys
    proj_root = os.path.dirname(os.path.dirname(HERE))
    if proj_root not in sys.path:
        sys.path.insert(0, proj_root)
    from pdftexteditor.document import PDFDocument

    pdoc = PDFDocument(pdf_path)
    spans = pdoc.spans(0)
    merged = [s for s in spans if len(s.redact_bboxes) > 1]
    report = {
        "span_count": len(spans),
        "merged_boxes": [
            {"text": s.text, "redact_bboxes": len(s.redact_bboxes)}
            for s in merged
        ],
        "all_texts": [s.text for s in spans],
    }
    pdoc.close()
    return report


def main() -> None:
    pdf_path = os.path.join(HERE, "form_like.pdf")
    png_path = os.path.join(HERE, "form_like.png")
    build_form(pdf_path)
    w, h = render_png(pdf_path, png_path)
    print(f"built form_like.pdf: rendered {w}x{h} -> form_like.png")

    report = confirm_overlap_merge(pdf_path)
    print(f"spans(0): {report['span_count']} boxes")
    print(f"all texts: {report['all_texts']}")
    if report["merged_boxes"]:
        for mb in report["merged_boxes"]:
            print(
                f"OVERLAP-MERGE OK: box '{mb['text']}' carries "
                f"{mb['redact_bboxes']} redact_bboxes (>1)"
            )
    else:
        print("WARNING: no overlap-merged box found -- fixture needs adjusting")


if __name__ == "__main__":
    main()
