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

from . import models, security
from .config import (CHANNEL, DATA_DIR, ENV_NAME, INSTALLERS_DIR, METADATA_DIR,
                     PUBLISH_TOKEN, RELEASE_INFO, TARGETS_DIR, UPDATES_DIR,
                     ensure_dirs)
from .db import Base, engine, get_db

ensure_dirs()  # before StaticFiles mounts below need the directories

app = FastAPI(title="PDF Text Editor backend", docs_url=None, redoc_url=None)


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


GITHUB_URL = "https://github.com/eddaboss/PDFTextEditor"

# The landing page. Plain string (not an f-string) so the CSS braces stay
# literal; the dynamic bits are injected by ``home()`` via ``.replace()`` on
# the __TOKENS__ below. Positioning: this editor is ACTUALLY free -- it runs on
# your own machine, so the usual "free" PDF-tool catches (pay-to-edit,
# watermarks, daily caps, upload-to-our-cloud) simply do not apply.
_PAGE = """<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>PDF Text Editor - the free PDF editor that's actually free</title>
<meta name=description content="A real PDF editor that runs on your Mac. Retype text in place in the document's own font. No subscription, no watermark, no upload. Actually free.">
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
.mark{width:24px;height:24px;border-radius:6px;background:var(--clay-fill);
  display:grid;place-items:center;color:#fff;font-size:13px;font-weight:800;
  font-family:var(--display);flex:none}
.tagpill{font-size:12px;font-weight:600;color:var(--clay-press);
  background:rgba(194,100,63,.12);padding:3px 9px;border-radius:999px;
  border:1px solid rgba(194,100,63,.22)}
.chan{font-size:11px;font-weight:600;background:var(--canvas);color:var(--ink2);
  padding:2px 8px;border-radius:6px}
.nav{display:flex;align-items:center;gap:22px;font-size:15px;font-weight:500}
.nav a{color:var(--ink2);text-decoration:none;transition:color .15s}
.nav a:hover{color:var(--clay-press)}
@media(max-width:680px){.nav .hidesm{display:none}}

/* buttons */
.btn{display:inline-flex;align-items:center;gap:9px;padding:14px 24px;border-radius:12px;
  background:var(--clay-fill);color:#fff;text-decoration:none;font-weight:600;
  font-size:16px;border:1px solid transparent;cursor:pointer;
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
.feat{display:grid;grid-template-columns:repeat(auto-fit,minmax(290px,1fr));
  gap:14px 40px;margin-top:44px}
.frow{padding:22px 0;border-top:1px solid var(--line)}
.frow h3{font-size:19px;font-weight:700;display:flex;align-items:center;gap:11px}
.frow .ic{width:30px;height:30px;border-radius:9px;background:rgba(194,100,63,.12);
  color:var(--clay-press);display:grid;place-items:center;flex:none}
.frow .ic svg{width:17px;height:17px}
.frow p{margin:10px 0 0;font-size:15.5px;color:var(--ink2)}

/* final cta */
.final{text-align:center;border-top:1px solid var(--line)}
.final h2{font-size:clamp(30px,4.2vw,52px)}
.final .sub{margin-inline:auto}
.final .cta{justify-content:center}

footer{border-top:1px solid var(--line);background:var(--panel)}
.foot{display:flex;align-items:center;justify-content:space-between;gap:18px;
  flex-wrap:wrap;padding:28px 0;font-size:14px;color:var(--ink3)}
.foot a{color:var(--ink2);text-decoration:none}.foot a:hover{color:var(--clay-press)}

/* entrance motion (enhances already-visible content) */
.rise{animation:rise .7s cubic-bezier(.16,1,.3,1) both}
.rise.d1{animation-delay:.06s}.rise.d2{animation-delay:.12s}
.rise.d3{animation-delay:.18s}.rise.d4{animation-delay:.24s}
@keyframes rise{from{opacity:0;transform:translateY(14px)}to{opacity:1;transform:none}}
@media(prefers-reduced-motion:reduce){
  .rise{animation:none}.caret{animation:none}html{scroll-behavior:auto}}
</style></head><body>

<header><div class="wrap bar">
  <span class=brand><span class=mark>P</span>PDF Text Editor __CHAN__</span>
  <nav class=nav>
    <a class=hidesm href="#free">[[nav1]]</a>
    <a class=hidesm href="#does">[[nav2]]</a>
    <a href="__GH__" rel="noopener">GitHub</a>
  </nav>
</div></header>

<main><div class=wrap>

  <section class=hero>
    <div>
      <h1 class="rise">[[hero_pre]] <span class=u>[[hero_hl]]</span>.</h1>
      <p class="lede rise d1">[[hero_sub]]</p>
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

  <section id=does class=does>
    <h2>[[feat_heading]]</h2>
    <p class=sub>[[feat_sub]]</p>
    <div class=feat>
      <div class=frow><h3><span class=ic>__I_EDIT__</span>[[feat1_t]]</h3><p>[[feat1_b]]</p></div>
      <div class=frow><h3><span class=ic>__I_SCAN__</span>[[feat2_t]]</h3><p>[[feat2_b]]</p></div>
      <div class=frow><h3><span class=ic>__I_ADD__</span>[[feat3_t]]</h3><p>[[feat3_b]]</p></div>
      <div class=frow><h3><span class=ic>__I_SIGN__</span>[[feat4_t]]</h3><p>[[feat4_b]]</p></div>
      <div class=frow><h3><span class=ic>__I_PAGE__</span>[[feat5_t]]</h3><p>[[feat5_b]]</p></div>
      <div class=frow><h3><span class=ic>__I_UP__</span>[[feat6_t]]</h3><p>[[feat6_b]]</p></div>
    </div>
  </section>

  <section class=final>
    <h2>[[final_heading]]</h2>
    <p class=sub>[[final_sub]]</p>
    <div class=cta>__MAC_BTN__ __WIN_BTN__</div>
    <p class=micro>[[final_micro]]</p>
  </section>

</div></main>

<footer><div class="wrap foot">
  <span>[[footer_note]]</span>
  <span><a href="__GH__" rel="noopener">[[footer_link]]</a></span>
</div></footer>
__EDITOR__
</body></html>"""

# small inline SVGs reused via token replacement (keeps the markup readable)
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
    "hero_pre": "The PDF editor that's",
    "hero_hl": "actually free",
    "hero_sub": ("Open a PDF and retype the text right where it sits, set in the"
                 " document's own font. It runs on your Mac, so the file never"
                 " leaves your computer. No subscription, no watermark, no"
                 " sign-up."),
    "hero_micro": "Free and on-device. The app updates itself after you install it.",
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
    "band_heading": "Here, free just means free.",
    "band_sub": ("No tier, no upsell, no fine print. Everything the app does, it"
                 " does for everyone, at no cost."),
    "check1": "No subscription and no trial that quietly ends.",
    "check2": "No account or credit card to download and use it.",
    "check3": "No watermark on anything you save.",
    "check4": "No cap on pages or file size.",
    "check5": "Nothing uploaded. Your PDF stays on your computer.",
    "check6": "The source is public on GitHub. Read every line.",
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
    "feat6_t": "Stays current",
    "feat6_b": "It updates itself after you install it, so you are always on the latest build.",
    "final_heading": "Edit a PDF in the next two minutes.",
    "final_sub": "Download it, open a PDF, click a line of text. That is the whole setup.",
    "final_micro": "macOS now. Windows is on the way.",
    "footer_note": "PDF Text Editor · free and on-device",
    "footer_link": "View the code on GitHub",
}

# A Save in the browser writes content.json next to this file, so the copy you
# write lives in the repo and you can commit + push it.
CONTENT_PATH = Path(__file__).parent / "content.json"
# Inline editing is ON in local dev and OFF on the deployed site: no edit UI and
# no working save endpoint there, so a visitor can never rewrite the live page.
EDIT_ENABLED = (os.environ.get("PDFTE_EDIT",
                               "1" if ENV_NAME == "local" else "0") == "1")


def _load_copy() -> dict:
    """Defaults, with any saved content.json values layered on top."""
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


# The in-browser editor, injected ONLY in edit mode (see EDIT_ENABLED). Click a
# line to edit it; Save POSTs every line to /api/content, which writes the file.
_EDITOR_HTML = r"""
<style>
[data-edit]{outline:1.5px dashed transparent;outline-offset:3px;border-radius:4px;
  transition:outline-color .12s,background .12s;cursor:text}
[data-edit]:hover{outline-color:rgba(194,100,63,.55);background:rgba(194,100,63,.07)}
[data-edit]:focus{outline:2px solid #C2643F;outline-offset:3px;background:#fff}
#edbar{position:fixed;left:50%;bottom:22px;transform:translateX(-50%);z-index:9999;
  display:flex;align-items:center;gap:14px;background:#2A2520;color:#F5F1EA;
  padding:11px 12px 11px 18px;border-radius:14px;box-shadow:0 14px 40px rgba(0,0,0,.32);
  font:14px/1.3 -apple-system,system-ui,sans-serif;max-width:92vw}
#edbar b{font-weight:700;color:#fff}
#edsave{background:#AA4E2C;color:#fff;border:0;padding:9px 16px;border-radius:9px;
  font:600 14px/1 inherit;cursor:pointer;transition:background .15s;white-space:nowrap}
#edsave:hover:not(:disabled){background:#C2643F}
#edsave:disabled{opacity:.45;cursor:default}
#edhint{position:fixed;left:50%;top:74px;transform:translateX(-50%);z-index:9999;
  background:#6B311F;color:#fff;padding:8px 16px;border-radius:999px;
  font:500 13px/1 -apple-system,system-ui,sans-serif;box-shadow:0 8px 24px rgba(0,0,0,.2);
  animation:edfade 5s forwards}
@keyframes edfade{0%,80%{opacity:1}100%{opacity:0;visibility:hidden}}
</style>
<div id=edhint>Click any line of text to edit it</div>
<div id=edbar><span id=edstatus><b>Editing locally.</b> Changes save to content.json.</span>
<button id=edsave disabled>Save changes</button></div>
<script>
(function(){
  var els=[].slice.call(document.querySelectorAll('[data-edit]'));
  var save=document.getElementById('edsave'),status=document.getElementById('edstatus'),dirty=false;
  function markDirty(){dirty=true;save.disabled=false;status.innerHTML='<b>Unsaved changes.</b>';}
  els.forEach(function(el){
    el.setAttribute('contenteditable','plaintext-only');
    el.addEventListener('input',markDirty);
    el.addEventListener('keydown',function(e){
      if(e.key==='Enter'&&el.tagName!=='P'){e.preventDefault();el.blur();}
      if(e.key==='Escape'){el.blur();}
    });
  });
  save.addEventListener('click',function(){
    var data={};els.forEach(function(el){data[el.getAttribute('data-edit')]=el.innerText.replace(/\s+/g,' ').trim();});
    save.disabled=true;status.textContent='Saving...';
    fetch('/api/content',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)})
      .then(function(r){if(!r.ok)throw new Error('HTTP '+r.status);return r.json();})
      .then(function(){dirty=false;status.innerHTML='<b>Saved.</b> Commit content.json to publish.';})
      .catch(function(err){save.disabled=false;status.textContent='Save failed: '+err.message;});
  });
  window.addEventListener('beforeunload',function(e){if(dirty){e.preventDefault();e.returnValue='';}});
})();
</script>"""


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
        return f'<a class="btn" href="/download/{fname}">{icon}{label}</a>'

    mac_btn = btn("Download for Mac", mac, _APPLE)
    win_btn = btn("Download for Windows", win)
    ver_line = (f"Version {version} &middot; <b>free, no catch</b>" if version
                else "First build on the way.")

    page = _PAGE
    for key, text in _load_copy().items():
        safe = html.escape(text, quote=False)
        if EDIT_ENABLED:
            safe = f'<span data-edit="{key}">{safe}</span>'
        page = page.replace(f"[[{key}]]", safe)
    page = (page
            .replace("__MAC_BTN__", mac_btn)
            .replace("__WIN_BTN__", win_btn)
            .replace("__VER__", ver_line)
            .replace("__CHAN__", chan)
            .replace("__GH__", GITHUB_URL)
            .replace("__CK__", _CK)
            .replace("__EDITOR__", _EDITOR_HTML if EDIT_ENABLED else ""))
    for tok, svg in _ICONS.items():
        page = page.replace(tok, svg)
    return page


@app.get("/api/content")
def get_content() -> dict:
    """The current page copy (defaults + content.json). Used by the editor."""
    return _load_copy()


@app.post("/api/content")
async def set_content(request: Request) -> dict:
    """Save edited copy to content.json. LOCAL-ONLY: disabled on the deployed
    site so the public page cannot be rewritten by a visitor."""
    if not EDIT_ENABLED:
        raise HTTPException(403, "Editing is disabled on this server.")
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(400, "Expected a JSON object of {key: text}.")
    copy = _load_copy()
    for k, v in body.items():
        if k in _DEFAULT_COPY and isinstance(v, str):
            copy[k] = v[:2000]
    CONTENT_PATH.write_text(json.dumps(copy, indent=2, ensure_ascii=False),
                            "utf-8")
    return {"ok": True, "saved": len(copy)}


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
