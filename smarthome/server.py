"""FastAPI application: REST API, browser UI and the node WebSocket.

Endpoints
---------
Browser/API (session cookie, ``X-API-Key`` header or ``?key=`` for scripts)::

    POST   /api/login                 {"password": "..."}
    POST   /api/logout
    GET    /api/bootstrap             snapshot: devices, nodes, types, settings
    GET    /api/devices
    POST   /api/devices               create a device
    PATCH  /api/devices/{uid}         rename / edit hardware config
    DELETE /api/devices/{uid}
    POST   /api/devices/{uid}/props   {"props": {"brightness": 40}}
    POST   /api/devices/{uid}/action  {"action": "identify", "args": {}}
    GET    /api/devices/{uid}/history?hours=24
    GET    /api/events?limit=50
    GET    /api/nodes
    POST   /api/nodes/{node_id}/token   issue a new device token
    POST   /api/nodes/{node_id}/revoke
    DELETE /api/nodes/{node_id}
    POST   /api/types/reload
    GET    /api/settings ; PATCH /api/settings
    POST   /api/enroll                open the enrolment window
    WS     /ws/ui                     live updates for the browser

Node (bearer device token, or the enrolment window)::

    WS     /ws/node?node=strip-1&token=shd_...
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

from . import __version__, security
from .config import Config
from .hub import UNSET, DeviceError, Hub, OfflineNode, UnknownDevice
from .nodes import NodeAuth
from .registry import ManifestError, TypeRegistry
from .store import ROLE_ADMIN, ROLE_USER, ROLES, Store
from .ws import WebSocketTransport

LOG = logging.getLogger(__name__)

WEB_DIR = Path(__file__).resolve().parent / "web"
SESSION_COOKIE = security.SESSION_COOKIE


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #
class ApiError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


def _error_response(status: int, detail: str) -> JSONResponse:
    return JSONResponse({"error": detail}, status_code=status)


def _handle_api_error(_: Request, exc: Exception) -> JSONResponse:
    # Order matters: ``OfflineNode`` is a ``DeviceError``.
    if isinstance(exc, ApiError):
        return _error_response(exc.status, exc.detail)
    if isinstance(exc, UnknownDevice):
        return _error_response(404, str(exc))
    if isinstance(exc, OfflineNode):
        return _error_response(409, str(exc))
    if isinstance(exc, (DeviceError, ManifestError)):
        return _error_response(400, str(exc))
    LOG.exception("unhandled API error")
    return _error_response(500, "internal server error")


# --------------------------------------------------------------------------- #
# Application state
# --------------------------------------------------------------------------- #
class AppState:
    def __init__(self, config: Config, store: Store, registry: TypeRegistry, hub: Hub, auth: NodeAuth):
        self.config = config
        self.store = store
        self.registry = registry
        self.hub = hub
        self.auth = auth
        self.started = time.time()
        self.login_failures: Dict[str, list[float]] = {}
        self.ui_sockets = 0

    # -- login throttling -------------------------------------------------- #
    def login_blocked(self, client: str) -> float:
        limit = int(self.config.auth.get("login_max_failures", 5))
        window = float(self.config.auth.get("login_lockout_s", 60))
        now = time.time()
        attempts = [t for t in self.login_failures.get(client, []) if now - t < window]
        self.login_failures[client] = attempts
        if len(attempts) >= limit:
            return window - (now - attempts[0])
        return 0.0

    def note_login_failure(self, client: str) -> None:
        self.login_failures.setdefault(client, []).append(time.time())


def _session_user(state: AppState, request: Request) -> Optional[Dict[str, Any]]:
    """The signed-in user for this request, or ``None``.

    Credentials live in the database, not in config.json, so a password change
    takes effect immediately for every request without restarting the service.
    The cookie carries the user's ``updated`` stamp; if it moved on (a password
    or role change) the session is treated as stale.
    """
    cookie = request.cookies.get(SESSION_COOKIE)
    if not cookie:
        return None
    payload = security.verify_session(cookie, state.config.session_secret())
    if not payload:
        return None
    username = payload.get("sub")
    if not isinstance(username, str) or not username:
        return None
    user = state.store.get_user(username)
    if user is None:
        return None                      # deleted
    issued = payload.get("ver", 0)
    try:
        issued = float(issued)
    except (TypeError, ValueError):
        return None
    if issued != float(user.get("updated") or 0):
        return None                      # password or role changed
    return user


def _api_key_ok(state: AppState, request: Request) -> bool:
    key = request.headers.get("x-api-key") or request.query_params.get("key")
    if not key:
        return False
    api_key = state.config.api_key()
    return bool(api_key and security.safe_compare(key, api_key))


def _is_logged_in(state: AppState, request: Request) -> bool:
    return _session_user(state, request) is not None or _api_key_ok(state, request)


def _require_login(state: AppState, request: Request) -> Optional[Dict[str, Any]]:
    user = _session_user(state, request)
    if user is not None:
        return user
    if _api_key_ok(state, request):
        return {"username": "api-key", "role": ROLE_ADMIN, "display_name": "API key"}
    raise ApiError(401, "authentication required")


def _require_admin(state: AppState, request: Request) -> Dict[str, Any]:
    user = _require_login(state, request)
    if user is None or user.get("role") != ROLE_ADMIN:
        raise ApiError(403, "administrator access required")
    return user


async def _json_body(request: Request) -> Dict[str, Any]:
    raw = await request.body()
    if not raw:
        return {}
    if len(raw) > 256 * 1024:
        raise ApiError(413, "request body too large")
    try:
        payload = json.loads(raw)
    except ValueError:
        raise ApiError(400, "invalid JSON body")
    if not isinstance(payload, dict):
        raise ApiError(400, "JSON body must be an object")
    return payload


def _cookie_response(payload: Dict[str, Any], state: AppState, cookie: Optional[str]) -> Response:
    response = JSONResponse(payload)
    if cookie is not None:
        response.set_cookie(
            SESSION_COOKIE,
            cookie,
            max_age=int(state.config.auth.get("session_ttl_hours", 336) * 3600),
            httponly=True,
            samesite="lax",
            secure=bool(state.config.auth.get("cookie_secure", False)),
            path="/",
        )
    return response


# --------------------------------------------------------------------------- #
# App factory
# --------------------------------------------------------------------------- #
def create_app(config_path: Optional[str] = None) -> FastAPI:
    # bootstrap_password=False: with a user database, generating a throwaway
    # password in config.json is pointless, and it would auto-create an admin
    # that blocks first-run setup.
    config = Config.load(config_path, bootstrap_password=False)
    store = Store(config.db_path)
    registry = TypeRegistry(config.device_types_dirs)
    registry.reload()
    hub = Hub(config, store, registry)
    auth = NodeAuth(store, require_tokens=bool(config.auth.get("require_node_tokens", True)))
    state = AppState(config, store, registry, hub, auth)
    _bootstrap_users(config, store)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        await hub.start()
        if config.generated_password:
            _bootstrap_banner(config)
        LOG.info("SmartHome %s ready on http://%s:%s", __version__,
                 config.server["host"], config.server["port"])
        try:
            yield
        finally:
            await hub.stop()
            store.close()

    app = FastAPI(title="SmartHome", version=__version__, docs_url=None, redoc_url=None,
                  openapi_url=None, lifespan=lifespan)
    app.state.smart = state
    for exception_class in (ApiError, UnknownDevice, DeviceError, OfflineNode, ManifestError):
        app.add_exception_handler(exception_class, _handle_api_error)

    _register_api(app, state)
    _register_ui(app, state)
    _register_node_ws(app, state)
    return app


def _bootstrap_users(config: Config, store: Store) -> None:
    """Create the first administrator from the legacy config, once.

    Earlier versions kept a single admin password in config.json.  If the
    database has no users yet, carry that hash over so existing installs keep
    working, and record which account it was.
    """
    if store.count_users() > 0:
        return
    legacy_hash = (config.auth.get("password_hash") or "").strip()
    if not legacy_hash:
        return
    try:
        store.create_user("admin", legacy_hash, role=ROLE_ADMIN, display_name="Administrator")
    except Exception:  # noqa: BLE001 - a concurrent start may have won
        if store.count_users() == 0:
            raise
        return
    LOG.info("migrated the legacy admin password into the user database")


def _bootstrap_banner(config: Config) -> None:
    password = config.generated_password
    lines = [
        "",
        "=" * 68,
        "  SmartHome is ready.  Open  http://<raspberry-pi-ip>:%d" % config.server["port"],
        "",
        "  Admin password (generated once, shown only now):",
        "      %s" % password,
        "",
        "  Change it later:  python -m smarthome.cli set-password",
        "=" * 68,
        "",
    ]
    print("\n".join(lines), flush=True)


# --------------------------------------------------------------------------- #
# REST API
# --------------------------------------------------------------------------- #
def _register_api(app: FastAPI, state: AppState) -> None:

    @app.post("/api/login")
    async def login(request: Request) -> Response:
        client = request.client.host if request.client else "unknown"
        blocked = state.login_blocked(client)
        if blocked > 0:
            raise ApiError(429, "too many attempts, retry in %ds" % int(blocked + 1))
        body = await _json_body(request)
        password = body.get("password")
        if not isinstance(password, str) or not password:
            state.note_login_failure(client)
            raise ApiError(400, "password is required")
        username = body.get("username")
        if not isinstance(username, str) or not username.strip():
            # Single-user convenience: with exactly one account, "password" is
            # enough.  With several, the username is required.
            users = state.store.list_users()
            if len(users) == 1:
                username = users[0]["username"]
            else:
                state.note_login_failure(client)
                raise ApiError(400, "username is required")
        username = username.strip()
        user = state.store.get_user(username)
        if user is None:
            # Hash anyway so an unknown user costs the same as a wrong password.
            security.verify_password(password, security.hash_password("dummy-password-for-timing"))
            state.note_login_failure(client)
            await asyncio.sleep(0.25)
            raise ApiError(401, "wrong username or password")
        if not security.verify_password(password, user["password_hash"]):
            state.note_login_failure(client)
            await asyncio.sleep(0.25)
            raise ApiError(401, "wrong username or password")
        state.login_failures.pop(client, None)
        await asyncio.to_thread(state.store.touch_login, username)
        cookie = security.new_session_cookie(
            state.config.session_secret(),
            int(state.config.auth.get("session_ttl_hours", 336)),
            subject=username,
            role=user.get("role", ROLE_USER),
            version=float(user.get("updated") or 0),
        )
        return _cookie_response(
            {"ok": True, "user": {"username": username, "role": user.get("role", ROLE_USER)}},
            state, cookie,
        )

    @app.post("/api/logout")
    async def logout() -> Response:
        response = JSONResponse({"ok": True})
        response.delete_cookie(SESSION_COOKIE, path="/")
        return response

    @app.get("/api/me")
    async def me(request: Request) -> Any:
        user = _session_user(state, request)
        authenticated = user is not None or _api_key_ok(state, request)
        if user is None and authenticated:
            user = {"username": "api-key", "role": ROLE_ADMIN, "display_name": "API key"}
        return {
            "authenticated": authenticated,
            "title": state.config.ui.get("title", "SmartHome"),
            "version": __version__,
            "needs_setup": state.store.count_users() == 0,
            "user": None if user is None else {
                "username": user.get("username"),
                "role": user.get("role", ROLE_USER),
                "display_name": user.get("display_name", ""),
            },
        }

    @app.get("/api/bootstrap")
    async def bootstrap(request: Request) -> Any:
        _require_login(state, request)
        return state.hub.snapshot()

    @app.get("/api/devices")
    async def list_devices(request: Request) -> Any:
        _require_login(state, request)
        return {"devices": state.hub.list_devices()}

    @app.post("/api/devices")
    async def create_device(request: Request) -> Response:
        _require_login(state, request)
        body = await _json_body(request)
        device = await asyncio.to_thread(
            state.hub.create_device,
            uid=body.get("uid") or None,
            type_id=str(body.get("type") or ""),
            name=body.get("name") or None,
            room=str(body.get("room") or ""),
            node_id=body.get("node_id") or None,
            config=body.get("config") or {},
            desired=body.get("desired") or {},
        )
        return JSONResponse(device, status_code=201)

    @app.patch("/api/devices/{uid}")
    async def update_device(uid: str, request: Request) -> Any:
        _require_login(state, request)
        body = await _json_body(request)
        node_id: Any = UNSET
        if "node_id" in body:
            node_id = body["node_id"] or None
        return await asyncio.to_thread(
            state.hub.update_device,
            uid,
            name=body.get("name"),
            room=body.get("room"),
            node_id=node_id,
            config=body.get("config"),
            sort_index=body.get("sort_index"),
        )

    @app.delete("/api/devices/{uid}")
    async def delete_device(uid: str, request: Request) -> Any:
        _require_login(state, request)
        await asyncio.to_thread(state.hub.delete_device, uid)
        return {"ok": True}

    @app.post("/api/devices/{uid}/props")
    async def set_props(uid: str, request: Request) -> Any:
        _require_login(state, request)
        body = await _json_body(request)
        props = body.get("props")
        if not isinstance(props, dict):
            raise ApiError(400, "props must be an object")
        return await state.hub.set_props(uid, props)

    @app.post("/api/devices/{uid}/action")
    async def run_action(uid: str, request: Request) -> Any:
        _require_login(state, request)
        body = await _json_body(request)
        action = body.get("action")
        if not isinstance(action, str) or not action:
            raise ApiError(400, "action is required")
        args = body.get("args") or {}
        if not isinstance(args, dict):
            raise ApiError(400, "args must be an object")
        return await state.hub.run_action(uid, action, args)

    @app.get("/api/devices/{uid}/history")
    async def history(uid: str, request: Request) -> Any:
        _require_login(state, request)
        hours = 24.0
        try:
            hours = float(request.query_params.get("hours", "24"))
        except ValueError:
            pass
        points = await asyncio.to_thread(state.hub.history, uid, hours=hours)
        return {"uid": uid, "hours": hours, "points": points}

    @app.get("/api/events")
    async def events(request: Request) -> Any:
        _require_login(state, request)
        try:
            limit = max(1, min(500, int(request.query_params.get("limit", "50"))))
        except ValueError:
            limit = 50
        return {"events": state.store.list_events(limit=limit)}

    # -- nodes ---------------------------------------------------------- #
    @app.get("/api/nodes")
    async def list_nodes(request: Request) -> Any:
        _require_login(state, request)
        return {
            "nodes": state.hub.list_nodes(),
            "require_tokens": state.auth.require_tokens,
            **state.auth.describe(),
        }

    @app.post("/api/nodes/{node_id}/token")
    async def issue_token(node_id: str, request: Request) -> Any:
        _require_login(state, request)
        await _json_body(request)
        token = await asyncio.to_thread(state.auth.issue_token, node_id)
        state.hub.broadcast({"t": "nodes_changed"})
        return {"node_id": node_id, "token": token}

    @app.post("/api/nodes/{node_id}/revoke")
    async def revoke_token(node_id: str, request: Request) -> Any:
        _require_login(state, request)
        state.auth.revoke(node_id)
        state.hub.broadcast({"t": "nodes_changed"})
        return {"ok": True, "node_id": node_id}

    @app.delete("/api/nodes/{node_id}")
    async def delete_node(node_id: str, request: Request) -> Any:
        _require_admin(state, request)
        state.auth.revoke(node_id)
        await state.hub.forget_node(node_id)
        return {"ok": True}

    @app.post("/api/enroll")
    async def enroll(request: Request) -> Any:
        _require_login(state, request)
        body = await _json_body(request)
        try:
            seconds = float(body.get("seconds", 120))
        except (TypeError, ValueError):
            seconds = 120.0
        if body.get("close"):
            state.auth.close_enrollment()
            state.hub.broadcast({"t": "enrollment", **state.auth.describe()})
            return state.auth.describe()
        state.auth.open_enrollment(seconds)
        state.hub.broadcast({"t": "enrollment", **state.auth.describe()})
        return state.auth.describe()

    # -- types / settings ------------------------------------------------ #
    @app.post("/api/types/reload")
    async def reload_types(request: Request) -> Any:
        _require_login(state, request)
        errors = await asyncio.to_thread(state.hub.reload_types)
        return {"types": state.registry.public(), "errors": errors}

    @app.get("/api/settings")
    async def get_settings(request: Request) -> Any:
        _require_login(state, request)
        return {
            "settings": state.hub._public_settings(),
            "auth": state.auth.describe(),
        }

    @app.patch("/api/settings")
    async def update_settings(request: Request) -> Any:
        _require_login(state, request)
        body = await _json_body(request)
        settings = await asyncio.to_thread(state.hub.update_settings, body)
        return {"settings": settings}

    @app.post("/api/password")
    async def change_password(request: Request) -> Any:
        """Change the signed-in user's own password.

        Writes to the database, so the new password is live on the next request
        without restarting anything.
        """
        user = _require_login(state, request)
        body = await _json_body(request)
        new = body.get("new")
        current = body.get("current")

        if user.get("username") == "api-key":
            raise ApiError(403, "use the admin account, not the API key")
        if not isinstance(new, str) or len(new) < 8:
            raise ApiError(400, "new password must be at least 8 characters")
        record = state.store.get_user(user["username"])
        if record is None:
            raise ApiError(401, "account no longer exists")
        if not security.verify_password(str(current or ""), record["password_hash"]):
            raise ApiError(403, "current password is wrong")

        password_hash = await asyncio.to_thread(security.hash_password, new)
        updated = await asyncio.to_thread(
            state.store.set_user_password, user["username"], password_hash
        )
        if not updated:
            raise ApiError(500, "could not update the password")

        # Re-issue this browser's cookie so it survives its own version bump,
        # and so other browsers holding the old version are logged out.
        fresh = state.store.get_user_public(user["username"]) or {}
        cookie = security.new_session_cookie(
            state.config.session_secret(),
            int(state.config.auth.get("session_ttl_hours", 336)),
            subject=user["username"],
            role=fresh.get("role", ROLE_USER),
            version=float(fresh.get("updated") or 0),
        )
        state.store.add_event("password_changed", name=user["username"])
        return _cookie_response({"ok": True}, state, cookie)

    # -- users ---------------------------------------------------------- #
    @app.get("/api/users")
    async def list_users(request: Request) -> Any:
        _require_admin(state, request)
        return {"users": state.store.list_users()}

    @app.post("/api/users")
    async def create_user(request: Request) -> Response:
        _require_admin(state, request)
        body = await _json_body(request)
        username = body.get("username")
        password = body.get("password")
        role = body.get("role") or ROLE_USER
        display_name = str(body.get("display_name") or "").strip()

        error = _validate_username(username)
        if error:
            raise ApiError(400, error)
        if role not in ROLES:
            raise ApiError(400, "role must be one of: %s" % ", ".join(ROLES))
        if not isinstance(password, str) or len(password) < 8:
            raise ApiError(400, "password must be at least 8 characters")
        if await asyncio.to_thread(state.store.get_user, username) is not None:
            raise ApiError(400, "a user named %r already exists" % username)

        password_hash = await asyncio.to_thread(security.hash_password, password)
        try:
            user = await asyncio.to_thread(
                state.store.create_user, username, password_hash,
                role=role, display_name=display_name,
            )
        except Exception as exc:  # noqa: BLE001 - unique constraint race
            raise ApiError(400, "could not create the user: %s" % exc)
        state.store.add_event("user_created", name=username, data={"role": role})
        return JSONResponse(user, status_code=201)

    @app.patch("/api/users/{username}")
    async def update_user(username: str, request: Request) -> Any:
        actor = _require_admin(state, request)
        body = await _json_body(request)
        record = await asyncio.to_thread(state.store.get_user_public, username)
        if record is None:
            raise ApiError(404, "no such user")

        role = body.get("role")
        display_name = body.get("display_name")
        if role is not None:
            if role not in ROLES:
                raise ApiError(400, "role must be one of: %s" % ", ".join(ROLES))
            if role != ROLE_ADMIN and record.get("role") == ROLE_ADMIN:
                if await asyncio.to_thread(state.store.count_admins) <= 1:
                    raise ApiError(400, "cannot demote the last administrator")
        if display_name is not None:
            display_name = str(display_name).strip()

        new_password = body.get("password")
        if new_password is not None:
            if not isinstance(new_password, str) or len(new_password) < 8:
                raise ApiError(400, "password must be at least 8 characters")
            password_hash = await asyncio.to_thread(security.hash_password, new_password)
            await asyncio.to_thread(state.store.set_user_password, username, password_hash)

        if role is not None or display_name is not None:
            await asyncio.to_thread(
                state.store.update_user, username, role=role, display_name=display_name
            )
        state.store.add_event("user_updated", name=username, data={"by": actor["username"]})
        return await asyncio.to_thread(state.store.get_user_public, username) or {}

    @app.delete("/api/users/{username}")
    async def delete_user(username: str, request: Request) -> Any:
        actor = _require_admin(state, request)
        record = await asyncio.to_thread(state.store.get_user, username)
        if record is None:
            raise ApiError(404, "no such user")
        if record.get("role") == ROLE_ADMIN:
            if await asyncio.to_thread(state.store.count_admins) <= 1:
                raise ApiError(400, "cannot delete the last administrator")
        if actor.get("username") == username:
            raise ApiError(400, "you cannot delete your own account")
        await asyncio.to_thread(state.store.delete_user, username)
        state.store.add_event("user_deleted", name=username, data={"by": actor["username"]})
        return {"ok": True}

    @app.post("/api/setup")
    async def first_run_setup(request: Request) -> Response:
        """Create the first administrator; only possible on an empty install."""
        if await asyncio.to_thread(state.store.count_users) != 0:
            raise ApiError(409, "setup has already been completed")
        body = await _json_body(request)
        username = body.get("username") or "admin"
        password = body.get("password")
        error = _validate_username(username)
        if error:
            raise ApiError(400, error)
        if not isinstance(password, str) or len(password) < 8:
            raise ApiError(400, "password must be at least 8 characters")
        password_hash = await asyncio.to_thread(security.hash_password, password)
        await asyncio.to_thread(
            state.store.create_user, username, password_hash,
            role=ROLE_ADMIN, display_name=str(body.get("display_name") or "").strip(),
        )
        state.store.add_event("user_created", name=username, data={"role": ROLE_ADMIN})
        fresh = state.store.get_user(username) or {}
        cookie = security.new_session_cookie(
            state.config.session_secret(),
            int(state.config.auth.get("session_ttl_hours", 336)),
            subject=username, role=ROLE_ADMIN, version=float(fresh.get("updated") or 0),
        )
        LOG.info("first administrator %r created through setup", username)
        return _cookie_response({"ok": True, "username": username}, state, cookie)


USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{1,31}$")


def _validate_username(username: Any) -> str:
    if not isinstance(username, str) or not username.strip():
        return "username is required"
    if not USERNAME_PATTERN.match(username.strip()):
        return ("username must be 2-32 characters: letters, digits, dot, dash "
                "or underscore, starting with a letter or digit")
    return ""


# --------------------------------------------------------------------------- #
# Browser UI
# --------------------------------------------------------------------------- #
def _register_ui(app: FastAPI, state: AppState) -> None:
    # The two HTML entry points must never be cached: the same URL serves the
    # login page or the app depending on the session cookie, and a stale copy
    # would strand a signed-in browser on the login form.
    no_store = {"Cache-Control": "no-store, must-revalidate", "Pragma": "no-cache"}

    @app.get("/")
    async def index(request: Request) -> Response:
        if not _is_logged_in(state, request):
            if state.store.count_users() == 0:
                return FileResponse(WEB_DIR / "setup.html", media_type="text/html",
                                    headers=no_store)
            return FileResponse(WEB_DIR / "login.html", media_type="text/html", headers=no_store)
        return FileResponse(WEB_DIR / "index.html", media_type="text/html", headers=no_store)

    @app.get("/setup")
    async def setup_page(request: Request) -> Response:
        if state.store.count_users() > 0:
            return RedirectResponse("/", status_code=303)
        return FileResponse(WEB_DIR / "setup.html", media_type="text/html", headers=no_store)

    @app.get("/auth")
    async def auth_landing(request: Request) -> Response:
        """Turn a ``?key=`` link into a session cookie (kiosk / bookmark use).

        Handy on a wall tablet: open ``/auth?key=...`` once and the browser stays
        signed in.  The key is *not* stored anywhere except the browser history.
        """
        key = request.query_params.get("key") or ""
        api_key = state.config.api_key()
        if not key or not api_key or not security.safe_compare(key, api_key):
            return RedirectResponse("/", status_code=303)
        cookie = security.new_session_cookie(
            state.config.session_secret(), int(state.config.auth.get("session_ttl_hours", 336))
        )
        response = RedirectResponse("/", status_code=303)
        response.set_cookie(
            SESSION_COOKIE,
            cookie,
            max_age=int(state.config.auth.get("session_ttl_hours", 336) * 3600),
            httponly=True,
            samesite="lax",
            secure=bool(state.config.auth.get("cookie_secure", False)),
            path="/",
        )
        return response

    @app.get("/healthz")
    async def healthz() -> Any:
        # ``build`` is the newest mtime among the served UI files: comparing it
        # with `git log -1` on the checkout is the quickest way to tell whether
        # a deploy actually reached /opt/smarthome.
        try:
            build = max(int(p.stat().st_mtime)
                        for p in WEB_DIR.iterdir() if p.is_file())
        except (OSError, ValueError):
            build = 0
        return {
            "ok": True,
            "version": __version__,
            "build": build,
            "uptime_s": round(time.time() - state.started, 1),
            "nodes_online": sum(1 for n in state.hub.list_nodes() if n["online"]),
            "devices": len(state.store.list_devices()),
            "ui_clients": state.ui_sockets,
        }

    @app.websocket("/ws/ui")
    async def ui_socket(websocket: WebSocket) -> None:
        cookie = websocket.cookies.get(SESSION_COOKIE)
        key = websocket.query_params.get("key")
        api_key = state.config.api_key()
        authenticated = bool(cookie and security.verify_session(cookie, state.config.session_secret()))
        if not authenticated and key and api_key and security.safe_compare(key, api_key):
            authenticated = True
        if not authenticated:
            await websocket.close(code=4401, reason="authentication required")
            return

        await websocket.accept()
        queue = state.hub.register_ui()
        state.ui_sockets += 1
        try:
            await websocket.send_text(json.dumps(_ui_frame(state.hub.snapshot()), separators=(",", ":")))
            sender = asyncio.create_task(_ui_sender(websocket, queue), name="ui-sender")
            receiver = asyncio.create_task(_ui_receiver(websocket, state), name="ui-receiver")
            done, pending = await asyncio.wait({sender, receiver}, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            for task in done:
                exc = task.exception()
                if exc and not isinstance(exc, (WebSocketDisconnect, asyncio.CancelledError)):
                    LOG.debug("ui socket closed: %s", exc)
        finally:
            state.hub.unregister_ui(queue)
            state.ui_sockets = max(0, state.ui_sockets - 1)
            try:
                await websocket.close()
            except Exception:  # noqa: BLE001
                pass

    class RevalidatingStatic(StaticFiles):
        """Static files that the browser must revalidate on every load.

        Plain StaticFiles sends no Cache-Control, so browsers apply heuristic
        caching -- an updated app.css kept being served from cache after an
        upgrade, which made UI fixes look like they did nothing.  no-cache still
        allows a conditional request, so unchanged files answer 304 and cost
        almost nothing.
        """

        async def get_response(self, path: str, scope: Any) -> Response:
            response = await super().get_response(path, scope)
            response.headers["Cache-Control"] = "no-cache, must-revalidate"
            return response

    app.mount("/static", RevalidatingStatic(directory=str(WEB_DIR)), name="static")


def _ui_frame(payload: Dict[str, Any]) -> Dict[str, Any]:
    return payload


async def _ui_sender(websocket: WebSocket, queue: "asyncio.Queue[Dict[str, Any]]") -> None:
    while True:
        message = await queue.get()
        await websocket.send_text(json.dumps(message, separators=(",", ":"), default=str))


async def _ui_receiver(websocket: WebSocket, state: AppState) -> None:
    """Answer lightweight requests sent over the UI socket (ping / resync)."""
    while True:
        text = await websocket.receive_text()
        try:
            message = json.loads(text)
        except ValueError:
            continue
        kind = message.get("t") if isinstance(message, dict) else None
        if kind == "ping":
            await websocket.send_text(json.dumps({"t": "pong", "ts": message.get("ts")}))
        elif kind == "resync":
            await websocket.send_text(json.dumps(_ui_frame(state.hub.snapshot()), separators=(",", ":")))


# --------------------------------------------------------------------------- #
# Node WebSocket
# --------------------------------------------------------------------------- #
def _register_node_ws(app: FastAPI, state: AppState) -> None:

    @app.websocket("/ws/node")
    async def node_socket(websocket: WebSocket) -> None:
        transport = WebSocketTransport(websocket)
        token = websocket.query_params.get("token") or _bearer(websocket.headers.get("authorization", ""))
        claimed = websocket.query_params.get("node") or None
        result = state.auth.authenticate(token, claimed)
        if not result.ok:
            await websocket.accept()
            await websocket.send_text(json.dumps(
                {"t": "error", "code": "unauthorized", "detail": result.reason}
            ))
            await websocket.close(code=4401, reason=result.reason or "unauthorized")
            return

        await websocket.accept()
        if result.enroll and result.node_id:
            # Hand the board its token before the hub takes over the socket.
            new_token = state.auth.issue_token(result.node_id)
            await websocket.send_text(json.dumps(
                {"t": "enrolled", "node": result.node_id, "token": new_token,
                 "server": state.config.ui.get("title", "SmartHome")},
                separators=(",", ":"),
            ))
            state.hub.broadcast({"t": "nodes_changed"})
            LOG.info("enrolled node %s from %s", result.node_id, transport.remote)

        try:
            await state.hub.serve_node(transport, token_node_id=result.node_id)
        except WebSocketDisconnect:
            LOG.info("node disconnected abruptly (%s)", transport.remote)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            LOG.exception("node connection failed (%s)", transport.remote)
        finally:
            await transport.close()


def _bearer(header: str) -> str:
    if header.lower().startswith("bearer "):
        return header[7:].strip()
    return ""
