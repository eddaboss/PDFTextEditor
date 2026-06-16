"""The account system: email verification, password reset, password change, a
brute-force throttle, optional CORS, and the web pages that drive all of it.

Everything new lives here so the core ``main.py`` only gains a single
``accounts.install(app)`` call. The account pages are served by this same app, so
the browser fetches them at the same origin and no CORS is needed; CORS is wired
only if ``CORS_ORIGINS`` is set, for the case where the landing page is ever
hosted on a different host than the API.

Tokens for the emailed links are single-use and stored only as a hash (see
``security.new_link_token``). The throttle counts recent requests per client IP
in the ``rate_events`` table so it holds across restarts and workers.
"""
import datetime
import logging
from datetime import timedelta

from fastapi import (APIRouter, BackgroundTasks, Depends, Header, HTTPException,
                     Request)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from . import config, emailer, models, security
from .account_models import (PURPOSE_RESET, PURPOSE_VERIFY, EmailToken,
                             OnboardCode, RateEvent, run_migrations, utcnow)
from .db import SessionLocal, engine, get_db

log = logging.getLogger("pdfte.accounts")
router = APIRouter()


# --- auth dependency (kept local to avoid importing from main) --------------
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


# --- link building ----------------------------------------------------------
def _base_url(request: Request | None) -> str:
    """The absolute origin for emailed links: the configured public URL if set,
    else the current request's own base. Empty only when neither is available
    (e.g. a server-initiated send with PUBLIC_BASE_URL unset)."""
    if config.PUBLIC_BASE_URL:
        return config.PUBLIC_BASE_URL
    if request is not None:
        return str(request.base_url).rstrip("/")
    return ""


def _verify_link(raw: str, request: Request | None) -> str:
    base = _base_url(request)
    return f"{base}/verify?token={raw}" if base else ""


def _reset_link(raw: str, request: Request | None) -> str:
    base = _base_url(request)
    return f"{base}/reset?token={raw}" if base else ""


# --- single-use token helpers ----------------------------------------------
def _replace_token(db: Session, user: "models.User", purpose: str,
                   ttl_hours: int) -> str:
    """Drop any outstanding unused token of this purpose for the user, mint a new
    one, and return the raw value (the part that goes in the link)."""
    db.execute(delete(EmailToken).where(
        EmailToken.user_id == user.id,
        EmailToken.purpose == purpose,
        EmailToken.used_at.is_(None)))
    raw, token_hash = security.new_link_token()
    db.add(EmailToken(user_id=user.id, purpose=purpose, token_hash=token_hash,
                      expires_at=utcnow() + timedelta(hours=ttl_hours)))
    db.commit()
    return raw


def _lookup_token(db: Session, raw: str, purpose: str):
    if not raw:
        return None
    tok = db.scalar(select(EmailToken).where(
        EmailToken.token_hash == security.hash_link_token(raw),
        EmailToken.purpose == purpose))
    if not tok or tok.used_at is not None or tok.expires_at < utcnow():
        return None
    return tok


def _consume_token(db: Session, raw: str, purpose: str):
    """Look the token up, mark it used, and return its user. None if the token is
    missing, already used, or expired."""
    tok = _lookup_token(db, raw, purpose)
    if not tok:
        return None
    user = db.get(models.User, tok.user_id)
    if not user:
        return None
    tok.used_at = utcnow()
    db.commit()
    return user


def issue_and_send_verification(db: Session, user: "models.User",
                                request: Request | None = None) -> bool:
    """Mint a verify token and email the link. Returns False (without raising) if
    no absolute base URL is available to build the link."""
    raw = _replace_token(db, user, PURPOSE_VERIFY, config.VERIFY_TOKEN_TTL_HOURS)
    link = _verify_link(raw, request)
    if not link:
        log.warning("verification not sent: set PDFTE_PUBLIC_URL so links are "
                    "absolute. user_id=%s", user.id)
        return False
    return emailer.send_verification_email(user.email, link)


def _mark_verified(db: Session, user: "models.User") -> None:
    if not user.email_verified:
        user.email_verified = True
        user.email_verified_at = datetime.datetime.now(datetime.timezone.utc)
        db.commit()


# --- setup codes (download gate -> desktop app email pre-fill) ---------------
def issue_setup_code(db: Session, email: str) -> str:
    """Mint a fresh setup code for an email and add it to the session (the caller
    commits). Drops any earlier unused code for the same email. Returns the
    display code to show the user; only the hash is stored."""
    db.execute(delete(OnboardCode).where(
        OnboardCode.email == email, OnboardCode.used_at.is_(None)))
    display, code_hash = security.new_setup_code()
    db.add(OnboardCode(
        email=email, code_hash=code_hash,
        expires_at=utcnow() + timedelta(hours=config.ONBOARD_CODE_TTL_HOURS)))
    return display


# --- brute-force throttle (per client IP, DB-backed) ------------------------
def _client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _consume_rate(action: str, key: str, limit: int, window_minutes: int) -> bool:
    """Record one hit and report whether the client is still under the limit.
    Prunes this bucket's expired rows as it goes so the table stays small."""
    bucket = f"{action}:{key}"
    cutoff = utcnow() - timedelta(minutes=window_minutes)
    db = SessionLocal()
    try:
        db.execute(delete(RateEvent).where(
            RateEvent.bucket == bucket, RateEvent.created_at < cutoff))
        recent = db.scalar(select(func.count()).select_from(RateEvent).where(
            RateEvent.bucket == bucket)) or 0
        if recent >= limit:
            db.commit()
            return False
        db.add(RateEvent(bucket=bucket))
        db.commit()
        return True
    finally:
        db.close()


# (method, path) -> (action, limit, window minutes). Only these paths are
# throttled; every other request skips the check via a plain dict miss.
def _throttle_rules() -> dict:
    return {
        ("POST", "/api/auth/login"):
            ("login", config.LOGIN_MAX_ATTEMPTS, config.LOGIN_WINDOW_MINUTES),
        ("POST", "/api/auth/register"):
            ("register", config.REGISTER_MAX_ATTEMPTS,
             config.REGISTER_WINDOW_MINUTES),
        ("POST", "/api/auth/password/forgot"):
            ("reset", config.RESET_MAX_REQUESTS, config.RESET_WINDOW_MINUTES),
        ("POST", "/api/onboard/claim"):
            ("onboard", config.ONBOARD_MAX_ATTEMPTS, config.ONBOARD_WINDOW_MINUTES),
    }


async def _throttle_mw(request: Request, call_next):
    rule = _throttle_rules().get((request.method, request.url.path))
    if rule:
        action, limit, window = rule
        ok = await run_in_threadpool(
            _consume_rate, action, _client_ip(request), limit, window)
        if not ok:
            return JSONResponse(
                {"detail": "Too many attempts. Please wait a few minutes and "
                           "try again."}, status_code=429)
    return await call_next(request)


# --- JSON API ---------------------------------------------------------------
class ForgotIn(BaseModel):
    email: EmailStr


class ResetIn(BaseModel):
    token: str
    password: str = Field(min_length=8, max_length=200)


class ChangePasswordIn(BaseModel):
    current_password: str
    new_password: str = Field(min_length=8, max_length=200)


class ClaimCodeIn(BaseModel):
    code: str = Field(min_length=4, max_length=32)


@router.get("/api/account")
def account_info(user: "models.User" = Depends(current_user)) -> dict:
    return {
        "id": user.id,
        "email": user.email,
        "display_name": user.display_name,
        "email_verified": bool(user.email_verified),
        "created_at": user.created_at.isoformat() if user.created_at else None,
    }


@router.post("/api/onboard/claim")
def claim_setup_code(body: ClaimCodeIn, db: Session = Depends(get_db)) -> dict:
    """Redeem a setup code (shown at the download gate) for the email it carries,
    so the desktop app can pre-fill it. Single-use; the claim path is throttled
    so the short code space cannot be swept to harvest addresses."""
    rec = db.scalar(select(OnboardCode).where(
        OnboardCode.code_hash == security.hash_setup_code(body.code)))
    if not rec or rec.used_at is not None or rec.expires_at < utcnow():
        raise HTTPException(404, "That setup code is invalid or has expired.")
    rec.used_at = utcnow()
    db.commit()
    return {"email": rec.email}


@router.post("/api/auth/verify/send")
def resend_verification(request: Request,
                        user: "models.User" = Depends(current_user),
                        db: Session = Depends(get_db)) -> dict:
    if user.email_verified:
        return {"ok": True, "already_verified": True}
    sent = issue_and_send_verification(db, user, request)
    return {"ok": True, "sent": sent}


@router.post("/api/auth/password/forgot")
def forgot_password(body: ForgotIn, request: Request,
                    background: BackgroundTasks,
                    db: Session = Depends(get_db)) -> dict:
    """Always answers the same way whether or not the email is on file, so the
    endpoint cannot be used to discover who has an account. The token mint and
    email send run in a background task (after the response), so a hit and a miss
    return in the same time and cannot be told apart by response latency."""
    email = body.email.lower().strip()
    user = db.scalar(select(models.User).where(models.User.email == email))
    if user:
        background.add_task(_send_password_reset, user.id, _base_url(request))
    return {"ok": True}


def _send_password_reset(user_id: int, base_url: str) -> None:
    """Off-request work for forgot_password: mint a single-use reset token and
    email the link, on its own session since the request's session is closed."""
    db = SessionLocal()
    try:
        user = db.get(models.User, user_id)
        if not user:
            return
        raw = _replace_token(db, user, PURPOSE_RESET,
                             config.RESET_TOKEN_TTL_HOURS)
        link = f"{base_url}/reset?token={raw}" if base_url else ""
        if link:
            emailer.send_reset_email(user.email, link)
    finally:
        db.close()


@router.post("/api/auth/password/reset")
def reset_password(body: ResetIn, db: Session = Depends(get_db)) -> dict:
    user = _consume_token(db, body.token, PURPOSE_RESET)
    if not user:
        raise HTTPException(400, "This reset link is invalid or has expired.")
    user.password_hash = security.hash_password(body.password)
    # Following the emailed link proves control of the address, so confirm it too.
    _mark_verified(db, user)
    # Retire any other outstanding reset links for this account.
    db.execute(delete(EmailToken).where(
        EmailToken.user_id == user.id,
        EmailToken.purpose == PURPOSE_RESET,
        EmailToken.used_at.is_(None)))
    db.commit()
    return {"ok": True}


@router.post("/api/auth/password/change")
def change_password(body: ChangePasswordIn,
                    user: "models.User" = Depends(current_user),
                    db: Session = Depends(get_db)) -> dict:
    if not security.verify_password(body.current_password, user.password_hash):
        raise HTTPException(403, "Your current password is incorrect.")
    user.password_hash = security.hash_password(body.new_password)
    db.commit()
    return {"ok": True}


# --- web pages --------------------------------------------------------------
@router.get("/signup", response_class=HTMLResponse)
def signup_page() -> str:
    return _page("Create your account", _SIGNUP,
                 "Optional and free. You never need an account to use the editor.")


@router.get("/login", response_class=HTMLResponse)
def login_page() -> str:
    return _page("Sign in", _LOGIN, "Welcome back.")


@router.get("/forgot", response_class=HTMLResponse)
def forgot_page() -> str:
    return _page("Reset your password", _FORGOT,
                 "Enter your email and we will send a reset link.")


@router.get("/reset", response_class=HTMLResponse)
def reset_page(token: str = "", db: Session = Depends(get_db)) -> str:
    if _lookup_token(db, token, PURPOSE_RESET) is None:
        return _page("Reset link expired", _RESET_EXPIRED)
    return _page("Choose a new password", _RESET_FORM)


@router.get("/verify", response_class=HTMLResponse)
def verify_page(token: str = "", db: Session = Depends(get_db)) -> str:
    user = _consume_token(db, token, PURPOSE_VERIFY)
    if not user:
        return _page("Confirmation link expired", _VERIFY_FAIL)
    _mark_verified(db, user)
    return _page("Email confirmed", _VERIFY_OK)


@router.get("/account", response_class=HTMLResponse)
def account_page() -> str:
    return _page("Your account", _ACCOUNT)


# --- wiring -----------------------------------------------------------------
def install(app) -> None:
    """One call from main.py that adds optional CORS, the throttle middleware,
    the account routes, and the boot-time schema backfill."""
    if config.CORS_ORIGINS:
        app.add_middleware(
            CORSMiddleware, allow_origins=config.CORS_ORIGINS,
            allow_methods=["GET", "POST", "OPTIONS"],
            allow_headers=["Authorization", "Content-Type"],
            allow_credentials=False)
    app.middleware("http")(_throttle_mw)
    app.include_router(router)

    @app.on_event("startup")
    def _accounts_startup() -> None:
        run_migrations(engine)


# ============================================================================
# HTML  --  small, on-brand pages in the landing page's clay/paper palette.
# Dynamic values come from the JSON API client-side, so no server-side string
# interpolation touches these (nothing to escape, no injection surface).
# ============================================================================
_CSS = """
:root{--paper:#FBF9F5;--panel:#F5F1EA;--canvas:#ECE6DC;--line:#E2DACD;
--ink:#2A2520;--ink2:#5C5346;--ink3:#897E6E;--clay:#C2643F;--clay-fill:#AA4E2C;
--clay-press:#8B3E23;
--display:"Bricolage Grotesque",ui-sans-serif,system-ui,sans-serif;
--body:"Hanken Grotesk",ui-sans-serif,-apple-system,Segoe UI,Roboto,system-ui,sans-serif}
*{box-sizing:border-box}
body{margin:0;background:var(--canvas);color:var(--ink);font-family:var(--body);
font-size:16px;line-height:1.55;-webkit-font-smoothing:antialiased;min-height:100vh}
a{color:var(--clay-press)}
header{background:color-mix(in srgb,var(--paper) 90%,transparent);
border-bottom:1px solid var(--line);position:sticky;top:0;backdrop-filter:blur(8px)}
.bar{width:min(960px,92vw);margin:0 auto;display:flex;align-items:center;
justify-content:space-between;height:58px}
.brand{display:flex;align-items:center;gap:9px;font-family:var(--display);
font-weight:700;font-size:17px;letter-spacing:-.02em;color:var(--ink);text-decoration:none}
.mark{width:24px;height:24px;border-radius:6px;background:var(--clay-fill);
display:grid;place-items:center;color:#fff;font-size:13px;font-weight:800;
font-family:var(--display)}
.home{color:var(--ink2);text-decoration:none;font-weight:500;font-size:15px}
.home:hover{color:var(--clay-press)}
main.card{width:min(420px,92vw);margin:48px auto;background:var(--paper);
border:1px solid var(--line);border-radius:18px;padding:34px 32px;
box-shadow:0 24px 50px -28px rgba(58,40,26,.32)}
h1{font-family:var(--display);font-weight:700;letter-spacing:-.02em;
font-size:26px;line-height:1.12;margin:0 0 8px}
.sub{color:var(--ink2);font-size:15px;margin:0 0 22px}
form{display:flex;flex-direction:column;gap:15px;margin:0}
label{display:flex;flex-direction:column;gap:6px;font-size:14px;font-weight:600;
color:var(--ink2)}
input{font:inherit;font-weight:500;color:var(--ink);background:#fff;
border:1px solid var(--line);border-radius:10px;padding:11px 13px;outline:none;
transition:border-color .15s,box-shadow .15s}
input:focus{border-color:var(--clay);box-shadow:0 0 0 3px rgba(194,100,63,.16)}
button{font:inherit;font-weight:600;font-size:15.5px;cursor:pointer;
background:var(--clay-fill);color:#fff;border:0;border-radius:11px;padding:13px 18px;
transition:background .15s,transform .1s}
button:hover{background:var(--clay-press)}
button:active{transform:translateY(1px)}
button:disabled{opacity:.5;cursor:default}
button.ghost{background:transparent;color:var(--ink);border:1px solid var(--line)}
button.ghost:hover{background:var(--panel)}
.msg{font-size:14px;margin:16px 0 0;min-height:1.2em;line-height:1.45}
.msg.err{color:#9B2C1A}.msg.ok{color:#3F6B43}
.alt{font-size:14px;color:var(--ink3);margin:18px 0 0;text-align:center}
.alt a{font-weight:600;text-decoration:none}.alt a:hover{text-decoration:underline}
.kv{font-size:14.5px;color:var(--ink2);margin:8px 0}.kv b{color:var(--ink);font-weight:600}
.badge{display:inline-block;font-size:12px;font-weight:700;padding:2px 9px;
border-radius:999px;margin-left:4px}
.badge.no{background:rgba(155,44,26,.12);color:#9B2C1A}
.badge.yes{background:rgba(63,107,67,.14);color:#3F6B43}
.center{text-align:center}
.big{width:54px;height:54px;border-radius:15px;display:grid;place-items:center;
margin:6px auto 16px;font-size:27px;font-weight:800}
.big.ok{background:rgba(63,107,67,.14);color:#3F6B43}
.big.no{background:rgba(155,44,26,.12);color:#9B2C1A}
hr.div{border:none;border-top:1px solid var(--line);margin:22px 0}
.codebox{background:rgba(194,100,63,.08);border:1px solid rgba(194,100,63,.28);
border-radius:12px;padding:14px 16px;margin:0 0 22px}
.codettl{font-size:11.5px;font-weight:700;letter-spacing:.05em;
text-transform:uppercase;color:var(--clay-press);margin-bottom:9px}
.coderow{display:flex;align-items:center;gap:10px}
.codeval{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:20px;
font-weight:700;letter-spacing:.08em;color:var(--ink);background:#fff;
border:1px solid var(--line);border-radius:8px;padding:8px 12px;flex:1;text-align:center}
.codecopy{background:var(--clay-fill);color:#fff;border:0;border-radius:8px;
padding:9px 15px;font:inherit;font-weight:600;font-size:14px;cursor:pointer}
.codecopy:hover{background:var(--clay-press)}
.codehint{font-size:13px;color:var(--ink2);margin-top:10px}
@media(prefers-reduced-motion:reduce){*{transition:none!important}}
"""

_JS = """
const TOKEN_KEY='pdfte_token';
function getToken(){try{return localStorage.getItem(TOKEN_KEY)||''}catch(e){return ''}}
function setToken(t){try{localStorage.setItem(TOKEN_KEY,t)}catch(e){}}
function clearToken(){try{localStorage.removeItem(TOKEN_KEY)}catch(e){}}
async function api(path,opts){
  opts=opts||{};opts.headers=opts.headers||{};
  if(opts.body&&typeof opts.body!=='string'){opts.headers['Content-Type']='application/json';opts.body=JSON.stringify(opts.body);}
  if(opts.auth){var t=getToken();if(t)opts.headers['Authorization']='Bearer '+t;}
  const r=await fetch(path,opts);
  let data=null;try{data=await r.json()}catch(e){}
  if(!r.ok){var d=(data&&typeof data.detail==='string')?data.detail:('Something went wrong ('+r.status+').');throw new Error(d);}
  return data;
}
function show(el,text,kind){el.textContent=text;el.className='msg'+(kind?' '+kind:'');}
function renderSetupCode(){
  var box=document.getElementById('codebox');if(!box)return;
  var code;try{code=sessionStorage.getItem('pdfte_setup_code');}catch(e){}
  if(!code)return;
  box.innerHTML='<div class=codettl>Desktop app setup code</div>'
    +'<div class=coderow><code class=codeval></code><button type=button class=codecopy>Copy</button></div>'
    +'<div class=codehint>Open PDF Text Editor and paste this in the account panel to fill in your email.</div>';
  box.querySelector('.codeval').textContent=code;
  box.hidden=false;
  box.querySelector('.codecopy').addEventListener('click',function(){
    var b=this;try{navigator.clipboard.writeText(code);}catch(e){}
    b.textContent='Copied';setTimeout(function(){b.textContent='Copy';},1500);
  });
}
"""

_SIGNUP = """
<div id=codebox class=codebox hidden></div>
<form id=f autocomplete=on>
  <label>Email<input id=email type=email required autocomplete=email></label>
  <label>Name <span style="font-weight:500;color:var(--ink3)">(optional)</span><input id=name autocomplete=name></label>
  <label>Password<input id=pw type=password required minlength=8 autocomplete=new-password placeholder="At least 8 characters"></label>
  <button id=go type=submit>Create account</button>
</form>
<p class=msg id=msg></p>
<p class=alt>Already have an account? <a href="/login">Sign in</a></p>
<script>
(function(){
  var f=document.getElementById('f'),msg=document.getElementById('msg'),go=document.getElementById('go');
  var q=new URLSearchParams(location.search);
  if(q.get('email')){document.getElementById('email').value=q.get('email');document.getElementById('name').focus();}
  if(q.get('from')==='download'){show(msg,'Your download is starting. Create your account to finish setting up.','ok');}
  renderSetupCode();
  f.addEventListener('submit',async function(e){
    e.preventDefault();go.disabled=true;show(msg,'Creating your account...','');
    var email=document.getElementById('email').value.trim();
    try{
      var data=await api('/api/auth/register',{method:'POST',body:{email:email,password:document.getElementById('pw').value,display_name:document.getElementById('name').value.trim()}});
      setToken(data.token);
      try{await api('/api/auth/verify/send',{method:'POST',auth:true});}catch(e){}
      show(msg,'Account created. We sent a confirmation link to '+email+'. Taking you to your account...','ok');
      setTimeout(function(){location.href='/account'},1500);
    }catch(err){go.disabled=false;show(msg,err.message,'err');}
  });
})();
</script>
"""

_LOGIN = """
<div id=codebox class=codebox hidden></div>
<form id=f>
  <label>Email<input id=email type=email required autocomplete=email></label>
  <label>Password<input id=pw type=password required autocomplete=current-password></label>
  <button id=go type=submit>Sign in</button>
</form>
<p class=msg id=msg></p>
<p class=alt><a href="/forgot">Forgot your password?</a></p>
<p class=alt>New here? <a href="/signup">Create an account</a></p>
<script>
(function(){
  var f=document.getElementById('f'),msg=document.getElementById('msg'),go=document.getElementById('go');
  var q=new URLSearchParams(location.search);
  if(q.get('email')){document.getElementById('email').value=q.get('email');document.getElementById('pw').focus();}
  if(q.get('from')==='download'){show(msg,'Your download is starting. Sign in to finish setting up.','ok');}
  renderSetupCode();
  f.addEventListener('submit',async function(e){
    e.preventDefault();go.disabled=true;show(msg,'Signing in...','');
    try{
      var data=await api('/api/auth/login',{method:'POST',body:{email:document.getElementById('email').value.trim(),password:document.getElementById('pw').value}});
      setToken(data.token);location.href='/account';
    }catch(err){go.disabled=false;show(msg,err.message,'err');}
  });
})();
</script>
"""

_FORGOT = """
<form id=f>
  <label>Email<input id=email type=email required autocomplete=email></label>
  <button id=go type=submit>Send reset link</button>
</form>
<p class=msg id=msg></p>
<p class=alt><a href="/login">Back to sign in</a></p>
<script>
(function(){
  var f=document.getElementById('f'),msg=document.getElementById('msg'),go=document.getElementById('go');
  var pre=new URLSearchParams(location.search).get('email');
  if(pre)document.getElementById('email').value=pre;
  f.addEventListener('submit',async function(e){
    e.preventDefault();go.disabled=true;
    var email=document.getElementById('email').value.trim();
    try{await api('/api/auth/password/forgot',{method:'POST',body:{email:email}});}catch(e){}
    show(msg,'If an account exists for '+email+', a password reset link is on its way.','ok');
  });
})();
</script>
"""

_RESET_FORM = """
<form id=f>
  <label>New password<input id=pw type=password required minlength=8 autocomplete=new-password placeholder="At least 8 characters"></label>
  <button id=go type=submit>Update password</button>
</form>
<p class=msg id=msg></p>
<script>
(function(){
  var f=document.getElementById('f'),msg=document.getElementById('msg'),go=document.getElementById('go');
  var token=new URLSearchParams(location.search).get('token')||'';
  f.addEventListener('submit',async function(e){
    e.preventDefault();go.disabled=true;show(msg,'Updating...','');
    try{
      await api('/api/auth/password/reset',{method:'POST',body:{token:token,password:document.getElementById('pw').value}});
      show(msg,'Password updated. Taking you to sign in...','ok');
      setTimeout(function(){location.href='/login'},1400);
    }catch(err){go.disabled=false;show(msg,err.message,'err');}
  });
})();
</script>
"""

_RESET_EXPIRED = """
<div class=center><div class="big no">!</div></div>
<p class="sub center">This password reset link is invalid or has expired. Reset links last a short time for safety.</p>
<p class=alt><a href="/forgot">Request a new link</a></p>
"""

_VERIFY_OK = """
<div class=center><div class="big ok">&#10003;</div></div>
<p class="sub center">Your email address is confirmed. Thanks.</p>
<p class=alt><a href="/account">Go to your account</a></p>
"""

_VERIFY_FAIL = """
<div class=center><div class="big no">!</div></div>
<p class="sub center">This confirmation link is invalid or has expired. You can send a fresh one from your account.</p>
<p class=alt><a href="/account">Go to your account</a></p>
"""

_ACCOUNT = """
<div id=loading class=sub>Loading your account...</div>
<div id=panel style="display:none">
  <p class=kv>Email <b id=a_email></b><span id=a_badge></span></p>
  <p class=kv>Name <b id=a_name></b></p>
  <p class=kv>Member since <b id=a_since></b></p>
  <div id=verifyrow style="display:none;margin-top:16px">
    <button id=resend class=ghost type=button>Resend confirmation email</button>
  </div>
  <hr class=div>
  <button id=togglepw class=ghost type=button>Change password</button>
  <form id=pwform style="display:none;margin-top:14px">
    <label>Current password<input id=cur type=password required autocomplete=current-password></label>
    <label>New password<input id=newpw type=password required minlength=8 autocomplete=new-password></label>
    <button id=pwgo type=submit>Update password</button>
  </form>
  <p class=alt><button id=signout class=ghost type=button style="width:100%">Sign out</button></p>
</div>
<p class=msg id=msg></p>
<script>
(async function(){
  function $(id){return document.getElementById(id);}
  if(!getToken()){location.href='/login';return;}
  try{
    var a=await api('/api/account',{auth:true});
    $('a_email').textContent=a.email;
    $('a_name').textContent=a.display_name||'Not set';
    $('a_since').textContent=a.created_at?new Date(a.created_at).toLocaleDateString():'Unknown';
    var b=$('a_badge');
    if(a.email_verified){b.className='badge yes';b.textContent='Confirmed';}
    else{b.className='badge no';b.textContent='Not confirmed';$('verifyrow').style.display='block';}
    $('loading').style.display='none';$('panel').style.display='block';
  }catch(err){clearToken();location.href='/login';return;}
  if($('resend')){$('resend').addEventListener('click',async function(){
    this.disabled=true;
    try{await api('/api/auth/verify/send',{method:'POST',auth:true});show($('msg'),'Confirmation email sent. Check your inbox.','ok');}
    catch(e){this.disabled=false;show($('msg'),e.message,'err');}
  });}
  $('togglepw').addEventListener('click',function(){var fm=$('pwform');fm.style.display=(fm.style.display==='none')?'flex':'none';});
  $('pwform').addEventListener('submit',async function(e){
    e.preventDefault();$('pwgo').disabled=true;
    try{
      await api('/api/auth/password/change',{method:'POST',auth:true,body:{current_password:$('cur').value,new_password:$('newpw').value}});
      show($('msg'),'Password updated.','ok');$('pwform').reset();$('pwform').style.display='none';
    }catch(err){show($('msg'),err.message,'err');}
    finally{$('pwgo').disabled=false;}
  });
  $('signout').addEventListener('click',function(){clearToken();location.href='/login';});
})();
</script>
"""


def _page(title: str, inner: str, sub: str = "") -> str:
    sub_html = f'<p class="sub">{sub}</p>' if sub else ""
    return (
        "<!doctype html><html lang=en><head><meta charset=utf-8>"
        "<meta name=viewport content=\"width=device-width,initial-scale=1\">"
        "<meta name=robots content=\"noindex\">"
        f"<title>{title} &middot; PDF Text Editor</title>"
        "<link rel=preconnect href=\"https://fonts.googleapis.com\">"
        "<link rel=preconnect href=\"https://fonts.gstatic.com\" crossorigin>"
        "<link href=\"https://fonts.googleapis.com/css2?family=Bricolage+Grotesque:opsz,wght@12..96,600..800&family=Hanken+Grotesk:wght@400;500;600;700&display=swap\" rel=stylesheet>"
        "<style>" + _CSS + "</style></head><body>"
        "<header><div class=bar>"
        "<a class=brand href=\"/\"><span class=mark>P</span>PDF Text Editor</a>"
        "<a class=home href=\"/\">Home</a>"
        "</div></header>"
        "<script>" + _JS + "</script>"
        "<main class=card>"
        f"<h1>{title}</h1>" + sub_html + inner +
        "</main></body></html>")
