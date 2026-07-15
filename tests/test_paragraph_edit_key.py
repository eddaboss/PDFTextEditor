"""Regression: a ParagraphBox must not share its staged-edit slot with its
first member Span.

ParagraphBox.key and Span.key are BOTH (block_index, line_index, span_index),
and a ParagraphBox takes those three from members[0] -- so a paragraph and its
first line collide on .key. The staged-edit map (_edits) used to key off
box.key, so on a dense form where a paragraph and an overlapping single line are
both editable, committing one clobbered the other's staged text and the edit
reverted on reopen. The fix keys _edits off box.identity (("para",)-prefixed for
a paragraph), leaving box.key untouched (redaction keys off the raw indices).
"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from pdftexteditor.document import PDFDocument, Span, ParagraphBox


def _span(bi, li, si, text="line one"):
    return Span(
        text=text, bbox=(0.0, 0.0, 100.0, 10.0), origin=(0.0, 10.0),
        size=10.0, color=(0.0, 0.0, 0.0), font="Helvetica", flags=0,
        font_xref=None, ascender=0.9, descender=-0.2,
        block_index=bi, line_index=li, span_index=si, page_index=0,
    )


def _para(member0, member1):
    return ParagraphBox(
        page_index=0, bbox=(0.0, 0.0, 100.0, 30.0), origin=(0.0, 10.0),
        members=(member0, member1),
        text=member0.text + " " + member1.text, size=10.0,
        color=(0.0, 0.0, 0.0), font="Helvetica", flags=0, font_xref=None,
        ascender=0.9, descender=-0.2,
        block_index=member0.block_index, line_index=member0.line_index,
        span_index=member0.span_index,
    )


def test_paragraph_and_first_member_get_distinct_edit_keys():
    member0 = _span(2, 3, 1, "Needs assistance for all activities")
    member1 = _span(2, 4, 1, "Dependent upon adaptive device(s)")
    para = _para(member0, member1)

    # The underlying collision the bug came from: same .key.
    assert para.key == member0.key == (2, 3, 1)
    # identity is the collision-safe discriminator the fix keys off.
    assert para.identity != member0.identity

    k_para = PDFDocument._span_edit_key(0, para)
    k_span = PDFDocument._span_edit_key(0, member0)
    # The whole point: the paragraph and its first line no longer share one
    # _edits slot, so an edit to one can never clobber the other.
    assert k_para != k_span, (
        f"paragraph and member share _edits key: {k_para} == {k_span}")
    # Still page-qualified 2-tuples so the (page, _) iterations keep working.
    assert k_para[0] == 0 and k_span[0] == 0


if __name__ == "__main__":
    test_paragraph_and_first_member_get_distinct_edit_keys()
    print("ok: paragraph and first-member span get distinct staged-edit keys")
