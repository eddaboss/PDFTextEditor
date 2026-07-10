"""The Format inspector panel (BUILD_SPEC §4).

A docked side panel that reflects the currently selected box and writes style
changes back. It is PURE CHROME: it never touches the model directly. The
window feeds it a style dict (from ``document.effective_style(...)``) via
``set_target`` and routes its ``styleEdited`` signal to ``view.apply_style``.

Controls (object names are load-bearing for tests + QSS, BUILD_SPEC §4.1):
  * ``InspectorFamily`` -- QComboBox of ``FontEngine.available_families()``
    (populated lazily so construction never blocks on the system-font scan)
  * ``InspectorSize``   -- QDoubleSpinBox, 1..999, " pt" suffix
  * ``InspectorColor``  -- QToolButton swatch opening QColorDialog
  * ``InspectorBold`` / ``InspectorItalic`` -- checkable B / I toggles

Each user change emits ``styleEdited({field: value})`` for the ONE field that
changed, so the window turns it into ONE undo command (BUILD_SPEC §4.2). While
``set_target`` loads a selection, every control's signals are blocked so
populating does NOT echo back as a spurious edit.

The panel also shows a read-only fidelity hint (the same tier dot the status
chip uses) reflecting how the picked family will resolve via
``FontEngine.resolve_family`` -- green/blue/amber for embedded-N/A/system/
base-14 (BUILD_SPEC §4.1, recommended polish).
"""

from __future__ import annotations

from contextlib import contextmanager

from PySide6.QtCore import QRect, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QColorDialog,
    QComboBox,
    QCompleter,
    QDoubleSpinBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ..font_engine import (
    TIER_BASE14,
    TIER_EMBEDDED,
    TIER_SYSTEM,
    FontEngine,
)
from . import theme

# Style for the family completer popup so type-to-filter matches the combo's own
# dropdown view (rounded card, 4px pad, accent selection) rather than a raw
# native QListView (BUILD_SPEC §4.3 / PAGES_SPEC §5.6). The combo's own view is
# styled in theme.global_stylesheet(); the completer popup is a SEPARATE widget,
# so it carries its own matching QSS here.
def _completer_popup_qss() -> str:
    return f"""
QListView#FamilyCompleterPopup {{
    background: {theme.CHROME_BG};
    border: 1px solid {theme.CHROME_BORDER};
    border-radius: 8px;
    padding: 4px;
    outline: none;
    color: {theme.TEXT_PRIMARY};
}}
QListView#FamilyCompleterPopup::item {{
    border-radius: 5px;
    padding: 4px 8px;
    color: {theme.TEXT_PRIMARY};
}}
QListView#FamilyCompleterPopup::item:selected {{
    background: {theme.ACCENT};
    color: #FFFFFF;
}}
"""

# Fidelity hint copy per tier. A user-PICKED family is NEVER Tier 1 (the doc's
# embedded buffer can't supply fresh glyphs), so resolve_family only ever
# returns SYSTEM or BASE14 -- but the dict covers all three defensively. Each
# line states what will land in the SAVED PDF, so the chip teaches the
# consequence (the product's core trust signal), not just a tier name.

# Per-tier status block: (status word, plain-language consequence, ink color,
# tinted card background). The product's core trust signal -- how faithfully the
# saved PDF reproduces the font -- raised from a buried gray line into a tinted
# status card so it reads at a glance.
_FIDELITY_BLOCK = {
    TIER_EMBEDDED: ("Embedded", "Original font reused, pixel-identical",
                    theme.FIDELITY_GREEN, theme.FIDELITY_GREEN_BG),
    TIER_SYSTEM: ("System match", "Closest system font, embedded on save",
                  theme.FIDELITY_BLUE, theme.FIDELITY_BLUE_BG),
    TIER_BASE14: ("Substitute", "Standard font stand-in",
                  theme.FIDELITY_AMBER, theme.FIDELITY_AMBER_BG),
}

# How many of the 3 ladder pips light up per tier (best = embedded = all three).
_FIDELITY_PIPS = {TIER_EMBEDDED: 3, TIER_SYSTEM: 2, TIER_BASE14: 1}


class _FidelityCard(QFrame):
    """The font-fidelity status card -- the product's core trust signal, built
    out (not a one-liner): a colored swatch with a check, the tier word + a
    family-specific subtitle, a plain-language consequence sentence, and a
    3-pip ladder labelled Embedded / System match / Substitute so you can see
    at a glance how faithfully the saved PDF reproduces the font."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("InspectorFidelity")
        self._last_state = None
        theme.events.changed.connect(self._restyle)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(13, 13, 13, 13)
        lay.setSpacing(10)

        top = QHBoxLayout()
        top.setSpacing(10)
        self._swatch = QLabel("✓")
        self._swatch.setObjectName("FidelitySwatch")
        self._swatch.setFixedSize(30, 30)
        self._swatch.setAlignment(Qt.AlignCenter)
        sf = theme.ui_font(15, semibold=True)
        self._swatch.setFont(sf)
        tcol = QVBoxLayout()
        tcol.setSpacing(1)
        self._tier = QLabel()
        self._tier.setFont(theme.ui_font(13, semibold=True))
        self._sub = QLabel()
        self._sub.setFont(theme.ui_font(11, medium=True))
        self._sub.setWordWrap(True)
        tcol.addWidget(self._tier)
        tcol.addWidget(self._sub)
        top.addWidget(self._swatch, 0, Qt.AlignTop)
        top.addLayout(tcol, 1)
        lay.addLayout(top)

        self._desc = QLabel()
        self._desc.setWordWrap(True)
        self._desc.setFont(theme.ui_font(11, medium=True))
        lay.addWidget(self._desc)

        ladder = QHBoxLayout()
        ladder.setSpacing(4)
        self._pips = []
        for _ in range(3):
            pip = QFrame()
            pip.setObjectName("FidelityPip")
            pip.setFixedHeight(4)
            pip.setSizePolicy(QSizePolicy.Policy.Expanding,
                              QSizePolicy.Policy.Fixed)
            self._pips.append(pip)
            ladder.addWidget(pip)
        lay.addLayout(ladder)

        # The ladder is a SCALE: best (Embedded) on the left, fallback
        # (Substitute) on the right. Only the two ends are labelled -- the
        # current tier is already the coloured headline + the lit pips, so a
        # third middle label was redundant AND overflowed the narrow panel.
        lrow = QHBoxLayout()
        self._tier_labels = {
            TIER_EMBEDDED: QLabel("Embedded"),
            TIER_BASE14: QLabel("Substitute"),
        }
        small = theme.ui_font(10, medium=True)
        for lbl in self._tier_labels.values():
            lbl.setFont(small)
        lrow.addWidget(self._tier_labels[TIER_EMBEDDED])
        lrow.addStretch(1)
        lrow.addWidget(self._tier_labels[TIER_BASE14])
        lay.addLayout(lrow)

    def _consequence(self, tier: int, family: str) -> tuple[str, str]:
        """(subtitle, description) woven with the resolved family name."""
        fam = family or "the font"
        if tier == TIER_EMBEDDED:
            return (f"{fam} reused",
                    "The original embedded font is reused, so this edit is "
                    "pixel-identical in every viewer.")
        if tier == TIER_SYSTEM:
            return (f"{fam} found, embeds on save",
                    "A matching system font was located and will be embedded "
                    "into the file, so this edit renders faithfully in every "
                    "viewer.")
        return ("Standard font stand-in",
                "No exact match was found, so a standard font stands in; the "
                "saved text may look slightly different.")

    # A SOFT same-hue hairline per tier (the widget's .cs-fid border is a ~.36
    # alpha tint, not a full-strength colored line).
    _FIDELITY_BORDER = {
        TIER_EMBEDDED: "rgba(51,194,125,.38)",
        TIER_SYSTEM: "rgba(90,166,234,.38)",
        TIER_BASE14: "rgba(220,160,70,.38)",
    }

    def set_state(self, tier: int, family: str) -> None:
        self._last_state = (tier, family)   # re-applied on a live theme switch
        word, _line, color, bg = _FIDELITY_BLOCK.get(
            tier, _FIDELITY_BLOCK[TIER_BASE14])
        border = self._FIDELITY_BORDER.get(tier, self._FIDELITY_BORDER[TIER_BASE14])
        sub, desc = self._consequence(tier, family)
        # Per-tier tinted card (over the base QSS) + a soft same-hue hairline.
        self.setStyleSheet(
            f"QFrame#InspectorFidelity {{ background:{bg}; "
            f"border:1px solid {border}; border-radius:11px; }}"
            f"QLabel#InspectorFidelity {{ background:transparent; }}")
        self._swatch.setStyleSheet(
            f"background:{color}; border-radius:8px; color:#FFFFFF;")
        self._tier.setText(word)
        self._tier.setStyleSheet(f"color:{color}; background:transparent;")
        self._sub.setText(sub)
        self._sub.setStyleSheet(f"color:{theme.TEXT_SECONDARY}; background:transparent;")
        self._desc.setText(desc)
        self._desc.setStyleSheet(f"color:{theme.TEXT_SECONDARY}; background:transparent;")
        on = _FIDELITY_PIPS.get(tier, 1)
        for i, pip in enumerate(self._pips):
            fill = color if i < on else "rgba(255,255,255,.13)"
            pip.setStyleSheet(f"background:{fill}; border-radius:2px;")
        tier_color = {
            TIER_EMBEDDED: theme.FIDELITY_GREEN,
            TIER_SYSTEM: theme.FIDELITY_BLUE,
            TIER_BASE14: theme.FIDELITY_AMBER,
        }
        for t, lbl in self._tier_labels.items():
            active = (t == tier)
            lbl.setStyleSheet(
                f"color:{tier_color[t]}; background:transparent;"
                f"{'font-weight:700;' if active else ''}")
        self.setVisible(True)

    def clear(self) -> None:
        self.setVisible(False)

    def _restyle(self) -> None:
        """Re-apply the per-tier inline colors after a live theme switch (the
        TEXT_SECONDARY sub/desc lines are mode-dependent)."""
        if self._last_state is not None:
            self.set_state(*self._last_state)


class _FamilyCombo(QComboBox):
    """The family picker combo. Opening the dropdown first runs the owner's
    ``ensure_populated`` callback so a popup opened before the lazy family
    census landed forces the (blocking) build -- exactly the documented M4a
    cold-path tradeoff, paid only on explicit font UI."""

    ensure_populated = None     # set by Inspector after construction

    def showPopup(self) -> None:  # noqa: N802 - Qt API name
        if callable(self.ensure_populated):
            self.ensure_populated()
        super().showPopup()


class Inspector(QWidget):
    """The Format side panel. ``objectName == 'Inspector'`` (BUILD_SPEC §4.1)."""

    # Emitted whenever the user changes ONE control: {field: value} for the
    # single field that changed (BUILD_SPEC §4.1). The window routes this to
    # view.apply_style, which funnels to a single BoxCommand on the undo stack.
    styleEdited = Signal(dict)
    # The ANNOTATION section's mirror of styleEdited (annotations & markup
    # §5.5): {field: value} for the ONE annot style field that changed
    # (stroke / fill / width / opacity). The window turns it into one
    # AnnotCommand('style', ...) -- or a session-default write while a tool
    # is armed with nothing selected.
    annotStyleEdited = Signal(dict)

    # The empty-state placeholder. ``set_hint`` swaps it for selection kinds
    # the panel does not style (images), so the copy never contradicts the
    # current selection. Teaches the core gesture rather than just stating
    # "nothing selected" (product register: empty states teach the interface).
    _DEFAULT_HINT = ("Click any line of text on the page to change its font, "
                     "size, color, or weight here.")

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("Inspector")
        # A bare QWidget does not paint its QSS ``background`` unless told to;
        # without this the panel body fell through to the window's (now much
        # deeper) gutter color, so the Format column read as a darker patch
        # under its own header. Paint the styled CHROME_BG like every other panel.
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setSizePolicy(QSizePolicy.Policy.Preferred,
                           QSizePolicy.Policy.Expanding)

        # True while set_target is loading a selection: guards every control's
        # slot so programmatic population never echoes back as a user edit.
        self._loading = False
        # The current color (r,g,b 0..1), kept so the swatch + dialog seed match
        # and the emitted value is precise (the swatch button has no value of its
        # own once painted).
        self._color = (0.0, 0.0, 0.0)
        self._target = None
        # The ANNOTATION section's target (annotations & markup §5.5): an
        # AnnotRecord (selection) or a plain dict (an armed tool's session
        # defaults), mutually exclusive with the text target above.
        self._annot_target = None
        # The annot stroke / fill swatch values, kept like ``_color`` so the
        # dialog seed and the emitted tuples stay precise. ``_annot_fill``
        # remembers the last fill color so unchecking "No fill" restores it.
        self._annot_stroke = (0.85, 0.1, 0.1)
        self._annot_fill = (1.0, 1.0, 1.0)

        self._build()
        # Seed sensible defaults so a box ADDED before any selection (the Add
        # Text tool reads current_values) gets Helvetica 12pt black, not the
        # combo's alphabetical first item (BUILD_SPEC §3.5 step 3).
        self._seed_defaults()
        self.set_target(None, None)
        theme.events.changed.connect(self._restyle)

    def _restyle(self) -> None:
        """Re-read the inline token colors this panel sets outside the global
        sheet after a live light/dark switch (the fidelity card self-restyles)."""
        self._annot_hint.setStyleSheet(f"color: {theme.TEXT_SECONDARY};")
        self._paint_swatch()

    # =====================================================================
    # Construction
    # =====================================================================
    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 14, 16, 14)
        outer.setSpacing(14)

        # --- Empty-state hint (shown when nothing is selected) -----------
        self._empty_hint = QLabel(self._DEFAULT_HINT)
        self._empty_hint.setObjectName("InspectorEmptyHint")
        self._empty_hint.setWordWrap(True)
        self._empty_hint.setFont(theme.ui_font(12))
        outer.addWidget(self._empty_hint)

        # --- The control COLUMN (hidden in the empty state) --------------
        # A plain vertical stack of self-describing controls (no left-label
        # form rows), grouped under "Type" and "Style" eyebrows, matching the
        # Charcoal Studio widget's Format panel.
        self._form_host = QWidget()
        form = QVBoxLayout(self._form_host)
        form.setContentsMargins(0, 0, 0, 0)
        form.setSpacing(0)

        # Selection chip: WHAT is selected (i-beam tile + text + char count),
        # matching the widget .cs-sel. Populated in set_target.
        self._sel_chip = QFrame()
        self._sel_chip.setObjectName("InspectorSelChip")
        scl = QHBoxLayout(self._sel_chip)
        scl.setContentsMargins(11, 9, 11, 9)
        scl.setSpacing(10)
        self._sel_tile = QLabel("I")
        self._sel_tile.setObjectName("SelChipTile")
        self._sel_tile.setFixedSize(28, 28)
        self._sel_tile.setAlignment(Qt.AlignCenter)
        self._sel_tile.setFont(theme.ui_font(15, semibold=True))
        sctx = QVBoxLayout()
        sctx.setContentsMargins(0, 0, 0, 0)
        sctx.setSpacing(1)
        self._sel_title = QLabel("")
        self._sel_title.setObjectName("SelChipTitle")
        self._sel_title.setFont(theme.ui_font(13, medium=True))
        self._sel_sub = QLabel("")
        self._sel_sub.setObjectName("SelChipSub")
        self._sel_sub.setFont(theme.ui_font(11, medium=True))
        sctx.addWidget(self._sel_title)
        sctx.addWidget(self._sel_sub)
        scl.addWidget(self._sel_tile)
        scl.addLayout(sctx, 1)
        form.addWidget(self._sel_chip)
        form.addSpacing(16)

        # --- "Type" group: font, then size + color ----------------------
        type_head = QLabel("TYPE")
        type_head.setObjectName("InspectorSectionHeader")
        type_head.setFont(theme.caps_header_font())
        form.addWidget(type_head)
        form.addSpacing(9)

        # Font family combo: SEARCHABLE type-to-filter over the ~303 families,
        # loaded lazily (final-review perf fix). The combo starts empty and
        # fills from the published index via _try_load_families.
        self._families: list[str] = []
        self._families_lower: set[str] = set()
        self._families_loaded = False
        self.family_combo = _FamilyCombo()
        self.family_combo.setObjectName("InspectorFamily")
        self.family_combo.setEditable(True)
        self.family_combo.setInsertPolicy(QComboBox.NoInsert)
        self.family_combo.setFont(theme.ui_font(13))
        self.family_combo.setCursor(Qt.PointingHandCursor)
        self.family_combo.setMaxVisibleItems(18)
        completer = self.family_combo.completer()
        if completer is not None:
            completer.setCompletionMode(QCompleter.PopupCompletion)
            completer.setFilterMode(Qt.MatchContains)
            completer.setCaseSensitivity(Qt.CaseInsensitive)
            popup = completer.popup()
            if popup is not None:
                popup.setObjectName("FamilyCompleterPopup")
                popup.setFont(theme.ui_font(13))
                popup.setStyleSheet(_completer_popup_qss())
        self.family_combo.currentIndexChanged.connect(self._on_family_index)
        line = self.family_combo.lineEdit()
        if line is not None:
            line.setPlaceholderText("Search fonts…")
            # No clear (X) button -- the widget's font field shows only a chevron.
            line.editingFinished.connect(self._on_family_commit)
        form.addWidget(self.family_combo)
        form.addSpacing(8)
        self.family_combo.ensure_populated = (
            lambda: self._ensure_families(block=True))
        if not self._try_load_families():
            from PySide6.QtCore import QTimer
            self._families_timer = QTimer(self)
            self._families_timer.setInterval(50)
            self._families_timer.timeout.connect(self._poll_families)
            self._families_timer.start()

        # Size (fixed width) + Color chip (swatch + hex) on one row.
        sizerow = QWidget()
        szl = QHBoxLayout(sizerow)
        szl.setContentsMargins(0, 0, 0, 0)
        szl.setSpacing(8)
        self.size_spin = QDoubleSpinBox()
        self.size_spin.setObjectName("InspectorSize")
        self.size_spin.setRange(1.0, 999.0)
        self.size_spin.setDecimals(1)
        self.size_spin.setSingleStep(1.0)
        self.size_spin.setSuffix(" pt")
        self.size_spin.setFont(theme.ui_font(13, medium=True))
        self.size_spin.setKeyboardTracking(False)   # one edit per settle
        self.size_spin.setFixedWidth(96)
        self.size_spin.valueChanged.connect(self._on_size_changed)
        self._color_chip = QFrame()
        self._color_chip.setObjectName("InspectorColorChip")
        self._color_chip.setFixedHeight(34)
        self._color_chip.setCursor(Qt.PointingHandCursor)
        self._color_chip.setToolTip("Text color")
        ccl = QHBoxLayout(self._color_chip)
        ccl.setContentsMargins(10, 0, 10, 0)
        ccl.setSpacing(8)
        self._color_swatch = QFrame()
        self._color_swatch.setObjectName("InspectorColorSwatch")
        self._color_swatch.setFixedSize(18, 18)
        self._color_hex = QLabel("#000000")
        self._color_hex.setObjectName("InspectorColorHex")
        self._color_hex.setFont(theme.mono_font(12))
        ccl.addWidget(self._color_swatch)
        ccl.addWidget(self._color_hex)
        ccl.addStretch(1)
        # The whole chip is the click target for the color dialog.
        self._color_chip.mousePressEvent = lambda _e: self._on_color_clicked()
        szl.addWidget(self.size_spin)
        szl.addWidget(self._color_chip, 1)
        form.addWidget(sizerow)
        form.addSpacing(16)

        # --- "Style" group: B I U S segment + alignment segment ---------
        style_head = QLabel("STYLE")
        style_head.setObjectName("InspectorSectionHeader")
        style_head.setFont(theme.caps_header_font())
        form.addWidget(style_head)
        form.addSpacing(9)

        # Bold / Italic / Underline / Strikethrough as ONE segmented control.
        bis = QFrame()
        bis.setObjectName("InspectorSeg")
        bisl = QHBoxLayout(bis)
        bisl.setContentsMargins(3, 3, 3, 3)
        bisl.setSpacing(3)
        self.bold_button = self._seg_button("B", "Bold")
        bf = theme.ui_font(14, semibold=True)
        self.bold_button.setFont(bf)
        self.italic_button = self._seg_button("I", "Italic")
        itf = theme.ui_font(14)
        itf.setItalic(True)
        self.italic_button.setFont(itf)
        self.underline_button = self._seg_button("U", "Underline")
        uf = theme.ui_font(14)
        uf.setUnderline(True)
        self.underline_button.setFont(uf)
        self.strike_button = self._seg_button("S", "Strikethrough")
        kf = theme.ui_font(14)
        kf.setStrikeOut(True)
        self.strike_button.setFont(kf)
        self.bold_button.toggled.connect(self._on_bold_toggled)
        self.italic_button.toggled.connect(self._on_italic_toggled)
        self.underline_button.toggled.connect(self._on_underline_toggled)
        self.strike_button.toggled.connect(self._on_strike_toggled)
        for b in (self.bold_button, self.italic_button,
                  self.underline_button, self.strike_button):
            # These style the HIGHLIGHTED text inside the open inline editor, so
            # they must NOT take keyboard focus: a focus grab pulls it off the
            # editor, whose focus-out commits the edit and clears the selection
            # before the style can land (the "it unhighlights when I format it"
            # bug). NoFocus keeps the editor focused so the selection survives
            # and apply_style_to_selection styles it in place.
            b.setFocusPolicy(Qt.NoFocus)
            bisl.addWidget(b)
        form.addWidget(bis)
        form.addSpacing(8)

        # Alignment as a second segmented control, shown for any text selection.
        alignseg = QFrame()
        alignseg.setObjectName("InspectorSeg")
        asl = QHBoxLayout(alignseg)
        asl.setContentsMargins(3, 3, 3, 3)
        asl.setSpacing(3)
        self._align_group = QButtonGroup(self)
        self._align_group.setExclusive(True)
        self._align_buttons: dict[str, QToolButton] = {}
        align_labels = {"left": "Align left", "center": "Align center",
                        "right": "Align right", "justify": "Justify"}
        for name in ("left", "center", "right", "justify"):
            btn = self._seg_button(None, align_labels[name])
            btn.setIcon(self._align_icon(name))
            btn.setIconSize(QSize(16, 16))
            btn.toggled.connect(
                lambda checked, a=name: self._on_align_toggled(a, checked))
            self._align_group.addButton(btn)
            self._align_buttons[name] = btn
            asl.addWidget(btn)
        form.addWidget(alignseg)

        outer.addWidget(self._form_host)

        # Read-only fidelity hint: how the picked family will resolve on save.
        # Placed directly UNDER the TEXT STYLE controls (not after the trailing
        # stretch) so it reads as the font row's resolution status adjacent to the
        # font/size/color controls, instead of a lone stranded line at the panel
        # bottom (review minor). Full panel width so it never wraps/clips.
        self._fidelity_header = QLabel("FONT FIDELITY")
        self._fidelity_header.setObjectName("InspectorSectionHeader")
        self._fidelity_header.setFont(theme.caps_header_font())
        outer.addSpacing(8)
        outer.addWidget(self._fidelity_header)
        self.fidelity_label = _FidelityCard()
        outer.addSpacing(6)
        outer.addWidget(self.fidelity_label)

        # --- Paragraph layout controls (REFLOW_SPEC §R5.2) ---------------
        # Shown ONLY when a ParagraphBox is selected (alignment + line spacing
        # feed the wrap engine). Hidden for a Span/NewBox so these never apply to
        # a single-line run.
        self._para_host = QWidget()
        pform = QFormLayout(self._para_host)
        pform.setContentsMargins(0, 0, 0, 0)
        pform.setHorizontalSpacing(12)
        pform.setVerticalSpacing(12)
        pform.setLabelAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        pform.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)

        pheader = QLabel("PARAGRAPH")
        pheader.setObjectName("InspectorSectionHeader")
        pheader.setFont(theme.caps_header_font())
        pform.addRow(pheader)

        # Alignment moved UP into the Style group's second segment (shown for any
        # text selection). The paragraph section now holds only line spacing,
        # which is meaningful only for a multi-line ParagraphBox.

        # Line spacing: a multiplier spin (0.8 - 3.0, x suffix).
        self.spacing_spin = QDoubleSpinBox()
        self.spacing_spin.setObjectName("InspectorLineSpacing")
        self.spacing_spin.setRange(0.8, 3.0)
        self.spacing_spin.setDecimals(2)
        self.spacing_spin.setSingleStep(0.1)
        self.spacing_spin.setSuffix(" ×")
        self.spacing_spin.setValue(1.0)
        self.spacing_spin.setFont(theme.ui_font(13))
        self.spacing_spin.setKeyboardTracking(False)
        self.spacing_spin.valueChanged.connect(self._on_spacing_changed)
        pform.addRow(self._field_label("Spacing"), self.spacing_spin)

        outer.addWidget(self._para_host)
        self._para_host.setVisible(False)

        # --- Annotation style controls (annotations & markup §5.5) -------
        # Shown ONLY for an annot target (set_annot_target); every text
        # section hides while it is up. One emission per control change,
        # mirroring the styleEdited invariant.
        self._annot_host = QWidget()
        aform = QFormLayout(self._annot_host)
        aform.setContentsMargins(0, 0, 0, 0)
        aform.setHorizontalSpacing(12)
        aform.setVerticalSpacing(12)
        aform.setLabelAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        aform.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        self._annot_form = aform

        aheader = QLabel("ANNOTATION")
        aheader.setObjectName("InspectorSectionHeader")
        aheader.setFont(theme.caps_header_font())
        aform.addRow(aheader)

        # Stroke color swatch.
        self.annot_stroke_button = QToolButton()
        self.annot_stroke_button.setObjectName("AnnotStroke")
        self.annot_stroke_button.setCursor(Qt.PointingHandCursor)
        self.annot_stroke_button.setToolTip("Stroke color")
        self.annot_stroke_button.setIconSize(QSize(20, 20))
        self.annot_stroke_button.clicked.connect(self._on_annot_stroke_clicked)
        aform.addRow(self._field_label("Stroke"), self.annot_stroke_button)

        # Border width (drawn kinds; harmless for markup, which has none).
        self.annot_width_spin = QDoubleSpinBox()
        self.annot_width_spin.setObjectName("AnnotWidth")
        self.annot_width_spin.setRange(0.5, 12.0)
        self.annot_width_spin.setDecimals(1)
        self.annot_width_spin.setSingleStep(0.5)
        self.annot_width_spin.setSuffix(" pt")
        self.annot_width_spin.setFont(theme.ui_font(13))
        self.annot_width_spin.setKeyboardTracking(False)
        self.annot_width_spin.valueChanged.connect(self._on_annot_width_changed)
        aform.addRow(self._field_label("Width"), self.annot_width_spin)

        # Fill swatch + "No fill" (rect/ellipse only; row hides otherwise).
        fill_row = QWidget()
        frl = QHBoxLayout(fill_row)
        frl.setContentsMargins(0, 0, 0, 0)
        frl.setSpacing(8)
        self.annot_fill_button = QToolButton()
        self.annot_fill_button.setObjectName("AnnotFill")
        self.annot_fill_button.setCursor(Qt.PointingHandCursor)
        self.annot_fill_button.setToolTip("Fill color")
        self.annot_fill_button.setIconSize(QSize(20, 20))
        self.annot_fill_button.clicked.connect(self._on_annot_fill_clicked)
        self.annot_nofill_check = QCheckBox("No fill")
        self.annot_nofill_check.setObjectName("AnnotNoFill")
        self.annot_nofill_check.setFont(theme.ui_font(12))
        self.annot_nofill_check.toggled.connect(self._on_annot_nofill_toggled)
        frl.addWidget(self.annot_fill_button)
        frl.addWidget(self.annot_nofill_check)
        frl.addStretch(1)
        self._annot_fill_row = fill_row
        self._annot_fill_label = self._field_label("Fill")
        aform.addRow(self._annot_fill_label, fill_row)

        # Opacity, 10-100%.
        self.annot_opacity_spin = QDoubleSpinBox()
        self.annot_opacity_spin.setObjectName("AnnotOpacity")
        self.annot_opacity_spin.setRange(10.0, 100.0)
        self.annot_opacity_spin.setDecimals(0)
        self.annot_opacity_spin.setSingleStep(10.0)
        self.annot_opacity_spin.setSuffix(" %")
        self.annot_opacity_spin.setFont(theme.ui_font(13))
        self.annot_opacity_spin.setKeyboardTracking(False)
        self.annot_opacity_spin.valueChanged.connect(
            self._on_annot_opacity_changed)
        aform.addRow(self._field_label("Opacity"), self.annot_opacity_spin)

        # Read-only hint for a PRE-EXISTING file annot: the section grays
        # (delete / note-move / contents only -- never foreign styles, §8).
        self._annot_hint = QLabel("Saved annotation")
        self._annot_hint.setObjectName("InspectorAnnotHint")
        self._annot_hint.setFont(theme.ui_font(11))
        self._annot_hint.setStyleSheet(f"color: {theme.TEXT_SECONDARY};")
        self._annot_hint.setWordWrap(True)
        aform.addRow(self._annot_hint)

        outer.addWidget(self._annot_host)
        self._annot_host.setVisible(False)

        outer.addStretch(1)

    def _seed_defaults(self) -> None:
        """Load Helvetica / 12pt / black into the controls WITHOUT emitting, so
        ``current_values()`` returns a usable add-style before any selection."""
        with self._loaded():
            self._select_family("Helvetica")
            self.size_spin.setValue(12.0)
            self._color = (0.0, 0.0, 0.0)
            self._paint_swatch()
            self.bold_button.setChecked(False)
            self.italic_button.setChecked(False)

    def _field_label(self, text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setFont(theme.ui_font(12, medium=True))
        lbl.setStyleSheet(f"color: {theme.TEXT_SECONDARY};")
        return lbl

    def _seg_button(self, text, tip: str) -> QToolButton:
        """One equal-width segment inside an #InspectorSeg segmented control."""
        b = QToolButton()
        b.setObjectName("SegButton")
        b.setCheckable(True)
        b.setCursor(Qt.PointingHandCursor)
        b.setToolTip(tip)
        b.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        if text is not None:
            b.setText(text)
        return b

    # =====================================================================
    # Public API (BUILD_SPEC §4.1)
    # =====================================================================
    def set_target(self, box, style: dict | None) -> None:
        """Populate every control from ``style`` (the dict from
        ``document.effective_style(...)``) WITHOUT emitting ``styleEdited``.
        ``box=None`` disables the panel (empty state). Any annot target is
        dropped: the text and ANNOTATION sections are mutually exclusive
        (annotations & markup §5.5). Restores the DEFAULT placeholder copy;
        an image selection sets its own via ``set_hint`` afterwards."""
        self._empty_hint.setText(self._DEFAULT_HINT)
        self._target = box
        self._annot_target = None
        self._annot_host.setVisible(False)
        if box is None or style is None:
            self._set_enabled(False)
            return

        with self._loaded():
            # Show the REAL typeface name, not the opaque scan-bank alias the box
            # stores internally (e.g. 'ScanFont-01669' -> 'Solway Medium').
            family = style.get("font_family", "")
            self._select_family(FontEngine.display_name_for(family))
            self.size_spin.setValue(float(style.get("size", 12.0) or 12.0))
            self._color = self._norm_color(style.get("color", (0.0, 0.0, 0.0)))
            self._paint_swatch()
            self.bold_button.setChecked(bool(style.get("bold", False)))
            self.italic_button.setChecked(bool(style.get("italic", False)))
            self.underline_button.setChecked(bool(style.get("underline", False)))
            self.strike_button.setChecked(bool(style.get("strike", False)))
            is_para = bool(getattr(box, "is_paragraph", False))
            # The selection chip names WHAT is selected.
            text = (getattr(box, "text", "") or "").strip()
            kind = ("Paragraph" if is_para
                    else "New text box" if getattr(box, "box_id", None) is not None
                    else "Text run")
            disp = text if len(text) <= 30 else text[:29] + "…"
            self._sel_title.setText(f"“{disp}”" if disp else kind)
            n = len(text)
            self._sel_sub.setText(
                f"{kind} · {n} character{'s' if n != 1 else ''}")
            # Alignment is shown for ANY text selection (the Style group's second
            # segment); line spacing stays paragraph-only.
            align = style.get("alignment", "left")
            btn = self._align_buttons.get(align) or self._align_buttons["left"]
            btn.setChecked(True)
            self._para_host.setVisible(is_para)
            if is_para:
                self.spacing_spin.setValue(
                    float(style.get("line_spacing", 1.0) or 1.0))
        self._set_enabled(True)
        self._refresh_fidelity()

    def set_hint(self, text: str | None) -> None:
        """Override the empty-state placeholder (e.g. while an IMAGE is
        selected: the panel is pure text chrome, and 'Select a text box...'
        would contradict the live selection). ``None`` restores the default;
        ``set_target`` also restores it on the next selection change."""
        self._empty_hint.setText(text or self._DEFAULT_HINT)

    def current_values(self) -> dict:
        """Snapshot of all controls (BUILD_SPEC §4.1)."""
        return {
            "font_family": self.family_combo.currentText(),
            "size": float(self.size_spin.value()),
            "color": self._color,
            "bold": self.bold_button.isChecked(),
            "italic": self.italic_button.isChecked(),
        }

    def set_annot_target(self, target) -> None:
        """Point the ANNOTATION section at ``target`` (annotations & markup
        §5.5) WITHOUT emitting ``annotStyleEdited``:

          * an AnnotRecord -- a staged spec populates + enables the
            controls; a PRE-EXISTING file annot grays them under the
            "Saved annotation" hint (§8: no foreign style edits);
          * a dict (the window's per-tool session defaults, carrying at
            least "kind") -- the controls edit the defaults in place;
          * None -- the section hides (the empty hint returns when no text
            target is up either).

        Every TEXT section hides while an annot target is set (the two are
        mutually exclusive); fill shows for the rect/ellipse kinds only."""
        self._annot_target = target
        if target is None:
            self._annot_host.setVisible(False)
            if self._target is None:
                self._set_enabled(False)
            return
        # An annot target REPLACES any text target (mutual exclusion §5.5).
        self._target = None
        self._form_host.setVisible(False)
        self.fidelity_label.setVisible(False)
        self.fidelity_label.clear()
        self._para_host.setVisible(False)
        self._empty_hint.setVisible(False)

        if isinstance(target, dict):       # an armed tool's session defaults
            kind = target.get("kind", "")
            existing = False
            stroke = target.get("stroke", (0.85, 0.1, 0.1))
            fill = target.get("fill")
            width = target.get("width", 2.0)
            opacity = target.get("opacity", 1.0)
        else:                              # an AnnotRecord
            kind = getattr(target, "kind", "")
            existing = bool(getattr(target, "is_existing", False))
            spec = getattr(target, "spec", None)
            stroke = getattr(spec, "stroke", (0.85, 0.1, 0.1))
            fill = getattr(spec, "fill", None)
            width = getattr(spec, "width", 2.0)
            opacity = getattr(spec, "opacity", 1.0)

        with self._loaded():
            self._annot_stroke = self._norm_color(stroke)
            if fill is not None:
                self._annot_fill = self._norm_color(fill)
            self.annot_nofill_check.setChecked(fill is None)
            self.annot_width_spin.setValue(float(width or 2.0))
            self.annot_opacity_spin.setValue(
                round(float(opacity if opacity is not None else 1.0) * 100))
            self._paint_annot_swatches()
        shapes_only = kind in ("rect", "ellipse")
        self._annot_fill_label.setVisible(shapes_only)
        self._annot_fill_row.setVisible(shapes_only)
        self._annot_hint.setVisible(existing)
        for w in (self.annot_stroke_button, self.annot_width_spin,
                  self.annot_fill_button, self.annot_nofill_check,
                  self.annot_opacity_spin):
            w.setEnabled(not existing)
        self._annot_host.setVisible(True)

    # =====================================================================
    # Control slots -> styleEdited (one field each)
    # =====================================================================
    def _on_family_index(self, _index: int) -> None:
        """The user picked a family from the dropdown / completer popup (a real
        index change). The combo's items are all known families, so this always
        commits a valid family."""
        if self._loading:
            return
        self._commit_family(self.family_combo.currentText())

    def _on_family_commit(self) -> None:
        """The line edit settled (Enter / focus-out). Commit ONLY when the typed
        text exactly matches a known family (case-insensitive); otherwise revert
        to the target's current family so a partial/junk string never emits a
        non-existent family (PAGES_SPEC §5.6; invariant §0.3 requires an
        embeddable family)."""
        if self._loading:
            return
        typed = self.family_combo.currentText().strip()
        canonical = self._canonical_family(typed)
        if canonical is None:
            # Free-typed text that is not a known family: revert to the box's
            # current family (or the seeded default) WITHOUT emitting.
            self._revert_family()
            return
        self._commit_family(canonical)

    def _commit_family(self, family: str) -> None:
        """Emit the family edit using the EXACT shape the window/model expect
        (``{"font_family": <known family>}``). Gated on a known family; refreshes
        the fidelity hint. Selects the canonical item so the field shows the
        proper-cased family."""
        canonical = self._canonical_family(family)
        if canonical is None:
            return
        with self._loaded():
            self._select_family(canonical)
        self._refresh_fidelity()
        self.styleEdited.emit({"font_family": canonical})

    def _canonical_family(self, text: str) -> str | None:
        """The exactly-matching known family for ``text`` (case-insensitive), or
        None when ``text`` is not a family in ``available_families()``. Also
        honors a family added to the combo on the fly for an embedded-only
        original (``_select_family``), so re-committing the box's own family is
        never rejected."""
        if not text:
            return None
        # A typed commit needs the real census; force the blocking build if
        # the lazy load has not landed yet (same tradeoff as popup-open).
        self._ensure_families(block=True)
        low = text.casefold()
        if low in self._families_lower:
            for f in self._families:
                if f.casefold() == low:
                    return f
        # An embedded-only original family the inspector added on the fly is a
        # real combo item even though it is not in available_families().
        idx = self.family_combo.findText(text, Qt.MatchFixedString)
        if idx >= 0:
            return self.family_combo.itemText(idx)
        return None

    def _revert_family(self) -> None:
        """Restore the combo's edit text to the last KNOWN-GOOD family WITHOUT
        emitting, after free-typed junk settled. With ``NoInsert`` the combo's
        ``currentIndex`` still points at the last real selection, so its item text
        is the family to restore; falls back to the seeded default."""
        idx = self.family_combo.currentIndex()
        with self._loaded():
            if idx >= 0:
                self.family_combo.setEditText(self.family_combo.itemText(idx))
            else:
                self._select_family("Helvetica")
        self._refresh_fidelity()

    def _on_size_changed(self, value: float) -> None:
        if self._loading:
            return
        self.styleEdited.emit({"size": float(value)})

    def _on_bold_toggled(self, checked: bool) -> None:
        if self._loading:
            return
        self._refresh_fidelity()
        self.styleEdited.emit({"bold": bool(checked)})

    def _on_italic_toggled(self, checked: bool) -> None:
        if self._loading:
            return
        self._refresh_fidelity()
        self.styleEdited.emit({"italic": bool(checked)})

    def _on_underline_toggled(self, checked: bool) -> None:
        if self._loading:
            return
        self.styleEdited.emit({"underline": bool(checked)})

    def _on_strike_toggled(self, checked: bool) -> None:
        if self._loading:
            return
        self.styleEdited.emit({"strike": bool(checked)})

    def _on_color_clicked(self) -> None:
        if self._loading:
            return
        initial = QColor.fromRgbF(*self._color)
        chosen = QColorDialog.getColor(
            initial, self, "Text Color",
            QColorDialog.ColorDialogOption.DontUseNativeDialog,
        )
        if not chosen.isValid():
            return
        self._color = (chosen.redF(), chosen.greenF(), chosen.blueF())
        self._paint_swatch()
        self.styleEdited.emit({"color": self._color})

    def _on_align_toggled(self, alignment: str, checked: bool) -> None:
        """A paragraph alignment toggle turned ON (REFLOW_SPEC §R5.2). Routes to
        ``Edit.alignment`` via the window's apply_style. Only the ON transition
        emits (the exclusive group turns the previous one off automatically)."""
        if self._loading or not checked:
            return
        self.styleEdited.emit({"alignment": alignment})

    def _on_spacing_changed(self, value: float) -> None:
        if self._loading:
            return
        self.styleEdited.emit({"line_spacing": float(value)})

    # =====================================================================
    # ANNOTATION control slots -> annotStyleEdited (one field each, §5.5)
    # =====================================================================
    def _on_annot_stroke_clicked(self) -> None:
        if self._loading:
            return
        initial = QColor.fromRgbF(*self._annot_stroke)
        chosen = QColorDialog.getColor(
            initial, self, "Stroke Color",
            QColorDialog.ColorDialogOption.DontUseNativeDialog,
        )
        if not chosen.isValid():
            return
        self._annot_stroke = (chosen.redF(), chosen.greenF(), chosen.blueF())
        self._paint_annot_swatches()
        self.annotStyleEdited.emit({"stroke": self._annot_stroke})

    def _on_annot_fill_clicked(self) -> None:
        if self._loading:
            return
        initial = QColor.fromRgbF(*self._annot_fill)
        chosen = QColorDialog.getColor(
            initial, self, "Fill Color",
            QColorDialog.ColorDialogOption.DontUseNativeDialog,
        )
        if not chosen.isValid():
            return
        self._annot_fill = (chosen.redF(), chosen.greenF(), chosen.blueF())
        self._paint_annot_swatches()
        with self._loaded():               # picking a color implies a fill
            self.annot_nofill_check.setChecked(False)
        self.annotStyleEdited.emit({"fill": self._annot_fill})

    def _on_annot_nofill_toggled(self, checked: bool) -> None:
        if self._loading:
            return
        self.annotStyleEdited.emit(
            {"fill": None if checked else self._annot_fill})

    def _on_annot_width_changed(self, value: float) -> None:
        if self._loading:
            return
        self.annotStyleEdited.emit({"width": float(value)})

    def _on_annot_opacity_changed(self, value: float) -> None:
        if self._loading:
            return
        self.annotStyleEdited.emit({"opacity": float(value) / 100.0})

    # =====================================================================
    # Helpers
    # =====================================================================
    @contextmanager
    def _loaded(self):
        """Block control echo while programmatically loading a selection."""
        self._loading = True
        try:
            yield
        finally:
            self._loading = False

    def _set_enabled(self, on: bool) -> None:
        """Toggle between the live form and the empty-state hint."""
        self._form_host.setVisible(on)
        self.fidelity_label.setVisible(on)
        self._empty_hint.setVisible(not on)
        for w in (self.family_combo, self.size_spin, self._color_chip,
                  self.bold_button, self.italic_button,
                  self.underline_button, self.strike_button,
                  *self._align_buttons.values()):
            w.setEnabled(on)
        if not on:
            # The paragraph controls are also hidden in the empty state; they
            # re-show in set_target only for a ParagraphBox selection.
            self._para_host.setVisible(False)
            self.fidelity_label.clear()

    # --- lazy family census (final-review perf fix) ----------------------
    def _try_load_families(self) -> bool:
        """Install the family census IF the system index is published (no
        blocking). Returns True once loaded."""
        if self._families_loaded:
            return True
        fams = FontEngine.available_families_now()
        if fams is None:
            return False
        self._install_families(fams)
        return True

    def _poll_families(self) -> None:
        """50 ms poll while the prewarm scan runs; stops itself on load."""
        if self._try_load_families():
            timer = getattr(self, "_families_timer", None)
            if timer is not None:
                timer.stop()
                timer.deleteLater()
                self._families_timer = None

    def _ensure_families(self, block: bool = False) -> bool:
        """Make sure the census is installed; ``block=True`` builds the index
        synchronously (popup open / typed commit before the prewarm landed --
        the documented M4a tradeoff, paid only on explicit font UI)."""
        if self._try_load_families():
            return True
        if not block:
            return False
        self._install_families(FontEngine.available_families())
        return True

    def _install_families(self, fams: list) -> None:
        """Fill the combo with the census WITHOUT emitting, preserving the
        current selection/edit text (the seed default or an embedded-only
        family added on the fly re-attaches via ``_select_family``)."""
        self._families = list(fams)
        self._families_lower = {f.casefold() for f in self._families}
        self._families_loaded = True
        with self._loaded():
            current = self.family_combo.currentText()
            self.family_combo.clear()
            self.family_combo.addItems(self._families)
            self._apply_item_fonts()
            if current:
                self._select_family(current)

    def _apply_item_fonts(self) -> None:
        """Render each family name in its OWN font in the dropdown -- a live type
        specimen, so the picker reads as a preview (the user can SEE each face).
        Per-item Qt.FontRole; falls back silently to the combo font for any family
        that will not load."""
        for i in range(self.family_combo.count()):
            self._set_item_font(i, self.family_combo.itemText(i))

    def _set_item_font(self, idx: int, family: str) -> None:
        if idx < 0:
            return
        try:
            f = QFont(family)
            f.setPointSize(13)
            self.family_combo.setItemData(idx, f, Qt.FontRole)
        except Exception:
            pass

    def _select_family(self, family: str) -> None:
        """Select ``family`` in the combo if present; otherwise add it once so
        the inspector always shows the box's real family even when it is not in
        the scanned system index (e.g. an embedded-only original or a scan
        typeface shown by its real name)."""
        idx = self.family_combo.findText(family, Qt.MatchFixedString)
        if idx < 0 and family:
            self.family_combo.addItem(family)
            idx = self.family_combo.findText(family, Qt.MatchFixedString)
            self._set_item_font(idx, family)
        if idx >= 0:
            self.family_combo.setCurrentIndex(idx)

    @staticmethod
    def _norm_color(color) -> tuple:
        try:
            r, g, b = color
            return (float(r), float(g), float(b))
        except (TypeError, ValueError):
            return (0.0, 0.0, 0.0)

    @staticmethod
    def _align_icon(name: str) -> QIcon:
        """A small four-line alignment glyph (left / center / right / justify),
        with alternating ragged line widths so the alignment is unmistakable.
        Drawn in the secondary text color; the :checked QSS recolors the button,
        and the icon reads against both states."""
        n = 18
        rows = (0.22, 0.40, 0.58, 0.76)
        pm = QPixmap(n, n)
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing, True)
        pen = QPen(QColor(theme.TEXT_SECONDARY))
        pen.setWidthF(1.6)
        pen.setCapStyle(Qt.RoundCap)
        p.setPen(pen)
        for i, ry in enumerate(rows):
            y = n * ry
            if name == "justify":
                x0, x1 = n * 0.18, n * 0.82
            elif name == "left":
                x0 = n * 0.18
                x1 = n * (0.82 if i % 2 == 0 else 0.64)
            elif name == "right":
                x1 = n * 0.82
                x0 = n * (0.18 if i % 2 == 0 else 0.36)
            else:  # center
                half = (0.32 if i % 2 == 0 else 0.23)
                x0, x1 = n * (0.5 - half), n * (0.5 + half)
            p.drawLine(x0, y, x1, y)
        p.end()
        return QIcon(pm)

    def _paint_swatch(self) -> None:
        """Update the color chip: fill the swatch with the current color (a
        contrast ring keeps a near-white color visible) and show its hex."""
        r, g, b = (max(0, min(255, int(round(c * 255)))) for c in self._color)
        self._color_swatch.setStyleSheet(
            f"QFrame#InspectorColorSwatch {{ background: rgb({r},{g},{b}); "
            f"border-radius: 5px; border: 1px solid rgba(255,255,255,.22); }}")
        self._color_hex.setText(f"#{r:02X}{g:02X}{b:02X}")

    def _paint_annot_swatches(self) -> None:
        """Repaint the stroke + fill swatch buttons as plain color squares
        framed by a solid contrast ring (so light colors stay visible)."""
        for button, color in ((self.annot_stroke_button, self._annot_stroke),
                              (self.annot_fill_button, self._annot_fill)):
            n = 20
            pm = QPixmap(n, n)
            pm.fill(Qt.transparent)
            p = QPainter(pm)
            p.setRenderHint(QPainter.Antialiasing, True)
            ring = QPen(QColor(theme.CHROME_BORDER))
            ring.setWidthF(1.25)
            p.setPen(ring)
            p.setBrush(QColor.fromRgbF(*color))
            p.drawRoundedRect(QRectF(2.0, 2.0, n - 4.0, n - 4.0), 3.0, 3.0)
            p.end()
            button.setIcon(QIcon(pm))

    def _refresh_fidelity(self) -> None:
        """Show how the currently picked family + style will resolve on save.

        Mirrors the status chip's tier dot (BUILD_SPEC §4.1). This is a display
        hint only; it does not touch the model. ``resolve_family`` is pure +
        cached, so calling it here is cheap and matches what save_as will do.
        """
        if not self._form_host.isVisible():
            self.fidelity_label.clear()
            return
        family = self.family_combo.currentText()
        if not family:
            self.fidelity_label.clear()
            return
        engine = self._engine
        if engine is None:
            self.fidelity_label.clear()
            return
        rf = engine.resolve_family(
            family, self.bold_button.isChecked(),
            self.italic_button.isChecked(), "Ag",
        )
        # The full status card: swatch + tier word + family-specific subtitle +
        # consequence sentence + the 3-pip tier ladder (the core trust signal).
        self.fidelity_label.set_state(rf.tier, family)

    # --- font engine for the fidelity hint -------------------------------
    # The window injects a live engine (bound to the open doc) via
    # set_font_engine so resolve_family runs against the same instance + cache
    # the save path uses. Falls back to a transient engine-less state (no hint)
    # before a document is open.
    _engine: FontEngine | None = None

    def set_font_engine(self, engine: FontEngine | None) -> None:
        """Bind the document's FontEngine so the fidelity hint resolves through
        the same instance/cache as save_as. Called by the window on open."""
        self._engine = engine
        self._refresh_fidelity()
