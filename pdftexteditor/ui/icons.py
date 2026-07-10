"""One consistent line-icon set, rendered from SVG via QtSvg.

Glyphs are real Lucide icons (lucide-static v0.460.0, ISC license),
baked as inline SVG path data; stroke 1.75 on the shared 24px grid.

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

# name -> (inner Lucide SVG markup, filled?). Every glyph is a real Lucide icon
# baked as bare inner geometry; styling (stroke 1.75, currentColor, round
# caps/joins) is applied centrally in _svg_bytes.
#
# CONVENTIONS (the registry of record; new icons pull the matching Lucide glyph
# so the set stays one family):
#   * 24px grid: viewBox "0 0 24 24" (Lucide's native grid).
#   * line icons: fill="none", stroke-width 1.75, round caps + joins -- entries
#     are BARE geometry (paths/rects/circles), no per-glyph styling.
#   * filled silhouettes (the bool flag True) are the exception; Lucide is all
#     stroke, so none are filled today.
#   * APPEND-ONLY: never rename or remove keys -- make_icon falls back to an
#     empty QIcon on unknown names and QSS/tests couple to existing ones.
_ICONS: dict[str, tuple[str, bool]] = {
    'open': ('<path d="m6 14 1.5-2.9A2 2 0 0 1 9.24 10H20a2 2 0 0 1 1.94 2.5l-1.54 6a2 2 0 0 1-1.95 1.5H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h3.9a2 2 0 0 1 1.69.9l.81 1.2a2 2 0 0 0 1.67.9H18a2 2 0 0 1 2 2v2"/>', False),
    'save': ('<path d="M15.2 3a2 2 0 0 1 1.4.6l3.8 3.8a2 2 0 0 1 .6 1.4V19a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2z"/> <path d="M17 21v-7a1 1 0 0 0-1-1H8a1 1 0 0 0-1 1v7"/> <path d="M7 3v4a1 1 0 0 0 1 1h7"/>', False),
    'save_as': ('<path d="M15.2 3a2 2 0 0 1 1.4.6l3.8 3.8a2 2 0 0 1 .6 1.4V19a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2z"/> <path d="M17 21v-7a1 1 0 0 0-1-1H8a1 1 0 0 0-1 1v7"/> <path d="M7 3v4a1 1 0 0 0 1 1h7"/>', False),
    'undo': ('<path d="M9 14 4 9l5-5"/> <path d="M4 9h10.5a5.5 5.5 0 0 1 5.5 5.5a5.5 5.5 0 0 1-5.5 5.5H11"/>', False),
    'redo': ('<path d="m15 14 5-5-5-5"/> <path d="M20 9H9.5A5.5 5.5 0 0 0 4 14.5A5.5 5.5 0 0 0 9.5 20H13"/>', False),
    'prev': ('<path d="m15 18-6-6 6-6"/>', False),
    'next': ('<path d="m9 18 6-6-6-6"/>', False),
    'chevron_down': ('<path d="m6 9 6 6 6-6"/>', False),
    'zoom_out': ('<circle cx="11" cy="11" r="8"/> <line x1="21" x2="16.65" y1="21" y2="16.65"/> <line x1="8" x2="14" y1="11" y2="11"/>', False),
    'zoom_in': ('<circle cx="11" cy="11" r="8"/> <line x1="21" x2="16.65" y1="21" y2="16.65"/> <line x1="11" x2="11" y1="8" y2="14"/> <line x1="8" x2="14" y1="11" y2="11"/>', False),
    'find': ('<circle cx="11" cy="11" r="8"/> <path d="m21 21-4.3-4.3"/>', False),
    'doc': ('<path d="M15 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7Z"/> <path d="M14 2v4a2 2 0 0 0 2 2h4"/> <path d="M10 9H8"/> <path d="M16 13H8"/> <path d="M16 17H8"/>', False),
    'text_edit': ('<polyline points="4 7 4 4 20 4 20 7"/> <line x1="9" x2="15" y1="20" y2="20"/> <line x1="12" x2="12" y1="4" y2="20"/>', False),
    'add_text': ('<rect width="18" height="18" x="3" y="3" rx="2"/> <path d="M8 12h8"/> <path d="M12 8v8"/>', False),
    'select': ('<path d="M4.037 4.688a.495.495 0 0 1 .651-.651l16 6.5a.5.5 0 0 1-.063.947l-6.124 1.58a2 2 0 0 0-1.438 1.435l-1.579 6.126a.5.5 0 0 1-.947.063z"/>', False),
    'select_text': ('<path d="M17 22h-1a4 4 0 0 1-4-4V6a4 4 0 0 1 4-4h1"/> <path d="M7 22h1a4 4 0 0 0 4-4v-1"/> <path d="M7 2h1a4 4 0 0 1 4 4v1"/>', False),
    'delete': ('<path d="M3 6h18"/> <path d="M19 6v14c0 1-1 2-2 2H7c-1 0-2-1-2-2V6"/> <path d="M8 6V4c0-1 1-2 2-2h4c1 0 2 1 2 2v2"/> <line x1="10" x2="10" y1="11" y2="17"/> <line x1="14" x2="14" y1="11" y2="17"/>', False),
    'close': ('<path d="M18 6 6 18"/> <path d="m6 6 12 12"/>', False),
    'align_left': ('<path d="M15 12H3"/> <path d="M17 18H3"/> <path d="M21 6H3"/>', False),
    'align_center': ('<path d="M17 12H7"/> <path d="M19 18H5"/> <path d="M21 6H3"/>', False),
    'align_right': ('<path d="M21 12H9"/> <path d="M21 18H7"/> <path d="M21 6H3"/>', False),
    'align_justify': ('<path d="M3 12h18"/> <path d="M3 18h18"/> <path d="M3 6h18"/>', False),
    'rotate': ('<path d="M21 12a9 9 0 1 1-9-9c2.52 0 4.93 1 6.74 2.74L21 8"/> <path d="M21 3v5h-5"/>', False),
    'highlight': ('<path d="m9 11-6 6v3h9l3-3"/> <path d="m22 12-4.6 4.6a2 2 0 0 1-2.8 0l-5.2-5.2a2 2 0 0 1 0-2.8L14 4"/>', False),
    'underline': ('<path d="M6 4v6a6 6 0 0 0 12 0V4"/> <line x1="4" x2="20" y1="20" y2="20"/>', False),
    'strikethrough': ('<path d="M16 4H9a3 3 0 0 0-2.83 4"/> <path d="M14 12a4 4 0 0 1 0 8H6"/> <line x1="4" x2="20" y1="12" y2="12"/>', False),
    'squiggly': ('<path d="m6 16 6-12 6 12"/> <path d="M8 12h8"/> <path d="m16 20 2 2 4-4"/>', False),
    'note': ('<path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/> <path d="M13 8H7"/> <path d="M17 12H7"/>', False),
    'ink': ('<path d="M15.707 21.293a1 1 0 0 1-1.414 0l-1.586-1.586a1 1 0 0 1 0-1.414l5.586-5.586a1 1 0 0 1 1.414 0l1.586 1.586a1 1 0 0 1 0 1.414z"/> <path d="m18 13-1.375-6.874a1 1 0 0 0-.746-.776L3.235 2.028a1 1 0 0 0-1.207 1.207L5.35 15.879a1 1 0 0 0 .776.746L13 18"/> <path d="m2.3 2.3 7.286 7.286"/> <circle cx="11" cy="11" r="2"/>', False),
    'shape': ('<path d="M8.3 10a.7.7 0 0 1-.626-1.079L11.4 3a.7.7 0 0 1 1.198-.043L16.3 8.9a.7.7 0 0 1-.572 1.1Z"/> <rect x="3" y="14" width="7" height="7" rx="1"/> <circle cx="17.5" cy="17.5" r="3.5"/>', False),
    'rect': ('<rect width="18" height="18" x="3" y="3" rx="2"/>', False),
    'ellipse': ('<circle cx="12" cy="12" r="10"/>', False),
    'line': ('<path d="M22 2 2 22"/>', False),
    'arrow': ('<path d="M7 7h10v10"/> <path d="M7 17 17 7"/>', False),
    'image': ('<rect width="18" height="18" x="3" y="3" rx="2" ry="2"/> <circle cx="9" cy="9" r="2"/> <path d="m21 15-3.086-3.086a2 2 0 0 0-2.828 0L6 21"/>', False),
    'signature': ('<path d="m21 17-2.156-1.868A.5.5 0 0 0 18 15.5v.5a1 1 0 0 1-1 1h-2a1 1 0 0 1-1-1c0-2.545-3.991-3.97-8.5-4a1 1 0 0 0 0 5c4.153 0 4.745-11.295 5.708-13.5a2.5 2.5 0 1 1 3.31 3.284"/> <path d="M3 21h18"/>', False),
    'comments': ('<path d="M14 9a2 2 0 0 1-2 2H6l-4 4V4a2 2 0 0 1 2-2h8a2 2 0 0 1 2 2z"/> <path d="M18 9h2a2 2 0 0 1 2 2v11l-4-4h-6a2 2 0 0 1-2-2v-1"/>', False),
    'bookmark': ('<path d="m19 21-7-4-7 4V5a2 2 0 0 1 2-2h10a2 2 0 0 1 2 2v16z"/>', False),
    'bookmark_add': ('<path d="m19 21-7-4-7 4V5a2 2 0 0 1 2-2h10a2 2 0 0 1 2 2v16z"/> <line x1="12" x2="12" y1="7" y2="13"/> <line x1="15" x2="9" y1="10" y2="10"/>', False),
    'info': ('<circle cx="12" cy="12" r="10"/> <path d="M12 16v-4"/> <path d="M12 8h.01"/>', False),
    'keyboard': ('<path d="M10 8h.01"/> <path d="M12 12h.01"/> <path d="M14 8h.01"/> <path d="M16 12h.01"/> <path d="M18 8h.01"/> <path d="M6 8h.01"/> <path d="M7 16h10"/> <path d="M8 12h.01"/> <rect width="20" height="16" x="2" y="4" rx="2"/>', False),
    'cut': ('<circle cx="6" cy="6" r="3"/> <path d="M8.12 8.12 12 12"/> <path d="M20 4 8.12 15.88"/> <circle cx="6" cy="18" r="3"/> <path d="M14.8 14.8 20 20"/>', False),
    'copy': ('<rect width="14" height="14" x="8" y="8" rx="2" ry="2"/> <path d="M4 16c-1.1 0-2-.9-2-2V4c0-1.1.9-2 2-2h10c1.1 0 2 .9 2 2"/>', False),
    'paste': ('<path d="M15 2H9a1 1 0 0 0-1 1v2c0 .6.4 1 1 1h6c.6 0 1-.4 1-1V3c0-.6-.4-1-1-1Z"/> <path d="M8 4H6a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2M16 4h2a2 2 0 0 1 2 2v2M11 14h10"/> <path d="m17 10 4 4-4 4"/>', False),
    'clear_recent': ('<path d="M11 12H3"/> <path d="M16 6H3"/> <path d="M16 18H3"/> <path d="m19 10-4 4"/> <path d="m15 10 4 4"/>', False),
    'share': ('<circle cx="18" cy="5" r="3"/> <circle cx="6" cy="12" r="3"/> <circle cx="18" cy="19" r="3"/> <line x1="8.59" x2="15.42" y1="13.51" y2="17.49"/> <line x1="15.41" x2="8.59" y1="6.51" y2="10.49"/>', False),
    'minus': ('<path d="M5 12h14"/>', False),
    'plus': ('<path d="M5 12h14"/> <path d="M12 5v14"/>', False),
    'grid': ('<rect width="18" height="18" x="3" y="3" rx="2"/> <path d="M3 12h18"/> <path d="M12 3v18"/>', False),
    'sun': ('<circle cx="12" cy="12" r="4"/> <path d="M12 2v2"/> <path d="M12 20v2"/> <path d="m4.93 4.93 1.41 1.41"/> <path d="m17.66 17.66 1.41 1.41"/> <path d="M2 12h2"/> <path d="M20 12h2"/> <path d="m6.34 17.66-1.41 1.41"/> <path d="m19.07 4.93-1.41 1.41"/>', False),
    'moon': ('<path d="M12 3a6 6 0 0 0 9 9 9 9 0 1 1-9-9Z"/>', False),
    'file_up': ('<path d="M15 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7Z"/> <path d="M14 2v4a2 2 0 0 0 2 2h4"/> <path d="M12 12v6"/> <path d="m15 15-3-3-3 3"/>', False),
    'clock': ('<circle cx="12" cy="12" r="10"/> <polyline points="12 6 12 12 16 14"/>', False),
    'home': ('<path d="M15 21v-8a1 1 0 0 0-1-1h-4a1 1 0 0 0-1 1v8"/> <path d="M3 10a2 2 0 0 1 .709-1.528l7-5.999a2 2 0 0 1 2.582 0l7 5.999A2 2 0 0 1 21 10v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>', False),
    'arrow_right': ('<path d="M5 12h14"/> <path d="m12 5 7 7-7 7"/>', False),
    'shield_check': ('<path d="M20 13c0 5-3.5 7.5-7.66 8.95a1 1 0 0 1-.67-.01C7.5 20.5 4 18 4 13V6a1 1 0 0 1 1-1c2 0 4.5-1.2 6.24-2.72a1.17 1.17 0 0 1 1.52 0C14.51 3.81 17 5 19 5a1 1 0 0 1 1 1z"/> <path d="m9 12 2 2 4-4"/>', False),
}

_VIEWBOX = "0 0 24 24"
_RENDER_PX = 64   # rendered large; Qt smooth-downscales to the icon slot (crisp on retina)


def _svg_bytes(name: str, color: str) -> QByteArray:
    inner, filled = _ICONS[name]
    if filled:
        attrs = f'fill="{color}" stroke="none"'
    else:
        attrs = (f'fill="none" stroke="{color}" stroke-width="1.75" '
                 'stroke-linecap="round" stroke-linejoin="round"')
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{_VIEWBOX}" '
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
