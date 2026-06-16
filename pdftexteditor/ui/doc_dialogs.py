"""Document-tool dialogs (doc-tools workstream §1): PURE CHROME, never touch
the model (the Inspector pattern). Every dialog is constructed by a MainWindow
``_do_*`` method ONLY when its options/seam argument is ``None``, so offscreen
tests bypass the modal entirely by passing explicit options -- the dialog-seam
rule that keeps the suites free of un-dismissable ``exec()`` modals.

This module holds the M1 pair (Properties, Export Images), the M2 stamps pair
(Watermark, Header & Footer), the M3 crop confirm step (Crop), and the M4
security sheet (Security). Optimize deliberately has NO dialog (fixed flags,
§2.8) and Print uses the native QPrintDialog.
"""

from __future__ import annotations

import datetime
import os
from dataclasses import dataclass, field

from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QColor, QIntValidator
from PySide6.QtWidgets import (
    QCheckBox,
    QColorDialog,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QRadioButton,
    QSlider,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from .. import doctools
from ..fonts import base14_code_for_family
from . import theme

# The stamp dialogs' font picker offers the base-14 trio only (doc-tools §5:
# arbitrary families would need save-side font registration outside the bake
# pipeline). ``base14_code_for_family`` maps name + B/I to the fitz code.
_STAMP_FAMILIES = ("Helvetica", "Times", "Courier")

# DPI presets for the export combo; the field stays editable for anything in
# the probe-verified 18..1200 working range.
_DPI_PRESETS = ("72", "150", "300", "600")
_DPI_MIN, _DPI_MAX = 18, 1200


@dataclass(frozen=True)
class ExportImagesOptions:
    """What ``MainWindow._do_export_images`` needs; tests construct it
    directly (the dialog-seam rule)."""

    fmt: str          # "png" | "jpg"
    dpi: int          # window clamps to 18..1200
    pages: str        # 1-based range string for _parse_page_ranges
    out_dir: str      # destination folder


def _section_header(text: str) -> QLabel:
    """The small-caps section header every project dialog uses (the
    _SplitDialog / Inspector convention)."""
    header = QLabel(text)
    header.setObjectName("InspectorSectionHeader")
    header.setFont(theme.ui_font(11, semibold=True))
    return header


class _ExportImagesDialog(QDialog):
    """File > Export > Pages as Images... (§2.3): format, DPI, page range,
    destination folder. Read the result via ``options()``."""

    def __init__(self, page_count: int, start_dir: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Export Pages as Images")
        self._start_dir = start_dir or os.path.expanduser("~")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 18, 20, 16)
        lay.setSpacing(12)
        lay.addWidget(_section_header("EXPORT PAGES AS IMAGES"))

        form = QFormLayout()
        form.setSpacing(8)
        self.cb_format = QComboBox()
        self.cb_format.addItems(["PNG", "JPEG"])
        form.addRow("Format:", self.cb_format)

        self.cb_dpi = QComboBox()
        self.cb_dpi.setEditable(True)
        self.cb_dpi.addItems(list(_DPI_PRESETS))
        self.cb_dpi.setCurrentText("150")
        self.cb_dpi.setValidator(
            QIntValidator(_DPI_MIN, _DPI_MAX, self.cb_dpi))
        form.addRow("Resolution (DPI):", self.cb_dpi)

        self.ed_pages = QLineEdit(
            f"1-{page_count}" if page_count > 1 else "1")
        self.ed_pages.setPlaceholderText("e.g. 1-3, 5, 8-10")
        form.addRow("Pages:", self.ed_pages)

        self.ed_dir = QLineEdit(self._start_dir)
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._pick_dir)
        row = QHBoxLayout()
        row.addWidget(self.ed_dir, 1)
        row.addWidget(browse)
        form.addRow("Folder:", row)
        lay.addLayout(form)

        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)

    def _pick_dir(self) -> None:
        d = QFileDialog.getExistingDirectory(
            self, "Choose output folder",
            self.ed_dir.text() or self._start_dir)
        if d:
            self.ed_dir.setText(d)

    def options(self) -> ExportImagesOptions:
        fmt = ("jpg" if self.cb_format.currentText().upper().startswith("JP")
               else "png")
        try:
            dpi = int(self.cb_dpi.currentText())
        except ValueError:
            dpi = 150
        dpi = max(_DPI_MIN, min(dpi, _DPI_MAX))
        return ExportImagesOptions(
            fmt=fmt, dpi=dpi, pages=self.ed_pages.text(),
            out_dir=self.ed_dir.text())


class _PropertiesDialog(QDialog):
    """File > Document Properties... (Cmd+D, §2.1). One dialog, two halves:
    editable Description fields on top (read back via ``fields()``; the window
    stages changed values as ONE structural op), a read-only document grid +
    fonts table below, all fed from ``PDFDocument.properties()``."""

    def __init__(self, props: dict, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Document Properties")
        self.setMinimumWidth(540)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 18, 20, 16)
        lay.setSpacing(12)

        # --- editable Description fields -------------------------------
        lay.addWidget(_section_header("DESCRIPTION"))
        meta = props.get("metadata", {}) or {}
        form = QFormLayout()
        form.setSpacing(8)
        self.ed_title = QLineEdit(meta.get("title", ""))
        self.ed_author = QLineEdit(meta.get("author", ""))
        self.ed_subject = QLineEdit(meta.get("subject", ""))
        self.ed_keywords = QLineEdit(meta.get("keywords", ""))
        form.addRow("Title:", self.ed_title)
        form.addRow("Author:", self.ed_author)
        form.addRow("Subject:", self.ed_subject)
        form.addRow("Keywords:", self.ed_keywords)
        lay.addLayout(form)
        note = QLabel(
            "Applying changed fields bakes pending edits into the page first "
            "(one undoable step), like every document-level operation.")
        note.setWordWrap(True)
        note.setObjectName("PropertiesNote")
        lay.addWidget(note)

        # --- read-only document facts -----------------------------------
        lay.addWidget(_section_header("DOCUMENT"))
        facts = QFormLayout()
        facts.setSpacing(6)
        sizes = ", ".join(
            doctools.format_page_size(w, h)
            for w, h in props.get("page_sizes", []))
        rows = (
            ("File:", props.get("path", "")),
            ("File size:", doctools.human_size(props.get("file_size", 0))),
            ("Pages:", str(props.get("page_count", 0))),
            ("Page sizes:", sizes),
            ("PDF version:", props.get("pdf_version", "") or "unknown"),
            ("Security:", "Password protected" if props.get("encrypted")
             else "Not encrypted"),
        )
        for label, value in rows:
            val = QLabel(value)
            val.setWordWrap(True)
            val.setTextInteractionFlags(Qt.TextSelectableByMouse)
            if label == "File:":
                # A filesystem path has no spaces, so word wrap cannot break
                # it and the label hard-clipped at the column width, hiding
                # the FILENAME (the most important part). Middle-elide to the
                # value column instead; the tooltip carries the full path.
                val.setToolTip(value)
                val.setText(val.fontMetrics().elidedText(
                    value, Qt.ElideMiddle, 380))
            facts.addRow(label, val)
        lay.addLayout(facts)

        # --- fonts table -------------------------------------------------
        lay.addWidget(_section_header("FONTS"))
        fonts = props.get("fonts", [])
        table = QTableWidget(len(fonts), 3, self)
        table.setHorizontalHeaderLabels(["Name", "Type", "Embedded"])
        for r, (name, ftype, embedded) in enumerate(fonts):
            cells = (name, ftype, "yes" if embedded else "no")
            for c, text in enumerate(cells):
                item = QTableWidgetItem(text)
                item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                table.setItem(r, c, item)
        table.verticalHeader().setVisible(False)
        table.horizontalHeader().setStretchLastSection(True)
        table.setColumnWidth(0, 260)
        table.setMaximumHeight(170)
        table.setSelectionMode(QTableWidget.NoSelection)
        self.fonts_table = table
        lay.addWidget(table)

        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)

    def fields(self) -> dict:
        """The four editable Description values as typed (the window diffs
        against ``metadata_fields()`` and stages only real changes)."""
        return {
            "title": self.ed_title.text(),
            "author": self.ed_author.text(),
            "subject": self.ed_subject.text(),
            "keywords": self.ed_keywords.text(),
        }


# ===========================================================================
# Stamps (doc-tools M2): watermark + header/footer
# ===========================================================================
@dataclass(frozen=True)
class WatermarkOptions:
    """What ``MainWindow._do_watermark`` needs; mirrors the
    ``PDFDocument.add_watermark`` kwargs plus the 1-based page-range string
    (the dialog-seam rule -- tests construct it directly)."""

    text: str = "DRAFT"
    base14_code: str = "helv"
    fontsize: float = 48.0
    color: tuple = (0.8, 0.1, 0.1)
    opacity: float = 0.3          # 0..1
    angle: float = 45.0           # degrees; positive rises to the right
    position: str = "center"      # doctools.GRID_POSITIONS
    behind: bool = False
    pages: str = "1"              # range string for _parse_page_ranges


@dataclass(frozen=True)
class HeaderFooterOptions:
    """What ``MainWindow._do_header_footer`` needs; mirrors
    ``PDFDocument.add_header_footer`` (slot templates keep their tokens --
    substitution happens in the model, at stamp time)."""

    slots: dict = field(default_factory=dict)   # doctools.HF_SLOTS -> template
    base14_code: str = "helv"
    fontsize: float = 9.0
    color: tuple = (0.0, 0.0, 0.0)
    top: float = 30.0
    bottom: float = 18.0
    side: float = 36.0
    start_at: int = 1
    pages: str = "1"


class _ColorSwatch(QPushButton):
    """A small color-well button: paints the current color and opens the
    native QColorDialog on click. Read back via ``rgb()`` (0..1 floats, the
    model's color convention)."""

    def __init__(self, initial: tuple, parent=None):
        super().__init__(parent)
        self.setFixedSize(46, 24)
        self.setCursor(Qt.PointingHandCursor)
        self._color = QColor.fromRgbF(*initial)
        self._apply()
        self.clicked.connect(self._pick)

    def _apply(self) -> None:
        self.setStyleSheet(
            f"background-color: {self._color.name()};"
            " border: 1px solid rgba(0, 0, 0, 90); border-radius: 4px;")

    def _pick(self) -> None:
        chosen = QColorDialog.getColor(self._color, self, "Choose Color")
        if chosen.isValid():
            self._color = chosen
            self._apply()

    def rgb(self) -> tuple:
        return (self._color.redF(), self._color.greenF(), self._color.blueF())


def _font_trio_row(default_size: int) -> tuple:
    """The shared stamp font picker: base-14 family combo + Bold/Italic
    checks + a point-size spin, laid out in one row. Returns ``(layout,
    family_combo, bold_check, italic_check, size_spin)``."""
    combo = QComboBox()
    combo.addItems(list(_STAMP_FAMILIES))
    bold = QCheckBox("Bold")
    italic = QCheckBox("Italic")
    size = QSpinBox()
    size.setRange(4, 400)
    size.setValue(default_size)
    size.setSuffix(" pt")
    row = QHBoxLayout()
    row.addWidget(combo, 1)
    row.addWidget(bold)
    row.addWidget(italic)
    row.addWidget(size)
    return row, combo, bold, italic, size


class _WatermarkDialog(QDialog):
    """Document > Add Watermark... (§2.4): text, base-14 font trio + size,
    color, opacity slider, angle (with 0/45/-45 presets), 9-grid position,
    behind-content toggle, page range. Read the result via ``options()``;
    the window maps it onto ONE ``add_watermark`` structural op."""

    def __init__(self, page_count: int, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Add Watermark")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 18, 20, 16)
        lay.setSpacing(12)
        lay.addWidget(_section_header("ADD WATERMARK"))

        form = QFormLayout()
        form.setSpacing(8)
        self.ed_text = QLineEdit("DRAFT")
        form.addRow("Text:", self.ed_text)

        font_row, self.cb_family, self.chk_bold, self.chk_italic, \
            self.sp_size = _font_trio_row(48)
        form.addRow("Font:", font_row)

        self.swatch = _ColorSwatch((0.8, 0.1, 0.1))
        form.addRow("Color:", self.swatch)

        self.sl_opacity = QSlider(Qt.Horizontal)
        self.sl_opacity.setRange(5, 100)
        self.sl_opacity.setValue(30)
        self._opacity_label = QLabel("30%")
        self.sl_opacity.valueChanged.connect(
            lambda v: self._opacity_label.setText(f"{v}%"))
        op_row = QHBoxLayout()
        op_row.addWidget(self.sl_opacity, 1)
        op_row.addWidget(self._opacity_label)
        form.addRow("Opacity:", op_row)

        self.sp_angle = QSpinBox()
        self.sp_angle.setRange(-180, 180)
        self.sp_angle.setValue(45)
        self.sp_angle.setSuffix("°")
        angle_row = QHBoxLayout()
        angle_row.addWidget(self.sp_angle)
        for preset in (0, 45, -45):
            btn = QPushButton(f"{preset}°")
            btn.clicked.connect(
                lambda _=False, v=preset: self.sp_angle.setValue(v))
            angle_row.addWidget(btn)
        angle_row.addStretch(1)
        form.addRow("Angle:", angle_row)

        self.cb_position = QComboBox()
        self.cb_position.addItems(list(doctools.GRID_POSITIONS))
        self.cb_position.setCurrentText("center")
        form.addRow("Position:", self.cb_position)

        self.chk_behind = QCheckBox("Behind page content")
        form.addRow("", self.chk_behind)

        self.ed_pages = QLineEdit(
            f"1-{page_count}" if page_count > 1 else "1")
        self.ed_pages.setPlaceholderText("e.g. 1-3, 5, 8-10")
        form.addRow("Pages:", self.ed_pages)
        lay.addLayout(form)

        note = QLabel(
            "The watermark becomes ordinary page content: applying bakes "
            "pending edits first (one undoable step), and the stamped text "
            "is afterwards editable like any other text.")
        note.setWordWrap(True)
        note.setObjectName("PropertiesNote")
        lay.addWidget(note)

        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)

    def options(self) -> WatermarkOptions:
        return WatermarkOptions(
            text=self.ed_text.text(),
            base14_code=base14_code_for_family(
                self.cb_family.currentText(),
                self.chk_bold.isChecked(), self.chk_italic.isChecked()),
            fontsize=float(self.sp_size.value()),
            color=self.swatch.rgb(),
            opacity=self.sl_opacity.value() / 100.0,
            angle=float(self.sp_angle.value()),
            position=self.cb_position.currentText(),
            behind=self.chk_behind.isChecked(),
            pages=self.ed_pages.text())


class _HeaderFooterDialog(QDialog):
    """Document > Add Header & Footer... (§2.5): six slot fields (header /
    footer x left / center / right), token insert buttons, start-numbering
    spin, font trio + size, color, margins, page range, and a live preview
    of the substituted page-1 strings. Tokens substitute at STAMP time --
    static content; later reorders do not renumber (stated below)."""

    def __init__(self, page_count: int, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Add Header & Footer")
        self.setMinimumWidth(560)
        self._page_count = page_count
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 18, 20, 16)
        lay.setSpacing(12)
        lay.addWidget(_section_header("ADD HEADER & FOOTER"))

        # --- the six slot fields, a 2x3 grid with alignment captions ------
        grid = QGridLayout()
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(6)
        self.slot_edits: dict[str, QLineEdit] = {}
        for col, caption in enumerate(("Left", "Center", "Right")):
            head = QLabel(caption)
            head.setAlignment(Qt.AlignHCenter)
            grid.addWidget(head, 0, col + 1)
        for row, band in enumerate(("header", "footer")):
            grid.addWidget(QLabel(band.capitalize() + ":"), row + 1, 0)
            for col, align in enumerate(("left", "center", "right")):
                edit = QLineEdit()
                edit.installEventFilter(self)
                edit.textChanged.connect(self._update_preview)
                self.slot_edits[f"{band}_{align}"] = edit
                grid.addWidget(edit, row + 1, col + 1)
        lay.addLayout(grid)
        # Token insertion targets the last-focused slot (default:
        # header_center, the most common home for "Page N of M").
        self._active_slot = self.slot_edits["header_center"]

        token_row = QHBoxLayout()
        token_row.addWidget(QLabel("Insert token:"))
        for token in ("{page}", "{pages}", "{date}"):
            btn = QPushButton(token)
            btn.clicked.connect(
                lambda _=False, t=token: self._insert_token(t))
            token_row.addWidget(btn)
        token_row.addStretch(1)
        lay.addLayout(token_row)

        form = QFormLayout()
        form.setSpacing(8)
        self.sp_start = QSpinBox()
        self.sp_start.setRange(1, 99999)
        self.sp_start.setValue(1)
        self.sp_start.valueChanged.connect(self._update_preview)
        form.addRow("Start numbering at:", self.sp_start)

        font_row, self.cb_family, self.chk_bold, self.chk_italic, \
            self.sp_size = _font_trio_row(9)
        form.addRow("Font:", font_row)

        self.swatch = _ColorSwatch((0.0, 0.0, 0.0))
        form.addRow("Color:", self.swatch)

        margin_row = QHBoxLayout()
        self.sp_top = QDoubleSpinBox()
        self.sp_bottom = QDoubleSpinBox()
        self.sp_side = QDoubleSpinBox()
        for spin, label, default in ((self.sp_top, "Top", 30.0),
                                     (self.sp_bottom, "Bottom", 18.0),
                                     (self.sp_side, "Side", 36.0)):
            spin.setRange(0.0, 288.0)
            spin.setDecimals(1)
            spin.setValue(default)
            spin.setSuffix(" pt")
            margin_row.addWidget(QLabel(label + ":"))
            margin_row.addWidget(spin)
        margin_row.addStretch(1)
        form.addRow("Margins:", margin_row)

        self.ed_pages = QLineEdit(
            f"1-{page_count}" if page_count > 1 else "1")
        self.ed_pages.setPlaceholderText("e.g. 1-3, 5, 8-10")
        form.addRow("Pages:", self.ed_pages)
        lay.addLayout(form)

        self.preview = QLabel()
        self.preview.setWordWrap(True)
        self.preview.setObjectName("PropertiesNote")
        lay.addWidget(self.preview)
        self._update_preview()

        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)

    def eventFilter(self, obj, event):
        """Track the last-focused slot field so the token buttons insert
        where the user is typing."""
        if event.type() == QEvent.FocusIn and obj in self.slot_edits.values():
            self._active_slot = obj
        return super().eventFilter(obj, event)

    def _insert_token(self, token: str) -> None:
        self._active_slot.insert(token)
        self._active_slot.setFocus()

    def _update_preview(self) -> None:
        """The substituted FIRST-page strings, refreshed live (the dialog's
        WYSIWYG hint); also states the static-content rule."""
        date = datetime.date.today().strftime("%Y-%m-%d")
        parts = []
        for slot in doctools.HF_SLOTS:
            template = self.slot_edits[slot].text()
            if template.strip():
                parts.append(doctools.substitute_tokens(
                    template, page_no=self.sp_start.value(),
                    total=self._page_count, date=date))
        shown = "  ·  ".join(parts) if parts else "(nothing to stamp yet)"
        self.preview.setText(
            f"First page preview: {shown}\n"
            "Tokens are filled in when the stamp is applied; reordering "
            "pages later does not renumber.")

    def options(self) -> HeaderFooterOptions:
        return HeaderFooterOptions(
            slots={k: e.text() for k, e in self.slot_edits.items()},
            base14_code=base14_code_for_family(
                self.cb_family.currentText(),
                self.chk_bold.isChecked(), self.chk_italic.isChecked()),
            fontsize=float(self.sp_size.value()),
            color=self.swatch.rgb(),
            top=float(self.sp_top.value()),
            bottom=float(self.sp_bottom.value()),
            side=float(self.sp_side.value()),
            start_at=int(self.sp_start.value()),
            pages=self.ed_pages.text())


# ===========================================================================
# Crop (doc-tools M3)
# ===========================================================================
class _CropDialog(QDialog):
    """Document > Crop Pages... confirm step (§2.6): the drawn rect in PDF
    points + scope radios (This page / All pages) + Apply/Cancel. Pure
    chrome: the window's ``_on_crop_rect_selected`` constructs it (through
    the injectable ``_crop_scope_provider`` seam) after the canvas drag and
    maps the result onto ONE ``crop_pages`` structural op via
    ``_do_crop_apply``, which tests call directly -- the dialog-seam rule."""

    def __init__(self, page_index: int, rect: tuple, page_count: int,
                 parent=None):
        super().__init__(parent)
        self.setWindowTitle("Crop Pages")
        x0, y0, x1, y1 = rect
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 18, 20, 16)
        lay.setSpacing(12)
        lay.addWidget(_section_header("CROP PAGES"))

        info = QLabel(
            f"Selected area: {x1 - x0:.1f} × {y1 - y0:.1f} pt at "
            f"({x0:.1f}, {y0:.1f}) on page {page_index + 1}.")
        info.setWordWrap(True)
        lay.addWidget(info)

        self.rb_page = QRadioButton(f"This page (page {page_index + 1})")
        self.rb_all = QRadioButton(f"All {page_count} pages")
        self.rb_page.setChecked(True)
        lay.addWidget(self.rb_page)
        lay.addWidget(self.rb_all)

        note = QLabel(
            "The same area is applied per page and clamped to each page's "
            "bounds; pages where it falls below 36 pt are skipped. Applying "
            "bakes pending edits first (one undoable step).")
        note.setWordWrap(True)
        note.setObjectName("PropertiesNote")
        lay.addWidget(note)

        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("Apply")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)

    def scope(self) -> str:
        """``"page"`` (just the drawn page) or ``"all"`` -- the
        ``_do_crop_apply`` scope values."""
        return "all" if self.rb_all.isChecked() else "page"


# ===========================================================================
# Security (doc-tools M4)
# ===========================================================================
@dataclass(frozen=True)
class SecurityOptions:
    """What ``MainWindow._do_security`` needs (the dialog-seam rule -- tests
    construct it directly). ``action`` is ``"set"`` (protect with
    ``password``) or ``"remove"`` (drop the encryption on the next save)."""

    action: str            # "set" | "remove"
    password: str = ""


class _SecurityDialog(QDialog):
    """Document > Security... (§2.7): a status line, set/change password
    (password + confirm, AES-256), and Remove Security. Read the result via
    ``options()``. Security is a PENDING SAVE OPTION on the model -- not an
    undo entry -- so this dialog is also the REVERT surface (the note says
    so): reopen it to change or remove an unsaved security change."""

    def __init__(self, encrypted: bool, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Security")
        self._action = "set"
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 18, 20, 16)
        lay.setSpacing(12)
        lay.addWidget(_section_header("SECURITY"))

        self.status_label = QLabel(
            "AES-256 password set (applies when the file is saved)"
            if encrypted else "Not encrypted")
        lay.addWidget(self.status_label)

        form = QFormLayout()
        form.setSpacing(8)
        self.ed_password = QLineEdit()
        self.ed_password.setEchoMode(QLineEdit.Password)
        self.ed_confirm = QLineEdit()
        self.ed_confirm.setEchoMode(QLineEdit.Password)
        form.addRow("Password:", self.ed_password)
        form.addRow("Confirm:", self.ed_confirm)
        lay.addLayout(form)

        note = QLabel(
            "Security changes apply on the next save and are not on the "
            "undo stack. Reopen this dialog to change or remove them. "
            "The password protects opening the file (AES-256).")
        note.setWordWrap(True)
        note.setObjectName("PropertiesNote")
        lay.addWidget(note)

        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self._ok = buttons.button(QDialogButtonBox.Ok)
        self._ok.setText("Set Password")
        # Remove Security: its own accept path (ActionRole emits no
        # accepted/rejected of its own), enabled only when there is
        # encryption to remove.
        self.btn_remove = QPushButton("Remove Security")
        self.btn_remove.setEnabled(encrypted)
        buttons.addButton(self.btn_remove, QDialogButtonBox.ActionRole)
        self.btn_remove.clicked.connect(self._remove)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)

        # Set Password only arms once the two fields agree on a non-empty
        # password (the inline validation; no separate error popup).
        self.ed_password.textChanged.connect(self._sync_ok)
        self.ed_confirm.textChanged.connect(self._sync_ok)
        self._sync_ok()

    def _sync_ok(self) -> None:
        pw = self.ed_password.text()
        self._ok.setEnabled(bool(pw) and pw == self.ed_confirm.text())

    def _remove(self) -> None:
        self._action = "remove"
        self.accept()

    def options(self) -> SecurityOptions:
        return SecurityOptions(action=self._action,
                               password=self.ed_password.text())
