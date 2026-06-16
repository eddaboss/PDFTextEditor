"""Persistent signature library (images & signatures §6): named transparent
PNGs under the per-user app-data folder (``QStandardPaths.AppDataLocation``, the
same base as crash-recovery and thumbnails), listed newest-first for the
Signature menu's one-click placement.

Qt-FREE on purpose (stdlib only): the library is pure file plumbing, so the
test suite exercises CRUD against a tempdir with zero chrome -- the
``dir=`` parameter is the injection seam, and tests must NEVER touch the
real default folder. The folder itself is created lazily on the first
``save`` (a user who never saves a signature never grows dotfiles).

Writes are atomic (temp file + ``os.replace``, the document.py
``_atomic_write`` discipline) so a mid-write crash never corrupts an
existing signature.
"""

from __future__ import annotations

import os
import re
import tempfile

# Legacy location (pre cross-platform): a dotfolder in the home dir. Kept only
# so existing signatures can be migrated into the QStandardPaths folder below.
_LEGACY_DIR = os.path.expanduser(os.path.join("~", ".pdftexteditor", "signatures"))


def _default_dir() -> str:
    """The per-user signatures folder. Lives under
    ``QStandardPaths.AppDataLocation`` (same base as crash-recovery and
    thumbnails) so it is correct on macOS AND Windows instead of a home-dir
    dotfolder. Imported lazily so this module stays Qt-free for the
    tempdir-injected tests (which pass ``dir=`` and never reach here). Falls
    back to a temp dir when Qt has no location configured."""
    from PySide6.QtCore import QStandardPaths
    base = QStandardPaths.writableLocation(QStandardPaths.AppDataLocation)
    if not base:
        base = os.path.join(tempfile.gettempdir(), "pdftexteditor")
    return os.path.join(base, "signatures")

# Names are sanitized to this charset (§6): anything else collapses to "-",
# so a path-hostile name ("a/b: c?") can never escape the library folder.
_NAME_OK = re.compile(r"[^A-Za-z0-9 _-]+")


class SignatureLibrary:
    """CRUD over one folder of ``<name>.png`` files.

    ``save(name, png_bytes) -> path`` (sanitized, ``-2``/``-3``… deduped),
    ``list() -> [(name, path)]`` newest-first, ``load(path) -> bytes``,
    ``delete(path)``. No caching: the folder is tiny and re-reading keeps
    the menu honest if the user edits it externally (Manage Signatures…
    opens it in Finder).
    """

    def __init__(self, dir: str | None = None):
        # Only the default (real) library migrates legacy signatures; an
        # injected ``dir`` (the tests) must never touch the real folder.
        self._is_default = dir is None
        self._dir = dir or _default_dir()

    @property
    def dir(self) -> str:
        return self._dir

    def ensure_dir(self) -> str:
        """Create the library folder if missing (lazy: first save / Manage),
        migrating any signatures left in the legacy dotfolder on first use."""
        os.makedirs(self._dir, exist_ok=True)
        if self._is_default:
            self._migrate_legacy()
        return self._dir

    def _migrate_legacy(self) -> None:
        """One-time, non-destructive copy of signatures from the legacy
        ``~/.pdftexteditor/signatures`` into the new location. Skips once the new
        folder holds any ``.png`` (so it runs at most once) and always leaves the
        legacy copies in place."""
        if self._dir == _LEGACY_DIR or not os.path.isdir(_LEGACY_DIR):
            return
        try:
            if any(f.lower().endswith(".png") for f in os.listdir(self._dir)):
                return  # already populated; nothing to migrate
            import shutil
            for name in os.listdir(_LEGACY_DIR):
                if not name.lower().endswith(".png"):
                    continue
                src = os.path.join(_LEGACY_DIR, name)
                dst = os.path.join(self._dir, name)
                if os.path.isfile(src) and not os.path.exists(dst):
                    shutil.copy2(src, dst)
        except OSError:
            pass  # migration is a courtesy; never block the library

    @staticmethod
    def sanitize(name: str) -> str:
        """The §6 name rule: keep ``[A-Za-z0-9 _-]``, collapse runs of
        anything else to one ``-``, trim, fall back to ``Signature``."""
        clean = _NAME_OK.sub("-", name or "").strip(" -_") or "Signature"
        return clean

    def save(self, name: str, png_bytes: bytes) -> str:
        """Write ``png_bytes`` as ``<sanitized name>.png`` (deduped with a
        ``-2`` suffix and counting up) and return the path. Atomic."""
        self.ensure_dir()
        base = self.sanitize(name)
        path = os.path.join(self._dir, f"{base}.png")
        counter = 2
        while os.path.exists(path):
            path = os.path.join(self._dir, f"{base}-{counter}.png")
            counter += 1
        fd, tmp = tempfile.mkstemp(suffix=".png", dir=self._dir)
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(png_bytes)
            os.replace(tmp, path)
        except Exception:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise
        return path

    def list(self) -> list[tuple[str, str]]:
        """``[(display name, path)]`` of every ``.png``, newest-first (by
        mtime, ties broken by name for a stable menu)."""
        if not os.path.isdir(self._dir):
            return []
        entries = []
        for fn in os.listdir(self._dir):
            if not fn.lower().endswith(".png"):
                continue
            path = os.path.join(self._dir, fn)
            try:
                mtime = os.path.getmtime(path)
            except OSError:
                continue
            entries.append((mtime, os.path.splitext(fn)[0], path))
        entries.sort(key=lambda e: (-e[0], e[1]))
        return [(name, path) for _mtime, name, path in entries]

    def load(self, path: str) -> bytes:
        with open(path, "rb") as fh:
            return fh.read()

    def delete(self, path: str) -> None:
        os.unlink(path)
