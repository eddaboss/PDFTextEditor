"""Bookmarks panel (navigation workstream M1).

The left column's FOURTH stacked page (after Format, Find & Replace, and
Comments): the document outline as a tree, with jump-to, Add, Rename, and
Delete. It is PURE CHROME, built exactly like ``FindReplacePanel`` /
``CommentsPanel``: the window injects four callables at construction --

  * ``get_outline() -> list[[level, title, page1], ...]``  (document.outline)
  * ``set_outline(entries)``    (ONE StructuralCommand via _run_structural)
  * ``jump_to_page(page0)``     (the window's _set_view_page)
  * ``current_page() -> int``   (the window's view_page_index)

-- so all model/undo coupling lives in the window and this module imports
zero model code. The window calls ``refresh()`` after every structural op,
tab switch, and open (it rides ``_sync_all`` like the Comments panel), so
the tree always mirrors ``working``'s live outline.

Every mutation (Add / Rename / Delete) commits by flattening the tree
depth-first into ``[[depth + 1, title, page], ...]`` -- tree depth makes the
hierarchy ALWAYS valid for ``set_toc`` -- and calling ``set_outline``, so
one gesture is one structural undo step. Entries whose target page was
deleted (``page == -1``) render dimmed with an explanatory tooltip and do
not jump. No modal dialogs anywhere (the offscreen-test rule); renames use
the tree's inline ``editItem`` editor.

Object names are load-bearing for tests + QSS.
"""

from __future__ import annotations

from PySide6.QtCore import QSize, Qt, QTimer
from PySide6.QtGui import QBrush, QColor, QIcon
from PySide6.QtWidgets import (
    QHBoxLayout,
    QToolButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from . import theme

_DANGLING_TIP = "Target page was deleted"


class BookmarkPanel(QWidget):
    """The document outline, listed and editable (navigation M1)."""

    def __init__(self, get_outline, set_outline, jump_to_page, current_page,
                 parent=None, icon_factory=None):
        super().__init__(parent)
        self.setObjectName("BookmarkPanel")
        self._get_outline = get_outline
        self._set_outline = set_outline
        self._jump_to_page = jump_to_page
        self._current_page = current_page
        self._icon_factory = icon_factory
        # True while refresh()/internal item writes run, so itemChanged from
        # our own mutations never re-commits (the CommentsPanel guard).
        self._refreshing = False
        # Rename commits are DEFERRED one event-loop turn: the commit's
        # refresh rebuilds the tree, and doing that synchronously inside
        # itemChanged (emitted mid-``setModelData`` by the inline editor)
        # would destroy the emitting item under the delegate's feet.
        self._commit_pending = False

        self._build()

    # --- construction ----------------------------------------------------
    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(12, 10, 12, 10)
        outer.setSpacing(8)

        self.tree = QTreeWidget()
        self.tree.setObjectName("BookmarkTree")
        self.tree.setHeaderHidden(True)
        self.tree.setColumnCount(1)
        self.tree.setFont(theme.ui_font(12))
        self.tree.setIndentation(14)
        self.tree.itemClicked.connect(self._on_item_clicked)
        # Return / double-click both activate (the spec's Return-to-jump).
        self.tree.itemActivated.connect(self._on_item_clicked)
        self.tree.itemChanged.connect(self._on_item_changed)
        self.tree.currentItemChanged.connect(
            lambda *_: self._sync_enablement())
        outer.addWidget(self.tree, 1)

        # Footer row: Add / Rename / Delete as flat icon buttons.
        footer = QHBoxLayout()
        footer.setContentsMargins(0, 0, 0, 0)
        footer.setSpacing(4)
        self.add_button = self._footer_button(
            "BookmarkAdd", "bookmark_add", "Add bookmark for this page",
            self._on_add_clicked)
        self.rename_button = self._footer_button(
            "BookmarkRename", "text_edit", "Rename bookmark",
            self._on_rename_clicked)
        self.delete_button = self._footer_button(
            "BookmarkDelete", "delete", "Delete bookmark",
            self._on_delete_clicked)
        footer.addWidget(self.add_button)
        footer.addWidget(self.rename_button)
        footer.addWidget(self.delete_button)
        footer.addStretch(1)
        outer.addLayout(footer)

        self._sync_enablement()

    def _footer_button(self, object_name: str, icon_name: str, tip: str,
                       slot) -> QToolButton:
        btn = QToolButton()
        btn.setObjectName(object_name)
        btn.setAutoRaise(True)
        btn.setCursor(Qt.PointingHandCursor)
        btn.setToolTip(tip)
        btn.setIconSize(QSize(theme.ICON_SIZE, theme.ICON_SIZE))
        icon = self._icon_factory(icon_name) if self._icon_factory else QIcon()
        btn.setIcon(icon)
        btn.clicked.connect(slot)
        return btn

    # --- public API --------------------------------------------------------
    def refresh(self) -> None:
        """Rebuild the tree from ``get_outline()``, nested by entry level,
        expanded, preserving the selection by (title, page) best effort. The
        window calls this after every structural op (so undo/redo of bookmark
        + page ops update the tree), tab switch, and open; a model read
        failure must never crash chrome."""
        try:
            entries = list(self._get_outline() or [])
        except Exception:  # noqa: BLE001 - a model read must not crash chrome
            entries = []
        selected = self.selected_ref()
        self._refreshing = True
        try:
            self.tree.clear()
            # Stack of (level, item) for nesting: an entry at level L parents
            # under the nearest stack item with level L-1. set_toc guarantees
            # levels step by at most +1, but stay defensive on file outlines.
            stack: list[tuple[int, QTreeWidgetItem]] = []
            restore = None
            for entry in entries:
                try:
                    level, title, page = int(entry[0]), str(entry[1]), \
                        int(entry[2])
                except (TypeError, ValueError, IndexError):
                    continue
                while stack and stack[-1][0] >= level:
                    stack.pop()
                item = QTreeWidgetItem([title])
                item.setFlags(item.flags() | Qt.ItemIsEditable)
                item.setData(0, Qt.UserRole, (title, page))
                if page == -1:
                    item.setForeground(
                        0, QBrush(QColor(theme.TEXT_SECONDARY)))
                    item.setToolTip(0, _DANGLING_TIP)
                if stack:
                    stack[-1][1].addChild(item)
                else:
                    self.tree.addTopLevelItem(item)
                stack.append((level, item))
                if restore is None and selected is not None \
                        and (title, page) == selected:
                    restore = item
            self.tree.expandAll()
            if restore is not None:
                self.tree.setCurrentItem(restore)
        finally:
            self._refreshing = False
        self._sync_enablement()

    def selected_ref(self):
        """The selected row's ``(title, page1)``, or None."""
        item = self.tree.currentItem()
        return None if item is None else item.data(0, Qt.UserRole)

    # --- interaction -----------------------------------------------------
    def _on_item_clicked(self, item: QTreeWidgetItem, _col: int = 0) -> None:
        if self._refreshing or item is None:
            return
        ref = item.data(0, Qt.UserRole)
        if not ref or ref[1] == -1:
            return                      # dangling: click is a no-op
        try:
            self._jump_to_page(ref[1] - 1)
        except Exception:  # noqa: BLE001 - a jump failure must not crash chrome
            pass

    def _on_item_changed(self, item: QTreeWidgetItem, _col: int) -> None:
        """An inline rename committed (the ``editItem`` editor closed). Empty
        titles revert in place -- the model rejects them, and a flash per
        keystroke would be noise -- everything else commits ONE outline."""
        if self._refreshing or item is None:
            return
        ref = item.data(0, Qt.UserRole) or ("", -1)
        title = item.text(0).strip()
        if not title:
            self._refreshing = True
            try:
                item.setText(0, ref[0])
            finally:
                self._refreshing = False
            return
        if title == ref[0]:
            return                      # unchanged: no command
        self._refreshing = True
        try:
            item.setData(0, Qt.UserRole, (title, ref[1]))
        finally:
            self._refreshing = False
        self._queue_commit()

    def _on_add_clicked(self) -> None:
        """Add a bookmark for the CURRENT page as a sibling after the selected
        item (or appended at top level), commit (one structural command), then
        start the inline rename on the re-materialized row -- the commit
        refresh rebuilds the tree, so the editor opens on the new item."""
        try:
            page1 = int(self._current_page()) + 1
        except Exception:  # noqa: BLE001
            page1 = 1
        title = f"Page {page1}"
        item = QTreeWidgetItem([title])
        item.setFlags(item.flags() | Qt.ItemIsEditable)
        item.setData(0, Qt.UserRole, (title, page1))
        self._refreshing = True
        try:
            cur = self.tree.currentItem()
            if cur is not None:
                parent = cur.parent()
                if parent is not None:
                    parent.insertChild(parent.indexOfChild(cur) + 1, item)
                else:
                    self.tree.insertTopLevelItem(
                        self.tree.indexOfTopLevelItem(cur) + 1, item)
            else:
                self.tree.addTopLevelItem(item)
            self.tree.setCurrentItem(item)
        finally:
            self._refreshing = False
        self._commit()
        # The commit refreshed the tree (selection preserved by (title, page));
        # open the inline rename on the new row.
        fresh = self.tree.currentItem()
        if fresh is not None and fresh.data(0, Qt.UserRole) == (title, page1):
            self.tree.editItem(fresh, 0)

    def _on_rename_clicked(self) -> None:
        item = self.tree.currentItem()
        if item is not None:
            self.tree.editItem(item, 0)

    def _on_delete_clicked(self) -> None:
        """Remove the selected item AND its subtree, then commit."""
        item = self.tree.currentItem()
        if item is None:
            return
        self._refreshing = True
        try:
            parent = item.parent()
            if parent is not None:
                parent.removeChild(item)
            else:
                self.tree.takeTopLevelItem(
                    self.tree.indexOfTopLevelItem(item))
        finally:
            self._refreshing = False
        self._commit()

    # --- commit ------------------------------------------------------------
    def _queue_commit(self) -> None:
        """Commit on the NEXT event-loop turn (rename path only -- see the
        ``_commit_pending`` note in __init__); coalesces bursts to one."""
        if self._commit_pending:
            return
        self._commit_pending = True
        QTimer.singleShot(0, self._run_queued_commit)

    def _run_queued_commit(self) -> None:
        self._commit_pending = False
        self._commit()

    def _commit(self) -> None:
        """Flatten the tree depth-first into ``[[depth + 1, title, page], ...]``
        (tree depth keeps the hierarchy always valid for ``set_toc``) and hand
        it to the window's ``set_outline`` -- ONE structural command. The
        window's refresh wiring rebuilds this tree synchronously afterwards.
        A flatten that matches the live outline is a no-op (no phantom undo
        entry, e.g. a rename that landed back on the old title)."""
        entries: list[list] = []

        def walk(item: QTreeWidgetItem, depth: int) -> None:
            ref = item.data(0, Qt.UserRole) or (item.text(0), -1)
            entries.append([depth + 1, item.text(0), ref[1]])
            for k in range(item.childCount()):
                walk(item.child(k), depth + 1)

        for i in range(self.tree.topLevelItemCount()):
            walk(self.tree.topLevelItem(i), 0)
        try:
            if entries == list(self._get_outline() or []):
                return
        except Exception:  # noqa: BLE001 - fall through to the real commit
            pass
        try:
            self._set_outline(entries)
        except Exception:  # noqa: BLE001 - the window flashes its own errors
            pass

    # --- chrome ------------------------------------------------------------
    def _sync_enablement(self) -> None:
        has_row = self.tree.currentItem() is not None
        self.rename_button.setEnabled(has_row)
        self.delete_button.setEnabled(has_row)
