"""Regression: a NEWLY ADDED text box must never inherit an INVISIBLE color.

A document's header text is often WHITE because it sits on a dark/colored bar.
That white must NOT carry onto a box dropped on the white page body -- there the
text is typed and present but unreadable (the box shows empty/white though the
editor shows black). The contrast guard (``PageView._add_color_visible_on``)
keeps the color when it is visible (incl. a deliberate white-on-the-dark-bar
add) and falls back to a contrasting ink when it would be invisible.

Run:
    QT_QPA_PLATFORM=offscreen \
      /Users/edward/Documents/GitHub/PDFTextEditor/.venv/bin/python \
      tests/test_add_box_contrast.py
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
from PySide6.QtCore import QEventLoop, QTimer  # noqa: E402

_APP = QApplication.instance() or QApplication([])

from pdftexteditor.ui.main_window import MainWindow  # noqa: E402


def pump(ms: int) -> None:
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()


def check(failures, tag, cond, msg):
    if not cond:
        failures.append(f"{tag}: {msg}")
    return cond


def build_fixture(path: str) -> None:
    """One page: a dark bar with WHITE header text, black body text, and a wide
    blank white area below -- the minimal shape of a form with a colored header."""
    doc = fitz.open()
    page = doc.new_page(width=612, height=792)
    bar = fitz.Rect(40, 50, 560, 74)
    page.draw_rect(bar, color=(0.20, 0.30, 0.60), fill=(0.20, 0.30, 0.60))
    page.insert_text((48, 67), "Subject Details", fontsize=12,
                     color=(1, 1, 1), fontname="helv")        # WHITE on the bar
    page.insert_text((48, 120), "No results were found.", fontsize=11,
                     color=(0, 0, 0), fontname="helv")        # black body text
    doc.save(path)
    doc.close()


def add_box_at(w, px, py, text="Sample"):
    """Drive the real Add-Text flow at a PDF point; return the new NewBox."""
    v = w.view
    before = {b.edit_key for b in w.document.new_boxes(0)}
    v.enter_add_text_mode()
    v._do_add_at(v._scene_point(px, py, 0))
    pump(40)
    if v._editor is not None:
        v._editor.setPlainText(text)
        v.commit_edit()
        pump(40)
    new = [b for b in w.document.new_boxes(0) if b.edit_key not in before]
    return new[0] if new else None


def baked_dark_px(doc, box, scale=4.0):
    out = tempfile.mktemp(suffix=".pdf")
    doc.save_as(out)
    d = fitz.open(out)
    try:
        clip = fitz.Rect(box.bbox) + (-2, -3, 12, 3)
        pix = d[0].get_pixmap(matrix=fitz.Matrix(scale, scale), clip=clip,
                              alpha=False)
        s, step, n = pix.samples, pix.n, 0
        for i in range(0, len(s), step):
            r, g, b = s[i], s[i + 1], s[i + 2]
            if 0.299 * r + 0.587 * g + 0.114 * b < 140:
                n += 1
        return n
    finally:
        d.close()
        os.unlink(out)


def run():
    failures: list = []
    fx = os.path.join(tempfile.gettempdir(), "contrast_fixture.pdf")
    build_fixture(fx)

    w = MainWindow()
    w._suppress_close_guard = True
    w.resize(1000, 900)
    w.open_path(fx)
    w.show()
    pump(250)
    doc, v = w.document, w.view

    header = next(s for s in doc.spans(0) if "Subject" in s.text)
    check(failures, "precond", min(header.color) > 0.8,
          f"header span should be white, got {header.color}")

    def select_white_header():
        """Re-establish the invisible precondition before each add: selecting the
        white header makes the inspector's add-default color white."""
        v.select_box(header)
        pump(40)

    # Done BEFORE any black box is added, so ``style_near`` cannot inherit a
    # nearby dark box's color (that would mask what we're testing):

    # 3) Add ON the dark bar FIRST -> a legitimate white-on-dark add is PRESERVED
    #    (the guard only corrects an INVISIBLE pairing; white on the bar is fine).
    select_white_header()
    on_bar = add_box_at(w, 250, 67, "OnBar")
    if check(failures, "on-bar", on_bar is not None, "no box added on the bar"):
        check(failures, "on-bar", min(on_bar.color) > 0.8,
              f"white-on-dark add must stay white, got {on_bar.color}")

    # 1) Add on the BLANK BODY -> white default must be corrected to visible ink.
    select_white_header()
    body = add_box_at(w, 300, 430, "Jordan Lee")
    check(failures, "blank-body", body is not None, "no box was added")
    if body is not None:
        check(failures, "blank-body", max(body.color) < 0.3,
              f"body box must be dark/visible, got {body.color}")
        check(failures, "blank-body", baked_dark_px(doc, body) > 0,
              "body box baked with NO dark ink (still invisible)")

    # 2) Add NEXT TO the white header but still on white -> corrected to visible.
    select_white_header()
    near = add_box_at(w, 130, 96, "Near")
    if check(failures, "near-header", near is not None, "no box added near header"):
        check(failures, "near-header", max(near.color) < 0.3,
              f"box next to header on white bg must be dark, got {near.color}")

    # 4) SELF-HEAL: an already-invisible box (created before the guard existed)
    #    recolors to visible the moment it is opened to edit and committed.
    invisible = doc.add_box(0, (300, 430), "Jordan Lee", "Helvetica", 11.0,
                            (1.0, 1.0, 1.0), False, False)
    v.reload()
    pump(60)
    key = invisible.edit_key
    v.select_box(doc._new_boxes.get(key))
    pump(30)
    hs = next((h for h in v._hotspots
               if getattr(getattr(h, "box", None), "edit_key", None) == key), None)
    if check(failures, "self-heal", hs is not None, "could not re-open invisible box"):
        v.begin_edit(hs)
        v.commit_edit()                       # commit WITHOUT changing the text
        pump(60)
        healed = doc._new_boxes.get(key)
        check(failures, "self-heal", healed is not None and max(healed.color) < 0.3,
              f"invisible box should heal to dark on edit, got "
              f"{healed.color if healed else None}")

    # 5) DELETE actually removes a box (model-confirmed), even an invisible one.
    doomed = doc.add_box(0, (300, 470), "Doomed", "Helvetica", 11.0,
                         (1.0, 1.0, 1.0), False, False)
    v.reload()
    pump(60)
    dkey = doomed.edit_key
    v.select_box(doc._new_boxes.get(dkey))
    pump(30)
    n_before = len(doc.new_boxes(0))
    v.delete_selected()
    pump(40)
    gone = doc._new_boxes.get(dkey) is None or \
        getattr(doc._new_boxes.get(dkey), "deleted", False)
    check(failures, "delete", gone and len(doc.new_boxes(0)) == n_before - 1,
          "delete_selected did not remove the box from the model")

    w.close()

    if failures:
        print("FAILED:")
        for f in failures:
            print("  -", f)
        sys.exit(1)
    print("PASSED -- new boxes never inherit an invisible color; "
          "white-on-dark-bar add preserved; invisible boxes self-heal on edit; "
          "delete removes the box.")


if __name__ == "__main__":
    run()
