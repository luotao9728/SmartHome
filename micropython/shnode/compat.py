"""Compatibility shims so the same firmware runs on MicroPython and CPython.

MicroPython exposes ``uasyncio``/``ujson``/``utime``; CPython only has the
unprefixed names.  Import everything from here and the rest of the code does not
care which interpreter it runs on (which is how the CI exercises the firmware).
"""

import sys

MICROPYTHON = sys.implementation.name == "micropython"

if MICROPYTHON:  # pragma: no cover - only executed on a board
    # Newer builds expose both names; older ones only ``uasyncio``.
    try:
        import asyncio  # noqa: F401
    except ImportError:
        import uasyncio as asyncio  # noqa: F401
    try:
        import json  # noqa: F401
    except ImportError:
        import ujson as json  # noqa: F401
    try:
        import time  # noqa: F401
    except ImportError:
        import utime as time  # noqa: F401
else:
    import asyncio  # noqa: F401
    import json  # noqa: F401
    import time  # noqa: F401


# --------------------------------------------------------------------------- #
# time helpers
# --------------------------------------------------------------------------- #
if MICROPYTHON:  # pragma: no cover
    def ticks_ms() -> int:
        return time.ticks_ms()

    def ticks_diff(a: int, b: int) -> int:
        return time.ticks_diff(a, b)

    def ticks_add(a: int, b: int) -> int:
        return time.ticks_add(a, b)

    async def sleep_ms(ms: int) -> None:
        await asyncio.sleep_ms(ms)

    def uptime_s() -> float:
        return time.ticks_diff(time.ticks_ms(), 0) / 1000.0
else:
    def ticks_ms() -> int:
        return int(time.monotonic() * 1000)

    def ticks_diff(a: int, b: int) -> int:
        return a - b

    def ticks_add(a: int, b: int) -> int:
        return a + b

    async def sleep_ms(ms: int) -> None:
        await asyncio.sleep(ms / 1000.0)

    _START = time.monotonic()

    def uptime_s() -> float:
        return time.monotonic() - _START


async def sleep(seconds: float) -> None:
    if MICROPYTHON:  # pragma: no cover
        await asyncio.sleep_ms(max(0, int(seconds * 1000)))
    else:
        await asyncio.sleep(seconds)


def create_task(coro, name=None):
    """``asyncio.create_task`` that also works on older MicroPython builds."""
    try:
        return asyncio.create_task(coro)
    except AttributeError:  # pragma: no cover - very old firmware
        return asyncio.ensure_future(coro)


def cancel_task(task) -> None:
    if task is None:
        return
    try:
        task.cancel()
    except Exception:  # noqa: BLE001
        pass


def is_cancelled_error(exc: BaseException) -> bool:
    name = type(exc).__name__
    return name in ("CancelledError", "GeneratorExit") or isinstance(exc, asyncio.CancelledError)


# --------------------------------------------------------------------------- #
# randomness
# --------------------------------------------------------------------------- #
# MicroPython's ``random`` module only guarantees ``getrandbits``/``seed``; the
# convenience helpers exist on CPython and on newer ports only.
try:
    from random import random as random_float  # noqa: F401
except ImportError:  # pragma: no cover - small ports
    import random as _random

    def random_float() -> float:
        return _random.getrandbits(30) / float(1 << 30)


def random_between(low: float, high: float) -> float:
    return low + (high - low) * random_float()


# --------------------------------------------------------------------------- #
# filesystem
# --------------------------------------------------------------------------- #
def read_json(path: str, default=None):
    try:
        with open(path, "r") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return default


def write_json(path: str, data) -> bool:
    """Write JSON atomically, so a power cut cannot leave a half file.

    ``os.rename`` cannot overwrite on Windows, so ``os.replace`` is used when
    the port provides it (CPython always does; MicroPython renames over).
    """
    tmp = path + ".tmp"
    try:
        with open(tmp, "w") as handle:
            json.dump(data, handle)
    except OSError:
        return False
    try:
        import os

        replace = getattr(os, "replace", None)
        if replace is not None:
            replace(tmp, path)
        else:
            try:
                os.remove(path)
            except OSError:
                pass
            os.rename(tmp, path)
        return True
    except OSError:
        return False


def file_exists(path: str) -> bool:
    try:
        import os

        os.stat(path)
        return True
    except OSError:
        return False
