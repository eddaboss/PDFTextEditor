"""On-demand OCR component pack (OCR_SPEC: optional, separately-versioned).

The cross-platform RapidOCR engine and its onnxruntime runtime are large (the
bulk of the installer), so they are NOT bundled in the base app. They are
delivered as a per-platform zip, downloaded once into the per-user app-data dir
on first use, then added to ``sys.path`` so ``ocr.engine`` can import them.
Apple Vision (macOS) needs none of this, so a Mac user who never picks RapidOCR
never downloads anything.

The pack is versioned independently of the app (``PACK_VERSION``); the release
pipeline names the hosted zip with it, so the app only ever downloads a pack it
was built to use.
"""
from __future__ import annotations

import importlib.util
import shutil
import sys
import urllib.request
import zipfile
from pathlib import Path

from .. import appconfig

# Bump when the pack contents change (the RapidOCR / onnxruntime versions in
# requirements-ocr.txt). The release pipeline reads this to name the zip, so the
# app and the hosted pack stay in lockstep.
PACK_VERSION = "1"          # rapidocr-onnxruntime 1.2.3 + onnxruntime 1.26.0

# An importable top-level package the pack provides; its presence means the pack
# extracted cleanly.
_MARKER = "rapidocr_onnxruntime"


def pack_root() -> Path:
    """The parent dir holding all installed pack versions, under the per-user
    app-data location (same base as crash-recovery, signatures, the updater)."""
    from PySide6.QtCore import QStandardPaths
    base = QStandardPaths.writableLocation(QStandardPaths.AppDataLocation)
    return Path(base) / "ocr_pack"


def pack_dir() -> Path:
    """The dir holding THIS platform + version's packages."""
    return pack_root() / f"{appconfig.PLATFORM}-{PACK_VERSION}"


def is_downloaded() -> bool:
    """Whether this version's pack has been downloaded into the data dir."""
    d = pack_dir()
    return d.is_dir() and (d / _MARKER).exists()


def is_available() -> bool:
    """Whether the OCR stack can be imported right now: true if the pack was
    downloaded (on sys.path) OR the deps are bundled in the base build (the
    transition state, before the build is slimmed). Checks importability rather
    than just the pack dir, so it is correct either way."""
    return importlib.util.find_spec(_MARKER) is not None


def ensure_on_path() -> bool:
    """Put a downloaded pack on ``sys.path`` (idempotent), then report whether the
    OCR stack is importable. Safe to call at import and before every OCR run."""
    d = pack_dir()
    if d.is_dir():
        p = str(d)
        if p not in sys.path:
            sys.path.insert(0, p)
        importlib.invalidate_caches()
    return is_available()


def download_url() -> str:
    return (f"{appconfig.API_BASE_URL}/download/"
            f"ocr-pack-{appconfig.PLATFORM}-{PACK_VERSION}.zip")


def download_and_install(progress=None) -> None:
    """Download and unpack the OCR pack for this platform/version. ``progress``,
    if given, is called as ``progress(done_bytes, total_bytes)`` (total may be 0
    when the server omits Content-Length). Replaces any partial install
    atomically; raises on any failure (the caller reports it)."""
    root = pack_root()
    root.mkdir(parents=True, exist_ok=True)
    tag = f"{appconfig.PLATFORM}-{PACK_VERSION}"
    tmp_zip = root / f".{tag}.part"
    staging = root / f".{tag}.unpack"
    dest = pack_dir()

    try:
        with urllib.request.urlopen(download_url(), timeout=60) as resp:
            total = int(resp.headers.get("Content-Length", 0) or 0)
            done = 0
            with open(tmp_zip, "wb") as f:
                while True:
                    chunk = resp.read(262144)
                    if not chunk:
                        break
                    f.write(chunk)
                    done += len(chunk)
                    if progress:
                        progress(done, total)

        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        with zipfile.ZipFile(tmp_zip, "r") as z:
            z.extractall(staging)

        # Swap the freshly-unpacked dir into place.
        if dest.exists():
            shutil.rmtree(dest, ignore_errors=True)
        staging.replace(dest)
    finally:
        if tmp_zip.exists():
            tmp_zip.unlink()
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)

    if not ensure_on_path():
        # Extracted but the marker is missing: a bad/empty pack. Clear it so a
        # retry starts clean rather than looking half-installed.
        shutil.rmtree(dest, ignore_errors=True)
        raise RuntimeError("OCR pack downloaded but did not unpack correctly.")
