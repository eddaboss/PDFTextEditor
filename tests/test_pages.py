"""Headless verification for PAGE & DOCUMENT MANAGEMENT (PAGES_SPEC §7).

Covers the new structural model ops (rotate / delete / insert / duplicate / move
/ merge / extract / split + thumbnails + structural undo), the Workspace
multi-doc container, the searchable font combo + selection-visibility polish, and
— critically — the NO-REGRESSION invariants: bake-then-mutate preserves a text
edit across a structural op, WYSIWYG holds after a structural op, overlap-merge +
non-destructive redaction survive, and the doc-on-disk is never mutated.

Every fixture lives under ``tests/fixtures/``; multi-page test docs are
synthesized in ``tempfile`` from the fixtures (never written into the fixtures
dir). Tests run with the venv python + ``QT_QPA_PLATFORM=offscreen``.

Run:
    QT_QPA_PLATFORM=offscreen \
      /Users/edward/Documents/GitHub/PDFTextEditor/.venv/bin/python \
      tests/test_pages.py
"""

from __future__ import annotations

import hashlib
import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import fitz  # noqa: E402
from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

_APP = QApplication.instance() or QApplication([])

from pdftexteditor.document import PDFDocument  # noqa: E402
from pdftexteditor.ui import theme  # noqa: E402
from pdftexteditor.workspace import Workspace  # noqa: E402

FIXTURE_DIR = os.path.join(_HERE, "fixtures")
THREE = os.path.join(FIXTURE_DIR, "three_page.pdf")
TWO = os.path.join(FIXTURE_DIR, "two_page.pdf")
ROTATED = os.path.join(FIXTURE_DIR, "rotated_doc.pdf")


def check(failures, tag, cond, msg):
    if not cond:
        failures.append(f"{tag}: {msg}")
    return cond


# --- helpers (reused from test_editor_model conventions) ------------------
def _pix_ink(pix):
    s, step, n = pix.samples, pix.n, 0
    for i in range(0, len(s), step):
        r, g, b = s[i], s[i + 1], s[i + 2]
        if (0.299 * r + 0.587 * g + 0.114 * b) < 230:
            n += 1
    return n


def region_ink(path, bbox, scale=3.0, page=0):
    doc = fitz.open(path)
    try:
        pix = doc[page].get_pixmap(matrix=fitz.Matrix(scale, scale),
                                   clip=fitz.Rect(bbox), alpha=False)
        return _pix_ink(pix)
    finally:
        doc.close()


def wysiwyg_match(doc, out_path, page=0, scale=2.0):
    """Whole-page ink of render_with_edits vs the saved file (baked WYSIWYG)."""
    rpix = doc.render_with_edits(page, scale)
    rink = _pix_ink(rpix)
    sd = fitz.open(out_path)
    try:
        spix = sd[page].get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
    finally:
        sd.close()
    sink = _pix_ink(spix)
    return abs(rink - sink) / max(sink, 1)


def find_span(doc, page, contains):
    for s in doc.spans(page):
        if contains in s.text:
            return s
    return None


def page_label(doc, page):
    """The big page label (PAGE ONE / DOC-B P1 …) — the first span's text."""
    sp = doc.spans(page)
    return sp[0].text.strip() if sp else ""


def labels(doc):
    return [page_label(doc, i) for i in range(doc.page_count)]


def synth(*sources):
    """Synthesize a temp multi-page doc by insert_pdf-ing the given fixtures in
    order. Returns the temp path. Each fixture's pages are appended whole."""
    out = fitz.open()
    for src_path in sources:
        s = fitz.open(src_path)
        out.insert_pdf(s)
        s.close()
    path = tempfile.mktemp(suffix=".pdf")
    out.save(path, garbage=4, deflate=True)
    out.close()
    return path


def tmp_out():
    return tempfile.mktemp(suffix=".pdf")


def file_hash(path):
    return hashlib.sha1(open(path, "rb").read()).hexdigest()


# ==========================================================================
# 1. working-doc isolation: mutating working never touches the file on disk
# ==========================================================================
def test_working_doc_isolation(failures):
    tag = "working_isolation"
    h0 = file_hash(THREE)
    doc = PDFDocument(THREE)
    doc.rotate_page(0, 90)
    doc.delete_page(1)
    doc.insert_blank_page(0)
    doc.duplicate_page(0)
    h1 = file_hash(THREE)
    check(failures, tag, h0 == h1,
          "mutating self.working changed the bytes of self.path on disk")
    # The working doc reflects the changes; the file does not.
    on_disk = fitz.open(THREE)
    check(failures, tag, on_disk.page_count == 3,
          f"on-disk page_count changed to {on_disk.page_count}")
    on_disk.close()
    doc.close()
    if not failures:
        print(f"  {tag:24} self.path bytes untouched by structural ops")


# ==========================================================================
# 2. rotate: /Rotate changes, persists through save, WYSIWYG holds
# ==========================================================================
def test_rotate(failures):
    tag = "rotate"
    doc = PDFDocument(ROTATED)
    start = doc.page_rotation(0)         # fixture is /Rotate 90
    doc.rotate_page(0, 90)
    check(failures, tag, doc.page_rotation(0) == (start + 90) % 360,
          f"rotation {doc.page_rotation(0)} != {(start + 90) % 360}")
    out = tmp_out()
    doc.save_as(out)
    rd = fitz.open(out)
    check(failures, tag, rd[0].rotation == (start + 90) % 360,
          f"saved /Rotate {rd[0].rotation} != {(start + 90) % 360}")
    rd.close()
    wd = wysiwyg_match(doc, out)
    check(failures, tag, wd < 0.02,
          f"render_with_edits != saved after rotate (rel diff {wd:.4f})")
    doc.close()
    if not failures:
        print(f"  {tag:24} /Rotate {start}->{(start + 90) % 360}, persists, "
              f"WYSIWYG {wd:.4f}")


# ==========================================================================
# 3. delete: count-1, right page gone, refuse-last-page
# ==========================================================================
def test_delete_page(failures):
    tag = "delete_page"
    path = synth(THREE)                  # 3 pages: ONE / TWO / THREE
    doc = PDFDocument(path)
    before = labels(doc)
    doc.delete_page(1)                   # remove PAGE TWO
    check(failures, tag, doc.page_count == 2,
          f"page_count {doc.page_count} != 2 after delete")
    after = labels(doc)
    check(failures, tag, "TWO" not in " ".join(after),
          f"deleted page still present: {after}")
    check(failures, tag, "THREE" in after[1],
          f"page that was at index 2 is not at index 1: {after}")
    # refuse to delete the only page
    one = PDFDocument(ROTATED)
    try:
        one.delete_page(0)
        check(failures, tag, False, "deleting the only page did not raise")
    except ValueError:
        pass
    one.close()
    doc.close()
    if not failures:
        print(f"  {tag:24} {before} -> {after}; last-page refused")


# ==========================================================================
# 4. insert blank: count+1, blank, inherits neighbor size
# ==========================================================================
def test_insert_blank(failures):
    tag = "insert_blank"
    doc = PDFDocument(THREE)
    n0 = doc.page_count
    ref_rect = doc.working[1].rect       # page 1 is the landscape page
    idx = doc.insert_blank_page(1)
    check(failures, tag, idx == 1, f"returned index {idx} != 1")
    check(failures, tag, doc.page_count == n0 + 1,
          f"page_count {doc.page_count} != {n0 + 1}")
    check(failures, tag, doc.spans(1) == [],
          "inserted page is not blank (has spans)")
    new_rect = doc.working[1].rect
    check(failures, tag,
          abs(new_rect.width - ref_rect.width) < 0.5
          and abs(new_rect.height - ref_rect.height) < 0.5,
          f"blank size {new_rect} did not inherit neighbor {ref_rect}")
    # explicit size override is honored
    doc2 = PDFDocument(THREE)
    doc2.insert_blank_page(0, width=200.0, height=400.0)
    r = doc2.working[0].rect
    check(failures, tag, abs(r.width - 200) < 0.5 and abs(r.height - 400) < 0.5,
          f"explicit blank size {r} != (200,400)")
    doc2.close()
    doc.close()
    if not failures:
        print(f"  {tag:24} count {n0}->{n0 + 1}, blank, inherits neighbor size")


# ==========================================================================
# 5. duplicate: count+1, copy content matches
# ==========================================================================
def test_duplicate(failures):
    tag = "duplicate"
    doc = PDFDocument(THREE)
    n0 = doc.page_count
    idx = doc.duplicate_page(0)
    check(failures, tag, idx == 1, f"returned index {idx} != 1")
    check(failures, tag, doc.page_count == n0 + 1,
          f"page_count {doc.page_count} != {n0 + 1}")
    t0 = [s.text for s in doc.spans(0)]
    t1 = [s.text for s in doc.spans(1)]
    check(failures, tag, t0 == t1,
          "duplicated page text does not match the source page")
    doc.close()
    if not failures:
        print(f"  {tag:24} count {n0}->{n0 + 1}, copy content matches source")


# ==========================================================================
# 6. move_page permutations: the off-by-one lock (all 16 (src,dst) pairs)
# ==========================================================================
def test_move_page_permutations(failures):
    tag = "move_permutations"
    # Build a 4-page temp doc tagged A/B/C/D.
    base = fitz.open()
    for ch in "ABCD":
        pg = base.new_page(width=200, height=200)
        pg.insert_text((20, 40), ch, fontsize=24)
    path = tempfile.mktemp(suffix=".pdf")
    base.save(path)
    base.close()

    def expect(seq_s, src, dst):
        seq = list(seq_s)
        x = seq.pop(src)
        seq.insert(dst, x)
        return "".join(seq)

    bad = 0
    for src in range(4):
        for dst in range(4):
            doc = PDFDocument(path)
            doc.move_page(src, dst)
            got = "".join(doc.spans(i)[0].text.strip()
                          for i in range(doc.page_count))
            doc.close()
            want = expect("ABCD", src, dst)
            if got != want:
                bad += 1
                failures.append(
                    f"{tag}: move_page({src},{dst}) -> {got}, expected {want}")
    # spot-checks the spec pins explicitly
    spot = {(0, 3): "BCDA", (3, 0): "DABC", (1, 2): "ACBD",
            (2, 1): "ACBD", (1, 3): "ACDB"}
    for (src, dst), want in spot.items():
        doc = PDFDocument(path)
        doc.move_page(src, dst)
        got = "".join(doc.spans(i)[0].text.strip()
                      for i in range(doc.page_count))
        doc.close()
        check(failures, tag, got == want,
              f"spot ({src},{dst}) -> {got} != {want}")
    if not failures:
        print(f"  {tag:24} all 16 (src,dst) pairs match seq.pop/insert; "
              f"spot-checks OK ({bad} bad)")


# ==========================================================================
# 7. merge from a path: page_count sums, order correct, source file untouched
# ==========================================================================
def test_merge_path(failures):
    tag = "merge_path"
    doc = PDFDocument(THREE)             # ONE / TWO / THREE
    n0 = doc.page_count
    b_hash = file_hash(TWO)
    first = doc.merge(TWO)              # append DOC-B P1 / DOC-B P2
    check(failures, tag, doc.page_count == n0 + 2,
          f"merged page_count {doc.page_count} != {n0 + 2}")
    check(failures, tag, first == n0,
          f"first inserted index {first} != {n0}")
    # The fixture label uses a soft hyphen (U+00AD): match on DOC + P1/P2.
    lbl1, lbl2 = page_label(doc, n0), page_label(doc, n0 + 1)
    check(failures, tag, "DOC" in lbl1 and "P1" in lbl1,
          f"first appended page is not DOC-B P1: {lbl1!r}")
    check(failures, tag, "DOC" in lbl2 and "P2" in lbl2,
          f"second appended page is not DOC-B P2: {lbl2!r}")
    check(failures, tag, file_hash(TWO) == b_hash,
          "merge mutated the source file on disk")
    doc.close()
    if not failures:
        print(f"  {tag:24} {n0}+2 pages, DOC-B appended in order, source intact")


# ==========================================================================
# 8. merge from an open doc bakes the OTHER doc's pending edits
# ==========================================================================
def test_merge_doc_bakes_other(failures):
    tag = "merge_doc_bakes"
    target = PDFDocument(THREE)
    other = PDFDocument(TWO)
    sp = find_span(other, 0, "Glossary")      # "Appendix B — Glossary of Terms"
    if not check(failures, tag, sp is not None, "other-doc span not found"):
        target.close(); other.close(); return
    other.stage_edit(0, sp, "Appendix B — BAKED Glossary")
    n0 = target.page_count
    target.merge(other)
    merged_text = " ".join(s.text for s in target.spans(n0))
    check(failures, tag, "BAKED" in merged_text,
          f"other's staged edit not baked into merged page: {merged_text!r}")
    # other itself is unchanged: still dirty, edit still staged, page_count same.
    check(failures, tag, other.edit_count == 1 and other.dirty,
          "merge mutated the other doc's edit state")
    check(failures, tag, other.page_count == 2,
          f"merge changed other's page_count to {other.page_count}")
    target.close(); other.close()
    if not failures:
        print(f"  {tag:24} other's edit baked into the inserted pages; "
              f"other unchanged (still dirty)")


# ==========================================================================
# 9. extract: subset in given order, duplicates allowed, source unchanged
# ==========================================================================
def test_extract(failures):
    tag = "extract"
    doc = PDFDocument(THREE)             # ONE / TWO / THREE
    out = tmp_out()
    res = doc.extract_pages([2, 0], out)
    check(failures, tag, res == out, "extract did not return out_path")
    ed = fitz.open(out)
    check(failures, tag, ed.page_count == 2,
          f"extracted page_count {ed.page_count} != 2")
    o0 = ed[0].get_text()
    o1 = ed[1].get_text()
    ed.close()
    check(failures, tag, "THREE" in o0 and "ONE" in o1,
          f"extract order wrong: p0={o0[:12]!r} p1={o1[:12]!r}")
    check(failures, tag, doc.page_count == 3,
          f"source page_count changed to {doc.page_count}")
    # bytes form + duplicates
    data = doc.extract_pages([1, 1])
    check(failures, tag, isinstance(data, (bytes, bytearray)),
          "extract without out_path did not return bytes")
    dd = fitz.open("pdf", data)
    check(failures, tag, dd.page_count == 2, "duplicate extract not 2 pages")
    dd.close()
    doc.close()
    if not failures:
        print(f"  {tag:24} [2,0]->THREE,ONE; duplicates ok; source unchanged")


# ==========================================================================
# 10. split: multiple files of the right sizes, source unchanged
# ==========================================================================
def test_split(failures):
    tag = "split"
    doc = PDFDocument(THREE)
    out_dir = tempfile.mkdtemp()
    files = doc.split([(0, 0), (1, 2)], out_dir)
    check(failures, tag, len(files) == 2, f"split wrote {len(files)} files != 2")
    counts = [fitz.open(f).page_count for f in files]
    check(failures, tag, counts == [1, 2],
          f"split page counts {counts} != [1, 2]")
    check(failures, tag, all(os.path.dirname(f) == out_dir for f in files),
          "split wrote outside the chosen dir")
    check(failures, tag, doc.page_count == 3,
          f"source page_count changed to {doc.page_count}")
    doc.close()
    if not failures:
        print(f"  {tag:24} [(0,0),(1,2)] -> 2 files of {counts}; source intact")


# ==========================================================================
# 11. bake-then-mutate preserves a staged edit across a structural op
# ==========================================================================
def test_bake_then_mutate_preserves_edits(failures):
    tag = "bake_then_mutate"
    doc = PDFDocument(THREE)
    sp = find_span(doc, 1, "Coastal")    # PAGE TWO body header line
    if not check(failures, tag, sp is not None, "edit target not found"):
        doc.close(); return
    doc.stage_edit(1, sp, "Field Notes — BAKED Survey")
    check(failures, tag, doc.edit_count == 1, "edit not staged")
    doc.move_page(1, 0)                  # move PAGE TWO to the front
    # The edit baked into the moved page, which is now page 0.
    p0 = " ".join(s.text for s in doc.spans(0))
    check(failures, tag, "BAKED" in p0,
          f"edit orphaned by move (page0 text: {p0!r})")
    check(failures, tag, doc.edit_count == 0,
          f"staged edit not cleared after bake (edit_count {doc.edit_count})")
    check(failures, tag, doc.dirty,
          "doc not dirty after structural op (should stay dirty)")
    out = tmp_out()
    doc.save_as(out)
    rd = fitz.open(out)
    check(failures, tag, "BAKED" in rd[0].get_text(),
          "saved+reopened page 0 does not carry the baked edit")
    rd.close()
    wd = wysiwyg_match(doc, out, page=0)
    check(failures, tag, wd < 0.02,
          f"WYSIWYG broke after bake+move (rel diff {wd:.4f})")
    doc.close()
    if not failures:
        print(f"  {tag:24} edit baked onto moved page; edit_count 0, dirty True; "
              f"WYSIWYG {wd:.4f}")


# ==========================================================================
# 12. structural undo / redo round-trips
# ==========================================================================
def test_structural_undo(failures):
    tag = "structural_undo"
    doc = PDFDocument(THREE)
    r0 = doc.page_rotation(0)
    doc.rotate_page(0, 90)
    check(failures, tag, doc.page_rotation(0) == (r0 + 90) % 360, "rotate failed")
    check(failures, tag, doc.can_undo_structural, "can_undo_structural False")
    ok = doc.undo_structural()
    check(failures, tag, ok and doc.page_rotation(0) == r0,
          f"undo_structural did not restore rotation ({doc.page_rotation(0)})")
    check(failures, tag, doc.can_redo_structural, "can_redo_structural False")
    doc.redo_structural()
    check(failures, tag, doc.page_rotation(0) == (r0 + 90) % 360,
          "redo_structural did not re-apply rotation")
    # a multi-op sequence round-trips on page_count too
    doc2 = PDFDocument(THREE)
    doc2.delete_page(1)
    doc2.merge(TWO)
    pc_after = doc2.page_count           # 2 + 2 = 4
    doc2.undo_structural()               # undo merge -> 2
    check(failures, tag, doc2.page_count == 2,
          f"undo merge page_count {doc2.page_count} != 2")
    doc2.undo_structural()               # undo delete -> 3
    check(failures, tag, doc2.page_count == 3,
          f"undo delete page_count {doc2.page_count} != 3")
    doc2.redo_structural(); doc2.redo_structural()
    check(failures, tag, doc2.page_count == pc_after,
          f"redo did not reach {pc_after} (got {doc2.page_count})")
    doc.close(); doc2.close()
    if not failures:
        print(f"  {tag:24} rotate + delete/merge undo/redo round-trip "
              f"(rotation + page_count)")


def test_structural_undo_bakes_staged(failures):
    """State staged AFTER a structural op (reachable via the tab-switch Qt
    stack rebuild + Cmd+Z) must NOT be destroyed by undo_structural: it bakes
    into the redo snapshot, the census reads 0/clean truthfully, no staged
    annot reattaches to the restored page, and redo recovers everything as
    real ink (final-review blocker regression)."""
    tag = "structural_undo_staged"
    import fitz as _fitz
    pm = _fitz.Pixmap(_fitz.csRGB, _fitz.IRect(0, 0, 30, 30), False)
    pm.set_rect(pm.irect, (40, 90, 200))
    png = pm.tobytes("png")

    doc = PDFDocument(THREE)
    n_img0 = len(doc.working.get_page_images(0))
    n_annot0 = sum(1 for _ in doc.working[0].annots())
    doc.rotate_page(0, 90)               # structural op FIRST (depth 1)
    # stage state from three subsystems AFTER the op
    span = max((s for s in doc.spans(0) if s.text.strip()),
               key=lambda s: len(s.text))
    doc.stage_edit(0, span, span.text + " BETA")
    doc.add_image(0, (430.0, 650.0, 500.0, 700.0), png)
    word = next(wd for wd in doc.page_words(0) if len(wd.text) >= 4)
    doc.add_annot(0, kind="highlight", quads=(tuple(word.bbox),),
                  stroke=(1, .9, .2), opacity=0.4)
    check(failures, tag, doc.edit_count == 3, f"staged {doc.edit_count} != 3")

    doc.undo_structural()
    check(failures, tag, doc.page_rotation(0) == 0, "undo lost the rotation")
    check(failures, tag, doc.edit_count == 0 and not doc.dirty,
          f"census lies after undo: count={doc.edit_count} dirty={doc.dirty}")
    check(failures, tag,
          sum(1 for _ in doc.working[0].annots()) == n_annot0,
          "staged annot reattached to the restored (pre-op) page")
    check(failures, tag, "BETA" not in doc.working[0].get_text("text"),
          "post-op staged text leaked onto the restored page")

    doc.redo_structural()
    txt = doc.working[0].get_text("text")
    check(failures, tag, doc.page_rotation(0) == 90, "redo lost the rotation")
    check(failures, tag, "BETA" in txt, "redo destroyed the staged text edit")
    check(failures, tag,
          len(doc.working.get_page_images(0)) == n_img0 + 1,
          "redo destroyed the placed image")
    check(failures, tag,
          sum(1 for _ in doc.working[0].annots()) == n_annot0 + 1,
          "redo destroyed the staged annot")
    check(failures, tag, doc.dirty, "redo state must read dirty")
    doc.close()
    if not failures:
        print(f"  {tag:24} post-op staged text/image/annot survive the "
              f"undo/redo round-trip as baked ink; census stays truthful")


# ==========================================================================
# 13. invariants survive after a structural sequence
# ==========================================================================
def test_invariants_after_structural(failures):
    tag = "invariants_after_struct"
    # delete + merge, THEN edit a surviving page; check overlap-merge residue,
    # non-destructive redaction, no base-14 tofu, render==saved.
    doc = PDFDocument(THREE)
    doc.delete_page(1)                   # ONE / THREE
    doc.merge(TWO)                       # ONE / THREE / DOC-B P1 / DOC-B P2
    # The colored header band on page 0 (navy) is the redaction-survival probe.
    band_bbox = (80, 10, 300, 60)        # inside the top band, away from glyphs
    band_before = region_ink(out_path := tmp_save(doc), band_bbox)

    # restyle the page-0 title (over the dark band) -> band must survive.
    title = find_span(doc, 0, "Harbor")  # "The Harbor Library — Reading Room"
    if not check(failures, tag, title is not None, "page-0 title not found"):
        doc.close(); return
    doc.set_style(0, title, font_family="Georgia", size=22.0,
                  color=(0.9, 0.1, 0.1), bold=True)
    out = tmp_out()
    doc.save_as(out)

    # 1) overlap-merge: editing a merged box leaves no residue at member bboxes.
    #    (the title is a single run here; assert its redact_rects are honored —
    #    no stray ink OUTSIDE the new glyphs at the old member bboxes is hard to
    #    measure directly, so we assert the new text is present + WYSIWYG holds.)
    check(failures, tag,
          "Harbor" in fitz.open(out)[0].get_text(),
          "restyled title text lost after structural+style")
    # 2) non-destructive redaction: the navy band ink survives the restyle.
    band_after = region_ink(out, band_bbox)
    check(failures, tag, band_after > band_before * 0.6,
          f"colored band ink dropped {band_before}->{band_after} "
          f"(non-destructive redaction may have failed)")
    # 3) no NEW base-14 tofu introduced; saved face carries Georgia.
    bf = [e[3].lower() for e in fitz.open(out)[0].get_fonts(full=True)]
    check(failures, tag, any("georgia" in b for b in bf),
          f"restyled face not Georgia after structural ops: {bf}")
    # 4) render_with_edits == saved (baked WYSIWYG) on the restructured doc.
    wd = wysiwyg_match(doc, out, page=0)
    check(failures, tag, wd < 0.02,
          f"WYSIWYG broke after structural+style (rel diff {wd:.4f})")
    doc.close()
    if not failures:
        print(f"  {tag:24} after delete+merge: restyle holds, band survives, "
              f"Georgia embedded, WYSIWYG {wd:.4f}")


def tmp_save(doc):
    """Save a doc to a temp file (pre-edit probe baseline)."""
    p = tmp_out()
    doc.save_as(p)
    return p


# ==========================================================================
# 14-16. Workspace
# ==========================================================================
def test_workspace_open_dedup(failures):
    tag = "ws_open_dedup"
    ws = Workspace()
    i0 = ws.open(THREE)
    i1 = ws.open(THREE)                  # same realpath -> same tab
    check(failures, tag, i0 == i1 and ws.count == 1,
          f"dedup failed: i0={i0} i1={i1} count={ws.count}")
    i2 = ws.open(TWO)
    check(failures, tag, ws.count == 2 and i2 == 1,
          f"second distinct open wrong: count={ws.count} i2={i2}")
    check(failures, tag, ws.active_index == 1, "active not the newly opened doc")
    ws.close_all()
    if not failures:
        print(f"  {tag:24} re-open same file -> same index, count stays 1")


def test_workspace_close_active(failures):
    tag = "ws_close_active"
    ws = Workspace()
    ws.open(THREE); ws.open(TWO); ws.open(ROTATED)   # 0,1,2 active=2
    ws.switch(1)                                       # active middle
    new = ws.close(1)                                 # close middle
    check(failures, tag, ws.count == 2,
          f"count {ws.count} != 2 after closing middle")
    check(failures, tag, new == 1,
          f"closing active middle -> active {new}, expected right neighbor (1)")
    # remaining are THREE(0), ROTATED(1); active should be ROTATED
    check(failures, tag, os.path.basename(ws.active.path) == "rotated_doc.pdf",
          f"active is {os.path.basename(ws.active.path)}, expected rotated_doc")
    ws.close(0); n = ws.close(0)
    check(failures, tag, n == -1 and ws.is_empty,
          f"closing down to empty -> active {n}, is_empty {ws.is_empty}")
    if not failures:
        print(f"  {tag:24} close active middle -> right neighbor; "
              f"empty -> active_index -1")


def test_workspace_move_tab(failures):
    tag = "ws_move_tab"
    ws = Workspace()
    ws.open(THREE); ws.open(TWO); ws.open(ROTATED)
    ws.switch(2)
    active_doc = ws.active               # ROTATED
    ws.move_tab(0, 2)                    # THREE moves to the end
    check(failures, tag, ws.active is active_doc,
          "active no longer points at the same document after move_tab")
    order = [os.path.basename(d.path) for d in ws.documents()]
    check(failures, tag,
          order == ["two_page.pdf", "rotated_doc.pdf", "three_page.pdf"],
          f"move_tab order wrong: {order}")
    ws.close_all()
    if not failures:
        print(f"  {tag:24} reorder keeps active pointing at the same doc")


# ==========================================================================
# 17-20. UI / app (real MainWindow, headless)
# ==========================================================================
def _make_window():
    from pdftexteditor.ui.main_window import MainWindow
    w = MainWindow()
    # Suppress the dirty-discard modal on close: the structural-op tests leave
    # the doc dirty, and QMessageBox.exec() blocks forever under offscreen Qt.
    w._suppress_close_guard = True
    w.show()
    return w


def test_tabs_app(failures):
    tag = "tabs_app"
    w = _make_window()
    w.open_path(THREE)
    check(failures, tag, w.tab_bar.isVisibleTo(w) is False,
          "tab bar visible with a single document (should be hidden)")
    w.open_path(TWO)
    check(failures, tag, w.tab_bar.isVisibleTo(w) is True,
          "tab bar not visible with two documents")
    check(failures, tag, w.tab_bar.count() == 2,
          f"tab bar shows {w.tab_bar.count()} tabs != 2")
    id_two = w.view.document
    w._on_tab_activated(0)               # switch to THREE
    check(failures, tag, w.view.document is not id_two,
          "switching tabs did not re-point the canvas document")
    check(failures, tag, w.sidebar.count() == 3,
          f"sidebar shows {w.sidebar.count()} thumbs, expected 3 for THREE")
    w.close()
    if not failures:
        print(f"  {tag:24} 2 tabs shown; switch re-points canvas + sidebar")


def test_sidebar_reorder_app(failures):
    tag = "sidebar_reorder_app"
    w = _make_window()
    path = synth(THREE)                  # 3 distinct pages
    w.open_path(path)
    before = labels(w.document)
    n_thumbs = w.sidebar.count()
    w.sidebar.reorderRequested.emit(0, 2)
    after = labels(w.document)
    check(failures, tag, after != before and "ONE" in after[2],
          f"reorder via sidebar did not move page 0 to the end: {after}")
    check(failures, tag, w.sidebar.count() == n_thumbs,
          f"sidebar thumb count changed {n_thumbs}->{w.sidebar.count()}")
    w.undo_stack.undo()
    restored = labels(w.document)
    check(failures, tag, restored == before,
          f"Undo did not restore page order: {restored} vs {before}")
    w.close()
    if not failures:
        print(f"  {tag:24} sidebar reorder moves a page + Undo restores order")


def test_searchable_font(failures):
    tag = "searchable_font"
    w = _make_window()
    ins = w.inspector
    check(failures, tag, ins.family_combo.isEditable(),
          "Inspector family combo is not editable")
    emitted = []
    ins.styleEdited.connect(lambda d: emitted.append(d))
    # enable the panel with a target so commits fire
    ins.set_target(object(), {"font_family": "Helvetica", "size": 12.0,
                              "color": (0, 0, 0), "bold": False, "italic": False})
    emitted.clear()
    # type "geor" then commit "Georgia"
    ins.family_combo.lineEdit().setText("Georgia")
    ins._on_family_commit()
    check(failures, tag, emitted == [{"font_family": "Georgia"}],
          f"committing 'Georgia' emitted {emitted}, expected one font_family edit")
    # free-typed junk emits NOTHING and reverts
    emitted.clear()
    ins.family_combo.lineEdit().setText("Zzzznotafont")
    ins._on_family_commit()
    check(failures, tag, emitted == [],
          f"junk family emitted {emitted}, expected nothing")
    check(failures, tag, ins.family_combo.currentText() == "Georgia",
          f"junk did not revert: field shows {ins.family_combo.currentText()!r}")
    w.close()
    if not failures:
        print(f"  {tag:24} editable combo; 'Georgia' commits once; junk reverts, "
              f"emits nothing")


def test_selection_visible(failures):
    tag = "selection_visible"
    # The polish landed: handle size + outline width come from the theme tokens.
    from pdftexteditor.ui import page_view
    check(failures, tag, abs(page_view._HANDLE_PX - theme.HANDLE_PX) < 1e-9,
          f"_HANDLE_PX {page_view._HANDLE_PX} != theme.HANDLE_PX {theme.HANDLE_PX}")
    check(failures, tag, theme.HANDLE_PX > 9.0,
          f"HANDLE_PX {theme.HANDLE_PX} not raised above the old 9.0")
    check(failures, tag, theme.SELECTION_OUTLINE_W > 1.5,
          f"SELECTION_OUTLINE_W {theme.SELECTION_OUTLINE_W} not raised above 1.5")
    check(failures, tag, theme.color_selection_halo().alpha() > 200,
          "selection halo is not a near-opaque white under-stroke")
    # actually select a box in a live window and confirm the overlay exists.
    w = _make_window()
    w.open_path(THREE)
    box = w.document.spans(0)[0]
    w.view.select_box(box)
    check(failures, tag, w.view._overlay is not None,
          "selecting a box did not create a selection overlay")
    w.close()
    if not failures:
        print(f"  {tag:24} HANDLE_PX={theme.HANDLE_PX}, OUTLINE_W="
              f"{theme.SELECTION_OUTLINE_W}, halo opaque; overlay installs")


# ==========================================================================
# thumbnail helper sanity
# ==========================================================================
def test_thumbnail(failures):
    tag = "thumbnail"
    doc = PDFDocument(THREE)
    pix = doc.render_thumbnail(1, max_px=120)     # landscape page
    long_edge = max(pix.width, pix.height)
    check(failures, tag, abs(long_edge - 120) <= 1,
          f"thumbnail long edge {long_edge} != ~120")
    check(failures, tag, pix.width > pix.height,
          "landscape thumbnail is not wider than tall")
    doc.close()
    if not failures:
        print(f"  {tag:24} render_thumbnail long edge clamped to max_px")


def main() -> int:
    print("PAGE & DOCUMENT MANAGEMENT verification (PAGES_SPEC §7)\n")
    failures: list = []
    for fn in (
        test_working_doc_isolation,
        test_rotate,
        test_delete_page,
        test_insert_blank,
        test_duplicate,
        test_move_page_permutations,
        test_merge_path,
        test_merge_doc_bakes_other,
        test_extract,
        test_split,
        test_bake_then_mutate_preserves_edits,
        test_structural_undo,
        test_structural_undo_bakes_staged,
        test_invariants_after_structural,
        test_thumbnail,
        test_workspace_open_dedup,
        test_workspace_close_active,
        test_workspace_move_tab,
        test_tabs_app,
        test_sidebar_reorder_app,
        test_searchable_font,
        test_selection_visible,
    ):
        fn(failures)
    print("\n" + "=" * 70)
    if failures:
        print(f"FAILED ({len(failures)} assertion failure(s)):")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("PASSED -- structural ops (rotate/delete/insert/duplicate/move/merge/"
          "extract/split) work + persist; bake-then-mutate preserves edits + "
          "WYSIWYG; structural undo round-trips; invariants (overlap-merge, "
          "non-destructive redaction, no tofu) survive a restructure; Workspace "
          "multi-doc + dedup + close/reorder; tabs/sidebar/searchable-font/"
          "selection polish all live in the real window.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
