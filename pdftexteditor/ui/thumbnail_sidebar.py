"""The page thumbnail sidebar (PAGES_SPEC §5.1).

A ``QListWidget`` in IconMode showing one rendered thumbnail per page of the
ACTIVE document, with the current page highlighted, drag-to-reorder, a
right-click context menu (rotate / duplicate / insert blank / delete), and
click-to-navigate.

Pinned behavior (PAGES_SPEC §5.1):
  * the MODEL is the source of truth for reorder: the list NEVER reorders its own
    items. On an internal drag it computes (src, dst) in the destination-slot
    convention (§3.5), emits ``reorderRequested(src, dst)``, and lets the window
    apply ``move_page`` + ``refresh()`` so the list rebuilds from the new truth
    (no optimistic desync if the op fails).
  * ``set_current_page`` highlights a row WITHOUT emitting ``pageActivated`` so
    canvas→sidebar sync never loops back.
"""

from __future__ import annotations

from PySide6.QtCore import QRect, QRectF, QSize, Qt, Signal
from PySide6.QtGui import (QBrush, QColor, QFont, QIcon, QImage, QPainter, QPen,
                           QPixmap)
from PySide6.QtWidgets import (
    QListWidget,
    QListWidgetItem,
    QMenu,
    QStyle,
    QStyledItemDelegate,
)

from . import theme

_THUMB_MAX_PX = 460          # long-edge px for each rendered thumbnail. Rendered
                             # well above the ~132pt display width so the painter
                             # DOWNSCALES (crisp on Retina) instead of upscaling a
                             # 150px source (the old blur). The full-res pixmap is
                             # kept and drawn straight into the cell.
_ICON_W = 132                # page image width in DISPLAY (logical) px
_LABEL_H = 22                # reserved gutter height under the thumbnail for the
                             # page # (raised 18->22 so the number sits in a clear
                             # caption gutter, not overlapping the page image,
                             # incl. on the tinted selected card)
_CELL_PAD = 8                # item padding/margin reserve (QSS pad 4 + margin 2)


class _ThumbDelegate(QStyledItemDelegate):
    """Paints each page cell as the thumbnail with its number in a caption gutter
    below, and draws the selected/hover highlight as a ring that HUGS THE PAGE
    image -- not a box around the whole cell (which would float around the number
    and the cell padding, the "arbitrary box" look)."""

    def paint(self, painter, option, index):  # noqa: D102
        rect = option.rect
        icon = index.data(Qt.DecorationRole)
        text = index.data(Qt.DisplayRole) or ""
        selected = bool(option.state & QStyle.State_Selected)
        hovered = bool(option.state & QStyle.State_MouseOver)

        # Page (thumbnail) rect: full icon width, centered, with the label gutter
        # reserved at the bottom -- mirrors the cell sizing in refresh() so the
        # rect lands exactly on the page image (portrait OR landscape).
        page_w = _ICON_W
        page_h = rect.height() - _LABEL_H - 8
        page_x = rect.x() + (rect.width() - page_w) // 2
        page_y = rect.y() + 4
        page_rect = QRect(page_x, page_y, page_w, max(0, page_h))

        painter.save()
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setRenderHint(QPainter.SmoothPixmapTransform, True)

        # A soft shadow so each page sheet floats on the panel (the design's
        # --shadow-sm; the active page gets a deeper --shadow-md lift). Emulated
        # with a few stacked, fading rounded rects because a QGraphicsDropShadow
        # effect cannot run inside an item delegate.
        if page_rect.height() > 0:
            base = theme.color_sheet_shadow()
            strong = selected
            layers = 6 if strong else 4
            spread = 8.0 if strong else 5.0
            off_y = 4.0 if strong else 2.0
            peak = 34 if strong else 20
            painter.setPen(Qt.NoPen)
            for i in range(layers, 0, -1):
                t = i / layers
                alpha = int(peak * (1.0 - t) * (1.0 - t))
                if alpha <= 0:
                    continue
                c = QColor(base)
                c.setAlpha(alpha)
                grow = spread * t
                sr = QRectF(page_rect).adjusted(-grow, -grow + off_y,
                                                grow, grow + off_y)
                painter.setBrush(QBrush(c))
                painter.drawRoundedRect(sr, 5.0, 5.0)

        # The decoration is the FULL-RES page pixmap; drawing it into the smaller
        # logical page_rect lets the painter downscale at device resolution, so it
        # stays sharp on Retina. (A QIcon fallback covers any legacy item.)
        if isinstance(icon, QPixmap) and not icon.isNull():
            painter.drawPixmap(page_rect, icon)
        elif isinstance(icon, QIcon) and not icon.isNull():
            pm = icon.pixmap(page_rect.size())
            painter.drawPixmap(page_rect, pm)

        # The highlight ring, tight around the page image.
        if selected or hovered:
            ring = QRectF(page_rect).adjusted(-2.5, -2.5, 2.5, 2.5)
            if selected:
                painter.setPen(QPen(QColor(theme.ACCENT), 2))
            else:
                painter.setPen(QPen(QColor(theme.CHROME_BORDER), 1))
            painter.setBrush(Qt.NoBrush)
            painter.drawRoundedRect(ring, 4.0, 4.0)

        # Page number, in the caption gutter beneath the page.
        label_rect = QRect(rect.x(), rect.bottom() - _LABEL_H + 2,
                           rect.width(), _LABEL_H - 2)
        f = QFont(option.font)
        f.setBold(selected)
        painter.setFont(f)
        painter.setPen(QColor(theme.ACCENT_TEXT if selected
                              else theme.TEXT_SECONDARY))
        painter.drawText(label_rect, Qt.AlignHCenter | Qt.AlignTop, str(text))
        painter.restore()


class PageThumbnailSidebar(QListWidget):
    """One rendered thumbnail per page of the active document."""

    # Signals (the window connects these) ---------------------------------
    pageActivated = Signal(int)              # click/keyboard select -> navigate
    reorderRequested = Signal(int, int)      # (src, dst) DESTINATION-SLOT (§3.5)
    rotateRequested = Signal(int, int)       # (page_index, deg) deg in {90,-90,180}
    deleteRequested = Signal(int)            # page_index
    duplicateRequested = Signal(int)         # page_index
    insertBlankRequested = Signal(int)       # insert BEFORE this index (==count appends)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("PageThumbnailSidebar")
        self._doc = None
        self._suppress_activate = False

        self.setViewMode(QListWidget.IconMode)
        self.setMovement(QListWidget.Snap)
        self.setFlow(QListWidget.TopToBottom)
        self.setWrapping(False)
        self.setResizeMode(QListWidget.Adjust)
        self.setSpacing(8)
        self.setUniformItemSizes(False)
        self.setIconSize(QSize(_ICON_W, int(_ICON_W * 1.4)))
        self.setSelectionMode(QListWidget.SingleSelection)
        self.setDragDropMode(QListWidget.InternalMove)
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(self._on_context_menu)
        self.currentRowChanged.connect(self._on_row_changed)
        self.setStyleSheet(_SIDEBAR_QSS)
        # A delegate owns all item painting so the selected-page highlight is a
        # ring around the PAGE image, not the QSS box around the whole cell.
        self.setItemDelegate(_ThumbDelegate(self))

    # --- public API ------------------------------------------------------
    def set_document(self, doc) -> None:
        """Rebuild all thumbnails from ``doc`` (or clear when None)."""
        self._doc = doc
        self.refresh()

    def refresh(self) -> None:
        """Re-render every thumbnail from the current document (after any op)."""
        prev = self.currentRow()
        self._suppress_activate = True
        self.clear()
        if self._doc is not None:
            cell_w = self._cell_width()
            for i in range(self._doc.page_count):
                pixmap, pw, ph = self._thumb_icon(i)
                item = QListWidgetItem(str(i + 1))
                item.setData(Qt.DecorationRole, pixmap)
                item.setTextAlignment(Qt.AlignHCenter | Qt.AlignBottom)
                item.setData(Qt.UserRole, i)
                # Each cell spans the full panel width so the delegate can CENTER
                # the page image in it; the height tracks the thumbnail's REAL
                # aspect so a rotated/landscape page gets a SHORTER cell and the
                # selection ring hugs the page (PAGES_SPEC §5.1).
                if pw > 0 and ph > 0:
                    thumb_h = int(round(_ICON_W * ph / pw))
                    item.setSizeHint(QSize(cell_w, thumb_h + _LABEL_H + _CELL_PAD))
                self.addItem(item)
            if 0 <= prev < self.count():
                self.setCurrentRow(prev)
            elif self.count() > 0:
                self.setCurrentRow(min(max(prev, 0), self.count() - 1))
        self._suppress_activate = False

    def set_current_page(self, page_index: int) -> None:
        """Highlight the row for ``page_index`` WITHOUT emitting pageActivated."""
        if not 0 <= page_index < self.count():
            return
        self._suppress_activate = True
        self.setCurrentRow(page_index)
        self._suppress_activate = False

    def current_page(self) -> int:
        return self.currentRow()

    # --- rendering -------------------------------------------------------
    def _thumb_icon(self, page_index: int) -> tuple[QPixmap, int, int]:
        """Return ``(pixmap, page_w, page_h)`` for ``page_index``. The pixmap is
        FULL render resolution (not downscaled) so the delegate can draw it into
        the smaller display rect and stay sharp on Retina; the page dimensions let
        ``refresh`` size each cell to the thumbnail's true aspect."""
        try:
            pix = self._doc.render_thumbnail(page_index, _THUMB_MAX_PX)
        except Exception:  # noqa: BLE001 - a render hiccup must not crash the UI
            return QPixmap(), 0, 0
        image = QImage(pix.samples, pix.width, pix.height, pix.stride,
                       QImage.Format_RGB888).copy()
        return QPixmap.fromImage(image), pix.width, pix.height

    # --- layout ----------------------------------------------------------
    def _cell_width(self) -> int:
        """Cell width = the full usable panel width, so the delegate centers the
        page image in it (no left-anchored thumbnail floating in empty space)."""
        vw = self.viewport().width()
        return max(_ICON_W + _CELL_PAD, vw - 2 * self.spacing() - 2)

    def resizeEvent(self, event) -> None:  # noqa: D102
        super().resizeEvent(event)
        cell_w = self._cell_width()
        for i in range(self.count()):
            item = self.item(i)
            sh = item.sizeHint()
            if sh.width() != cell_w:
                item.setSizeHint(QSize(cell_w, sh.height()))

    # --- navigation ------------------------------------------------------
    def _on_row_changed(self, row: int) -> None:
        if self._suppress_activate or row < 0:
            return
        self.pageActivated.emit(row)

    # --- drag-to-reorder (model is the source of truth) ------------------
    def dropEvent(self, event) -> None:
        """Intercept an internal move: compute (src, dst) in destination-slot
        convention and emit ``reorderRequested`` WITHOUT letting the list reorder
        its own items. The window applies ``move_page`` + ``refresh()``."""
        src = self.currentRow()
        if src < 0:
            event.ignore()
            return
        dst = self._drop_row(event)
        event.setDropAction(Qt.IgnoreAction)
        event.accept()
        if dst is None or dst == src:
            return
        self.reorderRequested.emit(src, dst)

    def _drop_row(self, event) -> int | None:
        """The destination-slot index for a drop, in the §3.5 convention
        (``seq.pop(src); seq.insert(dst, x)``). Qt's drop indicator gives the
        insertion point in the ORIGINAL list; with the dragged row removed first,
        a forward drag shifts the target down by one."""
        pos = event.position().toPoint()
        item = self.itemAt(pos)
        if item is None:
            target = self.count() - 1
        else:
            target = self.row(item)
            rect = self.visualItemRect(item)
            # Dropped in the lower half of an item -> after it.
            if pos.y() > rect.center().y():
                target += 1
        src = self.currentRow()
        n = self.count()
        # Convert the original-list insertion point to a destination slot.
        if target > src:
            dst = target - 1
        else:
            dst = target
        return max(0, min(dst, n - 1))

    # --- context menu ----------------------------------------------------
    def _on_context_menu(self, pos) -> None:
        item = self.itemAt(pos)
        if item is None:
            return
        row = self.row(item)
        menu = QMenu(self)
        act_rot_r = menu.addAction("Rotate Right 90°")
        act_rot_l = menu.addAction("Rotate Left 90°")
        act_rot_180 = menu.addAction("Rotate 180°")
        menu.addSeparator()
        act_dup = menu.addAction("Duplicate Page")
        act_ins_before = menu.addAction("Insert Blank Before")
        act_ins_after = menu.addAction("Insert Blank After")
        menu.addSeparator()
        act_del = menu.addAction("Delete Page")
        act_del.setEnabled(self.count() > 1)

        chosen = menu.exec(self.viewport().mapToGlobal(pos))
        if chosen is None:
            return
        if chosen is act_rot_r:
            self.rotateRequested.emit(row, 90)
        elif chosen is act_rot_l:
            self.rotateRequested.emit(row, -90)
        elif chosen is act_rot_180:
            self.rotateRequested.emit(row, 180)
        elif chosen is act_dup:
            self.duplicateRequested.emit(row)
        elif chosen is act_ins_before:
            self.insertBlankRequested.emit(row)
        elif chosen is act_ins_after:
            self.insertBlankRequested.emit(row + 1)
        elif chosen is act_del:
            self.deleteRequested.emit(row)


_SIDEBAR_QSS = f"""
QListWidget#PageThumbnailSidebar {{
    background: {theme.CHROME_BG};
    /* The LEFT divider that separates the thumbnails from the center canvas
       lives on the PagesHost container (so it is continuous past the header
       band); the list itself draws no border. The pane is on the RIGHT, so a
       border-right would draw against the window frame and leave the canvas side
       undivided -- the bug this fixes. */
    border: none;
    padding: 6px;
    color: {theme.TEXT_SECONDARY};
}}
/* Items (thumbnail + number + selection ring) are painted by _ThumbDelegate so
   the active-page highlight hugs the PAGE image rather than boxing the whole
   cell. The list therefore declares no ::item border/background here -- a QSS
   ::item rule would box the full cell again and fight the delegate. */
"""
