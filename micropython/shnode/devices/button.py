"""Push button / digital input driver.

Polls the pin in its background loop and raises events (press, release, click,
double click, long press).  The hub stores them in its activity log; a future
automation rule can subscribe to them without touching this driver.
"""

from ..compat import ticks_diff, ticks_ms
from .base import Driver, make_input


class Button(Driver):
    type = "button"
    poll_interval = 0.02

    settable = ()

    def __init__(self, uid, config=None, *, name=None):
        super().__init__(uid, config, name=name)
        self.pin = int(self.config.get("pin", 9))
        self.invert = bool(self.config.get("invert", True))
        self.debounce_ms = max(5, int(self.config.get("debounce_ms", 50)))
        self.long_press_ms = max(200, int(self.config.get("long_press_ms", 800)))
        self._pin = None
        self._pressed = False
        self._last_change = 0
        self._press_started = 0
        self._last_release = 0
        self._presses = 0
        self._long_fired = False
        self.state = {"pressed": False, "presses": 0}

    def setup(self) -> None:
        self._pin = make_input(self.pin, pull_up=self.invert)
        self._pressed = self._read()
        self.state["pressed"] = self._pressed

    def teardown(self) -> None:
        self._pin = None

    def _read(self) -> bool:
        if self._pin is None:
            return False
        try:
            level = bool(self._pin.value())
        except (OSError, ValueError):
            return False
        return (not level) if self.invert else level

    async def loop(self) -> None:
        now = ticks_ms()
        pressed = self._read()
        if pressed != self._pressed:
            if ticks_diff(now, self._last_change) < self.debounce_ms:
                return
            self._last_change = now
            self._pressed = pressed
            self.state["pressed"] = pressed
            self.request_report()
            if pressed:
                self._press_started = now
                self._long_fired = False
                self._presses += 1
                self.state["presses"] = self._presses
                await self.emit("press", {"count": self._presses})
            else:
                held = ticks_diff(now, self._press_started)
                await self.emit("release", {"held_ms": held})
                if not self._long_fired:
                    double = ticks_diff(now, self._last_release) < 350
                    await self.emit("double_click" if double else "click", {})
                self._last_release = now
            return

        if self._pressed and not self._long_fired:
            if ticks_diff(now, self._press_started) >= self.long_press_ms:
                self._long_fired = True
                await self.emit("long_press", {"held_ms": self.long_press_ms})

    def snapshot(self) -> dict:
        return {"pressed": bool(self.state.get("pressed")), "presses": int(self.state.get("presses") or 0)}
