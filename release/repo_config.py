"""Shared tufup repository configuration (release tooling).

Used by init_repo.py (one-time bootstrap) and publish.py (each release, run in
CI). The same keys sign BOTH channels' repos, so a single bundled root.json is
the trust anchor for the stable AND dev app builds; the channel only decides
which env's volume the build's updater points at.

Security posture: one signing key for all TUF roles, stored in CI secrets. This
matches the official tufup example. Production-grade TUF would separate roles and
keep the root key offline; even so, because the client verifies every update
against the bundled root.json, a compromised update *server* cannot forge an
update -- only compromised CI (which holds the key) could. That is still a large
improvement over an unsigned manifest.
"""
import os
import pathlib

from tufup.repo import DEFAULT_KEY_MAP

APP_NAME = "PDFTextEditor"                 # tufup id: NO whitespace
APP_VERSION_ATTR = "pdftexteditor.__version__"

KEY_NAME = "pdfte"
KEY_MAP = {role: [KEY_NAME] for role in DEFAULT_KEY_MAP.keys()}
ENCRYPTED_KEYS = []
THRESHOLDS = dict(root=1, targets=1, snapshot=1, timestamp=1)
EXPIRATION_DAYS = dict(root=365, targets=30, snapshot=30, timestamp=7)

# Working dirs (CI overrides PDFTE_RELEASE_DIR to an ephemeral path).
ROOT = pathlib.Path(os.environ.get(
    "PDFTE_RELEASE_DIR",
    str(pathlib.Path(__file__).resolve().parent / "_repo")))
REPO_DIR = ROOT / "repo"      # holds metadata/ + targets/ (+ the tufup config)
KEYS_DIR = ROOT / "keys"      # holds the private signing key
