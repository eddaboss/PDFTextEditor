"""Build the NAVIGATION fixture (navigation workstream M1).

``outline_doc.pdf``: five US-Letter pages of invented onboarding-agenda
content (fake neutral names only: Acme Corp, Jordan Carter, Riley Morgan)
carrying a REAL three-entry outline installed via ``set_toc`` --

    [[1, "Welcome", 1], [2, "Agenda", 2], [1, "Policies", 4]]

-- so the bookmark-wipe regression (``insert_pdf`` drops the outline, wiping
bookmarks on every save) and the outline()/set_outline()/BookmarkPanel build
are testable against a committed file. All text is BASE-14 Helvetica (no
font dependencies): the navigation suite stages an in-place edit on this
fixture, and base-14 faces ride the editor's font tiers without any system
font requirement.

Body lines are spaced 28 pt apart (past the paragraph-grouping first-gap
ceiling at 2.2 x 12 = 26.4 pt) so each line stays its own editable box. The
word "handbook" appears EXACTLY once in the document (page 1) so the
edit-then-save TOC regression has an unambiguous target.

Run with the project venv:
    QT_QPA_PLATFORM=offscreen \
      /Users/edward/Documents/GitHub/PDFTextEditor/.venv/bin/python \
      tests/fixtures/build_nav_fixture.py

It writes outline_doc.pdf, renders a stacked verification outline_doc.png,
appends/replaces the marker-fenced navigation section of manifest.md
(idempotent), then reopens the fixture through PDFDocument and prints
outline / page / span facts.
"""

from __future__ import annotations

import os
import sys

import fitz

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(os.path.dirname(HERE))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

BLACK = (0.0, 0.0, 0.0)
NAVY = (0.10, 0.16, 0.45)
SLATE = (0.28, 0.33, 0.40)

LETTER_W, LETTER_H = 612, 792
# Body lines 28 pt apart at 12 pt so each stays its OWN box (the grouping
# first-gap ceiling is 2.2 * size = 26.4 pt).
LINE_STEP = 28

TOC = [[1, "Welcome", 1], [2, "Agenda", 2], [1, "Policies", 4]]

_PAGES = [
    ("Acme Corp Onboarding Agenda", [
        "Welcome to Acme Corp. This packet covers your first week.",
        "Facilitator: Jordan Carter, people operations.",
        "Keep this agenda with the employee handbook for reference.",
        "Questions between sessions go to the onboarding desk.",
    ]),
    ("Agenda", [
        "Day one opens with introductions and a building tour.",
        "Jordan Carter walks through accounts, badges, and equipment.",
        "Riley Morgan hosts the team lunch and the floor walk.",
        "The afternoon closes with benefits enrollment.",
    ]),
    ("Schedule Details", [
        "Sessions start on the hour and run fifty minutes.",
        "Rooms are listed on the daily sheet at the front desk.",
        "Remote attendees join on the bridge line posted each morning.",
        "Breaks are built in between every pair of sessions.",
    ]),
    ("Policies", [
        "Review the conduct and security policies before day two.",
        "Riley Morgan collects the signed acknowledgment forms.",
        "Expense and travel policies apply from your start date.",
        "Policy questions route to people operations.",
    ]),
    ("Notes", [
        "Use this page for your own notes during the sessions.",
        "Your manager schedules the thirty-day check-in.",
        "The buddy program pairs you with a tenured teammate.",
        "Welcome aboard from the whole Acme Corp crew.",
    ]),
]


def write(page, xy, text, size, color=BLACK, bold=False):
    """Base-14 text only (helv / Helvetica-Bold): no font files, no deps."""
    page.insert_text(xy, text, fontname="hebo" if bold else "helv",
                     fontsize=size, color=color)


def build(path: str) -> None:
    doc = fitz.open()
    for i, (head, lines) in enumerate(_PAGES):
        page = doc.new_page(width=LETTER_W, height=LETTER_H)
        page.draw_rect(fitz.Rect(0, 0, LETTER_W, 64), color=None, fill=NAVY)
        write(page, (54, 42), head, 22, color=(1, 1, 1), bold=True)
        y = 120
        for ln in lines:
            write(page, (54, y), ln, 12)
            y += LINE_STEP
        write(page, (54, LETTER_H - 48),
              f"Onboarding sheet {i + 1} of {len(_PAGES)}", 10, color=SLATE)
    doc.set_toc(TOC)
    doc.save(path)
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


MANIFEST_BEGIN = "<!-- NAV FIXTURES -->"
MANIFEST_END = "<!-- /NAV FIXTURES -->"

_MANIFEST_SECTION = f"""## Navigation fixture (bookmarks / outline)

Built by `build_nav_fixture.py` for the **navigation, menus & chrome**
workstream. PII-free, invented content (Acme Corp, Jordan Carter, Riley
Morgan); BASE-14 Helvetica only, so the fixture has zero font dependencies.

### `outline_doc.pdf` — 5 letter pages WITH a real outline

The bookmark fixture: the only committed PDF whose outline is populated, so
the regression "every save silently wipes the bookmarks" and the
outline()/set_outline()/BookmarkPanel build are testable against a real file.

- **Outline:** `{TOC!r}` (one nested entry under "Welcome"; "Policies"
  targets page 4, so `delete_page(1)` remaps it and dangles "Agenda").
- **Pages:** 5 x US Letter portrait (612 x 792), navy header band + white
  Helvetica-Bold 22 pt heading per page, four 12 pt Helvetica body lines
  spaced 28 pt apart (past the paragraph-grouping first-gap ceiling, so each
  line is its own editable box), 10 pt slate footer.
- **Fonts:** base-14 `helv` / `hebo` only — no embedded files, no system
  font requirement.
- **Edit target:** the word `handbook` appears **exactly once** in the whole
  document (page 1 body), so the edit-then-save TOC regression is
  unambiguous.
- **Verification:** `outline_doc.png` (all five pages stacked, 1.5x zoom) is
  committed alongside; the builder re-opens the file through `PDFDocument`
  and asserts the outline carry plus the single-`handbook` contract.

```sh
QT_QPA_PLATFORM=offscreen \\
  /Users/edward/Documents/GitHub/PDFTextEditor/.venv/bin/python \\
  tests/fixtures/build_nav_fixture.py
```"""


def append_manifest() -> str:
    """Append (or replace) the navigation section of manifest.md, fenced by
    BEGIN/END markers so re-running the builder is idempotent and never
    disturbs the sections of other workstreams."""
    manifest_path = os.path.join(HERE, "manifest.md")
    block = f"{MANIFEST_BEGIN}\n{_MANIFEST_SECTION}\n{MANIFEST_END}\n"
    existing = ""
    if os.path.exists(manifest_path):
        with open(manifest_path, "r") as fh:
            existing = fh.read()
    if MANIFEST_BEGIN in existing and MANIFEST_END in existing:
        head, rest = existing.split(MANIFEST_BEGIN, 1)
        tail = rest.split(MANIFEST_END, 1)[1]
        new_content = head + block + tail
    else:
        new_content = existing.rstrip("\n") + "\n\n" + block
    with open(manifest_path, "w") as fh:
        fh.write(new_content)
    return manifest_path


def main() -> None:
    from pdftexteditor.document import PDFDocument

    pdf_path = os.path.join(HERE, "outline_doc.pdf")
    png_path = os.path.join(HERE, "outline_doc.png")
    build(pdf_path)
    w, h = render_all_pages_png(pdf_path, png_path)
    print(f"built outline_doc.pdf: {os.path.getsize(pdf_path)} B -> "
          f"outline_doc.png ({w}x{h})")
    append_manifest()
    print("manifest.md navigation section updated")

    raw = fitz.open(pdf_path)
    assert raw.get_toc() == TOC, f"outline mismatch: {raw.get_toc()!r}"
    print(f"  on-disk outline: {raw.get_toc()!r}")
    handbook_hits = sum(
        raw[i].get_text("text").replace(" ", " ").count("handbook")
        for i in range(raw.page_count))
    print(f"  'handbook' occurrences (whole doc): {handbook_hits}")
    assert handbook_hits == 1, "fixture contract: exactly one 'handbook'"
    raw.close()

    doc = PDFDocument(pdf_path)
    assert doc.outline() == TOC, f"carry failed: {doc.outline()!r}"
    print(f"  PDFDocument: pages={doc.page_count} outline={doc.outline()!r}")
    print(f"  spans per page: "
          f"{[len(doc.spans(i)) for i in range(doc.page_count)]}")
    doc.close()


if __name__ == "__main__":
    main()
