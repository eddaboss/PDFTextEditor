"""Capture the LIVE inline editor for a scanned-OCR box, offscreen.
Proves the flow: clicking in leaves the scan 1-for-1; typing re-renders only the
changed word. Renders the scene (what the user sees) on-open and after an edit.
Run: PYTHONPATH=. ~/Documents/GitHub/PDFTextEditor/.venv/bin/python scripts/diag_live_editor.py
"""
import os, sys, tempfile
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import numpy as np  # noqa
from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QImage, QPainter, QColor, QTextCursor
from PySide6.QtCore import QRectF, Qt
_app = QApplication.instance() or QApplication([])

from pdftexteditor.ui.main_window import MainWindow
from pdftexteditor.ocr import engine as E, reconstruct as R
from scripts.diag_ocr_font import make_scan, FONTS

import os.path as _p


def pump(n=6):
    for _ in range(n):
        _app.processEvents()


def render_scene(v, src: QRectF, scale=3.0):
    w, h = max(1, int(src.width() * scale)), max(1, int(src.height() * scale))
    img = QImage(w, h, QImage.Format_RGB888); img.fill(QColor("white"))
    p = QPainter(img); v.scene().render(p, QRectF(0, 0, w, h), src); p.end()
    return img


def main():
    with tempfile.TemporaryDirectory() as d:
        path = _p.join(d, "scan.pdf"); make_scan(path)
        w = MainWindow(); w.resize(1300, 950); w.show(); w.open_path(path); pump()
        if getattr(w, "empty_state", None) is not None:
            w.empty_state.hide()
        doc = w.document
        # run OCR synchronously and apply (same path as the async worker)
        rgb = doc.render_page_image(0, 300.0)
        lines = E.get_engine("auto").recognize(rgb)
        res = R.reconstruct_page(rgb, 300.0, lines,
                                 _p.join(FONTS, "Tinos-Regular.ttf"),
                                 _p.join(FONTS, "Arimo[wght].ttf"))
        w._ocr_apply_page(0, res); w.view.reload(); pump()
        print("family:", res.family_name)

        v = w.view
        hs = None
        for h in v._hotspots:
            if "Orders read back" in getattr(h.box, "text", ""):
                hs = h; break
        if hs is None:
            print("FAIL: no OCR hotspot for the target line"); return
        box = hs.box
        src = v._span_scene_rect(box).adjusted(-12, -14, 130, 14)

        # 1) ON OPEN: click in. Must look EXACTLY like the scan (no font change).
        v.begin_edit(hs); pump()
        print("scan_preserve mode:", v._editor_scan_preserve,
              "| editor text alpha:", v._editor.defaultTextColor().alpha(),
              "| editor qfont family:", v._editor.font().family())
        render_scene(v, src).save("/tmp/live_open.png"); print("saved /tmp/live_open.png")

        # 2) TYPE: change "MD" -> "RN". Only that word should re-render.
        ed = v._editor
        cur = ed.textCursor(); cur.select(QTextCursor.Document)
        cur.insertText(box.text.replace("MD", "RN"))
        ed.setTextCursor(cur)
        v._update_ocr_preview()                  # force the debounced live render
        pump()
        print("preview shown:", v._editor_preview is not None)
        render_scene(v, src).save("/tmp/live_edit.png"); print("saved /tmp/live_edit.png")
        v.cancel_edit(); pump()
        w.close()


if __name__ == "__main__":
    main()
