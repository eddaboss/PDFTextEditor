"""Build-time environment selection: which update channel and backend this build
talks to.

The channel is BAKED at build time into ``_build_channel.py`` (written by the
build script / CI). A plain source checkout has no such file and defaults to
``dev`` so running from source never touches production. The backend URLs are
public (not secrets), so they live here directly.
"""
import os

try:
    from ._build_channel import CHANNEL as _BAKED  # written at build time
except Exception:
    _BAKED = None

CHANNEL = _BAKED or os.environ.get("PDFTE_BUILD_CHANNEL", "dev")
IS_DEV = CHANNEL != "stable"

_PROD_API = "https://pdftexteditor.up.railway.app"
_DEV_API = "https://pdftexteditor-dev.up.railway.app"

import sys

# Each platform has its own tufup repo under the channel server so the macOS and
# Windows archives never collide.
PLATFORM = "mac" if sys.platform == "darwin" else "win"

API_BASE_URL = _DEV_API if IS_DEV else _PROD_API
_UPDATES_BASE = f"{API_BASE_URL}/updates/{PLATFORM}"
METADATA_BASE_URL = _UPDATES_BASE + "/metadata/"
TARGET_BASE_URL = _UPDATES_BASE + "/targets/"

# tufup application id -- no whitespace, identical across channels (the channel
# is decided by which server the build points at, above).
TUFUP_APP_NAME = "PDFTextEditor"

DISPLAY_NAME = "PDF Text Editor (Dev)" if IS_DEV else "PDF Text Editor"
BUNDLE_ID = ("com.eddaboss.pdftexteditor.dev" if IS_DEV
             else "com.eddaboss.pdftexteditor")
