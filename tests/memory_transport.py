"""In-memory transport used by the tests (and by ``tests/fake_node.py``).

It behaves like the WebSocket transport from the hub's point of view but lets a
test script (or a simulated node) drive both directions directly.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional


class MemoryTransport:
    def __init__(self, remote: str = "test"):
        self.remote = remote
        self._inbox: "asyncio.Queue[Optional[Dict[str, Any]]]" = asyncio.Queue()
        self.sent: List[Dict[str, Any]] = []
        self._connected = True
        self.closed_code: Optional[int] = None
        self.close_reason = ""

    # -- hub facing ------------------------------------------------------- #
    @property
    def connected(self) -> bool:
        return self._connected

    async def recv(self) -> Optional[Dict[str, Any]]:
        message = await self._inbox.get()
        if message is None:
            self._connected = False
        return message

    async def send(self, message: Dict[str, Any]) -> None:
        if not self._connected:
            raise ConnectionError("transport closed")
        self.sent.append(message)

    async def close(self, code: int = 1000, reason: str = "") -> None:
        if self.closed_code is None:
            self.closed_code = code
            self.close_reason = reason
        self._connected = False

    # -- test facing ------------------------------------------------------ #
    def push(self, message: Dict[str, Any]) -> None:
        self._inbox.put_nowait(message)

    def disconnect(self) -> None:
        self._inbox.put_nowait(None)

    async def wait_for(self, predicate, timeout: float = 2.0) -> Dict[str, Any]:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            for message in list(self.sent):
                if predicate(message):
                    return message
            await asyncio.sleep(0.005)
        raise AssertionError("timed out waiting for message; sent=%r" % (self.sent,))

    def messages(self, kind: str) -> List[Dict[str, Any]]:
        return [m for m in self.sent if m.get("t") == kind]
