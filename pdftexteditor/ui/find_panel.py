"""Find & Replace panel (REFLOW_SPEC §R5.1).

A slim search/replace bar shown in the LEFT panel when the Find tool is active.
It is PURE CHROME: it never touches the model or the undo stack directly. The
window injects three callables at construction --

  * ``search(query, match_case, whole_word) -> list[Match]``  (document.find_all)
  * ``navigate(match)``                                       (scroll + select)
  * ``replace(match, replacement) -> Match | None``           (one undo step)
  * ``replace_all(query, replacement, match_case, whole_word) -> int``

-- so all model/undo coupling lives in the window. The panel owns only the query
state, the current result list, and the active match index.

Search is cross-page: a query is run against the CURRENT staged text of every
box (spans + new boxes, where a span may be a multi-line ``ParagraphBox``), so a
match can span a paragraph's soft line breaks. Replace / Replace All flow through
the window's normal text-edit command route, so each is undoable and rewraps
paragraphs through the bake (WYSIWYG preserved).

Object names are load-bearing for tests + QSS.
"""

from __future__ import annotations

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import (
    QCheckBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from . import theme


class FindReplacePanel(QWidget):
    """Cross-page Find & Replace, staged through the window's edit pipeline."""

    # Emitted when the panel is closed (Esc / the close button), so the window
    # can return the tool strip to Select.
    closed = Signal()

    def __init__(self, search, navigate, replace, replace_all, parent=None,
                 icon_factory=None):
        super().__init__(parent)
        self.setObjectName("FindReplacePanel")
        self._search = search
        self._navigate = navigate
        self._replace = replace
        self._replace_all = replace_all
        # Optional painted-icon factory (the window's make_icon). When provided,
        # the prev/next match buttons use the SAME chevron icons as the toolbar
        # nav, so there is ONE chevron language across the app instead of two
        # different glyph treatments (review minor). Falls back to the unicode
        # glyphs when not injected (e.g. a standalone panel in a test).
        self._icon_factory = icon_factory

        self._matches: list = []
        self._index = -1            # active match in self._matches, -1 == none
        self._last_query = ""
        self._last_flags = (False, False)

        self._build()

    # --- construction ----------------------------------------------------
    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(12, 10, 12, 10)
        outer.setSpacing(8)

        # Header row: title + close button.
        head = QHBoxLayout()
        head.setContentsMargins(0, 0, 0, 0)
        head.setSpacing(6)
        title = QLabel("Find & Replace")
        title.setObjectName("FindPanelTitle")
        title.setFont(theme.ui_font(11, semibold=True))
        head.addWidget(title)
        head.addStretch(1)
        self.close_button = QToolButton()
        self.close_button.setObjectName("FindClose")
        self.close_button.setText("✕")
        self.close_button.setCursor(Qt.PointingHandCursor)
        self.close_button.setToolTip("Close (Esc)")
        self.close_button.clicked.connect(self.close_panel)
        head.addWidget(self.close_button)
        outer.addLayout(head)

        # Find field.
        self.find_edit = QLineEdit()
        self.find_edit.setObjectName("FindField")
        self.find_edit.setPlaceholderText("Find")
        self.find_edit.setClearButtonEnabled(True)
        self.find_edit.setFont(theme.ui_font(13))
        self.find_edit.textChanged.connect(self._on_query_changed)
        self.find_edit.returnPressed.connect(self.find_next)
        outer.addWidget(self.find_edit)

        # Replace field.
        self.replace_edit = QLineEdit()
        self.replace_edit.setObjectName("ReplaceField")
        self.replace_edit.setPlaceholderText("Replace with")
        self.replace_edit.setClearButtonEnabled(True)
        self.replace_edit.setFont(theme.ui_font(13))
        self.replace_edit.returnPressed.connect(self.replace_current)
        outer.addWidget(self.replace_edit)

        # Options row: Match case / Whole word.
        opts = QHBoxLayout()
        opts.setContentsMargins(0, 0, 0, 0)
        opts.setSpacing(12)
        self.case_check = QCheckBox("Match case")
        self.case_check.setObjectName("FindMatchCase")
        self.case_check.setFont(theme.ui_font(12))
        self.case_check.toggled.connect(self._on_options_changed)
        self.word_check = QCheckBox("Whole word")
        self.word_check.setObjectName("FindWholeWord")
        self.word_check.setFont(theme.ui_font(12))
        self.word_check.toggled.connect(self._on_options_changed)
        opts.addWidget(self.case_check)
        opts.addWidget(self.word_check)
        opts.addStretch(1)
        outer.addLayout(opts)

        # Result count + prev/next nav.
        nav = QHBoxLayout()
        nav.setContentsMargins(0, 0, 0, 0)
        nav.setSpacing(6)
        self.count_label = QLabel("")
        self.count_label.setObjectName("FindCount")
        self.count_label.setFont(theme.ui_font(12))
        self.count_label.setStyleSheet(f"color: {theme.TEXT_SECONDARY};")
        nav.addWidget(self.count_label)
        nav.addStretch(1)
        self.prev_button = QToolButton()
        self.prev_button.setObjectName("FindPrev")
        self.prev_button.setToolTip("Previous match (Shift+Enter)")
        self.prev_button.setCursor(Qt.PointingHandCursor)
        self.prev_button.clicked.connect(self.find_prev)
        self.next_button = QToolButton()
        self.next_button.setObjectName("FindNext")
        self.next_button.setToolTip("Next match (Enter)")
        self.next_button.setCursor(Qt.PointingHandCursor)
        self.next_button.clicked.connect(self.find_next)
        # Use the toolbar's painted chevrons when the icon factory is injected, so
        # the find-nav matches the toolbar prev/next in weight + shape; otherwise
        # fall back to the unicode glyphs.
        if self._icon_factory is not None:
            self.prev_button.setIcon(self._icon_factory("prev"))
            self.next_button.setIcon(self._icon_factory("next"))
            self.prev_button.setIconSize(QSize(theme.ICON_SIZE, theme.ICON_SIZE))
            self.next_button.setIconSize(QSize(theme.ICON_SIZE, theme.ICON_SIZE))
        else:
            self.prev_button.setText("‹")
            self.next_button.setText("›")
        nav.addWidget(self.prev_button)
        nav.addWidget(self.next_button)
        outer.addLayout(nav)

        # Replace actions.
        actions = QHBoxLayout()
        actions.setContentsMargins(0, 0, 0, 0)
        actions.setSpacing(8)
        self.replace_button = QPushButton("Replace")
        self.replace_button.setObjectName("FindReplace")
        self.replace_button.setCursor(Qt.PointingHandCursor)
        self.replace_button.clicked.connect(self.replace_current)
        self.replace_all_button = QPushButton("Replace All")
        self.replace_all_button.setObjectName("FindReplaceAll")
        self.replace_all_button.setCursor(Qt.PointingHandCursor)
        self.replace_all_button.clicked.connect(self.replace_all)
        actions.addWidget(self.replace_button)
        actions.addWidget(self.replace_all_button)
        outer.addLayout(actions)

        outer.addStretch(1)
        self._sync_enablement()

    # --- public API ------------------------------------------------------
    def focus_find(self) -> None:
        """Give the find field focus + select its text (Cmd+F entry point)."""
        self.find_edit.setFocus(Qt.ShortcutFocusReason)
        self.find_edit.selectAll()

    def set_query(self, text: str) -> None:
        """Seed the find field (e.g. from the current selection)."""
        if text and text != self.find_edit.text():
            self.find_edit.setText(text)

    def refresh(self) -> None:
        """Re-run the current search (after a document change / page op) and
        clamp the active index. Keeps the result count honest without moving the
        view."""
        self._run_search(navigate=False, keep_index=True)

    def close_panel(self) -> None:
        self.clear_results()
        self.closed.emit()

    def clear_results(self) -> None:
        self._matches = []
        self._index = -1
        self._update_count()
        self._sync_enablement()

    # --- search ----------------------------------------------------------
    def _on_query_changed(self, _text: str) -> None:
        self._run_search(navigate=False)

    def _on_options_changed(self, _checked: bool) -> None:
        self._run_search(navigate=False)

    def _flags(self) -> tuple[bool, bool]:
        return (self.case_check.isChecked(), self.word_check.isChecked())

    def _run_search(self, *, navigate: bool, keep_index: bool = False) -> None:
        query = self.find_edit.text()
        flags = self._flags()
        prev_index = self._index
        self._last_query = query
        self._last_flags = flags
        if not query:
            self.clear_results()
            return
        try:
            self._matches = list(self._search(query, flags[0], flags[1]))
        except Exception:  # noqa: BLE001 - a search failure must not crash chrome
            self._matches = []
        if not self._matches:
            self._index = -1
        elif keep_index and 0 <= prev_index < len(self._matches):
            self._index = prev_index
        else:
            self._index = 0
        self._update_count()
        self._sync_enablement()
        if navigate and self._index >= 0:
            self._go_to(self._index)

    # --- navigation ------------------------------------------------------
    def find_next(self) -> None:
        if not self._ensure_results():
            return
        self._index = (self._index + 1) % len(self._matches)
        self._go_to(self._index)

    def find_prev(self) -> None:
        if not self._ensure_results():
            return
        self._index = (self._index - 1) % len(self._matches)
        self._go_to(self._index)

    def _ensure_results(self) -> bool:
        """Make sure a fresh result list exists before navigating; returns False
        when there is nothing to navigate."""
        if (self.find_edit.text() != self._last_query
                or self._flags() != self._last_flags
                or not self._matches):
            self._run_search(navigate=False)
        return bool(self._matches)

    def _go_to(self, index: int) -> None:
        if not (0 <= index < len(self._matches)):
            return
        self._update_count()
        try:
            self._navigate(self._matches[index])
        except Exception:  # noqa: BLE001 - navigation must not crash chrome
            pass

    # --- replace ---------------------------------------------------------
    def replace_current(self) -> None:
        """Replace the ACTIVE match, then advance to the next one. The window's
        ``replace`` callback stages one undoable text edit and returns a refreshed
        result list position (we just re-search to stay consistent)."""
        if not self._ensure_results():
            return
        if not (0 <= self._index < len(self._matches)):
            return
        match = self._matches[self._index]
        replacement = self.replace_edit.text()
        try:
            self._replace(match, replacement)
        except Exception:  # noqa: BLE001 - a replace failure must not crash chrome
            pass
        # The text changed; re-run the search and land on the same ordinal so the
        # next Replace targets the following occurrence.
        target = self._index
        self._run_search(navigate=False)
        if self._matches:
            self._index = min(target, len(self._matches) - 1)
            self._go_to(self._index)
        else:
            self._update_count()
            self._sync_enablement()

    def replace_all(self) -> None:
        query = self.find_edit.text()
        if not query:
            return
        replacement = self.replace_edit.text()
        flags = self._flags()
        try:
            n = self._replace_all(query, replacement, flags[0], flags[1])
        except Exception:  # noqa: BLE001 - a replace-all failure must not crash chrome
            n = 0
        self._run_search(navigate=False)
        if n:
            self.count_label.setText(
                f"Replaced {n} occurrence{'s' if n != 1 else ''}")
        self._sync_enablement()

    # --- chrome ----------------------------------------------------------
    def _update_count(self) -> None:
        total = len(self._matches)
        if not self.find_edit.text():
            self.count_label.setText("")
        elif total == 0:
            self.count_label.setText("No results")
        elif self._index >= 0:
            self.count_label.setText(f"{self._index + 1} of {total}")
        else:
            self.count_label.setText(f"{total} found")

    def _sync_enablement(self) -> None:
        has = bool(self._matches)
        has_query = bool(self.find_edit.text())
        self.prev_button.setEnabled(has)
        self.next_button.setEnabled(has)
        self.replace_button.setEnabled(has)
        self.replace_all_button.setEnabled(has_query)

    # --- keyboard --------------------------------------------------------
    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() == Qt.Key_Escape:
            self.close_panel()
            return
        if event.key() in (Qt.Key_Return, Qt.Key_Enter):
            if event.modifiers() & Qt.ShiftModifier:
                self.find_prev()
            else:
                self.find_next()
            return
        super().keyPressEvent(event)
