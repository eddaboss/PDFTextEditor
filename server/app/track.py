"""Anonymous analytics: page views, downloads, and (later) updater check-ins,
stamped with Cloudflare's visitor geo. Feeds the private metrics dashboard.

No PII: an event is keyed by a random ``vid`` cookie, never an email. The
recorder is BEST-EFFORT -- it must never raise into a page load or a download.
"""
from __future__ import annotations

import uuid

from .models import Event

VID_COOKIE = "vid"
# A browser that clicked "exclude this browser" on the metrics dashboard carries
# this cookie; record() then drops its events so your own traffic isn't counted.
NOTRACK_COOKIE = "pdfte_notrack"


def new_vid() -> str:
    return uuid.uuid4().hex


def visitor_id(request) -> str:
    return (request.cookies.get(VID_COOKIE) or "")[:36]


def geo(request) -> tuple[str, str, str]:
    """(country ISO-2, region, city) from Cloudflare's visitor-location headers.
    Empty until the "Add visitor location headers" managed transform is on;
    cf-ipcountry ships by default, city/region come from that transform."""
    h = request.headers
    return (
        h.get("cf-ipcountry", "")[:2].upper(),
        (h.get("cf-region", "") or h.get("cf-region-code", ""))[:80],
        h.get("cf-ipcity", "")[:128],
    )


def platform_from_ua(ua: str) -> str:
    ua = (ua or "").lower()
    if "windows" in ua:
        return "windows"
    if any(k in ua for k in ("macintosh", "mac os", "darwin", "iphone", "ipad")):
        return "mac"
    return "other"


def platform_from_name(name: str) -> str:
    n = (name or "").lower()
    if n.endswith(".exe") or "setup" in n:
        return "windows"
    if n.endswith(".dmg"):
        return "mac"
    return "other"


def record(db, request, kind: str, *, path: str = "", referrer: str = "",
           platform: str = "", app_version: str = "", account_id=None) -> None:
    """Store one event. Swallows everything -- analytics never breaks the site."""
    if request.cookies.get(NOTRACK_COOKIE):
        return  # this browser opted out from the metrics dashboard
    try:
        country, region, city = geo(request)
        if not platform:
            platform = platform_from_ua(request.headers.get("user-agent", ""))
        db.add(Event(
            kind=kind,
            visitor_id=visitor_id(request),
            path=(path or "")[:255],
            referrer=(referrer or request.headers.get("referer", ""))[:512],
            country=country, region=region, city=city,
            platform=platform, app_version=(app_version or "")[:32],
            account_id=account_id,
        ))
        db.commit()
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
