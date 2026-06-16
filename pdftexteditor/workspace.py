"""The Workspace: N open ``PDFDocument``s + an active index (PAGES_SPEC §2).

A pure-model container backing the document-tab UI. Qt-free: the tab CHROME is
the window's job; this only owns the list of open docs, which one is active, and
the open/close/switch/reorder lifecycle. Each tab carries its OWN edits + undo
(they live on the ``PDFDocument``), so switching tabs is just re-pointing the
view/sidebar/inspector at ``workspace.active``.

Pinned semantics (PAGES_SPEC §2):
  * ``open()`` de-dups by ``os.path.realpath`` — a second open of the same file
    switches to and returns the existing index (matches Preview / Acrobat).
  * ``close()`` calls ``doc.close()`` to release the fitz handle; the active
    index re-clamps to the right neighbor (else the new last tab), -1 when empty.
  * the Workspace does NOT own dirty-guard dialogs; the window asks
    ``workspace.document(idx).dirty`` before ``workspace.close(idx)``.
"""

from __future__ import annotations

import os

from .document import PDFDocument


class Workspace:
    """Holds the open documents and the active index for the tab UI."""

    def __init__(self) -> None:
        self._docs: list[PDFDocument] = []
        self._active: int = -1            # -1 when empty

    # --- queries ---------------------------------------------------------
    @property
    def count(self) -> int:
        return len(self._docs)

    @property
    def is_empty(self) -> bool:
        return not self._docs

    @property
    def active_index(self) -> int:
        return self._active

    @property
    def active(self) -> PDFDocument | None:
        if 0 <= self._active < len(self._docs):
            return self._docs[self._active]
        return None

    def document(self, idx: int) -> PDFDocument:
        return self._docs[idx]

    def documents(self) -> list[PDFDocument]:
        return list(self._docs)

    def index_of(self, doc: PDFDocument) -> int:
        for i, d in enumerate(self._docs):
            if d is doc:
                return i
        return -1

    def tab_name(self, idx: int) -> str:
        """The plain document name for a tab (no dirty marker). The tab chrome
        renders dirtiness as a styled dot, so the tab text stays clean."""
        return os.path.basename(self._docs[idx].path) or "Untitled"

    def is_dirty(self, idx: int) -> bool:
        """Whether the doc at ``idx`` has unsaved changes (drives the tab dot)."""
        return self._docs[idx].dirty

    def title(self, idx: int) -> str:
        """The labelled name: ``basename(path)`` plus a trailing ' •' when dirty.
        Used where a TEXT marker is appropriate (e.g. the combine-tabs submenu);
        the document tab strip instead uses ``tab_name`` + a styled dirty dot."""
        name = self.tab_name(idx)
        return f"{name} •" if self._docs[idx].dirty else name

    def any_dirty(self) -> bool:
        return any(d.dirty for d in self._docs)

    # --- mutation --------------------------------------------------------
    def open(self, path: str, password: str | None = None) -> int:
        """Open ``path`` into a new ``PDFDocument``, append it, make it active,
        and RETURN its index. If ``path`` is already open (same realpath), do NOT
        re-open: switch to and return the existing index (de-dup, PAGES_SPEC
        §2). ``password`` threads through to the model's open gate (doc-tools
        M4): an encrypted file with no/a wrong password raises
        ``PasswordRequired`` for the window's provider loop."""
        real = os.path.realpath(path)
        for i, d in enumerate(self._docs):
            if os.path.realpath(d.path) == real:
                self._active = i
                return i
        doc = PDFDocument(path, password)
        self._docs.append(doc)
        self._active = len(self._docs) - 1
        return self._active

    def add_document(self, doc: PDFDocument) -> int:
        """Append an already-constructed ``PDFDocument`` (used by
        split→open-result), make it active, and return its index."""
        self._docs.append(doc)
        self._active = len(self._docs) - 1
        return self._active

    def close(self, idx: int) -> int:
        """Close the doc at ``idx`` (releasing its fitz handle), remove it, and
        return the NEW active index (-1 when the workspace becomes empty).

        The active index re-clamps: closing the active tab activates the neighbor
        to its RIGHT (the tab that slides into its slot), else the new last
        tab."""
        if not 0 <= idx < len(self._docs):
            return self._active
        doc = self._docs.pop(idx)
        try:
            doc.close()
        except Exception:  # noqa: BLE001 - a double-close must not crash the UI
            pass
        if not self._docs:
            self._active = -1
            return self._active
        if idx < self._active:
            # A tab before the active one closed; the active doc shifted left.
            self._active -= 1
        elif idx == self._active:
            # The active tab closed: keep the slot (now the right neighbor),
            # clamped to the new last index.
            self._active = min(idx, len(self._docs) - 1)
        # idx > self._active: the active doc kept its index.
        return self._active

    def switch(self, idx: int) -> None:
        """Make ``idx`` active. Out-of-range is a no-op."""
        if 0 <= idx < len(self._docs):
            self._active = idx

    def move_tab(self, src: int, dst: int) -> None:
        """Reorder the open tabs (drag-reorder of the tab bar). Keeps ``active``
        pointing at the SAME document object."""
        n = len(self._docs)
        if not (0 <= src < n and 0 <= dst < n) or src == dst:
            return
        active_doc = self.active
        doc = self._docs.pop(src)
        self._docs.insert(dst, doc)
        if active_doc is not None:
            self._active = self.index_of(active_doc)

    def close_all(self) -> None:
        for d in self._docs:
            try:
                d.close()
            except Exception:  # noqa: BLE001
                pass
        self._docs = []
        self._active = -1
