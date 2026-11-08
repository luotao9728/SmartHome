# Writing a new device type

Worked example: a **servo cover** (blind/roller shutter) on an ESP32-C6.

## 1. The manifest — `device_types/servo_cover.json`

The manifest is the contract between the hub, the UI and the firmware.  Start
from an existing type and delete what you do not need.

```json
{
  "id": "servo_cover",
  "name": "Servo Cover",
  "version": 1,
  "category": "cover",
  "icon": "door",
  "order": 50,
  "description": "A blind driven by a hobby servo on a PWM pin.",
  "properties": {
    "position": {
      "type": "int", "min": 0, "max": 100, "default": 0,
      "unit": "%", "control": "slider", "label": "Position"
    },
    "open": {
      "type": "bool", "default": false, "control": "switch", "label": "Open"
    },
    "calibrate_ms": {
      "type": "int", "min": 100, "max": 5000, "default": 800,
      "unit": "ms", "control": "number", "advanced": true,
      "label": "Calibration sweep"
    }
  },
  "config": {
    "pin": {"type": "int", "min": 0, "max": 48, "default": 10,
            "required": true, "label": "Servo pin"},
    "min_us": {"type": "int", "min": 200, "max": 1200, "default": 500,
               "unit": "µs", "label": "Closed pulse"},
    "max_us": {"type": "int", "min": 1800, "max": 3000, "default": 2500,
               "unit": "µs", "label": "Open pulse"}
  },
  "actions": {
    "calibrate": {
      "label": "Calibrate",
      "description": "Sweep the full range so you can check the linkage.",
      "args": {}
    }
  },
  "events": {
    "stalled": {"label": "Stalled"}
  }
}
```

What each part buys you:

| Field | Effect |
| --- | --- |
| `properties.*.type` | server-side coercion and API validation |
| `min` / `max` | slider bounds **and** server rejection outside them |
| `unit`, `label` | rendered next to the control |
| `control` | which widget: `switch`, `slider`, `number`, `select`, `color`, `text`, `readonly` |
| `advanced: true` | hidden on the card, shown in the device's Advanced tab |
| `settable: false` | read-only: shown, never sent, never in `desired` |
| `config` | hardware wizard fields; validated on create and edit |
| `actions` | buttons on the card; `args` become a small form |
| `events` | documentation for the activity log |
| `category`, `icon`, `order` | grouping and sort order in the UI |

Reload with **Settings → Device types → Reload manifests**.  The card, its
controls, the add-device wizard and the API all understand the new type
immediately — no restart, no code change.

## 2. The driver — `micropython/shnode/devices/servo_cover.py`

```python
import machine

from ..compat import sleep_ms
from ..device import DeviceError
from .base import Driver


class ServoCover(Driver):
    type = "servo_cover"

    #: properties accepted from the hub.  Anything else in a cmd is rejected.
    settable = ("position", "open", "calibrate_ms")

    POLL_INTERVAL = 0.02

    def __init__(self, uid, config=None, *, name=None):
        super().__init__(uid, config, name=name)
        self.pin = int(self.config.get("pin", 10))
        self.min_us = int(self.config.get("min_us", 500))
        self.max_us = int(self.config.get("max_us", 2500))
        self._servo = None
        self._target_us = self.min_us
        self._current_us = self.min_us
        self._calibrate = 5000
        self.state = {"position": 0, "open": False, "calibrate_ms": 800}

    # -- hardware ------------------------------------------------------- #
    def setup(self):
        self._servo = machine.PWM(machine.Pin(self.pin), freq=50)
        self._write(self.min_us)

    def teardown(self):
        if self._servo is not None:
            try:
                self._servo.deinit()
            except Exception:
                pass
            self._servo = None

    def _write(self, pulse_us):
        # 50 Hz -> 20 ms period -> 1 us = 3.2768 duty units
        self._servo.duty_u16(int(pulse_us * 65535 / 20000))
        self._current_us = pulse_us

    def _pulse_for(self, percent):
        span = self.max_us - self.min_us
        return self.min_us + int(span * max(0, min(100, percent)) / 100)

    # -- protocol ------------------------------------------------------- #
    def apply(self, props):
        if "calibrate_ms" in props:
            self._calibrate = max(100, min(5000, int(props.pop("calibrate_ms"))))
            self.state["calibrate_ms"] = self._calibrate
        if "position" in props:
            percent = int(props.pop("position"))
            if not 0 <= percent <= 100:
                raise DeviceError("position must be 0..100")
            self.state["position"] = percent
            self.state["open"] = percent > 0
            self._target_us = self._pulse_for(percent)
        if "open" in props:
            opened = bool(props.pop("open"))
            self.state["position"] = 100 if opened else 0
            self.state["open"] = opened
            self._target_us = self._pulse_for(self.state["position"])
        if props:
            raise DeviceError("unsupported properties %s" % (sorted(props),))
        # The servo moves in the background so the network loop never blocks.
        self.request_report()

    async def loop(self):
        step = (self.max_us - self.min_us) // 40 or 1
        if self._current_us == self._target_us:
            return
        if self._current_us < self._target_us:
            self._write(min(self._target_us, self._current_us + step))
        else:
            self._write(max(self._target_us, self._current_us - step))
        await sleep_ms(20)

    # -- actions -------------------------------------------------------- #
    async def action_calibrate(self):
        for pulse in (self.min_us, self.max_us, self.min_us):
            self._write(pulse)
            await sleep_ms(self._calibrate // 2)

    def snapshot(self):
        return {
            "position": int(self.state["position"]),
            "open": bool(self.state["open"]),
            "calibrate_ms": int(self._calibrate),
        }
```

Notes on the driver contract:

* `apply()` runs synchronously inside the network loop: keep it short.  Start a
  task (or let `loop()` do the work) for anything that takes time.
* Raise `DeviceError` for anything the hardware cannot do — it becomes a
  negative ack and a toast in the UI.
* `snapshot()` is what the hub stores as *reported*.  Include every property
  the driver really knows; omit what it does not (the hub leaves it alone).
* `self.request_report()` tells the hub a property changed.  Changes made in
  `apply()` are acked automatically, but changes from `loop()` or an action
  need this.
* `await self.emit("stalled", {...})` adds an entry to the activity log.
* `poll_interval` (class attribute) sets how often `loop()` runs.
* Missing hardware raises `Unavailable` (a `DeviceError`): the device is marked
  with an error instead of taking the node down.

## 3. Register it — `micropython/main.py`

```python
from shnode.devices.servo_cover import ServoCover   # with the other imports

DEVICES = [
    {"type": "servo_cover", "uid": "living-blind", "name": "Living room blind",
     "config": {"pin": 10}},
    ...
]

registry = {
    ...
    "servo_cover": ServoCover,
}
```

## 4. Deploy and check

```bash
python tools/flash_node.py --port COM5 --node esp32-desk   # files only, keeps config.json
python -m pytest -q                                        # nothing broken
python scripts/ui_dump.py http://pi:8080 --key $KEY         # see the card as text
```

The first time the node connects it announces `living-blind`; the hub creates
it automatically, adopts its current position, and the card appears under
"Other" until you give it a room.

## Checklist for a new type

- [ ] `id` matches `type` in the driver class.
- [ ] Every property in `settable` exists in the manifest (and vice versa).
- [ ] `min`/`max` in the manifest match the driver's own validation.
- [ ] `snapshot()` returns the same keys every time.
- [ ] `setup()` can fail with `Unavailable` without raising anything else.
- [ ] Long work is in `loop()` or an action, never in `apply()`.
- [ ] `teardown()` turns the output off.
