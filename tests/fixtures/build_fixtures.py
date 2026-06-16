"""Build realistic PDF test fixtures that embed REAL macOS system fonts.

Each fixture is laid out by hand with PyMuPDF so we control baselines, sizes,
colors, and which exact font file backs every run of text. The goal is to give
the in-place text editor something faithful to a real-world document: embedded
TrueType fonts (not base-14 substitutes), proper line geometry, mixed styles,
color, a subsetted font (missing-glyph fallback), and a two-family page.

Run with the project venv:
    QT_QPA_PLATFORM=offscreen \
      /Users/edward/Documents/GitHub/PDFTextEditor/.venv/bin/python \
      tests/fixtures/build_fixtures.py

It writes the .pdf fixtures, renders each to a verification .png, probes
get_fonts(full=True) / extract_font() for every fixture, and emits manifest.md.
"""

from __future__ import annotations

import json
import os

import fitz

HERE = os.path.dirname(os.path.abspath(__file__))
FONT_DIR = "/System/Library/Fonts/Supplemental"

FONTS = {
    "arial": f"{FONT_DIR}/Arial.ttf",
    "arial_bold": f"{FONT_DIR}/Arial Bold.ttf",
    "arial_italic": f"{FONT_DIR}/Arial Italic.ttf",
    "times": f"{FONT_DIR}/Times New Roman.ttf",
    "times_bold": f"{FONT_DIR}/Times New Roman Bold.ttf",
    "times_italic": f"{FONT_DIR}/Times New Roman Italic.ttf",
    "georgia": f"{FONT_DIR}/Georgia.ttf",
    "georgia_bold": f"{FONT_DIR}/Georgia Bold.ttf",
    "georgia_italic": f"{FONT_DIR}/Georgia Italic.ttf",
    "comic": f"{FONT_DIR}/Comic Sans MS.ttf",
    "comic_bold": f"{FONT_DIR}/Comic Sans MS Bold.ttf",
}

# RGB tuples in 0..1, matching PyMuPDF's color convention.
BLACK = (0.0, 0.0, 0.0)
NAVY = (0.10, 0.16, 0.45)
CRIMSON = (0.79, 0.09, 0.13)
FOREST = (0.13, 0.45, 0.20)
PLUM = (0.45, 0.16, 0.50)
SLATE = (0.28, 0.33, 0.40)

PAGE_W, PAGE_H = 612, 792  # US Letter


def line_writer(page, x, y, leading):
    """Return a stateful writer that lays down baseline-aligned lines.

    Each call places one run at the current baseline; advance() drops to the
    next line by `leading` points. Keeping x/y explicit means the fixtures have
    honest, predictable baseline geometry for the editor to read back.
    """
    state = {"x": x, "y": y}

    def write(text, font_file, size, color=BLACK, dx=0.0):
        nonlocal state
        fontname = "F" + str(abs(hash((font_file, text))) % 100000)
        page.insert_text(
            (state["x"] + dx, state["y"]),
            text,
            fontname=fontname,
            fontfile=font_file,
            fontsize=size,
            color=color,
        )
        # crude advance so inline runs on one line don't overlap
        return size * 0.5 * len(text)

    def newline(by=None):
        state["y"] += by if by is not None else leading

    def move(nx=None, ny=None):
        if nx is not None:
            state["x"] = nx
        if ny is not None:
            state["y"] = ny

    write.newline = newline
    write.move = move
    write.state = state
    return write


# --- individual fixtures -------------------------------------------------


def build_body_paragraphs(path):
    """Multi-paragraph body text, several real-sentence lines per paragraph.

    Single family (Times New Roman), single size, single color: the clean
    baseline case for paragraph reflow and line-geometry handling.
    """
    doc = fitz.open()
    page = doc.new_page(width=PAGE_W, height=PAGE_H)
    w = line_writer(page, 72, 96, leading=18)
    size = 11
    paragraphs = [
        [
            "The harbor woke slowly under a thin coastal fog, and the fishing",
            "boats nudged against their moorings as the tide began to turn. Gulls",
            "wheeled over the breakwater, calling to one another in the gray light",
            "while the first dock crews shuffled out with thermoses of coffee.",
        ],
        [
            "By mid-morning the fog had burned away and the water turned a hard,",
            "bright blue. Tourists drifted along the boardwalk, pausing at the",
            "stalls that sold smoked fish and hand-knotted rope, and the smell of",
            "salt and frying batter hung over the whole waterfront like a banner.",
        ],
        [
            "Old Marten kept his shop at the far end of the pier, where the planks",
            "creaked loudest and the rent was cheapest. He had mended nets there",
            "for forty years, and he claimed he could read the coming weather in",
            "the way the canvas awnings snapped against their frames each dawn.",
        ],
    ]
    for para in paragraphs:
        for ln in para:
            w(ln, FONTS["times"], size)
            w.newline()
        w.newline(by=10)  # paragraph gap
    doc.save(path, garbage=4, deflate=True)
    doc.close()


def build_multi_size(path):
    """One page, one family (Arial), several distinct font sizes."""
    doc = fitz.open()
    page = doc.new_page(width=PAGE_W, height=PAGE_H)
    w = line_writer(page, 72, 120, leading=0)
    rows = [
        ("Quarterly Field Report", 28),
        ("Coastal Operations, Northern Division", 18),
        ("Prepared for the regional review board, second quarter.", 13),
        ("Section 1. Summary of observed conditions across all monitored sites.", 11),
        ("Footnote: figures are provisional and subject to later revision.", 8),
    ]
    for text, size in rows:
        w(text, FONTS["arial"], size)
        w.newline(by=size * 1.7)
    doc.save(path, garbage=4, deflate=True)
    doc.close()


def build_bold_italic(path):
    """Regular / bold / italic / bold-italic runs of one family (Georgia)."""
    doc = fitz.open()
    page = doc.new_page(width=PAGE_W, height=PAGE_H)
    w = line_writer(page, 72, 110, leading=30)
    size = 15
    # Each line demonstrates one style with a real sentence.
    w("This sentence is set in regular Georgia roman.", FONTS["georgia"], size)
    w.newline()
    w("This sentence is set in Georgia bold for emphasis.", FONTS["georgia_bold"], size)
    w.newline()
    w("This sentence is set in Georgia italic for a title.", FONTS["georgia_italic"], size)
    w.newline()
    # Mixed-style single line: regular + bold + italic runs side by side.
    base_y = w.state["y"]
    page.insert_text((72, base_y), "Inline styles: ",
                     fontname="Fmixreg", fontfile=FONTS["georgia"], fontsize=size, color=BLACK)
    page.insert_text((178, base_y), "bold word",
                     fontname="Fmixbold", fontfile=FONTS["georgia_bold"], fontsize=size, color=BLACK)
    page.insert_text((268, base_y), " then ",
                     fontname="Fmixreg2", fontfile=FONTS["georgia"], fontsize=size, color=BLACK)
    page.insert_text((320, base_y), "italic word",
                     fontname="Fmixital", fontfile=FONTS["georgia_italic"], fontsize=size, color=BLACK)
    page.insert_text((420, base_y), " on one line.",
                     fontname="Fmixreg3", fontfile=FONTS["georgia"], fontsize=size, color=BLACK)
    doc.save(path, garbage=4, deflate=True)
    doc.close()


def build_colored(path):
    """Non-black text in several colors, one family (Arial)."""
    doc = fitz.open()
    page = doc.new_page(width=PAGE_W, height=PAGE_H)
    w = line_writer(page, 72, 120, leading=34)
    size = 16
    w("Navy heading for the colored-text fixture.", FONTS["arial_bold"], size, color=NAVY)
    w.newline()
    w("Crimson line marking an important warning.", FONTS["arial"], size, color=CRIMSON)
    w.newline()
    w("Forest green note about a passing status.", FONTS["arial"], size, color=FOREST)
    w.newline()
    w("Plum colored aside in the running text.", FONTS["arial_italic"], size, color=PLUM)
    w.newline()
    w("Slate gray caption beneath the figure above.", FONTS["arial"], 11, color=SLATE)
    doc.save(path, garbage=4, deflate=True)
    doc.close()


def build_subset(path):
    """Full Comic Sans embedded, then doc.subset_fonts() prunes unused glyphs.

    This is the missing-glyph fallback case: after subsetting, the embedded
    font carries only the glyphs actually used on the page, so any attempt to
    reinsert characters outside that set must fall back. The basename gains a
    six-letter subset prefix (e.g. ABCDEF+Comic Sans MS).
    """
    doc = fitz.open()
    page = doc.new_page(width=PAGE_W, height=PAGE_H)
    w = line_writer(page, 72, 120, leading=26)
    size = 16
    # Deliberately narrow alphabet so subsetting bites hard.
    w("Subset fixture: handle the cat.", FONTS["comic"], size, color=BLACK)
    w.newline()
    w("Add a dance and call the band.", FONTS["comic"], size, color=CRIMSON)
    w.newline()
    w("Bold tail end here.", FONTS["comic_bold"], size, color=NAVY)
    # Subset in place: drops every glyph not used above.
    doc.subset_fonts(verbose=False)
    doc.save(path, garbage=4, deflate=True)
    doc.close()


def build_mixed_families(path):
    """Two families on one page: Times New Roman body with Comic Sans MS heads."""
    doc = fitz.open()
    page = doc.new_page(width=PAGE_W, height=PAGE_H)
    w = line_writer(page, 72, 110, leading=18)
    # Comic Sans heading
    w("Mixed Families Memo", FONTS["comic_bold"], 22, color=NAVY)
    w.newline(by=30)
    # Times body paragraph
    body = [
        "This paragraph is typeset in Times New Roman to stand in for ordinary",
        "running body copy. It carries the bulk of the page in a quiet serif",
        "face so the reader's eye settles into a steady rhythm down the lines.",
    ]
    for ln in body:
        w(ln, FONTS["times"], 12, color=BLACK)
        w.newline()
    w.newline(by=12)
    # Comic Sans subhead
    w("A friendlier aside", FONTS["comic"], 16, color=CRIMSON)
    w.newline(by=24)
    more = [
        "Here the voice shifts into Comic Sans MS to mark a casual note, the",
        "kind of callout a designer drops in to soften a dense report.",
    ]
    for ln in more:
        w(ln, FONTS["comic"], 12, color=SLATE)
        w.newline()
    doc.save(path, garbage=4, deflate=True)
    doc.close()


FIXTURES = [
    ("body_paragraphs.pdf", "Multi-paragraph body text", build_body_paragraphs),
    ("multi_size.pdf", "Multiple font sizes on one page", build_multi_size),
    ("bold_italic.pdf", "Bold and italic runs", build_bold_italic),
    ("colored_text.pdf", "Colored (non-black) text", build_colored),
    ("subset_font.pdf", "Subsetted font (missing-glyph fallback)", build_subset),
    ("mixed_families.pdf", "Two families on one page", build_mixed_families),
]


def probe(path):
    """Return the per-fixture font report from get_fonts/extract_font."""
    doc = fitz.open(path)
    report = {"page_count": doc.page_count, "fonts": []}
    page = doc[0]
    for entry in page.get_fonts(full=True):
        xref, ext, ftype, basename, refname, encoding = entry[:6]
        try:
            ef = doc.extract_font(xref)
            ef_basename, ef_ext, ef_type, content = ef
            extracted = {
                "basename": ef_basename,
                "ext": ef_ext,
                "type": ef_type,
                "bytes": len(content) if content else 0,
                "subset_prefix": "+" in (ef_basename or ""),
            }
        except Exception as exc:  # pragma: no cover - defensive
            extracted = {"error": str(exc)}
        report["fonts"].append({
            "xref": xref,
            "ext": ext,
            "type": ftype,
            "basename": basename,
            "refname": refname,
            "encoding": encoding,
            "extract_font": extracted,
        })
    doc.close()
    return report


def render_png(pdf_path, png_path):
    doc = fitz.open(pdf_path)
    pix = doc[0].get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
    pix.save(png_path)
    doc.close()
    return pix.width, pix.height


def main():
    reports = {}
    for filename, _desc, builder in FIXTURES:
        pdf_path = os.path.join(HERE, filename)
        builder(pdf_path)
        # Verify it opens and renders without error.
        png_path = os.path.join(HERE, filename.replace(".pdf", ".png"))
        w, h = render_png(pdf_path, png_path)
        reports[filename] = {
            "render": {"png": os.path.basename(png_path), "w": w, "h": h},
            "probe": probe(pdf_path),
        }
        print(f"built {filename}: rendered {w}x{h} -> {os.path.basename(png_path)}")

    with open(os.path.join(HERE, "font_reports.json"), "w") as fh:
        json.dump(reports, fh, indent=2)
    print("wrote font_reports.json")
    return reports


if __name__ == "__main__":
    main()
