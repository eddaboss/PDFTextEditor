"""Build PII-FREE fixtures for PARAGRAPH-REFLOW and CONTINUOUS-SCROLL testing.

This is the fixture pair for the "smart paragraph grouping + auto reflow" and
"continuous vertical scroll" build (the North Star: change the words in an
existing PDF and leave zero trace). Both files are laid out by hand with PyMuPDF
so every baseline, size, color, and leading is honest and predictable, and every
run embeds a REAL macOS system face via ``fontfile=`` (same convention as
``build_fixtures.py`` / ``build_pages_fixtures.py``), so spans extract through the
editor's font tiers exactly like the existing fixtures.

Two fixtures:

``paragraphs.pdf`` — ONE page that MIRRORS THE REAL PROBLEM. It contains a
genuine multi-line body paragraph (a single paragraph wrapped over 5 lines at
one font/size/leading) that the grouping pass must MERGE into ONE editable box;
PLUS, on the same page, clearly SEPARATE elements that must NOT merge into it or
into each other: a big bold heading, a two-column label:value table, a short
two-line unrelated note set apart, and a bulleted list. Today every visual line
is its own span/block, so the paragraph's five lines are FIVE separate boxes:
the bug. The grouping engine is validated against this page.

``multipage_body.pdf`` — FOUR pages of flowing body text (two body paragraphs
per page, each multi-line) for continuous-scroll (lazy render of visible pages,
fit-to-width, scroll across page boundaries) and cross-page find/replace testing.
A distinctive shared phrase ("monitoring cadence") is seeded once per page so a
document-wide find/replace has a known number of hits to verify.

Run with the project venv:
    QT_QPA_PLATFORM=offscreen \
      /Users/edward/Documents/GitHub/PDFTextEditor/.venv/bin/python \
      tests/fixtures/build_reflow_fixtures.py

It writes the two .pdf fixtures, renders each to a verification .png (every page
of the multipage file stacked vertically), confirms each opens through the real
``PDFDocument`` model layer, reports the PER-LINE spans / bboxes / leading of the
target paragraph (proving the lines are currently SEPARATE — the bug to fix), and
appends a manifest section to ``manifest.md``.
"""

from __future__ import annotations

import json
import os
import sys

import fitz

HERE = os.path.dirname(os.path.abspath(__file__))
# Make ``pdftexteditor`` importable so we can confirm each fixture opens through
# the real model layer and inspect the spans it extracts (repo root is two up).
REPO_ROOT = os.path.dirname(os.path.dirname(HERE))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

FONT_DIR = "/System/Library/Fonts/Supplemental"
# Real system faces. Arial stands in for Helvetica (the de-facto metric-compatible
# pairing macOS ships), Times New Roman for Times — the exact files the existing
# fixtures embed, so the font tiers behave identically here.
FONTS = {
    "arial": f"{FONT_DIR}/Arial.ttf",
    "arial_bold": f"{FONT_DIR}/Arial Bold.ttf",
    "arial_italic": f"{FONT_DIR}/Arial Italic.ttf",
    "times": f"{FONT_DIR}/Times New Roman.ttf",
    "times_bold": f"{FONT_DIR}/Times New Roman Bold.ttf",
    "times_italic": f"{FONT_DIR}/Times New Roman Italic.ttf",
}

# RGB tuples in 0..1, PyMuPDF convention.
BLACK = (0.0, 0.0, 0.0)
NAVY = (0.10, 0.16, 0.45)
SLATE = (0.28, 0.33, 0.40)
CRIMSON = (0.79, 0.09, 0.13)
FOREST = (0.13, 0.45, 0.20)

PAGE_W, PAGE_H = 612, 792  # US Letter portrait
LEFT = 72                  # left text margin
RIGHT = PAGE_W - 72        # right text margin (column width = 468 pt)


def _font_for(font_file: str, text: str) -> str:
    """Per-(file,text) registration name, mirroring the other fixture builders so
    each distinct run embeds its own face reference."""
    return "F" + str(abs(hash((font_file, text))) % 100000)


def _norm(text: str) -> str:
    """Normalize extracted text for MATCHING/COUNTING only.

    PyMuPDF's ``insert_text`` maps the ASCII space glyph to U+00A0 (NBSP) in the
    text it later extracts via ``get_text`` / ``rawdict`` for these TrueType
    faces (verified on this system, PyMuPDF 1.x). That is faithful to many real
    PDFs (extracted whitespace is frequently a space VARIANT), but it means a
    naive equality / substring check against an ASCII-space string would miss.
    This collapses NBSP and the soft-hyphen U+00AD back to their ASCII forms so
    the verification report identifies the paragraph and counts find/replace
    hits correctly. It is NOT applied to the PDF content itself — the fixtures
    keep their real, as-extracted text so the editor sees genuine PDF whitespace.
    """
    return text.replace(" ", " ").replace("­", "-")


def write(page, xy, text, font_file, size, color=BLACK):
    """Place one baseline-aligned run; geometry is explicit (no auto layout)."""
    page.insert_text(
        xy, text, fontname=_font_for(font_file, text),
        fontfile=font_file, fontsize=size, color=color,
    )


def write_block(page, x, y, lines, font_file, size, leading, color=BLACK):
    """Lay down ``lines`` as consecutive baselines starting at (x, y), each
    dropped by ``leading`` points. Returns the y of the NEXT free baseline.

    This is exactly how a real document stacks the lines of one paragraph: same
    font, same size, constant leading, same left x. The editor currently turns
    each of these baselines into its OWN span — the multi-line-paragraph bug the
    grouping pass exists to fix.
    """
    cy = y
    for ln in lines:
        write(page, (x, cy), ln, font_file, size, color=color)
        cy += leading
    return cy


# --- paragraphs.pdf ------------------------------------------------------

# The TARGET paragraph: ONE body paragraph, hand-wrapped to 5 lines at the SAME
# 11pt Times, SAME 15pt leading, SAME left margin. Grouping must collapse these
# five baselines into a SINGLE editable, reflowable box. Invented content.
TARGET_PARAGRAPH = [
    "The quarterly operations review will convene in the east annex on the",
    "second Tuesday of the month, beginning promptly at nine in the morning,",
    "and every team lead is expected to bring a short written summary of the",
    "milestones reached since the previous session along with any blockers that",
    "still need a decision from the wider group before the next planning cycle.",
]
TARGET_LEADING = 15.0
TARGET_SIZE = 11.0
TARGET_TOP_BASELINE = 168.0  # baseline of the paragraph's first line


def build_paragraphs(path):
    """One page mirroring the real problem: a multi-line paragraph that must
    GROUP into one box, surrounded by elements that must STAY SEPARATE.

    Layout (top to bottom):
      - Heading (20pt Arial Bold, navy) — bigger/bolder, must not join the body.
      - The 5-line target body paragraph (11pt Times, 15pt leading).
      - A 2-column label:value table (4 rows) — labels left, values mid-page;
        each cell is its own field and must stay separate (the overlap-merge
        pass only merges true overlaps, never a label next to its value).
      - A short 2-line unrelated note (10pt Times Italic, slate), set apart by a
        larger gap and a different style, so it must not fold into the paragraph.
      - A bulleted list (4 bullets, 11pt Times) — each bullet is its own line and
        must remain its own item, NOT merge into a paragraph.
    """
    doc = fitz.open()
    page = doc.new_page(width=PAGE_W, height=PAGE_H)

    # 1) Heading — distinct size + weight + color, sits well above the body.
    write(page, (LEFT, 110), "Operations Planning Notice",
          FONTS["arial_bold"], 20, color=NAVY)

    # 2) The target multi-line paragraph (the grouping target).
    after_para = write_block(
        page, LEFT, TARGET_TOP_BASELINE, TARGET_PARAGRAPH,
        FONTS["times"], TARGET_SIZE, TARGET_LEADING, color=BLACK,
    )

    # 3) Two-column label:value table. Labels at LEFT, values at a fixed second
    #    column (x=250). Bold labels, regular values, one row per pair. Distinct
    #    fields: the editor must NOT merge a label with its value or with the
    #    row above/below.
    table_rows = [
        ("Meeting date", "Second Tuesday, 9:00 AM"),
        ("Location", "East Annex, Room 4"),
        ("Facilitator", "Rotating chair"),
        ("Deliverable", "One-page milestone summary"),
    ]
    col1_x, col2_x = LEFT, 250
    ty = after_para + 24  # gap below the paragraph
    table_leading = 20.0
    table_top = ty
    for label, value in table_rows:
        write(page, (col1_x, ty), label, FONTS["times_bold"], 11, color=BLACK)
        write(page, (col2_x, ty), value, FONTS["times"], 11, color=BLACK)
        ty += table_leading

    # 4) Short 2-line unrelated note, set apart in italic slate with a bigger gap
    #    so it is visually and stylistically distinct from the body paragraph.
    note_top = ty + 26
    note_lines = [
        "Note: parking in the annex lot is limited, so arrive early or",
        "use the overflow structure across the service road.",
    ]
    write_block(page, LEFT, note_top, note_lines,
                FONTS["times_italic"], 10, 14.0, color=SLATE)

    # 5) Bulleted list — each bullet its own item; must not collapse into a
    #    paragraph. Bullet glyph + a hanging-indent text start.
    bullets = [
        "Confirm attendance by end of week.",
        "Submit milestone summaries in advance.",
        "Flag any cross-team dependencies.",
        "Reserve audiovisual equipment if presenting.",
    ]
    by = note_top + 14.0 * len(note_lines) + 30
    bullet_leading = 18.0
    bullet_top = by
    for b in bullets:
        write(page, (LEFT, by), "•", FONTS["times"], 11, color=CRIMSON)
        write(page, (LEFT + 16, by), b, FONTS["times"], 11, color=BLACK)
        by += bullet_leading

    doc.save(path, garbage=4, deflate=True)
    doc.close()
    return {
        "heading_baseline": 110,
        "target_paragraph": {
            "top_baseline": TARGET_TOP_BASELINE,
            "size": TARGET_SIZE,
            "leading": TARGET_LEADING,
            "n_lines": len(TARGET_PARAGRAPH),
            "left_x": LEFT,
        },
        "table_top": table_top,
        "table_rows": len(table_rows),
        "note_top": note_top,
        "bullet_top": bullet_top,
        "n_bullets": len(bullets),
    }


# --- multipage_body.pdf --------------------------------------------------

# Four pages, two multi-line body paragraphs each. Same family/size/leading so
# every paragraph is a grouping target across a SCROLLING document. A shared
# phrase "monitoring cadence" appears EXACTLY ONCE per page so a document-wide
# find/replace has a known, page-spanning hit count (4) to verify. Invented.
MULTIPAGE_PARAGRAPHS = [
    # Page 0
    [
        [
            "The field station logged a quiet week, with instruments holding steady",
            "across every recorded channel and no alarms tripping after the storm",
            "front cleared the ridge on Monday. The crew used the lull to recalibrate",
            "the older sensors and to revisit the monitoring cadence that had drifted",
            "out of step during the busier stretch earlier in the season.",
        ],
        [
            "By the weekend the reservoir had risen a few inches and the access road",
            "was passable again, so the supply run went out on schedule and returned",
            "with the replacement filters everyone had been waiting on for two weeks.",
        ],
    ],
    # Page 1
    [
        [
            "Analysis of the spring samples confirmed what the rough field notes had",
            "already suggested, namely that the turbidity spikes tracked the rainfall",
            "almost exactly and faded within a day once the inflow settled. The lab",
            "recommended tightening the monitoring cadence around storm events so the",
            "first flush is captured before it dilutes into the broader record.",
        ],
        [
            "A second batch, drawn from the lower basin, told a calmer story and gave",
            "the team a clean baseline to compare the disturbed upstream readings",
            "against when they prepare the mid-year report for the oversight board.",
        ],
    ],
    # Page 2
    [
        [
            "The equipment budget came in under the projected ceiling, largely because",
            "two of the planned replacements were deferred after a closer inspection",
            "showed the existing units would last another full season with minor",
            "servicing. Those savings were redirected toward a spare data logger and a",
            "longer monitoring cadence trial on the north transect.",
        ],
        [
            "Training for the incoming seasonal staff is scheduled for the last week of",
            "the month, and the handbook has been revised to fold in the lessons from",
            "the previous rotation so the new crew starts on firmer footing.",
        ],
    ],
    # Page 3
    [
        [
            "Looking ahead, the steering group wants the next phase to emphasize",
            "consistency over coverage, trimming a few of the redundant sampling sites",
            "in favor of a denser record at the locations that actually drive the model.",
            "Holding a steady monitoring cadence at those anchor points, they argued,",
            "will do more for the long-term trend than scattering effort thinly.",
        ],
        [
            "The season will close with a public summary and an open dataset, both of",
            "which are already drafted and waiting only on the final quarter of",
            "readings before they can be released to the wider community.",
        ],
    ],
]
MP_SIZE = 11.0
MP_LEADING = 15.0
MP_PARA_GAP = 12.0          # extra gap between the two paragraphs on a page
MP_TOP_BASELINE = 110.0     # first baseline on each page (below the page label)
MP_FIND_PHRASE = "monitoring cadence"


def build_multipage_body(path):
    """Four pages of flowing body text (2 paragraphs/page) for continuous-scroll
    + cross-page find/replace. Each page carries a small label so scroll position
    is verifiable; the shared phrase appears once per page (4 total)."""
    doc = fitz.open()
    per_page = []
    for pi, paragraphs in enumerate(MULTIPAGE_PARAGRAPHS):
        page = doc.new_page(width=PAGE_W, height=PAGE_H)
        # Small page label (own block, distinct style) so a scrolled view can be
        # identified and so it never folds into the body grouping.
        write(page, (LEFT, 72), f"Site Bulletin — Page {pi + 1} of 4",
              FONTS["arial_bold"], 12, color=NAVY)
        y = MP_TOP_BASELINE
        para_tops = []
        for para in paragraphs:
            para_tops.append(y)
            y = write_block(page, LEFT, y, para,
                            FONTS["times"], MP_SIZE, MP_LEADING, color=BLACK)
            y += MP_PARA_GAP
        per_page.append({
            "page": pi,
            "n_paragraphs": len(paragraphs),
            "para_line_counts": [len(p) for p in paragraphs],
            "para_top_baselines": [round(t, 1) for t in para_tops],
        })
    doc.save(path, garbage=4, deflate=True)
    doc.close()
    return per_page


# --- verification + reporting --------------------------------------------

def render_png(pdf_path, png_path, zoom=2.0):
    """Render the FIRST page of ``pdf_path`` to ``png_path``."""
    doc = fitz.open(pdf_path)
    pix = doc[0].get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
    pix.save(png_path)
    doc.close()
    return pix.width, pix.height


def render_all_pages_png(pdf_path, png_path, zoom=1.5):
    """Render EVERY page stacked vertically into one PNG (for the multipage
    file), so a single image shows the whole document for scroll inspection."""
    doc = fitz.open(pdf_path)
    mat = fitz.Matrix(zoom, zoom)
    pix_list = [doc[i].get_pixmap(matrix=mat, alpha=False)
                for i in range(doc.page_count)]
    try:
        from PIL import Image
    except Exception:
        base, ext = os.path.splitext(png_path)
        for i, p in enumerate(pix_list):
            p.save(f"{base}_p{i + 1}{ext}")
        doc.close()
        return max(p.width for p in pix_list), sum(p.height for p in pix_list)
    gap = 16
    width = max(p.width for p in pix_list)
    height = sum(p.height for p in pix_list) + gap * (len(pix_list) - 1)
    canvas = Image.new("RGB", (width, height), (228, 230, 234))
    yoff = 0
    for p in pix_list:
        img = Image.frombytes("RGB", (p.width, p.height), p.samples)
        canvas.paste(img, (0, yoff))
        yoff += p.height + gap
    canvas.save(png_path)
    doc.close()
    return width, height


def _classify(span):
    """Best-effort element tag for a span on paragraphs.pdf, by geometry/style,
    purely for the human-readable report (NOT used by the app)."""
    x0, y0, x1, y1 = span.bbox
    if round(span.size) >= 18:
        return "heading"
    if y0 < 200:
        return "heading-area"
    return "body"


def inspect_paragraphs(pdf_path, layout):
    """Open paragraphs.pdf through the REAL model and prove the target paragraph
    is currently FIVE SEPARATE spans (the bug), reporting each line's bbox +
    the measured leading between consecutive baselines."""
    from pdftexteditor.document import PDFDocument

    pdoc = PDFDocument(pdf_path)
    spans = pdoc.spans(0)
    total_spans = len(spans)

    # The target paragraph: spans whose text matches the known paragraph lines.
    # Normalize NBSP so the ASCII-space TARGET_PARAGRAPH strings match the
    # extracted text (PyMuPDF maps inserted spaces to U+00A0; see _norm).
    target_texts = {_norm(ln).strip() for ln in TARGET_PARAGRAPH}
    para_spans = [s for s in spans if _norm(s.text).strip() in target_texts]
    # Order them top-to-bottom by baseline y.
    para_spans.sort(key=lambda s: s.origin[1])

    lines = []
    prev_baseline = None
    for s in para_spans:
        baseline_y = s.origin[1]
        leading = (None if prev_baseline is None
                   else round(baseline_y - prev_baseline, 2))
        prev_baseline = baseline_y
        lines.append({
            "text": _norm(s.text),
            "bbox": tuple(round(v, 2) for v in s.bbox),
            "origin": tuple(round(v, 2) for v in s.origin),
            "size": round(s.size, 2),
            "font": s.font,
            "block_index": s.block_index,
            "line_index": s.line_index,
            "span_index": s.span_index,
            "leading_from_prev": leading,
        })

    # Distinct-box check for the separate elements: count spans that are NOT part
    # of the target paragraph (heading, table cells, note, bullets) so we can
    # assert they did not merge into the paragraph or vanish.
    non_para = [s for s in spans if _norm(s.text).strip() not in target_texts]

    pdoc.close()
    return {
        "total_spans": total_spans,
        "target_paragraph_span_count": len(para_spans),
        "target_paragraph_lines": lines,
        "non_paragraph_span_count": len(non_para),
        "non_paragraph_sample": [
            {"text": _norm(s.text), "size": round(s.size, 2),
             "bbox": tuple(round(v, 2) for v in s.bbox)}
            for s in sorted(non_para, key=lambda s: s.origin[1])
        ],
        "layout": layout,
    }


def inspect_multipage(pdf_path, per_page):
    """Open multipage_body.pdf through the model: confirm 4 pages, per-page span
    counts, and the per-page count of the cross-page find/replace phrase."""
    from pdftexteditor.document import PDFDocument

    pdoc = PDFDocument(pdf_path)
    page_count = pdoc.page_count
    pages = []
    phrase_total = 0
    for i in range(page_count):
        spans = pdoc.spans(i)
        # Normalize NBSP before counting the ASCII-space find phrase.
        hits = sum(_norm(s.text).count(MP_FIND_PHRASE) for s in spans)
        phrase_total += hits
        pages.append({
            "page": i,
            "span_count": len(spans),
            "phrase_hits": hits,
            "layout": per_page[i],
        })
    pdoc.close()
    return {
        "page_count": page_count,
        "find_phrase": MP_FIND_PHRASE,
        "find_phrase_total_hits": phrase_total,
        "pages": pages,
    }


MANIFEST_MARKER = "<!-- REFLOW FIXTURES -->"


def append_manifest(report):
    """Append (or replace) the reflow-fixtures section of manifest.md, fenced by
    a marker so re-running the builder is idempotent."""
    manifest_path = os.path.join(HERE, "manifest.md")
    section = _manifest_section(report)
    block = f"\n{MANIFEST_MARKER}\n{section}\n"
    existing = ""
    if os.path.exists(manifest_path):
        with open(manifest_path, "r") as fh:
            existing = fh.read()
    if MANIFEST_MARKER in existing:
        head = existing.split(MANIFEST_MARKER, 1)[0].rstrip("\n")
        new_content = head + "\n" + block
    else:
        new_content = existing.rstrip("\n") + "\n" + block
    with open(manifest_path, "w") as fh:
        fh.write(new_content)
    return manifest_path


def _manifest_section(report):
    p = report["paragraphs"]
    m = report["multipage"]
    para = p["inspect"]
    lines_tbl = "\n".join(
        f"| {i} | `({ln['block_index']},{ln['line_index']},{ln['span_index']})` "
        f"| {ln['bbox']} | {ln['origin']} | {ln['size']} "
        f"| {ln['leading_from_prev'] if ln['leading_from_prev'] is not None else '—'} "
        f"| {ln['text'][:46]}{'…' if len(ln['text']) > 46 else ''} |"
        for i, ln in enumerate(para["target_paragraph_lines"])
    )
    pages_tbl = "\n".join(
        f"| {pg['page']} | {pg['span_count']} | {pg['phrase_hits']} "
        f"| {pg['layout']['para_line_counts']} |"
        for pg in m["inspect"]["pages"]
    )
    return f"""## Reflow & continuous-scroll fixtures

Built by `build_reflow_fixtures.py` for the **smart paragraph grouping + auto
reflow** and **continuous vertical scroll** build. PII-free, invented content;
real system faces embedded via `fontfile=` (Arial for Helvetica, Times New
Roman), so spans extract through the editor's font tiers like every other fixture.

### `paragraphs.pdf` — the multi-line-paragraph bug, plus must-not-merge elements

One US-Letter page (612 × 792) holding, top to bottom:

- **Heading** — `Operations Planning Notice`, 20 pt Arial Bold, navy.
- **Target body paragraph** — ONE paragraph hand-wrapped to **{p['layout']['target_paragraph']['n_lines']} lines** at
  **{p['layout']['target_paragraph']['size']} pt Times**, constant **{p['layout']['target_paragraph']['leading']} pt leading**, left x =
  {p['layout']['target_paragraph']['left_x']}. Grouping must merge these into **one** editable, reflowable box.
- **2-column label:value table** — {p['layout']['table_rows']} rows (bold label at x={LEFT}, value at
  x=250). Distinct fields: must NOT merge label↔value or row↔row.
- **Short 2-line note** — 10 pt Times Italic, slate, set apart by a wider gap.
- **Bulleted list** — {p['layout']['n_bullets']} bullets (crimson `•` + 11 pt Times text). Each bullet is
  its own item; must NOT collapse into a paragraph.

**The bug, measured through `PDFDocument.spans(0)`:** the target paragraph is
currently **{para['target_paragraph_span_count']} SEPARATE spans** (one per visual line), not one box. Total spans
on the page: **{para['total_spans']}** ({para['non_paragraph_span_count']} of them the heading / table / note / bullets).
Grouping must collapse the five rows below into ONE box WITHOUT swallowing any of
the {para['non_paragraph_span_count']} surrounding spans.

Per-line spans of the target paragraph (proof they are separate; leading is the
baseline-to-baseline delta and is constant, the signature grouping keys on):

| Line | (blk,line,span) | bbox | origin | size | leading | text |
| --- | --- | --- | --- | --- | --- | --- |
{lines_tbl}

### `multipage_body.pdf` — continuous scroll + cross-page find/replace

**{m['inspect']['page_count']} pages**, two multi-line body paragraphs per page (11 pt Times, 15 pt
leading), each page labelled `Site Bulletin — Page N of 4`. For continuous
vertical scroll (lazy render of visible pages, fit-to-width, scrolling across
page boundaries). The phrase **`{m['inspect']['find_phrase']}`** appears **exactly once per page**
({m['inspect']['find_phrase_total_hits']} total) so a document-wide find/replace has a known, page-spanning
hit count to verify.

| Page | Spans | `{m['inspect']['find_phrase']}` hits | Paragraph line counts |
| --- | --- | --- | --- |
{pages_tbl}

> **Whitespace note (important for grouping + find/replace):** PyMuPDF's
> `insert_text` maps the ASCII space glyph to **U+00A0 (NBSP)** in the text these
> fixtures extract via `get_text` / `rawdict` (verified on this system), and the
> soft hyphen in a word like `One-page` extracts as **U+00AD**. This is faithful
> to real PDFs, where extracted whitespace is often a space variant. Word-level
> grouping, reflow tokenization, and find/replace must therefore treat U+00A0 as
> a word separator (and normalize U+00AD) — a naive search for an ASCII-space
> phrase returns **zero** hits. The hit counts above are measured AFTER
> normalizing NBSP→space, so they reflect what a NBSP-aware find/replace sees.

### Verification

Both fixtures open through `pdftexteditor.document.PDFDocument`. `paragraphs.png`
and `multipage_body.png` (all four pages stacked) are written alongside for
visual inspection. The per-line report above is emitted by the builder so the
grouping engine can be validated: after grouping, `spans(0)`-equivalent box
enumeration should report the five paragraph rows as **one** box while leaving
the heading, the {p['layout']['table_rows']} table rows, the 2-line note, and the {p['layout']['n_bullets']} bullets as
distinct boxes.
"""


def main():
    paragraphs_pdf = os.path.join(HERE, "paragraphs.pdf")
    multipage_pdf = os.path.join(HERE, "multipage_body.pdf")

    layout = build_paragraphs(paragraphs_pdf)
    per_page = build_multipage_body(multipage_pdf)

    p_png = os.path.join(HERE, "paragraphs.png")
    m_png = os.path.join(HERE, "multipage_body.png")
    pw, ph = render_png(paragraphs_pdf, p_png)
    mw, mh = render_all_pages_png(multipage_pdf, m_png)

    para_inspect = inspect_paragraphs(paragraphs_pdf, layout)
    mp_inspect = inspect_multipage(multipage_pdf, per_page)

    report = {
        "paragraphs": {
            "pdf": "paragraphs.pdf",
            "png": {"file": "paragraphs.png", "w": pw, "h": ph},
            "layout": layout,
            "inspect": para_inspect,
        },
        "multipage": {
            "pdf": "multipage_body.pdf",
            "png": {"file": "multipage_body.png", "w": mw, "h": mh},
            "per_page": per_page,
            "inspect": mp_inspect,
        },
    }

    manifest_path = append_manifest(report)

    # Console summary.
    print(f"built paragraphs.pdf -> paragraphs.png ({pw}x{ph})")
    print(f"  total spans on page: {para_inspect['total_spans']}")
    print(f"  TARGET PARAGRAPH is {para_inspect['target_paragraph_span_count']} "
          f"SEPARATE spans (the bug to fix):")
    for i, ln in enumerate(para_inspect["target_paragraph_lines"]):
        lead = ln["leading_from_prev"]
        print(f"    line {i}: key={ (ln['block_index'], ln['line_index'], ln['span_index']) } "
              f"bbox={ln['bbox']} size={ln['size']} "
              f"leading={'(first)' if lead is None else lead} :: {ln['text'][:50]}")
    print(f"  non-paragraph spans (heading/table/note/bullets): "
          f"{para_inspect['non_paragraph_span_count']}")
    print()
    print(f"built multipage_body.pdf -> multipage_body.png ({mw}x{mh})")
    print(f"  page_count={mp_inspect['page_count']}, "
          f"phrase '{mp_inspect['find_phrase']}' hits/page="
          f"{[pg['phrase_hits'] for pg in mp_inspect['pages']]} "
          f"(total {mp_inspect['find_phrase_total_hits']})")
    for pg in mp_inspect["pages"]:
        print(f"    page {pg['page']}: spans={pg['span_count']}, "
              f"phrase_hits={pg['phrase_hits']}, "
              f"para_lines={pg['layout']['para_line_counts']}")
    print()
    print(f"appended manifest section -> {os.path.basename(manifest_path)}")
    return report


if __name__ == "__main__":
    result = main()
    print("\n=== JSON REPORT ===")
    print(json.dumps(result, indent=2))
