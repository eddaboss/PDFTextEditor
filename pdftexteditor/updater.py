"""Self-update via tufup.

Checks the build's channel server (appconfig URLs) and verifies EVERYTHING
against the bundled ``root.json`` trust anchor, so even though the app binary is
not OS-code-signed, an update cannot be forged by a compromised server.

All public helpers swallow network/trust errors and return falsy values: a
failed update check must never crash the editor.
"""
import os
import shutil
import sys
import tempfile
from pathlib import Path

from PySide6.QtCore import QObject, QStandardPaths, Signal

from . import appconfig


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


def check_for_updates(current_version: str):
    """Return the new TargetMeta if a newer signed release exists, else None.
    Never raises -- a network or trust failure simply means 'no update'."""
    try:
        return _make_client(current_version).check_for_updates()
    except Exception:
        return None


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
            if result is not None:
                self.available.emit(result)
        threading.Thread(target=work, name="update-check", daemon=True).start()
