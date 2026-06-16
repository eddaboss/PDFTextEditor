"""Comments panel (annotations & markup §5.4).

The left column's THIRD stacked page (after Format and Find & Replace): a
list of EVERY annotation in the document -- staged specs and pre-existing
file annots alike -- with jump-to, Edit Note, and Delete actions. It is PURE
CHROME, built exactly like ``FindReplacePanel``: the window injects four
callables at construction --

  * ``list_annots() -> list[record]``   (document.annotations, every page)
  * ``jump(ref)``                       (scroll_to_page + select by identity)
  * ``edit(ref)``                       (the §5.3 note popup on the record)
  * ``delete(ref)``                     (ONE undoable 'delete' AnnotCommand)

-- so all model/undo coupling lives in the window and this module imports
zero model code (records are read through ``getattr``). The window calls
``refresh()`` after every AnnotCommand redo/undo, after structural ops, and
on tab switches, so the rows always mirror the live annotation state.

Rows are sorted by (page, y, x); each shows a kind glyph, "p.N", the
contents snippet (or the kind's label when empty), and a "(file)" suffix for
pre-existing file annots. Click = jump; double-click = Edit Note.

Object names are load-bearing for tests + QSS.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from . import theme

# One compact glyph per annotation kind (cheap, theme-neutral row markers;
# the canvas already shows the real appearance).
_KIND_GLYPHS = {
    "highlight": "▮",
    "underline": "▁",
    "strikeout": "─",
    "squiggly": "~",
    "note": "✎",
    "ink": "∿",
    "rect": "▭",
    "ellipse": "○",
    "line": "╱",
    "arrow": "→",
}

# The label shown when an annotation has no contents text.
_KIND_LABELS = {
    "highlight": "Highlight",
    "underline": "Underline",
    "strikeout": "Strikethrough",
    "squiggly": "Squiggly",
    "note": "Sticky Note",
    "ink": "Ink",
    "rect": "Rectangle",
    "ellipse": "Ellipse",
    "line": "Line",
    "arrow": "Arrow",
}

_SNIPPET_LEN = 48


class CommentsPanel(QWidget):
    """Every annotation in the document, listed and actionable (§5.4)."""

    def __init__(self, list_annots, jump, edit, delete, parent=None):
        super().__init__(parent)
        self.setObjectName("CommentsPanel")
        self._list_annots = list_annots
        self._jump = jump
        self._edit = edit
        self._delete = delete
        self._refreshing = False

        self._build()

    # --- construction ----------------------------------------------------
    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(12, 10, 12, 10)
        outer.setSpacing(8)

        # Count line ("3 comments" / "No comments").
        self.count_label = QLabel("No comments")
        self.count_label.setObjectName("CommentsCount")
        self.count_label.setFont(theme.ui_font(12))
        self.count_label.setStyleSheet(f"color: {theme.TEXT_SECONDARY};")
        outer.addWidget(self.count_label)

        self.list = QListWidget()
        self.list.setObjectName("CommentsList")
        self.list.setFont(theme.ui_font(12))
        self.list.setWordWrap(False)
        self.list.itemClicked.connect(self._on_item_clicked)
        self.list.itemDoubleClicked.connect(self._on_item_double_clicked)
        self.list.currentItemChanged.connect(
            lambda *_: self._sync_enablement())
        outer.addWidget(self.list, 1)

        # Row actions on the SELECTED row (§5.4).
        actions = QHBoxLayout()
        actions.setContentsMargins(0, 0, 0, 0)
        actions.setSpacing(8)
        self.edit_button = QPushButton("Edit Note")
        self.edit_button.setObjectName("CommentsEdit")
        self.edit_button.setCursor(Qt.PointingHandCursor)
        self.edit_button.clicked.connect(self._on_edit_clicked)
        self.delete_button = QPushButton("Delete")
        self.delete_button.setObjectName("CommentsDelete")
        self.delete_button.setCursor(Qt.PointingHandCursor)
        self.delete_button.clicked.connect(self._on_delete_clicked)
        actions.addWidget(self.edit_button)
        actions.addWidget(self.delete_button)
        outer.addLayout(actions)

        self._sync_enablement()

    # --- public API --------------------------------------------------------
    def refresh(self) -> None:
        """Rebuild the rows from ``list_annots()``, sorted by (page, y, x),
        keeping the selected row across the rebuild by identity. The window
        calls this after every annot command, structural op, and tab switch
        (§5.4); a failure must never crash chrome."""
        try:
            records = list(self._list_annots() or [])
        except Exception:  # noqa: BLE001 - a model read must not crash chrome
            records = []
        records.sort(key=self._sort_key)
        selected = self.selected_identity()
        self._refreshing = True
        try:
            self.list.clear()
            for rec in records:
                item = QListWidgetItem(self._row_text(rec))
                item.setData(Qt.UserRole, getattr(rec, "identity", None))
                contents = (getattr(rec, "contents", "") or "").strip()
                if contents:
                    item.setToolTip(contents)
                self.list.addItem(item)
                if selected is not None \
                        and getattr(rec, "identity", None) == selected:
                    self.list.setCurrentItem(item)
        finally:
            self._refreshing = False
        n = self.list.count()
        self.count_label.setText(
            "No comments" if n == 0
            else f"{n} comment{'s' if n != 1 else ''}")
        self._sync_enablement()

    def selected_identity(self):
        """The selected row's annot identity, or None."""
        item = self.list.currentItem()
        return None if item is None else item.data(Qt.UserRole)

    # --- rows ----------------------------------------------------------------
    @staticmethod
    def _sort_key(rec) -> tuple:
        identity = getattr(rec, "identity", None) or (0,)
        rect = getattr(rec, "display_rect", None) or (0.0, 0.0, 0.0, 0.0)
        return (identity[0], rect[1], rect[0])

    @staticmethod
    def _row_text(rec) -> str:
        kind = getattr(rec, "kind", "") or ""
        identity = getattr(rec, "identity", None) or (0,)
        glyph = _KIND_GLYPHS.get(kind, "•")
        snippet = " ".join(
            (getattr(rec, "contents", "") or "").split())
        if len(snippet) > _SNIPPET_LEN:
            snippet = snippet[: _SNIPPET_LEN - 1] + "…"
        if not snippet:
            snippet = _KIND_LABELS.get(kind, kind.capitalize() or "Annotation")
        text = f"{glyph}  p.{identity[0] + 1}  {snippet}"
        if getattr(rec, "is_existing", False):
            text += "  (file)"
        return text

    # --- interaction -----------------------------------------------------------
    def _on_item_clicked(self, item: QListWidgetItem) -> None:
        if self._refreshing or item is None:
            return
        try:
            self._jump(item.data(Qt.UserRole))
        except Exception:  # noqa: BLE001 - a jump failure must not crash chrome
            pass

    def _on_item_double_clicked(self, item: QListWidgetItem) -> None:
        if self._refreshing or item is None:
            return
        try:
            self._edit(item.data(Qt.UserRole))
        except Exception:  # noqa: BLE001 - an edit failure must not crash chrome
            pass

    def _on_edit_clicked(self) -> None:
        ident = self.selected_identity()
        if ident is None:
            return
        try:
            self._edit(ident)
        except Exception:  # noqa: BLE001
            pass

    def _on_delete_clicked(self) -> None:
        ident = self.selected_identity()
        if ident is None:
            return
        try:
            self._delete(ident)
        except Exception:  # noqa: BLE001
            pass

    # --- chrome ------------------------------------------------------------
    def _sync_enablement(self) -> None:
        has_row = self.list.currentItem() is not None
        self.edit_button.setEnabled(has_row)
        self.delete_button.setEnabled(has_row)
