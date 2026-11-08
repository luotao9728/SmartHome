"""SmartHome MicroPython node client (``shnode``).

A small, dependency-free implementation of the hub's node protocol that runs on
CPython (for tests on a PC) and MicroPython (ESP32-C6 / S3 / RP2-W).

The package is called ``shnode`` rather than ``smarthome`` so that it can be
imported side by side with the server package on a development machine.

Public entry point::

    from shnode.agent import Agent
    from shnode.devices.pwm_led_strip import PwmLedStrip

    agent = Agent.from_config("/config.json")
    agent.add(PwmLedStrip("strip-1", {"pin": 4}))
    agent.run_forever()

Design notes
------------
* **One websocket, reconnecting forever.**  Exponential backoff with jitter, and
  a hard reset of the backoff after the hub greets us.
* **Commands are acknowledged.**  ``cmd`` messages are applied and answered
  with an ``ack`` carrying the resulting state.  Duplicate ids are not
  re-applied (the hub retries on a missing ack).
* **Reported state is authoritative.**  Devices push ``state`` whenever
  something changes, plus a periodic full report so the hub can detect drift.
* **The main loop stays responsive.**  Blocking work (fades, pulses, sensor
  reads) happens in per-device asyncio tasks, never in the network loop.
"""

from .agent import Agent
from .device import Device, DeviceError, Unavailable
from .transport import WebsocketClient

__version__ = "1.0.0"

__all__ = [
    "Agent",
    "Device",
    "DeviceError",
    "Unavailable",
    "WebsocketClient",
    "__version__",
]
