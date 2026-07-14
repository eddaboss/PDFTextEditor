"""PDF Text Editor backend.

One small, public API per environment. Responsibilities:

  * Serve the tufup update repository (``/metadata/`` + ``/targets/``) that the
    desktop app's self-updater reads.
  * Serve the first-install installers (``/download/...``) and a download page.
  * User accounts (register / login / me) -- the foundation only. No billing,
    no cloud storage, no document data ever touches this service.
  * A token-auth publish flow so the release pipeline can push a new signed
    release onto the persistent volume.
"""
import datetime
import hashlib
import hmac
import html
import io
import json
import logging
import os
import tarfile
import tempfile
from pathlib import Path

from fastapi import (Depends, FastAPI, File, Header, HTTPException, Request,
                     UploadFile)
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse,
                               RedirectResponse, Response)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from . import accounts, emailer, metrics, models, r2, security, track
from .account_models import GateCode, utcnow
from .config import (CHANNEL, DATA_DIR, ENV_NAME, INSTALLERS_DIR, JWT_SECRET,
                     METADATA_DIR, PUBLISH_TOKEN, RELEASE_INFO, SITE_PASSWORD,
                     TARGETS_DIR, UPDATES_DIR, ensure_dirs)
from .db import Base, engine, get_db

ensure_dirs()  # before StaticFiles mounts below need the directories

app = FastAPI(title="PDF Text Editor backend", docs_url=None, redoc_url=None)
log = logging.getLogger("pdfte")

# The account system (email verification, password reset, password change, a
# brute-force throttle, optional CORS, and the account pages) lives in
# accounts.py and wires itself in with this one call.
accounts.install(app)
# Private TOTP-gated metrics dashboard (no-op until METRICS_TOTP_SECRET is set).
metrics.install(app)


@app.on_event("startup")
def _startup() -> None:
    ensure_dirs()
    Base.metadata.create_all(engine)


# --- dev-site password gate -------------------------------------------------
# When PDFTE_SITE_PASSWORD is set (the dev environment), the human-facing site
# and the installer download sit behind a shared password so the public cannot
# install the dev build. Left empty in production, so prod stays open. The
# machine-to-machine paths below are never gated, so the desktop self-updater,
# Railway's health check, and the token-protected publish flow keep working.
_GATE_COOKIE = "pdfte_gate"
_GATE_OPEN_PREFIXES = ("/updates", "/health", "/api/version", "/api/publish",
                       "/api/repo-state", "/_gate/",
                       # The desktop app calls the account API directly and has
                       # no way to carry the site-gate cookie, so these stay open
                       # (they have their own auth + rate limiting).
                       "/api/auth", "/api/onboard", "/api/account",
                       # Release/admin paths are token-protected, not cookie-gated.
                       "/api/admin")


def _gate_token() -> str:
    """A cookie value that cannot be forged without the deploy's JWT secret, and
    that changes (invalidating old cookies) if the password is rotated."""
    return hmac.new(JWT_SECRET.encode(), SITE_PASSWORD.encode(),
                    hashlib.sha256).hexdigest()


def _gate_ok(request: Request) -> bool:
    cookie = request.cookies.get(_GATE_COOKIE, "")
    return bool(cookie) and hmac.compare_digest(cookie, _gate_token())


def _login_page(error: bool = False) -> str:
    err = "<p class=err>That password did not match. Try again.</p>" if error else ""
    return _LOGIN_HTML.replace("__LOGO__", _LOGO_SVG).replace("__ERR__", err)


@app.middleware("http")
async def _site_gate(request: Request, call_next):
    if not SITE_PASSWORD:
        return await call_next(request)          # gate disabled (production)
    if request.url.path.startswith(_GATE_OPEN_PREFIXES) or _gate_ok(request):
        return await call_next(request)
    return HTMLResponse(_login_page(), status_code=401)


@app.middleware("http")
async def _visitor_cookie(request: Request, call_next):
    """Give every browser a stable anonymous id so page views, downloads, and the
    agreement can be stitched into one funnel. Defined AFTER the gate so it runs
    outermost and stamps the cookie on every response. httponly: the server reads
    it (the sendBeacon carries it automatically); page JS never needs it."""
    resp = await call_next(request)
    if not request.cookies.get(track.VID_COOKIE):
        resp.set_cookie(track.VID_COOKIE, track.new_vid(),
                        max_age=60 * 60 * 24 * 365, httponly=True, samesite="lax")
    return resp


@app.post("/_gate/login")
async def gate_login(request: Request) -> Response:
    form = await request.form()
    if SITE_PASSWORD and hmac.compare_digest(str(form.get("password", "")),
                                             SITE_PASSWORD):
        resp = Response(status_code=303, headers={"Location": "/"})
        resp.set_cookie(_GATE_COOKIE, _gate_token(), max_age=60 * 60 * 24 * 30,
                        httponly=True, samesite="lax", secure=True)
        return resp
    return HTMLResponse(_login_page(error=True), status_code=401)


# --- tufup update repository (read by the desktop self-updater) -------------
# The TUF client fetches signed metadata then the archive/patch targets.
#
# The heavy target archives are offloaded to R2 (free egress): if the file is in
# R2 we 302 the client straight there on a short-lived signed URL; otherwise we
# serve it from the volume exactly as before. This route is declared BEFORE the
# static mount so it wins for /updates/<plat>/targets/*, while the signed
# metadata (tiny) keeps being served statically by the mount below. tufup checks
# the target's signed hash regardless of where the bytes come from, so the
# redirect changes nothing about update integrity.
@app.get("/updates/{platform}/targets/{filename}")
def update_target(platform: str, filename: str):
    safe = os.path.basename(filename)
    if platform not in ("mac", "win") or safe != filename:
        raise HTTPException(404, "Not found.")
    key = f"{CHANNEL}/updates/{platform}/targets/{safe}"
    if r2.exists(key):
        return RedirectResponse(r2.presigned_get(key), status_code=302,
                                headers={"Cache-Control": "no-store"})
    path = UPDATES_DIR / platform / "targets" / safe
    if not path.is_file():
        raise HTTPException(404, "Not found.")
    return FileResponse(str(path), media_type="application/octet-stream")


# Everything else under /updates (the signed metadata + release.json) stays a
# plain static file. The whole per-platform tree: /updates/<mac|win>/{metadata,
# targets}. The desktop client points at its platform's subtree (see appconfig).
app.mount("/updates", StaticFiles(directory=str(UPDATES_DIR), check_dir=False),
          name="updates")


# --- health + friendly manifest --------------------------------------------
@app.get("/health")
def health() -> dict:
    return {"ok": True, "channel": CHANNEL, "env": ENV_NAME}


@app.get("/api/version")
def api_version() -> JSONResponse:
    """The friendly manifest the app shows ('Update available', release notes).
    The actual update integrity is enforced by tufup; this is for display."""
    if RELEASE_INFO.exists():
        return JSONResponse(json.loads(RELEASE_INFO.read_text("utf-8")))
    return JSONResponse({"channel": CHANNEL, "version": None,
                         "notes": [], "mac": None, "windows": None})


# --- accounts ---------------------------------------------------------------
class RegisterIn(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=200)
    display_name: str = Field(default="", max_length=120)


class LoginIn(BaseModel):
    email: EmailStr
    password: str


def _user_public(u: "models.User") -> dict:
    return {"id": u.id, "email": u.email, "display_name": u.display_name,
            "created_at": u.created_at.isoformat() if u.created_at else None}


@app.post("/api/auth/register")
def register(body: RegisterIn, db: Session = Depends(get_db)) -> dict:
    email = body.email.lower().strip()
    exists = db.scalar(select(models.User).where(models.User.email == email))
    if exists:
        raise HTTPException(409, "An account with that email already exists.")
    user = models.User(email=email,
                       password_hash=security.hash_password(body.password),
                       display_name=body.display_name.strip())
    db.add(user)
    db.commit()
    db.refresh(user)
    # Link any download-gate agreements made with this email to the new account.
    db.query(models.Consent).filter(
        models.Consent.email == email,
        models.Consent.account_id.is_(None),
    ).update({models.Consent.account_id: user.id})
    # The download gate already proved this email with the 6-digit code, so a new
    # account for that same address starts confirmed -- don't make them verify the
    # same email twice. (The client's follow-up verify/send then no-ops.)
    proven = db.scalar(select(models.Consent.id).where(
        models.Consent.email == email,
        models.Consent.email_verified.is_(True),
    ))
    if proven is not None:
        user.email_verified = True
        user.email_verified_at = datetime.datetime.now(datetime.timezone.utc)
    db.commit()
    return {"token": security.make_token(user.id, user.email),
            "user": _user_public(user)}


@app.post("/api/auth/login")
def login(body: LoginIn, db: Session = Depends(get_db)) -> dict:
    email = body.email.lower().strip()
    user = db.scalar(select(models.User).where(models.User.email == email))
    if not user or not security.verify_password(body.password, user.password_hash):
        raise HTTPException(401, "Incorrect email or password.")
    return {"token": security.make_token(user.id, user.email),
            "user": _user_public(user)}


def current_user(authorization: str = Header(default=""),
                 db: Session = Depends(get_db)) -> "models.User":
    if not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "Missing bearer token.")
    token = authorization.split(" ", 1)[1].strip()
    try:
        payload = security.decode_token(token)
    except Exception:
        raise HTTPException(401, "Invalid or expired token.")
    user = db.get(models.User, int(payload["sub"]))
    if not user:
        raise HTTPException(401, "Account no longer exists.")
    return user


@app.get("/api/auth/me")
def me(user: "models.User" = Depends(current_user)) -> dict:
    return _user_public(user)


# --- installers + download page --------------------------------------------
@app.get("/download/{filename}")
def download(filename: str, request: Request, db: Session = Depends(get_db)):
    # basename only: no path traversal can escape the installers dir.
    safe = os.path.basename(filename)
    if safe != filename:
        raise HTTPException(404, "Not found.")
    key = f"{CHANNEL}/installers/{safe}"
    from_r2 = r2.exists(key)
    if not from_r2 and not (INSTALLERS_DIR / safe).is_file():
        raise HTTPException(404, "Not found.")
    # Count the real download (not 404s), with the platform read off the filename.
    track.record(db, request, "download", path=f"/download/{safe}",
                 platform=track.platform_from_name(safe))
    # Offloaded to R2 (free egress) when present; else served from the volume.
    if from_r2:
        return RedirectResponse(r2.presigned_get(key), status_code=302,
                                headers={"Cache-Control": "no-store"})
    return FileResponse(str(INSTALLERS_DIR / safe), filename=safe,
                        media_type="application/octet-stream")


# --- download-gate consent -------------------------------------------------
class ConsentIn(BaseModel):
    email: EmailStr
    agreed: bool


class GateVerifyIn(BaseModel):
    email: EmailStr
    code: str = ""


def _client_ip(request: Request) -> str:
    # X-Forwarded-For is set by Railway's proxy; take the first hop.
    fwd = request.headers.get("x-forwarded-for", "")
    return (fwd.split(",")[0].strip() if fwd
            else (request.client.host if request.client else ""))[:64]


def _release_files() -> dict:
    info = (json.loads(RELEASE_INFO.read_text("utf-8"))
            if RELEASE_INFO.exists() else {})
    return {"mac": info.get("mac"), "windows": info.get("windows")}


def _record_consent(db, email, account, request, *, verified: bool) -> None:
    """Write the provable agreement record for this email."""
    db.add(models.Consent(
        email=email, account_id=account.id if account else None,
        terms_version=TERMS_VERSION, ip=_client_ip(request),
        user_agent=request.headers.get("user-agent", "")[:1000],
        email_verified=verified,
        visitor_id=track.visitor_id(request)))
    db.commit()


_GATE_CODE_TTL = datetime.timedelta(minutes=10)


def _send_gate_code(db, email: str, request: Request) -> None:
    """Email a fresh 6-digit code, unless one was sent seconds ago (anti-spam).
    The code (not the send) is what gates the download, so a delivery failure is
    logged, never raised."""
    latest = db.scalar(select(GateCode).where(
        GateCode.email == email, GateCode.used_at.is_(None))
        .order_by(GateCode.id.desc()))
    if latest and (utcnow() - latest.created_at).total_seconds() < 30:
        return                       # just sent one; don't spam the inbox
    db.execute(delete(GateCode).where(
        GateCode.email == email, GateCode.used_at.is_(None)))
    display, code_hash = security.new_gate_code()
    db.add(GateCode(email=email, code_hash=code_hash,
                    expires_at=utcnow() + _GATE_CODE_TTL))
    db.commit()
    text_body = (f"Your PDF for Free download code is {display}.\n\n"
                 f"It expires in 10 minutes. If you did not request it, you can "
                 f"ignore this email.")
    html_body = (
        '<div style="font-family:system-ui,sans-serif;font-size:15px;color:#2A2520">'
        'Your PDF for Free download code:'
        f'<div style="font-size:32px;font-weight:700;letter-spacing:.22em;'
        f'margin:14px 0;color:#AA4E2C">{display}</div>'
        'It expires in 10 minutes. If you did not request it, ignore this email.'
        '</div>')
    try:
        emailer.send_email(email, "Your PDF for Free download code",
                           text_body, html_body)
    except Exception:
        log.warning("gate code email failed to send")


@app.post("/api/consent")
def record_consent(body: ConsentIn, request: Request,
                   db: Session = Depends(get_db)) -> dict:
    """Step 1 of the download gate. An existing-account email takes the password
    path (which proves the address), so its agreement is recorded now. A NEW
    visitor is emailed a 6-digit code; their agreement is recorded only once they
    verify it via /api/gate/verify."""
    if not body.agreed:
        raise HTTPException(400, "You must agree to the Terms to download.")
    email = body.email.lower().strip()
    account = db.scalar(select(models.User).where(models.User.email == email))
    if account is not None:
        _record_consent(db, email, account, request, verified=True)
        return {"ok": True, "has_account": True}
    _send_gate_code(db, email, request)
    return {"ok": True, "has_account": False, "needs_code": True}


@app.post("/api/gate/verify")
def gate_verify(body: GateVerifyIn, request: Request,
                db: Session = Depends(get_db)) -> dict:
    """Step 2: check the emailed 6-digit code, record the VERIFIED agreement, and
    return the installer names so the download can start."""
    email = body.email.lower().strip()
    row = db.scalar(select(GateCode).where(
        GateCode.email == email, GateCode.used_at.is_(None))
        .order_by(GateCode.id.desc()))
    now = utcnow()
    if row is None or row.expires_at < now:
        raise HTTPException(400, "That code has expired -- request a new one.")
    if row.attempts >= 5:
        raise HTTPException(429, "Too many tries -- request a new code.")
    if not hmac.compare_digest(row.code_hash,
                               security.hash_gate_code(body.code)):
        row.attempts += 1
        db.commit()
        raise HTTPException(400, "That code is not right.")
    row.used_at = now
    account = db.scalar(select(models.User).where(models.User.email == email))
    _record_consent(db, email, account, request, verified=True)
    return {"ok": True, **_release_files()}


class TrackIn(BaseModel):
    path: str = ""
    ref: str = ""


@app.post("/api/track")
def api_track(body: TrackIn, request: Request,
              db: Session = Depends(get_db)) -> dict:
    """Record an anonymous page view -- a sendBeacon fires on every page load.
    Public + best-effort: never raises, stores no PII beyond the CF-derived city.
    Only a same-origin path is kept (never a full URL)."""
    path = body.path if body.path.startswith("/") else "/"
    track.record(db, request, "pageview", path=path, referrer=body.ref)
    return {"ok": True}


GITHUB_URL = "https://github.com/eddaboss/PDFTextEditor"
SITE_URL = "https://pdf-for-free.com"  # the canonical public domain
# Bump when the Terms / Privacy change materially: the download gate records
# which version each person agreed to, so a bump means new agreements going
# forward (and you can tell who agreed to what).
TERMS_VERSION = "2026-07-13"
# Where the Donate button points. Set this to your real link (GitHub Sponsors,
# Ko-fi, Buy Me a Coffee, a Stripe payment link, PayPal, etc.).
DONATE_URL = os.environ.get("PDFTE_DONATE_URL",
                            "https://venmo.com/u/Edward-Lukowsky")

# The landing page. Plain string (not an f-string) so the CSS braces stay
# literal; the dynamic bits are injected by ``home()`` via ``.replace()`` on
# the __TOKENS__ below. Positioning: this editor is ACTUALLY free -- it runs on
# your own machine, so the usual "free" PDF-tool catches (pay-to-edit,
# watermarks, daily caps, upload-to-our-cloud) simply do not apply.
_PAGE = """<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>PDF for Free - the PDF editor that's actually free</title>
<link rel="icon" href="/favicon.ico" sizes="32x32 48x48">
<link rel="icon" type="image/png" sizes="96x96" href="/favicon-96.png">
<link rel="icon" type="image/png" sizes="192x192" href="/favicon-192.png">
<link rel="icon" type="image/svg+xml" href="/favicon.svg">
<link rel="apple-touch-icon" href="/apple-touch-icon.png">
<link rel="mask-icon" href="/favicon.svg" color="#C2643F">
<meta name="theme-color" content="#C2643F">
<meta name=description content="A free PDF editor for Mac and Windows. Retype text in place in the document's own font, OCR scans, sign, and reorder pages, all on your own computer. No subscription, no watermark, nothing uploaded.">
<meta name=robots content="index,follow">
<link rel=canonical href="https://pdf-for-free.com/">
<meta property="og:type" content="website">
<meta property="og:site_name" content="PDF for Free">
<meta property="og:title" content="PDF for Free - the PDF editor that's actually free">
<meta property="og:description" content="A free PDF editor for Mac and Windows. Retype text in place in the document's own font, OCR scans, and sign. No subscription, no watermark, nothing uploaded.">
<meta property="og:url" content="https://pdf-for-free.com/">
<meta property="og:image" content="https://pdf-for-free.com/og.png">
<meta property="og:image:width" content="1200">
<meta property="og:image:height" content="630">
<meta name="twitter:card" content="summary_large_image">
<meta name="twitter:title" content="PDF for Free - the PDF editor that's actually free">
<meta name="twitter:description" content="A free PDF editor for Mac and Windows. Edit text in place, OCR scans, sign. No subscription, no watermark, nothing uploaded.">
<meta name="twitter:image" content="https://pdf-for-free.com/og.png">
<script type="application/ld+json">
{"@context":"https://schema.org","@type":"SoftwareApplication","name":"PDF for Free","alternateName":"PDF Text Editor","url":"https://pdf-for-free.com/","description":"A real PDF editor that runs on your Mac or PC. Retype text in place in the document's own font. No subscription, no watermark, no upload. Actually free.","applicationCategory":"UtilitiesApplication","operatingSystem":"macOS, Windows","downloadUrl":"https://pdf-for-free.com/","screenshot":"https://pdf-for-free.com/og.png","offers":{"@type":"Offer","price":"0","priceCurrency":"USD"},"publisher":{"@type":"Organization","name":"PDF for Free","url":"https://pdf-for-free.com/"}}
</script>
<link rel=preconnect href="https://fonts.googleapis.com">
<link rel=preconnect href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Bricolage+Grotesque:opsz,wght@12..96,500..800&family=Hanken+Grotesk:wght@400;500;600;700&display=swap" rel=stylesheet>
<style>
:root{
  --paper:#FBF9F5; --panel:#F5F1EA; --canvas:#ECE6DC; --line:#E2DACD;
  --ink:#2A2520; --ink2:#5C5346; --ink3:#897E6E;
  --clay:#C2643F; --clay-fill:#AA4E2C; --clay-press:#8B3E23; --clay-deep:#6B311F;
  --sheet:#fff; --shadow:rgba(58,40,26,.16); --shadow2:rgba(58,40,26,.08);
  --display:"Bricolage Grotesque",ui-sans-serif,system-ui,sans-serif;
  --body:"Hanken Grotesk",ui-sans-serif,-apple-system,Segoe UI,Roboto,system-ui,sans-serif;
  color-scheme:light;
}
*{box-sizing:border-box}
html{scroll-behavior:smooth}
body{margin:0;background:var(--paper);color:var(--ink);font-family:var(--body);
  font-size:clamp(16px,1.05vw,17.5px);line-height:1.6;-webkit-font-smoothing:antialiased;
  text-rendering:optimizeLegibility}
.wrap{width:min(1080px,92vw);margin-inline:auto}
h1,h2,h3{font-family:var(--display);font-weight:700;letter-spacing:-.02em;
  line-height:1.04;text-wrap:balance;margin:0}
p{text-wrap:pretty}
a{color:inherit}
em{font-style:normal;opacity:.7}

/* header */
header{position:sticky;top:0;z-index:50;background:color-mix(in srgb,var(--paper) 86%,transparent);
  backdrop-filter:saturate(140%) blur(10px);border-bottom:1px solid var(--line)}
.bar{display:flex;align-items:center;justify-content:space-between;gap:16px;
  height:60px}
.brand{display:flex;align-items:center;gap:10px;font-family:var(--display);
  font-weight:700;font-size:18px;letter-spacing:-.02em}
.mark{width:30px;height:30px;flex:none;display:block}
.mark svg{width:100%;height:100%;display:block}
.tagpill{font-size:12px;font-weight:600;color:var(--clay-press);
  background:rgba(194,100,63,.12);padding:3px 9px;border-radius:999px;
  border:1px solid rgba(194,100,63,.22)}
.chan{font-size:11px;font-weight:600;background:var(--canvas);color:var(--ink2);
  padding:2px 8px;border-radius:6px}
.nav{display:flex;align-items:center;gap:22px;font-size:15px;font-weight:500}
.nav a{color:var(--ink2);text-decoration:none;transition:color .15s}
.nav a:hover{color:var(--clay-press)}
.nav a.navdonate{background:var(--clay-fill);color:#fff;padding:7px 16px;
  border-radius:9px;font-weight:600;transition:background .15s}
.nav a.navdonate:hover{background:var(--clay-press);color:#fff}
@media(max-width:680px){.nav .hidesm{display:none}.nav{gap:0}}

/* buttons */
.btn{display:inline-flex;align-items:center;gap:9px;padding:14px 24px;border-radius:12px;
  background:var(--clay-fill);color:#fff;text-decoration:none;font-weight:600;
  font-family:inherit;font-size:16px;border:1px solid transparent;cursor:pointer;
  transition:transform .12s cubic-bezier(.2,.8,.2,1),background .15s,box-shadow .15s;
  box-shadow:0 1px 2px var(--shadow2)}
.btn:hover{background:var(--clay-press);transform:translateY(-1px);
  box-shadow:0 8px 22px rgba(139,62,35,.22)}
.btn:active{transform:translateY(0)}
.btn-ghost{background:transparent;color:var(--ink);border-color:var(--line);box-shadow:none}
.btn-ghost:hover{background:var(--panel);border-color:var(--ink3);color:var(--ink)}
.btn-soon{background:var(--canvas);color:var(--ink3);cursor:default;box-shadow:none}
.btn-soon:hover{background:var(--canvas);transform:none;box-shadow:none}
.btn svg{width:18px;height:18px;flex:none}
:focus-visible{outline:3px solid rgba(194,100,63,.5);outline-offset:2px;border-radius:8px}

/* hero */
.hero{display:grid;grid-template-columns:1.05fr .95fr;gap:clamp(32px,5vw,72px);
  align-items:center;padding:clamp(56px,9vw,116px) 0 clamp(40px,6vw,72px)}
.hero h1{font-size:clamp(40px,6.4vw,76px);font-weight:800}
.hero h1 .u{color:var(--clay-press);position:relative;white-space:nowrap}
.hero h1 .u::after{content:"";position:absolute;left:-2px;right:-2px;bottom:.08em;height:.14em;
  background:rgba(194,100,63,.28);border-radius:3px;z-index:-1}
.lede{font-size:clamp(18px,1.6vw,21px);color:var(--ink2);margin:22px 0 0;
  max-width:36ch}
/* playful hero Q&A */
.qa{margin:26px 0 0;max-width:46ch;display:flex;flex-direction:column;gap:15px}
.qa div{display:flex;flex-direction:column;gap:3px}
.qa dt{font-size:clamp(15px,1.25vw,17px);color:var(--ink2);line-height:1.4}
.qa dt::before{content:"Q.";font-family:var(--display);font-weight:700;
  color:var(--ink3);margin-right:7px}
.qa dd{margin:0;font-size:clamp(16px,1.35vw,18px);font-weight:700;
  color:var(--clay-press);line-height:1.4}
.qa dd::before{content:"A.";font-family:var(--display);margin-right:7px;
  color:var(--clay)}
.cta{display:flex;gap:14px;flex-wrap:wrap;margin-top:32px}
.ver{margin:18px 0 0;font-size:14px;color:var(--ink3)}
.ver b{color:var(--clay-press);font-weight:600}
.micro{margin:6px 0 0;font-size:13.5px;color:var(--ink3)}
/* the editor mockup needs real room; below this the hero stacks to one column */
@media(max-width:959px){.hero{grid-template-columns:1fr;padding-top:48px}
  .lede{max-width:46ch} .heroart{order:-1}}
/* in the deck at mid widths keep the mockup, but give the text column the larger
   share so the headline doesn't wrap to a sliver, and it still fits one screen */
@media (pointer:fine) and (min-width:700px) and (max-width:959px) and (min-height:680px) and (prefers-reduced-motion:no-preference){
  html.motion-ready .hero{grid-template-columns:minmax(0,1.55fr) minmax(0,1fr);
    gap:clamp(16px,3vw,34px)}
  html.motion-ready .hero .heroart{display:block;order:0;align-self:center}}

/* editor mock */
.heroart{justify-self:center;width:100%}
.mock{background:var(--panel);border:1px solid var(--line);border-radius:16px;
  box-shadow:0 30px 60px -20px var(--shadow),0 8px 18px -10px var(--shadow2);
  overflow:hidden;transform:rotate(.4deg)}
.mockbar{display:flex;align-items:center;gap:8px;padding:11px 14px;background:var(--canvas);
  border-bottom:1px solid var(--line)}
.dot{width:11px;height:11px;border-radius:50%;background:#d8cdbb}
.mocktitle{margin-left:8px;font-size:13px;color:var(--ink3);font-weight:500}
.mockbody{display:grid;grid-template-columns:46px 1fr;min-height:300px}
.rail{background:var(--panel);border-right:1px solid var(--line);
  display:flex;flex-direction:column;align-items:center;gap:12px;padding:14px 0}
.tool{width:24px;height:24px;border-radius:7px;background:var(--canvas)}
.tool.on{background:rgba(194,100,63,.16);box-shadow:inset 0 0 0 1.5px var(--clay)}
.page{background:var(--sheet);margin:22px;border-radius:4px;padding:30px 28px;
  box-shadow:0 2px 10px var(--shadow2);display:flex;flex-direction:column;gap:15px}
.ln{height:11px;border-radius:3px;background:#ece7df}
.ln.h{height:15px;width:62%;background:#d9cdbb}
.ln.s{width:48%}
.editln{position:relative;min-height:26px;display:flex;align-items:center;margin:3px 0}
.editbox{position:absolute;inset:-5px -8px;border:2px solid var(--clay);border-radius:5px;
  background:rgba(194,100,63,.05)}
.editln .txt{font-family:var(--body);font-weight:600;font-size:15px;color:var(--ink);
  line-height:1.35;position:relative;z-index:1}
.caret{display:inline-block;width:2px;height:18px;background:var(--clay-press);
  margin-left:1px;position:relative;z-index:1;animation:blink 1.1s steps(1) infinite}
@keyframes blink{50%{opacity:0}}
.hand{position:absolute;width:7px;height:7px;background:#fff;border:2px solid var(--clay);
  border-radius:2px;z-index:2}
.hand.tl{top:-9px;left:-12px}.hand.tr{top:-9px;right:-12px}
.hand.bl{bottom:-9px;left:-12px}.hand.br{bottom:-9px;right:-12px}

/* generic section */
section{padding:clamp(48px,7vw,96px) 0}
.eyebrow{display:inline-block;font-weight:600;font-size:14px;color:var(--clay-press);
  margin:0 0 14px}
h2{font-size:clamp(28px,3.6vw,44px)}
.sub{font-size:clamp(17px,1.4vw,19px);color:var(--ink2);max-width:60ch;margin:16px 0 0}

/* opening on a mac */
.macopen{border-top:1px solid var(--line)}
.macopen .steps{list-style:none;margin:26px 0 0;padding:0;max-width:62ch;
  display:flex;flex-direction:column;gap:16px;counter-reset:s}
.macopen .steps li{display:flex;gap:14px;font-size:16px;line-height:1.55;color:var(--ink2)}
.macopen .steps li>div{min-width:0;flex:1}
.macopen .steps li::before{counter-increment:s;content:counter(s);flex:none;
  width:26px;height:26px;border-radius:50%;background:var(--clay-fill);color:#fff;
  font-weight:700;font-size:14px;display:grid;place-items:center;margin-top:1px}
.macopen .steps b{color:var(--ink);font-weight:600}
.cmd{display:block;margin:11px 0 0;font-family:ui-monospace,Menlo,Consolas,monospace;
  font-size:13.5px;line-height:1.5;background:var(--ink);color:#f3e9dd;padding:12px 14px;
  border-radius:10px;overflow-x:auto;white-space:nowrap;-webkit-user-select:all;user-select:all;
  max-width:100%}
.macopen .micro{margin-top:22px}
.opengrid{display:grid;grid-template-columns:1fr 1fr;gap:clamp(28px,4.5vw,56px);margin-top:28px}
.opencol{min-width:0}
.osh{font-family:var(--display);font-size:18px;color:var(--clay-press);margin:0;
  padding-bottom:10px;border-bottom:1px solid var(--line)}
.opencol .steps{margin-top:16px;max-width:none}
@media(max-width:680px){.opengrid{grid-template-columns:1fr;gap:34px}}

/* the asterisk section */
.catch{border-top:1px solid var(--line)}
.catchgrid{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));
  gap:18px;margin-top:40px}
.catchitem{background:var(--panel);border:1px solid var(--line);border-radius:14px;
  padding:24px}
.catchitem .x{width:30px;height:30px;border-radius:8px;background:rgba(160,70,44,.1);
  color:var(--clay-press);display:grid;place-items:center;font-weight:800;margin-bottom:14px}
.catchitem h3{font-size:18px;font-weight:700}
.catchitem p{margin:8px 0 0;font-size:15px;color:var(--ink2)}

/* drenched "free means free" band */
.band{background:var(--clay-deep);color:#f6e7df;border-radius:24px;
  padding:clamp(40px,5.5vw,72px);margin:clamp(48px,7vw,96px) 0}
.band h2{color:#fff}
.band .sub{color:#e7c8b8}
.checks{list-style:none;padding:0;margin:36px 0 0;display:grid;
  grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:16px 32px}
.checks li{display:flex;gap:13px;align-items:flex-start;font-size:17px;line-height:1.45}
.checks .ck{flex:none;width:26px;height:26px;border-radius:50%;background:rgba(255,255,255,.12);
  display:grid;place-items:center;margin-top:1px}
.checks .ck svg{width:15px;height:15px;color:#ffd9c4}
.checks b{color:#fff;font-weight:700}
.checks span{color:#e7c8b8}

/* features */
.feat{display:flex;flex-wrap:wrap;justify-content:center;gap:0 40px;margin-top:44px}
.frow{flex:1 1 300px;max-width:440px;padding:22px 0;border-top:1px solid var(--line)}
.frow h3{font-size:19px;font-weight:700;display:flex;align-items:center;gap:11px}
.frow .ic{width:30px;height:30px;border-radius:9px;background:rgba(194,100,63,.12);
  color:var(--clay-press);display:grid;place-items:center;flex:none}
.frow .ic svg{width:17px;height:17px}
.frow p{margin:10px 0 0;font-size:15.5px;color:var(--ink2)}

/* what's paid */
.paid{border-top:1px solid var(--line)}
.paidgrid{display:flex;flex-wrap:wrap;justify-content:center;gap:18px;margin-top:40px}
.paiditem{flex:1 1 300px;max-width:430px;background:var(--panel);
  border:1px solid var(--line);border-radius:14px;padding:24px}
.ptag{display:inline-block;font-size:12px;font-weight:600;color:var(--clay-press);
  background:rgba(194,100,63,.12);border:1px solid rgba(194,100,63,.22);
  padding:3px 10px;border-radius:999px;margin-bottom:13px}
.paiditem h3{font-size:18px;font-weight:700}
.paiditem p{margin:8px 0 0;font-size:15px;color:var(--ink2)}
.paidnote{margin:26px 0 0;font-size:15px;color:var(--ink3);max-width:60ch}

/* donation */
.donate{border-top:1px solid var(--line)}
.donatebox{background:rgba(194,100,63,.08);border:1px solid var(--accent-border,rgba(194,100,63,.30));
  border-radius:20px;padding:clamp(32px,5vw,56px);text-align:center}
.donatebox h2{font-size:clamp(26px,3vw,38px)}
.donatebox p{margin:14px auto 0;max-width:54ch;font-size:17px;color:var(--ink2)}
.dbtn{margin-top:26px}

/* final cta */
.final{text-align:center;border-top:1px solid var(--line)}
.final h2{font-size:clamp(30px,4.2vw,52px)}
.final .sub{margin-inline:auto}
.final .cta{justify-content:center}

footer{border-top:1px solid var(--line);background:var(--panel)}
.foot{display:flex;align-items:center;justify-content:space-between;gap:18px;
  flex-wrap:wrap;padding:28px 0;font-size:14px;color:var(--ink3)}
.foot a{color:var(--ink2);text-decoration:none}.foot a:hover{color:var(--clay-press)}
/* a <button> that reads as a nav/footer link (opens the legal modal) */
.navlink{font:inherit;background:none;border:0;padding:0;margin:0;cursor:pointer;
  color:var(--ink2);transition:color .15s}
.navlink:hover{color:var(--clay-press)}
.foot .navlink{font-size:14px;color:var(--ink3)}.foot .navlink:hover{color:var(--clay-press)}

/* privacy / terms modal */
.lmodal{position:fixed;inset:0;z-index:120;display:flex;align-items:center;
  justify-content:center;padding:20px;background:rgba(42,37,32,.5);
  backdrop-filter:blur(3px)}
.lmodal[hidden]{display:none}
.lcard{position:relative;background:var(--paper);border:1px solid var(--line);
  border-radius:18px;width:min(680px,100%);max-height:86vh;overflow:hidden;
  display:flex;flex-direction:column;box-shadow:0 30px 80px -20px rgba(42,37,32,.5)}
.lclose{position:absolute;top:12px;right:14px;width:34px;height:34px;border:0;
  border-radius:9px;background:transparent;color:var(--ink2);font-size:24px;
  line-height:1;cursor:pointer;transition:background .15s,color .15s}
.lclose:hover{background:var(--canvas);color:var(--ink)}
.ltabs{display:flex;gap:6px;padding:18px 20px 0 22px;border-bottom:1px solid var(--line)}
.ltab{font:inherit;font-weight:600;background:none;border:0;cursor:pointer;
  padding:10px 14px;color:var(--ink3);border-bottom:2px solid transparent;
  margin-bottom:-1px;transition:color .15s,border-color .15s}
.ltab:hover{color:var(--ink)}
.ltab.on{color:var(--clay-press);border-bottom-color:var(--clay)}
.lbody{overflow-y:auto;padding:24px 26px 28px}
.lbody h3{font-family:var(--display);font-size:24px;margin:0 0 2px}
.lbody h4{font-family:var(--display);font-size:16px;margin:22px 0 6px;color:var(--ink)}
.lbody p{margin:0 0 12px;font-size:15px;line-height:1.6;color:var(--ink2);max-width:none}
.lbody .leff{font-size:13px;color:var(--ink3);margin-bottom:18px}
.lbody b{color:var(--ink);font-weight:700}

/* download gate (agree before download) */
.dlmodal{position:fixed;inset:0;z-index:100;display:flex;align-items:center;
  justify-content:center;padding:20px;background:rgba(42,37,32,.5);
  backdrop-filter:blur(3px)}
.dlmodal[hidden]{display:none}
.dlcard{position:relative;background:var(--paper);border:1px solid var(--line);
  border-radius:18px;width:min(440px,100%);padding:30px 28px 28px;
  box-shadow:0 30px 80px -20px rgba(42,37,32,.5)}
.dlcard h3{font-family:var(--display);font-size:23px;margin:0 6px 8px 0}
.dlintro{font-size:14.5px;color:var(--ink2);margin:0 0 18px}
.dlfield{width:100%;padding:12px 14px;border:1px solid var(--line);border-radius:10px;
  font:inherit;font-size:15px;background:#fff;color:var(--ink);margin:0 0 12px}
.dlfield:focus{outline:2px solid var(--clay);outline-offset:1px;border-color:var(--clay)}
.dlcheck{display:flex;gap:10px;align-items:flex-start;margin:16px 0 20px;
  font-size:14px;color:var(--ink2);line-height:1.5;cursor:pointer}
.dlcheck input{margin-top:3px;flex:none;width:17px;height:17px;accent-color:var(--clay-fill)}
.inlinelink{font:inherit;background:none;border:0;padding:0;cursor:pointer;
  color:var(--clay-press);font-weight:600;text-decoration:underline}
.dlgo{width:100%;justify-content:center}
.dlgo:disabled{opacity:.5;cursor:default;transform:none;box-shadow:none}
.dlerr{margin:12px 0 0;font-size:13.5px;color:#b3402a;font-weight:500}

/* ===================== FULL-BLEED COLOUR FIELDS =====================
   Each section is its own full-width colour "page": a ::before paints the colour
   edge to edge while the content column stays centred on top. Light pages carry
   the intro, instructions and fine print; saturated pages carry the beats (the
   dark "catches", the burnt-orange "free means free", the deep-rust features, the
   terracotta call to action). Applied site-wide, so on phones the sections simply
   stack as colour bands. */
body{overflow-x:clip}
main>.wrap>section{position:relative}
main>.wrap>section::before{content:"";position:absolute;z-index:-1;
  top:0;bottom:0;left:50%;width:100vw;transform:translateX(-50%)}
.hero::before{background:#FBF9F5}
.macopen::before{background:#ECE6DC}
.catch::before{background:#241F1A}
.bandwrap::before{background:#AA4E2C}
.paid::before{background:#F5F1EA}
.does::before{background:#8B3E23}
.final::before{background:#6B311F}
.donate::before{background:#FBF9F5}
/* the colour change is the seam now -- drop the old hairline separators */
.macopen,.catch,.paid,.does,.donate,.final{border-top:0}

/* dark / saturated pages: cream type */
.catch,.bandwrap,.does,.final{color:#F7E7DC}
.catch h2,.bandwrap h2,.does h2,.final h2{color:#fff}
.catch .sub,.bandwrap .sub,.does .sub,.final .sub{color:#EBD0C0}
.does .frow h3{color:#fff}
.does .frow p{color:#E7D4C6}
.does .frow{border-top-color:rgba(255,255,255,.16)}
.does .frow .ic{background:rgba(255,255,255,.14)}
.does .frow .ic svg{color:#FCE9DD}
.final .micro{color:#EBD0C0}

/* the "free means free" band: flatten the old panel, sit straight on the orange */
.band{background:transparent;border-radius:0;box-shadow:none;margin:0;padding:0}
.band h2{color:#fff}.band .sub{color:#F4DCCB}
.checks .ck{background:rgba(255,255,255,.18)}
.checks .ck svg{color:#fff}
.checks b{color:#fff}.checks span{color:#F4DCCB}

/* cards on the near-black "catches" page float as light cards */
.catch .catchitem{background:#FBF7F1;border-color:rgba(255,255,255,.10)}
.catch .catchitem h3{color:var(--ink)}
.catch .catchitem .x{background:rgba(160,70,44,.12);color:var(--clay-press)}
/* lift the cards off the matching panel on the light "paid" page */
.paid .paiditem{background:#fff}
.paid .paidnote{color:var(--ink2)}

/* buttons on the terracotta CTA page invert to read clearly */
.final .btn{background:#fff;color:var(--clay-press);box-shadow:0 8px 22px rgba(74,30,16,.3)}
.final .btn:hover{background:#FBEFE6;color:var(--clay-deep);transform:translateY(-1px)}
.final .btn-ghost{background:transparent;color:#fff;border-color:rgba(255,255,255,.55)}
.final .btn-ghost:hover{background:rgba(255,255,255,.12);border-color:#fff;color:#fff}

/* ============================ MOTION ============================
   Calm + editorial: ease-out only, never bounce. Everything is progressive
   enhancement -- the page is fully visible without JS (reveal initial states
   only arm once <html> gets .motion-ready), and reduced-motion collapses it. */
:root{
  --ease-out:cubic-bezier(.22,1,.36,1);   /* ease-out-quint */
  --ease-soft:cubic-bezier(.16,1,.3,1);    /* ease-out-expo  */
}
/* hero first-load entrance (the one rehearsed moment) */
.rise{animation:rise .7s var(--ease-soft) both}
.rise.d1{animation-delay:.06s}.rise.d2{animation-delay:.12s}
.rise.d3{animation-delay:.18s}.rise.d4{animation-delay:.24s}
@keyframes rise{from{opacity:0;transform:translateY(14px)}to{opacity:1;transform:none}}

/* scroll reveals -- only armed after JS adds .motion-ready, so no-JS ships visible */
html.motion-ready [data-rv]{opacity:0;
  transition:opacity .7s var(--ease-out),transform .7s var(--ease-out)}
html.motion-ready [data-rv=up]{transform:translateY(48px)}   /* text flies up from below */
html.motion-ready [data-rv=scale]{transform:scale(.975)}
html.motion-ready [data-rv].rv-in{opacity:1;transform:none}
/* staggered children for card grids / lists (legit sibling stagger, capped) */
html.motion-ready [data-rvs]>*{opacity:0;transform:translateY(34px);
  transition:opacity .7s var(--ease-out),transform .7s var(--ease-out)}
html.motion-ready [data-rvs].rv-in>*{opacity:1;transform:none}
html.motion-ready [data-rvs].rv-in>*:nth-child(2){transition-delay:.06s}
html.motion-ready [data-rvs].rv-in>*:nth-child(3){transition-delay:.12s}
html.motion-ready [data-rvs].rv-in>*:nth-child(4){transition-delay:.18s}
html.motion-ready [data-rvs].rv-in>*:nth-child(5){transition-delay:.24s}
html.motion-ready [data-rvs].rv-in>*:nth-child(6){transition-delay:.30s}

/* ===== full-page deck: one section per screen, hard snap =====
   MANDATORY snap + scroll-snap-stop:always, so the page moves strictly section by
   section -- every scroll lands cleanly on the next "page" and you cannot rest
   half-way between two. Each section fills the screen and centers its content.
   Enabled only on tall, mouse-driven, motion-OK viewports; phones, tablets and
   short windows scroll normally (the fly-in reveals still run). A section whose
   content genuinely exceeds the screen stays scrollable, so nothing is trapped. */
@media (pointer:fine) and (min-width:700px) and (min-height:680px) and (prefers-reduced-motion:no-preference){
  html.motion-ready{scroll-snap-type:y mandatory;scroll-padding-top:60px}
  html.motion-ready main>.wrap>section{
    scroll-snap-align:start;scroll-snap-stop:always;
    min-height:100vh;min-height:100svh;
    padding-top:clamp(48px,5.5vh,76px);padding-bottom:clamp(36px,4.5vh,60px)}
  /* center each page's content, without overriding the hero's own grid layout */
  html.motion-ready main>.wrap>section:not(.hero){
    display:flex;flex-direction:column;justify-content:safe center}
  html.motion-ready main>.wrap>section.hero{align-content:safe center;
    padding-top:clamp(60px,7vh,84px);padding-bottom:clamp(28px,3.5vh,52px)}

  /* ---- fill the page: each section's content + spacing scale up (vh-based, so
     they track window height) so every page is well-filled like the hero, not a
     small centred cluster. Single-child panels (band, donate) get a min-height. ---- */
  html.motion-ready main>.wrap>section h2{font-size:clamp(30px,4.8vh,50px)}
  html.motion-ready main .sub{font-size:clamp(17px,2.3vh,21px);margin-top:clamp(14px,2.1vh,22px)}
  html.motion-ready .hero h1{font-size:clamp(38px,6.2vh,62px)}
  html.motion-ready .hero .qa{margin-top:clamp(14px,2.4vh,24px);gap:clamp(9px,1.6vh,15px)}
  html.motion-ready .hero .qa dt{font-size:clamp(14px,1.8vh,17px)}
  html.motion-ready .hero .qa dd{font-size:clamp(15px,2vh,18px)}
  html.motion-ready .hero .cta{margin-top:clamp(16px,2.8vh,32px)}
  html.motion-ready .hero .ver{margin-top:clamp(10px,1.6vh,16px)}
  html.motion-ready .opengrid,html.motion-ready .catchgrid,
    html.motion-ready .paidgrid,html.motion-ready .feat{margin-top:clamp(18px,3.2vh,38px)}
  html.motion-ready .catchgrid{gap:clamp(13px,2.2vh,22px)}
  html.motion-ready .catchitem,html.motion-ready .paiditem{padding:clamp(17px,2.7vh,28px)}
  html.motion-ready .catchitem h3,html.motion-ready .paiditem h3{font-size:clamp(17px,2.1vh,20px)}
  html.motion-ready .catchitem p,html.motion-ready .paiditem p{font-size:clamp(14px,1.7vh,16px);
    margin-top:clamp(6px,1.1vh,11px)}
  html.motion-ready .catchitem .x{width:clamp(27px,3.2vh,34px);height:clamp(27px,3.2vh,34px);
    margin-bottom:clamp(9px,1.5vh,14px)}
  html.motion-ready .frow{padding:clamp(12px,2.1vh,22px) 0}
  html.motion-ready .frow h3{font-size:clamp(18px,2.3vh,22px)}
  html.motion-ready .frow p{font-size:clamp(14px,1.7vh,16px);margin-top:clamp(7px,1.2vh,12px)}
  html.motion-ready .macopen .steps{gap:clamp(10px,1.8vh,18px)}
  html.motion-ready .macopen .steps li{font-size:clamp(14px,1.8vh,16px)}
  html.motion-ready .osh{font-size:clamp(16px,2vh,19px);padding-bottom:clamp(8px,1.4vh,13px)}
  html.motion-ready .bandwrap>.band{width:100%;min-height:58vh;
    display:flex;flex-direction:column;justify-content:center;
    padding:clamp(30px,4.6vh,64px) clamp(36px,5vw,76px)}
  html.motion-ready .band .checks{margin-top:clamp(18px,3.2vh,38px);
    gap:clamp(13px,2.2vh,20px) 36px;font-size:clamp(15px,2vh,18px)}
  html.motion-ready .paidnote{margin-top:clamp(16px,2.6vh,28px)}
  html.motion-ready main>.wrap>section.final h2{font-size:clamp(40px,7vh,72px)}
  html.motion-ready main>.wrap>section.final{justify-content:space-between;
    padding-top:clamp(60px,8.5vh,104px);padding-bottom:clamp(48px,7vh,88px)}
  html.motion-ready .final .sub{font-size:clamp(18px,2.6vh,23px);margin-top:0}
  html.motion-ready .final .cta{margin-top:0}
  html.motion-ready .donate .donatebox{width:100%;min-height:54vh;
    display:flex;flex-direction:column;justify-content:center;align-items:center;
    padding:clamp(34px,5vh,68px) clamp(32px,5vw,64px)}
  html.motion-ready main>.wrap>section.donate .donatebox h2{font-size:clamp(32px,5vh,50px)}
  html.motion-ready .donatebox p{font-size:clamp(17px,2.3vh,21px);margin-top:clamp(14px,2vh,20px)}
}

/* hover micro-interactions */
.catchitem,.paiditem{transition:transform .2s var(--ease-out),
  box-shadow .2s var(--ease-out),border-color .2s var(--ease-out)}
.catchitem:hover,.paiditem:hover{transform:translateY(-3px);
  border-color:rgba(194,100,63,.4);
  box-shadow:0 16px 34px -18px var(--shadow),0 4px 10px -6px var(--shadow2)}
.frow{transition:border-top-color .2s var(--ease-out)}
.frow:hover{border-top-color:var(--clay)}
.frow .ic{transition:background .2s var(--ease-out),transform .2s var(--ease-out)}
.frow:hover .ic{background:rgba(194,100,63,.2);transform:scale(1.07)}
.cmd{transition:box-shadow .2s var(--ease-out)}
.cmd:hover{box-shadow:0 8px 20px -10px rgba(42,37,32,.55)}
.checks li{transition:transform .2s var(--ease-out)}
.checks li:hover{transform:translateX(3px)}
.btn:active{transform:scale(.98)}

/* header gains a touch more weight once you scroll past the hero */
header{transition:box-shadow .25s var(--ease-out),background .25s var(--ease-out)}
header.scrolled{box-shadow:0 6px 24px -14px var(--shadow);
  background:color-mix(in srgb,var(--paper) 94%,transparent)}

/* modal entrances: backdrop fades, card eases up */
.lmodal,.dlmodal{animation:backdropIn .25s var(--ease-out)}
.lcard,.dlcard{animation:cardIn .34s var(--ease-out) both}
@keyframes backdropIn{from{opacity:0}to{opacity:1}}
@keyframes cardIn{from{opacity:0;transform:translateY(10px) scale(.97)}
  to{opacity:1;transform:none}}

@media(prefers-reduced-motion:reduce){
  .rise,.lmodal,.dlmodal,.lcard,.dlcard{animation:none}
  .caret{animation:none}html{scroll-behavior:auto}
  html.motion-ready [data-rv],html.motion-ready [data-rvs]>*{
    opacity:1;transform:none;transition:none}
  *{transition-duration:.01ms !important;animation-duration:.01ms !important}}
</style></head><body>

<header><div class="wrap bar">
  <span class=brand><span class=mark>__LOGO__</span>PDF for Free __CHAN__</span>
  <nav class=nav>
    <a class=hidesm href="#free">[[nav1]]</a>
    <a class=hidesm href="#does">[[nav2]]</a>
    <button class="navlink legalbtn hidesm" type=button data-legal=terms>[[nav_legal]]</button>
    <a class=navdonate href="__DONATE__" target="_blank" rel="noopener noreferrer">[[nav_donate]]</a>
  </nav>
</div></header>

<main><div class=wrap>

  <section class=hero>
    <div>
      <h1 class="rise">[[hero_pre]] <span class=u>[[hero_hl]]</span>.</h1>
      <dl class="qa rise d1">
        <div><dt>[[q1]]</dt><dd>[[a1]]</dd></div>
        <div><dt>[[q2]]</dt><dd>[[a2]]</dd></div>
        <div><dt>[[q3]]</dt><dd>[[a3]]</dd></div>
        <div><dt>[[q4]]</dt><dd>[[a4]]</dd></div>
      </dl>
      <div class="cta rise d2">__MAC_BTN__ __WIN_BTN__</div>
      <p class="ver rise d2">__VER__</p>
      <p class="micro rise d2">[[hero_micro]]</p>
    </div>
    <div class="heroart rise d3">
      <div class=mock>
        <div class=mockbar><span class=dot></span><span class=dot></span><span class=dot></span><span class=mocktitle>letter.pdf - edited</span></div>
        <div class=mockbody>
          <div class=rail><span class="tool on"></span><span class=tool></span><span class=tool></span><span class=tool></span></div>
          <div class=page>
            <div class="ln h"></div>
            <div class=ln></div>
            <div class=editln>
              <span class=editbox></span>
              <span class=txt>the document's own font</span><span class=caret></span>
              <span class="hand tl"></span><span class="hand tr"></span><span class="hand bl"></span><span class="hand br"></span>
            </div>
            <div class=ln></div>
            <div class="ln s"></div>
          </div>
        </div>
      </div>
    </div>
  </section>

  <section id=open class=macopen>
    <h2>Opening it the first time</h2>
    <p class=sub>It is a free app I sign myself, not through a paid developer
      program, so the first launch shows a warning on Mac and on Windows. You only
      do this once.</p>
    <div class=opengrid>
      <div class=opencol>
        <h3 class=osh>macOS</h3>
        <ol class=steps>
          <li><div>Open the <b>.dmg</b> and drag <b>__APPNAME__</b> into your
            <b>Applications</b> folder.</div></li>
          <li><div>If macOS says the app is damaged or cannot be opened, open the
            <b>Terminal</b> app and paste this line, then press Return:
            <code class=cmd>xattr -dr com.apple.quarantine "/Applications/__APPNAME__.app"</code></div></li>
          <li><div>Open <b>__APPNAME__</b> normally. It opens every time after that.</div></li>
        </ol>
        <p class=micro>No Terminal? Control-click the app in Applications, choose
          <b>Open</b>, then <b>Open</b> again.</p>
      </div>
      <div class=opencol>
        <h3 class=osh>Windows</h3>
        <ol class=steps>
          <li><div>Run the downloaded <b>.exe</b> installer.</div></li>
          <li><div>If <b>Microsoft Defender SmartScreen</b> says "Windows protected
            your PC," click <b>More info</b>, then <b>Run anyway</b>.</div></li>
          <li><div>Finish installing and open <b>__APPNAME__</b> normally.</div></li>
        </ol>
        <p class=micro>Or right-click the <b>.exe</b>, choose <b>Properties</b>,
          tick <b>Unblock</b>, then <b>OK</b> before running.</p>
      </div>
    </div>
  </section>

  <section class=catch>
    <h2>[[catch_heading]]</h2>
    <p class=sub>[[catch_sub]]</p>
    <div class=catchgrid>
      <div class=catchitem><div class=x>&times;</div><h3>[[catch1_t]]</h3><p>[[catch1_b]]</p></div>
      <div class=catchitem><div class=x>&times;</div><h3>[[catch2_t]]</h3><p>[[catch2_b]]</p></div>
      <div class=catchitem><div class=x>&times;</div><h3>[[catch3_t]]</h3><p>[[catch3_b]]</p></div>
      <div class=catchitem><div class=x>&times;</div><h3>[[catch4_t]]</h3><p>[[catch4_b]]</p></div>
    </div>
  </section>

  <section id=free class=bandwrap>
    <div class=band>
      <h2>[[band_heading]]</h2>
      <p class=sub>[[band_sub]]</p>
      <ul class=checks>
        <li><span class=ck>__CK__</span><span>[[check1]]</span></li>
        <li><span class=ck>__CK__</span><span>[[check2]]</span></li>
        <li><span class=ck>__CK__</span><span>[[check3]]</span></li>
        <li><span class=ck>__CK__</span><span>[[check4]]</span></li>
        <li><span class=ck>__CK__</span><span>[[check5]]</span></li>
        <li><span class=ck>__CK__</span><span>[[check6]]</span></li>
      </ul>
    </div>
  </section>

  <section class=paid>
    <h2>[[paid_heading]]</h2>
    <p class=sub>[[paid_sub]]</p>
    <div class=paidgrid>
      <div class=paiditem><div class=ptag>Coming soon</div><h3>[[paid1_t]]</h3><p>[[paid1_b]]</p></div>
      <div class=paiditem><div class=ptag>Coming soon</div><h3>[[paid2_t]]</h3><p>[[paid2_b]]</p></div>
    </div>
    <p class=paidnote>[[paid_note]]</p>
  </section>

  <section id=does class=does>
    <h2>[[feat_heading]]</h2>
    <p class=sub>[[feat_sub]]</p>
    <div class=feat>
      <div class=frow><h3><span class=ic>__I_EDIT__</span>[[feat1_t]]</h3><p>[[feat1_b]]</p></div>
      <div class=frow><h3><span class=ic>__I_SCAN__</span>[[feat2_t]]</h3><p>[[feat2_b]]</p></div>
      <div class=frow><h3><span class=ic>__I_ADD__</span>[[feat3_t]]</h3><p>[[feat3_b]]</p></div>
      <div class=frow><h3><span class=ic>__I_SIGN__</span>[[feat4_t]]</h3><p>[[feat4_b]]</p></div>
      <div class=frow><h3><span class=ic>__I_PAGE__</span>[[feat5_t]]</h3><p>[[feat5_b]]</p></div>
    </div>
  </section>

  <section class=final>
    <h2>[[final_heading]]</h2>
    <p class=sub>[[final_sub]]</p>
    <div class=cta>__MAC_BTN__ __WIN_BTN__</div>
    <p class=micro>[[final_micro]]</p>
  </section>

  <section class=donate>
    <div class=donatebox>
      <h2>[[donate_heading]]</h2>
      <p>[[donate_sub]]</p>
      <a class="btn dbtn" href="__DONATE__" target="_blank" rel="noopener noreferrer">[[donate_btn]]</a>
    </div>
  </section>

</div></main>

<footer><div class="wrap foot">
  <span>[[footer_note]]</span>
  <button class="navlink legalbtn" type=button data-legal=privacy>[[footer_legal]]</button>
</div></footer>

<div class=lmodal id=lmodal hidden>
  <div class=lcard role=dialog aria-modal=true aria-label="Privacy and Terms">
    <button class=lclose type=button data-legal-close aria-label="Close">&times;</button>
    <div class=ltabs>
      <button class="ltab on" type=button data-ltab=privacy>Privacy</button>
      <button class=ltab type=button data-ltab=terms>Terms</button>
    </div>
    <div class=lbody>
      <div class=ltabpanel data-lpanel=privacy>__PRIVACY__</div>
      <div class=ltabpanel hidden data-lpanel=terms>__TERMS__</div>
    </div>
  </div>
</div>
<script>
(function(){
  var m=document.getElementById('lmodal');
  function show(tab){
    document.querySelectorAll('[data-ltab]').forEach(function(t){t.classList.toggle('on',t.dataset.ltab===tab);});
    document.querySelectorAll('[data-lpanel]').forEach(function(p){p.hidden=(p.dataset.lpanel!==tab);});
    m.hidden=false;document.body.style.overflow='hidden';
  }
  function hide(){m.hidden=true;document.body.style.overflow='';}
  document.querySelectorAll('[data-legal]').forEach(function(b){
    b.addEventListener('click',function(){show(b.dataset.legal==='terms'?'terms':'privacy');});
  });
  document.querySelectorAll('[data-ltab]').forEach(function(t){
    t.addEventListener('click',function(){show(t.dataset.ltab);});
  });
  document.querySelector('[data-legal-close]').addEventListener('click',hide);
  m.addEventListener('click',function(e){if(e.target===m)hide();});
  document.addEventListener('keydown',function(e){if(e.key==='Escape'&&!m.hidden)hide();});
})();
</script>

<div class=dlmodal id=dlmodal hidden>
  <div class=dlcard role=dialog aria-modal=true aria-label="Agree and download">
    <button class=lclose type=button data-dl-close aria-label="Close">&times;</button>
    <h3 id=dltitle>Before you download</h3>
    <p class=dlintro id=dlintro>Add your email and agree to the terms. We link the agreement to your account so it stays on record.</p>
    <input class=dlfield type=email id=dlemail placeholder="you@example.com" autocomplete=email>
    <input class=dlfield type=password id=dlpw placeholder="Your password" autocomplete=current-password hidden>
    <input class=dlfield type=text inputmode=numeric id=dlcodein placeholder="6-digit code" autocomplete=one-time-code maxlength=6 hidden>
    <label class=dlcheck id=dlagreerow><input type=checkbox id=dlagree> <span>I have read and agree to the <button type=button class=inlinelink data-legal=terms>Terms</button> and <button type=button class=inlinelink data-legal=privacy>Privacy Policy</button>.</span></label>
    <button class="btn dlgo" type=button id=dlgo disabled>Agree &amp; continue</button>
    <a id=dlforgot href="/forgot" style="display:none;font-size:13px;margin-top:10px;color:var(--clay-press);text-decoration:none">Forgot your password?</a>
    <button type=button id=dlresend class=inlinelink style="display:none;margin-top:10px;font-size:13px;color:var(--clay-press)">Resend the code</button>
    <p class=dlerr id=dlerr hidden></p>
    <div id=dlcode hidden style="margin-top:6px"></div>
  </div>
</div>
<script>
(function(){
  var m=document.getElementById('dlmodal'),email=document.getElementById('dlemail'),
      pw=document.getElementById('dlpw'),agree=document.getElementById('dlagree'),
      go=document.getElementById('dlgo'),err=document.getElementById('dlerr'),
      title=document.getElementById('dltitle'),intro=document.getElementById('dlintro'),
      agreerow=document.getElementById('dlagreerow'),forgot=document.getElementById('dlforgot'),
      codebox=document.getElementById('dlcode'),codein=document.getElementById('dlcodein'),
      resend=document.getElementById('dlresend'),target=null,stage='consent';
  function emailok(){return /\S+@\S+\.\S+/.test(email.value.trim());}
  function valid(){if(stage==='code')return /^[0-9]{6}$/.test(codein.value.trim());
    return stage==='password'?pw.value.length>0:(agree.checked&&emailok());}
  function sync(){go.disabled=!valid();}
  email.addEventListener('input',sync);agree.addEventListener('change',sync);pw.addEventListener('input',sync);
  email.addEventListener('keydown',function(e){if(e.key==='Enter'&&valid())go.click();});
  pw.addEventListener('keydown',function(e){if(e.key==='Enter'&&valid())go.click();});
  codein.addEventListener('input',sync);
  codein.addEventListener('keydown',function(e){if(e.key==='Enter'&&valid())go.click();});
  resend.addEventListener('click',function(){resend.disabled=true;resend.textContent='Sent';
    jpost('/api/consent',{email:email.value.trim(),agreed:true}).catch(function(){});
    setTimeout(function(){resend.disabled=false;resend.textContent='Resend the code';},20000);});
  function resetCard(url){
    target=url;stage='consent';err.hidden=true;codebox.hidden=true;codebox.innerHTML='';
    title.textContent='Before you download';
    intro.textContent='Add your email and agree to the terms. We link the agreement to your account so it stays on record.';
    intro.style.display='';email.style.display='';email.disabled=false;
    pw.hidden=true;pw.value='';codein.hidden=true;codein.value='';resend.style.display='none';
    forgot.style.display='none';agreerow.style.display='';
    go.style.display='';go.textContent='Agree & continue';sync();
  }
  function open(url){resetCard(url);m.hidden=false;document.body.style.overflow='hidden';
    setTimeout(function(){email.focus();},40);}
  function close(){m.hidden=true;document.body.style.overflow='';}
  document.querySelectorAll('[data-dl]').forEach(function(b){
    b.addEventListener('click',function(){open(b.getAttribute('data-dl'));});});
  document.querySelector('[data-dl-close]').addEventListener('click',close);
  m.addEventListener('click',function(e){if(e.target===m)close();});
  document.addEventListener('keydown',function(e){if(e.key==='Escape'&&!m.hidden)close();});
  function download(){if(target){var f=document.createElement('iframe');f.style.display='none';f.src=target;document.body.appendChild(f);}}
  function fail(msg){go.disabled=false;err.textContent=msg;err.hidden=false;}
  function jpost(url,body,token){
    var h={'Content-Type':'application/json'};if(token)h['Authorization']='Bearer '+token;
    return fetch(url,{method:'POST',headers:h,body:JSON.stringify(body)}).then(function(r){
      return r.json().catch(function(){return {};}).then(function(j){if(!r.ok)throw new Error(j.detail||'Something went wrong.');return j;});});
  }
  function toPassword(){
    stage='password';agreerow.style.display='none';email.disabled=true;
    title.textContent='Welcome back';
    intro.textContent='This email already has an account. Enter your password to sign in and get your app code.';
    pw.hidden=false;forgot.style.display='inline-block';go.textContent='Sign in & download';
    err.hidden=true;sync();setTimeout(function(){pw.focus();},30);
  }
  function toCode(){
    stage='code';agreerow.style.display='none';email.disabled=true;pw.hidden=true;
    title.textContent='Check your email';
    intro.textContent='We emailed a 6-digit code to '+email.value.trim()+'. Enter it to download.';
    codein.hidden=false;resend.style.display='inline-block';go.textContent='Verify & download';
    err.hidden=true;sync();setTimeout(function(){codein.focus();},30);
  }
  function newUser(){
    download();title.textContent='Your download is starting';
    intro.innerHTML='No account uses this email yet. Open the app to create one, or <a href="/signup?email='+encodeURIComponent(email.value.trim())+'" style="color:var(--clay-press)">create it on the web</a>.';
    email.style.display='none';agreerow.style.display='none';go.style.display='none';
    codein.hidden=true;resend.style.display='none';err.hidden=true;
  }
  function showCode(code){
    download();title.textContent="You're signed in";intro.style.display='none';
    email.style.display='none';pw.hidden=true;forgot.style.display='none';go.style.display='none';
    codebox.innerHTML='<div style="font-size:12px;font-weight:700;letter-spacing:.05em;text-transform:uppercase;color:var(--clay-press);margin-bottom:8px">Your app sign-in code</div>'
      +'<div style="display:flex;gap:10px;align-items:center"><code id=dlcv style="font-family:ui-monospace,Menlo,monospace;font-size:22px;font-weight:700;letter-spacing:.08em;background:#fff;border:1px solid var(--line);border-radius:8px;padding:8px 12px;flex:1;text-align:center"></code><button type=button id=dlcp class=btn style="padding:10px 14px">Copy</button></div>'
      +'<div style="font-size:13px;color:var(--ink2);margin-top:10px">Open PDF Text Editor and enter this on the first screen. It expires in an hour.</div>';
    codebox.querySelector('#dlcv').textContent=code;codebox.hidden=false;
    document.getElementById('dlcp').addEventListener('click',function(){var b=this;try{navigator.clipboard.writeText(code);}catch(e){}b.textContent='Copied';setTimeout(function(){b.textContent='Copy';},1500);});
  }
  go.addEventListener('click',function(){
    if(!valid())return;
    if(stage==='consent'){
      go.disabled=true;go.textContent='One sec...';err.hidden=true;
      jpost('/api/consent',{email:email.value.trim(),agreed:true}).then(function(d){
        if(d&&d.has_account){toPassword();}else{toCode();}
      }).catch(function(e2){go.textContent='Agree & continue';fail(e2.message);});
    }else if(stage==='code'){
      go.disabled=true;go.textContent='Verifying...';err.hidden=true;
      jpost('/api/gate/verify',{email:email.value.trim(),code:codein.value.trim()}).then(function(){
        newUser();
      }).catch(function(e2){go.textContent='Verify & download';fail(e2.message);});
    }else{
      go.disabled=true;go.textContent='Signing in...';err.hidden=true;
      jpost('/api/auth/login',{email:email.value.trim(),password:pw.value}).then(function(d){
        try{localStorage.setItem('pdfte_token',d.token);}catch(e){}
        return jpost('/api/onboard/code',{},d.token);
      }).then(function(c){showCode(c.code);}).catch(function(e2){go.textContent='Sign in & download';fail(e2.message);});
    }
  });
})();
</script>
<script>
/* Motion: progressive enhancement. The page is fully visible without JS; reveal
   initial states only arm once <html> gets .motion-ready. The hero keeps its own
   rehearsed .rise entrance, so it is skipped here. */
(function(){
  var R=document.documentElement;
  var reduce=window.matchMedia&&window.matchMedia('(prefers-reduced-motion:reduce)').matches;
  var secs=document.querySelectorAll('main > .wrap > section');
  for(var i=0;i<secs.length;i++){
    var s=secs[i];
    if(s.classList.contains('hero'))continue;
    if(s.classList.contains('bandwrap')){               // drenched panel: eases up in scale
      var band=s.querySelector('.band');
      if(band)band.setAttribute('data-rv','scale');
      continue;
    }
    s.querySelectorAll(':scope > h2, :scope > .sub, :scope > .paidnote, :scope > .micro, :scope > .cta, :scope > .donatebox')
      .forEach(function(el){el.setAttribute('data-rv','up');});           // headers + closers slide up
    s.querySelectorAll(':scope .opengrid, :scope .catchgrid, :scope .paidgrid, :scope .feat')
      .forEach(function(el){el.setAttribute('data-rvs','');});            // grids stagger their children
  }
  R.classList.add('motion-ready');
  var hdr=document.querySelector('header'),ticking=false;
  function onScroll(){
    if(ticking)return;ticking=true;
    requestAnimationFrame(function(){
      if(hdr)hdr.classList.toggle('scrolled',window.scrollY>8);ticking=false;});
  }
  window.addEventListener('scroll',onScroll,{passive:true});onScroll();
  var items=[].slice.call(document.querySelectorAll('[data-rv],[data-rvs]'));
  if(reduce||!('IntersectionObserver'in window)){      // no-motion / no-IO: reveal everything now
    items.forEach(function(el){el.classList.add('rv-in');});return;
  }
  // Re-trigger: a section's text flies in every time you arrive at its page, and
  // re-arms once it has fully scrolled away (at ratio 0, so off-screen and unseen).
  var io=new IntersectionObserver(function(entries){
    entries.forEach(function(e){
      if(e.intersectionRatio>=.12){e.target.classList.add('rv-in');}
      else if(e.intersectionRatio<=.001){e.target.classList.remove('rv-in');}
    });
  },{threshold:[0,.12]});
  items.forEach(function(el){io.observe(el);});
})();
</script>
<script>/* anonymous page-view beacon (no PII; geo added server-side from CF) */
try{navigator.sendBeacon('/api/track',new Blob([JSON.stringify({path:location.pathname,ref:document.referrer})],{type:'application/json'}))}catch(e){}</script>
</body></html>"""

# small inline SVGs reused via token replacement (keeps the markup readable)
# The brand mark: the app icon as inline SVG (burnt-orange squircle, white page,
# grey lines, red edit caret) -- matches assets/icon_1024.png. Used as the header
# logo and, via /favicon.svg, the favicon.
_LOGO_SVG = (
    '<svg viewBox="0 0 1024 1024" xmlns="http://www.w3.org/2000/svg" '
    'role="img" aria-label="PDF for Free">'
    '<defs><linearGradient id="clay" x1="0" y1="0" x2="0" y2="1">'
    '<stop offset="0" stop-color="#C2643F"/>'
    '<stop offset="1" stop-color="#8B3E23"/></linearGradient></defs>'
    '<rect x="80" y="80" width="864" height="864" rx="200" fill="url(#clay)"/>'
    '<rect x="300" y="250" width="424" height="550" rx="34" fill="#fff"/>'
    '<rect x="370" y="380" width="284" height="34" rx="17" fill="#C8CCD2"/>'
    '<rect x="370" y="480" width="233" height="34" rx="17" fill="#C8CCD2"/>'
    '<rect x="370" y="580" width="261" height="34" rx="17" fill="#C8CCD2"/>'
    '<rect x="370" y="680" width="170" height="34" rx="17" fill="#C8CCD2"/>'
    '<rect x="561" y="658" width="10" height="64" fill="#E04040"/>'
    '<rect x="548" y="658" width="36" height="10" fill="#E04040"/>'
    '<rect x="548" y="712" width="36" height="10" fill="#E04040"/>'
    '</svg>')

# Branded password gate shown by the dev-site middleware. __LOGO__/__ERR__ are
# filled by _login_page(); CSS braces are literal (this is not an f-string).
_LOGIN_HTML = """<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>PDF for Free &middot; dev preview</title>
<meta name=robots content="noindex,nofollow">
<link rel=preconnect href="https://fonts.googleapis.com">
<link rel=preconnect href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Bricolage+Grotesque:opsz,wght@12..96,500..800&family=Hanken+Grotesk:wght@400;500;600;700&display=swap" rel=stylesheet>
<style>
:root{--paper:#FBF9F5;--panel:#F5F1EA;--canvas:#ECE6DC;--line:#E2DACD;
--ink:#2A2520;--ink2:#5C5346;--ink3:#897E6E;--clay:#C2643F;--clay-fill:#AA4E2C;--clay-press:#8B3E23;
--display:"Bricolage Grotesque",ui-sans-serif,system-ui,sans-serif;
--body:"Hanken Grotesk",ui-sans-serif,-apple-system,Segoe UI,Roboto,system-ui,sans-serif}
*{box-sizing:border-box}
body{margin:0;min-height:100vh;display:grid;place-items:center;padding:24px;
background:var(--paper);color:var(--ink);font-family:var(--body)}
.card{width:100%;max-width:380px;background:var(--panel);border:1px solid var(--line);
border-radius:20px;padding:36px 32px;text-align:center;
box-shadow:0 24px 64px -34px rgba(42,37,32,.45)}
.logo{width:62px;height:62px;margin:0 auto 18px}
.logo svg{width:100%;height:100%;display:block}
h1{font-family:var(--display);font-weight:700;font-size:23px;margin:0 0 6px;letter-spacing:-.02em}
.chan{display:inline-block;font-size:11px;font-weight:600;background:var(--canvas);
color:var(--ink2);padding:2px 9px;border-radius:999px;margin-left:4px;
vertical-align:middle;text-transform:uppercase;letter-spacing:.06em}
p.sub{color:var(--ink2);font-size:15px;margin:0 0 22px;line-height:1.5}
form{display:flex;flex-direction:column;gap:12px}
input{font-family:var(--body);font-size:16px;padding:13px 15px;border:1px solid var(--line);
border-radius:12px;background:var(--paper);color:var(--ink);width:100%}
input:focus{outline:none;border-color:var(--clay);box-shadow:0 0 0 3px rgba(194,100,63,.16)}
button{font-family:var(--body);font-weight:600;font-size:16px;padding:13px;border:0;
border-radius:12px;background:var(--clay-fill);color:#fff;cursor:pointer;transition:background .15s}
button:hover{background:var(--clay-press)}
.err{color:#B23A2E;font-size:14px;font-weight:500;margin:2px 0 0}
.foot{margin:20px 0 0;font-size:12.5px;color:var(--ink3);line-height:1.5}
</style></head>
<body><main class=card>
<div class=logo>__LOGO__</div>
<h1>PDF for Free <span class=chan>dev</span></h1>
<p class=sub>Development preview. Enter the password to continue.</p>
<form method=post action="/_gate/login" autocomplete=off>
<input type=password name=password placeholder="Password" aria-label="Password" autofocus required>
__ERR__
<button type=submit>Enter</button>
</form>
<p class=foot>Looking for the real thing? Visit <b>pdf-for-free.com</b></p>
</main></body></html>"""

_CK = '<svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M4 10.5l4 4 8-9"/></svg>'
_APPLE = ('<svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">'
          '<path d="M17.05 12.04c-.03-2.6 2.13-3.85 2.22-3.91-1.21-1.77-3.1-2.01'
          '-3.77-2.04-1.6-.16-3.13.94-3.94.94-.81 0-2.07-.92-3.4-.89-1.75.03'
          '-3.36 1.02-4.26 2.58-1.82 3.15-.47 7.81 1.3 10.37.86 1.25 1.89 2.66'
          ' 3.23 2.61 1.3-.05 1.79-.84 3.36-.84 1.57 0 2.01.84 3.39.81 1.4-.02'
          ' 2.29-1.28 3.15-2.54.99-1.45 1.4-2.86 1.42-2.93-.03-.01-2.73-1.05'
          '-2.76-4.16M14.5 4.7c.72-.87 1.2-2.08 1.07-3.28-1.03.04-2.28.69-3.02'
          ' 1.56-.66.77-1.24 2-1.08 3.18 1.15.09 2.32-.59 3.03-1.46"/></svg>')
def _ic(path):
    return ('<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" '
            'stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round">'
            + path + '</svg>')
_ICONS = {
    "__I_EDIT__": _ic('<path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4z"/>'),
    "__I_SCAN__": _ic('<path d="M3 7V5a2 2 0 0 1 2-2h2"/><path d="M17 3h2a2 2 0 0 1 2 2v2"/><path d="M21 17v2a2 2 0 0 1-2 2h-2"/><path d="M7 21H5a2 2 0 0 1-2-2v-2"/><path d="M7 12h10"/>'),
    "__I_ADD__": _ic('<rect x="3" y="3" width="18" height="18" rx="2"/><path d="M12 8v8"/><path d="M8 12h8"/>'),
    "__I_SIGN__": _ic('<path d="M3 17c3-1 4-9 7-9s2 6 4 6 2-3 4-3"/><path d="M3 21h18"/>'),
    "__I_PAGE__": _ic('<rect x="4" y="3" width="12" height="16" rx="2"/><path d="M8 21h10a2 2 0 0 0 2-2V8"/>'),
    "__I_UP__": _ic('<path d="M21 12a9 9 0 1 1-3-6.7"/><path d="M21 4v5h-5"/>'),
}


# ============================================================================
# PAGE COPY  --  every line of text on the landing page is below. The fastest
# way to change it is in the browser: in local dev each line shows a pencil,
# click to edit, then Save (which writes content.json next to this file). Commit
# content.json to publish. You can also just edit the strings here. Keep the
# keys on the left unchanged; the page maps [[key]] to each value.
# ============================================================================
_DEFAULT_COPY = {
    "nav1": "What free means",
    "nav2": "Features",
    "nav_legal": "Terms",
    "nav_donate": "Donate",
    "hero_pre": "The PDF editor that's",
    "hero_hl": "actually free",
    "q1": "Is it the best PDF editor?",
    "a1": "No, it is not.",
    "q2": "Did I make it because I graduated and my school took away my free Adobe Acrobat?",
    "a2": "Yes, I did.",
    "q3": "Will it work all the time?",
    "a3": "Probably not.",
    "q4": "Is it free?",
    "a4": ("Yes, 100%. (Cloud OCR and cloud storage are coming, and those will"
           " cost money. I'm not a charity giving away cloud compute.)"),
    "hero_micro": "Free and on-device.",
    "catch_heading": "Everyone says free. Then the asterisk shows up.",
    "catch_sub": ("You go to edit one PDF and hit a wall. Turns out the free part"
                  " was just opening it. The usual catches:"),
    "catch1_t": "Free to open, paid to edit",
    "catch1_b": ("Viewing and the odd comment are free. Changing the actual text"
                 " is behind a monthly subscription."),
    "catch2_t": "A watermark on your file",
    "catch2_b": "Your export gets stamped with their logo until you upgrade to a paid plan.",
    "catch3_t": "A few files a day",
    "catch3_b": "Then a daily limit, a countdown, and a checkout page.",
    "catch4_t": "Uploaded to their server",
    "catch4_b": ("The editing happens in their cloud, so your document leaves"
                 " your machine to do it."),
    "band_heading": "Here, free means free.",
    "band_sub": ("The app and 100% of the PDF editing are free, forever. The only"
                 " things that will ever cost money are the optional cloud"
                 " features we're building."),
    "check1": "The editor is free forever. No subscription, no trial.",
    "check2": "No account required. Sign-in is optional and free, no credit card.",
    "check3": "No watermark on anything you save.",
    "check4": "No cap on pages or file size.",
    "check5": "Your PDFs stay on your own computer, unless you turn on a cloud feature.",
    "check6": "No ads, no tracking, nothing harvested from your files.",
    "paid_heading": "Coming soon: two optional cloud add-ons.",
    "paid_sub": ("Everything above is free forever. We're building two optional"
                 " cloud extras; they will run in the cloud and cost real money"
                 " to run, so they will be paid. The app works fully without them,"
                 " today and after they launch."),
    "paid1_t": "Sharper OCR",
    "paid1_b": ("Cloud OCR (Google Document AI) will read messy or low-quality"
                " scans far better than the built-in on-device OCR. It costs money"
                " to run, so it will be pay-per-use. The free OCR stays free."),
    "paid2_t": "Cloud storage",
    "paid2_b": ("Keep your PDFs in sync so you can pick up on another device."
                " Storage and bandwidth cost money, so it will be a paid option."),
    "paid_note": ("No PDF editing is ever locked behind a paywall. You only pay"
                  " for cloud compute and storage that cannot run for free, and"
                  " only when you choose to use them."),
    "donate_heading": "Like it? You can chip in.",
    "donate_sub": ("The editor is free and always will be. If it saved you an"
                   " Adobe subscription, a small donation helps cover the cloud"
                   " bills and keeps it going. Totally optional."),
    "donate_btn": "Donate",
    "feat_heading": "It actually edits PDFs.",
    "feat_sub": ("Not a viewer with a comment tool bolted on. A real editor that"
                 " changes what is on the page."),
    "feat1_t": "Edit text in place",
    "feat1_b": ("Click any line and retype it. The replacement is set in the"
                " document's own font, so the page still looks like the original,"
                " not a patch."),
    "feat2_t": "Fix scanned PDFs",
    "feat2_b": "Built-in OCR turns a flat scan into real text you can select, search, and edit.",
    "feat3_t": "Add, move, resize, delete",
    "feat3_b": ("Drop a new text box anywhere, drag boxes to reposition, resize"
                " them, or remove them outright."),
    "feat4_t": "Annotate and sign",
    "feat4_b": "Highlight, mark up, and drop a signature where it belongs.",
    "feat5_t": "Work with pages",
    "feat5_b": "Reorder, rotate, split, and merge pages without leaving the app.",
    "final_heading": "Edit a PDF in the next two minutes.",
    "final_sub": "Download it, open a PDF, click a line of text. That is the whole setup.",
    "final_micro": "macOS now. Windows is on the way.",
    "footer_note": "PDF for Free · free and on-device",
    "footer_legal": "Privacy & Terms",
    "footer_link": "View the code on GitHub",
}

# The site copy: defaults, with content.json layered on top when present.
CONTENT_PATH = Path(__file__).parent / "content.json"


def _load_copy() -> dict:
    """Defaults, with any content.json values layered on top."""
    copy = dict(_DEFAULT_COPY)
    if CONTENT_PATH.exists():
        try:
            saved = json.loads(CONTENT_PATH.read_text("utf-8"))
            for k, v in saved.items():
                if k in _DEFAULT_COPY and isinstance(v, str):
                    copy[k] = v
        except (ValueError, OSError):
            pass
    return copy


# Legal copy shown in the Privacy / Terms modal. Plain-language and grounded in
# what the app actually does. Edit the wording here; the Contact sections point
# to the public contact form (no personal email or name in the repo). The lone
# exception is the DMCA designated agent in Terms sec. 16 -- a shared role inbox
# (dmca@, no personal name) that safe harbor requires be displayed publicly.
_PRIVACY_HTML = """
<h3>Privacy Policy</h3>
<p class=leff>Last updated July 13, 2026</p>
<p>PDF for Free is a desktop app that edits PDFs on your own computer, for macOS
and Windows. This policy explains, in plain language, what we collect, how we use
it, and your rights. It is written for users in the United States, including
California.</p>

<h4>Your PDFs never leave your device</h4>
<p>All PDF editing, rendering, and OCR happen locally on your own computer. Your
documents are never uploaded to us or anyone else. We do not see, receive, or
store the files you edit, the text in them, or your OCR results. Optional cloud
features (such as cloud OCR and cloud storage) are planned but not yet available;
if you choose to use one after it launches, only the specific data you send to
that feature would leave your device, and we will update this policy with the
details before that happens.</p>

<h4>What the app sends over the network</h4>
<p>The app talks to our servers only to: (a) check for and download updates;
(b) the first time you use OCR, download the optional OCR component; and (c) if
you choose to create or sign in to an account, send the email, password, and
display name you type. That is all. The app contains no analytics, telemetry,
crash reporting, advertising, device fingerprinting, or &ldquo;phone-home&rdquo; of
any kind, and an account is optional; the app is fully functional without
one.</p>

<h4>Information we collect</h4>
<p><b>Account (optional).</b> If you create one: your email address, a password
we store only as a secure hash, and a display name.</p>
<p><b>Download agreement.</b> When you agree to the Terms to download, we record
your email, that you verified it, the version of the Terms you agreed to, the date
and time, your IP address, and your browser's user-agent, so the agreement is on
record.</p>
<p><b>Email verification.</b> To confirm you own the address you enter, we email a
one-time 6-digit code through our email provider and check the code you type back.</p>
<p><b>Website analytics (anonymous).</b> On this website we record page views and
downloads, tied to a random cookie-based visitor id that is never linked to your
name or email, plus an approximate location (country, region, city) that our
network provider derives from your IP. This is used only to understand site
traffic in aggregate, never for advertising.</p>
<p><b>Server logs.</b> When you visit the site, download the app, or the app
checks for updates, our servers receive routine request information such as your
IP address and user-agent.</p>

<h4>How we use it</h4>
<p>To run optional accounts, to deliver downloads and updates, to keep a record
that you agreed to the Terms and verified your email, to understand website
traffic in aggregate, and to keep the Service secure and working. We do not use
your information for advertising, and we do not sell it.</p>

<h4>Cookies</h4>
<p>The website uses one first-party cookie to hold the anonymous analytics
visitor id described above, plus short-lived cookies needed to operate the
download flow. We do not use third-party advertising or cross-site tracking
cookies.</p>

<h4>Who we share it with</h4>
<p>We share information only with the service providers that make the Service
work, each only for its part:</p>
<ul>
<li><b>Railway</b>: hosting for our website, API, and database.</li>
<li><b>Cloudflare</b>: content delivery and security for the website, and
the approximate-location data used for analytics; <b>Cloudflare R2</b> stores and
serves the app installers and update files.</li>
<li><b>Resend</b>: sends the email-verification code and any account emails.</li>
<li><b>Google Fonts</b>: serves the fonts used on this website.</li>
<li><b>Google Forms</b>: hosts the contact form.</li>
</ul>
<p>We do not sell your personal information and do not share it for cross-context
behavioral advertising. Donations, if you make one, go through your own Venmo
account; no payment details pass through us.</p>

<h4>Data retention</h4>
<p>We keep account and download-agreement records for as long as your account or
the record is needed for our legal and operational purposes, then delete or
anonymize them. Email-verification codes are short-lived and single-use, and
analytics records are anonymous.</p>

<h4>Security</h4>
<p>We use reasonable measures to protect your information, including hashing
passwords, storing verification codes only as hashes, and serving the site and
APIs over HTTPS. No system is perfectly secure, so we cannot guarantee absolute
security.</p>

<h4>Your California privacy rights</h4>
<p>If you are a California resident, you have the right to know what personal
information we collect and how we use it, to request a copy, to ask us to correct
or delete it, and not to be discriminated against for exercising these rights.
Because we do not sell or share your personal information for cross-context
behavioral advertising, there is nothing to opt out of, but you may still contact
us to exercise these rights and we will verify and respond as required by law.</p>

<h4>Children</h4>
<p>The Service is intended for adults and is not directed to children. We do not
knowingly collect information from anyone under 18. If you believe a minor has
given us information, contact us and we will delete it.</p>

<h4>Changes</h4>
<p>We may update this policy; when we do, we will change the date above.
Significant changes will be made clear on this page.</p>

<h4>Contact</h4>
<p>To exercise your rights, request deletion, or ask any question about privacy,
use our <a href="https://docs.google.com/forms/d/e/1FAIpQLSeLS7dXUPF8zk9zkzXZjICMv-Nl1NLQogI7hLfu1NTKQcYVew/viewform" target="_blank" rel="noopener">contact form</a>.</p>
"""

_TERMS_HTML = """
<h3>Terms of Service</h3>
<p class=leff>Last updated July 13, 2026 (version 2026-07-13)</p>
<p>These Terms of Service ("Terms") are a binding agreement between you and the
individual developer of PDF for Free ("we", "us", or "the Developer"). They cover
the PDF for Free desktop application (for macOS and Windows) and this website
(together, the "Service"). By checking "I agree" at download, or by downloading,
installing, or using the Service, you accept these Terms and our Privacy Policy.
If you do not agree, do not download or use the Service.</p>

<h4>1. Eligibility</h4>
<p>You must be at least 18 years old and able to form a binding contract to use
the Service. If you use it on behalf of an organization, you represent that you
have authority to bind that organization to these Terms, and "you" includes that
organization.</p>

<h4>2. License</h4>
<p>We grant you a limited, personal, non-exclusive, non-transferable, revocable
license to download and use the Service for your own document editing. We retain
all right, title, and interest in the Service, including all intellectual
property in it. These Terms grant you no rights except the license stated here.</p>

<h4>3. Acceptable use</h4>
<p>You agree not to: (a) resell, sublicense, rent, or redistribute the Service;
(b) use it for anything unlawful, infringing, or harmful; (c) attempt to break,
overload, probe, or gain unauthorized access to the Service or its
infrastructure; (d) circumvent or tamper with any security or access controls;
(e) use the Service to build or train a competing product; or (f) remove or alter
any notices, or misrepresent the Service as your own.</p>

<h4>4. Your account (optional)</h4>
<p>You do not need an account to use the app; it is fully functional without one.
If you choose to create an account, you agree to provide accurate information, to
keep your password secure, and that you are responsible for activity under your
account. We may suspend or terminate accounts that violate these Terms, abuse the
Service, or create risk for us or other users.</p>

<h4>5. Cost</h4>
<p>The app and all of its PDF editing are free, and today there are no paid
features, subscriptions, or charges. We plan to add optional paid features (cloud OCR for tough scans, and cloud storage to
sync files across devices) because they cost us to run. When they launch they will be optional, the core app
and its editing will stay free, and we will show each feature's price and terms
before you use it. Donations are voluntary and go through your own Venmo account.</p>

<h4>6. Your files and content</h4>
<p>You keep all ownership of the documents you edit; we claim no ownership of
them. Today all editing, rendering, and OCR happen on your own device, and the
app never sends your files, their contents, or your OCR results to us. If we
launch an optional cloud feature and you choose to use it, you would grant us only
the limited permission needed to provide that feature (for example, sending a page
to cloud OCR, or storing a file you choose to sync). You are solely responsible
for your files, for having the right to use and edit them, and for keeping your
own backups.</p>

<h4>7. Third-party services</h4>
<p>The website and download service rely on third parties, including Railway
(hosting), Cloudflare and Cloudflare R2 (content delivery and file hosting),
Resend (sending verification email), Google Fonts, and Google Forms (the contact
form). Your use of those is also subject to those providers' terms, and we are
not responsible for their acts, omissions, or availability.</p>

<h4>8. Disclaimer of warranties</h4>
<p>THE SERVICE IS PROVIDED "AS IS" AND "AS AVAILABLE", WITH ALL FAULTS AND
WITHOUT WARRANTY OF ANY KIND. TO THE FULLEST EXTENT PERMITTED BY LAW, WE DISCLAIM
ALL WARRANTIES, EXPRESS OR IMPLIED, INCLUDING MERCHANTABILITY, FITNESS FOR A
PARTICULAR PURPOSE, TITLE, AND NON-INFRINGEMENT. WE DO NOT WARRANT THAT THE
SERVICE WILL BE UNINTERRUPTED, ERROR-FREE, SECURE, OR THAT IT WILL PRESERVE,
EDIT, OR RENDER YOUR DOCUMENTS CORRECTLY. YOU USE THE SERVICE AT YOUR OWN RISK
AND ARE RESPONSIBLE FOR KEEPING YOUR OWN BACKUPS.</p>

<h4>9. Limitation of liability</h4>
<p>TO THE FULLEST EXTENT PERMITTED BY LAW, WE WILL NOT BE LIABLE FOR ANY
INDIRECT, INCIDENTAL, SPECIAL, CONSEQUENTIAL, EXEMPLARY, OR PUNITIVE DAMAGES, OR
FOR ANY LOST PROFITS, DATA, OR DOCUMENTS, ARISING FROM OR RELATING TO THE
SERVICE, EVEN IF ADVISED OF THE POSSIBILITY. OUR TOTAL LIABILITY FOR ALL CLAIMS
WILL NOT EXCEED THE GREATER OF THE AMOUNT YOU PAID US IN THE 12 MONTHS BEFORE THE
CLAIM OR US$50. Some jurisdictions do not allow certain limitations, so parts of
this section may not apply to you.</p>

<h4>10. Indemnification</h4>
<p>You agree to indemnify, defend, and hold harmless the Developer from any
claims, damages, liabilities, and expenses (including reasonable legal fees)
arising from your use of the Service, your content, or your violation of these
Terms, the law, or any third party's rights.</p>

<h4>11. Termination</h4>
<p>You may stop using the Service at any time. We may suspend or end your access
at any time, with or without notice, including if you violate these Terms.
Sections that by their nature should survive (including 6, 8-10, 12, and 13) survive
termination.</p>

<h4>12. Dispute resolution; arbitration; class-action waiver</h4>
<p>Please read this carefully; it affects your rights. You and the Developer
agree to first try to resolve any dispute informally through our contact form. If that fails,
any dispute arising out of or relating to the Service or these Terms will be
resolved by binding individual arbitration administered by a recognized
arbitration provider under its consumer rules, seated in California, rather than
in court, except that either party may bring an individual claim in small-claims
court. TO THE EXTENT PERMITTED BY LAW, YOU AND THE DEVELOPER WAIVE ANY RIGHT TO A
JURY TRIAL AND ANY RIGHT TO BRING OR PARTICIPATE IN A CLASS, COLLECTIVE, OR
REPRESENTATIVE ACTION. You may opt out of this arbitration agreement by contacting
us through our contact form within 30 days of first accepting these Terms.</p>

<h4>13. Governing law</h4>
<p>These Terms are governed by the laws of the State of California, without
regard to its conflict-of-laws rules. For any matter not subject to arbitration,
you agree to the exclusive jurisdiction and venue of the state and federal courts
located in California.</p>

<h4>14. Changes to these Terms</h4>
<p>We may update these Terms as the project evolves. When we make material
changes we will update the date and version above, and continued use after that
means you accept the updated Terms. The download gate records which version you
agreed to.</p>

<h4>15. General</h4>
<p>These Terms and the Privacy Policy are the entire agreement between us about
the Service. If any provision is unenforceable, the rest stays in effect. Our
failure to enforce a provision is not a waiver. You may not assign these Terms;
we may. We are not liable for delays or failures caused by events beyond our
reasonable control.</p>

<h4>16. Copyright and DMCA notices</h4>
<p>We respect intellectual property rights and respond to clear notices of
alleged copyright infringement that comply with the Digital Millennium Copyright
Act (DMCA). Our agent designated to receive notifications of claimed infringement
is registered with the U.S. Copyright Office under registration number
DMCA-1075531, and can be reached directly at:</p>
<p><b>Designated agent for copyright notices:</b><br>
DMCA Agent<br>
18034 Ventura Blvd, Unit #655<br>
Encino, CA 91316, United States<br>
Phone: (818) 794-0599<br>
Email: <a href="mailto:dmca@hockeydatamodels.com">dmca@hockeydatamodels.com</a></p>
<p>To report material you believe infringes your copyright, send a written notice
to the designated agent above, or through our
<a href="https://docs.google.com/forms/d/e/1FAIpQLSeLS7dXUPF8zk9zkzXZjICMv-Nl1NLQogI7hLfu1NTKQcYVew/viewform" target="_blank" rel="noopener">contact form</a>
(choose &ldquo;Copyright or DMCA notice&rdquo;), that includes: (a) your physical or
electronic signature; (b) identification of the copyrighted work you claim has
been infringed; (c) identification of the material you claim is infringing and
information reasonably sufficient to let us locate it; (d) your contact
information; (e) a statement that you have a good-faith belief that the use is
not authorized by the copyright owner, its agent, or the law; and (f) a
statement, made under penalty of perjury, that the information in your notice is
accurate and that you are the copyright owner or are authorized to act on the
owner's behalf. A notice missing these elements may not be valid.</p>
<p>We may remove or disable access to material claimed to be infringing, and in
appropriate circumstances we will terminate the accounts of repeat infringers.</p>

<h4>17. Contact</h4>
<p>Questions about these Terms? Use our <a href="https://docs.google.com/forms/d/e/1FAIpQLSeLS7dXUPF8zk9zkzXZjICMv-Nl1NLQogI7hLfu1NTKQcYVew/viewform" target="_blank" rel="noopener">contact form</a>.</p>
"""


@app.get("/", response_class=HTMLResponse)
def home() -> str:
    info = (json.loads(RELEASE_INFO.read_text("utf-8"))
            if RELEASE_INFO.exists() else {})
    version = info.get("version")
    mac, win = info.get("mac"), info.get("windows")
    chan = "" if CHANNEL == "stable" else f'<span class="chan">{CHANNEL}</span>'
    # The Mac app (and so the quarantine command path) is named per channel.
    app_name = "PDF Text Editor (Dev)" if CHANNEL != "stable" else "PDF Text Editor"

    def btn(label: str, fname, icon=""):
        if not fname:
            return (f'<span class="btn btn-soon">{label} <em>&middot; soon</em>'
                    f'</span>')
        # Not a direct link: opens the download gate (agree to terms first),
        # which then sends the browser to /download/<file>.
        return (f'<button class="btn" type="button" '
                f'data-dl="/download/{fname}">{icon}{label}</button>')

    mac_btn = btn("Download for Mac", mac, _APPLE)
    win_btn = btn("Download for Windows", win)
    ver_line = (f"Version {version} &middot; <b>free, no catch</b>" if version
                else "First build on the way.")

    page = _PAGE
    for key, text in _load_copy().items():
        page = page.replace(f"[[{key}]]", html.escape(text, quote=False))
    page = (page
            .replace("__MAC_BTN__", mac_btn)
            .replace("__WIN_BTN__", win_btn)
            .replace("__VER__", ver_line)
            .replace("__CHAN__", chan)
            .replace("__APPNAME__", app_name)
            .replace("__GH__", GITHUB_URL)
            .replace("__DONATE__", DONATE_URL)
            .replace("__LOGO__", _LOGO_SVG)
            .replace("__PRIVACY__", _PRIVACY_HTML)
            .replace("__TERMS__", _TERMS_HTML)
            .replace("__CK__", _CK))
    for tok, svg in _ICONS.items():
        page = page.replace(tok, svg)
    # Drop paragraphs the copy left empty (e.g. a cleared tagline) so there are
    # no blank gaps.
    for empty in ("<p class=sub></p>", "<p class=micro></p>",
                  '<p class="micro rise d2"></p>'):
        page = page.replace(empty, "")
    return page


@app.get("/favicon.svg")
def favicon() -> Response:
    """The brand mark as an SVG favicon (same art as the app icon)."""
    return Response(_LOGO_SVG, media_type="image/svg+xml",
                    headers={"Cache-Control": "public, max-age=86400"})


@app.get("/og.png")
def og_image() -> FileResponse:
    """The social-share card (1200x630) for link previews."""
    return FileResponse(str(Path(__file__).parent / "og-image.png"),
                        media_type="image/png",
                        headers={"Cache-Control": "public, max-age=86400"})


def _asset(name: str, media: str) -> FileResponse:
    return FileResponse(str(Path(__file__).parent / name), media_type=media,
                        headers={"Cache-Control": "public, max-age=604800"})


# Raster favicons: Google Search + older clients need a PNG/ICO, not the SVG.
@app.get("/favicon.ico")
def favicon_ico() -> FileResponse:
    return _asset("favicon.ico", "image/x-icon")


@app.get("/favicon-96.png")
def favicon_96() -> FileResponse:
    return _asset("favicon-96.png", "image/png")


@app.get("/favicon-192.png")
def favicon_192() -> FileResponse:
    return _asset("favicon-192.png", "image/png")


@app.get("/apple-touch-icon.png")
def apple_touch_icon() -> FileResponse:
    return _asset("apple-touch-icon.png", "image/png")


@app.get("/robots.txt")
def robots() -> Response:
    body = ("User-agent: *\nAllow: /\n"
            f"Sitemap: {SITE_URL}/sitemap.xml\n")
    return Response(body, media_type="text/plain")


@app.get("/sitemap.xml")
def sitemap() -> Response:
    body = ('<?xml version="1.0" encoding="UTF-8"?>\n'
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
            f'  <url><loc>{SITE_URL}/</loc>'
            '<changefreq>weekly</changefreq><priority>1.0</priority></url>\n'
            '</urlset>\n')
    return Response(body, media_type="application/xml")


# --- release publish flow (release pipeline only) ---------------------------
def _check_publish_token(authorization: str) -> None:
    if not PUBLISH_TOKEN:
        raise HTTPException(503, "Publishing is not configured.")
    if not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "Missing publish token.")
    token = authorization.split(" ", 1)[1].strip()
    if token != PUBLISH_TOKEN:
        raise HTTPException(403, "Bad publish token.")


@app.get("/api/repo-state")
def repo_state(authorization: str = Header(default="")) -> Response:
    """Return the current tufup repo (updates/) as a tar.gz so the release
    pipeline can add an incremental bundle on top of it. Token-protected."""
    _check_publish_token(authorization)
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        if UPDATES_DIR.exists():
            tar.add(str(UPDATES_DIR), arcname="updates")
    buf.seek(0)
    return Response(buf.read(), media_type="application/gzip")


@app.post("/api/publish")
def publish(authorization: str = Header(default=""),
            bundle: UploadFile = File(...)) -> dict:
    """Receive a tar.gz whose root holds ``updates/`` and/or ``installers/`` and
    MERGE it onto the persistent volume. Per-platform publishes never wipe each
    other: a publish only adds or overwrites its own files. The release pipeline
    builds + signs the repo, then calls this. Token-protected."""
    _check_publish_token(authorization)
    ensure_dirs()
    # Stream extraction STRAIGHT from the spooled upload -- never read the whole
    # (multi-GB, OCR-inclusive) bundle into RAM. Loading it via .read() + BytesIO
    # ballooned memory ~6x and OOM-killed the memory-capped prod container on a
    # large release (the roomier dev box survived, which masked it). tarfile
    # decompresses the disk-backed UploadFile in a small streaming window.
    bundle.file.seek(0)
    with tarfile.open(fileobj=bundle.file, mode="r:gz") as tar:
        # filter='data' blocks path traversal; merge (no rmtree) leaves the other
        # platform's repo and the other installer intact.
        tar.extractall(str(DATA_DIR), filter="data")
    # Mirror the heavy bytes (installers + tufup targets) into R2 so users pull
    # them from there (free egress) instead of Railway. Best-effort: if R2 is off
    # or errors, the volume still serves everything, publish still succeeds.
    if r2.enabled():
        try:
            r2.sync_release(CHANNEL, str(INSTALLERS_DIR), str(UPDATES_DIR))
        except Exception as e:
            log.warning("R2 sync failed (serving release from volume): %s", e)
    return {"ok": True, "channel": CHANNEL}


@app.post("/api/admin/wipe-updates")
def wipe_updates(authorization: str = Header(default="")) -> dict:
    """DISABLED on purpose. Deleting the update tree removes the tufup/TUF
    metadata, which restarts the channel's metadata version counter at 1 -- BELOW
    the version installed clients have already cached. Every such client then
    rejects all later releases as a rollback attack and is bricked until its
    local cache is cleared. TUF metadata versions must only ever increase per
    channel, forever; this wipe broke updates exactly once already. To prune
    accumulated archives, do a normal signed republish that drops old targets,
    which still increments the version."""
    _check_publish_token(authorization)
    raise HTTPException(
        status_code=410,
        detail="wipe-updates is disabled: wiping the tufup metadata rolls the "
               "TUF version back to 1 and bricks installed clients. Prune via a "
               "signed republish instead.")
