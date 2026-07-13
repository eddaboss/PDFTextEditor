"""Private metrics dashboard: visits vs downloads, geo by city, the
visit -> download -> signup funnel, and platform split.

Gated by a TOTP code (an authenticator app) -- no Cloudflare Access needed.
Set METRICS_TOTP_SECRET (base32) to enable it; unset means the route 404s. A
correct code mints a short-lived cookie signed with JWT_SECRET.

Everything it reads is anonymous (Event.visitor_id is a random cookie). Emails
live in users/consents and are only ever shown as COUNTS here.
"""
from __future__ import annotations

import base64
import datetime
import hashlib
import hmac
import html
import os
import struct
import time

from fastapi import Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .config import JWT_SECRET
from .db import get_db
from .models import Consent, Event, User

PATH = "/0101-pdfte-metrics-0101"          # obscure + TOTP-gated
_SECRET = os.environ.get("METRICS_TOTP_SECRET", "")
_COOKIE = "pdfte_metrics"
_SESSION_TTL = 8 * 60 * 60                  # 8h


# --- TOTP (RFC 6238, stdlib) + signed session ------------------------------
def _totp(when: int, step: int = 30, digits: int = 6) -> str:
    key = base64.b32decode(_SECRET.upper() + "=" * (-len(_SECRET) % 8))
    mac = hmac.new(key, struct.pack(">Q", when // step), hashlib.sha1).digest()
    off = mac[-1] & 0x0F
    code = (struct.unpack(">I", mac[off:off + 4])[0] & 0x7FFFFFFF) % (10 ** digits)
    return str(code).zfill(digits)


def _totp_ok(code: str) -> bool:
    code = (code or "").strip()
    if not (_SECRET and code.isdigit()):
        return False
    now = int(time.time())
    return any(hmac.compare_digest(_totp(now + d * 30), code) for d in (-1, 0, 1))


def _mint() -> str:
    exp = str(int(time.time()) + _SESSION_TTL)
    sig = hmac.new(JWT_SECRET.encode(), exp.encode(), hashlib.sha256).hexdigest()
    return f"{exp}.{sig}"


def _session_ok(cookie: str) -> bool:
    try:
        exp, sig = (cookie or "").rsplit(".", 1)
    except ValueError:
        return False
    good = hmac.new(JWT_SECRET.encode(), exp.encode(), hashlib.sha256).hexdigest()
    return (hmac.compare_digest(sig, good) and exp.isdigit()
            and int(exp) > int(time.time()))


# --- queries ----------------------------------------------------------------
def _stats(db: Session) -> dict:
    since = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=30)

    def _count(*where):
        return db.scalar(select(func.count()).select_from(Event).where(*where)) or 0

    visits = _count(Event.kind == "pageview")
    downloads = _count(Event.kind == "download")
    uniq_visitors = db.scalar(
        select(func.count(func.distinct(Event.visitor_id))).where(
            Event.kind == "pageview", Event.visitor_id != "")) or 0
    signups = db.scalar(select(func.count()).select_from(User)) or 0
    consents = db.scalar(select(func.count()).select_from(Consent)) or 0

    # visitors who reached each funnel stage (distinct browsers)
    def _uniq(q):
        return {v for (v,) in db.execute(q) if v}
    downloaders = _uniq(select(func.distinct(Event.visitor_id)).where(
        Event.kind == "download", Event.visitor_id != ""))
    consenters = _uniq(select(func.distinct(Consent.visitor_id)).where(
        Consent.visitor_id != ""))
    accounts_from_visit = db.scalar(
        select(func.count(func.distinct(Consent.visitor_id))).where(
            Consent.visitor_id != "", Consent.account_id.isnot(None))) or 0

    plat = db.execute(
        select(Event.platform, func.count()).where(Event.kind == "download")
        .group_by(Event.platform)).all()
    cities = db.execute(
        select(Event.city, Event.country, func.count())
        .where(Event.city != "").group_by(Event.city, Event.country)
        .order_by(func.count().desc()).limit(12)).all()
    pages = db.execute(
        select(Event.path, func.count()).where(Event.kind == "pageview")
        .group_by(Event.path).order_by(func.count().desc()).limit(10)).all()

    # last-30-day daily series for visits + downloads
    day = func.date(Event.ts)
    rows = db.execute(
        select(day, Event.kind, func.count()).where(Event.ts >= since)
        .group_by(day, Event.kind)).all()
    series: dict[str, dict[str, int]] = {}
    for d, kind, n in rows:
        series.setdefault(str(d), {})[kind] = n
    days = [(since + datetime.timedelta(days=i)).date().isoformat() for i in range(31)]
    daily = [(d, series.get(d, {}).get("pageview", 0),
              series.get(d, {}).get("download", 0)) for d in days]

    return dict(
        visits=visits, downloads=downloads, uniq_visitors=uniq_visitors,
        signups=signups, consents=consents,
        n_downloaders=len(downloaders), n_consenters=len(consenters),
        accounts_from_visit=accounts_from_visit,
        platforms=plat, cities=cities, pages=pages, daily=daily)


# --- render -----------------------------------------------------------------
def _bars(daily) -> str:
    """Tiny inline-SVG chart: visits (bg bar) with downloads (fg) per day."""
    mx = max([v for _, v, _ in daily] + [1])
    w, h, n = 760, 150, len(daily)
    bw = w / n
    out = []
    for i, (d, v, dl) in enumerate(daily):
        x = i * bw
        vh = (v / mx) * (h - 18)
        dh = (dl / mx) * (h - 18)
        out.append(f'<rect x="{x:.1f}" y="{h-18-vh:.1f}" width="{bw-1.5:.1f}" '
                   f'height="{vh:.1f}" rx="1.5" fill="#D8C6AC"><title>{d}: '
                   f'{v} visits, {dl} downloads</title></rect>')
        if dh:
            out.append(f'<rect x="{x:.1f}" y="{h-18-dh:.1f}" width="{bw-1.5:.1f}" '
                       f'height="{dh:.1f}" rx="1.5" fill="#AA4E2C"/>')
    labels = "".join(
        f'<text x="{i*bw:.1f}" y="{h-4}" font-size="9" fill="#897E6E">{d[5:]}</text>'
        for i, (d, _, _) in enumerate(daily) if i % 5 == 0)
    return (f'<svg viewBox="0 0 {w} {h}" width="100%" preserveAspectRatio="none" '
            f'style="max-width:100%">{"".join(out)}{labels}</svg>')


def _row(label, val) -> str:
    return f"<tr><td>{html.escape(str(label))}</td><td class=n>{val}</td></tr>"


def _page(s: dict) -> str:
    def pct(a, b):
        return f"{100*a/b:.1f}%" if b else "-"
    tiles = [
        ("Visits (page views)", s["visits"]),
        ("Unique visitors", s["uniq_visitors"]),
        ("Downloads", s["downloads"]),
        ("Signups", s["signups"]),
    ]
    tile_html = "".join(
        f'<div class=tile><div class=k>{v:,}</div><div class=l>{html.escape(l)}</div></div>'
        for l, v in tiles)
    plat = "".join(_row(p or "unknown", f"{n:,}") for p, n in s["platforms"]) \
        or _row("no downloads yet", 0)
    cities = "".join(_row(f"{c}, {co}", f"{n:,}") for c, co, n in s["cities"]) \
        or _row("no geo yet (enable CF visitor-location headers)", 0)
    pages = "".join(_row(p or "/", f"{n:,}") for p, n in s["pages"]) \
        or _row("no visits yet", 0)
    funnel = "".join([
        _row("Visitors", f'{s["uniq_visitors"]:,}'),
        _row("→ Agreed at gate", f'{s["n_consenters"]:,} ({pct(s["n_consenters"], s["uniq_visitors"])})'),
        _row("→ Downloaded", f'{s["n_downloaders"]:,} ({pct(s["n_downloaders"], s["uniq_visitors"])})'),
        _row("→ Made an account", f'{s["accounts_from_visit"]:,} ({pct(s["accounts_from_visit"], s["uniq_visitors"])})'),
    ])
    return f"""<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<meta name=robots content="noindex,nofollow"><title>PDF for Free — metrics</title>
<link rel=preconnect href="https://fonts.googleapis.com">
<link rel=preconnect href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Bricolage+Grotesque:opsz,wght@12..96,500..800&family=Hanken+Grotesk:wght@400;500;600;700&display=swap" rel=stylesheet>
<style>
:root{{--paper:#FBF9F5;--panel:#F5F1EA;--canvas:#ECE6DC;--line:#E2DACD;--ink:#2A2520;
--ink2:#5C5346;--ink3:#897E6E;--clay:#C2643F;--clay-fill:#AA4E2C;--clay-press:#8B3E23;
--shadow2:rgba(58,40,26,.08);
--display:"Bricolage Grotesque",ui-sans-serif,system-ui,sans-serif;
--body:"Hanken Grotesk",ui-sans-serif,-apple-system,Segoe UI,Roboto,sans-serif}}
*{{box-sizing:border-box}}
body{{margin:0 auto;max-width:1080px;background:var(--paper);color:var(--ink);
font-family:var(--body);-webkit-font-smoothing:antialiased;padding:30px}}
h1{{font-family:var(--display);font-weight:700;letter-spacing:-.02em;font-size:28px;margin:0}}
h1 .u{{color:var(--clay-press)}}
.sub{{color:var(--ink3);font-size:14px;margin:4px 0 24px}}
h2{{font-family:var(--display);font-weight:600;letter-spacing:-.01em;font-size:14.5px;color:var(--ink2);margin:28px 0 10px}}
.tiles{{display:grid;grid-template-columns:repeat(auto-fit,minmax(165px,1fr));gap:14px}}
.tile,.card,.chart{{background:var(--panel);border:1px solid var(--line);border-radius:16px;
box-shadow:0 2px 10px var(--shadow2)}}
.tile{{padding:16px 18px}}
.tile .k{{font-family:var(--display);font-weight:700;font-size:31px;letter-spacing:-.02em;line-height:1.05}}
.tile .l{{color:var(--ink3);font-size:13px;margin-top:3px}}
.card{{padding:6px 18px}}.chart{{background:#fff;padding:16px 18px 8px}}
.cols{{display:grid;grid-template-columns:1fr 1fr;gap:16px;align-items:start}}
@media(max-width:720px){{.cols{{grid-template-columns:1fr}}body{{padding:18px}}}}
table{{width:100%;border-collapse:collapse}}
td{{padding:9px 2px;border-top:1px solid var(--line);font-size:14px;color:var(--ink2)}}
td.n{{text-align:right;font-variant-numeric:tabular-nums;font-weight:600;color:var(--ink)}}
tr:first-child td{{border-top:0}}
.legend{{color:var(--ink3);font-size:12.5px;margin:8px 0 4px;display:flex;gap:18px}}
.sw{{display:inline-block;width:11px;height:11px;border-radius:3px;margin-right:6px;vertical-align:-1px}}
.note{{color:var(--ink3);font-size:13px;margin-top:26px;border-top:1px solid var(--line);padding-top:14px}}
</style></head><body>
<h1>Traffic &amp; downloads<span class=u>.</span></h1>
<p class=sub>pdf-for-free.com &middot; anonymous &middot; last 31 days &middot; geo from Cloudflare</p>
<div class=tiles>{tile_html}</div>
<h2>Visits vs downloads</h2>
<div class=chart>{_bars(s["daily"])}
<p class=legend><span><span class=sw style="background:#D8C6AC"></span>visits</span>
<span><span class=sw style="background:#AA4E2C"></span>downloads</span></p></div>
<div class=cols>
 <div><h2>Where &mdash; by city</h2><div class=card><table>{cities}</table></div></div>
 <div><h2>Funnel</h2><div class=card><table>{funnel}</table></div></div>
 <div><h2>Downloads by platform</h2><div class=card><table>{plat}</table></div></div>
 <div><h2>Top pages</h2><div class=card><table>{pages}</table></div></div>
</div>
<p class=note>Agreements on record: {s['consents']:,}. Active-installs-by-version needs a desktop-side ping (not built yet).</p>
</body></html>"""


_LOGIN = """<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<meta name=robots content="noindex"><title>metrics</title>
<link rel=preconnect href="https://fonts.googleapis.com">
<link rel=preconnect href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Bricolage+Grotesque:wght@700&family=Hanken+Grotesk:wght@400;600&display=swap" rel=stylesheet>
<style>:root{--paper:#FBF9F5;--panel:#F5F1EA;--line:#E2DACD;--ink:#2A2520;--clay-fill:#AA4E2C;--clay-press:#8B3E23}
body{margin:0;background:var(--paper);color:var(--ink);font-family:"Hanken Grotesk",system-ui,sans-serif;
display:grid;place-items:center;height:100vh}
form{background:var(--panel);border:1px solid var(--line);padding:30px;border-radius:18px;width:280px;
text-align:center;box-shadow:0 10px 34px rgba(58,40,26,.10)}
.t{font-family:"Bricolage Grotesque",system-ui,sans-serif;font-weight:700;font-size:20px;letter-spacing:-.02em}
input{width:100%;padding:12px;margin:14px 0 4px;font-size:23px;text-align:center;letter-spacing:.32em;
background:#fff;border:1px solid var(--line);border-radius:11px;color:var(--ink);font-family:inherit}
input:focus{outline:3px solid rgba(194,100,63,.4);border-color:var(--clay-fill)}
button{width:100%;padding:12px;margin-top:10px;background:var(--clay-fill);color:#fff;border:0;
border-radius:11px;font-size:15px;font-weight:600;cursor:pointer;font-family:inherit}
button:hover{background:var(--clay-press)}.e{color:#B0341F;font-size:13px;min-height:16px;margin-top:8px}</style>
</head><body><form method=post><div class=t>Metrics</div>
<input name=code inputmode=numeric autocomplete=one-time-code placeholder="000000" autofocus maxlength=6>
<button>Enter</button><div class=e>__ERR__</div></form></body></html>"""


def install(app) -> None:
    """Wire the dashboard routes. No-op (route 404s) when the TOTP secret is
    unset, so an un-provisioned deploy simply has no dashboard."""
    if not _SECRET:
        return

    @app.get(PATH, response_class=HTMLResponse)
    def dash(request: Request, db: Session = Depends(get_db)):
        if not _session_ok(request.cookies.get(_COOKIE, "")):
            return HTMLResponse(_LOGIN.replace("__ERR__", ""))
        return HTMLResponse(_page(_stats(db)))

    @app.post(PATH, response_class=HTMLResponse)
    async def login(request: Request):
        form = await request.form()
        if not _totp_ok(str(form.get("code", ""))):
            return HTMLResponse(_LOGIN.replace("__ERR__", "Wrong code"),
                                status_code=401)
        resp = RedirectResponse(PATH, status_code=303)
        resp.set_cookie(_COOKIE, _mint(), max_age=_SESSION_TTL,
                        httponly=True, samesite="lax")
        return resp
