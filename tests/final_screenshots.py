"""Render the FINAL deliverable screenshots into tests/screenshots/final/.

Drives the REAL MainWindow (offscreen) on the reflow fixtures and grabs the
six shots the build calls for:

  01_three_pane.png        the redesigned 3-pane window (left tools+Format /
                           center continuous page / right thumbnails), fit-width
  02_selected_paragraph.png a ParagraphBox single-click-selected (overlay shown,
                           Format panel populated with alignment + line spacing)
  03_mid_reflow_edit.png   the inline multi-line editor mounted over the
                           paragraph, mid text edit (caret live)
  04_find_replace_bar.png  the Find & Replace bar open in the left column with a
                           cross-page query and the match count
  05_alignment_applied.png the selected paragraph after Center alignment is
                           applied (layout re-laid)
  06_second_page.png       a later page scrolled into view (continuous scroll)

Run:
    QT_QPA_PLATFORM=offscreen \
      /Users/edward/Documents/GitHub/PDFTextEditor/.venv/bin/python \
      tests/final_screenshots.py
"""

from __future__ import annotations

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from PySide6.QtWidgets import QApplication  # noqa: E402

_APP = QApplication.instance() or QApplication(sys.argv)

from pdftexteditor.ui.main_window import MainWindow  # noqa: E402

FIXTURES = os.path.join(_HERE, "fixtures")
MULTIPAGE = os.path.join(FIXTURES, "multipage_body.pdf")
PARAGRAPHS = os.path.join(FIXTURES, "paragraphs.pdf")
OUT_DIR = os.path.join(_HERE, "screenshots", "final")
os.makedirs(OUT_DIR, exist_ok=True)


def pump(n: int = 6) -> None:
    for _ in range(n):
        _APP.processEvents()


def open_window(path: str) -> MainWindow:
    w = MainWindow()
    w.resize(1360, 980)
    w.show()
    w.open_path(path)
    pump(8)
    if getattr(w, "empty_state", None) is not None:
        w.empty_state.hide()
    w.repaint()
    pump(4)
    return w


def shot(w: MainWindow, name: str) -> str:
    out = os.path.join(OUT_DIR, name)
    pump(3)
    w.repaint()
    pump(2)
    w.grab().save(out)
    return out


def close(w: MainWindow) -> None:
    w._suppress_close_guard = True
    w.close()
    pump(2)


def first_paragraph(view):
    """Materialize page 0 and return its first ParagraphBox + hotspot."""
    layer = view._layers[0]
    if not layer.rendered:
        view._materialize_page(layer)
    pump(2)
    paras = [b for b in layer.boxes if getattr(b, "is_paragraph", False)]
    if not paras:
        return None, None
    box = paras[0]
    return box, view._hotspot_for(box)


def main() -> int:
    paths: list[str] = []

    # ---- 01: the redesigned 3-pane window on a multipage doc -------------
    w = open_window(MULTIPAGE)
    # Ensure fit-width (default) and both docks visible.
    paths.append(shot(w, "01_three_pane.png"))
    close(w)

    # ---- 02-05: paragraph select / edit / find / alignment --------------
    w = open_window(PARAGRAPHS)
    view = w.view
    box, hotspot = first_paragraph(view)
    if box is None:
        print("ERROR: no ParagraphBox on page 0 of paragraphs.pdf")
        close(w)
        return 1

    # 02: select the paragraph (overlay + Format panel populated).
    view.select_box(box)
    pump(4)
    paths.append(shot(w, "02_selected_paragraph.png"))

    # 03: mid reflow edit -- mount the inline editor over the paragraph and
    # stage a LONGER replacement so the box visibly re-wraps under the caret.
    hs = view._hotspot_for(box)
    view.begin_edit(hs)
    pump(3)
    editor = getattr(view, "_editor", None)
    if editor is not None:
        editor.setPlainText(
            "The board reviewed the revised quarterly operations plan in detail "
            "and approved the expanded rollout across every regional office for "
            "the upcoming fiscal cycle, effective the first business day.")
        cur = editor.textCursor()
        cur.movePosition(cur.MoveOperation.End)
        editor.setTextCursor(cur)
        editor.setFocus()
    pump(4)
    paths.append(shot(w, "03_mid_reflow_edit.png"))
    # Cancel the live edit cleanly so the next shots start from a clean doc.
    view._cancel_editor_silent()
    pump(3)

    # 04: Find & Replace bar in the left column, seeded with a cross-page query.
    view.clear_selection()
    pump(2)
    w.act_find.trigger()
    pump(3)
    w.find_panel.set_query("operations")
    pump(2)
    w.find_panel.refresh()
    pump(3)
    paths.append(shot(w, "04_find_replace_bar.png"))
    # Return to Select so the Format panel comes back.
    w.left_panel.set_active_tool("select")
    pump(3)

    # 05: alignment applied -- select the paragraph and click Center.
    box2, _ = first_paragraph(view)
    view.select_box(box2)
    pump(4)
    insp = w.inspector
    center_btn = insp._align_buttons.get("center")
    if center_btn is not None:
        center_btn.setChecked(True)
        center_btn.click()
    pump(5)
    paths.append(shot(w, "05_alignment_applied.png"))
    close(w)

    # ---- 06: a later page scrolled into view (continuous scroll) --------
    w = open_window(MULTIPAGE)
    view = w.view
    last = max(1, w.document.page_count - 1)
    target = min(1, last) if w.document.page_count >= 2 else 0
    # Prefer page index 1 (the SECOND page) if it exists; else the last.
    target = 1 if w.document.page_count >= 2 else last
    view.set_page(target)
    view._on_scroll_settled()
    pump(6)
    paths.append(shot(w, "06_second_page.png"))
    close(w)

    print("Final screenshots rendered:")
    for p in paths:
        ok = os.path.exists(p) and os.path.getsize(p) > 0
        print(f"  [{'OK' if ok else 'MISSING'}] {p}")
    missing = [p for p in paths if not (os.path.exists(p) and os.path.getsize(p) > 0)]
    if missing:
        print(f"FAILED: {len(missing)} screenshot(s) missing/empty")
        return 1
    print(f"PASSED -- {len(paths)} final screenshots rendered.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
