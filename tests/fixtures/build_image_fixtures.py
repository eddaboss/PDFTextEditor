"""Build the EXISTING-IMAGES fixture: ``image_doc.pdf`` (images & signatures
§8 M3).

Two US-Letter pages of embedded-font text (real macOS system faces via
``fontfile=``, the ``build_fixtures.py`` convention) with FAKE NEUTRAL names
only ("Jordan Carter", "Acme Corp", "Riley Morgan") -- PII-free, invented
content, and ONE procedural RGBA gradient "logo" image whose single xref is
placed on BOTH pages (the shared-logo scenario):

  page 0 -- heading "Jordan Carter — Acme Corp", body lines, and the logo at
  ``PAGE0_LOGO_RECT`` (top-right). The M3 suite stages this occurrence's
  deletion and asserts the page-scoped image-REMOVE redaction spares the
  page text AND the same xref's occurrence on page 1 (the §0 probe, kept as
  a regression).

  page 1 -- the SAME xref reinserted at ``PAGE1_LOGO_RECT`` (smaller,
  top-left) over its own body text.

The logo is generated in code (no external files): a left-to-right red->blue
color ramp with a top-to-bottom alpha ramp (opaque top, fully transparent
bottom rows), so inserting it via ``stream=`` auto-creates an SMask -- the
move-macro test can assert transparency survives the extract + reinsert
round trip. Both placements pass ``keep_proportion=False`` so the authored
rects round-trip ``get_image_info`` EXACTLY (probe-pinned).

Idempotent; writes ONLY inside tests/fixtures/ (the .pdf, a verification
.png, and this builder's fenced manifest.md section). Run with the project
venv:
    QT_QPA_PLATFORM=offscreen \
      /Users/edward/Documents/GitHub/PDFTextEditor/.venv/bin/python \
      tests/fixtures/build_image_fixtures.py
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
}

BLACK = (0.0, 0.0, 0.0)
NAVY = (0.10, 0.16, 0.45)
SLATE = (0.28, 0.33, 0.40)

LETTER_W, LETTER_H = 612, 792

# The authored logo rects (unrotated text-space PDF points). The M3 suite
# imports these so its asserts can never drift from the builder.
PAGE0_LOGO_RECT = (392.0, 96.0, 512.0, 176.0)     # top-right, 120 x 80
PAGE1_LOGO_RECT = (72.0, 96.0, 162.0, 156.0)      # top-left, 90 x 60
LOGO_PX = (120, 80)                               # source pixel size

PAGE0_BODY = [
    "This certificate template is maintained by",
    "Jordan Carter for the Acme Corp onboarding",
    "program. The logo block in the corner is a",
    "placed image, separate from the body text,",
    "so removing or moving it must leave every",
    "printed word exactly where it stands today.",
]

PAGE1_BODY = [
    "The second sheet reuses the same logo asset at a smaller size,",
    "the way a letterhead reuses one embedded resource. Riley Morgan",
    "keeps this page as the distribution copy for Acme Corp partners.",
]


def _font_for(font_file: str, text: str) -> str:
    """Per-(file,text) registration name (the build_fixtures.py convention)."""
    return "F" + str(abs(hash((font_file, text))) % 100000)


def write(page, xy, text, font_file, size, color=BLACK):
    page.insert_text(
        xy, text, fontname=_font_for(font_file, text),
        fontfile=font_file, fontsize=size, color=color,
    )


def make_logo_png(w: int = LOGO_PX[0], h: int = LOGO_PX[1]) -> bytes:
    """The procedural RGBA gradient logo: a red->blue color ramp left to
    right with the BOTTOM QUARTER fully transparent (hard alpha cut -- the
    Pixmap PNG writer premultiplies partial alpha, so binary alpha keeps
    every committed pixel value exact). The placed image MUST carry an
    SMask, and any page ink under the transparent band survives."""
    pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, w, h), 1)
    cut = h - h // 4                  # rows >= cut are fully transparent
    for y in range(h):
        alpha = 255 if y < cut else 0
        for x in range(w):
            t = x / max(w - 1, 1)
            pix.set_pixel(x, y, (int(255 * (1 - t)), 0, int(255 * t), alpha))
    return pix.tobytes("png")


def build(path: str) -> tuple:
    doc = fitz.open()

    # --- page 0: text + the logo occurrence the tests delete -------------
    p0 = doc.new_page(width=LETTER_W, height=LETTER_H)
    write(p0, (72, 90), "Jordan Carter — Acme Corp", FONTS["arial_bold"], 20,
          color=NAVY)
    y = 140
    for line in PAGE0_BODY:
        write(p0, (72, y), line, FONTS["georgia"], 12)
        y += 17
    write(p0, (72, 740), "Template owner: Jordan Carter", FONTS["arial"], 10,
          color=SLATE)
    logo = make_logo_png()
    xref = p0.insert_image(fitz.Rect(PAGE0_LOGO_RECT), stream=logo,
                           keep_proportion=False)

    # --- page 1: the SAME xref again (the shared-logo scenario) ----------
    p1 = doc.new_page(width=LETTER_W, height=LETTER_H)
    write(p1, (72, 220), "Distribution Copy", FONTS["arial_bold"], 16,
          color=NAVY)
    y = 260
    for line in PAGE1_BODY:
        write(p1, (72, y), line, FONTS["georgia"], 12)
        y += 17
    write(p1, (72, 740), "Reviewed weekly by Riley Morgan", FONTS["arial"],
          10, color=SLATE)
    p1.insert_image(fitz.Rect(PAGE1_LOGO_RECT), stream=logo,
                    keep_proportion=False, xref=xref)

    doc.save(path, garbage=4, deflate=True)
    doc.close()
    return xref, len(logo)


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


def verify(path: str) -> dict:
    """The fixture opens through the real model layer; both pages carry ONE
    occurrence of ONE shared xref at the authored rects, with an SMask."""
    from pdftexteditor.document import PDFDocument

    doc = fitz.open(path)
    assert doc.page_count == 2, doc.page_count
    info0 = doc[0].get_image_info(xrefs=True)
    info1 = doc[1].get_image_info(xrefs=True)
    assert len(info0) == 1 and len(info1) == 1, (info0, info1)
    assert info0[0]["xref"] == info1[0]["xref"], "logo xref must be shared"
    for info, rect in ((info0[0], PAGE0_LOGO_RECT),
                       (info1[0], PAGE1_LOGO_RECT)):
        got = tuple(round(v, 2) for v in info["bbox"])
        want = tuple(round(v, 2) for v in rect)
        assert got == want, (got, want)
    smask = doc.extract_image(info0[0]["xref"])["smask"]
    assert smask, "the RGBA logo must carry an SMask"
    xref = info0[0]["xref"]
    doc.close()

    model = PDFDocument(path)
    spans = [len(model.spans(i)) for i in range(model.page_count)]
    xims = [[(x.xref, x.occ) for x in model.existing_images(i)]
            for i in range(model.page_count)]
    model.close()
    print(f"image_doc.pdf: 2 pages, spans per page {spans}, shared logo "
          f"xref {xref}, existing_images {xims}")
    return {"spans": spans, "xref": xref}


MANIFEST_BEGIN = "<!-- IMAGE FIXTURES BEGIN -->"
MANIFEST_END = "<!-- IMAGE FIXTURES END -->"


def _manifest_section(report: dict) -> str:
    return f"""## Existing-images fixture (images & signatures M3)

Built by **`build_image_fixtures.py`**. PII-free, invented content with fake
neutral names only (Jordan Carter, Acme Corp, Riley Morgan).

### `image_doc.pdf` — one RGBA logo xref shared by two pages

The existing-image management fixture: detect / page-scoped delete / move of
images already in the file.

- **Pages:** 2 × US Letter portrait (612 × 792), embedded Arial/Georgia text
  (spans per page {report['spans']}).
- **Logo:** ONE procedural 120 × 80 RGBA gradient (red→blue across, the
  bottom quarter fully transparent, so the embedded image carries an
  **SMask**). The SAME xref is placed on page 1 at
  `{PAGE0_LOGO_RECT}` and on page 2 at `{PAGE1_LOGO_RECT}` (both inserted
  with `keep_proportion=False`, so `get_image_info` round-trips the authored
  rects exactly).
- **Contract:** deleting the page-1 occurrence (scoped image-REMOVE
  redaction) must spare the page-1 text and the page-2 occurrence; the move
  macro must preserve transparency through `extract_image_bytes`.
- **Verification:** `image_doc.png` (both pages stacked, 1.5×) is committed
  alongside; the builder re-opens the file through `PDFDocument` and asserts
  the shared xref + authored rects + SMask.

### How to rebuild

```sh
QT_QPA_PLATFORM=offscreen \\
  /Users/edward/Documents/GitHub/PDFTextEditor/.venv/bin/python \\
  tests/fixtures/build_image_fixtures.py
```"""


def append_manifest(report: dict) -> str:
    """Append (or replace in place) this builder's fenced manifest section --
    idempotent (the build_reflow_fixtures.py pattern, with an END marker so
    re-running never clobbers sections appended after this one)."""
    manifest_path = os.path.join(HERE, "manifest.md")
    block = f"{MANIFEST_BEGIN}\n{_manifest_section(report)}\n{MANIFEST_END}"
    existing = ""
    if os.path.exists(manifest_path):
        with open(manifest_path, "r") as fh:
            existing = fh.read()
    if MANIFEST_BEGIN in existing and MANIFEST_END in existing:
        head, rest = existing.split(MANIFEST_BEGIN, 1)
        _, tail = rest.split(MANIFEST_END, 1)
        new_content = head + block + tail
    else:
        new_content = existing.rstrip("\n") + "\n\n" + block + "\n"
    with open(manifest_path, "w") as fh:
        fh.write(new_content)
    return manifest_path


if __name__ == "__main__":
    out = os.path.join(HERE, "image_doc.pdf")
    build(out)
    render_png(out, os.path.join(HERE, "image_doc.png"))
    report = verify(out)
    append_manifest(report)
    print("built", out)
