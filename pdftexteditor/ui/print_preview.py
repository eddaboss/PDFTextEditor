"""Clay print dialog -- a custom, app-native print preview WITH settings.

Qt's stock QPrintPreviewDialog looks nothing like the app and Windows has no
native print preview to hand off to, so this is the full print experience on
Windows/Linux (macOS keeps its native panel). Left: a live page preview on the
warm Clay gutter. Right: a settings panel -- Destination (printer), Pages,
Copies, Color -- exactly the controls a real print dialog needs. Hitting Print
configures the caller's QPrinter in place and prints directly.

Rendering rides ``render_with_edits`` -- the same save pipeline the print job
uses -- so the preview is WYSIWYG. Only the current page is rendered, so it stays
responsive on any document size.
"""
from __future__ import annotations

from PySide6.QtCore import Qt, QSize, QTimer, QPoint, QRectF
from PySide6.QtGui import (QColor, QImage, QPageLayout, QPageSize, QPainter,
                           QPalette, QPixmap)
from PySide6.QtPrintSupport import QPrinter, QPrinterInfo
from PySide6.QtWidgets import (
    QButtonGroup, QCheckBox, QFrame, QGridLayout,
    QHBoxLayout, QLabel, QLineEdit, QMenu, QPushButton, QRadioButton,
    QScrollArea, QSpinBox, QStyleFactory, QToolButton, QVBoxLayout, QWidget,
)

from . import theme
from .icons import make_icon


def _parse_ranges(text: str, n: int) -> list[int]:
    """'1-3, 5' -> [0,1,2,4] (0-based, in order). ValueError on bad/out-of-range
    input, so the dialog can show the message inline."""
    out: list[int] = []
    if not text.strip():
        raise ValueError("Enter a page or range.")
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, _, b = part.partition("-")
            a, b = a.strip(), b.strip()
            if not (a.isdigit() and b.isdigit()):
                raise ValueError(f"'{part}' isn't a valid range.")
            s, e = int(a), int(b)
        else:
            if not part.isdigit():
                raise ValueError(f"'{part}' isn't a valid page.")
            s = e = int(part)
        if not (1 <= s <= n) or not (1 <= e <= n):
            raise ValueError(f"Pages must be between 1 and {n}.")
        step = 1 if e >= s else -1
        out.extend(range(s, e + step, step))
    if not out:                       # separator-only input, e.g. "," or ", ,"
        raise ValueError("Enter a page or range.")
    return [p - 1 for p in out]


def _qss() -> str:
    return f"""
    #PvRoot {{ background: {theme.PANEL_BG}; }}
    #PvHeader {{ background: {theme.CHROME_BG};
                 border-bottom: 1px solid {theme.CHROME_BORDER}; }}
    #PvTitle {{ color: {theme.TEXT_PRIMARY}; font-size: 17px; font-weight: 600; }}
    #PvSub {{ color: {theme.TEXT_SECONDARY}; font-size: 12px; }}
    #PvScroll {{ border: none; background: {theme.CANVAS_BG}; }}
    #PvGutter {{ background: {theme.CANVAS_BG}; }}
    #PvBar {{ background: {theme.CHROME_BG};
              border-top: 1px solid {theme.CHROME_BORDER}; }}
    #PvPage, #PvZoom {{ color: {theme.TEXT_SECONDARY}; font-size: 13px; }}
    #PvIconBtn {{ background: transparent; border: none; border-radius: 8px; }}
    #PvIconBtn:hover {{ background: {theme.ACCENT_HOVER}; }}
    #PvPanel {{ background: {theme.PANEL_BG};
                border-left: 1px solid {theme.CHROME_BORDER}; }}
    #PvSettings, #PvSettingsBody {{ background: {theme.PANEL_BG}; border: none; }}
    #PvFooterBar {{ background: {theme.PANEL_BG};
                    border-top: 1px solid {theme.CHROME_BORDER}; }}
    #PvPanel QCheckBox {{ color: {theme.TEXT_PRIMARY}; font-size: 13px;
                          spacing: 8px; padding: 2px 0; }}
    #PvPanel QCheckBox:disabled {{ color: {theme.TEXT_TERTIARY}; }}
    #PvPanel QCheckBox::indicator {{ width: 15px; height: 15px; border-radius: 4px;
                          border: 1px solid {theme.BORDER_STRONG};
                          background: {theme.CONTROL_FILL}; }}
    #PvPanel QCheckBox::indicator:checked {{ background: {theme.ACCENT_FILL};
                          border-color: {theme.ACCENT_FILL}; }}
    #PvEyebrow {{ color: {theme.PANEL_HEADER}; font-size: 11px; font-weight: 700;
                  letter-spacing: 1px; }}
    #PvErr {{ color: {theme.ACCENT}; font-size: 12px; }}
    #PvPanel QLineEdit {{
        background: {theme.CONTROL_FILL}; color: {theme.TEXT_PRIMARY};
        border: 1px solid {theme.BORDER_STRONG}; border-radius: 8px;
        padding: 6px 10px; min-height: 20px; font-size: 13px; }}
    #PvPanel QLineEdit:focus {{ border: 1px solid {theme.ACCENT_BORDER}; }}
    /* Destination select: a left-aligned button with a Clay chevron overlaid. */
    #PvSelect {{ background: {theme.CONTROL_FILL}; color: {theme.TEXT_PRIMARY};
        border: 1px solid {theme.BORDER_STRONG}; border-radius: 8px;
        padding: 8px 32px 8px 12px; text-align: left; font-size: 13px;
        min-height: 20px; }}
    #PvSelect:hover {{ border: 1px solid {theme.ACCENT_BORDER}; }}
    #PvSelect:focus {{ border: 1px solid {theme.ACCENT_BORDER}; outline: none; }}
    #PvPanel QRadioButton {{ color: {theme.TEXT_PRIMARY}; font-size: 13px;
                             spacing: 7px; padding: 2px 0; }}
    #PvGhost {{ background: transparent; color: {theme.TEXT_SECONDARY};
                border: 1px solid {theme.BORDER_STRONG}; border-radius: 8px;
                padding: 8px 14px; font-size: 12px; }}
    #PvGhost:hover {{ border: 1px solid {theme.ACCENT_BORDER};
                color: {theme.TEXT_PRIMARY}; }}
    #PvCancel {{ background: {theme.CONTROL_FILL}; color: {theme.TEXT_PRIMARY};
                 border: 1px solid {theme.BORDER_STRONG}; border-radius: 8px;
                 padding: 9px 18px; font-size: 13px; font-weight: 500; }}
    #PvCancel:hover {{ background: {theme.CARD_BG_HOVER}; }}
    #PvPrint {{ background: {theme.ACCENT_FILL}; color: #FFFFFF; border: none;
                border-radius: 8px; padding: 9px 22px; font-size: 13px;
                font-weight: 600; }}
    #PvPrint:hover {{ background: {theme.ACCENT_PRESSED}; }}
    #PvPrint:pressed {{ background: {theme.ACCENT_DEEP}; }}
    """


def _spin_qss() -> str:
    return f"""
    QSpinBox {{ background: {theme.CONTROL_FILL}; color: {theme.TEXT_PRIMARY};
        border: 1px solid {theme.BORDER_STRONG}; border-radius: 8px;
        padding: 6px 30px 6px 12px; min-height: 20px; font-size: 13px; }}
    QSpinBox:focus {{ border: 1px solid {theme.ACCENT_BORDER}; }}
    /* Hide the native steppers (they broke the rounded corner); a Clay up/down
       stepper is overlaid in code instead. */
    QSpinBox::up-button, QSpinBox::down-button {{ width: 0; border: none;
        background: transparent; }}
    QSpinBox::up-arrow, QSpinBox::down-arrow {{ image: none; width: 0; height: 0; }}
    """


# N-up grid as (rows, cols) -- must match _paint_pages in main_window.
_GRID = {1: (1, 1), 2: (1, 2), 4: (2, 2), 6: (2, 3), 9: (3, 3)}


class ClayPrintPreview(QWidget):
    """Custom Clay print view, embedded as an in-app PAGE (not a separate
    window). The host swaps it into the content stack and passes callbacks:
      * ``on_print``  -- fired when Print is hit; read ``selected_pages`` (0-based
        list or None=all) and ``grayscale``; destination/copies/color are already
        applied to the passed ``printer``.
      * ``on_cancel`` -- fired on Cancel/Escape (return to the editor).
      * ``on_system`` -- fired by the "System dialog" button (hand off to the
        native OS print dialog)."""

    def __init__(self, printer, doc, subtitle: str = "", *, on_print=None,
                 on_cancel=None, on_system=None, parent=None):
        super().__init__(parent)
        self._printer = printer
        self._doc = doc
        self._n = max(1, doc.page_count)
        self._sheet_idx = 0          # current SHEET (a sheet may hold N pages)
        self._scale = 1.0            # multiplier ON TOP of fit-to-view (1.0 = Fit)
        self._pts: dict[int, tuple[int, int]] = {}
        self._color_supported = True  # set from the printer in _apply_color_caps
        self._insets_cache: dict = {}  # (printer, paper, orient) -> margin fractions
        self._subtitle = subtitle
        self._on_print_cb = on_print
        self._on_cancel_cb = on_cancel
        self._on_system_cb = on_system
        # Native macOS QComboBox/QSpinBox IGNORE QSS sub-control styling (the
        # drop-down bar + steppers render as native chrome no matter what). Fusion
        # honours QSS identically on every platform, so we force it on just those
        # two widgets to get the flat Clay caret/steppers. Kept on self so the
        # QStyle outlives the widgets.
        self._fusion = QStyleFactory.create("Fusion")
        self.selected_pages = None
        self.grayscale = False
        # Native-parity print options (applied on Print).
        self.paper_size = None                        # QPageSize | None (default)
        self.orientation = None                       # QPageLayout.Orientation | None (auto)
        self.scale_mode = "fit"                       # "fit" | "actual"
        self.nup = 1                                  # pages per sheet
        self.duplex = QPrinter.DuplexMode.DuplexNone
        self.collate = False
        # Coalesce the flurry of resizeEvents during a window drag into ONE
        # re-render, so a heavily-edited page (slow render_with_edits) does not
        # re-bake dozens of times a second.
        self._resize_timer = QTimer(self)
        self._resize_timer.setSingleShot(True)
        self._resize_timer.setInterval(80)
        self._resize_timer.timeout.connect(self._rebuild_sheets)
        self.setObjectName("PvRoot")
        self.setAutoFillBackground(True)   # opaque -> fully covers what's behind
        self._build()
        self.setStyleSheet(_qss())
        self._apply_color_caps()           # gate Color to the printer + preselect
        self._apply_duplex_caps()          # populate + gate Two-sided to the printer
        self._rebuild_sheets()

    # ---- layout -----------------------------------------------------------
    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(self._header())
        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)
        body.addWidget(self._preview_col(), 1)
        body.addWidget(self._settings_panel())
        root.addLayout(body, 1)

    def _header(self) -> QWidget:
        bar = QFrame()
        bar.setObjectName("PvHeader")
        h = QHBoxLayout(bar)
        h.setContentsMargins(22, 15, 16, 14)
        col = QVBoxLayout()
        col.setSpacing(1)
        title = QLabel("Print")
        title.setObjectName("PvTitle")
        col.addWidget(title)
        sub = self._subtitle or f"{self._n} page{'s' if self._n != 1 else ''}"
        lbl = QLabel(sub)
        lbl.setObjectName("PvSub")
        col.addWidget(lbl)
        h.addLayout(col)
        h.addStretch(1)
        return bar

    def _preview_col(self) -> QWidget:
        col = QWidget()
        v = QVBoxLayout(col)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)
        self._scroll = QScrollArea()
        self._scroll.setObjectName("PvScroll")
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.NoFrame)
        self._scroll.setAlignment(Qt.AlignHCenter | Qt.AlignTop)
        pal = self._scroll.viewport().palette()
        pal.setColor(QPalette.Window, QColor(theme.CANVAS_BG))
        self._scroll.viewport().setPalette(pal)
        self._scroll.viewport().setAutoFillBackground(True)
        self._scroll.verticalScrollBar().valueChanged.connect(self._update_indicator)
        # Every sheet is stacked in this holder and the area scrolls through them.
        holder = QWidget()
        holder.setObjectName("PvGutter")
        self._sheets_layout = QVBoxLayout(holder)
        self._sheets_layout.setContentsMargins(28, 28, 28, 28)
        self._sheets_layout.setSpacing(22)
        self._sheets_layout.setAlignment(Qt.AlignHCenter | Qt.AlignTop)
        self._sheet_labels: list = []
        self._scroll.setWidget(holder)
        v.addWidget(self._scroll, 1)
        v.addWidget(self._preview_bar())
        return col

    def _preview_bar(self) -> QWidget:
        bar = QFrame()
        bar.setObjectName("PvBar")
        h = QHBoxLayout(bar)
        h.setContentsMargins(14, 10, 14, 10)
        h.setSpacing(6)
        self._prev = self._icon_btn("prev", self._go_prev)
        self._pagelbl = QLabel()
        self._pagelbl.setObjectName("PvPage")
        self._pagelbl.setAlignment(Qt.AlignCenter)
        self._pagelbl.setMinimumWidth(58)
        self._next = self._icon_btn("next", self._go_next)
        h.addWidget(self._prev)
        h.addWidget(self._pagelbl)
        h.addWidget(self._next)
        h.addStretch(1)
        h.addWidget(self._icon_btn("zoom_out", self._zoom_out))
        self._zoomlbl = QLabel("Fit")
        self._zoomlbl.setObjectName("PvZoom")
        self._zoomlbl.setAlignment(Qt.AlignCenter)
        self._zoomlbl.setMinimumWidth(46)
        h.addWidget(self._zoomlbl)
        h.addWidget(self._icon_btn("zoom_in", self._zoom_in))
        return bar

    def _settings_panel(self) -> QWidget:
        panel = QFrame()
        panel.setObjectName("PvPanel")
        panel.setFixedWidth(320)
        outer = QVBoxLayout(panel)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # The settings scroll; the Cancel/Print actions stay pinned at the bottom.
        scroll = QScrollArea()
        scroll.setObjectName("PvSettings")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        pal = scroll.viewport().palette()
        pal.setColor(QPalette.Window, QColor(theme.PANEL_BG))
        scroll.viewport().setPalette(pal)
        scroll.viewport().setAutoFillBackground(True)
        body = QWidget()
        body.setObjectName("PvSettingsBody")
        v = QVBoxLayout(body)
        v.setContentsMargins(22, 22, 22, 8)
        v.setSpacing(8)

        # --- Destination: a Clay button + menu (QComboBox's native arrow is
        # untameable on the macOS style), styled and popping a QMenu of printers.
        v.addWidget(self._eyebrow("DESTINATION"))
        self._dest_infos = list(QPrinterInfo.availablePrinters())
        self._dest_info = None
        self._dest_menu = QMenu(self)
        default = QPrinterInfo.defaultPrinter()
        for info in self._dest_infos:
            act = self._dest_menu.addAction(info.description() or info.printerName())
            act.triggered.connect(lambda _=False, i=info: self._set_dest(i))
            if self._dest_info is None or (
                    not default.isNull()
                    and info.printerName() == default.printerName()):
                self._dest_info = info
        self._dest_btn = QPushButton("Default printer")
        self._dest_btn.setObjectName("PvSelect")
        self._dest_btn.setCursor(Qt.PointingHandCursor)
        self._dest_btn.clicked.connect(self._open_dest_menu)
        if self._dest_info is not None:
            self._set_dest(self._dest_info)
        v.addWidget(self._caret_overlay(self._dest_btn, self._open_dest_menu))
        v.addSpacing(6)

        # --- Pages ---
        v.addWidget(self._eyebrow("PAGES"))
        self._pg_all = QRadioButton("All")
        self._pg_all.setChecked(True)
        self._pg_custom = QRadioButton("Pages")
        grp = QButtonGroup(self)
        grp.addButton(self._pg_all)
        grp.addButton(self._pg_custom)
        v.addWidget(self._pg_all)
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)
        row.addWidget(self._pg_custom)
        self._range = QLineEdit()
        self._range.setPlaceholderText("e.g. 1-3, 5")
        self._range.setEnabled(False)
        self._range.textEdited.connect(lambda _: self._pg_custom.setChecked(True))
        # Debounced (shares the resize timer): typing a range must not re-bake
        # every page on each keystroke -- render_with_edits is slow on edited pages.
        self._range.textEdited.connect(lambda _: self._resize_timer.start())
        row.addWidget(self._range, 1)
        v.addLayout(row)
        self._pg_custom.toggled.connect(self._range.setEnabled)
        self._pg_custom.toggled.connect(
            lambda on: self._range.setFocus() if on else None)
        self._pg_all.toggled.connect(lambda _: self._refresh_preview())
        self._err = QLabel("")
        self._err.setObjectName("PvErr")
        self._err.setWordWrap(True)
        self._err.hide()
        v.addWidget(self._err)
        v.addSpacing(6)

        # --- Copies (+ Collate, meaningful only for >1 copy) ---
        v.addWidget(self._eyebrow("COPIES"))
        self._copies = QSpinBox()
        if self._fusion is not None:
            self._copies.setStyle(self._fusion)
        self._copies.setStyleSheet(_spin_qss())
        self._copies.setRange(1, 99)
        self._copies.setValue(1)
        v.addWidget(self._spin_with_stepper(self._copies))
        self._collate = QCheckBox("Collate")
        if self._fusion is not None:
            self._collate.setStyle(self._fusion)
        self._collate.setEnabled(False)
        self._collate.toggled.connect(lambda on: setattr(self, "collate", on))
        self._copies.valueChanged.connect(
            lambda n: self._collate.setEnabled(n > 1))
        v.addWidget(self._collate)
        v.addSpacing(6)

        # --- Color (the whole section is hidden on a mono-only printer, where
        # there is no choice to make -- see _apply_color_caps) ---
        self._color_box = QWidget()
        cbl = QVBoxLayout(self._color_box)
        cbl.setContentsMargins(0, 0, 0, 6)
        cbl.setSpacing(8)
        cbl.addWidget(self._eyebrow("COLOR"))
        self._color = QRadioButton("Color")
        self._color.setChecked(True)
        self._bw = QRadioButton("Black && white")   # && -> a literal ampersand
        cgrp = QButtonGroup(self)
        cgrp.addButton(self._color)
        cgrp.addButton(self._bw)
        self._bw.toggled.connect(lambda _: self._refresh_preview())
        cbl.addWidget(self._color)
        cbl.addWidget(self._bw)
        v.addWidget(self._color_box)

        # --- Paper size ---
        v.addWidget(self._eyebrow("PAPER SIZE"))
        _sizes = [("Letter", QPageSize.PageSizeId.Letter),
                  ("Legal", QPageSize.PageSizeId.Legal),
                  ("A4", QPageSize.PageSizeId.A4),
                  ("A3", QPageSize.PageSizeId.A3),
                  ("A5", QPageSize.PageSizeId.A5),
                  ("Tabloid", QPageSize.PageSizeId.Tabloid)]
        cur_id = self._printer.pageLayout().pageSize().id()
        init_paper = next((l for l, s in _sizes if QPageSize(s).id() == cur_id), "A4")
        self.paper_size = QPageSize(dict(_sizes)[init_paper])
        v.addWidget(self._dropdown(
            [(l, QPageSize(s)) for l, s in _sizes], init_paper,
            lambda ps: setattr(self, "paper_size", ps)))
        v.addSpacing(6)

        # --- Orientation (Auto = per-page shape) ---
        v.addWidget(self._eyebrow("ORIENTATION"))
        _O = QPageLayout.Orientation
        v.addWidget(self._dropdown(
            [("Auto", None), ("Portrait", _O.Portrait), ("Landscape", _O.Landscape)],
            "Auto", lambda o: setattr(self, "orientation", o)))
        v.addSpacing(6)

        # --- Scale ---
        v.addWidget(self._eyebrow("SCALE"))
        v.addWidget(self._dropdown(
            [("Fit to page", "fit"), ("Actual size", "actual")],
            "Fit to page", lambda s: setattr(self, "scale_mode", s)))
        v.addSpacing(6)

        # --- Pages per sheet (N-up) ---
        v.addWidget(self._eyebrow("PAGES PER SHEET"))
        v.addWidget(self._dropdown(
            [(str(n), n) for n in (1, 2, 4, 6, 9)], "1",
            lambda n: setattr(self, "nup", n)))

        # --- Two-sided (populated + gated to the printer's duplex support in
        # _apply_duplex_caps, and re-gated on every destination change -- same
        # pattern as the Color section above) ---
        self._duplex_box = QWidget()
        _dbl = QVBoxLayout(self._duplex_box)
        _dbl.setContentsMargins(0, 6, 0, 0)
        _dbl.setSpacing(8)
        v.addWidget(self._duplex_box)

        v.addStretch(1)
        scroll.setWidget(body)
        outer.addWidget(scroll, 1)

        # --- Pinned footer: system-dialog escape + Cancel / Print ---
        footer = QWidget()
        footer.setObjectName("PvFooterBar")
        fl = QVBoxLayout(footer)
        fl.setContentsMargins(22, 12, 22, 20)
        fl.setSpacing(8)
        sysbtn = QPushButton("Use system dialog")
        sysbtn.setObjectName("PvGhost")
        sysbtn.setCursor(Qt.PointingHandCursor)
        sysbtn.clicked.connect(self._on_system)
        fl.addWidget(sysbtn)
        actions = QHBoxLayout()
        actions.setContentsMargins(0, 2, 0, 0)
        actions.setSpacing(8)
        actions.addStretch(1)
        cancel = QPushButton("Cancel")
        cancel.setObjectName("PvCancel")
        cancel.setCursor(Qt.PointingHandCursor)
        cancel.clicked.connect(self._on_cancel)
        printb = QPushButton("  Print")
        printb.setObjectName("PvPrint")
        printb.setIcon(make_icon("printer", "#FFFFFF"))
        printb.setIconSize(QSize(18, 18))
        printb.setCursor(Qt.PointingHandCursor)
        printb.setDefault(True)
        printb.clicked.connect(self._on_print)
        actions.addWidget(cancel)
        actions.addWidget(printb)
        fl.addLayout(actions)
        outer.addWidget(footer)
        return panel

    def _dropdown(self, options, initial_label, on_select) -> QWidget:
        """A Clay select: a #PvSelect button + styled QMenu of (label, value)
        pairs. On choose it sets the button label and calls on_select(value)."""
        btn = QPushButton(initial_label)
        btn.setObjectName("PvSelect")
        btn.setCursor(Qt.PointingHandCursor)
        menu = QMenu(self)
        for label, value in options:
            act = menu.addAction(label)
            act.triggered.connect(
                lambda _=False, l=label, v=value:
                    (btn.setText(l), on_select(v), self._refresh_preview()))

        def _open():
            menu.setMinimumWidth(btn.width())
            menu.exec(btn.mapToGlobal(QPoint(0, btn.height() + 2)))
        btn.clicked.connect(_open)
        return self._caret_overlay(btn, _open)

    def _eyebrow(self, text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setObjectName("PvEyebrow")
        return lbl

    def _icon_btn(self, name: str, slot) -> QPushButton:
        b = QPushButton()
        b.setObjectName("PvIconBtn")
        b.setIcon(make_icon(name, theme.TOOLBAR_ICON))
        b.setIconSize(QSize(20, 20))
        b.setFixedSize(34, 34)
        b.setCursor(Qt.PointingHandCursor)
        b.clicked.connect(slot)
        return b

    def _caret_overlay(self, widget: QWidget, on_click) -> QWidget:
        """Overlay a Clay chevron at the right of ``widget`` (a select button).
        The chevron is a QToolButton (same clean-rendering mechanism as the spin
        stepper) that triggers ``on_click`` -- e.g. opening the menu."""
        wrap = QWidget()
        g = QGridLayout(wrap)
        g.setContentsMargins(0, 0, 0, 0)
        g.addWidget(widget, 0, 0)
        chev = QToolButton()
        chev.setIcon(make_icon("chevron_down", theme.TEXT_SECONDARY))
        chev.setIconSize(QSize(14, 14))
        chev.setCursor(Qt.PointingHandCursor)
        chev.setStyleSheet("QToolButton { border: none; background: transparent;"
                           " margin-right: 9px; }")
        chev.clicked.connect(on_click)
        g.addWidget(chev, 0, 0, Qt.AlignRight | Qt.AlignVCenter)
        return wrap

    def _set_dest(self, info) -> None:
        self._dest_info = info
        self._dest_btn.setText(info.description() or info.printerName())
        if hasattr(self, "_color"):        # after the initial build
            self._apply_color_caps()       # re-gate Color to the new printer
            self._apply_duplex_caps()      # re-gate Two-sided to the new printer
            self._refresh_preview()        # mono printer -> preview goes greyscale

    def _open_dest_menu(self) -> None:
        if not self._dest_infos:
            return
        self._dest_menu.setMinimumWidth(self._dest_btn.width())
        self._dest_menu.exec(self._dest_btn.mapToGlobal(
            QPoint(0, self._dest_btn.height() + 2)))

    def _spin_with_stepper(self, spin: QSpinBox) -> QWidget:
        """Overlay a compact Clay up/down stepper (native steppers hidden)."""
        wrap = QWidget()
        g = QGridLayout(wrap)
        g.setContentsMargins(0, 0, 0, 0)
        g.addWidget(spin, 0, 0)
        col = QWidget()
        col.setStyleSheet("background: transparent;")
        cv = QVBoxLayout(col)
        cv.setContentsMargins(0, 3, 9, 3)
        cv.setSpacing(0)
        btn_qss = ("QToolButton {{ border: none; background: transparent; }}"
                   "QToolButton:hover {{ background: {h}; border-radius: 4px; }}"
                   ).format(h=theme.ACCENT_HOVER)
        for icon_name, step in (("chevron_up", spin.stepUp),
                                ("chevron_down", spin.stepDown)):
            b = QToolButton()
            b.setIcon(make_icon(icon_name, theme.TEXT_SECONDARY))
            b.setIconSize(QSize(13, 13))
            b.setCursor(Qt.PointingHandCursor)
            b.setStyleSheet(btn_qss)
            b.setAutoRepeat(True)
            b.clicked.connect(step)
            cv.addWidget(b)
        g.addWidget(col, 0, 0, Qt.AlignRight | Qt.AlignVCenter)
        return wrap

    # ---- WYSIWYG rendering: compose the SHEET the printer will output ------
    def _points(self, i: int) -> tuple[int, int]:
        if i not in self._pts:
            p = self._doc.render_with_edits(i, 1.0)   # 72 dpi -> px == points
            self._pts[i] = (max(1, p.width), max(1, p.height))
        return self._pts[i]

    def _print_pages(self) -> list:
        """0-based page indices that will actually print (the Pages selection)."""
        if self._pg_custom.isChecked():
            try:
                pgs = _parse_ranges(self._range.text(), self._n)
            except ValueError:
                pgs = list(range(self._n))     # mid-typing/invalid -> preview all
        else:
            pgs = list(range(self._n))
        pgs = [p for p in pgs if 0 <= p < self._n]
        return pgs or [0]

    def _sheets(self) -> list:
        pgs = self._print_pages()
        return [pgs[k:k + self.nup] for k in range(0, len(pgs), self.nup)]

    def _effective_grayscale(self) -> bool:
        # Mono when the user picked B&W OR the printer can't do colour.
        return self._bw.isChecked() or not self._color_supported

    def _effective_orientation(self, page_indices):
        _O = QPageLayout.Orientation
        if self.orientation is not None:
            return self.orientation
        if self.nup == 1:
            pw, ph = self._points(page_indices[0])
            return _O.Landscape if pw > ph else _O.Portrait
        rows, cols = _GRID.get(self.nup, (1, 1))
        return _O.Landscape if cols > rows else _O.Portrait

    def _render_page(self, i: int, px_per_pt: float, gray: bool):
        pix = self._doc.render_with_edits(i, px_per_pt)
        img = QImage(bytes(pix.samples), pix.width, pix.height, pix.stride,
                     QImage.Format.Format_RGB888).copy()
        if gray:
            img = img.convertToFormat(QImage.Format.Format_Grayscale8)
        return img

    def _printable_insets(self, orient):
        """(left, top, right, bottom) fractions of the paper OUTSIDE the printable
        area, for the current paper + orientation on the selected printer -- the
        printer's unprintable hardware margin, so the preview insets content
        exactly where the print does."""
        ps = self.paper_size or QPageSize(QPageSize.PageSizeId.Letter)
        key = (self._dest_info.printerName() if self._dest_info else "",
               ps.id(), orient)
        cached = self._insets_cache.get(key)
        if cached is not None:
            return cached
        q = QPrinter(QPrinter.PrinterMode.HighResolution)
        if self._dest_info is not None:
            q.setPrinterName(self._dest_info.printerName())
        q.setPageSize(ps)
        q.setPageOrientation(orient)
        lay = q.pageLayout()
        U = QPageLayout.Unit.Point
        full, paint = lay.fullRect(U), lay.paintRect(U)
        if full.width() > 0 and full.height() > 0:
            ins = (max(0.0, paint.x() / full.width()),
                   max(0.0, paint.y() / full.height()),
                   max(0.0, (full.width() - paint.x() - paint.width()) / full.width()),
                   max(0.0, (full.height() - paint.y() - paint.height()) / full.height()))
        else:
            ins = (0.0, 0.0, 0.0, 0.0)
        self._insets_cache[key] = ins
        return ins

    def _compose_sheet(self, page_indices):
        """Render ONE physical sheet exactly as it will print: the paper (size +
        orientation), the page(s) laid out in the N-up grid WITHIN the printable
        area (content is inset by the printer's hardware margin, matching
        _paint_pages), scaled (fit/actual), and greyscaled for a mono printer."""
        ps = self.paper_size or QPageSize(QPageSize.PageSizeId.Letter)
        pt = ps.sizePoints()
        pw, ph = pt.width(), pt.height()
        orient = self._effective_orientation(page_indices)
        if orient == QPageLayout.Orientation.Landscape:
            pw, ph = ph, pw
        # Compute the printable insets BEFORE opening the sheet painter -- the
        # query builds a QPrinter (a paint device), which conflicts with an
        # active QPainter.
        il, it_, ir, ib = self._printable_insets(orient)
        vp = self._scroll.viewport().size()
        avail_w = max(140, vp.width() - 72)
        avail_h = max(140, vp.height() - 72)
        dpr = self.devicePixelRatioF() or 1.0
        fit = max(0.05, min(avail_w / pw, avail_h / ph) * self._scale)
        ppt = fit * dpr                                    # sheet px per point
        sheet = QImage(max(1, round(pw * ppt)), max(1, round(ph * ppt)),
                       QImage.Format.Format_RGB888)
        sheet.fill(QColor("#FFFFFF"))
        p = QPainter(sheet)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        # The printable area (paper minus the unprintable hardware margin).
        ox, oy = il * sheet.width(), it_ * sheet.height()
        area_w = sheet.width() * (1.0 - il - ir)
        area_h = sheet.height() * (1.0 - it_ - ib)
        rows, cols = _GRID.get(self.nup, (1, 1))
        cw, ch = area_w / cols, area_h / rows
        pad = 0.03 * min(cw, ch) if self.nup > 1 else 0.0
        gray = self._effective_grayscale()
        for idx, pi in enumerate(page_indices):
            r, c = divmod(idx, cols)
            cx, cy = ox + c * cw, oy + r * ch
            img = self._render_page(pi, ppt, gray)
            aw, ah = cw - 2 * pad, ch - 2 * pad
            # "actual size": img is already ppt px/pt, matching the paper scale.
            if self.nup == 1 and self.scale_mode == "actual":
                s = 1.0
            else:
                s = min(aw / img.width(), ah / img.height())
            w, h = img.width() * s, img.height() * s
            p.drawImage(QRectF(cx + (cw - w) / 2.0, cy + (ch - h) / 2.0, w, h), img)
        p.end()
        sheet.setDevicePixelRatio(dpr)
        return sheet

    def _rebuild_sheets(self) -> None:
        """Compose EVERY sheet and stack them in the scroll area (continuous
        scroll). ponytail: composes all sheets eagerly -- fine for typical docs;
        add visible-only rendering if a huge PDF gets sluggish."""
        while self._sheets_layout.count():
            w = self._sheets_layout.takeAt(0).widget()
            if w is not None:
                w.deleteLater()
        self._sheet_labels = []
        dpr = self.devicePixelRatioF() or 1.0
        for pages in self._sheets():
            pm = QPixmap.fromImage(self._compose_sheet(pages))
            lbl = QLabel()
            lbl.setAlignment(Qt.AlignCenter)
            lbl.setPixmap(pm)
            lbl.setFixedSize(QSize(round(pm.width() / dpr), round(pm.height() / dpr)))
            self._sheets_layout.addWidget(lbl, 0, Qt.AlignHCenter)
            self._sheet_labels.append(lbl)
        self._zoomlbl.setText("Fit" if abs(self._scale - 1.0) < 0.01
                              else f"{self._scale:.0%}")
        n = len(self._sheet_labels)
        self._sheet_idx = min(self._sheet_idx, max(0, n - 1))
        self._pagelbl.setText(f"{self._sheet_idx + 1} / {n}" if n else "0 / 0")
        self._prev.setEnabled(self._sheet_idx > 0)
        self._next.setEnabled(self._sheet_idx < n - 1)
        # Geometry isn't laid out yet, so defer the scroll-position readout.
        QTimer.singleShot(0, self._update_indicator)

    def _sheet_top(self, i: int) -> int:
        """Y offset of sheet ``i`` in the holder, from the fixed sheet heights +
        layout margins/spacing (no mapTo -> works before the layout activates)."""
        top = self._sheets_layout.contentsMargins().top()
        spacing = self._sheets_layout.spacing()
        y = top
        for j in range(i):
            y += self._sheet_labels[j].height() + spacing
        return y

    def _update_indicator(self) -> None:
        """Set the sheet counter from the scroll position (which sheet is in view)."""
        n = len(self._sheet_labels)
        if n == 0:
            self._pagelbl.setText("0 / 0")
            self._prev.setEnabled(False)
            self._next.setEnabled(False)
            return
        spacing = self._sheets_layout.spacing()
        center = (self._scroll.verticalScrollBar().value()
                  + self._scroll.viewport().height() / 2)
        idx = n - 1
        y = self._sheets_layout.contentsMargins().top()
        for i, lbl in enumerate(self._sheet_labels):
            if center < y + lbl.height() + spacing / 2:
                idx = i
                break
            y += lbl.height() + spacing
        # Fires on every scroll tick: skip the styled-widget churn (setText +
        # :disabled repolish) unless the sheet in view actually changed.
        if idx == self._sheet_idx and self._pagelbl.text() == f"{idx + 1} / {n}":
            return
        self._sheet_idx = idx
        self._pagelbl.setText(f"{idx + 1} / {n}")
        self._prev.setEnabled(idx > 0)
        self._next.setEnabled(idx < n - 1)

    def _refresh_preview(self) -> None:
        """A setting changed -> recompose all sheets."""
        if getattr(self, "_sheets_layout", None) is not None:
            self._rebuild_sheets()

    # ---- interaction (prev/next scroll to the neighbouring sheet) ----------
    def _scroll_to_sheet(self, i: int) -> None:
        if 0 <= i < len(self._sheet_labels):
            self._scroll.verticalScrollBar().setValue(max(0, self._sheet_top(i) - 18))

    def _go_prev(self) -> None:
        self._scroll_to_sheet(self._sheet_idx - 1)

    def _go_next(self) -> None:
        self._scroll_to_sheet(self._sheet_idx + 1)

    def _zoom_in(self) -> None:
        self._scale = min(self._scale * 1.25, 5.0)
        self._rebuild_sheets()

    def _zoom_out(self) -> None:
        self._scale = max(self._scale / 1.25, 0.5)
        self._rebuild_sheets()

    def _apply_color_caps(self) -> None:
        """Gate the Color option to what the selected printer can actually do,
        and preselect its default colour mode (a mono printer -> B&W, disabled)."""
        modes = (list(self._dest_info.supportedColorModes())
                 if self._dest_info is not None else [])
        # An empty list = the driver reported no caps; assume colour is available
        # rather than force greyscale on a printer that may well do colour.
        self._color_supported = (not modes) or QPrinter.ColorMode.Color in modes
        # No colour choice on a mono printer -> hide the whole section (B&W stays
        # checked so the effective mode + preview are greyscale).
        self._color_box.setVisible(self._color_supported)
        if not self._color_supported:
            self._bw.setChecked(True)
        elif (self._dest_info is not None and self._dest_info.defaultColorMode()
              == QPrinter.ColorMode.GrayScale):
            self._bw.setChecked(True)
        else:
            self._color.setChecked(True)

    def _apply_duplex_caps(self) -> None:
        """Gate the Two-sided control to the selected printer's duplex support
        and rebuild its options, so switching destinations never leaves a stale
        option list -- or a self.duplex that the printer can't do -- behind."""
        _D = QPrinter.DuplexMode
        supported = (list(self._dest_info.supportedDuplexModes())
                     if self._dest_info is not None else [])
        opts = [("One-sided", _D.DuplexNone)]
        if _D.DuplexLongSide in supported:
            opts.append(("Two-sided, long edge", _D.DuplexLongSide))
        if _D.DuplexShortSide in supported:
            opts.append(("Two-sided, short edge", _D.DuplexShortSide))
        # A mode the new printer can't do falls back to one-sided.
        if self.duplex not in [v for _, v in opts]:
            self.duplex = _D.DuplexNone
        lay = self._duplex_box.layout()
        while lay.count():
            w = lay.takeAt(0).widget()
            if w is not None:
                w.deleteLater()
        cur = next((l for l, v in opts if v == self.duplex), "One-sided")
        lay.addWidget(self._eyebrow("TWO-SIDED"))
        lay.addWidget(self._dropdown(
            opts, cur, lambda d: setattr(self, "duplex", d)))
        # No choice to make on a one-sided-only printer -> hide the section.
        self._duplex_box.setVisible(len(opts) > 1)

    def _apply_printer_settings(self) -> None:
        if self._dest_info is not None:
            self._printer.setPrinterName(self._dest_info.printerName())
        if self.paper_size is not None:
            self._printer.setPageSize(self.paper_size)
        self._printer.setCopyCount(self._copies.value())
        self._printer.setCollateCopies(self.collate)
        self._printer.setDuplex(self.duplex)
        self._printer.setColorMode(
            QPrinter.ColorMode.GrayScale if self._effective_grayscale()
            else QPrinter.ColorMode.Color)

    def _on_print(self) -> None:
        if self._pg_custom.isChecked():
            try:
                self.selected_pages = _parse_ranges(self._range.text(), self._n)
            except ValueError as exc:
                self._err.setText(str(exc))
                self._err.show()
                self._range.setFocus()
                return
        else:
            self.selected_pages = None
        self.grayscale = self._effective_grayscale()
        self._apply_printer_settings()
        if self._on_print_cb is not None:
            self._on_print_cb()

    def _on_cancel(self) -> None:
        if self._on_cancel_cb is not None:
            self._on_cancel_cb()

    def _on_system(self) -> None:
        if self._on_system_cb is not None:
            self._on_system_cb()

    def showEvent(self, e) -> None:
        super().showEvent(e)
        self._rebuild_sheets()

    def resizeEvent(self, e) -> None:
        super().resizeEvent(e)
        if getattr(self, "_sheets_layout", None) is not None:
            self._resize_timer.start()   # debounced -> one rebuild when drag settles

    def keyPressEvent(self, e) -> None:
        k = e.key()
        if k == Qt.Key_Escape:
            self._on_cancel()
        elif k in (Qt.Key_Right, Qt.Key_Down, Qt.Key_PageDown):
            self._go_next()
        elif k in (Qt.Key_Left, Qt.Key_Up, Qt.Key_PageUp):
            self._go_prev()
        elif k in (Qt.Key_Plus, Qt.Key_Equal):
            self._zoom_in()
        elif k in (Qt.Key_Minus, Qt.Key_Underscore):
            self._zoom_out()
        else:
            super().keyPressEvent(e)
