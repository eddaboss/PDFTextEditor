"""Test verify.match_by_synth on the date fields: does it return an acceptable bold sans?"""
import os
import re
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import numpy as np  # noqa: E402
import fitz  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

_app = QApplication.instance() or QApplication([])

from pdftexteditor.document import PDFDocument  # noqa: E402
from pdftexteditor.ocr import engine as E, supermatch as SM  # noqa: E402
from pdftexteditor.ocr import fontbank as FB, degrade as DG, verify as V  # noqa: E402
from render_verify_date import split_cov  # noqa: E402

PDF = os.path.expanduser("~/Downloads/doc05154920260624150538.pdf")
PG = 1
DATE_RE = re.compile(r"^\d{1,2}/\d{1,2}/\d{2,4}$|^\d{1,2}/\d{4}$")


def main():
    doc = PDFDocument(PDF)
    rgb = doc.render_page_image(PG, 300.0)
    lines = E.get_engine("auto").recognize(rgb)
    keys = FB._load_fingerprints()["paths"]
    ttf = FB._ensure_ttf_cache()
    for ln in lines:
        t = ln.text.strip()
        if not DATE_RE.match(t):
            continue
        x0, y0, x1, y1 = ln.bbox
        x0i, y0i = max(0, int(x0)), max(0, int(y0))
        x1i, y1i = min(rgb.shape[1], int(x1) + 1), min(rgb.shape[0], int(y1) + 1)
        region = rgb[y0i:y1i, x0i:x1i].copy()
        chars = [c for c in t if c.strip()]
        spans = split_cov(DG.coverage(region), len(chars))
        cells = [(chars[i], (x0i + a, y0i, x0i + b, y1i)) for i, (a, b) in enumerate(spans)]
        ms = SM._coarse_scores(rgb, cells)
        cand = [keys[int(i)] for i in np.argsort(-ms)[:20]]
        key = V.match_by_synth(doc, region, t, ttf, cand, seeds=5)
        nm = fitz.Font(fontfile=os.path.join(ttf, key)).name if key else "None"
        print(f"{t!r} -> {key} ({nm})   [Arial Bold in pool: {'00121.ttf' in cand}]")
    doc.close()


if __name__ == "__main__":
    main()
