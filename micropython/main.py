"""SmartHome node firmware entry point.

Copy this file to the board's filesystem as ``/main.py``.  Edit the ``DEVICES``
list to describe the hardware attached to *this* board; everything else is
handled by the library.

    import main
    main.REBOOT_ON_ERROR = True
"""

import sys

# --------------------------------------------------------------------------- #
# Configure this board
# --------------------------------------------------------------------------- #
CONFIG_PATH = "/config.json"

#: One entry per physical device on this board.  Keys of ``config`` match the
#: ``config`` schema of the device type manifest on the server.
DEVICES = [
    # {"type": "pwm_led_strip", "uid": "desk-strip", "name": "Desk strip", "config": {"pin": 4, "frequency_hz": 1000}},
    # {"type": "relay", "uid": "pump-relay", "name": "Pump", "config": {"pin": 5, "active_low": False}},
    # {"type": "button", "uid": "desk-button", "name": "Desk button", "config": {"pin": 9}},
    # {"type": "temperature_sensor", "uid": "room-temp", "name": "Room temperature", "config": {"pin": 6, "interval_s": 15}},
]

#: Reboot the board if the main task ever dies (MicroPython does not restart it).
REBOOT_ON_ERROR = True

#: Delay before reboot, so the error can be read on the serial console.
REBOOT_DELAY_MS = 5000


def build_devices():
    """Turn the ``DEVICES`` table into driver instances."""
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
    for entry in DEVICES:
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
