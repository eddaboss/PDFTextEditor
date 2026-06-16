"""End-to-end tests for the account system, run against the SQLite fallback so
they need no Postgres and no mail provider. Email is captured in-process so the
verify / reset links (and their one-time tokens) can be followed.

Run from the ``server`` directory:  python -m pytest test_accounts.py
"""
import os
import tempfile
from urllib.parse import parse_qs, urlparse

# Configure the app via env BEFORE importing it: config.py reads these at import.
_TMP = tempfile.mkdtemp(prefix="pdfte-test-")
os.environ.update(
    PDFTE_DATA_DIR=_TMP,
    DATABASE_URL="sqlite+pysqlite:///:memory:",
    JWT_SECRET="test-secret-not-for-production-but-long-enough-now",
    PDFTE_PUBLIC_URL="https://test.local",
    LOGIN_MAX_ATTEMPTS="5",
    REGISTER_MAX_ATTEMPTS="50",
    RESET_MAX_REQUESTS="50",
)

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import emailer  # noqa: E402
from app.account_models import RateEvent  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.main import app  # noqa: E402
from sqlalchemy import delete  # noqa: E402

# Captured outbound mail: send_* are replaced so nothing leaves the process.
SENT = {"verify": [], "reset": []}


@pytest.fixture(scope="module")
def client():
    emailer.send_verification_email = lambda to, link: (
        SENT["verify"].append((to, link)) or True)
    emailer.send_reset_email = lambda to, link: (
        SENT["reset"].append((to, link)) or True)
    with TestClient(app) as c:
        yield c


@pytest.fixture(autouse=True)
def _reset_state():
    SENT["verify"].clear()
    SENT["reset"].clear()
    db = SessionLocal()
    db.execute(delete(RateEvent))  # so one test's throttle never bleeds into another
    db.commit()
    db.close()
    yield


def _token(link: str) -> str:
    return parse_qs(urlparse(link).query)["token"][0]


def _ip(addr: str) -> dict:
    return {"x-forwarded-for": addr}


def _register(client, email, password="password123", **headers):
    return client.post("/api/auth/register",
                       json={"email": email, "password": password,
                             "display_name": "Test"}, headers=headers or {})


def test_register_then_verify_email(client):
    r = _register(client, "verify@example.com", **_ip("10.0.0.1"))
    assert r.status_code == 200, r.text
    token = r.json()["token"]
    auth = {"Authorization": f"Bearer {token}"}

    acct = client.get("/api/account", headers=auth).json()
    assert acct["email"] == "verify@example.com"
    assert acct["email_verified"] is False

    # The signup page triggers the verification email via this endpoint.
    assert client.post("/api/auth/verify/send", headers=auth).status_code == 200
    assert len(SENT["verify"]) == 1
    to, link = SENT["verify"][0]
    assert to == "verify@example.com"

    page = client.get("/verify", params={"token": _token(link)})
    assert page.status_code == 200
    assert "confirmed" in page.text.lower()

    acct = client.get("/api/account", headers=auth).json()
    assert acct["email_verified"] is True


def test_verify_token_is_single_use(client):
    r = _register(client, "once@example.com", **_ip("10.0.0.2"))
    auth = {"Authorization": f"Bearer {r.json()['token']}"}
    client.post("/api/auth/verify/send", headers=auth)
    tok = _token(SENT["verify"][0][1])

    assert client.get("/verify", params={"token": tok}).status_code == 200
    # Reusing the now-consumed token lands on the expired page, not success.
    again = client.get("/verify", params={"token": tok})
    assert "expired" in again.text.lower() or "invalid" in again.text.lower()


def test_verify_bad_token_does_not_verify(client):
    page = client.get("/verify", params={"token": "nonsense"})
    assert page.status_code == 200
    assert "expired" in page.text.lower() or "invalid" in page.text.lower()


def test_login_success_and_wrong_password(client):
    _register(client, "login@example.com", password="rightpass1",
              **_ip("10.0.0.3"))

    ok = client.post("/api/auth/login",
                     json={"email": "login@example.com", "password": "rightpass1"},
                     headers=_ip("10.0.0.3"))
    assert ok.status_code == 200 and ok.json()["token"]

    bad = client.post("/api/auth/login",
                      json={"email": "login@example.com", "password": "wrong"},
                      headers=_ip("10.0.0.3"))
    assert bad.status_code == 401


def test_login_is_rate_limited(client):
    _register(client, "brute@example.com", password="rightpass1",
              **_ip("10.9.9.9"))
    # LOGIN_MAX_ATTEMPTS=5: the 6th attempt from the same IP is throttled.
    seen_429 = False
    for _ in range(8):
        resp = client.post(
            "/api/auth/login",
            json={"email": "brute@example.com", "password": "wrong"},
            headers=_ip("203.0.113.7"))
        if resp.status_code == 429:
            seen_429 = True
            break
    assert seen_429, "expected a 429 after repeated login attempts"


def test_forgot_does_not_leak_account_existence(client):
    # Unknown email: still a 200, and no mail is sent.
    r = client.post("/api/auth/password/forgot",
                    json={"email": "ghost@example.com"}, headers=_ip("10.0.0.4"))
    assert r.status_code == 200
    assert SENT["reset"] == []


def test_password_reset_flow(client):
    _register(client, "reset@example.com", password="oldpass123",
              **_ip("10.0.0.5"))
    r = client.post("/api/auth/password/forgot",
                    json={"email": "reset@example.com"}, headers=_ip("10.0.0.5"))
    assert r.status_code == 200
    assert len(SENT["reset"]) == 1
    tok = _token(SENT["reset"][0][1])

    # The reset page renders the form for a valid token.
    assert "new password" in client.get(
        "/reset", params={"token": tok}).text.lower()

    done = client.post("/api/auth/password/reset",
                       json={"token": tok, "password": "newpass456"})
    assert done.status_code == 200

    # New password works, old one does not.
    assert client.post("/api/auth/login",
                       json={"email": "reset@example.com", "password": "newpass456"},
                       headers=_ip("10.0.0.55")).status_code == 200
    assert client.post("/api/auth/login",
                       json={"email": "reset@example.com", "password": "oldpass123"},
                       headers=_ip("10.0.0.55")).status_code == 401

    # The reset token cannot be replayed.
    assert client.post("/api/auth/password/reset",
                       json={"token": tok, "password": "another789"}
                       ).status_code == 400


def test_reset_also_confirms_email(client):
    r = _register(client, "rc@example.com", password="oldpass123",
                  **_ip("10.0.0.6"))
    auth = {"Authorization": f"Bearer {r.json()['token']}"}
    assert client.get("/api/account", headers=auth).json()["email_verified"] is False

    client.post("/api/auth/password/forgot",
                json={"email": "rc@example.com"}, headers=_ip("10.0.0.6"))
    tok = _token(SENT["reset"][0][1])
    client.post("/api/auth/password/reset",
                json={"token": tok, "password": "freshpass1"})

    assert client.get("/api/account", headers=auth).json()["email_verified"] is True


def test_change_password_requires_current(client):
    r = _register(client, "chg@example.com", password="origpass1",
                  **_ip("10.0.0.7"))
    auth = {"Authorization": f"Bearer {r.json()['token']}"}

    wrong = client.post("/api/auth/password/change",
                        json={"current_password": "nope", "new_password": "brandnew1"},
                        headers=auth)
    assert wrong.status_code == 403

    ok = client.post("/api/auth/password/change",
                     json={"current_password": "origpass1", "new_password": "brandnew1"},
                     headers=auth)
    assert ok.status_code == 200
    assert client.post("/api/auth/login",
                       json={"email": "chg@example.com", "password": "brandnew1"},
                       headers=_ip("10.0.0.77")).status_code == 200


def test_short_password_rejected(client):
    r = _register(client, "short@example.com", password="abc", **_ip("10.0.0.8"))
    assert r.status_code == 422  # pydantic min_length


def test_account_pages_render(client):
    for path in ("/login", "/signup", "/forgot", "/account"):
        resp = client.get(path)
        assert resp.status_code == 200
        assert "PDF Text Editor" in resp.text


def test_run_migrations_backfills_existing_users_table():
    """The path prod actually hits: a users table that predates email_verified.
    create_all never alters it, so run_migrations must add the columns."""
    from sqlalchemy import create_engine, inspect, text
    from sqlalchemy.pool import StaticPool

    from app.account_models import run_migrations

    eng = create_engine("sqlite+pysqlite:///:memory:",
                        connect_args={"check_same_thread": False},
                        poolclass=StaticPool, future=True)
    with eng.begin() as conn:
        conn.execute(text(
            "CREATE TABLE users (id INTEGER PRIMARY KEY, email VARCHAR, "
            "password_hash VARCHAR, display_name VARCHAR, created_at TIMESTAMP)"))
        conn.execute(text(
            "INSERT INTO users (email, password_hash) VALUES ('old@x.com', 'h')"))

    run_migrations(eng)
    run_migrations(eng)  # idempotent: a second run must not error

    cols = {c["name"] for c in inspect(eng).get_columns("users")}
    assert "email_verified" in cols and "email_verified_at" in cols
    with eng.connect() as conn:
        row = conn.execute(text(
            "SELECT email_verified FROM users WHERE email='old@x.com'")).first()
    assert row[0] in (0, False)  # existing rows default to unverified
