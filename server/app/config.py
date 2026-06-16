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
# When set, the human-facing site and the installer download sit behind a shared
# password (see the gate in main.py). Set it on the dev environment so the public
# cannot install the dev build; leave it empty in production so prod stays open.
SITE_PASSWORD = os.environ.get("PDFTE_SITE_PASSWORD", "")

# --- accounts: email, links, throttling -------------------------------------
# The site's own public origin (e.g. https://pdftexteditor.up.railway.app), used
# to build the verify / reset links we email. Set it per Railway environment so
# the dev site links to the dev host and prod to prod. When empty we fall back to
# the request's own base URL wherever a request is in hand.
PUBLIC_BASE_URL = os.environ.get("PDFTE_PUBLIC_URL", "").rstrip("/")

# Outbound email. Preferred provider is Resend (HTTP API): set RESEND_API_KEY and
# mail is sent through it. Otherwise, if SMTP_HOST is set the message goes over
# SMTP; with neither set, the body is logged to stdout so the whole account flow
# stays testable locally and in CI with no mail provider.
RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")
SMTP_HOST = os.environ.get("SMTP_HOST", "")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
# "starttls" (587, default), "ssl" (465), or "none" (unencrypted, dev relays).
SMTP_SECURITY = os.environ.get("SMTP_SECURITY", "starttls").lower()
# The From address must be on a domain verified with the mail provider. Resend is
# set up for pdf-for-free.com, so default the sender there.
EMAIL_FROM = os.environ.get("EMAIL_FROM", "no-reply@pdf-for-free.com")
EMAIL_FROM_NAME = os.environ.get("EMAIL_FROM_NAME", "PDF Text Editor")

# How long the emailed links stay valid.
VERIFY_TOKEN_TTL_HOURS = int(os.environ.get("VERIFY_TOKEN_TTL_HOURS", "48"))
RESET_TOKEN_TTL_HOURS = int(os.environ.get("RESET_TOKEN_TTL_HOURS", "2"))

# Brute-force throttling on the sensitive POST endpoints, counted per client IP
# over a rolling window. Generous enough never to bother a real person.
LOGIN_MAX_ATTEMPTS = int(os.environ.get("LOGIN_MAX_ATTEMPTS", "10"))
LOGIN_WINDOW_MINUTES = int(os.environ.get("LOGIN_WINDOW_MINUTES", "15"))
REGISTER_MAX_ATTEMPTS = int(os.environ.get("REGISTER_MAX_ATTEMPTS", "8"))
REGISTER_WINDOW_MINUTES = int(os.environ.get("REGISTER_WINDOW_MINUTES", "60"))
RESET_MAX_REQUESTS = int(os.environ.get("RESET_MAX_REQUESTS", "5"))
RESET_WINDOW_MINUTES = int(os.environ.get("RESET_WINDOW_MINUTES", "60"))

# Sign-in codes: minted only AFTER a person proves their password (or creates an
# account) at the download gate, then redeemed once by the desktop app to sign
# in without retyping the password. Short-lived by design, and the claim path is
# throttled so the code space cannot be swept.
ONBOARD_CODE_TTL_HOURS = int(os.environ.get("ONBOARD_CODE_TTL_HOURS", "1"))
ONBOARD_MAX_ATTEMPTS = int(os.environ.get("ONBOARD_MAX_ATTEMPTS", "20"))
ONBOARD_WINDOW_MINUTES = int(os.environ.get("ONBOARD_WINDOW_MINUTES", "10"))

# Extra browser origins allowed to call the API (comma-separated). Empty is the
# safe default: the account pages are served by this same app, so same-origin
# fetches need no CORS at all. Set this only if the landing page is ever hosted
# on a different origin than the API.
CORS_ORIGINS = [o.strip() for o in os.environ.get("CORS_ORIGINS", "").split(",")
                if o.strip()]


def ensure_dirs() -> None:
    for d in (UPDATES_DIR, METADATA_DIR, TARGETS_DIR, INSTALLERS_DIR):
        d.mkdir(parents=True, exist_ok=True)
