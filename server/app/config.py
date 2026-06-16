"""Backend configuration, driven entirely by environment variables.

This API carries ONLY version metadata, installer binaries, and user accounts.
It never touches a user's documents. One deploy per environment: the Railway
``dev`` environment serves the ``dev`` update channel, ``production`` serves
``stable`` -- so the environment IS the channel and there is nothing per-channel
to branch on inside the app.
"""
import os
import secrets
from pathlib import Path

# The update channel this deploy serves. Set per Railway environment.
CHANNEL = os.environ.get("PDFTE_CHANNEL", "stable")
ENV_NAME = os.environ.get("RAILWAY_ENVIRONMENT_NAME", "local")

# Persistent volume (Railway mounts it at /data). Falls back to a local dir so
# the app also runs on a laptop for verification.
DATA_DIR = Path(os.environ.get("PDFTE_DATA_DIR", "/data"))
UPDATES_DIR = DATA_DIR / "updates"          # tufup repo root: metadata/ + targets/
METADATA_DIR = UPDATES_DIR / "metadata"     # served at /metadata/
TARGETS_DIR = UPDATES_DIR / "targets"       # served at /targets/
INSTALLERS_DIR = DATA_DIR / "installers"    # first-install DMG + EXE, served at /download/
RELEASE_INFO = UPDATES_DIR / "release.json"  # our friendly manifest (version + notes + files)

DATABASE_URL = os.environ.get("DATABASE_URL", "")
# A stable secret for signing JWTs. MUST be set in the deploy; a random per-boot
# value would invalidate every token on restart.
JWT_SECRET = os.environ.get("JWT_SECRET") or secrets.token_urlsafe(32)
JWT_TTL_HOURS = int(os.environ.get("JWT_TTL_HOURS", "720"))  # 30 days
# Shared secret the release pipeline presents to POST /api/publish. Empty means
# publishing is disabled (safer default until the token is configured).
PUBLISH_TOKEN = os.environ.get("PUBLISH_TOKEN", "")


def ensure_dirs() -> None:
    for d in (UPDATES_DIR, METADATA_DIR, TARGETS_DIR, INSTALLERS_DIR):
        d.mkdir(parents=True, exist_ok=True)
