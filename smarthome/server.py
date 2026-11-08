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
from .store import Store
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


def _is_logged_in(state: AppState, request: Request) -> bool:
    cookie = request.cookies.get(SESSION_COOKIE)
    if cookie and security.verify_session(cookie, state.config.session_secret()):
        return True
    key = request.headers.get("x-api-key") or request.query_params.get("key")
    api_key = state.config.api_key()
    if key and api_key and security.safe_compare(key, api_key):
        return True
    return False


def _require_login(state: AppState, request: Request) -> None:
    if not _is_logged_in(state, request):
        raise ApiError(401, "authentication required")


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
    config = Config.load(config_path)
    store = Store(config.db_path)
    registry = TypeRegistry(config.device_types_dirs)
    registry.reload()
    hub = Hub(config, store, registry)
    auth = NodeAuth(store, require_tokens=bool(config.auth.get("require_node_tokens", True)))
    state = AppState(config, store, registry, hub, auth)

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
        if not security.verify_password(password, state.config.auth["password_hash"]):
            state.note_login_failure(client)
            await asyncio.sleep(0.25)
            raise ApiError(401, "wrong password")
        state.login_failures.pop(client, None)
        cookie = security.new_session_cookie(
            state.config.session_secret(), int(state.config.auth.get("session_ttl_hours", 336))
        )
        return _cookie_response({"ok": True}, state, cookie)

    @app.post("/api/logout")
    async def logout() -> Response:
        response = JSONResponse({"ok": True})
        response.delete_cookie(SESSION_COOKIE, path="/")
        return response

    @app.get("/api/me")
    async def me(request: Request) -> Any:
        logged_in = _is_logged_in(state, request)
        return {
            "authenticated": logged_in,
            "title": state.config.ui.get("title", "SmartHome"),
            "version": __version__,
            "bootstrap": not state.config.generated_password and not state.config.auth.get("password_hash"),
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
        _require_login(state, request)
        await asyncio.to_thread(state.hub.delete_node, node_id)
        state.auth.revoke(node_id)
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
        _require_login(state, request)
        body = await _json_body(request)
        new = body.get("new")
        current = body.get("current")
        if not isinstance(new, str) or len(new) < 8:
            raise ApiError(400, "new password must be at least 8 characters")
        if state.config.auth.get("password_hash") and not security.verify_password(
            str(current or ""), state.config.auth["password_hash"]
        ):
            raise ApiError(403, "current password is wrong")
        await asyncio.to_thread(state.config.set_admin_password, new)
        return {"ok": True}


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
            return FileResponse(WEB_DIR / "login.html", media_type="text/html", headers=no_store)
        return FileResponse(WEB_DIR / "index.html", media_type="text/html", headers=no_store)

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
        return {
            "ok": True,
            "version": __version__,
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

    app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")


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
