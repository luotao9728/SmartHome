"""SmartHome node firmware entry point.

Copy this file to the board's filesystem as ``/main.py``.  Edit the ``DEVICES``
list to describe the hardware attached to *this* board; everything else is
handled by the library.

    import main
    main.REBOOT_ON_ERROR = True
"""

import json
import sys

# --------------------------------------------------------------------------- #
# Configure this board
# --------------------------------------------------------------------------- #
CONFIG_PATH = "/config.json"

#: The flasher writes this file; it takes precedence over ``DEVICES`` below so a
#: board can be provisioned from the web UI without editing any source.
DEVICES_PATH = "/devices.json"

#: One entry per physical device on this board, used when /devices.json is
#: absent.  Keys of ``config`` match the ``config`` schema of the device type
#: manifest on the server.  Safe PWM pins on the C6: 4, 5, 6, 7, 10-22.
DEVICES = [
    # {"type": "pwm_led_strip", "uid": "kitchen-strip", "name": "Kitchen Light", "config": {"pin": 4, "frequency_hz": 1000}},
    # {"type": "relay", "uid": "pump-relay", "name": "Pump", "config": {"pin": 5, "active_low": False}},
    # {"type": "button", "uid": "desk-button", "name": "Desk button", "config": {"pin": 9}},
    # {"type": "temperature_sensor", "uid": "room-temp", "name": "Room temperature", "config": {"pin": 6, "interval_s": 15}},
]

#: Reboot the board if the main task ever dies (MicroPython does not restart it).
REBOOT_ON_ERROR = True

#: Delay before reboot, so the error can be read on the serial console.
REBOOT_DELAY_MS = 5000


def device_entries():
    """Device definitions: /devices.json when present, else the table above."""
    try:
        with open(DEVICES_PATH) as handle:
            loaded = json.load(handle)
    except (OSError, ValueError):
        return DEVICES
    if isinstance(loaded, list):
        return loaded
    if isinstance(loaded, dict) and isinstance(loaded.get("devices"), list):
        return loaded["devices"]
    print("main: %s is malformed, using the built-in DEVICES table" % DEVICES_PATH)
    return DEVICES


def build_devices():
    """Turn the device table into driver instances."""
    from shnode.devices.button import Button
    from shnode.devices.pwm_led_strip import PwmLedStrip
    from shnode.devices.relay import Relay
    from shnode.devices.rgb_led_strip import RgbLedStrip
    from shnode.devices.temperature_sensor import TemperatureSensor

    registry = {
        "pwm_led_strip": PwmLedStrip,
        "relay": Relay,
        "button": Button,
        "temperature_sensor": TemperatureSensor,
        "rgb_led_strip": RgbLedStrip,
    }
    devices = []
    for entry in device_entries():
        if not isinstance(entry, dict):
            continue
        type_id = entry.get("type")
        driver = registry.get(type_id)
        if driver is None:
            print("main: unknown device type %r, skipped" % (type_id,))
            continue
        devices.append(
            driver(
                entry.get("uid") or type_id,
                entry.get("config") or {},
                name=entry.get("name"),
            )
        )
    return devices


def start() -> None:
    from shnode.agent import Agent

    devices = build_devices()
    if not devices:
        print("main: DEVICES is empty - add at least one device")
    agent = Agent.from_config(CONFIG_PATH, devices=devices)
    agent.run_forever()


def _safe_fallback() -> None:
    """MicroPython keeps /boot.py running; here we just stay quiet."""
    print("main: running without a device list")


if __name__ == "__main__":
    try:
        start()
    except Exception as exc:  # noqa: BLE001
        printer = getattr(sys, "print_exception", None)
        if printer is not None:
            printer(exc)
        else:
            print("fatal: %r" % (exc,))
        if REBOOT_ON_ERROR:
            try:
                import machine
                import time

                time.sleep_ms(REBOOT_DELAY_MS)
                machine.reset()
            except Exception:  # noqa: BLE001
                pass
