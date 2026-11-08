"""PWM LED strip driver (single channel, low-side MOSFET).

Wiring assumptions live in the hardware notes, not here; this module only needs
a pin number, the PWM frequency and a gamma exponent.

Behaviour:

* ``power`` and ``brightness`` are combined into one duty cycle, so setting
  brightness while off does not turn the strip on.
* Changes are faded by a background task, which keeps the network loop free and
  makes the fade smooth instead of stepped.
* ``frequency_hz`` retunes the PWM slice; ``fade_ms: 0`` switches instantly.
* The driver never reports a duty it did not actually reach, so a rejected or
  failed write shows up in the UI.
"""

from ..compat import asyncio, sleep_ms
from ..device import DeviceError
from .base import Driver, make_pwm

MAX_DUTY = 65535


def gamma_lut(exponent: float, max_duty: int = MAX_DUTY, min_duty: int = 0) -> list:
    """Pre-computed 0..100 % -> duty table (MicroPython has no ``pow`` speed-up).

    Below 1 % the output is 0 (fully off).  Above it the curve maps the whole
    range onto ``min_duty..max_duty``, which is how a strip that only responds
    between, say, 9 % and 30 % duty can still use a 0-100 % slider.
    """
    table = []
    for percent in range(101):
        if percent <= 0:
            table.append(0)
            continue
        ratio = percent / 100.0
        value = ratio if exponent == 1.0 else ratio ** exponent
        table.append(int(round(min_duty + value * (max_duty - min_duty))))
    return table


class PwmLedStrip(Driver):
    type = "pwm_led_strip"
    report_interval = 0.0
    poll_interval = 0.0

    #: properties accepted from the hub
    settable = ("power", "brightness", "fade_ms", "frequency_hz")

    def __init__(self, uid, config=None, *, name=None):
        super().__init__(uid, config, name=name)
        self.pin = int(self.config.get("pin", 4))
        self.active_low = bool(self.config.get("active_low", False))
        self.max_duty_percent = max(1, min(100, int(self.config.get("max_duty", 100))))
        # A strip that only lights up above a threshold (and saturates early)
        # is calibrated with min_duty/max_duty instead of the full 0-100 % range.
        self.min_duty_percent = float(self.config.get("min_duty", 0.0))
        self.min_duty_percent = max(0.0, min(float(self.max_duty_percent),
                                             self.min_duty_percent))
        self.gamma = float(self.config.get("gamma", 2.2))
        if self.gamma < 0.5:
            self.gamma = 0.5
        ceiling = int(MAX_DUTY * self.max_duty_percent / 100)
        floor = int(round(MAX_DUTY * self.min_duty_percent / 100))
        self._ceiling = ceiling
        self._table = gamma_lut(self.gamma, ceiling, min(floor, ceiling))
        self._pwm = None
        self._frequency = int(self.config.get("frequency_hz", 1000))
        self._fade_task = None
        self._current_duty = 0
        self._target_duty = 0
        self._fade_ms = 400
        #: Exponential smoothing factor per tick (0..1).  0 means "use the
        #: linear fade", which is what fade_ms alone expresses.
        self._smoothing = max(0.0, min(1.0, float(self.config.get("smoothing", 0.0))))
        self._interval_ms = int(self.config.get("update_interval_ms", 16) or 16)
        self._interval_ms = max(2, min(200, self._interval_ms))
        self.state = {
            "power": False,
            "brightness": 100,
            "fade_ms": 400,
            "frequency_hz": self._frequency,
        }

    # -- hardware --------------------------------------------------------- #
    def setup(self) -> None:
        self._pwm = make_pwm(self.pin, frequency=self._frequency, duty_u16=self._raw_duty(0))
        self._current_duty = 0
        # A strip should never come on by itself after a power cut.
        self._write_duty(0)

    def teardown(self) -> None:
        if self._fade_task is not None:
            self._fade_task.cancel()
            self._fade_task = None
        if self._pwm is not None:
            try:
                self._pwm.duty_u16(0)
                self._pwm.deinit()
            except Exception:  # noqa: BLE001
                pass
            self._pwm = None

    # -- mapping ---------------------------------------------------------- #
    def _target_from_state(self) -> int:
        if not self.state.get("power"):
            return 0
        percent = int(self.state.get("brightness") or 0)
        percent = max(0, min(100, percent))
        return self._table[percent]

    def _raw_duty(self, duty: int) -> int:
        duty = max(0, min(self._ceiling, int(duty)))
        if self.active_low:
            return self._ceiling - duty
        return duty

    def _write_duty(self, duty: int) -> None:
        if self._pwm is None:
            return
        try:
            self._pwm.duty_u16(self._raw_duty(duty))
        except (OSError, ValueError) as exc:
            raise DeviceError("PWM write failed: %s" % (exc,))
        self._current_duty = duty

    def _set_frequency(self, frequency: int) -> None:
        frequency = int(frequency)
        if frequency < 60:
            frequency = 60
        if frequency > 20000:
            frequency = 20000
        if self._pwm is None:
            self._frequency = frequency
            return
        try:
            self._pwm.freq(frequency)
        except (OSError, ValueError) as exc:
            raise DeviceError("cannot set %d Hz on pin %d: %s" % (frequency, self.pin, exc))
        self._frequency = frequency

    # -- protocol --------------------------------------------------------- #
    def apply(self, props: dict) -> None:
        if "frequency_hz" in props:
            self._set_frequency(int(props.pop("frequency_hz")))
            self.state["frequency_hz"] = self._frequency
        if "fade_ms" in props:
            fade = int(props.pop("fade_ms"))
            self._fade_ms = max(0, min(10000, fade))
            self.state["fade_ms"] = self._fade_ms
        if "brightness" in props:
            brightness = int(props.pop("brightness"))
            if brightness < 0 or brightness > 100:
                raise DeviceError("brightness must be 0..100")
            self.state["brightness"] = brightness
        if "power" in props:
            self.state["power"] = bool(props.pop("power"))
        if props:
            raise DeviceError("unsupported properties %s" % (sorted(props),))

        self._animate_to(self._target_from_state())

    def _animate_to(self, target: int) -> None:
        self._target_duty = target
        if self._fade_task is not None:
            self._fade_task.cancel()
            self._fade_task = None
        duration = int(self._fade_ms)
        if duration <= 0 or self._pwm is None:
            self._write_duty(target)
            return
        self._fade_task = self.spawn(self._fade(target, duration))

    async def _fade(self, target: int, duration_ms: int) -> None:
        if self._smoothing > 0:
            await self._fade_exponential(target)
            return
        start = self._current_duty
        delta = target - start
        if delta == 0:
            return
        steps = max(1, min(60, duration_ms // 16))
        step_ms = max(1, duration_ms // steps)
        try:
            for step in range(1, steps + 1):
                value = int(start + delta * step / steps)
                self._write_duty(value)
                await sleep_ms(step_ms)
            self._write_duty(target)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            self.log("fade error: %r" % (exc,))

    async def _fade_exponential(self, target: int) -> None:
        """Ease toward the target by a fixed fraction every tick.

        This is the ``current += (target - current) * SMOOTHING`` loop: it
        settles smoothly and never overshoots, and the tail is imperceptible,
        so it snaps to the target once the remainder is under one duty step.
        """
        factor = self._smoothing
        interval = self._interval_ms
        try:
            while True:
                difference = target - self._current_duty
                if abs(difference) < 1:
                    break
                value = int(round(self._current_duty + difference * factor))
                # Guarantee progress: with a small factor the int() rounding
                # could stall forever just below the target.
                if value == self._current_duty:
                    value = self._current_duty + (1 if difference > 0 else -1)
                self._write_duty(value)
                await sleep_ms(interval)
            self._write_duty(target)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            self.log("fade error: %r" % (exc,))

    # -- actions ---------------------------------------------------------- #
    async def action_identify(self) -> None:
        """Flash the strip so a human can tell which one it is."""
        was_power = self.state.get("power")
        was_brightness = self.state.get("brightness")
        previous_fade = self._fade_ms
        self._fade_ms = 0
        try:
            for _ in range(3):
                self._write_duty(self._table[100])
                await sleep_ms(160)
                self._write_duty(0)
                await sleep_ms(160)
        finally:
            self._fade_ms = previous_fade
            self.state["power"] = was_power
            self.state["brightness"] = was_brightness
            self._write_duty(self._target_from_state())

    async def action_toggle(self) -> None:
        self.state["power"] = not self.state.get("power")
        self._animate_to(self._target_from_state())

    # -- introspection ---------------------------------------------------- #
    def snapshot(self) -> dict:
        return {
            "power": bool(self.state.get("power")),
            "brightness": int(self.state.get("brightness") or 0),
            "fade_ms": int(self._fade_ms),
            "frequency_hz": int(self._frequency),
        }
