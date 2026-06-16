"""Help-menu chrome (navigation workstream M2): the generated keyboard-
shortcuts cheatsheet and the About dialog.

``ShortcutsDialog`` is built FROM THE LIVE MENUBAR: it walks every menu
recursively and lists each action that carries a non-empty shortcut, grouped
by top-level menu. Because the rows are generated (never hand-written), the
sheet self-updates as workstreams add actions at their menu anchors -- that
is the point. Both dialogs are NON-MODAL by construction (``show()``, never
``exec()`` -- the offscreen-test rule), pure chrome (zero model imports;
the About icon comes through an injected factory), and cached on the window.

PySide 6.11 hazard (probe-verified by the perf-foundation menubar census):
a recursive menu walk that lets the intermediate QMenu/QAction wrappers die
mid-walk lets shiboken/gc DELETE the live C++ menus and their separator
actions out from under the window. ``walk_shortcut_actions`` keeps every
wrapper alive in a local list for the duration of the walk -- the
verified-safe pattern.

Object names (``ShortcutsDialog``, ``AboutDialog``) are load-bearing for
tests + QSS.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import (
    QDialog,
    QGridLayout,
    QLabel,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from . import theme


def display_name(text: str) -> str:
    """An action's text with mnemonic markers stripped: ``&&`` is the
    escaped literal ampersand (kept), a single ``&`` is a mnemonic marker
    (dropped) -- the Qt convention."""
    return text.replace("&&", "\x00").replace("&", "").replace("\x00", "&")


def walk_shortcut_actions(menubar) -> list:
    """Every shortcut-bearing action reachable from ``menubar``, depth-first,
    as ``(group_title, action)`` pairs grouped by top-level menu. Each action
    appears once (the first menu home wins); separators and submenu headers
    are skipped. Uses the keep-alive pattern (module docstring)."""
    keep: list = []
    seen: set = set()
    out: list = []

    def walk(group: str, menu) -> None:
        keep.append(menu)
        for act in menu.actions():
            keep.append(act)
            sub = act.menu()
            if sub is not None:
                walk(group, sub)
                continue
            if act.isSeparator() or id(act) in seen:
                continue
            if not act.shortcut().isEmpty():
                seen.add(id(act))
                out.append((group, act))

    for top in menubar.actions():
        keep.append(top)
        sub = top.menu()
        if sub is not None:
            walk(display_name(top.text()), sub)
    return out


class ShortcutsDialog(QDialog):
    """Help > Keyboard Shortcuts… (Cmd+/): the generated cheatsheet.

    ``refresh()`` rebuilds the rows from the menubar, so a cached instance
    stays current across later menu additions. ``self.rows`` keeps the
    ``(group, name, keys)`` snapshot the harness asserts against."""

    def __init__(self, menubar, parent=None):
        super().__init__(parent)
        self.setObjectName("ShortcutsDialog")
        self.setWindowTitle("Keyboard Shortcuts")
        self.setModal(False)            # offscreen-test rule: never exec()
        self._menubar = menubar
        self.rows: list[tuple[str, str, str]] = []

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setObjectName("ShortcutsScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(scroll.Shape.NoFrame)
        outer.addWidget(scroll)
        self._scroll = scroll
        self.resize(440, 560)
        self.refresh()

    def refresh(self) -> None:
        """Rebuild the two-column grid (name | shortcut) from the LIVE
        menubar, grouped under one header label per top-level menu."""
        self.rows = []
        body = QWidget()
        grid = QGridLayout(body)
        grid.setContentsMargins(20, 14, 20, 14)
        grid.setHorizontalSpacing(28)
        grid.setVerticalSpacing(5)
        row = 0
        last_group = None
        for group, act in walk_shortcut_actions(self._menubar):
            if group != last_group:
                header = QLabel(group)
                header.setObjectName("ShortcutsGroupHeader")
                header.setFont(theme.ui_font(12, semibold=True))
                header.setStyleSheet(f"color: {theme.PANEL_HEADER};")
                top_pad = 12 if last_group is not None else 0
                header.setContentsMargins(0, top_pad, 0, 2)
                grid.addWidget(header, row, 0, 1, 2)
                row += 1
                last_group = group
            # Stateful toggles (Show <-> Hide ...) carry a stable cheatsheet
            # name via the "cheatsheet_label" property, so the sheet never
            # bakes the CURRENT toggle state into a reference document.
            stable = act.property("cheatsheet_label")
            name = display_name(stable if stable else act.text())
            keys = act.shortcut().toString(QKeySequence.NativeText)
            name_label = QLabel(name)
            name_label.setObjectName("ShortcutsName")
            name_label.setFont(theme.ui_font(13))
            keys_label = QLabel(keys)
            keys_label.setObjectName("ShortcutsKeys")
            keys_label.setFont(theme.mono_font())
            keys_label.setStyleSheet(f"color: {theme.TEXT_SECONDARY};")
            keys_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            grid.addWidget(name_label, row, 0)
            grid.addWidget(keys_label, row, 1)
            row += 1
            self.rows.append((group, name, keys))
        grid.setRowStretch(row, 1)
        # setWidget owns + deletes the previous body, so refresh leaks nothing.
        self._scroll.setWidget(body)


class AboutDialog(QDialog):
    """Help > About PDF Text Editor: name, version, one-line promise.
    Local-only wording on purpose -- no links, no telemetry, no cloud."""

    def __init__(self, version: str, icon_factory=None, parent=None):
        super().__init__(parent)
        self.setObjectName("AboutDialog")
        self.setWindowTitle("About PDF Text Editor")
        self.setModal(False)            # offscreen-test rule: never exec()

        col = QVBoxLayout(self)
        col.setContentsMargins(36, 28, 36, 28)
        col.setSpacing(6)
        col.setAlignment(Qt.AlignHCenter)
        if icon_factory is not None:
            icon = icon_factory("doc")
            pm = icon.pixmap(48, 48)
            if not pm.isNull():
                icon_label = QLabel()
                icon_label.setObjectName("AboutIcon")
                icon_label.setPixmap(pm)
                icon_label.setAlignment(Qt.AlignHCenter)
                col.addWidget(icon_label)
        name_label = QLabel("PDF Text Editor")
        name_label.setObjectName("AboutName")
        name_label.setFont(theme.ui_font(16, semibold=True))
        name_label.setAlignment(Qt.AlignHCenter)
        col.addWidget(name_label)
        self.version_label = QLabel(f"Version {version}")
        self.version_label.setObjectName("AboutVersion")
        self.version_label.setFont(theme.ui_font(12))
        self.version_label.setStyleSheet(f"color: {theme.TEXT_SECONDARY};")
        self.version_label.setAlignment(Qt.AlignHCenter)
        col.addWidget(self.version_label)
        tagline = QLabel("Local, private PDF editing.")
        tagline.setObjectName("AboutTagline")
        tagline.setFont(theme.ui_font(12))
        tagline.setStyleSheet(f"color: {theme.TEXT_SECONDARY};")
        tagline.setAlignment(Qt.AlignHCenter)
        col.addWidget(tagline)
        self.setFixedSize(self.sizeHint().expandedTo(self.minimumSizeHint()))
