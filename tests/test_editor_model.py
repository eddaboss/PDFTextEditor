"""Headless verification for the FULL-editor MODEL + FONT layers (EDITOR_SPEC §8).

Exercises every new capability the way save_as / render_with_edits will, with
zero tolerated exceptions, on tests/fixtures/form_like.pdf plus a sweep of the
existing fixtures:

  1. resolve_family is NEVER Tier 1 and ALWAYS embeddable + glyph-covering.
  2. set_style round-trips: family + size + color + bold change the saved face,
     real ink, old text gone, never base-14 tofu; render_with_edits == save.
  3. move_box shifts the saved ink; resize_box scales the saved font size.
  4. delete_box removes the box's ink while a neighbor cell/border survives
     (non-destructive redaction held).
  5. add_box draws new text at ~origin with an embeddable font; undo removes it,
     redo restores it.
  6. A mixed text->style->move->delete->add sequence, 5x undo returns the doc to
     pristine, 5x redo reproduces the final state (one generalized history).

Run:
    QT_QPA_PLATFORM=offscreen \
      /Users/edward/Documents/GitHub/PDFTextEditor/.venv/bin/python \
      tests/test_editor_model.py
"""

from __future__ import annotations

import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import fitz  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

_APP = QApplication.instance() or QApplication([])

# Headless safety net: the offscreen Qt platform on Windows does not auto-load
# the OS font database, so the SYSTEM-tier fixtures' families report unavailable
# to Qt and resolve falls to BASE14. Register them so a headless run exercises
# the same SYSTEM-tier path the real GUI app does.
from pdftexteditor.font_engine import FontEngine as _FE  # noqa: E402
_FE.register_system_fonts_with_qt(
    ("Arial", "Times New Roman", "Georgia", "Comic Sans MS"))

from pdftexteditor.document import PDFDocument  # noqa: E402
from pdftexteditor.font_engine import (  # noqa: E402
    TIER_BASE14,
    TIER_EMBEDDED,
    TIER_SYSTEM,
    FontEngine,
)

FIXTURE_DIR = os.path.join(_HERE, "fixtures")
FORM = os.path.join(FIXTURE_DIR, "form_like.pdf")
_TIER = {TIER_EMBEDDED: "EMBEDDED", TIER_SYSTEM: "SYSTEM", TIER_BASE14: "BASE14"}

_BASE14_BASEFONTS = {
    "helvetica", "helvetica-bold", "helvetica-oblique", "helvetica-boldoblique",
    "times-roman", "times-bold", "times-italic", "times-bolditalic",
    "courier", "courier-bold", "courier-oblique", "courier-boldoblique",
    "symbol", "zapfdingbats",
}


def check(failures, tag, cond, msg):
    if not cond:
        failures.append(f"{tag}: {msg}")
    return cond


def region_ink(path, bbox, scale=3.0):
    doc = fitz.open(path)
    try:
        pix = doc[0].get_pixmap(matrix=fitz.Matrix(scale, scale),
                                clip=fitz.Rect(bbox), alpha=False)
        s, step, n = pix.samples, pix.n, 0
        for i in range(0, len(s), step):
            r, g, b = s[i], s[i + 1], s[i + 2]
            if (0.299 * r + 0.587 * g + 0.114 * b) < 230:
                n += 1
        return n
    finally:
        doc.close()


def saved_basefonts(path):
    doc = fitz.open(path)
    try:
        out = []
        for entry in doc[0].get_fonts(full=True):
            base = entry[3] or ""
            if len(base) > 7 and base[6] == "+" and base[:6].isalpha():
                base = base[7:]
            out.append(base.lower())
        return out
    finally:
        doc.close()


def saved_span(path, want_text):
    """First span on page 0 whose text contains want_text -> dict, else None."""
    doc = fitz.open(path)
    try:
        for block in doc[0].get_text("rawdict")["blocks"]:
            if block.get("type", 0) != 0:
                continue
            for line in block["lines"]:
                for span in line["spans"]:
                    text = "".join(c["c"] for c in span.get("chars", []))
                    # PyMuPDF emits a non-breaking space (U+00A0) between words;
                    # normalize so a plain-space query still matches.
                    if want_text in text.replace(" ", " "):
                        return {"text": text, "origin": tuple(span["origin"]),
                                "size": span["size"], "bbox": tuple(span["bbox"]),
                                "font": span["font"],
                                "color": tuple(c / 255 for c in
                                               fitz.sRGB_to_rgb(span["color"]))}
        return None
    finally:
        doc.close()


def has_words(path, *words):
    """True iff every word appears (as a whole word) on page 0 -- robust to the
    space-folding / block-ordering of get_text(), which can split a freshly
    inserted run so the literal joined string is not a substring of get_text()
    even though the glyphs are all on the page."""
    doc = fitz.open(path)
    try:
        present = {w[4] for w in doc[0].get_text("words")}
        return all(w in present for w in words)
    finally:
        doc.close()


def new_base14(before_path, after_path):
    """Base-14 builtin basefonts present AFTER the edit that were NOT there in
    the original file -- so the document's own pre-existing standard-font
    references (a form that references Helvetica/Arial without embedding) are
    not mistaken for a substitution introduced by an edit."""
    before = set(saved_basefonts(before_path)) & _BASE14_BASEFONTS
    after = set(saved_basefonts(after_path)) & _BASE14_BASEFONTS
    return sorted(after - before)


def find_span(doc, contains):
    for s in doc.spans(0):
        if contains in s.text:
            return s
    return None


def _pix_ink(pix):
    s, step, n = pix.samples, pix.n, 0
    for i in range(0, len(s), step):
        r, g, b = s[i], s[i + 1], s[i + 2]
        if (0.299 * r + 0.587 * g + 0.114 * b) < 230:
            n += 1
    return n


def wysiwyg_match(doc, out_path, scale=2.0):
    """Whole-page ink of render_with_edits vs the saved file -- the baked
    WYSIWYG invariant (§0.1): the on-screen page is the SAME pipeline as save."""
    rpix = doc.render_with_edits(0, scale)
    rink = _pix_ink(rpix)
    sd = fitz.open(out_path)
    try:
        spix = sd[0].get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
    finally:
        sd.close()
    sink = _pix_ink(spix)
    return abs(rink - sink) / max(sink, 1)


def save(doc):
    out = os.path.join(tempfile.gettempdir(),
                       f"editmodel_{next(_ctr)}.pdf")
    doc.save_as(out)
    return out


def _counter():
    i = 0
    while True:
        yield i
        i += 1


_ctr = _counter()


# --------------------------------------------------------------------------
# 1. resolve_family is never Tier 1, always embeddable + covering
# --------------------------------------------------------------------------
def test_resolve_family(failures):
    tag = "resolve_family"
    doc = PDFDocument(FORM)
    eng = doc.font_engine
    fams = FontEngine.available_families()
    check(failures, tag, len(fams) > 3, "available_families looks empty")
    text = "Zephyr 1234 Kx Qophs"
    tier1 = tofu = 0
    sys_n = b14_n = 0
    for fam in fams:
        for bold in (False, True):
            for italic in (False, True):
                rf = eng.resolve_family(fam, bold, italic, text)
                if rf.tier == TIER_EMBEDDED:
                    tier1 += 1
                    continue
                fobj = eng.fitz_font_for(rf)
                if not FontEngine.font_covers(fobj, text):
                    tofu += 1
                if rf.tier == TIER_SYSTEM:
                    sys_n += 1
                else:
                    b14_n += 1
    check(failures, tag, tier1 == 0,
          f"{tier1} family resolutions returned TIER_EMBEDDED (must never)")
    check(failures, tag, tofu == 0, f"{tofu} family resolutions tofu")
    doc.close()
    if not failures:
        print(f"  {tag:20} {len(fams)} families x3 styles -> "
              f"SYSTEM={sys_n} BASE14={b14_n}, no Tier1, no tofu")


# --------------------------------------------------------------------------
# 2. set_style round-trips
# --------------------------------------------------------------------------
def test_style(failures):
    tag = "set_style"
    doc = PDFDocument(FORM)
    box = find_span(doc, "Sample Report")
    if not check(failures, tag, box is not None, "target span not found"):
        doc.close()
        return
    orig_text = box.text
    doc.set_style(0, box, font_family="Georgia", size=34.0,
                  color=(0.85, 0.1, 0.1), bold=True)
    check(failures, tag, doc.edit_count == 1, "edit_count != 1 after style")

    # render_with_edits must agree with the save (baked WYSIWYG).
    pix = doc.render_with_edits(0, 2.0)
    check(failures, tag, pix is not None, "render_with_edits returned None")

    out = save(doc)
    basefonts = saved_basefonts(out)
    introduced = new_base14(FORM, out)
    check(failures, tag, not introduced,
          f"styled save INTRODUCED a base-14 substitute: {introduced}")
    check(failures, tag, any("georgia" in b for b in basefonts),
          f"saved page does not carry Georgia: {basefonts}")
    ss = saved_span(out, "Sample Report")
    check(failures, tag, ss is not None and has_words(out, "Sample", "Report"),
          "styled text not extractable")
    if ss:
        check(failures, tag, abs(ss["size"] - 34.0) < 1.5,
              f"saved size {ss['size']:.1f} != 34")
        check(failures, tag, ss["color"][0] > 0.5 and ss["color"][1] < 0.4,
              f"saved color {ss['color']} not red-ish")
    check(failures, tag, region_ink(out, box.bbox) > 50, "no ink after style")
    wd = wysiwyg_match(doc, out)
    check(failures, tag, wd < 0.02,
          f"render_with_edits != saved (rel ink diff {wd:.4f}) -- WYSIWYG broke")
    doc.undo()
    check(failures, tag, doc.edit_count == 0, "undo did not clear style")
    es = doc.effective_style(0, box)
    check(failures, tag, abs(es["size"] - box.size) < 0.01,
          "effective_style not restored to original size after undo")
    doc.redo()
    check(failures, tag, doc.edit_count == 1, "redo did not restore style")
    doc.close()
    if not failures:
        print(f"  {tag:20} family+size+color+bold -> saved Georgia, "
              f"red, 34pt, ink, undo/redo OK")


# --------------------------------------------------------------------------
# 3. move + resize geometry
# --------------------------------------------------------------------------
def test_move_resize(failures):
    tag = "move_resize"
    doc = PDFDocument(FORM)
    box = find_span(doc, "Cleared")
    if not check(failures, tag, box is not None, "move target not found"):
        doc.close()
        return
    ox, oy = box.origin
    doc.move_box(0, box, 30.0, -12.0)
    out = save(doc)
    moved = saved_span(out, "Cleared")
    check(failures, tag, moved is not None, "moved text missing")
    if moved:
        mx, my = moved["origin"]
        check(failures, tag, abs(mx - (ox + 30.0)) < 3.0,
              f"moved x {mx:.1f} != {ox + 30.0:.1f}")
        check(failures, tag, abs(my - (oy - 12.0)) < 3.0,
              f"moved y {my:.1f} != {oy - 12.0:.1f}")
    # original location should be cleared (no ink at the old bbox center band)
    old_ink = region_ink(out, box.redact_rects[0])
    check(failures, tag, old_ink < 80,
          f"original location still has ink after move ({old_ink}px)")
    doc.undo()

    # resize: scale=2 should roughly double the saved font size.
    box2 = find_span(doc, "1/8/2026")
    if box2 is None:
        box2 = find_span(doc, "1,250") or find_span(doc, "Cleared")
    start = box2.size
    doc.resize_box(0, box2, 2.0)
    out2 = save(doc)
    rs = saved_span(out2, box2.text.strip().split()[0][:5] if box2.text.strip() else "")
    rs = rs or saved_span(out2, "250") or saved_span(out2, "2026")
    if rs:
        check(failures, tag, abs(rs["size"] - start * 2.0) < 2.0,
              f"resized size {rs['size']:.1f} != ~{start * 2.0:.1f}")
    doc.undo()
    out3 = save(doc)
    rs3 = saved_span(out3, "2026") or saved_span(out3, "250")
    if rs3:
        check(failures, tag, abs(rs3["size"] - start) < 1.0,
              f"resize undo did not restore size ({rs3['size']:.1f} vs {start})")
    doc.close()
    if not failures:
        print(f"  {tag:20} move shifts ink ~(+30,-12), original cleared; "
              f"resize x2 doubles size; undo restores")


# --------------------------------------------------------------------------
# 3b. size <-> scale round-trip + effective_bbox tracks staged geometry
# --------------------------------------------------------------------------
def test_size_scale_roundtrip(failures):
    """Once a box is resized, setting an ABSOLUTE size in the inspector must
    land exactly that size, not size*scale (the regression the review caught:
    resize x2 then set 41pt jumped to 82pt). And effective_bbox must follow the
    staged move/resize/size so the selection overlay tracks the live ink."""
    tag = "size_scale"
    doc = PDFDocument(FORM)
    box = find_span(doc, "Sample Report")
    if not check(failures, tag, box is not None, "target span not found"):
        doc.close()
        return
    start = box.size

    # resize x2 -> effective size doubles.
    doc.resize_box(0, box, 2.0)
    es = doc.effective_style(0, box)
    check(failures, tag, abs(es["size"] - start * 2.0) < 0.01,
          f"after resize x2 effective size {es['size']} != {start * 2.0}")

    # now set an ABSOLUTE 41pt: it must read back as 41, not 82.
    doc.set_style(0, box, size=41.0)
    es2 = doc.effective_style(0, box)
    check(failures, tag, abs(es2["size"] - 41.0) < 0.01,
          f"set_style(41) after resize -> effective size {es2['size']} (want 41)")
    edit = doc._edits[doc._span_edit_key(0, box)]
    check(failures, tag, abs(doc._effective_size(box, edit) - 41.0) < 0.01,
          f"_effective_size {doc._effective_size(box, edit)} != 41 after resize+set")

    # saved file carries 41pt.
    out = save(doc)
    ss = saved_span(out, "Sample")
    if ss:
        check(failures, tag, abs(ss["size"] - 41.0) < 2.0,
              f"saved size {ss['size']:.1f} != 41")

    # a non-positive size is ignored (span branch now guards like the NewBox one).
    doc2 = PDFDocument(FORM)
    b2 = find_span(doc2, "Sample Report")
    doc2.set_style(0, b2, size=-5.0)
    es_neg = doc2.effective_style(0, b2)
    check(failures, tag, es_neg["size"] > 0,
          f"non-positive size leaked into effective_style: {es_neg['size']}")
    doc2.close()

    # effective_bbox tracks move + resize.
    doc3 = PDFDocument(FORM)
    b3 = find_span(doc3, "Cleared")
    base_bbox = b3.bbox
    check(failures, tag, doc3.effective_bbox(0, b3) == base_bbox,
          "effective_bbox of an unedited span != its bbox")
    doc3.move_box(0, b3, 40.0, -25.0)
    eb = doc3.effective_bbox(0, b3)
    check(failures, tag,
          abs(eb[0] - (base_bbox[0] + 40.0)) < 0.01
          and abs(eb[1] - (base_bbox[1] - 25.0)) < 0.01,
          f"effective_bbox did not follow the move: {eb} vs base {base_bbox}")
    doc3.resize_box(0, b3, 2.0)
    eb2 = doc3.effective_bbox(0, b3)
    # height should roughly double (grew from the baseline), and stay shifted.
    h0 = base_bbox[3] - base_bbox[1]
    h2 = eb2[3] - eb2[1]
    check(failures, tag, h2 > h0 * 1.5,
          f"effective_bbox height {h2:.1f} did not grow with resize (was {h0:.1f})")
    doc3.close()

    doc.close()
    if not failures:
        print(f"  {tag:20} resize x2 then set 41pt -> 41 (not 82); non-positive "
              f"size ignored; effective_bbox tracks move+resize")


# --------------------------------------------------------------------------
# 4. delete removes ink, neighbor survives (non-destructive redaction)
# --------------------------------------------------------------------------
def test_delete(failures):
    tag = "delete_box"
    doc = PDFDocument(FORM)
    box = find_span(doc, "$1,250")
    neighbor = find_span(doc, "Amount")
    if not check(failures, tag, box is not None and neighbor is not None,
                 "delete target / neighbor not found"):
        doc.close()
        return
    base_neighbor_ink = region_ink(FORM, neighbor.bbox)
    doc.delete_box(0, box)
    out = save(doc)
    after = region_ink(out, box.redact_rects[0])
    check(failures, tag, after < 60,
          f"deleted region still has ink ({after}px)")
    txt = fitz.open(out); ptext = txt[0].get_text(); txt.close()
    check(failures, tag, "$1,250" not in ptext,
          "deleted text still extractable from saved page")
    neigh_ink = region_ink(out, neighbor.bbox)
    check(failures, tag, abs(neigh_ink - base_neighbor_ink) < base_neighbor_ink * 0.25 + 30,
          f"neighbor ink changed too much ({base_neighbor_ink}->{neigh_ink}) "
          f"-- non-destructive redaction may have failed")
    doc.undo()
    out2 = save(doc)
    txt2 = fitz.open(out2); ptext2 = txt2[0].get_text(); txt2.close()
    check(failures, tag, "$1,250" in ptext2 or region_ink(out2, box.bbox) > 50,
          "delete undo did not restore the box")
    doc.close()
    if not failures:
        print(f"  {tag:20} deleted region ink->{after}, text gone, "
              f"neighbor {base_neighbor_ink}->{neigh_ink} intact; undo restores")


# --------------------------------------------------------------------------
# 5. add_box
# --------------------------------------------------------------------------
def test_add(failures):
    tag = "add_box"
    doc = PDFDocument(FORM)
    origin = (300.0, 500.0)
    nb = doc.add_box(0, origin, "Hello 42", "Helvetica", 14.0,
                     (0.0, 0.0, 0.0), False, False)
    check(failures, tag, doc.edit_count == 1, "edit_count != 1 after add")
    check(failures, tag, nb in doc.new_boxes(0), "new box not surfaced")
    # bbox derived from metrics, non-degenerate, near the origin
    x0, y0, x1, y1 = nb.bbox
    check(failures, tag, x1 > x0 and y1 > y0, f"degenerate bbox {nb.bbox}")
    check(failures, tag, abs(x0 - origin[0]) < 1.0 and y0 < origin[1] < y1,
          f"bbox {nb.bbox} not anchored on baseline origin {origin}")
    out = save(doc)
    check(failures, tag, has_words(out, "Hello", "42"),
          "added text not in saved page")
    check(failures, tag, region_ink(out, nb.bbox) > 30,
          "added box has no ink")
    introduced = new_base14(FORM, out)
    check(failures, tag, not introduced,
          f"added box INTRODUCED a base-14 builtin (not embedded): {introduced}")
    # undo removes it entirely
    doc.undo()
    check(failures, tag, doc.edit_count == 0 and not doc.new_boxes(0),
          "add undo did not remove the box")
    out2 = save(doc)
    check(failures, tag, not has_words(out2, "Hello"),
          "added text still present after undo")
    doc.redo()
    check(failures, tag, len(doc.new_boxes(0)) == 1, "redo did not restore add")
    doc.close()
    if not failures:
        print(f"  {tag:20} 'Hello 42' drawn at origin, embeddable font, "
              f"ink; undo removes, redo restores")


# --------------------------------------------------------------------------
# 6. Mixed undo/redo across all edit kinds on one history
# --------------------------------------------------------------------------
def test_mixed_history(failures):
    tag = "mixed_history"
    doc = PDFDocument(FORM)
    orig_doc = fitz.open(FORM)
    orig_text = orig_doc[0].get_text()
    orig_doc.close()

    s_text = find_span(doc, "Pending")
    s_style = find_span(doc, "Field A")
    s_move = find_span(doc, "Date")
    s_del = find_span(doc, "10/11/1995")
    for nm, s in (("text", s_text), ("style", s_style), ("move", s_move),
                  ("del", s_del)):
        if not check(failures, tag, s is not None, f"{nm} target missing"):
            doc.close()
            return

    doc.stage_edit(0, s_text, "Approved now")              # text
    doc.set_style(0, s_style, color=(0, 0, 1), bold=True)  # style
    doc.move_box(0, s_move, 10.0, 5.0)                      # move
    doc.delete_box(0, s_del)                                # delete
    doc.add_box(0, (320.0, 520.0), "Extra 99", "Times New Roman", 12.0,
                (0, 0, 0), False, True)                     # add
    check(failures, tag, doc.edit_count == 5,
          f"edit_count {doc.edit_count} != 5 after 5 mixed mutations")

    # final save renders without exception
    final = save(doc)
    fdoc = fitz.open(final); ftext = fdoc[0].get_text(); fdoc.close()
    check(failures, tag, has_words(final, "Approved", "now"),
          "final text edit missing")
    check(failures, tag, has_words(final, "Extra", "99"), "final add missing")
    check(failures, tag, "10/11/1995" not in ftext, "final delete not applied")

    # 5x undo -> pristine
    for _ in range(5):
        doc.undo()
    check(failures, tag, doc.edit_count == 0,
          f"edit_count {doc.edit_count} != 0 after 5 undo")
    check(failures, tag, not doc.new_boxes(0), "new box survived full undo")
    out_pristine = save(doc)
    pdoc = fitz.open(out_pristine); ptext = pdoc[0].get_text(); pdoc.close()
    check(failures, tag, ptext.split() == orig_text.split(),
          "5x undo did not restore the original text")

    # 5x redo -> final state again
    for _ in range(5):
        doc.redo()
    check(failures, tag, doc.edit_count == 5,
          f"edit_count {doc.edit_count} != 5 after 5 redo")
    out_final2 = save(doc)
    f2 = fitz.open(out_final2); t2 = f2[0].get_text(); f2.close()
    check(failures, tag, has_words(out_final2, "Approved", "Extra", "99")
          and "10/11/1995" not in t2,
          "5x redo did not reproduce the final state")
    doc.close()
    if not failures:
        print(f"  {tag:20} text+style+move+delete+add, 5x undo->pristine, "
              f"5x redo->final (one stack)")


# --------------------------------------------------------------------------
# 7. sweep the other fixtures: a restyle + add saves cleanly on each
# --------------------------------------------------------------------------
def test_fixture_sweep(failures):
    tag = "fixture_sweep"
    fixtures = ["body_paragraphs", "multi_size", "bold_italic", "colored_text",
                "subset_font", "mixed_families"]
    for fx in fixtures:
        path = os.path.join(FIXTURE_DIR, f"{fx}.pdf")
        if not os.path.exists(path):
            continue
        doc = PDFDocument(path)
        spans = doc.spans(0)
        if not spans:
            doc.close()
            continue
        target = max(spans, key=lambda s: len(s.text.strip()))
        doc.set_style(0, target, font_family="Verdana", size=target.size + 4,
                      color=(0.1, 0.4, 0.1))
        doc.add_box(0, (100.0, 600.0), "Added 7", "Courier New", 11.0,
                    (0, 0, 0), False, False)
        try:
            out = save(doc)
        except Exception as exc:
            failures.append(f"{tag}[{fx}]: save raised {exc}")
            doc.close()
            continue
        introduced = new_base14(path, out)
        check(failures, f"{tag}[{fx}]", not introduced,
              f"styled save INTRODUCED base-14: {introduced}")
        check(failures, f"{tag}[{fx}]", has_words(out, "Added", "7"),
              "added box text missing")
        doc.close()
    if not failures:
        print(f"  {tag:20} restyle+add saves cleanly on all "
              f"{len(fixtures)} legacy fixtures")


def test_wysiwyg_all_kinds(failures):
    """The baked-WYSIWYG invariant (§0.1) for EVERY edit kind: the on-screen
    render_with_edits page is the same pixels as the saved file."""
    tag = "wysiwyg"
    worst = 0.0
    for kind in ("style", "move", "resize", "delete", "add"):
        doc = PDFDocument(FORM)
        box = find_span(doc, "Sample Report")
        if kind == "style":
            doc.set_style(0, box, font_family="Georgia", size=34.0,
                          color=(0.85, 0.1, 0.1), bold=True)
        elif kind == "move":
            doc.move_box(0, box, 25.0, 40.0)
        elif kind == "resize":
            doc.resize_box(0, box, 1.5)
        elif kind == "delete":
            doc.delete_box(0, box)
        elif kind == "add":
            doc.add_box(0, (300.0, 500.0), "Hello 42", "Helvetica", 14.0,
                        (0, 0, 0), False, False)
        out = save(doc)
        d = wysiwyg_match(doc, out)
        worst = max(worst, d)
        check(failures, tag, d < 0.02,
              f"{kind}: render_with_edits != saved (rel diff {d:.4f})")
        doc.close()
    if not failures:
        print(f"  {tag:20} style/move/resize/delete/add: on-screen == saved "
              f"(worst rel ink diff {worst:.4f})")


def test_recenter_rotated(failures):
    """The centered-line recenter must fire on /Rotate 90 pages too
    (final-review blocker regression): span.bbox lives in unrotated text
    space, so the centered test must compare against the UNROTATED page
    width. A swapped centered name on a rotated landscape page saves with
    its ink centroid on the band center, same as the unrotated control."""
    tag = "recenter_rotated"

    def build(rotate):
        out = fitz.open()
        page = out.new_page(width=792, height=612)
        name = "Jordan Carter"
        fs = 30.0
        tw = fitz.get_text_length(name, fontname="hebo", fontsize=fs)
        page.insert_text(((792 - tw) / 2.0, 280), name, fontname="hebo",
                         fontsize=fs)
        if rotate:
            page.set_rotation(90)
        path = tempfile.mktemp(suffix=".pdf")
        out.save(path)
        out.close()
        return path

    def centroid_offset(path):
        """Signed px offset of the dark-ink centroid from the name band's
        center, along the band's long DISPLAY axis."""
        d = fitz.open(path)
        p = d[0]
        band = fitz.Rect(60, 240, 732, 300) * p.rotation_matrix
        pix = p.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
        w, n, s = pix.width, pix.n, pix.samples
        x0, x1 = int(band.x0 * 2), int(band.x1 * 2)
        y0, y1 = int(band.y0 * 2), int(band.y1 * 2)
        long_is_y = (y1 - y0) > (x1 - x0)
        tot, acc = 0, 0.0
        for y in range(y0, y1 + 1):
            base = y * w * n
            for x in range(x0, x1 + 1):
                i = base + x * n
                if (0.299 * s[i] + 0.587 * s[i + 1] + 0.114 * s[i + 2]) < 120:
                    tot += 1
                    acc += (y if long_is_y else x)
        d.close()
        if not tot:
            return None
        mid = ((y0 + y1) / 2.0) if long_is_y else ((x0 + x1) / 2.0)
        return acc / tot - mid

    for rotate in (False, True):
        for newname in ("Ana Li", "Alexandria Featherstone"):
            src = build(rotate)
            doc = PDFDocument(src)
            span = find_span(doc, "Jordan")
            if not check(failures, tag, span is not None, "name span missing"):
                doc.close()
                continue
            doc.stage_edit(0, span, newname)
            out = save(doc)
            off = centroid_offset(out)
            doc.close()
            check(failures, tag, off is not None and abs(off) <= 8.0,
                  f"rot={rotate} '{newname}' saved off-center by "
                  f"{off if off is None else round(off, 1)}px")
    if not failures:
        print(f"  {tag:20} centered-name swap recenters on rotated AND "
              f"unrotated pages (<=8px of band center)")


def main():
    print("Editor MODEL + FONT verification (EDITOR_SPEC §8)\n")
    failures = []
    for fn in (test_resolve_family, test_style, test_move_resize,
               test_size_scale_roundtrip, test_delete,
               test_add, test_mixed_history, test_wysiwyg_all_kinds,
               test_recenter_rotated,
               test_fixture_sweep):
        fn(failures)
    print("\n" + "=" * 66)
    if failures:
        print(f"FAILED ({len(failures)}):")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("PASSED -- resolve_family never Tier1/always embeddable; style/move/"
          "resize/delete/add all save + render correctly; non-destructive "
          "redaction held; mixed undo/redo round-trips on one history.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
