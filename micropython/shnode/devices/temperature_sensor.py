"""Temperature sensor driver (DS18B20 on one-wire, plus a simulated source).

The simulated source makes the whole stack demoable on a desk: without a
``ds18b20`` module (or without hardware) the driver produces a slowly varying
temperature so the UI, history and thresholds can be exercised end to end.
"""

from ..compat import random_float, sleep_ms
from ..device import DeviceError
from .base import Driver, make_input

try:  # MicroPython only; absent on CPython and on boards without one-wire
    import onewire  # type: ignore
    import ds18x20  # type: ignore
except ImportError:  # pragma: no cover
    onewire = None
    ds18x20 = None


class TemperatureSensor(Driver):
    type = "temperature_sensor"
    poll_interval = 0.0  # the loop sleeps for ``interval_s`` itself

    settable = ("offset",)

    def __init__(self, uid, config=None, *, name=None):
        super().__init__(uid, config, name=name)
        self.pin = int(self.config.get("pin", 6))
        self.interval_s = max(1, int(self.config.get("interval_s", 10)))
        self.offset = 0.0
        self._sensor = None
        self._rom = None
        self._simulated = False
        self._sim_time = 0.0
        self._last_value = None
        self._conversion = None
        self.state = {"temperature": 0.0, "offset": 0.0}
        self.poll_interval = float(self.interval_s)

    # -- hardware --------------------------------------------------------- #
    def setup(self) -> None:
        if onewire is None or ds18x20 is None:
            self._simulated = True
            self.log("no one-wire support, simulating")
            self.state["temperature"] = 21.5
            self.offset = float(self.config.get("offset", 0.0))
            self.state["offset"] = self.offset
            return
        try:
            bus = onewire.OneWire(make_input(self.pin))
            self._sensor = ds18x20.DS18X20(bus)
            devices = self._sensor.scan()
            if not devices:
                raise DeviceError("no DS18B20 found on pin %d" % self.pin)
            self._rom = devices[0]
            self._sensor.convert_temp()
            self._conversion = self.interval_s * 1000
        except DeviceError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise DeviceError("one-wire setup failed: %r" % (exc,))

    def teardown(self) -> None:
        self._sensor = None
        self._rom = None

    # -- sampling --------------------------------------------------------- #
    def _sample(self) -> float:
        if self._simulated or self._sensor is None:
            # A slow sine plus a little noise: ~18..24 degC over 10 minutes.
            import math

            self._sim_time += self.interval_s
            value = 21.0 + 3.0 * math.sin(self._sim_time / 95.0) + (random_float() - 0.5) * 0.2
            return round(value, 2)
        try:
            self._sensor.convert_temp()
            return float(self._sensor.read_temp(self._rom))
        except Exception as exc:  # noqa: BLE001
            raise DeviceError("read failed: %r" % (exc,))

    async def loop(self) -> None:
        try:
            value = self._sample()
        except DeviceError as exc:
            self.log(str(exc))
            await sleep_ms(self.interval_s * 1000)
            return
        value = round(value + self.offset, 2)
        if self._last_value is None or abs(value - self._last_value) >= 0.05:
            self._last_value = value
            self.state["temperature"] = value
            self.request_report()
        await sleep_ms(self.interval_s * 1000)

    # -- protocol --------------------------------------------------------- #
    def apply(self, props: dict) -> None:
        if "offset" in props:
            offset = float(props.pop("offset"))
            if offset < -20 or offset > 20:
                raise DeviceError("offset must be -20..20")
            self.offset = offset
            self.state["offset"] = offset
        if props:
            raise DeviceError("unsupported properties %s" % (sorted(props),))

    def snapshot(self) -> dict:
        return {
            "temperature": round(float(self.state.get("temperature") or 0.0), 2),
            "offset": round(float(self.offset), 2),
        }
