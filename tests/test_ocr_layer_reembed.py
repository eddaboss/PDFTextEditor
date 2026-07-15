"""Regression: re-baking a reopened OCR file must REPLACE the embedded editable
layer, not silently keep the stale one.

``_baked_copy`` re-embeds the serialized OCR layer with ``embfile_add``. When the
file was reopened, ``out`` already contains ``layer_io.EMB_NAME``, so a plain
``embfile_add`` raises ValueError('...already exists') -- which the surrounding
bare ``except`` swallowed, dropping the new layer and leaving the STALE one in
the file. The fix updates in place when the name is already present. This test
round-trips a doc through ``tobytes`` (so the name really exists on reopen) and
asserts the update-or-add path yields the NEW blob.

CI-only: imports fitz (not installed locally). py_compiled locally, run in CI.
"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import fitz

from pdftexteditor.ocr import layer_io


def _reembed(out: "fitz.Document", blob: bytes) -> None:
    """The exact update-or-add used in PDFDocument._baked_copy."""
    if layer_io.EMB_NAME in out.embfile_names():
        out.embfile_upd(layer_io.EMB_NAME, blob)
    else:
        out.embfile_add(layer_io.EMB_NAME, blob)


def test_reembed_replaces_stale_layer():
    v1 = b"ocr-layer-version-1"
    v2 = b"ocr-layer-version-2-longer"

    doc = fitz.open()
    doc.new_page()
    # First bake: name absent -> add.
    _reembed(doc, v1)
    assert doc.embfile_get(layer_io.EMB_NAME) == v1

    # Reopen: the embedded file now exists in the freshly loaded doc.
    reopened = fitz.open("pdf", doc.tobytes())
    assert layer_io.EMB_NAME in reopened.embfile_names()

    # Second bake: name present -> update (the old code raised + dropped v2).
    _reembed(reopened, v2)
    assert reopened.embfile_get(layer_io.EMB_NAME) == v2


if __name__ == "__main__":
    test_reembed_replaces_stale_layer()
    print("ok")
