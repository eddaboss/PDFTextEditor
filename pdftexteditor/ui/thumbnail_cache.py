"""First-page thumbnails for the start-screen Recent gallery.

The home screen shows each recent document as a card carrying a real preview of
its first page. Rendering a page costs tens of milliseconds, so doing eight of
them up front would freeze the window on launch. Two things keep it smooth:

* a DISK CACHE keyed by ``realpath`` + size + mtime: once a file's first page is
  rendered it is saved as a PNG under the app's cache dir, so every later launch
  (and every reopen of the start screen) paints from disk instantly, and a file
  that changed on disk re-renders because its mtime moved.
* an INCREMENTAL, single-threaded loader: cache misses render one-per-event-loop
  -tick on the GUI thread via a 0ms ``QTimer``, so the loop fully repaints and
  stays responsive between renders. PyMuPDF shares one global context and is not
  safe to drive from several threads at once, so a background pool is deliberately
  avoided -- the cache makes the synchronous path a one-time cost per file.

Nothing here bakes staged edits: a thumbnail is the page as it sits on disk,
which is exactly what a "recent files" preview should show.
"""

from __future__ import annotations

import hashlib
import os

import fitz
from PySide6.QtCore import QObject, QStandardPaths, QTimer, Signal
from PySide6.QtGui import QPixmap


def cache_dir() -> str:
    """The per-user cache folder for rendered thumbnails (created on demand).

    Lives under ``QStandardPaths.CacheLocation`` so it is disposable: the OS may
    clear it and the next launch simply re-renders. Falls back to a temp dir if
    Qt hands back nothing (no ``QStandardPaths`` configured)."""
    base = QStandardPaths.writableLocation(QStandardPaths.CacheLocation)
    if not base:
        import tempfile
        base = os.path.join(tempfile.gettempdir(), "pdftexteditor")
    out = os.path.join(base, "thumbnails")
    try:
        os.makedirs(out, exist_ok=True)
    except OSError:
        pass
    return out


def _key(path: str) -> str:
    """A cache key that changes when the FILE changes: realpath + size + mtime.
    A moved/edited file misses the cache and re-renders; an untouched file hits
    it forever. Stat failures fall back to the path alone (still cacheable)."""
    real = os.path.realpath(path)
    try:
        st = os.stat(real)
        sig = f"{real}|{st.st_size}|{int(st.st_mtime)}"
    except OSError:
        sig = real
    return hashlib.sha1(sig.encode("utf-8", "replace")).hexdigest()


def cached_png(path: str) -> str:
    """The on-disk PNG location for ``path``'s current state (may not exist)."""
    return os.path.join(cache_dir(), f"{_key(path)}.png")


def render_to_cache(path: str, max_px: int = 600) -> str | None:
    """Render page 0 of ``path`` to a PNG in the cache and return its location,
    or ``None`` on any failure (an unreadable/encrypted file just shows no
    preview). The long edge is scaled to ``max_px`` so the card stays crisp on a
    Retina display. Runs on the GUI thread one file at a time (see module docs);
    a cache hit skips the work entirely."""
    out = cached_png(path)
    if os.path.isfile(out):
        return out
    doc = None
    try:
        doc = fitz.open(path)
        if doc.page_count < 1:
            return None
        page = doc[0]
        rect = page.rect
        long_edge = max(rect.width, rect.height, 1.0)
        scale = max_px / long_edge
        pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
        pix.save(out)
        return out
    except Exception:  # noqa: BLE001 - a bad/locked file just yields no preview
        return None
    finally:
        if doc is not None:
            try:
                doc.close()
            except Exception:  # noqa: BLE001
                pass


class ThumbnailLoader(QObject):
    """Hands a card its first-page ``QPixmap``, from cache instantly or rendered
    incrementally on a 0ms timer so the start screen never blocks.

    Connect ``ready(source_path, pixmap)`` and call ``request(path)`` per card.
    A cached file emits on the next tick; a miss queues behind any other misses
    and renders one per event-loop iteration. ``reset()`` drops the queue when
    the gallery is rebuilt so stale requests never paint over a new layout."""

    ready = Signal(str, QPixmap)

    def __init__(self, max_px: int = 600, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._max_px = max_px
        self._queue: list[str] = []
        self._timer = QTimer(self)
        self._timer.setInterval(0)   # fire when the event loop is otherwise idle
        self._timer.timeout.connect(self._step)

    def reset(self) -> None:
        """Forget every pending request (the gallery is being rebuilt)."""
        self._queue.clear()
        self._timer.stop()

    def request(self, path: str) -> None:
        """Ask for ``path``'s thumbnail. Cache hits are still delivered via the
        queue so the caller always receives ``ready`` asynchronously (uniform
        wiring, and the first paint is never stalled by a burst of loads)."""
        self._queue.append(path)
        if not self._timer.isActive():
            self._timer.start()

    def _step(self) -> None:
        if not self._queue:
            self._timer.stop()
            return
        path = self._queue.pop(0)
        png = cached_png(path)
        if not os.path.isfile(png):
            png = render_to_cache(path, self._max_px)
        if png:
            pm = QPixmap(png)
            if not pm.isNull():
                self.ready.emit(path, pm)
        if not self._queue:
            self._timer.stop()
