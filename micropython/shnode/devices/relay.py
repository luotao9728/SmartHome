"""Relay / on-off output driver.

Also demonstrates three patterns other drivers reuse:

* a background task (``auto_off``),
* a non-blocking action (``pulse``) that temporarily overrides the state,
* raising :class:`DeviceError` for a command that cannot be honoured.
"""

from ..compat import asyncio, sleep_ms
from ..device import DeviceError
from .base import Driver, make_output


class Relay(Driver):
    type = "relay"
    report_interval = 0.0
    poll_interval = 0.0

    settable = ("power", "auto_off_s")

    def __init__(self, uid, config=None, *, name=None):
        super().__init__(uid, config, name=name)
        self.pin = int(self.config.get("pin", 5))
        self.active_low = bool(self.config.get("active_low", False))
        self._pin = None
        self._auto_off_task = None
        self._pulse_task = None
        self.state = {"power": False, "auto_off_s": 0}

    # -- hardware --------------------------------------------------------- #
    def setup(self) -> None:
        self._pin = make_output(self.pin, value=0)
        self._write(False)

    def teardown(self) -> None:
        for task in (self._auto_off_task, self._pulse_task):
            if task is not None:
                task.cancel()
        self._auto_off_task = None
        self._pulse_task = None
        if self._pin is not None:
            try:
                self._write(False)
            except Exception:  # noqa: BLE001
                pass
            self._pin = None

    def _write(self, on: bool) -> None:
        if self._pin is None:
            return
        level = (0 if on else 1) if self.active_low else (1 if on else 0)
        try:
            self._pin.value(level)
        except (OSError, ValueError) as exc:
            raise DeviceError("cannot drive pin %d: %s" % (self.pin, exc))

    # -- protocol --------------------------------------------------------- #
    def apply(self, props: dict) -> None:
        if "auto_off_s" in props:
            seconds = int(props.pop("auto_off_s"))
            if seconds < 0 or seconds > 86400:
                raise DeviceError("auto_off_s must be 0..86400")
            self.state["auto_off_s"] = seconds
        if "power" in props:
            power = bool(props.pop("power"))
            if power and self.state.get("power"):
                return
            self._switch(power)
        if props:
            raise DeviceError("unsupported properties %s" % (sorted(props),))
        if self.state.get("power") and self.state.get("auto_off_s"):
            self._schedule_auto_off(self.state["auto_off_s"])

    def _switch(self, on: bool) -> None:
        self._write(on)
        self.state["power"] = on
        if self._auto_off_task is not None:
            self._auto_off_task.cancel()
            self._auto_off_task = None
        if on and self.state.get("auto_off_s"):
            self._schedule_auto_off(self.state["auto_off_s"])
        # Report even when the change came from an action rather than the hub.
        self.request_report()

    def _schedule_auto_off(self, seconds: int) -> None:
        if self._auto_off_task is not None:
            self._auto_off_task.cancel()
        self._auto_off_task = self.spawn(self._auto_off(seconds))

    async def _auto_off(self, seconds: int) -> None:
        try:
            await sleep_ms(int(seconds * 1000))
        except asyncio.CancelledError:
            return
        if not self.state.get("power"):
            return
        self._write(False)
        self.state["power"] = False
        self.request_report()
        await self.emit("switched", {"power": False, "reason": "auto_off"})

    # -- actions ---------------------------------------------------------- #
    async def action_pulse(self, ms: int = 500) -> None:
        ms = max(50, min(10000, int(ms)))
        if self._pulse_task is not None:
            self._pulse_task.cancel()
        self._switch(True)
        self._pulse_task = self.spawn(self._pulse(ms))

    async def _pulse(self, ms: int) -> None:
        try:
            await sleep_ms(ms)
        except asyncio.CancelledError:
            return
        if self.state.get("power"):
            self._switch(False)
            self.request_report()

    def snapshot(self) -> dict:
        return {"power": bool(self.state.get("power")), "auto_off_s": int(self.state.get("auto_off_s") or 0)}
