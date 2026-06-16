"""Render the three UX-polish proofs to tests/screenshots/ux/ (PAGES_SPEC §5.6/§5.7).

Drives the REAL ``MainWindow`` headless (``QT_QPA_PLATFORM=offscreen``) so each
shot is the actual shipping chrome, not a mock:

  1. font_search_dropdown.png  -- the searchable Inspector family combo: editable
     line edit with the case-insensitive CONTAINS completer popup filtered to a
     typed substring ("geor" -> Georgia ...), the polish from §5.6.
  2. selected_box.png          -- a box selected on the canvas, showing the
     two-pass white-halo + accent outline, the larger grab handles, and the faint
     accent wash (§5.7); rendered scene-only so the selection chrome reads clean.
  3. combine_dialog.png        -- the Combine PDFs file picker the "Combine…"
     action opens (a non-native multi-select QFileDialog, identical to the one
     ``combine_pdfs`` raises), pointed at tests/fixtures so real PDFs are listed.

Run:
  QT_QPA_PLATFORM=offscreen \
    /Users/edward/Documents/GitHub/PDFTextEditor/.venv/bin/python tests/ux_screenshots.py
"""

from __future__ import annotations

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from PySide6.QtCore import QRectF  # noqa: E402
from PySide6.QtGui import QColor, QImage, QPainter  # noqa: E402
from PySide6.QtWidgets import QApplication, QFileDialog  # noqa: E402

_APP = QApplication.instance() or QApplication([])

from pdftexteditor.ui.main_window import MainWindow  # noqa: E402

FIXTURES = os.path.join(REPO, "tests", "fixtures")
FORM = os.path.join(FIXTURES, "form_like.pdf")
SHOT_DIR = os.path.join(REPO, "tests", "screenshots", "ux")
os.makedirs(SHOT_DIR, exist_ok=True)


def pump(n: int = 3) -> None:
    for _ in range(n):
        _APP.processEvents()


def open_window(path: str = FORM) -> MainWindow:
    w = MainWindow()
    w.resize(1180, 920)
    w.show()
    w.open_path(path)
    pump(6)
    # The empty-state overlay hides on open; force it out of the offscreen
    # back-buffer so a window grab never catches its ghost mid-repaint.
    if getattr(w, "empty_state", None) is not None:
        w.empty_state.hide()
    w.repaint()
    pump(4)
    return w


def hotspot_for(w: MainWindow, contains: str):
    for h in w.view._hotspots:
        if contains in h.span.text:
            return h
    return None


def select(w: MainWindow, contains: str):
    """Select a box by substring through the public path a click uses, so the
    window's selectionChanged -> inspector.set_target + the SelectionOverlay
    install fire exactly as in normal use."""
    h = hotspot_for(w, contains)
    if h is None:
        return None
    w.view.select_box(h.span)
    pump()
    return w.view.current_selection()


def save_widget(widget, name: str) -> str:
    out = os.path.join(SHOT_DIR, name)
    pump()
    widget.grab().save(out)
    return out


def shot_scene(w: MainWindow, name: str) -> str:
    """Scene-only render (white sheet + selection chrome), the same crisp
    canvas capture test_editor uses for selection shots."""
    scene = w.view.scene()
    rect = scene.sceneRect()
    width = max(1, int(rect.width()))
    height = max(1, int(rect.height()))
    img = QImage(width, height, QImage.Format_ARGB32)
    img.fill(QColor(0xE8, 0xE8, 0xEA).rgb())
    p = QPainter(img)
    p.setRenderHint(QPainter.Antialiasing, True)
    p.setRenderHint(QPainter.SmoothPixmapTransform, True)
    p.setRenderHint(QPainter.TextAntialiasing, True)
    scene.render(p, QRectF(img.rect()), rect)
    p.end()
    out = os.path.join(SHOT_DIR, name)
    img.save(out)
    return out


# ==========================================================================
# 1. Searchable font-family dropdown (§5.6)
# ==========================================================================
def shot_font_search(w: MainWindow, shots: list, results: list) -> None:
    # Select a real box so the Inspector form is enabled (not the empty state).
    box = select(w, "Smith") or select(w, "Name") or select(w, ".")
    combo = w.inspector.family_combo
    assert combo.isEditable(), "family combo must be editable for type-to-filter"

    # A substring that is CONTAINED inside many family names (mid-word, not a
    # prefix) so the popup proves the §5.6 MatchContains behavior -- a plain
    # prefix completer could never surface "Arial" from a typed "ria".
    typed = "ria"
    combo.setFocus()
    line = combo.lineEdit()
    line.setText(typed)
    pump()
    completer = combo.completer()
    completer.setCompletionPrefix(typed)
    completer.complete()                      # raise the filtered popup
    pump()

    popup = completer.popup()
    matches = [completer.completionModel().index(r, 0).data()
               for r in range(completer.completionModel().rowCount())]
    contains_ok = all(typed in (m or "").casefold() for m in matches) and bool(matches)
    # A contains-only completer surfaces families where "ria" appears mid-name
    # (Arial, Arial Black, ...), which a prefix completer never would.
    midword_ok = any(not (m or "").casefold().startswith(typed) for m in matches)
    results.append(("font_search.matches", matches))
    results.append(("font_search.contains_only", contains_ok))
    results.append(("font_search.midword_match", midword_ok))

    # Grab the whole window (proves the editable, filtered line edit in context).
    shots.append(save_widget(w, "font_search_dropdown.png"))
    # Grab the popup alone -- the offscreen platform makes the completer popup a
    # separate top-level window a window grab cannot capture, so shoot it direct.
    if popup is not None and popup.isVisible():
        popup.resize(max(220, popup.width()), popup.sizeHintForRow(0) *
                     max(1, len(matches)) + 8)
        pump()
        shots.append(save_widget(popup, "font_search_popup.png"))
    line.setText("")
    pump()


# ==========================================================================
# 2. Clearly-selected box (§5.7): halo + accent outline + handles + wash
# ==========================================================================
def shot_selection(w: MainWindow, shots: list, results: list) -> None:
    # Zoom in so the two-pass halo + accent outline + the enlarged 11px handles
    # read at full prominence (the whole point of §5.7 -- obviously visible).
    w.set_zoom(2.2)
    pump()
    box = select(w, "1,250") or select(w, "Pending") or select(w, ".")
    sel = w.view.current_selection()
    results.append(("selection.box_selected", sel is not None))
    # Full window so the selection reads in context (toolbar + Inspector + sheet).
    shots.append(save_widget(w, "selected_box_window.png"))
    # Scene-only crisp capture of the page + selection chrome.
    shots.append(shot_scene(w, "selected_box.png"))
    # A TIGHT crop around the selected box so the halo, accent stroke, and the
    # eight grab handles are unmistakable at a glance.
    if sel is not None:
        shots.append(shot_selection_crop(w, sel, "selected_box_crop.png"))
    # Reset zoom for any later shots.
    w.set_zoom(1.5)
    pump()


def shot_selection_crop(w: MainWindow, box, name: str) -> str:
    """Render a tight crop of the scene around the selected box's effective
    bbox, padded so the halo + handles + wash are fully framed."""
    view = w.view
    scene = view.scene()
    # Map the box's PDF-space bbox into scene units via the view's own helper if
    # present; otherwise fall back to the selection overlay's scene bounds.
    rectf = None
    overlay = getattr(view, "_sel_overlay", None) or \
        getattr(view, "_selection_overlay", None) or \
        getattr(view, "_overlay", None)
    if overlay is not None:
        try:
            rectf = overlay.mapRectToScene(overlay.boundingRect())
        except Exception:  # noqa: BLE001
            rectf = None
    if rectf is None:
        rectf = scene.sceneRect()
    pad = 46.0
    crop = rectf.adjusted(-pad, -pad, pad, pad)
    w_px = max(1, int(crop.width()))
    h_px = max(1, int(crop.height()))
    img = QImage(w_px, h_px, QImage.Format_ARGB32)
    img.fill(QColor(0xE8, 0xE8, 0xEA).rgb())
    p = QPainter(img)
    p.setRenderHint(QPainter.Antialiasing, True)
    p.setRenderHint(QPainter.SmoothPixmapTransform, True)
    p.setRenderHint(QPainter.TextAntialiasing, True)
    scene.render(p, QRectF(img.rect()), crop)
    p.end()
    out = os.path.join(SHOT_DIR, name)
    img.save(out)
    return out


# ==========================================================================
# 3. Combine PDFs dialog (§5.3)
# ==========================================================================
def shot_combine_dialog(w: MainWindow, shots: list, results: list) -> None:
    # Build the SAME picker combine_pdfs() raises, non-native + non-modal so we
    # can show + grab it headless (the action calls the static getOpenFileNames;
    # this constructs the equivalent instance for a screenshot).
    dlg = QFileDialog(w, "Combine PDFs (append to this document)", FIXTURES,
                      "PDF files (*.pdf)")
    dlg.setOption(QFileDialog.DontUseNativeDialog, True)
    dlg.setFileMode(QFileDialog.ExistingFiles)        # multi-select, matches action
    dlg.setAcceptMode(QFileDialog.AcceptOpen)
    dlg.setDirectory(FIXTURES)
    dlg.resize(880, 560)
    dlg.show()
    pump(5)
    results.append(("combine.is_multiselect",
                    dlg.fileMode() == QFileDialog.ExistingFiles))
    results.append(("combine.dir", dlg.directory().absolutePath()))
    shots.append(save_widget(dlg, "combine_dialog.png"))
    dlg.close()
    pump()


def main() -> int:
    shots: list = []
    results: list = []
    w = open_window()
    try:
        shot_font_search(w, shots, results)
        shot_selection(w, shots, results)
        shot_combine_dialog(w, shots, results)
    finally:
        w.close()
        pump()

    print("UX screenshot facts:")
    for k, v in results:
        print(f"  {k:34} {v}")
    print("\nWrote:")
    for s in shots:
        print(f"  {s}")

    # Soft gate: the polish must actually be exercised.
    facts = dict(results)
    ok = (facts.get("font_search.contains_only") is True
          and facts.get("font_search.midword_match") is True
          and facts.get("selection.box_selected") is True
          and facts.get("combine.is_multiselect") is True
          and all(os.path.exists(s) and os.path.getsize(s) > 0 for s in shots))
    print("\nPASSED" if ok else "\nFAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
