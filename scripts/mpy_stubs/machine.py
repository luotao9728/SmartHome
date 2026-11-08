"""Fake ``machine`` module for importing the firmware under a desktop
MicroPython build that has no ``machine.PWM``/``Pin``.

``scripts/mpy_imports.py`` adds this directory to ``sys.path`` first, so the
drivers can be constructed and exercised.  It is **never** copied to a board:
``tools/flash_node.py`` only copies ``micropython/``.
"""


class Pin:
    IN = 0
    OUT = 1
    PULL_UP = 2
    PULL_DOWN = 3

    def __init__(self, number, mode=None, pull=None, value=None):
        self.number = number
        self.mode = mode
        self.pull = pull
        self._value = 1 if value else 0

    def value(self, value=None):
        if value is None:
            return self._value
        self._value = 1 if value else 0

    def on(self):
        self._value = 1

    def off(self):
        self._value = 0

    def __call__(self):
        return self._value


class PWM:
    def __init__(self, pin, freq=1000, duty_u16=0):
        self._freq = int(freq)
        self._duty = int(duty_u16)
        self.pin = pin

    def freq(self, value=None):
        if value is None:
            return self._freq
        self._freq = int(value)

    def duty_u16(self, value=None):
        if value is None:
            return self._duty
        self._duty = max(0, min(65535, int(value)))

    def deinit(self):
        self._duty = 0


def unique_id():
    return b"\x01\x02\x03\x04\x05\x06"
