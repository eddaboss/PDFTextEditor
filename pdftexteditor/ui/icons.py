"""One consistent line-icon set, rendered from SVG via QtSvg.

Replaces the old hand-drawn ``QPainter`` glyphs (and the text-character buttons
"I" / "A+") with a single coherent icon family: a 24px grid, 1.8px stroke,
round caps/joins. The toolbar and the left tool strip now read as a designed
set instead of a mix of crude glyphs and letters.

One entry point, ``make_icon(name, color)``, is a drop-in for the previous
factory (same signature + Normal/Disabled, plus an accent ``On`` state for the
active tool). Every call site is unchanged.
"""

from __future__ import annotations

from PySide6.QtCore import QByteArray, Qt
from PySide6.QtGui import QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer

from . import theme

# name -> (inner SVG markup, filled?). All glyphs share one stroke style; the
# pointer is the only filled one.
#
# CONVENTIONS (navigation M3 -- the registry of record for new icons; every
# workstream's additions must match so the set keeps reading as one family):
#   * 24px grid: viewBox "0 0 24 24", ink roughly inside the 3..21 box
#     (per-name tight viewBoxes only via _VIEWBOXES, see the nav chevrons).
#   * line icons: fill="none", stroke-width 1.95 (2.0 for _HEAVY names),
#     round caps + round joins -- set centrally in _svg_bytes, so entries are
#     BARE geometry (paths/rects/circles), no per-glyph styling.
#   * filled silhouettes (the bool flag True) are the exception, not the rule.
#   * APPEND-ONLY: never rename or remove keys -- make_icon falls back to an
#     empty QIcon on unknown names and QSS/tests couple to existing ones.
_ICONS: dict[str, tuple[str, bool]] = {
    "open": ('<path d="M3 7.5A1.5 1.5 0 0 1 4.5 6H9l2 2.2h8.5A1.5 1.5 0 0 1 21 '
             '9.7v8.8A1.5 1.5 0 0 1 19.5 20h-15A1.5 1.5 0 0 1 3 18.5Z"/>', False),
    "save": ('<path d="M6 4h10.5L20 7.5V19a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V5'
             'a1 1 0 0 1 1-1Z"/><path d="M8 4v5h7V4"/><path d="M8 20v-6h8v6"/>',
             False),
    "save_as": ('<path d="M5.5 5h9.5l3.5 3.5v10A1.5 1.5 0 0 1 17 20H7a1.5 1.5 0 '
                '0 1-1.5-1.5Z"/><path d="M8 5v4h6V5"/>'
                '<rect x="8" y="13" width="8" height="6" rx="1"/>', False),
    "undo": ('<path d="M9 7l-5 5 5 5"/><path d="M4 12h10a6 6 0 0 1 6 6"/>', False),
    "redo": ('<path d="M15 7l5 5-5 5"/><path d="M20 12H10a6 6 0 0 0-6 6"/>', False),
    # Small, clean page-nav chevrons (deliberately NOT the big slot-filling
    # glyph): a compact centered chevron with breathing room in the slot.
    "prev": ('<path d="M14 7.5l-5 4.5 5 4.5"/>', False),
    "next": ('<path d="M10 7.5l5 4.5-5 4.5"/>', False),
    # Down-chevron for the Save split CTA's caret half (Lucide-style).
    "chevron_down": ('<path d="M7 10l5 5 5-5"/>', False),
    "zoom_out": ('<circle cx="10.5" cy="10.5" r="6.5"/><path d="M15.5 15.5l5 5"/>'
                 '<path d="M7.5 10.5h6"/>', False),
    "zoom_in": ('<circle cx="10.5" cy="10.5" r="6.5"/><path d="M15.5 15.5l5 5"/>'
                '<path d="M10.5 7.5v6M7.5 10.5h6"/>', False),
    "find": ('<circle cx="10.5" cy="10.5" r="6.5"/><path d="M15.5 15.5l5 5"/>', False),
    "doc": ('<path d="M7 3.5h7l5 5v10A1.5 1.5 0 0 1 17.5 20h-11A1.5 1.5 0 0 1 5 '
            '18.5v-13A1.5 1.5 0 0 1 6.5 3.5Z"/><path d="M14 3.5V9h5"/>'
            '<path d="M9 13h6M9 16h6"/>', False),
    "text_edit": ('<path d="M5 8V6h12v2"/><path d="M11 6v12"/><path d="M8.5 18h5"/>',
                  False),
    "add_text": ('<path d="M4 8.5V6.5h10v2"/><path d="M9 6.5v11"/>'
                 '<path d="M6.5 17.5h5"/><path d="M18 12.5v6M15 15.5h6"/>', False),
    "select": ('<path d="M6 3.5l12 7-5.2 1.3 2.9 5.8-2.3 1.1-2.9-5.8L6 17.6Z"/>',
               True),
    # Text-select I-beam (ws2 M4): split serifs top + bottom around the stem,
    # the classic text-cursor glyph.
    "select_text": ('<path d="M9 4.5h2.5M12.5 4.5H15"/><path d="M12 5v14"/>'
                    '<path d="M9 19.5h2.5M12.5 19.5H15"/>'
                    '<path d="M5 12h4M15 12h4"/>', False),
    "delete": ('<path d="M5 7h14"/><path d="M9 7V5h6v2"/><path d="M7 7l1 12.5A1.5 '
               '1.5 0 0 0 9.5 21h5a1.5 1.5 0 0 0 1.5-1.5L17 7"/>'
               '<path d="M10 11v6M14 11v6"/>', False),
    "close": ('<path d="M7 7l10 10M17 7L7 17"/>', False),
    "align_left": ('<path d="M4 6h16M4 10.5h10M4 15h16M4 19.5h10"/>', False),
    "align_center": ('<path d="M4 6h16M7 10.5h10M4 15h16M7 19.5h10"/>', False),
    "align_right": ('<path d="M4 6h16M10 10.5h10M4 15h16M10 19.5h10"/>', False),
    "align_justify": ('<path d="M4 6h16M4 10.5h16M4 15h16M4 19.5h16"/>', False),
    "rotate": ('<path d="M20 11a8 8 0 1 0-2 6"/><path d="M20 5v6h-6"/>', False),
    # Markup / annotation tools (annotations & markup §5.2).
    "highlight": ('<path d="M9.5 14.5L15.5 6l3 3-7.2 7.3z"/>'
                  '<path d="M9.5 14.5l-1.2 3.6-2.6.9 1.5-4.2"/>'
                  '<path d="M4 21h16"/>', False),
    "underline": ('<path d="M7 4.5v6a5 5 0 0 0 10 0v-6"/>'
                  '<path d="M6 20h12"/>', False),
    "strikethrough": ('<path d="M16.5 7.5c-.6-1.5-2.3-2.5-4.5-2.5-2.6 0-4.5 '
                      '1.3-4.5 3.2 0 1.4 1 2.3 3 2.8"/>'
                      '<path d="M7.5 16.5c.6 1.5 2.3 2.5 4.5 2.5 2.6 0 '
                      '4.5-1.3 4.5-3.2 0-.6-.2-1.1-.5-1.5"/>'
                      '<path d="M5 12h14"/>', False),
    "squiggly": ('<path d="M6 5h12"/><path d="M12 5v9"/>'
                 '<path d="M4 19c1.3-2.4 2.7-2.4 4 0s2.7 2.4 4 0 2.7-2.4 4 0 '
                 '2.7 2.4 4 0"/>', False),
    "note": ('<path d="M4 5.5A1.5 1.5 0 0 1 5.5 4h13A1.5 1.5 0 0 1 20 5.5v9'
             'a1.5 1.5 0 0 1-1.5 1.5H12l-4 4v-4H5.5A1.5 1.5 0 0 1 4 14.5Z"/>'
             '<path d="M8 8.5h8M8 11.5h5"/>', False),
    "ink": ('<path d="M4 16c2.5-5 4.5-6.7 6-5.3s.3 5.3 2.3 5.3 3.7-4.7 '
            '7.7-5.7"/>', False),
    "shape": ('<rect x="4" y="4" width="11" height="11" rx="1"/>'
              '<circle cx="15.5" cy="15.5" r="4.5"/>', False),
    # The four shape tools each get a DISTINCT glyph so the Markup palette rows
    # are visually scannable (they no longer hide behind one Shapes button).
    "rect": ('<rect x="4" y="6.5" width="16" height="11" rx="1.5"/>', False),
    "ellipse": ('<ellipse cx="12" cy="12" rx="8.5" ry="6.5"/>', False),
    "line": ('<path d="M5 19 19 5"/>', False),
    "arrow": ('<path d="M5 19 17.5 6.5"/><path d="M11 6.5h7v7"/>', False),
    # Insert image (images & signatures §4): the classic picture frame --
    # sun + mountain landscape on the shared 24px line grid.
    "image": ('<rect x="3.5" y="5" width="17" height="14" rx="1.5"/>'
              '<circle cx="9" cy="10" r="1.6"/>'
              '<path d="M3.5 16.5l4.5-4 3.5 3 3.5-4 5.5 5.5"/>', False),
    # Signature (images & signatures §4 M2): a nib over the signing line --
    # the classic "sign here" glyph, same 24px line grid.
    "signature": ('<path d="M14.5 5l4.5 4.5L9.5 19 4 20l1-5.5z"/>'
                  '<path d="M12.5 7l4.5 4.5"/>'
                  '<path d="M14 20h7"/>', False),
    # Comments panel (annotations & markup §5.4): two stacked speech
    # bubbles, distinct from the single-bubble sticky-note tool glyph.
    "comments": ('<path d="M7.5 14H5.5A1.5 1.5 0 0 1 4 12.5v-7A1.5 1.5 0 0 1 '
                 '5.5 4H14a1.5 1.5 0 0 1 1.5 1.5V7"/>'
                 '<path d="M9 8.5A1.5 1.5 0 0 1 10.5 7h8A1.5 1.5 0 0 1 20 8.5'
                 'v6a1.5 1.5 0 0 1-1.5 1.5H14l-3.5 3.5V16h-.5A1.5 1.5 0 0 1 '
                 '9 14.5Z"/>', False),
    # Bookmarks panel (navigation M1): the classic ribbon, plus an add
    # variant (smaller ribbon + plus) for the panel's Add button.
    "bookmark": ('<path d="M7 4.5h10v15l-5-3.6-5 3.6Z"/>', False),
    "bookmark_add": ('<path d="M6.5 4.5h7.5v15l-3.75-2.7L6.5 19.5Z"/>'
                     '<path d="M18 4.5v5M15.5 7h5"/>', False),
    # Chrome glyphs (navigation M3): About, the shortcuts cheatsheet, the
    # Edit-menu clipboard trio, and the Clear Recents housekeeping link.
    "info": ('<circle cx="12" cy="12" r="8.5"/><path d="M12 11.2v5"/>'
             '<path d="M12 7.6v.3"/>', False),
    "keyboard": ('<rect x="3" y="6.5" width="18" height="11" rx="1.5"/>'
                 '<path d="M6.3 10h.2M9.4 10h.2M12.5 10h.2M15.6 10h.2'
                 'M18.7 10h.2M6.3 14h.2M18.7 14h.2"/>'
                 '<path d="M9 14h6"/>', False),
    "cut": ('<circle cx="6.5" cy="6.5" r="2.5"/>'
            '<circle cx="6.5" cy="17.5" r="2.5"/>'
            '<path d="M8.7 8 20 19M8.7 16 20 5M12.8 12.2l1.6 1.6"/>', False),
    "copy": ('<rect x="9" y="9" width="11" height="11" rx="1.5"/>'
             '<path d="M5.5 15H5a1.5 1.5 0 0 1-1.5-1.5v-9A1.5 1.5 0 0 1 5 3'
             'h9a1.5 1.5 0 0 1 1.5 1.5V5"/>', False),
    "paste": ('<path d="M8.5 5H6.5A1.5 1.5 0 0 0 5 6.5v13A1.5 1.5 0 0 0 '
              '6.5 21h11a1.5 1.5 0 0 0 1.5-1.5v-13A1.5 1.5 0 0 0 17.5 5'
              'h-2"/><rect x="8.5" y="3" width="7" height="4" rx="1"/>'
              '<path d="M9 12h6M9 15.5h4"/>', False),
    "clear_recent": ('<circle cx="10.5" cy="12.5" r="7"/>'
                     '<path d="M10.5 9v3.5l2.3 1.8"/>'
                     '<path d="M16.5 4.5 21 9M21 4.5l-4.5 4.5"/>', False),
    # Share (Charcoal redesign top bar): the classic three-node share graph.
    "share": ('<circle cx="6" cy="12" r="2.4"/><circle cx="17" cy="6" r="2.4"/>'
              '<circle cx="17" cy="18" r="2.4"/>'
              '<path d="m8.1 10.9 6.8-3.8M8.1 13.1l6.8 3.8"/>', False),
    # Plain minus / plus for the zoom stepper (the widget's .cs-zoom uses simple
    # strokes, not the busy magnifier glyphs the zoom ACTIONS carry).
    "minus": ('<path d="M5 12h14"/>', False),
    "plus": ('<path d="M12 5v14M5 12h14"/>', False),
    # 2x2 grid (Pages header "organize" button).
    "grid": ('<rect x="4" y="4" width="7" height="7" rx="1.4"/>'
             '<rect x="13" y="4" width="7" height="7" rx="1.4"/>'
             '<rect x="4" y="13" width="7" height="7" rx="1.4"/>'
             '<rect x="13" y="13" width="7" height="7" rx="1.4"/>', False),
}

_VIEWBOX = "0 0 24 24"
_RENDER_PX = 64   # rendered large; Qt smooth-downscales to the icon slot (crisp on retina)

# No glyphs use the heavier stroke (the page-nav chevrons used to, which made
# them read as thick/oversized; they are now the normal thin line weight).
_HEAVY: set[str] = set()

# Per-name TIGHT viewBoxes (cropped to ink so a glyph fills the slot). The
# page-nav chevrons deliberately use the DEFAULT 0 0 24 24 box now, so they
# render as a small chevron with breathing room rather than a big slot-filler.
_VIEWBOXES: dict[str, str] = {}


def _svg_bytes(name: str, color: str) -> QByteArray:
    inner, filled = _ICONS[name]
    viewbox = _VIEWBOXES.get(name, _VIEWBOX)
    if filled:
        attrs = f'fill="{color}" stroke="none"'
    else:
        width = 2.0 if name in _HEAVY else 1.95
        attrs = (f'fill="none" stroke="{color}" stroke-width="{width}" '
                 'stroke-linecap="round" stroke-linejoin="round"')
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{viewbox}" '
           f'{attrs}>{inner}</svg>')
    return QByteArray(svg.encode("utf-8"))


def _pixmap(name: str, color: str, px: int = _RENDER_PX,
            opacity: float = 1.0) -> QPixmap:
    renderer = QSvgRenderer(_svg_bytes(name, color))
    pm = QPixmap(px, px)
    pm.fill(Qt.transparent)
    painter = QPainter(pm)
    painter.setRenderHint(QPainter.Antialiasing, True)
    if opacity < 1.0:
        painter.setOpacity(opacity)
    renderer.render(painter)
    painter.end()
    return pm


def make_icon(name: str, color: str = theme.TOOLBAR_ICON) -> QIcon:
    """Named icon from the shared set, with Normal / Disabled / accent-On
    pixmaps. Unknown names return an empty icon (matches the old factory).

    Disabled is the SAME glyph at low opacity (a faded version of itself), not
    a different washed-out grey -- so a disabled nav chevron reads as 'dimmed',
    not 'broken', next to its enabled sibling."""
    if name not in _ICONS:
        return QIcon()
    icon = QIcon()
    icon.addPixmap(_pixmap(name, color), QIcon.Normal, QIcon.Off)
    icon.addPixmap(_pixmap(name, color, opacity=0.32), QIcon.Disabled)
    # The active tool (checked QToolButton) reads in accent.
    icon.addPixmap(_pixmap(name, theme.ACCENT), QIcon.Normal, QIcon.On)
    icon.addPixmap(_pixmap(name, theme.ACCENT), QIcon.Active, QIcon.On)
    return icon
