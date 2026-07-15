"""The LEFT chrome: a vertical ACTIVITY RAIL + the contextual panel for the
current selection (Charcoal Studio redesign; REFLOW_SPEC §R4.1).

This is pure chrome. The far-left rail is a column of checkable mode buttons in
an exclusive group (Select / Text / Edit / Add / Find / Notes / Outline), each an
icon over a tiny caps label. Selecting one emits ``toolSelected(name)`` and the
window maps it to the view's mode calls (the same ``enter_add_text_mode`` /
``exit_add_text_mode`` it already drives). To the right of the rail sits the
contextual panel: a header that names the active mode over a stack holding the
Format/Inspector panel (default) and the Find / Comments / Bookmarks panels.

The rail is its own darker surface (objectName ``ActivityRail``) so it reads as a
distinct mode switcher, not part of the panel. The icon factory is injected
(``icon_factory(name) -> QIcon``) so this module never imports ``main_window``
(which imports this one).
"""

from __future__ import annotations

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (
    QButtonGroup,
    QFrame,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QStackedWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from . import theme


# Rail MODE buttons in display order, each (id, icon_name, short label, tooltip).
# These are an exclusive checkable group; selecting one emits ``toolSelected``.
# "select" is the default; "text_edit" arms in-place text editing; "add_text"
# arms the add tool; "markup" opens the markup-tools palette; "comments" opens
# the Comments panel (annotations & markup §5.4); "bookmarks" opens the
# Bookmarks panel (navigation M1); "find" opens Find & Replace. The Select-Text
# tool (copy words off the page, ws2 M4) stays reachable via its 'S' shortcut +
# the Tools menu, so it is no longer a rail button.
# Every rail CATEGORY across all three modes, keyed by id -> (icon, label, tip).
# The rail shows only the current mode's subset (``_MODE_TOOLS``); selecting one
# emits ``toolSelected`` and swaps the panel to what that category does
# (``_TOOL_PANEL``). Edit keeps the original document tools; Form Fields and
# Fill Form each get their own category set (three-mode adaptive rail).
_CATEGORIES = {
    "select": ("select", "Select", "Select (V)"),
    "text_edit": ("text_edit", "Text", "Text Edit (E)"),
    "markup": ("highlight", "Markup", "Markup tools"),
    "comments": ("comments", "Notes", "Comments (Cmd+Shift+C)"),
    "bookmarks": ("bookmark", "Outline", "Bookmarks (Cmd+Alt+B)"),
    "find": ("find", "Find", "Find & Replace (Cmd+F)"),
    # Form Fields (form making) -- one unified builder surface
    "field_build": ("field_add", "Build", "Build the form's fields"),
    # Fill Form (filling + signing)
    "fill_fields": ("mode_fill_form", "Fill", "Fill in the form's fields"),
    "fill_sign": ("signature", "Sign", "Sign the document"),
    "fill_image": ("image", "Image", "Place an image"),
    "fill_flatten": ("flatten", "Finish", "Export a flattened copy"),
}
# Which categories show in each top mode, in display order.
_MODE_TOOLS = {
    "document": ("select", "text_edit", "markup", "comments", "bookmarks", "find"),
    "form_fields": ("field_build",),
    "fill_form": ("fill_fields", "fill_sign", "fill_image", "fill_flatten"),
}
# The category auto-selected when a mode becomes active.
_MODE_DEFAULT_TOOL = {
    "document": "select", "form_fields": "field_build", "fill_form": "fill_fields",
}
# Category -> (panel key, header title). Categories NOT listed here (select,
# text_edit) fall back to the Format panel. The window installs each keyed panel
# via ``install_named_panel``; the built-in ones keep their dedicated slots.
_TOOL_PANEL = {
    "markup": ("markup", "Markup"),
    "find": ("find", "Find & Replace"),
    "comments": ("comments", "Comments"),
    "bookmarks": ("bookmarks", "Bookmarks"),
    "field_build": ("forms_build", "Form fields"),
    "fill_fields": ("fill_fields", "Fill the form"),
    "fill_sign": ("fill_sign", "Signature"),
    "fill_image": ("fill_image", "Insert image"),
    "fill_flatten": ("fill_flatten", "Finish"),
}

# The TOP-LEVEL editing modes, shown as a distinct button cluster at the very top
# of the rail (their own exclusive group + ``topModeSelected``). "Edit" is the
# Document/default mode (selected on open); the two form modes carry icons. Ids
# match page_view's TOP_DOCUMENT/TOP_FORM_FIELDS/TOP_FILL_FORM strings, so the
# window can route them straight to its existing _on_top_mode_selected slot.
_MODES = (
    ("document", "mode_edit", "Edit", "Edit the document (Ctrl+1)"),
    ("form_fields", "mode_form_fields", "Fields", "Form Fields (Ctrl+2)"),
    ("fill_form", "mode_fill_form", "Fill", "Fill in the form (Ctrl+3)"),
)

# Rail ACTION buttons, pinned at the bottom below a divider. Unlike modes, these
# fire a one-shot command (not a sticky mode), so they are momentary buttons that
# emit ``actionRequested(name)`` instead of joining the exclusive group.
_RAIL_ACTIONS = (
    ("image", "image", "Image", "Insert image (Cmd+Shift+I)"),
    ("signature", "signature", "Sign", "Signature (Cmd+Shift+G)"),
)


class LeftPanel(QWidget):
    """A vertical tool strip on top of the Format/Inspector panel."""

    # "select"|"text_edit"|"add_text"|"markup"|"comments"|"bookmarks"|"find"
    toolSelected = Signal(str)
    # A one-shot rail action ("image"|"signature"); the window triggers the action.
    actionRequested = Signal(str)
    # Top mode picked in the rail ("document"|"form_fields"|"fill_form"); the
    # window routes it to the same _on_top_mode_selected the old menu items used.
    topModeSelected = Signal(str)

    def __init__(self, inspector, icon_factory, parent=None):
        super().__init__(parent)
        self.setObjectName("LeftPanel")
        self.setSizePolicy(QSizePolicy.Policy.Preferred,
                           QSizePolicy.Policy.Expanding)
        self._inspector = inspector
        self._icon_factory = icon_factory
        self._buttons: dict[str, QToolButton] = {}
        self._syncing = False

        # The whole left chrome is two columns: a vertical activity RAIL on the
        # far left, and the contextual content column (header + stack) to its
        # right. The rail is a distinct darker surface so it reads as a mode
        # switcher rather than part of the panel.
        outer = QHBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # --- Activity rail (a vertical column of mode buttons) -----------
        rail = QWidget()
        rail.setObjectName("ActivityRail")
        rail.setFixedWidth(theme.RAIL_WIDTH)
        rl = QVBoxLayout(rail)
        rl.setContentsMargins(8, 8, 8, 10)
        rl.setSpacing(2)

        rail_font = theme.ui_font(theme.UI_FONT_RAIL, medium=True)

        # --- Top-level MODE cluster (Edit / Fields / Fill) ---------------
        # A distinct exclusive group at the very top of the rail, above a
        # divider, so the three editing modes read as the primary switch (they
        # used to hide in the native View menu). "Edit" is checked by default.
        self._mode_group = QButtonGroup(self)
        self._mode_group.setExclusive(True)
        self._mode_buttons: dict[str, QToolButton] = {}
        for mode_id, icon_name, label, tip in _MODES:
            btn = QToolButton()
            btn.setObjectName("RailButton")
            btn.setCheckable(True)
            btn.setAutoRaise(True)
            btn.setCursor(Qt.PointingHandCursor)
            btn.setToolTip(tip)
            btn.setText(label)
            btn.setFont(rail_font)
            btn.setToolButtonStyle(Qt.ToolButtonTextUnderIcon)
            btn.setIconSize(QSize(theme.ICON_SIZE, theme.ICON_SIZE))
            btn.setMinimumHeight(theme.RAIL_BUTTON)
            btn.setSizePolicy(QSizePolicy.Policy.Expanding,
                              QSizePolicy.Policy.Fixed)
            btn.setIcon(icon_factory(icon_name) if icon_factory else QIcon())
            btn.clicked.connect(
                lambda _checked=False, m=mode_id: self._on_mode_clicked(m))
            self._mode_group.addButton(btn)
            self._mode_buttons[mode_id] = btn
            rl.addWidget(btn)
        self._mode_buttons["document"].setChecked(True)   # Edit is the default
        mode_divider = QFrame()
        mode_divider.setObjectName("RailDivider")
        mode_divider.setFrameShape(QFrame.HLine)
        mode_divider.setFixedHeight(1)
        rl.addWidget(mode_divider)

        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        for tool_id, (icon_name, label, tip) in _CATEGORIES.items():
            btn = QToolButton()
            btn.setObjectName("RailButton")
            btn.setCheckable(True)
            btn.setAutoRaise(True)
            btn.setCursor(Qt.PointingHandCursor)
            btn.setToolTip(tip)
            btn.setText(label)
            btn.setFont(rail_font)
            btn.setToolButtonStyle(Qt.ToolButtonTextUnderIcon)
            btn.setIconSize(QSize(theme.ICON_SIZE, theme.ICON_SIZE))
            btn.setMinimumHeight(theme.RAIL_BUTTON)
            btn.setSizePolicy(QSizePolicy.Policy.Expanding,
                              QSizePolicy.Policy.Fixed)
            icon = icon_factory(icon_name) if icon_factory else QIcon()
            btn.setIcon(icon)
            btn.clicked.connect(
                lambda _checked=False, t=tool_id: self._on_tool_clicked(t))
            self._group.addButton(btn)
            self._buttons[tool_id] = btn
            # Only the Document-mode categories show until a mode sets otherwise
            # (set_mode swaps the visible set); the rest are built once, hidden.
            btn.setVisible(tool_id in _MODE_TOOLS["document"])
            rl.addWidget(btn)

        # Push the one-shot action buttons (Image / Sign) to the bottom of the
        # rail, separated from the modes by a hairline divider.
        rl.addStretch(1)
        divider = QFrame()
        divider.setObjectName("RailDivider")
        divider.setFrameShape(QFrame.HLine)
        divider.setFixedHeight(1)
        rl.addWidget(divider)
        self._action_buttons: dict[str, QToolButton] = {}
        for act_id, icon_name, label, tip in _RAIL_ACTIONS:
            btn = QToolButton()
            btn.setObjectName("RailButton")
            btn.setAutoRaise(True)
            btn.setCursor(Qt.PointingHandCursor)
            btn.setToolTip(tip)
            btn.setText(label)
            btn.setFont(rail_font)
            btn.setToolButtonStyle(Qt.ToolButtonTextUnderIcon)
            btn.setIconSize(QSize(theme.ICON_SIZE, theme.ICON_SIZE))
            btn.setMinimumHeight(theme.RAIL_BUTTON)
            btn.setSizePolicy(QSizePolicy.Policy.Expanding,
                              QSizePolicy.Policy.Fixed)
            btn.setIcon(icon_factory(icon_name) if icon_factory else QIcon())
            btn.clicked.connect(
                lambda _checked=False, a=act_id: self.actionRequested.emit(a))
            self._action_buttons[act_id] = btn
            rl.addWidget(btn)
        outer.addWidget(rail)

        # Select is the default armed tool.
        self._buttons["select"].setChecked(True)

        # --- Contextual content column (header + swappable stack) --------
        content = QWidget()
        content.setObjectName("LeftPanelContent")
        col = QVBoxLayout(content)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(0)
        outer.addWidget(content, 1)

        # The header names the active mode ("Find & Replace" on the find page,
        # else "Format") so the column always self-describes. It lives inside
        # the content column (not as a QDockWidget title-bar widget, which the
        # dock did not honor) so it is never overlapped.
        header_bar = QWidget()
        header_bar.setObjectName("LeftPanelHeader")
        header_bar.setFixedHeight(theme.TOOLBAR_HEIGHT)
        hbl = QHBoxLayout(header_bar)
        hbl.setContentsMargins(16, 0, 12, 0)
        hbl.setSpacing(0)
        self._header_label = QLabel("Format")
        self._header_label.setObjectName("LeftPanelTitle")
        self._header_label.setFont(theme.ui_font(theme.UI_FONT_TITLE, semibold=True))
        hbl.addWidget(self._header_label)
        hbl.addStretch(1)
        col.addWidget(header_bar)

        # --- Swappable content below the header --------------------------
        # A stack holding the Format/Inspector panel (page 0, the default),
        # the Find & Replace panel (page 1), the Comments panel (page 2,
        # annotations & markup §5.4), and the Bookmarks panel (page 3,
        # navigation M1 -- the indices claimed by the conflict ledger). Each
        # panel tool swaps the stack to its page; any other tool swaps it
        # back to Format, so the left column hosts exactly one at a time
        # (REFLOW_SPEC §R4.1 / §R5.1).
        self._stack = QStackedWidget()
        self._stack.setObjectName("LeftPanelStack")
        inspector.setParent(self)
        self._stack.addWidget(inspector)        # index 0
        self._find_panel = None                  # installed lazily by the window
        self._comments_panel = None              # installed lazily by the window
        self._bookmark_panel = None              # installed lazily by the window
        self._markup_panel = None                # installed lazily by the window
        self._forms_panel = None                 # installed lazily by the window
        # Mode-adaptive category panels (Add / Fields / Fill / Sign / Image /
        # Finish), keyed by ``_TOOL_PANEL`` panel key. The window installs each
        # via ``install_named_panel``; ``_sync_content`` swaps to them.
        self._named: dict = {}
        col.addWidget(self._stack, 1)

    # --- public API ------------------------------------------------------
    def install_find_panel(self, panel) -> None:
        """Mount the Find & Replace panel as the second stack page (the window
        builds it once the document/undo plumbing exists). Idempotent."""
        if self._find_panel is panel:
            return
        if self._find_panel is not None:
            self._stack.removeWidget(self._find_panel)
        self._find_panel = panel
        if panel is not None:
            self._stack.addWidget(panel)        # index 1

    def install_comments_panel(self, panel) -> None:
        """Mount the Comments panel as the THIRD stack page (annotations &
        markup §5.4; the window builds it with its injected callables).
        Install AFTER the find panel so it lands at stack index 2 (the
        conflict-ledger slot). Idempotent."""
        if self._comments_panel is panel:
            return
        if self._comments_panel is not None:
            self._stack.removeWidget(self._comments_panel)
        self._comments_panel = panel
        if panel is not None:
            self._stack.addWidget(panel)        # index 2

    def install_bookmark_panel(self, panel) -> None:
        """Mount the Bookmarks panel as the FOURTH stack page (navigation M1;
        the window builds it with its injected callables). Install AFTER the
        comments panel so it lands at stack index 3 (the conflict-ledger
        slot). Idempotent."""
        if self._bookmark_panel is panel:
            return
        if self._bookmark_panel is not None:
            self._stack.removeWidget(self._bookmark_panel)
        self._bookmark_panel = panel
        if panel is not None:
            self._stack.addWidget(panel)        # index 3

    def install_markup_panel(self, panel) -> None:
        """Mount the Markup tools palette as a stack page (the window builds it
        with the markup actions). Shown when the rail's Markup mode is active.
        Idempotent."""
        if self._markup_panel is panel:
            return
        if self._markup_panel is not None:
            self._stack.removeWidget(self._markup_panel)
        self._markup_panel = panel
        if panel is not None:
            self._stack.addWidget(panel)

    def install_forms_panel(self, panel) -> None:
        """Mount the Forms builder panel as a stack page (the window builds it
        with its injected callables). Shown while the top mode is FORM FIELDS.
        Idempotent."""
        if self._forms_panel is panel:
            return
        if self._forms_panel is not None:
            self._stack.removeWidget(self._forms_panel)
        self._forms_panel = panel
        if panel is not None:
            self._stack.addWidget(panel)

    def show_find_panel(self) -> None:
        """Swap the content area to the Find panel (if installed)."""
        if self._find_panel is not None:
            self._stack.setCurrentWidget(self._find_panel)

    def show_forms_panel(self) -> None:
        """Swap the content area to the Forms builder panel (if installed)."""
        if self._forms_panel is not None:
            self._stack.setCurrentWidget(self._forms_panel)

    def show_markup_panel(self) -> None:
        """Swap the content area to the Markup tools palette (if installed)."""
        if self._markup_panel is not None:
            self._stack.setCurrentWidget(self._markup_panel)

    def show_comments_panel(self) -> None:
        """Swap the content area to the Comments panel (if installed)."""
        if self._comments_panel is not None:
            self._stack.setCurrentWidget(self._comments_panel)

    def show_bookmark_panel(self) -> None:
        """Swap the content area to the Bookmarks panel (if installed)."""
        if self._bookmark_panel is not None:
            self._stack.setCurrentWidget(self._bookmark_panel)

    def show_format_panel(self) -> None:
        """Swap the content area back to the Format/Inspector panel."""
        self._stack.setCurrentWidget(self._inspector)

    def install_named_panel(self, key: str, panel) -> None:
        """Mount a mode-adaptive category panel under ``key`` (one of the
        ``_TOOL_PANEL`` panel keys: forms_add / forms_list / fill_fields /
        fill_sign / fill_image / fill_flatten). Idempotent."""
        if self._named.get(key) is panel:
            return
        old = self._named.get(key)
        if old is not None:
            self._stack.removeWidget(old)
        self._named[key] = panel
        if panel is not None:
            self._stack.addWidget(panel)

    def show_named_panel(self, key: str) -> None:
        panel = self._named.get(key)
        if panel is not None:
            self._stack.setCurrentWidget(panel)

    def set_mode(self, mode: str) -> None:
        """Show only the current top mode's rail categories + select that mode's
        default category (three-mode adaptive rail). The one-shot Image/Sign
        actions at the rail's foot belong to Document mode; in Fill Form they
        reappear as their own categories."""
        tools = _MODE_TOOLS.get(mode, _MODE_TOOLS["document"])
        for tool_id, btn in self._buttons.items():
            btn.setVisible(tool_id in tools)
        for btn in getattr(self, "_action_buttons", {}).values():
            btn.setVisible(mode == "document")
        self.set_active_tool(_MODE_DEFAULT_TOOL.get(mode, "select"))

    def find_panel(self):
        return self._find_panel

    def comments_panel(self):
        return self._comments_panel

    def bookmark_panel(self):
        return self._bookmark_panel

    def set_active_tool(self, tool_id: str) -> None:
        """Check the given tool WITHOUT emitting ``toolSelected`` (used to keep
        the strip in sync when the mode changes from elsewhere, e.g. the Add Text
        toolbar action or a commit returning to Select). Also swaps the content
        area so the Format/Find panel matches the armed tool."""
        btn = self._buttons.get(tool_id)
        if btn is None:
            return
        self._syncing = True
        try:
            btn.setChecked(True)
        finally:
            self._syncing = False
        self._sync_content(tool_id)

    def _sync_content(self, tool_id: str) -> None:
        entry = _TOOL_PANEL.get(tool_id)
        if entry is None:                      # select / text_edit -> Format
            self.show_format_panel()
            self._header_label.setText("Format")
            return
        key, header = entry
        self._header_label.setText(header)
        # The four Document-mode panels keep their dedicated slots; the new
        # mode-adaptive categories live in the ``_named`` stack pages.
        builtin = {
            "markup": self.show_markup_panel,
            "find": self.show_find_panel,
            "comments": self.show_comments_panel,
            "bookmarks": self.show_bookmark_panel,
        }.get(key)
        if builtin is not None:
            builtin()
        elif key in self._named:
            self.show_named_panel(key)
        else:
            self.show_format_panel()           # panel not installed yet

    def active_tool(self) -> str:
        for tool_id, btn in self._buttons.items():
            if btn.isChecked():
                return tool_id
        return "select"

    def set_enabled_tools(self, on: bool) -> None:
        """Enable/disable the rail (disabled while no document is open)."""
        for btn in self._buttons.values():
            btn.setEnabled(on)
        for btn in getattr(self, "_action_buttons", {}).values():
            btn.setEnabled(on)
        for btn in getattr(self, "_mode_buttons", {}).values():
            btn.setEnabled(on)

    # --- top-mode cluster ------------------------------------------------
    def _on_mode_clicked(self, mode_id: str) -> None:
        if self._syncing:
            return
        self.topModeSelected.emit(mode_id)

    def set_active_mode(self, mode_id: str) -> None:
        """Check the given mode button WITHOUT emitting ``topModeSelected`` --
        used to follow a mode change driven from elsewhere (Ctrl+1/2/3, a tab
        switch restoring a doc's stored mode, or a programmatic bounce)."""
        btn = self._mode_buttons.get(mode_id)
        if btn is None:
            return
        self._syncing = True
        try:
            btn.setChecked(True)
        finally:
            self._syncing = False

    def set_mode_enabled(self, mode_id: str, on: bool) -> None:
        """Enable/disable one mode button (Fill Form is only available on a doc
        that actually has a form)."""
        btn = self._mode_buttons.get(mode_id)
        if btn is not None:
            btn.setEnabled(on)

    # --- internal --------------------------------------------------------
    def _on_tool_clicked(self, tool_id: str) -> None:
        if self._syncing:
            return
        self._sync_content(tool_id)
        self.toolSelected.emit(tool_id)
