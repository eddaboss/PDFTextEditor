"""Build the synthetic ACROFORM fixture for the forms build (ws5_forms §7).

PII-free, invented content only (names: Jordan Carter, Riley Morgan; company:
Acme Corp). Two US-Letter pages carrying a REAL AcroForm so detect / fill /
navigate / flatten are exercised against the widget machinery a user's form
PDFs actually use (``form_like.pdf`` stays the FLAT lookalike regression
control -- it has no widgets):

  Page 0 -- "Acme Corp Onboarding" heading (an embedded-Arial text span, the
  text-edit-coexistence target), field labels, then widgets:
    * text     ``employee_name``  (fontsize 11)
    * text     ``notes``          (multiline: PDF_TX_FIELD_IS_MULTILINE)
    * checkbox ``ack_policy``     (on-state "Yes")
    * combobox ``department``     (People Ops / Finance / Engineering)
    * radio    ``shift``          (kids Day / Night) -- built from RAW XREF
      objects because ``page.add_widget`` rejects PDF_WIDGET_TYPE_RADIOBUTTON
      in PyMuPDF 1.27 ("bad xref", probed): per kid two form-XObject
      appearance streams (on: a filled square, off: empty), the kid widget
      annot dict with /AS /Off and /Parent, a parent field <</FT/Btn
      /Ff 32768 /T(shift) /V/Off /Kids[...]>>, kids appended to the page
      /Annots array and the parent to the catalog AcroForm/Fields.
  Page 1 -- one plain span plus text field ``manager_name`` (the cross-page
  tab-wrap target).

Run with the project venv (Qt offscreen so nothing pops a window):

    QT_QPA_PLATFORM=offscreen \
      /Users/edward/Documents/GitHub/PDFTextEditor/.venv/bin/python \
      tests/fixtures/build_acroform_fixture.py

It writes acroform.pdf, renders acroform.png (both pages stacked, 1.5x), and
appends an idempotent manifest.md section (fenced by markers, so re-running
the builder never duplicates or clobbers other sections).
"""

from __future__ import annotations

import os

import fitz

HERE = os.path.dirname(os.path.abspath(__file__))
FONT_DIR = "/System/Library/Fonts/Supplemental"
ARIAL = f"{FONT_DIR}/Arial.ttf"
ARIAL_BOLD = f"{FONT_DIR}/Arial Bold.ttf"

PAGE_W, PAGE_H = 612, 792

INK = (0.10, 0.12, 0.16)
LABEL = (0.30, 0.34, 0.40)
BORDER = (0.62, 0.67, 0.74)

# (name, rect) per widget so the test suite can assert enumeration geometry.
FIELD_RECTS = {
    "employee_name": (220, 130, 480, 152),
    "notes": (220, 170, 480, 240),
    "ack_policy": (220, 262, 236, 278),
    "department": (220, 300, 400, 322),
    "shift_day": (220, 342, 236, 358),
    "shift_night": (300, 342, 316, 358),
    "manager_name": (220, 130, 480, 152),       # page 1
}
DEPARTMENTS = ["People Ops", "Finance", "Engineering"]


def _label(page, y, text):
    page.insert_text((72, y), text, fontname="AB",
                     fontfile=ARIAL_BOLD, fontsize=11, color=LABEL)


def _field_frame(page, rect):
    """A thin page-content border around a widget area (form chrome that is
    constant ink in every render -- the widgets themselves draw no border)."""
    page.draw_rect(fitz.Rect(rect), color=BORDER, width=0.8)


def _add_text_widget(page, name, rect, *, fontsize=11.0, flags=0):
    w = fitz.Widget()
    w.field_name = name
    w.field_type = fitz.PDF_WIDGET_TYPE_TEXT
    w.field_flags = flags
    w.rect = fitz.Rect(rect)
    w.text_fontsize = fontsize
    page.add_widget(w)


def _add_radio_group(doc, page, group_name, kids):
    """Build a radio group from raw xref objects (ws5_forms §7: ``add_widget``
    raises ``ValueError: bad xref`` for PDF_WIDGET_TYPE_RADIOBUTTON in 1.27).
    ``kids`` is ``[(on_state_name, rect_topleft_coords), ...]``. Rects in the
    raw annot dicts are PDF bottom-left coords: y' = page_height - y."""
    parent = doc.get_new_xref()
    doc.update_object(parent,
                      f"<</FT/Btn /Ff 32768 /T({group_name}) /V/Off>>")
    kid_xrefs = []
    for on_name, (x0, y0, x1, y1) in kids:
        on_x = doc.get_new_xref()
        doc.update_object(
            on_x, "<</Type/XObject/Subtype/Form/BBox[0 0 16 16]"
                  "/Resources<<>>>>")
        doc.update_stream(on_x, b"0 g 4 4 8 8 re f")
        off_x = doc.get_new_xref()
        doc.update_object(
            off_x, "<</Type/XObject/Subtype/Form/BBox[0 0 16 16]"
                   "/Resources<<>>>>")
        doc.update_stream(off_x, b"")
        kid = doc.get_new_xref()
        rect = f"[{x0} {PAGE_H - y1} {x1} {PAGE_H - y0}]"
        doc.update_object(
            kid,
            f"<</Type/Annot/Subtype/Widget/Rect{rect}/F 4/AS/Off"
            f"/Parent {parent} 0 R"
            f"/AP<</N<</{on_name} {on_x} 0 R/Off {off_x} 0 R>>>>"
            f"/MK<</BC[0 0 0]/CA(l)>>>>")
        kid_xrefs.append(kid)
    doc.xref_set_key(parent, "Kids",
                     "[" + " ".join(f"{k} 0 R" for k in kid_xrefs) + "]")
    # Hook the kids into the page's /Annots array and the parent into the
    # catalog's AcroForm/Fields (both are direct arrays here -- add_widget
    # already created them for the regular widgets above).
    ptype, annots = doc.xref_get_key(page.xref, "Annots")
    assert ptype == "array", f"page /Annots not an array: {ptype} {annots}"
    doc.xref_set_key(
        page.xref, "Annots",
        annots.rstrip("]") + " "
        + " ".join(f"{k} 0 R" for k in kid_xrefs) + "]")
    cat = doc.pdf_catalog()
    ftype, fields = doc.xref_get_key(cat, "AcroForm/Fields")
    assert ftype == "array", f"AcroForm/Fields not an array: {ftype} {fields}"
    doc.xref_set_key(cat, "AcroForm/Fields",
                     fields.rstrip("]") + f" {parent} 0 R]")


def build(path: str) -> None:
    doc = fitz.open()

    # --- page 0: heading span + the five fillable fields -------------------
    p0 = doc.new_page(width=PAGE_W, height=PAGE_H)
    # The text-edit-coexistence target: an EMBEDDED-Arial span the in-place
    # editor can redact-and-reinsert while the widgets on the page survive.
    p0.insert_text((72, 80), "Acme Corp Onboarding", fontname="AB",
                   fontfile=ARIAL_BOLD, fontsize=20, color=INK)
    p0.insert_text((72, 100), "Synthetic intake form (no real data)",
                   fontname="AR", fontfile=ARIAL, fontsize=10, color=LABEL)

    _label(p0, 144, "Employee name:")
    _field_frame(p0, FIELD_RECTS["employee_name"])
    _add_text_widget(p0, "employee_name", FIELD_RECTS["employee_name"],
                     fontsize=11.0)

    _label(p0, 184, "Notes:")
    _field_frame(p0, FIELD_RECTS["notes"])
    _add_text_widget(p0, "notes", FIELD_RECTS["notes"], fontsize=10.0,
                     flags=fitz.PDF_TX_FIELD_IS_MULTILINE)

    _label(p0, 274, "I acknowledge the policy:")
    _field_frame(p0, FIELD_RECTS["ack_policy"])
    w = fitz.Widget()
    w.field_name = "ack_policy"
    w.field_type = fitz.PDF_WIDGET_TYPE_CHECKBOX
    w.rect = fitz.Rect(FIELD_RECTS["ack_policy"])
    p0.add_widget(w)

    _label(p0, 314, "Department:")
    _field_frame(p0, FIELD_RECTS["department"])
    w = fitz.Widget()
    w.field_name = "department"
    w.field_type = fitz.PDF_WIDGET_TYPE_COMBOBOX
    w.rect = fitz.Rect(FIELD_RECTS["department"])
    w.choice_values = list(DEPARTMENTS)
    w.field_value = DEPARTMENTS[0]
    w.text_fontsize = 11.0          # 0 = auto-size scales to the rect height
    p0.add_widget(w)

    _label(p0, 354, "Shift:")
    p0.insert_text((240, 354), "Day", fontname="AR", fontfile=ARIAL,
                   fontsize=10, color=LABEL)
    p0.insert_text((320, 354), "Night", fontname="AR", fontfile=ARIAL,
                   fontsize=10, color=LABEL)
    _field_frame(p0, FIELD_RECTS["shift_day"])
    _field_frame(p0, FIELD_RECTS["shift_night"])
    _add_radio_group(doc, p0, "shift",
                     [("Day", FIELD_RECTS["shift_day"]),
                      ("Night", FIELD_RECTS["shift_night"])])

    p0.insert_text((72, PAGE_H - 48),
                   "Acme Corp HR  |  Prepared for Riley Morgan  |  Page 1 of 2",
                   fontname="AR", fontfile=ARIAL, fontsize=9, color=LABEL)

    # --- page 1: one plain span + the cross-page tab-wrap target -----------
    p1 = doc.new_page(width=PAGE_W, height=PAGE_H)
    p1.insert_text((72, 80), "Manager review sheet", fontname="AB",
                   fontfile=ARIAL_BOLD, fontsize=16, color=INK)
    _label(p1, 144, "Manager name:")
    _field_frame(p1, FIELD_RECTS["manager_name"])
    _add_text_widget(p1, "manager_name", FIELD_RECTS["manager_name"],
                     fontsize=11.0)

    doc.save(path, garbage=4, deflate=True)
    doc.close()


def render_png(pdf_path: str, png_path: str, zoom: float = 1.5) -> None:
    """Both pages stacked vertically into one verification PNG (the
    build_pages_fixtures pattern: Pillow paste with gray gaps)."""
    from PIL import Image
    doc = fitz.open(pdf_path)
    pixes = [doc[i].get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
             for i in range(doc.page_count)]
    width = max(p.width for p in pixes)
    gap = 16
    height = sum(p.height for p in pixes) + gap * (len(pixes) - 1)
    canvas = Image.new("RGB", (width, height), (228, 230, 234))
    y = 0
    for p in pixes:
        img = Image.frombytes("RGB", (p.width, p.height), p.samples)
        canvas.paste(img, (0, y))
        y += p.height + gap
    canvas.save(png_path)
    doc.close()


def verify(pdf_path: str) -> dict:
    """Reopen and report the widget census the test suite depends on."""
    doc = fitz.open(pdf_path)
    report = {"is_form_pdf": doc.is_form_pdf, "pages": doc.page_count,
              "widgets": []}
    for pi in range(doc.page_count):
        for w in doc[pi].widgets():
            report["widgets"].append(
                (pi, w.field_type_string, w.field_name, w.field_value,
                 tuple(w.rect), w.field_flags, w.on_state()))
    doc.close()
    return report


MANIFEST_BEGIN = "<!-- ACROFORM FIXTURE -->"
MANIFEST_END = "<!-- /ACROFORM FIXTURE -->"


def manifest_section(report: dict) -> str:
    rows = "\n".join(
        f"| {pi} | {kind} | `{name}` | {value!r} | "
        f"({r[0]:.0f}, {r[1]:.0f}, {r[2]:.0f}, {r[3]:.0f}) | {flags} | "
        f"{on or '—'} |"
        for (pi, kind, name, value, r, flags, on) in report["widgets"])
    return f"""# AcroForm Fixture

Built by **`build_acroform_fixture.py`** for the **forms** build (detect /
fill / navigate / flatten). PII-free, invented content with fake neutral
names only (Jordan Carter, Riley Morgan, Acme Corp). Two US-Letter pages
carrying a REAL AcroForm (`doc.is_form_pdf == {report['is_form_pdf']}`,
a truthy field COUNT); `form_like.pdf` remains the FLAT no-widget
regression control.

### 12. `acroform.pdf` — 2 pages, 7 widgets ({report['is_form_pdf']} fields)

Page 0 carries the "Acme Corp Onboarding" heading (embedded Arial Bold, the
text-edit-coexistence target) plus labels and five fields; page 1 carries one
plain span and the cross-page tab-wrap target.

| Page | Type | Field name | Initial value | Rect (text space) | Flags | On-state |
| --- | --- | --- | --- | --- | --- | --- |
{rows}

- The `shift` radio group is built from **raw xref objects** (parent
  `/FT/Btn /Ff 32768`, kid widgets with `/AS /Off` + per-kid on/off form
  XObjects) because `page.add_widget` rejects radio widgets in PyMuPDF 1.27;
  once built, `page.widgets()` enumerates both kids with `on_state()`
  Day / Night.
- `notes` is multiline (`PDF_TX_FIELD_IS_MULTILINE`, flags 4096);
  `department` is a combobox over People Ops / Finance / Engineering with
  initial value `People Ops`; `ack_policy`'s on-state is `Yes`.
- Field borders are page CONTENT (thin gray frames), not widget borders, so
  widget ink deltas isolate the filled values.
- **Verification:** the builder reopens the file and asserts the census
  above; `acroform.png` (both pages stacked, 1.5x) is committed alongside.

### How to rebuild

```sh
QT_QPA_PLATFORM=offscreen \\
  /Users/edward/Documents/GitHub/PDFTextEditor/.venv/bin/python \\
  tests/fixtures/build_acroform_fixture.py
```
"""


def append_manifest(report: dict) -> str:
    """Append (or replace in place) the acroform section of manifest.md,
    fenced by BEGIN/END markers so re-running is idempotent and never
    clobbers sections other builders own."""
    manifest_path = os.path.join(HERE, "manifest.md")
    block = f"{MANIFEST_BEGIN}\n{manifest_section(report)}\n{MANIFEST_END}\n"
    existing = ""
    if os.path.exists(manifest_path):
        with open(manifest_path, "r") as fh:
            existing = fh.read()
    if MANIFEST_BEGIN in existing and MANIFEST_END in existing:
        head, rest = existing.split(MANIFEST_BEGIN, 1)
        tail = rest.split(MANIFEST_END, 1)[1]
        new_content = head + block + tail
    else:
        new_content = existing.rstrip("\n") + "\n\n---\n\n" + block
    with open(manifest_path, "w") as fh:
        fh.write(new_content)
    return manifest_path


def main() -> None:
    pdf_path = os.path.join(HERE, "acroform.pdf")
    png_path = os.path.join(HERE, "acroform.png")
    build(pdf_path)
    render_png(pdf_path, png_path)
    report = verify(pdf_path)

    expected = {
        (0, "Text", "employee_name"), (0, "Text", "notes"),
        (0, "CheckBox", "ack_policy"), (0, "ComboBox", "department"),
        (0, "RadioButton", "shift"), (1, "Text", "manager_name"),
    }
    got = {(pi, kind, name) for (pi, kind, name, *_rest) in report["widgets"]}
    assert got == expected, f"widget census wrong: {sorted(got)}"
    radio_states = sorted(on for (_pi, kind, _n, _v, _r, _f, on)
                          in report["widgets"] if kind == "RadioButton")
    assert radio_states == ["Day", "Night"], radio_states
    assert report["is_form_pdf"], "saved fixture lost its AcroForm"
    assert len(report["widgets"]) == 7, report["widgets"]

    append_manifest(report)
    print(f"built acroform.pdf: {report['pages']} pages, "
          f"{len(report['widgets'])} widgets, "
          f"is_form_pdf={report['is_form_pdf']}")
    for row in report["widgets"]:
        print("  ", row)
    print("rendered acroform.png; manifest.md section updated")


if __name__ == "__main__":
    main()
