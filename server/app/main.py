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
import html
import io
import json
import os
import shutil
import tarfile
import tempfile
from pathlib import Path

from fastapi import (Depends, FastAPI, File, Header, HTTPException, Request,
                     UploadFile)
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import accounts, models, security
from .config import (CHANNEL, DATA_DIR, ENV_NAME, INSTALLERS_DIR, METADATA_DIR,
                     PUBLISH_TOKEN, RELEASE_INFO, TARGETS_DIR, UPDATES_DIR,
                     ensure_dirs)
from .db import Base, engine, get_db

ensure_dirs()  # before StaticFiles mounts below need the directories

app = FastAPI(title="PDF Text Editor backend", docs_url=None, redoc_url=None)

# The account system (email verification, password reset, password change, a
# brute-force throttle, optional CORS, and the account pages) lives in
# accounts.py and wires itself in with this one call.
accounts.install(app)


@app.on_event("startup")
def _startup() -> None:
    ensure_dirs()
    Base.metadata.create_all(engine)


# --- tufup update repository (read by the desktop self-updater) -------------
# The TUF client fetches signed metadata then the archive/patch targets. These
# are plain static files produced by the release pipeline.
# The whole per-platform update tree: /updates/<mac|win>/{metadata,targets}.
# The desktop client points at its platform's subtree (see appconfig).
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
def download(filename: str) -> FileResponse:
    # basename only: no path traversal can escape the installers dir.
    safe = os.path.basename(filename)
    path = INSTALLERS_DIR / safe
    if safe != filename or not path.is_file():
        raise HTTPException(404, "Not found.")
    return FileResponse(str(path), filename=safe,
                        media_type="application/octet-stream")


# --- download-gate consent -------------------------------------------------
class ConsentIn(BaseModel):
    email: EmailStr
    agreed: bool


@app.post("/api/consent")
def record_consent(body: ConsentIn, request: Request,
                   db: Session = Depends(get_db)) -> dict:
    """Record that someone agreed to the Terms + Privacy before downloading.
    Keyed by email and linked to an account if one already exists with that
    email (otherwise linked later, at register). Returns the installer file
    names so the client can start the download."""
    if not body.agreed:
        raise HTTPException(400, "You must agree to the Terms to download.")
    email = body.email.lower().strip()
    # X-Forwarded-For is set by Railway's proxy; take the first hop.
    fwd = request.headers.get("x-forwarded-for", "")
    ip = (fwd.split(",")[0].strip() if fwd
          else (request.client.host if request.client else ""))[:64]
    account = db.scalar(select(models.User).where(models.User.email == email))
    db.add(models.Consent(
        email=email,
        account_id=account.id if account else None,
        terms_version=TERMS_VERSION,
        ip=ip,
        user_agent=request.headers.get("user-agent", "")[:1000],
    ))
    # A setup code the desktop app can redeem to pre-fill this email (see
    # accounts.claim_setup_code). The page shows it on the destination it lands on.
    setup_code = accounts.issue_setup_code(db, email)
    db.commit()
    info = (json.loads(RELEASE_INFO.read_text("utf-8"))
            if RELEASE_INFO.exists() else {})
    # has_account lets the page route an agreeing visitor to sign-in (an account
    # already uses this email) or create-account (it does not), both pre-filled.
    return {"ok": True, "terms_version": TERMS_VERSION,
            "has_account": account is not None, "setup_code": setup_code,
            "mac": info.get("mac"), "windows": info.get("windows")}


GITHUB_URL = "https://github.com/eddaboss/PDFTextEditor"
SITE_URL = "https://pdf-for-free.com"  # the canonical public domain
# Bump when the Terms / Privacy change materially: the download gate records
# which version each person agreed to, so a bump means new agreements going
# forward (and you can tell who agreed to what).
TERMS_VERSION = "2026-06-15"
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
<title>PDF Text Editor - the free PDF editor that's actually free</title>
<link rel="icon" type="image/svg+xml" href="/favicon.svg">
<link rel="mask-icon" href="/favicon.svg" color="#C2643F">
<meta name="theme-color" content="#C2643F">
<meta name=description content="A real PDF editor that runs on your Mac. Retype text in place in the document's own font. No subscription, no watermark, no upload. Actually free.">
<meta name=robots content="index,follow">
<link rel=canonical href="https://pdf-for-free.com/">
<meta property="og:type" content="website">
<meta property="og:site_name" content="PDF for Free">
<meta property="og:title" content="PDF for Free - the PDF editor that's actually free">
<meta property="og:description" content="A real PDF editor that runs on your Mac. Retype text in place in the document's own font. No subscription, no watermark, no upload.">
<meta property="og:url" content="https://pdf-for-free.com/">
<meta property="og:image" content="https://pdf-for-free.com/og.png">
<meta property="og:image:width" content="1200">
<meta property="og:image:height" content="630">
<meta name="twitter:card" content="summary_large_image">
<meta name="twitter:title" content="PDF for Free - the PDF editor that's actually free">
<meta name="twitter:description" content="A real PDF editor that runs on your Mac. No subscription, no watermark, no upload.">
<meta name="twitter:image" content="https://pdf-for-free.com/og.png">
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
@media(max-width:860px){.hero{grid-template-columns:1fr;padding-top:48px}
  .lede{max-width:46ch} .heroart{order:-1}}

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
.editln{position:relative;height:26px;display:flex;align-items:center;margin:3px 0}
.editbox{position:absolute;inset:-5px -8px;border:2px solid var(--clay);border-radius:5px;
  background:rgba(194,100,63,.05)}
.editln .txt{font-family:var(--body);font-weight:600;font-size:15px;color:var(--ink);
  position:relative;z-index:1}
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
  font:inherit;font-size:15px;background:#fff;color:var(--ink)}
.dlfield:focus{outline:2px solid var(--clay);outline-offset:1px;border-color:var(--clay)}
.dlcheck{display:flex;gap:10px;align-items:flex-start;margin:16px 0 20px;
  font-size:14px;color:var(--ink2);line-height:1.5;cursor:pointer}
.dlcheck input{margin-top:3px;flex:none;width:17px;height:17px;accent-color:var(--clay-fill)}
.inlinelink{font:inherit;background:none;border:0;padding:0;cursor:pointer;
  color:var(--clay-press);font-weight:600;text-decoration:underline}
.dlgo{width:100%;justify-content:center}
.dlgo:disabled{opacity:.5;cursor:default;transform:none;box-shadow:none}
.dlerr{margin:12px 0 0;font-size:13.5px;color:#b3402a;font-weight:500}

/* entrance motion (enhances already-visible content) */
.rise{animation:rise .7s cubic-bezier(.16,1,.3,1) both}
.rise.d1{animation-delay:.06s}.rise.d2{animation-delay:.12s}
.rise.d3{animation-delay:.18s}.rise.d4{animation-delay:.24s}
@keyframes rise{from{opacity:0;transform:translateY(14px)}to{opacity:1;transform:none}}
@media(prefers-reduced-motion:reduce){
  .rise{animation:none}.caret{animation:none}html{scroll-behavior:auto}}
</style></head><body>

<header><div class="wrap bar">
  <span class=brand><span class=mark>__LOGO__</span>PDF Text Editor __CHAN__</span>
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
      <div class=paiditem><div class=ptag>Pay per use</div><h3>[[paid1_t]]</h3><p>[[paid1_b]]</p></div>
      <div class=paiditem><div class=ptag>Optional</div><h3>[[paid2_t]]</h3><p>[[paid2_b]]</p></div>
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
    <h3>Before you download</h3>
    <p class=dlintro>Add your email and agree to the terms. We link the agreement to your account so it stays on record.</p>
    <input class=dlfield type=email id=dlemail placeholder="you@example.com" autocomplete=email>
    <label class=dlcheck><input type=checkbox id=dlagree> <span>I have read and agree to the <button type=button class=inlinelink data-legal=terms>Terms</button> and <button type=button class=inlinelink data-legal=privacy>Privacy Policy</button>.</span></label>
    <button class="btn dlgo" type=button id=dlgo disabled>Agree &amp; download</button>
    <p class=dlerr id=dlerr hidden></p>
  </div>
</div>
<script>
(function(){
  var m=document.getElementById('dlmodal'),email=document.getElementById('dlemail'),
      agree=document.getElementById('dlagree'),go=document.getElementById('dlgo'),
      err=document.getElementById('dlerr'),target=null;
  function valid(){return agree.checked && /\S+@\S+\.\S+/.test(email.value.trim());}
  function sync(){go.disabled=!valid();}
  email.addEventListener('input',sync);agree.addEventListener('change',sync);
  email.addEventListener('keydown',function(e){if(e.key==='Enter'&&valid())go.click();});
  function open(url){target=url;err.hidden=true;sync();m.hidden=false;
    document.body.style.overflow='hidden';setTimeout(function(){email.focus();},40);}
  function close(){m.hidden=true;document.body.style.overflow='';}
  document.querySelectorAll('[data-dl]').forEach(function(b){
    b.addEventListener('click',function(){open(b.getAttribute('data-dl'));});});
  document.querySelector('[data-dl-close]').addEventListener('click',close);
  m.addEventListener('click',function(e){if(e.target===m)close();});
  document.addEventListener('keydown',function(e){if(e.key==='Escape'&&!m.hidden)close();});
  go.addEventListener('click',function(){
    if(!valid())return;go.disabled=true;go.textContent='One sec...';err.hidden=true;
    fetch('/api/consent',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({email:email.value.trim(),agreed:true})})
      .then(function(r){if(!r.ok)return r.json().then(function(j){throw new Error(j.detail||'Could not record agreement');});return r.json();})
      .then(function(d){
        var addr=email.value.trim();
        // Carry the setup code to the page we land on, where it is shown for the app.
        if(d&&d.setup_code){try{sessionStorage.setItem('pdfte_setup_code',d.setup_code);}catch(e){}}
        // Start the real download without leaving the page: an attachment loaded
        // in a hidden frame downloads while we send the page on to the account.
        if(target){var f=document.createElement('iframe');f.style.display='none';f.src=target;document.body.appendChild(f);}
        // Existing account -> sign in; otherwise create one. Email pre-filled either way.
        var dest=(d&&d.has_account)?'/login':'/signup';
        go.textContent='Downloading...';
        setTimeout(function(){window.location=dest+'?email='+encodeURIComponent(addr)+'&from=download';},700);
      })
      .catch(function(e2){go.disabled=false;go.textContent='Agree & download';err.textContent=e2.message;err.hidden=false;});
  });
})();
</script>
</body></html>"""

# small inline SVGs reused via token replacement (keeps the markup readable)
# The brand mark: the app icon as inline SVG (burnt-orange squircle, white page,
# grey lines, red edit caret) -- matches assets/icon_1024.png. Used as the header
# logo and, via /favicon.svg, the favicon.
_LOGO_SVG = (
    '<svg viewBox="0 0 1024 1024" xmlns="http://www.w3.org/2000/svg" '
    'role="img" aria-label="PDF Text Editor">'
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
    "a4": ("Yes, 100%. (Unless you want better OCR or cloud storage. I'm not a"
           " charity giving that away for free.)"),
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
                 " things you ever pay for are cloud features that cost real"
                 " money to run."),
    "check1": "The editor is free forever. No subscription, no trial.",
    "check2": "A free account to sign in, and no credit card.",
    "check3": "No watermark on anything you save.",
    "check4": "No cap on pages or file size.",
    "check5": "Your PDFs stay on your Mac, unless you turn on a cloud feature.",
    "check6": "No ads, no tracking, nothing harvested from your files.",
    "paid_heading": "Two things cost money, and only if you want them.",
    "paid_sub": ("Everything above is free forever. These two run in the cloud"
                 " and cost real money every time, so they are paid. The app"
                 " works fully without either one."),
    "paid1_t": "Sharper OCR",
    "paid1_b": ("Cloud OCR (Google Document AI) reads messy or low-quality scans"
                " far better than the built-in on-device OCR. Each page costs"
                " money to run, so it is pay-per-use. The free OCR stays free."),
    "paid2_t": "Cloud storage",
    "paid2_b": ("Keep your PDFs in sync so you can pick up on another device."
                " Storage and bandwidth cost money, so it is a paid option."),
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
    "footer_note": "PDF Text Editor · free and on-device",
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
# what the app actually does. Edit the wording here; set edw.luko@gmail.com
# (and, if you want one, a governing-law line) before going live.
_PRIVACY_HTML = """
<h3>Privacy Policy</h3>
<p class=leff>Last updated June 15, 2026</p>
<p>PDF for Free is a desktop app that edits PDFs on your own computer. This
policy explains what information we collect, how we use it, and your rights, in
plain language. It is written for users in the United States, including
California.</p>

<h4>Your PDFs stay on your device</h4>
<p>All PDF editing happens locally on your Mac. Your documents are not uploaded
to us to be edited, and we never see, read, or store the files you edit, unless
you choose to use one of the optional cloud features described below.</p>

<h4>Information we collect</h4>
<p><b>Account.</b> Your email address, a securely hashed password, and the
display name you choose.</p>
<p><b>Agreement records.</b> When you agree to the Terms at download, we record
your email, the version of the Terms you agreed to, the date and time, your IP
address, and your browser's user-agent, so the agreement is on record and can be
linked to your account.</p>
<p><b>Server logs.</b> When you download the app or it checks for updates, our
servers receive routine request information such as your IP address.</p>
<p><b>Cloud features (only if you use them).</b> The page images you send to
cloud OCR, and the files you choose to sync with cloud storage.</p>
<p><b>Payments.</b> Handled by our payment processor and by Venmo for donations.
We never receive or store your full card details.</p>

<h4>How we use it</h4>
<p>To sign you in and run your account, to deliver downloads and updates, to keep
a record that you accepted the Terms, to provide any cloud feature you turn on,
to take payment for paid features, and to keep the Service secure and working. We
do not use your information for advertising.</p>

<h4>Optional cloud features</h4>
<p><b>Cloud OCR.</b> If you turn it on, the pages you run through it are sent to
Google Document AI to be read into text, then returned to you. Use it only on
documents you are comfortable processing in the cloud. The built-in on-device
OCR never leaves your computer.</p>
<p><b>Cloud storage.</b> If you turn it on, the PDFs you choose to sync are
stored on our servers so you can open them on another device. You can delete them
at any time. Both cloud features are off by default and entirely optional.</p>

<h4>How we share it</h4>
<p>We share information only with the service providers that make the Service
work: our hosting provider, our payment processor, Google (cloud OCR), and Venmo
(donations), each only as needed for their part. We do not sell your personal
information, and we do not share it for cross-context behavioral advertising.</p>

<h4>Data retention</h4>
<p>We keep account and agreement records for as long as your account exists and
as needed for our legal and operational purposes, then delete or anonymize them.
Cloud-storage files are kept until you delete them or close your account.</p>

<h4>Security</h4>
<p>We use reasonable measures to protect your information, including hashing
passwords and serving the site and APIs over HTTPS. No system is perfectly
secure, so we cannot guarantee absolute security.</p>

<h4>Your California privacy rights</h4>
<p>If you are a California resident, you have the right to know what personal
information we collect and how we use it, to request a copy, to ask us to correct
or delete it, and to not be discriminated against for exercising these rights.
Because we do not sell or share your personal information for cross-context
behavioral advertising, there is nothing to opt out of, but you may still
contact us to exercise any of these rights. Email us and we will verify and
respond as required by law.</p>

<h4>Children</h4>
<p>The Service is not directed to children under 13, and we do not knowingly
collect their information. If you believe a child has given us information,
contact us and we will delete it.</p>

<h4>Changes</h4>
<p>We may update this policy; when we do, we will change the date above.
Significant changes will be made clear on this page.</p>

<h4>Contact</h4>
<p>To exercise your rights, request deletion, or ask any question about privacy,
email edw.luko@gmail.com.</p>
"""

_TERMS_HTML = """
<h3>Terms of Service</h3>
<p class=leff>Last updated June 15, 2026 (version 2026-06-15)</p>
<p>These Terms of Service ("Terms") are a binding agreement between you and the
individual developer of PDF for Free ("we", "us", or "the Developer"). They
cover the PDF for Free desktop application, this website, and any related cloud
services (together, the "Service"). By checking "I agree" at download, or by
downloading, installing, or using the Service, you accept these Terms and our
Privacy Policy. If you do not agree, do not download or use the Service.</p>

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

<h4>3. What you may not do</h4>
<p>You agree not to: (a) resell, sublicense, rent, or redistribute the Service;
(b) use it for anything unlawful, infringing, or harmful; (c) upload to a cloud
feature any content you do not have the right to process; (d) attempt to break,
overload, probe, or gain unauthorized access to the Service or its
infrastructure; (e) circumvent or tamper with billing, usage limits, or
security; (f) use the Service to build or train a competing product; or (g)
remove or alter any notices, or misrepresent the Service as your own.</p>

<h4>4. Your account</h4>
<p>Using the app requires a free account. You agree to provide accurate
information, to keep your password secure, and that you are responsible for all
activity under your account. We may suspend or terminate accounts that violate
these Terms, abuse the Service, or create risk for us or other users.</p>

<h4>5. Free and paid features</h4>
<p>The app and all PDF editing are free and will remain free. Cloud OCR (page
processing via Google Document AI) and cloud storage are optional paid features,
because they cost us money to run. Prices and limits are shown before you use a
paid feature and may change on a going-forward basis.</p>

<h4>6. Billing, auto-renewal, and cancellation</h4>
<p>Paid features are billed through our third-party payment processor; we do not
store your full card details. Pay-per-use charges (such as cloud OCR) are
incurred as you use them. If a feature is offered as a recurring subscription
(such as cloud storage), it will, consistent with California's Automatic
Renewal Law, automatically renew at the stated price and interval until you
cancel, and you may cancel at any time from your account settings or by emailing
us, effective at the end of the current billing period.</p>

<h4>7. Refunds</h4>
<p>Except where required by law, payments are non-refundable, including used
pay-per-use charges and the current period of any subscription. If the law in
your jurisdiction gives you a refund or cancellation right, that right applies.</p>

<h4>8. Your files and content</h4>
<p>You keep all ownership of the documents you edit. We claim no ownership of
them. You grant us only the limited permission needed to provide a feature you
choose to use (for example, transmitting a page to cloud OCR, or storing a file
you choose to sync). The app's local editing never sends your files to us. You
are solely responsible for your files and for having the right to use and edit
them.</p>

<h4>9. Third-party services</h4>
<p>The Service relies on third parties, including Google Document AI (cloud
OCR), our hosting provider, our payment processor, and Venmo (donations). Your
use of those features is also subject to those providers' terms, and we are not
responsible for their acts, omissions, or availability.</p>

<h4>10. Disclaimer of warranties</h4>
<p>THE SERVICE IS PROVIDED "AS IS" AND "AS AVAILABLE", WITH ALL FAULTS AND
WITHOUT WARRANTY OF ANY KIND. TO THE FULLEST EXTENT PERMITTED BY LAW, WE DISCLAIM
ALL WARRANTIES, EXPRESS OR IMPLIED, INCLUDING MERCHANTABILITY, FITNESS FOR A
PARTICULAR PURPOSE, TITLE, AND NON-INFRINGEMENT. WE DO NOT WARRANT THAT THE
SERVICE WILL BE UNINTERRUPTED, ERROR-FREE, SECURE, OR THAT IT WILL PRESERVE,
EDIT, OR RENDER YOUR DOCUMENTS CORRECTLY. YOU USE THE SERVICE AT YOUR OWN RISK
AND ARE RESPONSIBLE FOR KEEPING YOUR OWN BACKUPS.</p>

<h4>11. Limitation of liability</h4>
<p>TO THE FULLEST EXTENT PERMITTED BY LAW, WE WILL NOT BE LIABLE FOR ANY
INDIRECT, INCIDENTAL, SPECIAL, CONSEQUENTIAL, EXEMPLARY, OR PUNITIVE DAMAGES, OR
FOR ANY LOST PROFITS, DATA, OR DOCUMENTS, ARISING FROM OR RELATING TO THE
SERVICE, EVEN IF ADVISED OF THE POSSIBILITY. OUR TOTAL LIABILITY FOR ALL CLAIMS
WILL NOT EXCEED THE GREATER OF THE AMOUNT YOU PAID US IN THE 12 MONTHS BEFORE THE
CLAIM OR US$50. Some jurisdictions do not allow certain limitations, so parts of
this section may not apply to you.</p>

<h4>12. Indemnification</h4>
<p>You agree to indemnify, defend, and hold harmless the Developer from any
claims, damages, liabilities, and expenses (including reasonable legal fees)
arising from your use of the Service, your content, or your violation of these
Terms, the law, or any third party's rights.</p>

<h4>13. Termination</h4>
<p>You may stop using the Service at any time. We may suspend or end your access
at any time, with or without notice, including if you violate these Terms.
Sections that by their nature should survive (including 8-12, 14, and 15) survive
termination.</p>

<h4>14. Dispute resolution; arbitration; class-action waiver</h4>
<p>Please read this carefully; it affects your rights. You and the Developer
agree to first try to resolve any dispute informally by email. If that fails,
any dispute arising out of or relating to the Service or these Terms will be
resolved by binding individual arbitration administered by a recognized
arbitration provider under its consumer rules, seated in California, rather than
in court, except that either party may bring an individual claim in small-claims
court. TO THE EXTENT PERMITTED BY LAW, YOU AND THE DEVELOPER WAIVE ANY RIGHT TO A
JURY TRIAL AND ANY RIGHT TO BRING OR PARTICIPATE IN A CLASS, COLLECTIVE, OR
REPRESENTATIVE ACTION. You may opt out of this arbitration agreement by emailing
us within 30 days of first accepting these Terms.</p>

<h4>15. Governing law</h4>
<p>These Terms are governed by the laws of the State of California, without
regard to its conflict-of-laws rules. For any matter not subject to arbitration,
you agree to the exclusive jurisdiction and venue of the state and federal courts
located in California.</p>

<h4>16. Changes to these Terms</h4>
<p>We may update these Terms as the project evolves. When we make material
changes we will update the date and version above, and continued use after that
means you accept the updated Terms. The download gate records which version you
agreed to.</p>

<h4>17. General</h4>
<p>These Terms and the Privacy Policy are the entire agreement between us about
the Service. If any provision is unenforceable, the rest stays in effect. Our
failure to enforce a provision is not a waiver. You may not assign these Terms;
we may. We are not liable for delays or failures caused by events beyond our
reasonable control.</p>

<h4>18. Contact</h4>
<p>Questions about these Terms? Email edw.luko@gmail.com.</p>
"""


@app.get("/", response_class=HTMLResponse)
def home() -> str:
    info = (json.loads(RELEASE_INFO.read_text("utf-8"))
            if RELEASE_INFO.exists() else {})
    version = info.get("version")
    mac, win = info.get("mac"), info.get("windows")
    chan = "" if CHANNEL == "stable" else f'<span class="chan">{CHANNEL}</span>'

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
    raw = bundle.file.read()
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as tar:
        # filter='data' blocks path traversal; merge (no rmtree) leaves the other
        # platform's repo and the other installer intact.
        tar.extractall(str(DATA_DIR), filter="data")
    return {"ok": True, "channel": CHANNEL}
