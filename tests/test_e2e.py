"""End-to-end tests: the real firmware against a real server over a real socket.

This is the test that proves the *whole loop* works -- uvicorn serving the
WebSocket, the hub reconciling, the MicroPython client reconnecting, the device
driver applying commands -- with no mocks in the middle.

The firmware runs under CPython here (``shnode`` is portable), which is the same
code deployed to the board; ``scripts/mpy_imports.py`` covers the parts that
only a real MicroPython interpreter can check.
"""

from __future__ import annotations

import asyncio
import json
import socket
import sys
import threading
import time
from pathlib import Path

import pytest
import uvicorn

ROOT = Path(__file__).resolve().parent.parent
FIRMWARE = ROOT / "micropython"
if str(FIRMWARE) not in sys.path:
    sys.path.insert(0, str(FIRMWARE))

from shnode.agent import Agent  # noqa: E402
from shnode.devices.pwm_led_strip import PwmLedStrip  # noqa: E402
from shnode.devices.relay import Relay  # noqa: E402

from smarthome import security  # noqa: E402
from smarthome.config import Config  # noqa: E402
from smarthome.server import create_app  # noqa: E402
from smarthome.store import Store  # noqa: E402

PASSWORD = "e2e-password"


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class LiveServer:
    """uvicorn on a background thread, sharing its database with the test."""

    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        config_path = data_dir / "config.json"
        self.config = Config.load(config_path, bootstrap_password=False)
        self.config.data["auth"]["password_hash"] = security.hash_password(PASSWORD)
        self.config.data["devices"].update({
            "heartbeat_s": 0.5,
            "offline_after_s": 2.0,
            "ack_timeout_s": 0.4,
            "ack_retries": 2,
        })
        self.config.save()
        self.app = create_app(str(config_path))
        self.port = free_port()
        self.url = "http://127.0.0.1:%d" % self.port
        self.server = None
        self.thread = None

    def start(self) -> None:
        config = uvicorn.Config(
            self.app,
            host="127.0.0.1",
            port=self.port,
            log_level="warning",
            ws_ping_interval=None,
            ws_ping_timeout=None,
            lifespan="on",
        )
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, name="e2e-server", daemon=True)
        self.thread.start()
        deadline = time.time() + 20
        while time.time() < deadline:
            if getattr(self.server, "started", False):
                return
            time.sleep(0.05)
        raise RuntimeError("server did not start")

    def stop(self) -> None:
        if self.server is not None:
            self.server.should_exit = True
        if self.thread is not None:
            self.thread.join(timeout=10)

    # -- helpers ---------------------------------------------------------- #
    def login(self):
        import httpx

        client = httpx.Client(base_url=self.url, timeout=10, follow_redirects=True)
        response = client.post("/api/login", json={"password": PASSWORD})
        assert response.status_code == 200, response.text
        return client

    def issue_token(self, node_id: str) -> str:
        client = self.login()
        try:
            response = client.post("/api/nodes/%s/token" % node_id, json={})
            return response.json()["token"]
        finally:
            client.close()

    def devices(self) -> dict:
        client = self.login()
        try:
            payload = client.get("/api/devices").json()
        finally:
            client.close()
        return {device["uid"]: device for device in payload["devices"]}

    def post(self, path: str, body=None):
        client = self.login()
        try:
            return client.post(path, json=body if body is not None else {})
        finally:
            client.close()

    def get(self, path: str, params=None):
        client = self.login()
        try:
            return client.get(path, params=params)
        finally:
            client.close()


@pytest.fixture()
def live(tmp_path: Path):
    data_dir = tmp_path / "live"
    data_dir.mkdir()
    server = LiveServer(data_dir)
    server.start()
    try:
        yield server
    finally:
        server.stop()


async def wait_until(predicate, timeout: float = 10.0, interval: float = 0.05, label: str = ""):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        result = predicate()
        if result:
            return result
        last = result
        await asyncio.sleep(interval)
    raise AssertionError("timed out waiting for %s (last=%r)" % (label or predicate, last))


def make_agent(server: LiveServer, node_id: str, token: str) -> Agent:
    agent = Agent({
        "server": server.url,
        "node_id": node_id,
        "token": token,
        "wifi_ssid": "",
        "heartbeat_s": 1,
        "report_s": 5,
        "request_timeout_s": 10,
    })
    agent.add(PwmLedStrip("e2e-strip", {"pin": 4}, name="E2E strip"))
    agent.add(Relay("e2e-relay", {"pin": 5}, name="E2E relay"))
    return agent


async def run_agent(agent: Agent):
    task = asyncio.create_task(agent.main())
    return task


async def stop_agent(agent: Agent, task) -> None:
    agent.stop()
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):  # noqa: BLE001
        pass
    for device in agent.devices:
        await device.stop()


# --------------------------------------------------------------------------- #
# the happy path
# --------------------------------------------------------------------------- #
async def test_firmware_connects_and_registers_devices(live: LiveServer):
    token = live.issue_token("e2e-node")
    agent = make_agent(live, "e2e-node", token)
    task = await run_agent(agent)
    try:
        await wait_until(lambda: "e2e-strip" in live.devices(), label="device registration")
        devices = live.devices()
        assert devices["e2e-strip"]["node_id"] == "e2e-node"
        assert devices["e2e-strip"]["online"] is True
        assert devices["e2e-strip"]["reported"]["brightness"] == 100
        assert devices["e2e-relay"]["reported"]["power"] is False
        assert agent.online is True
    finally:
        await stop_agent(agent, task)


async def test_command_round_trip_reaches_the_driver(live: LiveServer):
    token = live.issue_token("e2e-node")
    agent = make_agent(live, "e2e-node", token)
    task = await run_agent(agent)
    try:
        await wait_until(lambda: "e2e-strip" in live.devices(), label="registration")
        strip = agent.device("e2e-strip")
        response = live.post("/api/devices/e2e-strip/props",
                             {"props": {"power": True, "brightness": 35, "fade_ms": 0}})
        assert response.status_code == 200, response.text
        await wait_until(lambda: strip.snapshot()["power"] is True, label="hardware on")
        assert strip._current_duty == strip._table[35]
        await wait_until(lambda: live.devices()["e2e-strip"]["reported"]["brightness"] == 35,
                         label="reported brightness")
        device = live.devices()["e2e-strip"]
        assert device["desired"]["brightness"] == 35
        assert device["pending"] == []
        assert device["error"] == ""
    finally:
        await stop_agent(agent, task)


async def test_action_runs_on_the_board(live: LiveServer):
    token = live.issue_token("e2e-node")
    agent = make_agent(live, "e2e-node", token)
    task = await run_agent(agent)
    try:
        await wait_until(lambda: "e2e-relay" in live.devices(), label="registration")
        relay = agent.device("e2e-relay")
        response = live.post("/api/devices/e2e-relay/action",
                             {"action": "pulse", "args": {"ms": 100}})
        assert response.status_code == 200, response.text
        await wait_until(lambda: relay.snapshot()["power"] is True, label="pulse on")
        await wait_until(lambda: relay.snapshot()["power"] is False, timeout=5, label="pulse off")
    finally:
        await stop_agent(agent, task)


async def test_state_is_reported_when_a_device_changes_by_itself(live: LiveServer):
    token = live.issue_token("e2e-node")
    agent = make_agent(live, "e2e-node", token)
    task = await run_agent(agent)
    try:
        await wait_until(lambda: "e2e-relay" in live.devices(), label="registration")
        # The board changes its own state (a physical button, a timer, ...).
        await agent.device("e2e-relay").action_pulse(ms=150)
        await wait_until(lambda: live.devices()["e2e-relay"]["reported"]["power"] is True,
                         label="unsolicited report")
        await wait_until(lambda: live.devices()["e2e-relay"]["reported"]["power"] is False,
                         timeout=5, label="relay back off")
    finally:
        await stop_agent(agent, task)


async def test_desired_state_is_reapplied_after_a_reconnect(live: LiveServer):
    token = live.issue_token("e2e-node")
    agent = make_agent(live, "e2e-node", token)
    task = await run_agent(agent)
    try:
        await wait_until(lambda: "e2e-strip" in live.devices(), label="registration")
        live.post("/api/devices/e2e-strip/props", {"props": {"power": True, "brightness": 60}})
        strip = agent.device("e2e-strip")
        await wait_until(lambda: strip.snapshot()["brightness"] == 60, label="applied")

        # Drop the link the way a Wi-Fi glitch would, and reset the hardware
        # behind the hub's back (as a board reboot would).
        await agent.ws.close()
        strip.apply({"power": False, "brightness": 100})
        assert strip.snapshot()["power"] is False

        await wait_until(lambda: strip.snapshot()["power"] is True, timeout=20,
                         label="desired state re-applied")
        assert strip.snapshot()["brightness"] == 60
        assert live.devices()["e2e-strip"]["reported"]["brightness"] == 60
    finally:
        await stop_agent(agent, task)


async def test_board_reconnects_after_the_server_restarts(live: LiveServer):
    token = live.issue_token("e2e-node")
    agent = make_agent(live, "e2e-node", token)
    # The agent's own Wi-Fi step is a no-op on CPython, so a reconnect loop works.
    task = await run_agent(agent)
    try:
        await wait_until(lambda: agent.online, label="first connection")
        port = live.port
        live.stop()
        await wait_until(lambda: not agent.online, timeout=10, label="notice the disconnect")

        restarted = LiveServer(live.data_dir)
        restarted.port = port
        restarted.url = "http://127.0.0.1:%d" % port
        restarted.start()
        try:
            # Rebuild the ASGI app pointing at the same database and port.
            await wait_until(lambda: agent.online, timeout=40, label="reconnect")
        finally:
            restarted.stop()
    finally:
        await stop_agent(agent, task)


async def test_node_with_a_bad_token_is_refused(live: LiveServer):
    agent = make_agent(live, "e2e-node", "shd_" + "x" * 40)
    task = asyncio.create_task(agent.main())
    try:
        await asyncio.sleep(2.0)
        assert agent.online is False
        assert "e2e-strip" not in live.devices()
    finally:
        await stop_agent(agent, task)


async def test_enrollment_issues_a_token_and_the_board_keeps_it(live: LiveServer, tmp_path: Path):
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({
        "server": live.url,
        "node_id": "e2e-enroll",
        "token": "",
        "heartbeat_s": 1,
        "report_s": 5,
    }), encoding="utf-8")

    response = live.post("/api/enroll", {"seconds": 60})
    assert response.status_code == 200

    agent = Agent.from_config(str(config_path), devices=[])
    agent.add(Relay("enrolled-relay", {"pin": 5}, name="Enrolled"))
    task = asyncio.create_task(agent.main())
    try:
        await wait_until(lambda: "enrolled-relay" in live.devices(), timeout=15, label="enrolment")
        assert agent.token.startswith("shd_")
        stored = json.loads(config_path.read_text(encoding="utf-8"))
        assert stored["token"] == agent.token
        assert stored["node_id"] == "e2e-enroll"
    finally:
        await stop_agent(agent, task)


async def test_temperature_history_accumulates(live: LiveServer):
    from shnode.devices.temperature_sensor import TemperatureSensor

    token = live.issue_token("e2e-sensor")
    agent = Agent({
        "server": live.url, "node_id": "e2e-sensor", "token": token,
        "heartbeat_s": 1, "report_s": 5,
    })
    agent.add(TemperatureSensor("e2e-temp", {"pin": 6, "interval_s": 1}, name="Sim temp"))
    task = await run_agent(agent)
    try:
        await wait_until(lambda: "e2e-temp" in live.devices(), label="sensor registration")
        await wait_until(lambda: live.devices()["e2e-temp"]["reported"].get("temperature") is not None,
                         label="first reading")

        def history_points():
            response = live.get("/api/devices/e2e-temp/history", {"hours": 1})
            return response.json()["points"]

        # The sensor samples every second and the hub writes a row per changed
        # reading, so two rows need a couple of samples; allow for a slow CI
        # machine without making the test hang.
        await wait_until(lambda: len(history_points()) >= 2, timeout=40, label="history rows")
        temps = [point["values"]["v"]["temperature"] for point in history_points()]
        assert all(-60 < value < 200 for value in temps)
    finally:
        await stop_agent(agent, task)
