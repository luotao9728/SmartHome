"""Multi-user accounts: creation, roles, password changes and isolation."""

from __future__ import annotations

import pytest

from smarthome.store import ROLE_ADMIN, ROLE_USER

from .conftest import PASSWORD  # noqa: F401 - re-exported for tests below
from .test_server import auth_client, client  # noqa: F401  # pytest fixtures


def make_user(auth_client, username, password, role=ROLE_USER, display_name=""):
    response = auth_client.post("/api/users", json={
        "username": username, "password": password,
        "role": role, "display_name": display_name,
    })
    assert response.status_code == 201, response.text
    return response.json()


def login_as(base_url, username, password):
    from fastapi.testclient import TestClient

    # A fresh client so cookie jars do not bleed between users.
    return TestClient(base_url, base_url="").post  # placeholder, replaced below


# --------------------------------------------------------------------------- #
# the migrated legacy admin
# --------------------------------------------------------------------------- #
def test_legacy_admin_password_becomes_the_first_user(auth_client, config):
    """An existing install keeps working after the upgrade."""
    users = auth_client.get("/api/users").json()["users"]
    assert [u["username"] for u in users] == ["admin"]
    assert users[0]["role"] == ROLE_ADMIN
    # The password from config.json still logs in.
    assert auth_client.post("/api/login", json={"password": PASSWORD}).status_code == 200


def test_me_reports_the_signed_in_user(auth_client):
    body = auth_client.get("/api/me").json()
    assert body["authenticated"] is True
    assert body["user"]["username"] == "admin"
    assert body["user"]["role"] == ROLE_ADMIN
    assert body["needs_setup"] is False


# --------------------------------------------------------------------------- #
# creating users
# --------------------------------------------------------------------------- #
def test_admin_creates_a_user_who_can_log_in(auth_client, client):
    user = make_user(auth_client, "alice", "alice-password", ROLE_USER, "Alice")
    assert user["username"] == "alice"
    assert user["role"] == ROLE_USER
    assert "password_hash" not in user, "the hash must never leave the server"

    # A separate client, like another browser.
    response = client.post("/api/login", json={"username": "alice", "password": "alice-password"})
    assert response.status_code == 200
    me = client.get("/api/me").json()
    assert me["user"]["username"] == "alice"
    assert me["user"]["role"] == ROLE_USER


def test_duplicate_username_is_rejected(auth_client):
    make_user(auth_client, "bob", "bob-password")
    duplicate = auth_client.post("/api/users", json={"username": "bob", "password": "another-password"})
    assert duplicate.status_code == 400
    assert "already exists" in duplicate.json()["error"]


@pytest.mark.parametrize("bad", ["", "a", "has space", "no/slash", "x" * 40, "-leading"])
def test_invalid_usernames_are_rejected(auth_client, bad):
    response = auth_client.post("/api/users", json={"username": bad, "password": "long-enough-pw"})
    assert response.status_code == 400, bad


def test_short_password_is_rejected(auth_client):
    response = auth_client.post("/api/users", json={"username": "shorty", "password": "short"})
    assert response.status_code == 400


def test_invalid_role_is_rejected(auth_client):
    response = auth_client.post("/api/users", json={
        "username": "roleless", "password": "long-enough-pw", "role": "superuser"})
    assert response.status_code == 400


def test_users_are_listed_without_hashes(auth_client):
    make_user(auth_client, "carol", "carol-password")
    payload = auth_client.get("/api/users").json()
    names = [u["username"] for u in payload["users"]]
    assert "admin" in names and "carol" in names
    assert all("password_hash" not in u for u in payload["users"])


# --------------------------------------------------------------------------- #
# normal users are limited
# --------------------------------------------------------------------------- #
def test_normal_user_cannot_manage_users(auth_client, client):
    make_user(auth_client, "dave", "dave-password")
    assert client.post("/api/login", json={"username": "dave", "password": "dave-password"}).status_code == 200

    assert client.get("/api/users").status_code == 403
    assert client.post("/api/users", json={"username": "eve", "password": "eve-password"}).status_code == 403
    assert client.delete("/api/users/admin").status_code == 403
    # But normal device use is allowed.
    assert client.get("/api/devices").status_code == 200
    assert client.get("/api/bootstrap").status_code == 200


def test_normal_user_can_change_their_own_password(client, auth_client):
    make_user(auth_client, "frank", "frank-password")
    assert client.post("/api/login", json={"username": "frank", "password": "frank-password"}).status_code == 200

    changed = client.post("/api/password", json={"current": "frank-password", "new": "frank-new-password"})
    assert changed.status_code == 200, changed.text

    # Still signed in on this client...
    assert client.get("/api/me").json()["user"]["username"] == "frank"
    # ...and the new password works from a fresh login.
    assert client.post("/api/login", json={"username": "frank", "password": "frank-new-password"}).status_code == 200
    assert client.post("/api/login", json={"username": "frank", "password": "frank-password"}).status_code == 401


def test_wrong_current_password_is_refused(client, auth_client):
    make_user(auth_client, "grace", "grace-password")
    client.post("/api/login", json={"username": "grace", "password": "grace-password"})
    response = client.post("/api/password", json={"current": "nope", "new": "grace-new-password"})
    assert response.status_code == 403


# --------------------------------------------------------------------------- #
# admin password reset of another user
# --------------------------------------------------------------------------- #
def test_admin_resets_another_users_password(auth_client, client):
    make_user(auth_client, "heidi", "heidi-password")
    response = auth_client.patch("/api/users/heidi", json={"password": "heidi-reset-password"})
    assert response.status_code == 200, response.text

    assert client.post("/api/login", json={"username": "heidi", "password": "heidi-reset-password"}).status_code == 200
    assert client.post("/api/login", json={"username": "heidi", "password": "heidi-password"}).status_code == 401


def test_password_change_invalidates_other_sessions(auth_client):
    """A reset must log out browsers holding the old session."""
    from fastapi.testclient import TestClient

    make_user(auth_client, "ivan", "ivan-password")

    # A second, independent client = another browser with its own cookie jar.
    other = TestClient(auth_client.app)
    with other:
        assert other.post("/api/login",
                          json={"username": "ivan", "password": "ivan-password"}).status_code == 200
        assert other.get("/api/me").json()["authenticated"] is True

        auth_client.patch("/api/users/ivan", json={"password": "ivan-new-password"})

        # The stale cookie no longer identifies a valid session.
        assert other.get("/api/me").json()["authenticated"] is False
        assert other.get("/api/devices").status_code == 401


def test_own_password_change_keeps_you_signed_in(auth_client):
    """The browser that changed its own password must not be logged out."""
    from fastapi.testclient import TestClient

    make_user(auth_client, "olga", "olga-password")
    mine = TestClient(auth_client.app)
    with mine:
        assert mine.post("/api/login",
                         json={"username": "olga", "password": "olga-password"}).status_code == 200
        response = mine.post("/api/password",
                             json={"current": "olga-password", "new": "olga-new-password"})
        assert response.status_code == 200, response.text
        # The cookie was re-issued, so this session survives its own change.
        assert mine.get("/api/me").json()["user"]["username"] == "olga"
        assert mine.get("/api/devices").status_code == 200


# --------------------------------------------------------------------------- #
# roles and safety rails
# --------------------------------------------------------------------------- #
def test_admin_can_promote_a_user(auth_client, client):
    make_user(auth_client, "judy", "judy-password")
    promoted = auth_client.patch("/api/users/judy", json={"role": ROLE_ADMIN})
    assert promoted.status_code == 200
    assert promoted.json()["role"] == ROLE_ADMIN

    assert client.post("/api/login", json={"username": "judy", "password": "judy-password"}).status_code == 200
    assert client.get("/api/users").status_code == 200


def test_cannot_demote_or_delete_the_last_admin(auth_client):
    demote = auth_client.patch("/api/users/admin", json={"role": ROLE_USER})
    assert demote.status_code == 400
    assert "last administrator" in demote.json()["error"]

    delete = auth_client.delete("/api/users/admin")
    assert delete.status_code == 400
    assert "last administrator" in delete.json()["error"]


def test_can_demote_when_another_admin_exists(auth_client):
    make_user(auth_client, "kim", "kim-password", ROLE_ADMIN)
    response = auth_client.patch("/api/users/admin", json={"role": ROLE_USER})
    assert response.status_code == 200
    assert response.json()["role"] == ROLE_USER


def test_cannot_delete_your_own_account(auth_client):
    make_user(auth_client, "leo", "leo-password", ROLE_ADMIN)
    # auth_client is "admin"; promote first so the target is not the last admin.
    response = auth_client.delete("/api/users/admin")
    assert response.status_code == 400
    assert "own account" in response.json()["error"]


def test_delete_user_removes_access(auth_client):
    from fastapi.testclient import TestClient

    make_user(auth_client, "mallory", "mallory-password")
    other = TestClient(auth_client.app)
    with other:
        assert other.post("/api/login",
                          json={"username": "mallory", "password": "mallory-password"}).status_code == 200
        assert auth_client.delete("/api/users/mallory").status_code == 200
        assert other.post("/api/login",
                          json={"username": "mallory", "password": "mallory-password"}).status_code == 401
        assert other.get("/api/devices").status_code == 401


# --------------------------------------------------------------------------- #
# login edge cases
# --------------------------------------------------------------------------- #
def test_login_requires_a_username_once_there_are_several(auth_client, client):
    make_user(auth_client, "nina", "nina-password")
    ambiguous = client.post("/api/login", json={"password": "nina-password"})
    assert ambiguous.status_code == 400
    assert "username is required" in ambiguous.json()["error"]


def test_single_user_can_log_in_without_a_username(client, config):
    # The fixture install has only the migrated "admin" user.
    assert client.post("/api/login", json={"password": PASSWORD}).status_code == 200


def test_unknown_user_and_wrong_password_look_identical(client, auth_client):
    make_user(auth_client, "oscar", "oscar-password")
    unknown = client.post("/api/login", json={"username": "nobody", "password": "whatever"})
    wrong = client.post("/api/login", json={"username": "oscar", "password": "whatever"})
    assert unknown.status_code == wrong.status_code == 401
    assert unknown.json()["error"] == wrong.json()["error"]


def test_api_key_still_works_and_is_admin(config, client, auth_client, monkeypatch):
    key = config.api_key()
    assert client.get("/api/bootstrap", headers={"X-API-Key": key}).status_code == 200
    assert client.get("/api/users", headers={"X-API-Key": key}).status_code == 200


# --------------------------------------------------------------------------- #
# first-run setup
# --------------------------------------------------------------------------- #
def test_setup_creates_the_first_admin_when_empty(tmp_path):
    """A brand new install has no users, so /api/setup must work."""
    from fastapi.testclient import TestClient

    from smarthome.config import Config
    from smarthome.server import create_app

    data_dir = tmp_path / "fresh"
    data_dir.mkdir()
    # bootstrap_password=False leaves the config without a legacy hash, which is
    # the state a new install starts in.
    config = Config.load(data_dir / "config.json", bootstrap_password=False)
    config.save()

    app = create_app(str(config.path))
    with TestClient(app) as fresh:
        assert fresh.get("/api/me").json()["needs_setup"] is True
        created = fresh.post("/api/setup", json={"username": "owner", "password": "owner-password"})
        assert created.status_code == 200, created.text
        assert fresh.get("/api/me").json()["user"]["username"] == "owner"
        # Only once.
        again = fresh.post("/api/setup", json={"username": "other", "password": "other-password"})
        assert again.status_code == 409


def test_serve_does_not_precreate_an_admin(tmp_path, monkeypatch):
    """`python -m smarthome serve` used to generate a config password, which
    created an admin behind the user's back and hid the setup page.

    Exercises the same code path in-process: cmd_serve builds its Config with
    bootstrap_password=False, then hands the file to create_app.
    """
    from smarthome.cli import _config
    class Args:
        config = None

    data_dir = tmp_path / "serve"
    data_dir.mkdir()
    config_path = data_dir / "config.json"
    Args.config = str(config_path)
    monkeypatch.setenv("SMARTHOME_CONFIG", str(config_path))
    monkeypatch.delenv("SMARTHOME_DATA_DIR", raising=False)

    config = _config(Args)
    assert config.path == config_path
    assert not config.auth.get("password_hash"), "serve must not generate an admin password"
    assert config.generated_password is None


def test_empty_install_needs_setup(tmp_path):
    """A database with no users must advertise setup, not reject every login."""
    from fastapi.testclient import TestClient

    from smarthome.config import Config
    from smarthome.server import create_app

    data_dir = tmp_path / "blank"
    data_dir.mkdir()
    config = Config.load(data_dir / "config.json", bootstrap_password=False)
    config.save()

    with TestClient(create_app(str(config.path))) as blank:
        assert blank.get("/api/me").json()["needs_setup"] is True
        # The setup page is what a browser lands on.
        page = blank.get("/")
        assert page.status_code == 200
        assert "setup-form" in page.text


def test_legacy_config_password_is_still_honoured(tmp_path):
    """An install from before the users table keeps working after upgrade."""
    from fastapi.testclient import TestClient

    from smarthome import security
    from smarthome.config import Config
    from smarthome.server import create_app

    data_dir = tmp_path / "legacy"
    data_dir.mkdir()
    config = Config.load(data_dir / "config.json", bootstrap_password=False)
    config.data["auth"]["password_hash"] = security.hash_password("legacy-password")
    config.save()

    with TestClient(create_app(str(config.path))) as legacy:
        assert legacy.get("/api/me").json()["needs_setup"] is False
        assert legacy.post("/api/login", json={"password": "legacy-password"}).status_code == 200
        users = legacy.get("/api/users").json()["users"]
        assert [u["username"] for u in users] == ["admin"]
        assert users[0]["role"] == "admin"


def test_login_blocked_after_repeated_failures(client, auth_client):
    make_user(auth_client, "peggy", "peggy-password")
    for _ in range(6):
        client.post("/api/login", json={"username": "peggy", "password": "wrong-password"})
    blocked = client.post("/api/login", json={"username": "peggy", "password": "peggy-password"})
    assert blocked.status_code == 429
