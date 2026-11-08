"""Password hashing, signed session cookies and device tokens.

Only the standard library is used so the hub runs on a stock Raspberry Pi OS
installation (``pip install fastapi uvicorn`` is the only extra requirement).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from typing import Any, Optional

PBKDF2_ROUNDS = 240_000
MIN_ROUNDS = 100_000
MAX_ROUNDS = 4_000_000

SESSION_COOKIE = "sh_session"
DEVICE_TOKEN_PREFIX = "shd_"


# --------------------------------------------------------------------------- #
# base64 helpers (URL safe, unpadded)
# --------------------------------------------------------------------------- #
def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64d(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


# --------------------------------------------------------------------------- #
# Passwords
# --------------------------------------------------------------------------- #
def hash_password(password: str, *, rounds: int = PBKDF2_ROUNDS) -> str:
    """Return an encoded PBKDF2-SHA256 hash (``algorithm$rounds$salt$digest``)."""
    if not isinstance(password, str) or len(password) < 8:
        raise ValueError("password must be at least 8 characters long")
    if not (MIN_ROUNDS <= rounds <= MAX_ROUNDS):
        raise ValueError("invalid PBKDF2 round count")
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, rounds)
    return "pbkdf2_sha256$%d$%s$%s" % (rounds, _b64e(salt), _b64e(digest))


def verify_password(password: str, encoded: str) -> bool:
    """Constant-time check of ``password`` against an encoded hash."""
    try:
        algorithm, rounds_text, salt_text, digest_text = (encoded or "").split("$")
        if algorithm != "pbkdf2_sha256":
            return False
        rounds = int(rounds_text)
        if not (MIN_ROUNDS <= rounds <= MAX_ROUNDS):
            return False
        salt = _b64d(salt_text)
        expected = _b64d(digest_text)
    except (ValueError, TypeError):
        return False
    actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, rounds)
    return hmac.compare_digest(actual, expected)


def generate_password(length: int = 16) -> str:
    """Human-copyable random password (no look-alike characters)."""
    alphabet = "abcdefghijkmnopqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(alphabet) for _ in range(length))


def new_secret() -> str:
    """Random key used to sign browser sessions."""
    return secrets.token_urlsafe(48)


# --------------------------------------------------------------------------- #
# Device tokens
# --------------------------------------------------------------------------- #
def new_device_token() -> str:
    return DEVICE_TOKEN_PREFIX + secrets.token_urlsafe(32)


def safe_compare(left: str, right: str) -> bool:
    """``hmac.compare_digest`` for user supplied text.

    The stdlib version raises ``TypeError`` for non-ASCII ``str`` values, and a
    URL can contain anything, so compare the UTF-8 encodings instead.
    """
    if not isinstance(left, str) or not isinstance(right, str):
        return False
    try:
        return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))
    except (UnicodeEncodeError, TypeError):
        return False


def hash_device_token(token: str) -> str:
    """Tokens are stored hashed: a leaked database cannot be replayed."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def looks_like_device_token(token: Optional[str]) -> bool:
    return bool(token) and token.startswith(DEVICE_TOKEN_PREFIX) and len(token) >= 30


# --------------------------------------------------------------------------- #
# Signed browser sessions
# --------------------------------------------------------------------------- #
def sign_session(payload: dict[str, Any], secret: str) -> str:
    body = _b64e(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    signature = _b64e(hmac.new(secret.encode("utf-8"), body.encode("ascii"), hashlib.sha256).digest())
    return body + "." + signature


def verify_session(cookie: str, secret: str, *, now: Optional[float] = None) -> Optional[dict[str, Any]]:
    """Return the session payload when the cookie is authentic and unexpired."""
    if not cookie or "." not in cookie or not secret:
        return None
    body, signature = cookie.rsplit(".", 1)
    expected = _b64e(hmac.new(secret.encode("utf-8"), body.encode("ascii"), hashlib.sha256).digest())
    if not hmac.compare_digest(signature, expected):
        return None
    try:
        payload = json.loads(_b64d(body))
    except (ValueError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    expires = payload.get("exp")
    if not isinstance(expires, (int, float)):
        return None
    if (now if now is not None else time.time()) >= expires:
        return None
    return payload


def new_session_cookie(secret: str, ttl_hours: int, *, subject: str = "admin",
                       role: str = "admin", version: float = 0.0) -> str:
    """Build a signed session cookie.

    ``version`` is the user's ``updated`` stamp at issue time, kept as a float:
    truncating it to whole seconds would let two changes in the same second
    share a version, so a stale cookie would keep validating.  Bumping it (a
    password or role change) invalidates sessions issued before it.
    """
    now = time.time()
    return sign_session(
        {
            "sub": subject,
            "role": role,
            "ver": float(version),
            "iat": int(now),
            "exp": int(now + ttl_hours * 3600),
        },
        secret,
    )
