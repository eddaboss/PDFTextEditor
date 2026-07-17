"""Design tokens + the global stylesheet for the editor chrome.

Single source of truth for colors, type, and metrics (BUILD_SPEC §7). Both the
canvas (page_view) and the chrome (main_window) import these so the look is
consistent and tweakable in one place. Colors are plain hex/rgba strings plus a
handful of ``QColor`` accessors for the scene items the canvas paints.

DESIGN DIRECTION (2026): "Clay." Warm, paper-forward and editorial -- a crafted
writing tool (Bear / Ulysses / iA Writer), never a SaaS dashboard. ONE accent --
clay / terracotta -- carries the active tool, the current selection, and the
primary action, and nothing else; it is never decoration. Everything else is a
considered warm neutral. The white PDF page is the hero and the only bright
surface in either mode; it floats on a calm warm gutter under one gentle,
warm-tinted shadow. Surfaces separate by tone and low-opacity warm hairlines,
never hard gray rules. Corners are soft (8-14px), spacing is generous, and there
are NO gradients (a gradient toolbar is the single biggest "old Office" tell, and
gradients render differently per Qt platform).

TWO REAL MODES, not a mechanical invert (see the Clay color system):
  * Light  = bright warm paper so the white page belongs (the default).
  * Dark   = a refined WARM near-black (browned, not blue-charcoal, never gloomy).
The active mode is chosen from the OS appearance at startup (``_detect_os_mode``)
and installed onto the module-level tokens by ``_apply``; every name + accessor
below reflects whichever palette is live, so the ~270 call sites and the QSS pick
up the right colors with no per-call-site change. ``set_mode``/``current_mode``
expose it (the app reads ``current_mode`` to match Qt's native title-bar scheme).

CROSS-PLATFORM: every value here is plain hex/rgba (QSS is a CSS subset, no
OKLCH) and the type stack defers to the OS UI font, so the same tokens render
natively on macOS and Windows. ``SHEET_WHITE`` is reserved for the PDF page and
is the ONLY bright surface; the chrome must never tint the rendered document.
Every name and accessor below is load-bearing for the widgets + the QSS.
"""

from __future__ import annotations

import sys

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QColor, QFont, QFontDatabase

# --- Mode-independent constants -------------------------------------------
# The PDF page is the one bright surface in BOTH modes and is never re-tinted.
SHEET_WHITE = "#FFFFFF"

# Selection-overlay geometry (PAGES_SPEC §4.2): a 2-pass halo + accent outline
# plus grab handles so a selected box reads at a glance. The white under-stroke
# is a constant -- it is drawn on the page, which is always white.
SELECTION_OUTLINE_W = 2.25                            # accent pen width
SELECTION_OUTLINE_HALO = "rgba(255,255,255,.95)"     # white under-stroke
HANDLE_PX = 11.0                                     # handle square edge

# --- Clay palettes --------------------------------------------------------
# Two deliberate modes. Each dict maps the public token name -> its value for
# that mode; ``_apply`` copies the live one onto the module globals. Surfaces
# layer by tone: in light the recessed gutter is the DARKEST warm surface so the
# white page lifts; in dark the chrome browns step rail -> panel -> bar and the
# gutter is deepest. ONE clay accent (light: clay-500 #C2643F ink / clay-600
# #AA4E2C fill; dark: a brightened clay #DB8A63 ink / the same #AA4E2C fill).
_LIGHT = {
    # Surfaces (warm paper)
    "CHROME_BG": "#FBF9F5",            # top bar / status (lightest chrome)
    "PANEL_BG": "#F5F1EA",             # Format + Pages panels
    "RAIL_BG": "#F5F1EA",              # activity rail / canvas tab strip (warm-50, = panel)
    "CANVAS_BG": "#ECE6DC",            # the gutter the white page floats on
    "CONTROL_FILL": "#E5DED2",         # recessed inputs / cards / segments
    "CONTROL_FILL_DISABLED": "#ECE6DC",
    "CARD_BG": "#FBF9F5",              # a Recent gallery tile
    "CARD_BG_HOVER": "#FFFFFF",
    "SEG_FILL": "#E5DED2",             # segmented-control container (= CONTROL_FILL)
    # Hairlines (warm, never a hard gray rule)
    "CHROME_BORDER": "rgba(58,52,46,0.13)",       # panel / bar / divider hairline
    "BORDER_STRONG": "rgba(58,52,46,0.20)",       # recessed-control outline, one step up
    "DIVIDER": "rgba(58,52,46,0.08)",
    "SHEET_SHADOW": "rgba(58,40,26,0.16)",        # warm page float (ambient layer)
    "SHEET_SHADOW_CONTACT": "rgba(58,40,26,0.08)",# tighter contact layer under the page
    # Ink
    "TEXT_PRIMARY": "#2A2520",         # warm near-black
    "TEXT_SECONDARY": "#6B6256",
    "TEXT_TERTIARY": "#897E6E",        # warm-500
    "PANEL_HEADER": "#8C8273",         # section eyebrow (caps, tracked)
    "TOOLBAR_ICON": "#574E43",         # line-icon ink on the warm bar
    "TOOLBAR_ICON_DISABLED": "#A99D8B",
    # Clay accent
    "ACCENT": "#C2643F",               # clay-500: active tool / selection ink / accent text
    "ACCENT_FILL": "#AA4E2C",          # clay-600: fill behind white text (AA white 5.5:1)
    "ACCENT_PRESSED": "#8B3E23",       # clay-700: a filled CTA's :hover/:pressed (darken one step)
    "ACCENT_DEEP": "#6B311F",          # clay-800: one step deeper still
    "ACCENT_TEXT": "#8B3E23",          # clay-700: ON label/icon on a light clay wash
    "ACCENT_HOVER": "rgba(194,100,63,0.10)",       # hovered / checked wash
    "ACCENT_ACTIVE_WASH": "rgba(194,100,63,0.16)", # active-rail / checked-segment wash
    "ACCENT_BORDER": "rgba(194,100,63,0.42)",
    "ACCENT_SELECTION": "rgba(194,100,63,0.22)",
    "SELECTION_OUTLINE": "#C2643F",    # canvas selection rect + handles (= ACCENT)
    # Neutral washes for flat chrome (low-opacity warm ink)
    "WASH_HOVER": "rgba(58,52,46,0.05)",
    "WASH_STRONG": "rgba(58,52,46,0.07)",
    "WASH_PRESSED": "rgba(58,52,46,0.09)",
    "SCROLL_HANDLE": "rgba(58,52,46,0.22)",
    "SCROLL_HANDLE_HOVER": "rgba(58,52,46,0.34)",
    # Edited-run marks (drawn on the white page -> identical in both modes)
    "EDITED_TINT": "rgba(198,138,46,0.14)",
    "EDITED_UNDERLINE": "#C68A2E",     # ochre underline on an edited run
    "EDITED_TEXT": "#2A2520",
    # Semantic status, deliberately OFF the clay accent
    "DANGER": "#C0453B",               # warm red
    "TAB_DOT_ACTIVE": "#3E9E6B",       # green: open tab, saved
    "TAB_DOT_OTHER": "#C68A2E",        # system amber: another open tab, saved
    "FIDELITY_GREEN": "#3E9E6B",       # TIER_EMBEDDED
    "FIDELITY_BLUE": "#4C86C6",        # TIER_SYSTEM
    "FIDELITY_AMBER": "#C68A2E",       # TIER_BASE14
    "FIDELITY_GREEN_BG": "rgba(62,158,107,0.15)",
    "FIDELITY_BLUE_BG": "rgba(76,134,198,0.15)",
    "FIDELITY_AMBER_BG": "rgba(198,138,46,0.15)",
}

_DARK = {
    # Surfaces (browned near-blacks, layered by tone)
    "CHROME_BG": "#251E19",            # top bar / status (lightest chrome)
    "PANEL_BG": "#1F1916",             # side panels
    "RAIL_BG": "#1A1511",              # activity rail (darkest chrome)
    "CANVAS_BG": "#15110E",            # the gutter (deepest tone)
    "CONTROL_FILL": "#1B1612",         # recessed control fill on dark
    "CONTROL_FILL_DISABLED": "#211B17",
    "CARD_BG": "#221C18",
    "CARD_BG_HOVER": "#271F1A",
    "SEG_FILL": "#1B1612",
    # Hairlines (low-opacity warm light)
    "CHROME_BORDER": "rgba(255,248,240,0.09)",
    "BORDER_STRONG": "rgba(255,248,240,0.15)",
    "DIVIDER": "rgba(255,248,240,0.05)",
    "SHEET_SHADOW": "rgba(0,0,0,0.55)",            # deep page float (ambient layer)
    "SHEET_SHADOW_CONTACT": "rgba(0,0,0,0.42)",   # tighter contact layer under the page
    # Ink (warm off-white)
    "TEXT_PRIMARY": "#ECE4DA",
    "TEXT_SECONDARY": "#A89D8F",
    "TEXT_TERTIARY": "#786E62",
    "PANEL_HEADER": "#8B8173",
    "TOOLBAR_ICON": "#B7AC9E",
    "TOOLBAR_ICON_DISABLED": "#5E564B",
    # Clay accent (brightened ink on dark; same warm fill)
    "ACCENT": "#DB8A63",               # clay-light
    "ACCENT_FILL": "#AA4E2C",
    "ACCENT_PRESSED": "#BD5E3A",
    "ACCENT_DEEP": "#A8472A",
    "ACCENT_TEXT": "#E2A584",
    "ACCENT_HOVER": "rgba(219,138,99,0.14)",
    "ACCENT_ACTIVE_WASH": "rgba(219,138,99,0.22)",
    "ACCENT_BORDER": "rgba(219,138,99,0.42)",
    "ACCENT_SELECTION": "rgba(219,138,99,0.26)",
    "SELECTION_OUTLINE": "#DB8A63",
    # Neutral washes (low-opacity warm light)
    "WASH_HOVER": "rgba(255,248,240,0.05)",
    "WASH_STRONG": "rgba(255,248,240,0.08)",
    "WASH_PRESSED": "rgba(255,248,240,0.10)",
    "SCROLL_HANDLE": "rgba(255,248,240,0.13)",
    "SCROLL_HANDLE_HOVER": "rgba(255,248,240,0.22)",
    # Edited-run marks (on the white page -> same as light)
    "EDITED_TINT": "rgba(198,138,46,0.14)",
    "EDITED_UNDERLINE": "#C68A2E",
    "EDITED_TEXT": "#ECE4DA",
    # Semantic status: the design fixes these hues across BOTH modes (they sit
    # OFF the clay accent so trust state never reads as "selected"). Not re-tinted.
    "DANGER": "#C0453B",
    "TAB_DOT_ACTIVE": "#3E9E6B",
    "TAB_DOT_OTHER": "#C68A2E",
    "FIDELITY_GREEN": "#3E9E6B",
    "FIDELITY_BLUE": "#4C86C6",
    "FIDELITY_AMBER": "#C68A2E",
    "FIDELITY_GREEN_BG": "rgba(62,158,107,0.15)",
    "FIDELITY_BLUE_BG": "rgba(76,134,198,0.15)",
    "FIDELITY_AMBER_BG": "rgba(198,138,46,0.15)",
}

# RGBA tuples for the QColor accessors the canvas paints with. The clay base rgb
# differs by mode (clay-500 vs the brightened clay-light); the ochre edited marks
# and the white selection halo are constant (they sit on the white page).
_RGBA_LIGHT = {
    "accent": (194, 100, 63),
    "accent_hover": (194, 100, 63, 26),       # ~.10
    "accent_selection": (194, 100, 63, 56),   # ~.22
    "editor_selection": (194, 100, 63, 96),   # ~.38 band over the white page
    "edited_tint": (198, 138, 46, 36),        # ~.14
    "edited_hover": (198, 138, 46, 66),       # ~.26
    "sheet_shadow": (58, 40, 26, 70),         # warm-tinted page shadow
    "selection_halo": (255, 255, 255, 242),
}
_RGBA_DARK = {
    "accent": (219, 138, 99),
    "accent_hover": (219, 138, 99, 36),       # ~.14
    "accent_selection": (219, 138, 99, 66),   # ~.26
    "editor_selection": (219, 138, 99, 100),
    "edited_tint": (198, 138, 46, 36),
    "edited_hover": (198, 138, 46, 66),
    "sheet_shadow": (0, 0, 0, 150),
    "selection_halo": (255, 255, 255, 242),
}

MODE = "light"
_RGBA = _RGBA_LIGHT


def _apply(mode: str) -> None:
    """Install ``mode``'s palette onto the module-level tokens. The QSS f-string
    and every ``theme.NAME`` reference read these globals, so one call recolors
    the whole app to the chosen mode."""
    global MODE, _RGBA
    MODE = "dark" if mode == "dark" else "light"
    _RGBA = _RGBA_DARK if MODE == "dark" else _RGBA_LIGHT
    globals().update(_DARK if MODE == "dark" else _LIGHT)


def _detect_os_mode() -> str:
    """The OS light/dark appearance, resolved WITHOUT a QApplication (this runs
    at import, before the app object exists) so the baked QSS constants are
    correct from the first window. macOS reads the global ``AppleInterfaceStyle``
    default; Windows reads the Personalize registry key. Anything else -> light."""
    try:
        if sys.platform == "darwin":
            import subprocess
            out = subprocess.run(
                ["defaults", "read", "-g", "AppleInterfaceStyle"],
                capture_output=True, text=True, timeout=1.5)
            return "dark" if out.stdout.strip() == "Dark" else "light"
        if sys.platform.startswith("win"):
            import winreg
            key = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize")
            value, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
            return "light" if value else "dark"
    except Exception:  # noqa: BLE001 - any failure just falls back to light
        pass
    return "light"


def set_mode(mode: str) -> None:
    """Flip the live palette WITHOUT re-applying any stylesheet (headless/tests).
    The runtime UI switch goes through ``retheme``."""
    _apply(mode)


def current_mode() -> str:
    return MODE


class _ThemeEvents(QObject):
    """Fires after the live palette changes. Widgets that own their OWN
    stylesheet or paint with ``QColor`` (the canvas, the recent cards) connect
    here to refresh; everything on the global app stylesheet is handled by
    ``retheme`` re-applying it."""

    changed = Signal()


# ponytail: one module-level emitter, not a per-widget observer registry.
events = _ThemeEvents()


def retheme(mode: str) -> None:
    """The ONE runtime mode switch: flip tokens, re-apply the app-level
    stylesheet, sync Qt's native color scheme, then notify listeners. Safe
    before a QApplication exists (then it just flips tokens + emits)."""
    if mode == MODE:
        return
    _apply(mode)
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance()
    if app is not None:
        app.setStyleSheet(global_stylesheet())
        try:
            app.styleHints().setColorScheme(
                Qt.ColorScheme.Dark if MODE == "dark" else Qt.ColorScheme.Light)
        except (AttributeError, TypeError):
            pass
    events.changed.emit()


def detect_os_mode() -> str:
    """Public alias for the OS appearance probe (consumed by the live OS-follow
    listener and the 'Use system' menu choice)."""
    return _detect_os_mode()


# Install the OS-detected palette at import, before any widget or baked QSS
# constant reads a token.
_apply(_detect_os_mode())

# --- Metrics --------------------------------------------------------------
TOOLBAR_HEIGHT = 50
STATUS_HEIGHT = 30
BUTTON_SIZE = 32
ICON_SIZE = 22
BUTTON_RADIUS = 8                # buttons / icon buttons (Clay 8px band)
CONTROL_RADIUS = 10              # inputs / selects / recessed cards (Clay 10px)
SHEET_MARGIN = 40                # gutter around the sheet in the scene
PAGE_GAP = 18                    # vertical gap between stacked pages
SHADOW_BLUR = 40                 # a soft, large page lift so the sheet floats on dark
SHADOW_OFFSET_Y = 11
FOCUS_RING = 2
WINDOW_MIN_W = 780
WINDOW_MIN_H = 560
WINDOW_DEFAULT_W = 1200
WINDOW_DEFAULT_H = 920
RAIL_WIDTH = 64                  # the far-left activity rail
RAIL_BUTTON = 52                 # a rail mode button (icon over a tiny caps label)

# --- Type -----------------------------------------------------------------
# The OS UI font (San Francisco on macOS, Segoe UI on Windows), a fixed scale
# with real size + weight contrast. Caption (11) < label (12) < body (13) <
# title (16). An empty family string lets Qt pick the native system UI font so
# the chrome reads native on every platform.
UI_FONT_FAMILY = ""
UI_FONT_SIZE = 13
UI_FONT_CAPTION = 11             # section eyebrows (caps, tracked, semibold)
UI_FONT_LABEL = 12               # field labels
UI_FONT_TITLE = 16               # panel titles ("Format", "Pages")
UI_FONT_RAIL = 9                 # the tiny caps label under each rail icon
MONO_FONT_FAMILY = "monospace"
MONO_FONT_SIZE = 12


def ui_font(size: int = UI_FONT_SIZE, *, medium: bool = False,
            semibold: bool = False) -> QFont:
    """The system UI font at a given size and optional weight.

    Uses Qt's canonical system-UI font (San Francisco on macOS, Segoe UI on
    Windows) via QFontDatabase, which guarantees full glyph coverage. An empty
    QFont("") family was matching a fallback that lacked digits/punctuation, so
    Qt fell back PER GLYPH and the differing advances spaced regular-weight
    numbers and labels out ("1 5 . 0", "2 0 characters") -- the systemFont has
    every glyph, so the spacing is tight again. ``UI_FONT_FAMILY`` (when set)
    still overrides, for tests/theming."""
    if UI_FONT_FAMILY:
        f = QFont(UI_FONT_FAMILY)
    else:
        f = QFontDatabase.systemFont(QFontDatabase.SystemFont.GeneralFont)
    f.setPixelSize(size)
    if semibold:
        f.setWeight(QFont.DemiBold)
    elif medium:
        f.setWeight(QFont.Medium)
    return f


def caps_header_font() -> QFont:
    """The tracked small-caps section eyebrow (TEXT STYLE / PARAGRAPH / RECENT).
    11px semibold with positive letter-spacing so an uppercase label reads as a
    deliberate section eyebrow instead of shouting. QSS has no letter-spacing,
    so the tracking is set here on the QFont and applied at the call sites."""
    f = ui_font(UI_FONT_CAPTION, semibold=True)
    f.setLetterSpacing(QFont.PercentageSpacing, 108)
    return f


def mono_font(size: int = MONO_FONT_SIZE, *, semibold: bool = False) -> QFont:
    """The monospaced UI font (zoom %, page numbers). Qt's canonical FixedFont
    (Menlo on macOS, Consolas on Windows) -- full digit coverage, so the
    page/zoom numbers no longer split ("1 5 pt"). ``semibold`` gives the numerals
    the design's 600 weight (the toolbar/footer numbers read as deliberate)."""
    if MONO_FONT_FAMILY and MONO_FONT_FAMILY != "monospace":
        f = QFont(MONO_FONT_FAMILY)
    else:
        f = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
    f.setPixelSize(size)
    f.setStyleHint(QFont.Monospace)
    if semibold:
        f.setWeight(QFont.DemiBold)
    return f


def display_font(size: int = 33) -> QFont:
    """The ONE editorial serif -- Newsreader (bundled in assets/fonts, registered
    by FontEngine.register_bundled_fonts) -- reserved for DISPLAY MOMENTS only:
    the empty-state headline and the brand wordmark. It is the single "warm"
    typographic gesture and is never used for labels or body. Falls back through
    a system serif chain (Iowan Old Style on macOS, Georgia on both, then Times)
    so it degrades gracefully and stays Qt-portable to Windows."""
    f = QFont()
    f.setFamilies(["Newsreader", "Iowan Old Style", "Georgia",
                   "Times New Roman", "serif"])
    f.setStyleHint(QFont.Serif)
    f.setPixelSize(size)
    f.setWeight(QFont.Medium)                       # 500 -- a real bundled face
    f.setLetterSpacing(QFont.PercentageSpacing, 99)  # -0.01em display tracking
    return f


# --- QColor accessors (for QGraphicsScene items) --------------------------
def color_canvas_bg() -> QColor:
    return QColor(CANVAS_BG)


def color_sheet_white() -> QColor:
    return QColor(SHEET_WHITE)


def color_sheet_shadow() -> QColor:
    """The page drop-shadow ink: one gentle, warm-tinted lift in light mode and a
    deeper soft shadow in dark, so the white sheet floats off the gutter."""
    return QColor(*_RGBA["sheet_shadow"])


def page_shadow_ambient() -> tuple[int, int, QColor]:
    """(blurRadius, offsetY, color) for the page's soft AMBIENT float shadow --
    the wide, diffuse layer. Warm-tinted and shallow in light; deep in dark."""
    if MODE == "dark":
        return 46, 16, QColor(0, 0, 0, 140)        # ~.55
    return 34, 10, QColor(58, 40, 26, 41)          # warm, ~.16


def page_shadow_contact() -> tuple[int, int, QColor]:
    """(blurRadius, offsetY, color) for the tighter CONTACT shadow that grounds
    the page right at its edge -- the second, closer layer."""
    if MODE == "dark":
        return 14, 4, QColor(0, 0, 0, 107)         # ~.42
    return 8, 2, QColor(58, 40, 26, 20)            # warm, ~.08


def color_accent() -> QColor:
    return QColor(ACCENT)


def color_danger() -> QColor:
    """The danger/warning red, used for the overflow cue on a selection whose
    reflowed text grew past its box bottom (REFLOW_SPEC §R2.5)."""
    return QColor(DANGER)


def color_accent_hover() -> QColor:
    """Faint clay wash for a hovered (unedited) line on the page."""
    return QColor(*_RGBA["accent_hover"])


def color_accent_selection() -> QColor:
    return QColor(*_RGBA["accent_selection"])


def color_edited_tint() -> QColor:
    return QColor(*_RGBA["edited_tint"])    # ochre wash on edited runs (on the white page)


def color_edited_hover() -> QColor:
    """Hover wash for an ALREADY-EDITED run: a slightly stronger ochre so
    hovering an edited run reads as ochre, never the clay unedited-hover wash."""
    return QColor(*_RGBA["edited_hover"])


def color_edited_underline() -> QColor:
    return QColor(EDITED_UNDERLINE)


def color_fidelity(tier: int) -> QColor:
    """Map a ResolvedFont.tier to its fidelity-dot color."""
    return QColor({1: FIDELITY_GREEN, 2: FIDELITY_BLUE, 3: FIDELITY_AMBER}
                  .get(tier, FIDELITY_AMBER))


def color_selection_outline() -> QColor:
    """The accent stroke for the canvas selection rectangle (BUILD_SPEC §3.1)."""
    return QColor(SELECTION_OUTLINE)


def color_selection_handle() -> QColor:
    """Fill for the 8 resize handles on a selected box (BUILD_SPEC §3.1)."""
    return QColor(SELECTION_OUTLINE)


def color_selection_halo() -> QColor:
    """The white under-stroke drawn beneath the accent selection outline so it
    reads on both light and dark page regions (PAGES_SPEC §4.2/§5.7)."""
    return QColor(*_RGBA["selection_halo"])


def color_editor_selection() -> QColor:
    """Selection highlight INSIDE the inline text editor (text-editing UX §2.3):
    the clay accent at a strong-but-translucent alpha, so it reads as a selection
    band over the white sheet while the glyphs underneath keep their own ink."""
    return QColor(*_RGBA["editor_selection"])


def global_stylesheet() -> str:
    """The application-wide QSS (BUILD_SPEC §7). Warm paper surfaces layered by
    tone, with low-opacity warm hairlines, soft rounded corners, and a single
    clay accent for active/selected/primary states. No gradients. Reads the live
    palette, so it renders the light or dark Clay mode installed by ``_apply``."""
    return f"""
    QMainWindow, QWidget#CanvasContainer {{
        background: {CANVAS_BG};
    }}

    /* --- Top toolbar -------------------------------------------------- */
    QToolBar#MainToolbar {{
        background: {CHROME_BG};
        border: none;
        border-bottom: 1px solid {CHROME_BORDER};
        padding: 5px 12px;
        spacing: 2px;
    }}
    QToolBar#MainToolbar QToolButton {{
        background: transparent;
        border: 1px solid transparent;
        border-radius: {BUTTON_RADIUS}px;
        padding: 4px;
        min-width: {BUTTON_SIZE}px;
        min-height: {BUTTON_SIZE}px;
        color: {TEXT_PRIMARY};
    }}
    QToolBar#MainToolbar QToolButton:hover {{
        background: {WASH_HOVER};
    }}
    QToolBar#MainToolbar QToolButton:pressed {{
        background: {WASH_PRESSED};
    }}
    QToolBar#MainToolbar QToolButton:checked {{
        background: {ACCENT_HOVER};
        border-color: {ACCENT_BORDER};
        color: {ACCENT};
    }}
    QToolBar#MainToolbar QToolButton:disabled {{
        color: {TOOLBAR_ICON_DISABLED};
    }}
    QToolBar#MainToolbar QToolButton::menu-indicator {{
        subcontrol-position: right center;
        right: 2px;
    }}
    /* Compact global icon buttons (undo / redo / share): 30px pills, centered
       in the bar by their container (not stretched to bar height). */
    QToolBar#MainToolbar QToolButton#TopGlobalBtn {{
        min-width: 30px; min-height: 30px; padding: 0px; border-radius: 8px;
    }}
    /* Centered document title (saved/edited dot + filename + page meta). */
    QLabel#TitlebarDoc {{ color: {TEXT_PRIMARY}; }}
    QLabel#TitlebarMeta {{ color: {TEXT_TERTIARY}; }}

    /* Unified zoom bar: [ Fit | - 100% + ] -- ONE bordered neutral pill on the
       recessed control fill, no dropdown. The children are flat/transparent so
       the pill shows through; the clay accent is reserved for Save. */
    QFrame#ZoomBar {{
        background: {CONTROL_FILL};
        border: 1px solid {BORDER_STRONG};
        border-radius: {CONTROL_RADIUS}px;
    }}
    QFrame#ZoomBar QFrame#ZoomDivider {{
        background: {CHROME_BORDER};
        border: none;
    }}
    QFrame#ZoomBar QToolButton#ZoomFitBtn {{
        border: none; background: transparent; border-radius: {BUTTON_RADIUS}px;
        color: {TEXT_PRIMARY}; padding: 0px 8px;
    }}
    QFrame#ZoomBar QToolButton#ZoomFitBtn:hover {{ background: {WASH_HOVER}; }}
    QFrame#ZoomBar QToolButton#ZoomStepBtn {{
        border: none; background: transparent;
        min-width: 22px; min-height: 22px; padding: 0px; border-radius: 6px;
        color: {TEXT_SECONDARY};
    }}
    QFrame#ZoomBar QToolButton#ZoomStepBtn:hover {{
        background: {WASH_HOVER}; color: {TEXT_PRIMARY};
    }}
    QFrame#ZoomBar QToolButton#ZoomStepBtn:disabled {{ color: {TEXT_TERTIARY}; }}
    QFrame#ZoomBar QToolButton#ZoomButton {{
        border: none; background: transparent;
        /* The caret is part of the value TEXT (see _ZOOM_CARET), so "NN% v" is
           one unit -- min-width slack centres the whole thing between - and +. */
        min-width: 52px; padding: 0px 4px; color: {TEXT_PRIMARY};
    }}
    /* The dropdown caret lives in the button TEXT (see _ZOOM_CARET) so the whole
       "NN% v" centres between the - and + steps; the native menu-indicator is
       hidden because its asymmetric reserved space pulls the value off-centre. */
    QFrame#ZoomBar QToolButton#ZoomButton::menu-indicator {{
        image: none; width: 0px;
    }}
    QFrame#ZoomBar QToolButton#ZoomButton:hover {{
        background: {WASH_HOVER}; border-radius: {BUTTON_RADIUS}px;
    }}

    /* Primary Save CTA: a SPLIT pill -- a "Save" half + a caret half. The ONE
       clay accent appears ONLY here, and ONLY when there are unsaved changes
       (the [dirty="true"] variant). CLEAN = a quiet neutral pill. */
    QToolBar#MainToolbar QWidget#SaveSplit {{ background: transparent; }}

    /* DIRTY -- filled clay, white text, hover/press darken one step. */
    QToolBar#MainToolbar QPushButton#SaveButton[dirty="true"] {{
        background: {ACCENT_FILL}; color: #FFFFFF; border: none;
        border-top-left-radius: {CONTROL_RADIUS}px;
        border-bottom-left-radius: {CONTROL_RADIUS}px;
        border-top-right-radius: 0px; border-bottom-right-radius: 0px;
        padding: 0px 14px 0px 12px; font-size: 13px; font-weight: 500;
    }}
    QToolBar#MainToolbar QPushButton#SaveButton[dirty="true"]:hover {{ background: {ACCENT_PRESSED}; }}
    QToolBar#MainToolbar QPushButton#SaveButton[dirty="true"]:pressed {{ background: {ACCENT_DEEP}; }}
    QToolBar#MainToolbar QPushButton#SaveCaret[dirty="true"] {{
        background: {ACCENT_FILL}; border: none;
        border-left: 1px solid rgba(255,255,255,0.28);
        border-top-right-radius: {CONTROL_RADIUS}px;
        border-bottom-right-radius: {CONTROL_RADIUS}px;
        border-top-left-radius: 0px; border-bottom-left-radius: 0px;
        min-width: 27px;
    }}
    QToolBar#MainToolbar QPushButton#SaveCaret[dirty="true"]:hover {{ background: {ACCENT_PRESSED}; }}

    /* CLEAN -- a quiet neutral pill: BORDER_STRONG outline, control fill, no
       shadow; the caret owns the inner hairline (border-left). */
    QToolBar#MainToolbar QPushButton#SaveButton[dirty="false"] {{
        background: {CONTROL_FILL}; color: {TEXT_SECONDARY};
        border: 1px solid {BORDER_STRONG}; border-right: none;
        border-top-left-radius: {CONTROL_RADIUS}px;
        border-bottom-left-radius: {CONTROL_RADIUS}px;
        border-top-right-radius: 0px; border-bottom-right-radius: 0px;
        padding: 0px 14px 0px 12px; font-size: 13px; font-weight: 500;
    }}
    QToolBar#MainToolbar QPushButton#SaveButton[dirty="false"]:hover {{
        background: {WASH_HOVER}; color: {TEXT_PRIMARY};
    }}
    QToolBar#MainToolbar QPushButton#SaveButton:disabled {{ color: {TOOLBAR_ICON_DISABLED}; }}
    QToolBar#MainToolbar QPushButton#SaveCaret[dirty="false"] {{
        background: {CONTROL_FILL};
        border: 1px solid {BORDER_STRONG}; border-left: 1px solid {BORDER_STRONG};
        border-top-right-radius: {CONTROL_RADIUS}px;
        border-bottom-right-radius: {CONTROL_RADIUS}px;
        border-top-left-radius: 0px; border-bottom-left-radius: 0px;
        min-width: 27px;
    }}
    QToolBar#MainToolbar QPushButton#SaveCaret[dirty="false"]:hover {{ background: {WASH_HOVER}; }}
    /* We draw our own chevron icon -- hide Qt's menu-indicator arrow. */
    QToolBar#MainToolbar QPushButton#SaveCaret::menu-indicator {{ image: none; width: 0; }}

    /* Group dividers: a short, soft hairline that floats inside the bar
       rather than a full-height hard rule. */
    QToolBar#MainToolbar::separator {{
        background: {CHROME_BORDER};
        width: 1px;
        margin: 9px 11px;
    }}

    /* --- Footer page navigator: "Page [N] of M". [N] is an editable MONO
       field in a recessed box; the words are quiet ui-font text. No pill, no
       chevrons. Every colour is a mode-aware token so it reads in dark too. - */
    QWidget#FooterNavPill {{
        background: transparent;
        border: none;
    }}
    QStatusBar#MainStatus QToolButton {{
        background: transparent; border: none; border-radius: 6px;
        padding: 1px; color: {TEXT_SECONDARY};
    }}
    QStatusBar#MainStatus QToolButton:hover {{
        background: {WASH_HOVER}; color: {TEXT_PRIMARY};
    }}
    QStatusBar#MainStatus QToolButton:disabled {{ color: {TOOLBAR_ICON_DISABLED}; }}

    /* Editable page field: a recessed box; hover -> clay border; focus -> clay
       ring. The mono digits read as TEXT_PRIMARY via the QFont set in code. */
    QLineEdit#PageField {{
        background: {CONTROL_FILL};
        border: 1px solid {BORDER_STRONG};
        border-radius: {BUTTON_RADIUS}px;
        padding: 0px;
        color: {TEXT_PRIMARY};
        selection-background-color: {ACCENT_FILL};
        selection-color: #FFFFFF;
    }}
    QLineEdit#PageField:hover {{ border: 1px solid {ACCENT_BORDER}; }}
    QLineEdit#PageField:focus {{
        background: {CONTROL_FILL};
        border: 1px solid {ACCENT};
    }}
    QLineEdit#PageField:disabled {{
        background: {CONTROL_FILL_DISABLED};
        color: {TOOLBAR_ICON_DISABLED};
        border: 1px solid {BORDER_STRONG};
    }}
    QLabel#PageLabel, QLabel#PageTotal, QLabel#StatusLabel {{
        color: {TEXT_SECONDARY};
    }}

    /* --- Activity rail (the far-left vertical mode switcher) ---------- */
    QWidget#ActivityRail {{
        background: {RAIL_BG};
        border-right: 1px solid {CHROME_BORDER};
    }}
    QToolButton#RailButton {{
        background: transparent;
        border: none;
        border-radius: 11px;
        padding: 5px 0px;
        color: {TEXT_SECONDARY};
    }}
    QToolButton#RailButton:hover {{
        background: {WASH_HOVER};
        color: {TEXT_PRIMARY};
    }}
    QToolButton#RailButton:checked {{
        background: {ACCENT_ACTIVE_WASH};
        color: {ACCENT_TEXT};
    }}
    QToolButton#RailButton:disabled {{ color: {TOOLBAR_ICON_DISABLED}; }}
    QFrame#RailDivider {{
        background: {CHROME_BORDER};
        border: none;
        max-height: 1px;
        margin: 4px 18px;
    }}

    /* --- Find & Replace panel (REFLOW_SPEC §R5.1) -------------------- */
    QLineEdit#FindField, QLineEdit#ReplaceField {{
        background: {CONTROL_FILL};
        border: 1px solid {BORDER_STRONG};
        border-radius: {CONTROL_RADIUS}px;
        padding: 6px 9px;
        color: {TEXT_PRIMARY};
        selection-background-color: {ACCENT_FILL};
        selection-color: #FFFFFF;
    }}
    QLineEdit#FindField:focus, QLineEdit#ReplaceField:focus {{
        border: 1px solid {ACCENT};
    }}
    QLabel#FindPanelTitle {{ color: {PANEL_HEADER}; }}
    QToolButton#FindClose, QToolButton#FindPrev, QToolButton#FindNext {{
        border: none;
        border-radius: 6px;
        padding: 1px 7px;
        color: {TOOLBAR_ICON};
        font-size: 15px;
    }}
    QToolButton#FindClose:hover, QToolButton#FindPrev:hover,
    QToolButton#FindNext:hover {{ background: {ACCENT_HOVER}; }}
    QToolButton#FindPrev:disabled, QToolButton#FindNext:disabled {{
        color: {TOOLBAR_ICON_DISABLED};
    }}
    QPushButton#FindReplace, QPushButton#FindReplaceAll {{
        background: {CONTROL_FILL};
        border: 1px solid {BORDER_STRONG};
        border-radius: {CONTROL_RADIUS}px;
        padding: 6px 11px;
        color: {TEXT_PRIMARY};
    }}
    QPushButton#FindReplace:hover, QPushButton#FindReplaceAll:hover {{
        border: 1px solid {ACCENT_BORDER};
        background: {ACCENT_HOVER};
    }}
    QPushButton#FindReplace:disabled, QPushButton#FindReplaceAll:disabled {{
        background: {CONTROL_FILL_DISABLED};
        color: {TOOLBAR_ICON_DISABLED};
        border: 1px solid {BORDER_STRONG};
    }}

    /* --- Primary action button (a real accent CTA) ------------------ */
    QPushButton#PrimaryButton {{
        background: {ACCENT_FILL};
        border: none;
        border-radius: {CONTROL_RADIUS}px;
        padding: 8px 18px;
        color: #FFFFFF;
        font-weight: 600;
    }}
    QPushButton#PrimaryButton:hover {{ background: {ACCENT_DEEP}; }}
    QPushButton#PrimaryButton:pressed {{ background: {ACCENT_DEEP}; }}
    QPushButton#PrimaryButton:disabled {{
        background: {CONTROL_FILL_DISABLED};
        color: {TOOLBAR_ICON_DISABLED};
    }}

    /* --- Draw-Signature dialog (images & signatures §6) -------------- */
    QLineEdit#SignatureName {{
        background: {CONTROL_FILL};
        border: 1px solid {BORDER_STRONG};
        border-radius: {CONTROL_RADIUS}px;
        padding: 6px 9px;
        color: {TEXT_PRIMARY};
        selection-background-color: {ACCENT_FILL};
        selection-color: #FFFFFF;
    }}
    QLineEdit#SignatureName:focus {{ border: 1px solid {ACCENT}; }}
    QPushButton#SignatureClear, QPushButton#SignatureLoad {{
        background: {CONTROL_FILL};
        border: 1px solid {BORDER_STRONG};
        border-radius: {CONTROL_RADIUS}px;
        padding: 6px 11px;
        color: {TEXT_PRIMARY};
    }}
    QPushButton#SignatureClear:hover, QPushButton#SignatureLoad:hover {{
        border: 1px solid {ACCENT_BORDER};
        background: {ACCENT_HOVER};
    }}
    QSpinBox#SignaturePenWidth, QComboBox#SignatureColor {{
        background: {CONTROL_FILL};
        border: 1px solid {BORDER_STRONG};
        border-radius: {CONTROL_RADIUS}px;
        padding: 5px 9px;
        color: {TEXT_PRIMARY};
        min-height: 22px;
    }}
    QSpinBox#SignaturePenWidth:focus, QComboBox#SignatureColor:focus {{
        border: 1px solid {ACCENT};
    }}

    /* --- Generic dialogs / labels on the dark chrome ----------------- */
    QDialog {{ background: {PANEL_BG}; }}
    QDialog QLabel {{ color: {TEXT_PRIMARY}; background: transparent; }}

    /* --- Generic dark form controls (dialogs etc.) ------------------- */
    /* These class-level rules give every plain control the dark look so
       dialog labels/inputs are not dark-on-light or invisible; objectName'd
       controls (the Inspector / Find / Signature widgets above) still win on
       specificity. QSS-only, so it stays portable to Windows. */
    QCheckBox, QRadioButton {{ color: {TEXT_PRIMARY}; background: transparent; spacing: 7px; }}
    QCheckBox::indicator, QRadioButton::indicator {{ width: 16px; height: 16px; }}
    QCheckBox::indicator {{
        border: 1px solid {BORDER_STRONG}; border-radius: 4px; background: {CONTROL_FILL};
    }}
    QRadioButton::indicator {{
        border: 1px solid {BORDER_STRONG}; border-radius: 8px; background: {CONTROL_FILL};
    }}
    QCheckBox::indicator:checked, QRadioButton::indicator:checked {{
        background: {ACCENT_FILL}; border-color: {ACCENT_BORDER};
    }}
    QComboBox, QSpinBox, QDoubleSpinBox, QLineEdit, QPlainTextEdit, QTextEdit,
    QTimeEdit, QDateEdit {{
        background: {CONTROL_FILL}; color: {TEXT_PRIMARY};
        border: 1px solid {BORDER_STRONG}; border-radius: {CONTROL_RADIUS}px;
        padding: 4px 8px; selection-background-color: {ACCENT_FILL}; selection-color: #FFFFFF;
    }}
    QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus, QLineEdit:focus,
    QPlainTextEdit:focus, QTextEdit:focus {{ border: 1px solid {ACCENT}; }}
    QComboBox:disabled, QSpinBox:disabled, QDoubleSpinBox:disabled,
    QLineEdit:disabled {{ background: {CONTROL_FILL_DISABLED}; color: {TOOLBAR_ICON_DISABLED}; }}
    QComboBox::drop-down {{ border: none; width: 20px; }}
    /* A visible dropdown caret so a combo reads as a dropdown, not a text field
       (a CSS triangle -- no image asset needed). */
    QComboBox::down-arrow {{
        image: none; width: 0; height: 0;
        border-left: 4px solid transparent;
        border-right: 4px solid transparent;
        border-top: 5px solid {TEXT_SECONDARY};
        margin-right: 7px;
    }}
    QComboBox QAbstractItemView {{
        background: {CHROME_BG}; color: {TEXT_PRIMARY};
        border: 1px solid {BORDER_STRONG}; border-radius: 10px; padding: 4px; outline: none;
        selection-background-color: {ACCENT_FILL}; selection-color: #FFFFFF;
    }}
    QGroupBox {{
        color: {TEXT_PRIMARY}; border: 1px solid {BORDER_STRONG};
        border-radius: {CONTROL_RADIUS}px; margin-top: 10px; padding-top: 6px;
    }}
    QGroupBox::title {{
        subcontrol-origin: margin; subcontrol-position: top left;
        left: 11px; padding: 0 4px; color: {PANEL_HEADER};
    }}
    /* Plain dialog push buttons (OK / Cancel / Apply). #PrimaryButton +
       #FindReplace etc. keep their own look via objectName specificity. */
    QDialog QPushButton, QMessageBox QPushButton {{
        background: {CONTROL_FILL}; color: {TEXT_PRIMARY};
        border: 1px solid {BORDER_STRONG}; border-radius: {BUTTON_RADIUS}px;
        padding: 6px 16px; min-width: 64px;
    }}
    QDialog QPushButton:hover, QMessageBox QPushButton:hover {{
        border-color: {ACCENT_BORDER}; background: {ACCENT_HOVER};
    }}
    QDialog QPushButton:default, QMessageBox QPushButton:default {{
        background: {ACCENT_FILL}; border-color: {ACCENT_FILL}; color: #FFFFFF;
    }}
    QDialog QPushButton:disabled, QMessageBox QPushButton:disabled {{
        background: {CONTROL_FILL_DISABLED}; color: {TOOLBAR_ICON_DISABLED};
    }}

    /* --- Tooltips (dark, to match the chrome) ------------------------ */
    QToolTip {{
        background: {CHROME_BG}; color: {TEXT_PRIMARY};
        border: 1px solid {BORDER_STRONG}; border-radius: 6px; padding: 4px 8px;
    }}

    /* --- Status bar -------------------------------------------------- */
    QStatusBar#MainStatus {{
        background: {RAIL_BG};
        border-top: 1px solid {CHROME_BORDER};
        color: {TEXT_PRIMARY};
    }}
    QStatusBar#MainStatus::item {{ border: none; }}

    /* --- Menu bar ---------------------------------------------------- */
    /* The File/Edit/View... bar. Clay-styled for the in-window menu on Windows
       and Linux (where the bar lives in the window); on macOS the native top
       menu bar is used and this block is a no-op there. Hover and press match
       the QMenu dropdowns below: a solid accent fill with white text. */
    QMenuBar {{
        background: {CHROME_BG};
        border: none;
        border-bottom: 1px solid {CHROME_BORDER};
        padding: 2px 8px;
        color: {TEXT_PRIMARY};
        font-size: {UI_FONT_SIZE}px;
    }}
    QMenuBar::item {{
        background: transparent;
        padding: 5px 10px;
        border-radius: {BUTTON_RADIUS}px;
        color: {TEXT_PRIMARY};
    }}
    QMenuBar::item:selected {{
        background: {ACCENT_FILL};
        color: #FFFFFF;
    }}
    QMenuBar::item:pressed {{
        background: {ACCENT_PRESSED};
        color: #FFFFFF;
    }}

    /* --- Menus ------------------------------------------------------- */
    QMenu {{
        background: {CHROME_BG};
        border: 1px solid {BORDER_STRONG};
        border-radius: 11px;
        padding: 6px;
        color: {TEXT_PRIMARY};
    }}
    QMenu::item {{
        padding: 6px 24px 6px 15px;
        border-radius: 7px;
        color: {TEXT_PRIMARY};
    }}
    QMenu::item:selected {{
        background: {ACCENT_FILL};
        color: #FFFFFF;
    }}
    QMenu::item:disabled {{ color: {TOOLBAR_ICON_DISABLED}; }}
    QMenu::separator {{
        height: 1px;
        background: {DIVIDER};
        margin: 6px 11px;
    }}

    /* --- Document tab strip (PAGES_SPEC §5.2) ------------------------ */
    /* The full-width strip carries the bar background + bottom border so the
       tab bar itself can hug its tabs at the LEFT (no macOS centering). */
    QWidget#DocumentTabStrip {{
        background: {RAIL_BG};
        border-bottom: 1px solid {CHROME_BORDER};
    }}
    QTabBar#DocumentTabBar {{
        background: transparent;
        qproperty-drawBase: 0;
    }}
    /* No vertical margin: the tab fills the bar's height, so its rect == its
       drawn box and the painted close X / dirty dot centre exactly. Breathing
       room above the tabs comes from the STRIP's top inset instead. The wide
       side padding reserves room for the painted glyphs so the name never runs
       under them. */
    QTabBar#DocumentTabBar::tab {{
        background: transparent;
        border: 1px solid transparent;
        border-top-left-radius: 8px;
        border-top-right-radius: 8px;
        padding: 6px 22px 6px 22px;
        margin: 0px 2px 0px 2px;
        color: {TEXT_SECONDARY};
        min-width: 84px;
    }}
    QTabBar#DocumentTabBar::tab:hover {{ background: {WASH_HOVER}; }}
    QTabBar#DocumentTabBar::tab:selected {{
        background: {CANVAS_BG};
        border: 1px solid {BORDER_STRONG};
        border-bottom-color: {CANVAS_BG};
        color: {TEXT_PRIMARY};
    }}

    /* --- Inspector / Format dock (BUILD_SPEC §4.3) ------------------- */
    QWidget#Inspector {{
        background: {PANEL_BG};
    }}
    QWidget#Inspector QLabel {{
        color: {TEXT_SECONDARY};
        background: transparent;
    }}
    QLabel#InspectorSectionHeader {{
        color: {PANEL_HEADER};
        font-weight: 600;
    }}
    QLabel#InspectorEmptyHint {{ color: {TEXT_SECONDARY}; }}
    /* Fidelity status block: a soft tinted card so the product's core trust
       signal reads as a deliberate badge. The tier swatch + label are rich
       text set in code; the per-tier tinted background is set in code too. */
    QFrame#InspectorFidelity {{
        background: {CONTROL_FILL};
        border: 1px solid {BORDER_STRONG};
        border-radius: {CONTROL_RADIUS}px;
    }}
    /* Family combo + size spin: a recessed dark card on the panel. */
    QComboBox#InspectorFamily, QDoubleSpinBox#InspectorSize {{
        background: {CONTROL_FILL};
        border: 1px solid {BORDER_STRONG};
        border-radius: {CONTROL_RADIUS}px;
        padding: 6px 9px;
        color: {TEXT_PRIMARY};
        selection-background-color: {ACCENT_FILL};
        selection-color: #FFFFFF;
        min-height: 22px;
    }}
    QComboBox#InspectorFamily:focus, QDoubleSpinBox#InspectorSize:focus {{
        border: 1px solid {ACCENT};
    }}
    QComboBox#InspectorFamily:disabled, QDoubleSpinBox#InspectorSize:disabled {{
        background: {CONTROL_FILL_DISABLED};
        color: {TOOLBAR_ICON_DISABLED};
    }}
    QComboBox#InspectorFamily::drop-down {{ border: none; width: 22px; }}
    QComboBox#InspectorFamily QAbstractItemView {{
        background: {CHROME_BG};
        border: 1px solid {BORDER_STRONG};
        border-radius: 11px;
        padding: 5px;
        outline: none;
        color: {TEXT_PRIMARY};
        selection-background-color: {ACCENT_FILL};
        selection-color: #FFFFFF;
    }}
    QDoubleSpinBox#InspectorSize::up-button,
    QDoubleSpinBox#InspectorSize::down-button {{
        width: 16px; border: none; background: transparent;
    }}
    /* Segmented controls (Style: B I U S; Alignment: l c r justify) -- one
       rounded recessed container holding flat EQUAL segments; the active
       segment is an accent wash, not a per-button border (the widget .cs-seg). */
    QFrame#InspectorSeg {{
        background: {SEG_FILL};
        border: 1px solid {BORDER_STRONG};
        border-radius: 8px;
    }}
    QToolButton#SegButton {{
        background: transparent;
        border: none;
        border-radius: 6px;
        min-height: 28px;
        color: {TEXT_SECONDARY};
    }}
    QToolButton#SegButton:hover {{ background: {WASH_HOVER}; color: {TEXT_PRIMARY}; }}
    QToolButton#SegButton:checked {{
        background: {ACCENT_ACTIVE_WASH};
        color: {ACCENT_TEXT};
    }}
    /* Indeterminate: the selection mixes this style (some bold, some not). A
       dashed muted chip -- neither on nor off -- reads as "varies". */
    QToolButton#SegButton[mixed="true"] {{
        background: {WASH_HOVER};
        color: {TEXT_SECONDARY};
        border: 1px dashed {TEXT_SECONDARY};
    }}
    QToolButton#SegButton:disabled {{ color: {TOOLBAR_ICON_DISABLED}; }}

    /* Color chip: a swatch + hex readout sharing the size row. */
    QFrame#InspectorColorChip {{
        background: {CONTROL_FILL};
        border: 1px solid {BORDER_STRONG};
        border-radius: 8px;
    }}
    QFrame#InspectorColorChip:hover {{ border: 1px solid {ACCENT_BORDER}; }}
    QLabel#InspectorColorHex {{ color: {TEXT_SECONDARY}; background: transparent; }}

    /* Selection chip at the top of the Format panel (.cs-sel): a soft clay tile. */
    QFrame#InspectorSelChip {{
        background: {ACCENT_HOVER};
        border: 1px solid {ACCENT_BORDER};
        border-radius: 10px;
    }}
    QFrame#InspectorSelChip QLabel {{ background: transparent; }}
    QLabel#SelChipTile {{
        background: {ACCENT_SELECTION};
        border-radius: 7px;
        color: {ACCENT_TEXT};
    }}
    QLabel#SelChipTitle {{ color: {TEXT_PRIMARY}; }}

    /* Forms builder panel (three-mode authoring §UI): field rows match the
       Bookmark tree's row treatment; the row-action buttons are the app's
       secondary outline style. Everything else in the panel reuses the
       Inspector eyebrow / segmented-control / empty-hint QSS. */
    QWidget#FormsPanel {{ background: {PANEL_BG}; }}
    QLabel#FormsArmedHint {{ color: {ACCENT_TEXT}; background: transparent; }}
    QListWidget#FormsList {{
        background: transparent;
        border: none;
        outline: none;
        color: {TEXT_PRIMARY};
    }}
    QListWidget#FormsList::item {{
        border-radius: 6px;
        padding: 7px 8px;
        margin: 1px 0;
    }}
    QListWidget#FormsList::item:hover {{ background: {WASH_HOVER}; }}
    QListWidget#FormsList::item:selected {{
        background: {ACCENT_HOVER};
        color: {TEXT_PRIMARY};
    }}
    QPushButton#FormsRowBtn {{
        background: transparent;
        border: 1px solid {BORDER_STRONG};
        border-radius: {BUTTON_RADIUS}px;
        padding: 6px 12px;
        color: {TEXT_PRIMARY};
    }}
    QPushButton#FormsRowBtn:hover {{
        background: {WASH_HOVER};
        border: 1px solid {ACCENT_BORDER};
    }}
    QPushButton#FormsRowBtn:disabled {{
        color: {TOOLBAR_ICON_DISABLED};
        border: 1px solid {CHROME_BORDER};
    }}
    /* Field-type palette buttons: clean icon+label rows; the armed type reads
       as a light accent-tinted card (no loud accent text). */
    QToolButton#FieldTypeBtn {{
        background: transparent;
        border: 1px solid transparent;
        border-radius: {BUTTON_RADIUS}px;
        padding: 7px 10px;
        color: {TEXT_PRIMARY};
    }}
    QToolButton#FieldTypeBtn:hover {{ background: {WASH_HOVER}; }}
    QToolButton#FieldTypeBtn:checked {{
        background: {ACCENT_ACTIVE_WASH};
        border: 1px solid {ACCENT_BORDER};
        color: {TEXT_PRIMARY};
        font-weight: 600;
    }}
    QLabel#SelChipSub {{ color: {TEXT_SECONDARY}; }}
    /* Line-spacing spin (the only remaining paragraph-only control). */
    QDoubleSpinBox#InspectorLineSpacing {{
        background: {CONTROL_FILL};
        border: 1px solid {BORDER_STRONG};
        border-radius: {CONTROL_RADIUS}px;
        padding: 5px 9px;
        color: {TEXT_PRIMARY};
        min-height: 22px;
    }}
    QDoubleSpinBox#InspectorLineSpacing:focus {{ border: 1px solid {ACCENT}; }}
    QDoubleSpinBox#InspectorLineSpacing::up-button,
    QDoubleSpinBox#InspectorLineSpacing::down-button {{
        width: 16px; border: none; background: transparent;
    }}
    /* Annotation swatch/spin controls inherit the same recessed-card vocabulary. */
    QToolButton#AnnotStroke, QToolButton#AnnotFill {{
        background: {CONTROL_FILL};
        border: 1px solid {BORDER_STRONG};
        border-radius: {BUTTON_RADIUS}px;
        padding: 4px;
        min-width: 32px;
        min-height: 28px;
    }}
    QToolButton#AnnotStroke:hover, QToolButton#AnnotFill:hover {{
        background: {WASH_HOVER};
    }}
    QDoubleSpinBox#AnnotWidth, QDoubleSpinBox#AnnotOpacity {{
        background: {CONTROL_FILL};
        border: 1px solid {BORDER_STRONG};
        border-radius: {CONTROL_RADIUS}px;
        padding: 5px 9px;
        color: {TEXT_PRIMARY};
        min-height: 22px;
    }}
    QDoubleSpinBox#AnnotWidth:focus, QDoubleSpinBox#AnnotOpacity:focus {{
        border: 1px solid {ACCENT};
    }}

    /* --- Left contextual panel + right Pages panel ------------------- */
    QWidget#LeftPanel {{
        background: {PANEL_BG};
        border-right: 1px solid {CHROME_BORDER};
    }}
    QWidget#LeftPanelHeader {{
        background: {PANEL_BG};
        border-bottom: 1px solid {CHROME_BORDER};
    }}
    QLabel#LeftPanelTitle {{ color: {TEXT_PRIMARY}; }}
    QWidget#PagesHost {{
        background: {PANEL_BG};
        border-left: 1px solid {CHROME_BORDER};
    }}
    QWidget#PagesHeader {{
        background: {PANEL_BG};
        border-bottom: 1px solid {CHROME_BORDER};
    }}
    QLabel#PagesTitle {{ color: {TEXT_PRIMARY}; }}
    QLabel#PagesCount {{ color: {TEXT_SECONDARY}; }}
    QToolButton#PagesOrganize {{
        background: transparent; border: none; border-radius: 7px;
        padding: 4px; color: {TEXT_SECONDARY};
    }}
    QToolButton#PagesOrganize:hover {{ background: {WASH_HOVER}; color: {TEXT_PRIMARY}; }}
    QToolButton#PagesOrganize::menu-indicator {{ image: none; width: 0; }}

    /* --- Markup tools palette (rail Markup mode) --------------------- */
    QWidget#MarkupPanel {{ background: {PANEL_BG}; }}
    QWidget#MarkupPanel QLabel {{ background: transparent; }}
    QToolButton#MarkupToolButton {{
        background: transparent;
        border: 1px solid transparent;
        border-radius: {BUTTON_RADIUS}px;
        padding: 6px 14px 6px 8px;
        color: {TEXT_PRIMARY};
    }}
    QToolButton#MarkupToolButton:hover {{ background: {WASH_HOVER}; }}
    QToolButton#MarkupToolButton:checked {{
        background: {ACCENT_HOVER};
        border-color: {ACCENT_BORDER};
        color: {TEXT_PRIMARY};
    }}
    QTreeWidget#BookmarkTree {{
        background: transparent;
        border: none;
        outline: none;
        color: {TEXT_PRIMARY};
    }}
    QTreeWidget#BookmarkTree::item {{
        border-radius: 6px;
        padding: 4px 5px;
    }}
    QTreeWidget#BookmarkTree::item:hover {{ background: {WASH_HOVER}; }}
    QTreeWidget#BookmarkTree::item:selected {{
        background: {ACCENT_HOVER};
        color: {TEXT_PRIMARY};
    }}

    /* --- Scrollbars (slim, warm, unobtrusive) ----------------------- */
    QScrollBar:vertical {{
        background: transparent; width: 11px; margin: 2px;
    }}
    QScrollBar::handle:vertical {{
        background: {SCROLL_HANDLE};
        border-radius: 4px; min-height: 28px;
    }}
    QScrollBar::handle:vertical:hover {{ background: {SCROLL_HANDLE_HOVER}; }}
    QScrollBar:horizontal {{
        background: transparent; height: 11px; margin: 2px;
    }}
    QScrollBar::handle:horizontal {{
        background: {SCROLL_HANDLE};
        border-radius: 4px; min-width: 28px;
    }}
    QScrollBar::handle:horizontal:hover {{ background: {SCROLL_HANDLE_HOVER}; }}
    QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
    QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}
    """
