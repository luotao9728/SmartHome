#!/usr/bin/env python3
"""A simulated SmartHome node.

Runs the *real* MicroPython firmware (``micropython/shnode``) against a real
hub over a real WebSocket, but on CPython.  Two uses:

* smoke-testing a server: ``python -m tools.fake_node --server http://pi:8080``
* automated end-to-end tests (``tests/test_e2e.py``)

The drivers fall back to simulated hardware (PWM/pins are fake objects), so the
whole protocol path is exercised without a microcontroller.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FIRMWARE = ROOT / "micropython"
if str(FIRMWARE) not in sys.path:
    sys.path.insert(0, str(FIRMWARE))

from shnode.agent import Agent  # noqa: E402
from shnode.devices.pwm_led_strip import PwmLedStrip  # noqa: E402
from shnode.devices.relay import Relay  # noqa: E402
from shnode.devices.temperature_sensor import TemperatureSensor  # noqa: E402


def build_agent(server: str, node_id: str, token: str = "", *, devices=True, verbose=False) -> Agent:
    config = {
        "server": server,
        "node_id": node_id,
        "token": token,
        "wifi_ssid": "",
        "heartbeat_s": 2,
        "report_s": 5,
        "request_timeout_s": 10,
    }
    agent = Agent(config, verbose=verbose)
    if devices:
        agent.add(PwmLedStrip("strip-1", {"pin": 4, "frequency_hz": 1000}, name="Desk strip"))
        agent.add(Relay("relay-1", {"pin": 5}, name="Pump"))
        agent.add(TemperatureSensor("temp-1", {"pin": 6, "interval_s": 2}, name="Room temperature"))
    return agent


async def run(server: str, node_id: str, token: str, seconds: float, *, devices=True, verbose=False):
    agent = build_agent(server, node_id, token, devices=devices, verbose=verbose)
    task = asyncio.create_task(agent.main())
    try:
        await asyncio.wait_for(task, seconds) if seconds else await task
    except asyncio.TimeoutError:
        pass
    finally:
        agent.stop()
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        for device in agent.devices:
            try:
                await device.stop()
            except Exception:  # noqa: BLE001
                pass


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Run a simulated SmartHome node")
    parser.add_argument("--server", default=os.environ.get("SMARTHOME_SERVER", "http://127.0.0.1:8080"))
    parser.add_argument("--node", default="sim-node")
    parser.add_argument("--token", default="")
    parser.add_argument("--seconds", type=float, default=0, help="0 = run until Ctrl+C")
    parser.add_argument("--no-devices", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)
    try:
        asyncio.run(run(args.server, args.node, args.token, args.seconds,
                        devices=not args.no_devices, verbose=args.verbose))
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
