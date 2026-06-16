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
