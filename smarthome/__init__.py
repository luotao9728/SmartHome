"""SmartHome hub - a small, extensible smart-home server for a Raspberry Pi.

The hub owns the device registry, the desired/reported state model and the
WebSocket transport.  Device *types* live in ``device_types/*.json`` so new
hardware only needs a manifest plus (optionally) a MicroPython driver.
"""

__version__ = "1.0.0"

APP_NAME = "SmartHome"
