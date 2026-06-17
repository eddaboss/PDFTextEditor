"""Self-update via tufup.

Checks the build's channel server (appconfig URLs) and verifies EVERYTHING
against the bundled ``root.json`` trust anchor, so even though the app binary is
not OS-code-signed, an update cannot be forged by a compromised server.

A failed check never crashes the editor, but it no longer pretends the app is
up to date: check_for_updates returns an ``("__error__", msg)`` sentinel on
failure (the Settings UI shows "couldn't check"), and it self-heals a stale or
rolled-back local metadata cache by retrying once from the bundled root.
"""
import logging
import os
import shutil
import sys
import tempfile
import threading
from pathlib import Path

from PySide6.QtCore import QObject, QStandardPaths, Signal

from . import appconfig

log = logging.getLogger(__name__)


def is_frozen() -> bool:
    return getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS")


def updates_supported() -> bool:
    """Self-update only makes sense in a real installed (frozen) build."""
    return is_frozen()


def _bundled_root_json() -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
    candidates = [
        base / "pdftexteditor" / "assets" / "root.json",
        Path(__file__).resolve().parent / "assets" / "root.json",
    ]
    for c in candidates:
        if c.exists():
            return c
    return candidates[-1]


def _cache_dirs() -> "tuple[Path, Path]":
    base = QStandardPaths.writableLocation(QStandardPaths.AppDataLocation)
    if not base:
        base = os.path.join(tempfile.gettempdir(), "pdftexteditor")
    root = Path(base) / "update_cache"
    meta, targ = root / "metadata", root / "targets"
    meta.mkdir(parents=True, exist_ok=True)
    targ.mkdir(parents=True, exist_ok=True)
    # Install the trust anchor on first run (tufup needs root.json present).
    dst = meta / "root.json"
    if not dst.exists():
        shutil.copy(_bundled_root_json(), dst)
    return meta, targ


def _reset_metadata_cache() -> None:
    """Wipe the cached TUF metadata so the next client re-bootstraps from the
    bundled root.json. Recovers a client whose cached metadata version sits AHEAD
    of the server's (e.g. after a channel was reset): TUF treats the server as a
    rollback and rejects every future update forever otherwise. The downloaded
    targets cache is left intact."""
    base = QStandardPaths.writableLocation(QStandardPaths.AppDataLocation)
    if not base:
        base = os.path.join(tempfile.gettempdir(), "pdftexteditor")
    shutil.rmtree(Path(base) / "update_cache" / "metadata", ignore_errors=True)


def _install_dir() -> Path:
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent  # source checkout (no real update)


def _make_client(current_version: str):
    from tufup.client import Client
    meta, targ = _cache_dirs()
    return Client(
        app_name=appconfig.TUFUP_APP_NAME,
        app_install_dir=_install_dir(),
        current_version=current_version,
        metadata_dir=meta,
        metadata_base_url=appconfig.METADATA_BASE_URL,
        target_dir=targ,
        target_base_url=appconfig.TARGET_BASE_URL,
    )


def _is_error(result) -> bool:
    """True if a check_for_updates() result is the failure sentinel."""
    return isinstance(result, tuple) and bool(result) and result[0] == "__error__"


def check_for_updates(current_version: str):
    """Return the new TargetMeta if a newer signed release exists, None if the
    app is already current, or ``("__error__", message)`` if the check itself
    FAILED (never silently reported as 'up to date'). Self-heals a stale or
    rolled-back local metadata cache: on any failure it wipes the cached metadata
    and retries once from the bundled root.json, so a client left ahead of the
    server (e.g. after a channel reset) recovers on its own instead of rejecting
    every future update as a rollback. Never raises."""
    try:
        return _make_client(current_version).check_for_updates()
    except Exception as first:
        log.warning("update check failed (%s); resetting metadata cache and retrying",
                    first)
        try:
            _reset_metadata_cache()
            return _make_client(current_version).check_for_updates()
        except Exception as second:
            log.warning("update check failed after cache reset: %s", second)
            return ("__error__", str(second))


def apply_update(current_version: str) -> bool:
    """Download + apply the latest update (patch-based when smaller). Returns
    True if an update was applied (the app then needs a restart)."""
    try:
        client = _make_client(current_version)
        if client.check_for_updates() is None:
            return False
        client.download_and_apply_update()
        return True
    except Exception:
        return False


class UpdateChecker(QObject):
    """Background, on-launch update check. Emits ``available`` (on the GUI
    thread) only when a newer signed release exists, so the app can show a
    one-click 'Update available' prompt without ever blocking startup."""

    available = Signal(object)

    def check(self, current_version: str) -> None:
        if not updates_supported():
            return

        def work():
            result = check_for_updates(current_version)
            if result is not None and not _is_error(result):
                self.available.emit(result)
        threading.Thread(target=work, name="update-check", daemon=True).start()
