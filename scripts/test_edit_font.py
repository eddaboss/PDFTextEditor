"""Verify the wired _edit_synth_font resolves the date field to the right font via the
short-field synth matcher."""
import os
import re
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import fitz  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

_app = QApplication.instance() or QApplication([])

from pdftexteditor.document import PDFDocument  # noqa: E402
from pdftexteditor.ocr import engine as E  # noqa: E402

PDF = os.path.expanduser("~/Downloads/doc05154920260624150538.pdf")
PG = 1
DATE_RE = re.compile(r"^\d{1,2}/\d{1,2}/\d{2,4}$|^\d{1,2}/\d{4}$")


def main():
    doc = PDFDocument(PDF)
    rgb = doc.render_page_image(PG, 300.0)
    lines = E.get_engine("auto").recognize(rgb)
    for ln in lines:
        t = ln.text.strip()
        if not DATE_RE.match(t):
            continue
        x0, y0, x1, y1 = ln.bbox
        region = rgb[max(0, int(y0)):int(y1) + 1, max(0, int(x0)):int(x1) + 1].copy()
        ctx = {"region": region, "orig_text": t, "page_index": PG}
        fpath = doc._edit_synth_font(ctx)
        nm = fitz.Font(fontfile=fpath).name if fpath and os.path.exists(fpath) else fpath
        print(f"{t!r} -> _edit_synth_font: {os.path.basename(fpath) if fpath else None} ({nm})")
    doc.close()


if __name__ == "__main__":
    main()
