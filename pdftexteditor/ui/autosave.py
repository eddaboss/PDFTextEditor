"""Crash recovery of unsaved edits.

The model stages edits in RAM and only writes to disk on an explicit Save, so a
crash, force-quit, or OS reboot used to lose everything edited since the last
save. This module keeps a disposable RECOVERY COPY of each dirty document so the
next launch can offer it back.

How it survives only a crash, never a clean quit:

* While a document is dirty, the window periodically writes a full baked copy of
  it here (``write_recovery``), keyed by the source file's path.
* A successful Save or a discarded change clears that document's copy
  (``clear_recovery``); a clean app quit clears the whole folder
  (``clear_all``).
* So at the NEXT launch, any copy still sitting here means the app went down
  WITHOUT cleaning up -- i.e. a crash -- and ``scan_recoveries`` surfaces it for
  the recover prompt.

Copies live under ``QStandardPaths.AppDataLocation`` (the proper per-user app
data dir, disposable). Each copy is a ``<key>.pdf`` plus a ``<key>.json`` sidecar
recording the original source path, its mtime, and when the copy was written.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile

from PySide6.QtCore import QStandardPaths


def recovery_dir() -> str:
    """The per-user folder holding recovery copies (created on demand)."""
    base = QStandardPaths.writableLocation(QStandardPaths.AppDataLocation)
    if not base:
        base = os.path.join(tempfile.gettempdir(), "pdftexteditor")
    out = os.path.join(base, "recovery")
    try:
        os.makedirs(out, exist_ok=True)
    except OSError:
        pass
    return out


def _key(source: str) -> str:
    """A stable key for a source path, so reopening the same file maps to (and
    overwrites) the same recovery slot rather than piling up copies."""
    real = os.path.realpath(source)
    return hashlib.sha1(real.encode("utf-8", "replace")).hexdigest()


def _paths(source: str) -> tuple[str, str]:
    k = _key(source)
    d = recovery_dir()
    return os.path.join(d, k + ".pdf"), os.path.join(d, k + ".json")


def write_recovery(doc, source: str, saved_at: float) -> None:
    """Write a full baked copy of ``doc`` (every staged edit applied) plus its
    metadata sidecar. ``doc.save_as`` already writes atomically, so a crash
    mid-autosave never corrupts a prior recovery copy."""
    pdf_path, meta_path = _paths(source)
    doc.save_as(pdf_path)
    try:
        mtime = os.path.getmtime(source)
    except OSError:
        mtime = 0.0
    info = {
        "source": os.path.abspath(source),
        "name": os.path.basename(source),
        "source_mtime": mtime,
        "saved_at": saved_at,
    }
    tmp = meta_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(info, fh)
    os.replace(tmp, meta_path)


def clear_recovery(source: str) -> None:
    """Drop the recovery copy for ``source`` (it was saved or discarded)."""
    for p in _paths(source):
        try:
            if os.path.isfile(p):
                os.remove(p)
        except OSError:
            pass


def clear_all() -> None:
    """Drop every recovery copy (a clean quit leaves nothing to recover)."""
    d = recovery_dir()
    try:
        names = os.listdir(d)
    except OSError:
        return
    for n in names:
        try:
            os.remove(os.path.join(d, n))
        except OSError:
            pass


def scan_recoveries() -> list:
    """Every recovery copy currently on disk, newest first. Each entry is a
    dict with ``recovery_pdf``, ``source``, ``name``, ``source_mtime``,
    ``saved_at``, and ``source_exists``. A surviving copy means the last run did
    not clean up (a crash)."""
    d = recovery_dir()
    try:
        names = os.listdir(d)
    except OSError:
        return []
    out: list = []
    for n in names:
        if not n.endswith(".json"):
            continue
        meta_path = os.path.join(d, n)
        pdf_path = meta_path[:-5] + ".pdf"
        if not os.path.isfile(pdf_path):
            continue
        try:
            with open(meta_path, encoding="utf-8") as fh:
                info = json.load(fh)
        except Exception:  # noqa: BLE001 - a corrupt sidecar is just skipped
            continue
        src = info.get("source")
        if not src:
            continue
        out.append({
            "recovery_pdf": pdf_path,
            "source": src,
            "name": info.get("name") or os.path.basename(src),
            "source_mtime": info.get("source_mtime", 0.0),
            "saved_at": info.get("saved_at", 0.0),
            "source_exists": os.path.isfile(src),
        })
    out.sort(key=lambda e: e["saved_at"], reverse=True)
    return out
