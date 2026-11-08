"""Shared hardware helpers and the firmware side of a driver."""

from ..compat import MICROPYTHON
from ..device import Device, Unavailable


class Driver(Device):
    """Alias for :class:`Device`, kept for readable imports in drivers."""


def make_pwm(pin_number: int, *, frequency: int = 1000, duty_u16: int = 0):
    """Create a PWM output, or raise :class:`Unavailable` with a clear message."""
    if not MICROPYTHON:  # pragma: no cover - desktop simulation
        return SimulatedPwm(pin_number, frequency, duty_u16)
    try:
        from machine import PWM, Pin

        pwm = PWM(Pin(pin_number), freq=frequency, duty_u16=duty_u16)
        return pwm
    except (ValueError, OSError) as exc:
        raise Unavailable("cannot use pin %s for PWM: %s" % (pin_number, exc))


def make_output(pin_number: int, *, value: int = 0):
    if not MICROPYTHON:  # pragma: no cover
        return SimulatedPin(pin_number, value)
    try:
        from machine import Pin

        return Pin(pin_number, Pin.OUT, value=1 if value else 0)
    except (ValueError, OSError) as exc:
        raise Unavailable("cannot use pin %s as output: %s" % (pin_number, exc))


def make_input(pin_number: int, *, pull_up: bool = True):
    if not MICROPYTHON:  # pragma: no cover
        return SimulatedPin(pin_number, 1 if pull_up else 0)
    try:
        from machine import Pin

        if pull_up:
            return Pin(pin_number, Pin.IN, Pin.PULL_UP)
        return Pin(pin_number, Pin.IN)
    except (ValueError, OSError) as exc:
        raise Unavailable("cannot use pin %s as input: %s" % (pin_number, exc))


# --------------------------------------------------------------------------- #
# Desktop fakes: they keep the same call surface so the drivers can be tested
# (and the UI demoed) without hardware.
# --------------------------------------------------------------------------- #
class SimulatedPwm:  # pragma: no cover - used off-device
    def __init__(self, pin, frequency=1000, duty_u16=0):
        self.pin = pin
        self._freq = frequency
        self._duty = duty_u16
        self.deinited = False

    def freq(self, value=None):
        if value is None:
            return self._freq
        self._freq = int(value)

    def duty_u16(self, value=None):
        if value is None:
            return self._duty
        self._duty = int(max(0, min(65535, value)))

    def deinit(self):
        self.deinited = True
        self._duty = 0


class SimulatedPin:  # pragma: no cover - used off-device
    def __init__(self, pin, value=0):
        self.pin = pin
        self._value = int(bool(value))

    def value(self, value=None):
        if value is None:
            return self._value
        self._value = int(bool(value))

    def on(self):
        self._value = 1

    def off(self):
        self._value = 0

    def __call__(self):
        return self._value
