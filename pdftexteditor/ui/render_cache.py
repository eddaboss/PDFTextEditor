"""Baked-pixmap LRU cache for the continuous-scroll view (perf foundation M2).

The single biggest user-felt speedup: scroll re-entry onto an edited page, a
zoom revisit, and a tab switch back stop re-running the bake pipeline
(``render_with_edits`` costs 30-100+ ms on a dense edited page; a hit is a
dict lookup plus a cheap ``QPixmap.fromImage``).

Key contract (the soul-preserving part):

  key   = (page_index, round(zoom * dpr, 4), document.render_signature(page))
  value = the rendered, DPR-tagged ``QImage``

``render_signature`` (document.py) is the ONE cache-key registry: equal
signatures GUARANTEE ``render_with_edits`` produces the same pixels at a fixed
scale, so a hit can only ever re-display bytes the real pipeline produced for
exactly this staged state -- WYSIWYG cannot drift through this cache. Undo and
redo (both granularities) change the signature, structural ops bump its
generation, so stale hits are impossible by construction; eviction is therefore
purely a memory concern, never a correctness one.

The cache does NOT key on the document identity. PageView owns exactly one
document at a time and must ``clear()`` on ``set_document``/``clear_document``
(tab switches swap documents on one shared view; two pristine documents share
the signature ``(gen, (), ())``, so doc A's pixels would key-collide with
doc B's without the clear).

Capacity: 12 entries by default -- the lazy band keeps ~4-8 pages materialized,
plus headroom for one zoom revisit. An A4 page at zoom 2 x dpr 2 is ~25 MB, so
the worst case is ~300 MB transiently, in line with the app's measured RSS
today. Constructor arg for tuning.

This module is deliberately Qt-free (values are opaque to it), so the LRU
mechanics are unit-testable headless. It lives under ``pdftexteditor/`` so
PyInstaller's ``collect_submodules`` bundles it with no spec change.
"""

from __future__ import annotations

from collections import OrderedDict


class PageRenderCache:
    """A small LRU of baked page images keyed by
    ``(page_index, round(scale, 4), render_signature)``.

    ``scale`` is ``zoom * devicePixelRatio`` -- the only zoom-ish input the
    rendered pixels depend on. Rounding to 4 decimals collapses float jitter
    from fit-mode math while still separating any humanly distinguishable
    zoom levels."""

    def __init__(self, capacity: int = 12):
        self._capacity = max(1, int(capacity))
        # Insertion/recency-ordered: oldest first, most recently used last.
        self._entries: "OrderedDict[tuple, object]" = OrderedDict()

    # -- introspection ------------------------------------------------------
    @property
    def capacity(self) -> int:
        return self._capacity

    def __len__(self) -> int:
        return len(self._entries)

    @staticmethod
    def _key(page_index: int, scale: float, signature: tuple) -> tuple:
        return (page_index, round(scale, 4), signature)

    # -- the cache protocol --------------------------------------------------
    def get(self, page_index: int, scale: float, signature: tuple):
        """The cached image for this exact (page, scale, signature), or None.
        A hit refreshes the entry's recency."""
        key = self._key(page_index, scale, signature)
        image = self._entries.get(key)
        if image is not None:
            self._entries.move_to_end(key)
        return image

    def put(self, page_index: int, scale: float, signature: tuple,
            image) -> None:
        """Insert (or refresh) an entry, evicting the least recently used
        entries beyond capacity."""
        key = self._key(page_index, scale, signature)
        self._entries[key] = image
        self._entries.move_to_end(key)
        while len(self._entries) > self._capacity:
            self._entries.popitem(last=False)

    def purge_page(self, page_index: int) -> None:
        """Drop every entry for one page (all scales, all signatures).

        ``repaint_box`` calls this on the mutated page: correctness never
        needs it (the signature already changed, so the stale entries can
        never hit again), but it frees their memory immediately instead of
        waiting for LRU aging."""
        for key in [k for k in self._entries if k[0] == page_index]:
            del self._entries[key]

    def clear(self) -> None:
        """Drop everything. MANDATORY on document swap (see module docstring:
        the key does not encode the document)."""
        self._entries.clear()
