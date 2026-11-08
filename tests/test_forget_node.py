"""Forgetting a node must remove it even while it is still connected.

Regression: ``Hub.delete_node`` deleted the database row but left the live
WebSocket session in ``Hub._sessions``.  ``snapshot()`` lists live sessions
first, so the node stayed visible in the UI and kept heartbeating -- pressing
"Forget" looked like it did nothing.
"""

from __future__ import annotations

import asyncio
import time

from .test_e2e import (  # noqa: F401 - `live` is a pytest fixture
    LiveServer,
    live,
    make_agent,
    run_agent,
    stop_agent,
    wait_until,
)


def test_forget_removes_a_connected_node(live: LiveServer):
    async def scenario():
        token = live.issue_token("esp-forget")
        agent = make_agent(live, "esp-forget", token)
        task = await run_agent(agent)
        try:
            # It is connected and announced.
            await wait_until(
                lambda: any(n["node_id"] == "esp-forget" and n["online"]
                            for n in live.get("/api/nodes").json()["nodes"]),
                label="node online",
            )

            client = live.login()
            try:
                response = client.delete("/api/nodes/esp-forget")
                assert response.status_code == 200, response.text
            finally:
                client.close()

            # Gone from the API *immediately*, not just from the database.
            after = live.get("/api/nodes").json()["nodes"]
            assert [n["node_id"] for n in after] == [], (
                "the node must disappear while still connected; got %r" % after
            )

            # Its devices survive, just unassigned -- they are not deleted with
            # the board.
            devices = live.devices()
            assert "e2e-strip" in devices
            assert devices["e2e-strip"].get("node_id") in (None, "")

            # The board's socket was closed, so its agent goes offline and
            # cannot immediately re-register with the revoked token.
            await wait_until(lambda: not agent.online, timeout=10,
                             label="agent disconnected")
        finally:
            await stop_agent(agent, task)

    asyncio.run(scenario())


def test_forgotten_node_stays_gone_after_reconnect(live: LiveServer):
    """A forgotten board must not silently reappear using its old token."""
    async def scenario():
        token = live.issue_token("esp-once")
        agent = make_agent(live, "esp-once", token)
        task = await run_agent(agent)
        try:
            await wait_until(
                lambda: live.get("/api/nodes").json()["nodes"], label="node present"
            )

            client = live.login()
            try:
                assert client.delete("/api/nodes/esp-once").status_code == 200
            finally:
                client.close()

            await wait_until(lambda: not agent.online, timeout=15,
                             label="agent disconnected")
            # Give it time to retry with the old token.
            time.sleep(2)
            nodes = live.get("/api/nodes").json()["nodes"]
            assert nodes == [], "a forgotten node must not re-register: %r" % nodes
        finally:
            await stop_agent(agent, task)

    asyncio.run(scenario())
