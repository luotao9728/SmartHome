"""WebSocket transport adapter (FastAPI/Starlette <-> hub).

The hub only knows about :class:`smarthome.hub.NodeTransport`; this module
translates a Starlette ``WebSocket`` into that interface.  Keeping it separate
means the hub can be unit tested with a plain in-memory transport.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Dict, Optional

from . import protocol

LOG = logging.getLogger(__name__)

try:  # Starlette is required at runtime but not for unit tests of the hub.
    from starlette.websockets import WebSocket, WebSocketDisconnect
except Exception:  # pragma: no cover - only hit when FastAPI is not installed
    WebSocket = Any  # type: ignore[assignment]
    WebSocketDisconnect = Exception  # type: ignore[assignment,misc]


class WebSocketTransport:
    """Bridges a Starlette WebSocket to the hub's transport protocol."""

    def __init__(self, websocket: WebSocket):
        self.ws = websocket
        self.remote = self._remote_of(websocket)
        self._connected = True
        self._closed = False

    @staticmethod
    def _remote_of(websocket: Any) -> str:
        client = getattr(websocket, "client", None)
        host = getattr(client, "host", None)
        return str(host or "unknown")

    @property
    def connected(self) -> bool:
        return self._connected

    async def recv(self) -> Optional[Dict[str, Any]]:
        while True:
            try:
                frame = await self.ws.receive()
            except WebSocketDisconnect:
                self._connected = False
                return None
            except Exception as exc:  # noqa: BLE001
                LOG.debug("websocket receive failed: %s", exc)
                self._connected = False
                return None
            kind = frame.get("type")
            if kind == "websocket.disconnect":
                self._connected = False
                return None
            text = frame.get("text")
            if text is None:
                data = frame.get("bytes")
                if not data:
                    continue
                try:
                    text = data.decode("utf-8")
                except UnicodeDecodeError:
                    LOG.warning("node %s sent non UTF-8 bytes", self.remote)
                    continue
            if len(text) > protocol.FRAME_MAX:
                LOG.warning("node %s sent an oversized frame (%d bytes)", self.remote, len(text))
                continue
            try:
                message = json.loads(text)
            except ValueError:
                LOG.warning("node %s sent invalid JSON", self.remote)
                continue
            if not isinstance(message, dict):
                LOG.warning("node %s sent a non-object JSON value", self.remote)
                continue
            return message

    async def send(self, message: Dict[str, Any]) -> None:
        if not self._connected or self._closed:
            raise ConnectionError("transport closed")
        try:
            await self.ws.send_text(json.dumps(message, separators=(",", ":")))
        except Exception as exc:  # noqa: BLE001
            self._connected = False
            raise ConnectionError("send failed: %s" % (exc,)) from exc

    async def close(self, code: int = 1000, reason: str = "") -> None:
        if self._closed:
            return
        self._closed = True
        self._connected = False
        try:
            await self.ws.close(code=code, reason=reason)
        except Exception:  # noqa: BLE001
            pass


class ServerInfoTransport:
    """Used to send the first frame before the hub takes over the socket."""

    def __init__(self, websocket: WebSocket):
        self.ws = websocket
        self.remote = WebSocketTransport._remote_of(websocket)

    async def recv(self) -> Optional[Dict[str, Any]]:  # pragma: no cover
        return await WebSocketTransport(self.ws).recv()

    async def send(self, message: Dict[str, Any]) -> None:
        await self.ws.send_text(json.dumps(message, separators=(",", ":")))

    async def close(self, code: int = 1000, reason: str = "") -> None:
        try:
            await self.ws.close(code=code, reason=reason)
        except Exception:  # noqa: BLE001
            pass

    @property
    def connected(self) -> bool:
        return True


async def wait_for_close(transport: WebSocketTransport) -> None:
    """Drain a socket that the hub refused, so the client sees a clean close."""
    try:
        while True:
            frame = await transport.ws.receive()
            if frame.get("type") == "websocket.disconnect":
                return
    except Exception:  # noqa: BLE001
        return
    finally:
        await transport.close()


def spawn(coro: Any, name: str) -> "asyncio.Task[Any]":
    return asyncio.create_task(coro, name=name)
