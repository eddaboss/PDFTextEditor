"""Password hashing (Argon2id), JWT access tokens, and single-use link tokens."""
import datetime
import hashlib
import secrets

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError, VerificationError

from .config import JWT_SECRET, JWT_TTL_HOURS

_ph = PasswordHasher()


def hash_password(password: str) -> str:
    return _ph.hash(password)


def verify_password(password: str, hashed: str) -> bool:
    try:
        _ph.verify(hashed, password)
        return True
    except (VerifyMismatchError, VerificationError):
        return False


def make_token(user_id: int, email: str) -> str:
    now = datetime.datetime.now(datetime.timezone.utc)
    payload = {
        "sub": str(user_id),
        "email": email,
        "iat": int(now.timestamp()),
        "exp": int((now + datetime.timedelta(hours=JWT_TTL_HOURS)).timestamp()),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm="HS256")


def decode_token(token: str) -> dict:
    return jwt.decode(token, JWT_SECRET, algorithms=["HS256"])


# --- single-use link tokens (email verification, password reset) ------------
def new_link_token() -> tuple[str, str]:
    """Return ``(raw, token_hash)``. The raw token goes in the emailed link; only
    the hash is stored, so the database never holds anything usable on its own."""
    raw = secrets.token_urlsafe(32)
    return raw, hash_link_token(raw)


def hash_link_token(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


# --- setup codes (short, human-typeable; redeemed by the desktop app) --------
# Crockford-ish alphabet: no 0/O/1/I/L so a code is easy to read and type.
_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"


def new_setup_code() -> tuple[str, str]:
    """Return ``(display, code_hash)``. ``display`` is shown to the user as
    ``XXXX-XXXX``; only ``code_hash`` is stored."""
    raw = "".join(secrets.choice(_CODE_ALPHABET) for _ in range(8))
    return f"{raw[:4]}-{raw[4:]}", hash_setup_code(raw)


def hash_setup_code(code: str) -> str:
    norm = code.upper().replace("-", "").replace(" ", "")
    # Domain-separated from link tokens so the two hash spaces never overlap.
    return hashlib.sha256(("setup:" + norm).encode("utf-8")).hexdigest()


# --- download-gate email codes (6 digits, emailed to prove the address) ------
def new_gate_code() -> tuple[str, str]:
    """Return ``(display, code_hash)`` for the download-gate email check. Six
    cryptographically-secure digits (``secrets``, never the ``random`` module);
    only the hash is ever stored."""
    code = f"{secrets.randbelow(1_000_000):06d}"
    return code, hash_gate_code(code)


def hash_gate_code(code: str) -> str:
    norm = "".join(ch for ch in str(code) if ch.isdigit())
    return hashlib.sha256(("gate:" + norm).encode("utf-8")).hexdigest()
