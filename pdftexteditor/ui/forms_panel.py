"""Forms panel (three-mode authoring §UI).

The left column's stacked page shown while the top mode is FORM FIELDS: a form
BUILDER that reads as a sibling of the Inspector (same margins, section
eyebrows, segmented control, and list-row treatment). Two parts:

  * an "ADD A FIELD" section -- a segmented Text / Checkbox picker (the app's
    #InspectorSeg control) that arms which kind the next drag-to-create places,
    with a live hint, and
  * a "FIELDS" section -- every field in the document (authored + pre-existing
    "(file)" widgets) as BookmarkTree-style rows, with jump-on-click and
    Edit / Delete on the fields the user authored.

Like the other left panels it is PURE CHROME: the window injects five callables
and this module imports zero model code (fields read via ``getattr``). The
window calls ``refresh()`` after every field-author command, structural op, and
tab switch. All colour/spacing comes from ``theme`` tokens (light + dark safe);
object names are load-bearing for the QSS + tests.
"""

from __future__ import annotations

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (
    QButtonGroup,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from . import theme

# One compact glyph per field kind (theme-neutral row markers; the canvas
# shows the real widget).
_KIND_GLYPHS = {
    "text": "✎",
    "date": "🗓",
    "checkbox": "☑",
    "radio": "◉",
    "combo": "▾",
    "listbox": "☰",
    "signature": "✒",
    "button": "⬚",
}
_KIND_LABELS = {
    "text": "Text field",
    "date": "Date",
    "checkbox": "Checkbox",
    "radio": "Radio",
    "combo": "Dropdown",
    "listbox": "List box",
    "signature": "Signature",
    "button": "Button",
}
# The kinds the authoring backend can create (add_form_field accepts these),
# shown as the palette, in order. Combo/listbox arrive in a later slice.
_AUTHOR_KINDS = (
    ("text", "Text"),
    ("date", "Date"),
    ("checkbox", "Checkbox"),
    ("signature", "Signature"),
)
# Monochrome line icon per authorable kind (the app icon set, via icon_factory)
# -- so the palette reads as designed instead of stray emoji glyphs.
_KIND_ICONS = {
    "text": "text_edit",
    "date": "calendar",
    "checkbox": "check_square",
    "signature": "signature",
}
_NAME_LEN = 30


class FormsPanel(QWidget):
    """Build fillable forms: a segmented add-field picker + a list of fields."""

    def __init__(self, list_fields, arm_add, jump, edit, delete, align=None,
                 parent=None, show_add=True, show_list=True, apply_props=None,
                 icon_factory=None):
        super().__init__(parent)
        self.setObjectName("FormsPanel")
        self.setAttribute(Qt.WA_StyledBackground, True)  # paint #FormsPanel bg
        self._list_fields = list_fields
        self._icon_factory = icon_factory
        self._arm_add = arm_add
        self._jump = jump
        self._edit = edit
        self._delete = delete
        self._align = align
        self._apply_props = apply_props
        # FIELD PROPERTIES sub-section (replaces the old FieldPropsPopup): edits
        # the selected field's name / type / size live in the bar. Hidden until
        # a field is selected; a loading guard stops programmatic fills echoing
        # as edits.
        self._loading_props = False
        self._sel_field_identity = None
        self._field_host = None
        self._prop_name = None
        self._prop_type = None
        self._prop_size = None
        self._prop_size_row = None
        self._prop_align_row = None
        self._prop_align_group = None
        self._prop_align_buttons = {}
        self._refreshing = False
        self._armed_kind = "text"
        # The panel splits into two categories in the mode-adaptive rail: an ADD
        # picker (show_list=False) and the FIELDS list (show_add=False). Each
        # section's widgets stay None when its flag is off so the shared methods
        # (refresh / _sync_hint / _sync_enablement) can no-op safely.
        self._show_add = show_add
        self._show_list = show_list
        self._seg_buttons: dict = {}
        self._hint = None
        self.list = None
        self._count = None
        self._empty = None
        self.align_button = None
        self.edit_button = None
        self.delete_button = None
        self._build()
        theme.events.changed.connect(self._restyle)

    def _restyle(self) -> None:
        # The armed hint (#FormsArmedHint -> ACCENT_TEXT) and empty state
        # (#InspectorEmptyHint -> TEXT_SECONDARY) re-color via the global QSS on
        # a theme flip; only the count has no QSS rule, so re-apply it here.
        if self._count is not None:
            self._count.setStyleSheet(f"color: {theme.TEXT_SECONDARY};")

    # --- construction ----------------------------------------------------
    @staticmethod
    def _eyebrow(text: str) -> QLabel:
        lab = QLabel(text)
        lab.setObjectName("InspectorSectionHeader")   # reuse the app eyebrow QSS
        lab.setFont(theme.caps_header_font())
        return lab

    def _seg_button(self, kind: str, label: str) -> QToolButton:
        b = QToolButton()
        b.setObjectName("FieldTypeBtn")
        b.setCheckable(True)
        b.setCursor(Qt.PointingHandCursor)
        b.setText(f"  {label}")
        b.setToolTip(f"Place a {label.lower()} field on the page")
        if self._icon_factory is not None:
            ico = self._icon_factory(_KIND_ICONS.get(kind, ""))
            if isinstance(ico, QIcon):
                b.setIcon(ico)
                b.setIconSize(QSize(16, 16))
        b.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        b.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        b.setMinimumHeight(34)
        b.setFocusPolicy(Qt.NoFocus)
        return b

    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 14, 16, 14)       # match the Inspector
        outer.setSpacing(12)

        if self._show_add:
            outer.addWidget(self._eyebrow("ADD A FIELD"))

            # Field-type PALETTE: a clean vertical stack of icon+label buttons
            # (Text, Date, Checkbox, Signature). Pick one, then click on the
            # page to place it.
            self._seg_group = QButtonGroup(self)
            self._seg_group.setExclusive(True)
            pal = QVBoxLayout()
            pal.setContentsMargins(0, 0, 0, 0)
            pal.setSpacing(4)
            for kind, label in _AUTHOR_KINDS:
                b = self._seg_button(kind, label)
                b.clicked.connect(lambda _c=False, k=kind: self._on_pick_kind(k))
                self._seg_group.addButton(b)
                self._seg_buttons[kind] = b
                pal.addWidget(b)
            self._seg_buttons["text"].setChecked(True)
            outer.addLayout(pal)

        if not self._show_list:
            outer.addStretch(1)
            return

        # "FIELDS" section header with a right-aligned count.
        head = QHBoxLayout()
        head.setContentsMargins(0, 6, 0, 0)
        head.addWidget(self._eyebrow("FIELDS"))
        head.addStretch(1)
        self._count = QLabel("0")
        self._count.setFont(theme.caps_header_font())
        self._count.setStyleSheet(f"color: {theme.TEXT_SECONDARY};")
        head.addWidget(self._count)
        outer.addLayout(head)

        # Align every field onto the box drawn on the sheet, so fills clip the
        # printed square consistently. Hidden when there are no fields.
        self.align_button = QPushButton("Align all to the boxes")
        self.align_button.setObjectName("FormsRowBtn")
        self.align_button.setCursor(Qt.PointingHandCursor)
        self.align_button.clicked.connect(self._on_align_clicked)
        self.align_button.setToolTip(
            "Snap every field exactly onto the checkbox/box drawn on the page")
        outer.addWidget(self.align_button)

        self.list = QListWidget()
        self.list.setObjectName("FormsList")
        self.list.setFont(theme.ui_font(12))
        self.list.setWordWrap(False)
        self.list.setFrameShape(QFrame.NoFrame)
        self.list.itemClicked.connect(self._on_item_clicked)
        self.list.itemDoubleClicked.connect(self._on_item_double_clicked)
        self.list.currentItemChanged.connect(lambda *_: self._sync_enablement())
        outer.addWidget(self.list, 1)

        # Empty state -- teaches the two-step flow (shown in place of the list).
        self._empty = QLabel(
            "No form fields yet.\n\n"
            "1.  Pick a field type above.\n"
            "2.  Click on the page to place it.")
        self._empty.setObjectName("InspectorEmptyHint")
        self._empty.setFont(theme.ui_font(12))
        self._empty.setWordWrap(True)
        self._empty.setAlignment(Qt.AlignLeft | Qt.AlignTop)
        outer.addWidget(self._empty, 1)

        actions = QHBoxLayout()
        actions.setContentsMargins(0, 0, 0, 0)
        actions.setSpacing(8)
        self.edit_button = QPushButton("Edit…")
        self.edit_button.setObjectName("FormsRowBtn")
        self.edit_button.setCursor(Qt.PointingHandCursor)
        self.edit_button.setToolTip("Name, type, and font size (double-click a "
                                    "field too)")
        self.edit_button.clicked.connect(self._on_edit_clicked)
        self.delete_button = QPushButton("Delete")
        self.delete_button.setObjectName("FormsRowBtn")
        self.delete_button.setCursor(Qt.PointingHandCursor)
        self.delete_button.clicked.connect(self._on_delete_clicked)
        actions.addWidget(self.edit_button)
        actions.addWidget(self.delete_button)
        outer.addLayout(actions)

        # --- SELECTED FIELD properties (replaces the old pop-up) ---------
        # Shown only while a field is selected; edits its name / type / size
        # live in the bar. set_field_target populates + shows it. Everything
        # here commits through apply_props under a loading guard.
        self._field_host = QWidget()
        self._field_host.setObjectName("FieldPropsHost")
        fl = QVBoxLayout(self._field_host)
        fl.setContentsMargins(0, 10, 0, 0)
        fl.setSpacing(8)
        fl.addWidget(self._eyebrow("SELECTED FIELD"))
        self._prop_name = QLineEdit()
        self._prop_name.setObjectName("FieldPropName")
        self._prop_name.setPlaceholderText("Field name")
        self._prop_name.setFont(theme.ui_font(12))
        self._prop_name.editingFinished.connect(self._on_prop_commit)
        fl.addWidget(self._prop_name)
        trow = QHBoxLayout()
        trow.setContentsMargins(0, 0, 0, 0)
        trow.setSpacing(6)
        tlab = QLabel("Type")
        tlab.setFont(theme.ui_font(12))
        trow.addWidget(tlab)
        self._prop_type = QComboBox()
        for kind, label in _AUTHOR_KINDS:
            self._prop_type.addItem(label, kind)
        self._prop_type.currentIndexChanged.connect(self._on_prop_commit)
        trow.addWidget(self._prop_type, 1)
        fl.addLayout(trow)
        self._prop_size_row = QWidget()
        srow = QHBoxLayout(self._prop_size_row)
        srow.setContentsMargins(0, 0, 0, 0)
        srow.setSpacing(6)
        slab = QLabel("Size")
        slab.setFont(theme.ui_font(12))
        srow.addWidget(slab)
        self._prop_size = QComboBox()
        self._prop_size.setEditable(True)
        self._prop_size.addItem("Auto", 0.0)
        for s in (8, 9, 10, 11, 12, 14, 16, 18, 20, 24, 28, 36):
            self._prop_size.addItem(str(s), float(s))
        self._prop_size.currentIndexChanged.connect(self._on_prop_commit)
        self._prop_size.lineEdit().editingFinished.connect(self._on_prop_commit)
        srow.addWidget(self._prop_size, 1)
        fl.addWidget(self._prop_size_row)
        # Alignment (text/date only): left / centre / right, a segmented control
        # of icon buttons. Writes the field's /Q.
        self._prop_align_row = QWidget()
        arow = QHBoxLayout(self._prop_align_row)
        arow.setContentsMargins(0, 0, 0, 0)
        arow.setSpacing(6)
        alab = QLabel("Align")
        alab.setFont(theme.ui_font(12))
        arow.addWidget(alab)
        aseg = QFrame()
        aseg.setObjectName("InspectorSeg")
        asl = QHBoxLayout(aseg)
        asl.setContentsMargins(3, 3, 3, 3)
        asl.setSpacing(3)
        self._prop_align_group = QButtonGroup(self)
        self._prop_align_group.setExclusive(True)
        for q, icon_name, tip in ((0, "align_left", "Left"),
                                  (1, "align_center", "Centre"),
                                  (2, "align_right", "Right")):
            ab = QToolButton()
            ab.setObjectName("SegButton")
            ab.setCheckable(True)
            ab.setCursor(Qt.PointingHandCursor)
            ab.setToolTip(f"Align {tip.lower()}")
            if self._icon_factory is not None:
                ico = self._icon_factory(icon_name)
                if isinstance(ico, QIcon):
                    ab.setIcon(ico)
                    ab.setIconSize(QSize(16, 16))
            ab.setFocusPolicy(Qt.NoFocus)
            ab.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            ab.clicked.connect(self._on_prop_commit)
            self._prop_align_group.addButton(ab)
            self._prop_align_buttons[q] = ab
            asl.addWidget(ab)
        arow.addWidget(aseg, 1)
        fl.addWidget(self._prop_align_row)
        self._field_host.setVisible(False)
        outer.addWidget(self._field_host)

        self._sync_enablement()

    # --- public API ------------------------------------------------------
    def refresh(self) -> None:
        """Rebuild the rows from ``list_fields()`` (every page), sorted by
        (page, y, x), keeping the selected row across the rebuild by identity.
        A model read must never crash chrome."""
        if self.list is None:                  # ADD-only panel has no list
            return
        try:
            fields = list(self._list_fields() or [])
        except Exception:  # noqa: BLE001 - a model read must not crash chrome
            fields = []
        fields.sort(key=self._sort_key)
        selected = self.selected_identity()
        self._refreshing = True
        try:
            self.list.clear()
            for f in fields:
                item = QListWidgetItem(self._row_text(f))
                item.setData(Qt.UserRole, getattr(f, "identity", None))
                item.setData(Qt.UserRole + 1, int(getattr(f, "xref", 0)) < 0)
                self.list.addItem(item)
                if selected is not None \
                        and getattr(f, "identity", None) == selected:
                    self.list.setCurrentItem(item)
        finally:
            self._refreshing = False
        n = self.list.count()
        self._count.setText(str(n))
        self.list.setVisible(n > 0)
        self.align_button.setVisible(n > 0)
        self._empty.setVisible(n == 0)
        self._sync_enablement()

    def selected_identity(self):
        if self.list is None:
            return None
        item = self.list.currentItem()
        return None if item is None else item.data(Qt.UserRole)

    def _selected_authored(self) -> bool:
        if self.list is None:
            return False
        item = self.list.currentItem()
        return bool(item is not None and item.data(Qt.UserRole + 1))

    # --- selected-field properties (in-bar, replaces the pop-up) ----------
    def set_field_target(self, field) -> None:
        """Show + populate the SELECTED FIELD properties for ``field`` (a
        FormField), or hide them when ``None``. Populated under a loading guard
        so it never echoes as an edit. A pre-existing baked widget (xref >= 0)
        can only have its SIZE changed -- name/type are file truth, so they are
        disabled (mirrors the old popup's allow_name_kind gate)."""
        if self._field_host is None:
            return
        if field is None:
            self._sel_field_identity = None
            self._field_host.setVisible(False)
            return
        self._sel_field_identity = getattr(field, "identity", None)
        kind = getattr(field, "kind", "text") or "text"
        existing = int(getattr(field, "xref", -1) or -1) >= 0
        self._loading_props = True
        try:
            self._prop_name.setText((getattr(field, "name", "") or "").strip())
            idx = self._prop_type.findData(kind)
            self._prop_type.setCurrentIndex(idx if idx >= 0 else 0)
            self._select_prop_size(
                float(getattr(field, "text_fontsize", 0.0) or 0.0))
            # Name + Size are editable on ANY field (authored OR a field already
            # in the file -- rename/resize/fontsize all work on a baked widget).
            # Only RETYPE of a pre-existing widget is not supported yet.
            self._prop_name.setEnabled(True)
            self._prop_type.setEnabled(not existing)
            self._prop_type.setToolTip(
                "The type of a field already in the file can't be changed yet"
                if existing else "")
            self._prop_size.setEnabled(True)
            self._prop_size_row.setVisible(kind in ("text", "date"))
            q = int(getattr(field, "align", 0) or 0)
            ab = (self._prop_align_buttons.get(q)
                  or self._prop_align_buttons.get(0))
            if ab is not None:
                ab.setChecked(True)
            self._prop_align_row.setVisible(kind in ("text", "date"))
        finally:
            self._loading_props = False
        self._field_host.setVisible(True)

    def _select_prop_size(self, size: float) -> None:
        idx = self._prop_size.findData(float(size))
        if idx >= 0:
            self._prop_size.setCurrentIndex(idx)
        else:
            self._prop_size.setEditText("Auto" if size <= 0 else str(int(size)))

    def _read_prop_size(self) -> float:
        data = self._prop_size.currentData()
        if data is not None:
            return float(data)
        txt = self._prop_size.currentText().strip().lower()
        if txt in ("", "auto"):
            return 0.0
        try:
            return float(txt)
        except ValueError:
            return 0.0

    def _read_prop_align(self) -> int:
        for q, b in self._prop_align_buttons.items():
            if b.isChecked():
                return int(q)
        return 0

    def _on_prop_commit(self, *_a) -> None:
        if self._loading_props or self._sel_field_identity is None:
            return
        if not callable(self._apply_props):
            return
        name = self._prop_name.text().strip()
        kind = self._prop_type.currentData() or "text"
        size = self._read_prop_size()
        align = self._read_prop_align()
        try:
            self._apply_props(self._sel_field_identity, name, kind, size, align)
        except Exception:  # noqa: BLE001 - a props commit must not crash chrome
            pass

    # --- rows ------------------------------------------------------------
    @staticmethod
    def _sort_key(f) -> tuple:
        page = int(getattr(f, "page_index", 0))
        rect = getattr(f, "rect", None) or (0.0, 0.0, 0.0, 0.0)
        return (page, rect[1], rect[0])

    @staticmethod
    def _row_text(f) -> str:
        kind = getattr(f, "kind", "") or ""
        glyph = _KIND_GLYPHS.get(kind, "•")
        page = int(getattr(f, "page_index", 0))
        name = (getattr(f, "name", "") or "").strip() or _KIND_LABELS.get(
            kind, kind.capitalize() or "Field")
        if len(name) > _NAME_LEN:
            name = name[: _NAME_LEN - 1] + "…"
        text = f"{glyph}   {name}    p.{page + 1}"
        if int(getattr(f, "xref", 0)) >= 0:
            text += "   (file)"       # a pre-existing widget: shown, not edited
        return text

    # --- interaction -----------------------------------------------------
    def _on_pick_kind(self, kind: str) -> None:
        self._armed_kind = kind
        try:
            self._arm_add(kind)
        except Exception:  # noqa: BLE001
            pass

    def _on_align_clicked(self) -> None:
        if callable(self._align):
            try:
                self._align()
            except Exception:  # noqa: BLE001
                pass

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
            self._edit(item.data(Qt.UserRole))   # authored: all; existing: size
        except Exception:  # noqa: BLE001
            pass

    def _on_edit_clicked(self) -> None:
        ident = self.selected_identity()
        if ident is not None:
            try:
                self._edit(ident)
            except Exception:  # noqa: BLE001
                pass

    def _on_delete_clicked(self) -> None:
        ident = self.selected_identity()
        if ident is not None:
            try:
                self._delete(ident)
            except Exception:  # noqa: BLE001
                pass

    def _sync_enablement(self) -> None:
        # Edit + Delete both work on ANY selected field: authored fields edit
        # name/kind/size, pre-existing widgets edit size (and stage a delete).
        if self.edit_button is None:
            return
        has = self.list.currentItem() is not None
        self.edit_button.setEnabled(has)
        self.delete_button.setEnabled(has)
