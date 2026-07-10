"""Tiny stdlib client for the optional accounts API (Settings > Account).

The editor is fully functional with no account; this only powers the optional
sign-in. The token persists in QSettings. Uses urllib so it adds no dependency.
Every call returns ``(status_code, payload)`` and never raises.
"""
import json
import urllib.error
import urllib.request

from . import appconfig

_TIMEOUT = 20


def _request(path: str, method: str = "GET", body=None, token=None):
    url = appconfig.API_BASE_URL + path
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Content-Type": "application/json"} if data else {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read()).get("detail", "")
        except Exception:
            detail = ""
        return exc.code, {"detail": detail or f"Request failed ({exc.code})."}
    except Exception as exc:  # noqa: BLE001 - network errors are expected
        return 0, {"detail": f"Could not reach the server: {exc}"}


def register(email: str, password: str, display_name: str = ""):
    return _request("/api/auth/register", "POST",
                    {"email": email, "password": password,
                     "display_name": display_name})


def login(email: str, password: str):
    return _request("/api/auth/login", "POST",
                    {"email": email, "password": password})


def claim_setup_code(code: str):
    """Redeem a one-time sign-in code from the website for a session token, so
    the app signs in with no password. Returns ``(status, {"token", "user"})``."""
    return _request("/api/onboard/claim", "POST", {"code": code})
