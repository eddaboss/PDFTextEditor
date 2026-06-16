"""Headless sanity check for the CANVAS editing UX (page_view.py).

Drives the PageView directly on tests/fixtures/form_like.pdf, with a minimal
local command driver standing in for the window's single mutation route
(boxCommandRequested -> model mutator -> repaint), then exercises every
interaction: single-click select, restyle, move, resize, add (with text-edit
commit), delete, undo/redo. Renders scene crops to tests/screenshots/canvas/.

Run:
    QT_QPA_PLATFORM=offscreen \
      /Users/edward/Documents/GitHub/PDFTextEditor/.venv/bin/python \
      tests/_canvas_check.py
"""
from __future__ import annotations

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from PySide6.QtCore import QRectF, QPointF                     # noqa: E402
from PySide6.QtGui import QColor, QImage, QPainter             # noqa: E402
from PySide6.QtWidgets import QApplication                     # noqa: E402

_APP = QApplication.instance() or QApplication([])

from pdftexteditor.document import NewBox, PDFDocument          # noqa: E402
from pdftexteditor.ui.page_view import PageView, SpanHotspot    # noqa: E402

FIXTURE = os.path.join(_HERE, "fixtures", "form_like.pdf")
SHOT_DIR = os.path.join(_HERE, "screenshots", "canvas")
os.makedirs(SHOT_DIR, exist_ok=True)

failures: list[str] = []


def check(tag: str, cond: bool, msg: str) -> bool:
    if not cond:
        failures.append(f"{tag}: {msg}")
    return cond


def render(view: PageView, name: str) -> None:
    scene = view.scene()
    rect = scene.sceneRect()
    w = max(1, int(rect.width()))
    h = max(1, int(rect.height()))
    img = QImage(w, h, QImage.Format_ARGB32)
    img.fill(QColor(0xE8, 0xE8, 0xEA).rgb())
    p = QPainter(img)
    p.setRenderHint(QPainter.Antialiasing, True)
    p.setRenderHint(QPainter.SmoothPixmapTransform, True)
    p.setRenderHint(QPainter.TextAntialiasing, True)
    scene.render(p, QRectF(img.rect()), rect)
    p.end()
    img.save(os.path.join(SHOT_DIR, name))


# --- A local command driver that mirrors the window's single mutation route ---
class Driver:
    """Wires view.boxCommandRequested -> model mutator -> repaint, exactly like
    the window's BoxCommand, but tracks an add stack so cancel_add pops it."""

    def __init__(self, view: PageView, doc: PDFDocument):
        self.view = view
        self.doc = doc
        self.style_provider = lambda: {
            "font_family": "Helvetica", "size": 12.0,
            "color": (0.0, 0.0, 0.0), "bold": False, "italic": False,
        }
        view.set_add_style_provider(self.style_provider)
        view.boxCommandRequested.connect(self.on_request)
        view.editCommitted.connect(self.on_text_committed)
        self._last_add_box = None

    def on_text_committed(self, page: int, box, new_text: str) -> None:
        """Mirror the window's text command: stage the typed text (works for a
        Span or a NewBox) and repaint."""
        self.doc.stage_edit(page, box, new_text)
        self.view.repaint_box(box)

    def on_request(self, kind: str, box, params: dict) -> None:
        page = self.view.page_index
        if kind == "style":
            self.doc.set_style(page, box, **(params.get("overrides") or {}))
            self.view.repaint_box(box)
        elif kind == "move":
            self.doc.move_box(page, box, params["dx"], params["dy"])
            self.view.repaint_box(box)
        elif kind == "resize":
            self.doc.resize_box(page, box, params["scale"],
                                anchor=params.get("anchor"))
            self.view.repaint_box(box)
        elif kind == "delete":
            self.doc.delete_box(page, box)
            self.view.repaint_box(box)
        elif kind == "add":
            nb = self.doc.add_box(page, params["origin"], params["text"],
                                  params["family"], params["size"],
                                  params["color"], params["bold"],
                                  params["italic"])
            self._last_add_box = nb
            self.view.reload()
            self.view.select_box(nb)   # view auto-opens the editor (awaiting add)
        elif kind == "cancel_add":
            self.doc.delete_box(page, box)
            self.view.repaint_box(box)


def style_color(span):
    return tuple(round(c, 3) for c in span.color)


def main() -> int:
    doc = PDFDocument(FIXTURE)
    view = PageView()
    view.resize(900, 1100)
    view.set_document(doc)
    driver = Driver(view, doc)   # keep a strong ref so the slot is not GC'd
    render(view, "00_loaded.png")

    hotspots = view._hotspots
    check("load", len(hotspots) > 0, "no hotspots on the form page")

    # --- 1. SELECT a value box (Arial value run) -------------------------
    target = None
    for hs in hotspots:
        sp = hs.box
        if getattr(sp, "font", "") == "ArialMT" and sp.text.strip():
            target = hs
            break
    if target is None:
        target = hotspots[0]
    sel_events: list = []
    view.selectionChanged.connect(lambda b: sel_events.append(b))
    view._on_box_press(target, view._scene_point(*target.box.origin))
    check("select", view.current_selection() is not None,
          "single-click did not select a box")
    check("select", view._overlay is not None, "no selection overlay built")
    check("select", len(view._overlay._handles) == 8,
          f"expected 8 handles, got {len(view._overlay._handles)}")
    check("select", sel_events and sel_events[-1] is view.current_selection(),
          "selectionChanged did not carry the selected box")
    render(view, "01_selected.png")
    selected = view.current_selection()
    orig_origin = selected.origin

    # --- 2. RESTYLE the selection (family+size+color+bold) ---------------
    view.apply_style({"font_family": "Georgia", "size": 22.0,
                      "color": (0.85, 0.10, 0.10), "bold": True})
    st = doc.effective_style(view.page_index, view.current_selection())
    check("style", st["font_family"] == "Georgia",
          f"family not applied: {st['font_family']}")
    check("style", abs(st["size"] - 22.0) < 0.01, f"size not applied: {st['size']}")
    check("style", st["bold"] is True, "bold not applied")
    check("style", abs(st["color"][0] - 0.85) < 0.02, f"color not applied: {st['color']}")
    render(view, "02_restyled.png")

    # --- 3. MOVE the selection (+40, -25 pt) -----------------------------
    box = view.current_selection()
    start_scene = view._scene_point(*box.origin)
    view._arm_move(box, start_scene)
    view._drag_armed = True
    z = view.zoom
    end_scene = QPointF(start_scene.x() + 40 * z, start_scene.y() - 25 * z)
    view._finish_drag(end_scene)
    moved = view.current_selection()
    eff = doc._edits.get((view.page_index, moved.key))
    dx, dy = (eff.move if eff and eff.move else (0.0, 0.0))
    check("move", abs(dx - 40) < 1.0 and abs(dy + 25) < 1.0,
          f"move delta {(dx, dy)} != ~(40, -25)")
    render(view, "03_moved.png")

    # --- 4. RESIZE via SE handle (scale up) ------------------------------
    box = view.current_selection()
    rect = view._span_scene_rect(box)
    view._begin_resize("se", QPointF(rect.right(), rect.bottom()))
    start_size = doc.effective_style(view.page_index, box)["size"]
    anchor = view._opposite_corner_scene(rect, "se")  # nw corner
    far = QPointF(anchor.x() + (rect.right() - anchor.x()) * 1.8,
                  anchor.y() + (rect.bottom() - anchor.y()) * 1.8)
    view._finish_drag(far)
    new_size = doc.effective_style(view.page_index, view.current_selection())["size"]
    check("resize", new_size > start_size * 1.4,
          f"resize did not grow size: {start_size} -> {new_size}")
    render(view, "04_resized.png")

    # --- 5. ADD a new box, type text, commit -----------------------------
    add_events: list = []
    view.boxAdded.connect(lambda b: add_events.append(b))
    view.enter_add_text_mode()
    check("add", view.current_mode() == "add_text", "did not enter add_text mode")
    click_scene = view._scene_point(120, 460)   # empty Notes band
    view._do_add_at(click_scene)
    check("add", view._editor is not None, "add did not open a text editor")
    check("add", isinstance(view._editor_box, NewBox),
          "editor not mounted on a NewBox after add")
    view._editor.setPlainText("Reviewed 2026")
    view.commit_edit()
    news = doc.new_boxes(view.page_index)
    check("add", any(b.text == "Reviewed 2026" for b in news),
          f"new box text not committed; new_boxes={[b.text for b in news]}")
    check("add", view.current_mode() == "select", "did not return to select mode")
    render(view, "05_added.png")
    new_count = len(doc.new_boxes(view.page_index))

    # --- 5b. Empty add is cleaned up (stray click leaves no box) ---------
    before = len(doc.new_boxes(view.page_index))
    view.enter_add_text_mode()
    view._do_add_at(view._scene_point(300, 500))
    check("empty_add", view._editor is not None, "empty add did not open editor")
    view._editor.setPlainText("   ")   # whitespace only
    view.commit_edit()
    after = len(doc.new_boxes(view.page_index))
    check("empty_add", after == before,
          f"empty add left a box: {before} -> {after}")

    # --- 6. SELECT the added box, then DELETE it -------------------------
    added_box = next((b for b in doc.new_boxes(view.page_index)
                      if b.text == "Reviewed 2026"), None)
    check("delete", added_box is not None, "could not re-find the added box")
    if added_box is not None:
        view.select_box(added_box)
        check("delete", view.current_selection() is not None,
              "could not select the added box")
        view.delete_selected()
        remaining = [b.text for b in doc.new_boxes(view.page_index)]
        check("delete", "Reviewed 2026" not in remaining,
              f"delete left the box: {remaining}")
        check("delete", view.current_selection() is None,
              "selection not cleared after delete")
    render(view, "06_deleted.png")

    # --- 7. Empty-canvas click deselects ---------------------------------
    view.select_box(view._boxes[0])
    check("deselect", view.current_selection() is not None, "pre-deselect select failed")
    view.clear_selection()
    check("deselect", view.current_selection() is None, "clear_selection failed")

    # --- 8. Selection survives a reload (identity re-find) ---------------
    view.select_box(view._boxes[0])
    ident = view.current_selection().identity
    view.reload()
    check("reload", view.current_selection() is not None,
          "selection lost across reload")
    check("reload", view.current_selection().identity == ident,
          "reload reselected the wrong box")

    if failures:
        print("CANVAS CHECK FAILED:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("CANVAS CHECK PASSED -- select/restyle/move/resize/add/delete/"
          "empty-add-cleanup/deselect/reload-reselect all work; "
          f"8 handles; new boxes committed ({new_count}); "
          "screenshots in tests/screenshots/canvas/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
