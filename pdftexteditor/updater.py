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
import subprocess
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
    if not is_frozen():
        return Path(__file__).resolve().parent.parent  # source checkout (no real update)
    exe_dir = Path(sys.executable).resolve().parent
    # On macOS the frozen exe runs from <App>.app/Contents/MacOS. The update
    # archive holds the bundle's Contents/, so the install target is the .app
    # ITSELF (Contents/ merges into it). Returning the MacOS dir would make tufup
    # copy Contents/ INTO it -- a broken, nested bundle.
    if (sys.platform == "darwin" and exe_dir.name == "MacOS"
            and exe_dir.parent.name == "Contents"):
        return exe_dir.parent.parent
    return exe_dir


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


def _install_and_relaunch(src_dir, dst_dir, **kwargs) -> None:
    """tufup install callable: place the update, relaunch the app, then hard-exit
    THIS process. Replaces tufup's default install, which is broken here twice
    over: it relaunches with ``subprocess.Popen(sys.executable, shell=True)`` -- a
    shell cannot parse the macOS app path (spaces + parens) -- and it calls
    ``sys.exit(0)``, which from our Qt worker thread only kills the thread and
    leaves the old app frozen on "Downloading and installing…". We run off the GUI
    thread, so the process has to be torn down with ``os._exit``."""
    if sys.platform == "darwin":
        # The running .app can be overwritten in place; merge the new Contents/
        # into the bundle, then relaunch via `open` (argv list, so the spaces and
        # parens are safe; -n forces a fresh instance).
        shutil.copytree(src_dir, dst_dir, dirs_exist_ok=True, symlinks=True)
        subprocess.Popen(["/usr/bin/open", "-n", str(dst_dir)])
    else:
        # Windows: the running exe is locked, so tufup defers the swap to a script
        # that waits for THIS process to exit before replacing files and
        # relaunching. Its own sys.exit only kills our worker thread, hence the
        # os._exit below that actually lets that script proceed.
        from tufup.utils.platform_specific import install_update
        try:
            install_update(src_dir=src_dir, dst_dir=dst_dir, **kwargs)
        except SystemExit:
            pass
    os._exit(0)


def apply_update(current_version: str) -> bool:
    """Download + apply the latest update, then relaunch. On success this does NOT
    return -- _install_and_relaunch tears down the process and starts the updated
    build. Returns False only if there was nothing to install or it failed.

    skip_confirmation=True is REQUIRED: without it tufup calls input() for a
    terminal y/n prompt, which raises EOFError in a windowed (no-stdin) build and
    aborts the install."""
    try:
        client = _make_client(current_version)
        if client.check_for_updates() is None:
            return False
        client.download_and_apply_update(
            skip_confirmation=True, install=_install_and_relaunch)
        return True
    except Exception as e:
        log.warning("update install failed: %s", e)
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
