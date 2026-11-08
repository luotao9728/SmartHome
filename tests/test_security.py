"""Unit tests for security helpers."""

from __future__ import annotations

import time

import pytest

from smarthome import security


def test_password_roundtrip():
    encoded = security.hash_password("hunter2hunter2")
    assert encoded.startswith("pbkdf2_sha256$")
    assert security.verify_password("hunter2hunter2", encoded)
    assert not security.verify_password("wrong password", encoded)


def test_password_hash_is_salted():
    a = security.hash_password("same-password")
    b = security.hash_password("same-password")
    assert a != b


@pytest.mark.parametrize("bad", ["", "short", None, 123])
def test_short_passwords_are_rejected(bad):
    with pytest.raises((ValueError, TypeError)):
        security.hash_password(bad)


@pytest.mark.parametrize(
    "encoded",
    [
        "",
        "nonsense",
        "pbkdf2_sha256$1$aa$bb",
        "md5$100000$aa$bb",
        "pbkdf2_sha256$x$aa$bb",
        "pbkdf2_sha256$100000$aa",
    ],
)
def test_verify_password_rejects_malformed_hashes(encoded):
    assert security.verify_password("whatever", encoded) is False


def test_device_tokens_are_prefixed_and_hashed():
    token = security.new_device_token()
    assert token.startswith("shd_")
    assert security.looks_like_device_token(token)
    digest = security.hash_device_token(token)
    assert digest != token
    assert len(digest) == 64
    assert security.hash_device_token(token) == digest


def test_session_cookie_roundtrip():
    secret = security.new_secret()
    cookie = security.new_session_cookie(secret, 1)
    payload = security.verify_session(cookie, secret)
    assert payload is not None and payload["sub"] == "admin"


def test_session_cookie_rejects_tampering_and_expiry():
    secret = security.new_secret()
    cookie = security.new_session_cookie(secret, 1)
    body, signature = cookie.rsplit(".", 1)
    assert security.verify_session(body + "." + ("A" * len(signature)), secret) is None
    assert security.verify_session(cookie, "another-secret") is None

    expired = security.sign_session({"sub": "admin", "exp": time.time() - 5}, secret)
    assert security.verify_session(expired, secret) is None


def test_generated_passwords_are_random_and_copyable():
    values = {security.generate_password() for _ in range(50)}
    assert len(values) == 50
    for value in values:
        assert not any(ch in value for ch in "0O1lI")
